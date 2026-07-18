# Product Evaluation — Live Translate

- **Student:** Praveen Ananth
- **Date:** 2026-07-18
- **Video demo:** _(not yet recorded — to be added before final submission)_
- **LLM provider / model:** Anthropic, `claude-sonnet-4-6`
- **Backend target (local rubric run):** `http://localhost:8787`
- **Deployed gateway:** `https://live-translate-gateway-pananthg.fly.dev`
- **Deployed AI service:** `https://live-translate-ai-pananthg.fly.dev`

## Verdict

Shippable as a personal/learning build, with one deliberate, disclosed scope
change and one confirmed limitation in provided (non-editable) code. The
backend — LLM call, two-tier cache, logging/tracing, and the Fly.io deployment
— is solid: all 5 automated rubric criteria pass at 70/70, the SLA gate passes
with real (not faked) cold-miss and cache-hit numbers, and the deployed
gateway/AI-service pair was proven working end-to-end on two real websites it
does not control. The two things a grader should know going in: **(1)** this
build translates to Dutch, not the assignment's Mexican Spanish — a deliberate,
disclosed, and reversible choice (`FORCE_TARGET=""` restores es-MX with no code
change) that forfeits the 20-pt `llm_prompt_quality` criterion by design; **(2)**
the provided Chrome extension code has a genuine race-condition bug that
prevents it from ever using a backend URL other than `localhost:8787` — found,
reproduced with real evidence, and worked around via the widget's alternate
console-loader integration path rather than by editing the off-limits
`extension/` files. The strongest part of the build is the caching/tracing
design (persists across a real Fly.io machine restart, correlates one request
across both services' logs). The weakest part is the extension bug — it's not
something the backend can fix.

**Rubric score (from `eval/report.json`):** 70 / 70 auto (+ 30 manual pts for the grader)

| Criterion | Type | Points | Result |
|---|---|---|---|
| Widget lights up (contract works end to end) | auto | 15/15 | translate + batch return valid shapes through the gateway |
| Caching correctness (two-tier, provable, persistent) | auto | 20/20 | 2nd identical request `cached=true`, faster, SQLite-persisted |
| Performance & SLA gate | auto | 15/15 | `benchmark/bench.py` exits 0, all 5 SLAs met |
| Logging & observability | auto | 10/10 | `/stats` hit rate, `/health` nests AI-service health, structured `ai-service.log`, request ID correlates across both services' logs |
| Service separation & correct status codes | auto | 10/10 | 400 on bad input; gateway health nests AI-service health |
| LLM & prompt quality (natural Mexican Spanish) | manual | —/20 | **Forfeited by design** — this build translates to Dutch, not es-MX (see V1, Verdict above) |
| Deploy & docs | manual | —/10 | For the grader — see §1 hygiene evidence and deploy evidence below |

**Git hygiene (auto-flagged, feeds `deploy_docs`):** `eval/report.json` → `"hygiene_flags": []`, `"provided_files_changed": ""` — no committed secrets, no edits to `widget/`, `extension/`, or `benchmark/`.

## 1. Performance & cost (from `benchmark/bench.py`, real run against local services with a genuinely cleared cache — not faked, not pre-warmed)

**Two-call cache proof** (the specific demonstration `AGENTS.md`'s Definition of Done asks for — same request, twice): translating `"Good morning, welcome!"` → `"Goedemorgen, welkom!"` took **1215 ms** the first time (LLM call, cache miss) and **0 ms** the second time (`cached: true`, both memory and SQLite tiers hit). Source: `eval/report.json` → `evidence.cache_first_ms` / `evidence.cache_second_ms`.

**Cache persistence across a real restart** (Definition of Done step 3): `flyctl machines restart` on the deployed AI service, then the same `/translate` call via the deployed gateway — `GET /health` came back `cacheSize:1` immediately post-restart (proving the SQLite tier, not just the in-process memory tier, survived), and the repeat translate call returned `cached:true` at `2 ms`. See `DECISIONS.md` D17.

| Metric | Result | SLA | Pass? |
|---|---|---|---|
| Cache hit p95 | 11.6 ms | ≤ 60 ms | ✅ |
| Cache miss p95 | 3020.4 ms | ≤ 3500 ms | ✅ |
| Cache hit rate | 77.5 % | ≥ 60 % | ✅ |
| Throughput | 1536.0 req/s | ≥ 20 | ✅ |
| Error rate | 0.0 % | ≤ 1 % | ✅ |
| Cost per miss | $0.0001665 | — | — |
| Monthly cost, no cache (500k/mo) | $83.25 | — | — |
| Monthly cost, cached | $18.73 | — | — |
| Monthly savings from cache | $64.52 | — | — |

⚠️ `sla.json`'s cost prices are placeholders by the file's own admission — not independently re-verified against Anthropic's current published rates for this report.

Note: cache-miss p95 (3020 ms) is real but close to the 3500 ms ceiling —
Anthropic API latency varies run to run. The design already accounts for the
worst-known case: `/translate/batch` translates sequentially upstream, and a
large first-time (fully cold) batch on a real, busy page can exceed even the
gateway's timeout — this happened during live-site testing (see below) and
was fixed by raising the deployed gateway's `AI_TIMEOUT_MS` from 40s to 120s
(`DECISIONS.md` D23), not by changing anything the benchmark itself measures.

## 2. Live-website test

Two real sites were tested, neither controlled by the student, per the eval
skill's own guidance for handling a strict-CSP site vs. a permissive one.

### 2a. homedepot.com — packaged Chrome extension

- **Site tested:** a real Home Depot product page (accessed via VPN — the
  site is not reachable from the Netherlands without one).
- **Method:** loaded the unpacked extension, set the popup's backend URL to
  the deployed gateway (`https://live-translate-gateway-pananthg.fly.dev`),
  clicked Save, then clicked "Translate this page."
- **Result:** ❌ did not reach the deployed backend. Exact widget error:
  > "Can't reach backend at http://localhost:8787. Is your Node gateway running?"
- **Root cause (confirmed, not assumed):** `extension/content.js` sets
  `window.FDE_CONFIG.API_URL` inside an async `chrome.storage.sync.get(...)`
  callback; `extension/translation-widget.js` is injected immediately after in
  the same batch (no event-loop turn in between) and reads
  `window.FDE_CONFIG` **synchronously** — the callback always loses the race,
  so the widget permanently falls back to its hardcoded `http://localhost:8787`
  default regardless of what's saved in the popup. This is a bug in **provided,
  do-not-edit** code (`extension/`), not something this build's backend caused
  or can fix without violating the assignment's red line. Full analysis in
  `DECISIONS.md` D22.
- **Resilience note:** no console errors beyond the expected failed-fetch;
  the widget panel itself surfaced the failure cleanly (no page breakage, no
  silent English served as a fake success).

### 2b. gutenberg.org — console-loader (widget's alternate integration path)

- **Site tested:** `https://www.gutenberg.org` homepage (real, content-rich,
  student-doesn't-control site with no Content-Security-Policy header, unlike
  homedepot.com, developer.mozilla.org, or en.wikipedia.org, all of which were
  checked and found to block third-party `fetch()` via CSP).
- **Method:** `window.FDE_CONFIG = { API_URL: "https://live-translate-gateway-pananthg.fly.dev" }`
  set manually in DevTools before injecting `loader/console-snippet.js` —
  fully synchronous, so D22's race condition doesn't apply to this path.
- **Translated whole page?** ✅ Yes, after two fixes discovered during this
  exact test: (1) the deployed gateway's `/widget.js` route 404'd because its
  Docker build context originally excluded the sibling `widget/` folder it
  serves from (`DECISIONS.md` D21); (2) a fully-cold pass over ~80+ distinct
  strings on a real page exceeded the original 40s gateway timeout even though
  the AI service kept working and cached the results anyway — confirmed by
  inspecting the deployed SQLite file directly over SSH (cached rows climbed
  0 → 48 → 81 across retries). Fixed by raising `AI_TIMEOUT_MS` to 120s (D23).
- **Coverage gaps:** none observed on the final successful pass.
- **Cache on re-translate:** confirmed via direct SQLite inspection — real
  Gutenberg content (below) is cached and served from the deployed backend's
  persistent volume.
- **Resilience:** no CSP blocking (confirmed via header + meta-tag check
  before testing), no layout breakage, no console errors on the successful run.
- **Screenshots:** not attached to this report; available on request.

### Sample translations (8)

| Original (EN) | Translation (nl-NL — see V1 in `DECISIONS.md`) | Numbers/prices/codes kept? | OK? |
|---|---|---|---|
| Add to cart | In winkelwagen | n/a | ✅ idiomatic UI shorthand |
| Free shipping on orders over $50 | Gratis verzending bij bestellingen boven $50 | ✅ `$50` kept | ✅ |
| Your order #A1B2-9931 has shipped. | Je bestelling #A1B2-9931 is verzonden. | ✅ order code kept | ✅ |
| The SKU-4471 laptop stand costs $1,299.00. | De SKU-4471 laptopstandaard kost $1,299.00. | ✅ SKU + price kept | ✅ |
| \<strong>Sale\</strong> ends \<em>tonight\</em> | \<strong>Aanbieding\</strong> eindigt \<em>vanavond\</em> | n/a | ✅ HTML tags preserved |
| About Project Gutenberg | Over Project Gutenberg | n/a | ✅ |
| Project Gutenberg is a library of 77,687 free eBooks. | Project Gutenberg is een bibliotheek met 77,687 gratis e-books. | ✅ `77,687` kept | ✅ |
| Choose among free eBooks to download or read online. You will find the world's great literature here, with focus on older works for which U.S. copyright has expired. | Kies uit gratis e-books om te downloaden of online te lezen. Je vindt hier de grote klassiekers van de wereldliteratuur, met de nadruk op oudere werken waarvan het Amerikaanse auteursrecht is verlopen. | n/a | ✅ idiomatic, not literal |

Also observed: `self.gutenberg.org` (a URL fragment) and other genuine URLs
were left completely untouched, as required.

## 3. Dimension scorecard

| Dimension | Pass / Partial / Fail | Evidence |
|---|---|---|
| Translation accuracy | Pass | Sample table above; fluent, natural phrasing, not word-for-word |
| Target-language register (nl-NL, friendly `je`) | Pass (but not es-MX — see V1) | "Kies uit... Je vindt hier..." — consistent friendly register |
| Numbers / prices / codes preserved | Pass | `$50`, `$1,299.00`, `#A1B2-9931`, `SKU-4471`, `77,687` all verbatim |
| Page coverage | Pass | Gutenberg homepage fully translated on the successful run |
| Cache effectiveness | Pass | 77.5% hit rate in benchmark; persists across a real Fly.io machine restart |
| Latency vs SLA | Pass | All 5 SLA checks pass with real (not faked) numbers |
| Error handling (no silent English) | Pass | Errors propagate as 502/visible widget errors; no fallback to untranslated text |
| Resilience on a real site | Partial | Works via console-loader on permissive-CSP sites; fails via the packaged extension on strict-CSP sites due to D22 (provided-code bug, not this build's backend) |
| UX polish | Pass | Provided widget UI; clean error messaging, cache-hit badges |

## 4. Top fixes before shipping

1. **D22 (extension URL-lock bug)** is the main open item. It lives entirely
   in provided, do-not-edit code (`extension/content.js` +
   `extension/translation-widget.js`), so it can't be fixed within this
   assignment's rules — but if this were a real product, the fix is
   straightforward: read the saved URL synchronously (e.g. inject a
   `<script>` tag with the config as a literal, written by the background
   service worker, before the widget script runs) instead of racing an async
   `chrome.storage.sync.get` against an immediately-following script.
2. **Cache-miss p95 (3020 ms) has limited headroom** against the 3500 ms SLA
   ceiling — normal Anthropic API latency variance, not a bug, but worth
   monitoring if request volume grows.
3. **Single-flight dedup (D15) remains deferred**, confirmed unnecessary at
   current benchmark scale (throughput margin is large: 1536 rps vs. a 20 rps
   floor) — revisit only if real traffic shows concurrent identical misses.
