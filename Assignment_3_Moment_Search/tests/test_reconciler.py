"""Crash recovery: a source orphaned by a dead worker must not stay stranded.

Discovered live, not hypothesised: a resilience test killed the worker
mid-embed, and after a clean restart, sources stuck in 'embedding' never
recovered. Worse - because in-flight rows count against
DISPATCH_MAX_INFLIGHT, 6 orphaned rows exactly exhausted a capacity of 6 and
silently deadlocked the ENTIRE queue: new, unrelated registrations stopped
being admitted too, with the API still returning 202 and no error anywhere.

These tests drive `dispatcher.reconcile_once` and `_reconcile_decision` with
`db` mocked out, same pattern as `test_admin_contract.DispatchRouting` - the
SQL plumbing (db.find_stale_inflight, db.reconcile_row) is thin enough to be
exercised live against Postgres, same as the rest of db.py; what deserves a
unit test is the POLICY (retry vs give up) and the orchestration.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import dispatcher  # noqa: E402


class ReconcileDecision(unittest.TestCase):
    """Pure policy: retry, or give up."""

    def test_under_the_cap_gets_reset_to_pending(self):
        self.assertEqual(dispatcher._reconcile_decision(0, max_attempts=5), "pending")
        self.assertEqual(dispatcher._reconcile_decision(4, max_attempts=5), "pending")

    def test_at_or_over_the_cap_is_given_up_on(self):
        self.assertEqual(dispatcher._reconcile_decision(5, max_attempts=5), "failed")
        self.assertEqual(dispatcher._reconcile_decision(9, max_attempts=5), "failed")


class ReconcileOnce(unittest.TestCase):
    """Orchestration: every stale row gets a decision applied, correctly."""

    def _run(self, stale_rows, max_attempts=5):
        applied = []
        with mock.patch.object(dispatcher.db, "find_stale_inflight",
                               lambda s: stale_rows), \
             mock.patch.object(dispatcher.db, "reconcile_row",
                               lambda rid, to_status, error:
                               applied.append((rid, to_status, error))), \
             mock.patch.object(dispatcher.config, "RECONCILE_MAX_ATTEMPTS", max_attempts):
            n = dispatcher.reconcile_once()
        return n, applied

    def test_a_freshly_stranded_row_is_reset_to_pending(self):
        rows = [{"id": "pa_1", "user_id": "u1", "status": "embedding",
                 "attempts": 1, "updated_at": "2026-01-01T00:00:00Z"}]
        n, applied = self._run(rows)
        self.assertEqual(n, 1)
        self.assertEqual(len(applied), 1)
        rid, to_status, error = applied[0]
        self.assertEqual(rid, "pa_1")
        self.assertEqual(to_status, "pending")
        self.assertIn("embedding", error)
        self.assertIn("worker crash", error)

    def test_a_row_that_keeps_coming_back_is_marked_failed_not_retried_forever(self):
        rows = [{"id": "pa_2", "user_id": "u1", "status": "chunking",
                 "attempts": 5, "updated_at": "2026-01-01T00:00:00Z"}]
        n, applied = self._run(rows, max_attempts=5)
        self.assertEqual(n, 1)
        _, to_status, error = applied[0]
        self.assertEqual(to_status, "failed")
        self.assertIn("giving up after 5 attempts", error)

    def test_every_stale_row_is_processed_independently(self):
        """One row's outcome must not affect another's."""
        rows = [
            {"id": "a", "user_id": "u1", "status": "fetching",
             "attempts": 1, "updated_at": "t"},
            {"id": "b", "user_id": "u1", "status": "embedding",
             "attempts": 5, "updated_at": "t"},
            {"id": "c", "user_id": "u2", "status": "queued",
             "attempts": 0, "updated_at": "t"},
        ]
        n, applied = self._run(rows, max_attempts=5)
        self.assertEqual(n, 3)
        outcomes = {rid: status for rid, status, _ in applied}
        self.assertEqual(outcomes, {"a": "pending", "b": "failed", "c": "pending"})

    def test_no_stale_rows_is_a_clean_no_op(self):
        n, applied = self._run([])
        self.assertEqual(n, 0)
        self.assertEqual(applied, [])

    def test_reconcile_runs_before_dispatch_in_the_same_tick(self):
        """Freeing a stale slot must be visible to dispatch in the SAME tick,
        not one tick later - otherwise a recovered row waits a full
        DISPATCH_INTERVAL_S for no reason."""
        order = []
        with mock.patch.object(dispatcher, "reconcile_once",
                               lambda: order.append("reconcile") or 0), \
             mock.patch.object(dispatcher, "dispatch_once",
                               lambda: order.append("dispatch") or 0), \
             mock.patch.object(dispatcher.time, "sleep", side_effect=KeyboardInterrupt):
            try:
                dispatcher.run_forever()
            except KeyboardInterrupt:
                pass
        self.assertEqual(order, ["reconcile", "dispatch"])


class DeadlockScenario(unittest.TestCase):
    """The exact failure mode observed live: orphaned rows exhaust capacity
    and dispatch_once() alone can never recover - reconcile_once() must run
    first to free the slots."""

    def test_dispatch_alone_cannot_recover_from_a_full_deadlock(self):
        with mock.patch.object(dispatcher.db, "count_inflight", lambda: 6), \
             mock.patch.object(dispatcher.config, "DISPATCH_MAX_INFLIGHT", 6), \
             mock.patch.object(dispatcher.db, "wfq_claim") as claim:
            n = dispatcher.dispatch_once()
        claim.assert_not_called()
        self.assertEqual(n, 0)

    def test_reconciling_the_orphans_frees_the_deadlock_for_dispatch(self):
        stale = [{"id": f"pa_{i}", "user_id": "u1", "status": "embedding",
                  "attempts": 1, "updated_at": "t"} for i in range(6)]
        inflight = [6]  # mutable: reconcile "fixing" rows lowers this

        def fake_reconcile_row(rid, to_status, error):
            inflight[0] -= 1  # each reset row leaves the in-flight count

        with mock.patch.object(dispatcher.db, "find_stale_inflight", lambda s: stale), \
             mock.patch.object(dispatcher.db, "reconcile_row", fake_reconcile_row):
            dispatcher.reconcile_once()
        self.assertEqual(inflight[0], 0, "all 6 orphans should have been reconciled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
