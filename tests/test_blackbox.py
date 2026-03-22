"""
Black-box integration tests for the Log Alert Service.

These tests interact exclusively through the HTTP API — they do not inspect
internal state, private methods, or data structures. The service is treated
as an opaque HTTP server and validated against the requirements in
requirements.md.

The test suite **automatically starts a server** on a random port with default
configuration. No pre-running server is required.

To point at an external server instead of auto-starting one, set the
SERVER_URL environment variable:

Run:
    # Auto-start server (default)
    python -m unittest tests.test_blackbox -v

    # Against an external server
    SERVER_URL=http://192.168.1.10:9090 python -m unittest tests.test_blackbox -v

    # On Windows (PowerShell)
    $env:SERVER_URL = "http://192.168.1.10:9090"
    python -m unittest tests.test_blackbox -v

    # Run a specific test class
    python -m unittest tests.test_blackbox.TestAlertTriggering -v
"""

import concurrent.futures
import json
import os
import threading
import time
import unittest
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

from src.engine import AggregationEngine
from src.models import Config
from src.server import ThreadedHTTPServer, RequestHandler


# ---------------------------------------------------------------------------
# Module-level server — auto-started unless SERVER_URL is set
# ---------------------------------------------------------------------------

_module_server = None
_module_engine = None
_module_thread = None
BASE_URL = None  # set in setUpModule


def setUpModule():
    """Start a default-config server on a random port for the test suite."""
    global _module_server, _module_engine, _module_thread, BASE_URL

    external = os.environ.get("SERVER_URL", "").strip()
    if external:
        BASE_URL = external.rstrip("/")
        return

    config = Config()  # default config
    _module_engine = AggregationEngine(config)
    _module_server = ThreadedHTTPServer(("127.0.0.1", 0), RequestHandler)
    _module_server.engine = _module_engine
    _module_server.config = config
    port = _module_server.server_address[1]
    _module_thread = threading.Thread(target=_module_server.serve_forever)
    _module_thread.daemon = True
    _module_thread.start()
    BASE_URL = f"http://127.0.0.1:{port}"


def tearDownModule():
    """Shut down the module-level server if we started one."""
    global _module_server, _module_engine, _module_thread
    if _module_server is not None:
        _module_engine.shutdown()
        _module_server.shutdown()
        _module_thread.join(timeout=5)
        _module_server = None
        _module_engine = None
        _module_thread = None


# ---------------------------------------------------------------------------
# Managed server helpers — for tests that need controlled config / clean state
# ---------------------------------------------------------------------------

def _start_server(config):
    """Start a server on a random port, return (server, engine, port, thread)."""
    engine = AggregationEngine(config)
    server = ThreadedHTTPServer(("127.0.0.1", 0), RequestHandler)
    server.engine = engine
    server.config = config
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever)
    t.daemon = True
    t.start()
    return server, engine, port, t


def _stop_server(server, engine, thread):
    """Cleanly shut down the server and engine."""
    engine.shutdown()
    server.shutdown()
    thread.join(timeout=5)


