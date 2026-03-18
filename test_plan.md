# Black-Box Test Plan — Log Alert Service

## Overview

This document describes the black-box integration tests in `tests/test_blackbox.py`. These tests validate the service **exclusively through its HTTP API** — no internal state, private methods, or data structures are inspected. The service is treated as an opaque HTTP endpoint and verified against the requirements in `requirements.md`.

## How to Run

The tests **automatically start a server** on a random port with default configuration. No pre-running server is required.

```bash
# Run all tests (server auto-started)
python -m unittest tests.test_blackbox -v

# Run a specific test class
python -m unittest tests.test_blackbox.TestAlertTriggering -v

# Run a single test
python -m unittest tests.test_blackbox.TestFiltering.test_info_does_not_count -v
```

To test against an **external server** instead of auto-starting one, set the `SERVER_URL` environment variable:

```bash
# Linux/macOS
SERVER_URL=http://192.168.1.10:9090 python -m unittest tests.test_blackbox -v

# Windows (PowerShell)
$env:SERVER_URL = "http://192.168.1.10:9090"
python -m unittest tests.test_blackbox -v
```

No external dependencies are needed — all tests use Python stdlib (`unittest`, `urllib`, `json`, `threading`, `os`, `concurrent.futures`).

## Test Architecture

- By default, a **module-level server** is automatically started on a random port with default configuration via `setUpModule()` / `tearDownModule()`. No pre-running server is needed.
- Optionally, set the `SERVER_URL` environment variable to point at an external server instead.
- **Sections 1–11** (external-server tests) run against the module-level server using delta-based assertions (count change) to be resilient to shared state.
- **Section 12** (isolated-server tests) start their own managed servers with custom configuration (e.g., low thresholds, short windows) for scenarios requiring clean state.
- Alert-triggering tests read the server's configured threshold via `GET /api/config` and send exactly enough logs to trigger.
- All interaction is via `POST /api/logs`, `GET /api/status`, `GET /api/alerts`, `GET /api/alerts/{id}`, and `GET /api/config`.

---

## Test Matrix

### 1. Log Ingestion (`TestLogIngestion`) — 5 tests

Tests the core `POST /api/logs` endpoint for accepting log entries.

| # | Test | Requirement | What it verifies |
|---|------|-------------|------------------|
| 1 | `test_single_log_accepted` | Logs can arrive individually | A single log entry returns 200 with `accepted >= 1` |
| 2 | `test_batch_logs_accepted` | Logs can arrive in batches | A batch of 5 logs returns `accepted == 5` |
| 3 | `test_empty_batch` | Graceful handling | Empty array returns `accepted=0, parse_errors=0` |
| 4 | `test_response_contains_accepted_and_parse_errors` | API contract | Response body contains both `accepted` and `parse_errors` fields |
| 5 | `test_single_object_post` | Single object POST | `POST /api/logs` with `{...}` (not array) returns `accepted=1` |

---

### 2. Filtering (`TestFiltering`) — 10 tests

Tests that only qualifying logs (Error/Fatal) count toward the threshold, and that stale/future logs are discarded. Uses **delta-based assertions** (count before vs. after) to work correctly against a shared server with existing state.

| # | Test | Requirement | What it verifies |
|---|------|-------------|------------------|
| 1 | `test_error_level_counts` | Error logs count | Error log increments `current_count` (delta ≥ 1) |
| 2 | `test_fatal_level_counts` | Fatal logs count | Fatal log increments `current_count` (delta ≥ 1) |
| 3 | `test_info_does_not_count` | Info ignored | Info log does NOT change count (delta == 0) |
| 4 | `test_warning_does_not_count` | Warning ignored | Warning log does NOT change count (delta == 0) |
| 5 | `test_debug_does_not_count` | Debug ignored | Debug log does NOT change count (delta == 0) |
| 6 | `test_stale_logs_discarded` | Late arrival > 60s discarded | Log 120s in the past → count unchanged (delta == 0) |
| 7 | `test_future_logs_beyond_grace_discarded` | Far-future discarded | Log 300s in the future → count unchanged (delta == 0) |
| 8 | `test_mixed_batch_only_qualifying_counted` | Filtering in batch | Batch of 6 (2 Error + 1 Fatal + 3 non-qualifying) → delta == 3 |
| 9 | `test_non_qualifying_still_accepted` | Non-qualifying not rejected | Info log returns `accepted=1, parse_errors=0` |
| 10 | `test_slightly_future_accepted` | Clock skew grace | Log 5s in the future (within 60s grace) → accepted and counted |

---

### 3. Sliding Window (`TestSlidingWindow`) — 1 test

Tests that errors within the window are counted. Uses delta-based assertion against the module-level server.

| # | Test | Requirement | What it verifies |
|---|------|-------------|------------------|
| 1 | `test_errors_within_window_counted` | Errors in window counted | 5 errors → count increases by at least 5 |

---

