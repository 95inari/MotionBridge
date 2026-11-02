from __future__ import annotations

from collections.abc import Mapping
import json
import struct
from typing import Any

from .protocol_v1 import (
    CONTROL_LENGTH_PREFIX_BYTES,
    MAX_CONTROL_PAYLOAD_BYTES,
    PROTOCOL_MAJOR,
    PROTOCOL_MINOR,
)


CONTROL_LENGTH_PREFIX = struct.Struct("<I")

if CONTROL_LENGTH_PREFIX.size != CONTROL_LENGTH_PREFIX_BYTES:
    raise RuntimeError("TCP control length prefix constant does not match its codec")


class ControlProtocolError(ValueError):
    """A malformed or unsupported TCP control frame."""


def encode_control_message(message: Mapping[str, Any]) -> bytes:
    """Encode one UTF-8 JSON control object with the v1 uint32 prefix."""

    if not isinstance(message, Mapping):
        raise TypeError("control message must be a mapping")
    try:
        payload = json.dumps(
            dict(message),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ControlProtocolError(f"control message is not valid JSON: {error}") from error
    if not payload or len(payload) > MAX_CONTROL_PAYLOAD_BYTES:
        raise ControlProtocolError(
            f"control payload must contain 1..{MAX_CONTROL_PAYLOAD_BYTES} bytes"
        )
    return CONTROL_LENGTH_PREFIX.pack(len(payload)) + payload


class ControlFrameDecoder:
    """Incremental decoder for fragmented and coalesced TCP control frames."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._expected_payload_bytes: int | None = None
        self._faulted = False

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes | bytearray | memoryview) -> tuple[dict[str, Any], ...]:
        if self._faulted:
            raise ControlProtocolError("decoder is faulted; call reset before reuse")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes-like")
        if data:
            self._buffer.extend(data)

        messages: list[dict[str, Any]] = []
        try:
            while True:
                if self._expected_payload_bytes is None:
                    if len(self._buffer) < CONTROL_LENGTH_PREFIX.size:
                        break
                    payload_bytes = CONTROL_LENGTH_PREFIX.unpack_from(self._buffer)[0]
                    del self._buffer[: CONTROL_LENGTH_PREFIX.size]
                    if payload_bytes < 1 or payload_bytes > MAX_CONTROL_PAYLOAD_BYTES:
                        raise ControlProtocolError(
                            "control payload length is outside the 1..65536 byte limit"
                        )
                    self._expected_payload_bytes = payload_bytes

                if len(self._buffer) < self._expected_payload_bytes:
                    break
                payload_bytes = self._expected_payload_bytes
                payload = bytes(self._buffer[:payload_bytes])
                del self._buffer[:payload_bytes]
                self._expected_payload_bytes = None
                try:
                    value = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ControlProtocolError(
                        f"control payload is not a UTF-8 JSON object: {error}"
                    ) from error
                if not isinstance(value, dict):
                    raise ControlProtocolError("control JSON root must be an object")
                messages.append(value)
        except ControlProtocolError:
            self._faulted = True
            self._buffer.clear()
            self._expected_payload_bytes = None
            raise
        return tuple(messages)

    def reset(self) -> None:
        self._buffer.clear()
        self._expected_payload_bytes = None
        self._faulted = False


def validate_common_control_fields(
    message: Mapping[str, Any],
    *,
    expected_session_id: int,
) -> str:
    """Validate the v1 envelope and return its message type."""

    message_type = message.get("type")
    if not isinstance(message_type, str) or not message_type:
        raise ControlProtocolError("control message type must be a non-empty string")
    if message.get("protocolMajor") != PROTOCOL_MAJOR:
        raise ControlProtocolError("control protocol major mismatch")
    minor = message.get("protocolMinor")
    if type(minor) is not int or not 0 <= minor <= PROTOCOL_MINOR:
        raise ControlProtocolError("unsupported control protocol minor")
    session_id = message.get("sessionId")
    if type(session_id) is not int or session_id != expected_session_id:
        raise ControlProtocolError("control session ID mismatch")
    request_id = message.get("requestId")
    if type(request_id) is not int or not 0 <= request_id <= 0xFFFFFFFF:
        raise ControlProtocolError("control requestId must be a uint32")
    return message_type


def common_control_fields(
    message_type: str,
    session_id: int,
    request_id: int,
) -> dict[str, Any]:
    if not message_type:
        raise ValueError("message_type must not be empty")
    if type(session_id) is not int or not 0 <= session_id <= 0xFFFFFFFF:
        raise ValueError("session_id must be a uint32")
    if type(request_id) is not int or not 0 <= request_id <= 0xFFFFFFFF:
        raise ValueError("request_id must be a uint32")
    return {
        "type": message_type,
        "protocolMajor": PROTOCOL_MAJOR,
        "protocolMinor": PROTOCOL_MINOR,
        "sessionId": session_id,
        "requestId": request_id,
    }
