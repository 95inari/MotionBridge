"""Transactional tone-curve envelopes for sparse Correction layers.

The UI deliberately stays outside this module.  It supplies a callable that
maps normalized time to a correction weight (for example a Blender
``CurveMapping`` evaluator), while this module owns the important invariants:

* quaternion deltas are exponentiated as rotations, never per WXYZ channel;
* location is scaled as a vector and is limited to the Hips pose bone;
* all preview writes are made against one immutable key snapshot;
* Cancel and failed writes restore key values, handles, interpolation and
  selection exactly, including removal of curves created by the preview.

The implementation reuses Correction Edit's NLA inverse-remapping and key
transaction boundary.  Therefore callers must already be in Motion Bridge
Correction Edit mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import math
import sys
from typing import Callable, Iterable, Mapping, Sequence

import bpy
from mathutils import Quaternion, Vector

from .action_api import (
    ActionChannelAccess,
    insert_location_key,
    insert_quaternion_key,
    iter_action_fcurves,
    remove_location_keys_in_range,
    remove_quaternion_keys_in_range,
)
from . import correction_edit as _correction_edit
from .correction_layers import action_checksum


_FRAME_EPSILON = 1.0e-6
_QUATERNION_EPSILON = 1.0e-12
_DEFAULT_LOCATION_BONES = ("Hips",)
_KEY_INTERPOLATIONS = frozenset({"CONSTANT", "LINEAR", "BEZIER"})
_MAX_PREVIEW_SAMPLES = 100_000
_DEFAULT_ADAPTIVE_TOLERANCE = 0.002
_DEFAULT_ADAPTIVE_MAX_SAMPLES = 512
_METADATA_SCHEMA_VERSION = 1
_METADATA_SCHEMA_ID = "motion-bridge.correction-envelope/1"
_METADATA_SCHEMA_KEY = "motion_bridge_envelope_schema"
_METADATA_JSON_KEY = "motion_bridge_envelope_metadata"

WeightEvaluator = Callable[[float], float]


class EnvelopePreset(str, Enum):
    """Built-in normalized envelope shapes."""

    SMOOTH_PULSE = "SMOOTH_PULSE"
    PLATEAU = "PLATEAU"
    EASE_IN = "EASE_IN"
    EASE_OUT = "EASE_OUT"
    EASE_IN_OUT = "EASE_IN_OUT"
    S_CURVE = "S_CURVE"


class EnvelopeRangeSource(str, Enum):
    """How an :class:`EnvelopeRange` was resolved."""

    SELECTED_KEYS = "SELECTED_KEYS"
    PREVIEW_RANGE = "PREVIEW_RANGE"


@dataclass(frozen=True, slots=True)
class EnvelopeRange:
    """One forward Scene/Correction-Action time interval."""

    scene_start: float
    scene_end: float
    action_start: float
    action_end: float
    source: EnvelopeRangeSource


@dataclass(frozen=True, slots=True)
class _PoseOffset:
    bone_name: str
    rotation: tuple[float, float, float, float]
    location: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class _ExistingKey:
    bone_name: str
    property_name: str
    frame: float
    values: tuple[float, ...]


def _finite_number(value: object, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{label} must be finite") from error
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _unit_interval(value: object, label: str) -> float:
    result = _finite_number(value, label)
    return min(1.0, max(0.0, result))


def _smoothstep(value: float) -> float:
    return value * value * (3.0 - (2.0 * value))


def _smootherstep(value: float) -> float:
    return value * value * value * (
        value * ((value * 6.0) - 15.0) + 10.0
    )


def evaluate_envelope_preset(
    preset: EnvelopePreset | str,
    normalized_time: float,
    *,
    attack: float = 0.2,
    release: float = 0.2,
) -> float:
    """Evaluate a built-in envelope at normalized time.

    The input is clamped to ``0..1``.  ``attack`` and ``release`` are used by
    ``PLATEAU`` and are normalized fractions of the complete range.  They may
    meet but cannot overlap.  Custom CurveMapping callbacks are intentionally
    *not* clamped by the preview writer, allowing negative and overshoot
    correction weights in Advanced mode.
    """

    try:
        shape = preset if isinstance(preset, EnvelopePreset) else EnvelopePreset(
            str(preset).upper()
        )
    except ValueError as error:
        supported = ", ".join(item.value for item in EnvelopePreset)
        raise ValueError(f"Envelope preset must be one of: {supported}") from error
    time = _unit_interval(normalized_time, "Envelope normalized time")

    if shape is EnvelopePreset.SMOOTH_PULSE:
        if time <= 0.5:
            return _smootherstep(time * 2.0)
        return _smootherstep((1.0 - time) * 2.0)
    if shape is EnvelopePreset.PLATEAU:
        attack_value = _unit_interval(attack, "Envelope attack")
        release_value = _unit_interval(release, "Envelope release")
        if attack_value + release_value > 1.0 + _FRAME_EPSILON:
            raise ValueError("Envelope attack and release cannot overlap")
        if attack_value > _FRAME_EPSILON and time < attack_value:
            return _smootherstep(time / attack_value)
        release_start = 1.0 - release_value
        if release_value > _FRAME_EPSILON and time > release_start:
            return _smootherstep((1.0 - time) / release_value)
        return 1.0
    if shape is EnvelopePreset.EASE_IN:
        return time * time
    if shape is EnvelopePreset.EASE_OUT:
        inverse = 1.0 - time
        return 1.0 - (inverse * inverse)
    if shape is EnvelopePreset.EASE_IN_OUT:
        if time < 0.5:
            return 2.0 * time * time
        return 1.0 - (((-2.0 * time) + 2.0) ** 2.0 / 2.0)
    if shape is EnvelopePreset.S_CURVE:
        return _smootherstep(time)
    raise AssertionError("Unhandled EnvelopePreset")


def preset_weight_evaluator(
    preset: EnvelopePreset | str,
    *,
    attack: float = 0.2,
    release: float = 0.2,
) -> WeightEvaluator:
    """Return a callback suitable for ``begin_*_envelope_preview``."""

    # Validate the enum and Plateau parameters before the callback is retained.
    evaluate_envelope_preset(preset, 0.0, attack=attack, release=release)

    def evaluate(normalized_time: float) -> float:
        return evaluate_envelope_preset(
            preset,
            normalized_time,
            attack=attack,
            release=release,
        )

    return evaluate


def _normalized_quaternion(
    quaternion: Quaternion | Sequence[float],
) -> tuple[float, float, float, float]:
    try:
        values = tuple(float(value) for value in quaternion)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("Quaternion delta must contain four finite values") from error
    if len(values) != 4 or not all(math.isfinite(value) for value in values):
        raise ValueError("Quaternion delta must contain four finite values")
    norm = math.hypot(*values)
    if not math.isfinite(norm) or norm <= _QUATERNION_EPSILON:
        raise ValueError("Quaternion delta must have a non-zero finite norm")
    return tuple(value / norm for value in values)  # type: ignore[return-value]


def _canonical_rotation_hemisphere(
    quaternion: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Choose one deterministic representative of q/-q's shortest rotation."""

    should_flip = quaternion[0] < 0.0
    if abs(quaternion[0]) <= _QUATERNION_EPSILON:
        # At exactly 180 degrees W cannot choose the hemisphere.  Canonicalize
        # the first non-zero vector component so q and -q still produce the
        # same weighted rotation.
        for component in quaternion[1:]:
            if abs(component) > _QUATERNION_EPSILON:
                should_flip = component < 0.0
                break
    if should_flip:
        return tuple(-value for value in quaternion)  # type: ignore[return-value]
    return quaternion


