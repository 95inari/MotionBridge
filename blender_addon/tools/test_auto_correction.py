from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import bpy
from mathutils import Quaternion


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))

from motion_bridge import addon  # noqa: E402
from motion_bridge.canonical.rig_spec import JOINTS, SKELETON_ID  # noqa: E402
from motion_bridge.editing.action_api import iter_action_fcurves  # noqa: E402
import motion_bridge.editing.auto_correction as auto_module  # noqa: E402
from motion_bridge.editing.auto_correction import (  # noqa: E402
    FootLockSide,
    apply_foot_lock_correction,
    apply_joint_limit_correction,
    apply_smoothing_correction,
)
from motion_bridge.editing.correction_layers import (  # noqa: E402
    action_checksum,
    create_correction_layer,
    get_correction_layer_stack,
    validate_layer_stack,
)
from motion_bridge.editing.correction_writer import (  # noqa: E402
    MOTION_REVISION_PROPERTY,
    CorrectionBoneTarget,
    CorrectionFrameTarget,
)
import motion_bridge.editing.correction_writer as writer_module  # noqa: E402
from motion_bridge.foot_lock import FootLockSettings  # noqa: E402
from motion_bridge.motion_filter import SmoothingStrength  # noqa: E402
from motion_bridge.range_spec import RangeSource, RangeSpec  # noqa: E402


FIRST_FRAME = 1
LAST_FRAME = 9
PREVIEW_RANGE = RangeSpec(3.0, 7.0, RangeSource.PREVIEW_RANGE)
ROTATION_EPSILON_DEGREES = 0.02
POSITION_EPSILON = 0.002


class _LayoutRecorder:
    def __init__(
        self,
        operators: list[str] | None = None,
        labels: list[str] | None = None,
    ) -> None:
        self.operators = [] if operators is None else operators
        self.labels = [] if labels is None else labels
        self.alert = False
        self.enabled = True
        self.scale_y = 1.0

    def box(self) -> _LayoutRecorder:
        return _LayoutRecorder(self.operators, self.labels)

    def row(self, **_kwargs: object) -> _LayoutRecorder:
        return _LayoutRecorder(self.operators, self.labels)

    def column(self, **_kwargs: object) -> _LayoutRecorder:
        return _LayoutRecorder(self.operators, self.labels)

    def label(self, **kwargs: object) -> None:
        self.labels.append(str(kwargs.get("text", "")))

    def prop(self, *_args: object, **_kwargs: object) -> None:
        return None

    def operator(self, operator_id: str, **_kwargs: object) -> SimpleNamespace:
        self.operators.append(operator_id)
        return SimpleNamespace()


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for action in tuple(bpy.data.actions):
        if action.users == 0:
            bpy.data.actions.remove(action)
    for armature in tuple(bpy.data.armatures):
        if armature.users == 0:
            bpy.data.armatures.remove(armature)


def _create_fixture(
    name: str,
    *,
    rotation_degrees: dict[tuple[int, str], float] | None = None,
    hips_x: dict[int, float] | None = None,
) -> bpy.types.Object:
    _clear_scene()
    data = bpy.data.armatures.new(f"{name}_Data")
    armature = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(armature)
    armature["motion_bridge_skeleton_id"] = SKELETON_ID
    armature.rotation_mode = "QUATERNION"
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature

    bpy.ops.object.mode_set(mode="EDIT")
    bones: dict[str, bpy.types.EditBone] = {}
    for joint in JOINTS[1:]:
        assert joint.head is not None and joint.tail is not None
        bone = data.edit_bones.new(joint.name)
        bone.head = joint.head
        bone.tail = joint.tail
        if joint.parent_index > 0:
            bone.parent = bones[JOINTS[joint.parent_index].name]
            bone.use_connect = (bone.head - bone.parent.tail).length <= 1.0e-7
        bones[joint.name] = bone
    bpy.ops.object.mode_set(mode="OBJECT")

    rotations = {} if rotation_degrees is None else rotation_degrees
    locations = {} if hips_x is None else hips_x
    for pose_bone in armature.pose.bones:
        pose_bone.rotation_mode = "QUATERNION"
    for frame in range(FIRST_FRAME, LAST_FRAME + 1):
        bpy.context.scene.frame_set(frame)
        for pose_bone in armature.pose.bones:
            angle = rotations.get((frame, pose_bone.name), 0.0)
            pose_bone.rotation_quaternion = Quaternion(
                (1.0, 0.0, 0.0),
                math.radians(angle),
            )
            pose_bone.location = (
                (locations.get(frame, 0.0), 0.0, 0.0)
                if pose_bone.name == "Hips"
                else (0.0, 0.0, 0.0)
            )
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
    action = armature.animation_data.action
    action.name = f"{name}_Base"
    for curve in iter_action_fcurves(action):
        for point in curve.keyframe_points:
            point.interpolation = "LINEAR"
    create_correction_layer(bpy.context, armature)
    validate_layer_stack(armature, check_content=True).raise_for_errors()
    return armature


