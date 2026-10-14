"""T-071 part 1 verification and measurements (docs/model_fixes.md, T071a_plan.md P6).

Every number this task reports comes from here, with its output file under the task scratch ``out/``.
The database is read-only and is the task's own scratch copy
(``/workspaces/thesis/scratch/t071a/after.db``), recomputed with this branch by
``scripts/verify_t148b.py recompute``.

Sub-commands::

    uv run python scripts/verify_t071a.py changes --db DB --out out/changes.json
    uv run python scripts/verify_t071a.py unknown --db DB --out out/unknown.txt
    uv run python scripts/verify_t071a.py reit    --db DB --out out/reit_diff.txt
    uv run python scripts/verify_t071a.py golden  --out out/golden.txt
    uv run python scripts/verify_t071a.py pillars --db DB --out out/pillars.json
    uv run python scripts/verify_t071a.py sha1    --db DB --out out/sha1.txt
    uv run python scripts/verify_t071a.py all     --db DB --out-dir out/
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from cycle.config import CycleSettings
from fundamental_agent import db as fdb
from fundamental_agent.applicability import metric_negative_equity
from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.skills import GROUP_METRICS
from kg_schema import applicability
from kg_schema.company_profile import CompanyProfile, profile_as_of, profiles
from kg_schema.db import connect_ro

REPO = Path(__file__).resolve().parents[1]
GOLDEN_CSV = REPO / "docs" / "checklist_sec" / "golden_set.csv"
# The cycle date the plan names for the universe-coverage measurement (T-097/T-119's ranking date).
CYCLE_DATE = "2026-09-22"
PRODUCTION_DB = Path("/workspaces/thesis/data/financial.db")
UNIVERSE_DB = Path("/workspaces/thesis/data/universe.db")

# T-073's four pillars (the redesign the user set on 2026-10-13, recorded in TASKS.md): each pillar is
# the union of its groups' metrics, taken from the code's own GROUP_METRICS. "Moat" is dropped (no
# computed input), and valuation stays in the narrative, so it is in no pillar.
PILLARS: dict[str, tuple[str, ...]] = {
    "profitability": ("profitability", "roic", "efficiency"),
    "financial_strength": ("leverage", "liquidity"),
    "cash_flow": ("cashflow",),
    "growth": ("growth", "cagr"),
}

NOT_APPLICABLE_FLAG = "not_applicable"
ADJUSTED_FLAG = "adj"


def _pillar_metrics(pillar: str) -> tuple[str, ...]:
    return tuple(metric for group in PILLARS[pillar] for metric in GROUP_METRICS[group])


def _pillar_metric_set() -> dict[str, set[str]]:
    return {pillar: set(_pillar_metrics(pillar)) for pillar in PILLARS}


def _inputs(raw: str | None) -> dict[str, Any]:
    """The stored audit inputs; ``{}`` when the row carries none or they are not an object."""
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


class Stored:
    """The recomputed database, read once: filings, their stored metric rows and the profiles."""

    def __init__(self, db_path: str) -> None:
        self.conn = connect_ro(db_path)
        self.db_path = db_path
        self.engine = fdb.METRICS_ENGINE_VERSION
        self.assets = {
            int(r["id"]): {
                "ticker": r["ticker"],
                "company_name": r["company_name"],
                "cik": r["cik"],
                "sector": r["sector"],
                "sub_industry": r["sub_industry"],
            }
            for r in self.conn.execute(
                "SELECT a.id, a.ticker, a.company_name, a.cik, s.name AS sector, a.sub_industry "
                "FROM assets a LEFT JOIN sectors s ON a.sector_id = s.id"
            )
        }
        self.filings: dict[int, dict[str, Any]] = {
            int(r["id"]): {
                "asset_id": int(r["asset_id"]),
                "cik": r["cik"],
                "form": r["form"],
                "period_end": r["period_end"],
                "available_at": r["available_at"],
            }
            for r in self.conn.execute(
                "SELECT id, asset_id, cik, form, period_end, available_at FROM sec_filings"
            )
        }
        self.rows: dict[int, dict[str, tuple[float | None, dict[str, Any]]]] = defaultdict(dict)
        for r in self.conn.execute(
            "SELECT filing_id, metric_name, value, inputs_json FROM fundamental_metrics "
            "WHERE engine_version = ?",
            (self.engine,),
        ):
            self.rows[int(r["filing_id"])][r["metric_name"]] = (
                r["value"],
                _inputs(r["inputs_json"]),
            )
        self.profile_of: dict[int, CompanyProfile | None] = {}
        for filing_id, filing in self.filings.items():
            self.profile_of[filing_id] = profile_as_of(filing["cik"], filing["period_end"])
        # negative equity, as the metrics themselves evaluated it (APP-07): the flag on any row of
        # the filing. The overlay is not a profile field (T-071 part 1, P2).
        self.negative: dict[int, bool] = {
            filing_id: any(
                "negative_equity" in key for _v, inputs in rows.values() for key in inputs
            )
            for filing_id, rows in self.rows.items()
        }

    def negative_equity(self, filing_id: int, metric: str) -> bool:
        """APP-07's condition **for this metric**, exactly as the applying code evaluated it: the
        application's own :func:`fundamental_agent.applicability.metric_negative_equity` on the stored
        inputs. The overlay is not a profile field (T-071 part 1, P2)."""
        row = self.rows.get(filing_id, {}).get(metric)
        if row is None:
            return False
        value, inputs = row
        return metric_negative_equity(MetricResult(metric, value, "ratio", inputs))

    def code_rule(self, filing_id: int, metric: str) -> tuple[str, str]:
        """The cell the matrix resolves for *metric* on this filing's profile and negative equity."""
        profile = self.profile_of.get(filing_id)
        if profile is None:
            return ("A", "")
        return applicability.applicability(
            metric, profile, negative_equity=self.negative_equity(filing_id, metric)
        )

    def observed(self, filing_id: int, metric: str) -> tuple[str, str]:
        """``(code, rule)`` as the stored row records it: the NA/ADJ flag and its rule."""
        row = self.rows.get(filing_id, {}).get(metric)
        if row is None:
            return ("missing", "")
        _value, inputs = row
        if inputs.get(f"flag_{metric}_{NOT_APPLICABLE_FLAG}"):
            return ("NA", str(inputs.get("applicability_rule") or ""))
        if inputs.get(f"flag_{metric}_{ADJUSTED_FLAG}"):
            return ("ADJ", str(inputs.get("applicability_rule") or ""))
        return ("A", "")