def weighted_quaternion_delta(
    quaternion_delta: Quaternion | Sequence[float],
    weight: float,
) -> Quaternion:
    """Return ``Exp(weight * Log(delta))`` using the principal rotation log.

    Unlike component-wise WXYZ scaling this always returns a unit quaternion.
    The deterministic shortest-arc hemisphere makes the result invariant to
    whether Blender stored ``q`` or ``-q``.  Negative and >1 weights are
    supported for Advanced undershoot/overshoot.
    """

    scale = _finite_number(weight, "Envelope weight")
    w, x, y, z = _canonical_rotation_hemisphere(
        _normalized_quaternion(quaternion_delta)
    )
    vector_length = math.sqrt((x * x) + (y * y) + (z * z))
    if vector_length <= _QUATERNION_EPSILON:
        return Quaternion((1.0, 0.0, 0.0, 0.0))

    half_angle = math.atan2(vector_length, min(1.0, max(-1.0, w)))
    weighted_half_angle = scale * half_angle
    vector_scale = math.sin(weighted_half_angle) / vector_length
    result = Quaternion(
        (
            math.cos(weighted_half_angle),
            x * vector_scale,
            y * vector_scale,
            z * vector_scale,
        )
    )
    result.normalize()
    return result


def weighted_location_delta(
    location_delta: Vector | Sequence[float],
    weight: float,
) -> Vector:
    """Return the finite XYZ location delta multiplied by ``weight``."""

    scale = _finite_number(weight, "Envelope weight")
    try:
        values = tuple(float(value) for value in location_delta)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("Location delta must contain three finite values") from error
    if len(values) != 3 or not all(math.isfinite(value) for value in values):
        raise ValueError("Location delta must contain three finite values")
    return Vector(tuple(value * scale for value in values))


def _property_path(bone_name: str, property_name: str) -> str:
    escaped = bpy.utils.escape_identifier(str(bone_name))
    return f'pose.bones["{escaped}"].{property_name}'


def _location_targets(
    bone_names: Sequence[str],
    *,
    include_location: bool,
    location_bone_names: Iterable[str] | None,
) -> tuple[str, ...]:
    if not include_location:
        return ()
    allowed = {
        str(name)
        for name in (
            _DEFAULT_LOCATION_BONES
            if location_bone_names is None
            else location_bone_names
        )
    }
    unsupported = allowed.difference(_DEFAULT_LOCATION_BONES)
    if unsupported:
        names = ", ".join(sorted(unsupported))
        raise ValueError(
            "位置補正はHipsだけに使用できます。"
            f"未対応のボーン: {names}"
        )
    targets = tuple(name for name in bone_names if name in allowed)
    if not targets:
        raise ValueError(
            "位置補正を使う場合はHipsを選択してください。"
            "Hips以外のボーンには回転補正だけを使用できます"
        )
    return targets


def _point_selected(point: bpy.types.Keyframe) -> bool:
    return bool(
        point.select_control_point
        or point.select_left_handle
        or point.select_right_handle
    )


def selected_correction_key_frames(
    access: ActionChannelAccess,
    bone_names: Iterable[str],
    *,
    include_location: bool = False,
    location_bone_names: Iterable[str] | None = _DEFAULT_LOCATION_BONES,
) -> tuple[float, ...]:
    """Return selected relevant Correction key times in Action space.

    A selected point on any WXYZ/XYZ channel selects that complete property
    key time; unrelated bones and properties are ignored.
    """

    names = tuple(dict.fromkeys(str(name) for name in bone_names))
    paths = {
        _property_path(name, "rotation_quaternion")
        for name in names
    }
    paths.update(
        _property_path(name, "location")
        for name in _location_targets(
            names,
            include_location=include_location,
            location_bone_names=location_bone_names,
        )
    )
    frames = sorted(
        {
            float(point.co[0])
            for curve in iter_action_fcurves(access.action, access.slot)
            if curve.data_path in paths
            for point in curve.keyframe_points
            if _point_selected(point) and math.isfinite(float(point.co[0]))
        }
    )
    result: list[float] = []
    for frame in frames:
        if not result or abs(frame - result[-1]) > _FRAME_EPSILON:
            result.append(frame)
    return tuple(result)


def selected_correction_key_range(
    access: ActionChannelAccess,
    bone_names: Iterable[str],
    *,
    include_location: bool = False,
    location_bone_names: Iterable[str] | None = _DEFAULT_LOCATION_BONES,
) -> tuple[float, float] | None:
    """Return inclusive selected-key Action range, or ``None``."""

    frames = selected_correction_key_frames(
        access,
        bone_names,
        include_location=include_location,
        location_bone_names=location_bone_names,
    )
    # One selected key cannot define both ends of a tone-curve interval.
    return (frames[0], frames[-1]) if len(frames) >= 2 else None


