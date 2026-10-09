"""The pieces added after the first review of T-147 (``review_T-147_wip.md``): the DQC element lists, N1's causes, the
columns the pipeline reads (F2), the per-company L-09 findings (F1) and the O&G capex table."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from sec_xcheck import capex_measures as cx
from sec_xcheck import dqc_lists, dqc_measures, golden, l09, records
from sec_xcheck import n1_amendments as n1
from sec_xcheck.common import SecClient
from sec_xcheck_support import rec

from fundamental_agent.statements import Statements

G = "us-gaap_"


# -- DQC lists -------------------------------------------------------------------------------------------
def _xlsx(path: Path, rows: list[list[str]]) -> None:
    strings: list[str] = []
    cells = []
    for r, row in enumerate(rows, 1):
        for c, text in enumerate(row):
            if text not in strings:
                strings.append(text)
            cells.append(f'<c r="{chr(65 + c)}{r}" t="s"><v>{strings.index(text)}</v></c>')
    by_row: dict[int, list[str]] = {}
    for cell in cells:
        by_row.setdefault(int("".join(ch for ch in cell.split('"')[1] if ch.isdigit())), []).append(
            cell
        )
    sheet = "".join(f'<row r="{r}">{"".join(cs)}</row>' for r, cs in by_row.items())
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "xl/sharedStrings.xml",
            f"<sst {ns}>" + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>",
        )
        z.writestr(
            "xl/worksheets/sheet1.xml",
            f"<worksheet {ns}><sheetData>{sheet}</sheetData></worksheet>",
        )


def _lists(tmp: Path) -> Path:
    _xlsx(
        tmp / "DQC_0014_ListOfElements.xlsx",
        [["Release Version", "namespace", "element"], ["2.0", "us-gaap", "DerivativeLiabilities"]],
    )
    _xlsx(
        tmp / "DQC_0013_ListOfElements.xlsx",
        [
            ["preconditionValue", "preconditionCriteria", "namespace", "elementName"],
            ["pretax", "> 0", "us-gaap", "EffectiveIncomeTaxRateReconciliationTaxCredits"],
        ],
    )
    for year in dqc_lists.YEARS:
        (tmp / f"dqc_15_usgaap_{year}_concepts.csv").write_text(
            "1,DividendsCommonStock\n2,PaymentsToAcquirePropertyPlantAndEquipment\n"
        )
    return tmp


def test_the_lists_are_read_with_the_standard_library(tmp_path: Path) -> None:
    d = _lists(tmp_path)
    assert dqc_lists.dqc14(d) == {"DerivativeLiabilities"}
    assert set(dqc_lists.dqc13(d)) == {"EffectiveIncomeTaxRateReconciliationTaxCredits"}
    assert dqc_lists.dqc15(d)[2022] == {
        "DividendsCommonStock",
        "PaymentsToAcquirePropertyPlantAndEquipment",
    }


def _doc(accn: str) -> dict[str, object]:
    def f(end: str, val: float, start: str | None = "2023-01-01") -> dict[str, object]:
        out: dict[str, object] = {"end": end, "val": val, "accn": accn, "filed": "2024-02-20"}
        if start:
            out["start"] = start
        return out

    def units(*facts: dict[str, object]) -> dict[str, object]:
        return {"units": {"USD": list(facts)}}

    return {
        "facts": {
            "us-gaap": {
                "DividendsCommonStock": units(
                    f("2023-12-31", -5.0)
                ),  # DQC_0015: must not be negative
                "PaymentsToAcquirePropertyPlantAndEquipment": units(
                    f("2023-12-31", -10.0)
                ),  # a line item the resolver reads
                "DerivativeLiabilities": units(f("2023-12-31", -2.0, None)),  # DQC_0014
                "EffectiveIncomeTaxRateReconciliationTaxCredits": units(
                    f("2023-12-31", -0.01)
                ),  # DQC_0013, needs pretax > 0
                "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": units(
                    f("2023-12-31", 100.0)
                ),
            }
        }
    }


def test_negative_values_of_listed_elements_are_counted_on_each_rule(tmp_path: Path) -> None:
    cache = tmp_path / "cache" / "companyfacts"
    cache.mkdir(parents=True)
    (cache / "CIK0000000001.json").write_text(json.dumps(_doc("a-1")))
    lists = dqc_measures.load_lists(_lists(tmp_path))
    found = {
        f.key: f
        for f in dqc_measures.measure(
            [rec(cik="1", accession="a-1")], SecClient(tmp_path / "cache", offline=True), lists
        )
    }
    assert found["src.ID-13.negative_listed_element"].count == 1
    assert (
        "1 of these filings are on an element the resolver reads"
        in found["src.ID-13.negative_listed_element"].note
    )
    assert (
        found["src.ID-14.negative_listed_element"].count == 1
    )  # pre-tax income is positive: the precondition holds
    assert found["src.ID-15.negative_listed_element"].count == 1


def test_the_dqc_0013_precondition_needs_positive_pretax_income() -> None:
    name = dqc_measures.PRETAX[0]
    assert (
        dqc_measures.pretax_positive(
            {(name, "2023-01-01|2023-12-31"): 5.0}, "2023-12-31", "2023-01-01"
        )
        is True
    )
    assert (
        dqc_measures.pretax_positive(
            {(name, "2023-01-01|2023-12-31"): -5.0}, "2023-12-31", "2023-01-01"
        )
        is False
    )
    assert dqc_measures.pretax_positive({}, "2023-12-31", "2023-01-01") is None


# -- N1 causes -----------------------------------------------------------------------------------------------
def _orig(report: str = "2022-12-31", filed: str = "2023-02-22") -> dict[str, str]:
    return {"reportDate": report, "filingDate": filed, "accessionNumber": "a"}


def test_a_52_53_week_year_end_one_year_after_a_january_year_end_is_a_label_collision() -> None:
    # AVY: FY ended 2022-01-01 (stored, labelled FY2022) and FY ended 2022-12-31 shares that label; the later one is lost
    assert n1.missing_causes(
        _orig(),
        ["2022-01-01", "2023-12-30"],
        amended=False,
        last_stored_filing="2026-09-02",
        has_error=False,
    ) == ["label_collision"]


def test_causes_are_not_exclusive_and_the_default_is_unexplained() -> None:
    both = n1.missing_causes(
        _orig(), ["2022-01-01"], amended=True, last_stored_filing="2026-09-02", has_error=True
    )
    assert both == ["label_collision", "amendment", "recorded_error"]
    assert n1.missing_causes(
        _orig("2021-12-31"), [], amended=False, last_stored_filing="2026-09-02", has_error=False
    ) == ["unexplained"]
    late = _orig(filed="2026-09-03")
    assert "after_cutoff" in n1.missing_causes(
        late, [], amended=False, last_stored_filing="2026-09-02", has_error=False
    )


# -- F2: the columns the pipeline reads -----------------------------------------------------------------------
def _q_payload() -> Statements:
    cols = {
        "2023-06-30 (Q2)": 10.0,
        "2022-06-30 (Q2)": 8.0,
        "2023-06-30 (YTD)": 21.0,
        "2022-06-30 (YTD)": 17.0,
    }
    row = {"concept": G + "NetCashProvidedByUsedInOperatingActivities", "label": "OCF", **cols}
    return Statements.from_payload(
        {
            "cash_flow": [row],
            "balance_sheet": [{"concept": G + "Assets", "label": "Assets", "2023-06-30": 1.0}],
        }
    )


def test_a_10q_is_read_in_its_target_prior_and_both_year_to_date_columns() -> None:
    stmts = _q_payload()
    target = next(p for p in stmts.periods if p.key == "2023-06-30 (Q2)")
    roles = {role: p.key for role, p in records.pipeline_columns(stmts, "10-Q", target)}
    assert roles == {
        "prior": "2022-06-30 (Q2)",
        "ytd": "2023-06-30 (YTD)",
        "ytd_prior": "2022-06-30 (YTD)",
    }


def test_a_10k_is_read_in_its_target_prior_and_the_cagr_base() -> None:
    row = {
        "concept": G + "Revenues",
        "label": "Revenue",
        "2021-12-31 (FY)": 1.0,
        "2022-12-31 (FY)": 2.0,
        "2023-12-31 (FY)": 3.0,
    }
    stmts = Statements.from_payload({"income_statement": [row]})
    target = next(p for p in stmts.periods if p.date == "2023-12-31")
    roles = {role: p.date for role, p in records.pipeline_columns(stmts, "10-K", target)}
    assert roles == {"prior": "2022-12-31", "cagr_base": "2021-12-31"}


def test_further_columns_count_as_filing_columns_and_name_their_role() -> None:
    a, b = rec(), rec(role="ytd", column_end="2023-06-30", form="10-Q")
    f = records.count("k", ["r"], "t", [a, b], lambda r: True)
    assert (f.count, f.unit, f.filings) == (2, "filing-columns", 1)
    assert "[ytd 2023-06-30]" in f.examples[0][2] or "[ytd 2023-06-30]" in f.examples[-1][2]
    assert records.targets([a, b]) == [a]


# -- F1: the ICE finding ---------------------------------------------------------------------------------------
def _row(ticker: str, diff: str, pct: str = "") -> dict[str, str]:
    return {
        "ticker": ticker, "accession": "a", "status": "derived", "column_tag": "Q3", "column_period_end": "2022-09-30",
        "derived_value": "1235000000", "expost_value": "2387000000" if diff else "", "expost_difference": diff,
        "expost_difference_pct": pct, "anchor_label": "Total revenues, less transaction-based expenses",
    }  # fmt: skip


def test_a_company_whose_derived_values_fail_the_ex_post_audit_gets_its_own_finding() -> None:
    rows = [_row("ICE", "1152000000", "0.93"), _row("APA", "0", "0.0")]
    found = {f.key: f for f in l09.per_company_findings(rows)}
    assert found["L09.derived_by_company"].count == 2
    assert found["L09.expost_fails.ICE"].count == 1 and "L09.expost_fails.APA" not in found


# -- M5 / M6 ------------------------------------------------------------------------------------------------------
def test_the_og_table_lists_mineral_purchases_beside_development_by_year() -> None:
    fang = rec(ticker="FANG", capex={cx.OG_PROP: -1.57e9, cx.OG_ED: -1.85e9})
    (row,) = cx.og_year_table([fang])
    assert row["mineral_to_development"] == round(1.57 / 1.85, 3) and row["ticker"] == "FANG"
    assert cx.og_year_table([rec(capex={cx.PPE: -1.0})]) == []


def test_the_golden_set_shows_overlays_and_flags_available_to_common_net_income() -> None:
    assert (
        golden.overlays({"reit": "True", "negative_equity_latest": "True"})
        == "REIT, negative equity"
    )
    assert golden.overlays({"reit": "False"}) == "none"
    wat = rec(items={"net_income": (1.0, G + "NetIncomeLossAvailableToCommonStockholdersBasic")})
    row = golden.row(wat, "operating", "Waters")
    assert row["agree"] == "no" and "N13" in row["to_check_by_hand"]


def test_sources_register_names_the_dqc_lists() -> None:
    text = Path(__file__).resolve().parents[1] / "docs" / "checklist_sec" / "sources_register.md"
    if text.exists():  # the register is a tracked input of the audit
        assert "B_DQC_element_lists/DQC_0015_ListOfElements.xlsx" in text.read_text(
            encoding="utf-8"
        )
