"""Safe NLA tweak-mode editing for sparse Motion Bridge corrections.

Blender's NLA tweak operator is intentionally kept behind this module.  The
public functions preserve editor state, target a stable Correction layer, and
only write complete quaternion/location channel sets to that layer's Action.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import math
from typing import Callable, Iterator

import bpy

from .action_api import (
    ActionChannelAccess,
    insert_location_key,
    insert_quaternion_key,
    iter_action_fcurves,
    remove_fcurve,
    remove_location_key,
    remove_location_keys_in_range,
    remove_quaternion_key,
    remove_quaternion_keys_in_range,
)
from .comparison import corrections_preview_enabled
from .pose_api import pose_bone_selected, set_pose_bone_selected
from .correction_layers import (
    CorrectionLayer,
    CorrectionLayerStack,
    get_correction_layer_stack,
    require_unshared_correction_actions,
    validate_layer_stack,
)


_FRAME_EPSILON = 1e-6
_KEY_INTERPOLATIONS = frozenset({"CONSTANT", "LINEAR", "BEZIER"})
_LOCATION_CORRECTION_BONE = "Hips"


@dataclass(frozen=True, slots=True)
class _NlaSelectionState:
    animation_data: bpy.types.AnimData
    active_track: bpy.types.NlaTrack | None
    track_selection: tuple[tuple[bpy.types.NlaTrack, bool], ...]
    strip_selection: tuple[tuple[bpy.types.NlaStrip, bool], ...]


@dataclass(frozen=True, slots=True)
class _TimelineEditorState:
    area: bpy.types.Area
    mode: str
    show_only_selected: bool
    show_only_slot_of_active_object: bool | None


@dataclass(frozen=True, slots=True)
class _EditorState:
    active_object: bpy.types.Object | None
    selected_objects: tuple[bpy.types.Object, ...]
    mode: str
    selected_bones: tuple[str, ...]
    active_bone: str | None
    frame: int
    subframe: float
    action: bpy.types.Action | None
    action_slot: object | None
    nla_selection: tuple[_NlaSelectionState, ...]
    timeline_editor: _TimelineEditorState | None


@dataclass(frozen=True, slots=True)
class _KeyframePointState:
    co: tuple[float, float]
    handle_left: tuple[float, float]
    handle_right: tuple[float, float]
    handle_left_type: str
    handle_right_type: str
    interpolation: str
    easing: str
    amplitude: float
    back: float
    period: float
    keyframe_type: str
    select_control_point: bool
    select_left_handle: bool
    select_right_handle: bool


@dataclass(frozen=True, slots=True)
class _CurveKeyState:
    curve: bpy.types.FCurve
    points: tuple[_KeyframePointState, ...]


@dataclass(frozen=True, slots=True)
class CorrectionEditSession:
    """One active stable-ID Correction edit and its restoration snapshot."""

    layer_id: str
    action: bpy.types.Action
    strip: bpy.types.NlaStrip
    _armature: bpy.types.Object
    _state: _EditorState
    _slot_handle: int | None
    _rna_remapped: bool = False


_SESSIONS: dict[int, CorrectionEditSession] = {}


def _session_key(armature: bpy.types.Object) -> int:
    try:
        return int(armature.as_pointer())
    except (ReferenceError, TypeError):
        return 0


def clear_correction_edit_sessions() -> None:
    """Forget snapshots after file load invalidates their Blender IDs."""

    _SESSIONS.clear()


def has_correction_edit_session(armature: bpy.types.Object | None) -> bool:
    """Return whether Motion Bridge still owns an edit snapshot for this rig."""

    return bool(
        armature is not None
        and armature.type == "ARMATURE"
        and _SESSIONS.get(_session_key(armature)) is not None
    )


def stale_correction_tweak_layer(
    armature: bpy.types.Object | None,
) -> CorrectionLayer | None:
    """Return the Correction layer left in Tweak Mode across a file reload.

    Blender persists NLA Tweak Mode in ``.blend`` files, while the in-memory
    editor snapshot intentionally cannot survive a reload.  Only recognize an
    exact Action/strip binding from a valid Motion Bridge stack; unrelated NLA
    sessions must continue to require an explicit native Blender exit.
    """

    if (
        armature is None
        or armature.type != "ARMATURE"
        or has_correction_edit_session(armature)
    ):
        return None
    animation_data = armature.animation_data
    if (
        animation_data is None
        or not animation_data.use_tweak_mode
        or animation_data.action is None
    ):
        return None
    try:
        stack = _require_stack(armature)
    except (ReferenceError, RuntimeError, TypeError, ValueError):
        return None
    matches = tuple(
        layer
        for layer in stack.correction_layers
        if layer.action is animation_data.action
        and layer.strip.action is animation_data.action
    )
    if len(matches) != 1:
        return None
    layer = matches[0]
    active_slot = getattr(animation_data, "action_slot", None)
    strip_slot = getattr(layer.strip, "action_slot", None)
    if strip_slot is not None and (
        active_slot is None
        or getattr(active_slot, "handle", None) != getattr(strip_slot, "handle", None)
    ):
        return None
    return layer


def exit_stale_correction_tweak_mode(
    context: bpy.types.Context,
    armature: bpy.types.Object,
) -> CorrectionLayer:
    """Exit a persisted Correction Tweak Mode without touching its Action.

    No edit-session snapshot exists after a file reload, so this restores the
    currently visible editor state except for the stale Action binding itself.
    The NLA strip keeps the Action and all saved Correction keys.
    """

    layer = stale_correction_tweak_layer(armature)
    if layer is None:
        raise ValueError("保存されたMotion Bridgeの修正モードではありません")
    animation_data = armature.animation_data
    if animation_data is None:
        raise ValueError("修正モードの復旧に必要なアニメーションデータがありません")
    action = layer.action
    layer_id = layer.layer_id
    # The active Action is only Blender's transient Tweak binding.  Rebinding
    # it while restoring the UI would layer the Correction twice, so explicitly
    # restore ``None`` after the native exit.
    state = replace(
        _capture_editor_state(context, armature),
        action=None,
        action_slot=None,
    )
    _activate_armature(context, armature)
    exited = False
    try:
        with _nla_operator_context(context, armature):
            if "FINISHED" not in _exit_operator():
                raise RuntimeError(
                    "保存された修正モードをBlenderが終了できませんでした"
                )
        exited = not animation_data.use_tweak_mode
        if (
            not exited
            or animation_data.action is not None
            or bool(getattr(context.scene, "is_nla_tweakmode", False))
        ):
            raise RuntimeError("保存された修正モードを完全に終了できませんでした")
    finally:
        if exited:
            _restore_editor_state(context, armature, state)

    recovered = _require_stack(armature)
    matching_layers = tuple(
        candidate
        for candidate in recovered.correction_layers
        if candidate.layer_id == layer_id and candidate.action is action
    )
    if len(matching_layers) != 1 or matching_layers[0].strip.action is not action:
        raise RuntimeError("復旧中に修正Actionとの関連付けが変化しました")
    return matching_layers[0]


def cancel_correction_edit_mode(
    context: bpy.types.Context,
    armature: bpy.types.Object,
) -> CorrectionEditSession | None:
    """Leave an owned tweak session without validating edited key content.

    This is the recovery path for Blender's native Tweak Mode exit and add-on
    shutdown.  It restores the snapshot when native UI actions have already
    ended tweak mode, instead of leaving a stale session that blocks re-entry.
    """

    key = _session_key(armature)
    session = _SESSIONS.get(key)
    if session is None:
        return None
    animation_data = armature.animation_data
    if animation_data is None:
        raise ValueError("修正作業の復元に必要なアニメーションデータがありません")

    layer = _resolve_session_layer(armature, session)
    stale_references = session._rna_remapped or not _session_references_match_layer(
        session, layer
    )
    restore_state = _recovery_editor_state(
        context,
        armature,
        session,
        stale_references=stale_references,
    )
    if animation_data.use_tweak_mode:
        _require_current_session_tweak(context, armature, session, layer)
        _exit_owned_tweak_mode(context, armature)
    elif bool(getattr(context.scene, "is_nla_tweakmode", False)):
        raise ValueError(
            "別のオブジェクトでNLA Tweak Modeが開いています。先にそちらを終了してください"
        )

    recovered_session = replace(
        session,
        action=layer.action,
        strip=layer.strip,
        _rna_remapped=stale_references,
    )
    _restore_editor_state(context, armature, restore_state)
    _SESSIONS.pop(key, None)
    return recovered_session


def cancel_all_correction_edit_sessions(context: bpy.types.Context) -> tuple[str, ...]:
    """Best-effort cancellation for add-on unregister."""

    errors: list[str] = []
    for key, session in tuple(_SESSIONS.items()):
        try:
            cancel_correction_edit_mode(context, session._armature)
        except Exception as exception:
            errors.append(str(exception))
            _SESSIONS.pop(key, None)
    return tuple(errors)


def _require_stack(armature: bpy.types.Object) -> CorrectionLayerStack:
    validation = validate_layer_stack(armature, check_content=False)
    validation.raise_for_errors()
    if validation.stack is None:
        raise ValueError("Armature has no resolvable Motion Bridge Correction stack")
    return validation.stack


def _layer_for_id(
    stack: CorrectionLayerStack,
    layer_id: str,
) -> CorrectionLayer:
    for layer in stack.correction_layers:
        if layer.layer_id == layer_id:
            return layer
    raise ValueError("Correction layer id does not belong to this stack")


def _resolve_session_layer(
    armature: bpy.types.Object,
    session: CorrectionEditSession,
) -> CorrectionLayer:
    """Resolve a session through persisted IDs instead of stale RNA wrappers."""

    stack = _require_stack(armature)
    layer = _layer_for_id(stack, session.layer_id)
    if layer.strip.action is not layer.action:
        raise ValueError("修正レイヤーとActionの関連付けが変化しています")
    strip_slot_handle = getattr(
        getattr(layer.strip, "action_slot", None),
        "handle",
        None,
    )
    if strip_slot_handle != session._slot_handle:
        raise ValueError("修正レイヤーのAction Slotが編集開始時から変化しています")
    return layer


def _session_references_match_layer(
    session: CorrectionEditSession,
    layer: CorrectionLayer,
) -> bool:
    """Return whether cached RNA wrappers still describe the resolved layer."""

    try:
        return int(session.action.as_pointer()) == int(
            layer.action.as_pointer()
        ) and int(session.strip.as_pointer()) == int(layer.strip.as_pointer())
    except (ReferenceError, TypeError):
        return False


def _require_current_session_tweak(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    session: CorrectionEditSession,
    layer: CorrectionLayer,
) -> None:
    """Prove that the open Tweak binding is the session's exact layer."""

    animation_data = armature.animation_data
    if animation_data is None or not animation_data.use_tweak_mode:
        raise ValueError("Motion Bridgeの修正モードは開いていません")
    if not bool(getattr(context.scene, "is_nla_tweakmode", False)):
        raise ValueError("NLA Tweak Modeの状態が一致しないため安全に復元できません")
    if (
        animation_data.action is not layer.action
        or layer.strip.action is not animation_data.action
    ):
        raise ValueError(
            "別のNLA Actionが編集中です。先にそのTweak Modeを終了してください"
        )
    active_slot_handle = getattr(
        getattr(animation_data, "action_slot", None),
        "handle",
        None,
    )
    strip_slot_handle = getattr(
        getattr(layer.strip, "action_slot", None),
        "handle",
        None,
    )
    if (
        active_slot_handle != session._slot_handle
        or strip_slot_handle != session._slot_handle
    ):
        raise ValueError("編集中のAction Slotが修正作業と一致しません")


