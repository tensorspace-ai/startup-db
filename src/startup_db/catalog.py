"""Read legacy records, rank research work, and detect company identity collisions."""

from __future__ import annotations

import json
import re
import threading
import tomllib
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

LEGAL_SUFFIX = re.compile(
    r"\b(?:inc|incorporated|ltd|limited|llc|plc|corp|corporation|co|company)\b"
)
ANALYSIS_FIELDS = (
    "description",
    "detailed_description",
    "dual_use_description",
    "investment_rationale",
    "strategic_value",
    "competitive_edge",
    "stage_rationale",
    "priority_rationale",
    "key_technologies",
    "use_cases",
    "competitors",
    "risk_factors",
)


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def folded(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", folded(value)).strip("-")


def name_key(value: str) -> str:
    # Hebrew names must not disappear from identity protection.
    normalized = unicodedata.normalize("NFKD", value).casefold()
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = LEGAL_SUFFIX.sub(" ", normalized)
    return " ".join("".join(char if char.isalnum() else " " for char in normalized).split())


def domain(value: str) -> str:
    if not value or value.casefold() == "unknown":
        return ""
    return (
        (urlparse(value if "://" in value else "https://" + value).hostname or "")
        .lower()
        .removeprefix("www.")
        .rstrip(".")
    )


def source_urls(values: list[Any]) -> set[str]:
    urls: set[str] = set()
    for value in values:
        if isinstance(value, dict):
            value = value.get("url", value.get("href", value.get("link", "")))
        if isinstance(value, str):
            urls.update(url.rstrip("/") for url in re.findall(r"https?://[^\s<>'\")]+", value))
    return urls


@dataclass(frozen=True)
class Record:
    path: Path
    data: dict[str, Any]

    @cached_property
    def name(self) -> str:
        return str(self.data.get("name", self.path.stem))

    @cached_property
    def aliases(self) -> set[str]:
        company = self.data.get("intelligence", {}).get("company", {})
        names = [self.name, company.get("legal_name", ""), *company.get("aliases", [])]
        return {name_key(value) for value in names if value}

    @cached_property
    def words(self) -> int:
        return sum(
            len(re.findall(r"\b[\w'-]+\b", str(self.data.get(field, ""))))
            for field in ANALYSIS_FIELDS
        )

    def summary(self) -> dict[str, Any]:
        return {
            "file": self.path.name,
            "name": self.name,
            "aliases": sorted(self.aliases),
            "website": self.data.get("website", ""),
            "sector": self.data.get("sector", ""),
            "description": self.data.get("description", ""),
            "redirect_to": self.data.get("redirect_to", ""),
        }

    @cached_property
    def resolved_path(self) -> Path:
        return self.path.resolve()

    @cached_property
    def website_domain(self) -> str:
        return domain(str(self.data.get("website", "")))

    @cached_property
    def company_id(self) -> str | None:
        value = self.data.get("intelligence", {}).get("company", {}).get("company_id")
        return str(value) if value else None

    def identity_summary(self) -> dict[str, Any]:
        summary = {
            "file": self.path.name,
            "name": self.name,
            "aliases": sorted(self.aliases),
            "domain": self.website_domain,
        }
        if self.company_id:
            summary["company_id"] = self.company_id
        return summary


def identity_records(path: Path, directory: Path) -> list[Record]:
    """Read a compact preflight snapshot; publication still checks the live catalog."""
    values = json.loads(path.read_text(encoding="utf-8"))
    return [
        Record(
            directory / item["file"],
            {
                "name": item["name"],
                "website": item["domain"],
                "intelligence": {
                    "company": {
                        "aliases": item["aliases"],
                        "company_id": item.get("company_id"),
                    }
                },
            },
        )
        for item in values
    ]


class CatalogCache:
    """Reparse only changed files; still stat every file before publication."""

    def __init__(self) -> None:
        self.entries: dict[Path, tuple[tuple[int, int, int, int], Record]] = {}
        self.lock = threading.RLock()
        self.parses = 0

    def read(self, directory: Path) -> list[Record]:
        with self.lock:
            paths = sorted(
                path for path in directory.glob("*.toml") if not path.name.startswith("_")
            )
            records = []
            for path in paths:
                stat = path.stat()
                signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
                previous = self.entries.get(path)
                if previous is None or previous[0] != signature:
                    record = Record(path, load_toml(path))
                    # Fail closed if a writer changed the file while we parsed it.
                    after = path.stat()
                    if signature != (
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                        after.st_size,
                        after.st_ino,
                    ):
                        raise ValueError(f"Startup changed while being read: {path}")
                    self.entries[path] = (signature, record)
                    self.parses += 1
                records.append(self.entries[path][1])
            present = set(paths)
            self.entries = {
                path: value
                for path, value in self.entries.items()
                if path.parent != directory or path in present
            }
            return records


_CATALOG_CACHE = CatalogCache()


def catalog(directory: Path) -> list[Record]:
    # Broken records must surface instead of silently disappearing from uniqueness checks.
    return _CATALOG_CACHE.read(directory.resolve())


def selection(
    records: list[Record], only: str | None, resume_from: str | None, limit: int
) -> list[Record]:
    selected = [r for r in records if not r.data.get("redirect_to")]

    def matches(record: Record, value: str) -> bool:
        return value.casefold() in {
            record.path.name.casefold(),
            record.path.stem.casefold(),
            record.name.casefold(),
            str(record.path).casefold(),
            f"data/startups/{record.path.name}".casefold(),
        }

    if only:
        requested = [value.strip() for value in only.split(",") if value.strip()]
        missing = [value for value in requested if not any(matches(r, value) for r in selected)]
        if missing:
            raise ValueError(f"No startup matched: {', '.join(missing)}")
        selected = [r for r in selected if any(matches(r, value) for value in requested)]
    selected.sort(key=lambda r: (r.words, r.path.stat().st_size, r.path.name))
    if resume_from:
        start = next((i for i, r in enumerate(selected) if matches(r, resume_from)), None)
        if start is None:
            raise ValueError(f"No startup matched --resume-from {resume_from!r}")
        selected = selected[start:]
    return selected[:limit] if limit else selected


def identity_matches(
    candidate: Record, existing: list[Record], minimum_score: float = 0.72
) -> list[dict[str, Any]]:
    """Alias/domain guards plus explainable fuzzy name similarity, not claimed embeddings."""
    matches: list[dict[str, Any]] = []
    candidate_domain = candidate.website_domain
    for record in existing:
        if record.resolved_path == candidate.resolved_path:
            continue
        existing_domain = record.website_domain
        exact_alias = bool(candidate.aliases & record.aliases)
        exact_domain = bool(candidate_domain and candidate_domain == existing_domain)
        exact_id = bool(candidate.company_id and candidate.company_id == record.company_id)
        score = 1.0 if exact_alias else 0.0
        if not exact_alias:
            for left in candidate.aliases:
                for right in record.aliases:
                    matcher = SequenceMatcher(None, left, right)
                    # Both bounds are guaranteed >= ratio; no qualifying match is dropped.
                    if (
                        matcher.real_quick_ratio() >= minimum_score
                        and matcher.quick_ratio() >= minimum_score
                    ):
                        score = max(score, matcher.ratio())
        if exact_alias or exact_domain or exact_id or score >= minimum_score:
            matches.append(
                {
                    "file": record.path.name,
                    "name": record.name,
                    "score": score,
                    "exact_alias": exact_alias,
                    "exact_domain": exact_domain,
                    "exact_id": exact_id,
                }
            )
    return sorted(
        matches,
        key=lambda item: (item["exact_alias"] or item["exact_domain"], item["score"]),
        reverse=True,
    )


def write_catalog(records: list[Record], path: Path) -> None:
    path.write_text(
        json.dumps(
            [r.identity_summary() for r in records], ensure_ascii=False, separators=(",", ":")
        )
        + "\n",
        encoding="utf-8",
    )


def exact_identity_collisions(records: list[Record]) -> dict[str, list[dict[str, Any]]]:
    """Index audit identities once rather than comparing every company pair."""
    aliases: dict[str, list[Record]] = defaultdict(list)
    domains: dict[str, list[Record]] = defaultdict(list)
    company_ids: dict[str, list[Record]] = defaultdict(list)
    collisions: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for record in records:
        for alias in record.aliases:
            aliases[alias].append(record)
        website_domain = domain(str(record.data.get("website", "")))
        if website_domain:
            domains[website_domain].append(record)
        if record.company_id:
            company_ids[record.company_id].append(record)
    for flag, index in (
        ("exact_alias", aliases),
        ("exact_domain", domains),
        ("exact_id", company_ids),
    ):
        for group in index.values():
            if len(group) < 2:
                continue
            for record in group:
                for other in group:
                    if record.path == other.path:
                        continue
                    item = collisions[record.path.name].setdefault(
                        other.path.name,
                        {
                            "file": other.path.name,
                            "name": other.name,
                            "exact_alias": False,
                            "exact_domain": False,
                            "exact_id": False,
                        },
                    )
                    item[flag] = True
    return {
        key: sorted(values.values(), key=lambda item: item["file"])
        for key, values in collisions.items()
    }
