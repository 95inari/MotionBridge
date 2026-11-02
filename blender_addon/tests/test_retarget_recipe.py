from __future__ import annotations

import json
from pathlib import Path
import unittest
import uuid

from motion_bridge.retargeting.profile import HUMANOID_MAPPING_FIELDS
from motion_bridge.retargeting.recipe import (
    RETARGET_ALGORITHM_VERSION,
    RetargetRecipe,
    RetargetRecipeError,
)


def _mapping() -> dict[str, str]:
    return {
        field.human_bone: f"Source::{field.human_bone}"
        for field in HUMANOID_MAPPING_FIELDS
    }


def _recipe(**overrides: object) -> RetargetRecipe:
    values: dict[str, object] = {
        "source_instance_id": str(uuid.UUID("12345678-1234-5678-9234-567812345678")),
        "source_object_hint": "Source",
        "source_archive_action_hint": "SOURCE_ARCHIVE::Walk",
        "source_archive_id": "a" * 64,
        "source_archive_checksum": "b" * 64,
        "source_slot_identifier": "OBSource",
        "source_file_format": "fbx",
        "source_filepath": str((Path.cwd() / "deleted/source.fbx").resolve()),
        "source_file_sha256": "c" * 64,
        "source_armature_name": "ImportedRig",
        "source_action_name": "Walk",
        "source_hierarchy_sha256": "d" * 64,
        "source_bind_pose_sha256": "e" * 64,
        "canonical_bind_pose_sha256": "f" * 64,
        "canonical_slot_identifier": "OBCanonical",
        "canonical_slot_display_name": "Canonical",
        "frame_start": 1,
        "frame_end": 30,
        "mapping": _mapping(),
    }
    values.update(overrides)
    return RetargetRecipe.create(**values)


class RetargetRecipeTests(unittest.TestCase):
    def test_round_trip_is_canonical_and_keeps_explicit_mapping(self) -> None:
        recipe = _recipe()
        encoded = recipe.to_json()
        self.assertEqual(recipe, RetargetRecipe.from_json(encoded))
        self.assertEqual(recipe.mapping, _mapping())
        self.assertEqual(encoded, recipe.to_json())
        self.assertFalse(Path(recipe.source_filepath).exists())

    def test_mapping_input_order_does_not_change_recipe(self) -> None:
        reversed_mapping = dict(reversed(tuple(_mapping().items())))
        self.assertEqual(_recipe().to_json(), _recipe(mapping=reversed_mapping).to_json())

    def test_windows_source_path_round_trips_on_any_host(self) -> None:
        recipe = _recipe(source_filepath=r"C:\Motion Takes\walk.fbx")
        self.assertEqual(recipe, RetargetRecipe.from_json(recipe.to_json()))

    def test_invalid_schema_algorithm_hash_and_mapping_are_rejected(self) -> None:
        payload = _recipe().to_dict()
        invalid_payloads = []
        for key, value in (
            ("schemaVersion", 99),
            ("algorithmVersion", RETARGET_ALGORITHM_VERSION + "-future"),
        ):
            invalid = json.loads(json.dumps(payload))
            invalid[key] = value
            invalid_payloads.append(invalid)
        invalid_hash = json.loads(json.dumps(payload))
        invalid_hash["source"]["fileSha256"] = "not-a-hash"
        invalid_payloads.append(invalid_hash)
        missing_mapping = json.loads(json.dumps(payload))
        del missing_mapping["mapping"]["Hips"]
        invalid_payloads.append(missing_mapping)

        for invalid in invalid_payloads:
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(RetargetRecipeError),
            ):
                RetargetRecipe.from_json(json.dumps(invalid))

    def test_reversed_or_non_integer_frame_range_is_rejected(self) -> None:
        with self.assertRaises(RetargetRecipeError):
            _recipe(frame_start=31, frame_end=30)
        with self.assertRaises(RetargetRecipeError):
            _recipe(frame_start=1.5)


if __name__ == "__main__":
    unittest.main()
