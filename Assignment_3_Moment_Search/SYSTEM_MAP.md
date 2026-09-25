# SYSTEM_MAP — how MomentSearch actually works

Read from the code, not from the assignment README. Where the two disagree the
code wins and the disagreement is recorded in §9.

Base commit `8526743`. Line references point at the **working tree**, not at
upstream: `src/db.py` has already gained the document columns, so its numbers
have shifted. Every reference in this file was checked mechanically against the
tree, not typed from memory.

---

## 1. Status lifecycle

**Video** (`src/db.py:3`, and the transitions themselves):

```
pending -> queued -> fetching -> sampling -> embedding -> indexed
                                     |                      |
                                     +-> skipped (duplicate)
                                     +-> failed
```

| Status | Written at | Meaning |
|---|---|---|
| `pending` | `db.upsert_pending` (`db.py:94`), and on retry `videos.py:184` | in the manifest, waiting for the fair dispatcher |
| `queued` | `db.wfq_claim` (`db.py:281`) - the atomic claim | admitted, Prefect run scheduled |
| `fetching` | `pipeline.t_fetch` (`pipeline.py:45`) | downloading / hashing |
| `sampling` | `pipeline.t_sample` (`pipeline.py:70`) | ffmpeg keyframes, dedup, thumbnail upload |
| `embedding` | `pipeline.t_embed_index` (`pipeline.py:98`) | CLIP batches -> Qdrant |
| `indexed` | `pipeline.t_embed_index` (`pipeline.py:118`) | searchable |
| `skipped` | `pipeline.t_fetch` (`pipeline.py:62`) | duplicate `(user_id, source_hash)` |
| `failed` | `pipeline.ingest_video` except-arm (`pipeline.py:173`) | terminal until retried |

`progress` (surfaced as `pct`) is **0..1 within the current stage, not overall**
- `db.set_progress` (`db.py:181`). `t_sample` updates it every 25 thumbnails
(`pipeline.py:86`); `t_embed_index` after each CLIP batch (`pipeline.py:117`).
So "0.5" means half of *this* stage. Any unified progress bar has to combine it
with `status` or it will appear to go backwards at each stage change.

**`INFLIGHT_STATUSES` (`config.py:113`) is the capacity accounting**, and it is
a hardcoded tuple: `("queued", "fetching", "sampling", "embedding")`. Anything
not in it is invisible to `db.count_inflight` (`db.py:241`) and therefore to the
dispatcher's free-slot calculation. See §9 finding F2.

## 2. Flow and task decomposition

`src/jobs.py` -> Prefect Cloud -> `src/worker.py` -> `src/ingest/pipeline.py`.

* **The API never imports the pipeline.** `jobs.enqueue_video` (`jobs.py:18`)
  calls `run_deployment(..., timeout=0)` - fire and forget. This is why torch
  and ffmpeg are not in the API's import graph, and it is the mechanism behind
  the whole decoupling rubric area.
* **`worker.py:39`** calls `ingest_video.serve(name="ingest", limit=WORKER_CONCURRENCY)`,
  registering the deployment `ms-ingest-video/ingest` and long-polling. Outbound
  HTTPS only, no inbound ports. Scale = more replicas.
* **The serve loop self-heals** (`worker.py:35-45`): a Prefect Cloud blip used
  to kill the worker permanently; it now retries every 15s.

**Task boundaries and retries** (`pipeline.py`):

| Task | Retries | Backoff | Notes |
|---|---|---|---|
| `t_fetch` | 2 | 30s, 120s | network-bound, so a real retry policy |
| `t_sample` | **0** | - | CPU-bound; a failure here is usually a bad file, not a blip |
| `t_embed_index` | 2 | 60s | the Qdrant write |
| `t_transcript` | 1 | 30s | best-effort, never fails the flow (`pipeline.py:152`) |

Task-level retries are what stop a completed stage from re-running when a later
one fails - Prefect re-executes the failed task, not the flow.

## 3. The fair dispatcher (not in the assignment's description)

`src/dispatcher.py` sits **between** the API and Prefect and is the reason
registration returns without enqueuing anything:

```
every DISPATCH_INTERVAL_S (3s):
  slots = DISPATCH_MAX_INFLIGHT - count_inflight()
  wfq_claim(slots)          # round-robin across users, atomic
  jobs.enqueue_video(...)   # only now does Prefect hear about it
```

Fairness comes from `db.wfq_claim` (`db.py:251`): rank each user's pending rows
by age with `row_number() OVER (PARTITION BY user_id ORDER BY created_at)`, then
order by that rank, so everyone's oldest goes before anyone's second. The
`UPDATE ... WHERE status='pending' RETURNING` is the atomic claim, so racing
dispatchers each hand a row out once.

