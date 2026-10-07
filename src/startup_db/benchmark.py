"""Reproducible, offline benchmarks; provider/network timings require real run telemetry."""

from __future__ import annotations

import hashlib
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .catalog import CatalogCache, identity_matches, selection, write_catalog
from .config import Settings
from .context import research_brief, schema_guide
from .index import ResearchIndex, Screen
from .lookup import lookup
from .models import Startup
from .patches import apply_patch, refresh_schema
from .planning import DEFAULT_REFRESH_TOPICS
from .staging import write_checker


def benchmark(directory: Path, iterations: int = 3) -> dict[str, Any]:
    if not 1 <= iterations <= 20:
        raise ValueError("iterations must be between 1 and 20")
    cache = CatalogCache()
    started = time.perf_counter()
    records = cache.read(directory)
    cold = time.perf_counter() - started

    def measure(operation: Callable[[], object]) -> dict[str, Any]:
        samples = []
        for _ in range(iterations):
            started = time.perf_counter()
            operation()
            samples.append(time.perf_counter() - started)
        return {"median_seconds": statistics.median(samples), "samples_seconds": samples}

    measurements = {
        "warm_catalog_load": measure(lambda: cache.read(directory)),
        "selection_10": measure(lambda: selection(records, None, None, 10)),
    }
    if records:
        measurements["identity_match"] = measure(lambda: identity_matches(records[0], records))
        identities = [record.identity_summary() for record in records]
        measurements["identity_lookup"] = measure(lambda: lookup(records[0].name, identities))
    dossier = next(
        (
            record
            for record in records
            if record.data.get("intelligence", {}).get("schema_version") == 2
        ),
        None,
    )
    context_bytes: dict[str, Any] | None = None
    if dossier:
        original = dossier.path.read_text(encoding="utf-8")
        checks = dossier.data["intelligence"]["research"]["topic_checks"]
        patch = json.dumps(
            {
                "updates": {
                    "intelligence": {
                        "research": {
                            "topic_checks": [
                                check
                                for check in checks
                                if check["topic"] in DEFAULT_REFRESH_TOPICS
                            ]
                        }
                    }
                }
            }
        )
        measurements["refresh_patch_merge"] = measure(
            lambda: apply_patch(patch, original, DEFAULT_REFRESH_TOPICS)
        )

        def json_bytes(value: Any) -> int:
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())

        context_bytes = {
            "file": dossier.path.name,
            "full_original": len(original.encode()),
            "full_intelligence": json_bytes(dossier.data["intelligence"]),
            "default_refresh": json_bytes(research_brief(dossier.data, DEFAULT_REFRESH_TOPICS)),
            "funding_only": json_bytes(research_brief(dossier.data, ["funding"])),
        }
    with tempfile.TemporaryDirectory() as temporary:
        target = Path(temporary) / "catalog.json"
        measurements["catalog_write"] = measure(lambda: write_catalog(records, target))
        catalog_bytes = target.stat().st_size
        index = ResearchIndex(Path(temporary) / "research.sqlite3", directory)
        index_cold = index.sync()
        measurements["warm_index_sync"] = measure(index.sync)
        measurements["company_screen"] = measure(lambda: index.screen(Screen(query="quantum")))
        measurements["deal_screen"] = measure(index.deals)
        measurements["investor_screen"] = measure(index.investors)

        def screen_cli() -> None:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "startup_db",
                    "screen",
                    "quantum",
                    "--dir",
                    str(directory.resolve()),
                    "--state-dir",
                    temporary,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if result.returncode:
                raise ValueError(f"Benchmark CLI failed: {result.stderr.strip()}")

        measurements["python_cli_screen_wall"] = measure(screen_cli)
        index_bytes = index.database.stat().st_size
        if dossier:
            workspace = Path(temporary) / "preflight"
            workspace.mkdir()
            settings = Settings(
                directory=directory.resolve(), state_dir=Path(temporary), min_words=0, min_sources=1
            ).resolved()
            write_checker(workspace, settings, "refresh", original, dossier.path)
            (workspace / "candidate.toml").write_text(original, encoding="utf-8")

            def preflight() -> None:
                result = subprocess.run(
                    [sys.executable, str(workspace / "check_candidate.py")],
                    capture_output=True,
                    text=True,
                )
                if result.returncode:
                    raise ValueError(f"Benchmark preflight failed: {result.stdout} {result.stderr}")

            measurements["staged_preflight_wall"] = measure(preflight)
    checksum = hashlib.sha256()
    toml_bytes = 0
    for record in records:
        content = record.path.read_bytes()
        checksum.update(record.path.name.encode() + b"\x00" + content)
        toml_bytes += len(content)
    return {
        "method": "offline; cold parse reported separately from warm stat-validated cache",
        "python": platform.python_version(),
        "platform": platform.system(),
        "machine": platform.machine(),
        "iterations": iterations,
        "records": len(records),
        "corpus_sha256": checksum.hexdigest(),
        "toml_bytes": toml_bytes,
        "cold_catalog_seconds": cold,
        "cold_index": index_cold,
        "index_database_bytes": index_bytes,
        "catalog_json_bytes": catalog_bytes,
        "enrichment_catalog_bytes": 3,
        "schema_json_bytes": len(
            json.dumps(Startup.model_json_schema(), separators=(",", ":")).encode()
        ),
        "refresh_schema_json_bytes": len(
            json.dumps(refresh_schema(), separators=(",", ":")).encode()
        ),
        "schema_guide_bytes": len(schema_guide(Startup.model_json_schema()).encode()),
        "refresh_schema_guide_bytes": len(schema_guide(refresh_schema()).encode()),
        "example_context_bytes": context_bytes,
        "measurements": measurements,
        "provider_runs": 0,
    }