def _usable_timeline_restore_state(
    context: bpy.types.Context,
    state: _TimelineEditorState | None,
) -> _TimelineEditorState | None:
    if state is None or context.screen is None:
        return None
    try:
        pointer = int(state.area.as_pointer())
        available = any(
            int(area.as_pointer()) == pointer for area in context.screen.areas
        )
        return state if available and state.area.type == "DOPESHEET_EDITOR" else None
    except (ReferenceError, TypeError):
        return None


def _recovery_editor_state(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    session: CorrectionEditSession,
    *,
    stale_references: bool,
) -> _EditorState:
    if not stale_references:
        return session._state
    # Undo/Redo can remap Action, Strip, Track, and selection RNA wrappers. Use
    # a fresh snapshot for safe cleanup while retaining the original Timeline
    # destination when its Area itself is still alive.
    current = _capture_editor_state(context, armature)
    timeline_editor = _usable_timeline_restore_state(
        context,
        session._state.timeline_editor,
    )
    return replace(
        current,
        action=None,
        action_slot=None,
        timeline_editor=timeline_editor,
    )


def _capture_editor_state(
    context: bpy.types.Context,
    armature: bpy.types.Object,
) -> _EditorState:
    animation_data = armature.animation_data
    if animation_data is None:
        raise ValueError("Correction Edit requires armature animation data")
    active_object = context.view_layer.objects.active
    mode = active_object.mode if active_object is not None else "OBJECT"
    active_bone = armature.data.bones.active
    nla_selection = tuple(
        _NlaSelectionState(
            candidate.animation_data,
            candidate.animation_data.nla_tracks.active,
            tuple(
                (track, bool(track.select))
                for track in candidate.animation_data.nla_tracks
            ),
            tuple(
                (strip, bool(strip.select))
                for track in candidate.animation_data.nla_tracks
                for strip in track.strips
            ),
        )
        for candidate in context.view_layer.objects
        if candidate.animation_data is not None and candidate.animation_data.nla_tracks
    )
    timeline_editor = _capture_smallest_timeline_editor(context)
    return _EditorState(
        active_object,
        tuple(context.selected_objects),
        mode,
        tuple(bone.name for bone in armature.pose.bones if pose_bone_selected(bone)),
        active_bone.name if active_bone is not None else None,
        int(context.scene.frame_current),
        float(context.scene.frame_subframe),
        animation_data.action,
        getattr(animation_data, "action_slot", None),
        nla_selection,
        timeline_editor,
    )


