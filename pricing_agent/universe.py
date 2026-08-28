"""Turn the pricing gateway's ``/universe`` rows into typed companies.

The rows carry Wikipedia-shaped column names (``Symbol``, ``Security``,
``GICS Sector`` ...), so this is a light rename, not a scrape.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class Company(BaseModel):
    """One tracked S&P 500 constituent."""

    symbol: str
    name: str
    cik: str
    sector: str
    sub_industry: str


def parse_universe(rows: list[dict[str, Any]]) -> list[Company]:
    companies: list[Company] = []
    for row in rows:
        symbol = str(row.get("Symbol") or "").strip()
        if not symbol:
            continue
        cik = str(row.get("CIK") or "").strip()
        companies.append(
            Company(
                symbol=symbol,
                name=str(row.get("Security") or "").strip(),
                sector=str(row.get("GICS Sector") or "").strip(),
                sub_industry=str(row.get("GICS Sub-Industry") or "").strip(),
                cik=cik.zfill(10) if cik.isdigit() else cik,
            )
        )
    if not companies:
        raise ValueError("pricing /universe returned no usable rows")
    return companies
