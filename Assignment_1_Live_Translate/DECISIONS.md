# Decision log — Assignment 1 · Live Translate

**Build type:** personal / learning. Built TODO-by-TODO, explain-then-code.
**Stack:** Node gateway (`:8787`) → Python FastAPI AI service (`:8000`) → Anthropic Claude → SQLite cache.

This file records *why* the code looks the way it does — the decisions, the
deliberate variances from the assignment as shipped, and what they cost.

---

## 1. Variances from the assignment as written

These are intentional. Each one is a deviation from the scaffold or the brief,
with the reason and the price paid.

| # | The assignment asks | This build does | Why | What it costs |
|---|---|---|---|---|
| **V1** | English → **Mexican Spanish (es-MX)** | English → **Dutch (nl-NL)**, friendly `je` register | Personal learning build; Dutch was chosen deliberately | **Forfeits the 20-pt `llm_prompt_quality` criterion**, which explicitly grades "natural Mexican Spanish". `eval.py`'s live sample check also expects Spanish. **Reversible:** set `FORCE_TARGET=""` in `.env` and the service becomes target-driven, honouring the widget's `es-MX` with no code change. |
| **V2** | Cache keyed on `(text, target)` | Keyed on `(text, **effective language**)` via `llm.resolve_target()` | The widget hardcodes `target:"es-MX"` but `FORCE_TARGET` decides the real output language. Keying on the raw code means flipping `FORCE_TARGET` would serve **Dutch from cache for a Spanish request** — wrong output, reported as a healthy `cached:true` hit. Silent and nasty. | None. `_key()` itself is untouched; we just pass the resolved code. Old rows linger as harmless dead weight after a flip. |
| **V3** | Gateway logging stub hints `console.log(...)` | Logs to stdout **and** `gateway-node/gateway.log` | `eval.py` greps for the trace ID in `gateway.log` **as a file on disk** (`ROOT/gateway.log` or `backend/gateway-node/gateway.log`). `console.log` alone creates no file, so trace correlation would fail unless you remembered `npm start > gateway.log`. Mirrors `lib/logger.py`, which already writes `ai-service.log` via a FileHandler. | None. |
| **V4** | Stub hints `throw new Error("AI service " + res.status)` | AI service returns a **502 with the real error**; gateway unwraps and relays it | FastAPI's default 500 body is the literal string "Internal Server Error" — the cause never leaves the Python process, so the gateway could only report a shrug. | **Exposes upstream provider error text to the browser.** Acceptable for a personal build; on a public deploy you'd normally return a generic message + `requestId` and keep detail in logs. One-line change to gate. |
| **V5** | `text.strip()` | `strip()` **+ collapse internal whitespace runs** | HTML renders any run of whitespace (spaces, newlines, tabs) as a single space, so `"Add to\n   cart"` and `"Add to cart"` are the same string to a reader. Normalizing collapses page chunks onto one cache entry instead of several identical-looking ones — directly feeds the ≥60% hit-rate SLA. | Cache key is no longer byte-exact to the source. Text inside `<pre>`/`<code>` *does* preserve whitespace in HTML; not special-cased, since the widget sends rendered text chunks. |
| **V6** | `cache.py` TODO asks for "an index on `key`" | **Skipped**, with a comment explaining why | `key` is the PRIMARY KEY, so SQLite already maintains a unique index for it (`sqlite_autoindex_translations_1`). A second index would duplicate the same B-tree and add write cost on every insert. Verified by test: only the autoindex exists. | None — it's strictly redundant. |

---

## 2. Decisions

### LLM (`lib/llm.py`)
- **D1 — Provider/model:** Anthropic, `claude-sonnet-4-6`. Confirmed as a live, currently-served model rather than trusting the scaffold's placeholder. Alias, not a dated snapshot (`claude-sonnet-4-6-20260218` would pin it).
- **D2 — `temperature=0`:** deterministic translation. Considered `0.02` as anti-repetition insurance; rejected as unnecessary. Largely moot anyway — temperature only affects the *first* time a string is seen; every repeat is served from cache.
- **D3 — `max_tokens=1024`:** matches the scaffold; handles a long paragraph with headroom.
- **D4 — Lazy client init:** `app.py` imports `lib.llm` **before** `load_dotenv()` runs. Constructing `AsyncAnthropic()` at import time would read the key before `.env` loads and crash on startup. Built on first call instead, so a missing key surfaces as a 502, not an import crash.
- **D5 — Fail loud, no fallback:** deliberately **no** `try/except` returning the original English. Returning untranslated text as if it succeeded is the assignment's automatic-fail rule, and a genuine production bug (ships English while looking healthy). Errors propagate.
- **D6 — Prompt:** natural Netherlands Dutch, friendly `je` (not formal `u`); translation only, no preamble/notes/wrapping quotes; preserves numbers, prices, product/model codes, URLs, emails, HTML markup; idiomatic UI phrasing over word-for-word.
- **D7 — `resolve_target()` is the single source of truth** for "what language is this?", used by both the prompt and the cache key. If they disagreed, V2's bug returns. Aliases canonicalized (`nl-NL`/`nl_NL` → `nl`) so spellings don't fork the keyspace.

