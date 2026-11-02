from __future__ import annotations

from dataclasses import dataclass
import math
import threading
from typing import Final

import bpy

from ..canonical.rig_spec import JOINTS, SKELETON_ID
from .basis_v1 import (
    Matrix3,
    blender_to_unity_vector,
    global_rest_relative_delta,
    hips_local_delta_to_unity,
    trajectory_rotation_to_unity,
)


IDENTITY_QUATERNION: Final = (0.0, 0.0, 0.0, 1.0)
JOINT_NAMES: Final = tuple(joint.name for joint in JOINTS)
PHYSICAL_JOINT_NAMES: Final = JOINT_NAMES[1:]


@dataclass(slots=True)
class PoseSample:
    """Mutable sampling buffers reused by :class:`CanonicalPoseSampler`."""

    root_position: list[float]
    root_rotation: list[float]
    hips_local_position_delta: list[float]
    joint_rotations: list[list[float]]
    frame_index: int = 0


def validate_canonical_armature(armature: bpy.types.Object) -> None:
    if armature.type != "ARMATURE":
        raise ValueError("Live pose source must be an armature object")
    if armature.get("motion_bridge_skeleton_id") != SKELETON_ID:
        raise ValueError("Live pose source is not the Motion Bridge Canonical skeleton")
    if not _matrix_is_rigid_transform(armature.matrix_world):
        raise ValueError(
            "Canonical armature matrix_world must have finite translation, rotation, and unit scale"
        )

    actual_names = set(armature.data.bones.keys())
    expected_names = set(PHYSICAL_JOINT_NAMES)
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        raise ValueError(f"Canonical bone set mismatch; missing={missing}, extra={extra}")
    for joint in JOINTS[1:]:
        bone = armature.data.bones[joint.name]
        expected_parent = None if joint.parent_index == 0 else JOINTS[joint.parent_index].name
        actual_parent = bone.parent.name if bone.parent is not None else None
        if actual_parent != expected_parent:
            raise ValueError(
                f"Canonical parent mismatch for {joint.name}: "
                f"expected={expected_parent}, actual={actual_parent}"
            )


class CanonicalPoseSampler:
    """Main-thread evaluated-pose sampler with reusable output buffers."""

    def __init__(self, armature: bpy.types.Object) -> None:
        _require_main_thread()
        validate_canonical_armature(armature)
        self._armature = armature
        self._session_anchor_inverse = armature.matrix_world.inverted_safe()
        self._rest_rotations: tuple[Matrix3 | None, ...] = (None,) + tuple(
            _rotation_matrix3(armature.data.bones[joint.name].matrix_local)
            for joint in JOINTS[1:]
        )
        hips_rest = armature.data.bones[JOINTS[1].name].matrix_local.translation
        self._hips_rest_translation = (
            float(hips_rest.x),
            float(hips_rest.y),
            float(hips_rest.z),
        )
        self._previous_root_rotation: tuple[float, float, float, float] | None = None
        self._previous_joint_rotations: list[tuple[float, float, float, float] | None] = [
            None
        ] * len(JOINTS)
        self._sample = PoseSample(
            root_position=[0.0, 0.0, 0.0],
            root_rotation=[0.0, 0.0, 0.0, 1.0],
            hips_local_position_delta=[0.0, 0.0, 0.0],
            joint_rotations=[[0.0, 0.0, 0.0, 1.0] for _joint in JOINTS],
        )

    @property
    def armature(self) -> bpy.types.Object:
        return self._armature

    def sample(self, scene: bpy.types.Scene) -> PoseSample:
        """Sample once; the returned object aliases buffers reused next call."""

        _require_main_thread()
        if scene is not bpy.context.scene:
            raise ValueError("scene must be the active Blender scene")
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = self._armature.evaluated_get(depsgraph)
        relative_object = self._session_anchor_inverse @ evaluated.matrix_world

        root_position = blender_to_unity_vector(relative_object.translation)
        root_rotation = trajectory_rotation_to_unity(_rotation_matrix3(relative_object))
        root_rotation = _continuous_quaternion(root_rotation, self._previous_root_rotation)
        self._previous_root_rotation = root_rotation
        _copy_vector(self._sample.root_position, root_position)
        _copy_quaternion(self._sample.root_rotation, root_rotation)

        _copy_quaternion(self._sample.joint_rotations[0], IDENTITY_QUATERNION)
        self._previous_joint_rotations[0] = IDENTITY_QUATERNION
        for joint in JOINTS[1:]:
            pose_bone = evaluated.pose.bones.get(joint.name)
            if pose_bone is None:
                raise ValueError(f"Evaluated Canonical pose is missing bone: {joint.name}")
            rest_rotation = self._rest_rotations[joint.index]
            assert rest_rotation is not None
            rotation = global_rest_relative_delta(
                _rotation_matrix3(pose_bone.matrix),
                rest_rotation,
            )
            rotation = _continuous_quaternion(
                rotation,
                self._previous_joint_rotations[joint.index],
            )
            self._previous_joint_rotations[joint.index] = rotation
            _copy_quaternion(self._sample.joint_rotations[joint.index], rotation)

        hips_pose = evaluated.pose.bones[JOINTS[1].name].matrix.translation
        hips_delta_blender = (
            float(hips_pose.x) - self._hips_rest_translation[0],
            float(hips_pose.y) - self._hips_rest_translation[1],
            float(hips_pose.z) - self._hips_rest_translation[2],
        )
        hips_delta = hips_local_delta_to_unity(hips_delta_blender)
        _copy_vector(self._sample.hips_local_position_delta, hips_delta)
        self._sample.frame_index = int(scene.frame_current)
        return self._sample


