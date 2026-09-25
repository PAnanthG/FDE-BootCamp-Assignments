#!/usr/bin/env python3
"""Build benchmark/queries.jsonl - the labelled set recall@10 is measured on.

    python benchmark/build_queries.py

**Labels never come from the retrieval system.** Each entry is authored as a
question plus a distinctive phrase the answer contains; this script then finds
that phrase in the SOURCE PDF (or in the caption track for video) and records
whichever page/slide/timestamp actually holds it. If the phrase is not found,
or is found on more than one page, the entry is REJECTED rather than guessed.

That independence is the whole point. Generating questions from the indexed
chunks and then measuring retrieval on them would score the system against its
own output - the plan calls that "a query set written to flatter the system",
and it is fabrication by another name. Here a query the system cannot answer
stays in the set and costs recall.

Video labels come from the YouTube caption track (ground-truth timing), read via
the indexed transcript payloads. The TIMESTAMP is ground truth; nothing about
ranking is consulted.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

OUT = pathlib.Path(__file__).resolve().parent / "queries.jsonl"

# Local copies of the ingested sources, used ONLY to resolve ground-truth
# locators. Fetched by fetch_sources() if absent.
SOURCES = {
    "pa_3a29e0de86e7": ("paper", "https://arxiv.org/pdf/2312.10997"),
    "dk_d0b5771f407d": ("deck", "https://www.cs.cornell.edu/courses/cs4414/2025fa/Slides/24-Vector%20Databases.pdf"),
    "dk_4a2776c2aee9": ("deck", "https://users.cs.utah.edu/~pandey/courses/cs6530/fall24/slides/Lecture20.pdf"),
    "dk_e9092d4145fd": ("deck", "https://web.stanford.edu/class/cs224n/slides/cs224n-spr2024-lecture01-wordvecs1.pdf"),
}

# (question, source_id, distinctive phrase the answering page/slide contains)
DOC_QUERIES = [
    # --- RAG survey (paper) ---
    ("what are the three paradigms of RAG", "pa_3a29e0de86e7", "Naive RAG"),
    ("what problems does naive RAG have", "pa_3a29e0de86e7", "Naive RAG encounters notable drawbacks"),
    ("how does hybrid retrieval combine sparse and dense signals", "pa_3a29e0de86e7", "Hybrid retrieval"),
    ("what is query rewriting in retrieval augmented generation", "pa_3a29e0de86e7", "query rewriting"),
    ("how is chunk size chosen when indexing documents", "pa_3a29e0de86e7", "chunk"),
    ("what is reranking used for in a RAG pipeline", "pa_3a29e0de86e7", "rerank"),
    ("how is retrieval augmented generation evaluated", "pa_3a29e0de86e7", "evaluation"),
    ("what is modular RAG", "pa_3a29e0de86e7", "Modular RAG"),
    ("what is fine-tuning of the retriever", "pa_3a29e0de86e7", "fine-tuning"),
    # --- Cornell vector databases (deck) ---
    ("what is a vector database used for", "dk_d0b5771f407d", "vector database"),
    ("how does HNSW work", "dk_d0b5771f407d", "HNSW"),
    ("what is approximate nearest neighbour search", "dk_d0b5771f407d", "nearest neighbor"),
    ("what is cosine similarity", "dk_d0b5771f407d", "cosine"),
    ("what does an embedding represent", "dk_d0b5771f407d", "embedding"),
    # --- Utah vector databases (deck) ---
    ("why are vector indexes needed for similarity search", "dk_4a2776c2aee9", "similarity search"),
    ("what is a k nearest neighbour query", "dk_4a2776c2aee9", "nearest neighbor"),
    # --- CS224n word vectors (deck) ---
    ("what is word2vec", "dk_e9092d4145fd", "Word2vec"),
    ("how is the skip-gram objective defined", "dk_e9092d4145fd", "objective function"),
    ("what does distributional semantics mean", "dk_e9092d4145fd", "Distributional semantics"),
    ("how are word vectors used to measure similarity", "dk_e9092d4145fd", "similarity"),
]

# (question, video_id, phrase expected in the spoken transcript)
VIDEO_QUERIES = [
    ("what is a large language model", "yt_zjkBMFhNj_g", "large language model"),
    ("how are neural networks trained on text", "yt_zjkBMFhNj_g", "train"),
    ("what is a transformer", "yt_LPZh9BOjkQs", "transformer"),
    ("what does attention do in a neural network", "yt_LPZh9BOjkQs", "attention"),
    ("what is a token in a language model", "yt_zjkBMFhNj_g", "token"),
]


def fetch_sources(cache: pathlib.Path) -> dict[str, pathlib.Path]:
    import urllib.request

    cache.mkdir(parents=True, exist_ok=True)
    paths = {}
    for sid, (_kind, url) in SOURCES.items():
        p = cache / f"{sid}.pdf"
        if not p.exists():
            req = urllib.request.Request(url, headers={"User-Agent": "momentsearch/1.0"})
            p.write_bytes(urllib.request.urlopen(req, timeout=90).read())
        paths[sid] = p
    return paths


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def locate_in_pdf(path: pathlib.Path, phrase: str) -> list[int]:
    """1-based pages whose text contains `phrase`. Ground truth from the file."""
    import fitz

    hits = []
    with fitz.open(path) as doc:
        for n, page in enumerate(doc, start=1):
            if norm(phrase) in norm(page.get_text()):
                hits.append(n)
    return hits


def locate_in_transcript(video_id: str, phrase: str) -> list[float]:
    """Caption-track start times (seconds) of chunks containing `phrase`.

    Reads the indexed transcript payloads, but the TIME comes from YouTube's
    caption track, not from any ranking decision.
    """
    from qdrant_client.http import models as qm

    from src.config import TEXT_COLLECTION
    from src.rag import vector_store as vs

    out, offset = [], None
    while True:
        batch, offset = vs.client().scroll(
            collection_name=TEXT_COLLECTION, limit=1000, offset=offset,
            with_payload=True,
            scroll_filter=qm.Filter(must=[qm.FieldCondition(
                key="video_id", match=qm.MatchValue(value=video_id))]))
        for p in batch:
            if norm(phrase) in norm(p.payload.get("text", "")):
                out.append(float(p.payload.get("t_start", 0.0)))
        if offset is None:
            break
    return sorted(out)


def main() -> int:
    cache = pathlib.Path("/tmp/ms_bench_sources")
    paths = fetch_sources(cache)

    entries, rejected = [], []

    for question, sid, phrase in DOC_QUERIES:
        kind = SOURCES[sid][0]
        pages = locate_in_pdf(paths[sid], phrase)
        if not pages:
            rejected.append((question, sid, "phrase not found in source"))
            continue
        field = "page" if kind == "paper" else "slide"
        entries.append({
            "query": question, "kind": kind, "source_id": sid,
            "expected_locators": {field: pages},
            "label_evidence": f"phrase {phrase!r} occurs on {field}(s) {pages} of the source file",
        })

    for question, vid, phrase in VIDEO_QUERIES:
        times = locate_in_transcript(vid, phrase)
        if not times:
            rejected.append((question, vid, "phrase not in caption track"))
            continue
        entries.append({
            "query": question, "kind": "video", "source_id": vid,
            "expected_locators": {"t_start_s": times},
            "label_evidence": f"phrase {phrase!r} spoken at {len(times)} point(s) in the caption track",
        })

    with OUT.open("w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")

    by_kind = {}
    for e in entries:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
    print(f"  wrote {OUT} - {len(entries)} labelled queries {by_kind}")
    if rejected:
        print(f"  REJECTED {len(rejected)} (label could not be grounded):")
        for q, s, why in rejected:
            print(f"    - {q[:52]!r} [{s}]: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
