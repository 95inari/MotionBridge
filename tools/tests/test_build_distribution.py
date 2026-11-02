"""Exercise release archives with clean-room sources and damaged inputs."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
import zipfile

from tools import build_distribution as builder


VERSION = "0.1.13"
UNITY_VERSION = "0.1.0"


def write(root: Path, relative: str | Path, content: str | bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)


def write_json(root: Path, relative: str | Path, payload: object) -> None:
    write(root, relative, json.dumps(payload))


def unzip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if archive.testzip() is not None:
            raise AssertionError("ZIP CRC failed")
        if len(archive.namelist()) != len(set(archive.namelist())):
            raise AssertionError("Duplicate ZIP members")
        return {name: archive.read(name) for name in archive.namelist()}


def untar(data: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        result = {}
        for member in archive.getmembers():
            if not member.isfile() or member.name in result:
                raise AssertionError("UPM must contain unique regular files")
            stream = archive.extractfile(member)
            assert stream is not None
            result[member.name] = stream.read()
        return result


class DistributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="motionbridge-package-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "source"
        self.unity = self.root / builder.UNITY_PACKAGE
        self.output = self.base / "output"
        self.create_source()

    def create_source(self) -> None:
        write_json(self.root, "distribution_config.json", {
            "schemaVersion": 1,
            "licensePolicyConfirmed": True,
        })
        write(self.root, "blender_addon/blender_manifest.toml", (
            'schema_version = "1.0.0"\nid = "motion_bridge"\n'
            f'version = "{VERSION}"\nname = "Motion Bridge"\n'
            'type = "add-on"\nblender_version_min = "4.5.0"\n'
            'license = ["SPDX:MIT", "SPDX:GPL-3.0-or-later"]\n'
        ))
        for name in ("__init__.py", "motion_bridge/__init__.py", "motion_bridge/addon.py",
                     "motion_bridge/motion_quality_worker.py", "motion_bridge/motion_quality_process.py"):
            write(self.root, "blender_addon/" + name, '"""Fixture module."""\n')
        write(self.root, "blender_addon/motion_bridge/live/control_client.py",
              f'addon_version: str = "{VERSION}"\n')
        for name, content in {
            "README_JA.md": "# License scope\n",
            "MIT.txt": "MIT License fixture\nCopyright 2026 kuko\n",
            "GPL-3.0-or-later.txt": "GPL version 3 or later fixture\n",
            "COMMERCIAL_TERMS_JA.md": "# Commercial terms fixture\n",
        }.items():
            write(self.root, "licenses/" + name, content)
            if name.endswith(".txt"):
                write(self.root, "blender_addon/licenses/" + name, content)
        write(self.root, "blender_addon/LICENSE.txt", "See licenses/ for component terms.\n")
        write_json(self.unity, "package.json", {
            "name": "com.kuko.motionbridge",
            "version": UNITY_VERSION,
            "unity": "6000.2",
            "dependencies": {
                "com.unity.modules.animation": "1.0.0",
                "com.unity.modules.imgui": "1.0.0",
                "com.unity.modules.jsonserialize": "1.0.0",
            },
            "samples": [{"displayName": "Canonical Source Avatar", "path": "Samples~/Canonical"}],
        })
        write(self.unity, "README.md", "# Install from tarball\n")
        write(self.unity, "Runtime/Pose.cs", "namespace MotionBridge { class Pose {} }\n")
        write(self.unity, "Runtime/Protocol/MotionBridgeProtocol.cs",
              f'public static class MotionBridgeProtocol {{ public const string PackageVersion = "{UNITY_VERSION}"; }}\n')
        write(self.unity, "Editor/Window.cs", "namespace MotionBridge { class Window {} }\n")
        write_json(self.unity, "Runtime/Runtime.asmdef", {"name": "MotionBridge.Runtime"})
        write_json(self.unity, "Editor/Editor.asmdef", {"name": "MotionBridge.Editor"})
        for index, name in enumerate((
            "Runtime", "Editor", "Runtime/Pose.cs", "Editor/Window.cs",
            "Runtime/Runtime.asmdef", "Editor/Editor.asmdef", "package.json", "README.md",
            "Runtime/Protocol", "Runtime/Protocol/MotionBridgeProtocol.cs",
        ), start=1):
            write(self.unity, name + ".meta", f"fileFormatVersion: 2\nguid: {index:032x}\n")
        write(self.unity, "Samples~/Canonical/MB_Canonical_Humanoid_v1.fbx", b"CANONICAL_FBX_FIXTURE")
        write(self.unity, "Samples~/Canonical/README.md", "# Canonical Source Avatar\n")
        write(self.root, "samples/canonical/mb_canonical_humanoid_v1.blend", b"CANONICAL_BLEND_FIXTURE")
        write_json(self.root, "compatibility_manifest.json", {
            "blender": {"extensionVersion": VERSION},
            "unity": {"packageVersion": UNITY_VERSION, "editorVersion": "6000.2"},
        })
        for name in ("docs/distribution/README_JA.md", "QUICKSTART.md", "CHANGELOG.md"):
            write(self.root, name, "# Buyer documentation\n")

    def payloads(self) -> dict[str, bytes]:
        version, payloads = builder.build_files(self.root)
        self.assertEqual(VERSION, version)
        return payloads

    def assert_integrity(self, files: dict[str, bytes]) -> None:
        checksums = dict(line.split("  ", 1)[::-1] for line in files["SHA256SUMS.txt"].decode().splitlines())
        self.assertEqual(set(files) - {"SHA256SUMS.txt"}, set(checksums))
        for name, digest in checksums.items():
            self.assertEqual(hashlib.sha256(files[name]).hexdigest(), digest, name)
        manifest = json.loads(files["release_manifest.json"])
        self.assertEqual(set(files) - {"SHA256SUMS.txt", "release_manifest.json"}, set(manifest["files"]))
        for name, record in manifest["files"].items():
            self.assertEqual(len(files[name]), record["bytes"], name)
            self.assertEqual(hashlib.sha256(files[name]).hexdigest(), record["sha256"], name)

    def test_installable_layout_contains_worker_samples_and_license_texts(self) -> None:
        files = self.payloads()
        addon = unzip(files[f"Blender/motion_bridge-{VERSION}.zip"])
        self.assertIn("blender_manifest.toml", addon)
        self.assertIn("__init__.py", addon)
        self.assertIn("motion_bridge/motion_quality_worker.py", addon)
        self.assertIn("motion_bridge/motion_quality_process.py", addon)
        self.assertIn("LICENSE.txt", addon)
        for name in ("MIT.txt", "GPL-3.0-or-later.txt"):
            self.assertEqual(files["Licenses/" + name], addon["licenses/" + name])
        upm = untar(files[f"Unity/com.kuko.motionbridge-{UNITY_VERSION}.tgz"])
        self.assertTrue(all(name.startswith("package/") for name in upm))
        package = json.loads(upm["package/package.json"])
        self.assertEqual("com.kuko.motionbridge", package["name"])
        self.assertEqual(UNITY_VERSION, package["version"])
        for sample in package["samples"]:
            self.assertIn("package/" + sample["path"] + "/MB_Canonical_Humanoid_v1.fbx", upm)
        self.assertEqual(files["Licenses/COMMERCIAL_TERMS_JA.md"], upm["package/LICENSE.md"])
        self.assertIn("package/LICENSE.md.meta", upm)
        self.assertEqual(b"CANONICAL_BLEND_FIXTURE", files["Samples/mb_canonical_humanoid_v1.blend"])
        self.assert_integrity(files)

    def test_reproducible_across_locations_source_modes_and_timestamps(self) -> None:
        first = builder.build(self.root, self.output)
        relocated = self.base / "other source location 日本語"
        shutil.copytree(self.root, relocated)
        for path in relocated.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
                os.utime(path, (1234567890, 1234567890))
        second = builder.build(relocated, self.base / "other output")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(first.with_suffix(".zip.sha256").read_bytes(), second.with_suffix(".zip.sha256").read_bytes())
        digest, filename = first.with_suffix(".zip.sha256").read_text().strip().split("  ")
        self.assertEqual(first.name, filename)
        self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), digest)
        contents = unzip(first.read_bytes())
        prefix = f"MotionBridge-{VERSION}/"
        self.assertTrue(all(name.startswith(prefix) for name in contents))
        self.assert_integrity({name.removeprefix(prefix): data for name, data in contents.items()})

    def test_private_third_party_test_and_cache_pollution_is_excluded(self) -> None:
        marker = b"DO_NOT_SHIP_PRIVATE_OR_THIRD_PARTY"
        for name in (
            ".env", "build/leaked.py", "docs/distribution/BOOTH_DESCRIPTION.md",
            "docs/distribution/RELEASE_CHECKLIST.md", "samples/poc_manifest.json",
            "verification/unity-6000.2/Assets/Vendor/paid_model.fbx",
            "blender_addon/tests/test_private.py", "blender_addon/tools/local.py",
            "blender_addon/motion_bridge/__pycache__/secret.py",
            "blender_addon/motion_bridge/.DS_Store", "blender_addon/motion_bridge/private.txt",
        ):
            write(self.root, name, marker)
        for name in ("Tests/Private.cs", "Tests.meta", "Runtime/private.env", "Runtime/cache.pyc",
                     "Runtime/.git/secret.cs", "Samples~/Canonical/private.fbx", "Samples~/Private/paid.fbx"):
            write(self.unity, name, marker)
        files = self.payloads()
        all_payloads = dict(files)
        all_payloads.update(unzip(files[f"Blender/motion_bridge-{VERSION}.zip"]))
        all_payloads.update(untar(files[f"Unity/com.kuko.motionbridge-{UNITY_VERSION}.tgz"]))
        for name, data in all_payloads.items():
            self.assertNotIn(marker, data, name)
            self.assertNotIn("..", Path(name).parts, name)
            self.assertFalse(Path(name).is_absolute(), name)

    def test_missing_worker_is_rejected(self) -> None:
        (self.root / "blender_addon/motion_bridge/motion_quality_worker.py").unlink()
        with self.assertRaisesRegex(ValueError, "runtime file"):
            self.payloads()

    def test_missing_sample_is_rejected_without_replacing_previous_release(self) -> None:
        first = builder.build(self.root, self.output)
        previous = {path.name: path.read_bytes() for path in self.output.iterdir()}
        (self.unity / "Samples~/Canonical/MB_Canonical_Humanoid_v1.fbx").unlink()
        with self.assertRaises(ValueError):
            builder.build(self.root, self.output)
        self.assertEqual(previous, {path.name: path.read_bytes() for path in self.output.iterdir()})
        self.assertTrue(first.is_file())

    def test_source_file_symlink_is_rejected(self) -> None:
        external = self.base / "private.py"
        external.write_text("private = True\n")
        (self.root / "blender_addon/motion_bridge/private.py").symlink_to(external)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.payloads()

    def test_unity_packages_parent_symlink_is_rejected(self) -> None:
        packages = self.unity.parent
        relocated = self.base / "external-packages"
        packages.rename(relocated)
        packages.symlink_to(relocated, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.payloads()

    def test_compatibility_version_drift_is_rejected(self) -> None:
        path = self.root / "compatibility_manifest.json"
        original = path.read_bytes()
        for component, field in (("blender", "extensionVersion"), ("unity", "packageVersion"), ("unity", "editorVersion")):
            with self.subTest(component=component, field=field):
                manifest = json.loads(original)
                manifest[component][field] = "99.99.99"
                path.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, "version disagrees"):
                    self.payloads()
        path.write_bytes(original)

    def test_handshake_version_drift_is_rejected(self) -> None:
        write(self.root, "blender_addon/motion_bridge/live/control_client.py", 'addon_version: str = "0.0.1"\n')
        with self.assertRaisesRegex(ValueError, "handshake version"):
            self.payloads()

    def test_unity_metadata_damage_is_rejected(self) -> None:
        path = self.unity / "Runtime/Pose.cs.meta"
        original = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(ValueError, "Missing Unity metadata"):
            self.payloads()
        path.write_bytes((self.unity / "Editor/Window.cs.meta").read_bytes())
        with self.assertRaisesRegex(ValueError, "duplicate Unity GUID"):
            self.payloads()
        path.write_text("fileFormatVersion: 2\nguid: broken\n")
        with self.assertRaisesRegex(ValueError, "Unity GUID"):
            self.payloads()
        path.write_bytes(original)

    def test_unity_handshake_version_drift_is_rejected(self) -> None:
        write(self.unity, "Runtime/Protocol/MotionBridgeProtocol.cs",
              'public static class MotionBridgeProtocol { public const string PackageVersion = "0.0.1"; }\n')
        with self.assertRaisesRegex(ValueError, "Unity handshake version"):
            self.payloads()

    def test_missing_unity_module_is_rejected(self) -> None:
        path = self.unity / "package.json"
        original = path.read_bytes()
        for module in ("animation", "imgui", "jsonserialize"):
            with self.subTest(module=module):
                package = json.loads(original)
                del package["dependencies"]["com.unity.modules." + module]
                path.write_text(json.dumps(package))
                with self.assertRaisesRegex(ValueError, "Required Unity module dependency"):
                    self.payloads()
        path.write_bytes(original)

    def test_removed_sample_registration_is_rejected(self) -> None:
        path = self.unity / "package.json"
        package = json.loads(path.read_bytes())
        package["samples"] = []
        path.write_text(json.dumps(package))
        with self.assertRaisesRegex(ValueError, "sample is not registered"):
            self.payloads()

    def test_personal_paths_in_fbx_metadata_are_rejected(self) -> None:
        for source in (b"/Users/author/private/source.blend", b"/home/author/source.blend",
                       rb"C:\Users\author\source.blend", b"C:/Users/author/source.blend"):
            with self.subTest(source=source):
                write(self.unity, "Samples~/Canonical/MB_Canonical_Humanoid_v1.fbx",
                      b"ApplicationNativeFile\x00" + source)
                with self.assertRaisesRegex(ValueError, "Personal source path"):
                    self.payloads()

    def test_conflicting_license_copies_are_rejected(self) -> None:
        write(self.root, "blender_addon/licenses/MIT.txt", "outdated license copy")
        with self.assertRaisesRegex(ValueError, "license copy is out of date"):
            self.payloads()

    def test_unconfirmed_terms_allow_only_a_marked_checksummed_preview(self) -> None:
        write_json(self.root, "distribution_config.json", {"schemaVersion": 1, "licensePolicyConfirmed": False})
        with self.assertRaisesRegex(ValueError, "License terms are pending"):
            builder.build(self.root, self.output)
        self.assertFalse(self.output.exists())
        preview = builder.build(self.root, self.output, preview=True)
        self.assertTrue(preview.name.endswith("-PREVIEW.zip"))
        prefix = f"MotionBridge-{VERSION}/"
        files = {name.removeprefix(prefix): data for name, data in unzip(preview.read_bytes()).items()}
        self.assertIn("PREVIEW_NOT_FOR_SALE.txt", files)
        self.assertIn("利用条件の確認用", files["START_HERE.md"].decode())
        self.assertEqual("preview", json.loads(files["release_manifest.json"])["distributionStatus"])
        self.assert_integrity(files)


class RepositoryPackagingContractTests(unittest.TestCase):
    def test_unity_declares_modules_needed_in_an_empty_project(self) -> None:
        # Editor GUI and TCP JsonUtility must not depend on modules incidentally
        # pulled in by the development project's test framework or render pipeline.
        package = json.loads((builder.REPOSITORY / builder.UNITY_PACKAGE / "package.json").read_bytes())
        dependencies = package["dependencies"]
        for module in ("animation", "imgui", "jsonserialize"):
            with self.subTest(module=module):
                self.assertEqual("1.0.0", dependencies.get("com.unity.modules." + module))


if __name__ == "__main__":
    unittest.main()
