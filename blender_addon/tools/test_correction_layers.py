from __future__ import annotations

import hashlib
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
    _channelbag_slot_identifier,
    action_slot_identifiers,
    insert_object_location_key,
    insert_object_quaternion_key,
    insert_quaternion_key,
    iter_action_curve_locations,
    iter_action_fcurves,
)
from motion_bridge.editing.pose_api import pose_bone_selected  # noqa: E402
from motion_bridge.editing.bake import (  # noqa: E402
    activate_action_for_export,
    bake_layer_stack,
)
from motion_bridge.editing.correction_layers import (  # noqa: E402
    PROP_ARCHIVE_CHECKSUM,
    PROP_PROVENANCE,
    action_is_unshared_for_armature,
    action_checksum,
    create_correction_layer,
    get_correction_layer_stack,
    set_correction_layer_muted,
    validate_layer_stack,
)
import motion_bridge.editing.correction_layers as correction_module  # noqa: E402


FIRST_FRAME = 1
LAST_FRAME = 12
BASE_ANGLE_DEGREES = 35.0
CORRECTION_ANGLE_DEGREES = 90.0
ROOT_ANGLE_DEGREES = 20.0
ROOT_LOCATION = (0.25, -0.5, 1.25)


def _materialized_v1_checksum(action: bpy.types.Action) -> str:
    curves: list[dict[str, object]] = []
    for location in iter_action_curve_locations(action):
        curve = correction_module._curve_payload(location.curve)
        curve["layerIndex"] = location.layer_index
        curve["stripIndex"] = location.strip_index
        curve["slotIdentifier"] = location.slot_identifier
        curves.append(curve)
    curves.sort(
        key=lambda item: (
            item["layerIndex"],
            item["stripIndex"],
            item["slotIdentifier"],
            item["dataPath"],
            item["arrayIndex"],
            item["group"],
        )
    )
    encoded = json.dumps(
        {
            "schema": 1,
            "frameRange": [
                float(action.frame_range[0]),
                float(action.frame_range[1]),
            ],
            "slots": sorted(action_slot_identifiers(action)),
            "curves": curves,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for action in tuple(bpy.data.actions):
        bpy.data.actions.remove(action)


def _create_base_action() -> tuple[bpy.types.Object, bpy.types.Action]:
    _clear_scene()
    armature_data = bpy.data.armatures.new("MB_Correction_Test_Armature")
    armature = bpy.data.objects.new("MB_Correction_Test", armature_data)
    bpy.context.scene.collection.objects.link(armature)
    armature["motion_bridge_skeleton_id"] = SKELETON_ID
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature

    bpy.ops.object.mode_set(mode="EDIT")
    bone = armature_data.edit_bones.new("Hips")
    bone.head = (0.0, 0.0, 0.0)
    bone.tail = (0.0, 0.0, 1.0)
    bpy.ops.object.mode_set(mode="POSE")

    pose_bone = armature.pose.bones["Hips"]
    pose_bone.rotation_mode = "QUATERNION"
    armature.rotation_mode = "QUATERNION"
    base = Quaternion((0.0, 0.0, 1.0), math.radians(BASE_ANGLE_DEGREES))
    root_rotation = Quaternion((0.0, 1.0, 0.0), math.radians(ROOT_ANGLE_DEGREES))
    for frame in (FIRST_FRAME, LAST_FRAME):
        bpy.context.scene.frame_set(frame)
        armature.location = ROOT_LOCATION
        armature.rotation_quaternion = root_rotation
        armature.keyframe_insert(data_path="location", frame=frame, group="Root")
        armature.keyframe_insert(
            data_path="rotation_quaternion",
            frame=frame,
            group="Root",
        )
        pose_bone.rotation_quaternion = base
        pose_bone.keyframe_insert(
            data_path="rotation_quaternion",
            frame=frame,
            group="Hips",
        )
    action = armature.animation_data.action if armature.animation_data else None
    if action is None:
        raise AssertionError("Failed to create synthetic Base Action")
    action.name = "MB_Correction_Base"
    return armature, action


def _evaluated_quaternion(armature: bpy.types.Object, frame: float) -> Quaternion:
    bpy.context.scene.frame_set(math.floor(frame), subframe=frame - math.floor(frame))
    bpy.context.view_layer.update()
    return armature.pose.bones["Hips"].matrix.to_quaternion().normalized()


def _evaluated_root(
    armature: bpy.types.Object,
    frame: float,
) -> tuple[tuple[float, float, float], Quaternion]:
    bpy.context.scene.frame_set(math.floor(frame), subframe=frame - math.floor(frame))
    bpy.context.view_layer.update()
    location, rotation, _scale = armature.matrix_world.decompose()
    return tuple(location), rotation.normalized()


def _angular_distance_degrees(first: Quaternion, second: Quaternion) -> float:
    return math.degrees(first.rotation_difference(second).angle)


def _root_error(
    expected: tuple[tuple[float, float, float], Quaternion],
    actual: tuple[tuple[float, float, float], Quaternion],
) -> tuple[float, float]:
    location_error = math.sqrt(
        sum(
            (expected_value - actual_value) ** 2
            for expected_value, actual_value in zip(expected[0], actual[0])
        )
    )
    return location_error, _angular_distance_degrees(expected[1], actual[1])


def _capture_editor_state() -> dict[str, object]:
    context = bpy.context
    active = context.view_layer.objects.active
    animation_data = active.animation_data if active is not None else None
    selected_bones = (
        tuple(sorted(bone.name for bone in active.pose.bones if pose_bone_selected(bone)))
        if active is not None and active.type == "ARMATURE"
        else ()
    )
    active_bone = (
        active.data.bones.active.name
        if active is not None
        and active.type == "ARMATURE"
        and active.data.bones.active is not None
        else None
    )
    return {
        "active": active,
        "selected": tuple(sorted(obj.name for obj in context.selected_objects)),
        "mode": context.object.mode if context.object is not None else None,
        "frameStart": context.scene.frame_start,
        "frameEnd": context.scene.frame_end,
        "frame": context.scene.frame_current,
        "subframe": context.scene.frame_subframe,
        "lastSlotIdentifier": (
            getattr(animation_data, "last_slot_identifier", None)
            if animation_data is not None
            else None
        ),
        "selectedBones": selected_bones,
        "activeBone": active_bone,
    }


def _assert_baked_quaternion_continuity(action: bpy.types.Action) -> None:
    curves = {
        curve.array_index: curve
        for curve in iter_action_fcurves(action)
        if curve.data_path == 'pose.bones["Hips"].rotation_quaternion'
    }
    if set(curves) != {0, 1, 2, 3}:
        raise AssertionError("Baked Hips quaternion does not contain WXYZ curves")
    previous: tuple[float, float, float, float] | None = None
    for point_index in range(len(curves[0].keyframe_points)):
        values = tuple(
            float(curves[index].keyframe_points[point_index].co[1])
            for index in range(4)
        )
        norm = math.sqrt(sum(value * value for value in values))
        if abs(norm - 1.0) > 1e-5:
            raise AssertionError(f"Baked quaternion is not normalized: {norm}")
        if (
            previous is not None
            and sum(a * b for a, b in zip(previous, values)) < -1e-8
        ):
            raise AssertionError("Baked quaternion changed hemisphere")
        previous = values


def _assert_complete_root_correction_channels(action: bpy.types.Action) -> None:
    curves = {
        (curve.data_path, int(curve.array_index)): curve
        for curve in iter_action_fcurves(action)
        if curve.data_path in {"location", "rotation_quaternion"}
    }
    expected = {
        *(("location", index) for index in range(3)),
        *(("rotation_quaternion", index) for index in range(4)),
    }
    if set(curves) != expected:
        raise AssertionError("Object Root correction omitted complete XYZ/WXYZ channels")

    expected_frames = {float(FIRST_FRAME), float(LAST_FRAME)}
    for key, curve in curves.items():
        frames = {float(point.co[0]) for point in curve.keyframe_points}
        if frames != expected_frames:
            raise AssertionError(f"Object Root channel frames differ: {key}: {frames}")
        if not all(
            math.isfinite(float(point.co[1])) for point in curve.keyframe_points
        ):
            raise AssertionError(f"Object Root channel contains non-finite data: {key}")

    quaternions: list[tuple[float, float, float, float]] = []
    for frame in sorted(expected_frames):
        values = tuple(
            float(
                next(
                    point.co[1]
                    for point in curves[("rotation_quaternion", index)].keyframe_points
                    if abs(float(point.co[0]) - frame) <= 1.0e-6
                )
            )
            for index in range(4)
        )
        if abs(math.hypot(*values) - 1.0) > 1.0e-6:
            raise AssertionError(f"Object Root quaternion is not normalized: {values}")
        quaternions.append(values)
    if sum(
        first * second
        for first, second in zip(quaternions[0], quaternions[1], strict=True)
    ) < 0.0:
        raise AssertionError("Object Root quaternion changed hemisphere")


def _assert_editor_state(expected: dict[str, object]) -> None:
    actual = _capture_editor_state()
    if actual != expected:
        raise AssertionError(f"Editor state changed: {actual!r} != {expected!r}")


def _partial_bake_failure(
    armature: bpy.types.Object,
    _frame_start: int,
    _frame_end: int,
) -> set[str]:
    partial = bpy.data.actions.new("MB_Partial_Bake_Must_Be_Removed")
    if armature.animation_data is None:
        raise AssertionError("Synthetic armature has no animation data")
    armature.animation_data.action = partial
    raise RuntimeError("synthetic partial bake failure")


def _add_foreign_nla_track(
    armature: bpy.types.Object,
    source_action: bpy.types.Action,
    name: str,
) -> tuple[bpy.types.NlaTrack, bpy.types.Action]:
    animation_data = armature.animation_data
    if animation_data is None:
        raise AssertionError("Foreign NLA fixture has no animation data")
    action = source_action.copy()
    action.name = f"{name} Action"
    for key in tuple(action.keys()):
        del action[key]
    track = animation_data.nla_tracks.new()
    track.name = name
    track.strips.new(f"{name} Strip", FIRST_FRAME, action)
    return track, action


def _foreign_nla_snapshot(
    track: bpy.types.NlaTrack,
    action: bpy.types.Action,
) -> tuple[object, ...]:
    strip = track.strips[0]
    return (
        track.as_pointer(),
        track.name,
        bool(track.mute),
        bool(track.lock),
        strip.as_pointer(),
        strip.name,
        strip.action.as_pointer() if strip.action is not None else 0,
        float(strip.frame_start),
        float(strip.frame_end),
        float(strip.influence),
        strip.blend_type,
        strip.extrapolation,
        action.as_pointer(),
        action_checksum(action),
    )


def _base_creation_snapshot(
    armature: bpy.types.Object,
    action: bpy.types.Action,
) -> tuple[object, ...]:
    animation_data = armature.animation_data
    return (
        tuple(sorted((str(key), repr(action[key])) for key in action.keys())),
        tuple(
            sorted(
                (
                    curve.data_path,
                    int(curve.array_index),
                    bool(curve.lock),
                    bool(getattr(curve, "hide", False)),
                    bool(curve.group.lock) if curve.group is not None else None,
                )
                for curve in iter_action_fcurves(action)
            )
        ),
        action_checksum(action),
        bool(action.use_fake_user),
        int(action.users),
        tuple(sorted((str(key), repr(armature[key])) for key in armature.keys())),
        (
            None
            if animation_data is None or animation_data.action is None
            else animation_data.action.as_pointer()
        ),
        (
            str(getattr(animation_data.action_slot, "identifier", ""))
            if animation_data is not None
            and getattr(animation_data, "action_slot", None) is not None
            else ""
        ),
        0 if animation_data is None else len(animation_data.nla_tracks),
    )


def main() -> None:
    fallback_channelbag = type(
        "FallbackChannelbag",
        (),
        {"slot": None, "slot_handle": 101},
    )()
    if (
        _channelbag_slot_identifier(fallback_channelbag, {101: "OBFallback"})
        != "OBFallback"
    ):
        raise AssertionError("Channelbag slot-handle checksum fallback failed")

    foreign_armature, foreign_base = _create_base_action()
    foreign_track, foreign_action = _add_foreign_nla_track(
        foreign_armature,
        foreign_base,
        "Third Party NLA",
    )
    foreign_before = _foreign_nla_snapshot(foreign_track, foreign_action)
    try:
        create_correction_layer(bpy.context, foreign_armature)
    except ValueError as error:
        if "Unsupported NLA coexistence" not in str(error):
            raise
    else:
        raise AssertionError("Foreign pre-existing NLA was not rejected")
    if _foreign_nla_snapshot(foreign_track, foreign_action) != foreign_before:
        raise AssertionError("Rejected stack creation modified foreign NLA")
    if foreign_armature.animation_data.action is not foreign_base:
        raise AssertionError("Rejected stack creation changed the active Base Action")
    if "motion_bridge_stack_id" in foreign_armature:
        raise AssertionError("Rejected stack creation left stack metadata behind")

    shared_armature, shared_base = _create_base_action()
    if not action_is_unshared_for_armature(shared_armature, shared_base):
        raise AssertionError("A single local Base Action was misclassified as shared")
    shared_copy = shared_armature.copy()
    shared_copy.data = shared_armature.data.copy()
    shared_copy.name = "MB_Correction_Shared_Base_Copy"
    bpy.context.scene.collection.objects.link(shared_copy)
    if (
        shared_copy.animation_data is None
        or shared_copy.animation_data.action is not shared_base
    ):
        raise AssertionError("External consumer fixture did not share the Base Action")
    if action_is_unshared_for_armature(shared_armature, shared_base) or (
        action_is_unshared_for_armature(shared_copy, shared_base)
    ):
        raise AssertionError("A shared Base Action was classified as private")
    original_before = _base_creation_snapshot(shared_armature, shared_base)
    copy_before = _base_creation_snapshot(shared_copy, shared_base)
    editor_before = _capture_editor_state()
    action_pointers_before = {
        action.as_pointer() for action in bpy.data.actions
    }
    try:
        create_correction_layer(bpy.context, shared_copy)
    except ValueError as error:
        if "unshared Base Action" not in str(error):
            raise
    else:
        raise AssertionError("Correction creation mutated a shared Base Action")
    if {
        action.as_pointer() for action in bpy.data.actions
    } != action_pointers_before:
        raise AssertionError("Blocked shared-Base creation leaked an Action")
    if _base_creation_snapshot(shared_armature, shared_base) != original_before:
        raise AssertionError("Blocked creation changed the original Base consumer")
    if _base_creation_snapshot(shared_copy, shared_base) != copy_before:
        raise AssertionError("Blocked creation changed the copied Base consumer")
    _assert_editor_state(editor_before)

    armature, base_action = _create_base_action()
    if action_checksum(base_action) != _materialized_v1_checksum(base_action):
        raise AssertionError("Streaming checksum changed the persisted v1 digest")
    companion = bpy.data.objects.new("MB_Preexisting_Selection", None)
    bpy.context.scene.collection.objects.link(companion)
    companion.select_set(True)
    bpy.context.view_layer.objects.active = armature
    if armature.mode != "POSE":
        bpy.ops.object.mode_set(mode="POSE")

    scene = bpy.context.scene
    scene.frame_start = 3
    scene.frame_end = 42
    scene.frame_set(7, subframe=0.25)
    identity_expected = _evaluated_quaternion(armature, 7.25)
    root_expected = _evaluated_root(armature, 7.25)
    expected_state = _capture_editor_state()

    stack = create_correction_layer(
        bpy.context,
        armature,
        provenance={"sourcePath": "/synthetic/source.fbx", "importScale": 1.0},
    )
    _assert_editor_state(expected_state)

    if stack.base_action is not base_action:
        raise AssertionError("The active Action was not retained as Base")
    if any(
        not location.slot_identifier
        for location in iter_action_curve_locations(stack.base_action)
    ):
        raise AssertionError(
            "Slotted Action checksum lost its Channelbag slot identifier"
        )
    validation = validate_layer_stack(armature)
    validation.raise_for_errors()
    if validation.warnings:
        raise AssertionError(f"Unexpected validation warnings: {validation.warnings}")
    if not stack.base_track.lock or stack.base_strip.blend_type != "REPLACE":
        raise AssertionError("Base track is not locked REPLACE")
    if stack.correction_track.lock or stack.correction_strip.blend_type != "COMBINE":
        raise AssertionError("Correction track is not editable COMBINE")
    if (
        action_checksum(stack.archive_action)
        != stack.archive_action[PROP_ARCHIVE_CHECKSUM]
    ):
        raise AssertionError("Archive checksum did not survive custom properties")
    provenance = json.loads(stack.archive_action[PROP_PROVENANCE])
    if provenance["sourcePath"] != "/synthetic/source.fbx":
        raise AssertionError("Archive provenance was not stored")

    access = stack.correction_access
    insert_object_location_key(access, FIRST_FRAME, (0.0, 0.0, 0.0))
    insert_object_location_key(access, LAST_FRAME, (0.0, 0.0, 0.0))
    insert_object_location_key(access, FIRST_FRAME, (-0.0, 0.0, -0.0))
    insert_object_quaternion_key(access, FIRST_FRAME, (2.0, 0.0, 0.0, 0.0))
    insert_object_quaternion_key(access, LAST_FRAME, (-3.0, 0.0, 0.0, 0.0))
    _assert_complete_root_correction_channels(stack.correction_action)
    validate_layer_stack(armature).raise_for_errors()

    valid_root_checksum = action_checksum(stack.correction_action)
    for invalid_insert in (
        lambda: insert_object_location_key(access, 6.0, (math.inf, 0.0, 0.0)),
        lambda: insert_object_quaternion_key(access, 6.0, (0.0, 0.0, 0.0, 0.0)),
    ):
        try:
            invalid_insert()
        except ValueError:
            pass
        else:
            raise AssertionError("Object Root helper accepted invalid transform data")
    if action_checksum(stack.correction_action) != valid_root_checksum:
        raise AssertionError("Rejected Object Root key partially mutated the Action")

    root_quaternion_w = next(
        curve
        for curve in iter_action_fcurves(stack.correction_action)
        if curve.data_path == "rotation_quaternion" and curve.array_index == 0
    )
    last_w = root_quaternion_w.keyframe_points[-1]
    last_w.co[1] = 0.5
    root_quaternion_w.update()
    normalized_validation = validate_layer_stack(armature)
    if not any(
        "Quaternion correction is not normalized" in error
        for error in normalized_validation.errors
    ):
        raise AssertionError("Layer validation accepted a non-unit Object Root key")
    insert_object_quaternion_key(access, LAST_FRAME, (1.0, 0.0, 0.0, 0.0))

    root_location_z = next(
        curve
        for curve in iter_action_fcurves(stack.correction_action)
        if curve.data_path == "location" and curve.array_index == 2
    )
    root_location_z.keyframe_points.remove(root_location_z.keyframe_points[-1])
    root_location_z.update()
    incomplete_validation = validate_layer_stack(armature)
    if not any(
        "Location channel 2 is missing frame" in error
        for error in incomplete_validation.errors
    ):
        raise AssertionError("Layer validation accepted an incomplete Object Root key")
    insert_object_location_key(access, LAST_FRAME, (0.0, 0.0, 0.0))
    validate_layer_stack(armature).raise_for_errors()

    identity_actual = _evaluated_quaternion(armature, 7.25)
    identity_error = _angular_distance_degrees(identity_expected, identity_actual)
    if identity_error > 1e-4:
        raise AssertionError(
            f"Identity correction changed Base by {identity_error} degrees"
        )
    root_identity_error = _root_error(root_expected, _evaluated_root(armature, 7.25))
    if max(root_identity_error) > 1e-5:
        raise AssertionError(f"Identity correction changed root: {root_identity_error}")

    second = create_correction_layer(bpy.context, armature)
    _assert_editor_state(expected_state)
    first_ids = tuple(
        item.as_pointer()
        for item in (
            stack.archive_action,
            stack.base_action,
            stack.correction_action,
            stack.base_track,
            stack.base_strip,
            stack.correction_track,
            stack.correction_strip,
        )
    )
    second_ids = tuple(
        item.as_pointer()
        for item in (
            second.archive_action,
            second.base_action,
            second.correction_action,
            second.base_track,
            second.base_strip,
            second.correction_track,
            second.correction_strip,
        )
    )
    if first_ids != second_ids:
        raise AssertionError("create_correction_layer is not idempotent")
    if get_correction_layer_stack(armature) is None:
        raise AssertionError("Lightweight Correction stack lookup failed")
    set_correction_layer_muted(armature, stack.active_layer_id, True)
    stack.correction_strip.influence = 0.5
    validate_layer_stack(armature).raise_for_errors()
    set_correction_layer_muted(armature, stack.active_layer_id, False)
    stack.correction_strip.influence = 1.0

    correction = Quaternion((1.0, 0.0, 0.0), math.radians(CORRECTION_ANGLE_DEGREES))
    for frame in (FIRST_FRAME, LAST_FRAME):
        insert_quaternion_key(access, "Hips", frame, correction)
    validation = validate_layer_stack(armature)
    validation.raise_for_errors()

    combined = _evaluated_quaternion(armature, 7.25)
    correction_delta = _angular_distance_degrees(identity_expected, combined)
    if abs(correction_delta - CORRECTION_ANGLE_DEGREES) > 0.01:
        raise AssertionError(
            f"COMBINE correction delta is {correction_delta}, expected 90 degrees"
        )

    naive_component_add = Quaternion(
        tuple(identity_expected[index] + correction[index] for index in range(4))
    ).normalized()
    add_artifact_distance = _angular_distance_degrees(combined, naive_component_add)
    if add_artifact_distance < 5.0:
        raise AssertionError(
            "Result is indistinguishable from quaternion component ADD"
        )

    try:
        bake_layer_stack(
            bpy.context,
            armature,
            _baker=lambda start, end: _partial_bake_failure(
                armature,
                start,
                end,
            ),
        )
    except RuntimeError as error:
        if str(error) != "synthetic partial bake failure":
            raise
    else:
        raise AssertionError("Synthetic partial bake failure was not propagated")
    if bpy.data.actions.get("MB_Partial_Bake_Must_Be_Removed") is not None:
        raise AssertionError("Partial bake Action leaked after failure")
    _assert_editor_state(expected_state)
    validate_layer_stack(armature).raise_for_errors()

    baked = bake_layer_stack(bpy.context, armature)
    _assert_editor_state(expected_state)
    validate_layer_stack(armature).raise_for_errors()
    if baked.name != "MB_BAKED::MB_Correction_Base":
        raise AssertionError(f"Unexpected baked Action name: {baked.name}")
    baked_range = tuple(float(value) for value in baked.frame_range)
    if baked_range != (float(FIRST_FRAME), float(LAST_FRAME)):
        raise AssertionError(f"Unexpected baked Action range: {baked_range}")
    _assert_baked_quaternion_continuity(baked)
    baked_paths = {curve.data_path for curve in iter_action_fcurves(baked)}
    if not {"location", "rotation_quaternion"}.issubset(baked_paths):
        raise AssertionError("Visual bake omitted Canonical root trajectory channels")
    try:
        with activate_action_for_export(armature, baked):
            if not all(track.mute for track in armature.animation_data.nla_tracks):
                raise AssertionError("NLA tracks were not muted on export failure path")
            raise RuntimeError("synthetic export failure")
    except RuntimeError as error:
        if str(error) != "synthetic export failure":
            raise
    _assert_editor_state(expected_state)
    validate_layer_stack(armature).raise_for_errors()
    with activate_action_for_export(armature, baked):
        if (
            armature.animation_data is None
            or armature.animation_data.action is not baked
        ):
            raise AssertionError("Baked Action was not activated for export")
        if not all(track.mute for track in armature.animation_data.nla_tracks):
            raise AssertionError("NLA stack remained active during baked Action export")
        baked_evaluated = _evaluated_quaternion(armature, 7.25)
        baked_error = _angular_distance_degrees(combined, baked_evaluated)
        if baked_error > 1e-4:
            raise AssertionError(
                f"Baked Action differs from NLA result by {baked_error}"
            )
        baked_root_error = _root_error(root_expected, _evaluated_root(armature, 7.25))
        if max(baked_root_error) > 1e-5:
            raise AssertionError(
                f"Baked root differs from NLA result: {baked_root_error}"
            )
    _assert_editor_state(expected_state)
    validate_layer_stack(armature).raise_for_errors()

    foreign_track, foreign_action = _add_foreign_nla_track(
        armature,
        stack.base_action,
        "Third Party NLA After Stack",
    )
    foreign_before = _foreign_nla_snapshot(foreign_track, foreign_action)
    coexistence = validate_layer_stack(armature)
    if not any("Unsupported NLA coexistence" in error for error in coexistence.errors):
        raise AssertionError("Foreign NLA coexistence was not classified explicitly")
    try:
        create_correction_layer(bpy.context, armature)
    except ValueError as error:
        if "Unsupported NLA coexistence" not in str(error):
            raise
    else:
        raise AssertionError("Existing stack accepted foreign NLA")
    if _foreign_nla_snapshot(foreign_track, foreign_action) != foreign_before:
        raise AssertionError("Validation or failed mutation modified foreign NLA")
    armature.animation_data.nla_tracks.remove(foreign_track)
    if foreign_action.users == 0:
        bpy.data.actions.remove(foreign_action)
    validate_layer_stack(armature).raise_for_errors()

    archive_curve = next(iter_action_fcurves(stack.archive_action))
    archive_point = archive_curve.keyframe_points[0]
    archived_value = float(archive_point.co[1])
    archive_point.co[1] = archived_value + 0.125
    archive_curve.update()
    tampered = validate_layer_stack(armature)
    if not any("Archive Action content" in error for error in tampered.errors):
        raise AssertionError("Archive checksum validation did not detect content drift")
    _assert_editor_state(expected_state)
    print(
        "MOTION_BRIDGE_CORRECTION_TEST="
        + json.dumps(
            {
                "identityErrorDegrees": identity_error,
                "correctionDeltaDegrees": correction_delta,
                "componentAddArtifactDistanceDegrees": add_artifact_distance,
                "archiveChecksum": stack.archive_action[PROP_ARCHIVE_CHECKSUM],
                "archiveTamperDetected": True,
                "bakedAction": baked.name,
                "bakedFrameRange": baked_range,
                "bakedPoseErrorDegrees": baked_error,
                "bakedQuaternionContinuous": True,
                "bakedRootLocationError": baked_root_error[0],
                "bakedRootRotationErrorDegrees": baked_root_error[1],
                "partialBakeCleanup": True,
                "streamingChecksumCompatible": True,
                "idempotent": True,
                "foreignNlaSafelyBlocked": True,
                "objectRootChannelsValidated": True,
                "sharedBaseCreationBlocked": True,
                "stateRestored": True,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
