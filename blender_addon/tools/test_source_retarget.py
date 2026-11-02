from __future__ import annotations

import json
import math
from pathlib import Path
import sys

import bpy
from mathutils import Matrix, Quaternion, Vector


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))

from motion_bridge.canonical.rig_spec import JOINTS, SKELETON_ID  # noqa: E402
from motion_bridge import addon  # noqa: E402
from motion_bridge.editing.action_api import (  # noqa: E402
    action_slot_for_id,
    create_action_for_id,
    iter_action_fcurves,
)
from motion_bridge.retargeting import (  # noqa: E402
    RETARGET_BAKED_ROLE,
    bake_source_to_canonical,
    validate_humanoid_mapping,
)


FIRST_FRAME = 1
LAST_FRAME = 5
SOURCE_SCALE = 2.0
SOURCE_HIPS_DELTA = Vector((0.4, -0.2, 0.3))
SOURCE_ANCHOR_LOCATION = Vector((3.0, -4.0, 2.0))
SOURCE_ANCHOR_ROTATION = Quaternion((0.0, 0.0, 1.0), math.radians(31.0))
ROOT_DELTA_LOCATION = Vector((1.2, -0.6, 0.4))
ROOT_DELTA_ROTATION = Quaternion((0.0, 0.0, 1.0), math.radians(47.0))
ARM_DELTA = Quaternion(Vector((0.3, 0.8, 0.4)).normalized(), math.radians(73.0))


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for action in tuple(bpy.data.actions):
        bpy.data.actions.remove(action)


def _roll_reference(head: Vector, tail: Vector) -> Vector:
    direction = (tail - head).normalized()
    reference = Vector((0.0, -1.0, 0.0))
    if abs(direction.dot(reference)) > 0.98:
        return Vector((0.0, 0.0, 1.0))
    return reference


def _create_armature(
    name: str,
    *,
    scale: float,
    source: bool,
) -> tuple[bpy.types.Object, dict[str, str]]:
    armature_data = bpy.data.armatures.new(f"{name}_Data")
    armature = bpy.data.objects.new(name, armature_data)
    bpy.context.scene.collection.objects.link(armature)
    if not source:
        armature["motion_bridge_skeleton_id"] = SKELETON_ID

    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    edit_bones: dict[str, bpy.types.EditBone] = {}
    mapping: dict[str, str] = {}
    for joint in JOINTS[1:]:
        assert joint.head is not None and joint.tail is not None
        bone_name = f"SRC::{joint.human_bone}" if source else joint.name
        bone = armature_data.edit_bones.new(bone_name)
        bone.head = Vector(joint.head) * scale
        bone.tail = Vector(joint.tail) * scale
        if joint.parent_index > 0:
            bone.parent = edit_bones[JOINTS[joint.parent_index].name]
        bone.align_roll(_roll_reference(bone.head, bone.tail))
        if source:
            # Roll differs per bone while heads/tails and Human hierarchy stay valid.
            bone.roll += math.radians(19.0 + joint.index * 7.0)
        edit_bones[joint.name] = bone
        assert joint.human_bone is not None
        mapping[joint.human_bone] = bone_name
    bpy.ops.object.mode_set(mode="OBJECT")
    for pose_bone in armature.pose.bones:
        pose_bone.rotation_mode = "QUATERNION"
    armature.select_set(False)
    return armature, mapping


