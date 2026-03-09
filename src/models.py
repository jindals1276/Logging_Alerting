"""
models.py — Data models for the Log Alert Service.

Defines three core dataclasses:
  - LogEntry:  A single log event received from a source machine.
  - Alert:     A triggered alert when error count exceeds the threshold.
  - Config:    Service configuration (thresholds, window size, port, etc.).
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Supported ISO 8601 timestamp formats, tried in order during parsing.
# Covers variations with/without microseconds and trailing 'Z'.
TIMESTAMP_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%fZ",   # 2026-03-06T10:30:00.123456Z
    "%Y-%m-%dT%H:%M:%SZ",       # 2026-03-06T10:30:00Z
    "%Y-%m-%dT%H:%M:%S.%f",     # 2026-03-06T10:30:00.123456
    "%Y-%m-%dT%H:%M:%S",        # 2026-03-06T10:30:00
)


@dataclass
class LogEntry:
    """Represents a single log event from a source machine.

    Fields:
        timestamp:    When the event occurred on the source machine.
        machine_name: Identifier of the machine that produced the log.
        error_code:   Application-specific error code (e.g. "ERR_CONN").
        log_level:    Severity level (e.g. "Error", "Fatal", "Warning").
        message:      Human-readable description of the event.
    """
    timestamp: datetime
    machine_name: str
    error_code: str
    log_level: str
    message: str

    @staticmethod
    def from_dict(d: dict) -> Optional["LogEntry"]:
        """Parse a JSON dict into a LogEntry.

        Tries each format in TIMESTAMP_FORMATS until one succeeds.
        Returns None if the timestamp cannot be parsed (entry is skipped).
        Missing fields default to empty strings.
        """
        ts_str = d.get("timestamp", "")
        for fmt in TIMESTAMP_FORMATS:
            try:
                ts = datetime.strptime(ts_str, fmt)
                entry = LogEntry(
                    timestamp=ts,
                    machine_name=d.get("machine_name", ""),
                    error_code=d.get("error_code", ""),
                    log_level=d.get("log_level", ""),
                    message=d.get("message", ""),
                )
                logger.debug("Parsed LogEntry: machine=%s level=%s ts=%s",
                             entry.machine_name, entry.log_level, ts_str)
                return entry
            except ValueError:
                continue
        logger.warning("Failed to parse timestamp '%s' from log entry", ts_str)
        return None


@dataclass
class Alert:
    """Represents a triggered alert when the error threshold is breached.

    An alert captures the state of the aggregation window at the moment
    the threshold was exceeded, including a per-machine, per-error breakdown.

    Fields:
        alert_id:     Unique identifier (UUID4), auto-generated on creation.
        triggered_at: Server timestamp when the alert fired.
        window_start: Start of the aggregation window that triggered this alert.
        window_end:   End of the aggregation window (time of the breach).
        total_count:  Number of qualifying logs in the window at trigger time.
        threshold:    The configured threshold that was exceeded.
        breakdown:    List of dicts with per-(machine, error_code) counts.
    """
    alert_id: str
    triggered_at: datetime
    window_start: datetime
    window_end: datetime
    total_count: int
    threshold: int
    breakdown: list = field(default_factory=list)

    @staticmethod
    def create(window_start: datetime, window_end: datetime,
               total_count: int, threshold: int, breakdown: list) -> "Alert":
        """Factory method — creates an Alert with auto-generated ID and timestamp."""
        alert = Alert(
            alert_id=str(uuid.uuid4()),
            triggered_at=datetime.utcnow(),
            window_start=window_start,
            window_end=window_end,
            total_count=total_count,
            threshold=threshold,
            breakdown=breakdown,
        )
        logger.info("Alert created: id=%s count=%d threshold=%d window=[%s, %s]",
                     alert.alert_id, total_count, threshold,
                     window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     window_end.strftime("%Y-%m-%dT%H:%M:%SZ"))
        return alert

    def to_dict(self) -> dict:
        """Serialize the alert to a JSON-compatible dict for API responses."""
        fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
        return {
            "alert_id": self.alert_id,
            "triggered_at": self.triggered_at.strftime(fmt),
            "window_start": self.window_start.strftime(fmt),
            "window_end": self.window_end.strftime(fmt),
            "total_count": self.total_count,
            "threshold": self.threshold,
            "breakdown": self.breakdown,
        }


@dataclass
class Config:
    """Service configuration with sensible defaults.

    Fields:
        alert_threshold:            Number of qualifying logs to trigger an alert.
        window_duration_seconds:    Sliding window size (default 2 hours).
        slide_interval_seconds:     How far the window slides on each tick (default 1s).
        qualifying_log_levels:      Only these log levels count toward the threshold.
        late_arrival_grace_seconds: Logs older than this (vs server time) are discarded.
        port:                       HTTP server listen port.
        log_level:                  Python logging level for the service.
    """
    alert_threshold: int = 1000
    window_duration_seconds: int = 7200
    slide_interval_seconds: int = 1
    qualifying_log_levels: list = field(default_factory=lambda: ["Error", "Fatal"])
    late_arrival_grace_seconds: int = 60
    port: int = 8080
    log_level: str = "INFO"

    @staticmethod
    def from_file(path: str) -> "Config":
        """Load configuration from a JSON file.

        Any keys missing from the file fall back to default values.
        """
        logger.info("Loading config from %s", path)
        with open(path, "r") as f:
            d = json.load(f)
        config = Config(
            alert_threshold=d.get("alert_threshold", 1000),
            window_duration_seconds=d.get("window_duration_seconds", 7200),
            slide_interval_seconds=d.get("slide_interval_seconds", 1),
            qualifying_log_levels=d.get("qualifying_log_levels", ["Error", "Fatal"]),
            late_arrival_grace_seconds=d.get("late_arrival_grace_seconds", 60),
            port=d.get("port", 8080),
            log_level=d.get("log_level", "INFO"),
        )
        logger.info("Config loaded: threshold=%d window=%ds levels=%s grace=%ds",
                     config.alert_threshold, config.window_duration_seconds,
                     config.qualifying_log_levels, config.late_arrival_grace_seconds)
        return config

    def is_qualifying(self, level: str) -> bool:
        """Check if a log level counts toward the alert threshold (case-insensitive)."""
        return level.lower() in [l.lower() for l in self.qualifying_log_levels]
