from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import threading

import bpy
from mathutils import Quaternion


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))

from motion_bridge.canonical.rig_spec import SKELETON_ID  # noqa: E402
from motion_bridge.editing.action_api import (  # noqa: E402
    ActionChannelAccess,
    ensure_fcurve,
    insert_object_location_key,
    insert_object_quaternion_key,
    insert_quaternion_key,
    iter_action_fcurves,
)
from motion_bridge.editing.correction_layers import (  # noqa: E402
    action_checksum,
    add_correction_layer,
    create_correction_layer,
    set_active_correction_layer,
    validate_layer_stack,
)
import motion_bridge.editing.correction_writer as writer_module  # noqa: E402
from motion_bridge.editing.correction_writer import (  # noqa: E402
    CorrectionBoneTarget,
    CorrectionFrameTarget,
    write_correction_target,
    write_correction_targets,
)
from motion_bridge.motion_quality import BoneSample, MotionFrame  # noqa: E402
from motion_bridge.editing.pose_api import (  # noqa: E402
    pose_bone_selected,
    set_pose_bone_selected,
)


FIRST_FRAME = 1
LAST_FRAME = 10
EPSILON_DEGREES = 1.0e-3
EPSILON_LOCATION = 1.0e-5


def _quaternion(axis: tuple[float, float, float], degrees: float) -> tuple[float, ...]:
    return tuple(Quaternion(axis, math.radians(degrees)))


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for action in tuple(bpy.data.actions):
        bpy.data.actions.remove(action)


def _add_unused_object_slot_channels(action: bpy.types.Action) -> object:
    """Add unsafe-looking curves to a Slot the Armature never evaluates."""

    slots = getattr(action, "slots", None)
    layers = getattr(action, "layers", None)
    if slots is None or layers is None:
        raise AssertionError("Root writer test requires Blender's Slotted Action API")
    unused_slot = slots.new("OBJECT", "MB_Unused_Root_Slot")
    keyframe_strip = next(
        (
            strip
            for layer in layers
            for strip in layer.strips
            if hasattr(strip, "channelbags")
        ),
        None,
    )
    if keyframe_strip is None:
        raise AssertionError("Base Action has no Keyframe Strip")
    channelbag = keyframe_strip.channelbags.new(unused_slot)
    for data_path, value in (("scale", 1.0), ("rotation_euler", 0.0)):
        curve = ensure_fcurve(
            ActionChannelAccess(action, unused_slot, channelbag),
            data_path,
            index=0,
            group_name="Unused Root Slot",
        )
        for frame in (FIRST_FRAME, LAST_FRAME):
            point = curve.keyframe_points.insert(frame, value)
            point.interpolation = "LINEAR"
        curve.update()
    return unused_slot


def _channelbag_for_action_slot(
    action: bpy.types.Action,
    slot: object,
) -> object:
    handle = getattr(slot, "handle", None)
    matches = tuple(
        channelbag
        for layer in action.layers
        for strip in layer.strips
        for channelbag in getattr(strip, "channelbags", ())
        if getattr(channelbag, "slot_handle", None) == handle
    )
    if len(matches) != 1:
        raise AssertionError(
            f"Expected one channelbag for Action Slot, found {len(matches)}"
        )
    return matches[0]


def _add_unit_base_scale_curves(
    action: bpy.types.Action,
    slot: object,
) -> tuple[bpy.types.FCurve, ...]:
    channelbag = _channelbag_for_action_slot(action, slot)
    curves: list[bpy.types.FCurve] = []
    for index in range(3):
        curve = ensure_fcurve(
            ActionChannelAccess(action, slot, channelbag),
            "scale",
            index=index,
            group_name="Root Scale Compatibility",
        )
        for frame in (FIRST_FRAME, LAST_FRAME):
            point = curve.keyframe_points.insert(frame, 1.0)
            point.interpolation = "LINEAR"
        curve.update()
        curves.append(curve)
    return tuple(curves)


def _create_fixture() -> tuple[
    bpy.types.Object,
    bpy.types.Object,
    object,
    object,
]:
    _clear_scene()
    data = bpy.data.armatures.new("MB_Correction_Writer_Data")
    armature = bpy.data.objects.new("MB_Correction_Writer", data)
    bpy.context.scene.collection.objects.link(armature)
    armature["motion_bridge_skeleton_id"] = SKELETON_ID
    armature.rotation_mode = "QUATERNION"
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature

    bpy.ops.object.mode_set(mode="EDIT")
    hips = data.edit_bones.new("Hips")
    hips.head = (0.0, 0.0, 0.0)
    hips.tail = (0.0, 0.0, 1.0)
    spine = data.edit_bones.new("Spine")
    spine.head = (0.0, 0.0, 1.0)
    spine.tail = (0.0, 0.0, 1.8)
    spine.parent = hips
    left_arm = data.edit_bones.new("LeftUpperArm")
    left_arm.head = (0.0, 0.0, 1.6)
    left_arm.tail = (0.8, 0.0, 1.6)
    left_arm.parent = spine
    bpy.ops.object.mode_set(mode="POSE")

    base_rotations = {
        "Hips": _quaternion((0.0, 0.0, 1.0), 20.0),
        "Spine": _quaternion((1.0, 0.0, 0.0), -10.0),
        "LeftUpperArm": _quaternion((0.0, 1.0, 0.0), 5.0),
    }
    for pose_bone in armature.pose.bones:
        pose_bone.rotation_mode = "QUATERNION"
        for frame in (FIRST_FRAME, LAST_FRAME):
            pose_bone.rotation_quaternion = base_rotations[pose_bone.name]
            pose_bone.location = (0.1, 0.0, 0.0) if pose_bone.name == "Hips" else (0, 0, 0)
            pose_bone.keyframe_insert(
                data_path="rotation_quaternion",
                frame=frame,
                group=pose_bone.name,
            )
            if pose_bone.name == "Hips":
                pose_bone.keyframe_insert(
                    data_path="location",
                    frame=frame,
                    group=pose_bone.name,
                )
    for frame in (FIRST_FRAME, LAST_FRAME):
        armature.location = (0.75, -0.2, 0.1)
        armature.rotation_quaternion = _quaternion((0.0, 0.0, 1.0), 18.0)
        armature.keyframe_insert(data_path="location", frame=frame, group="Root")
        armature.keyframe_insert(
            data_path="rotation_quaternion",
            frame=frame,
            group="Root",
        )
    base = armature.animation_data.action
    base.name = "MB_Correction_Writer_Base"
    stack = create_correction_layer(bpy.context, armature)
    lower = stack.active_layer
    for frame in (FIRST_FRAME, LAST_FRAME):
        insert_quaternion_key(
            lower.access,
            "LeftUpperArm",
            frame,
            _quaternion((1.0, 0.0, 0.0), 12.0),
        )
        insert_object_location_key(lower.access, frame, (0.3, 0.1, -0.05))
        insert_object_quaternion_key(
            lower.access,
            frame,
            _quaternion((0.0, 1.0, 0.0), 11.0),
        )
    active = add_correction_layer(bpy.context, armature, name="Writer Destination")
    insert_quaternion_key(
        active.access,
        "LeftUpperArm",
        5.0,
        _quaternion((0.0, 1.0, 0.0), 7.0),
    )

    companion = bpy.data.objects.new("MB_Correction_Writer_Companion", None)
    bpy.context.scene.collection.objects.link(companion)
    return armature, companion, lower, active


