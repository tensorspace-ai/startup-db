import datetime as dt
import json
import sys
import threading

import pytest

from startup_db.config import Settings
from startup_db.providers import (
    AgentResult,
    Invocation,
    execute,
    failure_kind,
    invocation,
    quota_wait,
)


def test_research_and_tool_output_cannot_trigger_quota():
    events = [
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Company sells rate-limit protection; quota exhausted",
            },
        },
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "exit_code": 1, "aggregated_output": "Error 429"},
        },
        {"type": "turn.completed"},
    ]
    result = AgentResult(0, "\n".join(json.dumps(event) for event in events), "Rate-limit warning")
    assert result.failure_output() == ""
    assert failure_kind(result) == "transient"
    # A failed process with structured research output still cannot promote prose to errors.
    result = AgentResult(1, json.dumps(events[0]), "network disconnected")
    assert failure_kind(result) == "transient"


def test_structured_failure_even_with_zero_exit():
    for event in (
        {"type": "turn.failed", "error": {"message": "usage limit reached"}},
        {"type": "result", "is_error": True, "result": "Too many requests"},
    ):
        assert failure_kind(AgentResult(0, json.dumps(event), "")) == "quota"


def test_fatal_auth_and_billing_do_not_retry_as_quota():
    assert failure_kind(AgentResult(1, "insufficient_quota", "")) == "fatal"
    assert failure_kind(AgentResult(127, "", "missing binary")) == "fatal"
    assert failure_kind(AgentResult(1, "rate limit, invalid api key", "")) == "fatal"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("429 retry-after: 12.5", 13),
        ("Usage limit reached. Try again in 2 hours 30 minutes", 9000),
        ("Please wait for the limit to reset in 3 minutes", 180),
        ("Claude AI usage limit reached|1893542400000", 86400),
        ("resets at 3pm (UTC)", 54000),
        ("retry-after: Tue, 01 Jan 2030 00:01:00 GMT", 60),
    ],
)
def test_quota_wait_formats(message, expected):
    assert quota_wait(message, dt.datetime(2030, 1, 1, tzinfo=dt.UTC)) == expected


def test_copilot_auto_omits_effort(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda _: None)
    call = invocation(Settings(provider="copilot", model="auto"), tmp_path, "research")
    assert call.command[:3] == ["gh", "copilot", "--"]
    assert "--effort" not in call.command
    assert call.stdin is None


def test_codex_uses_staged_cwd_and_configured_model(tmp_path):
    call = invocation(Settings(), tmp_path, "research")
    assert "--search" in call.command and "--json" in call.command
    assert "--model" not in call.command
    assert call.command[call.command.index("--cd") + 1] == str(tmp_path)
    assert call.stdin == "research"


def test_claude_receives_effort_and_structured_output(tmp_path):
    call = invocation(Settings(provider="claude", effort="high"), tmp_path, "research")
    assert call.command[call.command.index("--effort") + 1] == "high"
    assert call.command[call.command.index("--output-format") + 1] == "json"
    assert "--dangerously-skip-permissions" not in call.command


def test_process_timeout_retains_partial_output(tmp_path):
    call = Invocation(
        [sys.executable, "-c", "import time; print('partial', flush=True); time.sleep(30)"], None
    )
    result = execute(call, tmp_path, 0.05)
    assert result.returncode == 124 and result.timed_out
    assert "partial" in result.stdout


def test_process_cancellation(tmp_path):
    stopped = threading.Event()
    stopped.set()
    call = Invocation([sys.executable, "-c", "import time; time.sleep(30)"], None)
    assert execute(call, tmp_path, 0, stopped).returncode == 130
