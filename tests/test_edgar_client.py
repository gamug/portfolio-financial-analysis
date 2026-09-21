"""EDGAR client: envelope handling, ticker normalization, retry behavior, and the
multi-filing contract of portfolio-data-mining PR #39 (T-092)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from fundamental_agent.edgar_client import (
    EdgarAmbiguousError,
    EdgarClient,
    EdgarError,
    EdgarNotFoundError,
    FilingRef,
    normalize_ticker,
)

# Real responses captured from the gateway on 2026-09-21, after PR #39.
_REAL: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "edgar_multi_filing_responses.json").read_text()
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


# -- the multi-filing contract (T-092) ---------------------------------------------------


def _serving(payload: dict[str, Any], seen: list[httpx.Request] | None = None) -> EdgarClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=payload)

    return _client(handler)


def test_filing_by_year_returns_every_filing_most_recent_first() -> None:
    with _serving(_REAL["filing_by_year_XOM_10-Q_2024"]) as client:
        refs = client.filing_by_year("XOM", "10-Q", 2024)
    assert refs == [
        FilingRef("10-Q", "2024-11-04", "0000034088-24-000068"),
        FilingRef("10-Q", "2024-08-05", "0000034088-24-000050"),
        FilingRef("10-Q", "2024-04-29", "0000034088-24-000029"),
    ]
    dates = [r.filing_date or "" for r in refs]
    assert dates == sorted(dates, reverse=True)


def test_filing_by_year_an_empty_list_is_not_an_error() -> None:
    with _serving(_REAL["filing_by_year_APO_10-K_2022_empty"]) as client:
        assert client.filing_by_year("APO", "10-K", 2022) == []


def test_filing_by_year_rejects_the_pre_pr39_object_payload_loudly() -> None:
    legacy = {"success": True, "data": {"filing_date": "2024-11-04", "accession_number": "x"}}
    with _serving(legacy) as client, pytest.raises(EdgarError, match="predates"):
        client.filing_by_year("XOM", "10-Q", 2024)


def test_financials_sends_the_accession_number_only_when_given() -> None:
    seen: list[httpx.Request] = []
    with _serving({"success": True, "data": {"income_statement": []}}, seen) as client:
        client.financials("XOM", "10-K", 2024)
        client.financials("XOM", "10-Q", 2024, accession_number="0000034088-24-000068")
    assert "accession_number" not in seen[0].url.params
    assert seen[1].url.params["accession_number"] == "0000034088-24-000068"


def test_an_unqualified_multi_filing_request_raises_ambiguity_with_the_candidates() -> None:
    with (
        _serving(_REAL["financials_XOM_10-Q_2024_ambiguous"]) as client,
        pytest.raises(EdgarAmbiguousError) as info,
    ):
        client.financials("XOM", "10-Q", 2024)
    assert info.value.candidates == [
        "0000034088-24-000068",
        "0000034088-24-000050",
        "0000034088-24-000029",
    ]
    assert isinstance(info.value, EdgarError)  # existing handlers still catch it


def test_company_not_found_is_a_not_found_error_so_alternate_spellings_are_tried() -> None:
    with (
        _serving(_REAL["filing_by_year_ZZZZZZ_unknown_company"]) as client,
        pytest.raises(EdgarNotFoundError, match="Company not found"),
    ):
        client.filing_by_year("ZZZZZZ", "10-K", 2024)
