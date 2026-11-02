from __future__ import annotations

from dataclasses import FrozenInstanceError
import math
import unittest

from motion_bridge.foot_lock import (
    FOOT_NAMES,
    FootLockContactInterval,
    FootLockSettings,
    detect_foot_lock_intervals,
    solve_foot_lock,
)
from motion_bridge.motion_quality import BoneSample, EffectorSample, MotionFrame, MotionSequence


IDENTITY = (1.0, 0.0, 0.0, 0.0)


def _qz(degrees: float) -> tuple[float, float, float, float]:
    half = math.radians(degrees) * 0.5
    return math.cos(half), 0.0, 0.0, math.sin(half)


def _bone(
    name: str,
    head: tuple[float, float, float],
    tail: tuple[float, float, float],
    parent: str | None,
    *,
    sign: float = 1.0,
) -> BoneSample:
    rotation = tuple(sign * value for value in IDENTITY)
    return BoneSample(
        name=name,
        local_rotation_wxyz=rotation,
        global_rotation_wxyz=rotation,
        head_world=head,
        tail_world=tail,
        parent=parent,
    )


def _frame(
    frame: int,
    *,
    left_slide: float = 0.0,
    right_slide: float = 0.0,
    left_effector: tuple[float, float, float] | None = None,
    right_effector: tuple[float, float, float] | None = None,
    quaternion_sign: float = 1.0,
    root_scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> MotionFrame:
    bones: list[BoneSample] = [
        _bone("Hips", (0.0, 0.0, 1.0), (0.0, 0.0, 1.15), None, sign=quaternion_sign)
    ]
    effectors: list[EffectorSample] = []
    for prefix, side_x, slide, override in (
        ("Left", 0.10, left_slide, left_effector),
        ("Right", -0.10, right_slide, right_effector),
    ):
        hip = (side_x, 0.0, 1.0)
        knee = (side_x + slide * 0.5, 0.12, 0.55)
        ankle = (side_x + slide, 0.0, 0.10)
        toe = (side_x + slide, -0.18, 0.04)
        upper_name = f"{prefix}UpperLeg"
        lower_name = f"{prefix}LowerLeg"
        foot_name = f"{prefix}Foot"
        bones.extend(
            (
                _bone(upper_name, hip, knee, "Hips", sign=quaternion_sign),
                _bone(lower_name, knee, ankle, upper_name, sign=quaternion_sign),
                _bone(foot_name, ankle, toe, lower_name, sign=quaternion_sign),
            )
        )
        effectors.append(EffectorSample(foot_name, toe if override is None else override))
    return MotionFrame(
        frame=float(frame),
        time_seconds=(frame - 1) / 30.0,
        root_location=(0.0, 0.0, 0.0),
        root_rotation_wxyz=IDENTITY,
        root_scale=root_scale,
        bones=tuple(bones),
        effectors=tuple(effectors),
    )


def _sequence(*frames: MotionFrame) -> MotionSequence:
    return MotionSequence(
        fps=30.0,
        start_frame=frames[0].frame,
        end_frame=frames[-1].frame,
        frames=frames,
    )


def _interval(
    foot_name: str,
    start: int,
    end: int,
    anchor: tuple[float, float, float],
) -> FootLockContactInterval:
    return FootLockContactInterval(
        foot_name=foot_name,
        frame_start=start,
        frame_end=end,
        anchor_world=anchor,
        sample_count=end - start + 1,
    )


def _qmul(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    lw, lx, ly, lz = left
    rw, rx, ry, rz = right
    return (
        lw * rw - lx * rx - ly * ry - lz * rz,
        lw * rx + lx * rw + ly * rz - lz * ry,
        lw * ry - lx * rz + ly * rw + lz * rx,
        lw * rz + lx * ry - ly * rx + lz * rw,
    )


def _qinv(
    value: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    w, x, y, z = value
    norm_squared = sum(component * component for component in value)
    return (w / norm_squared, -x / norm_squared, -y / norm_squared, -z / norm_squared)


def _qrotate(
    rotation: tuple[float, float, float, float],
    value: tuple[float, float, float],
) -> tuple[float, float, float]:
    pure = (0.0, *value)
    rotated = _qmul(_qmul(rotation, pure), _qinv(rotation))
    return rotated[1], rotated[2], rotated[3]


def _add(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> tuple[float, float, float]:
    return tuple(a + b for a, b in zip(left, right, strict=True))  # type: ignore[return-value]


def _sub(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> tuple[float, float, float]:
    return tuple(a - b for a, b in zip(left, right, strict=True))  # type: ignore[return-value]


def _reconstructed_effector(
    frame: MotionFrame,
    target_bones: dict[str, tuple[float, float, float, float]],
    prefix: str,
) -> tuple[float, float, float]:
    bones = {bone.name: bone for bone in frame.bones}
    parent = bones["Hips"]
    upper = bones[f"{prefix}UpperLeg"]
    lower = bones[f"{prefix}LowerLeg"]
    foot = bones[f"{prefix}Foot"]

    def solved_global(
        old_parent_global: tuple[float, float, float, float],
        new_parent_global: tuple[float, float, float, float],
        bone: BoneSample,
    ) -> tuple[float, float, float, float]:
        rest = _qmul(
            _qmul(_qinv(old_parent_global), bone.global_rotation_wxyz),
            _qinv(bone.local_rotation_wxyz),
        )
        return _qmul(_qmul(new_parent_global, rest), target_bones[bone.name])

    upper_global = solved_global(
        parent.global_rotation_wxyz,
        parent.global_rotation_wxyz,
        upper,
    )
    lower_global = solved_global(upper.global_rotation_wxyz, upper_global, lower)
    foot_global = solved_global(lower.global_rotation_wxyz, lower_global, foot)
    upper_delta = _qmul(upper_global, _qinv(upper.global_rotation_wxyz))
    lower_delta = _qmul(lower_global, _qinv(lower.global_rotation_wxyz))
    foot_delta = _qmul(foot_global, _qinv(foot.global_rotation_wxyz))
    knee = _add(upper.head_world, _qrotate(upper_delta, _sub(upper.tail_world, upper.head_world)))
    ankle = _add(knee, _qrotate(lower_delta, _sub(lower.tail_world, lower.head_world)))
    return _add(
        ankle,
        _qrotate(foot_delta, _sub(_effector(frame, foot.name), foot.head_world)),
    )


def _effector(frame: MotionFrame, name: str) -> tuple[float, float, float]:
    return next(value.position_world for value in frame.effectors if value.name == name)


class FootLockContactTests(unittest.TestCase):
    def test_foot_far_below_ground_is_not_locked(self) -> None:
        frames = tuple(
            _frame(
                frame,
                left_effector=(0.1, -0.18, -0.50),
                right_effector=(-0.1, -0.18, 1.0),
            )
            for frame in range(1, 4)
        )

        intervals = detect_foot_lock_intervals(_sequence(*frames))

        self.assertEqual((), intervals)

    def test_detects_both_feet_with_stable_world_anchors(self) -> None:
        sequence = _sequence(
            _frame(1, left_slide=-0.002, right_slide=0.001),
            _frame(2, left_slide=0.000, right_slide=0.000),
            _frame(3, left_slide=0.002, right_slide=-0.001),
        )

        intervals = detect_foot_lock_intervals(sequence)

        self.assertEqual(FOOT_NAMES, tuple(value.foot_name for value in intervals))
        self.assertTrue(all((value.start, value.end) == (1.0, 3.0) for value in intervals))
        self.assertAlmostEqual(0.10, intervals[0].anchor[0])
        self.assertAlmostEqual(-0.10, intervals[1].anchor[0])
        self.assertTrue(all(value.peak_speed <= 0.15 for value in intervals))

    def test_contact_gap_join_and_swing_boundary_match_existing_detector(self) -> None:
        heights = (1.0, 0.04, 0.04, 0.04, 0.50, 0.04, 0.04, 0.04, 1.0)
        frames = tuple(
            _frame(
                index,
                left_effector=(0.1, -0.18, height),
                right_effector=(-0.1, -0.18, 1.0),
            )
            for index, height in enumerate(heights, start=1)
        )
        settings = FootLockSettings(min_contact_frames=3, join_gap_frames=1)

        intervals = detect_foot_lock_intervals(_sequence(*frames), settings)

        self.assertEqual(1, len(intervals))
        self.assertEqual("LeftFoot", intervals[0].foot_name)
        self.assertEqual((2.0, 8.0), (intervals[0].start, intervals[0].end))
        self.assertAlmostEqual(0.74 / 7.0, intervals[0].anchor[2])
        self.assertAlmostEqual(0.50, intervals[0].peak_ground_distance)

    def test_fast_low_foot_is_not_contact(self) -> None:
        frames = tuple(
            _frame(
                index,
                left_effector=(index * 0.1, 0.0, 0.04),
                right_effector=(-0.1, 0.0, 1.0),
            )
            for index in range(1, 6)
        )
        self.assertEqual((), detect_foot_lock_intervals(_sequence(*frames)))

    def test_existing_contact_interval_adapts_structurally(self) -> None:
        class ExistingContact:
            frame_start = 2
            frame_end = 4
            anchor = (1.0, 2.0, 3.0)
            peak_speed = 0.1
            peak_ground_distance = 0.02

        adapted = FootLockContactInterval.from_contact_interval(
            "LeftFoot",
            ExistingContact(),
        )
        self.assertEqual((2.0, 4.0, 3), (adapted.start, adapted.end, adapted.sample_count))
        self.assertEqual((1.0, 2.0, 3.0), adapted.anchor_world)


class FootLockSolverTests(unittest.TestCase):
    def test_nonidentity_rest_relations_still_reconstruct_world_anchor(self) -> None:
        source = _frame(1, left_slide=0.02)
        rotations = {
            "Hips": (_qz(-7.0), _qz(19.0)),
            "LeftUpperLeg": (_qz(13.0), _qz(31.0)),
            "LeftLowerLeg": (_qz(-23.0), _qz(4.0)),
            "LeftFoot": (_qz(37.0), _qz(-18.0)),
        }
        bones = tuple(
            BoneSample(
                name=bone.name,
                local_rotation_wxyz=rotations.get(bone.name, (bone.local_rotation_wxyz,))[0],
                global_rotation_wxyz=rotations.get(
                    bone.name,
                    (bone.global_rotation_wxyz, bone.global_rotation_wxyz),
                )[-1],
                head_world=bone.head_world,
                tail_world=bone.tail_world,
                parent=bone.parent,
            )
            for bone in source.bones
        )
        frame = MotionFrame(
            frame=source.frame,
            time_seconds=source.time_seconds,
            root_location=source.root_location,
            root_rotation_wxyz=source.root_rotation_wxyz,
            root_scale=source.root_scale,
            bones=bones,
            effectors=source.effectors,
        )
        anchor = (0.08, -0.18, 0.04)

        target = solve_foot_lock(
            _sequence(frame),
            (_interval("LeftFoot", 1, 1, anchor),),
        ).targets[0]
        values = {bone.bone_name: bone.local_rotation_wxyz for bone in target.bones}

        self.assertLess(
            math.dist(_reconstructed_effector(frame, values, "Left"), anchor),
            1.0e-9,
        )

    def test_neutral_boundary_targets_prevent_constant_extrapolation_leaks(self) -> None:
        sequence = _sequence(*(_frame(index, left_slide=index * 0.002) for index in range(1, 6)))
        anchor = (0.106, -0.18, 0.04)
        result = solve_foot_lock(
            sequence,
            (_interval("LeftFoot", 2, 4, anchor),),
        )

        self.assertEqual((1.0, 2.0, 3.0, 4.0, 5.0), tuple(t.scene_frame for t in result.targets))
        self.assertEqual(("LeftFoot",), result.targets[0].neutral_feet)
        self.assertEqual(("LeftFoot",), result.targets[-1].neutral_feet)
        self.assertEqual((), result.targets[0].contact_feet)
        self.assertTrue(
            all(target.contact_feet == ("LeftFoot",) for target in result.targets[1:4])
        )
        self.assertEqual(2, result.neutral_sample_count)
        self.assertEqual(3, result.locked_sample_count)
        self.assertEqual(anchor, result.contact_anchor("LeftFoot", 3))
        with self.assertRaisesRegex(ValueError, "not locked"):
            result.contact_anchor("LeftFoot", 1)

        for frame, target in (
            (sequence.frames[0], result.targets[0]),
            (sequence.frames[-1], result.targets[-1]),
        ):
            source = {bone.name: bone.local_rotation_wxyz for bone in frame.bones}
            self.assertEqual(
                source["LeftUpperLeg"],
                next(
                    bone.local_rotation_wxyz
                    for bone in target.bones
                    if bone.bone_name == "LeftUpperLeg"
                ),
            )

    def test_opposite_side_contact_and_neutral_boundaries_merge_per_frame(self) -> None:
        sequence = _sequence(*(_frame(index) for index in range(1, 5)))
        result = solve_foot_lock(
            sequence,
            (
                _interval("LeftFoot", 2, 3, (0.1, -0.18, 0.04)),
                _interval("RightFoot", 1, 2, (-0.1, -0.18, 0.04)),
            ),
        )

        self.assertEqual(4, len(result.targets))
        self.assertEqual(("RightFoot",), result.targets[0].contact_feet)
        self.assertEqual(("LeftFoot",), result.targets[0].neutral_feet)
        self.assertEqual(("LeftFoot", "RightFoot"), result.targets[1].contact_feet)
        self.assertEqual(("LeftFoot",), result.targets[2].contact_feet)
        self.assertEqual(("RightFoot",), result.targets[2].neutral_feet)
        self.assertEqual(("LeftFoot",), result.targets[3].neutral_feet)
        self.assertTrue(all(len(target.bones) == 6 for target in result.targets[:3]))
        self.assertEqual(3, len(result.targets[3].bones))

    def test_reachable_anchor_is_exact_and_writer_shaped(self) -> None:
        sequence = _sequence(
            _frame(1, left_slide=-0.02),
            _frame(2, left_slide=0.00),
            _frame(3, left_slide=0.02),
        )
        anchor = (0.10, -0.18, 0.04)
        interval = _interval("LeftFoot", 1, 3, anchor)

        result = solve_foot_lock(sequence, (interval,))

        self.assertEqual(3, result.locked_sample_count)
        self.assertEqual(0, result.clamped_sample_count)
        self.assertLess(result.maximum_residual_distance, 1.0e-10)
        for frame, target, diagnostic in zip(
            sequence.frames,
            result.targets,
            result.diagnostics,
            strict=True,
        ):
            self.assertEqual(
                ("LeftFoot", "LeftLowerLeg", "LeftUpperLeg"),
                tuple(bone.bone_name for bone in target.bones),
            )
            target_values = {
                bone.bone_name: bone.local_rotation_wxyz for bone in target.bones
            }
            actual = _reconstructed_effector(frame, target_values, "Left")
            self.assertLess(math.dist(actual, anchor), 1.0e-9)
            self.assertLess(math.dist(diagnostic.solved_effector_world, anchor), 1.0e-10)
            source_upper = next(
                bone for bone in frame.bones if bone.name == "LeftUpperLeg"
            )
            source_knee_vector = _sub(source_upper.tail_world, source_upper.head_world)
            self.assertGreater(
                sum(
                    a * b
                    for a, b in zip(
                        diagnostic.bend_direction_world,
                        source_knee_vector,
                        strict=True,
                    )
                ),
                0.0,
            )

    def test_two_planted_feet_share_one_frame_without_overwrite(self) -> None:
        sequence = _sequence(_frame(1), _frame(2), _frame(3))
        intervals = (
            _interval("LeftFoot", 1, 3, (0.12, -0.18, 0.04)),
            _interval("RightFoot", 1, 3, (-0.12, -0.18, 0.04)),
        )

        result = solve_foot_lock(sequence, intervals)

        self.assertEqual(6, result.locked_sample_count)
        self.assertEqual(3, len(result.targets))
        self.assertTrue(all(len(target.bones) == 6 for target in result.targets))
        self.assertTrue(
            all(
                len({bone.bone_name for bone in target.bones}) == 6
                for target in result.targets
            )
        )
        for index, (frame, target) in enumerate(
            zip(sequence.frames, result.targets, strict=True)
        ):
            target_values = {
                bone.bone_name: bone.local_rotation_wxyz for bone in target.bones
            }
            left = _reconstructed_effector(frame, target_values, "Left")
            right = _reconstructed_effector(frame, target_values, "Right")
            self.assertLess(math.dist(left, intervals[0].anchor), 1.0e-9, index)
            self.assertLess(math.dist(right, intervals[1].anchor), 1.0e-9, index)

    def test_unreachable_far_and_near_anchors_clamp_without_nan(self) -> None:
        sequence = _sequence(_frame(1))
        source = sequence.frames[0]
        foot = next(bone for bone in source.bones if bone.name == "LeftFoot")
        upper = next(bone for bone in source.bones if bone.name == "LeftUpperLeg")
        foot_offset = _sub(_effector(source, "LeftFoot"), foot.head_world)
        near_anchor = _add(upper.head_world, foot_offset)

        for anchor in ((0.1, 0.0, 5.0), near_anchor):
            with self.subTest(anchor=anchor):
                result = solve_foot_lock(
                    sequence,
                    (_interval("LeftFoot", 1, 1, anchor),),
                )
                diagnostic = result.diagnostics[0]
                self.assertTrue(diagnostic.was_clamped)
                self.assertGreater(diagnostic.residual_distance, 0.0)
                self.assertGreaterEqual(diagnostic.reach_distance, diagnostic.minimum_reach)
                self.assertLessEqual(diagnostic.reach_distance, diagnostic.maximum_reach)
                for bone in result.targets[0].bones:
                    self.assertTrue(all(math.isfinite(value) for value in bone.local_rotation_wxyz))
                    self.assertAlmostEqual(1.0, math.hypot(*bone.local_rotation_wxyz))

    def test_output_quaternion_signs_are_continuous(self) -> None:
        sequence = _sequence(
            _frame(1, left_slide=-0.01, quaternion_sign=1.0),
            _frame(2, left_slide=0.00, quaternion_sign=-1.0),
            _frame(3, left_slide=0.01, quaternion_sign=1.0),
        )
        result = solve_foot_lock(
            sequence,
            (_interval("LeftFoot", 1, 3, (0.1, -0.18, 0.04)),),
        )
        by_frame = [
            {bone.bone_name: bone.local_rotation_wxyz for bone in target.bones}
            for target in result.targets
        ]
        for left, right in zip(by_frame, by_frame[1:], strict=False):
            for name in left:
                dot = sum(
                    a * b for a, b in zip(left[name], right[name], strict=True)
                )
                self.assertGreaterEqual(dot, 0.0, name)

    def test_solver_is_repeatable_and_does_not_mutate_input(self) -> None:
        sequence = _sequence(_frame(1), _frame(2, left_slide=0.01), _frame(3))
        interval = _interval("LeftFoot", 1, 3, (0.1, -0.18, 0.04))
        before = sequence.canonical_json()

        first = solve_foot_lock(sequence, (interval,))
        second = solve_foot_lock(sequence, (interval,))

        self.assertEqual(first, second)
        self.assertEqual(before, sequence.canonical_json())
        with self.assertRaises(FrozenInstanceError):
            first.targets[0].scene_frame = 10.0  # type: ignore[misc]

    def test_no_contacts_emits_no_targets(self) -> None:
        sequence = _sequence(_frame(1), _frame(2), _frame(3))
        result = solve_foot_lock(
            sequence,
            settings=FootLockSettings(max_contact_height=0.0),
        )
        self.assertEqual(((), (), ()), (result.intervals, result.targets, result.diagnostics))


class FootLockSafetyTests(unittest.TestCase):
    def test_overlapping_same_foot_intervals_are_rejected(self) -> None:
        sequence = _sequence(*(_frame(index) for index in range(1, 5)))
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            solve_foot_lock(
                sequence,
                (
                    _interval("LeftFoot", 1, 3, (0.1, 0.0, 0.0)),
                    _interval("LeftFoot", 3, 4, (0.1, 0.0, 0.0)),
                ),
            )

    def test_interval_must_align_to_dense_sampled_frames(self) -> None:
        first = _frame(1)
        third = MotionFrame(
            frame=3.0,
            time_seconds=2.0 / 30.0,
            root_location=first.root_location,
            root_rotation_wxyz=first.root_rotation_wxyz,
            root_scale=first.root_scale,
            bones=first.bones,
            effectors=first.effectors,
        )
        with self.assertRaisesRegex(ValueError, "consecutive whole-frame"):
            solve_foot_lock(_sequence(first, third), ())

    def test_nonuniform_root_scale_is_rejected_before_solve(self) -> None:
        sequence = _sequence(_frame(1, root_scale=(1.0, 1.0, 2.0)))
        with self.assertRaisesRegex(ValueError, "positive and uniform"):
            solve_foot_lock(
                sequence,
                (_interval("LeftFoot", 1, 1, (0.1, -0.18, 0.04)),),
            )

    def test_disconnected_chain_is_rejected(self) -> None:
        source = _frame(1)
        bones = tuple(
            BoneSample(
                name=bone.name,
                local_rotation_wxyz=bone.local_rotation_wxyz,
                global_rotation_wxyz=bone.global_rotation_wxyz,
                head_world=(9.0, 9.0, 9.0)
                if bone.name == "LeftLowerLeg"
                else bone.head_world,
                tail_world=bone.tail_world,
                parent=bone.parent,
            )
            for bone in source.bones
        )
        malformed = MotionFrame(
            frame=source.frame,
            time_seconds=source.time_seconds,
            root_location=source.root_location,
            root_rotation_wxyz=source.root_rotation_wxyz,
            root_scale=source.root_scale,
            bones=bones,
            effectors=source.effectors,
        )
        with self.assertRaisesRegex(ValueError, "chain joints are disconnected"):
            solve_foot_lock(
                _sequence(malformed),
                (_interval("LeftFoot", 1, 1, (0.1, -0.18, 0.04)),),
            )

    def test_invalid_settings_and_interval_values_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_contact_speed"):
            FootLockSettings(max_contact_speed=-1.0)
        with self.assertRaisesRegex(TypeError, "min_contact_frames"):
            FootLockSettings(min_contact_frames=True)
        with self.assertRaisesRegex(ValueError, "sample_count"):
            FootLockContactInterval("LeftFoot", 1, 3, (0.0, 0.0, 0.0), 2)
        with self.assertRaisesRegex(ValueError, "LeftFoot or RightFoot"):
            _interval("Foot", 1, 1, (0.0, 0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
