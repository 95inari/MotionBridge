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
TOOLS_ROOT = Path(__file__).resolve().parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from test_layer_merge import _create_base_action  # noqa: E402
from motion_bridge import addon  # noqa: E402
from motion_bridge.editing.correction_layers import (  # noqa: E402
    create_correction_layer,
    validate_layer_stack,
)


def _rotation(armature: bpy.types.Object, frame: int) -> Quaternion:
    bpy.context.scene.frame_set(frame)
    bpy.context.view_layer.update()
    evaluated = armature.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return evaluated.pose.bones["Hips"].matrix.to_quaternion()


def main() -> None:
    addon.register()
    try:
        armature, _base = _create_base_action()
        armature.rotation_mode = "QUATERNION"
        for frame, x in ((1, 0.0), (12, 1.0)):
            bpy.context.scene.frame_set(frame)
            armature.location = (x, 0.0, 0.0)
            armature.rotation_quaternion = Quaternion()
            armature.keyframe_insert(data_path="location", frame=frame, group="Root")
            armature.keyframe_insert(
                data_path="rotation_quaternion", frame=frame, group="Root"
            )
        bpy.context.scene.frame_set(12)
        armature.pose.bones["Hips"].rotation_quaternion = Quaternion(
            (0.0, 0.0, 1.0), math.radians(80.0)
        )
        armature.pose.bones["Hips"].keyframe_insert(
            data_path="rotation_quaternion", frame=12, group="Hips"
        )
        create_correction_layer(bpy.context, armature)
        source = _rotation(armature, 1)
        original_target = _rotation(armature, 8)
        if abs(source.dot(original_target)) > 0.999:
            raise AssertionError("Source and target frames should differ before paste")
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.context.scene.frame_set(1)
        if bpy.ops.motion_bridge.copy_source_pose() != {"FINISHED"}:
            raise AssertionError("Copy source pose failed")
        bpy.context.scene.frame_set(8)
        if bpy.ops.motion_bridge.paste_source_pose() != {"FINISHED"}:
            raise AssertionError("Paste source pose failed")
        pasted = _rotation(armature, 8)
        if abs(source.dot(pasted)) < 0.99999:
            raise AssertionError("Pasted pose differs from the copied source frame")
        evaluated = armature.evaluated_get(bpy.context.evaluated_depsgraph_get())
        if abs(float(evaluated.matrix_world.translation.x)) > 1.0e-5:
            raise AssertionError("Pasted Root location differs from the copied source")
        validate_layer_stack(armature).raise_for_errors()
        print("MOTION_BRIDGE_SOURCE_POSE_CLIPBOARD_TEST=" + json.dumps({"status": "PASS"}))
    finally:
        addon.unregister()


if __name__ == "__main__":
    main()