def _each_row(stored: Stored, metrics: list[str]) -> Any:
    """Yield ``(filing_id, metric, observed, rule, resolved, columns)`` for every stored row.

    ``observed`` is what the row records (the NA/ADJ flag and its rule); ``resolved`` is the cell the
    matrix answers for that filing's profile and that metric's own negative-equity condition;
    ``columns`` are the columns that condition activates.
    """
    for filing_id in stored.rows:
        profile = stored.profile_of.get(filing_id)
        for metric in metrics:
            observed, rule = stored.observed(filing_id, metric)
            if observed == "missing" or profile is None:
                continue
            negative = stored.negative_equity(filing_id, metric)
            resolved = applicability.applicability(metric, profile, negative_equity=negative)
            columns = applicability.active_columns(profile, negative_equity=negative)
            yield filing_id, metric, observed, rule, resolved, columns


def _bucketed(stored: Stored, matrix: dict[str, Any]) -> tuple[Any, Any, Any]:
    """``(buckets, rules, activating)`` keyed by ``metric|column`` over the activating filings."""
    buckets: dict[str, Counter[str]] = defaultdict(Counter)
    rules: dict[str, Counter[str]] = defaultdict(Counter)
    activating: dict[str, set[int]] = defaultdict(set)
    for filing_id, metric, observed, rule, resolved, columns in _each_row(stored, sorted(matrix)):
        for column in columns:
            activating[column].add(filing_id)
            if matrix[metric].get(column, ("A", "")) != resolved:
                # the row resolves to another active column's cell (the precedence decides it), so it
                # belongs to that cell, not this one
                continue
            key = f"{metric}|{column}"
            if observed == "NA":
                buckets[key]["empty_na"] += 1
            elif observed == "ADJ":
                buckets[key]["flagged_adj"] += 1
            else:
                buckets[key]["unchanged"] += 1
            if rule:
                rules[key][rule] += 1
    return buckets, rules, activating


def _matrix_checks(stored: Stored, matrix: dict[str, Any]) -> tuple[list[dict[str, Any]], set[str]]:
    """Rows whose recorded cell is not the resolved one, and the non-A cells a filing reaches."""
    mismatches: list[dict[str, Any]] = []
    reached: set[str] = set()
    for filing_id, metric, observed, rule, resolved, columns in _each_row(stored, sorted(matrix)):
        if (observed, rule) != resolved:
            mismatches.append(
                {
                    "filing_id": filing_id,
                    "asset": stored.assets[stored.filings[filing_id]["asset_id"]]["ticker"],
                    "metric": metric,
                    "observed": [observed, rule],
                    "matrix": list(resolved),
                }
            )
        for column in columns:
            cell = matrix[metric].get(column, ("A", ""))
            if cell[0] != "A" and cell == resolved:
                reached.add(f"{metric}|{column}")
    return mismatches, reached


