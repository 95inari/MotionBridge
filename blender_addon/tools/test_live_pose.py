from __future__ import annotations

import json
import math
from pathlib import Path
import socket
import sys

import bpy
from mathutils import Matrix, Vector


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))

from motion_bridge.canonical.rig_spec import (  # noqa: E402
    JOINTS,
    SKELETON_HASH,
    SKELETON_ID,
)
from motion_bridge import addon  # noqa: E402
from motion_bridge.live.pose_session import LivePoseSession  # noqa: E402
from motion_bridge.live.protocol_decode_v1 import (  # noqa: E402
    PosePacketFlags,
    decode_pose_packet,
)


class _WouldBlockSocket:
    def setblocking(self, _blocking: bool) -> None:
        pass

    def sendto(self, _packet: memoryview, _destination: tuple[str, int]) -> int:
        raise BlockingIOError("synthetic full UDP send buffer")


class _FakeControlClient:
    def __init__(
        self,
        state,
        *,
        retryable: bool = False,
        last_error: str | None = None,
        endpoint: tuple[str, int] | None = None,
        session_id: int = 0x12345678,
        send_hz: int = 60,
    ) -> None:
        self.state = state
        self.failure_is_retryable = retryable
        self.last_error = last_error
        self.udp_endpoint = endpoint
        self.session_id = session_id
        self.accepted_pose_hz = send_hz
        self.closed_with_goodbye: bool | None = None

    def poll(self):
        return self.state

    def start_streaming(self) -> None:
        self.state = addon.ControlSessionState.STREAMING

    def close(self, *, send_goodbye: bool = True) -> None:
        self.closed_with_goodbye = send_goodbye


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)


def _roll_reference(head: Vector, tail: Vector) -> Vector:
    direction = (tail - head).normalized()
    forward = Vector((0.0, -1.0, 0.0))
    if abs(direction.dot(forward)) > 0.98:
        return Vector((0.0, 0.0, 1.0))
    return forward


def _create_canonical_armature() -> bpy.types.Object:
    _clear_scene()
    armature = bpy.data.armatures.new("MB_Live_Pose_Test_Armature")
    obj = bpy.data.objects.new("MB_Live_Pose_Test", armature)
    bpy.context.scene.collection.objects.link(obj)
    obj["motion_bridge_skeleton_id"] = SKELETON_ID
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")

    edit_bones: dict[str, bpy.types.EditBone] = {}
    for joint in JOINTS[1:]:
        if joint.head is None or joint.tail is None:
            raise AssertionError(f"Physical joint has no endpoints: {joint.name}")
        bone = armature.edit_bones.new(joint.name)
        bone.head = joint.head
        bone.tail = joint.tail
        if joint.parent_index > 0:
            parent = edit_bones[JOINTS[joint.parent_index].name]
            bone.parent = parent
            bone.use_connect = (bone.head - parent.tail).length <= 1.0e-7
        bone.align_roll(_roll_reference(bone.head, bone.tail))
        edit_bones[joint.name] = bone

    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.context.view_layer.update()
    return obj


def _received_packet(receiver: socket.socket):
    datagram, _sender = receiver.recvfrom(2048)
    return decode_pose_packet(datagram)


def _assert_vector(
    actual: tuple[float, ...], expected: tuple[float, ...], tolerance: float = 1e-5
) -> None:
    if len(actual) != len(expected) or any(
        abs(left - right) > tolerance for left, right in zip(actual, expected)
    ):
        raise AssertionError(f"Vector mismatch: actual={actual}, expected={expected}")


def _assert_quaternion(
    actual: tuple[float, float, float, float],
    expected: tuple[float, float, float, float],
    tolerance: float = 1e-5,
) -> None:
    dot = abs(sum(left * right for left, right in zip(actual, expected)))
    if abs(1.0 - dot) > tolerance:
        raise AssertionError(f"Quaternion mismatch: actual={actual}, expected={expected}")


