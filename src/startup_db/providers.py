"""CLI adapters, process lifecycle, and failure-channel-only retry classification."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import math
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Settings

FATAL = re.compile(
    r"insufficient[ _-]?(?:credits?|funds|quota)|credit balance|invalid[ _-]?(?:api[ _-]?key|token)|authentication[ _-]?error|not logged in|please run /login|oauth token.*(?:expired|revoked|invalid)|unknown model|model.*(?:not found|not supported|does not exist)|unknown (?:option|flag|feature)|unrecognized arguments",
    re.I,
)
QUOTA = re.compile(
    r"rate[ _-]?limit|usage[ _-]?limit|too many requests|resource[ _-]?exhausted|quota.*(?:exceeded|exhausted|reached|reset)|out of quota|overloaded|\b(?:http|status|error|code)\D{0,12}429\b|limit to reset",
    re.I,
)
DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(days?|hours?|hrs?|minutes?|mins?|seconds?|secs?)", re.I)
UNITS = {"day": 86400, "hour": 3600, "hr": 3600, "minute": 60, "min": 60, "second": 1, "sec": 1}


@dataclass(frozen=True)
class Invocation:
    command: list[str]
    stdin: str | None


@dataclass(frozen=True)
class AgentResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    def events(self) -> Iterator[dict[str, Any]]:
        try:
            whole = json.loads(self.stdout)
        except ValueError:
            whole = None
        if isinstance(whole, dict):
            yield whole
            return
        for line in io.StringIO(self.stdout):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event

    @cached_property
    def event_summary(self) -> dict[str, Any]:
        """Tool results and research prose are never quota evidence."""
        failures: list[str] = []
        structured = False
        counts: dict[str, int] = {}
        usage: dict[str, int] = {}
        cost: float | None = None
        # Claude's single pretty JSON object and JSONL streams are both supported.
        for event in self.events():
            structured = True
            kind = str(event.get("type", "unknown"))
            counts[kind] = counts.get(kind, 0) + 1
            if kind == "item.completed" and isinstance(event.get("item"), dict):
                item_kind = "item:" + str(event["item"].get("type", "unknown"))
                counts[item_kind] = counts.get(item_kind, 0) + 1
            if kind in {"turn.completed", "result", "response.completed"}:
                reported = event.get("usage", {})
                if isinstance(reported, dict):
                    for key in (
                        "input_tokens",
                        "output_tokens",
                        "cached_input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    ):
                        value = reported.get(key)
                        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                            usage[key] = usage.get(key, 0) + value
                reported_cost = event.get("total_cost_usd")
                if (
                    isinstance(reported_cost, (int, float))
                    and not isinstance(reported_cost, bool)
                    and math.isfinite(reported_cost)
                    and reported_cost >= 0
                ):
                    cost = (cost or 0) + reported_cost
            if kind in {"error", "turn.failed", "session.error", "response.failed"}:
                failures.append(json.dumps(event))
            elif event.get("is_error") is True:
                failures.append(str(event.get("result", event.get("error", ""))))
        # Plain stdout is a failure channel only for unsuccessful unstructured runs.
        if self.returncode and not structured:
            failures.append(self.stdout)
        if self.returncode or failures:
            failures.append(self.stderr)
        return {
            "failure_output": "\n".join(failures),
            "event_counts": counts,
            "usage": usage or None,
            "reported_cost_usd": cost,
        }

    def failure_output(self) -> str:
        return str(self.event_summary["failure_output"])


def invocation(settings: Settings, workspace: Path, prompt: str) -> Invocation:
    if settings.provider == "codex":
        command = [
            "codex",
            "--sandbox",
            settings.sandbox,
            "--ask-for-approval",
            settings.approval_policy,
        ]
        if settings.model:
            command += ["--model", settings.model]
        if settings.search:
            command.append("--search")
        command += [
            "exec",
            "--cd",
            str(workspace),
            "--skip-git-repo-check",
            "--color",
            "never",
            "--json",
            "-c",
            f'model_reasoning_effort="{settings.effort}"',
            "--output-last-message",
            str(workspace / "response.txt"),
        ]
        if settings.ephemeral:
            command.append("--ephemeral")
        return Invocation([*command, "-"], prompt)
    if settings.provider == "claude":
        command = [
            "claude",
            "--print",
            "--output-format",
            "json",
            "--model",
            settings.model or "opus",
            "--effort",
            settings.effort,
        ]
        if settings.claude_dangerously_skip_permissions:
            command.append("--dangerously-skip-permissions")
        else:
            command += [
                "--permission-mode",
                "acceptEdits",
                "--allowedTools",
                "WebSearch,WebFetch,Read,Write,Edit,Glob,Grep,Bash(curl:*),Bash(python3:*)",
            ]
        return Invocation(command, prompt)
    # Prefer the standalone CLI but retain installations exposed via GitHub CLI.
    command = ["copilot"] if shutil.which("copilot") else ["gh", "copilot", "--"]
    command += [
        "--model",
        settings.model or "gpt-5.4-mini",
        "--no-ask-user",
        "--allow-tool=write",
        "--allow-tool=shell(curl)",
        "--allow-tool=shell(python3)",
        "--allow-all-urls",
        "--output-format",
        "json",
    ]
    if settings.model != "auto":
        command += ["--effort", settings.effort]
    if settings.silent:
        command.append("--silent")
    return Invocation([*command, "-p", prompt], None)


def kill_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    elif process.poll() is None:
        process.kill()


def execute(
    call: Invocation, workspace: Path, timeout: float, stop: threading.Event | None = None
) -> AgentResult:
    try:
        process = subprocess.Popen(
            call.command,
            cwd=workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        return AgentResult(127, "", str(exc))
    try:
        started = time.monotonic()
        prompt = call.stdin
        while True:
            if stop is not None and stop.is_set():
                kill_process_tree(process)
                stdout, stderr = process.communicate()
                return AgentResult(130, stdout, stderr + "\nRun interrupted")
            if timeout and time.monotonic() - started >= timeout:
                kill_process_tree(process)
                stdout, stderr = process.communicate()
                return AgentResult(124, stdout, stderr + "\nAgent timed out", timed_out=True)
            try:
                polling = (
                    min(0.5, max(0.001, timeout - (time.monotonic() - started))) if timeout else 0.5
                )
                stdout, stderr = process.communicate(prompt, timeout=polling)
                return AgentResult(process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                prompt = None
    except BaseException:
        kill_process_tree(process)
        process.communicate()
        raise


def quota_wait(output: str, now: dt.datetime | None = None) -> float | None:
    current = now or dt.datetime.now(dt.UTC)
    epoch = re.search(r"usage limit reached\|(\d{10,13})", output, re.I)
    if epoch:
        value = int(epoch[1])
        return max(1, value / (1000 if value >= 10**12 else 1) - current.timestamp())
    retry = re.search(r"retry[ _-]?after\D{0,4}(\d+(?:\.\d+)?)", output, re.I)
    if retry:
        return max(1, math.ceil(float(retry[1])))
    # Retry-After can be an HTTP date as well as a number.
    http_date = re.search(r"retry-after:\s*([A-Za-z]{3},[^\n]+GMT)", output, re.I)
    if http_date:
        from email.utils import parsedate_to_datetime

        try:
            return max(1, (parsedate_to_datetime(http_date[1]) - current).total_seconds())
        except ValueError:
            pass
    relative = re.search(
        r"(?:try again|retry|resets?|limit to reset)\s+(?:in|after)\s+([^\n]{1,100})", output, re.I
    )
    if relative:
        seconds = sum(
            float(amount) * UNITS[unit.lower().removesuffix("s")]
            for amount, unit in DURATION.findall(relative[1])
        )
        if seconds:
            return math.ceil(seconds)
    clock = re.search(
        r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)?(?:\s*\(([^)]+)\))?", output, re.I
    )
    if clock and (clock[2] or clock[3]):
        hour, minute = int(clock[1]), int(clock[2] or 0)
        if clock[3]:
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if clock[3].lower() == "pm" else 0)
        if hour > 23 or minute > 59:
            return None
        try:
            local = current.astimezone(ZoneInfo(clock[4])) if clock[4] else current.astimezone()
        except ZoneInfoNotFoundError:
            return None
        reset = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if reset <= local:
            reset += dt.timedelta(days=1)
        return (reset - local).total_seconds()
    return None


def failure_kind(result: AgentResult) -> Literal["fatal", "quota", "transient"]:
    output = result.failure_output()
    if result.returncode == 127 or FATAL.search(output):
        return "fatal"
    if QUOTA.search(output):
        return "quota"
    return "transient"


def wait_with_progress(seconds: float, stop: threading.Event | None = None) -> None:
    deadline = time.monotonic() + seconds
    while remaining := deadline - time.monotonic():
        if remaining <= 0:
            break
        print(f"Retry pause: {math.ceil(remaining)}s remaining", flush=True)
        if stop is None:
            time.sleep(min(remaining, 30))
        elif stop.wait(min(remaining, 30)):
            raise KeyboardInterrupt


def doctor() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for provider, command in (
        ("codex", ["codex", "--version"]),
        ("claude", ["claude", "--version"]),
        (
            "copilot",
            ["copilot", "--version"]
            if shutil.which("copilot")
            else ["gh", "copilot", "--", "--version"],
        ),
    ):
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=20, check=False
            )
            checks.append(
                {
                    "provider": provider,
                    "available": result.returncode == 0,
                    "version": result.stdout.strip(),
                    "error": result.stderr.strip(),
                }
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks.append({"provider": provider, "available": False, "error": str(exc)})
    return checks
