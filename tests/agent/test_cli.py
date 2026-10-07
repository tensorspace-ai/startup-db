import csv
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
import tomlkit

from startup_db.catalog import Record, load_toml
from startup_db.cli import configuration, main, parser
from startup_db.config import settings_from_file
from startup_db.export import exported_records, funding_csv
from startup_db.patches import apply_patch
from startup_db.storage import merge_candidate


def test_config_precedence_and_unknown_keys(tmp_path):
    directory = tmp_path / "data" / "startups"
    directory.mkdir(parents=True)
    config = tmp_path / "agent.toml"
    config.write_text(
        f'[agent]\nroot = "{tmp_path}"\nprovider = "claude"\nlimit = 7\nmin_words = 1000\n'
    )
    settings = settings_from_file(config, {"limit": 2}, {"limit": 0, "min_words": 700})
    assert settings.provider == "claude" and settings.limit == 2 and settings.min_words == 1000
    assert settings.directory == directory
    config.write_text(config.read_text() + "misspelled_setting = true\n")
    with pytest.raises(ValueError):
        settings_from_file(config, {})


def test_refresh_defaults_to_due_only_with_explicit_force(tmp_path, record, capsys, monkeypatch):
    directory = tmp_path / "data" / "startups"
    directory.mkdir(parents=True)
    for check in record["intelligence"]["research"]["topic_checks"]:
        check.update(checked_at="2099-01-01T00:00:00Z", next_review_on="2099-02-01")
    record["intelligence"]["research"]["topic_checks"][1]["status"] = "conflicting"
    (directory / "company.toml").write_text(tomlkit.dumps(record))
    args = ["refresh", "--root", str(tmp_path), "--only", "company", "--topics", "funding"]
    assert configuration(parser().parse_args(args)).due_only
    assert main([*args, "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert not (tmp_path / ".agent-runs").exists()
    assert main([*args, "--force", "--dry-run"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1
    assert not configuration(parser().parse_args(["improve", "--root", str(tmp_path)])).due_only
    monkeypatch.setattr(
        "startup_db.providers.execute", lambda *_: pytest.fail("fresh record reran")
    )
    assert main(args) == 0
    assert "Published" not in capsys.readouterr().out


def test_invalid_cli_settings_fail_without_state(tmp_path, capsys):
    (tmp_path / "data" / "startups").mkdir(parents=True)
    assert main(["discover", "--root", str(tmp_path), "--jobs", "0", "--dry-run"]) == 2
    assert "jobs" in capsys.readouterr().err
    assert not (tmp_path / ".agent-runs").exists()


def test_status_without_runs_is_read_only(tmp_path, capsys):
    (tmp_path / "data" / "startups").mkdir(parents=True)
    assert main(["status", "--root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert not (tmp_path / ".agent-runs").exists()


def test_validate_and_aliases(tmp_path, record, capsys):
    directory = tmp_path / "data" / "startups"
    directory.mkdir(parents=True)
    target = directory / "test-robotics.toml"
    target.write_text(tomlkit.dumps(record))
    assert (
        main(
            [
                "discover",
                "--validate-file",
                str(target),
                "--root",
                str(tmp_path),
                "--min-words",
                "0",
                "--min-sources",
                "2",
            ]
        )
        == 0
    )
    assert next(iter(json.loads(capsys.readouterr().out).values()))["ok"]
    assert main(["enrich", "--root", str(tmp_path), "--only", "missing", "--dry-run"]) == 2
    assert not (tmp_path / ".agent-runs").exists()


def test_funding_export_preserves_sources_units_and_disclosure(record):
    record["intelligence"]["funding"] = {
        "status": "reported",
        "summary": "Disclosed Seed round",
        "rounds": [
            {
                "id": "seed",
                "round_type": "Seed",
                "instrument": "equity",
                "status": "closed",
                "amount_status": "reported",
                "amount": 12_000_000,
                "currency": "USD",
                "source_ids": ["s2"],
                "lead_investors": ["Investor"],
            },
            {
                "id": "a",
                "round_type": "Series A",
                "instrument": "equity",
                "status": "announced",
                "amount_status": "undisclosed",
                "source_ids": ["s2"],
            },
        ],
    }
    record["intelligence"]["schema_version"] = 1
    records = [Record(Path("test-robotics.toml"), record)]
    rows = list(csv.DictReader(io.StringIO(funding_csv(records))))
    assert float(rows[0]["amount"]) == 12_000_000
    assert rows[0]["source_urls"] == "https://investor.example/test"
    assert rows[1]["amount"] == "" and rows[1]["amount_status"] == "undisclosed"
    exported = exported_records(records)[0]
    assert exported["observed_equity_totals_by_currency"] == {"USD": 12_000_000}


def test_legacy_launchers_run_without_installed_python_dependencies():
    root = Path(__file__).resolve().parents[2]
    for name, args in (
        ("find_new_startup_with_agent.py", []),
        ("improve_opportunities_with_codex.py", ["--only", "vendict"]),
        ("improve_opportunities_with_copilot.py", ["--only", "vendict", "--model", "auto"]),
    ):
        result = subprocess.run(
            [sys.executable, "-S", str(root / "scripts" / name), *args, "--dry-run"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=root,
        )
        assert result.returncode == 0, result.stderr
        prompt = json.loads(result.stdout)[0]
        assert "funding history" in prompt["prompt"]
        assert "candidate.toml" in prompt["prompt"]


@pytest.mark.parametrize("mode", ["discover", "improve", "refresh"])
def test_toml_dossier_is_consumable_by_site_parser(tmp_path, record, mode):
    root = Path(__file__).resolve().parents[2]
    if not (root / "node_modules" / "@iarna" / "toml").is_dir():
        pytest.skip("npm dependencies are installed separately")
    target = tmp_path / "record.toml"
    metadata = {
        "label": "Investor evidence",
        "url": record["public_sources"][1],
        "description": "Round announcement",
    }
    original = "# Authored dossier\n" + tomlkit.dumps(record)
    record["public_sources"][1] = metadata
    incoming = tomlkit.dumps(record)
    if mode == "refresh":
        content = apply_patch(
            json.dumps(
                {
                    "updates": {
                        "public_sources": [metadata],
                        "intelligence": {
                            "research": {
                                "topic_checks": [
                                    record["intelligence"]["research"]["topic_checks"][1]
                                ]
                            }
                        },
                    }
                }
            ),
            original,
            ["funding"],
        )
    else:
        content = merge_candidate(incoming, original if mode == "improve" else None)
    target.write_text(content)
    result = subprocess.run(
        [
            "node",
            "-e",
            "const fs=require('fs'),toml=require('@iarna/toml');const d=toml.parse(fs.readFileSync(process.argv[1],'utf8'));process.stdout.write(JSON.stringify({name:d.name,version:d.intelligence.schema_version,sources:d.public_sources}));",
            str(target),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["version"] == 2
    assert load_toml(target)["name"] == record["name"]
    sources = load_toml(target)["public_sources"]
    assert all(isinstance(source, dict) for source in sources)
    assert metadata in sources
    assert {source["url"] for source in sources} == {record["public_sources"][0], metadata["url"]}
    if mode != "discover":
        assert content.startswith("# Authored dossier\n")


def test_indexed_cli_workflow(tmp_path, record, capsys):
    directory = tmp_path / "data" / "startups"
    directory.mkdir(parents=True)
    (directory / "company.toml").write_text(tomlkit.dumps(record))
    root = ["--root", str(tmp_path)]
    assert main(["screen", "robotics", *root, "--israel", "confirmed", "--save", "robotics"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["total"] == 1
    assert main(["company", "il-test-robotics", *root]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["data"]["name"] == record["name"]
    assert main(["watch", "robotics", *root]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["changed"] == []
    for command in ("index", "deals", "investors"):
        assert main([command, *root]) == 0
        assert json.loads(capsys.readouterr().out)["index"]["changed"] == 0
