"""The company card and the fidelity gate (T-151 part 3, D15/D16)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from conftest import seed_filing
from portfolio_common.db import Database

from fundamental_agent import db, filing_text
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
    assert "metrics-v11" in card.metrics_version
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


def test_card_prints_the_value_of_every_line_item(
    memory_db: Database, aapl_10k: Statements
) -> None:
    """V5: the card shows each line item's **value** (period / prior year / TTM as the metric
    recorded it), so a person can check the number against the filing."""
    filing_id = _seed(memory_db, aapl_10k)
    provenance = {
        **_PROVENANCE,
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
            "revenue": [
                {"basis": "period", "concept": "us-gaap_Revenues", "how": "concept", "flags": []}
            ],
        },
    }
    db.record_metrics(
        memory_db,
        filing_id,
        [
            (
                "profitability",
                MetricResult(
                    "return_on_equity",
                    9.99,
                    "ratio",
                    {
                        "net_income_ttm": 81_000_000.0,
                        "prior_equity": 62_000_000.0,
                        "revenue": 383_285_000_000.0,
                        "provenance": provenance,
                    },
                ),
            )
        ],
    )
    card = build_card(memory_db, "0000320193", "2023-12-31")
    values = {item["item"]: item["value"] for item in card.line_items}
    assert values["net_income"] == 81_000_000.0
    assert values["equity"] == 62_000_000.0
    assert values["revenue"] == 383_285_000_000.0
    text = render(card)
    assert "81,000,000.00" in text
    assert "383,285,000,000.00" in text


def test_card_prints_the_accession_and_the_edgar_link(
    memory_db: Database, aapl_10k: Statements, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V5: every value carries the accession it was read from and the link to the filing's
    document."""
    filing_id = _seed(memory_db, aapl_10k)
    db.record_metrics(
        memory_db,
        filing_id,
        [
            (
                "profitability",
                MetricResult(
                    "return_on_equity",
                    9.99,
                    "ratio",
                    {"provenance": _PROVENANCE},
                ),
            )
        ],
    )
    # the filing's folder is deterministic and needs no network
    card = build_card(memory_db, "0000320193", "2023-12-31")
    assert card.filing["edgar_folder"] == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106"
    )
    equity = next(item for item in card.line_items if item["item"] == "equity")
    assert equity["accession"] == "0000320193-23-000106"
    assert equity["edgar_url"] == card.filing["edgar_folder"]

    # with the EDGAR helper the card names the primary document
    monkeypatch.setattr(
        filing_text,
        "primary_document_url",
        lambda cik, accession, *, form=None, client=None: (
            "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20230930.htm"
        ),
    )
    card = build_card(memory_db, "0000320193", "2023-12-31", with_edgar=True)
    assert card.filing["edgar_document_url"].endswith("aapl-20230930.htm")
    equity = next(item for item in card.line_items if item["item"] == "equity")
    assert equity["edgar_url"].endswith("aapl-20230930.htm")
    assert "aapl-20230930.htm" in render(card)


def test_card_prints_the_vetoes_that_apply_on_the_date(
    memory_db: Database, aapl_10k: Statements
) -> None:
    """V5: the card shows the vetoes that apply on its date, evaluated as the cycle's veto step
    evaluates them (the cycle's readers, the cycle's own rules)."""
    filing_id = _seed(memory_db, aapl_10k)
    db.record_metrics(
        memory_db,
        filing_id,
        [
            (
                "leverage",
                MetricResult(
                    "debt_to_equity",
                    5.0,
                    "ratio",
                    {"provenance": {"accession": "0000320193-23-000106", "items": {}}},
                ),
            )
        ],
    )
    card = build_card(memory_db, "0000320193", "2023-12-31")
    vetoes = {v["rule_id"]: v for v in card.vetoes}
    assert "LEVERAGE_EXTREME" in vetoes
    assert vetoes["LEVERAGE_EXTREME"]["severity"] == "HARD"
    assert vetoes["LEVERAGE_EXTREME"]["evidence"]["value"] == 5.0
    text = render(card)
    assert "Vetoes on 2023-12-31" in text
    assert "LEVERAGE_EXTREME" in text
    # the vetoes can be left out
    card = build_card(memory_db, "0000320193", "2023-12-31", with_vetoes=False)
    assert card.vetoes == []


def test_dq01_tolerance_infers_the_unit() -> None:
    # an exact multiple of a million gets half a million (the relative term is smaller)
    assert dq01_tolerance(78_000_000.0) == 500_000.0
    # a value that is not a whole million: the unit is a thousand, but 0.5% of 1,234,000 wins
    assert dq01_tolerance(1_234_000.0) == 0.005 * 1_234_000.0
    # a small value: the rounding floor (0.5 x USD 1) wins
    assert dq01_tolerance(50.0) == 0.5
    # G5: L-10's n_terms -- a two-part sum against the filer's total has 3 terms, so a 1M gap on a
    # 78M sum is within the rounding term (ROK 0001024478-26-000005 is not a mismatch)
    assert dq01_tolerance(78_000_000.0, 3) == 1_500_000.0
    assert abs(79_000_000.0 - 78_000_000.0) <= dq01_tolerance(78_000_000.0, 3)
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


