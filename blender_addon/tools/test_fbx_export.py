from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import tempfile
from typing import NoReturn

import bpy
from mathutils import Quaternion


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))

from motion_bridge import addon  # noqa: E402
from motion_bridge.canonical.rig_spec import SKELETON_ID  # noqa: E402
from motion_bridge.editing import bake_layer_stack, create_correction_layer  # noqa: E402
from motion_bridge.editing.action_api import insert_quaternion_key  # noqa: E402
from motion_bridge.io.fbx_export import (  # noqa: E402
    export_active_canonical_action,
    export_corrected_canonical_action,
)


FIRST_FRAME = 1
LAST_FRAME = 12


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)


def _create_two_key_action() -> tuple[bpy.types.Object, bpy.types.Action]:
    _clear_scene()
    scene = bpy.context.scene
    armature = bpy.data.armatures.new("MB_Export_Test_Armature")
    obj = bpy.data.objects.new("MB_Export_Test", armature)
    scene.collection.objects.link(obj)
    obj["motion_bridge_skeleton_id"] = SKELETON_ID

    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    bone = armature.edit_bones.new("Hips")
    bone.head = (0.0, 0.0, 0.0)
    bone.tail = (0.0, 0.0, 1.0)
    bpy.ops.object.mode_set(mode="POSE")

    pose_bone = obj.pose.bones["Hips"]
    pose_bone.rotation_mode = "QUATERNION"
    scene.frame_set(FIRST_FRAME)
    pose_bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    pose_bone.keyframe_insert(
        data_path="rotation_quaternion", frame=FIRST_FRAME, group="Hips"
    )
    scene.frame_set(LAST_FRAME)
    pose_bone.rotation_quaternion = (0.9238795, 0.0, 0.0, 0.3826834)
    pose_bone.keyframe_insert(
        data_path="rotation_quaternion", frame=LAST_FRAME, group="Hips"
    )

    action = obj.animation_data.action if obj.animation_data else None
    if action is None:
        raise AssertionError("Synthetic keyframes did not create an Action")
    action.name = "MB_Two_Key_Action"
    return obj, action


def _raise_export_error(**_kwargs: object) -> NoReturn:
    raise RuntimeError("synthetic export failure")


def _assert_editor_state(
    obj: bpy.types.Object,
    action: bpy.types.Action | None,
    selected_names: set[str],
    mode: str,
    frame_range: tuple[int, int],
    frame: int,
) -> None:
    context = bpy.context
    if context.view_layer.objects.active is not obj:
        raise AssertionError("Active object was not restored")
    if {selected.name for selected in context.selected_objects} != selected_names:
        raise AssertionError("Selection was not restored")
    if obj.mode != mode:
        raise AssertionError(f"Mode was not restored: {obj.mode} != {mode}")
    if obj.animation_data is None or obj.animation_data.action is not action:
        raise AssertionError("Active Action was not restored")
    if (context.scene.frame_start, context.scene.frame_end) != frame_range:
        raise AssertionError("Timeline range was not restored")
    if context.scene.frame_current != frame:
        raise AssertionError("Current frame was not restored")


