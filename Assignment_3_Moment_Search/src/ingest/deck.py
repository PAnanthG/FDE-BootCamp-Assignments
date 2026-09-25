"""Per-deck ingest pipeline - slides, including slides that are pure image.

pending -> fetching -> captioning -> chunking -> embedding -> indexed | failed

Same shape as the paper flow, with one extra stage that earns its place: a
slide is frequently a diagram with three words on it, so text extraction alone
indexes almost nothing and the deck retrieves badly. Image-heavy slides are
rendered and captioned by the vision model before chunking. The sample
scorecard docks exactly this failure ("image-only slides captioned thinly"),
so a caption that is merely present is not the goal - it has to describe the
slide well enough to retrieve on.

Same ordering invariant as the paper flow: upsert -> verify -> then status.
"""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from prefect import flow, task

from .. import db
from ..config import DECK_CAPTION_CONCURRENCY as CAPTION_CONCURRENCY
from . import docindex, docparse
from .fetch import scratch_dir
from .paper import PermanentIngestError, _download

_MAX_DECK_MB = 64
_CAPTION_RENDER_DPI = 110  # enough for text-in-image to survive downscaling

CAPTION_PROMPT = (
    "This is slide {n} of a presentation deck titled {title!r}. Describe what "
    "the slide communicates in 2-4 sentences, so that someone searching for "
    "this content could find it. Name any diagram, chart, or architecture "
    "shown and state what it depicts. Read out any text visible in the image. "
    "Do not preface your answer; return only the description."
)


@task(name="deck-fetch", retries=2, retry_delay_seconds=[30, 120])
def t_fetch(doc_id: str, user_id: str) -> str:
    db.set_status(doc_id, "fetching")
    row = db.get_video(doc_id)
    if row is None:
        raise ValueError(f"no manifest row for {doc_id}")

    suffix = ".pptx" if (row.get("url") or "").lower().endswith(".pptx") else ".pdf"
    try:
        path = _download(row, doc_id)
    except PermanentIngestError as exc:
        db.set_status(doc_id, "failed", error=str(exc))
        return ""
    if suffix == ".pptx":
        renamed = scratch_dir() / f"{doc_id}.pptx"
        path.replace(renamed)
        path = renamed

    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > _MAX_DECK_MB:
        path.unlink(missing_ok=True)
        raise ValueError(f"deck is {size_mb:.1f}MB, over the {_MAX_DECK_MB}MB limit")

    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    db.set_status(doc_id, "fetching", source_hash=source_hash)
    dup = db.find_duplicate(user_id, source_hash, exclude_id=doc_id)
    if dup:
        path.unlink(missing_ok=True)
        db.set_status(doc_id, "skipped", error=f"duplicate of {dup['id']}")
        return ""
    return str(path)


def _render_slide(pdf_path: str, page_index: int) -> bytes | None:
    """Render one slide to PNG bytes for the vision model.

    `llm.describe_image` re-encodes to JPEG and downscales to
    LLM_IMAGE_MAX_PX, so rendering at a generous DPI here costs nothing at the
    API and keeps small text in diagrams legible after the downscale.
    Returns None if the page cannot be rendered - a caption is best-effort.
    """
    try:
        import fitz

        with fitz.open(pdf_path) as doc:
            return doc[page_index].get_pixmap(dpi=_CAPTION_RENDER_DPI).tobytes("png")
    except Exception as exc:
        print(f"[deck] render failed for slide {page_index + 1}: {exc}")
        return None


