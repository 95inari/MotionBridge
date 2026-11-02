from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Callable

import bpy


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))
TOOLS_ROOT = Path(__file__).resolve().parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from motion_bridge import addon  # noqa: E402
from motion_bridge.canonical.rig_spec import SKELETON_ID  # noqa: E402
from motion_bridge.editing.correction_layers import (  # noqa: E402
    action_checksum,
    get_correction_layer_stack,
    validate_layer_stack,
)
from motion_bridge.retargeting.profile import HUMANOID_MAPPING_FIELDS  # noqa: E402
from test_source_retarget import (  # noqa: E402
    FIRST_FRAME,
    LAST_FRAME,
    _create_armature as _create_motion_armature,
    _key_source_motion,
)


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for action in tuple(bpy.data.actions):
        bpy.data.actions.remove(action)


def _create_canonical() -> bpy.types.Object:
    data = bpy.data.armatures.new("MB_Phase0_Undo_Data")
    armature = bpy.data.objects.new("MB_Phase0_Undo", data)
    bpy.context.scene.collection.objects.link(armature)
    armature["motion_bridge_skeleton_id"] = SKELETON_ID
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode="EDIT")
    hips = data.edit_bones.new("Hips")
    hips.head = (0.0, 0.0, 0.0)
    hips.tail = (0.0, 0.0, 1.0)
    bpy.ops.object.mode_set(mode="POSE")
    pose_bone = armature.pose.bones["Hips"]
    pose_bone.rotation_mode = "QUATERNION"
    for frame in (1.0, 2.0):
        pose_bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        pose_bone.keyframe_insert(
            data_path="rotation_quaternion",
            frame=frame,
            group="Hips",
        )
    bpy.ops.object.mode_set(mode="OBJECT")
    return armature


def _history_operator(operator) -> set[str]:
    screen = bpy.context.screen
    window = bpy.context.window
    if screen is None or window is None:
        raise AssertionError("Blender UI context is unavailable for Undo/Redo")
    area = next((item for item in screen.areas if item.type == "VIEW_3D"), None)
    if area is None:
        raise AssertionError("VIEW_3D context is unavailable for Undo/Redo")
    region = next((item for item in area.regions if item.type == "WINDOW"), None)
    if region is None:
        raise AssertionError("VIEW_3D Window region is unavailable for Undo/Redo")
    with bpy.context.temp_override(
        window=window,
        screen=screen,
        area=area,
        region=region,
    ):
        return operator()


def _semantic_state(armature_name: str) -> dict[str, object]:
    armature = bpy.data.objects.get(armature_name)
    if armature is None:
        raise AssertionError(f"Undo state lost {armature_name}")
    animation_data = armature.animation_data
    active_action = animation_data.action if animation_data is not None else None
    active_slot = (
        getattr(animation_data, "action_slot", None)
        if animation_data is not None
        else None
    )
    tracks = ()
    if animation_data is not None:
        tracks = tuple(
            (
                track.name,
                bool(track.mute),
                bool(track.lock),
                tuple(
                    (
                        strip.name,
                        strip.action.name if strip.action is not None else None,
                        str(
                            getattr(
                                getattr(strip, "action_slot", None),
                                "identifier",
                                "",
                            )
                        ),
                    )
                    for strip in track.strips
                ),
            )
            for track in animation_data.nla_tracks
        )
    scene = bpy.context.scene
    return {
        "activeObject": (
            bpy.context.active_object.name
            if bpy.context.active_object is not None
            else None
        ),
        "selectedObjects": tuple(
            sorted(obj.name for obj in bpy.context.selected_objects)
        ),
        "mode": bpy.context.object.mode if bpy.context.object is not None else None,
        "frame": (scene.frame_current, round(float(scene.frame_subframe), 6)),
        "frameRange": (scene.frame_start, scene.frame_end),
        "activeAction": active_action.name if active_action is not None else None,
        "activeSlot": str(getattr(active_slot, "identifier", "")),
        "stackId": armature.get("motion_bridge_stack_id"),
        "tracks": tracks,
    }


def _mapping_state(source_name: str) -> dict[str, object]:
    scene = bpy.context.scene
    settings = scene.motion_bridge_session
    source = bpy.data.objects.get(source_name)
    return {
        "activeObject": (
            bpy.context.active_object.name
            if bpy.context.active_object is not None
            else None
        ),
        "selectedObjects": tuple(
            sorted(obj.name for obj in bpy.context.selected_objects)
        ),
        "frame": (scene.frame_current, round(float(scene.frame_subframe), 6)),
        "frameRange": (scene.frame_start, scene.frame_end),
        "preset": settings.retarget_preset,
        "source": (
            settings.source_armature.name
            if settings.source_armature is not None
            else None
        ),
        "boundSource": (
            settings.manual_mapping_bound_source.name
            if settings.manual_mapping_bound_source is not None
            else None
        ),
        "mapping": tuple(
            (
                field.property_name,
                getattr(settings.manual_mapping, field.property_name),
            )
            for field in HUMANOID_MAPPING_FIELDS
        ),
        "profile": str(settings.manual_mapping_profile_json),
        "registry": str(settings.manual_mapping_profiles_json),
        "sourceIdentity": (
            None
            if source is None
            else (
                source.get("motion_bridge_mapping_source_instance_id"),
                source.get("motion_bridge_mapping_source_action_checksum"),
            )
        ),
    }