def _capture_smallest_timeline_editor(
    context: bpy.types.Context,
) -> _TimelineEditorState | None:
    screen = context.screen
    if screen is None:
        return None
    candidates: list[tuple[bpy.types.Area, bpy.types.SpaceDopeSheetEditor]] = []
    for area in screen.areas:
        if area.type != "DOPESHEET_EDITOR":
            continue
        space = area.spaces.active
        if getattr(space, "mode", None) == "TIMELINE":
            candidates.append((area, space))
    if not candidates:
        return None
    area, space = min(
        candidates,
        key=lambda item: (
            int(item[0].width) * int(item[0].height),
            int(item[0].height),
            int(item[0].width),
        ),
    )
    dopesheet = space.dopesheet
    active_slot_filter = (
        bool(dopesheet.show_only_slot_of_active_object)
        if hasattr(dopesheet, "show_only_slot_of_active_object")
        else None
    )
    return _TimelineEditorState(
        area,
        str(space.mode),
        bool(dopesheet.show_only_selected),
        active_slot_filter,
    )


def _show_correction_keys_in_timeline(
    state: _TimelineEditorState | None,
) -> None:
    if state is None:
        return
    area = state.area
    if area.type != "DOPESHEET_EDITOR":
        return
    space = area.spaces.active
    space.mode = "ACTION"
    dopesheet = space.dopesheet
    dopesheet.show_only_selected = True
    if hasattr(dopesheet, "show_only_slot_of_active_object"):
        dopesheet.show_only_slot_of_active_object = True
    area.tag_redraw()


def _restore_timeline_editor(state: _TimelineEditorState | None) -> None:
    if state is None:
        return
    area = state.area
    try:
        if area.type != "DOPESHEET_EDITOR":
            return
        space = area.spaces.active
        space.mode = state.mode
        dopesheet = space.dopesheet
        dopesheet.show_only_selected = state.show_only_selected
        if state.show_only_slot_of_active_object is not None and hasattr(
            dopesheet, "show_only_slot_of_active_object"
        ):
            dopesheet.show_only_slot_of_active_object = (
                state.show_only_slot_of_active_object
            )
        area.tag_redraw()
    except ReferenceError:
        # The user may close or replace the Area while Correction Edit is open.
        return


def _leave_current_mode(context: bpy.types.Context) -> None:
    active = context.view_layer.objects.active
    if active is not None and active.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")


def _activate_armature(
    context: bpy.types.Context,
    armature: bpy.types.Object,
) -> None:
    _leave_current_mode(context)
    for obj in context.view_layer.objects:
        if obj.select_get():
            obj.select_set(False)
    armature.select_set(True)
    context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode="POSE")


def _restore_editor_state(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    state: _EditorState,
) -> None:
    animation_data = armature.animation_data
    if animation_data is not None:
        animation_data.action = state.action
        if (
            state.action is not None
            and state.action_slot is not None
            and hasattr(animation_data, "action_slot")
        ):
            animation_data.action_slot = state.action_slot
    for nla_state in state.nla_selection:
        try:
            nla_state.animation_data.nla_tracks.active = nla_state.active_track
        except (ReferenceError, TypeError):
            continue
        for track, selected in nla_state.track_selection:
            try:
                track.select = selected
            except ReferenceError:
                continue
        for strip, selected in nla_state.strip_selection:
            try:
                strip.select = selected
            except ReferenceError:
                continue

    _leave_current_mode(context)
    for obj in context.view_layer.objects:
        if obj.select_get():
            obj.select_set(False)
    for obj in state.selected_objects:
        try:
            if obj.name in context.view_layer.objects:
                obj.select_set(True)
        except ReferenceError:
            continue
    active = state.active_object
    try:
        active_available = (
            active is not None and active.name in context.view_layer.objects
        )
    except ReferenceError:
        active_available = False
    context.view_layer.objects.active = active if active_available else None
    if active_available and state.mode != "OBJECT":
        active.select_set(True)
        bpy.ops.object.mode_set(mode=state.mode)

    for bone in armature.pose.bones:
        set_pose_bone_selected(bone, bone.name in state.selected_bones)
    armature.data.bones.active = (
        armature.data.bones.get(state.active_bone)
        if state.active_bone is not None
        else None
    )
    context.scene.frame_set(state.frame, subframe=state.subframe)
    _restore_timeline_editor(state.timeline_editor)


def _choose_operator_area(context: bpy.types.Context) -> bpy.types.Area:
    screen = context.screen
    if screen is None:
        raise RuntimeError("NLA Tweak Mode requires a Blender screen context")
    for area in screen.areas:
        if area.type == "NLA_EDITOR":
            return area
    for preferred_type in ("DOPESHEET_EDITOR", "VIEW_3D"):
        for area in screen.areas:
            if area.type == preferred_type:
                return area
    if not screen.areas:
        raise RuntimeError("NLA Tweak Mode requires an editor area")
    return screen.areas[0]


@contextmanager
def _nla_operator_context(
    context: bpy.types.Context,
    armature: bpy.types.Object,
) -> Iterator[None]:
    area = _choose_operator_area(context)
    previous_type = area.type
    previous_ui_type = getattr(area, "ui_type", None)
    changed_type = previous_type != "NLA_EDITOR"
    if changed_type:
        area.type = "NLA_EDITOR"
    try:
        region = next(
            (candidate for candidate in area.regions if candidate.type == "WINDOW"),
            None,
        )
        if region is None:
            raise RuntimeError("NLA editor has no WINDOW region")
        override = {
            "area": area,
            "region": region,
            "active_object": armature,
            "object": armature,
        }
        if context.window is not None:
            override["window"] = context.window
        if context.screen is not None:
            override["screen"] = context.screen
        with context.temp_override(**override):
            yield
    finally:
        if changed_type:
            area.type = previous_type
            if previous_ui_type is not None:
                try:
                    area.ui_type = previous_ui_type
                except TypeError:
                    pass


