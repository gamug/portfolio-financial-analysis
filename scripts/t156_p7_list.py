"""T-156 phase 1 — every empty at 2026-09-22 with the facts that decide its class.

Prints: ticker, form, oi/da period-input presence, the D&A flags, the cash-flow D&A-nature
concepts, and whether an OperatingIncomeLoss fact exists.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

DB = "/workspaces/thesis/scratch/t156/work.db"
AUDIT = "/workspaces/thesis/scratch/t156/out/p4_audit_MS.json"
DA_RE = re.compile(r"depreciat|amorti|deplet", re.IGNORECASE)
AGG = {
    "us-gaap_DepreciationDepletionAndAmortization",
    "us-gaap_DepreciationAmortizationAndAccretionNet",
    "us-gaap_DepreciationAndAmortization",
}
CHILD = {
    "us-gaap_Depreciation",
    "us-gaap_Depletion",
    "us-gaap_OtherDepreciationAndAmortization",
    "us-gaap_AmortizationOfIntangibleAssets",
}


def main() -> None:
    audit = json.loads(Path(AUDIT).read_text())["members"]
    isEmpty = lambda e: e.get("has_usable_filing") and e.get("ebitda") is None  # noqa: E731
    empties = {a: e for a, e in audit.items() if isEmpty(e)}
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rowsout = []
    for a, e in sorted(empties.items(), key=lambda kv: kv[1]["ticker"]):
        ij = e.get("inputs") or {}
        oi = ij.get("operating_income")
        da = ij.get("depreciation_amortization")
        flags = sorted(k[len("flag_") :] for k in ij if k.startswith("flag_"))
        daf = [f for f in e.get("da_facts", []) if f["statement"] == "cash_flow"]
        isf = [f for f in e.get("da_facts", []) if f["statement"] == "income_statement"]
        concepts = sorted({f["concept"] for f in daf})
        agg = [c for c in concepts if c in AGG]
        child = [c for c in concepts if c in CHILD]
        ext = [c for c in concepts if not c.startswith("us-gaap_")]
        other = [
            c for c in concepts if c not in AGG and c not in CHILD and c.startswith("us-gaap_")
        ]
        oifacts = e.get("oi_facts", [])
        rowsout.append(
            {
                "ticker": e["ticker"],
                "asset": a,
                "form": e["form"],
                "acc": e.get("accession"),
                "type": e["type"],
                "fin": e["financial_firm"],
                "oi": oi,
                "da": da,
                "da_flags": [f for f in flags if "depreciation" in f or "da_" in f],
                "ttm_flags": [f for f in flags if "ttm" in f],
                "agg": agg,
                "child": child,
                "ext": ext,
                "other": other,
                "is_da": sorted({f["concept"] for f in isf}),
                "has_oi_fact": len(oifacts) > 0,
            }
        )
    # print a compact table
    print(
        f"{'ticker':<6} {'form':<5} {'oi':>3} {'da':>3} {'fin':>3} "
        f"{'daflags':<45} {'agg/child':<40} {'ext':<45} {'other':<45} oifact"
    )
    for r in rowsout:
        print(
            f"{r['ticker']:<6} {r['form']:<5} "
            f"{'-' if r['oi'] is None else 'P':>3} {'-' if r['da'] is None else 'P':>3} "
            f"{'F' if r['fin'] else '.':>3} "
            f"{','.join(f.replace('depreciation_amortization_', '') for f in r['da_flags'])[:45]:<45} "
            f"{','.join(c.split('_', 1)[-1] for c in r['agg'] + r['child'])[:40]:<40} "
            f"{','.join(r['ext'])[:45]:<45} {','.join(c.split('_', 1)[-1] for c in r['other'])[:45]:<45} "
            f"{'Y' if r['has_oi_fact'] else '.'}"
        )
    Path("/workspaces/thesis/scratch/t156/out/p7_empties.json").write_text(
        json.dumps(rowsout, indent=1)
    )
    con.close()


if __name__ == "__main__":
    main()
