"""One-click, non-destructive motion corrections for the Canonical rig.

The public functions sample the currently evaluated stack, run a Blender-free
solver, and write the absolute result into a new top Correction layer.  The
new layer is removed if writing or post-write verification fails, so a failed
automatic operation never leaves a partial edit behind.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Callable, Final, Iterable

import bpy

from ..foot_lock import (
    FOOT_NAMES,
    FootLockContactInterval,
    FootLockResult,
    FootLockSettings,
    detect_foot_lock_intervals,
    solve_foot_lock,
)
from ..joint_limits import JointLimitFrame, limit_motion_joints
from ..motion_filter import (
    SmoothedFrameTarget,
    SmoothingChannels,
    SmoothingStrength,
    smooth_motion,
    smoothing_preset,
)
from ..motion_quality import MotionFrame, MotionSequence, QuaternionWXYZ
from ..range_spec import RangeSpec
from .comparison import corrections_preview_enabled
from .correction_edit import has_correction_edit_session
from .correction_layers import (
    CorrectionLayer,
    CorrectionLayerStack,
    add_correction_layer,
    remove_correction_layer,
    require_unshared_correction_actions,
    set_active_correction_layer,
    validate_layer_stack,
)
from .correction_writer import (
    MOTION_REVISION_PROPERTY,
    CorrectionBoneTarget,
    CorrectionFrameTarget,
    write_correction_targets,
)
from .motion_sample import sample_motion_range


AUTO_CORRECTION_MAX_FRAMES: Final = 600
AUTO_CORRECTION_CHANGE_EPSILON_DEGREES: Final = 0.01
AUTO_CORRECTION_ROTATION_VERIFY_TOLERANCE_DEGREES: Final = 0.1
AUTO_CORRECTION_FOOT_VERIFY_TOLERANCE: Final = 0.002


class AutoCorrectionKind(str, Enum):
    SMOOTH = "smooth"
    FOOT_LOCK = "foot_lock"
    JOINT_LIMITS = "joint_limits"


class FootLockSide(str, Enum):
    BOTH = "both"
    LEFT = "left"
    RIGHT = "right"


@dataclass(frozen=True, slots=True)
class AutoCorrectionResult:
    """Compact operator-facing summary of one automatic correction."""

    kind: AutoCorrectionKind
    applied: bool
    layer_id: str | None = None
    layer_name: str | None = None
    written_frame_count: int = 0
    written_bone_sample_count: int = 0
    affected_sample_count: int = 0
    interval_count: int = 0
    clamped_sample_count: int = 0
    maximum_change_degrees: float = 0.0


@dataclass(frozen=True, slots=True)
class _RevisionState:
    existed: bool
    value: object | None


def apply_smoothing_correction(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    range_spec: RangeSpec,
    *,
    strength: SmoothingStrength = SmoothingStrength.STANDARD,
) -> AutoCorrectionResult:
    """Smooth local bone rotations and add one reversible Correction layer."""

    stack = _preflight(context, armature, range_spec)
    radius = math.ceil(smoothing_preset(strength).radius_frames)
    padded_range = _padded_range(stack, range_spec, radius=radius)
    sequence = _sample(
        context,
        armature,
        padded_range,
        maximum=AUTO_CORRECTION_MAX_FRAMES + radius * 2,
    )
    solved = smooth_motion(
        sequence,
        strength=strength,
        channels=SmoothingChannels(
            root_location=False,
            root_rotation=False,
            bone_names=None,
            hips_location=False,
        ),
    )
    selected_targets = _targets_inside_range(solved.targets, range_spec)
    maximum_change = _maximum_smoothing_change(sequence, selected_targets)
    if maximum_change <= AUTO_CORRECTION_CHANGE_EPSILON_DEGREES:
        return AutoCorrectionResult(AutoCorrectionKind.SMOOTH, False)
    changed_bones = _changed_smoothing_bones(sequence, selected_targets)
    targets = _smoothing_writer_targets(
        sequence,
        selected_targets,
        range_spec,
        changed_bones,
    )
    layer, frame_count, bone_count = _write_new_layer(
        context,
        armature,
        targets,
        name="Auto Smooth",
    )
    return AutoCorrectionResult(
        AutoCorrectionKind.SMOOTH,
        True,
        layer_id=layer.layer_id,
        layer_name=layer.name,
        written_frame_count=frame_count,
        written_bone_sample_count=bone_count,
        affected_sample_count=len(selected_targets) * len(changed_bones),
        maximum_change_degrees=maximum_change,
    )


def apply_joint_limit_correction(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    range_spec: RangeSpec,
) -> AutoCorrectionResult:
    """Soft-limit Canonical local rotations in a new Correction layer."""

    stack = _preflight(context, armature, range_spec)
    padded_range = _padded_range(stack, range_spec, radius=1)
    sequence = _sample(
        context,
        armature,
        padded_range,
        maximum=AUTO_CORRECTION_MAX_FRAMES + 2,
    )
    solved = limit_motion_joints(sequence, preserve_endpoints=True)
    if solved.corrected_sample_count == 0:
        return AutoCorrectionResult(AutoCorrectionKind.JOINT_LIMITS, False)
    limited_names = _changed_joint_bones(sequence, solved.frames)
    targets = tuple(
        CorrectionFrameTarget(
            scene_frame=frame.frame,
            bones=tuple(
                CorrectionBoneTarget(
                    bone_name=bone_name,
                    local_rotation_wxyz=rotation,
                )
                for bone_name, rotation in frame.local_rotations_wxyz
                if bone_name in limited_names
            ),
        )
        for frame in solved.frames
    )
    layer, frame_count, bone_count = _write_new_layer(
        context,
        armature,
        targets,
        name="Auto Joint Limits",
    )
    return AutoCorrectionResult(
        AutoCorrectionKind.JOINT_LIMITS,
        True,
        layer_id=layer.layer_id,
        layer_name=layer.name,
        written_frame_count=frame_count,
        written_bone_sample_count=bone_count,
        affected_sample_count=solved.corrected_sample_count,
        maximum_change_degrees=solved.maximum_change_degrees,
    )


def apply_foot_lock_correction(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    range_spec: RangeSpec,
    *,
    side: FootLockSide = FootLockSide.BOTH,
    settings: FootLockSettings | None = None,
) -> AutoCorrectionResult:
    """Detect contacts, solve both leg chains, and add a verified Foot Lock."""

    stack = _preflight(context, armature, range_spec)
    if not isinstance(side, FootLockSide):
        raise TypeError("side must be a FootLockSide")
    checked_settings = FootLockSettings() if settings is None else settings
    if not isinstance(checked_settings, FootLockSettings):
        raise TypeError("settings must be FootLockSettings")
    padded_range = _padded_range(stack, range_spec, radius=1)
    sequence = _sample(
        context,
        armature,
        padded_range,
        maximum=AUTO_CORRECTION_MAX_FRAMES + 2,
    )
    intervals = _selected_clipped_intervals(
        sequence,
        detect_foot_lock_intervals(sequence, checked_settings),
        range_spec,
        side,
    )
    if not intervals:
        return AutoCorrectionResult(AutoCorrectionKind.FOOT_LOCK, False)
    solved = solve_foot_lock(sequence, intervals, settings=checked_settings)
    if not solved.targets:
        return AutoCorrectionResult(AutoCorrectionKind.FOOT_LOCK, False)
    targets = solved.to_correction_frame_targets()

    def verify() -> None:
        _verify_foot_lock(
            context,
            armature,
            padded_range,
            solved,
            maximum=AUTO_CORRECTION_MAX_FRAMES + 2,
        )

    layer, frame_count, bone_count = _write_new_layer(
        context,
        armature,
        targets,
        name="Auto Foot Lock",
        verify=verify,
    )
    return AutoCorrectionResult(
        AutoCorrectionKind.FOOT_LOCK,
        True,
        layer_id=layer.layer_id,
        layer_name=layer.name,
        written_frame_count=frame_count,
        written_bone_sample_count=bone_count,
        affected_sample_count=solved.locked_sample_count,
        interval_count=len(solved.intervals),
        clamped_sample_count=solved.clamped_sample_count,
    )


def _preflight(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    range_spec: RangeSpec,
) -> CorrectionLayerStack:
    if not isinstance(range_spec, RangeSpec):
        raise TypeError("range_spec must be a RangeSpec")
    if range_spec.integer_sample_count > AUTO_CORRECTION_MAX_FRAMES:
        raise ValueError(
            f"補正範囲が{range_spec.integer_sample_count:,}フレームあります。"
            f"プレビュー範囲を{AUTO_CORRECTION_MAX_FRAMES:,}フレーム以内に設定してください"
        )
    validation = validate_layer_stack(armature, check_content=True)
    validation.raise_for_errors()
    stack = validation.stack
    if stack is None:
        raise ValueError("修正レイヤーの状態を確認できません")
    requested_start, requested_end = range_spec.expanded_integer_bounds()
    whole_start, whole_end = _whole_integer_bounds(stack)
    if requested_start < whole_start or requested_end > whole_end:
        raise ValueError(
            "自動補正はモーションAction内の整数フレームだけに適用できます"
        )
    require_unshared_correction_actions(armature, stack)
    animation_data = armature.animation_data
    if animation_data is None:
        raise ValueError("モーションのアニメーションデータがありません")
    if (
        animation_data.action is not None
        or bool(animation_data.use_tweak_mode)
        or bool(getattr(context.scene, "is_nla_tweakmode", False))
        or has_correction_edit_session(armature)
    ):
        raise ValueError("開いている修正作業を終了してから自動補正してください")
    if not corrections_preview_enabled(armature):
        raise ValueError("「修正後」へ戻してから自動補正してください")
    if any(layer.solo for layer in stack.correction_layers):
        raise ValueError("修正レイヤーのSoloを解除してから自動補正してください")
    return stack


def _sample(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    range_spec: RangeSpec,
    *,
    maximum: int = AUTO_CORRECTION_MAX_FRAMES,
) -> MotionSequence:
    return sample_motion_range(
        context,
        armature,
        range_spec=range_spec,
        max_samples=maximum,
        include_key_density=False,
    )


def _write_new_layer(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    targets: tuple[CorrectionFrameTarget, ...],
    *,
    name: str,
    verify: Callable[[], None] | None = None,
) -> tuple[CorrectionLayer, int, int]:
    stack = validate_layer_stack(armature, check_content=True).stack
    if stack is None:
        raise ValueError("修正レイヤーの状態を確認できません")
    previous_active_id = stack.active_layer_id
    revision = _capture_revision(armature)
    layer: CorrectionLayer | None = None
    try:
        layer = add_correction_layer(context, armature, name=name)
        written = write_correction_targets(
            context,
            armature,
            targets,
            interpolation="LINEAR",
            rotation_tolerance_degrees=(
                AUTO_CORRECTION_ROTATION_VERIFY_TOLERANCE_DEGREES
            ),
        )
        if verify is not None:
            verify()
        validate_layer_stack(armature, check_content=True).raise_for_errors()
        return layer, written.frame_count, written.bone_sample_count
    except Exception as error:
        cleanup_error: Exception | None = None
        if layer is not None:
            try:
                remove_correction_layer(armature, layer.layer_id)
                set_active_correction_layer(armature, previous_active_id)
            except Exception as exception:  # pragma: no cover - invalidated RNA
                cleanup_error = exception
        _restore_revision(armature, revision)
        if cleanup_error is not None and hasattr(error, "add_note"):
            error.add_note("自動補正レイヤーの後片付けにも失敗しました: " + str(cleanup_error))
        raise


def _padded_range(
    stack: CorrectionLayerStack,
    requested: RangeSpec,
    *,
    radius: int,
) -> RangeSpec:
    if type(radius) is not int or radius < 0:
        raise ValueError("自動補正の前後範囲が正しくありません")
    whole_start, whole_end = _whole_integer_bounds(stack)
    requested_start, requested_end = requested.expanded_integer_bounds()
    return RangeSpec(
        float(max(whole_start, requested_start - radius)),
        float(min(whole_end, requested_end + radius)),
        requested.source,
    )


def _whole_integer_bounds(stack: CorrectionLayerStack) -> tuple[int, int]:
    start, end = (float(value) for value in stack.base_action.frame_range)
    first = math.ceil(start - 1.0e-6)
    last = math.floor(end + 1.0e-6)
    if last < first:
        raise ValueError("モーションActionに自動補正できる整数フレームがありません")
    return first, last


def _selected_clipped_intervals(
    sequence: MotionSequence,
    intervals: Iterable[FootLockContactInterval],
    requested: RangeSpec,
    side: FootLockSide,
) -> tuple[FootLockContactInterval, ...]:
    requested_start, requested_end = requested.expanded_integer_bounds()
    allowed = {
        FootLockSide.BOTH: frozenset(FOOT_NAMES),
        FootLockSide.LEFT: frozenset(("LeftFoot",)),
        FootLockSide.RIGHT: frozenset(("RightFoot",)),
    }[side]
    frames = {int(frame.frame): frame for frame in sequence.frames}
    result: list[FootLockContactInterval] = []
    for interval in intervals:
        if interval.foot_name not in allowed:
            continue
        start = max(requested_start, int(interval.frame_start))
        end = min(requested_end, int(interval.frame_end))
        if end < start:
            continue
        positions = tuple(
            _effector_position(frames[frame], interval.foot_name)
            for frame in range(start, end + 1)
        )
        count = len(positions)
        anchor = tuple(
            sum(position[axis] for position in positions) / count
            for axis in range(3)
        )
        result.append(
            FootLockContactInterval(
                foot_name=interval.foot_name,
                frame_start=float(start),
                frame_end=float(end),
                anchor_world=anchor,
                sample_count=count,
                peak_speed=interval.peak_speed,
                peak_ground_distance=interval.peak_ground_distance,
            )
        )
    return tuple(sorted(result, key=lambda value: (value.frame_start, value.foot_name)))


def _verify_foot_lock(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    range_spec: RangeSpec,
    solved: FootLockResult,
    *,
    maximum: int,
) -> None:
    evaluated = _sample(
        context,
        armature,
        range_spec,
        maximum=maximum,
    )
    frames = {frame.frame: frame for frame in evaluated.frames}
    for diagnostic in solved.diagnostics:
        frame = frames.get(diagnostic.frame)
        if frame is None:
            raise RuntimeError("Foot Lockの確認フレームを取得できませんでした")
        actual = _effector_position(frame, diagnostic.foot_name)
        error = math.dist(actual, diagnostic.solved_effector_world)
        if error > AUTO_CORRECTION_FOOT_VERIFY_TOLERANCE:
            raise RuntimeError(
                f"Foot Lockを安全に確認できませんでした（{diagnostic.foot_name} "
                f"{diagnostic.frame:g}F、誤差 {error:.5g}m）"
            )


def _effector_position(frame: MotionFrame, foot_name: str) -> tuple[float, float, float]:
    for effector in frame.effectors:
        if effector.name == foot_name:
            return effector.position_world
    raise ValueError(f"{foot_name}の足先位置を取得できません")


def _targets_inside_range(
    targets: Iterable[SmoothedFrameTarget],
    requested: RangeSpec,
) -> tuple[SmoothedFrameTarget, ...]:
    start, end = requested.expanded_integer_bounds()
    return tuple(
        target for target in targets if start <= target.scene_frame <= end
    )


def _smoothing_writer_targets(
    sequence: MotionSequence,
    selected: tuple[SmoothedFrameTarget, ...],
    requested: RangeSpec,
    changed_bones: frozenset[str],
) -> tuple[CorrectionFrameTarget, ...]:
    source = {frame.frame: frame for frame in sequence.frames}
    start, end = requested.expanded_integer_bounds()
    targets: list[CorrectionFrameTarget] = [
        CorrectionFrameTarget(
            scene_frame=target.scene_frame,
            bones=tuple(
                CorrectionBoneTarget(
                    bone_name=bone.bone_name,
                    local_rotation_wxyz=bone.local_rotation_wxyz,
                )
                for bone in target.bone_rotations
                if bone.bone_name in changed_bones
            ),
        )
        for target in selected
    ]
    for frame_number in (start - 1, end + 1):
        frame = source.get(float(frame_number))
        if frame is not None:
            targets.append(
                CorrectionFrameTarget(
                    scene_frame=frame.frame,
                    bones=tuple(
                        CorrectionBoneTarget(
                            bone_name=bone.name,
                            local_rotation_wxyz=bone.local_rotation_wxyz,
                        )
                        for bone in frame.bones
                        if bone.name in changed_bones
                    ),
                )
            )
    return tuple(sorted(targets, key=lambda target: target.scene_frame))


def _changed_smoothing_bones(
    sequence: MotionSequence,
    targets: Iterable[SmoothedFrameTarget],
) -> frozenset[str]:
    source = {
        frame.frame: {bone.name: bone.local_rotation_wxyz for bone in frame.bones}
        for frame in sequence.frames
    }
    return frozenset(
        bone.bone_name
        for target in targets
        for bone in target.bone_rotations
        if _quaternion_distance_degrees(
            source[target.scene_frame][bone.bone_name],
            bone.local_rotation_wxyz,
        )
        > AUTO_CORRECTION_CHANGE_EPSILON_DEGREES
    )


def _changed_joint_bones(
    sequence: MotionSequence,
    frames: Iterable[JointLimitFrame],
) -> frozenset[str]:
    source = {
        frame.frame: {bone.name: bone.local_rotation_wxyz for bone in frame.bones}
        for frame in sequence.frames
    }
    return frozenset(
        bone_name
        for frame in frames
        for bone_name, rotation in frame.local_rotations_wxyz
        if _quaternion_distance_degrees(
            source[frame.frame][bone_name],
            rotation,
        )
        > AUTO_CORRECTION_CHANGE_EPSILON_DEGREES
    )


def _maximum_smoothing_change(
    sequence: MotionSequence,
    targets: Iterable[SmoothedFrameTarget],
) -> float:
    source = {
        frame.frame: {bone.name: bone.local_rotation_wxyz for bone in frame.bones}
        for frame in sequence.frames
    }
    maximum = 0.0
    for target in targets:
        for bone in target.bone_rotations:
            maximum = max(
                maximum,
                _quaternion_distance_degrees(
                    source[target.scene_frame][bone.bone_name],
                    bone.local_rotation_wxyz,
                ),
            )
    return maximum


def _quaternion_distance_degrees(
    left: QuaternionWXYZ,
    right: QuaternionWXYZ,
) -> float:
    dot = abs(sum(a * b for a, b in zip(left, right, strict=True)))
    return math.degrees(2.0 * math.acos(max(-1.0, min(1.0, dot))))


def _capture_revision(armature: bpy.types.Object) -> _RevisionState:
    return _RevisionState(
        MOTION_REVISION_PROPERTY in armature,
        armature.get(MOTION_REVISION_PROPERTY),
    )


def _restore_revision(armature: bpy.types.Object, state: _RevisionState) -> None:
    if state.existed:
        armature[MOTION_REVISION_PROPERTY] = state.value
    elif MOTION_REVISION_PROPERTY in armature:
        del armature[MOTION_REVISION_PROPERTY]


__all__ = (
    "AUTO_CORRECTION_CHANGE_EPSILON_DEGREES",
    "AUTO_CORRECTION_FOOT_VERIFY_TOLERANCE",
    "AUTO_CORRECTION_MAX_FRAMES",
    "AutoCorrectionKind",
    "AutoCorrectionResult",
    "FootLockSide",
    "apply_foot_lock_correction",
    "apply_joint_limit_correction",
    "apply_smoothing_correction",
)
