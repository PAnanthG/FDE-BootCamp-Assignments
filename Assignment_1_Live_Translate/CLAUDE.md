# CLAUDE.md — working context for Claude Code

Read this first, then **`DECISIONS.md`** for *why* the code looks the way it does.
`AGENTS.md` (provided by the assignment) is the source of truth for requirements.

---

## What this is

FDE Assignment 1 — "Live Translate". A provided browser widget calls a **Node
gateway** (`:8787`), which proxies to a **Python FastAPI AI service** (`:8000`),
which calls **Anthropic Claude** and caches results in a **two-tier cache**
(in-memory LRU + SQLite).

**This is a personal/learning build.** It translates to **Dutch**, not the
assignment's Mexican Spanish. See `DECISIONS.md` §1 (V1) — this is deliberate and
reversible via one env var.

**How the human wants to work:** TODO-by-TODO, **explain before writing code**.
Surface real decisions and let them choose rather than picking silently. Flag
non-obvious requirements proactively — the untagged `X-Request-Id` requirement was
caught that way and would otherwise have cost points.

---

## Current state

**All four marked TODOs + the unmarked tracing requirement are implemented and tested.**

| File | Status |
|---|---|
| `backend/ai-service-python/lib/llm.py` | ✅ Done — Anthropic call, Dutch prompt, `resolve_target()` |
| `backend/ai-service-python/lib/cache.py` | ✅ Done — WAL, LRU, upsert |
| `backend/ai-service-python/app.py` | ✅ Done — `translate_one()`, `_normalize()`, 502 handler, request-ID logging |
| `backend/gateway-node/server.js` | ✅ Done — logging middleware, `callAiService()`, tracing, file logging |
| Hygiene | ✅ `.gitignore` + both `.env.example` updated |

**Outstanding, in order:**
1. ~~Run `smoke_llm.py` with a real key~~ **Done 2026-07-16.** Dutch quality validated (see `DECISIONS.md` §5.1). Note: `requirements.txt` needed an `httpx<0.28` pin to work with `anthropic==0.39.0` — see `DECISIONS.md` §5.7 before assuming a fresh install "just works".
2. ~~`python benchmark/bench.py`~~ **Done 2026-07-16.** Exit 0, all 5 SLAs passed (hit p95 11.8ms, miss p95 1923ms, hit rate 75.0%, error rate 0.0%, throughput 1471.6 req/s). Single-flight (D15) confirmed not needed — see `DECISIONS.md`. Node.js wasn't installed on this machine; installed via `nvm` (LTS, v24.18.0) to run the gateway for the end-to-end run.
3. **Fly.io deploy** — both services; point the extension popup at the public gateway.
4. **`PRODUCT_EVAL.md`** + 60–90s video demo.

---

## Golden rules

- **Never edit `widget/`, `extension/`, `benchmark/`, or `eval/`.** Red-line in the rubric. The widget hardcodes `target:"es-MX"`; that's why the Dutch override lives in the backend.
- **Never return untranslated English as if it succeeded.** Automatic fail, and a real production bug. No `try/except` that falls back to `text`. Errors propagate → 502.
- **Never commit `.env`, keys, `*.log`, or `translations.db`.** Committing a secret is an automatic fail.
- **The human sets their own API key.** Don't ask for it, don't write it to a file.
- **Cache is not invalidated by prompt edits.** Tuning the prompt? `rm backend/ai-service-python/translations.db` **and restart** (the memory tier is in-process). Otherwise you'll change the prompt, see no difference, and chase a ghost.

---

## Commands

