"""Thin HTTP client for the local SEC EDGAR gateway.

Every gateway response is an envelope ``{"success": bool, "data": ...}``; this client
unwraps it and turns anything unexpected into an :class:`EdgarError`.
"""

from __future__ import annotations

import time
from typing import Any, cast

import httpx

FILING_FORMS = ("10-K", "10-Q", "8-K", "S-1")
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
_MAX_BACKOFF_SECONDS = 8.0


class EdgarError(RuntimeError):
    """The gateway returned an error status or an unsuccessful payload."""


class EdgarNotFoundError(EdgarError):
    """The gateway has no data for the requested ticker/form/year."""


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

    def filing_by_year(self, ticker: str, form: str, year: int) -> dict[str, Any]:
        raw = self._get(f"/filing_by_year/{ticker}", {"form": form, "year": year})
        return cast("dict[str, Any]", raw)

    def financials(self, ticker: str, form: str, year: int) -> dict[str, list[dict[str, Any]]]:
        raw = self._get(f"/financials/{ticker}", {"form": form, "year": year})
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
        raise EdgarError(f"{path} -> unsuccessful payload: {payload!r:.200}")
    return payload["data"]
