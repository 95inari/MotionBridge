from __future__ import annotations

import math
from typing import Sequence


Vector3 = tuple[float, float, float]
Quaternion = tuple[float, float, float, float]
Matrix3 = tuple[Vector3, Vector3, Vector3]

# F = FBX right-handed local basis to Unity left-handed local basis.
FBX_LOCAL_HANDEDNESS: Matrix3 = (
    (-1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
    (0.0, 0.0, 1.0),
)

# R = the -90 degree X axis rotation introduced at the imported FBX root.
FBX_ROOT_TO_UNITY: Matrix3 = (
    (1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0),
    (0.0, -1.0, 0.0),
)

# B = R * F, the fixed Blender world/armature to Unity world/session basis.
BLENDER_TO_UNITY: Matrix3 = (
    (-1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0),
    (0.0, -1.0, 0.0),
)


def matrix3_multiply(left: Sequence[Sequence[float]], right: Sequence[Sequence[float]]) -> Matrix3:
    return tuple(
        tuple(
            sum(float(left[row][inner]) * float(right[inner][column]) for inner in range(3))
            for column in range(3)
        )
        for row in range(3)
    )  # type: ignore[return-value]


def matrix3_transpose(value: Sequence[Sequence[float]]) -> Matrix3:
    return tuple(
        tuple(float(value[column][row]) for column in range(3)) for row in range(3)
    )  # type: ignore[return-value]


def transform_vector(matrix: Sequence[Sequence[float]], vector: Sequence[float]) -> Vector3:
    if len(vector) != 3:
        raise ValueError("Vector must contain three components")
    return tuple(
        sum(float(matrix[row][column]) * float(vector[column]) for column in range(3))
        for row in range(3)
    )  # type: ignore[return-value]


def blender_to_unity_vector(vector: Sequence[float]) -> Vector3:
    """Convert a Blender world/session vector with the fixed basis B = R * F."""

    return transform_vector(BLENDER_TO_UNITY, vector)


def hips_local_delta_to_unity(vector: Sequence[float]) -> Vector3:
    """Convert a Hips translation in its FBX-root parent space using F only."""

    return transform_vector(FBX_LOCAL_HANDEDNESS, vector)


def normalize_quaternion(value: Sequence[float]) -> Quaternion:
    if len(value) != 4 or not all(math.isfinite(float(component)) for component in value):
        raise ValueError("Quaternion must contain four finite components")
    scale = max(abs(float(component)) for component in value)
    if scale <= 1e-15:
        raise ValueError("Quaternion must not be zero")
    scaled = tuple(float(component) / scale for component in value)
    inverse_norm = 1.0 / math.sqrt(sum(component * component for component in scaled))
    normalized = tuple(component * inverse_norm for component in scaled)
    if normalized[3] < 0.0:
        normalized = tuple(-component for component in normalized)
    return normalized  # type: ignore[return-value]


def quaternion_to_matrix3(value: Sequence[float]) -> Matrix3:
    x, y, z, w = normalize_quaternion(value)
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return (
        (1.0 - 2.0 * (yy + zz), 2.0 * (xy - wz), 2.0 * (xz + wy)),
        (2.0 * (xy + wz), 1.0 - 2.0 * (xx + zz), 2.0 * (yz - wx)),
        (2.0 * (xz - wy), 2.0 * (yz + wx), 1.0 - 2.0 * (xx + yy)),
    )


def matrix3_to_quaternion(value: Sequence[Sequence[float]]) -> Quaternion:
    m00, m01, m02 = (float(value[0][column]) for column in range(3))
    m10, m11, m12 = (float(value[1][column]) for column in range(3))
    m20, m21, m22 = (float(value[2][column]) for column in range(3))
    trace = m00 + m11 + m22
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        quaternion = ((m21 - m12) / scale, (m02 - m20) / scale, (m10 - m01) / scale, 0.25 * scale)
    elif m00 > m11 and m00 > m22:
        scale = math.sqrt(max(0.0, 1.0 + m00 - m11 - m22)) * 2.0
        quaternion = (0.25 * scale, (m01 + m10) / scale, (m02 + m20) / scale, (m21 - m12) / scale)
    elif m11 > m22:
        scale = math.sqrt(max(0.0, 1.0 + m11 - m00 - m22)) * 2.0
        quaternion = ((m01 + m10) / scale, 0.25 * scale, (m12 + m21) / scale, (m02 - m20) / scale)
    else:
        scale = math.sqrt(max(0.0, 1.0 + m22 - m00 - m11)) * 2.0
        quaternion = ((m02 + m20) / scale, (m12 + m21) / scale, 0.25 * scale, (m10 - m01) / scale)
    return normalize_quaternion(quaternion)


def conjugate_rotation(
    rotation: Sequence[Sequence[float]], basis: Sequence[Sequence[float]]
) -> Matrix3:
    return matrix3_multiply(
        matrix3_multiply(basis, rotation),
        matrix3_transpose(basis),
    )


def global_rest_relative_delta(
    pose_global_blender: Sequence[Sequence[float]],
    rest_global_blender: Sequence[Sequence[float]],
) -> Quaternion:
    """Return ``B * pose * inverse(rest) * inverse(B)`` as `[x,y,z,w]`."""

    delta_blender = matrix3_multiply(
        pose_global_blender,
        matrix3_transpose(rest_global_blender),
    )
    return matrix3_to_quaternion(conjugate_rotation(delta_blender, BLENDER_TO_UNITY))


def trajectory_rotation_to_unity(
    relative_rotation_blender: Sequence[Sequence[float]],
) -> Quaternion:
    return matrix3_to_quaternion(
        conjugate_rotation(relative_rotation_blender, BLENDER_TO_UNITY)
    )
