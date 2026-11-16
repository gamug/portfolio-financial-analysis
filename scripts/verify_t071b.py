"""T-071 part 2a measurements (docs/model_fixes.md "T-071 part 2a"; T071b_plan.md, Measurement).

Every number this task reports comes from here, with its output file under the task scratch ``out/``.
The databases are the task's own scratch copies -- ``t071b/before.db`` (the copy as it was: gate
version ``dq-v3``, no rule input) and ``t071b/after.db`` (recomputed with this branch by
``scripts/verify_t148b.py recompute``: gate version ``dq-v4`` and the D-13 rule input). Nothing here
writes a database, and nothing is read from production.

The rules are evaluated with ``cycle``'s own readers (``cycle.data.latest_metrics``,
``cycle.data.data_quality``, ``cycle.data.asset_profiles``, ``cycle.data.utility_cash_coverage``),
so the measurement sees exactly what a cycle run would. The two sides differ only in their database:
the *before* side reads the earlier gate version through the same reader
(``data_quality(..., gate_version=...)``), which is what makes the gate-methodology change visible.

Sub-commands::

    uv run python scripts/verify_t071b.py vetoes     --before B --after A --out out/vetoes_by_sector.json
    uv run python scripts/verify_t071b.py neg-equity --before B --after A --out out/neg_equity.json
    uv run python scripts/verify_t071b.py d04        --before B --out out/d04_sensitivity.json
    uv run python scripts/verify_t071b.py d13        --db A --out out/d13_utilities.json
    uv run python scripts/verify_t071b.py d14        --before B --after A --out out/d14_reits.json
    uv run python scripts/verify_t071b.py liquidity  --before B --after A --out out/liquidity.json
    uv run python scripts/verify_t071b.py debt       --db B --out out/r5_debt.json
    uv run python scripts/verify_t071b.py all        --before B --after A --out-dir out/
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from cycle import data
from cycle.rules.base import RuleContext, VetoHit
from cycle.rules.builtin import RULES, _LeverageRule
from kg_schema import versions as kg_versions
from kg_schema.db import connect_ro

DATA_QUALITY_GATE_VERSION = kg_versions.DATA_QUALITY_GATE_VERSION
resolve_metric_versions = kg_versions.resolve_metric_versions
# The D-13 rule-input contract (T-071 part 2a). Absent on master, which the R7 control runs, where
# the rule input and its rule do not exist -- the fallbacks are the stored names themselves.
UTILITY_COVERAGE_BOUND_IN = getattr(
    kg_versions, "UTILITY_COVERAGE_BOUND_IN", "average_ambiguous_in"
)
UTILITY_COVERAGE_BOUND_AS_WC = getattr(
    kg_versions, "UTILITY_COVERAGE_BOUND_AS_WC", "average_ambiguous_as_wc"
)
UTILITY_COVERAGE_MISSING_BOUND_FLAG = getattr(
    kg_versions, "UTILITY_COVERAGE_MISSING_BOUND_FLAG", "flag_utility_cash_coverage_missing_bound"
)
UTILITY_COVERAGE_AMBIGUOUS_FLAG = getattr(
    kg_versions,
    "UTILITY_COVERAGE_AMBIGUOUS_FLAG",
    "flag_utility_cash_coverage_ambiguous_working_capital",
)
UTILITY_COVERAGE_THRESHOLD = getattr(kg_versions, "UTILITY_COVERAGE_THRESHOLD", 0.05)

UNIVERSE = "SP500"
UNIVERSE_DB = "/workspaces/thesis/data/universe_history.db"
CYCLE_DATE = "2026-09-22"
FIRST_QUARTER = "2023-03-31"
MONTHS_IN_YEAR = 12
LEVERAGE_DTE_THRESHOLD = 3.0  # T-116's positive-equity threshold
# R5's "material" test: an understated share above 5% of debt for more than 20 companies.
MATERIAL_COMPANIES = 20
MATERIAL_SHARE = 0.05
CURRENT_MATURITY_CONCEPT = "us-gaap_LongTermDebtAndCapitalLeaseObligationsCurrent"
LONG_TERM_DEBT_CURRENT = "us-gaap_LongTermDebtCurrent"
NONCURRENT_LONG_TERM = "us-gaap_LongTermDebtNoncurrent"
BEFORE_GATE = "dq-v3"  # the copy as it was (T-071 part 2a's D1 bumps it to dq-v4)
AFTER_GATE = DATA_QUALITY_GATE_VERSION
# The rules whose exclusions the plan reports by sector (the price rules need observations, not
# fundamentals, and are out of this task's scope).
REPORTED_RULES = (
    "LEVERAGE_EXTREME",
    "NEGATIVE_FCF",
    "UTILITY_CASH_COVERAGE",
    "LIQUIDITY_DISTRESS",
    "DATA_QUALITY",
)
# The GICS sectors the old ``LIQUIDITY_EXEMPT_SECTORS`` named (D5's *before* predicate, re-stated
# here because the constant no longer exists in the code -- it is the control's definition).
OLD_LIQUIDITY_EXEMPT_SECTORS = frozenset({"Financials", "Utilities"})
# APP-04b: the real-estate acquisition lines the REIT capex includes.
REIT_ACQUISITION_CONCEPTS = (
    "us-gaap_PaymentsToAcquireRealEstate",
    "us-gaap_PaymentsToAcquireRealEstateHeldForInvestment",
)


def _quarter_ends(first: str, last: str) -> list[str]:
    """Every calendar quarter end from *first* to *last*, inclusive."""
    out = []
    year, month = int(first[:4]), int(first[5:7])
    end_year, end_month = int(last[:4]), int(last[5:7])
    while (year, month) <= (end_year, end_month):
        day = 31 if month in (3, 12) else 30
        out.append(f"{year:04d}-{month:02d}-{day}")
        month += 3
        if month > MONTHS_IN_YEAR:
            year, month = year + 1, month - MONTHS_IN_YEAR
    return out


def _json_object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _data_quality(conn: Any, date: str, versions: Any, gate_version: str) -> Any:
    """``data.data_quality`` with the gate version when the reader accepts one (this branch), else
    the code's own default (the R7 control, whose default is the gate version ``before.db`` holds)."""
    try:
        return data.data_quality(conn, date, versions, gate_version=gate_version)
    except TypeError:
        return data.data_quality(conn, date, versions)


