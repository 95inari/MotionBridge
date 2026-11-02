from __future__ import annotations

from dataclasses import dataclass
import math
import struct
from typing import Sequence


MAGIC = b"MBP1"
PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0
MESSAGE_TYPE_POSE = 1
FLAG_CONTACTS = 1 << 1
MAX_DESCRIPTOR_JOINTS = 64
MAX_JOINTS = 60
MAX_PACKET_BYTES = 1200
CONTROL_LENGTH_PREFIX_BYTES = 4
MAX_CONTROL_PAYLOAD_BYTES = 65_536
MINIMUM_NORMALIZED_NORM_SQUARED = 0.9801
MAXIMUM_NORMALIZED_NORM_SQUARED = 1.0201
DEFAULT_CONTROL_PORT = 39871

HEADER = struct.Struct("<4sBBBBIIQiHH")
ROOT_HIPS_AND_MASK = struct.Struct("<3f4f3fQ")
QUATERNION = struct.Struct("<4f")
CONTACT_WEIGHTS = struct.Struct("<4f")
CONTACT_EPOCHS = struct.Struct("<4I")
CONTACT_POSITION = struct.Struct("<3f")
GROUND_PLANE = struct.Struct("<4f")

HEADER_BYTES = HEADER.size
CONTACT_COUNT = 4
CONTACT_BLOCK_BYTES = (
    CONTACT_WEIGHTS.size
    + CONTACT_EPOCHS.size
    + CONTACT_POSITION.size * CONTACT_COUNT
    + QUATERNION.size * CONTACT_COUNT
    + GROUND_PLANE.size
)
FIXED_PAYLOAD_BYTES = ROOT_HIPS_AND_MASK.size + CONTACT_BLOCK_BYTES


@dataclass(frozen=True)
class PosePacketInfo:
    session_id: int
    sequence: int
    timestamp_usec: int
    frame_index: int
    flags: int
    joint_count: int
    packet_bytes: int


def _finite(values: Sequence[float]) -> bool:
    return all(math.isfinite(value) for value in values)


def _wire_float32_values(
    codec: struct.Struct,
    values: Sequence[float],
    field: str,
) -> tuple[float, ...]:
    try:
        wire_values = codec.unpack(codec.pack(*values))
    except (OverflowError, struct.error) as exception:
        raise ValueError(f"{field} cannot be represented as float32") from exception
    if not _finite(wire_values):
        raise ValueError(f"{field} must contain finite float32 values")
    return wire_values


def _normalized_quaternion(value: Sequence[float]) -> tuple[float, float, float, float]:
    if len(value) != 4 or not _finite(value):
        raise ValueError("Quaternion must contain four finite values")
    scale = max(abs(component) for component in value)
    if scale <= 1e-12:
        raise ValueError("Quaternion must not be zero")
    scaled = tuple(component / scale for component in value)
    inverse_norm = 1.0 / math.sqrt(sum(component * component for component in scaled))
    normalized = tuple(component * inverse_norm for component in scaled)
    if not _finite(normalized):
        raise ValueError("Quaternion normalization produced a non-finite value")
    wire_quaternion = _wire_float32_values(QUATERNION, normalized, "Quaternion")
    wire_norm_squared = sum(component * component for component in wire_quaternion)
    if (
        wire_norm_squared < MINIMUM_NORMALIZED_NORM_SQUARED
        or wire_norm_squared > MAXIMUM_NORMALIZED_NORM_SQUARED
    ):
        raise ValueError("Quaternion float32 norm must be in [0.99, 1.01]")
    return wire_quaternion  # type: ignore[return-value]


def _is_identity_quaternion(value: Sequence[float], tolerance: float = 1e-5) -> bool:
    x, y, z, w = value
    return (
        abs(x) <= tolerance
        and abs(y) <= tolerance
        and abs(z) <= tolerance
        and abs(abs(w) - 1.0) <= tolerance
    )


