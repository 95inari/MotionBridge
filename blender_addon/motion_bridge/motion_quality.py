"""Deterministic, Blender-independent motion quality analysis.

The data classes in this module are the interchange boundary between Blender
sampling and motion tools.  They deliberately use only immutable Python/JSON
values, and quaternion field names include ``wxyz`` to make Blender's component
order explicit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import math
from typing import Any, Final, Iterable, Mapping, TypeAlias

from .canonical.descriptor import canonicalize_json_bytes


Vec3: TypeAlias = tuple[float, float, float]
QuaternionWXYZ: TypeAlias = tuple[float, float, float, float]
JSONScalar: TypeAlias = None | bool | int | float | str
Metadata: TypeAlias = tuple[tuple[str, JSONScalar], ...]

MOTION_SEQUENCE_SCHEMA: Final = "motion-bridge.motion-sequence/1"
MOTION_QUALITY_REPORT_SCHEMA: Final = "motion-bridge.motion-quality-report/1"
MOTION_VALIDATION_SETTINGS_SCHEMA: Final = "motion-bridge.motion-validation-settings/1"

MIN_FPS: Final = 0.001
MAX_FPS: Final = 480.0
MAX_FRAME_ABS: Final = 1_000_000_000.0
MAX_SOURCE_ID_LENGTH: Final = 1024
MAX_NAME_LENGTH: Final = 256
MAX_ABS_COORDINATE_LIMIT: Final = 1.0e12
MAX_MOTION_QUALITY_ISSUES: Final = 4096
_MAX_SAFE_INTEGER: Final = (1 << 53) - 1
_MIN_QUATERNION_NORM: Final = 1.0e-12


class IssueSeverity(str, Enum):
    """Action level for an individual quality issue."""

    REVIEW = "review"
    FIX = "fix"


class QualitySummary(str, Enum):
    """Beginner-facing three-level result."""

    NO_ISSUES = "no_issues"
    REVIEW_RECOMMENDED = "review_recommended"
    FIX_RECOMMENDED = "fix_recommended"


@dataclass(frozen=True, slots=True)
class BoneSample:
    """Evaluated bone pose; rotations use Blender's WXYZ component order."""

    name: str
    local_rotation_wxyz: QuaternionWXYZ
    global_rotation_wxyz: QuaternionWXYZ
    head_world: Vec3
    tail_world: Vec3
    parent: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name, "bone name"))
        if self.parent is not None:
            object.__setattr__(self, "parent", _name(self.parent, "bone parent"))
        object.__setattr__(
            self,
            "local_rotation_wxyz",
            _quaternion(self.local_rotation_wxyz, f"{self.name}.local_rotation_wxyz"),
        )
        object.__setattr__(
            self,
            "global_rotation_wxyz",
            _quaternion(self.global_rotation_wxyz, f"{self.name}.global_rotation_wxyz"),
        )
        object.__setattr__(self, "head_world", _vec3(self.head_world, f"{self.name}.head_world"))
        object.__setattr__(self, "tail_world", _vec3(self.tail_world, f"{self.name}.tail_world"))

    def to_canonical_data(self) -> dict[str, Any]:
        return {
            "globalRotationWxyz": list(self.global_rotation_wxyz),
            "headWorld": list(self.head_world),
            "localRotationWxyz": list(self.local_rotation_wxyz),
            "name": self.name,
            "parent": self.parent,
            "tailWorld": list(self.tail_world),
        }


@dataclass(frozen=True, slots=True)
class EffectorSample:
    """Evaluated world-space hand, foot, or custom effector."""

    name: str
    position_world: Vec3
    rotation_wxyz: QuaternionWXYZ | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name, "effector name"))
        object.__setattr__(
            self,
            "position_world",
            _vec3(self.position_world, f"{self.name}.position_world"),
        )
        if self.rotation_wxyz is not None:
            object.__setattr__(
                self,
                "rotation_wxyz",
                _quaternion(self.rotation_wxyz, f"{self.name}.rotation_wxyz"),
            )

    def to_canonical_data(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "positionWorld": list(self.position_world),
            "rotationWxyz": None if self.rotation_wxyz is None else list(self.rotation_wxyz),
        }


@dataclass(frozen=True, slots=True)
class ContactSample:
    """Contact state for one effector at one sampled frame."""

    effector: str
    active: bool
    ground_height: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "effector", _name(self.effector, "contact effector"))
        if type(self.active) is not bool:
            raise TypeError("contact active must be a boolean")
        object.__setattr__(
            self,
            "ground_height",
            _finite_float(self.ground_height, f"{self.effector}.ground_height"),
        )

    def to_canonical_data(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "effector": self.effector,
            "groundHeight": self.ground_height,
        }


@dataclass(frozen=True, slots=True)
class MotionFrame:
    """One evaluated sample with monotonic time using an arbitrary origin."""

    frame: float
    time_seconds: float
    root_location: Vec3
    root_rotation_wxyz: QuaternionWXYZ
    root_scale: Vec3
    bones: tuple[BoneSample, ...]
    effectors: tuple[EffectorSample, ...] = ()
    contacts: tuple[ContactSample, ...] = ()
    key_count: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame", _finite_float(self.frame, "frame"))
        object.__setattr__(
            self,
            "time_seconds",
            _finite_float(self.time_seconds, f"frame {self.frame:g} time_seconds"),
        )
        object.__setattr__(self, "root_location", _vec3(self.root_location, "root_location"))
        object.__setattr__(
            self,
            "root_rotation_wxyz",
            _quaternion(self.root_rotation_wxyz, "root_rotation_wxyz"),
        )
        object.__setattr__(self, "root_scale", _vec3(self.root_scale, "root_scale"))

        bones = _typed_sorted_tuple(self.bones, BoneSample, "bones", lambda value: value.name)
        effectors = _typed_sorted_tuple(
            self.effectors,
            EffectorSample,
            "effectors",
            lambda value: value.name,
        )
        contacts = _typed_sorted_tuple(
            self.contacts,
            ContactSample,
            "contacts",
            lambda value: value.effector,
        )
        object.__setattr__(self, "bones", bones)
        object.__setattr__(self, "effectors", effectors)
        object.__setattr__(self, "contacts", contacts)

        if self.key_count is not None:
            object.__setattr__(self, "key_count", _nonnegative_integer(self.key_count, "key_count"))

    def to_canonical_data(self) -> dict[str, Any]:
        return {
            "bones": [bone.to_canonical_data() for bone in self.bones],
            "contacts": [contact.to_canonical_data() for contact in self.contacts],
            "effectors": [effector.to_canonical_data() for effector in self.effectors],
            "frame": self.frame,
            "keyCount": self.key_count,
            "rootLocation": list(self.root_location),
            "rootRotationWxyz": list(self.root_rotation_wxyz),
            "rootScale": list(self.root_scale),
            "timeSeconds": self.time_seconds,
        }


