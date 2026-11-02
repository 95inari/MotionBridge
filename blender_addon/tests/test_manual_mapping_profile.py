import json
import unittest
import uuid

from motion_bridge.retargeting.profile import (
    HUMANOID_MAPPING_FIELDS,
    HUMAN_BONES,
    SOURCE_MAPPING_PROFILE_SCHEMA_VERSION,
    SourceMappingProfile,
    SourceMappingProfileError,
    StaleSourceMappingProfileError,
    mapping_from_profile_values,
    profile_values_from_mapping,
)


def _source_identity(**overrides: str) -> dict[str, str]:
    identity = {
        "source_instance_id": str(uuid.UUID("12345678-1234-5678-9234-567812345678")),
        "source_archive_id": "a" * 64,
        "source_hierarchy_sha256": "b" * 64,
        "source_bind_pose_sha256": "c" * 64,
        "source_action_name": "Walk",
        "source_action_slot_identifier": "OBSource",
    }
    identity.update(overrides)
    return identity


def _source_profile(**overrides: object) -> SourceMappingProfile:
    values: dict[str, object] = {
        "mapping": {
            "Hips": "mixamorig:Hips",
            "Head": "mixamorig:Head",
            "UpperChest": "",
        },
        **_source_identity(),
    }
    values.update(overrides)
    return SourceMappingProfile.create(**values)  # type: ignore[arg-type]


class ManualMappingProfileTests(unittest.TestCase):
    def test_profile_has_stable_unique_22_human_bones(self) -> None:
        self.assertEqual(len(HUMANOID_MAPPING_FIELDS), 22)
        self.assertEqual(len(set(HUMAN_BONES)), 22)
        self.assertEqual(
            len({field.property_name for field in HUMANOID_MAPPING_FIELDS}),
            22,
        )
        self.assertEqual(HUMAN_BONES[0], "Hips")
        self.assertEqual(HUMAN_BONES[-1], "RightToes")

    def test_mapping_round_trip_keeps_blank_optional_fields_sparse(self) -> None:
        mapping = {
            field.human_bone: f"src::{field.human_bone}"
            for field in HUMANOID_MAPPING_FIELDS
            if field.required
        }
        values = profile_values_from_mapping(mapping)

        self.assertEqual(len(values), 22)
        self.assertEqual(values["bone_upper_chest"], "")
        self.assertEqual(mapping_from_profile_values(values), mapping)

    def test_unknown_human_bone_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown Human bone"):
            profile_values_from_mapping({"Tail": "tail"})

    def test_incomplete_or_unknown_profile_is_rejected(self) -> None:
        values = profile_values_from_mapping({})
        values.pop("bone_hips")
        with self.assertRaisesRegex(ValueError, "profile is incomplete"):
            mapping_from_profile_values(values)

        values = profile_values_from_mapping({})
        values["bone_tail"] = "tail"
        with self.assertRaisesRegex(ValueError, "Unknown manual mapping"):
            mapping_from_profile_values(values)

    def test_non_string_source_name_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be a string"):
            profile_values_from_mapping({"Hips": 42})  # type: ignore[dict-item]

    def test_source_bound_profile_round_trip_is_canonical_and_sparse(self) -> None:
        profile = _source_profile()
        encoded = profile.to_json()

        self.assertEqual(SourceMappingProfile.from_json(encoded), profile)
        self.assertEqual(encoded, profile.to_json())
        self.assertEqual(
            json.loads(encoded)["schemaVersion"],
            SOURCE_MAPPING_PROFILE_SCHEMA_VERSION,
        )
        self.assertEqual(
            profile.mapping,
            {"Head": "mixamorig:Head", "Hips": "mixamorig:Hips"},
        )

    def test_mapping_input_order_does_not_change_serialization(self) -> None:
        first = _source_profile(mapping={"Hips": "hips", "Head": "head"})
        second = _source_profile(mapping={"Head": "head", "Hips": "hips"})
        self.assertEqual(first.to_json(), second.to_json())

    def test_unknown_schema_and_malformed_json_are_rejected(self) -> None:
        payload = _source_profile().to_dict()
        for schema_version in (True, 0, 2, "1"):
            invalid = json.loads(json.dumps(payload))
            invalid["schemaVersion"] = schema_version
            with (
                self.subTest(schema_version=schema_version),
                self.assertRaisesRegex(
                    SourceMappingProfileError, "schema is unsupported"
                ),
            ):
                SourceMappingProfile.from_json(json.dumps(invalid))

        for encoded in ("", "[]", "{not-json"):
            with (
                self.subTest(encoded=encoded),
                self.assertRaises(SourceMappingProfileError),
            ):
                SourceMappingProfile.from_json(encoded)

    def test_invalid_uuid_and_hashes_are_rejected(self) -> None:
        invalid_identities = (
            {"source_instance_id": "not-a-uuid"},
            {"source_instance_id": "ABCDEFAB-1234-4678-9234-567812345678"},
            {"source_archive_id": "not-a-hash"},
            {"source_hierarchy_sha256": "B" * 64},
            {"source_bind_pose_sha256": "c" * 63},
        )
        for overrides in invalid_identities:
            with (
                self.subTest(overrides=overrides),
                self.assertRaises(SourceMappingProfileError),
            ):
                _source_profile(**overrides)

    def test_unknown_mapping_bone_and_non_string_value_are_rejected(self) -> None:
        for mapping in ({"Tail": "tail"}, {"Hips": 42}):
            with (
                self.subTest(mapping=mapping),
                self.assertRaises(SourceMappingProfileError),
            ):
                _source_profile(mapping=mapping)

    def test_matching_source_identity_is_accepted(self) -> None:
        profile = _source_profile()
        identity = _source_identity()

        self.assertTrue(profile.matches_source(**identity))
        self.assertIsNone(profile.require_source(**identity))

    def test_action_rename_does_not_stale_source_identity(self) -> None:
        profile = _source_profile()
        renamed = _source_identity(source_action_name="Walk.001")

        self.assertTrue(profile.matches_source(**renamed))
        self.assertIsNone(profile.require_source(**renamed))
        self.assertEqual(profile.source_action_name, "Walk")

    def test_each_stale_safety_identity_field_is_rejected(self) -> None:
        profile = _source_profile()
        stale_values = {
            "source_instance_id": "87654321-4321-4765-8765-876543218765",
            "source_archive_id": "d" * 64,
            "source_hierarchy_sha256": "e" * 64,
            "source_bind_pose_sha256": "f" * 64,
            "source_action_slot_identifier": "OBAnotherSource",
        }

        for field, stale_value in stale_values.items():
            current = _source_identity(**{field: stale_value})
            with self.subTest(field=field):
                self.assertFalse(profile.matches_source(**current))
                with self.assertRaisesRegex(
                    StaleSourceMappingProfileError,
                    "refresh it from the current automatic mapping",
                ):
                    profile.require_source(**current)

    def test_current_identity_is_validated_before_matching(self) -> None:
        profile = _source_profile()
        with self.assertRaisesRegex(SourceMappingProfileError, "must be a UUID"):
            profile.matches_source(**_source_identity(source_instance_id="not-a-uuid"))


if __name__ == "__main__":
    unittest.main()
