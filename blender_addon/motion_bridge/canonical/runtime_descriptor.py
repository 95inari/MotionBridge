from __future__ import annotations

from typing import Any

import bpy
from mathutils import Matrix, Vector

from .descriptor import skeleton_hash_hex, validate_descriptor
from .rig_spec import JOINTS, SKELETON_ID, joint_path
from ..live.pose_sampler import validate_canonical_armature


_FBX_LOCAL_HANDEDNESS = Matrix(
    (
        (-1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
)
_FBX_ROOT_TO_UNITY = Matrix(
    (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, -1.0, 0.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
)


def descriptor_from_armature(
    armature: bpy.types.Object,
    *,
    session_id: int = 0,
    request_id: int = 0,
) -> dict[str, Any]:
    """Build the wire descriptor from the same Armature sampled for live pose."""

    validate_canonical_armature(armature)
    _require_uint32(session_id, "session_id")
    _require_uint32(request_id, "request_id")
    root_rotation = _quaternion(_FBX_ROOT_TO_UNITY)
    joints: list[dict[str, Any]] = [
        {
            "index": 0,
            "name": "Root",
            "humanBone": None,
            "parentIndex": -1,
            "jointPath": "",
            "required": True,
            "restLocalPosition": [0.0, 0.0, 0.0],
            "restLocalRotation": root_rotation,
            "restGlobalRotation": root_rotation,
        }
    ]
    for joint in JOINTS[1:]:
        bone = armature.data.bones[joint.name]
        blender_global = bone.matrix_local.copy()
        blender_local = (
            blender_global
            if bone.parent is None
            else bone.parent.matrix_local.inverted_safe() @ blender_global
        )
        local_matrix = (
            _FBX_LOCAL_HANDEDNESS @ blender_local @ _FBX_LOCAL_HANDEDNESS
        )
        global_matrix = (
            _FBX_ROOT_TO_UNITY
            @ _FBX_LOCAL_HANDEDNESS
            @ blender_global
            @ _FBX_LOCAL_HANDEDNESS
        )
        joints.append(
            {
                "index": joint.index,
                "name": joint.name,
                "humanBone": joint.human_bone,
                "parentIndex": joint.parent_index,
                "jointPath": joint_path(joint.index),
                "required": joint.required,
                "restLocalPosition": _vector(local_matrix.to_translation()),
                "restLocalRotation": _quaternion(local_matrix),
                "restGlobalRotation": _quaternion(global_matrix),
            }
        )

    descriptor: dict[str, Any] = {
        "type": "skeleton_descriptor",
        "protocolMajor": 1,
        "protocolMinor": 0,
        "sessionId": session_id,
        "requestId": request_id,
        "skeletonId": SKELETON_ID,
        "coordinateSpace": "unity-left-handed-y-up-z-forward",
        "rootIndex": 0,
        "joints": joints,
    }
    descriptor["skeletonHash"] = skeleton_hash_hex(descriptor)
    validate_descriptor(descriptor)
    return descriptor


def _vector(value: Vector) -> list[float]:
    return [float(value.x), float(value.y), float(value.z)]


def _quaternion(matrix: Matrix) -> list[float]:
    value = matrix.to_quaternion().normalized()
    return [float(value.x), float(value.y), float(value.z), float(value.w)]


def _require_uint32(value: int, name: str) -> None:
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"{name} must be a uint32")
