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
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

def _parse_to_utc(ts_str: str) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp string and normalize to timezone-aware UTC.

    Handles the following formats:
      - With Z suffix:       2026-03-06T10:30:00Z         -> UTC
      - With UTC offset:     2026-03-06T10:30:00+00:00    -> UTC
      - With non-UTC offset: 2026-03-06T10:30:00+05:30    -> converted to UTC
      - Bare (no timezone):  2026-03-06T10:30:00           -> assumed UTC
      - With microseconds:   2026-03-06T10:30:00.123456Z  -> UTC

    All results are timezone-aware datetime objects in UTC. This enforces
    a single timezone throughout the service, preventing silent bugs from
    timezone mismatches between clients and server.

    Returns None if the string cannot be parsed.
    """
    if not ts_str:
        return None

    try:
        ts = datetime.fromisoformat(ts_str)
    except ValueError:
        return None

    if ts.tzinfo is None:
        # Bare timestamp with no timezone — assume UTC.
        # This is the best-effort case; the client should ideally send
        # an explicit timezone suffix. Documented as "assumed UTC".
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        # Timestamp has timezone info — convert to UTC.
        # e.g. 16:00:00+05:30 becomes 10:30:00+00:00
        ts = ts.astimezone(timezone.utc)

    return ts


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

        Uses _parse_to_utc() to parse the timestamp and normalize to
        timezone-aware UTC. Accepts Z, +00:00, non-UTC offsets (converted),
        and bare timestamps (assumed UTC). Returns None if unparseable.
        Missing fields default to empty strings.
        """
        ts_str = d.get("timestamp", "")
        ts = _parse_to_utc(ts_str)
        if ts is None:
            logger.warning("Failed to parse timestamp '%s' from log entry", ts_str)
            return None

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
    # LLM-generated analysis fields. Populated asynchronously by AlertAnalyzer
    # after the alert fires. Readers may see "pending" until the background
    # thread completes (typically 1-2 seconds).
    analysis: Optional[str] = None
    analysis_status: str = "pending"  # "pending", "completed", "skipped", "failed"

    @staticmethod
    def create(window_start: datetime, window_end: datetime,
               total_count: int, threshold: int, breakdown: list) -> "Alert":
        """Factory method — creates an Alert with auto-generated ID and timestamp."""
        alert = Alert(
            alert_id=str(uuid.uuid4()),
            triggered_at=datetime.now(timezone.utc),
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
            "analysis": self.analysis,
            "analysis_status": self.analysis_status,
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
