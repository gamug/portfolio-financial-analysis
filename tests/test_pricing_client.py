"""Pricing HTTP client: parsing, empty-result handling, retry, spelling fallback."""

from __future__ import annotations

import httpx
import pytest

from pricing_agent.pricing_client import (
    PricingClient,
    PricingError,
    normalize_ticker,
)

_CANDLE = {
    "date": "2022-01-03",
    "open": 1.0,
    "high": 2.0,
    "low": 0.5,
    "close": 1.5,
    "volume": 10,
    "source": "yfinance",
}


def test_normalize_ticker_prefers_dash_for_share_classes() -> None:
    assert normalize_ticker("brk.b") == ["BRK.B", "BRK-B", "BRKB"]
    assert normalize_ticker("AAPL") == ["AAPL"]


def _client(handler: object) -> PricingClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    http = httpx.Client(base_url="http://pricing.test", transport=transport)
    return PricingClient("http://pricing.test", client=http)


def test_daily_parses_candles() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ticker": "AAPL", "source": "yfinance", "candles": [_CANDLE], "warning": None},
        )

    with _client(handler) as client:
        prices = client.daily("AAPL", "2022-01-01", "2022-01-05")
    assert not prices.is_empty
    assert prices.candles[0].close == 1.5
    assert prices.source == "yfinance"


def test_daily_empty_result_is_not_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ticker": "ZZZZ", "source": "yfinance", "candles": [], "warning": "no rows"}
        )

    with _client(handler) as client:
        prices = client.daily("ZZZZ", "2022-01-01", "2022-01-05")
    assert prices.is_empty
    assert prices.warning == "no rows"


def test_daily_any_spelling_falls_through_to_dash() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        has_data = request.url.path.endswith("BRK-B")
        return httpx.Response(
            200,
            json={
                "ticker": request.url.path.rsplit("/", 1)[-1],
                "source": "yfinance",
                "candles": [_CANDLE] if has_data else [],
                "warning": None if has_data else "no rows",
            },
        )

    with _client(handler) as client:
        prices = client.daily_any_spelling("BRK.B", "2022-01-01", "2022-01-05")
    assert not prices.is_empty
    assert seen[0].endswith("BRK.B")
    assert any(p.endswith("BRK-B") for p in seen)


def test_get_retries_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pricing_agent.pricing_client.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=[{"Symbol": "AAPL"}])

    with _client(handler) as client:
        assert client.universe() == [{"Symbol": "AAPL"}]
    assert calls["n"] == 3


def test_universe_non_list_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a list"})

    with _client(handler) as client, pytest.raises(PricingError):
        client.universe()
