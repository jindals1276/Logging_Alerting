"""
test_server.py -- Integration tests for the HTTP server.

Spins up a real ThreadedHTTPServer on a random port, sends HTTP
requests via urllib, and asserts on the JSON responses. Each test
class starts a fresh server to avoid cross-test state.

Covers:
  - POST /api/logs (valid batch, single object, invalid JSON, parse errors)
  - GET /api/alerts (empty list, after alert triggered)
  - GET /api/alerts/{id} (found, not found)
  - GET /api/status (before and after data)
  - GET /api/config
  - 404 for unknown endpoints
"""

import json
import threading
import unittest
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

from src.engine import AggregationEngine
from src.models import Config
from src.server import ThreadedHTTPServer, RequestHandler


def make_log_dict(machine="web-01", error_code="ERR_CONN", log_level="Error",
                  message="test", ts_offset_seconds=0):
    """Helper -- create a log entry dict (as JSON would arrive over HTTP)."""
    ts = datetime.now(timezone.utc) + timedelta(seconds=ts_offset_seconds)
    return {
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "machine_name": machine,
        "error_code": error_code,
        "log_level": log_level,
        "message": message,
    }


class ServerTestBase(unittest.TestCase):
    """Base class that starts/stops a test server on a random port."""

    def setUp(self):
        # Use a low threshold for easy alert triggering in tests.
        self.config = Config(
            alert_threshold=3,
            window_duration_seconds=10,
            slide_interval_seconds=1,
            qualifying_log_levels=["Error", "Fatal"],
            late_arrival_grace_seconds=60,
            port=0,  # OS picks a free port.
        )
        self.engine = AggregationEngine(self.config)

        # Create server on port 0 (random free port).
        self.server = ThreadedHTTPServer(("127.0.0.1", 0), RequestHandler)
        self.server.engine = self.engine
        self.server.config = self.config

        # Get the actual port assigned by the OS.
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"

        # Run server in a background thread.
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.daemon = True
        self.server_thread.start()

    def tearDown(self):
        self.engine.shutdown()
        self.server.shutdown()
        self.server_thread.join(timeout=5)

    def _post_json(self, path, data):
        """Send a POST request with JSON body and return (status, parsed_json)."""
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _get_json(self, path):
        """Send a GET request and return (status, parsed_json)."""
        req = urllib.request.Request(f"{self.base_url}{path}", method="GET")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class TestPostLogs(ServerTestBase):
    """Tests for POST /api/logs."""

    def test_post_valid_batch(self):
        """A valid batch of log entries should be accepted."""
        logs = [make_log_dict() for _ in range(2)]
        status, body = self._post_json("/api/logs", logs)
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 2)
        self.assertEqual(body["parse_errors"], 0)

    def test_post_single_object(self):
        """A single log object (not wrapped in an array) should be accepted."""
        log = make_log_dict()
        status, body = self._post_json("/api/logs", log)
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 1)

    def test_post_triggers_alert(self):
        """When the batch causes the threshold to be reached, the response
        should include the alert details."""
        logs = [make_log_dict() for _ in range(3)]  # threshold is 3
        status, body = self._post_json("/api/logs", logs)
        self.assertEqual(status, 200)
        self.assertIn("alert", body)
        self.assertIn("alert_id", body["alert"])
        self.assertEqual(body["alert"]["total_count"], 3)

    def test_post_with_parse_errors(self):
        """Entries with unparseable timestamps should be counted as parse_errors
        but not reject the whole batch."""
        logs = [
            make_log_dict(),  # valid
            {"timestamp": "bad-date", "machine_name": "m1",
             "error_code": "E1", "log_level": "Error", "message": ""},  # invalid
        ]
        status, body = self._post_json("/api/logs", logs)
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 1)
        self.assertEqual(body["parse_errors"], 1)

    def test_post_invalid_json(self):
        """A request with invalid JSON should return 400."""
        body = b"not json"
        req = urllib.request.Request(
            f"{self.base_url}/api/logs",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                self.fail("Should have raised HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
            resp_body = json.loads(e.read())
            self.assertIn("error", resp_body)

    def test_post_empty_batch(self):
        """An empty array should return accepted=0 with no errors."""
        status, body = self._post_json("/api/logs", [])
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 0)
        self.assertEqual(body["parse_errors"], 0)


class TestGetAlerts(ServerTestBase):
    """Tests for GET /api/alerts and GET /api/alerts/{id}."""

    def test_get_alerts_empty(self):
        """Before any alerts, GET /api/alerts should return an empty list."""
        status, body = self._get_json("/api/alerts")
        self.assertEqual(status, 200)
        self.assertEqual(body, [])

    def test_get_alerts_after_trigger(self):
        """After triggering an alert, it should appear in the alerts list."""
        # Trigger an alert.
        logs = [make_log_dict() for _ in range(3)]
        self._post_json("/api/logs", logs)

        status, body = self._get_json("/api/alerts")
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 1)
        self.assertIn("alert_id", body[0])

    def test_get_alert_by_id(self):
        """GET /api/alerts/{id} should return the matching alert."""
        # Trigger an alert and capture its ID.
        logs = [make_log_dict() for _ in range(3)]
        _, post_body = self._post_json("/api/logs", logs)
        alert_id = post_body["alert"]["alert_id"]

        status, body = self._get_json(f"/api/alerts/{alert_id}")
        self.assertEqual(status, 200)
        self.assertEqual(body["alert_id"], alert_id)

    def test_get_alert_not_found(self):
        """GET /api/alerts/{id} with a bad ID should return 404."""
        status, body = self._get_json("/api/alerts/nonexistent-id")
        self.assertEqual(status, 404)
        self.assertIn("error", body)


class TestGetStatus(ServerTestBase):
    """Tests for GET /api/status."""

    def test_status_before_data(self):
        """Status should show null window and zero counts initially."""
        status, body = self._get_json("/api/status")
        self.assertEqual(status, 200)
        self.assertIsNone(body["window_start"])
        self.assertEqual(body["current_count"], 0)
        self.assertEqual(body["total_alerts"], 0)

    def test_status_after_data(self):
        """Status should reflect ingested log count."""
        logs = [make_log_dict() for _ in range(2)]
        self._post_json("/api/logs", logs)

        status, body = self._get_json("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["current_count"], 2)
        self.assertIsNotNone(body["window_start"])


class TestGetConfig(ServerTestBase):
    """Tests for GET /api/config."""

    def test_config_returns_active_values(self):
        """GET /api/config should return the running configuration."""
        status, body = self._get_json("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(body["alert_threshold"], 3)
        self.assertEqual(body["window_duration_seconds"], 10)
        self.assertEqual(body["qualifying_log_levels"], ["Error", "Fatal"])
        self.assertEqual(body["late_arrival_grace_seconds"], 60)


class TestNotFound(ServerTestBase):
    """Tests for unknown endpoints."""

    def test_get_unknown_path(self):
        """GET to an unknown path should return 404."""
        status, body = self._get_json("/api/unknown")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_post_unknown_path(self):
        """POST to an unknown path should return 404."""
        status, body = self._post_json("/api/unknown", {})
        self.assertEqual(status, 404)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