### Cache (`lib/cache.py`)
- **D8 — `PRAGMA journal_mode=WAL`:** the read path *writes* (bumping `access_count`). SQLite's default mode locks the whole DB on write, so at the benchmark's ≥20 req/s readers would queue behind it and blow the 60ms hit SLA. WAL lets readers and one writer coexist. Persisted in the DB file, so set once.
- **D9 — Upsert, not `INSERT OR REPLACE`:** two concurrent requests for the same text can both miss and both call the LLM, so conflicts are real. `REPLACE` deletes the row and resets `access_count` to 1; `ON CONFLICT DO UPDATE` refreshes the value while preserving hit history.
- **D10 — LRU cap, default 5000 (`CACHE_MEM_MAX`):** bounds a long-lived process. Eviction is safe: an evicted entry still lives in SQLite, so the next lookup is a `db_hit` (milliseconds), never a miss (seconds + an LLM call).

### Gateway (`server.js`)
- **D11 — `AI_TIMEOUT_MS=40000`:** catches a genuinely hung AI service without failing slow-but-working requests. Generous because `/translate/batch` translates **sequentially** upstream — 30 uncached strings legitimately takes 30s+. Env-tunable so Fly.io can differ from local.
- **D12 — Tracing:** request ID = inbound `X-Request-Id` if present (so an upstream trace continues unbroken), else `randomUUID()`. Stashed on `req`, echoed back on the response header, forwarded to the AI service, logged by both. Also forwarded on the `/health` and `/stats` passthroughs for consistency.
- **D13 — Structured JSON logs** in the gateway, matching `logger.py`'s shape, so both services grep identically.

### Service (`app.py`)
- **D14 — `model` on a cache hit** is reported as the current `MODEL`, not the model that actually produced the row. `cache.get()` returns only the string; changing its return type for a cosmetic field wasn't worth it. A conscious choice, not an accident.
- **D15 — Single-flight deferred.** Concurrent identical misses each call the LLM (N× tokens for one translation). The fix is an in-flight map so duplicates await the first call. Deliberately **not built** — measure at benchmark time, add only if the numbers demand it. **Verdict (2026-07-16): not needed.** All 5 SLAs passed cleanly with margin (throughput 1471.6 req/s vs. a 20 req/s floor, miss p95 1923ms vs. a 3500ms ceiling, 0% errors). The benchmark's cold phase has no duplicate phrases, so it doesn't directly exercise concurrent-identical-miss dedup — but nothing in the numbers points to a bottleneck there either. Leaving it deferred; revisit only if real traffic shows repeated cold misses on the same string arriving concurrently.

