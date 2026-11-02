"""Blender-independent humanoid joint-limit correction.

The Canonical rig points each bone along local +Y.  Limits are therefore
expressed as a direction-independent swing cone plus a signed twist interval
around local +Y.  This avoids Euler-order discontinuities and keeps the solver
usable by background tests and future non-Blender front ends.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Final, Iterable, Mapping

from .motion_quality import MotionSequence, QuaternionWXYZ, validate_motion_sequence


SOLVE_PROFILE_SCHEMA: Final = "motion-bridge.solve-profile/1"
MAX_SOLVE_FRAMES: Final = 20_000
MAX_SOLVE_BONE_SAMPLES: Final = 2_000_000
_EPSILON: Final = 1.0e-12


class SolveProfileError(ValueError):
    """Raised when a joint-limit profile is malformed or unsafe."""


@dataclass(frozen=True, slots=True)
class JointLimit:
    """One Canonical bone's signed local-space swing/twist limits."""

    bone_name: str
    max_swing_degrees: float
    min_twist_degrees: float
    max_twist_degrees: float
    soft_zone_degrees: float = 5.0
    min_swing_x_degrees: float | None = None
    max_swing_x_degrees: float | None = None
    min_swing_z_degrees: float | None = None
    max_swing_z_degrees: float | None = None

    def __post_init__(self) -> None:
        name = _name(self.bone_name, "joint-limit bone")
        swing = _finite(self.max_swing_degrees, f"{name} max swing")
        twist_min = _finite(self.min_twist_degrees, f"{name} minimum twist")
        twist_max = _finite(self.max_twist_degrees, f"{name} maximum twist")
        soft_zone = _finite(self.soft_zone_degrees, f"{name} soft zone")
        if not 0.0 < swing < 180.0:
            raise SolveProfileError(f"{name} max swing must be within (0, 180) degrees")
        if not -180.0 <= twist_min <= twist_max <= 180.0:
            raise SolveProfileError(
                f"{name} twist limits must be ordered within [-180, 180] degrees"
            )
        if soft_zone < 0.0:
            raise SolveProfileError(f"{name} soft zone must be non-negative")
        if soft_zone >= swing:
            raise SolveProfileError(f"{name} soft zone must be smaller than max swing")
        if twist_max > twist_min and soft_zone * 2.0 >= twist_max - twist_min:
            raise SolveProfileError(
                f"{name} soft zone must be less than half the twist interval"
            )
        swing_x_min, swing_x_max = _swing_interval(
            self.min_swing_x_degrees,
            self.max_swing_x_degrees,
            swing,
            soft_zone,
            f"{name} X swing",
        )
        swing_z_min, swing_z_max = _swing_interval(
            self.min_swing_z_degrees,
            self.max_swing_z_degrees,
            swing,
            soft_zone,
            f"{name} Z swing",
        )
        object.__setattr__(self, "bone_name", name)
        object.__setattr__(self, "max_swing_degrees", swing)
        object.__setattr__(self, "min_twist_degrees", twist_min)
        object.__setattr__(self, "max_twist_degrees", twist_max)
        object.__setattr__(self, "soft_zone_degrees", soft_zone)
        object.__setattr__(self, "min_swing_x_degrees", swing_x_min)
        object.__setattr__(self, "max_swing_x_degrees", swing_x_max)
        object.__setattr__(self, "min_swing_z_degrees", swing_z_min)
        object.__setattr__(self, "max_swing_z_degrees", swing_z_max)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bone": self.bone_name,
            "maxSwingDegrees": self.max_swing_degrees,
            "maxSwingXDegrees": self.max_swing_x_degrees,
            "maxSwingZDegrees": self.max_swing_z_degrees,
            "maxTwistDegrees": self.max_twist_degrees,
            "minSwingXDegrees": self.min_swing_x_degrees,
            "minSwingZDegrees": self.min_swing_z_degrees,
            "minTwistDegrees": self.min_twist_degrees,
            "softZoneDegrees": self.soft_zone_degrees,
        }


