from __future__ import annotations

import unittest

from motion_bridge.live.scheduling import PoseSendScheduler


class PoseSendSchedulerTests(unittest.TestCase):
    def test_initial_dirty_pose_sends_immediately_then_obeys_rate_limit(self) -> None:
        scheduler = PoseSendScheduler(60)

        self.assertTrue(scheduler.should_send(10.0))
        scheduler.did_send(10.0)
        scheduler.mark_dirty()
        self.assertFalse(scheduler.should_send(10.01))
        self.assertTrue(scheduler.should_send(10.0 + 1.0 / 60.0))

    def test_clean_pose_uses_five_hertz_heartbeat(self) -> None:
        scheduler = PoseSendScheduler(60, heartbeat_hz=5)
        scheduler.did_send(1.0)

        self.assertFalse(scheduler.should_send(1.199))
        self.assertTrue(scheduler.should_send(1.2))

    def test_dirty_pose_interrupts_heartbeat_at_send_rate(self) -> None:
        scheduler = PoseSendScheduler(30, heartbeat_hz=5)
        scheduler.did_send(4.0)
        scheduler.mark_dirty()

        self.assertFalse(scheduler.should_send(4.02))
        self.assertTrue(scheduler.should_send(4.0 + 1.0 / 30.0))

    def test_clock_regression_and_invalid_time_are_safe(self) -> None:
        scheduler = PoseSendScheduler(60)
        scheduler.did_send(5.0)
        self.assertTrue(scheduler.should_send(4.0))
        scheduler.did_send(4.0)

        for value in (-1.0, float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    scheduler.should_send(value)

    def test_invalid_rates_are_rejected(self) -> None:
        for send_hz, heartbeat_hz in ((24, 5), (60, 0), (30, 31)):
            with self.subTest(send_hz=send_hz, heartbeat_hz=heartbeat_hz):
                with self.assertRaises(ValueError):
                    PoseSendScheduler(send_hz, heartbeat_hz=heartbeat_hz)


if __name__ == "__main__":
    unittest.main()
