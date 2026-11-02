from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
import uuid

import bpy

from ..canonical.rig_spec import SKELETON_ID
from ..retargeting.recipe import PROP_RETARGET_RECIPE
from .action_api import (
    ActionChannelAccess,
    access_existing_action,
    action_slot_for_id,
    action_slot_for_strip,
    action_slot_identifiers,
    assign_nla_strip_action,
    create_action_for_id,
    iter_action_curve_locations,
    iter_action_fcurves,
    strip_slot_matches,
)


LAYER_SCHEMA_VERSION = 1
BASE_TRACK_NAME = "[Motion Bridge] Base"
CORRECTION_TRACK_NAME = "[Motion Bridge] Correction"
CORRECTION_TRACK_PREFIX = f"{CORRECTION_TRACK_NAME} :: "

PROP_SCHEMA = "motion_bridge_layer_schema"
PROP_STACK_ID = "motion_bridge_stack_id"
PROP_ROLE = "motion_bridge_role"
PROP_ARCHIVE_ACTION = "motion_bridge_archive_action"
PROP_BASE_ACTION = "motion_bridge_base_action"
PROP_CORRECTION_ACTION = "motion_bridge_correction_action"
PROP_ARCHIVE_CHECKSUM = "motion_bridge_archive_checksum"
PROP_BASE_CHECKSUM = "motion_bridge_base_checksum"
PROP_CHECKSUM_ALGORITHM = "motion_bridge_checksum_algorithm"
PROP_PROVENANCE = "motion_bridge_archive_provenance"
PROP_LAYER_ID = "motion_bridge_layer_id"
PROP_LAYER_NAME = "motion_bridge_layer_name"
PROP_ACTIVE_LAYER_ID = "motion_bridge_active_layer_id"
PROP_LAYER_MUTED = "motion_bridge_layer_muted"
PROP_LAYER_SOLO = "motion_bridge_layer_solo"
PROP_CORRECTIONS_BYPASSED = "motion_bridge_corrections_bypassed"

ROLE_ARCHIVE = "ARCHIVE"
ROLE_BASE = "BASE"
ROLE_CORRECTION = "CORRECTION"
_HIPS_LOCATION_DATA_PATH = 'pose.bones["Hips"].location'
_ROOT_LOCATION_DATA_PATH = "location"
_ROOT_ROTATION_DATA_PATH = "rotation_quaternion"
_ROOT_TRANSFORM_DRIVER_PATHS = frozenset(
    {
        "location",
        "rotation_axis_angle",
        "rotation_euler",
        "rotation_quaternion",
        "scale",
        "delta_location",
        "delta_rotation_euler",
        "delta_rotation_quaternion",
        "delta_scale",
    }
)


@dataclass(frozen=True, slots=True)
class CorrectionLayer:
    """One sparse quaternion-safe COMBINE layer in deterministic NLA order."""

    layer_id: str
    name: str
    action: bpy.types.Action
    track: bpy.types.NlaTrack
    strip: bpy.types.NlaStrip

    @property
    def access(self) -> ActionChannelAccess:
        return access_existing_action(self.action, self.strip.id_data)

    @property
    def muted(self) -> bool:
        return bool(self.track.mute or self.strip.mute)

    @property
    def solo(self) -> bool:
        return bool(self.action.get(PROP_LAYER_SOLO, False))


@dataclass(frozen=True, slots=True)
class CorrectionLayerStack:
    archive_action: bpy.types.Action
    base_action: bpy.types.Action
    correction_action: bpy.types.Action
    base_track: bpy.types.NlaTrack
    base_strip: bpy.types.NlaStrip
    correction_track: bpy.types.NlaTrack
    correction_strip: bpy.types.NlaStrip
    correction_layers: tuple[CorrectionLayer, ...] = ()
    active_layer_id: str = ""

    @property
    def correction_access(self) -> ActionChannelAccess:
        return access_existing_action(self.correction_action, self.base_strip.id_data)

    @property
    def active_layer(self) -> CorrectionLayer:
        for layer in self.correction_layers:
            if layer.layer_id == self.active_layer_id:
                return layer
        # Legacy callers can still construct the original seven-field shape.
        return CorrectionLayer(
            self.active_layer_id,
            str(self.correction_strip.name),
            self.correction_action,
            self.correction_track,
            self.correction_strip,
        )


@dataclass(frozen=True, slots=True)
class LayerStackValidation:
    errors: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    stack: CorrectionLayerStack | None = None

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def raise_for_errors(self) -> None:
        if self.errors:
            raise ValueError(
                "Invalid Motion Bridge layer stack: " + "; ".join(self.errors)
            )


def _finite(value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Action checksum cannot encode NaN or infinity")
    return value


def _curve_payload(curve: bpy.types.FCurve) -> dict[str, Any]:
    points: list[dict[str, Any]] = []
    for point in curve.keyframe_points:
        points.append(
            {
                "co": [_finite(point.co[0]), _finite(point.co[1])],
                "handleLeft": [
                    _finite(point.handle_left[0]),
                    _finite(point.handle_left[1]),
                ],
                "handleRight": [
                    _finite(point.handle_right[0]),
                    _finite(point.handle_right[1]),
                ],
                "handleLeftType": point.handle_left_type,
                "handleRightType": point.handle_right_type,
                "interpolation": point.interpolation,
                "easing": point.easing,
                "amplitude": _finite(point.amplitude),
                "back": _finite(point.back),
                "period": _finite(point.period),
            }
        )
    return {
        "dataPath": curve.data_path,
        "arrayIndex": int(curve.array_index),
        "extrapolation": curve.extrapolation,
        "group": curve.group.name if curve.group is not None else "",
        "groupMute": bool(curve.group.mute) if curve.group is not None else False,
        "mute": bool(curve.mute),
        "points": points,
        "sampledPoints": [
            [_finite(point.co[0]), _finite(point.co[1])]
            for point in curve.sampled_points
        ],
        "modifiers": [_modifier_payload(modifier) for modifier in curve.modifiers],
    }


def _primitive_rna_value(value: Any) -> Any:
    if isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return _finite(value)
    try:
        return [_finite(item) for item in value]
    except (TypeError, ValueError):
        return None


def _modifier_payload(modifier: bpy.types.FModifier) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    collections: dict[str, list[dict[str, Any]]] = {}
    for prop in modifier.bl_rna.properties:
        identifier = prop.identifier
        if identifier in {"rna_type", "type"} or prop.is_readonly:
            continue
        value = getattr(modifier, identifier)
        if prop.type in {"BOOLEAN", "INT", "FLOAT", "STRING", "ENUM"}:
            encoded = _primitive_rna_value(value)
            if encoded is not None:
                properties[identifier] = encoded
        elif prop.type == "COLLECTION":
            encoded_items: list[dict[str, Any]] = []
            for item in value:
                encoded_item: dict[str, Any] = {}
                for item_prop in item.bl_rna.properties:
                    if item_prop.identifier == "rna_type" or item_prop.is_readonly:
                        continue
                    if item_prop.type not in {
                        "BOOLEAN",
                        "INT",
                        "FLOAT",
                        "STRING",
                        "ENUM",
                    }:
                        continue
                    encoded = _primitive_rna_value(getattr(item, item_prop.identifier))
                    if encoded is not None:
                        encoded_item[item_prop.identifier] = encoded
                encoded_items.append(encoded_item)
            collections[identifier] = encoded_items
    return {
        "type": modifier.type,
        "properties": properties,
        "collections": collections,
    }


def action_checksum(action: bpy.types.Action) -> str:
    """Hash animation values with memory bounded to one FCurve payload."""

    locations = list(iter_action_curve_locations(action))
    locations.sort(
        key=lambda location: (
            location.layer_index,
            location.strip_index,
            location.slot_identifier,
            location.curve.data_path,
            int(location.curve.array_index),
            location.curve.group.name if location.curve.group is not None else "",
        )
    )

    def encode(value: Any) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    digest = hashlib.sha256()
    digest.update(b'{"curves":[')
    for index, location in enumerate(locations):
        if index:
            digest.update(b",")
        curve = _curve_payload(location.curve)
        curve["layerIndex"] = location.layer_index
        curve["stripIndex"] = location.strip_index
        curve["slotIdentifier"] = location.slot_identifier
        digest.update(encode(curve))
    digest.update(b'],"frameRange":')
    digest.update(
        encode([_finite(action.frame_range[0]), _finite(action.frame_range[1])])
    )
    digest.update(b',"schema":1,"slots":')
    digest.update(encode(sorted(action_slot_identifiers(action))))
    digest.update(b"}")
    return digest.hexdigest()


def action_content_checksum(action: bpy.types.Action) -> str:
    """Hash evaluated animation content without Blender-assigned Slot names.

    Action and Slot identifiers can gain ``.001`` suffixes when a Source Take
    is imported into a workfile that already contains same-named datablocks.
    A take identity must remain stable across that scene-local renaming, while
    still distinguishing different animation stored in the same source file.
    """

    def encode(value: Any) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    curves: list[bytes] = []
    for location in iter_action_curve_locations(action):
        curve = _curve_payload(location.curve)
        # Group names are editor labels rather than animation semantics and may
        # also be importer-generated. Group mute remains part of the payload.
        curve.pop("group", None)
        curve["layerIndex"] = location.layer_index
        curve["stripIndex"] = location.strip_index
        curves.append(encode(curve))
    curves.sort()

    slot_target_types = sorted(
        target_type for _identifier, target_type in action_slot_identifiers(action)
    )
    digest = hashlib.sha256()
    digest.update(b'{"curves":[')
    for index, curve in enumerate(curves):
        if index:
            digest.update(b",")
        digest.update(curve)
    digest.update(b'],"frameRange":')
    digest.update(
        encode([_finite(action.frame_range[0]), _finite(action.frame_range[1])])
    )
    digest.update(b',"schema":1,"slotTargetTypes":')
    digest.update(encode(slot_target_types))
    digest.update(b"}")
    return digest.hexdigest()


def _default_provenance(base_action: bpy.types.Action) -> dict[str, Any]:
    library_path = (
        base_action.library.filepath if base_action.library is not None else ""
    )
    value: dict[str, Any] = {
        "sourceAction": base_action.name,
        "sourceBlend": str(Path(bpy.data.filepath).resolve())
        if bpy.data.filepath
        else "",
        "sourceLibrary": library_path,
    }
    recipe_json = base_action.get(PROP_RETARGET_RECIPE)
    if recipe_json is not None:
        try:
            recipe = json.loads(str(recipe_json))
        except json.JSONDecodeError as error:
            raise ValueError("Base Action retarget recipe is not valid JSON") from error
        if not isinstance(recipe, dict):
            raise ValueError("Base Action retarget recipe must be a JSON object")
        value["retargetRecipe"] = recipe
    return value


def _provenance_json(
    base_action: bpy.types.Action,
    provenance: Mapping[str, Any] | None,
) -> str:
    value = _default_provenance(base_action)
    if provenance:
        value.update(provenance)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Archive provenance must be finite JSON data") from error


def _canonical_armature(armature: bpy.types.Object) -> None:
    if armature is None or armature.type != "ARMATURE":
        raise ValueError("Correction layers require an armature Object")
    if armature.get("motion_bridge_skeleton_id") != SKELETON_ID:
        raise ValueError(
            "Correction layers require the Motion Bridge Canonical skeleton"
        )


def _corrections_bypassed(armature: bpy.types.Object) -> bool:
    """Return the persisted Before/After preview state without resolving curves."""

    return bool(armature.get(PROP_CORRECTIONS_BYPASSED, False))


def _named_track(
    animation_data: bpy.types.AnimData,
    name: str,
) -> bpy.types.NlaTrack | None:
    matches = [track for track in animation_data.nla_tracks if track.name == name]
    return matches[0] if len(matches) == 1 else None


def _archive_for_stack(stack_id: str) -> list[bpy.types.Action]:
    return [
        action
        for action in bpy.data.actions
        if action.get(PROP_STACK_ID) == stack_id
        and action.get(PROP_ROLE) == ROLE_ARCHIVE
    ]


def _legacy_layer_id(stack_id: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"https://motionbridge.local/stacks/{stack_id}/corrections/0",
        )
    )


