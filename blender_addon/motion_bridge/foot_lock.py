"""Deterministic, Blender-independent foot-lock target generation.

The solver consumes the immutable world-space samples produced by
``MotionRangeSampler`` and emits rest-relative local quaternion targets.  It
does not touch Blender data.  The small adapter methods import the Correction
Writer lazily, so analysis and tests remain usable with ordinary Python.

This MVP locks the sampled foot effector position.  It preserves each frame's
evaluated foot orientation and does not translate Root or Hips.  Reachable
anchors are solved analytically; unreachable anchors are clamped to the
two-bone reach shell and reported instead of stretching the rig.

For validated dense Canonical samples, output is deterministic, finite,
normalized, and quaternion-sign continuous.  Both feet are emitted together
when contacts overlap.  A non-degenerate source knee stays on the same side of
the requested hip-to-ankle axis; a straight/ambiguous knee uses a deterministic
fallback axis.  Neutral source-pose targets immediately outside each interval
prevent Correction FCurve constant extrapolation from leaking the lock.  Exact
world-position locking assumes the generated Canonical rig contract: rigid
leg segments, no animated bone scale/shear, and positive uniform Object scale.
The MVP deliberately does not solve pelvis balance, toe roll, or a fixed foot
orientation.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING, Any, Final, Iterable, Protocol

from .motion_quality import (
    BoneSample,
    MotionFrame,
    MotionSequence,
    QuaternionWXYZ,
    Vec3,
    validate_motion_sequence,
)

if TYPE_CHECKING:
    from .editing.correction_writer import CorrectionFrameTarget


FOOT_NAMES: Final = ("LeftFoot", "RightFoot")
MAX_FOOT_LOCK_INTERVALS: Final = 40_000
_MIN_VECTOR_NORM: Final = 1.0e-12
_FRAME_EPSILON: Final = 1.0e-6


@dataclass(frozen=True, slots=True)
class FootLockLeg:
    foot_name: str
    upper_bone_name: str
    lower_bone_name: str
    parent_bone_name: str = "Hips"


FOOT_LOCK_LEGS: Final[tuple[FootLockLeg, ...]] = (
    FootLockLeg("LeftFoot", "LeftUpperLeg", "LeftLowerLeg"),
    FootLockLeg("RightFoot", "RightUpperLeg", "RightLowerLeg"),
)
_LEG_BY_FOOT: Final = {leg.foot_name: leg for leg in FOOT_LOCK_LEGS}


class ContactIntervalLike(Protocol):
    """Structural bridge for ``editing.foot_contact.FootContactInterval``."""

    frame_start: int
    frame_end: int
    anchor: tuple[float, float, float]
    peak_speed: float
    peak_ground_distance: float


@dataclass(frozen=True, slots=True)
class FootLockSettings:
    """Safe defaults for automatic contact detection and analytic IK."""

    ground_height: float = 0.0
    max_contact_height: float = 0.08
    max_contact_speed: float = 0.15
    min_contact_frames: int = 3
    join_gap_frames: int = 1
    reach_margin: float = 1.0e-6
    chain_position_tolerance: float = 1.0e-4

    def __post_init__(self) -> None:
        object.__setattr__(self, "ground_height", _finite(self.ground_height, "ground_height"))
        object.__setattr__(
            self,
            "max_contact_height",
            _nonnegative(self.max_contact_height, "max_contact_height"),
        )
        object.__setattr__(
            self,
            "max_contact_speed",
            _nonnegative(self.max_contact_speed, "max_contact_speed"),
        )
        object.__setattr__(
            self,
            "min_contact_frames",
            _positive_integer(self.min_contact_frames, "min_contact_frames"),
        )
        object.__setattr__(
            self,
            "join_gap_frames",
            _nonnegative_integer(self.join_gap_frames, "join_gap_frames"),
        )
        object.__setattr__(
            self,
            "reach_margin",
            _positive(self.reach_margin, "reach_margin"),
        )
        object.__setattr__(
            self,
            "chain_position_tolerance",
            _nonnegative(self.chain_position_tolerance, "chain_position_tolerance"),
        )


@dataclass(frozen=True, slots=True)
class FootLockContactInterval:
    """One planted foot interval and its fixed world-space position."""

    foot_name: str
    frame_start: float
    frame_end: float
    anchor_world: Vec3
    sample_count: int
    peak_speed: float = 0.0
    peak_ground_distance: float = 0.0

    def __post_init__(self) -> None:
        if self.foot_name not in _LEG_BY_FOOT:
            raise ValueError("foot_name must be LeftFoot or RightFoot")
        start = _whole_frame(self.frame_start, "frame_start")
        end = _whole_frame(self.frame_end, "frame_end")
        if end < start:
            raise ValueError("Foot Lock interval end must not precede start")
        expected_count = int(end - start) + 1
        count = _positive_integer(self.sample_count, "sample_count")
        if count != expected_count:
            raise ValueError("sample_count must match the inclusive Foot Lock interval")
        object.__setattr__(self, "frame_start", start)
        object.__setattr__(self, "frame_end", end)
        object.__setattr__(self, "anchor_world", _vec3(self.anchor_world, "anchor_world"))
        object.__setattr__(self, "sample_count", count)
        object.__setattr__(
            self,
            "peak_speed",
            _nonnegative(self.peak_speed, "peak_speed"),
        )
        object.__setattr__(
            self,
            "peak_ground_distance",
            _nonnegative(self.peak_ground_distance, "peak_ground_distance"),
        )

    @property
    def start(self) -> float:
        return self.frame_start

    @property
    def end(self) -> float:
        return self.frame_end

    @property
    def anchor(self) -> Vec3:
        return self.anchor_world

    def contains(self, frame: float) -> bool:
        return self.frame_start <= frame <= self.frame_end

    @classmethod
    def from_contact_interval(
        cls,
        foot_name: str,
        interval: ContactIntervalLike,
    ) -> FootLockContactInterval:
        """Adapt the existing Blender contact detector without importing bpy."""

        try:
            start = interval.frame_start
            end = interval.frame_end
            anchor = interval.anchor
            peak_speed = interval.peak_speed
            peak_ground_distance = interval.peak_ground_distance
        except AttributeError as error:
            raise TypeError("interval must provide FootContactInterval fields") from error
        return cls(
            foot_name=foot_name,
            frame_start=start,
            frame_end=end,
            anchor_world=anchor,
            sample_count=int(end - start) + 1,
            peak_speed=peak_speed,
            peak_ground_distance=peak_ground_distance,
        )


@dataclass(frozen=True, slots=True)
class FootLockBoneTarget:
    """Blender-free equivalent of ``CorrectionBoneTarget``."""

    bone_name: str
    local_rotation_wxyz: QuaternionWXYZ

    def __post_init__(self) -> None:
        if not isinstance(self.bone_name, str) or not self.bone_name:
            raise ValueError("bone_name must be a non-empty string")
        object.__setattr__(
            self,
            "local_rotation_wxyz",
            _quaternion(self.local_rotation_wxyz, "local_rotation_wxyz"),
        )


@dataclass(frozen=True, slots=True)
class FootLockFrameTarget:
    """All simultaneous leg targets for one Scene frame."""

    scene_frame: float
    bones: tuple[FootLockBoneTarget, ...]
    contact_feet: tuple[str, ...] = ()
    neutral_feet: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        frame = _whole_frame(self.scene_frame, "scene_frame")
        try:
            bones = tuple(self.bones)
        except TypeError as error:
            raise TypeError("bones must be an iterable of FootLockBoneTarget values") from error
        if not bones or not all(isinstance(value, FootLockBoneTarget) for value in bones):
            raise ValueError("Foot Lock frame must contain FootLockBoneTarget values")
        bones = tuple(sorted(bones, key=lambda value: value.bone_name))
        if len({value.bone_name for value in bones}) != len(bones):
            raise ValueError("Foot Lock frame contains duplicate bone targets")
        contact_feet = _foot_name_tuple(self.contact_feet, "contact_feet")
        neutral_feet = _foot_name_tuple(self.neutral_feet, "neutral_feet")
        if set(contact_feet) & set(neutral_feet):
            raise ValueError("A foot cannot be contact and neutral in the same frame")
        expected_bones = {
            bone_name
            for foot_name in (*contact_feet, *neutral_feet)
            for bone_name in (
                _LEG_BY_FOOT[foot_name].upper_bone_name,
                _LEG_BY_FOOT[foot_name].lower_bone_name,
                foot_name,
            )
        }
        if expected_bones and expected_bones != {value.bone_name for value in bones}:
            raise ValueError("Foot Lock frame metadata does not match its bone targets")
        object.__setattr__(self, "scene_frame", frame)
        object.__setattr__(self, "bones", bones)
        object.__setattr__(self, "contact_feet", contact_feet)
        object.__setattr__(self, "neutral_feet", neutral_feet)

    def to_correction_frame_target(self) -> CorrectionFrameTarget:
        """Create the concrete Writer target; call only inside Blender."""

        from .editing.correction_writer import (  # noqa: PLC0415
            CorrectionBoneTarget,
            CorrectionFrameTarget,
        )

        return CorrectionFrameTarget(
            scene_frame=self.scene_frame,
            bones=tuple(
                CorrectionBoneTarget(
                    bone_name=bone.bone_name,
                    local_rotation_wxyz=bone.local_rotation_wxyz,
                )
                for bone in self.bones
            ),
        )


@dataclass(frozen=True, slots=True)
class FootLockDiagnostic:
    frame: float
    foot_name: str
    requested_anchor_world: Vec3
    solved_effector_world: Vec3
    knee_world: Vec3
    bend_direction_world: Vec3
    requested_reach_distance: float
    reach_distance: float
    minimum_reach: float
    maximum_reach: float
    residual_distance: float
    was_clamped: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame", _whole_frame(self.frame, "frame"))
        if self.foot_name not in _LEG_BY_FOOT:
            raise ValueError("foot_name must be LeftFoot or RightFoot")
        for field_name in (
            "requested_anchor_world",
            "solved_effector_world",
            "knee_world",
            "bend_direction_world",
        ):
            object.__setattr__(self, field_name, _vec3(getattr(self, field_name), field_name))
        for field_name in (
            "requested_reach_distance",
            "reach_distance",
            "minimum_reach",
            "maximum_reach",
            "residual_distance",
        ):
            object.__setattr__(
                self,
                field_name,
                _nonnegative(getattr(self, field_name), field_name),
            )
        if type(self.was_clamped) is not bool:
            raise TypeError("was_clamped must be a boolean")


@dataclass(frozen=True, slots=True)
class FootLockResult:
    """Pure solve output plus diagnostics for safe UI reporting."""

    intervals: tuple[FootLockContactInterval, ...]
    targets: tuple[FootLockFrameTarget, ...]
    diagnostics: tuple[FootLockDiagnostic, ...]

    def __post_init__(self) -> None:
        for field_name, item_type in (
            ("intervals", FootLockContactInterval),
            ("targets", FootLockFrameTarget),
            ("diagnostics", FootLockDiagnostic),
        ):
            try:
                values = tuple(getattr(self, field_name))
            except TypeError as error:
                raise TypeError(f"{field_name} must be iterable") from error
            if not all(isinstance(value, item_type) for value in values):
                raise TypeError(f"{field_name} contains an invalid Foot Lock value")
            object.__setattr__(self, field_name, values)

    @property
    def locked_sample_count(self) -> int:
        return len(self.diagnostics)

    @property
    def clamped_sample_count(self) -> int:
        return sum(value.was_clamped for value in self.diagnostics)

    @property
    def maximum_residual_distance(self) -> float:
        return max((value.residual_distance for value in self.diagnostics), default=0.0)

    @property
    def neutral_sample_count(self) -> int:
        return sum(len(target.neutral_feet) for target in self.targets)

    def contact_anchor(self, foot_name: str, frame: float) -> Vec3:
        """Return the fixed anchor used for one contacting foot and frame."""

        if foot_name not in _LEG_BY_FOOT:
            raise ValueError("foot_name must be LeftFoot or RightFoot")
        checked_frame = _whole_frame(frame, "frame")
        for interval in self.intervals:
            if interval.foot_name == foot_name and interval.contains(checked_frame):
                return interval.anchor_world
        raise ValueError(f"{foot_name} is not locked at Scene frame {checked_frame:g}")

    def to_correction_frame_targets(self) -> tuple[CorrectionFrameTarget, ...]:
        """Convert all targets for ``write_correction_targets`` inside Blender."""

        return tuple(target.to_correction_frame_target() for target in self.targets)


def detect_foot_lock_intervals(
    sequence: MotionSequence,
    settings: FootLockSettings | None = None,
) -> tuple[FootLockContactInterval, ...]:
    """Detect planted runs independently for both canonical feet."""

    checked_settings = _settings(settings)
    _validate_dense_sequence(sequence)
    detected: list[FootLockContactInterval] = []
    for foot_name in FOOT_NAMES:
        positions = tuple(_effector_position(frame, foot_name) for frame in sequence.frames)
        signed_heights = tuple(
            position[2] - checked_settings.ground_height for position in positions
        )
        ground_distances = tuple(abs(value) for value in signed_heights)
        height_candidates = tuple(
            distance <= checked_settings.max_contact_height
            for distance in ground_distances
        )
        speeds = _sample_speeds(
            sequence,
            positions,
            height_candidates,
        )
        flags = tuple(
            height_ok and speed <= checked_settings.max_contact_speed
            for height_ok, speed in zip(height_candidates, speeds, strict=True)
        )
        runs = _join_runs(_true_runs(flags), checked_settings.join_gap_frames)
        for start_index, end_index in runs:
            count = end_index - start_index + 1
            if count < checked_settings.min_contact_frames:
                continue
            selected = positions[start_index : end_index + 1]
            anchor = tuple(
                sum(value[axis] for value in selected) / count for axis in range(3)
            )
            detected.append(
                FootLockContactInterval(
                    foot_name=foot_name,
                    frame_start=sequence.frames[start_index].frame,
                    frame_end=sequence.frames[end_index].frame,
                    anchor_world=anchor,
                    sample_count=count,
                    peak_speed=max(speeds[start_index : end_index + 1]),
                    peak_ground_distance=max(
                        ground_distances[start_index : end_index + 1]
                    ),
                )
            )
    return tuple(sorted(detected, key=lambda value: (value.frame_start, value.foot_name)))


def solve_foot_lock(
    sequence: MotionSequence,
    intervals: Iterable[FootLockContactInterval] | None = None,
    *,
    settings: FootLockSettings | None = None,
) -> FootLockResult:
    """Generate quaternion-only Correction targets for planted feet.

    Two simultaneous contacts are solved in one frame target.  Because the
    canonical legs have disjoint bones, neither side overwrites the other.
    Only one neutral target per side is emitted immediately before and after a
    contact.  If a contact touches a sampled edge, the caller must ensure that
    edge is also the Correction Action edge or sample one padding frame.
    """

    checked_settings = _settings(settings)
    _validate_dense_sequence(sequence)
    checked_intervals = (
        detect_foot_lock_intervals(sequence, checked_settings)
        if intervals is None
        else _validated_intervals(sequence, intervals)
    )
    if not checked_intervals:
        return FootLockResult((), (), ())

    frame_indices = {frame.frame: index for index, frame in enumerate(sequence.frames)}
    intervals_by_index: list[dict[str, FootLockContactInterval]] = [
        {} for _frame in sequence.frames
    ]
    neutral_by_index: list[set[str]] = [set() for _frame in sequence.frames]
    for interval in checked_intervals:
        start_index = frame_indices[interval.frame_start]
        end_index = frame_indices[interval.frame_end]
        for index in range(start_index, end_index + 1):
            intervals_by_index[index][interval.foot_name] = interval
        if start_index > 0:
            neutral_by_index[start_index - 1].add(interval.foot_name)
        if end_index + 1 < len(sequence.frames):
            neutral_by_index[end_index + 1].add(interval.foot_name)
    for index, active in enumerate(intervals_by_index):
        neutral_by_index[index].difference_update(active)

    previous_rotations: dict[str, QuaternionWXYZ] = {}
    targets: list[FootLockFrameTarget] = []
    diagnostics: list[FootLockDiagnostic] = []
    for frame_index, frame in enumerate(sequence.frames):
        active = intervals_by_index[frame_index]
        neutral = neutral_by_index[frame_index]
        if not active and not neutral:
            continue
        frame_bones: list[FootLockBoneTarget] = []
        bone_by_name = {bone.name: bone for bone in frame.bones}
        for foot_name in sorted((*active, *neutral)):
            if foot_name in active:
                interval = active[foot_name]
                solved_bones, diagnostic = _solve_leg(
                    frame,
                    bone_by_name,
                    _LEG_BY_FOOT[foot_name],
                    interval.anchor_world,
                    checked_settings,
                )
                diagnostics.append(diagnostic)
            else:
                solved_bones = _neutral_leg_rotations(
                    frame,
                    bone_by_name,
                    _LEG_BY_FOOT[foot_name],
                )
            for bone_name, quaternion in solved_bones:
                source = bone_by_name[bone_name].local_rotation_wxyz
                continuous = _continuous_quaternion(
                    quaternion,
                    previous_rotations.get(bone_name, _quaternion(source, bone_name)),
                )
                previous_rotations[bone_name] = continuous
                frame_bones.append(FootLockBoneTarget(bone_name, continuous))
        targets.append(
            FootLockFrameTarget(
                frame.frame,
                tuple(frame_bones),
                contact_feet=tuple(sorted(active)),
                neutral_feet=tuple(sorted(neutral)),
            )
        )

    return FootLockResult(
        checked_intervals,
        tuple(targets),
        tuple(diagnostics),
    )


def _solve_leg(
    frame: MotionFrame,
    bones: dict[str, BoneSample],
    leg: FootLockLeg,
    anchor: Vec3,
    settings: FootLockSettings,
) -> tuple[tuple[tuple[str, QuaternionWXYZ], ...], FootLockDiagnostic]:
    required = (
        leg.parent_bone_name,
        leg.upper_bone_name,
        leg.lower_bone_name,
        leg.foot_name,
    )
    missing = tuple(name for name in required if name not in bones)
    if missing:
        raise ValueError(
            f"Frame {frame.frame:g} is missing canonical Foot Lock bones: "
            + ", ".join(missing)
        )
    parent, upper, lower, foot = (bones[name] for name in required)
    if upper.parent != parent.name or lower.parent != upper.name or foot.parent != lower.name:
        raise ValueError(f"Frame {frame.frame:g} has an invalid {leg.foot_name} hierarchy")
    _require_chain_joint(upper.tail_world, lower.head_world, leg, frame, settings)
    _require_chain_joint(lower.tail_world, foot.head_world, leg, frame, settings)
    _require_uniform_positive_scale(frame)

    hip = upper.head_world
    source_knee = upper.tail_world
    source_ankle = lower.tail_world
    source_effector = _effector_position(frame, leg.foot_name)
    foot_offset = _sub(source_effector, foot.head_world)
    requested_ankle = _sub(anchor, foot_offset)
    upper_vector = _sub(source_knee, hip)
    lower_vector = _sub(source_ankle, lower.head_world)
    upper_length = _norm(upper_vector)
    lower_length = _norm(lower_vector)
    if upper_length <= _MIN_VECTOR_NORM or lower_length <= _MIN_VECTOR_NORM:
        raise ValueError(f"Frame {frame.frame:g} has a zero-length {leg.foot_name} leg segment")
    if settings.reach_margin * 2.0 >= 2.0 * min(upper_length, lower_length):
        raise ValueError("reach_margin is too large for a canonical leg segment")

    requested_vector = _sub(requested_ankle, hip)
    requested_distance = _norm(requested_vector)
    fallback_direction = _normalized_or(_sub(source_ankle, hip), upper_vector)
    reach_direction = _normalized_or(requested_vector, fallback_direction)
    minimum_reach = abs(upper_length - lower_length) + settings.reach_margin
    maximum_reach = upper_length + lower_length - settings.reach_margin
    solved_distance = min(max(requested_distance, minimum_reach), maximum_reach)
    solved_ankle = _add(hip, _scale(reach_direction, solved_distance))

    source_knee_vector = _sub(source_knee, hip)
    projected_bend = _sub(
        source_knee_vector,
        _scale(reach_direction, _dot(source_knee_vector, reach_direction)),
    )
    bend_direction = _normalized_or(
        projected_bend,
        _deterministic_perpendicular(reach_direction),
    )
    along = (
        solved_distance * solved_distance
        + upper_length * upper_length
        - lower_length * lower_length
    ) / (2.0 * solved_distance)
    bend_height_squared = max(0.0, upper_length * upper_length - along * along)
    desired_knee = _add(
        _add(hip, _scale(reach_direction, along)),
        _scale(bend_direction, math.sqrt(bend_height_squared)),
    )

    desired_upper_vector = _sub(desired_knee, hip)
    desired_lower_vector = _sub(solved_ankle, desired_knee)
    upper_global = _rotated_global(
        upper.global_rotation_wxyz,
        upper_vector,
        desired_upper_vector,
    )
    lower_global = _rotated_global(
        lower.global_rotation_wxyz,
        lower_vector,
        desired_lower_vector,
    )
    foot_global = _quaternion(foot.global_rotation_wxyz, f"{foot.name}.global")

    parent_global = _quaternion(parent.global_rotation_wxyz, f"{parent.name}.global")
    upper_local = _local_rotation_for_global(
        parent.global_rotation_wxyz,
        parent_global,
        upper,
        upper_global,
    )
    lower_local = _local_rotation_for_global(
        upper.global_rotation_wxyz,
        upper_global,
        lower,
        lower_global,
    )
    foot_local = _local_rotation_for_global(
        lower.global_rotation_wxyz,
        lower_global,
        foot,
        foot_global,
    )

    solved_effector = _add(solved_ankle, foot_offset)
    residual = math.dist(solved_effector, anchor)
    was_clamped = (
        requested_distance < minimum_reach or requested_distance > maximum_reach
    )
    diagnostic = FootLockDiagnostic(
        frame=frame.frame,
        foot_name=leg.foot_name,
        requested_anchor_world=anchor,
        solved_effector_world=solved_effector,
        knee_world=desired_knee,
        bend_direction_world=bend_direction,
        requested_reach_distance=requested_distance,
        reach_distance=solved_distance,
        minimum_reach=minimum_reach,
        maximum_reach=maximum_reach,
        residual_distance=residual,
        was_clamped=was_clamped,
    )
    return (
        (
            (upper.name, upper_local),
            (lower.name, lower_local),
            (foot.name, foot_local),
        ),
        diagnostic,
    )


def _neutral_leg_rotations(
    frame: MotionFrame,
    bones: dict[str, BoneSample],
    leg: FootLockLeg,
) -> tuple[tuple[str, QuaternionWXYZ], ...]:
    names = (leg.upper_bone_name, leg.lower_bone_name, leg.foot_name)
    missing = tuple(name for name in names if name not in bones)
    if missing:
        raise ValueError(
            f"Frame {frame.frame:g} is missing canonical Foot Lock bones: "
            + ", ".join(missing)
        )
    return tuple(
        (name, _quaternion(bones[name].local_rotation_wxyz, f"{name}.local"))
        for name in names
    )


def _local_rotation_for_global(
    original_parent_global: QuaternionWXYZ,
    desired_parent_global: QuaternionWXYZ,
    bone: BoneSample,
    desired_global: QuaternionWXYZ,
) -> QuaternionWXYZ:
    """Infer the fixed rest relation, then solve Blender's local pose basis."""

    old_parent = _quaternion(original_parent_global, "original parent global")
    old_global = _quaternion(bone.global_rotation_wxyz, f"{bone.name}.global")
    old_local = _quaternion(bone.local_rotation_wxyz, f"{bone.name}.local")
    rest_relation = _qmul(_qmul(_qinverse(old_parent), old_global), _qinverse(old_local))
    return _quaternion(
        _qmul(
            _qmul(_qinverse(rest_relation), _qinverse(desired_parent_global)),
            desired_global,
        ),
        f"{bone.name} solved local",
    )


