import copy
import sqlite3

import pytest
import tomlkit

from startup_db.index import ResearchIndex, Screen, date_interval


def write(directory, record, name="company.toml"):
    (directory / name).write_text(tomlkit.dumps(record))


def financed(record):
    value = copy.deepcopy(record)
    value["intelligence"]["schema_version"] = 1
    value["intelligence"]["funding"] = {
        "status": "reported",
        "summary": "Partial history",
        "history_status": "partial",
        "reported_total_raised": 30_000_000,
        "reported_total_currency": "USD",
        "total_source_ids": ["s1"],
        "total_as_of": "2025",
        "total_scope": "Company reported total",
        "rounds": [
            {
                "id": "seed",
                "round_type": "Seed",
                "instrument": "equity",
                "status": "closed",
                "announced_on": "2024",
                "amount_status": "reported",
                "amount": 10_000_000,
                "currency": "USD",
                "lead_investors": ["Example Capital"],
                "source_ids": ["s1"],
            },
            {
                "id": "debt",
                "round_type": "Debt",
                "instrument": "debt",
                "status": "closed",
                "announced_on": "2025-02",
                "amount_status": "reported",
                "amount": 20_000_000,
                "currency": "USD",
                "source_ids": ["s1"],
            },
            {
                "id": "ils",
                "round_type": "Grant",
                "instrument": "grant",
                "status": "closed",
                "announced_on": "2025-03-01",
                "amount_status": "reported",
                "amount": 5_000_000,
                "currency": "ILS",
                "source_ids": ["s1"],
            },
            {
                "id": "cancelled",
                "round_type": "Series A",
                "instrument": "equity",
                "status": "cancelled",
                "amount_status": "reported",
                "amount": 100_000_000,
                "currency": "USD",
                "source_ids": ["s1"],
            },
        ],
    }
    return value


def test_incremental_index_and_rollback(tmp_path, record):
    directory = tmp_path / "startups"
    directory.mkdir()
    write(directory, record)
    index = ResearchIndex(tmp_path / "index.sqlite3", directory)
    assert index.sync()["changed"] == 1
    assert index.sync()["changed"] == 0
    write(directory, {**record, "sector": "AI"})
    assert index.sync()["changed"] == 1
    assert index.screen(Screen(sector="AI"))["total"] == 1
    write(directory, {**record, "sector": "Climate"})
    (directory / "zbroken.toml").write_text("broken=[")
    with pytest.raises(ValueError):
        index.sync()
    assert index.screen(Screen(sector="AI"))["total"] == 1
    (directory / "zbroken.toml").unlink()
    index.sync()
    (directory / "company.toml").unlink()
    assert index.sync()["removed"] == 1
    assert index.screen(Screen())["total"] == 0
    assert index.screen(Screen(query="robotics"))["total"] == 0


def test_existing_index_search_row_migration_preserves_updates_and_deletions(tmp_path, record):
    directory = tmp_path / "startups"
    directory.mkdir()
    write(directory, record)
    database = tmp_path / "index.sqlite3"
    index = ResearchIndex(database, directory)
    index.sync()
    # Recreate the old on-disk schema without rebuilding its FTS contents.
    with sqlite3.connect(database) as db:
        db.execute("DROP TABLE search_rows")
        db.execute("DELETE FROM metadata WHERE key='index_schema_version'")
    migrated = ResearchIndex(database, directory)
    assert migrated.sync()["changed"] == 0
    assert migrated.screen(Screen(query="robotics"))["total"] == 1
    changed = {**record, "name": "Rebranded Company"}
    write(directory, changed)
    assert migrated.sync()["changed"] == 1
    assert migrated.screen(Screen(query="Rebranded"))["total"] == 1
    (directory / "company.toml").unlink()
    assert migrated.sync()["removed"] == 1
    assert migrated.screen(Screen(query="Rebranded"))["total"] == 0


def test_large_screen_batches_capital_and_preserves_row_order(tmp_path):
    directory = tmp_path / "startups"
    directory.mkdir()
    for number in range(450):
        (directory / f"company-{number:03d}.toml").write_text(
            f'name="Company {number:03d}"\ndescription="robots"\n'
        )
    index = ResearchIndex(tmp_path / "index.sqlite3", directory)
    index.sync()
    with index.connection() as db:
        for number in (0, 399, 400, 449):
            db.executemany(
                "INSERT INTO capital VALUES (?, ?, ?, ?, ?)",
                [
                    (f"company-{number:03d}.toml", "reported_total", "USD", 100, "2026"),
                    (f"company-{number:03d}.toml", "observed_equity", "ILS", 50, "2025"),
                ],
            )
    page = index.screen(Screen(limit=450))
    assert page["total"] == 450 and len(page["items"]) == 450
    assert [item["name"] for item in page["items"]] == [
        f"Company {number:03d}" for number in range(450)
    ]
    for number in (0, 399, 400, 449):
        assert [capital["currency"] for capital in page["items"][number]["capital"]] == [
            "ILS",
            "USD",
        ]
    assert page["items"][1]["capital"] == []
    assert index.screen(Screen(offset=450))["items"] == []