class PosePacketEncoder:
    """Allocation-stable PosePacket v1 encoder.

    The returned memoryview aliases an internal buffer and must be sent or
    copied before the next call to :meth:`encode`.
    """

    def __init__(self) -> None:
        self._buffer = bytearray(MAX_PACKET_BYTES)

    def encode(
        self,
        *,
        session_id: int,
        sequence: int,
        timestamp_usec: int,
        frame_index: int,
        flags: int,
        root_position: Sequence[float],
        root_rotation: Sequence[float],
        hips_local_position_delta: Sequence[float] = (0.0, 0.0, 0.0),
        joint_rotations: Sequence[Sequence[float]],
        contact_weights: Sequence[float] = (0.0, 0.0, 0.0, 0.0),
        contact_epochs: Sequence[int] = (0, 0, 0, 0),
        contact_positions: Sequence[Sequence[float]] = (
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0),
        ),
        contact_rotations: Sequence[Sequence[float]] = (
            (0.0, 0.0, 0.0, 1.0),
            (0.0, 0.0, 0.0, 1.0),
            (0.0, 0.0, 0.0, 1.0),
            (0.0, 0.0, 0.0, 1.0),
        ),
        ground_plane: Sequence[float] = (0.0, 1.0, 0.0, 0.0),
        present_mask: int | None = None,
    ) -> tuple[memoryview, PosePacketInfo]:
        joint_count = len(joint_rotations)
        if joint_count < 1 or joint_count > MAX_JOINTS:
            raise ValueError(f"joint_count must be in [1, {MAX_JOINTS}]")
        if flags < 0 or flags > 0x0F:
            raise ValueError("Reserved flag bits must be zero")
        if len(root_position) != 3 or not _finite(root_position):
            raise ValueError("root_position must contain three finite values")
        if len(hips_local_position_delta) != 3 or not _finite(hips_local_position_delta):
            raise ValueError("hips_local_position_delta must contain three finite values")
        if len(contact_weights) != CONTACT_COUNT or not _finite(contact_weights):
            raise ValueError("contact_weights must contain four finite values")
        if any(weight < 0.0 or weight > 1.0 for weight in contact_weights):
            raise ValueError("contact weights must be in [0, 1]")
        if not (flags & FLAG_CONTACTS) and any(weight != 0.0 for weight in contact_weights):
            raise ValueError("contact weights must be zero when the contacts flag is unset")
        if len(contact_epochs) != CONTACT_COUNT or any(
            epoch < 0 or epoch > 0xFFFFFFFF for epoch in contact_epochs
        ):
            raise ValueError("contact_epochs must contain four uint32 values")
        if len(contact_positions) != CONTACT_COUNT or any(
            len(position) != 3 or not _finite(position) for position in contact_positions
        ):
            raise ValueError("contact_positions must contain four finite vectors")
        if len(contact_rotations) != CONTACT_COUNT:
            raise ValueError("contact_rotations must contain four quaternions")
        if len(ground_plane) != 4 or not _finite(ground_plane):
            raise ValueError("ground_plane must contain four finite values")
        wire_ground_plane = _wire_float32_values(
            GROUND_PLANE,
            ground_plane,
            "ground_plane",
        )
        ground_normal_norm_squared = sum(
            component * component for component in wire_ground_plane[:3]
        )
        if (
            ground_normal_norm_squared < MINIMUM_NORMALIZED_NORM_SQUARED
            or ground_normal_norm_squared > MAXIMUM_NORMALIZED_NORM_SQUARED
        ):
            raise ValueError("ground plane normal must be normalized")

        root_quaternion = _normalized_quaternion(root_rotation)
        payload_bytes = FIXED_PAYLOAD_BYTES + QUATERNION.size * joint_count
        packet_bytes = HEADER_BYTES + payload_bytes
        if packet_bytes > MAX_PACKET_BYTES:
            raise ValueError("Pose packet exceeds safe UDP payload size")

        if present_mask is None:
            present_mask = (1 << joint_count) - 1
        if present_mask < 0 or present_mask >= (1 << MAX_JOINTS):
            raise ValueError("present_mask is outside the pose transport joint range")
        valid_mask = (1 << joint_count) - 1
        if present_mask & ~valid_mask:
            raise ValueError("present_mask contains a bit outside joint_count")

        HEADER.pack_into(
            self._buffer,
            0,
            MAGIC,
            PROTOCOL_MAJOR,
            PROTOCOL_MINOR,
            MESSAGE_TYPE_POSE,
            flags,
            session_id,
            sequence,
            timestamp_usec,
            frame_index,
            payload_bytes,
            joint_count,
        )

        offset = HEADER_BYTES
        ROOT_HIPS_AND_MASK.pack_into(
            self._buffer,
            offset,
            *root_position,
            *root_quaternion,
            *hips_local_position_delta,
            present_mask,
        )
        offset += ROOT_HIPS_AND_MASK.size

        for index, quaternion in enumerate(joint_rotations):
            normalized = _normalized_quaternion(quaternion)
            if not (present_mask & (1 << index)) and not _is_identity_quaternion(normalized):
                raise ValueError("Absent joint rotations must be identity")
            QUATERNION.pack_into(self._buffer, offset, *normalized)
            offset += QUATERNION.size

        CONTACT_WEIGHTS.pack_into(self._buffer, offset, *contact_weights)
        offset += CONTACT_WEIGHTS.size
        CONTACT_EPOCHS.pack_into(self._buffer, offset, *contact_epochs)
        offset += CONTACT_EPOCHS.size
        for position in contact_positions:
            CONTACT_POSITION.pack_into(self._buffer, offset, *position)
            offset += CONTACT_POSITION.size
        for rotation in contact_rotations:
            QUATERNION.pack_into(self._buffer, offset, *_normalized_quaternion(rotation))
            offset += QUATERNION.size
        GROUND_PLANE.pack_into(self._buffer, offset, *wire_ground_plane)
        info = PosePacketInfo(
            session_id=session_id,
            sequence=sequence,
            timestamp_usec=timestamp_usec,
            frame_index=frame_index,
            flags=flags,
            joint_count=joint_count,
            packet_bytes=packet_bytes,
        )
        return memoryview(self._buffer)[:packet_bytes], info
