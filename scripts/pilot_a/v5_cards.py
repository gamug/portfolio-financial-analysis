#!/usr/bin/env python3
"""V5: the 25 cards, regenerated with the values, the accessions, the EDGAR links and the vetoes.

    .venv/bin/python scripts/pilot_a/v5_cards.py \
        --db /workspaces/thesis/pilot_a2/financial.db \
        --sample /workspaces/thesis/scratch/t143a/out/p2b_v5_sample.json \
        --out-dir /workspaces/thesis/scratch/t143a/out/v5_cards_p2b_v2 \
        --manifest /workspaces/thesis/scratch/t143a/out/p2b_v5_cards_manifest.json

The same seeded draw and the same as-of dates as the first round (``p2b_v5_sample.json``, the
sampler's seed 20261018), so the two card sets are comparable line by line.  Each card is the
``fundamental_agent card`` of the company: its line items with their values, their accessions and
the EDGAR link to the filing's document, and the vetoes that apply on the card's date.

Read-only: the database is opened read-only; the only writes are the card files.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from fundamental_agent.card import build_card, render
from kg_schema.db import connect_ro


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--sample", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--no-edgar", action="store_true", help="do not read EDGAR's filing index")
    parser.add_argument("--pause", type=float, default=0.4, help="seconds between the EDGAR reads")
    args = parser.parse_args()

    sample = json.loads(Path(args.sample).read_text())
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = connect_ro(args.db)
    entries: list[dict[str, Any]] = []
    try:
        for item in sample["sample"]:
            ticker = str(item["ticker"])
            as_of = str(item["card_as_of"])
            if not as_of:
                entries.append({"ticker": ticker, "skipped": "no 10-K available"})
                continue
            card = build_card(
                conn,
                str(item["cik"]),
                as_of,
                with_edgar=not args.no_edgar,
                with_vetoes=True,
            )
            path = out_dir / f"{ticker}.txt"
            path.write_text(render(card))
            entries.append(
                {
                    "ticker": ticker,
                    "company": card.company_name,
                    "stratum": item["stratum"],
                    "cik": item["cik"],
                    "as_of": as_of,
                    "accession": card.filing["accession"],
                    "edgar_document_url": card.filing.get("edgar_document_url"),
                    "edgar_folder": card.filing.get("edgar_folder"),
                    "line_items": len(card.line_items),
                    "metrics": len(card.metrics),
                    "empty_metrics": len(card.empty),
                    "vetoes": [v["rule_id"] for v in card.vetoes],
                    "path": str(path),
                }
            )
            print(
                f"{ticker:6s} {as_of} {card.filing['accession']} "
                f"items={len(card.line_items)} vetoes={len(card.vetoes)}",
                flush=True,
            )
            if not args.no_edgar:
                time.sleep(args.pause)  # the SEC's rate limit is ~10 requests a second
    finally:
        conn.close()
    Path(args.manifest).write_text(
        json.dumps(
            {
                "db": args.db,
                "sample": args.sample,
                "seed": sample.get("seed"),
                "drawn": sample.get("drawn"),
                "cards": entries,
            },
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )
    print(f"wrote {len(entries)} cards into {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
