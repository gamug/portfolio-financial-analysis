"""T-156 phase 1 — the evidence summary for the STOP AND ASK.

Counts the flag distribution on the members' newest filings and lists the class table and the
cases that fit no class.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

FEAT = "/workspaces/thesis/scratch/t156/out/m1_features.json"
RAW = "/workspaces/thesis/scratch/t156/out/m1_classes_raw.json"


def main() -> None:
    feats = json.loads(Path(FEAT).read_text())
    withF = {a: f for a, f in feats.items() if f.get("has_filing")}
    print("members", len(feats), "with usable filing", len(withF))
    print("no usable filing:", [f["ticker"] for f in feats.values() if not f["has_filing"]])
    fl = Counter()
    for f in withF.values():
        for k in f["metric_flags"]:
            if "depreciation" in k:
                fl[k] += 1
    print("leverage D&A flag distribution on the members' newest filings:")
    for k, n in fl.most_common():
        print(f"   {n:4d} {k}")
    # every metric flag that names a da item at all
    fl2 = Counter(k for f in withF.values() for k in f["metric_flags"] if "ttm_not_built" in k)
    print("ttm_not_built flags:", dict(fl2))
    # class table
    rows = json.loads(Path(RAW).read_text())
    da = Counter(r.get("da_class") for r in rows if r.get("da_class"))
    oi = Counter(r.get("oi_class") for r in rows if r.get("oi_class"))
    print("D&A classes:", dict(da))
    print("OI classes:", dict(oi))
    print("empty ebitda:", sum(1 for f in withF.values() if f["ebitda"] is None))
    print(
        "financial-firm empties:",
        sum(1 for f in withF.values() if f["ebitda"] is None and f["financial_firm"]),
    )
    print(
        "non-financial empties:",
        sum(1 for f in withF.values() if f["ebitda"] is None and not f["financial_firm"]),
    )
    print("da_absent:", [r["ticker"] for r in rows if r.get("da_class") == "da_absent"])
    print(
        "da_refused_ttm:",
        [
            (r["ticker"], r["da_term"]["method"])
            for r in rows
            if r.get("da_class") == "da_refused_ttm"
        ],
    )
    print(
        "oi_absent_other:",
        [
            (r["ticker"], r["oi_term"]["method"])
            for r in rows
            if r.get("oi_class") == "oi_absent_other"
        ],
    )
    print(
        "UNCLASSIFIED:",
        [
            (r["ticker"], r["form"], r.get("da_evidence"))
            for r in rows
            if r.get("da_class") == "UNCLASSIFIED"
        ],
    )


if __name__ == "__main__":
    main()
