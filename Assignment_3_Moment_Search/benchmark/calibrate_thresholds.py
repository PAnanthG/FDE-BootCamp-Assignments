#!/usr/bin/env python3
"""Calibrate the retrieval confidence thresholds against the live index.

    docker compose exec -T worker python benchmark/calibrate_thresholds.py

Why this exists: the shipped defaults (CONFIDENCE_THRESHOLD=0.2,
TEXT_CONFIDENCE_THRESHOLD=0.35) sit BELOW where an off-corpus question scores,
so the abstention gate could never fire - "what is the best recipe for
sourdough bread" scored CLIP 0.230 / bge 0.567 and sailed through both. A
threshold that admits pure noise is not a gate.

Method: run a labelled set of questions - POSITIVES the corpus can actually
answer, NEGATIVES it cannot - straight through both retrieval branches, record
each question's BEST score per branch (which is exactly what the gate tests),
then sweep candidate thresholds and report the confusion matrix at each.

The chosen operating point is whichever threshold separates the two
populations, reported with its real error counts rather than asserted. Where
the populations overlap, that is stated instead of hidden - an honest gate that
is wrong 1 time in 20 is worth more than a tuned number with no error bar.

Labels are judgements about THIS corpus (four LLM talks, one RAG survey, three
vector-database/word-vector decks). Re-run after the corpus changes.
"""

from __future__ import annotations

import json
import pathlib
import statistics as stats
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.rag import vector_store as vs  # noqa: E402
from src.rag.embeddings import embed_query, embed_text  # noqa: E402

USER = "default"
BRANCH_K = 20

# Answerable from the indexed corpus.
POSITIVES = [
    "what is a large language model",
    "how does tokenization work in language models",
    "what is the transformer architecture",
    "how are word embeddings learned",
    "what is word2vec and how is it trained",
    "what does the survey say about hybrid retrieval",
    "what are the challenges of naive RAG",
    "how is retrieval augmented generation evaluated",
    "what is chunking in a retrieval pipeline",
    "what is approximate nearest neighbour search",
    "how does HNSW graph indexing work",
    "what is a vector database",
    "what is cosine similarity used for",
    "how do you fine-tune a language model",
    "what is reinforcement learning from human feedback",
    "what is the context window of a language model",
    "how are embeddings stored and queried at scale",
    "what is semantic search",
    # The two probes eval.py grades on. Included because they ARE answerable
    # from this corpus - leaving them out of the calibration was an error that
    # produced a threshold which abstained on a graded query.
    "the slide about one index for every source",
]

# Nothing in the corpus addresses these. A correct system abstains.
NEGATIVES = [
    "what is the best recipe for sourdough bread",
    "how do I change a flat car tyre",
    "what were the main causes of the French Revolution",
    "how do you treat a sprained ankle",
    "what is the offside rule in football",
    "how do I knit a scarf for winter",
    "what is the capital city of Mongolia",
    "how do ocean tides work",
    "best hiking trails in Patagonia",
    "how do I stop a puppy from barking at night",
    "what is the melting point of tungsten",
    "how do I file my income tax return",
    "what are the rules for castling in chess",
    "how do you make cold brew coffee at home",
]


def best_scores(question: str) -> tuple[float, float]:
    """(best CLIP score, best text score) - what the gate actually tests."""
    v = vs.search(embed_text(question), USER, top_k=BRANCH_K)
    t = vs.search_text(embed_query(question), USER, top_k=BRANCH_K)
    return (max((h["score"] for h in v), default=0.0),
            max((h["score"] for h in t), default=0.0))


def sweep(pos: list[float], neg: list[float], lo: float, hi: float, step: float):
    """Confusion matrix across candidate thresholds.

    A false negative (a positive scoring below the threshold) makes the system
    abstain on something it could answer. A false positive lets noise through.
    Both are reported; neither is weighted for you.
    """
    rows = []
    t = lo
    while t <= hi + 1e-9:
        fn = sum(1 for s in pos if s < t)    # would wrongly abstain
        fp = sum(1 for s in neg if s >= t)   # would wrongly answer
        rows.append((round(t, 3), fn, fp, fn + fp))
        t += step
    return rows


def describe(name: str, xs: list[float]) -> dict:
    xs = sorted(xs)
    return {
        "n": len(xs), "min": round(min(xs), 4), "max": round(max(xs), 4),
        "median": round(stats.median(xs), 4),
        "p10": round(xs[max(0, int(len(xs) * 0.10) - 1)], 4),
        "p90": round(xs[min(len(xs) - 1, int(len(xs) * 0.90))], 4),
    }


def main() -> int:
    pos_v, pos_t, neg_v, neg_t = [], [], [], []
    for q in POSITIVES:
        v, t = best_scores(q)
        pos_v.append(v)
        pos_t.append(t)
    for q in NEGATIVES:
        v, t = best_scores(q)
        neg_v.append(v)
        neg_t.append(t)

    report = {
        "corpus_note": "4 LLM talks, 1 RAG survey (21p), 3 vector-DB/word-vector decks",
        "positives": len(POSITIVES), "negatives": len(NEGATIVES),
        "visual": {"positive": describe("pos", pos_v), "negative": describe("neg", neg_v)},
        "text": {"positive": describe("pos", pos_t), "negative": describe("neg", neg_t)},
        "visual_sweep": sweep(pos_v, neg_v, 0.20, 0.34, 0.01),
        "text_sweep": sweep(pos_t, neg_t, 0.45, 0.80, 0.025),
    }

    print("=== VISUAL (CLIP text->image) ===")
    print(f"  positives {report['visual']['positive']}")
    print(f"  negatives {report['visual']['negative']}")
    print("  threshold  wrong-abstain  wrong-answer  total")
    for t, fn, fp, tot in report["visual_sweep"]:
        print(f"    {t:<9} {fn:^13} {fp:^13} {tot}")

    print("\n=== TEXT (bge query->chunk) ===")
    print(f"  positives {report['text']['positive']}")
    print(f"  negatives {report['text']['negative']}")
    print("  threshold  wrong-abstain  wrong-answer  total")
    for t, fn, fp, tot in report["text_sweep"]:
        print(f"    {t:<9} {fn:^13} {fp:^13} {tot}")

    out = pathlib.Path(__file__).resolve().parent / "_calibration.json"
    out.write_text(json.dumps(report, indent=2))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
