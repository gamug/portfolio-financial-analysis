"""The D6 staged evidence for the leavers' company type (T-153 parts 3a/3b).

The rows are evidence, not a classification: the delivered GICS label, the 10-K's own Article 9
statement and the cover page's registered classes are staged here, and
``scripts/build_company_profile.py`` assigns the type and merges the rows into the profile.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

from kg_schema import company_profile

STAGED = Path(__file__).resolve().parent.parent / "docs" / "leaver_company_types.csv"
_SPEC = importlib.util.spec_from_file_location(
    "leaver_company_types",
    Path(__file__).resolve().parent.parent / "scripts" / "leaver_company_types.py",
)
assert _SPEC and _SPEC.loader
staged_script = importlib.util.module_from_spec(_SPEC)
sys.modules["leaver_company_types"] = staged_script
_SPEC.loader.exec_module(staged_script)


def _rows() -> list[dict[str, str]]:
    text = STAGED.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    return list(csv.DictReader(lines))


def test_the_staged_evidence_covers_every_leaver_cik() -> None:
    rows = _rows()
    assert len(rows) == 86
    assert len({r["cik"] for r in rows}) == 86
    for row in rows:
        assert row["cik"] and row["symbols"] and row["exit_date"]
        assert row["article_9"] in {"true", "false"}


def test_a_found_statement_carries_its_quote_and_accession() -> None:
    for row in _rows():
        if row["article_9"] == "true":
            assert row["evidence_quote"].strip()
            assert row["last_10k_accession"]
        else:
            assert not row["evidence_quote"].strip()


def test_a_quote_is_plain_verbatim_text() -> None:
    """The review's fix: no HTML entities survive into a quote."""
    for row in _rows():
        assert "&#" not in row["evidence_quote"]
        assert "&amp;" not in row["evidence_quote"]


def test_the_hand_verified_rows_carry_their_own_statement() -> None:
    """CMA and ZION were verified by hand (the method's sentence was generic / could not match);
    DRE's was a director's other seat, so it is not an Article 9 statement at all."""
    by_cik = {r["cik"]: r for r in _rows()}
    assert by_cik["0000028412"]["article_9"] == "true"  # Comerica
    assert "financial holding company" in by_cik["0000028412"]["evidence_quote"]
    assert by_cik["0000109380"]["article_9"] == "true"  # Zions
    assert "is a bank headquartered" in by_cik["0000109380"]["evidence_quote"]
    assert by_cik["0000783280"]["article_9"] == "false"  # Duke Realty
    assert by_cik["0000783280"]["evidence_quote"] == ""


#: CIKs that left the index **after** the profile was generated: the production-derived
#: ``company_types.csv`` still carries them (its ``assets`` were never re-synced), so part 3b
#: replaces those five rows' intervals rather than adding them.
_RECENT_DEPARTURES = {
    "0000024545",  # TAP, left 2026-09-20
    "0001316835",  # BLDR, left 2026-09-20
    "0001437107",  # WBD, left 2026-10-05
    "0001671933",  # TTD, left 2026-09-20
    "0001755672",  # CTVA, left 2026-10-05
}
#: The two APP-00 leaves untyped: no 10-K or 10-Q on EDGAR, so no Article 9 statement exists, and
#: the other-financial codes do not include 40101015 (T-153 part 3b, reviewer A 2026-10-14).
_UNTYPED = {"0001132979", "0001288784"}


def test_every_staged_cik_is_in_the_profile_except_the_two_app00_leaves_untyped() -> None:
    """Part 3b merges the leavers' rows: after it, the profile holds every staged CIK but the two
    no cell of APP-00's table applies to."""
    staged = {r["cik"] for r in _rows()}
    assert staged - set(company_profile.profiles()) == _UNTYPED