def _set_target_selection(
    context: bpy.types.Context,
    stack: CorrectionLayerStack,
    target: CorrectionLayer,
) -> None:
    # NLA operators can consume selected strips from unselected Objects when
    # the editor is not filtered to "Only Show Selected".  Clear the entire
    # view layer so a second AnimData never enters tweak mode accidentally.
    for candidate in context.view_layer.objects:
        candidate_animation_data = candidate.animation_data
        if candidate_animation_data is None:
            continue
        for track in candidate_animation_data.nla_tracks:
            track.select = False
            for strip in track.strips:
                strip.select = False
    animation_data = target.strip.id_data.animation_data
    animation_data.nla_tracks.active = target.track
    target_track_pointer = int(target.track.as_pointer())
    target_strip_pointer = int(target.strip.as_pointer())
    for track in (stack.base_track,) + tuple(
        layer.track for layer in stack.correction_layers
    ):
        track.select = int(track.as_pointer()) == target_track_pointer
        for strip in track.strips:
            strip.select = int(strip.as_pointer()) == target_strip_pointer


def _enter_operator() -> set[str]:
    properties = bpy.ops.nla.tweakmode_enter.get_rna_type().properties
    keywords: dict[str, bool] = {"isolate_action": False}
    if "use_upper_stack_evaluation" in properties:
        keywords["use_upper_stack_evaluation"] = False
    return bpy.ops.nla.tweakmode_enter(**keywords)


def _exit_operator() -> set[str]:
    return bpy.ops.nla.tweakmode_exit(isolate_action=False)


def _exit_owned_tweak_mode(
    context: bpy.types.Context,
    armature: bpy.types.Object,
) -> None:
    animation_data = armature.animation_data
    if animation_data is None:
        raise ValueError("修正モードの終了に必要なアニメーションデータがありません")
    _activate_armature(context, armature)
    with _nla_operator_context(context, armature):
        if "FINISHED" not in _exit_operator():
            raise RuntimeError("Blenderが修正用Tweak Modeの終了を拒否しました")
    if (
        animation_data.use_tweak_mode
        or animation_data.action is not None
        or bool(getattr(context.scene, "is_nla_tweakmode", False))
    ):
        raise RuntimeError("NLA Tweak Modeを完全に終了できませんでした")


def is_correction_editing(armature: bpy.types.Object | None) -> bool:
    if armature is None or armature.type != "ARMATURE":
        return False
    animation_data = armature.animation_data
    if animation_data is None or not animation_data.use_tweak_mode:
        return False
    session = _SESSIONS.get(_session_key(armature))
    if session is None:
        return False
    try:
        layer = _resolve_session_layer(armature, session)
        _require_current_session_tweak(bpy.context, armature, session, layer)
        if not _session_references_match_layer(session, layer):
            _SESSIONS[_session_key(armature)] = replace(
                session,
                action=layer.action,
                strip=layer.strip,
                _rna_remapped=True,
            )
        return True
    except (ReferenceError, RuntimeError, TypeError, ValueError):
        return False


def active_correction_edit_layer(
    armature: bpy.types.Object | None,
) -> CorrectionLayer | None:
    if not is_correction_editing(armature):
        return None
    assert armature is not None
    stack = get_correction_layer_stack(armature)
    animation_data = armature.animation_data
    if stack is None or animation_data is None:
        return None
    return next(
        (
            layer
            for layer in stack.correction_layers
            if layer.action is animation_data.action
        ),
        None,
    )


def enter_correction_edit_mode(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    layer_id: str | None = None,
) -> CorrectionEditSession:
    """Enter NLA tweak mode on one stable Correction layer.

    Base and Archive Actions are never rebound.  Editor selection, mode, NLA
    selection, and timeline position are restored by
    :func:`exit_correction_edit_mode`.
    """

    stack = _require_stack(armature)
    require_unshared_correction_actions(armature, stack)
    target = _layer_for_id(stack, layer_id or stack.active_layer_id)
    animation_data = armature.animation_data
    if animation_data is None:
        raise ValueError("Correction Edit requires armature animation data")
    stale_session = _SESSIONS.get(_session_key(armature))
    if stale_session is not None:
        if is_correction_editing(armature):
            raise ValueError("This Correction layer is already in Tweak Mode")
        cancel_correction_edit_mode(context, armature)
        stack = _require_stack(armature)
        target = _layer_for_id(stack, layer_id or stack.active_layer_id)
    if animation_data.use_tweak_mode:
        raise ValueError("Armature is already in NLA Tweak Mode")
    if bool(getattr(context.scene, "is_nla_tweakmode", False)):
        raise ValueError(
            "Exit the Scene's existing NLA Tweak Mode before Correction Edit"
        )
    if animation_data.action is not None:
        raise ValueError("Correction Edit requires no active Action binding")
    if not corrections_preview_enabled(armature):
        raise ValueError("Switch Correction preview to After before editing")
    if target.track.mute or target.strip.mute:
        raise ValueError("Unmute the active Correction layer before editing")
    if target.track.lock:
        raise ValueError("The active Correction layer is locked")
    influence = float(target.strip.influence)
    if not math.isfinite(influence) or abs(influence - 1.0) > _FRAME_EPSILON:
        raise ValueError(
            "Set the active Correction layer influence to 1 before editing"
        )

    state = _capture_editor_state(context, armature)
    session = CorrectionEditSession(
        target.layer_id,
        target.action,
        target.strip,
        armature,
        state,
        getattr(getattr(target.strip, "action_slot", None), "handle", None),
    )
    entered = False
    try:
        _activate_armature(context, armature)
        with _nla_operator_context(context, armature):
            # Switching an Area to NLA_EDITOR can clear strip selection, so
            # select only after the operator context exists.
            _set_target_selection(context, stack, target)
            if "FINISHED" not in _enter_operator():
                raise RuntimeError("Blender refused to enter NLA Tweak Mode")
        entered = bool(animation_data.use_tweak_mode)
        if (
            not entered
            or animation_data.action is not target.action
            or not bool(getattr(context.scene, "is_nla_tweakmode", False))
        ):
            raise RuntimeError(
                "NLA Tweak Mode did not bind the active Correction Action"
            )
        active_slot = getattr(animation_data, "action_slot", None)
        target_slot = getattr(target.strip, "action_slot", None)
        if target_slot is not None and (
            active_slot is None
            or getattr(active_slot, "handle", None)
            != getattr(target_slot, "handle", None)
        ):
            raise RuntimeError(
                "NLA Tweak Mode selected the wrong Correction Action Slot"
            )
        _show_correction_keys_in_timeline(state.timeline_editor)
        _SESSIONS[_session_key(armature)] = session
        return session
    except Exception:
        if entered or animation_data.use_tweak_mode:
            try:
                with _nla_operator_context(context, armature):
                    _exit_operator()
            except Exception:
                pass
        _restore_editor_state(context, armature, state)
        raise


