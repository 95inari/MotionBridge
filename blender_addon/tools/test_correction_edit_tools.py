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
    iter_action_fcurves,
    remove_fcurve,
)
from motion_bridge.editing.pose_api import set_pose_bone_selected  # noqa: E402
from motion_bridge.editing.correction_edit import (  # noqa: E402
    apply_correction_pose_range,
    enter_correction_edit_mode,
    exit_correction_edit_mode,
    insert_correction_pose_keys,
    remove_correction_pose_keys,
    reset_correction_pose_keys,
)
from motion_bridge.editing.correction_layers import (  # noqa: E402
    action_checksum,
    create_correction_layer,
    validate_layer_stack,
)


FIRST_FRAME = 1
LAST_FRAME = 20
EDIT_FRAME = 10.25
EPSILON = 1.0e-6


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for action in tuple(bpy.data.actions):
        bpy.data.actions.remove(action)


def _create_armature() -> bpy.types.Object:
    _clear_scene()
    data = bpy.data.armatures.new("MB_Correction_Tools_Data")
    armature = bpy.data.objects.new("MB_Correction_Tools", data)
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
    base.name = "MB_Correction_Tools_Base"
    return armature


def _select_only(armature: bpy.types.Object, bone_name: str) -> None:
    for bone in armature.pose.bones:
        set_pose_bone_selected(bone, bone.name == bone_name)
    armature.data.bones.active = armature.data.bones[bone_name]


def _curves(action: bpy.types.Action) -> dict[tuple[str, int], bpy.types.FCurve]:
    return {
        (curve.data_path, int(curve.array_index)): curve
        for curve in iter_action_fcurves(action)
    }


def _point(
    curve: bpy.types.FCurve,
    frame: float,
) -> bpy.types.Keyframe:
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


def _values_at(
    curves: dict[tuple[str, int], bpy.types.FCurve],
    data_path: str,
    size: int,
    frame: float,
) -> tuple[float, ...]:
    return tuple(
        float(_point(curves[(data_path, index)], frame).co[1]) for index in range(size)
    )


def _assert_values(
    curves: dict[tuple[str, int], bpy.types.FCurve],
    data_path: str,
    frame: float,
    expected: tuple[float, ...],
) -> None:
    actual = _values_at(curves, data_path, len(expected), frame)
    if any(abs(left - right) > EPSILON for left, right in zip(actual, expected)):
        raise AssertionError(
            f"Unexpected values at {frame} on {data_path}: {actual!r} != {expected!r}"
        )


def _assert_no_keys_at(
    curves: dict[tuple[str, int], bpy.types.FCurve],
    data_path: str,
    size: int,
    frame: float,
) -> None:
    for index in range(size):
        curve = curves[(data_path, index)]
        if any(
            abs(float(point.co[0]) - frame) <= EPSILON
            for point in curve.keyframe_points
        ):
            raise AssertionError(f"Key remained at {frame} on {data_path}[{index}]")