def _layer_id(action: bpy.types.Action, stack_id: str) -> str:
    value = action.get(PROP_LAYER_ID)
    if isinstance(value, str) and value:
        return value
    return _legacy_layer_id(stack_id)


def _layer_name(action: bpy.types.Action, index: int) -> str:
    value = action.get(PROP_LAYER_NAME)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return f"Correction {index + 1}"


def _correction_tracks(
    animation_data: bpy.types.AnimData,
    stack_id: str,
) -> list[bpy.types.NlaTrack]:
    result: list[bpy.types.NlaTrack] = []
    for track in animation_data.nla_tracks:
        if len(track.strips) != 1:
            continue
        action = track.strips[0].action
        if (
            action is not None
            and action.get(PROP_STACK_ID) == stack_id
            and action.get(PROP_ROLE) == ROLE_CORRECTION
        ):
            result.append(track)
    return result


def _foreign_nla_tracks(
    animation_data: bpy.types.AnimData,
    stack_id: str,
) -> list[bpy.types.NlaTrack]:
    """Return tracks that are not owned by the requested Motion Bridge stack."""

    result: list[bpy.types.NlaTrack] = []
    for track in animation_data.nla_tracks:
        if len(track.strips) == 1:
            action = track.strips[0].action
            if (
                action is not None
                and action.get(PROP_STACK_ID) == stack_id
                and action.get(PROP_ROLE) in {ROLE_BASE, ROLE_CORRECTION}
            ):
                continue
        result.append(track)
    return result


def has_unsupported_nla_coexistence(armature: bpy.types.Object | None) -> bool:
    """Return whether Motion Bridge must leave ownership-external NLA alone."""

    if armature is None or armature.type != "ARMATURE":
        return False
    animation_data = armature.animation_data
    if animation_data is None or not animation_data.nla_tracks:
        return False
    stack_id = armature.get(PROP_STACK_ID)
    if not isinstance(stack_id, str) or not stack_id:
        return True
    return bool(_foreign_nla_tracks(animation_data, stack_id))


def _resolve_stack(armature: bpy.types.Object) -> CorrectionLayerStack | None:
    animation_data = armature.animation_data
    stack_id = armature.get(PROP_STACK_ID)
    if animation_data is None or not isinstance(stack_id, str) or not stack_id:
        return None
    base_track = _named_track(animation_data, BASE_TRACK_NAME)
    correction_tracks = _correction_tracks(animation_data, stack_id)
    archive_name = armature.get(PROP_ARCHIVE_ACTION)
    named_archive = (
        bpy.data.actions.get(archive_name) if isinstance(archive_name, str) else None
    )
    if (
        named_archive is not None
        and named_archive.get(PROP_STACK_ID) == stack_id
        and named_archive.get(PROP_ROLE) == ROLE_ARCHIVE
    ):
        archives = [named_archive]
    else:
        archives = _archive_for_stack(stack_id)
    if (
        base_track is None
        or len(base_track.strips) != 1
        or not correction_tracks
        or len(archives) != 1
    ):
        return None
    base_strip = base_track.strips[0]
    if base_strip.action is None:
        return None
    layers: list[CorrectionLayer] = []
    for index, correction_track in enumerate(correction_tracks):
        correction_strip = correction_track.strips[0]
        correction_action = correction_strip.action
        if correction_action is None:
            return None
        layers.append(
            CorrectionLayer(
                _layer_id(correction_action, stack_id),
                _layer_name(correction_action, index),
                correction_action,
                correction_track,
                correction_strip,
            )
        )

    active_id = armature.get(PROP_ACTIVE_LAYER_ID)
    active = next(
        (
            layer
            for layer in layers
            if isinstance(active_id, str) and layer.layer_id == active_id
        ),
        None,
    )
    if active is None:
        legacy_action_name = armature.get(PROP_CORRECTION_ACTION)
        active = next(
            (
                layer
                for layer in layers
                if isinstance(legacy_action_name, str)
                and layer.action.name == legacy_action_name
            ),
            layers[0],
        )
    return CorrectionLayerStack(
        archives[0],
        base_strip.action,
        active.action,
        base_track,
        base_strip,
        active.track,
        active.strip,
        tuple(layers),
        active.layer_id,
    )


def get_correction_layer_stack(
    armature: bpy.types.Object | None,
) -> CorrectionLayerStack | None:
    """Resolve stack references without checksum work for lightweight UI draw."""

    if armature is None or armature.type != "ARMATURE":
        return None
    return _resolve_stack(armature)


def _local_action_user_count(
    armature: bpy.types.Object,
    action: bpy.types.Action,
) -> int:
    """Count Action consumers owned by this armature's animation data."""

    animation_data = armature.animation_data
    if animation_data is None:
        return 0
    users = int(animation_data.action is action)
    users += sum(
        strip.action is action
        for track in animation_data.nla_tracks
        for strip in track.strips
    )
    return users


def action_is_unshared_for_armature(
    armature: bpy.types.Object | None,
    action: bpy.types.Action | None,
) -> bool:
    """Return whether ``action`` has consumers only on this armature.

    The fake-user reservation is not an external consumer. A local NLA Tweak
    session legitimately binds the same Action both to its strip and as the
    temporary active Action, so ownership is based on actual local bindings
    rather than a hard-coded ``users == 1`` check.
    """

    if armature is None or armature.type != "ARMATURE" or action is None:
        return False
    try:
        if not any(candidate is action for candidate in bpy.data.actions):
            return False
        local_users = _local_action_user_count(armature, action)
        global_users = int(action.users) - int(bool(action.use_fake_user))
    except ReferenceError:
        return False
    return local_users > 0 and global_users == local_users


def correction_actions_are_unshared(
    armature: bpy.types.Object | None,
) -> bool:
    """Return whether every Correction Action is private to this armature.

    Blender counts an Action bound by NLA Tweak Mode twice: once for its strip
    and once for the temporary active Action binding.  Comparing global users
    with this armature's actual bindings therefore detects another Scene or
    Object consumer without rejecting a valid local edit session.
    """

    if armature is None or armature.type != "ARMATURE":
        return False
    stack = _resolve_stack(armature)
    if stack is None or not stack.correction_layers:
        return False
    return all(
        action_is_unshared_for_armature(armature, layer.action)
        for layer in stack.correction_layers
    )


