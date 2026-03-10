"""
benchmark.py -- Measure throughput of the Log Alert Service.

Runs two benchmarks:

  1. Raw engine throughput:
     Calls process_batch() directly, bypassing HTTP. Measures the pure
     in-memory processing speed (filter pipeline + bucketing + threshold
     check). This is the theoretical ceiling.

  2. End-to-end HTTP throughput:
     Sends batches via POST /api/logs to a running server. Measures
     the real-world throughput including JSON serialization, network
     round-trip, HTTP parsing, and response handling.

Usage:
  # Raw engine benchmark only (no server needed)
  python -m src.benchmark

  # Both benchmarks (server must be running on default port)
  python -m src.benchmark --http

  # Customize parameters
  python -m src.benchmark --http --url http://localhost:8080 \
      --batch-sizes 100,500,1000,5000 --batches 50
"""

import argparse
import json
import random
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta

from src.engine import AggregationEngine
from src.models import Config, LogEntry


# --- Helpers ---

def generate_entries(count, machines, spread_seconds=30):
    """Generate a list of LogEntry objects for engine benchmarking.

    Spreads timestamps over spread_seconds so entries land in multiple
    buckets (realistic workload).
    """
    now = datetime.utcnow()
    error_codes = ["ERR_CONN", "ERR_TIMEOUT", "ERR_OOM", "ERR_DISK"]
    entries = []
    for _ in range(count):
        offset = timedelta(seconds=random.uniform(-spread_seconds, 0))
        entries.append(LogEntry(
            timestamp=now + offset,
            machine_name=random.choice(machines),
            error_code=random.choice(error_codes),
            log_level=random.choice(["Error", "Fatal"]),
            message="benchmark",
        ))
    return entries


def generate_dicts(count, machines, spread_seconds=30):
    """Generate a list of log entry dicts for HTTP benchmarking.

    Same as generate_entries but returns JSON-serializable dicts
    (avoids including LogEntry parsing in the HTTP benchmark).
    """
    now = datetime.utcnow()
    error_codes = ["ERR_CONN", "ERR_TIMEOUT", "ERR_OOM", "ERR_DISK"]
    entries = []
    for _ in range(count):
        offset = timedelta(seconds=random.uniform(-spread_seconds, 0))
        ts = now + offset
        entries.append({
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "machine_name": random.choice(machines),
            "error_code": random.choice(error_codes),
            "log_level": random.choice(["Error", "Fatal"]),
            "message": "benchmark",
        })
    return entries


def post_batch(url, dicts):
    """POST a batch of log dicts and return the elapsed time in seconds."""
    body = json.dumps(dicts).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.perf_counter()
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()
    return time.perf_counter() - start


# --- Benchmarks ---

def benchmark_engine(batch_sizes, num_batches, machines):
    """Benchmark raw engine throughput (no HTTP).

    For each batch size, creates a fresh engine, sends num_batches
    batches, and measures total time. Reports messages/second.
    """
    print("=" * 65)
    print("  RAW ENGINE BENCHMARK (no HTTP overhead)")
    print("=" * 65)
    print(f"  Batches per size: {num_batches}")
    print(f"  Machines: {len(machines)}")
    print(f"  {'Batch Size':>12}  {'Total Msgs':>12}  {'Time (s)':>10}"
          f"  {'msgs/sec':>12}")
    print(f"  {'-'*12}  {'-'*12}  {'-'*10}  {'-'*12}")

    for batch_size in batch_sizes:
        # Use a very high threshold so alerts don't fire and reset state.
        config = Config(alert_threshold=999_999_999)
        engine = AggregationEngine(config)

        # Pre-generate all batches to exclude generation time.
        batches = [generate_entries(batch_size, machines)
                   for _ in range(num_batches)]

        total_msgs = batch_size * num_batches

        # Time only the processing.
        start = time.perf_counter()
        for batch in batches:
            engine.process_batch(batch)
        elapsed = time.perf_counter() - start

        engine.shutdown()

        rate = total_msgs / elapsed if elapsed > 0 else float("inf")
        print(f"  {batch_size:>12,}  {total_msgs:>12,}  {elapsed:>10.3f}"
              f"  {rate:>12,.0f}")

    print()


def benchmark_http(base_url, batch_sizes, num_batches, machines):
    """Benchmark end-to-end HTTP throughput.

    For each batch size, sends num_batches batches via POST /api/logs
    and measures total time including network round-trip.
    """
    print("=" * 65)
    print("  END-TO-END HTTP BENCHMARK")
    print("=" * 65)
    print(f"  Target: {base_url}/api/logs")
    print(f"  Batches per size: {num_batches}")
    print(f"  {'Batch Size':>12}  {'Total Msgs':>12}  {'Time (s)':>10}"
          f"  {'msgs/sec':>12}")
    print(f"  {'-'*12}  {'-'*12}  {'-'*10}  {'-'*12}")

    url = f"{base_url}/api/logs"

    for batch_size in batch_sizes:
        # Pre-generate all batches as dicts.
        batches = [generate_dicts(batch_size, machines)
                   for _ in range(num_batches)]

        total_msgs = batch_size * num_batches

        # Time only the HTTP calls.
        start = time.perf_counter()
        for batch in batches:
            try:
                post_batch(url, batch)
            except Exception as e:
                print(f"  [ERROR] {e}")
                print("  Is the server running? Use: python -m src.server")
                return
        elapsed = time.perf_counter() - start

        rate = total_msgs / elapsed if elapsed > 0 else float("inf")
        print(f"  {batch_size:>12,}  {total_msgs:>12,}  {elapsed:>10.3f}"
              f"  {rate:>12,.0f}")

    print()


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark the Log Alert Service throughput",
    )
    parser.add_argument(
        "--http", action="store_true",
        help="Also run the HTTP benchmark (server must be running)",
    )
    parser.add_argument(
        "--url", type=str, default="http://localhost:8080",
        help="Base URL for HTTP benchmark (default: http://localhost:8080)",
    )
    parser.add_argument(
        "--batch-sizes", type=str, default="100,500,1000,5000",
        help="Comma-separated batch sizes to test (default: 100,500,1000,5000)",
    )
    parser.add_argument(
        "--batches", type=int, default=50,
        help="Number of batches per size (default: 50)",
    )
    parser.add_argument(
        "--machines", type=int, default=10,
        help="Number of simulated machines (default: 10)",
    )
    args = parser.parse_args()

    batch_sizes = [int(s.strip()) for s in args.batch_sizes.split(",")]
    machines = [f"web-{i:02d}" for i in range(1, args.machines + 1)]

    print()
    benchmark_engine(batch_sizes, args.batches, machines)

    if args.http:
        benchmark_http(args.url, batch_sizes, args.batches, machines)
    else:
        print("  Tip: run with --http to also benchmark end-to-end throughput")
        print("  (requires the server to be running: python -m src.server)")
        print()


if __name__ == "__main__":
    main()
