"""Motion Bridge file import/export boundary.

Blender-backed symbols are resolved lazily so the pure provenance helpers can
be tested without importing :mod:`bpy`.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .source_take import (
    SOURCE_ARCHIVE_KIND,
    SOURCE_ARCHIVE_ACTION_PROPERTY,
    SOURCE_ARCHIVE_CHECKSUM_ALGORITHM,
    SOURCE_ARCHIVE_CHECKSUM_ALGORITHM_PROPERTY,
    SOURCE_ARCHIVE_CHECKSUM_PROPERTY,
    SOURCE_ACTION_SLOT_PROPERTY,
    SOURCE_BIND_POSE_PROPERTY,
    SOURCE_INSTANCE_ID_PROPERTY,
    SOURCE_PROVENANCE_SCHEMA_PROPERTY,
    SOURCE_PROVENANCE_SCHEMA_VERSION,
    SOURCE_TAKE_ID_PROPERTY,
    SourceFileFormat,
    SourceTakeProvenance,
    SourceTakeValidationError,
    ValidatedSourcePath,
    bone_bind_pose_fingerprint,
    bone_hierarchy_fingerprint,
    sha256_file,
    validate_source_take_path,
)


_LAZY_EXPORTS = {
    "MB_OT_ExportCanonicalActionFbx": (".fbx_export", "MB_OT_ExportCanonicalActionFbx"),
    "active_canonical_action": (".fbx_export", "active_canonical_action"),
    "export_active_canonical_action": (".fbx_export", "export_active_canonical_action"),
    "SourceTakeImport": (".source_import", "SourceTakeImport"),
    "SourceTakeCandidate": (".source_import", "SourceTakeCandidate"),
    "SourceTakeImportError": (".source_import", "SourceTakeImportError"),
    "SourceTakeSelectionRequired": (
        ".source_import",
        "SourceTakeSelectionRequired",
    ),
    "import_source_take": (".source_import", "import_source_take"),
}

__all__ = (
    "MB_OT_ExportCanonicalActionFbx",
    "SOURCE_ARCHIVE_KIND",
    "SOURCE_ARCHIVE_ACTION_PROPERTY",
    "SOURCE_ARCHIVE_CHECKSUM_ALGORITHM",
    "SOURCE_ARCHIVE_CHECKSUM_ALGORITHM_PROPERTY",
    "SOURCE_ARCHIVE_CHECKSUM_PROPERTY",
    "SOURCE_ACTION_SLOT_PROPERTY",
    "SOURCE_BIND_POSE_PROPERTY",
    "SOURCE_INSTANCE_ID_PROPERTY",
    "SOURCE_PROVENANCE_SCHEMA_PROPERTY",
    "SOURCE_PROVENANCE_SCHEMA_VERSION",
    "SOURCE_TAKE_ID_PROPERTY",
    "SourceFileFormat",
    "SourceTakeCandidate",
    "SourceTakeImport",
    "SourceTakeImportError",
    "SourceTakeSelectionRequired",
    "SourceTakeProvenance",
    "SourceTakeValidationError",
    "ValidatedSourcePath",
    "active_canonical_action",
    "bone_bind_pose_fingerprint",
    "bone_hierarchy_fingerprint",
    "export_active_canonical_action",
    "import_source_take",
    "sha256_file",
    "validate_source_take_path",
)


def __getattr__(name: str) -> Any:
    export = _LAZY_EXPORTS.get(name)
    if export is None:
        raise AttributeError(name)
    module_name, attribute_name = export
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
