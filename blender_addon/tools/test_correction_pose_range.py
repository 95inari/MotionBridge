from __future__ import annotations

import json
import math
from pathlib import Path
import sys

import bpy
from mathutils import Quaternion


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))

from motion_bridge.canonical.rig_spec import SKELETON_ID  # noqa: E402
import motion_bridge.editing.correction_edit as correction_edit_module  # noqa: E402
from motion_bridge.editing.action_api import (  # noqa: E402
    ensure_fcurve,
    insert_location_key,
    iter_action_fcurves,
    remove_fcurve,
)
from motion_bridge.editing.pose_api import set_pose_bone_selected  # noqa: E402
from motion_bridge.editing.correction_edit import (  # noqa: E402
    apply_correction_pose_range,
    enter_correction_edit_mode,
    exit_correction_edit_mode,
    insert_correction_pose_keys,
)
from motion_bridge.editing.correction_layers import (  # noqa: E402
    action_checksum,
    create_correction_layer,
    validate_layer_stack,
)


FIRST_FRAME = 1
LAST_FRAME = 20
# Blender's inverse NLA quaternion remap is float32 internally.
EPSILON = 1.0e-5


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for action in tuple(bpy.data.actions):
        bpy.data.actions.remove(action)


def _create_armature() -> bpy.types.Object:
    _clear_scene()
    data = bpy.data.armatures.new("MB_Correction_Range_Data")
    armature = bpy.data.objects.new("MB_Correction_Range", data)
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
            pose_bone.location = (0.0, 0.0, 0.0)
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
    base = armature.animation_data.action if armature.animation_data else None
    if base is None:
        raise AssertionError("Synthetic Base Action was not created")
    base.name = "MB_Correction_Range_Base"
    return armature


def _select_only(armature: bpy.types.Object, bone_name: str) -> None:
    for bone in armature.pose.bones:
        set_pose_bone_selected(bone, bone.name == bone_name)
    armature.data.bones.active = armature.data.bones[bone_name]


def _insert_pose(
    armature: bpy.types.Object,
    bone_name: str,
    frame: int,
    rotation: Quaternion,
    location: tuple[float, float, float],
) -> None:
    bpy.context.scene.frame_set(frame)
    _select_only(armature, bone_name)
    bone = armature.pose.bones[bone_name]
    bone.rotation_quaternion = rotation
    bone.location = location
    insert_correction_pose_keys(
        bpy.context,
        armature,
        include_location=bone_name == "Hips",
        interpolation="LINEAR",
    )


def _curve_map(action: bpy.types.Action) -> dict[tuple[str, int], bpy.types.FCurve]:
    return {
        (curve.data_path, int(curve.array_index)): curve
        for curve in iter_action_fcurves(action)
    }


def _point(curve: bpy.types.FCurve, frame: float) -> bpy.types.Keyframe:
    matches = [
        point
        for point in curve.keyframe_points
        if abs(float(point.co[0]) - frame) <= EPSILON
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"Expected one key at {frame} on "
            f"{curve.data_path}[{curve.array_index}], got {len(matches)}"
        )
    return matches[0]


def _frames(curve: bpy.types.FCurve) -> tuple[float, ...]:
    return tuple(round(float(point.co[0]), 6) for point in curve.keyframe_points)


def _assert_property_frames(
    curves: dict[tuple[str, int], bpy.types.FCurve],
    data_path: str,
    size: int,
    expected: tuple[float, ...],
) -> None:
    for index in range(size):
        actual = _frames(curves[(data_path, index)])
        if actual != expected:
            raise AssertionError(
                f"Unexpected frames on {data_path}[{index}]: {actual!r} != {expected!r}"
            )


def _assert_values(
    curves: dict[tuple[str, int], bpy.types.FCurve],
    data_path: str,
    frame: float,
    expected: tuple[float, ...],
) -> None:
    actual = tuple(
        float(_point(curves[(data_path, index)], frame).co[1])
        for index in range(len(expected))
    )
    if any(
        abs(value - wanted) > EPSILON
        for value, wanted in zip(actual, expected, strict=True)
    ):
        raise AssertionError(
            f"Unexpected values at {frame} on {data_path}: {actual!r} != {expected!r}"
        )


