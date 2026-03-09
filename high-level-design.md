# Log Alert Service — High-Level Design

## Architecture Overview

```
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│  Machine A   │   │  Machine B   │   │  Machine C   │
│ (Log Source) │   │ (Log Source) │   │ (Log Source) │
└──────┬───────┘   └──────┬───────┘   └──────┬───────┘
       │                  │                  │
       │   POST /api/logs │                  │
       └──────────────────┼──────────────────┘
                          ▼
              ┌───────────────────────┐
              │      HTTP Server      │
              │  (Threaded, :8080)    │
              │                       │
              │  POST /api/logs       │  ← Ingest log batches
              │  GET  /api/alerts     │  ← Query past alerts
              │  GET  /api/status     │  ← Current window state
              └───────────┬───────────┘
                          │
                          ▼
              ┌───────────────────────┐
              │    Filter Pipeline    │
              │                       │
              │ 1. Level check        │  ← Only Error/Fatal
              │ 2. Late arrival check │  ← Drop if > 60s old
              │ 3. Window bound check │  ← Drop if < window_start
              └───────────┬───────────┘
                          │ qualifying logs
                          ▼
              ┌───────────────────────┐
              │  Aggregation Engine   │
              │                       │
              │ - Time buckets        │  ← OrderedDict of per-second aggregates
              │ - Running count       │  ← O(1) threshold check
              │ - Breakdown map       │  ← {(machine, error): count}
              │ - Thread lock         │  ← Concurrent safety
              └───────────┬───────────┘
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
    ┌──────────────────┐   ┌──────────────────┐
    │ Threshold Check  │   │  Window Slider   │
    │ (on each batch)  │   │ (background      │
    │                  │   │  thread, runs     │
    │ count >= T?      │   │  every 1 second)  │
    │   YES → Alert    │   │                   │
    │   NO  → continue │   │ window expired?   │
    └────────┬─────────┘   │   YES → slide +1s │
             │             │   NO  → sleep      │
             ▼             └────────────────────┘
    ┌──────────────────┐
    │  Alert Handler   │
    │                  │
    │ - Print to       │
    │   console        │
    │ - Store in       │
    │   alert list     │
    │ - Reset window   │
    └──────────────────┘
```

## Components

### 1. HTTP Server

Threaded HTTP server using Python stdlib. Accepts log batches via POST, exposes alerts and status via GET. Each request is handled in its own thread.

### 2. Filter Pipeline

Applied to every incoming log entry in order:

1. **Level check** — Only Error and Fatal pass through
2. **Late arrival check** — Drop if timestamp is older than the configurable grace period (default 60s)
3. **Future timestamp guard** — Drop if timestamp is too far in the future (beyond grace period). Logs slightly in the future (clock skew) are accepted but clamped to server time for bucketing, preventing them from landing in buckets beyond the window's end boundary.
4. **Window bound check** — Drop if timestamp falls before the current `window_start` (handles post-reset stragglers). Skipped during first window initialization since there are no stragglers to filter.

Rejects early and cheap.

### 3. Aggregation Engine

The core component. Uses **time-bucketed aggregates** instead of storing individual log entries, bounding memory usage regardless of log throughput. Holds:

- An **OrderedDict of time buckets** — each bucket covers one slide interval (default 1 second) and stores a count plus a per-(machine, error_code) breakdown for that slot. Max buckets = `window_duration / slide_interval` (e.g. 7200 for a 2-hour window with 1s buckets).
- A **global running count** of qualifying logs across all buckets (avoids summing buckets on every threshold check)
- A **global breakdown map** `{(machine_name, error_code): count}` updated incrementally on insert (+1) and bucket eviction (-1)
- A **threading lock** protecting all shared state

### 4. Threshold Check

Runs synchronously after every batch insert. If `running_count >= threshold`, fires an alert and resets the window.

### 5. Window Slider

A background daemon thread. Every second it checks: has the 2-hour window elapsed without an alert? If yes, it:

1. Slides `window_start` forward by 1 second
2. Evicts entire time buckets whose key (start time) falls before the new `window_start`
3. Subtracts each evicted bucket's counts from the global running count and breakdown map
4. Re-checks the threshold after the slide