def scene_to_correction_frame(
    armature: bpy.types.Object,
    scene_frame: float,
) -> float:
    """Convert scene time into the active NLA Correction Action's time."""

    value = float(scene_frame)
    if not math.isfinite(value):
        raise ValueError("Correction key frame must be finite")
    animation_data = armature.animation_data
    if animation_data is None or not is_correction_editing(armature):
        raise ValueError("Enter Correction Edit before converting key time")
    converted = float(animation_data.nla_tweak_strip_time_to_scene(value, invert=True))
    if not math.isfinite(converted):
        raise ValueError("NLA produced a non-finite Correction Action frame")
    return converted


def _selected_pose_bones(
    armature: bpy.types.Object,
) -> tuple[bpy.types.PoseBone, ...]:
    return tuple(bone for bone in armature.pose.bones if pose_bone_selected(bone))


def _validate_action_frame(action: bpy.types.Action, frame: float, label: str) -> float:
    value = float(frame)
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    frame_start, frame_end = (float(item) for item in action.frame_range)
    if value < frame_start - _FRAME_EPSILON or value > frame_end + _FRAME_EPSILON:
        raise ValueError(
            f"{label} {value:g} is outside Action range {frame_start:g}-{frame_end:g}"
        )
    return value


def _require_pose_key_context(
    context: bpy.types.Context,
    armature: bpy.types.Object,
) -> tuple[
    CorrectionEditSession,
    tuple[bpy.types.PoseBone, ...],
    ActionChannelAccess,
]:
    session = _SESSIONS.get(_session_key(armature))
    animation_data = armature.animation_data
    if session is None or animation_data is None:
        raise ValueError("Enter Correction Edit before changing pose keys")
    require_unshared_correction_actions(armature)
    try:
        layer = _resolve_session_layer(armature, session)
        _require_current_session_tweak(context, armature, session, layer)
    except (ReferenceError, RuntimeError, TypeError, ValueError) as error:
        raise ValueError(
            "修正キーを変更する前に修正モードへ入り直してください"
        ) from error
    if not _session_references_match_layer(session, layer):
        session = replace(
            session,
            action=layer.action,
            strip=layer.strip,
            _rna_remapped=True,
        )
        _SESSIONS[_session_key(armature)] = session
    if context.view_layer.objects.active is not armature or armature.mode != "POSE":
        raise ValueError("The edited armature must be active in Pose Mode")
    active_slot_handle = getattr(
        getattr(animation_data, "action_slot", None),
        "handle",
        None,
    )
    if active_slot_handle != session._slot_handle:
        raise ValueError("The active Correction Action Slot changed during editing")
    bones = _selected_pose_bones(armature)
    if not bones:
        raise ValueError("Select at least one pose bone")
    return session, bones, layer.access


def _edit_action_frame(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    session: CorrectionEditSession,
    frame: float | None,
    *,
    label: str,
) -> float:
    value = (
        scene_to_correction_frame(
            armature,
            float(context.scene.frame_current) + float(context.scene.frame_subframe),
        )
        if frame is None
        else float(frame)
    )
    return _validate_action_frame(session.action, value, label)


def _unique_frames(*frames: float) -> tuple[float, ...]:
    result: list[float] = []
    for frame in sorted(frames):
        if not result or abs(frame - result[-1]) > _FRAME_EPSILON:
            result.append(frame)
    return tuple(result)


def _capture_pose_values(
    bones: tuple[bpy.types.PoseBone, ...],
    *,
    include_location: bool,
) -> tuple[tuple[str, tuple[float, ...], tuple[float, ...]], ...]:
    values = tuple(
        (
            bone.name,
            tuple(float(value) for value in bone.rotation_quaternion),
            tuple(float(value) for value in bone.location),
        )
        for bone in bones
    )
    for bone, (_bone_name, quaternion, location) in zip(
        bones,
        values,
        strict=True,
    ):
        if bone.rotation_mode != "QUATERNION":
            raise ValueError(
                f'Set pose bone "{bone.name}" Rotation Mode to Quaternion before keying'
            )
        quaternion_norm = math.hypot(*quaternion)
        if (
            not all(math.isfinite(value) for value in quaternion)
            or not math.isfinite(quaternion_norm)
            or quaternion_norm == 0.0
        ):
            raise ValueError(f'Pose bone "{bone.name}" has an invalid quaternion')
        if include_location and not all(math.isfinite(value) for value in location):
            raise ValueError(f'Pose bone "{bone.name}" has a non-finite location')
    return values


def _require_supported_location_target(
    bones: tuple[bpy.types.PoseBone, ...],
    *,
    include_location: bool,
) -> None:
    """Keep public Correction location edits inside the Hips-only contract."""

    if not include_location:
        return
    if tuple(bone.name for bone in bones) != (_LOCATION_CORRECTION_BONE,):
        raise ValueError("位置補正はHipsを1本だけ選択したときに使用できます")


def _keyframe_point_state(point: bpy.types.Keyframe) -> _KeyframePointState:
    return _KeyframePointState(
        tuple(float(value) for value in point.co),
        tuple(float(value) for value in point.handle_left),
        tuple(float(value) for value in point.handle_right),
        str(point.handle_left_type),
        str(point.handle_right_type),
        str(point.interpolation),
        str(point.easing),
        float(point.amplitude),
        float(point.back),
        float(point.period),
        str(point.type),
        bool(point.select_control_point),
        bool(point.select_left_handle),
        bool(point.select_right_handle),
    )


def _restore_curve_key_state(state: _CurveKeyState) -> None:
    curve = state.curve
    points = curve.keyframe_points
    points.clear()
    if state.points:
        points.add(len(state.points))
    for point, saved in zip(points, state.points, strict=True):
        point.co = saved.co
        point.interpolation = saved.interpolation
        point.easing = saved.easing
        point.amplitude = saved.amplitude
        point.back = saved.back
        point.period = saved.period
        point.type = saved.keyframe_type
        point.handle_left_type = saved.handle_left_type
        point.handle_right_type = saved.handle_right_type
        point.handle_left = saved.handle_left
        point.handle_right = saved.handle_right
        point.select_control_point = saved.select_control_point
        point.select_left_handle = saved.select_left_handle
        point.select_right_handle = saved.select_right_handle


def _curve_pointer(curve: bpy.types.FCurve) -> int:
    return int(curve.as_pointer())


