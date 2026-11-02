"""Blender-independent range selection for motion tools.

Relevant selected keys take precedence over an enabled Preview Range, which in
turn takes precedence over the whole take.  Invalid higher-priority input is an
error rather than a silent fallback, so a tool never edits or scans more motion
than the user indicated.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Final


_FRAME_EPSILON: Final = 1.0e-6


class RangeSource(str, Enum):
    SELECTED_KEYS = "selected_keys"
    PREVIEW_RANGE = "preview_range"
    WHOLE_TAKE = "whole_take"


@dataclass(frozen=True, slots=True)
class RangeSpec:
    """One inclusive Scene-frame range with an explicit source."""

    scene_start: float
    scene_end: float
    source: RangeSource

    def __post_init__(self) -> None:
        start = _finite_frame(self.scene_start, "確認範囲の開始")
        end = _finite_frame(self.scene_end, "確認範囲の終了")
        if end < start - _FRAME_EPSILON:
            raise ValueError("確認範囲の開始を終了より前にしてください")
        if not isinstance(self.source, RangeSource):
            raise TypeError("range source must be a RangeSource")
        object.__setattr__(self, "scene_start", start)
        object.__setattr__(self, "scene_end", end)

    def expanded_integer_bounds(self) -> tuple[int, int]:
        """Return inclusive integer frames, absorbing tiny mapping noise."""

        start = math.floor(self.scene_start + _FRAME_EPSILON)
        end = math.ceil(self.scene_end - _FRAME_EPSILON)
        return start, max(start, end)

    @property
    def integer_sample_count(self) -> int:
        start, end = self.expanded_integer_bounds()
        return end - start + 1


def resolve_range_spec(
    *,
    whole_take_range: tuple[float, float] | None,
    selected_key_range: tuple[float, float] | None = None,
    preview_range: tuple[float, float] | None = None,
    minimum_duration: float = 0.0,
) -> RangeSpec:
    """Resolve selected > Preview > whole-take range without silent fallback."""

    minimum = _finite_frame(minimum_duration, "最小範囲")
    if minimum < 0.0:
        raise ValueError("minimum_duration must not be negative")
    whole = _candidate(
        whole_take_range,
        RangeSource.WHOLE_TAKE,
        reverse_message="モーション全体の範囲を取得できません",
    )
    if whole is None:
        raise ValueError("モーション全体の範囲を取得できません")

    if selected_key_range is not None:
        selected = _candidate(
            selected_key_range,
            RangeSource.SELECTED_KEYS,
            reverse_message="2つ以上の修正キーを選択してください",
        )
        assert selected is not None
        _require_duration(
            selected,
            minimum,
            "2つ以上の修正キーを選択してください",
        )
        _require_contained(
            selected,
            whole,
            "選択キーがモーション全体の範囲外です",
        )
        return selected

    if preview_range is not None:
        preview = _candidate(
            preview_range,
            RangeSource.PREVIEW_RANGE,
            reverse_message="プレビュー範囲の開始を終了より前にしてください",
        )
        assert preview is not None
        _require_duration(
            preview,
            minimum,
            "プレビュー範囲を2フレーム以上にしてください",
        )
        _require_contained(
            preview,
            whole,
            "プレビュー範囲をモーション全体の内側に設定してください",
        )
        return preview

    _require_duration(
        whole,
        minimum,
        "モーション全体に2フレーム以上必要です",
    )
    return whole


def centered_preview_range(
    whole_take: RangeSpec,
    current_frame: float,
    *,
    maximum_samples: int,
) -> RangeSpec:
    """Return a bounded inclusive Preview Range containing current_frame."""

    if not isinstance(whole_take, RangeSpec):
        raise TypeError("whole_take must be a RangeSpec")
    if type(maximum_samples) is not int or maximum_samples <= 0:
        raise ValueError("maximum_samples must be a positive integer")
    current = _finite_frame(current_frame, "現在フレーム")
    whole_start, whole_end = whole_take.expanded_integer_bounds()
    # Every consumer samples integer Scene frames.  Validate in that same
    # effective domain so a fractional strip such as 1.5..100.5 consistently
    # owns the expanded frames 1..101 as well.
    if current < whole_start - _FRAME_EPSILON or current > (
        whole_end + _FRAME_EPSILON
    ):
        raise ValueError("現在フレームをモーション全体の内側へ移動してください")

    count = min(maximum_samples, whole_end - whole_start + 1)
    center = min(whole_end, max(whole_start, math.floor(current + 0.5)))
    start = center - ((count - 1) // 2)
    end = start + count - 1
    if start < whole_start:
        start = whole_start
        end = start + count - 1
    if end > whole_end:
        end = whole_end
        start = end - count + 1
    return RangeSpec(float(start), float(end), RangeSource.PREVIEW_RANGE)


def _finite_frame(value: object, label: str) -> float:
    if type(value) not in (int, float):
        raise TypeError(f"{label}は数値で指定してください")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label}を正しく指定してください")
    return result


def _candidate(
    value: tuple[float, float] | None,
    source: RangeSource,
    *,
    reverse_message: str,
) -> RangeSpec | None:
    if value is None:
        return None
    if type(value) is not tuple or len(value) != 2:
        raise TypeError(reverse_message)
    try:
        start = _finite_frame(value[0], "範囲の開始")
        end = _finite_frame(value[1], "範囲の終了")
    except (TypeError, ValueError) as error:
        raise ValueError(reverse_message) from error
    if end < start - _FRAME_EPSILON:
        raise ValueError(reverse_message)
    return RangeSpec(start, end, source)


def _require_duration(spec: RangeSpec, minimum: float, message: str) -> None:
    if spec.scene_end - spec.scene_start < minimum - _FRAME_EPSILON:
        raise ValueError(message)


def _require_contained(
    candidate: RangeSpec,
    whole: RangeSpec,
    message: str,
) -> None:
    candidate_start, candidate_end = candidate.expanded_integer_bounds()
    whole_start, whole_end = whole.expanded_integer_bounds()
    if candidate_start < whole_start or candidate_end > whole_end:
        raise ValueError(message)


__all__ = [
    "RangeSource",
    "RangeSpec",
    "centered_preview_range",
    "resolve_range_spec",
]