### 4. Alert Triggering (`TestAlertTriggering`) — 8 tests

Tests the alert mechanism. Reads the server's configured threshold via `GET /api/config` and sends exactly enough errors to trigger, accounting for existing count.

| # | Test | Requirement | What it verifies |
|---|------|-------------|------------------|
| 1 | `test_alert_fires_at_threshold` | Alert at threshold | Sends enough errors to reach threshold → `alert` present in response |
| 2 | `test_no_alert_below_threshold` | No premature alert | 1 error → no `alert` in response |
| 3 | `test_alert_has_required_fields` | Alert structure | Alert contains `alert_id`, `window_start`, `window_end`, `total_count`, `breakdown`, `threshold` |
| 4 | `test_alert_breakdown_content` | Breakdown content | Breakdown entries have `machine_name`, `error_code`, `count` |
| 5 | `test_breakdown_sorted_by_count_descending` | Breakdown sorting | Breakdown entries are ordered highest count first |
| 6 | `test_window_resets_after_alert` | Reset after alert | After alert fires, `current_count` resets to 0 |
| 7 | `test_second_alert_after_reset` | Multiple alerts | Two alerts can fire sequentially after reset |
| 8 | `test_alert_has_unique_id` | Unique IDs | Two consecutive alerts have different `alert_id` values |

---

### 5. Alert History API (`TestAlertHistory`) — 5 tests

Tests the alert retrieval endpoints. Adapts to existing server state (e.g., triggers an alert if none exist).

| # | Test | Requirement | What it verifies |
|---|------|-------------|------------------|
| 1 | `test_alerts_endpoint_returns_list` | API contract | `GET /api/alerts` returns a JSON list |
| 2 | `test_alert_appears_in_history` | Alert stored | After triggering, alert count in `GET /api/alerts` increases |
| 3 | `test_get_alert_by_id` | Retrieve by ID | `GET /api/alerts/{id}` returns matching alert (200) |
| 4 | `test_get_alert_unknown_id_returns_404` | Unknown ID | `GET /api/alerts/nonexistent` returns 404 |
| 5 | `test_alert_has_analysis_fields` | Analysis fields | Alert includes `analysis` and `analysis_status` fields |

---

### 6. Status API (`TestStatusAPI`) — 4 tests

Tests the `GET /api/status` endpoint. Uses delta-based assertions for count changes.

| # | Test | Requirement | What it verifies |
|---|------|-------------|------------------|
| 1 | `test_status_returns_required_fields` | API contract | Response has `current_count`, `threshold`, `progress_pct` |
| 2 | `test_status_reflects_ingested_logs` | Count accuracy | After 3 errors, count increases by at least 3 |
| 3 | `test_status_threshold_matches_config` | Config match | Threshold in status == threshold in `GET /api/config` |
| 4 | `test_status_has_total_alerts` | Alert counter | `total_alerts` field present and is an integer |

---

### 7. Config API (`TestConfigAPI`) — 2 tests

Tests the `GET /api/config` endpoint against the running server's configuration.

| # | Test | Requirement | What it verifies |
|---|------|-------------|------------------|
| 1 | `test_config_returns_all_fields` | API contract | Response has `alert_threshold`, `window_duration_seconds`, `qualifying_log_levels`, `late_arrival_grace_seconds` |
| 2 | `test_config_qualifying_levels` | Levels listed | `qualifying_log_levels` contains "Error" and "Fatal" |

---

### 8. Timezone Handling (`TestTimezoneHandling`) — 5 tests

Tests that timestamps in various formats are accepted and normalized to UTC. Validates only the POST response (`accepted`/`parse_errors`) to avoid flaky assertions from shared server state.

| # | Test | Requirement | What it verifies |
|---|------|-------------|------------------|
| 1 | `test_z_suffix_accepted` | Z suffix | `2026-03-06T10:30:00.123456Z` → `accepted=1, parse_errors=0` |
| 2 | `test_plus_zero_offset_accepted` | +00:00 offset | `...+00:00` → `accepted=1` |
| 3 | `test_non_utc_offset_accepted_and_converted` | Non-UTC conversion | `...+05:30` → `accepted=1, parse_errors=0` |
| 4 | `test_bare_timestamp_assumed_utc` | Bare = UTC | Timestamp with no tz suffix → `accepted=1` |
| 5 | `test_negative_offset_accepted` | Negative offset | `...-05:00` → `accepted=1` |

---

### 9. Robustness / Error Handling (`TestRobustness`) — 7 tests

Tests that the service handles bad input gracefully without crashing.