@dataclass(frozen=True, slots=True)
class MotionSequence:
    """Immutable, JSON-serializable motion sampled in Blender world units."""

    fps: float
    start_frame: float
    end_frame: float
    frames: tuple[MotionFrame, ...]
    source_id: str = ""
    keyed_channel_count: int | None = None
    metadata: Metadata = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "fps", _finite_float(self.fps, "fps"))
        object.__setattr__(self, "start_frame", _finite_float(self.start_frame, "start_frame"))
        object.__setattr__(self, "end_frame", _finite_float(self.end_frame, "end_frame"))
        try:
            frames = tuple(self.frames)
        except TypeError as error:
            raise TypeError("frames must be an iterable of MotionFrame values") from error
        if not all(isinstance(frame, MotionFrame) for frame in frames):
            raise TypeError("frames must contain only MotionFrame values")
        object.__setattr__(self, "frames", frames)

        if not isinstance(self.source_id, str):
            raise TypeError("source_id must be a string")
        if len(self.source_id) > MAX_SOURCE_ID_LENGTH:
            raise ValueError(f"source_id must contain at most {MAX_SOURCE_ID_LENGTH} characters")
        if self.keyed_channel_count is not None:
            value = _positive_integer(self.keyed_channel_count, "keyed_channel_count")
            object.__setattr__(self, "keyed_channel_count", value)
        object.__setattr__(self, "metadata", _metadata(self.metadata))

    def to_canonical_data(self) -> dict[str, Any]:
        return {
            "endFrame": self.end_frame,
            "fps": self.fps,
            "frames": [frame.to_canonical_data() for frame in self.frames],
            "keyedChannelCount": self.keyed_channel_count,
            "metadata": {key: value for key, value in self.metadata},
            "schema": MOTION_SEQUENCE_SCHEMA,
            "sourceId": self.source_id,
            "startFrame": self.start_frame,
        }

    def canonical_json(self) -> str:
        return canonicalize_json_bytes(self.to_canonical_data()).decode("utf-8")

    def content_hash(self) -> str:
        return hashlib.sha256(canonicalize_json_bytes(self.to_canonical_data())).hexdigest()


@dataclass(frozen=True, slots=True)
class LimbDefinition:
    """A reach chain measured from its first bone head to an effector."""

    name: str
    bones: tuple[str, ...]
    effector: str
    rest_length: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name, "limb name"))
        try:
            bones = tuple(_name(value, f"{self.name} bone") for value in self.bones)
        except TypeError as error:
            raise TypeError("limb bones must be an iterable of names") from error
        if not bones:
            raise ValueError(f"{self.name} must contain at least one bone")
        if len(set(bones)) != len(bones):
            raise ValueError(f"{self.name} contains duplicate bones")
        object.__setattr__(self, "bones", bones)
        object.__setattr__(self, "effector", _name(self.effector, "limb effector"))
        if self.rest_length is not None:
            rest_length = _finite_float(self.rest_length, f"{self.name}.rest_length")
            if rest_length <= 0.0:
                raise ValueError(f"{self.name}.rest_length must be positive")
            object.__setattr__(self, "rest_length", rest_length)

    def to_canonical_data(self) -> dict[str, Any]:
        return {
            "bones": list(self.bones),
            "effector": self.effector,
            "name": self.name,
            "restLength": self.rest_length,
        }


