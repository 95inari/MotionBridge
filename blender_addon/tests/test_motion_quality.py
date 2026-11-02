from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
import math
import unittest

from motion_bridge.motion_quality import (
    BoneSample,
    ContactSample,
    EffectorSample,
    IssueSeverity,
    LimbDefinition,
    MAX_MOTION_QUALITY_ISSUES,
    MotionFrame,
    MotionSequence,
    MotionValidationSettings,
    QualitySummary,
    analyze_motion,
    validate_motion_sequence,
)


IDENTITY = (1.0, 0.0, 0.0, 0.0)


def _qz(degrees: float, sign: float = 1.0) -> tuple[float, float, float, float]:
    half = math.radians(degrees) * 0.5
    return (sign * math.cos(half), 0.0, 0.0, sign * math.sin(half))


def _bone(
    name: str,
    *,
    rotation: tuple[float, float, float, float] = IDENTITY,
    head: tuple[float, float, float] = (0.0, 0.0, 0.0),
    tail: tuple[float, float, float] = (0.0, 0.0, 0.2),
    parent: str | None = None,
) -> BoneSample:
    return BoneSample(
        name=name,
        local_rotation_wxyz=rotation,
        global_rotation_wxyz=rotation,
        head_world=head,
        tail_world=tail,
        parent=parent,
    )


def _basic_frame(
    frame: int,
    *,
    fps: float = 30.0,
    bone_name: str = "Hips",
    rotation: tuple[float, float, float, float] = IDENTITY,
    root_location: tuple[float, float, float] = (0.0, 0.0, 0.0),
    time_seconds: float | None = None,
    key_count: int | None = None,
) -> MotionFrame:
    return MotionFrame(
        frame=frame,
        time_seconds=frame / fps if time_seconds is None else time_seconds,
        root_location=root_location,
        root_rotation_wxyz=IDENTITY,
        root_scale=(1.0, 1.0, 1.0),
        bones=(_bone(bone_name, rotation=rotation),),
        key_count=key_count,
    )


def _sequence(*frames: MotionFrame, fps: float = 30.0, **kwargs: object) -> MotionSequence:
    return MotionSequence(
        fps=fps,
        start_frame=frames[0].frame,
        end_frame=frames[-1].frame,
        frames=frames,
        **kwargs,
    )


