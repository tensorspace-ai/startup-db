# Data review: October 22, 2026

This review covers 31 new dossiers and 51 expanded profiles. Of the expanded
profiles, 35 move from legacy prose to schema v2 and 16 refresh existing
structured research. The batch includes 1,763 research-source references;
references can recur across companies.

| Coverage | Before | After |
| --- | ---: | ---: |
| TOML records | 1,894 | 1,925 |
| Company and organization profiles | 1,870 | 1,901 |
| Valid schema-v2 dossiers | 477 | 543 |
| Legacy profiles | 1,393 | 1,358 |
| Redirects | 24 | 24 |

All 82 changed dossiers pass the default publication requirements, including
typed data, source references, claim checks, research depth, and comparison
against catalog identities. All 543 structured dossiers also pass the default
contract. None of the changed profiles has an exact identity collision, and
the batch introduces no new exact collision pairs.

The content review checked financing scope, dated totals, company identity,
ownership, and the distinction between announced and completed transactions.
It removed four absence-of-parent placeholders from structured parent fields,
aligned four acquired businesses with their acquisition status, moved ten
parent-company tickers into ownership prose, and corrected three source types.
Unknown ownership remains omitted from the structured parent field and described
in the research narrative.

Original announcements were spot-checked for acquisitions and selected funding
claims, including [WalkMe's completed SAP acquisition](https://www.sec.gov/Archives/edgar/data/1847584/000117891324003051/exhibit_99-1.htm),
[Aquarius Spectrum's Aliaxis acquisition](https://aliaxis.com/wp-content/uploads/Press-release-Aliaxis-Aquarius-Spectrum-Closing_FINAL.pdf),
[Loora's Series B](https://prtimes.jp/main/html/rd/p/000000001.000186158.html),
and [Intuition Robotics' Kanematsu investment](https://www.kanematsu.co.jp/en/press/release/20250909_release).
All 31 new company website URLs returned successful HTTP responses during review.
A responsive website does not establish the accuracy of its claims.

The offline suite passed with 122 tests and three skips. The search index loaded
all 1,925 records without invalid dossiers, and the funding CSV export produced
1,306 events across 468 profiles. The public-tree check passed for the proposed
index. An independent `detect-secrets` scan of the startup data and review
documentation reported no findings.

The remaining 1,358 legacy profiles have not passed the full dossier contract.
The catalog audit still flags 69 existing profiles for exact identity overlaps,
down from 77 before this batch. Those flags can involve shared domains, aliases,
or redirects and require individual review; they are not 69 confirmed duplicate
companies. Structural validation and source spot-checks do not independently
verify every assertion in this batch or the broader catalog.

To reproduce the structural review:

```sh
uv run startup-agent audit
uv run startup-agent validate data/startups/walkme.toml
uv run pytest -q
python3 scripts/check_public_tree.py
git diff --cached --check
```
