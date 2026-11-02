from __future__ import annotations

import json
import math
from pathlib import Path
import sys

import bpy
from mathutils import Quaternion, Vector


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))

from motion_bridge.canonical.rig_spec import SKELETON_ID  # noqa: E402
from motion_bridge.editing.action_api import iter_action_fcurves  # noqa: E402
from motion_bridge.editing.pose_api import set_pose_bone_selected  # noqa: E402
import motion_bridge.editing.correction_envelope as envelope_module  # noqa: E402
from motion_bridge.editing.correction_edit import (  # noqa: E402
    enter_correction_edit_mode,
    exit_correction_edit_mode,
    insert_correction_pose_keys,
)
from motion_bridge.editing.correction_envelope import (  # noqa: E402
    EnvelopeRange,
    EnvelopePreset,
    EnvelopeRangeSource,
    adaptive_envelope_samples,
    begin_committed_pose_envelope_preview,
    begin_pose_envelope_preview,
    begin_reweight_envelope_preview,
    committed_pose_envelope_available,
    evaluate_envelope_preset,
    has_correction_envelope_preview,
    resolve_envelope_range,
    weighted_location_delta,
    weighted_quaternion_delta,
)
from motion_bridge.editing.correction_layers import (  # noqa: E402
    action_checksum,
    create_correction_layer,
    validate_layer_stack,
)


FIRST_FRAME = 1
LAST_FRAME = 20
EPSILON = 1.0e-5


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for action in tuple(bpy.data.actions):
        bpy.data.actions.remove(action)


def _create_armature(name: str) -> tuple[bpy.types.Object, bpy.types.Action]:
    _clear_scene()
    data = bpy.data.armatures.new(name + "_Data")
    armature = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(armature)
    armature["motion_bridge_skeleton_id"] = SKELETON_ID
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature

    bpy.ops.object.mode_set(mode="EDIT")
    hips = data.edit_bones.new("Hips")
    hips.head = (0.0, 0.0, 0.0)
    hips.tail = (0.0, 0.0, 1.0)
    arm = data.edit_bones.new("LeftUpperArm")
    arm.head = (0.0, 0.0, 0.8)
    arm.tail = (0.8, 0.0, 0.8)
    arm.parent = hips
    bpy.ops.object.mode_set(mode="POSE")

    for pose_bone in armature.pose.bones:
        pose_bone.rotation_mode = "QUATERNION"
        for frame in (FIRST_FRAME, LAST_FRAME):
            pose_bone.rotation_quaternion = Quaternion()
            pose_bone.location = Vector((0.0, 0.0, 0.0))
            pose_bone.keyframe_insert(
                data_path="rotation_quaternion",
                frame=frame,
                group=pose_bone.name,
            )
            pose_bone.keyframe_insert(
                data_path="location",
                frame=frame,
                group=pose_bone.name,
            )
    animation_data = armature.animation_data
    if animation_data is None or animation_data.action is None:
        raise AssertionError("Synthetic Base Action was not created")
    animation_data.action.name = name + "_Base"
    return armature, animation_data.action


def _select_bones(armature: bpy.types.Object, *bone_names: str) -> None:
    selected = frozenset(bone_names)
    for bone in armature.pose.bones:
        set_pose_bone_selected(bone, bone.name in selected)
    armature.data.bones.active = (
        armature.data.bones.get(bone_names[0]) if bone_names else None
    )


def _insert_pose(
    armature: bpy.types.Object,
    bone_name: str,
    frame: int,
    *,
    rotation: Quaternion,
    location: tuple[float, float, float],
) -> None:
    bpy.context.scene.frame_set(frame)
    _select_bones(armature, bone_name)
    bone = armature.pose.bones[bone_name]
    bone.rotation_quaternion = rotation
    bone.location = location
    names = insert_correction_pose_keys(
        bpy.context,
        armature,
        include_location=bone_name == "Hips",
        interpolation="LINEAR",
    )
    if names != (bone_name,):
        raise AssertionError(f"Unexpected keyed bones: {names!r}")


def _property_path(bone_name: str, property_name: str) -> str:
    escaped = bpy.utils.escape_identifier(bone_name)
    return f'pose.bones["{escaped}"].{property_name}'


def _select_action_keys(
    action: bpy.types.Action,
    selections: dict[tuple[str, str], frozenset[float]],
) -> None:
    paths = {
        _property_path(bone_name, property_name): frames
        for (bone_name, property_name), frames in selections.items()
    }
    for curve in iter_action_fcurves(action):
        selected_frames = paths.get(curve.data_path, frozenset())
        for point in curve.keyframe_points:
            selected = any(
                abs(float(point.co[0]) - frame) <= EPSILON for frame in selected_frames
            )
            point.select_control_point = selected
            point.select_left_handle = False
            point.select_right_handle = False


