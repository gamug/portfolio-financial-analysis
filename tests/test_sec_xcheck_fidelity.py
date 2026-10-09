"""``scripts/sec_xcheck/fidelity.py``: a re-run of today's resolver over stored ``financial_facts`` is faithful to the
metrics stored from the same payload (T-147, level (b)). End to end on a real captured filing: the facts and metrics are
written the way the pipeline writes them, then replayed from storage alone."""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import seed_filing
from portfolio_common.db import Database
from sec_xcheck import fidelity
from sec_xcheck.common import filings, own_period_key, statements_from_db

from fundamental_agent import db
from fundamental_agent.metrics import COMPUTERS
from fundamental_agent.statements import Statements, iter_facts


@pytest.fixture
def stored(memory_db: Database, aapl_10k: Statements) -> Database:
    """AAPL FY2023: facts and the computed metrics stored exactly as the pipeline stores them."""
    memory_db.execute("INSERT INTO sectors (id, name) VALUES (1, 'Information Technology')")
    memory_db.execute(
        "INSERT INTO assets (id, ticker, company_name, cik, sector_id, sub_industry) "
        "VALUES (1, 'AAPL', 'Apple', '0000320193', 1, 'Technology Hardware, Storage & Peripherals')"
    )
    period = aapl_10k.latest_fy()
    assert period is not None
    filing_id = seed_filing(memory_db, 1, period_end=period.date, filing_date="2023-11-03")
    memory_db.execute(
        "UPDATE sec_filings SET accession_number = '0000320193-23-000106' WHERE id = ?",
        (filing_id,),
    )
    db.append_financial_facts(memory_db, filing_id, iter_facts(aapl_10k))
    prior = aapl_10k.prior_of(period)
    results = [
        (group, result)
        for group, compute in COMPUTERS.items()
        for result in compute(aapl_10k, period.key, prior.key if prior else None, None)
    ]
    db.record_metrics(memory_db, filing_id, results)
    return memory_db


def test_the_rebuilt_statements_resolve_the_same_revenue_as_the_payload(
    stored: Database, aapl_10k: Statements
) -> None:
    (f,) = filings(stored)
    rebuilt = statements_from_db(stored, f["id"])
    key = own_period_key(rebuilt, f["form"], f["period_end"])
    assert key is not None
    for item in (
        "revenue",
        "net_income",
        "total_assets",
        "equity",
        "operating_cash_flow",
        "capital_expenditure",
    ):
        assert rebuilt.get(item, key) == aapl_10k.get(item, key)


def test_a_replay_of_a_stored_filing_matches_every_stored_input(stored: Database) -> None:
    total, by_input, examples = fidelity.run(stored)
    assert examples == []
    assert total["filings_compared"] == 1 and total["MATCH"] > 50
    assert fidelity.rates(total) == (1.0, 1.0)
    assert by_input["revenue"]["MATCH"] > 0


def test_a_stored_input_that_differs_from_the_replay_is_reported(stored: Database) -> None:
    row = stored.execute(
        "SELECT id, inputs_json FROM fundamental_metrics WHERE metric_name = 'net_margin'"
    ).fetchone()
    inputs: dict[str, Any] = json.loads(row["inputs_json"])
    inputs["revenue"] += 1e9
    stored.execute(
        "UPDATE fundamental_metrics SET inputs_json = ? WHERE id = ?",
        (json.dumps(inputs), row["id"]),
    )
    total, _by, examples = fidelity.run(stored)
    assert total["VALUE_DIFFERS"] == 1
    assert examples[0][:2] == ("VALUE_DIFFERS", "AAPL") and examples[0][5] == "revenue"
    assert fidelity.rates(total)[0] < 1.0


def test_compare_inputs_classes() -> None:
    out = fidelity.compare_inputs({"a": 1.0, "b": 2.0, "c": 3.0}, {"a": 1.0, "b": 9.0, "d": 4.0})
    assert out == {"a": "MATCH", "b": "VALUE_DIFFERS", "c": "STORED_ONLY", "d": "REPLAY_ONLY"}


def test_ttm_path_inputs_are_not_counted_against_fidelity() -> None:
    assert fidelity.is_ttm_input("revenue_ttm") and fidelity.is_ttm_input("annualized_x4")
    assert not fidelity.is_ttm_input("revenue")


def test_a_revenue_total_that_the_statement_contradicts_is_a_documented_drift(
    nee_10k: Statements,
) -> None:
    key = nee_10k.latest_fy().key  # type: ignore[union-attr]
    # NEE's revenue is the utility total (T-102): a difference there is the documented engine fix, not a failure
    assert fidelity.drift_for(nee_10k, "revenue", key) == "T-102"
    assert fidelity.drift_for(nee_10k, "equity", key) is None
