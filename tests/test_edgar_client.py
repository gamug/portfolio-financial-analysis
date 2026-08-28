"""EDGAR client: envelope handling, ticker normalization, retry behavior."""

from __future__ import annotations

import httpx
import pytest

from fundamental_agent.edgar_client import (
    EdgarClient,
    EdgarError,
    EdgarNotFoundError,
    normalize_ticker,
)


def test_normalize_ticker_orders_candidates() -> None:
    assert normalize_ticker("brk.b") == ["BRK.B", "BRK-B", "BRKB"]
    assert normalize_ticker("AAPL") == ["AAPL"]


def _client(handler: object) -> EdgarClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    http = httpx.Client(base_url="http://edgar.test", transport=transport)
    return EdgarClient("http://edgar.test", client=http)


def test_unwraps_success_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": {"cik": "320193"}})

    with _client(handler) as client:
        assert client.company_info("AAPL") == {"cik": "320193"}


def test_unsuccessful_payload_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "data": None})

    with _client(handler) as client, pytest.raises(EdgarError):
        client.company_info("AAPL")


def test_404_raises_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    with _client(handler) as client, pytest.raises(EdgarNotFoundError):
        client.financials("ZZZZ", "10-K", 2023)


def test_retries_then_succeeds_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fundamental_agent.edgar_client.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"success": True, "data": [2022, 2023]})

    with _client(handler) as client:
        assert client.years_available("AAPL", "10-K") == [2022, 2023]
    assert calls["n"] == 3


def test_resolve_tries_alternate_spellings() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("BRK-B"):
            return httpx.Response(200, json={"success": True, "data": {"name": "Berkshire"}})
        return httpx.Response(404, json={"detail": "Not Found"})

    with _client(handler) as client:
        ticker, info = client.resolve("BRK.B")
    assert ticker == "BRK-B"
    assert info["name"] == "Berkshire"
    assert seen[0].endswith("BRK.B")
