"""Audit round #11, D-19: a fiscal period that ends before the company profile's interval (the 2020+ load's FY2019
10-Ks, filed in 2020) reads the profile at the interval's start, flagged; never an unknown type for it, and nothing
changes for a period inside the interval, an unknown CIK or a leaver's later period."""

from __future__ import annotations

from typing import Any

from fundamental_agent.agents import FilingContext, FundamentalAnalyst
from fundamental_agent.applicability import profile_for_period
from fundamental_agent.statements import Statements

WFC = "0000072971"  # article_9, the profile's interval from 2020-01-01
HES = "0000004447"  # a leaver, valid_to 2025-07-23


def test_a_period_before_the_interval_reads_the_profile_at_its_start() -> None:
    profile, window_start = profile_for_period(WFC, "2019-12-31 (FY)")
    assert profile is not None and profile.type == "article_9"
    assert window_start is True
    assert profile.valid_from > "2019-12-31"


def test_a_period_inside_the_interval_is_read_as_before() -> None:
    profile, window_start = profile_for_period(WFC, "2023-12-31 (FY)")
    assert profile is not None and profile.type == "article_9"
    assert window_start is False


def test_an_unknown_cik_and_a_leavers_later_period_stay_unknown() -> None:
    assert profile_for_period("0000000001", "2019-12-31 (FY)") == (None, False)
    assert profile_for_period(HES, "2025-12-31 (FY)") == (None, False)


def test_a_fy2019_bank_filing_gets_its_na_cells_and_the_flag(aapl_10k: Statements) -> None:
    """Before D-19 the FY2019 10-K of a bank computed every metric under `company_type_unknown`."""
    analyst = FundamentalAnalyst(None, "rule-based")
    ctx = FilingContext(
        "WFC",
        "Wells Fargo",
        "10-K",
        "FY2019",
        aapl_10k,
        "2019-12-31 (FY)",
        "2018-12-31 (FY)",
        cik=WFC,
    )
    results = {result.name: result for _group, result in analyst._compute_all(ctx)}
    gross = results["gross_margin"]
    assert gross.inputs["flag_gross_margin_not_applicable"] == 1.0
    assert gross.inputs["provenance"]["applicability_rule"] == "APP-01"
    for result in results.values():
        assert result.inputs[f"flag_{result.name}_company_type_window_start"] == 1.0, result.name
        assert f"flag_{result.name}_company_type_unknown" not in result.inputs, result.name


def test_attach_context_sets_the_type_for_a_period_before_the_interval(memory_db: Any) -> None:
    from fundamental_agent import db  # noqa: PLC0415

    memory_db.execute("INSERT INTO assets (id, ticker, cik) VALUES (1, 'WFC', ?)", (WFC,))
    stmts = Statements.from_payload({"income_statement": [], "balance_sheet": [], "cash_flow": []})
    db.attach_context(memory_db, 1, "10-K", "2019-12-31 (FY)", stmts)
    assert stmts.company_type == "article_9"
