"""``metric_measures``, ``period_measures`` and ``n1_amendments``: the metric-definition, period and amendment checks
(MET-03, MET-04, MET-09, APP-07, PER-08, PER-12 to PER-14, N1), each on hand-built records or submissions (T-147)."""

from __future__ import annotations

import pytest
from sec_xcheck import metric_measures as mm
from sec_xcheck import n1_amendments as n1
from sec_xcheck import period_measures as pm
from sec_xcheck.submissions import page_names, rows_of
from sec_xcheck_support import metric, rec

# -- MET-03 / L-05 / N10 -------------------------------------------------------------------------


def test_the_21_percent_default_shows_as_an_effective_rate_when_pretax_is_not_positive() -> None:
    loss = rec(items={"pretax_income": -5.0}, metrics={"effective_tax_rate": metric(0.21)})
    assert mm.tax_rate_defaulted(loss) and mm.tax_rate_defaulted_on_loss(loss)
    profit = rec(
        items={"pretax_income": 100.0, "income_tax": 25.0},
        metrics={"effective_tax_rate": metric(0.25)},
    )
    assert not mm.tax_rate_defaulted(profit) and mm.nopat_on_filed_rate(profit)


def test_nopat_on_an_operating_loss_is_reduced_by_tax() -> None:
    taxed = rec(items={"operating_income": -100.0}, metrics={"nopat": metric(-79.0)})
    assert mm.nopat_taxed_on_loss(taxed)
    assert not mm.nopat_taxed_on_loss(
        rec(items={"operating_income": -100.0}, metrics={"nopat": metric(-100.0)})
    )


def test_the_tax_rate_clamp_hides_a_raw_rate_outside_zero_to_fifty_percent() -> None:
    assert mm.tax_rate_clamped(
        rec(items={"pretax_income": 100.0, "income_tax": -10.0})
    )  # a benefit, clamped to 0
    assert mm.tax_rate_clamped(rec(items={"pretax_income": 100.0, "income_tax": 80.0}))
    assert not mm.tax_rate_clamped(rec(items={"pretax_income": 100.0, "income_tax": 25.0}))


# -- MET-04 / MET-09 / APP-07 ---------------------------------------------------------------------


def test_growth_from_a_base_at_or_below_zero_is_not_meaningful() -> None:
    r = rec(metrics={"net_income_growth": metric(0.5, net_income_prior=-10.0)})
    assert mm.growth_on_nonpositive_base("net_income_growth")(r)
    assert not mm.growth_on_nonpositive_base("net_income_growth")(
        rec(metrics={"net_income_growth": metric(0.5, net_income_prior=10.0)})
    )


def test_net_cash_with_positive_ebitda_is_the_case_t116_discards() -> None:
    cash_rich = rec(
        metrics={
            "net_debt_to_ebitda": metric(
                -1.2, operating_income=80.0, depreciation_amortization=20.0
            )
        }
    )
    assert mm.net_cash_positive_ebitda(cash_rich) and not mm.nd_ebitda_on_nonpositive_ebitda(
        cash_rich
    )
    burning = rec(
        metrics={
            "net_debt_to_ebitda": metric(
                -399.0, operating_income=-20.0, depreciation_amortization=9.0
            )
        }
    )
    assert mm.nd_ebitda_on_nonpositive_ebitda(burning) and not mm.net_cash_positive_ebitda(burning)


def test_ratios_on_negative_equity_and_non_positive_invested_capital() -> None:
    r = rec(
        items={"equity": -50.0},
        metrics={
            "return_on_equity": metric(0.4),
            "debt_to_equity": metric(-3.0),
            "return_on_invested_capital": metric(0.1, equity=-50.0, total_debt=40.0, cash=0.0),
        },
    )
    assert (
        mm.roe_on_negative_equity(r)
        and mm.de_on_negative_equity(r)
        and mm.roic_on_nonpositive_capital(r)
    )
    assert not mm.roic_on_nonpositive_capital(
        rec(metrics={"return_on_invested_capital": metric(0.1, equity=50.0, total_debt=40.0)})
    )


# -- PER-08 / PER-12 to PER-14 ---------------------------------------------------------------------


def _periods(tag: str, gap: int | None, fy_dates: list[str] | None = None) -> dict[str, object]:
    return {"tag": tag, "prior_key": "x", "prior_gap_days": gap, "fy_dates": fy_dates or []}


def test_a_quarter_pairing_the_code_accepts_and_the_rule_rejects() -> None:
    assert pm.gap_accepted_by_code_only(
        rec(periods=_periods("Q3", 350))
    )  # within ±20 days of 365, outside [364, 371]
    assert not pm.gap_accepted_by_code_only(rec(periods=_periods("Q3", 364)))
    assert pm.fy_prior_not_a_year(rec(periods=_periods("FY", 210)))  # a changed fiscal year end
    assert not pm.fy_prior_not_a_year(rec(periods=_periods("FY", 371)))


