from __future__ import annotations

from dataclasses import replace
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
from motion_bridge.addon import _idle_correction_stack  # noqa: E402
from motion_bridge.editing.action_api import iter_action_fcurves  # noqa: E402
from motion_bridge.editing.pose_api import (  # noqa: E402
    pose_bone_selected,
    set_pose_bone_selected,
)
from motion_bridge.editing.correction_edit import (  # noqa: E402
    _nla_operator_context,
    cancel_all_correction_edit_sessions,
    cancel_correction_edit_mode,
    clear_correction_edit_sessions,
    enter_correction_edit_mode,
    exit_correction_edit_mode,
    exit_stale_correction_tweak_mode,
    has_correction_edit_session,
    insert_correction_pose_keys,
    stale_correction_tweak_layer,
)
from motion_bridge.editing.correction_layers import (  # noqa: E402
    action_checksum,
    add_correction_layer,
    create_correction_layer,
    get_correction_layer_stack,
    validate_layer_stack,
)


FIRST_FRAME = 1
LAST_FRAME = 20
EDIT_FRAME = 10.25
LOCATION_EPSILON = 1e-7
QUATERNION_EPSILON = 1e-6


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for action in tuple(bpy.data.actions):
        bpy.data.actions.remove(action)


def _create_armature() -> tuple[bpy.types.Object, bpy.types.Object]:
    _clear_scene()
    data = bpy.data.armatures.new("MB_Correction_Edit_Data")
    armature = bpy.data.objects.new("MB_Correction_Edit", data)
    bpy.context.scene.collection.objects.link(armature)
    armature["motion_bridge_skeleton_id"] = SKELETON_ID
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature

    bpy.ops.object.mode_set(mode="EDIT")
    hips = data.edit_bones.new("Hips")
    hips.head = (0.0, 0.0, 0.0)
    hips.tail = (0.0, 0.0, 1.0)
    left = data.edit_bones.new("LeftUpperArm")
    left.head = (0.0, 0.0, 0.8)
    left.tail = (0.8, 0.0, 0.8)
    left.parent = hips
    right = data.edit_bones.new("RightUpperArm")
    right.head = (0.0, 0.0, 0.8)
    right.tail = (-0.8, 0.0, 0.8)
    right.parent = hips
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
    base.name = "MB_Correction_Edit_Base"

    companion = bpy.data.objects.new("MB_Correction_Edit_Companion", None)
    bpy.context.scene.collection.objects.link(companion)
    companion.select_set(True)
    return armature, companion


def _create_unrelated_nla_armature() -> tuple[
    bpy.types.Object, bpy.types.NlaTrack, bpy.types.NlaStrip
]:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    data = bpy.data.armatures.new("MB_Unrelated_Edit_Data")
    armature = bpy.data.objects.new("MB_Unrelated_Edit", data)
    bpy.context.scene.collection.objects.link(armature)
    bpy.ops.object.select_all(action="DESELECT")
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode="EDIT")
    bone = data.edit_bones.new("OtherBone")
    bone.head = (0.0, 0.0, 0.0)
    bone.tail = (0.0, 0.0, 1.0)
    bpy.ops.object.mode_set(mode="POSE")
    armature.pose.bones["OtherBone"].rotation_mode = "QUATERNION"
    for frame in (FIRST_FRAME, LAST_FRAME):
        armature.pose.bones["OtherBone"].keyframe_insert(
            data_path="rotation_quaternion",
            frame=frame,
            group="OtherBone",
        )
    animation_data = armature.animation_data
    action = animation_data.action if animation_data is not None else None
    if animation_data is None or action is None:
        raise AssertionError("Unrelated NLA fixture has no Action")
    action.name = "MB_Unrelated_Edit_Action"
    track = animation_data.nla_tracks.new()
    track.name = "MB Unrelated Selected Track"
    strip = track.strips.new("MB Unrelated Selected Strip", FIRST_FRAME, action)
    animation_data.action = None
    animation_data.nla_tracks.active = track
    track.select = True
    strip.select = True
    bpy.ops.object.mode_set(mode="OBJECT")
    armature.select_set(False)
    return armature, track, strip


