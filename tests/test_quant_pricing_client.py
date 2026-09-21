"""QuantPricingClient: the ``portfolio-data-mining`` corporate-actions contract (T-085).

Hermetic -- an ``httpx.MockTransport`` stands in for the gateway. The response shape is
what upstream's ``GET /pricing/{ticker}/actions`` documents (its ``get_corporate_actions``
docstring): ``{ticker, start_date, end_date, source, dividends, splits, warning}``, with
``warning`` non-null *only* when yfinance failed. The values are illustrative, modelled on
upstream's own live check (XOM's quarterly dividends, NVDA's 10-for-1 split on 2024-06-10).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from quant.pricing_client import (
    ActionsNotSupported,
    ActionsUnavailable,
    GatewayError,
    QuantPricingClient,
    yfinance_symbol,
)

_XOM_2024 = [
    {"date": "2024-02-14", "value": 0.95},
    {"date": "2024-05-14", "value": 0.95},
    {"date": "2024-08-15", "value": 0.95},
    {"date": "2024-11-15", "value": 0.99},
]
_NVDA_SPLIT = [{"date": "2024-06-10", "value": 10.0}]


def _body(
    ticker: str,
    dividends: list[dict[str, Any]] | None = None,
    splits: list[dict[str, Any]] | None = None,
    warning: str | None = None,
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "source": "yfinance",
        "dividends": dividends or [],
        "splits": splits or [],
        "warning": warning,
    }


def _client(
    handler: Callable[[httpx.Request], httpx.Response], *, max_retries: int = 1
) -> QuantPricingClient:
    http = httpx.Client(base_url="http://gateway.test", transport=httpx.MockTransport(handler))
    return QuantPricingClient("http://gateway.test", max_retries=max_retries, client=http)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("quant.pricing_client.time.sleep", lambda _s: None)


def test_parses_the_upstream_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/pricing/XOM/actions"
        return httpx.Response(200, json=_body("XOM", dividends=_XOM_2024))

    with _client(handler) as client:
        raw = client.actions("XOM", "2024-01-01", "2024-12-31")
    assert [(d.ex_date, d.value) for d in raw.dividends] == [
        (d["date"], d["value"]) for d in _XOM_2024
    ]
    assert raw.splits == []


def test_parses_a_split() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_body("NVDA", splits=_NVDA_SPLIT))

    with _client(handler) as client:
        raw = client.actions("NVDA", "2024-01-01", "2024-12-31")
    assert [(s.ex_date, s.value) for s in raw.splits] == [("2024-06-10", 10.0)]


def test_a_range_with_no_actions_is_a_clean_empty_result() -> None:
    """``warning: null`` + empty lists is "paid nothing", not a failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_body("BRK-B"))

    with _client(handler) as client:
        raw = client.actions("BRK.B", "2024-01-01", "2024-12-31")
    assert raw.dividends == [] and raw.splits == []


def test_a_warning_is_unavailable_never_an_empty_result() -> None:
    """A yfinance failure arrives as 200 + empty lists + a ``warning``; recording that
    as "no dividends" is the silent failure T-085 exists to prevent."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_body("XOM", warning="yfinance failed to return corporate actions: boom")
        )

    with _client(handler) as client:
        with pytest.raises(ActionsUnavailable, match="boom"):
            client.actions("XOM", "2024-01-01", "2024-12-31")
        assert client.probe("XOM") is False


def test_a_dotted_share_class_is_requested_with_the_yfinance_spelling() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200, json=_body("BF-B", dividends=[{"date": "2024-03-01", "value": 0.2}])
        )

    with _client(handler) as client:
        raw = client.actions("BF.B", "2024-01-01", "2024-12-31")
    assert seen == ["/pricing/BF-B/actions"]
    assert raw.ticker == "BF.B"  # the caller's spelling comes back, for its own bookkeeping
    assert yfinance_symbol(" bf.b ") == "BF-B"
    assert yfinance_symbol("XOM") == "XOM"


def test_falls_back_to_the_actions_flag_on_the_candle_route() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/pricing/XOM/actions":
            return httpx.Response(404)
        assert request.url.path == "/pricing/XOM"
        assert request.url.params["actions"] == "true"
        return httpx.Response(200, json=_body("XOM", dividends=_XOM_2024[:1]))

    with _client(handler) as client:
        raw = client.actions("XOM", "2024-01-01", "2024-12-31")
    assert len(raw.dividends) == 1


def test_no_actions_route_anywhere_is_not_supported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/actions"):
            return httpx.Response(404)
        return httpx.Response(200, json={"ticker": "XOM", "candles": []})  # OHLCV only

    with _client(handler) as client:
        with pytest.raises(ActionsNotSupported):
            client.actions("XOM", "2024-01-01", "2024-12-31")
        assert client.probe("XOM") is False


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"detail": "start_date must be <= end_date"}),
        httpx.Response(422, json={"detail": "bad date"}),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
    ],
    ids=["400", "422", "not-json", "not-an-object"],
)
def test_an_unusable_response_is_a_gateway_error(response: httpx.Response) -> None:
    """Not a bare ``HTTPStatusError``/``JSONDecodeError`` -- the backfill catches
    :class:`GatewayError` per asset."""
    with _client(lambda request: response) as client:
        with pytest.raises(GatewayError):
            client.actions("XOM", "2024-01-01", "2024-12-31")
        assert client.probe("XOM") is False


def test_a_server_error_exhausts_the_retries_then_raises() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    with _client(handler, max_retries=3) as client, pytest.raises(GatewayError, match="3 attempts"):
        client.actions("XOM", "2024-01-01", "2024-12-31")
    assert calls == 3