def _rotated_global(
    original_global: QuaternionWXYZ,
    original_direction: Vec3,
    desired_direction: Vec3,
) -> QuaternionWXYZ:
    delta = _quaternion_from_to(original_direction, desired_direction)
    return _quaternion(_qmul(delta, original_global), "solved global rotation")


def _validated_intervals(
    sequence: MotionSequence,
    intervals: Iterable[FootLockContactInterval],
) -> tuple[FootLockContactInterval, ...]:
    if isinstance(intervals, (str, bytes)):
        raise TypeError("intervals must contain FootLockContactInterval values")
    try:
        iterator = iter(intervals)
    except TypeError as error:
        raise TypeError("intervals must be iterable") from error
    collected: list[FootLockContactInterval] = []
    for value in iterator:
        if len(collected) >= MAX_FOOT_LOCK_INTERVALS:
            raise ValueError(
                f"Foot Lock accepts at most {MAX_FOOT_LOCK_INTERVALS} contact intervals"
            )
        collected.append(value)
    result = tuple(collected)
    if not all(isinstance(value, FootLockContactInterval) for value in result):
        raise TypeError("intervals must contain only FootLockContactInterval values")
    frames = {frame.frame for frame in sequence.frames}
    by_foot: dict[str, list[FootLockContactInterval]] = {name: [] for name in FOOT_NAMES}
    for interval in result:
        if interval.frame_start not in frames or interval.frame_end not in frames:
            raise ValueError("Foot Lock interval boundaries must be sampled Scene frames")
        if not sequence.start_frame <= interval.frame_start <= interval.frame_end <= sequence.end_frame:
            raise ValueError("Foot Lock interval is outside the sampled range")
        by_foot[interval.foot_name].append(interval)
    for foot_name, foot_intervals in by_foot.items():
        ordered = sorted(foot_intervals, key=lambda value: value.frame_start)
        if any(
            right.frame_start <= left.frame_end
            for left, right in zip(ordered, ordered[1:], strict=False)
        ):
            raise ValueError(f"{foot_name} Foot Lock intervals must not overlap")
    return tuple(sorted(result, key=lambda value: (value.frame_start, value.foot_name)))


