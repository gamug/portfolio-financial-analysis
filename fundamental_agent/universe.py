"""Build the analysis universe from the Wikipedia S&P 500 constituents list."""

from __future__ import annotations

import os

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel

WIKIPEDIA_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
# Wikipedia blocks browser-mimicking UAs from datacenters and requires a descriptive
# one (project + contact URL). Override via WIKIPEDIA_USER_AGENT to add a real contact.
_DEFAULT_USER_AGENT = (
    "portfolio-finantial-analysis/0.1 "
    "(+https://github.com/portfolio-finantial-analysis; research use) python-httpx"
)
_MIN_COLUMNS = 8


def _user_agent() -> str:
    return os.environ.get("WIKIPEDIA_USER_AGENT", _DEFAULT_USER_AGENT)


class Company(BaseModel):
    """One S&P 500 constituent as listed on Wikipedia."""

    symbol: str
    name: str
    cik: str
    sector: str
    sub_industry: str


def parse_constituents(html: str) -> list[Company]:
    """Parse the ``#constituents`` table out of the Wikipedia page HTML."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id="constituents") or soup.find("table", class_="wikitable")
    if table is None:
        raise ValueError("could not locate the S&P 500 constituents table")

    companies: list[Company] = []
    for row in table.select("tbody > tr"):
        cells = row.find_all("td")
        if len(cells) < _MIN_COLUMNS:
            continue
        symbol = _clean(cells[0].get_text())
        cik = _clean(cells[6].get_text())
        companies.append(
            Company(
                symbol=symbol,
                name=_clean(cells[1].get_text()),
                sector=_clean(cells[2].get_text()),
                sub_industry=_clean(cells[3].get_text()),
                cik=cik.zfill(10) if cik.isdigit() else cik,
            )
        )
    if not companies:
        raise ValueError("constituents table parsed but yielded no rows")
    return companies


def fetch_sp500(*, timeout: float = 30.0, client: httpx.Client | None = None) -> list[Company]:
    """Fetch and parse the current S&P 500 constituents."""
    owns_client = client is None
    http = client or httpx.Client(timeout=timeout, headers={"User-Agent": _user_agent()})
    try:
        response = http.get(WIKIPEDIA_SP500_URL)
        response.raise_for_status()
        return parse_constituents(response.text)
    finally:
        if owns_client:
            http.close()


def _clean(text: str) -> str:
    return text.replace("\xa0", " ").strip()
