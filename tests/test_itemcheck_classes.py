"""The itemcheck's "none" class, split three ways (reviewer A, P3 round 1, 2026-10-20).

A stored value that is **absent** while the SEC holds a non-dimensional fact of one of the item's
own concepts for the same accession and period is not one finding but three:

* the gateway **dropped** the row (the run's drop log, item K) -- the gateway regression: FAIL;
* the value is stored under another concept the item does not read -- WARN, counted by item;
* the gateway never served the row -- WARN, notes only.

PR #178, review round 1: the drop log keeps **every** row the gateway nulled (the same concept in
several columns, two rows in one column), and the match is by **value** against the SEC's facts of
the item's own concepts -- the gateway's concept and the SEC's need not be the same one -- on the
item's own **statement** (the gateway drops the cash-flow cash rows by design).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from sec_xcheck.itemcheck import classify, load_drop_log, map_values, own_concepts, stored_facts

from fundamental_agent.pipeline import DROP_LOG_ENV, log_gateway_drops
from fundamental_agent.statements import REGISTRY

FIXTURES = Path(__file__).parent / "fixtures"
PAYLOADS = json.loads((FIXTURES / "gateway_drop_payloads.json").read_text(encoding="utf-8"))
REVENUE = "RevenueFromContractWithCustomerExcludingAssessedTax"
CASH = "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"
# PHM filed the 2023-09-30 quarter's revenue as ``us-gaap:Revenues`` (A's round 1, checked in the
# SEC's companyconcept); the gateway dropped the row it had tagged with the other concept.
PHM_REVENUE = 4_003_533_000.0


def _payload(label: str) -> dict[str, Any]:
    return cast("dict[str, Any]", PAYLOADS["payloads"][label])


def _log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str
) -> dict[str, list[tuple[str, str, str, str, float]]]:
    """The run's drop log for one payload: the writer writes it, the reader reads it back."""
    payload = _payload(label)
    path = tmp_path / "gateway_drops.jsonl"
    monkeypatch.setenv(DROP_LOG_ENV, str(path))
    assert log_gateway_drops(payload, payload["accession"]) == len(payload["corrections"])
    return cast("dict[str, list[tuple[str, str, str, str, float]]]", load_drop_log(str(path)))


def test_the_drop_log_keeps_every_row_the_gateway_nulled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PHM's revenue row is nulled in four columns; only the last one survived the old reader."""
    drops = _log(tmp_path, monkeypatch, "PHM 10-Q 2023Q3")["0000822416-23-000042"]
    assert len(drops) == 8
    assert [o for _s, _c, _d, _t, o in drops if o == PHM_REVENUE] == [PHM_REVENUE]
    assert {(statement, concept, date) for statement, concept, date, _t, _o in drops} >= {
        ("income_statement", REVENUE, "2023-09-30"),
        ("income_statement", REVENUE, "2022-09-30"),
    }


def test_two_rows_in_one_column_are_both_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agilent's cash row is nulled twice in the same column: the beginning balance
    (1,490,000,000) and the ending one (1,056,000,000)."""
    drops = _log(tmp_path, monkeypatch, "Agilent 10-K FY2022")["0001090872-22-000026"]
    cash = [o for _s, concept, _d, _t, o in drops if concept == CASH]
    assert 1_490_000_000.0 in cash
    assert 1_056_000_000.0 in cash
    assert len([1 for _s, _c, date, _t, _o in drops if date == "2022-10-31"]) == 3


def test_the_same_row_twice_in_the_log_is_one_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The log is appended on every fetch: a retry repeats the row, and the readers count
    distinct (accession, concept, column, original)."""
    payload = _payload("PHM 10-Q 2023Q3")
    path = tmp_path / "gateway_drops.jsonl"
    monkeypatch.setenv(DROP_LOG_ENV, str(path))
    log_gateway_drops(payload, payload["accession"])
    log_gateway_drops(payload, payload["accession"])
    assert len(load_drop_log(str(path))["0000822416-23-000042"]) == 8