def _action_to_scene_frame(
    armature: bpy.types.Object,
    action_frame: float,
) -> float:
    animation_data = armature.animation_data
    if animation_data is None or not _correction_edit.is_correction_editing(armature):
        raise ValueError("Enter Correction Edit before resolving an envelope range")
    result = float(
        animation_data.nla_tweak_strip_time_to_scene(float(action_frame), invert=False)
    )
    if not math.isfinite(result):
        raise ValueError("NLA produced a non-finite Scene frame")
    return result


def resolve_envelope_range(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    *,
    preview_scene_start: float,
    preview_scene_end: float,
    prefer_preview_range: bool = False,
    include_location: bool = False,
    location_bone_names: Iterable[str] | None = _DEFAULT_LOCATION_BONES,
) -> EnvelopeRange:
    """Resolve selected keys or the explicitly requested numeric Preview range."""

    session, bones, access = _correction_edit._require_pose_key_context(
        context,
        armature,
    )
    bone_names = tuple(bone.name for bone in bones)
    selected = selected_correction_key_range(
        access,
        bone_names,
        include_location=include_location,
        location_bone_names=location_bone_names,
    )
    if selected is not None and not prefer_preview_range:
        action_start = _correction_edit._validate_action_frame(
            session.action,
            selected[0],
            "Selected Correction range start",
        )
        action_end = _correction_edit._validate_action_frame(
            session.action,
            selected[1],
            "Selected Correction range end",
        )
        scene_start = _action_to_scene_frame(armature, action_start)
        scene_end = _action_to_scene_frame(armature, action_end)
        if scene_end < scene_start - _FRAME_EPSILON:
            raise ValueError("NLA maps the selected Correction range in reverse")
        return EnvelopeRange(
            scene_start,
            scene_end,
            action_start,
            action_end,
            EnvelopeRangeSource.SELECTED_KEYS,
        )

    scene_start = _finite_number(preview_scene_start, "Preview range start")
    scene_end = _finite_number(preview_scene_end, "Preview range end")
    if scene_end <= scene_start + _FRAME_EPSILON:
        raise ValueError("Preview Scene range must contain at least two frames")
    action_start = _correction_edit._validate_action_frame(
        session.action,
        _correction_edit.scene_to_correction_frame(armature, scene_start),
        "Preview Correction range start",
    )
    action_end = _correction_edit._validate_action_frame(
        session.action,
        _correction_edit.scene_to_correction_frame(armature, scene_end),
        "Preview Correction range end",
    )
    if action_end < action_start - _FRAME_EPSILON:
        raise ValueError("NLA maps the Preview range in reverse")
    return EnvelopeRange(
        scene_start,
        scene_end,
        action_start,
        action_end,
        EnvelopeRangeSource.PREVIEW_RANGE,
    )


def _validate_envelope_range(
    session_action: bpy.types.Action,
    envelope_range: EnvelopeRange,
) -> EnvelopeRange:
    if not isinstance(envelope_range, EnvelopeRange):
        raise TypeError("envelope_range must be an EnvelopeRange")
    scene_start = _finite_number(envelope_range.scene_start, "Envelope Scene start")
    scene_end = _finite_number(envelope_range.scene_end, "Envelope Scene end")
    if scene_end <= scene_start + _FRAME_EPSILON:
        raise ValueError("Envelope Scene range must have positive duration")
    action_start = _correction_edit._validate_action_frame(
        session_action,
        envelope_range.action_start,
        "Envelope Action start",
    )
    action_end = _correction_edit._validate_action_frame(
        session_action,
        envelope_range.action_end,
        "Envelope Action end",
    )
    if action_end <= action_start + _FRAME_EPSILON:
        raise ValueError("Envelope Action range must have positive duration")
    return EnvelopeRange(
        scene_start,
        scene_end,
        action_start,
        action_end,
        envelope_range.source,
    )


def _uniform_scene_frames(
    envelope_range: EnvelopeRange,
    sample_step: float,
) -> tuple[float, ...]:
    step = _finite_number(sample_step, "Envelope sample step")
    if step <= 0.0:
        raise ValueError("Envelope sample step must be positive")
    span = envelope_range.scene_end - envelope_range.scene_start
    if span <= _FRAME_EPSILON:
        return (envelope_range.scene_start,)
    estimated = int(math.floor(span / step)) + 2
    if estimated > _MAX_PREVIEW_SAMPLES:
        raise ValueError("Envelope range would create too many preview samples")
    frames = [envelope_range.scene_start]
    next_frame = envelope_range.scene_start + step
    while next_frame < envelope_range.scene_end - _FRAME_EPSILON:
        frames.append(next_frame)
        next_frame += step
    if abs(frames[-1] - envelope_range.scene_end) > _FRAME_EPSILON:
        frames.append(envelope_range.scene_end)
    return tuple(frames)


