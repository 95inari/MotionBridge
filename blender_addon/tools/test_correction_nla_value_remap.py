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
from motion_bridge.editing.action_api import (  # noqa: E402
    insert_location_key,
    insert_quaternion_key,
    iter_action_fcurves,
)
from motion_bridge.editing.pose_api import set_pose_bone_selected  # noqa: E402
from motion_bridge.editing.correction_edit import (  # noqa: E402
    enter_correction_edit_mode,
    exit_correction_edit_mode,
    insert_correction_pose_keys,
)
from motion_bridge.editing.correction_layers import (  # noqa: E402
    action_checksum,
    add_correction_layer,
    create_correction_layer,
)


EPSILON = 1.0e-5


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for action in tuple(bpy.data.actions):
        bpy.data.actions.remove(action)


def _create_animated_armature() -> tuple[bpy.types.Object, bpy.types.Action]:
    _clear_scene()
    data = bpy.data.armatures.new("MB_NLA_Remap_Data")
    armature = bpy.data.objects.new("MB_NLA_Remap", data)
    bpy.context.scene.collection.objects.link(armature)
    armature["motion_bridge_skeleton_id"] = SKELETON_ID
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature

    bpy.ops.object.mode_set(mode="EDIT")
    hips = data.edit_bones.new("Hips")
    hips.head = (0.0, 0.0, 0.0)
    hips.tail = (0.0, 0.0, 1.0)
    bpy.ops.object.mode_set(mode="POSE")

    bone = armature.pose.bones["Hips"]
    bone.rotation_mode = "QUATERNION"
    base_rotation = Quaternion((0.0, 0.0, 1.0), math.radians(30.0))
    for frame in (1.0, 20.0):
        bone.rotation_quaternion = base_rotation
        bone.location = (1.0, 0.0, 0.0)
        bone.keyframe_insert(
            data_path="rotation_quaternion",
            frame=frame,
            group=bone.name,
        )
        bone.keyframe_insert(data_path="location", frame=frame, group=bone.name)
    animation_data = armature.animation_data
    if animation_data is None or animation_data.action is None:
        raise AssertionError("Fixture did not create a Base Action")
    animation_data.action.name = "MB_NLA_Remap_Base"
    return armature, animation_data.action


def _key_values(
    action: bpy.types.Action,
    data_path: str,
    frame: float,
    size: int,
) -> tuple[float, ...]:
    curves = {
        int(curve.array_index): curve
        for curve in iter_action_fcurves(action)
        if curve.data_path == data_path
    }
    values: list[float] = []
    for index in range(size):
        points = [
            point
            for point in curves[index].keyframe_points
            if abs(float(point.co[0]) - frame) <= EPSILON
        ]
        if len(points) != 1:
            raise AssertionError(f"Expected one {data_path}[{index}] key at {frame}")
        values.append(float(points[0].co[1]))
    return tuple(values)


def _rotation_error_degrees(left: Quaternion, right: Quaternion) -> float:
    return math.degrees(left.rotation_difference(right).angle)


def main() -> None:
    armature, base = _create_animated_armature()
    base_checksum = action_checksum(base)
    stack = create_correction_layer(bpy.context, armature)
    lower_layer = stack.active_layer
    lower_rotation = Quaternion((0.0, 0.0, 1.0), math.radians(5.0))
    insert_quaternion_key(
        lower_layer.access,
        "Hips",
        10.0,
        lower_rotation,
    )
    insert_location_key(
        lower_layer.access,
        "Hips",
        10.0,
        (0.1, 0.0, 0.0),
    )
    lower_checksum = action_checksum(lower_layer.action)
    layer = add_correction_layer(bpy.context, armature, name="Upper Correction")
    enter_correction_edit_mode(bpy.context, armature)

    bpy.context.scene.frame_set(10)
    bone = armature.pose.bones["Hips"]
    set_pose_bone_selected(bone, True)
    armature.data.bones.active = armature.data.bones[bone.name]
    desired_rotation = Quaternion((0.0, 0.0, 1.0), math.radians(40.0))
    desired_location = (1.25, 0.0, 0.0)
    bone.rotation_quaternion = desired_rotation
    bone.location = desired_location
    insert_correction_pose_keys(
        bpy.context,
        armature,
        include_location=True,
        interpolation="LINEAR",
    )

    rotation_path = 'pose.bones["Hips"].rotation_quaternion'
    location_path = 'pose.bones["Hips"].location'
    stored_rotation = Quaternion(
        _key_values(layer.action, rotation_path, 10.0, 4)
    ).normalized()
    stored_location = _key_values(layer.action, location_path, 10.0, 3)
    expected_delta = Quaternion((0.0, 0.0, 1.0), math.radians(5.0))
    if _rotation_error_degrees(stored_rotation, expected_delta) > 0.01:
        raise AssertionError(
            "Correction stored the evaluated pose instead of the NLA-remapped "
            f"delta: {tuple(stored_rotation)!r}"
        )
    if any(
        abs(value - expected) > EPSILON
        for value, expected in zip(
            stored_location,
            (0.15, 0.0, 0.0),
            strict=True,
        )
    ):
        raise AssertionError(f"Unexpected remapped location: {stored_location!r}")

    exit_correction_edit_mode(bpy.context, armature)
    bpy.context.scene.frame_set(10)
    bpy.context.view_layer.update()
    evaluated_rotation = bone.rotation_quaternion.copy().normalized()
    evaluated_location = tuple(float(value) for value in bone.location)
    if _rotation_error_degrees(evaluated_rotation, desired_rotation) > 0.01:
        raise AssertionError(
            f"Corrected pose double-applied rotation: {tuple(evaluated_rotation)!r}"
        )
    if any(
        abs(value - expected) > EPSILON
        for value, expected in zip(
            evaluated_location,
            desired_location,
            strict=True,
        )
    ):
        raise AssertionError(
            f"Corrected pose double-applied location: {evaluated_location!r}"
        )
    if action_checksum(base) != base_checksum:
        raise AssertionError("Correction keying mutated the Base Action")
    if action_checksum(lower_layer.action) != lower_checksum:
        raise AssertionError("Correction keying mutated a lower Correction Action")

    print(
        "MOTION_BRIDGE_CORRECTION_NLA_VALUE_REMAP_TEST="
        + json.dumps(
            {
                "status": "PASS",
                "baseImmutable": True,
                "lowerCorrectionImmutable": True,
                "storedRotationDeltaDegrees": 5.0,
                "storedLocationDelta": 0.15,
                "evaluatedPoseMatchesDesired": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