def _scene_frame(frame: int) -> None:
    bpy.context.scene.frame_set(frame)
    bpy.context.view_layer.update()


def _local_rotation(armature: bpy.types.Object, bone_name: str, frame: int) -> Quaternion:
    _scene_frame(frame)
    evaluated = armature.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return writer_module._evaluated_local_matrix(
        evaluated.pose.bones[bone_name]
    ).to_quaternion()


def _foot_position(
    armature: bpy.types.Object,
    foot_name: str,
    frame: int,
) -> tuple[float, float, float]:
    _scene_frame(frame)
    evaluated = armature.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return tuple(evaluated.matrix_world @ evaluated.pose.bones[foot_name].tail)


def _rotation_error_degrees(left: Quaternion, right: Quaternion) -> float:
    angle = math.degrees(left.normalized().rotation_difference(right.normalized()).angle)
    return min(angle, abs(360.0 - angle))


def _assert_outside_unchanged(
    armature: bpy.types.Object,
    bone_name: str,
    before: dict[int, Quaternion],
) -> None:
    for frame, expected in before.items():
        error = _rotation_error_degrees(
            _local_rotation(armature, bone_name, frame),
            expected,
        )
        if error > ROTATION_EPSILON_DEGREES:
            raise AssertionError(
                f"Preview halo leaked at {frame}F for {bone_name}: {error} degrees"
            )


def _protected_stack_signature(armature: bpy.types.Object) -> tuple[object, ...]:
    stack = get_correction_layer_stack(armature)
    if stack is None:
        raise AssertionError("Correction stack disappeared")
    return (
        action_checksum(stack.archive_action),
        action_checksum(stack.base_action),
        tuple(
            (layer.layer_id, action_checksum(layer.action))
            for layer in stack.correction_layers
        ),
        stack.active_layer_id,
        armature.get(MOTION_REVISION_PROPERTY),
    )


def _test_smoothing_and_rollback() -> dict[str, object]:
    armature = _create_fixture(
        "MB_Auto_Smooth",
        rotation_degrees={(5, "Spine"): 35.0},
    )
    before_outside = {
        frame: _local_rotation(armature, "Spine", frame) for frame in (2, 8)
    }
    before_center = _local_rotation(armature, "Spine", 5)
    protected_before = _protected_stack_signature(armature)[:2]
    started = time.monotonic()
    result = apply_smoothing_correction(
        bpy.context,
        armature,
        PREVIEW_RANGE,
        strength=SmoothingStrength.STANDARD,
    )
    duration = time.monotonic() - started
    if not result.applied or result.written_frame_count != 7:
        raise AssertionError(f"Smoothing result is incomplete: {result!r}")
    after_center = _local_rotation(armature, "Spine", 5)
    identity = Quaternion((1.0, 0.0, 0.0, 0.0))
    if _rotation_error_degrees(after_center, identity) >= _rotation_error_degrees(
        before_center,
        identity,
    ):
        raise AssertionError("One-click smoothing did not reduce the rotation spike")
    _assert_outside_unchanged(armature, "Spine", before_outside)
    if _protected_stack_signature(armature)[:2] != protected_before:
        raise AssertionError("Smoothing modified Archive or Base")

    before_rollback = _protected_stack_signature(armature)
    target = CorrectionFrameTarget(
        scene_frame=5.0,
        bones=(
            CorrectionBoneTarget(
                "Spine",
                tuple(_local_rotation(armature, "Spine", 5)),
            ),
        ),
    )

    def fail_verification() -> None:
        raise RuntimeError("injected post-write verification failure")

    try:
        auto_module._write_new_layer(
            bpy.context,
            armature,
            (target,),
            name="Injected Failure",
            verify=fail_verification,
        )
    except RuntimeError as error:
        if "injected" not in str(error):
            raise
    else:
        raise AssertionError("Injected verification failure did not propagate")
    if _protected_stack_signature(armature) != before_rollback:
        raise AssertionError("Automatic correction rollback changed stack or revision")
    return {
        "durationSeconds": round(duration, 4),
        "frames": result.written_frame_count,
        "maximumChangeDegrees": round(result.maximum_change_degrees, 4),
        "rollback": True,
    }


