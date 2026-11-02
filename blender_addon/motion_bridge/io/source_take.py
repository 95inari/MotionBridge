"""Pure Source Take validation and provenance helpers.

This module deliberately has no Blender dependency.  It is used by the
Blender import transaction, but can also validate paths and provenance in the
system-Python test suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Iterable


SOURCE_PROVENANCE_SCHEMA_VERSION = 2
SOURCE_PROVENANCE_SCHEMA_PROPERTY = "motion_bridge_source_schema_version"
SOURCE_ARCHIVE_KIND = "SOURCE_ARCHIVE"
SOURCE_INSTANCE_ID_PROPERTY = "motion_bridge_source_instance_id"
SOURCE_TAKE_ID_PROPERTY = "motion_bridge_source_take_id"
SOURCE_ARCHIVE_ACTION_PROPERTY = "motion_bridge_source_archive_action"
SOURCE_ACTION_SLOT_PROPERTY = "motion_bridge_source_action_slot"
SOURCE_BIND_POSE_PROPERTY = "motion_bridge_source_bind_pose_sha256"
SOURCE_ARCHIVE_CHECKSUM_PROPERTY = "motion_bridge_source_archive_checksum"
SOURCE_ARCHIVE_CHECKSUM_ALGORITHM_PROPERTY = (
    "motion_bridge_source_archive_checksum_algorithm"
)
SOURCE_ARCHIVE_CHECKSUM_ALGORITHM = "sha256-action-v1"


class SourceTakeValidationError(ValueError):
    """Raised before Blender is allowed to mutate the current file."""


class SourceFileFormat(StrEnum):
    FBX = "fbx"
    GLTF = "gltf"
    GLB = "glb"


@dataclass(frozen=True, slots=True)
class ValidatedSourcePath:
    path: Path
    file_format: SourceFileFormat


@dataclass(frozen=True, slots=True)
class SourceTakeProvenance:
    """Stable metadata attached to an imported armature and archive Action."""

    file_format: SourceFileFormat
    source_filepath: str
    file_sha256: str
    source_armature: str
    action_name: str
    frame_start: float
    frame_end: float
    bone_hierarchy_sha256: str
    take_id: str = ""

    def __post_init__(self) -> None:
        if not self.source_filepath or not Path(self.source_filepath).is_absolute():
            raise SourceTakeValidationError(
                "source_filepath must be a resolved absolute path"
            )
        if len(self.file_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.file_sha256
        ):
            raise SourceTakeValidationError("file_sha256 must be lowercase SHA-256")
        if len(self.bone_hierarchy_sha256) != 64 or any(
            character not in "0123456789abcdef"
            for character in self.bone_hierarchy_sha256
        ):
            raise SourceTakeValidationError(
                "bone_hierarchy_sha256 must be lowercase SHA-256"
            )
        if self.take_id and (
            len(self.take_id) != 64
            or any(
                character not in "0123456789abcdef" for character in self.take_id
            )
        ):
            raise SourceTakeValidationError("take_id must be lowercase SHA-256")
        if not self.source_armature:
            raise SourceTakeValidationError("source_armature must not be empty")
        if not self.action_name:
            raise SourceTakeValidationError("action_name must not be empty")
        if not math.isfinite(self.frame_start) or not math.isfinite(self.frame_end):
            raise SourceTakeValidationError("Action frame range must be finite")
        if self.frame_end < self.frame_start:
            raise SourceTakeValidationError("Action frame range is reversed")

    @property
    def archive_id(self) -> str:
        if self.take_id:
            # One immutable Source Archive represents exactly one selected take.
            return self.take_id
        # Blender may suffix imported datablock names (for example ``.001``)
        # when the scene already contains a name collision.  Names remain in
        # provenance for inspection, but are intentionally excluded from the
        # stable archive identity so importing identical bytes is repeatable.
        payload = json.dumps(
            {
                "boneHierarchySha256": self.bone_hierarchy_sha256,
                "fileFormat": self.file_format.value,
                "fileSha256": self.file_sha256,
                "frameEnd": self.frame_end,
                "frameStart": self.frame_start,
                "schemaVersion": SOURCE_PROVENANCE_SCHEMA_VERSION,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def custom_properties(self) -> dict[str, str | int | float | bool]:
        properties: dict[str, str | int | float | bool] = {
            SOURCE_PROVENANCE_SCHEMA_PROPERTY: SOURCE_PROVENANCE_SCHEMA_VERSION,
            "motion_bridge_source_format": self.file_format.value,
            "motion_bridge_source_filepath": self.source_filepath,
            "motion_bridge_source_file_sha256": self.file_sha256,
            "motion_bridge_source_armature": self.source_armature,
            "motion_bridge_source_action": self.action_name,
            "motion_bridge_source_frame_start": self.frame_start,
            "motion_bridge_source_frame_end": self.frame_end,
            "motion_bridge_source_bone_hierarchy_sha256": (self.bone_hierarchy_sha256),
            "motion_bridge_source_archive_id": self.archive_id,
        }
        if self.take_id:
            properties[SOURCE_TAKE_ID_PROPERTY] = self.take_id
        return properties


def validate_source_take_path(filepath: str | os.PathLike[str]) -> ValidatedSourcePath:
    """Resolve and validate a readable FBX/glTF Source Take path."""

    raw_path = Path(filepath).expanduser()
    extension = raw_path.suffix.lower().removeprefix(".")
    try:
        file_format = SourceFileFormat(extension)
    except ValueError as error:
        raise SourceTakeValidationError(
            "Source Take must use a .fbx, .gltf, or .glb extension"
        ) from error

    try:
        path = raw_path.resolve(strict=True)
    except OSError as error:
        raise SourceTakeValidationError(
            f"Source Take does not exist: {raw_path}"
        ) from error
    if not path.is_file():
        raise SourceTakeValidationError(f"Source Take is not a file: {path}")
    try:
        with path.open("rb") as source:
            source.read(1)
    except OSError as error:
        raise SourceTakeValidationError(
            f"Source Take is not readable: {path}"
        ) from error
    return ValidatedSourcePath(path, file_format)


def sha256_file(
    filepath: str | os.PathLike[str], *, chunk_size: int = 1024 * 1024
) -> str:
    """Hash file bytes with bounded memory use."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    with Path(filepath).open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def bone_hierarchy_fingerprint(
    bones: Iterable[tuple[str, str | None]],
) -> str:
    """Hash bone names and parent relationships independent of collection order."""

    relationships: dict[str, str | None] = {}
    for name, parent_name in bones:
        if not isinstance(name, str) or not name:
            raise SourceTakeValidationError("Bone names must be non-empty strings")
        if parent_name is not None and (
            not isinstance(parent_name, str) or not parent_name
        ):
            raise SourceTakeValidationError(
                "Parent bone names must be non-empty strings or None"
            )
        if name in relationships:
            raise SourceTakeValidationError(f"Duplicate bone name: {name}")
        relationships[name] = parent_name

    if not relationships:
        raise SourceTakeValidationError("An imported armature must contain bones")
    unknown_parents = sorted(
        {
            parent_name
            for parent_name in relationships.values()
            if parent_name is not None and parent_name not in relationships
        }
    )
    if unknown_parents:
        raise SourceTakeValidationError(
            f"Unknown parent bones: {', '.join(unknown_parents)}"
        )

    canonical = json.dumps(
        [
            {"name": name, "parent": relationships[name]}
            for name in sorted(relationships)
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def bone_bind_pose_fingerprint(
    bones: Iterable[
        tuple[
            str,
            str | None,
            Iterable[float],
            Iterable[float],
            Iterable[float],
        ]
    ],
) -> str:
    """Hash bone hierarchy, endpoints, and armature-space rest matrices.

    The hierarchy-only fingerprint remains part of the public Source Take
    provenance.  This stronger scene-instance fingerprint guards deterministic
    Base regeneration against a changed roll, length, or bind pose.
    """

    encoded: dict[str, dict[str, object]] = {}

    def finite_values(
        values: Iterable[float],
        *,
        count: int,
        label: str,
    ) -> list[float]:
        result = [float(value) for value in values]
        if len(result) != count or not all(math.isfinite(value) for value in result):
            raise SourceTakeValidationError(
                f"{label} must contain {count} finite values"
            )
        # Treat signed zero as the same rest-pose value.
        return [0.0 if value == 0.0 else value for value in result]

    for name, parent_name, head, tail, matrix in bones:
        if not isinstance(name, str) or not name:
            raise SourceTakeValidationError("Bone names must be non-empty strings")
        if parent_name is not None and (
            not isinstance(parent_name, str) or not parent_name
        ):
            raise SourceTakeValidationError(
                "Parent bone names must be non-empty strings or None"
            )
        if name in encoded:
            raise SourceTakeValidationError(f"Duplicate bone name: {name}")
        encoded[name] = {
            "head": finite_values(head, count=3, label=f"{name} head"),
            "matrix": finite_values(matrix, count=16, label=f"{name} matrix"),
            "parent": parent_name,
            "tail": finite_values(tail, count=3, label=f"{name} tail"),
        }

    if not encoded:
        raise SourceTakeValidationError("An imported armature must contain bones")
    unknown_parents = sorted(
        {
            value["parent"]
            for value in encoded.values()
            if value["parent"] is not None and value["parent"] not in encoded
        }
    )
    if unknown_parents:
        raise SourceTakeValidationError(
            f"Unknown parent bones: {', '.join(str(value) for value in unknown_parents)}"
        )

    canonical = json.dumps(
        [
            {"name": name, **encoded[name]}
            for name in sorted(encoded)
        ],
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