def _validate_dense_sequence(sequence: MotionSequence) -> None:
    validate_motion_sequence(sequence)
    if (
        abs(sequence.frames[0].frame - sequence.start_frame) > _FRAME_EPSILON
        or abs(sequence.frames[-1].frame - sequence.end_frame) > _FRAME_EPSILON
    ):
        raise ValueError("Foot Lock samples must cover the complete declared frame range")
    _whole_frame(sequence.start_frame, "start_frame")
    _whole_frame(sequence.end_frame, "end_frame")
    for frame in sequence.frames:
        _whole_frame(frame.frame, "sample frame")
    if any(
        abs(right.frame - left.frame - 1.0) > _FRAME_EPSILON
        for left, right in zip(sequence.frames, sequence.frames[1:], strict=False)
    ):
        raise ValueError("Foot Lock requires consecutive whole-frame motion samples")


def _settings(settings: FootLockSettings | None) -> FootLockSettings:
    if settings is None:
        return FootLockSettings()
    if not isinstance(settings, FootLockSettings):
        raise TypeError("settings must be FootLockSettings")
    return settings


def _effector_position(frame: MotionFrame, foot_name: str) -> Vec3:
    for effector in frame.effectors:
        if effector.name == foot_name:
            return effector.position_world
    for bone in frame.bones:
        if bone.name == foot_name:
            return bone.tail_world
    raise ValueError(f"Frame {frame.frame:g} has no {foot_name} effector")


