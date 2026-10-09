"""``scripts/sec_xcheck/l09.py``: the register of every derived revenue value, with its guard and its ex-post comparison
(L-09, D-03). Real APA captures for T-140; a hand-built payload for T-117 and for a refusal."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, cast

from conftest import seed_filing
from portfolio_common.db import Database
from sec_xcheck import l09

from fundamental_agent import db
from fundamental_agent.statements import Statements, iter_facts

FIXTURES = Path(__file__).parent / "fixtures"
FY2022 = "financials_APA_10-K_2023_acc-0001784031-23-000007.json"
FY2021 = "financials_APA_10-K_2022_acc-0001784031-22-000009.json"
_M = 1_000_000.0


def _apa(name: str) -> Statements:
    return Statements.from_payload(
        cast("dict[str, Any]", json.loads((FIXTURES / name).read_text())["data"])
    )


def _income(rows: list[tuple[str, str, float]]) -> Statements:
    """A one-column income statement from (concept, label, value) rows in statement order."""
    return Statements.from_payload(
        {
            "income_statement": [
                {"concept": c, "label": label, "2023-12-31 (FY)": v} for c, label, v in rows
            ]
        }
    )


def test_apa_columns_are_derived_by_t140_from_total_revenues_and_other() -> None:
    derived = {d["p"].key: d for d in l09.derivations(_apa(FY2022))}
    d = derived["2022-12-31 (FY)"]
    assert (d["method"], d["status"], d["value"]) == ("T-140", "derived", 11_075 * _M)
    assert d["anchor_label"] == "Total revenues and other" and d["guard_pass"]
    assert all(x["method"] == "T-140" for x in derived.values())


def test_t117_corrects_a_total_that_a_later_smaller_total_contradicts() -> None:
    stmts = _income(
        [
            ("us-gaap_Revenues", "Total revenues", 2000.0),  # the gateway's inflated figure
            ("us-gaap_DerivativeGainLoss", "Derivative instrument gain", 50.0),
            ("apa_RevenuesAndOther", "Total revenues and other", 1000.0),
        ]
    )
    (d,) = l09.derivations(stmts)
    assert (d["method"], d["status"], d["value"], d["filed_total"]) == (
        "T-117",
        "derived",
        950.0,
        2000.0,
    )
    assert d["anchor_value"] == 1000.0 and d["between"] == [50.0]
    assert (
        stmts.get("revenue", "2023-12-31 (FY)") == 950.0
    )  # what the resolver stores is what the register lists


def test_a_derivation_whose_guard_fails_is_refused_not_derived() -> None:
    stmts = _income(
        [
            ("us-gaap_Revenues", "Total revenues", 2000.0),
            (
                "us-gaap_OtherNonoperatingIncome",
                "Gain on sale",
                600.0,
            ),  # larger than 25% of the anchor
            ("apa_RevenuesAndOther", "Total revenues and other", 1000.0),
        ]
    )
    (d,) = l09.derivations(stmts)
    assert d["status"] == "refused" and d["value"] is None and not d["guard_pass"]


def test_a_cost_total_label_is_a_false_anchor_that_the_guard_refuses() -> None:
    """ADP: ``TOTAL COSTS OF REVENUES`` reads as a revenue total because the exclusion matches ``cost`` but not ``costs``;
    the guard refuses it and the resolver keeps the component sum -- so a refusal is not always an empty value."""
    stmts = _income(
        [
            ("us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", 15000.0),
            ("us-gaap_CostOfRevenue", "Cost of revenues", 7000.0),
            ("us-gaap_OtherCosts", "Selling", 700.0),
            ("adp_TotalCosts", "TOTAL COSTS OF REVENUES", 8445.0),
        ]
    )
    (d,) = l09.derivations(stmts)
    assert d["status"] == "refused" and d["method"] == "T-140"
    assert stmts.get("revenue", "2023-12-31 (FY)") == 15000.0  # not left empty


def test_a_column_with_a_filed_total_and_no_contradiction_is_not_in_the_register() -> None:
    assert l09.derivations(_income([("us-gaap_Revenues", "Total revenues", 100.0)])) == []


def test_the_register_dedupes_comparatives_and_adds_the_ex_post_value(
    memory_db: Database, tmp_path: Path
) -> None:
    memory_db.execute("INSERT INTO sectors (id, name) VALUES (1, 'Energy')")
    memory_db.execute(
        "INSERT INTO assets (id, ticker, company_name, cik, sector_id) VALUES (1, 'APA', 'APA', '0001841666', 1)"
    )
    for name, period_end, filed, acc in (
        (FY2021, "2021-12-31", "2022-02-24", "a-2021"),
        (FY2022, "2022-12-31", "2023-02-23", "a-2022"),
    ):
        fid = seed_filing(memory_db, 1, period_end=period_end, filing_date=filed)
        memory_db.execute("UPDATE sec_filings SET accession_number = ? WHERE id = ?", (acc, fid))
        db.append_financial_facts(memory_db, fid, iter_facts(_apa(name)))
    rows = l09.build_register(memory_db)
    by = {(r["column_period_end"], r["column_tag"]): r for r in rows}
    fy2021 = by[("2021-12-31", "FY")]
    assert (
        fy2021["column_kind"] == "own" and fy2021["accession"] == "a-2021"
    )  # the filing's own column is the representative
    assert (
        fy2021["also_derived_in_filings"] == 1
    )  # the same value is a comparative in the FY2022 10-K
    assert fy2021["expost_accession"] == "a-2022" and float(fy2021["expost_value"]) == 7_985 * _M
    assert float(fy2021["expost_difference"]) == 0.0 and "may be restated" in fy2021["expost_note"]
    assert fy2021["verified_by"] == "" and fy2021["verified_on"] == ""  # for the user's hand check
    out = tmp_path / "l09.csv"
    l09.write_csv(rows, out)
    with out.open(encoding="utf-8") as fh:
        assert next(iter(csv.DictReader(fh))).keys() >= {
            "verified_by",
            "verified_on",
            "expost_difference",
        }
    f = {x.key: x for x in l09.findings(rows, 2)}
    assert f["L09.derived_values"].count == len([r for r in rows if r["status"] == "derived"])
