"""T-150: the accounting-identity gate (CON-01 to CON-04, CON-07, CON-08, CON-19) over a
filing's stored facts, with DQ-01's tolerance and L-10's rounding term.

The identity arithmetic is pure (:func:`quality.evaluate_identities` over ``{concept: value}``);
the fact reader and the persistence are exercised on a small in-memory fixture.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest
from conftest import seed_filing
from portfolio_common.db import Database

from fundamental_agent import db, quality
from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.quality import (
    _ASSETS,
    _ASSETS_CURRENT,
    _ASSETS_NONCURRENT,
    _EQUITY_INCL_NCI,
    _LIAB_EQ,
    _LIABILITIES,
    _LIABILITIES_CURRENT,
    _LIABILITIES_NONCURRENT,
    _MINORITY_INTEREST,
    _NCI_INCOME,
    _NET_INCOME,
    _OCF,
    _OCF_CONT,
    _OCF_DISC,
    _PROFIT_LOSS,
    _STOCKHOLDERS_EQUITY,
)
from fundamental_agent.statements import G as GAAP

# DQ-01's first "change in cash" concept (C3), used by the CON-07/CON-08 fixtures.
CHANGE = GAAP + (
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecrease"
    "ExcludingExchangeRateEffect"
)
_ICF = GAAP + "NetCashProvidedByUsedInInvestingActivities"
_FCF = GAAP + "NetCashProvidedByUsedInFinancingActivities"

MILLION = 1_000_000.0
BILLION = 1_000_000_000.0


def _result(values: Mapping[str, float | None], rule: str) -> quality.IdentityResult | None:
    """The *rule*'s result over one balance/flow column (the fixture's concept dict)."""
    results = quality.evaluate_identities(values, values, instant_column="C", flow_column="C")
    return next((r for r in results if r.rule_id == rule), None)


# -- L-10 and DQ-01 arithmetic ------------------------------------------------------------------


def test_l10_unit_is_the_largest_exact_divisor() -> None:
    assert quality.l10_unit([12 * MILLION, 5 * MILLION]) == MILLION
    assert quality.l10_unit([12 * MILLION, 5.5 * MILLION]) == 1_000.0
    assert quality.l10_unit([1_234.0, 5_678.0]) == 1.0
    # a zero term divides by anything: the other term decides
    assert quality.l10_unit([0.0, 8 * MILLION]) == MILLION


def test_l10_rounding_con07_is_two_million() -> None:
    """L-10's own example: CON-07, four terms on a statement in millions, gets USD 2,000,000."""
    terms = [2 * MILLION, -3 * MILLION, 4 * MILLION, -1 * MILLION]
    assert quality.l10_rounding(terms) == 2 * MILLION


def test_dq01_boundaries() -> None:
    rounding = MILLION
    base = 1_000 * MILLION  # threshold = max(1e6, 0.005 x |1e9|) = 5e6
    assert quality.dq01_level(rounding, rounding, base) == "pass"  # exactly rounding -> pass
    assert quality.dq01_level(rounding + 1, rounding, base) == "warn"
    assert quality.dq01_level(5 * MILLION, rounding, base) == "warn"  # exactly threshold -> warn
    assert quality.dq01_level(5 * MILLION + 1, rounding, base) == "block"


def test_a_missing_base_never_blocks() -> None:
    """C3's default: alone on the rounding term, a not-pass is a WARN with ``base_missing``."""
    assert quality.dq01_level(1.0, 1.0, None) == "pass"
    assert quality.dq01_level(9 * BILLION, 1.0, None) == "warn"


# -- one identity at each level, per rule -------------------------------------------------------


def _con01(assets: float, liabilities_and_equity: float) -> dict[str, float | None]:
    return {_ASSETS: assets, _LIAB_EQ: liabilities_and_equity}


def test_con01_pass_warn_block() -> None:
    passing = _result(_con01(10 * BILLION, 10 * BILLION), "CON_01")
    assert passing is not None and passing.level == "pass"
    warn = _result(_con01(10 * BILLION, 10 * BILLION - 10 * MILLION), "CON_01")
    assert warn is not None and warn.level == "warn"
    block = _result(_con01(10 * BILLION, 10 * BILLION - 60 * MILLION), "CON_01")
    assert block is not None and block.level == "block"


