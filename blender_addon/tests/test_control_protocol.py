import errno
import socket
import struct
import time
import unittest

from motion_bridge.canonical.descriptor import skeleton_hash_hex
from motion_bridge.live.control_client import (
    BlenderControlClient,
    BlenderControlClientConfig,
    ControlSessionState,
    ReconnectBackoff,
    is_retryable_control_error,
)
from motion_bridge.live.control_protocol import (
    ControlFrameDecoder,
    ControlProtocolError,
    encode_control_message,
)


SESSION_ID = 0x12345678


def _message(message_type: str, request_id: int, **fields: object) -> dict:
    result = {
        "type": message_type,
        "protocolMajor": 1,
        "protocolMinor": 0,
        "sessionId": SESSION_ID,
        "requestId": request_id,
    }
    result.update(fields)
    return result


def _descriptor() -> dict:
    descriptor = _message(
        "skeleton_descriptor",
        0,
        skeletonId="mb-canonical-humanoid-v1",
        skeletonHash="",
        coordinateSpace="unity-left-handed-y-up-z-forward",
        rootIndex=0,
        joints=[
            {
                "index": 0,
                "name": "Root",
                "humanBone": None,
                "parentIndex": -1,
                "jointPath": "",
                "required": True,
                "restLocalPosition": [0.0, 0.0, 0.0],
                "restLocalRotation": [0.0, 0.0, 0.0, 1.0],
                "restGlobalRotation": [0.0, 0.0, 0.0, 1.0],
            }
        ],
    )
    descriptor["skeletonHash"] = skeleton_hash_hex(descriptor)
    return descriptor


class _AlreadyConnectedSocket:
    def __init__(self, wrapped: socket.socket, connect_result: int = 0) -> None:
        self._wrapped = wrapped
        self._connect_result = connect_result
        self.getsockopt_calls = 0

    def setblocking(self, value: bool) -> None:
        self._wrapped.setblocking(value)

    def connect_ex(self, _address: tuple[str, int]) -> int:
        return self._connect_result

    def getsockopt(self, *_args: object) -> int:
        self.getsockopt_calls += 1
        return 0

    def send(self, data: bytes | bytearray) -> int:
        return self._wrapped.send(data)

    def recv(self, size: int) -> bytes:
        return self._wrapped.recv(size)

    def close(self) -> None:
        self._wrapped.close()


class ControlFrameCodecTests(unittest.TestCase):
    def test_fragmented_frame_is_emitted_only_when_complete(self) -> None:
        frame = encode_control_message(_message("ping", 7))
        decoder = ControlFrameDecoder()

        self.assertEqual((), decoder.feed(frame[:2]))
        self.assertEqual((), decoder.feed(frame[2:5]))
        self.assertEqual((), decoder.feed(frame[5:-1]))
        self.assertEqual((_message("ping", 7),), decoder.feed(frame[-1:]))
        self.assertEqual(0, decoder.buffered_bytes)

    def test_multiple_frames_in_one_read_preserve_order(self) -> None:
        first = _message("ping", 10)
        second = _message("pong", 10)
        decoder = ControlFrameDecoder()

        self.assertEqual(
            (first, second),
            decoder.feed(
                encode_control_message(first) + encode_control_message(second)
            ),
        )

    def test_invalid_size_utf8_json_and_root_fault_decoder(self) -> None:
        invalid_frames = (
            struct.pack("<I", 0),
            struct.pack("<I", 65_537),
            struct.pack("<I", 1) + b"\xff",
            struct.pack("<I", 1) + b"{",
            encode_control_message({"value": [1, 2, 3]}).replace(b"{", b"[", 1),
        )
        for frame in invalid_frames:
            with self.subTest(frame=frame[:8]):
                decoder = ControlFrameDecoder()
                with self.assertRaises(ControlProtocolError):
                    decoder.feed(frame)
                with self.assertRaises(ControlProtocolError):
                    decoder.feed(b"")
                decoder.reset()
                self.assertEqual((), decoder.feed(b""))

    def test_encoder_rejects_non_finite_and_oversized_json(self) -> None:
        with self.assertRaises(ControlProtocolError):
            encode_control_message({"value": float("nan")})
        with self.assertRaises(ControlProtocolError):
            encode_control_message({"value": "x" * 65_537})


