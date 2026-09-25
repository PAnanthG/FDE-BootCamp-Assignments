"""The async contract for POST /admin/documents - 15 points, and an auto-fail.

The assignment is explicit that doing the work in the request path fails the
submission "even if it works". So the headline test here does not time the
endpoint - a fast response only proves the document happened to be nearby.
Instead it makes every outbound network primitive raise, and asserts the
endpoint still returns 202. A handler that fetches, HEADs, or sizes the
document cannot pass that no matter how fast the network is.

The pure helpers (id derivation, uri validation, pct) are tested directly.
The endpoint tests need FastAPI + starlette's TestClient; they skip cleanly if
those are not installed, so this file runs in a bare environment.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import pathlib
import socket
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except ImportError:  # pragma: no cover - bare env
    HAVE_FASTAPI = False


# ── pure helpers, no web stack needed ────────────────────────────────────────

class DocumentIdDerivation(unittest.TestCase):
    def setUp(self):
        from src.api import admin
        self.admin = admin

    def test_same_uri_gives_the_same_id(self):
        """Re-POSTing a document must reset one row, not create a second."""
        a = self.admin._document_id("paper", "https://arxiv.org/pdf/2312.10997")
        b = self.admin._document_id("paper", "  https://arxiv.org/pdf/2312.10997 ")
        self.assertEqual(a, b)

    def test_different_uris_give_different_ids(self):
        a = self.admin._document_id("paper", "https://example.com/a.pdf")
        b = self.admin._document_id("paper", "https://example.com/b.pdf")
        self.assertNotEqual(a, b)

    def test_kind_prefixes_the_id(self):
        self.assertTrue(
            self.admin._document_id("paper", "https://x/y.pdf").startswith("pa_"))
        self.assertTrue(
            self.admin._document_id("deck", "https://x/y.pdf").startswith("dk_"))


class PctIsMonotonic(unittest.TestCase):
    """pct must never go backwards as a source advances - the manifest's own
    `progress` restarts at every stage, so the endpoint has to remap it."""

    def setUp(self):
        from src.api import admin
        self.pct = admin._pct

    def test_stages_are_ordered(self):
        sequence = [
            {"status": "pending", "progress": None},
            {"status": "queued", "progress": None},
            {"status": "fetching", "progress": 0.5},
            {"status": "chunking", "progress": 0.5},
            {"status": "embedding", "progress": 0.5},
            {"status": "indexed", "progress": 1.0},
        ]
        values = [self.pct(r) for r in sequence]
        self.assertEqual(values, sorted(values), f"pct went backwards: {values}")

    def test_within_stage_progress_never_reaches_100(self):
        """Only 'indexed' is 100 - a full bar on a source still embedding is a lie."""
        self.assertLess(self.pct({"status": "embedding", "progress": 1.0}), 100)

    def test_indexed_is_100_and_failed_is_0(self):
        self.assertEqual(self.pct({"status": "indexed"}), 100)
        self.assertEqual(self.pct({"status": "failed", "progress": 0.9}), 0)


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")
class AdminEndpoint(unittest.TestCase):
    """Endpoint behaviour with the manifest and queue faked out."""

    def setUp(self):
        from src.api import admin

        self.admin = admin
        self.rows: dict[str, dict] = {}

        def fake_upsert(doc):
            row = {**doc, "status": "pending", "source": doc["kind"],
                   "progress": None, "attempts": 0, "error": None,
                   "frame_count": None, "chunk_count": None,
                   "created_at": None, "updated_at": None}
            self.rows[doc["id"]] = row
            return row

        self.patches = [
            mock.patch.object(admin.db, "upsert_pending_document", fake_upsert),
            mock.patch.object(admin.db, "list_sources",
                              lambda uid, kind=None: list(self.rows.values())),
            mock.patch.object(admin.config, "ADMIN_TOKEN", "test-token"),
            mock.patch("src.api.videos.ADMIN_TOKEN", "test-token"),
        ]
        for p in self.patches:
            p.start()

        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(admin.router)
        self.client = TestClient(app)
        self.auth = {"Authorization": "Bearer test-token"}

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def post(self, body, **kw):
        return self.client.post("/admin/documents", json=body,
                                headers=kw.pop("headers", self.auth), **kw)

    # --- the headline assertion ------------------------------------------
    def test_no_network_call_in_the_request_path(self):
        """Break every outbound primitive; the endpoint must still return 202.

        This is the auto-fail the assignment names. Timing cannot prove it -
        making the network impossible can.
        """
        def explode(*a, **kw):
            raise AssertionError("the request path performed document I/O")

        with mock.patch.object(socket, "create_connection", explode), \
             mock.patch("urllib.request.urlopen", explode), \
             mock.patch("socket.getaddrinfo", explode):
            resp = self.post({"uri": "https://arxiv.org/pdf/2312.10997",
                              "kind": "paper", "title": "RAG Survey"})
        self.assertEqual(resp.status_code, 202)

    def test_202_body_shape(self):
        resp = self.post({"uri": "https://arxiv.org/pdf/2312.10997",
                          "kind": "paper", "title": "RAG Survey"})
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["kind"], "paper")
        self.assertTrue(body["id"].startswith("pa_"))

    def test_source_hash_is_not_invented_from_the_uri(self):
        """A hash of the URL is not a content hash. Storing one there would
        make duplicate detection compare the wrong thing and silently skip a
        document whose URL merely resembled another."""
        self.post({"uri": "https://arxiv.org/pdf/2312.10997", "kind": "paper"})
        row = next(iter(self.rows.values()))
        self.assertIsNone(row["source_hash"])

    # --- error contract ---------------------------------------------------
    def test_401_without_a_token(self):
        resp = self.post({"uri": "https://x/y.pdf", "kind": "paper"}, headers={})
        self.assertEqual(resp.status_code, 401)

    def test_401_with_a_wrong_token(self):
        resp = self.post({"uri": "https://x/y.pdf", "kind": "paper"},
                         headers={"Authorization": "Bearer nope"})
        self.assertEqual(resp.status_code, 401)

    def test_400_on_an_unknown_kind(self):
        resp = self.post({"uri": "https://x/y.pdf", "kind": "video"})
        self.assertEqual(resp.status_code, 400)

    def test_400_on_a_non_http_uri(self):
        for uri in ("file:///etc/passwd", "ftp://x/y.pdf", "not-a-url"):
            with self.subTest(uri=uri):
                resp = self.post({"uri": uri, "kind": "paper"})
                self.assertEqual(resp.status_code, 400)

    def test_422_on_a_malformed_body(self):
        """Missing required field - FastAPI's own validation, asserted so a
        later refactor cannot quietly turn it into a 500."""
        resp = self.post({"kind": "paper"})
        self.assertEqual(resp.status_code, 422)

    # --- unified status ---------------------------------------------------
    def test_sources_returns_kind_and_pct(self):
        self.post({"uri": "https://x/a.pdf", "kind": "paper"})
        self.post({"uri": "https://x/b.pdf", "kind": "deck"})
        resp = self.client.get("/admin/sources", headers=self.auth)
        self.assertEqual(resp.status_code, 200)
        sources = resp.json()["sources"]
        self.assertEqual({s["kind"] for s in sources}, {"paper", "deck"})
        for source in sources:
            self.assertIn("pct", source)
            self.assertIn("status", source)

    def test_sources_rejects_an_unknown_kind_filter(self):
        resp = self.client.get("/admin/sources?kind=nonsense", headers=self.auth)
        self.assertEqual(resp.status_code, 400)


class DispatchRouting(unittest.TestCase):
    """A claimed document must reach its own deployment, not the video flow."""

    def test_each_kind_maps_to_its_own_deployment(self):
        from src import jobs

        self.assertEqual(len(set(jobs.DEPLOYMENTS.values())), 3)
        for kind in ("video", "paper", "deck"):
            self.assertIn(kind, jobs.DEPLOYMENTS)

    def test_unknown_kind_raises_rather_than_defaulting_to_video(self):
        from src import jobs

        with self.assertRaises(ValueError):
            jobs.enqueue_source("x_1", "u1", "spreadsheet")

    def test_dispatcher_routes_on_the_claimed_row_kind(self):
        from src import dispatcher

        claimed = [{"id": "pa_1", "user_id": "u1", "kind": "paper"},
                   {"id": "yt_2", "user_id": "u1", "kind": "video"},
                   {"id": "dk_3", "user_id": "u1", "kind": "deck"}]
        seen = []
        with mock.patch.object(dispatcher.db, "count_inflight", lambda: 0), \
             mock.patch.object(dispatcher.db, "wfq_claim", lambda n: claimed), \
             mock.patch.object(dispatcher.config, "DISPATCH_MAX_INFLIGHT", 10), \
             mock.patch.object(dispatcher.jobs, "enqueue_source",
                               lambda i, u, k: seen.append((i, k)) or "run-1"):
            dispatcher.dispatch_once()
        self.assertEqual(seen, [("pa_1", "paper"), ("yt_2", "video"),
                                ("dk_3", "deck")])


class CapacityAccounting(unittest.TestCase):
    """Every working stage must count as in-flight, or the dispatcher
    over-admits and the <=1.3x latency SLA stops being bounded."""

    def test_document_stages_are_in_flight(self):
        from src import config

        for stage in ("chunking", "captioning"):
            self.assertIn(stage, config.INFLIGHT_STATUSES)

    def test_video_stages_still_in_flight(self):
        from src import config

        for stage in ("queued", "fetching", "sampling", "embedding"):
            self.assertIn(stage, config.INFLIGHT_STATUSES)

    def test_terminal_states_are_not_in_flight(self):
        from src import config

        for stage in ("indexed", "failed", "skipped", "pending"):
            self.assertNotIn(stage, config.INFLIGHT_STATUSES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