@dataclass(frozen=True, slots=True)
class MotionValidationSettings:
    """Thresholds and input budgets for deterministic validation.

    Warning thresholds map to :class:`IssueSeverity.REVIEW`; error thresholds
    map to :class:`IssueSeverity.FIX`.  A value equal to a threshold is accepted;
    an issue is created only when it exceeds that threshold.
    """

    quaternion_unit_warning: float = 1.0e-3
    quaternion_unit_error: float = 1.0e-2
    bone_angular_speed_warning_deg_s: float = 720.0
    bone_angular_speed_error_deg_s: float = 1080.0
    bone_angular_acceleration_warning_deg_s2: float = 10_000.0
    bone_angular_acceleration_error_deg_s2: float = 20_000.0
    root_translation_warning_m_per_frame: float = 0.10
    root_translation_error_m_per_frame: float = 0.25
    root_rotation_warning_deg_per_frame: float = 45.0
    root_rotation_error_deg_per_frame: float = 90.0
    foot_slide_warning_m: float = 0.02
    foot_slide_error_m: float = 0.05
    ground_penetration_warning_m: float = 0.01
    ground_penetration_error_m: float = 0.03
    limb_reach_warning_ratio: float = 0.98
    limb_reach_error_ratio: float = 1.02
    key_density_warning_keys_channel_s: float = 18.0
    key_density_error_keys_channel_s: float = 28.0
    auto_detect_contacts: bool = True
    contact_effectors: tuple[str, ...] = ("LeftFoot", "RightFoot")
    contact_ground_height: float = 0.0
    contact_max_height: float = 0.05
    contact_max_speed_m_s: float = 0.20
    contact_min_samples: int = 2
    limbs: tuple[LimbDefinition, ...] = field(default_factory=lambda: DEFAULT_LIMBS)
    max_frame_count: int = 100_000
    max_total_bone_samples: int = 2_000_000
    max_bone_count: int = 256
    max_effector_count: int = 64
    max_duration_seconds: float = 7_200.0
    max_frame_span: float = 10_000_000.0
    max_abs_coordinate: float = 1_000_000.0
    time_tolerance_seconds: float = 1.0e-6

    def __post_init__(self) -> None:
        threshold_pairs = (
            ("quaternion_unit", self.quaternion_unit_warning, self.quaternion_unit_error),
            (
                "bone_angular_speed",
                self.bone_angular_speed_warning_deg_s,
                self.bone_angular_speed_error_deg_s,
            ),
            (
                "bone_angular_acceleration",
                self.bone_angular_acceleration_warning_deg_s2,
                self.bone_angular_acceleration_error_deg_s2,
            ),
            (
                "root_translation",
                self.root_translation_warning_m_per_frame,
                self.root_translation_error_m_per_frame,
            ),
            (
                "root_rotation",
                self.root_rotation_warning_deg_per_frame,
                self.root_rotation_error_deg_per_frame,
            ),
            ("foot_slide", self.foot_slide_warning_m, self.foot_slide_error_m),
            (
                "ground_penetration",
                self.ground_penetration_warning_m,
                self.ground_penetration_error_m,
            ),
            ("limb_reach", self.limb_reach_warning_ratio, self.limb_reach_error_ratio),
            (
                "key_density",
                self.key_density_warning_keys_channel_s,
                self.key_density_error_keys_channel_s,
            ),
        )
        for label, warning, error in threshold_pairs:
            warning_value = _finite_float(warning, f"{label} warning")
            error_value = _finite_float(error, f"{label} error")
            if warning_value < 0.0 or error_value < warning_value:
                raise ValueError(f"{label} thresholds require 0 <= warning <= error")

        if type(self.auto_detect_contacts) is not bool:
            raise TypeError("auto_detect_contacts must be a boolean")
        names = tuple(sorted(_name(value, "contact effector") for value in self.contact_effectors))
        if len(set(names)) != len(names):
            raise ValueError("contact_effectors must not contain duplicates")
        object.__setattr__(self, "contact_effectors", names)

        _finite_float(self.contact_ground_height, "contact_ground_height")
        for field_name in ("contact_max_height", "contact_max_speed_m_s"):
            _finite_float(getattr(self, field_name), field_name)
        if self.contact_max_height < 0.0 or self.contact_max_speed_m_s < 0.0:
            raise ValueError("contact detection limits must be non-negative")
        for field_name in (
            "max_duration_seconds",
            "max_frame_span",
            "max_abs_coordinate",
            "time_tolerance_seconds",
        ):
            value = _finite_float(getattr(self, field_name), field_name)
            if value <= 0.0:
                raise ValueError(f"{field_name} must be positive")
        if self.max_abs_coordinate > MAX_ABS_COORDINATE_LIMIT:
            raise ValueError(
                f"max_abs_coordinate must not exceed {MAX_ABS_COORDINATE_LIMIT:g}"
            )

        for field_name in (
            "contact_min_samples",
            "max_frame_count",
            "max_total_bone_samples",
            "max_bone_count",
            "max_effector_count",
        ):
            _positive_integer(getattr(self, field_name), field_name)

        try:
            limbs = tuple(self.limbs)
        except TypeError as error:
            raise TypeError("limbs must be an iterable of LimbDefinition values") from error
        if not all(isinstance(limb, LimbDefinition) for limb in limbs):
            raise TypeError("limbs must contain only LimbDefinition values")
        limbs = tuple(sorted(limbs, key=lambda value: value.name))
        if len({limb.name for limb in limbs}) != len(limbs):
            raise ValueError("limbs must have unique names")
        object.__setattr__(self, "limbs", limbs)

    def to_canonical_data(self) -> dict[str, Any]:
        return {
            "autoDetectContacts": self.auto_detect_contacts,
            "boneAngularAccelerationErrorDegS2": self.bone_angular_acceleration_error_deg_s2,
            "boneAngularAccelerationWarningDegS2": self.bone_angular_acceleration_warning_deg_s2,
            "boneAngularSpeedErrorDegS": self.bone_angular_speed_error_deg_s,
            "boneAngularSpeedWarningDegS": self.bone_angular_speed_warning_deg_s,
            "contactEffectors": list(self.contact_effectors),
            "contactGroundHeight": self.contact_ground_height,
            "contactMaxHeight": self.contact_max_height,
            "contactMaxSpeedMS": self.contact_max_speed_m_s,
            "contactMinSamples": self.contact_min_samples,
            "footSlideErrorM": self.foot_slide_error_m,
            "footSlideWarningM": self.foot_slide_warning_m,
            "groundPenetrationErrorM": self.ground_penetration_error_m,
            "groundPenetrationWarningM": self.ground_penetration_warning_m,
            "keyDensityErrorKeysChannelS": self.key_density_error_keys_channel_s,
            "keyDensityWarningKeysChannelS": self.key_density_warning_keys_channel_s,
            "limbReachErrorRatio": self.limb_reach_error_ratio,
            "limbReachWarningRatio": self.limb_reach_warning_ratio,
            "limbs": [limb.to_canonical_data() for limb in self.limbs],
            "maxAbsCoordinate": self.max_abs_coordinate,
            "maxBoneCount": self.max_bone_count,
            "maxDurationSeconds": self.max_duration_seconds,
            "maxEffectorCount": self.max_effector_count,
            "maxFrameCount": self.max_frame_count,
            "maxFrameSpan": self.max_frame_span,
            "maxTotalBoneSamples": self.max_total_bone_samples,
            "quaternionUnitError": self.quaternion_unit_error,
            "quaternionUnitWarning": self.quaternion_unit_warning,
            "rootRotationErrorDegPerFrame": self.root_rotation_error_deg_per_frame,
            "rootRotationWarningDegPerFrame": self.root_rotation_warning_deg_per_frame,
            "rootTranslationErrorMPerFrame": self.root_translation_error_m_per_frame,
            "rootTranslationWarningMPerFrame": self.root_translation_warning_m_per_frame,
            "schema": MOTION_VALIDATION_SETTINGS_SCHEMA,
            "timeToleranceSeconds": self.time_tolerance_seconds,
        }

    def canonical_json(self) -> str:
        return canonicalize_json_bytes(self.to_canonical_data()).decode("utf-8")

    def content_hash(self) -> str:
        return hashlib.sha256(canonicalize_json_bytes(self.to_canonical_data())).hexdigest()


@dataclass(frozen=True, slots=True)
class FrameInterval:
    start_frame: float
    end_frame: float
    sample_count: int

    def __post_init__(self) -> None:
        start = _finite_float(self.start_frame, "interval start_frame")
        end = _finite_float(self.end_frame, "interval end_frame")
        if end < start:
            raise ValueError("interval end_frame must not precede start_frame")
        object.__setattr__(self, "start_frame", start)
        object.__setattr__(self, "end_frame", end)
        object.__setattr__(self, "sample_count", _positive_integer(self.sample_count, "sample_count"))

    def to_canonical_data(self) -> dict[str, Any]:
        return {
            "endFrame": self.end_frame,
            "sampleCount": self.sample_count,
            "startFrame": self.start_frame,
        }


