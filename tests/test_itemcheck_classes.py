"""The itemcheck's "none" class, split three ways (reviewer A, P3 round 1, 2026-10-20).

A stored value that is **absent** while the SEC holds a non-dimensional fact of one of the item's
own concepts for the same accession and period is not one finding but three:

* the gateway **dropped** the row (the run's drop log, item K) -- the gateway regression: FAIL;
* the value is stored under another concept the item does not read -- WARN, counted by item;
* the gateway never served the row -- WARN, notes only.
"""

from __future__ import annotations

import json
from pathlib import Path

from sec_xcheck.itemcheck import classify, load_drop_log

REVENUE = "RevenueFromContractWithCustomerExcludingAssessedTax"


def _values() -> dict[float, set[str]]:
    return {100.0: {REVENUE}}


def _own() -> set[str]:
    return {REVENUE}


def test_a_gateway_dropped_row_whose_original_is_the_sec_fact_is_a_failure() -> None:
    cls, extra = classify(None, _own(), _values(), drops={REVENUE: 100.0})
    assert cls == "FAIL_gateway_dropped"
    assert extra == [REVENUE]


def test_a_gateway_drop_of_another_value_is_not_this_failure() -> None:
    """The drop log names the rows the gateway nulled; a drop of a different value says nothing
    about this one."""
    cls, _ = classify(None, _own(), _values(), drops={"CostOfRevenue": 100.0})
    assert cls == "WARN_never_served"


def test_a_value_stored_under_another_concept_is_a_warning_by_item() -> None:
    cls, extra = classify(
        None, _own(), _values(), stored_facts={100.0: {"InterestExpenseOperating"}}
    )
    assert cls == "WARN_other_concept"
    assert extra == ["InterestExpenseOperating"]


def test_a_value_stored_under_the_items_own_concept_is_not_another_concept() -> None:
    """The value is there and the item reads it: nothing to report."""
    cls, _ = classify(None, _own(), _values(), stored_facts={100.0: {REVENUE}})
    assert cls == "WARN_never_served"


def test_a_row_the_gateway_never_served_is_a_warning() -> None:
    cls, extra = classify(None, _own(), _values())
    assert cls == "WARN_never_served"
    assert extra == [REVENUE]


def test_no_sec_fact_of_the_items_own_concepts_stays_none() -> None:
    """The SEC holds no fact of the item's own concepts for that accession and period: there is
    nothing to compare, and the stored value may be right."""
    assert classify(None, _own(), {100.0: {"SomeOtherConcept"}})[0] == "none"


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
    assert load_drop_log(str(path)) == {"0000016918-23-000103": {REVENUE: 4003533000.0}}
    assert load_drop_log(None) == {}
