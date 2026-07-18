"""End-to-end tests: real FastAPI routes + real SQLite. Only the LLM call is faked."""
import os, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["TRANSLATION_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "app.db")
os.environ["FORCE_TARGET"] = "nl"
os.environ.setdefault("MODEL", "claude-sonnet-4-6")

import app as A

CALLS = []
FAKE = {"Add to cart": "Voeg toe aan winkelwagen", "Best sellers": "Topsellers"}
async def fake_translate(text, target="es-MX", model=None):
    CALLS.append((text, target))
    time.sleep(0.05)                       # pretend the provider takes 50ms
    return FAKE.get(text, f"[nl]{text}")
A.translate_text = fake_translate          # patch the name app.py actually calls

from fastapi.testclient import TestClient

fails = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))
    if not cond: fails.append(name)

with TestClient(A.app) as c:               # triggers startup -> cache.init()
    print("\n1. Miss then hit — the core flow")
    r1 = c.post("/translate", json={"text": "Add to cart", "target": "es-MX"}).json()
    check("miss returns translation", r1["translated"] == "Voeg toe aan winkelwagen", str(r1))
    check("miss reports cached=False", r1["cached"] is False)
    check("miss called the LLM once", len(CALLS) == 1)
    check("response has model", r1["model"] == "claude-sonnet-4-6")

    r2 = c.post("/translate", json={"text": "Add to cart", "target": "es-MX"}).json()
    check("hit returns same translation", r2["translated"] == r1["translated"])
    check("hit reports cached=True", r2["cached"] is True)
    check("hit did NOT call the LLM", len(CALLS) == 1, f"{len(CALLS)} calls")
    check("hit is faster than miss", r2["latencyMs"] < r1["latencyMs"], f"{r2['latencyMs']}ms vs {r1['latencyMs']}ms")
    check("hit latency well under 60ms SLA", r2["latencyMs"] < 60, f"{r2['latencyMs']}ms")

    print("\n2. Whitespace normalization -> same cache entry")
    before = len(CALLS)
    for variant in ["Add to\n   cart", "  Add to cart  ", "Add to\t\tcart", "Add to\n\ncart"]:
        rv = c.post("/translate", json={"text": variant, "target": "es-MX"}).json()
        check(f"{variant!r} -> cache hit", rv["cached"] is True and rv["translated"] == "Voeg toe aan winkelwagen", str(rv))
    check("no extra LLM calls for whitespace variants", len(CALLS) == before, f"{len(CALLS)-before} extra")

    print("\n3. Empty / whitespace-only input")
    for empty in ["", "   ", "\n\t "]:
        re_ = c.post("/translate", json={"text": empty, "target": "es-MX"}).json()
        check(f"{empty!r} -> empty, no LLM", re_["translated"] == "" and re_["cached"] is False and re_["latencyMs"] == 0)
    check("empty input never called the LLM", len(CALLS) == before)

    print("\n4. Option B — FORCE_TARGET flip cannot serve wrong language")
    os.environ["FORCE_TARGET"] = "es-MX"
    calls_before = len(CALLS)
    rflip = c.post("/translate", json={"text": "Add to cart", "target": "es-MX"}).json()
    check("flip to es-MX is a MISS, not a Dutch hit", rflip["cached"] is False, str(rflip))
    check("flip triggered a fresh LLM call", len(CALLS) == calls_before + 1)
    os.environ["FORCE_TARGET"] = "nl"
    rback = c.post("/translate", json={"text": "Add to cart", "target": "es-MX"}).json()
    check("flipping back still hits the Dutch entry", rback["cached"] is True and rback["translated"] == "Voeg toe aan winkelwagen", str(rback))

    print("\n5. /translate/batch contract")
    rb = c.post("/translate/batch", json={"texts": ["Add to cart", "Best sellers", "Add to cart"], "target": "es-MX"}).json()
    check("batch returns results + latencyMs", "results" in rb and "latencyMs" in rb)
    check("batch result shape is {translated, cached}", set(rb["results"][0]) == {"translated", "cached"}, str(rb["results"][0]))
    check("batch translated all 3", [x["translated"] for x in rb["results"]] == ["Voeg toe aan winkelwagen", "Topsellers", "Voeg toe aan winkelwagen"])
    check("duplicate within batch is a hit", rb["results"][2]["cached"] is True)

    print("\n6. /health and /stats")
    h = c.get("/health").json()
    check("health ok + model + cacheSize", h["status"] == "ok" and h["model"] == "claude-sonnet-4-6" and h["cacheSize"] >= 2, str(h))
    st = c.get("/stats").json()
    check("stats exposes hit_rate_pct", "hit_rate_pct" in st, str(st))
    check("stats hit rate above the 60% SLA", st["hit_rate_pct"] >= 60, str(st))

    print("\n7. FAIL LOUD — provider error must not return English")
    async def boom(text, target="es-MX", model=None): raise RuntimeError("provider 529")
    A.translate_text = boom
    try:
        rr = c.post("/translate", json={"text": "Never seen before", "target": "es-MX"})
        check("error is not a 2xx with English", rr.status_code >= 500, f"status={rr.status_code} body={rr.text[:80]}")
    except RuntimeError:
        check("error is not a 2xx with English", True)   # TestClient re-raises = loud
    A.translate_text = fake_translate
    async def check_not_cached():
        return await A.cache.get("Never seen before", "nl")
    import asyncio
    check("failed translation was NOT cached", asyncio.run(check_not_cached()) is None)

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
