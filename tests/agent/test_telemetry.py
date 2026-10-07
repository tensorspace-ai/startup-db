import json

from startup_db.providers import AgentResult, failure_kind
from startup_db.telemetry import metrics


def test_cli_event_summary_cached_and_usage_is_not_guessed():
    stdout = "\n".join(
        json.dumps(item)
        for item in [
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "usage": {"input_tokens": 999999}},
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 40},
            },
        ]
    )
    result = AgentResult(0, stdout, "")
    assert result.event_summary["usage"] == {
        "input_tokens": 100,
        "cached_input_tokens": 20,
        "output_tokens": 40,
    }
    assert result.event_summary is result.event_summary
    assert result.event_summary["event_counts"]["item:command_execution"] == 1
    assert AgentResult(0, "plain prose", "").event_summary["usage"] is None


def test_pretty_claude_result_failure_and_reported_cost():
    result = AgentResult(
        0,
        json.dumps(
            {
                "type": "result",
                "is_error": True,
                "result": "usage limit reached",
                "total_cost_usd": 0.07,
                "usage": {"input_tokens": 45},
            },
            indent=2,
        ),
        "",
    )
    assert failure_kind(result) == "quota"
    assert result.event_summary["reported_cost_usd"] == 0.07


def test_retained_metrics_keep_missing_usage_unknown(tmp_path):
    for i, content in enumerate(
        [
            {
                "provider_seconds": 10,
                "preparation_seconds": 0.1,
                "usage": {"input_tokens": 100},
                "reported_cost_usd": 0.2,
            },
            {"provider_seconds": 20, "failure_kind": "quota", "usage": None},
            {"provider_seconds": 30, "timed_out": True},
        ]
    ):
        directory = tmp_path / "run" / "job" / f"attempt-{i:04d}"
        directory.mkdir(parents=True)
        (directory / "process.json").write_text(json.dumps(content))
    result = metrics(tmp_path)
    assert result["provider_seconds"] == {"total": 60, "median": 20, "p95": 30}
    assert result["attempts_without_reported_usage"] == 2
    assert result["usage"] == {"input_tokens": 100}
    assert result["quota_failures"] == result["timeouts"] == 1
    assert result["reported_cost_usd"] == 0.2
    assert metrics(tmp_path / "missing")["usage"] is None
