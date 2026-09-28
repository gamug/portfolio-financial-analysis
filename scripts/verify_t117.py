"""Full-universe validation of T-117's revenue-total contradiction guard, read-only
(docs/model_fixes.md, T-117).

    uv run python scripts/verify_t117.py [--db PATH]

For every stored filing (every asset, not a sample), rebuilds the statements from stored
``financial_facts`` and calls ``Statements._label_total_correction`` directly against each of
its own reporting periods -- the same check ``Statements.get("revenue", ...)`` runs internally.
Prints every filing where it fires (contradicted, corrected or not), so the rule's full-universe
behavior can be inspected filing by filing rather than assumed from the APA case it was built
against (the mistake T-095 made, per T-117's own acceptance criterion).

Nothing is written.
"""

from __future__ import annotations

import argparse
import collections

from portfolio_common.db import Database

from fundamental_agent.statements import INSTANT, REGISTRY, Statements
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db")
    args = parser.parse_args()
    conn = connect_ro(resolve_db_path(args.db))
    spec = REGISTRY["revenue"]

    filings = conn.execute(
        "SELECT f.id, f.form, f.fiscal_period, f.period_end, a.ticker "
        "FROM sec_filings f JOIN assets a ON a.id = f.asset_id ORDER BY a.ticker, f.period_end"
    ).fetchall()
    print(f"scanning {len(filings)} stored filings across the universe (every asset with data)")

    by_ticker: collections.Counter[str] = collections.Counter()
    corrected_n = rejected_only_n = 0
    for f in filings:
        stmts = _statements(conn, int(f["id"]))
        for period in stmts.periods:
            if period.date != f["period_end"] or period.tag == INSTANT:
                continue
            total_value = stmts._first_total_match(spec, period.key)
            if total_value is None:
                continue
            corrected, contradicted = stmts._label_total_correction(spec, period.key, total_value)
            if not contradicted:
                continue
            by_ticker[str(f["ticker"])] += 1
            if corrected is not None:
                corrected_n += 1
                verb = "corrected"
                detail = f"{total_value:,.0f} -> {corrected:,.0f}"
            else:
                rejected_only_n += 1
                verb = "rejected (unsafe to derive)"
                detail = f"{total_value:,.0f} -> None"
            print(
                f"  {f['ticker']:6} {f['form']:5} {f['fiscal_period']:9} "
                f"{period.key:20} {verb}: {detail}"
            )

    total = corrected_n + rejected_only_n
    print(
        f"\n{total} filing-periods flagged across {len(by_ticker)} ticker(s): "
        f"{dict(sorted(by_ticker.items()))}"
    )
    print(
        f"  {corrected_n} corrected (derived a replacement value), {rejected_only_n} rejected only"
    )
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