def require_unshared_correction_actions(
    armature: bpy.types.Object,
    stack: CorrectionLayerStack | None = None,
) -> CorrectionLayerStack:
    """Reject mutation while another Object or Scene shares a Correction.

    A Blender Full Copy duplicates Objects and NLA bindings but keeps their
    Actions shared.  Editing or deleting Action-backed layer metadata in that
    state can silently change or invalidate the original Scene.  Phase 0 uses
    a conservative hard block; making Actions single-user is an explicit user
    operation and is never performed implicitly here.
    """

    if armature is None or armature.type != "ARMATURE":
        raise ValueError("Correction changes require an armature Object")
    resolved = stack
    if resolved is None:
        validation = validate_layer_stack(armature, check_content=False)
        validation.raise_for_errors()
        resolved = validation.stack
    if resolved is None or not resolved.correction_layers:
        raise ValueError("Armature has no resolvable Motion Bridge Correction stack")
    shared = tuple(
        layer.name
        for layer in resolved.correction_layers
        if not action_is_unshared_for_armature(armature, layer.action)
    )
    if shared:
        raise ValueError(
            "Correction changes require unshared Correction Actions; "
            "another Object or Full Copy Scene uses: " + ", ".join(shared)
        )
    return resolved


def _configure_strip(
    strip: bpy.types.NlaStrip,
    *,
    blend_type: str,
    extrapolation: str,
) -> None:
    strip.blend_type = blend_type
    strip.influence = 1.0
    strip.mute = False
    strip.use_animated_influence = False
    strip.use_animated_time = False
    strip.use_auto_blend = False
    strip.blend_in = 0.0
    strip.blend_out = 0.0
    strip.extrapolation = extrapolation


def _correction_track_name(name: str, *, first: bool) -> str:
    return CORRECTION_TRACK_NAME if first else f"{CORRECTION_TRACK_PREFIX}{name}"


def _protect_action(action: bpy.types.Action) -> None:
    """Apply Graph Editor guards; the checksum remains the authority."""

    for curve in iter_action_fcurves(action):
        curve.lock = True
        if hasattr(curve, "hide"):
            curve.hide = True
        if curve.group is not None and hasattr(curve.group, "lock"):
            curve.group.lock = True


def _protection_state(action: bpy.types.Action) -> list[tuple[Any, ...]]:
    return [
        (
            curve,
            bool(curve.lock),
            bool(curve.hide) if hasattr(curve, "hide") else None,
            curve.group,
            bool(curve.group.lock)
            if curve.group is not None and hasattr(curve.group, "lock")
            else None,
        )
        for curve in iter_action_fcurves(action)
    ]


def _restore_protection_state(state: list[tuple[Any, ...]]) -> None:
    for curve, locked, hidden, group, group_locked in state:
        curve.lock = locked
        if hidden is not None:
            curve.hide = hidden
        if group is not None and group_locked is not None:
            group.lock = group_locked


def _strip_for_action(
    track: bpy.types.NlaTrack,
    name: str,
    action: bpy.types.Action,
    slot: object | None,
    frame_start: float,
) -> bpy.types.NlaStrip:
    # The collection constructor still declares ``start`` as an integer in
    # Blender 4.5/5.0; the strip property itself accepts subframes.
    strip = track.strips.new(name, math.floor(frame_start), action)
    strip.frame_start = frame_start
    assign_nla_strip_action(strip, action, slot)
    return strip


def _restore_binding(
    animation_data: bpy.types.AnimData,
    action: bpy.types.Action | None,
    slot: object | None,
) -> None:
    animation_data.action = action
    if (
        action is not None
        and slot is not None
        and hasattr(animation_data, "action_slot")
    ):
        animation_data.action_slot = slot


def create_correction_layer(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    *,
    provenance: Mapping[str, Any] | None = None,
    _on_stage: Callable[[str], None] | None = None,
) -> CorrectionLayerStack:
    """Create Archive/Base/Correction assets around the active Base Action.

    Re-running this function returns the existing valid stack.  A partially
    damaged stack is rejected instead of silently creating duplicate tracks.
    Selection, mode, and timeline state are never used as working storage and
    therefore remain unchanged on both success and failure.
    """

    _canonical_armature(armature)
    existing_id = armature.get(PROP_STACK_ID)
    if existing_id:
        return _validated_stack(armature)

    animation_data = armature.animation_data
    base_action = animation_data.action if animation_data is not None else None
    if base_action is None:
        raise ValueError("Canonical armature must have an active Base Action")
    if not action_is_unshared_for_armature(armature, base_action):
        raise ValueError(
            "Correction creation requires an unshared Base Action; another "
            "Object or Full Copy Scene uses it"
        )
    base_slot = action_slot_for_id(base_action, armature)
    if getattr(base_action, "slots", None) and base_slot is None:
        raise ValueError("Active Base Action has no unambiguous armature Action Slot")
    if animation_data.nla_tracks:
        raise ValueError(
            "Unsupported NLA coexistence: Motion Bridge found unrelated NLA tracks; "
            "the existing tracks were not modified"
        )
    if not any(iter_action_fcurves(base_action, base_slot)):
        raise ValueError("Active Base Action has no animation curves")

    frame_start, frame_end = (float(value) for value in base_action.frame_range)
    if not all(math.isfinite(value) for value in (frame_start, frame_end)):
        raise ValueError("Base Action frame range must be finite")
    if frame_end < frame_start:
        raise ValueError("Base Action frame range is reversed")

    stack_id = str(uuid.uuid4())
    layer_id = str(uuid.uuid4())
    layer_name = "Correction 1"
    checksum = action_checksum(base_action)
    provenance_json = _provenance_json(base_action, provenance)
    archive_action: bpy.types.Action | None = None
    correction_action: bpy.types.Action | None = None
    base_track: bpy.types.NlaTrack | None = None
    correction_track: bpy.types.NlaTrack | None = None
    base_protection_state = _protection_state(base_action)
    armature_metadata_keys = (
        PROP_SCHEMA,
        PROP_STACK_ID,
        PROP_ARCHIVE_ACTION,
        PROP_BASE_ACTION,
        PROP_CORRECTION_ACTION,
        PROP_ACTIVE_LAYER_ID,
        PROP_CORRECTIONS_BYPASSED,
        PROP_BASE_CHECKSUM,
        PROP_RETARGET_RECIPE,
    )
    base_metadata_keys = (
        PROP_SCHEMA,
        PROP_STACK_ID,
        PROP_ROLE,
        PROP_ARCHIVE_ACTION,
        PROP_BASE_ACTION,
        PROP_BASE_CHECKSUM,
    )
    metadata_state = tuple(
        (owner, key, key in owner, owner.get(key))
        for owner, keys in (
            (armature, armature_metadata_keys),
            (base_action, base_metadata_keys),
        )
        for key in keys
    )
    try:
        archive_action = base_action.copy()
        archive_action.name = f"MB_ARCHIVE::{base_action.name}"
        archive_action.use_fake_user = True
        _protect_action(archive_action)
        _protect_action(base_action)

        correction_access = create_action_for_id(
            f"MB_CORRECTION::{base_action.name}",
            armature,
            frame_start=frame_start,
            frame_end=frame_end,
        )
        correction_action = correction_access.action

        # An Action cannot be both the active action-track and an NLA strip.
        # Detach it explicitly, retaining the slot for the Base strip.
        animation_data.action = None
        base_track = animation_data.nla_tracks.new()
        base_track.name = BASE_TRACK_NAME
        base_strip = _strip_for_action(
            base_track,
            "Base",
            base_action,
            base_slot,
            frame_start,
        )
        _configure_strip(base_strip, blend_type="REPLACE", extrapolation="HOLD")
        base_track.lock = True
        base_track.mute = False
        base_track.select = False

        correction_track = animation_data.nla_tracks.new()
        correction_track.name = _correction_track_name(layer_name, first=True)
        correction_strip = _strip_for_action(
            correction_track,
            layer_name,
            correction_action,
            correction_access.slot,
            frame_start,
        )
        _configure_strip(
            correction_strip,
            blend_type="COMBINE",
            extrapolation="NOTHING",
        )
        correction_track.lock = False
        correction_track.mute = False
        correction_track.select = True

        archive_action[PROP_SCHEMA] = LAYER_SCHEMA_VERSION
        archive_action[PROP_STACK_ID] = stack_id
        archive_action[PROP_ROLE] = ROLE_ARCHIVE
        archive_action[PROP_ARCHIVE_CHECKSUM] = checksum
        archive_action[PROP_CHECKSUM_ALGORITHM] = "sha256"
        archive_action[PROP_PROVENANCE] = provenance_json

        base_action[PROP_SCHEMA] = LAYER_SCHEMA_VERSION
        base_action[PROP_STACK_ID] = stack_id
        base_action[PROP_ROLE] = ROLE_BASE
        base_action[PROP_ARCHIVE_ACTION] = archive_action.name
        base_action[PROP_BASE_CHECKSUM] = checksum

        correction_action[PROP_SCHEMA] = LAYER_SCHEMA_VERSION
        correction_action[PROP_STACK_ID] = stack_id
        correction_action[PROP_ROLE] = ROLE_CORRECTION
        correction_action[PROP_BASE_ACTION] = base_action.name
        correction_action[PROP_BASE_CHECKSUM] = checksum
        correction_action[PROP_LAYER_ID] = layer_id
        correction_action[PROP_LAYER_NAME] = layer_name
        correction_action[PROP_LAYER_MUTED] = False
        correction_action[PROP_LAYER_SOLO] = False

        armature[PROP_SCHEMA] = LAYER_SCHEMA_VERSION
        armature[PROP_STACK_ID] = stack_id
        armature[PROP_ARCHIVE_ACTION] = archive_action.name
        armature[PROP_BASE_ACTION] = base_action.name
        armature[PROP_CORRECTION_ACTION] = correction_action.name
        armature[PROP_ACTIVE_LAYER_ID] = layer_id
        armature[PROP_CORRECTIONS_BYPASSED] = False
        recipe_json = base_action.get(PROP_RETARGET_RECIPE)
        if isinstance(recipe_json, str) and recipe_json:
            armature[PROP_RETARGET_RECIPE] = recipe_json
        if _on_stage is not None:
            _on_stage("metadata_written")

        stack = CorrectionLayerStack(
            archive_action,
            base_action,
            correction_action,
            base_track,
            base_strip,
            correction_track,
            correction_strip,
            (
                CorrectionLayer(
                    layer_id,
                    layer_name,
                    correction_action,
                    correction_track,
                    correction_strip,
                ),
            ),
            layer_id,
        )
        validation = validate_layer_stack(armature, check_content=False)
        validation.raise_for_errors()
        return stack
    except Exception:
        # Roll back new data-blocks/tracks; the caller's Base Action remains.
        if correction_track is not None:
            animation_data.nla_tracks.remove(correction_track)
        if base_track is not None:
            animation_data.nla_tracks.remove(base_track)
        _restore_binding(animation_data, base_action, base_slot)
        if correction_action is not None and correction_action.users == 0:
            bpy.data.actions.remove(correction_action)
        if archive_action is not None:
            archive_action.use_fake_user = False
            if archive_action.users == 0:
                bpy.data.actions.remove(archive_action)
        _restore_protection_state(base_protection_state)
        for owner, key, existed, value in reversed(metadata_state):
            if existed:
                owner[key] = value
            elif key in owner:
                del owner[key]
        raise