def _key_selection_signature(
    action: bpy.types.Action,
) -> tuple[tuple[str, int, float, bool, bool, bool], ...]:
    return tuple(
        sorted(
            (
                str(curve.data_path),
                int(curve.array_index),
                round(float(point.co[0]), 6),
                bool(point.select_control_point),
                bool(point.select_left_handle),
                bool(point.select_right_handle),
            )
            for curve in iter_action_fcurves(action)
            for point in curve.keyframe_points
        )
    )


def _property_curves(
    action: bpy.types.Action,
    bone_name: str,
    property_name: str,
    size: int,
) -> tuple[bpy.types.FCurve, ...] | None:
    path = _property_path(bone_name, property_name)
    curves = {
        int(curve.array_index): curve
        for curve in iter_action_fcurves(action)
        if curve.data_path == path and 0 <= int(curve.array_index) < size
    }
    if not curves:
        return None
    if set(curves) != set(range(size)):
        raise AssertionError(f"Incomplete {property_name} curves for {bone_name}")
    return tuple(curves[index] for index in range(size))


def _curve_frames(curve: bpy.types.FCurve) -> tuple[float, ...]:
    return tuple(round(float(point.co[0]), 6) for point in curve.keyframe_points)


def _key_values(
    action: bpy.types.Action,
    bone_name: str,
    property_name: str,
    size: int,
    frame: float,
) -> tuple[float, ...]:
    curves = _property_curves(action, bone_name, property_name, size)
    if curves is None:
        raise AssertionError(f"Missing {property_name} curves for {bone_name}")
    values: list[float] = []
    for curve in curves:
        matches = tuple(
            point
            for point in curve.keyframe_points
            if abs(float(point.co[0]) - frame) <= EPSILON
        )
        if len(matches) != 1:
            raise AssertionError(
                f"Expected one {property_name} key for {bone_name} at {frame:g}"
            )
        values.append(float(matches[0].co[1]))
    return tuple(values)


def _assert_close(actual: float, expected: float, label: str) -> None:
    if abs(actual - expected) > EPSILON:
        raise AssertionError(f"{label}: {actual!r} != {expected!r}")


def _assert_range(
    envelope_range: object,
    *,
    source: EnvelopeRangeSource,
    scene_start: float,
    scene_end: float,
    action_start: float,
    action_end: float,
) -> None:
    if envelope_range.source is not source:
        raise AssertionError(
            f"Unexpected Envelope range source: {envelope_range.source!r}"
        )
    _assert_close(float(envelope_range.scene_start), scene_start, "scene start")
    _assert_close(float(envelope_range.scene_end), scene_end, "scene end")
    _assert_close(float(envelope_range.action_start), action_start, "action start")
    _assert_close(float(envelope_range.action_end), action_end, "action end")


def _test_selected_range_and_location_guard() -> None:
    armature, base = _create_armature("MB_Envelope_Range")
    base_checksum = action_checksum(base)
    layer = create_correction_layer(bpy.context, armature).active_layer
    enter_correction_edit_mode(bpy.context, armature)

    hips_rotation = Quaternion((0.0, 0.0, 1.0), math.radians(15.0))
    arm_rotation = Quaternion((1.0, 0.0, 0.0), math.radians(20.0))
    for frame in (6, 14):
        _insert_pose(
            armature,
            "Hips",
            frame,
            rotation=hips_rotation,
            location=(0.1, 0.2, 0.3),
        )
    for frame in (2, 18):
        _insert_pose(
            armature,
            "LeftUpperArm",
            frame,
            rotation=arm_rotation,
            location=(0.4, 0.5, 0.6),
        )

    # Selected keys on selected bones win over the wider Preview Range.  Keys
    # belonging to another bone must not expand the envelope.
    _select_action_keys(
        layer.action,
        {
            ("Hips", "rotation_quaternion"): frozenset((6.0, 14.0)),
            ("LeftUpperArm", "rotation_quaternion"): frozenset((2.0, 18.0)),
        },
    )
    _select_bones(armature, "Hips")
    selected_range = resolve_envelope_range(
        bpy.context,
        armature,
        preview_scene_start=4.0,
        preview_scene_end=16.0,
    )
    _assert_range(
        selected_range,
        source=EnvelopeRangeSource.SELECTED_KEYS,
        scene_start=6.0,
        scene_end=14.0,
        action_start=6.0,
        action_end=14.0,
    )

    numeric_range = resolve_envelope_range(
        bpy.context,
        armature,
        preview_scene_start=4.0,
        preview_scene_end=16.0,
        prefer_preview_range=True,
    )
    _assert_range(
        numeric_range,
        source=EnvelopeRangeSource.PREVIEW_RANGE,
        scene_start=4.0,
        scene_end=16.0,
        action_start=4.0,
        action_end=16.0,
    )

    # With no relevant selected key, the Timeline Preview Range is the
    # deterministic fallback.
    _select_action_keys(layer.action, {})
    fallback_range = resolve_envelope_range(
        bpy.context,
        armature,
        preview_scene_start=4.0,
        preview_scene_end=16.0,
    )
    _assert_range(
        fallback_range,
        source=EnvelopeRangeSource.PREVIEW_RANGE,
        scene_start=4.0,
        scene_end=16.0,
        action_start=4.0,
        action_end=16.0,
    )

    # Location remains Hips-only even when a low-level/Advanced caller supplies
    # an explicit list. This catches accidental arm translation by API misuse.
    _select_action_keys(
        layer.action,
        {
            ("LeftUpperArm", "location"): frozenset((2.0, 18.0)),
        },
    )
    _select_bones(armature, "LeftUpperArm")
    correction_checksum = action_checksum(layer.action)
    try:
        resolve_envelope_range(
            bpy.context,
            armature,
            preview_scene_start=4.0,
            preview_scene_end=16.0,
            include_location=True,
        )
    except ValueError as error:
        if "Hips" not in str(error):
            raise
    else:
        raise AssertionError("Envelope silently ignored a non-Hips location request")
    try:
        resolve_envelope_range(
            bpy.context,
            armature,
            preview_scene_start=4.0,
            preview_scene_end=16.0,
            include_location=True,
            location_bone_names=("Hips", "LeftUpperArm"),
        )
    except ValueError as error:
        if "Hips" not in str(error):
            raise
    else:
        raise AssertionError("Envelope API accepted a non-Hips location target")
    if action_checksum(layer.action) != correction_checksum:
        raise AssertionError("Rejected Envelope location target mutated Correction")

    if action_checksum(base) != base_checksum:
        raise AssertionError("Envelope range resolution mutated the Base Action")
    validate_layer_stack(armature).raise_for_errors()
    exit_correction_edit_mode(bpy.context, armature)


