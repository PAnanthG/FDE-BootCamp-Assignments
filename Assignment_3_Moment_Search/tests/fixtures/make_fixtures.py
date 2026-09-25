#!/usr/bin/env python3
"""Regenerate the test fixtures deterministically.

The generated files are committed - the tests must not depend on this script
running, or on a network. This exists so the fixtures can be regenerated and
reviewed rather than being opaque binaries nobody can reason about.

    python3 tests/fixtures/make_fixtures.py

Every page carries a unique sentinel string containing its own page number, so
a payload-integrity test can assert "the chunk whose text contains SENTINEL_P3
came back with page == 3" - which is exactly the failure mode the assignment
README names as the most common one.
"""

from __future__ import annotations

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent

# Page 4 is deliberately long enough to split into several chunks, so the tests
# can prove that EVERY chunk of a page keeps that page's locator - not just the
# first one. Page 5 is deliberately near-empty, standing in for an image-only
# page (a scanned figure, or a slide that is one full-bleed picture).
PAGES: list[str] = [
    # page 1
    "SENTINEL_P1 Retrieval Augmented Generation A Survey\n\n"
    "This paper surveys hybrid retrieval for question answering over "
    "heterogeneous corpora.",
    # page 2
    "SENTINEL_P2 Background\n\n"
    "Dense retrievers embed queries and passages into a shared vector space. "
    "Sparse retrievers match lexical overlap. Hybrid retrieval combines both.",
    # page 3
    "SENTINEL_P3 Hybrid Retrieval\n\n"
    "The survey says hybrid retrieval consistently outperforms either branch "
    "alone, because lexical and semantic matching fail on different queries.",
    # page 4 - long, forces multiple chunks
    "SENTINEL_P4 Evaluation\n\n" + (
        "Recall at ten is the headline metric for retrieval quality and it is "
        "reported across every corpus in the benchmark suite. "
    ) * 12,
    # page 5 - near-empty, stands in for an image-only page
    "SENTINEL_P5",
]

SLIDES: list[str] = [
    "SENTINEL_S1 One Index For Every Source",
    "SENTINEL_S2 Why a single collection\n\nVideos, papers and decks share one "
    "index so a single query can cite all three kinds.",
    # slide 3 has no text at all - the image-only case Stage 4 must caption
    "",
    "SENTINEL_S4 Locators\n\nVideo cites a timestamp, paper cites a page, deck "
    "cites a slide.",
]


def build_pdf(path: pathlib.Path, pages: list[str]) -> None:
    import fitz  # PyMuPDF

    doc = fitz.open()
    for body in pages:
        page = doc.new_page(width=595, height=842)  # A4
        if body:
            page.insert_textbox(fitz.Rect(56, 56, 539, 786), body,
                                fontname="helv", fontsize=11, align=0)
    doc.save(path, deflate=True)
    doc.close()


def build_image_only_pdf(path: pathlib.Path) -> None:
    """A one-page PDF whose only content is a drawn shape - no extractable text.

    Used to prove the deck path detects an image-only slide and captions it
    rather than silently indexing an empty string.
    """
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.draw_rect(fitz.Rect(100, 100, 495, 500), color=(0.1, 0.2, 0.6),
                   fill=(0.85, 0.9, 1.0), width=3)
    page.draw_circle(fitz.Point(297, 620), 90, color=(0.8, 0.2, 0.2),
                     fill=(1.0, 0.85, 0.85), width=3)
    doc.save(path, deflate=True)
    doc.close()


def main() -> int:
    try:
        import fitz  # noqa: F401
    except ImportError:
        print("PyMuPDF is required to regenerate fixtures: pip install pymupdf",
              file=sys.stderr)
        return 1

    build_pdf(HERE / "paper_5p.pdf", PAGES)
    build_pdf(HERE / "deck_4s.pdf", SLIDES)
    build_image_only_pdf(HERE / "slide_image_only.pdf")

    for name in ("paper_5p.pdf", "deck_4s.pdf", "slide_image_only.pdf"):
        size = (HERE / name).stat().st_size
        print(f"  wrote {name} ({size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
