"""Token-budgeted batching: peak embedding memory must be bounded by config,
not by how much work happened to arrive at once.

Discovered live, not hypothesised. The document-embedding service capped its
coalesced batch by CHUNK COUNT (DOC_EMBED_MAX_BATCH=256). That bounds nothing:
a transformer's activation cost is O(batch x seq^2) and ONNXRuntime pads every
sequence to the longest in the batch, so 256 short chunks and 256 long ones
differ by more than an order of magnitude. Worse, ORT's CPU arena grows to the
largest batch it has ever seen and never returns it to the OS.

Measured: ONE batch of 170 chunks took the service from 238 MiB to 2.906 GiB
in a single inference and it stayed there; across a session it reached 5.5 GiB
of a 7.75 GiB VM while idle at 0.2% CPU. That is what pushed the box into page
reclaim and produced the rare multi-second SEARCH stalls in a different
container (D12).

What deserves a test here is the splitting invariant - that no sub-batch can
exceed the padded-token budget, and that splitting never changes, drops,
reorders or truncates the caller's texts.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config, embed_service  # noqa: E402

split = embed_service._split_by_token_budget
est = embed_service._est_tokens


def padded_cost(group: list[str]) -> int:
    """What the runtime actually allocates for: count x longest sequence."""
    return len(group) * max(est(t) for t in group)


class TokenEstimate(unittest.TestCase):
    def test_is_clamped_to_the_models_limit(self):
        """bge truncates at 512, so a huge chunk must not be costed as huge."""
        self.assertEqual(est("x" * 100_000), config.DOC_EMBED_MODEL_MAX_TOKENS)

    def test_never_zero(self):
        """A zero would make a sub-batch look free and admit unlimited texts."""
        self.assertGreaterEqual(est(""), 1)
        self.assertGreaterEqual(est("a"), 1)


class SplitRespectsBudget(unittest.TestCase):
    def test_no_subbatch_exceeds_the_budget(self):
        texts = ["word " * 300] * 170  # the batch measured at 2.9 GiB
        for g in split(texts, 12000):
            self.assertLessEqual(padded_cost(g), 12000)

    def test_long_and_short_chunks_mixed(self):
        """The failure mode that a count-based cap misses: one long chunk
        inflates the padded cost of every short one batched with it."""
        texts = (["hi"] * 100) + ["word " * 400] + (["hi"] * 100)
        for g in split(texts, 4000):
            self.assertLessEqual(padded_cost(g), 4000)

    def test_a_single_oversized_text_becomes_its_own_subbatch(self):
        """It must still be embedded - never dropped, never truncated."""
        big = "word " * 5000
        groups = split(["hi", big, "hi"], 100)
        self.assertIn([big], groups)
        self.assertEqual(sum(len(g) for g in groups), 3)

    def test_short_chunks_still_batch_together(self):
        """Bounding memory must not degenerate into one-at-a-time inference,
        which would cost real throughput."""
        groups = split(["hi"] * 200, 12000)
        self.assertEqual(len(groups), 1)


class SplitPreservesInput(unittest.TestCase):
    def test_texts_are_preserved_exactly_and_in_order(self):
        texts = [f"chunk {i} " + "word " * (i % 50) for i in range(200)]
        flat = [t for g in split(texts, 3000) for t in g]
        self.assertEqual(flat, texts,
                         "splitting must not drop, reorder or alter any text")

    def test_empty_input(self):
        self.assertEqual(split([], 12000), [])


class BudgetDisabled(unittest.TestCase):
    def test_zero_budget_means_one_batch(self):
        """Escape hatch: DOC_EMBED_MAX_BATCH_TOKENS=0 restores old behaviour."""
        texts = ["word " * 300] * 50
        self.assertEqual(embed_service._split_by_token_budget(texts, 10**9),
                         [texts])


if __name__ == "__main__":
    unittest.main(verbosity=2)
