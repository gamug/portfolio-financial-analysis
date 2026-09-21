"""Thin HTTP client for the local SEC EDGAR gateway.

Every gateway response is an envelope ``{"success": bool, "data": ...}``; this client
unwraps it and turns anything unexpected into an :class:`EdgarError`.

``portfolio-data-mining``'s PR #39 (T-092) changed two routes: ``filing_by_year`` now
returns **every** filing for a form and year (a company files three 10-Qs a year), and
``financials`` takes an optional ``accession_number`` -- without it, a year with several
filings is an error. This client speaks that contract only.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, cast

import httpx

FILING_FORMS = ("10-K", "10-Q", "8-K", "S-1")
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
_MAX_BACKOFF_SECONDS = 8.0


class EdgarError(RuntimeError):
    """The gateway returned an error status or an unsuccessful payload."""


class EdgarNotFoundError(EdgarError):
    """The gateway has no data for the requested ticker/form/year."""


class EdgarAmbiguousError(EdgarError):
    """A ``financials`` request matched several filings and named none of them."""

    def __init__(self, message: str, candidates: list[str]) -> None:
        super().__init__(message)
        self.candidates = candidates


@dataclass(frozen=True)
class FilingRef:
    """One filing as ``filing_by_year`` lists it."""

    form: str
    filing_date: str | None
    accession_number: str


# "Found 3 '10-Q' filings for 'XOM' in 2024; call get_filing_by_year ... Available: a, b, c"
_AMBIGUOUS_RE = re.compile(
    r"Found \d+ '[^']*' filings?.*?Available:\s*(?P<accessions>[0-9A-Za-z][0-9A-Za-z,\s-]*)",
    re.DOTALL,
)


def normalize_ticker(ticker: str) -> list[str]:
    """Return candidate spellings for *ticker*, most-likely first.

    Wikipedia writes share classes as ``BRK.B``; EDGAR usually wants ``BRK-B`` and
    occasionally ``BRKB``.
    """
    raw = ticker.strip().upper()
    candidates = [raw]
    for alt in (raw.replace(".", "-"), raw.replace(".", ""), raw.replace("-", ".")):
        if alt and alt not in candidates:
            candidates.append(alt)
    return candidates


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

    def company_info(self, ticker: str) -> dict[str, Any]:
        return cast("dict[str, Any]", self._get(f"/company_info/{ticker}"))

    def years_available(self, ticker: str, form: str) -> list[int]:
        data = self._get(f"/years_available/{ticker}", {"form": form})
        return sorted(int(year) for year in data)

    def filing_by_year(self, ticker: str, form: str, year: int) -> list[FilingRef]:
        """Every *form* filing whose filing date falls in *year*, most recent first.

        Empty when there is none (a valid answer, not an error). A single-object payload
        is the pre-#39 contract and is rejected loudly rather than guessed at."""
        raw = self._get(f"/filing_by_year/{ticker}", {"form": form, "year": year})
        if not isinstance(raw, list):
            raise EdgarError(
                f"/filing_by_year/{ticker} returned {type(raw).__name__}, expected a list of "
                "filings -- the gateway predates portfolio-data-mining PR #39"
            )
        return [
            FilingRef(
                form=str(item.get("form", form)),
                filing_date=str(item["filing_date"]) if item.get("filing_date") else None,
                accession_number=str(item["accession_number"]),
            )
            for item in raw
            if isinstance(item, dict) and item.get("accession_number")
        ]

    def financials(
        self, ticker: str, form: str, year: int, accession_number: str | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        """The statements of one filing. *accession_number* is required whenever the
        year holds more than one filing of *form* (raises :class:`EdgarAmbiguousError`)."""
        params: dict[str, Any] = {"form": form, "year": year}
        if accession_number is not None:
            params["accession_number"] = accession_number
        raw = self._get(f"/financials/{ticker}", params)
        return cast("dict[str, list[dict[str, Any]]]", raw)

    # -- helpers --------------------------------------------------------------

    def resolve(self, ticker: str) -> tuple[str, dict[str, Any]]:
        """Return the first ticker spelling EDGAR recognises and its company info."""
        errors: list[str] = []
        for candidate in normalize_ticker(ticker):
            try:
                return candidate, self.company_info(candidate)
            except EdgarNotFoundError as exc:
                errors.append(str(exc))
        raise EdgarNotFoundError(f"no EDGAR match for {ticker!r}: {'; '.join(errors)}")


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
