"""Tests for the live layer: series, rates, burn projections and freshness."""

from __future__ import annotations

import os
import tempfile
import time
import unittest

from app.server import GaugeService, _now_ms


def _snapshot(
    fetched_at: int,
    *,
    five_used: float,
    weekly_used: float,
    monthly_used: float,
    reset_in_ms: int = 4 * 3_600_000,
) -> dict[str, object]:
    def window(kind: str, label: str, used: float, cap: float) -> dict[str, object]:
        return {
            "kind": kind,
            "label": label,
            "used": used,
            "cap": cap,
            "remaining": round(cap - used, 3),
            "remaining_pct": round(100 - used / cap * 100, 1),
            "exceeded": used >= cap,
            "reset_at": fetched_at + reset_in_ms,
            "available": True,
            "note": "",
        }

    return {
        "fetched_at": fetched_at,
        "account": {"name": "Test", "user_name": "test"},
        "plan": {"name": "GOAT", "status": "active"},
        "credits": {"monthly_remaining": round(70 - monthly_used, 2)},
        "windows": {
            "five_hour": window("five_hour", "5 小时", five_used, 14.0),
            "weekly": window("weekly", "本周", weekly_used, 35.0),
            "monthly": window("monthly", "本月", monthly_used, 70.0),
        },
        "usage": {"total_count": 10, "total_cost": 1.0},
        "alerts": [],
    }


class LiveTestCaseBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._previous_data = os.environ.get("GOATGAUGE_DATA")
        os.environ["GOATGAUGE_DATA"] = self._tmp.name
        self.service = GaugeService()
        # A key detected from this machine would make the freshness states
        # depend on whoever runs the suite, so start every test unconfigured.
        self.service.clear_key()

    def tearDown(self) -> None:
        self.service.store.close()
        if self._previous_data is None:
            os.environ.pop("GOATGAUGE_DATA", None)
        else:
            os.environ["GOATGAUGE_DATA"] = self._previous_data
        self._tmp.cleanup()

    def seed_snapshots(self, points: list[tuple[int, float]]) -> None:
        for index, (offset_ms, five_used) in enumerate(points):
            self.service.store.save_snapshot(
                _snapshot(
                    _now_ms() - offset_ms,
                    five_used=five_used,
                    weekly_used=5.0 + index * 0.01,
                    monthly_used=20.0 + index * 0.01,
                )
            )

    def minutes_ago(self, count: int) -> int:
        return count * 60_000


class BurnProjectionTestCase(LiveTestCaseBase):
    def test_steady_climb_yields_expected_burn(self) -> None:
        # 0.1 credit/minute == 6 credits/hour
        self.seed_snapshots(
            [(self.minutes_ago(minute), (60 - minute) * 0.1) for minute in range(60, 0, -1)]
        )
        self.service._snapshot = _snapshot(
            _now_ms(), five_used=6.0, weekly_used=6.0, monthly_used=30.0
        )
        five = self.service.live()["projections"]["five_hour"]
        self.assertEqual(five["verdict"], "before_reset")
        self.assertAlmostEqual(five["burn_per_hour"], 6.0, delta=0.6)
        self.assertIsNotNone(five["exhaust_at"])
        self.assertLess(five["exhaust_at"], five["reset_at"])

    def test_projection_needs_a_live_snapshot_not_just_history(self) -> None:
        self.seed_snapshots(
            [(self.minutes_ago(minute), (60 - minute) * 0.1) for minute in range(60, 0, -1)]
        )
        five = self.service.live()["projections"]["five_hour"]
        self.assertEqual(five["verdict"], "unknown")
        self.assertIsNone(five["remaining"])

    def test_upstream_jitter_is_not_mistaken_for_a_reset(self) -> None:
        # A net climb with small downticks must still yield a slope. Treating
        # rounding jitter as a reset walked the segment back to a single point
        # and collapsed the projection to "unknown".
        points: list[tuple[int, float]] = []
        used = 1.0
        for index in range(60, 0, -1):
            used += -0.01 if index % 7 == 0 else 0.05
            points.append((self.minutes_ago(index), round(used, 4)))
        self.seed_snapshots(points)
        self.service._snapshot = _snapshot(
            _now_ms(), five_used=round(used, 4), weekly_used=5.0, monthly_used=20.0
        )
        five = self.service.live()["projections"]["five_hour"]
        self.assertIsNotNone(five["burn_per_hour"])
        self.assertGreater(five["burn_per_hour"], 1.0)
        self.assertEqual(five["verdict"], "before_reset")

    def test_reset_inside_the_window_excludes_the_earlier_climb(self) -> None:
        climb = [(self.minutes_ago(minute), (60 - minute) * 0.1) for minute in range(60, 20, -1)]
        after = [(self.minutes_ago(minute), 0.2 + (20 - minute) * 0.01) for minute in range(20, 0, -1)]
        self.seed_snapshots(climb + after)
        five = self.service.live()["projections"]["five_hour"]
        self.assertIsNotNone(five["burn_per_hour"])
        self.assertLess(five["burn_per_hour"], 1.5)
        self.assertGreater(five["burn_per_hour"], 0.2)

    def test_no_history_leaves_the_projection_unknown(self) -> None:
        self.service._snapshot = _snapshot(
            _now_ms(), five_used=4.0, weekly_used=4.0, monthly_used=4.0
        )
        five = self.service.live()["projections"]["five_hour"]
        self.assertIsNone(five["burn_per_hour"])
        self.assertEqual(five["verdict"], "unknown")

    def test_exhausted_window_is_reported_as_such(self) -> None:
        self.seed_snapshots(
            [(self.minutes_ago(minute), (60 - minute) * 0.1) for minute in range(60, 0, -1)]
        )
        self.service._snapshot = _snapshot(
            _now_ms(), five_used=14.0, weekly_used=6.0, monthly_used=30.0
        )
        five = self.service.live()["projections"]["five_hour"]
        self.assertEqual(five["verdict"], "exhausted")

    def test_flat_usage_is_idle(self) -> None:
        self.seed_snapshots([(self.minutes_ago(minute), 3.0) for minute in range(60, 0, -1)])
        self.service._snapshot = _snapshot(
            _now_ms(), five_used=3.0, weekly_used=5.0, monthly_used=20.0
        )
        five = self.service.live()["projections"]["five_hour"]
        self.assertEqual(five["verdict"], "idle")


