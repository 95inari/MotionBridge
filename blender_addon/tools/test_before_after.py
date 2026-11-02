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
from motion_bridge.editing.action_api import (  # noqa: E402
    action_slot_for_id,
    insert_quaternion_key,
)
from motion_bridge.editing.bake import (  # noqa: E402
    activate_action_for_export,
    bake_layer_stack,
)
from motion_bridge.editing.comparison import (  # noqa: E402
    PROP_CORRECTIONS_BYPASSED,
    apply_corrections_preview_state,
    configured_after_corrections,
    corrections_preview_enabled,
    set_corrections_preview_enabled,
)
from motion_bridge.editing.correction_layers import (  # noqa: E402
    PROP_LAYER_MUTED,
    PROP_LAYER_SOLO,
    action_checksum,
    add_correction_layer,
    create_correction_layer,
    duplicate_correction_layer,
    get_correction_layer_stack,
    move_correction_layer_down,
    remove_correction_layer,
    rename_correction_layer,
    set_correction_layer_muted,
    set_correction_layer_solo,
    validate_layer_stack,
)
from motion_bridge.io.fbx_export import (  # noqa: E402
    export_corrected_canonical_action,
)
import motion_bridge.editing.comparison as comparison_module  # noqa: E402
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
    data = bpy.data.armatures.new("MB_Before_After_Data")
    armature = bpy.data.objects.new("MB_Before_After", data)
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
    base_rotation = Quaternion((0.0, 0.0, 1.0), math.radians(20.0))
    for frame in (FIRST_FRAME, LAST_FRAME):
        bpy.context.scene.frame_set(frame)
        pose_bone.rotation_quaternion = base_rotation
        pose_bone.keyframe_insert(
            data_path="rotation_quaternion",
            frame=frame,
            group="Hips",
        )
    action = armature.animation_data.action if armature.animation_data else None
    if action is None:
        raise AssertionError("Synthetic Base Action was not created")
    action.name = "MB_Before_After_Base"
    return armature, action


def _key_layer(layer: object, quaternion: Quaternion) -> None:
    for frame in (FIRST_FRAME, LAST_FRAME):
        insert_quaternion_key(layer.access, "Hips", frame, quaternion)


def _evaluated(armature: bpy.types.Object, frame: float = 6.5) -> Quaternion:
    integer = math.floor(frame)
    bpy.context.scene.frame_set(integer, subframe=frame - integer)
    bpy.context.view_layer.update()
    return armature.pose.bones["Hips"].matrix.to_quaternion().normalized()


def _angle(first: Quaternion, second: Quaternion) -> float:
    return math.degrees(first.rotation_difference(second).angle)


def _mutes(armature: bpy.types.Object) -> tuple[bool, ...]:
    stack = get_correction_layer_stack(armature)
    if stack is None:
        raise AssertionError("Correction stack could not be resolved")
    return tuple(layer.track.mute for layer in stack.correction_layers)


def _choices(armature: bpy.types.Object) -> tuple[tuple[str, bool, bool], ...]:
    stack = get_correction_layer_stack(armature)
    if stack is None:
        raise AssertionError("Correction stack could not be resolved")
    return tuple(
        (
            layer.layer_id,
            bool(layer.action.get(PROP_LAYER_MUTED, False)),
            bool(layer.action.get(PROP_LAYER_SOLO, False)),
        )
        for layer in stack.correction_layers
    )


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
    if slot is not None and hasattr(animation_data, "action_slot"):
        animation_data.action_slot = slot


