"""Real-SQLite tests for lib/cache.py — no API key, no network, actual disk I/O."""
import asyncio, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import aiosqlite
from lib.cache import TwoTierCache, _key

fails = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))
    if not cond: fails.append(name)

async def main():
    db = os.path.join(tempfile.mkdtemp(), "t.db")

    print("\n1. init() — schema, WAL, and index hygiene")
    c = TwoTierCache(db)
    await c.init()
    await c.init()  # idempotent
    check("init() is idempotent", True)
    async with aiosqlite.connect(db) as d:
        async with d.execute("PRAGMA journal_mode") as cur:
            mode = (await cur.fetchone())[0]
        async with d.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='translations'") as cur:
            idx = [r[0] for r in await cur.fetchall()]
        async with d.execute("PRAGMA table_info(translations)") as cur:
            cols = [r[1] for r in await cur.fetchall()]
    check("journal_mode is WAL", mode.lower() == "wal", mode)
    check("only the PRIMARY KEY autoindex exists (no redundant index)",
          idx == ["sqlite_autoindex_translations_1"], str(idx))
    check("all 7 columns present",
          cols == ["key","source","target","translated","model","access_count","created_at"], str(cols))

    print("\n2. Miss -> set -> memory hit")
    check("cold get is a miss (None)", await c.get("Add to cart", "nl") is None)
    await c.set("Add to cart", "nl", "Voeg toe aan winkelwagen", model="claude-sonnet-4-6")
    check("get after set returns value", await c.get("Add to cart", "nl") == "Voeg toe aan winkelwagen")
    s = await c.stats()
    check("stats: 1 miss, 1 memory_hit", s["misses"] == 1 and s["memory_hits"] == 1, str(s))
    check("stats: requests counted", s["requests"] == 2, str(s))

    print("\n3. Restart survival — new instance, cold memory, same file")
    c2 = TwoTierCache(db)
    await c2.init()
    got = await c2.get("Add to cart", "nl")
    s2 = await c2.stats()
    check("db_hit after 'restart'", got == "Voeg toe aan winkelwagen" and s2["db_hits"] == 1, str(s2))
    check("db hit warmed the memory tier", s2["memory_entries"] == 1, str(s2))
    await c2.get("Add to cart", "nl")
    s2 = await c2.stats()
    check("second lookup is now a memory_hit", s2["memory_hits"] == 1 and s2["db_hits"] == 1, str(s2))

    print("\n4. access_count bumps on db hits")
    async with aiosqlite.connect(db) as d:
        async with d.execute("SELECT access_count FROM translations WHERE key=?", (_key("Add to cart","nl"),)) as cur:
            n = (await cur.fetchone())[0]
    check("access_count incremented past default", n == 2, f"got {n}")

    print("\n5. Cache key isolates languages (Option B)")
    await c2.set("Add to cart", "es-MX", "Agregar al carrito", model="m")
    check("same text, different target -> separate entries",
          await c2.get("Add to cart", "nl") == "Voeg toe aan winkelwagen"
          and await c2.get("Add to cart", "es-MX") == "Agregar al carrito")
    check("size() counts both rows", await c2.size() == 2, str(await c2.size()))

    print("\n6. Upsert preserves access_count (no REPLACE reset)")
    await c2.set("Add to cart", "nl", "Aan winkelwagen toevoegen", model="m2")
    async with aiosqlite.connect(db) as d:
        async with d.execute("SELECT translated, model, access_count FROM translations WHERE key=?",
                             (_key("Add to cart","nl"),)) as cur:
            tr, mdl, ac = await cur.fetchone()
    check("upsert refreshed translated+model", tr == "Aan winkelwagen toevoegen" and mdl == "m2")
    check("upsert did NOT reset access_count", ac >= 2, f"access_count={ac}")
    check("upsert did not duplicate the row", await c2.size() == 2)

    print("\n7. LRU cap + eviction")
    db2 = os.path.join(tempfile.mkdtemp(), "lru.db")
    c3 = TwoTierCache(db2, mem_max=3)
    await c3.init()
    for i in range(3):
        await c3.set(f"s{i}", "nl", f"v{i}", model="m")
    check("memory holds 3", (await c3.stats())["memory_entries"] == 3)
    await c3.get("s0", "nl")               # touch s0 -> now most-recent
    await c3.set("s3", "nl", "v3", model="m")  # forces one eviction
    check("memory still capped at 3", (await c3.stats())["memory_entries"] == 3, str(await c3.stats()))
    check("touched entry s0 survived eviction", _key("s0","nl") in c3._mem)
    check("LRU victim s1 was evicted", _key("s1","nl") not in c3._mem)
    before = (await c3.stats())["db_hits"]
    got = await c3.get("s1", "nl")
    after = (await c3.stats())["db_hits"]
    check("evicted entry falls back to SQLite (db_hit, not miss)",
          got == "v1" and after == before + 1, f"got={got}")

    print("\n8. stats() hit-rate math")
    c4 = TwoTierCache(os.path.join(tempfile.mkdtemp(), "s.db")); await c4.init()
    await c4.get("a", "nl")                       # miss
    await c4.set("a", "nl", "A", model="m")
    for _ in range(3): await c4.get("a", "nl")    # 3 memory hits
    s4 = await c4.stats()
    check("hit_rate_pct = 75.0 (3 hits / 4)", s4["hit_rate_pct"] == 75.0, str(s4))

    print("\n9. Unicode + long text round-trip")
    long_txt = "Free shipping on orders over $50. " * 50
    await c4.set(long_txt, "nl", "Gratis verzending — bij bestellingen boven €50. ✓", model="m")
    check("unicode/em-dash/emoji survive round-trip",
          await c4.get(long_txt, "nl") == "Gratis verzending — bij bestellingen boven €50. ✓")

    print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))

asyncio.run(main())