def test_legacy_not_misrepresented_and_invalid_dossiers_reported(tmp_path, record):
    legacy = copy.deepcopy(record)
    legacy.pop("intelligence")
    write(tmp_path, legacy, "legacy.toml")
    broken = copy.deepcopy(record)
    broken["intelligence"]["company"].pop("company_id")
    write(tmp_path, broken, "invalid.toml")
    index = ResearchIndex(tmp_path / "index.sqlite3", tmp_path)
    coverage = index.sync()
    assert len(coverage["invalid_dossiers"]) == 1
    assert coverage["v2_dossiers"] == 0
    assert index.screen(Screen(israel="confirmed"))["total"] == 0
    assert index.screen(Screen(israel="legacy_unverified"))["total"] == 2
    assert index.screen(Screen(min_funding=0))["total"] == 0


def test_currencies_totals_and_investor_portfolio(tmp_path, record):
    write(tmp_path, financed(record))
    index = ResearchIndex(tmp_path / "index.sqlite3", tmp_path)
    index.sync()
    assert index.screen(Screen(min_funding=25_000_000))["total"] == 1
    assert (
        index.screen(Screen(min_funding=25_000_000, funding_basis="observed_equity"))["total"] == 0
    )
    assert index.screen(Screen(currency="ILS", min_funding=1))["total"] == 0
    assert index.screen(Screen(investor="Example Capital"))["total"] == 1
    assert index.deals(investor="Example Capital")["total"] == 1
    assert index.investors()["items"][0]["lead_rounds"] == 1
    assert index.deals()["total"] == 3
    assert index.deals(include_inactive=True)["total"] == 4
    assert index.deals(min_amount=1, currency="ILS")["total"] == 1


def test_partial_dates_remain_partial(tmp_path, record):
    assert date_interval("2024-02") == ("2024-02-01", "2024-02-29")
    write(tmp_path, financed(record))
    index = ResearchIndex(tmp_path / "index.sqlite3", tmp_path)
    index.sync()
    result = index.deals(since="2024-06-01", until="2024-06-02")
    assert result["items"][0]["date"] == "2024"
    assert result["total"] == 1
    assert index.deals(since="2026")["total"] == 0
    with pytest.raises(ValueError):
        index.deals(since="2026", until="2025")


def test_hebrew_and_operator_safe_search(tmp_path, record):
    record["name"] = "מערכות רובוטיות"
    write(tmp_path, record)
    index = ResearchIndex(tmp_path / "index.sqlite3", tmp_path)
    index.sync()
    assert index.screen(Screen(query="מערכות"))["total"] == 1
    assert index.screen(Screen(query='מערכות" OR missing'))["total"] == 0
    with pytest.raises(ValueError):
        index.screen(Screen(query="***"))


def test_saved_screen_tracks_added_changed_and_removed(tmp_path, record):
    write(tmp_path, record)
    index = ResearchIndex(tmp_path / "index.sqlite3", tmp_path)
    index.sync()
    assert index.save_screen("watchlist", Screen())["initial"]
    assert not index.save_screen("watchlist")["changed"]
    write(tmp_path, {**record, "employees": "50"})
    index.sync()
    assert index.save_screen("watchlist")["changed"][0]["file"] == "company.toml"
    write(tmp_path, financed(record))
    index.sync()
    changed = index.save_screen("watchlist")["changed"][0]
    assert changed["field_changes"]["capital"]["before"] == []
    assert len(changed["field_changes"]["capital"]["after"]) == 2
    with pytest.raises(ValueError, match="different filters"):
        index.save_screen("watchlist", Screen(sector="Other"))
    (tmp_path / "company.toml").unlink()
    index.sync()
    assert len(index.save_screen("watchlist")["removed"]) == 1
    assert len(index.saved_screens()) == 1


def test_index_rejects_directory_switch(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    ResearchIndex(tmp_path / "index.sqlite3", tmp_path)
    with pytest.raises(ValueError, match="different startup directory"):
        ResearchIndex(tmp_path / "index.sqlite3", other)