def _materialize_layer_metadata(
    armature: bpy.types.Object,
    stack: CorrectionLayerStack,
) -> None:
    """Upgrade a persisted single-layer v1 stack without touching its curves."""

    for index, layer in enumerate(stack.correction_layers):
        if not isinstance(layer.action.get(PROP_LAYER_ID), str):
            layer.action[PROP_LAYER_ID] = layer.layer_id
        if not isinstance(layer.action.get(PROP_LAYER_NAME), str):
            layer.action[PROP_LAYER_NAME] = layer.name
        if PROP_LAYER_MUTED not in layer.action:
            layer.action[PROP_LAYER_MUTED] = layer.muted
            layer.track.mute = layer.muted
            layer.strip.mute = False
        if PROP_LAYER_SOLO not in layer.action:
            layer.action[PROP_LAYER_SOLO] = False
        if index > 0 and layer.track.name == CORRECTION_TRACK_NAME:
            layer.track.name = _correction_track_name(layer.name, first=False)
    armature[PROP_ACTIVE_LAYER_ID] = stack.active_layer_id
    armature[PROP_CORRECTION_ACTION] = stack.correction_action.name


def _validated_stack(armature: bpy.types.Object) -> CorrectionLayerStack:
    # CRUD must remain proportional to layer count, not dense Base/Archive key
    # count. Full checksums and quaternion scans are reserved for explicit
    # validation and the bake/export boundary.
    validation = validate_layer_stack(armature, check_content=False)
    validation.raise_for_errors()
    assert validation.stack is not None
    stack = require_unshared_correction_actions(armature, validation.stack)
    _materialize_layer_metadata(armature, stack)
    resolved = _resolve_stack(armature)
    if resolved is None:
        raise ValueError("Correction stack cannot be resolved after metadata upgrade")
    return resolved


def list_correction_layers(armature: bpy.types.Object) -> tuple[CorrectionLayer, ...]:
    """Return all Correction layers from bottom to top NLA evaluation order."""

    return _validated_stack(armature).correction_layers


def _normalized_layer_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError("Correction layer name must be a string")
    value = " ".join(name.split())
    if not value:
        raise ValueError("Correction layer name must not be empty")
    return value


def _unique_layer_name(stack: CorrectionLayerStack, requested: str) -> str:
    requested = _normalized_layer_name(requested)
    existing = {layer.name.casefold() for layer in stack.correction_layers}
    if requested.casefold() not in existing:
        return requested
    suffix = 2
    while f"{requested} {suffix}".casefold() in existing:
        suffix += 1
    return f"{requested} {suffix}"


def _layer_for_id(stack: CorrectionLayerStack, layer_id: str) -> CorrectionLayer:
    for layer in stack.correction_layers:
        if layer.layer_id == layer_id:
            return layer
    raise ValueError(f"Unknown Correction layer id: {layer_id}")


def _set_active_values(
    armature: bpy.types.Object,
    stack: CorrectionLayerStack,
    layer: CorrectionLayer,
) -> None:
    armature[PROP_ACTIVE_LAYER_ID] = layer.layer_id
    armature[PROP_CORRECTION_ACTION] = layer.action.name
    for candidate in stack.correction_layers:
        candidate.track.select = candidate.layer_id == layer.layer_id


def set_active_correction_layer(
    armature: bpy.types.Object,
    layer_id: str,
) -> CorrectionLayer:
    stack = _validated_stack(armature)
    layer = _layer_for_id(stack, layer_id)
    previous_id = armature.get(PROP_ACTIVE_LAYER_ID)
    previous_action = armature.get(PROP_CORRECTION_ACTION)
    previous_selection = tuple(
        candidate.track.select for candidate in stack.correction_layers
    )
    try:
        _set_active_values(armature, stack, layer)
        validate_layer_stack(armature, check_content=False).raise_for_errors()
    except Exception:
        armature[PROP_ACTIVE_LAYER_ID] = previous_id
        armature[PROP_CORRECTION_ACTION] = previous_action
        for candidate, selected in zip(
            stack.correction_layers,
            previous_selection,
        ):
            candidate.track.select = selected
        raise
    resolved = _resolve_stack(armature)
    assert resolved is not None
    return resolved.active_layer


def _append_layer_action(
    armature: bpy.types.Object,
    stack: CorrectionLayerStack,
    action: bpy.types.Action,
    slot: object | None,
    name: str,
) -> CorrectionLayer:
    animation_data = armature.animation_data
    assert animation_data is not None
    stack_id = str(armature[PROP_STACK_ID])
    layer_id = str(uuid.uuid4())
    previous_id = armature.get(PROP_ACTIVE_LAYER_ID)
    previous_action = armature.get(PROP_CORRECTION_ACTION)
    previous_track_selection = tuple(
        (layer.track, bool(layer.track.select)) for layer in stack.correction_layers
    )
    track: bpy.types.NlaTrack | None = None
    try:
        action.name = f"MB_CORRECTION::{stack.base_action.name}::{name}"
        action.use_fake_user = False
        action[PROP_SCHEMA] = LAYER_SCHEMA_VERSION
        action[PROP_STACK_ID] = stack_id
        action[PROP_ROLE] = ROLE_CORRECTION
        action[PROP_BASE_ACTION] = stack.base_action.name
        action[PROP_BASE_CHECKSUM] = stack.base_action[PROP_BASE_CHECKSUM]
        action[PROP_LAYER_ID] = layer_id
        action[PROP_LAYER_NAME] = name
        action[PROP_LAYER_MUTED] = False
        action[PROP_LAYER_SOLO] = False

        track = animation_data.nla_tracks.new()
        track.name = _correction_track_name(name, first=False)
        strip = _strip_for_action(
            track,
            name,
            action,
            slot,
            float(stack.base_action.frame_range[0]),
        )
        _configure_strip(strip, blend_type="COMBINE", extrapolation="NOTHING")
        track.lock = False
        track.mute = _corrections_bypassed(armature) or any(
            layer.solo for layer in stack.correction_layers
        )
        track.is_solo = False

        pending = _resolve_stack(armature)
        if pending is None:
            raise ValueError("New Correction layer could not be resolved")
        _set_active_values(armature, pending, _layer_for_id(pending, layer_id))
        validate_layer_stack(armature, check_content=False).raise_for_errors()
    except Exception:
        if track is not None:
            animation_data.nla_tracks.remove(track)
        armature[PROP_ACTIVE_LAYER_ID] = previous_id
        armature[PROP_CORRECTION_ACTION] = previous_action
        for previous_track, selected in previous_track_selection:
            previous_track.select = selected
        action.use_fake_user = False
        if action.users == 0:
            bpy.data.actions.remove(action)
        raise
    resolved = _resolve_stack(armature)
    assert resolved is not None
    return _layer_for_id(resolved, layer_id)


def add_correction_layer(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    *,
    name: str | None = None,
) -> CorrectionLayer:
    """Append an empty sparse COMBINE layer and make it active."""

    del context  # Kept in the API for operator symmetry and future undo integration.
    stack = _validated_stack(armature)
    layer_name = _unique_layer_name(
        stack,
        name or f"Correction {len(stack.correction_layers) + 1}",
    )
    frame_start, frame_end = (float(value) for value in stack.base_action.frame_range)
    access = create_action_for_id(
        f"MB_CORRECTION::{stack.base_action.name}::{layer_name}",
        armature,
        frame_start=frame_start,
        frame_end=frame_end,
    )
    return _append_layer_action(
        armature,
        stack,
        access.action,
        access.slot,
        layer_name,
    )