class Snapshot:
    """One database read through ``cycle``'s readers at one date.

    It builds ``RuleContext`` from ``dataclasses.fields`` and tolerates the readers this branch adds
    (``asset_profiles``, ``utility_cash_coverage``, the ``gate_version`` parameter), so the *same*
    script also runs against master's code -- the R7 control. On master those readers are absent and
    the gate version is the code's own (``dq-v3``, the one ``before.db`` holds)."""

    def __init__(self, db: str, date: str, gate_version: str) -> None:
        self.db, self.date, self.gate_version = db, date, gate_version
        self.conn = connect_ro(db)
        self.versions = resolve_metric_versions(self.conn)
        self.dq = _data_quality(self.conn, date, self.versions, gate_version)
        self.raw_metrics = data.latest_metrics(self.conn, date, self.versions)
        self.metrics = self.dq.apply(self.raw_metrics)
        rows = data.active_universe(self.conn, UNIVERSE, date, UNIVERSE_DB)
        self.asset_ids = [int(r["id"]) for r in rows]
        self.ticker = {int(r["id"]): str(r["ticker"]) for r in rows}
        self.sector = {int(r["id"]): r["sector"] for r in rows}
        self.profiles = (
            data.asset_profiles(self.conn, self.asset_ids, date)
            if hasattr(data, "asset_profiles")
            else {}
        )
        self.coverage = (
            data.utility_cash_coverage(self.conn, date)
            if hasattr(data, "utility_cash_coverage")
            else {}
        )
        raw_neg = getattr(self.dq, "negative_equity", None)
        # This branch's DataQuality.negative_equity is the DQ_NEG_EQUITY evidence per asset; master's
        # is a plain set of asset ids, which its rules never read.
        self.negative_equity: dict[int, dict[str, Any]] = (
            raw_neg if isinstance(raw_neg, dict) else {}
        )
        values: dict[str, Any] = {
            "cycle_date": date,
            "metrics": {a: self.metrics.get(a, {}) for a in self.asset_ids},
            "price_obs": {},
            "last_fundamental": data.last_fundamental_dates(self.conn, date),
            "data_quality": {a: self.dq.hard[a] for a in self.asset_ids if a in self.dq.hard},
            "sectors": {a: self.sector.get(a) for a in self.asset_ids},
            "negative_equity": {
                a: self.negative_equity[a] for a in self.asset_ids if a in self.negative_equity
            },
            "profiles": self.profiles,
            "utility_cash_coverage": self.coverage,
        }
        accepted = {f.name for f in dataclasses.fields(RuleContext)}
        self.ctx = RuleContext(**{k: v for k, v in values.items() if k in accepted})

    def members_with_a_usable_filing(self) -> int:
        return sum(1 for a in self.asset_ids if self.raw_metrics.get(a))

    def hits(self) -> dict[str, list[VetoHit]]:
        """rule_id -> its hits this date (every rule, so a rule with none is an empty list)."""
        return {rule.RULE_ID: list(rule.evaluate(self.ctx)) for rule in RULES}

    def evaluated(self) -> dict[str, frozenset[int]]:
        return {rule.RULE_ID: rule.evaluate(self.ctx).evaluated for rule in RULES}

    def hard_by_rule_sector(self) -> dict[str, dict[str, list[str]]]:
        """rule_id -> sector -> the HARD-hit tickers, for :data:`REPORTED_RULES`."""
        out: dict[str, dict[str, list[str]]] = {rid: defaultdict(list) for rid in REPORTED_RULES}  # type: ignore[assignment]
        for rid, hits in self.hits().items():
            if rid not in REPORTED_RULES:
                continue
            for h in hits:
                if h.severity == "HARD":
                    out[rid][self.sector.get(h.asset_id) or "unknown"].append(
                        self.ticker.get(h.asset_id, str(h.asset_id))
                    )
        return {
            rid: {s: sorted(t) for s, t in sorted(by_sector.items())}
            for rid, by_sector in out.items()
        }


def _sector_counts(hard: dict[str, dict[str, list[str]]]) -> dict[str, Any]:
    return {
        rid: {"companies": sum(len(t) for t in by_sector.values()), "by_sector": by_sector}
        for rid, by_sector in hard.items()
    }


# -- vetoes by sector --------------------------------------------------------------------------


def cmd_vetoes(args: argparse.Namespace) -> None:
    """Per rule x sector x date, before -> after (plan, "Measurement").

    Two modes:

    * ``--side before --db B`` -- evaluate one side and write its per-date, per-rule, per-sector
      result. Run this **with master's code on the path** (``PYTHONPATH`` = the control worktree's
      ``src``) to get R7's control: the *old* rules on ``before.db``.
    * ``--after A --before-json control.json`` -- evaluate the branch's rules on ``after.db`` and pair
      them with the control's output, writing the combined ``vetoes_by_sector.json``. A plain
      ``--before B`` (no ``--before-json``) evaluates both sides with this branch's code, which shows
      only the database change, not a code-level one.
    """
    dates = [CYCLE_DATE] + [d for d in _quarter_ends(FIRST_QUARTER, CYCLE_DATE) if d != CYCLE_DATE]
    if args.side == "before":
        control: dict[str, Any] = {
            "db": args.db,
            "gate": BEFORE_GATE,
            "per_date": {
                date: {
                    "members_with_a_usable_filing": (
                        snap := Snapshot(args.db, date, BEFORE_GATE)
                    ).members_with_a_usable_filing(),
                    "rules": _sector_counts(snap.hard_by_rule_sector()),
                }
                for date in dates
            },
        }
        Path(args.out).write_text(json.dumps(control, indent=2) + "\n")
        print(f"wrote {args.out} (before side, {len(dates)} dates)")
        return
    before_json = getattr(args, "before_json", None)
    loaded = json.loads(Path(before_json).read_text()) if before_json else None
    report: dict[str, Any] = {
        "universe": UNIVERSE,
        "rules": list(REPORTED_RULES),
        "before_gate": BEFORE_GATE,
        "after_gate": AFTER_GATE,
        "before_source": before_json or args.before,
        "per_date": {},
    }
    for date in dates:
        after = Snapshot(args.after, date, AFTER_GATE)
        if loaded is not None:
            entry = loaded["per_date"][date]
            before_rules, before_usable = entry["rules"], entry["members_with_a_usable_filing"]
        else:
            before = Snapshot(args.before, date, BEFORE_GATE)
            before_rules = _sector_counts(before.hard_by_rule_sector())
            before_usable = before.members_with_a_usable_filing()
        report["per_date"][date] = {
            "members": len(after.asset_ids),
            "members_with_a_usable_filing": after.members_with_a_usable_filing(),
            "before_members_with_a_usable_filing": before_usable,
            "before": before_rules,
            "after": _sector_counts(after.hard_by_rule_sector()),
        }
        print(f"{date}: after {report['per_date'][date]['after']}", flush=True)
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {args.out}")
    at = report["per_date"][CYCLE_DATE]
    for rid in REPORTED_RULES:
        print(
            f"  {rid:22} {CYCLE_DATE}: {at['before'][rid]['companies']:>3} -> "
            f"{at['after'][rid]['companies']:>3}"
        )


