"""
lib/cache.py — two-tier cache: memory + SQLite
==============================================
Why two tiers?
  - MEMORY (dict): instant, but lost on restart.
  - SQLite (disk): survives restarts, and is where you can inspect what your
    service has learned. Check memory first, then disk, then LLM.

The cache key must be deterministic for the same (text, target). Hashing the
input with sha256 gives you a compact, collision-safe key.

KEYING NOTE: callers pass the *resolved* target code from llm.resolve_target()
(e.g. "nl"), not the raw code the widget sent ("es-MX"). FORCE_TARGET decides
what language we actually emit, so it must be part of the key — otherwise
flipping FORCE_TARGET would serve Dutch from the cache for a Spanish request
and report it as a healthy hit. Keying on the effective language makes that
collision impossible: a flip simply produces a clean miss.

Not handled by the key: the PROMPT itself. If you edit the prompt in llm.py,
existing rows keep returning the old wording — delete translations.db (and
restart, to clear the memory tier) while tuning.
"""
import hashlib
import os
from collections import OrderedDict

import aiosqlite


def _key(text: str, target: str) -> str:
    return hashlib.sha256(f"{target}::{text}".encode("utf-8")).hexdigest()


# Cap on the memory tier so a long-lived process can't grow without bound.
# Eviction is safe: an evicted entry still lives in SQLite, so the next lookup
# is a db_hit (a few ms), never a miss (a few seconds + an LLM call).
MEM_MAX_DEFAULT = int(os.getenv("CACHE_MEM_MAX", "5000"))


class TwoTierCache:
    def __init__(self, db_path: str, mem_max: int = MEM_MAX_DEFAULT):
        self.db_path = db_path
        self.mem_max = mem_max
        # OrderedDict (not dict) so we can evict least-recently-used cheaply.
        self._mem: OrderedDict[str, str] = OrderedDict()
        self._stats = {"requests": 0, "memory_hits": 0, "db_hits": 0, "misses": 0}

    def _remember(self, k: str, translated: str) -> None:
        """Insert/refresh in the memory tier, evicting LRU entries past the cap."""
        self._mem[k] = translated
        self._mem.move_to_end(k)
        while len(self._mem) > self.mem_max:
            self._mem.popitem(last=False)  # pop the least-recently-used end

    async def init(self) -> None:
        """Create the translations table if it doesn't exist."""
        async with aiosqlite.connect(self.db_path) as db:
            # WAL matters here: our READ path also writes (access_count bump).
            # In SQLite's default journal mode a write locks the whole database,
            # so under concurrent load readers would queue behind it and blow the
            # 60ms cache-hit SLA. WAL lets readers and one writer run together.
            # The setting is persisted in the db file, so setting it once is enough.
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS translations(
                    key          TEXT PRIMARY KEY,
                    source       TEXT NOT NULL,
                    target       TEXT NOT NULL,
                    translated   TEXT NOT NULL,
                    model        TEXT,
                    access_count INTEGER  DEFAULT 1,
                    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # Deliberately NO extra index on `key`: it is the PRIMARY KEY, so
            # SQLite already maintains a unique index for it
            # (sqlite_autoindex_translations_1). A second index would duplicate
            # that same B-tree and just add write cost on every insert.
            await db.commit()

    async def get(self, text: str, target: str) -> str | None:
        """Return a cached translation or None. Check memory, then SQLite."""
        self._stats["requests"] += 1
        k = _key(text, target)

        # 1) memory tier
        if k in self._mem:
            self._stats["memory_hits"] += 1
            self._mem.move_to_end(k)  # mark as recently used
            return self._mem[k]

        # 2) SQLite tier — SELECT and the access_count bump share one connection
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT translated FROM translations WHERE key = ?", (k,)
            ) as cur:
                row = await cur.fetchone()

            if row is None:
                self._stats["misses"] += 1
                return None

            await db.execute(
                "UPDATE translations SET access_count = access_count + 1 WHERE key = ?",
                (k,),
            )
            await db.commit()

        translated = row[0]
        self._remember(k, translated)  # warm memory so the next hit skips disk
        self._stats["db_hits"] += 1
        return translated

    async def set(self, text: str, target: str, translated: str, model: str) -> None:
        """Store a translation in both tiers."""
        k = _key(text, target)
        self._remember(k, translated)
        async with aiosqlite.connect(self.db_path) as db:
            # Upsert, not INSERT OR REPLACE: two concurrent requests for the same
            # text can both miss and both call the LLM, so a conflict is real.
            # REPLACE would delete the row and reset access_count to 1; this
            # refreshes the value while preserving the hit history.
            await db.execute(
                """
                INSERT INTO translations(key, source, target, translated, model)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    translated = excluded.translated,
                    model      = excluded.model
                """,
                (k, text, target, translated, model),
            )
            await db.commit()

    async def size(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM translations") as cur:
                row = await cur.fetchone()
                return row[0] if row else 0

    async def stats(self) -> dict:
        total = self._stats["memory_hits"] + self._stats["db_hits"] + self._stats["misses"]
        hits = self._stats["memory_hits"] + self._stats["db_hits"]
        hit_rate = round(100 * hits / total, 1) if total else 0.0
        return {**self._stats, "hit_rate_pct": hit_rate, "memory_entries": len(self._mem)}
