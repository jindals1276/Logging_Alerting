import json
import os
import tempfile
import unittest
from datetime import datetime

from src.models import LogEntry, Alert, Config


class TestLogEntry(unittest.TestCase):

    def test_parse_valid_entry(self):
        d = {
            "timestamp": "2026-03-06T10:30:00.123456Z",
            "machine_name": "web-01",
            "error_code": "ERR_CONN",
            "log_level": "Error",
            "message": "Connection refused",
        }
        entry = LogEntry.from_dict(d)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.machine_name, "web-01")
        self.assertEqual(entry.error_code, "ERR_CONN")
        self.assertEqual(entry.log_level, "Error")
        self.assertEqual(entry.timestamp, datetime(2026, 3, 6, 10, 30, 0, 123456))

    def test_parse_without_microseconds(self):
        d = {"timestamp": "2026-03-06T10:30:00Z", "machine_name": "m1",
             "error_code": "E1", "log_level": "Fatal", "message": ""}
        entry = LogEntry.from_dict(d)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.timestamp, datetime(2026, 3, 6, 10, 30, 0))

    def test_parse_without_z_suffix(self):
        d = {"timestamp": "2026-03-06T10:30:00.123456", "machine_name": "m1",
             "error_code": "E1", "log_level": "Error", "message": ""}
        entry = LogEntry.from_dict(d)
        self.assertIsNotNone(entry)

    def test_parse_bare_timestamp(self):
        d = {"timestamp": "2026-03-06T10:30:00", "machine_name": "m1",
             "error_code": "E1", "log_level": "Error", "message": ""}
        entry = LogEntry.from_dict(d)
        self.assertIsNotNone(entry)

    def test_parse_invalid_timestamp_returns_none(self):
        d = {"timestamp": "not-a-date", "machine_name": "m1",
             "error_code": "E1", "log_level": "Error", "message": ""}
        entry = LogEntry.from_dict(d)
        self.assertIsNone(entry)

    def test_parse_empty_timestamp_returns_none(self):
        d = {"timestamp": "", "machine_name": "m1",
             "error_code": "E1", "log_level": "Error", "message": ""}
        entry = LogEntry.from_dict(d)
        self.assertIsNone(entry)

    def test_parse_missing_fields_default_to_empty(self):
        d = {"timestamp": "2026-03-06T10:30:00Z"}
        entry = LogEntry.from_dict(d)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.machine_name, "")
        self.assertEqual(entry.error_code, "")
        self.assertEqual(entry.log_level, "")
        self.assertEqual(entry.message, "")


class TestAlert(unittest.TestCase):

    def test_create_generates_id_and_triggered_at(self):
        start = datetime(2026, 3, 6, 8, 0, 0)
        end = datetime(2026, 3, 6, 10, 0, 0)
        breakdown = [{"machine_name": "web-01", "error_code": "ERR_CONN", "count": 50}]

        alert = Alert.create(start, end, 50, 40, breakdown)
        self.assertIsNotNone(alert.alert_id)
        self.assertEqual(len(alert.alert_id), 36)  # uuid4 format
        self.assertIsNotNone(alert.triggered_at)
        self.assertEqual(alert.total_count, 50)
        self.assertEqual(alert.threshold, 40)
        self.assertEqual(alert.breakdown, breakdown)

    def test_to_dict_serialization(self):
        start = datetime(2026, 3, 6, 8, 0, 0)
        end = datetime(2026, 3, 6, 10, 0, 0)
        alert = Alert.create(start, end, 100, 80, [])

        d = alert.to_dict()
        self.assertEqual(d["alert_id"], alert.alert_id)
        self.assertEqual(d["total_count"], 100)
        self.assertEqual(d["threshold"], 80)
        self.assertIn("window_start", d)
        self.assertIn("window_end", d)
        self.assertIn("triggered_at", d)
        self.assertIsInstance(d["breakdown"], list)

    def test_two_alerts_have_different_ids(self):
        start = datetime(2026, 3, 6, 8, 0, 0)
        end = datetime(2026, 3, 6, 10, 0, 0)
        a1 = Alert.create(start, end, 10, 10, [])
        a2 = Alert.create(start, end, 10, 10, [])
        self.assertNotEqual(a1.alert_id, a2.alert_id)


class TestConfig(unittest.TestCase):

    def test_defaults(self):
        config = Config()
        self.assertEqual(config.alert_threshold, 1000)
        self.assertEqual(config.window_duration_seconds, 7200)
        self.assertEqual(config.slide_interval_seconds, 1)
        self.assertEqual(config.qualifying_log_levels, ["Error", "Fatal"])
        self.assertEqual(config.late_arrival_grace_seconds, 60)
        self.assertEqual(config.port, 8080)
        self.assertEqual(config.log_level, "INFO")

    def test_from_file(self):
        data = {
            "alert_threshold": 500,
            "window_duration_seconds": 3600,
            "slide_interval_seconds": 2,
            "qualifying_log_levels": ["Fatal"],
            "late_arrival_grace_seconds": 30,
            "port": 9090,
            "log_level": "DEBUG",
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                         delete=False) as f:
            json.dump(data, f)
            path = f.name
        try:
            config = Config.from_file(path)
            self.assertEqual(config.alert_threshold, 500)
            self.assertEqual(config.window_duration_seconds, 3600)
            self.assertEqual(config.slide_interval_seconds, 2)
            self.assertEqual(config.qualifying_log_levels, ["Fatal"])
            self.assertEqual(config.late_arrival_grace_seconds, 30)
            self.assertEqual(config.port, 9090)
            self.assertEqual(config.log_level, "DEBUG")
        finally:
            os.unlink(path)

    def test_from_file_with_partial_config_uses_defaults(self):
        data = {"alert_threshold": 200}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                         delete=False) as f:
            json.dump(data, f)
            path = f.name
        try:
            config = Config.from_file(path)
            self.assertEqual(config.alert_threshold, 200)
            self.assertEqual(config.window_duration_seconds, 7200)  # default
            self.assertEqual(config.qualifying_log_levels, ["Error", "Fatal"])  # default
        finally:
            os.unlink(path)

    def test_is_qualifying_case_insensitive(self):
        config = Config(qualifying_log_levels=["Error", "Fatal"])
        self.assertTrue(config.is_qualifying("Error"))
        self.assertTrue(config.is_qualifying("error"))
        self.assertTrue(config.is_qualifying("ERROR"))
        self.assertTrue(config.is_qualifying("Fatal"))
        self.assertTrue(config.is_qualifying("fatal"))
        self.assertFalse(config.is_qualifying("Warning"))
        self.assertFalse(config.is_qualifying("Info"))

    def test_is_qualifying_custom_levels(self):
        config = Config(qualifying_log_levels=["Warning"])
        self.assertTrue(config.is_qualifying("Warning"))
        self.assertFalse(config.is_qualifying("Error"))
        self.assertFalse(config.is_qualifying("Fatal"))


if __name__ == "__main__":
    unittest.main()
