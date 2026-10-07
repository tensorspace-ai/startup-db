"""Read-only staged validation shared by the provider preflight and host."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

from .catalog import Record, catalog, write_catalog
from .config import Settings
from .patches import apply_patch
from .planning import DEFAULT_REFRESH_TOPICS
from .storage import merge_candidate, preservation_errors
from .validation import Report, validate


def check_candidate(
    workspace: Path,
    settings: Settings,
    mode: str,
    original: str | None,
    target: Path | None,
    *,
    existing: list[Record] | None = None,
) -> tuple[str, Report]:
    patch = workspace / "update.json"
    candidate = workspace / "candidate.toml"
    if mode == "refresh" and original is not None and patch.is_file() and not patch.is_symlink():
        content = apply_patch(
            patch.read_text(encoding="utf-8"), original, settings.topics or DEFAULT_REFRESH_TOPICS
        )
    elif candidate.is_file() and not candidate.is_symlink():
        content = merge_candidate(candidate.read_text(encoding="utf-8"), original)
    else:
        raise ValueError("Write a regular candidate.toml (or update.json for refresh) first")
    data = tomllib.loads(content)
    report = validate(
        data,
        existing=catalog(settings.directory) if existing is None else existing,
        target=target,
        min_sources=settings.min_sources,
        min_words=settings.min_words,
        duplicate_threshold=settings.duplicate_threshold,
        require_israeli=mode == "discover",
    )
    if data.get("intelligence", {}).get("schema_version") != 2:
        report.errors.append("New publications require intelligence.schema_version=2")
    report.errors.extend(preservation_errors(data, original))
    return content, report


def write_checker(
    workspace: Path,
    settings: Settings,
    mode: str,
    original: str | None,
    target: Path | None,
) -> None:
    # The provider's python3 may lack our dependencies. Re-exec using the host's
    # interpreter, including when installed as a wheel outside the repository.
    write_catalog(catalog(settings.directory), workspace / ".validation-catalog.json")
    (workspace / ".validation-context.json").write_text(
        json.dumps(
            {
                "settings": settings.model_dump(mode="json"),
                "mode": mode,
                "original": original,
                "target": str(target) if target else None,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    script = f"""import json
import os
import sys
from pathlib import Path

if sys.executable != {sys.executable!r}:
    os.execv({sys.executable!r}, [{sys.executable!r}, __file__])
sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
from startup_db.config import Settings
from startup_db.catalog import identity_records
from startup_db.staging import check_candidate
from startup_db.validation import Report

try:
    workspace = Path(__file__).resolve().parent
    context = json.loads((workspace / ".validation-context.json").read_text())
    settings = Settings.model_validate(context["settings"])
    _, report = check_candidate(
        workspace, settings, context["mode"], context["original"],
        Path(context["target"]) if context["target"] else None,
        existing=identity_records(workspace / ".validation-catalog.json", settings.directory),
    )
except (OSError, ValueError) as exc:
    report = Report(errors=[str(exc)])
print(json.dumps(report.as_dict(), indent=2))
raise SystemExit(0 if report.ok else 1)
"""
    (workspace / "check_candidate.py").write_text(script, encoding="utf-8")


def repair_context(job_dir: Path, workspace: Path) -> list[str]:
    """Retain a rejected, successfully produced output across retries and resume."""
    for previous in sorted(job_dir.glob("attempt-*"), reverse=True):
        if previous == workspace:
            continue
        try:
            report = json.loads((previous / "validation.json").read_text(encoding="utf-8"))
            process = json.loads((previous / "process.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if report.get("ok") or process.get("returncode") != 0 or process.get("failure_kind"):
            continue
        for name in ("update.json", "candidate.toml"):
            path = previous / name
            if path.is_file() and not path.is_symlink():
                (workspace / f"previous_{name}").write_bytes(path.read_bytes())
        return list(report.get("errors", []))
    return []
