# Startup research agent

`startup-db` is a standalone uv-managed Python package extracted from
Claw & Talon. Startup records remain TOML; structured private-market research
lives in an additional `intelligence` table. See [privacy guidance](privacy.md)
before running providers or sharing generated artifacts.

## Setup

```sh
uv sync --locked
uv run startup-agent --help
```

Install and authenticate your chosen CLI separately: Codex, GitHub Copilot CLI
(`copilot` or `gh copilot`), or Claude Code. Codex uses your configured model unless
`--model` is supplied. There is no new API credential requirement.

## Research data

The dossier tracks legal names and aliases, products, business model, locations,
founders and executives, investors, funding rounds, valuations, ownership, exits,
financial metrics, traction, intellectual property, and comparables. Every
structured fact references source IDs; sources include publisher, URL, access date,
publication date when known, and supported claims.

Funding amounts use absolute currency units (`10000000`, not `10` for $10M).
Rounds distinguish equity, convertible, debt, grants, secondaries, and IPOs;
extensions remain separate events linked to the original round, with explicit
incremental, cumulative, or unknown amount scope. Cumulative and unknown-scope
amounts are excluded from observed sums. Announced,
closed, targeted, and cancelled transactions are distinct. Dates preserve known
precision. Undisclosed amounts and valuations are omitted, never zero-filled.
Reported company totals have their own sources, date, and scope. Observed round
sums stay separate and grouped by currency; debt, grants, secondaries, targets,
and cancelled rounds are excluded from observed equity totals.

This collects the categories commonly used in private-market diligence from
accessible sources. It does not provide licensed PitchBook access or assume that
private cap tables, revenues, or valuations are publicly available. Research must
record missing information and conflicting sources explicitly.

Schema and citation checks establish structural consistency. They cannot prove
that a source supports every claim; inspect retained prompts, source claims, and
validation reports when reviewing research.

### Schema v2 and Israeli coverage

New publications require `intelligence.schema_version = 2`; v1 dossiers remain
readable and can be upgraded by `improve`. The default focus now covers technology
sectors across Israel, including Israeli-founded firms headquartered overseas.
Discovery requires a sourced `israel_connection` with an explicit basis, instead
of inferring nationality from an English name or requiring an Israeli HQ.

V2 adds a stable `company.company_id`, optional sourced legal registry identifiers,
incorporation country and founding date, disclosed ownership holdings, and typed
financial values with units, currency, period, date and reported/estimated basis.
Keep private metrics unknown when they are not disclosed. Numerical values do not
replace the original published display value.

Each research topic has its own dated `topic_checks` entry, queries, source IDs,
status and next-review date. A recent prose edit no longer makes old funding
research appear fresh. `claim_checks` binds critical facts to direct evidence using
stable paths, e.g. `funding.rounds.series-c-2025.amount`; numerical funding and
valuation fields fail v2 validation when only inference is recorded. This is an
auditable evidence assertion, not an automated guarantee of source truth.

Conflicting round amounts can retain multiple sourced `amount_observations`, each
with a stable ID and direct claim check, while the canonical amount stays absent.
Those alternatives are excluded from totals and monetary filters. Deal records
have stable IDs, explicit buyer/seller/target/issuer role, announcement/closing
dates and amount disclosure status. Direct checks also cover deal status, role,
price and disclosed holding percentages. Numeric metric checks include their
date (`financials.<name>@<as_of>.numeric_value`), and `value_relation` preserves
published bounds such as "more than" rather than turning them into exact figures.

`funding.history_status` and `coverage_through` distinguish a partial history from
a reconstructed public history. Observed totals still exclude undisclosed values,
cumulative extensions, grants, debt and secondary proceeds.

### Targeted refresh and planning

```sh
uv run startup-agent plan --topics funding,investors --stale-days 30 --limit 20
uv run startup-agent refresh --only company-slug --topics funding,investors
uv run startup-agent refresh --due-only --topics funding,investors --limit 20
uv run startup-agent refresh --only company-slug --topics funding --force
```

