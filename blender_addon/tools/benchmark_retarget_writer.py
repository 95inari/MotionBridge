from __future__ import annotations

import argparse
from array import array
import json
import math
from pathlib import Path
import statistics
import sys
import time

import bpy
from mathutils import Quaternion, Vector


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))

from motion_bridge.canonical.rig_spec import JOINTS  # noqa: E402
from motion_bridge.editing.action_api import (  # noqa: E402
    ActionChannelAccess,
    create_action_for_id,
    ensure_fcurve,
    iter_action_fcurves,
)
from motion_bridge.retargeting.bake import _ActionWriter  # noqa: E402


class _LegacyInsertWriter:
    """Previous per-key implementation retained only as a benchmark oracle."""

    def __init__(self, access: ActionChannelAccess) -> None:
        self._access = access
        self._curves: dict[tuple[str, int], bpy.types.FCurve] = {}

    def insert_quaternion(
        self,
        bone_name: str,
        frame: float,
        quaternion: Quaternion,
    ) -> None:
        path = f'pose.bones["{bone_name}"].rotation_quaternion'
        for index in range(4):
            self._insert(path, index, bone_name, frame, float(quaternion[index]))

    def insert_object_quaternion(
        self,
        frame: float,
        quaternion: Quaternion,
    ) -> None:
        for index in range(4):
            self._insert(
                "rotation_quaternion",
                index,
                "Root",
                frame,
                float(quaternion[index]),
            )

    def insert_vector(
        self,
        data_path: str,
        group_name: str,
        frame: float,
        value: Vector,
    ) -> None:
        for index in range(3):
            self._insert(data_path, index, group_name, frame, float(value[index]))

    def finish(self) -> None:
        for curve in self._curves.values():
            curve.update()

    def _insert(
        self,
        data_path: str,
        index: int,
        group_name: str,
        frame: float,
        value: float,
    ) -> None:
        key = (data_path, index)
        curve = self._curves.get(key)
        if curve is None:
            curve = ensure_fcurve(
                self._access,
                data_path,
                index,
                group_name=group_name,
            )
            self._curves[key] = curve
        point = curve.keyframe_points.insert(frame, value, options={"FAST"})
        point.interpolation = "LINEAR"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=600)
    parser.add_argument("--iterations", type=int, default=3)
    arguments = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    result = parser.parse_args(arguments)
    if result.frames < 2:
        parser.error("--frames must be at least 2")
    if result.iterations < 1:
        parser.error("--iterations must be positive")
    return result


def _create_target() -> bpy.types.Object:
    target = bpy.data.objects.new("MB_Retarget_Writer_Benchmark", None)
    bpy.context.scene.collection.objects.link(target)
    return target


def _samples(
    frame_count: int,
) -> list[tuple[Quaternion, Vector, tuple[Quaternion, ...], Vector]]:
    result: list[tuple[Quaternion, Vector, tuple[Quaternion, ...], Vector]] = []
    for frame_index in range(frame_count):
        phase = frame_index / (frame_count - 1)
        root = Quaternion((0.0, 0.0, 1.0), phase * math.radians(95.0))
        location = Vector((phase * 2.0, math.sin(phase * math.pi), phase * -0.25))
        bones = tuple(
            Quaternion(
                Vector((0.3 + joint.index * 0.01, 0.7, 0.2)).normalized(),
                phase * math.radians(10.0 + joint.index),
            )
            for joint in JOINTS[1:]
        )
        hips = Vector((phase * 0.05, 0.0, math.sin(phase * math.pi) * 0.1))
        result.append((root, location, bones, hips))
    return result


def _write_action(
    target: bpy.types.Object,
    samples: list[tuple[Quaternion, Vector, tuple[Quaternion, ...], Vector]],
    writer_type: type[_ActionWriter] | type[_LegacyInsertWriter],
    name: str,
) -> tuple[bpy.types.Action, float]:
    access = create_action_for_id(
        name,
        target,
        frame_start=1,
        frame_end=len(samples),
    )
    started = time.perf_counter()
    writer = writer_type(access)
    for frame, (root, location, bones, hips) in enumerate(samples, start=1):
        writer.insert_object_quaternion(frame, root)
        writer.insert_vector("location", "Root", frame, location)
        for joint, quaternion in zip(JOINTS[1:], bones):
            writer.insert_quaternion(joint.name, frame, quaternion)
        writer.insert_vector(
            'pose.bones["Hips"].location',
            "Hips",
            frame,
            hips,
        )
    writer.finish()
    return access.action, time.perf_counter() - started


def _action_signature(action: bpy.types.Action) -> tuple[tuple[object, ...], ...]:
    result: list[tuple[object, ...]] = []
    for curve in iter_action_fcurves(action):
        point_count = len(curve.keyframe_points)
        coordinates = array("f", [0.0]) * (point_count * 2)
        interpolations = array("i", [0]) * point_count
        curve.keyframe_points.foreach_get("co", coordinates)
        curve.keyframe_points.foreach_get("interpolation", interpolations)
        result.append(
            (
                curve.data_path,
                curve.array_index,
                coordinates.tobytes(),
                interpolations.tobytes(),
            )
        )
    return tuple(result)


def _assert_linear(action: bpy.types.Action) -> None:
    for curve in iter_action_fcurves(action):
        if any(point.interpolation != "LINEAR" for point in curve.keyframe_points):
            raise AssertionError(f"Non-linear interpolation: {curve.data_path}")


def main() -> None:
    arguments = _parse_args()
    target = _create_target()
    samples = _samples(arguments.frames)
    bulk_times: list[float] = []
    legacy_times: list[float] = []
    curve_count = 0
    key_count = 0

    for iteration in range(arguments.iterations):
        bulk, bulk_elapsed = _write_action(
            target,
            samples,
            _ActionWriter,
            f"MB_Bulk_{iteration}",
        )
        legacy, legacy_elapsed = _write_action(
            target,
            samples,
            _LegacyInsertWriter,
            f"MB_Legacy_{iteration}",
        )
        if _action_signature(bulk) != _action_signature(legacy):
            raise AssertionError("Bulk and legacy FCurve output differ")
        _assert_linear(bulk)
        curve_count = sum(1 for _curve in iter_action_fcurves(bulk))
        key_count = sum(
            len(curve.keyframe_points) for curve in iter_action_fcurves(bulk)
        )
        bulk_times.append(bulk_elapsed)
        legacy_times.append(legacy_elapsed)
        bpy.data.actions.remove(bulk)
        bpy.data.actions.remove(legacy)

    bulk_median = statistics.median(bulk_times)
    legacy_median = statistics.median(legacy_times)
    print(
        json.dumps(
            {
                "status": "PASS",
                "frames": arguments.frames,
                "curves": curve_count,
                "keys": key_count,
                "iterations": arguments.iterations,
                "bulkMedianSeconds": bulk_median,
                "legacyMedianSeconds": legacy_median,
                "speedup": legacy_median / bulk_median,
                "outputIdentical": True,
                "linearInterpolation": True,
                "bulkApi": [
                    "keyframe_points.add",
                    "foreach_set(co)",
                    "foreach_set(interpolation)",
                ],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
