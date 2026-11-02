from __future__ import annotations

from dataclasses import FrozenInstanceError
import math
import unittest

from motion_bridge.range_spec import (
    RangeSource,
    RangeSpec,
    centered_preview_range,
    resolve_range_spec,
)


class RangeSpecTests(unittest.TestCase):
    def test_precedence_is_selected_then_preview_then_whole_take(self) -> None:
        selected = resolve_range_spec(
            whole_take_range=(1.0, 100.0),
            selected_key_range=(20.0, 30.0),
            preview_range=(10.0, 40.0),
        )
        preview = resolve_range_spec(
            whole_take_range=(1.0, 100.0),
            preview_range=(10.0, 40.0),
        )
        whole = resolve_range_spec(whole_take_range=(1.0, 100.0))

        self.assertEqual(RangeSpec(20.0, 30.0, RangeSource.SELECTED_KEYS), selected)
        self.assertEqual(RangeSpec(10.0, 40.0, RangeSource.PREVIEW_RANGE), preview)
        self.assertEqual(RangeSpec(1.0, 100.0, RangeSource.WHOLE_TAKE), whole)

    def test_selected_range_ignores_lower_priority_invalid_preview(self) -> None:
        actual = resolve_range_spec(
            whole_take_range=(1.0, 100.0),
            selected_key_range=(20.0, 30.0),
            preview_range=(90.0, 10.0),
        )
        self.assertEqual(RangeSource.SELECTED_KEYS, actual.source)

    def test_invalid_preview_never_falls_back_to_whole_take(self) -> None:
        with self.assertRaisesRegex(ValueError, "プレビュー範囲の開始"):
            resolve_range_spec(
                whole_take_range=(1.0, 100.0),
                preview_range=(30.0, 20.0),
            )

    def test_candidate_must_be_inside_whole_take(self) -> None:
        with self.assertRaisesRegex(ValueError, "プレビュー範囲.*内側"):
            resolve_range_spec(
                whole_take_range=(1.0, 100.0),
                preview_range=(0.0, 10.0),
            )
        with self.assertRaisesRegex(ValueError, "選択キー.*範囲外"):
            resolve_range_spec(
                whole_take_range=(1.0, 100.0),
                selected_key_range=(90.0, 101.0),
            )

    def test_single_frame_is_allowed_unless_tool_requires_duration(self) -> None:
        actual = resolve_range_spec(
            whole_take_range=(1.0, 100.0),
            preview_range=(20.0, 20.0),
        )
        self.assertEqual(1, actual.integer_sample_count)
        with self.assertRaisesRegex(ValueError, "2フレーム"):
            resolve_range_spec(
                whole_take_range=(1.0, 100.0),
                preview_range=(20.0, 20.0),
                minimum_duration=1.0e-3,
            )

    def test_nonfinite_and_missing_whole_take_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "モーション全体"):
            resolve_range_spec(whole_take_range=None)
        with self.assertRaisesRegex(ValueError, "モーション全体"):
            resolve_range_spec(whole_take_range=(1.0, math.inf))

    def test_integer_bounds_expand_and_absorb_mapping_noise(self) -> None:
        spec = RangeSpec(
            1.0000001,
            9.9999999,
            RangeSource.PREVIEW_RANGE,
        )
        self.assertEqual((1, 10), spec.expanded_integer_bounds())
        self.assertEqual(10, spec.integer_sample_count)

    def test_centered_preview_clamps_at_take_edges(self) -> None:
        whole = RangeSpec(1.0, 100.0, RangeSource.WHOLE_TAKE)
        middle = centered_preview_range(whole, 50.0, maximum_samples=21)
        first = centered_preview_range(whole, 1.0, maximum_samples=21)
        last = centered_preview_range(whole, 100.0, maximum_samples=21)

        self.assertEqual((40, 60), middle.expanded_integer_bounds())
        self.assertEqual((1, 21), first.expanded_integer_bounds())
        self.assertEqual((80, 100), last.expanded_integer_bounds())
        self.assertTrue(
            all(value.source is RangeSource.PREVIEW_RANGE for value in (middle, first, last))
        )

    def test_centered_preview_rejects_current_frame_outside_take(self) -> None:
        whole = RangeSpec(1.0, 100.0, RangeSource.WHOLE_TAKE)
        with self.assertRaisesRegex(ValueError, "現在フレーム"):
            centered_preview_range(whole, 101.0, maximum_samples=20)

    def test_fractional_take_uses_one_integer_sampling_domain(self) -> None:
        whole = RangeSpec(1.5, 100.5, RangeSource.WHOLE_TAKE)
        first = centered_preview_range(whole, 1.0, maximum_samples=20)
        last = centered_preview_range(whole, 101.0, maximum_samples=20)

        self.assertEqual((1, 20), first.expanded_integer_bounds())
        self.assertEqual((82, 101), last.expanded_integer_bounds())
        self.assertEqual(
            first,
            resolve_range_spec(
                whole_take_range=(1.5, 100.5),
                preview_range=first.expanded_integer_bounds(),
            ),
        )
        self.assertEqual(
            last,
            resolve_range_spec(
                whole_take_range=(1.5, 100.5),
                preview_range=last.expanded_integer_bounds(),
            ),
        )

        with self.assertRaisesRegex(ValueError, "現在フレーム"):
            centered_preview_range(whole, 0.999, maximum_samples=20)

    def test_range_spec_is_immutable(self) -> None:
        spec = RangeSpec(1.0, 2.0, RangeSource.WHOLE_TAKE)
        with self.assertRaises((AttributeError, FrozenInstanceError)):
            spec.scene_end = 3.0  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
