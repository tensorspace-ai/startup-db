"""Portable research exports and a non-mutating database coverage audit."""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from .catalog import Record, exact_identity_collisions
from .models import Startup
from .validation import validate


def audit(records: list[Record], min_sources: int, min_words: int) -> dict[str, Any]:
    entries = []
    collisions = exact_identity_collisions(records)
    for record in records:
        if record.data.get("redirect_to"):
            continue
        report = validate(record.data, min_sources=min_sources, min_words=min_words)
        duplicates = collisions.get(record.path.name, [])
        entries.append(
            {
                "file": record.path.name,
                "name": record.name,
                "has_intelligence": "intelligence" in record.data,
                "duplicates": duplicates,
                **report.as_dict(),
            }
        )
    return {
        "records": len(entries),
        "valid_dossiers": sum(entry["ok"] for entry in entries),
        "needs_research": sum(not entry["ok"] for entry in entries),
        "duplicate_identities": sum(bool(entry["duplicates"]) for entry in entries),
        "entries": entries,
    }


def exported_records(records: list[Record]) -> list[dict[str, Any]]:
    exported = []
    for record in records:
        if record.data.get("redirect_to"):
            continue
        report = validate(record.data, min_sources=1, min_words=0)
        totals: dict[str, float] = {}
        if report.ok:
            totals = Startup.model_validate(
                record.data
            ).intelligence.funding.observed_equity_totals()
        exported.append(
            {
                "file": record.path.name,
                "data": record.data,
                "validation": report.as_dict(),
                "observed_equity_totals_by_currency": totals,
            }
        )
    return exported


def funding_csv(records: list[Record]) -> str:
    output = io.StringIO(newline="")
    fields = [
        "company",
        "file",
        "round_id",
        "company_id",
        "round_type",
        "instrument",
        "status",
        "announced_on",
        "closed_on",
        "amount_status",
        "amount_scope",
        "amount",
        "amount_observations",
        "currency",
        "lead_investors",
        "participating_investors",
        "pre_money_valuation",
        "post_money_valuation",
        "valuation_currency",
        "valuation_basis",
        "is_extension_of",
        "source_urls",
        "notes",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for record in records:
        if "intelligence" not in record.data:
            continue
        startup = Startup.model_validate(record.data)
        sources = {s.id: s.url for s in startup.intelligence.research.sources}
        for event in startup.intelligence.funding.rounds:
            row = {key: value for key, value in event.model_dump().items() if key in fields}
            for key in ("lead_investors", "participating_investors"):
                row[key] = "; ".join(row[key])
            row.update(
                company=record.name,
                company_id=startup.intelligence.company.company_id,
                file=record.path.name,
                round_id=event.id,
                source_urls="; ".join(sources[key] for key in event.source_ids),
                amount_observations=json.dumps(
                    [item.model_dump(exclude_none=True) for item in event.amount_observations],
                    ensure_ascii=False,
                ),
            )
            safe = {
                key: "'" + value
                if isinstance(value, str) and value.startswith(("=", "+", "-", "@"))
                else value
                for key, value in row.items()
            }
            writer.writerow(safe)
    return output.getvalue()
