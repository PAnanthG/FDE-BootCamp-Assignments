# Stage 0 action 7 — service provisioning checklist

Six values in `.env` need accounts I cannot create. Everything else is done:
`.env` exists (gitignored, untracked, verified), `ADMIN_TOKEN` is generated, and
`scripts/check-env.py` will tell you when the file is complete and the services actually
answer.

```bash
python3 scripts/check-env.py
```

It prints set/unset and reachability only — **never a secret value** — so its output is
safe to paste into a report or screenshot. Right now it says `NOT READY: 6 failures`.

**D6 applies to every value below: it goes into `.env` and nowhere else.** Not into chat,
not into a commit, not into a screenshot. `.env` is ignored by git and guard check 3 now
recognises the shape of every credential in this list, so an accidental paste into a
tracked file gets refused at commit time rather than discovered later.

---

## 1. Neon Postgres — the source manifest

Free tier, no card.

1. <https://neon.tech> → new project.
2. Project → **Connection Details** → select **Pooled connection**.
3. Copy the whole `postgresql://...` string into `DATABASE_URL`.

**Take the pooled endpoint, not the direct one.** The host contains `-pooler.`. Workers
open a connection per flow run and the direct endpoint runs out under concurrency.
`check-env.py` warns if `-pooler.` is missing. Keep the `?sslmode=require` suffix.

## 2. Prefect Cloud — the work queue

Free tier, no card. This is the queue the whole "decoupling" rubric area (20 pts) is about.

1. <https://app.prefect.cloud> → sign up → create a workspace.
2. Avatar menu → **API Keys** → **Create API Key** → copy into `PREFECT_API_KEY`.
3. `PREFECT_API_URL` is
   `https://api.prefect.cloud/api/accounts/<account-id>/workspaces/<workspace-id>` —
   both ids are in the browser URL when the workspace is open.

`check-env.py` warns if the URL is missing `/accounts/`, which is the usual mistake
(pasting the dashboard URL instead of the API URL).

## 3. Qdrant Cloud — the vector index

Free tier: 1 GB cluster, no card.

1. <https://cloud.qdrant.io> → create a free cluster (pick the region nearest you).
2. Cluster → **API Keys** → create → `QDRANT_API_KEY`.
3. Cluster URL (`https://xxxx.<region>.aws.cloud.qdrant.io:6333`) → `QDRANT_URL`.

Include the scheme. `check-env.py` calls `GET /collections` with the key, so it
distinguishes "unreachable" from "reachable but key rejected".

Leave `QDRANT_COLLECTION=moments` and the three low-RAM flags at their defaults for now —
`QDRANT_QUANTIZATION`, `QDRANT_ON_DISK`, `QDRANT_HNSW_ON_DISK` are on by default because
frame vectors grow fast, and the free tier is 1 GB.

## 4. Vision-capable LLM — the answer synthesizer

Retrieval is local CLIP and needs no key; this is only for synthesizing the cited answer.
**It must be vision-capable — it is shown the actual video frames.**

`LLM_PROVIDER=openai` and `LLM_MODEL=gpt-4o-mini` are already set as sensible defaults;
put your key in `LLM_API_KEY`. Alternatives, all env-switched:

| Provider | `LLM_PROVIDER` | Model to set |
|---|---|---|
| OpenAI | `openai` | `gpt-4o-mini` (cheap) or `gpt-4o` |
| Anthropic | `anthropic` | `claude-sonnet-5` — also uncomment `anthropic` in `requirements.txt` |
| NVIDIA | `nvidia` | `meta/llama-3.2-90b-vision-instruct` |
| Local (Ollama/vLLM) | `openai` + `LLM_BASE_URL` | `llava`, `qwen2.5-vl`, … |

Stage 4 needs this same model to caption image-only slides, so a weak vision model shows
up twice — the sample scorecard docks "image-only slides captioned thinly" specifically.
Worth spending on `gpt-4o` here rather than the mini if cost allows.

## 5. Object storage — not needed yet

`STORAGE_PROVIDER=local` is set. The API serves bytes itself and files land in `./data/`,
which is gitignored. That is enough through Stage 8.

Before Stage 9 (Fly.io) this has to become real storage — `fly storage create` provisions
Tigris and injects the credentials into the deployed app automatically, so the switch is
`STORAGE_PROVIDER=flyio` plus `fly secrets set` for everything else. Nothing to do now.

---

## After filling `.env`

```bash
python3 scripts/check-env.py
```

Expect `READY`. Then Stage 2 can start. Two things to expect on the first
`docker compose up --build`:

- **It builds a CLIP image and downloads model weights.** First run is slow — minutes,
  not seconds. Weights are cached in the `hf_cache` volume afterwards.
- **A blocking seed gate runs first.** `docker compose up` runs `src/seed.py`, which
  indexes four sample talks, and the API does not start until it exits successfully
  (`service_completed_successfully`). This is deliberate — the UI is never reachable with
  a half-indexed corpus.

That second point matters for the benchmark, not just for patience. **Idle search p95 is
the denominator of the 1.3× SLA.** Measuring it while seeding is still running, or on a
cold cache, makes the ratio meaningless (plan R10). Measure only after seeding has
finished, on a quiet system, and record the exact protocol — Stage 7 has to reuse it
verbatim for the during-ingest measurement.

Docker itself is confirmed working on this machine: server 29.6.2, 10 CPUs, 8.3 GB
allocated to the VM, `hello-world` runs. `WORKER_CONCURRENCY=2` with ffmpeg + CLIP at
roughly 1–1.5 GB peak per run fits that comfortably.
