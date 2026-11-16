"""The a3_vs_a2 comparer: the classes and the item-level SEC lookup (T-143 part A)."""

from __future__ import annotations

import csv
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pilot_a" / "a3_vs_a2.py"
_SPEC = importlib.util.spec_from_file_location("a3_vs_a2", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
a3_vs_a2 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(a3_vs_a2)


def _db(path: Path, *, extra: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT, cik TEXT);
        CREATE TABLE sec_filings (
            id INTEGER PRIMARY KEY, asset_id INTEGER, form TEXT, period_end TEXT,
            accession_number TEXT, cik TEXT
        );
        CREATE TABLE fundamental_metrics (
            id INTEGER PRIMARY KEY, filing_id INTEGER, metric_group TEXT, metric_name TEXT,
            value REAL, inputs_json TEXT
        );
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY, filing_id INTEGER, period_key TEXT, concept TEXT, value REAL
        );
        """
    )
    conn.execute("INSERT INTO assets VALUES (1, 'AAA', '0000000001')")
    conn.execute("INSERT INTO assets VALUES (2, 'BBB', '0000000002')")
    conn.execute("INSERT INTO assets VALUES (3, 'CCC', '0000000003')")
    conn.execute("INSERT INTO sec_filings VALUES (1, 1, '10-K', '2024-12-31', 'A1', '0000000001')")
    if extra == "A2":
        conn.execute(
            "INSERT INTO sec_filings VALUES (2, 2, '10-K', '2024-12-31', 'A2', '0000000002')"
        )
    if extra == "A3":
        conn.execute(
            "INSERT INTO sec_filings VALUES (3, 3, '10-K', '2024-12-31', 'A3', '0000000003')"
        )
    return conn


def _metric(  # noqa: PLR0913, PLR0917 - the fixture names every field explicitly
    conn: sqlite3.Connection,
    filing_id: int,
    group: str,
    name: str,
    value: float | None,
    items: dict[str, list[dict[str, object]]] | None = None,
    reason: str | None = None,
    item_values: dict[str, float] | None = None,
) -> None:
    inputs: dict[str, object] = {
        "provenance": {
            "period": "2024-12-31 (FY)",
            "items": items or {},
            "empty_reason": reason,
        }
    }
    inputs.update(item_values or {})
    conn.execute(
        "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, inputs_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (filing_id, group, name, value, json.dumps(inputs)),
    )


def _entry(
    concept: str | None, how: str = "concept", flags: list[str] | None = None
) -> list[dict[str, object]]:
    return [{"basis": "period", "concept": concept, "how": how, "flags": flags or []}]


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    a2 = _db(tmp_path / "a2.db", extra="A2")
    a3 = _db(tmp_path / "a3.db", extra="A3")
    for conn in (a2, a3):
        _metric(conn, 1, "profitability", "same_metric", 0.1)
        _metric(
            conn,
            1,
            "profitability",
            "fill_metric",
            None if conn is a2 else 0.2,
            reason="missing_input",
        )
        _metric(
            conn,
            1,
            "profitability",
            "emptied_metric",
            0.3 if conn is a2 else None,
            reason="capture_failure",
        )
        _metric(conn, 1, "profitability", "changed_metric", 1.0 if conn is a2 else 2.0)
        _metric(
            conn,
            1,
            "profitability",
            "reason_metric",
            None,
            reason="missing_input" if conn is a2 else "not_reported",
        )
    _metric(a2, 2, "profitability", "only_a2_metric", 4.0)
    _metric(a3, 3, "profitability", "only_a3_metric", 5.0)

    _metric(
        a2,
        1,
        "profitability",
        "revenue_metric",
        100.0,
        {"revenue": _entry("us-gaap_Revenues")},
        item_values={"revenue": 100.0},
    )
    _metric(
        a3,
        1,
        "profitability",
        "revenue_metric",
        100.0,
        {"revenue": _entry("us-gaap_Revenues")},
        item_values={"revenue": 100.0},
    )
    _metric(
        a2,
        1,
        "leverage",
        "debt_metric",
        None,
        {"long_term_debt": _entry("us-gaap_LongTermDebtNoncurrent")},
        reason="missing_input",
    )
    _metric(
        a3,
        1,
        "leverage",
        "debt_metric",
        None,
        {"long_term_debt": _entry("us-gaap_LongTermDebtNoncurrent")},
        reason="missing_input",
    )
    _metric(
        a2,
        1,
        "liquidity",
        "cash_metric",
        10.0,
        {"cash": _entry("us-gaap_CashAndCashEquivalentsAtCarryingValue")},
        item_values={"cash": 10.0},
    )
    _metric(
        a3,
        1,
        "liquidity",
        "cash_metric",
        11.0,
        {"cash": _entry("us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")},
        item_values={"cash": 11.0},
    )
    _metric(
        a3,
        1,
        "efficiency",
        "inventory_metric",
        7.0,
        {"inventory": _entry("us-gaap_InventoryNet")},
        item_values={"inventory": 7.0},
    )
    _metric(
        a2,
        1,
        "efficiency",
        "receivables_metric",
        3.0,
        {"receivables": _entry("us-gaap_AccountsReceivableNetCurrent")},
        item_values={"receivables": 3.0},
    )
    a2.execute(
        "INSERT INTO financial_facts (filing_id, period_key, concept, value) VALUES "
        "(1, '2024-12-31', 'us-gaap_LongTermDebtNoncurrent', 40.0)"
    )
    a2.commit()
    a3.execute(
        "INSERT INTO financial_facts (filing_id, period_key, concept, value) VALUES "
        "(1, '2024-12-31', 'us-gaap_LongTermDebtNoncurrent', 50.0)"
    )
    a3.commit()
    a2.close()
    a3.close()

    cache = tmp_path / "companyfacts"
    cache.mkdir()
    (cache / "CIK0000000001.json").write_text(
        json.dumps(
            {
                "facts": {
                    "us-gaap": {
                        "Revenues": {
                            "units": {"USD": [{"accn": "A1", "end": "2024-12-31", "val": 100.0}]}
                        },
                        "LongTermDebtNoncurrent": {
                            "units": {"USD": [{"accn": "A1", "end": "2024-12-31", "val": 50.0}]}
                        },
                        "CashAndCashEquivalentsAtCarryingValue": {
                            "units": {"USD": [{"accn": "A1", "end": "2024-12-31", "val": 10.0}]}
                        },
                        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": {
                            "units": {"USD": [{"accn": "A1", "end": "2024-12-31", "val": 11.0}]}
                        },
                    }
                }
            }
        )
    )
    return tmp_path / "a2.db", tmp_path / "a3.db", cache


def test_classes_and_the_item_row_trace_to_the_sec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a2, a3, cache = _fixture(tmp_path)
    metrics = tmp_path / "metrics.csv"
    items = tmp_path / "items.csv"
    summary = tmp_path / "summary.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "a3_vs_a2",
            "--a2-db",
            str(a2),
            "--a3-db",
            str(a3),
            "--companyfacts-dir",
            str(cache),
            "--out-metrics",
            str(metrics),
            "--out-items",
            str(items),
            "--out-summary",
            str(summary),
        ],
    )
    assert a3_vs_a2.main() == 0

    rows = list(csv.DictReader(metrics.open()))
    classes = {row["metric_name"]: row["class"] for row in rows}
    assert classes["same_metric"] == "same"
    assert classes["fill_metric"] == "fill"
    assert classes["emptied_metric"] == "emptied"
    assert classes["changed_metric"] == "changed"
    assert classes["reason_metric"] == "reason_only"
    assert classes["only_a2_metric"] == "only_a2"
    assert classes["only_a3_metric"] == "only_a3"

    item_rows = {row["item"]: row for row in csv.DictReader(items.open())}
    assert "revenue" not in item_rows
    assert item_rows["long_term_debt"]["value_a2"] == "40.0"
    assert item_rows["long_term_debt"]["value_a3"] == "50.0"
    assert item_rows["long_term_debt"]["sec_value_a3"] == "50.0"
    assert item_rows["cash"]["concept_a2"] == "us-gaap_CashAndCashEquivalentsAtCarryingValue"
    assert item_rows["cash"]["sec_value_a3"] == "11.0"
    assert item_rows["inventory"]["value_a3"] == "7.0"
    assert item_rows["receivables"]["value_a2"] == "3.0"

    text = summary.read_text()
    assert "| `fill` | 1 |" in text
    assert "| `reason_only` | 1 |" in text
    assert "`A2` BBB 10-K 2024-12-31" in text
    assert "`A3` CCC 10-K 2024-12-31" in text
    assert "Item rows whose value, concept or how differs: 4" in text


def _flow_fact(start: str, end: str, value: float) -> dict[str, object]:
    return {"accn": "A1", "start": start, "end": end, "val": value}


def test_a_ttm_only_item_is_not_emptied() -> None:
    a2 = {
        "depreciation_amortization": 506.0,
        "provenance": {
            "period": "2022-03-31 (Q1)",
            "items": {
                "depreciation_amortization": [
                    {"basis": "period", "concept": "us-gaap_Depreciation", "how": "concept"}
                ]
            },
        },
    }
    a3 = {
        "depreciation_amortization": 506.0,
        "provenance": {
            "period": "2022-03-31 (Q1)",
            "items": {
                "depreciation_amortization": [
                    {
                        "basis": "ttm",
                        "concept": "us-gaap_Depreciation",
                        "how": "concept",
                        "value": 2012.0,
                    }
                ]
            },
        },
    }
    first = a3_vs_a2._dedupe(
        a3_vs_a2._item_records({("A1", "g", "ebitda"): {"inputs": a2}})[
            ("A1", "depreciation_amortization")
        ]
    )
    second = a3_vs_a2._dedupe(
        a3_vs_a2._item_records({("A1", "g", "ebitda"): {"inputs": a3}})[
            ("A1", "depreciation_amortization")
        ]
    )
    first_basis, second_basis = a3_vs_a2._choose_bases(first, second)
    assert (first_basis, second_basis) == ("period", "ttm")
    assert first[first_basis]["value"] == 506.0
    assert second[second_basis]["value"] == 2012.0


def test_the_sec_quarter_is_chosen_over_the_ytd(tmp_path: Path) -> None:
    cache = tmp_path / "companyfacts"
    cache.mkdir()
    (cache / "CIK0000000001.json").write_text(
        json.dumps(
            {
                "facts": {
                    "us-gaap": {
                        "Revenues": {
                            "units": {
                                "USD": [
                                    _flow_fact("2022-01-01", "2022-03-31", 100.0),
                                    _flow_fact("2021-10-01", "2022-03-31", 250.0),
                                ]
                            }
                        }
                    }
                }
            }
        )
    )
    assert a3_vs_a2._sec_values(
        str(cache), "1", "A1", "2022-03-31 (Q2)", ["us-gaap_Revenues"], "revenue"
    ) == ["100.0"]
    assert a3_vs_a2._sec_values(
        str(cache), "1", "A1", "2022-03-31 (YTD)", ["us-gaap_Revenues"], "revenue"
    ) == ["250.0"]


def test_sixteen_and_twelve_week_quarters_match(tmp_path: Path) -> None:
    cache = tmp_path / "companyfacts"
    cache.mkdir()
    (cache / "CIK0000000001.json").write_text(
        json.dumps(
            {
                "facts": {
                    "us-gaap": {
                        "Revenues": {
                            "units": {
                                "USD": [
                                    _flow_fact("2021-11-28", "2022-03-20", 112.0),
                                    _flow_fact("2021-12-26", "2022-03-19", 83.0),
                                ]
                            }
                        }
                    }
                }
            }
        )
    )
    assert a3_vs_a2._sec_values(
        str(cache), "1", "A1", "2022-03-20 (Q1)", ["us-gaap_Revenues"], "revenue"
    ) == ["112.0"]
    assert a3_vs_a2._sec_values(
        str(cache), "1", "A1", "2022-03-19 (Q1)", ["us-gaap_Revenues"], "revenue"
    ) == ["83.0"]


def test_the_facts_fallback_takes_the_labelled_column() -> None:
    facts = {
        ("2022-06-11 (Q2)", "us-gaap_CostOfGoodsAndServicesSold"): 9415.0,
        ("2022-06-11", "us-gaap_CostOfGoodsAndServicesSold"): 16848.0,
    }
    record = {
        "value": None,
        "concepts": ["us-gaap_CostOfGoodsAndServicesSold"],
        "period": "2022-06-11 (Q2)",
    }
    assert a3_vs_a2._value_from_facts(facts, record) == 9415.0
