"""``scripts/verify_t148a.py``: the replay's sign hint, the N13 population and the cost table (T-148 part 1)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from conftest import seed_filing

from fundamental_agent import db
from fundamental_agent.metrics import leverage
from fundamental_agent.statements import Statements
from kg_schema import connect

_SPEC = importlib.util.spec_from_file_location(
    "verify_t148a", Path(__file__).resolve().parent.parent / "scripts" / "verify_t148a.py"
)
assert _SPEC and _SPEC.loader
verify = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_t148a"] = verify
_SPEC.loader.exec_module(verify)

FIXTURES = Path(__file__).parent / "fixtures"


def test_the_sec_sign_hint_is_the_ratio_of_the_filed_value_to_the_displayed_one() -> None:
    assert verify._sign_of([147.2], 147.2) == 1.0
    assert verify._sign_of([147.2], -147.2) == -1.0  # displayed negated, filed positive
    assert (
        verify._sign_of([5.0, 147.2], -147.2) == -1.0
    )  # a 10-Q has more than one fact per end date
    assert verify._sign_of([5.0], 147.2) is None  # no filed value matches: no hint, no guess
    assert verify._sign_of(None, 147.2) is None
    assert verify._sign_of([0.0], 0.0) is None  # a zero has no sign


def test_the_old_net_income_concept_is_the_first_document_order_match() -> None:
    """The population of N13: the line T-096's registry order reached before T-148."""
    stmts = Statements.from_payload(
        json.loads(
            (FIXTURES / "financials_WAT_10-Q_2023_acc-0001193125-23-271909.json").read_text()
        )["data"]
    )
    key = stmts.quarter_periods()[-1].key
    assert verify._old_net_income_concept(stmts, key) == verify.N13_CONCEPT
    duke = Statements.from_payload(
        json.loads(
            (FIXTURES / "financials_DUK_10-K_2025_acc-0001326160-25-000072.json").read_text()
        )["data"]
    )
    # Duke files ProfitLoss first, in document order: the old resolver read the consolidated line
    assert verify._old_net_income_concept(duke, duke.fy_periods()[-1].key) == "us-gaap_ProfitLoss"


def test_the_cost_table_counts_a_filing_whose_debt_is_empty(
    aapl_10k: Statements, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = tmp_path / "cost.db"
    conn = connect(path)
    db.ensure_schema(conn)
    conn.execute("INSERT INTO sectors (id, name) VALUES (1, 'Information Technology')")
    conn.execute(
        "INSERT INTO assets (id, ticker, cik, sector_id) VALUES (1, 'AAPL', '0000320193', 1)"
    )
    period = aapl_10k.latest_fy()
    assert period is not None
    filing_id = seed_filing(conn, 1, period_end=period.date, filing_date="2023-11-03")
    # the same filing without its long-term debt line: only the short-term ones remain
    payload = json.loads((FIXTURES / "financials_AAPL_10-K_2023.json").read_text())["data"]
    payload["balance_sheet"] = [
        r for r in payload["balance_sheet"] if r["concept"] != "us-gaap_LongTermDebtNoncurrent"
    ]
    stmts = Statements.from_payload(payload)
    db.record_metrics(
        conn, filing_id, [("leverage", r) for r in leverage.compute(stmts, period.key)]
    )
    conn.commit()
    conn.close()

    verify.cmd_cost(argparse.Namespace(db=str(path)))
    out = capsys.readouterr().out
    assert "Information Technology" in out
    assert "short_term_debt_only" in out
    verify.cmd_flags(argparse.Namespace(db=str(path)))
    assert "flag_total_debt_short_term_debt_only" in capsys.readouterr().out


def _statements_of(fixture: str) -> tuple[Statements, str]:
    stmts = Statements.from_payload(json.loads((FIXTURES / fixture).read_text())["data"])
    periods = stmts.quarter_periods() if "10-Q" in fixture else stmts.fy_periods()
    return stmts, periods[-1].key


def test_the_cash_category_names_what_the_balance_sheet_files_instead() -> None:
    """The review of PR #136: why a filing's cash is empty. A real balance sheet with a cash line is
    not in the population; strip the line and the category follows what is left."""
    stmts, key = _statements_of("financials_AAPL_10-K_2023.json")
    cash = "us-gaap_CashAndCashEquivalentsAtCarryingValue"
    assert stmts.get("cash", key) is not None
    assert verify._cash_category(stmts, key).startswith(
        "d."
    )  # the resolver reads it: would be a bug

    for concept, expected in (
        ("us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "a."),
        ("us-gaap_Cash", "b."),
        ("us-gaap_Deposits", "c."),
    ):
        payload = json.loads((FIXTURES / "financials_AAPL_10-K_2023.json").read_text())["data"]
        for row in payload["balance_sheet"]:
            if row["concept"] == cash:
                row["concept"] = concept if concept != "us-gaap_Deposits" else "us-gaap_Cash"
        if concept == "us-gaap_Deposits":
            payload["balance_sheet"].append(
                {**payload["balance_sheet"][0], "concept": concept, "abstract": False}
            )
        replaced = Statements.from_payload(payload)
        assert verify._cash_category(replaced, key).startswith(expected)

    payload = json.loads((FIXTURES / "financials_AAPL_10-K_2023.json").read_text())["data"]
    payload["balance_sheet"] = [r for r in payload["balance_sheet"] if r["concept"] != cash]
    assert verify._cash_category(Statements.from_payload(payload), key).startswith("e.")


def test_the_net_income_step_follows_the_rule_that_resolved_it() -> None:
    steps = {
        "financials_HPE_10-K_2024_acc-0001645590-24-000139.json": "i.",
        "financials_ALLE_10-Q_2025_acc-0001579241-25-000030.json": "ii.",
        "financials_PNC_10-K_2024_acc-0000713676-24-000028.json": "iii.",
        "financials_APA_10-K_2022_acc-0001784031-22-000009.json": "iv.",
    }
    for fixture, step in steps.items():
        stmts, key = _statements_of(fixture)
        if step == "iv.":
            key = "2021-12-31 (FY)"
        assert verify._ni_step(stmts.resolve("net_income", key)).startswith(step), fixture
        # the first version of part 1 read the income statement alone: all four were empty
        assert verify._part1_net_income(stmts, key) is None
