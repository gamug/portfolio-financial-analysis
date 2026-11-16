"""T-156 phase 1 — per class at 2026-09-22: the companies and the 20 largest by market cap on D."""

from __future__ import annotations

import csv
import json
from pathlib import Path

OUT = Path("/workspaces/thesis/scratch/t156/out")


def main() -> None:
    rows = list(csv.DictReader(OUT.joinpath("m1_rows.csv").open()))
    per: dict[str, dict] = {}
    for key in ("da_class", "oi_class"):
        for r in rows:
            c = r[key]
            if not c:
                continue
            d = per.setdefault(c, {"companies": set(), "top20_by_market_cap": []})
            d["companies"].add(r["ticker"])
            mc = r["market_cap"]
            d["top20_by_market_cap"].append(
                (r["ticker"], None if mc in ("", "None") else float(mc))
            )
    out = {}
    for c, d in per.items():
        top = sorted(d["top20_by_market_cap"], key=lambda t: (t[1] is None, -(t[1] or 0)))[:20]
        out[c] = {
            "companies": sorted(d["companies"]),
            "n_companies": len(d["companies"]),
            "top20_by_market_cap": [{"ticker": t, "mcap": v} for t, v in top],
        }
    OUT.joinpath("m1_class_members.json").write_text(json.dumps(out, indent=1))
    print(json.dumps({c: v["n_companies"] for c, v in out.items()}, indent=1))


if __name__ == "__main__":
    main()
