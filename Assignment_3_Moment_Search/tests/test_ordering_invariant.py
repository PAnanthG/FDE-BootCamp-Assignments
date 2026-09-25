"""The no-loss ordering invariant: upsert -> verify -> THEN commit status.

Worth 15 points, and the failure it guards against is silent: a source whose
row says 'indexed' while its chunks are not in the index is never retried by
anything, because every retry path keys off the status. It just stops existing.

These tests drive `docindex.embed_and_index` - the same function both Prefect
flows call - with fakes for the index and the manifest, so the orderings that
matter can actually be provoked. Against a live Qdrant a short write is very
hard to arrange on purpose, which is exactly why it goes untested and ships.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.ingest import docindex, docparse  # noqa: E402

CHUNKS = [
    {"text": "The survey says hybrid retrieval outperforms either branch.",
     "locator": 3, "idx": 0},
    {"text": "Recall at ten is the headline metric across the benchmark.",
     "locator": 4, "idx": 1},
]


class FakeStore:
    """An index whose write behaviour can be bent to reproduce real failures."""

    def __init__(self, *, drop_writes=0, raise_on_upsert=False):
        self.points: dict[str, dict] = {}
        self.drop_writes = drop_writes      # simulate a partially-landed write
        self.raise_on_upsert = raise_on_upsert
        self.calls: list[str] = []

    def ensure_text_collection(self):
        self.calls.append("ensure")

    def delete_video(self, user_id, doc_id):
        self.calls.append("delete")
        for pid in [p for p, v in self.points.items() if v["video_id"] == doc_id]:
            del self.points[pid]

    def upsert_document_chunks(self, doc_id, vectors, payloads):
        self.calls.append("upsert")
        if self.raise_on_upsert:
            raise ConnectionError("qdrant unreachable")
        keep = payloads[:len(payloads) - self.drop_writes] if self.drop_writes else payloads
        for payload in keep:
            self.points[docparse.chunk_point_id(doc_id, payload["idx"])] = dict(payload)

    def count_document_points(self, user_id, doc_id):
        self.calls.append("count")
        return sum(1 for v in self.points.values() if v["video_id"] == doc_id)


class FakeManifest:
    def __init__(self):
        self.status_history: list[tuple[str, dict]] = []
        self.progress: list[float] = []

    def set_status(self, doc_id, status, **kw):
        self.status_history.append((status, kw))

    def set_progress(self, doc_id, progress):
        self.progress.append(progress)

    @property
    def statuses(self):
        return [s for s, _ in self.status_history]


def fake_embed(texts):
    return [[0.1, 0.2, 0.3] for _ in texts]


def run(store, manifest, chunks=CHUNKS, kind="paper"):
    return docindex.embed_and_index(
        "pa_x", "u1", chunks, kind,
        store=store, embed=fake_embed,
        set_status=manifest.set_status, set_progress=manifest.set_progress,
        embed_version="test-v1",
    )


class HappyPath(unittest.TestCase):
    def test_commits_indexed_after_a_verified_write(self):
        store, manifest = FakeStore(), FakeManifest()
        self.assertEqual(run(store, manifest), 2)
        self.assertEqual(manifest.statuses, ["embedding", "indexed"])

    def test_verify_happens_before_the_status_commit(self):
        """The ordering itself, not just the outcome."""
        store, manifest = FakeStore(), FakeManifest()

        order: list[str] = []
        real_count = store.count_document_points
        store.count_document_points = lambda u, d: (order.append("count"),
                                                    real_count(u, d))[1]
        real_status = manifest.set_status
        manifest.set_status = lambda d, s, **kw: (order.append(f"status:{s}"),
                                                  real_status(d, s, **kw))[1]
        run(store, manifest)
        self.assertEqual(order, ["status:embedding", "count", "status:indexed"])

    def test_reports_the_verified_count_not_the_intended_one(self):
        store, manifest = FakeStore(), FakeManifest()
        run(store, manifest)
        _, kwargs = manifest.status_history[-1]
        self.assertEqual(kwargs["chunk_count"], 2)


class PartialWrite(unittest.TestCase):
    """The write returns, but the index is holding less than we sent."""

    def setUp(self):
        self.store = FakeStore(drop_writes=1)
        self.manifest = FakeManifest()

    def test_raises_instead_of_committing(self):
        with self.assertRaises(docindex.VerificationError):
            run(self.store, self.manifest)

    def test_never_marks_indexed(self):
        with self.assertRaises(docindex.VerificationError):
            run(self.store, self.manifest)
        self.assertNotIn("indexed", self.manifest.statuses,
                         "marked indexed on a write that did not fully land")

    def test_leaves_the_row_in_embedding_so_a_retry_picks_it_up(self):
        with self.assertRaises(docindex.VerificationError):
            run(self.store, self.manifest)
        self.assertEqual(self.manifest.statuses, ["embedding"])

    def test_error_names_both_counts(self):
        with self.assertRaises(docindex.VerificationError) as ctx:
            run(self.store, self.manifest)
        self.assertIn("2", str(ctx.exception))
        self.assertIn("1", str(ctx.exception))


class UpsertFails(unittest.TestCase):
    def test_connection_error_never_reaches_indexed(self):
        store, manifest = FakeStore(raise_on_upsert=True), FakeManifest()
        with self.assertRaises(ConnectionError):
            run(store, manifest)
        self.assertNotIn("indexed", manifest.statuses)
        self.assertNotIn("count", store.calls)


class Idempotency(unittest.TestCase):
    """A redelivered run must converge, not duplicate - Stage 8 leans on this."""

    def test_second_run_yields_the_same_point_count(self):
        store, manifest = FakeStore(), FakeManifest()
        first = run(store, manifest)
        second = run(store, manifest)
        self.assertEqual(first, second)
        self.assertEqual(len(store.points), 2)

    def test_reingesting_a_shorter_document_leaves_no_orphans(self):
        """Delete-before-upsert is what makes this true: without it, chunk 1
        from the first run would linger and be cited from a page the new
        version of the document no longer has."""
        store, manifest = FakeStore(), FakeManifest()
        run(store, manifest)
        run(store, manifest, chunks=CHUNKS[:1])
        self.assertEqual(len(store.points), 1)
        self.assertEqual({p["loc"] for p in store.points.values()}, {3})


class Guards(unittest.TestCase):
    def test_zero_chunks_is_refused_rather_than_indexed_empty(self):
        store, manifest = FakeStore(), FakeManifest()
        with self.assertRaises(ValueError):
            run(store, manifest, chunks=[])
        self.assertEqual(manifest.statuses, [])

    def test_deck_kind_writes_slide_locators(self):
        store, manifest = FakeStore(), FakeManifest()
        run(store, manifest, kind="deck")
        for payload in store.points.values():
            self.assertIn("slide", payload)
            self.assertNotIn("page", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