def _na_reason_violations(stored: Stored, matrix: dict[str, Any]) -> Counter[str]:
    """Rows whose matrix NA flag did **not** produce ``empty_reason: not_applicable``.

    T-071 part 1 fix 2 (G4's precedence: ``not_applicable`` first): a row the matrix makes NA must
    read that reason, never a denominator or a growth base of its own formula (IVZ's
    ``free_cash_flow_conversion`` carried both). The count must be **0**.
    """
    bad: Counter[str] = Counter()
    for filing_id in stored.rows:
        for metric in sorted(matrix):
            observed, _rule = stored.observed(filing_id, metric)
            if observed != "NA":
                continue
            row = stored.rows[filing_id][metric]
            reason = (row[1].get("provenance") or {}).get("empty_reason")
            if reason != NOT_APPLICABLE_FLAG:
                bad[metric] += 1
    return bad


def _per_metric_column(
    stored: Stored, matrix: dict[str, Any], buckets: Any, rules: Any, activating: Any
) -> dict[str, Any]:
    """The per-``metric|column`` report: the matrix's cell and the rows' split."""
    out: dict[str, Any] = {}
    for metric in sorted(matrix):
        for column in applicability.COLUMNS:
            code, rule = matrix[metric].get(column, ("A", ""))
            key = f"{metric}|{column}"
            filings = activating.get(column, set())
            out[key] = {
                "matrix": code,
                "rule": rule,
                "filings": len(filings),
                "companies": len({stored.filings[f]["asset_id"] for f in filings}),
                "empty_na": buckets[key]["empty_na"],
                "flagged_adj": buckets[key]["flagged_adj"],
                "unchanged": buckets[key]["unchanged"],
                "rules_observed": dict(rules[key]),
            }
    return out


def cmd_changes(args: argparse.Namespace) -> None:
    """Per metric x company-type column: rows emptied as NA, flagged ADJ or unchanged."""
    stored = Stored(args.db)
    matrix = {metric: applicability.cells(metric) for metric in applicability.metrics()}
    buckets, rules, activating = _bucketed(stored, matrix)
    mismatches, reached = _matrix_checks(stored, matrix)
    na_bad = _na_reason_violations(stored, matrix)
    per_metric_column = _per_metric_column(stored, matrix, buckets, rules, activating)

    cells_non_a = {
        f"{metric}|{column}"
        for metric in matrix
        for column, (code, _rule) in matrix[metric].items()
        if code != "A"
    }
    app_rules = {rule for cells in matrix.values() for code, rule in cells.values() if code != "A"}
    observed_rules: Counter[str] = Counter()
    for counter in rules.values():
        observed_rules.update(counter)
    rows_total = sum(len(rows) for rows in stored.rows.values())
    not_reached = sorted(cells_non_a - reached)
    # `earnings_yield` is VALORIZATION: `cycle` computes it (Annex B), so its cells are applied in
    # T-071 part 2. No other non-A cell may go unreached.
    non_fundamental = {"earnings_yield"}
    summary = {
        "db": args.db,
        "engine": stored.engine,
        "filings_with_rows": len(stored.rows),
        "companies": len({stored.filings[f]["asset_id"] for f in stored.rows}),
        "metric_rows": rows_total,
        "columns_activated": {
            column: len(filings) for column, filings in sorted(activating.items())
        },
        "per_metric_column": per_metric_column,
        "app_rules_in_the_matrix": sorted(app_rules),
        "app_rules_observed": dict(sorted(observed_rules.items())),
        "checks": {
            "rows_matching_the_matrix": rows_total - len(mismatches),
            "rows_not_matching": mismatches[:50],
            "cells_never_reached_by_a_filing": not_reached,
            "cells_never_reached_unexplained": sorted(
                cell for cell in not_reached if cell.split("|")[0] not in non_fundamental
            ),
            "app_rules_never_observed": sorted(app_rules - set(observed_rules)),
            "na_rows_with_another_empty_reason": sum(na_bad.values()),
            "na_rows_with_another_empty_reason_by_metric": dict(sorted(na_bad.items())),
        },
    }
    Path(args.out).write_text(json.dumps(summary, indent=2) + "\n")
    print(f"wrote {args.out}")
    print(f"  filings {summary['filings_with_rows']}, companies {summary['companies']}")
    print(f"  rows not matching the matrix: {len(mismatches)}")
    print(
        "  non-A cells never reached:"
        f" {summary['checks']['cells_never_reached_by_a_filing']}"
        f"; unexplained: {summary['checks']['cells_never_reached_unexplained']}"
    )
    print(f"  APP rules never observed: {summary['checks']['app_rules_never_observed']}")
    print(
        f"  NA rows whose empty_reason is not `not_applicable`:"
        f" {summary['checks']['na_rows_with_another_empty_reason']}"
        f" {summary['checks']['na_rows_with_another_empty_reason_by_metric']}"
    )


