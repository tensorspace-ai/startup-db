import copy
from pathlib import Path

import pytest
from pydantic import ValidationError

from startup_db.catalog import Record, exact_identity_collisions, selection
from startup_db.models import Funding, FundingRound, Startup
from startup_db.validation import validate


def round_data(**changes):
    return {
        "id": "seed",
        "round_type": "Seed",
        "instrument": "equity",
        "status": "closed",
        "announced_on": "2025-01",
        "amount_status": "reported",
        "amount": 10_000_000,
        "currency": "USD",
        "source_ids": ["s1"],
        **changes,
    }


def test_valid_record_and_source_integrity(record):
    assert validate(record, min_sources=2, min_words=0).ok
    record["intelligence"]["company"]["source_ids"] = ["invented-source"]
    assert "unknown evidence" in " ".join(validate(record).errors)


@pytest.mark.parametrize(
    "changes",
    [
        {"amount_status": "undisclosed"},
        {"amount": 0},
        {"amount": float("nan")},
        {"amount": True},
        {"currency": None},
        {"announced_on": "2025-02-30"},
        {"post_money_valuation": 50_000_000},
    ],
)
def test_bad_funding_is_rejected(changes):
    with pytest.raises(ValidationError):
        FundingRound.model_validate(round_data(**changes))


def test_repeat_seed_rounds_and_currencies_are_not_collapsed():
    funding = Funding.model_validate(
        {
            "status": "reported",
            "summary": "Multiple events",
            "rounds": [
                round_data(),
                round_data(
                    id="extension",
                    is_extension_of="seed",
                    amount_scope="incremental_extension",
                    announced_on="2025-06",
                    amount=2_000_000,
                ),
                round_data(id="eur", announced_on="2026", currency="EUR", amount=3_000_000),
                round_data(id="debt", instrument="debt", amount=4_000_000),
                round_data(id="target", status="targeted", amount=8_000_000),
            ],
        }
    )
    assert funding.observed_equity_totals() == {"USD": 12_000_000, "EUR": 3_000_000}


def test_duplicate_announcement_rejected():
    with pytest.raises(ValidationError, match="duplicate funding announcement"):
        Funding.model_validate(
            {
                "status": "reported",
                "summary": "Duplicate",
                "rounds": [round_data(), round_data(id="copy")],
            }
        )


def test_total_requires_date_scope_and_evidence():
    with pytest.raises(ValidationError, match="reported total"):
        Funding.model_validate(
            {
                "status": "reported",
                "summary": "Total",
                "rounds": [],
                "reported_total_raised": 10_000_000,
            }
        )


def test_aliases_and_domain_reject_duplicates(record):
    existing = copy.deepcopy(record)
    existing["name"] = "Test Robotics Ltd."
    assert not validate(
        record, existing=[Record(Path("old.toml"), existing)], min_sources=2, min_words=0
    ).ok
    record["name"] = "Rebranded Company"
    assert not validate(
        record, existing=[Record(Path("old.toml"), existing)], min_sources=2, min_words=0
    ).ok
    record["website"] = "https://newbrand.example"
    record["intelligence"]["company"]["aliases"] = ["Test Robotics"]
    assert not validate(
        record, existing=[Record(Path("old.toml"), existing)], min_sources=2, min_words=0
    ).ok


def test_bad_types_and_depth_are_enforced(record):
    record["dual_use"] = "true"
    with pytest.raises(ValidationError):
        Startup.model_validate(record)
    record["dual_use"] = True
    assert any("words" in error for error in validate(record, min_sources=2, min_words=900).errors)


def test_legacy_fields_and_extensions_are_preserved(record):
    record["tags"] = ["custom"]
    assert Startup.model_validate(record).model_dump()["tags"] == ["custom"]


def test_unmatched_selection_is_an_error():
    with pytest.raises(ValueError, match="No startup matched"):
        selection([], "typo", None, 1)


def test_cumulative_extension_is_not_added_twice():
    funding = Funding.model_validate(
        {
            "status": "reported",
            "summary": "Seed expanded to 15M",
            "rounds": [
                round_data(),
                round_data(
                    id="extension",
                    announced_on="2025-06",
                    amount=15_000_000,
                    amount_scope="cumulative_round",
                    is_extension_of="seed",
                ),
            ],
        }
    )
    assert funding.observed_equity_totals() == {"USD": 10_000_000}


def test_extension_cycles_are_invalid():
    with pytest.raises(ValidationError, match="cannot form a cycle"):
        Funding.model_validate(
            {
                "status": "reported",
                "summary": "Invalid",
                "rounds": [
                    round_data(is_extension_of="extension", amount_scope="incremental_extension"),
                    round_data(
                        id="extension",
                        announced_on="2025-06",
                        amount_scope="incremental_extension",
                        is_extension_of="seed",
                    ),
                ],
            }
        )


def test_exact_audit_index_reports_alias_and_domain(record):
    old = copy.deepcopy(record)
    old["name"] = "Test Robotics Ltd."
    matches = exact_identity_collisions(
        [Record(Path("a.toml"), record), Record(Path("b.toml"), old)]
    )
    assert matches["a.toml"][0]["exact_alias"] and matches["a.toml"][0]["exact_domain"]


def test_company_source_cannot_be_mislabeled_as_independent(record):
    record["intelligence"]["research"]["sources"][1]["url"] = "https://test-robotics.example/news"
    record["public_sources"][1] = "https://test-robotics.example/news"
    assert any(
        "non-company source" in error
        for error in validate(record, min_sources=2, min_words=0).errors
    )
