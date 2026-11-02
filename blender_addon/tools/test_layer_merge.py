from __future__ import annotations

import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import bpy
from mathutils import Quaternion


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))

from motion_bridge.canonical.rig_spec import SKELETON_ID  # noqa: E402
from motion_bridge.editing.action_api import (  # noqa: E402
    action_slot_for_id,
    ensure_fcurve,
    insert_quaternion_key,
    iter_action_fcurves,
    remove_fcurve,
)
from motion_bridge.editing.correction_layers import (  # noqa: E402
    PROP_ACTIVE_LAYER_ID,
    PROP_ARCHIVE_CHECKSUM,
    PROP_BASE_CHECKSUM,
    PROP_LAYER_MUTED,
    LayerStackValidation,
    action_checksum,
    add_correction_layer,
    create_correction_layer,
    list_correction_layers,
    remove_correction_layer,
    validate_layer_stack,
)
from motion_bridge.editing.comparison import (  # noqa: E402
    corrections_preview_enabled,
    set_corrections_preview_enabled,
)
from motion_bridge.editing.layer_merge import (  # noqa: E402
    merge_correction_layer_down,
)
import motion_bridge.editing.layer_merge as merge_module  # noqa: E402
import motion_bridge.editing.correction_layers as correction_module  # noqa: E402


FIRST_FRAME = 1
LAST_FRAME = 12
BONES = ("Hips", "Spine", "Arm")
LINEAR_VALIDATION_KEY_COUNT = 512


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for action in tuple(bpy.data.actions):
        bpy.data.actions.remove(action)


def _create_base_action() -> tuple[bpy.types.Object, bpy.types.Action]:
    _clear_scene()
    data = bpy.data.armatures.new("MB_Layer_Merge_Data")
    armature = bpy.data.objects.new("MB_Layer_Merge", data)
    bpy.context.scene.collection.objects.link(armature)
    armature["motion_bridge_skeleton_id"] = SKELETON_ID
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode="EDIT")
    previous = None
    for index, name in enumerate(BONES):
        bone = data.edit_bones.new(name)
        bone.head = (0.0, 0.0, float(index))
        bone.tail = (0.0, 0.0, float(index + 1))
        bone.parent = previous
        previous = bone
    bpy.ops.object.mode_set(mode="POSE")
    rotations = {
        "Hips": Quaternion((0.0, 0.0, 1.0), math.radians(12.0)),
        "Spine": Quaternion((1.0, 0.0, 0.0), math.radians(8.0)),
        "Arm": Quaternion((0.0, 1.0, 0.0), math.radians(5.0)),
    }
    for name in BONES:
        armature.pose.bones[name].rotation_mode = "QUATERNION"
    for frame in (FIRST_FRAME, LAST_FRAME):
        bpy.context.scene.frame_set(frame)
        for name, rotation in rotations.items():
            pose_bone = armature.pose.bones[name]
            pose_bone.rotation_quaternion = rotation
            pose_bone.keyframe_insert(
                data_path="rotation_quaternion",
                frame=frame,
                group=name,
            )
    action = armature.animation_data.action if armature.animation_data else None
    if action is None:
        raise AssertionError("Synthetic Base Action was not created")
    action.name = "MB_Layer_Merge_Base"
    return armature, action


def _key_quaternion_layer(
    layer: object,
    bone_name: str,
    rotation: Quaternion,
) -> None:
    for frame in (FIRST_FRAME, LAST_FRAME):
        insert_quaternion_key(layer.access, bone_name, frame, rotation)


def _key_scalar_layer(
    layer: object,
    *,
    data_path: str = 'pose.bones["Arm"].location',
    array_index: int = 0,
    group_name: str = "Arm Translation",
) -> bpy.types.FCurve:
    curve = ensure_fcurve(
        layer.access,
        data_path,
        array_index,
        group_name=group_name,
    )
    first = curve.keyframe_points.insert(FIRST_FRAME, 0.1, options={"FAST"})
    last = curve.keyframe_points.insert(LAST_FRAME, 0.25, options={"FAST"})
    first.interpolation = "LINEAR"
    last.interpolation = "LINEAR"
    curve.update()
    return curve