def adaptive_envelope_samples(
    evaluator: WeightEvaluator,
    *,
    tolerance: float = _DEFAULT_ADAPTIVE_TOLERANCE,
    rotation_deltas: Iterable[Quaternion | Sequence[float]] = (),
    rotation_tolerance_degrees: float = 0.1,
    seed_times: Iterable[float] | None = None,
    max_samples: int = _DEFAULT_ADAPTIVE_MAX_SAMPLES,
    max_depth: int = 16,
) -> tuple[tuple[float, float], ...]:
    """Adaptively approximate a weight curve with LINEAR key samples.

    Each candidate interval probes its 1/4, 1/2 and 3/4 points.  It is split
    until all probe values differ from linear interpolation by at most
    ``tolerance``.  When quaternion deltas are supplied, it also bounds the
    rotational difference between the exact Log/Exp result and Blender's
    normalized component-wise LINEAR interpolation.  Only split boundaries
    become keys, so a long flat Plateau remains sparse while curved weights
    and large-angle rotation deltas receive enough samples.
    """

    error_limit = _finite_number(tolerance, "Envelope sample tolerance")
    if error_limit <= 0.0:
        raise ValueError("Envelope sample tolerance must be positive")
    try:
        sample_limit = int(max_samples)
        depth_limit = int(max_depth)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("Envelope sample limits must be integers") from error
    if sample_limit < 2 or sample_limit > _MAX_PREVIEW_SAMPLES:
        raise ValueError("Envelope max samples must be between 2 and 100000")
    if depth_limit < 1 or depth_limit > 30:
        raise ValueError("Envelope max sample depth must be between 1 and 30")
    rotation_limit = math.radians(
        _finite_number(
            rotation_tolerance_degrees,
            "Envelope rotation sample tolerance",
        )
    )
    if rotation_limit <= 0.0:
        raise ValueError("Envelope rotation sample tolerance must be positive")
    rotations = tuple(_normalized_quaternion(delta) for delta in rotation_deltas)
    evaluator_seed_times = getattr(evaluator, "sample_times", ()) or ()
    supplied_seed_times = (
        tuple(evaluator_seed_times)
        if seed_times is None
        else tuple(seed_times) + tuple(evaluator_seed_times)
    )
    normalized_seed_times = {0.0, 1.0}
    for seed_time in supplied_seed_times:
        value = _finite_number(seed_time, "Envelope adaptive seed time")
        if value < 0.0 or value > 1.0:
            raise ValueError("Envelope adaptive seed times must be inside 0..1")
        normalized_seed_times.add(value)
    ordered_seed_times = tuple(sorted(normalized_seed_times))
    if len(ordered_seed_times) > sample_limit:
        raise ValueError("Envelope adaptive seed times exceed max_samples")

    cache: dict[float, float] = {}

    def evaluate(time: float) -> float:
        value = cache.get(time)
        if value is None:
            value = _evaluate_weights(evaluator, (time,))[0]
            cache[time] = value
        return value

    result: dict[float, float] = {
        time: evaluate(time) for time in ordered_seed_times
    }
    limit_reached = False
    depth_reached = False

    def normalized_lerp(
        start: Quaternion,
        end: Quaternion,
        factor: float,
    ) -> Quaternion:
        end_values = tuple(float(value) for value in end)
        if start.dot(end) < 0.0:
            end_values = tuple(-value for value in end_values)
        values = tuple(
            ((1.0 - factor) * float(left)) + (factor * right)
            for left, right in zip(start, end_values, strict=True)
        )
        norm = math.hypot(*values)
        if norm <= _QUATERNION_EPSILON:
            return Quaternion((1.0, 0.0, 0.0, 0.0))
        return Quaternion(tuple(value / norm for value in values))

    def rotation_error_exceeded(
        start_time: float,
        end_time: float,
        probe_time: float,
    ) -> bool:
        if not rotations:
            return False
        factor = (probe_time - start_time) / (end_time - start_time)
        start_weight = evaluate(start_time)
        end_weight = evaluate(end_time)
        probe_weight = evaluate(probe_time)
        for delta in rotations:
            exact = weighted_quaternion_delta(delta, probe_weight)
            approximated = normalized_lerp(
                weighted_quaternion_delta(delta, start_weight),
                weighted_quaternion_delta(delta, end_weight),
                factor,
            )
            if exact.rotation_difference(approximated).angle > rotation_limit:
                return True
        return False

    def refine(start: float, end: float, depth: int) -> None:
        nonlocal depth_reached, limit_reached
        start_value = evaluate(start)
        end_value = evaluate(end)
        span = end - start
        probes = (
            start + (span * 0.25),
            start + (span * 0.5),
            start + (span * 0.75),
        )
        should_split = any(
            (
                abs(
                    evaluate(time)
                    - (
                        start_value
                        + ((end_value - start_value) * ((time - start) / span))
                    )
                )
                > error_limit
            )
            or rotation_error_exceeded(start, end, time)
            for time in probes
        )
        if not should_split:
            return
        if depth >= depth_limit:
            depth_reached = True
            return
        midpoint = probes[1]
        if midpoint not in result:
            if len(result) >= sample_limit:
                limit_reached = True
                return
            result[midpoint] = evaluate(midpoint)
        refine(start, midpoint, depth + 1)
        refine(midpoint, end, depth + 1)

    for start, end in zip(ordered_seed_times, ordered_seed_times[1:], strict=False):
        refine(start, end, 0)
    if limit_reached:
        raise ValueError(
            "Envelope curve exceeds the adaptive sample limit; increase max_samples"
        )
    if depth_reached:
        raise ValueError(
            "Envelope curve exceeds the adaptive depth limit; increase max_depth"
        )
    return tuple(sorted(result.items()))


def _normalized_time(envelope_range: EnvelopeRange, scene_frame: float) -> float:
    span = envelope_range.scene_end - envelope_range.scene_start
    if span <= _FRAME_EPSILON:
        return 0.5
    return min(
        1.0,
        max(0.0, (scene_frame - envelope_range.scene_start) / span),
    )


def _evaluate_weights(
    evaluator: WeightEvaluator,
    normalized_times: Sequence[float],
) -> tuple[float, ...]:
    if not callable(evaluator):
        raise TypeError("Envelope weight evaluator must be callable")
    result: list[float] = []
    for normalized_time in normalized_times:
        try:
            value = evaluator(normalized_time)
        except Exception as error:
            raise ValueError(
                f"Envelope curve evaluation failed at {normalized_time:g}"
            ) from error
        result.append(_finite_number(value, "Envelope curve weight"))
    return tuple(result)


def _key_interpolation(value: str) -> str:
    result = str(value).upper()
    if result not in _KEY_INTERPOLATIONS:
        supported = ", ".join(sorted(_KEY_INTERPOLATIONS))
        raise ValueError(f"Envelope key interpolation must be one of: {supported}")
    return result


def _curve_set(
    access: ActionChannelAccess,
    data_path: str,
    size: int,
) -> tuple[bpy.types.FCurve, ...] | None:
    by_index: dict[int, bpy.types.FCurve] = {}
    for curve in iter_action_fcurves(access.action, access.slot):
        index = int(curve.array_index)
        if curve.data_path == data_path and 0 <= index < size:
            if index in by_index:
                raise ValueError(f"Correction has duplicate channels: {data_path}")
            by_index[index] = curve
    if not by_index:
        return None
    if set(by_index) != set(range(size)):
        raise ValueError(f"Correction property must key all channels: {data_path}")
    return tuple(by_index[index] for index in range(size))


def _exact_key(
    curve: bpy.types.FCurve,
    frame: float,
) -> bpy.types.Keyframe:
    matches = tuple(
        point
        for point in curve.keyframe_points
        if abs(float(point.co[0]) - frame) <= _FRAME_EPSILON
    )
    if len(matches) != 1:
        raise ValueError(
            f"Correction property is not completely keyed at Action frame {frame:g}: "
            f"{curve.data_path}"
        )
    return matches[0]