def _verify_reimport(
    filepath: Path,
    *,
    expected_delta_degrees: float,
) -> dict[str, object]:
    _clear_scene()
    result = bpy.ops.import_scene.fbx(
        filepath=str(filepath),
        use_anim=True,
        anim_offset=0.0,
    )
    if "FINISHED" not in result:
        raise AssertionError(f"FBX import failed: {result}")

    armatures = [obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE"]
    if len(armatures) != 1:
        raise AssertionError(f"Expected one imported armature, got {len(armatures)}")
    imported = armatures[0]
    action = imported.animation_data.action if imported.animation_data else None
    if action is None:
        raise AssertionError("Reimported armature has no active Action")
    start, end = (float(frame) for frame in action.frame_range)
    if abs(start - FIRST_FRAME) > 1e-4 or abs(end - LAST_FRAME) > 1e-4:
        raise AssertionError(f"Unexpected imported Action range: {(start, end)}")

    scene = bpy.context.scene
    scene.frame_set(FIRST_FRAME)
    first_rotation = imported.pose.bones["Hips"].matrix.to_quaternion()
    scene.frame_set(LAST_FRAME)
    last_rotation = imported.pose.bones["Hips"].matrix.to_quaternion()
    rotation_delta = first_rotation.rotation_difference(last_rotation)
    delta_degrees = math.degrees(rotation_delta.angle)
    if (
        abs(delta_degrees - expected_delta_degrees) > 0.05
        or rotation_delta.axis.z < 0.9999
    ):
        raise AssertionError(
            "Reimported rotation delta mismatch: "
            f"angle={delta_degrees}, axis={tuple(rotation_delta.axis)}"
        )

    return {
        "action": action.name,
        "frameStart": start,
        "frameEnd": end,
        "rotationDeltaDegrees": delta_degrees,
        "fbxBytes": filepath.stat().st_size,
    }


def _verify_bake_target_removal_cleanup() -> None:
    obj, _action = _create_two_key_action()
    create_correction_layer(bpy.context, obj)
    if obj.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    obj.select_set(False)
    companion = bpy.data.objects.new("MB_Bake_Handler_Companion", None)
    bpy.context.scene.collection.objects.link(companion)
    companion.select_set(True)
    bpy.context.view_layer.objects.active = companion

    scene = bpy.context.scene
    scene.frame_start = 3
    scene.frame_end = 42
    scene.frame_set(7)
    target_data = obj.data
    target_pointer = obj.as_pointer()
    object_pointers_before = {item.as_pointer() for item in bpy.data.objects}
    action_pointers_before = {item.as_pointer() for item in bpy.data.actions}
    collection_pointers_before = {
        item.as_pointer() for item in bpy.data.collections
    }
    handler_called = False

    def remove_target_after_bake_role(
        _scene: bpy.types.Scene,
        _depsgraph: bpy.types.Depsgraph,
    ) -> None:
        nonlocal handler_called
        if handler_called:
            return
        if not any(
            action.as_pointer() not in action_pointers_before
            and action.get("motion_bridge_role") == "BAKED"
            for action in bpy.data.actions
        ):
            return
        handler_called = True
        bpy.data.objects.remove(obj, do_unlink=True)

    bpy.app.handlers.frame_change_post.append(remove_target_after_bake_role)
    try:
        try:
            bake_layer_stack(bpy.context, obj)
        except RuntimeError as error:
            if "removed before visual bake could finish" not in str(error):
                raise
        else:
            raise AssertionError(
                "Visual bake succeeded after a frame handler removed its target"
            )
    finally:
        if remove_target_after_bake_role in bpy.app.handlers.frame_change_post:
            bpy.app.handlers.frame_change_post.remove(remove_target_after_bake_role)

    if not handler_called:
        raise AssertionError("Visual bake rollback handler was not exercised")
    if {item.as_pointer() for item in bpy.data.actions} != action_pointers_before:
        raise AssertionError("Target removal leaked the visual-bake Action")
    if any(action.get("motion_bridge_role") == "BAKED" for action in bpy.data.actions):
        raise AssertionError("Target removal retained a BAKED Action")
    if not any(item is target_data for item in bpy.data.armatures):
        raise AssertionError("Visual bake rollback removed user-owned Armature data")
    if target_data.users != 0:
        raise AssertionError("Deleted target unexpectedly retained an Armature data user")
    if {
        item.as_pointer() for item in bpy.data.objects
    } != object_pointers_before - {target_pointer}:
        raise AssertionError("Visual bake rollback changed unrelated Objects")
    if {
        item.as_pointer() for item in bpy.data.collections
    } != collection_pointers_before:
        raise AssertionError("Visual bake rollback changed Collections")
    if bpy.context.view_layer.objects.active is not companion:
        raise AssertionError("Visual bake rollback did not restore the active Object")
    if tuple(bpy.context.selected_objects) != (companion,):
        raise AssertionError("Visual bake rollback did not restore selection")
    if (scene.frame_start, scene.frame_end, scene.frame_current) != (3, 42, 7):
        raise AssertionError("Visual bake rollback did not restore the timeline")


def main() -> None:
    registered = False
    try:
        addon.register()
        addon.unregister()
        addon.register()
        registered = True
        if not hasattr(bpy.types.Scene, "motion_bridge_session"):
            raise AssertionError("Session property was not registered")

        obj, action = _create_two_key_action()
        companion = bpy.data.objects.new("MB_Preexisting_Selection", None)
        bpy.context.scene.collection.objects.link(companion)
        companion.select_set(True)
        bpy.context.view_layer.objects.active = obj

        scene = bpy.context.scene
        scene.frame_start = 3
        scene.frame_end = 42
        scene.frame_set(7)
        expected_selection = {selected.name for selected in bpy.context.selected_objects}
        expected_mode = obj.mode
        expected_range = (scene.frame_start, scene.frame_end)

        with tempfile.TemporaryDirectory(prefix="motion_bridge_fbx_test_") as directory:
            directory_path = Path(directory)
            try:
                export_active_canonical_action(
                    bpy.context,
                    directory_path / "failure.fbx",
                    _exporter=_raise_export_error,
                )
            except RuntimeError as error:
                if str(error) != "synthetic export failure":
                    raise
            else:
                raise AssertionError("Synthetic FBX failure was not propagated")
            _assert_editor_state(
                obj,
                action,
                expected_selection,
                expected_mode,
                expected_range,
                7,
            )

            filepath = directory_path / "two_key_action.fbx"
            result = bpy.ops.motion_bridge.export_canonical_action_fbx(filepath=str(filepath))
            if "FINISHED" not in result:
                raise AssertionError(f"Export operator failed: {result}")
            _assert_editor_state(
                obj,
                action,
                expected_selection,
                expected_mode,
                expected_range,
                7,
            )

            stack = create_correction_layer(bpy.context, obj)
            identity = Quaternion((1.0, 0.0, 0.0, 0.0))
            z_correction = Quaternion(
                (0.0, 0.0, 1.0),
                math.radians(45.0),
            )
            insert_quaternion_key(
                stack.correction_access,
                "Hips",
                FIRST_FRAME,
                identity,
            )
            insert_quaternion_key(
                stack.correction_access,
                "Hips",
                LAST_FRAME,
                z_correction,
            )
            try:
                export_corrected_canonical_action(
                    bpy.context,
                    directory_path / "corrected_failure.fbx",
                    _exporter=_raise_export_error,
                )
            except RuntimeError as error:
                if str(error) != "synthetic export failure":
                    raise
            else:
                raise AssertionError("Synthetic corrected FBX failure was not propagated")
            _assert_editor_state(
                obj,
                None,
                expected_selection,
                expected_mode,
                expected_range,
                7,
            )
            if any(action.name.startswith("MB_BAKED::") for action in bpy.data.actions):
                raise AssertionError("Failed corrected export leaked its temporary Action")

            corrected_filepath = directory_path / "corrected_action.fbx"
            result = bpy.ops.motion_bridge.export_corrected_fbx(
                filepath=str(corrected_filepath)
            )
            if "FINISHED" not in result:
                raise AssertionError(f"Corrected export operator failed: {result}")
            _assert_editor_state(
                obj,
                None,
                expected_selection,
                expected_mode,
                expected_range,
                7,
            )
            if any(action.name.startswith("MB_BAKED::") for action in bpy.data.actions):
                raise AssertionError("Temporary corrected-export Action was not removed")

            report = {
                "active": _verify_reimport(
                    filepath,
                    expected_delta_degrees=45.0,
                ),
                "corrected": _verify_reimport(
                    corrected_filepath,
                    expected_delta_degrees=90.0,
                ),
            }
            _verify_bake_target_removal_cleanup()
            report["handlerBakeRollback"] = True

        addon.unregister()
        registered = False
        if hasattr(bpy.types.Scene, "motion_bridge_session"):
            raise AssertionError("Session property was not unregistered")
        print("MOTION_BRIDGE_FBX_TEST=" + json.dumps(report, sort_keys=True))
    finally:
        if registered:
            addon.unregister()


if __name__ == "__main__":
    main()