# -- D1/D2: negative equity --------------------------------------------------------------------


def _roe_quarantine_rows(conn: Any) -> list[dict[str, Any]]:
    """The filings where ``DQ_NEG_EQUITY`` quarantined ROE, with the ROE row and its inputs.

    D1 (D-04): ROE's own APP-07 test runs on the **prior-year** equity it divides by
    (``metrics/profitability.py``), so the gate must not quarantine it a second time. This is the
    plan's "those 7 filings keep their ROE" (TASKS.md's 2026-10-13 note), measured filing by filing:
    every filing whose DQ_NEG_EQUITY row names ``return_on_equity``, on either gate version."""
    rows = conn.execute(
        """
        SELECT d.filing_id, d.gate_version, d.quarantined, f.accession_number, f.period_end,
               a.ticker, m.value AS roe, m.inputs_json
        FROM data_quality_issue d
        JOIN sec_filings f ON f.id = d.filing_id
        JOIN assets a ON a.id = f.asset_id
        LEFT JOIN fundamental_metrics m ON m.filing_id = d.filing_id
             AND m.metric_name = 'return_on_equity'
        WHERE d.rule_id = 'DQ_NEG_EQUITY' AND d.metric_name = 'return_on_equity'
        ORDER BY a.ticker, f.period_end, d.gate_version
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        inputs = _json_object(r["inputs_json"])
        out.append(
            {
                "ticker": r["ticker"],
                "accession": r["accession_number"],
                "period_end": r["period_end"],
                "gate_version": r["gate_version"],
                "quarantined": bool(r["quarantined"]),
                "roe": r["roe"],
                "prior_year_equity": inputs.get("equity")
                or inputs.get("equity_prior_year")
                or inputs.get("average_equity"),
                "roe_inputs": inputs,
            }
        )
    return out


def _roe_regained(after_conn: Any, before_conn: Any) -> list[dict[str, Any]]:
    """R6: the DQ_NEG_EQUITY filings whose ROE has a value now, with the equity it divides by.

    ``before`` is the same filing's ROE on the copy as it was: the matrix emptied it from the
    current equity, so it is ``None`` there."""
    rows = after_conn.execute(
        """
        SELECT f.id, f.accession_number, f.form, f.period_end, a.ticker, m.value, m.inputs_json
        FROM fundamental_metrics m
        JOIN sec_filings f ON f.id = m.filing_id
        JOIN assets a ON a.id = f.asset_id
        WHERE m.metric_name = 'return_on_equity' AND m.value IS NOT NULL
          AND f.id IN (SELECT filing_id FROM data_quality_issue WHERE rule_id = 'DQ_NEG_EQUITY')
        ORDER BY a.ticker, f.period_end
        """
    ).fetchall()
    out = []
    for r in rows:
        inputs = _json_object(r["inputs_json"])
        before = before_conn.execute(
            "SELECT value FROM fundamental_metrics WHERE filing_id = ? "
            "AND metric_name = 'return_on_equity'",
            (int(r["id"]),),
        ).fetchone()
        out.append(
            {
                "ticker": r["ticker"],
                "form": r["form"],
                "period_end": r["period_end"],
                "accession": r["accession_number"],
                "equity": inputs.get("equity"),
                "prior_year_equity": inputs.get("prior_equity"),
                "roe": r["value"],
                "roe_before": before["value"] if before else None,
            }
        )
    return out


def cmd_neg_equity(args: argparse.Namespace) -> None:
    """D1/D2's companies and inputs; the ROE rows D1 stops quarantining (D-04)."""
    before = Snapshot(args.before, CYCLE_DATE, BEFORE_GATE)
    after = Snapshot(args.after, CYCLE_DATE, AFTER_GATE)
    # D1: the DQ_NEG_EQUITY filings on each side, and ROE as the cycle read it (the gate quarantine
    # is applied by ``dq.apply``, so this is exactly what a rule/score would see).
    rows: list[dict[str, Any]] = []
    for aid in sorted(set(before.dq.negative_equity) | set(after.dq.negative_equity)):
        entry = {
            "ticker": after.ticker.get(aid) or before.ticker.get(aid),
            "sector": after.sector.get(aid) or before.sector.get(aid),
            "profile_type": (
                (after.profiles.get(aid) or before.profiles.get(aid)).type
                if (after.profiles.get(aid) or before.profiles.get(aid))
                else None
            ),
            "equity": (after.dq.negative_equity.get(aid) or {}).get("equity"),
            "roe_before": before.metrics.get(aid, {}).get("profitability.return_on_equity"),
            "roe_after": after.metrics.get(aid, {}).get("profitability.return_on_equity"),
            "de_before": before.metrics.get(aid, {}).get("leverage.debt_to_equity"),
            "de_after": after.metrics.get(aid, {}).get("leverage.debt_to_equity"),
            "inputs": after.dq.negative_equity.get(aid) or before.dq.negative_equity.get(aid),
        }
        rows.append(entry)

    # D1: which companies leave the HARD DATA_QUALITY veto because DQ_NEG_EQUITY is no longer HARD.
    def dq_neg_assets(snap: Snapshot) -> set[int]:
        return {
            a
            for a, issues in snap.dq.hard.items()
            if any(i["rule_id"] == "DQ_NEG_EQUITY" for i in issues)
        }

    left = dq_neg_assets(before) - dq_neg_assets(after)

    # D2: the negative-equity companies the screen vetoes (HARD) on each side.
    def leverage_neg_hits(snap: Snapshot) -> dict[str, str]:
        out = {}
        for h in _LeverageRule().evaluate(snap.ctx):
            if h.asset_id in snap.dq.negative_equity:
                out[snap.ticker.get(h.asset_id, str(h.asset_id))] = h.severity
        return out

    report = {
        "cycle_date": CYCLE_DATE,
        "before_gate": BEFORE_GATE,
        "after_gate": AFTER_GATE,
        "negative_equity_companies": len(rows),
        "companies": rows,
        "data_quality_neg_equity_vetoes": {
            "before": sorted(before.ticker.get(a, str(a)) for a in dq_neg_assets(before)),
            "after": sorted(after.ticker.get(a, str(a)) for a in dq_neg_assets(after)),
            "leave_it": sorted(after.ticker.get(a, str(a)) for a in left),
        },
        "leverage_extreme_negative_equity": {
            "before": leverage_neg_hits(before),
            "after": leverage_neg_hits(after),
        },
        "roe_kept_rows": [
            {
                "ticker": r["ticker"],
                "equity": r["equity"],
                "roe_before": r["roe_before"],
                "roe_after": r["roe_after"],
            }
            for r in rows
            if r["roe_before"] is None and r["roe_after"] is not None
        ],
        # D1's own subject: the filings whose DQ_NEG_EQUITY row names ROE, on either gate version.
        "roe_quarantine": _roe_quarantine_rows(before.conn),
        # R6: the ROE values the applicability fix gives back.
        "roe_regained": _roe_regained(after.conn, before.conn),
    }
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {args.out}")
    print(
        f"  negative-equity companies {len(rows)}; DATA_QUALITY DQ_NEG_EQUITY vetoes "
        f"{len(report['data_quality_neg_equity_vetoes']['before'])} -> "
        f"{len(report['data_quality_neg_equity_vetoes']['after'])}; "
        f"ROE rows kept {len(report['roe_kept_rows'])}"
    )
    print(
        f"  LEVERAGE_EXTREME on negative equity: before "
        f"{report['leverage_extreme_negative_equity']['before']} after "
        f"{report['leverage_extreme_negative_equity']['after']}"
    )
    quarantined = [r for r in report["roe_quarantine"] if r["quarantined"]]
    print(
        f"  ROE quarantine rows {len(report['roe_quarantine'])}; quarantined {len(quarantined)} "
        f"({sorted({r['ticker'] for r in quarantined})})"
    )
    regained = report["roe_regained"]
    print(
        f"  R6: DQ_NEG_EQUITY filings whose ROE has a value now: {len(regained)}"
        f" ({len({r['ticker'] for r in regained})} companies); "
        f"ROE before on those filings: {sum(1 for r in regained if r['roe_before'] is not None)}"
    )
    for r in regained[:6]:
        print(
            f"    {r['ticker']:5} {r['form']:4} {r['period_end']} roe={r['roe']:.4f} "
            f"equity={r['equity']} prior_equity={r['prior_year_equity']}"
        )


# -- D2's 3x3 sensitivity grid -----------------------------------------------------------------


def cmd_d04(args: argparse.Namespace) -> None:
    """D2's net debt/EBITDA {4,5,6} x coverage {1.0,1.5,2.0} grid, by sector (D-04)."""
    snap = Snapshot(args.before, CYCLE_DATE, BEFORE_GATE)
    grid: dict[str, Any] = {}
    for ndte in (4.0, 5.0, 6.0):
        for cov in (1.0, 1.5, 2.0):
            rule = _LeverageRule(
                neg_equity_net_debt_to_ebitda_threshold=ndte,
                neg_equity_interest_coverage_threshold=cov,
            )
            by_sector: dict[str, list[str]] = defaultdict(list)
            for h in rule.evaluate(snap.ctx):
                if h.severity == "HARD" and h.asset_id in snap.dq.negative_equity:
                    by_sector[snap.sector.get(h.asset_id) or "unknown"].append(
                        snap.ticker.get(h.asset_id, str(h.asset_id))
                    )
            grid[f"net_debt_to_ebitda>{ndte}|coverage<{cov}"] = {
                "companies": sum(len(v) for v in by_sector.values()),
                "by_sector": {s: sorted(t) for s, t in sorted(by_sector.items())},
            }
    report = {
        "cycle_date": CYCLE_DATE,
        "db": args.before,
        "gate": BEFORE_GATE,
        "note": "the DQ_NEG_EQUITY record is the dq-v3 one here (the copy as it was); the grid is a "
        "sensitivity over T-116's two thresholds, not a second rule",
        "grid": grid,
    }
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {args.out}")
    for key, entry in grid.items():
        print(f"  {key:34} {entry['companies']:>2} {entry['by_sector']}")


# -- D13: the utilities ------------------------------------------------------------------------


def _stored_coverage_rows(conn: Any) -> dict[int, dict[str, Any]]:
    """The stored D-13 rule input rows, newest 10-K per asset (what the rule reads)."""
    rows = conn.execute(
        """
        WITH known AS (
            SELECT f.id, ROW_NUMBER() OVER (
                PARTITION BY f.asset_id ORDER BY f.period_end DESC, f.available_at DESC, f.id DESC
            ) AS rn
            FROM sec_filings f
            WHERE f.form LIKE '10-K%' AND f.period_end IS NOT NULL AND f.available_at IS NOT NULL
        )
        SELECT f.asset_id, f.accession_number, f.period_end, m.value, m.inputs_json
        FROM fundamental_metrics m
        JOIN sec_filings f ON f.id = m.filing_id
        JOIN known k ON k.id = f.id AND k.rn = 1
        WHERE m.metric_group = 'rule_input' AND m.metric_name = 'utility_cash_coverage_3y'
        """
    ).fetchall()
    return {
        int(r["asset_id"]): {
            "accession": r["accession_number"],
            "period_end": r["period_end"],
            "value": r["value"],
            "inputs": _json_object(r["inputs_json"]),
        }
        for r in rows
    }


def cmd_d13(args: argparse.Namespace) -> None:
    """Per utility: the three yearly ratios, both bounds, the components, the sensitivities (D-13)."""
    snap = Snapshot(args.db, CYCLE_DATE, AFTER_GATE)
    stored = _stored_coverage_rows(snap.conn)
    utilities = {
        a: p
        for a, p in snap.profiles.items()
        if p is not None and p.regulated_utility and a in snap.asset_ids
    }
    table: list[dict[str, Any]] = []
    for aid in sorted(utilities, key=lambda a: snap.ticker.get(a, str(a))):
        entry: dict[str, Any] = {
            "ticker": snap.ticker.get(aid, str(aid)),
            "sector": snap.sector.get(aid),
            "gics_sub_industry": utilities[aid].sub_industry,
        }
        row = stored.get(aid)
        if row is None:
            entry["rule_input"] = None
            entry["note"] = (
                "no 10-K rule input in the store (no 10-K, or its debt does not resolve)"
            )
            table.append(entry)
            continue
        inputs = row["inputs"]
        bound_in = inputs.get(UTILITY_COVERAGE_BOUND_IN)
        bound_wc = inputs.get(UTILITY_COVERAGE_BOUND_AS_WC)
        entry.update(
            {
                "accession": row["accession"],
                "period_end": row["period_end"],
                "years": inputs.get("years"),
                "ambiguous_concepts": inputs.get("ambiguous_concepts"),
                "ambiguous_in": bound_in,
                "ambiguous_as_wc": bound_wc,
                "missing_bound": bool(inputs.get(UTILITY_COVERAGE_MISSING_BOUND_FLAG)),
                "ambiguous_working_capital": bool(inputs.get(UTILITY_COVERAGE_AMBIGUOUS_FLAG)),
                "below_5_both": (
                    bound_in is not None
                    and bound_wc is not None
                    and bound_in < UTILITY_COVERAGE_THRESHOLD
                    and bound_wc < UTILITY_COVERAGE_THRESHOLD
                ),
            }
        )
        table.append(entry)
    with_both = [e for e in table if e.get("ambiguous_in") is not None]
    # the 5%-12% sensitivity: how many utilities fall below each cutoff, on each bound
    sensitivity = {
        f"{cutoff:.2f}": {
            "ambiguous_in": sum(1 for e in with_both if e["ambiguous_in"] < cutoff),
            "ambiguous_as_wc": sum(1 for e in with_both if e["ambiguous_as_wc"] < cutoff),
            "either_bound": sum(
                1 for e in with_both if e["ambiguous_in"] < cutoff or e["ambiguous_as_wc"] < cutoff
            ),
        }
        for cutoff in (0.05, 0.06, 0.07, 0.08, 0.09, 0.10, 0.11, 0.12)
    }
    report = {
        "cycle_date": CYCLE_DATE,
        "db": args.db,
        "utilities": len(table),
        "with_both_bounds": len(with_both),
        "below_5_on_both_bounds": [e["ticker"] for e in table if e.get("below_5_both")],
        "table": table,
        "sensitivity_5_to_12": sensitivity,
        "working_capital_in_out_sensitivity": {
            e["ticker"]: round((e["ambiguous_as_wc"] or 0.0) - (e["ambiguous_in"] or 0.0), 6)
            for e in with_both
        },
    }
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {args.out}")
    print(
        f"  utilities {len(table)}, with both bounds {len(with_both)}, "
        f"both below 5% {report['below_5_on_both_bounds']}"
    )
    for cutoff, entry in sensitivity.items():
        print(
            f"  cutoff {cutoff}: in {entry['ambiguous_in']}, as-wc {entry['ambiguous_as_wc']}, "
            f"either {entry['either_bound']}"
        )


# -- D14: the REITs ----------------------------------------------------------------------------


def cmd_d14(args: argparse.Namespace) -> None:
    """The REITs ``NEGATIVE_FCF`` vetoes, per date, with the FCF's parts (D-14, measured)."""
    dates = [CYCLE_DATE] + [d for d in _quarter_ends(FIRST_QUARTER, CYCLE_DATE) if d != CYCLE_DATE]
    per_date: dict[str, Any] = {}
    for date in dates:
        before = Snapshot(args.before, date, BEFORE_GATE)
        after = Snapshot(args.after, date, AFTER_GATE)
        reits = {
            a
            for a, p in after.profiles.items()
            if p is not None and p.reit and a in after.asset_ids
        }
        vetoed = sorted(
            a
            for a in reits
            if any(h.severity == "HARD" for h in after.hits()["NEGATIVE_FCF"] if h.asset_id == a)
        )
        rows = []
        for aid in vetoed:
            row = after.raw_metrics.get(aid, {}).get("cashflow.free_cash_flow_margin")
            filing = _latest_filing(after, aid)
            inputs = _json_object(filing["inputs_json"]) if filing else {}
            period = (inputs.get("provenance") or {}).get("period")
            acq, acq_concepts = _acquisition_value(
                after, filing["filing_id"] if filing else None, inputs
            )
            capex = _fcf_part(inputs, "capital_expenditure")
            rows.append(
                {
                    "ticker": after.ticker.get(aid),
                    "sector": after.sector.get(aid),
                    "period": period,
                    "operating_cash_flow": _fcf_part(inputs, "operating_cash_flow"),
                    "capital_expenditure": capex,
                    "free_cash_flow": _fcf_part(inputs, "free_cash_flow"),
                    "free_cash_flow_margin": row,
                    "acquisitions_value": acq,
                    "acquisitions_concepts": acq_concepts,
                    "acquisitions_share_of_capex": (
                        acq / capex if acq is not None and capex else None
                    ),
                    "capex_concepts": [
                        i.get("concept")
                        for i in (inputs.get("provenance") or {})
                        .get("items", {})
                        .get("capital_expenditure", [])
                    ],
                    "before_severity": next(
                        (h.severity for h in before.hits()["NEGATIVE_FCF"] if h.asset_id == aid),
                        None,
                    ),
                }
            )
        per_date[date] = {"reits": len(reits), "negative_fcf_vetoes": len(vetoed), "rows": rows}
        print(f"{date}: {len(vetoed)} of {len(reits)} REITs vetoed by NEGATIVE_FCF", flush=True)
    report = {
        "universe": UNIVERSE,
        "before_gate": BEFORE_GATE,
        "after_gate": AFTER_GATE,
        "note": (
            "D-14 is measured, not decided. ``acquisitions_value`` sums every cash-flow concept whose "
            "own caption names an acquisition (R8's R1 rule: the filer's words decide, so a REIT "
            "that files only an extension element -- ARE's are_AcquisitionOfRealEstateAndOtherAssets…, "
            "DLR's dlr_PaymentsToAcquireAssetOrBusinessesNetOfCashAcquired -- is counted), over the "
            "same terms the capex composite used (including the prior fiscal year's own 10-K, by its "
            "accession), each fact as filed x preferred_sign (ID-11), plus the two standard APP-04b "
            "concepts. ``acquisitions_concepts`` names the lines summed; ``capex_concepts`` is the "
            "line the composite read, which is not the whole investing programme. Two cases: FRT's "
            "capex is its acquisition line alone (``PaymentsToAcquireRealEstate``), so its 100% is "
            "the line read, not zero development spending -- FRT also files "
            '``CapitalExpendituresDevelopmentRedevelopment`` ("Capital expenditures - development and '
            "redevelopment\"), a filer extension MET-08's map does not read; and DLR's capex row names "
            'development only (``PaymentsToDevelopRealEstateAssets``, "Improvements to investments in '
            'real estate") while its acquisitions are the separate extension line above. APP-04b / '
            "MET-08 (b) is why ``PaymentsForCapitalImprovements`` is never added to an acquisition "
            'line: it covers "acquisition of or capital improvements", so adding it would double '
            "count."
        ),
        "per_date": per_date,
    }
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {args.out}")


def _fcf_part(inputs: dict[str, Any], name: str) -> Any:
    """A cash-flow part from a stored inputs block: a 10-K's own key, or a 10-Q's ``_ttm`` one."""
    value = inputs.get(name)
    return value if value is not None else inputs.get(f"{name}_ttm")


def _latest_filing(snap: Snapshot, asset_id: int) -> Any:
    """The newest usable filing row (id, inputs) behind ``latest_metrics`` for one asset."""
    row = snap.conn.execute(
        """
        SELECT f.id AS filing_id, m.inputs_json
        FROM fundamental_metrics m JOIN sec_filings f ON f.id = m.filing_id
        WHERE f.asset_id = ? AND f.available_at IS NOT NULL AND f.available_at <= ?
          AND m.metric_name = 'free_cash_flow_margin'
        ORDER BY f.period_end DESC, f.available_at DESC, f.id DESC LIMIT 1
        """,
        (asset_id, snap.date),
    ).fetchone()
    return {"filing_id": int(row["filing_id"]), "inputs_json": row["inputs_json"]} if row else None


def _acquisition_value(
    snap: Snapshot, filing_id: int | None, inputs: dict[str, Any]
) -> tuple[float | None, list[str]]:
    """APP-04b's acquisition share, with the line classified from the filer's own caption (R8).

    The capex item records the columns it used -- a 10-K's fiscal year, or a 10-Q's
    ``prior_fiscal_year`` / ``prior_ytd`` / ``ytd`` terms. Every cash-flow concept whose **own
    caption** names an acquisition of real estate or properties (the filer's words, as R1 classifies
    an extension) is summed over those same terms, with the same weights as the capex composite. This
    is what fills the share for a REIT that files its acquisitions only under an extension element
    (ARE files ``are_AcquisitionOfRealEstateAndOtherAssets...``), which the standard concepts miss.
    """
    if filing_id is None:
        return None, []
    items = ((inputs.get("provenance") or {}).get("items") or {}).get("capital_expenditure") or []
    if not items:
        return None, []
    columns = items[0].get("columns") or []
    if not columns:
        return None, []
    facts = snap.conn.execute(
        "SELECT filing_id, concept, label, statement, period_key, value, preferred_sign "
        "FROM financial_facts WHERE statement = 'cash_flow' AND (filing_id = ? OR filing_id IN "
        "(SELECT id FROM sec_filings WHERE accession_number IN (SELECT value FROM json_each(?))))",
        (filing_id, json.dumps([c[2] for c in columns if c[2]])),
    ).fetchall()
    # ID-11: the resolver reads the filed value times the row's preferred_sign, never abs(). The
    # capex columns reach into the *prior 10-K* for the prior fiscal year (a 10-Q's own statement
    # carries only year-to-date columns), so the facts come from every accession the item names.
    by_key = {
        (int(r["filing_id"]), str(r["concept"]), str(r["period_key"])): (
            None if r["value"] is None else r["value"] * (r["preferred_sign"] or 1.0)
        )
        for r in facts
    }
    accession_to_filing = {
        str(r["accession_number"]): int(r["id"])
        for r in snap.conn.execute(
            "SELECT id, accession_number FROM sec_filings WHERE accession_number IN "
            "(SELECT value FROM json_each(?))",
            (json.dumps([c[2] for c in columns if c[2]]),),
        )
    }
    concepts = {
        str(r["concept"])
        for r in facts
        if int(r["filing_id"]) == filing_id
        and _is_real_estate_acquisition(str(r["concept"]), str(r["label"] or ""))
    } | set(REIT_ACQUISITION_CONCEPTS)
    total = 0.0
    found = False
    for role, key, accession in columns:
        source = accession_to_filing.get(str(accession), filing_id) if accession else filing_id
        weight = -1.0 if role == "prior_ytd" else 1.0
        for concept in concepts:
            # a 10-K column names the instant (``2025-12-31``); its cash-flow duration key is the FY
            for candidate in (str(key), f"{key} (FY)"):
                value = by_key.get((source, concept, candidate))
                if value is not None:
                    total += weight * value
                    found = True
                    break
    return (total if found else None), sorted(concepts)


def _is_real_estate_acquisition(concept: str, label: str) -> bool:
    """The filer's own caption names an acquisition (R8's R1 rule), and it is not an intangible.

    APP-04b puts a REIT's acquisitions in its capex, so a filer extension counts (ARE's
    ``are_AcquisitionOfRealEstateAndOtherAssets...``, DLR's
    ``dlr_PaymentsToAcquireAssetOrBusinessesNetOfCashAcquired``) and so does a REIT's business
    combination (IRM's ``PaymentsToAcquireBusinessesNetOfCashAcquired``, captioned "Cash paid for
    acquisitions, net of cash acquired"). A bare acquisition of intangibles, and the income
    statement's acquisition costs, are not capex.
    """
    text = label.lower()
    if "acquisition" not in text:
        return False
    return "intangible" not in concept.lower() and "businesscombination" not in concept.lower()


# -- D5: liquidity by company type -------------------------------------------------------------


def cmd_liquidity(args: argparse.Namespace) -> None:
    """D5's newly evaluated companies and hits: the old GICS exemption vs the profile (D5)."""
    before = Snapshot(args.before, CYCLE_DATE, BEFORE_GATE)
    after = Snapshot(args.after, CYCLE_DATE, AFTER_GATE)
    # The old predicate re-stated (the constant is gone): exempt = the GICS sector is Financials or
    # Utilities. The new one is the profile (financial_firm or the regulated-utility overlay).
    old_exempt = {a for a in after.asset_ids if after.sector.get(a) in OLD_LIQUIDITY_EXEMPT_SECTORS}
    new_exempt = {
        a
        for a in after.asset_ids
        if (p := after.profiles.get(a)) is not None and (p.financial_firm or p.regulated_utility)
    }
    newly_evaluated = sorted(
        after.ticker.get(a, str(a)) for a in (old_exempt - new_exempt) & set(after.asset_ids)
    )
    no_longer_exempt_hits = sorted(
        after.ticker.get(h.asset_id, str(h.asset_id))
        for h in after.hits()["LIQUIDITY_DISTRESS"]
        if h.asset_id in (old_exempt - new_exempt)
    )
    report = {
        "cycle_date": CYCLE_DATE,
        "before_gate": BEFORE_GATE,
        "after_gate": AFTER_GATE,
        "old_predicate": sorted(OLD_LIQUIDITY_EXEMPT_SECTORS),
        "old_exempt_companies": len(old_exempt),
        "new_exempt_companies": len(new_exempt),
        "newly_evaluated": newly_evaluated,
        "newly_evaluated_hits": no_longer_exempt_hits,
        "still_exempt_but_not_new": sorted(
            after.ticker.get(a, str(a)) for a in (old_exempt & new_exempt)
        ),
        "before_hits": sorted(
            before.ticker.get(h.asset_id, str(h.asset_id))
            for h in before.hits()["LIQUIDITY_DISTRESS"]
        ),
        "after_hits": sorted(
            after.ticker.get(h.asset_id, str(h.asset_id))
            for h in after.hits()["LIQUIDITY_DISTRESS"]
        ),
    }
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {args.out}")
    print(
        f"  exempt {len(old_exempt)} -> {len(new_exempt)}; newly evaluated "
        f"{len(newly_evaluated)}: {newly_evaluated}; their hits {no_longer_exempt_hits}"
    )


# -- R5: the debt finding ----------------------------------------------------------------------


def _debt_population_rows(conn: Any) -> list[Any]:
    """Every filing that files the current-maturity line and no ``LongTermDebtCurrent``, with the
    stored ``debt_to_assets`` row that carries MET-07's ``total_debt`` and its provenance."""
    return conn.execute(
        """
        WITH pop AS (
            SELECT DISTINCT filing_id FROM financial_facts
            WHERE concept = 'us-gaap_LongTermDebtAndCapitalLeaseObligationsCurrent'
              AND filing_id NOT IN (SELECT filing_id FROM financial_facts
                                    WHERE concept = 'us-gaap_LongTermDebtCurrent')
        )
        SELECT m.filing_id, m.value, m.inputs_json, f.asset_id, a.ticker, a.cik, s.name AS sector
        FROM fundamental_metrics m
        JOIN sec_filings f ON f.id = m.filing_id
        JOIN assets a ON a.id = f.asset_id
        LEFT JOIN sectors s ON s.id = a.sector_id
        WHERE m.metric_name = 'debt_to_assets' AND m.filing_id IN (SELECT filing_id FROM pop)
        """
    ).fetchall()


def _debt_population_size(conn: Any) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT filing_id) FROM financial_facts "
        "WHERE concept = 'us-gaap_LongTermDebtAndCapitalLeaseObligationsCurrent' "
        "AND filing_id NOT IN (SELECT filing_id FROM financial_facts "
        "WHERE concept = 'us-gaap_LongTermDebtCurrent')"
    ).fetchone()
    return int(row[0])


