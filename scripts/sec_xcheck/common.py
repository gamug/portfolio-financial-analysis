"""Shared plumbing of the ``scripts/sec_xcheck`` cross-checks: paths, the SEC cache, a read-only database.

Nothing here writes to a database. SEC data is fetched into a gitignored cache directory (never committed):

* ``SEC_XCHECK_CACHE``   -- cache root (default ``<repo>/.cache/sec_xcheck``); ``companyfacts/CIK##########.json``
  and ``submissions/CIK##########.json`` live under it.
* ``SEC_XCHECK_SOURCES`` -- the source documents of ``sources_register.md`` (default
  ``docs/checklist_sec/sec documentation``); the PDFs are copyrighted and never committed.
* ``KG_FINANCIAL_DB``    -- the database to read (or pass ``--db``).
* ``SEC_USER_AGENT``     -- the SEC contact; default ``research@example.com`` (never a personal address).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Any

from fundamental_agent.statements import STATEMENT_KEYS, Statements
from kg_schema.cli import resolve_db_path
from kg_schema.queries import connect_ro

REPO = Path(__file__).resolve().parents[2]

DEFAULT_CACHE = REPO / ".cache" / "sec_xcheck"
DEFAULT_SOURCES = REPO / "docs" / "checklist_sec" / "sec documentation"
DEFAULT_USER_AGENT = "research@example.com"
MAX_REQUESTS_PER_SECOND = 10

FY_DAYS = (340, 380)
QUARTER_DAYS = (75, 105)
# The convention the measurements share: a duration is a fiscal year or a quarter by its length.
YEAR_FORMS = ("10-K", "10-K/A", "10-KT")


def cache_dir(explicit: str | None = None) -> Path:
    return Path(explicit or os.environ.get("SEC_XCHECK_CACHE") or DEFAULT_CACHE).expanduser()


def sources_dir(explicit: str | None = None) -> Path:
    return Path(explicit or os.environ.get("SEC_XCHECK_SOURCES") or DEFAULT_SOURCES).expanduser()


def user_agent() -> str:
    return os.environ.get("SEC_USER_AGENT") or DEFAULT_USER_AGENT


def cik10(cik: str | int) -> str:
    return f"{int(cik):010d}"


def write_atomic(path: Path, body: bytes) -> None:
    """Write through a temporary name so a second fetcher never reads a half-written response."""
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_bytes(body)
    tmp.replace(path)


class SecClient:
    """Cached, rate-limited reader of SEC JSON endpoints (``companyfacts`` and ``submissions``).

    A cached file is never refetched. At most :data:`MAX_REQUESTS_PER_SECOND` requests per second.
    ``offline=True`` (the default for every check script) raises instead of touching the network.
    """

    def __init__(self, cache: Path, *, offline: bool = True, agent: str | None = None) -> None:
        self.cache = cache
        self.offline = offline
        self.agent = agent or user_agent()
        self.requests = 0
        self._last = 0.0

    def _path(self, kind: str, cik: str | int) -> Path:
        return self.cache / kind / f"CIK{cik10(cik)}.json"

    def _throttle(self) -> None:
        wait = (1.0 / MAX_REQUESTS_PER_SECOND) - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _fetch(self, url: str, retries: int = 3) -> bytes:
        for attempt in range(retries):
            self._throttle()
            self.requests += 1
            req = urllib.request.Request(url, headers={"User-Agent": self.agent})  # noqa: S310
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                    return bytes(resp.read())
            except urllib.error.HTTPError as exc:
                if exc.code == HTTPStatus.NOT_FOUND:
                    raise
                if attempt == retries - 1:
                    raise
                time.sleep(2**attempt)
        raise RuntimeError(url)  # pragma: no cover

    def get_document(self, cik: str | int, accession: str, primary: str) -> str | None:
        """The primary document of a filing (HTML), cached by accession; ``None`` on HTTP 404."""
        path = self.cache / "documents" / f"{accession}.htm"
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        if self.offline:
            raise FileNotFoundError(f"{path} is not cached; run with --fetch to download it")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{primary}"
        try:
            body = self._fetch(url)
        except urllib.error.HTTPError as exc:
            if exc.code == HTTPStatus.NOT_FOUND:
                return None
            raise
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, body)
        return body.decode("utf-8", errors="replace")

    def get_filing_documents(
        self, cik: str | int, accession: str, primary: str
    ) -> list[tuple[str, str]]:
        """The other body documents of a filing (``name``, HTML), from its ``index.json``: the HTML files that are
        neither the primary document, an exhibit (``*_ex*``), nor an XBRL viewer page (``R<n>.htm``)."""
        index_path = self.cache / "documents" / f"{accession}.index.json"
        if index_path.exists():
            index = json.loads(index_path.read_text(encoding="utf-8"))
        else:
            if self.offline:
                raise FileNotFoundError(
                    f"{index_path} is not cached; run with --fetch to download it"
                )
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/index.json"
            body = self._fetch(url)
            index_path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(index_path, body)
            index = json.loads(body)
        names = [
            it["name"]
            for it in index.get("directory", {}).get("item", [])
            if it["name"].endswith(".htm")
            and it["name"] != primary
            and "_ex" not in it["name"]
            and not re.match(r"R\d+\.htm$", it["name"])
        ]
        out: list[tuple[str, str]] = []
        for name in names:
            path = self.cache / "documents" / f"{accession}__{name}"
            if path.exists():
                out.append((name, path.read_text(encoding="utf-8", errors="replace")))
            elif not self.offline:
                body = self._fetch(
                    f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{name}"
                )
                write_atomic(path, body)
                out.append((name, body.decode("utf-8", errors="replace")))
        return out

    def get_file(self, kind: str, name: str) -> dict[str, Any] | None:
        """A named page of an endpoint (``submissions/CIK##########-submissions-001.json``), cached the same way."""
        path = self.cache / kind / name
        if path.exists():
            return dict(json.loads(path.read_text(encoding="utf-8")))
        if self.offline:
            raise FileNotFoundError(f"{path} is not cached; run with --fetch to download it")
        try:
            body = self._fetch(f"https://data.sec.gov/{kind}/{name}")
        except urllib.error.HTTPError as exc:
            if exc.code == HTTPStatus.NOT_FOUND:
                return None
            raise
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, body)
        return dict(json.loads(body))

    def get(self, kind: str, cik: str | int) -> dict[str, Any] | None:
        """The cached JSON, fetched once. ``None`` when the SEC has no such document (HTTP 404)."""
        path = self._path(kind, cik)
        if path.exists():
            return dict(json.loads(path.read_text(encoding="utf-8")))
        if self.offline:
            raise FileNotFoundError(f"{path} is not cached; run with --fetch to download it")
        url = {
            "companyfacts": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10(cik)}.json",
            "submissions": f"https://data.sec.gov/submissions/CIK{cik10(cik)}.json",
        }[kind]
        try:
            body = self._fetch(url)
        except urllib.error.HTTPError as exc:
            if exc.code == HTTPStatus.NOT_FOUND:
                return None
            raise
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, body)
        return dict(json.loads(body))


