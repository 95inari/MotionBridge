"""Transactional evaluated-pose writer for one active Correction layer.

This module intentionally writes through Blender's NLA Tweak Mode instead of
placing desired pose values directly in FCurves.  Blender then performs the
inverse ``COMBINE`` remap against Base and lower Correction layers.  The writer
only targets a private, normalized active Correction and restores editor/NLA
state through the existing Correction Edit transaction.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import math
import threading
from typing import Any, Final, Iterable, Iterator

import bpy

from ..motion_quality import MotionFrame, QuaternionWXYZ, Vec3
from .action_api import (
    ActionChannelAccess,
    insert_object_location_key,
    insert_object_quaternion_key,
    iter_action_fcurves,
)
from .pose_api import set_pose_bone_selected
from .correction_edit import (
    _capture_nla_remapped_pose_values,
    _correction_key_transaction,
    cancel_correction_edit_mode,
    enter_correction_edit_mode,
    exit_correction_edit_mode,
    has_correction_edit_session,
    scene_to_correction_frame,
)
from .correction_layers import (
    CorrectionLayer,
    CorrectionLayerStack,
    require_unshared_correction_actions,
    root_trajectory_safety_errors,
    validate_layer_stack,
)


MAX_CORRECTION_WRITE_FRAMES: Final = 20_000
MAX_CORRECTION_WRITE_BONE_SAMPLES: Final = 2_000_000
_FRAME_EPSILON: Final = 1.0e-6
_SUPPORTED_INTERPOLATIONS: Final = frozenset({"CONSTANT", "LINEAR", "BEZIER"})
_HIPS_BONE: Final = "Hips"
MOTION_REVISION_PROPERTY: Final = "motion_bridge_motion_revision"


@dataclass(frozen=True, slots=True)
class CorrectionBoneTarget:
    """Desired evaluated local pose for one bone.

    ``local_rotation_wxyz`` is normalized on construction.  ``location`` is
    accepted only for Hips when the target is written.
    """

    bone_name: str
    local_rotation_wxyz: QuaternionWXYZ
    location: Vec3 | None = None

    def __post_init__(self) -> None:
        name = _nonempty_name(self.bone_name, "bone_name")
        quaternion = _quaternion(self.local_rotation_wxyz, "local_rotation_wxyz")
        object.__setattr__(self, "bone_name", name)
        object.__setattr__(self, "local_rotation_wxyz", quaternion)
        if self.location is not None:
            object.__setattr__(self, "location", _vec3(self.location, "location"))


@dataclass(frozen=True, slots=True)
class CorrectionFrameTarget:
    """Desired evaluated bone and/or Root transforms at one Scene frame."""

    scene_frame: float
    bones: tuple[CorrectionBoneTarget, ...] = ()
    root_location: Vec3 | None = None
    root_rotation_wxyz: QuaternionWXYZ | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "scene_frame", _finite_float(self.scene_frame, "scene_frame"))
        try:
            bones = tuple(self.bones)
        except TypeError as error:
            raise TypeError("bones must be an iterable of CorrectionBoneTarget values") from error
        if not all(isinstance(bone, CorrectionBoneTarget) for bone in bones):
            raise TypeError("bones must contain only CorrectionBoneTarget values")
        bones = tuple(sorted(bones, key=lambda bone: bone.bone_name))
        if len({bone.bone_name for bone in bones}) != len(bones):
            raise ValueError("Correction frame target contains duplicate bones")
        object.__setattr__(self, "bones", bones)
        if self.root_location is not None:
            object.__setattr__(
                self,
                "root_location",
                _vec3(self.root_location, "root_location"),
            )
        if self.root_rotation_wxyz is not None:
            object.__setattr__(
                self,
                "root_rotation_wxyz",
                _quaternion(self.root_rotation_wxyz, "root_rotation_wxyz"),
            )
        if (
            not bones
            and self.root_location is None
            and self.root_rotation_wxyz is None
        ):
            raise ValueError("Correction frame target must contain a bone or Root target")

    @classmethod
    def from_motion_frame(
        cls,
        frame: MotionFrame,
        *,
        include_root: bool = False,
    ) -> CorrectionFrameTarget:
        """Project a sample to bone targets, optionally including world Root."""

        if not isinstance(frame, MotionFrame):
            raise TypeError("frame must be a MotionFrame")
        return cls(
            scene_frame=frame.frame,
            bones=tuple(
                CorrectionBoneTarget(
                    bone_name=bone.name,
                    local_rotation_wxyz=bone.local_rotation_wxyz,
                )
                for bone in frame.bones
            ),
            root_location=frame.root_location if include_root else None,
            root_rotation_wxyz=(
                frame.root_rotation_wxyz if include_root else None
            ),
        )


@dataclass(frozen=True, slots=True)
class CorrectionWriteResult:
    layer_id: str
    action_name: str
    frame_count: int
    bone_sample_count: int
    location_sample_count: int
    root_location_sample_count: int
    root_rotation_sample_count: int
    scene_frame_start: float
    scene_frame_end: float


def write_correction_targets(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    targets: (
        MotionFrame
        | CorrectionFrameTarget
        | Iterable[MotionFrame | CorrectionFrameTarget]
    ),
    *,
    interpolation: str = "LINEAR",
    rotation_tolerance_degrees: float = 0.01,
    location_tolerance: float = 1.0e-5,
) -> CorrectionWriteResult:
    """Write desired evaluated poses into only the active Correction Action.

    All targets are preflighted before Blender state or Action curves change.
    The operation is atomic across every frame: any keying, validation, NLA,
    or verification failure restores the complete targeted curve set and the
    original frame, selection, mode, active Action, and NLA selection.
    """

    _require_main_thread()
    checked_targets = _targets(targets)
    key_interpolation = _interpolation(interpolation)
    rotation_tolerance = _nonnegative_float(
        rotation_tolerance_degrees,
        "rotation_tolerance_degrees",
    )
    position_tolerance = _nonnegative_float(location_tolerance, "location_tolerance")
    stack, layer = _destination(context, armature)
    _validate_targets_for_destination(armature, stack, layer, checked_targets)
    bone_names = tuple(
        sorted({bone.bone_name for target in checked_targets for bone in target.bones})
    )
    include_location = any(
        bone.location is not None for target in checked_targets for bone in target.bones
    )
    include_root_location = any(
        target.root_location is not None for target in checked_targets
    )
    include_root_rotation = any(
        target.root_rotation_wxyz is not None for target in checked_targets
    )
    root_paths = tuple(
        path
        for path, included in (
            ("location", include_root_location),
            ("rotation_quaternion", include_root_rotation),
        )
        if included
    )

    session_open = False
    with _writer_transform_transaction(context, armature, bone_names):
        with _correction_key_transaction(
            context,
            layer.access,
            bone_names,
            include_location=include_location,
            extra_paths=root_paths,
        ):
            try:
                enter_correction_edit_mode(context, armature, layer.layer_id)
                session_open = True
                for target in checked_targets:
                    _set_scene_frame(context.scene, target.scene_frame)
                    if (
                        target.root_location is not None
                        or target.root_rotation_wxyz is not None
                    ):
                        _write_root_target(
                            context,
                            armature,
                            layer.access,
                            target,
                            interpolation=key_interpolation,
                        )
                    if target.bones:
                        _write_bone_targets(
                            context,
                            armature,
                            layer.access,
                            target.bones,
                            interpolation=key_interpolation,
                        )
                exit_correction_edit_mode(context, armature)
                session_open = False

                with _preserve_scene_frame(context.scene):
                    _verify_written_targets(
                        context,
                        armature,
                        checked_targets,
                        rotation_tolerance_degrees=rotation_tolerance,
                        location_tolerance=position_tolerance,
                    )
                validation = validate_layer_stack(armature, check_content=True)
                validation.raise_for_errors()
                resolved = validation.stack
                if resolved is None or resolved.active_layer_id != layer.layer_id:
                    raise RuntimeError("Active Correction changed while writing targets")
                _advance_motion_revision(armature)
            except Exception as write_error:
                if session_open or has_correction_edit_session(armature):
                    try:
                        cancel_correction_edit_mode(context, armature)
                    except Exception as cleanup_error:
                        if hasattr(write_error, "add_note"):
                            write_error.add_note(
                                "Correction Edit cleanup also failed: "
                                + str(cleanup_error)
                            )
                raise

    location_count = sum(
        bone.location is not None for target in checked_targets for bone in target.bones
    )
    return CorrectionWriteResult(
        layer_id=layer.layer_id,
        action_name=layer.action.name,
        frame_count=len(checked_targets),
        bone_sample_count=sum(len(target.bones) for target in checked_targets),
        location_sample_count=location_count,
        root_location_sample_count=sum(
            target.root_location is not None for target in checked_targets
        ),
        root_rotation_sample_count=sum(
            target.root_rotation_wxyz is not None for target in checked_targets
        ),
        scene_frame_start=checked_targets[0].scene_frame,
        scene_frame_end=checked_targets[-1].scene_frame,
    )


def write_correction_target(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    target: MotionFrame | CorrectionFrameTarget,
    **kwargs: Any,
) -> CorrectionWriteResult:
    """Single-frame convenience wrapper for :func:`write_correction_targets`."""

    return write_correction_targets(context, armature, target, **kwargs)


def _require_main_thread() -> None:
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Correction targets can only be written on Blender's main thread")


def _advance_motion_revision(armature: bpy.types.Object) -> None:
    current = armature.get(MOTION_REVISION_PROPERTY, 0)
    if type(current) is not int or current < 0:
        current = 0
    armature[MOTION_REVISION_PROPERTY] = (current + 1) % (1 << 31)


def _targets(
    values: MotionFrame
    | CorrectionFrameTarget
    | Iterable[MotionFrame | CorrectionFrameTarget],
) -> tuple[CorrectionFrameTarget, ...]:
    if isinstance(values, (MotionFrame, CorrectionFrameTarget)):
        candidates = (values,)
    else:
        if isinstance(values, (str, bytes)):
            raise TypeError("targets must contain MotionFrame or CorrectionFrameTarget values")
        try:
            candidates = tuple(values)
        except TypeError as error:
            raise TypeError("targets must be iterable") from error
    if not candidates:
        raise ValueError("At least one Correction target is required")
    if len(candidates) > MAX_CORRECTION_WRITE_FRAMES:
        raise ValueError(
            f"Correction write has {len(candidates)} frames; "
            f"maximum is {MAX_CORRECTION_WRITE_FRAMES}"
        )

    result: list[CorrectionFrameTarget] = []
    for candidate in candidates:
        if isinstance(candidate, MotionFrame):
            result.append(CorrectionFrameTarget.from_motion_frame(candidate))
        elif isinstance(candidate, CorrectionFrameTarget):
            result.append(candidate)
        else:
            raise TypeError(
                "targets must contain only MotionFrame or CorrectionFrameTarget values"
            )
    result.sort(key=lambda target: target.scene_frame)
    if any(
        abs(right.scene_frame - left.scene_frame) <= _FRAME_EPSILON
        for left, right in zip(result, result[1:], strict=False)
    ):
        raise ValueError("Correction targets must have unique Scene frames")
    sample_count = sum(len(target.bones) for target in result)
    if sample_count > MAX_CORRECTION_WRITE_BONE_SAMPLES:
        raise ValueError(
            f"Correction write has {sample_count} bone samples; "
            f"maximum is {MAX_CORRECTION_WRITE_BONE_SAMPLES}"
        )
    return tuple(result)


def _destination(
    context: bpy.types.Context,
    armature: bpy.types.Object,
) -> tuple[CorrectionLayerStack, CorrectionLayer]:
    if context is None or armature is None or armature.type != "ARMATURE":
        raise ValueError("Correction writer requires an Armature Object")
    if not _same_rna(context.scene, bpy.context.scene) or not _same_rna(
        context.view_layer,
        bpy.context.view_layer,
    ):
        raise ValueError("Correction writer requires the active Scene and View Layer")
    if armature.name not in context.view_layer.objects:
        raise ValueError("Correction writer Armature must be in the active View Layer")
    if has_correction_edit_session(armature):
        raise ValueError("Finish the open Correction Edit before using Correction writer")
    validation = validate_layer_stack(armature, check_content=True)
    validation.raise_for_errors()
    stack = validation.stack
    if stack is None:
        raise ValueError("Armature has no valid Motion Bridge Correction stack")
    require_unshared_correction_actions(armature, stack)
    layer = stack.active_layer
    strip = layer.strip
    if layer.track.mute or strip.mute:
        raise ValueError("Active Correction must be unmuted before writing")
    influence = float(strip.influence)
    if not math.isfinite(influence) or abs(influence - 1.0) > _FRAME_EPSILON:
        raise ValueError("Active Correction influence must be 1 before writing")
    if (
        strip.use_animated_influence
        or strip.use_animated_time
        or abs(float(strip.scale) - 1.0) > _FRAME_EPSILON
        or abs(float(strip.repeat) - 1.0) > _FRAME_EPSILON
        or bool(getattr(strip, "use_reverse", False))
        or abs(float(strip.action_frame_start) - float(strip.frame_start)) > _FRAME_EPSILON
        or abs(float(strip.action_frame_end) - float(strip.frame_end)) > _FRAME_EPSILON
    ):
        raise ValueError("Active Correction must use normalized, unretimed playback")
    active_index = stack.correction_layers.index(layer)
    enabled_upper_layers = tuple(
        candidate.name
        for candidate in stack.correction_layers[active_index + 1 :]
        if not candidate.muted
    )
    if enabled_upper_layers:
        names = ", ".join(enabled_upper_layers)
        raise ValueError(
            "Active Correction must be the highest enabled layer before writing; "
            f"enabled layers above it: {names}"
        )
    return stack, layer


def _validate_targets_for_destination(
    armature: bpy.types.Object,
    stack: CorrectionLayerStack,
    layer: CorrectionLayer,
    targets: tuple[CorrectionFrameTarget, ...],
) -> None:
    frame_start, frame_end = (float(value) for value in layer.action.frame_range)
    available = {bone.name: bone for bone in armature.pose.bones}
    if any(
        target.root_location is not None or target.root_rotation_wxyz is not None
        for target in targets
    ):
        _validate_root_destination(armature, stack, targets)
    for target in targets:
        if (
            target.scene_frame < frame_start - _FRAME_EPSILON
            or target.scene_frame > frame_end + _FRAME_EPSILON
        ):
            raise ValueError(
                f"Scene frame {target.scene_frame:g} is outside active Correction "
                f"range {frame_start:g}-{frame_end:g}"
            )
        for bone_target in target.bones:
            bone = available.get(bone_target.bone_name)
            if bone is None:
                raise ValueError(
                    f'Correction target bone "{bone_target.bone_name}" does not exist'
                )
            if bone.rotation_mode != "QUATERNION":
                raise ValueError(
                    f'Set pose bone "{bone.name}" Rotation Mode to Quaternion before writing'
                )
            if bone_target.location is not None and bone_target.bone_name != _HIPS_BONE:
                raise ValueError("Correction location targets are supported only for Hips")
            active_constraints = tuple(
                constraint.name
                for constraint in bone.constraints
                if not bool(getattr(constraint, "mute", False))
                and float(getattr(constraint, "influence", 1.0)) > _FRAME_EPSILON
            )
            if active_constraints:
                joined = ", ".join(active_constraints)
                raise ValueError(
                    f'Correction target bone "{bone.name}" has active constraints: {joined}'
                )
            if _target_has_driver(
                armature,
                bone,
                include_location=bone_target.location is not None,
            ):
                raise ValueError(
                    f'Correction target bone "{bone.name}" has a driven target channel'
                )


def _validate_root_destination(
    armature: bpy.types.Object,
    stack: CorrectionLayerStack,
    targets: tuple[CorrectionFrameTarget, ...],
) -> None:
    """Require the shared world/local-equivalent Root transform invariant."""

    requested_paths: set[str] = set()
    for target in targets:
        if target.root_location is not None:
            requested_paths.add("location")
        if target.root_rotation_wxyz is not None:
            requested_paths.add("rotation_quaternion")
    errors = root_trajectory_safety_errors(
        armature,
        stack,
        requested_paths=requested_paths,
    )
    if errors:
        raise ValueError("; ".join(errors))


def _write_bone_targets(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    access: ActionChannelAccess,
    targets: tuple[CorrectionBoneTarget, ...],
    *,
    interpolation: str,
) -> None:
    """Assign and inverse-remap all compatible bones in one Blender key pass."""

    for target in targets:
        pose_bone = armature.pose.bones[target.bone_name]
        pose_bone.rotation_quaternion = target.local_rotation_wxyz
        if target.location is not None:
            pose_bone.location = target.location

    rotation_only = tuple(target for target in targets if target.location is None)
    location_targets = tuple(target for target in targets if target.location is not None)
    for group, include_location in (
        (rotation_only, False),
        (location_targets, True),
    ):
        if not group:
            continue
        selected_names = {target.bone_name for target in group}
        for pose_bone in armature.pose.bones:
            set_pose_bone_selected(pose_bone, pose_bone.name in selected_names)
        armature.data.bones.active = armature.data.bones[group[0].bone_name]
        bones = tuple(
            pose_bone
            for pose_bone in armature.pose.bones
            if pose_bone.name in selected_names
        )
        scene_frame = float(context.scene.frame_current) + float(
            context.scene.frame_subframe
        )
        action_frame = scene_to_correction_frame(armature, scene_frame)
        _capture_nla_remapped_pose_values(
            context,
            armature,
            bones,
            access,
            action_frame=action_frame,
            include_location=include_location,
            interpolation=interpolation,
        )


def _write_root_target(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    access: ActionChannelAccess,
    target: CorrectionFrameTarget,
    *,
    interpolation: str,
) -> None:
    scene_frame = float(context.scene.frame_current) + float(
        context.scene.frame_subframe
    )
    action_frame = scene_to_correction_frame(armature, scene_frame)
    if target.root_location is not None:
        armature.location = target.root_location
    if target.root_rotation_wxyz is not None:
        armature.rotation_quaternion = target.root_rotation_wxyz
    if target.root_location is not None and not armature.keyframe_insert(
        data_path="location",
        frame=scene_frame,
        group="Root",
    ):
        raise RuntimeError("Blender could not key Root trajectory location")
    if target.root_rotation_wxyz is not None and not armature.keyframe_insert(
        data_path="rotation_quaternion",
        frame=scene_frame,
        group="Root",
    ):
        raise RuntimeError("Blender could not key Root trajectory rotation")

    if target.root_location is not None:
        location = _complete_root_key_values(
            access,
            "location",
            3,
            action_frame,
        )
        insert_object_location_key(
            access,
            action_frame,
            location,
            interpolation=interpolation,
        )
    if target.root_rotation_wxyz is not None:
        rotation = _complete_root_key_values(
            access,
            "rotation_quaternion",
            4,
            action_frame,
        )
        insert_object_quaternion_key(
            access,
            action_frame,
            rotation,
            interpolation=interpolation,
        )


def _complete_root_key_values(
    access: ActionChannelAccess,
    data_path: str,
    size: int,
    frame: float,
) -> tuple[float, ...]:
    curves: dict[int, bpy.types.FCurve] = {}
    for curve in iter_action_fcurves(access.action, access.slot):
        index = int(curve.array_index)
        if curve.data_path == data_path and 0 <= index < size:
            if index in curves:
                raise RuntimeError(
                    f"Correction has duplicate Root {data_path} channel {index}"
                )
            curves[index] = curve
    if set(curves) != set(range(size)):
        raise RuntimeError(f"Blender did not create complete Root {data_path} channels")
    values: list[float] = []
    for index in range(size):
        matches = tuple(
            point
            for point in curves[index].keyframe_points
            if abs(float(point.co[0]) - frame) <= _FRAME_EPSILON
        )
        if len(matches) != 1:
            raise RuntimeError(
                f"Blender did not create one Root {data_path}[{index}] key "
                f"at Action frame {frame:g}"
            )
        value = float(matches[0].co[1])
        if not math.isfinite(value):
            raise RuntimeError(f"Blender created a non-finite Root {data_path} key")
        values.append(value)
    return tuple(values)


def _verify_written_targets(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    targets: tuple[CorrectionFrameTarget, ...],
    *,
    rotation_tolerance_degrees: float,
    location_tolerance: float,
) -> None:
    for target in targets:
        _set_scene_frame(context.scene, target.scene_frame)
        context.view_layer.update()
        depsgraph = context.evaluated_depsgraph_get()
        evaluated = armature.evaluated_get(depsgraph)
        if target.root_location is not None:
            actual_root_location = _vec3(
                evaluated.matrix_world.translation,
                "evaluated Root location",
            )
            root_error = math.dist(actual_root_location, target.root_location)
            if root_error > location_tolerance:
                raise RuntimeError(
                    "Root location verification failed at Scene frame "
                    f"{target.scene_frame:g}: {root_error:.6g}"
                )
        if target.root_rotation_wxyz is not None:
            actual_root_rotation = _quaternion(
                evaluated.matrix_world.to_quaternion(),
                "evaluated Root rotation",
            )
            root_rotation_error = _quaternion_error_degrees(
                actual_root_rotation,
                target.root_rotation_wxyz,
            )
            if root_rotation_error > rotation_tolerance_degrees:
                raise RuntimeError(
                    "Root rotation verification failed at Scene frame "
                    f"{target.scene_frame:g}: {root_rotation_error:.6g} degrees"
                )
        for expected in target.bones:
            actual_bone = evaluated.pose.bones.get(expected.bone_name)
            if actual_bone is None:
                raise RuntimeError(
                    f'Correction verification lost bone "{expected.bone_name}"'
                )
            local_matrix = _evaluated_local_matrix(actual_bone)
            actual_rotation = _quaternion(
                local_matrix.to_quaternion(), "evaluated local_rotation_wxyz"
            )
            error_degrees = _quaternion_error_degrees(
                actual_rotation,
                expected.local_rotation_wxyz,
            )
            if error_degrees > rotation_tolerance_degrees:
                raise RuntimeError(
                    f'Correction rotation verification failed for "{expected.bone_name}" '
                    f"at Scene frame {target.scene_frame:g}: {error_degrees:.6g} degrees"
                )
            if expected.location is not None:
                actual_location = _vec3(
                    local_matrix.translation,
                    "evaluated Hips location",
                )
                error = math.dist(actual_location, expected.location)
                if error > location_tolerance:
                    raise RuntimeError(
                        "Correction location verification failed for Hips at Scene frame "
                        f"{target.scene_frame:g}: {error:.6g}"
                    )


@contextmanager
def _writer_transform_transaction(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    bone_names: tuple[str, ...],
) -> Iterator[None]:
    """Restore assigned RNA transforms and revision if a write rolls back."""

    object_rotation_mode = str(armature.rotation_mode)
    object_matrix_basis = armature.matrix_basis.copy()
    bone_states = tuple(
        (
            bone,
            str(bone.rotation_mode),
            bone.matrix_basis.copy(),
        )
        for name in bone_names
        if (bone := armature.pose.bones.get(name)) is not None
    )
    had_revision = MOTION_REVISION_PROPERTY in armature
    revision = armature.get(MOTION_REVISION_PROPERTY)
    try:
        yield
    except Exception:
        armature.rotation_mode = object_rotation_mode
        armature.matrix_basis = object_matrix_basis
        for bone, rotation_mode, matrix_basis in bone_states:
            bone.rotation_mode = rotation_mode
            bone.matrix_basis = matrix_basis
        if had_revision:
            armature[MOTION_REVISION_PROPERTY] = revision
        elif MOTION_REVISION_PROPERTY in armature:
            del armature[MOTION_REVISION_PROPERTY]
        context.view_layer.update()
        raise


def _target_has_driver(
    armature: bpy.types.Object,
    pose_bone: bpy.types.PoseBone,
    *,
    include_location: bool,
) -> bool:
    animation_data = armature.animation_data
    if animation_data is None:
        return False
    target_paths = {pose_bone.path_from_id("rotation_quaternion")}
    if include_location:
        target_paths.add(pose_bone.path_from_id("location"))
    return any(driver.data_path in target_paths for driver in animation_data.drivers)


def _evaluated_local_matrix(pose_bone: bpy.types.PoseBone) -> object:
    bone = pose_bone.bone
    parent = pose_bone.parent
    if parent is None:
        return bone.convert_local_to_pose(
            pose_bone.matrix,
            bone.matrix_local,
            invert=True,
        )
    return bone.convert_local_to_pose(
        pose_bone.matrix,
        bone.matrix_local,
        parent_matrix=parent.matrix,
        parent_matrix_local=parent.bone.matrix_local,
        invert=True,
    )


@contextmanager
def _preserve_scene_frame(scene: bpy.types.Scene) -> Iterator[None]:
    frame = int(scene.frame_current)
    subframe = float(scene.frame_subframe)
    try:
        yield
    finally:
        scene.frame_set(frame, subframe=subframe)


def _set_scene_frame(scene: bpy.types.Scene, value: float) -> None:
    integer = math.floor(value)
    scene.frame_set(integer, subframe=value - integer)


def _quaternion_error_degrees(
    left: QuaternionWXYZ,
    right: QuaternionWXYZ,
) -> float:
    dot = abs(sum(a * b for a, b in zip(left, right, strict=True)))
    if dot >= 1.0 - 1.0e-12:
        return 0.0
    return math.degrees(2.0 * math.acos(max(-1.0, min(1.0, dot))))


def _interpolation(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("interpolation must be a string")
    result = value.upper()
    if result not in _SUPPORTED_INTERPOLATIONS:
        supported = ", ".join(sorted(_SUPPORTED_INTERPOLATIONS))
        raise ValueError(f"Correction interpolation must be one of: {supported}")
    return result


def _nonempty_name(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if not value or "\x00" in value or len(value) > 256:
        raise ValueError(f"{label} must contain 1 to 256 non-NUL characters")
    return value


def _finite_float(value: Any, label: str) -> float:
    if type(value) is bool or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{label} must be finite") from error
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _nonnegative_float(value: Any, label: str) -> float:
    result = _finite_float(value, label)
    if result < 0.0:
        raise ValueError(f"{label} must be non-negative")
    return result


def _components(value: Any, size: int, label: str) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{label} must be a numeric vector")
    try:
        result = tuple(_finite_float(component, label) for component in value)
    except TypeError as error:
        raise TypeError(f"{label} must be a numeric vector") from error
    if len(result) != size:
        raise ValueError(f"{label} must contain exactly {size} components")
    return result


def _vec3(value: Any, label: str) -> Vec3:
    result = _components(value, 3, label)
    return (result[0], result[1], result[2])


def _quaternion(value: Any, label: str) -> QuaternionWXYZ:
    result = _components(value, 4, label)
    norm = math.hypot(*result)
    if not math.isfinite(norm) or norm <= 1.0e-12:
        raise ValueError(f"{label} must have a non-zero finite norm")
    return (
        result[0] / norm,
        result[1] / norm,
        result[2] / norm,
        result[3] / norm,
    )


def _same_rna(left: object, right: object) -> bool:
    try:
        return int(left.as_pointer()) == int(right.as_pointer())  # type: ignore[attr-defined]
    except (AttributeError, ReferenceError, TypeError):
        return False


__all__ = (
    "CorrectionBoneTarget",
    "CorrectionFrameTarget",
    "CorrectionWriteResult",
    "MAX_CORRECTION_WRITE_BONE_SAMPLES",
    "MAX_CORRECTION_WRITE_FRAMES",
    "MOTION_REVISION_PROPERTY",
    "write_correction_target",
    "write_correction_targets",
)
