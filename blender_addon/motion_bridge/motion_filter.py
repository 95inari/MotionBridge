"""Deterministic, Blender-independent one-click motion smoothing.

The filter consumes the immutable :class:`MotionSequence` boundary used by
motion-quality analysis and returns writer-ready absolute targets without
importing ``bpy``.  Translation uses time-domain Gaussian local-linear
regression with non-uniform sample integration weights.  Rotation applies the
same regression in a shortest-arc quaternion Log/Exp tangent space;
quaternion components are never averaged.

``MotionSequence`` deliberately does not carry pose-bone translation.  A
Blender adapter can therefore supply Hips local locations as a separate tuple
aligned with ``sequence.frames``.  This keeps trajectory Root and Hips/COG
translation as distinct channels.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from types import MappingProxyType
from typing import Final, Iterable, Mapping

from .motion_quality import (
    MotionSequence,
    QuaternionWXYZ,
    Vec3,
    validate_motion_sequence,
)
from .range_spec import RangeSource, RangeSpec


MAX_SMOOTH_FRAME_COUNT: Final = 20_000
MAX_SMOOTH_BONE_COUNT: Final = 256
MAX_SMOOTH_CHANNEL_SAMPLES: Final = 2_000_000
MAX_SMOOTH_WINDOW_SAMPLES: Final = 257
MAX_SMOOTH_ABS_COORDINATE: Final = 1_000_000.0

_FRAME_EPSILON: Final = 1.0e-6
_MIN_QUATERNION_NORM: Final = 1.0e-12
_AMBIGUOUS_ROTATION_EPSILON: Final = 1.0e-7
_MAX_NAME_LENGTH: Final = 256


class SmoothingStrength(str, Enum):
    """Beginner-facing strength choices; no numeric tuning is required."""

    LOW = "low"
    STANDARD = "standard"
    STRONG = "strong"


@dataclass(frozen=True, slots=True)
class SmoothingPreset:
    """Fixed time-domain kernel parameters for one strength choice."""

    sigma_frames: float
    radius_frames: float
    blend: float

    def __post_init__(self) -> None:
        sigma = _finite_float(self.sigma_frames, "sigma_frames")
        radius = _finite_float(self.radius_frames, "radius_frames")
        blend = _finite_float(self.blend, "blend")
        if sigma <= 0.0:
            raise ValueError("sigma_frames must be positive")
        if radius <= 0.0:
            raise ValueError("radius_frames must be positive")
        if not 0.0 <= blend <= 1.0:
            raise ValueError("blend must be between zero and one")
        object.__setattr__(self, "sigma_frames", sigma)
        object.__setattr__(self, "radius_frames", radius)
        object.__setattr__(self, "blend", blend)


@dataclass(frozen=True, slots=True)
class SmoothingChannels:
    """Channels included in generated targets.

    ``bone_names=None`` means every sampled bone.  An empty tuple selects no
    bone rotation.  Hips location is emitted only when the caller also supplies
    ``hips_locations`` to :func:`smooth_motion`.
    """

    root_location: bool = True
    root_rotation: bool = True
    bone_names: tuple[str, ...] | None = None
    hips_location: bool = True

    def __post_init__(self) -> None:
        for label in ("root_location", "root_rotation", "hips_location"):
            if type(getattr(self, label)) is not bool:
                raise TypeError(f"{label} must be a boolean")
        if self.bone_names is None:
            return
        if isinstance(self.bone_names, (str, bytes)):
            raise TypeError("bone_names must be an iterable of names")
        try:
            names = tuple(_name(value, "bone name") for value in self.bone_names)
        except TypeError as error:
            raise TypeError("bone_names must be an iterable of names") from error
        if len(set(names)) != len(names):
            raise ValueError("bone_names must not contain duplicates")
        object.__setattr__(self, "bone_names", tuple(sorted(names)))


@dataclass(frozen=True, slots=True)
class SmoothingLimits:
    """Resource and coordinate ceilings; callers may only lower hard limits."""

    max_frame_count: int = MAX_SMOOTH_FRAME_COUNT
    max_bone_count: int = MAX_SMOOTH_BONE_COUNT
    max_channel_samples: int = MAX_SMOOTH_CHANNEL_SAMPLES
    max_window_samples: int = MAX_SMOOTH_WINDOW_SAMPLES
    max_abs_coordinate: float = MAX_SMOOTH_ABS_COORDINATE

    def __post_init__(self) -> None:
        integer_limits = (
            ("max_frame_count", self.max_frame_count, MAX_SMOOTH_FRAME_COUNT),
            ("max_bone_count", self.max_bone_count, MAX_SMOOTH_BONE_COUNT),
            (
                "max_channel_samples",
                self.max_channel_samples,
                MAX_SMOOTH_CHANNEL_SAMPLES,
            ),
            (
                "max_window_samples",
                self.max_window_samples,
                MAX_SMOOTH_WINDOW_SAMPLES,
            ),
        )
        for label, value, hard_limit in integer_limits:
            if type(value) is not int:
                raise TypeError(f"{label} must be an integer")
            if value <= 0:
                raise ValueError(f"{label} must be positive")
            if value > hard_limit:
                raise ValueError(f"{label} must not exceed {hard_limit}")
        coordinate = _finite_float(self.max_abs_coordinate, "max_abs_coordinate")
        if coordinate <= 0.0:
            raise ValueError("max_abs_coordinate must be positive")
        if coordinate > MAX_SMOOTH_ABS_COORDINATE:
            raise ValueError(
                f"max_abs_coordinate must not exceed {MAX_SMOOTH_ABS_COORDINATE:g}"
            )
        object.__setattr__(self, "max_abs_coordinate", coordinate)


@dataclass(frozen=True, slots=True)
class SmoothedBoneRotation:
    """One desired local bone rotation in Blender WXYZ order."""

    bone_name: str
    local_rotation_wxyz: QuaternionWXYZ

    def __post_init__(self) -> None:
        object.__setattr__(self, "bone_name", _name(self.bone_name, "bone_name"))
        object.__setattr__(
            self,
            "local_rotation_wxyz",
            _normalized_quaternion(self.local_rotation_wxyz, "local_rotation_wxyz"),
        )


@dataclass(frozen=True, slots=True)
class SmoothedFrameTarget:
    """Absolute evaluated target for a later Blender Correction Writer adapter."""

    scene_frame: float
    time_seconds: float
    bone_rotations: tuple[SmoothedBoneRotation, ...]
    root_location: Vec3 | None = None
    root_rotation_wxyz: QuaternionWXYZ | None = None
    hips_location: Vec3 | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "scene_frame", _finite_float(self.scene_frame, "scene_frame"))
        object.__setattr__(
            self,
            "time_seconds",
            _finite_float(self.time_seconds, "time_seconds"),
        )
        try:
            bones = tuple(self.bone_rotations)
        except TypeError as error:
            raise TypeError(
                "bone_rotations must be an iterable of SmoothedBoneRotation values"
            ) from error
        if not all(isinstance(value, SmoothedBoneRotation) for value in bones):
            raise TypeError(
                "bone_rotations must contain only SmoothedBoneRotation values"
            )
        bones = tuple(sorted(bones, key=lambda value: value.bone_name))
        if len({value.bone_name for value in bones}) != len(bones):
            raise ValueError("bone_rotations must not contain duplicate bones")
        object.__setattr__(self, "bone_rotations", bones)
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
                _normalized_quaternion(
                    self.root_rotation_wxyz,
                    "root_rotation_wxyz",
                ),
            )
        if self.hips_location is not None:
            object.__setattr__(
                self,
                "hips_location",
                _vec3(self.hips_location, "hips_location"),
            )
        if (
            not bones
            and self.root_location is None
            and self.root_rotation_wxyz is None
            and self.hips_location is None
        ):
            raise ValueError("a smoothed frame target must contain at least one channel")


@dataclass(frozen=True, slots=True)
class MotionSmoothingResult:
    """Immutable filter output with source and range provenance."""

    source_hash: str
    strength: SmoothingStrength
    range_spec: RangeSpec
    targets: tuple[SmoothedFrameTarget, ...]

    def __post_init__(self) -> None:
        if not _is_sha256(self.source_hash):
            raise ValueError("source_hash must be a lowercase SHA-256 value")
        if not isinstance(self.strength, SmoothingStrength):
            raise TypeError("strength must be a SmoothingStrength")
        if not isinstance(self.range_spec, RangeSpec):
            raise TypeError("range_spec must be a RangeSpec")
        try:
            targets = tuple(self.targets)
        except TypeError as error:
            raise TypeError("targets must be an iterable of SmoothedFrameTarget values") from error
        if not targets:
            raise ValueError("targets must not be empty")
        if not all(isinstance(value, SmoothedFrameTarget) for value in targets):
            raise TypeError("targets must contain only SmoothedFrameTarget values")
        for previous, current in zip(targets, targets[1:], strict=False):
            if current.scene_frame <= previous.scene_frame:
                raise ValueError("target scene frames must be strictly increasing")
            if current.time_seconds <= previous.time_seconds:
                raise ValueError("target sample times must be strictly increasing")
        object.__setattr__(self, "targets", targets)

    @property
    def frame_count(self) -> int:
        return len(self.targets)

    @property
    def bone_sample_count(self) -> int:
        return sum(len(target.bone_rotations) for target in self.targets)


def smoothing_preset(strength: SmoothingStrength) -> SmoothingPreset:
    """Return the immutable preset for one beginner-facing strength."""

    if not isinstance(strength, SmoothingStrength):
        raise TypeError("strength must be a SmoothingStrength")
    return SMOOTHING_PRESETS[strength]


def smooth_motion(
    sequence: MotionSequence,
    *,
    strength: SmoothingStrength = SmoothingStrength.STANDARD,
    range_spec: RangeSpec | None = None,
    channels: SmoothingChannels | None = None,
    hips_locations: Iterable[Vec3] | None = None,
    limits: SmoothingLimits | None = None,
) -> MotionSmoothingResult:
    """Smooth selected absolute motion channels without changing ``sequence``.

    The returned frames cover only ``range_spec``.  Its first and last sampled
    values are preserved, so applying the targets cannot introduce a boundary
    jump.  A one- or two-sample range is valid and returned unchanged.
    """

    if not isinstance(sequence, MotionSequence):
        raise TypeError("sequence must be a MotionSequence")
    preset = smoothing_preset(strength)
    selected_channels = SmoothingChannels() if channels is None else channels
    if not isinstance(selected_channels, SmoothingChannels):
        raise TypeError("channels must be a SmoothingChannels")
    checked_limits = SmoothingLimits() if limits is None else limits
    if not isinstance(checked_limits, SmoothingLimits):
        raise TypeError("limits must be a SmoothingLimits")

    validate_motion_sequence(sequence)
    selected_range, selected_indices = _selected_indices(sequence, range_spec)
    selected_bones = _selected_bone_names(sequence, selected_channels)
    _validate_sequence_limits(
        sequence,
        selected_indices,
        selected_bones,
        checked_limits,
    )
    checked_hips = _checked_hips_locations(
        hips_locations,
        sequence,
        selected_indices,
        checked_limits.max_abs_coordinate,
    )
    include_hips = selected_channels.hips_location and checked_hips is not None
    channel_count = (
        len(selected_bones)
        + int(selected_channels.root_location)
        + int(selected_channels.root_rotation)
        + int(include_hips)
    )
    if channel_count == 0:
        raise ValueError("at least one smoothing channel must be selected")
    if len(selected_indices) * channel_count > checked_limits.max_channel_samples:
        raise ValueError("motion filter exceeds max_channel_samples")

    selected_frames = tuple(sequence.frames[index] for index in selected_indices)
    times = tuple(frame.time_seconds for frame in selected_frames)
    kernels = _time_kernels(
        times,
        sequence.fps,
        preset,
        checked_limits.max_window_samples,
    )

    root_locations = (
        _smooth_vectors(
            tuple(frame.root_location for frame in selected_frames),
            kernels,
            times,
            preset.blend,
        )
        if selected_channels.root_location
        else None
    )
    root_rotations = (
        _smooth_quaternions(
            tuple(frame.root_rotation_wxyz for frame in selected_frames),
            kernels,
            times,
            preset.blend,
            "Root rotation",
        )
        if selected_channels.root_rotation
        else None
    )
    if root_locations is not None:
        _require_coordinate_limit(
            root_locations,
            checked_limits.max_abs_coordinate,
            "smoothed Root location",
        )

    bone_inputs: dict[str, tuple[QuaternionWXYZ, ...]] = {}
    for bone_name in selected_bones:
        bone_inputs[bone_name] = tuple(
            _bone_rotation(frame.bones, bone_name) for frame in selected_frames
        )
    bone_outputs = {
        bone_name: _smooth_quaternions(
            values,
            kernels,
            times,
            preset.blend,
            f"{bone_name} rotation",
        )
        for bone_name, values in bone_inputs.items()
    }

    hips_output = (
        _smooth_vectors(
            tuple(checked_hips[index] for index in selected_indices),
            kernels,
            times,
            preset.blend,
        )
        if include_hips and checked_hips is not None
        else None
    )
    if hips_output is not None:
        _require_coordinate_limit(
            hips_output,
            checked_limits.max_abs_coordinate,
            "smoothed Hips location",
        )

    targets = tuple(
        SmoothedFrameTarget(
            scene_frame=frame.frame,
            time_seconds=frame.time_seconds,
            bone_rotations=tuple(
                SmoothedBoneRotation(bone_name, bone_outputs[bone_name][offset])
                for bone_name in selected_bones
            ),
            root_location=None if root_locations is None else root_locations[offset],
            root_rotation_wxyz=(
                None if root_rotations is None else root_rotations[offset]
            ),
            hips_location=None if hips_output is None else hips_output[offset],
        )
        for offset, frame in enumerate(selected_frames)
    )
    return MotionSmoothingResult(
        source_hash=sequence.content_hash(),
        strength=strength,
        range_spec=selected_range,
        targets=targets,
    )


def _validate_sequence_limits(
    sequence: MotionSequence,
    selected_indices: tuple[int, ...],
    selected_bones: tuple[str, ...],
    limits: SmoothingLimits,
) -> None:
    if len(selected_indices) > limits.max_frame_count:
        raise ValueError("motion filter exceeds max_frame_count")
    if len(selected_bones) > limits.max_bone_count:
        raise ValueError("motion filter exceeds max_bone_count")
    for index in selected_indices:
        frame = sequence.frames[index]
        for component in frame.root_location:
            if abs(component) > limits.max_abs_coordinate:
                raise ValueError("Root location exceeds max_abs_coordinate")


def _selected_indices(
    sequence: MotionSequence,
    requested: RangeSpec | None,
) -> tuple[RangeSpec, tuple[int, ...]]:
    whole = RangeSpec(
        sequence.start_frame,
        sequence.end_frame,
        RangeSource.WHOLE_TAKE,
    )
    selected = whole if requested is None else requested
    if not isinstance(selected, RangeSpec):
        raise TypeError("range_spec must be a RangeSpec")
    whole_start, whole_end = whole.expanded_integer_bounds()
    selected_start, selected_end = selected.expanded_integer_bounds()
    if selected_start < whole_start or selected_end > whole_end:
        raise ValueError("range_spec must be inside the motion sequence")
    indices = tuple(
        index
        for index, frame in enumerate(sequence.frames)
        if selected_start - _FRAME_EPSILON
        <= frame.frame
        <= selected_end + _FRAME_EPSILON
    )
    if not indices:
        raise ValueError("range_spec contains no sampled motion frames")
    return selected, indices


def _selected_bone_names(
    sequence: MotionSequence,
    channels: SmoothingChannels,
) -> tuple[str, ...]:
    available = tuple(bone.name for bone in sequence.frames[0].bones)
    if channels.bone_names is None:
        return available
    missing = tuple(name for name in channels.bone_names if name not in available)
    if missing:
        raise ValueError(f"bone_names contain missing bones: {', '.join(missing)}")
    return channels.bone_names


def _checked_hips_locations(
    values: Iterable[Vec3] | None,
    sequence: MotionSequence,
    selected_indices: tuple[int, ...],
    coordinate_limit: float,
) -> tuple[Vec3, ...] | None:
    if values is None:
        return None
    if isinstance(values, (str, bytes)):
        raise TypeError("hips_locations must be an iterable of vectors")
    try:
        supplied = tuple(values)
    except TypeError as error:
        raise TypeError("hips_locations must be an iterable of vectors") from error
    if len(supplied) != len(sequence.frames):
        raise ValueError("hips_locations must align one-to-one with sequence.frames")
    if "Hips" not in {bone.name for bone in sequence.frames[0].bones}:
        raise ValueError("hips_locations require a sampled Hips bone")
    checked = tuple(
        _vec3(value, f"hips_locations[{index}]")
        for index, value in enumerate(supplied)
    )
    if any(
        abs(component) > coordinate_limit
        for index in selected_indices
        for component in checked[index]
    ):
        raise ValueError("Hips location exceeds max_abs_coordinate")
    return checked


def _require_coordinate_limit(
    values: tuple[Vec3, ...],
    limit: float,
    label: str,
) -> None:
    if any(abs(component) > limit for value in values for component in value):
        raise ValueError(f"{label} exceeds max_abs_coordinate")


def _time_kernels(
    times: tuple[float, ...],
    fps: float,
    preset: SmoothingPreset,
    max_window_samples: int,
) -> tuple[tuple[tuple[int, float], ...], ...]:
    count = len(times)
    if count <= 2:
        return tuple(((index, 1.0),) for index in range(count))
    sigma = preset.sigma_frames / fps
    radius = preset.radius_frames / fps
    kernels: list[tuple[tuple[int, float], ...]] = []
    left = 0
    right = 0
    for index, center in enumerate(times):
        if index == 0 or index == count - 1:
            kernels.append(((index, 1.0),))
            continue
        while left < count and times[left] < center - radius - _FRAME_EPSILON:
            left += 1
        right = max(right, index)
        while right + 1 < count and times[right + 1] <= (
            center + radius + _FRAME_EPSILON
        ):
            right += 1
        if right - left + 1 > max_window_samples:
            raise ValueError("motion filter exceeds max_window_samples")

        domain_start = max(times[0], center - radius)
        domain_end = min(times[-1], center + radius)
        weighted: list[tuple[int, float]] = []
        for sample_index in range(left, right + 1):
            cell_start = (
                times[0]
                if sample_index == 0
                else 0.5 * (times[sample_index - 1] + times[sample_index])
            )
            cell_end = (
                times[-1]
                if sample_index == count - 1
                else 0.5 * (times[sample_index] + times[sample_index + 1])
            )
            measure = max(
                0.0,
                min(cell_end, domain_end) - max(cell_start, domain_start),
            )
            if measure == 0.0:
                continue
            time_offset = (times[sample_index] - center) / sigma
            weight = measure * math.exp(-0.5 * time_offset * time_offset)
            if weight > 0.0:
                weighted.append((sample_index, weight))
        total = math.fsum(weight for _, weight in weighted)
        if not math.isfinite(total) or total <= 0.0:
            kernels.append(((index, 1.0),))
        else:
            kernels.append(
                tuple((sample_index, weight / total) for sample_index, weight in weighted)
            )
    return tuple(kernels)


def _smooth_vectors(
    values: tuple[Vec3, ...],
    kernels: tuple[tuple[tuple[int, float], ...], ...],
    times: tuple[float, ...],
    blend: float,
) -> tuple[Vec3, ...]:
    result: list[Vec3] = []
    last_index = len(values) - 1
    for index, (original, kernel) in enumerate(zip(values, kernels, strict=True)):
        if index == 0 or index == last_index:
            result.append(original)
            continue
        fitted = tuple(
            _local_linear_intercept(
                tuple(
                    (sample_index, weight, values[sample_index][axis])
                    for sample_index, weight in kernel
                ),
                times,
                index,
            )
            for axis in range(3)
        )
        result.append(
            (
                original[0] + blend * (fitted[0] - original[0]),
                original[1] + blend * (fitted[1] - original[1]),
                original[2] + blend * (fitted[2] - original[2]),
            )
        )
    return tuple(result)


def _smooth_quaternions(
    values: tuple[QuaternionWXYZ, ...],
    kernels: tuple[tuple[tuple[int, float], ...], ...],
    times: tuple[float, ...],
    blend: float,
    label: str,
) -> tuple[QuaternionWXYZ, ...]:
    aligned = _aligned_quaternions(values, label)
    result: list[QuaternionWXYZ] = []
    last_index = len(aligned) - 1
    for index, (center, kernel) in enumerate(zip(aligned, kernels, strict=True)):
        if index == 0 or index == last_index:
            output = center
        else:
            inverse = _quaternion_conjugate(center)
            logarithms: list[tuple[int, float, Vec3]] = []
            for sample_index, weight in kernel:
                relative = _quaternion_multiply(inverse, aligned[sample_index])
                if relative[0] < 0.0:
                    relative = _negated_quaternion(relative)
                logarithms.append((sample_index, weight, _quaternion_log(relative)))
            tangent = tuple(
                _local_linear_intercept(
                    tuple(
                        (sample_index, weight, logarithm[axis])
                        for sample_index, weight, logarithm in logarithms
                    ),
                    times,
                    index,
                )
                for axis in range(3)
            )
            delta = _quaternion_exp(
                (blend * tangent[0], blend * tangent[1], blend * tangent[2])
            )
            output = _normalized_quaternion(
                _quaternion_multiply(center, delta),
                f"{label} output",
            )
        if result and _quaternion_dot(result[-1], output) < 0.0:
            output = _negated_quaternion(output)
        result.append(output)
    return tuple(result)


def _local_linear_intercept(
    observations: tuple[tuple[int, float, float], ...],
    times: tuple[float, ...],
    center_index: int,
) -> float:
    """Fit ``value = intercept + slope * dt`` and evaluate it at ``dt=0``."""

    center_time = times[center_index]
    mean_time = math.fsum(
        weight * (times[sample_index] - center_time)
        for sample_index, weight, _ in observations
    )
    mean_value = math.fsum(weight * value for _, weight, value in observations)
    variance = math.fsum(
        weight * ((times[sample_index] - center_time) - mean_time) ** 2
        for sample_index, weight, _ in observations
    )
    variance_scale = math.fsum(
        weight * (times[sample_index] - center_time) ** 2
        for sample_index, weight, _ in observations
    )
    if variance <= max(1.0e-30, variance_scale * 1.0e-12):
        return mean_value
    covariance = math.fsum(
        weight
        * ((times[sample_index] - center_time) - mean_time)
        * (value - mean_value)
        for sample_index, weight, value in observations
    )
    return mean_value - (covariance / variance) * mean_time


def _aligned_quaternions(
    values: tuple[QuaternionWXYZ, ...],
    label: str,
) -> tuple[QuaternionWXYZ, ...]:
    aligned: list[QuaternionWXYZ] = []
    for index, value in enumerate(values):
        current = _normalized_quaternion(value, f"{label}[{index}]")
        if aligned:
            dot = _quaternion_dot(aligned[-1], current)
            shortest_dot = abs(max(-1.0, min(1.0, dot)))
            angle = 2.0 * math.acos(shortest_dot)
            if angle >= math.pi - _AMBIGUOUS_ROTATION_EPSILON:
                raise ValueError(
                    f"{label} has an ambiguous 180-degree step at sample {index}"
                )
            if dot < 0.0:
                current = _negated_quaternion(current)
        aligned.append(current)
    return tuple(aligned)


def _bone_rotation(
    bones: tuple[object, ...],
    bone_name: str,
) -> QuaternionWXYZ:
    for bone in bones:
        if getattr(bone, "name", None) == bone_name:
            return getattr(bone, "local_rotation_wxyz")
    raise RuntimeError(f"validated bone disappeared: {bone_name}")


def _quaternion_log(value: QuaternionWXYZ) -> Vec3:
    normalized = _normalized_quaternion(value, "quaternion logarithm input")
    vector_norm = math.hypot(normalized[1], normalized[2], normalized[3])
    if vector_norm <= _MIN_QUATERNION_NORM:
        return (0.0, 0.0, 0.0)
    half_angle = math.atan2(vector_norm, normalized[0])
    scale = half_angle / vector_norm
    return (
        normalized[1] * scale,
        normalized[2] * scale,
        normalized[3] * scale,
    )


def _quaternion_exp(value: Vec3) -> QuaternionWXYZ:
    half_angle = math.hypot(*value)
    if half_angle <= _MIN_QUATERNION_NORM:
        return (1.0, value[0], value[1], value[2])
    scale = math.sin(half_angle) / half_angle
    return (
        math.cos(half_angle),
        value[0] * scale,
        value[1] * scale,
        value[2] * scale,
    )


def _quaternion_multiply(
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


def _quaternion_conjugate(value: QuaternionWXYZ) -> QuaternionWXYZ:
    return (value[0], -value[1], -value[2], -value[3])


def _negated_quaternion(value: QuaternionWXYZ) -> QuaternionWXYZ:
    return (-value[0], -value[1], -value[2], -value[3])


def _quaternion_dot(left: QuaternionWXYZ, right: QuaternionWXYZ) -> float:
    return math.fsum(a * b for a, b in zip(left, right, strict=True))


def _normalized_quaternion(value: object, label: str) -> QuaternionWXYZ:
    checked = _vector(value, 4, label)
    result = (checked[0], checked[1], checked[2], checked[3])
    norm = math.hypot(*result)
    if not math.isfinite(norm) or norm <= _MIN_QUATERNION_NORM:
        raise ValueError(f"{label} must have a finite non-zero norm")
    return (
        result[0] / norm,
        result[1] / norm,
        result[2] / norm,
        result[3] / norm,
    )


def _vec3(value: object, label: str) -> Vec3:
    checked = _vector(value, 3, label)
    return (checked[0], checked[1], checked[2])


def _vector(value: object, size: int, label: str) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{label} must be a numeric vector")
    try:
        supplied = tuple(value)  # type: ignore[arg-type]
    except TypeError as error:
        raise TypeError(f"{label} must be a numeric vector") from error
    if len(supplied) != size:
        raise ValueError(f"{label} must contain exactly {size} values")
    return tuple(
        _finite_float(component, f"{label}[{index}]")
        for index, component in enumerate(supplied)
    )


def _finite_float(value: object, label: str) -> float:
    if type(value) is bool or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{label} must be finite") from error
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _name(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if not value or len(value) > _MAX_NAME_LENGTH or "\x00" in value:
        raise ValueError(
            f"{label} must contain 1 to {_MAX_NAME_LENGTH} non-NUL characters"
        )
    return value


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


SMOOTHING_PRESETS: Final[Mapping[SmoothingStrength, SmoothingPreset]] = (
    MappingProxyType(
        {
            SmoothingStrength.LOW: SmoothingPreset(0.70, 2.0, 0.35),
            SmoothingStrength.STANDARD: SmoothingPreset(1.25, 3.0, 0.65),
            SmoothingStrength.STRONG: SmoothingPreset(2.00, 5.0, 0.85),
        }
    )
)


__all__ = [
    "MAX_SMOOTH_ABS_COORDINATE",
    "MAX_SMOOTH_BONE_COUNT",
    "MAX_SMOOTH_CHANNEL_SAMPLES",
    "MAX_SMOOTH_FRAME_COUNT",
    "MAX_SMOOTH_WINDOW_SAMPLES",
    "SMOOTHING_PRESETS",
    "MotionSmoothingResult",
    "SmoothedBoneRotation",
    "SmoothedFrameTarget",
    "SmoothingChannels",
    "SmoothingLimits",
    "SmoothingPreset",
    "SmoothingStrength",
    "smooth_motion",
    "smoothing_preset",
]
