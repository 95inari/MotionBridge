from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

import bpy


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))
TOOLS_ROOT = Path(__file__).resolve().parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from build_canonical import create_canonical_source  # noqa: E402
from motion_bridge.canonical.runtime_descriptor import (  # noqa: E402
    descriptor_from_armature,
)
from motion_bridge.live.control_client import (  # noqa: E402
    BlenderControlClient,
    BlenderControlClientConfig,
    ControlSessionState,
)
from motion_bridge.live.pose_session import LivePoseSession  # noqa: E402


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port-file", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    values = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    return parser.parse_args(values)


def _poll_until(
    client: BlenderControlClient,
    expected: ControlSessionState,
    timeout_seconds: float = 10.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = client.poll()
        if state is expected:
            return
        if state is ControlSessionState.ERROR:
            raise RuntimeError(client.last_error or "control session failed")
        time.sleep(0.002)
    raise TimeoutError(f"Timed out waiting for control state {expected.value}")


def main() -> None:
    arguments = _arguments()
    port_info = json.loads(arguments.port_file.read_text(encoding="utf-8"))
    session_id = int(port_info["sessionId"])
    canonical = create_canonical_source(arguments.report.with_suffix(".blend"))
    descriptor = descriptor_from_armature(canonical)
    client = BlenderControlClient(
        BlenderControlClientConfig(
            session_id=session_id,
            descriptor=descriptor,
            control_port=int(port_info["controlPort"]),
            blender_version=bpy.app.version_string,
            requested_pose_hz=60,
        )
    )
    pose_session: LivePoseSession | None = None
    try:
        client.connect()
        _poll_until(client, ControlSessionState.READY)
        endpoint = client.udp_endpoint
        if endpoint is None:
            raise RuntimeError("Unity did not negotiate a UDP endpoint")
        pose_session = LivePoseSession(
            canonical,
            endpoint[0],
            endpoint[1],
            session_id,
            "WORLD_ROOT",
            send_hz=client.accepted_pose_hz or 60,
        )
        client.start_streaming()
        client.poll()
        for sequence in range(3):
            canonical.location.x = sequence * 0.1
            bpy.context.scene.frame_set(sequence + 1)
            bpy.context.view_layer.update()
            pose_session.tick(bpy.context.scene)
            client.poll()
            time.sleep(0.05)
        if pose_session.sent_packets != 3 or pose_session.dropped_packets != 0:
            raise RuntimeError(
                "Cross-runtime UDP sends were not lossless: "
                f"sent={pose_session.sent_packets}, dropped={pose_session.dropped_packets}"
            )
        telemetry_deadline = time.monotonic() + 3.0
        while time.monotonic() < telemetry_deadline:
            if client.poll() is ControlSessionState.ERROR:
                raise RuntimeError(client.last_error or "control session failed")
            stats = client.pose_stats
            if client.clock_sync_estimate is not None and stats is not None:
                if stats.received >= 3 and stats.one_way_age_usec_p95 is not None:
                    break
            time.sleep(0.01)
        else:
            raise TimeoutError("Timed out waiting for negotiated live telemetry")
        clock_sync = client.clock_sync_estimate
        stats = client.pose_stats
        if clock_sync is None or stats is None:
            raise RuntimeError("Negotiated telemetry was not published")
        if (
            stats.received != 3
            or stats.dropped_by_sequence != 0
            or stats.rejected != 0
            or stats.receiver_overrun != 0
            or stats.one_way_age_usec_p50 is None
            or stats.one_way_age_usec_p95 is None
            or stats.one_way_age_usec_p95 < stats.one_way_age_usec_p50
        ):
            raise RuntimeError(f"Unexpected Unity pose telemetry: {stats!r}")
        client.pause_streaming()
        client.poll()
        time.sleep(0.05)
        client.start_streaming()
        client.poll()
        time.sleep(0.05)
        report = {
            "blenderVersion": bpy.app.version_string,
            "sessionId": session_id,
            "controlState": client.state.value,
            "udpEndpoint": endpoint,
            "sentPackets": pose_session.sent_packets,
            "droppedPackets": pose_session.dropped_packets,
            "lastSequence": pose_session.sequence - 1,
            "clockSyncRttUsec": clock_sync.round_trip_time_usec,
            "unityReceivedPackets": stats.received,
            "unitySequenceGaps": stats.dropped_by_sequence,
            "unityRejectedPackets": stats.rejected,
            "unityReceiverOverruns": stats.receiver_overrun,
            "unityOneWayAgeUsecP50": stats.one_way_age_usec_p50,
            "unityOneWayAgeUsecP95": stats.one_way_age_usec_p95,
            "pauseResume": True,
        }
        arguments.report.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        print("MOTION_BRIDGE_BLENDER_E2E=" + json.dumps(report, sort_keys=True))
    finally:
        if pose_session is not None:
            pose_session.close()
        client.close()


if __name__ == "__main__":
    main()