V2 refresh can produce `update.json` with a partial `updates` object. The host merges
new rounds by ID, sources by ID, people by name/role, dated financial metrics by
name/date, and topic checks by topic. Historical facts, sources, unselected
freshness checks, comments and long analysis remain intact. A source ID cannot be
rebound to a different URL; null-based deletion is rejected. Source conflicts and
corrections should be recorded explicitly. Legacy refresh performs full enrichment
first because it lacks the structured baseline needed for patches.

Refresh defaults to `--due-only`: it skips recently checked topics and prioritizes
missing or stale checks. A checked conflict becomes due on its next review date
or after `--stale-days` (default 30), rather than immediately on every run.
`--force` deliberately researches selected records even when fresh. `--only`
and `--topics` narrow selection without bypassing freshness. Improve keeps its
existing default; use `improve --due-only` to filter it by freshness.
New refresh runs also defer unchanged records whose validation attempts were
exhausted within the last seven days, so repeated batches can reach other startups.
The saved baseline, due topics, and research settings must still match. Edited
records, changed research settings, and provider failures remain retryable.
Use `--retry-failed-days 0` to retry these failures now, or `--force` to bypass
both freshness and failure deferral. `--resume` retains the saved job list and
settings and explicitly retries its failed jobs.
Targeted v2 refresh reads a compact `current.json` rather than the long narrative;
the original `current.toml` remains available for audit or a necessary full rewrite.
Its `schema.json` describes partial updates and required array identities; the
final merged record still passes the full publication schema. Inspect that contract
with `uv run startup-agent schema --refresh`.
Publication also converts URL strings to source objects when `public_sources`
contains both formats, preserving labels and descriptions while keeping the TOML
compatible with the site's parser.

The research guide includes English/Hebrew queries and company/investor releases,
Finder, Israeli regulatory/government sources, TASE disclosures and patent sources.
SEC Form D offering targets must be distinguished from amounts actually sold, and
Israel Innovation Authority grants remain separate from equity funding.
Treat retained directory pages as historical leads and corroborate current facts
with original sources.

## Commands

### Company, deal, and investor research

```sh
# Build/update the SQLite/FTS5 index and report actual structured coverage.
uv run startup-agent index

# Broad search includes legacy profiles, with verification status visible.
uv run startup-agent screen quantum --entity-type startup --limit 20

# Sourced Israeli dossiers with at least $100M reported funding.
uv run startup-agent screen --israel confirmed --min-schema 2 --min-funding 100000000

# Select the funding basis and currency; missing values never match.
uv run startup-agent screen --funding-basis observed_equity --currency ILS --min-funding 1000000
uv run startup-agent company quantum-machines
uv run startup-agent deals --since 2025 --kind funding --min-amount 50000000
uv run startup-agent investors "Red Dot"
uv run startup-agent investors "Red Dot Capital Partners" --portfolio

# Save all matches, then checkpoint additions, removals, and changed TOML files.
uv run startup-agent screen quantum --entity-type startup --save quantum-startups
uv run startup-agent watch quantum-startups
uv run startup-agent watch
```

Every indexed command automatically refreshes changed files. TOML is authoritative;
`.agent-runs/research.sqlite3` is a rebuildable index. An unchanged refresh checks
file signatures without reparsing prose. Malformed TOML rolls back the entire index
refresh. Invalid structured dossiers remain searchable as unverified profiles,
with errors reported; their financial claims are excluded from structured screens.
Redirect records are excluded from company results. Search supports Hebrew and
weights name matches above body matches. Filters use structured evidence only;
funding numbers are not extracted from legacy prose during indexing.

Reported totals preserve their source date and scope. Observed equity includes
known individual equity/convertible rounds, excluding debt, grants, secondary sales,
cumulative extensions, targets and cancellations. No currency conversion is implied.
Date filters overlap year/month-only dates while preserving their published
precision in results. Undated events are excluded when dates are filtered.
Investor portfolios describe participation in catalog companies/rounds, not an
investor's complete global portfolio or individual check sizes. Investor aliases
are not guessed. Saved screens track file changes locally and send no notifications.
Changed matches include before/after company status, Israeli verification and
dated funding summaries when those fields change. CSV funding exports retain
source URLs, notes, stable company IDs and conflicting amount observations.

The offline `benchmark` command measures implementation performance; timings
do not establish verified research coverage.

### Agent execution

