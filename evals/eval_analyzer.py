"""
eval_analyzer.py -- Evaluation suite for the LLM-based AlertAnalyzer.

Unlike unit tests (pass/fail on code correctness), evals measure the
QUALITY of LLM-generated analysis. They answer: "Does the analysis
correctly identify patterns, root causes, and recommend sensible actions?"

Architecture:
  - Define eval scenarios: each has a known alert pattern and a list of
    expected mentions (things a good analysis SHOULD reference).
  - Run the analyzer on each scenario (calls the real Claude API).
  - Score the output using an LLM-as-judge: a second Claude call evaluates
    whether the analysis addresses the expected criteria.
  - Report per-scenario scores and an overall score.

Usage:
  # Requires ANTHROPIC_API_KEY env var
  python -m evals.eval_analyzer

  # Verbose mode: print full analysis text for each scenario
  python -m evals.eval_analyzer --verbose

When to run:
  - After changing the prompt in analyzer.py
  - After switching Claude models or adjusting max_tokens
  - After modifying the alert breakdown format
  NOT on every build -- evals cost API calls and take ~30-60 seconds.
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

from src.analyzer import AlertAnalyzer, CLAUDE_API_URL, CLAUDE_MODEL, CLAUDE_API_VERSION
from src.models import Alert


# ---------------------------------------------------------------------------
# Eval scenarios: each represents a distinct alert pattern the analyzer
# should be able to interpret correctly.
# ---------------------------------------------------------------------------

SCENARIOS = [
    {
        "name": "Single machine failure",
        "description": "One machine dominates all errors — suggests machine-specific issue",
        "alert": Alert.create(
            window_start=datetime(2026, 3, 6, 8, 0, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 3, 6, 10, 0, 0, tzinfo=timezone.utc),
            total_count=1200,
            threshold=1000,
            breakdown=[
                {"machine_name": "web-03", "error_code": "ERR_CONN_REFUSED", "count": 1100},
                {"machine_name": "web-01", "error_code": "ERR_TIMEOUT", "count": 60},
                {"machine_name": "web-02", "error_code": "ERR_TIMEOUT", "count": 40},
            ],
        ),
        "expected_mentions": [
            "web-03 is the primary source of errors",
            "ERR_CONN_REFUSED is the dominant error type",
            "machine-specific issue (not systemic)",
            "investigate web-03 specifically",
        ],
    },
    {
        "name": "Widespread timeout",
        "description": "All machines show the same error — suggests shared dependency",
        "alert": Alert.create(
            window_start=datetime(2026, 3, 6, 14, 0, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 3, 6, 16, 0, 0, tzinfo=timezone.utc),
            total_count=1500,
            threshold=1000,
            breakdown=[
                {"machine_name": "web-01", "error_code": "ERR_TIMEOUT", "count": 380},
                {"machine_name": "web-02", "error_code": "ERR_TIMEOUT", "count": 370},
                {"machine_name": "web-03", "error_code": "ERR_TIMEOUT", "count": 390},
                {"machine_name": "web-04", "error_code": "ERR_TIMEOUT", "count": 360},
            ],
        ),
        "expected_mentions": [
            "timeouts are spread evenly across all machines",
            "shared dependency or downstream service issue",
            "not a single-machine problem",
            "check shared infrastructure (database, network, load balancer)",
        ],
    },
    {
        "name": "Mixed errors on few machines",
        "description": "Multiple error types on a subset of machines — complex failure",
        "alert": Alert.create(
            window_start=datetime(2026, 3, 6, 6, 0, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 3, 6, 8, 0, 0, tzinfo=timezone.utc),
            total_count=1050,
            threshold=1000,
            breakdown=[
                {"machine_name": "web-01", "error_code": "ERR_OOM", "count": 400},
                {"machine_name": "web-01", "error_code": "ERR_DISK_FULL", "count": 300},
                {"machine_name": "web-02", "error_code": "ERR_OOM", "count": 200},
                {"machine_name": "web-02", "error_code": "ERR_DISK_FULL", "count": 150},
            ],
        ),
        "expected_mentions": [
            "web-01 is the most affected machine",
            "resource exhaustion (memory and disk)",
            "OOM and disk full suggest the machines are under-provisioned or have a resource leak",
            "check resource usage, consider scaling or cleanup",
        ],
    },
    {
        "name": "Authentication storm",
        "description": "Auth failures across all machines — possible credential/config issue",
        "alert": Alert.create(
            window_start=datetime(2026, 3, 6, 3, 0, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 3, 6, 5, 0, 0, tzinfo=timezone.utc),
            total_count=2000,
            threshold=1000,
            breakdown=[
                {"machine_name": "web-01", "error_code": "ERR_AUTH_FAILED", "count": 500},
                {"machine_name": "web-02", "error_code": "ERR_AUTH_FAILED", "count": 490},
                {"machine_name": "web-03", "error_code": "ERR_AUTH_FAILED", "count": 510},
                {"machine_name": "web-04", "error_code": "ERR_AUTH_FAILED", "count": 500},
            ],
        ),
        "expected_mentions": [
            "authentication failures across all machines",
            "credential or configuration issue (not machine-specific)",
            "expired certificate, rotated credentials, or auth service down",
            "check auth service health and credential configuration",
        ],
    },
    {
        "name": "Database deadlocks",
        "description": "DB deadlocks from app servers — suggests database contention",
        "alert": Alert.create(
            window_start=datetime(2026, 3, 6, 12, 0, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 3, 6, 13, 30, 0, tzinfo=timezone.utc),
            total_count=1100,
            threshold=1000,
            breakdown=[
                {"machine_name": "api-01", "error_code": "ERR_DB_DEADLOCK", "count": 600},
                {"machine_name": "api-02", "error_code": "ERR_DB_DEADLOCK", "count": 350},
                {"machine_name": "api-01", "error_code": "ERR_TIMEOUT", "count": 100},
                {"machine_name": "api-02", "error_code": "ERR_TIMEOUT", "count": 50},
            ],
        ),
        "expected_mentions": [
            "database deadlocks are the primary error",
            "api-01 is more affected than api-02",
            "database contention or problematic query pattern",
            "review database queries, check for lock contention",
        ],
    },
]


# ---------------------------------------------------------------------------
# LLM-as-judge: evaluates the quality of each analysis
# ---------------------------------------------------------------------------

def judge_analysis(analysis: str, scenario: dict, api_key: str) -> dict:
    """Use Claude to evaluate the quality of an analysis.

    Sends the analysis and expected criteria to Claude, asks it to score
    each criterion (met/partially met/not met) and provide an overall
    score from 0.0 to 1.0.

    Returns a dict with:
      - criteria_scores: list of {criterion, score, reasoning}
      - overall_score: float 0.0-1.0
      - judge_reasoning: str
    """
    criteria_text = "\n".join(
        f"  {i+1}. {c}" for i, c in enumerate(scenario["expected_mentions"])
    )

    judge_prompt = f"""You are evaluating the quality of an alert analysis produced by an AI system.