def duplicate_correction_layer(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    layer_id: str,
    *,
    name: str | None = None,
) -> CorrectionLayer:
    """Duplicate one layer's sparse Action into a new stable layer."""

    del context
    stack = _validated_stack(armature)
    source = _layer_for_id(stack, layer_id)
    layer_name = _unique_layer_name(stack, name or f"{source.name} Copy")
    duplicate = source.action.copy()
    duplicate_slot = action_slot_for_id(duplicate, armature)
    if getattr(duplicate, "slots", None) and duplicate_slot is None:
        duplicate.use_fake_user = False
        if duplicate.users == 0:
            bpy.data.actions.remove(duplicate)
        raise ValueError("Duplicated Correction Action has no unambiguous Action Slot")
    return _append_layer_action(
        armature,
        stack,
        duplicate,
        duplicate_slot,
        layer_name,
    )


def rename_correction_layer(
    armature: bpy.types.Object,
    layer_id: str,
    name: str,
) -> CorrectionLayer:
    stack = _validated_stack(armature)
    layer = _layer_for_id(stack, layer_id)
    value = _normalized_layer_name(name)
    if any(
        other.layer_id != layer_id and other.name.casefold() == value.casefold()
        for other in stack.correction_layers
    ):
        raise ValueError(f"Correction layer name already exists: {value}")
    index = stack.correction_layers.index(layer)
    previous = (
        layer.name,
        layer.action.name,
        layer.action.get(PROP_LAYER_NAME),
        layer.track.name,
        layer.strip.name,
        armature.get(PROP_CORRECTION_ACTION),
    )
    try:
        layer.action[PROP_LAYER_NAME] = value
        layer.action.name = f"MB_CORRECTION::{stack.base_action.name}::{value}"
        layer.track.name = _correction_track_name(value, first=index == 0)
        layer.strip.name = value
        if stack.active_layer_id == layer_id:
            armature[PROP_CORRECTION_ACTION] = layer.action.name
        validate_layer_stack(armature, check_content=False).raise_for_errors()
    except Exception:
        old_name, old_action_name, old_property, old_track, old_strip, old_active = (
            previous
        )
        layer.action.name = old_action_name
        layer.action[PROP_LAYER_NAME] = old_property or old_name
        layer.track.name = old_track
        layer.strip.name = old_strip
        armature[PROP_CORRECTION_ACTION] = old_active
        raise
    resolved = _resolve_stack(armature)
    assert resolved is not None
    return _layer_for_id(resolved, layer_id)


@dataclass(frozen=True, slots=True)
class _LayerBindingState:
    action: bpy.types.Action
    name: str
    track_mute: bool
    track_select: bool
    strip_mute: bool
    influence: float
    strip_select: bool
    strip_use_sync_length: bool
    strip_use_animated_time_cyclic: bool
    strip_time: float


def _binding_state(layer: CorrectionLayer) -> _LayerBindingState:
    return _LayerBindingState(
        layer.action,
        layer.name,
        bool(layer.track.mute),
        bool(layer.track.select),
        bool(layer.strip.mute),
        float(layer.strip.influence),
        bool(layer.strip.select),
        bool(getattr(layer.strip, "use_sync_length", False)),
        bool(getattr(layer.strip, "use_animated_time_cyclic", False)),
        float(getattr(layer.strip, "strip_time", 0.0)),
    )


def _apply_binding_state(
    armature: bpy.types.Object,
    track: bpy.types.NlaTrack,
    state: _LayerBindingState,
    *,
    first: bool,
) -> None:
    strip = track.strips[0]
    slot = action_slot_for_id(state.action, armature)
    if getattr(state.action, "slots", None) and slot is None:
        raise ValueError("Correction Action has no unambiguous Action Slot")
    assign_nla_strip_action(strip, state.action, slot)
    track.name = _correction_track_name(state.name, first=first)
    track.lock = False
    track.mute = state.track_mute
    track.is_solo = False
    track.select = state.track_select
    strip.name = state.name
    strip.mute = state.strip_mute
    strip.influence = state.influence
    strip.select = state.strip_select
    if hasattr(strip, "use_sync_length"):
        strip.use_sync_length = state.strip_use_sync_length
    if hasattr(strip, "use_animated_time_cyclic"):
        strip.use_animated_time_cyclic = state.strip_use_animated_time_cyclic
    if hasattr(strip, "strip_time"):
        strip.strip_time = state.strip_time


def _swap_adjacent_layers(
    armature: bpy.types.Object,
    stack: CorrectionLayerStack,
    first_index: int,
) -> None:
    first = stack.correction_layers[first_index]
    second = stack.correction_layers[first_index + 1]
    first_state = _binding_state(first)
    second_state = _binding_state(second)
    try:
        _apply_binding_state(
            armature,
            first.track,
            second_state,
            first=first_index == 0,
        )
        _apply_binding_state(
            armature,
            second.track,
            first_state,
            first=False,
        )
    except Exception:
        _apply_binding_state(
            armature,
            first.track,
            first_state,
            first=first_index == 0,
        )
        _apply_binding_state(
            armature,
            second.track,
            second_state,
            first=False,
        )
        raise


def move_correction_layer(
    armature: bpy.types.Object,
    layer_id: str,
    offset: int,
) -> CorrectionLayer:
    """Move one layer by one NLA slot; +1 is visually/evaluatively upward."""

    if isinstance(offset, bool) or offset not in {-1, 1}:
        raise ValueError("Correction layer move offset must be -1 or +1")
    stack = _validated_stack(armature)
    layer = _layer_for_id(stack, layer_id)
    source_index = stack.correction_layers.index(layer)
    target_index = source_index + offset
    if target_index < 0 or target_index >= len(stack.correction_layers):
        return layer
    first_index = min(source_index, target_index)
    _swap_adjacent_layers(armature, stack, first_index)
    try:
        validate_layer_stack(armature, check_content=False).raise_for_errors()
    except Exception:
        rollback = _resolve_stack(armature)
        if rollback is not None:
            _swap_adjacent_layers(armature, rollback, first_index)
        raise
    resolved = _resolve_stack(armature)
    assert resolved is not None
    return _layer_for_id(resolved, layer_id)


def move_correction_layer_up(
    armature: bpy.types.Object,
    layer_id: str,
) -> CorrectionLayer:
    return move_correction_layer(armature, layer_id, 1)


def move_correction_layer_down(
    armature: bpy.types.Object,
    layer_id: str,
) -> CorrectionLayer:
    return move_correction_layer(armature, layer_id, -1)


def set_correction_layer_muted(
    armature: bpy.types.Object,
    layer_id: str,
    muted: bool,
) -> CorrectionLayer:
    stack = _validated_stack(armature)
    layer = _layer_for_id(stack, layer_id)
    previous = (
        layer.action.get(PROP_LAYER_MUTED),
        bool(layer.track.mute),
        bool(layer.strip.mute),
    )
    try:
        layer.action[PROP_LAYER_MUTED] = bool(muted)
        if _corrections_bypassed(armature):
            layer.track.mute = True
            layer.strip.mute = False
        elif not any(candidate.solo for candidate in stack.correction_layers):
            layer.track.mute = bool(muted)
            layer.strip.mute = False
        validate_layer_stack(armature, check_content=False).raise_for_errors()
    except Exception:
        old_property, old_track, old_strip = previous
        layer.action[PROP_LAYER_MUTED] = bool(old_property)
        layer.track.mute = old_track
        layer.strip.mute = old_strip
        raise
    resolved = _resolve_stack(armature)
    assert resolved is not None
    return _layer_for_id(resolved, layer_id)


def set_correction_layer_solo(
    armature: bpy.types.Object,
    layer_id: str | None,
) -> CorrectionLayer | None:
    """Solo one Correction while keeping Base evaluated; ``None`` clears solo."""

    stack = _validated_stack(armature)
    target = _layer_for_id(stack, layer_id) if layer_id is not None else None
    had_solo = any(layer.solo for layer in stack.correction_layers)
    previous = tuple(
        (
            layer,
            layer.action.get(PROP_LAYER_MUTED),
            layer.action.get(PROP_LAYER_SOLO),
            bool(layer.track.mute),
            bool(layer.strip.mute),
        )
        for layer in stack.correction_layers
    )
    try:
        for layer in stack.correction_layers:
            if not had_solo and not _corrections_bypassed(armature):
                layer.action[PROP_LAYER_MUTED] = layer.muted
            layer.action[PROP_LAYER_SOLO] = target is not None and layer == target
            if _corrections_bypassed(armature):
                layer.track.mute = True
            elif target is None:
                layer.track.mute = bool(layer.action.get(PROP_LAYER_MUTED, False))
            else:
                layer.track.mute = layer != target
            layer.strip.mute = False
            layer.track.is_solo = False
        validate_layer_stack(armature, check_content=False).raise_for_errors()
    except Exception:
        for layer, old_muted, old_solo, track_mute, strip_mute in previous:
            layer.action[PROP_LAYER_MUTED] = bool(old_muted)
            layer.action[PROP_LAYER_SOLO] = bool(old_solo)
            layer.track.mute = track_mute
            layer.strip.mute = strip_mute
        raise
    if target is None:
        return None
    resolved = _resolve_stack(armature)
    assert resolved is not None
    return _layer_for_id(resolved, target.layer_id)


