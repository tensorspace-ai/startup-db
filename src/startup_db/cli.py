"""One command line interface for startup discovery, enrichment, and diligence."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from filelock import Timeout

from .catalog import catalog, load_toml
from .config import Settings, settings_from_file
from .index import ResearchIndex, Screen
from .planning import research_plan
from .providers import doctor
from .state import Store
from .telemetry import metrics


def add_settings(command: argparse.ArgumentParser, *, research: bool = False) -> None:
    command.add_argument("--config", type=Path, help="Read settings from a TOML [agent] table")
    command.add_argument("--root", type=Path, default=argparse.SUPPRESS)
    command.add_argument("--dir", dest="directory", type=Path, default=argparse.SUPPRESS)
    command.add_argument("--state-dir", type=Path, default=argparse.SUPPRESS)
    command.add_argument("--min-words", type=int, default=argparse.SUPPRESS)
    command.add_argument("--min-sources", type=int, default=argparse.SUPPRESS)
    command.add_argument(
        "--topics",
        type=lambda value: [topic.strip() for topic in value.split(",") if topic.strip()],
        default=argparse.SUPPRESS,
    )
    command.add_argument("--stale-days", type=int, default=argparse.SUPPRESS)
    command.add_argument(
        "--duplicate-threshold",
        "--semantic-duplicate-threshold",
        dest="duplicate_threshold",
        type=float,
        default=argparse.SUPPRESS,
        help="Fuzzy name guard; this is not an embedding score",
    )
    if not research:
        return
    command.add_argument(
        "--provider",
        "--agent",
        dest="provider",
        choices=("codex", "copilot", "claude"),
        default=argparse.SUPPRESS,
    )
    command.add_argument(
        "--model",
        default=argparse.SUPPRESS,
        help="Override provider model; Codex otherwise uses its configured model",
    )
    command.add_argument(
        "--effort", choices=("low", "medium", "high", "xhigh"), default=argparse.SUPPRESS
    )
    command.add_argument("--focus", default=argparse.SUPPRESS)
    command.add_argument(
        "--limit",
        type=int,
        default=argparse.SUPPRESS,
        help="Successful discoveries or selected enrichment records; 0 means continuous discovery/all records",
    )
    command.add_argument(
        "--jobs",
        type=int,
        default=argparse.SUPPRESS,
        help="Concurrent enrichment workers; discovery is sequential",
    )
    command.add_argument(
        "--only",
        default=argparse.SUPPRESS,
        help="Comma-separated filenames, slugs, or company names",
    )
    command.add_argument(
        "--resume-from",
        default=argparse.SUPPRESS,
        help="Select from this record in smallest-analysis-first order",
    )
    command.add_argument(
        "--resume",
        metavar="RUN_ID",
        help="Resume the stored configuration and job list for an existing run",
    )
    command.add_argument(
        "--timeout",
        "--codex-timeout",
        "--copilot-timeout",
        dest="timeout",
        type=float,
        default=argparse.SUPPRESS,
    )
    command.add_argument(
        "--max-attempts",
        "--max-consecutive-failures",
        dest="max_attempts",
        type=int,
        default=argparse.SUPPRESS,
    )
    command.add_argument(
        "--max-rate-limit-retries",
        type=int,
        default=argparse.SUPPRESS,
        help="Default 3; 0 allows unlimited quota retries",
    )
    command.add_argument("--rate-limit-default-wait", type=float, default=argparse.SUPPRESS)
    command.add_argument("--rate-limit-fudge", type=float, default=argparse.SUPPRESS)
    command.add_argument("--max-quota-wait", type=float, default=argparse.SUPPRESS)
    command.add_argument("--failure-backoff", type=float, default=argparse.SUPPRESS)
    command.add_argument("--sleep", type=float, default=argparse.SUPPRESS)
    command.add_argument(
        "--sandbox",
        choices=("read-only", "workspace-write", "danger-full-access"),
        default=argparse.SUPPRESS,
    )
    command.add_argument(
        "--approval-policy", choices=("never", "on-request"), default=argparse.SUPPRESS
    )
    command.add_argument(
        "--no-search", dest="search", action="store_false", default=argparse.SUPPRESS
    )
    command.add_argument(
        "--persist-sessions", dest="ephemeral", action="store_false", default=argparse.SUPPRESS
    )
    command.add_argument("--silent", action="store_true", default=argparse.SUPPRESS)
    command.add_argument(
        "--claude-dangerously-skip-permissions", action="store_true", default=argparse.SUPPRESS
    )
    command.add_argument(
        "--dry-run",
        action="store_true",
        help="Print prompts and commands; create no state and invoke no provider",
    )


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="startup-agent", description=__doc__)
    commands = command.add_subparsers(dest="command", required=True)
    for name, aliases, help_text in (
        ("discover", ["find"], "Find new startups and research complete diligence dossiers"),
        ("improve", ["enrich"], "Deepen existing records, shortest analysis first"),
        ("refresh", [], "Refresh funding, investors, ownership, team, and traction"),
    ):
        research = commands.add_parser(name, aliases=aliases, help=help_text)
        research.set_defaults(command=name)
        add_settings(research, research=True)
        if name != "discover":
            freshness = research.add_mutually_exclusive_group()
            freshness.add_argument(
                "--due-only",
                action="store_true",
                default=argparse.SUPPRESS,
                help="Select missing or stale research in priority order (default for refresh)",
            )
            freshness.add_argument(
                "--force",
                dest="due_only",
                action="store_false",
                default=argparse.SUPPRESS,
                help="Research selected records even when recently checked",
            )
        if name == "discover":
            research.add_argument(
                "--validate-file", type=Path, help="Compatibility alias for validate FILE"
            )
        if name == "refresh":
            research.add_argument(
                "--retry-failed-days",
                type=int,
                default=argparse.SUPPRESS,
                help="Defer unchanged exhausted validation failures for 7 days; 0 retries immediately",
            )
    schema = commands.add_parser("schema", help="Print the complete startup research JSON Schema")
    schema.add_argument(
        "--refresh", action="store_true", help="Print the partial incremental-update contract"
    )
    commands.add_parser("doctor", help="Check installed provider CLIs without starting research")
    performance = commands.add_parser(
        "benchmark", help="Measure catalog, matching, selection, and workspace inputs offline"
    )
    add_settings(performance)
    performance.add_argument("--iterations", type=int, default=3)
    performance.add_argument("--output", type=Path)
    planning = commands.add_parser(
        "plan", help="Prioritize missing, stale, or conflicting research topics"
    )
    add_settings(planning)
    planning.add_argument("--limit", type=int, default=20)
    indexed = commands.add_parser(
        "index", help="Refresh the persistent company, deal, and investor index"
    )
    add_settings(indexed)
    screening = commands.add_parser(
        "screen", help="Search companies and filter sourced funding and Israeli identity"
    )
    add_settings(screening)
    screening.add_argument("query", nargs="?", default="")
    for field in (
        "sector",
        "headquarters",
        "entity-type",
        "operating-status",
        "israel",
        "investor",
    ):
        screening.add_argument("--" + field)
    screening.add_argument("--min-funding", type=float)
    screening.add_argument("--max-funding", type=float)
    screening.add_argument("--currency", default="USD")
    screening.add_argument(
        "--funding-basis", choices=("reported_total", "observed_equity"), default="reported_total"
    )
    screening.add_argument("--min-schema", type=int, default=0)
    screening.add_argument("--limit", type=int, default=20)
    screening.add_argument("--offset", type=int, default=0)
    screening.add_argument(
        "--save", metavar="NAME", help="Save this screen and checkpoint all its matching records"
    )
    profile = commands.add_parser(
        "company", help="Show a full dossier and funding/deal timeline by ID, name, or filename"
    )
    add_settings(profile)
    profile.add_argument("identity")
    deals = commands.add_parser("deals", help="Screen structured funding and ownership events")
    add_settings(deals)
    for field in ("company", "investor", "since", "until", "kind"):
        deals.add_argument("--" + field)
    deals.add_argument("--min-amount", type=float)
    deals.add_argument("--currency", default="USD")
    deals.add_argument("--include-inactive", action="store_true")
    deals.add_argument("--limit", type=int, default=100)
    investors = commands.add_parser(
        "investors", help="Find investors or view their catalog portfolio and rounds"
    )
    add_settings(investors)
    investors.add_argument("query", nargs="?", default="")
    investors.add_argument(
        "--portfolio",
        action="store_true",
        help="Use an exact investor name for company and round membership",
    )
    investors.add_argument("--limit", type=int, default=100)
    watch = commands.add_parser(
        "watch", help="List saved screens or checkpoint added, removed, and changed companies"
    )
    add_settings(watch)
    watch.add_argument("name", nargs="?")
    check = commands.add_parser("validate", help="Validate staged or existing TOML records")
    check.add_argument("paths", nargs="+", type=Path)
    add_settings(check)
    check.add_argument(
        "--legacy",
        action="store_true",
        help="Check old records without requiring a research dossier",
    )
    coverage = commands.add_parser(
        "audit", help="Report research gaps and duplicate identities without editing records"
    )
    add_settings(coverage)
    export = commands.add_parser("export", help="Export full JSON dossiers or a funding-round CSV")
    add_settings(export)
    export.add_argument("--format", choices=("json", "funding-csv"), default="json")
    export.add_argument("--output", type=Path, help="Default: stdout")
    status = commands.add_parser("status", help="Show durable run and job status")
    add_settings(status)
    status.add_argument("run_id", nargs="?")
    measurement = commands.add_parser(
        "metrics", help="Summarize retained provider timings, usage, and quota failures"
    )
    add_settings(measurement)
    measurement.add_argument("run_id", nargs="?")
    return command


def configuration(args: argparse.Namespace) -> Settings:
    overrides: dict[str, object] = {
        key: value for key, value in vars(args).items() if key in Settings.model_fields
    }
    defaults: dict[str, object] = {}
    if args.command in {"improve", "refresh"}:
        defaults = {"limit": 0, "min_words": 700}
    if args.command == "refresh":
        defaults["due_only"] = True
    return settings_from_file(args.config, overrides, defaults)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if getattr(args, "validate_file", None):
            args.command = "validate"
            args.paths = [args.validate_file]
            args.legacy = False
        if args.command == "schema":
            from .models import Startup
            from .patches import refresh_schema

            print(
                json.dumps(
                    refresh_schema() if args.refresh else Startup.model_json_schema(), indent=2
                )
            )
            return 0
        if args.command == "doctor":
            checks = doctor()
            print(json.dumps(checks, indent=2))
            return 0 if any(check["available"] for check in checks) else 1
        settings = configuration(args)
        if args.command == "metrics":
            directory = settings.state_dir
            if args.run_id:
                if not (directory / "runs.sqlite3").exists():
                    raise ValueError(f"Unknown run ID: {args.run_id}")
                Store(directory).run(args.run_id)
                directory /= args.run_id
            print(json.dumps(metrics(directory), indent=2))
            return 0
        if args.command in {"index", "screen", "company", "deals", "investors", "watch"}:
            index = ResearchIndex(settings.state_dir / "research.sqlite3", settings.directory)
            synced = index.sync()
            indexed_result: Any
            if args.command == "index":
                indexed_result = synced
            elif args.command == "screen":
                fields = {
                    key: value
                    for key, value in vars(args).items()
                    if key in Screen.model_fields and value is not None
                }
                filters = Screen.model_validate(fields)
                indexed_result = index.screen(filters)
                if args.save:
                    indexed_result["saved_screen"] = index.save_screen(args.save, filters)
            elif args.command == "company":
                indexed_result = index.company(args.identity)
            elif args.command == "deals":
                fields = {
                    key: value
                    for key, value in vars(args).items()
                    if key
                    in {
                        "company",
                        "investor",
                        "since",
                        "until",
                        "kind",
                        "currency",
                        "min_amount",
                        "include_inactive",
                        "limit",
                    }
                    and value is not None
                }
                indexed_result = index.deals(**fields)
            elif args.command == "investors":
                if args.portfolio:
                    if not args.query:
                        raise ValueError("Investor portfolio requires an exact investor name")
                    indexed_result = {
                        "companies": index.screen(Screen(investor=args.query, limit=args.limit)),
                        "rounds": index.deals(investor=args.query, limit=args.limit),
                    }
                else:
                    indexed_result = index.investors(args.query, args.limit)
            else:
                indexed_result = (
                    index.save_screen(args.name) if args.name else index.saved_screens()
                )
            print(
                json.dumps(
                    {"index": synced, "result": indexed_result},
                    indent=2,
                    ensure_ascii=False,
                    default=str,
                )
            )
            return 0
        if args.command == "benchmark":
            from .benchmark import benchmark

            content = json.dumps(benchmark(settings.directory, args.iterations), indent=2) + "\n"
            if args.output:
                args.output.write_text(content, encoding="utf-8")
            else:
                print(content, end="")
            return 0
        if args.command in {"discover", "improve", "refresh"}:
            from .runner import Runner, dry_run

            if args.dry_run:
                if args.resume:
                    raise ValueError("--resume and --dry-run cannot be combined")
                print(json.dumps(dry_run(settings, args.command), indent=2, ensure_ascii=False))
                return 0
            return Runner(settings, args.command, resume=args.resume).run()
        if args.command == "status":
            if not (settings.state_dir / "runs.sqlite3").exists():
                if args.run_id:
                    raise ValueError(f"Unknown run ID: {args.run_id}")
                print("[]")
                return 0
            store = Store(settings.state_dir)
            if args.run_id:
                store.run(args.run_id)
            print(json.dumps(store.summary(args.run_id), indent=2))
            return 0
        records = catalog(settings.directory)
        result: Any
        if args.command == "plan":
            result = research_plan(records, settings.topics, settings.stale_days)
            print(
                json.dumps(
                    {
                        "due": len(result),
                        "items": result[: settings.limit] if settings.limit else result,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "validate":
            from .validation import validate

            result = {
                str(path): validate(
                    load_toml(path),
                    existing=records,
                    target=path,
                    min_sources=settings.min_sources,
                    min_words=settings.min_words,
                    duplicate_threshold=settings.duplicate_threshold,
                    legacy=args.legacy,
                ).as_dict()
                for path in args.paths
            }
            print(json.dumps(result, indent=2))
            return 0 if all(report["ok"] for report in result.values()) else 1
        from .export import audit, exported_records, funding_csv

        if args.command == "audit":
            result = audit(records, settings.min_sources, settings.min_words)
            print(json.dumps(result, indent=2))
            return 0
        content = (
            funding_csv(records)
            if args.format == "funding-csv"
            else json.dumps(exported_records(records), indent=2, ensure_ascii=False, default=str)
            + "\n"
        )
        if args.output:
            args.output.write_text(content, encoding="utf-8")
        else:
            print(content, end="")
        return 0
    except KeyboardInterrupt:
        print("Interrupted; use --resume RUN_ID to continue.", file=sys.stderr)
        return 130
    except (OSError, ValueError, sqlite3.Error, Timeout) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
