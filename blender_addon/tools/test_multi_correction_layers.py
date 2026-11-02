from __future__ import annotations

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
from motion_bridge.editing import (  # noqa: E402
    activate_action_for_export,
    add_correction_layer,
    bake_layer_stack,
    create_correction_layer,
    duplicate_correction_layer,
    list_correction_layers,
    move_correction_layer_down,
    move_correction_layer_up,
    remove_correction_layer,
    rename_correction_layer,
    set_active_correction_layer,
    set_correction_layer_muted,
    set_correction_layer_solo,
    validate_layer_stack,
)
from motion_bridge.editing.action_api import (  # noqa: E402
    insert_quaternion_key,
    iter_action_fcurves,
)
from motion_bridge.editing.correction_layers import (  # noqa: E402
    PROP_ACTIVE_LAYER_ID,
    PROP_ARCHIVE_CHECKSUM,
    PROP_LAYER_ID,
    PROP_LAYER_MUTED,
    PROP_LAYER_NAME,
    PROP_LAYER_SOLO,
    LayerStackValidation,
    action_checksum,
)
from motion_bridge.io.fbx_export import (  # noqa: E402
    export_corrected_canonical_action,
)
import motion_bridge.editing.correction_layers as correction_module  # noqa: E402


FIRST_FRAME = 1
LAST_FRAME = 12


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for action in tuple(bpy.data.actions):
        bpy.data.actions.remove(action)


def _create_base_action() -> tuple[bpy.types.Object, bpy.types.Action]:
    _clear_scene()
    data = bpy.data.armatures.new("MB_Multi_Correction_Data")
    armature = bpy.data.objects.new("MB_Multi_Correction", data)
    bpy.context.scene.collection.objects.link(armature)
    armature["motion_bridge_skeleton_id"] = SKELETON_ID
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode="EDIT")
    bone = data.edit_bones.new("Hips")
    bone.head = (0.0, 0.0, 0.0)
    bone.tail = (0.0, 0.0, 1.0)
    bpy.ops.object.mode_set(mode="POSE")
    pose_bone = armature.pose.bones["Hips"]
    pose_bone.rotation_mode = "QUATERNION"
    base = Quaternion((0.0, 0.0, 1.0), math.radians(25.0))
    for frame in (FIRST_FRAME, LAST_FRAME):
        bpy.context.scene.frame_set(frame)
        pose_bone.rotation_quaternion = base
        pose_bone.keyframe_insert(
            data_path="rotation_quaternion",
            frame=frame,
            group="Hips",
        )
    action = armature.animation_data.action if armature.animation_data else None
    if action is None:
        raise AssertionError("Synthetic Base Action was not created")
    action.name = "MB_Multi_Base"
    return armature, action


def _key_layer(layer: object, quaternion: Quaternion) -> None:
    access = layer.access
    for frame in (FIRST_FRAME, LAST_FRAME):
        insert_quaternion_key(access, "Hips", frame, quaternion)


def _evaluated(armature: bpy.types.Object, frame: float = 7.25) -> Quaternion:
    integer = math.floor(frame)
    bpy.context.scene.frame_set(integer, subframe=frame - integer)
    bpy.context.view_layer.update()
    return armature.pose.bones["Hips"].matrix.to_quaternion().normalized()


def _angle(first: Quaternion, second: Quaternion) -> float:
    return math.degrees(first.rotation_difference(second).angle)


def _layer_ids(armature: bpy.types.Object) -> tuple[str, ...]:
    return tuple(layer.layer_id for layer in list_correction_layers(armature))


def _assert_stack_order(armature: bpy.types.Object) -> None:
    validation = validate_layer_stack(armature)
    validation.raise_for_errors()
    assert validation.stack is not None
    tracks = tuple(armature.animation_data.nla_tracks)
    expected = (validation.stack.base_track,) + tuple(
        layer.track for layer in validation.stack.correction_layers
    )
    if tracks != expected:
        raise AssertionError("NLA track order does not match Correction layer order")