def _bake_state(target_name: str) -> dict[str, object]:
    target = bpy.data.objects.get(target_name)
    if target is None:
        raise AssertionError(f"Bake state lost {target_name}")
    animation_data = target.animation_data
    action = animation_data.action if animation_data is not None else None
    slot = (
        getattr(animation_data, "action_slot", None)
        if animation_data is not None
        else None
    )
    state = _mapping_state(
        bpy.context.scene.motion_bridge_session.source_armature.name
    )
    state.update(
        {
            "target": target.name,
            "rotationMode": target.rotation_mode,
            "action": action.name if action is not None else None,
            "actionChecksum": (
                action_checksum(action) if action is not None else None
            ),
            "actionSlot": str(getattr(slot, "identifier", "")),
            "nlaTracks": (
                len(animation_data.nla_tracks)
                if animation_data is not None
                else 0
            ),
        }
    )
    return state


def _assert_undo_redo_cycles(
    label: str,
    baseline: dict[str, object],
    result: dict[str, object],
    state: Callable[[], dict[str, object]],
    *,
    validate_redo: Callable[[], None] | None = None,
) -> None:
    for cycle in range(10):
        if _history_operator(bpy.ops.ed.undo) != {"FINISHED"}:
            raise AssertionError(f"{label} Undo failed at {cycle + 1}")
        if state() != baseline:
            raise AssertionError(f"{label} Undo state drifted at {cycle + 1}")
        if _history_operator(bpy.ops.ed.redo) != {"FINISHED"}:
            raise AssertionError(f"{label} Redo failed at {cycle + 1}")
        if state() != result:
            raise AssertionError(f"{label} Redo state drifted at {cycle + 1}")
        if validate_redo is not None:
            validate_redo()


def _push_history_checkpoint(label: str) -> None:
    if bpy.ops.ed.undo_push(message=label) != {"FINISHED"}:
        raise AssertionError(f"Could not create Undo checkpoint: {label}")


