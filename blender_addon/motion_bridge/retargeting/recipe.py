"""Pure persisted recipe for deterministic Source Archive retargeting."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Mapping
import uuid

from ..canonical.rig_spec import SKELETON_ID
from .profile import HUMAN_BONES, HUMANOID_MAPPING_FIELDS


RETARGET_RECIPE_SCHEMA_VERSION = 1
RETARGET_ALGORITHM_VERSION = "canonical-evaluated-v1"
PROP_RETARGET_RECIPE = "motion_bridge_retarget_recipe"


class RetargetRecipeError(ValueError):
    """Raised when saved regeneration metadata is missing or unsafe."""


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RetargetRecipeError(f"Retarget recipe {label} must be a non-empty string")
    return value


def _sha256(value: object, label: str) -> str:
    text = _required_string(value, label)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise RetargetRecipeError(
            f"Retarget recipe {label} must be a lowercase SHA-256"
        )
    return text


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RetargetRecipeError(f"Retarget recipe {label} must be an integer")
    return value


def _mapping_items(mapping: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(mapping, dict):
        raise RetargetRecipeError("Retarget recipe mapping must be a JSON object")
    known = set(HUMAN_BONES)
    unknown = sorted(set(mapping) - known)
    if unknown:
        raise RetargetRecipeError(
            f"Retarget recipe contains unknown Human bones: {', '.join(unknown)}"
        )
    normalized: dict[str, str] = {}
    for human_bone, source_name in mapping.items():
        if not isinstance(human_bone, str):
            raise RetargetRecipeError("Retarget recipe Human bone names must be strings")
        normalized[human_bone] = _required_string(
            source_name,
            f"mapping for {human_bone}",
        )
    missing = sorted(
        field.human_bone
        for field in HUMANOID_MAPPING_FIELDS
        if field.required and field.human_bone not in normalized
    )
    if missing:
        raise RetargetRecipeError(
            f"Retarget recipe is missing required Human bones: {', '.join(missing)}"
        )
    names = tuple(normalized.values())
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise RetargetRecipeError(
            f"Retarget recipe maps source bones more than once: {', '.join(duplicates)}"
        )
    return tuple(sorted(normalized.items()))


@dataclass(frozen=True, slots=True)
class RetargetRecipe:
    source_instance_id: str
    source_object_hint: str
    source_archive_action_hint: str
    source_archive_id: str
    source_archive_checksum: str
    source_slot_identifier: str
    source_file_format: str
    source_filepath: str
    source_file_sha256: str
    source_armature_name: str
    source_action_name: str
    source_hierarchy_sha256: str
    source_bind_pose_sha256: str
    canonical_skeleton_id: str
    canonical_bind_pose_sha256: str
    canonical_slot_identifier: str
    canonical_slot_display_name: str
    frame_start: int
    frame_end: int
    mapping_items: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        try:
            parsed_instance = uuid.UUID(self.source_instance_id)
        except (AttributeError, TypeError, ValueError) as error:
            raise RetargetRecipeError(
                "Retarget recipe source instance id must be a UUID"
            ) from error
        if str(parsed_instance) != self.source_instance_id:
            raise RetargetRecipeError(
                "Retarget recipe source instance id must be a canonical UUID"
            )
        for value, label in (
            (self.source_object_hint, "source object hint"),
            (self.source_archive_action_hint, "source Archive Action hint"),
            (self.source_armature_name, "source armature name"),
            (self.source_action_name, "source Action name"),
        ):
            _required_string(value, label)
        for value, label in (
            (self.source_archive_id, "source archive id"),
            (self.source_archive_checksum, "source Archive checksum"),
            (self.source_file_sha256, "source file hash"),
            (self.source_hierarchy_sha256, "source hierarchy hash"),
            (self.source_bind_pose_sha256, "source bind-pose hash"),
            (self.canonical_bind_pose_sha256, "Canonical bind-pose hash"),
        ):
            _sha256(value, label)
        if self.source_file_format not in {"fbx", "gltf", "glb"}:
            raise RetargetRecipeError("Retarget recipe source format is unsupported")
        source_filepath = _required_string(self.source_filepath, "source filepath")
        if not (
            PurePosixPath(source_filepath).is_absolute()
            or PureWindowsPath(source_filepath).is_absolute()
        ):
            raise RetargetRecipeError(
                "Retarget recipe source filepath must be absolute"
            )
        if self.canonical_skeleton_id != SKELETON_ID:
            raise RetargetRecipeError("Retarget recipe Canonical skeleton id is unsupported")
        if not isinstance(self.source_slot_identifier, str):
            raise RetargetRecipeError("Retarget recipe source slot id must be a string")
        if not isinstance(self.canonical_slot_identifier, str):
            raise RetargetRecipeError("Retarget recipe Canonical slot id must be a string")
        if not isinstance(self.canonical_slot_display_name, str):
            raise RetargetRecipeError(
                "Retarget recipe Canonical slot display name must be a string"
            )
        _integer(self.frame_start, "frame start")
        _integer(self.frame_end, "frame end")
        if self.frame_end < self.frame_start:
            raise RetargetRecipeError("Retarget recipe frame range is reversed")
        normalized = _mapping_items(dict(self.mapping_items))
        if normalized != self.mapping_items:
            raise RetargetRecipeError("Retarget recipe mapping must be sorted and unique")

    @property
    def mapping(self) -> dict[str, str]:
        return dict(self.mapping_items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithmVersion": RETARGET_ALGORITHM_VERSION,
            "canonical": {
                "bindPoseSha256": self.canonical_bind_pose_sha256,
                "skeletonId": self.canonical_skeleton_id,
                "slotDisplayName": self.canonical_slot_display_name,
                "slotIdentifier": self.canonical_slot_identifier,
            },
            "frameEnd": self.frame_end,
            "frameStart": self.frame_start,
            "mapping": self.mapping,
            "schemaVersion": RETARGET_RECIPE_SCHEMA_VERSION,
            "source": {
                "action": self.source_action_name,
                "archiveActionHint": self.source_archive_action_hint,
                "archiveChecksum": self.source_archive_checksum,
                "archiveId": self.source_archive_id,
                "armature": self.source_armature_name,
                "bindPoseSha256": self.source_bind_pose_sha256,
                "fileFormat": self.source_file_format,
                "fileSha256": self.source_file_sha256,
                "filepath": self.source_filepath,
                "hierarchySha256": self.source_hierarchy_sha256,
                "instanceId": self.source_instance_id,
                "objectHint": self.source_object_hint,
                "slotIdentifier": self.source_slot_identifier,
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
    def from_json(cls, value: object) -> RetargetRecipe:
        if not isinstance(value, str) or not value:
            raise RetargetRecipeError("Canonical Action has no retarget recipe")
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as error:
            raise RetargetRecipeError("Canonical retarget recipe is not valid JSON") from error
        if not isinstance(payload, dict):
            raise RetargetRecipeError("Canonical retarget recipe must be a JSON object")
        if payload.get("schemaVersion") != RETARGET_RECIPE_SCHEMA_VERSION:
            raise RetargetRecipeError("Canonical retarget recipe schema is unsupported")
        if payload.get("algorithmVersion") != RETARGET_ALGORITHM_VERSION:
            raise RetargetRecipeError("Canonical retarget algorithm is unsupported")
        source = payload.get("source")
        canonical = payload.get("canonical")
        if not isinstance(source, dict) or not isinstance(canonical, dict):
            raise RetargetRecipeError(
                "Canonical retarget recipe source/canonical sections are missing"
            )
        frame_start = _integer(payload.get("frameStart"), "frame start")
        frame_end = _integer(payload.get("frameEnd"), "frame end")
        return cls(
            source_instance_id=_required_string(source.get("instanceId"), "source instance id"),
            source_object_hint=_required_string(source.get("objectHint"), "source object hint"),
            source_archive_action_hint=_required_string(
                source.get("archiveActionHint"),
                "source Archive Action hint",
            ),
            source_archive_id=_sha256(source.get("archiveId"), "source archive id"),
            source_archive_checksum=_sha256(
                source.get("archiveChecksum"),
                "source Archive checksum",
            ),
            source_slot_identifier=(
                source.get("slotIdentifier")
                if isinstance(source.get("slotIdentifier"), str)
                else ""
            ),
            source_file_format=_required_string(source.get("fileFormat"), "source format"),
            source_filepath=_required_string(source.get("filepath"), "source filepath"),
            source_file_sha256=_sha256(source.get("fileSha256"), "source file hash"),
            source_armature_name=_required_string(source.get("armature"), "source armature name"),
            source_action_name=_required_string(source.get("action"), "source Action name"),
            source_hierarchy_sha256=_sha256(
                source.get("hierarchySha256"),
                "source hierarchy hash",
            ),
            source_bind_pose_sha256=_sha256(
                source.get("bindPoseSha256"),
                "source bind-pose hash",
            ),
            canonical_skeleton_id=_required_string(
                canonical.get("skeletonId"),
                "Canonical skeleton id",
            ),
            canonical_bind_pose_sha256=_sha256(
                canonical.get("bindPoseSha256"),
                "Canonical bind-pose hash",
            ),
            canonical_slot_identifier=(
                canonical.get("slotIdentifier")
                if isinstance(canonical.get("slotIdentifier"), str)
                else ""
            ),
            canonical_slot_display_name=(
                canonical.get("slotDisplayName")
                if isinstance(canonical.get("slotDisplayName"), str)
                else ""
            ),
            frame_start=frame_start,
            frame_end=frame_end,
            mapping_items=_mapping_items(payload.get("mapping")),
        )

    @classmethod
    def create(
        cls,
        *,
        source_instance_id: str,
        source_object_hint: str,
        source_archive_action_hint: str,
        source_archive_id: str,
        source_archive_checksum: str,
        source_slot_identifier: str,
        source_file_format: str,
        source_filepath: str,
        source_file_sha256: str,
        source_armature_name: str,
        source_action_name: str,
        source_hierarchy_sha256: str,
        source_bind_pose_sha256: str,
        canonical_bind_pose_sha256: str,
        canonical_slot_identifier: str,
        canonical_slot_display_name: str,
        frame_start: int,
        frame_end: int,
        mapping: Mapping[str, str],
    ) -> RetargetRecipe:
        if not isinstance(mapping, Mapping):
            raise RetargetRecipeError("Retarget recipe mapping must be a mapping")
        return cls(
            source_instance_id=source_instance_id,
            source_object_hint=source_object_hint,
            source_archive_action_hint=source_archive_action_hint,
            source_archive_id=source_archive_id,
            source_archive_checksum=source_archive_checksum,
            source_slot_identifier=source_slot_identifier,
            source_file_format=source_file_format,
            source_filepath=source_filepath,
            source_file_sha256=source_file_sha256,
            source_armature_name=source_armature_name,
            source_action_name=source_action_name,
            source_hierarchy_sha256=source_hierarchy_sha256,
            source_bind_pose_sha256=source_bind_pose_sha256,
            canonical_skeleton_id=SKELETON_ID,
            canonical_bind_pose_sha256=canonical_bind_pose_sha256,
            canonical_slot_identifier=canonical_slot_identifier,
            canonical_slot_display_name=canonical_slot_display_name,
            frame_start=frame_start,
            frame_end=frame_end,
            mapping_items=_mapping_items(dict(mapping)),
        )


def finite_frame_range(recipe: RetargetRecipe) -> tuple[float, float]:
    """Small helper kept pure for callers comparing Blender RNA ranges."""

    values = (float(recipe.frame_start), float(recipe.frame_end))
    if not all(math.isfinite(value) for value in values):
        raise RetargetRecipeError("Retarget recipe frame range must be finite")
    return values
