"""Command-line entry point: ``python -m fundamental_agent
run|score|pending|quality|repair-accessions|migrate|coverage``."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from fundamental_agent import db, quality, repair
from fundamental_agent.card import build_card, render
from fundamental_agent.config import DEFAULT_EDGAR_BASE_URL, Settings
from fundamental_agent.edgar_client import EdgarClient
from fundamental_agent.fidelity import run_fidelity
from fundamental_agent.pending import run_pending
from fundamental_agent.pipeline import DEFAULT_FORMS, DEFAULT_SINCE_YEAR, RunParams, run
from fundamental_agent.scoring import ScoreParams, parse_dates, read_lines, run_score
from kg_schema import connect
from kg_schema.cli import add_coverage_parser, coverage_from_args, resolve_db_path, run_migrate
from kg_schema.env import universe_database_path
from kg_schema.queries import connect_ro
from kg_schema.rundate import add_analysis_date_argument
from kg_schema.rundate import resolve as resolve_analysis_date
from kg_schema.versions import VersionError


def build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915 - one parser block per command
    parser = argparse.ArgumentParser(
        prog="fundamental_agent",
        description="Fundamental economic analysis of S&P 500 SEC filings.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="analyze filings and write snapshots")
    run_cmd.add_argument("--db", help="override KG_FINANCIAL_DB path")
    run_cmd.add_argument("--universe-db", help="override KG_UNIVERSE_DB path")
    add_analysis_date_argument(run_cmd)
    run_cmd.add_argument(
        "--limit", type=int, help="cap the universe to the first N tickers (dev aid)"
    )
    run_cmd.add_argument("--tickers", help="comma-separated tickers to restrict the run to")
    run_cmd.add_argument(
        "--forms",
        default=",".join(DEFAULT_FORMS),
        help=f"comma-separated filing forms (default: {','.join(DEFAULT_FORMS)})",
    )
    run_cmd.add_argument(
        "--since-year",
        type=int,
        default=DEFAULT_SINCE_YEAR,
        help=f"earliest filing year to analyze (default: {DEFAULT_SINCE_YEAR})",
    )
    run_cmd.add_argument(
        "--until-year", type=int, help="latest filing year (default: current year)"
    )
    run_cmd.add_argument(
        "--fresh",
        action="store_true",
        help="re-analyze filings even if a snapshot already exists",
    )
    run_cmd.add_argument(
        "--refresh-universe",
        action="store_true",
        help="deprecated no-op (the universe is always read fresh from universe.db)",
    )
    run_cmd.add_argument(
        "--sections",
        action="store_true",
        help="also fetch each filing's primary document and extract MD&A / risk-factor text",
    )
    run_cmd.add_argument(
        "--metrics-only",
        action="store_true",
        help="compute and store the facts, metrics, data-quality gates and rule inputs, but "
        "write no FUNDAMENTAL score and call no model (T-143 part A, P1)",
    )
    run_cmd.add_argument(
        "--allow-dirty",
        action="store_true",
        help="override the clean-tree guard (T-114) and write this run's code_version even "
        "though the working tree has uncommitted changes -- results would come from code "
        "HEAD alone can't reproduce; for a deliberate run from a work-in-progress checkout, "
        "not routine use",
    )

    score_cmd = sub.add_parser(
        "score",
        help="write the FUNDAMENTAL scores from the metrics already stored (no gateway call)",
    )
    score_cmd.add_argument("--db", help="override KG_FINANCIAL_DB path")
    score_cmd.add_argument("--universe-db", help="override KG_UNIVERSE_DB path")
    score_cmd.add_argument(
        "--rule-based",
        action="store_true",
        help="the deterministic fallback (rule-based-fallback-v1): no model call, no LLM settings",
    )
    score_cmd.add_argument(
        "--workers", type=int, default=1, help="concurrent model calls (default: 1)"
    )
    score_cmd.add_argument(
        "--for-dates",
        help="file with one ISO date per line: score each member's latest filing available then",
    )
    score_cmd.add_argument("--accessions", help="file with one accession number per line")
    score_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="print the filings and the model calls the run would make, with a prompt-size "
        "estimate per filing; write nothing and call nothing",
    )
    score_cmd.add_argument("--limit", type=int, help="cap the filings this run attempts")
    score_cmd.add_argument(
        "--metrics-version",
        help="pin one stored metric engine version (default: the newest stored per metric group)",
    )
    score_cmd.add_argument(
        "--allow-dirty",
        action="store_true",
        help="override the clean-tree guard (T-114) for this run's code_version",
    )

    pending_cmd = sub.add_parser(
        "pending",
        help="read-only: is there a filing this step has not scored? One line, no LLM (T-017)",
    )
    pending_cmd.add_argument("--db", help="override KG_FINANCIAL_DB path")
    pending_cmd.add_argument("--universe-db", help="override KG_UNIVERSE_DB path")
    pending_cmd.add_argument(
        "--metrics-only",
        action="store_true",
        help="treat a filing a metrics-only run completed as done (T-143 part A, P1); plain "
        "pending leaves it pending (no FUNDAMENTAL row) so the orchestrator still runs it",
    )
    add_analysis_date_argument(pending_cmd)

    quality_cmd = sub.add_parser(
        "quality",
        help="run the Ring-1 data-quality gates over every stored filing's metrics (backfill)",
    )
    quality_cmd.add_argument("--db", help="override KG_FINANCIAL_DB path")
    quality_cmd.add_argument(
        "--metrics-version",
        help="gate only this stored metrics engine version (default: every version stored)",
    )

    repair_cmd = sub.add_parser(
        "repair-accessions",
        help="replace legacy quarters that share one 10-Q's accession (T-120; dry run by default)",
    )
    repair_cmd.add_argument("--db", help="override KG_FINANCIAL_DB path")
    repair_cmd.add_argument(
        "--apply", action="store_true", help="write the repair (default: report the plan only)"
    )
    repair_cmd.add_argument(
        "--drop-unresolved",
        action="store_true",
        help="with --apply, also delete stale rows whose own filing the gateway cannot find",
    )

    migrate_cmd = sub.add_parser(
        "migrate", help="apply pending shared-schema migrations (advances schema_version)"
    )
    migrate_cmd.add_argument("--db", help="override KG_FINANCIAL_DB path")

    card_cmd = sub.add_parser(
        "card", help="print one company's card for an as-of date (read-only; T-151 part 3, D15)"
    )
    card_cmd.add_argument("--db", required=True, help="database to read (opened read-only)")
    card_cmd.add_argument("--cik", required=True, help="the company's CIK (APP-10a)")
    card_cmd.add_argument("--date", required=True, help="as-of date YYYY-MM-DD (point-in-time)")
    card_cmd.add_argument(
        "--metrics-version",
        help="the stored engine version to read (default: the current one)",
    )
    card_cmd.add_argument("--out", help="write the card to this file (default: stdout)")
    card_cmd.add_argument(
        "--edgar",
        action="store_true",
        help=(
            "read the filing's index on EDGAR to name its primary document, so the card carries "
            "the link to the document itself (V5); without it the card prints the filing's "
            "folder, which needs no network"
        ),
    )
    card_cmd.add_argument(
        "--no-vetoes",
        action="store_true",
        help="do not evaluate the cycle's rules on the card's date (the vetoes section)",
    )

    fidelity_cmd = sub.add_parser(
        "fidelity",
        help="compare the stored line items with the SEC's companyfacts (read-only; D16)",
    )
    fidelity_cmd.add_argument("--db", required=True, help="database to read (opened read-only)")
    fidelity_cmd.add_argument(
        "--companyfacts-dir", required=True, help="the SEC companyfacts cache directory"
    )
    fidelity_cmd.add_argument("--golden", help="an optional golden_set.csv to compare against too")
    fidelity_cmd.add_argument("--out", help="the report file (default: stdout)")
    fidelity_cmd.add_argument("--csv", help="write the mismatching (filing, item) pairs here")

    add_coverage_parser(sub)
    return parser


def _split(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def _score_settings(args: argparse.Namespace) -> Settings:
    """The settings a ``score`` run uses, with the command's overrides applied.

    ``--rule-based`` makes no model call, so it needs no LLM configuration (as ``quality`` does not).
    A dry run only counts, and falls back to the LLM-free settings when they are not configured.
    """
    if args.rule_based:
        settings = Settings.load(metrics_only=True)
    else:
        try:
            settings = Settings.load()
        except RuntimeError:
            if not args.dry_run:
                raise
            settings = Settings.load(metrics_only=True)
    updates: dict[str, object] = {}
    if args.db:
        updates["db_path"] = Path(args.db)
    if args.universe_db:
        updates["universe_db_path"] = Path(args.universe_db)
    if args.allow_dirty:
        updates["allow_dirty"] = True
    return settings.model_copy(update=updates) if updates else settings


def _run_score(args: argparse.Namespace) -> int:
    """``fundamental_agent score``: the FUNDAMENTAL rows from the stored metrics."""
    try:
        params = ScoreParams(
            rule_based=args.rule_based,
            workers=args.workers,
            for_dates=parse_dates(read_lines(args.for_dates)),
            accessions=read_lines(args.accessions),
            limit=args.limit,
            dry_run=args.dry_run,
            metrics_version=args.metrics_version,
        )
        report = run_score(_score_settings(args), params)
    except (ValueError, VersionError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    for line in report.errors[:20]:
        print(line, file=sys.stderr)
    print(json.dumps(report.as_dict()))
    # Like `run`: a batch that scored nothing and failed everything is a failed step; a run whose
    # only failures are per-filing data problems is recorded and exits 0.
    if params.dry_run:
        return 0
    return 1 if report.failed and not report.scored else 0


def _run_quality(
    args: argparse.Namespace,
) -> int:  # No LLM involved, so no Settings.load() (which insists on the LLM variables).
    conn = connect(resolve_db_path(args.db))
    try:
        db.ensure_schema(conn)
        reports = quality.gate_all(conn, engine_version=args.metrics_version)
    finally:
        conn.close()
    if not reports:
        which = args.metrics_version or "any metrics engine version"
        print(f"no fundamental_metrics stored for {which}", file=sys.stderr)
        return 1
    print(f"data-quality gates {quality.GATE_VERSION}")
    for version, report in reports.items():
        print(f"\n{version}: {report.filings} filings gated, {report.inserted} new issue rows")
        for rule_id in quality.ALL_RULE_IDS:
            hit = report.filings_by_rule.get(rule_id, 0)
            hard = report.hard_by_rule.get(rule_id, 0)
            print(f"  {rule_id:<17} {hit:>6} filings  ({hard} HARD)")
    return 0


def _run_repair(args: argparse.Namespace) -> int:
    # Gateway only, no LLM: no Settings.load().
    conn = connect(resolve_db_path(args.db))
    try:
        db.ensure_schema(conn)
        try:
            groups = repair.shared_groups(conn)
        except repair.RepairRefused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        if not groups:
            print("no shared accession numbers: nothing to repair")
            return 0
        base_url = os.environ.get("EDGAR_BASE_URL", DEFAULT_EDGAR_BASE_URL)
        with EdgarClient(base_url) as edgar:
            outcomes = repair.repair(
                conn, edgar, apply=args.apply, drop_unresolved=args.drop_unresolved, groups=groups
            )
    finally:
        conn.close()
    verb = "replaced" if args.apply else "would replace (dry run)"
    for o in outcomes:
        g = o.group
        print(f"{g.ticker} {g.accession} (filed {g.filing_date}): keep {g.keep_period}")
        for r in o.replaced:
            s = r.stale
            print(
                f"  {verb:<24} {s.fiscal_period} (filing {s.filing_id}, {s.facts} facts, "
                f"{s.sections} sections) -> {r.accession} filed {r.filing_date}"
                + (
                    f", period end {s.period_end} -> {r.period_end}"
                    if r.period_end != s.period_end
                    else ""
                )
            )
        for s in o.unresolved:
            state = "dropped" if o.dropped else "unresolved, left as is"
            print(f"  {state:<24} {s.fiscal_period} (filing {s.filing_id})")
        if o.error:
            print(f"  ! gateway: {o.error}", file=sys.stderr)
    pending = sum(1 for o in outcomes if o.unresolved and not o.dropped)
    print(
        f"\n{len(outcomes)} shared accessions; "
        f"{sum(len(o.replaced) for o in outcomes)} quarters {'replaced' if args.apply else 'found'}; "
        f"{pending} accessions left unresolved"
    )
    return 1 if pending else 0


def _run_card(args: argparse.Namespace) -> int:
    # Read-only: connect_ro never opens a write connection.

    conn = connect_ro(resolve_db_path(args.db))
    try:
        card = build_card(
            conn,
            args.cik,
            args.date,
            metrics_version=args.metrics_version,
            with_edgar=bool(args.edgar),
            with_vetoes=not args.no_vetoes,
        )
    finally:
        conn.close()
    text = render(card)
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


def _run_fidelity(args: argparse.Namespace) -> int:

    conn = connect_ro(resolve_db_path(args.db))
    try:
        report, mismatches = run_fidelity(conn, args.companyfacts_dir, golden=args.golden)
    finally:
        conn.close()
    if args.out:
        Path(args.out).write_text(report)
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(report)
    if args.csv and mismatches:
        with Path(args.csv).open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(mismatches[0]))
            writer.writeheader()
            writer.writerows(mismatches)
        print(f"wrote {args.csv}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:  # noqa: C901, PLR0911, PLR0912 - one dispatch branch per command
    args = build_parser().parse_args(argv)
    if args.command == "migrate":
        return run_migrate(args.db)
    if args.command == "coverage":
        return coverage_from_args(args)
    if args.command == "pending":
        return run_pending(
            resolve_db_path(args.db),
            Path(universe_database_path(args.universe_db)).expanduser(),
            resolve_analysis_date(args.analysis_date),
            metrics_only=args.metrics_only,
        )
    if args.command == "quality":
        return _run_quality(args)
    if args.command == "score":
        return _run_score(args)
    if args.command == "repair-accessions":
        return _run_repair(args)
    if args.command == "card":
        return _run_card(args)
    if args.command == "fidelity":
        return _run_fidelity(args)
    settings = Settings.load(metrics_only=args.metrics_only)
    updates: dict[str, object] = {}
    if args.db:
        updates["db_path"] = Path(args.db)
    if args.universe_db:
        updates["universe_db_path"] = Path(args.universe_db)
    if args.allow_dirty:
        updates["allow_dirty"] = True
    if updates:
        settings = settings.model_copy(update=updates)

    params = RunParams(
        forms=_split(args.forms) or list(DEFAULT_FORMS),
        since_year=args.since_year,
        until_year=args.until_year,
        limit=args.limit,
        tickers=_split(args.tickers),
        fresh=args.fresh,
        refresh_universe=args.refresh_universe,
        sections=args.sections,
        analysis_date=resolve_analysis_date(args.analysis_date),
        metrics_only=args.metrics_only,
    )
    report = run(settings, params)

    print(
        f"\nrun {report.run_id}: {report.completed} analyzed, "
        f"{report.skipped} skipped, {report.failed} failed "
        f"(of {report.planned} planned)"
    )
    for line in report.errors[:20]:
        print(f"  ! {line}", file=sys.stderr)
    # T-017: the step fails only when nothing completed and a failure may clear on a retry
    # (``failures.classify``). A run whose only failures are known data failures is a quiet run.
    return 1 if report.service_failed and not report.completed else 0


if __name__ == "__main__":
    raise SystemExit(main())
