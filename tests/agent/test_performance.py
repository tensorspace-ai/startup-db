import os
import random
from difflib import SequenceMatcher
from pathlib import Path

import pytest
import tomlkit

from startup_db.catalog import (
    CatalogCache,
    Record,
    identity_matches,
    identity_records,
    name_key,
    write_catalog,
)
from startup_db.lookup import lookup


def test_cache_reuses_unchanged_records_and_detects_rewrites(tmp_path, record):
    target = tmp_path / "company.toml"
    target.write_text(tomlkit.dumps(record))
    cache = CatalogCache()
    first = cache.read(tmp_path)[0]
    assert cache.read(tmp_path)[0] is first and cache.parses == 1
    before = target.stat()
    target.write_text(target.read_text().replace("Test Robotics", "Next Robotics"))
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert cache.read(tmp_path)[0].name == "Next Robotics" and cache.parses == 2
    target.unlink()
    assert cache.read(tmp_path) == []


def test_cache_never_masks_invalid_updated_toml(tmp_path, record):
    target = tmp_path / "company.toml"
    target.write_text(tomlkit.dumps(record))
    cache = CatalogCache()
    cache.read(tmp_path)
    target.write_text('name = "unclosed')
    with pytest.raises(ValueError):
        cache.read(tmp_path)


def test_hebrew_names_are_distinct_and_detected():
    assert name_key("חברת רובוטיקה") != name_key("חברת תוכנה")
    a = Record(Path("a.toml"), {"name": "חברת רובוטיקה"})
    b = Record(Path("b.toml"), {"name": "חברת רובוטיקה"})
    assert identity_matches(a, [b])[0]["exact_alias"]


def test_low_duplicate_threshold_is_not_skipped_by_fuzzy_bounds(record):
    import copy

    from startup_db.validation import validate

    left = copy.deepcopy(record)
    right = copy.deepcopy(record)
    for item, name, id in ((left, "AAAA", "il-left"), (right, "AABB", "il-right")):
        item["name"] = name
        item["website"] = f"https://{id}.example"
        item["intelligence"]["company"]["company_id"] = id
        item["intelligence"]["company"]["aliases"] = []
    records = [Record(Path("other.toml"), right)]
    assert not validate(
        left, existing=records, duplicate_threshold=0.5, min_sources=2, min_words=0
    ).ok
    assert validate(left, existing=records, duplicate_threshold=0.6, min_sources=2, min_words=0).ok


def test_preflight_identity_snapshot_preserves_all_identity_guards(tmp_path, record):
    import copy

    existing = copy.deepcopy(record)
    existing["intelligence"]["company"]["aliases"] = ["Former Name", "חברת רובוטיקה"]
    records = [Record(tmp_path / "existing.toml", existing)]
    snapshot = tmp_path / "catalog.json"
    write_catalog(records, snapshot)
    restored = identity_records(snapshot, tmp_path)
    for field in ("name", "website", "company_id"):
        candidate = copy.deepcopy(record)
        candidate["name"] = "Different Name"
        candidate["website"] = "https://different.example"
        candidate["intelligence"]["company"]["company_id"] = "different-id"
        if field == "company_id":
            candidate["intelligence"]["company"]["company_id"] = record["intelligence"]["company"][
                "company_id"
            ]
        else:
            candidate[field] = "Former Name" if field == "name" else record[field]
        target = Record(tmp_path / "new.toml", candidate)
        assert identity_matches(target, restored) == identity_matches(target, records)


def test_bounded_lookup_preserves_exhaustive_matches_and_order():
    rng = random.Random(42)
    records = [
        {
            "name": "".join(rng.choices("abcde", k=rng.randrange(3, 30))),
            "aliases": ["".join(rng.choices("abcde", k=8))],
            "domain": "example.com",
        }
        for _ in range(120)
    ]
    records += [
        {
            "name": "Quantum Machines",
            "aliases": ["QM", "חברת רובוטיקה"],
            "domain": "quantum-machines.co",
        }
    ]
    for query in [
        "Quantum Machines",
        "QM",
        "חברת רובוטיקה",
        "example",
        "abc",
        "abcdefghijklmnopqrstuvw",
    ]:
        expected = []
        for record in records:
            names = [record["name"], record["domain"], *record["aliases"]]
            score = max(
                SequenceMatcher(None, query.casefold(), name.casefold()).ratio() for name in names
            )
            if any(query.casefold() in name.casefold() for name in names) or score >= 0.5:
                expected.append({**record, "similarity": round(score, 4)})
        expected.sort(key=lambda item: item["similarity"], reverse=True)
        assert lookup(query, records) == expected[:10]


def test_offline_benchmark_reports_context_and_preflight_without_providers(tmp_path, record):
    from startup_db.benchmark import benchmark

    target = tmp_path / "company.toml"
    target.write_text(tomlkit.dumps(record))
    result = benchmark(tmp_path, iterations=1)
    assert result["records"] == 1 and result["provider_runs"] == 0
    assert result["schema_guide_bytes"] < result["schema_json_bytes"]
    assert result["example_context_bytes"]["file"] == "company.toml"
    assert (
        result["example_context_bytes"]["funding_only"]
        < result["example_context_bytes"]["full_original"]
    )
    for operation in ("identity_lookup", "refresh_patch_merge", "staged_preflight_wall"):
        assert result["measurements"][operation]["median_seconds"] > 0


def test_repair_prompt_is_bounded_and_duplicate_discovery_keeps_full_contract(record):
    from startup_db.config import Settings
    from startup_db.prompts import research_prompt

    settings = Settings()
    full = research_prompt(settings, "improve", record)
    repair = research_prompt(
        settings, "improve", record, ["intelligence.financials: Field required"], repair=True
    )
    assert len(repair) < len(full) / 2
    assert "Do not restart discovery or repeat completed topic searches" in repair
    assert "financials: Field required" in repair
    duplicate = research_prompt(
        settings, "discover", None, ["duplicate company identity: existing.toml"], repair=True
    )
    assert "lookup.py" in duplicate and "Explicitly search ALL" in duplicate
