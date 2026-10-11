"""EDGAR client: envelope handling, retry behavior, the multi-filing contract of
portfolio-data-mining PR #39 (T-092), and fetching by CIK, never by ticker (T-149)."""

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
    NotACikError,
    require_cik,
)

# Real responses captured from the gateway on 2026-09-21, after PR #39.
_REAL: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "edgar_multi_filing_responses.json").read_text()
)


# Real responses captured on 2026-10-11, after portfolio-data-mining T-045: every filing row
# carries its filer's `cik` and the SEC's `period_of_report`.
_IDENTITY: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "edgar_filing_identity_responses.json").read_text()
)


def _client(handler: object) -> EdgarClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    http = httpx.Client(base_url="http://edgar.test", transport=transport)
    return EdgarClient("http://edgar.test", client=http)


def test_unwraps_success_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": {"cik": "320193"}})

    with _client(handler) as client:
        assert client.company_info("320193") == {"cik": "320193"}


def test_unsuccessful_payload_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "data": None})

    with _client(handler) as client, pytest.raises(EdgarError):
        client.company_info("320193")


def test_404_raises_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    with _client(handler) as client, pytest.raises(EdgarNotFoundError):
        client.financials("9999999", "10-K", 2023)


def test_retries_then_succeeds_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fundamental_agent.edgar_client.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"success": True, "data": [2022, 2023]})

    with _client(handler) as client:
        assert client.years_available("320193", "10-K") == [2022, 2023]
    assert calls["n"] == 3


# -- the multi-filing contract (T-092) ---------------------------------------------------


def _serving(payload: dict[str, Any], seen: list[httpx.Request] | None = None) -> EdgarClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=payload)

    return _client(handler)


def test_filing_by_year_returns_every_filing_most_recent_first() -> None:
    with _serving(_REAL["filing_by_year_XOM_10-Q_2024"]) as client:
        refs = client.filing_by_year("34088", "10-Q", 2024)
    assert refs == [
        FilingRef("10-Q", "2024-11-04", "0000034088-24-000068"),
        FilingRef("10-Q", "2024-08-05", "0000034088-24-000050"),
        FilingRef("10-Q", "2024-04-29", "0000034088-24-000029"),
    ]
    dates = [r.filing_date or "" for r in refs]
    assert dates == sorted(dates, reverse=True)


def test_filing_by_year_an_empty_list_is_not_an_error() -> None:
    with _serving(_REAL["filing_by_year_APO_10-K_2022_empty"]) as client:
        assert client.filing_by_year("1858681", "10-K", 2022) == []


def test_filing_by_year_rejects_the_pre_pr39_object_payload_loudly() -> None:
    legacy = {"success": True, "data": {"filing_date": "2024-11-04", "accession_number": "x"}}
    with _serving(legacy) as client, pytest.raises(EdgarError, match="predates"):
        client.filing_by_year("34088", "10-Q", 2024)


def test_financials_sends_the_accession_number_only_when_given() -> None:
    seen: list[httpx.Request] = []
    with _serving({"success": True, "data": {"income_statement": []}}, seen) as client:
        client.financials("34088", "10-K", 2024)
        client.financials("34088", "10-Q", 2024, accession_number="0000034088-24-000068")
    assert "accession_number" not in seen[0].url.params
    assert seen[1].url.params["accession_number"] == "0000034088-24-000068"


def test_financials_raises_on_a_nonempty_reconciliation_errors_list() -> None:
    """PR #98 review, item 1: T-042 (upstream PR #46) returns a statement
    rendered-but-unvalidated when its own reconciliation fails, recorded in
    `data["reconciliation_errors"]` (`[{"statement", "error"}]`) alongside
    `data["corrections"]` rather than failing the whole response. Such a statement's
    values are indistinguishable in shape from a genuinely clean one, so this client
    must surface the failure itself -- silently returning it would let an unvalidated,
    possibly-synthesized value reach `financial_facts` with `correction_rule = NULL`,
    meaning "filed as-is", exactly what T-118(3) exists to prevent."""
    payload = {
        "income_statement": [],
        "balance_sheet": [{"concept": "us-gaap_Assets"}],
        "cash_flow": [],
        "corrections": [],
        "reconciliation_errors": [
            {"statement": "balance_sheet", "error": "xbrl.facts.query() raised KeyError"}
        ],
    }
    with (
        _serving({"success": True, "data": payload}) as client,
        pytest.raises(EdgarError, match=r"balance_sheet.*KeyError"),
    ):
        client.financials("34088", "10-K", 2024)


def test_financials_with_no_reconciliation_errors_returns_normally() -> None:
    """A pre-PR-#46 gateway (no `reconciliation_errors` key at all) and a post-PR-#46
    gateway with an empty list must both pass through unchanged -- only a genuinely
    non-empty list is a failure."""
    payloads: list[dict[str, Any]] = [
        {"income_statement": [], "balance_sheet": [], "cash_flow": []},
        {"income_statement": [], "balance_sheet": [], "cash_flow": [], "reconciliation_errors": []},
    ]
    for payload in payloads:
        with _serving({"success": True, "data": payload}) as client:
            assert client.financials("34088", "10-K", 2024) == payload