def _key_source_motion(
    source: bpy.types.Object,
    mapping: dict[str, str],
) -> bpy.types.Action:
    bpy.context.view_layer.objects.active = source
    source.select_set(True)
    hips_name = mapping["Hips"]
    arm_name = mapping["LeftUpperArm"]
    for frame, factor in ((FIRST_FRAME, 0.0), (LAST_FRAME, 1.0)):
        bpy.context.scene.frame_set(frame)
        root_delta_rotation = Quaternion((1.0, 0.0, 0.0, 0.0)).slerp(
            ROOT_DELTA_ROTATION,
            factor,
        )
        source.location = (
            SOURCE_ANCHOR_LOCATION
            + SOURCE_ANCHOR_ROTATION @ (ROOT_DELTA_LOCATION * factor)
        )
        source.rotation_quaternion = SOURCE_ANCHOR_ROTATION @ root_delta_rotation
        source.keyframe_insert(data_path="location", frame=frame, group="Root")
        source.keyframe_insert(
            data_path="rotation_quaternion",
            frame=frame,
            group="Root",
        )
        for pose_bone in source.pose.bones:
            pose_bone.matrix_basis = Matrix.Identity(4)

        hips = source.pose.bones[hips_name]
        hips_rest = source.data.bones[hips_name].matrix_local
        hips.matrix = Matrix.LocRotScale(
            hips_rest.translation + SOURCE_HIPS_DELTA * factor,
            hips_rest.to_quaternion(),
            Vector((1.0, 1.0, 1.0)),
        )
        hips.keyframe_insert(data_path="location", frame=frame, group="Hips")

        arm = source.pose.bones[arm_name]
        arm_rest = source.data.bones[arm_name].matrix_local
        delta = Quaternion((1.0, 0.0, 0.0, 0.0)).slerp(ARM_DELTA, factor)
        arm.matrix = Matrix.LocRotScale(
            arm.matrix.translation,
            delta @ arm_rest.to_quaternion(),
            Vector((1.0, 1.0, 1.0)),
        )
        arm.keyframe_insert(
            data_path="rotation_quaternion",
            frame=frame,
            group="LeftUpperArm",
        )

    if source.animation_data is None or source.animation_data.action is None:
        raise AssertionError("Synthetic source Action was not created")
    source.animation_data.action.name = "MB_Source_Original"
    return source.animation_data.action


def _action_signature(action: bpy.types.Action) -> tuple[object, ...]:
    return tuple(
        (
            curve.data_path,
            curve.array_index,
            tuple(
                (float(point.co[0]), float(point.co[1]))
                for point in curve.keyframe_points
            ),
        )
        for curve in iter_action_fcurves(action)
    )


def _assign_action(obj: bpy.types.Object, action: bpy.types.Action) -> None:
    animation_data = obj.animation_data_create()
    animation_data.action = action
    slot = action_slot_for_id(action, obj)
    if slot is not None:
        if hasattr(animation_data, "action_slot"):
            animation_data.action_slot = slot
        else:
            animation_data.action_slot_handle = slot.handle


def _editor_signature(target: bpy.types.Object) -> tuple[object, ...]:
    active = bpy.context.view_layer.objects.active
    return (
        active,
        tuple(sorted(obj.name for obj in bpy.context.selected_objects)),
        active.mode if active is not None else None,
        bpy.context.scene.frame_start,
        bpy.context.scene.frame_end,
        bpy.context.scene.frame_current,
        bpy.context.scene.frame_subframe,
        target.animation_data.action if target.animation_data is not None else None,
        target.rotation_mode,
        tuple(
            round(float(target.matrix_basis[row][column]), 6)
            for row in range(4)
            for column in range(4)
        ),
    )


def _angular_error(first: Quaternion, second: Quaternion) -> float:
    return math.degrees(first.rotation_difference(second).angle)


def _assert_quaternion_curves(action: bpy.types.Action) -> None:
    for joint in JOINTS[1:]:
        path = f'pose.bones["{joint.name}"].rotation_quaternion'
        curves = {
            curve.array_index: curve
            for curve in iter_action_fcurves(action)
            if curve.data_path == path
        }
        if set(curves) != {0, 1, 2, 3}:
            raise AssertionError(f"Incomplete quaternion channels: {joint.name}")
        previous: tuple[float, float, float, float] | None = None
        for key_index in range(LAST_FRAME - FIRST_FRAME + 1):
            values = tuple(
                float(curves[index].keyframe_points[key_index].co[1])
                for index in range(4)
            )
            norm = math.sqrt(sum(value * value for value in values))
            if abs(norm - 1.0) > 1e-5:
                raise AssertionError(f"Non-unit quaternion for {joint.name}: {norm}")
            if previous is not None and sum(a * b for a, b in zip(previous, values)) < 0:
                raise AssertionError(f"Quaternion hemisphere flip for {joint.name}")
            previous = values
    root_curves = {
        curve.array_index: curve
        for curve in iter_action_fcurves(action)
        if curve.data_path == "rotation_quaternion"
    }
    if set(root_curves) != {0, 1, 2, 3}:
        raise AssertionError("Incomplete object trajectory quaternion channels")
    root_location_curves = {
        curve.array_index: curve
        for curve in iter_action_fcurves(action)
        if curve.data_path == "location"
    }
    if set(root_location_curves) != {0, 1, 2}:
        raise AssertionError("Incomplete object trajectory location channels")
    if any(
        len(curve.keyframe_points) != LAST_FRAME - FIRST_FRAME + 1
        for curve in root_location_curves.values()
    ):
        raise AssertionError("Object trajectory does not contain one key per frame")
    if any(
        abs(float(curve.keyframe_points[0].co[1])) > 1e-6
        for curve in root_location_curves.values()
    ):
        raise AssertionError("Object trajectory does not start at the session anchor")
    previous_root: tuple[float, float, float, float] | None = None
    for key_index in range(LAST_FRAME - FIRST_FRAME + 1):
        values = tuple(
            float(root_curves[index].keyframe_points[key_index].co[1])
            for index in range(4)
        )
        norm = math.sqrt(sum(value * value for value in values))
        if abs(norm - 1.0) > 1e-5:
            raise AssertionError(f"Non-unit object quaternion: {norm}")
        if previous_root is not None and sum(
            a * b for a, b in zip(previous_root, values)
        ) < 0:
            raise AssertionError("Object trajectory quaternion changed hemisphere")
        previous_root = values
    first_root = tuple(
        float(root_curves[index].keyframe_points[0].co[1]) for index in range(4)
    )
    if abs(first_root[0] - 1.0) > 1e-6 or any(
        abs(value) > 1e-6 for value in first_root[1:]
    ):
        raise AssertionError("Object rotation does not start at the session anchor")