### 6. Alert Handler

On threshold breach:

1. Builds the per-machine, per-error breakdown from the breakdown map
2. Prints alert details to the console
3. Stores the alert in an in-memory list (queryable via API)
4. Resets the window — clears deque, count, breakdown map, sets `window_start = now`

## Data Flow

```
Incoming log → Filter → Place in time bucket → Increment global count & breakdown
                                              → Check threshold
                                                  → Alert? → Print + Reset
                                                  → No?    → Wait for more

Background (every 1s):
  Window expired? → Slide +1s → Evict oldest bucket(s) → Decrement global count
                 → Re-check threshold after slide
```

## Time Model: Hybrid (System Time + Event Time)

The service uses a **hybrid time model** — window boundaries are anchored to system time, while log placement within the window uses the log's event timestamp.

| Concern | Time Source | Rationale |
|---------|------------|-----------|
| Window start/end boundaries | System time | Window must slide even if no logs arrive |
| Log placement within window | Event time (log timestamp) | Accurately reflects when the event occurred on the source machine |
| Late arrival check | System time vs event time | `server_now - log_timestamp > grace_period` → discard |
| Window expiry (background slider) | System time | `server_now > window_start + 2h` → slide forward |
| Window reset after alert | System time | `window_start = server_now` |

### Why not pure event-time?

A pure event-time approach (window boundaries derived from the latest log timestamp seen) was considered. Comparison:

| | Hybrid | Pure Event-Time |
|--|--------|-----------------|
| Window slides during quiet periods | Yes — background thread uses system clock | No — window freezes if no logs arrive, defeating the slide requirement |
| Server clock dependency | Yes — if server clock drifts, window boundaries shift | No — fully deterministic from log data |
| Replay/testing determinism | Non-deterministic (depends on wall clock) | Deterministic — same logs always produce same results |
| Implementation complexity | Simple | Higher — needs watermark tracking; a single machine with a far-future timestamp can jump the watermark and evict valid logs from others |
| Suited for | Live real-time processing | Batch processing or stream replay (Kafka Streams, Flink) |

**Decision:** Hybrid is the right fit because this is a local real-time service processing live logs. The core requirement — "slide the window if threshold not met for 2 hours" — inherently needs a real-time clock to detect that 2 hours have passed, even during periods of no log traffic.

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Time-bucketed aggregates** | Memory bounded at O(window/interval) buckets regardless of log throughput, vs O(n) for storing individual entries |
| **OrderedDict for buckets** | Insertion-ordered; oldest bucket is always first, enabling O(1) eviction from the front |
| **Global running count + breakdown map** | Avoids summing all buckets on every threshold check; maintained incrementally |
| **Bucket eviction on slide** | Entire bucket removed in O(1); its counts subtracted from global totals |
| **Lock granularity** | Single lock on the engine — simple, and contention is low since operations are fast (no I/O under lock) |
| **Background thread for sliding** | Decouples window management from log ingestion; window slides even if no logs arrive |

## Configuration (`config.json`)

```json
{
  "alert_threshold": 1000,
  "window_duration_seconds": 7200,
  "slide_interval_seconds": 1,
  "qualifying_log_levels": ["Error", "Fatal"],
  "late_arrival_grace_seconds": 60,
  "port": 8080
}
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `alert_threshold` | 1000 | Total qualifying log count to trigger an alert |
| `window_duration_seconds` | 7200 (2 hours) | Sliding window size |
| `slide_interval_seconds` | 1 | Window slide step when threshold is not met |
| `qualifying_log_levels` | ["Error", "Fatal"] | Log levels that count toward the threshold |
| `late_arrival_grace_seconds` | 60 | Logs older than this (relative to server time) are discarded |
| `port` | 8080 | HTTP server port |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/logs` | Ingest a batch of log entries (JSON array) |
| GET | `/api/alerts` | List all generated alerts |
| GET | `/api/alerts/{id}` | Get a specific alert with breakdown |
| GET | `/api/status` | Current window state and progress |
