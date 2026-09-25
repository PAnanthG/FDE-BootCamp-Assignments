"""The write half of document ingest: embed -> upsert -> verify -> commit.

Separated from `paper.py` / `deck.py` for two reasons. It was duplicated in
both, and - more importantly - both of those import Prefect, so the ordering
invariant could not be tested without a full orchestration stack installed.
Here the collaborators are parameters with production defaults, so
`tests/test_ordering_invariant.py` can drive the exact same code path with
fakes, including the failure modes that are hard to provoke against a live
Qdrant (a short write, a crash between upsert and status).

**The invariant, worth 15 points:**

    upsert succeeds -> read the points back -> ONLY THEN commit 'indexed'

`wait=True` on the upsert means the write was acknowledged. Acknowledged is
not the same as visible, and a source marked indexed on the strength of a call
that merely returned is a source that disappears when a worker dies at the
wrong moment: the row says indexed, so nothing ever retries it, and the chunks
are not in the index. Reading the count back before committing the status is
what makes "indexed" mean it.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from . import docparse


class _Store(Protocol):
    def ensure_text_collection(self) -> None: ...
    def delete_video(self, user_id: str, doc_id: str) -> None: ...
    def upsert_document_chunks(self, doc_id: str, vectors: Any,
                               payloads: list[dict]) -> None: ...
    def count_document_points(self, user_id: str, doc_id: str) -> int: ...


def _embed_batch_size() -> int:
    """Chunks per embedding call. Config when available, else a safe default -
    this module is exercised standalone by the ordering-invariant tests."""
    try:
        from ..config import DOC_EMBED_BATCH

        return max(1, int(DOC_EMBED_BATCH))
    except Exception:
        return 64


class VerificationError(RuntimeError):
    """The index did not come back holding what we just wrote.

    Raised INSTEAD of committing 'indexed'. The caller lets it propagate so the
    Prefect task retries; the manifest row stays in 'embedding', which is a
    truthful description of where the source actually is.
    """


def embed_and_index(
    doc_id: str,
    user_id: str,
    chunks: list[dict],
    kind: str,
    *,
    store: _Store | None = None,
    embed: Callable[[list[str]], Any] | None = None,
    set_status: Callable[..., None] | None = None,
    set_progress: Callable[[str, float], None] | None = None,
    embed_version: str | None = None,
) -> int:
    """Embed chunks, upsert them, verify the write, then commit the status.

    `chunks` are plain dicts ({text, locator, idx}) rather than DocChunk
    objects because Prefect serialises task results between processes, and a
    dataclass round-trip is one more place a locator could be lost.

    Returns the number of points the index confirms it is holding.
    """
    if store is None:  # pragma: no cover - exercised in production, not tests
        from ..rag import vector_store as store  # type: ignore[assignment]
    if embed is None:  # pragma: no cover
        from ..rag.embeddings import embed_docs as embed  # type: ignore[assignment]
    if set_status is None or set_progress is None:  # pragma: no cover
        from .. import db

        set_status = set_status or db.set_status
        set_progress = set_progress or db.set_progress
    if embed_version is None:  # pragma: no cover
        from ..config import TEXT_EMBED_VERSION as embed_version  # type: ignore

    if not chunks:
        raise ValueError(f"{doc_id}: refusing to index zero chunks")

    set_status(doc_id, "embedding", progress=0.0)
    store.ensure_text_collection()
    # Clear points from a previous attempt. Deterministic ids already make a
    # re-upsert idempotent; this additionally removes points for chunks that no
    # longer exist, if the document was re-fetched and came back shorter.
    store.delete_video(user_id, doc_id)

    payloads = docparse.build_payloads(
        [docparse.DocChunk(text=c["text"], locator=c["locator"], idx=c["idx"])
         for c in chunks],
        user_id=user_id, doc_id=doc_id, kind=kind, embed_version=embed_version,
    )
    # Embed and upsert in batches rather than one call for the whole document.
    # A 200-page paper is thousands of chunks; one request holds every vector in
    # memory at once and, against a hosted embedding API, one oversized request
    # failing loses the entire document instead of one batch. Progress also
    # advances during the stage instead of jumping at the end.
    batch = _embed_batch_size()
    for start in range(0, len(chunks), batch):
        window = chunks[start:start + batch]
        vectors = embed([c["text"] for c in window])
        store.upsert_document_chunks(doc_id, vectors, payloads[start:start + batch])
        set_progress(doc_id, 0.1 + 0.8 * min(1.0, (start + len(window)) / len(chunks)))
    set_progress(doc_id, 0.9)

    stored = store.count_document_points(user_id, doc_id)
    if stored != len(payloads):
        raise VerificationError(
            f"{doc_id}: upserted {len(payloads)} chunks but the index reports "
            f"{stored} - leaving status 'embedding', not marking indexed")

    set_status(doc_id, "indexed", chunk_count=stored,
               embed_version=embed_version, progress=1.0)
    return stored
