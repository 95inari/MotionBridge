from __future__ import annotations

import json
from pathlib import Path
import re
import tomllib
import unittest

from motion_bridge.io.source_take import SOURCE_PROVENANCE_SCHEMA_VERSION
from motion_bridge.live.control_client import (
    BlenderControlClientConfig,
    _REQUESTED_CAPABILITIES,
)
from motion_bridge.live.protocol_v1 import PROTOCOL_MAJOR, PROTOCOL_MINOR
from motion_bridge.retargeting.profile import SOURCE_MAPPING_PROFILE_SCHEMA_VERSION
from motion_bridge.retargeting.recipe import RETARGET_RECIPE_SCHEMA_VERSION


REPOSITORY = Path(__file__).resolve().parents[2]


def _integer_constant(path: Path, name: str) -> int:
    match = re.search(
        rf"^{re.escape(name)}\s*=\s*(\d+)\s*$",
        path.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    if match is None:
        raise AssertionError(f"Could not read {name} from {path}")
    return int(match.group(1))


class CompatibilityManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(
            (REPOSITORY / "compatibility_manifest.json").read_text(encoding="utf-8")
        )

    def test_release_versions_match_package_manifests(self) -> None:
        blender = tomllib.loads(
            (REPOSITORY / "blender_addon/blender_manifest.toml").read_text(
                encoding="utf-8"
            )
        )
        unity = json.loads(
            (
                REPOSITORY
                / "verification/unity-6000.2/Packages/com.kuko.motionbridge/package.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            blender["version"], self.manifest["blender"]["extensionVersion"]
        )
        self.assertEqual(blender["version"], BlenderControlClientConfig.addon_version)
        self.assertEqual(unity["version"], self.manifest["unity"]["packageVersion"])
        self.assertEqual(unity["unity"], self.manifest["unity"]["editorVersion"])

    def test_protocol_and_schema_versions_match_runtime(self) -> None:
        self.assertEqual(PROTOCOL_MAJOR, self.manifest["protocol"]["major"])
        self.assertEqual(PROTOCOL_MINOR, self.manifest["protocol"]["minor"])
        schemas = self.manifest["dataSchemas"]
        self.assertEqual(SOURCE_PROVENANCE_SCHEMA_VERSION, schemas["sourceTake"])
        self.assertEqual(
            SOURCE_MAPPING_PROFILE_SCHEMA_VERSION, schemas["sourceMappingProfile"]
        )
        self.assertEqual(RETARGET_RECIPE_SCHEMA_VERSION, schemas["retargetRecipe"])
        self.assertEqual(
            _integer_constant(
                REPOSITORY / "blender_addon/motion_bridge/editing/correction_layers.py",
                "LAYER_SCHEMA_VERSION",
            ),
            schemas["correctionLayer"],
        )
        self.assertEqual(
            _integer_constant(
                REPOSITORY
                / "blender_addon/motion_bridge/editing/correction_envelope.py",
                "_METADATA_SCHEMA_VERSION",
            ),
            schemas["correctionEnvelope"],
        )

    def test_only_end_to_end_capabilities_are_advertised(self) -> None:
        enabled = tuple(sorted(self.manifest["capabilities"]["enabledEndToEnd"]))
        self.assertEqual(enabled, tuple(sorted(_REQUESTED_CAPABILITIES)))
        reserved = self.manifest["capabilities"]["reservedNotAdvertised"]
        self.assertIn("contacts-v1", reserved)
        self.assertNotIn("contacts-v1", _REQUESTED_CAPABILITIES)


if __name__ == "__main__":
    unittest.main()