def _classify_total_debt(conn: Any, rows: list[Any]) -> tuple[list[int], list[dict[str, Any]], int]:
    """``(empty filing ids, understated rows, filings whose total includes the current portion)``.

    Understated: MET-07 resolved ``long_term_debt`` to the **noncurrent-only** element while the filing
    files the current-maturity line separately, so the stored total omits it."""
    empty: list[int] = []
    understated: list[dict[str, Any]] = []
    includes = 0
    for r in rows:
        fid = int(r["filing_id"])
        inputs = _json_object(r["inputs_json"])
        prov = inputs.get("provenance") or {}
        lt = ((prov.get("items") or {}).get("long_term_debt") or [{}])[0].get("concept")
        total = inputs.get("total_debt")
        if total is None:
            empty.append(fid)
            continue
        if lt != NONCURRENT_LONG_TERM:
            includes += 1
            continue
        cm = conn.execute(
            "SELECT value FROM financial_facts WHERE filing_id = ? AND concept = ?",
            (fid, CURRENT_MATURITY_CONCEPT),
        ).fetchone()
        understated.append(
            {
                "filing_id": fid,
                "ticker": r["ticker"],
                "sector": r["sector"],
                "cik": r["cik"],
                "total_debt": total,
                "current_maturity_line": cm["value"] if cm else None,
                "share": (cm["value"] / total) if cm and cm["value"] and total else None,
            }
        )
    return empty, understated, includes


