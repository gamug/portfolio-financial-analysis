"""Thin HTTP client for the local SEC EDGAR gateway.

Every gateway response is an envelope ``{"success": bool, "data": ...}``; this client
unwraps it and turns anything unexpected into an :class:`EdgarError`.

``portfolio-data-mining``'s PR #39 (T-092) changed two routes: ``filing_by_year`` now
returns **every** filing for a form and year (a company files three 10-Qs a year), and
``financials`` takes an optional ``accession_number`` -- without it, a year with several
filings is an error. This client speaks that contract only.

A company is identified by its CIK, never its ticker (T-149, checklist APP-10a: tickers are
reassigned, and a company that left the index has none). Every company-scoped call takes the
10-digit CIK string and refuses anything else; the gateway accepts one in place of a ticker, with
or without leading zeros, and finds a company that left the index by its CIK alone.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, cast

import httpx

from kg_schema.identity import normalize_cik

FILING_FORMS = ("10-K", "10-Q", "8-K", "S-1")
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
_MAX_BACKOFF_SECONDS = 8.0


class EdgarError(RuntimeError):
    """The gateway returned an error status or an unsuccessful payload."""


class EdgarNotFoundError(EdgarError):
    """The gateway has no data for the requested CIK/form/year."""


class EdgarAmbiguousError(EdgarError):
    """A ``financials`` request matched several filings and named none of them."""

    def __init__(self, message: str, candidates: list[str]) -> None:
        super().__init__(message)
        self.candidates = candidates


class NotACikError(EdgarError, ValueError):
    """A company was named by something other than its CIK (a ticker, an empty string)."""


@dataclass(frozen=True)
class FilingRef:
    """One filing as ``filing_by_year`` lists it.

    ``cik`` (the filer the row was listed under, 10 digits) and ``period_of_report`` (the SEC's
    ``reportDate``, ``YYYY-MM-DD``, ``None`` when the SEC gives none) arrived with
    portfolio-data-mining T-045; ``None`` here means the gateway sent none."""

    form: str
    filing_date: str | None
    accession_number: str
    cik: str | None = None
    period_of_report: str | None = None


# "Found 3 '10-Q' filings for 'XOM' in 2024; call get_filing_by_year ... Available: a, b, c"
_AMBIGUOUS_RE = re.compile(
    r"Found \d+ '[^']*' filings?.*?Available:\s*(?P<accessions>[0-9A-Za-z][0-9A-Za-z,\s-]*)",
    re.DOTALL,
)


def require_cik(value: object) -> str:
    """*value* as the 10-digit CIK string every company-scoped call carries, else
    :class:`NotACikError`. A ticker is refused, never looked up (APP-10a)."""
    try:
        cik = normalize_cik(value)
    except ValueError:
        cik = None
    if cik is None:
        raise NotACikError(f"{value!r} is not a CIK: filings are fetched by CIK, never by ticker")
    return cik


class EdgarClient:
    """Blocking client covering the handful of endpoints the agent uses."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._client = client or httpx.Client(base_url=self._base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> EdgarClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = self._client.get(path, params=params)
            except httpx.TransportError as exc:  # timeouts, DNS, connection resets
                last_error = exc
                time.sleep(min(2.0**attempt, _MAX_BACKOFF_SECONDS))
                continue
            if response.status_code == httpx.codes.NOT_FOUND:
                raise EdgarNotFoundError(f"{path} -> 404")
            if response.status_code in _RETRYABLE_STATUS:
                last_error = EdgarError(f"{path} -> {response.status_code}")
                time.sleep(min(2.0**attempt, _MAX_BACKOFF_SECONDS))
                continue
            response.raise_for_status()
            return _unwrap(path, response.json())
        raise EdgarError(f"{path} failed after {self._max_retries} attempts") from last_error

    # -- endpoint wrappers -------------------------------------------------

    def company_info(self, cik: str) -> dict[str, Any]:
        return cast("dict[str, Any]", self._get(f"/company_info/{require_cik(cik)}"))

    def years_available(self, cik: str, form: str) -> list[int]:
        data = self._get(f"/years_available/{require_cik(cik)}", {"form": form})
        return sorted(int(year) for year in data)

    def filing_by_year(self, cik: str, form: str, year: int) -> list[FilingRef]:
        """Every *form* filing whose filing date falls in *year*, most recent first.

        Empty when there is none (a valid answer, not an error). A single-object payload
        is the pre-#39 contract and is rejected loudly rather than guessed at."""
        cik = require_cik(cik)
        raw = self._get(f"/filing_by_year/{cik}", {"form": form, "year": year})
        if not isinstance(raw, list):
            raise EdgarError(
                f"/filing_by_year/{cik} returned {type(raw).__name__}, expected a list of "
                "filings -- the gateway predates portfolio-data-mining PR #39"
            )
        return [
            FilingRef(
                form=str(item.get("form", form)),
                filing_date=str(item["filing_date"]) if item.get("filing_date") else None,
                accession_number=str(item["accession_number"]),
                cik=normalize_cik(item.get("cik")),
                period_of_report=str(item["period_of_report"])
                if item.get("period_of_report")
                else None,
            )
            for item in raw
            if isinstance(item, dict) and item.get("accession_number")
        ]

    def financials(
        self, cik: str, form: str, year: int, accession_number: str | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        """The statements of one filing. *accession_number* is required whenever the
        year holds more than one filing of *form* (raises :class:`EdgarAmbiguousError`).

        Raises :class:`EdgarError` when the gateway's own reconciliation (T-042) failed
        for one or more statements (``data["reconciliation_errors"]``, upstream PR #46) --
        such a statement comes back rendered but unvalidated, indistinguishable in shape
        from a genuinely clean one, so it must never reach ``financial_facts`` looking
        like either. The caller's existing per-filing failure handling (unscored, retried
        next run) already does the right thing once this raises. A failed *cover-page* read
        (``statement == "cover"``, portfolio-data-mining T-043) is the exception: it leaves
        the statements validated and only the share count empty, so it does not raise."""
        cik = require_cik(cik)
        params: dict[str, Any] = {"form": form, "year": year}
        if accession_number is not None:
            params["accession_number"] = accession_number
        raw = self._get(f"/financials/{cik}", params)
        # The cover-page count (portfolio-data-mining T-043) is read independently of the
        # statements and fails in isolation: its error rides in the same list but leaves all three
        # statements validated, so it must not fail the filing -- the caller records it and the
        # filing simply has no cover count (T-132).
        errors = [
            e for e in raw.get("reconciliation_errors") or [] if e.get("statement") != "cover"
        ]
        if errors:
            detail = "; ".join(f"{e.get('statement')}: {e.get('error')}" for e in errors)
            raise EdgarError(f"/financials/{cik} reconciliation failed: {detail}")
        return cast("dict[str, list[dict[str, Any]]]", raw)


def _unwrap(path: str, payload: Any) -> Any:
    if not isinstance(payload, dict) or not payload.get("success"):
        message = str(payload.get("error", "")) if isinstance(payload, dict) else ""
        if match := _AMBIGUOUS_RE.search(message):
            accessions = [a.strip() for a in match.group("accessions").split(",") if a.strip()]
            raise EdgarAmbiguousError(f"{path} -> {message}", accessions)
        if "Company not found" in message:
            raise EdgarNotFoundError(f"{path} -> {message.splitlines()[0]}")
        raise EdgarError(f"{path} -> unsuccessful payload: {payload!r:.200}")
    return payload["data"]
