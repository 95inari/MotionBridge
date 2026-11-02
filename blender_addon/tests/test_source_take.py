from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from motion_bridge.io import (
    SOURCE_PROVENANCE_SCHEMA_VERSION,
    SourceFileFormat,
    SourceTakeProvenance,
    SourceTakeValidationError,
    bone_bind_pose_fingerprint,
    bone_hierarchy_fingerprint,
    sha256_file,
    validate_source_take_path,
)


class SourceTakePathTests(unittest.TestCase):
    def test_all_supported_extensions_are_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename, expected in (
                ("take.FBX", SourceFileFormat.FBX),
                ("take.gltf", SourceFileFormat.GLTF),
                ("take.GLB", SourceFileFormat.GLB),
            ):
                with self.subTest(filename=filename):
                    path = root / filename
                    path.write_bytes(b"source")
                    validated = validate_source_take_path(path)
                    self.assertEqual(validated.path, path.resolve())
                    self.assertIs(validated.file_format, expected)

    def test_missing_unsupported_and_directory_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unsupported = root / "take.bvh"
            unsupported.write_bytes(b"source")
            directory_path = root / "folder.glb"
            directory_path.mkdir()
            for path in (unsupported, root / "missing.fbx", directory_path):
                with (
                    self.subTest(path=path),
                    self.assertRaises(SourceTakeValidationError),
                ):
                    validate_source_take_path(path)

    def test_file_hash_is_content_based_and_chunk_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "take.fbx"
            payload = bytes(range(251)) * 17
            path.write_bytes(payload)
            expected = hashlib.sha256(payload).hexdigest()
            self.assertEqual(sha256_file(path), expected)
            self.assertEqual(sha256_file(path, chunk_size=13), expected)
            with self.assertRaises(ValueError):
                sha256_file(path, chunk_size=0)


class SourceTakeProvenanceTests(unittest.TestCase):
    def test_bind_pose_fingerprint_is_order_independent_and_pose_sensitive(self) -> None:
        identity = tuple(
            1.0 if row == column else 0.0
            for row in range(4)
            for column in range(4)
        )
        spine_matrix = list(identity)
        spine_matrix[11] = 1.0
        bones = (
            (
                "Hips",
                None,
                (0.0, 0.0, 0.0),
                (0.0, 0.0, 1.0),
                identity,
            ),
            (
                "Spine",
                "Hips",
                (0.0, 0.0, 1.0),
                (0.0, 0.0, 2.0),
                tuple(spine_matrix),
            ),
        )
        fingerprint = bone_bind_pose_fingerprint(bones)
        self.assertEqual(fingerprint, bone_bind_pose_fingerprint(reversed(bones)))
        changed = list(bones)
        changed[1] = (
            "Spine",
            "Hips",
            (0.0, 0.0, 1.0),
            (0.2, 0.0, 2.0),
            tuple(spine_matrix),
        )
        self.assertNotEqual(fingerprint, bone_bind_pose_fingerprint(changed))

    def test_hierarchy_fingerprint_ignores_input_order(self) -> None:
        first = (("Hips", None), ("Spine", "Hips"), ("Head", "Spine"))
        reordered = tuple(reversed(first))
        self.assertEqual(
            bone_hierarchy_fingerprint(first),
            bone_hierarchy_fingerprint(reordered),
        )
        self.assertNotEqual(
            bone_hierarchy_fingerprint(first),
            bone_hierarchy_fingerprint(
                (("Hips", None), ("Spine", "Hips"), ("Head", "Hips"))
            ),
        )

    def test_invalid_hierarchies_are_rejected(self) -> None:
        invalid = (
            (),
            (("Hips", None), ("Hips", None)),
            (("Spine", "Missing"),),
            (("", None),),
        )
        for bones in invalid:
            with (
                self.subTest(bones=bones),
                self.assertRaises(SourceTakeValidationError),
            ):
                bone_hierarchy_fingerprint(bones)

    def test_provenance_properties_and_archive_id_are_deterministic(self) -> None:
        hierarchy = bone_hierarchy_fingerprint((("Hips", None), ("Spine", "Hips")))
        source_path = str((Path.cwd() / "project/source/take.fbx").resolve())
        moved_path = str((Path.cwd() / "moved/source/take.fbx").resolve())
        provenance = SourceTakeProvenance(
            file_format=SourceFileFormat.FBX,
            source_filepath=source_path,
            file_sha256="a" * 64,
            source_armature="Rig",
            action_name="Walk",
            frame_start=1.0,
            frame_end=30.0,
            bone_hierarchy_sha256=hierarchy,
        )
        same = SourceTakeProvenance(
            file_format=SourceFileFormat.FBX,
            source_filepath=source_path,
            file_sha256="a" * 64,
            source_armature="Rig",
            action_name="Walk",
            frame_start=1.0,
            frame_end=30.0,
            bone_hierarchy_sha256=hierarchy,
        )
        self.assertEqual(provenance.archive_id, same.archive_id)
        moved = SourceTakeProvenance(
            file_format=SourceFileFormat.FBX,
            source_filepath=moved_path,
            file_sha256="a" * 64,
            source_armature="Rig",
            action_name="Walk",
            frame_start=1.0,
            frame_end=30.0,
            bone_hierarchy_sha256=hierarchy,
        )
        self.assertEqual(
            provenance.archive_id,
            moved.archive_id,
            "Moving identical source bytes must not change archive identity",
        )
        collision_named = SourceTakeProvenance(
            file_format=SourceFileFormat.FBX,
            source_filepath=source_path,
            file_sha256="a" * 64,
            source_armature="Rig.001",
            action_name="Walk.001",
            frame_start=1.0,
            frame_end=30.0,
            bone_hierarchy_sha256=hierarchy,
        )
        self.assertEqual(
            provenance.archive_id,
            collision_named.archive_id,
            "Scene-local Blender name suffixes must not change archive identity",
        )
        properties = provenance.custom_properties()
        self.assertEqual(
            properties["motion_bridge_source_schema_version"],
            SOURCE_PROVENANCE_SCHEMA_VERSION,
        )
        self.assertEqual(properties["motion_bridge_source_format"], "fbx")
        self.assertEqual(
            properties["motion_bridge_source_filepath"],
            source_path,
        )
        self.assertEqual(
            properties["motion_bridge_source_bone_hierarchy_sha256"], hierarchy
        )
        self.assertEqual(
            properties["motion_bridge_source_archive_id"], provenance.archive_id
        )

    def test_explicit_take_id_is_the_archive_identity(self) -> None:
        take_id = "b" * 64
        provenance = SourceTakeProvenance(
            file_format=SourceFileFormat.FBX,
            source_filepath=str((Path.cwd() / "source/takes.fbx").resolve()),
            file_sha256="a" * 64,
            source_armature="Rig",
            action_name="Run",
            frame_start=1.0,
            frame_end=30.0,
            bone_hierarchy_sha256="c" * 64,
            take_id=take_id,
        )
        self.assertEqual(take_id, provenance.archive_id)
        self.assertEqual(
            take_id,
            provenance.custom_properties()["motion_bridge_source_take_id"],
        )
        with self.assertRaises(SourceTakeValidationError):
            SourceTakeProvenance(
                file_format=SourceFileFormat.FBX,
                source_filepath=str((Path.cwd() / "source/takes.fbx").resolve()),
                file_sha256="a" * 64,
                source_armature="Rig",
                action_name="Run",
                frame_start=1.0,
                frame_end=30.0,
                bone_hierarchy_sha256="c" * 64,
                take_id="not-a-take-id",
            )


if __name__ == "__main__":
    unittest.main()
