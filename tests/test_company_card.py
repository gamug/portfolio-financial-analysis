"""The company card and the fidelity gate (T-151 part 3, D15/D16)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import seed_filing
from portfolio_common.db import Database

from fundamental_agent import db
from fundamental_agent.card import build_card, render
from fundamental_agent.fidelity import dq01_tolerance, run_fidelity
from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.statements import Statements, iter_facts

# a stored provenance block, as metrics/provenance.py writes it
_PROVENANCE = {
    "accession": "0000320193-23-000106",
    "period": "2023-09-30 (FY)",
    "rule_ids": ["MD-10", "MET-02", "APP-07"],
    "items": {
        "net_income": [
            {"basis": "ttm", "concept": "us-gaap_NetIncomeLoss", "how": "concept", "flags": []}
        ],
        "equity": [
            {
                "basis": "prior_year",
                "concept": "us-gaap_StockholdersEquity",
                "how": "concept",
                "flags": [],
                "source": "filing",
            }
        ],
    },
    "flags": [],
    "empty_reason": None,
}


def _seed(memory_db: Database, aapl_10k: Statements) -> int:
    memory_db.execute(
        "INSERT INTO assets (id, ticker, cik, company_name) VALUES (1, 'AAPL', '0000320193', 'Apple')"
    )
    period = aapl_10k.latest_fy()
    assert period is not None
    filing_id = seed_filing(
        memory_db, 1, period_end=period.date, filing_date="2023-11-03", form="10-K"
    )
    memory_db.execute(
        "UPDATE sec_filings SET accession_number = '0000320193-23-000106' WHERE id = ?",
        (filing_id,),
    )
    db.append_financial_facts(memory_db, filing_id, iter_facts(aapl_10k))
    return filing_id


def test_card_prints_the_stored_row_not_a_recomputation(
    memory_db: Database, aapl_10k: Statements
) -> None:
    """G3: the card reads the stored rows. The stored value (9.99) is not what the code would
    compute for Apple's ROE, and it is what the card prints."""
    filing_id = _seed(memory_db, aapl_10k)
    db.record_metrics(
        memory_db,
        filing_id,
        [
            (
                "profitability",
                MetricResult("return_on_equity", 9.99, "ratio", {"provenance": _PROVENANCE}),
            ),
            # a row recorded without a provenance block
            ("profitability", MetricResult("net_margin", 0.5, "ratio", {})),
        ],
    )
    card = build_card(memory_db, "0000320193", "2023-12-31")
    assert card.ticker == "AAPL"
    assert card.filing["form"] == "10-K"
    assert card.filing["accession"] == "0000320193-23-000106"
    assert card.is_reit is False
    assert "metrics-v7" in card.metrics_version
    roe = next(m for m in card.metrics if m["name"] == "return_on_equity")
    assert roe["value"] == 9.99
    assert "MD-10" in roe["rule_ids"]
    assert roe["provenance"]["items"]["net_income"][0]["basis"] == "ttm"
    text = render(card)
    assert "Company card — AAPL" in text
    assert "9.990000" in text
    # the line items come from the stored provenance, with their basis
    equity = next(item for item in card.line_items if item["item"] == "equity")
    assert equity["basis"] == "prior_year"
    assert equity["source"] == "filing"
    # a row without a provenance block says so
    assert "provenance: not stored (recompute needed)" in text


def test_card_uses_the_latest_filing_available_on_or_before_the_date(
    memory_db: Database, aapl_10k: Statements
) -> None:
    _seed(memory_db, aapl_10k)
    # a later filing the as-of date excludes
    later = seed_filing(
        memory_db, 1, period_end="2024-09-28", filing_date="2024-11-01", form="10-K"
    )
    memory_db.execute("UPDATE sec_filings SET accession_number = 'later' WHERE id = ?", (later,))
    card = build_card(memory_db, "0000320193", "2024-06-30")
    assert card.filing["accession"] == "0000320193-23-000106"


def test_card_refuses_an_unknown_cik(memory_db: Database) -> None:
    with pytest.raises(SystemExit):
        build_card(memory_db, "9999999999", "2024-12-31")


def test_dq01_tolerance_infers_the_unit() -> None:
    # an exact multiple of a million gets half a million (the relative term is smaller)
    assert dq01_tolerance(78_000_000.0) == 500_000.0
    # a value that is not a whole million: the unit is a thousand, but 0.5% of 1,234,000 wins
    assert dq01_tolerance(1_234_000.0) == 0.005 * 1_234_000.0
    # a small value: the rounding floor (0.5 x USD 1) wins
    assert dq01_tolerance(50.0) == 0.5
    # the relative term wins for a large non-round value
    assert dq01_tolerance(1_000_000_001.0) == 0.005 * 1_000_000_001.0


def test_fidelity_compares_a_stored_item_with_the_sec_value(
    memory_db: Database, aapl_10k: Statements, tmp_path: Path
) -> None:
    _seed(memory_db, aapl_10k)
    # a synthetic companyfacts document carrying Apple's revenue for the accession and period
    doc = {
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {
                        "USD": [
                            {
                                "accn": "0000320193-23-000106",
                                "start": "2022-09-25",
                                "end": "2023-09-30",
                                "val": 383_285_000_000.0,
                            }
                        ]
                    }
                }
            }
        }
    }
    (tmp_path / "CIK0000320193.json").write_text(json.dumps(doc))
    report, mismatches = run_fidelity(memory_db, str(tmp_path))
    assert "filings compared: 1" in report
    assert mismatches == []
    assert "revenue" in report
    # a contradictory SEC value is a mismatch
    doc["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"][
        0
    ]["val"] = 999_000_000_000.0
    (tmp_path / "CIK0000320193.json").write_text(json.dumps(doc))
    _report, mismatches = run_fidelity(memory_db, str(tmp_path))
    assert [m["item"] for m in mismatches] == ["revenue"]
    assert mismatches[0]["ticker"] == "AAPL"
