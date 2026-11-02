from __future__ import annotations

import unittest

from motion_bridge.performance.budget import TakeBudgetTier, classify_take_size


class TakeBudgetTests(unittest.TestCase):
    def test_thresholds_match_dense_take_policy(self) -> None:
        cases = (
            (0, TakeBudgetTier.NATIVE),
            (499_999, TakeBudgetTier.NATIVE),
            (500_000, TakeBudgetTier.HEAVY),
            (2_000_000, TakeBudgetTier.HEAVY),
            (2_000_001, TakeBudgetTier.SPLIT_RECOMMENDED),
        )
        for points, expected in cases:
            with self.subTest(points=points):
                self.assertEqual(classify_take_size(points), expected)

    def test_invalid_counts_are_rejected(self) -> None:
        for value in (-1, 1.5, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    classify_take_size(value)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
