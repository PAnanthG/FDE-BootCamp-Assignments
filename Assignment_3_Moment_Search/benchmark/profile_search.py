#!/usr/bin/env python3
"""Time each stage of the read path, so a latency regression can be attributed.

    docker compose exec -T api python benchmark/profile_search.py [label]

`bench.py` measures the whole `/ask_stream` response, which is one number
covering five very different things - a CLIP text encode, a bge query encode,
two Qdrant round trips, and a hosted LLM call. When that number doubles under
ingest load it says nothing about WHERE the time went, and the obvious guesses
(CPU contention? Qdrant? the model provider?) are not distinguishable.

This times the stages individually against the same live services, so running it
idle and again during a backfill attributes the difference instead of guessing.
Deliberately excludes the LLM: that call is an external service our ingest load
cannot touch, and at ~88% of total latency it drowns everything else.
"""

from __future__ import annotations

import json
import statistics
import sys
import time

sys.path.insert(0, "/app")

from src import config  # noqa: E402
from src.rag import vector_store as vs  # noqa: E402
from src.rag.embeddings import embed_query, embed_text  # noqa: E402

QUERIES = [
    "what does the survey say about hybrid retrieval",
    "what is approximate nearest neighbour search",
    "how are embeddings used for similarity search",
    "what is a large language model",
]


def timed(fn):
    t0 = time.perf_counter()
    out = fn()
    return (time.perf_counter() - t0) * 1000.0, out


def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "unlabelled"
    stages = {"clip_text_encode": [], "bge_query_encode": [],
              "qdrant_visual_search": [], "qdrant_text_search": [], "total": []}

    for _ in range(10):
        for q in QUERIES:
            t_all = time.perf_counter()
            ms, cvec = timed(lambda: embed_text(q))       # CLIP text encoder
            stages["clip_text_encode"].append(ms)
            ms, tvec = timed(lambda: embed_query(q))      # bge query encoder
            stages["bge_query_encode"].append(ms)
            ms, _ = timed(lambda: vs.search(cvec, "default", top_k=config.BRANCH_TOP_K))
            stages["qdrant_visual_search"].append(ms)
            ms, _ = timed(lambda: vs.search_text(tvec, "default", top_k=config.BRANCH_TOP_K))
            stages["qdrant_text_search"].append(ms)
            stages["total"].append((time.perf_counter() - t_all) * 1000.0)

    def pct(xs, p):
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(len(xs) * p / 100))]

    report = {"label": label, "n": len(stages["total"]), "stages": {}}
    print(f"=== {label} (n={len(stages['total'])}) ===")
    print(f"  {'stage':<22} {'median':>9} {'p95':>9} {'max':>9}")
    for name, xs in stages.items():
        report["stages"][name] = {"median": round(statistics.median(xs), 1),
                                  "p95": round(pct(xs, 95), 1),
                                  "max": round(max(xs), 1)}
        print(f"  {name:<22} {statistics.median(xs):>8.1f}ms {pct(xs, 95):>8.1f}ms {max(xs):>8.1f}ms")
    print("JSON " + json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