@dataclass(frozen=True, slots=True)
class MetricSummary:
    name: str
    unit: str
    maximum: float | None
    p95: float | None
    count: int
    exceedance_count: int
    frame_range: FrameInterval | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name, "metric name"))
        if not isinstance(self.unit, str):
            raise TypeError("metric unit must be a string")
        if self.maximum is not None:
            object.__setattr__(self, "maximum", _finite_float(self.maximum, "metric maximum"))
        if self.p95 is not None:
            object.__setattr__(self, "p95", _finite_float(self.p95, "metric p95"))
        object.__setattr__(self, "count", _nonnegative_integer(self.count, "metric count"))
        object.__setattr__(
            self,
            "exceedance_count",
            _nonnegative_integer(self.exceedance_count, "metric exceedance_count"),
        )
        if self.exceedance_count > self.count:
            raise ValueError("metric exceedance_count must not exceed count")
        if self.count == 0 and (self.maximum is not None or self.p95 is not None):
            raise ValueError("empty metrics cannot have maximum or p95 values")
        if self.count > 0 and (self.maximum is None or self.p95 is None or self.frame_range is None):
            raise ValueError("non-empty metrics require maximum, p95, and frame_range")

    def to_canonical_data(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "exceedanceCount": self.exceedance_count,
            "max": self.maximum,
            "name": self.name,
            "p95": self.p95,
            "range": None if self.frame_range is None else self.frame_range.to_canonical_data(),
            "unit": self.unit,
        }


@dataclass(frozen=True, slots=True)
class MotionIssue:
    code: str
    metric: str
    severity: IssueSeverity
    subject: str
    interval: FrameInterval
    peak: float
    threshold: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _name(self.code, "issue code"))
        object.__setattr__(self, "metric", _name(self.metric, "issue metric"))
        if not isinstance(self.severity, IssueSeverity):
            raise TypeError("issue severity must be an IssueSeverity")
        if not isinstance(self.subject, str):
            raise TypeError("issue subject must be a string")
        object.__setattr__(self, "peak", _finite_float(self.peak, "issue peak"))
        object.__setattr__(self, "threshold", _finite_float(self.threshold, "issue threshold"))

    def to_canonical_data(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "interval": self.interval.to_canonical_data(),
            "metric": self.metric,
            "peak": self.peak,
            "severity": self.severity.value,
            "subject": self.subject,
            "threshold": self.threshold,
        }