class MotionQualityGoldenTests(unittest.TestCase):
    def _golden_sequence(self) -> MotionSequence:
        frames: list[MotionFrame] = []
        for index, (joint_angle, root_angle, root_x, foot_x, hand_x, keys) in enumerate(
            (
                (0.0, 0.0, 0.0, 0.00, 0.9, 0),
                (90.0, 120.0, 0.2, 0.03, 1.0, 100),
                (90.0, 120.0, 0.2, 0.04, 1.1, 50),
            )
        ):
            bones = (
                _bone("Joint", rotation=_qz(joint_angle)),
                _bone("Upper", head=(0.0, 0.0, 0.0), tail=(0.4, 0.0, 0.0)),
                _bone(
                    "Lower",
                    head=(0.4, 0.0, 0.0),
                    tail=(0.8, 0.0, 0.0),
                    parent="Upper",
                ),
                _bone(
                    "Hand",
                    head=(0.8, 0.0, 0.0),
                    tail=(1.0, 0.0, 0.0),
                    parent="Lower",
                ),
                _bone(
                    "LeftFoot",
                    head=(foot_x, 0.0, 0.1),
                    tail=(foot_x, 0.0, -0.02),
                ),
            )
            frames.append(
                MotionFrame(
                    frame=index,
                    time_seconds=index / 10.0,
                    root_location=(root_x, 0.0, 0.0),
                    root_rotation_wxyz=_qz(root_angle),
                    root_scale=(1.0, 1.0, 1.0),
                    bones=bones,
                    effectors=(
                        EffectorSample("Hand", (hand_x, 0.0, 0.0)),
                        EffectorSample("LeftFoot", (foot_x, 0.0, -0.02)),
                    ),
                    contacts=(ContactSample("LeftFoot", True, 0.0),),
                    key_count=keys,
                )
            )
        return MotionSequence(
            fps=10.0,
            start_frame=0,
            end_frame=2,
            frames=tuple(frames),
            source_id="golden",
            keyed_channel_count=10,
            metadata={"z": 2, "a": "stable"},
        )

    def _golden_settings(self) -> MotionValidationSettings:
        return MotionValidationSettings(
            bone_angular_speed_warning_deg_s=100.0,
            bone_angular_speed_error_deg_s=800.0,
            bone_angular_acceleration_warning_deg_s2=100.0,
            bone_angular_acceleration_error_deg_s2=8_000.0,
            root_translation_warning_m_per_frame=0.05,
            root_translation_error_m_per_frame=0.15,
            root_rotation_warning_deg_per_frame=60.0,
            root_rotation_error_deg_per_frame=100.0,
            foot_slide_warning_m=0.01,
            foot_slide_error_m=0.035,
            ground_penetration_warning_m=0.005,
            ground_penetration_error_m=0.015,
            limb_reach_warning_ratio=0.95,
            limb_reach_error_ratio=1.05,
            key_density_warning_keys_channel_s=20.0,
            key_density_error_keys_channel_s=80.0,
            auto_detect_contacts=False,
            limbs=(LimbDefinition("arm", ("Upper", "Lower", "Hand"), "Hand"),),
        )

    def test_known_metrics_and_three_level_summary(self) -> None:
        report = analyze_motion(self._golden_sequence(), self._golden_settings())

        self.assertEqual(QualitySummary.FIX_RECOMMENDED, report.summary)
        self.assertTrue(report.input_finite)
        self.assertEqual(3, report.sample_count)
        self.assertAlmostEqual(900.0, report.metric("bone_angular_speed").maximum)
        self.assertAlmostEqual(9_000.0, report.metric("bone_angular_acceleration").maximum)
        self.assertAlmostEqual(0.2, report.metric("root_translation_discontinuity").maximum)
        self.assertAlmostEqual(120.0, report.metric("root_rotation_discontinuity").maximum)
        self.assertAlmostEqual(0.04, report.metric("foot_slide").maximum)
        self.assertAlmostEqual(0.02, report.metric("ground_penetration").p95)
        self.assertAlmostEqual(1.1, report.metric("limb_reach").maximum)
        self.assertAlmostEqual(100.0, report.metric("key_density").maximum)
        self.assertEqual(3, report.metric("key_density").count)
        self.assertEqual(0.0, report.metric("key_density").frame_range.start_frame)
        self.assertEqual(2.0, report.metric("key_density").frame_range.end_frame)
        self.assertTrue(any(issue.severity is IssueSeverity.FIX for issue in report.issues))

    def test_report_json_and_hash_are_golden_and_repeatable(self) -> None:
        first = analyze_motion(self._golden_sequence(), self._golden_settings())
        second = analyze_motion(self._golden_sequence(), self._golden_settings())
        payload = json.loads(first.canonical_json())

        self.assertEqual(first.canonical_json(), second.canonical_json())
        self.assertEqual(first.content_hash(), second.content_hash())
        self.assertEqual("motion-bridge.motion-quality-report/1", payload["schema"])
        self.assertEqual("fix_recommended", payload["summary"])
        self.assertEqual(
            "955bf3dffff37d62507fc902e3062435fc5fd69aa2891af60d617e751fc5a341",
            first.content_hash(),
        )
        self.assertNotIn("NaN", first.canonical_json())

    def test_nearest_rank_p95_is_deterministic(self) -> None:
        report = analyze_motion(self._golden_sequence(), self._golden_settings())
        # Nearest-rank p95 of [0, 50, 100] is 100.
        self.assertEqual(100.0, report.metric("key_density").p95)


