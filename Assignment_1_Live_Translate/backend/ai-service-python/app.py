"""
FDE · Assignment 1 · Python AI Service  (this is the real assignment)
=====================================================================
A small FastAPI service that translates English → Mexican Spanish with:
  - an LLM call            (lib/llm.py)
  - a two-tier cache       (lib/cache.py)  — memory + SQLite
  - structured logging     (lib/logger.py) — provided, wired for you

The Node gateway forwards the browser's requests here. You implement the
TODOs so the widget lights up. Run:

    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env          # then add your API key
    uvicorn app:app --reload --port 8000
"""
import os
import re
import time

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from lib.cache import TwoTierCache
from lib.llm import resolve_target, translate_text
from lib.logger import get_logger

load_dotenv()

MODEL = os.getenv("MODEL", "claude-sonnet-4-6")
DB_PATH = os.getenv("TRANSLATION_DB_PATH", "translations.db")

_WS = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Collapse whitespace runs to single spaces, then trim.

    Not just a hit-rate trick: HTML renders ANY run of whitespace (spaces,
    newlines, tabs) as a single space, so "Add to\n   cart" and "Add to cart"
    are the same string to a reader. Page chunks arrive with whatever
    indentation the source HTML happened to have, so normalizing collapses them
    onto one cache entry instead of several identical-looking ones.

    We normalize ONCE and use the result everywhere (cache key, LLM input, and
    the `source` column) so the stored row always matches what we translated.

    Caveat: <pre>/<code> text does preserve whitespace in HTML; we don't special
    case it since the widget sends rendered text chunks, not raw markup blocks.
    """
    return _WS.sub(" ", text or "").strip()


app = FastAPI(title="FDE Live Translate — AI Service")
log = get_logger("ai-service")
cache = TwoTierCache(DB_PATH)

# request/response shapes ----------------------------------------------------
class TranslateIn(BaseModel):
    text: str
    target: str = "es-MX"

class BatchIn(BaseModel):
    texts: list[str]
    target: str = "es-MX"


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    """Surface the real reason a translation failed, as a 502.

    FastAPI's default for an unhandled error is a 500 whose body is the literal
    string "Internal Server Error" — the actual cause never leaves the process,
    so the gateway can only report a shrug. We return 502 (this service IS a
    gateway to the LLM provider) with the concrete error plus the trace id, so a
    failure is diagnosable from the browser instead of only from the logs.

    Note: this deliberately exposes upstream error text to the caller. Fine for
    a personal build; on a public deployment you'd usually return a generic
    message + requestId and keep the detail in the logs.

    Fail-loud is preserved: this reports the error, it never invents a
    translation or falls back to the untranslated English.
    """
    request_id = request.headers.get("x-request-id", "-")
    log.error(
        "unhandled_error",
        extra={"requestId": request_id, "error": f"{type(exc).__name__}: {exc}"},
    )
    return JSONResponse(
        status_code=502,
        content={"error": f"{type(exc).__name__}: {exc}", "requestId": request_id},
    )


@app.on_event("startup")
async def startup():
    await cache.init()
    log.info("ai_service_started", extra={"model": MODEL, "db": DB_PATH})


# --- core: translate one string --------------------------------------------
async def translate_one(text: str, target: str) -> dict:
    """Translate a single string, using the cache first.

    Returns a dict shaped exactly like the widget expects:
        {"translated": str, "cached": bool, "latencyMs": int, "model": str}
    """
    text = _normalize(text)
    if not text:
        return {"translated": "", "cached": False, "latencyMs": 0, "model": MODEL}

    t0 = time.perf_counter()

    # Key on the language we will ACTUALLY emit, not the code the caller sent.
    # The widget always sends "es-MX" but FORCE_TARGET decides the real output
    # language, so keying on the raw code would let a FORCE_TARGET flip serve
    # Dutch from cache for a Spanish request — wrong, and reported as a hit.
    key_target = resolve_target(target)

    # 1) cache first — a hit must never reach the LLM
    cached_value = await cache.get(text, key_target)
    if cached_value is not None:
        return {
            "translated": cached_value,
            "cached": True,
            "latencyMs": int((time.perf_counter() - t0) * 1000),
            "model": MODEL,
        }

    # 2) miss — call the provider, then write back so the next one is a hit.
    #    No try/except on purpose: a provider failure must surface as an error,
    #    never as untranslated English pretending to be a translation.
    #    (translate_text resolves the language the same way; passing the raw
    #    target is equivalent since resolve_target is idempotent here.)
    translated = await translate_text(text, target, model=MODEL)
    await cache.set(text, key_target, translated, model=MODEL)

    # 3) same clock on both paths — the hit/miss gap is the whole demo
    return {
        "translated": translated,
        "cached": False,
        "latencyMs": int((time.perf_counter() - t0) * 1000),
        "model": MODEL,
    }


@app.post("/translate")
async def translate(body: TranslateIn, request: Request):
    # The gateway forwards its request ID here; logging it on our line is what
    # makes ONE request greppable across both services' logs by a single id.
    request_id = request.headers.get("x-request-id", "-")
    try:
        result = await translate_one(body.text, body.target)
    except Exception as err:
        # Log the failure (with the trace id) and re-raise — visibility, without
        # ever swallowing the error into a fake "successful" English response.
        log.error("translate_failed", extra={"requestId": request_id, "error": str(err)})
        raise
    log.info(
        "translate",
        extra={
            "requestId": request_id,
            "cached": result["cached"],
            "latencyMs": result["latencyMs"],
            "chars": len(body.text),
        },
    )
    return result


@app.post("/translate/batch")
async def translate_batch(body: BatchIn, request: Request):
    request_id = request.headers.get("x-request-id", "-")
    t0 = time.perf_counter()
    results = []
    try:
        for t in body.texts:
            results.append(await translate_one(t, body.target))
    except Exception as err:
        log.error("translate_batch_failed", extra={"requestId": request_id, "error": str(err)})
        raise
    latency = int((time.perf_counter() - t0) * 1000)
    hits = sum(1 for r in results if r["cached"])
    log.info(
        "translate_batch",
        extra={"requestId": request_id, "count": len(results), "hits": hits, "latencyMs": latency},
    )
    # widget expects {results: [{translated, cached}], latencyMs}
    return {"results": [{"translated": r["translated"], "cached": r["cached"]} for r in results], "latencyMs": latency}


@app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL, "cacheSize": await cache.size()}


@app.get("/stats")
async def stats():
    return await cache.stats()
