"""Document parsing and chunking - the shared spine of the paper and deck paths.

Everything here is a pure function over text. No Qdrant, no Postgres, no
Prefect, no model: that is what lets the payload-integrity test exercise the
whole parse -> chunk -> payload path without a running stack, and it is why
this module is separate from `paper.py` / `deck.py`, which do the I/O.

The one property this file exists to protect: **a chunk must carry the page or
slide it came from, all the way to the Qdrant payload.** The assignment README
names a dropped `page` payload as the most common failure, and 30 of 100 points
ride on locators being right. `tests/test_payload_integrity.py` asserts it.

Why chunking is written here rather than reused: the video path's chunker
(`transcript.chunk_cues`) groups caption cues into ~20-SECOND windows. A page
has no timeline, so there is nothing to reuse - the plan assumed a semantic
chunker in `src/rag/chunk.py` that does not exist in this repo.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# A page or slide yielding fewer than this many characters of extractable text
# is treated as image-only. Rationale for 40: a text-bearing slide almost always
# carries a title plus a bullet (comfortably 40+ chars), while a full-bleed
# picture slide typically extracts 0 chars - or a handful from a page-number or
# footer artefact, which is exactly what a 0 threshold would miss. Deck ingest
# sends these to the vision model for a caption instead of indexing near-nothing.
IMAGE_ONLY_MAX_CHARS = 40

# Chunk sizing, in characters. Sentence-boundary aware, so a chunk is not cut
# mid-clause; overlap keeps a claim that straddles the split retrievable from
# either side. Characters rather than tokens deliberately - bge is the embedder
# and this needs no tokenizer at parse time.
#
# Sizing is PER KIND, because a page and a slide are not the same object:
#
#   paper 1200/150 - a journal page is dense continuous prose. One global 900
#     produced 161 chunks from 21 pages (~7.7 per page), which fragments an
#     argument across several vectors and makes each one a weak match for the
#     question it actually answers. 1200 keeps a claim and its justification
#     together; the 150 overlap covers a claim split across the boundary.
#
#   deck 1800/100 - a slide is already an authored unit and is usually far
#     shorter than this, so the effect of the larger budget is that a slide
#     stays ONE chunk. Splitting a slide would give two citations pointing at
#     the same slide number, which is noise in a citation list. Overlap is
#     small because it only ever applies inside one long slide.
#
# Both are overridable by env (PAPER_CHUNK_CHARS etc.) so Stage 7 can tune
# against measured recall rather than taste.
DEFAULT_MAX_CHARS = 1200
DEFAULT_OVERLAP_CHARS = 150
MIN_CHUNK_CHARS = 24  # below this a chunk is noise, not a retrievable claim


def chunk_profile(kind: str) -> tuple[int, int]:
    """(max_chars, overlap_chars) for a kind, from config with sane fallbacks.

    Imported lazily so this module keeps working with no app config present -
    the payload-integrity tests exercise it standalone.
    """
    try:
        from .. import config

        if kind == "deck":
            return (config.DECK_CHUNK_CHARS, config.DECK_CHUNK_OVERLAP)
        return (config.PAPER_CHUNK_CHARS, config.PAPER_CHUNK_OVERLAP)
    except Exception:
        return ((1800, 100) if kind == "deck"
                else (DEFAULT_MAX_CHARS, DEFAULT_OVERLAP_CHARS))

KINDS = ("paper", "deck")
LOCATOR_FIELD = {"paper": "page", "deck": "slide"}


@dataclass(frozen=True)
class Unit:
    """One page of a paper, or one slide of a deck, as extracted.

    `locator` is 1-based and is the number a human sees in a PDF reader or a
    slide footer - the whole point of the citation.
    """

    locator: int
    text: str
    is_image_only: bool = False


@dataclass(frozen=True)
class DocChunk:
    """One retrievable passage, attributed to exactly one locator.

    There is no `spans` field, and that is the design: a chunk is never built
    from more than one page or slide, so its locator is exact by construction
    rather than by attribution rule. See `chunk_units`.
    """

    text: str
    locator: int
    idx: int


def chunk_point_id(doc_id: str, idx: int) -> str:
    """Deterministic Qdrant point id.

    Same uuid5 scheme as the video path, with a `doc` discriminator so a
    document chunk can never collide with a frame (`<id>:<n>`) or a transcript
    chunk (`<id>:text:<n>`). Determinism is what makes a re-run after a crash
    overwrite rather than duplicate - Stage 8's no-loss proof depends on it.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{doc_id}:doc:{idx}"))


# ── parsing ──────────────────────────────────────────────────────────────────