| # | Test | Requirement | What it verifies |
|---|------|-------------|------------------|
| 1 | `test_malformed_json_body` | Bad JSON | Non-JSON body → HTTP 400+; service stays healthy |
| 2 | `test_invalid_timestamp_skipped` | Bad timestamp | `"not-a-date"` → `parse_errors=1, accepted=0` |
| 3 | `test_partial_batch_valid_and_invalid` | Partial success | 2 valid + 1 invalid → `accepted=2, parse_errors=1` |
| 4 | `test_missing_fields_still_parsed` | Default fields | Entry with only timestamp → accepted (fields default to empty) |
| 5 | `test_unknown_route_returns_404` | 404 handling | `GET /api/nonexistent` → 404 |
| 6 | `test_service_healthy_after_bad_requests` | Resilience | After garbage input, valid logs are still accepted |
| 7 | `test_empty_timestamp_is_parse_error` | Empty timestamp | `""` timestamp → `parse_errors=1` |

---

### 10. Concurrency (`TestConcurrency`) — 2 tests

Tests thread safety under concurrent load. Uses delta-based count assertions.

| # | Test | Requirement | What it verifies |
|---|------|-------------|------------------|
| 1 | `test_concurrent_posts_no_data_loss` | Thread-safe ingestion | 10 threads × 20 logs = 200 total; count increases by at least 200 |
| 2 | `test_concurrent_reads_and_writes` | Mixed concurrent ops | 3 POST threads + 3 GET threads running simultaneously → no crashes |

---

### 11. Memory Bounded (`TestMemoryBounded`) — 1 test

Tests that the service handles large volumes without crashing.

| # | Test | Requirement | What it verifies |
|---|------|-------------|------------------|
| 1 | `test_large_batch_does_not_crash` | Bounded memory | 500-entry batch → `accepted=500`, no errors |

---

### 12. Isolated Server Tests — 9 tests (5 classes)

These tests start their own managed server instance with custom configuration, enabling clean-state and custom-threshold tests that are not possible against a shared server.

#### 12a. Sliding Window Eviction (`TestIsolatedSlidingWindow`) — 1 test

| # | Test | Requirement | What it verifies |
|---|------|-------------|------------------|
| 1 | `test_old_errors_evicted_after_window_expires` | Window eviction | 5 errors sent, wait 7s (window=5s) → count decreases |

#### 12b. Alert Triggering (`TestIsolatedAlertTriggering`) — 3 tests

| # | Test | Requirement | What it verifies |
|---|------|-------------|------------------|
| 1 | `test_alert_fires_above_threshold` | Alert at threshold | 10 errors (threshold=5) → alert fires |
| 2 | `test_breakdown_counts_accurate` | Breakdown accuracy | 3 from web-01 + 2 from web-02 → exact counts in breakdown |
| 3 | `test_post_reset_straggler_dropped` | Post-reset straggler | Log timestamped before alert reset → not counted |

#### 12c. Alert History (`TestIsolatedAlertHistory`) — 2 tests

| # | Test | Requirement | What it verifies |
|---|------|-------------|------------------|
| 1 | `test_alerts_empty_initially` | Clean state | Fresh server → `GET /api/alerts` returns empty list |
| 2 | `test_multiple_alerts_in_history` | Multiple alerts | 3 alert cycles → history contains all 3 |

#### 12d. Status API (`TestIsolatedStatusAPI`) — 2 tests

| # | Test | Requirement | What it verifies |
|---|------|-------------|------------------|
| 1 | `test_progress_percentage` | Progress accuracy | 50/100 errors → `progress_pct ≈ 50.0` |
| 2 | `test_status_resets_after_alert` | Status reset | After alert fires → `current_count == 0` |

#### 12e. Config API (`TestIsolatedConfigAPI`) — 1 test

| # | Test | Requirement | What it verifies |
|---|------|-------------|------------------|
| 1 | `test_config_reflects_custom_values` | Custom config | Server started with threshold=42, window=600s → config endpoint matches |

---

## Summary

| Test Class | Tests | Requirement Area |
|------------|------:|------------------|
| `TestLogIngestion` | 5 | Log ingestion via HTTP API |
| `TestFiltering` | 10 | Log level filtering, stale/future discard |
| `TestSlidingWindow` | 1 | Window counting |
| `TestAlertTriggering` | 8 | Alert threshold, breakdown, reset |
| `TestAlertHistory` | 5 | Alert retrieval API |
| `TestStatusAPI` | 4 | Status endpoint accuracy |
| `TestConfigAPI` | 2 | Configuration endpoint |
| `TestTimezoneHandling` | 5 | Timezone normalization |
| `TestRobustness` | 7 | Error handling, resilience |
| `TestConcurrency` | 2 | Thread safety |
| `TestMemoryBounded` | 1 | Large batch handling |
| `TestIsolatedSlidingWindow` | 1 | Window eviction (short window) |
| `TestIsolatedAlertTriggering` | 3 | Alert with clean state (threshold, breakdown, straggler) |
| `TestIsolatedAlertHistory` | 2 | Alert history with clean state (empty, multiple) |
| `TestIsolatedStatusAPI` | 2 | Status with clean state (progress %, reset) |
| `TestIsolatedConfigAPI` | 1 | Config with custom values |
| **Total** | **59** | |
