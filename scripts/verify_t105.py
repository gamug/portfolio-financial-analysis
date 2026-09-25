"""Reproduce T-105's verification from stored data, read-only (docs/model_fixes.md, T-105).

    uv run python scripts/verify_t105.py [--db PATH] [--engine-version metrics-v2]

For every filing of the assets that have metrics, rebuilds the statements from stored
``financial_facts``, recomputes the TTM-dependent ratios with today's code -- the TTM reading
prior filings recorded under ``--engine-version``, the version whose rows are stored -- and
prints:

1. 10-K vs 10-Q medians, stored (before) vs recomputed (after);
2. the TTM methods used, and where the year-to-date identity and four recorded quarters were
   both computable, how often they differ by more than the cross-check tolerance (1%);
3. APA's revenue as resolved against the income statement's own "Total revenues and other"
   (the T-117 case).

Nothing is written.
"""

from __future__ import annotations

import argparse
import collections
import statistics
from typing import Any

from portfolio_common.db import Database

from fundamental_agent import db
from fundamental_agent.metrics import compute_group, valuation
from fundamental_agent.metrics.base import TTMFlow
from fundamental_agent.pipeline import _TTM_ITEMS, _targets, _YearTask, _ytd_columns
from fundamental_agent.pricing import close_on_or_before
from fundamental_agent.quality import TTM_CROSSCHECK_TOLERANCE
from fundamental_agent.statements import Statements
from kg_schema.cli import resolve_db_path
from kg_schema.queries import connect_ro

METRICS = (
    "free_cash_flow_yield",
    "enterprise_fcf_yield",
    "sbc_adjusted_fcf_yield",
    "net_debt_to_ebitda",
    "return_on_invested_capital",
    "return_on_assets",
)


def _statements(conn: Database, filing_id: int) -> Statements:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT statement, concept, label, standard_concept, period_key, value "
        "FROM financial_facts WHERE filing_id = ? ORDER BY id",
        (filing_id,),
    ):
        row = rows.setdefault(
            (r["statement"], r["concept"]),
            {
                "concept": r["concept"],
                "label": r["label"],
                "standard_concept": r["standard_concept"],
                "abstract": False,
                "dimension": False,
            },
        )
        row.setdefault(r["period_key"], r["value"])
    payload: dict[str, list[Any]] = {"income_statement": [], "balance_sheet": [], "cash_flow": []}
    for (statement, _concept), row in rows.items():
        payload.setdefault(statement, []).append(row)
    return Statements.from_payload(payload)