@dataclass(frozen=True, slots=True)
class SolveProfile:
    """Versioned, deterministic joint-limit profile."""

    name: str
    limits: tuple[JointLimit, ...]

    def __post_init__(self) -> None:
        name = _name(self.name, "solve-profile name")
        try:
            limits = tuple(self.limits)
        except TypeError as error:
            raise TypeError("solve-profile limits must be iterable") from error
        if not limits:
            raise SolveProfileError("solve-profile must contain at least one joint limit")
        if not all(isinstance(limit, JointLimit) for limit in limits):
            raise TypeError("solve-profile limits must contain JointLimit values")
        normalized = tuple(sorted(limits, key=lambda limit: limit.bone_name))
        if len({limit.bone_name for limit in normalized}) != len(normalized):
            raise SolveProfileError("solve-profile contains duplicate bones")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "limits", normalized)

    @property
    def limits_by_bone(self) -> dict[str, JointLimit]:
        return {limit.bone_name: limit for limit in self.limits}

    def to_dict(self) -> dict[str, Any]:
        return {
            "limits": [limit.to_dict() for limit in self.limits],
            "name": self.name,
            "schema": SOLVE_PROFILE_SCHEMA,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, value: object) -> SolveProfile:
        if not isinstance(value, str) or not value:
            raise SolveProfileError("solve-profile JSON is missing")
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as error:
            raise SolveProfileError("solve-profile is not valid JSON") from error
        if not isinstance(payload, Mapping):
            raise SolveProfileError("solve-profile must be a JSON object")
        if payload.get("schema") != SOLVE_PROFILE_SCHEMA:
            raise SolveProfileError("solve-profile schema is unsupported")
        raw_limits = payload.get("limits")
        if not isinstance(raw_limits, list):
            raise SolveProfileError("solve-profile limits must be a JSON array")
        limits: list[JointLimit] = []
        for index, raw_limit in enumerate(raw_limits):
            if not isinstance(raw_limit, Mapping):
                raise SolveProfileError(f"solve-profile limit {index} must be an object")
            expected_keys = {
                "bone",
                "maxSwingDegrees",
                "maxSwingXDegrees",
                "maxSwingZDegrees",
                "maxTwistDegrees",
                "minSwingXDegrees",
                "minSwingZDegrees",
                "minTwistDegrees",
                "softZoneDegrees",
            }
            if set(raw_limit) != expected_keys:
                raise SolveProfileError(
                    f"solve-profile limit {index} has unknown or missing fields"
                )
            limits.append(
                JointLimit(
                    bone_name=raw_limit["bone"],
                    max_swing_degrees=raw_limit["maxSwingDegrees"],
                    min_twist_degrees=raw_limit["minTwistDegrees"],
                    max_twist_degrees=raw_limit["maxTwistDegrees"],
                    soft_zone_degrees=raw_limit["softZoneDegrees"],
                    min_swing_x_degrees=raw_limit["minSwingXDegrees"],
                    max_swing_x_degrees=raw_limit["maxSwingXDegrees"],
                    min_swing_z_degrees=raw_limit["minSwingZDegrees"],
                    max_swing_z_degrees=raw_limit["maxSwingZDegrees"],
                )
            )
        if set(payload) != {"limits", "name", "schema"}:
            raise SolveProfileError("solve-profile has unknown or missing fields")
        return cls(name=payload.get("name"), limits=tuple(limits))


@dataclass(frozen=True, slots=True)
class JointLimitFrame:
    frame: float
    local_rotations_wxyz: tuple[tuple[str, QuaternionWXYZ], ...]


@dataclass(frozen=True, slots=True)
class JointLimitResult:
    profile_name: str
    frames: tuple[JointLimitFrame, ...]
    limited_bone_names: tuple[str, ...]
    corrected_sample_count: int
    maximum_change_degrees: float


def _limit(
    bone: str,
    swing: float,
    twist_min: float,
    twist_max: float,
    soft: float = 5.0,
    *,
    swing_x: tuple[float, float] | None = None,
    swing_z: tuple[float, float] | None = None,
) -> JointLimit:
    return JointLimit(
        bone,
        swing,
        twist_min,
        twist_max,
        soft,
        *(swing_x or (None, None)),
        *(swing_z or (None, None)),
    )


