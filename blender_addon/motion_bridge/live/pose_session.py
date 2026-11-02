from __future__ import annotations

import ipaddress
import socket
import time

import bpy

from .pose_sampler import CanonicalPoseSampler
from .protocol_v1 import PosePacketEncoder, PosePacketInfo


_WORLD_ROOT_MODES = frozenset({"WORLD_ROOT", "WorldRoot"})
_IN_PLACE_MODES = frozenset({"IN_PLACE", "InPlace"})
_ZERO_VECTOR = (0.0, 0.0, 0.0)
_IDENTITY_QUATERNION = (0.0, 0.0, 0.0, 1.0)
_FLAG_WORLD_ROOT = 1 << 0


class LivePoseSession:
    """Synchronous UDP pose sender intended for a 30/60 Hz Blender timer.

    The sampler and Blender API stay on the calling main thread. There is no
    worker or socket thread, and the packet encoder reuses its internal buffer.
    """

    def __init__(
        self,
        armature: bpy.types.Object,
        host: str,
        port: int,
        session_id: int,
        root_mode: str,
        *,
        send_hz: int = 60,
        initial_sequence: int = 0,
        udp_socket: socket.socket | None = None,
    ) -> None:
        if not host:
            raise ValueError("host must not be empty")
        try:
            address = ipaddress.ip_address(
                "127.0.0.1" if host.lower() == "localhost" else host
            )
        except ValueError as error:
            raise ValueError("host must be an IPv4 loopback address") from error
        if address.version != 4 or not address.is_loopback:
            raise ValueError("host must be an IPv4 loopback address")
        if type(port) is not int or not 1 <= port <= 0xFFFF:
            raise ValueError("port must be in [1, 65535]")
        if type(session_id) is not int or not 0 <= session_id <= 0xFFFFFFFF:
            raise ValueError("session_id must be a uint32")
        if type(initial_sequence) is not int or not 0 <= initial_sequence <= 0xFFFFFFFF:
            raise ValueError("initial_sequence must be a uint32")
        if send_hz not in (30, 60):
            raise ValueError("send_hz must be 30 or 60")
        if root_mode not in _WORLD_ROOT_MODES | _IN_PLACE_MODES:
            raise ValueError("root_mode must be WORLD_ROOT or IN_PLACE")

        self._sampler = CanonicalPoseSampler(armature)
        self._encoder = PosePacketEncoder()
        self._destination = (str(address), port)
        self._session_id = session_id
        self._root_mode = root_mode
        self._send_hz = send_hz
        self._sequence = initial_sequence
        self._last_error: str | None = None
        self._sent_packets = 0
        self._dropped_packets = 0
        self._last_send_succeeded = False
        self._closed = False
        self._owns_socket = udp_socket is None
        self._socket = udp_socket or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setblocking(False)

    @property
    def sequence(self) -> int:
        """The sequence number that the next successful tick will send."""

        return self._sequence

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def sent_packets(self) -> int:
        return self._sent_packets

    @property
    def dropped_packets(self) -> int:
        return self._dropped_packets

    @property
    def last_send_succeeded(self) -> bool:
        return self._last_send_succeeded

    @property
    def interval_seconds(self) -> float:
        return 1.0 / self._send_hz

    def tick(self, scene: bpy.types.Scene) -> PosePacketInfo:
        """Sample, encode, and send one full pose packet on the calling thread."""

        try:
            if self._closed:
                raise RuntimeError("LivePoseSession is closed")
            sample = self._sampler.sample(scene)
            world_root = self._root_mode in _WORLD_ROOT_MODES
            packet, info = self._encoder.encode(
                session_id=self._session_id,
                sequence=self._sequence,
                timestamp_usec=time.monotonic_ns() // 1_000,
                frame_index=sample.frame_index,
                flags=_FLAG_WORLD_ROOT if world_root else 0,
                root_position=sample.root_position if world_root else _ZERO_VECTOR,
                root_rotation=sample.root_rotation if world_root else _IDENTITY_QUATERNION,
                hips_local_position_delta=sample.hips_local_position_delta,
                joint_rotations=sample.joint_rotations,
            )
            try:
                sent = self._socket.sendto(packet, self._destination)
            except BlockingIOError:
                # UDP pose data is full-state and latest-only. Dropping one
                # sample is preferable to blocking Blender's main thread.
                self._dropped_packets += 1
                self._last_send_succeeded = False
            else:
                if sent != info.packet_bytes:
                    raise RuntimeError(
                        f"UDP pose send was truncated: {sent}/{info.packet_bytes}"
                    )
                self._sent_packets += 1
                self._last_send_succeeded = True
        except Exception as error:
            self._last_error = f"{type(error).__name__}: {error}"
            raise

        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        self._last_error = None
        return info

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_socket:
            self._socket.close()

    def __enter__(self) -> LivePoseSession:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()