`ENABLE_FAIR_DISPATCH=false` bypasses it entirely and the API enqueues inline
(`videos.py:148`).

**Consequence for documents:** `dispatch_once` (`dispatcher.py:39`) calls
`jobs.enqueue_video` for *every* claimed row. See §9 finding F1.

## 4. Payload schema - the contract documents must join

Two collections, not one, and necessarily so: CLIP and bge produce different
dimensions.

| | Visual | Text |
|---|---|---|
| Collection | `QDRANT_COLLECTION` = `moments` | `TEXT_COLLECTION` = `moments_text` |
| Dim | 512 (`clip-ViT-B-32`) | 384 (bge-small) or 1536 (OpenAI) |
| Created by | `ensure_collection` (`vector_store.py:129`) | `ensure_text_collection` (`vector_store.py:134`) |
| Point id | `uuid5("{video_id}:{frame_idx}")` (`vector_store.py:76`) | `uuid5("{video_id}:text:{i}")` (`vector_store.py:182`) |

**Frame payload** (`pipeline.py:110`):

| Field | Type | Notes |
|---|---|---|
| `user_id` | str | tenant index, `is_tenant=True` (`vector_store.py:115`) |
| `video_id` | str | keyword index; the filter/delete/join key everywhere |
| `ms` | int | frame timestamp - **the locator** |
| `idx` | int | frame ordinal; also the thumbnail storage key |
| `modality` | `"frame"` | |
| `t_start`, `t_end` | float | both = `ms/1000` for a frame |
| `embed_version` | str | stamped for re-index without breaking the live index |

**Transcript payload** (`pipeline.py:146`): same minus `idx`, with
`modality:"text"`, real `t_start`/`t_end` spans, and `text`.

**There is no `kind` field.** Video points predate the concept, so downstream
code must read absent `kind` as `"video"`.

Payloads are deliberately thin - titles and URLs live in Postgres and are joined
at answer time (`vector_store.py:15`, `search.py:129`).

## 5. The crash-safety seam - worth 15 points

`t_embed_index` (`pipeline.py:95-120`), in order:

1. `db.set_status(..., "embedding")`
2. `ensure_collection()`
3. `delete_video(user_id, video_id)` - clears **both** collections
   (`vector_store.py:263`), so a re-run cannot leave stale points
4. per batch: `embed_jpegs` -> `upsert_frames(..., wait=True)` -> `set_progress`
5. **after the loop** `db.set_status(..., "indexed", frame_count=total)`

**The ordering is correct as provided**: the status is committed only after all
upserts return. Combined with deterministic point ids, a redelivered run
overwrites rather than duplicates.

Two things it does *not* do, both of which matter for Stage 8:

* **No read-back verification.** `wait=True` means acknowledged, not visible.
* **`t_transcript` runs AFTER the row already says `indexed`**
  (`pipeline.py:169`). A crash between the two leaves a video marked indexed
  with no transcript chunks, and nothing retries it. Tolerable for a
  best-effort branch on the video path; **not** a pattern to copy for documents,
  where the text branch is the only branch.

## 6. Chunking

There is **no semantic chunker** and no `src/rag/chunk.py`. The only chunker is
`transcript.chunk_cues` (`transcript.py:82`), which groups caption cues into
~`TRANSCRIPT_CHUNK_SECONDS` (20s) windows carrying `t_start`/`t_end`.

It is time-based, so nothing about it transfers to a page. Document chunking is
new code (`src/ingest/docparse.py`), which is real Stage 3 scope the plan did
not budget for.

## 7. Retrieval and citation assembly

`rag_search.retrieve` (`search.py:103`):

1. Visual branch: `embed_text(q)` -> `vector_store.search`, `BRANCH_TOP_K=20`
2. Text branch: `embed_query(q)` -> `search_text`, same k, only if
   `ENABLE_TRANSCRIPT`
3. `_fuse` (`search.py:31`) - RRF, `rrf = 1/(RRF_K + rank)`, `RRF_K=60`
4. Windows: hits within `FUSION_WINDOW_S=15` seconds **of the same
   `video_id`** merge into one "moment" (`search.py:52-53`)
5. Best hit per modality only, then `CROSS_MODAL_BOOST=1.5` if both agree
6. Metadata join `db.videos_by_ids`, then citation dicts (`search.py:139`)

