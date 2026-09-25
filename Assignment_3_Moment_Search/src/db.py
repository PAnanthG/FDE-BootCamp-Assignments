"""Postgres (Neon) access layer — the videos manifest, source of truth.

One row per (user's) video; `status` tracks the ingest lifecycle:
pending -> fetching -> sampling -> embedding -> indexed | skipped | failed
(skipped = duplicate (user_id, source_hash); indexed = searchable in Qdrant).
"""
from __future__ import annotations

import os
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import DATABASE_URL, INFLIGHT_STATUSES

_pool: ConnectionPool | None = None
_pool_pid: int | None = None


def pool() -> ConnectionPool:
    """Process-local pool. Prefect runs flows in subprocesses; a child must
    never reuse the parent's SSL connections (corrupts the TLS stream), so a
    fork gets a fresh pool."""
    global _pool, _pool_pid
    if _pool is None or _pool_pid != os.getpid():
        # check= pings each connection before lending it out — Neon silently
        # drops idle SSL connections, which otherwise 500s the first request
        # after a quiet period.
        _pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=5,
                               check=ConnectionPool.check_connection,
                               kwargs={"row_factory": dict_row})
        _pool_pid = os.getpid()
    return _pool


SCHEMA = """
CREATE TABLE IF NOT EXISTS ms_videos (
    id           TEXT PRIMARY KEY,           -- yt_<id> | up_<uuid>
    user_id      TEXT NOT NULL,
    source       TEXT NOT NULL,              -- youtube | upload
    url          TEXT,                       -- YouTube URL (source=youtube)
    storage_key  TEXT,                       -- uploads/<user>/<id>.<ext> (source=upload)
    source_hash  TEXT,                       -- sha256 of the file / yt video id
    title        TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    error        TEXT,
    frame_count  INT,
    progress     REAL,                       -- 0..1 within the current stage
    attempts     INT NOT NULL DEFAULT 0,
    embed_version TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ms_videos_user_idx   ON ms_videos (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ms_videos_status_idx ON ms_videos (status);
CREATE INDEX IF NOT EXISTS ms_videos_hash_idx   ON ms_videos (user_id, source_hash);

-- Assignment 3: papers and decks share this manifest rather than getting a
-- table of their own. They need the identical lifecycle (pending -> ... ->
-- indexed | failed), the identical fair-dispatch claim (wfq_claim), and the
-- identical metadata join at citation time (videos_by_ids). A parallel
-- ms_documents table would have meant duplicating all three, and GET
-- /admin/sources would then have to union two schemas that must not drift.
--
-- Additive only: new nullable columns, so every existing video query is
-- unaffected and `source` keeps its meaning for rows written by the video path.
--   source: youtube | upload | paper | deck
--   kind:   video | paper | deck   (NULL on rows predating this = video)
ALTER TABLE ms_videos ADD COLUMN IF NOT EXISTS kind       TEXT;
ALTER TABLE ms_videos ADD COLUMN IF NOT EXISTS chunk_count INT;
UPDATE ms_videos SET kind = 'video' WHERE kind IS NULL;
CREATE INDEX IF NOT EXISTS ms_videos_kind_idx ON ms_videos (user_id, kind);

-- Bring-your-own-model: a tenant's hosted LLM endpoint (vLLM / Ollama / any
-- OpenAI-compatible server, NVIDIA NIM, or Anthropic). When a row exists the
-- read path answers with THIS model instead of the server's LLM_* env config.
CREATE TABLE IF NOT EXISTS ms_user_llms (
    user_id    TEXT PRIMARY KEY,
    provider   TEXT NOT NULL DEFAULT 'openai',  -- openai | nvidia | anthropic
    model      TEXT NOT NULL,
    base_url   TEXT,                            -- e.g. http://my-vllm:8000/v1
    api_key    TEXT,                            -- optional (vLLM often has none)
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def init_schema() -> None:
    """Run the (idempotent, IF NOT EXISTS) schema DDL.

    Retries once on DeadlockDetected. Observed in practice: worker.py calls
    this at process startup, and two instances starting within moments of each
    other - a restart racing an old process still shutting down, or two worker
    replicas booting together - can deadlock on the ALTER TABLE/UPDATE
    statements. The DDL is safe to retry (every statement is IF NOT EXISTS or
    idempotent), and an uncaught deadlock here crashes the whole worker
    process, killing any flow runs it was mid-executing with no reconciler to
    resume them - a resilience gap distinct from and worse than the schema
    error itself.
    """
    import time as _time

    import psycopg

    for attempt in range(2):
        try:
            with pool().connection() as conn:
                conn.execute(SCHEMA)
            return
        except psycopg.errors.DeadlockDetected:
            if attempt == 1:
                raise
            _time.sleep(1.0)


def upsert_pending(video: dict[str, Any]) -> dict:
    """Insert a video as pending; re-submitting an existing id resets it."""
    with pool().connection() as conn:
        row = conn.execute(
            """
            INSERT INTO ms_videos (id, user_id, source, url, storage_key, source_hash, title, status)
            VALUES (%(id)s, %(user_id)s, %(source)s, %(url)s, %(storage_key)s,
                    %(source_hash)s, %(title)s, 'pending')
            ON CONFLICT (id) DO UPDATE SET
                url = COALESCE(EXCLUDED.url, ms_videos.url),
                storage_key = COALESCE(EXCLUDED.storage_key, ms_videos.storage_key),
                source_hash = COALESCE(EXCLUDED.source_hash, ms_videos.source_hash),
                title = COALESCE(EXCLUDED.title, ms_videos.title),
                status = 'pending', error = NULL, progress = NULL, updated_at = now()
            RETURNING *
            """,
            video,
        ).fetchone()
    return row


def set_status(video_id: str, status: str, *, error: str | None = None,
               title: str | None = None, frame_count: int | None = None,
               source_hash: str | None = None, embed_version: str | None = None,
               progress: float | None = None, chunk_count: int | None = None) -> None:
    with pool().connection() as conn:
        conn.execute(
            """
            UPDATE ms_videos SET status = %s, error = %s,
                title = COALESCE(%s, title),
                frame_count = COALESCE(%s, frame_count),
                source_hash = COALESCE(%s, source_hash),
                embed_version = COALESCE(%s, embed_version),
                chunk_count = COALESCE(%s, chunk_count),
                progress = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (status, error, title, frame_count, source_hash, embed_version,
             chunk_count, progress, video_id),
        )


