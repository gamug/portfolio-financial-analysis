"""T-156 phase 1 — inspect the empty-EBITDA filings and their flag signatures."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

AUDIT = "/workspaces/thesis/scratch/t156/out/p4_audit_MS.json"


def main() -> None:
    data = json.loads(Path(AUDIT).read_text())["members"]
    empties = {
        a: e for a, e in data.items() if e.get("has_usable_filing") and e.get("ebitda") is None
    }
    print("empties", len(empties))
    print("no usable filing", sum(1 for e in data.values() if not e["has_usable_filing"]))
    print("financial_firm empties", sum(1 for e in empties.values() if e["financial_firm"]))
    print("reit empties", sum(1 for e in empties.values() if e["reit"]))
    sig = Counter()
    for e in empties.values():
        ij = e.get("inputs") or {}
        flags = tuple(sorted(k for k in ij if k.startswith("flag_")))
        sig[(e["form"][:4], flags)] += 1
    for (form, flags), n in sig.most_common():
        print(f"{n:4d}  {form}  {flags}")
    print("=== input presence among empties ===")
    c = Counter()
    for e in empties.values():
        ij = e.get("inputs") or {}
        c[(ij.get("operating_income") is None, ij.get("depreciation_amortization") is None)] += 1
    print("(oi_missing, da_missing) ->", dict(c))
    print("=== annualization stamps ===")
    print(
        Counter(
            (
                json.dumps(
                    {k: v for k, v in (e.get("inputs") or {}).items() if "annual" in k},
                    sort_keys=True,
                )
            )
            for e in empties.values()
        )
    )
    print("=== example empties (first 8) ===")
    for a, e in list(empties.items())[:8]:
        ij = e.get("inputs") or {}
        print(
            a,
            e["ticker"],
            e["form"],
            e["accession"],
            e["period_end"],
            e["type"],
            "ebitda",
            e["ebitda"],
            "ndte",
            e.get("net_debt_to_ebitda"),
        )
        print("   inputs oi/da:", ij.get("operating_income"), ij.get("depreciation_amortization"))
        print("   flags:", sorted(k for k in ij if k.startswith("flag_")))
        print("   da concepts:", sorted({f["concept"] for f in e.get("da_facts", [])}))
        print(
            "   oi facts:",
            [(f["concept"], f["period_key"], f["value"]) for f in e.get("oi_facts", [])][:3],
        )


if __name__ == "__main__":
    main()
