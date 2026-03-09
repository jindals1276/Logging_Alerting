# Log Alert Service — Low-Level Design

## 1. `models.py` — Data Models

### LogEntry

- **Fields:** `timestamp: datetime`, `machine_name: str`, `error_code: str`, `log_level: str`, `message: str`
- `from_dict(d: dict) -> Optional[LogEntry]` — Parses a JSON dict, tries multiple ISO 8601 formats for timestamp. Returns `None` if unparseable.

### Alert

- **Fields:** `alert_id: str (uuid4)`, `triggered_at: datetime`, `window_start: datetime`, `window_end: datetime`, `total_count: int`, `threshold: int`, `breakdown: list[dict]`
- `create(window_start, window_end, total_count, threshold, breakdown) -> Alert` — Factory method, auto-generates ID and `triggered_at`.
- `to_dict() -> dict` — Serializes for JSON API response.

### Config

- **Fields:** `alert_threshold`, `window_duration_seconds`, `slide_interval_seconds`, `qualifying_log_levels`, `late_arrival_grace_seconds`, `port`
- `from_file(path: str) -> Config` — Loads from JSON file.
- `is_qualifying(level: str) -> bool` — Case-insensitive level check.

---

## 2. `engine.py` — AggregationEngine

### Memory Model — Time-Bucketed Aggregates

Instead of storing every individual log entry (O(n) memory), logs are aggregated
into fixed-size time buckets. Each bucket covers one `slide_interval` (default 1s)
and stores only a count plus a per-(machine, error_code) breakdown for that slot.

Memory is bounded at `window_duration / slide_interval` buckets (e.g. 7200 for a
2-hour window with 1-second intervals), regardless of whether 10 or 10 million
logs arrive per second.

### `_TimeBucket` (internal helper)

```
count:     int                               # total qualifying logs in this time slot
breakdown: dict[(machine, error_code), int]  # per-pair counts within this slot only
```

- `add(machine_name, error_code)` — Increments count and breakdown for one log.

### State

```
_lock:          threading.Lock
_buckets:       OrderedDict[datetime, _TimeBucket]  # keyed by truncated timestamp, oldest first
_count:         int                                  # global running total across all buckets
_breakdown:     dict[(machine, error_code), int]     # global incremental counts across all buckets
_window_start:  Optional[datetime]                   # None until first log arrives
_alerts:        list[Alert]                          # historical alerts
_slider_thread: threading.Thread                     # background daemon
_running:       bool                                 # controls slider lifecycle
```

### Methods

#### `__init__(config: Config)`

Initialize all state, start the background slider thread as a daemon.

#### `process_batch(entries: list[LogEntry]) -> Optional[Alert]`

```
Acquire lock.
For each entry:
    1. if not config.is_qualifying(entry.log_level) → skip
    2. if (now - entry.timestamp) > grace_period → skip (late arrival)
    3. if (entry.timestamp - now) > grace_period → skip (far future)
    4. if NOT first_window AND entry.timestamp < _window_start → skip (pre-window straggler)
       Note: skipped during first window init — no stragglers to filter,
       and the late arrival check already guards against very old entries.
    5. If _window_start is None → set _window_start = now (first log initializes window)
    6. Clamp timestamp: clamped_ts = min(entry.timestamp, now)
       — future-dated logs (clock skew) are bucketed at server time to
       prevent them from landing beyond the window's end boundary.
    7. Compute bucket_key = _truncate_to_bucket(clamped_ts)
    8. Get or create _buckets[bucket_key], call bucket.add(machine, error_code)
    8. Increment global _count
    9. Increment global _breakdown[(machine, error_code)]
After loop: call _check_threshold()
Release lock.
Return alert if generated, else None.
```

#### `_check_threshold() -> Optional[Alert]`

Called under lock.

```
if _count >= config.alert_threshold:
    Build breakdown list from _breakdown map, sorted by count descending.
    Create Alert with window_start, window_end=now, total_count, breakdown.
    Print alert to console via _print_alert().
    Append to _alerts.
    Call _reset_window().
    Return alert.
Return None.
```

#### `_truncate_to_bucket(ts: datetime) -> datetime`

Called under lock. Truncates a timestamp to its bucket boundary.

```
For 1s interval:  2026-03-06T10:30:00.456789 → 2026-03-06T10:30:00
For 5s interval:  2026-03-06T10:30:07        → 2026-03-06T10:30:05

total_seconds = ts.hour * 3600 + ts.minute * 60 + ts.second
truncated = (total_seconds // interval) * interval
Return ts with hour/minute/second replaced and microsecond=0
```

#### `_reset_window()`

Called under lock.

```
_buckets.clear()
_count = 0
_breakdown.clear()
_window_start = now
```

#### `_slide_window()`

Called under lock. Evicts entire buckets rather than individual log entries.

```
new_start = _window_start + timedelta(seconds=slide_interval)
_window_start = new_start
While _buckets is not empty:
    oldest_key = first key in _buckets (OrderedDict preserves insertion order)
    if oldest_key >= new_start → break (still in window)
    bucket = _buckets.pop(oldest_key)
    _count -= bucket.count
    for (machine, error_code), cnt in bucket.breakdown:
        _breakdown[(machine, error_code)] -= cnt
        if _breakdown[(machine, error_code)] <= 0:
            del _breakdown[(machine, error_code)]
```

