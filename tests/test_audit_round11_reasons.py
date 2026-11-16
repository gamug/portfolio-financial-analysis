"""Audit round #11, Part 2 (the user and reviewer A, 2026-10-29): every empty value records its reason. The two rule
inputs the fundamental engine writes beside the metrics -- ``value_inputs`` (its value is the trailing-year net income)
and ``utility_cash_coverage_3y`` (D-13's three-year bound) -- carried none: 366 and 788 empty rows of the database.
They now carry ``provenance.empty_reason`` with G4's codes, and a trailing year annualized x4 (not formed, H2) is
``prior_year_missing``, never ``missing_input``.
"""

from __future__ import annotations

from typing import Any

from fundamental_agent import utility_coverage, value_inputs
from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.statements import Statements
from kg_schema.versions import VALUE_INPUTS_MISSING, VALUE_INPUTS_NET_INCOME


def _value_inputs(roa_inputs: dict[str, Any], value: float | None) -> dict[str, Any]:
    computed = [("profitability", MetricResult("return_on_assets", value, "ratio", roa_inputs))]
    return value_inputs.build(computed).inputs


def test_an_empty_value_inputs_row_records_the_net_income_reason() -> None:
    got = _value_inputs({"provenance": {"empty_reason": "capture_failure"}}, None)
    assert got["provenance"] == {"empty_reason": "capture_failure"}


def test_a_trailing_year_built_x4_is_prior_year_missing() -> None:
    got = _value_inputs({"net_income_ttm": 400.0, "annualized_x4": 1.0, "provenance": {}}, 1.0)
    assert got[VALUE_INPUTS_NET_INCOME] is None
    assert got[VALUE_INPUTS_MISSING][VALUE_INPUTS_NET_INCOME] == "prior_year_missing"
    assert got["provenance"] == {"empty_reason": "prior_year_missing"}


def test_a_filled_value_inputs_row_carries_no_reason() -> None:
    got = _value_inputs({"net_income_ttm": 400.0, "provenance": {}}, 1.0)
    assert got[VALUE_INPUTS_NET_INCOME] == 400.0 and "provenance" not in got


class _Rows:
    """A minimal connection: the cash-flow years, no working-capital lines, and the previous 10-K query."""

    def __init__(self, keys: list[str], previous_10k: bool) -> None:
        self.keys, self.previous_10k = keys, previous_10k

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        rows: list[dict[str, Any]] = []
        if "FROM sec_filings WHERE id" in sql:
            rows = [{"period_end": "2022-12-31"}]
        elif "DISTINCT period_key" in sql:
            rows = [{"period_key": k} for k in self.keys]
        elif "SELECT 1 FROM sec_filings" in sql or "SELECT id FROM sec_filings" in sql:
            rows = [{"id": 1}] if self.previous_10k else []
        return _Cursor(rows)


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None

    def __iter__(self) -> Any:
        return iter(self.rows)


def _stmts(cfo_years: dict[str, float], debt_years: dict[str, float]) -> Statements:
    def row(concept: str, values: dict[str, float]) -> dict[str, Any]:
        return {
            "concept": "us-gaap_" + concept,
            "label": concept,
            "abstract": False,
            "dimension": False,
            "preferred_sign": 1.0,
            **values,
        }

    return Statements.from_payload(
        {
            "income_statement": [],
            "balance_sheet": [
                row("LongTermDebt", debt_years),
                row("Assets", dict.fromkeys(debt_years, 1e4)),
            ],
            "cash_flow": [row("NetCashProvidedByUsedInOperatingActivities", cfo_years)],
            "corrections": [],
            "filed_facts": [],
        }
    )


YEARS = ["2020-12-31 (FY)", "2021-12-31 (FY)", "2022-12-31 (FY)"]


def test_a_utility_bound_missing_the_oldest_years_debt_is_prior_year_missing() -> None:
    """The oldest year's debt is on neither this 10-K nor a stored previous one (before the window)."""
    stmts = _stmts(dict.fromkeys(YEARS, 900.0), {"2021-12-31": 3_000.0, "2022-12-31": 3_100.0})
    res = utility_coverage.compute(_Rows(YEARS, previous_10k=False), 1, 1, stmts)  # type: ignore[arg-type]
    assert res.value is None and res.inputs["provenance"] == {"empty_reason": "prior_year_missing"}


def test_a_utility_bound_with_fewer_than_three_years_is_not_reported() -> None:
    stmts = _stmts(dict.fromkeys(YEARS[1:], 900.0), {"2021-12-31": 3_000.0, "2022-12-31": 3_100.0})
    res = utility_coverage.compute(_Rows(YEARS[1:], previous_10k=False), 1, 1, stmts)  # type: ignore[arg-type]
    assert res.value is None and res.inputs["provenance"] == {"empty_reason": "not_reported"}
