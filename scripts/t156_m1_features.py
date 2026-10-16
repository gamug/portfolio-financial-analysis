"""T-156 phase 1 — M1 features for every empty EBITDA at 2026-09-22 (and every member).

The EBITDA terms on a 10-Q are read on the ``ttm`` basis, and the ``net_debt_to_ebitda`` row's
provenance records them (ITEMS["net_debt_to_ebitda"] names both on ``ttm``). On a 10-K the same
entries fall back to the filing's own period column.

Output: out/m1_features.json  (one entry per member) and a console summary.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

DB = "/workspaces/thesis/scratch/t156/work.db"
AUDIT = "/workspaces/thesis/scratch/t156/out/p4_audit_MS.json"
DATE = "2026-09-22"

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


def term_of(entry: dict | None, form: str) -> dict:
    """(present, method, flags) of one ndte provenance item entry.

    On a 10-Q the metric reads ``ttm`` (``FilingContext.real_ttm``: ``ytd`` / ``quarters`` only --
    an ``x4`` or a ``refused`` flow is not in it), so the entry is present only when it carries one
    of those methods. On a 10-K the same entry falls back to the filing's own period resolution,
    and it is present when that resolution found a concept.
    """
    if entry is None:
        return {"present": False, "method": None, "flags": [], "concept": None}
    method = entry.get("method")
    present = method in ("ytd", "quarters") if form == "10-Q" else entry.get("concept") is not None
    return {
        "present": present,
        "method": method,
        "flags": entry.get("flow_flags") or [],
        "concept": entry.get("concept"),
    }


def main() -> None:  # noqa: PLR0915 - one pass over the signals
    audit = json.loads(Path(AUDIT).read_text())["members"]
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    feats: dict[str, dict] = {}
    for a, e in audit.items():
        f = {
            "ticker": e["ticker"],
            "type": e["type"],
            "financial_firm": e["financial_firm"],
            "has_filing": e["has_usable_filing"],
            "ebitda": e["ebitda"],
        }
        if not e["has_usable_filing"]:
            feats[a] = f
            continue
        f.update(form=e["form"], acc=e["accession"], period_end=e["period_end"])
        ij = e.get("inputs") or {}
        f["oi_input"] = ij.get("operating_income")
        f["da_input"] = ij.get("depreciation_amortization")
        f["metric_flags"] = sorted(k for k in ij if k.startswith("flag_"))
        # the ndte row's provenance: the ebitda terms on their real basis
        row = con.execute(
            "SELECT value, inputs_json FROM fundamental_metrics WHERE filing_id=? AND "
            "metric_group='leverage' AND metric_name='net_debt_to_ebitda'",
            (e["filing_id"],),
        ).fetchone()
        items = (
            (json.loads(row["inputs_json"] or "{}").get("provenance") or {}).get("items")
        ) or {}
        f["ndte_value"] = row["value"] if row else None
        f["da_term"] = term_of((items.get("depreciation_amortization") or [{}])[0], e["form"])
        f["oi_term"] = term_of((items.get("operating_income") or [{}])[0], e["form"])
        f["da_term_concept"] = (items.get("depreciation_amortization") or [{}])[0].get("concept")
        f["oi_term_concept"] = (items.get("operating_income") or [{}])[0].get("concept")
        # D&A facts by column
        daf = [x for x in e.get("da_facts", []) if x["statement"] == "cash_flow"]
        f["da_by_col"] = {}
        for x in daf:
            f["da_by_col"].setdefault(x["period_key"], []).append(x["concept"])
        isdaf = [x for x in e.get("da_facts", []) if x["statement"] == "income_statement"]
        bsdaf = [x for x in e.get("da_facts", []) if x["statement"] == "balance_sheet"]
        f["da_is"] = sorted({x["concept"] for x in isdaf})
        f["da_bs"] = sorted({x["concept"] for x in bsdaf})
        concepts = sorted({c for v in f["da_by_col"].values() for c in v})
        f["da_cf_concepts"] = concepts
        f["da_agg"] = [c for c in concepts if c in AGG]
        f["da_child"] = [c for c in concepts if c in CHILD]
        f["da_ext"] = [c for c in concepts if not c.startswith("us-gaap_")]
        f["da_other"] = [
            c for c in concepts if c not in AGG and c not in CHILD and c.startswith("us-gaap_")
        ]
        f["has_oi_fact"] = bool(e.get("oi_facts"))
        f["subtotals"] = sorted(
            {x["concept"].split("_", 1)[-1] for x in e.get("income_subtotals", [])}
        )
        feats[a] = f

    Path("/workspaces/thesis/scratch/t156/out/m1_features.json").write_text(
        json.dumps(feats, indent=1, default=str)
    )
    # summary
    empties = {a: f for a, f in feats.items() if f.get("has_filing") and f.get("ebitda") is None}
    print("members", len(feats), "with filing", sum(1 for f in feats.values() if f["has_filing"]))
    print("empties", len(empties))
    print("financial-firm empties", sum(1 for f in empties.values() if f["financial_firm"]))
    nonfin = {a: f for a, f in empties.items() if not f["financial_firm"]}
    print("--- term basis (non-financial) ---")
    print(
        "da_term missing",
        sum(1 for f in nonfin.values() if not f["da_term"]["present"]),
        "oi_term missing",
        sum(1 for f in nonfin.values() if not f["oi_term"]["present"]),
        "both",
        sum(
            1
            for f in nonfin.values()
            if not f["da_term"]["present"] and not f["oi_term"]["present"]
        ),
        "neither",
        sum(1 for f in nonfin.values() if f["da_term"]["present"] and f["oi_term"]["present"]),
    )
    print("--- period-input basis (all empties incl. financial) ---")
    print(
        "da_input missing",
        sum(1 for f in empties.values() if f["da_input"] is None),
        "oi_input missing",
        sum(1 for f in empties.values() if f["oi_input"] is None),
    )
    print("--- da flow methods among term-missing 10-Qs ---")
    c = Counter(
        (f["form"], f["da_term"]["method"], tuple(f["da_term"]["flags"]))
        for f in nonfin.values()
        if not f["da_term"]["present"]
    )
    for k, n in c.most_common():
        print(f"   {n:3d} {k}")
    print("--- the 'neither' case(s): empty ebitda but both terms present ---")
    for a, f in nonfin.items():
        if f["da_term"]["present"] and f["oi_term"]["present"]:
            print(
                a,
                f["ticker"],
                f["form"],
                f["acc"],
                "ebitda",
                f["ebitda"],
                "ndte",
                f["ndte_value"],
                "da_term",
                f["da_term"],
                "oi_term",
                f["oi_term"],
            )
    con.close()


if __name__ == "__main__":
    main()
