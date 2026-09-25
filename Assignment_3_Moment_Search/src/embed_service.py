"""Bulk document-chunk embedding service — one warm bge model, micro-batched.

    uvicorn src.embed_service:app --host 0.0.0.0 --port 8002

**Why this is a SEPARATE container from clip_service.** clip_service sits on the
SEARCH path: it answers `/embed/text` and `/embed/query`, one short string, while
a user waits. Bulk ingest embedding used to share that process, and a 64-chunk
batch monopolised it — query encodes queued behind the batch and search latency
spiked to 8.5s against a 29ms median. Splitting the two workloads into separate
processes is the only way the bulk path cannot block the interactive one, no
matter how heavily it is loaded.

**Why a service rather than embedding in the worker.** Prefect runs every flow
as its own OS subprocess, so with WORKER_CONCURRENCY=6 the worker held SIX
independent copies of the bge model — memory peaked at 7.25 GiB of a 7.75 GiB
box, and concurrency 8 was OOM-killed outright. One warm model here replaces N
copies there, which is what buys back the headroom.

**Why micro-batching.** bge scales sub-linearly with threads (measured: 3.3
chunks/s at 1 thread, 9.0 at 4 — not 4x), so many small single-threaded
inferences waste the hardware. Collecting requests that arrive within a few
milliseconds into ONE larger inference lets a single well-threaded session do
the work at its efficient point instead.

Endpoints:
  POST /embed/docs  {"texts": [...]}  -> {"vectors": [[...], ...]}
  GET  /healthz                       -> {"ok", "model", "dim", "stats"}
"""
from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI
from pydantic import BaseModel

from . import config
from .rag import embeddings


@dataclass
class _Job:
    """One caller's texts, plus the future its result is delivered on."""

    texts: list[str]
    future: asyncio.Future = field(repr=False)


_queue: asyncio.Queue[_Job] | None = None
# Exactly one worker thread: inference is CPU-bound and the model is given the
# whole thread budget for a single batch. A second concurrent batch would just
# split the same cores and lose to the sub-linear scaling curve above.
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embed")
_stats = {"requests": 0, "batches": 0, "texts": 0, "max_batch": 0,
          "subbatches": 0}


def _cap_threads() -> None:
    """Bound ONNXRuntime so this service cannot starve api/clip of CPU.

    Docker reserves no CPU per container by default, so a container that
    saturates every core stalls the others regardless of how little work they
    need — that is what produced multi-second search stalls earlier.
    """
    n = config.DOC_EMBED_SERVICE_THREADS
    if n > 0:
        os.environ.setdefault("OMP_NUM_THREADS", str(n))
        print(f"[embed] ONNX threads capped at {n} (host has {os.cpu_count()})")


def _est_tokens(text: str) -> int:
    """Cheap token estimate for batch sizing only - never changes what is
    embedded. ~4 chars/token for English prose, clamped to the model's limit
    because bge truncates there anyway."""
    return max(1, min(config.DOC_EMBED_MODEL_MAX_TOKENS, len(text) // 4))


def _split_by_token_budget(texts: list[str], budget: int) -> list[list[str]]:
    """Split into sub-batches whose PADDED cost (count x longest sequence)
    stays under `budget`.

    Why padded cost and not a plain count: ONNXRuntime pads every sequence in a
    batch to the longest one, and activation memory is O(batch x seq^2). A cap
    expressed in chunks therefore bounds nothing - 256 short chunks and 256
    long ones differ by more than an order of magnitude in peak memory. Since
    the arena never gives that peak back (see config.DOC_EMBED_MAX_BATCH_TOKENS),
    one unlucky batch permanently inflates the container.

    A single over-budget text is never dropped or truncated - it becomes its
    own sub-batch, which is the smallest work unit that still embeds it.
    """
    out: list[list[str]] = []
    cur: list[str] = []
    cur_max = 0
    for t in texts:
        n_tok = _est_tokens(t)
        new_max = max(cur_max, n_tok)
        if cur and (len(cur) + 1) * new_max > budget:
            out.append(cur)
            cur, cur_max = [t], n_tok
        else:
            cur.append(t)
            cur_max = new_max
    if cur:
        out.append(cur)
    return out


def _embed_sync(texts: list[str]):
    """Run the model. Called only on the single pool thread.

    Runs token-budgeted sub-batches sequentially so peak activation memory is
    bounded by config, not by how much work happened to arrive at once. Order
    is preserved, so the caller fan-out below stays a simple offset walk.
    """
    budget = config.DOC_EMBED_MAX_BATCH_TOKENS
    groups = _split_by_token_budget(texts, budget) if budget > 0 else [texts]
    if len(groups) > 1:
        _stats["subbatches"] += len(groups)
    out: list = []
    for g in groups:
        out.extend(embeddings.embed_docs_local(g).tolist())
    return out


async def _batch_loop() -> None:
    """Coalesce queued jobs into one inference, then fan results back out.

    Waits BATCH_WINDOW_MS for more work after the first job arrives, capped at
    MAX_BATCH texts. The window is short enough to be invisible next to a
    multi-second document ingest, and long enough for concurrent flow runs to
    land in the same batch.
    """
    assert _queue is not None
    loop = asyncio.get_running_loop()
    window = config.DOC_EMBED_BATCH_WINDOW_MS / 1000.0

    while True:
        job = await _queue.get()
        jobs = [job]
        total = len(job.texts)
        deadline = time.monotonic() + window

        while total < config.DOC_EMBED_MAX_BATCH:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                nxt = await asyncio.wait_for(_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            jobs.append(nxt)
            total += len(nxt.texts)

        flat = [t for j in jobs for t in j.texts]
        _stats["batches"] += 1
        _stats["texts"] += len(flat)
        _stats["max_batch"] = max(_stats["max_batch"], len(flat))

        try:
            vectors = await loop.run_in_executor(_pool, _embed_sync, flat)
        except Exception as exc:  # noqa: BLE001
            # One poisoned batch must not take down the loop or wedge callers
            # waiting on their futures.
            for j in jobs:
                if not j.future.done():
                    j.future.set_exception(exc)
            continue

        i = 0
        for j in jobs:
            n = len(j.texts)
            if not j.future.done():
                j.future.set_result(vectors[i:i + n])
            i += n


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _queue
    _cap_threads()
    _queue = asyncio.Queue()
    embeddings.embed_docs_local(["warmup"])  # load the model at boot, not on
    print(f"[embed] {config.TEXT_EMBED_MODEL} warm (dim {config.TEXT_EMBED_DIM})")
    task = asyncio.create_task(_batch_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="MomentSearch document embedding service", lifespan=lifespan)


class DocsRequest(BaseModel):
    texts: list[str]


@app.get("/healthz")
def healthz():
    return {"ok": True, "model": config.TEXT_EMBED_MODEL,
            "dim": config.TEXT_EMBED_DIM, "stats": dict(_stats)}


@app.post("/embed/docs")
async def embed_docs(req: DocsRequest):
    if not req.texts:
        return {"vectors": []}
    assert _queue is not None
    _stats["requests"] += 1
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    await _queue.put(_Job(texts=list(req.texts), future=future))
    return {"vectors": await future}
