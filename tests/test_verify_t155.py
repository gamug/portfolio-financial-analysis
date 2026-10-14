"""``scripts/verify_t155.py``: the succession classification's pure pieces and its reading table."""

from __future__ import annotations

import csv
import importlib.util
import sqlite3
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "verify_t155", Path(__file__).resolve().parent.parent / "scripts" / "verify_t155.py"
)
assert _SPEC and _SPEC.loader
verify = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_t155"] = verify
_SPEC.loader.exec_module(verify)


def test_to_text_drops_markup_and_collapses_space() -> None:
    assert verify.to_text("<p>Hello&nbsp; <b>world</b></p>\n<p>2</p>") == "Hello world\n2"


def test_succession_sentence_prefers_the_sentence_that_names_the_predecessor() -> None:
    text = (
        "This Current Report establishes New X as the successor issuer to Old X pursuant to Rule 12g-3(a). "
        "Other filler text that is longer."
    )
    assert verify.succession_sentence(text, "Old X") == (
        "This Current Report establishes New X as the successor issuer to Old X pursuant to Rule 12g-3(a)."
    )


def test_succession_sentence_falls_back_to_a_rule_12g_3_sentence() -> None:
    text = "The new shares are deemed registered under Section 12(b) pursuant to Rule 12g-3(a)."
    assert verify.succession_sentence(text, "New X") == text


def test_succession_sentence_is_none_without_a_succession() -> None:
    assert verify.succession_sentence("A plain 8-K about a dividend.", "X") is None


def test_the_reading_covers_every_register_line() -> None:
    with verify.REGISTER.open(newline="", encoding="utf-8") as fh:
        register = list(csv.DictReader(fh))
    assert len(register) == 19
    for row in register:
        assert (row["ticker"], row["accession_number"]) in verify.READING


def test_the_amendment_uses_the_original_filings_evidence() -> None:
    assert verify.EVIDENCE_OF["0002041610-25-000029"] == "0001193125-25-175046"


def test_inputs_at_picks_the_latest_filing_and_its_prior_10k() -> None:
    rows = [
        {
            "form": "10-K",
            "filingDate": "2023-02-24",
            "reportDate": "2022-12-31",
            "accessionNumber": "k1",
            "_cik": "pred",
        },
        {
            "form": "10-Q",
            "filingDate": "2023-05-05",
            "reportDate": "2023-03-31",
            "accessionNumber": "q1",
            "_cik": "own",
        },
    ]
    latest, prior = verify.inputs_at(rows, "2023-05-05")
    assert latest["accessionNumber"] == "q1"
    assert prior is not None and prior["accessionNumber"] == "k1"
    # a 10-K's own prior column carries the prior-year capital, so there is no separate prior 10-K
    assert verify.inputs_at(rows, "2023-04-01")[0]["accessionNumber"] == "k1"
    assert verify.inputs_at(rows, "2023-04-01")[1] is None


def test_first_member_date_reads_the_universe_file(tmp_path: Path) -> None:
    path = tmp_path / "u.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE universe_membership (symbol TEXT, cik TEXT, valid_from TEXT, valid_to TEXT)"
    )
    conn.execute("INSERT INTO universe_membership VALUES ('X', '0000000001', '2021-01-01', NULL)")
    conn.execute("INSERT INTO universe_membership VALUES ('Y', '0000000002', '2023-05-01', NULL)")
    conn.commit()
    conn.close()
    assert verify.first_member_date(path, "0000000001") == verify.WINDOW_START
    assert verify.first_member_date(path, "0000000002") == "2023-05-01"
    assert verify.first_member_date(path, "0000000009") is None


def test_is_multi_year_marks_growth_cagr_and_the_cash_flow_group() -> None:
    assert verify._is_multi_year("revenue_growth", "growth")
    assert verify._is_multi_year("net_income_cagr", "cagr")
    assert verify._is_multi_year("free_cash_flow_margin", "cashflow")  # trailing twelve months
    assert not verify._is_multi_year("net_margin", "profitability")
