"""Comment-preserving enrichment and locked, conflict-aware atomic publication."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import tomlkit
from filelock import FileLock

from .catalog import catalog, slugify
from .config import Settings
from .state import Store
from .validation import Report, validate


class PublicationError(ValueError):
    pass


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def homogeneous_sources(sources: list[Any]) -> list[Any]:
    """Keep source metadata while avoiding mixed arrays rejected by the site parser."""
    if any(isinstance(item, dict) for item in sources) and any(
        isinstance(item, str) for item in sources
    ):
        return [{"url": item} if isinstance(item, str) else item for item in sources]
    return sources


def merge_candidate(content: str, original: str | None) -> str:
    incoming = tomlkit.parse(content)
    document = tomlkit.parse(original) if original is not None else incoming
    if original is not None:
        for key, value in incoming.items():
            if key not in document or document[key] != value:
                document[key] = value
    sources = document.get("public_sources", [])
    if isinstance(sources, list):
        normalized = homogeneous_sources(sources)
        if normalized != sources:
            document["public_sources"] = normalized
    return tomlkit.dumps(document)


def preservation_errors(data: dict[str, Any], original: str | None) -> list[str]:
    if original is None:
        return []
    previous = tomllib.loads(original)
    errors = []
    for key in ("crawled_at", "priority_rank", "signals"):
        if key in previous and data.get(key) != previous[key]:
            errors.append(f"enrichment must preserve {key}")
    old_name = previous.get("name", "")
    from .catalog import Record

    before = Record(Path("before.toml"), previous)
    after = Record(Path("after.toml"), data)
    if before.company_id and before.company_id != after.company_id:
        errors.append("enrichment must preserve the stable company_id")
    if before.aliases and not before.aliases & after.aliases:
        errors.append(
            f"company identity changed from {old_name!r}; retain its former name as a sourced alias"
        )
    return errors


def publish(
    content: str,
    settings: Settings,
    store: Store,
    run_id: str,
    key: str,
    original: str | None,
    baseline: str | None,
    target: Path | None,
) -> tuple[Path, Report]:
    data = tomllib.loads(content)
    target = target or settings.directory / (slugify(str(data.get("name", ""))) + ".toml")
    if (
        not target.stem
        or target.stem.startswith("_")
        or target.parent.resolve() != settings.directory
    ):
        raise PublicationError("Invalid publication target")
    with FileLock(str(settings.directory / "_agent-publication.lock"), timeout=30):
        if original is None and target.exists():
            raise PublicationError(f"New startup would overwrite existing record: {target.name}")
        if original is not None and (
            not target.is_file() or digest(target.read_bytes()) != baseline
        ):
            raise PublicationError(
                f"Record changed since selection: {target.name}; current edits were preserved"
            )
        report = validate(
            data,
            existing=catalog(settings.directory),
            target=target,
            min_sources=settings.min_sources,
            min_words=settings.min_words,
            duplicate_threshold=settings.duplicate_threshold,
            require_israeli=original is None,
        )
        if data.get("intelligence", {}).get("schema_version") != 2:
            report.errors.append("New publications require intelligence.schema_version=2")
        report.errors.extend(preservation_errors(data, original))
        if not report.ok:
            return target, report
        payload = content.encode("utf-8")
        checksum = digest(payload)
        # Durable intent allows recovery if the process stops after rename but before checkpoint.
        store.update_job(
            run_id,
            key,
            status="publishing",
            target=str(target),
            digest=checksum,
            report=json.dumps(report.as_dict()),
        )
        fd, temporary = tempfile.mkstemp(prefix="_agent-", suffix=".tmp", dir=settings.directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if target.exists():
                os.chmod(temporary, target.stat().st_mode & 0o777)
            else:
                os.chmod(temporary, 0o644)
            if original is None:
                # Hard-link creation cannot overwrite a concurrent uncooperative writer.
                os.link(temporary, target)
            else:
                if digest(target.read_bytes()) != baseline:
                    raise PublicationError(
                        f"Record changed during validation: {target.name}; current edits were preserved"
                    )
                os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        store.update_job(run_id, key, status="published", error=None)
        return target, report
