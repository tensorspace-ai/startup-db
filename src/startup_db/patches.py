"""Incremental research updates preserve history, provenance, and authored analysis."""

from __future__ import annotations

import copy
import json
from typing import Any

import tomlkit

from .models import Startup
from .prompts import utc_now
from .storage import homogeneous_sources

KEYS = {
    "intelligence.funding.rounds": ("id",),
    "intelligence.research.sources": ("id",),
    "intelligence.research.topic_checks": ("topic",),
    "intelligence.research.claim_checks": ("field_path",),
    "intelligence.people": ("name", "role"),
    "intelligence.investors": ("name",),
    "intelligence.financials": ("name", "as_of"),
    "intelligence.ownership.deals": ("id",),
    "intelligence.ownership.holdings": ("shareholder", "as_of", "share_class"),
    "intelligence.traction": ("kind", "description", "date"),
    "intelligence.intellectual_property": ("kind", "description", "date"),
    "intelligence.comparables": ("name",),
}
UNIONS = {
    "intelligence.research.search_queries",
    "intelligence.research.topics_checked",
    "intelligence.research.gaps",
    "intelligence.research.conflicts",
    "public_sources",
}
ALLOWED_TOP_LEVEL = {
    "intelligence",
    "updated_at",
    "funding_stage",
    "employees",
    "headquarters",
    "entity_type",
    "public_sources",
    "description",
}


def refresh_schema() -> dict[str, Any]:
    """Describe partial updates; merged publications still use the complete model."""
    schema = Startup.model_json_schema()

    def partial(value: Any) -> Any:
        if isinstance(value, list):
            return [
                partial(item)
                for item in value
                if not (isinstance(item, dict) and item.get("type") == "null")
            ]
        if isinstance(value, dict):
            return {
                key: partial(item)
                for key, item in value.items()
                if key not in {"required", "default", "minItems"}
            }
        return value

    definitions = partial(schema["$defs"])
    identities = {
        "FundingRound": ["id"],
        "AmountObservation": ["id"],
        "Source": ["id"],
        "TopicCheck": ["topic"],
        "ClaimCheck": ["field_path"],
        "Person": ["name", "role"],
        "Investor": ["name"],
        "Metric": ["name", "as_of"],
        "Deal": ["id"],
        "Holding": ["shareholder", "as_of"],
        "Milestone": ["kind", "description"],
        "Comparable": ["name"],
    }
    for name, fields in identities.items():
        definitions[name]["required"] = fields
    return {
        "$defs": definitions,
        "type": "object",
        "additionalProperties": False,
        "required": ["updates"],
        "properties": {
            "updates": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    key: partial(value)
                    for key, value in schema["properties"].items()
                    if key in ALLOWED_TOP_LEVEL
                },
            }
        },
        "description": "Partial refresh. Identified arrays merge by their required keys; final merged TOML must pass the complete Startup schema and evidence checks.",
    }


def merge_updates(previous: Any, update: Any, path: str = "") -> Any:
    # Clone the original once; recursive merges only mutate this private copy.
    return _merge_updates(copy.deepcopy(previous), update, path)


def _merge_updates(previous: Any, update: Any, path: str) -> Any:
    result: Any
    if isinstance(previous, dict) and not isinstance(update, dict):
        raise ValueError(f"Patch requires an object at {path}")
    if isinstance(previous, list) and not isinstance(update, list):
        raise ValueError(f"Patch requires an array at {path}")
    if isinstance(previous, dict) and isinstance(update, dict):
        result = previous
        for key, value in update.items():
            if value is None:
                raise ValueError(
                    f"Patch cannot delete {path}.{key}; use a sourced status correction"
                )
            result[key] = _merge_updates(result.get(key), value, f"{path}.{key}".strip("."))
        return result
    if isinstance(previous, list) and isinstance(update, list):
        if path in KEYS or path.endswith(".amount_observations"):
            fields = KEYS.get(path, ("id",))

            def identity(item: Any) -> tuple[Any, ...]:
                if not isinstance(item, dict):
                    raise ValueError(f"{path} entries must be objects")
                key = tuple(item.get(field) for field in fields)
                if not any(key):
                    raise ValueError(f"{path} entries require an identity")
                return key

            result = previous
            positions = {identity(item): i for i, item in enumerate(result)}
            seen: set[tuple[Any, ...]] = set()
            for item in update:
                key = identity(item)
                if key in seen:
                    raise ValueError(f"Duplicate patch identity in {path}: {key}")
                seen.add(key)
                if key in positions:
                    index = positions[key]
                    if path == "intelligence.research.sources" and item.get(
                        "url", result[index].get("url")
                    ) != result[index].get("url"):
                        raise ValueError("A source ID cannot be reused for a different URL")
                    result[index] = _merge_updates(result[index], item, path + ".entry")
                else:
                    positions[key] = len(result)
                    result.append(copy.deepcopy(item))
            return result
        if path in UNIONS:
            result = previous
            strings = {item for item in result if isinstance(item, str)}
            for item in update:
                if isinstance(item, str):
                    if item not in strings:
                        result.append(item)
                        strings.add(item)
                elif item not in result:
                    result.append(copy.deepcopy(item))
            return result
    return copy.deepcopy(update)