def limit_motion_joints(
    sequence: MotionSequence,
    *,
    profile: SolveProfile | None = None,
    max_frames: int = MAX_SOLVE_FRAMES,
    max_bone_samples: int = MAX_SOLVE_BONE_SAMPLES,
    preserve_endpoints: bool = True,
) -> JointLimitResult:
    """Soft-limit local bone rotations without mutating ``sequence``.

    Every input bone is returned. Bones absent from ``profile`` keep the same
    represented rotation. Output quaternion signs are made continuous per bone.
    """

    if not isinstance(sequence, MotionSequence):
        raise TypeError("sequence must be a MotionSequence")
    if profile is None:
        profile = DEFAULT_SOLVE_PROFILE
    if not isinstance(profile, SolveProfile):
        raise TypeError("profile must be a SolveProfile")
    if type(preserve_endpoints) is not bool:
        raise TypeError("preserve_endpoints must be a boolean")
    validate_motion_sequence(sequence)
    frame_limit = _positive_integer(max_frames, "maximum solve frames")
    sample_limit = _positive_integer(max_bone_samples, "maximum bone samples")
    if not sequence.frames:
        raise ValueError("joint-limit correction requires at least one motion frame")
    if len(sequence.frames) > frame_limit:
        raise ValueError(
            f"joint-limit correction has {len(sequence.frames)} frames; "
            f"maximum is {frame_limit}"
        )
    sample_count = sum(len(frame.bones) for frame in sequence.frames)
    if sample_count > sample_limit:
        raise ValueError(
            f"joint-limit correction has {sample_count} bone samples; "
            f"maximum is {sample_limit}"
        )

    expected_bones = tuple(bone.name for bone in sequence.frames[0].bones)
    previous_frame: float | None = None
    previous_time: float | None = None
    for frame in sequence.frames:
        if previous_frame is not None and frame.frame <= previous_frame:
            raise ValueError("joint-limit frames must be strictly increasing")
        if previous_time is not None and frame.time_seconds <= previous_time:
            raise ValueError("joint-limit times must be strictly increasing")
        if tuple(bone.name for bone in frame.bones) != expected_bones:
            raise ValueError("joint-limit frames must contain the same ordered bones")
        previous_frame = frame.frame
        previous_time = frame.time_seconds

    limits = profile.limits_by_bone
    available_names = set(expected_bones)
    limited_names = tuple(sorted(set(limits).intersection(available_names)))
    previous: dict[str, QuaternionWXYZ] = {}
    corrected_count = 0
    maximum_change = 0.0
    output_frames: list[JointLimitFrame] = []
    final_index = len(sequence.frames) - 1
    for frame_index, frame in enumerate(sequence.frames):
        rotations: list[tuple[str, QuaternionWXYZ]] = []
        for bone in frame.bones:
            original = _normalize_quaternion(bone.local_rotation_wxyz)
            limit = limits.get(bone.name)
            preserve = preserve_endpoints and frame_index in {0, final_index}
            corrected = (
                original
                if limit is None or preserve
                else _limit_quaternion(original, limit)
            )
            reference = previous.get(bone.name)
            if reference is not None and _dot(reference, corrected) < 0.0:
                corrected = _negate(corrected)
            previous[bone.name] = corrected
            change = _quaternion_distance_degrees(original, corrected)
            if limit is not None and change > 1.0e-7:
                corrected_count += 1
                maximum_change = max(maximum_change, change)
            rotations.append((bone.name, corrected))
        output_frames.append(
            JointLimitFrame(
                frame=frame.frame,
                local_rotations_wxyz=tuple(rotations),
            )
        )
    return JointLimitResult(
        profile_name=profile.name,
        frames=tuple(output_frames),
        limited_bone_names=limited_names,
        corrected_sample_count=corrected_count,
        maximum_change_degrees=maximum_change,
    )


def _limit_quaternion(
    quaternion: QuaternionWXYZ,
    limit: JointLimit,
) -> QuaternionWXYZ:
    swing, twist = _swing_twist_y(quaternion)
    swing_x, swing_z = _swing_vector_xz_degrees(swing)
    limited_x = _soft_interval_limit(
        swing_x,
        limit.min_swing_x_degrees,
        limit.max_swing_x_degrees,
        limit.soft_zone_degrees,
    )
    limited_z = _soft_interval_limit(
        swing_z,
        limit.min_swing_z_degrees,
        limit.max_swing_z_degrees,
        limit.soft_zone_degrees,
    )
    swing_angle = math.hypot(limited_x, limited_z)
    limited_swing_angle = _soft_upper_limit(
        swing_angle,
        limit.max_swing_degrees,
        limit.soft_zone_degrees,
    )
    if swing_angle > _EPSILON and limited_swing_angle < swing_angle:
        ratio = limited_swing_angle / swing_angle
        limited_x *= ratio
        limited_z *= ratio
    swing = _swing_from_xz_degrees(limited_x, limited_z)

    twist_angle = _signed_twist_y_degrees(twist)
    limited_twist_angle = _soft_interval_limit(
        twist_angle,
        limit.min_twist_degrees,
        limit.max_twist_degrees,
        limit.soft_zone_degrees,
    )
    twist = _axis_angle_y(limited_twist_angle)
    return _normalize_quaternion(_multiply(swing, twist))


def _soft_upper_limit(value: float, maximum: float, zone: float) -> float:
    if zone <= 0.0:
        return min(value, maximum)
    start = maximum - zone
    if value <= start:
        return value
    return start + zone * (1.0 - math.exp(-(value - start) / zone))


