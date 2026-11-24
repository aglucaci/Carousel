"""Schedule periods and next-scan times (plan task 15); no database needed."""

import datetime as dt
import unittest

from carousel.workers import schedule as sched

UTC = dt.timezone.utc
WED = dt.datetime(2026, 11, 25, 15, 30, tzinfo=UTC)  # a Wednesday


class PeriodTests(unittest.TestCase):
    def test_period_start(self) -> None:
        self.assertEqual(sched.period_start("daily", WED), dt.datetime(2026, 11, 25, tzinfo=UTC))
        self.assertEqual(sched.period_start("weekly", WED), dt.datetime(2026, 11, 23, tzinfo=UTC))  # Monday
        monday = dt.datetime(2026, 11, 23, tzinfo=UTC)
        self.assertEqual(sched.period_start("weekly", monday), monday)
        # Other time zones and naive times (taken as UTC) land in the same UTC period.
        tokyo = dt.datetime(2026, 11, 26, 0, 30, tzinfo=dt.timezone(dt.timedelta(hours=9)))
        self.assertEqual(sched.period_start("daily", tokyo), dt.datetime(2026, 11, 25, tzinfo=UTC))
        self.assertEqual(sched.period_start("daily", WED.replace(tzinfo=None)), dt.datetime(2026, 11, 25, tzinfo=UTC))
        with self.assertRaises(ValueError):
            sched.period_start("0 13 * * *", WED)

    def test_next_scan_at(self) -> None:
        self.assertIsNone(sched.next_scan_at(None, "active", None, WED))
        self.assertIsNone(sched.next_scan_at("daily", "paused", None, WED))
        self.assertEqual(sched.next_scan_at("daily", "active", None, WED), WED)  # never run: due now
        yesterday = WED - dt.timedelta(days=1)
        self.assertEqual(sched.next_scan_at("daily", "active", yesterday, WED), WED)
        this_morning = WED.replace(hour=1)
        self.assertEqual(sched.next_scan_at("daily", "active", this_morning, WED), dt.datetime(2026, 11, 26, tzinfo=UTC))
        self.assertEqual(sched.next_scan_at("weekly", "active", this_morning, WED), dt.datetime(2026, 11, 30, tzinfo=UTC))
        last_week = dt.datetime(2026, 11, 22, 23, tzinfo=UTC)  # Sunday
        self.assertEqual(sched.next_scan_at("weekly", "active", last_week, WED), WED)

    def test_validate(self) -> None:
        self.assertEqual(sched.validate("weekly"), "weekly")
        self.assertIsNone(sched.validate(None))
        with self.assertRaises(ValueError):
            sched.validate("hourly")


class RunForeverTests(unittest.TestCase):
    def test_errors_do_not_stop_the_loop(self) -> None:
        calls, sleeps = [], []
        def broken_sessions():
            calls.append(1)
            raise RuntimeError("database down")
        with self.assertLogs("carousel.scheduler", "ERROR"):
            sched.run_forever(broken_sessions, queue=None, interval=5, ticks=3, sleep=sleeps.append)
        self.assertEqual((len(calls), sleeps), (3, [5, 5]))


if __name__ == "__main__":
    unittest.main()