def apply_patch(content: str, original: str, topics: list[str]) -> str:
    document = json.loads(content)
    if (
        not isinstance(document, dict)
        or set(document) != {"updates"}
        or not isinstance(document["updates"], dict)
    ):
        raise ValueError("update.json must contain only an updates object")
    updates = document["updates"]
    if set(updates) - ALLOWED_TOP_LEVEL:
        raise ValueError(
            f"Refresh patch cannot change fields: {sorted(set(updates) - ALLOWED_TOP_LEVEL)}"
        )
    original_document = tomlkit.parse(original)
    previous = original_document.unwrap()
    if previous.get("intelligence", {}).get("schema_version") != 2:
        raise ValueError("Incremental refresh requires a v2 dossier; enrich legacy records first")
    intelligence = updates.get("intelligence", {})
    if not isinstance(intelligence, dict) or not isinstance(intelligence.get("research", {}), dict):
        raise ValueError("Patch intelligence and research must be objects")
    checks = intelligence.get("research", {}).get("topic_checks", [])
    if not isinstance(checks, list) or not all(isinstance(item, dict) for item in checks):
        raise ValueError("Patch topic_checks must be an array of objects")
    if set(topics) - {item.get("topic") for item in checks}:
        raise ValueError("Refresh patch requires dated topic_checks for every selected topic")
    if {item.get("topic") for item in checks} - set(topics):
        raise ValueError("Refresh patch cannot advance unselected topic freshness")
    merged = merge_updates(previous, updates)
    # An old assertion cannot verify a corrected price or investor list.
    changed_checks = {
        item.get("field_path")
        for item in updates.get("intelligence", {}).get("research", {}).get("claim_checks", [])
    }
    previous_claims = previous["intelligence"]["research"].get("claim_checks", [])
    old_rounds = {item["id"]: item for item in previous["intelligence"]["funding"]["rounds"]}
    new_rounds = {item["id"]: item for item in merged["intelligence"]["funding"]["rounds"]}
    for check in previous_claims:
        path = check["field_path"]
        parts = path.split(".")
        if len(parts) == 4 and parts[:2] == ["funding", "rounds"]:
            event = new_rounds.get(parts[2], {})
            changed = event.get(parts[3]) != old_rounds.get(parts[2], {}).get(parts[3])
        elif path == "funding.reported_total_raised":
            changed = merged["intelligence"]["funding"].get("reported_total_raised") != previous[
                "intelligence"
            ]["funding"].get("reported_total_raised")
        else:
            continue
        if changed and path not in changed_checks:
            raise ValueError(f"Changed fact needs renewed claim verification: {path}")
    now = utc_now()
    merged["updated_at"] = now
    merged["intelligence"]["research"]["researched_at"] = now
    sources = merged["intelligence"]["research"]["sources"]
    urls = {item if isinstance(item, str) else item.get("url") for item in merged["public_sources"]}
    strings = all(isinstance(item, str) for item in merged["public_sources"])
    for source in sources:
        if source["url"] not in urls:
            merged["public_sources"].append(
                source["url"]
                if strings
                else {
                    "label": source["title"],
                    "url": source["url"],
                    "description": source["publisher"],
                }
            )
            urls.add(source["url"])
    merged["public_sources"] = homogeneous_sources(merged["public_sources"])
    for key, value in merged.items():
        if key not in original_document or original_document[key] != value:
            original_document[key] = value
    return tomlkit.dumps(original_document)
