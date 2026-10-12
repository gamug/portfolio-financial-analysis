"""T-148 part 1 (a): the filed-sign hint in storage -- ``financial_facts.preferred_sign`` (migration
m013, contract version 13), written by the pipeline's fact append and read back by the audit's replay.

The stored ``value`` stays the displayed one, as it always was; the hint beside it is what turns it
into the filed value (ID-12). There is no backfill: a row written before T-148 keeps NULL and reads
as displayed, flagged (``test_line_item_resolution.py``).
"""

from __future__ import annotations

from conftest import seed_filing
from portfolio_common.db import Database
from sec_xcheck.common import filings, own_period_key, statements_from_db

import kg_schema
from fundamental_agent import db
from fundamental_agent.statements import Statements, iter_facts
from kg_schema import migrations, queries


def _asset_and_filing(memory_db: Database, aapl_10k: Statements) -> int:
    memory_db.execute("INSERT INTO assets (id, ticker, cik) VALUES (1, 'AAPL', '0000320193')")
    period = aapl_10k.latest_fy()
    assert period is not None
    return seed_filing(memory_db, 1, period_end=period.date, filing_date="2023-11-03")


def test_the_fact_append_stores_the_displayed_value_and_the_sign_hint(
    memory_db: Database, aapl_10k: Statements
) -> None:
    filing_id = _asset_and_filing(memory_db, aapl_10k)
    db.append_financial_facts(memory_db, filing_id, iter_facts(aapl_10k))
    row = memory_db.execute(
        "SELECT value, preferred_sign FROM financial_facts WHERE filing_id = ? AND concept = ? "
        "AND period_key = '2023-09-30 (FY)'",
        (filing_id, "us-gaap_PaymentsToAcquirePropertyPlantAndEquipment"),
    ).fetchone()
    assert (row["value"], row["preferred_sign"]) == (-10_959_000_000.0, -1.0)


def test_a_fact_without_a_hint_is_stored_with_null(memory_db: Database) -> None:
    memory_db.execute("INSERT INTO assets (id, ticker, cik) VALUES (1, 'X', '0000000001')")
    filing_id = seed_filing(memory_db, 1, period_end="2023-12-31", filing_date="2024-02-01")
    db.append_financial_facts(
        memory_db,
        filing_id,
        [
            {
                "statement": "income_statement",
                "concept": "us-gaap_Revenues",
                "period_key": "2023-12-31 (FY)",
                "value": 5.0,
            }
        ],
    )
    assert memory_db.execute("SELECT preferred_sign FROM financial_facts").fetchone()[0] is None


def test_m013_adds_the_column_and_leaves_every_existing_row_null(memory_db: Database) -> None:
    kg_schema.ensure(memory_db, run_migrations=True)
    memory_db.execute("INSERT INTO assets (id, ticker, cik) VALUES (1, 'X', '0000000001')")
    filing_id = seed_filing(memory_db, 1, period_end="2023-12-31", filing_date="2024-02-01")
    # a database as it was before T-148: no column, a stored fact
    memory_db.execute("ALTER TABLE financial_facts DROP COLUMN preferred_sign")
    memory_db.execute(
        "INSERT INTO financial_facts (filing_id, statement, concept, period_key, value, "
        "filing_version, event_time) VALUES (?, 'cash_flow', 'us-gaap_X', '2023-12-31 (FY)', -3.0, "
        "'v', '2023-12-31')",
        (filing_id,),
    )
    memory_db.execute("DELETE FROM schema_version WHERE version >= 13")
    memory_db.commit()
    assert "preferred_sign" not in memory_db.table_columns("financial_facts")

    assert migrations.apply_migrations(memory_db) == [13]
    assert queries.current_version(memory_db) == 13
    assert "preferred_sign" in memory_db.table_columns("financial_facts")
    assert memory_db.execute("SELECT preferred_sign FROM financial_facts").fetchone()[0] is None
    assert migrations.apply_migrations(memory_db) == []  # idempotent


def test_the_audit_replay_reads_the_stored_sign_back(
    memory_db: Database, aapl_10k: Statements
) -> None:
    """A filing's resolver output rebuilt from its stored facts equals the payload's, capex and
    tax included -- the replay the audit's level (b) measurements run."""
    filing_id = _asset_and_filing(memory_db, aapl_10k)
    db.append_financial_facts(memory_db, filing_id, iter_facts(aapl_10k))
    (f,) = filings(memory_db)
    stored = statements_from_db(memory_db, filing_id)
    key = own_period_key(stored, f["form"], f["period_end"])
    assert key is not None
    for item in ("capital_expenditure", "income_tax", "cogs", "net_income", "equity"):
        assert stored.resolve(item, key) == aapl_10k.resolve(item, key), item
    assert stored.get("capital_expenditure", key) == 10_959_000_000.0


def test_a_fact_stored_before_the_column_replays_as_displayed_and_flagged(
    memory_db: Database, aapl_10k: Statements
) -> None:
    filing_id = _asset_and_filing(memory_db, aapl_10k)
    db.append_financial_facts(memory_db, filing_id, iter_facts(aapl_10k))
    memory_db.execute("UPDATE financial_facts SET preferred_sign = NULL")
    (f,) = filings(memory_db)
    stored = statements_from_db(memory_db, filing_id)
    key = own_period_key(stored, f["form"], f["period_end"])
    assert key is not None
    tax = stored.resolve("income_tax", key)
    assert tax.value == 16_741_000_000.0 and tax.flags == ("sign_unverified",)
