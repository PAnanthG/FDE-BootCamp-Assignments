# How to apply this package

These are ONLY the files changed or added for this build. The scaffold
(`widget/`, `extension/`, `benchmark/`, `eval/`, `lib/logger.py`, `requirements.txt`,
`package.json`, the READMEs) is unmodified — get it from the upstream repo.

## 1. Clone the assignment (if you haven't)

```bash
git clone https://github.com/hamzafarooq/multi-agent-course.git
cd multi-agent-course/FDE/Assignment_1_Live_Translate
```

## 2. Overlay this package

Unzip so these paths land on top of the assignment folder:

```
.gitignore                                    <- new
CLAUDE.md                                     <- new (Claude Code reads this)
DECISIONS.md                                  <- new (why the code looks like this)
backend/ai-service-python/app.py              <- MODIFIED (translate_one, 502 handler, tracing)
backend/ai-service-python/lib/llm.py          <- MODIFIED (was a stub)
backend/ai-service-python/lib/cache.py        <- MODIFIED (was a stub)
backend/ai-service-python/.env.example        <- MODIFIED (FORCE_TARGET, CACHE_MEM_MAX)
backend/ai-service-python/smoke_llm.py        <- new (live EN->NL check, needs your key)
backend/ai-service-python/tests/              <- new (4 suites, no key needed)
backend/gateway-node/server.js                <- MODIFIED (was 2 stubs)
backend/gateway-node/.env.example             <- MODIFIED (AI_TIMEOUT_MS)
```

From the assignment root:

```bash
unzip -o live-translate-build.zip
```

## 3. Verify it landed (no API key needed)

```bash
cd backend/ai-service-python
pip install -r requirements.txt
python3 tests/test_cache.py          # expect: ALL PASSED
python3 tests/test_app.py            # expect: ALL PASSED

cd ../gateway-node && npm install && cd ../ai-service-python
bash tests/integration_test.sh       # expect: ALL PASSED
```

## 4. Then add your key and check the Dutch

```bash
cp .env.example .env      # put your real ANTHROPIC_API_KEY in it
python3 smoke_llm.py
```

## 5. Open in Claude Code

```bash
cd multi-agent-course/FDE/Assignment_1_Live_Translate
claude
```

It reads `CLAUDE.md` automatically. Point it at `DECISIONS.md` for the reasoning.

---

**Outstanding work** (see `DECISIONS.md` §5):
1. `smoke_llm.py` with a real key — no live LLM call has ever been made
2. `python benchmark/bench.py` — must exit 0
3. Fly.io deploy of both services
4. `PRODUCT_EVAL.md` + 60–90s video
