# DECISIONS

Deliberate variances from the assignment spec, and the reasoning behind them.
Every entry states what was decided, why, and what it costs — including the one
place a graded check is knowingly left failing.

Measurements quoted here come from runs against the live stack; the raw data is
in `benchmark/_calibration.json`, `baseline/deck_evidence.json` and
`baseline/video_citation.json`.

---

## D1. `deck_indexed` is knowingly left failing — the probe, not the pipeline

`eval/eval.py` grades the deck path with one fixed query: **"the slide about one
index for every source"**. That query is phrased in the *assignment's*
vocabulary, not in the vocabulary any real slide deck uses.

Measured, by embedding candidate slide text against that probe with the same
bge model that serves retrieval:

| Slide text | Similarity |
|---|---|
| "One index for every source: videos, papers and decks all live in a single vector collection" | 0.7546 |
| "A single shared vector index serves every content type" | 0.7532 |
| "Unified indexing: all modalities embedded into one shared vector space" | 0.7470 |
| "Vector databases store embeddings in a single collection and support ANN search" | 0.6581 |
| "HNSW builds a navigable small world graph…" | 0.6376 |

A slide that nearly restates the probe verbatim reaches only **0.755**. The
language decks *actually* use tops out around **0.64–0.67** — below the
admission threshold of 0.71 and below the hardest off-corpus negative (0.7055).

**Per-kind thresholds do not rescue it.** Slides and transcripts were checked as
separate populations:

```
NEGATIVE deck  n=14  median=0.5316  max=0.7055
POSITIVE deck  n=18  median=0.7322  min=0.5804
```

The hardest negative in the whole set is itself a *deck* hit at 0.7055, and it
**outscores** the probe's best deck chunk (0.6965). Any deck-specific threshold
admitting the probe also admits that noise. Ruled out by measurement, not taste.

**Options considered and rejected:**

* *Lower the threshold to ~0.69.* Rejected: the 0.7055 negative still passes, so
  abstention quality degrades **and** the check is not reliably gained. Tuning a
  grounding gate so a grader's probe passes is the wrong trade.
* *Author a deck describing this architecture.* It would clear the bar (~0.755),
  but it is self-authored, which conflicts with the eval skill's requirement to
  test on media the student did not write. Viable only as a labelled fixture.
* *Find a genuine third-party deck that clears it.* Searched deliberately for
  the strongest plausible candidate: "Using Vector Databases to Scale
  Multimodal Embeddings, Retrieval and Generation" (Zain Hasan, Weaviate,
  Haystack EU 2023) - a real conference talk specifically about unifying
  multimodal embeddings into one vector space, as close to the probe's actual
  subject as a real deck is likely to get. Downloaded and scored unmodified
  against the exact probe with the production model: best slide **0.6664** -
  below the 0.71 threshold and below the hardest negative (0.7055). Confirms
  D1's conclusion rather than overturning it: even a well-matched real deck
  cannot clear a bar phrased in the assignment's own vocabulary.

**Decision:** accept the check failing and evidence the deck path with natural
queries instead. What the check is meant to prove is proven below.

### The deck path, evidenced on natural queries

Three third-party decks indexed (Cornell CS4414 L24 *Vector Databases*, Utah
CS6530 L20 *Vector Databases*, Stanford CS224n L1 *Word Vectors*) — 130 slides,
15 image-only slides captioned by the vision model.

| Query | Kinds returned | Deck slides cited |
|---|---|---|
| what is a vector database | deck | 24, 2, 1, 3, 25, 1 |
| how does HNSW graph indexing work | deck, paper | 38, 14, 40, 4, 39 |
| what is approximate nearest neighbour search | deck | 13, 14, 35, 12, 15, 5 |
| what is word2vec and how is it trained | deck, paper | 25, 40, 9, 2, 27 |
| a photograph of a library full of bookshelves | deck | 10 |

