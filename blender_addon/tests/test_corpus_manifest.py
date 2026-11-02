from __future__ import annotations

import json
from pathlib import Path
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]


class CorpusManifestTests(unittest.TestCase):
    def test_procedural_generators_are_reproducible_and_present(self) -> None:
        manifest = json.loads(
            (REPOSITORY / "samples/synthetic/corpus_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(1, manifest["schemaVersion"])
        self.assertFalse(manifest["license"]["containsThirdPartyAssets"])
        for generator in manifest["generators"]:
            self.assertIs(type(generator["generatorVersion"]), int)
            self.assertGreater(generator["generatorVersion"], 0)
            seed = generator["seed"]
            if seed is None:
                self.assertIs(generator.get("deterministic"), True)
            else:
                self.assertIs(type(seed), int)
                source = (REPOSITORY / generator["entrypoint"]).read_text(
                    encoding="utf-8"
                )
                self.assertIn("--seed", source)
            self.assertTrue(generator["usage"])
            self.assertTrue((REPOSITORY / generator["entrypoint"]).is_file())

    def test_unbuilt_target_specs_cannot_be_release_gates(self) -> None:
        manifest = json.loads(
            (REPOSITORY / "samples/synthetic/corpus_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        targets = manifest["plannedTargetSpecifications"]
        self.assertEqual(
            {"short-leg", "canonical", "long-leg"}, {item["id"] for item in targets}
        )
        self.assertTrue(all(item["status"] == "specification-only" for item in targets))
        self.assertIn(
            "specification-only targets are excluded", manifest["releaseGatePolicy"]
        )


if __name__ == "__main__":
    unittest.main()