#### `_slider_loop()`

Background thread — runs while `_running` is True.

```
While _running:
    sleep(slide_interval seconds)
    Acquire lock:
        if _window_start is None → continue (no data yet)
        if now >= _window_start + window_duration:
            call _slide_window()
            call _check_threshold()  # re-evaluate after eviction
    Release lock.
```

#### `_print_alert(alert: Alert)`

Print formatted alert to console:

```
Header with alert ID
Window range
Total count vs threshold
Table: machine | error_code | count
```

#### `get_alerts() -> list[dict]`

Under lock. Return `[a.to_dict() for a in _alerts]`.

#### `get_alert(alert_id: str) -> Optional[dict]`

Under lock. Find and return matching alert, or None.

#### `get_status() -> dict`

Under lock. Return current `window_start`, `window_end`, `current_count`, `threshold`, `progress_pct`, `total_alerts`.

#### `shutdown()`

Set `_running = False`, join slider thread.

---

## 3. `server.py` — HTTP Server

### Classes

#### `ThreadedHTTPServer(ThreadingMixIn, HTTPServer)`

```
daemon_threads = True
```

#### `RequestHandler(BaseHTTPRequestHandler)`

##### `do_POST()`

```
/api/logs →
    Read body, parse JSON (array or single object).
    Convert to LogEntry list via from_dict (skip entries that return None).
    Call engine.process_batch().
    Respond with {accepted, parse_errors, alert?}.
```

##### `do_GET()`

```
/api/alerts      → engine.get_alerts() → JSON response
/api/alerts/{id} → engine.get_alert(id) → JSON or 404
/api/status      → engine.get_status() → JSON
/api/config      → return current config values as JSON
else             → 404
```

##### `_send_json(status_code, data)`

Serialize data to JSON, set `Content-Type: application/json`, write response.

##### `log_message()`

Override to suppress success logs, only log 4xx/5xx.

### `main()`

```
Parse --config CLI arg.
Load Config from file (fall back to defaults if not found).
Create AggregationEngine(config).
Print startup banner with config summary and endpoints.
Start ThreadedHTTPServer on configured port.
On KeyboardInterrupt: engine.shutdown(), server.shutdown().
```

---

## 4. `log_generator.py` — Test Client

### `main()`

```
CLI args: --url, --machines, --rate, --interval, --error-ratio, --burst

Burst mode:
    Generate N logs, POST to /api/logs, print result and status.

Continuous mode:
    Loop: generate batch, POST, print stats.
    Every 10 batches: GET /api/status and print progress.
    Sleep(interval) between batches.
```

### `generate_log_entry(machines, error_ratio) -> dict`

```
Random machine from pool, random error code, level weighted by error_ratio.
Timestamp = now + small jitter (-2s to 0s).
Return as dict matching LogEntry JSON schema.
```

---

## 5. Thread Interaction

```
Main Thread                  Request Threads (N)         Slider Thread
    │                              │                          │
    │  start server                │                          │
    │  start slider ───────────────┼──────────────────────>   │
    │                              │                          │
    │                     POST /api/logs                      │
    │                        │                                │
    │                   acquire _lock                         │
    │                   filter + bucket                       │
    │                   _check_threshold()                    │
    │                   release _lock                         │
    │                        │                          sleep(1s)
    │                        │                          acquire _lock
    │                        │                          _slide_window()
    │                        │                          (evict old buckets)
    │                        │                          _check_threshold()
    │                        │                          release _lock
    │                        │                                │
    │  KeyboardInterrupt     │                                │
    │  engine.shutdown() ────┼───────────────────────────>  join
    │  server.shutdown()     │                                │
```

---

## 6. Edge Cases

| Scenario | Behavior |
|----------|----------|
| No logs arrive for 2+ hours | Slider thread keeps sliding, buckets eventually all evicted, count = 0 |
| Burst of logs triggers threshold mid-batch | Alert fires immediately after the batch insert; all logs in the batch are bucketed before threshold check |
| Log arrives with timestamp before `window_start` (post-reset straggler) | Dropped in filter step 4 (only after an established window; skipped during first init) |
| Log arrives with timestamp slightly in the future (clock skew) | Accepted if within grace period; bucket key clamped to server time |
| Log arrives with timestamp far in the future (beyond grace) | Dropped in filter step 3 |
| Machine name or error code contains special characters | No delimiter parsing needed — breakdown map keys are tuples of raw strings (both in buckets and globally) |
| Multiple alerts in quick succession | Each alert resets the window; next threshold must be reached fresh |
| Server restarts | All state is lost (in-memory only) — acceptable for local service |
| Concurrent POST requests | Thread lock serializes access to shared state; each request processed atomically |
| Empty batch submitted | No-op, returns `{accepted: 0, parse_errors: 0}` |
| Invalid JSON body | Returns 400 with error message |
