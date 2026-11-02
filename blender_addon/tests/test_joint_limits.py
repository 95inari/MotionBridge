from __future__ import annotations

import math
import unittest

from motion_bridge.joint_limits import (
    DEFAULT_SOLVE_PROFILE,
    JointLimit,
    SolveProfile,
    SolveProfileError,
    limit_motion_joints,
)
from motion_bridge.motion_quality import BoneSample, MotionFrame, MotionSequence


def _axis_angle(
    axis: tuple[float, float, float],
    degrees: float,
) -> tuple[float, float, float, float]:
    half = math.radians(degrees) * 0.5
    scale = math.sin(half)
    return (math.cos(half), *(component * scale for component in axis))


def _multiply(
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


def _bone(name: str, rotation: tuple[float, float, float, float]) -> BoneSample:
    return BoneSample(
        name=name,
        local_rotation_wxyz=rotation,
        global_rotation_wxyz=rotation,
        head_world=(0.0, 0.0, 0.0),
        tail_world=(0.0, 1.0, 0.0),
    )


def _sequence(
    rotations: tuple[
        tuple[
            tuple[str, tuple[float, float, float, float]],
            ...,
        ],
        ...,
    ],
) -> MotionSequence:
    frames = tuple(
        MotionFrame(
            frame=float(index + 1),
            time_seconds=index / 30.0,
            root_location=(0.0, 0.0, 0.0),
            root_rotation_wxyz=(1.0, 0.0, 0.0, 0.0),
            root_scale=(1.0, 1.0, 1.0),
            bones=tuple(_bone(name, quaternion) for name, quaternion in frame),
        )
        for index, frame in enumerate(rotations)
    )
    return MotionSequence(
        fps=30.0,
        start_frame=1.0,
        end_frame=float(len(frames)),
        frames=frames,
    )


def _rotation(
    result: object,
    frame_index: int,
    bone_name: str,
) -> tuple[float, float, float, float]:
    frame = result.frames[frame_index]
    return dict(frame.local_rotations_wxyz)[bone_name]


def _angle(quaternion: tuple[float, float, float, float]) -> float:
    return math.degrees(
        2.0 * math.acos(max(-1.0, min(1.0, abs(float(quaternion[0])))))
    )


class SolveProfileTests(unittest.TestCase):
    def test_default_profile_is_canonical_and_round_trips(self) -> None:
        encoded = DEFAULT_SOLVE_PROFILE.to_json()
        decoded = SolveProfile.from_json(encoded)
        self.assertEqual(decoded, DEFAULT_SOLVE_PROFILE)
        self.assertEqual(decoded.to_json(), encoded)
        self.assertEqual(
            tuple(limit.bone_name for limit in decoded.limits),
            tuple(sorted(limit.bone_name for limit in decoded.limits)),
        )

    def test_invalid_limits_and_json_are_rejected(self) -> None:
        with self.assertRaises(SolveProfileError):
            JointLimit("Spine", 180.0, -20.0, 20.0)
        with self.assertRaises(SolveProfileError):
            JointLimit("Spine", 20.0, 30.0, -30.0)
        with self.assertRaises(SolveProfileError):
            JointLimit(
                "Spine",
                45.0,
                -30.0,
                30.0,
                min_swing_x_degrees=-10.0,
            )
        with self.assertRaises(SolveProfileError):
            JointLimit(
                "Spine",
                45.0,
                -30.0,
                30.0,
                min_swing_z_degrees=5.0,
                max_swing_z_degrees=20.0,
            )
        with self.assertRaises(SolveProfileError):
            SolveProfile(
                "duplicate",
                (
                    JointLimit("Spine", 45.0, -30.0, 30.0),
                    JointLimit("Spine", 50.0, -30.0, 30.0),
                ),
            )
        with self.assertRaises(SolveProfileError):
            SolveProfile.from_json('{"schema":"unknown"}')


class JointLimitSolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = SolveProfile(
            "test",
            (
                JointLimit("Spine", 45.0, -30.0, 30.0, 0.0),
            ),
        )

    def test_swing_and_twist_are_clamped_without_euler_conversion(self) -> None:
        original = _multiply(
            _axis_angle((1.0, 0.0, 0.0), 90.0),
            _axis_angle((0.0, 1.0, 0.0), 80.0),
        )
        result = limit_motion_joints(
            _sequence(((('Spine', original),),)),
            profile=self.profile,
            preserve_endpoints=False,
        )
        corrected = _rotation(result, 0, "Spine")
        from motion_bridge import joint_limits as implementation

        swing, twist = implementation._swing_twist_y(corrected)
        self.assertAlmostEqual(_angle(swing), 45.0, places=6)
        self.assertAlmostEqual(
            implementation._signed_twist_y_degrees(twist),
            30.0,
            places=6,
        )
        self.assertEqual(result.corrected_sample_count, 1)
        self.assertGreater(result.maximum_change_degrees, 0.0)

    def test_signed_hinge_blocks_hyperextension_and_side_bend(self) -> None:
        hinge = SolveProfile(
            "knee",
            (
                JointLimit(
                    "LeftLowerLeg",
                    165.0,
                    -40.0,
                    40.0,
                    0.0,
                    min_swing_x_degrees=-165.0,
                    max_swing_x_degrees=10.0,
                    min_swing_z_degrees=-12.0,
                    max_swing_z_degrees=12.0,
                ),
            ),
        )
        result = limit_motion_joints(
            _sequence(
                (
                    (("LeftLowerLeg", _axis_angle((1.0, 0.0, 0.0), 90.0)),),
                    (("LeftLowerLeg", _axis_angle((1.0, 0.0, 0.0), -90.0)),),
                    (("LeftLowerLeg", _axis_angle((0.0, 0.0, 1.0), 45.0)),),
                )
            ),
            profile=hinge,
            preserve_endpoints=False,
        )
        from motion_bridge import joint_limits as implementation

        hyper_x, _hyper_z = implementation._swing_vector_xz_degrees(
            implementation._swing_twist_y(_rotation(result, 0, "LeftLowerLeg"))[0]
        )
        flex_x, _flex_z = implementation._swing_vector_xz_degrees(
            implementation._swing_twist_y(_rotation(result, 1, "LeftLowerLeg"))[0]
        )
        _side_x, side_z = implementation._swing_vector_xz_degrees(
            implementation._swing_twist_y(_rotation(result, 2, "LeftLowerLeg"))[0]
        )
        self.assertAlmostEqual(hyper_x, 10.0, places=6)
        self.assertAlmostEqual(flex_x, -90.0, places=6)
        self.assertAlmostEqual(side_z, 12.0, places=6)

    def test_soft_zone_is_continuous_and_never_exceeds_limit(self) -> None:
        profile = SolveProfile(
            "soft",
            (JointLimit("Spine", 45.0, -30.0, 30.0, 5.0),),
        )
        unchanged = _axis_angle((1.0, 0.0, 0.0), 39.0)
        softened = _axis_angle((1.0, 0.0, 0.0), 45.0)
        outside = _axis_angle((1.0, 0.0, 0.0), 120.0)
        result = limit_motion_joints(
            _sequence(
                (
                    (("Spine", unchanged),),
                    (("Spine", softened),),
                    (("Spine", outside),),
                )
            ),
            profile=profile,
            preserve_endpoints=False,
        )
        self.assertAlmostEqual(_angle(_rotation(result, 0, "Spine")), 39.0, places=6)
        self.assertGreater(_angle(_rotation(result, 1, "Spine")), 40.0)
        self.assertLess(_angle(_rotation(result, 1, "Spine")), 45.0)
        self.assertLessEqual(_angle(_rotation(result, 2, "Spine")), 45.0)

    def test_unknown_bones_pass_through_and_signs_remain_continuous(self) -> None:
        physical = _axis_angle((0.0, 0.0, 1.0), 20.0)
        negated = tuple(-component for component in physical)
        sequence = _sequence(
            (
                (("Custom", physical), ("Spine", (1.0, 0.0, 0.0, 0.0))),
                (("Custom", negated), ("Spine", (1.0, 0.0, 0.0, 0.0))),
            )
        )
        before = sequence.canonical_json()
        result = limit_motion_joints(sequence, profile=self.profile)
        first = _rotation(result, 0, "Custom")
        second = _rotation(result, 1, "Custom")
        self.assertGreater(sum(a * b for a, b in zip(first, second, strict=True)), 0.999)
        self.assertAlmostEqual(_angle(first), 20.0, places=6)
        self.assertAlmostEqual(_angle(second), 20.0, places=6)
        self.assertEqual(sequence.canonical_json(), before)
        self.assertEqual(result.limited_bone_names, ("Spine",))

    def test_deterministic_output_and_budget_validation(self) -> None:
        sequence = _sequence(
            (
                (("Spine", _axis_angle((1.0, 0.0, 0.0), 80.0)),),
                (("Spine", _axis_angle((1.0, 0.0, 0.0), 85.0)),),
            )
        )
        self.assertEqual(
            limit_motion_joints(sequence, profile=self.profile),
            limit_motion_joints(sequence, profile=self.profile),
        )
        with self.assertRaisesRegex(ValueError, "maximum is 1"):
            limit_motion_joints(sequence, profile=self.profile, max_frames=1)
        with self.assertRaisesRegex(ValueError, "maximum is 1"):
            limit_motion_joints(sequence, profile=self.profile, max_bone_samples=1)
        with self.assertRaises(ValueError):
            limit_motion_joints(sequence, profile=self.profile, max_frames=True)

    def test_frame_order_and_skeleton_drift_are_rejected(self) -> None:
        first = MotionFrame(
            frame=1.0,
            time_seconds=0.0,
            root_location=(0.0, 0.0, 0.0),
            root_rotation_wxyz=(1.0, 0.0, 0.0, 0.0),
            root_scale=(1.0, 1.0, 1.0),
            bones=(_bone("Spine", (1.0, 0.0, 0.0, 0.0)),),
        )
        bad_time = MotionFrame(
            frame=2.0,
            time_seconds=0.0,
            root_location=(0.0, 0.0, 0.0),
            root_rotation_wxyz=(1.0, 0.0, 0.0, 0.0),
            root_scale=(1.0, 1.0, 1.0),
            bones=(_bone("Spine", (1.0, 0.0, 0.0, 0.0)),),
        )
        changed_bones = MotionFrame(
            frame=2.0,
            time_seconds=1.0 / 30.0,
            root_location=(0.0, 0.0, 0.0),
            root_rotation_wxyz=(1.0, 0.0, 0.0, 0.0),
            root_scale=(1.0, 1.0, 1.0),
            bones=(_bone("Head", (1.0, 0.0, 0.0, 0.0)),),
        )
        for second, message in (
            (bad_time, "times must be strictly increasing"),
            (changed_bones, "same ordered bones|inconsistent bone set"),
        ):
            sequence = MotionSequence(
                fps=30.0,
                start_frame=1.0,
                end_frame=2.0,
                frames=(first, second),
            )
            with self.assertRaisesRegex(ValueError, message):
                limit_motion_joints(sequence, profile=self.profile)


if __name__ == "__main__":
    unittest.main()
