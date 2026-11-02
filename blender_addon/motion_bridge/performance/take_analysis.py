from __future__ import annotations

from dataclasses import dataclass
import math

import bpy

from ..editing.action_api import iter_action_fcurves
from .budget import TakeBudgetTier, classify_take_size


@dataclass(frozen=True, slots=True)
class TakeAnalysis:
    action_name: str
    fcurve_count: int
    keyframe_points: int
    sampled_points: int
    frame_start: float
    frame_end: float
    tier: TakeBudgetTier

    @property
    def total_points(self) -> int:
        return self.keyframe_points + self.sampled_points

    @property
    def duration_frames(self) -> float:
        return max(0.0, self.frame_end - self.frame_start)


def analyze_action(action: bpy.types.Action) -> TakeAnalysis:
    if action is None:
        raise ValueError("action is required")
    frame_start, frame_end = (float(value) for value in action.frame_range)
    if not math.isfinite(frame_start) or not math.isfinite(frame_end):
        raise ValueError("Action frame range must be finite")

    curve_count = 0
    keyframe_points = 0
    sampled_points = 0
    for curve in iter_action_fcurves(action):
        curve_count += 1
        keyframe_points += len(curve.keyframe_points)
        sampled_points += len(curve.sampled_points)
    total_points = keyframe_points + sampled_points
    return TakeAnalysis(
        action.name,
        curve_count,
        keyframe_points,
        sampled_points,
        frame_start,
        frame_end,
        classify_take_size(total_points),
    )


def format_point_count(value: int) -> str:
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000.0:.1f}K"
    return f"{value / 1_000_000.0:.2f}M"