# -- unknown.txt -----------------------------------------------------------------------------------


def cmd_unknown(args: argparse.Namespace) -> None:
    """The CIKs with no profile: who they are, their filings, and why."""
    stored = Stored(args.db)
    known = profiles()
    unknown: dict[str, dict[str, Any]] = {}
    for asset in stored.assets.values():
        cik = asset["cik"]
        if cik and cik not in known:
            entry = unknown.setdefault(
                cik,
                {
                    "cik": cik,
                    "tickers": [],
                    "names": [],
                    "filings": 0,
                },
            )
            entry["tickers"].append(asset["ticker"])
            entry["names"].append(asset["company_name"])
    for filing in stored.filings.values():
        cik = filing["cik"]
        if cik in unknown:
            unknown[cik]["filings"] += 1
    total_filings = len(stored.filings)
    unknown_filings = sum(e["filings"] for e in unknown.values())
    share = unknown_filings / total_filings if total_filings else 0.0
    lines = [
        "# T-071 part 1 -- the CIKs with no company profile (P6)",
        f"# database: {args.db}",
        f"# profiles known: {len(known)}",
        f"# assets: {len(stored.assets)}; filings: {total_filings}",
        f"# unknown CIKs: {len(unknown)}; their filings: {unknown_filings}"
        f" ({share:.3%} of filings; the stop threshold is 2%)",
        "",
    ]
    if not unknown:
        lines.append(
            "None. Every asset's CIK has a profile: the profile holds one row per CIK of the "
            "checklist's company_types.csv (500 rows), and the pilot universe's 500 CIKs are "
            "exactly those rows."
        )
    for entry in sorted(unknown.values(), key=lambda e: e["cik"]):
        lines.append(
            f"{entry['cik']} {', '.join(entry['tickers'])} "
            f"({'; '.join(entry['names'])}) -- {entry['filings']} filings; "
            "reason: the CIK is not in the checklist's company-type table (company_types.csv)"
        )
    Path(args.out).write_text("\n".join(lines) + "\n")
    print(f"wrote {args.out} (unknown CIKs {len(unknown)}, filings {unknown_filings}, {share:.3%})")


# -- reit_diff.txt ---------------------------------------------------------------------------------


def _old_reit(sector: str | None, sub_industry: str | None) -> bool:
    """The GICS predicate `db.is_equity_reit` used before T-071 part 1."""
    return sector == "Real Estate" and (sub_industry or "").endswith("REITs")


def cmd_reit(args: argparse.Namespace) -> None:
    """The old GICS REIT predicate against the profile's ``reit`` overlay, every disagreement named."""
    stored = Stored(args.db)
    disagreements = []
    old_count = new_count = 0
    for _asset_id, asset in sorted(stored.assets.items(), key=lambda item: item[1]["ticker"]):
        profile = profile_as_of(asset["cik"]) if asset["cik"] else None
        old = _old_reit(asset["sector"], asset["sub_industry"])
        new = bool(profile and profile.reit)
        old_count += old
        new_count += new
        if old != new:
            disagreements.append(
                {
                    "ticker": asset["ticker"],
                    "cik": asset["cik"],
                    "sector": asset["sector"],
                    "sub_industry": asset["sub_industry"],
                    "old": old,
                    "profile": new,
                    "profile_type": profile.type if profile else None,
                }
            )
    lines = [
        "# T-071 part 1 -- the old GICS REIT predicate against the profile (P6, P4)",
        f"# database: {args.db}",
        f"# assets: {len(stored.assets)}",
        f"# old predicate (sector 'Real Estate' and a sub-industry ending in 'REITs'): {old_count}",
        f"# profile `reit` overlay: {new_count}",
        f"# disagreements: {len(disagreements)}",
        "",
    ]
    if not disagreements:
        lines.append(
            "None. Every asset's two answers agree, so routing `db.is_equity_reit` through the "
            "profile changes no company's REIT status (the handoff's stop condition is met)."
        )
    for d in disagreements:
        lines.append(
            f"{d['ticker']} {d['cik']}: sector {d['sector']!r} sub-industry {d['sub_industry']!r} "
            f"-> old {d['old']}, profile {d['profile']} (type {d['profile_type']})"
        )
    Path(args.out).write_text("\n".join(lines) + "\n")
    print(
        f"wrote {args.out} (old {old_count}, profile {new_count}, disagreements {len(disagreements)})"
    )


