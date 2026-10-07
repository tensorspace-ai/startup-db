"""Typed, source-linked private-market intelligence alongside the site's legacy fields."""

from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contracts import RESEARCH_TOPICS as RESEARCH_TOPICS
from .contracts import EntityType as EntityType
from .contracts import partial_date as partial_date

Score = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
Money = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Status = Literal["reported", "undisclosed", "not_found", "not_applicable", "conflicting"]


def http_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValueError("must be an absolute HTTP(S) URL without credentials")
    return value


def optional_http_url(value: str | None) -> str | None:
    return http_url(value) if value is not None else None


def optional_date(value: str | None) -> str | None:
    return partial_date(value) if value is not None else None


def currency_code(value: str | None) -> str | None:
    if value is not None and not re.fullmatch(r"[A-Z]{3}", value):
        raise ValueError("must be a three-letter uppercase currency code")
    return value


def timestamp(value: str) -> str:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value


class Structured(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Evidence(Structured):
    source_ids: list[str] = Field(min_length=1)
    notes: str = ""


class Source(Structured):
    id: str = Field(min_length=1)
    url: str
    title: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    kind: Literal[
        "company", "investor", "filing", "government", "customer", "media", "directory", "other"
    ]
    accessed_at: str
    published_on: str | None = None
    claims: list[str] = Field(min_length=1)

    _url = field_validator("url")(http_url)

    _publication_date = field_validator("published_on")(optional_date)
    _access_date = field_validator("accessed_at")(timestamp)


class AmountObservation(Evidence):
    id: str = Field(min_length=1)
    amount: Money
    currency: str
    as_of: str | None = None
    _currency = field_validator("currency")(currency_code)
    _date = field_validator("as_of")(optional_date)


class FundingRound(Evidence):
    id: str = Field(min_length=1)
    round_type: str = Field(
        min_length=1,
        description="Pre-Seed, Seed, Series A, extension, debt, grant, secondary, IPO, etc.",
    )
    instrument: Literal["equity", "convertible", "debt", "grant", "secondary", "ipo", "unknown"]
    status: Literal["announced", "closed", "targeted", "cancelled"]
    announced_on: str | None = None
    closed_on: str | None = None
    amount_status: Status
    amount: Money | None = Field(default=None, description="Absolute currency units, NOT millions")
    amount_observations: list[AmountObservation] = []
    amount_scope: Literal["round", "incremental_extension", "cumulative_round", "unknown"] = "round"
    currency: str | None = None
    lead_investors: list[str] = []
    participating_investors: list[str] = []
    pre_money_valuation: Money | None = None
    post_money_valuation: Money | None = None
    valuation_currency: str | None = None
    valuation_basis: Literal["reported", "estimated", "undisclosed"] = "undisclosed"
    is_extension_of: str | None = None

    _dates = field_validator("announced_on", "closed_on")(optional_date)
    _currencies = field_validator("currency", "valuation_currency")(currency_code)

    @model_validator(mode="after")
    def disclosures(self) -> FundingRound:
        ids = [item.id for item in self.amount_observations]
        if len(ids) != len(set(ids)):
            raise ValueError("amount observation IDs must be unique")
        if (self.amount is not None) != (self.amount_status == "reported"):
            raise ValueError("amount must be present only when amount_status is reported")
        if self.amount is not None and not self.currency:
            raise ValueError("a disclosed amount requires currency")
        if self.is_extension_of and self.amount is not None and self.amount_scope == "round":
            raise ValueError(
                "extension amounts must identify incremental_extension, cumulative_round, or unknown scope"
            )
        valuations = self.pre_money_valuation is not None or self.post_money_valuation is not None
        if valuations and (not self.valuation_currency or self.valuation_basis == "undisclosed"):
            raise ValueError("valuation requires its own currency and reported/estimated basis")
        if not valuations and self.valuation_basis != "undisclosed":
            raise ValueError("valuation basis requires a valuation value")
        if (
            self.post_money_valuation
            and self.pre_money_valuation
            and self.post_money_valuation < self.pre_money_valuation
        ):
            raise ValueError("post-money valuation cannot be lower than pre-money valuation")
        return self


class Funding(Structured):
    status: Status
    summary: str = Field(min_length=1)
    rounds: list[FundingRound]
    reported_total_raised: Money | None = None
    reported_total_currency: str | None = None
    total_source_ids: list[str] = []
    total_as_of: str | None = None
    total_scope: str = ""
    history_status: Literal["partial", "complete_public_record", "not_found"] = "partial"
    coverage_through: str | None = None
    _coverage = field_validator("coverage_through")(optional_date)

    @model_validator(mode="after")
    def totals(self) -> Funding:
        if self.reported_total_raised is not None:
            if (
                not self.reported_total_currency
                or not self.total_source_ids
                or not self.total_as_of
                or not self.total_scope
            ):
                raise ValueError("reported total needs currency, sources, as-of date, and scope")
            currency_code(self.reported_total_currency)
            partial_date(self.total_as_of)
        elif self.reported_total_currency or self.total_source_ids or self.total_as_of:
            raise ValueError("total metadata requires reported_total_raised")
        if self.rounds and self.status not in {"reported", "conflicting"}:
            raise ValueError("funding with rounds must have reported or conflicting status")
        if not self.rounds and self.status == "reported" and self.reported_total_raised is None:
            raise ValueError("reported funding needs rounds or a sourced reported total")
        ids = [r.id for r in self.rounds]
        if len(ids) != len(set(ids)):
            raise ValueError("funding round IDs must be unique")
        seen: set[tuple[str, str, str | None, float | None, str | None]] = set()
        by_id = {event.id: event for event in self.rounds}
        for item in self.rounds:
            fingerprint = (
                item.round_type.casefold(),
                item.instrument,
                item.announced_on or item.closed_on,
                item.amount,
                item.currency,
            )
            if fingerprint in seen:
                raise ValueError(
                    "duplicate funding announcement; merge sources instead of counting twice"
                )
            seen.add(fingerprint)
            if item.is_extension_of and (
                item.is_extension_of not in ids or item.is_extension_of == item.id
            ):
                raise ValueError("extension must reference another round ID")
            visited = {item.id}
            extension = item.is_extension_of
            while extension:
                if extension in visited:
                    raise ValueError("funding extension references cannot form a cycle")
                visited.add(extension)
                if extension not in by_id:
                    raise ValueError("extension references an unknown round ID")
                extension = by_id[extension].is_extension_of
        return self

    def observed_equity_totals(self) -> dict[str, float]:
        """Known individual rounds only, never a claim of complete lifetime funding."""
        totals: dict[str, float] = {}
        for item in self.rounds:
            if (
                item.status in {"announced", "closed"}
                and item.instrument in {"equity", "convertible"}
                and item.amount is not None
                and item.currency
                and item.amount_scope in {"round", "incremental_extension"}
            ):
                totals[item.currency] = totals.get(item.currency, 0) + item.amount
        return totals


class Person(Evidence):
    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    is_founder: bool
    current: bool
    background: str = ""
    linkedin_url: str | None = None
    _linkedin = field_validator("linkedin_url")(optional_http_url)


class Investor(Evidence):
    name: str = Field(min_length=1)
    investor_type: Literal[
        "vc", "corporate", "angel", "pe", "government", "accelerator", "other", "unknown"
    ]
    website: str | None = None
    round_ids: list[str]
    relationship: Literal["current", "historical", "unknown"]
    _website = field_validator("website")(optional_http_url)


class RegistryIdentifier(Evidence):
    jurisdiction: str
    scheme: str = Field(description="Israel company number, SEC CIK, LEI, etc.")
    identifier: str = Field(min_length=1)


class IsraelConnection(Evidence):
    status: Literal["confirmed", "not_established", "not_israeli"]
    basis: list[
        Literal[
            "incorporated_in_israel",
            "headquarters_in_israel",
            "israeli_founders",
            "israeli_rd",
            "other",
        ]
    ]
    summary: str = Field(min_length=1)
    as_of: str
    _date = field_validator("as_of")(partial_date)

    @model_validator(mode="after")
    def connection(self) -> IsraelConnection:
        if self.status == "confirmed" and not self.basis:
            raise ValueError("a confirmed Israeli connection requires its basis")
        if self.status != "confirmed" and self.basis:
            raise ValueError(
                "unconfirmed Israeli connection cannot claim confirmed connection types"
            )
        return self


class Company(Evidence):
    company_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    legal_name: str | None = None
    aliases: list[str]
    operating_status: Literal["active", "stealth", "acquired", "public", "closed", "unknown"]
    business_model: str
    products: list[str]
    locations: list[str]
    linkedin_url: str | None = None
    _linkedin = field_validator("linkedin_url")(optional_http_url)
    country_of_incorporation: str | None = None
    founded_on: str | None = None
    registry_identifiers: list[RegistryIdentifier] = []
    _founded = field_validator("founded_on")(optional_date)


class Deal(Evidence):
    id: str | None = None
    deal_type: Literal["acquisition", "ipo", "merger", "buyout", "secondary", "other"]
    company_role: Literal["buyer", "seller", "target", "issuer", "unknown"] = "unknown"
    announced_on: str | None = None
    completed_on: str | None = None
    status: Literal["announced", "completed", "cancelled", "unknown"]
    counterparty: str
    amount: Money | None = None
    amount_status: Status | None = None
    currency: str | None = None

    _date = field_validator("announced_on", "completed_on")(optional_date)
    _currency = field_validator("currency")(currency_code)

    @model_validator(mode="after")
    def price(self) -> Deal:
        if self.amount is not None and not self.currency:
            raise ValueError("deal amount needs currency")
        if self.amount_status is not None and (self.amount is not None) != (
            self.amount_status == "reported"
        ):
            raise ValueError("deal amount must be present only when amount_status is reported")
        return self


class Holding(Evidence):
    shareholder: str
    ownership_percent: Annotated[float, Field(ge=0, le=100)]
    as_of: str
    fully_diluted: bool | None = None
    share_class: str | None = None
    _date = field_validator("as_of")(partial_date)


class Ownership(Structured):
    status: Status
    summary: str
    parent_company: str | None = None
    stock_ticker: str | None = None
    cap_table_status: Status
    shareholders: list[str]
    source_ids: list[str]
    deals: list[Deal]
    holdings: list[Holding] = []


class Metric(Evidence):
    name: str
    value: str = Field(
        min_length=1, description="Published value with units; no invented estimates"
    )
    as_of: str
    basis: Literal["reported", "estimated"]
    _date = field_validator("as_of")(partial_date)
    metric: (
        Literal[
            "revenue",
            "arr",
            "ebitda",
            "net_income",
            "cash",
            "burn",
            "runway_months",
            "employees",
            "other",
        ]
        | None
    ) = None
    numeric_value: float | None = None
    value_relation: Literal[
        "exact", "at_least", "more_than", "at_most", "less_than", "approximate"
    ] = "exact"
    unit: Literal["currency", "people", "months", "percent", "count", "other"] | None = None
    currency: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    _currency = field_validator("currency")(currency_code)
    _period = field_validator("period_start", "period_end")(optional_date)

    @model_validator(mode="after")
    def numeric_metric(self) -> Metric:
        if self.numeric_value is not None and (not self.metric or not self.unit):
            raise ValueError("numeric metrics require metric kind and unit")
        if self.unit == "currency" and self.numeric_value is not None and not self.currency:
            raise ValueError("numeric currency metrics require currency")
        if self.currency and self.unit != "currency":
            raise ValueError("currency requires a currency unit")
        if self.period_start and self.period_end and self.period_start > self.period_end:
            raise ValueError("metric period_start must not be after period_end")
        return self


class Milestone(Evidence):
    kind: Literal[
        "customer", "partnership", "contract", "product", "hiring", "regulatory", "patent", "other"
    ]
    description: str
    date: str | None = None
    _date = field_validator("date")(optional_date)


class Comparable(Evidence):
    name: str
    website: str | None = None
    rationale: str
    differentiation: str
    _website = field_validator("website")(optional_http_url)


class TopicCheck(Structured):
    topic: str
    status: Literal[
        "reported", "no_public_disclosure", "not_found", "not_applicable", "conflicting"
    ]
    checked_at: str
    next_review_on: str
    queries: list[str] = Field(min_length=1)
    source_ids: list[str]
    summary: str = Field(min_length=1)
    _checked = field_validator("checked_at")(timestamp)
    _review = field_validator("next_review_on")(partial_date)

    @model_validator(mode="after")
    def topic_metadata(self) -> TopicCheck:
        if self.topic not in RESEARCH_TOPICS:
            raise ValueError("unknown research topic")
        if len(self.next_review_on) != 10:
            raise ValueError("next_review_on must be YYYY-MM-DD")
        if self.status in {"reported", "conflicting"} and not self.source_ids:
            raise ValueError("reported or conflicting topic checks need sources")
        return self


class ClaimCheck(Evidence):
    field_path: str = Field(
        min_length=1,
        description="Relative to intelligence; funding.rounds.seed.amount uses event ID",
    )
    support: Literal["direct", "inferred", "conflicting"]
    summary: str = Field(min_length=1)
    verified_at: str
    _verified = field_validator("verified_at")(timestamp)


class Research(Structured):
    researched_at: str
    sources: list[Source] = Field(min_length=1)
    topics_checked: list[str]
    search_queries: list[str] = Field(min_length=1)
    gaps: list[str]
    conflicts: list[str]
    diligence_questions: list[str] = Field(min_length=3)
    _timestamp = field_validator("researched_at")(timestamp)
    topic_checks: list[TopicCheck] = []
    claim_checks: list[ClaimCheck] = []


class Intelligence(Structured):
    schema_version: Literal[1, 2]
    company: Company
    funding: Funding
    people: list[Person]
    investors: list[Investor]
    ownership: Ownership
    financials: list[Metric]
    traction: list[Milestone]
    intellectual_property: list[Milestone]
    comparables: list[Comparable]
    research: Research
    israel_connection: IsraelConnection | None = None

    @model_validator(mode="after")
    def evidence_integrity(self) -> Intelligence:
        source_ids = [s.id for s in self.research.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("research source IDs must be unique")
        known = set(source_ids)

        def check(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"source_ids", "total_source_ids"}:
                        missing = set(child) - known
                        if missing:
                            raise ValueError(f"unknown evidence source IDs: {sorted(missing)}")
                    else:
                        check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        check(self.model_dump())
        rounds = {r.id: r for r in self.funding.rounds}
        for investor in self.investors:
            if set(investor.round_ids) - set(rounds):
                raise ValueError(f"investor {investor.name} references unknown funding rounds")
        for section, entries in (
            ("financials", self.financials),
            ("traction", self.traction),
            ("intellectual_property", self.intellectual_property),
            ("team", self.people),
            ("investors", self.investors),
            ("competition", self.comparables),
        ):
            if not entries and not any(section in gap.lower() for gap in self.research.gaps):
                raise ValueError(f"empty {section} needs an explicit research gap")
        if set(RESEARCH_TOPICS) - set(self.research.topics_checked):
            raise ValueError(
                "research must cover all required topics, including funding and ownership"
            )
        if self.funding.status in {"not_found", "undisclosed"} and not any(
            "funding" in gap.lower() for gap in self.research.gaps
        ):
            raise ValueError("unavailable funding needs an explicit research gap")
        ownership = self.ownership
        if (
            ownership.status == "reported"
            or ownership.parent_company
            or ownership.stock_ticker
            or ownership.shareholders
        ) and not ownership.source_ids:
            raise ValueError("reported ownership needs evidence")
        if self.schema_version == 2:
            if not self.company.company_id or self.israel_connection is None:
                raise ValueError("v2 requires stable company_id and a sourced israel_connection")
            topics = [item.topic for item in self.research.topic_checks]
            if len(topics) != len(set(topics)) or set(RESEARCH_TOPICS) - set(topics):
                raise ValueError("v2 requires one dated topic_check for every research topic")
            self.verify_claims()
        return self

    def verify_claims(self) -> None:
        required = {}
        if self.company.operating_status != "unknown":
            required["company.operating_status"] = self.company.source_ids
        if self.israel_connection and self.israel_connection.status != "not_established":
            required["israel_connection"] = self.israel_connection.source_ids
        for event in self.funding.rounds:
            if (
                event.amount_status == "conflicting"
                and len({(item.amount, item.currency) for item in event.amount_observations}) < 2
            ):
                raise ValueError("v2 conflicting amounts require at least two sourced observations")
            for observation in event.amount_observations:
                required[
                    f"funding.rounds.{event.id}.amount_observations.{observation.id}.amount"
                ] = observation.source_ids
            for field in (
                "amount",
                "pre_money_valuation",
                "post_money_valuation",
                "lead_investors",
                "participating_investors",
            ):
                if getattr(event, field):
                    required[f"funding.rounds.{event.id}.{field}"] = event.source_ids
        if self.funding.reported_total_raised is not None:
            required["funding.reported_total_raised"] = self.funding.total_source_ids
        if self.ownership.parent_company:
            required["ownership.parent_company"] = self.ownership.source_ids
        deal_ids = [deal.id for deal in self.ownership.deals]
        if len(deal_ids) != len(set(deal_ids)):
            raise ValueError("v2 deal IDs must be unique")
        for deal in self.ownership.deals:
            if not deal.id or deal.amount_status is None:
                raise ValueError("v2 deals require stable id and amount_status")
            if deal.status != "unknown":
                required[f"ownership.deals.{deal.id}.status"] = deal.source_ids
            if deal.company_role != "unknown":
                required[f"ownership.deals.{deal.id}.company_role"] = deal.source_ids
            if deal.amount is not None:
                required[f"ownership.deals.{deal.id}.amount"] = deal.source_ids
        for holding in self.ownership.holdings:
            required[
                f"ownership.holdings.{holding.shareholder}@{holding.as_of}.ownership_percent"
            ] = holding.source_ids
        for metric in self.financials:
            if metric.numeric_value is not None:
                required[f"financials.{metric.name}@{metric.as_of}.numeric_value"] = (
                    metric.source_ids
                )
        checks = {item.field_path: item for item in self.research.claim_checks}
        if len(checks) != len(self.research.claim_checks):
            raise ValueError("claim_checks field paths must be unique")
        missing = []
        for path, source_ids in required.items():
            check = checks.get(path)
            if (
                not check
                or check.support != "direct"
                or not set(check.source_ids) & set(source_ids)
            ):
                missing.append(path)
        if missing:
            raise ValueError(
                "v2 requires direct claim verification linked to the fact's sources: "
                + ", ".join(missing)
                + ". field_path is relative to intelligence (no 'intelligence.' prefix); "
                "each direct check must share a source ID with its fact"
            )


class Startup(BaseModel):
    # Retain site-specific extensions such as tags, scores, and crawler metadata.
    model_config = ConfigDict(extra="allow", strict=True, allow_inf_nan=False)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    detailed_description: str = Field(min_length=1)
    logo: str
    website: str
    sector: str
    founded: str
    headquarters: str
    employees: str
    funding_stage: str
    entity_type: EntityType
    dual_use: bool
    dual_use_description: str
    key_technologies: list[str]
    use_cases: list[str]
    investible: bool
    investment_rationale: str
    strategic_value: str
    public_sources: list[str | dict[str, Any]]
    potential_score: Score
    technology_score: Score
    market_score: Score
    team_score: Score
    dual_use_score: Score
    strategic_alignment_score: Score
    stage: Literal["early", "mid", "mature", "unknown"]
    stage_rationale: str
    competitors: list[str]
    competitive_edge: str
    risk_factors: list[str]
    risk_level: Literal["low", "medium", "high", "unknown"]
    signals: list[str]
    priority_rank: int
    priority_rationale: str
    crawled_at: str
    updated_at: str
    intelligence: Intelligence

    @field_validator("website")
    @classmethod
    def website_url(cls, value: str) -> str:
        if value == "Unknown":
            return value
        http_url(value)
        if urlparse(value).hostname == "finder.startupnationcentral.org":
            raise ValueError("website must be the company's canonical website")
        return value

    @field_validator("updated_at")
    @classmethod
    def updated(cls, value: str) -> str:
        return timestamp(value)

    @model_validator(mode="after")
    def record_kind(self) -> Startup:
        if self.model_extra and self.model_extra.get("redirect_to"):
            raise ValueError("a researched record cannot be a redirect")
        return self

    def analysis_words(self) -> int:
        fields = (
            "description",
            "detailed_description",
            "dual_use_description",
            "investment_rationale",
            "strategic_value",
            "competitive_edge",
            "stage_rationale",
            "priority_rationale",
            "key_technologies",
            "use_cases",
            "competitors",
            "risk_factors",
        )
        return sum(len(re.findall(r"\b[\w'-]+\b", str(getattr(self, field)))) for field in fields)