**Abstention already exists** (`search.py:225`): gate on the *raw* per-branch
bests, not the RRF score - `CONFIDENCE_THRESHOLD=0.2` visual,
`TEXT_CONFIDENCE_THRESHOLD=0.35` text. If neither clears, abstain **without**
calling the LLM. `_validate_citations` (`search.py:173`) additionally strips
`[n]` references the model invented.

Emitted citation shape:

```
{n, video_id, title, url, source, ms, timestamp, idx, thumbnail,
 media_url, deeplink, score, transcript, modalities}
```

## 8. Extension points for papers and decks

| Need | Hook |
|---|---|
| Manifest row | `db.upsert_pending_document` - same table (see DECISIONS) |
| Schedule | `jobs.py` needs per-kind deployment names |
| Execute | new flows, served alongside `ingest_video` in `worker.py:39` |
| Admit fairly | `dispatcher.dispatch_once` must route by `kind` |
| Index | `TEXT_COLLECTION` via `upsert_document_chunks` |
| Retrieve | free - `search_text` filters only on `user_id` |
| Group | `_fuse` window key (`search.py:52-53`) |
| Cite | `search.retrieve` citation dict (`search.py:139`) |

---

## 9. Findings - where the code differs from the spec, and what breaks

### F1. The dispatcher will hand documents to the video flow *(blocker)*

`dispatcher.dispatch_once` (`dispatcher.py:39`) calls `jobs.enqueue_video` for
every row `wfq_claim` returns. Since documents now share `ms_videos`, a pending
paper gets scheduled as `ms-ingest-video/ingest`, whose `t_fetch` will treat it
as an upload or a YouTube URL and fail. Must route on `kind`. Stage 5.

### F2. Document stages are invisible to capacity accounting *(blocker)*

`INFLIGHT_STATUSES` (`config.py:113`) lists only the video stages. The new
`chunking` and `captioning` statuses are not in it, so `count_inflight`
undercounts and the dispatcher over-admits - it sees free slots while documents
are actively working. This directly threatens the <=1.3x search-latency SLA,
which is the one number the whole decoupling area is judged on. Stage 5.

### F3. `/ask_stream` does not exist *(largest gap)*

The assignment README lists `GET /ask_stream?q=...` as **provided**. It is not in
this repo. The only query endpoint is `POST /api/ask` (`api/search.py:139`),
returning JSON.

`eval/eval.py:45` reads `GET /ask_stream?q=...` as Server-Sent Events and takes
the `citations` array from the first event containing one. Four automated checks
depend on it - `paper_indexed`, `deck_indexed`, `cross_source`, `grounded` -
worth **50 of 100 points**. Without the endpoint they all fail regardless of how
good ingestion is.

It also expects a different citation shape than `retrieve` emits:

```python
c["kind"]                      # absent today
c["locator"]["page"]           # absent today - locator is flat `ms`
c["locator"]["slide"]
c["text"]                      # today the field is `transcript`
```

So Stage 6 is not "add `kind` to the existing citations" - it is: build an SSE
endpoint, and restructure the citation into `{kind, text, locator:{...}}` while
keeping the existing UI (which reads `c.timestamp`, `c.deeplink`,
`ui/index.html:388,418`) working.

### F4. `_fuse` collapses every document chunk into one window *(blocker)*

`search.py:52-53` groups by `(video_id, |dt| <= 15s)` and `ranked()` (`search.py:43`)
falls back to `t = ms/1000 = 0` when a payload has no `t_start`. Document chunks
have neither, so **all** chunks of a paper land in one window at t=0: a 40-page
paper returns as a single citation. The window key must become
`(source_id, kind, locator)` for documents. Stage 6.

### F5. Port mismatch

`eval.py:64` defaults `--base-url` to `http://localhost:8100`;
`docker-compose.yml:43` publishes `8000:8000`. Pass `--base-url` explicitly or
change the published port. Cheap, but it silently fails every check if missed.

### F6. Paths in the spec are stale

No `src/api/admin.py` (admin routes are in `api/videos.py` under `/api/videos`),
no `src/rag/chunk.py`, no root `app.py`/`worker.py`. Per plan R12, trust the code.

### F7. The sample-seed gate shapes the Stage 2 baseline

`docker compose up` runs `src/seed.py` to completion before api/worker start
(`docker-compose.yml:52,71`). Idle p95 must be measured only after it finishes,
and the same protocol reused verbatim in `bench.py`, or the 1.3x ratio is
meaningless (plan R10).

### F8. Samples are delete-protected

`is_sample` (`samples.py`) makes the four seeded talks undeletable
(`videos.py:197`). Any benchmark that assumes a clean index must account for
them.