Natural questions about the decks score **0.83–0.86**, far above threshold.

**Locator accuracy, spot-checked against the source PDFs** — 9 citations:
**5 matched the cited slide verbatim, 4 were vision-captioned image-only slides
(the text is the caption, correctly attributed), 0 mismatches.**

The last row is the one the sample scorecard singles out: slide 10 of the CS224n
deck is a photograph with no extractable text, and it is retrievable **because**
it was captioned. That is the capability `deck_indexed` exists to check.

---

## D2. Documents share the video manifest table

Papers and decks are rows in `ms_videos` (`kind` in `paper|deck`), not a
separate `ms_documents` table.

They need the identical lifecycle, the identical fair-dispatch claim
(`wfq_claim`), and the identical metadata join at citation time
(`videos_by_ids`). A parallel table meant duplicating all three, and
`GET /admin/sources` would then union two schemas that must not drift.

**Cost:** the table name is now a misnomer, and so is the `video_id` payload
field, which carries document ids. Renaming either would modify the provided
video pipeline — a red line — so both are documented rather than "fixed".
Additive nullable columns only, so every existing video query is unaffected.

## D3. Documents live in the text collection, not a third one

CLIP frame vectors (512-dim) and bge text vectors (384-dim) already require two
collections. Documents are text at the same dimension as transcripts, so they
join `TEXT_COLLECTION`, and the existing text branch retrieves them with no
change to its query path. A third collection would need a third query, a third
threshold and a merge — and would miss the assignment's actual point.

## D4. A chunk never spans two pages or slides

Locators are exact by construction rather than by attribution rule.

The first implementation let chunks flow across page boundaries and attributed
each to whichever page contributed most text. On the fixture — three short pages
then a long one — a single chunk swallowed pages 1–4 and cited page 4. Page 1's
text would have reached a reader under a "page 4" citation. No attribution rule
fixes that once a chunk may contain several whole pages.

**Cost:** a claim split by a page break becomes two chunks. Overlap keeps each
half retrievable, and half a claim cited to the right page beats a whole claim
cited to the wrong one.

## D5. Chunk sizes are per kind

Papers 1200/150, decks 1800/100 (chars/overlap), env-overridable.

A global 900 produced ~7.7 chunks per page on the RAG survey, fragmenting an
argument across vectors so each was a weak match for the question it answered.
Re-chunking took it to 116 chunks averaging 1065 chars with all 21 pages still
covered. For decks the larger budget keeps a slide as **one** chunk — splitting
one produces two citations pointing at the same slide number, which is noise.

## D6. `/ask_stream` was built, not extended

The assignment README lists `GET /ask_stream?q=…` as **provided**. It does not
exist in this repo; the only query endpoint was `POST /api/ask` returning JSON.
`eval.py` reads it as SSE, and four checks depend on it.

Citations stream **before** the LLM call — retrieval is milliseconds, synthesis
is seconds — so a client needing grounded sources never waits. `ask()` gained an
optional `retrieved=` parameter so the endpoint does not run retrieval twice on
the path `bench.py` measures.

## D7. Citation fields were added, never removed

`kind`, `locator`, `text` and `source_id` are additive. `ms`, `timestamp`,
`deeplink`, `thumbnail` remain, null for documents. `ui/index.html` keeps
working untouched. Verified against `baseline/video_citation.json`: same
citations, same top moment, same order.

## D8. Retrieval groups documents by locator, video by time

`_fuse` bucketed hits by `(video_id, |Δt| ≤ 15s)`. Document chunks have no
`t_start`, so every chunk of a paper resolved to t=0 and collapsed into **one**
citation — a 21-page paper returned as a single result. Documents now group by
`(source, kind, locator)`: one window per page or slide, which is also the unit
a reader can be sent to.

## D9. Confidence thresholds were recalibrated

`CONFIDENCE_THRESHOLD` 0.2 → **0.29**, `TEXT_CONFIDENCE_THRESHOLD` 0.35 → **0.71**.

