"""T-156 phase 2 — the E1 both-statements agreement and the E4 identity fidelity (probe)."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

from fundamental_agent.fidelity import dq01_tolerance

DB = "/workspaces/thesis/scratch/t156/work.db"
OUT = Path("/workspaces/thesis/scratch/t156/out")
G = "us-gaap_"
PA = (
    G
    + "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"
)
PB = (
    G
    + "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"
)
NONOP = G + "NonoperatingIncomeExpense"
INTDBT = G + "InterestAndDebtExpense"
EQM = G + "IncomeLossFromEquityMethodInvestments"
GP = G + "GrossProfit"
OPEX = G + "OperatingExpenses"
GOV = G + "GovernmentAssistanceOperatingIncome"
OTHEROP = G + "OtherOperatingIncomeExpenseNet"
OI = G + "OperatingIncomeLoss"


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = {}

    # E1: the same D&A element filed on both statements
    rows = con.execute(
        """
        SELECT a.filing_id, a.concept, a.period_key, a.value isv, b.value cfv
        FROM financial_facts a
        JOIN financial_facts b ON b.filing_id=a.filing_id AND b.concept=a.concept
             AND b.period_key=a.period_key AND b.statement='cash_flow'
        WHERE a.statement='income_statement' AND a.value IS NOT NULL AND b.value IS NOT NULL
          AND (lower(substr(a.concept, instr(a.concept,'_')+1)) LIKE '%depreciat%'
            OR lower(substr(a.concept, instr(a.concept,'_')+1)) LIKE '%amorti%'
            OR lower(substr(a.concept, instr(a.concept,'_')+1)) LIKE '%deplet%')
        """
    ).fetchall()
    agree = [r for r in rows if abs(r["isv"] - r["cfv"]) <= dq01_tolerance(r["cfv"], 2)]
    dis = [r for r in rows if abs(r["isv"] - r["cfv"]) > dq01_tolerance(r["cfv"], 2)]
    out["e1"] = {"pairs": len(rows), "agree": len(agree), "disagree": len(dis)}
    print("E1 both-statements pairs", len(rows), "agree", len(agree))
    print("  disagreeing concepts:", Counter(r["concept"] for r in dis).most_common(12))
    print("  disagreeing filings:", len({r["filing_id"] for r in dis}))

    # E4 identity fidelity
    def pairs(sql, params=()):
        return con.execute(sql, params).fetchall()

    id1 = pairs(
        """
        SELECT oi.filing_id, oi.period_key, oi.value oi, pa.value pa, n.value nonop, i.value intdbt
        FROM financial_facts oi
        JOIN financial_facts pa ON pa.filing_id=oi.filing_id AND pa.period_key=oi.period_key AND pa.concept=?
        JOIN financial_facts n ON n.filing_id=oi.filing_id AND n.period_key=oi.period_key AND n.concept=?
        JOIN financial_facts i ON i.filing_id=oi.filing_id AND i.period_key=oi.period_key AND i.concept=?
        WHERE oi.concept=? AND oi.value IS NOT NULL AND pa.value IS NOT NULL
          AND n.value IS NOT NULL AND i.value IS NOT NULL""",
        (PA, NONOP, INTDBT, OI),
    )
    ok1 = sum(
        1
        for r in id1
        if abs((r["oi"] + r["nonop"] - r["intdbt"]) - r["pa"]) <= dq01_tolerance(r["pa"], 4)
    )
    out["id1"] = {"pairs": len(id1), "agree": ok1}
    print("identity 1 pairs", len(id1), "agree", ok1)

    id2 = pairs(
        """
        SELECT e.filing_id, e.period_key, e.value eqm, pa.value pa, pb.value pb
        FROM financial_facts e
        JOIN financial_facts pa ON pa.filing_id=e.filing_id AND pa.period_key=e.period_key AND pa.concept=?
        JOIN financial_facts pb ON pb.filing_id=e.filing_id AND pb.period_key=e.period_key AND pb.concept=?
        WHERE e.concept=? AND e.value IS NOT NULL AND pa.value IS NOT NULL AND pb.value IS NOT NULL""",
        (PA, PB, EQM),
    )
    ok2 = sum(1 for r in id2 if abs((r["pa"] + r["eqm"]) - r["pb"]) <= dq01_tolerance(r["pb"], 3))
    dis2 = [r for r in id2 if abs((r["pa"] + r["eqm"]) - r["pb"]) > dq01_tolerance(r["pb"], 3)]
    out["id2"] = {
        "pairs": len(id2),
        "agree": ok2,
        "disagree": len(dis2),
        "disagree_filings": len({r["filing_id"] for r in dis2}),
    }
    print(
        "  id2 disagreeing filings:",
        len({r["filing_id"] for r in dis2}),
        "of",
        len({r["filing_id"] for r in id2}),
    )
    for r in dis2[:10]:
        print(
            "    dis",
            r["filing_id"],
            r["period_key"],
            "eqm",
            r["eqm"],
            "pa",
            r["pa"],
            "pb",
            r["pb"],
            "diff",
            r["pa"] + r["eqm"] - r["pb"],
        )
    print("identity 2 pairs", len(id2), "agree", ok2)

    id3 = pairs(
        """
        SELECT oi.filing_id, oi.period_key, oi.value oiv, gp.value gpv, ox.value oxv
        FROM financial_facts oi
        JOIN financial_facts gp ON gp.filing_id=oi.filing_id AND gp.period_key=oi.period_key AND gp.concept=?
        JOIN financial_facts ox ON ox.filing_id=oi.filing_id AND ox.period_key=oi.period_key AND ox.concept=?
        WHERE oi.concept=? AND oi.value IS NOT NULL AND gp.value IS NOT NULL AND ox.value IS NOT NULL""",
        (GP, OPEX, OI),
    )
    ok3 = sum(
        1 for r in id3 if abs((r["gpv"] - r["oxv"]) - r["oiv"]) <= dq01_tolerance(r["oiv"], 3)
    )
    out["id3"] = {"pairs": len(id3), "agree": ok3}
    print("identity 3 pairs (no optional children)", len(id3), "agree", ok3)

    # identity 3 with the optional children present
    id3b = pairs(
        """
        SELECT oi.filing_id, oi.value oiv, gp.value gpv, ox.value oxv,
               COALESCE(gov.value,0) gov, COALESCE(oth.value,0) oth
        FROM financial_facts oi
        JOIN financial_facts gp ON gp.filing_id=oi.filing_id AND gp.period_key=oi.period_key AND gp.concept=?
        JOIN financial_facts ox ON ox.filing_id=oi.filing_id AND ox.period_key=oi.period_key AND ox.concept=?
        LEFT JOIN financial_facts gov ON gov.filing_id=oi.filing_id AND gov.period_key=oi.period_key AND gov.concept=?
        LEFT JOIN financial_facts oth ON oth.filing_id=oi.filing_id AND oth.period_key=oi.period_key AND oth.concept=?
        WHERE oi.concept=? AND oi.value IS NOT NULL AND gp.value IS NOT NULL AND ox.value IS NOT NULL""",
        (GP, OPEX, GOV, OTHEROP, OI),
    )
    ok3b = sum(
        1
        for r in id3b
        if abs((r["gpv"] - r["oxv"] + r["gov"] + r["oth"]) - r["oiv"])
        <= dq01_tolerance(r["oiv"], 5)
    )
    out["id3_with_optionals"] = {"pairs": len(id3b), "agree": ok3b}
    print("identity 3 with the optional children (absent=0)", len(id3b), "agree", ok3b)
    OUT.joinpath("p2_fidelity_recon.json").write_text(json.dumps(out, indent=1))
    con.close()


if __name__ == "__main__":
    main()