class ActivityRateTestCase(LiveTestCaseBase):
    def record(self, minutes_ago: float, *, tokens: int = 1000) -> dict[str, object]:
        stamp = time.gmtime((time.time() - minutes_ago * 60))
        created = time.strftime("%Y-%m-%dT%H:%M:%S", stamp) + "Z"
        return {
            "id": f"r-{minutes_ago}",
            "created_at": created,
            "model": "deepseek/deepseek-v4.1-flash",
            "tokens_in": tokens,
            "tokens_out": 100,
            "cost_total": 0.05,
            "cost_cache": 0.0,
            "duration_ms": 1000,
            "status": "completed",
        }

    def test_no_cache_buckets_report_no_hit_rate(self) -> None:
        # Absence is not a measured 0.0%: the readout must drop the chip.
        self.assertIsNone(self.service.live()["rates"]["hit_rate_pct"])

    def test_no_records_has_no_basis(self) -> None:
        rates = self.service.live()["rates"]
        self.assertEqual(rates["basis"], "none")
        self.assertIsNone(rates["requests_per_min"])
        self.assertIsNone(rates["cost_per_hour"])

    def test_single_record_is_reported_as_insufficient(self) -> None:
        self.service.store.insert_usage_records([self.record(5)])
        rates = self.service.live()["rates"]
        self.assertEqual(rates["basis"], "single")
        self.assertIsNone(rates["requests_per_min"])

    def test_rate_is_weighted_by_the_active_span(self) -> None:
        # 11 records spread over 10 minutes -> ~1.1 requests/minute
        self.service.store.insert_usage_records(
            [self.record(minute) for minute in range(0, 11)]
        )
        rates = self.service.live()["rates"]
        self.assertEqual(rates["basis"], "active_span")
        self.assertEqual(rates["requests"], 11)
        self.assertAlmostEqual(rates["requests_per_min"], 1.1, delta=0.15)

    def test_generation_speed_is_normalized_by_streaming_time(self) -> None:
        # 11 records spread over ~11 minutes would read as ~1.7 tok/s against
        # the wall clock; against the 11s actually spent streaming it is 100.
        self.service.store.insert_usage_records(
            [self.record(minute) for minute in range(0, 11)]
        )
        rates = self.service.live()["rates"]
        self.assertEqual(rates["duration_ms"], 11_000)
        self.assertAlmostEqual(rates["tokens_per_sec"], 100.0, delta=1.0)

    def test_single_record_still_yields_generation_speed(self) -> None:
        # A lone completed record carries its own tokens and duration, so the
        # rate is defined even though requests/minute is not. Keeping the two
        # together made the chip flicker as the window held one record.
        self.service.store.insert_usage_records([self.record(5)])
        rates = self.service.live()["rates"]
        self.assertEqual(rates["basis"], "single")
        self.assertIsNone(rates["requests_per_min"])
        self.assertEqual(rates["tokens_per_sec"], 100.0)

    def test_generation_speed_is_absent_without_durations(self) -> None:
        record = self.record(1)
        record["duration_ms"] = 0
        self.service.store.insert_usage_records([record, {**record, "id": "r2"}])
        rates = self.service.live()["rates"]
        self.assertIsNone(rates["tokens_per_sec"])

    def test_records_outside_the_window_are_ignored(self) -> None:
        self.service.store.insert_usage_records([self.record(200), self.record(240)])
        rates = self.service.live()["rates"]
        self.assertEqual(rates["requests"], 0)
        self.assertEqual(rates["basis"], "none")


