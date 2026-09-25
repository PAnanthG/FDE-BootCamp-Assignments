"""Fair dispatcher — the WFQ scheduler that sits in front of Prefect.

Why this exists: if the API enqueued every video to Prefect at register-time,
Prefect would run them in submitted order (FIFO) — one user who uploads 50
videos blocks everyone behind them. Instead, videos wait `pending` in Postgres
and THIS loop admits them:

  every DISPATCH_INTERVAL_S:
    reconcile any source orphaned by a dead worker (see reconcile_once)
    slots = DISPATCH_MAX_INFLIGHT - (videos currently queued/running)
    claim up to `slots` pending videos in FAIR order (round-robin across users)
    schedule a Prefect run for each

Because only ~capacity videos are ever handed to Prefect at once, the *waiting
line lives in our DB, fairly ordered* (db.wfq_claim) rather than FIFO inside
Prefect. No user can starve the others. Set ENABLE_FAIR_DISPATCH=false to fall
back to immediate FIFO enqueue (useful for A/B teaching the difference) — the
reconciler below is scoped to fair-dispatch mode only, same as this thread.

Runs as a background thread in worker.py. With one worker that's exact; with
several, each runs a dispatcher — the atomic claim keeps videos handed out once,
at worst mildly over-admitting (harmless; Prefect still caps execution).
"""
from __future__ import annotations

import threading
import time

from . import config, db, jobs


def dispatch_once() -> int:
    """Admit as many fairly-chosen pending videos as free capacity allows.
    Returns how many were dispatched this tick."""
    slots = config.DISPATCH_MAX_INFLIGHT - db.count_inflight()
    if slots <= 0:
        return 0
    claimed = db.wfq_claim(slots)
    for row in claimed:
        try:
            # Papers and decks share this queue with videos, so the kind on the
            # claimed row decides which deployment gets the run.
            jobs.enqueue_source(row["id"], row["user_id"],
                                row.get("kind") or "video")
        except Exception as exc:
            # Couldn't reach Prefect — put it back so it's retried next tick.
            db.set_status(row["id"], "pending", error=f"dispatch: {exc}")
    if claimed:
        print(f"[dispatch] admitted {len(claimed)} source(s) "
              f"({db.count_inflight()}/{config.DISPATCH_MAX_INFLIGHT} in flight)")
    return len(claimed)


def _reconcile_decision(attempts: int, max_attempts: int) -> str:
    """Pure: does a stale row get one more try, or is it given up on?

    Separated from reconcile_once() so this one-line policy is testable
    without a database - the SQL plumbing (db.find_stale_inflight,
    db.reconcile_row) is exercised live, same as the rest of db.py.
    """
    return "failed" if attempts >= max_attempts else "pending"


def reconcile_once() -> int:
    """Recover sources orphaned by a worker that died mid-run.

    Nothing else in the system ever revisits a row once it leaves 'pending'
    for an in-flight status, so a crash strands it there permanently — and
    because in-flight rows count against DISPATCH_MAX_INFLIGHT, enough
    stranded rows silently deadlock the ENTIRE queue, not just themselves.
    Observed live: one killed worker orphaned 6 sources, which exactly
    exhausted DISPATCH_MAX_INFLIGHT=6 at the time; new, unrelated
    registrations stopped being admitted too, with no error anywhere.

    A row is stale once `RECONCILE_STALE_AFTER_S` passes with no update -
    set_status/set_progress touch updated_at on every real tick of a running
    flow, so that only happens once the worker that owned it is gone. Stale
    rows are reset to 'pending' (the fair dispatcher reclaims them normally)
    unless they have already been attempted `RECONCILE_MAX_ATTEMPTS` times, in
    which case they are marked 'failed' instead of retried forever.
    """
    stale = db.find_stale_inflight(config.RECONCILE_STALE_AFTER_S)
    for row in stale:
        outcome = _reconcile_decision(row["attempts"], config.RECONCILE_MAX_ATTEMPTS)
        reason = (f"reconciler: stuck in '{row['status']}' since "
                  f"{row['updated_at']} with no progress - likely a worker crash")
        if outcome == "failed":
            reason += f"; giving up after {row['attempts']} attempts"
        db.reconcile_row(row["id"], to_status=outcome, error=reason)
        print(f"[reconcile] {row['id']} was stuck in '{row['status']}' "
              f"(attempts={row['attempts']}) -> {outcome}")
    return len(stale)


def run_forever() -> None:
    print(f"[dispatch] fair scheduler on — max in-flight "
          f"{config.DISPATCH_MAX_INFLIGHT}, tick {config.DISPATCH_INTERVAL_S}s")
    while True:
        try:
            # Reconcile FIRST: freeing a stale row's capacity this tick means
            # dispatch_once() can use that slot in the SAME tick rather than
            # waiting one more.
            reconcile_once()
            dispatch_once()
        except Exception as exc:  # never let the scheduler thread die
            print(f"[dispatch] error: {type(exc).__name__}: {exc}")
        time.sleep(config.DISPATCH_INTERVAL_S)


def start_in_background() -> None:
    """Start the dispatcher as a daemon thread (no-op if fair dispatch is off)."""
    if not config.ENABLE_FAIR_DISPATCH:
        print("[dispatch] fair dispatch disabled — FIFO (immediate enqueue)")
        return
    threading.Thread(target=run_forever, daemon=True, name="dispatcher").start()