def _assert_linear_quaternion_validation() -> int:
    point_visits = 0

    class CountingPoints:
        def __init__(self, value: float) -> None:
            self._points = tuple(
                SimpleNamespace(co=(float(frame), value))
                for frame in range(LINEAR_VALIDATION_KEY_COUNT)
            )

        def __iter__(self):
            nonlocal point_visits
            for point in self._points:
                point_visits += 1
                yield point

    data_path = 'pose.bones["Arm"].rotation_quaternion'
    curves = tuple(
        SimpleNamespace(
            data_path=data_path,
            array_index=array_index,
            keyframe_points=CountingPoints(value),
        )
        for array_index, value in enumerate((1.0, 0.0, 0.0, 0.0))
    )
    original_iterator = correction_module.iter_action_fcurves
    correction_module.iter_action_fcurves = lambda _action: iter(curves)
    try:
        errors = correction_module._quaternion_curve_errors(object())
    finally:
        correction_module.iter_action_fcurves = original_iterator
    if errors:
        raise AssertionError(f"Dense valid quaternion was rejected: {errors[0]}")
    if point_visits > 8 * LINEAR_VALIDATION_KEY_COUNT:
        raise AssertionError(
            "Quaternion validation revisited dense keyframes nonlinearly: "
            f"{point_visits} visits"
        )
    linear_point_visits = point_visits

    curves[0].keyframe_points._points += (SimpleNamespace(co=(0.0000004, 1.0)),)
    point_visits = 0
    correction_module.iter_action_fcurves = lambda _action: iter(curves)
    try:
        collision_errors = correction_module._quaternion_curve_errors(object())
    finally:
        correction_module.iter_action_fcurves = original_iterator
    if not any("multiple keys at rounded frame" in error for error in collision_errors):
        raise AssertionError("Rounded-frame quaternion collision was not rejected")
    return linear_point_visits


def _author_metadata(action: bpy.types.Action) -> None:
    for curve_index, curve in enumerate(iter_action_fcurves(action)):
        curve.extrapolation = "LINEAR"
        curve.color_mode = "CUSTOM"
        curve.color = (
            0.15 + curve_index * 0.05,
            0.35,
            0.65 - curve_index * 0.05,
        )
        curve.select = curve_index % 2 == 0
        if curve.group is not None:
            curve.group.color_set = "CUSTOM"
            curve.group.colors.normal = (0.2, 0.3, 0.4)
            curve.group.colors.select = (0.4, 0.5, 0.6)
            curve.group.colors.active = (0.6, 0.7, 0.8)
            curve.group.show_expanded = False
        for point_index, point in enumerate(curve.keyframe_points):
            point.interpolation = "BEZIER"
            point.easing = "EASE_IN_OUT"
            point.handle_left_type = "FREE"
            point.handle_right_type = "FREE"
            point.handle_left = (
                float(point.co[0]) - 0.3,
                float(point.co[1]) - 0.02 * (curve_index + 1),
            )
            point.handle_right = (
                float(point.co[0]) + 0.4,
                float(point.co[1]) + 0.03 * (curve_index + 1),
            )
            point.amplitude = 0.8 + point_index * 0.1
            point.back = 0.6 + point_index * 0.1
            point.period = 0.7 + point_index * 0.1
            point.type = "EXTREME" if point_index == 0 else "BREAKDOWN"
            point.select_control_point = point_index == 0
            point.select_left_handle = point_index == 1
            point.select_right_handle = point_index == 0


