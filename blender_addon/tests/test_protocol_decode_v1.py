import math
from pathlib import Path
import struct
import unittest

from motion_bridge.live.protocol_decode_v1 import (
    PoseDecodeError,
    PosePacketDecodeError,
    PosePacketDecoder,
    PosePacketFlags,
    decode_pose_packet,
)
from motion_bridge.live.protocol_v1 import FIXED_PAYLOAD_BYTES, HEADER_BYTES, PosePacketEncoder


FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "protocol/golden/pose_v1_full_state_1_joint.hex"
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


def shared_fixture() -> bytearray:
    return bytearray.fromhex(FIXTURE_PATH.read_text(encoding="ascii"))


class PosePacketDecoderTests(unittest.TestCase):
    def assert_decode_error(
        self,
        packet: bytes | bytearray,
        expected: PoseDecodeError,
    ) -> None:
        with self.assertRaises(PosePacketDecodeError) as caught:
            decode_pose_packet(packet)
        self.assertIs(expected, caught.exception.code)

    def test_decodes_shared_full_state_fixture(self) -> None:
        frame = PosePacketDecoder.decode(shared_fixture())

        self.assertEqual(0x12345678, frame.session_id)
        self.assertEqual(7, frame.sequence)
        self.assertEqual(123_000, frame.timestamp_usec)
        self.assertEqual(42, frame.frame_index)
        self.assertEqual(PosePacketFlags.WORLD_ROOT | PosePacketFlags.CONTACTS, frame.flags)
        self.assertEqual(1, frame.joint_count)
        self.assertEqual(FIXED_PAYLOAD_BYTES + 16, frame.payload_bytes)
        self.assertEqual(HEADER_BYTES + FIXED_PAYLOAD_BYTES + 16, frame.packet_bytes)
        self.assertEqual((1.0, 2.0, 3.0), frame.root_position)
        self.assertEqual((0.0, 0.0, 0.0, 1.0), frame.root_rotation)
        for actual, expected in zip(frame.hips_local_position_delta, (0.1, 0.2, 0.3)):
            self.assertAlmostEqual(expected, actual)
        self.assertEqual(1, frame.present_mask)
        self.assertEqual(((0.0, 0.0, 0.0, 1.0),), frame.joint_rotations)
        self.assertEqual((0.75, 0.5, 0.25, 0.0), frame.contact_weights)
        self.assertEqual((17, 18, 19, 20), frame.contact_epochs)
        self.assertEqual(
            ((1.0, 0.0, 0.0), (2.0, 0.0, 0.0), (3.0, 0.0, 0.0), (4.0, 0.0, 0.0)),
            frame.contact_positions,
        )
        self.assertEqual(((0.0, 0.0, 0.0, 1.0),) * 4, frame.contact_rotations)
        self.assertAlmostEqual(-0.2, frame.ground_plane[3])

    def test_rejects_invalid_length(self) -> None:
        cases = {
            "truncated": shared_fixture()[:-1],
            "trailing": shared_fixture() + b"\0",
            "wrong payload length": shared_fixture(),
        }
        struct.pack_into("<H", cases["wrong payload length"], 28, 0)

        for name, packet in cases.items():
            with self.subTest(name=name):
                self.assert_decode_error(packet, PoseDecodeError.INVALID_LENGTH)

    def test_rejects_bad_magic(self) -> None:
        packet = shared_fixture()
        packet[0:4] = b"NOPE"
        self.assert_decode_error(packet, PoseDecodeError.BAD_MAGIC)

    def test_rejects_unsupported_version(self) -> None:
        for offset, value in ((4, 2), (5, 1)):
            packet = shared_fixture()
            packet[offset] = value
            with self.subTest(offset=offset):
                self.assert_decode_error(packet, PoseDecodeError.UNSUPPORTED_VERSION)

    def test_rejects_nan(self) -> None:
        for offset in (32, 60, 128, 240):
            packet = shared_fixture()
            struct.pack_into("<f", packet, offset, math.nan)
            with self.subTest(offset=offset):
                self.assert_decode_error(packet, PoseDecodeError.NON_FINITE_VALUE)

    def test_rejects_invalid_quaternion(self) -> None:
        for offset in (44, 80, 176):
            packet = shared_fixture()
            struct.pack_into("<4f", packet, offset, 0.0, 0.0, 0.0, 0.0)
            with self.subTest(offset=offset):
                self.assert_decode_error(packet, PoseDecodeError.INVALID_QUATERNION)

    def test_rejects_cross_language_ground_norm_boundary_values(self) -> None:
        for name, ground_plane in GROUND_NORM_BOUNDARY_CASES:
            packet = shared_fixture()
            struct.pack_into("<4f", packet, len(packet) - 16, *ground_plane)
            with self.subTest(name=name):
                self.assert_decode_error(packet, PoseDecodeError.NON_FINITE_VALUE)

    def test_rejects_present_mask_outside_joint_count(self) -> None:
        packet = shared_fixture()
        struct.pack_into("<Q", packet, 72, 0b10)
        self.assert_decode_error(packet, PoseDecodeError.INVALID_PRESENT_MASK)

    def test_rejects_non_identity_rotation_for_absent_joint(self) -> None:
        packet = shared_fixture()
        struct.pack_into("<Q", packet, 72, 0)
        struct.pack_into("<4f", packet, 80, 1.0, 0.0, 0.0, 0.0)
        self.assert_decode_error(packet, PoseDecodeError.INVALID_PRESENT_MASK)

    def test_rejects_contact_weight_when_flag_is_unset(self) -> None:
        packet = shared_fixture()
        packet[7] &= ~int(PosePacketFlags.CONTACTS)
        self.assert_decode_error(packet, PoseDecodeError.INVALID_CONTACT_WEIGHT)

    def test_rejects_contact_weight_outside_unit_interval(self) -> None:
        packet = shared_fixture()
        struct.pack_into("<f", packet, 96, 1.01)
        self.assert_decode_error(packet, PoseDecodeError.INVALID_CONTACT_WEIGHT)

    def test_accepts_maximum_size_packet(self) -> None:
        encoder = PosePacketEncoder()
        packet, info = encoder.encode(
            session_id=1,
            sequence=1,
            timestamp_usec=1,
            frame_index=1,
            flags=0,
            root_position=(0.0, 0.0, 0.0),
            root_rotation=(0.0, 0.0, 0.0, 1.0),
            joint_rotations=((0.0, 0.0, 0.0, 1.0),) * 60,
        )

        frame = decode_pose_packet(packet)
        self.assertEqual(1200, info.packet_bytes)
        self.assertEqual(60, frame.joint_count)
        self.assertEqual(1200, frame.packet_bytes)


if __name__ == "__main__":
    unittest.main()