```bash
# --- AI service (:8000) ---
cd backend/ai-service-python
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add your real ANTHROPIC_API_KEY
uvicorn app:app --reload --port 8000

# --- gateway (:8787) --- separate terminal
cd backend/gateway-node
npm install
cp .env.example .env
npm start

# --- validate ---
python smoke_llm.py                       # live EN->NL quality check (needs key)
python benchmark/bench.py                 # SLA gate, must exit 0
curl -s localhost:8787/health | jq
curl -s localhost:8787/stats | jq

# --- trace correlation (the graded one) ---
curl -s -H 'X-Request-Id: probe-123' -H 'Content-Type: application/json' \
  -d '{"text":"Add to cart","target":"es-MX"}' localhost:8787/translate
grep probe-123 backend/gateway-node/gateway.log backend/ai-service-python/ai-service.log
# MUST appear in both files
```

---

## Architecture notes that will bite you

- **`app.py` imports `lib.llm` before `load_dotenv()` runs.** Any module-level client construction reads the key too early and crashes on startup. The Anthropic client is built lazily on first call. Don't "clean this up".
- **`llm.resolve_target()` is the single source of truth** for the effective output language. Both the prompt and the **cache key** use it. If they ever diverge, the cache can serve Dutch for a Spanish request and report it as a healthy hit. Don't key the cache on the raw `target`.
- **The gateway must write `gateway.log` as a file.** `eval.py` greps it on disk. `console.log` alone creates no file.
- **`/translate/batch` is sequential upstream** — a large uncached batch legitimately takes tens of seconds. That's why `AI_TIMEOUT_MS` is 40s and not 10s.
- **WAL is on** (`PRAGMA journal_mode=WAL`) because the read path writes (`access_count` bump). Expect `translations.db-wal` / `-shm` side-files; both are gitignored.

---

## Testing without an API key

**A test suite already exists — run it before and after any change.** It fakes the
provider, so it needs no key and costs nothing. It covers everything except
translation quality (for that, `smoke_llm.py` + a real key).

```bash
cd backend/ai-service-python
python3 tests/test_llm_mock.py        # 18 checks — call params, Dutch override,
                                      #   FORCE_TARGET hatch, cleaning, fail-loud
python3 tests/test_cache.py           # 24 checks — REAL SQLite: schema, WAL,
                                      #   LRU eviction, upsert, restart survival
python3 tests/test_app.py             # 30 checks — real FastAPI routes + SQLite
bash tests/integration_test.sh        # both services over real HTTP:
                                      #   trace correlation, 400/502, AI-down
```

`tests/run_ai_fake.py` runs the real `app.py` under uvicorn with only the provider
call faked — useful for poking at a live service by hand without a key.

If you need a fake in your own test, patch the module-level name
`app.translate_text` — `translate_one` resolves it at call time:

```python
import app as A
async def fake(text, target="es-MX", model=None):
    return "[nl] " + text
A.translate_text = fake
```

Cache tests need no faking at all — they use real SQLite.

**Shell gotchas found the hard way:**
- Background services **don't survive between separate tool calls**. Start them, test them, and tear them down inside a single shell session.
- **Never `pkill -f run_ai_fake`** from a script whose own command line contains that string — `pkill -f` matches the script itself and kills your own shell. Use PID files.

---

## Env vars added by this build

| Var | Service | Default | Purpose |
|---|---|---|---|
| `FORCE_TARGET` | ai-service | `nl` | Output language override. `""` = honour caller's `target` (restores es-MX). Safe to flip — the cache keys on the effective language. |
| `CACHE_MEM_MAX` | ai-service | `5000` | Memory-tier LRU cap. |
| `AI_TIMEOUT_MS` | gateway | `40000` | Gateway → AI fetch timeout. |

---

## The SLA you're building against

`benchmark/sla.json`: hit p95 ≤ **60ms** · miss p95 ≤ **3500ms** · hit rate ≥ **60%** · error rate ≤ **1%** · throughput ≥ **20 req/s**.

Measured locally with a faked 50ms provider: **miss 58ms, hit 0ms**. The real
miss path will be seconds, not milliseconds — the hit path is what the 60ms
target is about.

⚠️ `sla.json`'s cost prices are placeholders by its own admission. Verify against
Anthropic's current published rates before quoting cost figures.
