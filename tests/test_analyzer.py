"""
test_analyzer.py -- Unit tests for the LLM-based AlertAnalyzer.

Tests cover:
  - Skipped behavior when no API key is configured
  - Successful analysis with mocked API response
  - Graceful failure handling on API errors
  - Verification that LLM analysis does not hold the engine lock
"""

import json
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from src.analyzer import AlertAnalyzer
from src.engine import AggregationEngine
from src.models import Alert, Config, LogEntry


def make_alert():
    """Helper -- create a sample Alert for testing."""
    return Alert.create(
        window_start=datetime(2026, 3, 6, 8, 0, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 3, 6, 10, 0, 0, tzinfo=timezone.utc),
        total_count=1500,
        threshold=1000,
        breakdown=[
            {"machine_name": "web-01", "error_code": "ERR_CONN", "count": 800},
            {"machine_name": "web-02", "error_code": "ERR_TIMEOUT", "count": 500},
            {"machine_name": "web-03", "error_code": "ERR_OOM", "count": 200},
        ],
    )


def make_entry():
    """Helper -- create a qualifying LogEntry at current time."""
    return LogEntry(
        timestamp=datetime.now(timezone.utc),
        machine_name="web-01",
        error_code="ERR_CONN",
        log_level="Error",
        message="test",
    )


class TestAnalyzerDisabled(unittest.TestCase):
    """Tests for when no API key is configured."""

    def test_disabled_without_api_key(self):
        """Analyzer should report disabled when no key is provided."""
        analyzer = AlertAnalyzer(api_key=None)
        self.assertFalse(analyzer.enabled)

    def test_enrich_skips_without_api_key(self):
        """enrich() should set status to 'skipped' and not spawn a thread."""
        analyzer = AlertAnalyzer(api_key=None)
        alert = make_alert()
        analyzer.enrich(alert)

        self.assertEqual(alert.analysis_status, "skipped")
        self.assertIsNone(alert.analysis)

    def test_enrich_skipped_is_synchronous(self):
        """When disabled, enrich() should return immediately (no threads)."""
        analyzer = AlertAnalyzer(api_key=None)
        alert = make_alert()

        # Count active threads before and after.
        threads_before = threading.active_count()
        analyzer.enrich(alert)
        threads_after = threading.active_count()

        # No new threads should be spawned.
        self.assertEqual(threads_before, threads_after)


class TestAnalyzerEnabled(unittest.TestCase):
    """Tests for when an API key is configured (with mocked API calls)."""

    def test_enabled_with_api_key(self):
        """Analyzer should report enabled when a key is provided."""
        analyzer = AlertAnalyzer(api_key="test-key-123")
        self.assertTrue(analyzer.enabled)

    @patch("src.analyzer.urllib.request.urlopen")
    def test_enrich_calls_api_and_sets_analysis(self, mock_urlopen):
        """enrich() should call Claude API and populate alert.analysis."""
        # Mock the API response.
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "content": [{"type": "text", "text": "The errors are concentrated on web-01."}]
        }).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        analyzer = AlertAnalyzer(api_key="test-key-123")
        alert = make_alert()
        analyzer.enrich(alert)

        # Wait for the background thread to finish.
        time.sleep(0.5)

        self.assertEqual(alert.analysis_status, "completed")
        self.assertIn("web-01", alert.analysis)

    @patch("src.analyzer.urllib.request.urlopen")
    def test_prompt_contains_breakdown_data(self, mock_urlopen):
        """The prompt sent to Claude should contain the alert breakdown."""
        # Capture the request body sent to the API.
        captured_body = {}

        def capture_request(req, **kwargs):
            captured_body["data"] = json.loads(req.data)
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({
                "content": [{"type": "text", "text": "analysis"}]
            }).encode("utf-8")
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        mock_urlopen.side_effect = capture_request

        analyzer = AlertAnalyzer(api_key="test-key-123")
        alert = make_alert()
        analyzer.enrich(alert)

        time.sleep(0.5)

        # Verify the prompt includes machine names and error codes.
        prompt = captured_body["data"]["messages"][0]["content"]
        self.assertIn("web-01", prompt)
        self.assertIn("ERR_CONN", prompt)
        self.assertIn("1500", prompt)  # total count
        self.assertIn("1000", prompt)  # threshold

    @patch("src.analyzer.urllib.request.urlopen")
    def test_enrich_handles_api_failure(self, mock_urlopen):
        """If the API call fails, analysis_status should be 'failed'."""
        mock_urlopen.side_effect = Exception("API connection error")

        analyzer = AlertAnalyzer(api_key="test-key-123")
        alert = make_alert()
        analyzer.enrich(alert)

        # Wait for the background thread to finish.
        time.sleep(0.5)

        self.assertEqual(alert.analysis_status, "failed")
        self.assertIsNone(alert.analysis)


class TestAnalyzerDoesNotBlockEngine(unittest.TestCase):
    """Verify that LLM analysis does not hold the engine lock."""

    @patch("src.analyzer.urllib.request.urlopen")
    def test_engine_lock_not_held_during_analysis(self, mock_urlopen):
        """After process_batch triggers an alert, the engine lock should
        be immediately available even while LLM analysis is in progress."""

        # Make the API call slow (simulates 1-2s LLM latency).
        def slow_api_call(req, **kwargs):
            time.sleep(1.0)
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({
                "content": [{"type": "text", "text": "analysis"}]
            }).encode("utf-8")
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        mock_urlopen.side_effect = slow_api_call

        analyzer = AlertAnalyzer(api_key="test-key-123")
        config = Config(alert_threshold=2)
        engine = AggregationEngine(config, analyzer=analyzer)

        try:
            # Trigger an alert.
            entries = [make_entry() for _ in range(2)]
            alert = engine.process_batch(entries)
            self.assertIsNotNone(alert)

            # Immediately try to acquire the engine lock.
            # If the LLM call held the lock, this would block for 1 second.
            lock_acquired = engine._lock.acquire(timeout=0.1)
            self.assertTrue(lock_acquired, "Engine lock should not be held during LLM analysis")
            if lock_acquired:
                engine._lock.release()

            # Wait for the background analysis to complete.
            time.sleep(1.5)
            self.assertEqual(alert.analysis_status, "completed")
        finally:
            engine.shutdown()


class TestAlertSerialization(unittest.TestCase):
    """Verify that analysis fields appear in Alert.to_dict()."""

    def test_to_dict_includes_analysis_fields(self):
        """Alert.to_dict() should include analysis and analysis_status."""
        alert = make_alert()
        d = alert.to_dict()
        self.assertIn("analysis", d)
        self.assertIn("analysis_status", d)
        self.assertEqual(d["analysis_status"], "pending")
        self.assertIsNone(d["analysis"])

    def test_to_dict_after_completion(self):
        """After analysis is set, to_dict() should reflect the values."""
        alert = make_alert()
        alert.analysis = "Root cause: network partition"
        alert.analysis_status = "completed"

        d = alert.to_dict()
        self.assertEqual(d["analysis"], "Root cause: network partition")
        self.assertEqual(d["analysis_status"], "completed")

    def test_to_dict_skipped(self):
        """Skipped analysis should show status='skipped' and analysis=None."""
        alert = make_alert()
        alert.analysis_status = "skipped"

        d = alert.to_dict()
        self.assertEqual(d["analysis_status"], "skipped")
        self.assertIsNone(d["analysis"])


if __name__ == "__main__":
    unittest.main()