def _scene_frame(value: float) -> None:
    integer = math.floor(value)
    bpy.context.scene.frame_set(integer, subframe=value - integer)
    bpy.context.view_layer.update()


def _editor_state(armature: bpy.types.Object) -> dict[str, object]:
    animation_data = armature.animation_data
    return {
        "activeObject": (
            bpy.context.view_layer.objects.active.name
            if bpy.context.view_layer.objects.active is not None
            else None
        ),
        "selectedObjects": tuple(sorted(obj.name for obj in bpy.context.selected_objects)),
        "mode": bpy.context.object.mode if bpy.context.object is not None else None,
        "frame": int(bpy.context.scene.frame_current),
        "subframe": float(bpy.context.scene.frame_subframe),
        "action": animation_data.action,
        "tweak": bool(animation_data.use_tweak_mode),
        "activeTrack": animation_data.nla_tracks.active,
        "tracks": tuple(
            (bool(track.select), bool(track.mute), bool(track.is_solo), bool(track.lock))
            for track in animation_data.nla_tracks
        ),
        "strips": tuple(
            (
                bool(strip.select),
                bool(strip.mute),
                float(strip.influence),
                float(strip.scale),
                float(strip.repeat),
            )
            for track in animation_data.nla_tracks
            for strip in track.strips
        ),
        "selectedBones": tuple(
            sorted(bone.name for bone in armature.pose.bones if pose_bone_selected(bone))
        ),
        "activeBone": (
            armature.data.bones.active.name
            if armature.data.bones.active is not None
            else None
        ),
        "areaTypes": tuple(area.type for area in bpy.context.screen.areas),
    }


def _configure_external_state(
    armature: bpy.types.Object,
    companion: bpy.types.Object,
) -> dict[str, object]:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    armature.select_set(True)
    companion.select_set(True)
    bpy.context.view_layer.objects.active = companion
    for index, bone in enumerate(armature.pose.bones):
        set_pose_bone_selected(bone, bone.name in {"Spine", "LeftUpperArm"})
        if index == 0:
            armature.data.bones.active = bone.bone
    for index, track in enumerate(armature.animation_data.nla_tracks):
        track.select = index % 2 == 0
        track.strips[0].select = index % 2 == 1
    armature.animation_data.nla_tracks.active = armature.animation_data.nla_tracks[0]
    _scene_frame(4.5)
    return _editor_state(armature)


def _assert_state(armature: bpy.types.Object, expected: dict[str, object]) -> None:
    actual = _editor_state(armature)
    if actual != expected:
        raise AssertionError(f"Writer did not restore editor state:\n{actual!r}\n{expected!r}")


def _rotation_error(
    left: tuple[float, ...] | Quaternion,
    right: tuple[float, ...] | Quaternion,
) -> float:
    first = Quaternion(left).normalized()
    second = Quaternion(right).normalized()
    angle = math.degrees(first.rotation_difference(second).angle)
    return min(angle, abs(360.0 - angle))


def _assert_evaluated(
    armature: bpy.types.Object,
    target: CorrectionFrameTarget,
) -> None:
    _scene_frame(target.scene_frame)
    evaluated = armature.evaluated_get(bpy.context.evaluated_depsgraph_get())
    if target.root_location is not None:
        root_location_error = math.dist(
            tuple(evaluated.matrix_world.translation),
            target.root_location,
        )
        if root_location_error > EPSILON_LOCATION:
            raise AssertionError(
                f"Evaluated Root location error at {target.scene_frame}: "
                f"{root_location_error}"
            )
    if target.root_rotation_wxyz is not None:
        root_rotation_error = _rotation_error(
            evaluated.matrix_world.to_quaternion(),
            target.root_rotation_wxyz,
        )
        if root_rotation_error > EPSILON_DEGREES:
            raise AssertionError(
                f"Evaluated Root rotation error at {target.scene_frame}: "
                f"{root_rotation_error} degrees"
            )
    for expected in target.bones:
        actual = evaluated.pose.bones[expected.bone_name]
        local_matrix = writer_module._evaluated_local_matrix(actual)
        error = _rotation_error(local_matrix.to_quaternion(), expected.local_rotation_wxyz)
        if error > EPSILON_DEGREES:
            raise AssertionError(
                f"Evaluated {expected.bone_name} rotation error at "
                f"{target.scene_frame}: {error} degrees"
            )
        if expected.location is not None:
            location_error = math.dist(tuple(local_matrix.translation), expected.location)
            if location_error > EPSILON_LOCATION:
                raise AssertionError(
                    f"Evaluated Hips location error at {target.scene_frame}: "
                    f"{location_error}"
                )


