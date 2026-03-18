# Log Alert Service — Requirements

## What is this?

A real-time monitoring service that watches log streams from multiple machines and raises an alert when too many errors happen within a time window.

Think of it as a smoke detector for your server fleet — it watches for a surge of errors and immediately tells you which machines and error types are responsible.

## The Problem

You have a fleet of machines (web servers, API servers, databases, etc.) that produce logs continuously. Most logs are informational, but some indicate errors — connection failures, timeouts, out-of-memory crashes, authentication failures, etc.

You need to know when something is going wrong **before** it becomes a full outage. Specifically, you want to be alerted when the number of error logs crosses a threshold within a rolling time window.

## Core Requirements

### 1. Log Ingestion

- The service accepts log entries from multiple source machines via an HTTP API.
- Each log entry contains:
  - **Timestamp** — when the event happened on the source machine
  - **Machine name** — which machine produced the log (e.g. "web-01")
  - **Error code** — what type of error occurred (e.g. "ERR_TIMEOUT")
  - **Log level** — severity: Info, Warning, Error, Fatal, Debug
  - **Message** — human-readable description of the event
- Logs can arrive individually or in batches.
- Logs may arrive slightly out of order or with small clock differences between machines.

### 2. Filtering

- Only **Error** and **Fatal** level logs count toward the alert threshold. Info, Warning, and Debug logs are ignored.
- Logs that are too old (more than 60 seconds behind server time) are discarded — they are considered stale and should not affect current alerting.
- Logs with timestamps too far in the future (beyond the grace period) are discarded — they indicate a misconfigured clock.
- Logs slightly in the future (minor clock skew) are accepted but treated as if they arrived "now".

### 3. Sliding Time Window

- The service monitors errors within a **rolling 2-hour window**.
- The window slides forward over time. If the error threshold is not breached within 2 hours, the oldest errors age out and the window moves forward.
- The window advances even during quiet periods when no logs arrive.

### 4. Alert Threshold

- When the number of qualifying error logs within the current window reaches or exceeds the configured threshold (default: 1000), an **alert is triggered**.
- The alert includes:
  - When the window started and ended
  - Total error count
  - A **breakdown** showing which machines and error codes contributed the most errors, sorted by count
- After an alert fires, the window resets and counting starts fresh.

### 5. Alert History

- All triggered alerts are stored and can be queried later via the API.
- Each alert has a unique ID for retrieval.

### 6. AI-Powered Alert Analysis (Optional)

- When an alert fires, the service can optionally call an AI model (Claude) to generate:
  - A human-readable **summary** of what happened
  - Likely **root cause suggestions** based on the error patterns
  - **Recommended actions** for the operations team
- This feature is optional — the service works fully without it. It activates only when an API key is configured.
- The AI analysis runs in the background and does not delay the alert itself.

## Non-Functional Requirements

### Performance

- The service must handle high log throughput without falling behind.
- Memory usage must be bounded — it should not grow proportionally with the number of logs received. Only aggregated counts are stored, not individual log entries.

### Reliability

- The service must handle concurrent requests from multiple machines safely.
- Malformed or unparseable log entries should be skipped without crashing the service or rejecting the entire batch.
- If the AI analysis feature fails (API error, timeout), it should not affect the core alerting functionality.

### Timezone Handling

- All timestamps are normalized to UTC internally.
- Clients can send timestamps in any timezone — the service converts them to UTC at the point of ingestion.
- Bare timestamps (with no timezone indicator) are assumed to be UTC.

### Configuration

All parameters are configurable:

| Parameter | Default | Description |
|-----------|---------|-------------|
| Alert threshold | 1000 | Number of errors to trigger an alert |
| Window duration | 2 hours | Size of the rolling time window |
| Qualifying log levels | Error, Fatal | Which log levels count |
| Late arrival grace period | 60 seconds | How old a log can be before it's discarded |
| Server port | 8080 | HTTP listen port |

### API

The service exposes a simple REST API:

| What you want to do | How |
|---------------------|-----|
| Send logs | `POST /api/logs` with a JSON array of log entries |
| See all past alerts | `GET /api/alerts` |
| See a specific alert | `GET /api/alerts/{id}` |
| Check current window state | `GET /api/status` (shows count, progress toward threshold) |
| View active configuration | `GET /api/config` |

## Out of Scope

The following are explicitly **not** part of this service:

- **Log storage** — this service monitors and alerts, it does not store or search historical logs. Use a dedicated log store (Elasticsearch, Loki, etc.) for that.
- **Persistence across restarts** — all state is in-memory. If the service restarts, the window and alert history are lost. This is acceptable for a local monitoring service.
- **Authentication/authorization** — the API is open. In production, place it behind a reverse proxy or API gateway for access control.
- **Notification delivery** — the service prints alerts to the console and stores them in-memory. Integrating with Slack, PagerDuty, email, etc. is a future enhancement.
- **Distributed deployment** — the service runs as a single process on a single machine. Distributed log aggregation would require a different architecture (e.g. Kafka + Flink).
