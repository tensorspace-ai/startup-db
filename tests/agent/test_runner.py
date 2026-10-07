import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import tomlkit

from startup_db.catalog import load_toml
from startup_db.config import Settings
from startup_db.providers import AgentResult
from startup_db.runner import Runner, dry_run, research_selection
from startup_db.staging import repair_context, write_checker
from startup_db.storage import PublicationError, digest


@pytest.fixture
def settings(tmp_path):
    directory = tmp_path / "data" / "startups"
    directory.mkdir(parents=True)
    return Settings(
        root=tmp_path,
        directory=directory,
        state_dir=tmp_path / ".agent-runs",
        min_sources=2,
        min_words=0,
        sleep=0,
        failure_backoff=0,
        rate_limit_default_wait=0,
        rate_limit_fudge=0,
    ).resolved()


def fake_provider(monkeypatch, record, callback=None):
    def execute(call, workspace, timeout, stop):
        if callback:
            result = callback(workspace)
            if result:
                return result
        (workspace / "candidate.toml").write_text(tomlkit.dumps(record))
        return AgentResult(0, json.dumps({"type": "turn.completed"}), "")

    monkeypatch.setattr("startup_db.providers.execute", execute)


def test_discovery_publishes_and_checkpoint_resume_skips(settings, record, monkeypatch):
    fake_provider(monkeypatch, record)
    runner = Runner(settings, "discover")
    assert runner.run() == 0
    target = settings.directory / "test-robotics.toml"
    assert load_toml(target)["intelligence"]["schema_version"] == 2
    job = runner.store.jobs(runner.run_id)[0]
    assert job["status"] == "published" and job["attempts"] == 1
    assert (Path(job["artifact"]) / "validation.json").is_file()
    process = json.loads((Path(job["artifact"]) / "process.json").read_text())
    assert process["provider_seconds"] >= 0
    assert process["usage"] is None
    monkeypatch.setattr(
        "startup_db.providers.execute", lambda *_: pytest.fail("completed job reran")
    )
    assert Runner(settings, "discover", resume=runner.run_id).run() == 0


def test_enrichment_preserves_extra_fields_and_comments(settings, record, monkeypatch):
    original = copy.deepcopy(record)
    original["tags"] = ["retain-me"]
    target = settings.directory / "test-robotics.toml"
    target.write_text("# Authored profile\n" + tomlkit.dumps(original))
    record["description"] = "Updated source-backed description"
    fake_provider(monkeypatch, record)
    assert Runner(settings, "improve").run() == 0
    assert load_toml(target)["tags"] == ["retain-me"]
    assert "# Authored profile" in target.read_text()
    assert load_toml(target)["description"] == record["description"]


def test_invalid_candidate_never_enters_live_database(settings, record, monkeypatch):
    record["intelligence"]["funding"]["rounds"] = [{"amount": 10}]
    fake_provider(monkeypatch, record)
    runner = Runner(settings.model_copy(update={"max_attempts": 2}), "discover")
    assert runner.run() == 1
    assert list(settings.directory.glob("*.toml")) == []
    job = runner.store.jobs(runner.run_id)[0]
    assert job["attempts"] == 2 and job["status"] == "failed"
    prompt = (Path(job["artifact"]) / "prompt.txt").read_text()
    assert "intelligence.funding.rounds.0" in prompt


def test_concurrent_user_edit_preserved(settings, record, monkeypatch):
    target = settings.directory / "test-robotics.toml"
    target.write_text(tomlkit.dumps(record))

    def edit_during_research(workspace):
        target.write_text(target.read_text() + "\n# User change during research\n")

    fake_provider(monkeypatch, record, edit_during_research)
    runner = Runner(settings, "improve")
    assert runner.run() == 1
    assert "# User change during research" in target.read_text()
    assert "changed since selection" in runner.store.jobs(runner.run_id)[0]["error"]


def test_success_with_quota_prose_does_not_pause(settings, record, monkeypatch):
    def execute(call, workspace, timeout, stop):
        (workspace / "candidate.toml").write_text(tomlkit.dumps(record))
        return AgentResult(
            0,
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "rate limit exceeded"},
                }
            ),
            "",
        )

    monkeypatch.setattr("startup_db.providers.execute", execute)
    monkeypatch.setattr(
        "startup_db.providers.wait_with_progress", lambda *_: pytest.fail("false retry")
    )
    assert Runner(settings, "discover").run() == 0