def upsert_pending_document(doc: dict[str, Any]) -> dict:
    """Insert a paper/deck as pending. Mirrors upsert_pending for videos.

    Re-submitting the same id resets it to pending, which is what makes a
    retry after a permanent failure a plain re-POST rather than a special case.
    """
    with pool().connection() as conn:
        row = conn.execute(
            """
            INSERT INTO ms_videos (id, user_id, source, kind, url, storage_key,
                                   source_hash, title, status)
            VALUES (%(id)s, %(user_id)s, %(kind)s, %(kind)s, %(url)s,
                    %(storage_key)s, %(source_hash)s, %(title)s, 'pending')
            ON CONFLICT (id) DO UPDATE SET
                url = COALESCE(EXCLUDED.url, ms_videos.url),
                storage_key = COALESCE(EXCLUDED.storage_key, ms_videos.storage_key),
                source_hash = COALESCE(EXCLUDED.source_hash, ms_videos.source_hash),
                title = COALESCE(EXCLUDED.title, ms_videos.title),
                status = 'pending', error = NULL, progress = NULL,
                chunk_count = NULL, updated_at = now()
            RETURNING *
            """,
            doc,
        ).fetchone()
    return row


def list_sources(user_id: str, kind: str | None = None) -> list[dict]:
    """Unified video + document listing for GET /admin/sources.

    `kind` is NULL on rows written before this column existed; those are
    videos, so it is coalesced rather than filtered on directly.
    """
    q = ("SELECT *, COALESCE(kind, 'video') AS kind FROM ms_videos "
         "WHERE user_id = %s")
    params: list = [user_id]
    if kind:
        q += " AND COALESCE(kind, 'video') = %s"
        params.append(kind)
    q += " ORDER BY created_at DESC"
    with pool().connection() as conn:
        return conn.execute(q, tuple(params)).fetchall()


def set_progress(video_id: str, progress: float) -> None:
    with pool().connection() as conn:
        conn.execute("UPDATE ms_videos SET progress = %s, updated_at = now() WHERE id = %s",
                     (round(progress, 3), video_id))


def bump_attempts(video_id: str) -> int:
    with pool().connection() as conn:
        row = conn.execute(
            "UPDATE ms_videos SET attempts = attempts + 1, updated_at = now() WHERE id = %s RETURNING attempts",
            (video_id,),
        ).fetchone()
    return row["attempts"] if row else 0


def get_video(video_id: str) -> dict | None:
    with pool().connection() as conn:
        return conn.execute("SELECT * FROM ms_videos WHERE id = %s", (video_id,)).fetchone()


def find_duplicate(user_id: str, source_hash: str, exclude_id: str) -> dict | None:
    """An already-indexed video with the same content for the same user."""
    with pool().connection() as conn:
        return conn.execute(
            """
            SELECT * FROM ms_videos
            WHERE user_id = %s AND source_hash = %s AND id <> %s AND status = 'indexed'
            LIMIT 1
            """,
            (user_id, source_hash, exclude_id),
        ).fetchone()


def list_videos(user_id: str, status: str | None = None) -> list[dict]:
    q = "SELECT * FROM ms_videos WHERE user_id = %s"
    params: list = [user_id]
    if status:
        q += " AND status = %s"
        params.append(status)
    q += " ORDER BY created_at DESC"
    with pool().connection() as conn:
        return conn.execute(q, tuple(params)).fetchall()


