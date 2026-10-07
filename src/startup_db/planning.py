"""Freshness and completeness drive research work instead of prose length alone."""

from __future__ import annotations

import datetime as dt
from typing import Any

from .catalog import Record
from .contracts import RESEARCH_TOPICS

DEFAULT_REFRESH_TOPICS = ["funding", "investors", "team", "ownership", "financials", "traction"]
SOURCE_GUIDE = {
    "identity": [
        "Official company/about page",
        "Israeli Corporations Authority public company lookup",
        "Startup Nation Finder legacy directory (SNC operation ended August 31, 2026; check dates and corroborate current facts)",
        "SEC filings where applicable",
    ],
    "funding": [
        "Company and lead-investor announcements",
        "SEC Form D for US offerings (offering size is NOT necessarily money raised)",
        "Israel Innovation Authority grants (separate from equity)",
        "CTech/Calcalist, Globes, Geektime, NoCamels for discovery",
    ],
    "investors": [
        "Named round participants",
        "Investor portfolio pages",
        "Public fund announcements",
    ],
    "team": ["Company leadership pages", "Founder interviews", "Public professional profiles"],
    "ownership": [
        "Buyer/seller announcements",
        "Public-company filings",
        "TASE MAYA disclosures",
        "Competition Authority decisions where relevant",
    ],
    "financials": [
        "Audited or filed accounts",
        "Company-reported dated metrics",
        "Published third-party estimates clearly labeled",
    ],
    "traction": [
        "Customer announcements",
        "Government contract notices",
        "Product and regulatory releases",
    ],
    "intellectual_property": [
        "Israel Patent Office",
        "WIPO PATENTSCOPE",
        "USPTO/Google Patents linked to assignees",
    ],
    "competition": ["Official competitor product information", "Customer/substitute evidence"],
    "dual_use": ["Technical capability evidence", "Public government/customer disclosures"],
}


def research_plan(
    records: list[Record], topics: list[str], stale_days: int, now: dt.datetime | None = None
) -> list[dict[str, Any]]:
    current = now or dt.datetime.now(dt.UTC)
    requested = topics or list(RESEARCH_TOPICS)
    rows = []
    for record in records:
        if record.data.get("redirect_to"):
            continue
        intelligence = record.data.get("intelligence", {})
        checks = {
            item["topic"]: item for item in intelligence.get("research", {}).get("topic_checks", [])
        }
        due = []
        reasons = []
        age_days = 0.0
        for topic in requested:
            check = checks.get(topic)
            if check is None:
                due.append(topic)
                reasons.append(f"{topic}: never checked")
                continue
            try:
                checked = dt.datetime.fromisoformat(check["checked_at"].replace("Z", "+00:00"))
                if checked.tzinfo is None:
                    raise ValueError("missing timezone")
                age = max(0, (current - checked).total_seconds() / 86400)
                next_review = dt.date.fromisoformat(check["next_review_on"])
            except (ValueError, KeyError):
                due.append(topic)
                reasons.append(f"{topic}: invalid freshness metadata")
                continue
            age_days = max(age_days, age)
            # An unresolved conflict is still a completed check. Prioritize it
            # when due, but do not pay to research it again before its review date.
            if next_review <= current.date() or age >= stale_days:
                due.append(topic)
                reasons.append(
                    f"{topic}: {'conflicting evidence' if check['status'] == 'conflicting' else 'review due'}"
                )
        if due:
            rows.append(
                {
                    "file": record.path.name,
                    "name": record.name,
                    "company_id": record.company_id,
                    "topics": due,
                    "reasons": reasons,
                    "schema_version": intelligence.get("schema_version", 0),
                    "age_days": round(age_days, 1),
                    "priority": len(due) * 10
                    + (30 if intelligence.get("research", {}).get("conflicts") else 0)
                    + min(age_days, 365) / 10,
                    "queries": [
                        query for topic in due for query in topic_queries(record.name, topic)
                    ],
                }
            )
    return sorted(rows, key=lambda item: (-item["priority"], item["file"]))


def topic_queries(company: str, topic: str) -> list[str]:
    terms = {
        "funding": ("funding raised investors valuation", "גיוס השקעה סבב"),
        "investors": ("investors portfolio", "משקיעים"),
        "ownership": ("acquisition parent ownership", "רכישה בעלות"),
        "team": ("founders leadership", "מייסדים הנהלה"),
        "financials": ("revenue ARR financial results", "הכנסות"),
        "traction": ("customers partnerships contracts", "לקוחות שיתוף פעולה"),
    }
    english, hebrew = terms.get(topic, (topic.replace("_", " "), "ישראל"))
    return [f'"{company}" {english}', f'"{company}" {hebrew}']
