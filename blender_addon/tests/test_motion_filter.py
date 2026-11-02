from __future__ import annotations

from dataclasses import FrozenInstanceError
import math
import unittest

from motion_bridge.motion_filter import (
    MAX_SMOOTH_FRAME_COUNT,
    MotionSmoothingResult,
    SmoothingChannels,
    SmoothingLimits,
    SmoothingPreset,
    SmoothingStrength,
    smooth_motion,
    smoothing_preset,
)
from motion_bridge.motion_quality import BoneSample, MotionFrame, MotionSequence
from motion_bridge.range_spec import RangeSource, RangeSpec


IDENTITY = (1.0, 0.0, 0.0, 0.0)


def _qz(degrees: float, sign: float = 1.0) -> tuple[float, float, float, float]:
    half_angle = math.radians(degrees) * 0.5
    return (
        sign * math.cos(half_angle),
        0.0,
        0.0,
        sign * math.sin(half_angle),
    )


def _quaternion_angle_degrees(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float] = IDENTITY,
) -> float:
    left_norm = math.hypot(*left)
    right_norm = math.hypot(*right)
    dot = abs(
        sum(a * b for a, b in zip(left, right, strict=True))
        / (left_norm * right_norm)
    )
    return math.degrees(2.0 * math.acos(max(-1.0, min(1.0, dot))))


def _bone(
    name: str,
    rotation: tuple[float, float, float, float] = IDENTITY,
    parent: str | None = None,
) -> BoneSample:
    return BoneSample(
        name=name,
        local_rotation_wxyz=rotation,
        global_rotation_wxyz=rotation,
        head_world=(0.0, 0.0, 0.0),
        tail_world=(0.0, 0.0, 0.2),
        parent=parent,
    )


def _sequence(
    root_x: tuple[float, ...],
    *,
    scene_frames: tuple[float, ...] | None = None,
    root_angles: tuple[float, ...] | None = None,
    hips_angles: tuple[float, ...] | None = None,
    spine_angles: tuple[float, ...] | None = None,
    fps: float = 10.0,
) -> MotionSequence:
    count = len(root_x)
    frames = tuple(float(index) for index in range(count)) if scene_frames is None else scene_frames
    roots = (0.0,) * count if root_angles is None else root_angles
    hips = (0.0,) * count if hips_angles is None else hips_angles
    spines = (0.0,) * count if spine_angles is None else spine_angles
    samples = tuple(
        MotionFrame(
            frame=scene_frame,
            time_seconds=scene_frame / fps,
            root_location=(root_x[index], 0.0, 0.0),
            root_rotation_wxyz=_qz(roots[index]),
            root_scale=(1.0, 1.0, 1.0),
            bones=(
                _bone("Hips", _qz(hips[index])),
                _bone("Spine", _qz(spines[index]), "Hips"),
            ),
        )
        for index, scene_frame in enumerate(frames)
    )
    return MotionSequence(
        fps=fps,
        start_frame=frames[0],
        end_frame=frames[-1],
        frames=samples,
        source_id="filter-test",
    )


