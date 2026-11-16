"""T-156 phase 1 — M2's risk measures.

1. da_income_statement_only: how often the income-statement and cash-flow values of the same D&A
   element agree (DQ-01's tolerance), on filings that file it on both.
2. da_element_with_other / caption_and_other: the "other" share of the wider element / extension,
   where the filer's own facts allow it.
3. oi_absent_presentation: the difference between the candidate EBIT (pretax income + interest
   expense) and the filed operating income, on filings that file both.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path
from statistics import median

from portfolio_common.db import in_clause

from fundamental_agent.fidelity import dq01_tolerance

DB = "/workspaces/thesis/scratch/t156/work.db"
OUT = Path("/workspaces/thesis/scratch/t156/out")
DA_WHERE = (
    "(lower(substr(concept, instr(concept,'_')+1)) LIKE '%depreciat%' "
    "OR lower(substr(concept, instr(concept,'_')+1)) LIKE '%amorti%' "
    "OR lower(substr(concept, instr(concept,'_')+1)) LIKE '%deplet%')"
)
INTEREST = (
    "us-gaap_InterestExpense",
    "us-gaap_InterestExpenseNonoperating",
    "us-gaap_InterestAndDebtExpense",
    "us-gaap_InterestExpenseDebt",
)
AGG = (
    "us-gaap_DepreciationDepletionAndAmortization",
    "us-gaap_DepreciationAmortizationAndAccretionNet",
    "us-gaap_DepreciationAndAmortization",
)


#: The "within one percent" band of the candidate-EBIT comparison.
ONE_POINT = 0.01


def p90(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, round(0.9 * (len(s) - 1)))]


def main() -> None:  # noqa: C901, PLR0912, PLR0915 - one block per risk measure
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out: dict = {}

    # 1. IS vs CF agreement for the same element
    pairs = con.execute(
        """
        SELECT a.filing_id, a.concept, a.period_key, a.value AS is_v, b.value AS cf_v
        FROM financial_facts a
        JOIN financial_facts b ON b.filing_id=a.filing_id AND b.concept=a.concept
             AND b.period_key=a.period_key AND b.statement='cash_flow'
        WHERE a.statement='income_statement' AND a.value IS NOT NULL AND b.value IS NOT NULL
          AND (lower(substr(a.concept, instr(a.concept,'_')+1)) LIKE '%depreciat%' OR lower(substr(a.concept, instr(a.concept,'_')+1)) LIKE '%amorti%' OR lower(substr(a.concept, instr(a.concept,'_')+1)) LIKE '%deplet%')
        """
    ).fetchall()
    agree = 0
    rel = []
    exact = 0
    for r in pairs:
        tol = dq01_tolerance(r["cf_v"], 2)
        d = abs(r["is_v"] - r["cf_v"])
        if d <= tol:
            agree += 1
        if r["is_v"] == r["cf_v"]:
            exact += 1
        if r["cf_v"]:
            rel.append(d / abs(r["cf_v"]))
    out["is_vs_cf"] = {
        "pairs": len(pairs),
        "exact": exact,
        "agree_dq01": agree,
        "agree_share": agree / len(pairs) if pairs else None,
        "median_rel_diff": median(rel) if rel else None,
        "p90_rel_diff": p90(rel),
    }

    # 2. the "other" share of the D&A carrier: the wider us-gaap element (da_element_with_other) or
    #    the "and other" extension (caption_and_other), against a narrow D&A in the same filing.
    carriers = {
        "AEP": ["us-gaap_UtilitiesOperatingExpenseDepreciationAndAmortization"],
        "IP": ["us-gaap_CostDepreciationAmortizationAndDepletion"],
    }
    and_other = re.compile(r"and\s*other", re.IGNORECASE)
    da_word = re.compile(r"depreciat|amorti|deplet", re.IGNORECASE)
    rows = list(csv.DictReader(OUT.joinpath("m1_rows.csv").open()))
    shares = {}
    for r in rows:
        if r["da_class"] == "da_extension_only" and r["da_evidence"]:
            ev = json.loads(r["da_evidence"])
            if not ev.get("caption_and_other"):
                continue
        elif r["da_class"] != "da_element_with_other":
            continue
        fid = con.execute(
            "SELECT id FROM sec_filings WHERE accession_number=?", (r["accession"],)
        ).fetchone()
        if fid is None:
            continue
        fid = fid["id"]
        facts = con.execute(
            "SELECT concept, statement, period_key, value, label FROM financial_facts "
            "WHERE filing_id=? AND value IS NOT NULL",
            (fid,),
        ).fetchall()
        wide = [
            f
            for f in facts
            if f["concept"] in carriers.get(r["ticker"], [])
            or (
                r["da_class"] == "da_extension_only"
                and not f["concept"].startswith("us-gaap_")
                and and_other.search(f["concept"].split("_", 1)[-1])
                and (
                    da_word.search(f["concept"].split("_", 1)[-1])
                    or da_word.search(f["label"] or "")
                )
            )
        ]
        for w in wide:
            narrow = next(
                (
                    f["value"]
                    for f in facts
                    if f["concept"] in AGG and f["period_key"] == w["period_key"]
                ),
                None,
            )
            if narrow is None:
                narrow = next(
                    (
                        f["value"]
                        for f in facts
                        if f["concept"] == "us-gaap_Depreciation"
                        and f["period_key"] == w["period_key"]
                    ),
                    None,
                )
            if narrow is not None and w["value"]:
                shares.setdefault(r["ticker"], []).append(
                    {
                        "concept": w["concept"],
                        "period_key": w["period_key"],
                        "wide": w["value"],
                        "narrow": narrow,
                        "other_share": (w["value"] - narrow) / w["value"],
                    }
                )
            else:
                shares.setdefault(r["ticker"], []).append(
                    {
                        "concept": w["concept"],
                        "period_key": w["period_key"],
                        "wide": w["value"],
                        "narrow": None,
                        "other_share": None,
                    }
                )
    out["other_share"] = shares

    # 3. the candidate EBIT (pretax + interest) against the filed operating income
    oi_sql = "\n".join(
        [
            "SELECT a.filing_id, a.period_key, a.value AS oi, p.value AS pretax, "
            "i.value AS interest, i.concept AS interest_concept",
            "FROM financial_facts a",
            "JOIN financial_facts p ON p.filing_id=a.filing_id AND p.period_key=a.period_key",
            "     AND p.concept LIKE '%IncomeLossFromContinuingOperationsBeforeIncomeTaxes%'",
            "JOIN financial_facts i ON i.filing_id=a.filing_id AND i.period_key=a.period_key",
            "     AND i.concept IN " + in_clause(INTEREST),
            "WHERE a.concept LIKE '%OperatingIncomeLoss' AND a.value IS NOT NULL",
            "  AND p.value IS NOT NULL AND i.value IS NOT NULL",
        ]
    )
    q = con.execute(oi_sql, INTEREST).fetchall()
    diffs, reldiffs, interest_neg = [], [], 0
    for r in q:
        cand = r["pretax"] + r["interest"]
        d = cand - r["oi"]
        diffs.append(d)
        if r["oi"]:
            reldiffs.append(d / abs(r["oi"]))
        if r["interest"] < 0:
            interest_neg += 1
    out["oi_candidate"] = {
        "pairs": len(q),
        "interest_negative": interest_neg,
        "median_abs_diff": median([abs(x) for x in diffs]) if diffs else None,
        "p90_abs_diff": p90([abs(x) for x in diffs]),
        "median_rel_diff": median(reldiffs) if reldiffs else None,
        "p90_rel_diff": p90(reldiffs),
        "within_1pct": sum(1 for x in reldiffs if abs(x) <= ONE_POINT),
        # the same candidate with interest added back as a positive expense (diagnostic only)
        "as_expense_median_rel_diff": median(
            [(r["pretax"] + abs(r["interest"]) - r["oi"]) / abs(r["oi"]) for r in q if r["oi"]]
        )
        if q
        else None,
        "as_expense_p90_rel_diff": p90(
            [(r["pretax"] + abs(r["interest"]) - r["oi"]) / abs(r["oi"]) for r in q if r["oi"]]
        )
        if q
        else None,
    }
    interest_sql = "SELECT COUNT(*) FROM financial_facts WHERE concept IN " + in_clause(INTEREST)  # noqa: S608
    out["interest_sign"] = {
        "facts": con.execute(interest_sql + " AND value IS NOT NULL", INTEREST).fetchone()[0],
        "negative": con.execute(interest_sql + " AND value < 0", INTEREST).fetchone()[0],
    }
    OUT.joinpath("m2_risk.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps({k: v for k, v in out.items() if k != "other_share"}, indent=1))
    print("other_share measured for:", list(out["other_share"]))
    for t, rows2 in out["other_share"].items():
        for x in rows2:
            ns = "n/a" if x["narrow"] is None else f"{x['narrow']:.0f}"
            os_ = "cannot" if x["other_share"] is None else f"{x['other_share']:.3f}"
            print(
                f"   {t} {x['concept']} {x['period_key']} wide={x['wide']:.0f} narrow={ns} other={os_}"
            )
    con.close()


if __name__ == "__main__":
    main()