def remove_correction_layer(
    armature: bpy.types.Object,
    layer_id: str,
) -> CorrectionLayer:
    """Remove one Correction and return the active surviving layer."""

    stack = _validated_stack(armature)
    if len(stack.correction_layers) <= 1:
        raise ValueError("Correction stack must retain at least one layer")
    layer = _layer_for_id(stack, layer_id)
    index = stack.correction_layers.index(layer)
    survivors = [
        candidate for candidate in stack.correction_layers if candidate != layer
    ]
    next_active = (
        survivors[min(index, len(survivors) - 1)]
        if stack.active_layer_id == layer_id
        else _layer_for_id(stack, stack.active_layer_id)
    )
    animation_data = armature.animation_data
    assert animation_data is not None
    original_states = tuple(
        _binding_state(candidate) for candidate in stack.correction_layers
    )
    original_layer_properties = tuple(
        (
            candidate.action,
            candidate.action.get(PROP_LAYER_MUTED),
            candidate.action.get(PROP_LAYER_SOLO),
        )
        for candidate in stack.correction_layers
    )
    original_active_id = armature.get(PROP_ACTIVE_LAYER_ID)
    original_active_name = armature.get(PROP_CORRECTION_ACTION)
    original_action = animation_data.action
    original_action_slot = (
        action_slot_for_id(original_action, armature)
        if original_action is not None
        else None
    )
    removed_track = False
    try:
        if animation_data.action is layer.action:
            animation_data.action = None
        animation_data.nla_tracks.remove(layer.track)
        removed_track = True
        pending = _resolve_stack(armature)
        if pending is None:
            raise RuntimeError("Correction stack could not resolve after layer removal")
        if layer.solo:
            for survivor in pending.correction_layers:
                survivor.action[PROP_LAYER_SOLO] = False
                survivor.track.mute = _corrections_bypassed(armature) or bool(
                    survivor.action.get(PROP_LAYER_MUTED, False)
                )
                survivor.strip.mute = False
        _set_active_values(
            armature,
            pending,
            _layer_for_id(pending, next_active.layer_id),
        )
        validate_layer_stack(armature, check_content=False).raise_for_errors()
    except Exception:
        if removed_track:
            restored_track = animation_data.nla_tracks.new()
            restored_strip = _strip_for_action(
                restored_track,
                layer.name,
                layer.action,
                action_slot_for_id(layer.action, armature),
                float(stack.base_action.frame_range[0]),
            )
            _configure_strip(
                restored_strip,
                blend_type="COMBINE",
                extrapolation="NOTHING",
            )
            correction_tracks = _correction_tracks(
                animation_data,
                str(armature[PROP_STACK_ID]),
            )
            if len(correction_tracks) == len(original_states):
                for binding_index, (track, state) in enumerate(
                    zip(correction_tracks, original_states)
                ):
                    _apply_binding_state(
                        armature,
                        track,
                        state,
                        first=binding_index == 0,
                    )
        for action, muted_value, solo_value in original_layer_properties:
            action[PROP_LAYER_MUTED] = bool(muted_value)
            action[PROP_LAYER_SOLO] = bool(solo_value)
        armature[PROP_ACTIVE_LAYER_ID] = original_active_id
        armature[PROP_CORRECTION_ACTION] = original_active_name
        _restore_binding(animation_data, original_action, original_action_slot)
        raise

    resolved = _resolve_stack(armature)
    if resolved is None:
        raise RuntimeError("Validated Correction stack could not be resolved")
    action = layer.action
    action.use_fake_user = False
    if action.users == 0:
        bpy.data.actions.remove(action)
    else:
        for key in (
            PROP_SCHEMA,
            PROP_STACK_ID,
            PROP_ROLE,
            PROP_BASE_ACTION,
            PROP_BASE_CHECKSUM,
            PROP_LAYER_ID,
            PROP_LAYER_NAME,
            PROP_LAYER_MUTED,
            PROP_LAYER_SOLO,
        ):
            if key in action:
                del action[key]
    return resolved.active_layer


def _iter_scoped_action_fcurves(
    action: bpy.types.Action,
    slot: object | None,
) -> Iterator[bpy.types.FCurve]:
    """Keep the legacy one-argument iterator boundary when no Slot is given."""

    if slot is None:
        return iter_action_fcurves(action)
    return iter_action_fcurves(action, slot)


def _quaternion_curve_errors(
    action: bpy.types.Action,
    slot: object | None = None,
) -> list[str]:
    errors: list[str] = []
    by_path: dict[str, dict[int, bpy.types.FCurve]] = {}
    for curve in _iter_scoped_action_fcurves(action, slot):
        if curve.data_path == "rotation_euler" or curve.data_path.endswith(
            ".rotation_euler"
        ):
            errors.append(f"Correction uses Euler rotation: {curve.data_path}")
        if curve.data_path == "rotation_quaternion" or curve.data_path.endswith(
            ".rotation_quaternion"
        ):
            curves = by_path.setdefault(curve.data_path, {})
            index = int(curve.array_index)
            if index in curves:
                errors.append(
                    f"Quaternion channel {index} is duplicated: {curve.data_path}"
                )
            curves[index] = curve
    for data_path, curves in by_path.items():
        if set(curves) != {0, 1, 2, 3}:
            errors.append(f"Quaternion correction must key WXYZ together: {data_path}")
            continue
        values_by_channel: dict[int, dict[float, float]] = {}
        frames: set[float] = set()
        for index, curve in curves.items():
            values_by_frame: dict[float, float] = {}
            for point in curve.keyframe_points:
                raw_frame = float(point.co[0])
                value = float(point.co[1])
                if not math.isfinite(raw_frame):
                    errors.append(
                        f"Quaternion channel {index} has a non-finite frame: "
                        f"{data_path}"
                    )
                    continue
                frame = round(raw_frame, 6)
                frames.add(frame)
                if frame in values_by_frame:
                    errors.append(
                        "Quaternion channel "
                        f"{index} has multiple keys at rounded frame {frame}: "
                        f"{data_path}"
                    )
                elif not math.isfinite(value):
                    errors.append(
                        f"Quaternion channel {index} is non-finite at frame "
                        f"{frame}: {data_path}"
                    )
                values_by_frame[frame] = value
            values_by_channel[index] = values_by_frame
        previous_frame: float | None = None
        previous_quaternion: tuple[float, float, float, float] | None = None
        for frame in sorted(frames):
            values: list[float] = []
            for index in sorted(curves):
                channel_values = values_by_channel[index]
                if frame not in channel_values:
                    errors.append(
                        f"Quaternion channel {index} is missing frame {frame}: {data_path}"
                    )
                else:
                    values.append(channel_values[frame])
            if len(values) == 4:
                norm_squared = sum(value * value for value in values)
                if not math.isfinite(norm_squared) or abs(norm_squared - 1.0) > 2e-3:
                    errors.append(
                        f"Quaternion correction is not normalized at frame {frame}: {data_path}"
                    )
                    continue
                norm = math.sqrt(norm_squared)
                quaternion = tuple(value / norm for value in values)
                if (
                    previous_quaternion is not None
                    and sum(
                        left * right
                        for left, right in zip(
                            previous_quaternion,
                            quaternion,
                            strict=True,
                        )
                    )
                    < -1.0e-8
                ):
                    errors.append(
                        "Quaternion correction changes hemisphere between frames "
                        f"{previous_frame} and {frame}: {data_path}"
                    )
                previous_frame = frame
                previous_quaternion = quaternion
    return errors


def _location_curve_errors(
    action: bpy.types.Action,
    slot: object | None = None,
) -> list[str]:
    """Validate complete Object Root and Hips Correction translation keys."""

    errors: list[str] = []
    by_path: dict[str, dict[int, bpy.types.FCurve]] = {}
    for curve in _iter_scoped_action_fcurves(action, slot):
        if curve.data_path not in {"location", "delta_location"} and not (
            curve.data_path.endswith(".location")
            or curve.data_path.endswith(".delta_location")
        ):
            continue
        if curve.data_path not in {
            _ROOT_LOCATION_DATA_PATH,
            _HIPS_LOCATION_DATA_PATH,
        }:
            errors.append(
                "Correction location is limited to the Hips pose bone or Object Root: "
                f"{curve.data_path}"
            )
        curves = by_path.setdefault(curve.data_path, {})
        index = int(curve.array_index)
        if index in curves:
            errors.append(
                f"Location channel {index} is duplicated: {curve.data_path}"
            )
        curves[index] = curve

    for data_path, curves in by_path.items():
        if set(curves) != {0, 1, 2}:
            errors.append(f"Location correction must key XYZ together: {data_path}")
            continue
        values_by_channel: dict[int, dict[float, float]] = {}
        frames: set[float] = set()
        for index, curve in curves.items():
            values_by_frame: dict[float, float] = {}
            for point in curve.keyframe_points:
                raw_frame = float(point.co[0])
                value = float(point.co[1])
                if not math.isfinite(raw_frame):
                    errors.append(
                        f"Location channel {index} has a non-finite frame: "
                        f"{data_path}"
                    )
                    continue
                frame = round(raw_frame, 6)
                frames.add(frame)
                if frame in values_by_frame:
                    errors.append(
                        f"Location channel {index} has multiple keys at frame "
                        f"{frame}: {data_path}"
                    )
                elif not math.isfinite(value):
                    errors.append(
                        f"Location channel {index} is non-finite at frame "
                        f"{frame}: {data_path}"
                    )
                values_by_frame[frame] = value
            values_by_channel[index] = values_by_frame
        for frame in sorted(frames):
            for index in range(3):
                if frame not in values_by_channel[index]:
                    errors.append(
                        f"Location channel {index} is missing frame "
                        f"{frame}: {data_path}"
                    )
    return errors