def test_phms_dropped_revenue_is_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The canary: the gateway dropped PHM's revenue row, and the SEC holds the same value as
    ``us-gaap:Revenues`` for the same accession and period."""
    own = own_concepts()["revenue"]
    assert REVENUE in own
    assert "Revenues" in own
    drops = _log(tmp_path, monkeypatch, "PHM 10-Q 2023Q3")["0000822416-23-000042"]
    cls, extra = classify(
        None,
        own,
        {PHM_REVENUE: {"Revenues"}},
        drops=drops,
        period_end="2023-09-30",
        statements=REGISTRY["revenue"].statements,
    )
    assert cls == "FAIL_gateway_dropped"
    assert extra == ["Revenues"]


def test_a_dropped_row_of_another_period_is_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The comparative quarter's dropped row says nothing about this filing's own period."""
    own = own_concepts()["revenue"]
    drops = _log(tmp_path, monkeypatch, "PHM 10-Q 2023Q3")["0000822416-23-000042"]
    cls, _ = classify(
        None,
        own,
        {3_895_062_000.0: {"Revenues"}},
        drops=drops,
        period_end="2023-09-30",
        statements=REGISTRY["revenue"].statements,
    )
    assert cls == "WARN_never_served"


def test_the_cash_flow_cash_drops_are_not_a_failure_for_the_balance_sheet_cash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway drops the cash-flow cash rows by design (PHM's four and Agilent's six are all
    ``cash_flow``); the balance-sheet cash item must never be a failure for them."""
    own = own_concepts()["cash"]
    statements = REGISTRY["cash"].statements
    assert statements == ("balance_sheet",)
    phm = _log(tmp_path, monkeypatch, "PHM 10-Q 2023Q3")["0000822416-23-000042"]
    cls, _ = classify(
        None,
        own,
        {1_094_553_000.0: {CASH}},
        drops=phm,
        period_end="2023-09-30",
        statements=statements,
    )
    assert cls != "FAIL_gateway_dropped"
    agilent = _log(tmp_path, monkeypatch, "Agilent 10-K FY2022")["0001090872-22-000026"]
    cls, _ = classify(
        None,
        own,
        {1_056_000_000.0: {CASH}},
        drops=agilent,
        period_end="2022-10-31",
        statements=statements,
    )
    assert cls != "FAIL_gateway_dropped"


def test_the_statement_is_what_keeps_the_cash_flow_cash_rows_out() -> None:
    """A drop of a concept the item reads, on another statement, is never its failure: the cash
    item reads the balance sheet, and the gateway drops the cash-flow rows by design."""
    own = own_concepts()["cash"]
    assert "CashAndCashEquivalentsAtCarryingValue" in own
    drops = [
        (
            "cash_flow",
            "CashAndCashEquivalentsAtCarryingValue",
            "2022-10-31",
            "FY",
            1_056_000_000.0,
        )
    ]
    cls, _ = classify(
        None,
        own,
        {1_056_000_000.0: {"CashAndCashEquivalentsAtCarryingValue"}},
        drops=drops,
        period_end="2022-10-31",
        statements=REGISTRY["cash"].statements,
    )
    assert cls != "FAIL_gateway_dropped"
    # the same row on the item's own statement is the failure the rule is about
    cls, _ = classify(
        None,
        own,
        {1_056_000_000.0: {"CashAndCashEquivalentsAtCarryingValue"}},
        drops=[("balance_sheet", *drops[0][1:])],
        period_end="2022-10-31",
        statements=REGISTRY["cash"].statements,
    )
    assert cls == "FAIL_gateway_dropped"


def test_the_same_drop_on_two_statements_keeps_both_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dedupe key carries the statement: the same concept, column and value dropped on the
    cash-flow statement and on the balance sheet are two rows. With the statement left out, only
    the first row in the log survived and the class depended on the log's order."""
    path = tmp_path / "gateway_drops.jsonl"
    monkeypatch.setenv(DROP_LOG_ENV, str(path))
    row = {
        "accession": "0001090872-22-000026",
        "concept": "us-gaap_CashAndCashEquivalentsAtCarryingValue",
        "column": "2022-10-31",
        "original": 1_056_000_000.0,
        "corrected": None,
        "rule": "T-042",
        "reason": "no_filed_nondimensional_fact",
    }
    with path.open("w", encoding="utf-8") as handle:
        for statement in ("cash_flow", "balance_sheet"):
            handle.write(json.dumps({**row, "statement": statement}) + "\n")
    drops = load_drop_log(str(path))["0001090872-22-000026"]
    assert len(drops) == 2
    assert {statement for statement, _c, _d, _t, _o in drops} == {"cash_flow", "balance_sheet"}
    own = own_concepts()["cash"]
    values = {1_056_000_000.0: {"CashAndCashEquivalentsAtCarryingValue"}}
    # the cash-flow row comes first, but the balance-sheet drop survived: the item that reads the
    # balance sheet is a failure for its own dropped row
    cls, _ = classify(
        None,
        own,
        values,
        drops=drops,
        period_end="2022-10-31",
        statements=REGISTRY["cash"].statements,
    )
    assert cls == "FAIL_gateway_dropped"
    # and the cash-flow item reads its own dropped row
    cls, _ = classify(
        None,
        own,
        values,
        drops=drops,
        period_end="2022-10-31",
        statements=("cash_flow",),
    )
    assert cls == "FAIL_gateway_dropped"