def test_the_recent_departures_are_replaced_by_their_own_interval() -> None:
    """They are in the profile, but bounded at their own exit -- not the table's open interval."""
    rows = company_profile.profiles()
    for cik in _RECENT_DEPARTURES:
        assert rows[cik].valid_to, f"{cik} should carry its own valid_to"


def test_the_delivered_gics_label_is_staged_with_its_revision() -> None:
    """T-153 part 3b: the label in force on the last membership day, cited by its revision. MBC,
    FTRE and PHIN are the three the page never listed (one-to-four-day spin-off members)."""
    rows = _rows()
    empty = [r["symbols"] for r in rows if not r["gics_sub_industry"]]
    assert sorted(empty) == ["FTRE", "MBC", "PHIN"]
    for row in rows:
        if not row["gics_sub_industry"]:
            continue
        assert row["gics_sector"]
        assert row["gics_revision_id"].isdigit()
        assert row["gics_revision_timestamp"]


def test_the_cover_page_symbols_are_staged_for_every_row_with_a_10k() -> None:
    """The SEC's point-in-time class list (D7): the cover of the last 10-K on or before the exit.
    SOLS has no 10-K in the window either (a 2025 spin-off), like SBNY and FRC."""
    rows = _rows()
    without = sorted(r["symbols"] for r in rows if not r["cover_symbols"])
    assert without == ["FRC", "FTRE", "MBC", "PHIN", "SBNY", "SOLS"]
    for row in rows:
        if row["cover_symbols"]:
            assert row["last_10k_accession"]


def test_the_multi_class_rows_under_the_cover_page_rule() -> None:
    """A leaver is multi-class when its last 10-K's cover registers more than one class -- the
    classification in force on its last membership day, not the submissions file's live list."""
    multi = {r["symbols"] for r in _rows() if len(r["cover_symbols"].split(" | ")) > 1}
    assert multi == {
        "BIO",
        "TAP",
        "K",
        "LNC",
        "VFC",
        "ZION",
        "SIVB",
        "VNO",
        "BWA",
        "EMN",
        "LKQ",
        "CE",
        "UA/UAA",
        "PBCT",
        "DISCA/DISCK/WBD",
        "WBA",
        "DXC",
        "CTVA",
    }
    # Discovery's own classes (DISCA/DISCB/DISCK) are not the reason: the CIK's last 10-K is
    # WBD's (it left on 2026-10-05), whose cover registers WBD and two note series
    discovery = next(r for r in _rows() if r["cik"] == "0001437107")
    assert discovery["cover_symbols"] == "WBD | WBDI30 | WBDI33"
    assert discovery["last_10k_accession"] == "0001437107-26-000020"


def test_cover_symbols_reads_the_inline_xbrl_facts() -> None:
    """The cover parser: one entry per ``dei:TradingSymbol`` fact, distinct, in document order."""
    html = (
        '<ix:nonNumeric name="dei:TradingSymbol" id="f-1">UAA</ix:nonNumeric>'
        '<ix:nonNumeric id="f-2" name="dei:TradingSymbol">UA</ix:nonNumeric>'
        '<ix:nonNumeric name="dei:TradingSymbol">CE /26A</ix:nonNumeric>'
        '<ix:nonNumeric name="dei:TradingSymbol">UAA</ix:nonNumeric>'  # a repeat is dropped
    )

    class FakeClient:
        def get_document(self, cik: str, accession: str, name: str) -> str:
            return html

    filing = {"accessionNumber": "a", "primaryDocument": "d"}
    assert staged_script.cover_symbols(FakeClient(), "0001336917", filing) == [
        "UAA",
        "UA",
        "CE /26A",
    ]


def test_a_cover_with_no_trading_symbol_fact_is_empty() -> None:
    class FakeClient:
        def get_document(self, cik: str, accession: str, name: str) -> str:
            return "<html><body>no facts here</body></html>"

    filing = {"accessionNumber": "a", "primaryDocument": "d"}
    assert staged_script.cover_symbols(FakeClient(), "1", filing) == []