def test_an_unqualified_multi_filing_request_raises_ambiguity_with_the_candidates() -> None:
    with (
        _serving(_REAL["financials_XOM_10-Q_2024_ambiguous"]) as client,
        pytest.raises(EdgarAmbiguousError) as info,
    ):
        client.financials("34088", "10-Q", 2024)
    assert info.value.candidates == [
        "0000034088-24-000068",
        "0000034088-24-000050",
        "0000034088-24-000029",
    ]
    assert isinstance(info.value, EdgarError)  # existing handlers still catch it


def test_a_cik_the_gateway_does_not_know_is_a_not_found_error() -> None:
    with (
        _serving(_REAL["filing_by_year_ZZZZZZ_unknown_company"]) as client,
        pytest.raises(EdgarNotFoundError, match="Company not found"),
    ):
        client.filing_by_year("9999999", "10-K", 2024)


# -- by CIK, never by ticker (T-149, APP-10a) ----------------------------------------------


@pytest.mark.parametrize("given", ["0000320193", "320193", 320193, " 0000320193 "])
def test_every_spelling_of_a_cik_reaches_the_gateway_as_ten_digits(given: Any) -> None:
    seen: list[httpx.Request] = []
    with _serving(_IDENTITY["filing_by_year_0000320193_10-K_2024"], seen) as client:
        client.filing_by_year(given, "10-K", 2024)
    assert seen[0].url.path == "/filing_by_year/0000320193"


@pytest.mark.parametrize("ticker", ["AAPL", "BRK.B", "", "TWTR", "0000320193-24-000123"])
def test_a_ticker_is_refused_before_any_request_is_made(ticker: str) -> None:
    seen: list[httpx.Request] = []
    with _serving({"success": True, "data": []}, seen) as client:
        for call in (
            lambda: client.filing_by_year(ticker, "10-K", 2024),
            lambda: client.financials(ticker, "10-K", 2024),
            lambda: client.years_available(ticker, "10-K"),
            lambda: client.company_info(ticker),
        ):
            with pytest.raises(NotACikError, match="never by ticker"):
                call()
    assert seen == []


def test_require_cik_pads_and_refuses() -> None:
    assert require_cik(1418091) == "0001418091"
    with pytest.raises(NotACikError):
        require_cik("TWTR")
    with pytest.raises(NotACikError):
        require_cik(None)
    with pytest.raises(NotACikError):
        require_cik("12345678901")  # more than ten digits is not a CIK


def test_a_listing_row_carries_its_filer_and_the_period_it_reports() -> None:
    """Real captures: Apple's FY2024 10-K ends 2024-09-28; Twitter, which left the index, is
    found by its CIK alone and its FY2021 10-K (filed in 2022) ends 2021-12-31."""
    with _serving(_IDENTITY["filing_by_year_0000320193_10-K_2024"]) as client:
        (apple,) = client.filing_by_year("0000320193", "10-K", 2024)
    assert apple == FilingRef(
        "10-K", "2024-11-01", "0000320193-24-000123", "0000320193", "2024-09-28"
    )
    with _serving(_IDENTITY["filing_by_year_1418091_10-K_2022"]) as client:
        (twitter,) = client.filing_by_year("1418091", "10-K", 2022)
    assert (twitter.cik, twitter.period_of_report) == ("0001418091", "2021-12-31")
    assert twitter.accession_number == "0001418091-22-000029"


def test_a_listing_without_the_new_fields_leaves_them_none() -> None:
    """A gateway before portfolio-data-mining T-045: the fields are absent, not an error."""
    with _serving(_REAL["filing_by_year_XOM_10-Q_2024"]) as client:
        refs = client.filing_by_year("34088", "10-Q", 2024)
    assert all(r.cik is None and r.period_of_report is None for r in refs)


def test_a_filing_with_no_period_of_report_keeps_none() -> None:
    row = {"form": "10-K", "filing_date": "2024-02-01", "accession_number": "a-1", "cik": "1"}
    with _serving({"success": True, "data": [{**row, "period_of_report": None}]}) as client:
        (ref,) = client.filing_by_year("1", "10-K", 2024)
    assert (ref.cik, ref.period_of_report) == ("0000000001", None)  # the CIK is normalized too


def test_the_real_ticker_miss_is_a_not_found_the_cik_does_not_have() -> None:
    """Twitter left the index: its ticker no longer resolves, its CIK does."""
    with (
        _serving(_IDENTITY["filing_by_year_TWTR_10-K_2022"]) as client,
        pytest.raises(EdgarNotFoundError, match="Company not found"),
    ):
        client._get("/filing_by_year/TWTR", {"form": "10-K", "year": 2022})
