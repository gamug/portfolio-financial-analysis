#!/usr/bin/env python3
"""V5's sampler: 25 companies, one per stratum, by a seeded random draw (T-143 part A).

    uv run python scripts/pilot_a/v5_sampler.py \
        --db /workspaces/thesis/pilot_a/financial.db \
        --universe-db /workspaces/thesis/data/universe_history_gics.db \
        --company-types docs/checklist_sec/company_types.csv \
        --seed 20261018 --out /workspaces/thesis/scratch/t143a/out/p3_v5_sample.json

Read-only. The plan's strata and their sizes::

    operating 8 · Article 9 2 · Article 7 2 · other financial 2 · REIT 2
    regulated utility 2 · multi-class 2 · negative equity 2 · leavers 3

The types come from the tracked company-type table (``docs/checklist_sec/company_types.csv``:
``type``, ``reit``, ``negative_equity_latest``, ``multi_class_listed``, GICS sector); "leaver" comes
from the membership window (a line that left the index); "regulated utility" is the GICS sector
``Utilities``. The strata are **disjoint** by the precedence written below, so a company is drawn once.
The draw is a seeded ``random.Random`` over each stratum's candidates **sorted by CIK**, so the same
seed always gives the same 25 companies.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sqlite3
from pathlib import Path
from typing import Any

#: The plan's strata: name -> how many companies to draw.
STRATA: dict[str, int] = {
    "operating": 8,
    "article_9": 2,
    "article_7": 2,
    "other_financial": 2,
    "reit": 2,
    "regulated_utility": 2,
    "multi_class": 2,
    "negative_equity": 2,
    "leavers": 3,
}
#: The precedence that makes the strata disjoint (first match wins).
PRECEDENCE = (
    "leavers",
    "article_9",
    "article_7",
    "other_financial",
    "reit",
    "regulated_utility",
    "multi_class",
    "negative_equity",
    "operating",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--universe-db", required=True)
    parser.add_argument("--company-types", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    with open(args.company_types, newline="") as handle:
        types = {row["cik"]: row for row in csv.DictReader(handle)}

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    assets = [
        {"cik": str(r["cik"]), "ticker": str(r["ticker"]), "id": int(r["id"])}
        for r in conn.execute("SELECT id, cik, ticker FROM assets")
    ]
    # The card is taken by CIK, so the latest 10-K is looked up by CIK (a two-class line's
    # filings sit under one of its two tickers).
    latest_10k: dict[str, tuple[str, str, str]] = {}
    for row in conn.execute(
        "SELECT a.cik, f.fiscal_period, f.period_end, f.available_at FROM sec_filings f "
        "JOIN assets a ON a.id = f.asset_id WHERE f.form = '10-K' ORDER BY f.period_end"
    ):
        latest_10k[str(row["cik"])] = (
            str(row["fiscal_period"]),
            str(row["period_end"]),
            str(row["available_at"]),
        )
    universe = sqlite3.connect(f"file:{args.universe_db}?mode=ro", uri=True)
    universe.row_factory = sqlite3.Row
    leaver_ciks = {
        str(r["cik"])
        for r in universe.execute(
            "SELECT DISTINCT cik FROM universe_membership WHERE valid_to IS NOT NULL"
        )
    }

    pools: dict[str, list[dict[str, Any]]] = {name: [] for name in STRATA}
    for asset in assets:
        row = types.get(asset["cik"])
        if row is None:
            continue
        flags = {
            "leavers": asset["cik"] in leaver_ciks,
            "article_9": row["type"] == "article_9",
            "article_7": row["type"] == "article_7",
            "other_financial": row["type"] == "other_financial",
            "reit": row["reit"].strip().lower() == "true",
            "regulated_utility": row["gics_sector"] == "Utilities",
            "multi_class": row["multi_class_listed"].strip().lower() == "true",
            "negative_equity": row["negative_equity_latest"].strip().lower() == "true",
            "operating": row["type"] == "operating",
        }
        for stratum in PRECEDENCE:
            if flags[stratum]:
                pools[stratum].append(asset)
                break

    rng = random.Random(args.seed)  # noqa: S311 - a deterministic draw, not a secret
    sample: list[dict[str, Any]] = []
    for stratum in PRECEDENCE:
        pool = sorted(pools[stratum], key=lambda a: a["cik"])
        k = STRATA[stratum]
        drawn = rng.sample(pool, min(k, len(pool)))
        for asset in drawn:
            period = latest_10k.get(asset["cik"])
            sample.append(
                {
                    "stratum": stratum,
                    "cik": asset["cik"],
                    "ticker": asset["ticker"],
                    "latest_10k_period": None if period is None else period[0],
                    "latest_10k_period_end": None if period is None else period[1],
                    "latest_10k_available_at": None if period is None else period[2],
                    # The card must show the 10-K's own numbers, so its as-of date is the day
                    # the 10-K became available (the filing date plus the availability rule).
                    "card_as_of": None if period is None else period[2],
                }
            )
    doc = {
        "seed": args.seed,
        "company_types": args.company_types,
        "pool_sizes": {name: len(pools[name]) for name in PRECEDENCE},
        "drawn": len(sample),
        "sample": sample,
    }
    Path(args.out).write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    for entry in sample:
        print(
            f"{entry['stratum']:20s} {entry['ticker']:6s} cik={entry['cik']} "
            f"10-K={entry['latest_10k_period']} ({entry['latest_10k_period_end']})"
        )
    print(f"pools: {doc['pool_sizes']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