Scenario: {scenario['name']}
Description: {scenario['description']}

The AI produced this analysis:
---
{analysis}
---

Evaluate whether the analysis addresses each of these expected criteria:
{criteria_text}

For each criterion, score it as:
- 1.0 = fully addressed (the analysis clearly mentions or implies this)
- 0.5 = partially addressed (mentioned tangentially or incompletely)
- 0.0 = not addressed at all

Respond in this exact JSON format (no other text):
{{
  "criteria_scores": [
    {{"criterion": "...", "score": 1.0, "reasoning": "..."}},
    ...
  ],
  "overall_score": 0.85,
  "judge_reasoning": "Overall assessment of the analysis quality"
}}"""

    request_body = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": judge_prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        CLAUDE_API_URL,
        data=request_body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": CLAUDE_API_VERSION,
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    # Extract text from response.
    content = data.get("content", [])
    text = "\n".join(b["text"] for b in content if b.get("type") == "text")

    # Parse the JSON from the judge's response.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # If the judge didn't return valid JSON, return a failure score.
        return {
            "criteria_scores": [],
            "overall_score": 0.0,
            "judge_reasoning": f"Failed to parse judge response: {text[:200]}",
        }


# ---------------------------------------------------------------------------
# Main eval runner
# ---------------------------------------------------------------------------

def run_evals(api_key: str, verbose: bool = False):
    """Run all eval scenarios, score them, and print a summary."""
    analyzer = AlertAnalyzer(api_key=api_key)

    print("=" * 70)
    print("  ALERT ANALYZER EVALUATION SUITE")
    print("=" * 70)
    print(f"  Model: {CLAUDE_MODEL}")
    print(f"  Scenarios: {len(SCENARIOS)}")
    print(f"  Scoring: LLM-as-judge (same model)")
    print("=" * 70)
    print()

    results = []

    for i, scenario in enumerate(SCENARIOS, 1):
        print(f"  [{i}/{len(SCENARIOS)}] {scenario['name']}...")

        # Step 1: Generate analysis.
        alert = scenario["alert"]
        prompt = analyzer._build_prompt(alert)
        try:
            analysis = analyzer._send_request(prompt)
        except Exception as e:
            print(f"    FAILED: {e}")
            results.append({"name": scenario["name"], "score": 0.0, "error": str(e)})
            continue

        if verbose:
            print(f"    Analysis:")
            for line in analysis.split("\n"):
                print(f"      {line}")
            print()

        # Step 2: Judge the analysis.
        try:
            judgment = judge_analysis(analysis, scenario, api_key)
        except Exception as e:
            print(f"    Judge failed: {e}")
            results.append({"name": scenario["name"], "score": 0.0, "error": str(e)})
            continue

        score = judgment.get("overall_score", 0.0)
        results.append({
            "name": scenario["name"],
            "score": score,
            "judgment": judgment,
        })

        # Print per-criterion scores.
        for cs in judgment.get("criteria_scores", []):
            marker = "+" if cs["score"] >= 0.75 else ("~" if cs["score"] >= 0.25 else "-")
            print(f"    [{marker}] {cs['score']:.1f}  {cs['criterion']}")

        print(f"    Score: {score:.2f}")
        if verbose and judgment.get("judge_reasoning"):
            print(f"    Judge: {judgment['judge_reasoning']}")
        print()

    # Print summary.
    print("=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    print(f"  {'Scenario':<40} {'Score':>8}")
    print(f"  {'-'*40} {'-'*8}")

    total_score = 0.0
    for r in results:
        score_str = f"{r['score']:.2f}" if "error" not in r else "ERROR"
        print(f"  {r['name']:<40} {score_str:>8}")
        total_score += r["score"]

    avg_score = total_score / len(results) if results else 0.0
    print(f"  {'-'*40} {'-'*8}")
    print(f"  {'OVERALL AVERAGE':<40} {avg_score:>8.2f}")
    print("=" * 70)

    # Exit with non-zero if average score is below threshold.
    if avg_score < 0.6:
        print(f"\n  WARN: Average score {avg_score:.2f} is below 0.60 threshold.")
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate the AlertAnalyzer's LLM-generated analysis quality",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print full analysis text for each scenario",
    )
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable is required.")
        print("Evals call the real Claude API to generate and judge analyses.")
        sys.exit(1)

    exit_code = run_evals(api_key, verbose=args.verbose)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