### Deploy (Fly.io, 2026-07-18)
- **D16 — Two apps, not one.** `live-translate-ai-pananthg` (FastAPI) and `live-translate-gateway-pananthg` (Express), matching the local two-service split (D-none, but see the README's "why split gateway from AI service"). Each gets its own Dockerfile and `fly.toml` — neither existed in the scaffold; `AGENTS.md` only mandates `fly launch` → `fly deploy`, not a specific config, so these were written from scratch.
- **D17 — A 1GB volume mounted at `/data`, not the container's ephemeral disk.** `TRANSLATION_DB_PATH=/data/translations.db` on the AI service. Without a volume, `translations.db` lives on the container's writable layer and is discarded on every redeploy or machine replacement — the "SQLite tier MUST survive a process restart" requirement (`AGENTS.md`) would hold locally and silently fail in production. Verified: `flyctl machines restart` on the AI service, then the same `/translate` call via the gateway still came back `cached:true` at 2ms.
- **D18 — Gateway's `AI_SERVICE_URL` is a plain `fly.toml` env var, not a secret.** It's a public `*.fly.dev` hostname, not a credential — same treatment as `PORT`/`AI_TIMEOUT_MS`. Only `ANTHROPIC_API_KEY` goes through `flyctl secrets set`.
- **D19 — `min_machines_running = 0` + `auto_stop_machines = "stop"` on both apps.** Machines idle down to zero and cold-start on the next request. Right call for a graded personal project (near-zero cost at rest); the cost is a cold-start on the first hit after idle — acceptable since the SLA gate is about steady-state latency, not first-request-ever latency. Fly still spun up 2 machines for the gateway by default (HA), even with `min_machines_running=0` — that's a "new app" default, not something `min_machines_running` overrides retroactively; both still auto-stop when idle.
- **D20 — Extension popup URL is a runtime setting, not code.** `popup.js` reads/writes `chrome.storage.sync` with a `DEFAULT_URL` fallback — pointing it at the public gateway is done by typing the URL into the popup's own field and clicking Save, **not** by editing `extension/`. Keeps the "never edit widget/extension" rule intact while still satisfying "point the extension popup's backend URL at the public gateway."

---

## 3. New environment variables

None of these existed in the scaffold. All are documented in the `.env.example` files.

| Var | Where | Default | Purpose |
|---|---|---|---|
| `FORCE_TARGET` | ai-service | `nl` | Output language override. `""` = honour the caller's `target` (restores es-MX / enables the language-picker stretch goal). |
| `CACHE_MEM_MAX` | ai-service | `5000` | Memory-tier LRU cap. |
| `AI_TIMEOUT_MS` | gateway | `40000` | Gateway → AI service fetch timeout. |

---

## 4. Evaluation criteria (`eval/rubric.json`, 100 pts)

| ID | Criterion | Pts | Type | Status |
|---|---|---|---|---|
| `widget_lights_up` | Contract works end to end | 15 | auto | ✅ Verified live — `/translate` + `/translate/batch` return valid shapes through the gateway |
| `caching_correctness` | Two-tier, provable, persistent | 20 | auto | ✅ Verified — 2nd request `cached:true` at **0ms** vs 58ms; SQLite survives restart |
| `performance_sla` | `bench.py` exits 0 | 15 | auto | ⏳ **Not yet run** |
| `logging_observability` | `/stats` hit rate, `/health` reports AI, structured logs, **trace correlation** | 10 | auto | ✅ All four sub-checks verified, incl. sentinel `X-Request-Id` in both logs |
| `service_separation_contract` | 400 on bad input; health nests AI health | 10 | auto | ✅ Verified |
| `llm_prompt_quality` | **"natural Mexican Spanish (es-MX)"** | 20 | manual | ❌ **Forfeited by V1** (Dutch). Recoverable via `FORCE_TARGET=""` |
| `deploy_docs` | Fly.io deploy, one-command run, clean git hygiene | 10 | manual | ⏳ Hygiene ✅; deploy pending |

**SLA gate** (`benchmark/sla.json`): hit p95 ≤ 60ms · miss p95 ≤ 3500ms · hit rate ≥ 60% · error rate ≤ 1% · throughput ≥ 20 req/s.

⚠️ `sla.json`'s `cost_model` prices ($3/$15 per MTok) are **placeholders by the file's own admission** — verify against Anthropic's current published rates before trusting any cost figure in `PRODUCT_EVAL.md`.

---

## 5. Known gaps / open items

1. ~~No live LLM call has ever been made.~~ **Done.** `smoke_llm.py` run against the real API on 2026-07-16 — Dutch quality validated: idiomatic UI phrasing (`In winkelwagen`, `Bestverkocht`), prices/order codes/SKUs/URLs/HTML/bare numbers preserved untouched, friendly `je` register throughout. See §7 below for the dependency fix needed to get there.
2. ~~Benchmark not run~~ **Done 2026-07-16.** `python benchmark/bench.py` end-to-end (gateway → AI service), exit 0, all 5 SLAs passed: hit p95 11.8ms (≤60), miss p95 1923ms (≤3500), hit rate 75.0% (≥60), error rate 0.0% (≤1.0), throughput 1471.6 req/s (≥20). Cold pass used 20 real Anthropic calls; no duplicate phrases in the cold batch, so this didn't stress-test concurrent-identical-miss dedup specifically — see D15 below.
3. ~~Fly.io deploy~~ **Done 2026-07-18** — see D16–D20. `PRODUCT_EVAL.md` and the 60–90s video are still outstanding.
4. **Double log line on failure:** a provider error writes both `translate_failed` (route, has batch context) and `unhandled_error` (handler). Mild redundancy; the route-level catch is arguably now unnecessary.
5. **`@app.on_event("startup")` is deprecated** in current FastAPI (warns on boot). It's provided code and works; left alone.
6. ~~`requirements.txt` pins `anthropic==0.39.0`; tested here against 0.116.0.~~ See §7 — this pin turned out to be the actual problem, not a hypothetical one.
7. **`requirements.txt` needed an `httpx<0.28` pin.** `anthropic==0.39.0` still passes `proxies=` into httpx's client constructor; httpx 0.28 dropped that kwarg, so a fresh `pip install -r requirements.txt` crashed on the first `AsyncAnthropic()` call with `TypeError: AsyncClient.__init__() got an unexpected keyword argument 'proxies'`. Fixed by pinning `httpx<0.28` alongside the `anthropic` line. Discovered running `smoke_llm.py` for the first time (2026-07-16); would have hit any fresh grader install too.
8. **Cache is not invalidated by prompt changes.** The key covers text + language, not the prompt. Edit the prompt in `llm.py` and existing rows keep returning the old wording. **`rm translations.db` and restart** while tuning (restart matters — the memory tier is in-process).

---

## 6. Test coverage so far

| Suite | What it proves | Provider |
|---|---|---|
| `llm.py` mock tests (18) | Call params, Dutch override, `FORCE_TARGET` hatch, output cleaning, fail-loud, lazy client | faked |
| `cache.py` tests (24) | Real SQLite: schema, WAL on, no redundant index, LRU eviction → `db_hit` not miss, upsert preserves `access_count`, restart survival, unicode | n/a — real |
| `app.py` e2e (30) | Real FastAPI routes + real SQLite: hit/miss, normalization, Option B flip safety, batch, `/stats`, failures not cached | faked |
| Live integration | Both services over real HTTP: **trace correlation across both logs**, UUID generation, 400/502 paths, AI-down resilience, hit at 0ms | faked |

Everything except translation **quality** is verified. Quality is the one thing only a real key can answer.
