"""T-156 phase 2 — E1's ID-12 check: the both-statements agreement before and after the filed-sign
conversion (value x preferred_sign, ID-12), plus DAL's and UAL's recovered D&A/EBITDA.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fundamental_agent.fidelity import dq01_tolerance

DB = "/workspaces/thesis/scratch/t156/work.db"
OUT = Path("/workspaces/thesis/scratch/t156/out")
G = "us-gaap_"


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT a.filing_id, a.concept, a.period_key, a.value isv, a.preferred_sign is_sig,
               b.value cfv, b.preferred_sign cf_sig
        FROM financial_facts a
        JOIN financial_facts b ON b.filing_id=a.filing_id AND b.concept=a.concept
             AND b.period_key=a.period_key AND b.statement='cash_flow'
        WHERE a.statement='income_statement' AND a.value IS NOT NULL AND b.value IS NOT NULL
          AND (lower(substr(a.concept, instr(a.concept,'_')+1)) LIKE '%depreciat%'
            OR lower(substr(a.concept, instr(a.concept,'_')+1)) LIKE '%amorti%'
            OR lower(substr(a.concept, instr(a.concept,'_')+1)) LIKE '%deplet%')
        """
    ).fetchall()
    before = sum(1 for r in rows if abs(r["isv"] - r["cfv"]) <= dq01_tolerance(r["cfv"], 2))

    def filed(v, sig):
        return v * (sig if sig in (1.0, -1.0) else 1.0)

    after = 0
    still = []
    for r in rows:
        is_f = filed(r["isv"], r["is_sig"])
        cf_f = filed(r["cfv"], r["cf_sig"])
        if abs(is_f - cf_f) <= dq01_tolerance(cf_f, 2):
            after += 1
        else:
            still.append(
                (
                    r["filing_id"],
                    r["concept"],
                    r["period_key"],
                    r["isv"],
                    r["is_sig"],
                    r["cfv"],
                    r["cf_sig"],
                )
            )
    print(f"both-statements pairs {len(rows)}")
    print(
        f"  before the ID-12 conversion (displayed value): agree {before} ({before / len(rows):.1%})"
    )
    print(
        f"  after  the ID-12 conversion (filed sign):      agree {after} ({after / len(rows):.1%})"
    )
    print(f"  still differing after conversion: {len(still)}")
    for s in still[:10]:
        print("   ", s)
    # signs involved
    signs = {(r["is_sig"], r["cf_sig"]) for r in rows}
    print("  preferred_sign pairs (is, cf):", sorted(signs, key=str))
    OUT.joinpath("p2_e1_id12.json").write_text(
        json.dumps(
            {"pairs": len(rows), "before": before, "after": after, "still": still},
            indent=1,
            default=str,
        )
    )

    print("\n=== DAL and UAL at 2026-09-22 ===")
    for ticker in ("DAL", "UAL"):
        f = con.execute(
            """
            WITH known AS (SELECT f.id, ROW_NUMBER() OVER (PARTITION BY f.asset_id
                ORDER BY f.period_end DESC, f.available_at DESC, f.id DESC) rn FROM sec_filings f
                WHERE f.period_end IS NOT NULL AND f.available_at IS NOT NULL AND f.available_at <= '2026-09-22')
            SELECT s.id, s.form, s.accession_number, s.period_end FROM known k
            JOIN sec_filings s ON s.id=k.id JOIN assets a ON a.id=s.asset_id WHERE k.rn=1 AND a.ticker=?
            """,
            (ticker,),
        ).fetchone()
        facts = con.execute(
            "SELECT statement, period_key, concept, value, preferred_sign FROM financial_facts "
            "WHERE filing_id=? AND concept IN (?,?,?,?) ORDER BY statement, period_key",
            (
                f["id"],
                G + "DepreciationDepletionAndAmortization",
                G + "DepreciationAndAmortization",
                G + "DepreciationAmortizationAndAccretionNet",
                G + "OperatingIncomeLoss",
            ),
        ).fetchall()
        print(f"-- {ticker} {f['form']} {f['accession_number']} pe={f['period_end']}")
        for r in facts:
            sig = r["preferred_sign"]
            filed = r["value"] * (sig if sig in (1.0, -1.0) else 1.0)
            print(
                f"     {r['statement']:<17} {r['period_key']:<20} {r['concept'].split('_', 1)[-1]:<40} "
                f"displayed={r['value']:.0f} sign={sig} filed={filed:.0f}"
            )
    con.close()


if __name__ == "__main__":
    main()
