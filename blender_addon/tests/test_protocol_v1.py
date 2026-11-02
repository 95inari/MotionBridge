import struct
import unittest
from pathlib import Path

from motion_bridge.live.protocol_v1 import (
    HEADER,
    MAGIC,
    MAX_PACKET_BYTES,
    MAXIMUM_NORMALIZED_NORM_SQUARED,
    MINIMUM_NORMALIZED_NORM_SQUARED,
    PosePacketEncoder,
)


GROUND_NORM_BOUNDARY_CASES = (
    (
        "csharp_float_accumulation",
        (0.9444475769996643, 0.016245830804109573, -0.296403169631958, 0.0),
    ),
    (
        "python_pre_quantization",
        (-0.44875013786458123, 0.6513351440677846, -0.5953871814419072, 0.0),
    ),
)


class PosePacketEncoderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.encoder = PosePacketEncoder()

    def test_identity_packet_has_expected_header_and_size(self) -> None:
        packet, info = self.encoder.encode(
            session_id=0x12345678,
            sequence=7,
            timestamp_usec=123_000,
            frame_index=42,
            flags=0,
            root_position=(0.0, 0.0, 0.0),
            root_rotation=(0.0, 0.0, 0.0, 1.0),
            joint_rotations=((0.0, 0.0, 0.0, 1.0),) * 56,
        )

        header = HEADER.unpack_from(packet, 0)
        self.assertEqual(MAGIC, header[0])
        self.assertEqual(56, header[-1])
        self.assertEqual(1136, info.packet_bytes)
        self.assertLessEqual(len(packet), MAX_PACKET_BYTES)

    def test_quaternion_is_normalized(self) -> None:
        packet, _ = self.encoder.encode(
            session_id=1,
            sequence=1,
            timestamp_usec=1,
            frame_index=1,
            flags=0,
            root_position=(0.0, 0.0, 0.0),
            root_rotation=(0.0, 0.0, 0.0, 2.0),
            joint_rotations=((0.0, 0.0, 0.0, 3.0),),
        )
        root_w = struct.unpack_from("<f", packet, 56)[0]
        joint_w = struct.unpack_from("<f", packet, 92)[0]
        self.assertAlmostEqual(1.0, root_w)
        self.assertAlmostEqual(1.0, joint_w)

    def test_quaternion_wire_norm_uses_binary64_bounds(self) -> None:
        packet, _ = self.encoder.encode(
            session_id=1,
            sequence=1,
            timestamp_usec=1,
            frame_index=1,
            flags=0,
            root_position=(0.0, 0.0, 0.0),
            root_rotation=(1.0, 2.0, 3.0, 4.0),
            joint_rotations=((4.0, 3.0, 2.0, 1.0),),
        )

        for offset in (44, 80):
            quaternion = struct.unpack_from("<4f", packet, offset)
            norm_squared = sum(component * component for component in quaternion)
            self.assertGreaterEqual(norm_squared, MINIMUM_NORMALIZED_NORM_SQUARED)
            self.assertLessEqual(norm_squared, MAXIMUM_NORMALIZED_NORM_SQUARED)

    def test_ground_plane_is_validated_after_float32_quantization(self) -> None:
        for name, ground_plane in GROUND_NORM_BOUNDARY_CASES:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.encoder.encode(
                    session_id=1,
                    sequence=1,
                    timestamp_usec=1,
                    frame_index=1,
                    flags=0,
                    root_position=(0.0, 0.0, 0.0),
                    root_rotation=(0.0, 0.0, 0.0, 1.0),
                    joint_rotations=((0.0, 0.0, 0.0, 1.0),),
                    ground_plane=ground_plane,
                )

    def test_zero_quaternion_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.encoder.encode(
                session_id=1,
                sequence=1,
                timestamp_usec=1,
                frame_index=1,
                flags=0,
                root_position=(0.0, 0.0, 0.0),
                root_rotation=(0.0, 0.0, 0.0, 1.0),
                joint_rotations=((0.0, 0.0, 0.0, 0.0),),
            )

    def test_reserved_flag_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.encoder.encode(
                session_id=1,
                sequence=1,
                timestamp_usec=1,
                frame_index=1,
                flags=0x10,
                root_position=(0.0, 0.0, 0.0),
                root_rotation=(0.0, 0.0, 0.0, 1.0),
                joint_rotations=((0.0, 0.0, 0.0, 1.0),),
            )

    def test_huge_quaternion_remains_decodable_unit_quaternion(self) -> None:
        packet, _ = self.encoder.encode(
            session_id=1,
            sequence=1,
            timestamp_usec=1,
            frame_index=1,
            flags=0,
            root_position=(0.0, 0.0, 0.0),
            root_rotation=(1e308, 0.0, 0.0, 0.0),
            joint_rotations=((0.0, 1e308, 0.0, 0.0),),
        )
        root_x = struct.unpack_from("<f", packet, 44)[0]
        joint_y = struct.unpack_from("<f", packet, 84)[0]
        self.assertAlmostEqual(1.0, root_x)
        self.assertAlmostEqual(1.0, joint_y)

    def test_present_mask_cannot_address_missing_joint(self) -> None:
        with self.assertRaises(ValueError):
            self.encoder.encode(
                session_id=1,
                sequence=1,
                timestamp_usec=1,
                frame_index=1,
                flags=0,
                root_position=(0.0, 0.0, 0.0),
                root_rotation=(0.0, 0.0, 0.0, 1.0),
                joint_rotations=((0.0, 0.0, 0.0, 1.0),),
                present_mask=0b10,
            )

    def test_contact_weight_requires_contacts_flag(self) -> None:
        with self.assertRaises(ValueError):
            self.encoder.encode(
                session_id=1,
                sequence=1,
                timestamp_usec=1,
                frame_index=1,
                flags=0,
                root_position=(0.0, 0.0, 0.0),
                root_rotation=(0.0, 0.0, 0.0, 1.0),
                joint_rotations=((0.0, 0.0, 0.0, 1.0),),
                contact_weights=(1.0, 0.0, 0.0, 0.0),
            )

    def test_hips_and_contact_state_offsets(self) -> None:
        packet, _ = self.encoder.encode(
            session_id=1,
            sequence=1,
            timestamp_usec=1,
            frame_index=1,
            flags=0b10,
            root_position=(0.0, 0.0, 0.0),
            root_rotation=(0.0, 0.0, 0.0, 1.0),
            hips_local_position_delta=(1.0, 2.0, 3.0),
            joint_rotations=((0.0, 0.0, 0.0, 1.0),),
            contact_weights=(0.75, 0.0, 0.0, 0.0),
            contact_epochs=(17, 0, 0, 0),
            contact_positions=((4.0, 5.0, 6.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
        )

        self.assertEqual((1.0, 2.0, 3.0), struct.unpack_from("<3f", packet, 60))
        self.assertAlmostEqual(0.75, struct.unpack_from("<f", packet, 96)[0])
        self.assertEqual(17, struct.unpack_from("<I", packet, 112)[0])
        self.assertEqual((4.0, 5.0, 6.0), struct.unpack_from("<3f", packet, 128))

    def test_encoder_matches_shared_full_state_fixture(self) -> None:
        packet, _ = self.encoder.encode(
            session_id=0x12345678,
            sequence=7,
            timestamp_usec=123_000,
            frame_index=42,
            flags=0b11,
            root_position=(1.0, 2.0, 3.0),
            root_rotation=(0.0, 0.0, 0.0, 1.0),
            hips_local_position_delta=(0.1, 0.2, 0.3),
            joint_rotations=((0.0, 0.0, 0.0, 1.0),),
            contact_weights=(0.75, 0.5, 0.25, 0.0),
            contact_epochs=(17, 18, 19, 20),
            contact_positions=((1.0, 0.0, 0.0), (2.0, 0.0, 0.0), (3.0, 0.0, 0.0), (4.0, 0.0, 0.0)),
            contact_rotations=((0.0, 0.0, 0.0, 1.0),) * 4,
            ground_plane=(0.0, 1.0, 0.0, -0.2),
        )
        fixture_path = (
            Path(__file__).resolve().parents[2]
            / "protocol/golden/pose_v1_full_state_1_joint.hex"
        )
        expected = bytes.fromhex(fixture_path.read_text(encoding="ascii"))
        self.assertEqual(expected, bytes(packet))


if __name__ == "__main__":
    unittest.main()