@dataclass(frozen=True, slots=True)
class MotionQualityReport:
    source_hash: str
    settings_hash: str
    summary: QualitySummary
    sample_count: int
    input_finite: bool
    metrics: tuple[MetricSummary, ...]
    issues: tuple[MotionIssue, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not _is_sha256(self.source_hash) or not _is_sha256(self.settings_hash):
            raise ValueError("report hashes must be lowercase SHA-256 values")
        if not isinstance(self.summary, QualitySummary):
            raise TypeError("summary must be a QualitySummary")
        object.__setattr__(self, "sample_count", _positive_integer(self.sample_count, "sample_count"))
        if type(self.input_finite) is not bool:
            raise TypeError("input_finite must be a boolean")
        if self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        metrics = tuple(sorted(self.metrics, key=lambda value: value.name))
        if not all(isinstance(metric, MetricSummary) for metric in metrics):
            raise TypeError("metrics must contain only MetricSummary values")
        if len({metric.name for metric in metrics}) != len(metrics):
            raise ValueError("metrics must have unique names")
        issues = tuple(
            sorted(
                self.issues,
                key=lambda value: (
                    value.interval.start_frame,
                    value.interval.end_frame,
                    value.code,
                    value.subject,
                    value.severity.value,
                ),
            )
        )
        if not all(isinstance(issue, MotionIssue) for issue in issues):
            raise TypeError("issues must contain only MotionIssue values")
        object.__setattr__(self, "metrics", metrics)
        object.__setattr__(self, "issues", issues)

    def metric(self, name: str) -> MetricSummary:
        for metric in self.metrics:
            if metric.name == name:
                return metric
        raise KeyError(name)

    def to_canonical_data(self) -> dict[str, Any]:
        return {
            "inputFinite": self.input_finite,
            "issues": [issue.to_canonical_data() for issue in self.issues],
            "metrics": [metric.to_canonical_data() for metric in self.metrics],
            "sampleCount": self.sample_count,
            "schema": MOTION_QUALITY_REPORT_SCHEMA,
            "schemaVersion": self.schema_version,
            "settingsHash": self.settings_hash,
            "sourceHash": self.source_hash,
            "summary": self.summary.value,
        }

    def canonical_json(self) -> str:
        return canonicalize_json_bytes(self.to_canonical_data()).decode("utf-8")

    def content_hash(self) -> str:
        return hashlib.sha256(canonicalize_json_bytes(self.to_canonical_data())).hexdigest()


@dataclass(frozen=True, slots=True)
class _Observation:
    value: float
    frame: float
    sample_index: int
    subject: str


@dataclass(frozen=True, slots=True)
class _MetricDefinition:
    unit: str
    issue_code: str
    warning: float
    error: float


def validate_motion_sequence(
    sequence: MotionSequence,
    settings: MotionValidationSettings | None = None,
) -> None:
    """Reject malformed or unsafe-to-analyze input.

    Non-finite values are rejected at construction time so they can never
    contaminate p95 or canonical JSON.  This function enforces cross-frame
    invariants and configurable resource/range limits.
    """

    if not isinstance(sequence, MotionSequence):
        raise TypeError("sequence must be a MotionSequence")
    checked = MotionValidationSettings() if settings is None else settings
    if not isinstance(checked, MotionValidationSettings):
        raise TypeError("settings must be MotionValidationSettings")
    if not MIN_FPS <= sequence.fps <= MAX_FPS:
        raise ValueError(f"fps must be between {MIN_FPS:g} and {MAX_FPS:g}")
    if abs(sequence.start_frame) > MAX_FRAME_ABS or abs(sequence.end_frame) > MAX_FRAME_ABS:
        raise ValueError(f"frame range must stay within +/-{MAX_FRAME_ABS:g}")
    if sequence.end_frame < sequence.start_frame:
        raise ValueError("end_frame must not precede start_frame")
    if sequence.end_frame - sequence.start_frame > checked.max_frame_span:
        raise ValueError("frame range exceeds max_frame_span")
    if not sequence.frames:
        raise ValueError("motion sequence must contain at least one frame")
    if len(sequence.frames) > checked.max_frame_count:
        raise ValueError("motion sequence exceeds max_frame_count")
    if not sequence.start_frame <= sequence.frames[0].frame <= sequence.end_frame:
        raise ValueError("first sampled frame is outside the declared range")
    if not sequence.start_frame <= sequence.frames[-1].frame <= sequence.end_frame:
        raise ValueError("last sampled frame is outside the declared range")

    first = sequence.frames[0]
    bone_names = tuple(bone.name for bone in first.bones)
    effector_names = tuple(effector.name for effector in first.effectors)
    parents = {bone.name: bone.parent for bone in first.bones}
    _validate_hierarchy(parents)
    if len(bone_names) > checked.max_bone_count:
        raise ValueError("motion sequence exceeds max_bone_count")
    if len(effector_names) > checked.max_effector_count:
        raise ValueError("motion sequence exceeds max_effector_count")
    if len(sequence.frames) * len(bone_names) > checked.max_total_bone_samples:
        raise ValueError("motion sequence exceeds max_total_bone_samples")

    saw_key_count = False
    saw_missing_key_count = False
    previous: MotionFrame | None = None
    for index, frame in enumerate(sequence.frames):
        if tuple(bone.name for bone in frame.bones) != bone_names:
            raise ValueError(f"frame {frame.frame:g} has an inconsistent bone set")
        if tuple(effector.name for effector in frame.effectors) != effector_names:
            raise ValueError(f"frame {frame.frame:g} has an inconsistent effector set")
        if {bone.name: bone.parent for bone in frame.bones} != parents:
            raise ValueError(f"frame {frame.frame:g} has an inconsistent bone hierarchy")

        available_effectors = set(effector_names) | set(bone_names)
        for contact in frame.contacts:
            if contact.effector not in available_effectors:
                raise ValueError(
                    f"frame {frame.frame:g} contact references missing effector {contact.effector!r}"
                )

        if frame.key_count is None:
            saw_missing_key_count = True
        else:
            saw_key_count = True
        _validate_coordinate_limits(frame, checked.max_abs_coordinate)

        if previous is not None:
            if frame.frame <= previous.frame:
                raise ValueError("sampled frames must be strictly increasing")
            if frame.time_seconds <= previous.time_seconds:
                raise ValueError("sample times must be strictly increasing")
            expected_delta = (frame.frame - previous.frame) / sequence.fps
            actual_delta = frame.time_seconds - previous.time_seconds
            tolerance = max(checked.time_tolerance_seconds, expected_delta * 1.0e-9)
            if abs(actual_delta - expected_delta) > tolerance:
                raise ValueError(
                    f"sample time delta at frame {frame.frame:g} does not match frame delta/fps"
                )
        previous = frame

    duration = sequence.frames[-1].time_seconds - sequence.frames[0].time_seconds
    if duration > checked.max_duration_seconds:
        raise ValueError("motion sequence exceeds max_duration_seconds")
    if saw_key_count and saw_missing_key_count:
        raise ValueError("key_count must be supplied for every frame or no frames")
    if saw_key_count and sequence.keyed_channel_count is None:
        raise ValueError("keyed_channel_count is required when frame key_count is supplied")
    if not saw_key_count and sequence.keyed_channel_count is not None:
        raise ValueError("frame key_count is required when keyed_channel_count is supplied")


def analyze_motion(
    sequence: MotionSequence,
    settings: MotionValidationSettings | None = None,
) -> MotionQualityReport:
    """Return deterministic metrics, issue intervals, and a three-level summary."""

    checked = MotionValidationSettings() if settings is None else settings
    validate_motion_sequence(sequence, checked)
    definitions = _metric_definitions(checked)
    observations: dict[str, list[_Observation]] = {name: [] for name in definitions}

    _observe_quaternion_quality(sequence, observations)
    _observe_bone_dynamics(sequence, observations)
    _observe_root_discontinuities(sequence, observations)
    _observe_contacts(sequence, checked, observations)
    _observe_limb_reach(sequence, checked, observations)
    _observe_key_density(sequence, observations)

    issues: list[MotionIssue] = []
    metrics: list[MetricSummary] = []
    for name in sorted(definitions):
        definition = definitions[name]
        metric_observations = observations[name]
        issue_observations: list[tuple[_Observation, IssueSeverity, float]] = []
        for observation in metric_observations:
            severity = _severity(observation.value, definition.warning, definition.error)
            if severity is not None:
                threshold = definition.error if severity is IssueSeverity.FIX else definition.warning
                issue_observations.append((observation, severity, threshold))
        metrics.append(
            _metric_summary(
                name,
                definition.unit,
                metric_observations,
                len(issue_observations),
            )
        )
        issues.extend(
            _issue_intervals(
                metric=name,
                code=definition.issue_code,
                observations=issue_observations,
                maximum_count=MAX_MOTION_QUALITY_ISSUES - len(issues),
            )
        )

    if any(issue.severity is IssueSeverity.FIX for issue in issues):
        summary = QualitySummary.FIX_RECOMMENDED
    elif issues:
        summary = QualitySummary.REVIEW_RECOMMENDED
    else:
        summary = QualitySummary.NO_ISSUES

    return MotionQualityReport(
        source_hash=sequence.content_hash(),
        settings_hash=checked.content_hash(),
        summary=summary,
        sample_count=len(sequence.frames),
        input_finite=True,
        metrics=tuple(metrics),
        issues=tuple(issues),
    )


def _metric_definitions(settings: MotionValidationSettings) -> dict[str, _MetricDefinition]:
    return {
        "bone_angular_acceleration": _MetricDefinition(
            "deg/s^2",
            "bone_angular_acceleration_spike",
            settings.bone_angular_acceleration_warning_deg_s2,
            settings.bone_angular_acceleration_error_deg_s2,
        ),
        "bone_angular_speed": _MetricDefinition(
            "deg/s",
            "bone_angular_speed_spike",
            settings.bone_angular_speed_warning_deg_s,
            settings.bone_angular_speed_error_deg_s,
        ),
        "foot_slide": _MetricDefinition(
            "m",
            "foot_slide_during_contact",
            settings.foot_slide_warning_m,
            settings.foot_slide_error_m,
        ),
        "ground_penetration": _MetricDefinition(
            "m",
            "ground_penetration_during_contact",
            settings.ground_penetration_warning_m,
            settings.ground_penetration_error_m,
        ),
        "key_density": _MetricDefinition(
            "keys/channel/s",
            "key_density_high",
            settings.key_density_warning_keys_channel_s,
            settings.key_density_error_keys_channel_s,
        ),
        "limb_reach": _MetricDefinition(
            "ratio",
            "limb_reach_exceeded",
            settings.limb_reach_warning_ratio,
            settings.limb_reach_error_ratio,
        ),
        "quaternion_sign_flip": _MetricDefinition(
            "boolean",
            "quaternion_sign_flip",
            0.5,
            math.inf,
        ),
        "quaternion_unit_error": _MetricDefinition(
            "absolute norm error",
            "quaternion_not_unit",
            settings.quaternion_unit_warning,
            settings.quaternion_unit_error,
        ),
        "root_rotation_discontinuity": _MetricDefinition(
            "deg/frame",
            "root_rotation_discontinuity",
            settings.root_rotation_warning_deg_per_frame,
            settings.root_rotation_error_deg_per_frame,
        ),
        "root_translation_discontinuity": _MetricDefinition(
            "m/frame",
            "root_translation_discontinuity",
            settings.root_translation_warning_m_per_frame,
            settings.root_translation_error_m_per_frame,
        ),
    }


def _observe_quaternion_quality(
    sequence: MotionSequence,
    observations: dict[str, list[_Observation]],
) -> None:
    previous: dict[str, QuaternionWXYZ] = {}
    for sample_index, frame in enumerate(sequence.frames):
        current: dict[str, QuaternionWXYZ] = {"Root": frame.root_rotation_wxyz}
        for bone in frame.bones:
            current[f"Bone:{bone.name}:local"] = bone.local_rotation_wxyz
            current[f"Bone:{bone.name}:global"] = bone.global_rotation_wxyz
        for effector in frame.effectors:
            if effector.rotation_wxyz is not None:
                current[f"Effector:{effector.name}"] = effector.rotation_wxyz

        for subject in sorted(current):
            quaternion = current[subject]
            observations["quaternion_unit_error"].append(
                _Observation(
                    abs(_quaternion_norm(quaternion) - 1.0),
                    frame.frame,
                    sample_index,
                    subject,
                )
            )
            prior = previous.get(subject)
            if prior is not None:
                observations["quaternion_sign_flip"].append(
                    _Observation(
                        1.0 if _quaternion_dot(prior, quaternion) < 0.0 else 0.0,
                        frame.frame,
                        sample_index,
                        subject,
                    )
                )
        previous = current


def _observe_bone_dynamics(
    sequence: MotionSequence,
    observations: dict[str, list[_Observation]],
) -> None:
    speeds: dict[str, list[_Observation]] = {}
    previous = sequence.frames[0]
    previous_bones = {bone.name: bone for bone in previous.bones}
    for sample_index, frame in enumerate(sequence.frames[1:], start=1):
        delta_time = frame.time_seconds - previous.time_seconds
        current_bones = {bone.name: bone for bone in frame.bones}
        for name in sorted(current_bones):
            angle = math.degrees(
                _quaternion_angle(
                    previous_bones[name].local_rotation_wxyz,
                    current_bones[name].local_rotation_wxyz,
                )
            )
            observation = _Observation(angle / delta_time, frame.frame, sample_index, name)
            observations["bone_angular_speed"].append(observation)
            speeds.setdefault(name, []).append(observation)
        previous = frame
        previous_bones = current_bones

    frames_by_index = sequence.frames
    for name in sorted(speeds):
        bone_speeds = speeds[name]
        for left, right in zip(bone_speeds, bone_speeds[1:], strict=False):
            delta_time = (
                frames_by_index[right.sample_index].time_seconds
                - frames_by_index[left.sample_index].time_seconds
            )
            observations["bone_angular_acceleration"].append(
                _Observation(
                    abs(right.value - left.value) / delta_time,
                    right.frame,
                    right.sample_index,
                    name,
                )
            )


def _observe_root_discontinuities(
    sequence: MotionSequence,
    observations: dict[str, list[_Observation]],
) -> None:
    for sample_index, (left, right) in enumerate(
        zip(sequence.frames, sequence.frames[1:], strict=False),
        start=1,
    ):
        delta_frame = right.frame - left.frame
        observations["root_translation_discontinuity"].append(
            _Observation(
                _distance(left.root_location, right.root_location) / delta_frame,
                right.frame,
                sample_index,
                "Root",
            )
        )
        observations["root_rotation_discontinuity"].append(
            _Observation(
                math.degrees(
                    _quaternion_angle(left.root_rotation_wxyz, right.root_rotation_wxyz)
                )
                / delta_frame,
                right.frame,
                sample_index,
                "Root",
            )
        )


def _observe_contacts(
    sequence: MotionSequence,
    settings: MotionValidationSettings,
    observations: dict[str, list[_Observation]],
) -> None:
    positions = [_effective_effector_positions(frame) for frame in sequence.frames]
    explicit_names = {
        contact.effector for frame in sequence.frames for contact in frame.contacts
    }
    contact_states: dict[str, list[tuple[bool, float]]] = {}

    for name in sorted(explicit_names):
        values: list[tuple[bool, float]] = []
        for frame in sequence.frames:
            by_name = {contact.effector: contact for contact in frame.contacts}
            contact = by_name.get(name)
            values.append(
                (False, settings.contact_ground_height)
                if contact is None
                else (contact.active, contact.ground_height)
            )
        contact_states[name] = values

    if settings.auto_detect_contacts:
        for name in settings.contact_effectors:
            if name in explicit_names or not all(name in values for values in positions):
                continue
            active = _auto_contact_states(sequence, positions, name, settings)
            contact_states[name] = [
                (value, settings.contact_ground_height) for value in active
            ]

    for name in sorted(contact_states):
        anchor: Vec3 | None = None
        states = contact_states[name]
        for sample_index, ((active, ground_height), frame_positions, frame) in enumerate(
            zip(states, positions, sequence.frames, strict=True)
        ):
            position = frame_positions.get(name)
            if not active or position is None:
                anchor = None
                continue
            if anchor is None:
                anchor = position
            slide = math.hypot(position[0] - anchor[0], position[1] - anchor[1])
            penetration = max(0.0, ground_height - position[2])
            observations["foot_slide"].append(
                _Observation(slide, frame.frame, sample_index, name)
            )
            observations["ground_penetration"].append(
                _Observation(penetration, frame.frame, sample_index, name)
            )


def _auto_contact_states(
    sequence: MotionSequence,
    positions: list[dict[str, Vec3]],
    name: str,
    settings: MotionValidationSettings,
) -> list[bool]:
    count = len(sequence.frames)
    speed = [0.0] * count
    for index in range(count):
        adjacent: list[float] = []
        if index > 0:
            delta = sequence.frames[index].time_seconds - sequence.frames[index - 1].time_seconds
            adjacent.append(_distance(positions[index - 1][name], positions[index][name]) / delta)
        if index + 1 < count:
            delta = sequence.frames[index + 1].time_seconds - sequence.frames[index].time_seconds
            adjacent.append(_distance(positions[index][name], positions[index + 1][name]) / delta)
        speed[index] = max(adjacent, default=0.0)

    candidates = [
        position[name][2] - settings.contact_ground_height <= settings.contact_max_height
        and speed[index] <= settings.contact_max_speed_m_s
        for index, position in enumerate(positions)
    ]
    result = [False] * count
    run_start: int | None = None
    for index in range(count + 1):
        candidate = index < count and candidates[index]
        if candidate and run_start is None:
            run_start = index
        elif not candidate and run_start is not None:
            if index - run_start >= settings.contact_min_samples:
                result[run_start:index] = [True] * (index - run_start)
            run_start = None
    return result


def _observe_limb_reach(
    sequence: MotionSequence,
    settings: MotionValidationSettings,
    observations: dict[str, list[_Observation]],
) -> None:
    first_bones = {bone.name: bone for bone in sequence.frames[0].bones}
    first_positions = _effective_effector_positions(sequence.frames[0])
    usable: list[tuple[LimbDefinition, float]] = []
    for limb in settings.limbs:
        if not all(name in first_bones for name in limb.bones):
            continue
        if limb.effector not in first_positions:
            continue
        rest_length = limb.rest_length
        if rest_length is None:
            rest_length = sum(
                _distance(first_bones[name].head_world, first_bones[name].tail_world)
                for name in limb.bones
            )
        if rest_length <= 1.0e-12:
            continue
        usable.append((limb, rest_length))

    for sample_index, frame in enumerate(sequence.frames):
        bones = {bone.name: bone for bone in frame.bones}
        positions = _effective_effector_positions(frame)
        for limb, rest_length in usable:
            reach = _distance(bones[limb.bones[0]].head_world, positions[limb.effector])
            observations["limb_reach"].append(
                _Observation(reach / rest_length, frame.frame, sample_index, limb.name)
            )


def _observe_key_density(
    sequence: MotionSequence,
    observations: dict[str, list[_Observation]],
) -> None:
    if sequence.keyed_channel_count is not None:
        denominator = float(sequence.keyed_channel_count)
        for sample_index, frame in enumerate(sequence.frames):
            assert frame.key_count is not None
            observations["key_density"].append(
                _Observation(
                    frame.key_count * sequence.fps / denominator,
                    frame.frame,
                    sample_index,
                    "Action",
                )
            )
        return

    metadata = dict(sequence.metadata)
    density = metadata.get("key_density")
    if density is None:
        return
    density_value = _finite_float(density, "metadata key_density")
    if density_value < 0.0:
        raise ValueError("metadata key_density must be non-negative")
    observations["key_density"].append(
        _Observation(density_value, sequence.start_frame, 0, "Action")
    )


def _metric_summary(
    name: str,
    unit: str,
    observations: list[_Observation],
    exceedance_count: int,
) -> MetricSummary:
    if not observations:
        return MetricSummary(name, unit, None, None, 0, 0, None)
    values = sorted(observation.value for observation in observations)
    p95_index = max(0, math.ceil(len(values) * 0.95) - 1)
    frames = [observation.frame for observation in observations]
    return MetricSummary(
        name=name,
        unit=unit,
        maximum=values[-1],
        p95=values[p95_index],
        count=len(values),
        exceedance_count=exceedance_count,
        frame_range=FrameInterval(min(frames), max(frames), len(set(frames))),
    )


def _issue_intervals(
    *,
    metric: str,
    code: str,
    observations: list[tuple[_Observation, IssueSeverity, float]],
    maximum_count: int,
) -> list[MotionIssue]:
    grouped: dict[tuple[str, IssueSeverity, float], list[_Observation]] = {}
    for observation, severity, threshold in observations:
        grouped.setdefault((observation.subject, severity, threshold), []).append(observation)

    result: list[MotionIssue] = []
    for (subject, severity, threshold), values in sorted(
        grouped.items(),
        key=lambda item: (item[0][0], item[0][1].value, item[0][2]),
    ):
        values.sort(key=lambda value: (value.sample_index, value.frame))
        run: list[_Observation] = []
        for value in values:
            if run and value.sample_index != run[-1].sample_index + 1:
                _require_issue_capacity(result, maximum_count)
                result.append(_make_issue(code, metric, severity, subject, threshold, run))
                run = []
            run.append(value)
        if run:
            _require_issue_capacity(result, maximum_count)
            result.append(_make_issue(code, metric, severity, subject, threshold, run))
    return result


def _require_issue_capacity(
    issues: list[MotionIssue],
    maximum_count: int,
) -> None:
    if len(issues) >= maximum_count:
        raise ValueError(
            "motion quality issue interval limit exceeded "
            f"(maximum {MAX_MOTION_QUALITY_ISSUES})"
        )


def _make_issue(
    code: str,
    metric: str,
    severity: IssueSeverity,
    subject: str,
    threshold: float,
    observations: list[_Observation],
) -> MotionIssue:
    return MotionIssue(
        code=code,
        metric=metric,
        severity=severity,
        subject=subject,
        interval=FrameInterval(
            observations[0].frame,
            observations[-1].frame,
            len(observations),
        ),
        peak=max(value.value for value in observations),
        threshold=threshold,
    )


def _severity(value: float, warning: float, error: float) -> IssueSeverity | None:
    if value > error:
        return IssueSeverity.FIX
    if value > warning:
        return IssueSeverity.REVIEW
    return None


def _effective_effector_positions(frame: MotionFrame) -> dict[str, Vec3]:
    result = {bone.name: bone.tail_world for bone in frame.bones}
    result.update({effector.name: effector.position_world for effector in frame.effectors})
    return result


def _validate_coordinate_limits(frame: MotionFrame, maximum: float) -> None:
    vectors: list[tuple[str, Vec3]] = [
        ("root_location", frame.root_location),
        ("root_scale", frame.root_scale),
    ]
    for bone in frame.bones:
        vectors.extend(
            (
                (f"{bone.name}.head_world", bone.head_world),
                (f"{bone.name}.tail_world", bone.tail_world),
            )
        )
    vectors.extend(
        (f"{effector.name}.position_world", effector.position_world)
        for effector in frame.effectors
    )
    for label, vector in vectors:
        if any(abs(value) > maximum for value in vector):
            raise ValueError(f"frame {frame.frame:g} {label} exceeds max_abs_coordinate")
    if any(abs(value) <= 1.0e-12 for value in frame.root_scale):
        raise ValueError(f"frame {frame.frame:g} root_scale components must be non-zero")


def _validate_hierarchy(parents: dict[str, str | None]) -> None:
    for name, parent in parents.items():
        if parent is not None and parent not in parents:
            raise ValueError(f"bone {name!r} references missing parent {parent!r}")
        seen = {name}
        cursor = parent
        while cursor is not None:
            if cursor in seen:
                raise ValueError(f"bone hierarchy contains a cycle at {cursor!r}")
            seen.add(cursor)
            cursor = parents[cursor]


def _metadata(value: Metadata | Mapping[str, JSONScalar]) -> Metadata:
    items: Iterable[tuple[str, JSONScalar]]
    if isinstance(value, Mapping):
        items = value.items()
    else:
        try:
            items = tuple(value)
        except TypeError as error:
            raise TypeError("metadata must be a mapping or key/value iterable") from error
    result: list[tuple[str, JSONScalar]] = []
    for item in items:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError("metadata entries must be (key, scalar) tuples")
        key, scalar = item
        if not isinstance(key, str) or not key:
            raise ValueError("metadata keys must be non-empty strings")
        if scalar is None or type(scalar) is bool or isinstance(scalar, str):
            pass
        elif type(scalar) is int:
            if abs(scalar) > (1 << 53) - 1:
                raise ValueError(f"metadata {key!r} integer is outside the JSON safe range")
        elif type(scalar) is float:
            if not math.isfinite(scalar):
                raise ValueError(f"metadata {key!r} must be finite")
        else:
            raise TypeError(f"metadata {key!r} must be a JSON scalar")
        result.append((key, scalar))
    result.sort(key=lambda item: item[0])
    if len({key for key, _value in result}) != len(result):
        raise ValueError("metadata keys must be unique")
    return tuple(result)


def _typed_sorted_tuple(
    values: Iterable[Any],
    expected_type: type,
    label: str,
    key: Any,
) -> tuple[Any, ...]:
    try:
        result = tuple(values)
    except TypeError as error:
        raise TypeError(f"{label} must be iterable") from error
    if not all(isinstance(value, expected_type) for value in result):
        raise TypeError(f"{label} contains an invalid value")
    result = tuple(sorted(result, key=key))
    identities = tuple(key(value) for value in result)
    if len(set(identities)) != len(identities):
        raise ValueError(f"{label} contains duplicate names")
    return result


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if not value or len(value) > MAX_NAME_LENGTH or "\x00" in value:
        raise ValueError(f"{label} must contain 1 to {MAX_NAME_LENGTH} non-NUL characters")
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


def _vector(value: Any, size: int, label: str) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{label} must be a numeric vector")
    try:
        values = tuple(value)
    except TypeError as error:
        raise TypeError(f"{label} must be a numeric vector") from error
    if len(values) != size:
        raise ValueError(f"{label} must contain exactly {size} values")
    return tuple(_finite_float(component, f"{label}[{index}]") for index, component in enumerate(values))


def _vec3(value: Any, label: str) -> Vec3:
    checked = _vector(value, 3, label)
    return (checked[0], checked[1], checked[2])


def _quaternion(value: Any, label: str) -> QuaternionWXYZ:
    checked = _vector(value, 4, label)
    result = (checked[0], checked[1], checked[2], checked[3])
    norm = _quaternion_norm(result)
    if not math.isfinite(norm):
        raise ValueError(f"{label} norm must be finite")
    if norm <= _MIN_QUATERNION_NORM:
        raise ValueError(f"{label} must have a non-zero norm")
    return result


def _positive_integer(value: Any, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")
    if value <= 0:
        raise ValueError(f"{label} must be positive")
    if value > _MAX_SAFE_INTEGER:
        raise ValueError(f"{label} exceeds the JSON safe integer range")
    return value


def _nonnegative_integer(value: Any, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")
    if value < 0:
        raise ValueError(f"{label} must be non-negative")
    if value > _MAX_SAFE_INTEGER:
        raise ValueError(f"{label} exceeds the JSON safe integer range")
    return value


def _quaternion_norm(value: QuaternionWXYZ) -> float:
    return math.hypot(*value)


def _normalized_quaternion(value: QuaternionWXYZ) -> QuaternionWXYZ:
    norm = _quaternion_norm(value)
    return tuple(component / norm for component in value)  # type: ignore[return-value]


def _quaternion_dot(left: QuaternionWXYZ, right: QuaternionWXYZ) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _quaternion_angle(left: QuaternionWXYZ, right: QuaternionWXYZ) -> float:
    dot = abs(_quaternion_dot(_normalized_quaternion(left), _normalized_quaternion(right)))
    # Normalizing identical binary64 quaternions can leave dot one ulp below
    # one.  Treat that numerical residue as zero motion instead of inventing a
    # small angular-speed spike.
    if dot >= 1.0 - 1.0e-12:
        return 0.0
    return 2.0 * math.acos(max(-1.0, min(1.0, dot)))


def _distance(left: Vec3, right: Vec3) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right, strict=True)))


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