def _rotation_matrix3(matrix: object) -> Matrix3:
    quaternion = matrix.to_quaternion().normalized()  # type: ignore[attr-defined]
    rotation = quaternion.to_matrix()
    return tuple(
        tuple(float(rotation[row][column]) for column in range(3)) for row in range(3)
    )  # type: ignore[return-value]


def _continuous_quaternion(
    value: tuple[float, float, float, float],
    previous: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float]:
    if previous is not None and sum(a * b for a, b in zip(value, previous)) < 0.0:
        return tuple(-component for component in value)  # type: ignore[return-value]
    return value


def _copy_vector(target: list[float], value: tuple[float, float, float]) -> None:
    target[0], target[1], target[2] = value


def _copy_quaternion(
    target: list[float], value: tuple[float, float, float, float]
) -> None:
    target[0], target[1], target[2], target[3] = value


def _matrix_is_rigid_transform(matrix: object, tolerance: float = 1e-6) -> bool:
    values = tuple(
        tuple(float(matrix[row][column]) for column in range(4))  # type: ignore[index]
        for row in range(4)
    )
    if not all(math.isfinite(component) for row in values for component in row):
        return False
    if any(abs(values[3][column]) > tolerance for column in range(3)):
        return False
    if abs(values[3][3] - 1.0) > tolerance:
        return False

    columns = tuple(
        tuple(values[row][column] for row in range(3)) for column in range(3)
    )
    for index, column in enumerate(columns):
        norm_squared = sum(component * component for component in column)
        if abs(norm_squared - 1.0) > tolerance:
            return False
        for other in columns[index + 1 :]:
            if abs(sum(left * right for left, right in zip(column, other))) > tolerance:
                return False
    determinant = (
        columns[0][0]
        * (columns[1][1] * columns[2][2] - columns[1][2] * columns[2][1])
        - columns[1][0]
        * (columns[0][1] * columns[2][2] - columns[0][2] * columns[2][1])
        + columns[2][0]
        * (columns[0][1] * columns[1][2] - columns[0][2] * columns[1][1])
    )
    return abs(determinant - 1.0) <= tolerance


def _require_main_thread() -> None:
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Canonical pose sampling must run on Blender's main thread")
