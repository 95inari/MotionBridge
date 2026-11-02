from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
import errno
import ipaddress
import math
import re
import select
import socket
import time
from typing import Any

from ..canonical.descriptor import validate_descriptor
from .control_protocol import (
    ControlFrameDecoder,
    ControlProtocolError,
    common_control_fields,
    encode_control_message,
    validate_common_control_fields,
)


_CONNECTING_ERRORS = frozenset(
    {
        0,
        errno.EINPROGRESS,
        errno.EWOULDBLOCK,
        errno.EALREADY,
        errno.EINTR,
        errno.EISCONN,
    }
)
_REQUESTED_CAPABILITIES = (
    "global-rest-delta-v1",
    "clock-sync-v1",
    "pose-stats-v1",
    "one-way-pose-age-v1",
)


class ControlSessionState(Enum):
    DISCONNECTED = "Disconnected"
    CONNECTING = "Connecting"
    HANDSHAKING = "Handshaking"
    READY = "Ready"
    STREAMING = "Streaming"
    PAUSED = "Paused"
    ERROR = "Error"


class ReconnectBackoff:
    """Bounded exponential retry schedule driven by a monotonic clock.

    The scheduler deliberately owns no socket or Blender state.  A live-link
    coordinator can therefore keep the user's streaming intent while replacing
    every failed TCP/UDP session with fresh transport objects.
    """

    def __init__(
        self,
        initial_seconds: float = 0.25,
        maximum_seconds: float = 4.0,
    ) -> None:
        if not math.isfinite(initial_seconds) or initial_seconds <= 0:
            raise ValueError("initial_seconds must be finite and positive")
        if not math.isfinite(maximum_seconds) or maximum_seconds < initial_seconds:
            raise ValueError(
                "maximum_seconds must be finite and at least initial_seconds"
            )
        self._initial_seconds = initial_seconds
        self._maximum_seconds = maximum_seconds
        self._attempt_count = 0
        self._next_retry_at: float | None = None

    @property
    def attempt_count(self) -> int:
        return self._attempt_count

    @property
    def next_retry_at(self) -> float | None:
        return self._next_retry_at

    def schedule(self, now: float) -> float:
        self._validate_now(now)
        delay = min(
            self._maximum_seconds,
            self._initial_seconds * (2 ** min(self._attempt_count, 30)),
        )
        self._attempt_count += 1
        self._next_retry_at = now + delay
        return delay

    def seconds_until_retry(self, now: float) -> float:
        self._validate_now(now)
        if self._next_retry_at is None:
            return 0.0
        return max(0.0, self._next_retry_at - now)

    def reset(self) -> None:
        self._attempt_count = 0
        self._next_retry_at = None

    @staticmethod
    def _validate_now(now: float) -> None:
        if not math.isfinite(now) or now < 0:
            raise ValueError("monotonic time must be finite and non-negative")


def is_retryable_control_error(error: BaseException) -> bool:
    """Return whether a fresh connection may recover from ``error``.

    Protocol and descriptor errors are deterministic for the same settings and
    must stay terminal.  ``ConnectionError`` and ``TimeoutError`` are subclasses
    of ``OSError``, so all transient socket failures share one classification.
    """

    return isinstance(error, OSError) and not isinstance(error, ControlProtocolError)


class _HandshakePhase(Enum):
    NONE = "none"
    WAIT_ACCEPT = "wait_accept"
    WAIT_READY = "wait_ready"


@dataclass(frozen=True, slots=True)
class AcceptedHumanTrait:
    muscle_count: int
    muscle_names_hash: str


@dataclass(frozen=True, slots=True)
class ClockSyncEstimate:
    unity_minus_blender_offset_usec: float
    round_trip_time_usec: int
    sample_count: int


@dataclass(frozen=True, slots=True)
class PoseStats:
    last_sequence: int
    received: int
    dropped_by_sequence: int
    rejected: int
    receiver_overrun: int
    one_way_age_usec_p50: int | None = None
    one_way_age_usec_p95: int | None = None