def _correction_curve_errors(
    action: bpy.types.Action,
    armature: bpy.types.Object,
    slot: object | None = None,
) -> list[str]:
    """Return all transform-channel violations for a Correction Action."""

    errors = _quaternion_curve_errors(action, slot)
    errors.extend(_location_curve_errors(action, slot))
    muted_groups: set[str] = set()
    pose_rotation_paths = {
        f'pose.bones["{bpy.utils.escape_identifier(bone.name)}"].rotation_quaternion'
        for bone in armature.data.bones
    }
    for curve in _iter_scoped_action_fcurves(action, slot):
        data_path = curve.data_path
        if curve.mute:
            errors.append(
                f"Correction transform curve must not be muted: "
                f"{data_path}[{curve.array_index}]"
            )
        group = curve.group
        if group is not None and group.mute and group.name not in muted_groups:
            errors.append(f"Correction transform group must not be muted: {group.name}")
            muted_groups.add(group.name)
        if curve.modifiers:
            errors.append(
                f"Correction transform modifiers are not supported: "
                f"{data_path}[{curve.array_index}]"
            )
        if curve.sampled_points:
            errors.append(
                f"Sampled Correction transform curves are not supported: "
                f"{data_path}[{curve.array_index}]"
            )
        if data_path in pose_rotation_paths:
            continue
        if data_path == _ROOT_ROTATION_DATA_PATH:
            continue
        if data_path.endswith(".rotation_quaternion"):
            errors.append(
                "Correction rotation must target an existing pose bone: "
                f"{data_path}"
            )
            continue
        if data_path in {_ROOT_LOCATION_DATA_PATH, _HIPS_LOCATION_DATA_PATH}:
            continue
        if data_path == "scale" or data_path.endswith(".scale"):
            errors.append(f"Correction scale keys are not supported: {data_path}")
            continue
        if (
            data_path == "rotation_euler"
            or data_path.endswith(".rotation_euler")
            or data_path in {"location", "delta_location"}
            or data_path.endswith(".location")
            or data_path.endswith(".delta_location")
        ):
            # A more specific error was emitted above.
            continue
        errors.append(f"Unsupported Correction channel: {data_path}")
    return errors


