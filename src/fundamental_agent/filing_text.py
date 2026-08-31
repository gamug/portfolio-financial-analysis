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
# The filing's own "-index.html" carries a Document Format Files table whose Type
# column names the primary document unambiguously -- more reliable than the
# size/name heuristic over index.json, which some filers publish incomplete.
_ACCESSION_RE = re.compile(r"^\s*(\d{10})-(\d{2})-(\d{6})\s*$")
_DOC_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_DOC_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_DOC_HREF_RE = re.compile(
    r"""href=["'](?:/ix\?doc=)?(/Archives/edgar/data/\d+/\d+/[^"']+?\.(?:htm|html))["']""",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
# Seq, Description, Document, Type (Size may be absent) -- need at least the first 4.
_DOC_TABLE_MIN_CELLS = 4
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_BACKOFF_SECONDS = 8.0
# SEC 403s a User-Agent with no contact address. This default keeps the shape SEC
# wants (name + address) but the address is a placeholder -- real runs MUST export
# SEC_USER_AGENT with a reachable contact.
_DEFAULT_USER_AGENT = "portfolio-financial-analysis/0.1 sec-contact@example.com"


class FilingTextError(RuntimeError):
    """The primary document could not be fetched or located."""


def _user_agent() -> str:
    return os.environ.get("SEC_USER_AGENT", _DEFAULT_USER_AGENT)


def _accession_nodash(accession_number: str) -> str:
    return accession_number.replace("-", "")


def _archive_cik(accession_number: str, fallback_cik: str) -> int:
    """The archive folder lives under the *filer* CIK, which is the accession-number
    prefix -- not necessarily the asset's current CIK (e.g. XOM re-registered under a
    new CIK, but its old filings stay under ``34088``). Fall back to *fallback_cik*
    when the accession isn't the standard ``0000000000-00-000000`` shape.
    """
    m = _ACCESSION_RE.match(accession_number or "")
    if m:
        return int(m.group(1))
    return int(str(fallback_cik).lstrip("0") or "0")


def _primary_from_filing_index(
    client: httpx.Client, cik_int: int, accession_number: str, form: str
) -> str | None:
    """Return the primary document's archive path from the filing's ``-index.html``.

    The row whose ``Type`` is the form itself (``10-K`` / ``10-Q``, or its ``/A``
    amendment) at the lowest sequence number is the filing body, however the file is
    named. The table's own ``href`` is used, so the path carries the real registrant
    CIK even when a filing agent's CIK prefixes the accession number. Returns
    ``None`` if the page can't be read or has no such row, so the caller can fall
    back to the index.json heuristic.
    """
    accnd = _accession_nodash(accession_number)
    url = f"/Archives/edgar/data/{cik_int}/{accnd}/{accession_number}-index.html"
    try:
        page = _get(client, url).text
    except (FilingTextError, httpx.HTTPStatusError):
        return None
    want = form.upper().replace(" ", "").replace("/", "")
    best: tuple[int, str] | None = None
    for row_m in _DOC_ROW_RE.finditer(page):
        row = row_m.group(1)
        href = _DOC_HREF_RE.search(row)
        if href is None:
            continue
        cells = [
            _TAG_RE.sub("", c).replace("\xa0", " ").replace("&nbsp;", " ").strip()
            for c in _DOC_CELL_RE.findall(row)
        ]
        if len(cells) < _DOC_TABLE_MIN_CELLS:
            continue
        seq_raw, _description, _document, doc_type = cells[0], cells[1], cells[2], cells[3]
        if doc_type.upper().replace(" ", "").replace("/", "") not in (want, f"{want}A"):
            continue
        try:
            seq = int(seq_raw)
        except ValueError:
            seq = 9999
        if best is None or seq < best[0]:
            best = (seq, href.group(1))
    return best[1] if best else None


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
        if not resp.content:
            # SEC occasionally serves a 200 with an empty body for an archived
            # document that is genuinely missing; retry once, then surface it.
            last = FilingTextError(f"{url} -> 200 but empty body")
            time.sleep(min(2.0**attempt, _MAX_BACKOFF_SECONDS))
            continue
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
    cik: str,
    accession_number: str,
    *,
    form: str | None = None,
    client: httpx.Client | None = None,
) -> tuple[str, str]:
    """Return ``(html, source_url)`` for the filing's primary document.

    When *form* is given, the filing's ``-index.html`` document table (which names
    the body by ``Type``) is tried first; otherwise -- or if that yields nothing --
    the primary ``.htm`` is inferred from ``index.json`` by size.
    """
    owned = client is None
    client = client or httpx.Client(base_url=_SEC_BASE, timeout=30.0, follow_redirects=True)
    try:
        cik_int = _archive_cik(accession_number, cik)
        folder = f"/Archives/edgar/data/{cik_int}/{_accession_nodash(accession_number)}"
        name = _primary_from_filing_index(client, cik_int, accession_number, form) if form else None
        if name is None:
            index = _get(client, f"{folder}/index.json").json()
            name = _primary_document_name(index)
        # _primary_from_filing_index returns an absolute archive path; index.json
        # yields a bare filename that lives in the accession folder.
        url = name if name.startswith("/Archives/") else f"{folder}/{name}"
        html = _get(client, url).text
        return html, f"{_SEC_BASE}{url}"
    finally:
        if owned:
            client.close()