def _assert_bezier_auto_clamped(
    curves: dict[tuple[str, int], bpy.types.FCurve],
    data_path: str,
    size: int,
    frames: tuple[float, ...],
) -> None:
    for index in range(size):
        for frame in frames:
            point = _point(curves[(data_path, index)], frame)
            if point.interpolation != "BEZIER":
                raise AssertionError(
                    f"Expected BEZIER at {frame} on {data_path}[{index}]"
                )
            if (
                point.handle_left_type != "AUTO_CLAMPED"
                or point.handle_right_type != "AUTO_CLAMPED"
            ):
                raise AssertionError(
                    f"Expected AUTO_CLAMPED handles at {frame} on {data_path}[{index}]"
                )


def _expect_value_error(callback, label: str) -> None:
    try:
        callback()
    except ValueError:
        return
    raise AssertionError(f"{label} did not raise ValueError")


def main() -> None:
    armature = _create_armature()
    animation_data = armature.animation_data
    if animation_data is None or animation_data.action is None:
        raise AssertionError("Fixture has no Base Action")
    base = animation_data.action
    base_checksum = action_checksum(base)
    stack = create_correction_layer(bpy.context, armature)
    layer = stack.active_layer

    enter_correction_edit_mode(bpy.context, armature)

    outside_before_rotation = Quaternion(
        (0.0, 0.0, 1.0), math.radians(9.0)
    ).normalized()
    outside_before_location = (0.04, -0.08, 0.12)
    replaced_rotation = Quaternion((0.0, 1.0, 0.0), math.radians(19.0)).normalized()
    replaced_location = (0.1, 0.2, -0.3)
    outside_after_rotation = Quaternion(
        (0.0, 0.0, 1.0), math.radians(-11.0)
    ).normalized()
    outside_after_location = (0.17, -0.34, 0.51)
    unselected_rotation = Quaternion((1.0, 0.0, 0.0), math.radians(23.0)).normalized()
    unselected_location = (-0.2, 0.3, 0.4)

    _insert_pose(
        armature,
        "Hips",
        4,
        outside_before_rotation,
        outside_before_location,
    )
    _insert_pose(
        armature,
        "Hips",
        10,
        replaced_rotation,
        replaced_location,
    )
    _insert_pose(
        armature,
        "Hips",
        17,
        outside_after_rotation,
        outside_after_location,
    )
    _insert_pose(
        armature,
        "LeftUpperArm",
        10,
        unselected_rotation,
        unselected_location,
    )

    target_rotation = Quaternion((0.25, 0.8, -0.35), math.radians(47.0)).normalized()
    target_location = (0.25, -0.5, 0.75)
    # Force a fresh evaluation at the capture frame after creating the
    # unselected bone's key, then override only the selected Correction pose.
    bpy.context.scene.frame_set(9)
    bpy.context.scene.frame_set(10)
    _select_only(armature, "Hips")
    hips = armature.pose.bones["Hips"]
    hips.rotation_quaternion = target_rotation
    hips.location = target_location

    names = apply_correction_pose_range(
        bpy.context,
        armature,
        scene_frame_start=8.0,
        scene_frame_end=12.0,
        ease_in_frames=2.0,
        ease_out_frames=3.0,
        include_location=True,
        interpolation="BEZIER",
        replace_inner_keys=True,
    )
    if names != ("Hips",):
        raise AssertionError(f"Range edit returned unexpected bones: {names!r}")

    rotation_path = 'pose.bones["Hips"].rotation_quaternion'
    location_path = 'pose.bones["Hips"].location'
    arm_rotation_path = 'pose.bones["LeftUpperArm"].rotation_quaternion'
    curves = _curve_map(layer.action)
    expected_hips_frames = (4.0, 6.0, 8.0, 12.0, 15.0, 17.0)
    _assert_property_frames(curves, rotation_path, 4, expected_hips_frames)
    _assert_property_frames(curves, location_path, 3, expected_hips_frames)
    _assert_property_frames(curves, arm_rotation_path, 4, (10.0,))

    neutral = (1.0, 0.0, 0.0, 0.0)
    zero = (0.0, 0.0, 0.0)
    for frame in (6.0, 15.0):
        _assert_values(curves, rotation_path, frame, neutral)
        _assert_values(curves, location_path, frame, zero)
    for frame in (8.0, 12.0):
        _assert_values(curves, rotation_path, frame, tuple(target_rotation))
        _assert_values(curves, location_path, frame, target_location)
    _assert_values(
        curves,
        rotation_path,
        4.0,
        tuple(outside_before_rotation),
    )
    _assert_values(curves, location_path, 4.0, outside_before_location)
    _assert_values(
        curves,
        rotation_path,
        17.0,
        tuple(outside_after_rotation),
    )
    _assert_values(curves, location_path, 17.0, outside_after_location)
    _assert_values(curves, arm_rotation_path, 10.0, tuple(unselected_rotation))
    _assert_bezier_auto_clamped(
        curves,
        rotation_path,
        4,
        (6.0, 8.0, 12.0, 15.0),
    )
    _assert_bezier_auto_clamped(
        curves,
        location_path,
        3,
        (6.0, 8.0, 12.0, 15.0),
    )

    # A second numeric-range apply removes several existing points per FCurve.
    # Blender invalidates point wrappers as the collection shrinks.
    bpy.context.scene.frame_set(10)
    _select_only(armature, "Hips")
    hips.rotation_quaternion = target_rotation
    hips.location = target_location
    apply_correction_pose_range(
        bpy.context,
        armature,
        scene_frame_start=8.0,
        scene_frame_end=12.0,
        ease_in_frames=2.0,
        ease_out_frames=3.0,
        include_location=True,
        interpolation="BEZIER",
        replace_inner_keys=True,
    )
    validate_layer_stack(armature).raise_for_errors()

    # Capturing through Blender's NLA keying context is temporary for range
    # application; keeping inner keys must not leak a key at the current frame.
    preserved_inner_rotation = Quaternion(
        (1.0, 0.0, 0.0),
        math.radians(7.0),
    ).normalized()
    preserved_inner_location = (0.03, -0.02, 0.01)
    for frame in (9, 11):
        _insert_pose(
            armature,
            "Hips",
            frame,
            preserved_inner_rotation,
            preserved_inner_location,
        )
    bpy.context.scene.frame_set(10)
    _select_only(armature, "Hips")
    hips.rotation_quaternion = target_rotation
    hips.location = target_location
    apply_correction_pose_range(
        bpy.context,
        armature,
        scene_frame_start=8.0,
        scene_frame_end=12.0,
        ease_in_frames=2.0,
        ease_out_frames=3.0,
        include_location=True,
        interpolation="BEZIER",
        replace_inner_keys=False,
    )
    curves = _curve_map(layer.action)
    for data_path, size in ((rotation_path, 4), (location_path, 3)):
        for index in range(size):
            if 10.0 in _frames(curves[(data_path, index)]):
                raise AssertionError(
                    f"Temporary NLA capture key leaked into {data_path}[{index}]"
                )
    for frame in (9.0, 11.0):
        _assert_values(
            curves,
            rotation_path,
            frame,
            tuple(preserved_inner_rotation),
        )
        _assert_values(
            curves,
            location_path,
            frame,
            preserved_inner_location,
        )

    correction_checksum = action_checksum(layer.action)
    bpy.context.scene.frame_set(10)
    _expect_value_error(
        lambda: apply_correction_pose_range(
            bpy.context,
            armature,
            scene_frame_start=12.0,
            scene_frame_end=8.0,
            include_location=True,
        ),
        "Reversed range",
    )
    bpy.context.scene.frame_set(5)
    _expect_value_error(
        lambda: apply_correction_pose_range(
            bpy.context,
            armature,
            scene_frame_start=8.0,
            scene_frame_end=12.0,
            include_location=True,
        ),
        "Current frame outside range",
    )
    bpy.context.scene.frame_set(10)
    _expect_value_error(
        lambda: apply_correction_pose_range(
            bpy.context,
            armature,
            scene_frame_start=8.0,
            scene_frame_end=12.0,
            include_location=True,
            interpolation="CUBIC_SPARKLES",
        ),
        "Invalid interpolation",
    )

    original_insert_quaternion = correction_edit_module.insert_quaternion_key
    quaternion_insert_calls = 0

    def _fail_after_range_delete(*args, **kwargs) -> None:
        nonlocal quaternion_insert_calls
        quaternion_insert_calls += 1
        if quaternion_insert_calls == 2:
            raise RuntimeError("Injected range writer failure")
        original_insert_quaternion(*args, **kwargs)

    correction_edit_module.insert_quaternion_key = _fail_after_range_delete
    try:
        try:
            apply_correction_pose_range(
                bpy.context,
                armature,
                scene_frame_start=8.0,
                scene_frame_end=12.0,
                ease_in_frames=2.0,
                ease_out_frames=3.0,
                include_location=True,
                interpolation="BEZIER",
                replace_inner_keys=True,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("Injected range writer failure did not propagate")
    finally:
        correction_edit_module.insert_quaternion_key = original_insert_quaternion
    if action_checksum(layer.action) != correction_checksum:
        raise AssertionError("Rejected range edits partially mutated the Correction")
    if action_checksum(base) != base_checksum:
        raise AssertionError("Correction range edit mutated the Base Action")

    # Low-level Blender/Python keying cannot silently bypass the public
    # Hips-only location contract. Finishing must reject it without mutation.
    insert_location_key(
        layer.access,
        "LeftUpperArm",
        10.0,
        unselected_location,
        interpolation="LINEAR",
    )
    invalid_checksum = action_checksum(layer.action)
    invalid_validation = validate_layer_stack(armature)
    if not any("limited to the Hips" in error for error in invalid_validation.errors):
        raise AssertionError("Layer validation accepted non-Hips location keys")
    try:
        exit_correction_edit_mode(bpy.context, armature)
    except ValueError as error:
        if "limited to the Hips" not in str(error):
            raise
    else:
        raise AssertionError("Finish accepted non-Hips location keys")
    if action_checksum(layer.action) != invalid_checksum:
        raise AssertionError("Rejected non-Hips location keys were mutated")
    invalid_path = 'pose.bones["LeftUpperArm"].location'
    for curve in tuple(iter_action_fcurves(layer.action)):
        if curve.data_path == invalid_path:
            remove_fcurve(layer.access, curve)

    delta_curves = tuple(
        ensure_fcurve(
            layer.access,
            "delta_location",
            index,
            group_name="Unsupported Object Delta",
        )
        for index in range(3)
    )
    for index, curve in enumerate(delta_curves):
        curve.keyframe_points.insert(10.0, float(index + 1))
    delta_checksum = action_checksum(layer.action)
    delta_validation = validate_layer_stack(armature)
    if not any("limited to the Hips" in error for error in delta_validation.errors):
        raise AssertionError("Layer validation accepted object delta_location keys")
    try:
        exit_correction_edit_mode(bpy.context, armature)
    except ValueError as error:
        if "limited to the Hips" not in str(error):
            raise
    else:
        raise AssertionError("Finish accepted object delta_location keys")
    if action_checksum(layer.action) != delta_checksum:
        raise AssertionError("Rejected object delta_location keys were mutated")
    for curve in delta_curves:
        remove_fcurve(layer.access, curve)

    validate_layer_stack(armature).raise_for_errors()
    exit_correction_edit_mode(bpy.context, armature)

    print(
        "MOTION_BRIDGE_CORRECTION_POSE_RANGE_TEST="
        + json.dumps(
            {
                "status": "PASS",
                "rangeKeysComplete": True,
                "easeFramesExact": True,
                "bezierAutoClamped": True,
                "innerKeysReplaced": True,
                "outsideKeysPreserved": True,
                "selectedBoneOnly": True,
                "nonHipsLocationRejected": True,
                "objectDeltaLocationRejected": True,
                "invalidInputTransactional": True,
                "runtimeFailureTransactional": True,
                "temporaryCaptureRemoved": True,
                "baseImmutable": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