def main() -> None:
    _clear_scene()
    addon.register()
    registered = True
    history_tested = not bpy.app.background
    original_global_undo = bpy.context.preferences.edit.use_global_undo
    try:
        undo_operators = (
            addon.MB_OT_CreateCorrectionLayer,
            addon.MB_OT_PopulateManualMapping,
            addon.MB_OT_ClearManualMapping,
            addon.MB_OT_BakeSourceToCanonical,
        )
        missing = [
            operator.__name__
            for operator in undo_operators
            if "UNDO" not in operator.bl_options
        ]
        if missing:
            raise AssertionError(
                "Mutation operators without UNDO: " + ", ".join(missing)
            )

        if history_tested:
            bpy.context.preferences.edit.use_global_undo = True
        armature = _create_canonical()
        armature_name = armature.name
        companion = bpy.data.objects.new("MB_Phase0_Undo_Companion", None)
        bpy.context.scene.collection.objects.link(companion)
        companion.select_set(True)
        bpy.context.scene.frame_start = 3
        bpy.context.scene.frame_end = 42
        bpy.context.scene.frame_set(7, subframe=0.25)
        baseline_state = _semantic_state(armature_name)
        if history_tested:
            _push_history_checkpoint("Motion Bridge Phase 0 baseline")
        result = bpy.ops.motion_bridge.create_correction_layer()
        if "FINISHED" not in result:
            raise AssertionError(f"Correction stack operator failed: {result}")
        stack = get_correction_layer_stack(bpy.data.objects[armature_name])
        if stack is None:
            raise AssertionError("Correction stack was not created")
        validate_layer_stack(bpy.data.objects[armature_name]).raise_for_errors()
        result_state = _semantic_state(armature_name)

        if history_tested:
            # Blender does not append an operator's automatic UNDO step while a
            # startup --python script is still executing. Explicitly bracket the
            # already-verified UNDO operator with checkpoints so this foreground
            # regression can exercise Blender's serialized state repeatedly.
            _push_history_checkpoint("Motion Bridge Phase 0 result")

            def validate_stack_redo() -> None:
                redone = bpy.data.objects.get(armature_name)
                if redone is None or get_correction_layer_stack(redone) is None:
                    raise AssertionError("Redo did not restore the Correction stack")
                validate_layer_stack(redone).raise_for_errors()

            _assert_undo_redo_cycles(
                "Correction stack",
                baseline_state,
                result_state,
                lambda: _semantic_state(armature_name),
                validate_redo=validate_stack_redo,
            )
            restored = bpy.data.objects.get(armature_name)
            if restored is None or get_correction_layer_stack(restored) is None:
                raise AssertionError("Correction cycles did not finish at Redo state")

            _clear_scene()
            target, _target_mapping = _create_motion_armature(
                "MB_Phase0_Undo_Target",
                scale=1.0,
                source=False,
            )
            target_name = target.name
            source, source_mapping = _create_motion_armature(
                "MB_Phase0_Undo_Source",
                scale=1.0,
                source=True,
            )
            source_name = source.name
            _key_source_motion(source, source_mapping)
            for selected in tuple(bpy.context.selected_objects):
                selected.select_set(False)
            target.select_set(True)
            bpy.context.view_layer.objects.active = target
            scene = bpy.context.scene
            settings = scene.motion_bridge_session
            settings.source_armature = source
            settings.retarget_preset = "AUTO"
            scene.frame_start = FIRST_FRAME
            scene.frame_end = LAST_FRAME
            scene.frame_set((FIRST_FRAME + LAST_FRAME) // 2, subframe=0.25)
            populate_baseline = _mapping_state(source_name)
            _push_history_checkpoint("Motion Bridge Populate baseline")
            if bpy.ops.motion_bridge.populate_manual_mapping() != {"FINISHED"}:
                raise AssertionError("Populate operator failed in Undo test")
            populate_result = _mapping_state(source_name)
            _push_history_checkpoint("Motion Bridge Populate result")
            _assert_undo_redo_cycles(
                "Populate mapping",
                populate_baseline,
                populate_result,
                lambda: _mapping_state(source_name),
            )

            clear_baseline = _mapping_state(source_name)
            _push_history_checkpoint("Motion Bridge Clear baseline")
            if bpy.ops.motion_bridge.clear_manual_mapping() != {"FINISHED"}:
                raise AssertionError("Clear operator failed in Undo test")
            clear_result = _mapping_state(source_name)
            _push_history_checkpoint("Motion Bridge Clear result")
            _assert_undo_redo_cycles(
                "Clear mapping",
                clear_baseline,
                clear_result,
                lambda: _mapping_state(source_name),
            )

            settings = bpy.context.scene.motion_bridge_session
            settings.retarget_preset = "AUTO"
            bake_baseline = _bake_state(target_name)
            _push_history_checkpoint("Motion Bridge Bake baseline")
            if bpy.ops.motion_bridge.bake_source_to_canonical() != {"FINISHED"}:
                raise AssertionError("Bake operator failed in Undo test")
            bake_result = _bake_state(target_name)
            _push_history_checkpoint("Motion Bridge Bake result")

            def validate_bake_redo() -> None:
                redone_target = bpy.data.objects.get(target_name)
                animation_data = (
                    redone_target.animation_data
                    if redone_target is not None
                    else None
                )
                if animation_data is None or animation_data.action is None:
                    raise AssertionError("Bake Redo did not restore its Action binding")

            _assert_undo_redo_cycles(
                "Bake",
                bake_baseline,
                bake_result,
                lambda: _bake_state(target_name),
                validate_redo=validate_bake_redo,
            )
    finally:
        bpy.context.preferences.edit.use_global_undo = original_global_undo
        if registered:
            addon.unregister()

    print(
        "MOTION_BRIDGE_PHASE0_UNDO_TEST="
        + json.dumps(
            {
                "status": "PASS",
                "mutationOperatorsDeclareUndo": True,
                "correctionStackUndoRedo": history_tested,
                "populateMappingUndoRedo": history_tested,
                "clearMappingUndoRedo": history_tested,
                "bakeUndoRedo": history_tested,
                "historySkippedInBackground": not history_tested,
                "undoRedoCycles": 10 if history_tested else 0,
                "selectionFrameAndSlotsVerified": history_tested,
                "automatedHistoryUsesExplicitCheckpoints": history_tested,
                "uiAutomaticUndoStepRequiresManualAcceptance": True,
            },
            sort_keys=True,
        )
    )


def _quit_foreground_after_script() -> None:
    """Quit after the startup script returns to Blender's event loop."""

    bpy.ops.wm.quit_blender()
    return None


if __name__ == "__main__":
    main()
    if not bpy.app.background:
        # Calling quit directly while --python is still unwinding is unreliable
        # after repeated history restores. The zero-delay timer runs at the next
        # event-loop boundary and gives the runner a deterministic process exit.
        bpy.app.timers.register(_quit_foreground_after_script, first_interval=0.0)
