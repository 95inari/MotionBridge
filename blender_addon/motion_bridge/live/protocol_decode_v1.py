from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntFlag
import math

from .protocol_v1 import (
    CONTACT_COUNT,
    CONTACT_EPOCHS,
    CONTACT_POSITION,
    CONTACT_WEIGHTS,
    FIXED_PAYLOAD_BYTES,
    GROUND_PLANE,
    HEADER,
    HEADER_BYTES,
    MAGIC,
    MAX_JOINTS,
    MAX_PACKET_BYTES,
    MAXIMUM_NORMALIZED_NORM_SQUARED,
    MESSAGE_TYPE_POSE,
    MINIMUM_NORMALIZED_NORM_SQUARED,
    PROTOCOL_MAJOR,
    PROTOCOL_MINOR,
    QUATERNION,
    ROOT_HIPS_AND_MASK,
)


class PosePacketFlags(IntFlag):
    NONE = 0
    WORLD_ROOT = 1 << 0
    CONTACTS = 1 << 1
    PLAYING = 1 << 2
    SCRUB_OR_EDIT = 1 << 3


class PoseDecodeError(str, Enum):
    TOO_SHORT = "too_short"
    BAD_MAGIC = "bad_magic"
    UNSUPPORTED_VERSION = "unsupported_version"
    UNSUPPORTED_MESSAGE_TYPE = "unsupported_message_type"
    RESERVED_FLAGS = "reserved_flags"
    INVALID_LENGTH = "invalid_length"
    INVALID_JOINT_COUNT = "invalid_joint_count"
    INVALID_PRESENT_MASK = "invalid_present_mask"
    NON_FINITE_VALUE = "non_finite_value"
    INVALID_QUATERNION = "invalid_quaternion"
    INVALID_CONTACT_WEIGHT = "invalid_contact_weight"


class PosePacketDecodeError(ValueError):
    def __init__(self, code: PoseDecodeError, message: str) -> None:
        super().__init__(message)
        self.code = code


Vector3 = tuple[float, float, float]
Vector4 = tuple[float, float, float, float]
Quaternion = Vector4


@dataclass(frozen=True, slots=True)
class DecodedPosePacket:
    session_id: int
    sequence: int
    timestamp_usec: int
    frame_index: int
    flags: PosePacketFlags
    joint_count: int
    payload_bytes: int
    packet_bytes: int
    root_position: Vector3
    root_rotation: Quaternion
    hips_local_position_delta: Vector3
    present_mask: int
    joint_rotations: tuple[Quaternion, ...]
    contact_weights: Vector4
    contact_epochs: tuple[int, int, int, int]
    contact_positions: tuple[Vector3, Vector3, Vector3, Vector3]
    contact_rotations: tuple[Quaternion, Quaternion, Quaternion, Quaternion]
    ground_plane: Vector4


def _fail(code: PoseDecodeError, message: str) -> None:
    raise PosePacketDecodeError(code, message)


def _is_finite(values: tuple[float, ...]) -> bool:
    return all(math.isfinite(value) for value in values)


def _vector3(values: tuple[float, ...], field: str) -> Vector3:
    if not _is_finite(values):
        _fail(PoseDecodeError.NON_FINITE_VALUE, f"{field} must contain finite values")
    return values  # type: ignore[return-value]


def _quaternion(values: tuple[float, ...], field: str) -> Quaternion:
    if not _is_finite(values):
        _fail(PoseDecodeError.NON_FINITE_VALUE, f"{field} must contain finite values")

    norm_squared = sum(component * component for component in values)
    if (
        norm_squared < MINIMUM_NORMALIZED_NORM_SQUARED
        or norm_squared > MAXIMUM_NORMALIZED_NORM_SQUARED
    ):
        _fail(
            PoseDecodeError.INVALID_QUATERNION,
            f"{field} norm must be in [0.99, 1.01]",
        )

    norm = math.sqrt(norm_squared)
    return tuple(component / norm for component in values)  # type: ignore[return-value]


def _is_identity(value: Quaternion, tolerance: float = 1e-5) -> bool:
    x, y, z, w = value
    return (
        abs(x) <= tolerance
        and abs(y) <= tolerance
        and abs(z) <= tolerance
        and abs(abs(w) - 1.0) <= tolerance
    )


ReadablePacket = bytes | bytearray | memoryview


