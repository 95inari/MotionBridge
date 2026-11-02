from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import time

import bpy


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))

from motion_bridge.editing.action_api import (  # noqa: E402
    create_action_for_id,
    ensure_fcurve,
)
from motion_bridge.performance.take_analysis import analyze_action  # noqa: E402


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark a deterministic dense Action")
    parser.add_argument("--seed", type=int, default=20260901)
    parser.add_argument("--bones", type=int, default=65)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--output", type=Path)
    arguments = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    values = parser.parse_args(arguments)
    if values.bones < 1 or values.seconds < 1 or values.fps < 1 or values.iterations < 1:
        parser.error("bones, seconds, fps, and iterations must be positive")
    return values


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)


def _create_armature(bone_count: int) -> bpy.types.Object:
    _clear_scene()
    armature = bpy.data.armatures.new("MB_Dense_Benchmark_Armature")
    obj = bpy.data.objects.new("MB_Dense_Benchmark", armature)
    bpy.context.scene.collection.objects.link(obj)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    parent = None
    for index in range(bone_count):
        bone = armature.edit_bones.new(f"Bone_{index:03d}")
        bone.head = (0.0, 0.0, index * 0.02)
        bone.tail = (0.0, 0.0, index * 0.02 + 0.1)
        bone.parent = parent
        parent = bone
    bpy.ops.object.mode_set(mode="OBJECT")
    for bone in obj.pose.bones:
        bone.rotation_mode = "QUATERNION"
    return obj


def _component_value(seed: int, bone: int, component: int, frame: int) -> float:
    if component == 0:
        return 0.999
    phase = (seed % 997) * 0.0001 + bone * 0.071 + component * 0.193
    frequency = 0.0025 * (1 + (bone + component) % 7)
    return math.sin(frame * frequency + phase) * 0.025


def _build_dense_action(
    obj: bpy.types.Object,
    *,
    seed: int,
    bone_count: int,
    frame_count: int,
) -> bpy.types.Action:
    access = create_action_for_id(
        "MB_Dense_Take_Benchmark",
        obj,
        frame_start=0,
        frame_end=frame_count - 1,
    )
    for bone_index in range(bone_count):
        bone_name = f"Bone_{bone_index:03d}"
        data_path = f'pose.bones["{bone_name}"].rotation_quaternion'
        for component in range(4):
            curve = ensure_fcurve(
                access,
                data_path,
                component,
                group_name=bone_name,
            )
            points = curve.keyframe_points
            points.add(frame_count)
            coordinates = [0.0] * (frame_count * 2)
            for frame in range(frame_count):
                coordinates[frame * 2] = float(frame)
                coordinates[frame * 2 + 1] = _component_value(
                    seed,
                    bone_index,
                    component,
                    frame,
                )
            points.foreach_set("co", coordinates)
            curve.update()

    animation_data = obj.animation_data_create()
    animation_data.action = access.action
    if access.slot is not None and hasattr(animation_data, "action_slot"):
        animation_data.action_slot = access.slot
    return access.action


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1)
    return ordered[max(0, index)]


def _measure_playback(frame_count: int, iterations: int) -> list[float]:
    scene = bpy.context.scene
    probe_frames = tuple(range(frame_count))
    for frame in probe_frames[: min(10, frame_count)]:
        scene.frame_set(frame)
    samples_ms: list[float] = []
    for _iteration in range(iterations):
        for frame in probe_frames:
            started = time.perf_counter_ns()
            scene.frame_set(frame)
            samples_ms.append((time.perf_counter_ns() - started) / 1_000_000.0)
    return samples_ms


def main() -> None:
    arguments = _arguments()
    frame_count = arguments.seconds * arguments.fps + 1
    obj = _create_armature(arguments.bones)
    generation_started = time.perf_counter()
    action = _build_dense_action(
        obj,
        seed=arguments.seed,
        bone_count=arguments.bones,
        frame_count=frame_count,
    )
    generation_seconds = time.perf_counter() - generation_started
    analysis = analyze_action(action)
    samples_ms = _measure_playback(frame_count, arguments.iterations)
    report = {
        "schemaVersion": 1,
        "seed": arguments.seed,
        "blenderVersion": bpy.app.version_string,
        "blenderBuildHash": bpy.app.build_hash.decode("ascii", errors="replace"),
        "os": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "bones": arguments.bones,
        "seconds": arguments.seconds,
        "fps": arguments.fps,
        "iterations": arguments.iterations,
        "framesPerIteration": frame_count,
        "fcurves": analysis.fcurve_count,
        "keyPoints": analysis.total_points,
        "takeTier": analysis.tier.value,
        "generationSeconds": generation_seconds,
        "frameTimeMs": {
            "mean": statistics.fmean(samples_ms),
            "p50": _percentile(samples_ms, 0.50),
            "p95": _percentile(samples_ms, 0.95),
            "p99": _percentile(samples_ms, 0.99),
            "max": max(samples_ms),
        },
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded + "\n", encoding="utf-8")
    print("MOTION_BRIDGE_DENSE_BENCHMARK=" + json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