DEFAULT_LIMBS: Final[tuple[LimbDefinition, ...]] = (
    LimbDefinition("left_arm", ("LeftUpperArm", "LeftLowerArm", "LeftHand"), "LeftHand"),
    LimbDefinition("left_leg", ("LeftUpperLeg", "LeftLowerLeg", "LeftFoot"), "LeftFoot"),
    LimbDefinition("right_arm", ("RightUpperArm", "RightLowerArm", "RightHand"), "RightHand"),
    LimbDefinition("right_leg", ("RightUpperLeg", "RightLowerLeg", "RightFoot"), "RightFoot"),
)


__all__ = (
    "BoneSample",
    "ContactSample",
    "DEFAULT_LIMBS",
    "EffectorSample",
    "FrameInterval",
    "IssueSeverity",
    "LimbDefinition",
    "MAX_ABS_COORDINATE_LIMIT",
    "MAX_FPS",
    "MAX_MOTION_QUALITY_ISSUES",
    "MIN_FPS",
    "MOTION_QUALITY_REPORT_SCHEMA",
    "MOTION_SEQUENCE_SCHEMA",
    "MOTION_VALIDATION_SETTINGS_SCHEMA",
    "Metadata",
    "MetricSummary",
    "MotionFrame",
    "MotionIssue",
    "MotionQualityReport",
    "MotionSequence",
    "MotionValidationSettings",
    "QualitySummary",
    "QuaternionWXYZ",
    "Vec3",
    "analyze_motion",
    "validate_motion_sequence",
)
