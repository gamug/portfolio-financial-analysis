"""T-156 phase 2 — the E3 and E4 STOP evidence (probe).

E3: the plan's rule compares an extension caption with the B6 standard label of an accepted D&A
element. The plan's own example ("Depreciation and amortization") equals no standard label; this
probe counts both readings and the per-filing matches.

E4: the identity fidelity (identity 1 has no checkable pair; identity 2 disagrees on 19 of 39
filings) and the identity-3 optional children.
"""

from __future__ import annotations

import csv
import json
import re
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
OI = G + "OperatingIncomeLoss"

STD_LABELS = {
    G + "DepreciationDepletionAndAmortization": "Depreciation, Depletion and Amortization",
    G + "DepreciationAmortizationAndAccretionNet": "Depreciation, Amortization and Accretion, Net",
    G + "DepreciationAndAmortization": "Depreciation, Depletion and Amortization, Nonproduction",
    G + "Depreciation": "Depreciation",
    G + "Depletion": "Depletion",
    G + "OtherDepreciationAndAmortization": "Other Depreciation and Amortization",
    G + "AmortizationOfIntangibleAssets": "Intangible Asset, Finite-Lived, Amortization Expense",
}


def norm(s: str) -> str:
    s = s.lower().replace("&", "and")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r" expense$", "", s)


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [
        r
        for r in csv.DictReader(OUT.joinpath("m1_rows.csv").open())
        if r["da_class"] == "da_extension_only"
    ]
    std = {norm(v) for v in STD_LABELS.values()}
    names = {norm(re.sub(r"(?<!^)(?=[A-Z])", " ", k.replace("us-gaap_", ""))) for k in STD_LABELS}

    print("=== E3: the 30, per company ===")
    counts = Counter()
    per_filing = {}
    for r in sorted(rows, key=lambda r: r["ticker"]):
        ev = json.loads(r["da_evidence"])
        fid = con.execute(
            "SELECT id FROM sec_filings WHERE accession_number=?", (r["accession"],)
        ).fetchone()["id"]
        hits_std = hits_name = 0
        caps = []
        for c in ev["extensions"]:
            lab = con.execute(
                "SELECT DISTINCT label FROM financial_facts WHERE filing_id=? AND concept=?",
                (fid, c),
            ).fetchone()["label"]
            caps.append((c, lab))
            if norm(lab) in std:
                hits_std += 1
            if norm(lab) in names:
                hits_name += 1
        counts["filings"] += 1
        counts["std_taken" if hits_std == 1 else "std_other"] += 1
        counts["name_taken" if hits_name == 1 else "name_other"] += 1
        per_filing[r["ticker"]] = {"std": hits_std, "name": hits_name, "captions": caps}
        if hits_name:
            print(f"  TAKEN(name) {r['ticker']:6} {[lab for _, lab in caps]}")
    print("counts:", dict(counts))
    print(
        "filings with more than one matching caption (either reading):",
        [t for t, v in per_filing.items() if v["std"] > 1 or v["name"] > 1],
    )
    OUT.joinpath("p2_e3_table.json").write_text(json.dumps(per_filing, indent=1))

    print("\n=== E4: identity 2 disagreements ===")
    id2 = con.execute(
        """
        SELECT e.filing_id, e.period_key, e.value eqm, pa.value pa, pb.value pb
        FROM financial_facts e
        JOIN financial_facts pa ON pa.filing_id=e.filing_id AND pa.period_key=e.period_key AND pa.concept=?
        JOIN financial_facts pb ON pb.filing_id=e.filing_id AND pb.period_key=e.period_key AND pb.concept=?
        WHERE e.concept=? AND e.value IS NOT NULL AND pa.value IS NOT NULL AND pb.value IS NOT NULL""",
        (PA, PB, EQM),
    ).fetchall()
    dis = [r for r in id2 if abs((r["pa"] + r["eqm"]) - r["pb"]) > dq01_tolerance(r["pb"], 3)]
    flip = [r for r in dis if abs((r["pa"] - r["eqm"]) - r["pb"]) <= dq01_tolerance(r["pb"], 3)]
    print(
        f"pairs {len(id2)}; disagree {len(dis)} over {len({r['filing_id'] for r in dis})} filings; "
        f"of those pb = pa - eqm exactly for {len(flip)} pairs"
    )
    print("agree rate", f"{len(id2) - len(dis)}/{len(id2)}")
    print(
        "disagreeing filings (sector):",
        Counter(
            con.execute(
                "SELECT a.ticker, s.name FROM assets a LEFT JOIN sectors s ON s.id=a.sector_id WHERE a.id=(SELECT asset_id FROM sec_filings WHERE id=?)",
                (r["filing_id"],),
            ).fetchone()["name"]
            for r in dis
        ),
    )

    print("\n=== E4: the identity-1/2/3 candidates among the 85 + 4 ===")
    for r in csv.DictReader(OUT.joinpath("m1_rows.csv").open()):
        if r["oi_class"] not in ("oi_absent_presentation", "oi_absent_other"):
            continue
        fid = con.execute(
            "SELECT id FROM sec_filings WHERE accession_number=?", (r["accession"],)
        ).fetchone()["id"]
        got = {
            x["concept"]
            for x in con.execute(
                "SELECT DISTINCT concept FROM financial_facts WHERE filing_id=? AND statement='income_statement'",
                (fid,),
            )
        }
        id1 = {PA, NONOP, INTDBT} <= got
        id2 = {PB, EQM, NONOP, INTDBT} <= got and PA not in got
        id3 = {G + "GrossProfit", G + "OperatingExpenses"} <= got
        if id1 or id2 or id3:
            print(
                f"  {r['ticker']:6} id1={id1} id2={id2} id3={id3} "
                f"gov={G + 'GovernmentAssistanceOperatingIncome' in got} otherop={G + 'OtherOperatingIncomeExpenseNet' in got}"
            )
    con.close()


if __name__ == "__main__":
    main()
