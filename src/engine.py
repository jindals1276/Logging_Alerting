"""
engine.py — Aggregation Engine for the Log Alert Service.

This is the core component of the service. It maintains a sliding time window
of qualifying log entries (Error/Fatal) and triggers an alert when the count
exceeds a configured threshold.

Memory model — Time-Bucketed Aggregates:

  Instead of storing every individual log entry (which could consume unbounded
  memory under high throughput), logs are aggregated into fixed-size time
  buckets (1 bucket per slide_interval, typically 1 second).

  Each bucket holds:
    - A total count of qualifying logs that fell into that time slot.
    - A breakdown map {(machine_name, error_code): count} for that slot.

  This means memory is bounded: at most (window_duration / slide_interval)
  buckets exist at any time. For a 2-hour window with 1-second buckets,
  that's at most 7200 buckets — regardless of whether 10 or 10 million
  logs arrive per second.

  On window slide, entire buckets are evicted in O(1) per bucket, and their
  counts are subtracted from the global running totals.

Other design choices:
  - A global running count avoids summing all buckets on every threshold check.
  - A global breakdown map is maintained incrementally (insert +1, evict -1),
    so alert breakdowns are available instantly.
  - A single threading lock protects all shared state. Contention is low
    because operations under the lock are pure in-memory (no I/O).
  - A background daemon thread slides the window forward even when no
    logs arrive, ensuring the 2-hour window doesn't stall during quiet periods.

Lifecycle:
  1. Create engine:   engine = AggregationEngine(config)
  2. Feed log batches: engine.process_batch(entries)
  3. Query state:      engine.get_status(), engine.get_alerts()
  4. Shut down:        engine.shutdown()
"""

import collections
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.models import Alert, Config, LogEntry

logger = logging.getLogger(__name__)


class _TimeBucket:
    """Aggregated counts for a single time slot.

    Each bucket covers a time range of [bucket_start, bucket_start + interval).
    Instead of storing individual log entries, we only track:
      - count:     total number of qualifying logs in this slot.
      - breakdown: {(machine_name, error_code): count} for this slot.

    This makes each bucket O(unique_machine_error_pairs) in memory,
    which is far smaller than O(num_logs) when throughput is high.
    """
    __slots__ = ("count", "breakdown")

    def __init__(self):
        self.count: int = 0
        # Per-(machine, error_code) counts within this bucket only.
        self.breakdown: dict[tuple[str, str], int] = {}

    def add(self, machine_name: str, error_code: str):
        """Record one qualifying log entry in this bucket."""
        self.count += 1
        key = (machine_name, error_code)
        self.breakdown[key] = self.breakdown.get(key, 0) + 1