def _require_uniform_positive_scale(frame: MotionFrame) -> None:
    x, y, z = frame.root_scale
    if min(x, y, z) <= _MIN_VECTOR_NORM or max(x, y, z) - min(x, y, z) > 1.0e-6:
        raise ValueError(
            f"Frame {frame.frame:g} Root scale must be positive and uniform for Foot Lock"
        )


def _require_chain_joint(
    left: Vec3,
    right: Vec3,
    leg: FootLockLeg,
    frame: MotionFrame,
    settings: FootLockSettings,
) -> None:
    if math.dist(left, right) > settings.chain_position_tolerance:
        raise ValueError(
            f"Frame {frame.frame:g} {leg.foot_name} chain joints are disconnected"
        )


def _sample_speeds(
    sequence: MotionSequence,
    positions: tuple[Vec3, ...],
    height_candidates: tuple[bool, ...],
) -> tuple[float, ...]:
    if len(positions) == 1:
        return (0.0,)
    segment_speeds = tuple(
        math.dist(left_position, right_position)
        / (right_frame.time_seconds - left_frame.time_seconds)
        for left_position, right_position, left_frame, right_frame in zip(
            positions,
            positions[1:],
            sequence.frames,
            sequence.frames[1:],
            strict=False,
        )
    )
    speeds: list[float] = []
    for index in range(len(positions)):
        adjacent: list[float] = []
        if index and height_candidates[index - 1] and height_candidates[index]:
            adjacent.append(segment_speeds[index - 1])
        if (
            index < len(segment_speeds)
            and height_candidates[index]
            and height_candidates[index + 1]
        ):
            adjacent.append(segment_speeds[index])
        speeds.append(max(adjacent, default=0.0))
    return tuple(speeds)


