"""
log_generator.py -- Test client for the Log Alert Service.

Generates synthetic log entries and POSTs them to the server's /api/logs
endpoint. Supports two modes:

  Burst mode (--burst N):
    Generate N log entries in a single batch, POST them, print the result
    and current server status, then exit. Useful for quickly testing
    threshold triggering.

  Continuous mode (default):
    Loop indefinitely, generating batches of --rate entries every --interval
    seconds. Every 10 batches, fetches and prints the server's /api/status
    to show window progress. Useful for simulating sustained log traffic.

Usage examples:
  # Burst: send 1000 error logs at once
  python -m src.log_generator --burst 1000

  # Continuous: 50 logs/batch, one batch per second, 40% error rate
  python -m src.log_generator --rate 50 --interval 1.0 --error-ratio 0.4

  # Target a remote server with custom machine pool
  python -m src.log_generator --url http://10.0.0.5:8080 --machines 10
"""

import argparse
import json
import random
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta

# --- Pools of realistic values for generating synthetic log entries ---

# Error codes representing common application failures.
ERROR_CODES = [
    "ERR_CONN_REFUSED",
    "ERR_TIMEOUT",
    "ERR_DISK_FULL",
    "ERR_OOM",
    "ERR_AUTH_FAILED",
    "ERR_DB_DEADLOCK",
    "ERR_RATE_LIMITED",
    "ERR_SSL_HANDSHAKE",
]

# Log levels, split into error (qualifying) and non-error (filtered out).
ERROR_LEVELS = ["Error", "Fatal"]
NON_ERROR_LEVELS = ["Info", "Warning", "Debug"]

# Message templates for added realism in generated logs.
MESSAGES = [
    "Connection refused by downstream service",
    "Request timed out after 30s",
    "Disk usage exceeded 95% threshold",
    "Out of memory: unable to allocate 256MB",
    "Authentication failed for user 'svc-account'",
    "Database deadlock detected on table 'orders'",
    "Rate limit exceeded: 429 Too Many Requests",
    "SSL handshake failed: certificate expired",
    "Health check failed on port 8443",
    "Process crashed with exit code 137 (OOM killed)",
]


def generate_log_entry(machines, error_ratio):
    """Generate a single synthetic log entry as a JSON-compatible dict.

    Args:
        machines:    List of machine name strings to pick from randomly.
        error_ratio: Float 0.0-1.0 controlling the fraction of entries
                     that have Error/Fatal level (qualifying logs) vs
                     Info/Warning/Debug (non-qualifying). For example,
                     0.3 means ~30% of logs will be errors.

    Returns:
        A dict matching the LogEntry JSON schema expected by POST /api/logs.
        Timestamp includes a small random jitter (-2s to 0s) to simulate
        realistic clock variation between source machines.
    """
    # Pick a random machine from the pool.
    machine = random.choice(machines)

    # Decide whether this log is an error or not, based on the ratio.
    if random.random() < error_ratio:
        level = random.choice(ERROR_LEVELS)
        error_code = random.choice(ERROR_CODES)
    else:
        level = random.choice(NON_ERROR_LEVELS)
        error_code = ""  # Non-error logs don't need an error code.

    message = random.choice(MESSAGES)

    # Add small jitter to timestamp to simulate clock skew between machines.
    # Range: -2s to 0s (slightly in the past, never in the future).
    jitter = timedelta(seconds=random.uniform(-2.0, 0.0))
    ts = datetime.utcnow() + jitter

    return {
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "machine_name": machine,
        "error_code": error_code,
        "log_level": level,
        "message": message,
    }


def post_logs(url, logs):
    """POST a batch of log entries to the server.

    Args:
        url:  Full URL to the /api/logs endpoint.
        logs: List of log entry dicts.

    Returns:
        Parsed JSON response dict on success, or None on failure.
    """
    body = json.dumps(logs).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        print(f"  [ERROR] Failed to POST: {e}")
        return None
    except Exception as e:
        print(f"  [ERROR] Unexpected error: {e}")
        return None