def test_a_concept_flag_concept_counts_as_the_items_own_concept() -> None:
    """T-160: a ``concept_flags`` concept is one the resolver reads and flags
    (``statements.py:232``); net income's available-to-common line is the check's case
    (``statements.py:774-779``)."""
    assert "NetIncomeLossAvailableToCommonStockholdersBasic" in own_concepts()["net_income"]


def test_a_resolver_named_concept_counts_as_the_items_own_concept() -> None:
    """T-160 round 2: only a concept the resolver can return **alone** is the item's own -- the
    bare ``Cash`` line (``statements.py:2651-2660``). A component of a sum is not (a stored value
    equal to one term is a coincidence or a defect; the sum is in ``SOURCED_MAPS``)."""
    assert "Cash" in own_concepts()["cash"]
    assert "CashEquivalentsAtCarryingValue" not in own_concepts()["cash"]
    assert "Depreciation" not in own_concepts()["depreciation_amortization"]
    assert "CurrentIncomeTaxExpenseBenefit" not in own_concepts()["income_tax"]


def test_a_stored_da_equal_to_one_child_is_not_ok_own_concept() -> None:
    """T-160 round 2: one D&A child is not the item's own concept; a single component is a
    coincidence or a defect, never a whole (``_da_sourced_sum`` needs Depreciation plus another
    child, ``statements.py:447-452``)."""
    own = own_concepts()["depreciation_amortization"]
    values = {100.0: {"Depreciation"}}
    cls, _ = classify(100.0, own, values, map_values("depreciation_amortization", values))
    assert cls != "ok_own_concept"
    assert cls == "OTHER_CONCEPT"


def test_a_depreciation_plus_amortization_sum_is_ok_sourced_map() -> None:
    """T-160 round 2: the exact two-part D&A sum is a sourced map (``statements.py:662``,
    ``_da_exact_sum`` at ``:2774``)."""
    own = own_concepts()["depreciation_amortization"]
    values = {100.0: {"Depreciation"}, 20.0: {"AmortizationOfIntangibleAssets"}}
    mapped = map_values("depreciation_amortization", values)
    assert mapped == [120.0]
    assert classify(120.0, own, values, mapped)[0] == "ok_sourced_map"


def test_a_value_stored_under_another_concept_is_a_warning_by_item() -> None:
    own = own_concepts()["revenue"]
    cls, extra = classify(
        None, own, {100.0: {REVENUE}}, stored_facts={100.0: {"InterestExpenseOperating"}}
    )
    assert cls == "WARN_other_concept"
    assert extra == ["InterestExpenseOperating"]


def test_a_value_stored_under_the_items_own_concept_is_not_another_concept() -> None:
    own = own_concepts()["revenue"]
    cls, _ = classify(None, own, {100.0: {REVENUE}}, stored_facts={100.0: {REVENUE}})
    assert cls == "WARN_never_served"


def test_a_row_the_gateway_never_served_is_a_warning() -> None:
    own = own_concepts()["revenue"]
    cls, extra = classify(None, own, {100.0: {REVENUE}})
    assert cls == "WARN_never_served"
    assert extra == [REVENUE]


def test_no_sec_fact_of_the_items_own_concepts_stays_none() -> None:
    """The SEC holds no fact of the item's own concepts for that accession and period: there is
    nothing to compare, and the stored value may be right."""
    own = own_concepts()["revenue"]
    assert classify(None, own, {100.0: {"SomeOtherConcept"}})[0] == "none"