def _test_presets() -> None:
    expected_presets = {
        "SMOOTH_PULSE",
        "PLATEAU",
        "EASE_IN",
        "EASE_OUT",
        "EASE_IN_OUT",
        "S_CURVE",
    }
    actual_presets = {preset.name for preset in EnvelopePreset}
    missing = expected_presets - actual_presets
    if missing:
        raise AssertionError(f"Missing Envelope presets: {sorted(missing)!r}")

    samples_by_preset: dict[EnvelopePreset, tuple[float, ...]] = {}
    for preset in EnvelopePreset:
        samples = tuple(
            float(evaluate_envelope_preset(preset, index / 100.0))
            for index in range(101)
        )
        if not all(math.isfinite(value) for value in samples):
            raise AssertionError(f"{preset.name} produced a non-finite weight")
        if min(samples) < -EPSILON or max(samples) > 1.0 + EPSILON:
            raise AssertionError(f"{preset.name} escaped the safe 0..1 range")
        samples_by_preset[preset] = samples

    pulse = samples_by_preset[EnvelopePreset.SMOOTH_PULSE]
    _assert_close(pulse[0], 0.0, "Smooth Pulse start")
    _assert_close(pulse[50], 1.0, "Smooth Pulse peak")
    _assert_close(pulse[-1], 0.0, "Smooth Pulse end")
    for index in range(101):
        _assert_close(
            pulse[index],
            pulse[100 - index],
            "Smooth Pulse symmetry",
        )

    plateau = samples_by_preset[EnvelopePreset.PLATEAU]
    _assert_close(plateau[0], 0.0, "Plateau start")
    _assert_close(plateau[50], 1.0, "Plateau center")
    _assert_close(plateau[-1], 0.0, "Plateau end")
    if sum(value >= 1.0 - EPSILON for value in plateau) < 20:
        raise AssertionError("Plateau has no useful full-strength hold")

    monotonic = (
        EnvelopePreset.EASE_IN,
        EnvelopePreset.EASE_OUT,
        EnvelopePreset.EASE_IN_OUT,
        EnvelopePreset.S_CURVE,
    )
    for preset in monotonic:
        samples = samples_by_preset[preset]
        _assert_close(samples[0], 0.0, f"{preset.name} start")
        _assert_close(samples[-1], 1.0, f"{preset.name} end")
        if any(right + EPSILON < left for left, right in zip(samples, samples[1:])):
            raise AssertionError(f"{preset.name} is not monotonic")
    if not (
        samples_by_preset[EnvelopePreset.EASE_IN][25]
        < samples_by_preset[EnvelopePreset.EASE_OUT][25]
    ):
        raise AssertionError("Ease In and Ease Out have indistinguishable timing")


