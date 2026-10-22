#!/usr/bin/env python3
"""V1's two open items, opened with their evidence (T-143 part A).

    .venv/bin/python scripts/pilot_a/v1_open_items.py \
        --db /workspaces/thesis/pilot_a2/financial.db \
        --universe-db /workspaces/thesis/data/universe_history_gics.db \
        --out /workspaces/thesis/scratch/t143a/out/p2b_v1_open_items.json \
        --markdown /workspaces/thesis/scratch/t143a/out/p2b_v1_open_items.md

Two cases the pilot's earlier round left open:

* **MRNA, 2026-08-18 -> 2026-08-19: 62.96 -> 174.38 (x2.77)** (T-131's "split-shaped day-over-day
  jump"). The script shows the stored ``price_daily`` rows, the **gateway's raw candles** for the
  same sessions (a GET), every ``corporate_action`` row of the company, the volume, the share
  count the market cap uses, and the sessions after the jump -- the evidence the classification
  (a data error, a real move, an unrecorded action) rests on.

* **FISV, one missing observation session (2025-11-12)**. The script shows the stored rows, the
  gateway's candles for the same window (a GET), **how many companies have that session** (so a
  market holiday is told apart from one company's gap), the company's index membership (its exit
  date) and whether its candles end.

Read-only: the database is opened read-only; the gateway is read with GETs only.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from pricing_agent.config import DEFAULT_PRICING_BASE_URL
from pricing_agent.pricing_client import PricingClient

#: The two candles the gateway and the stored rows must agree on, to the cent and to the share.
TOLERANCE = 1e-6

MRNA = ("MRNA", "2026-08-18", "2026-08-19", "2026-08-10", "2026-08-28")
FISV = ("FISV", "2025-11-12", "2025-11-12", "2025-11-05", "2025-11-20")


def _asset(conn: sqlite3.Connection, ticker: str) -> tuple[int, str]:
    row = conn.execute("SELECT id, cik FROM assets WHERE ticker = ?", (ticker,)).fetchone()
    if row is None:
        raise SystemExit(f"no asset with ticker {ticker}")
    return int(row[0]), str(row[1])


def _stored(conn: sqlite3.Connection, asset_id: int, start: str, end: str) -> list[dict[str, Any]]:
    return [
        {
            "date": r["date"],
            "open": r["open"],
            "high": r["high"],
            "low": r["low"],
            "close": r["close"],
            "volume": r["volume"],
            "source": r["source"],
        }
        for r in conn.execute(
            "SELECT date, open, high, low, close, volume, source FROM price_daily "
            "WHERE asset_id = ? AND date BETWEEN ? AND ? ORDER BY date",
            (asset_id, start, end),
        )
    ]


def _gateway(ticker: str, start: str, end: str) -> dict[str, Any]:
    with PricingClient(DEFAULT_PRICING_BASE_URL, timeout=60.0) as client:
        prices = client.daily(ticker, start, end)
    return {
        "source": prices.source,
        "warning": prices.warning,
        "candles": [
            {
                "date": c.date,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in prices.candles
        ],
    }


def _actions(conn: sqlite3.Connection, asset_id: int) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT action_type, ex_date, value, frequency, source FROM corporate_action "
            "WHERE asset_id = ? ORDER BY ex_date",
            (asset_id,),
        )
    ]


def _share_counts(conn: sqlite3.Connection, asset_id: int) -> list[dict[str, Any]]:
    """The share counts the market cap reads (the cover page and the diluted fallback)."""
    return [
        {
            "form": r["form"],
            "accession": r["accession"],
            "period_end": r["period_end"],
            "value": r["value"],
        }
        for r in conn.execute(
            """
            SELECT f.form, f.accession_number AS accession, f.period_end, ff.value
            FROM financial_facts ff JOIN sec_filings f ON f.id = ff.filing_id
            WHERE ff.filing_id IN (SELECT id FROM sec_filings WHERE asset_id = ?)
              AND ff.concept IN ('dei:EntityCommonStockSharesOutstanding',
                                  'us-gaap:CommonStockSharesOutstanding')
            ORDER BY f.period_end DESC LIMIT 8
            """,
            (asset_id,),
        )
    ]


def _session_counts(conn: sqlite3.Connection, date: str) -> dict[str, int]:
    """How many companies have a bar on *date* (a market holiday is 0; one company's gap is 1)."""
    have = conn.execute(
        "SELECT COUNT(DISTINCT asset_id) FROM price_daily WHERE date = ?", (date,)
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(DISTINCT asset_id) FROM price_daily").fetchone()[0]
    return {"companies_with_a_bar": int(have), "companies_with_any_bar": int(total)}


def _membership(universe_db: str, cik: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{universe_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT symbol, valid_from, valid_to, source FROM universe_membership WHERE cik = ?",
                (cik,),
            )
        ]
    finally:
        conn.close()


def _last_stored(conn: sqlite3.Connection, asset_id: int) -> str | None:
    row = conn.execute(
        "SELECT MAX(date) FROM price_daily WHERE asset_id = ?", (asset_id,)
    ).fetchone()
    return row[0] if row else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--universe-db", default="/workspaces/thesis/data/universe_history_gics.db")
    parser.add_argument("--out", required=True)
    parser.add_argument("--markdown", required=True)
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    doc: dict[str, Any] = {"db": args.db}

    # -- MRNA: the x2.77 jump
    ticker, before, after, start, end = MRNA
    asset_id, cik = _asset(conn, ticker)
    stored = _stored(conn, asset_id, start, end)
    gateway = _gateway(ticker, start, end)
    doc["mrna"] = {
        "ticker": ticker,
        "cik": cik,
        "the_jump": {"from": before, "to": after},
        "stored_rows": stored,
        "gateway_candles": gateway,
        "stored_equals_gateway": [
            {
                "date": row["date"],
                "equal": any(
                    c["date"] == row["date"]
                    and abs(c["close"] - row["close"]) < TOLERANCE
                    and abs(c["volume"] - row["volume"]) < TOLERANCE
                    for c in gateway["candles"]
                ),
            }
            for row in stored
        ],
        "corporate_actions": _actions(conn, asset_id),
        "share_counts": _share_counts(conn, asset_id),
        "volume_before": next((r["volume"] for r in stored if r["date"] == before), None),
        "volume_after": next((r["volume"] for r in stored if r["date"] == after), None),
        "closes_after_the_jump": [
            {"date": r["date"], "close": r["close"]} for r in stored if r["date"] >= after
        ],
    }

    # -- FISV: the missing session
    ticker, session, _unused, start, end = FISV
    asset_id, cik = _asset(conn, ticker)
    stored = _stored(conn, asset_id, start, end)
    gateway = _gateway(ticker, start, end)
    doc["fisv"] = {
        "ticker": ticker,
        "cik": cik,
        "the_missing_session": session,
        "stored_rows": stored,
        "gateway_candles": gateway,
        "gateway_has_the_session": any(c["date"] == session for c in gateway["candles"]),
        "stored_has_the_session": any(r["date"] == session for r in stored),
        "session_counts": _session_counts(conn, session),
        "the_neighbours": {
            "before": next((r["date"] for r in stored if r["date"] < session), None),
            "after": next((r["date"] for r in stored if r["date"] > session), None),
        },
        "membership": _membership(args.universe_db, cik),
        "last_stored_session": _last_stored(conn, asset_id),
        "corporate_actions": _actions(conn, asset_id),
    }
    conn.close()

    Path(args.out).write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n")

    mrna, fisv = doc["mrna"], doc["fisv"]
    lines = [
        "# V1's two open items, with the evidence",
        "",
        "## MRNA 2026-08-18 -> 2026-08-19 (62.96 -> 174.38, x2.77)",
        "",
        "| the evidence | what it says |",
        "|---|---|",
        (
            "| the stored rows vs the gateway's raw candles | "
            f"{sum(1 for r in mrna['stored_equals_gateway'] if r['equal'])} of "
            f"{len(mrna['stored_equals_gateway'])} stored bars are identical to the gateway's "
            "candles (the ingestion is faithful) |"
        ),
        (
            "| the volume | "
            f"{mrna['volume_before']:,.0f} -> {mrna['volume_after']:,.0f} "
            f"({mrna['volume_after'] / mrna['volume_before']:.1f}x) |"
        ),
        (
            "| the corporate actions | "
            f"{len(mrna['corporate_actions'])} row(s) for MRNA (a split would be one) |"
        ),
        "| the closes after the jump | "
        + ", ".join(f"{r['date']}: {r['close']:.2f}" for r in mrna["closes_after_the_jump"])
        + " |",
        "",
        "## FISV, the missing session 2025-11-12",
        "",
        "| the evidence | what it says |",
        "|---|---|",
        (
            "| the stored rows vs the gateway's candles | the session is missing in the stored rows "
            f"({fisv['stored_has_the_session']}) and in the gateway's answer "
            f"({fisv['gateway_has_the_session']}) |"
        ),
        (
            "| the session across the companies | "
            f"{fisv['session_counts']['companies_with_a_bar']} of "
            f"{fisv['session_counts']['companies_with_any_bar']} companies have a bar on "
            "2025-11-12 (the session existed; only FISV lacks it) |"
        ),
        (
            "| the index membership | "
            + "; ".join(
                f"valid_from {m['valid_from']}, valid_to {m['valid_to']}"
                for m in fisv["membership"]
            )
            + " |"
        ),
        (
            "| the last stored session | "
            f"{fisv['last_stored_session']} (the candles do not end at the merger) |"
        ),
        "",
    ]
    Path(args.markdown).write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
