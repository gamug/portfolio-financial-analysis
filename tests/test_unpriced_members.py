"""R1 (T-153 part 3c): the coverage union's classification, the import's two sides, the trigger.

The measurement itself is a real-data script; what is pinned here is the rule it applies, so a
re-run cannot silently change a class, a bound or the trigger's meaning.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "unpriced_members",
    Path(__file__).resolve().parent.parent / "scripts" / "unpriced_members.py",
)
assert _SPEC and _SPEC.loader
unpriced = importlib.util.module_from_spec(_SPEC)
sys.modules["unpriced_members"] = unpriced
_SPEC.loader.exec_module(unpriced)


def test_a_member_symbol_resolves_to_its_request_line() -> None:
    """DISCA and CDAY are the request's own aliases (its "Covers" column): WBD and DAY."""
    assert unpriced.line_of("DISCA") == "WBD"
    assert unpriced.line_of("CDAY") == "DAY"
    assert unpriced.line_of("wbd") == "WBD"
    assert unpriced.line_of("VLTO") == "VLTO"


def test_a_delivered_line_is_priced_after_the_import_but_its_tail_is_not() -> None:
    """SIVB is delivered through 2023-03-09: its halt days stay unpriced, earlier days do not."""
    assert unpriced.unpriced_after_import("SIVB", "2023-03-09") is False
    assert unpriced.unpriced_after_import("SIVB", "2023-03-10") is True
    assert unpriced.unpriced_after_import("SBNY", "2023-03-13") is True
    assert unpriced.unpriced_after_import("XLNX", "2022-02-14") is True
    assert unpriced.unpriced_after_import("XLNX", "2022-02-11") is False


def test_info_and_wrk_stay_unpriced_for_the_whole_line() -> None:
    assert unpriced.unpriced_after_import("INFO", "2022-01-03") is True
    assert unpriced.unpriced_after_import("WRK", "2024-07-03") is True
    assert unpriced.unpriced_class("INFO", "2022-01-03").startswith("permanent")
    assert unpriced.unpriced_class("WRK", "2024-07-03").startswith("permanent")


def test_a_non_request_member_gap_is_permanent() -> None:
    """VLTO is a current member, not one of the 35: the import does not price it."""
    assert unpriced.unpriced_after_import("VLTO", "2023-10-02") is True
    assert unpriced.unpriced_class("VLTO", "2023-10-02") == "permanent: gateway gap"
    assert "VLTO" in unpriced.MEMBER_GAPS


def test_a_delivered_line_before_its_tail_is_tiingo_dependent() -> None:
    assert unpriced.unpriced_class("CMA", "2023-03-31") == "tiingo-dependent"
    assert unpriced.unpriced_class("CMA", "2024-06-24").startswith("permanent: exit tail")


def test_the_freshness_rule_is_the_price_vetoes_own() -> None:
    """A row within the age limit with a non-NULL close is usable; 8 days is not."""
    dates = ["2026-06-30"]
    assert unpriced.usable((dates, [10.0]), date(2026, 7, 7)) is True  # exactly 7 days
    assert unpriced.usable((dates, [10.0]), date(2026, 7, 8)) is False  # 8 days
    assert unpriced.usable((dates, [None]), date(2026, 7, 1)) is False  # a NULL close
    assert unpriced.usable(None, date(2026, 7, 1)) is False  # no row at all
    assert unpriced.MAX_OBSERVATION_AGE_DAYS == 7


def test_the_trigger_needs_a_bound_in_the_band_and_the_bounds_to_differ() -> None:
    """The WTW/BNY pair alone moves the share: in the band and equal is not a trigger."""
    assert unpriced.trigger([("2022-10-12", 0.0537, 0.0537)]) == []  # in the band, equal
    assert unpriced.trigger([("2022-01-03", 0.0733, 0.0693)]) == []  # differ, outside the band
    hits = unpriced.trigger([("2022-01-03", 0.0536, 0.0497)])
    assert hits == [("2022-01-03", 0.0536, 0.0497)]  # in the band and differ
    assert unpriced.BAND == (0.046, 0.054)
    assert unpriced.BAND_VERDICT == 0.05