# -- golden.txt ------------------------------------------------------------------------------------


def _golden_rows() -> list[dict[str, str]]:
    return list(csv.DictReader(GOLDEN_CSV.open()))


def _profile_overlays(profile: CompanyProfile) -> set[str]:
    """The profile's overlays, as the golden set names them."""
    names = set()
    if profile.reit:
        names.add("reit")
    if profile.multi_class:
        names.add("multi-class")
    if profile.multi_sector:
        names.add("multi-sector")
    if profile.regulated_utility:
        names.add("regulated utility")
    return names


# Overlays the golden set and the profile name differently, so neither is compared:
# * the golden set's `negative equity` is evaluated from the filing (APP-07), never a profile field;
# * `multi-sector` is the profile's own base type (multi_sector_holding), which the golden set carries
#   in its `type` column instead;
# * `regulated utility` is the profile's D-13 GICS proxy, which the golden set has no column for.
_NOT_COMPARABLE = {"regulated utility", "multi-sector"}


def _golden_line(
    ticker: str, row: dict[str, str], profile: CompanyProfile | None
) -> tuple[bool, str]:
    """``(agrees, line)`` for one golden-set row."""
    golden_type = row["type"]
    golden_overlays = {
        part.strip().lower() for part in (row["overlays"] or "").split(",") if part.strip()
    } - {"none"}
    if profile is None:
        return (
            False,
            f"{ticker}: NO PROFILE (golden type {golden_type}, overlays {golden_overlays})",
        )
    overlays = _profile_overlays(profile)
    comparable = overlays - _NOT_COMPARABLE
    type_ok = profile.type == golden_type
    overlays_ok = golden_overlays - {"negative equity"} == comparable
    status = "AGREE" if (type_ok and overlays_ok) else "DIFFER"
    return type_ok and overlays_ok, (
        f"{ticker:6s} {status:6s} type golden={golden_type} profile={profile.type}"
        f" ({'ok' if type_ok else 'MISMATCH'}); overlays golden={sorted(golden_overlays)}"
        f" profile={sorted(overlays)} (comparable {sorted(comparable)},"
        f" {'ok' if overlays_ok else 'MISMATCH'})"
    )


def cmd_golden(args: argparse.Namespace) -> None:
    """The profile against golden_set.csv's ``type`` / ``overlays`` columns for its 23 tickers."""
    known = profiles()
    by_ticker: dict[str, CompanyProfile] = {}
    for entry in known.values():
        # the profile's `tickers` cell lists an issuer's classes ("GOOG/GOOGL"): any of them names it
        for ticker in entry.ticker.replace("/", ",").split(","):
            if ticker.strip():
                by_ticker[ticker.strip()] = entry
    lines = [
        "# T-071 part 1 -- the profile against docs/checklist_sec/golden_set.csv (P6)",
        "# the golden set holds line choices, not values; only its `type` and `overlays` columns are",
        "# comparable with the profile.",
        "",
    ]
    rows = _golden_rows()
    compared = [_golden_line(row["ticker"], row, by_ticker.get(row["ticker"])) for row in rows]
    lines.extend(line for _agrees, line in compared)
    disagreements = sum(1 for agrees, _line in compared if not agrees)
    lines += [
        "",
        f"# golden tickers: {len(rows)}; disagreements: {disagreements}",
        "# Overlays not compared, with the reason: `negative equity` (the golden set's) is evaluated",
        "# from the filing, APP-07, and is never a profile field; `multi-sector` is the profile's own",
        "# base type (multi_sector_holding), which the golden set carries in its `type` column; and",
        "# `regulated utility` is the profile's D-13 GICS proxy, which the golden set has no column for.",
    ]
    Path(args.out).write_text("\n".join(lines) + "\n")
    print(f"wrote {args.out} (disagreements {disagreements})")


# -- pillars.json ----------------------------------------------------------------------------------


def _empty_pillar(stored: Stored, filing_id: int, metrics: set[str]) -> bool:
    """A pillar has no reading for this filing: none of its metrics has a value."""
    rows = stored.rows.get(filing_id, {})
    return not any(
        rows.get(metric) is not None and rows[metric][0] is not None for metric in metrics
    )


