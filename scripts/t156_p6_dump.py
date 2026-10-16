"""T-156 phase 1 — dump one filing in full: inputs + every fact whose concept/label names D&A,
plus the income-statement subtotals. Usage: dump.py TICKER [TICKER...]"""

from __future__ import annotations

import json
import re
import sqlite3
import sys

DB = "/workspaces/thesis/scratch/t156/work.db"
DATE = "2026-09-22"
DA_RE = re.compile(r"depreciat|amorti|deplet", re.IGNORECASE)


def main(tickers: list[str]) -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    for t in tickers:
        a = con.execute(
            "SELECT id,ticker,company_name,cik FROM assets WHERE ticker=?", (t,)
        ).fetchone()
        if not a:
            print("no asset", t)
            continue
        f = con.execute(
            """
            WITH known AS (
                SELECT f.id, ROW_NUMBER() OVER (
                    PARTITION BY f.asset_id ORDER BY f.period_end DESC, f.available_at DESC, f.id DESC
                ) AS rn FROM sec_filings f
                WHERE f.period_end IS NOT NULL AND f.available_at IS NOT NULL AND f.available_at <= ?
            )
            SELECT s.* FROM known k JOIN sec_filings s ON s.id=k.id WHERE k.rn=1 AND s.asset_id=?
            """,
            (DATE, a["id"]),
        ).fetchone()
        print("=" * 100)
        print(a["ticker"], a["company_name"], "asset", a["id"], "filing", dict(f))
        row = con.execute(
            "SELECT value, inputs_json FROM fundamental_metrics WHERE filing_id=? AND metric_group='leverage' AND metric_name='ebitda'",
            (f["id"],),
        ).fetchone()
        print("leverage.ebitda value:", row["value"])
        print(
            "inputs:", json.dumps(json.loads(row["inputs_json"] or "{}"), indent=1, sort_keys=True)
        )
        facts = con.execute(
            "SELECT concept,label,statement,period_key,value FROM financial_facts WHERE filing_id=? ORDER BY statement,concept,period_key",
            (f["id"],),
        ).fetchall()
        print(f"-- cash_flow facts ({sum(1 for x in facts if x['statement'] == 'cash_flow')}) --")
        for x in facts:
            if x["statement"] != "cash_flow":
                continue
            local = x["concept"].split("_", 1)[-1]
            mark = "*DA*" if (DA_RE.search(local) or DA_RE.search(x["label"] or "")) else "    "
            print(
                f"  {mark} {x['concept']:>70s} | {x['period_key']:<20s} | {x['value']!r:>18s} | {x['label']}"
            )
        print("-- income_statement facts naming DA or OperatingIncomeLoss or subtotals --")
        for x in facts:
            if x["statement"] != "income_statement":
                continue
            local = x["concept"].split("_", 1)[-1]
            if (
                DA_RE.search(local)
                or DA_RE.search(x["label"] or "")
                or re.search(
                    r"OperatingIncomeLoss|IncomeLossFromContinuingOperationsBeforeIncomeTaxes|ProfitLoss$|NetIncomeLoss$",
                    local,
                )
            ):
                print(
                    f"     {x['concept']:>70s} | {x['period_key']:<20s} | {x['value']!r:>18s} | {x['label']}"
                )


if __name__ == "__main__":
    main(sys.argv[1:])
