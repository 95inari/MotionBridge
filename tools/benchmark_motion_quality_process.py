from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import tempfile
import time


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ADDON_ROOT = REPOSITORY_ROOT / "blender_addon"
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))

from motion_bridge.motion_quality import (  # noqa: E402
    BoneSample,
    MotionFrame,
    MotionSequence,
    MotionValidationSettings,
)
from motion_bridge.motion_quality_process import (  # noqa: E402
    MotionQualityProcessJob,
    _INPUT_NAME,
    _JOB_PREFIX,
)


DEFAULT_FRAME_COUNT = 7_201
DEFAULT_FPS = 30.0
DEFAULT_CHUNK_MS = 8.0
DEFAULT_TIMEOUT_SECONDS = 180.0
IDENTITY = (1.0, 0.0, 0.0, 0.0)
BONES = (
    ("Hips", None),
    ("Spine", "Hips"),
    ("Chest", "Spine"),
    ("UpperChest", "Chest"),
    ("Neck", "UpperChest"),
    ("Head", "Neck"),
    ("LeftShoulder", "UpperChest"),
    ("LeftUpperArm", "LeftShoulder"),
    ("LeftLowerArm", "LeftUpperArm"),
    ("LeftHand", "LeftLowerArm"),
    ("RightShoulder", "UpperChest"),
    ("RightUpperArm", "RightShoulder"),
    ("RightLowerArm", "RightUpperArm"),
    ("RightHand", "RightLowerArm"),
    ("LeftUpperLeg", "Hips"),
    ("LeftLowerLeg", "LeftUpperLeg"),
    ("LeftFoot", "LeftLowerLeg"),
    ("LeftToes", "LeftFoot"),
    ("RightUpperLeg", "Hips"),
    ("RightLowerLeg", "RightUpperLeg"),
    ("RightFoot", "RightLowerLeg"),
    ("RightToes", "RightFoot"),
)


def _nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _build_sequence(frame_count: int, fps: float) -> MotionSequence:
    frames: list[MotionFrame] = []
    for frame_index in range(frame_count):
        phase = frame_index * 0.015
        samples: list[BoneSample] = []
        for bone_index, (name, parent) in enumerate(BONES):
            angle = 0.025 * math.sin(phase + bone_index * 0.17)
            quaternion = (
                math.cos(angle * 0.5),
                0.0,
                0.0,
                math.sin(angle * 0.5),
            )
            lateral = (bone_index % 3 - 1) * 0.08
            height = bone_index * 0.075
            sway = 0.002 * math.sin(phase + bone_index * 0.11)
            samples.append(
                BoneSample(
                    name=name,
                    parent=parent,
                    local_rotation_wxyz=quaternion,
                    global_rotation_wxyz=quaternion,
                    head_world=(lateral + sway, 0.0, height),
                    tail_world=(lateral + sway, 0.0, height + 0.07),
                )
            )
        frames.append(
            MotionFrame(
                frame=float(frame_index),
                time_seconds=frame_index / fps,
                root_location=(
                    frame_index * 0.0001,
                    0.005 * math.sin(phase),
                    0.0,
                ),
                root_rotation_wxyz=IDENTITY,
                root_scale=(1.0, 1.0, 1.0),
                bones=tuple(samples),
            )
        )
    return MotionSequence(
        fps=fps,
        start_frame=0.0,
        end_frame=float(frame_count - 1),
        frames=tuple(frames),
        source_id=f"benchmark-{frame_count}x{len(BONES)}",
    )


def _owned_directories() -> set[str]:
    return {
        path.name
        for path in Path(tempfile.gettempdir()).glob(f"{_JOB_PREFIX}*")
    }


def _serialize(
    job: MotionQualityProcessJob,
    *,
    chunk_seconds: float,
) -> tuple[float, list[float], list[int]]:
    durations: list[float] = []
    frame_counts: list[int] = []
    started = time.perf_counter()
    while job.phase == "serializing":
        before = job._frame_index
        step_started = time.perf_counter()
        job.step_serialization(time.monotonic() + chunk_seconds)
        durations.append((time.perf_counter() - step_started) * 1000.0)
        frame_counts.append(job._frame_index - before)
    return time.perf_counter() - started, durations, frame_counts