```sh
# Inspect the next prompt without starting an agent or changing files.
uv run startup-agent discover --provider codex --dry-run

# Discover exactly five valid, distinct startups with complete research.
uv run startup-agent discover --provider codex --limit 5

# Enrich selected profiles using any supported provider.
uv run startup-agent improve --only newcore,vendict --provider claude
uv run startup-agent improve --provider copilot --model auto --jobs 3 --limit 10

# Focus on fresh deal/company information. Default selection includes all records.
uv run startup-agent refresh --only vendict --provider codex

# Inspect or resume a run. Resume restores its stored settings and selection.
uv run startup-agent status
uv run startup-agent metrics
uv run startup-agent metrics RUN_ID
uv run startup-agent discover --resume RUN_ID

# Validate new-format records, or perform a limited legacy check.
uv run startup-agent validate data/startups/quantum-machines.toml
uv run startup-agent validate data/startups/vendict.toml --legacy

# Inspect backlog/duplicates, export dossiers, or export one row per funding round.
uv run startup-agent audit > /tmp/startup-db-audit.json
uv run startup-agent export --output /tmp/startup-dossiers.json
uv run startup-agent export --format funding-csv --output /tmp/startup-rounds.csv
uv run startup-agent schema > /tmp/startup-schema.json
uv run startup-agent doctor
```

`discover` defaults to one successful publication; `--limit 0` continues until
interrupted or retries are exhausted. `improve`/`enrich` and `refresh` default to
all eligible records. Improve uses smallest analysis first; refresh prioritizes
due research. `--only`, `--resume-from`, and
`--limit` restrict that selection. Redirects and `_`-prefixed files are skipped.
Unmatched selections and malformed existing TOML are errors, rather than being
silently omitted from identity checks.

`--min-words` defaults to 900 for discovery and 700 combined analysis words for
enrichment. `--min-sources` defaults to four distinct URLs and research must include
at least one non-company source. These are publication requirements, not only
prompt requests. Empty sections must have explicit research gaps.

## Configuration

Use `--config agent.toml` after the subcommand. Paths are resolved against `root`,
which defaults to the current working directory. Explicit CLI options override
the config; the config overrides command defaults. Unknown keys are errors.

```toml
[agent]
provider = "codex"
directory = "data/startups"
state_dir = ".agent-runs"
focus = "Israeli robotics and energy startups with strategic dual-use relevance"
min_sources = 4
min_words = 900
limit = 5
timeout = 900
max_attempts = 3
max_rate_limit_retries = 3
jobs = 1
```

## Execution and recovery

Each provider runs from its own staging directory, with a compact `catalog.json`,
the complete `schema.json`, and an original `current.toml` for enrichment. Agents
write `candidate.toml`; the host checks structure, word count, sources, aliases,
domains, funding references, and preservation requirements before publication.
Existing extra fields and unchanged comments survive the merge. `crawled_at`,
`priority_rank`, and `signals` are preserved during enrichment.

Every workspace includes `check_candidate.py`. Providers run
`python3 check_candidate.py` and repair errors before finishing; it applies the
same merge and validation as the host without publishing. JSON Schema alone does
not cover the dossier's claim-link and preservation rules. Claim `field_path`
values are relative to `intelligence` (for example, `company.operating_status`
and `israel_connection`), and direct checks must share source IDs with their facts.
Retries and resumed jobs retain the latest rejected output from a successful
provider call as `previous_candidate.toml` or `previous_update.json`, together with
its validation errors, so research can be repaired instead of repeated.

Providers start with a compact `schema-guide.txt` and `current.json`, reading
specific full schema definitions or original prose only as needed. Enrichment can
omit unchanged top-level fields from `candidate.toml`; the host preserves them.
Incremental refresh context contains selected sections and the source ID/URL
index. With `refresh --due-only`, each v2 job checkpoints only its actually due
topics, so retries/resume keep that scope and fresh topic checks stay unchanged.

Preflight uses a compact identity snapshot to avoid parsing the whole repository
on every checker invocation. The host checks the live catalog again before and
under the publication lock. Checker code stays small; its original baseline and
settings are stored in a separate internal context file. A resumed run can finish
validation/publication of durably successful provider output without another
provider call. Interrupted, timed-out, rejected, and failed provider calls are
excluded from this recovery path.

