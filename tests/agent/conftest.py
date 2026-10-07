from __future__ import annotations

import copy
from typing import Any

import pytest

from startup_db.models import RESEARCH_TOPICS, Startup


@pytest.fixture
def record() -> dict[str, Any]:
    data: dict[str, Any] = {}
    arrays = {
        "key_technologies",
        "use_cases",
        "competitors",
        "risk_factors",
        "signals",
        "public_sources",
    }
    scores = {
        "potential_score",
        "technology_score",
        "market_score",
        "team_score",
        "dual_use_score",
        "strategic_alignment_score",
    }
    for key in Startup.model_fields:
        if key == "intelligence":
            continue
        data[key] = (
            [] if key in arrays else 60.0 if key in scores else "Specific researched analysis"
        )
    data.update(
        name="Test Robotics",
        website="https://test-robotics.example",
        dual_use=True,
        investible=True,
        priority_rank=0,
        entity_type="startup",
        stage="early",
        risk_level="high",
        updated_at="2026-10-03T12:00:00Z",
        crawled_at="2026-10-01T12:00:00Z",
    )
    source = {
        "id": "s1",
        "url": "https://test-robotics.example/about",
        "title": "About",
        "publisher": "Test Robotics",
        "kind": "company",
        "accessed_at": "2026-10-03T12:00:00Z",
        "claims": ["Company builds robots"],
    }
    source2 = {
        **source,
        "id": "s2",
        "url": "https://investor.example/test",
        "publisher": "Investor",
        "kind": "investor",
    }
    data["public_sources"] = [source["url"], source2["url"]]
    data["intelligence"] = {
        "schema_version": 2,
        "company": {
            "company_id": "il-test-robotics",
            "aliases": [],
            "operating_status": "active",
            "business_model": "Robot sales",
            "products": ["Robot"],
            "locations": ["Haifa, Israel"],
            "source_ids": ["s1"],
        },
        "israel_connection": {
            "status": "confirmed",
            "basis": ["headquarters_in_israel"],
            "summary": "HQ in Haifa",
            "as_of": "2026-10-03",
            "source_ids": ["s1"],
        },
        "funding": {
            "status": "not_found",
            "summary": "Funding search found no disclosed rounds",
            "rounds": [],
        },
        "people": [],
        "investors": [],
        "ownership": {
            "status": "not_found",
            "summary": "No disclosed cap table",
            "cap_table_status": "not_found",
            "shareholders": [],
            "source_ids": [],
            "deals": [],
        },
        "financials": [],
        "traction": [],
        "intellectual_property": [],
        "comparables": [],
        "research": {
            "researched_at": "2026-10-03T12:00:00Z",
            "sources": [source, source2],
            "topics_checked": list(RESEARCH_TOPICS),
            "search_queries": ["Test Robotics funding"],
            "gaps": [
                "No published " + section
                for section in (
                    "team",
                    "investors",
                    "financials",
                    "traction",
                    "intellectual_property",
                    "competition",
                    "funding",
                )
            ],
            "conflicts": [],
            "diligence_questions": ["Verify sales", "Check patents", "Confirm runway"],
            "topic_checks": [
                {
                    "topic": topic,
                    "status": "reported" if topic in {"identity", "dual_use"} else "not_found",
                    "checked_at": "2026-10-03T12:00:00Z",
                    "next_review_on": "2026-11-03",
                    "queries": ["Test Robotics " + topic],
                    "source_ids": ["s1"],
                    "summary": "Test research",
                }
                for topic in RESEARCH_TOPICS
            ],
            "claim_checks": [
                {
                    "field_path": path,
                    "support": "direct",
                    "summary": "Test evidence",
                    "verified_at": "2026-10-03T12:00:00Z",
                    "source_ids": ["s1"],
                }
                for path in ("company.operating_status", "israel_connection")
            ],
        },
    }
    return copy.deepcopy(data)