def _set_global_delta(
    obj: bpy.types.Object,
    bone_name: str,
    delta: Matrix,
    translation_delta: Vector | None = None,
) -> None:
    rest = obj.data.bones[bone_name].matrix_local.copy()
    pose = delta @ rest
    pose.translation = rest.translation + (translation_delta or Vector((0.0, 0.0, 0.0)))
    obj.pose.bones[bone_name].matrix = pose


def main() -> None:
    addon._remove_live_handlers()
    addon._install_live_handlers()
    addon._install_live_handlers()
    for handlers, callback in addon._LIVE_DIRTY_HANDLERS:
        if handlers.count(callback) != 1:
            raise AssertionError("Live dirty handler registration is not idempotent")
    addon._remove_live_handlers()
    for handlers, callback in addon._LIVE_DIRTY_HANDLERS:
        if callback in handlers:
            raise AssertionError("Live dirty handler was not removed")

    obj = _create_canonical_armature()
    scene = bpy.context.scene
    scene.frame_set(1)
    descriptor = addon._live_descriptor(obj)
    if descriptor["skeletonHash"] != SKELETON_HASH:
        raise AssertionError("Canonical fixture does not match the versioned hash")

    bpy.ops.object.mode_set(mode="EDIT")
    obj.data.edit_bones["LeftUpperArm"].roll += math.radians(10.0)
    bpy.ops.object.mode_set(mode="OBJECT")
    try:
        addon._live_descriptor(obj)
    except ValueError as error:
        if "配布版と一致しません" not in str(error):
            raise
    else:
        raise AssertionError("Live descriptor accepted a changed Canonical bone roll")
    bpy.ops.object.mode_set(mode="EDIT")
    left_upper_arm = obj.data.edit_bones["LeftUpperArm"]
    left_upper_arm.align_roll(_roll_reference(left_upper_arm.head, left_upper_arm.tail))
    bpy.ops.object.mode_set(mode="OBJECT")
    if addon._live_descriptor(obj)["skeletonHash"] != SKELETON_HASH:
        raise AssertionError("Canonical fixture did not recover its versioned hash")

    noncanonical = bpy.data.objects.new("MB_Live_Pose_Noncanonical", None)
    scene.collection.objects.link(noncanonical)
    bpy.context.view_layer.objects.active = noncanonical
    if addon._resolve_canonical_armature(bpy.context) is not obj:
        raise AssertionError("Unique Canonical armature was not resolved from the scene")
    second_data = bpy.data.armatures.new("MB_Live_Pose_Second_Armature")
    second = bpy.data.objects.new("MB_Live_Pose_Second", second_data)
    second["motion_bridge_skeleton_id"] = SKELETON_ID
    scene.collection.objects.link(second)
    if addon._resolve_canonical_armature(bpy.context) is not None:
        raise AssertionError("Ambiguous Canonical armatures were resolved implicitly")
    bpy.data.objects.remove(second, do_unlink=True)
    bpy.data.armatures.remove(second_data)
    bpy.context.view_layer.objects.active = obj

    try:
        LivePoseSession(
            obj,
            "192.0.2.1",
            39872,
            session_id=1,
            root_mode="WORLD_ROOT",
        )
    except ValueError as error:
        if "loopback" not in str(error):
            raise
    else:
        raise AssertionError("A non-loopback pose destination was accepted")

    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(2.0)
    host, port = receiver.getsockname()

    reconnect_config = addon.BlenderControlClientConfig(
        session_id=0x12345678,
        descriptor=addon.descriptor_from_armature(obj),
    )
    addon._pending_control_config = reconnect_config
    addon._pending_live_armature = obj
    addon._pending_root_mode = "WORLD_ROOT"
    addon._live_reconnect_backoff.reset()
    transport_failure = _FakeControlClient(
        addon.ControlSessionState.ERROR,
        retryable=True,
        last_error="ConnectionError: synthetic Unity reload",
    )
    addon._control_client = transport_failure  # type: ignore[assignment]
    retry_delay = addon._live_timer()
    if retry_delay != 0.25:
        raise AssertionError(f"Unexpected first reconnect delay: {retry_delay}")
    if addon._pending_control_config is not reconnect_config:
        raise AssertionError("Retryable failure discarded the desired control config")
    if addon._pending_live_armature is not obj:
        raise AssertionError("Retryable failure discarded the desired armature")
    if transport_failure.closed_with_goodbye is not False:
        raise AssertionError("Failed transport sent goodbye while scheduling reconnect")

    addon._stop_live_session()
    if addon._pending_control_config is not None or addon._live_timer() is not None:
        raise AssertionError("Explicit Stop did not cancel reconnect intent")
    if addon._live_reconnect_backoff.attempt_count != 0:
        raise AssertionError("Explicit Stop did not reset reconnect backoff")

    addon._pending_control_config = reconnect_config
    addon._pending_live_armature = obj
    protocol_failure = _FakeControlClient(
        addon.ControlSessionState.ERROR,
        retryable=False,
        last_error="ControlProtocolError: synthetic descriptor mismatch",
    )
    addon._control_client = protocol_failure  # type: ignore[assignment]
    if addon._live_timer() is not None:
        raise AssertionError("Terminal protocol failure kept the timer alive")
    if addon._pending_control_config is not None:
        raise AssertionError("Terminal protocol failure retained reconnect intent")

    addon._pending_control_config = reconnect_config
    addon._pending_live_armature = obj
    addon._pending_root_mode = "WORLD_ROOT"
    addon._live_reconnect_backoff.schedule(0.0)
    ready_client = _FakeControlClient(
        addon.ControlSessionState.READY,
        endpoint=(host, port),
    )
    addon._control_client = ready_client  # type: ignore[assignment]
    if addon._live_timer() is None:
        raise AssertionError("Ready reconnect did not start a new live pose session")
    reconnected_pose = _received_packet(receiver)
    if reconnected_pose.sequence != 0 or addon._live_session.sequence != 1:
        raise AssertionError("Reconnected LivePoseSession did not restart at sequence 0")
    if addon._live_reconnect_backoff.attempt_count != 0:
        raise AssertionError("Successful Ready did not reset reconnect backoff")
    addon._stop_live_session()

    obj.matrix_world = Matrix.Translation((4.0, -3.0, 2.0)) @ Matrix.Rotation(
        math.radians(35.0), 4, "Z"
    )
    bpy.context.view_layer.update()
    anchored = LivePoseSession(
        obj,
        host,
        port,
        session_id=0x12345677,
        root_mode="WORLD_ROOT",
    )
    try:
        anchored.tick(scene)
        anchored_pose = _received_packet(receiver)
        _assert_vector(anchored_pose.root_position, (0.0, 0.0, 0.0))
        _assert_quaternion(anchored_pose.root_rotation, (0.0, 0.0, 0.0, 1.0))
    finally:
        anchored.close()

    obj.scale = (2.0, 1.0, 1.0)
    bpy.context.view_layer.update()
    try:
        LivePoseSession(
            obj,
            host,
            port,
            session_id=0x12345677,
            root_mode="WORLD_ROOT",
        )
    except ValueError as error:
        if "unit scale" not in str(error):
            raise
    else:
        raise AssertionError("A non-unit Canonical session anchor was accepted")

    obj.matrix_world = Matrix.Identity(4)
    bpy.context.view_layer.update()
    session = LivePoseSession(
        obj,
        host,
        port,
        session_id=0x12345678,
        root_mode="WORLD_ROOT",
        send_hz=60,
        initial_sequence=41,
    )
    try:
        rest_info = session.tick(scene)
        rest = _received_packet(receiver)
        if rest_info.sequence != 41 or rest.sequence != 41 or session.sequence != 42:
            raise AssertionError("Rest packet sequence was not preserved/incremented")
        if rest.session_id != 0x12345678 or rest.joint_count != len(JOINTS):
            raise AssertionError("Rest packet session or Canonical joint count mismatch")
        if not (rest.flags & PosePacketFlags.WORLD_ROOT):
            raise AssertionError("WORLD_ROOT flag is missing")
        _assert_vector(rest.root_position, (0.0, 0.0, 0.0))
        _assert_vector(rest.hips_local_position_delta, (0.0, 0.0, 0.0))
        for rotation in rest.joint_rotations:
            _assert_quaternion(rotation, (0.0, 0.0, 0.0, 1.0))

        obj.location = (1.0, 2.0, 3.0)
        hips_yaw = Matrix.Rotation(math.radians(90.0), 4, "Z")
        shoulder_raise = Matrix.Rotation(math.radians(90.0), 4, "X")
        _set_global_delta(
            obj,
            "Hips",
            hips_yaw,
            Vector((0.10, -0.25, 0.15)),
        )
        _set_global_delta(obj, "LeftShoulder", shoulder_raise)
        scene.frame_set(8)
        bpy.context.view_layer.update()

        pose_info = session.tick(scene)
        pose = _received_packet(receiver)
        if pose_info.sequence != 42 or pose.sequence != 42 or session.sequence != 43:
            raise AssertionError("Pose packet sequence was not preserved/incremented")
        if pose.frame_index != 8 or pose_info.frame_index != 8:
            raise AssertionError("Scene frame index was not encoded")
        _assert_vector(pose.root_position, (-1.0, 3.0, -2.0))
        _assert_quaternion(pose.root_rotation, (0.0, 0.0, 0.0, 1.0))
        _assert_vector(pose.hips_local_position_delta, (-0.10, -0.25, 0.15))
        _assert_quaternion(pose.joint_rotations[0], (0.0, 0.0, 0.0, 1.0))
        half = math.sqrt(0.5)
        _assert_quaternion(pose.joint_rotations[1], (0.0, -half, 0.0, half))
        # The shoulder's local +X raise is composed under the Hips global yaw.
        _assert_quaternion(pose.joint_rotations[7], (0.5, -0.5, 0.5, 0.5))
        if session.last_error is not None:
            raise AssertionError(f"Unexpected live session error: {session.last_error}")
        if abs(session.interval_seconds - 1.0 / 60.0) > 1e-12:
            raise AssertionError("60 Hz timer interval is incorrect")
    finally:
        session.close()
        receiver.close()

    try:
        session.tick(scene)
    except RuntimeError:
        if session.last_error is None:
            raise AssertionError("Closed-session error was not recorded")
    else:
        raise AssertionError("Closed LivePoseSession accepted a tick")

    obj.location = (0.0, 0.0, 0.0)
    obj.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    bpy.context.view_layer.update()
    dropped = LivePoseSession(
        obj,
        host,
        port,
        session_id=0x12345678,
        root_mode="IN_PLACE",
        initial_sequence=100,
        udp_socket=_WouldBlockSocket(),  # type: ignore[arg-type]
    )
    try:
        dropped_info = dropped.tick(scene)
        if dropped_info.sequence != 100 or dropped.sequence != 101:
            raise AssertionError("A dropped full-state pose did not advance sequence")
        if dropped.sent_packets != 0 or dropped.dropped_packets != 1:
            raise AssertionError("Would-block UDP send was not counted as a dropped pose")
        if dropped.last_send_succeeded or dropped.last_error is not None:
            raise AssertionError("A recoverable UDP drop was reported as a session failure")
    finally:
        dropped.close()

    print(
        "MOTION_BRIDGE_LIVE_POSE_TEST="
        + json.dumps(
            {
                "blender": bpy.app.version_string,
                "jointCount": len(JOINTS),
                "restSequence": rest.sequence,
                "poseSequence": pose.sequence,
                "rootPosition": pose.root_position,
                "hipsDelta": pose.hips_local_position_delta,
                "wouldBlockDrops": dropped.dropped_packets,
                "reconnectSequence": reconnected_pose.sequence,
                "retryDelay": retry_delay,
                "handlerCleanup": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