def _expect_validation_error(
    source: bpy.types.Object,
    target: bpy.types.Object,
    mapping: dict[str, str],
    message: str,
) -> None:
    try:
        validate_humanoid_mapping(source, target, mapping)
    except ValueError as error:
        if message not in str(error):
            raise AssertionError(f"Unexpected validation error: {error}") from error
    else:
        raise AssertionError(f"Expected validation error containing {message!r}")


def main() -> None:
    _clear_scene()
    target, _target_mapping = _create_armature(
        "MB_Canonical_Retarget_Target",
        scale=1.0,
        source=False,
    )
    source, mapping = _create_armature(
        "MB_Arbitrary_Source",
        scale=SOURCE_SCALE,
        source=True,
    )
    source.rotation_mode = "QUATERNION"
    source.rotation_quaternion = SOURCE_ANCHOR_ROTATION
    source.location = SOURCE_ANCHOR_LOCATION
    source_action = _key_source_motion(source, mapping)
    source_signature = _action_signature(source_action)

    missing = dict(mapping)
    del missing["Head"]
    _expect_validation_error(source, target, missing, "Missing required")
    duplicate = dict(mapping)
    duplicate["RightHand"] = duplicate["LeftHand"]
    _expect_validation_error(source, target, duplicate, "mapped more than once")
    crossed = dict(mapping)
    crossed["LeftUpperArm"], crossed["RightUpperArm"] = (
        crossed["RightUpperArm"],
        crossed["LeftUpperArm"],
    )
    _expect_validation_error(source, target, crossed, "Hierarchy mismatch")

    required_humans = {
        joint.human_bone
        for joint in JOINTS
        if joint.required and joint.human_bone is not None
    }
    required_mapping = {
        human_bone: source_name
        for human_bone, source_name in mapping.items()
        if human_bone in required_humans
    }
    plan = validate_humanoid_mapping(source, target, required_mapping)
    if abs(plan.height_scale - 0.5) > 1e-6:
        raise AssertionError(f"Unexpected height ratio: {plan.height_scale}")

    preexisting_access = create_action_for_id(
        "MB_Target_Preexisting",
        target,
        frame_start=9,
        frame_end=10,
    )
    _assign_action(target, preexisting_access.action)
    companion = bpy.data.objects.new("MB_Preexisting_Selection", None)
    bpy.context.scene.collection.objects.link(companion)
    source.select_set(True)
    companion.select_set(True)
    bpy.context.view_layer.objects.active = source
    bpy.ops.object.mode_set(mode="POSE")
    bpy.context.scene.frame_start = -7
    bpy.context.scene.frame_end = 37
    bpy.context.scene.frame_set(3, subframe=0.25)
    expected_editor = _editor_signature(target)

    baked = bake_source_to_canonical(
        bpy.context,
        source,
        target,
        required_mapping,
        frame_start=FIRST_FRAME,
        frame_end=LAST_FRAME,
    )
    if _editor_signature(target) != expected_editor:
        raise AssertionError("Editor selection/mode/timeline/Action was not restored")
    if _action_signature(source_action) != source_signature:
        raise AssertionError("Source Action was modified by retarget bake")
    if baked.get("motion_bridge_role") != RETARGET_BAKED_ROLE:
        raise AssertionError("Retarget Action role metadata is missing")
    if target.animation_data is None or target.animation_data.action is not preexisting_access.action:
        raise AssertionError("Target active Action was not restored")
    _assert_quaternion_curves(baked)

    if source.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    _assign_action(target, baked)
    target.rotation_mode = "QUATERNION"
    for pose_bone in target.pose.bones:
        pose_bone.rotation_mode = "QUATERNION"
    bpy.context.scene.frame_set(LAST_FRAME)
    bpy.context.view_layer.update()
    target_rest = target.data.bones["LeftUpperArm"].matrix_local.to_quaternion()
    target_pose = target.pose.bones["LeftUpperArm"].matrix.to_quaternion()
    actual_delta = (target_pose @ target_rest.inverted()).normalized()
    expected_delta = (
        SOURCE_ANCHOR_ROTATION @ ARM_DELTA @ SOURCE_ANCHOR_ROTATION.inverted()
    ).normalized()
    rotation_error = _angular_error(expected_delta, actual_delta)
    if rotation_error > 1e-4:
        raise AssertionError(f"Global rest-relative rotation error: {rotation_error}")

    hips_rest = target.data.bones["Hips"].matrix_local.translation
    hips_pose = target.pose.bones["Hips"].matrix.translation
    actual_hips_delta = hips_pose - hips_rest
    expected_hips_delta = SOURCE_ANCHOR_ROTATION @ SOURCE_HIPS_DELTA * 0.5
    hips_error = (actual_hips_delta - expected_hips_delta).length
    if hips_error > 1e-5:
        raise AssertionError(f"Height-scaled Hips error: {hips_error}")

    root_location_error = (
        target.location - ROOT_DELTA_LOCATION * plan.height_scale
    ).length
    root_rotation_error = _angular_error(
        target.rotation_quaternion.normalized(),
        ROOT_DELTA_ROTATION,
    )
    if root_location_error > 1e-5 or root_rotation_error > 1e-4:
        raise AssertionError(
            "Session-relative object trajectory error: "
            f"location={root_location_error}, rotation={root_rotation_error}"
        )
    frame_end_source_rotation = (
        SOURCE_ANCHOR_ROTATION @ ROOT_DELTA_ROTATION
    ).normalized()
    double_mixed_delta = (
        frame_end_source_rotation
        @ ARM_DELTA
        @ frame_end_source_rotation.inverted()
    ).normalized()
    double_mix_separation = _angular_error(actual_delta, double_mixed_delta)
    if double_mix_separation < 1.0:
        raise AssertionError("Animated object yaw leaked into the limb delta")

    _assign_action(target, preexisting_access.action)
    bpy.context.scene.frame_start = -7
    bpy.context.scene.frame_end = 37
    bpy.context.scene.frame_set(3, subframe=0.25)
    source.select_set(True)
    companion.select_set(True)
    bpy.context.view_layer.objects.active = source
    bpy.ops.object.mode_set(mode="POSE")
    expected_failure_editor = _editor_signature(target)
    actions_before = {action.as_pointer() for action in bpy.data.actions}

    def fail_on_frame(frame: int) -> None:
        if frame == 3:
            raise RuntimeError("synthetic retarget failure")

    try:
        bake_source_to_canonical(
            bpy.context,
            source,
            target,
            required_mapping,
            frame_start=FIRST_FRAME,
            frame_end=LAST_FRAME,
            name="MB_RETARGET_MUST_BE_REMOVED",
            _on_frame=fail_on_frame,
        )
    except RuntimeError as error:
        if "synthetic retarget failure" not in str(error):
            raise
    else:
        raise AssertionError("Synthetic retarget failure did not propagate")
    if {action.as_pointer() for action in bpy.data.actions} != actions_before:
        raise AssertionError("Failed retarget bake leaked a temporary Action")
    actual_failure_editor = _editor_signature(target)
    if actual_failure_editor != expected_failure_editor:
        raise AssertionError(
            "Failure path did not restore editor state: "
            f"{actual_failure_editor!r} != {expected_failure_editor!r}"
        )
    if _action_signature(source_action) != source_signature:
        raise AssertionError("Failure path modified the source Action")

    if source.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    target.animation_data.action = None
    for selected in tuple(bpy.context.selected_objects):
        selected.select_set(False)
    target.select_set(True)
    bpy.context.view_layer.objects.active = target
    bpy.context.scene.frame_start = FIRST_FRAME
    bpy.context.scene.frame_end = LAST_FRAME
    # Schema v1 takes already stored the Archive name but did not persist the
    # UUID/slot/bind metadata required by deterministic archived bake.  Keep
    # their original evaluated-source workflow instead of making old .blend
    # files fail with a re-import error.
    source["motion_bridge_source_schema_version"] = 1
    source["motion_bridge_source_archive_action"] = "Legacy Source Archive"
    if addon._uses_archived_source_bake(source):
        raise AssertionError("Legacy Source Take was routed to the v2 archived bake")
    addon.register()
    try:
        settings = bpy.context.scene.motion_bridge_session
        settings.source_armature = source
        settings.retarget_preset = "AUTO"
        result = bpy.ops.motion_bridge.bake_source_to_canonical()
        if "FINISHED" not in result:
            raise AssertionError(f"Retarget operator failed: {result}")
        operator_action = target.animation_data.action
        if (
            operator_action is None
            or operator_action.get("motion_bridge_role") != RETARGET_BAKED_ROLE
        ):
            raise AssertionError("Retarget operator did not activate the baked Action")
        result = bpy.ops.motion_bridge.create_correction_layer()
        if "FINISHED" not in result or not target.get("motion_bridge_stack_id"):
            raise AssertionError("Retarget Action did not enter the Correction workflow")

        handler_target, _ = _create_armature(
            "MB_Handler_Removed_Target",
            scale=1.0,
            source=False,
        )
        for selected in tuple(bpy.context.selected_objects):
            selected.select_set(False)
        handler_target.select_set(True)
        bpy.context.view_layer.objects.active = handler_target
        actions_before_handler = {
            action.as_pointer() for action in bpy.data.actions
        }
        handler_state = {"armed": True, "called": False}

        def remove_target_after_bake_role(
            _scene: bpy.types.Scene,
            _depsgraph: bpy.types.Depsgraph,
        ) -> None:
            if not handler_state["armed"]:
                return
            completed_new_action = any(
                action.as_pointer() not in actions_before_handler
                and action.get("motion_bridge_role") == RETARGET_BAKED_ROLE
                for action in bpy.data.actions
            )
            if not completed_new_action:
                return
            handler_state["armed"] = False
            handler_state["called"] = True
            bpy.data.objects.remove(handler_target, do_unlink=True)

        bpy.app.handlers.frame_change_post.append(remove_target_after_bake_role)
        try:
            try:
                handler_result = bpy.ops.motion_bridge.bake_source_to_canonical()
            except RuntimeError as error:
                if "モーションを変換できません" not in str(error):
                    raise
            else:
                if "CANCELLED" not in handler_result:
                    raise AssertionError(
                        "Bake succeeded after its target was removed by a frame handler"
                    )
        finally:
            if remove_target_after_bake_role in bpy.app.handlers.frame_change_post:
                bpy.app.handlers.frame_change_post.remove(
                    remove_target_after_bake_role
                )
        if not handler_state["called"]:
            raise AssertionError("Bake rollback frame handler was not exercised")
        if {
            action.as_pointer() for action in bpy.data.actions
        } != actions_before_handler:
            raise AssertionError(
                "Target removal during Bake restore leaked a completed Action"
            )
    finally:
        addon.unregister()

    print(
        json.dumps(
            {
                "status": "PASS",
                "heightScale": plan.height_scale,
                "rotationErrorDegrees": rotation_error,
                "hipsTranslationError": hips_error,
                "rootLocationError": root_location_error,
                "rootRotationErrorDegrees": root_rotation_error,
                "doubleMixSeparationDegrees": double_mix_separation,
                "frameCount": LAST_FRAME - FIRST_FRAME + 1,
                "sourcePreserved": True,
                "failureCleanup": True,
                "handlerTargetRemovalCleanup": True,
                "legacySourceFallback": True,
                "operatorWorkflow": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
