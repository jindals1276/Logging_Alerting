"""
server.py -- Threaded HTTP server for the Log Alert Service.

Exposes a REST API for:
  - Ingesting log batches (POST /api/logs)
  - Querying triggered alerts (GET /api/alerts, GET /api/alerts/{id})
  - Checking current window state (GET /api/status)
  - Viewing active configuration (GET /api/config)

Uses Python's stdlib HTTPServer with ThreadingMixIn so each request
is handled in its own thread. The server itself is stateless -- all
state lives in the AggregationEngine, which is thread-safe.

Usage:
  python -m src.server                    # defaults
  python -m src.server --config config.json
"""

import argparse
import json
import logging
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from src.engine import AggregationEngine
from src.models import Config, LogEntry

logger = logging.getLogger(__name__)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTP server that handles each request in a separate thread.

    daemon_threads = True ensures request threads die when the main
    thread exits, preventing the server from hanging on shutdown.
    """
    daemon_threads = True


class RequestHandler(BaseHTTPRequestHandler):
    """Handles HTTP requests and delegates to the AggregationEngine.

    The engine and config are attached to the server instance and
    accessed via self.server.engine / self.server.config.
    """

    def do_POST(self):
        """Handle POST requests.

        POST /api/logs -- Ingest a batch of log entries.
          Body: JSON array of log objects, or a single log object.
          Response: {accepted, parse_errors, alert?}
        """
        if self.path == "/api/logs":
            self._handle_post_logs()
        else:
            self._send_json(404, {"error": "Not found"})

    def do_GET(self):
        """Handle GET requests.

        GET /api/alerts       -- List all triggered alerts.
        GET /api/alerts/{id}  -- Get a specific alert by ID.
        GET /api/status       -- Current window state and progress.
        GET /api/config       -- Active configuration values.
        """
        if self.path == "/api/alerts":
            alerts = self.server.engine.get_alerts()
            self._send_json(200, alerts)

        elif self.path.startswith("/api/alerts/"):
            # Extract alert ID from the URL path.
            alert_id = self.path[len("/api/alerts/"):]
            alert = self.server.engine.get_alert(alert_id)
            if alert:
                self._send_json(200, alert)
            else:
                self._send_json(404, {"error": f"Alert '{alert_id}' not found"})

        elif self.path == "/api/status":
            status = self.server.engine.get_status()
            self._send_json(200, status)

        elif self.path == "/api/config":
            # Expose current config as JSON for debugging/monitoring.
            config = self.server.config
            self._send_json(200, {
                "alert_threshold": config.alert_threshold,
                "window_duration_seconds": config.window_duration_seconds,
                "slide_interval_seconds": config.slide_interval_seconds,
                "qualifying_log_levels": config.qualifying_log_levels,
                "late_arrival_grace_seconds": config.late_arrival_grace_seconds,
                "port": config.port,
            })

        else:
            self._send_json(404, {"error": "Not found"})

    def _handle_post_logs(self):
        """Parse and ingest a batch of log entries.

        Accepts either a JSON array of log objects or a single log object.
        For each object, attempts to parse it into a LogEntry via from_dict.
        Entries that fail parsing are counted as parse_errors but don't
        reject the whole batch -- partial success is allowed.

        Future improvement: under very high load, this method could append
        entries to a thread-safe buffer instead of calling process_batch()
        immediately. A background flush thread would drain the buffer every
        10-50ms and call process_batch() once, amortizing lock and threshold
        check overhead (~4x improvement). See src/benchmark.py for measuring
        throughput on your hardware.
        """
        try:
            # Read the request body.
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            self._send_json(400, {"error": f"Invalid JSON: {e}"})
            return

        # Normalize: wrap a single object in a list for uniform handling.
        if isinstance(data, dict):
            data = [data]
        elif not isinstance(data, list):
            self._send_json(400, {"error": "Body must be a JSON array or object"})
            return

        # Parse each dict into a LogEntry. Track how many failed.
        entries = []
        parse_errors = 0
        for item in data:
            entry = LogEntry.from_dict(item)
            if entry:
                entries.append(entry)
            else:
                parse_errors += 1

        # Feed qualifying entries to the engine.
        alert = self.server.engine.process_batch(entries)

        # Build response.
        response = {
            "accepted": len(entries),
            "parse_errors": parse_errors,
        }
        if alert:
            response["alert"] = alert.to_dict()

        self._send_json(200, response)

    def _send_json(self, status_code, data):
        """Send a JSON response with the given status code.

        Sets Content-Type to application/json and encodes the data
        as UTF-8.
        """
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Override default request logging.

        Suppress noisy success logs (2xx/3xx). Only log client errors
        (4xx) and server errors (5xx) to keep the console focused on
        alert output.
        """
        # args[1] is the status code string (e.g. "200", "404").
        if len(args) >= 2:
            try:
                code = int(args[1])
                if code < 400:
                    return  # Suppress success logs.
            except (ValueError, IndexError):
                pass
        logger.warning(format, *args)


def main():
    """Entry point for the Log Alert Service.

    Parses CLI args, loads config, creates the engine and server,
    and runs until interrupted with Ctrl+C.
    """
    # Parse command-line arguments.
    parser = argparse.ArgumentParser(description="Log Alert Service")
    parser.add_argument("--config", type=str, default=None,
                        help="Path to config JSON file (uses defaults if not provided)")
    args = parser.parse_args()

    # Load configuration.
    if args.config:
        config = Config.from_file(args.config)
    else:
        config = Config()

    # Set up logging based on config.
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Create the aggregation engine.
    engine = AggregationEngine(config)

    # Create and configure the HTTP server.
    server = ThreadedHTTPServer(("", config.port), RequestHandler)
    # Attach engine and config to the server so handlers can access them.
    server.engine = engine
    server.config = config

    # Print startup banner.
    print("=" * 60)
    print("  Log Alert Service")
    print("=" * 60)
    print(f"  Port:       {config.port}")
    print(f"  Threshold:  {config.alert_threshold} qualifying logs")
    print(f"  Window:     {config.window_duration_seconds}s"
          f" ({config.window_duration_seconds // 3600}h"
          f" {(config.window_duration_seconds % 3600) // 60}m)")
    print(f"  Levels:     {', '.join(config.qualifying_log_levels)}")
    print(f"  Grace:      {config.late_arrival_grace_seconds}s")
    print(f"  Log level:  {config.log_level}")
    print(f"")
    print(f"  Endpoints:")
    print(f"    POST /api/logs          Ingest log batches")
    print(f"    GET  /api/alerts        List all alerts")
    print(f"    GET  /api/alerts/{{id}}   Get alert by ID")
    print(f"    GET  /api/status        Current window state")
    print(f"    GET  /api/config        Active configuration")
    print("=" * 60)
    print(f"  Listening on http://localhost:{config.port}")
    print("=" * 60)

    # Run the server until Ctrl+C.
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        engine.shutdown()
        server.shutdown()
        print("Server stopped.")


if __name__ == "__main__":
    main()
