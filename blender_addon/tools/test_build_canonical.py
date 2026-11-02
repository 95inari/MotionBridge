from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any
from unittest.mock import patch

import bpy
from io_scene_fbx import parse_fbx


TOOLS_ROOT = Path(__file__).resolve().parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

import build_canonical  # noqa: E402


def _native_file_properties(root: Any) -> list[Any]:
    result = []

    def visit(element: Any) -> None:
        if (
            element.id == b"P"
            and element.props
            and element.props[0] == b"Original|ApplicationNativeFile"
        ):
            result.append(element)
        for child in element.elems:
            visit(child)

    visit(root)
    return result


def main() -> None:
    repository = TOOLS_ROOT.parents[1]
    source = repository / "samples/canonical/mb_canonical_humanoid_v1.blend"
    descriptor = repository / "protocol/generated/canonical_humanoid_v1.json"
    preserved = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (source, descriptor)
    }
    normalize = build_canonical._normalize_fbx_source_metadata
    with tempfile.TemporaryDirectory(prefix="motion_bridge_canonical_metadata_") as temporary:
        private_folder = Path(temporary) / "private-author" / "日本語 project"
        private_folder.mkdir(parents=True)
        source_copy = private_folder / source.name
        shutil.copy2(source, source_copy)
        armature = build_canonical._load_source(source_copy)
        expected_parents = {
            bone.name: bone.parent.name if bone.parent else None
            for bone in armature.data.bones
        }

        def normalize_and_verify(output: Path) -> None:
            shutil.copy2(output, output.with_name("original.fbx"))
            expected, version = parse_fbx.parse(str(output))
            native_properties = _native_file_properties(expected)
            assert len(native_properties) == 1
            assert native_properties[0].props[-1] == str(source_copy).encode("utf-8")
            native_properties[0].props[-1] = source.name.encode("utf-8")
            normalize(output)
            actual, actual_version = parse_fbx.parse(str(output))
            assert version == actual_version
            assert expected == actual, "Normalization changed other FBX elements"

        output = Path(temporary) / "canonical.fbx"
        with patch.object(
            build_canonical,
            "_normalize_fbx_source_metadata",
            side_effect=normalize_and_verify,
        ):
            build_canonical.export_fbx(armature, output)
        assert str(private_folder).encode("utf-8") not in output.read_bytes()
        assert str(source_copy).encode("utf-8") not in output.read_bytes()
        assert bpy.data.filepath == str(source_copy)

        def reimport_signature(path: Path) -> tuple[object, ...]:
            build_canonical._clear_scene()
            result = bpy.ops.import_scene.fbx(filepath=str(path), use_custom_props=True)
            assert result == {"FINISHED"}
            imported = bpy.data.objects.get(build_canonical.ARMATURE_OBJECT_NAME)
            assert imported is not None
            assert expected_parents == {
                bone.name: bone.parent.name if bone.parent else None
                for bone in imported.data.bones
            }
            return (
                tuple(tuple(row) for row in imported.matrix_world),
                imported["motion_bridge_skeleton_id"],
                imported["motion_bridge_skeleton_version"],
                tuple(
                    (
                        bone.name,
                        tuple(tuple(row) for row in bone.matrix_local),
                        tuple(bone.head_local),
                        tuple(bone.tail_local),
                    )
                    for bone in imported.data.bones
                ),
            )

        assert reimport_signature(output) == reimport_signature(output.with_name("original.fbx"))

    for path, expected_hash in preserved.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash
    print("MOTION_BRIDGE_CANONICAL_METADATA_TEST=PASS")


if __name__ == "__main__":
    main()