def test_cagr_crosses_asc842_for_a_fiscal_year_that_began_before_the_adoption() -> None:
    november_filer = rec(
        periods=_periods("FY", 364, ["2019-11-30", "2020-11-30", "2021-11-30"]),
        metrics={"revenue_cagr": metric(0.1)},
    )
    calendar_filer = rec(
        periods=_periods("FY", 364, ["2019-12-31", "2020-12-31", "2021-12-31"]),
        metrics={"revenue_cagr": metric(0.1)},
    )
    check = pm.cagr_crosses("revenue_cagr", pm.ASC842_FIRST_YEAR_BEGINS)
    assert check(november_filer) and not check(calendar_filer)


def test_cecl_crossing_applies_to_lenders_only() -> None:
    dates = ["2019-12-31", "2020-12-31", "2021-12-31"]
    bank = rec(
        sub_industry="Regional Banks",
        periods=_periods("FY", 364, dates),
        metrics={"net_income_cagr": metric(0.1)},
    )
    maker = rec(
        sub_industry="Machinery",
        periods=_periods("FY", 364, dates),
        metrics={"net_income_cagr": metric(0.1)},
    )
    check = pm.cagr_crosses("net_income_cagr", pm.CECL_FIRST_YEAR_BEGINS, lenders=True)
    assert check(bank) and not check(maker)


# -- N1 ----------------------------------------------------------------------------------------------


def _f(accession: str, form: str, filed: str, report: str) -> dict[str, str]:
    return {"accessionNumber": accession, "form": form, "filingDate": filed, "reportDate": report}


def test_a_part_iii_amendment_filed_after_the_10k_is_the_filing_the_pipeline_keeps() -> None:
    filings = [
        _f("a-1", "10-K", "2024-02-20", "2023-12-31"),
        _f("a-2", "10-K/A", "2024-04-25", "2023-12-31"),
    ]
    (sim,) = n1.simulate_selection(filings, range(2024, 2025))
    assert sim["kept"]["accessionNumber"] == "a-2" and sim["outcome"] == "amendment_same_fy"


def test_an_amendment_of_an_earlier_year_can_displace_this_years_10k() -> None:
    filings = [
        _f("a-1", "10-K", "2024-02-20", "2023-12-31"),
        _f(
            "a-2", "10-K/A", "2024-06-01", "2022-12-31"
        ),  # an old year's amendment, filed later in 2024
    ]
    (sim,) = n1.simulate_selection(filings, range(2024, 2025))
    assert sim["outcome"] == "amendment_other_fy"


def test_a_plain_year_keeps_the_original_and_other_years_are_independent() -> None:
    filings = [
        _f("a-1", "10-K", "2023-02-20", "2022-12-31"),
        _f("a-2", "10-K", "2024-02-20", "2023-12-31"),
        _f("a-3", "10-Q", "2024-05-01", "2024-03-31"),
    ]
    assert [s["outcome"] for s in n1.simulate_selection(filings, range(2023, 2025))] == [
        "original",
        "original",
    ]


def test_stored_accessions_are_classified_by_their_form_at_the_source() -> None:
    by = {
        "a-1": _f("a-1", "10-K/A", "2024-04-25", "2023-12-31"),
        "a-2": _f("a-2", "10-K", "2024-02-20", "2023-12-31"),
    }
    assert n1.classify_stored("a-1", by) == "amendment"
    assert n1.classify_stored("a-2", by) == "original"
    assert n1.classify_stored("zzz", by) == "not_in_submissions"


def test_a_10q_amendment_shares_its_period_with_the_original() -> None:
    filings = [
        _f("q-1", "10-Q", "2024-05-01", "2024-03-31"),
        _f("q-2", "10-Q/A", "2024-06-15", "2024-03-30"),
    ]
    ((amendment, original),) = n1.quarterly_collisions(filings)
    assert (amendment["accessionNumber"], original["accessionNumber"]) == ("q-2", "q-1")


def test_submissions_rows_and_pages() -> None:
    block = {
        "accessionNumber": ["a", "b"],
        "form": ["10-K", "8-K"],
        "filingDate": ["2024-02-01", "2024-03-01"],
        "reportDate": ["2023-12-31", ""],
    }
    assert [r["form"] for r in rows_of(block)] == ["10-K", "8-K"]
    doc = {
        "filings": {
            "files": [
                {"name": "old.json", "filingTo": "2020-01-01"},
                {"name": "new.json", "filingTo": "2023-06-01"},
            ]
        }
    }
    assert page_names(doc, "2021-12-01") == ["new.json"]


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("2024-03-31", "2024-03-30", True),
        ("2024-03-31", "2024-04-30", False),
        (None, "2024-03-31", False),
    ],
)
def test_close_is_a_week_either_way(a: str | None, b: str, expected: bool) -> None:
    assert n1.close(a, b) is expected