def test_con02_is_a_warn_rule_even_at_the_material_level() -> None:
    values = {_ASSETS: 10 * BILLION, _ASSETS_CURRENT: 3 * BILLION, _ASSETS_NONCURRENT: 2 * BILLION}
    result = _result(values, "CON_02")
    assert result is not None
    assert result.level == "block"
    assert result.warn_only is True  # C4: CON-02 records a WARN whatever the level
    rows = quality._identity_issues_from_results([result], {})
    assert len(rows) == 1 and rows[0].quarantined is False  # one row, nothing quarantined


def test_con03_needs_all_three_operands_and_uses_assets_as_base() -> None:
    assert _result({_LIABILITIES: 5 * BILLION, _LIABILITIES_CURRENT: 1 * BILLION}, "CON_03") is None
    values = {
        _LIABILITIES: 5 * BILLION,
        _LIABILITIES_CURRENT: 1 * BILLION,
        _LIABILITIES_NONCURRENT: 3 * BILLION,
    }
    result = _result(values, "CON_03")
    assert result is not None and result.level == "warn"
    assert result.base_missing is True  # Assets is not an operand and was not filed


def test_con04_only_when_minority_interest_is_filed() -> None:
    assert (
        _result({_EQUITY_INCL_NCI: 5 * BILLION, _STOCKHOLDERS_EQUITY: 5 * BILLION}, "CON_04")
        is None
    )
    values = {
        _EQUITY_INCL_NCI: 5 * BILLION,
        _STOCKHOLDERS_EQUITY: 4.9 * BILLION,
        _MINORITY_INTEREST: 0.1 * BILLION,
        _ASSETS: 10 * BILLION,
    }
    result = _result(values, "CON_04")
    assert result is not None and result.level == "pass"


def test_con19_reads_the_filed_sign() -> None:
    values = {
        _PROFIT_LOSS: 1_000 * MILLION,
        _NET_INCOME: 800 * MILLION,
        _NCI_INCOME: 200 * MILLION,
    }
    result = _result(values, "CON_19")
    assert result is not None and result.level == "pass"


def test_con08_with_base_missing_stays_a_warn() -> None:
    """CON-08's base (the change in cash) is not an operand: with none filed it is a WARN cap."""
    result = quality.con08_result({_OCF: 100 * MILLION, _OCF_CONT: 95 * MILLION}, "C")
    assert result is not None
    assert result.level == "warn" and result.base_missing is True


def test_con07_uses_the_change_in_cash_as_base() -> None:
    values = {CHANGE: 100 * MILLION, _OCF: 500 * MILLION, _ICF: -300 * MILLION, _FCF: -80 * MILLION}
    result = quality.con07_result(values, "C")
    assert result is not None
    assert result.base == 100 * MILLION
    assert result.level == "block"  # 2e6 < 20e6, threshold = max(2e6, 0.5e6) = 2e6
    assert result.warn_only is True


def test_con08_continuing_only_when_no_discontinued_line() -> None:
    cont = quality.con08_result({_OCF: 100 * MILLION, _OCF_CONT: 100 * MILLION}, "C")
    assert cont is not None and cont.n_terms == 2 and cont.level == "pass"
    both = quality.con08_result(
        {_OCF: 100 * MILLION, _OCF_CONT: 80 * MILLION, _OCF_DISC: 20 * MILLION}, "C"
    )
    assert both is not None and both.n_terms == 3 and both.level == "pass"


def test_a_rule_with_a_missing_operand_is_not_tested() -> None:
    assert _result({_ASSETS: 10 * BILLION}, "CON_01") is None
    assert quality.con08_result({_OCF: 100 * MILLION}, "C") is None


# -- the fact reader and the gate persistence ---------------------------------------------------


def _asset(conn: Database) -> int:
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'XOM')")
    return 1


def _fact(  # noqa: PLR0913, PLR0917 - one stored fact, all fields explicit
    conn: Database,
    fid: int,
    statement: str,
    concept: str,
    column: str,
    value: float,
    *,
    sign: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO financial_facts "
        "(filing_id, statement, concept, label, period_key, value, preferred_sign) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (fid, statement, concept, concept, column, value, sign),
    )


