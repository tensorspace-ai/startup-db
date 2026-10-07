"""Summarize retained provider timings without estimating unreported token usage."""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Any


def metrics(directory: Path) -> dict[str, Any]:
    samples: list[float] = []
    preparation: list[float] = []
    usage: dict[str, int] = {}
    errors = []
    attempts = measured_usage = quota_failures = timeouts = 0
    reported_cost = 0.0
    cost_attempts = 0
    for path in sorted(directory.rglob("attempt-*/process.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(item, dict):
                raise ValueError("process.json must be an object")
            attempts += 1
            seconds = item.get("provider_seconds")
            if isinstance(seconds, (float, int)) and math.isfinite(seconds) and seconds >= 0:
                samples.append(seconds)
            if isinstance(item.get("preparation_seconds"), (float, int)):
                preparation.append(item["preparation_seconds"])
            timeouts += bool(item.get("timed_out"))
            quota_failures += item.get("failure_kind") == "quota"
            if isinstance(item.get("usage"), dict):
                measured_usage += 1
                for key, count in item["usage"].items():
                    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                        usage[key] = usage.get(key, 0) + count
            cost = item.get("reported_cost_usd")
            if isinstance(cost, (float, int)) and math.isfinite(cost) and cost >= 0:
                cost_attempts += 1
                reported_cost += cost
        except (OSError, ValueError, TypeError) as exc:
            errors.append({"file": str(path), "error": str(exc)})
    ordered = sorted(samples)
    return {
        "attempts": attempts,
        "timed_attempts": len(samples),
        "provider_seconds": {
            "total": sum(samples),
            "median": statistics.median(samples),
            "p95": ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)],
        }
        if samples
        else None,
        "preparation_seconds_total": sum(preparation) if preparation else None,
        "timeouts": timeouts,
        "quota_failures": quota_failures,
        "usage": usage or None,
        "attempts_with_reported_usage": measured_usage,
        "attempts_without_reported_usage": attempts - measured_usage,
        "reported_cost_usd": reported_cost if cost_attempts else None,
        "attempts_with_reported_cost": cost_attempts,
        "errors": errors,
        "note": "Only retained CLI telemetry is counted. Missing usage/cost is unknown. Provider-specific cache token semantics are preserved. Timing excludes cooldowns, network work outside the CLI, and user review; no end-to-end or dollar cost is inferred.",
    }