class AggregationEngine:
    """Sliding-window aggregation engine with time-bucketed storage.

    Thread-safe: all public methods acquire the internal lock before
    reading or mutating shared state.
    """

    def __init__(self, config: Config, analyzer=None):
        self._config = config

        # Optional LLM analyzer for enriching alerts with human-readable
        # summaries. Called outside the lock after alert creation.
        self._analyzer = analyzer

        # --- Shared state (all access must hold self._lock) ---

        # OrderedDict of time buckets, keyed by bucket start time (datetime
        # truncated to the nearest slide_interval). Ordered by insertion
        # time so the oldest bucket is always first — enabling O(1) eviction.
        #
        # Example with 1-second buckets:
        #   {
        #     datetime(2026,3,6,10,0,0): _TimeBucket(count=15, breakdown={...}),
        #     datetime(2026,3,6,10,0,1): _TimeBucket(count=23, breakdown={...}),
        #     ...
        #   }
        #
        # Max size: window_duration / slide_interval (e.g. 7200 for 2h/1s).
        self._buckets: collections.OrderedDict[datetime, _TimeBucket] = (
            collections.OrderedDict()
        )

        # Global running count across all buckets. Always equals the sum of
        # bucket.count for all buckets, but maintained separately so threshold
        # checks are O(1).
        self._count: int = 0

        # Global breakdown map {(machine_name, error_code): count} across all
        # buckets. Incremented on insert, decremented on bucket eviction.
        # Keys with count 0 are deleted to keep the map clean.
        self._breakdown: dict[tuple[str, str], int] = {}

        # Start of the current aggregation window. None until the first
        # qualifying log arrives, at which point it's set to server time.
        self._window_start: Optional[datetime] = None

        # Historical list of all alerts triggered during this engine's lifetime.
        self._alerts: list[Alert] = []

        # Lock protecting all shared state above.
        self._lock = threading.Lock()

        # --- Background slider thread ---

        # Controls the slider loop. Set to False to stop the thread.
        self._running = True

        # Daemon thread that periodically checks whether the window has
        # expired (window_start + window_duration <= now). If so, it slides
        # the window forward and evicts stale buckets.
        self._slider_thread = threading.Thread(
            target=self._slider_loop,
            name="window-slider",
            daemon=True,  # Dies automatically when the main process exits
        )
        self._slider_thread.start()
        logger.info("AggregationEngine started (threshold=%d, window=%ds, slide=%ds)",
                     config.alert_threshold, config.window_duration_seconds,
                     config.slide_interval_seconds)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_batch(self, entries: list[LogEntry]) -> Optional[Alert]:
        """Filter and ingest a batch of log entries into time buckets.

        Performance note: each call acquires the lock, processes all entries,
        and runs _check_threshold(). Single-message calls (batch_size=1) are
        roughly 4x slower than large batches due to per-call lock and
        threshold-check overhead. If this becomes a bottleneck, consider
        adding a micro-batching layer in the HTTP handler that buffers
        entries for 10-50ms before flushing them here as a single batch.
        Run `python -m src.benchmark` to measure on your hardware.

        For each entry the filter pipeline applies four checks in order:
          1. Level check   — only qualifying levels (e.g. Error, Fatal) pass.
          2. Late arrival  — drop if the log is older than the grace period.
          3. Future guard  — drop if the log is too far in the future
                             (beyond the grace period). Logs slightly in the
                             future (due to clock skew) are accepted but
                             clamped to server time for bucketing.
          4. Window bound  — drop if the log falls before window_start
                             (e.g. a straggler arriving after a window reset).

        Qualifying entries are placed into the appropriate time bucket.
        The bucket key is clamped to min(entry.timestamp, now) and then
        truncated to the nearest slide_interval. This clamping ensures
        future-dated logs (from clock-skewed machines) don't end up in
        buckets beyond the window's end boundary.

        After inserting all qualifying entries, checks whether the threshold
        has been breached. Returns the Alert if one was triggered, else None.
        """
        now = datetime.now(timezone.utc)
        grace = timedelta(seconds=self._config.late_arrival_grace_seconds)

        with self._lock:
            # Track whether this batch is the very first to initialize the
            # window. During first initialization, we skip the window bound
            # check because there's no "previous window" to straggle from.
            # Without this, entries slightly before 'now' (due to timing)
            # would be incorrectly dropped after the first entry sets
            # window_start = now.
            is_first_window = self._window_start is None

            for entry in entries:
                # --- Filter pipeline ---

                # 1. Level check: skip logs that aren't Error/Fatal (or
                #    whatever levels are configured as qualifying).
                if not self._config.is_qualifying(entry.log_level):
                    continue

                # 2. Late arrival check: if the log's timestamp is too far
                #    in the past relative to server time, discard it. This
                #    prevents old replayed logs from inflating the count.
                if (now - entry.timestamp) > grace:
                    logger.debug("Dropped late arrival: ts=%s age=%s",
                                 entry.timestamp, now - entry.timestamp)
                    continue

                # 3. Future timestamp guard: reject logs whose timestamp is
                #    too far in the future (beyond the grace period). This
                #    catches wildly wrong clocks or fabricated timestamps.
                #    Logs slightly in the future (within grace) are accepted
                #    — their bucket key will be clamped to 'now' below.
                if (entry.timestamp - now) > grace:
                    logger.debug("Dropped far-future log: ts=%s ahead=%s",
                                 entry.timestamp, entry.timestamp - now)
                    continue

                # 4. Window bound check: if we have an established window
                #    (from a previous batch or a reset), drop logs whose
                #    timestamp falls before window_start. This handles
                #    stragglers that arrive after a window reset.
                #    Skip this check during first window initialization —
                #    there are no stragglers to filter, and the late arrival
                #    check (step 2) already guards against very old entries.
                if not is_first_window and entry.timestamp < self._window_start:
                    logger.debug("Dropped pre-window log: ts=%s window_start=%s",
                                 entry.timestamp, self._window_start)
                    continue

                # --- First log initializes the window ---
                # The window doesn't start until we actually receive data,
                # so the background slider won't run on an empty window.
                if self._window_start is None:
                    self._window_start = now
                    logger.info("Window initialized at %s", self._window_start)

                # --- Insert into the appropriate time bucket ---
                # Clamp to min(timestamp, now) so that future-dated logs
                # (from clock-skewed source machines) are bucketed at server
                # time. This prevents them from landing in buckets beyond
                # the window's end boundary, which would cause incorrect
                # eviction timing.
                clamped_ts = min(entry.timestamp, now)
                bucket_key = self._truncate_to_bucket(clamped_ts)

                # Get or create the bucket for this time slot.
                if bucket_key not in self._buckets:
                    self._buckets[bucket_key] = _TimeBucket()
                self._buckets[bucket_key].add(entry.machine_name, entry.error_code)

                # Update global running totals.
                self._count += 1
                key = (entry.machine_name, entry.error_code)
                self._breakdown[key] = self._breakdown.get(key, 0) + 1

            # After processing the full batch, check if we've hit the threshold.
            alert = self._check_threshold()

        # LLM enrichment runs OUTSIDE the lock. The enrich() method spawns
        # a background thread and returns immediately, so this adds near-zero
        # latency to process_batch(). The analysis is written back onto the
        # alert object asynchronously (1-2 seconds later).
        if alert is not None and self._analyzer is not None:
            self._analyzer.enrich(alert)

        return alert

    def get_alerts(self) -> list[dict]:
        """Return all historical alerts as a list of JSON-serializable dicts."""
        with self._lock:
            return [a.to_dict() for a in self._alerts]

    def get_alert(self, alert_id: str) -> Optional[dict]:
        """Return a single alert by ID, or None if not found."""
        with self._lock:
            for a in self._alerts:
                if a.alert_id == alert_id:
                    return a.to_dict()
        return None

    def get_status(self) -> dict:
        """Return the current window state for the /api/status endpoint.

        Includes window boundaries, current count, threshold, progress
        percentage, and total number of alerts triggered so far.
        """
        with self._lock:
            if self._window_start is not None:
                window_end = self._window_start + timedelta(
                    seconds=self._config.window_duration_seconds)
                progress = (self._count / self._config.alert_threshold * 100
                            if self._config.alert_threshold > 0 else 0)
            else:
                window_end = None
                progress = 0.0

            return {
                "window_start": (self._window_start.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                                 if self._window_start else None),
                "window_end": (window_end.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                               if window_end else None),
                "current_count": self._count,
                "threshold": self._config.alert_threshold,
                "progress_pct": round(progress, 2),
                "total_alerts": len(self._alerts),
            }

    def shutdown(self):
        """Stop the background slider thread and wait for it to finish."""
        logger.info("Shutting down AggregationEngine...")
        self._running = False
        self._slider_thread.join(timeout=5)
        logger.info("AggregationEngine shut down.")

    # ------------------------------------------------------------------
    # Internal methods (called under self._lock)
    # ------------------------------------------------------------------

    def _truncate_to_bucket(self, ts: datetime) -> datetime:
        """Truncate a timestamp to its bucket boundary.

        For a 1-second slide interval, this strips microseconds:
          2026-03-06T10:30:00.456789 → 2026-03-06T10:30:00

        For a 5-second interval, it rounds down to the nearest 5s:
          2026-03-06T10:30:07 → 2026-03-06T10:30:05

        The bucket key is the start of the time slot that contains ts.
        """
        interval = self._config.slide_interval_seconds
        # Total seconds since midnight, truncated to the interval boundary.
        total_seconds = ts.hour * 3600 + ts.minute * 60 + ts.second
        truncated_seconds = (total_seconds // interval) * interval
        return ts.replace(
            hour=truncated_seconds // 3600,
            minute=(truncated_seconds % 3600) // 60,
            second=truncated_seconds % 60,
            microsecond=0,
        )

    def _check_threshold(self) -> Optional[Alert]:
        """Check if the current count meets or exceeds the threshold.

        If breached, builds the alert breakdown from the global breakdown
        map, prints it to the console, stores it, resets the window, and
        returns the Alert.

        Must be called while holding self._lock.
        """
        if self._count < self._config.alert_threshold:
            return None

        # Build the per-(machine, error_code) breakdown, sorted by count
        # descending so the biggest contributors appear first.
        breakdown = [
            {"machine_name": machine, "error_code": error, "count": count}
            for (machine, error), count in sorted(
                self._breakdown.items(), key=lambda item: item[1], reverse=True
            )
        ]

        now = datetime.now(timezone.utc)
        alert = Alert.create(
            window_start=self._window_start,
            window_end=now,
            total_count=self._count,
            threshold=self._config.alert_threshold,
            breakdown=breakdown,
        )

        self._print_alert(alert)
        self._alerts.append(alert)

        # Reset the window so the next threshold must be reached fresh.
        self._reset_window()

        return alert

    def _reset_window(self):
        """Clear all aggregation state and start a new window at current time.

        Called after an alert fires. Clears all buckets, resets global count
        and breakdown map, and sets window_start to now. Any in-flight logs
        with older timestamps will be filtered out by the window bound check
        in process_batch().

        Must be called while holding self._lock.
        """
        self._buckets.clear()
        self._count = 0
        self._breakdown.clear()
        self._window_start = datetime.now(timezone.utc)
        logger.info("Window reset. New window_start=%s", self._window_start)

    def _slide_window(self):
        """Slide the window forward by one slide interval and evict stale buckets.

        Moves window_start forward by slide_interval_seconds, then removes
        any buckets whose key (start time) falls before the new window_start.

        For each evicted bucket, the bucket's counts are subtracted from the
        global running count and breakdown map. This is O(1) per evicted
        bucket (plus the number of unique machine/error pairs in that bucket).

        Must be called while holding self._lock.
        """
        new_start = self._window_start + timedelta(
            seconds=self._config.slide_interval_seconds)
        self._window_start = new_start

        # Evict buckets whose time slot is entirely before the new window_start.
        # Since _buckets is an OrderedDict in insertion order (oldest first),
        # we pop from the front until we find a bucket that's still in-window.
        evicted_count = 0
        while self._buckets:
            # Peek at the oldest bucket key without removing it yet.
            oldest_key = next(iter(self._buckets))
            if oldest_key >= new_start:
                break  # This bucket and all after it are still in the window.

            # Remove the bucket and subtract its counts from global totals.
            bucket = self._buckets.pop(oldest_key)
            self._count -= bucket.count

            # Subtract each (machine, error_code) count from the global
            # breakdown map. Remove entries that drop to zero.
            for key, cnt in bucket.breakdown.items():
                self._breakdown[key] -= cnt
                if self._breakdown[key] <= 0:
                    del self._breakdown[key]

            evicted_count += 1

        if evicted_count:
            logger.debug("Window slid to %s — evicted %d bucket(s), count now %d",
                         new_start, evicted_count, self._count)

    def _slider_loop(self):
        """Background thread loop that slides the window when it expires.

        Runs every slide_interval_seconds. On each tick:
          1. If no window exists yet (no data received), do nothing.
          2. If the window has expired (now >= window_start + duration),
             slide it forward and re-check the threshold. The re-check
             handles the edge case where eviction drops the count and we
             want to verify the remaining logs still exceed the threshold.
          3. Otherwise, sleep and check again.

        Exits when self._running is set to False by shutdown().
        """
        interval = self._config.slide_interval_seconds
        duration = timedelta(seconds=self._config.window_duration_seconds)

        logger.info("Slider thread started (interval=%ds, window=%ds)",
                     interval, self._config.window_duration_seconds)

        while self._running:
            # Sleep first, then check. This avoids an immediate slide
            # on startup before any data has arrived.
            # Use a short sleep loop so we can respond to shutdown quickly.
            for _ in range(interval * 10):
                if not self._running:
                    return
                threading.Event().wait(0.1)

            alert = None
            with self._lock:
                # No data yet — nothing to slide.
                if self._window_start is None:
                    continue

                # Check if the full window duration has elapsed.
                now = datetime.now(timezone.utc)
                if now >= self._window_start + duration:
                    self._slide_window()
                    # Re-check threshold after eviction. Sliding may have
                    # changed the count, and we want to detect if the
                    # remaining logs still exceed the threshold.
                    alert = self._check_threshold()

            # LLM enrichment outside the lock (same pattern as process_batch).
            if alert is not None and self._analyzer is not None:
                self._analyzer.enrich(alert)

        logger.info("Slider thread exiting.")

    def _print_alert(self, alert: Alert):
        """Print a formatted alert to the console for operator visibility.

        Output includes the alert ID, window range, total count vs threshold,
        and a table of the top contributors by (machine, error_code, count).
        """
        sep = "=" * 60
        print(f"\n{sep}")
        print(f"  ALERT TRIGGERED: {alert.alert_id}")
        print(f"{sep}")
        print(f"  Window:    {alert.window_start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
              f" -> {alert.window_end.strftime('%Y-%m-%dT%H:%M:%SZ')}")
        print(f"  Count:     {alert.total_count} (threshold: {alert.threshold})")
        print(f"  Breakdown:")
        print(f"    {'Machine':<20} {'Error Code':<20} {'Count':>8}")
        print(f"    {'-'*20} {'-'*20} {'-'*8}")
        for item in alert.breakdown:
            print(f"    {item['machine_name']:<20} {item['error_code']:<20} "
                  f"{item['count']:>8}")
        print(f"{sep}\n")
