"""Non-destructive visual bake of a Motion Bridge correction stack."""

from __future__ import annotations

from contextlib import contextmanager
import math
from typing import Callable, Iterator

import bpy

from .action_api import action_slot_for_id, iter_action_fcurves
from .comparison import configured_after_corrections
from .correction_layers import (
    PROP_ROLE,
    PROP_STACK_ID,
    validate_layer_stack,
)
from .pose_api import (
    pose_bone_selected as _pose_bone_selected,
    set_pose_bone_selected as _set_pose_bone_selected,
)


ROLE_BAKED = "BAKED"


def _assign_action(
    animation_data: bpy.types.AnimData,
    action: bpy.types.Action | None,
    slot: object | None,
) -> None:
    animation_data.action = action
    if action is not None and slot is not None:
        if hasattr(animation_data, "action_slot"):
            animation_data.action_slot = slot
        elif hasattr(animation_data, "action_slot_handle"):
            animation_data.action_slot_handle = slot.handle


def _ensure_quaternion_continuity(action: bpy.types.Action) -> None:
    """Normalize WXYZ keys and choose the nearest quaternion hemisphere."""

    by_path: dict[str, dict[int, bpy.types.FCurve]] = {}
    for curve in iter_action_fcurves(action):
        if curve.data_path == "rotation_quaternion" or curve.data_path.endswith(
            ".rotation_quaternion"
        ):
            by_path.setdefault(curve.data_path, {})[int(curve.array_index)] = curve

    for curves in by_path.values():
        if set(curves) != {0, 1, 2, 3}:
            raise ValueError("Visual bake produced incomplete quaternion channels")
        points_by_channel = {
            index: {round(float(point.co[0]), 6): point for point in curve.keyframe_points}
            for index, curve in curves.items()
        }
        shared_frames = set.intersection(
            *(set(points) for points in points_by_channel.values())
        )
        all_frames = set.union(*(set(points) for points in points_by_channel.values()))
        if shared_frames != all_frames:
            raise ValueError("Visual bake produced misaligned quaternion keyframes")
        previous: tuple[float, float, float, float] | None = None
        for frame in sorted(shared_frames):
            values = tuple(
                float(points_by_channel[index][frame].co[1]) for index in range(4)
            )
            norm = math.sqrt(sum(value * value for value in values))
            if not math.isfinite(norm) or norm <= 1e-12:
                raise ValueError(f"Visual bake produced an invalid quaternion at frame {frame}")
            values = tuple(value / norm for value in values)
            if previous is not None and sum(a * b for a, b in zip(previous, values)) < 0.0:
                values = tuple(-value for value in values)
            for index, value in enumerate(values):
                point = points_by_channel[index][frame]
                point.co[1] = value
                point.handle_left[1] = value
                point.handle_right[1] = value
                point.interpolation = "LINEAR"
            previous = values
        for curve in curves.values():
            curve.update()


def _run_visual_bake(frame_start: int, frame_end: int) -> set[str]:
    kwargs = dict(
        frame_start=frame_start,
        frame_end=frame_end,
        step=1,
        only_selected=False,
        visual_keying=True,
        clear_constraints=False,
        clear_parents=False,
        use_current_action=False,
        clean_curves=False,
        # Canonical root trajectory lives on the Armature Object while limb
        # channels live on Pose bones.  Both must land in the animation FBX.
        bake_types={"OBJECT", "POSE"},
    )
    # ``channel_types`` is available on Blender 4.5/5.x.  The fallback stays
    # here so loading a legacy build does not spread an API branch elsewhere.
    try:
        return bpy.ops.nla.bake(
            **kwargs,
            channel_types={"LOCATION", "ROTATION", "SCALE", "BBONE", "PROPS"},
        )
    except TypeError as error:
        if "channel_types" not in str(error):
            raise
        return bpy.ops.nla.bake(**kwargs)