class ReconnectBackoffTests(unittest.TestCase):
    def test_schedule_doubles_to_bound_and_reset_restores_initial_delay(self) -> None:
        backoff = ReconnectBackoff(initial_seconds=0.25, maximum_seconds=1.0)

        self.assertEqual(0.0, backoff.seconds_until_retry(10.0))
        self.assertEqual(0.25, backoff.schedule(10.0))
        self.assertEqual(0.25, backoff.seconds_until_retry(10.0))
        self.assertEqual(0.0, backoff.seconds_until_retry(10.25))
        self.assertEqual(0.5, backoff.schedule(10.25))
        self.assertEqual(1.0, backoff.schedule(10.75))
        self.assertEqual(1.0, backoff.schedule(11.75))
        self.assertEqual(4, backoff.attempt_count)

        backoff.reset()
        self.assertEqual(0, backoff.attempt_count)
        self.assertIsNone(backoff.next_retry_at)
        self.assertEqual(0.25, backoff.schedule(20.0))

    def test_invalid_schedule_values_are_rejected(self) -> None:
        for initial, maximum in ((0.0, 1.0), (1.0, 0.5), (float("nan"), 1.0)):
            with self.subTest(initial=initial, maximum=maximum):
                with self.assertRaises(ValueError):
                    ReconnectBackoff(initial, maximum)
        backoff = ReconnectBackoff()
        for now in (-1.0, float("nan"), float("inf")):
            with self.subTest(now=now):
                with self.assertRaises(ValueError):
                    backoff.schedule(now)

    def test_only_transport_failures_are_retryable(self) -> None:
        self.assertTrue(is_retryable_control_error(ConnectionError("closed")))
        self.assertTrue(is_retryable_control_error(TimeoutError("timed out")))
        self.assertFalse(is_retryable_control_error(ControlProtocolError("invalid")))
        self.assertFalse(is_retryable_control_error(RuntimeError("invalid state")))