def _soft_interval_limit(
    value: float,
    minimum: float,
    maximum: float,
    zone: float,
) -> float:
    if zone <= 0.0:
        return min(maximum, max(minimum, value))
    lower_end = minimum + zone
    upper_start = maximum - zone
    if value < lower_end:
        return lower_end - zone * (1.0 - math.exp(-(lower_end - value) / zone))
    if value > upper_start:
        return upper_start + zone * (1.0 - math.exp(-(value - upper_start) / zone))
    return value


def _swing_twist_y(
    quaternion: QuaternionWXYZ,
) -> tuple[QuaternionWXYZ, QuaternionWXYZ]:
    w, x, y, z = _normalize_quaternion(quaternion)
    twist_norm = math.hypot(w, y)
    twist = (
        (1.0, 0.0, 0.0, 0.0)
        if twist_norm <= _EPSILON
        else (w / twist_norm, 0.0, y / twist_norm, 0.0)
    )
    swing = _normalize_quaternion(_multiply((w, x, y, z), _conjugate(twist)))
    if swing[0] < 0.0:
        swing = _negate(swing)
    return swing, twist


def _swing_vector_xz_degrees(
    quaternion: QuaternionWXYZ,
) -> tuple[float, float]:
    value = _normalize_quaternion(quaternion)
    if value[0] < 0.0:
        value = _negate(value)
    vector_norm = math.hypot(value[1], value[3])
    if vector_norm <= _EPSILON:
        return 0.0, 0.0
    angle = math.degrees(2.0 * math.atan2(vector_norm, value[0]))
    return angle * value[1] / vector_norm, angle * value[3] / vector_norm


def _swing_from_xz_degrees(x_degrees: float, z_degrees: float) -> QuaternionWXYZ:
    angle = math.hypot(x_degrees, z_degrees)
    if angle <= _EPSILON:
        return (1.0, 0.0, 0.0, 0.0)
    half = math.radians(angle) * 0.5
    scale = math.sin(half) / angle
    return _normalize_quaternion(
        (math.cos(half), x_degrees * scale, 0.0, z_degrees * scale)
    )


def _signed_twist_y_degrees(quaternion: QuaternionWXYZ) -> float:
    value = _normalize_quaternion(quaternion)
    angle = math.degrees(2.0 * math.atan2(value[2], value[0]))
    return (angle + 180.0) % 360.0 - 180.0


def _axis_angle_y(angle_degrees: float) -> QuaternionWXYZ:
    half = math.radians(angle_degrees) * 0.5
    return (math.cos(half), 0.0, math.sin(half), 0.0)


def _quaternion_power_shortest(
    quaternion: QuaternionWXYZ,
    exponent: float,
) -> QuaternionWXYZ:
    value = _normalize_quaternion(quaternion)
    if value[0] < 0.0:
        value = _negate(value)
    half_angle = math.acos(max(-1.0, min(1.0, value[0])))
    vector_norm = math.sqrt(sum(component * component for component in value[1:]))
    if half_angle <= _EPSILON or vector_norm <= _EPSILON:
        return (1.0, 0.0, 0.0, 0.0)
    scale = math.sin(half_angle * exponent) / vector_norm
    return _normalize_quaternion(
        (
            math.cos(half_angle * exponent),
            value[1] * scale,
            value[2] * scale,
            value[3] * scale,
        )
    )


def _quaternion_angle_degrees(quaternion: QuaternionWXYZ) -> float:
    value = _normalize_quaternion(quaternion)
    return math.degrees(2.0 * math.acos(max(-1.0, min(1.0, abs(value[0])))))


def _quaternion_distance_degrees(
    left: QuaternionWXYZ,
    right: QuaternionWXYZ,
) -> float:
    dot = abs(_dot(_normalize_quaternion(left), _normalize_quaternion(right)))
    return math.degrees(2.0 * math.acos(max(-1.0, min(1.0, dot))))


def _multiply(
    left: QuaternionWXYZ,
    right: QuaternionWXYZ,
) -> QuaternionWXYZ:
    lw, lx, ly, lz = left
    rw, rx, ry, rz = right
    return (
        lw * rw - lx * rx - ly * ry - lz * rz,
        lw * rx + lx * rw + ly * rz - lz * ry,
        lw * ry - lx * rz + ly * rw + lz * rx,
        lw * rz + lx * ry - ly * rx + lz * rw,
    )


def _conjugate(value: QuaternionWXYZ) -> QuaternionWXYZ:
    return (value[0], -value[1], -value[2], -value[3])


