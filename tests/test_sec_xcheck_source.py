"""Level (a): ``source_measures`` on a synthetic ``companyfacts`` document, ``precedence``, the submissions-based
checks and the runner helpers (T-147). Hermetic: the SEC side is a dict written to a temporary cache."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest
from sec_xcheck import period_measures as pm
from sec_xcheck import precedence, runner
from sec_xcheck import source_measures as sm
from sec_xcheck.common import SecClient, index_by_accession
from sec_xcheck.itemcheck import own_concepts
from sec_xcheck_support import metric, rec

from fundamental_agent.statements import Statements

ACCN = "0000000001-24-000001"


def _fact(end: str, val: float, start: str | None = None) -> dict[str, object]:
    out: dict[str, object] = {"end": end, "val": val, "accn": ACCN, "filed": "2024-02-20"}
    if start:
        out["start"] = start
    return out


def _doc(**concepts: tuple[str, list[dict[str, object]]]) -> dict[str, object]:
    return {
        "facts": {"us-gaap": {c: {"units": {unit: facts}} for c, (unit, facts) in concepts.items()}}
    }


FY = ("2023-01-01", "2023-12-31")
DOC = _doc(
    IncomeTaxExpenseBenefit=("USD", [_fact(FY[1], 25e6, FY[0])]),
    NetCashProvidedByUsedInOperatingActivities=("USD", [_fact(FY[1], 500e6, FY[0])]),
    NetCashProvidedByUsedInOperatingActivitiesContinuingOperations=(
        "USD",
        [_fact(FY[1], 450e6, FY[0])],
    ),
    LongTermDebt=("USD", [_fact(FY[1], 900e6)]),
    LongTermDebtCurrent=("USD", [_fact(FY[1], 100e6)]),
    Assets=("USD", [_fact(FY[1], 2000e6)]),
    LiabilitiesAndStockholdersEquity=("USD", [_fact(FY[1], 1900e6)]),
    Depreciation=("USD", [_fact(FY[1], 30e6, FY[0])]),
    CommonStockSharesOutstanding=(
        "USD",
        [_fact(FY[1], 1e9)],
    ),  # a share count carried in USD: ID-04
    Revenues=("USD", [_fact(FY[1], -5e6, FY[0])]),
)


def _index() -> sm.Index:
    return index_by_accession(DOC)  # type: ignore[arg-type,return-value]


def test_a_displayed_sign_that_is_the_negative_of_the_filed_value_is_flipped() -> None:
    r = rec(items={"income_tax": -25e6}, accession=ACCN)
    tallies: dict[str, Any] = defaultdict(sm.Tally)
    sm.item_checks(tallies, r, _index(), own_concepts())
    assert (
        tallies["item.income_tax.FLIPPED"].n == 1
        and tallies["item.income_tax.ok_own_concept"].n == 0
    )


def test_content_conditions_behind_n3_n4_n14_are_read_from_the_filed_facts() -> None:
    tallies: dict[str, Any] = defaultdict(sm.Tally)
    sm.content_checks(tallies, rec(accession=ACCN), _index())
    assert tallies["N3"].n == 1  # both operating cash flow lines filed with different values
    assert (
        tallies["N4"].n == 1
    )  # LongTermDebt (incl. current maturities) with LongTermDebtCurrent, no noncurrent line
    assert tallies["N14"].n == 1  # Depreciation is the only D&A concept
    assert tallies["N5"].n == 0 and tallies["D6c"].n == 0


def test_identities_are_tested_on_filed_values() -> None:
    tallies: dict[str, Any] = defaultdict(sm.Tally)
    sm.identity_checks(tallies, rec(accession=ACCN), _index())
    assert tallies["id.CON-01.block"].n == 1  # assets 2,000M against liabilities and equity 1,900M


def test_units_and_atypical_negatives_are_counted_for_registry_concepts() -> None:
    tallies: dict[str, Any] = defaultdict(sm.Tally)
    sm.fact_checks(
        tallies, rec(accession=ACCN), _index(), {c for cs in own_concepts().values() for c in cs}
    )
    assert tallies["ID04"].n == 1  # CommonStockSharesOutstanding carries USD, not shares
    assert tallies["ID13proxy"].n == 1  # negative Revenues


def test_measure_reads_each_ciks_facts_once_and_marks_the_proxy_incomplete(tmp_path: Path) -> None:
    path = tmp_path / "companyfacts" / "CIK0000000001.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(DOC))
    r = rec(cik="1", items={"income_tax": -25e6}, accession=ACCN)
    found = {f.key: f for f in sm.measure([r], SecClient(tmp_path, offline=True))}
    assert found["src.income_tax.FLIPPED"].count == 1
    assert found[
        "src.ID13.negative_atypical_proxy"
    ].incomplete  # the DQC lists are not in the registered sources
    assert found["src.N12.dead_concept_facts_ours"].count == 0


# -- precedence (D2) ------------------------------------------------------------------------------------
def test_concept_first_resolution_beats_a_label_match_that_comes_earlier_in_the_document() -> None:
    stmts = Statements.from_payload(
        {
            "balance_sheet": [
                {
                    "concept": "us-gaap_StockholdersEquityBeforeTreasuryStock",
                    "label": "Total shareholders' equity",
                    "standard_concept": None,
                    "2023-12-31": 700.0,
                },
                {
                    "concept": "us-gaap_StockholdersEquity",
                    "label": "Equity",
                    "standard_concept": None,
                    "2023-12-31": 500.0,
                },
            ]
        }
    )
    both = precedence.resolve_both(stmts, "2023-12-31 (FY)")
    assert both["equity"] == (
        700.0,
        500.0,
    )  # document order reads the wrong row; the spec's order reads the right one
    assert stmts.get("equity", "2023-12-31") == 700.0  # and the patch is undone afterwards


# -- submissions-based checks --------------------------------------------------------------------------------
def _submissions(tmp_path: Path, rows: list[tuple[str, str, str, str]]) -> SecClient:
    path = tmp_path / "submissions" / "CIK0000000001.json"
    path.parent.mkdir(parents=True)
    cols = {
        "accessionNumber": [],
        "form": [],
        "filingDate": [],
        "reportDate": [],
        "primaryDocument": [],
    }  # type: ignore[var-annotated]
    for accn, form, filed, report in rows:
        for k, v in zip(cols, (accn, form, filed, report, "x.htm"), strict=True):
            cols[k].append(v)
    path.write_text(json.dumps({"filings": {"recent": cols, "files": []}}))
    return SecClient(tmp_path, offline=True)


class _FakeConn:
    def execute(self, sql: str) -> list[dict[str, str]]:
        return [{"ticker": "AAA", "cik": "1"}]


def test_a_stored_period_end_that_differs_from_the_header_is_reported(tmp_path: Path) -> None:
    client = _submissions(tmp_path, [(ACCN, "10-K", "2024-02-20", "2023-12-31")])
    good = rec(cik="1", period_end="2023-12-31", accession=ACCN)
    bad = rec(cik="1", period_end="2022-12-31", ticker="BAD", accession=ACCN)
    found = {f.key: f for f in pm.id07(None, client, [good, bad])}
    assert found["ID07.period_end_differs_from_header"].count == 1
    assert found["ID07.period_end_differs_from_header"].examples[0][0] == "BAD"


def test_transition_reports_and_succession_8ks_are_counted_per_company(tmp_path: Path) -> None:
    client = _submissions(
        tmp_path,
        [
            ("a-1", "10-KT", "2026-02-27", "2025-12-31"),
            ("a-2", "8-K12B", "2024-10-01", ""),
            ("a-3", "8-K", "2024-10-01", ""),
        ],
    )
    found = {f.key: f for f in pm.forms_among_ciks(_FakeConn(), client)}
    assert (found["PER09.transition_reports"].count, found["APP10b.succession_8ks"].count) == (1, 1)


# -- runner -------------------------------------------------------------------------------------------------------
def test_read_types_keys_the_csv_by_cik(tmp_path: Path) -> None:
    path = tmp_path / "types.csv"
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["cik", "type", "financial_firm"])
        w.writeheader()
        w.writerow({"cik": "1", "type": "article_9", "financial_firm": "True"})
    assert runner.read_types(path)["1"]["type"] == "article_9"


def test_the_quarter_table_reports_financial_vetoes_per_rule() -> None:
    recs = [
        rec(
            cik=str(i),
            ticker=f"T{i}",
            metrics={
                "free_cash_flow_margin": metric(
                    -0.1 if i == 0 else 0.1, "cashflow", revenue=100.0, net_income=5.0
                )
            },
        )
        for i in range(25)
    ]
    table = runner.quarter_table(recs, {"0"}, ["2024-06-30"])
    assert table[0]["evaluated"] == 25 and table[0]["financial_hard"] == 1
    assert table[0]["financial_by_rule"] == {"NEGATIVE_FCF": 1}


def test_a_date_with_too_few_annual_filings_is_skipped() -> None:
    assert runner.quarter_table([rec()], set(), ["2024-06-30"]) == []


@pytest.mark.parametrize(
    "group",
    [
        "n1",
        "resolver",
        "capex",
        "metrics",
        "periods",
        "source",
        "cycle",
        "marketcap",
        "stored",
        "extra",
    ],
)
def test_every_documented_group_exists(group: str) -> None:
    assert group in runner.GROUPS
