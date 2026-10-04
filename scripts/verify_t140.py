"""Full-universe replay of T-140 (docs/model_fixes.md), read-only.

    uv run python scripts/verify_t140.py [--db PATH] [--quiet]

For every stored filing (every asset, not a sample), rebuilds the statements from stored
``financial_facts`` and reports, filing by filing rather than assumed from the APA case it was built
against (the mistake T-095 made):

(a) every income-statement column where the revenue rebuild (``Statements.rebuild_total``) fires or
    refuses -- the rebuild only runs where no ``total_concepts`` row has a value -- with the
    anchor row, the rows it subtracted and the value it would store;
(b) every filing whose 10-K or 10-Q label changes under the fiscal-year-end derivation
    (the pipeline's own ``_resolve_target``), and every filing that gets no period of its own.

Nothing is written. Run it against a scratch copy of a database when in doubt.
"""

from __future__ import annotations

import argparse
import collections
from collections.abc import Callable
from datetime import date

from portfolio_common.db import Database, Row

from fundamental_agent import db
from fundamental_agent.pipeline import _resolve_target, _YearTask
from fundamental_agent.statements import REGISTRY, Period, Statements, TotalRebuild
from kg_schema.cli import resolve_db_path
from kg_schema.queries import connect_ro


def _statements(conn: Database, filing_id: int) -> Statements:
    rows: dict[tuple[str, str], dict[str, object]] = {}
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
    payload: dict[str, list[object]] = {
        "income_statement": [],
        "balance_sheet": [],
        "cash_flow": [],
    }
    for (statement, _concept), row in rows.items():
        payload.setdefault(statement, []).append(row)
    return Statements.from_payload(payload)


def _fmt(value: float | None) -> str:
    return "None" if value is None else f"{value / 1e6:,.0f}M"


def _outcome(old: float | None, rebuild: TotalRebuild) -> str:
    if rebuild.value is None:
        return "REFUSED"
    if old is not None and rebuild.value < old:
        return "KEPT-TIER2"
    if old == rebuild.value:
        return "SAME"
    return "CORROBORATED" if rebuild.corroborated else "CHANGED"


def _describe(f: Row, p: Period, old: float | None, rebuild: TotalRebuild, outcome: str) -> str:
    own = "own" if p.date == f["period_end"] else "cmp"
    return (
        f"{outcome:<10} {f['ticker']:<5} {f['fiscal_period']:<8} {p.key:<20}{own}  "
        f"stored={_fmt(old)}  anchor={_fmt(rebuild.anchor_value)} ({rebuild.anchor_label!r})  "
        f"between={[round(b / 1e6) for b in rebuild.between]}  rebuilt={_fmt(rebuild.value)}"
        + (f"  [{rebuild.refusal}]" if rebuild.refusal else "")
    )


def revenue_replay(conn: Database, *, quiet: bool) -> None:
    spec = REGISTRY["revenue"]
    counts: collections.Counter[tuple[str, str]] = collections.Counter()
    own_filings: dict[str, set[int]] = collections.defaultdict(set)
    columns = 0
    filings = conn.execute(
        "SELECT f.id, a.ticker, f.form, f.fiscal_period, f.period_end FROM sec_filings f "
        "JOIN assets a ON a.id = f.asset_id ORDER BY a.ticker, f.period_end, f.id"
    ).fetchall()
    for f in filings:
        stmts = _statements(conn, int(f["id"]))
        for p in (q for q in stmts.periods if not q.is_instant):
            columns += 1
            if stmts._first_total_match(spec, p.key) is not None:
                continue  # a revenue total survived -- the rebuild never runs
            rebuild = stmts.rebuild_total(spec, p.key)
            if rebuild is None:
                continue
            old = stmts._component_value(spec, p.key)
            outcome = _outcome(old, rebuild)
            counts[(f["ticker"], outcome)] += 1
            if outcome in ("CHANGED", "CORROBORATED") and p.date == f["period_end"]:
                own_filings[f["ticker"]].add(int(f["id"]))
            if not (quiet and outcome == "SAME"):
                print(_describe(f, p, old, rebuild, outcome))
    print(f"\n(a) {len(filings)} filings, {columns} income-statement columns scanned")
    for outcome in ("CHANGED", "CORROBORATED", "SAME", "REFUSED", "KEPT-TIER2"):
        per = {t: n for (t, o), n in sorted(counts.items()) if o == outcome}
        print(f"  {outcome:<12} {sum(per.values()):>4} columns  {per}")
    print(
        "  filings whose OWN column gets a rebuilt revenue:",
        sum(len(v) for v in own_filings.values()),
        {t: len(v) for t, v in sorted(own_filings.items())},
    )


def _lookup(conn: Database, asset_id: int) -> Callable[[str], date | None]:
    def lookup(before: str) -> date | None:
        stored = db.latest_fiscal_year_end(conn, asset_id, before)
        return date.fromisoformat(stored) if stored else None

    return lookup


def label_replay(conn: Database) -> None:
    """Every stored 10-K and 10-Q through the pipeline's own ``_resolve_target`` (the latest stored
    10-K as the fiscal year end, else the payload's balance sheet), against the label it was stored
    with."""
    rows = conn.execute(
        "SELECT f.id, f.asset_id, a.ticker, f.form, f.fiscal_period, f.period_end "
        "FROM sec_filings f JOIN assets a ON a.id = f.asset_id "
        "WHERE f.period_end IS NOT NULL ORDER BY a.ticker, f.period_end, f.id"
    ).fetchall()
    relabelled: list[tuple[str, str, str]] = []
    q4_before: collections.Counter[str] = collections.Counter()
    unplaced = 0
    for r in rows:
        stmts = _statements(conn, int(r["id"]))
        task = _YearTask(
            int(r["asset_id"]), r["ticker"], r["ticker"], r["form"], int(r["period_end"][:4])
        )
        target = _resolve_target(stmts, task, _lookup(conn, int(r["asset_id"])))
        if r["fiscal_period"].endswith("Q4"):
            q4_before[r["ticker"]] += 1
        if isinstance(target, str):
            unplaced += 1
            print(f"NO-LABEL   {r['ticker']:<5} {r['fiscal_period']} {r['period_end']}: {target}")
        elif target.fiscal_period != r["fiscal_period"]:
            relabelled.append((r["ticker"], r["fiscal_period"], target.fiscal_period))
            print(
                f"RELABEL    {r['ticker']:<5} {r['fiscal_period']} -> {target.fiscal_period}"
                f"  (period end {r['period_end']})"
            )
    print(
        f"\n(b) {len(rows)} 10-K/10-Qs; {len(relabelled)} relabelled across "
        f"{len({t for t, _, _ in relabelled})} tickers; {sum(q4_before.values())} labelled Q4 before "
        f"({len(q4_before)} tickers: {sorted(q4_before)}); {unplaced} with no quarter of their own"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db")
    parser.add_argument("--quiet", action="store_true", help="omit the no-op (SAME) rebuilds")
    args = parser.parse_args()
    conn = connect_ro(resolve_db_path(args.db))
    revenue_replay(conn, quiet=args.quiet)
    print()
    label_replay(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
