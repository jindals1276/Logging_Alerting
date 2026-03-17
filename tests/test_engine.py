"""
test_engine.py — Unit tests for the AggregationEngine.

Covers:
  - Filter pipeline (level check, late arrival, future timestamp, window bound)
  - Aggregation (count, breakdown map, time bucketing)
  - Threshold triggering and alert generation
  - Window reset after alert
  - Window sliding and bucket eviction
  - Edge cases (empty batch, successive alerts)
  - Status API
"""

import unittest
from datetime import datetime, timedelta, timezone

from src.engine import AggregationEngine
from src.models import Config, LogEntry


def make_entry(machine="web-01", error_code="ERR_CONN", log_level="Error",
               message="test", ts_offset_seconds=0):
    """Helper — create a LogEntry with timestamp relative to now.

    ts_offset_seconds: negative = in the past, positive = in the future.
    """
    ts = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=ts_offset_seconds)
    return LogEntry(
        timestamp=ts,
        machine_name=machine,
        error_code=error_code,
        log_level=log_level,
        message=message,
    )


def make_config(**overrides):
    """Helper — create a Config with test-friendly defaults.

    Uses a low threshold (5) and short window (10s) so tests run fast.
    """
    defaults = {
        "alert_threshold": 5,
        "window_duration_seconds": 10,
        "slide_interval_seconds": 1,
        "qualifying_log_levels": ["Error", "Fatal"],
        "late_arrival_grace_seconds": 60,
        "port": 8080,
    }
    defaults.update(overrides)
    return Config(**defaults)


class TestFilterPipeline(unittest.TestCase):
    """Tests for the 4-step filter pipeline in process_batch()."""

    def setUp(self):
        self.config = make_config()
        self.engine = AggregationEngine(self.config)

    def tearDown(self):
        self.engine.shutdown()

    def test_qualifying_levels_pass_through(self):
        """Error and Fatal logs should be counted."""
        entries = [
            make_entry(log_level="Error"),
            make_entry(log_level="Fatal"),
        ]
        self.engine.process_batch(entries)
        status = self.engine.get_status()
        self.assertEqual(status["current_count"], 2)

    def test_non_qualifying_levels_are_filtered(self):
        """Warning, Info, Debug logs should be silently dropped."""
        entries = [
            make_entry(log_level="Warning"),
            make_entry(log_level="Info"),
            make_entry(log_level="Debug"),
        ]
        self.engine.process_batch(entries)
        status = self.engine.get_status()
        self.assertEqual(status["current_count"], 0)

    def test_level_check_is_case_insensitive(self):
        """'error', 'ERROR', 'Error' should all pass the level check."""
        entries = [
            make_entry(log_level="error"),
            make_entry(log_level="ERROR"),
            make_entry(log_level="Error"),
        ]
        self.engine.process_batch(entries)
        status = self.engine.get_status()
        self.assertEqual(status["current_count"], 3)

    def test_late_arrival_is_dropped(self):
        """Logs older than the grace period should be discarded."""
        # Grace period is 60s; this log is 120s old.
        entries = [make_entry(ts_offset_seconds=-120)]
        self.engine.process_batch(entries)
        status = self.engine.get_status()
        self.assertEqual(status["current_count"], 0)

    def test_within_grace_period_is_accepted(self):
        """Logs within the grace period should be accepted."""
        # 10 seconds old, well within 60s grace.
        entries = [make_entry(ts_offset_seconds=-10)]
        self.engine.process_batch(entries)
        status = self.engine.get_status()
        self.assertEqual(status["current_count"], 1)

    def test_slightly_future_timestamp_is_accepted(self):
        """Logs slightly in the future (clock skew, within grace period)
        should be accepted and clamped to server time for bucketing."""
        # 5 seconds in the future, within 60s grace.
        entries = [make_entry(ts_offset_seconds=5)]
        self.engine.process_batch(entries)
        status = self.engine.get_status()
        self.assertEqual(status["current_count"], 1)

    def test_far_future_timestamp_is_dropped(self):
        """Logs too far in the future (beyond the grace period) should be
        discarded — indicates a wildly wrong clock or fabricated timestamp."""
        # 120 seconds in the future, beyond 60s grace.
        entries = [make_entry(ts_offset_seconds=120)]
        self.engine.process_batch(entries)
        status = self.engine.get_status()
        self.assertEqual(status["current_count"], 0)

    def test_future_log_clamped_to_server_time(self):
        """A future-dated log (within grace) should be bucketed at server
        time, not at its own timestamp. This prevents the log from landing
        in a bucket beyond the window's end boundary."""
        config = make_config(alert_threshold=100)
        engine = AggregationEngine(config)
        try:
            # 10 seconds in the future.
            entries = [make_entry(ts_offset_seconds=10)]
            engine.process_batch(entries)

            with engine._lock:
                # The bucket key should be at or before 'now', not 10s ahead.
                for bucket_key in engine._buckets:
                    self.assertLessEqual(bucket_key, datetime.now(timezone.utc).replace(tzinfo=None))
        finally:
            engine.shutdown()

    def test_pre_window_straggler_is_dropped(self):
        """After a window reset, logs dated before the new window_start
        should be filtered out by the window bound check."""
        config = make_config(alert_threshold=2)
        engine = AggregationEngine(config)
        try:
            # Trigger an alert to reset the window.
            entries = [make_entry() for _ in range(2)]
            alert = engine.process_batch(entries)
            self.assertIsNotNone(alert)

            # Now send a log with a timestamp from before the reset.
            # It should be dropped by the window bound check.
            old_entry = LogEntry(
                timestamp=alert.window_start - timedelta(seconds=5),
                machine_name="web-01",
                error_code="ERR_CONN",
                log_level="Error",
                message="straggler",
            )
            engine.process_batch([old_entry])
            status = engine.get_status()
            self.assertEqual(status["current_count"], 0)
        finally:
            engine.shutdown()

    def test_mixed_batch_filters_correctly(self):
        """A batch with a mix of qualifying and non-qualifying entries
        should only count the qualifying ones."""
        entries = [
            make_entry(log_level="Error"),       # pass
            make_entry(log_level="Info"),         # filtered: wrong level
            make_entry(log_level="Fatal"),        # pass
            make_entry(log_level="Warning"),      # filtered: wrong level
            make_entry(ts_offset_seconds=-120),   # filtered: late arrival
            make_entry(ts_offset_seconds=120),    # filtered: far future
        ]
        self.engine.process_batch(entries)
        status = self.engine.get_status()
        self.assertEqual(status["current_count"], 2)


