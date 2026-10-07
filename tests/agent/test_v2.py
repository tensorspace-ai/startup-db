import copy
import datetime as dt
import json
from pathlib import Path

import pytest
import tomlkit
from pydantic import ValidationError

from startup_db.catalog import Record
from startup_db.models import Metric, Startup
from startup_db.patches import apply_patch, merge_updates
from startup_db.planning import research_plan
from startup_db.validation import validate


def test_v1_remains_readable(record):
    record["intelligence"]["schema_version"] = 1
    record["intelligence"].pop("israel_connection")
    record["intelligence"]["company"].pop("company_id")
    record["intelligence"]["research"].pop("topic_checks")
    record["intelligence"]["research"].pop("claim_checks")
    assert Startup.model_validate(record).intelligence.schema_version == 1
    assert not validate(record, min_sources=2, min_words=0, require_israeli=True).ok


def test_v2_requires_verified_money(record):
    record["intelligence"]["funding"] = {
        "status": "reported",
        "summary": "Seed",
        "rounds": [
            {
                "id": "seed",
                "round_type": "Seed",
                "instrument": "equity",
                "status": "closed",
                "amount_status": "reported",
                "amount": 10_000_000,
                "currency": "USD",
                "source_ids": ["s2"],
            }
        ],
    }
    with pytest.raises(ValidationError, match=r"funding\.rounds\.seed\.amount"):
        Startup.model_validate(record)
    record["intelligence"]["research"]["claim_checks"].append(
        {
            "field_path": "funding.rounds.seed.amount",
            "support": "direct",
            "summary": "Investor reported 10 million USD",
            "verified_at": "2026-10-03T12:00:00Z",
            "source_ids": ["s2"],
        }
    )
    assert Startup.model_validate(record).intelligence.funding.rounds[0].amount == 10_000_000
    record["intelligence"]["research"]["claim_checks"][-1]["support"] = "inferred"
    with pytest.raises(ValidationError, match="direct claim verification"):
        Startup.model_validate(record)


def test_numeric_financials_need_units_and_currency():
    with pytest.raises(ValidationError, match="require currency"):
        Metric.model_validate(
            {
                "name": "ARR",
                "value": "$20M",
                "as_of": "2026",
                "basis": "reported",
                "source_ids": ["s1"],
                "metric": "arr",
                "numeric_value": 20_000_000,
                "unit": "currency",
            }
        )


def test_plan_uses_topic_freshness_not_profile_timestamp(record):
    record["updated_at"] = "2026-10-03T12:00:00Z"
    record["intelligence"]["research"]["topic_checks"][1]["checked_at"] = "2025-01-01T00:00:00Z"
    rows = research_plan(
        [Record(Path("company.toml"), record)],
        ["funding"],
        30,
        dt.datetime(2026, 10, 3, 13, tzinfo=dt.UTC),
    )
    assert rows[0]["topics"] == ["funding"]
    assert any("גיוס" in query for query in rows[0]["queries"])


@pytest.mark.parametrize("status", ["reported", "not_found", "conflicting"])
def test_completed_checks_wait_until_review_due(record, status):
    check = record["intelligence"]["research"]["topic_checks"][1]
    check["status"] = status
    records = [Record(Path("company.toml"), record)]
    assert research_plan(records, ["funding"], 30, dt.datetime(2026, 10, 4, tzinfo=dt.UTC)) == []
    # Either the configured maximum age or the scheduled review can make it due.
    assert research_plan(records, ["funding"], 30, dt.datetime(2026, 11, 3, tzinfo=dt.UTC))
    check["next_review_on"] = "2026-10-05"
    assert research_plan(records, ["funding"], 30, dt.datetime(2026, 10, 5, tzinfo=dt.UTC))


def test_patch_preserves_prose_history_and_unselected_freshness(record):
    original = "# Authored profile\n" + tomlkit.dumps(record)
    check = copy.deepcopy(record["intelligence"]["research"]["topic_checks"][1])
    check["checked_at"] = "2026-10-04T12:00:00Z"
    patch = {
        "updates": {
            "funding_stage": "Seed",
            "intelligence": {
                "research": {"topic_checks": [check], "search_queries": ["new funding search"]}
            },
        }
    }
    merged = tomlkit.parse(apply_patch(json.dumps(patch), original, ["funding"])).unwrap()
    assert merged["detailed_description"] == record["detailed_description"]
    assert (
        merged["intelligence"]["research"]["topic_checks"][0]
        == record["intelligence"]["research"]["topic_checks"][0]
    )
    assert len(merged["intelligence"]["research"]["sources"]) == 2
    assert "# Authored profile" in apply_patch(json.dumps(patch), original, ["funding"])


def test_patch_cannot_rebind_source_id(record):
    original = tomlkit.dumps(record)
    patch = {
        "updates": {
            "intelligence": {
                "research": {
                    "sources": [{"id": "s1", "url": "https://unrelated.example"}],
                    "topic_checks": [record["intelligence"]["research"]["topic_checks"][1]],
                }
            }
        }
    }
    with pytest.raises(ValueError, match="different URL"):
        apply_patch(json.dumps(patch), original, ["funding"])


