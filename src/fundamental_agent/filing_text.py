"""Fetch a filing's primary document straight from SEC EDGAR.

The analysis gateway only serves structured financials, so narrative text is pulled
from ``www.sec.gov`` using the ``accession_number`` + ``cik`` already stored on
``sec_filings`` / ``assets``. SEC **rejects with HTTP 403** any request whose
``User-Agent`` has no contact address in it, and rate-limits to ~10 req/s. Set
``SEC_USER_AGENT`` to ``"Your Name your@email"`` before any real run; the built-in
default only carries a placeholder address and will be refused in production.

A gateway text endpoint can replace :func:`fetch_primary_document` later without
touching :mod:`fundamental_agent.sections`.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

import httpx

# XBRL viewer fragments SEC lists alongside the primary document.
_FRAGMENT_RE = re.compile(r"^r\d+\.html?$", re.IGNORECASE)
# Exhibits / certifications filed next to the primary document (ex-31.1, exhibit99,
# corp10k2023exhibit1019, d123cert, ...). Matched on a word boundary so a filer whose
# real document is e.g. "excelsior-20231231.htm" is not mistaken for an exhibit.
_EXHIBIT_RE = re.compile(r"(?:\b|_)(?:ex|exh|exhibit|cert)[-_.]?\d", re.IGNORECASE)

_SEC_BASE = "https://www.sec.gov"
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_BACKOFF_SECONDS = 8.0
# SEC 403s a User-Agent with no contact address. This default keeps the shape SEC
# wants (name + address) but the address is a placeholder -- real runs MUST export
# SEC_USER_AGENT with a reachable contact.
_DEFAULT_USER_AGENT = "portfolio-finantial-analysis/0.1 sec-contact@example.com"


class FilingTextError(RuntimeError):
    """The primary document could not be fetched or located."""


def _user_agent() -> str:
    return os.environ.get("SEC_USER_AGENT", _DEFAULT_USER_AGENT)


def _accession_nodash(accession_number: str) -> str:
    return accession_number.replace("-", "")


def _get(client: httpx.Client, url: str, *, max_retries: int = 3) -> httpx.Response:
    last: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = client.get(url, headers={"User-Agent": _user_agent()})
        except httpx.TransportError as exc:
            last = exc
            time.sleep(min(2.0**attempt, _MAX_BACKOFF_SECONDS))
            continue
        if resp.status_code in _RETRYABLE_STATUS:
            last = FilingTextError(f"{url} -> {resp.status_code}")
            time.sleep(min(2.0**attempt, _MAX_BACKOFF_SECONDS))
            continue
        resp.raise_for_status()
        return resp
    raise FilingTextError(f"{url} failed after {max_retries} attempts") from last


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _primary_document_name(index: dict[str, Any]) -> str:
    items = index.get("directory", {}).get("item", [])
    htmls = [
        (str(it["name"]), _as_int(it.get("size")))
        for it in items
        if str(it.get("name", "")).lower().endswith((".htm", ".html"))
        and "index" not in str(it.get("name", "")).lower()
        and not _FRAGMENT_RE.match(str(it.get("name", "")))
    ]
    if not htmls:
        raise FilingTextError("no primary .htm in filing directory listing")
    # The directory is alphabetical, not primary-first, so an exhibit like
    # "corp10k2023exhibit1019.htm" often sorts ahead of the real body. Drop the
    # exhibits/certs, then take the largest .htm -- the 10-K/10-Q body dwarfs them.
    non_exhibit = [h for h in htmls if not _EXHIBIT_RE.search(h[0])] or htmls
    return max(non_exhibit, key=lambda h: h[1])[0]


def fetch_primary_document(
    cik: str, accession_number: str, *, client: httpx.Client | None = None
) -> tuple[str, str]:
    """Return ``(html, source_url)`` for the filing's primary document."""
    owned = client is None
    client = client or httpx.Client(base_url=_SEC_BASE, timeout=30.0, follow_redirects=True)
    try:
        cik_int = int(str(cik).lstrip("0") or "0")
        folder = f"/Archives/edgar/data/{cik_int}/{_accession_nodash(accession_number)}"
        index = _get(client, f"{folder}/index.json").json()
        name = _primary_document_name(index)
        url = f"{folder}/{name}"
        html = _get(client, url).text
        return html, f"{_SEC_BASE}{url}"
    finally:
        if owned:
            client.close()