def match_value(a: float, b: float) -> bool:
    """Two stored/filed values agree to the dollar or to a part per million."""
    return abs(a - b) <= max(0.5, 1e-6 * abs(b))


def days_between(start: str, end: str) -> int:
    return (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days


def duration_matches(form: str, start: str | None, end: str) -> bool:
    """A duration fact counts for a 10-K when it spans a fiscal year, for a 10-Q when it spans a quarter."""
    if not start:
        return False
    days = days_between(start, end)
    if form.startswith("10-K"):
        return FY_DAYS[0] <= days <= FY_DAYS[1]
    return QUARTER_DAYS[0] <= days <= QUARTER_DAYS[1]


def iter_us_gaap(
    facts: dict[str, Any], taxonomies: tuple[str, ...] = ("us-gaap",)
) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield ``(taxonomy, concept, fact)`` for every fact of a ``companyfacts`` document."""
    for tax in taxonomies:
        for concept, group in facts.get("facts", {}).get(tax, {}).items():
            for unit, unit_facts in group.get("units", {}).items():
                for fact in unit_facts:
                    fact.setdefault(
                        "_unit", unit
                    )  # kept on the fact: ID-04 checks it against the concept's type
                    yield tax, concept, fact


def index_by_accession(
    doc: dict[str, Any], taxonomies: tuple[str, ...] = ("us-gaap",)
) -> dict[str, list[tuple[str, str, dict[str, Any]]]]:
    """``companyfacts`` facts grouped by the accession that filed them."""
    out: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for tax, concept, fact in iter_us_gaap(doc, taxonomies):
        out.setdefault(fact["accn"], []).append((tax, concept, fact))
    return out


_ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")


def is_accession(value: object) -> bool:
    return isinstance(value, str) and bool(_ACCESSION.match(value))


def open_ro(db_path: str | None) -> Any:
    """Open the database strictly read-only (``mode=ro``) through the repo's own factory."""
    return connect_ro(resolve_db_path(db_path))


# CIK successions the audit knows about: asset CIK -> predecessor CIKs whose companyfacts also carry the
# accessions (XOM's holding company, CIK 2115436, files under the predecessor's 34088 for older periods).
PREDECESSORS: dict[str, tuple[str, ...]] = {"0002115436": ("0000034088",)}


def cik_family(cik: str) -> tuple[str, ...]:
    return (cik10(cik), *PREDECESSORS.get(cik10(cik), ()))


def statements_from_db(conn: Any, filing_id: int) -> Any:
    """Rebuild a ``Statements`` from the stored ``financial_facts`` of one filing, rows in insertion order.

    ``financial_facts`` stores no abstract or dimensional rows and keys facts on
    ``(filing_id, statement, concept, period_key)``, so two payload rows of one concept collapse to the first
    (``INSERT OR IGNORE``); the rebuilt payload therefore carries one row per ``(statement, concept)``.
    """
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT statement, concept, label, standard_concept, period_key, value "
        "FROM financial_facts WHERE filing_id = ? ORDER BY id",
        (filing_id,),
    ):
        row = rows.setdefault(
            (r["statement"], r["concept"]),
            {
                "concept": r["concept"],
                "label": r["label"],
                "standard_concept": r["standard_concept"],
                "abstract": False,
                "dimension": False,
            },
        )
        row.setdefault(r["period_key"], r["value"])
    payload: dict[str, list[Any]] = {key: [] for key in STATEMENT_KEYS}
    for (statement, _concept), row in rows.items():
        payload.setdefault(statement, []).append(row)
    return Statements.from_payload(payload)


def own_period_key(stmts: Any, form: str, period_end: str) -> str | None:
    """The column a filing's own period sits in: the FY column of a 10-K, the quarter column of a 10-Q."""
    for p in stmts.periods:
        if p.date != period_end:
            continue
        if (form.startswith("10-K") and p.is_fy) or (form.startswith("10-Q") and p.is_quarter):
            return str(p.key)
    return None


def filings(conn: Any) -> list[Any]:
    """Every stored filing with its ticker and CIK, in a stable order."""
    return list(
        conn.execute(
            "SELECT f.id, f.asset_id, a.ticker, a.cik, f.form, f.fiscal_year, f.fiscal_period, f.period_end, "
            "f.filing_date, f.accession_number, f.available_at "
            "FROM sec_filings f JOIN assets a ON a.id = f.asset_id ORDER BY a.ticker, f.period_end, f.id"
        )
    )
