"""Incremental SQLite research index. TOML remains the authoritative source."""

from __future__ import annotations

import calendar
import contextlib
import hashlib
import json
import re
import sqlite3
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .catalog import name_key
from .contracts import EntityType, partial_date
from .prompts import utc_now

if TYPE_CHECKING:
    from .models import Startup


class Screen(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    query: str = Field(default="", max_length=500)
    sector: str = ""
    headquarters: str = ""
    entity_type: EntityType | None = None
    operating_status: (
        Literal["active", "stealth", "acquired", "public", "closed", "unknown"] | None
    ) = None
    israel: Literal["confirmed", "not_established", "not_israeli", "legacy_unverified"] | None = (
        None
    )
    min_funding: float | None = Field(default=None, ge=0)
    max_funding: float | None = Field(default=None, ge=0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    funding_basis: Literal["reported_total", "observed_equity"] = "reported_total"
    investor: str = ""
    min_schema: int = Field(default=0, ge=0, le=2)
    limit: int = Field(default=20, ge=1, le=10000)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def bounds(self) -> Screen:
        if (
            self.min_funding is not None
            and self.max_funding is not None
            and self.min_funding > self.max_funding
        ):
            raise ValueError("min_funding must not exceed max_funding")
        return self


def date_interval(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    partial_date(value)
    if len(value) == 4:
        return value + "-01-01", value + "-12-31"
    if len(value) == 7:
        year, month = map(int, value.split("-"))
        return value + "-01", f"{value}-{calendar.monthrange(year, month)[1]:02d}"
    return value, value


def full_text_query(value: str) -> str:
    # Treat user input as text, never FTS operators or SQL.
    return " AND ".join('"' + term + '"' for term in re.findall(r"\w+", value, re.UNICODE))


class ResearchIndex:
    def __init__(self, database: Path, directory: Path) -> None:
        self.database = database
        self.directory = directory.resolve()
        database.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS documents (
                    file TEXT PRIMARY KEY, signature TEXT NOT NULL, digest TEXT NOT NULL,
                    payload TEXT NOT NULL, error TEXT
                );
                CREATE TABLE IF NOT EXISTS companies (
                    file TEXT PRIMARY KEY REFERENCES documents(file) ON DELETE CASCADE,
                    company_id TEXT NOT NULL, name TEXT NOT NULL, sector TEXT NOT NULL,
                    headquarters TEXT NOT NULL, entity_type TEXT NOT NULL,
                    operating_status TEXT NOT NULL, israel TEXT NOT NULL,
                    schema_version INTEGER NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(file UNINDEXED, name, body, tokenize='unicode61');
                CREATE TABLE IF NOT EXISTS search_rows (
                    file TEXT PRIMARY KEY, search_rowid INTEGER NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS capital (
                    file TEXT REFERENCES companies(file) ON DELETE CASCADE, basis TEXT,
                    currency TEXT, amount REAL, as_of TEXT,
                    PRIMARY KEY(file, basis, currency)
                );
                CREATE TABLE IF NOT EXISTS events (
                    file TEXT REFERENCES companies(file) ON DELETE CASCADE, id TEXT,
                    kind TEXT, status TEXT, instrument TEXT, date TEXT,
                    date_start TEXT, date_end TEXT, amount REAL, currency TEXT,
                    payload TEXT NOT NULL, PRIMARY KEY(file, id)
                );
                CREATE TABLE IF NOT EXISTS participations (
                    file TEXT REFERENCES companies(file) ON DELETE CASCADE,
                    investor_key TEXT NOT NULL, name TEXT NOT NULL, round_id TEXT NOT NULL,
                    lead INTEGER NOT NULL, relationship TEXT,
                    PRIMARY KEY(file, investor_key, round_id)
                );
                CREATE INDEX IF NOT EXISTS investor_lookup ON participations(investor_key);
                CREATE INDEX IF NOT EXISTS event_dates ON events(date_start, date_end);
                CREATE INDEX IF NOT EXISTS company_identity ON companies(company_id);
                CREATE INDEX IF NOT EXISTS company_filters ON companies(israel, schema_version, operating_status);
                CREATE TABLE IF NOT EXISTS screens (
                    name TEXT PRIMARY KEY, filters TEXT NOT NULL, snapshot TEXT NOT NULL,
                    checked_at TEXT NOT NULL
                );
            """)
            db.execute("BEGIN IMMEDIATE")
            version = db.execute(
                "SELECT value FROM metadata WHERE key='index_schema_version'"
            ).fetchone()
            if version is None:
                db.execute("INSERT INTO search_rows SELECT file, rowid FROM search")
                db.execute("INSERT INTO metadata VALUES ('index_schema_version', '2')")
            previous = db.execute("SELECT value FROM metadata WHERE key='directory'").fetchone()
            if previous and previous[0] != str(self.directory):
                raise ValueError("Research index belongs to a different startup directory")
            db.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('directory', ?)", (str(self.directory),)
            )

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

    @staticmethod
    def signature(path: Path) -> str:
        stat = path.stat()
        return f"{stat.st_mtime_ns}:{stat.st_ctime_ns}:{stat.st_size}:{stat.st_ino}"

    def sync(self) -> dict[str, Any]:
        started = time.perf_counter()
        changed = removed = 0
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = {
                row["file"]: row["signature"]
                for row in db.execute("SELECT file, signature FROM documents")
            }
            present = set()
            for path in sorted(self.directory.glob("*.toml")):
                if path.name.startswith("_"):
                    continue
                present.add(path.name)
                signature = self.signature(path)
                if previous.get(path.name) == signature:
                    continue
                content = path.read_bytes()
                data = tomllib.loads(content.decode("utf-8"))
                if self.signature(path) != signature:
                    raise ValueError(f"Startup changed while indexing: {path}")
                self.remove(db, path.name)
                payload = json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))
                typed: Startup | None = None
                error = None
                if "intelligence" in data:
                    from .models import Startup

                    try:
                        typed = Startup.model_validate(data)
                    except ValueError as exc:
                        error = str(exc)
                db.execute(
                    "INSERT INTO documents VALUES (?, ?, ?, ?, ?)",
                    (path.name, signature, hashlib.sha256(content).hexdigest(), payload, error),
                )
                if not data.get("redirect_to"):
                    self.add_company(db, path.name, data, typed)
                changed += 1
            for file in set(previous) - present:
                self.remove(db, file)
                removed += 1
            coverage = self.coverage(db)
        return {
            "changed": changed,
            "removed": removed,
            "elapsed_seconds": time.perf_counter() - started,
            **coverage,
        }

    @staticmethod
    def remove(db: sqlite3.Connection, file: str) -> None:
        db.execute(
            "DELETE FROM search WHERE rowid=(SELECT search_rowid FROM search_rows WHERE file=?)",
            (file,),
        )
        db.execute("DELETE FROM search_rows WHERE file=?", (file,))
        db.execute("DELETE FROM documents WHERE file=?", (file,))

    def add_company(
        self, db: sqlite3.Connection, file: str, data: dict[str, Any], startup: Startup | None
    ) -> None:
        intel = startup.intelligence if startup else None
        company_id = intel.company.company_id if intel else None
        israel = (
            intel.israel_connection.status
            if intel and intel.israel_connection
            else "legacy_unverified"
        )
        name = str(data.get("name", Path(file).stem))
        db.execute(
            "INSERT INTO companies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                file,
                company_id or f"legacy:{file}",
                name,
                str(data.get("sector", "")),
                str(data.get("headquarters", "")),
                str(data.get("entity_type", "unverified_record")),
                intel.company.operating_status if intel else "unknown",
                israel,
                intel.schema_version if intel else 0,
                str(data.get("updated_at", "")),
            ),
        )
        text = " ".join(
            str(data.get(key, ""))
            for key in (
                "description",
                "detailed_description",
                "sector",
                "headquarters",
                "key_technologies",
                "use_cases",
            )
        )
        if intel:
            text += " " + " ".join(intel.company.aliases)
        search_row = db.execute("INSERT INTO search VALUES (?, ?, ?)", (file, name, text))
        db.execute("INSERT INTO search_rows VALUES (?, ?)", (file, search_row.lastrowid))
        if not intel:
            return
        funding = intel.funding
        if funding.reported_total_raised is not None:
            db.execute(
                "INSERT INTO capital VALUES (?, 'reported_total', ?, ?, ?)",
                (
                    file,
                    funding.reported_total_currency,
                    funding.reported_total_raised,
                    funding.total_as_of,
                ),
            )
        for currency, amount in funding.observed_equity_totals().items():
            db.execute(
                "INSERT INTO capital VALUES (?, 'observed_equity', ?, ?, ?)",
                (file, currency, amount, funding.coverage_through),
            )
        source_urls = {source.id: source.url for source in intel.research.sources}
        for event in funding.rounds:
            payload = event.model_dump(exclude_none=True)
            payload["source_urls"] = [source_urls[source] for source in event.source_ids]
            self.add_event(
                db,
                file,
                event.id,
                "funding",
                event.status,
                event.instrument,
                event.announced_on or event.closed_on,
                event.amount,
                event.currency,
                payload,
            )
            for lead, names in (
                (True, event.lead_investors),
                (False, event.participating_investors),
            ):
                for investor_name in names:
                    db.execute(
                        "INSERT INTO participations VALUES (?, ?, ?, ?, ?, 'historical') ON CONFLICT(file, investor_key, round_id) DO UPDATE SET lead=MAX(lead, excluded.lead)",
                        (file, name_key(investor_name), investor_name, event.id, lead),
                    )
        for investor in intel.investors:
            for round_id in investor.round_ids or [""]:
                db.execute(
                    "INSERT INTO participations VALUES (?, ?, ?, ?, 0, ?) ON CONFLICT(file, investor_key, round_id) DO UPDATE SET relationship=excluded.relationship",
                    (file, name_key(investor.name), investor.name, round_id, investor.relationship),
                )
        for i, deal in enumerate(intel.ownership.deals):
            payload = deal.model_dump(exclude_none=True)
            payload["source_urls"] = [source_urls[source] for source in deal.source_ids]
            self.add_event(
                db,
                file,
                "deal:" + (deal.id or str(i)),
                deal.deal_type,
                deal.status,
                None,
                deal.announced_on,
                deal.amount,
                deal.currency,
                payload,
            )

    @staticmethod
    def add_event(
        db: sqlite3.Connection,
        file: str,
        id: str,
        kind: str,
        status: str,
        instrument: str | None,
        date: str | None,
        amount: float | None,
        currency: str | None,
        payload: dict[str, Any],
    ) -> None:
        start, end = date_interval(date)
        db.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                file,
                id,
                kind,
                status,
                instrument,
                date,
                start,
                end,
                amount,
                currency,
                json.dumps(payload, ensure_ascii=False),
            ),
        )

    @staticmethod
    def coverage(db: sqlite3.Connection) -> dict[str, Any]:
        return {
            "documents": db.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            "companies": db.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
            "structured_dossiers": db.execute(
                "SELECT COUNT(*) FROM companies WHERE schema_version>0"
            ).fetchone()[0],
            "v2_dossiers": db.execute(
                "SELECT COUNT(*) FROM companies WHERE schema_version=2"
            ).fetchone()[0],
            "confirmed_israeli": db.execute(
                "SELECT COUNT(*) FROM companies WHERE israel='confirmed'"
            ).fetchone()[0],
            "funding_rounds": db.execute(
                "SELECT COUNT(*) FROM events WHERE kind='funding'"
            ).fetchone()[0],
            "invalid_dossiers": [
                dict(row)
                for row in db.execute("SELECT file, error FROM documents WHERE error IS NOT NULL")
            ],
        }

    def screen(self, filters: Screen) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        source = "companies c JOIN documents d USING(file)"
        order = "lower(c.name), c.file"
        if filters.query:
            expression = full_text_query(filters.query)
            if not expression:
                raise ValueError("Search must contain a word or number")
            source += " JOIN search ON search.file=c.file"
            order = "bm25(search, 0, 10, 1), " + order
            clauses.append("search MATCH ?")
            params.append(expression)
        for field in ("sector", "headquarters"):
            value = getattr(filters, field)
            if value:
                clauses.append(f"instr(lower(c.{field}), lower(?))>0")
                params.append(value)
        for field in ("entity_type", "operating_status", "israel"):
            value = getattr(filters, field)
            if value:
                clauses.append(f"c.{field}=?")
                params.append(value)
        clauses.append("c.schema_version>=?")
        params.append(filters.min_schema)
        if filters.investor:
            clauses.append(
                "EXISTS (SELECT 1 FROM participations p WHERE p.file=c.file AND p.investor_key=?)"
            )
            params.append(name_key(filters.investor))
        for comparator, bound in ((">=", filters.min_funding), ("<=", filters.max_funding)):
            if bound is not None:
                clauses.append(
                    f"EXISTS (SELECT 1 FROM capital x WHERE x.file=c.file AND x.basis=? AND x.currency=? AND x.amount{comparator}?)"
                )
                params.extend((filters.funding_basis, filters.currency, bound))
        where = " AND ".join(clauses)
        with self.connection() as db:
            db.execute("BEGIN")
            total = db.execute(f"SELECT COUNT(*) FROM {source} WHERE {where}", params).fetchone()[0]
            rows = db.execute(
                f"SELECT c.*, d.digest, d.error FROM {source} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                (*params, filters.limit, filters.offset),
            ).fetchall()
            # Bound SQL parameter counts and avoid one query per result row.
            capital: dict[str, list[dict[str, Any]]] = {}
            for start in range(0, len(rows), 400):
                files = [row["file"] for row in rows[start : start + 400]]
                placeholders = ",".join("?" for _ in files)
                for value in db.execute(
                    f"SELECT file, basis, currency, amount, as_of FROM capital WHERE file IN ({placeholders}) ORDER BY file, basis, currency",
                    files,
                ):
                    item = dict(value)
                    capital.setdefault(item.pop("file"), []).append(item)
            items = [{**dict(row), "capital": capital.get(row["file"], [])} for row in rows]
        return {
            "total": total,
            "items": items,
            "filters": filters.model_dump(),
            "funding_note": "Reported totals retain their source date; observed equity is a partial sum of individual equity/convertible rounds. Currencies are never combined.",
        }

    def company(self, identity: str) -> dict[str, Any]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT c.*, d.payload, d.error, d.digest FROM companies c JOIN documents d USING(file) WHERE c.company_id=? OR c.file=? OR lower(c.name)=lower(?)",
                (
                    identity,
                    identity if identity.endswith(".toml") else identity + ".toml",
                    identity,
                ),
            ).fetchall()
        if not rows:
            raise ValueError(f"Company not found: {identity}")
        if len(rows) > 1:
            raise ValueError(f"Ambiguous company identity: {identity}; use filename")
        result = dict(rows[0])
        result["data"] = json.loads(result.pop("payload"))
        result["timeline"] = self.deals(company=result["file"], include_inactive=True, limit=10000)[
            "items"
        ]
        return result

    def deals(
        self,
        *,
        company: str = "",
        investor: str = "",
        since: str | None = None,
        until: str | None = None,
        kind: str = "",
        currency: str = "USD",
        min_amount: float | None = None,
        include_inactive: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 10000:
            raise ValueError("limit must be between 1 and 10000")
        if min_amount is not None and (
            min_amount < 0 or not float("-inf") < min_amount < float("inf")
        ):
            raise ValueError("min_amount must be finite and nonnegative")
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("currency must be a three-letter uppercase code")
        start = date_interval(since)[0]
        end = date_interval(until)[1]
        if start and end and start > end:
            raise ValueError("since must not follow until")
        clauses = ["1=1"]
        params: list[Any] = []
        if not include_inactive:
            clauses.append("e.status IN ('announced', 'closed', 'completed')")
        for expression, value in (
            ("e.date_end>=?", start),
            ("e.date_start<=?", end),
            ("e.kind=?", kind),
        ):
            if value:
                clauses.append(expression)
                params.append(value)
        if company:
            clauses.append("(c.company_id=? OR c.file=? OR lower(c.name)=lower(?))")
            params.extend(
                (company, company if company.endswith(".toml") else company + ".toml", company)
            )
        if investor:
            clauses.append(
                "EXISTS (SELECT 1 FROM participations p WHERE p.file=e.file AND p.round_id=e.id AND p.investor_key=?)"
            )
            params.append(name_key(investor))
        if min_amount is not None:
            clauses.append("e.amount>=? AND e.currency=?")
            params.extend((min_amount, currency))
        query = "FROM events e JOIN companies c USING(file) WHERE " + " AND ".join(clauses)
        with self.connection() as db:
            total = db.execute("SELECT COUNT(*) " + query, params).fetchone()[0]
            rows = db.execute(
                "SELECT e.*, c.name, c.company_id, c.israel "
                + query
                + " ORDER BY e.date_start DESC, e.file, e.id LIMIT ?",
                (*params, limit),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["data"] = json.loads(item.pop("payload"))
            item.pop("date_start")
            item.pop("date_end")
            items.append(item)
        return {
            "total": total,
            "items": items,
            "date_note": "Partial dates overlap the requested interval; displayed dates keep original precision. Missing dates are excluded when a date filter is used.",
        }

    def investors(self, query: str = "", limit: int = 100) -> dict[str, Any]:
        if not 1 <= limit <= 10000:
            raise ValueError("limit must be between 1 and 10000")
        key = name_key(query)
        with self.connection() as db:
            rows = db.execute(
                "SELECT investor_key, MIN(name) AS name, COUNT(DISTINCT file) AS companies, COUNT(DISTINCT CASE WHEN round_id!='' THEN file || ':' || round_id END) AS rounds, SUM(lead) AS lead_rounds FROM participations WHERE instr(investor_key, ?)>0 GROUP BY investor_key ORDER BY companies DESC, investor_key LIMIT ?",
                (key, limit),
            ).fetchall()
            total = db.execute(
                "SELECT COUNT(DISTINCT investor_key) FROM participations WHERE instr(investor_key, ?)>0",
                (key,),
            ).fetchone()[0]
        return {
            "total": total,
            "items": [dict(row) for row in rows],
            "note": "Counts describe this catalog only. Round amounts are company financing, never the investor's check size. No guessed investor aliases are merged.",
        }

    def save_screen(self, name: str, filters: Screen | None = None) -> dict[str, Any]:
        with FileLock(str(self.database) + ".screens.lock", timeout=30):
            return self._save_screen(name, filters)

    def _save_screen(self, name: str, filters: Screen | None) -> dict[str, Any]:
        if not name.strip() or len(name) > 100:
            raise ValueError("Screen name must contain 1 to 100 characters")
        with self.connection() as db:
            previous = db.execute("SELECT * FROM screens WHERE name=?", (name,)).fetchone()
        if filters is None:
            if not previous:
                raise ValueError(f"Saved screen not found: {name}")
            filters = Screen.model_validate_json(previous["filters"])
        elif (
            previous
            and json.loads(previous["filters"])
            != filters.model_copy(update={"limit": 10000, "offset": 0}).model_dump()
        ):
            raise ValueError("Existing saved screen has different filters; use a new name")
        filters = filters.model_copy(update={"limit": 10000, "offset": 0})
        result = self.screen(filters)
        if result["total"] > 10000:
            raise ValueError(
                "Saved screens currently support up to 10000 matches; narrow the filters"
            )
        snapshot_fields = ("name", "digest", "company_id", "operating_status", "israel", "capital")
        current = {
            item["file"]: {field: item[field] for field in snapshot_fields}
            for item in result["items"]
        }
        old = json.loads(previous["snapshot"]) if previous else {}
        added = [dict(file=file, **current[file]) for file in sorted(set(current) - set(old))]
        removed = [dict(file=file, **old[file]) for file in sorted(set(old) - set(current))]
        changed = [
            dict(
                file=file,
                **current[file],
                field_changes={
                    field: {"before": old[file].get(field), "after": current[file][field]}
                    for field in snapshot_fields
                    if field != "digest" and old[file].get(field) != current[file][field]
                },
            )
            for file in sorted(set(current) & set(old))
            if current[file]["digest"] != old[file]["digest"]
        ]
        checked_at = utc_now()
        with self.connection() as db:
            db.execute(
                "INSERT INTO screens VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET snapshot=excluded.snapshot, checked_at=excluded.checked_at",
                (name, filters.model_dump_json(), json.dumps(current), checked_at),
            )
        return {
            "name": name,
            "total": result["total"],
            "initial": previous is None,
            "previous_check": previous["checked_at"] if previous else None,
            "checked_at": checked_at,
            "added": added,
            "removed": removed,
            "changed": changed,
        }

    def saved_screens(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [
                {
                    "name": row["name"],
                    "filters": json.loads(row["filters"]),
                    "checked_at": row["checked_at"],
                    "companies": len(json.loads(row["snapshot"])),
                }
                for row in db.execute("SELECT * FROM screens ORDER BY name")
            ]
