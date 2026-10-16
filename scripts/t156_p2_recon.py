"""T-156 phase 2 — reconnaissance over work.db (read-only) for E1, E2, E4.

Not the measurement script: a probe that counts what each E-item could touch.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

DB = "/workspaces/thesis/scratch/t156/work.db"
OUT = Path("/workspaces/thesis/scratch/t156/out")
DATE = "2026-09-22"

DA_RE = re.compile(r"depreciat|amorti|deplet", re.IGNORECASE)
AGG = (
    "us-gaap_DepreciationDepletionAndAmortization",
    "us-gaap_DepreciationAmortizationAndAccretionNet",
    "us-gaap_DepreciationAndAmortization",
)
CHILDREN = (
    "us-gaap_Depreciation",
    "us-gaap_Depletion",
    "us-gaap_OtherDepreciationAndAmortization",
    "us-gaap_AmortizationOfIntangibleAssets",
)
PRETAX_A = "us-gaap_IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"
PRETAX_B = "us-gaap_IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"


def da_rows(con, fid, statement):
    return con.execute(
        "SELECT concept, period_key, value, label FROM financial_facts WHERE filing_id=? AND "
        "statement=? AND (lower(substr(concept, instr(concept,'_')+1)) LIKE '%depreciat%' "
        "OR lower(substr(concept, instr(concept,'_')+1)) LIKE '%amorti%' "
        "OR lower(substr(concept, instr(concept,'_')+1)) LIKE '%deplet%')",
        (fid, statement),
    ).fetchall()


def main() -> None:  # noqa: C901, PLR0912, PLR0915 - one block per E-item
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = list(csv.DictReader(OUT.joinpath("m1_rows.csv").open()))

    print("=== E1: every filing with IS D&A facts and no CF D&A fact (DB-wide) ===")
    allf = con.execute("SELECT id FROM sec_filings").fetchall()
    e1 = {"filings": 0, "with_aggregate_on_is": 0, "children_only_on_is": 0, "none": 0}
    for r in allf:
        cf = da_rows(con, r["id"], "cash_flow")
        if cf:
            continue
        ist = da_rows(con, r["id"], "income_statement")
        if not ist:
            continue
        e1["filings"] += 1
        concepts = {x["concept"] for x in ist}
        if concepts & set(AGG):
            e1["with_aggregate_on_is"] += 1
        elif concepts & set(CHILDREN):
            e1["children_only_on_is"] += 1
        else:
            e1["none"] += 1
    print(json.dumps(e1))

    print("\n=== E2: the depreciation-only filers (60 + 7) ===")
    e2 = Counter()
    detail = []
    for r in rows:
        if r["da_class"] not in ("da_refused_depreciation_only", "ttm_refused"):
            continue
        fid = con.execute(
            "SELECT id FROM sec_filings WHERE accession_number=?", (r["accession"],)
        ).fetchone()["id"]
        cf = da_rows(con, fid, "cash_flow")
        ist = da_rows(con, fid, "income_statement")
        cf_concepts = {x["concept"] for x in cf}
        is_concepts = {x["concept"] for x in ist}
        cf_outside = {c for c in cf_concepts if c not in CHILDREN}
        is_amort = {c for c in is_concepts if c in CHILDREN and c != "us-gaap_Depreciation"}
        key = (
            "us-gaap_Depreciation" in cf_concepts,
            bool(cf_outside),
            bool(is_amort),
        )
        e2[key] += 1
        detail.append(
            {
                "ticker": r["ticker"],
                "form": r["form"],
                "dep_on_cf": "us-gaap_Depreciation" in cf_concepts,
                "cf_outside": sorted(cf_outside),
                "is_amort": sorted(is_amort),
            }
        )
    print("(dep_on_cf, cf_has_outside_lines, is_has_amort) -> count:", dict(e2))
    for d in detail:
        if d["is_amort"]:
            print("   ", d)

    print("\n=== E4: the operating-income-absent filers (85 + 4) ===")
    e4 = Counter()
    e4d = []
    for r in rows:
        if r["oi_class"] not in ("oi_absent_presentation", "oi_absent_other"):
            continue
        fid = con.execute(
            "SELECT id FROM sec_filings WHERE accession_number=?", (r["accession"],)
        ).fetchone()["id"]
        facts = con.execute(
            "SELECT concept, period_key FROM financial_facts WHERE filing_id=? AND statement='income_statement' "
            "AND concept IN (?,?,?,?,?,?,?,?)",
            (
                fid,
                PRETAX_A,
                PRETAX_B,
                "us-gaap_NonoperatingIncomeExpense",
                "us-gaap_InterestAndDebtExpense",
                "us-gaap_IncomeLossFromEquityMethodInvestments",
                "us-gaap_GrossProfit",
                "us-gaap_OperatingExpenses",
                "us-gaap_OtherOperatingIncomeExpenseNet",
            ),
        ).fetchall()
        present = {x["concept"] for x in facts}
        key = (
            PRETAX_A in present or PRETAX_B in present,
            "us-gaap_NonoperatingIncomeExpense" in present,
            "us-gaap_InterestAndDebtExpense" in present,
            "us-gaap_GrossProfit" in present,
            "us-gaap_OperatingExpenses" in present,
        )
        e4[key] += 1
        e4d.append({"ticker": r["ticker"], "class": r["oi_class"], "present": sorted(present)})
    print("(pretax, nonop, interest_debt, grossprofit, opexp) -> count:")
    for k, n in e4.most_common():
        print(f"   {n:4d} {k}")
    ident1 = sum(
        1
        for d in e4d
        if PRETAX_A in d["present"]
        and "us-gaap_NonoperatingIncomeExpense" in d["present"]
        and "us-gaap_InterestAndDebtExpense" in d["present"]
    )
    ident2only = sum(
        1
        for d in e4d
        if PRETAX_A not in d["present"]
        and PRETAX_B in d["present"]
        and "us-gaap_IncomeLossFromEquityMethodInvestments" in d["present"]
        and "us-gaap_NonoperatingIncomeExpense" in d["present"]
        and "us-gaap_InterestAndDebtExpense" in d["present"]
    )
    ident3 = sum(
        1
        for d in e4d
        if "us-gaap_GrossProfit" in d["present"] and "us-gaap_OperatingExpenses" in d["present"]
    )
    print(
        f"identity 1 directly: {ident1}; identity 2 forming the pre-tax term: {ident2only}; identity 3 (gross profit + opex): {ident3}"
    )
    OUT.joinpath("p2_recon_e2.json").write_text(json.dumps(detail, indent=1))
    OUT.joinpath("p2_recon_e4.json").write_text(json.dumps(e4d, indent=1))
    con.close()


if __name__ == "__main__":
    main()
