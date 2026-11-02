import copy
import json
import math
import unittest
from pathlib import Path

from motion_bridge.canonical.descriptor import (
    canonicalize_json_bytes,
    skeleton_hash_hex,
    validate_descriptor,
)
from motion_bridge.canonical.rig_spec import SKELETON_HASH


def _descriptor() -> dict:
    descriptor = {
        "type": "skeleton_descriptor",
        "protocolMajor": 1,
        "protocolMinor": 0,
        "sessionId": 0,
        "requestId": 0,
        "skeletonId": "mb-canonical-humanoid-v1",
        "skeletonHash": "",
        "coordinateSpace": "unity-left-handed-y-up-z-forward",
        "rootIndex": 0,
        "joints": [
            {
                "index": 0,
                "name": "Root",
                "humanBone": None,
                "parentIndex": -1,
                "jointPath": "",
                "required": True,
                "restLocalPosition": [-0.0, 0.0, 0.0],
                "restLocalRotation": [0.0, 0.0, 0.0, 1.0],
                "restGlobalRotation": [0.0, 0.0, 0.0, 1.0],
            },
            {
                "index": 1,
                "name": "Hips",
                "humanBone": "Hips",
                "parentIndex": 0,
                "jointPath": "Hips",
                "required": True,
                "restLocalPosition": [0.0, 1.0, 0.0],
                "restLocalRotation": [0.0, 0.0, 0.0, 1.0],
                "restGlobalRotation": [0.0, 0.0, 0.0, 1.0],
            },
        ],
    }
    descriptor["skeletonHash"] = skeleton_hash_hex(descriptor)
    return descriptor


class JsonCanonicalizationTests(unittest.TestCase):
    def test_rfc8785_serialization_sample(self) -> None:
        value = {
            "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 1e-27],
            "string": "\u20ac$\u000f\nA'B\"\\\\\"/",
            "literals": [None, True, False],
        }
        expected = (
            '{"literals":[null,true,false],"numbers":'
            '[333333333.3333333,1e+30,4.5,0.002,1e-27],'
            '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
        ).encode("utf-8")

        self.assertEqual(expected, canonicalize_json_bytes(value))

    def test_negative_zero_and_ecmascript_number_thresholds(self) -> None:
        value = [-0.0, 0.0, 1e-6, 1e-7, 1e20, 1e21]
        self.assertEqual(
            b"[0,0,0.000001,1e-7,100000000000000000000,1e+21]",
            canonicalize_json_bytes(value),
        )

    def test_object_keys_use_utf16_order_and_output_is_utf8(self) -> None:
        value = {
            "\ufb33": "Hebrew",
            "😀": "Emoji",
            "€": "Euro",
            "ö": "Latin",
            "\u0080": "Control",
            "1": "One",
            "\r": "Carriage Return",
        }
        result = canonicalize_json_bytes(value)
        text = result.decode("utf-8")
        ordered_keys = ['"\\r"', '"1"', '"\u0080"', '"ö"', '"€"', '"😀"', '"\ufb33"']

        offsets = [text.index(key + ":") for key in ordered_keys]
        self.assertEqual(sorted(offsets), offsets)
        self.assertIn("€".encode("utf-8"), result)

    def test_invalid_json_domain_values_are_rejected(self) -> None:
        for value in (math.nan, math.inf, -math.inf, "\ud800", 1 << 60):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    canonicalize_json_bytes(value)
        with self.assertRaises(TypeError):
            canonicalize_json_bytes({1: "non-string key"})


class SkeletonDescriptorTests(unittest.TestCase):
    def test_generated_canonical_descriptor_is_valid(self) -> None:
        descriptor_path = (
            Path(__file__).resolve().parents[2]
            / "protocol/generated/canonical_humanoid_v1.json"
        )
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))

        self.assertIsNone(validate_descriptor(descriptor))
        self.assertEqual(SKELETON_HASH, descriptor["skeletonHash"])
        self.assertEqual(23, len(descriptor["joints"]))
        self.assertEqual("", descriptor["joints"][0]["jointPath"])
        self.assertAlmostEqual(
            -math.sqrt(0.5),
            descriptor["joints"][0]["restLocalRotation"][0],
            places=6,
        )

    def test_schema_is_wire_descriptor_v1(self) -> None:
        schema_path = (
            Path(__file__).resolve().parents[2]
            / "protocol/schema/skeleton_descriptor_v1.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(1, schema["properties"]["protocolMajor"]["const"])
        self.assertEqual("skeleton_descriptor", schema["properties"]["type"]["const"])
        self.assertEqual(64, schema["properties"]["joints"]["maxItems"])
        self.assertTrue(
            {"sessionId", "requestId", "skeletonHash", "joints"}.issubset(schema["required"])
        )

    def test_valid_descriptor_and_hash_are_accepted(self) -> None:
        descriptor = _descriptor()
        self.assertIsNone(validate_descriptor(descriptor))
        self.assertEqual(
            "651e762705de7d428e9a91e8e5698c3383ec45e03d21bf321fed23a08731752e",
            descriptor["skeletonHash"],
        )

    def test_hash_uses_only_semantic_skeleton_projection(self) -> None:
        descriptor = _descriptor()
        changed_envelope = copy.deepcopy(descriptor)
        changed_envelope.update(
            type="ignored-by-hash",
            protocolMajor=99,
            protocolMinor=88,
            sessionId=0xFFFFFFFF,
            requestId=123,
            skeletonHash="f" * 64,
        )

        self.assertEqual(skeleton_hash_hex(descriptor), skeleton_hash_hex(changed_envelope))
        changed_envelope["joints"][1]["restLocalPosition"][1] = 1.25
        self.assertNotEqual(skeleton_hash_hex(descriptor), skeleton_hash_hex(changed_envelope))

    def test_descriptor_key_order_does_not_change_hash(self) -> None:
        descriptor = _descriptor()
        reversed_descriptor = dict(reversed(tuple(descriptor.items())))
        reversed_descriptor["joints"] = [
            dict(reversed(tuple(joint.items()))) for joint in descriptor["joints"]
        ]
        self.assertEqual(skeleton_hash_hex(descriptor), skeleton_hash_hex(reversed_descriptor))

    def test_hash_mismatch_invalid_hierarchy_and_nonfinite_rest_are_rejected(self) -> None:
        cases = []

        wrong_hash = _descriptor()
        wrong_hash["skeletonHash"] = "0" * 64
        cases.append(wrong_hash)

        invalid_path = _descriptor()
        invalid_path["joints"][1]["jointPath"] = "Wrong/Hips"
        invalid_path["skeletonHash"] = skeleton_hash_hex(invalid_path)
        cases.append(invalid_path)

        nonfinite = _descriptor()
        nonfinite["joints"][1]["restLocalPosition"][0] = math.nan
        cases.append(nonfinite)

        zero_rotation = _descriptor()
        zero_rotation["joints"][1]["restLocalRotation"] = [0.0, 0.0, 0.0, 0.0]
        zero_rotation["skeletonHash"] = skeleton_hash_hex(zero_rotation)
        cases.append(zero_rotation)

        for descriptor in cases:
            with self.subTest(descriptor=descriptor):
                with self.assertRaises(ValueError):
                    validate_descriptor(descriptor)


if __name__ == "__main__":
    unittest.main()