def _point_snapshot(point: Any) -> tuple[Any, ...]:
    return (
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


def _curve_snapshot(curve: bpy.types.FCurve) -> tuple[Any, ...]:
    group = curve.group
    group_snapshot = None
    if group is not None:
        group_snapshot = (
            str(group.name),
            str(group.color_set),
            tuple(float(value) for value in group.colors.normal),
            tuple(float(value) for value in group.colors.select),
            tuple(float(value) for value in group.colors.active),
            bool(group.show_expanded),
        )
    return (
        str(curve.extrapolation),
        str(curve.color_mode),
        tuple(float(value) for value in curve.color),
        bool(curve.select),
        group_snapshot,
        tuple(_point_snapshot(point) for point in curve.keyframe_points),
    )


def _action_snapshot(action: bpy.types.Action) -> dict[tuple[str, int], tuple[Any, ...]]:
    return {
        (str(curve.data_path), int(curve.array_index)): _curve_snapshot(curve)
        for curve in iter_action_fcurves(action)
    }


def _pose_snapshot(
    armature: bpy.types.Object,
    frames: tuple[float, ...] = (1.0, 3.25, 7.5, 12.0),
) -> dict[tuple[float, str], Quaternion]:
    result: dict[tuple[float, str], Quaternion] = {}
    for frame in frames:
        integer = math.floor(frame)
        bpy.context.scene.frame_set(integer, subframe=frame - integer)
        bpy.context.view_layer.update()
        for bone_name in BONES:
            result[(frame, bone_name)] = (
                armature.pose.bones[bone_name].matrix.to_quaternion().normalized()
            )
    return result


def _pose_error(
    first: dict[tuple[float, str], Quaternion],
    second: dict[tuple[float, str], Quaternion],
) -> float:
    return max(
        math.degrees(first[key].rotation_difference(second[key]).angle)
        for key in first
    )


def _logical_state(armature: bpy.types.Object) -> tuple[Any, ...]:
    layers = list_correction_layers(armature)
    return (
        tuple(layer.layer_id for layer in layers),
        tuple(layer.action.as_pointer() for layer in layers),
        tuple(action_checksum(layer.action) for layer in layers),
        tuple(bool(layer.track.select) for layer in layers),
        armature.get(PROP_ACTIVE_LAYER_ID),
        len(armature.animation_data.nla_tracks),
        frozenset(action.as_pointer() for action in bpy.data.actions),
    )


def _raw_binding_state(armature: bpy.types.Object) -> tuple[Any, ...]:
    return (
        tuple(
            (
                track.as_pointer(),
                track.strips[0].action.as_pointer(),
                float(track.strips[0].frame_start),
                float(track.strips[0].frame_end),
            )
            for track in armature.animation_data.nla_tracks
        ),
        armature.get(PROP_ACTIVE_LAYER_ID),
        frozenset(action.as_pointer() for action in bpy.data.actions),
    )


def _nla_edit_state(layer: object) -> tuple[Any, ...]:
    optional: list[tuple[str, Any]] = []
    for owner_name, owner in (("track", layer.track), ("strip", layer.strip)):
        for attribute in ("use_custom_color", "color"):
            if not hasattr(owner, attribute):
                continue
            value = getattr(owner, attribute)
            if attribute == "color":
                value = tuple(float(component) for component in value)
            optional.append((f"{owner_name}.{attribute}", value))
    return (
        str(layer.track.name),
        str(layer.strip.name),
        bool(layer.strip.select),
        bool(getattr(layer.strip, "use_sync_length", False)),
        bool(getattr(layer.strip, "use_animated_time_cyclic", False)),
        float(getattr(layer.strip, "strip_time", 0.0)),
        tuple(optional),
    )


def _expect_error(expected: str, callback: Any) -> None:
    try:
        callback()
    except ValueError as error:
        if expected not in str(error):
            raise AssertionError(f"Unexpected rejection: {error}") from error
    else:
        raise AssertionError(f"Expected merge rejection containing {expected!r}")


def _assign_active_action(
    armature: bpy.types.Object,
    action: bpy.types.Action | None,
) -> None:
    animation_data = armature.animation_data
    if animation_data is None:
        raise AssertionError("Armature has no animation data")
    animation_data.action = action
    if action is None:
        return
    slot = action_slot_for_id(action, armature)
    if slot is None:
        return
    if hasattr(animation_data, "action_slot"):
        animation_data.action_slot = slot
    elif hasattr(animation_data, "action_slot_handle"):
        animation_data.action_slot_handle = slot.handle


def main() -> None:
    armature, base = _create_base_action()
    stack = create_correction_layer(bpy.context, armature)
    archive = stack.archive_action
    base_checksum = action_checksum(base)
    archive_checksum = action_checksum(archive)

    lower = stack.active_layer
    _key_quaternion_layer(
        lower,
        "Hips",
        Quaternion((1.0, 0.0, 0.0), math.radians(25.0)),
    )
    upper = add_correction_layer(bpy.context, armature, name="Spine Detail")
    _key_quaternion_layer(
        upper,
        "Spine",
        Quaternion((0.0, 1.0, 0.0), math.radians(32.0)),
    )
    _author_metadata(upper.action)
    upper_metadata = _action_snapshot(upper.action)
    lower.action.use_fake_user = True
    upper.action.use_fake_user = True
    shared_armature = armature.copy()
    shared_armature.data = armature.data.copy()
    shared_armature.name = "MB_Layer_Merge_Shared"
    bpy.context.scene.collection.objects.link(shared_armature)
    shared_validation = validate_layer_stack(shared_armature)
    shared_validation.raise_for_errors()
    assert shared_validation.stack is not None
    shared_layer_ids = tuple(
        layer.layer_id for layer in shared_validation.stack.correction_layers
    )
    shared_action_pointers = tuple(
        layer.action.as_pointer()
        for layer in shared_validation.stack.correction_layers
    )
    shared_fake_user_state = tuple(
        bool(layer.action.use_fake_user)
        for layer in shared_validation.stack.correction_layers
    )
    shared_pose_before_merge = _pose_snapshot(shared_armature)
    pose_before_merge = _pose_snapshot(armature)
    lower_id = lower.layer_id
    upper_id = upper.layer_id
    original_shared_binding = _raw_binding_state(armature)
    copied_shared_binding = _raw_binding_state(shared_armature)
    _expect_error(
        "unshared Correction Actions",
        lambda: merge_correction_layer_down(
            bpy.context,
            armature,
            upper_id,
            name="Must Stay Shared",
        ),
    )
    if (
        _raw_binding_state(armature) != original_shared_binding
        or _raw_binding_state(shared_armature) != copied_shared_binding
    ):
        raise AssertionError("Shared Action merge rejection mutated an NLA stack")
    for candidate, expected_ids, expected_actions, expected_fake_users in (
        (
            armature,
            shared_layer_ids,
            shared_action_pointers,
            shared_fake_user_state,
        ),
        (
            shared_armature,
            shared_layer_ids,
            shared_action_pointers,
            shared_fake_user_state,
        ),
    ):
        candidate_validation = validate_layer_stack(candidate)
        candidate_validation.raise_for_errors()
        assert candidate_validation.stack is not None
        if (
            tuple(
                layer.layer_id
                for layer in candidate_validation.stack.correction_layers
            )
            != expected_ids
            or tuple(
                layer.action.as_pointer()
                for layer in candidate_validation.stack.correction_layers
            )
            != expected_actions
            or tuple(
                bool(layer.action.use_fake_user)
                for layer in candidate_validation.stack.correction_layers
            )
            != expected_fake_users
        ):
            raise AssertionError("Shared Action merge rejection changed layer identity")
    if _pose_error(shared_pose_before_merge, _pose_snapshot(shared_armature)) > 1e-5:
        raise AssertionError("Shared Action merge rejection changed the copied pose")

    shared_data = shared_armature.data
    bpy.data.objects.remove(shared_armature, do_unlink=True)
    if shared_data.users == 0:
        bpy.data.armatures.remove(shared_data)
    set_corrections_preview_enabled(armature, False)
    if corrections_preview_enabled(armature) or not all(
        layer.track.mute for layer in list_correction_layers(armature)
    ):
        raise AssertionError("Before preview was not active before merge")

    original_checksum_function = correction_module.action_checksum

    def fail_dense_integrity_scan(_action: bpy.types.Action) -> str:
        raise AssertionError("Correction merge performed a dense integrity scan")

    correction_module.action_checksum = fail_dense_integrity_scan
    try:
        merged = merge_correction_layer_down(
            bpy.context,
            armature,
            upper_id,
            name="Flattened",
        )
    finally:
        correction_module.action_checksum = original_checksum_function
    if corrections_preview_enabled(armature) or not merged.track.mute:
        raise AssertionError("Merge did not preserve the Before preview bypass")
    set_corrections_preview_enabled(armature, True)
    if merged.layer_id != lower_id or merged.name != "Flattened":
        raise AssertionError("Merge did not retain the lower stable id/name result")
    layers = list_correction_layers(armature)
    if len(layers) != 1 or any(layer.layer_id == upper_id for layer in layers):
        raise AssertionError("Successful merge did not remove only the upper layer")
    pose_error = _pose_error(pose_before_merge, _pose_snapshot(armature))
    if pose_error > 1e-5:
        raise AssertionError(f"Merged layer changed evaluated pose: {pose_error}")
    merged_snapshot = _action_snapshot(merged.action)
    for channel, metadata in upper_metadata.items():
        if merged_snapshot.get(channel) != metadata:
            raise AssertionError(f"Cloned key metadata changed for {channel}")

    metadata_guard = add_correction_layer(
        bpy.context,
        armature,
        name="Metadata Guard",
    )
    _key_scalar_layer(metadata_guard)
    metadata_state = _logical_state(armature)
    del base[PROP_BASE_CHECKSUM]
    _expect_error(
        "checksum metadata",
        lambda: merge_correction_layer_down(
            bpy.context,
            armature,
            metadata_guard.layer_id,
        ),
    )
    if _logical_state(armature) != metadata_state:
        raise AssertionError("Metadata rejection leaked a pending merged Action")
    base[PROP_BASE_CHECKSUM] = base_checksum
    remove_correction_layer(armature, metadata_guard.layer_id)

    overlap = add_correction_layer(bpy.context, armature, name="Overlap")
    _key_quaternion_layer(
        overlap,
        "Hips",
        Quaternion((0.0, 0.0, 1.0), math.radians(18.0)),
    )
    overlap_state = _logical_state(armature)
    overlap_pose = _pose_snapshot(armature)
    _expect_error(
        "overlap",
        lambda: merge_correction_layer_down(
            bpy.context,
            armature,
            overlap.layer_id,
        ),
    )
    if _logical_state(armature) != overlap_state or _pose_error(
        overlap_pose,
        _pose_snapshot(armature),
    ) > 1e-5:
        raise AssertionError("Overlap rejection mutated the Correction stack")
    remove_correction_layer(armature, overlap.layer_id)

    split_lower = add_correction_layer(bpy.context, armature, name="Split X")
    _key_scalar_layer(split_lower, array_index=0, group_name="Split X")
    split_upper = add_correction_layer(bpy.context, armature, name="Split Y")
    _key_scalar_layer(split_upper, array_index=1, group_name="Split Y")
    split_state = _logical_state(armature)
    _expect_error(
        "properties overlap",
        lambda: merge_correction_layer_down(
            bpy.context,
            armature,
            split_upper.layer_id,
        ),
    )
    if _logical_state(armature) != split_state:
        raise AssertionError("Split-property rejection mutated the Correction stack")
    remove_correction_layer(armature, split_upper.layer_id)
    remove_correction_layer(armature, split_lower.layer_id)

    group_lower = add_correction_layer(bpy.context, armature, name="Group Lower")
    _key_scalar_layer(
        group_lower,
        data_path='pose.bones["Arm"].location',
        group_name="Shared Metadata",
    )
    group_upper = add_correction_layer(bpy.context, armature, name="Group Upper")
    _key_scalar_layer(
        group_upper,
        data_path='pose.bones["Arm"].scale',
        group_name="Shared Metadata",
    )
    group_state = _logical_state(armature)
    _expect_error(
        "groups overlap",
        lambda: merge_correction_layer_down(
            bpy.context,
            armature,
            group_upper.layer_id,
        ),
    )
    if _logical_state(armature) != group_state:
        raise AssertionError("Group-overlap rejection mutated the Correction stack")
    remove_correction_layer(armature, group_upper.layer_id)
    remove_correction_layer(armature, group_lower.layer_id)

    partial_quaternion = add_correction_layer(
        bpy.context,
        armature,
        name="Partial Quaternion",
    )
    partial_curve = ensure_fcurve(
        partial_quaternion.access,
        'pose.bones["Arm"].rotation_quaternion',
        0,
        group_name="Arm Partial",
    )
    partial_curve.keyframe_points.insert(FIRST_FRAME, 1.0, options={"FAST"})
    partial_curve.update()
    partial_state = _logical_state(armature)
    _expect_error(
        "must key WXYZ",
        lambda: merge_correction_layer_down(
            bpy.context,
            armature,
            partial_quaternion.layer_id,
        ),
    )
    if _logical_state(armature) != partial_state:
        raise AssertionError("Partial-quaternion rejection mutated the stack")
    remove_correction_layer(armature, partial_quaternion.layer_id)

    quaternion_point_visits = _assert_linear_quaternion_validation()

    active_bound = add_correction_layer(
        bpy.context,
        armature,
        name="Active Binding Guard",
    )
    _key_scalar_layer(active_bound)
    _assign_active_action(armature, active_bound.action)
    active_binding_state = _logical_state(armature)
    active_binding_pose = _pose_snapshot(armature)
    active_slot = getattr(armature.animation_data, "action_slot", None)
    _expect_error(
        "active Action",
        lambda: merge_correction_layer_down(
            bpy.context,
            armature,
            active_bound.layer_id,
        ),
    )
    if (
        _logical_state(armature) != active_binding_state
        or armature.animation_data.action != active_bound.action
        or getattr(armature.animation_data, "action_slot", None) != active_slot
        or _pose_error(active_binding_pose, _pose_snapshot(armature)) > 1e-5
    ):
        raise AssertionError("Active-Action rejection mutated binding or pose")
    _assign_active_action(armature, None)
    remove_correction_layer(armature, active_bound.layer_id)

    unsafe = add_correction_layer(bpy.context, armature, name="Unsafe")
    unsafe_curve = _key_scalar_layer(unsafe)
    modifier = unsafe_curve.modifiers.new("NOISE")
    modifier.strength = 0.1
    modifier_state = _logical_state(armature)
    _expect_error(
        "modifiers",
        lambda: merge_correction_layer_down(
            bpy.context,
            armature,
            unsafe.layer_id,
        ),
    )
    if _logical_state(armature) != modifier_state:
        raise AssertionError("Modifier rejection mutated the Correction stack")
    unsafe_curve.modifiers.remove(modifier)

    unsafe_curve.convert_to_samples(FIRST_FRAME, LAST_FRAME)
    sampled_state = _logical_state(armature)
    _expect_error(
        "sampled points",
        lambda: merge_correction_layer_down(
            bpy.context,
            armature,
            unsafe.layer_id,
        ),
    )
    if _logical_state(armature) != sampled_state:
        raise AssertionError("Sample rejection mutated the Correction stack")
    unsafe_curve.convert_to_keyframes(FIRST_FRAME, LAST_FRAME)

    unsafe.track.name = "User Unsafe Track"
    unsafe.strip.name = "User Unsafe Strip"
    unsafe.strip.select = True
    unsafe.strip.use_sync_length = not bool(unsafe.strip.use_sync_length)
    unsafe.strip.use_animated_time_cyclic = True
    unsafe.strip.strip_time = 4.25
    rollback_edit_state = _nla_edit_state(unsafe)
    before_rollback = _logical_state(armature)
    pose_before_rollback = _pose_snapshot(armature)
    original_validate = merge_module.validate_layer_stack
    validation_calls = 0

    def fail_final_validation(
        obj: bpy.types.Object,
        *,
        check_content: bool = True,
    ) -> LayerStackValidation:
        nonlocal validation_calls
        validation_calls += 1
        result = original_validate(obj, check_content=check_content)
        if validation_calls == 1:
            return LayerStackValidation(("synthetic merge validation failure",))
        return result

    merge_module.validate_layer_stack = fail_final_validation
    try:
        _expect_error(
            "synthetic merge validation failure",
            lambda: merge_correction_layer_down(
                bpy.context,
                armature,
                unsafe.layer_id,
            ),
        )
    finally:
        merge_module.validate_layer_stack = original_validate
    if _logical_state(armature) != before_rollback:
        raise AssertionError("Failed merge did not restore Action/layer state")
    restored_unsafe = next(
        layer
        for layer in list_correction_layers(armature)
        if layer.layer_id == unsafe.layer_id
    )
    if _nla_edit_state(restored_unsafe) != rollback_edit_state:
        raise AssertionError("Failed merge did not restore NLA editing state")
    rollback_pose_error = _pose_error(pose_before_rollback, _pose_snapshot(armature))
    if rollback_pose_error > 1e-5:
        raise AssertionError(
            f"Failed merge did not restore evaluated pose: {rollback_pose_error}"
        )

    # The scalar curve above is intentionally invalid test input for modifier,
    # sample, and rollback rejection. Replace it with a valid sparse pose-bone
    # quaternion before exercising the successful final merge.
    remove_fcurve(restored_unsafe.access, unsafe_curve)
    _key_quaternion_layer(
        restored_unsafe,
        "Arm",
        Quaternion((0.0, 0.0, 1.0), math.radians(7.0)),
    )

    # Safety conditions apply to the adjacent pair only.  A separate muted,
    # fractional-influence layer must stay byte-for-byte logically untouched.
    unrelated = add_correction_layer(bpy.context, armature, name="Unrelated")
    unrelated.action[PROP_LAYER_MUTED] = True
    unrelated.track.mute = True
    unrelated.strip.influence = 0.35
    unrelated_action = unrelated.action.as_pointer()
    unrelated_checksum = action_checksum(unrelated.action)
    unrelated_frame_start = float(unrelated.strip.frame_start)
    unrelated_frame_end = float(unrelated.strip.frame_end)
    unrelated.strip.frame_start = unrelated_frame_start + 5.0
    shifted_state = _raw_binding_state(armature)
    _expect_error(
        "shifted or retimed",
        lambda: merge_correction_layer_down(
            bpy.context,
            armature,
            unsafe.layer_id,
        ),
    )
    if _raw_binding_state(armature) != shifted_state:
        raise AssertionError("Invalid unrelated timing mutated the Correction stack")
    unrelated.strip.frame_start = unrelated_frame_start
    unrelated.strip.frame_end = unrelated_frame_end
    merged_pair = merge_correction_layer_down(
        bpy.context,
        armature,
        unsafe.layer_id,
    )
    unrelated_after = next(
        layer
        for layer in list_correction_layers(armature)
        if layer.layer_id == unrelated.layer_id
    )
    if (
        merged_pair.layer_id != lower_id
        or unrelated_after.action.as_pointer() != unrelated_action
        or action_checksum(unrelated_after.action) != unrelated_checksum
        or not unrelated_after.muted
        or abs(float(unrelated_after.strip.influence) - 0.35) > 1e-6
    ):
        raise AssertionError("Merge changed an unrelated Correction layer")

    bottom = list_correction_layers(armature)[0]
    bottom_state = _logical_state(armature)
    _expect_error(
        "no lower Correction neighbor",
        lambda: merge_correction_layer_down(
            bpy.context,
            armature,
            bottom.layer_id,
        ),
    )
    if _logical_state(armature) != bottom_state:
        raise AssertionError("Lowest-layer rejection mutated the stack")

    if action_checksum(base) != base_checksum:
        raise AssertionError("Layer merge mutated the Base Action")
    if (
        action_checksum(archive) != archive_checksum
        or archive[PROP_ARCHIVE_CHECKSUM] != archive_checksum
    ):
        raise AssertionError("Layer merge mutated the Archive Action")
    validate_layer_stack(armature).raise_for_errors()
    print(
        "MOTION_BRIDGE_LAYER_MERGE_TEST="
        + json.dumps(
            {
                "status": "PASS",
                "stableLowerId": True,
                "sharedActionConsumerBlocked": True,
                "poseErrorDegrees": pose_error,
                "metadataPreserved": True,
                "denseIntegrityScanSkipped": True,
                "mergeWhileBefore": True,
                "overlapRejected": True,
                "propertyOverlapRejected": True,
                "groupOverlapRejected": True,
                "partialQuaternionRejected": True,
                "linearQuaternionPointVisits": quaternion_point_visits,
                "activeActionRejectedBeforeCommit": True,
                "metadataFailureCleanup": True,
                "modifiersRejected": True,
                "samplesRejected": True,
                "shiftedStripRejectedBeforeCommit": True,
                "rollbackPoseErrorDegrees": rollback_pose_error,
                "postCommitFailureRollback": True,
                "rollbackNlaEditState": True,
                "unrelatedLayerStatePreserved": True,
                "baseArchiveImmutable": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