def videos_by_ids(ids: list[str]) -> dict[str, dict]:
    """Metadata join for search citations (title/url/source live here, not in Qdrant)."""
    if not ids:
        return {}
    with pool().connection() as conn:
        rows = conn.execute("SELECT * FROM ms_videos WHERE id = ANY(%s)", (ids,)).fetchall()
    return {r["id"]: r for r in rows}


def delete_video(video_id: str) -> None:
    with pool().connection() as conn:
        conn.execute("DELETE FROM ms_videos WHERE id = %s", (video_id,))


# ── Fair scheduling (WFQ) ────────────────────────────────────────────────────

def count_inflight() -> int:
    """How many videos currently occupy execution capacity (scheduled/running)."""
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM ms_videos WHERE status = ANY(%s)",
            (list(INFLIGHT_STATUSES),),
        ).fetchone()
    return row["n"] if row else 0


def wfq_claim(limit: int) -> list[dict]:
    """Atomically claim up to `limit` pending videos in FAIR (round-robin across
    users) order, flipping them pending -> queued. Returns the claimed rows.

    Fairness: rank each user's pending videos by age (row_number partitioned by
    user_id), then order by that rank first — so we take everyone's oldest, then
    everyone's 2nd, ... A user who dumped 50 videos only gets one slot per round,
    exactly like the others. The UPDATE ... WHERE status='pending' RETURNING is
    the atomic claim: if two dispatchers race, each row is handed out once.
    """
    if limit <= 0:
        return []
    with pool().connection() as conn:
        picked = conn.execute(
            """
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY user_id ORDER BY created_at, id) AS rn
                FROM ms_videos WHERE status = 'pending'
            ) t
            ORDER BY rn, id
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        ids = [r["id"] for r in picked]
        if not ids:
            return []
        return conn.execute(
            """
            UPDATE ms_videos SET status = 'queued', updated_at = now()
            WHERE id = ANY(%s) AND status = 'pending'
            RETURNING id, user_id, COALESCE(kind, 'video') AS kind
            """,
            (ids,),
        ).fetchall()


# ── Crash recovery: sources orphaned by a dead worker ────────────────────────

def find_stale_inflight(stale_after_s: int) -> list[dict]:
    """In-flight rows with no update in `stale_after_s` seconds - orphaned by
    a crash. set_status/set_progress refresh updated_at on every real tick of
    a running flow, so a row only goes stale when nothing is left alive to
    update it (see config.RECONCILE_STALE_AFTER_S for the margin reasoning)."""
    with pool().connection() as conn:
        return conn.execute(
            """
            SELECT id, user_id, status, attempts, updated_at
            FROM ms_videos
            WHERE status = ANY(%s)
              AND updated_at < now() - (%s || ' seconds')::interval
            ORDER BY updated_at
            """,
            (list(INFLIGHT_STATUSES), stale_after_s),
        ).fetchall()


def reconcile_row(row_id: str, *, to_status: str, error: str) -> None:
    """Apply the reconciler's decision for one orphaned row.

    The DECISION (retry vs give up) belongs to the caller
    (dispatcher.reconcile_once, pure and unit-tested); this only applies it.
    'pending' lets the fair dispatcher reclaim it normally on the next tick;
    'failed' is terminal, same as any other failure.
    """
    if to_status not in ("pending", "failed"):
        raise ValueError(f"to_status must be 'pending' or 'failed', got {to_status!r}")
    with pool().connection() as conn:
        if to_status == "pending":
            conn.execute(
                "UPDATE ms_videos SET status='pending', error=%s, "
                "progress=NULL, updated_at=now() WHERE id=%s",
                (error, row_id))
        else:
            conn.execute(
                "UPDATE ms_videos SET status='failed', error=%s, "
                "updated_at=now() WHERE id=%s",
                (error, row_id))


# ── Bring-your-own-model (per-tenant LLM endpoint) ───────────────────────────

def get_user_llm(user_id: str) -> dict | None:
    with pool().connection() as conn:
        return conn.execute("SELECT * FROM ms_user_llms WHERE user_id = %s",
                            (user_id,)).fetchone()


def set_user_llm(user_id: str, *, provider: str, model: str,
                 base_url: str | None, api_key: str | None) -> dict:
    """Upsert a tenant's model endpoint. An empty api_key keeps the stored one
    (so users can change model/URL without re-pasting their secret)."""
    with pool().connection() as conn:
        return conn.execute(
            """
            INSERT INTO ms_user_llms (user_id, provider, model, base_url, api_key)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                provider = EXCLUDED.provider,
                model = EXCLUDED.model,
                base_url = EXCLUDED.base_url,
                api_key = COALESCE(NULLIF(EXCLUDED.api_key, ''), ms_user_llms.api_key),
                updated_at = now()
            RETURNING *
            """,
            (user_id, provider, model, base_url, api_key),
        ).fetchone()


def delete_user_llm(user_id: str) -> None:
    with pool().connection() as conn:
        conn.execute("DELETE FROM ms_user_llms WHERE user_id = %s", (user_id,))