def decode_pose_packet(packet: ReadablePacket) -> DecodedPosePacket:
    """Decode and validate one PosePacket v1 datagram.

    Session-specific checks such as the accepted session ID, required-joint
    mask, and sequence freshness belong at the publication boundary and are
    intentionally not handled by this wire decoder.
    """

    view = memoryview(packet).cast("B")
    packet_bytes = len(view)
    if packet_bytes < HEADER_BYTES:
        _fail(PoseDecodeError.TOO_SHORT, "Pose packet is shorter than its header")

    (
        magic,
        major,
        minor,
        message_type,
        raw_flags,
        session_id,
        sequence,
        timestamp_usec,
        frame_index,
        payload_bytes,
        joint_count,
    ) = HEADER.unpack_from(view, 0)

    if magic != MAGIC:
        _fail(PoseDecodeError.BAD_MAGIC, "Pose packet magic is not MBP1")
    if major != PROTOCOL_MAJOR or minor > PROTOCOL_MINOR:
        _fail(PoseDecodeError.UNSUPPORTED_VERSION, "Unsupported PosePacket version")
    if message_type != MESSAGE_TYPE_POSE:
        _fail(PoseDecodeError.UNSUPPORTED_MESSAGE_TYPE, "Unsupported PosePacket message type")
    if raw_flags & 0xF0:
        _fail(PoseDecodeError.RESERVED_FLAGS, "Reserved PosePacket flag bits must be zero")
    if joint_count < 1 or joint_count > MAX_JOINTS:
        _fail(
            PoseDecodeError.INVALID_JOINT_COUNT,
            f"joint_count must be in [1, {MAX_JOINTS}]",
        )

    expected_payload_bytes = FIXED_PAYLOAD_BYTES + QUATERNION.size * joint_count
    expected_packet_bytes = HEADER_BYTES + expected_payload_bytes
    if (
        payload_bytes != expected_payload_bytes
        or packet_bytes != expected_packet_bytes
        or packet_bytes > MAX_PACKET_BYTES
    ):
        _fail(PoseDecodeError.INVALID_LENGTH, "PosePacket length does not match joint_count")

    unpacked_root = ROOT_HIPS_AND_MASK.unpack_from(view, HEADER_BYTES)
    root_position = _vector3(unpacked_root[0:3], "root_position")
    root_rotation = _quaternion(unpacked_root[3:7], "root_rotation")
    hips_local_position_delta = _vector3(
        unpacked_root[7:10],
        "hips_local_position_delta",
    )
    present_mask = unpacked_root[10]

    valid_mask = (1 << joint_count) - 1
    if present_mask & ~valid_mask:
        _fail(
            PoseDecodeError.INVALID_PRESENT_MASK,
            "present_mask contains a bit outside joint_count",
        )

    offset = HEADER_BYTES + ROOT_HIPS_AND_MASK.size
    joint_rotations: list[Quaternion] = []
    for index in range(joint_count):
        rotation = _quaternion(
            QUATERNION.unpack_from(view, offset),
            f"joint_rotations[{index}]",
        )
        if not (present_mask & (1 << index)) and not _is_identity(rotation):
            _fail(
                PoseDecodeError.INVALID_PRESENT_MASK,
                "An absent joint must contain an identity quaternion",
            )
        joint_rotations.append(rotation)
        offset += QUATERNION.size

    contact_weights = CONTACT_WEIGHTS.unpack_from(view, offset)
    offset += CONTACT_WEIGHTS.size
    if not _is_finite(contact_weights):
        _fail(PoseDecodeError.NON_FINITE_VALUE, "contact_weights must be finite")
    if any(weight < 0.0 or weight > 1.0 for weight in contact_weights):
        _fail(PoseDecodeError.INVALID_CONTACT_WEIGHT, "contact weights must be in [0, 1]")
    flags = PosePacketFlags(raw_flags)
    if not (flags & PosePacketFlags.CONTACTS) and any(
        weight != 0.0 for weight in contact_weights
    ):
        _fail(
            PoseDecodeError.INVALID_CONTACT_WEIGHT,
            "contact weights must be zero when the contacts flag is unset",
        )

    contact_epochs = CONTACT_EPOCHS.unpack_from(view, offset)
    offset += CONTACT_EPOCHS.size

    contact_positions: list[Vector3] = []
    for index in range(CONTACT_COUNT):
        contact_positions.append(
            _vector3(
                CONTACT_POSITION.unpack_from(view, offset),
                f"contact_positions[{index}]",
            )
        )
        offset += CONTACT_POSITION.size

    contact_rotations: list[Quaternion] = []
    for index in range(CONTACT_COUNT):
        contact_rotations.append(
            _quaternion(
                QUATERNION.unpack_from(view, offset),
                f"contact_rotations[{index}]",
            )
        )
        offset += QUATERNION.size

    ground_plane = GROUND_PLANE.unpack_from(view, offset)
    offset += GROUND_PLANE.size
    if not _is_finite(ground_plane):
        _fail(PoseDecodeError.NON_FINITE_VALUE, "ground_plane must be finite")
    ground_normal_norm_squared = sum(
        component * component for component in ground_plane[:3]
    )
    if (
        ground_normal_norm_squared < MINIMUM_NORMALIZED_NORM_SQUARED
        or ground_normal_norm_squared > MAXIMUM_NORMALIZED_NORM_SQUARED
    ):
        _fail(PoseDecodeError.NON_FINITE_VALUE, "ground plane normal must be normalized")
    if offset != packet_bytes:
        _fail(PoseDecodeError.INVALID_LENGTH, "PosePacket contains trailing data")

    return DecodedPosePacket(
        session_id=session_id,
        sequence=sequence,
        timestamp_usec=timestamp_usec,
        frame_index=frame_index,
        flags=flags,
        joint_count=joint_count,
        payload_bytes=payload_bytes,
        packet_bytes=packet_bytes,
        root_position=root_position,
        root_rotation=root_rotation,
        hips_local_position_delta=hips_local_position_delta,
        present_mask=present_mask,
        joint_rotations=tuple(joint_rotations),
        contact_weights=contact_weights,
        contact_epochs=contact_epochs,
        contact_positions=tuple(contact_positions),  # type: ignore[arg-type]
        contact_rotations=tuple(contact_rotations),  # type: ignore[arg-type]
        ground_plane=ground_plane,
    )


class PosePacketDecoder:
    @staticmethod
    def decode(packet: ReadablePacket) -> DecodedPosePacket:
        return decode_pose_packet(packet)
