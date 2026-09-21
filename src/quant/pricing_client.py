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


class ActionsUnavailable(RuntimeError):
    """The gateway answered, but flagged the data unusable.

    ``portfolio-data-mining``'s actions route never raises: when yfinance fails it
    returns 200 with empty lists and a non-null ``warning``. That is *not* "this
    ticker paid nothing" (which arrives with ``warning: null``), so it must never
    be recorded as an empty result.
    """


def yfinance_symbol(ticker: str) -> str:
    """The spelling the actions route needs: yfinance writes a share class with a
    dash (``BF-B``), not the index's dot. The route only upper-cases what it is
    given, and yfinance answers an unknown symbol with a clean empty result, so a
    dotted ticker would otherwise read as a name that paid no dividends."""
    return ticker.strip().upper().replace(".", "-")


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
        except (ActionsNotSupported, GatewayError, ActionsUnavailable):
            return False
        return True

    def actions(self, ticker: str, start_date: str, end_date: str) -> RawActions:
        """Dividends and splits with ex-dates in ``[start_date, end_date]``.

        Raises :class:`ActionsNotSupported` when the deployment has no such route,
        :class:`GatewayError` on an error status / unusable body (after retries),
        and :class:`ActionsUnavailable` when the gateway flags the data unusable.
        """
        params = {"start_date": start_date, "end_date": end_date, "actions": "true"}
        symbol = yfinance_symbol(ticker)
        for path in (f"/pricing/{symbol}/actions", f"/pricing/{symbol}"):
            resp = self._get(path, params)
            if resp.status_code == httpx.codes.NOT_FOUND:
                continue
            body = _json_object(resp, path)
            divs = _parse_rows(body.get("dividends"))
            splits = _parse_rows(body.get("splits"))
            if divs is None and splits is None:
                continue
            if body.get("warning"):
                raise ActionsUnavailable(f"{ticker}: {body['warning']}")
            return RawActions(ticker=ticker, dividends=divs or [], splits=splits or [])
        raise ActionsNotSupported(f"no corporate-actions data at {self._base_url} for {ticker}")


def _json_object(resp: httpx.Response, path: str) -> dict[str, object]:
    """The response body as a JSON object, or :class:`GatewayError`."""
    if resp.is_error:
        raise GatewayError(f"{path} -> {resp.status_code}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise GatewayError(f"{path} -> unparseable body") from exc
    if not isinstance(body, dict):
        raise GatewayError(f"{path} -> expected a JSON object")
    return body


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
