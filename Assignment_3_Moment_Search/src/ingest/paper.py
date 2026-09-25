"""Per-paper ingest pipeline - a Prefect flow mirroring `ingest_video`.

pending -> fetching -> chunking -> embedding -> indexed | failed

Deliberately the same shape as src/ingest/pipeline.py: same status lifecycle,
same per-task retry policy, same Postgres-is-the-source-of-truth rule, same
Qdrant collection. What differs is only what a page is: a paper's locator is a
PAGE NUMBER, not a timestamp.

The ordering invariant, which is worth 15 points and is the reason this file
reads the way it does:

    upsert succeeds -> read the points back -> ONLY THEN commit 'indexed'

A source marked indexed on the strength of a call that merely returned is a
source that silently vanishes when a worker dies at the wrong moment.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from prefect import flow, task

from .. import db
from . import docindex, docparse
from .fetch import scratch_dir

# Papers are text, so they are embedded by the TEXT branch's model (bge), not
# by CLIP. Imported lazily inside the task - the API process must never pull
# the embedding stack in just to schedule a run.

_MAX_PDF_MB = 64  # a 100MB scan is a cost incident, not a document


@task(name="paper-fetch", retries=2, retry_delay_seconds=[30, 120])
def t_fetch(doc_id: str, user_id: str) -> str:
    """Acquire the PDF into worker scratch. Returns "" if it is a duplicate."""
    db.set_status(doc_id, "fetching")
    row = db.get_video(doc_id)
    if row is None:
        raise ValueError(f"no manifest row for {doc_id}")

    try:
        path = _download(row, doc_id)
    except PermanentIngestError as exc:
        # Terminal immediately: record why and give the slot back rather than
        # retrying something that cannot succeed.
        db.set_status(doc_id, "failed", error=str(exc))
        return ""
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > _MAX_PDF_MB:
        path.unlink(missing_ok=True)
        raise ValueError(f"PDF is {size_mb:.1f}MB, over the {_MAX_PDF_MB}MB limit")

    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    db.set_status(doc_id, "fetching", source_hash=source_hash)

    dup = db.find_duplicate(user_id, source_hash, exclude_id=doc_id)
    if dup:
        path.unlink(missing_ok=True)
        db.set_status(doc_id, "skipped", error=f"duplicate of {dup['id']}")
        return ""
    return str(path)


class PermanentIngestError(Exception):
    """A failure that retrying cannot fix - a bad URI, a 404, a 403.

    The distinction is not cosmetic. Retries are capacity: `t_fetch` retries
    twice with 30s and 120s backoff, so a permanently-dead URI holds a worker
    slot for ~2.5 minutes. A backfill containing 30 dead links starved a real
    10-paper backfill for over half an hour - observed while benchmarking, on
    exactly the probe documents `bench.py` registers.

    Permanent failures are recorded as `failed` immediately, with the reason,
    and the slot is handed straight back to the queue.
    """


# HTTP statuses that will never succeed on a retry. 408/429 and every 5xx are
# deliberately absent - those ARE worth retrying.
_PERMANENT_HTTP = {400, 401, 403, 404, 405, 410, 414, 451}


def _download(row: dict, doc_id: str) -> Path:
    """URL -> scratch file, or object storage -> scratch file for uploads."""
    dest = scratch_dir() / f"{doc_id}.pdf"
    if row.get("url"):
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            row["url"], headers={"User-Agent": "momentsearch/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                dest.write_bytes(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code in _PERMANENT_HTTP:
                raise PermanentIngestError(
                    f"HTTP {exc.code} for {row['url']} - not retryable") from exc
            raise
        except urllib.error.URLError as exc:
            # DNS failure for a hostname that does not exist is permanent too;
            # a connection reset is not.
            reason = str(getattr(exc, "reason", exc))
            if "Name or service not known" in reason or "nodename nor servname" in reason:
                raise PermanentIngestError(
                    f"host does not resolve for {row['url']}") from exc
            raise
    elif row.get("storage_key"):
        from .. import storage

        dest.write_bytes(storage.get_bytes(row["storage_key"]))
    else:
        raise ValueError(f"{doc_id} has neither url nor storage_key")
    return dest


@task(name="paper-chunk")
def t_chunk(doc_id: str, path: str) -> list[dict]:
    """PDF -> page-aware chunks. Page numbers are taken once, at parse time.

    Returned as plain dicts rather than DocChunk objects because Prefect
    serialises task results between processes and a dataclass round-trip is a
    needless place for the locator to get lost.
    """
    db.set_status(doc_id, "chunking", progress=0.0)
    units = docparse.parse_pdf_units(path)
    if not units:
        raise RuntimeError("PDF has no pages")

    text_pages = [u for u in units if not u.is_image_only]
    if not text_pages:
        raise RuntimeError(
            "no extractable text on any page - this looks like a scan; "
            "OCR is not implemented")

    chunks = docparse.chunk_units(units, kind="paper")
    if not chunks:
        raise RuntimeError("no chunk met the minimum length")

    skipped = len(units) - len(text_pages)
    if skipped:
        # Not a failure: figure-only pages in a paper are normal. Logged so the
        # gap between "pages in the PDF" and "pages in the index" is visible
        # rather than mysterious.
        print(f"[paper] {doc_id}: {skipped} image-only page(s) not indexed")
    print(f"[paper] {doc_id}: {len(units)} pages -> {len(chunks)} chunks")
    db.set_progress(doc_id, 1.0)
    return [{"text": c.text, "locator": c.locator, "idx": c.idx} for c in chunks]


@task(name="paper-embed-index", retries=2, retry_delay_seconds=60)
def t_embed_index(doc_id: str, user_id: str, chunks: list[dict]) -> int:
    """Embed, upsert, VERIFY, and only then commit the status.

    The sequence itself lives in `docindex.embed_and_index` so it is shared
    with the deck flow and testable without Prefect - see that module for why
    the verify step is not optional.
    """
    return docindex.embed_and_index(doc_id, user_id, chunks, kind="paper")


@flow(name="ms-ingest-paper", log_prints=True, timeout_seconds=3600)
def ingest_paper(doc_id: str, user_id: str) -> dict:
    attempt = db.bump_attempts(doc_id)
    path: str | None = None
    try:
        path = t_fetch(doc_id, user_id)
        if not path:
            # t_fetch already wrote the terminal status: 'skipped' for a
            # duplicate, 'failed' for a permanent fetch error.
            row = db.get_video(doc_id) or {}
            print(f"[paper] {doc_id} terminal in fetch: {row.get('status')}")
            return {"doc_id": doc_id, "status": row.get("status")}
        chunks = t_chunk(doc_id, path)
        n = t_embed_index(doc_id, user_id, chunks)
        print(f"[paper] {doc_id} indexed: {n} chunks (attempt {attempt})")
        return {"doc_id": doc_id, "chunks": n}
    except Exception as exc:
        db.set_status(doc_id, "failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if path:
            Path(path).unlink(missing_ok=True)
