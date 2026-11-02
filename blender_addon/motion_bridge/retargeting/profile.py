"""Pure helpers for the persistent manual Humanoid mapping profile."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Mapping
import uuid

from ..canonical.rig_spec import JOINTS


@dataclass(frozen=True, slots=True)
class HumanoidMappingField:
    human_bone: str
    property_name: str
    required: bool


def _property_name(human_bone: str) -> str:
    words = re.sub(r"(?<!^)(?=[A-Z])", "_", human_bone).lower()
    return f"bone_{words}"


HUMANOID_MAPPING_FIELDS = tuple(
    HumanoidMappingField(
        human_bone=joint.human_bone,
        property_name=_property_name(joint.human_bone),
        required=joint.required,
    )
    for joint in JOINTS
    if joint.human_bone is not None
)

HUMAN_BONES = tuple(field.human_bone for field in HUMANOID_MAPPING_FIELDS)

if len(HUMANOID_MAPPING_FIELDS) != 22:
    raise RuntimeError("The manual Humanoid mapping profile must contain 22 bones")


SOURCE_MAPPING_PROFILE_SCHEMA_VERSION = 1


class SourceMappingProfileError(ValueError):
    """Raised when a persisted manual mapping profile is malformed or unsafe."""


class StaleSourceMappingProfileError(SourceMappingProfileError):
    """Raised when a mapping profile belongs to a different Source Take."""


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SourceMappingProfileError(
            f"Source mapping profile {label} must be a non-empty string"
        )
    return value


def _canonical_uuid(value: object, label: str) -> str:
    text = _required_string(value, label)
    try:
        parsed = uuid.UUID(text)
    except (AttributeError, TypeError, ValueError) as error:
        raise SourceMappingProfileError(
            f"Source mapping profile {label} must be a UUID"
        ) from error
    if str(parsed) != text:
        raise SourceMappingProfileError(
            f"Source mapping profile {label} must be a canonical UUID"
        )
    return text


def _sha256(value: object, label: str) -> str:
    text = _required_string(value, label)
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise SourceMappingProfileError(
            f"Source mapping profile {label} must be a lowercase SHA-256"
        )
    return text


def _mapping_items(mapping: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(mapping, Mapping):
        raise SourceMappingProfileError(
            "Source mapping profile mapping must be a JSON object"
        )
    known_human_bones = set(HUMAN_BONES)
    unknown = sorted(
        str(human_bone) for human_bone in mapping if human_bone not in known_human_bones
    )
    if unknown:
        raise SourceMappingProfileError(
            "Source mapping profile contains unknown Human bones: " + ", ".join(unknown)
        )

    normalized: dict[str, str] = {}
    for human_bone, source_name in mapping.items():
        if not isinstance(human_bone, str):
            raise SourceMappingProfileError(
                "Source mapping profile Human bone names must be strings"
            )
        if not isinstance(source_name, str):
            raise SourceMappingProfileError(
                f"Source bone for {human_bone} must be a string"
            )
        # Incomplete profiles are valid while the user is filling them in.
        # Blank values use the same sparse representation as the legacy helper.
        if source_name:
            normalized[human_bone] = source_name
    return tuple(sorted(normalized.items()))


@dataclass(frozen=True, slots=True)
class SourceMappingProfile:
    """A manual mapping bound to one imported Source Take instance.

    Action names are retained because they are useful to users, while the UUID
    and fingerprints make the actual safety decision. An empty Action Slot id
    is valid for Blender Actions created without slots.
    """

    source_instance_id: str
    source_archive_id: str
    source_hierarchy_sha256: str
    source_bind_pose_sha256: str
    source_action_name: str
    source_action_slot_identifier: str
    mapping_items: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _canonical_uuid(self.source_instance_id, "source instance id")
        _sha256(self.source_archive_id, "source archive id")
        _sha256(self.source_hierarchy_sha256, "source hierarchy hash")
        _sha256(self.source_bind_pose_sha256, "source bind-pose hash")
        _required_string(self.source_action_name, "source Action name")
        if not isinstance(self.source_action_slot_identifier, str):
            raise SourceMappingProfileError(
                "Source mapping profile source Action Slot id must be a string"
            )
        if not isinstance(self.mapping_items, tuple):
            raise SourceMappingProfileError(
                "Source mapping profile mapping items must be a tuple"
            )
        try:
            raw_mapping = dict(self.mapping_items)
        except (TypeError, ValueError) as error:
            raise SourceMappingProfileError(
                "Source mapping profile mapping items are malformed"
            ) from error
        normalized = _mapping_items(raw_mapping)
        if normalized != self.mapping_items:
            raise SourceMappingProfileError(
                "Source mapping profile mapping must be sorted, sparse, and unique"
            )

    @property
    def mapping(self) -> dict[str, str]:
        return dict(self.mapping_items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mapping": self.mapping,
            "schemaVersion": SOURCE_MAPPING_PROFILE_SCHEMA_VERSION,
            "source": {
                "actionName": self.source_action_name,
                "actionSlotIdentifier": self.source_action_slot_identifier,
                "archiveId": self.source_archive_id,
                "bindPoseSha256": self.source_bind_pose_sha256,
                "hierarchySha256": self.source_hierarchy_sha256,
                "instanceId": self.source_instance_id,
            },
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
    def from_json(cls, value: object) -> SourceMappingProfile:
        if not isinstance(value, str) or not value:
            raise SourceMappingProfileError("Manual mapping profile is missing")
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as error:
            raise SourceMappingProfileError(
                "Manual mapping profile is not valid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise SourceMappingProfileError(
                "Manual mapping profile must be a JSON object"
            )
        schema_version = payload.get("schemaVersion")
        if (
            type(schema_version) is not int
            or schema_version != SOURCE_MAPPING_PROFILE_SCHEMA_VERSION
        ):
            raise SourceMappingProfileError(
                "Manual mapping profile schema is unsupported"
            )
        source = payload.get("source")
        if not isinstance(source, dict):
            raise SourceMappingProfileError(
                "Manual mapping profile source identity is missing"
            )
        return cls.create(
            mapping=payload.get("mapping"),
            source_instance_id=_canonical_uuid(
                source.get("instanceId"), "source instance id"
            ),
            source_archive_id=_sha256(source.get("archiveId"), "source archive id"),
            source_hierarchy_sha256=_sha256(
                source.get("hierarchySha256"), "source hierarchy hash"
            ),
            source_bind_pose_sha256=_sha256(
                source.get("bindPoseSha256"), "source bind-pose hash"
            ),
            source_action_name=_required_string(
                source.get("actionName"), "source Action name"
            ),
            source_action_slot_identifier=source.get("actionSlotIdentifier"),
        )

    @classmethod
    def create(
        cls,
        *,
        mapping: Mapping[str, str],
        source_instance_id: str,
        source_archive_id: str,
        source_hierarchy_sha256: str,
        source_bind_pose_sha256: str,
        source_action_name: str,
        source_action_slot_identifier: str,
    ) -> SourceMappingProfile:
        return cls(
            source_instance_id=source_instance_id,
            source_archive_id=source_archive_id,
            source_hierarchy_sha256=source_hierarchy_sha256,
            source_bind_pose_sha256=source_bind_pose_sha256,
            source_action_name=source_action_name,
            source_action_slot_identifier=source_action_slot_identifier,
            mapping_items=_mapping_items(mapping),
        )

    def matches_source(
        self,
        *,
        source_instance_id: str,
        source_archive_id: str,
        source_hierarchy_sha256: str,
        source_bind_pose_sha256: str,
        source_action_name: str,
        source_action_slot_identifier: str,
    ) -> bool:
        """Return whether every persisted Source Take safety field matches."""

        current_identity = self._validated_source_identity(
            source_instance_id=source_instance_id,
            source_archive_id=source_archive_id,
            source_hierarchy_sha256=source_hierarchy_sha256,
            source_bind_pose_sha256=source_bind_pose_sha256,
            source_action_name=source_action_name,
            source_action_slot_identifier=source_action_slot_identifier,
        )
        return current_identity == self._source_identity()

    def require_source(
        self,
        *,
        source_instance_id: str,
        source_archive_id: str,
        source_hierarchy_sha256: str,
        source_bind_pose_sha256: str,
        source_action_name: str,
        source_action_slot_identifier: str,
    ) -> None:
        """Reject use after a Source Take was replaced or mutated."""

        if not self.matches_source(
            source_instance_id=source_instance_id,
            source_archive_id=source_archive_id,
            source_hierarchy_sha256=source_hierarchy_sha256,
            source_bind_pose_sha256=source_bind_pose_sha256,
            source_action_name=source_action_name,
            source_action_slot_identifier=source_action_slot_identifier,
        ):
            raise StaleSourceMappingProfileError(
                "Manual mapping profile is stale for the current Source Take; "
                "refresh it from the current automatic mapping before use"
            )

    def _source_identity(self) -> tuple[str, str, str, str, str]:
        return (
            self.source_instance_id,
            self.source_archive_id,
            self.source_hierarchy_sha256,
            self.source_bind_pose_sha256,
            self.source_action_slot_identifier,
        )

    @staticmethod
    def _validated_source_identity(
        *,
        source_instance_id: str,
        source_archive_id: str,
        source_hierarchy_sha256: str,
        source_bind_pose_sha256: str,
        source_action_name: str,
        source_action_slot_identifier: str,
    ) -> tuple[str, str, str, str, str]:
        instance_id = _canonical_uuid(source_instance_id, "source instance id")
        archive_id = _sha256(source_archive_id, "source archive id")
        hierarchy = _sha256(source_hierarchy_sha256, "source hierarchy hash")
        bind_pose = _sha256(source_bind_pose_sha256, "source bind-pose hash")
        # Blender may suffix an Action datablock name while making a Full Copy
        # Scene. Keep it validated and serialized as a display hint, but use the
        # UUID, content fingerprints, and slot identifier for the safety match.
        _required_string(source_action_name, "source Action name")
        if not isinstance(source_action_slot_identifier, str):
            raise SourceMappingProfileError(
                "Source mapping profile source Action Slot id must be a string"
            )
        return (
            instance_id,
            archive_id,
            hierarchy,
            bind_pose,
            source_action_slot_identifier,
        )


def profile_values_from_mapping(mapping: Mapping[str, str]) -> dict[str, str]:
    """Return all 22 RNA property values for a HumanBone mapping."""

    if not isinstance(mapping, Mapping):
        raise TypeError("mapping must be a HumanBone-to-source-name mapping")
    known_human_bones = set(HUMAN_BONES)
    unknown = sorted(set(mapping) - known_human_bones)
    if unknown:
        raise ValueError(f"Unknown Human bone mapping keys: {', '.join(unknown)}")

    values: dict[str, str] = {}
    for field in HUMANOID_MAPPING_FIELDS:
        source_name = mapping.get(field.human_bone, "")
        if not isinstance(source_name, str):
            raise ValueError(f"Source bone for {field.human_bone} must be a string")
        values[field.property_name] = source_name
    return values


def mapping_from_profile_values(values: Mapping[str, str]) -> dict[str, str]:
    """Convert the persistent profile to a sparse explicit bake mapping.

    Blank optional fields are omitted. Blank required fields are also omitted so
    the shared ``validate_humanoid_mapping`` function remains the single source
    of truth for required-bone, duplicate, and hierarchy validation.
    """

    if not isinstance(values, Mapping):
        raise TypeError("values must be a property-name-to-source-name mapping")
    expected = {field.property_name for field in HUMANOID_MAPPING_FIELDS}
    missing = sorted(expected - set(values))
    if missing:
        raise ValueError(f"Manual mapping profile is incomplete: {', '.join(missing)}")
    unknown = sorted(set(values) - expected)
    if unknown:
        raise ValueError(f"Unknown manual mapping properties: {', '.join(unknown)}")

    mapping: dict[str, str] = {}
    for field in HUMANOID_MAPPING_FIELDS:
        source_name = values[field.property_name]
        if not isinstance(source_name, str):
            raise ValueError(f"Source bone for {field.human_bone} must be a string")
        if source_name:
            mapping[field.human_bone] = source_name
    return mapping