class TestAggregation(unittest.TestCase):
    """Tests for count tracking, breakdown map, and time bucketing."""

    def setUp(self):
        # High threshold to avoid triggering alerts during aggregation tests.
        self.config = make_config(alert_threshold=100)
        self.engine = AggregationEngine(self.config)

    def tearDown(self):
        self.engine.shutdown()

    def test_count_increments_correctly(self):
        """Count should match the number of qualifying entries ingested."""
        entries = [make_entry() for _ in range(7)]
        self.engine.process_batch(entries)
        self.assertEqual(self.engine.get_status()["current_count"], 7)

    def test_count_accumulates_across_batches(self):
        """Multiple batches should add to the running count."""
        self.engine.process_batch([make_entry() for _ in range(3)])
        self.engine.process_batch([make_entry() for _ in range(4)])
        self.assertEqual(self.engine.get_status()["current_count"], 7)

    def test_breakdown_tracks_machine_error_pairs(self):
        """The global breakdown should reflect counts per (machine, error_code)."""
        entries = [
            make_entry(machine="web-01", error_code="ERR_CONN"),
            make_entry(machine="web-01", error_code="ERR_CONN"),
            make_entry(machine="web-02", error_code="ERR_TIMEOUT"),
            make_entry(machine="web-01", error_code="ERR_TIMEOUT"),
        ]
        self.engine.process_batch(entries)

        # Inspect the global breakdown map (white-box test).
        with self.engine._lock:
            self.assertEqual(self.engine._breakdown[("web-01", "ERR_CONN")], 2)
            self.assertEqual(self.engine._breakdown[("web-02", "ERR_TIMEOUT")], 1)
            self.assertEqual(self.engine._breakdown[("web-01", "ERR_TIMEOUT")], 1)

    def test_logs_placed_into_time_buckets(self):
        """Logs with the same truncated timestamp should land in the same bucket."""
        # All entries have ts_offset=0, so they share the same second → same bucket.
        entries = [make_entry() for _ in range(5)]
        self.engine.process_batch(entries)

        with self.engine._lock:
            # Should be exactly 1 bucket since all logs are in the same second.
            self.assertEqual(len(self.engine._buckets), 1)
            bucket = next(iter(self.engine._buckets.values()))
            self.assertEqual(bucket.count, 5)

    def test_different_seconds_create_different_buckets(self):
        """Logs with different truncated timestamps should land in separate buckets."""
        entries = [
            make_entry(ts_offset_seconds=-3),  # 3 seconds ago
            make_entry(ts_offset_seconds=-1),  # 1 second ago
            make_entry(ts_offset_seconds=0),   # now
        ]
        self.engine.process_batch(entries)

        with self.engine._lock:
            # Should have up to 3 buckets (one per distinct second).
            # Could be 2 if -1 and 0 round to the same second, but at least 2.
            self.assertGreaterEqual(len(self.engine._buckets), 2)

    def test_window_start_initialized_on_first_log(self):
        """window_start should be None before any data, then set after
        the first qualifying log."""
        status = self.engine.get_status()
        self.assertIsNone(status["window_start"])

        self.engine.process_batch([make_entry()])
        status = self.engine.get_status()
        self.assertIsNotNone(status["window_start"])

    def test_window_not_initialized_by_non_qualifying_log(self):
        """A non-qualifying log (e.g. Info) should NOT initialize the window."""
        self.engine.process_batch([make_entry(log_level="Info")])
        status = self.engine.get_status()
        self.assertIsNone(status["window_start"])


