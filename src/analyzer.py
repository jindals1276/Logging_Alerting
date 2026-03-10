"""
analyzer.py -- LLM-based alert analysis using the Claude API.

When an alert fires, the AlertAnalyzer generates a human-readable summary
and root cause suggestions by calling the Claude API. The analysis runs
in a background daemon thread so it never blocks the engine or holds
the engine lock.

Architecture:
  - The engine creates an alert (under lock) and returns it.
  - The caller (engine.process_batch or _slider_loop) calls
    analyzer.enrich(alert) OUTSIDE the lock.
  - enrich() spawns a short-lived daemon thread that calls the Claude API,
    then writes the result back onto the Alert object.
  - If no API key is configured, enrich() sets analysis_status = "skipped"
    synchronously and returns immediately (no thread spawned).

The Claude API is called via urllib (no SDK dependency) to keep the
service lightweight.
"""

import json
import logging
import os
import threading
import urllib.request
import urllib.error
from typing import Optional

from src.models import Alert

logger = logging.getLogger(__name__)

# Claude API endpoint and model configuration.
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_API_VERSION = "2023-06-01"
MAX_TOKENS = 500


class AlertAnalyzer:
    """Enriches alerts with LLM-generated analysis.

    If an API key is provided (via constructor or ANTHROPIC_API_KEY env var),
    each call to enrich() spawns a background thread that calls the Claude
    API and writes the result onto the Alert. If no key is available, all
    calls are no-ops that set analysis_status = "skipped".
    """

    def __init__(self, api_key: Optional[str] = None):
        """Initialize the analyzer.

        Args:
            api_key: Anthropic API key. If None, falls back to the
                     ANTHROPIC_API_KEY environment variable. If still
                     None, LLM analysis is disabled.
        """
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._enabled = self._api_key is not None

        if self._enabled:
            logger.info("AlertAnalyzer enabled (API key configured)")
        else:
            logger.info("AlertAnalyzer disabled (no API key)")

    @property
    def enabled(self) -> bool:
        """Whether LLM analysis is active."""
        return self._enabled

    def enrich(self, alert: Alert) -> None:
        """Enrich an alert with LLM analysis.

        If enabled, spawns a daemon thread to call the Claude API and
        write the result onto alert.analysis / alert.analysis_status.
        The method returns immediately -- it never blocks.

        If disabled (no API key), sets analysis_status = "skipped"
        synchronously and returns.
        """
        if not self._enabled:
            alert.analysis_status = "skipped"
            return

        # Spawn a daemon thread for the API call. Daemon threads are
        # automatically killed on shutdown, so an in-flight analysis
        # is simply lost (the alert itself is already stored).
        thread = threading.Thread(
            target=self._call_llm,
            args=(alert,),
            name=f"llm-analysis-{alert.alert_id[:8]}",
            daemon=True,
        )
        thread.start()

    def _call_llm(self, alert: Alert) -> None:
        """Background thread: call Claude API and update the alert.

        Builds a prompt from the alert breakdown, sends it to Claude,
        and writes the response back onto the alert. On failure, sets
        analysis_status = "failed" and logs the error.

        Thread safety: writes alert.analysis first, then analysis_status
        last. Readers who see status="completed" are guaranteed to also
        see the analysis text.
        """
        try:
            prompt = self._build_prompt(alert)
            response = self._send_request(prompt)

            # Write analysis first, then status (ordering matters for readers).
            alert.analysis = response
            alert.analysis_status = "completed"
            logger.info("LLM analysis completed for alert %s", alert.alert_id)

        except Exception as e:
            alert.analysis_status = "failed"
            logger.error("LLM analysis failed for alert %s: %s",
                         alert.alert_id, e)

    def _build_prompt(self, alert: Alert) -> str:
        """Construct the user prompt from alert metadata.

        Includes the window range, total count vs threshold, and the
        full breakdown table. Asks Claude for a concise summary and
        root cause suggestions.
        """
        # Format the breakdown as a readable table.
        breakdown_lines = []
        for item in alert.breakdown:
            breakdown_lines.append(
                f"  {item['machine_name']:<20} {item['error_code']:<20} "
                f"{item['count']:>6}"
            )
        breakdown_table = "\n".join(breakdown_lines) if breakdown_lines else "  (empty)"

        return f"""An alert has been triggered in our log monitoring system. Analyze the following alert data and provide:

1. A 2-3 sentence human-readable summary of what happened.
2. Likely root cause suggestions based on the error patterns.
3. Recommended actions for the operations team.

Alert details:
- Window: {alert.window_start.strftime('%Y-%m-%dT%H:%M:%SZ')} to {alert.window_end.strftime('%Y-%m-%dT%H:%M:%SZ')}
- Total error count: {alert.total_count} (threshold: {alert.threshold})
- Breakdown by machine and error code:
  {"Machine":<20} {"Error Code":<20} {"Count":>6}
  {"-"*20} {"-"*20} {"-"*6}
{breakdown_table}

Be concise and actionable. Focus on patterns in the data — which machines are most affected, which error types dominate, and what that implies about the root cause."""

    def _send_request(self, prompt: str) -> str:
        """Send a request to the Claude API and return the response text.

        Uses urllib to avoid requiring the anthropic SDK as a dependency.
        Raises on any HTTP or parsing error.
        """
        request_body = json.dumps({
            "model": CLAUDE_MODEL,
            "max_tokens": MAX_TOKENS,
            "messages": [
                {"role": "user", "content": prompt}
            ],
        }).encode("utf-8")

        req = urllib.request.Request(
            CLAUDE_API_URL,
            data=request_body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self._api_key,
                "anthropic-version": CLAUDE_API_VERSION,
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())

        # Extract the text from Claude's response.
        # Response format: {"content": [{"type": "text", "text": "..."}]}
        content_blocks = data.get("content", [])
        texts = [block["text"] for block in content_blocks if block.get("type") == "text"]
        return "\n".join(texts)
