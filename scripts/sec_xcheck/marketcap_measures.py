"""D-07: the financial firms' share of the index's market capitalisation, approximate.

Production stores no cover-page share counts (``filing_cover_shares`` is a pilot-only table), so the as-of reader
``kg_schema.market_cap.market_caps_as_of`` cannot run on it. The estimate here is **price x cover shares**: the
last close on or before the date (``price_daily``, read-only) times the latest ``dei:EntityCommonStockSharesOutstanding``
the SEC ``companyfacts`` carries for a filing made on or before the date. It is **total, not float-adjusted**
(MKT-04) and **approximate**: no split adjustment, and a company whose cover reports its classes only with a
dimension has no non-dimensional total in ``companyfacts`` and is left out (coverage is reported).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sec_xcheck.common import SecClient, cik_family
from sec_xcheck.findings import Finding

CMD = "uv run python scripts/audit_sec_checklist.py measure --only marketcap"
MAX_SHARE_AGE_DAYS = 200
MAX_PRICE_AGE_DAYS = 10


def cover_shares(client: SecClient, cik: str, as_of: str) -> tuple[float, str] | None:
    """``(shares, end date)`` of the latest cover count filed on or before *as_of* and not older than the limit."""
    best: tuple[tuple[str, str], float] | None = None
    floor = (dt.date.fromisoformat(as_of) - dt.timedelta(days=MAX_SHARE_AGE_DAYS)).isoformat()
    for c in cik_family(cik):
        doc = client.get("companyfacts", c)
        if not doc:
            continue
        shares = (
            doc["facts"]
            .get("dei", {})
            .get("EntityCommonStockSharesOutstanding", {})
            .get("units", {})
        )
        for fact in shares.get("shares", []):
            order = (fact["filed"], fact["end"])
            if (
                fact["filed"] <= as_of
                and fact["end"] >= floor
                and (best is None or order > best[0])
            ):
                best = (order, float(fact["val"]))
    return None if best is None else (best[1], best[0][1])


def last_close(conn: Any, asset_id: int, as_of: str) -> float | None:
    floor = (dt.date.fromisoformat(as_of) - dt.timedelta(days=MAX_PRICE_AGE_DAYS)).isoformat()
    row = conn.execute(
        "SELECT close FROM price_daily WHERE asset_id = ? AND date <= ? AND date >= ? ORDER BY date DESC LIMIT 1",
        (asset_id, as_of, floor),
    ).fetchone()
    return None if row is None or row["close"] is None else float(row["close"])


def market_caps(conn: Any, client: SecClient, as_of: str) -> dict[str, dict[str, Any]]:
    """``{cik: {tickers, cap, reason}}``: the approximate total market cap of each CIK on *as_of*."""
    out: dict[str, dict[str, Any]] = {}
    for a in conn.execute(
        "SELECT id, ticker, cik FROM assets WHERE cik IS NOT NULL ORDER BY ticker"
    ):
        entry = out.setdefault(
            a["cik"], {"tickers": [], "cap": None, "reason": None, "shares": None, "prices": {}}
        )
        entry["tickers"].append(a["ticker"])
        price = last_close(conn, a["id"], as_of)
        entry["prices"][a["ticker"]] = price
    for cik, entry in out.items():
        shares = cover_shares(client, cik, as_of)
        prices = [p for p in entry["prices"].values() if p is not None]
        if shares is None:
            entry["reason"] = "no non-dimensional cover count in companyfacts"
        elif not prices:
            entry["reason"] = "no recent price"
        elif len(entry["tickers"]) > 1:
            entry["reason"] = (
                "several listed tickers share the CIK: one price cannot value all classes (MKT-03)"
            )
        else:
            entry["shares"] = shares[0]
            entry["cap"] = shares[0] * prices[0]
    return out


def share_by_type(
    caps: dict[str, dict[str, Any]], types: dict[str, dict[str, str]], as_of: str
) -> list[Finding]:
    total = sum(e["cap"] for e in caps.values() if e["cap"])
    covered = sum(1 for e in caps.values() if e["cap"])
    out = [
        Finding(
            "D07.cap_coverage",
            ["MKT-01", "MKT-04"],
            f"CIKs with an approximate market cap on {as_of} (price x cover shares)",
            "a",
            covered,
            len(caps),
            covered,
            unit="companies",
            command=CMD,
            note=f"total approximate cap of the covered set: USD {total / 1e12:,.1f} trillion; total, not float-adjusted",
        ),
    ]
    by: dict[str, float] = {}
    n: dict[str, int] = {}
    for cik, e in caps.items():
        if not e["cap"]:
            continue
        kind = types[cik]["type"]
        by[kind] = by.get(kind, 0.0) + e["cap"]
        n[kind] = n.get(kind, 0) + 1
    fin = sum(v for k, v in by.items() if k in ("article_9", "article_7", "other_financial"))
    out.append(
        Finding(
            "D07.financial_firms_cap_share",
            ["APP-00", "APP-02"],
            "Financial firms' (Article 9, Article 7, other financial) share of the covered market cap",
            "a",
            round(fin / total * 10000),
            10000,
            sum(n.get(k, 0) for k in ("article_9", "article_7", "other_financial")),
            unit="basis points",
            command=CMD,
            note="approximate; see the module docstring",
        )
    )
    for kind, v in sorted(by.items()):
        out.append(
            Finding(
                f"D07.cap_share.{kind}",
                ["APP-00"],
                f"{kind}: share of the covered market cap",
                "a",
                round(v / total * 10000),
                10000,
                n[kind],
                unit="basis points",
                command=CMD,
            )
        )
    return out