def _capture_unrelated_nla_state(armature: bpy.types.Object) -> dict[str, object]:
    animation_data = armature.animation_data
    if animation_data is None:
        raise AssertionError("Unrelated NLA fixture lost AnimData")
    return {
        "action": animation_data.action,
        "useTweakMode": bool(animation_data.use_tweak_mode),
        "activeTrack": animation_data.nla_tracks.active,
        "trackSelection": tuple(
            bool(track.select) for track in animation_data.nla_tracks
        ),
        "stripSelection": tuple(
            bool(strip.select)
            for track in animation_data.nla_tracks
            for strip in track.strips
        ),
    }


def _selected_bone_names(armature: bpy.types.Object) -> tuple[str, ...]:
    return tuple(sorted(bone.name for bone in armature.pose.bones if pose_bone_selected(bone)))


def _capture_editor_state(armature: bpy.types.Object) -> dict[str, object]:
    context = bpy.context
    animation_data = armature.animation_data
    return {
        "activeObject": (
            context.view_layer.objects.active.name
            if context.view_layer.objects.active is not None
            else None
        ),
        "selectedObjects": tuple(sorted(obj.name for obj in context.selected_objects)),
        "mode": context.object.mode if context.object is not None else None,
        "selectedBones": _selected_bone_names(armature),
        "activeBone": (
            armature.data.bones.active.name
            if armature.data.bones.active is not None
            else None
        ),
        "frame": context.scene.frame_current,
        "subframe": context.scene.frame_subframe,
        "action": animation_data.action if animation_data is not None else None,
        "useTweakMode": (
            bool(animation_data.use_tweak_mode) if animation_data is not None else False
        ),
        "trackSelection": tuple(
            bool(track.select) for track in animation_data.nla_tracks
        ),
        "activeTrack": animation_data.nla_tracks.active,
        "stripSelection": tuple(
            bool(strip.select)
            for track in animation_data.nla_tracks
            for strip in track.strips
        ),
        "areaTypes": tuple(area.type for area in context.screen.areas),
        "dopesheetEditors": tuple(
            (
                int(area.as_pointer()),
                area.spaces.active.mode,
                bool(area.spaces.active.dopesheet.show_only_selected),
                (
                    bool(area.spaces.active.dopesheet.show_only_slot_of_active_object)
                    if hasattr(
                        area.spaces.active.dopesheet,
                        "show_only_slot_of_active_object",
                    )
                    else None
                ),
            )
            for area in context.screen.areas
            if area.type == "DOPESHEET_EDITOR"
        ),
    }


def _smallest_timeline_area() -> bpy.types.Area | None:
    candidates = tuple(
        area
        for area in bpy.context.screen.areas
        if area.type == "DOPESHEET_EDITOR"
        and getattr(area.spaces.active, "mode", None) == "TIMELINE"
    )
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda area: (
            int(area.width) * int(area.height),
            int(area.height),
            int(area.width),
        ),
    )


def _assert_editor_state(
    armature: bpy.types.Object,
    expected: dict[str, object],
) -> None:
    actual = _capture_editor_state(armature)
    if actual != expected:
        raise AssertionError(
            "Correction Edit did not restore editor state:\n"
            f"actual={actual!r}\nexpected={expected!r}"
        )


def _curve_map(action: bpy.types.Action) -> dict[tuple[str, int], bpy.types.FCurve]:
    return {
        (curve.data_path, int(curve.array_index)): curve
        for curve in iter_action_fcurves(action)
    }


def _point_value(curve: bpy.types.FCurve, frame: float) -> float:
    matches = [
        float(point.co[1])
        for point in curve.keyframe_points
        if abs(float(point.co[0]) - frame) <= 1e-6
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"Expected one key at frame {frame} on "
            f"{curve.data_path}[{curve.array_index}], got {len(matches)}"
        )
    return matches[0]