def _wait_for_report(
    job: MotionQualityProcessJob,
    *,
    timeout_seconds: float,
) -> tuple[object, float, list[float]]:
    durations: list[float] = []
    deadline = time.monotonic() + timeout_seconds
    started = time.perf_counter()
    report = None
    while report is None and time.monotonic() < deadline:
        poll_started = time.perf_counter()
        report = job.poll()
        durations.append((time.perf_counter() - poll_started) * 1000.0)
        if report is None:
            time.sleep(0.005)
    if report is None:
        raise TimeoutError(f"analysis exceeded {timeout_seconds:g} seconds")
    return report, time.perf_counter() - started, durations


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark isolated Motion Quality serialization and analysis.",
    )
    parser.add_argument("--frames", type=int, default=DEFAULT_FRAME_COUNT)
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS)
    parser.add_argument("--chunk-ms", type=float, default=DEFAULT_CHUNK_MS)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    values = parser.parse_args()
    if values.frames < 2:
        parser.error("--frames must be at least 2")
    if not math.isfinite(values.fps) or values.fps <= 0.0:
        parser.error("--fps must be a positive finite value")
    if not math.isfinite(values.chunk_ms) or values.chunk_ms <= 0.0:
        parser.error("--chunk-ms must be a positive finite value")
    if not math.isfinite(values.timeout_seconds) or values.timeout_seconds <= 0.0:
        parser.error("--timeout-seconds must be a positive finite value")
    return values


def main() -> None:
    arguments = _arguments()
    baseline_directories = _owned_directories()
    build_started = time.perf_counter()
    sequence = _build_sequence(arguments.frames, arguments.fps)
    build_seconds = time.perf_counter() - build_started
    settings = MotionValidationSettings(auto_detect_contacts=False, limbs=())
    job = MotionQualityProcessJob(sequence, settings)
    job_dir = job._job_dir
    assert job_dir is not None

    try:
        serialization_seconds, step_ms, frame_counts = _serialize(
            job,
            chunk_seconds=arguments.chunk_ms / 1000.0,
        )
        input_path = job_dir / _INPUT_NAME
        input_bytes = input_path.stat().st_size
        report, analysis_seconds, poll_ms = _wait_for_report(
            job,
            timeout_seconds=arguments.timeout_seconds,
        )
        remaining_directories = _owned_directories() - baseline_directories
        result = {
            "analysisSeconds": analysis_seconds,
            "boneSamples": arguments.frames * len(BONES),
            "bonesPerFrame": len(BONES),
            "buildSeconds": build_seconds,
            "chunkBudgetMs": arguments.chunk_ms,
            "directoryRemoved": not job_dir.exists(),
            "frames": arguments.frames,
            "framesPerStepMax": max(frame_counts),
            "framesPerStepMedian": sorted(frame_counts)[len(frame_counts) // 2],
            "framesPerStepMin": min(frame_counts),
            "inputBytes": input_bytes,
            "inputMiB": input_bytes / (1024.0 * 1024.0),
            "inputTransportSha256": job._expected_input_hash,
            "issueCount": len(report.issues),
            "pollMaxMs": max(poll_ms),
            "pollP95Ms": _nearest_rank(poll_ms, 0.95),
            "processReaped": job._process is None,
            "python": sys.executable,
            "remainingOwnedDirectories": sorted(remaining_directories),
            "sampleCount": report.sample_count,
            "serializationSeconds": serialization_seconds,
            "serializationStepMaxMs": max(step_ms),
            "serializationStepMedianMs": sorted(step_ms)[len(step_ms) // 2],
            "serializationStepP95Ms": _nearest_rank(step_ms, 0.95),
            "serializationSteps": len(step_ms),
            "serializationStepsOver10Ms": sum(value > 10.0 for value in step_ms),
            "serializationStepsOver50Ms": sum(value > 50.0 for value in step_ms),
            "summary": report.summary.value,
        }
        print("MOTION_BRIDGE_MOTION_QUALITY_BENCHMARK=" + json.dumps(result, sort_keys=True))
    finally:
        job.cancel()


if __name__ == "__main__":
    main()