def _test_weighted_deltas() -> None:
    axis = Vector((0.25, 0.8, -0.35)).normalized()
    delta = Quaternion(axis, math.radians(170.0)).normalized()
    previous: Quaternion | None = None
    for index in range(17):
        weight = index / 16.0
        weighted = weighted_quaternion_delta(delta, weight)
        _assert_close(weighted.magnitude, 1.0, "Weighted Quaternion norm")
        expected = Quaternion(axis, math.radians(170.0) * weight)
        error_degrees = math.degrees(weighted.rotation_difference(expected).angle)
        if error_degrees > 0.001:
            raise AssertionError(
                f"Quaternion Log/Exp weight error at {weight:g}: "
                f"{error_degrees:g} degrees"
            )
        if previous is not None and previous.dot(weighted) < 0.0:
            raise AssertionError("Weighted Quaternion changed hemisphere")
        previous = weighted

    # q and -q represent the same rotation.  Weighting either encoding must
    # choose the same shortest rotational delta instead of taking a long arc.
    negated = Quaternion(tuple(-component for component in delta))
    positive_half = weighted_quaternion_delta(delta, 0.5)
    negative_half = weighted_quaternion_delta(negated, 0.5)
    if math.degrees(positive_half.rotation_difference(negative_half).angle) > 0.001:
        raise AssertionError("Quaternion sign changed the weighted rotation")

    location = Vector((1.25, -0.5, 0.75))
    for weight in (0.0, 0.25, 0.5, 1.0):
        weighted = weighted_location_delta(location, weight)
        expected = location * weight
        if (weighted - expected).length > EPSILON:
            raise AssertionError(
                f"Location delta is not linear at weight {weight:g}: {weighted!r}"
            )


def _test_adaptive_sampling_budget() -> None:
    constant = adaptive_envelope_samples(lambda _time: 0.5)
    if constant != ((0.0, 0.5), (1.0, 0.5)):
        raise AssertionError(f"Flat Envelope was oversampled: {constant!r}")

    def pulse_evaluator(time: float) -> float:
        return evaluate_envelope_preset(
            EnvelopePreset.SMOOTH_PULSE,
            time,
        )

    large_delta = Quaternion((0.0, 1.0, 0.0), math.radians(170.0))
    samples = adaptive_envelope_samples(
        pulse_evaluator,
        tolerance=0.002,
        rotation_deltas=(large_delta,),
        rotation_tolerance_degrees=0.1,
        max_samples=512,
    )
    if len(samples) <= 2 or len(samples) > 512:
        raise AssertionError(f"Unexpected adaptive sample count: {len(samples)}")
    if samples[0][0] != 0.0 or samples[-1][0] != 1.0:
        raise AssertionError("Adaptive sampling omitted an Envelope endpoint")

    def interpolate(time: float) -> float:
        for (left_time, left_value), (right_time, right_value) in zip(
            samples,
            samples[1:],
        ):
            if time <= right_time + EPSILON:
                factor = (time - left_time) / (right_time - left_time)
                return left_value + ((right_value - left_value) * factor)
        return samples[-1][1]

    maximum_weight_error = max(
        abs(pulse_evaluator(index / 1000.0) - interpolate(index / 1000.0))
        for index in range(1001)
    )
    if maximum_weight_error > 0.0025:
        raise AssertionError(
            f"Adaptive Envelope error exceeded tolerance: {maximum_weight_error:g}"
        )

    # CurveMapping control-point times are mandatory seeds so a narrow custom
    # feature cannot disappear between the generic quarter probes.
    def custom_curve(time: float) -> float:
        return time

    custom_curve.sample_times = (0.37, 0.63)  # type: ignore[attr-defined]
    seeded = adaptive_envelope_samples(custom_curve, max_samples=16)
    seeded_times = {round(time, 6) for time, _weight in seeded}
    if not {0.37, 0.63}.issubset(seeded_times):
        raise AssertionError("Adaptive sampling omitted CurveMapping control points")


