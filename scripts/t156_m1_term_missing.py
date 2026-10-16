"""T-156 phase 1 — list the term-missing cases with everything that decides the class."""

from __future__ import annotations

import json
from pathlib import Path

FEAT = "/workspaces/thesis/scratch/t156/out/m1_features.json"


def main() -> None:
    feats = json.loads(Path(FEAT).read_text())
    for _a, f in sorted(feats.items(), key=lambda kv: kv[1]["ticker"]):
        if not f.get("has_filing") or f.get("ebitda") is not None or f["financial_firm"]:
            continue
        dam = not f["da_term"]["present"]
        oim = not f["oi_term"]["present"]
        if not (dam or oim):
            continue
        print("=" * 110)
        print(
            f"{f['ticker']:6s} {f['form']:5s} {f['acc']} pe={f['period_end']} "
            f"DAM={'Y' if dam else '.'} OIM={'Y' if oim else '.'}"
        )
        if dam:
            print(
                f"   DA term: method={f['da_term']['method']} concept={f['da_term_concept']} flags={f['da_term']['flags']}"
            )
            print(f"   DA metric flags: {[x for x in f['metric_flags'] if 'depreciation' in x]}")
            print(f"   DA by column: {json.dumps(f['da_by_col'])}")
            print(
                f"   DA extension: {f['da_ext']}  other-gaap: {f['da_other']}  agg: {f['da_agg']}  child: {f['da_child']}"
            )
        if oim:
            print(
                f"   OI term: method={f['oi_term']['method']} concept={f['oi_term_concept']} has_oi_fact={f['has_oi_fact']}"
            )
            print(f"   income subtotals: {f['subtotals']}")


if __name__ == "__main__":
    main()
