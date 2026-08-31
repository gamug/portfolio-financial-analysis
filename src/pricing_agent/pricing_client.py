"""HTTP client for the pricing gateway (``/pricing/{ticker}`` and ``/universe``).

The gateway returns HTTP 200 with an empty ``candles`` list and a ``warning`` when a
ticker/date range has no data (bad symbol, share-class spelling, weekend-only range),
so callers must check :attr:`DailyPrices.candles` rather than relying on status codes.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from typing import Any

import httpx

_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
_MAX_BACKOFF_SECONDS = 8.0


class PricingError(RuntimeError):
    """The gateway returned an error status or an unparseable body."""


@dataclass(frozen=True)
class Candle:
    date: str  # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str


@dataclass(frozen=True)
class DailyPrices:
    ticker: str
    start_date: str
    end_date: str
    source: str
    candles: list[Candle]
    warning: str | None

    @property
    def is_empty(self) -> bool:
        return not self.candles


def normalize_ticker(ticker: str) -> list[str]:
    """Candidate spellings, most likely first. yfinance wants ``BRK-B`` not ``BRK.B``."""
    raw = ticker.strip().upper()
    candidates = [raw]
    for alt in (raw.replace(".", "-"), raw.replace("-", "."), raw.replace(".", "")):
        if alt and alt not in candidates:
            candidates.append(alt)
    return candidates


class PricingClient:
    """Blocking client for the two endpoints this collector uses."""

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

    def __enter__(self) -> PricingClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = self._client.get(path, params=params)
            except httpx.TransportError as exc:
                last_error = exc
                time.sleep(min(2.0**attempt, _MAX_BACKOFF_SECONDS))
                continue
            if response.status_code in _RETRYABLE_STATUS:
                last_error = PricingError(f"{path} -> {response.status_code}")
                time.sleep(min(2.0**attempt, _MAX_BACKOFF_SECONDS))
                continue
            response.raise_for_status()
            return response.json()
        raise PricingError(f"{path} failed after {self._max_retries} attempts") from last_error

    def universe(self) -> list[dict[str, Any]]:
        """The pricing service's tracked S&P 500 rows (Wikipedia-shaped columns)."""
        data = self._get("/universe")
        if not isinstance(data, list):
            raise PricingError("/universe did not return a list")
        return data

    def daily(self, ticker: str, start_date: str, end_date: str) -> DailyPrices:
        raw = self._get(
            f"/pricing/{ticker}",
            {"start_date": start_date, "end_date": end_date},
        )
        candles = [
            Candle(
                date=str(row["date"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume") or 0.0),
                source=str(row.get("source") or raw.get("source") or "unknown"),
            )
            for row in raw.get("candles", [])
        ]
        return DailyPrices(
            ticker=str(raw.get("ticker") or ticker),
            start_date=str(raw.get("start_date") or start_date),
            end_date=str(raw.get("end_date") or end_date),
            source=str(raw.get("source") or "unknown"),
            candles=candles,
            warning=raw.get("warning"),
        )

    def daily_any_spelling(self, ticker: str, start_date: str, end_date: str) -> DailyPrices:
        """Try each ticker spelling; return the first non-empty result (or the last)."""
        result: DailyPrices | None = None
        for candidate in normalize_ticker(ticker):
            result = self.daily(candidate, start_date, end_date)
            if not result.is_empty:
                return result
        assert result is not None
        return result


def today_iso() -> str:
    return dt.date.today().isoformat()