def _applicable_pillar(stored: Stored, filing_id: int, metrics: set[str]) -> bool:
    """The pillar is applicable: at least one of its metrics is not NA for this company type."""
    return any(stored.code_rule(filing_id, metric)[0] != "NA" for metric in metrics)


def _pillar_counts(
    stored: Stored,
) -> tuple[dict[str, Any], dict[str, dict[str, int]], int, int, int, int]:
    """Filings with an empty pillar, counting every pillar and then only an applicable one."""
    pillar_metrics = _pillar_metric_set()
    before = after = before_only = after_only = 0
    per_pillar: dict[str, dict[str, int]] = {
        pillar: {"empty_before": 0, "empty_after": 0} for pillar in PILLARS
    }
    for filing_id in stored.rows:
        any_before = any_after = False
        for pillar, metrics in pillar_metrics.items():
            if not _empty_pillar(stored, filing_id, metrics):
                continue
            per_pillar[pillar]["empty_before"] += 1
            any_before = True
            if _applicable_pillar(stored, filing_id, metrics):
                per_pillar[pillar]["empty_after"] += 1
                any_after = True
        before += any_before
        after += any_after
        before_only += any_before and not any_after
        after_only += any_after and not any_before
    return pillar_metrics, per_pillar, before, after, before_only, after_only


# The building blocks that are **not ratios**: `ebitda`, `nopat`, `effective_tax_rate` and the
# robustness ROIC variant are inputs/derivations beside the ratios, not a reading of the business, so
# the strict rule is reported with the current pillar lists **and** without them (fix 1, reviewer A).
NON_RATIO_METRICS = frozenset(
    {
        "ebitda",
        "nopat",
        "effective_tax_rate",
        "return_on_invested_capital_effective_rate",
    }
)


def _latest_filings(stored: Stored) -> dict[int, int]:
    """Each asset's latest filing available by :data:`CYCLE_DATE` (PER-01: ``available_at`` <= it)."""
    latest: dict[int, int] = {}
    for filing_id, filing in stored.filings.items():
        available = filing["available_at"]
        if not available or available > CYCLE_DATE:
            continue
        asset_id = filing["asset_id"]
        current = latest.get(asset_id)
        if current is None or (stored.filings[current]["available_at"], current) < (
            available,
            filing_id,
        ):
            latest[asset_id] = filing_id
    return latest


def _metric_reason(stored: Stored, filing_id: int, metric: str) -> str:
    """Why *metric* has no value on this filing, as the stored provenance records it."""
    row = stored.rows.get(filing_id, {}).get(metric)
    if row is None:
        return "not recorded"
    value, inputs = row
    if value is not None:
        return "has a value"
    reason = (inputs.get("provenance") or {}).get("empty_reason")
    return str(reason or "not recorded")


def _empty_pillars(
    stored: Stored, filing_id: int, pillar_metrics: dict[str, set[str]]
) -> dict[str, dict[str, str]]:
    """This filing's **applicable** pillars with no value, and every metric's own empty reason.

    The strict rule (MET-00): an empty metric is never a neutral reading, so a filing with **any**
    applicable pillar empty has no fundamental score. A metric the matrix makes NA reads
    ``not_applicable``; a metric with no row at all reads ``not recorded``.
    """
    out: dict[str, dict[str, str]] = {}
    for pillar, metrics in pillar_metrics.items():
        if _applicable_pillar(stored, filing_id, metrics) and _empty_pillar(
            stored, filing_id, metrics
        ):
            out[pillar] = {
                metric: _metric_reason(stored, filing_id, metric) for metric in sorted(metrics)
            }
    return out