def _curve_signature(action: bpy.types.Action, marker: str) -> tuple[object, ...]:
    return tuple(
        (
            curve.data_path,
            int(curve.array_index),
            tuple(
                (
                    tuple(float(value) for value in point.co),
                    str(point.interpolation),
                )
                for point in curve.keyframe_points
            ),
        )
        for curve in iter_action_fcurves(action)
        if marker in curve.data_path
    )


def _keyed_frames(
    action: bpy.types.Action,
    data_path_suffix: str,
) -> dict[int, tuple[float, ...]]:
    result: dict[int, tuple[float, ...]] = {}
    curves = tuple(
        curve for curve in iter_action_fcurves(action) if curve.data_path.endswith(data_path_suffix)
    )
    for curve in curves:
        result[int(curve.array_index)] = tuple(float(point.co[0]) for point in curve.keyframe_points)
    return result


def _exact_keyed_frames(
    action: bpy.types.Action,
    data_path: str,
) -> dict[int, tuple[float, ...]]:
    result: dict[int, tuple[float, ...]] = {}
    for curve in iter_action_fcurves(action):
        if curve.data_path == data_path:
            result[int(curve.array_index)] = tuple(
                float(point.co[0]) for point in curve.keyframe_points
            )
    return result


def _exact_key_values(
    action: bpy.types.Action,
    data_path: str,
    frame: float,
    size: int,
) -> tuple[float, ...]:
    curves = {
        int(curve.array_index): curve
        for curve in iter_action_fcurves(action)
        if curve.data_path == data_path
    }
    if set(curves) != set(range(size)):
        raise AssertionError(f"Incomplete {data_path} curves: {sorted(curves)!r}")
    values: list[float] = []
    for index in range(size):
        matches = tuple(
            point
            for point in curves[index].keyframe_points
            if abs(float(point.co[0]) - frame) <= 1.0e-6
        )
        if len(matches) != 1:
            raise AssertionError(
                f"Expected one {data_path}[{index}] key at {frame}: {matches!r}"
            )
        values.append(float(matches[0].co[1]))
    return tuple(values)


def _flip_quaternion_key(
    action: bpy.types.Action,
    data_path: str,
    frame: float,
) -> None:
    curves = tuple(
        curve
        for curve in iter_action_fcurves(action)
        if curve.data_path == data_path
    )
    if {int(curve.array_index) for curve in curves} != {0, 1, 2, 3}:
        raise AssertionError(f"Cannot sign-flip incomplete quaternion: {data_path}")
    for curve in curves:
        matches = tuple(
            point
            for point in curve.keyframe_points
            if abs(float(point.co[0]) - frame) <= 1.0e-6
        )
        if len(matches) != 1:
            raise AssertionError(
                f"Expected one quaternion key to sign-flip at {frame}: {data_path}"
            )
        point = matches[0]
        point.co = (float(point.co[0]), -float(point.co[1]))
        curve.update()


def _matrix_signature(matrix: object) -> tuple[float, ...]:
    return tuple(float(value) for row in matrix for value in row)


def _assert_matrix_near(
    actual: object,
    expected: tuple[float, ...],
    label: str,
) -> None:
    values = _matrix_signature(actual)
    error = max(abs(left - right) for left, right in zip(values, expected, strict=True))
    if error > 1.0e-6:
        raise AssertionError(f"{label} was not restored; maximum error is {error}")