def _assert_vector_keys(
    curves: dict[tuple[str, int], bpy.types.FCurve],
    data_path: str,
    expected_by_frame: dict[float, tuple[float, ...]],
    *,
    epsilon: float,
) -> None:
    component_count = len(next(iter(expected_by_frame.values())))
    available = {index for path, index in curves if path == data_path}
    if available != set(range(component_count)):
        raise AssertionError(
            f"Incomplete keyed vector for {data_path}: {sorted(available)}"
        )
    for frame, expected in expected_by_frame.items():
        actual = tuple(
            _point_value(curves[(data_path, index)], frame)
            for index in range(component_count)
        )
        if any(abs(a - b) > epsilon for a, b in zip(actual, expected)):
            raise AssertionError(
                f"Unexpected key values for {data_path} at {frame}: "
                f"{actual!r} != {expected!r}"
            )


def _select_only(armature: bpy.types.Object, bone_name: str) -> None:
    for bone in armature.pose.bones:
        set_pose_bone_selected(bone, bone.name == bone_name)
    armature.data.bones.active = armature.data.bones[bone_name]


def _configure_pre_edit_state(
    armature: bpy.types.Object,
    companion: bpy.types.Object,
) -> None:
    armature.select_set(True)
    companion.select_set(True)
    bpy.context.view_layer.objects.active = armature
    if armature.mode != "POSE":
        bpy.ops.object.mode_set(mode="POSE")
    for bone in armature.pose.bones:
        set_pose_bone_selected(bone, bone.name in {"Hips", "RightUpperArm"})
    armature.data.bones.active = armature.data.bones["RightUpperArm"]
    bpy.context.scene.frame_set(10, subframe=0.25)