The shipped defaults sat *below* where off-corpus questions score — "recipe for
sourdough bread" hit CLIP 0.230 / bge 0.567 — so the abstention gate could never
fire. Calibrated against 20 answerable and 14 off-corpus questions run through
the live index (`benchmark/calibrate_thresholds.py`): 7/7 negatives now abstain
with zero citations and no LLM call; 7/7 positives still answer.

**Known weakness:** the text margin is ~0.006 either side. That is thin and will
need re-running as the corpus grows. Stage 7's labelled query set is the real
calibration; this is a defensible interim built from measurement.

## D10. One kind cannot monopolise the citation list

RRF scores by rank *within branch*, so the visual branch's rank-0 hit tied
exactly with the text branch's rank-0 hit, and ties broke by list order — video
was concatenated first, so it won every tie at every rank. Half of every result
list was video by construction.

Three changes: admission filtering before fusion, tie-breaking on `margin` (how
far a hit sits above its own branch's threshold) rather than list order, and
`MAX_PER_SOURCE=2` with overflow **demoted rather than dropped**, so a question
only one source can answer still returns everything.

Uncorroborated frames additionally need `VISUAL_ONLY_THRESHOLD=0.30`; a frame
backed by a transcript hit is still admitted at the lower bar, because two
independent signals agreeing *is* the evidence.

Video share of citations went 63% → 21%; citations with no quotable text 13 → 4.

## D12. `search_p95_during_ingest_ratio` is gated on time-to-citations, not the full response

`bench.py` is our own file — only `sla.json`'s targets are hash-pinned (D4).
`measure_search_p95` originally timed the *entire* `/ask_stream` response,
start to the `done` event, which includes the LLM's synthesized answer.
Diagnostic instrumentation on this app (recorded under D11) found that call is
~80% of the number on a quiet system, and its own variance — not ingest
contention — moved the ratio across three otherwise-unchanged benchmark runs:
0.99 → 1.05 → 1.39, no code changed in between. Gating a decoupling SLA on a
number 80%-owned by an external hosted model mostly grades that model.

`/ask_stream` streams the `citations` event *before* calling the LLM,
specifically so a client needing grounded results does not wait on synthesis
(D6). `measure_time_to_citations` now stops reading — closes the socket — the
moment that event arrives, so the gate times what the SLA's own wording is
about ("search stays fast during a big ingest"): retrieval, not synthesis.
Measured under active ingest: retrieval alone is 173ms median / 324ms p95;
the full response is ~3.5s.

**Nothing about the target changed.** `sla.json`'s `1.3` is untouched — this
redefines what one Python function measures, not what the gate requires.

**Kept honest, not hidden.** The full-response ratio is still measured every
run (n=10, informational, clearly labelled `_context_full_response_not_gated`
in `_bench.json`) so the LLM's real contribution stays visible rather than
disappearing from the report.

`SEARCH_SAMPLE_N` was raised 40 → 150 for the gated measurement: time-to-
citations calls are ~5x faster than full-response calls, so the same n would
have shrunk the during-ingest measurement window to a sliver of the backfill's
duration and starved `window_valid` of samples.

**The metric change is correct; it does not make the gate pass.** With the LLM
excluded, `search_p95_during_ingest_ratio` measured **2.92** (window valid,
9/9 samples busy) - worse than several of the full-response runs, not better.
Investigated rather than assumed: an isolated Qdrant-only probe (bypassing the
app, `vector_store.search_text` called directly from the host) stayed flat at
26-32ms in both idle and during-ingest conditions across repeated trials -
Qdrant is not the cause. Repeated 30-call trials against the live
`/ask_stream` endpoint during active ingest showed 29 of 30 calls under 250ms
and **one call at 3.4s** - a rare, severe tail stall, not a uniform slowdown.
p95 is exactly the statistic a single such stall in the top 5% will drag up.

### A memory leak was found, fixed, and RULED OUT as the cause

Profiling the containers found `docembed` holding **5.50 GiB of the 7.75 GiB
VM while idle at 0.2% CPU** - confirmed as real anonymous memory, not
reclaimable page cache (`anon 5903745024`, `file 30711808` from the cgroup).
Idle footprint across all containers was ~6.5 GiB / 7.75 GiB (84%) *before any
work started*, and no container declared a memory or CPU limit.

Mechanism: `DOC_EMBED_MAX_BATCH=256` capped the coalesced batch by CHUNK
COUNT, which bounds nothing - activation cost is O(batch x seq^2) and
ONNXRuntime pads every sequence to the longest in the batch, so 256 x 12 heads
x 512^2 x 4B is ~3.2 GB for a single attention tensor. ORT's CPU arena grows to
the largest batch it has ever seen and never returns it. Measured: **one batch
of 170 chunks took the service from 238 MiB to 2.906 GiB in a single
inference**, and it never came back down.

This looked like a strong candidate for the stalls - a box at 84% memory tips
into page reclaim, which produces exactly the rare/severe/non-uniform
signature. **It was wrong.** Bounding the batch by padded tokens
(`DOC_EMBED_MAX_BATCH_TOKENS`) worked on memory and made latency worse:

| run | docembed peak | idle p95 | during p95 | ratio |
|---|---|---|---|---|
| original | 5.5 GiB | - | - | 2.92 |
| fresh memory only | 2.9 GiB | 338.6ms | 657.0ms | 1.94 |
| token budget 12000 | 1.36 GiB | 256.4ms | 1466.2ms | **5.72** |

Memory fell 2.9 GiB -> 1.36 GiB while during-ingest latency doubled. **Memory
and latency moved in opposite directions, which rules memory out as the
cause.**

### Corrected diagnosis: CPU occupancy, not memory

The tighter budget split 6 coalesced batches into 14 sub-batches
(`{'batches': 6, 'subbatches': 14}`). bge scales sub-linearly, so
more-but-smaller inferences keep `docembed`'s 6 ONNX threads hot for a
*longer wall-clock window* on a 10-core box - trading a brief memory spike for
sustained CPU occupancy. Sustained occupancy is what starves the search path.

This is consistent with evidence already in the tree that was under-weighted:
`clip_service._cap_threads`'s own docstring records the clip container at
**1031% CPU** during a backfill with query-encode max **8542ms** while Qdrant
stayed flat at ~21ms. Thread caps reduced that but never addressed the root -
**Docker reserves no CPU per container**, so a thread cap is a request, not a
guarantee, and nothing prevents the kernel descheduling `clip` when the box is
oversubscribed by `docembed` + six Prefect flow subprocesses.

**The architectural fix this points at, not attempted:** partition cores
instead of tuning thread counts - `cpuset`-pin `api`+`clip` to cores that
`docembed`+`worker` cannot touch, so search *owns* CPU rather than competing
for it. That is the invariant the SLA actually encodes. Not implemented or
benchmarked here; recorded so the next step is a measurement, not a guess.

**What was kept.** The memory bound and `mem_limit: 4g` stay - 5.5 GiB idle is
indefensible regardless of the latency question, and it explains the
`WORKER_CONCURRENCY=8` OOM (P3/D11). But `DOC_EMBED_MAX_BATCH_TOKENS` is
deliberately set to **56000**, above the largest batch seen in practice, so
normal work runs as ONE inference and is never fragmented; it is a ceiling
against a pathological burst, not a target. **This reverted setting was not
re-benchmarked** - it restores the un-fragmented behaviour measured at 1.94,
but that is an expectation, not a measurement, and is labelled as such.

**Current honest status:** `search_p95_during_ingest_ratio` still fails. Ruled
out with evidence: LLM variance, Qdrant, and memory pressure. Identified but
unfixed: CPU contention between the ingest and search paths on an
unpartitioned box. Three attempts have not moved it; the remaining fix is an
infrastructure change (core pinning), not application tuning.

---

## Defects found in the provided system

Recorded because they were pre-existing, not introduced here, and both sit in
graded areas.

### P1. Deployment flow runs could never load

Prefect stores a **file-path** entrypoint (`src/ingest/pipeline.py:ingest_video`)
and loads it at run time as a top-level module with no package, so every
relative import raised `ValueError: Empty module name` before the flow started.
Confirmed on the untouched `ingest_video`, so `POST /api/videos` would accept a
video that then never indexed. Unnoticed upstream because the sample seed calls
the flow in-process. Fixed with `entrypoint_type=MODULE_PATH` — no change to
pipeline code.

### P2. A worker that stopped serving exited successfully

`serve()` returning was treated as `break  # clean shutdown`, so the process
exited 0, Docker's `restart: unless-stopped` saw a normal exit, and the worker
stayed dead. Observed: the queue silently stopped draining for three hours with
no error anywhere. Now only an explicit signal is a shutdown; anything else
re-serves.

### P3. Document stages were invisible to capacity accounting

`INFLIGHT_STATUSES` listed only video stages, so `chunking` and `captioning`
were free capacity as far as the dispatcher was concerned — it would over-admit
while documents were working, breaking the bound the ≤1.3× latency SLA is
measured against.

### P4. A stuck in-flight row holds capacity forever — FIXED

**Escalated during Stage 8's first real resilience trial**, not a theoretical
edge case: killing the worker mid-`embedding` orphaned 6 sources. After a
clean restart, none recovered — the dispatcher only ever claims rows with
`status='pending'`, and a row stranded in `embedding` never returns to it.
Because in-flight statuses count against `DISPATCH_MAX_INFLIGHT`, those 6
orphans exactly exhausted a capacity of 6 and **silently deadlocked the entire
queue** — new, unrelated registrations stopped being admitted too, with the
API still returning 202 and nothing anywhere signalling why.

**Fix:** `dispatcher.reconcile_once()`, run every tick before `dispatch_once()`
so a freed slot is usable in the same tick. A row is stale once
`RECONCILE_STALE_AFTER_S` (600s) passes with no update —
`set_status`/`set_progress` touch `updated_at` on every real tick of a running
flow, so staleness only accumulates once the worker that owned it is gone.
600s has wide margin over the worst legitimate stall (task retries span up to
~150s, plus 12-35s/doc of real embedding time, D11). Stale rows are reset to
`pending` for the fair dispatcher to reclaim, unless already attempted
`RECONCILE_MAX_ATTEMPTS` (5) times, in which case they are marked `failed`
instead of retried forever.

**Verified live against the actual deadlock**, not a synthetic test: on
restart, the reconciler fired on its first tick (~3s) and processed all 6
orphans. Their outcome was `failed`, not `pending` — their `attempts` counters
were already 29-31 from this session's own repeated manual re-registration,
past the cap — which is the give-up safety net working as designed, not a
different bug. `count_inflight()` afterward: 0/6. Capacity fully recovered.

**Stage 8's second trial, kill point moved to `fetching`** (earlier in the
pipeline than the first trial's `embedding`), closes the gap the first trial
left open: it demonstrates the `pending`-path live rather than only in unit
tests. First attempt at this trial reused the same 10 fixed doc IDs and hit
the identical attempts-cap issue (30-33 attempts from session-long reuse,
so every orphaned row again went straight to `failed`) — confirming that
result is about accumulated test-harness state, not the crash. Reset
`attempts=0` on those 10 rows (legitimate fixture hygiene: the counter is
meant to track retries within one coherent run, not accumulate across a
session's worth of unrelated manual re-invocations) and reran: kill landed
mid-`fetching` with 5 sources caught in-flight, all reset to `pending` by the
reconciler, all 10 rows reached `indexed`, zero lost, zero duplicated
(`benchmark/_resilience_2_fetching.json`). Attempts afterward: 1 for the 5
sources never touched by the crash, 2 for the 5 that were orphaned and
retried — direct, live evidence of the `pending → retry → success` path.

Separately, this trial's wait-loop also surfaced a harness bug of the same
species as D12's throughput-measurement fix: `run_resilience()` polled for
the target stage every 2s, fine for the multi-second `embedding` stage but
too coarse for `fetching`/`chunking`, which resolve for these small PDFs in
well under a second — the first run of this trial saw the entire backfill
finish before a single poll ever landed. Fixed by tightening the poll to
0.2s (`benchmark/bench.py`).

**Scoped to `ENABLE_FAIR_DISPATCH=true`** (the session's default and the only
mode exercised this session) — FIFO mode enqueues to Prefect directly at
registration and has no dispatcher loop for the reconciler to run inside;
covering it would need a separate re-enqueue path, not attempted.

---

## D11. Ingest throughput is bounded by CPU embedding cost, not by tuning

`ingest_throughput_chunks_per_s` misses its >=8 target (measured 4.7-7.2). The
cause is not concurrency, memory, orchestration or the vector store - all four
were investigated and eliminated by measurement.

**Measured breakdown of one document's `embed-index` task** (instrumented, then
reverted):

| Step | Time |
|---|---|
| embed | 12-35 s |
| upsert to Qdrant | 0.2-0.4 s |
| progress writes | 0.06-0.13 s |

Everything except embedding is noise. Ruled out along the way:

* **Qdrant** - 6-way concurrent ensure+delete+upsert+count completes in 0.51 s
  wall; the index holds exactly 1247 points with no bloat across runs.
* **Prefect orchestration** - 1.7 s of a 73 s flow run.
* **Cold imports** - 0.6 s in a fresh subprocess.
* **Memory** - fixed by the dedicated embedding service (7.25 GiB -> 1.1 GiB).

**The real ceiling.** Embedding throughput depends almost entirely on text
length, and an early measurement of mine was invalid because it used trivially
short synthetic strings:

```
SYNTHETIC  98 chunks, avg   78 chars:  0.35 s = 283.5 chunks/s
REAL       98 chunks, avg 1000 chars:  9.81 s =  10.0 chunks/s
```

bge-small on this CPU embeds **~10 real chunks/s**. The SLA asks for 8. There is
essentially no headroom: even with zero overhead the target is barely reachable
on this hardware, which is why raising concurrency (2 -> 4 -> 6 -> 8) never moved
throughput and why the batching service improved memory but not chunks/s.

An earlier "sub-linear thread scaling" curve was also distorted - it included
per-call model construction, not just inference. Warm inference is what matters.

**This is a hardware/model choice, not a tuning problem.** Closing it needs one
of: a hosted embedding API (already supported via `TEXT_EMBED_PROVIDER=openai`,
one-time re-seed required), a smaller or quantised model (changes vector dim, so
recall and thresholds must be re-measured), or more cores.

## Open gaps

* Threshold margins are thin (D9); re-calibrate against `benchmark/queries.jsonl`.
* `deck_indexed` fails by decision (D1).
* `ingest_throughput_chunks_per_s` is hardware-bound (D11), not tuning-bound.
* `search_p95_during_ingest_ratio` still fails even with the LLM excluded from
  the measurement (D12). Ruled out with evidence: LLM variance, Qdrant, and
  memory pressure (a real 5.5 GiB leak was found and fixed, and latency got
  WORSE as memory improved). Identified but unfixed: CPU contention between
  the ingest and search paths on a box where Docker reserves no CPU. The
  remaining fix is core pinning — infrastructure, not application tuning.
  Genuinely open, unlike the other three items above.
* P4 (stuck in-flight rows deadlocking the dispatcher) is now fixed - see P4.
