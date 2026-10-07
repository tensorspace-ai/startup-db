"""Durable run manifests and per-item checkpoints backed by SQLite."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .config import Settings
from .prompts import utc_now


def refresh_history(directory: Path) -> list[dict[str, Any]]:
    """Read latest terminal refresh outcomes without creating or migrating state."""
    database = directory / "runs.sqlite3"
    if not database.is_file():
        return []
    db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            """SELECT j.target, j.baseline, j.status, j.attempts, j.error, j.report,
                      j.topics, r.id AS run_id, r.settings, r.updated_at
               FROM jobs j JOIN runs r ON r.id=j.run_id
               WHERE r.mode='refresh' AND j.status IN ('failed', 'published')
               ORDER BY r.updated_at DESC, j.rowid DESC"""
        )
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row["target"]:
                latest.setdefault(row["target"], dict(row))
        return list(latest.values())
    finally:
        db.close()


class Store:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.database = directory / "runs.sqlite3"
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, mode TEXT NOT NULL, settings TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    run_id TEXT NOT NULL, key TEXT NOT NULL, target TEXT,
                    baseline TEXT, original TEXT, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, digest TEXT, error TEXT,
                    report TEXT, artifact TEXT, topics TEXT, PRIMARY KEY (run_id, key),
                    FOREIGN KEY (run_id) REFERENCES runs(id)
                );
            """)
            db.execute("BEGIN IMMEDIATE")
            if "topics" not in {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN topics TEXT")

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, mode: str, settings: Settings) -> str:
        run_id = str(uuid.uuid4())
        now = utc_now()
        with self.connection() as db:
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, 'running')",
                (run_id, mode, settings.model_dump_json(), now, now),
            )
        return run_id

    def run(self, run_id: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown run ID: {run_id}")
        return dict(row)

    def add_job(
        self,
        run_id: str,
        key: str,
        *,
        target: Path | None = None,
        baseline: str | None = None,
        original: str | None = None,
    ) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO jobs (run_id, key, target, baseline, original) VALUES (?, ?, ?, ?, ?)",
                (run_id, key, str(target) if target else None, baseline, original),
            )

    def initialize_jobs(self, run_id: str, jobs: list[dict[str, Any]]) -> None:
        # A crash during selection must not leave a partial target list on resume.
        with self.connection() as db:
            db.executemany(
                "INSERT INTO jobs (run_id, key, target, baseline, original, topics) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        job["key"],
                        job.get("target"),
                        job.get("baseline"),
                        job.get("original"),
                        json.dumps(job["topics"]) if job.get("topics") is not None else None,
                    )
                    for job in jobs
                ],
            )

    def has_jobs(self, run_id: str) -> bool:
        with self.connection() as db:
            return (
                db.execute("SELECT 1 FROM jobs WHERE run_id=? LIMIT 1", (run_id,)).fetchone()
                is not None
            )

    def jobs(self, run_id: str, *, include_original: bool = True) -> list[dict[str, Any]]:
        columns = (
            "*"
            if include_original
            else "run_id, key, target, baseline, status, attempts, digest, error, report, artifact, topics"
        )
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    f"SELECT {columns} FROM jobs WHERE run_id=? ORDER BY rowid", (run_id,)
                )
            ]

    def job(self, run_id: str, key: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE run_id=? AND key=?", (run_id, key)
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown job: {key}")
        return dict(row)

    def update_job(self, run_id: str, key: str, **changes: Any) -> None:
        allowed = {
            "target",
            "status",
            "attempts",
            "digest",
            "error",
            "report",
            "artifact",
            "topics",
        }
        if not changes or set(changes) - allowed:
            raise ValueError("Invalid job update")
        columns = ", ".join(f"{name}=?" for name in changes)
        with self.connection() as db:
            db.execute(
                f"UPDATE jobs SET {columns} WHERE run_id=? AND key=?",
                (*changes.values(), run_id, key),
            )
            db.execute("UPDATE runs SET updated_at=? WHERE id=?", (utc_now(), run_id))

    def finish(self, run_id: str, status: str) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE runs SET status=?, updated_at=? WHERE id=?", (status, utc_now(), run_id)
            )

    def summary(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as db:
            runs = db.execute(
                "SELECT * FROM runs WHERE id=?"
                if run_id
                else "SELECT * FROM runs ORDER BY created_at DESC LIMIT 20",
                (run_id,) if run_id else (),
            ).fetchall()
            summaries: list[dict[str, Any]] = []
            for row in runs:
                run = dict(row)
                run["settings"] = json.loads(run["settings"])
                run["jobs"] = [
                    dict(job)
                    for job in db.execute(
                        "SELECT key, target, status, attempts, error, artifact, topics FROM jobs WHERE run_id=? ORDER BY rowid",
                        (run["id"],),
                    )
                ]
                run["published"] = sum(job["status"] == "published" for job in run["jobs"])
                summaries.append(run)
            return summaries