def _assert_interpolation(
    curves: dict[tuple[str, int], bpy.types.FCurve],
    data_path: str,
    size: int,
    frames: tuple[float, ...],
    expected: str,
) -> None:
    for index in range(size):
        for frame in frames:
            actual = str(_point(curves[(data_path, index)], frame).interpolation)
            if actual != expected:
                raise AssertionError(
                    f"Interpolation at {frame} on {data_path}[{index}] "
                    f"was {actual}, expected {expected}"
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
    _select_only(armature, "Hips")
    hips = armature.pose.bones["Hips"]
    rotation = Quaternion((0.2, 0.8, -0.3), math.radians(43.0)).normalized()
    location = (0.25, -0.5, 0.75)
    bpy.context.scene.frame_set(10, subframe=0.25)
    hips.rotation_quaternion = rotation
    hips.location = location
    names = insert_correction_pose_keys(
        bpy.context,
        armature,
        include_location=True,
        neutral_frame_before=8.0,
        neutral_frame_after=12.0,
        interpolation="BEZIER",
    )
    if names != ("Hips",):
        raise AssertionError(f"Insert returned unexpected bones: {names!r}")

    rotation_path = 'pose.bones["Hips"].rotation_quaternion'
    location_path = 'pose.bones["Hips"].location'
    curves = _curves(layer.action)
    _assert_interpolation(
        curves,
        rotation_path,
        4,
        (8.0, EDIT_FRAME, 12.0),
        "BEZIER",
    )
    _assert_interpolation(
        curves,
        location_path,
        3,
        (8.0, EDIT_FRAME, 12.0),
        "BEZIER",
    )

    # None means the current scene time converted to Correction Action time.
    reset_names = reset_correction_pose_keys(
        bpy.context,
        armature,
        include_location=False,
    )
    if reset_names != ("Hips",):
        raise AssertionError(f"Reset returned unexpected bones: {reset_names!r}")
    curves = _curves(layer.action)
    _assert_values(curves, rotation_path, EDIT_FRAME, (1.0, 0.0, 0.0, 0.0))
    _assert_values(curves, location_path, EDIT_FRAME, location)

    reset_correction_pose_keys(
        bpy.context,
        armature,
        include_location=True,
        frame=EDIT_FRAME,
    )
    curves = _curves(layer.action)
    _assert_values(curves, location_path, EDIT_FRAME, (0.0, 0.0, 0.0))
    # Resetting the current key must leave its neutral bracket intact.
    _assert_values(curves, rotation_path, 8.0, (1.0, 0.0, 0.0, 0.0))
    _assert_values(curves, rotation_path, 12.0, (1.0, 0.0, 0.0, 0.0))

    # Reset is an upsert, so it can deliberately cancel a correction even
    # where the selected bone does not already have a key.
    bpy.context.scene.frame_set(3)
    reset_correction_pose_keys(
        bpy.context,
        armature,
        include_location=True,
        frame=14.0,
    )
    curves = _curves(layer.action)
    _assert_values(curves, rotation_path, 14.0, (1.0, 0.0, 0.0, 0.0))
    _assert_values(curves, location_path, 14.0, (0.0, 0.0, 0.0))

    # Insert a second correction, then remove rotation and location separately.
    bpy.context.scene.frame_set(6)
    removal_rotation = Quaternion((0.7, 0.1, 0.4), math.radians(27.0)).normalized()
    removal_location = (-0.125, 0.375, 0.5)
    hips.rotation_quaternion = removal_rotation
    hips.location = removal_location
    insert_correction_pose_keys(
        bpy.context,
        armature,
        include_location=True,
        interpolation="CONSTANT",
    )
    bpy.context.scene.frame_set(9)
    remove_names = remove_correction_pose_keys(
        bpy.context,
        armature,
        include_location=False,
        frame=6.0,
    )
    if remove_names != ("Hips",):
        raise AssertionError(f"Remove returned unexpected bones: {remove_names!r}")
    curves = _curves(layer.action)
    _assert_no_keys_at(curves, rotation_path, 4, 6.0)
    _assert_values(curves, location_path, 6.0, removal_location)
    _assert_values(curves, rotation_path, EDIT_FRAME, (1.0, 0.0, 0.0, 0.0))

    remove_correction_pose_keys(
        bpy.context,
        armature,
        include_location=True,
        frame=6.0,
    )
    curves = _curves(layer.action)
    _assert_no_keys_at(curves, location_path, 3, 6.0)
    if any("LeftUpperArm" in path for path, _index in curves):
        raise AssertionError("Correction tools changed an unselected pose bone")

    # Invalid options and out-of-range edits are transactional.
    correction_checksum = action_checksum(layer.action)
    _expect_value_error(
        lambda: insert_correction_pose_keys(
            bpy.context,
            armature,
            interpolation="CUBIC_SPARKLES",
        ),
        "Invalid interpolation",
    )
    _expect_value_error(
        lambda: reset_correction_pose_keys(
            bpy.context,
            armature,
            frame=LAST_FRAME + 1.0,
        ),
        "Out-of-range reset",
    )
    _expect_value_error(
        lambda: remove_correction_pose_keys(
            bpy.context,
            armature,
            frame=LAST_FRAME + 1.0,
        ),
        "Out-of-range remove",
    )
    if action_checksum(layer.action) != correction_checksum:
        raise AssertionError(
            "Rejected Correction tool call partially mutated the Action"
        )

    # Location is a Hips-only public contract, including Advanced/API calls.
    _select_only(armature, "LeftUpperArm")
    location_rejection_checksum = action_checksum(layer.action)
    _expect_value_error(
        lambda: insert_correction_pose_keys(
            bpy.context,
            armature,
            include_location=True,
        ),
        "Non-Hips location insert",
    )
    _expect_value_error(
        lambda: reset_correction_pose_keys(
            bpy.context,
            armature,
            include_location=True,
        ),
        "Non-Hips location reset",
    )
    _expect_value_error(
        lambda: remove_correction_pose_keys(
            bpy.context,
            armature,
            include_location=True,
        ),
        "Non-Hips location remove",
    )
    _expect_value_error(
        lambda: apply_correction_pose_range(
            bpy.context,
            armature,
            scene_frame_start=1.0,
            scene_frame_end=20.0,
            include_location=True,
        ),
        "Non-Hips location range",
    )
    if action_checksum(layer.action) != location_rejection_checksum:
        raise AssertionError("Rejected non-Hips location edit mutated the Action")

    # Only real PoseBone quaternion paths belong in a Correction Action.
    # Object transforms, arbitrary suffixes, and missing bones must be rejected
    # both by whole-stack validation and by Finish Editing.
    invalid_rotation_paths = (
        "rotation_quaternion",
        "foo.rotation_quaternion",
        'pose.bones["MissingBone"].rotation_quaternion',
    )
    invalid_curves = []
    for path in invalid_rotation_paths:
        for index, value in enumerate((1.0, 0.0, 0.0, 0.0)):
            curve = ensure_fcurve(
                layer.access,
                path,
                index,
                group_name="Invalid Rotation",
            )
            point = curve.keyframe_points.insert(EDIT_FRAME, value)
            point.interpolation = "LINEAR"
            curve.update()
            invalid_curves.append(curve)
    invalid_stack = validate_layer_stack(armature)
    for path in invalid_rotation_paths:
        if not any(path in error for error in invalid_stack.errors):
            raise AssertionError(
                f"Stack validation accepted invalid rotation path: {path}"
            )
    _expect_value_error(
        lambda: exit_correction_edit_mode(bpy.context, armature),
        "Invalid rotation path on Finish Editing",
    )
    for curve in invalid_curves:
        remove_fcurve(layer.access, curve)

    # An unexpected Blender/RNA failure after key creation must restore keys
    # and remove every new selected-property curve.
    _select_only(armature, "LeftUpperArm")
    arm = armature.pose.bones["LeftUpperArm"]
    bpy.context.scene.frame_set(11)
    arm.rotation_quaternion = Quaternion(
        (1.0, 0.0, 0.0),
        math.radians(17.0),
    )
    arm.location = (0.2, 0.3, 0.4)
    before_runtime_failure = action_checksum(layer.action)
    original_insert_quaternion = correction_edit_module.insert_quaternion_key

    def _fail_quaternion_insert(*_args, **_kwargs) -> None:
        raise RuntimeError("Injected quaternion writer failure")

    correction_edit_module.insert_quaternion_key = _fail_quaternion_insert
    try:
        try:
            insert_correction_pose_keys(
                bpy.context,
                armature,
                include_location=False,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("Injected writer failure did not propagate")
    finally:
        correction_edit_module.insert_quaternion_key = original_insert_quaternion
    if action_checksum(layer.action) != before_runtime_failure:
        raise AssertionError("Runtime keying failure did not restore the Action")
    if any(
        "LeftUpperArm" in curve.data_path for curve in iter_action_fcurves(layer.action)
    ):
        raise AssertionError("Runtime keying failure left newly-created FCurves")
    if action_checksum(base) != base_checksum:
        raise AssertionError("Correction tools mutated the Base Action")

    validate_layer_stack(armature).raise_for_errors()
    exit_correction_edit_mode(bpy.context, armature)
    _expect_value_error(
        lambda: reset_correction_pose_keys(bpy.context, armature),
        "Reset outside Correction Edit",
    )
    _expect_value_error(
        lambda: remove_correction_pose_keys(bpy.context, armature),
        "Remove outside Correction Edit",
    )

    print(
        "MOTION_BRIDGE_CORRECTION_EDIT_TOOLS_TEST="
        + json.dumps(
            {
                "status": "PASS",
                "interpolationApplied": True,
                "resetRotationOnly": True,
                "resetLocationOptional": True,
                "removeRotationOnly": True,
                "removeLocationOptional": True,
                "explicitActionFrame": True,
                "selectedBonesOnly": True,
                "locationRestrictedToHips": True,
                "rotationRestrictedToPoseBones": True,
                "invalidInputTransactional": True,
                "runtimeFailureTransactional": True,
                "baseImmutable": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
