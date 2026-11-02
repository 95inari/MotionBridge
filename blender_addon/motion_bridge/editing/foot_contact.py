"""Evaluated-pose foot contact intervals for correction authoring."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Sequence

import bpy


_MAX_CONTACT_SAMPLES = 20_000


@dataclass(frozen=True, slots=True)
class FootContactSample:
    frame: int
    position: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class FootContactInterval:
    frame_start: int
    frame_end: int
    anchor: tuple[float, float, float]
    peak_speed: float
    peak_ground_distance: float

    @property
    def start(self) -> int:
        return self.frame_start

    @property
    def end(self) -> int:
        return self.frame_end


def _validated_samples(
    samples: Iterable[FootContactSample],
) -> tuple[FootContactSample, ...]:
    result = tuple(samples)
    if not result:
        raise ValueError("Foot contact detection requires at least one sample")
    previous_frame: int | None = None
    for sample in result:
        if not isinstance(sample.frame, int) or isinstance(sample.frame, bool):
            raise TypeError("Foot contact sample frames must be integers")
        if previous_frame is not None and sample.frame != previous_frame + 1:
            raise ValueError("Foot contact samples must use consecutive frames")
        if len(sample.position) != 3 or not all(
            math.isfinite(float(value)) for value in sample.position
        ):
            raise ValueError("Foot contact sample positions must be finite XYZ values")
        previous_frame = sample.frame
    return result


def _sample_speeds(
    samples: Sequence[FootContactSample],
    height_candidates: Sequence[bool],
    fps: float,
) -> tuple[float, ...]:
    if len(samples) == 1:
        return (0.0,)
    segment_speeds: list[float] = []
    for left, right in zip(samples[:-1], samples[1:], strict=True):
        distance = math.dist(left.position, right.position)
        seconds = (right.frame - left.frame) / fps
        segment_speeds.append(distance / seconds)
    speeds: list[float] = []
    for index in range(len(samples)):
        adjacent: list[float] = []
        if index and height_candidates[index - 1] and height_candidates[index]:
            adjacent.append(segment_speeds[index - 1])
        if (
            index < len(segment_speeds)
            and height_candidates[index]
            and height_candidates[index + 1]
        ):
            adjacent.append(segment_speeds[index])
        speeds.append(max(adjacent, default=0.0))
    return tuple(speeds)


def _true_runs(flags: Sequence[bool]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    for index, enabled in enumerate((*flags, False)):
        if enabled and run_start is None:
            run_start = index
        elif not enabled and run_start is not None:
            runs.append((run_start, index - 1))
            run_start = None
    return runs


def _join_runs(
    runs: Sequence[tuple[int, int]],
    gap_frames: int,
) -> tuple[tuple[int, int], ...]:
    if not runs:
        return ()
    joined: list[tuple[int, int]] = [runs[0]]
    for start, end in runs[1:]:
        previous_start, previous_end = joined[-1]
        if start - previous_end - 1 <= gap_frames:
            joined[-1] = (previous_start, end)
        else:
            joined.append((start, end))
    return tuple(joined)


def detect_foot_contact_intervals(
    samples: Iterable[FootContactSample],
    *,
    frames_per_second: float | None = None,
    fps: float | None = None,
    ground_height: float,
    max_height: float,
    max_speed: float,
    min_frames: int,
    gap_frames: int,
) -> tuple[FootContactInterval, ...]:
    """Classify and join contact runs without touching Blender state."""

    checked = _validated_samples(samples)
    if frames_per_second is not None and fps is not None:
        raise ValueError("Specify frames_per_second only once")
    effective_fps = frames_per_second if frames_per_second is not None else fps
    if effective_fps is None or not math.isfinite(effective_fps) or effective_fps <= 0.0:
        raise ValueError("Scene FPS must be finite and greater than zero")
    if not math.isfinite(ground_height):
        raise ValueError("Ground height must be finite")
    if not math.isfinite(max_height) or max_height < 0.0:
        raise ValueError("Maximum contact height must be finite and non-negative")
    if not math.isfinite(max_speed) or max_speed < 0.0:
        raise ValueError("Maximum contact speed must be finite and non-negative")
    if not isinstance(min_frames, int) or isinstance(min_frames, bool) or min_frames < 1:
        raise ValueError("Minimum contact frames must be a positive integer")
    if not isinstance(gap_frames, int) or isinstance(gap_frames, bool) or gap_frames < 0:
        raise ValueError("Contact join gap must be a non-negative integer")

    signed_heights = tuple(
        float(sample.position[2]) - ground_height for sample in checked
    )
    ground_distances = tuple(
        abs(height) for height in signed_heights
    )
    height_candidates = tuple(height <= max_height for height in signed_heights)
    speeds = _sample_speeds(checked, height_candidates, effective_fps)
    flags = tuple(
        height_candidate and speed <= max_speed
        for height_candidate, speed in zip(height_candidates, speeds, strict=True)
    )
    runs = _join_runs(_true_runs(flags), gap_frames)

    intervals: list[FootContactInterval] = []
    for start_index, end_index in runs:
        if end_index - start_index + 1 < min_frames:
            continue
        selected = checked[start_index : end_index + 1]
        count = len(selected)
        anchor = tuple(
            sum(float(sample.position[axis]) for sample in selected) / count
            for axis in range(3)
        )
        intervals.append(
            FootContactInterval(
                frame_start=selected[0].frame,
                frame_end=selected[-1].frame,
                anchor=anchor,
                peak_speed=max(speeds[start_index : end_index + 1]),
                peak_ground_distance=max(
                    ground_distances[start_index : end_index + 1]
                ),
            )
        )
    return tuple(intervals)


def contact_interval_at_frame(
    intervals: Iterable[FootContactInterval],
    frame: float,
) -> FootContactInterval:
    if not math.isfinite(frame):
        raise ValueError("Current frame must be finite")
    for interval in intervals:
        if interval.frame_start <= frame <= interval.frame_end:
            return interval
    raise ValueError("No foot contact interval contains the current frame")


def sample_canonical_foot_tail(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    foot_bone_name: str,
    scene_frame_start: int,
    scene_frame_end: int,
    *,
    max_samples: int = _MAX_CONTACT_SAMPLES,
) -> tuple[FootContactSample, ...]:
    if armature is None or armature.type != "ARMATURE":
        raise TypeError("Active object must be a Canonical Armature")
    if foot_bone_name not in {"LeftFoot", "RightFoot"}:
        raise ValueError("Foot bone must be LeftFoot or RightFoot")
    if armature.pose.bones.get(foot_bone_name) is None:
        raise ValueError(f"Canonical bone {foot_bone_name!r} was not found")
    if scene_frame_end < scene_frame_start:
        raise ValueError("Scene frame end must not be before frame start")
    sample_count = scene_frame_end - scene_frame_start + 1
    if max_samples < 1 or sample_count > max_samples:
        raise ValueError(
            f"Contact scan has {sample_count} frames; maximum is {max_samples}"
        )
    scene = context.scene
    original_frame = int(scene.frame_current)
    original_subframe = float(scene.frame_subframe)
    samples: list[FootContactSample] = []
    try:
        for frame in range(scene_frame_start, scene_frame_end + 1):
            scene.frame_set(frame, subframe=0.0)
            context.view_layer.update()
            evaluated = armature.evaluated_get(context.evaluated_depsgraph_get())
            pose_bone = evaluated.pose.bones.get(foot_bone_name)
            if pose_bone is None:
                raise ValueError(f"Canonical bone {foot_bone_name!r} was not found")
            world_tail = evaluated.matrix_world @ pose_bone.tail
            samples.append(
                FootContactSample(
                    frame=frame,
                    position=tuple(float(value) for value in world_tail),
                )
            )
    finally:
        scene.frame_set(original_frame, subframe=original_subframe)
        context.view_layer.update()
    return tuple(samples)


def detect_contact_range_at_current(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    *,
    foot_bone_name: str,
    scene_frame_start: int,
    scene_frame_end: int,
    ground_height: float,
    max_height: float,
    max_speed: float,
    min_frames: int,
    gap_frames: int,
    max_samples: int = _MAX_CONTACT_SAMPLES,
) -> FootContactInterval:
    """Sample the evaluated stack and return contact containing the current frame."""

    scene = context.scene
    current_frame = float(scene.frame_current) + float(scene.frame_subframe)
    if not scene_frame_start <= current_frame <= scene_frame_end:
        raise ValueError("Current frame is outside the Scene frame range")
    render = scene.render
    fps_base = float(render.fps_base)
    if not math.isfinite(fps_base) or fps_base <= 0.0:
        raise ValueError("Scene FPS base must be finite and greater than zero")
    frames_per_second = float(render.fps) / fps_base
    radius = max(120, min_frames + gap_frames + 2)
    current_floor = math.floor(current_frame)
    current_ceil = math.ceil(current_frame)
    window_start = max(scene_frame_start, current_floor - radius)
    window_end = min(scene_frame_end, current_ceil + radius)
    samples: tuple[FootContactSample, ...] = ()

    while True:
        sample_count = window_end - window_start + 1
        if max_samples < 1 or sample_count > max_samples:
            raise ValueError(
                f"Contact scan needs {sample_count} frames; maximum is {max_samples}"
            )
        if not samples:
            samples = sample_canonical_foot_tail(
                context,
                armature,
                foot_bone_name,
                window_start,
                window_end,
                max_samples=max_samples,
            )
        else:
            previous_start = samples[0].frame
            previous_end = samples[-1].frame
            left = (
                sample_canonical_foot_tail(
                    context,
                    armature,
                    foot_bone_name,
                    window_start,
                    previous_start - 1,
                    max_samples=max_samples,
                )
                if window_start < previous_start
                else ()
            )
            right = (
                sample_canonical_foot_tail(
                    context,
                    armature,
                    foot_bone_name,
                    previous_end + 1,
                    window_end,
                    max_samples=max_samples,
                )
                if previous_end < window_end
                else ()
            )
            samples = left + samples + right

        intervals = detect_foot_contact_intervals(
            samples,
            frames_per_second=frames_per_second,
            ground_height=ground_height,
            max_height=max_height,
            max_speed=max_speed,
            min_frames=min_frames,
            gap_frames=gap_frames,
        )
        interval = contact_interval_at_frame(intervals, current_frame)
        left_closed = (
            window_start == scene_frame_start
            or interval.frame_start - window_start > gap_frames
        )
        right_closed = (
            window_end == scene_frame_end
            or window_end - interval.frame_end > gap_frames
        )
        if left_closed and right_closed:
            return interval

        radius *= 2
        if not left_closed:
            window_start = max(scene_frame_start, current_floor - radius)
        if not right_closed:
            window_end = min(scene_frame_end, current_ceil + radius)


def detect_and_commit_contact_preview_range(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    *,
    foot_bone_name: str,
    scene_frame_start: int,
    scene_frame_end: int,
    ground_height: float,
    max_height: float,
    max_speed: float,
    min_frames: int,
    gap_frames: int,
    max_samples: int = _MAX_CONTACT_SAMPLES,
) -> FootContactInterval:
    """Commit Preview Range only after contact detection fully succeeds."""

    interval = detect_contact_range_at_current(
        context,
        armature,
        foot_bone_name=foot_bone_name,
        scene_frame_start=scene_frame_start,
        scene_frame_end=scene_frame_end,
        ground_height=ground_height,
        max_height=max_height,
        max_speed=max_speed,
        min_frames=min_frames,
        gap_frames=gap_frames,
        max_samples=max_samples,
    )
    scene = context.scene
    previous = (
        bool(scene.use_preview_range),
        int(scene.frame_preview_start),
        int(scene.frame_preview_end),
    )
    try:
        _set_preview_range(
            scene,
            enabled=True,
            frame_start=interval.frame_start,
            frame_end=interval.frame_end,
        )
    except Exception as exception:
        try:
            _set_preview_range(
                scene,
                enabled=previous[0],
                frame_start=previous[1],
                frame_end=previous[2],
            )
        except Exception:
            pass
        raise RuntimeError("Could not set the Timeline Preview Range") from exception
    return interval


def _set_preview_range(
    scene: bpy.types.Scene,
    *,
    enabled: bool,
    frame_start: int,
    frame_end: int,
) -> None:
    if frame_start > int(scene.frame_preview_end):
        scene.frame_preview_end = frame_end
        scene.frame_preview_start = frame_start
    else:
        scene.frame_preview_start = frame_start
        scene.frame_preview_end = frame_end
    scene.use_preview_range = enabled