def bake_layer_stack(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    *,
    name: str | None = None,
    _baker: Callable[[int, int], set[str]] | None = None,
) -> bpy.types.Action:
    """Visual-bake evaluated Base+Correction into a new, unbound Action.

    The original NLA stack, active Action/Slot, object selection, active object,
    mode, timeline range, and current subframe are restored in ``finally``.
    The returned Action is not assigned to the armature and can be passed to
    :func:`activate_action_for_export` for the existing FBX exporter.
    """

    validation = validate_layer_stack(armature)
    validation.raise_for_errors()
    assert validation.stack is not None
    stack = validation.stack
    animation_data = armature.animation_data
    assert animation_data is not None
    if getattr(animation_data, "use_tweak_mode", False):
        raise ValueError("Exit NLA Tweak Mode before visual bake")
    if animation_data.action is not None:
        raise ValueError("Visual bake requires no active Action above the NLA stack")
    if context.view_layer.objects.get(armature.name) is not armature:
        raise ValueError("Armature must belong to the current View Layer")

    source_range = tuple(float(value) for value in stack.base_action.frame_range)
    frame_start = math.floor(source_range[0])
    frame_end = math.ceil(source_range[1])
    if frame_end < frame_start:
        raise ValueError("Base Action frame range is reversed")

    view_layer = context.view_layer
    scene = context.scene
    previous_active = view_layer.objects.active
    previous_selected = tuple(context.selected_objects)
    previous_mode = previous_active.mode if previous_active is not None else None
    previous_active_bone = armature.data.bones.active
    previous_bone_selection = tuple(
        (pose_bone, _pose_bone_selected(pose_bone))
        for pose_bone in armature.pose.bones
    )
    previous_action = animation_data.action
    previous_slot = getattr(animation_data, "action_slot", None)
    previous_last_slot_identifier = getattr(
        animation_data,
        "last_slot_identifier",
        None,
    )
    previous_tweak_slot_handle = getattr(
        animation_data,
        "action_slot_handle_tweak_storage",
        None,
    )
    previous_frame_start = scene.frame_start
    previous_frame_end = scene.frame_end
    previous_frame = scene.frame_current
    previous_subframe = scene.frame_subframe
    previous_tracks = tuple(
        (track, track.mute, track.lock, track.select, track.is_solo)
        for track in animation_data.nla_tracks
    )
    action_pointers_before = {action.as_pointer() for action in bpy.data.actions}
    armature_data = armature.data
    baked_action: bpy.types.Action | None = None

    def target_object_is_live() -> bool:
        return any(candidate is armature for candidate in bpy.data.objects)

    def target_is_live() -> bool:
        if not target_object_is_live():
            return False
        if not any(candidate is armature for candidate in view_layer.objects):
            return False
        try:
            return (
                armature.type == "ARMATURE"
                and armature.data is armature_data
                and any(candidate is armature_data for candidate in bpy.data.armatures)
            )
        except ReferenceError:
            return False

    def cleanup_pending_action() -> None:
        candidates: list[bpy.types.Action] = []
        if baked_action is not None:
            candidates.append(baked_action)
        if target_object_is_live():
            active_action = animation_data.action
            if (
                active_action is not None
                and active_action.as_pointer() not in action_pointers_before
                and all(candidate is not active_action for candidate in candidates)
            ):
                candidates.append(active_action)
        for action in candidates:
            if not any(candidate is action for candidate in bpy.data.actions):
                continue
            if target_object_is_live() and animation_data.action is action:
                _assign_action(animation_data, None, None)
            action.use_fake_user = False
            if action.users == 0:
                bpy.data.actions.remove(action)

    try:
        if previous_active is not None and previous_active.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        for selected in tuple(context.selected_objects):
            selected.select_set(False)
        armature.select_set(True)
        view_layer.objects.active = armature
        bpy.ops.object.mode_set(mode="POSE")

        scene.frame_start = frame_start
        scene.frame_end = frame_end
        with configured_after_corrections(armature):
            scene.frame_set(frame_start)
            result = (_baker or _run_visual_bake)(frame_start, frame_end)
            if "FINISHED" not in result:
                raise RuntimeError(f"Visual bake failed: {result}")
            baked_action = animation_data.action
            if baked_action is None or not any(iter_action_fcurves(baked_action)):
                raise RuntimeError("Visual bake produced no Action curves")

            baked_action.name = name or f"MB_BAKED::{stack.base_action.name}"
            baked_action[PROP_ROLE] = ROLE_BAKED
            baked_action[PROP_STACK_ID] = armature[PROP_STACK_ID]
            baked_action["motion_bridge_bake_frame_start"] = frame_start
            baked_action["motion_bridge_bake_frame_end"] = frame_end
            _ensure_quaternion_continuity(baked_action)
            baked_action.use_fake_user = True
    except Exception:
        cleanup_pending_action()
        raise
    finally:
        try:
            if (
                view_layer.objects.active is not None
                and view_layer.objects.active.mode != "OBJECT"
            ):
                bpy.ops.object.mode_set(mode="OBJECT")
            if target_is_live():
                _assign_action(animation_data, previous_action, previous_slot)
                if previous_last_slot_identifier is not None:
                    animation_data.last_slot_identifier = previous_last_slot_identifier
                if previous_tweak_slot_handle is not None:
                    animation_data.action_slot_handle_tweak_storage = (
                        previous_tweak_slot_handle
                    )
                for track, mute, lock, select, is_solo in previous_tracks:
                    track.mute = mute
                    track.lock = lock
                    track.select = select
                    track.is_solo = is_solo

            for selected in tuple(context.selected_objects):
                selected.select_set(False)
            for selected in previous_selected:
                if any(candidate is selected for candidate in view_layer.objects):
                    selected.select_set(True)
            if previous_active is not None and any(
                candidate is previous_active for candidate in view_layer.objects
            ):
                view_layer.objects.active = previous_active
            else:
                view_layer.objects.active = None

            scene.frame_start = previous_frame_start
            scene.frame_end = previous_frame_end
            scene.frame_set(previous_frame, subframe=previous_subframe)
            target_survived = target_is_live()
            if target_survived:
                for pose_bone, selected in previous_bone_selection:
                    _set_pose_bone_selected(pose_bone, selected)
                armature.data.bones.active = previous_active_bone
            if (
                previous_active is not None
                and view_layer.objects.active is previous_active
                and previous_mode is not None
                and previous_active.mode != previous_mode
            ):
                bpy.ops.object.mode_set(mode=previous_mode)
            if not target_survived:
                raise RuntimeError(
                    "Correction rig was removed before visual bake could finish"
                )
        except Exception:
            cleanup_pending_action()
            raise

    assert baked_action is not None
    return baked_action


