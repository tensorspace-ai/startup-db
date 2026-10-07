"""Publication gates for structure, evidence coverage, identity, and research depth."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .catalog import Record, domain, identity_matches, source_urls
from .models import Startup, http_url


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    matches: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, **asdict(self)}


def validate(
    data: dict[str, Any],
    *,
    existing: list[Record] | None = None,
    target: Path | None = None,
    min_sources: int = 4,
    min_words: int = 900,
    duplicate_threshold: float = 0.9,
    legacy: bool = False,
    require_israeli: bool = False,
) -> Report:
    report = Report()
    startup: Startup | None = None
    if not legacy:
        try:
            startup = Startup.model_validate(data)
        except ValidationError as exc:
            report.errors.extend(
                f"{'.'.join(str(v) for v in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            )
            return report
    else:
        for key in ("name", "description", "website"):
            if not isinstance(data.get(key), str) or not data.get(key):
                report.errors.append(f"{key}: must be a nonempty string")
        if data.get("redirect_to"):
            report.warnings.append("Redirect record; legacy validation only")
        report.warnings.append("Legacy validation does not establish research completeness")

    urls = source_urls(data.get("public_sources", []))
    if startup:
        intelligence = startup.intelligence
        source_list = intelligence.research.sources
        research_urls = {s.url.rstrip("/") for s in source_list}
        if len(research_urls) < min_sources:
            report.errors.append(
                f"research requires {min_sources} distinct source URLs, found {len(research_urls)}"
            )
        if not research_urls <= urls:
            report.errors.append(
                "all research sources must also appear in public_sources for site compatibility"
            )
        independent = {
            s.publisher.casefold()
            for s in source_list
            if s.kind != "company" and domain(s.url) != domain(startup.website)
        }
        if not independent:
            report.errors.append("research needs at least one non-company source")
        words = startup.analysis_words()
        if words < min_words:
            report.errors.append(f"analysis requires {min_words} words, found {words}")
        funding = intelligence.funding
        if require_israeli and (
            intelligence.israel_connection is None
            or intelligence.israel_connection.status != "confirmed"
        ):
            report.errors.append(
                "Israeli discovery requires a confirmed, sourced israel_connection"
            )
        if require_israeli and (
            startup.entity_type != "startup"
            or intelligence.company.operating_status not in {"active", "stealth"}
            or intelligence.ownership.parent_company is not None
        ):
            report.errors.append(
                "Discovery requires an active or stealth startup without a known controlling parent; enrich existing public/acquired/fund records instead"
            )
        report.coverage = {
            "analysis_words": words,
            "source_count": len(research_urls),
            "independent_publishers": len(independent),
            "funding_status": funding.status,
            "funding_rounds": len(funding.rounds),
            "disclosed_rounds": sum(r.amount is not None for r in funding.rounds),
            "observed_equity_totals_by_currency": funding.observed_equity_totals(),
            "investors": len(intelligence.investors),
            "people": len(intelligence.people),
            "gaps": intelligence.research.gaps,
            "schema_version": intelligence.schema_version,
            "funding_history_status": funding.history_status,
            "israel_connection": intelligence.israel_connection.status
            if intelligence.israel_connection
            else "not_established",
            "verified_claims": len(intelligence.research.claim_checks),
        }
        if not funding.rounds:
            report.warnings.append(
                f"No individually documented funding rounds ({funding.status}); see research gaps"
            )
        if intelligence.research.conflicts:
            report.warnings.append("Research has unresolved source conflicts")
    elif len(urls) < min_sources and not data.get("redirect_to"):
        report.errors.append(
            f"public_sources requires {min_sources} distinct URLs, found {len(urls)}"
        )
    for url in urls:
        try:
            http_url(url)
        except ValueError:
            report.errors.append(f"Invalid public source URL: {url}")

    if existing is not None:
        candidate = Record(target or Path("candidate.toml"), data)
        report.matches = identity_matches(candidate, existing, min(duplicate_threshold, 0.72))
        for match in report.matches:
            if match["exact_alias"] or match["exact_domain"] or match["exact_id"]:
                report.errors.append(f"duplicate company identity: {match['file']}")
            elif match["score"] >= duplicate_threshold:
                report.errors.append(
                    f"possible company alias duplicate: {match['file']} ({match['score']}); resolve identity before publication"
                )
            else:
                report.warnings.append(f"similar company name: {match['file']} ({match['score']})")
    return report