def _socket_is_writable(control_socket: socket.socket) -> bool:
    _, writable, exceptional = select.select(
        [],
        [control_socket],
        [control_socket],
        0,
    )
    return bool(writable or exceptional)


@dataclass(frozen=True)
class BlenderControlClientConfig:
    session_id: int
    descriptor: Mapping[str, Any]
    control_port: int = 39_871
    control_host: str = "127.0.0.1"
    addon_version: str = "0.1.13"
    blender_version: str = "unknown"
    requested_pose_hz: int = 60
    root_mode: str = "WorldRoot"
    frame_rate_numerator: int = 60
    frame_rate_denominator: int = 1
    ping_interval_seconds: float = 2.0
    timeout_seconds: float = 5.0
    clock_sync_interval_seconds: float = 5.0

    def __post_init__(self) -> None:
        normalized_host = self.control_host.lower()
        if normalized_host == "localhost":
            normalized_host = "127.0.0.1"
        try:
            address = ipaddress.ip_address(normalized_host)
        except ValueError as error:
            raise ValueError("control_host must be localhost or 127.0.0.1") from error
        if str(address) != "127.0.0.1":
            raise ValueError("control_host must be localhost or 127.0.0.1")
        if type(self.control_port) is not int or not 1 <= self.control_port <= 0xFFFF:
            raise ValueError("control_port must be in [1, 65535]")
        if type(self.session_id) is not int or not 0 <= self.session_id <= 0xFFFFFFFF:
            raise ValueError("session_id must be a uint32")
        if not isinstance(self.descriptor, Mapping):
            raise TypeError("descriptor must be a mapping")
        validate_descriptor(dict(self.descriptor))
        if self.requested_pose_hz not in (30, 60):
            raise ValueError("requested_pose_hz must be 30 or 60")
        if self.root_mode not in ("WorldRoot", "InPlace"):
            raise ValueError("root_mode must be WorldRoot or InPlace")
        if self.frame_rate_numerator < 1 or self.frame_rate_denominator < 1:
            raise ValueError("frame rate numerator and denominator must be positive")
        if self.ping_interval_seconds <= 0:
            raise ValueError("ping_interval_seconds must be positive")
        if self.timeout_seconds <= self.ping_interval_seconds:
            raise ValueError(
                "timeout_seconds must be greater than ping_interval_seconds"
            )
        if self.clock_sync_interval_seconds <= 0:
            raise ValueError("clock_sync_interval_seconds must be positive")


