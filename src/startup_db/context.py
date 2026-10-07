"""Bounded research context; complete originals and schemas remain available on demand."""

from __future__ import annotations

from typing import Any

from .catalog import ANALYSIS_FIELDS


def schema_guide(schema: dict[str, Any]) -> str:
    def kind(field: dict[str, Any]) -> str:
        if "$ref" in field:
            return str(field["$ref"]).rsplit("/", 1)[-1]
        if "enum" in field:
            return "|".join(str(item) for item in field["enum"])
        if "const" in field:
            return str(field["const"])
        if "anyOf" in field:
            return "|".join(kind(item) for item in field["anyOf"] if item.get("type") != "null")
        if field.get("type") == "array":
            return f"list[{kind(field['items'])}]"
        return str(field.get("type", "object"))

    lines = ["Fields marked * are required. Unknown optional values are omitted, never null."]
    for name, definition in [("Root", schema), *schema.get("$defs", {}).items()]:
        properties = definition.get("properties", {})
        if not properties:
            continue
        required = set(definition.get("required", []))
        lines.append(name + ":")
        for field, value in properties.items():
            lines.append(f"  {field}{'*' if field in required else ''}: {kind(value)}")
    lines.append("Read only relevant definitions in schema.json for constraints and descriptions.")
    return "\n".join(lines) + "\n"


def research_brief(current: dict[str, Any], topics: list[str] | None = None) -> dict[str, Any]:
    brief: dict[str, Any] = {
        key: current[key]
        for key in (
            "name",
            "website",
            "description",
            "sector",
            "headquarters",
            "funding_stage",
            "employees",
            "entity_type",
            "founded",
            "crawled_at",
            "updated_at",
            "priority_rank",
        )
        if key in current
    }
    brief["retained_analysis_fields"] = [key for key in ANALYSIS_FIELDS if key in current]
    intel = current.get("intelligence")
    if not isinstance(intel, dict):
        return brief
    if topics is None:
        brief["intelligence"] = intel
        return brief
    sections = {"schema_version", "company", "israel_connection"}
    for topic in topics:
        sections.add({"team": "people", "competition": "comparables"}.get(topic, topic))
    if "investors" in topics:
        sections.add("funding")  # Investor round IDs need their existing event identities.
    selected = {key: intel[key] for key in sections if key in intel}
    research = intel.get("research", {})
    prefixes = {"identity": ("company.", "israel_connection"), "team": ("people.",)}
    selected["research"] = {
        "sources": [
            {key: source[key] for key in ("id", "url", "title", "publisher") if key in source}
            for source in research.get("sources", [])
        ],
        "topic_checks": [
            check for check in research.get("topic_checks", []) if check["topic"] in topics
        ],
        "claim_checks": [
            check
            for check in research.get("claim_checks", [])
            if any(
                check["field_path"].startswith(prefixes.get(topic, (topic + ".",)))
                for topic in topics
            )
        ],
        "gaps": research.get("gaps", []),
        "conflicts": research.get("conflicts", []),
    }
    brief["intelligence"] = selected
    return brief