def _rna_components_match(
    value: object,
    expected: tuple[float, ...],
) -> bool:
    try:
        components = tuple(float(component) for component in value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return False
    return len(components) == len(expected) and all(
        math.isfinite(component) and abs(component - target) <= 1.0e-6
        for component, target in zip(components, expected, strict=True)
    )


def _curve_is_constant_value(
    curve: bpy.types.FCurve,
    expected: float,
) -> bool:
    """Return whether an FCurve is provably constant at one neutral value."""

    if curve.mute:
        return True
    if curve.modifiers:
        return False
    points = tuple(curve.keyframe_points)
    samples = tuple(curve.sampled_points)
    if not points and not samples:
        return False
    for point in points:
        if (
            not math.isfinite(float(point.co[1]))
            or abs(float(point.co[1]) - expected) > 1.0e-6
        ):
            return False
        if point.interpolation == "BEZIER" and (
            not math.isfinite(float(point.handle_left[1]))
            or not math.isfinite(float(point.handle_right[1]))
            or abs(float(point.handle_left[1]) - expected) > 1.0e-6
            or abs(float(point.handle_right[1]) - expected) > 1.0e-6
        ):
            return False
    return all(
        math.isfinite(float(point.co[1]))
        and abs(float(point.co[1]) - expected) <= 1.0e-6
        for point in samples
    )


def _root_paths_in_stack(stack: CorrectionLayerStack) -> set[str]:
    paths: set[str] = set()
    for layer in stack.correction_layers:
        slot = action_slot_for_strip(layer.strip)
        paths.update(
            curve.data_path
            for curve in _iter_scoped_action_fcurves(layer.action, slot)
            if curve.data_path in {_ROOT_LOCATION_DATA_PATH, _ROOT_ROTATION_DATA_PATH}
        )
    return paths


def root_trajectory_safety_errors(
    armature: bpy.types.Object,
    stack: CorrectionLayerStack,
    *,
    requested_paths: set[str] | frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Validate the conservative v1 transform model used by Root Corrections.

    The invariant intentionally applies to location-only and rotation-only
    writes too.  Once a Correction contains an Object Root channel, later
    edits, bake, and export must keep evaluating the same unparented
    quaternion transform model.
    """

    paths = (
        _root_paths_in_stack(stack)
        if requested_paths is None
        else set(requested_paths)
    )
    if not paths:
        return ()
    unknown_paths = paths - {_ROOT_LOCATION_DATA_PATH, _ROOT_ROTATION_DATA_PATH}
    if unknown_paths:
        return ("Unsupported Root trajectory path: " + ", ".join(sorted(unknown_paths)),)

    errors: list[str] = []
    if armature.parent is not None:
        errors.append("Root trajectory requires an unparented Armature")
    if armature.rotation_mode != "QUATERNION":
        errors.append("Root trajectory requires Armature Object Quaternion rotation mode")
    if len(armature.constraints) > 0:
        errors.append("Root trajectory does not support Armature Object constraints")
    if not _rna_components_match(armature.scale, (1.0, 1.0, 1.0)):
        errors.append("Root trajectory requires unit Armature Object scale")
    if not _rna_components_match(armature.delta_location, (0.0, 0.0, 0.0)):
        errors.append("Root trajectory requires zero delta location")
    if not _rna_components_match(armature.delta_rotation_euler, (0.0, 0.0, 0.0)):
        errors.append("Root trajectory requires zero delta Euler rotation")
    if not _rna_components_match(
        armature.delta_rotation_quaternion,
        (1.0, 0.0, 0.0, 0.0),
    ) and not _rna_components_match(
        armature.delta_rotation_quaternion,
        (-1.0, 0.0, 0.0, 0.0),
    ):
        errors.append("Root trajectory requires identity delta rotation")
    if not _rna_components_match(armature.delta_scale, (1.0, 1.0, 1.0)):
        errors.append("Root trajectory requires unit delta scale")

    animation_data = armature.animation_data
    if animation_data is not None:
        driven_paths = sorted(
            {
                driver.data_path
                for driver in animation_data.drivers
                if driver.data_path in _ROOT_TRANSFORM_DRIVER_PATHS
            }
        )
        if driven_paths:
            errors.append(
                "Root trajectory does not support driven Object transforms: "
                + ", ".join(driven_paths)
            )

    neutral_paths = {
        "scale": (1.0, 1.0, 1.0),
        "delta_location": (0.0, 0.0, 0.0),
        "delta_rotation_euler": (0.0, 0.0, 0.0),
        "delta_rotation_quaternion": (1.0, 0.0, 0.0, 0.0),
        "delta_scale": (1.0, 1.0, 1.0),
    }
    base_slot = action_slot_for_strip(stack.base_strip)
    for curve in _iter_scoped_action_fcurves(stack.base_action, base_slot):
        data_path = curve.data_path
        if data_path in {"rotation_euler", "rotation_axis_angle"}:
            errors.append(
                "Root trajectory does not support Base Object rotation channel: "
                + data_path
            )
            continue
        neutral = neutral_paths.get(data_path)
        if neutral is None:
            continue
        index = int(curve.array_index)
        if index < 0 or index >= len(neutral) or not _curve_is_constant_value(
            curve,
            neutral[index],
        ):
            errors.append(
                "Root trajectory Base Object channel must stay neutral: "
                f"{data_path}[{index}]"
            )
    return tuple(errors)


def _unprotected_curves(action: bpy.types.Action) -> list[str]:
    return [
        f"{curve.data_path}[{curve.array_index}]"
        for curve in iter_action_fcurves(action)
        if not curve.lock or (hasattr(curve, "hide") and not curve.hide)
    ]


def validate_layer_stack(
    armature: bpy.types.Object,
    *,
    check_content: bool = True,
) -> LayerStackValidation:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        _canonical_armature(armature)
    except ValueError as error:
        return LayerStackValidation((str(error),))

    stack_id = armature.get(PROP_STACK_ID)
    if not isinstance(stack_id, str) or not stack_id:
        return LayerStackValidation(("Armature has no Motion Bridge stack id",))
    if armature.get(PROP_SCHEMA) != LAYER_SCHEMA_VERSION:
        errors.append("Unsupported layer stack schema")

    animation_data = armature.animation_data
    if animation_data is None:
        return LayerStackValidation(("Armature has no animation data",))
    correction_tracks = _correction_tracks(animation_data, stack_id)
    foreign_tracks = _foreign_nla_tracks(animation_data, stack_id)
    if foreign_tracks:
        errors.append(
            "Unsupported NLA coexistence: Motion Bridge found "
            f"{len(foreign_tracks)} unrelated NLA track(s); "
            "the unrelated tracks were not modified"
        )
    expected_track_count = 1 + len(correction_tracks)
    managed_track_count = len(animation_data.nla_tracks) - len(foreign_tracks)
    if managed_track_count != expected_track_count:
        errors.append(
            "Motion Bridge stack contains malformed owned NLA tracks: "
            f"expected {expected_track_count}, found {managed_track_count}"
        )
    base_count = sum(
        track.name == BASE_TRACK_NAME for track in animation_data.nla_tracks
    )
    if base_count != 1:
        errors.append(f"Expected one {BASE_TRACK_NAME} track, found {base_count}")
    if not correction_tracks:
        errors.append("Correction stack must retain at least one Correction layer")
    archives = _archive_for_stack(stack_id)
    if len(archives) != 1:
        errors.append(f"Expected one Archive Action, found {len(archives)}")

    stack = _resolve_stack(armature)
    if stack is None:
        return LayerStackValidation(tuple(errors or ["Layer stack cannot be resolved"]))

    archive = stack.archive_action
    base = stack.base_action
    for action, role in (
        (archive, ROLE_ARCHIVE),
        (base, ROLE_BASE),
        *((layer.action, ROLE_CORRECTION) for layer in stack.correction_layers),
    ):
        if action.get(PROP_SCHEMA) != LAYER_SCHEMA_VERSION:
            errors.append(f"{role} Action has an unsupported schema")
        if action.get(PROP_STACK_ID) != stack_id:
            errors.append(f"{role} Action belongs to another stack")
        if action.get(PROP_ROLE) != role:
            errors.append(f"Action role mismatch: expected {role}")

    checksum = archive.get(PROP_ARCHIVE_CHECKSUM)
    if archive.get(PROP_CHECKSUM_ALGORITHM) != "sha256":
        errors.append("Archive checksum algorithm must be sha256")
    if not isinstance(checksum, str) or len(checksum) != 64:
        errors.append("Archive checksum is missing or malformed")
    elif check_content:
        try:
            if action_checksum(archive) != checksum:
                errors.append("Archive Action content no longer matches its checksum")
            if action_checksum(base) != checksum:
                errors.append("Base Action content no longer matches the Archive")
        except ValueError as error:
            errors.append(str(error))
    try:
        provenance = json.loads(str(archive.get(PROP_PROVENANCE, "")))
        if not isinstance(provenance, dict):
            errors.append("Archive provenance must be a JSON object")
    except json.JSONDecodeError:
        errors.append("Archive provenance is not valid JSON")

    if armature.get(PROP_ARCHIVE_ACTION) != archive.name:
        errors.append("Armature Canonical Archive reference is stale")
    if armature.get(PROP_BASE_ACTION) != base.name:
        errors.append("Armature Base reference is stale")
    if base.get(PROP_ARCHIVE_ACTION) != archive.name:
        errors.append("Base Canonical Archive reference is stale")
    for index, layer in enumerate(stack.correction_layers):
        if layer.action.get(PROP_BASE_ACTION) != base.name:
            errors.append(f"Correction layer {index + 1} Base reference is stale")
        if layer.action.get(PROP_BASE_CHECKSUM) != checksum:
            errors.append(f"Correction layer {index + 1} Base checksum is stale")
    recipe_values = (
        archive.get(PROP_RETARGET_RECIPE),
        base.get(PROP_RETARGET_RECIPE),
        armature.get(PROP_RETARGET_RECIPE),
    )
    if any(value is not None for value in recipe_values) and not (
        isinstance(recipe_values[0], str)
        and recipe_values[0]
        and recipe_values[0] == recipe_values[1] == recipe_values[2]
    ):
        errors.append("Canonical retarget recipe references are inconsistent")

    if stack.base_track.lock is not True:
        errors.append("Base track must remain locked")
    if stack.base_track.mute or stack.base_strip.mute:
        errors.append("Base track/strip must not be muted")
    if stack.base_strip.blend_type != "REPLACE":
        errors.append("Base strip must use REPLACE")
    if stack.base_strip.extrapolation != "HOLD":
        errors.append("Base strip must hold the Base pose")
    if animation_data.nla_tracks[0] != stack.base_track:
        errors.append("Base track must remain first in deterministic NLA order")

    layer_ids: set[str] = set()
    for index, layer in enumerate(stack.correction_layers):
        if layer.track.lock:
            errors.append(f"Correction layer {index + 1} must remain editable")
        if layer.strip.blend_type != "COMBINE":
            errors.append(
                f"Correction layer {index + 1} must use quaternion-safe COMBINE"
            )
        if layer.strip.extrapolation != "NOTHING":
            errors.append(
                f"Correction layer {index + 1} must not extrapolate outside the Base range"
            )
        if layer.layer_id in layer_ids:
            errors.append(f"Duplicate Correction layer id: {layer.layer_id}")
        layer_ids.add(layer.layer_id)
        stored_id = layer.action.get(PROP_LAYER_ID)
        if stored_id is not None and stored_id != layer.layer_id:
            errors.append(f"Correction layer {index + 1} id metadata is inconsistent")
        stored_name = layer.action.get(PROP_LAYER_NAME)
        if stored_name is not None and (
            not isinstance(stored_name, str) or not stored_name.strip()
        ):
            errors.append(f"Correction layer {index + 1} has an invalid name")

    strips = [("Base", stack.base_strip)] + [
        (f"Correction {index + 1}", layer.strip)
        for index, layer in enumerate(stack.correction_layers)
    ]
    base_action_range = tuple(float(value) for value in stack.base_action.frame_range)
    for label, strip in strips:
        influence = float(strip.influence)
        if label == "Base" and abs(influence - 1.0) > 1e-6:
            errors.append("Base strip influence must be 1")
        if label != "Base" and (
            not math.isfinite(influence) or influence < 0.0 or influence > 1.0
        ):
            errors.append(f"{label} strip influence must be finite and within [0, 1]")
        if strip.use_animated_influence or strip.use_animated_time:
            errors.append(f"{label} strip animated influence/time is not supported")
        action_range = tuple(float(value) for value in strip.action.frame_range)
        if label != "Base" and (
            not strip.action.use_frame_range
            or any(
                abs(value - base_value) > 1e-6
                for value, base_value in zip(action_range, base_action_range)
            )
        ):
            errors.append(f"{label} Action must use the full Base frame range")
        if (
            abs(float(strip.action_frame_start) - action_range[0]) > 1e-6
            or abs(float(strip.action_frame_end) - action_range[1]) > 1e-6
        ):
            errors.append(f"{label} strip is trimmed or retimed")
        if (
            abs(float(strip.frame_start) - action_range[0]) > 1e-6
            or abs(float(strip.frame_end) - action_range[1]) > 1e-6
        ):
            errors.append(f"{label} strip is shifted or retimed")
        if (
            abs(float(strip.scale) - 1.0) > 1e-6
            or abs(float(strip.repeat) - 1.0) > 1e-6
        ):
            errors.append(f"{label} strip scale/repeat must remain 1")
        if (
            strip.use_auto_blend
            or abs(float(strip.blend_in)) > 1e-6
            or abs(float(strip.blend_out)) > 1e-6
        ):
            errors.append(f"{label} strip blend ramps are not supported")
        if bool(getattr(strip, "use_reverse", False)):
            errors.append(f"{label} strip reverse playback is not supported")
        slot = action_slot_for_id(strip.action, armature)
        if not strip_slot_matches(strip, slot):
            errors.append(f"{label} strip Action Slot is not explicit")

    correction_actions = {layer.action for layer in stack.correction_layers}
    if (
        animation_data.action is not None
        and animation_data.action not in correction_actions
    ):
        warnings.append("An unrelated active Action is layered above Motion Bridge NLA")
    if any(
        strip.action is archive
        for track in animation_data.nla_tracks
        for strip in track.strips
    ):
        errors.append("Archive Action must never be evaluated by NLA")
    if check_content:
        for label, action in (("Archive", archive), ("Base", base)):
            unprotected = _unprotected_curves(action)
            if unprotected:
                errors.append(
                    f"{label} Action has editable curves: {', '.join(unprotected[:3])}"
                )
        for layer in stack.correction_layers:
            errors.extend(
                _correction_curve_errors(
                    layer.action,
                    armature,
                    action_slot_for_strip(layer.strip),
                )
            )
        errors.extend(root_trajectory_safety_errors(armature, stack))

    active_value = armature.get(PROP_ACTIVE_LAYER_ID)
    if active_value is not None and active_value not in layer_ids:
        errors.append("Active Correction layer id does not belong to this stack")
    solo_layers = [layer for layer in stack.correction_layers if layer.solo]
    if len(solo_layers) > 1:
        errors.append("At most one Correction layer may be solo")
    bypass_value = armature.get(PROP_CORRECTIONS_BYPASSED, False)
    if not isinstance(bypass_value, bool):
        errors.append("Correction preview bypass state must be a boolean")
    corrections_bypassed = bool(bypass_value)
    if stack.base_track.is_solo or any(
        layer.track.is_solo for layer in stack.correction_layers
    ):
        errors.append("Native NLA solo is unsafe because it excludes the Base track")
    if corrections_bypassed:
        if any(
            not layer.track.mute or layer.strip.mute
            for layer in stack.correction_layers
        ):
            errors.append("Before preview must mute every Correction track")
    elif solo_layers:
        solo_id = solo_layers[0].layer_id
        for layer in stack.correction_layers:
            if layer.track.mute != (layer.layer_id != solo_id) or layer.strip.mute:
                errors.append("Correction solo effective mute state is inconsistent")
                break
    else:
        for layer in stack.correction_layers:
            if PROP_LAYER_MUTED not in layer.action:
                # Legacy single-layer stacks stored mute only on their NLA
                # binding.  Metadata materialization normalizes that state.
                continue
            expected_mute = bool(layer.action.get(PROP_LAYER_MUTED, False))
            if layer.track.mute != expected_mute or layer.strip.mute:
                errors.append("Correction effective mute state is inconsistent")
                break

    return LayerStackValidation(tuple(errors), tuple(warnings), stack)
