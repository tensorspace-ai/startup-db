"""Staged research jobs with bounded retries, resumable state, and atomic publication."""

from __future__ import annotations

import datetime as dt
import json
import shutil
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from filelock import FileLock

from . import providers
from .catalog import Record, catalog, selection, write_catalog
from .config import Settings
from .context import research_brief, schema_guide
from .models import Startup
from .patches import refresh_schema
from .planning import DEFAULT_REFRESH_TOPICS, research_plan
from .prompts import research_prompt
from .staging import check_candidate, repair_context, write_checker
from .state import Store, refresh_history
from .storage import PublicationError, digest, publish
from .validation import Report

_SCHEMA_TEXT = json.dumps(Startup.model_json_schema(), separators=(",", ":")) + "\n"
_REFRESH_SCHEMA_TEXT = json.dumps(refresh_schema(), separators=(",", ":")) + "\n"
_SCHEMA_GUIDE = schema_guide(json.loads(_SCHEMA_TEXT))
_REFRESH_SCHEMA_GUIDE = schema_guide(json.loads(_REFRESH_SCHEMA_TEXT))


def recent_refresh_failures(settings: Settings) -> dict[str, dict[str, Any]]:
    if not settings.retry_failed_days:
        return {}
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=settings.retry_failed_days)
    failures = {}
    for outcome in refresh_history(settings.state_dir):
        if outcome["status"] != "failed":
            continue
        previous = json.loads(outcome["settings"])
        report = json.loads(outcome["report"] or "{}")
        # Provider outages, quotas, and interrupted calls stay retryable. Only
        # an exhausted final validation rejection is eligible for deferral.
        if (
            dt.datetime.fromisoformat(outcome["updated_at"].replace("Z", "+00:00")) < cutoff
            or outcome["attempts"] < previous["max_attempts"]
            or not report.get("errors")
            or outcome["error"] != "; ".join(report["errors"])
            or any(
                previous.get(field) != getattr(settings, field)
                for field in (
                    "provider",
                    "model",
                    "effort",
                    "focus",
                    "min_words",
                    "min_sources",
                    "duplicate_threshold",
                    "max_attempts",
                )
            )
        ):
            continue
        outcome["scope"] = set(
            json.loads(outcome["topics"])
            if outcome["topics"] is not None
            else previous.get("topics") or DEFAULT_REFRESH_TOPICS
        )
        failures[outcome["target"]] = outcome
    return failures


def research_selection(
    settings: Settings, mode: str, deferred: list[str] | None = None
) -> list[Record]:
    records = catalog(settings.directory)
    selected = selection(
        records, settings.only, settings.resume_from, 0 if settings.due_only else settings.limit
    )
    if settings.due_only:
        topics = settings.topics or (DEFAULT_REFRESH_TOPICS if mode == "refresh" else [])
        due = research_plan(selected, topics, settings.stale_days)
        by_file = {record.path.name: record for record in selected}
        selected = [by_file[item["file"]] for item in due]
        if mode == "refresh":
            failures = recent_refresh_failures(settings)
            scopes = {item["file"]: set(item["topics"]) for item in due}
            eligible = []
            for record in selected:
                failure = failures.get(str(record.path.resolve()))
                if (
                    failure
                    and failure["baseline"] == digest(record.path.read_bytes())
                    and scopes[record.path.name] == failure["scope"]
                ):
                    if deferred is not None:
                        deferred.append(
                            f"[{record.path.name}] Skipped unchanged validation failure from "
                            f"{failure['run_id']}; retry after {settings.retry_failed_days} days "
                            "or use --retry-failed-days 0 / --force"
                        )
                else:
                    eligible.append(record)
            selected = eligible
        if settings.limit:
            selected = selected[: settings.limit]
    return selected


class LaunchGate:
    """Space launches and share provider cooldowns across workers."""

    def __init__(self, spacing: float, stop: threading.Event) -> None:
        self.spacing = spacing
        self.stop = stop
        self.lock = threading.Lock()
        self.next_at = 0.0

    def defer(self, seconds: float) -> None:
        with self.lock:
            self.next_at = max(self.next_at, time.monotonic() + seconds)

    def wait(self) -> None:
        while True:
            with self.lock:
                remaining = self.next_at - time.monotonic()
                if remaining <= 0:
                    self.next_at = time.monotonic() + self.spacing
                    return
            providers.wait_with_progress(min(remaining, 30), self.stop)