def test_quota_retries_preserve_success_count(settings, record, monkeypatch):
    calls = []

    def once(workspace):
        calls.append(workspace)
        if len(calls) == 1:
            return AgentResult(
                0,
                json.dumps({"type": "turn.failed", "error": {"message": "rate limit exceeded"}}),
                "",
            )

    fake_provider(monkeypatch, record, once)
    runner = Runner(settings, "discover")
    assert runner.run() == 0
    assert len(calls) == 2
    assert runner.store.jobs(runner.run_id)[0]["attempts"] == 2


def test_publishing_checkpoint_recovers_after_crash(settings, record, monkeypatch):
    fake_provider(monkeypatch, record)
    runner = Runner(settings, "discover")
    runner.prepare()
    key = runner.store.jobs(runner.run_id)[0]["key"]
    target = settings.directory / "test-robotics.toml"
    content = tomlkit.dumps(record).encode()
    target.write_bytes(content)
    runner.store.update_job(
        runner.run_id, key, status="publishing", target=str(target), digest=digest(content)
    )
    monkeypatch.setattr(
        "startup_db.providers.execute",
        lambda *_: pytest.fail("already-published output reran"),
    )
    assert Runner(settings, "discover", resume=runner.run_id).run() == 0


@pytest.mark.parametrize("mode", ["discover", "refresh"])
def test_successful_provider_output_recovers_before_validation(settings, record, monkeypatch, mode):
    if mode == "refresh":
        (settings.directory / "company.toml").write_text(tomlkit.dumps(record))
    runner = Runner(settings, mode)
    runner.prepare()
    job = runner.store.jobs(runner.run_id)[0]
    workspace = runner.run_dir / job["key"] / "attempt-0001"
    workspace.mkdir(parents=True)
    if mode == "refresh":
        check = record["intelligence"]["research"]["topic_checks"]
        (workspace / "update.json").write_text(
            json.dumps(
                {
                    "updates": {
                        "description": "Already researched before interruption",
                        "intelligence": {
                            "research": {
                                "topic_checks": [
                                    c
                                    for c in check
                                    if c["topic"]
                                    in [
                                        "funding",
                                        "investors",
                                        "team",
                                        "ownership",
                                        "financials",
                                        "traction",
                                    ]
                                ]
                            }
                        },
                    }
                }
            )
        )
    else:
        (workspace / "candidate.toml").write_text(tomlkit.dumps(record))
    (workspace / "process.json").write_text(json.dumps({"returncode": 0, "failure_kind": None}))
    runner.store.update_job(
        runner.run_id, job["key"], status="running", attempts=1, artifact=str(workspace)
    )
    monkeypatch.setattr(
        "startup_db.providers.execute", lambda *_: pytest.fail("completed research reran")
    )
    assert Runner(settings, mode, resume=runner.run_id).run() == 0
    recovered = runner.store.job(runner.run_id, job["key"])
    assert recovered["status"] == "published" and recovered["attempts"] == 1
    assert json.loads((workspace / "validation.json").read_text())["ok"]


@pytest.mark.parametrize(
    "process",
    [
        {"returncode": 130, "failure_kind": "transient"},
        {"returncode": 0, "failure_kind": "quota"},
        {"returncode": 0, "failure_kind": None, "timed_out": True},
    ],
)
def test_resume_never_recovers_unsuccessful_provider_candidates(
    settings, record, monkeypatch, process
):
    runner = Runner(settings, "discover")
    runner.prepare()
    job = runner.store.jobs(runner.run_id)[0]
    workspace = runner.run_dir / job["key"] / "attempt-0001"
    workspace.mkdir(parents=True)
    (workspace / "candidate.toml").write_text(
        tomlkit.dumps({**record, "description": "Untrusted partial result"})
    )
    (workspace / "process.json").write_text(json.dumps(process))
    runner.store.update_job(
        runner.run_id, job["key"], status="pending", attempts=1, artifact=str(workspace)
    )
    calls = []
    fake_provider(monkeypatch, record, lambda workspace: calls.append(workspace))
    assert Runner(settings, "discover", resume=runner.run_id).run() == 0
    assert len(calls) == 1
    assert (
        load_toml(settings.directory / "test-robotics.toml")["description"] == record["description"]
    )


