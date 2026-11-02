"""Conservative, lossless flattening for adjacent Correction layers.

The merge deliberately accepts only a small, provably safe subset: two
adjacent unit-influence COMBINE strips with identical time mapping and
disjoint, fully keyed Action properties.  Unsupported Action topology,
samples, modifiers, mute/solo state, active Action bindings, or overlapping
properties are hard errors.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import bpy

from .action_api import (
    ActionChannelAccess,
    access_existing_action,
    action_slot_for_id,
    assign_nla_strip_action,
    ensure_fcurve,
    iter_action_fcurves,
)
from .correction_layers import (
    LAYER_SCHEMA_VERSION,
    PROP_ACTIVE_LAYER_ID,
    PROP_BASE_ACTION,
    PROP_BASE_CHECKSUM,
    PROP_CORRECTION_ACTION,
    PROP_LAYER_ID,
    PROP_LAYER_MUTED,
    PROP_LAYER_NAME,
    PROP_LAYER_SOLO,
    PROP_ROLE,
    PROP_SCHEMA,
    PROP_STACK_ID,
    ROLE_CORRECTION,
    CorrectionLayer,
    CorrectionLayerStack,
    _apply_binding_state,
    _binding_state,
    _configure_strip,
    _correction_track_name,
    _correction_tracks,
    _layer_for_id,
    _normalized_layer_name,
    _quaternion_curve_errors,
    _resolve_stack,
    _restore_binding,
    _set_active_values,
    _strip_for_action,
    _validated_stack,
    validate_layer_stack,
)


_EPSILON = 1e-6
@dataclass(frozen=True, slots=True)
class _ActionInspection:
    access: ActionChannelAccess
    curves: tuple[bpy.types.FCurve, ...]
    channels: frozenset[tuple[str, int]]
    groups: frozenset[str]


@dataclass(frozen=True, slots=True)
class _MergeBindingState:
    core: Any
    track_name: str
    strip_name: str
    strip_select: bool
    strip_use_sync_length: bool
    strip_use_animated_time_cyclic: bool
    strip_time: float
    optional_track_attributes: tuple[tuple[str, Any], ...]
    optional_strip_attributes: tuple[tuple[str, Any], ...]


def _optional_attribute_state(
    owner: Any,
    names: tuple[str, ...],
) -> tuple[tuple[str, Any], ...]:
    state: list[tuple[str, Any]] = []
    for name in names:
        if not hasattr(owner, name):
            continue
        value = getattr(owner, name)
        if name == "color":
            value = tuple(value)
        state.append((name, value))
    return tuple(state)


def _merge_binding_state(layer: CorrectionLayer) -> _MergeBindingState:
    return _MergeBindingState(
        _binding_state(layer),
        str(layer.track.name),
        str(layer.strip.name),
        bool(layer.strip.select),
        bool(getattr(layer.strip, "use_sync_length", False)),
        bool(getattr(layer.strip, "use_animated_time_cyclic", False)),
        float(getattr(layer.strip, "strip_time", 0.0)),
        _optional_attribute_state(layer.track, ("use_custom_color", "color")),
        _optional_attribute_state(layer.strip, ("use_custom_color", "color")),
    )


def _apply_merge_binding_state(
    armature: bpy.types.Object,
    track: bpy.types.NlaTrack,
    state: _MergeBindingState,
    *,
    first: bool,
) -> None:
    _apply_binding_state(armature, track, state.core, first=first)
    strip = track.strips[0]
    track.name = state.track_name
    strip.name = state.strip_name
    strip.select = state.strip_select
    if hasattr(strip, "use_sync_length"):
        strip.use_sync_length = state.strip_use_sync_length
    if hasattr(strip, "use_animated_time_cyclic"):
        strip.use_animated_time_cyclic = state.strip_use_animated_time_cyclic
    if hasattr(strip, "strip_time"):
        strip.strip_time = state.strip_time
    for name, value in state.optional_track_attributes:
        setattr(track, name, value)
    for name, value in state.optional_strip_attributes:
        setattr(strip, name, value)


def _simple_action_access(
    action: bpy.types.Action,
    armature: bpy.types.Object,
    label: str,
) -> ActionChannelAccess:
    """Require the one-slot/one-keyframe-strip topology created by the addon."""

    access = access_existing_action(action, armature)
    slots = getattr(action, "slots", None)
    layers = getattr(action, "layers", None)
    if slots is None and layers is None:
        return access
    if slots is None or layers is None or len(slots) != 1 or access.slot is None:
        raise ValueError(f"{label} must contain exactly one armature Action Slot")
    if len(layers) != 1:
        raise ValueError(f"{label} must contain exactly one Action layer")
    strips = layers[0].strips
    if len(strips) != 1 or getattr(strips[0], "type", "KEYFRAME") != "KEYFRAME":
        raise ValueError(f"{label} must contain exactly one keyed Action strip")
    channelbags = getattr(strips[0], "channelbags", ())
    if len(channelbags) != 1 or getattr(
        channelbags[0],
        "slot_handle",
        None,
    ) != getattr(access.slot, "handle", None):
        raise ValueError(f"{label} has unsupported Action Slot channel topology")
    return access


def _inspect_action(
    action: bpy.types.Action,
    armature: bpy.types.Object,
    label: str,
) -> _ActionInspection:
    access = _simple_action_access(action, armature, label)
    all_curves = tuple(iter_action_fcurves(action))
    curves = tuple(iter_action_fcurves(action, access.slot))
    if {curve.as_pointer() for curve in all_curves} != {
        curve.as_pointer() for curve in curves
    }:
        raise ValueError(f"{label} contains curves outside its armature Action Slot")

    channels: set[tuple[str, int]] = set()
    groups: set[str] = set()
    for curve in curves:
        channel = (str(curve.data_path), int(curve.array_index))
        if channel in channels:
            raise ValueError(
                f"{label} contains duplicate channel {channel[0]}[{channel[1]}]"
            )
        channels.add(channel)
        if not curve.data_path:
            raise ValueError(f"{label} contains an FCurve without a data path")
        if not curve.is_valid:
            raise ValueError(
                f"{label} contains an invalid FCurve: {channel[0]}[{channel[1]}]"
            )
        if len(curve.sampled_points) != 0:
            raise ValueError(
                f"{label} contains sampled points: {channel[0]}[{channel[1]}]"
            )
        if len(curve.keyframe_points) == 0:
            raise ValueError(
                f"{label} contains a non-keyed FCurve: {channel[0]}[{channel[1]}]"
            )
        if len(curve.modifiers) != 0:
            raise ValueError(
                f"{label} contains FCurve modifiers: {channel[0]}[{channel[1]}]"
            )
        if curve.mute or (curve.group is not None and curve.group.mute):
            raise ValueError(
                f"{label} contains muted animation: {channel[0]}[{channel[1]}]"
            )
        if curve.group is not None:
            groups.add(str(curve.group.name))
        for point in curve.keyframe_points:
            values = (
                float(point.co[0]),
                float(point.co[1]),
                float(point.handle_left[0]),
                float(point.handle_left[1]),
                float(point.handle_right[0]),
                float(point.handle_right[1]),
            )
            if not all(math.isfinite(value) for value in values):
                raise ValueError(
                    f"{label} contains non-finite keyframe data: "
                    f"{channel[0]}[{channel[1]}]"
                )
    return _ActionInspection(access, curves, frozenset(channels), frozenset(groups))


def _require_unit_unmuted_pair(
    lower: CorrectionLayer,
    upper: CorrectionLayer,
) -> None:
    for layer in (lower, upper):
        if (
            layer.strip.mute
            or layer.solo
            or layer.track.is_solo
            or bool(layer.action.get(PROP_LAYER_MUTED, False))
        ):
            raise ValueError(
                "Merge requires both adjacent Correction layers configured unmuted"
            )
        if abs(float(layer.strip.influence) - 1.0) > _EPSILON:
            raise ValueError("Merge requires both adjacent layer influences to be 1")


def _strip_time_signature(strip: bpy.types.NlaStrip) -> tuple[float, ...]:
    return (
        float(strip.frame_start),
        float(strip.frame_end),
        float(strip.action_frame_start),
        float(strip.action_frame_end),
        float(strip.scale),
        float(strip.repeat),
    )


def _require_safe_strip(layer: CorrectionLayer) -> None:
    strip = layer.strip
    if strip.blend_type != "COMBINE" or strip.extrapolation != "NOTHING":
        raise ValueError("Merge requires quaternion-safe COMBINE Correction strips")
    if strip.use_animated_influence or strip.use_animated_time:
        raise ValueError("Merge does not support animated strip influence or time")
    if len(strip.fcurves) != 0 or len(strip.modifiers) != 0:
        raise ValueError("Merge does not support NLA strip FCurves or modifiers")
    if strip.use_auto_blend or abs(float(strip.blend_in)) > _EPSILON or abs(
        float(strip.blend_out)
    ) > _EPSILON:
        raise ValueError("Merge does not support strip blending ramps")
    if bool(getattr(strip, "use_reverse", False)):
        raise ValueError("Merge does not support reversed strips")


def _require_matching_time_mapping(
    lower: CorrectionLayer,
    upper: CorrectionLayer,
) -> None:
    _require_safe_strip(lower)
    _require_safe_strip(upper)
    lower_signature = _strip_time_signature(lower.strip)
    upper_signature = _strip_time_signature(upper.strip)
    if any(
        abs(lower_value - upper_value) > _EPSILON
        for lower_value, upper_value in zip(lower_signature, upper_signature)
    ):
        raise ValueError("Merge requires identical adjacent strip time mapping")
    if bool(lower.action.use_frame_range) != bool(upper.action.use_frame_range):
        raise ValueError("Merge requires identical Action frame-range behavior")
    lower_range = tuple(float(value) for value in lower.action.frame_range)
    upper_range = tuple(float(value) for value in upper.action.frame_range)
    if any(
        abs(lower_value - upper_value) > _EPSILON
        for lower_value, upper_value in zip(lower_range, upper_range)
    ):
        raise ValueError("Merge requires identical adjacent Action frame ranges")


def _copy_id_properties(source: Any, destination: Any) -> None:
    try:
        items = tuple(source.items())
    except TypeError:
        return
    for key, value in items:
        destination[key] = value


def _copy_group_metadata(
    source: bpy.types.ActionGroup | None,
    destination: bpy.types.ActionGroup | None,
) -> None:
    if source is None or destination is None:
        if source is not destination:
            raise RuntimeError("Cloned FCurve group binding was not preserved")
        return
    for attribute in (
        "color_set",
        "lock",
        "mute",
        "select",
        "show_expanded",
        "show_expanded_graph",
    ):
        if hasattr(source, attribute) and hasattr(destination, attribute):
            setattr(destination, attribute, getattr(source, attribute))
    if hasattr(source, "colors") and hasattr(destination, "colors"):
        for attribute in ("normal", "select", "active"):
            setattr(
                destination.colors,
                attribute,
                tuple(getattr(source.colors, attribute)),
            )
    _copy_id_properties(source, destination)


def _copy_curve_metadata(
    source: bpy.types.FCurve,
    destination: bpy.types.FCurve,
) -> None:
    for attribute in (
        "auto_smoothing",
        "color_mode",
        "extrapolation",
        "hide",
        "lock",
        "mute",
        "select",
    ):
        if hasattr(source, attribute) and hasattr(destination, attribute):
            setattr(destination, attribute, getattr(source, attribute))
    if hasattr(source, "color") and hasattr(destination, "color"):
        destination.color = tuple(source.color)
    _copy_id_properties(source, destination)


def _copy_keyframe_metadata(source: Any, destination: Any) -> None:
    destination.co = tuple(source.co)
    for attribute in (
        "amplitude",
        "back",
        "easing",
        "handle_left_type",
        "handle_right_type",
        "interpolation",
        "period",
        "select_control_point",
        "select_left_handle",
        "select_right_handle",
        "type",
    ):
        if hasattr(source, attribute) and hasattr(destination, attribute):
            setattr(destination, attribute, getattr(source, attribute))
    # Assign explicit handle positions last.  Setting the handle types can
    # otherwise recalculate these coordinates before the clone is complete.
    destination.handle_left = tuple(source.handle_left)
    destination.handle_right = tuple(source.handle_right)


def _clone_curve(
    source: bpy.types.FCurve,
    destination_access: ActionChannelAccess,
) -> bpy.types.FCurve:
    group_name = source.group.name if source.group is not None else ""
    destination = ensure_fcurve(
        destination_access,
        source.data_path,
        int(source.array_index),
        group_name=group_name,
    )
    destination.keyframe_points.add(len(source.keyframe_points))

    # Populate coordinates, update once to establish sorted/evaluable keys,
    # then restore every authoring attribute and exact handle coordinate.
    for source_point, destination_point in zip(
        source.keyframe_points,
        destination.keyframe_points,
    ):
        destination_point.co = tuple(source_point.co)
    destination.update()
    for source_point, destination_point in zip(
        source.keyframe_points,
        destination.keyframe_points,
    ):
        _copy_keyframe_metadata(source_point, destination_point)
    _copy_curve_metadata(source, destination)
    _copy_group_metadata(source.group, destination.group)
    return destination


def _result_name(
    stack: CorrectionLayerStack,
    lower: CorrectionLayer,
    upper: CorrectionLayer,
    requested: str | None,
) -> str:
    if requested is None:
        return lower.name
    value = _normalized_layer_name(requested)
    if any(
        layer not in {lower, upper} and layer.name.casefold() == value.casefold()
        for layer in stack.correction_layers
    ):
        raise ValueError(f"Correction layer name already exists: {value}")
    return value


def _remove_temporary_action(action: bpy.types.Action) -> None:
    action.use_fake_user = False
    if action.users == 0:
        bpy.data.actions.remove(action)


def _retire_action(action: bpy.types.Action) -> None:
    non_fake_users = int(action.users) - int(bool(action.use_fake_user))
    if non_fake_users > 0:
        # A duplicated armature may still reference this Action from its own
        # NLA stack.  Preserve its identity metadata and authoring state there.
        return
    action.use_fake_user = False
    if action.users == 0:
        bpy.data.actions.remove(action)


def _restore_tracks_after_failure(
    armature: bpy.types.Object,
    upper: CorrectionLayer,
    upper_frame_start: float,
    upper_strip_name: str,
    original_states: tuple[_MergeBindingState, ...],
) -> None:
    animation_data = armature.animation_data
    assert animation_data is not None
    restored_track = animation_data.nla_tracks.new()
    restored_strip = _strip_for_action(
        restored_track,
        upper_strip_name,
        upper.action,
        action_slot_for_id(upper.action, armature),
        upper_frame_start,
    )
    _configure_strip(restored_strip, blend_type="COMBINE", extrapolation="NOTHING")
    correction_tracks = _correction_tracks(
        animation_data,
        str(armature[PROP_STACK_ID]),
    )
    if len(correction_tracks) != len(original_states):
        raise RuntimeError("Could not reconstruct Correction tracks during rollback")
    for index, (track, state) in enumerate(zip(correction_tracks, original_states)):
        _apply_merge_binding_state(armature, track, state, first=index == 0)


def merge_correction_layer_down(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    layer_id: str,
    *,
    name: str | None = None,
) -> CorrectionLayer:
    """Merge one Correction into its immediate lower neighbor without resampling.

    The returned layer keeps the lower neighbor's stable ``layer_id``.  The
    upper layer is removed only after a complete merged Action has been built.
    Any failure before commit restores the original Action bindings, layer
    order, active metadata, track selection, and animation-data binding.
    """

    del context  # Kept for operator symmetry and Blender undo integration.
    animation_data = getattr(armature, "animation_data", None)
    if animation_data is not None and getattr(
        animation_data,
        "use_tweak_mode",
        False,
    ):
        raise ValueError("Correction layers cannot be merged in NLA tweak mode")
    if animation_data is not None and animation_data.action is not None:
        raise ValueError("Merge requires no active Action binding")
    stack = _validated_stack(armature)
    animation_data = armature.animation_data
    assert animation_data is not None
    upper = _layer_for_id(stack, layer_id)
    upper_index = stack.correction_layers.index(upper)
    if upper_index == 0:
        raise ValueError("The lowest Correction layer has no lower Correction neighbor")
    lower = stack.correction_layers[upper_index - 1]
    result_name = _result_name(stack, lower, upper, name)

    _require_unit_unmuted_pair(lower, upper)
    _require_matching_time_mapping(lower, upper)
    lower_inspection = _inspect_action(lower.action, armature, "Lower Correction")
    upper_inspection = _inspect_action(upper.action, armature, "Upper Correction")
    for label, action in (
        ("Lower Correction", lower.action),
        ("Upper Correction", upper.action),
    ):
        quaternion_errors = _quaternion_curve_errors(action)
        if quaternion_errors:
            raise ValueError(f"{label} is not merge-safe: {quaternion_errors[0]}")
    lower_paths = {data_path for data_path, _index in lower_inspection.channels}
    upper_paths = {data_path for data_path, _index in upper_inspection.channels}
    overlapping_paths = lower_paths & upper_paths
    if overlapping_paths:
        data_path = sorted(overlapping_paths)[0]
        raise ValueError(
            "Correction properties overlap and cannot be losslessly merged: "
            f"{data_path}"
        )
    shared_groups = (lower_inspection.groups & upper_inspection.groups) - {""}
    if shared_groups:
        raise ValueError(
            "Correction Action groups overlap and cannot preserve independent "
            f"metadata: {sorted(shared_groups)[0]}"
        )

    base_checksum = stack.base_action.get(PROP_BASE_CHECKSUM)
    if not isinstance(base_checksum, str) or len(base_checksum) != 64:
        raise ValueError("Base Action checksum metadata is missing or malformed")
    merged_action = lower.action.copy()
    try:
        merged_action.name = f"MB_MERGE_PENDING::{lower.action.name}"
        merged_action.use_fake_user = False
        merged_action[PROP_SCHEMA] = LAYER_SCHEMA_VERSION
        merged_action[PROP_STACK_ID] = armature[PROP_STACK_ID]
        merged_action[PROP_ROLE] = ROLE_CORRECTION
        merged_action[PROP_BASE_ACTION] = stack.base_action.name
        merged_action[PROP_BASE_CHECKSUM] = base_checksum
        merged_action[PROP_LAYER_ID] = lower.layer_id
        merged_action[PROP_LAYER_NAME] = result_name
        merged_action[PROP_LAYER_MUTED] = False
        merged_action[PROP_LAYER_SOLO] = False

        merged_access = access_existing_action(merged_action, armature)
        for curve in upper_inspection.curves:
            _clone_curve(curve, merged_access)
        merged_inspection = _inspect_action(
            merged_action,
            armature,
            "Merged Correction",
        )
        if merged_inspection.channels != (
            lower_inspection.channels | upper_inspection.channels
        ):
            raise RuntimeError("Merged Action did not retain every source channel")
    except Exception:
        _remove_temporary_action(merged_action)
        raise

    original_states = tuple(
        _merge_binding_state(layer) for layer in stack.correction_layers
    )
    original_active_id = armature.get(PROP_ACTIVE_LAYER_ID)
    original_active_name = armature.get(PROP_CORRECTION_ACTION)
    original_animation_action = animation_data.action
    original_animation_slot = (
        action_slot_for_id(original_animation_action, armature)
        if original_animation_action is not None
        else None
    )
    upper_frame_start = float(upper.strip.frame_start)
    upper_strip_name = str(upper.strip.name)
    upper_removed = False
    lower_swapped = False
    try:
        if original_animation_action in {lower.action, upper.action}:
            animation_data.action = None
        assign_nla_strip_action(
            lower.strip,
            merged_action,
            action_slot_for_id(merged_action, armature),
        )
        lower_swapped = True
        animation_data.nla_tracks.remove(upper.track)
        upper_removed = True

        pending = _resolve_stack(armature)
        if pending is None:
            raise RuntimeError("Merged Correction stack could not be resolved")
        merged_layer = _layer_for_id(pending, lower.layer_id)
        merged_layer.track.name = _correction_track_name(
            result_name,
            first=upper_index - 1 == 0,
        )
        merged_layer.strip.name = result_name
        _set_active_values(armature, pending, merged_layer)
        # Merge inspects every curve in the two Actions it changes.  Keep the
        # surrounding stack check structural so a dense Base/Archive is not
        # hashed again during an interactive layer operation.
        validate_layer_stack(armature, check_content=False).raise_for_errors()
    except Exception as error:
        try:
            if upper_removed:
                _restore_tracks_after_failure(
                    armature,
                    upper,
                    upper_frame_start,
                    upper_strip_name,
                    original_states,
                )
            elif lower_swapped:
                assign_nla_strip_action(
                    lower.strip,
                    lower.action,
                    action_slot_for_id(lower.action, armature),
                )
            armature[PROP_ACTIVE_LAYER_ID] = original_active_id
            armature[PROP_CORRECTION_ACTION] = original_active_name
            _restore_binding(
                animation_data,
                original_animation_action,
                original_animation_slot,
            )
            _remove_temporary_action(merged_action)
        except Exception as rollback_error:
            raise RuntimeError(
                f"Correction merge failed and rollback was incomplete: {rollback_error}"
            ) from error
        raise

    _retire_action(lower.action)
    _retire_action(upper.action)
    merged_action.name = f"MB_CORRECTION::{stack.base_action.name}::{result_name}"
    armature[PROP_CORRECTION_ACTION] = merged_action.name
    resolved = _resolve_stack(armature)
    if resolved is None:
        raise RuntimeError("Committed merged Correction stack could not be resolved")
    return _layer_for_id(resolved, lower.layer_id)