@contextmanager
def _correction_key_transaction(
    context: bpy.types.Context,
    access: ActionChannelAccess,
    bone_names: tuple[str, ...],
    *,
    include_location: bool,
    extra_paths: tuple[str, ...] = (),
) -> Iterator[Callable[[], None]]:
    """Rollback selected-property keys and newly-created curves on failure."""

    paths = {
        _pose_bone_property_path(bone_name, "rotation_quaternion")
        for bone_name in bone_names
    }
    if include_location:
        paths.update(
            _pose_bone_property_path(bone_name, "location") for bone_name in bone_names
        )
    paths.update(str(path) for path in extra_paths)
    existing = tuple(
        _CurveKeyState(
            curve,
            tuple(_keyframe_point_state(point) for point in curve.keyframe_points),
        )
        for curve in iter_action_fcurves(access.action, access.slot)
        if curve.data_path in paths
    )
    existing_pointers = {_curve_pointer(state.curve) for state in existing}
    group_collection = getattr(access.channelbag, "groups", None)
    if group_collection is None:
        group_collection = getattr(access.action, "groups", None)
    existing_group_pointers = (
        {int(group.as_pointer()) for group in group_collection}
        if group_collection is not None
        else set()
    )

    def restore() -> None:
        current_curves = tuple(
            curve
            for curve in iter_action_fcurves(access.action, access.slot)
            if curve.data_path in paths
        )
        for curve in current_curves:
            if _curve_pointer(curve) not in existing_pointers:
                remove_fcurve(access, curve)
        for state in existing:
            _restore_curve_key_state(state)
        if group_collection is not None:
            for group in tuple(group_collection):
                if int(group.as_pointer()) not in existing_group_pointers:
                    group_collection.remove(group)
        context.view_layer.update()

    try:
        yield restore
    except Exception as edit_error:
        try:
            restore()
        except Exception as rollback_error:
            if hasattr(edit_error, "add_note"):
                edit_error.add_note(
                    "Correction rollback also failed: " + str(rollback_error)
                )
            raise edit_error from rollback_error
        raise


def _complete_property_key_values(
    access: ActionChannelAccess,
    bone_name: str,
    property_name: str,
    size: int,
    frame: float,
) -> tuple[float, ...]:
    data_path = _pose_bone_property_path(bone_name, property_name)
    curves: dict[int, bpy.types.FCurve] = {}
    for curve in iter_action_fcurves(access.action, access.slot):
        index = int(curve.array_index)
        if curve.data_path == data_path and 0 <= index < size:
            if index in curves:
                raise RuntimeError(
                    f"Correction has duplicate {property_name} channel {index}: "
                    f"{data_path}"
                )
            curves[index] = curve
    if set(curves) != set(range(size)):
        raise RuntimeError(
            f"Blender did not create complete {property_name} channels: {data_path}"
        )
    values: list[float] = []
    for index in range(size):
        matches = tuple(
            point
            for point in curves[index].keyframe_points
            if abs(float(point.co[0]) - frame) <= _FRAME_EPSILON
        )
        if len(matches) != 1:
            raise RuntimeError(
                f"Blender did not create exactly one {property_name}[{index}] "
                f"key at Action frame {frame:g}"
            )
        value = float(matches[0].co[1])
        if not math.isfinite(value):
            raise RuntimeError(
                f"Blender created a non-finite {property_name}[{index}] key"
            )
        values.append(value)
    return tuple(values)


def _capture_nla_remapped_pose_values(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    bones: tuple[bpy.types.PoseBone, ...],
    access: ActionChannelAccess,
    *,
    action_frame: float,
    include_location: bool,
    interpolation: str,
) -> tuple[tuple[str, tuple[float, ...], tuple[float, ...]], ...]:
    """Key through Blender's NLA context and return active-strip values.

    PoseBone properties in tweak mode are the evaluated result of Base and the
    lower NLA stack.  ``keyframe_insert`` performs Blender's inverse NLA value
    remap; direct FCurve writes would incorrectly store that evaluated result
    as a COMBINE delta and apply the motion twice.
    """

    visible_values = _capture_pose_values(
        bones,
        include_location=include_location,
    )
    scene_frame = float(context.scene.frame_current) + float(
        context.scene.frame_subframe
    )
    for bone, (_bone_name, quaternion, location) in zip(
        bones,
        visible_values,
        strict=True,
    ):
        bone.rotation_quaternion = quaternion
        if not bone.keyframe_insert(
            data_path="rotation_quaternion",
            frame=scene_frame,
            group=bone.name,
        ):
            raise RuntimeError(
                f'Blender could not key Correction rotation for "{bone.name}"'
            )
        if include_location:
            bone.location = location
            if not bone.keyframe_insert(
                data_path="location",
                frame=scene_frame,
                group=bone.name,
            ):
                raise RuntimeError(
                    f'Blender could not key Correction location for "{bone.name}"'
                )

    remapped: list[tuple[str, tuple[float, ...], tuple[float, ...]]] = []
    for bone in bones:
        quaternion = _complete_property_key_values(
            access,
            bone.name,
            "rotation_quaternion",
            4,
            action_frame,
        )
        location = (
            _complete_property_key_values(
                access,
                bone.name,
                "location",
                3,
                action_frame,
            )
            if include_location
            else (0.0, 0.0, 0.0)
        )
        # Re-upsert the built-in keys to enforce complete normalized channels,
        # hemisphere continuity, and the user-selected interpolation.
        insert_quaternion_key(
            access,
            bone.name,
            action_frame,
            quaternion,
            interpolation=interpolation,
        )
        if include_location:
            insert_location_key(
                access,
                bone.name,
                action_frame,
                location,
                interpolation=interpolation,
            )
        remapped.append((bone.name, quaternion, location))
    return tuple(remapped)