def _unscored_members(
    stored: Stored, pillar_metrics: dict[str, set[str]], latest: dict[int, int]
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """The members with no fundamental score under the strict rule, and the count per empty pillar."""
    members: list[dict[str, Any]] = []
    by_pillar: Counter[str] = Counter()
    for asset_id, asset in sorted(stored.assets.items(), key=lambda item: item[1]["ticker"]):
        filing_id = latest.get(asset_id)
        if filing_id is None:
            members.append({"ticker": asset["ticker"], "reason": "no filing available by the date"})
            continue
        empty = _empty_pillars(stored, filing_id, pillar_metrics)
        if not empty:
            continue
        by_pillar.update(empty.keys())
        profile = stored.profile_of.get(filing_id)
        members.append(
            {
                "ticker": asset["ticker"],
                "company_name": asset["company_name"],
                "filing_id": filing_id,
                "form": stored.filings[filing_id]["form"],
                "period_end": stored.filings[filing_id]["period_end"],
                "profile": profile.type if profile else None,
                "empty_pillars": empty,
            }
        )
    return members, by_pillar


def _universe_coverage(stored: Stored, pillar_metrics: dict[str, set[str]]) -> dict[str, Any]:
    """The strict coverage rule at :data:`CYCLE_DATE`, with the current pillars and without the
    building blocks that are not ratios, against ``cycle``'s ``unscored_max_share``."""
    latest = _latest_filings(stored)
    without_non_ratios = {
        pillar: {metric for metric in metrics if metric not in NON_RATIO_METRICS}
        for pillar, metrics in pillar_metrics.items()
    }
    max_share = float(CycleSettings.model_fields["unscored_max_share"].default)
    report: dict[str, Any] = {}
    for key, metrics in (
        ("with_the_current_pillars", pillar_metrics),
        ("without_the_non_ratio_building_blocks", without_non_ratios),
    ):
        members, by_pillar = _unscored_members(stored, metrics, latest)
        share = len(members) / len(stored.assets) if stored.assets else 0.0
        report[key] = {
            "members_without_a_score": len(members),
            "share": share,
            "cycle_unscored_max_share": max_share,
            "exceeds_cycle_limit": share > max_share,
            "by_empty_pillar": dict(sorted(by_pillar.items())),
            "members": members,
        }
        if key == "without_the_non_ratio_building_blocks":
            report[key]["excluded_metrics"] = sorted(NON_RATIO_METRICS)
    return {
        "cycle_date": CYCLE_DATE,
        "members": len(stored.assets),
        "members_with_a_filing": len(latest),
        "strict_rule": (
            "no fundamental score when ANY applicable pillar has no value (MET-00: an empty metric is "
            "never a neutral reading); the members and each empty pillar's metrics' empty reasons below"
        ),
        "cycle_unscored_max_share": max_share,
        **report,
    }


def _control_transition(
    stored: Stored, control_db: str | None, pillar_metrics: dict[str, set[str]]
) -> dict[str, Any] | None:
    """The control's side of the pillar measure, per pillar (T-071 part 1, P6).

    The control is the same scratch database **before** the recompute with this branch
    (``t151c/after.db``): for every filing whose pillar is applicable and empty here, a targeted query
    asks whether that pillar held a **value** there. Every hit is a filing the matrix emptied, and one
    example per pillar shows the metrics on both sides.
    """
    if not control_db:
        return None
    control = connect_ro(control_db)
    hits: dict[str, list[int]] = {pillar: [] for pillar in PILLARS}
    for filing_id in stored.rows:
        for pillar, metrics in pillar_metrics.items():
            if not (
                _applicable_pillar(stored, filing_id, metrics)
                and _empty_pillar(stored, filing_id, metrics)
            ):
                continue
            values = {
                row["metric_name"]: row["value"]
                for row in control.execute(
                    "SELECT metric_name, value FROM fundamental_metrics "
                    "WHERE filing_id = ? AND engine_version = ?",
                    (filing_id, stored.engine),
                )
                if row["metric_name"] in metrics
            }
            if any(value is not None for value in values.values()):
                hits[pillar].append(filing_id)
    out: dict[str, Any] = {"control_db": control_db}
    for pillar, filing_ids in hits.items():
        profiles: Counter[str | None] = Counter()
        for fid in filing_ids:
            profile = stored.profile_of.get(fid)
            profiles[profile.type if profile is not None else None] += 1
        entry: dict[str, Any] = {
            "filings_emptied_by_this_branch": len(filing_ids),
            "profile_types": dict(sorted(profiles.items(), key=lambda item: str(item[0]))),
        }
        if filing_ids:
            filing_id = filing_ids[0]
            asset = stored.assets[stored.filings[filing_id]["asset_id"]]
            profile = stored.profile_of.get(filing_id)
            controls = {
                row["metric_name"]: row["value"]
                for row in control.execute(
                    "SELECT metric_name, value FROM fundamental_metrics WHERE filing_id = ? "
                    "AND engine_version = ?",
                    (filing_id, stored.engine),
                )
            }
            entry["example"] = {
                "filing_id": filing_id,
                "ticker": asset["ticker"],
                "company_name": asset["company_name"],
                "form": stored.filings[filing_id]["form"],
                "period_end": stored.filings[filing_id]["period_end"],
                "profile": profile.type if profile else None,
                "metrics": {
                    metric: {
                        "control": controls.get(metric),
                        "branch": (stored.rows.get(filing_id, {}).get(metric) or (None,))[0],
                        "cell": list(stored.code_rule(filing_id, metric)),
                    }
                    for metric in sorted(pillar_metrics[pillar])
                },
            }
        out[pillar] = entry
    return out


def cmd_pillars(args: argparse.Namespace) -> None:
    """T-073's input: the filings with an empty applicable pillar, and the universe coverage."""
    stored = Stored(args.db)
    pillar_metrics, per_pillar, before, after, before_only, after_only = _pillar_counts(stored)
    coverage = _universe_coverage(stored, pillar_metrics)
    control = _control_transition(stored, args.control, pillar_metrics)
    summary = {
        "db": args.db,
        "engine": stored.engine,
        "pillars": {pillar: list(_pillar_metrics(pillar)) for pillar in PILLARS},
        "filings_with_rows": len(stored.rows),
        "before_counting_every_empty_pillar": before,
        "after_only_an_applicable_pillar": after,
        "per_pillar": per_pillar,
        "control": control,
        "filings_only_before": before_only,
        "filings_only_after": after_only,
        "universe": coverage,
    }
    Path(args.out).write_text(json.dumps(summary, indent=2) + "\n")
    print(f"wrote {args.out}")
    print(
        f"  empty pillar: before {before}, after {after}"
        f" (before-only {before_only}, after-only {after_only})"
    )
    for key in ("with_the_current_pillars", "without_the_non_ratio_building_blocks"):
        entry = coverage[key]
        print(
            f"  universe at {CYCLE_DATE}, {key}: {entry['members_without_a_score']} of "
            f"{coverage['members']} with no score ({entry['share']:.1%}; cycle limit "
            f"{entry['cycle_unscored_max_share']:.0%}; over: {entry['exceeds_cycle_limit']}); "
            f"by empty pillar {entry['by_empty_pillar']}"
        )
    if control:
        for pillar in PILLARS:
            print(
                f"  control {pillar}: {control[pillar]['filings_emptied_by_this_branch']} filings"
                f" {control[pillar]['profile_types']}"
            )


# -- sha1.txt --------------------------------------------------------------------------------------


def _sha1(path: Path) -> str:
    """A file fingerprint, not a security boundary (the same use as ``verify_t149``)."""
    digest = hashlib.sha1()  # noqa: S324 - a file fingerprint, not a security boundary
    digest.update(path.read_bytes())
    return digest.hexdigest()


def cmd_sha1(args: argparse.Namespace) -> None:
    """The production and pilot sha1s, and the scratch copy's."""
    lines = ["# T-071 part 1 -- sha1 checks (never written; read-only throughout)"]
    for label, path in (
        ("financial.db (READ-ONLY)", PRODUCTION_DB),
        ("universe.db (READ-ONLY)", UNIVERSE_DB),
    ):
        lines.append(f"{_sha1(path)}  {path}  # {label}")
    scratch = Path(args.db)
    lines.append(f"{_sha1(scratch)}  {scratch}  # task scratch copy")
    Path(args.out).write_text("\n".join(lines) + "\n")
    print(f"wrote {args.out}")


# -- all -------------------------------------------------------------------------------------------


def cmd_all(args: argparse.Namespace) -> None:
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, handler in (
        ("changes.json", cmd_changes),
        ("unknown.txt", cmd_unknown),
        ("reit_diff.txt", cmd_reit),
        ("golden.txt", cmd_golden),
        ("pillars.json", cmd_pillars),
        ("sha1.txt", cmd_sha1),
    ):
        extra = {"control": args.control} if name == "pillars.json" else {}
        handler(argparse.Namespace(db=args.db, out=str(out / name), **extra))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, handler in (
        ("changes", cmd_changes),
        ("unknown", cmd_unknown),
        ("reit", cmd_reit),
        ("golden", cmd_golden),
        ("pillars", cmd_pillars),
        ("sha1", cmd_sha1),
    ):
        sub = subparsers.add_parser(name, help=handler.__doc__)
        if name != "golden":
            sub.add_argument("--db", required=True, help="the task's scratch copy (read-only)")
        if name == "pillars":
            sub.add_argument(
                "--control",
                default=None,
                help="the same database before the recompute (t151c/after.db), for the control",
            )
        sub.add_argument("--out", required=True)
        sub.set_defaults(func=handler)
    p_all = subparsers.add_parser("all", help="every measurement, into --out-dir")
    p_all.add_argument("--db", required=True)
    p_all.add_argument(
        "--control",
        default="/workspaces/thesis/scratch/t151c/after.db",
        help="the same database before the recompute, for the pillar control",
    )
    p_all.add_argument("--out-dir", required=True)
    p_all.set_defaults(func=cmd_all)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