def _ttm(
    conn: Database, filing: Any, stmts: Statements, target: Any, version: str
) -> dict[str, TTMFlow]:
    if filing["form"] != "10-Q":
        return {}
    current = {i: v for i in _TTM_ITEMS if (v := stmts.get(i, target.period.key)) is not None}
    cur, prior, prior_end = _ytd_columns(stmts, target)
    ytd = (
        {i: db.YTDPair(stmts.get(i, cur), stmts.get(i, prior), prior_end) for i in _TTM_ITEMS}
        if cur and prior
        else {}
    )
    return db.ttm_detail(
        conn,
        filing["asset_id"],
        period_end=target.period.date,
        current=current,
        ytd=ytd,
        engine_version=version,
    )


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def main() -> int:  # noqa: C901, PLR0912 - one linear report
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db")
    parser.add_argument("--engine-version", default="metrics-v2")
    args = parser.parse_args()
    conn = connect_ro(resolve_db_path(args.db))
    version = args.engine_version

    before: dict[str, dict[str, list[float]]] = collections.defaultdict(
        lambda: collections.defaultdict(list)
    )
    for r in conn.execute(
        "SELECT f.form, m.metric_name, m.value FROM fundamental_metrics m "
        "JOIN sec_filings f ON f.id = m.filing_id "
        "WHERE m.engine_version = ? AND m.value IS NOT NULL",
        (version,),
    ):
        before[r["metric_name"]][r["form"]].append(float(r["value"]))

    after: dict[str, dict[str, list[float]]] = collections.defaultdict(
        lambda: collections.defaultdict(list)
    )
    methods: collections.Counter[str] = collections.Counter()
    both = gaps = 0
    filings = conn.execute(
        "SELECT f.*, a.ticker FROM sec_filings f JOIN assets a ON a.id = f.asset_id "
        "WHERE f.asset_id IN (SELECT DISTINCT f2.asset_id FROM fundamental_metrics m "
        "JOIN sec_filings f2 ON f2.id = m.filing_id WHERE m.engine_version = ?) "
        "ORDER BY f.period_end",
        (version,),
    ).fetchall()
    for f in filings:
        stmts = _statements(conn, int(f["id"]))
        task = _YearTask(int(f["asset_id"]), f["ticker"], f["ticker"], f["form"], 0)
        target = next(
            (t for t in _targets(stmts, task) if t.fiscal_period == f["fiscal_period"]), None
        )
        if target is None:
            continue
        flows = _ttm(conn, f, stmts, target, version)
        for flow in flows.values():
            methods[flow.method] += 1
            if flow.method == "ytd" and flow.alt is not None:
                both += 1
                scale = max(abs(flow.value), abs(flow.alt))
                if scale and abs(flow.value - flow.alt) > TTM_CROSSCHECK_TOLERANCE * scale:
                    gaps += 1
        ttm = {k: v.value for k, v in flows.items()}
        out: dict[str, float | None] = {}
        for group in ("leverage", "roic", "profitability"):
            for result in compute_group(group, stmts, target.period.key, None, ttm):
                out[result.name] = result.value
        price = close_on_or_before(conn, int(f["asset_id"]), target.period.date)
        if price is not None:
            factors = db.detect_share_scale_factors(
                conn, int(f["asset_id"]), stmts, target.period.key, exclude_filing_id=int(f["id"])
            )
            for result in valuation.compute(stmts, target.period.key, price, factors, ttm):
                out[result.name] = result.value
        for name, value in out.items():
            if value is not None:
                after[name][f["form"]].append(value)

    print(f"1. 10-K / 10-Q medians ({len(filings)} filings, recorded version {version})")
    print(f"   {'metric':28} {'before K/Q':>11} {'after K/Q':>10} {'after 10-Q n':>13}")
    for name in METRICS:
        row = []
        for table in (before, after):
            k, q = _median(table[name]["10-K"]), _median(table[name]["10-Q"])
            row.append(f"{k / q:.2f}" if k is not None and q else "n/a")
        print(f"   {name:28} {row[0]:>11} {row[1]:>10} {len(after[name]['10-Q']):>13}")

    total = sum(methods.values())
    print(f"2. TTM methods over {total} flows: {dict(sorted(methods.items()))}")
    print(
        f"   identity and four quarters both computable: {both}; "
        f"differ by > {TTM_CROSSCHECK_TOLERANCE:.0%}: {gaps} (-> SOFT DQ_TTM_CROSSCHECK review)"
    )

    print("3. APA revenue: resolved vs the statement's own 'Total revenues and other'")
    for f in conn.execute(
        "SELECT f.id, f.fiscal_period, f.period_end FROM sec_filings f "
        "JOIN assets a ON a.id = f.asset_id WHERE a.ticker = 'APA' AND f.form = '10-K' "
        "ORDER BY f.period_end"
    ):
        stmts = _statements(conn, int(f["id"]))
        key = f"{f['period_end']} (FY)"
        own = conn.execute(
            "SELECT value FROM financial_facts WHERE filing_id = ? "
            "AND concept = 'apa_RevenuesAndOther' AND period_key = ?",
            (f["id"], key),
        ).fetchone()
        resolved = stmts.get("revenue", key)
        print(
            f"   {f['fiscal_period']}: resolved {resolved:,.0f}  own total {own[0]:,.0f}"
            if resolved is not None and own
            else f"   {f['fiscal_period']}: n/a"
        )
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