def test_patch_cannot_advance_unselected_topic(record):
    patch = {
        "updates": {
            "intelligence": {
                "research": {"topic_checks": record["intelligence"]["research"]["topic_checks"]}
            }
        }
    }
    with pytest.raises(ValueError, match="unselected topic"):
        apply_patch(json.dumps(patch), tomlkit.dumps(record), ["funding"])


def test_v2_deal_terms_and_roles_need_direct_evidence(record):
    record["intelligence"]["ownership"]["deals"] = [
        {
            "id": "acquisition",
            "deal_type": "acquisition",
            "company_role": "buyer",
            "status": "completed",
            "counterparty": "Target",
            "amount_status": "undisclosed",
            "source_ids": ["s1"],
        }
    ]
    with pytest.raises(ValidationError, match=r"ownership\.deals\.acquisition\.status"):
        Startup.model_validate(record)
    for field in ("status", "company_role"):
        record["intelligence"]["research"]["claim_checks"].append(
            {
                "field_path": "ownership.deals.acquisition." + field,
                "support": "direct",
                "summary": "Company announcement",
                "verified_at": "2026-10-03T12:00:00Z",
                "source_ids": ["s1"],
            }
        )
    assert Startup.model_validate(record).intelligence.ownership.deals[0].company_role == "buyer"


def test_conflicting_amounts_preserve_alternatives_without_counting_them(record):
    record["intelligence"]["funding"] = {
        "status": "conflicting",
        "summary": "Sources disagree",
        "rounds": [
            {
                "id": "seed",
                "round_type": "Seed",
                "instrument": "equity",
                "status": "announced",
                "amount_status": "conflicting",
                "source_ids": ["s1", "s2"],
                "amount_observations": [
                    {
                        "id": "company",
                        "amount": 10_000_000,
                        "currency": "USD",
                        "source_ids": ["s1"],
                    },
                    {
                        "id": "investor",
                        "amount": 12_000_000,
                        "currency": "USD",
                        "source_ids": ["s2"],
                    },
                ],
            }
        ],
    }
    for observation in record["intelligence"]["funding"]["rounds"][0]["amount_observations"]:
        record["intelligence"]["research"]["claim_checks"].append(
            {
                "field_path": f"funding.rounds.seed.amount_observations.{observation['id']}.amount",
                "support": "direct",
                "summary": "Recorded what this source reported",
                "source_ids": observation["source_ids"],
                "verified_at": "2026-10-03T12:00:00Z",
            }
        )
    funding = Startup.model_validate(record).intelligence.funding
    assert funding.observed_equity_totals() == {}
    assert funding.rounds[0].amount is None


@pytest.mark.parametrize(
    "updates",
    [
        {"intelligence": []},
        {"intelligence": {"research": []}},
        {"intelligence": {"research": {"topic_checks": ["bad"]}}},
    ],
)
def test_bad_patch_shape_is_a_validation_error(record, updates):
    with pytest.raises(ValueError):
        apply_patch(json.dumps({"updates": updates}), tomlkit.dumps(record), ["funding"])


def test_corrected_amount_cannot_reuse_old_claim_check(record):
    record["intelligence"]["funding"] = {
        "status": "reported",
        "summary": "Seed",
        "rounds": [
            {
                "id": "seed",
                "round_type": "Seed",
                "instrument": "equity",
                "status": "closed",
                "amount_status": "reported",
                "amount": 10_000_000,
                "currency": "USD",
                "source_ids": ["s1"],
            }
        ],
    }
    record["intelligence"]["research"]["claim_checks"].append(
        {
            "field_path": "funding.rounds.seed.amount",
            "support": "direct",
            "summary": "10M from company",
            "source_ids": ["s1"],
            "verified_at": "2026-10-03T12:00:00Z",
        }
    )
    update = {
        "updates": {
            "intelligence": {
                "funding": {"rounds": [{"id": "seed", "amount": 20_000_000}]},
                "research": {
                    "topic_checks": [record["intelligence"]["research"]["topic_checks"][1]]
                },
            }
        }
    }
    with pytest.raises(ValueError, match="renewed claim verification"):
        apply_patch(json.dumps(update), tomlkit.dumps(record), ["funding"])


def test_discovery_does_not_publish_israeli_funds_as_startups(record):
    record["entity_type"] = "fund"
    assert validate(record, min_sources=2, min_words=0).ok
    assert not validate(record, min_sources=2, min_words=0, require_israeli=True).ok


def test_patch_merge_keeps_inputs_independent_and_union_order(record):
    original = copy.deepcopy(record)
    update = {
        "intelligence": {"research": {"search_queries": ["new query", "new query"]}},
        "public_sources": [record["public_sources"][0], "https://new.example/source"],
    }
    before_update = copy.deepcopy(update)
    merged = merge_updates(record, update)
    assert record == original and update == before_update
    assert merged["intelligence"]["research"]["search_queries"] == [
        *record["intelligence"]["research"]["search_queries"],
        "new query",
    ]
    assert merged["public_sources"] == [*record["public_sources"], "https://new.example/source"]
    merged["intelligence"]["company"]["aliases"].append("new alias")
    merged["intelligence"]["research"]["sources"][0]["claims"].append("different claim")
    assert record == original and update == before_update