class BlenderControlClient:
    """Non-blocking Blender-side TCP control state machine.

    ``poll`` is designed for a Blender main-thread timer. It never waits on a
    socket and does not access Blender data, so session configuration may be
    captured before connecting and the transport can be tested independently.
    """

    def __init__(
        self,
        config: BlenderControlClientConfig,
        *,
        socket_factory: Callable[..., socket.socket] = socket.socket,
        monotonic: Callable[[], float] = time.monotonic,
        writable_probe: Callable[[socket.socket], bool] = _socket_is_writable,
    ) -> None:
        self._config = config
        self._socket_factory = socket_factory
        self._monotonic = monotonic
        self._writable_probe = writable_probe
        self._socket: socket.socket | None = None
        self._decoder = ControlFrameDecoder()
        self._outgoing = bytearray()
        self._state = ControlSessionState.DISCONNECTED
        self._phase = _HandshakePhase.NONE
        self._last_receive_at = 0.0
        self._last_ping_at = 0.0
        self._next_request_id = 1
        self._pending_ping_request_id: int | None = None
        self._last_error: str | None = None
        self._last_failure_retryable = False
        self._udp_host: str | None = None
        self._udp_port: int | None = None
        self._accepted_pose_hz: int | None = None
        self._human_trait: AcceptedHumanTrait | None = None
        self._target: dict[str, Any] | None = None
        self._pending_clock_sync: tuple[int, int] | None = None
        self._last_clock_sync_at = float("-inf")
        self._clock_sync_samples: list[tuple[int, float]] = []
        self._clock_sync_estimate: ClockSyncEstimate | None = None
        self._pose_stats: PoseStats | None = None
        self._accepted_capabilities: frozenset[str] = frozenset()

    @property
    def state(self) -> ControlSessionState:
        return self._state

    @property
    def session_id(self) -> int:
        return self._config.session_id

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def failure_is_retryable(self) -> bool:
        return self._last_failure_retryable

    @property
    def udp_endpoint(self) -> tuple[str, int] | None:
        if self._udp_host is None or self._udp_port is None:
            return None
        return self._udp_host, self._udp_port

    @property
    def accepted_pose_hz(self) -> int | None:
        return self._accepted_pose_hz

    @property
    def human_trait(self) -> AcceptedHumanTrait | None:
        return self._human_trait

    @property
    def target(self) -> Mapping[str, Any] | None:
        return self._target

    @property
    def clock_sync_estimate(self) -> ClockSyncEstimate | None:
        return self._clock_sync_estimate

    @property
    def pose_stats(self) -> PoseStats | None:
        return self._pose_stats

    @property
    def accepted_capabilities(self) -> frozenset[str]:
        return self._accepted_capabilities

    def connect(self) -> None:
        if self._state is not ControlSessionState.DISCONNECTED:
            raise RuntimeError("control client is already active")
        self._reset_negotiated_values()
        self._last_error = None
        self._last_failure_retryable = False
        control_socket = self._socket_factory(socket.AF_INET, socket.SOCK_STREAM)
        try:
            control_socket.setblocking(False)
            result = control_socket.connect_ex(
                (self._config.control_host, self._config.control_port)
            )
            if result not in _CONNECTING_ERRORS:
                raise OSError(result, "TCP control connect failed")
            self._socket = control_socket
            self._state = ControlSessionState.CONNECTING
            now = self._monotonic()
            self._last_receive_at = now
            self._last_ping_at = now
        except Exception:
            control_socket.close()
            raise

    def poll(self) -> ControlSessionState:
        control_socket = self._socket
        if control_socket is None:
            return self._state
        now = self._monotonic()
        try:
            if self._state is ControlSessionState.CONNECTING:
                if not self._writable_probe(control_socket):
                    if now - self._last_receive_at > self._config.timeout_seconds:
                        raise TimeoutError("TCP control connect timed out")
                    return self._state
                socket_error = control_socket.getsockopt(
                    socket.SOL_SOCKET, socket.SO_ERROR
                )
                if socket_error:
                    raise OSError(socket_error, "TCP control connect failed")
                self._finish_connect(now)

            self._flush_outgoing()
            self._receive_available()
            self._flush_outgoing()
            if now - self._last_receive_at > self._config.timeout_seconds:
                raise TimeoutError("TCP control heartbeat timed out")
            if (
                self._state
                in {
                    ControlSessionState.HANDSHAKING,
                    ControlSessionState.READY,
                    ControlSessionState.STREAMING,
                    ControlSessionState.PAUSED,
                }
                and now - self._last_ping_at >= self._config.ping_interval_seconds
                and self._pending_ping_request_id is None
            ):
                request_id = self._allocate_request_id()
                self._queue_message(
                    common_control_fields("ping", self._config.session_id, request_id)
                )
                self._pending_ping_request_id = request_id
                self._last_ping_at = now
                self._flush_outgoing()
            if (
                self._state
                in {
                    ControlSessionState.READY,
                    ControlSessionState.STREAMING,
                    ControlSessionState.PAUSED,
                }
                and now - self._last_clock_sync_at
                >= self._config.clock_sync_interval_seconds
                and self._pending_clock_sync is None
                and "clock-sync-v1" in self._accepted_capabilities
            ):
                self._queue_clock_sync(now)
                self._flush_outgoing()
        except (ControlProtocolError, OSError, TimeoutError) as error:
            self._fail(error)
        return self._state

    def start_streaming(self) -> None:
        if self._state not in {ControlSessionState.READY, ControlSessionState.PAUSED}:
            raise RuntimeError(
                "control session must be Ready or Paused before streaming"
            )
        self._queue_message(
            common_control_fields(
                "resume",
                self._config.session_id,
                self._allocate_request_id(),
            )
        )
        self._state = ControlSessionState.STREAMING

    def pause_streaming(self) -> None:
        if self._state is not ControlSessionState.STREAMING:
            raise RuntimeError("control session is not streaming")
        self._queue_message(
            common_control_fields(
                "pause",
                self._config.session_id,
                self._allocate_request_id(),
            )
        )
        self._state = ControlSessionState.PAUSED

    def close(self, *, send_goodbye: bool = True) -> None:
        control_socket = self._socket
        if control_socket is None:
            if self._state is not ControlSessionState.ERROR:
                self._state = ControlSessionState.DISCONNECTED
            return
        if send_goodbye and self._state not in {
            ControlSessionState.CONNECTING,
            ControlSessionState.ERROR,
        }:
            try:
                self._queue_message(
                    common_control_fields(
                        "goodbye",
                        self._config.session_id,
                        self._allocate_request_id(),
                    )
                )
                self._flush_outgoing()
            except OSError:
                pass
        self._close_socket()
        if self._state is not ControlSessionState.ERROR:
            self._state = ControlSessionState.DISCONNECTED

    def _finish_connect(self, now: float) -> None:
        self._state = ControlSessionState.HANDSHAKING
        self._phase = _HandshakePhase.WAIT_ACCEPT
        self._last_receive_at = now
        hello = common_control_fields("hello", self._config.session_id, 1)
        hello.update(
            {
                "addonVersion": self._config.addon_version,
                "blenderVersion": self._config.blender_version,
                "skeletonId": self._config.descriptor["skeletonId"],
                "skeletonHash": self._config.descriptor["skeletonHash"],
                "frameRate": {
                    "numerator": self._config.frame_rate_numerator,
                    "denominator": self._config.frame_rate_denominator,
                },
                "rootMode": self._config.root_mode,
                "requestedPoseHz": self._config.requested_pose_hz,
                "capabilities": list(_REQUESTED_CAPABILITIES),
            }
        )
        self._next_request_id = 2
        self._queue_message(hello)

    def _receive_available(self) -> None:
        if self._socket is None:
            return
        while True:
            try:
                data = self._socket.recv(8192)
            except BlockingIOError:
                return
            if not data:
                raise ConnectionError("TCP control peer closed the connection")
            receive_time = self._monotonic()
            for message in self._decoder.feed(data):
                self._last_receive_at = receive_time
                self._handle_message(message, receive_time=receive_time)

    def _handle_message(
        self,
        message: Mapping[str, Any],
        *,
        receive_time: float,
    ) -> None:
        message_type = validate_common_control_fields(
            message,
            expected_session_id=self._config.session_id,
        )
        if message_type == "ping":
            self._queue_message(
                common_control_fields(
                    "pong",
                    self._config.session_id,
                    message["requestId"],
                )
            )
            return
        if message_type == "pong":
            if message["requestId"] != self._pending_ping_request_id:
                raise ControlProtocolError(
                    "pong requestId does not match the pending ping"
                )
            self._pending_ping_request_id = None
            return
        if message_type == "clock_sync_pong":
            self._handle_clock_sync_pong(message, receive_time)
            return
        if message_type == "pose_stats":
            if "pose-stats-v1" not in self._accepted_capabilities:
                raise ControlProtocolError("pose_stats capability was not negotiated")
            self._handle_pose_stats(message)
            return
        if message_type == "error":
            code = message.get("code", "REMOTE_ERROR")
            detail = message.get("message", "Unity rejected the control session")
            raise ControlProtocolError(f"{code}: {detail}")
        if self._phase is _HandshakePhase.WAIT_ACCEPT and message_type == "accept":
            self._handle_accept(message)
            return
        if self._phase is _HandshakePhase.WAIT_READY and message_type == "ready":
            self._handle_ready(message)
            return
        raise ControlProtocolError(
            f"message {message_type!r} is invalid while {self._state.value}"
        )

    def _handle_accept(self, message: Mapping[str, Any]) -> None:
        if message["requestId"] != 1:
            raise ControlProtocolError("accept must acknowledge hello requestId 1")
        udp_host = message.get("udpHost")
        try:
            address = (
                ipaddress.ip_address(udp_host) if isinstance(udp_host, str) else None
            )
        except ValueError as error:
            raise ControlProtocolError("accept udpHost is invalid") from error
        if address is None or str(address) != "127.0.0.1":
            raise ControlProtocolError("accept udpHost must be 127.0.0.1")
        udp_port = message.get("udpPort")
        if type(udp_port) is not int or not 1 <= udp_port <= 0xFFFF:
            raise ControlProtocolError("accept udpPort must be in [1, 65535]")
        accepted_pose_hz = message.get("acceptedPoseHz")
        if accepted_pose_hz not in (30, 60):
            raise ControlProtocolError("accept acceptedPoseHz must be 30 or 60")
        human_trait = message.get("humanTrait")
        if not isinstance(human_trait, dict):
            raise ControlProtocolError("accept humanTrait must be an object")
        muscle_count = human_trait.get("muscleCount")
        if type(muscle_count) is not int or muscle_count <= 0:
            raise ControlProtocolError("accept humanTrait.muscleCount must be positive")
        muscle_names_hash = human_trait.get("muscleNamesHash")
        if (
            not isinstance(muscle_names_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", muscle_names_hash) is None
        ):
            raise ControlProtocolError(
                "accept humanTrait.muscleNamesHash must be 64 lowercase hex characters"
            )
        capabilities_value = message.get("capabilities", [])
        if not isinstance(capabilities_value, list) or any(
            not isinstance(capability, str) or not capability
            for capability in capabilities_value
        ):
            raise ControlProtocolError(
                "accept capabilities must be an array of strings"
            )
        if len(capabilities_value) != len(set(capabilities_value)):
            raise ControlProtocolError(
                "accept capabilities must not contain duplicates"
            )
        unsupported = set(capabilities_value).difference(_REQUESTED_CAPABILITIES)
        if unsupported:
            raise ControlProtocolError(
                "accept returned a capability Blender did not request"
            )
        if "one-way-pose-age-v1" in capabilities_value and not {
            "clock-sync-v1",
            "pose-stats-v1",
        }.issubset(capabilities_value):
            raise ControlProtocolError(
                "one-way-pose-age-v1 requires clock-sync-v1 and pose-stats-v1"
            )

        descriptor = dict(self._config.descriptor)
        descriptor.update(
            {
                "protocolMajor": 1,
                "protocolMinor": 0,
                "sessionId": self._config.session_id,
                "requestId": 2,
            }
        )
        self._udp_host = str(address)
        self._udp_port = udp_port
        self._accepted_pose_hz = accepted_pose_hz
        self._human_trait = AcceptedHumanTrait(muscle_count, muscle_names_hash)
        self._accepted_capabilities = frozenset(capabilities_value)
        self._phase = _HandshakePhase.WAIT_READY
        self._queue_message(descriptor)

    def _handle_ready(self, message: Mapping[str, Any]) -> None:
        if message["requestId"] != 2:
            raise ControlProtocolError("ready must acknowledge descriptor requestId 2")
        target = message.get("target")
        if not isinstance(target, dict):
            raise ControlProtocolError("ready target must be an object")
        if (
            target.get("avatarValid") is not True
            or target.get("avatarHuman") is not True
        ):
            raise ControlProtocolError(
                "ready target must contain a valid Humanoid Avatar"
            )
        human_scale = target.get("humanScale")
        if not isinstance(human_scale, (int, float)) or human_scale <= 0:
            raise ControlProtocolError("ready target humanScale must be positive")
        self._target = dict(target)
        self._phase = _HandshakePhase.NONE
        self._state = ControlSessionState.READY

    def _queue_clock_sync(self, now: float) -> None:
        request_id = self._allocate_request_id()
        t0_usec = self._monotonic_usec(now)
        message = common_control_fields(
            "clock_sync_ping",
            self._config.session_id,
            request_id,
        )
        message["t0BlenderSendUsec"] = t0_usec
        self._queue_message(message)
        self._pending_clock_sync = (request_id, t0_usec)
        self._last_clock_sync_at = now

    def _handle_clock_sync_pong(
        self,
        message: Mapping[str, Any],
        receive_time: float,
    ) -> None:
        pending = self._pending_clock_sync
        if pending is None or message["requestId"] != pending[0]:
            raise ControlProtocolError(
                "clock_sync_pong requestId does not match the pending clock sync"
            )
        t0_usec = self._nonnegative_int64(message, "t0BlenderSendUsec")
        t1_usec = self._nonnegative_int64(message, "t1UnityReceiveUsec")
        t2_usec = self._nonnegative_int64(message, "t2UnitySendUsec")
        if t0_usec != pending[1]:
            raise ControlProtocolError("clock_sync_pong t0 does not match its ping")
        if t2_usec < t1_usec:
            raise ControlProtocolError("clock_sync_pong t2 must not precede t1")

        t3_usec = self._monotonic_usec(receive_time)
        round_trip_time_usec = (t3_usec - t0_usec) - (t2_usec - t1_usec)
        offset_usec = ((t1_usec - t0_usec) + (t2_usec - t3_usec)) / 2.0
        self._pending_clock_sync = None
        if round_trip_time_usec < 0:
            self._invalidate_clock_sync(receive_time)
            return
        if (
            self._clock_sync_estimate is not None
            and abs(
                offset_usec - self._clock_sync_estimate.unity_minus_blender_offset_usec
            )
            > 5_000.0
        ):
            self._invalidate_clock_sync(receive_time)
            return

        self._clock_sync_samples.append((round_trip_time_usec, offset_usec))
        del self._clock_sync_samples[:-8]
        best_rtt, best_offset = min(
            self._clock_sync_samples, key=lambda value: value[0]
        )
        self._clock_sync_estimate = ClockSyncEstimate(
            unity_minus_blender_offset_usec=best_offset,
            round_trip_time_usec=best_rtt,
            sample_count=len(self._clock_sync_samples),
        )
        if "one-way-pose-age-v1" in self._accepted_capabilities:
            offset_message = common_control_fields(
                "clock_sync_offset",
                self._config.session_id,
                self._allocate_request_id(),
            )
            offset_message["unityMinusBlenderOffsetUsec"] = round(best_offset)
            self._queue_message(offset_message)

    def _invalidate_clock_sync(self, now: float) -> None:
        was_synchronized = self._clock_sync_estimate is not None
        self._clock_sync_samples.clear()
        self._clock_sync_estimate = None
        self._last_clock_sync_at = now - self._config.clock_sync_interval_seconds
        if was_synchronized and "one-way-pose-age-v1" in self._accepted_capabilities:
            self._queue_message(
                common_control_fields(
                    "clock_sync_reset",
                    self._config.session_id,
                    self._allocate_request_id(),
                )
            )

    def _handle_pose_stats(self, message: Mapping[str, Any]) -> None:
        if self._phase is not _HandshakePhase.NONE or self._state not in {
            ControlSessionState.READY,
            ControlSessionState.STREAMING,
            ControlSessionState.PAUSED,
        }:
            raise ControlProtocolError("pose_stats is invalid before ready")
        last_sequence = self._uint32(message, "lastSequence")
        received = self._nonnegative_int64(message, "received")
        dropped = self._nonnegative_int64(message, "droppedBySequence")
        rejected = self._nonnegative_int64(message, "rejected")
        receiver_overrun = self._nonnegative_int64(message, "receiverOverrun")
        has_p50 = "oneWayAgeUsecP50" in message
        has_p95 = "oneWayAgeUsecP95" in message
        if has_p50 and "one-way-pose-age-v1" not in self._accepted_capabilities:
            raise ControlProtocolError(
                "pose_stats one-way age capability was not negotiated"
            )
        if has_p50 != has_p95:
            raise ControlProtocolError(
                "pose_stats one-way age percentiles must be present together"
            )
        p50 = self._nonnegative_int64(message, "oneWayAgeUsecP50") if has_p50 else None
        p95 = self._nonnegative_int64(message, "oneWayAgeUsecP95") if has_p95 else None
        if p50 is not None and p95 is not None and p95 < p50:
            raise ControlProtocolError("pose_stats p95 must not be less than p50")
        previous = self._pose_stats
        if previous is not None and (
            received < previous.received
            or dropped < previous.dropped_by_sequence
            or rejected < previous.rejected
            or receiver_overrun < previous.receiver_overrun
        ):
            raise ControlProtocolError("pose_stats counters must not decrease")
        self._pose_stats = PoseStats(
            last_sequence=last_sequence,
            received=received,
            dropped_by_sequence=dropped,
            rejected=rejected,
            receiver_overrun=receiver_overrun,
            one_way_age_usec_p50=p50,
            one_way_age_usec_p95=p95,
        )

    @staticmethod
    def _nonnegative_int64(message: Mapping[str, Any], field: str) -> int:
        value = message.get(field)
        if type(value) is not int or not 0 <= value <= 0x7FFFFFFFFFFFFFFF:
            raise ControlProtocolError(f"{field} must be a non-negative int64")
        return value

    @staticmethod
    def _uint32(message: Mapping[str, Any], field: str) -> int:
        value = message.get(field)
        if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
            raise ControlProtocolError(f"{field} must be a uint32")
        return value

    @staticmethod
    def _monotonic_usec(monotonic_seconds: float) -> int:
        if not math.isfinite(monotonic_seconds) or monotonic_seconds < 0:
            raise ControlProtocolError(
                "monotonic clock must be finite and non-negative"
            )
        return int(monotonic_seconds * 1_000_000)

    def _queue_message(self, message: Mapping[str, Any]) -> None:
        self._outgoing.extend(encode_control_message(message))

    def _flush_outgoing(self) -> None:
        if self._socket is None:
            return
        while self._outgoing:
            try:
                sent = self._socket.send(self._outgoing)
            except BlockingIOError:
                return
            if sent <= 0:
                raise ConnectionError("TCP control send returned zero bytes")
            del self._outgoing[:sent]

    def _allocate_request_id(self) -> int:
        result = self._next_request_id
        self._next_request_id = (result + 1) & 0xFFFFFFFF
        return result

    def _fail(self, error: Exception) -> None:
        self._last_error = f"{type(error).__name__}: {error}"
        self._last_failure_retryable = is_retryable_control_error(error)
        self._state = ControlSessionState.ERROR
        self._phase = _HandshakePhase.NONE
        self._close_socket()

    def _close_socket(self) -> None:
        control_socket = self._socket
        self._socket = None
        self._outgoing.clear()
        self._decoder.reset()
        self._pending_ping_request_id = None
        self._pending_clock_sync = None
        if control_socket is not None:
            control_socket.close()

    def _reset_negotiated_values(self) -> None:
        self._udp_host = None
        self._udp_port = None
        self._accepted_pose_hz = None
        self._human_trait = None
        self._target = None
        self._pending_clock_sync = None
        self._last_clock_sync_at = float("-inf")
        self._clock_sync_samples.clear()
        self._clock_sync_estimate = None
        self._pose_stats = None
        self._accepted_capabilities = frozenset()

    def __enter__(self) -> BlenderControlClient:
        self.connect()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()