def _true_runs(flags: tuple[bool, ...]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, enabled in enumerate((*flags, False)):
        if enabled and start is None:
            start = index
        elif not enabled and start is not None:
            runs.append((start, index - 1))
            start = None
    return runs


def _join_runs(
    runs: list[tuple[int, int]],
    gap_frames: int,
) -> tuple[tuple[int, int], ...]:
    if not runs:
        return ()
    joined = [runs[0]]
    for start, end in runs[1:]:
        previous_start, previous_end = joined[-1]
        if start - previous_end - 1 <= gap_frames:
            joined[-1] = (previous_start, end)
        else:
            joined.append((start, end))
    return tuple(joined)


def _quaternion_from_to(source: Vec3, target: Vec3) -> QuaternionWXYZ:
    left = _normalized(source, "source direction")
    right = _normalized(target, "target direction")
    cosine = max(-1.0, min(1.0, _dot(left, right)))
    if cosine >= 1.0 - 1.0e-12:
        return (1.0, 0.0, 0.0, 0.0)
    if cosine <= -1.0 + 1.0e-12:
        axis = _deterministic_perpendicular(left)
        return (0.0, axis[0], axis[1], axis[2])
    cross = _cross(left, right)
    return _quaternion((1.0 + cosine, cross[0], cross[1], cross[2]), "from-to")


def _qmul(left: QuaternionWXYZ, right: QuaternionWXYZ) -> QuaternionWXYZ:
    lw, lx, ly, lz = left
    rw, rx, ry, rz = right
    return (
        lw * rw - lx * rx - ly * ry - lz * rz,
        lw * rx + lx * rw + ly * rz - lz * ry,
        lw * ry - lx * rz + ly * rw + lz * rx,
        lw * rz + lx * ry - ly * rx + lz * rw,
    )


def _qinverse(value: QuaternionWXYZ) -> QuaternionWXYZ:
    w, x, y, z = _quaternion(value, "quaternion inverse")
    return (w, -x, -y, -z)


def _continuous_quaternion(
    value: QuaternionWXYZ,
    reference: QuaternionWXYZ,
) -> QuaternionWXYZ:
    checked = _quaternion(value, "continuous quaternion")
    if sum(
        left * right for left, right in zip(checked, reference, strict=True)
    ) < 0.0:
        return tuple(-component for component in checked)  # type: ignore[return-value]
    return checked


def _deterministic_perpendicular(value: Vec3) -> Vec3:
    direction = _normalized(value, "perpendicular direction")
    axes: tuple[Vec3, ...] = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    reference = min(axes, key=lambda axis: abs(_dot(direction, axis)))
    return _normalized(_cross(direction, reference), "perpendicular")


def _add(left: Vec3, right: Vec3) -> Vec3:
    return tuple(a + b for a, b in zip(left, right, strict=True))  # type: ignore[return-value]


def _sub(left: Vec3, right: Vec3) -> Vec3:
    return tuple(a - b for a, b in zip(left, right, strict=True))  # type: ignore[return-value]


def _scale(value: Vec3, scalar: float) -> Vec3:
    return tuple(component * scalar for component in value)  # type: ignore[return-value]


def _dot(left: Vec3, right: Vec3) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _cross(left: Vec3, right: Vec3) -> Vec3:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _norm(value: Vec3) -> float:
    return math.hypot(*value)


def _normalized(value: Vec3, label: str) -> Vec3:
    norm = _norm(value)
    if not math.isfinite(norm) or norm <= _MIN_VECTOR_NORM:
        raise ValueError(f"{label} must have a finite, non-zero length")
    return _scale(value, 1.0 / norm)


def _normalized_or(value: Vec3, fallback: Vec3) -> Vec3:
    if _norm(value) > _MIN_VECTOR_NORM:
        return _normalized(value, "direction")
    return _normalized(fallback, "fallback direction")


def _foot_name_tuple(value: Any, label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{label} must be an iterable of foot names")
    try:
        checked = tuple(value)
    except TypeError as error:
        raise TypeError(f"{label} must be iterable") from error
    if any(name not in _LEG_BY_FOOT for name in checked):
        raise ValueError(f"{label} may contain only LeftFoot and RightFoot")
    if len(set(checked)) != len(checked):
        raise ValueError(f"{label} must not contain duplicates")
    return tuple(sorted(checked))


def _vec3(value: Any, label: str) -> Vec3:
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{label} must contain numeric XYZ values")
    try:
        checked = tuple(value)
    except TypeError as error:
        raise TypeError(f"{label} must contain XYZ values") from error
    if len(checked) != 3:
        raise ValueError(f"{label} must contain exactly three values")
    return tuple(
        _finite(component, f"{label}[{index}]")
        for index, component in enumerate(checked)
    )  # type: ignore[return-value]


def _quaternion(value: Any, label: str) -> QuaternionWXYZ:
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{label} must contain numeric WXYZ values")
    try:
        checked = tuple(value)
    except TypeError as error:
        raise TypeError(f"{label} must contain WXYZ values") from error
    if len(checked) != 4:
        raise ValueError(f"{label} must contain exactly four values")
    components = tuple(
        _finite(component, f"{label}[{index}]")
        for index, component in enumerate(checked)
    )
    norm = math.hypot(*components)
    if norm <= _MIN_VECTOR_NORM:
        raise ValueError(f"{label} must have a non-zero norm")
    return tuple(component / norm for component in components)  # type: ignore[return-value]


def _whole_frame(value: Any, label: str) -> float:
    checked = _finite(value, label)
    rounded = round(checked)
    if abs(checked - rounded) > _FRAME_EPSILON:
        raise ValueError(f"{label} must be a whole Scene frame")
    return float(rounded)


def _finite(value: Any, label: str) -> float:
    if type(value) is bool or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be numeric")
    try:
        checked = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{label} must be finite") from error
    if not math.isfinite(checked):
        raise ValueError(f"{label} must be finite")
    return checked


def _positive(value: Any, label: str) -> float:
    checked = _finite(value, label)
    if checked <= 0.0:
        raise ValueError(f"{label} must be positive")
    return checked


def _nonnegative(value: Any, label: str) -> float:
    checked = _finite(value, label)
    if checked < 0.0:
        raise ValueError(f"{label} must be non-negative")
    return checked


def _positive_integer(value: Any, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")
    if value <= 0:
        raise ValueError(f"{label} must be positive")
    return value


def _nonnegative_integer(value: Any, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")
    if value < 0:
        raise ValueError(f"{label} must be non-negative")
    return value