class MotionQualityBoundaryTests(unittest.TestCase):
    def test_fragmented_issue_intervals_have_a_hard_report_limit(self) -> None:
        frame_count = MAX_MOTION_QUALITY_ISSUES * 2 + 2
        frames = tuple(
            MotionFrame(
                frame=index,
                time_seconds=index / 30.0,
                root_location=(float((index + 1) // 2), 0.0, 0.0),
                root_rotation_wxyz=IDENTITY,
                root_scale=(1.0, 1.0, 1.0),
                bones=(),
            )
            for index in range(frame_count)
        )
        sequence = MotionSequence(
            fps=30.0,
            start_frame=0,
            end_frame=frame_count - 1,
            frames=frames,
        )

        with self.assertRaisesRegex(ValueError, "issue interval limit"):
            analyze_motion(
                sequence,
                MotionValidationSettings(auto_detect_contacts=False, limbs=()),
            )

    def test_equal_threshold_is_not_an_issue(self) -> None:
        sequence = _sequence(
            _basic_frame(0),
            _basic_frame(1, root_location=(0.2, 0.0, 0.0)),
        )
        settings = MotionValidationSettings(
            root_translation_warning_m_per_frame=0.2,
            root_translation_error_m_per_frame=0.2,
            auto_detect_contacts=False,
            limbs=(),
        )

        report = analyze_motion(sequence, settings)
        root_issues = [
            issue for issue in report.issues if issue.metric == "root_translation_discontinuity"
        ]
        self.assertEqual([], root_issues)
        self.assertEqual(QualitySummary.NO_ISSUES, report.summary)

    def test_sign_flip_is_reported_without_false_angular_speed(self) -> None:
        sequence = _sequence(
            _basic_frame(0, rotation=_qz(20.0)),
            _basic_frame(1, rotation=_qz(20.0, -1.0)),
        )
        report = analyze_motion(
            sequence,
            MotionValidationSettings(auto_detect_contacts=False, limbs=()),
        )

        self.assertEqual(0.0, report.metric("bone_angular_speed").maximum)
        self.assertEqual(1.0, report.metric("quaternion_sign_flip").maximum)
        self.assertEqual(QualitySummary.REVIEW_RECOMMENDED, report.summary)
        flip_issues = [issue for issue in report.issues if issue.code == "quaternion_sign_flip"]
        self.assertEqual(2, len(flip_issues))  # local and global samples
        self.assertTrue(all(issue.interval.start_frame == 1.0 for issue in flip_issues))

    def test_quaternion_unit_error_is_detected_but_normalized_for_motion(self) -> None:
        scaled = tuple(value * 1.02 for value in _qz(30.0))
        sequence = _sequence(
            _basic_frame(0, rotation=scaled),
            _basic_frame(1, rotation=scaled),
        )
        report = analyze_motion(
            sequence,
            MotionValidationSettings(auto_detect_contacts=False, limbs=()),
        )

        self.assertAlmostEqual(0.02, report.metric("quaternion_unit_error").maximum)
        self.assertEqual(0.0, report.metric("bone_angular_speed").maximum)
        self.assertEqual(QualitySummary.FIX_RECOMMENDED, report.summary)

    def test_auto_contact_uses_same_name_bone_tail_fallback(self) -> None:
        frames = tuple(
            MotionFrame(
                frame=index,
                time_seconds=index / 10.0,
                root_location=(0.0, 0.0, 0.0),
                root_rotation_wxyz=IDENTITY,
                root_scale=(1.0, 1.0, 1.0),
                bones=(
                    _bone(
                        "LeftFoot",
                        head=(x, 0.0, 0.1),
                        tail=(x, 0.0, -0.01),
                    ),
                ),
            )
            for index, x in enumerate((0.0, 0.01, 0.02))
        )
        sequence = MotionSequence(10.0, 0.0, 2.0, frames)
        settings = MotionValidationSettings(
            foot_slide_warning_m=0.005,
            foot_slide_error_m=0.015,
            ground_penetration_warning_m=0.001,
            ground_penetration_error_m=0.005,
            contact_max_speed_m_s=0.2,
            contact_min_samples=2,
            limbs=(),
        )

        report = analyze_motion(sequence, settings)
        self.assertAlmostEqual(0.02, report.metric("foot_slide").maximum)
        self.assertAlmostEqual(0.01, report.metric("ground_penetration").maximum)
        self.assertEqual(3, report.metric("foot_slide").count)
        self.assertEqual(QualitySummary.FIX_RECOMMENDED, report.summary)

    def test_missing_contact_run_is_not_guessed_from_a_single_frame(self) -> None:
        sequence = _sequence(_basic_frame(0))
        report = analyze_motion(
            sequence,
            MotionValidationSettings(contact_effectors=("Hips",), limbs=()),
        )
        self.assertEqual(0, report.metric("foot_slide").count)

    def test_metadata_key_density_fallback(self) -> None:
        sequence = _sequence(_basic_frame(0), metadata={"key_density": 31.0})
        report = analyze_motion(
            sequence,
            MotionValidationSettings(auto_detect_contacts=False, limbs=()),
        )
        self.assertEqual(31.0, report.metric("key_density").maximum)
        self.assertEqual(QualitySummary.FIX_RECOMMENDED, report.summary)


class MotionQualityCanonicalTests(unittest.TestCase):
    def test_nested_inputs_are_normalized_to_immutable_sorted_tuples(self) -> None:
        left = _bone("A")
        right = _bone("B")
        frame = MotionFrame(
            frame=0,
            time_seconds=0,
            root_location=[0, 0, 0],
            root_rotation_wxyz=[1, 0, 0, 0],
            root_scale=[1, 1, 1],
            bones=[right, left],
            effectors=[EffectorSample("Z", [0, 0, 0])],
        )
        sequence = MotionSequence(30, 0, 0, [frame], metadata={"z": 1, "a": True})

        self.assertEqual(("A", "B"), tuple(bone.name for bone in frame.bones))
        self.assertEqual((("a", True), ("z", 1)), sequence.metadata)
        self.assertIsInstance(frame.root_location, tuple)
        with self.assertRaises(FrozenInstanceError):
            frame.frame = 2  # type: ignore[misc]

    def test_semantically_equal_orderings_have_same_json_and_hash(self) -> None:
        a = _bone("A")
        b = _bone("B")

        def make(bones: list[BoneSample], metadata: dict[str, object]) -> MotionSequence:
            frame = MotionFrame(0, 0, (0, 0, 0), IDENTITY, (1, 1, 1), bones)
            return MotionSequence(30, 0, 0, (frame,), metadata=metadata)  # type: ignore[arg-type]

        first = make([a, b], {"a": 1, "b": "two"})
        second = make([b, a], {"b": "two", "a": 1})
        self.assertEqual(first.canonical_json(), second.canonical_json())
        self.assertEqual(first.content_hash(), second.content_hash())

    def test_settings_hash_covers_thresholds(self) -> None:
        default = MotionValidationSettings()
        changed = MotionValidationSettings(foot_slide_warning_m=0.019)
        self.assertNotEqual(default.content_hash(), changed.content_hash())
        self.assertEqual(default.content_hash(), MotionValidationSettings().content_hash())


class MotionQualityInvalidInputTests(unittest.TestCase):
    def test_non_finite_and_zero_quaternion_are_rejected_early(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite"):
            _bone("Bad", head=(math.nan, 0.0, 0.0))
        with self.assertRaisesRegex(ValueError, "non-zero norm"):
            _bone("Bad", rotation=(0.0, 0.0, 0.0, 0.0))
        with self.assertRaisesRegex(ValueError, "norm must be finite"):
            _bone("Bad", rotation=(1.0e308, 1.0e308, 1.0e308, 1.0e308))
        with self.assertRaisesRegex(ValueError, "finite"):
            MotionSequence(30, 0, 0, (_basic_frame(0),), metadata={"bad": math.inf})

    def test_invalid_fps_range_and_budget_are_rejected(self) -> None:
        sequence = _sequence(_basic_frame(0), _basic_frame(1))
        with self.assertRaisesRegex(ValueError, "fps"):
            analyze_motion(MotionSequence(481, 0, 0, (_basic_frame(0),)))
        with self.assertRaisesRegex(ValueError, "max_frame_count"):
            validate_motion_sequence(sequence, MotionValidationSettings(max_frame_count=1))

    def test_order_time_and_declared_range_are_validated(self) -> None:
        reversed_sequence = MotionSequence(
            30,
            0,
            1,
            (_basic_frame(1), _basic_frame(0)),
        )
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            validate_motion_sequence(reversed_sequence)

        bad_time = _sequence(
            _basic_frame(0),
            _basic_frame(1, time_seconds=1.0),
        )
        with self.assertRaisesRegex(ValueError, "frame delta/fps"):
            validate_motion_sequence(bad_time)

        outside = MotionSequence(30, 10, 20, (_basic_frame(0),))
        with self.assertRaisesRegex(ValueError, "outside"):
            validate_motion_sequence(outside)

    def test_cross_frame_skeleton_and_contact_references_are_validated(self) -> None:
        inconsistent = _sequence(_basic_frame(0), _basic_frame(1, bone_name="Spine"))
        with self.assertRaisesRegex(ValueError, "inconsistent bone set"):
            validate_motion_sequence(inconsistent)

        bad_contact_frame = MotionFrame(
            0,
            0,
            (0, 0, 0),
            IDENTITY,
            (1, 1, 1),
            (_bone("Hips"),),
            contacts=(ContactSample("MissingFoot", True),),
        )
        with self.assertRaisesRegex(ValueError, "missing effector"):
            validate_motion_sequence(_sequence(bad_contact_frame))

    def test_hierarchy_cycles_and_coordinate_limits_are_rejected(self) -> None:
        cycle_frame = MotionFrame(
            0,
            0,
            (0, 0, 0),
            IDENTITY,
            (1, 1, 1),
            (_bone("A", parent="B"), _bone("B", parent="A")),
        )
        with self.assertRaisesRegex(ValueError, "cycle"):
            validate_motion_sequence(_sequence(cycle_frame))

        far = _basic_frame(0, root_location=(11.0, 0.0, 0.0))
        with self.assertRaisesRegex(ValueError, "max_abs_coordinate"):
            validate_motion_sequence(
                _sequence(far),
                MotionValidationSettings(max_abs_coordinate=10.0),
            )

    def test_key_density_input_must_be_complete(self) -> None:
        mixed = _sequence(
            _basic_frame(0, key_count=1),
            _basic_frame(1),
            keyed_channel_count=4,
        )
        with self.assertRaisesRegex(ValueError, "every frame"):
            validate_motion_sequence(mixed)
        missing_channels = _sequence(_basic_frame(0, key_count=1))
        with self.assertRaisesRegex(ValueError, "keyed_channel_count"):
            validate_motion_sequence(missing_channels)

    def test_bad_settings_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "warning <= error"):
            MotionValidationSettings(
                foot_slide_warning_m=1.0,
                foot_slide_error_m=0.5,
            )
        with self.assertRaisesRegex(ValueError, "positive"):
            MotionValidationSettings(max_frame_count=0)


class MotionQualityPropertyTests(unittest.TestCase):
    def test_alternating_quaternion_sign_does_not_change_angular_metrics(self) -> None:
        positive_frames = tuple(
            _basic_frame(index, rotation=_qz(index * 10.0)) for index in range(10)
        )
        alternating_frames = tuple(
            _basic_frame(
                index,
                rotation=_qz(index * 10.0, -1.0 if index % 2 else 1.0),
            )
            for index in range(10)
        )
        settings = MotionValidationSettings(
            bone_angular_speed_warning_deg_s=1_000.0,
            bone_angular_speed_error_deg_s=2_000.0,
            bone_angular_acceleration_warning_deg_s2=1_000.0,
            bone_angular_acceleration_error_deg_s2=2_000.0,
            auto_detect_contacts=False,
            limbs=(),
        )
        positive = analyze_motion(_sequence(*positive_frames), settings)
        alternating = analyze_motion(_sequence(*alternating_frames), settings)

        self.assertAlmostEqual(
            positive.metric("bone_angular_speed").maximum,
            alternating.metric("bone_angular_speed").maximum,
        )
        self.assertAlmostEqual(300.0, alternating.metric("bone_angular_speed").maximum)
        self.assertEqual(1.0, alternating.metric("quaternion_sign_flip").maximum)

    def test_all_public_metric_values_are_finite_across_angle_sweep(self) -> None:
        frames = tuple(
            _basic_frame(index, rotation=_qz((index * 37.0) % 360.0))
            for index in range(40)
        )
        report = analyze_motion(
            _sequence(*frames),
            MotionValidationSettings(auto_detect_contacts=False, limbs=()),
        )
        for metric in report.metrics:
            if metric.maximum is not None:
                self.assertTrue(math.isfinite(metric.maximum), metric.name)
                self.assertTrue(math.isfinite(metric.p95), metric.name)


if __name__ == "__main__":
    unittest.main()