def _selected_existing_keys(
    access: ActionChannelAccess,
    bone_names: Sequence[str],
    location_names: Sequence[str],
) -> tuple[_ExistingKey, ...]:
    result: list[_ExistingKey] = []
    location_set = set(location_names)
    for bone_name in bone_names:
        properties = [("rotation_quaternion", 4)]
        if bone_name in location_set:
            properties.append(("location", 3))
        for property_name, size in properties:
            curves = _curve_set(
                access,
                _property_path(bone_name, property_name),
                size,
            )
            if curves is None:
                continue
            selected_frames = sorted(
                {
                    float(point.co[0])
                    for curve in curves
                    for point in curve.keyframe_points
                    if _point_selected(point)
                }
            )
            for frame in selected_frames:
                values = tuple(float(_exact_key(curve, frame).co[1]) for curve in curves)
                if not all(math.isfinite(value) for value in values):
                    raise ValueError("Selected Correction key contains non-finite values")
                if property_name == "rotation_quaternion":
                    values = _normalized_quaternion(values)
                result.append(_ExistingKey(bone_name, property_name, frame, values))
    return tuple(result)


def _set_existing_key_values_preserving_handles(
    access: ActionChannelAccess,
    updates: Sequence[tuple[_ExistingKey, Sequence[float]]],
) -> None:
    point_updates: list[
        tuple[
            bpy.types.FCurve,
            bpy.types.Keyframe,
            float,
            float,
            tuple[float, float],
            tuple[float, float],
            str,
            str,
        ]
    ] = []
    for key, values in updates:
        curves = _curve_set(
            access,
            _property_path(key.bone_name, key.property_name),
            len(key.values),
        )
        if curves is None:
            raise RuntimeError("Selected Correction property disappeared during preview")
        for curve, original, value in zip(curves, key.values, values, strict=True):
            point = _exact_key(curve, key.frame)
            point_updates.append(
                (
                    curve,
                    point,
                    float(original),
                    float(value),
                    tuple(float(item) for item in point.handle_left),
                    tuple(float(item) for item in point.handle_right),
                    str(point.handle_left_type),
                    str(point.handle_right_type),
                )
            )

    # Change every point first, restore translated handles second, and update
    # each curve once.  This prevents AUTO handle recalculation at an earlier
    # key from becoming the baseline for a later key on the same FCurve.
    for _curve, point, _original, value, _left, _right, _left_type, _right_type in point_updates:
        point.co = (float(point.co[0]), value)
    for _curve, point, original, value, left, right, left_type, right_type in point_updates:
        delta = value - original
        point.handle_left_type = left_type
        point.handle_right_type = right_type
        point.handle_left = (left[0], left[1] + delta)
        point.handle_right = (right[0], right[1] + delta)
    updated: set[int] = set()
    for curve, _point, _original, _value, _left, _right, _left_type, _right_type in point_updates:
        pointer = int(curve.as_pointer())
        if pointer not in updated:
            curve.update()
            updated.add(pointer)