def main() -> None:
    armature, companion, lower, active = _create_fixture()
    initial_validation = validate_layer_stack(armature)
    initial_validation.raise_for_errors()
    initial_stack = initial_validation.stack
    if initial_stack is None:
        raise AssertionError("Fixture did not create a Correction stack")
    base_checksum = action_checksum(initial_stack.base_action)
    archive_checksum = action_checksum(initial_stack.archive_action)
    lower_checksum = action_checksum(lower.action)
    non_target_signature = _curve_signature(active.action, "LeftUpperArm")
    expected_state = _configure_external_state(armature, companion)
    initial_revision = int(armature.get(writer_module.MOTION_REVISION_PROPERTY, 0))

    targets = (
        CorrectionFrameTarget(
            3.25,
            (
                CorrectionBoneTarget(
                    "Hips",
                    _quaternion((0.0, 0.0, 1.0), 45.0),
                    (0.25, 0.05, 0.0),
                ),
                CorrectionBoneTarget(
                    "Spine",
                    _quaternion((1.0, 0.0, 0.0), 15.0),
                ),
            ),
        ),
        CorrectionFrameTarget(
            8.0,
            (
                CorrectionBoneTarget(
                    "Hips",
                    _quaternion((0.0, 0.0, 1.0), -10.0),
                    (0.05, -0.02, 0.0),
                ),
                CorrectionBoneTarget(
                    "Spine",
                    _quaternion((1.0, 0.0, 0.0), 25.0),
                ),
            ),
        ),
    )
    result = write_correction_targets(bpy.context, armature, reversed(targets))
    if (
        result.layer_id != active.layer_id
        or result.frame_count != 2
        or result.bone_sample_count != 4
        or result.location_sample_count != 2
        or result.root_location_sample_count != 0
        or result.root_rotation_sample_count != 0
        or result.scene_frame_start != 3.25
        or result.scene_frame_end != 8.0
    ):
        raise AssertionError(f"Unexpected write result: {result!r}")
    if int(armature[writer_module.MOTION_REVISION_PROPERTY]) != initial_revision + 1:
        raise AssertionError("Successful Correction write did not advance its revision")
    _assert_state(armature, expected_state)

    stack = validate_layer_stack(armature).stack
    if stack is None:
        raise AssertionError("Writer damaged the Correction stack")
    if action_checksum(stack.base_action) != base_checksum:
        raise AssertionError("Writer changed Base Action")
    if action_checksum(stack.archive_action) != archive_checksum:
        raise AssertionError("Writer changed Archive Action")
    if action_checksum(lower.action) != lower_checksum:
        raise AssertionError("Writer changed a non-target Correction Action")
    if _curve_signature(active.action, "LeftUpperArm") != non_target_signature:
        raise AssertionError("Writer changed non-target bone curves")

    rotation_frames = _keyed_frames(active.action, 'pose.bones["Hips"].rotation_quaternion')
    location_frames = _keyed_frames(active.action, 'pose.bones["Hips"].location')
    if set(rotation_frames) != {0, 1, 2, 3} or any(
        frames != (3.25, 8.0) for frames in rotation_frames.values()
    ):
        raise AssertionError(f"Writer did not create complete WXYZ keys: {rotation_frames!r}")
    if set(location_frames) != {0, 1, 2} or any(
        frames != (3.25, 8.0) for frames in location_frames.values()
    ):
        raise AssertionError(f"Writer did not create complete XYZ keys: {location_frames!r}")
    for target in targets:
        _assert_evaluated(armature, target)
    _scene_frame(4.5)

    # Root-only and mixed Root/bone writes use the same evaluated NLA inverse
    # path as pose keys.  The second quaternion deliberately uses the opposite
    # representation to exercise hemisphere continuity in the Action output.
    root_first_rotation = _quaternion((0.0, 0.0, 1.0), 42.0)
    root_second_physical = _quaternion((0.0, 0.0, 1.0), 47.0)
    root_second_rotation = tuple(-value for value in root_second_physical)
    root_targets = (
        CorrectionFrameTarget(
            2.5,
            root_location=(2.4, -1.1, 0.35),
            root_rotation_wxyz=root_first_rotation,
        ),
        CorrectionFrameTarget(
            7.75,
            (
                CorrectionBoneTarget(
                    "Spine",
                    _quaternion((1.0, 0.0, 0.0), 32.0),
                ),
            ),
            root_location=(-0.6, 1.8, 0.2),
            root_rotation_wxyz=root_second_rotation,
        ),
    )
    root_result = write_correction_targets(bpy.context, armature, root_targets)
    if (
        root_result.frame_count != 2
        or root_result.bone_sample_count != 1
        or root_result.location_sample_count != 0
        or root_result.root_location_sample_count != 2
        or root_result.root_rotation_sample_count != 2
        or root_result.scene_frame_start != 2.5
        or root_result.scene_frame_end != 7.75
    ):
        raise AssertionError(f"Unexpected Root write result: {root_result!r}")
    if int(armature[writer_module.MOTION_REVISION_PROPERTY]) != initial_revision + 2:
        raise AssertionError("Root Correction write did not advance its revision once")
    _assert_state(armature, expected_state)

    root_location_frames = _exact_keyed_frames(active.action, "location")
    root_rotation_frames = _exact_keyed_frames(active.action, "rotation_quaternion")
    if set(root_location_frames) != {0, 1, 2} or any(
        frames != (2.5, 7.75) for frames in root_location_frames.values()
    ):
        raise AssertionError(
            f"Writer did not create complete Root XYZ keys: {root_location_frames!r}"
        )
    if set(root_rotation_frames) != {0, 1, 2, 3} or any(
        frames != (2.5, 7.75) for frames in root_rotation_frames.values()
    ):
        raise AssertionError(
            f"Writer did not create complete Root WXYZ keys: {root_rotation_frames!r}"
        )

    stored_first_location = _exact_key_values(active.action, "location", 2.5, 3)
    if math.dist(stored_first_location, root_targets[0].root_location) < 1.0e-3:
        raise AssertionError(
            "Root Action location equals the absolute target; NLA inverse was not applied"
        )
    stored_first_rotation = _exact_key_values(
        active.action,
        "rotation_quaternion",
        2.5,
        4,
    )
    stored_second_rotation = _exact_key_values(
        active.action,
        "rotation_quaternion",
        7.75,
        4,
    )
    if sum(
        left * right
        for left, right in zip(
            stored_first_rotation,
            stored_second_rotation,
            strict=True,
        )
    ) < -1.0e-8:
        raise AssertionError("Root quaternion Action keys are not hemisphere-continuous")
    for target in root_targets:
        _assert_evaluated(armature, target)
    _scene_frame(4.5)
    if action_checksum(validate_layer_stack(armature).stack.base_action) != base_checksum:
        raise AssertionError("Root writer changed Base Action")
    if action_checksum(lower.action) != lower_checksum:
        raise AssertionError("Root writer changed a lower Correction Action")

    # A persisted Root quaternion Correction is unsafe if users later switch
    # the Armature Object to Euler mode; stack validation must surface it.
    root_content_checksum = action_checksum(active.action)
    armature.rotation_mode = "XYZ"
    try:
        euler_validation = validate_layer_stack(armature)
        if not any(
            "root" in error.lower() and "quaternion" in error.lower()
            for error in euler_validation.errors
        ):
            raise AssertionError(
                "Stack validation accepted Root quaternion curves in Object Euler mode: "
                f"{euler_validation.errors!r}"
            )
    finally:
        armature.rotation_mode = "QUATERNION"
    if action_checksum(active.action) != root_content_checksum:
        raise AssertionError("Read-only Root compatibility validation changed its Action")
    validate_layer_stack(armature).raise_for_errors()

    # q and -q are identical at a key, but opposite signs on adjacent LINEAR
    # keys take the interpolation through an invalid long path.  Validate both
    # Object Root and pose-bone quaternion continuity after manual edits.
    for data_path, frame, label in (
        ("rotation_quaternion", 7.75, "Root"),
        ('pose.bones["Hips"].rotation_quaternion', 8.0, "bone"),
    ):
        checksum_before_flip = action_checksum(active.action)
        _flip_quaternion_key(active.action, data_path, frame)
        try:
            sign_validation = validate_layer_stack(armature)
            if not any(
                "hemisphere" in error.lower() and data_path in error
                for error in sign_validation.errors
            ):
                raise AssertionError(
                    f"Stack validation accepted a {label} quaternion sign discontinuity: "
                    f"{sign_validation.errors!r}"
                )
        finally:
            _flip_quaternion_key(active.action, data_path, frame)
        if action_checksum(active.action) != checksum_before_flip:
            raise AssertionError(f"{label} quaternion sign-flip fixture did not restore")
        validate_layer_stack(armature).raise_for_errors()

    # Muting an individual Root channel, muting its Action Group, or adding an
    # FCurve modifier changes evaluated output without changing key values.
    # All three must invalidate the managed Correction stack.
    root_location_curve = next(
        curve
        for curve in iter_action_fcurves(active.action)
        if curve.data_path == "location" and int(curve.array_index) == 0
    )
    root_rotation_curve = next(
        curve
        for curve in iter_action_fcurves(active.action)
        if curve.data_path == "rotation_quaternion" and int(curve.array_index) == 0
    )
    for curve, label in (
        (root_location_curve, "Root location"),
        (root_rotation_curve, "Root quaternion"),
    ):
        checksum_before_mute = action_checksum(active.action)
        curve.mute = True
        try:
            mute_validation = validate_layer_stack(armature)
            if not any("mute" in error.lower() for error in mute_validation.errors):
                raise AssertionError(
                    f"Stack validation accepted a muted {label} FCurve: "
                    f"{mute_validation.errors!r}"
                )
        finally:
            curve.mute = False
        if action_checksum(active.action) != checksum_before_mute:
            raise AssertionError(f"{label} mute fixture did not restore its Action")
        validate_layer_stack(armature).raise_for_errors()

    root_group = root_location_curve.group
    if root_group is None:
        raise AssertionError("Root location curve has no Action Group")
    checksum_before_group_mute = action_checksum(active.action)
    root_group.mute = True
    try:
        group_validation = validate_layer_stack(armature)
        if not any(
            "group" in error.lower() and "mute" in error.lower()
            for error in group_validation.errors
        ):
            raise AssertionError(
                "Stack validation accepted a muted Root Action Group: "
                f"{group_validation.errors!r}"
            )
    finally:
        root_group.mute = False
    if action_checksum(active.action) != checksum_before_group_mute:
        raise AssertionError("Root Action Group mute fixture did not restore")
    validate_layer_stack(armature).raise_for_errors()

    checksum_before_modifier = action_checksum(active.action)
    modifier = root_location_curve.modifiers.new("NOISE")
    try:
        modifier_validation = validate_layer_stack(armature)
        if not any(
            "modifier" in error.lower() for error in modifier_validation.errors
        ):
            raise AssertionError(
                "Stack validation accepted a modified Root FCurve: "
                f"{modifier_validation.errors!r}"
            )
    finally:
        root_location_curve.modifiers.remove(modifier)
    if action_checksum(active.action) != checksum_before_modifier:
        raise AssertionError("Root FCurve modifier fixture did not restore its Action")
    validate_layer_stack(armature).raise_for_errors()

    # Location-only and rotation-only requests remain sparse: they key only
    # their requested Object property and independently report sample counts.
    partial_root_targets = (
        CorrectionFrameTarget(1.5, root_location=(1.25, -0.4, 0.6)),
        CorrectionFrameTarget(
            9.25,
            root_rotation_wxyz=_quaternion((0.0, 0.0, 1.0), 28.0),
        ),
    )
    partial_result = write_correction_targets(
        bpy.context,
        armature,
        partial_root_targets,
    )
    if (
        partial_result.frame_count != 2
        or partial_result.bone_sample_count != 0
        or partial_result.location_sample_count != 0
        or partial_result.root_location_sample_count != 1
        or partial_result.root_rotation_sample_count != 1
    ):
        raise AssertionError(f"Unexpected partial Root write result: {partial_result!r}")
    location_only_frames = _exact_keyed_frames(active.action, "location")
    rotation_only_frames = _exact_keyed_frames(active.action, "rotation_quaternion")
    if any(1.5 not in frames or 9.25 in frames for frames in location_only_frames.values()):
        raise AssertionError(
            "Location-only Root request created an incomplete or extra property key"
        )
    if any(9.25 not in frames or 1.5 in frames for frames in rotation_only_frames.values()):
        raise AssertionError(
            "Rotation-only Root request created an incomplete or extra property key"
        )
    for target in partial_root_targets:
        _assert_evaluated(armature, target)
    _scene_frame(4.5)
    _assert_state(armature, expected_state)

    # MotionFrame is a bone-only adapter; its sampled Root is not silently
    # authored as trajectory data.
    motion_target_rotation = _quaternion((1.0, 0.0, 0.0), 35.0)
    motion_frame = MotionFrame(
        frame=6.0,
        time_seconds=0.0,
        root_location=(9.0, 8.0, 7.0),
        root_rotation_wxyz=_quaternion((0.0, 0.0, 1.0), 80.0),
        root_scale=(1.0, 1.0, 1.0),
        bones=(
            BoneSample(
                "Spine",
                motion_target_rotation,
                motion_target_rotation,
                (0.0, 0.0, 1.0),
                (0.0, 0.0, 1.8),
                "Hips",
            ),
        ),
    )
    motion_result = write_correction_target(bpy.context, armature, motion_frame)
    if (
        motion_result.bone_sample_count != 1
        or motion_result.root_location_sample_count != 0
        or motion_result.root_rotation_sample_count != 0
    ):
        raise AssertionError("MotionFrame adapter did not write one bone sample")
    committed_revision = initial_revision + 4
    if int(armature[writer_module.MOTION_REVISION_PROPERTY]) != committed_revision:
        raise AssertionError("Each committed Correction write must advance its revision")
    _assert_evaluated(
        armature,
        CorrectionFrameTarget(
            6.0,
            (CorrectionBoneTarget("Spine", motion_target_rotation),),
        ),
    )
    _scene_frame(4.5)
    _assert_state(armature, expected_state)

    # Unsupported and invalid requests fail during preflight without mutation.
    before_invalid = action_checksum(active.action)

    worker_errors: list[Exception] = []

    def write_from_worker() -> None:
        try:
            write_correction_target(bpy.context, armature, targets[0])
        except Exception as error:
            worker_errors.append(error)

    worker = threading.Thread(target=write_from_worker)
    worker.start()
    worker.join()
    if (
        len(worker_errors) != 1
        or not isinstance(worker_errors[0], RuntimeError)
        or "main thread" not in str(worker_errors[0])
    ):
        raise AssertionError(f"Background writer was not rejected: {worker_errors!r}")

    constraint = armature.pose.bones["Spine"].constraints.new("LIMIT_ROTATION")
    constraint.name = "Writer Safety Constraint"
    try:
        try:
            write_correction_target(bpy.context, armature, targets[0])
        except ValueError as error:
            if "active constraints" not in str(error):
                raise
        else:
            raise AssertionError("Constraint-driven target was accepted")
    finally:
        armature.pose.bones["Spine"].constraints.remove(constraint)

    invalid_requests = (
        CorrectionFrameTarget(
            4.0,
            (CorrectionBoneTarget("Spine", motion_target_rotation, (0, 0, 0)),),
        ),
        CorrectionFrameTarget(
            4.0,
            (CorrectionBoneTarget("MissingBone", motion_target_rotation),),
        ),
    )
    for invalid in invalid_requests:
        try:
            write_correction_target(bpy.context, armature, invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid Correction target was accepted")
    root_gate_target = CorrectionFrameTarget(
        4.0,
        root_location=(0.2, 0.3, 0.4),
        root_rotation_wxyz=_quaternion((0.0, 0.0, 1.0), 15.0),
    )

    def expect_target_gate(target: CorrectionFrameTarget, label: str) -> None:
        try:
            write_correction_target(bpy.context, armature, target)
        except ValueError:
            return
        raise AssertionError(f"Unsafe Root destination was accepted: {label}")

    def expect_root_gate(label: str) -> None:
        expect_target_gate(root_gate_target, label)

    armature.parent = companion
    try:
        expect_root_gate("parent")
    finally:
        armature.parent = None

    object_constraint = armature.constraints.new("COPY_LOCATION")
    object_constraint.name = "Writer Root Safety Constraint"
    try:
        expect_root_gate("Object constraint")
    finally:
        armature.constraints.remove(object_constraint)

    armature.driver_add("location", 0)
    try:
        expect_root_gate("Object driver")
    finally:
        armature.driver_remove("location", 0)

    # A driver on any Object transform channel can still affect the world
    # Root, even when that exact property was omitted from this sparse target.
    location_only_gate_target = CorrectionFrameTarget(
        4.0,
        root_location=(0.2, 0.3, 0.4),
    )
    for data_path, array_index, neutral_value in (
        ("rotation_quaternion", 0, 1.0),
        ("delta_location", 0, 0.0),
        ("scale", 0, 1.0),
    ):
        driver_curve = armature.driver_add(data_path, array_index)
        driver_curve.driver.expression = repr(neutral_value)
        bpy.context.view_layer.update()
        try:
            expect_target_gate(
                location_only_gate_target,
                f"non-requested {data_path} driver",
            )
        finally:
            armature.driver_remove(data_path, array_index)
            bpy.context.view_layer.update()

    armature.delta_location = (0.1, 0.0, 0.0)
    try:
        expect_root_gate("delta transform")
    finally:
        armature.delta_location = (0.0, 0.0, 0.0)

    armature.scale = (1.25, 1.0, 1.0)
    try:
        expect_root_gate("Object scale")
    finally:
        armature.scale = (1.0, 1.0, 1.0)

    armature.rotation_mode = "XYZ"
    try:
        expect_root_gate("Euler Object rotation")
    finally:
        armature.rotation_mode = "QUATERNION"

    if action_checksum(active.action) != before_invalid:
        raise AssertionError("Invalid target changed the active Correction")
    if int(armature[writer_module.MOTION_REVISION_PROPERTY]) != committed_revision:
        raise AssertionError("Rejected Correction write advanced its revision")
    _assert_state(armature, expected_state)

    # A failure after real Root and bone NLA key insertion rolls back all
    # targeted curves, assigned transforms, editor state, and revision.
    before_rollback = action_checksum(active.action)
    object_basis_before = _matrix_signature(armature.matrix_basis)
    bone_bases_before = {
        bone.name: _matrix_signature(bone.matrix_basis)
        for bone in armature.pose.bones
    }
    original_insert = writer_module._capture_nla_remapped_pose_values
    calls = 0

    def fail_after_insert(*args: object, **kwargs: object) -> object:
        nonlocal calls
        result_value = original_insert(*args, **kwargs)
        calls += 1
        if calls == 1:
            raise RuntimeError("injected writer failure")
        return result_value

    writer_module._capture_nla_remapped_pose_values = fail_after_insert
    try:
        try:
            write_correction_targets(
                bpy.context,
                armature,
                (
                    CorrectionFrameTarget(
                        4.0,
                        root_location=(1.8, 0.4, -0.2),
                        root_rotation_wxyz=_quaternion((0, 1, 0), 30),
                    ),
                    CorrectionFrameTarget(
                        7.0,
                        (CorrectionBoneTarget("Spine", _quaternion((0, 1, 0), 40)),),
                        root_location=(-0.8, 0.9, 0.5),
                        root_rotation_wxyz=_quaternion((0, 1, 0), 35),
                    ),
                ),
            )
        except RuntimeError as error:
            if "injected writer failure" not in str(error):
                raise
        else:
            raise AssertionError("Injected writer failure did not propagate")
    finally:
        writer_module._capture_nla_remapped_pose_values = original_insert
    if action_checksum(active.action) != before_rollback:
        raise AssertionError("Writer failure did not roll back the active Correction")
    if int(armature[writer_module.MOTION_REVISION_PROPERTY]) != committed_revision:
        raise AssertionError("Rolled-back Correction write advanced its revision")
    _assert_state(armature, expected_state)
    _assert_matrix_near(armature.matrix_basis, object_basis_before, "Object matrix_basis")
    for bone_name, expected_basis in bone_bases_before.items():
        _assert_matrix_near(
            armature.pose.bones[bone_name].matrix_basis,
            expected_basis,
            f"{bone_name} matrix_basis",
        )
    validate_layer_stack(armature).raise_for_errors()

    # Verification runs only after Correction Edit has exited.  A failure at
    # that late boundary must still remove real Root/bone keys and restore all
    # RNA/editor/revision state, not merely cancel an open Tweak session.
    post_exit_target = CorrectionFrameTarget(
        4.125,
        (
            CorrectionBoneTarget(
                "Hips",
                _quaternion((0.0, 0.0, 1.0), 31.0),
                (0.15, -0.03, 0.02),
            ),
            CorrectionBoneTarget(
                "Spine",
                _quaternion((1.0, 0.0, 0.0), 21.0),
            ),
        ),
        root_location=(1.6, -0.7, 0.45),
        root_rotation_wxyz=_quaternion((0.0, 1.0, 0.0), 24.0),
    )
    before_post_exit_failure = action_checksum(active.action)
    post_exit_object_basis = _matrix_signature(armature.matrix_basis)
    post_exit_bone_bases = {
        bone_name: _matrix_signature(armature.pose.bones[bone_name].matrix_basis)
        for bone_name in ("Hips", "Spine")
    }
    original_verify = writer_module._verify_written_targets
    observed_post_exit_keys = False

    def fail_post_exit_verification(
        _context: object,
        target_armature: bpy.types.Object,
        _targets: object,
        **_kwargs: object,
    ) -> None:
        nonlocal observed_post_exit_keys
        animation_data = target_armature.animation_data
        if animation_data is None:
            raise AssertionError("Post-exit verification lost Armature animation data")
        if animation_data.use_tweak_mode or writer_module.has_correction_edit_session(
            target_armature
        ):
            raise AssertionError("Target verification ran before Correction Edit exited")
        frame = post_exit_target.scene_frame
        _exact_key_values(active.action, "location", frame, 3)
        _exact_key_values(active.action, "rotation_quaternion", frame, 4)
        _exact_key_values(
            active.action,
            'pose.bones["Hips"].location',
            frame,
            3,
        )
        _exact_key_values(
            active.action,
            'pose.bones["Hips"].rotation_quaternion',
            frame,
            4,
        )
        _exact_key_values(
            active.action,
            'pose.bones["Spine"].rotation_quaternion',
            frame,
            4,
        )
        observed_post_exit_keys = True
        raise RuntimeError("injected post-exit verification failure")

    writer_module._verify_written_targets = fail_post_exit_verification
    try:
        try:
            write_correction_target(bpy.context, armature, post_exit_target)
        except RuntimeError as error:
            if "injected post-exit verification failure" not in str(error):
                raise
        else:
            raise AssertionError("Injected post-exit verification failure did not propagate")
    finally:
        writer_module._verify_written_targets = original_verify
    if not observed_post_exit_keys:
        raise AssertionError("Post-exit failure occurred before real keys were observed")
    if action_checksum(active.action) != before_post_exit_failure:
        raise AssertionError("Post-exit verification failure did not roll back its Action")
    if int(armature[writer_module.MOTION_REVISION_PROPERTY]) != committed_revision:
        raise AssertionError("Post-exit verification failure changed motion revision")
    _assert_state(armature, expected_state)
    _assert_matrix_near(
        armature.matrix_basis,
        post_exit_object_basis,
        "Post-exit Object matrix_basis",
    )
    for bone_name, expected_basis in post_exit_bone_bases.items():
        _assert_matrix_near(
            armature.pose.bones[bone_name].matrix_basis,
            expected_basis,
            f"Post-exit {bone_name} matrix_basis",
        )
    validate_layer_stack(armature).raise_for_errors()

    # Full Copy/shared Correction and non-normalized destination are hard blocks.
    shared_copy = armature.copy()
    shared_copy.data = armature.data.copy()
    bpy.context.scene.collection.objects.link(shared_copy)
    try:
        try:
            write_correction_target(bpy.context, armature, targets[0])
        except ValueError as error:
            if "unshared" not in str(error):
                raise
        else:
            raise AssertionError("Shared Correction Action was writable")
    finally:
        bpy.data.objects.remove(shared_copy, do_unlink=True)
    active.strip.influence = 0.5
    try:
        try:
            write_correction_target(bpy.context, armature, targets[0])
        except ValueError:
            pass
        else:
            raise AssertionError("Non-unit Correction influence was accepted")
    finally:
        active.strip.influence = 1.0
    active.track.mute = True
    try:
        try:
            write_correction_target(bpy.context, armature, targets[0])
        except ValueError:
            pass
        else:
            raise AssertionError("Muted Correction destination was accepted")
    finally:
        active.track.mute = False
    active.strip.use_animated_time = True
    try:
        try:
            write_correction_target(bpy.context, armature, targets[0])
        except ValueError:
            pass
        else:
            raise AssertionError("Retimed Correction destination was accepted")
    finally:
        active.strip.use_animated_time = False
    if action_checksum(active.action) != before_rollback:
        raise AssertionError("Blocked destination changed the active Correction")
    _assert_state(armature, expected_state)

    # Writing an intermediate layer while an enabled upper layer contributes
    # would make the requested evaluated pose ambiguous.  The safe public API
    # requires the highest enabled destination.
    upper = add_correction_layer(bpy.context, armature, name="Enabled Upper")
    set_active_correction_layer(armature, active.layer_id)
    try:
        write_correction_target(bpy.context, armature, targets[0])
    except ValueError as error:
        if "highest enabled layer" not in str(error):
            raise
    else:
        raise AssertionError("Writer accepted an active layer below an enabled layer")
    set_active_correction_layer(armature, upper.layer_id)
    if int(armature[writer_module.MOTION_REVISION_PROPERTY]) != committed_revision:
        raise AssertionError("Blocked destination advanced its motion revision")

    # Root preflight must inspect only the Action Slot evaluated by the Base
    # strip.  Scale/Euler curves in another Object Slot are unrelated data.
    slot_validation = validate_layer_stack(armature)
    slot_validation.raise_for_errors()
    slot_stack = slot_validation.stack
    if slot_stack is None:
        raise AssertionError("Correction stack disappeared before Slot isolation test")
    assigned_slot = getattr(slot_stack.base_strip, "action_slot", None)
    if assigned_slot is None:
        assigned_handle = getattr(slot_stack.base_strip, "action_slot_handle", None)
        assigned_slot = next(
            (
                slot
                for slot in slot_stack.base_action.slots
                if getattr(slot, "handle", None) == assigned_handle
            ),
            None,
        )
    if assigned_slot is None:
        raise AssertionError("Base strip has no resolvable evaluated Action Slot")

    scale_curves = _add_unit_base_scale_curves(
        slot_stack.base_action,
        assigned_slot,
    )
    unused_slot = _add_unused_object_slot_channels(slot_stack.base_action)
    if assigned_slot == unused_slot:
        raise AssertionError("Base strip unexpectedly evaluates the unused Action Slot")
    writer_module._validate_root_destination(
        armature,
        slot_stack,
        (root_gate_target,),
    )

    nonunit_point = max(
        scale_curves[0].keyframe_points,
        key=lambda point: float(point.co[0]),
    )
    original_nonunit_co = tuple(float(value) for value in nonunit_point.co)
    nonunit_point.co = (original_nonunit_co[0], 1.2)
    scale_curves[0].update()
    try:
        try:
            writer_module._validate_root_destination(
                armature,
                slot_stack,
                (root_gate_target,),
            )
        except ValueError:
            pass
        else:
            raise AssertionError("Root preflight accepted a non-unit Base scale key")
    finally:
        nonunit_point.co = original_nonunit_co
        scale_curves[0].update()

    overshoot_curve = scale_curves[1]
    overshoot_points = sorted(
        overshoot_curve.keyframe_points,
        key=lambda point: float(point.co[0]),
    )
    if len(overshoot_points) != 2:
        raise AssertionError("Base scale overshoot fixture requires two keys")
    left_point, right_point = overshoot_points
    left_point.interpolation = "BEZIER"
    right_point.interpolation = "BEZIER"
    left_point.handle_right_type = "FREE"
    right_point.handle_left_type = "FREE"
    left_point.handle_right = (4.0, 2.0)
    right_point.handle_left = (7.0, 2.0)
    overshoot_curve.update()
    try:
        writer_module._validate_root_destination(
            armature,
            slot_stack,
            (root_gate_target,),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Root preflight accepted Base scale Bezier overshoot")

    print(
        "MOTION_BRIDGE_CORRECTION_WRITER="
        + json.dumps(
            {
                "baseArchiveAndNonTargetUnchanged": True,
                "baseScaleSafety": True,
                "baseUnusedSlotIgnored": True,
                "completeQuaternionAndLocation": True,
                "evaluatedInverseKeying": True,
                "invalidAndRootGates": True,
                "motionFrameAdapter": True,
                "mainThreadAndConstraintGate": True,
                "rootCompleteAndInverseKeying": True,
                "rootDriverSurfaceGate": True,
                "rootModeAndHemisphereValidation": True,
                "rootMutedAndModifiedCurveGate": True,
                "rootPartialChannels": True,
                "rootPostExitVerificationRollback": True,
                "rootQuaternionContinuity": True,
                "rootRollback": True,
                "topEnabledLayerGate": True,
                "rollback": True,
                "revisionTracked": True,
                "sharedAndNormalizedGate": True,
                "stateRestored": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