def _test_pose_preview_lifecycle() -> None:
    armature, base = _create_armature("MB_Envelope_Preview")
    base_checksum = action_checksum(base)
    layer = create_correction_layer(bpy.context, armature).active_layer
    enter_correction_edit_mode(bpy.context, armature)

    outside_rotation = Quaternion((0.0, 0.0, 1.0), math.radians(9.0))
    for frame, sign in ((3, 1.0), (17, -1.0)):
        _insert_pose(
            armature,
            "Hips",
            frame,
            rotation=outside_rotation,
            location=(0.05 * sign, -0.1 * sign, 0.15 * sign),
        )
    _select_action_keys(
        layer.action,
        {
            ("Hips", "rotation_quaternion"): frozenset((3.0,)),
            ("Hips", "location"): frozenset((17.0,)),
        },
    )
    baseline_checksum = action_checksum(layer.action)
    baseline_selection = _key_selection_signature(layer.action)

    bpy.context.scene.frame_set(10, subframe=0.25)
    _select_bones(armature, "Hips", "LeftUpperArm")
    hips = armature.pose.bones["Hips"]
    arm = armature.pose.bones["LeftUpperArm"]
    hips_target = Quaternion((0.2, 0.8, -0.3), math.radians(80.0)).normalized()
    arm_target = Quaternion((0.7, 0.1, 0.4), math.radians(40.0)).normalized()
    hips_location = (0.4, -0.2, 0.6)
    hips.rotation_quaternion = hips_target
    hips.location = hips_location
    arm.rotation_quaternion = arm_target
    arm.location = (9.0, 8.0, 7.0)

    envelope_range = EnvelopeRange(
        6.0,
        14.0,
        6.0,
        14.0,
        EnvelopeRangeSource.PREVIEW_RANGE,
    )

    def _fail_initial_evaluation(_time: float) -> float:
        raise RuntimeError("Injected initial evaluator failure")

    try:
        begin_pose_envelope_preview(
            bpy.context,
            armature,
            envelope_range,
            _fail_initial_evaluation,
            include_location=True,
            interpolation="LINEAR",
            sample_step=2.0,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Initial Envelope evaluator failure did not propagate")
    if has_correction_envelope_preview(armature):
        raise AssertionError("Failed initial preview leaked an active session")
    if action_checksum(layer.action) != baseline_checksum:
        raise AssertionError("Failed initial preview changed the Correction Action")
    if _key_selection_signature(layer.action) != baseline_selection:
        raise AssertionError("Failed initial preview changed key selection")

    def quarter_weight(_time: float) -> float:
        return 0.25

    preview = begin_pose_envelope_preview(
        bpy.context,
        armature,
        envelope_range,
        quarter_weight,
        include_location=True,
        interpolation="LINEAR",
        sample_step=2.0,
    )
    if not preview.active or not has_correction_envelope_preview(armature):
        raise AssertionError("Pose Envelope preview was not registered as active")
    if preview.location_bone_names != ("Hips",):
        raise AssertionError(
            f"Unsafe default location targets: {preview.location_bone_names!r}"
        )
    if action_checksum(layer.action) == baseline_checksum:
        raise AssertionError("Pose Envelope preview did not write temporary keys")
    first_preview_checksum = action_checksum(layer.action)

    # Production live streaming listens to depsgraph updates.  An Envelope
    # redraw must dirty the scheduler so Unity receives the edited pose without
    # requiring a frame change.
    import motion_bridge.addon as addon_module

    class _DirtyScheduler:
        def __init__(self) -> None:
            self.count = 0

        def mark_dirty(self) -> None:
            self.count += 1

    dirty_scheduler = _DirtyScheduler()
    previous_scheduler = addon_module._live_scheduler
    handlers_were_installed = all(
        callback in collection
        for collection, callback in addon_module._LIVE_DIRTY_HANDLERS
    )
    addon_module._live_scheduler = dirty_scheduler
    addon_module._install_live_handlers()
    try:
        dirty_before = dirty_scheduler.count
        preview.update(quarter_weight)
        if dirty_scheduler.count <= dirty_before:
            raise AssertionError("Envelope update did not dirty Unity live streaming")
    finally:
        addon_module._live_scheduler = previous_scheduler
        if not handlers_were_installed:
            addon_module._remove_live_handlers()

    # CurveMapping redraw/update events can fire repeatedly with no actual
    # point change.  Rendering the same evaluator must be deterministic and
    # must not multiply the already-weighted rotation again.
    preview.update(quarter_weight)
    if action_checksum(layer.action) != first_preview_checksum:
        raise AssertionError("Repeated Envelope update accumulated correction error")

    preview.update(lambda _time: 0.5)
    half_preview_checksum = action_checksum(layer.action)
    if half_preview_checksum == first_preview_checksum:
        raise AssertionError("Envelope update ignored a changed curve evaluator")

    # A mid-write Blender failure must remove all partial samples and restore
    # the immutable pre-preview baseline.  The still-active session can then
    # be cancelled cleanly.
    original_insert_quaternion = envelope_module.insert_quaternion_key
    insertion_count = 0

    def _fail_second_quaternion(*args, **kwargs) -> None:
        nonlocal insertion_count
        insertion_count += 1
        if insertion_count == 2:
            raise RuntimeError("Injected Envelope writer failure")
        original_insert_quaternion(*args, **kwargs)

    envelope_module.insert_quaternion_key = _fail_second_quaternion
    try:
        try:
            preview.update(lambda _time: 0.75)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Injected Envelope writer failure did not propagate")
    finally:
        envelope_module.insert_quaternion_key = original_insert_quaternion
    if action_checksum(layer.action) != baseline_checksum:
        raise AssertionError("Failed Envelope update left partial preview keys")
    if _key_selection_signature(layer.action) != baseline_selection:
        raise AssertionError("Failed Envelope update changed key selection")

    preview.cancel()
    if preview.active or has_correction_envelope_preview(armature):
        raise AssertionError("Cancelled Envelope preview remained active")
    if action_checksum(layer.action) != baseline_checksum:
        raise AssertionError("Cancel did not restore the Correction Action exactly")
    if _key_selection_signature(layer.action) != baseline_selection:
        raise AssertionError("Cancel did not restore key selection exactly")
    if (
        int(bpy.context.scene.frame_current) != 10
        or abs(float(bpy.context.scene.frame_subframe) - 0.25) > EPSILON
    ):
        raise AssertionError("Envelope preview changed the current frame/subframe")

    # Apply commits sampled complete properties.  Location remains Hips-only,
    # while rotation applies to every selected bone.
    _select_bones(armature, "Hips", "LeftUpperArm")
    hips.rotation_quaternion = hips_target
    hips.location = hips_location
    arm.rotation_quaternion = arm_target
    arm.location = (9.0, 8.0, 7.0)
    applied = begin_pose_envelope_preview(
        bpy.context,
        armature,
        envelope_range,
        lambda normalized_time: evaluate_envelope_preset(
            EnvelopePreset.SMOOTH_PULSE,
            normalized_time,
        ),
        include_location=True,
        interpolation="LINEAR",
        sample_step=2.0,
    )
    ui_metadata = {
        "preset": "SMOOTH_PULSE",
        "curvePoints": [[0.0, 0.0], [0.5, 1.0], [1.0, 0.0]],
    }
    if applied.apply(ui_metadata) != ("Hips", "LeftUpperArm"):
        raise AssertionError("Envelope Apply returned unexpected bones")
    if applied.active or has_correction_envelope_preview(armature):
        raise AssertionError("Applied Envelope preview remained active")
    if int(layer.action.get("motion_bridge_envelope_schema", 0)) != 1:
        raise AssertionError("Envelope Apply omitted versioned metadata")
    encoded_metadata = layer.action.get("motion_bridge_envelope_metadata")
    if not isinstance(encoded_metadata, str):
        raise AssertionError("Envelope Apply omitted JSON metadata")
    stored_metadata = json.loads(encoded_metadata)
    if stored_metadata != applied.metadata:
        raise AssertionError("Envelope session and Action metadata disagree")
    if (
        stored_metadata.get("schema") != 1
        or stored_metadata.get("schemaId") != "motion-bridge.correction-envelope/1"
        or stored_metadata.get("kind") != "POSE_OFFSET"
        or stored_metadata.get("bones") != ["Hips", "LeftUpperArm"]
        or stored_metadata.get("locationBones") != ["Hips"]
        or stored_metadata.get("ui") != ui_metadata
    ):
        raise AssertionError(f"Incomplete Envelope metadata: {stored_metadata!r}")
    if stored_metadata.get("range") != {
        "scene": [6.0, 14.0],
        "action": [6.0, 14.0],
        "source": "PREVIEW_RANGE",
    }:
        raise AssertionError("Envelope metadata did not preserve its source range")
    if len(stored_metadata.get("weightSamples", ())) != 5:
        raise AssertionError("Envelope metadata omitted the applied weight samples")
    if (
        not committed_pose_envelope_available(layer.action)
        or stored_metadata.get("actionChecksum") != action_checksum(layer.action)
        or len(stored_metadata.get("sourcePose", ())) != 2
    ):
        raise AssertionError("Envelope metadata omitted the reusable source pose")

    expected_sample_frames = (6.0, 8.0, 10.0, 12.0, 14.0)
    for bone_name in ("Hips", "LeftUpperArm"):
        curves = _property_curves(
            layer.action,
            bone_name,
            "rotation_quaternion",
            4,
        )
        if curves is None:
            raise AssertionError(f"Apply omitted rotation for {bone_name}")
        for curve in curves:
            frames = _curve_frames(curve)
            if not all(frame in frames for frame in expected_sample_frames):
                raise AssertionError(
                    f"Apply omitted sampled rotation frames for {bone_name}: {frames!r}"
                )
        previous: Quaternion | None = None
        for frame in expected_sample_frames:
            rotation = Quaternion(
                _key_values(
                    layer.action,
                    bone_name,
                    "rotation_quaternion",
                    4,
                    frame,
                )
            )
            _assert_close(rotation.magnitude, 1.0, f"{bone_name} key norm")
            if previous is not None and previous.dot(rotation) < 0.0:
                raise AssertionError(f"{bone_name} sampled rotation flipped hemisphere")
            previous = rotation

    hips_location_curves = _property_curves(
        layer.action,
        "Hips",
        "location",
        3,
    )
    if hips_location_curves is None:
        raise AssertionError("Apply omitted Hips location")
    for curve in hips_location_curves:
        if not all(frame in _curve_frames(curve) for frame in expected_sample_frames):
            raise AssertionError("Apply omitted sampled Hips location frames")
    if (
        _property_curves(
            layer.action,
            "LeftUpperArm",
            "location",
            3,
        )
        is not None
    ):
        raise AssertionError("Apply created unsafe non-Hips location keys")

    for bone_name in ("Hips", "LeftUpperArm"):
        for frame in (6.0, 14.0):
            _assert_close(
                Quaternion(
                    _key_values(
                        layer.action,
                        bone_name,
                        "rotation_quaternion",
                        4,
                        frame,
                    )
                )
                .rotation_difference(Quaternion())
                .angle,
                0.0,
                f"{bone_name} Smooth Pulse endpoint",
            )
    center_hips = Quaternion(
        _key_values(
            layer.action,
            "Hips",
            "rotation_quaternion",
            4,
            10.0,
        )
    ).normalized()
    if math.degrees(center_hips.rotation_difference(hips_target).angle) > 0.01:
        raise AssertionError("Smooth Pulse center did not fully apply the pose")
    center_location = _key_values(
        layer.action,
        "Hips",
        "location",
        3,
        10.0,
    )
    if any(
        abs(actual - expected) > EPSILON
        for actual, expected in zip(center_location, hips_location, strict=True)
    ):
        raise AssertionError(
            f"Smooth Pulse center location mismatch: {center_location!r}"
        )

    if action_checksum(base) != base_checksum:
        raise AssertionError("Envelope preview/apply mutated the Base Action")
    committed_checksum = action_checksum(layer.action)
    exit_correction_edit_mode(bpy.context, armature)
    enter_correction_edit_mode(bpy.context, armature)
    _select_bones(armature, "Hips", "LeftUpperArm")
    reopened = begin_committed_pose_envelope_preview(
        bpy.context,
        armature,
        lambda _time: 1.0,
    )
    if action_checksum(layer.action) == committed_checksum:
        raise AssertionError("Reopened curve did not rebuild committed keys")
    reopened.cancel()
    if action_checksum(layer.action) != committed_checksum:
        raise AssertionError("Cancelling curve re-edit did not restore committed keys")
    reopened = begin_committed_pose_envelope_preview(
        bpy.context,
        armature,
        lambda _time: 1.0,
    )
    reopened.apply()
    if action_checksum(layer.action) == committed_checksum:
        raise AssertionError("Re-edited curve was not committed")
    edge_rotation = Quaternion(
        _key_values(layer.action, "Hips", "rotation_quaternion", 4, 6.0)
    )
    if edge_rotation.rotation_difference(Quaternion()).angle < math.radians(1.0):
        raise AssertionError("Re-edit could not restore a zero-weight endpoint")
    validate_layer_stack(armature).raise_for_errors()
    exit_correction_edit_mode(bpy.context, armature)


def _test_reweight_existing_lifecycle() -> None:
    armature, base = _create_armature("MB_Envelope_Reweight")
    base_checksum = action_checksum(base)
    layer = create_correction_layer(bpy.context, armature).active_layer
    enter_correction_edit_mode(bpy.context, armature)
    original_rotation = Quaternion((0.1, 0.9, -0.2), math.radians(60.0)).normalized()
    original_location = (0.6, -0.3, 0.9)
    for frame in (5, 10, 15):
        _insert_pose(
            armature,
            "Hips",
            frame,
            rotation=original_rotation,
            location=original_location,
        )
    _select_bones(armature, "Hips")
    _select_action_keys(
        layer.action,
        {
            ("Hips", "rotation_quaternion"): frozenset((5.0, 10.0, 15.0)),
            ("Hips", "location"): frozenset((5.0, 10.0, 15.0)),
        },
    )
    envelope_range = resolve_envelope_range(
        bpy.context,
        armature,
        preview_scene_start=3.0,
        preview_scene_end=17.0,
        include_location=True,
    )
    _assert_range(
        envelope_range,
        source=EnvelopeRangeSource.SELECTED_KEYS,
        scene_start=5.0,
        scene_end=15.0,
        action_start=5.0,
        action_end=15.0,
    )
    baseline_checksum = action_checksum(layer.action)
    baseline_selection = _key_selection_signature(layer.action)

    preview = begin_reweight_envelope_preview(
        bpy.context,
        armature,
        envelope_range,
        lambda _time: 0.5,
        include_location=True,
    )
    if preview.kind != "REWEIGHT_EXISTING":
        raise AssertionError(f"Unexpected reweight preview kind: {preview.kind!r}")
    for frame in (5.0, 10.0, 15.0):
        rotation = Quaternion(
            _key_values(
                layer.action,
                "Hips",
                "rotation_quaternion",
                4,
                frame,
            )
        ).normalized()
        expected_rotation = weighted_quaternion_delta(original_rotation, 0.5)
        if math.degrees(rotation.rotation_difference(expected_rotation).angle) > 0.001:
            raise AssertionError(f"Reweight rotation mismatch at frame {frame:g}")
        location = _key_values(
            layer.action,
            "Hips",
            "location",
            3,
            frame,
        )
        expected_location = tuple(value * 0.5 for value in original_location)
        if any(
            abs(actual - expected) > EPSILON
            for actual, expected in zip(location, expected_location, strict=True)
        ):
            raise AssertionError(f"Reweight location mismatch at frame {frame:g}")
    preview.cancel()
    if action_checksum(layer.action) != baseline_checksum:
        raise AssertionError("Reweight Cancel did not restore key values/handles")
    if _key_selection_signature(layer.action) != baseline_selection:
        raise AssertionError("Reweight Cancel did not restore key selection")

    applied = begin_reweight_envelope_preview(
        bpy.context,
        armature,
        envelope_range,
        lambda _time: 0.5,
        include_location=True,
    )
    applied.apply({"preset": "CUSTOM_HALF"})
    stored_metadata = json.loads(str(layer.action["motion_bridge_envelope_metadata"]))
    if stored_metadata.get("kind") != "REWEIGHT_EXISTING" or stored_metadata.get(
        "ui"
    ) != {"preset": "CUSTOM_HALF"}:
        raise AssertionError("Reweight Apply metadata cannot drive future reopening")
    if action_checksum(layer.action) == baseline_checksum:
        raise AssertionError("Reweight Apply did not commit its changed keys")
    if action_checksum(base) != base_checksum:
        raise AssertionError("Reweight Existing mutated the Base Action")
    validate_layer_stack(armature).raise_for_errors()
    exit_correction_edit_mode(bpy.context, armature)


def _test_curve_mapping_registration() -> None:
    import motion_bridge.addon as addon_module

    addon_module.register()
    try:
        settings = bpy.context.scene.motion_bridge_session
        expected_point_counts = {
            "SMOOTH_PULSE": 3,
            "PLATEAU": 4,
            "EASE_IN": 3,
            "EASE_OUT": 3,
            "EASE_IN_OUT": 4,
            "S_CURVE": 5,
        }
        for preset, expected_count in expected_point_counts.items():
            settings.correction_envelope_preset = preset
            node = addon_module._ensure_envelope_curve_node(settings)
            if node.bl_idname != "ShaderNodeFloatCurve":
                raise AssertionError("Envelope curve is not a ShaderNodeFloatCurve")
            points = node.mapping.curves[0].points
            if len(points) != expected_count:
                raise AssertionError(
                    f"{preset} CurveMapping point count: "
                    f"{len(points)} != {expected_count}"
                )
            evaluator = addon_module._envelope_curve_evaluator(node)
            values = tuple(evaluator(index / 20.0) for index in range(21))
            if not all(math.isfinite(value) for value in values):
                raise AssertionError(f"{preset} CurveMapping evaluated non-finite")
            expected_seeds = {round(float(point.location[0]), 6) for point in points}
            actual_seeds = {round(float(value), 6) for value in evaluator.sample_times}
            if actual_seeds != expected_seeds:
                raise AssertionError(f"{preset} control-point sampling seeds differ")
        payload = json.loads(addon_module._envelope_curve_metadata(settings, node))
        if (
            payload.get("schema") != 1
            or payload.get("preset") != "S_CURVE"
            or len(payload.get("points", ())) != 5
        ):
            raise AssertionError("CurveMapping metadata is incomplete")
    finally:
        addon_module.unregister()


def main() -> None:
    _test_selected_range_and_location_guard()
    _test_presets()
    _test_weighted_deltas()
    _test_adaptive_sampling_budget()
    _test_pose_preview_lifecycle()
    _test_reweight_existing_lifecycle()
    _test_curve_mapping_registration()
    print(
        "MOTION_BRIDGE_CORRECTION_ENVELOPE_ACCEPTANCE_TEST="
        + json.dumps(
            {
                "status": "PASS",
                "selectedKeyRangeWins": True,
                "previewRangeFallback": True,
                "hipsLocationDefault": True,
                "presetCoverage": True,
                "quaternionLogExpSafe": True,
                "locationLinear": True,
                "adaptiveSamplingBounded": True,
                "baseImmutable": True,
                "previewUpdateNonAccumulating": True,
                "previewFailureTransactional": True,
                "unityLiveDirty": True,
                "reweightExisting": True,
                "curveMappingRegistered": True,
                "cancelExact": True,
                "applyCompleteChannels": True,
                "applyMetadataVersioned": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