class CorrectionEnvelopePreview:
    """One live preview whose baseline can be applied or restored exactly."""

    def __init__(
        self,
        *,
        context: bpy.types.Context,
        armature: bpy.types.Object,
        action: bpy.types.Action,
        access: ActionChannelAccess,
        bone_names: tuple[str, ...],
        location_bone_names: tuple[str, ...],
        envelope_range: EnvelopeRange,
        evaluator: WeightEvaluator,
        transaction: object,
        restore_keys: Callable[[], None],
        render: Callable[[WeightEvaluator], tuple[tuple[float, float], ...]],
        kind: str,
        source_offsets: tuple[_PoseOffset, ...] = (),
    ) -> None:
        self._context = context
        self._armature = armature
        self._access = access
        self._location_bone_names = location_bone_names
        self._evaluator = evaluator
        self._transaction = transaction
        self._restore_keys = restore_keys
        self._render = render
        self._last_weight_samples: tuple[tuple[float, float], ...] = ()
        self._metadata: dict[str, object] | None = None
        self._active = True
        self.action = action
        self.bone_names = bone_names
        self.envelope_range = envelope_range
        self.kind = kind
        self._source_offsets = source_offsets

    @property
    def active(self) -> bool:
        return self._active

    @property
    def armature(self) -> bpy.types.Object:
        return self._armature

    @property
    def location_bone_names(self) -> tuple[str, ...]:
        return self._location_bone_names

    @property
    def weight_samples(self) -> tuple[tuple[float, float], ...]:
        return self._last_weight_samples

    @property
    def metadata(self) -> dict[str, object] | None:
        return self._metadata

    def _require_active(self) -> None:
        if not self._active or _ACTIVE_PREVIEWS.get(_object_key(self._armature)) is not self:
            raise ValueError("Correction Envelope preview is no longer active")

    def update(
        self,
        weight_evaluator: WeightEvaluator | None = None,
    ) -> tuple[str, ...]:
        """Rebuild preview keys from the original immutable snapshot."""

        self._require_active()
        evaluator = self._evaluator if weight_evaluator is None else weight_evaluator
        try:
            samples = self._render(evaluator)
        except Exception:
            # A failed callback or Blender write never leaves a partial preview.
            self._restore_keys()
            raise
        self._evaluator = evaluator
        self._last_weight_samples = samples
        return self.bone_names

    def apply(
        self,
        metadata: Mapping[str, object] | None = None,
    ) -> tuple[str, ...]:
        """Commit the current preview keys and release its rollback snapshot."""

        self._require_active()
        errors = _correction_edit._active_action_content_errors(
            self.action,
            self.armature,
        )
        if errors:
            raise ValueError("Invalid Correction Envelope keys: " + "; ".join(errors))
        try:
            ui_metadata = json.loads(
                json.dumps(
                    dict(metadata or {}),
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
            )
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("Correction Envelope metadata must be JSON serializable") from error
        payload: dict[str, object] = {
            "schema": _METADATA_SCHEMA_VERSION,
            "schemaId": _METADATA_SCHEMA_ID,
            "kind": self.kind,
            "range": {
                "scene": [
                    self.envelope_range.scene_start,
                    self.envelope_range.scene_end,
                ],
                "action": [
                    self.envelope_range.action_start,
                    self.envelope_range.action_end,
                ],
                "source": self.envelope_range.source.value,
            },
            "bones": list(self.bone_names),
            "locationBones": list(self.location_bone_names),
            "weightSamples": [list(sample) for sample in self.weight_samples],
            "ui": ui_metadata,
        }
        if self._source_offsets:
            payload["sourcePose"] = [
                {
                    "bone": offset.bone_name,
                    "rotation": list(offset.rotation),
                    "location": list(offset.location),
                }
                for offset in self._source_offsets
            ]
            payload["actionChecksum"] = action_checksum(self.action)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        old_values = {
            key: (key in self.action, self.action.get(key))
            for key in (_METADATA_SCHEMA_KEY, _METADATA_JSON_KEY)
        }
        try:
            self.action[_METADATA_SCHEMA_KEY] = _METADATA_SCHEMA_VERSION
            self.action[_METADATA_JSON_KEY] = encoded
        except Exception:
            for key, (existed, value) in old_values.items():
                if existed:
                    self.action[key] = value
                elif key in self.action:
                    del self.action[key]
            raise
        self._metadata = payload
        self._finish(restore=False)
        return self.bone_names

    def cancel(self) -> tuple[str, ...]:
        """Restore every targeted key/curve attribute and release the preview."""

        self._require_active()
        self._finish(restore=True)
        return self.bone_names

    def _finish(self, *, restore: bool) -> None:
        error: BaseException | None = None
        try:
            if restore:
                self._restore_keys()
        except BaseException as exception:
            error = exception
        finally:
            try:
                # Normal exit commits whichever state is currently in the Action.
                self._transaction.__exit__(None, None, None)
            finally:
                self._active = False
                if _ACTIVE_PREVIEWS.get(_object_key(self._armature)) is self:
                    _ACTIVE_PREVIEWS.pop(_object_key(self._armature), None)
        if error is not None:
            raise error

    def _abandon(self) -> None:
        """Forget invalid Blender IDs after file load without dereferencing them."""

        self._active = False
        try:
            self._transaction.__exit__(None, None, None)
        except Exception:
            pass


_ACTIVE_PREVIEWS: dict[int, CorrectionEnvelopePreview] = {}


def _object_key(armature: bpy.types.Object) -> int:
    try:
        return int(armature.as_pointer())
    except (ReferenceError, TypeError):
        return 0


def active_correction_envelope_preview(
    armature: bpy.types.Object | None,
) -> CorrectionEnvelopePreview | None:
    if armature is None or armature.type != "ARMATURE":
        return None
    preview = _ACTIVE_PREVIEWS.get(_object_key(armature))
    return preview if preview is not None and preview.active else None


def has_correction_envelope_preview(
    armature: bpy.types.Object | None,
) -> bool:
    return active_correction_envelope_preview(armature) is not None


def cancel_all_correction_envelope_previews() -> tuple[str, ...]:
    """Best-effort rollback used before Correction Edit exit/unregister."""

    errors: list[str] = []
    for preview in tuple(_ACTIVE_PREVIEWS.values()):
        try:
            preview.cancel()
        except Exception as error:
            errors.append(str(error))
            preview._abandon()
    _ACTIVE_PREVIEWS.clear()
    return tuple(errors)


def clear_correction_envelope_previews() -> None:
    """Forget stale snapshots after a Blender file load invalidates their IDs."""

    for preview in tuple(_ACTIVE_PREVIEWS.values()):
        preview._abandon()
    _ACTIVE_PREVIEWS.clear()


def _begin_transaction(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    *,
    include_location: bool,
) -> tuple[
    object,
    tuple[bpy.types.PoseBone, ...],
    ActionChannelAccess,
    object,
    Callable[[], None],
]:
    if has_correction_envelope_preview(armature):
        raise ValueError("Apply or Cancel the active Correction Envelope preview first")
    edit_session, bones, access = _correction_edit._require_pose_key_context(
        context,
        armature,
    )
    bone_names = tuple(bone.name for bone in bones)
    _correction_edit._require_range_editable_curves(
        access,
        bone_names,
        include_location=include_location,
    )
    transaction = _correction_edit._correction_key_transaction(
        context,
        access,
        bone_names,
        include_location=include_location,
    )
    restore_keys = transaction.__enter__()
    return edit_session, bones, access, transaction, restore_keys


def _close_failed_transaction(transaction: object) -> None:
    transaction.__exit__(*sys.exc_info())


def begin_pose_envelope_preview(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    envelope_range: EnvelopeRange,
    weight_evaluator: WeightEvaluator,
    *,
    include_location: bool = False,
    location_bone_names: Iterable[str] | None = _DEFAULT_LOCATION_BONES,
    interpolation: str = "LINEAR",
    sample_step: float | None = None,
    sample_tolerance: float = _DEFAULT_ADAPTIVE_TOLERANCE,
    max_samples: int = _DEFAULT_ADAPTIVE_MAX_SAMPLES,
    replace_inner_keys: bool = True,
    _source_offsets: tuple[_PoseOffset, ...] | None = None,
) -> CorrectionEnvelopePreview:
    """Preview the current selected-bone pose delta through an envelope."""

    if not callable(weight_evaluator):
        raise TypeError("Envelope weight evaluator must be callable")
    key_interpolation = _key_interpolation(interpolation)
    if key_interpolation != "LINEAR":
        raise ValueError(
            "Correction Envelope samples require LINEAR interpolation to avoid "
            "independent quaternion-channel overshoot"
        )
    uniform_step = (
        None
        if sample_step is None
        else _finite_number(sample_step, "Envelope sample step")
    )
    if uniform_step is not None and uniform_step <= 0.0:
        raise ValueError("Envelope sample step must be positive")
    adaptive_tolerance = _finite_number(
        sample_tolerance,
        "Envelope sample tolerance",
    )
    if adaptive_tolerance <= 0.0:
        raise ValueError("Envelope sample tolerance must be positive")
    try:
        adaptive_max_samples = int(max_samples)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("Envelope max samples must be an integer") from error
    if (
        adaptive_max_samples < 2
        or adaptive_max_samples > _MAX_PREVIEW_SAMPLES
    ):
        raise ValueError("Envelope max samples must be between 2 and 100000")
    edit_session, bones, access, transaction, restore_keys = _begin_transaction(
        context,
        armature,
        # NLA capture keys location for every selected bone when any root
        # location is requested, so the transaction snapshots all of them.
        include_location=include_location,
    )
    preview: CorrectionEnvelopePreview | None = None
    try:
        validated_range = _validate_envelope_range(
            edit_session.action,
            envelope_range,
        )
        bone_names = tuple(bone.name for bone in bones)
        location_names = _location_targets(
            bone_names,
            include_location=include_location,
            location_bone_names=location_bone_names,
        )
        if _source_offsets is None:
            current_scene = float(context.scene.frame_current) + float(
                context.scene.frame_subframe
            )
            if (
                current_scene < validated_range.scene_start - _FRAME_EPSILON
                or current_scene > validated_range.scene_end + _FRAME_EPSILON
            ):
                raise ValueError("Current frame must be inside the Envelope Scene range")
            current_action = _correction_edit._edit_action_frame(
                context,
                armature,
                edit_session,
                None,
                label="Current Envelope capture frame",
            )
            captured = _correction_edit._capture_nla_remapped_pose_values(
                context,
                armature,
                bones,
                access,
                action_frame=current_action,
                include_location=bool(location_names),
                interpolation=key_interpolation,
            )
            offsets = tuple(
                _PoseOffset(name, rotation, location)
                for name, rotation, location in captured
            )
        else:
            offsets = _source_offsets
            if {offset.bone_name for offset in offsets} != set(bone_names):
                raise ValueError("確定時と同じボーンを選択してカーブを再編集してください")
        # NLA inverse-remap keying is only a capture mechanism.
        restore_keys()
        uniform_scene_frames = (
            _uniform_scene_frames(validated_range, uniform_step)
            if uniform_step is not None
            else None
        )
        location_set = set(location_names)

        def render(
            evaluator: WeightEvaluator,
        ) -> tuple[tuple[float, float], ...]:
            if uniform_scene_frames is None:
                weight_samples = adaptive_envelope_samples(
                    evaluator,
                    tolerance=adaptive_tolerance,
                    rotation_deltas=(offset.rotation for offset in offsets),
                    max_samples=adaptive_max_samples,
                )
                scene_frames = tuple(
                    validated_range.scene_start
                    + (
                        (validated_range.scene_end - validated_range.scene_start)
                        * normalized_time
                    )
                    for normalized_time, _weight in weight_samples
                )
                weights = tuple(weight for _time, weight in weight_samples)
            else:
                scene_frames = uniform_scene_frames
                normalized_times = tuple(
                    _normalized_time(validated_range, frame)
                    for frame in scene_frames
                )
                weights = _evaluate_weights(evaluator, normalized_times)
                weight_samples = tuple(zip(normalized_times, weights, strict=True))
            action_frames = tuple(
                _correction_edit._validate_action_frame(
                    edit_session.action,
                    _correction_edit.scene_to_correction_frame(
                        armature,
                        scene_frame,
                    ),
                    "Envelope sample frame",
                )
                for scene_frame in scene_frames
            )
            # Always rebuild from the pre-preview snapshot; repeated UI drags
            # can never compound numerical error.
            restore_keys()
            try:
                if replace_inner_keys:
                    for bone_name in bone_names:
                        remove_quaternion_keys_in_range(
                            access,
                            bone_name,
                            validated_range.action_start,
                            validated_range.action_end,
                        )
                    for bone_name in location_names:
                        remove_location_keys_in_range(
                            access,
                            bone_name,
                            validated_range.action_start,
                            validated_range.action_end,
                        )
                for offset in offsets:
                    for action_frame, weight in zip(action_frames, weights, strict=True):
                        rotation = weighted_quaternion_delta(offset.rotation, weight)
                        insert_quaternion_key(
                            access,
                            offset.bone_name,
                            action_frame,
                            rotation,
                            interpolation=key_interpolation,
                        )
                        if offset.bone_name in location_set:
                            location = weighted_location_delta(offset.location, weight)
                            insert_location_key(
                                access,
                                offset.bone_name,
                                action_frame,
                                location,
                                interpolation=key_interpolation,
                            )
                context.view_layer.update()
            except Exception:
                restore_keys()
                raise
            return weight_samples

        preview = CorrectionEnvelopePreview(
            context=context,
            armature=armature,
            action=edit_session.action,
            access=access,
            bone_names=bone_names,
            location_bone_names=location_names,
            envelope_range=validated_range,
            evaluator=weight_evaluator,
            transaction=transaction,
            restore_keys=restore_keys,
            render=render,
            kind="POSE_OFFSET",
            source_offsets=offsets,
        )
        _ACTIVE_PREVIEWS[_object_key(armature)] = preview
        try:
            preview.update()
        except Exception:
            preview._finish(restore=True)
            raise
        return preview
    except Exception:
        if preview is None:
            _close_failed_transaction(transaction)
        raise


def begin_reweight_envelope_preview(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    envelope_range: EnvelopeRange,
    weight_evaluator: WeightEvaluator,
    *,
    include_location: bool = False,
    location_bone_names: Iterable[str] | None = _DEFAULT_LOCATION_BONES,
    sample_tolerance: float = _DEFAULT_ADAPTIVE_TOLERANCE,
    max_samples: int = _DEFAULT_ADAPTIVE_MAX_SAMPLES,
) -> CorrectionEnvelopePreview:
    """Preview reweighting of selected existing complete Correction keys."""

    if not callable(weight_evaluator):
        raise TypeError("Envelope weight evaluator must be callable")
    adaptive_tolerance = _finite_number(
        sample_tolerance,
        "Envelope sample tolerance",
    )
    if adaptive_tolerance <= 0.0:
        raise ValueError("Envelope sample tolerance must be positive")
    try:
        adaptive_max_samples = int(max_samples)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("Envelope max samples must be an integer") from error
    if (
        adaptive_max_samples < 2
        or adaptive_max_samples > _MAX_PREVIEW_SAMPLES
    ):
        raise ValueError("Envelope max samples must be between 2 and 100000")
    edit_session, bones, access, transaction, restore_keys = _begin_transaction(
        context,
        armature,
        include_location=include_location,
    )
    preview: CorrectionEnvelopePreview | None = None
    try:
        validated_range = _validate_envelope_range(
            edit_session.action,
            envelope_range,
        )
        bone_names = tuple(bone.name for bone in bones)
        location_names = _location_targets(
            bone_names,
            include_location=include_location,
            location_bone_names=location_bone_names,
        )
        keys = _selected_existing_keys(access, bone_names, location_names)
        if not keys:
            raise ValueError("Select at least one existing Correction key to reweight")
        key_scene_frames = tuple(
            _action_to_scene_frame(armature, key.frame) for key in keys
        )
        normalized_times = tuple(
            _normalized_time(validated_range, frame) for frame in key_scene_frames
        )

        def render(
            evaluator: WeightEvaluator,
        ) -> tuple[tuple[float, float], ...]:
            weights = _evaluate_weights(evaluator, normalized_times)
            weight_samples = adaptive_envelope_samples(
                evaluator,
                tolerance=adaptive_tolerance,
                rotation_deltas=(
                    key.values
                    for key in keys
                    if key.property_name == "rotation_quaternion"
                ),
                max_samples=adaptive_max_samples,
            )
            restore_keys()
            try:
                value_updates: list[tuple[_ExistingKey, Sequence[float]]] = []
                for key, weight in zip(keys, weights, strict=True):
                    if key.property_name == "rotation_quaternion":
                        rotation = tuple(weighted_quaternion_delta(key.values, weight))
                        # Keep the stored quaternion hemisphere when possible,
                        # preserving component-curve continuity and handles.
                        if sum(
                            value * original
                            for value, original in zip(rotation, key.values, strict=True)
                        ) < 0.0:
                            rotation = tuple(-value for value in rotation)
                        values: Sequence[float] = rotation
                    else:
                        values = tuple(weighted_location_delta(key.values, weight))
                    value_updates.append((key, values))
                _set_existing_key_values_preserving_handles(access, value_updates)
                context.view_layer.update()
            except Exception:
                restore_keys()
                raise
            return weight_samples

        preview = CorrectionEnvelopePreview(
            context=context,
            armature=armature,
            action=edit_session.action,
            access=access,
            bone_names=bone_names,
            location_bone_names=location_names,
            envelope_range=validated_range,
            evaluator=weight_evaluator,
            transaction=transaction,
            restore_keys=restore_keys,
            render=render,
            kind="REWEIGHT_EXISTING",
        )
        _ACTIVE_PREVIEWS[_object_key(armature)] = preview
        try:
            preview.update()
        except Exception:
            preview._finish(restore=True)
            raise
        return preview
    except Exception:
        if preview is None:
            _close_failed_transaction(transaction)
        raise


def committed_pose_envelope_available(action: bpy.types.Action | None) -> bool:
    """Whether an Action contains a saved pose source for curve re-editing."""

    if action is None:
        return False
    encoded = action.get(_METADATA_JSON_KEY)
    if not isinstance(encoded, str) or len(encoded) > 100_000:
        return False
    try:
        payload = json.loads(encoded)
    except (TypeError, ValueError):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("schemaId") == _METADATA_SCHEMA_ID
        and payload.get("kind") == "POSE_OFFSET"
        and isinstance(payload.get("sourcePose"), list)
        and payload["sourcePose"]
    )


