"""Lightweight shared vocabulary and date precision contract."""

from __future__ import annotations

import datetime as dt
import re
from typing import Literal

EntityType = Literal[
    "startup",
    "public_company",
    "acquired_asset",
    "defense_prime",
    "government_owned_company",
    "fund",
    "non_israeli_strategic_reference",
    "unverified_record",
    "defunct_or_wound_down",
]
RESEARCH_TOPICS = (
    "identity",
    "funding",
    "investors",
    "team",
    "ownership",
    "financials",
    "traction",
    "intellectual_property",
    "competition",
    "dual_use",
)


def partial_date(value: str) -> str:
    """Keep published precision; never invent a day for a year-only announcement."""
    if not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value):
        raise ValueError("must be YYYY, YYYY-MM, or YYYY-MM-DD")
    dt.date.fromisoformat(value + {4: "-01-01", 7: "-01", 10: ""}[len(value)])
    return value