class MotionSmoothingTests(unittest.TestCase):
    def test_three_presets_reduce_a_spike_monotonically(self) -> None:
        sequence = _sequence((0.0, 0.0, 0.0, 0.0, 10.0, 0.0, 0.0, 0.0, 0.0))

        centers = {
            strength: smooth_motion(sequence, strength=strength).targets[4].root_location[0]
            for strength in SmoothingStrength
        }

        self.assertGreater(centers[SmoothingStrength.LOW], centers[SmoothingStrength.STANDARD])
        self.assertGreater(
            centers[SmoothingStrength.STANDARD],
            centers[SmoothingStrength.STRONG],
        )
        self.assertGreater(centers[SmoothingStrength.STRONG], 0.0)
        self.assertLess(centers[SmoothingStrength.LOW], 10.0)
        self.assertEqual(3, len(tuple(SmoothingStrength)))

    def test_root_bones_and_hips_are_smoothed_with_fixed_endpoints(self) -> None:
        angles = (0.0, 0.0, 0.0, 100.0, 0.0, 0.0, 0.0)
        sequence = _sequence(
            (0.0, 0.0, 0.0, 10.0, 0.0, 0.0, 0.0),
            root_angles=angles,
            hips_angles=angles,
            spine_angles=angles,
        )
        hips_locations = (
            (0.0, 1.0, 2.0),
            (0.0, 1.0, 2.0),
            (0.0, 1.0, 2.0),
            (10.0, 1.0, 2.0),
            (0.0, 1.0, 2.0),
            (0.0, 1.0, 2.0),
            (0.0, 1.0, 2.0),
        )

        result = smooth_motion(sequence, hips_locations=hips_locations)
        center = result.targets[3]

        self.assertLess(center.root_location[0], 10.0)
        self.assertLess(center.hips_location[0], 10.0)
        self.assertEqual(1.0, center.hips_location[1])
        self.assertEqual(2.0, center.hips_location[2])
        self.assertLess(_quaternion_angle_degrees(center.root_rotation_wxyz), 100.0)
        self.assertEqual(("Hips", "Spine"), tuple(b.bone_name for b in center.bone_rotations))
        for bone in center.bone_rotations:
            self.assertLess(_quaternion_angle_degrees(bone.local_rotation_wxyz), 100.0)
            self.assertAlmostEqual(1.0, math.hypot(*bone.local_rotation_wxyz), places=12)
        self.assertEqual(sequence.frames[0].root_location, result.targets[0].root_location)
        self.assertEqual(sequence.frames[-1].root_location, result.targets[-1].root_location)
        self.assertEqual(hips_locations[0], result.targets[0].hips_location)
        self.assertEqual(hips_locations[-1], result.targets[-1].hips_location)

    def test_quaternion_signs_are_aligned_without_component_averaging(self) -> None:
        sequence = _sequence(
            (0.0,) * 5,
            root_angles=(45.0,) * 5,
            hips_angles=(45.0,) * 5,
        )
        signed_frames = []
        for index, frame in enumerate(sequence.frames):
            sign = -1.0 if index % 2 else 1.0
            root = tuple(sign * value for value in frame.root_rotation_wxyz)
            hips = tuple(sign * value for value in frame.bones[0].local_rotation_wxyz)
            signed_frames.append(
                MotionFrame(
                    frame=frame.frame,
                    time_seconds=frame.time_seconds,
                    root_location=frame.root_location,
                    root_rotation_wxyz=root,
                    root_scale=frame.root_scale,
                    bones=(
                        _bone("Hips", hips),
                        frame.bones[1],
                    ),
                )
            )
        signed = MotionSequence(
            fps=sequence.fps,
            start_frame=sequence.start_frame,
            end_frame=sequence.end_frame,
            frames=tuple(signed_frames),
        )

        result = smooth_motion(signed)

        roots = tuple(target.root_rotation_wxyz for target in result.targets)
        self.assertTrue(
            all(
                sum(a * b for a, b in zip(left, right, strict=True)) > 0.0
                for left, right in zip(roots, roots[1:], strict=False)
            )
        )
        self.assertTrue(all(math.isclose(math.hypot(*value), 1.0) for value in roots))
        self.assertTrue(
            all(math.isclose(_quaternion_angle_degrees(value), 45.0) for value in roots)
        )

    def test_range_is_isolated_and_its_own_endpoints_are_preserved(self) -> None:
        sequence = _sequence(tuple(float(index) for index in range(9)))
        requested = RangeSpec(2.0, 6.0, RangeSource.SELECTED_KEYS)

        result = smooth_motion(sequence, range_spec=requested)

        self.assertIs(requested, result.range_spec)
        self.assertEqual((2.0, 3.0, 4.0, 5.0, 6.0), tuple(t.scene_frame for t in result.targets))
        self.assertEqual(sequence.frames[2].root_location, result.targets[0].root_location)
        self.assertEqual(sequence.frames[6].root_location, result.targets[-1].root_location)
        self.assertEqual(5, result.frame_count)
        self.assertEqual(10, result.bone_sample_count)

    def test_irregular_sample_times_change_weights_not_sample_indices(self) -> None:
        values = (0.0, 0.0, 10.0, 0.0)
        uniform = _sequence(values, scene_frames=(0.0, 1.0, 2.0, 3.0))
        irregular = _sequence(values, scene_frames=(0.0, 1.0, 2.0, 10.0))

        uniform_center = smooth_motion(uniform).targets[2].root_location[0]
        irregular_center = smooth_motion(irregular).targets[2].root_location[0]

        self.assertGreater(irregular_center, uniform_center)
        self.assertLess(irregular_center, 10.0)

    def test_local_linear_filter_preserves_steady_motion_with_irregular_times(self) -> None:
        scene_frames = (0.0, 1.0, 2.0, 5.0, 6.0)
        locations = tuple(2.0 * value for value in scene_frames)
        angles = tuple(5.0 * value for value in scene_frames)
        sequence = _sequence(
            locations,
            scene_frames=scene_frames,
            root_angles=angles,
            hips_angles=angles,
            spine_angles=angles,
        )
        hips = tuple((value, -value, 1.0) for value in locations)

        result = smooth_motion(
            sequence,
            strength=SmoothingStrength.STRONG,
            hips_locations=hips,
        )

        for index, target in enumerate(result.targets):
            self.assertAlmostEqual(locations[index], target.root_location[0], places=11)
            self.assertAlmostEqual(hips[index][0], target.hips_location[0], places=11)
            self.assertAlmostEqual(hips[index][1], target.hips_location[1], places=11)
            self.assertAlmostEqual(
                angles[index],
                _quaternion_angle_degrees(target.root_rotation_wxyz),
                places=10,
            )
            for bone in target.bone_rotations:
                self.assertAlmostEqual(
                    angles[index],
                    _quaternion_angle_degrees(bone.local_rotation_wxyz),
                    places=10,
                )

    def test_one_and_two_sample_ranges_are_unchanged(self) -> None:
        sequence = _sequence((1.0, 4.0, 9.0))

        one = smooth_motion(
            sequence,
            range_spec=RangeSpec(1.0, 1.0, RangeSource.PREVIEW_RANGE),
        )
        two = smooth_motion(
            sequence,
            range_spec=RangeSpec(1.0, 2.0, RangeSource.PREVIEW_RANGE),
        )

        self.assertEqual((4.0,), tuple(target.root_location[0] for target in one.targets))
        self.assertEqual((4.0, 9.0), tuple(target.root_location[0] for target in two.targets))

    def test_channel_selection_emits_only_requested_values(self) -> None:
        sequence = _sequence((0.0, 0.0, 5.0, 0.0, 0.0))
        channels = SmoothingChannels(
            root_location=False,
            root_rotation=False,
            bone_names=("Spine",),
            hips_location=False,
        )

        result = smooth_motion(
            sequence,
            channels=channels,
            hips_locations=((0.0, 0.0, 0.0),) * len(sequence.frames),
        )

        for target in result.targets:
            self.assertIsNone(target.root_location)
            self.assertIsNone(target.root_rotation_wxyz)
            self.assertIsNone(target.hips_location)
            self.assertEqual(("Spine",), tuple(b.bone_name for b in target.bone_rotations))

    def test_output_is_deterministic_and_inputs_stay_unchanged(self) -> None:
        sequence = _sequence((0.0, 0.0, 3.0, 0.0, 0.0))
        hips = [(float(index), 0.0, 0.0) for index in range(5)]
        source_hash = sequence.content_hash()
        hips_snapshot = tuple(hips)

        first = smooth_motion(sequence, hips_locations=hips)
        second = smooth_motion(sequence, hips_locations=hips)

        self.assertEqual(first, second)
        self.assertEqual(source_hash, sequence.content_hash())
        self.assertEqual(hips_snapshot, tuple(hips))
        self.assertEqual(source_hash, first.source_hash)

    def test_ambiguous_half_turn_is_rejected(self) -> None:
        sequence = _sequence(
            (0.0, 0.0, 0.0),
            root_angles=(0.0, 180.0, 180.0),
        )

        with self.assertRaisesRegex(ValueError, "ambiguous 180-degree"):
            smooth_motion(sequence)

    def test_bad_range_bone_and_empty_channel_selection_are_rejected(self) -> None:
        sequence = _sequence((0.0, 1.0, 0.0))

        with self.assertRaisesRegex(ValueError, "inside"):
            smooth_motion(
                sequence,
                range_spec=RangeSpec(-1.0, 1.0, RangeSource.PREVIEW_RANGE),
            )
        with self.assertRaisesRegex(ValueError, "missing bones"):
            smooth_motion(sequence, channels=SmoothingChannels(bone_names=("Tail",)))
        with self.assertRaisesRegex(ValueError, "at least one"):
            smooth_motion(
                sequence,
                channels=SmoothingChannels(
                    root_location=False,
                    root_rotation=False,
                    bone_names=(),
                    hips_location=False,
                ),
            )

    def test_hips_input_shape_presence_finite_and_coordinate_limit(self) -> None:
        sequence = _sequence((0.0, 1.0, 0.0))
        without_hips = MotionSequence(
            fps=10.0,
            start_frame=0.0,
            end_frame=2.0,
            frames=tuple(
                MotionFrame(
                    frame=index,
                    time_seconds=index / 10.0,
                    root_location=(0.0, 0.0, 0.0),
                    root_rotation_wxyz=IDENTITY,
                    root_scale=(1.0, 1.0, 1.0),
                    bones=(_bone("Spine"),),
                )
                for index in range(3)
            ),
        )

        with self.assertRaisesRegex(ValueError, "one-to-one"):
            smooth_motion(sequence, hips_locations=((0.0, 0.0, 0.0),))
        with self.assertRaisesRegex(ValueError, "finite"):
            smooth_motion(
                sequence,
                hips_locations=((0.0, 0.0, 0.0), (math.nan, 0.0, 0.0), (0.0, 0.0, 0.0)),
            )
        with self.assertRaisesRegex(ValueError, "sampled Hips"):
            smooth_motion(
                without_hips,
                hips_locations=((0.0, 0.0, 0.0),) * 3,
            )
        with self.assertRaisesRegex(ValueError, "max_abs_coordinate"):
            smooth_motion(
                sequence,
                hips_locations=((0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
                limits=SmoothingLimits(max_abs_coordinate=1.0),
            )

    def test_resource_limits_cover_frames_channels_and_kernel_density(self) -> None:
        sequence = _sequence((0.0, 0.0, 1.0, 0.0))

        with self.assertRaisesRegex(ValueError, "max_frame_count"):
            smooth_motion(sequence, limits=SmoothingLimits(max_frame_count=3))
        with self.assertRaisesRegex(ValueError, "max_channel_samples"):
            smooth_motion(
                sequence,
                channels=SmoothingChannels(
                    root_location=True,
                    root_rotation=False,
                    bone_names=(),
                    hips_location=False,
                ),
                limits=SmoothingLimits(max_channel_samples=3),
            )
        with self.assertRaisesRegex(ValueError, "max_window_samples"):
            smooth_motion(sequence, limits=SmoothingLimits(max_window_samples=2))

    def test_resource_limits_apply_to_the_selected_range_and_bones(self) -> None:
        sequence = _sequence((0.0, 0.0, 1.0, 0.0, 0.0))
        selected = RangeSpec(1.0, 3.0, RangeSource.PREVIEW_RANGE)
        channels = SmoothingChannels(bone_names=("Spine",))

        result = smooth_motion(
            sequence,
            range_spec=selected,
            channels=channels,
            limits=SmoothingLimits(max_frame_count=3, max_bone_count=1),
        )

        self.assertEqual(3, result.frame_count)
        self.assertTrue(
            all(
                tuple(bone.bone_name for bone in target.bone_rotations) == ("Spine",)
                for target in result.targets
            )
        )

    def test_public_settings_validate_types_bounds_and_are_immutable(self) -> None:
        with self.assertRaises(TypeError):
            smooth_motion(_sequence((0.0,)), strength="standard")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            SmoothingChannels(root_location=1)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "duplicates"):
            SmoothingChannels(bone_names=("Hips", "Hips"))
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            SmoothingLimits(max_frame_count=MAX_SMOOTH_FRAME_COUNT + 1)
        with self.assertRaisesRegex(ValueError, "between zero and one"):
            SmoothingPreset(1.0, 2.0, 1.1)
        with self.assertRaisesRegex(ValueError, "finite"):
            SmoothingPreset(10**1000, 2.0, 0.5)

        limits = SmoothingLimits()
        with self.assertRaises((AttributeError, FrozenInstanceError)):
            limits.max_frame_count = 1  # type: ignore[misc]

    def test_presets_are_fixed_and_result_contract_is_typed(self) -> None:
        self.assertLess(
            smoothing_preset(SmoothingStrength.LOW).blend,
            smoothing_preset(SmoothingStrength.STRONG).blend,
        )
        result = smooth_motion(_sequence((0.0, 1.0, 0.0)))
        self.assertIsInstance(result, MotionSmoothingResult)
        self.assertIs(SmoothingStrength.STANDARD, result.strength)
        self.assertIs(RangeSource.WHOLE_TAKE, result.range_spec.source)


if __name__ == "__main__":
    unittest.main()