def get_status(base_url):
    """GET /api/status from the server and return parsed JSON, or None."""
    try:
        req = urllib.request.Request(f"{base_url}/api/status", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  [ERROR] Failed to GET status: {e}")
        return None


def print_status(status):
    """Print a formatted summary of the server's current window state."""
    if status is None:
        return
    print(f"  Status: count={status['current_count']}"
          f"  threshold={status['threshold']}"
          f"  progress={status['progress_pct']}%"
          f"  alerts={status['total_alerts']}"
          f"  window_start={status['window_start']}")


def run_burst(base_url, machines, count, error_ratio):
    """Burst mode: generate N logs in one batch, POST, print results.

    Sends a single large batch to quickly test threshold triggering.
    After the POST, fetches and prints the server status.
    """
    print(f"Burst mode: generating {count} logs...")
    logs = [generate_log_entry(machines, error_ratio) for _ in range(count)]

    # Count how many are qualifying (Error/Fatal) for user visibility.
    error_count = sum(1 for l in logs if l["log_level"] in ERROR_LEVELS)
    print(f"  Generated: {count} total, {error_count} errors,"
          f" {count - error_count} non-errors")

    result = post_logs(f"{base_url}/api/logs", logs)
    if result:
        print(f"  Response: accepted={result['accepted']}"
              f"  parse_errors={result['parse_errors']}")
        if "alert" in result:
            print(f"  ** ALERT triggered: {result['alert']['alert_id']}"
                  f"  count={result['alert']['total_count']}")

    # Fetch and display current server status.
    status = get_status(base_url)
    print_status(status)
    print("Done.")


def run_continuous(base_url, machines, rate, interval, error_ratio):
    """Continuous mode: send batches in a loop with periodic status checks.

    Generates 'rate' entries per batch, sleeps 'interval' seconds between
    batches. Every 10 batches, fetches /api/status and prints progress.
    Runs until interrupted with Ctrl+C.
    """
    print(f"Continuous mode: {rate} logs/batch, every {interval}s,"
          f" error_ratio={error_ratio}")
    print(f"  Target: {base_url}/api/logs")
    print(f"  Machines: {len(machines)} ({', '.join(machines[:5])}"
          f"{'...' if len(machines) > 5 else ''})")
    print(f"  Press Ctrl+C to stop.\n")

    batch_num = 0
    total_sent = 0
    total_alerts = 0

    try:
        while True:
            batch_num += 1
            logs = [generate_log_entry(machines, error_ratio) for _ in range(rate)]
            error_count = sum(1 for l in logs if l["log_level"] in ERROR_LEVELS)

            result = post_logs(f"{base_url}/api/logs", logs)
            if result:
                total_sent += result["accepted"]
                alert_msg = ""
                if "alert" in result:
                    total_alerts += 1
                    alert_msg = (f"  ** ALERT #{total_alerts}:"
                                 f" {result['alert']['alert_id'][:8]}...")
                print(f"  Batch {batch_num}: sent={rate}"
                      f" errors={error_count} accepted={result['accepted']}"
                      f" total_sent={total_sent}{alert_msg}")
            else:
                print(f"  Batch {batch_num}: FAILED (server unreachable?)")

            # Every 10 batches, fetch and display server status.
            if batch_num % 10 == 0:
                status = get_status(base_url)
                print_status(status)
                print()

            time.sleep(interval)

    except KeyboardInterrupt:
        print(f"\nStopped after {batch_num} batches, {total_sent} logs sent,"
              f" {total_alerts} alerts triggered.")


def main():
    """Entry point: parse CLI args and run in burst or continuous mode."""
    parser = argparse.ArgumentParser(
        description="Test client for the Log Alert Service",
    )
    parser.add_argument(
        "--url", type=str, default="http://localhost:8080",
        help="Base URL of the alert service (default: http://localhost:8080)",
    )
    parser.add_argument(
        "--machines", type=int, default=5,
        help="Number of simulated source machines (default: 5)",
    )
    parser.add_argument(
        "--rate", type=int, default=20,
        help="Number of log entries per batch in continuous mode (default: 20)",
    )
    parser.add_argument(
        "--interval", type=float, default=1.0,
        help="Seconds between batches in continuous mode (default: 1.0)",
    )
    parser.add_argument(
        "--error-ratio", type=float, default=0.3,
        help="Fraction of logs that are Error/Fatal, 0.0-1.0 (default: 0.3)",
    )
    parser.add_argument(
        "--burst", type=int, default=None,
        help="Burst mode: generate N logs in one batch and exit",
    )
    args = parser.parse_args()

    # Build machine name pool: web-01, web-02, ..., web-NN
    machines = [f"web-{i:02d}" for i in range(1, args.machines + 1)]

    base_url = args.url.rstrip("/")

    if args.burst:
        run_burst(base_url, machines, args.burst, args.error_ratio)
    else:
        run_continuous(base_url, machines, args.rate, args.interval, args.error_ratio)


if __name__ == "__main__":
    main()