def test_resume_detects_post_publication_edits(settings, record, monkeypatch):
    fake_provider(monkeypatch, record)
    runner = Runner(settings, "discover")
    assert runner.run() == 0
    target = settings.directory / "test-robotics.toml"
    target.write_text(target.read_text() + "\n# Later user edit\n")
    with pytest.raises(PublicationError, match="changed or disappeared"):
        Runner(settings, "discover", resume=runner.run_id).run()


def test_failed_run_resumes_same_settings(settings, record, monkeypatch):
    bad = copy.deepcopy(record)
    bad["name"] = ""
    fake_provider(monkeypatch, bad)
    runner = Runner(settings.model_copy(update={"max_attempts": 1}), "discover")
    assert runner.run() == 1
    fake_provider(monkeypatch, record)
    resumed = Runner(
        settings.model_copy(update={"model": "ignored", "limit": 99}),
        "discover",
        resume=runner.run_id,
    )
    assert resumed.settings.limit == 1 and resumed.settings.model is None
    assert resumed.run() == 0


def test_resume_repairs_rejected_draft_with_original_baseline(settings, record, monkeypatch):
    target = settings.directory / "test-robotics.toml"
    baseline = tomlkit.dumps(record)
    target.write_text(baseline)
    bad = copy.deepcopy(record)
    bad["description"] = "New researched description retained for repair"
    for check in bad["intelligence"]["research"]["claim_checks"]:
        check["field_path"] = "intelligence." + check["field_path"]
    fake_provider(monkeypatch, bad)
    runner = Runner(settings.model_copy(update={"max_attempts": 1}), "refresh")
    assert runner.run() == 1
    assert target.read_text() == baseline

    def repair(call, workspace, timeout, stop):
        previous = load_toml(workspace / "previous_candidate.toml")
        assert previous["description"] == bad["description"]
        assert (workspace / "current.toml").read_text() == baseline
        prompt = (workspace / "prompt.txt").read_text()
        assert "company.operating_status, israel_connection" in prompt
        assert "python3 check_candidate.py" in prompt
        assert "Repair the existing research" in prompt
        assert "Explicitly search ALL" not in prompt
        for check in previous["intelligence"]["research"]["claim_checks"]:
            check["field_path"] = check["field_path"].removeprefix("intelligence.")
        (workspace / "candidate.toml").write_text(tomlkit.dumps(previous))
        checked = subprocess.run(
            [shutil.which("python3") or sys.executable, "check_candidate.py"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
        assert json.loads(checked.stdout)["ok"]
        assert target.read_text() == baseline  # Preflight never publishes.
        return AgentResult(0, "", "")

    monkeypatch.setattr("startup_db.providers.execute", repair)
    assert Runner(settings, "refresh", resume=runner.run_id).run() == 0
    assert load_toml(target)["description"] == bad["description"]


@pytest.mark.parametrize("failure", ["missing_lists", "claim_paths", "disjoint_sources"])
def test_staged_checker_catches_rules_before_publication(settings, record, tmp_path, failure):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_checker(workspace, settings, "discover", None, None)
    intelligence = record["intelligence"]
    if failure == "missing_lists":
        for key in ("financials", "traction", "intellectual_property", "comparables"):
            intelligence.pop(key)
    elif failure == "claim_paths":
        for check in intelligence["research"]["claim_checks"]:
            check["field_path"] = "intelligence." + check["field_path"]
    else:
        for check in intelligence["research"]["claim_checks"]:
            check["source_ids"] = ["s2"]
    (workspace / "candidate.toml").write_text(tomlkit.dumps(record))
    checked = subprocess.run(
        [shutil.which("python3") or sys.executable, "check_candidate.py"],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 1, checked.stdout + checked.stderr
    report = json.loads(checked.stdout)
    assert not report["ok"]
    errors = " ".join(report["errors"])
    if failure == "missing_lists":
        for key in ("financials", "traction", "intellectual_property", "comparables"):
            assert f"intelligence.{key}: Field required" in errors
    else:
        assert "company.operating_status, israel_connection" in errors
        assert "relative to intelligence" in errors
    assert list(settings.directory.glob("*.toml")) == []


def test_repair_context_skips_interrupted_or_failed_provider_output(tmp_path):
    first = tmp_path / "attempt-0001"
    failed = tmp_path / "attempt-0002"
    interrupted = tmp_path / "attempt-0003"
    next_workspace = tmp_path / "attempt-0004"
    for workspace in (first, failed, interrupted, next_workspace):
        workspace.mkdir()
    for workspace in (first, failed):
        (workspace / "validation.json").write_text(
            json.dumps({"ok": False, "errors": ["missing claim verification"]})
        )
    (first / "process.json").write_text(json.dumps({"returncode": 0, "failure_kind": None}))
    (first / "update.json").write_text('{"updates": {}}')
    (failed / "process.json").write_text(json.dumps({"returncode": 1, "failure_kind": "quota"}))
    (failed / "candidate.toml").write_text("unusable provider output")
    (interrupted / "candidate.toml").write_text("unfinished output")
    assert repair_context(tmp_path, next_workspace) == ["missing claim verification"]
    assert (next_workspace / "previous_update.json").read_text() == '{"updates": {}}'
    assert not (next_workspace / "previous_candidate.toml").exists()


def test_parse_failure_retains_draft_and_feedback_for_repair(settings, record, monkeypatch):
    calls = []

    def execute(call, workspace, timeout, stop):
        calls.append(workspace)
        if len(calls) == 1:
            (workspace / "candidate.toml").write_text('name = "unclosed')
        else:
            assert (workspace / "previous_candidate.toml").read_text() == 'name = "unclosed'
            prompt = (workspace / "prompt.txt").read_text()
            errors = json.loads((calls[0] / "validation.json").read_text())["errors"]
            assert "Repair the existing research" in prompt
            assert errors and all(error in prompt for error in errors)
            (workspace / "candidate.toml").write_text(tomlkit.dumps(record))
        return AgentResult(0, "", "")

    monkeypatch.setattr("startup_db.providers.execute", execute)
    assert Runner(settings, "discover").run() == 0
    assert len(calls) == 2


def test_dry_run_has_no_side_effects(settings):
    before = set(settings.root.iterdir())
    assert len(dry_run(settings, "discover")) == 1
    assert set(settings.root.iterdir()) == before


def test_second_discovery_is_checked_against_first_publication(settings, record, monkeypatch):
    fake_provider(monkeypatch, record)
    runner = Runner(settings.model_copy(update={"limit": 2, "max_attempts": 1}), "discover")
    assert runner.run() == 1
    jobs = runner.store.jobs(runner.run_id)
    assert [job["status"] for job in jobs] == ["published", "failed"]
    assert len(list(settings.directory.glob("*.toml"))) == 1


def test_fatal_auth_error_stops_immediately(settings, record, monkeypatch):
    def fail(call, workspace, timeout, stop):
        # Even a plausible partial file must not publish after a provider failure.
        (workspace / "candidate.toml").write_text(tomlkit.dumps(record))
        return AgentResult(
            1, json.dumps({"type": "turn.failed", "error": {"message": "invalid api key"}}), ""
        )

    monkeypatch.setattr("startup_db.providers.execute", fail)
    runner = Runner(settings, "discover")
    assert runner.run() == 1
    assert runner.store.jobs(runner.run_id)[0]["attempts"] == 1
    assert list(settings.directory.glob("*.toml")) == []


def test_multiple_enrichment_workers_keep_distinct_targets(settings, record, monkeypatch):
    for number in range(3):
        item = copy.deepcopy(record)
        item["name"] = f"Unique Company {number}"
        item["intelligence"]["company"]["company_id"] = f"il-unique-{number}"
        item["website"] = f"https://unique-{number}.example"
        (settings.directory / f"company-{number}.toml").write_text(tomlkit.dumps(item))

    def execute(call, workspace, timeout, stop):
        data = load_toml(workspace / "current.toml")
        data["description"] = "Enriched " + data["name"]
        (workspace / "candidate.toml").write_text(tomlkit.dumps(data))
        return AgentResult(0, "", "")

    monkeypatch.setattr("startup_db.providers.execute", execute)
    # Names deliberately share a template; exact domains and identities remain distinct.
    runner = Runner(
        settings.model_copy(update={"jobs": 3, "limit": 0, "duplicate_threshold": 1}), "improve"
    )
    assert runner.run() == 0
    assert all(job["status"] == "published" for job in runner.store.jobs(runner.run_id))


def test_due_only_skips_fresh_topics_and_prioritizes_missing_checks(settings, record):
    from startup_db.runner import research_selection

    record["intelligence"]["research"]["topic_checks"][1]["checked_at"] = "2099-01-01T00:00:00Z"
    record["intelligence"]["research"]["topic_checks"][1]["next_review_on"] = "2099-02-01"
    target = settings.directory / "fresh.toml"
    target.write_text(tomlkit.dumps(record))
    due_settings = settings.model_copy(update={"due_only": True, "topics": ["funding"]})
    assert research_selection(due_settings, "refresh") == []
    stale = copy.deepcopy(record)
    stale["intelligence"]["research"]["topic_checks"] = []
    (settings.directory / "stale.toml").write_text(tomlkit.dumps(stale))
    assert [item.path.name for item in research_selection(due_settings, "refresh")] == [
        "stale.toml"
    ]


def exhausted_refresh(settings, record):
    for check in record["intelligence"]["research"]["topic_checks"]:
        check.update(checked_at="2020-01-01T00:00:00Z", next_review_on="2020-02-01")
    target = settings.directory / "a-failed.toml"
    target.write_text(tomlkit.dumps(record))
    due = settings.model_copy(update={"due_only": True})
    runner = Runner(due, "refresh")
    runner.prepare()
    runner.store.update_job(
        runner.run_id,
        target.name,
        status="failed",
        attempts=settings.max_attempts,
        error="duplicate identity",
        report=json.dumps({"errors": ["duplicate identity"]}),
    )
    runner.store.finish(runner.run_id, "failed")
    return runner, target, due


def test_new_refresh_defers_unchanged_failure_before_applying_limit(settings, record, monkeypatch):
    previous, target, due = exhausted_refresh(settings, record)
    deferred = []
    assert research_selection(due, "refresh", deferred) == []
    assert target.name in deferred[0] and previous.run_id in deferred[0]
    monkeypatch.setattr(
        "startup_db.providers.execute", lambda *_: pytest.fail("failed record reran")
    )
    assert Runner(due, "refresh").run() == 0
    other = settings.directory / "b-eligible.toml"
    other.write_text(tomlkit.dumps(record))
    assert [r.path.name for r in research_selection(due, "refresh")] == [other.name]
    # The original run and its failed checkpoint remain available for explicit resume.
    resumed = Runner(settings, "refresh", resume=previous.run_id)
    resumed.prepare()
    assert resumed.store.jobs(resumed.run_id)[0]["status"] == "failed"


@pytest.mark.parametrize(
    "change", ["edit", "topics", "contract", "force", "retry", "expired", "provider", "partial"]
)
def test_refresh_failure_deferral_does_not_hide_retryable_work(settings, record, change):
    previous, target, due = exhausted_refresh(settings, record)
    if change == "edit":
        target.write_text(target.read_text() + "\n# Reviewed identity\n")
    elif change == "topics":
        due = due.model_copy(update={"topics": ["funding"]})
    elif change == "contract":
        due = due.model_copy(update={"min_sources": 3})
    elif change == "force":
        due = due.model_copy(update={"due_only": False})
    elif change == "retry":
        due = due.model_copy(update={"retry_failed_days": 0})
    elif change == "expired":
        with previous.store.connection() as db:
            db.execute(
                "UPDATE runs SET updated_at='2020-01-01T00:00:00Z' WHERE id=?", (previous.run_id,)
            )
    elif change == "provider":
        previous.store.update_job(previous.run_id, target.name, error="Provider timed out")
    elif change == "partial":
        previous.store.update_job(previous.run_id, target.name, attempts=1)
    assert [r.path.name for r in research_selection(due, "refresh")] == [target.name]


def test_successful_newer_refresh_supersedes_failed_checkpoint(settings, record):
    _previous, target, due = exhausted_refresh(settings, record)
    successful = Runner(due.model_copy(update={"due_only": False}), "refresh")
    successful.prepare()
    successful.store.update_job(successful.run_id, target.name, status="published")
    successful.store.finish(successful.run_id, "complete")
    assert [r.path.name for r in research_selection(due, "refresh")] == [target.name]


def test_due_only_refreshes_only_due_topics_and_checkpoints_scope(settings, record, monkeypatch):
    for check in record["intelligence"]["research"]["topic_checks"]:
        check["checked_at"] = "2099-01-01T00:00:00Z"
        check["next_review_on"] = "2099-02-01"
    funding = record["intelligence"]["research"]["topic_checks"][1]
    funding["checked_at"] = "2020-01-01T00:00:00Z"
    funding["next_review_on"] = "2020-02-01"
    target = settings.directory / "company.toml"
    target.write_text(tomlkit.dumps(record))
    runner = Runner(settings.model_copy(update={"due_only": True}), "refresh")
    runner.prepare()
    assert json.loads(runner.store.jobs(runner.run_id)[0]["topics"]) == ["funding"]

    def execute(call, workspace, timeout, stop):
        assert "Research ONLY these topics: funding." in (workspace / "prompt.txt").read_text()
        current = json.loads((workspace / "current.json").read_text())
        assert "people" not in current["intelligence"]
        assert "financials" not in current["intelligence"]
        assert len(current["intelligence"]["research"]["topic_checks"]) == 1
        assert "claims" not in current["intelligence"]["research"]["sources"][0]
        (workspace / "update.json").write_text(
            json.dumps(
                {
                    "updates": {
                        "intelligence": {
                            "research": {
                                "topic_checks": [{**funding, "summary": "Reviewed funding only"}]
                            }
                        }
                    }
                }
            )
        )
        return AgentResult(0, "", "")

    monkeypatch.setattr("startup_db.providers.execute", execute)
    resumed = Runner(settings, "refresh", resume=runner.run_id)
    assert resumed.run() == 0
    published = load_toml(target)["intelligence"]
    assert (
        published["research"]["topic_checks"][0]
        == record["intelligence"]["research"]["topic_checks"][0]
    )
    assert published["people"] == record["intelligence"]["people"]


def test_legacy_refresh_does_not_need_to_rewrite_preserved_prose(settings, record, monkeypatch):
    legacy = copy.deepcopy(record)
    legacy.pop("intelligence")
    legacy["custom_metadata"] = {"keep": True}
    target = settings.directory / "company.toml"
    target.write_text("# Authored analysis\n" + tomlkit.dumps(legacy))

    def execute(call, workspace, timeout, stop):
        current = json.loads((workspace / "current.json").read_text())
        assert "detailed_description" not in current
        assert "detailed_description" in current["retained_analysis_fields"]
        assert "schema_version*: 1|2" in (workspace / "schema-guide.txt").read_text()
        (workspace / "candidate.toml").write_text(
            tomlkit.dumps(
                {"intelligence": record["intelligence"], "updated_at": record["updated_at"]}
            )
        )
        return AgentResult(0, "", "")

    monkeypatch.setattr("startup_db.providers.execute", execute)
    assert Runner(settings, "refresh").run() == 0
    assert load_toml(target)["detailed_description"] == record["detailed_description"]
    assert load_toml(target)["custom_metadata"] == {"keep": True}
    assert "# Authored analysis" in target.read_text()


def test_refresh_patch_is_published_with_compact_context(settings, record, monkeypatch):
    target = settings.directory / "test-robotics.toml"
    target.write_text(tomlkit.dumps(record))

    def execute(call, workspace, timeout, stop):
        current = json.loads((workspace / "current.json").read_text())
        assert "detailed_description" not in current
        assert "intelligence" in current
        assert "current.json" in (workspace / "prompt.txt").read_text()
        check = copy.deepcopy(record["intelligence"]["research"]["topic_checks"][1])
        check["summary"] = "No new funding announcement in this review"
        (workspace / "update.json").write_text(
            json.dumps({"updates": {"intelligence": {"research": {"topic_checks": [check]}}}})
        )
        checked = subprocess.run(
            [sys.executable, "check_candidate.py"], cwd=workspace, capture_output=True, text=True
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
        return AgentResult(
            0,
            json.dumps(
                {"type": "turn.completed", "usage": {"input_tokens": 120, "output_tokens": 40}}
            ),
            "",
        )

    monkeypatch.setattr("startup_db.providers.execute", execute)
    runner = Runner(settings.model_copy(update={"topics": ["funding"]}), "refresh")
    assert runner.run() == 0
    job = runner.store.jobs(runner.run_id)[0]
    process = json.loads((Path(job["artifact"]) / "process.json").read_text())
    assert process["usage"]["input_tokens"] == 120
    assert load_toml(target)["detailed_description"] == record["detailed_description"]