class FreshnessTestCase(LiveTestCaseBase):
    def test_unconfigured_before_any_credential(self) -> None:
        self.assertEqual(self.service.live()["fresh"]["state"], "unconfigured")

    def test_missing_snapshot_is_empty(self) -> None:
        self.service._api_key = "cc-test"
        self.service._credential_mode = "api_key"
        self.assertEqual(self.service.live()["fresh"]["state"], "empty")

    def test_recent_snapshot_is_live(self) -> None:
        self.service._api_key = "cc-test"
        self.service._credential_mode = "api_key"
        self.service._snapshot = _snapshot(
            _now_ms(), five_used=1.0, weekly_used=1.0, monthly_used=1.0
        )
        fresh = self.service.live()["fresh"]
        self.assertEqual(fresh["state"], "live")
        self.assertIsNotNone(fresh["age_ms"])

    def test_old_snapshot_is_stale_not_live(self) -> None:
        self.service.save_settings({"refresh_seconds": 30})
        self.service._api_key = "cc-test"
        self.service._credential_mode = "api_key"
        self.service._snapshot = _snapshot(
            _now_ms() - 90_000, five_used=1.0, weekly_used=1.0, monthly_used=1.0
        )
        self.assertEqual(self.service.live()["fresh"]["state"], "stale")

    def test_error_outweighs_a_fresh_snapshot(self) -> None:
        self.service._api_key = "cc-test"
        self.service._credential_mode = "api_key"
        self.service._snapshot = _snapshot(
            _now_ms(), five_used=1.0, weekly_used=1.0, monthly_used=1.0
        )
        self.service._last_error = "上游 502"
        fresh = self.service.live()["fresh"]
        self.assertEqual(fresh["state"], "error")
        self.assertEqual(fresh["last_error"], "上游 502")


class HostThemeTestCase(LiveTestCaseBase):
    def test_host_theme_is_reported_once_the_injector_knows_it(self) -> None:
        self.assertEqual(self.service.live()["host"]["theme"], "")
        self.service.host_theme = "dark"
        self.assertEqual(self.service.live()["host"]["theme"], "dark")


class StreamTestCase(LiveTestCaseBase):
    def test_watermark_and_capped_rows(self) -> None:
        stamp = time.gmtime(time.time())
        created = time.strftime("%Y-%m-%dT%H:%M:%S", stamp) + "Z"
        self.service.store.insert_usage_records(
            [
                {
                    "id": f"r{index}",
                    "created_at": created,
                    "model": "qwen/qwen3.8-max",
                    "tokens_in": 10,
                    "tokens_out": 1,
                    "cost_total": 0.01,
                    "status": "completed",
                }
                for index in range(40)
            ]
        )
        stream = self.service.live(since_ms=0)["stream"]
        self.assertEqual(stream["new_count"], 40)
        self.assertEqual(len(stream["records"]), 25)
        self.assertGreater(stream["watermark_ms"], 0)

    def test_since_watermark_reports_nothing_new(self) -> None:
        stamp = time.gmtime(time.time())
        created = time.strftime("%Y-%m-%dT%H:%M:%S", stamp) + "Z"
        self.service.store.insert_usage_records(
            [
                {
                    "id": "solo",
                    "created_at": created,
                    "model": "kimi",
                    "tokens_in": 1,
                    "tokens_out": 1,
                    "cost_total": 0.0,
                    "status": "completed",
                }
            ]
        )
        watermark = self.service.live()["stream"]["watermark_ms"]
        self.assertEqual(self.service.live(since_ms=watermark)["stream"]["new_count"], 0)

    def test_negative_since_is_clamped(self) -> None:
        self.assertGreaterEqual(self.service.live(since_ms=-999_999)["stream"]["new_count"], 0)


if __name__ == "__main__":
    unittest.main()
