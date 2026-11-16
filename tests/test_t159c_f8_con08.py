"""T-159c §2b (F8): CON_08 reads the discontinued operating leg filed under
``NetCashProvidedByUsedInDiscontinuedOperations`` when its caption names operating activities."""

from __future__ import annotations

from conftest import seed_filing
from portfolio_common.db import Database

from fundamental_agent import quality


def _facts(
    *,
    label: str,
    discontinued: float,
    flow_column: str = "2022-06-30 (Q2)",
) -> quality.FilingFacts:
    by_key = {
        ("cash_flow", quality._OCF): {flow_column: 4_360_000_000.0},
        ("cash_flow", quality._OCF_CONT): {flow_column: 8_384_000_000.0},
        ("cash_flow", quality._OCF_DISC_ALT): {flow_column: discontinued},
    }
    return quality.FilingFacts(
        form="10-Q",
        period_end="2022-06-30",
        by_key=by_key,
        instant_column="2022-06-30",
        flow_column=flow_column,
        ytd_column=None,
        labels={("cash_flow", quality._OCF_DISC_ALT, flow_column): label},
    )


def _con08(facts: quality.FilingFacts) -> list[quality.Issue]:
    return [issue for issue in quality.identity_issues(facts, {}) if issue.rule_id == "CON_08"]


def test_the_operating_block_leg_makes_the_identity_hold() -> None:
    """MPC: 8,384 - 4,024 = 4,360; the caption places the row in the operating block."""
    facts = _facts(
        label="operating activities - discontinued operations", discontinued=-4_024_000_000.0
    )
    assert facts.discontinued_operating(facts.flow_column) == -4_024_000_000.0
    assert _con08(facts) == []


def test_a_total_discontinued_line_is_not_the_operating_leg() -> None:
    facts = _facts(
        label="net cash provided by discontinued operations", discontinued=4_024_000_000.0
    )
    assert facts.discontinued_operating(facts.flow_column) is None
    assert _con08(facts)  # the identity does not hold: the total includes investing and financing


def test_a_caption_naming_investing_is_not_the_operating_leg() -> None:
    facts = _facts(
        label="investing activities - discontinued operations", discontinued=-4_024_000_000.0
    )
    assert facts.discontinued_operating(facts.flow_column) is None
    assert _con08(facts)


def test_load_facts_reads_the_caption_and_the_leg(memory_db: Database) -> None:
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    filing = seed_filing(
        memory_db,
        1,
        period_end="2022-06-30",
        filing_date="2022-07-28",
        form="10-Q",
        fiscal_period="2022Q2",
    )
    column = "2022-06-30 (Q2)"
    rows = [
        (filing, "cash_flow", quality._OCF, column, 4_360_000_000.0, 1.0, "Operating cash flow"),
        (
            filing,
            "cash_flow",
            quality._OCF_CONT,
            column,
            8_384_000_000.0,
            1.0,
            "Continuing operations",
        ),
        (
            filing,
            "cash_flow",
            quality._OCF_DISC_ALT,
            column,
            -4_024_000_000.0,
            1.0,
            "Operating activities - discontinued operations",
        ),
    ]
    memory_db.executemany(
        "INSERT INTO financial_facts "
        "(filing_id, statement, concept, period_key, value, preferred_sign, label, "
        "filing_version, event_time) VALUES (?, ?, ?, ?, ?, ?, ?, 'acc', ?)",
        [(*row, "2022-06-30") for row in rows],
    )
    memory_db.commit()
    facts = quality.load_facts(memory_db, [filing])[filing]
    assert facts.discontinued_operating(column) == -4_024_000_000.0
    assert _con08(facts) == []