@contextmanager
def activate_action_for_export(
    armature: bpy.types.Object,
    action: bpy.types.Action,
) -> Iterator[bpy.types.Action]:
    """Temporarily bind one baked Action and silence NLA during FBX export."""

    validation = validate_layer_stack(armature)
    validation.raise_for_errors()
    animation_data = armature.animation_data
    assert animation_data is not None
    if action.get(PROP_ROLE) != ROLE_BAKED:
        raise ValueError("Export Action was not created by bake_layer_stack")
    if action.get(PROP_STACK_ID) != armature.get(PROP_STACK_ID):
        raise ValueError("Export Action belongs to another Correction stack")
    slot = action_slot_for_id(action, armature)
    if getattr(action, "slots", None) and slot is None:
        raise ValueError("Baked Action has no unambiguous armature Action Slot")

    previous_action = animation_data.action
    previous_slot = getattr(animation_data, "action_slot", None)
    previous_last_slot_identifier = getattr(
        animation_data,
        "last_slot_identifier",
        None,
    )
    previous_tweak_slot_handle = getattr(
        animation_data,
        "action_slot_handle_tweak_storage",
        None,
    )
    previous_tracks = tuple(
        (track, track.mute, track.is_solo) for track in animation_data.nla_tracks
    )
    try:
        for track, _mute, _is_solo in previous_tracks:
            track.is_solo = False
            track.mute = True
        _assign_action(animation_data, action, slot)
        yield action
    finally:
        _assign_action(animation_data, previous_action, previous_slot)
        if previous_last_slot_identifier is not None:
            animation_data.last_slot_identifier = previous_last_slot_identifier
        if previous_tweak_slot_handle is not None:
            animation_data.action_slot_handle_tweak_storage = previous_tweak_slot_handle
        for track, mute, is_solo in previous_tracks:
            track.mute = mute
            track.is_solo = is_solo