def main() -> None:
    armature, companion = _create_armature()
    original_base = armature.animation_data.action
    original_base_checksum = action_checksum(original_base)
    stack = create_correction_layer(bpy.context, armature)
    first_layer = stack.active_layer
    active_layer = add_correction_layer(
        bpy.context,
        armature,
        name="Hands and Hips",
    )
    unrelated, _unrelated_track, _unrelated_strip = _create_unrelated_nla_armature()
    stack = get_correction_layer_stack(armature)
    if stack is None or stack.active_layer_id != active_layer.layer_id:
        raise AssertionError(
            "Test fixture did not activate the second Correction layer"
        )

    # Deliberately non-default NLA selection is part of the state contract.
    stack.base_track.select = True
    armature.animation_data.nla_tracks.active = stack.base_track
    first_layer.track.select = True
    active_layer.track.select = False
    stack.base_strip.select = True
    first_layer.strip.select = False
    active_layer.strip.select = False
    _configure_pre_edit_state(armature, companion)
    expected_state = _capture_editor_state(armature)
    timeline_area = _smallest_timeline_area()
    if timeline_area is None:
        raise AssertionError(
            "Correction Edit UI test requires the standard Timeline Area"
        )
    timeline_pointer = (
        int(timeline_area.as_pointer()) if timeline_area is not None else None
    )
    expected_unrelated_state = _capture_unrelated_nla_state(unrelated)
    if expected_state["action"] is not None or expected_state["useTweakMode"]:
        raise AssertionError("Fixture unexpectedly started with an active Action")

    session = enter_correction_edit_mode(bpy.context, armature)
    if (
        session.layer_id != active_layer.layer_id
        or session.action is not active_layer.action
        or session.strip.as_pointer() != active_layer.strip.as_pointer()
    ):
        raise AssertionError("Correction Edit did not target the active stable layer")
    if not armature.animation_data.use_tweak_mode:
        raise AssertionError("Correction Edit did not enter NLA tweak mode")
    if armature.animation_data.action is not active_layer.action:
        raise AssertionError("NLA tweak mode did not bind the active Correction Action")
    unrelated_during_edit = _capture_unrelated_nla_state(unrelated)
    if (
        unrelated_during_edit["useTweakMode"]
        or unrelated_during_edit["action"] is not None
    ):
        raise AssertionError("Correction Edit entered tweak mode on an unrelated rig")
    if (
        tuple(area.type for area in bpy.context.screen.areas)
        != expected_state["areaTypes"]
    ):
        raise AssertionError("NLA context override leaked an editor area type")
    if timeline_pointer is not None:
        action_area = next(
            (
                area
                for area in bpy.context.screen.areas
                if int(area.as_pointer()) == timeline_pointer
            ),
            None,
        )
        if action_area is None or action_area.type != "DOPESHEET_EDITOR":
            raise AssertionError("Correction Edit replaced the Timeline Area")
        action_space = action_area.spaces.active
        if action_space.mode != "ACTION":
            raise AssertionError("Correction Edit did not show the Action Editor")
        if not action_space.dopesheet.show_only_selected:
            raise AssertionError("Action Editor is not filtered to selected bones")
        if (
            hasattr(action_space.dopesheet, "show_only_slot_of_active_object")
            and not action_space.dopesheet.show_only_slot_of_active_object
        ):
            raise AssertionError("Action Editor is not filtered to the active slot")

    hips_rotation = Quaternion((0.3, 0.7, -0.2), math.radians(47.0)).normalized()
    hips_location = (0.125, -0.25, 0.375)
    bpy.context.scene.frame_set(10, subframe=0.25)
    _select_only(armature, "Hips")
    hips = armature.pose.bones["Hips"]
    hips.rotation_quaternion = hips_rotation
    hips.location = hips_location
    insert_correction_pose_keys(
        bpy.context,
        armature,
        include_location=True,
        neutral_frame_before=8.0,
        neutral_frame_after=12.0,
    )

    curves = _curve_map(active_layer.action)
    hips_rotation_path = 'pose.bones["Hips"].rotation_quaternion'
    hips_location_path = 'pose.bones["Hips"].location'
    _assert_vector_keys(
        curves,
        hips_rotation_path,
        {
            8.0: (1.0, 0.0, 0.0, 0.0),
            EDIT_FRAME: tuple(hips_rotation),
            12.0: (1.0, 0.0, 0.0, 0.0),
        },
        epsilon=QUATERNION_EPSILON,
    )
    _assert_vector_keys(
        curves,
        hips_location_path,
        {
            8.0: (0.0, 0.0, 0.0),
            EDIT_FRAME: hips_location,
            12.0: (0.0, 0.0, 0.0),
        },
        epsilon=LOCATION_EPSILON,
    )

    right_rotation = Quaternion((-0.2, 0.5, 0.7), math.radians(31.0)).normalized()
    bpy.context.scene.frame_set(15)
    _select_only(armature, "RightUpperArm")
    right = armature.pose.bones["RightUpperArm"]
    right.rotation_quaternion = right_rotation
    right.location = (9.0, 8.0, 7.0)  # Must not be keyed when include_location=False.
    insert_correction_pose_keys(
        bpy.context,
        armature,
        include_location=False,
        neutral_frame_before=14.0,
        neutral_frame_after=16.0,
    )
    curves = _curve_map(active_layer.action)
    right_rotation_path = 'pose.bones["RightUpperArm"].rotation_quaternion'
    right_location_path = 'pose.bones["RightUpperArm"].location'
    _assert_vector_keys(
        curves,
        right_rotation_path,
        {
            14.0: (1.0, 0.0, 0.0, 0.0),
            15.0: tuple(right_rotation),
            16.0: (1.0, 0.0, 0.0, 0.0),
        },
        epsilon=QUATERNION_EPSILON,
    )
    if any(path == right_location_path for path, _index in curves):
        raise AssertionError("Rotation-only correction unexpectedly keyed location")
    if any("LeftUpperArm" in path for path, _index in curves):
        raise AssertionError("An unselected pose bone received Correction keys")
    if any(iter_action_fcurves(first_layer.action)):
        raise AssertionError("Correction Edit wrote to an inactive layer")
    if action_checksum(original_base) != original_base_checksum:
        raise AssertionError("Correction Edit mutated the Base Action")

    # Exit must work even after the editor selection/mode/timeline have drifted.
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    companion.select_set(True)
    bpy.context.view_layer.objects.active = companion
    bpy.context.scene.frame_set(3, subframe=0.5)
    exit_correction_edit_mode(bpy.context, armature)
    _assert_editor_state(armature, expected_state)

    # Undo/Redo can leave the logical layer intact while remapping cached RNA
    # wrappers. Stable layer IDs must remain authoritative, and cleanup must
    # preserve keys while restoring the original Timeline destination.
    remapped_session = enter_correction_edit_mode(bpy.context, armature)
    remapped_checksum = action_checksum(active_layer.action)
    session_key = correction_edit_module._session_key(armature)
    correction_edit_module._SESSIONS[session_key] = replace(
        remapped_session,
        action=first_layer.action,
        strip=first_layer.strip,
    )
    if not correction_edit_module.is_correction_editing(armature):
        raise AssertionError("Stable layer ID did not recover remapped RNA references")
    recovered_session = exit_correction_edit_mode(bpy.context, armature)
    if recovered_session.action is not active_layer.action:
        raise AssertionError("RNA recovery returned the wrong Correction Action")
    if action_checksum(active_layer.action) != remapped_checksum:
        raise AssertionError("RNA recovery changed Correction keys")
    if (
        armature.animation_data.use_tweak_mode
        or armature.animation_data.action is not None
    ):
        raise AssertionError("RNA recovery left Correction Tweak Mode active")
    if has_correction_edit_session(armature):
        raise AssertionError("RNA recovery retained the stale session")
    recovered_editor_state = _capture_editor_state(armature)
    if recovered_editor_state["dopesheetEditors"] != expected_state["dopesheetEditors"]:
        raise AssertionError("RNA recovery did not restore the original Timeline")
    _configure_pre_edit_state(armature, companion)
    stack = get_correction_layer_stack(armature)
    if stack is None:
        raise AssertionError("RNA recovery lost the Correction stack")
    stack.base_track.select = True
    armature.animation_data.nla_tracks.active = stack.base_track
    first_layer.track.select = True
    active_layer.track.select = False
    stack.base_strip.select = True
    first_layer.strip.select = False
    active_layer.strip.select = False
    unrelated.animation_data.nla_tracks.active = _unrelated_track
    _unrelated_track.select = True
    _unrelated_strip.select = True

    # If editor setup itself fails after changing the Timeline, entry must
    # leave neither a UI change nor a partially-owned Tweak session behind.
    original_show_keys = correction_edit_module._show_correction_keys_in_timeline

    def _fail_after_showing_keys(state) -> None:
        original_show_keys(state)
        raise RuntimeError("Synthetic Action Editor setup failure")

    correction_edit_module._show_correction_keys_in_timeline = _fail_after_showing_keys
    try:
        try:
            enter_correction_edit_mode(bpy.context, armature)
        except RuntimeError as exception:
            if "Synthetic Action Editor setup failure" not in str(exception):
                raise
        else:
            raise AssertionError("Synthetic Action Editor setup failure was ignored")
    finally:
        correction_edit_module._show_correction_keys_in_timeline = original_show_keys
    if (
        armature.animation_data.use_tweak_mode
        or armature.animation_data.action is not None
    ):
        raise AssertionError("Failed editor setup left Correction Tweak Mode active")
    if has_correction_edit_session(armature):
        raise AssertionError("Failed editor setup retained a Correction session")
    _assert_editor_state(armature, expected_state)
    if _capture_unrelated_nla_state(unrelated) != expected_unrelated_state:
        raise AssertionError("Correction Edit changed unrelated NLA selection/state")
    validate_layer_stack(armature).raise_for_errors()

    # Blender's native Tweak Mode exit must not leave an unrecoverable session.
    second_session = enter_correction_edit_mode(bpy.context, armature)
    if second_session.action is not active_layer.action:
        raise AssertionError("Correction Edit retained a stale session")
    with _nla_operator_context(bpy.context, armature):
        result = bpy.ops.nla.tweakmode_exit(isolate_action=False)
    if "FINISHED" not in result or armature.animation_data.use_tweak_mode:
        raise AssertionError("Fixture could not leave Tweak Mode through native UI")
    if not has_correction_edit_session(armature):
        raise AssertionError("Native Tweak exit unexpectedly discarded recovery state")
    if _idle_correction_stack(bpy.context) is not None:
        raise AssertionError("Layer CRUD remained enabled for a stale edit session")

    cancel_correction_edit_mode(bpy.context, armature)
    if has_correction_edit_session(armature):
        raise AssertionError("Recover did not clear the stale edit session")
    _assert_editor_state(armature, expected_state)

    # A clean entry after recovery proves every saved NLA ID remained valid.
    recovered_session = enter_correction_edit_mode(bpy.context, armature)
    if recovered_session.action is not active_layer.action:
        raise AssertionError("Native Tweak exit did not recover on the next Enter")
    exit_correction_edit_mode(bpy.context, armature)
    _assert_editor_state(armature, expected_state)
    if _capture_unrelated_nla_state(unrelated) != expected_unrelated_state:
        raise AssertionError("Repeated edit changed unrelated NLA selection/state")

    # A genuinely different Correction Action must remain in Tweak Mode and
    # keep every key untouched when recovery is requested for the old session.
    conflict_session = enter_correction_edit_mode(bpy.context, armature)
    conflict_checksums = {
        layer.layer_id: action_checksum(layer.action)
        for layer in get_correction_layer_stack(armature).correction_layers
    }
    with _nla_operator_context(bpy.context, armature):
        if "FINISHED" not in correction_edit_module._exit_operator():
            raise AssertionError("Could not prepare the different-layer conflict")
    current_stack = get_correction_layer_stack(armature)
    if current_stack is None:
        raise AssertionError("Conflict fixture lost its Correction stack")
    with _nla_operator_context(bpy.context, armature):
        correction_edit_module._set_target_selection(
            bpy.context,
            current_stack,
            first_layer,
        )
        if "FINISHED" not in correction_edit_module._enter_operator():
            raise AssertionError("Could not enter the different Correction layer")
    try:
        cancel_correction_edit_mode(bpy.context, armature)
    except ValueError as exception:
        if "別のNLA Action" not in str(exception):
            raise
    else:
        raise AssertionError("Recovery exited a different Correction Action")
    if (
        not armature.animation_data.use_tweak_mode
        or armature.animation_data.action is not first_layer.action
    ):
        raise AssertionError("Different-layer recovery changed the open Tweak Mode")
    for layer in get_correction_layer_stack(armature).correction_layers:
        if action_checksum(layer.action) != conflict_checksums[layer.layer_id]:
            raise AssertionError("Different-layer recovery changed Correction keys")
    with _nla_operator_context(bpy.context, armature):
        if "FINISHED" not in correction_edit_module._exit_operator():
            raise AssertionError("Could not leave the different-layer conflict")
    restored_conflict = cancel_correction_edit_mode(bpy.context, armature)
    if (
        restored_conflict is None
        or restored_conflict.layer_id != conflict_session.layer_id
    ):
        raise AssertionError("Original session was lost after different-layer conflict")
    _configure_pre_edit_state(armature, companion)

    # Scene Tweak Mode may belong to another Object. Recovery must not change
    # that object's Action, strip selection, or keys.
    object_conflict_session = enter_correction_edit_mode(bpy.context, armature)
    with _nla_operator_context(bpy.context, armature):
        if "FINISHED" not in correction_edit_module._exit_operator():
            raise AssertionError("Could not prepare the other-object conflict")
    correction_edit_module._activate_armature(bpy.context, unrelated)
    for candidate in bpy.context.view_layer.objects:
        candidate_animation_data = candidate.animation_data
        if candidate_animation_data is None:
            continue
        for track in candidate_animation_data.nla_tracks:
            track.select = False
            for strip in track.strips:
                strip.select = False
    unrelated.animation_data.nla_tracks.active = _unrelated_track
    _unrelated_track.select = True
    _unrelated_strip.select = True
    unrelated_checksum = action_checksum(_unrelated_strip.action)
    with _nla_operator_context(bpy.context, unrelated):
        if "FINISHED" not in correction_edit_module._enter_operator():
            raise AssertionError("Could not enter the other Object's Tweak Mode")
    try:
        cancel_correction_edit_mode(bpy.context, armature)
    except ValueError as exception:
        if "別のオブジェクト" not in str(exception):
            raise
    else:
        raise AssertionError("Recovery exited another Object's Tweak Mode")
    if (
        not unrelated.animation_data.use_tweak_mode
        or unrelated.animation_data.action is not _unrelated_strip.action
        or action_checksum(_unrelated_strip.action) != unrelated_checksum
    ):
        raise AssertionError("Recovery changed another Object's Tweak Mode")
    with _nla_operator_context(bpy.context, unrelated):
        if "FINISHED" not in correction_edit_module._exit_operator():
            raise AssertionError("Could not leave the other Object's Tweak Mode")
    recovered_object_conflict = cancel_correction_edit_mode(bpy.context, armature)
    if (
        recovered_object_conflict is None
        or recovered_object_conflict.layer_id != object_conflict_session.layer_id
    ):
        raise AssertionError("Original session was lost after other-object conflict")
    _configure_pre_edit_state(armature, companion)

    enter_correction_edit_mode(bpy.context, armature)
    cancel_errors = cancel_all_correction_edit_sessions(bpy.context)
    if cancel_errors:
        raise AssertionError(f"Session shutdown recovery failed: {cancel_errors!r}")
    _assert_editor_state(armature, expected_state)
    final_session = enter_correction_edit_mode(bpy.context, armature)
    if final_session.action is not active_layer.action:
        raise AssertionError("Session shutdown left stale state")
    exit_correction_edit_mode(bpy.context, armature)
    _assert_editor_state(armature, expected_state)

    # A .blend can persist Tweak Mode, but its Python session snapshot cannot
    # survive the load.  The beginner recovery must only remove that transient
    # binding and preserve every key in the Correction Action.
    persisted_session = enter_correction_edit_mode(bpy.context, armature)
    persisted_checksum = action_checksum(persisted_session.action)
    persisted_view = _capture_editor_state(armature)
    clear_correction_edit_sessions()
    if has_correction_edit_session(armature):
        raise AssertionError("Simulated file reload retained an in-memory session")
    stale_layer = stale_correction_tweak_layer(armature)
    if stale_layer is None or stale_layer.layer_id != active_layer.layer_id:
        raise AssertionError("Saved Correction Tweak Mode was not recognized")
    recovered_layer = exit_stale_correction_tweak_mode(bpy.context, armature)
    if recovered_layer.layer_id != active_layer.layer_id:
        raise AssertionError("Saved Tweak recovery returned the wrong layer")
    if (
        armature.animation_data.use_tweak_mode
        or armature.animation_data.action is not None
    ):
        raise AssertionError("Saved Tweak recovery left an active Action binding")
    if bool(getattr(bpy.context.scene, "is_nla_tweakmode", False)):
        raise AssertionError("Saved Tweak recovery left Scene Tweak Mode enabled")
    if action_checksum(recovered_layer.action) != persisted_checksum:
        raise AssertionError("Saved Tweak recovery changed Correction keys")
    recovered_view = _capture_editor_state(armature)
    for key in (
        "activeObject",
        "selectedObjects",
        "mode",
        "selectedBones",
        "activeBone",
        "frame",
        "subframe",
        "areaTypes",
    ):
        if recovered_view[key] != persisted_view[key]:
            raise AssertionError(f"Saved Tweak recovery changed editor state: {key}")
    validate_layer_stack(armature).raise_for_errors()
    resumed_session = enter_correction_edit_mode(bpy.context, armature)
    if resumed_session.action is not active_layer.action:
        raise AssertionError("Correction Edit could not resume after saved recovery")
    exit_correction_edit_mode(bpy.context, armature)

    print(
        "MOTION_BRIDGE_CORRECTION_EDIT_TEST="
        + json.dumps(
            {
                "status": "PASS",
                "activeLayerTargeted": True,
                "backgroundNlaTweak": True,
                "selectedBonesOnly": True,
                "quaternionWxyzComplete": True,
                "locationOptional": True,
                "neutralRotationIdentity": True,
                "neutralLocationZero": True,
                "baseImmutable": True,
                "stateRestored": True,
                "unrelatedRigIsolated": True,
                "repeatableSession": True,
                "nativeTweakExitRecovered": True,
                "layerCrudBlockedWhileStale": True,
                "shutdownSessionRecovered": True,
                "savedTweakModeRecovered": True,
                "savedCorrectionKeysPreserved": True,
                "timelineActionEditor": True,
                "timelineFilters": True,
                "timelineStateRestored": True,
                "timelineFailureRolledBack": True,
                "staleRnaRecovered": True,
                "differentLayerConflictPreserved": True,
                "otherObjectConflictPreserved": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