def begin_committed_pose_envelope_preview(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    weight_evaluator: WeightEvaluator,
) -> CorrectionEnvelopePreview:
    """Rebuild the last committed pose envelope from its original pose delta."""

    session, _bones, _access = _correction_edit._require_pose_key_context(
        context, armature
    )
    action = session.action
    if not committed_pose_envelope_available(action):
        raise ValueError("再編集できる確定済みの補正カーブがありません")
    payload = json.loads(str(action[_METADATA_JSON_KEY]))
    if payload.get("actionChecksum") != action_checksum(action):
        raise ValueError("確定後に修正キーが変更されたため、元のカーブは再編集できません")
    range_data = payload.get("range")
    if not isinstance(range_data, dict):
        raise ValueError("保存された補正カーブの範囲が不正です")
    try:
        scene_range = range_data["scene"]
        action_range = range_data["action"]
        envelope_range = EnvelopeRange(
            _finite_number(scene_range[0], "Saved Scene start"),
            _finite_number(scene_range[1], "Saved Scene end"),
            _finite_number(action_range[0], "Saved Action start"),
            _finite_number(action_range[1], "Saved Action end"),
            EnvelopeRangeSource(range_data["source"]),
        )
        source_pose = payload["sourcePose"]
        offsets = tuple(
            _PoseOffset(
                str(item["bone"]),
                _normalized_quaternion(item["rotation"]),
                tuple(_finite_number(value, "Saved pose location") for value in item["location"]),
            )
            for item in source_pose
        )
        location_names = tuple(str(name) for name in payload["locationBones"])
    except (IndexError, KeyError, TypeError, ValueError) as error:
        raise ValueError("保存された補正カーブの元ポーズが不正です") from error
    if (
        len(offsets) != len(payload.get("bones", ()))
        or len({offset.bone_name for offset in offsets}) != len(offsets)
        or any(len(offset.location) != 3 for offset in offsets)
        or [offset.bone_name for offset in offsets] != payload["bones"]
    ):
        raise ValueError("保存された補正カーブのボーン情報が不正です")
    return begin_pose_envelope_preview(
        context,
        armature,
        envelope_range,
        weight_evaluator,
        include_location=bool(location_names),
        location_bone_names=location_names,
        interpolation="LINEAR",
        replace_inner_keys=True,
        _source_offsets=offsets,
    )


__all__ = (
    "CorrectionEnvelopePreview",
    "EnvelopePreset",
    "EnvelopeRange",
    "EnvelopeRangeSource",
    "WeightEvaluator",
    "adaptive_envelope_samples",
    "active_correction_envelope_preview",
    "begin_committed_pose_envelope_preview",
    "begin_pose_envelope_preview",
    "begin_reweight_envelope_preview",
    "cancel_all_correction_envelope_previews",
    "clear_correction_envelope_previews",
    "committed_pose_envelope_available",
    "evaluate_envelope_preset",
    "has_correction_envelope_preview",
    "preset_weight_evaluator",
    "resolve_envelope_range",
    "selected_correction_key_frames",
    "selected_correction_key_range",
    "weighted_location_delta",
    "weighted_quaternion_delta",
)