def test_h5_a_balance_sheet_item_is_compared_once_on_a_10q(
    memory_db: Database, tmp_path: Path
) -> None:
    """H5: an instant item has no year-to-date column, so the ytd kind must not compare it a second
    time."""
    memory_db.execute(
        "INSERT INTO assets (id, ticker, cik, company_name) VALUES (1, 'X', '0000000001', 'X')"
    )
    filing_id = seed_filing(
        memory_db,
        1,
        period_end="2024-06-30",
        filing_date="2024-08-01",
        form="10-Q",
        fiscal_period="2024Q2",
    )
    memory_db.execute(
        "UPDATE sec_filings SET accession_number = '0000000001-24-000001' WHERE id = ?",
        (filing_id,),
    )
    db.append_financial_facts(
        memory_db,
        filing_id,
        [
            {
                "statement": "balance_sheet",
                "concept": "us-gaap_Assets",
                "period_key": "2024-06-30",
                "value": 1_000.0,
            },
            {
                "statement": "income_statement",
                "concept": "us-gaap_Revenues",
                "period_key": "2024-06-30 (Q2)",
                "value": 100.0,
            },
            {
                "statement": "income_statement",
                "concept": "us-gaap_Revenues",
                "period_key": "2024-06-30 (YTD)",
                "value": 200.0,
            },
        ],
    )
    doc = {
        "facts": {
            "us-gaap": {
                "Assets": {
                    "units": {
                        "USD": [
                            {"accn": "0000000001-24-000001", "end": "2024-06-30", "val": 1_000.0}
                        ]
                    }
                },
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "accn": "0000000001-24-000001",
                                "start": "2024-04-01",
                                "end": "2024-06-30",
                                "val": 100.0,
                            },
                            {
                                "accn": "0000000001-24-000001",
                                "start": "2024-01-01",
                                "end": "2024-06-30",
                                "val": 200.0,
                            },
                        ]
                    }
                },
            }
        }
    }
    (tmp_path / "CIK0000000001.json").write_text(json.dumps(doc))
    report, mismatches = run_fidelity(memory_db, str(tmp_path))
    # the balance-sheet item is compared on the own column only
    assert re.search(r"^total_assets\s+own\b", report, re.MULTILINE)
    assert not re.search(r"^total_assets\s+ytd\b", report, re.MULTILINE)
    # the duration item is compared on both
    assert re.search(r"^revenue\s+own\b", report, re.MULTILINE)
    assert re.search(r"^revenue\s+ytd\b", report, re.MULTILINE)
    assert mismatches == []


def test_card_sums_a_mapped_item_concepts(memory_db: Database, aapl_10k: Statements) -> None:
    """V5 (reviewer B's check): a line item resolved as a **sum** of concepts (``[map]``) prints the
    sum of the filed values, not a dash."""
    filing_id = _seed(memory_db, aapl_10k)
    db.append_financial_facts(
        memory_db,
        filing_id,
        [
            {
                "statement": "balance_sheet",
                "concept": "us-gaap_LinesOfCreditCurrent",
                "period_key": "2023-09-30",
                "value": 800.0,
            },
            {
                "statement": "balance_sheet",
                "concept": "us-gaap_ShortTermBankLoansAndNotesPayable",
                "period_key": "2023-09-30",
                "value": 200.0,
            },
        ],
    )
    db.record_metrics(
        memory_db,
        filing_id,
        [
            (
                "leverage",
                MetricResult(
                    "debt_to_equity",
                    1.0,
                    "ratio",
                    {
                        "provenance": {
                            "accession": "0000320193-23-000106",
                            "period": "2023-09-30 (FY)",
                            "rule_ids": ["MD-15"],
                            "items": {
                                "short_term_debt": [
                                    {
                                        "basis": "period",
                                        "concept": "us-gaap_LinesOfCreditCurrent + "
                                        "us-gaap_ShortTermBankLoansAndNotesPayable",
                                        "how": "map",
                                        "flags": [],
                                    }
                                ]
                            },
                            "flags": [],
                            "empty_reason": None,
                        }
                    },
                ),
            )
        ],
    )
    card = build_card(memory_db, "0000320193", "2023-12-31")
    row = next(item for item in card.line_items if item["item"] == "short_term_debt")
    assert row["value"] == 1000.0  # the sum of the two filed values
    assert row["value_source"] == "the filing's own stored facts, summed"
    assert "1,000.00" in render(card)


def test_card_prints_the_cover_classes_and_the_count_the_cap_used(
    memory_db: Database, aapl_10k: Statements
) -> None:
    """V5 (reviewer B's check): the card prints each cover-page class with its date, the count the
    market cap rested on, and the guard's verdict when the filing's own cover count is not the one
    the cap rests on."""
    filing_id = _seed(memory_db, aapl_10k)
    memory_db.execute(
        "INSERT INTO filing_cover_shares (filing_id, class_member, value, as_of_date, event_time, "
        "ingested_at) VALUES (?, '', 1000.0, '2023-11-03', '2023-09-30', 'now')",
        (filing_id,),
    )
    db.record_metrics(
        memory_db,
        filing_id,
        [
            (
                "valuation",
                MetricResult(
                    "market_capitalization",
                    1000.0,
                    "usd",
                    {
                        "shares": 900.0,
                        "share_age_days": 61,
                        "provenance": {
                            "accession": "0000320193-23-000106",
                            "period": "2023-09-30 (FY)",
                            "rule_ids": ["MD-34"],
                            "items": {},
                            "flags": [],
                            "empty_reason": None,
                        },
                    },
                ),
            )
        ],
    )
    card = build_card(memory_db, "0000320193", "2023-12-31")
    assert card.cover_shares[0]["class_member"] == ""
    assert card.cover_shares[0]["value"] == 1000.0
    assert card.market_cap_count["shares"] == 900.0
    assert card.market_cap_count["share_age_days"] == 61
    assert "was not used" in card.market_cap_count["verdict"]
    text = render(card)
    assert "Cover-page shares" in text
    assert "the count the cap used" in text
    assert "the guard's verdict" in text