class TestThresholdAndAlert(unittest.TestCase):
    """Tests for threshold checking and alert generation."""

    def test_alert_fires_when_threshold_reached(self):
        """An alert should be returned when count >= threshold."""
        config = make_config(alert_threshold=3)
        engine = AggregationEngine(config)
        try:
            entries = [make_entry() for _ in range(3)]
            alert = engine.process_batch(entries)
            self.assertIsNotNone(alert)
            self.assertEqual(alert.total_count, 3)
            self.assertEqual(alert.threshold, 3)
        finally:
            engine.shutdown()

    def test_alert_fires_when_threshold_exceeded(self):
        """An alert should fire even if count > threshold (overshoot in a batch)."""
        config = make_config(alert_threshold=3)
        engine = AggregationEngine(config)
        try:
            entries = [make_entry() for _ in range(5)]
            alert = engine.process_batch(entries)
            self.assertIsNotNone(alert)
            self.assertEqual(alert.total_count, 5)
        finally:
            engine.shutdown()

    def test_no_alert_below_threshold(self):
        """No alert should fire if count < threshold."""
        config = make_config(alert_threshold=10)
        engine = AggregationEngine(config)
        try:
            entries = [make_entry() for _ in range(5)]
            alert = engine.process_batch(entries)
            self.assertIsNone(alert)
        finally:
            engine.shutdown()

    def test_alert_breakdown_sorted_by_count_descending(self):
        """Alert breakdown should list the biggest contributor first."""
        config = make_config(alert_threshold=5)
        engine = AggregationEngine(config)
        try:
            entries = [
                make_entry(machine="web-01", error_code="E1"),
                make_entry(machine="web-02", error_code="E2"),
                make_entry(machine="web-02", error_code="E2"),
                make_entry(machine="web-02", error_code="E2"),
                make_entry(machine="web-01", error_code="E1"),
            ]
            alert = engine.process_batch(entries)
            self.assertIsNotNone(alert)
            # web-02/E2 has 3 hits, web-01/E1 has 2 — should be first.
            self.assertEqual(alert.breakdown[0]["machine_name"], "web-02")
            self.assertEqual(alert.breakdown[0]["count"], 3)
            self.assertEqual(alert.breakdown[1]["machine_name"], "web-01")
            self.assertEqual(alert.breakdown[1]["count"], 2)
        finally:
            engine.shutdown()

    def test_alert_stored_in_history(self):
        """Triggered alerts should be retrievable via get_alerts()."""
        config = make_config(alert_threshold=2)
        engine = AggregationEngine(config)
        try:
            engine.process_batch([make_entry() for _ in range(2)])
            alerts = engine.get_alerts()
            self.assertEqual(len(alerts), 1)
            self.assertIn("alert_id", alerts[0])
        finally:
            engine.shutdown()

    def test_get_alert_by_id(self):
        """A specific alert should be retrievable by its ID."""
        config = make_config(alert_threshold=2)
        engine = AggregationEngine(config)
        try:
            alert = engine.process_batch([make_entry() for _ in range(2)])
            result = engine.get_alert(alert.alert_id)
            self.assertIsNotNone(result)
            self.assertEqual(result["alert_id"], alert.alert_id)
        finally:
            engine.shutdown()

    def test_get_alert_nonexistent_returns_none(self):
        """Querying a non-existent alert ID should return None."""
        config = make_config()
        engine = AggregationEngine(config)
        try:
            result = engine.get_alert("nonexistent-id")
            self.assertIsNone(result)
        finally:
            engine.shutdown()