def insert_correction_pose_keys(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    *,
    include_location: bool = False,
    neutral_frame_before: float | None = None,
    neutral_frame_after: float | None = None,
    pose_frame_start: float | None = None,
    pose_frame_end: float | None = None,
    interpolation: str = "LINEAR",
) -> tuple[str, ...]:
    """Key selected pose bones into only the active Correction Action.

    Rotation is always inserted as a normalized, complete WXYZ quaternion.
    Optional neutral frames use identity rotation and zero location, producing
    a bounded sparse correction without touching Base or Archive data.
    """

    session, bones, access = _require_pose_key_context(
        context,
        armature,
    )
    _require_supported_location_target(
        bones,
        include_location=include_location,
    )
    key_interpolation = str(interpolation).upper()
    if key_interpolation not in _KEY_INTERPOLATIONS:
        supported = ", ".join(sorted(_KEY_INTERPOLATIONS))
        raise ValueError(f"Correction key interpolation must be one of: {supported}")
    action_frame = _edit_action_frame(
        context,
        armature,
        session,
        None,
        label="Correction key frame",
    )
    if (pose_frame_start is None) != (pose_frame_end is None):
        raise ValueError("Correction pose range requires both start and end frames")
    pose_start = (
        _validate_action_frame(
            session.action,
            pose_frame_start,
            "Correction pose range start",
        )
        if pose_frame_start is not None
        else action_frame
    )
    pose_end = (
        _validate_action_frame(
            session.action,
            pose_frame_end,
            "Correction pose range end",
        )
        if pose_frame_end is not None
        else action_frame
    )
    if pose_end < pose_start - _FRAME_EPSILON:
        raise ValueError("Correction pose range is reversed")
    if (
        action_frame < pose_start - _FRAME_EPSILON
        or action_frame > pose_end + _FRAME_EPSILON
    ):
        raise ValueError("Current frame must be inside the Correction pose range")
    before = (
        _validate_action_frame(
            session.action, neutral_frame_before, "Neutral before frame"
        )
        if neutral_frame_before is not None
        else None
    )
    after = (
        _validate_action_frame(
            session.action, neutral_frame_after, "Neutral after frame"
        )
        if neutral_frame_after is not None
        else None
    )
    if before is not None and before >= pose_start - _FRAME_EPSILON:
        raise ValueError("Neutral before frame must be earlier than the pose range")
    if after is not None and after <= pose_end + _FRAME_EPSILON:
        raise ValueError("Neutral after frame must be later than the pose range")

    bone_names = tuple(bone.name for bone in bones)
    with _correction_key_transaction(
        context,
        access,
        bone_names,
        include_location=include_location,
    ):
        values = _capture_nla_remapped_pose_values(
            context,
            armature,
            bones,
            access,
            action_frame=action_frame,
            include_location=include_location,
            interpolation=key_interpolation,
        )
        pose_frames = _unique_frames(pose_start, action_frame, pose_end)
        for bone_name, quaternion, location in values:
            if before is not None:
                insert_quaternion_key(
                    access,
                    bone_name,
                    before,
                    (1.0, 0.0, 0.0, 0.0),
                    interpolation=key_interpolation,
                )
                if include_location:
                    insert_location_key(
                        access,
                        bone_name,
                        before,
                        (0.0, 0.0, 0.0),
                        interpolation=key_interpolation,
                    )
            for pose_frame in pose_frames:
                insert_quaternion_key(
                    access,
                    bone_name,
                    pose_frame,
                    quaternion,
                    interpolation=key_interpolation,
                )
                if include_location:
                    insert_location_key(
                        access,
                        bone_name,
                        pose_frame,
                        location,
                        interpolation=key_interpolation,
                    )
            if after is not None:
                insert_quaternion_key(
                    access,
                    bone_name,
                    after,
                    (1.0, 0.0, 0.0, 0.0),
                    interpolation=key_interpolation,
                )
                if include_location:
                    insert_location_key(
                        access,
                        bone_name,
                        after,
                        (0.0, 0.0, 0.0),
                        interpolation=key_interpolation,
                    )
        context.view_layer.update()
    return tuple(bone.name for bone in bones)


def reset_correction_pose_keys(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    *,
    include_location: bool = False,
    frame: float | None = None,
    interpolation: str = "LINEAR",
) -> tuple[str, ...]:
    """Upsert an identity correction for selected bones at one Action frame."""

    session, bones, access = _require_pose_key_context(context, armature)
    _require_supported_location_target(
        bones,
        include_location=include_location,
    )
    key_interpolation = str(interpolation).upper()
    if key_interpolation not in _KEY_INTERPOLATIONS:
        supported = ", ".join(sorted(_KEY_INTERPOLATIONS))
        raise ValueError(f"Correction key interpolation must be one of: {supported}")
    action_frame = _edit_action_frame(
        context,
        armature,
        session,
        frame,
        label="Correction reset frame",
    )
    bone_names = tuple(bone.name for bone in bones)
    with _correction_key_transaction(
        context,
        access,
        bone_names,
        include_location=include_location,
    ):
        for bone in bones:
            insert_quaternion_key(
                access,
                bone.name,
                action_frame,
                (1.0, 0.0, 0.0, 0.0),
                interpolation=key_interpolation,
            )
            if include_location:
                insert_location_key(
                    access,
                    bone.name,
                    action_frame,
                    (0.0, 0.0, 0.0),
                    interpolation=key_interpolation,
                )
        context.view_layer.update()
    return bone_names


def remove_correction_pose_keys(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    *,
    include_location: bool = False,
    frame: float | None = None,
) -> tuple[str, ...]:
    """Remove selected bones' complete correction keys at one Action frame."""

    session, bones, access = _require_pose_key_context(context, armature)
    _require_supported_location_target(
        bones,
        include_location=include_location,
    )
    action_frame = _edit_action_frame(
        context,
        armature,
        session,
        frame,
        label="Correction remove frame",
    )
    changed: list[str] = []
    bone_names = tuple(bone.name for bone in bones)
    with _correction_key_transaction(
        context,
        access,
        bone_names,
        include_location=include_location,
    ):
        for bone in bones:
            removed = remove_quaternion_key(access, bone.name, action_frame)
            if include_location:
                removed = (
                    remove_location_key(access, bone.name, action_frame) or removed
                )
            if removed:
                changed.append(bone.name)
        context.view_layer.update()
    return tuple(changed)


def _pose_bone_property_path(bone_name: str, property_name: str) -> str:
    escaped_name = bpy.utils.escape_identifier(str(bone_name))
    return f'pose.bones["{escaped_name}"].{property_name}'


def _require_range_editable_curves(
    access: ActionChannelAccess,
    bone_names: tuple[str, ...],
    *,
    include_location: bool,
) -> None:
    paths = {
        _pose_bone_property_path(bone_name, "rotation_quaternion")
        for bone_name in bone_names
    }
    if include_location:
        paths.update(
            _pose_bone_property_path(bone_name, "location") for bone_name in bone_names
        )
    for curve in iter_action_fcurves(access.action, access.slot):
        if curve.data_path not in paths:
            continue
        if curve.sampled_points:
            raise ValueError(
                f"Correction range cannot replace sampled data: {curve.data_path}"
            )
        if curve.modifiers:
            raise ValueError(
                f"Correction range cannot replace a modified curve: {curve.data_path}"
            )