def test_the_drop_log_is_read_into_the_classifier_context(tmp_path: Path) -> None:
    path = tmp_path / "gateway_drops.jsonl"
    path.write_text(
        json.dumps(
            {
                "accession": "0000016918-23-000103",
                "statement": "income_statement",
                "concept": f"us-gaap_{REVENUE}",
                "column": "2023-05-31 (Q1)",
                "original": 4003533000.0,
                "rule": "T-042",
                "reason": "no_filed_nondimensional_fact",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert load_drop_log(str(path)) == {
        "0000016918-23-000103": [("income_statement", REVENUE, "2023-05-31", "Q1", 4003533000.0)]
    }
    assert load_drop_log(None) == {}


# -- PR (the itemcheck, 5a-5d) -------------------------------------------------------------------


def test_a_negative_dropped_original_matches_a_positive_sec_fact() -> None:
    """5a: the gateway compares **magnitudes** (data-mining 5c77262, ``src/sec_edgar/agent.py``:
    ``reconcile_with_filed_facts`` compares ``abs(abs(filed) - abs(rendered))``), so a negative
    original against a positive SEC fact is the same value."""
    own = own_concepts()["revenue"]
    cls, _ = classify(
        None,
        own,
        {4_003_533_000.0: {"Revenues"}},
        drops=[
            (
                "income_statement",
                REVENUE,
                "2023-09-30",
                "Q3",
                -4_003_533_000.0,
            )
        ],
        period_end="2023-09-30",
        statements=REGISTRY["revenue"].statements,
    )
    assert cls == "FAIL_gateway_dropped"


def test_a_negative_stored_fact_under_another_concept_is_the_other_concept() -> None:
    """5b: the stored value is the value **as displayed** (db.py:73: the filed value times the
    preferred sign), so a negative stored fact matches a positive SEC fact."""
    own = own_concepts()["revenue"]
    cls, extra = classify(
        None,
        own,
        {100.0: {REVENUE}},
        stored_facts={-100.0: {"InterestExpenseOperating"}},
    )
    assert cls == "WARN_other_concept"
    assert extra == ["InterestExpenseOperating"]


def test_a_prefixed_own_concept_stored_fact_is_never_another_concept() -> None:
    """5c: the stored concepts lose their taxonomy prefix, the way ``own_concepts()`` strips it:
    a value stored under one of the item's own concepts is not another concept."""
    own = own_concepts()["revenue"]
    assert REVENUE in own
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE financial_facts (filing_id INTEGER, concept TEXT, period_key TEXT, value REAL)"
    )
    conn.execute(
        "INSERT INTO financial_facts VALUES (1, ?, '2023-09-30 (Q3)', 100.0)",
        (f"us-gaap_{REVENUE}",),
    )
    conn.commit()
    stored = stored_facts(conn, 1, "2023-09-30")
    assert stored == {100.0: {REVENUE}}
    cls, extra = classify(None, own, {100.0: {REVENUE}}, stored_facts=stored)
    assert cls == "WARN_never_served"
    assert extra == [REVENUE]
    conn.close()


def test_a_dropped_untagged_cash_flow_column_matches_a_167_day_sec_fact() -> None:
    """5d: A's duration rule -- an untagged column matches any duration under PER-08's year
    upper bound (a 24-week YTD column is 167 days), so the row is the gateway's drop."""
    own = own_concepts()["operating_cash_flow"]
    cls, _ = classify(
        None,
        own,
        {1_234_000_000.0: {"NetCashProvidedByUsedInOperatingActivities"}},
        drops=[
            (
                "cash_flow",
                "NetCashProvidedByUsedInOperatingActivities",
                "2023-09-30",
                "",
                1_234_000_000.0,
            )
        ],
        period_end="2023-09-30",
        statements=REGISTRY["operating_cash_flow"].statements,
        sec_facts=[("NetCashProvidedByUsedInOperatingActivities", 1_234_000_000.0, 167)],
    )
    assert cls == "FAIL_gateway_dropped"


def test_a_dropped_untagged_column_does_not_match_a_year_long_sec_fact() -> None:
    """5d: the rule has a bound -- a fact longer than PER-08's year (371 days) is not the dropped
    column's own duration."""
    own = own_concepts()["operating_cash_flow"]
    cls, _ = classify(
        None,
        own,
        {1_234_000_000.0: {"NetCashProvidedByUsedInOperatingActivities"}},
        drops=[
            (
                "cash_flow",
                "NetCashProvidedByUsedInOperatingActivities",
                "2023-09-30",
                "",
                1_234_000_000.0,
            )
        ],
        period_end="2023-09-30",
        statements=REGISTRY["operating_cash_flow"].statements,
        sec_facts=[("NetCashProvidedByUsedInOperatingActivities", 1_234_000_000.0, 400)],
    )
    assert cls != "FAIL_gateway_dropped"


def test_two_matching_facts_of_different_durations_are_ambiguous() -> None:
    """A's rule: two matching facts of different durations are ambiguous -- counted, not
    classified."""
    own = own_concepts()["operating_cash_flow"]
    cls, _ = classify(
        None,
        own,
        {1_234_000_000.0: {"NetCashProvidedByUsedInOperatingActivities"}},
        drops=[
            (
                "cash_flow",
                "NetCashProvidedByUsedInOperatingActivities",
                "2023-09-30",
                "",
                1_234_000_000.0,
            )
        ],
        period_end="2023-09-30",
        statements=REGISTRY["operating_cash_flow"].statements,
        sec_facts=[
            ("NetCashProvidedByUsedInOperatingActivities", 1_234_000_000.0, 84),
            ("NetCashProvidedByUsedInOperatingActivities", 1_234_000_000.0, 167),
        ],
    )
    assert cls != "FAIL_gateway_dropped"


def test_a_dropped_untagged_row_is_a_failure_when_the_form_has_no_fact_of_that_length() -> None:
    """5d, the gate (PR #179's review): the item's own facts come from the form's length, so a
    10-Q's year-to-date cash-flow fact (167-273 days) is never among them. The drop match must
    run before the "none" verdict, or the row is lost.

    The dropped column is **untagged**: for the 16/24-week filers that is the column the stored
    input reads (A's rule)."""
    own = own_concepts()["operating_cash_flow"]
    cls, _ = classify(
        None,
        own,
        {},  # no fact of the form's length
        drops=[
            (
                "cash_flow",
                "NetCashProvidedByUsedInOperatingActivities",
                "2023-09-30",
                "",
                1_234_000_000.0,
            )
        ],
        period_end="2023-09-30",
        statements=REGISTRY["operating_cash_flow"].statements,
        sec_facts=[("NetCashProvidedByUsedInOperatingActivities", 1_234_000_000.0, 167)],
    )
    assert cls == "FAIL_gateway_dropped"


def test_a_dropped_ytd_row_is_not_a_failure_because_the_quarter_input_does_not_read_it() -> None:
    """A's rule (reviewers A and B, 2026-10-22): a dropped ``(YTD)`` column is not the column the
    stored input reads -- the pipeline reads ``stmts.get(item, target.period.key)``, the filing's own
    quarter column. So a dropped year-to-date row is not the item's failure."""
    own = own_concepts()["operating_cash_flow"]
    cls, _ = classify(
        None,
        own,
        {},
        drops=[
            (
                "cash_flow",
                "NetCashProvidedByUsedInOperatingActivities",
                "2023-09-30",
                "YTD",
                1_234_000_000.0,
            )
        ],
        period_end="2023-09-30",
        statements=REGISTRY["operating_cash_flow"].statements,
        sec_facts=[("NetCashProvidedByUsedInOperatingActivities", 1_234_000_000.0, 182)],
    )
    assert cls != "FAIL_gateway_dropped"


def test_an_item_with_no_fact_and_no_drop_is_still_none() -> None:
    """The gate does not swallow the "none" verdict: without a drop there is nothing to classify."""
    own = own_concepts()["operating_cash_flow"]
    cls, _ = classify(
        None,
        own,
        {},
        drops=[],
        period_end="2023-09-30",
        statements=REGISTRY["operating_cash_flow"].statements,
        sec_facts=[("NetCashProvidedByUsedInOperatingActivities", 1_234_000_000.0, 182)],
    )
    assert cls == "none"