def _normalise(text: str) -> str:
    """Collapse PDF extraction artefacts without destroying sentence structure.

    De-hyphenates words broken across a line ("hy-\nphen" -> "hyphen"), which
    otherwise embed as two nonsense tokens, then squeezes runs of whitespace.
    """
    text = text.replace("­", "")                    # soft hyphen
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)         # de-hyphenate line break
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def parse_pdf_units(path: str | Path) -> list[Unit]:
    """PDF -> one Unit per page, in order, 1-based.

    Page numbers are taken at the very first step and never recomputed - the
    cheapest way to guarantee they cannot drift is never to derive them twice.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - environment problem
        raise RuntimeError(
            "PyMuPDF is required to parse PDFs: pip install pymupdf") from exc

    units: list[Unit] = []
    with fitz.open(str(path)) as doc:
        for number, page in enumerate(doc, start=1):
            text = _normalise(page.get_text("text") or "")
            units.append(Unit(locator=number, text=text,
                              is_image_only=len(text) < IMAGE_ONLY_MAX_CHARS))
    return units


def parse_pptx_units(path: str | Path) -> list[Unit]:
    """PPTX -> one Unit per slide, 1-based, including speaker notes.

    Notes are included because a deck's slide is often three words on screen
    and the actual claim sits in the notes; excluding them makes decks retrieve
    badly for reasons that have nothing to do with the index.
    """
    try:
        from pptx import Presentation
    except ImportError as exc:
        raise RuntimeError(
            "python-pptx is required to parse PPTX decks: pip install python-pptx"
        ) from exc

    units: list[Unit] = []
    for number, slide in enumerate(Presentation(str(path)).slides, start=1):
        parts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                parts.append(shape.text_frame.text)
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                parts.append(f"Speaker notes: {notes}")
        text = _normalise("\n".join(parts))
        units.append(Unit(locator=number, text=text,
                          is_image_only=len(text) < IMAGE_ONLY_MAX_CHARS))
    return units


# ── chunking ─────────────────────────────────────────────────────────────────

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n{2,}")


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(text) if s and s.strip()]


def chunk_units(units: list[Unit], *, kind: str,
                max_chars: int | None = None,
                overlap_chars: int | None = None) -> list[DocChunk]:
    """Units -> retrievable chunks. **A chunk never spans two units.**

    That is the whole locator-precision decision, and it was made by a failing
    test rather than in the abstract. The first version of this function let
    chunks flow across page boundaries and attributed each to whichever page
    contributed the most characters. On the fixture - a paper whose first three
    pages are short - one 900-character chunk swallowed pages 1 through 4 and
    cited page 4. Page 1's text would have been served to a reader under a
    "page 4" citation, who would open page 4 and not find it. That is exactly
    the imprecise-locator defect the sample scorecard docks, and no attribution
    rule fixes it: dominant-page, first-page and last-page are all wrong for
    some chunk when a chunk is allowed to contain several whole pages.

    Chunking strictly within a unit makes the locator exact by construction.
    The cost is a claim split by a page break becoming two chunks; the overlap
    below keeps each half independently retrievable, and a half-claim cited to
    the right page beats a whole claim cited to the wrong one.

    Empty and image-only units contribute nothing. Deck ingest replaces
    image-only slides with a vision caption BEFORE calling this, so anything
    still empty here is genuinely contentless and is dropped rather than
    indexed as a blank vector that matches everything weakly.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    profile_max, profile_overlap = chunk_profile(kind)
    max_chars = profile_max if max_chars is None else max_chars
    overlap_chars = profile_overlap if overlap_chars is None else overlap_chars

    chunks: list[DocChunk] = []

    def emit(text: str, locator: int) -> None:
        text = text.strip()
        if len(text) >= MIN_CHUNK_CHARS:
            chunks.append(DocChunk(text=text, locator=locator, idx=len(chunks)))

    for unit in units:
        if not unit.text.strip():
            continue
        buf: list[str] = []
        size = 0
        for sentence in _split_sentences(unit.text):
            # A single "sentence" longer than the budget is common in decks,
            # where a whole bullet list extracts as one unpunctuated line.
            # Hard-split rather than blow past max_chars.
            for piece in _hard_split(sentence, max_chars):
                if size and size + len(piece) + 1 > max_chars:
                    previous = " ".join(buf)
                    emit(previous, unit.locator)
                    tail = previous[-overlap_chars:] if overlap_chars > 0 else ""
                    buf = [tail] if len(tail) >= MIN_CHUNK_CHARS else []
                    size = len(tail) if buf else 0
                buf.append(piece)
                size += len(piece) + 1
        emit(" ".join(buf), unit.locator)

    # idx is assigned in emit() as a running count, so it stays dense and
    # unique across the whole document even though chunking is per-unit.
    return chunks


def _hard_split(sentence: str, max_chars: int) -> list[str]:
    if len(sentence) <= max_chars:
        return [sentence]
    return [sentence[i:i + max_chars] for i in range(0, len(sentence), max_chars)]


# ── payloads ─────────────────────────────────────────────────────────────────

def build_payloads(chunks: list[DocChunk], *, user_id: str, doc_id: str,
                   kind: str, embed_version: str) -> list[dict]:
    """Chunks -> Qdrant payloads for the shared text collection.

    Shape notes, all of them load-bearing:

    * `video_id` carries the DOCUMENT id. It is the field every existing
      filter, delete and metadata join already keys on
      (`vector_store._user_filter`, `db.videos_by_ids`), so documents ride the
      video path's machinery unchanged instead of forking it. The name is now a
      misnomer; renaming it would touch the provided video pipeline, which is a
      red line. Recorded in DECISIONS.md rather than "fixed".
    * `modality: "text"` so the existing transcript branch retrieves documents
      with no change to its query path.
    * `kind` distinguishes paper/deck from video. Video points predate this
      field, so absent `kind` must be read as "video" downstream.
    * `page` / `slide` is what the citation renders and what `eval.py` asserts
      on (`locator.page`, `locator.slide`).
    * `loc` duplicates it kind-agnostically. Retrieval groups hits into windows
      by time for video; documents have no timeline, so `loc` is the field the
      grouping key uses instead. Duplicated rather than derived so a consumer
      never has to branch on kind just to read a number.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    locator_field = LOCATOR_FIELD[kind]

    payloads: list[dict] = []
    for chunk in chunks:
        payload = {
            "user_id": user_id,
            "video_id": doc_id,
            "modality": "text",
            "kind": kind,
            locator_field: chunk.locator,
            "loc": chunk.locator,
            "idx": chunk.idx,
            "text": chunk.text,
            "embed_version": embed_version,
        }
        payloads.append(payload)
    return payloads
