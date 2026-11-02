"""Small Blender 4.5/5.x Action Slot compatibility boundary.

The rest of Motion Bridge deliberately does not reach through Action layers,
keyframe strips, channelbags, or slot handles directly.  Blender 4.5 made
Slotted Actions the supported API and Blender 5 removed the legacy
``Action.fcurves`` facade, so keeping those details here prevents version
checks from leaking into editing code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Iterator

import bpy


@dataclass(frozen=True, slots=True)
class ActionChannelAccess:
    action: bpy.types.Action
    slot: object | None
    channelbag: object | None


@dataclass(frozen=True, slots=True)
class ActionCurveLocation:
    layer_index: int
    strip_index: int
    slot_identifier: str
    curve: bpy.types.FCurve


def _id_type(animated_id: bpy.types.ID) -> str:
    identifier = animated_id.bl_rna.identifier.upper()
    if identifier == "POSE":
        return "OBJECT"
    return identifier


def action_slot_for_id(
    action: bpy.types.Action,
    animated_id: bpy.types.ID,
) -> object | None:
    """Return the explicitly bound/suitable slot for ``animated_id``.

    Active bindings win because one Action may have multiple Object slots.
    The identifier fallback is only needed when reconstructing a saved stack.
    """

    animation_data = getattr(animated_id, "animation_data", None)
    if animation_data is not None and animation_data.action is action:
        slot = getattr(animation_data, "action_slot", None)
        if slot is not None:
            return slot

    slots = getattr(action, "slots", None)
    if slots is None:
        return None
    expected_type = _id_type(animated_id)
    candidates = [
        slot
        for slot in slots
        if getattr(slot, "target_id_type", expected_type) == expected_type
    ]
    if len(candidates) == 1:
        return candidates[0]

    last_identifier = getattr(animation_data, "last_slot_identifier", "")
    for slot in candidates:
        if getattr(slot, "identifier", "") == last_identifier:
            return slot
    return None


def create_action_for_id(
    name: str,
    animated_id: bpy.types.ID,
    *,
    frame_start: float,
    frame_end: float,
) -> ActionChannelAccess:
    """Create a one-slot, one-layer Action and return its channel container."""

    action = bpy.data.actions.new(name)
    action.use_frame_range = True
    action.frame_start = frame_start
    action.frame_end = frame_end

    slots = getattr(action, "slots", None)
    layers = getattr(action, "layers", None)
    if slots is not None and layers is not None:
        slot = slots.new(_id_type(animated_id), animated_id.name)
        layer = layers.new("Motion Bridge Correction")
        strip = layer.strips.new(type="KEYFRAME")
        channelbag = strip.channelbags.new(slot)
        return ActionChannelAccess(action, slot, channelbag)

    # Compatibility with legacy Actions is intentionally contained here.  It
    # is not reached on supported Blender 4.5/5.x releases, but keeps the
    # archive checksum and tests useful when a legacy Action is loaded.
    if getattr(action, "fcurves", None) is None:
        bpy.data.actions.remove(action)
        raise RuntimeError("This Blender build exposes no writable Action API")
    return ActionChannelAccess(action, None, None)


def assign_nla_strip_action(
    strip: bpy.types.NlaStrip,
    action: bpy.types.Action,
    slot: object | None,
) -> None:
    """Assign both an Action and its explicit Slot to an NLA strip."""

    strip.action = action
    if slot is None:
        return
    if hasattr(strip, "action_slot"):
        strip.action_slot = slot
    elif hasattr(strip, "action_slot_handle"):
        strip.action_slot_handle = slot.handle
    else:
        raise RuntimeError("NLA strip cannot store an explicit Action Slot")


def strip_slot_matches(strip: bpy.types.NlaStrip, slot: object | None) -> bool:
    if slot is None:
        return not hasattr(strip, "action_slot") or strip.action_slot is None
    assigned = getattr(strip, "action_slot", None)
    if assigned is not None:
        return assigned == slot
    return getattr(strip, "action_slot_handle", None) == getattr(slot, "handle", None)


def action_slot_for_strip(strip: bpy.types.NlaStrip) -> object | None:
    """Return the Action Slot explicitly assigned to an NLA strip.

    Blender 4.5/5.x normally exposes ``action_slot`` directly.  The handle
    fallback keeps validation scoped to the evaluated slot when loading a
    file written by an API variant that only persisted the numeric handle.
    """

    assigned = getattr(strip, "action_slot", None)
    if assigned is not None:
        return assigned
    handle = getattr(strip, "action_slot_handle", None)
    action = getattr(strip, "action", None)
    if handle is None or action is None:
        return None
    matches = tuple(
        slot
        for slot in getattr(action, "slots", ())
        if getattr(slot, "handle", None) == handle
    )
    return matches[0] if len(matches) == 1 else None


def _channelbags(action: bpy.types.Action) -> Iterator[object]:
    layers = getattr(action, "layers", None)
    if layers is None:
        return
    for layer in layers:
        for strip in layer.strips:
            for channelbag in getattr(strip, "channelbags", ()):  # Blender 4.5+
                yield channelbag


def _channelbag_slot_identifier(
    channelbag: object,
    slot_identifiers_by_handle: dict[int | None, str],
) -> str:
    channel_slot = getattr(channelbag, "slot", None)
    identifier = str(getattr(channel_slot, "identifier", ""))
    if identifier:
        return identifier
    return slot_identifiers_by_handle.get(
        getattr(channelbag, "slot_handle", None),
        "",
    )


def iter_action_fcurves(
    action: bpy.types.Action,
    slot: object | None = None,
) -> Iterator[bpy.types.FCurve]:
    """Yield Action curves in stable layer/strip/channelbag collection order."""

    for location in iter_action_curve_locations(action, slot):
        yield location.curve


def iter_action_curve_locations(
    action: bpy.types.Action,
    slot: object | None = None,
) -> Iterator[ActionCurveLocation]:
    """Yield curves with their layered Action location for deterministic hashes."""

    layers = getattr(action, "layers", None)
    found_layered_structure = False
    slot_identifiers_by_handle = {
        getattr(action_slot, "handle", None): str(
            getattr(action_slot, "identifier", "")
        )
        for action_slot in getattr(action, "slots", ())
    }
    if layers is not None:
        for layer_index, layer in enumerate(layers):
            for strip_index, strip in enumerate(layer.strips):
                for channelbag in getattr(strip, "channelbags", ()):
                    found_layered_structure = True
                    if slot is not None and getattr(
                        channelbag, "slot_handle", None
                    ) != getattr(slot, "handle", None):
                        continue
                    identifier = _channelbag_slot_identifier(
                        channelbag,
                        slot_identifiers_by_handle,
                    )
                    for curve in channelbag.fcurves:
                        yield ActionCurveLocation(
                            layer_index,
                            strip_index,
                            identifier,
                            curve,
                        )
        if found_layered_structure or getattr(action, "is_action_layered", False):
            return

    legacy_curves = getattr(action, "fcurves", None)
    if legacy_curves is not None:
        for curve in legacy_curves:
            yield ActionCurveLocation(-1, -1, "", curve)


def _channelbag_for_slot(action: bpy.types.Action, slot: object | None) -> object | None:
    for channelbag in _channelbags(action):
        if slot is None or getattr(channelbag, "slot_handle", None) == getattr(
            slot, "handle", None
        ):
            return channelbag
    return None


def ensure_fcurve(
    access: ActionChannelAccess,
    data_path: str,
    index: int,
    *,
    group_name: str,
) -> bpy.types.FCurve:
    for curve in iter_action_fcurves(access.action, access.slot):
        if curve.data_path == data_path and curve.array_index == index:
            return curve

    channelbag = access.channelbag or _channelbag_for_slot(access.action, access.slot)
    if channelbag is not None:
        # Blender 4.5 accepts only data_path and index here; group_name was
        # added in 5.0. Assign the channelbag-local group using the shared API.
        curve = channelbag.fcurves.new(data_path, index=index)
        if group_name:
            curve.group = channelbag.groups.get(group_name) or channelbag.groups.new(
                group_name
            )
        return curve
    legacy_curves = getattr(access.action, "fcurves", None)
    if legacy_curves is None:
        raise RuntimeError("Action has no writable FCurve collection")
    return legacy_curves.new(data_path, index=index, action_group=group_name)


def remove_fcurve(
    access: ActionChannelAccess,
    curve: bpy.types.FCurve,
) -> None:
    """Remove one curve from the Action container that owns it."""

    pointer = int(curve.as_pointer())
    for channelbag in _channelbags(access.action):
        if access.slot is not None and getattr(
            channelbag,
            "slot_handle",
            None,
        ) != getattr(access.slot, "handle", None):
            continue
        for candidate in channelbag.fcurves:
            if int(candidate.as_pointer()) == pointer:
                channelbag.fcurves.remove(candidate)
                return
    legacy_curves = getattr(access.action, "fcurves", None)
    if legacy_curves is not None:
        for candidate in legacy_curves:
            if int(candidate.as_pointer()) == pointer:
                legacy_curves.remove(candidate)
                return
    raise ValueError("FCurve does not belong to this Action Slot")


def access_existing_action(
    action: bpy.types.Action,
    animated_id: bpy.types.ID,
) -> ActionChannelAccess:
    slot = action_slot_for_id(action, animated_id)
    return ActionChannelAccess(action, slot, _channelbag_for_slot(action, slot))


_FRAME_EPSILON = 1.0e-6
_SUPPORTED_KEY_INTERPOLATIONS = frozenset({"CONSTANT", "LINEAR", "BEZIER"})


def _finite_frame(frame: float) -> float:
    try:
        value = float(frame)
    except (TypeError, ValueError) as exc:
        raise ValueError("Keyframe frame must be a finite number") from exc
    if not math.isfinite(value):
        raise ValueError("Keyframe frame must be a finite number")
    return value


def _finite_components(
    values: Iterable[float],
    size: int,
    *,
    property_name: str,
) -> tuple[float, ...]:
    try:
        components = tuple(float(value) for value in values)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{property_name} values must be finite numbers") from exc
    if len(components) != size:
        raise ValueError(f"{property_name} requires exactly {size} values")
    if not all(math.isfinite(value) for value in components):
        raise ValueError(f"{property_name} values must be finite numbers")
    return components


def _key_interpolation(interpolation: str) -> str:
    value = str(interpolation).upper()
    if value not in _SUPPORTED_KEY_INTERPOLATIONS:
        supported = ", ".join(sorted(_SUPPORTED_KEY_INTERPOLATIONS))
        raise ValueError(f"Key interpolation must be one of: {supported}")
    return value


def _pose_bone_data_path(bone_name: str, property_name: str) -> str:
    escaped_name = bpy.utils.escape_identifier(str(bone_name))
    return f'pose.bones["{escaped_name}"].{property_name}'


def _property_curves(
    access: ActionChannelAccess,
    data_path: str,
    size: int,
) -> tuple[bpy.types.FCurve | None, ...]:
    curves: list[bpy.types.FCurve | None] = [None] * size
    for curve in iter_action_fcurves(access.action, access.slot):
        index = int(curve.array_index)
        if (
            curve.data_path == data_path
            and 0 <= index < size
            and curves[index] is None
        ):
            curves[index] = curve
    return tuple(curves)


def _nearest_quaternion_key(
    curves: tuple[bpy.types.FCurve | None, ...],
    frame: float,
) -> tuple[float, float, float, float] | None:
    """Return the evaluated quaternion at the nearest prior/next keyed frame."""

    if any(curve is None for curve in curves):
        return None

    keyed_frames = {
        float(point.co[0])
        for curve in curves
        if curve is not None
        for point in curve.keyframe_points
        if math.isfinite(float(point.co[0]))
        and abs(float(point.co[0]) - frame) > _FRAME_EPSILON
    }
    prior_frames = [candidate for candidate in keyed_frames if candidate < frame]
    if prior_frames:
        reference_frame = max(prior_frames)
    else:
        next_frames = [candidate for candidate in keyed_frames if candidate > frame]
        if not next_frames:
            return None
        reference_frame = min(next_frames)

    reference = tuple(float(curve.evaluate(reference_frame)) for curve in curves)
    if not all(math.isfinite(value) for value in reference):
        return None
    norm = math.hypot(*reference)
    if not math.isfinite(norm) or norm == 0.0:
        return None
    return (
        reference[0] / norm,
        reference[1] / norm,
        reference[2] / norm,
        reference[3] / norm,
    )


def _upsert_curve_key(
    curve: bpy.types.FCurve,
    frame: float,
    value: float,
    interpolation: str,
) -> None:
    matches = [
        (index, abs(float(point.co[0]) - frame))
        for index, point in enumerate(curve.keyframe_points)
        if abs(float(point.co[0]) - frame) <= _FRAME_EPSILON
    ]
    if matches:
        keep_index = min(matches, key=lambda item: item[1])[0]
        for index, _distance in reversed(matches):
            if index != keep_index:
                curve.keyframe_points.remove(curve.keyframe_points[index], fast=True)
                if index < keep_index:
                    keep_index -= 1
        point = curve.keyframe_points[keep_index]
        point.co = (frame, value)
        point.interpolation = interpolation
    else:
        point = curve.keyframe_points.insert(frame, value, options={"FAST"})
        point.interpolation = interpolation
    if interpolation == "BEZIER":
        point.handle_left_type = "AUTO_CLAMPED"
        point.handle_right_type = "AUTO_CLAMPED"
    curve.update()


def _insert_quaternion_property_key(
    access: ActionChannelAccess,
    data_path: str,
    group_name: str,
    frame: float,
    quaternion: Iterable[float],
    *,
    interpolation: str = "LINEAR",
) -> None:
    """Upsert a normalized, continuous WXYZ quaternion correction key."""

    # Finish all input validation before creating curves or changing keyframes.
    key_frame = _finite_frame(frame)
    key_interpolation = _key_interpolation(interpolation)
    values = _finite_components(
        quaternion,
        4,
        property_name="Quaternion correction",
    )
    norm = math.hypot(*values)
    if not math.isfinite(norm) or norm == 0.0:
        raise ValueError("Quaternion correction must have a non-zero finite norm")
    normalized = tuple(value / norm for value in values)

    existing_curves = _property_curves(access, data_path, 4)
    reference = _nearest_quaternion_key(existing_curves, key_frame)
    if reference is not None and sum(
        value * reference_value
        for value, reference_value in zip(normalized, reference, strict=True)
    ) < 0.0:
        normalized = tuple(-value for value in normalized)

    curves = tuple(
        ensure_fcurve(access, data_path, index, group_name=group_name)
        for index in range(4)
    )
    for curve, value in zip(curves, normalized, strict=True):
        _upsert_curve_key(curve, key_frame, value, key_interpolation)


def insert_quaternion_key(
    access: ActionChannelAccess,
    bone_name: str,
    frame: float,
    quaternion: Iterable[float],
    *,
    interpolation: str = "LINEAR",
) -> None:
    """Upsert a normalized, continuous WXYZ pose-bone correction key."""

    _insert_quaternion_property_key(
        access,
        _pose_bone_data_path(bone_name, "rotation_quaternion"),
        bone_name,
        frame,
        quaternion,
        interpolation=interpolation,
    )


def insert_object_quaternion_key(
    access: ActionChannelAccess,
    frame: float,
    quaternion: Iterable[float],
    *,
    interpolation: str = "LINEAR",
) -> None:
    """Upsert a normalized, hemisphere-continuous Object Root WXYZ key."""

    _insert_quaternion_property_key(
        access,
        "rotation_quaternion",
        "Root",
        frame,
        quaternion,
        interpolation=interpolation,
    )


def _insert_location_property_key(
    access: ActionChannelAccess,
    data_path: str,
    group_name: str,
    frame: float,
    location: Iterable[float],
    *,
    interpolation: str = "LINEAR",
) -> None:
    """Upsert a complete finite XYZ location correction key."""

    # Finish all input validation before creating curves or changing keyframes.
    key_frame = _finite_frame(frame)
    key_interpolation = _key_interpolation(interpolation)
    values = _finite_components(
        location,
        3,
        property_name="Location correction",
    )
    curves = tuple(
        ensure_fcurve(access, data_path, index, group_name=group_name)
        for index in range(3)
    )
    for curve, value in zip(curves, values, strict=True):
        _upsert_curve_key(curve, key_frame, value, key_interpolation)


def insert_location_key(
    access: ActionChannelAccess,
    bone_name: str,
    frame: float,
    location: Iterable[float],
    *,
    interpolation: str = "LINEAR",
) -> None:
    """Upsert a complete finite XYZ pose-bone location correction key."""

    _insert_location_property_key(
        access,
        _pose_bone_data_path(bone_name, "location"),
        bone_name,
        frame,
        location,
        interpolation=interpolation,
    )


def insert_object_location_key(
    access: ActionChannelAccess,
    frame: float,
    location: Iterable[float],
    *,
    interpolation: str = "LINEAR",
) -> None:
    """Upsert a complete finite Object Root XYZ correction key."""

    _insert_location_property_key(
        access,
        "location",
        "Root",
        frame,
        location,
        interpolation=interpolation,
    )


def _remove_property_key(
    access: ActionChannelAccess,
    bone_name: str,
    frame: float,
    *,
    property_name: str,
    size: int,
) -> bool:
    key_frame = _finite_frame(frame)
    data_path = _pose_bone_data_path(bone_name, property_name)
    removed = False
    for curve in _property_curves(access, data_path, size):
        if curve is None:
            continue
        matches = tuple(
            index
            for index, point in enumerate(curve.keyframe_points)
            if abs(float(point.co[0]) - key_frame) <= _FRAME_EPSILON
        )
        for index in reversed(matches):
            curve.keyframe_points.remove(curve.keyframe_points[index], fast=True)
            removed = True
        if matches:
            curve.update()
    return removed


def remove_quaternion_key(
    access: ActionChannelAccess,
    bone_name: str,
    frame: float,
) -> bool:
    """Remove every WXYZ correction key matching ``frame`` for one bone."""

    return _remove_property_key(
        access,
        bone_name,
        frame,
        property_name="rotation_quaternion",
        size=4,
    )


def remove_location_key(
    access: ActionChannelAccess,
    bone_name: str,
    frame: float,
) -> bool:
    """Remove every XYZ location correction key matching ``frame`` for one bone."""

    return _remove_property_key(
        access,
        bone_name,
        frame,
        property_name="location",
        size=3,
    )


def _remove_property_keys_in_range(
    access: ActionChannelAccess,
    bone_name: str,
    frame_start: float,
    frame_end: float,
    *,
    property_name: str,
    size: int,
) -> int:
    start = _finite_frame(frame_start)
    end = _finite_frame(frame_end)
    if end < start:
        raise ValueError("Key removal range is reversed")
    data_path = _pose_bone_data_path(bone_name, property_name)
    removed = 0
    for curve in _property_curves(access, data_path, size):
        if curve is None:
            continue
        matches = tuple(
            index
            for index, point in enumerate(curve.keyframe_points)
            if start - _FRAME_EPSILON
            <= float(point.co[0])
            <= end + _FRAME_EPSILON
        )
        for index in reversed(matches):
            curve.keyframe_points.remove(curve.keyframe_points[index], fast=True)
            removed += 1
        if matches:
            curve.update()
    return removed


def remove_quaternion_keys_in_range(
    access: ActionChannelAccess,
    bone_name: str,
    frame_start: float,
    frame_end: float,
) -> int:
    """Remove WXYZ correction points inside an inclusive Action-time range."""

    return _remove_property_keys_in_range(
        access,
        bone_name,
        frame_start,
        frame_end,
        property_name="rotation_quaternion",
        size=4,
    )


def remove_location_keys_in_range(
    access: ActionChannelAccess,
    bone_name: str,
    frame_start: float,
    frame_end: float,
) -> int:
    """Remove XYZ correction points inside an inclusive Action-time range."""

    return _remove_property_keys_in_range(
        access,
        bone_name,
        frame_start,
        frame_end,
        property_name="location",
        size=3,
    )


def action_slot_identifiers(action: bpy.types.Action) -> tuple[tuple[str, str], ...]:
    slots = getattr(action, "slots", None)
    if slots is None:
        return ()
    return tuple(
        (
            str(getattr(slot, "identifier", "")),
            str(getattr(slot, "target_id_type", "")),
        )
        for slot in slots
    )