def _reading_changes(
    conn: Any, snap: Snapshot, understated: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """At the cycle date, the companies whose D/E, net debt/EBITDA or LEVERAGE_EXTREME verdict would
    change if the current-maturity line were added to the stored total debt."""
    changes: list[dict[str, Any]] = []
    for s in understated:
        aid_row = conn.execute(
            "SELECT asset_id FROM sec_filings WHERE id = ?", (s["filing_id"],)
        ).fetchone()
        aid = int(aid_row["asset_id"]) if aid_row else None
        if aid is None or aid not in snap.asset_ids:
            continue
        if _latest_metric_filing(snap, aid) != s["filing_id"]:
            continue  # not the company's latest usable filing at the cycle date
        row = conn.execute(
            "SELECT inputs_json FROM fundamental_metrics WHERE filing_id = ? "
            "AND metric_name = 'debt_to_equity'",
            (s["filing_id"],),
        ).fetchone()
        inputs = _json_object(row["inputs_json"]) if row else {}
        eq = inputs.get("equity")
        ebitda = inputs.get("ebitda")
        cash = inputs.get("cash") or 0.0
        sti = inputs.get("short_term_investments") or 0.0
        total, cm = s["total_debt"], s["current_maturity_line"] or 0.0
        new_total = total + cm
        dte_old = (total / eq) if eq else None
        dte_new = (new_total / eq) if eq else None
        ndte_old = (total - cash - sti) / ebitda if ebitda and ebitda > 0 else None
        ndte_new = (new_total - cash - sti) / ebitda if ebitda and ebitda > 0 else None
        verdict_old = dte_old is not None and dte_old > LEVERAGE_DTE_THRESHOLD
        verdict_new = dte_new is not None and dte_new > LEVERAGE_DTE_THRESHOLD
        if dte_old != dte_new or ndte_old != ndte_new or verdict_old != verdict_new:
            changes.append(
                {
                    "ticker": s["ticker"],
                    "sector": s["sector"],
                    "de_before": dte_old,
                    "de_after": dte_new,
                    "ndte_before": ndte_old,
                    "ndte_after": ndte_new,
                    "leverage_extreme_before": verdict_old,
                    "leverage_extreme_after": verdict_new,
                    "equity_negative": bool(eq is not None and eq <= 0),
                }
            )
    return changes


def cmd_debt(args: argparse.Namespace) -> None:
    """R5: how MET-07 formed ``total_debt`` where the 10-K files the current-maturity line."""
    conn = connect_ro(args.db)
    rows = _debt_population_rows(conn)
    population = _debt_population_size(conn)
    empty, understated, includes = _classify_total_debt(conn, rows)
    shares = sorted(s["share"] for s in understated if s["share"] is not None)
    companies = {s["cik"] for s in understated}
    by_sector = Counter(s["sector"] for s in understated)
    snap = Snapshot(args.db, CYCLE_DATE, BEFORE_GATE)
    changes = _reading_changes(conn, snap, understated)
    median = statistics.median(shares) if shares else None
    report = {
        "db": args.db,
        "cycle_date": CYCLE_DATE,
        "population": {
            "filings_with_the_current_maturity_line_and_no_LongTermDebtCurrent": population,
            "with_a_leverage_row": len(rows),
        },
        "how_total_debt_was_formed": {
            "from_a_total_that_includes_the_current_portion": includes,
            "from_the_parts_without_it_understated": len(understated),
            "empty": len(empty),
        },
        "understated": {
            "filings": len(understated),
            "companies": len(companies),
            "by_sector": dict(sorted(by_sector.items(), key=lambda i: str(i[0]))),
            "share_of_total_debt": {
                "median": median,
                "p90": shares[int(0.9 * (len(shares) - 1))] if shares else None,
                "max": shares[-1] if shares else None,
                "n": len(shares),
            },
            "material": bool(
                len(companies) > MATERIAL_COMPANIES and (median or 0) > MATERIAL_SHARE
            ),
            "rows": understated,
        },
        "at_the_cycle_date": {
            "companies_whose_reading_would_change": len(changes),
            "rows": changes,
        },
        "empty_filings": len(empty),
        "note": (
            "measure only; no resolver change (R5). The current-maturity line "
            "us-gaap_LongTermDebtAndCapitalLeaseObligationsCurrent is not in SHORT_TERM_DEBT_MAP, "
            "and total_debt_capture_failure / total_debt_short_term_debt_only are the already "
            "recorded T-148/T-151 classes (docs/model_fixes.md)."
        ),
    }
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {args.out}")
    print(f"  population {population}; {report['how_total_debt_was_formed']}")
    print(
        f"  understated {len(understated)} filings / {len(companies)} companies; shares "
        f"{report['understated']['share_of_total_debt']}; material "
        f"{report['understated']['material']}"
    )
    print(f"  at {CYCLE_DATE}: {len(changes)} companies' reading would change")


def _latest_metric_filing(snap: Snapshot, asset_id: int) -> int | None:
    row = snap.conn.execute(
        """
        WITH known AS (
            SELECT f.id, ROW_NUMBER() OVER (
                PARTITION BY f.asset_id ORDER BY f.period_end DESC, f.available_at DESC, f.id DESC
            ) AS rn
            FROM sec_filings f
            WHERE f.period_end IS NOT NULL AND f.available_at IS NOT NULL AND f.available_at <= ?
        )
        SELECT k.id FROM known k WHERE k.rn = 1 AND k.id IN (
            SELECT filing_id FROM fundamental_metrics
        ) AND k.id IN (SELECT id FROM sec_filings WHERE asset_id = ?)
        """,
        (snap.date, asset_id),
    ).fetchone()
    return int(row["id"]) if row else None


# -- all ---------------------------------------------------------------------------------------


def cmd_all(args: argparse.Namespace) -> None:
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    jobs = (
        ("vetoes_by_sector.json", cmd_vetoes, ("before", "after", "side", "before_json")),
        ("neg_equity.json", cmd_neg_equity, ("before", "after")),
        ("d04_sensitivity.json", cmd_d04, ("before",)),
        ("d13_utilities.json", cmd_d13, ("db",)),
        ("d14_reits.json", cmd_d14, ("before", "after")),
        ("liquidity.json", cmd_liquidity, ("before", "after")),
        ("r5_debt.json", cmd_debt, ("db",)),
    )
    for name, handler, fields in jobs:
        kwargs = {
            "before": args.before,
            "after": args.after,
            "db": args.after,
            "side": "after",
            "before_json": getattr(args, "before_json", None),
        }
        handler(argparse.Namespace(out=str(out / name), **{f: kwargs[f] for f in fields}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, handler in (
        ("vetoes", cmd_vetoes),
        ("neg-equity", cmd_neg_equity),
        ("d04", cmd_d04),
        ("d13", cmd_d13),
        ("d14", cmd_d14),
        ("liquidity", cmd_liquidity),
        ("debt", cmd_debt),
    ):
        sub = subparsers.add_parser(name, help=handler.__doc__)
        needs = {
            "vetoes": (),
            "neg-equity": ("before", "after"),
            "d04": ("before",),
            "d13": ("db",),
            "d14": ("before", "after"),
            "liquidity": ("before", "after"),
            "debt": ("db",),
        }[name]
        for field in needs:
            sub.add_argument(f"--{field}", required=True)
        if name == "vetoes":
            sub.add_argument(
                "--side",
                choices=("before", "after"),
                default="after",
                help="before: write the control's per-date result (run with master's code on the "
                "path); after: write the combined before -> after report",
            )
            sub.add_argument(
                "--db", help="the database this side reads (required for --side before)"
            )
            sub.add_argument("--before", help="the before database, when no --before-json is given")
            sub.add_argument(
                "--before-json",
                default=None,
                help="the control's --side before output, produced by master's code",
            )
            sub.add_argument("--after", help="the after database (required for --side after)")
        sub.add_argument("--out", required=True)
        sub.set_defaults(func=handler)
    p_all = subparsers.add_parser("all", help="every measurement, into --out-dir")
    p_all.add_argument("--before", required=True)
    p_all.add_argument("--after", required=True)
    p_all.add_argument("--before-json", default=None, help="the control's --side before output")
    p_all.add_argument("--out-dir", required=True)
    p_all.set_defaults(func=cmd_all)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