def main() -> None:
    armature, base = _create_base_action()
    stack = create_correction_layer(bpy.context, armature)
    first = stack.active_layer
    second = add_correction_layer(bpy.context, armature, name="Hands")
    third = add_correction_layer(bpy.context, armature, name="Polish")
    _key_layer(first, Quaternion((1.0, 0.0, 0.0), math.radians(30.0)))
    _key_layer(second, Quaternion((0.0, 1.0, 0.0), math.radians(40.0)))
    _key_layer(third, Quaternion((0.0, 0.0, 1.0), math.radians(50.0)))

    archive = stack.archive_action
    base_checksum = action_checksum(base)
    archive_checksum = action_checksum(archive)
    base_track_state = (
        stack.base_track.mute,
        stack.base_strip.mute,
        stack.base_track.lock,
        stack.base_track.is_solo,
        stack.base_strip.blend_type,
    )
    archive_state = (archive.name, archive.use_fake_user, archive.users)

    # Simulate a stack saved before the preview-bypass property existed.
    if PROP_CORRECTIONS_BYPASSED in armature:
        del armature[PROP_CORRECTIONS_BYPASSED]
    if not corrections_preview_enabled(armature):
        raise AssertionError("A missing bypass property must mean After")
    if PROP_CORRECTIONS_BYPASSED in armature:
        raise AssertionError("The legacy preview query unexpectedly persisted a property")

    second.track.mute = True
    second.strip.mute = True
    del second.action[PROP_LAYER_MUTED]
    set_corrections_preview_enabled(armature, False)
    if (
        not bool(second.action[PROP_LAYER_MUTED])
        or not second.track.mute
        or second.strip.mute
    ):
        raise AssertionError("Before did not preserve a legacy layer mute choice")
    set_corrections_preview_enabled(armature, True)
    if _mutes(armature) != (False, True, False):
        raise AssertionError("After did not restore a migrated legacy mute choice")

    set_correction_layer_muted(armature, second.layer_id, True)
    if _mutes(armature) != (False, True, False):
        raise AssertionError("Configured normal layer mutes are incorrect")
    configured_pose = _evaluated(armature)
    configured_choices = _choices(armature)

    original_checksum = correction_module.action_checksum
    original_curve_iterator = correction_module.iter_action_fcurves

    def fail_curve_scan(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("Before/After performed an Action curve scan")

    correction_module.action_checksum = fail_curve_scan
    correction_module.iter_action_fcurves = fail_curve_scan
    try:
        set_corrections_preview_enabled(armature, False)
        if corrections_preview_enabled(armature) or _mutes(armature) != (True, True, True):
            raise AssertionError("Before did not mute every Correction track")
        if _choices(armature) != configured_choices:
            raise AssertionError("Before changed persisted layer choices")
        before_pose = _evaluated(armature)
        set_corrections_preview_enabled(armature, False)
        if _mutes(armature) != (True, True, True):
            raise AssertionError("Repeated Before was not idempotent")
        set_corrections_preview_enabled(armature, True)
    finally:
        correction_module.action_checksum = original_checksum
        correction_module.iter_action_fcurves = original_curve_iterator

    if _mutes(armature) != (False, True, False):
        raise AssertionError("After did not restore configured layer mutes")
    if _angle(configured_pose, _evaluated(armature)) > 1e-4:
        raise AssertionError("After did not restore the configured evaluated pose")
    if _angle(before_pose, configured_pose) < 1.0:
        raise AssertionError("Before and After did not produce distinct poses")

    _assign_active_action(armature, third.action)
    active_binding_mutes = _mutes(armature)
    active_binding_pose = _evaluated(armature)
    active_slot = getattr(armature.animation_data, "action_slot", None)
    try:
        set_corrections_preview_enabled(armature, False)
    except ValueError as error:
        if "active Action" not in str(error):
            raise
    else:
        raise AssertionError("Before accepted an active Action binding")
    if (
        not corrections_preview_enabled(armature)
        or _mutes(armature) != active_binding_mutes
        or armature.animation_data.action != third.action
        or getattr(armature.animation_data, "action_slot", None) != active_slot
        or _angle(active_binding_pose, _evaluated(armature)) > 1e-4
    ):
        raise AssertionError("Active-Action preview rejection changed state or pose")
    _assign_active_action(armature, None)

    set_corrections_preview_enabled(armature, False)
    validate_layer_stack(armature).raise_for_errors()
    baked = bake_layer_stack(bpy.context, armature, name="MB_BEFORE_AFTER_BAKED")
    if corrections_preview_enabled(armature) or _mutes(armature) != (True, True, True):
        raise AssertionError("Visual bake did not restore the persisted Before view")
    with activate_action_for_export(armature, baked):
        baked_error = _angle(configured_pose, _evaluated(armature))
    baked.use_fake_user = False
    bpy.data.actions.remove(baked)
    if baked_error > 1e-4:
        raise AssertionError(f"Before visual bake omitted configured After: {baked_error}")

    export_errors: list[float] = []

    def inspect_export(**_kwargs: object) -> set[str]:
        export_errors.append(_angle(configured_pose, _evaluated(armature)))
        return {"FINISHED"}

    export_corrected_canonical_action(
        bpy.context,
        "/tmp/motion_bridge_before_after.fbx",
        _exporter=inspect_export,
    )
    if len(export_errors) != 1 or export_errors[0] > 1e-4:
        raise AssertionError(f"Before export omitted configured After: {export_errors}")
    if corrections_preview_enabled(armature) or _mutes(armature) != (True, True, True):
        raise AssertionError("Corrected export did not restore the persisted Before view")
    set_corrections_preview_enabled(armature, True)

    set_correction_layer_solo(armature, third.layer_id)
    solo_choices = _choices(armature)
    if _mutes(armature) != (True, True, False):
        raise AssertionError("Solo configuration is incorrect")
    set_corrections_preview_enabled(armature, False)
    if _mutes(armature) != (True, True, True) or _choices(armature) != solo_choices:
        raise AssertionError("Before changed a solo configuration")
    set_corrections_preview_enabled(armature, True)
    if _mutes(armature) != (True, True, False) or _choices(armature) != solo_choices:
        raise AssertionError("After did not restore solo effective mutes")

    set_corrections_preview_enabled(armature, False)
    try:
        with configured_after_corrections(armature):
            if _mutes(armature) != (True, True, False):
                raise AssertionError("Bake helper did not temporarily apply After")
            if corrections_preview_enabled(armature):
                raise AssertionError("Bake helper changed the persisted bypass mode")
            armature[PROP_CORRECTIONS_BYPASSED] = False
            raise RuntimeError("synthetic bake failure")
    except RuntimeError as error:
        if str(error) != "synthetic bake failure":
            raise
    else:
        raise AssertionError("Synthetic bake failure did not propagate")
    if _mutes(armature) != (True, True, True):
        raise AssertionError("Bake helper did not restore exact Before mutes")
    if corrections_preview_enabled(armature):
        raise AssertionError("Bake helper did not restore the bypass property")

    before_failure_mutes = _mutes(armature)
    before_failure_property = armature[PROP_CORRECTIONS_BYPASSED]
    original_assign = comparison_module._assign_layer_mute
    assignment_count = 0

    def fail_second_assignment(layer: object, muted: bool) -> None:
        nonlocal assignment_count
        assignment_count += 1
        if assignment_count == 2:
            raise RuntimeError("synthetic comparison failure")
        original_assign(layer, muted)

    comparison_module._assign_layer_mute = fail_second_assignment
    try:
        try:
            set_corrections_preview_enabled(armature, True)
        except RuntimeError as error:
            if str(error) != "synthetic comparison failure":
                raise
        else:
            raise AssertionError("Synthetic comparison failure did not propagate")
    finally:
        comparison_module._assign_layer_mute = original_assign
    if _mutes(armature) != before_failure_mutes:
        raise AssertionError("Failed comparison did not roll back all track mutes")
    if armature[PROP_CORRECTIONS_BYPASSED] != before_failure_property:
        raise AssertionError("Failed comparison did not roll back its property")

    set_corrections_preview_enabled(armature, True)
    set_correction_layer_solo(armature, None)
    set_corrections_preview_enabled(armature, False)
    set_correction_layer_muted(armature, first.layer_id, True)
    set_correction_layer_solo(armature, third.layer_id)
    if not all(_mutes(armature)):
        raise AssertionError("Mute/solo mutation escaped the Before bypass")
    set_correction_layer_solo(armature, None)
    set_correction_layer_muted(armature, first.layer_id, False)
    added = add_correction_layer(bpy.context, armature, name="Added In Before")
    if not added.track.mute:
        raise AssertionError("A new layer escaped the persisted Before bypass")
    duplicated = duplicate_correction_layer(
        bpy.context,
        armature,
        first.layer_id,
        name="Duplicated In Before",
    )
    duplicated = rename_correction_layer(
        armature,
        duplicated.layer_id,
        "Renamed In Before",
    )
    move_correction_layer_down(armature, duplicated.layer_id)
    if not duplicated.track.mute or not all(_mutes(armature)):
        raise AssertionError("Duplicate/rename/move escaped the Before bypass")
    apply_corrections_preview_state(armature)
    if not all(_mutes(armature)):
        raise AssertionError("Preview refresh did not include a newly added layer")
    remove_correction_layer(armature, duplicated.layer_id)
    remove_correction_layer(armature, added.layer_id)
    apply_corrections_preview_state(armature)
    if len(_mutes(armature)) != 3 or not all(_mutes(armature)):
        raise AssertionError("Preview refresh failed after layer removal")
    if corrections_preview_enabled(armature):
        raise AssertionError("Layer CRUD changed the persisted Before mode")

    set_corrections_preview_enabled(armature, True)
    if _mutes(armature) != (False, True, False):
        raise AssertionError("Final After state did not restore normal mute choices")
    if action_checksum(base) != base_checksum or action_checksum(archive) != archive_checksum:
        raise AssertionError("Before/After modified Base or Archive Action content")
    if base_track_state != (
        stack.base_track.mute,
        stack.base_strip.mute,
        stack.base_track.lock,
        stack.base_track.is_solo,
        stack.base_strip.blend_type,
    ):
        raise AssertionError("Before/After modified the Base track")
    if archive_state != (archive.name, archive.use_fake_user, archive.users):
        raise AssertionError("Before/After modified the Archive Action")

    print(
        json.dumps(
            {
                "beforeAfter": "PASS",
                "legacyDefaultsToAfter": True,
                "normalMutes": True,
                "activeActionRejected": True,
                "solo": True,
                "transactional": True,
                "bakeStateRestoration": True,
                "beforeBakeUsesConfiguredAfter": True,
                "beforeExportUsesConfiguredAfter": True,
                "crudRefresh": True,
                "mutationsWhileBefore": True,
                "curveScans": 0,
                "baseArchiveUnchanged": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