class TestWindowReset(unittest.TestCase):
    """Tests for window reset behavior after an alert fires."""

    def test_count_resets_after_alert(self):
        """After an alert, the count should be back to zero."""
        config = make_config(alert_threshold=3)
        engine = AggregationEngine(config)
        try:
            engine.process_batch([make_entry() for _ in range(3)])
            status = engine.get_status()
            self.assertEqual(status["current_count"], 0)
        finally:
            engine.shutdown()

    def test_breakdown_resets_after_alert(self):
        """After an alert, the global breakdown map should be empty."""
        config = make_config(alert_threshold=3)
        engine = AggregationEngine(config)
        try:
            engine.process_batch([make_entry() for _ in range(3)])
            with engine._lock:
                self.assertEqual(len(engine._breakdown), 0)
        finally:
            engine.shutdown()

    def test_buckets_cleared_after_alert(self):
        """After an alert, all time buckets should be removed."""
        config = make_config(alert_threshold=3)
        engine = AggregationEngine(config)
        try:
            engine.process_batch([make_entry() for _ in range(3)])
            with engine._lock:
                self.assertEqual(len(engine._buckets), 0)
        finally:
            engine.shutdown()

    def test_new_logs_counted_after_reset(self):
        """After a reset, new qualifying logs should start a fresh count."""
        config = make_config(alert_threshold=3)
        engine = AggregationEngine(config)
        try:
            # Trigger first alert.
            engine.process_batch([make_entry() for _ in range(3)])
            # Send 2 more — should NOT trigger (threshold is 3).
            alert = engine.process_batch([make_entry() for _ in range(2)])
            self.assertIsNone(alert)
            self.assertEqual(engine.get_status()["current_count"], 2)
        finally:
            engine.shutdown()

    def test_successive_alerts(self):
        """Multiple alerts can fire across successive batches."""
        config = make_config(alert_threshold=2)
        engine = AggregationEngine(config)
        try:
            alert1 = engine.process_batch([make_entry() for _ in range(2)])
            self.assertIsNotNone(alert1)

            alert2 = engine.process_batch([make_entry() for _ in range(2)])
            self.assertIsNotNone(alert2)

            # Should have 2 alerts in history with different IDs.
            alerts = engine.get_alerts()
            self.assertEqual(len(alerts), 2)
            self.assertNotEqual(alerts[0]["alert_id"], alerts[1]["alert_id"])
        finally:
            engine.shutdown()


class TestWindowSliding(unittest.TestCase):
    """Tests for the _slide_window() bucket eviction logic.

    These tests call _slide_window() directly (white-box) to verify
    eviction behavior without waiting for the background thread.
    """

    def setUp(self):
        # Large threshold so alerts don't interfere with slide tests.
        self.config = make_config(alert_threshold=1000, window_duration_seconds=10)
        self.engine = AggregationEngine(self.config)

    def tearDown(self):
        self.engine.shutdown()

    def test_slide_evicts_old_buckets(self):
        """Buckets with keys before the new window_start should be evicted."""
        # Insert entries with timestamps 5 seconds in the past.
        old_entries = [make_entry(ts_offset_seconds=-5) for _ in range(3)]
        self.engine.process_batch(old_entries)
        self.assertEqual(self.engine.get_status()["current_count"], 3)

        with self.engine._lock:
            # Move window_start forward past the old buckets.
            self.engine._window_start = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=3)
            self.engine._slide_window()

        # The 5s-old bucket should be evicted.
        status = self.engine.get_status()
        self.assertEqual(status["current_count"], 0)

    def test_slide_keeps_recent_buckets(self):
        """Buckets within the new window should survive eviction."""
        entries = [
            make_entry(ts_offset_seconds=-5),  # old — should be evicted
            make_entry(ts_offset_seconds=-1),   # recent — should survive
            make_entry(ts_offset_seconds=0),    # now — should survive
        ]
        self.engine.process_batch(entries)

        with self.engine._lock:
            # Slide window to 3s ago, evicting buckets older than that.
            self.engine._window_start = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=3)
            self.engine._slide_window()

        status = self.engine.get_status()
        self.assertEqual(status["current_count"], 2)

    def test_slide_updates_global_breakdown_on_eviction(self):
        """Evicted bucket counts should be subtracted from the global breakdown."""
        entries = [
            make_entry(machine="web-01", error_code="E1", ts_offset_seconds=-5),
            make_entry(machine="web-01", error_code="E1", ts_offset_seconds=-5),
            make_entry(machine="web-02", error_code="E2", ts_offset_seconds=0),
        ]
        self.engine.process_batch(entries)

        with self.engine._lock:
            self.engine._window_start = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=3)
            self.engine._slide_window()

            # web-01/E1 bucket (5s old) should be evicted and removed
            # from the global breakdown. web-02/E2 should remain.
            self.assertNotIn(("web-01", "E1"), self.engine._breakdown)
            self.assertEqual(self.engine._breakdown[("web-02", "E2")], 1)

    def test_slide_on_empty_buckets_is_safe(self):
        """Sliding when there are no buckets should not raise errors."""
        with self.engine._lock:
            self.engine._window_start = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=15)
            # Should not raise.
            self.engine._slide_window()

        self.assertEqual(self.engine.get_status()["current_count"], 0)