def _post_logs_to(port, logs):
    """POST logs to a specific port (for managed server tests)."""
    data = json.dumps(logs).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/logs",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get_json_from(port, path):
    """GET a JSON endpoint from a specific port (for managed server tests)."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _url(path):
    return f"{BASE_URL}{path}"


def _post_logs(logs):
    """POST a list of log dicts to /api/logs, return (status_code, body_dict)."""
    data = json.dumps(logs).encode()
    req = urllib.request.Request(
        _url("/api/logs"),
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get_json(path):
    """GET a JSON endpoint, return (status_code, body)."""
    req = urllib.request.Request(_url(path))
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _make_log(machine="web-01", error_code="ERR_CONN", log_level="Error",
              message="test error", ts=None, ts_offset_seconds=0):
    """Build a single log entry dict with sensible defaults."""
    if ts is None:
        ts = datetime.now(timezone.utc) + timedelta(seconds=ts_offset_seconds)
    return {
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "machine_name": machine,
        "error_code": error_code,
        "log_level": log_level,
        "message": message,
    }


def _make_error_logs(count, machine="web-01", error_code="ERR_CONN"):
    """Build a batch of qualifying Error logs at the current time."""
    return [_make_log(machine=machine, error_code=error_code) for _ in range(count)]


# ===================================================================
# 1. LOG INGESTION
# ===================================================================

class TestLogIngestion(unittest.TestCase):
    """Requirement: The service accepts log entries via POST /api/logs,
    individually or in batches."""

    def test_single_log_accepted(self):
        """A single qualifying log entry should be accepted and response
        must contain both 'accepted' and 'parse_errors' fields."""
        status, body = _post_logs([_make_log()])
        self.assertEqual(status, 200)
        self.assertIn("accepted", body)
        self.assertIn("parse_errors", body)
        self.assertGreaterEqual(body["accepted"], 1)

    def test_batch_logs_accepted(self):
        """A batch of log entries should all be accepted."""
        logs = _make_error_logs(5)
        status, body = _post_logs(logs)
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 5)

    def test_empty_batch(self):
        """An empty array should be accepted with 0 counts."""
        status, body = _post_logs([])
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 0)
        self.assertEqual(body["parse_errors"], 0)

    def test_single_object_post(self):
        """POST /api/logs with a single object (not wrapped in array)."""
        log = _make_log()
        data = json.dumps(log).encode()
        req = urllib.request.Request(
            _url("/api/logs"),
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                code = resp.status
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            code = e.code
            body = json.loads(e.read())
        self.assertEqual(code, 200)
        self.assertEqual(body["accepted"], 1)


# ===================================================================
# 2. FILTERING
# ===================================================================

class TestFiltering(unittest.TestCase):
    """Requirement: Only Error/Fatal count toward threshold.
    Stale and far-future logs are discarded."""

    def _get_count(self):
        _, status = _get_json("/api/status")
        return status["current_count"]

    def _get_count_delta(self, logs):
        """Send logs and return how much current_count changed."""
        count_before = self._get_count()
        _post_logs(logs)
        count_after = self._get_count()
        return count_after - count_before

    def test_mixed_batch_only_qualifying_counted(self):
        """In a mixed batch, only Error/Fatal logs count."""
        logs = [
            _make_log(log_level="Error"),
            _make_log(log_level="Info"),
            _make_log(log_level="Fatal"),
            _make_log(log_level="Warning"),
            _make_log(log_level="Debug"),
            _make_log(log_level="Error"),
        ]
        delta = self._get_count_delta(logs)
        self.assertEqual(delta, 3)  # 2 Error + 1 Fatal

    def test_stale_logs_discarded(self):
        """Logs older than the grace period should be discarded."""
        stale_ts = datetime.now(timezone.utc) - timedelta(seconds=120)
        stale_log = _make_log(log_level="Error", ts=stale_ts)
        delta = self._get_count_delta([stale_log])
        self.assertEqual(delta, 0)

    def test_future_logs_beyond_grace_discarded(self):
        """Logs far in the future should be discarded."""
        future_ts = datetime.now(timezone.utc) + timedelta(seconds=300)
        future_log = _make_log(log_level="Error", ts=future_ts)
        delta = self._get_count_delta([future_log])
        self.assertEqual(delta, 0)

    def test_non_qualifying_still_accepted(self):
        """Non-qualifying logs are accepted (not parse errors), just not counted."""
        logs = [_make_log(log_level="Info")]
        status, body = _post_logs(logs)
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 1)
        self.assertEqual(body["parse_errors"], 0)

    def test_slightly_future_accepted(self):
        """Logs slightly in the future (within grace period) should be accepted."""
        future_ts = datetime.now(timezone.utc) + timedelta(seconds=5)
        future_log = _make_log(log_level="Error", ts=future_ts)
        delta = self._get_count_delta([future_log])
        self.assertGreaterEqual(delta, 1,
                                "Log 5s in the future (within 60s grace) should be counted")


# ===================================================================
# 3. SLIDING WINDOW & EVICTION
# ===================================================================

# NOTE: Sliding window eviction tests require a small window_duration_seconds
# (e.g. 5s). With the default 2-hour window they are impractical.
# The "errors within window are counted" case is already covered by
# TestFiltering.test_error_level_counts and test_mixed_batch_only_qualifying_counted.


# ===================================================================
# 3. ALERT TRIGGERING
# ===================================================================

class TestAlertTriggering(unittest.TestCase):
    """Requirement: Alert fires when count >= threshold, includes breakdown,
    window resets after alert.

    These tests read the server's configured threshold via GET /api/config
    and send exactly that many errors to trigger an alert.
    """

    def _get_threshold(self):
        _, config = _get_json("/api/config")
        return config["alert_threshold"]

    def _get_count(self):
        _, status = _get_json("/api/status")
        return status["current_count"]

    def _send_enough_to_trigger(self, machine="web-01", error_code="ERR_CONN"):
        """Send enough errors to trigger an alert, accounting for existing count."""
        threshold = self._get_threshold()
        current = self._get_count()
        needed = max(threshold - current, 0)
        logs = _make_error_logs(needed, machine=machine, error_code=error_code)
        return _post_logs(logs)

    def test_alert_fires_with_correct_structure(self):
        """Triggering an alert should return a well-formed alert object with
        all required fields and a properly structured breakdown."""
        _, body = self._send_enough_to_trigger(machine="db-01",
                                                error_code="ERR_OOM")
        self.assertIn("alert", body)
        self.assertIsNotNone(body["alert"])

        alert = body["alert"]
        for field in ("alert_id", "window_start", "window_end",
                       "total_count", "breakdown", "threshold"):
            self.assertIn(field, alert)

        breakdown = alert["breakdown"]
        self.assertGreater(len(breakdown), 0)
        entry = breakdown[0]
        for field in ("machine_name", "error_code", "count"):
            self.assertIn(field, entry)

        if len(breakdown) > 1:
            counts = [b["count"] for b in breakdown]
            self.assertEqual(counts, sorted(counts, reverse=True),
                             "Breakdown should be sorted by count descending")

    def test_no_alert_below_threshold(self):
        """Sending fewer than threshold errors should NOT trigger an alert."""
        _, body = _post_logs([_make_log(log_level="Error")])
        alert = body.get("alert")
        self.assertTrue(alert is None or alert == {},
                        f"Alert should not fire for 1 log, got: {alert}")

    def test_reset_and_second_alert_with_unique_id(self):
        """After an alert the window resets to 0, a second alert can fire,
        and each alert has a unique ID."""
        _, body1 = self._send_enough_to_trigger()
        self.assertIsNotNone(body1.get("alert"))

        _, status = _get_json("/api/status")
        self.assertEqual(status["current_count"], 0,
                         "Count should reset to 0 after alert fires")

        _, body2 = self._send_enough_to_trigger()
        self.assertIsNotNone(body2.get("alert"))
        self.assertNotEqual(body1["alert"]["alert_id"],
                            body2["alert"]["alert_id"])


# ===================================================================
# 5. ALERT HISTORY API
# ===================================================================

class TestAlertHistory(unittest.TestCase):
    """Requirement: All triggered alerts are stored and queryable."""

    def test_alerts_endpoint_returns_list(self):
        """GET /api/alerts should return a list."""
        status, body = _get_json("/api/alerts")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, list)

    def test_alert_appears_in_history(self):
        """After an alert fires, it should appear in GET /api/alerts."""
        _, config = _get_json("/api/config")
        threshold = config["alert_threshold"]
        current = _get_json("/api/status")[1]["current_count"]
        needed = max(threshold - current, 0)

        _, alerts_before = _get_json("/api/alerts")
        count_before = len(alerts_before)

        _post_logs(_make_error_logs(needed))

        _, alerts_after = _get_json("/api/alerts")
        self.assertGreater(len(alerts_after), count_before)

    def test_get_alert_by_id(self):
        """GET /api/alerts/{id} should return the specific alert with
        analysis fields."""
        _, alerts = _get_json("/api/alerts")
        if len(alerts) == 0:
            _, config = _get_json("/api/config")
            threshold = config["alert_threshold"]
            current = _get_json("/api/status")[1]["current_count"]
            _post_logs(_make_error_logs(max(threshold - current, 0)))
            _, alerts = _get_json("/api/alerts")

        alert_id = alerts[-1]["alert_id"]
        status, alert = _get_json(f"/api/alerts/{alert_id}")
        self.assertEqual(status, 200)
        self.assertEqual(alert["alert_id"], alert_id)
        self.assertIn("analysis_status", alert)
        self.assertIn("analysis", alert)

    def test_get_alert_unknown_id_returns_404(self):
        """GET /api/alerts/{bad-id} should return 404."""
        status, body = _get_json("/api/alerts/nonexistent-id-12345")
        self.assertEqual(status, 404)


# ===================================================================
# 6. STATUS API
# ===================================================================

class TestStatusAPI(unittest.TestCase):
    """Requirement: GET /api/status shows current window state."""

    def test_status_api(self):
        """Status endpoint returns required fields, reflects ingested logs,
        and threshold matches config."""
        _, config = _get_json("/api/config")

        status, body = _get_json("/api/status")
        self.assertEqual(status, 200)
        self.assertIn("current_count", body)
        self.assertIn("threshold", body)
        self.assertIn("progress_pct", body)
        self.assertIn("total_alerts", body)
        self.assertIsInstance(body["total_alerts"], int)
        self.assertEqual(body["threshold"], config["alert_threshold"])

        count_before = body["current_count"]
        _post_logs(_make_error_logs(3))
        count_after = _get_json("/api/status")[1]["current_count"]
        self.assertGreaterEqual(count_after, count_before + 3)


# ===================================================================
# 7. CONFIG API
# ===================================================================

class TestConfigAPI(unittest.TestCase):
    """Requirement: GET /api/config returns active configuration."""

    def test_config_api(self):
        """Config endpoint returns all fields with correct qualifying levels."""
        status, body = _get_json("/api/config")
        self.assertEqual(status, 200)
        self.assertIn("alert_threshold", body)
        self.assertIn("window_duration_seconds", body)
        self.assertIn("qualifying_log_levels", body)
        self.assertIn("late_arrival_grace_seconds", body)
        self.assertIn("Error", body["qualifying_log_levels"])
        self.assertIn("Fatal", body["qualifying_log_levels"])


# ===================================================================
# 8. TIMEZONE HANDLING
# ===================================================================

class TestTimezoneHandling(unittest.TestCase):
    """Requirement: Timestamps normalized to UTC. Supports Z, offsets,
    and bare timestamps (assumed UTC)."""

    def _make_log_raw_ts(self, ts_str, log_level="Error"):
        """Build a log entry dict with a raw timestamp string."""
        return {
            "timestamp": ts_str,
            "machine_name": "tz-test",
            "error_code": "ERR_TZ",
            "log_level": log_level,
            "message": "timezone test",
        }

    def test_z_suffix_accepted(self):
        """Timestamp with Z suffix should be accepted."""
        now = datetime.now(timezone.utc)
        ts_str = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        status, body = _post_logs([self._make_log_raw_ts(ts_str)])
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 1)
        self.assertEqual(body["parse_errors"], 0)

    def test_plus_zero_offset_accepted(self):
        """Timestamp with +00:00 should be accepted."""
        now = datetime.now(timezone.utc)
        ts_str = now.strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"
        status, body = _post_logs([self._make_log_raw_ts(ts_str)])
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 1)

    def test_non_utc_offset_accepted_and_converted(self):
        """Timestamp with +05:30 should be converted to UTC and counted."""
        now_utc = datetime.now(timezone.utc)
        ist = timezone(timedelta(hours=5, minutes=30))
        now_ist = now_utc.astimezone(ist)
        ts_str = now_ist.strftime("%Y-%m-%dT%H:%M:%S") + "+05:30"

        status, body = _post_logs([self._make_log_raw_ts(ts_str)])
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 1)
        self.assertEqual(body["parse_errors"], 0)

    def test_bare_timestamp_assumed_utc(self):
        """Bare timestamp (no timezone) should be assumed UTC and accepted."""
        now = datetime.now(timezone.utc)
        ts_str = now.strftime("%Y-%m-%dT%H:%M:%S.%f")  # no Z, no offset
        status, body = _post_logs([self._make_log_raw_ts(ts_str)])
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 1)

    def test_negative_offset_accepted(self):
        """Timestamp with -05:00 offset should be accepted and converted."""
        now_utc = datetime.now(timezone.utc)
        est = timezone(timedelta(hours=-5))
        now_est = now_utc.astimezone(est)
        ts_str = now_est.strftime("%Y-%m-%dT%H:%M:%S") + "-05:00"

        status, body = _post_logs([self._make_log_raw_ts(ts_str)])
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 1)


# ===================================================================
# 9. ROBUSTNESS / ERROR HANDLING
# ===================================================================

class TestRobustness(unittest.TestCase):
    """Requirement: Malformed entries are skipped without crashing.
    The service stays healthy after bad input."""

    def test_malformed_json_body(self):
        """Sending invalid JSON should return an error, not crash."""
        req = urllib.request.Request(
            _url("/api/logs"),
            data=b"this is not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertGreaterEqual(code, 400)

        # Service should still be healthy
        status, _ = _get_json("/api/status")
        self.assertEqual(status, 200)

    def test_invalid_timestamp_skipped(self):
        """An entry with an unparseable timestamp should be a parse error."""
        logs = [
            {"timestamp": "not-a-date", "machine_name": "m1",
             "error_code": "E1", "log_level": "Error", "message": "bad ts"},
        ]
        status, body = _post_logs(logs)
        self.assertEqual(status, 200)
        self.assertEqual(body["parse_errors"], 1)

    def test_partial_batch_valid_and_invalid(self):
        """Valid entries should be accepted even if others in the batch fail."""
        logs = [
            _make_log(log_level="Error"),
            {"timestamp": "garbage", "machine_name": "m1",
             "error_code": "E1", "log_level": "Error", "message": "bad"},
            _make_log(log_level="Error"),
        ]
        status, body = _post_logs(logs)
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 2)
        self.assertEqual(body["parse_errors"], 1)

    def test_missing_fields_still_parsed(self):
        """An entry with only a timestamp should still be accepted."""
        now = datetime.now(timezone.utc)
        logs = [{"timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}]
        status, body = _post_logs(logs)
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 1)

    def test_unknown_route_returns_404(self):
        """Requests to unknown routes should return 404."""
        req = urllib.request.Request(_url("/api/nonexistent"))
        try:
            with urllib.request.urlopen(req) as resp:
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertEqual(code, 404)

    def test_service_healthy_after_bad_requests(self):
        """After various bad requests, the service should still work."""
        req = urllib.request.Request(
            _url("/api/logs"),
            data=b"[[[broken",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError:
            pass

        # Service should still accept good logs
        status, body = _post_logs([_make_log()])
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["accepted"], 1)

    def test_empty_timestamp_is_parse_error(self):
        """Entry with empty timestamp string should be a parse error."""
        logs = [{"timestamp": "", "machine_name": "m1",
                 "error_code": "E1", "log_level": "Error", "message": "no ts"}]
        status, body = _post_logs(logs)
        self.assertEqual(body["parse_errors"], 1)


# ===================================================================
# 10. CONCURRENCY
# ===================================================================

class TestConcurrency(unittest.TestCase):
    """Requirement: Concurrent requests from multiple machines are
    handled safely without corrupting state."""

    def test_concurrent_posts_no_data_loss(self):
        """Concurrent POST requests should not lose any logs."""
        num_threads = 10
        logs_per_thread = 20
        total_expected = num_threads * logs_per_thread

        count_before = _get_json("/api/status")[1]["current_count"]

        def send_batch(thread_id):
            logs = _make_error_logs(logs_per_thread,
                                    machine=f"machine-{thread_id}")
            _, body = _post_logs(logs)
            return body["accepted"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as pool:
            futures = [pool.submit(send_batch, i) for i in range(num_threads)]
            accepted = sum(f.result() for f in concurrent.futures.as_completed(futures))

        self.assertEqual(accepted, total_expected)

        count_after = _get_json("/api/status")[1]["current_count"]
        self.assertGreaterEqual(count_after - count_before, total_expected)

    def test_concurrent_reads_and_writes(self):
        """Mixing GET and POST requests concurrently should not crash."""
        errors = []

        def post_work():
            try:
                for _ in range(10):
                    _post_logs(_make_error_logs(2))
            except Exception as e:
                errors.append(e)

        def read_work():
            try:
                for _ in range(10):
                    _get_json("/api/status")
                    _get_json("/api/alerts")
            except Exception as e:
                errors.append(e)

        threads = ([threading.Thread(target=post_work) for _ in range(3)] +
                   [threading.Thread(target=read_work) for _ in range(3)])
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertEqual(len(errors), 0, f"Concurrent operations caused errors: {errors}")


# ===================================================================
# 11. MEMORY BOUNDED (Non-Functional)
# ===================================================================

class TestMemoryBounded(unittest.TestCase):
    """Requirement: Memory usage must be bounded — only aggregated counts
    stored, not individual log entries."""

    def test_large_batch_does_not_crash(self):
        """Sending a large batch should succeed without errors."""
        logs = _make_error_logs(500)
        status, body = _post_logs(logs)
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 500)


# ===================================================================
# 12. ISOLATED SERVER TESTS
#     These tests start their own server with controlled config to
#     verify behaviors that require clean state or custom settings.
# ===================================================================

class TestIsolatedSlidingWindow(unittest.TestCase):
    """Sliding window eviction — requires a short window duration."""

    def setUp(self):
        self.config = Config(
            alert_threshold=1000,
            window_duration_seconds=5,
            slide_interval_seconds=1,
            late_arrival_grace_seconds=10,
        )
        self.server, self.engine, self.port, self.thread = _start_server(self.config)

    def tearDown(self):
        _stop_server(self.server, self.engine, self.thread)

    def test_old_errors_evicted_after_window_expires(self):
        """After the window duration passes, old errors should be evicted."""
        _post_logs_to(self.port, _make_error_logs(5))
        _, status_before = _get_json_from(self.port, "/api/status")
        self.assertEqual(status_before["current_count"], 5)

        # Wait longer than the 5s window for eviction
        time.sleep(7)

        _, status_after = _get_json_from(self.port, "/api/status")
        self.assertLess(status_after["current_count"], 5,
                        "Count should decrease after window slides past the old errors")


class TestIsolatedAlertTriggering(unittest.TestCase):
    """Alert tests that require clean state and a known threshold."""

    def setUp(self):
        self.config = Config(
            alert_threshold=5,
            window_duration_seconds=300,
            slide_interval_seconds=1,
            late_arrival_grace_seconds=60,
        )
        self.server, self.engine, self.port, self.thread = _start_server(self.config)

    def tearDown(self):
        _stop_server(self.server, self.engine, self.thread)

    def test_alert_fires_above_threshold(self):
        """Sending more than threshold errors in one batch should trigger."""
        logs = _make_error_logs(10)
        _, body = _post_logs_to(self.port, logs)
        self.assertIn("alert", body)
        self.assertIsNotNone(body["alert"])

    def test_breakdown_counts_accurate(self):
        """Breakdown should show numerically accurate per-machine counts."""
        logs = (_make_error_logs(3, machine="web-01", error_code="ERR_CONN") +
                _make_error_logs(2, machine="web-02", error_code="ERR_TIMEOUT"))
        _, body = _post_logs_to(self.port, logs)
        breakdown = body["alert"]["breakdown"]

        # Build a lookup: (machine, error_code) -> count
        lookup = {(b["machine_name"], b["error_code"]): b["count"]
                  for b in breakdown}
        self.assertEqual(lookup[("web-01", "ERR_CONN")], 3)
        self.assertEqual(lookup[("web-02", "ERR_TIMEOUT")], 2)

    def test_post_reset_straggler_dropped(self):
        """After an alert resets the window, a log timestamped before the
        reset should be dropped (too old for the new window)."""
        # Capture a timestamp from before the alert
        pre_reset_ts = datetime.now(timezone.utc) - timedelta(seconds=2)

        # Trigger an alert to reset the window
        _post_logs_to(self.port, _make_error_logs(5))

        # Now send a log with the pre-reset timestamp
        straggler = _make_log(log_level="Error", ts=pre_reset_ts)
        _post_logs_to(self.port, [straggler])

        _, status = _get_json_from(self.port, "/api/status")
        self.assertEqual(status["current_count"], 0,
                         "Straggler log from before reset should not increment count")


class TestIsolatedAlertHistory(unittest.TestCase):
    """Alert history tests that require clean state."""

    def setUp(self):
        self.config = Config(
            alert_threshold=3,
            window_duration_seconds=300,
            slide_interval_seconds=1,
            late_arrival_grace_seconds=60,
        )
        self.server, self.engine, self.port, self.thread = _start_server(self.config)

    def tearDown(self):
        _stop_server(self.server, self.engine, self.thread)

    def test_alerts_empty_initially(self):
        """GET /api/alerts should return empty list on a fresh server."""
        status, body = _get_json_from(self.port, "/api/alerts")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, list)
        self.assertEqual(len(body), 0)

    def test_multiple_alerts_in_history(self):
        """Multiple triggered alerts should all be in the history."""
        _post_logs_to(self.port, _make_error_logs(3))
        _post_logs_to(self.port, _make_error_logs(3))
        _post_logs_to(self.port, _make_error_logs(3))

        _, alerts = _get_json_from(self.port, "/api/alerts")
        self.assertEqual(len(alerts), 3)


class TestIsolatedStatusAPI(unittest.TestCase):
    """Status tests that require clean state or exact thresholds."""

    def setUp(self):
        self.config = Config(
            alert_threshold=100,
            window_duration_seconds=300,
            slide_interval_seconds=1,
            late_arrival_grace_seconds=60,
        )
        self.server, self.engine, self.port, self.thread = _start_server(self.config)

    def tearDown(self):
        _stop_server(self.server, self.engine, self.thread)

    def test_progress_percentage(self):
        """progress_pct should reflect count / threshold."""
        _post_logs_to(self.port, _make_error_logs(50))
        _, body = _get_json_from(self.port, "/api/status")
        self.assertAlmostEqual(body["progress_pct"], 50.0, delta=0.5)

    def test_status_resets_after_alert(self):
        """After an alert fires, count should reset to 0."""
        cfg = Config(alert_threshold=3, window_duration_seconds=300,
                     slide_interval_seconds=1, late_arrival_grace_seconds=60)
        server, engine, port, thread = _start_server(cfg)
        try:
            _post_logs_to(port, _make_error_logs(3))
            _, body = _get_json_from(port, "/api/status")
            self.assertEqual(body["current_count"], 0)
        finally:
            _stop_server(server, engine, thread)


class TestIsolatedConfigAPI(unittest.TestCase):
    """Config test that requires custom non-default values."""

    def setUp(self):
        self.config = Config(
            alert_threshold=42,
            window_duration_seconds=600,
            slide_interval_seconds=2,
            qualifying_log_levels=["Error", "Fatal"],
            late_arrival_grace_seconds=30,
        )
        self.server, self.engine, self.port, self.thread = _start_server(self.config)

    def tearDown(self):
        _stop_server(self.server, self.engine, self.thread)

    def test_config_reflects_custom_values(self):
        """Config should return the values we passed at startup."""
        _, body = _get_json_from(self.port, "/api/config")
        self.assertEqual(body["alert_threshold"], 42)
        self.assertEqual(body["window_duration_seconds"], 600)
        self.assertEqual(body["late_arrival_grace_seconds"], 30)


class TestCIVerification(unittest.TestCase):
    """Temporary test to verify CI blocks merge on failure."""

    def test_deliberate_failure(self):
        self.fail("This test intentionally fails to verify CI blocks merge")


if __name__ == "__main__":
    unittest.main()