def main() -> None:
    armature, base = _create_base_action()
    stack = create_correction_layer(bpy.context, armature)
    archive = stack.archive_action
    base_checksum = action_checksum(base)
    archive_checksum = action_checksum(archive)
    stack.correction_track.mute = True
    stack.correction_strip.mute = True
    for key in (PROP_LAYER_ID, PROP_LAYER_NAME, PROP_LAYER_MUTED, PROP_LAYER_SOLO):
        del stack.correction_action[key]
    del armature[PROP_ACTIVE_LAYER_ID]
    stack = create_correction_layer(bpy.context, armature)
    upgraded_layer_id = stack.active_layer_id
    if (
        stack.correction_action[PROP_LAYER_ID] != upgraded_layer_id
        or create_correction_layer(bpy.context, armature).active_layer_id
        != upgraded_layer_id
        or not bool(stack.correction_action[PROP_LAYER_MUTED])
        or not stack.correction_track.mute
        or stack.correction_strip.mute
    ):
        raise AssertionError("Legacy single-layer metadata upgrade is not stable")
    set_correction_layer_muted(armature, upgraded_layer_id, False)
    first = stack.active_layer
    _key_layer(
        first,
        Quaternion((1.0, 0.0, 0.0), math.radians(45.0)),
    )

    second = add_correction_layer(bpy.context, armature, name="Hands")
    if any(iter_action_fcurves(second.action)):
        raise AssertionError("New Correction layer is not sparse/empty")
    _key_layer(
        second,
        Quaternion((0.0, 1.0, 0.0), math.radians(55.0)),
    )
    second = rename_correction_layer(armature, second.layer_id, "Upper Body")
    duplicate = duplicate_correction_layer(
        bpy.context,
        armature,
        first.layer_id,
        name="Secondary",
    )
    if action_checksum(duplicate.action) != action_checksum(first.action):
        raise AssertionError("Duplicated Correction Action differs from its source")
    if len(set(_layer_ids(armature))) != 3:
        raise AssertionError("Correction layer ids are not stable and unique")
    if set_active_correction_layer(armature, second.layer_id).layer_id != second.layer_id:
        raise AssertionError("Active layer selection did not use its stable id")
    resolved = validate_layer_stack(armature).stack
    if resolved is None or resolved.correction_action is not second.action:
        raise AssertionError("Legacy active correction compatibility fields drifted")

    original_checksum_function = correction_module.action_checksum

    def fail_dense_integrity_scan(_action: bpy.types.Action) -> str:
        raise AssertionError("Correction CRUD performed a dense Action integrity scan")

    correction_module.action_checksum = fail_dense_integrity_scan
    try:
        list_correction_layers(armature)
        set_active_correction_layer(armature, first.layer_id)
        set_correction_layer_muted(armature, first.layer_id, True)
        set_correction_layer_muted(armature, first.layer_id, False)
    finally:
        correction_module.action_checksum = original_checksum_function

    before_move_ids = _layer_ids(armature)
    before_move_pose = _evaluated(armature)
    move_correction_layer_down(armature, second.layer_id)
    after_move_ids = _layer_ids(armature)
    after_move_pose = _evaluated(armature)
    if before_move_ids == after_move_ids or _angle(before_move_pose, after_move_pose) < 1.0:
        raise AssertionError("Layer move did not change deterministic evaluation order")
    move_correction_layer_up(armature, second.layer_id)
    if _layer_ids(armature) != before_move_ids:
        raise AssertionError("Move up/down did not restore stable layer order")
    if _angle(before_move_pose, _evaluated(armature)) > 1e-4:
        raise AssertionError("Restored layer order changed the evaluated pose")
    _assert_stack_order(armature)

    set_correction_layer_muted(armature, first.layer_id, True)
    set_correction_layer_muted(armature, duplicate.layer_id, True)
    expected_solo = _evaluated(armature)
    set_correction_layer_muted(armature, first.layer_id, False)
    set_correction_layer_muted(armature, duplicate.layer_id, False)
    set_correction_layer_solo(armature, second.layer_id)
    if _angle(expected_solo, _evaluated(armature)) > 1e-4:
        raise AssertionError("Solo did not evaluate Base plus only the selected Correction")
    set_correction_layer_solo(armature, None)
    if _angle(before_move_pose, _evaluated(armature)) > 1e-4:
        raise AssertionError("Clearing solo did not restore all Correction layers")

    set_correction_layer_solo(armature, second.layer_id)
    added_while_solo = add_correction_layer(
        bpy.context,
        armature,
        name="Added While Solo",
    )
    duplicated_while_solo = duplicate_correction_layer(
        bpy.context,
        armature,
        first.layer_id,
        name="Duplicated While Solo",
    )
    if not added_while_solo.track.mute or not duplicated_while_solo.track.mute:
        raise AssertionError("A layer added during solo became audible")
    remove_correction_layer(armature, added_while_solo.layer_id)
    remove_correction_layer(armature, duplicated_while_solo.layer_id)
    set_correction_layer_solo(armature, None)

    combined = _evaluated(armature)
    baked = bake_layer_stack(bpy.context, armature, name="MB_MULTI_BAKED")
    with activate_action_for_export(armature, baked):
        baked_error = _angle(combined, _evaluated(armature))
    if baked_error > 1e-4:
        raise AssertionError(f"Multi-layer visual bake error: {baked_error}")
    baked.use_fake_user = False
    bpy.data.actions.remove(baked)

    exported_errors: list[float] = []

    def inspect_export(**_kwargs: object) -> set[str]:
        exported_errors.append(_angle(combined, _evaluated(armature)))
        return {"FINISHED"}

    export_corrected_canonical_action(
        bpy.context,
        "/tmp/motion_bridge_multi_correction.fbx",
        _exporter=inspect_export,
    )
    if len(exported_errors) != 1 or exported_errors[0] > 1e-4:
        raise AssertionError(f"Corrected export omitted a layer: {exported_errors}")
    if any(action.name.startswith("MB_BAKED::") for action in bpy.data.actions):
        raise AssertionError("Corrected export leaked its temporary visual bake")

    before_failure_ids = _layer_ids(armature)
    before_failure_actions = {action.as_pointer() for action in bpy.data.actions}
    before_failure_tracks = len(armature.animation_data.nla_tracks)
    before_failure_track_selection = tuple(
        bool(track.select) for track in armature.animation_data.nla_tracks
    )
    original_configure = correction_module._configure_strip

    def fail_configure(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic add failure")

    correction_module._configure_strip = fail_configure
    try:
        try:
            add_correction_layer(bpy.context, armature, name="Must Roll Back")
        except RuntimeError as error:
            if str(error) != "synthetic add failure":
                raise
        else:
            raise AssertionError("Synthetic layer creation failure did not propagate")
    finally:
        correction_module._configure_strip = original_configure
    if (
        _layer_ids(armature) != before_failure_ids
        or {action.as_pointer() for action in bpy.data.actions} != before_failure_actions
        or len(armature.animation_data.nla_tracks) != before_failure_tracks
        or tuple(
            bool(track.select) for track in armature.animation_data.nla_tracks
        )
        != before_failure_track_selection
    ):
        raise AssertionError("Failed layer creation leaked Action/track state")

    original_validate = correction_module.validate_layer_stack
    validation_calls = 0

    def fail_after_remove(
        obj: bpy.types.Object,
        *,
        check_content: bool = True,
    ) -> LayerStackValidation:
        nonlocal validation_calls
        validation_calls += 1
        result = original_validate(obj, check_content=check_content)
        if validation_calls == 2:
            return LayerStackValidation(("synthetic remove validation failure",))
        return result

    second.strip.select = True
    second.strip.use_sync_length = not bool(second.strip.use_sync_length)
    second.strip.use_animated_time_cyclic = True
    second.strip.strip_time = 4.75
    before_remove_edit_state = (
        bool(second.strip.select),
        bool(second.strip.use_sync_length),
        bool(second.strip.use_animated_time_cyclic),
        float(second.strip.strip_time),
    )
    before_remove_ids = _layer_ids(armature)
    before_remove_actions = tuple(
        layer.action.as_pointer() for layer in list_correction_layers(armature)
    )
    correction_module.validate_layer_stack = fail_after_remove
    try:
        try:
            remove_correction_layer(armature, second.layer_id)
        except ValueError as error:
            if "synthetic remove validation failure" not in str(error):
                raise
        else:
            raise AssertionError("Synthetic remove validation failure did not propagate")
    finally:
        correction_module.validate_layer_stack = original_validate
    if _layer_ids(armature) != before_remove_ids or tuple(
        layer.action.as_pointer() for layer in list_correction_layers(armature)
    ) != before_remove_actions:
        raise AssertionError("Failed layer removal did not restore content/order")
    restored_second = next(
        layer
        for layer in list_correction_layers(armature)
        if layer.layer_id == second.layer_id
    )
    if (
        bool(restored_second.strip.select),
        bool(restored_second.strip.use_sync_length),
        bool(restored_second.strip.use_animated_time_cyclic),
        float(restored_second.strip.strip_time),
    ) != before_remove_edit_state:
        raise AssertionError("Failed layer removal did not restore NLA editing state")
    _assert_stack_order(armature)

    set_correction_layer_muted(armature, first.layer_id, True)
    set_correction_layer_solo(armature, second.layer_id)
    survivor = remove_correction_layer(armature, second.layer_id)
    remaining = list_correction_layers(armature)
    if any(layer.solo for layer in remaining):
        raise AssertionError("Removing the solo layer left stale solo state")
    first_after = next(layer for layer in remaining if layer.layer_id == first.layer_id)
    if not first_after.muted or not bool(first_after.action[PROP_LAYER_MUTED]):
        raise AssertionError("Removing solo layer did not restore persisted mute state")
    if survivor.layer_id not in _layer_ids(armature):
        raise AssertionError("Layer removal did not select a surviving active layer")
    remove_correction_layer(armature, first.layer_id)
    try:
        remove_correction_layer(armature, duplicate.layer_id)
    except ValueError as error:
        if "retain at least one" not in str(error):
            raise
    else:
        raise AssertionError("Final Correction layer could be removed")

    if action_checksum(base) != base_checksum:
        raise AssertionError("Correction CRUD mutated the Base Action")
    if (
        action_checksum(archive) != archive_checksum
        or archive[PROP_ARCHIVE_CHECKSUM] != archive_checksum
    ):
        raise AssertionError("Correction CRUD mutated the Archive Action")
    validate_layer_stack(armature).raise_for_errors()
    print(
        "MOTION_BRIDGE_MULTI_CORRECTION_TEST="
        + json.dumps(
            {
                "status": "PASS",
                "stableIds": True,
                "legacySingleLayerUpgrade": True,
                "deterministicOrder": True,
                "moveChangesEvaluation": True,
                "soloKeepsBase": True,
                "allLayersVisualBaked": True,
                "bakedPoseErrorDegrees": baked_error,
                "allLayersExported": True,
                "addFailureCleanup": True,
                "removeFailureRollback": True,
                "soloRemovalRestoresMute": True,
                "soloAddDuplicateSafe": True,
                "crudSkipsDenseIntegrityScan": True,
                "baseArchiveImmutable": True,
                "retainsOneLayer": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
