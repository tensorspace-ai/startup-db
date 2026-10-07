from pathlib import Path

from startup_db.catalog import load_toml
from startup_db.validation import validate


def test_reviewed_example_meets_publication_contract():
    path = Path(__file__).resolve().parents[2] / "data/startups/quantum-machines.toml"
    report = validate(load_toml(path))
    assert report.ok, report.errors
    assert report.coverage["schema_version"] == 2
    assert report.coverage["israel_connection"] == "confirmed"