class Runner:
    def __init__(self, settings: Settings, mode: str, *, resume: str | None = None) -> None:
        self.store = Store(settings.state_dir)
        self.stop = threading.Event()
        if resume:
            manifest = self.store.run(resume)
            if manifest["mode"] != mode:
                raise ValueError(f"Run {resume} is {manifest['mode']}, not {mode}")
            # Resume the exact research contract, provider, model, paths, and count.
            self.settings = Settings.model_validate_json(manifest["settings"]).resolved()
            self.run_id = resume
        else:
            self.settings = settings
            self.run_id = self.store.create(mode, settings)
        self.mode = mode
        self.gate = LaunchGate(self.settings.sleep, self.stop)
        self.run_dir = self.store.directory / self.run_id
        self.run_dir.mkdir(exist_ok=True)

    def prepare(self) -> None:
        if self.store.has_jobs(self.run_id):
            return
        if self.mode == "discover":
            catalog(self.settings.directory)
        jobs: list[dict[str, Any]] = []
        if self.mode == "discover":
            for number in range(1, (self.settings.limit or 1) + 1):
                jobs.append({"key": f"discover-{number:06d}"})
        else:
            deferred: list[str] = []
            selected = research_selection(self.settings, self.mode, deferred)
            for message in deferred:
                print(message, flush=True)
            for record in selected:
                original = record.path.read_bytes()
                jobs.append(
                    {
                        "key": record.path.name,
                        "target": str(record.path),
                        "baseline": digest(original),
                        "original": original.decode("utf-8"),
                        "topics": self.due_topics(record),
                    }
                )
        self.store.initialize_jobs(self.run_id, jobs)

    def due_topics(self, record: Record) -> list[str] | None:
        if not (
            self.mode == "refresh"
            and self.settings.due_only
            and record.data.get("intelligence", {}).get("schema_version") == 2
        ):
            return None
        plan = research_plan(
            [record], self.settings.topics or DEFAULT_REFRESH_TOPICS, self.settings.stale_days
        )
        return list(plan[0]["topics"]) if plan else []

    def recover(self) -> None:
        for job in self.store.jobs(self.run_id, include_original=False):
            if job["status"] not in {"publishing", "published"}:
                # A successful provider may have finished just before a crash.
                # Reuse only durably completed calls, never interrupted output.
                if job["status"] == "skipped" or not job["artifact"]:
                    continue
                workspace = Path(job["artifact"])
                try:
                    process = json.loads((workspace / "process.json").read_text())
                    validation = workspace / "validation.json"
                    rejected = validation.exists() and not json.loads(validation.read_text())["ok"]
                except (OSError, ValueError, KeyError):
                    continue
                if (
                    process.get("returncode") != 0
                    or process.get("timed_out")
                    or process.get("failure_kind", "unknown") is not None
                    or rejected
                ):
                    continue
                job = self.store.job(self.run_id, job["key"])
                settings = self.job_settings(job)
                if settings is None:
                    continue
                report = self.accept_output(workspace, settings, job)
                if report.ok:
                    print(
                        f"[{job['key']}] Recovered completed research without a provider call",
                        flush=True,
                    )
                else:
                    self.store.update_job(
                        self.run_id, job["key"], status="failed", error="; ".join(report.errors)
                    )
                continue
            target = Path(job["target"])
            if target.is_file() and digest(target.read_bytes()) == job["digest"]:
                self.store.update_job(self.run_id, job["key"], status="published", error=None)
            elif job["status"] == "published":
                # Do not overwrite a previously published record subsequently edited by the user.
                raise PublicationError(
                    f"Published record changed or disappeared: {target}; inspect it before resuming"
                )
            else:
                self.store.update_job(
                    self.run_id,
                    job["key"],
                    status="pending",
                    error="Publication interrupted before completion",
                )

    def job_settings(self, job: dict[str, Any]) -> Settings | None:
        topics = json.loads(job["topics"]) if job.get("topics") is not None else None
        if topics is None and job["original"] is not None:
            topics = self.due_topics(Record(Path(job["target"]), tomllib.loads(job["original"])))
            if topics is not None:
                self.store.update_job(self.run_id, job["key"], topics=json.dumps(topics))
        if topics == []:
            self.store.update_job(self.run_id, job["key"], status="skipped", error=None)
            return None
        return self.settings.model_copy(update={"topics": topics}) if topics else self.settings

    def accept_output(self, workspace: Path, settings: Settings, job: dict[str, Any]) -> Report:
        key, original = job["key"], job["original"]
        target = Path(job["target"]) if original is not None else None
        started = time.perf_counter()
        content = ""
        try:
            content, report = check_candidate(workspace, settings, self.mode, original, target)
        except PublicationError:
            raise
        except (OSError, ValueError) as exc:
            report = Report(errors=[str(exc)])
        timings = {"elapsed_seconds": time.perf_counter() - started}
        report_file = workspace / "validation.json"
        report_file.write_text(json.dumps({**report.as_dict(), **timings}, indent=2) + "\n")
        self.store.update_job(self.run_id, key, report=json.dumps(report.as_dict()))
        if report.ok:
            (workspace / "validated.toml").write_text(content, encoding="utf-8")
            started = time.perf_counter()
            destination, report = publish(
                content, settings, self.store, self.run_id, key, original, job["baseline"], target
            )
            timings["publication_seconds"] = time.perf_counter() - started
            report_file.write_text(json.dumps({**report.as_dict(), **timings}, indent=2) + "\n")
            if report.ok:
                print(
                    f"[{key}] Published {destination.name}; {report.coverage.get('funding_rounds', 0)} funding rounds",
                    flush=True,
                )
        return report

    def process(self, key: str) -> bool:
        job = self.store.job(self.run_id, key)
        if job["status"] == "published":
            return True
        original = job["original"]
        current = tomllib.loads(original) if original is not None else None
        target = Path(job["target"]) if original is not None else None
        settings = self.job_settings(job)
        if settings is None:
            return True
        feedback: list[str] = []
        attempts = 0
        quota_retries = 0
        count = job["attempts"]
        last_error = job["error"]
        while attempts < settings.max_attempts and not self.stop.is_set():
            self.gate.wait()
            count += 1
            workspace = self.run_dir / key / f"attempt-{count:04d}"
            workspace.mkdir(parents=True)
            prepared_at = time.perf_counter()
            if self.mode == "discover":
                write_catalog(catalog(settings.directory), workspace / "catalog.json")
                shutil.copyfile(Path(__file__).with_name("lookup.py"), workspace / "lookup.py")
            else:
                # Enrichment has a fixed identity. The host still checks the full catalog.
                (workspace / "catalog.json").write_text("[]\n", encoding="utf-8")
            incremental = (
                self.mode == "refresh"
                and current is not None
                and current.get("intelligence", {}).get("schema_version") == 2
            )
            (workspace / "schema.json").write_text(
                _REFRESH_SCHEMA_TEXT if incremental else _SCHEMA_TEXT, encoding="utf-8"
            )
            (workspace / "schema-guide.txt").write_text(
                _REFRESH_SCHEMA_GUIDE if incremental else _SCHEMA_GUIDE, encoding="utf-8"
            )
            if original is not None:
                (workspace / "current.toml").write_text(original, encoding="utf-8")
                if current is not None:
                    brief = research_brief(
                        current,
                        (settings.topics or DEFAULT_REFRESH_TOPICS) if incremental else None,
                    )
                    (workspace / "current.json").write_text(
                        json.dumps(brief, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8",
                    )
            feedback = repair_context(self.run_dir / key, workspace) or feedback
            write_checker(workspace, settings, self.mode, original, target)
            repair = (workspace / "previous_candidate.toml").is_file() or (
                workspace / "previous_update.json"
            ).is_file()
            prompt = research_prompt(settings, self.mode, current, feedback, repair=repair)
            (workspace / "prompt.txt").write_text(prompt, encoding="utf-8")
            self.store.update_job(
                self.run_id, key, status="running", attempts=count, artifact=str(workspace)
            )
            print(
                f"[{self.run_id[:8]} / {key}] {settings.provider} research, attempt {count}",
                flush=True,
            )
            call = providers.invocation(settings, workspace, prompt)
            provider_started = time.perf_counter()
            result = providers.execute(call, workspace, settings.timeout, self.stop)
            provider_seconds = time.perf_counter() - provider_started
            (workspace / "stdout.jsonl").write_text(result.stdout, encoding="utf-8")
            (workspace / "stderr.txt").write_text(result.stderr, encoding="utf-8")
            (workspace / "process.json").write_text(
                json.dumps(
                    {
                        "provider": settings.provider,
                        "model": settings.model,
                        "returncode": result.returncode,
                        "timed_out": result.timed_out,
                        "failure_kind": providers.failure_kind(result)
                        if result.returncode or result.failure_output()
                        else None,
                        "preparation_seconds": provider_started - prepared_at,
                        "provider_seconds": provider_seconds,
                        "input_artifact_bytes": {
                            path.name: path.stat().st_size
                            for path in workspace.iterdir()
                            if path.name
                            in {
                                "prompt.txt",
                                "schema.json",
                                "schema-guide.txt",
                                "catalog.json",
                                "current.toml",
                                "current.json",
                                "check_candidate.py",
                                "previous_candidate.toml",
                                "previous_update.json",
                            }
                        },
                        "stdout_bytes": len(result.stdout.encode("utf-8")),
                        "stderr_bytes": len(result.stderr.encode("utf-8")),
                        **{
                            key: value
                            for key, value in result.event_summary.items()
                            if key != "failure_output"
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            candidate = workspace / "candidate.toml"
            patch = workspace / "update.json"
            error: str | None = None
            # A provider-level failure invalidates a partial candidate, even on exit code 0.
            if result.returncode or result.failure_output():
                kind = providers.failure_kind(result)
                error = (
                    result.failure_output() or f"Agent failed with exit code {result.returncode}"
                )
                if result.returncode == 130:
                    self.store.update_job(self.run_id, key, status="pending", error=error)
                    if self.stop.is_set():
                        return False
                    raise KeyboardInterrupt
                if kind == "fatal":
                    self.store.update_job(self.run_id, key, status="failed", error=error)
                    print(f"[{key}] Fatal provider error: {error}", flush=True)
                    self.stop.set()
                    return False
                if kind == "quota":
                    last_error = error
                    quota_retries += 1
                    self.store.update_job(self.run_id, key, status="waiting", error=error)
                    if (
                        settings.max_rate_limit_retries
                        and quota_retries > settings.max_rate_limit_retries
                    ):
                        break
                    wait = providers.quota_wait(error)
                    if wait is None:
                        wait = min(
                            settings.rate_limit_default_wait * 2 ** min(quota_retries - 1, 12), 3600
                        )
                    wait = min(wait + settings.rate_limit_fudge, settings.max_quota_wait)
                    print(
                        f"[{key}] Provider quota exhausted; next attempt in {wait:.0f}s", flush=True
                    )
                    self.gate.defer(wait)
                    continue
            elif not (candidate.is_file() and not candidate.is_symlink()) and not (
                self.mode == "refresh"
                and original is not None
                and patch.is_file()
                and not patch.is_symlink()
            ):
                error = "Agent did not write a regular candidate.toml file"
            else:
                try:
                    report = self.accept_output(workspace, settings, job)
                    if report.ok:
                        return True
                    feedback = report.errors
                    error = "; ".join(report.errors)
                except PublicationError as exc:
                    self.store.update_job(self.run_id, key, status="failed", error=str(exc))
                    print(f"[{key}] Publication conflict: {exc}", flush=True)
                    return False
                except (OSError, ValueError) as exc:
                    error = str(exc)
                    feedback = [error]
            attempts += 1
            last_error = error
            self.store.update_job(self.run_id, key, status="failed", error=error)
            print(f"[{key}] Rejected attempt: {error}", flush=True)
            if attempts < settings.max_attempts:
                providers.wait_with_progress(
                    min(settings.failure_backoff * 2 ** (attempts - 1), 900), self.stop
                )
        self.store.update_job(
            self.run_id, key, status="failed", error=last_error or "Stopped before publication"
        )
        return False

    def run(self) -> int:
        with FileLock(str(self.run_dir / "run.lock"), timeout=0):
            try:
                self.prepare()
                self.recover()
                self.store.finish(self.run_id, "running")
                print(f"Run ID: {self.run_id}\nArtifacts: {self.run_dir}", flush=True)
                if self.mode == "discover":
                    # Discovery stays sequential: each new prompt sees every published identity.
                    jobs = self.store.jobs(self.run_id, include_original=False)
                    for job in jobs:
                        if not self.process(job["key"]):
                            self.store.finish(self.run_id, "failed")
                            return 1
                    if not self.settings.limit:
                        number = len(jobs) + 1
                        while not self.stop.is_set():
                            key = f"discover-{number:06d}"
                            self.store.add_job(self.run_id, key)
                            if not self.process(key):
                                self.store.finish(self.run_id, "failed")
                                return 1
                            number += 1
                else:
                    jobs = [
                        job
                        for job in self.store.jobs(self.run_id, include_original=False)
                        if job["status"] not in {"published", "skipped"}
                    ]
                    executor = ThreadPoolExecutor(max_workers=self.settings.jobs)
                    futures = [executor.submit(self.process, job["key"]) for job in jobs]
                    try:
                        success = all([future.result() for future in as_completed(futures)])
                    except BaseException:
                        self.stop.set()
                        for future in futures:
                            future.cancel()
                        raise
                    finally:
                        executor.shutdown(wait=True, cancel_futures=True)
                    if not success:
                        self.store.finish(self.run_id, "failed")
                        return 1
                self.store.finish(self.run_id, "complete")
                return 0
            except KeyboardInterrupt:
                self.stop.set()
                self.store.finish(self.run_id, "interrupted")
                raise
            except BaseException:
                self.stop.set()
                self.store.finish(self.run_id, "failed")
                raise


def dry_run(settings: Settings, mode: str) -> list[dict[str, Any]]:
    records = catalog(settings.directory)
    selected = research_selection(settings, mode) if mode != "discover" else []
    items = [record.data for record in selected] if mode != "discover" else [None]
    output = []
    for item in items:
        prompt = research_prompt(settings, mode, item)
        call = providers.invocation(settings, Path("<staging-workspace>"), prompt)
        command = ["<prompt>" if arg == prompt else arg for arg in call.command]
        output.append(
            {
                "company": item.get("name") if item else None,
                "command": command,
                "prompt": prompt,
                "catalog_count": len(records),
                "limit": settings.limit,
            }
        )
    return output
