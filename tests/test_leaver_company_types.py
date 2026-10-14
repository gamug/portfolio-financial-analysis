"""The D6 staged evidence for the leavers' company type (T-153 part 3a).

The rows are evidence, not a classification: part 3b adds the leavers' GICS, decides the Article
7 / other-financial types and merges them into ``company_types.csv``. This file must not collide
with the current profile's CIKs.
"""

from __future__ import annotations

import csv
from pathlib import Path

from kg_schema import company_profile

STAGED = Path(__file__).resolve().parent.parent / "docs" / "leaver_company_types.csv"


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
#: must edit those five rows' intervals rather than add them.
_RECENT_DEPARTURES = {
    "0000024545",  # TAP, left 2026-09-20
    "0001316835",  # BLDR, left 2026-09-20
    "0001437107",  # WBD, left 2026-10-05
    "0001671933",  # TTD, left 2026-09-20
    "0001755672",  # CTVA, left 2026-10-05
}


def test_the_staged_ciks_overlap_the_profile_only_for_recent_departures() -> None:
    """A leaver is not a current member, so part 3b merges new CIKs -- except the five that left
    after the profile was generated and are still in its snapshot."""
    staged = {r["cik"] for r in _rows()}
    assert (staged & set(company_profile.profiles())) == _RECENT_DEPARTURES
