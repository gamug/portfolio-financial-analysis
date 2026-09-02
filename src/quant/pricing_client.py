"""HTTP client for corporate actions from the pricing gateway.

The gateway's ``/pricing/{ticker}`` route returns only OHLCV candles. Some
yfinance-backed deployments also expose actions (dividends / splits) via an
``actions=true`` query flag or a ``/pricing/{ticker}/actions`` route. This client
probes both and raises :class:`ActionsNotSupported` when neither answers, so the
caller can fall back to deriving dividends from stored XBRL facts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
_MAX_BACKOFF_SECONDS = 8.0
_PAIR_LEN = 2


@dataclass(frozen=True)
class RawAction:
    ex_date: str
    value: float  # cash/share for a dividend, ratio for a split


class ActionsNotSupported(RuntimeError):
    """The gateway has no corporate-actions endpoint we can use."""


class GatewayError(RuntimeError):
    """The gateway returned an error status or an unparseable body."""


@dataclass(frozen=True)
class RawActions:
    ticker: str
    dividends: list[RawAction]  # (ex_date, cash/share)
    splits: list[RawAction]  # (ex_date, ratio)


class QuantPricingClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 60.0,
        max_retries: int = 3,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._client = client or httpx.Client(base_url=self._base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> QuantPricingClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, str] | None = None) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = self._client.get(path, params=params)
            except httpx.TransportError as exc:
                last_error = exc
                time.sleep(min(2.0**attempt, _MAX_BACKOFF_SECONDS))
                continue
            if response.status_code in _RETRYABLE_STATUS:
                last_error = GatewayError(f"{path} -> {response.status_code}")
                time.sleep(min(2.0**attempt, _MAX_BACKOFF_SECONDS))
                continue
            return response
        raise GatewayError(f"{path} failed after {self._max_retries} attempts") from last_error

    def probe(self, sample_ticker: str) -> bool:
        """True when the gateway serves corporate actions for *sample_ticker*."""
        try:
            self.actions(sample_ticker, "1900-01-01", "1900-01-02")
        except ActionsNotSupported:
            return False
        except GatewayError:
            return False
        return True

    def actions(self, ticker: str, start_date: str, end_date: str) -> RawActions:
        params = {"start_date": start_date, "end_date": end_date, "actions": "true"}
        for path in (f"/pricing/{ticker}/actions", f"/pricing/{ticker}"):
            resp = self._get(path, params)
            if resp.status_code == httpx.codes.NOT_FOUND:
                continue
            resp.raise_for_status()
            body = resp.json()
            divs = _parse_rows(body.get("dividends"))
            splits = _parse_rows(body.get("splits"))
            if divs is None and splits is None:
                continue
            return RawActions(ticker=ticker, dividends=divs or [], splits=splits or [])
        raise ActionsNotSupported(f"no corporate-actions data at {self._base_url} for {ticker}")


def _parse_rows(rows: object) -> list[RawAction] | None:
    if not isinstance(rows, list):
        return None
    out: list[RawAction] = []
    for row in rows:
        if isinstance(row, dict) and "date" in row and "value" in row:
            out.append(RawAction(str(row["date"]), float(row["value"])))
        elif isinstance(row, (list, tuple)) and len(row) == _PAIR_LEN:
            out.append(RawAction(str(row[0]), float(row[1])))
    return out