def apply_correction_pose_range(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    *,
    scene_frame_start: float,
    scene_frame_end: float,
    ease_in_frames: float = 2.0,
    ease_out_frames: float = 2.0,
    include_location: bool = False,
    interpolation: str = "BEZIER",
    replace_inner_keys: bool = True,
) -> tuple[str, ...]:
    """Apply the current selected-bone correction across one Scene range.

    Existing selected-property keys inside the neutral-to-neutral envelope are
    removed by default, then four complete key sets define a deterministic
    neutral -> offset hold -> neutral correction.  All times are independently
    converted through Blender's NLA scene/action mapping.
    """

    session, bones, access = _require_pose_key_context(context, armature)
    _require_supported_location_target(
        bones,
        include_location=include_location,
    )
    try:
        scene_start = float(scene_frame_start)
        scene_end = float(scene_frame_end)
        ease_in = float(ease_in_frames)
        ease_out = float(ease_out_frames)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("Correction range and ease values must be finite") from error
    if not all(
        math.isfinite(value) for value in (scene_start, scene_end, ease_in, ease_out)
    ):
        raise ValueError("Correction range and ease values must be finite")
    if scene_end < scene_start:
        raise ValueError("Correction Scene range is reversed")
    if ease_in < 0.0 or ease_out < 0.0:
        raise ValueError("Correction ease frames cannot be negative")
    current_scene_frame = float(context.scene.frame_current) + float(
        context.scene.frame_subframe
    )
    if (
        current_scene_frame < scene_start - _FRAME_EPSILON
        or current_scene_frame > scene_end + _FRAME_EPSILON
    ):
        raise ValueError("Current frame must be inside the Correction Scene range")

    key_interpolation = str(interpolation).upper()
    if key_interpolation not in _KEY_INTERPOLATIONS:
        supported = ", ".join(sorted(_KEY_INTERPOLATIONS))
        raise ValueError(f"Correction key interpolation must be one of: {supported}")
    pose_start = _validate_action_frame(
        session.action,
        scene_to_correction_frame(armature, scene_start),
        "Correction range start",
    )
    pose_end = _validate_action_frame(
        session.action,
        scene_to_correction_frame(armature, scene_end),
        "Correction range end",
    )
    if pose_end < pose_start - _FRAME_EPSILON:
        raise ValueError("NLA maps the Correction range in reverse")

    action_start, action_end = (float(value) for value in session.action.frame_range)
    neutral_before: float | None = None
    neutral_after: float | None = None
    if ease_in > 0.0 and pose_start > action_start + _FRAME_EPSILON:
        neutral_before = max(
            action_start,
            scene_to_correction_frame(armature, scene_start - ease_in),
        )
        if neutral_before >= pose_start - _FRAME_EPSILON:
            raise ValueError("NLA mapping leaves no time for Correction ease-in")
    if ease_out > 0.0 and pose_end < action_end - _FRAME_EPSILON:
        neutral_after = min(
            action_end,
            scene_to_correction_frame(armature, scene_end + ease_out),
        )
        if neutral_after <= pose_end + _FRAME_EPSILON:
            raise ValueError("NLA mapping leaves no time for Correction ease-out")

    bone_names = tuple(bone.name for bone in bones)
    _require_range_editable_curves(
        access,
        bone_names,
        include_location=include_location,
    )
    envelope_start = neutral_before if neutral_before is not None else pose_start
    envelope_end = neutral_after if neutral_after is not None else pose_end
    current_action_frame = _edit_action_frame(
        context,
        armature,
        session,
        None,
        label="Current Correction frame",
    )
    with _correction_key_transaction(
        context,
        access,
        bone_names,
        include_location=include_location,
    ) as restore_keys:
        values = _capture_nla_remapped_pose_values(
            context,
            armature,
            bones,
            access,
            action_frame=current_action_frame,
            include_location=include_location,
            interpolation=key_interpolation,
        )
        # keyframe_insert is needed only for Blender's inverse NLA value remap.
        # Remove that temporary current-frame key before writing the requested
        # range, including when the caller keeps existing inner keys.
        restore_keys()
        if replace_inner_keys:
            for bone_name in bone_names:
                remove_quaternion_keys_in_range(
                    access,
                    bone_name,
                    envelope_start,
                    envelope_end,
                )
                if include_location:
                    remove_location_keys_in_range(
                        access,
                        bone_name,
                        envelope_start,
                        envelope_end,
                    )

        for bone_name, quaternion, location in values:
            if neutral_before is not None:
                insert_quaternion_key(
                    access,
                    bone_name,
                    neutral_before,
                    (1.0, 0.0, 0.0, 0.0),
                    interpolation=key_interpolation,
                )
                if include_location:
                    insert_location_key(
                        access,
                        bone_name,
                        neutral_before,
                        (0.0, 0.0, 0.0),
                        interpolation=key_interpolation,
                    )
            for pose_frame in _unique_frames(pose_start, pose_end):
                insert_quaternion_key(
                    access,
                    bone_name,
                    pose_frame,
                    quaternion,
                    interpolation=key_interpolation,
                )
                if include_location:
                    insert_location_key(
                        access,
                        bone_name,
                        pose_frame,
                        location,
                        interpolation=key_interpolation,
                    )
            if neutral_after is not None:
                insert_quaternion_key(
                    access,
                    bone_name,
                    neutral_after,
                    (1.0, 0.0, 0.0, 0.0),
                    interpolation=key_interpolation,
                )
                if include_location:
                    insert_location_key(
                        access,
                        bone_name,
                        neutral_after,
                        (0.0, 0.0, 0.0),
                        interpolation=key_interpolation,
                    )
        context.view_layer.update()
    return bone_names


def _active_action_content_errors(
    action: bpy.types.Action,
    armature: bpy.types.Object,
) -> tuple[str, ...]:
    # Reuse the stack's sparse Action validator without scanning dense Base or
    # Archive data on the edit-mode hot path.
    from .correction_layers import _correction_curve_errors

    return tuple(_correction_curve_errors(action, armature))


def exit_correction_edit_mode(
    context: bpy.types.Context,
    armature: bpy.types.Object,
) -> CorrectionEditSession:
    """Validate the edited layer, leave NLA tweak mode, and restore the UI."""

    key = _session_key(armature)
    session = _SESSIONS.get(key)
    animation_data = armature.animation_data
    if session is None:
        raise ValueError("No restorable Motion Bridge Correction Edit is active")
    if animation_data is None:
        raise ValueError("修正作業の終了に必要なアニメーションデータがありません")
    layer = _resolve_session_layer(armature, session)
    _require_current_session_tweak(context, armature, session, layer)
    stale_references = session._rna_remapped or not _session_references_match_layer(
        session, layer
    )
    restore_state = _recovery_editor_state(
        context,
        armature,
        session,
        stale_references=stale_references,
    )
    recovered_session = replace(
        session,
        action=layer.action,
        strip=layer.strip,
        _rna_remapped=stale_references,
    )
    active_slot_handle = getattr(
        getattr(animation_data, "action_slot", None),
        "handle",
        None,
    )
    strip_slot_handle = getattr(
        getattr(layer.strip, "action_slot", None),
        "handle",
        None,
    )
    if (
        active_slot_handle != session._slot_handle
        or strip_slot_handle != session._slot_handle
    ):
        raise ValueError("The active Correction Action Slot changed during editing")
    errors = _active_action_content_errors(layer.action, armature)
    if errors:
        raise ValueError("Invalid Correction keys: " + "; ".join(errors))

    try:
        _exit_owned_tweak_mode(context, armature)
    except Exception:
        # Keep the session snapshot so Finish Editing can be retried.
        raise

    _restore_editor_state(context, armature, restore_state)
    _SESSIONS.pop(key, None)
    return recovered_session