def _test_joint_limits() -> dict[str, object]:
    armature = _create_fixture(
        "MB_Auto_Joints",
        rotation_degrees={(5, "LeftLowerLeg"): 179.0},
    )
    before_outside = {
        frame: _local_rotation(armature, "LeftLowerLeg", frame) for frame in (2, 8)
    }
    before = _rotation_error_degrees(
        _local_rotation(armature, "LeftLowerLeg", 5),
        Quaternion((1.0, 0.0, 0.0, 0.0)),
    )
    result = apply_joint_limit_correction(
        bpy.context,
        armature,
        PREVIEW_RANGE,
    )
    if not result.applied or result.affected_sample_count < 1:
        raise AssertionError(f"Joint-limit result is incomplete: {result!r}")
    after = _rotation_error_degrees(
        _local_rotation(armature, "LeftLowerLeg", 5),
        Quaternion((1.0, 0.0, 0.0, 0.0)),
    )
    if after >= before or after > 165.01:
        raise AssertionError(f"Joint limit did not clamp 179 degrees: {before} -> {after}")
    _assert_outside_unchanged(armature, "LeftLowerLeg", before_outside)
    return {
        "affectedSamples": result.affected_sample_count,
        "beforeDegrees": round(before, 4),
        "afterDegrees": round(after, 4),
    }


def _test_foot_lock() -> dict[str, object]:
    armature = _create_fixture(
        "MB_Auto_Foot",
        hips_x={frame: (frame - 5) * 0.005 for frame in range(1, 10)},
    )
    before_outside = {
        frame: _local_rotation(armature, "LeftUpperLeg", frame) for frame in (2, 8)
    }
    before_positions = tuple(
        _foot_position(armature, "LeftFoot", frame) for frame in range(3, 8)
    )
    result = apply_foot_lock_correction(
        bpy.context,
        armature,
        PREVIEW_RANGE,
        side=FootLockSide.LEFT,
        settings=FootLockSettings(max_contact_speed=1.0),
    )
    if not result.applied or result.interval_count != 1:
        raise AssertionError(f"Foot Lock result is incomplete: {result!r}")
    after_positions = tuple(
        _foot_position(armature, "LeftFoot", frame) for frame in range(3, 8)
    )
    before_span = max(
        math.dist(left, right)
        for left in before_positions
        for right in before_positions
    )
    after_span = max(
        math.dist(left, right)
        for left in after_positions
        for right in after_positions
    )
    if after_span > POSITION_EPSILON or after_span >= before_span:
        raise AssertionError(
            f"Foot Lock did not stabilize the evaluated foot: {before_span} -> {after_span}"
        )
    _assert_outside_unchanged(armature, "LeftUpperLeg", before_outside)
    return {
        "beforeSpan": round(before_span, 6),
        "afterSpan": round(after_span, 6),
        "lockedSamples": result.affected_sample_count,
    }


def _test_registered_beginner_surface() -> dict[str, object]:
    operator_ids = {
        addon.MB_OT_AutoSmoothMotion.bl_idname,
        addon.MB_OT_AutoFootLock.bl_idname,
        addon.MB_OT_AutoJointLimits.bl_idname,
    }
    registered = {
        cls.bl_idname
        for cls in addon._CLASSES
        if hasattr(cls, "bl_idname")
    }
    if not operator_ids.issubset(registered):
        raise AssertionError("One-click operators are not registered by the add-on")
    labels = {
        addon.MB_OT_AutoSmoothMotion.bl_label,
        addon.MB_OT_AutoFootLock.bl_label,
        addon.MB_OT_AutoJointLimits.bl_label,
    }
    expected = {"揺れをなめらかに", "足滑りを固定", "関節の曲がりすぎを直す"}
    if labels != expected:
        raise AssertionError(f"Beginner labels changed: {labels!r}")
    layout = _LayoutRecorder()
    addon._draw_beginner_auto_correction(
        bpy.context,
        layout,
        SimpleNamespace(beginner_auto_details=True),
        SimpleNamespace(
            stack_valid=True,
            correction_actions_private=True,
            corrections_enabled=True,
            editing=False,
            edit_session=False,
            envelope_preview=False,
        ),
    )
    if not operator_ids.issubset(layout.operators):
        raise AssertionError(f"Beginner card hid one-click actions: {layout.operators!r}")
    if not any("Foot Lockは最後" in label for label in layout.labels):
        raise AssertionError("Beginner card did not explain the safe operation order")
    return {
        "operators": sorted(operator_ids),
        "labels": sorted(labels),
        "cardDrawn": True,
    }


def main() -> None:
    result = {
        "footLock": _test_foot_lock(),
        "jointLimits": _test_joint_limits(),
        "registeredSurface": _test_registered_beginner_surface(),
        "smoothing": _test_smoothing_and_rollback(),
    }
    print("AUTO_CORRECTION_TEST_RESULT=" + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