class TestTimeBucketing(unittest.TestCase):
    """Tests specifically for the time bucket truncation logic."""

    def setUp(self):
        self.config = make_config(alert_threshold=1000)
        self.engine = AggregationEngine(self.config)

    def tearDown(self):
        self.engine.shutdown()

    def test_truncate_strips_microseconds(self):
        """With 1-second intervals, microseconds should be zeroed out."""
        ts = datetime(2026, 3, 6, 10, 30, 45, 123456)
        result = self.engine._truncate_to_bucket(ts)
        self.assertEqual(result, datetime(2026, 3, 6, 10, 30, 45, 0))

    def test_truncate_with_5_second_interval(self):
        """With 5-second intervals, timestamp should round down to nearest 5s."""
        config = make_config(slide_interval_seconds=5)
        engine = AggregationEngine(config)
        try:
            ts = datetime(2026, 3, 6, 10, 30, 47, 0)
            result = engine._truncate_to_bucket(ts)
            self.assertEqual(result, datetime(2026, 3, 6, 10, 30, 45, 0))
        finally:
            engine.shutdown()

    def test_truncate_at_exact_boundary(self):
        """A timestamp exactly on a bucket boundary should stay unchanged
        (except microseconds zeroed)."""
        ts = datetime(2026, 3, 6, 10, 30, 0, 0)
        result = self.engine._truncate_to_bucket(ts)
        self.assertEqual(result, datetime(2026, 3, 6, 10, 30, 0, 0))


class TestEmptyBatch(unittest.TestCase):
    """Tests for edge case: empty batch submitted."""

    def test_empty_batch_returns_none(self):
        """An empty batch should not trigger an alert."""
        config = make_config(alert_threshold=1)
        engine = AggregationEngine(config)
        try:
            alert = engine.process_batch([])
            self.assertIsNone(alert)
        finally:
            engine.shutdown()

    def test_empty_batch_does_not_initialize_window(self):
        """An empty batch should not set window_start."""
        config = make_config()
        engine = AggregationEngine(config)
        try:
            engine.process_batch([])
            self.assertIsNone(engine.get_status()["window_start"])
        finally:
            engine.shutdown()


class TestGetStatus(unittest.TestCase):
    """Tests for the get_status() API."""

    def test_status_before_any_data(self):
        """Status should show null window and zero counts before any data."""
        config = make_config()
        engine = AggregationEngine(config)
        try:
            status = engine.get_status()
            self.assertIsNone(status["window_start"])
            self.assertIsNone(status["window_end"])
            self.assertEqual(status["current_count"], 0)
            self.assertEqual(status["progress_pct"], 0.0)
            self.assertEqual(status["total_alerts"], 0)
        finally:
            engine.shutdown()

    def test_status_after_data(self):
        """Status should reflect current count and progress after ingestion."""
        config = make_config(alert_threshold=10)
        engine = AggregationEngine(config)
        try:
            engine.process_batch([make_entry() for _ in range(3)])
            status = engine.get_status()
            self.assertIsNotNone(status["window_start"])
            self.assertIsNotNone(status["window_end"])
            self.assertEqual(status["current_count"], 3)
            self.assertEqual(status["threshold"], 10)
            self.assertEqual(status["progress_pct"], 30.0)
        finally:
            engine.shutdown()

    def test_status_total_alerts_increments(self):
        """total_alerts should increment each time an alert fires."""
        config = make_config(alert_threshold=2)
        engine = AggregationEngine(config)
        try:
            engine.process_batch([make_entry() for _ in range(2)])
            engine.process_batch([make_entry() for _ in range(2)])
            status = engine.get_status()
            self.assertEqual(status["total_alerts"], 2)
        finally:
            engine.shutdown()


if __name__ == "__main__":
    unittest.main()
