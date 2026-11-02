from __future__ import annotations

import math


class PoseSendScheduler:
    """Dirty-pose scheduler with a low-rate full-state heartbeat.

    Blender handlers only call :meth:`mark_dirty`; all sampling remains on the
    main-thread timer.  The scheduler is deliberately independent of ``bpy`` so
    its timing and clock-regression behavior can be unit tested with exact
    synthetic timestamps.
    """

    def __init__(self, send_hz: int, *, heartbeat_hz: int = 5) -> None:
        if send_hz not in (30, 60):
            raise ValueError("send_hz must be 30 or 60")
        if heartbeat_hz <= 0 or heartbeat_hz > send_hz:
            raise ValueError("heartbeat_hz must be within (0, send_hz]")
        self._send_interval = 1.0 / send_hz
        self._heartbeat_interval = 1.0 / heartbeat_hz
        self._dirty = True
        self._last_send_time: float | None = None

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def send_interval(self) -> float:
        return self._send_interval

    @property
    def heartbeat_interval(self) -> float:
        return self._heartbeat_interval

    def mark_dirty(self) -> None:
        self._dirty = True

    def should_send(self, monotonic_seconds: float) -> bool:
        now = self._finite_time(monotonic_seconds)
        previous = self._last_send_time
        if previous is None:
            return True
        if now < previous:
            # A synthetic/test clock may regress; recovering immediately is
            # safer than suppressing all future poses until it catches up.
            self._last_send_time = None
            return True
        interval = self._send_interval if self._dirty else self._heartbeat_interval
        return now - previous + 1e-12 >= interval

    def did_send(self, monotonic_seconds: float) -> None:
        self._last_send_time = self._finite_time(monotonic_seconds)
        self._dirty = False

    @staticmethod
    def _finite_time(value: float) -> float:
        value = float(value)
        if not math.isfinite(value) or value < 0.0:
            raise ValueError("monotonic time must be finite and non-negative")
        return value
