"""The tracked company profile and its reader (T-071 part 1, P1/P2; checklist APP-00).

Reads only the tracked ``src/kg_schema/data/company_profile.csv`` and the reader,
``kg_schema.company_profile`` -- never ``docs/checklist_sec/`` (AGENTS.md). The counts are the
checklist's own company-type table's (the handoff's "What reviewer A found"), so a future change to
them (``T-153`` adds the leavers) has to update this test deliberately.
"""

from __future__ import annotations

import re
from collections import Counter

from kg_schema import applicability
from kg_schema.company_profile import PROFILE_CSV, profile_as_of, profiles

# the checklist's own counts (APP-00's table: 24 Article 9, 17 Article 7, 10 other financial,
# 1 multi-sector holding, 448 operating; overlays REIT 28, multi-class 76)
TYPES = {
    "operating": 448,
    "article_9": 24,
    "article_7": 17,
    "other_financial": 10,
    "multi_sector_holding": 1,
}
REITS = 28
MULTI_CLASS = 76
# D-13's GICS proxy: Electric 15, Gas 1, Multi- 12, Water 1
REGULATED_UTILITIES = 29


def _text() -> str:
    return PROFILE_CSV.read_text(encoding="utf-8")


def test_the_header_names_the_generator_and_the_source_sha256() -> None:
    header = [line for line in _text().splitlines() if line.startswith("#")]
    assert any("scripts/build_company_profile.py" in line for line in header)
    assert any("docs/checklist_sec/company_types.csv" in line for line in header)
    assert any(re.search(r"sha256 [0-9a-f]{64}", line) for line in header)


def test_the_profile_is_the_checklists_type_table_with_the_added_columns() -> None:
    rows = list(profiles().values())
    assert len(rows) == sum(TYPES.values()) == 500
    assert Counter(row.type for row in rows) == Counter(TYPES)
    assert sum(1 for row in rows if row.reit) == REITS
    assert sum(1 for row in rows if row.multi_class) == MULTI_CLASS
    assert sum(1 for row in rows if row.regulated_utility) == REGULATED_UTILITIES
    # APP-00: "financial firm" is exactly the three financial types
    assert all(
        row.financial_firm == (row.type in {"article_9", "article_7", "other_financial"})
        for row in rows
    )
    # every CIK is the 10-digit string the reader is keyed by (APP-10a)
    assert all(len(row.cik) == 10 and row.cik.isdigit() for row in rows)


def test_the_reader_is_keyed_by_the_cik_and_knows_no_other_company() -> None:
    apple = profile_as_of("320193")  # the same CIK, unpadded (kg_schema.identity.normalize_cik)
    assert apple is not None
    assert apple.cik == "0000320193"
    assert apple.type == "operating"
    assert apple.financial_firm is False
    wells = profile_as_of("0000072971")
    assert wells is not None and wells.type == "article_9" and wells.financial_firm
    # an unknown CIK is None -- never a guessed type
    assert profile_as_of("9999999999") is None
    assert profile_as_of(None) is None
    # the interval: valid_from is the data window's start (ID-20), valid_to is open (L-03)
    assert apple.valid_from == "2020-01-01" and apple.valid_to == ""
    assert profile_as_of("320193", "2021-01-01") is not None
    assert profile_as_of("320193", "2019-12-31") is None


def test_the_two_readers_agree_on_the_company_type() -> None:
    """A multi-sector holding is ADJ everywhere (APP-00); an operating company is A here and ADJ only
    where an overlay says so."""
    berkshire = profile_as_of("0001067983")
    assert berkshire is not None and berkshire.multi_sector
    for metric in applicability.metrics():
        assert applicability.applicability(metric, berkshire, negative_equity=False) == (
            "ADJ",
            "APP-00",
        )
    apple = profile_as_of("0000320193")
    assert apple is not None
    assert applicability.applicability("gross_margin", apple, negative_equity=False) == ("A", "")
    # a metric the matrix does not cover has no claim on it (`ebitda` is a building block, and the
    # robustness ROIC variant is Annex B's, not the matrix's)
    assert "ebitda" not in applicability.metrics()
    assert applicability.applicability("ebitda", apple, negative_equity=False) == ("A", "")