def _metric(conn: Database, fid: int, group: str, name: str, value: float) -> None:
    db.record_metrics(
        conn, fid, [(group, MetricResult(name, value, "ratio", {}))], engine_version="metrics-v1"
    )


def test_load_facts_reads_the_filed_sign(memory_db: Database) -> None:
    _asset(memory_db)
    fid = seed_filing(memory_db, 1, period_end="2024-12-31", filing_date="2025-02-01")
    _fact(memory_db, fid, "cash_flow", _OCF, "2024-12-31 (FY)", -500 * MILLION, sign=-1.0)
    facts = quality.load_facts(memory_db, [fid])
    assert facts[fid].flow_column == "2024-12-31 (FY)"
    assert facts[fid].value(_OCF, "2024-12-31 (FY)") == 500 * MILLION  # -500 x -1


def test_load_facts_reads_displayed_when_the_column_is_absent(
    memory_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _asset(memory_db)
    fid = seed_filing(memory_db, 1, period_end="2024-12-31", filing_date="2025-02-01")
    _fact(memory_db, fid, "cash_flow", _OCF, "2024-12-31 (FY)", -500 * MILLION, sign=-1.0)
    monkeypatch.setattr(quality, "_has_preferred_sign", lambda conn: False)
    facts = quality.load_facts(memory_db, [fid])
    assert facts[fid].value(_OCF, "2024-12-31 (FY)") == -500 * MILLION  # as displayed


def test_load_facts_picks_the_quarter_and_ytd_columns_of_a_10q(memory_db: Database) -> None:
    _asset(memory_db)
    fid = seed_filing(memory_db, 1, period_end="2025-09-30", filing_date="2025-11-01", form="10-Q")
    _fact(memory_db, fid, "balance_sheet", _ASSETS, "2025-09-30", 10 * BILLION)
    _fact(memory_db, fid, "income_statement", _NET_INCOME, "2025-09-30 (Q3)", 100 * MILLION)
    _fact(memory_db, fid, "cash_flow", _OCF, "2025-09-30 (Q3)", 500 * MILLION)
    _fact(memory_db, fid, "cash_flow", _OCF, "2025-09-30 (YTD)", 1_500 * MILLION)
    facts = quality.load_facts(memory_db, [fid])
    assert facts[fid].instant_column == "2025-09-30"
    assert facts[fid].flow_column == "2025-09-30 (Q3)"
    assert facts[fid].ytd_column == "2025-09-30 (YTD)"


def test_evaluate_identities_falls_back_to_the_ytd_column(
    memory_db: Database,
) -> None:
    """CON-07 on a 10-Q whose quarter cash-flow column is empty reads the year-to-date column."""
    change = CHANGE
    flow = {c: None for c in (change, _OCF, _ICF, _FCF)}
    ytd = {change: 100 * MILLION, _OCF: 500 * MILLION, _ICF: -300 * MILLION, _FCF: -80 * MILLION}
    results = quality.evaluate_identities(
        {}, flow, flow_column="2025-09-30 (Q3)", ytd=ytd, ytd_column="2025-09-30 (YTD)"
    )
    con07 = next(r for r in results if r.rule_id == "CON_07")
    assert con07.ytd is True and con07.column == "2025-09-30 (YTD)"


def test_a_block_writes_the_quarantine_rows_and_a_warn_one_identity_row(
    memory_db: Database,
) -> None:
    _asset(memory_db)
    fid = seed_filing(memory_db, 1, period_end="2024-12-31", filing_date="2025-02-01")
    _fact(memory_db, fid, "balance_sheet", _ASSETS, "2024-12-31", 10 * BILLION)
    _fact(memory_db, fid, "balance_sheet", _LIAB_EQ, "2024-12-31", 9 * BILLION)
    _metric(memory_db, fid, "profitability", "return_on_assets", 0.05)
    _metric(memory_db, fid, "efficiency", "asset_turnover", 0.5)
    _metric(memory_db, fid, "liquidity", "current_ratio", 1.2)  # not an affected metric

    report = quality.gate_version(memory_db, "metrics-v1", filing_id=fid)
    rows = {
        (r["metric_group"], r["metric_name"], r["severity"], r["quarantined"])
        for r in memory_db.execute(
            "SELECT metric_group, metric_name, severity, quarantined FROM data_quality_issue"
        )
    }
    assert rows == {
        ("identity", "CON_01", "SOFT", 0),
        ("profitability", "return_on_assets", "SOFT", 1),
        ("efficiency", "asset_turnover", "SOFT", 1),
    }
    assert report.filings_by_rule["CON_01"] == 1
    evidence = json.loads(
        memory_db.execute(
            "SELECT evidence_json FROM data_quality_issue "
            "WHERE metric_group = 'identity' AND rule_id = 'CON_01'"
        ).fetchone()[0]
    )
    assert evidence["level"] == "block" and evidence["n_terms"] == 2
    # append-only: a second run inserts nothing
    assert quality.gate_version(memory_db, "metrics-v1", filing_id=fid).inserted == 0


# -- CON_06 proper (F1b) ------------------------------------------------------------------------


def _sp(  # noqa: PLR0913 - one SubperiodFacts fixture, all fields explicit
    *,
    concept: str = _PROFIT_LOSS,
    quarters: dict[str, float] | None = None,
    agg: float | None = None,
    net_income: float | None = None,
    fye_md: tuple[int, int] | None = (12, 31),
    agg_column: str = "2024-09-30 (YTD)",
) -> quality.SubperiodFacts:
    cols = dict(quarters or {})
    if agg is not None:
        cols[agg_column] = agg
    duration = {("income_statement", concept): cols}
    if net_income is not None:
        duration[("income_statement", _NET_INCOME)] = {agg_column: net_income}
    return quality.SubperiodFacts(fye_md, duration)


_QUARTERS_3 = {
    "2024-03-31 (Q1)": 100 * MILLION,
    "2024-06-30 (Q2)": 100 * MILLION,
    "2024-09-30 (Q3)": 100 * MILLION,
}


def test_con06_pass() -> None:
    results, incomplete, _nt = quality.subperiod_results(
        _sp(quarters=_QUARTERS_3, agg=300 * MILLION, net_income=300 * MILLION)
    )
    assert incomplete == []
    assert len(results) == 1
    assert (
        results[0].level == "pass"
        and results[0].n_terms == 4
        and results[0].quarter_columns
        == (
            "2024-03-31 (Q1)",
            "2024-06-30 (Q2)",
            "2024-09-30 (Q3)",
        )
    )


def test_con06_warn_and_block() -> None:
    warn, _, _nt = quality.subperiod_results(
        _sp(quarters=_QUARTERS_3, agg=304 * MILLION, net_income=1_000 * MILLION)
    )
    # rounding 2M, threshold = 0.5% x 1,000M = 5M, diff 4M -> warn
    assert warn[0].level == "warn"
    block, _, _nt = quality.subperiod_results(
        _sp(quarters=_QUARTERS_3, agg=310 * MILLION, net_income=1_000 * MILLION)
    )
    assert block[0].level == "block"


def test_con06_incomplete_chain_is_not_tested() -> None:
    # a 9-month YTD with only Q2 + Q3 (Q1 missing) -> incomplete, no result
    partial = {"2024-06-30 (Q2)": 100 * MILLION, "2024-09-30 (Q3)": 100 * MILLION}
    results, incomplete, _nt = quality.subperiod_results(
        _sp(quarters=partial, agg=300 * MILLION, net_income=300 * MILLION)
    )
    assert results == []
    assert len(incomplete) == 1 and incomplete[0]["concept"] == _PROFIT_LOSS


def test_con06_missing_base_never_blocks() -> None:
    # no NetIncomeLoss filed -> base missing -> WARN cap even for a huge gap
    results, _, _nt = quality.subperiod_results(_sp(quarters=_QUARTERS_3, agg=900 * MILLION))
    assert results[0].level == "warn" and results[0].base_missing is True


def test_con06_a_shares_concept_is_not_tested() -> None:
    """F1c-1: ``WeightedAverageNumberOfDilutedSharesOutstanding`` is ``sharesItemType``, so its
    complete set is not an additive amount and is not tested."""
    shares = "us-gaap_WeightedAverageNumberOfDilutedSharesOutstanding"
    results, _, not_tested = quality.subperiod_results(
        _sp(concept=shares, quarters=_QUARTERS_3, agg=300 * MILLION, net_income=300 * MILLION)
    )
    assert results == [] and not_tested["not_additive"] == 1


def test_con06_an_extension_concept_is_not_tested() -> None:
    """F1c-1: a concept the 2026 schema does not declare (an extension) is not tested."""
    results, _, not_tested = quality.subperiod_results(
        _sp(concept="eqr_AllocationOfNetIncome", quarters=_QUARTERS_3, agg=300 * MILLION)
    )
    assert results == [] and not_tested["type_not_in_schema"] == 1


def test_con06_length_comes_from_dates_not_the_label() -> None:
    """F1c-2: a 6-month YTD (two quarters, fye 12-31) is tested on its own dates; the filing's
    ``fiscal_period`` label is never read (the CSX 2024Q3 shape: a Q3 label on a 2024-06-30 end)."""
    two = {"2024-03-31 (Q1)": 100 * MILLION, "2024-06-30 (Q2)": 100 * MILLION}
    results, incomplete, _ = quality.subperiod_results(
        _sp(
            quarters=two, agg=200 * MILLION, net_income=200 * MILLION, agg_column="2024-06-30 (YTD)"
        )
    )
    assert incomplete == []
    assert len(results) == 1 and results[0].level == "pass" and results[0].n_terms == 3


def test_con06_block_quarantines_the_ttm_metrics(memory_db: Database) -> None:
    _asset(memory_db)
    # a stored 10-K sets the company's fiscal-year end (F1c-2), which the gate reads, not a label.
    seed_filing(memory_db, 1, period_end="2023-12-31", filing_date="2024-02-15", form="10-K")
    fid = seed_filing(
        memory_db,
        1,
        period_end="2024-09-30",
        filing_date="2024-11-01",
        form="10-Q",
        fiscal_period="2024Q3",
    )
    for column, value in _QUARTERS_3.items():
        _fact(memory_db, fid, "income_statement", _PROFIT_LOSS, column, value)
    _fact(memory_db, fid, "income_statement", _PROFIT_LOSS, "2024-09-30 (YTD)", 310 * MILLION)
    _fact(memory_db, fid, "income_statement", _NET_INCOME, "2024-09-30 (YTD)", 1_000 * MILLION)
    _metric(memory_db, fid, "profitability", "return_on_assets", 0.05)

    quality.gate_version(memory_db, "metrics-v1", filing_id=fid)
    rows = {
        (r["rule_id"], r["metric_group"], r["metric_name"], r["quarantined"])
        for r in memory_db.execute(
            "SELECT rule_id, metric_group, metric_name, quarantined FROM data_quality_issue"
        )
    }
    assert ("CON_06", "identity", f"CON_06 {_PROFIT_LOSS} 2024-09-30 (YTD)", 0) in rows
    assert ("CON_06", "profitability", "return_on_assets", 1) in rows


def test_con19_is_not_tested_when_the_filed_sign_is_not_stored(
    memory_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2: a database without the ``preferred_sign`` column still tests CON-01; CON-19 is not
    tested (its NCI operand has no filed sign)."""
    _asset(memory_db)
    fid = seed_filing(memory_db, 1, period_end="2024-12-31", filing_date="2025-02-01")
    _fact(memory_db, fid, "balance_sheet", _ASSETS, "2024-12-31", 10 * BILLION)
    _fact(memory_db, fid, "balance_sheet", _LIAB_EQ, "2024-12-31", 10 * BILLION)
    _fact(memory_db, fid, "income_statement", _PROFIT_LOSS, "2024-12-31 (FY)", 1_000 * MILLION)
    _fact(memory_db, fid, "income_statement", _NET_INCOME, "2024-12-31 (FY)", 800 * MILLION)
    _fact(
        memory_db,
        fid,
        "income_statement",
        _NCI_INCOME,
        "2024-12-31 (FY)",
        -200 * MILLION,
        sign=-1.0,
    )

    def _results(facts: quality.FilingFacts) -> set[str]:
        balance = {c: facts.value(c, facts.instant_column) for c in quality._BALANCE_CONCEPTS}
        flow = {c: facts.value(c, facts.flow_column) for c in quality._FLOW_CONCEPTS}
        return {
            r.rule_id
            for r in quality.evaluate_identities(
                balance,
                flow,
                instant_column=facts.instant_column,
                flow_column=facts.flow_column,
                skip_con19=quality.con19_sign_missing(facts),
            )
        }

    with_sign = quality.load_facts(memory_db, [fid])[fid]
    assert quality.con19_sign_missing(with_sign) is False
    assert _results(with_sign) >= {"CON_01", "CON_19"}

    monkeypatch.setattr(quality, "_has_preferred_sign", lambda conn: False)
    no_sign = quality.load_facts(memory_db, [fid])[fid]
    assert quality.con19_sign_missing(no_sign) is True
    assert "CON_01" in _results(no_sign)
    assert "CON_19" not in _results(no_sign)


def _nci_facts(values: dict[str, float]) -> quality.FilingFacts:
    column = "2024-12-31 (FY)"
    by_key = {("income_statement", c): {column: v} for c, v in values.items()}
    signed = frozenset(("income_statement", c, column) for c in values)
    return quality.FilingFacts(
        form="10-K",
        period_end="2024-12-31",
        by_key=by_key,
        instant_column=None,
        flow_column=column,
        ytd_column=None,
        signed=signed,
    )


def test_con19_nci_udr_shape_sums_the_generic_and_a_child() -> None:
    """F3 (UDR): the generic NCI and the redeemable child do not sum (they add), so both stand."""
    operand = quality.nci_operand(
        _nci_facts({quality._NCI_INCOME: 104_000.0, quality._NCI_REDEEMABLE: 10_873_000.0})
    )
    assert operand.concepts == (quality._NCI_INCOME, quality._NCI_REDEEMABLE)
    assert operand.value == 10_977_000.0


def test_con19_nci_total_stands_for_its_breakdown() -> None:
    """F3: a filed total whose filed descendants sum to it stands for them (descendants dropped)."""
    operand = quality.nci_operand(
        _nci_facts(
            {
                quality._NCI_INCOME: 10_977_000.0,
                quality._NCI_REDEEMABLE: 10_873_000.0,
                quality._NCI_NONREDEEMABLE: 104_000.0,
            }
        )
    )
    assert operand.concepts == (quality._NCI_INCOME,)
    assert operand.value == 10_977_000.0


def test_con19_nci_children_only_is_tested() -> None:
    """F3: with no generic filed, the two children's sum is the NCI operand."""
    operand = quality.nci_operand(
        _nci_facts({quality._NCI_REDEEMABLE: 10_873_000.0, quality._NCI_NONREDEEMABLE: 104_000.0})
    )
    assert operand.concepts == (quality._NCI_REDEEMABLE, quality._NCI_NONREDEEMABLE)
    assert operand.value == 10_977_000.0


def test_a_warn_rule_at_the_material_level_quarantines_nothing(memory_db: Database) -> None:
    _asset(memory_db)
    fid = seed_filing(memory_db, 1, period_end="2024-12-31", filing_date="2025-02-01")
    _fact(memory_db, fid, "balance_sheet", _ASSETS, "2024-12-31", 10 * BILLION)
    _fact(memory_db, fid, "balance_sheet", _ASSETS_CURRENT, "2024-12-31", 3 * BILLION)
    _fact(memory_db, fid, "balance_sheet", _ASSETS_NONCURRENT, "2024-12-31", 2 * BILLION)
    _metric(memory_db, fid, "efficiency", "asset_turnover", 0.5)

    quality.gate_version(memory_db, "metrics-v1", filing_id=fid)
    rows = [
        (r["metric_group"], r["metric_name"], r["quarantined"])
        for r in memory_db.execute(
            "SELECT metric_group, metric_name, quarantined FROM data_quality_issue"
        )
    ]
    assert rows == [("identity", "CON_02", 0)]
    level = json.loads(
        memory_db.execute(
            "SELECT evidence_json FROM data_quality_issue WHERE rule_id = 'CON_02'"
        ).fetchone()[0]
    )["level"]
    assert level == "block"  # the level reached is recorded; the severity stays a WARN