@task(name="deck-caption", retries=1, retry_delay_seconds=30)
def t_caption(doc_id: str, path: str, title: str) -> list[dict]:
    """Extract slide text; caption the image-heavy ones with the vision model.

    Best-effort per slide: a vision timeout on slide 7 must not fail the deck.
    A slide that cannot be captioned keeps whatever text it had, and if that is
    nothing it is dropped downstream rather than indexed blank.
    """
    is_pptx = path.lower().endswith(".pptx")
    units = (docparse.parse_pptx_units(path) if is_pptx
             else docparse.parse_pdf_units(path))
    if not units:
        raise RuntimeError("deck has no slides")

    # PPTX cannot be rendered to an image without a converter, so captioning
    # only applies to the PDF path. A PPTX with image-only slides keeps its
    # speaker notes, which is usually where its content actually lives.
    needs_caption = [u for u in units if u.is_image_only]
    if needs_caption and is_pptx:
        print(f"[deck] {doc_id}: {len(needs_caption)} image-only slide(s); "
              "PPTX cannot be rendered for captioning - convert to PDF for "
              "full coverage")
        needs_caption = []

    captioned = 0
    if needs_caption:
        from .. import llm

        cfg = llm.env_config()
        if cfg is None:
            print(f"[deck] {doc_id}: no LLM configured - "
                  f"{len(needs_caption)} image-only slide(s) will not be indexed")
        else:
            db.set_status(doc_id, "captioning", progress=0.0)

            def caption_one(unit: docparse.Unit) -> tuple[int, str]:
                """Render + caption one slide. Never raises - a vision timeout
                on slide 7 must not fail the other 39."""
                rendered = _render_slide(path, unit.locator - 1)
                if not rendered:
                    return unit.locator, ""
                try:
                    return unit.locator, llm.describe_image(
                        rendered,
                        CAPTION_PROMPT.format(n=unit.locator, title=title or ""),
                        cfg,
                    ).strip()
                except Exception as exc:
                    print(f"[deck] {doc_id}: caption failed for slide "
                          f"{unit.locator} ({type(exc).__name__}: {exc})")
                    return unit.locator, ""

            # Captions are independent per slide and each is a round trip to a
            # hosted model, so wall-clock here is latency-bound, not CPU-bound:
            # a thread pool turns N serial round trips into ceil(N/workers).
            # Bounded by DECK_CAPTION_CONCURRENCY - unbounded fan-out just
            # trades wall-clock for provider rate-limit errors.
            workers = max(1, min(CAPTION_CONCURRENCY, len(needs_caption)))
            done = 0
            captions: dict[int, str] = {}
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for locator, caption in pool.map(caption_one, needs_caption):
                    captions[locator] = caption
                    done += 1
                    db.set_progress(doc_id, done / len(needs_caption))

            out: list[docparse.Unit] = []
            for unit in units:
                caption = captions.get(unit.locator, "")
                if caption:
                    captioned += 1
                    # The caption IS the slide's text from here on. Existing
                    # text (a stray page number) is kept ahead of it so nothing
                    # extracted is thrown away.
                    merged = f"{unit.text}\n{caption}".strip()
                    out.append(docparse.Unit(locator=unit.locator, text=merged,
                                             is_image_only=False))
                else:
                    out.append(unit)
            units = out

    print(f"[deck] {doc_id}: {len(units)} slides, {captioned} captioned")
    return [{"locator": u.locator, "text": u.text,
             "is_image_only": u.is_image_only} for u in units]


@task(name="deck-chunk")
def t_chunk(doc_id: str, units: list[dict]) -> list[dict]:
    db.set_status(doc_id, "chunking", progress=0.0)
    chunks = docparse.chunk_units(
        [docparse.Unit(locator=u["locator"], text=u["text"],
                       is_image_only=u["is_image_only"]) for u in units],
        kind="deck",
    )
    if not chunks:
        raise RuntimeError("no slide produced indexable text")
    print(f"[deck] {doc_id}: {len(chunks)} chunks")
    db.set_progress(doc_id, 1.0)
    return [{"text": c.text, "locator": c.locator, "idx": c.idx} for c in chunks]


@task(name="deck-embed-index", retries=2, retry_delay_seconds=60)
def t_embed_index(doc_id: str, user_id: str, chunks: list[dict]) -> int:
    """Embed, upsert, VERIFY, then commit - shared with the paper flow."""
    return docindex.embed_and_index(doc_id, user_id, chunks, kind="deck")


@flow(name="ms-ingest-deck", log_prints=True, timeout_seconds=3600)
def ingest_deck(doc_id: str, user_id: str) -> dict:
    attempt = db.bump_attempts(doc_id)
    path: str | None = None
    try:
        path = t_fetch(doc_id, user_id)
        if not path:
            row = db.get_video(doc_id) or {}
            print(f"[deck] {doc_id} terminal in fetch: {row.get('status')}")
            return {"doc_id": doc_id, "status": row.get("status")}
        row = db.get_video(doc_id) or {}
        units = t_caption(doc_id, path, row.get("title") or "")
        chunks = t_chunk(doc_id, units)
        n = t_embed_index(doc_id, user_id, chunks)
        print(f"[deck] {doc_id} indexed: {n} chunks (attempt {attempt})")
        return {"doc_id": doc_id, "chunks": n}
    except Exception as exc:
        db.set_status(doc_id, "failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if path:
            Path(path).unlink(missing_ok=True)