The offline `benchmark` command reports schema-guide and example context sizes,
identity lookup, refresh patch merging, and staged preflight wall time. Index
updates use a filename-to-FTS-row-ID mapping, migrated automatically for existing
indexes, and screens fetch capital information in bounded batches. Patch merging
clones the original once and preserves comments without a serialize/reparse cycle.
Rejected drafts get a shorter repair-only prompt with the retained validation
errors. Parse failures also retain their draft and errors. Duplicate discoveries
keep the full selection/research procedure because they need a different company.

Publication uses a directory lock, a fresh uniqueness check, atomic replacement,
and an original-file checksum. Discovery cannot overwrite an existing record;
enrichment refuses to replace a record edited since selection. A durable
publication intent recovers an interruption between writing the file and updating
its checkpoint. Completed jobs do not rerun on resume. If a published record has
since changed, resume stops so those edits are preserved.

Discovery is sequential so each prompt sees newly published companies.
`--jobs N` runs enrichment with separate workspaces and a shared provider cooldown.
These CLIs retain their configured filesystem/tool permissions; staging plus the
prompt contract is not an OS-level isolation guarantee. Codex defaults to
`workspace-write`/`never`; Claude uses `acceptEdits` with a narrow tool allowlist;
Copilot allows writes and narrow curl/Python shell commands.

Run manifests and checkpoints are in `.agent-runs/runs.sqlite3`. Each run/item/attempt
directory retains `prompt.txt`, `schema.json`, `catalog.json`, the original record,
provider stdout/stderr, process status, `candidate.toml`, `validation.json`, and
`validated.toml` when it passes. Rejected candidates stay outside the live database.
Provider logs may contain researched content and should be treated accordingly.

`process.json` also retains preparation/provider elapsed times, input artifact sizes,
event/tool counts, provider-reported token/cache usage and reported USD cost when
available. `validation.json` retains validation/publication timings. `metrics`
summarizes these artifacts, preserving missing usage/cost as unknown. It does not
estimate costs from token counts or call provider time an end-to-end measurement.
Quota cooldown and user review are outside the provider timing. Offline tests use
mock providers; actual latency and token savings need real retained provider runs.

Quota classification uses provider error events/failure channels. Research prose
or shell output mentioning rate limits cannot cause quota sleeps. Auth, billing,
missing CLI, and unsupported-model errors stop the run. Transient/validation
failures retry with feedback and capped backoff. Quota waits understand relative
durations, epochs, Retry-After seconds/dates, and timezone-qualified reset clocks.
The default is three ordinary attempts plus three quota retries per item;
`--max-rate-limit-retries 0` explicitly enables unlimited quota retries. Timeout or
interrupt terminates the CLI process group on POSIX. Resume retains the original
provider, model override, count, selection, and research requirements. When no model
override is set, Codex continues to use its own current configuration.

## Development

```sh
uv sync --locked
uv run pytest
uv run ruff check src/startup_db tests/agent
uv run mypy
uv build --out-dir python-dist
```

Use `python-dist` for Python artifacts; generated packages are ignored by Git.

## Migration from the scripts

These commands still work and delegate to the package through uv:

```sh
python3 scripts/find_new_startup_with_agent.py --agent claude --limit 3
python3 scripts/improve_opportunities_with_codex.py --only vendict --dry-run
python3 scripts/improve_opportunities_with_copilot.py --model auto --limit 5
```

The wrappers need only standard-library Python and uv; they install/use the locked
project environment. `--codex-timeout`, `--copilot-timeout`, `--agent`,
`--semantic-duplicate-threshold`, and `--validate-file` remain aliases. The
duplicate threshold now measures explainable fuzzy name similarity, alongside
exact aliases and domains; it is not a semantic embedding score. The previous
`--url-timeout` preflight option is accepted by launchers but no longer changes
execution: URL checks are part of the research task. Ordinary retries are bounded
by `--max-attempts` (legacy `--max-consecutive-failures`); zero is no longer allowed
for that setting. Quota retries default to three instead of infinite retries.
Codex now follows your configured model by default instead of hard-coding Spark;
use `--model` to explicitly retain a particular model. Site records that predate
the new schema can be read, selected, enriched, and exported without migration;
`validate --legacy` is available for their basic checks. New publications require
the complete evidence-linked intelligence dossier.