def _normalize_quaternion(value: Iterable[float]) -> QuaternionWXYZ:
    try:
        components = tuple(float(component) for component in value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("joint rotation must contain finite WXYZ values") from error
    if len(components) != 4 or not all(math.isfinite(component) for component in components):
        raise ValueError("joint rotation must contain finite WXYZ values")
    norm = math.hypot(*components)
    if norm <= _EPSILON:
        raise ValueError("joint rotation quaternion must have non-zero norm")
    return (
        components[0] / norm,
        components[1] / norm,
        components[2] / norm,
        components[3] / norm,
    )


def _dot(left: QuaternionWXYZ, right: QuaternionWXYZ) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _negate(value: QuaternionWXYZ) -> QuaternionWXYZ:
    return (-value[0], -value[1], -value[2], -value[3])


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SolveProfileError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise SolveProfileError(f"{label} must be finite")
    return result


def _swing_interval(
    minimum: object,
    maximum: object,
    cone: float,
    soft_zone: float,
    label: str,
) -> tuple[float, float]:
    if minimum is None and maximum is None:
        return -cone, cone
    if minimum is None or maximum is None:
        raise SolveProfileError(f"{label} requires both minimum and maximum")
    checked_minimum = _finite(minimum, f"{label} minimum")
    checked_maximum = _finite(maximum, f"{label} maximum")
    if not -180.0 <= checked_minimum <= 0.0 <= checked_maximum <= 180.0:
        raise SolveProfileError(
            f"{label} must be ordered within [-180, 180] and contain zero"
        )
    if soft_zone > 0.0 and soft_zone * 2.0 >= checked_maximum - checked_minimum:
        raise SolveProfileError(f"{label} soft zone is too large for its interval")
    return checked_minimum, checked_maximum


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _name(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 256:
        raise SolveProfileError(f"{label} must contain 1 to 256 non-NUL characters")
    return value


DEFAULT_SOLVE_PROFILE: Final = SolveProfile(
    name="Canonical Humanoid 標準",
    limits=(
        _limit("Hips", 70.0, -65.0, 65.0),
        _limit("Spine", 45.0, -35.0, 35.0),
        _limit("Chest", 50.0, -40.0, 40.0),
        _limit("UpperChest", 50.0, -40.0, 40.0),
        _limit("Neck", 65.0, -60.0, 60.0),
        _limit("Head", 85.0, -80.0, 80.0),
        _limit("LeftShoulder", 80.0, -60.0, 60.0),
        _limit("RightShoulder", 80.0, -60.0, 60.0),
        _limit("LeftUpperArm", 170.0, -120.0, 120.0, 8.0),
        _limit("RightUpperArm", 170.0, -120.0, 120.0, 8.0),
        _limit(
            "LeftLowerArm",
            165.0,
            -55.0,
            55.0,
            8.0,
            swing_x=(-10.0, 165.0),
            swing_z=(-20.0, 20.0),
        ),
        _limit(
            "RightLowerArm",
            165.0,
            -55.0,
            55.0,
            8.0,
            swing_x=(-10.0, 165.0),
            swing_z=(-20.0, 20.0),
        ),
        _limit("LeftHand", 100.0, -100.0, 100.0),
        _limit("RightHand", 100.0, -100.0, 100.0),
        _limit("LeftUpperLeg", 150.0, -75.0, 75.0, 8.0),
        _limit("RightUpperLeg", 150.0, -75.0, 75.0, 8.0),
        _limit(
            "LeftLowerLeg",
            165.0,
            -40.0,
            40.0,
            8.0,
            swing_x=(-165.0, 10.0),
            swing_z=(-12.0, 12.0),
        ),
        _limit(
            "RightLowerLeg",
            165.0,
            -40.0,
            40.0,
            8.0,
            swing_x=(-165.0, 10.0),
            swing_z=(-12.0, 12.0),
        ),
        _limit("LeftFoot", 90.0, -55.0, 55.0),
        _limit("RightFoot", 90.0, -55.0, 55.0),
        _limit("LeftToes", 65.0, -35.0, 35.0),
        _limit("RightToes", 65.0, -35.0, 35.0),
    ),
)


__all__ = (
    "DEFAULT_SOLVE_PROFILE",
    "JointLimit",
    "JointLimitFrame",
    "JointLimitResult",
    "MAX_SOLVE_BONE_SAMPLES",
    "MAX_SOLVE_FRAMES",
    "SOLVE_PROFILE_SCHEMA",
    "SolveProfile",
    "SolveProfileError",
    "limit_motion_joints",
)