class BlenderControlClientTests(unittest.TestCase):
    def setUp(self) -> None:
        client_socket, self.server_socket = socket.socketpair()
        self.server_socket.setblocking(False)
        self.now = [100.0]
        wrapped = _AlreadyConnectedSocket(client_socket)
        self.wrapped_socket = wrapped
        config = BlenderControlClientConfig(
            session_id=SESSION_ID,
            descriptor=_descriptor(),
            ping_interval_seconds=1.0,
            timeout_seconds=3.0,
        )
        self.client = BlenderControlClient(
            config,
            socket_factory=lambda *_args: wrapped,
            monotonic=lambda: self.now[0],
            writable_probe=lambda _socket: True,
        )
        self.server_decoder = ControlFrameDecoder()

    def tearDown(self) -> None:
        self.client.close(send_goodbye=False)
        self.wrapped_socket.close()
        self.server_socket.close()

    def _receive_from_client(self) -> tuple[dict, ...]:
        result = []
        while True:
            try:
                data = self.server_socket.recv(65_540)
            except BlockingIOError:
                break
            if not data:
                break
            result.extend(self.server_decoder.feed(data))
        return tuple(result)

    def _send_to_client(self, *messages: dict) -> None:
        payload = b"".join(encode_control_message(message) for message in messages)
        self.server_socket.sendall(payload)

    def _complete_handshake(self, *, pose_age: bool = True) -> dict:
        self.client.connect()
        self.client.poll()
        self._receive_from_client()
        self._send_to_client(
            _message(
                "accept",
                1,
                udpHost="127.0.0.1",
                udpPort=50_321,
                acceptedPoseHz=60,
                humanTrait={"muscleCount": 95, "muscleNamesHash": "2" * 64},
                capabilities=[
                    "global-rest-delta-v1",
                    "clock-sync-v1",
                    "pose-stats-v1",
                ]
                + (["one-way-pose-age-v1"] if pose_age else []),
            )
        )
        self.client.poll()
        self._receive_from_client()
        self._send_to_client(
            _message(
                "ready",
                2,
                target={
                    "name": "Character_A",
                    "avatarValid": True,
                    "avatarHuman": True,
                    "humanScale": 1.0,
                },
            )
        )
        self.assertEqual(ControlSessionState.READY, self.client.poll())
        return self._receive_from_client()[0]

    def test_handshake_stream_pause_ping_and_clean_stop(self) -> None:
        self.assertEqual(SESSION_ID, self.client.session_id)
        self.client.connect()
        self.assertEqual(ControlSessionState.CONNECTING, self.client.state)
        self.assertEqual(ControlSessionState.HANDSHAKING, self.client.poll())
        hello = self._receive_from_client()
        self.assertEqual(("hello",), tuple(message["type"] for message in hello))
        self.assertEqual(_descriptor()["skeletonHash"], hello[0]["skeletonHash"])
        self.assertNotIn("contacts-v1", hello[0]["capabilities"])

        accept = _message(
            "accept",
            1,
            unityVersion="6000.2.15f1",
            packageVersion="0.1.0",
            udpHost="127.0.0.1",
            udpPort=50_321,
            acceptedPoseHz=60,
            humanTrait={"muscleCount": 95, "muscleNamesHash": "2" * 64},
            capabilities=[
                "global-rest-delta-v1",
                "clock-sync-v1",
                "pose-stats-v1",
                "one-way-pose-age-v1",
            ],
        )
        self._send_to_client(accept)
        self.client.poll()
        descriptor = self._receive_from_client()
        self.assertEqual(("skeleton_descriptor",), tuple(m["type"] for m in descriptor))
        self.assertEqual(2, descriptor[0]["requestId"])
        self.assertEqual(("127.0.0.1", 50_321), self.client.udp_endpoint)
        self.assertEqual(95, self.client.human_trait.muscle_count)
        self.assertEqual("2" * 64, self.client.human_trait.muscle_names_hash)

        ready = _message(
            "ready",
            2,
            target={
                "name": "Character_A",
                "avatarValid": True,
                "avatarHuman": True,
                "humanScale": 1.03,
            },
        )
        self._send_to_client(ready)
        self.assertEqual(ControlSessionState.READY, self.client.poll())
        clock_ping = self._receive_from_client()[0]
        self.assertEqual("clock_sync_ping", clock_ping["type"])
        t0_usec = clock_ping["t0BlenderSendUsec"]
        self.now[0] += 0.004
        self._send_to_client(
            _message(
                "clock_sync_pong",
                clock_ping["requestId"],
                t0BlenderSendUsec=t0_usec,
                t1UnityReceiveUsec=t0_usec + 11_000,
                t2UnitySendUsec=t0_usec + 11_500,
            )
        )
        self.client.poll()
        self.assertEqual(3_500, self.client.clock_sync_estimate.round_trip_time_usec)
        self.assertEqual(
            9_250.0,
            self.client.clock_sync_estimate.unity_minus_blender_offset_usec,
        )
        offset = self._receive_from_client()[0]
        self.assertEqual("clock_sync_offset", offset["type"])
        self.assertEqual(9_250, offset["unityMinusBlenderOffsetUsec"])

        self.client.start_streaming()
        self.client.poll()
        self.assertEqual("resume", self._receive_from_client()[0]["type"])
        self.assertEqual(ControlSessionState.STREAMING, self.client.state)

        self.now[0] += 1.1
        self.client.poll()
        ping = self._receive_from_client()[0]
        self.assertEqual("ping", ping["type"])
        self._send_to_client(_message("pong", ping["requestId"]))
        self.client.poll()

        self._send_to_client(_message("ping", 99))
        self.client.poll()
        self.assertEqual("pong", self._receive_from_client()[0]["type"])

        self.client.pause_streaming()
        self.client.poll()
        self.assertEqual("pause", self._receive_from_client()[0]["type"])
        self.client.close()
        self.assertEqual("goodbye", self._receive_from_client()[0]["type"])
        self.assertEqual(ControlSessionState.DISCONNECTED, self.client.state)

    def test_accept_rejects_unrequested_contacts_capability(self) -> None:
        self.client.connect()
        self.client.poll()
        hello = self._receive_from_client()[0]
        self.assertNotIn("contacts-v1", hello["capabilities"])

        self._send_to_client(
            _message(
                "accept",
                1,
                udpHost="127.0.0.1",
                udpPort=50_321,
                acceptedPoseHz=60,
                humanTrait={"muscleCount": 95, "muscleNamesHash": "2" * 64},
                capabilities=["contacts-v1", "global-rest-delta-v1"],
            )
        )

        self.assertEqual(ControlSessionState.ERROR, self.client.poll())
        self.assertIn("did not request", self.client.last_error)
        self.assertEqual(frozenset(), self.client.accepted_capabilities)
        self.assertIsNone(self.client.udp_endpoint)

    def test_legacy_accept_does_not_enable_telemetry(self) -> None:
        self.client.connect()
        self.client.poll()
        self._receive_from_client()
        self._send_to_client(
            _message(
                "accept",
                1,
                udpHost="127.0.0.1",
                udpPort=50_321,
                acceptedPoseHz=60,
                humanTrait={"muscleCount": 95, "muscleNamesHash": "2" * 64},
                capabilities=["global-rest-delta-v1"],
            )
        )
        self.client.poll()
        self._receive_from_client()
        self._send_to_client(
            _message(
                "ready",
                2,
                target={
                    "name": "Character_A",
                    "avatarValid": True,
                    "avatarHuman": True,
                    "humanScale": 1.0,
                },
            )
        )
        self.assertEqual(ControlSessionState.READY, self.client.poll())
        self.assertEqual(
            frozenset({"global-rest-delta-v1"}),
            self.client.accepted_capabilities,
        )
        self.assertEqual((), self._receive_from_client())

        self._send_to_client(
            _message(
                "pose_stats",
                80,
                lastSequence=0,
                received=0,
                droppedBySequence=0,
                rejected=0,
                receiverOverrun=0,
            )
        )
        self.assertEqual(ControlSessionState.ERROR, self.client.poll())
        self.assertIn("capability was not negotiated", self.client.last_error)

    def test_clock_sync_uses_minimum_rtt_and_retries_offset_jump(self) -> None:
        first_ping = self._complete_handshake()
        first_t0 = first_ping["t0BlenderSendUsec"]
        self.now[0] += 0.004
        self._send_to_client(
            _message(
                "clock_sync_pong",
                first_ping["requestId"],
                t0BlenderSendUsec=first_t0,
                t1UnityReceiveUsec=first_t0 + 12_000,
                t2UnitySendUsec=first_t0 + 12_000,
            )
        )
        self.client.poll()
        self.assertEqual(4_000, self.client.clock_sync_estimate.round_trip_time_usec)
        self.assertEqual(
            10_000.0, self.client.clock_sync_estimate.unity_minus_blender_offset_usec
        )

        self.now[0] += 5.1
        self._send_to_client(_message("ping", 90))
        self.client.poll()
        messages = self._receive_from_client()
        first_offset = next(
            message for message in messages if message["type"] == "clock_sync_offset"
        )
        self.assertEqual(10_000, first_offset["unityMinusBlenderOffsetUsec"])
        second_ping = next(
            message for message in messages if message["type"] == "clock_sync_ping"
        )
        second_t0 = second_ping["t0BlenderSendUsec"]
        self.now[0] += 0.002
        self._send_to_client(
            _message(
                "clock_sync_pong",
                second_ping["requestId"],
                t0BlenderSendUsec=second_t0,
                t1UnityReceiveUsec=second_t0 + 12_000,
                t2UnitySendUsec=second_t0 + 12_000,
            )
        )
        self.client.poll()
        estimate = self.client.clock_sync_estimate
        self.assertEqual(2, estimate.sample_count)
        self.assertEqual(2_000, estimate.round_trip_time_usec)
        self.assertEqual(11_000.0, estimate.unity_minus_blender_offset_usec)

        self.now[0] += 5.1
        self._send_to_client(_message("ping", 91))
        self.client.poll()
        messages = self._receive_from_client()
        second_offset = next(
            message for message in messages if message["type"] == "clock_sync_offset"
        )
        self.assertEqual(11_000, second_offset["unityMinusBlenderOffsetUsec"])
        jump_ping = next(
            message for message in messages if message["type"] == "clock_sync_ping"
        )
        jump_t0 = jump_ping["t0BlenderSendUsec"]
        self.now[0] += 0.002
        self._send_to_client(
            _message(
                "clock_sync_pong",
                jump_ping["requestId"],
                t0BlenderSendUsec=jump_t0,
                t1UnityReceiveUsec=jump_t0 + 32_000,
                t2UnitySendUsec=jump_t0 + 32_000,
            )
        )
        self.client.poll()
        self.assertIsNone(self.client.clock_sync_estimate)
        recovery_messages = self._receive_from_client()
        self.assertEqual(
            1,
            sum(message["type"] == "clock_sync_reset" for message in recovery_messages),
        )
        self.assertFalse(
            any(message["type"] == "clock_sync_offset" for message in recovery_messages)
        )
        retry = next(
            message
            for message in recovery_messages
            if message["type"] == "clock_sync_ping"
        )
        self.assertNotEqual(jump_ping["requestId"], retry["requestId"])

    def test_clock_offset_message_requires_new_optional_capability(self) -> None:
        ping = self._complete_handshake(pose_age=False)
        t0_usec = ping["t0BlenderSendUsec"]
        self.now[0] += 0.002
        self._send_to_client(
            _message(
                "clock_sync_pong",
                ping["requestId"],
                t0BlenderSendUsec=t0_usec,
                t1UnityReceiveUsec=t0_usec + 3_000,
                t2UnitySendUsec=t0_usec + 3_000,
            )
        )
        self.client.poll()
        self.assertIsNotNone(self.client.clock_sync_estimate)
        self.assertFalse(
            any(
                message["type"] == "clock_sync_offset"
                for message in self._receive_from_client()
            )
        )

    def test_pose_stats_are_validated_and_published(self) -> None:
        self._complete_handshake()
        self.client.start_streaming()
        self.client.poll()
        self._receive_from_client()
        stats = _message(
            "pose_stats",
            80,
            lastSequence=1_200,
            received=1_195,
            droppedBySequence=5,
            rejected=2,
            receiverOverrun=1,
            oneWayAgeUsecP50=6_200,
            oneWayAgeUsecP95=9_400,
        )
        self._send_to_client(stats)
        self.client.poll()
        self.assertEqual(1_200, self.client.pose_stats.last_sequence)
        self.assertEqual(5, self.client.pose_stats.dropped_by_sequence)
        self.assertEqual(9_400, self.client.pose_stats.one_way_age_usec_p95)

        self._send_to_client(
            _message(
                "pose_stats",
                81,
                lastSequence=1_201,
                received=1_194,
                droppedBySequence=5,
                rejected=2,
                receiverOverrun=1,
            )
        )
        self.assertEqual(ControlSessionState.ERROR, self.client.poll())
        self.assertIn("must not decrease", self.client.last_error)

    def test_pose_stats_rejects_partial_one_way_age(self) -> None:
        self._complete_handshake()
        self._send_to_client(
            _message(
                "pose_stats",
                80,
                lastSequence=0,
                received=0,
                droppedBySequence=0,
                rejected=0,
                receiverOverrun=0,
                oneWayAgeUsecP50=1_000,
            )
        )
        self.assertEqual(ControlSessionState.ERROR, self.client.poll())
        self.assertIn("must be present together", self.client.last_error)

    def test_pose_stats_rejects_negative_one_way_age(self) -> None:
        self._complete_handshake()
        self._send_to_client(
            _message(
                "pose_stats",
                80,
                lastSequence=0,
                received=0,
                droppedBySequence=0,
                rejected=0,
                receiverOverrun=0,
                oneWayAgeUsecP50=-1,
                oneWayAgeUsecP95=1_000,
            )
        )
        self.assertEqual(ControlSessionState.ERROR, self.client.poll())
        self.assertIn("non-negative int64", self.client.last_error)

    def test_clock_sync_rejects_malformed_timestamp_order(self) -> None:
        ping = self._complete_handshake()
        t0_usec = ping["t0BlenderSendUsec"]
        self._send_to_client(
            _message(
                "clock_sync_pong",
                ping["requestId"],
                t0BlenderSendUsec=t0_usec,
                t1UnityReceiveUsec=t0_usec + 2_000,
                t2UnitySendUsec=t0_usec + 1_000,
            )
        )
        self.assertEqual(ControlSessionState.ERROR, self.client.poll())
        self.assertIn("t2 must not precede t1", self.client.last_error)

    def test_invalid_state_message_transitions_to_error(self) -> None:
        self.client.connect()
        self.client.poll()
        self._receive_from_client()
        self._send_to_client(_message("ready", 2, target={}))

        self.assertEqual(ControlSessionState.ERROR, self.client.poll())
        self.assertIn("invalid while Handshaking", self.client.last_error)
        self.assertFalse(self.client.failure_is_retryable)

    def test_clean_peer_close_is_retryable(self) -> None:
        self.client.connect()
        self.client.poll()
        self._receive_from_client()
        self.server_socket.close()

        self.assertEqual(ControlSessionState.ERROR, self.client.poll())
        self.assertIn("peer closed", self.client.last_error)
        self.assertTrue(self.client.failure_is_retryable)

    def test_missing_heartbeat_transitions_to_error_without_blocking(self) -> None:
        self.client.connect()
        self.client.poll()
        self._receive_from_client()
        self.now[0] += 3.1

        self.assertEqual(ControlSessionState.ERROR, self.client.poll())
        self.assertIn("timed out", self.client.last_error)
        self.assertTrue(self.client.failure_is_retryable)

    def test_config_rejects_non_loopback(self) -> None:
        with self.assertRaises(ValueError):
            BlenderControlClientConfig(
                session_id=SESSION_ID,
                descriptor=_descriptor(),
                control_host="0.0.0.0",
            )

        with self.assertRaisesRegex(ValueError, "localhost or 127.0.0.1"):
            BlenderControlClientConfig(
                session_id=SESSION_ID,
                descriptor=_descriptor(),
                control_host="127.0.0.2",
            )

    def test_connect_waits_for_writable_before_so_error_and_hello(self) -> None:
        client_socket, server_socket = socket.socketpair()
        server_socket.setblocking(False)
        ready = [False]
        wrapped = _AlreadyConnectedSocket(client_socket, errno.EINPROGRESS)
        client = BlenderControlClient(
            BlenderControlClientConfig(
                session_id=SESSION_ID,
                descriptor=_descriptor(),
                ping_interval_seconds=1.0,
                timeout_seconds=3.0,
            ),
            socket_factory=lambda *_args: wrapped,
            monotonic=lambda: self.now[0],
            writable_probe=lambda _socket: ready[0],
        )
        try:
            client.connect()
            self.assertEqual(ControlSessionState.CONNECTING, client.poll())
            self.assertEqual(0, wrapped.getsockopt_calls)
            with self.assertRaises(BlockingIOError):
                server_socket.recv(1)

            ready[0] = True
            self.assertEqual(ControlSessionState.HANDSHAKING, client.poll())
            self.assertEqual(1, wrapped.getsockopt_calls)
            decoder = ControlFrameDecoder()
            messages = decoder.feed(server_socket.recv(65_540))
            self.assertEqual(("hello",), tuple(message["type"] for message in messages))
        finally:
            client.close(send_goodbye=False)
            server_socket.close()

    def test_real_nonblocking_loopback_connect_emits_hello_after_writable(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
        except PermissionError:
            listener.close()
            self.skipTest("sandbox does not permit loopback socket binding")
        listener.listen(1)
        listener.settimeout(1.0)
        client = BlenderControlClient(
            BlenderControlClientConfig(
                session_id=SESSION_ID,
                descriptor=_descriptor(),
                control_port=listener.getsockname()[1],
                ping_interval_seconds=1.0,
                timeout_seconds=3.0,
            )
        )
        peer = None
        try:
            client.connect()
            self.assertEqual(ControlSessionState.CONNECTING, client.state)
            deadline = time.monotonic() + 1.0
            while (
                client.state is ControlSessionState.CONNECTING
                and time.monotonic() < deadline
            ):
                client.poll()
            self.assertEqual(ControlSessionState.HANDSHAKING, client.state)

            peer, _ = listener.accept()
            peer.settimeout(1.0)
            decoder = ControlFrameDecoder()
            messages = decoder.feed(peer.recv(65_540))
            self.assertEqual(("hello",), tuple(message["type"] for message in messages))
        finally:
            client.close(send_goodbye=False)
            if peer is not None:
                peer.close()
            listener.close()

    def test_accept_rejects_invalid_human_trait_without_publishing(self) -> None:
        invalid_values = (
            None,
            [],
            {"muscleCount": 0, "muscleNamesHash": "2" * 64},
            {"muscleCount": True, "muscleNamesHash": "2" * 64},
            {"muscleCount": 95, "muscleNamesHash": "2" * 63},
            {"muscleCount": 95, "muscleNamesHash": "A" * 64},
        )
        for human_trait in invalid_values:
            with self.subTest(human_trait=human_trait):
                client_socket, server_socket = socket.socketpair()
                server_socket.setblocking(False)
                wrapped = _AlreadyConnectedSocket(client_socket)
                client = BlenderControlClient(
                    BlenderControlClientConfig(
                        session_id=SESSION_ID,
                        descriptor=_descriptor(),
                        ping_interval_seconds=1.0,
                        timeout_seconds=3.0,
                    ),
                    socket_factory=lambda *_args, wrapped=wrapped: wrapped,
                    monotonic=lambda: self.now[0],
                    writable_probe=lambda _socket: True,
                )
                try:
                    client.connect()
                    client.poll()
                    server_socket.recv(65_540)
                    server_socket.sendall(
                        encode_control_message(
                            _message(
                                "accept",
                                1,
                                udpHost="127.0.0.1",
                                udpPort=50_321,
                                acceptedPoseHz=60,
                                humanTrait=human_trait,
                            )
                        )
                    )
                    self.assertEqual(ControlSessionState.ERROR, client.poll())
                    self.assertIsNone(client.human_trait)
                    self.assertIsNone(client.udp_endpoint)
                finally:
                    client.close(send_goodbye=False)
                    server_socket.close()


if __name__ == "__main__":
    unittest.main()
