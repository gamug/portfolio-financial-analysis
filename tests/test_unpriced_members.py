"""R1 (T-153 part 3c): the session rule, the two sides, the classes and the trigger.

The measurement itself is a real-data script; what is pinned here is the rule it applies, so a
re-run cannot silently change a bound, a class or the trigger's meaning.
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

from kg_schema.trading_calendar import last_session_on_or_before  # noqa: E402


def test_a_member_symbol_resolves_to_its_request_line() -> None:
    """DISCA and CDAY are the request's own aliases (its "Covers" column): WBD and DAY."""
    assert unpriced.line_of("DISCA") == "WBD"
    assert unpriced.line_of("CDAY") == "DAY"
    assert unpriced.line_of("wbd") == "WBD"
    assert unpriced.line_of("VLTO") == "VLTO"


def test_eligibility_is_the_latest_session_on_or_before_the_cycle_date() -> None:
    """Reviewer A's rule: a close on session(D), not within a window of it."""
    # a Monday holiday: MLK 2026-01-19 -> the Friday close is the session's close
    assert last_session_on_or_before(date(2026, 1, 19)) == date(2026, 1, 16)
    assert unpriced.usable_on_session((["2026-01-16"], [10.0]), "2026-01-16") is True
    # a member with D-1's close but not session(D)'s is unpriced
    assert unpriced.usable_on_session((["2026-07-02"], [10.0]), "2026-07-06") is False
    assert unpriced.usable_on_session((["2026-07-06"], [None]), "2026-07-06") is False
    assert unpriced.usable_on_session(None, "2026-07-06") is False


def test_the_price_vetoes_keep_their_seven_day_window() -> None:
    """Out of 3c's scope: a 7-day-old close is still a reading for the vetoes."""
    assert unpriced.usable((["2026-06-30"], [10.0]), date(2026, 7, 7)) is True  # exactly 7 days
    assert unpriced.usable((["2026-06-30"], [10.0]), date(2026, 7, 8)) is False  # 8 days
    assert unpriced.MAX_OBSERVATION_AGE_DAYS == 7


def test_after_the_import_a_delivered_line_reads_the_manifest_dates() -> None:
    """AVB is delivered through 2026-08-14 and 2026-08-17 is trimmed as 'carried'."""
    unpriced.MANIFEST_LAST.update({"AVB": "2026-08-14", "XLNX": "2022-02-11"})
    unpriced.MANIFEST_MISSING.update({"AVB": ["2026-08-17"], "XLNX": ["2022-02-14"]})
    assert unpriced.manifest_usable("AVB", "2026-08-14") is True
    assert unpriced.manifest_usable("AVB", "2026-08-17") is False
    assert unpriced.manifest_usable("AVB", "2026-08-18") is False  # past its last served session
    assert unpriced.manifest_usable("XLNX", "2022-02-14") is False
    assert unpriced.manifest_window_usable("AVB", "2026-08-17") is True  # 3 days: the 7-day rule
    assert unpriced.manifest_window_usable("AVB", "2026-08-24") is False  # 10 days


def test_info_wrk_and_a_non_request_gap_stay_unpriced_after_the_import() -> None:
    assert unpriced.unpriced_after_import("INFO", "2022-01-03") is True
    assert unpriced.unpriced_after_import("WRK", "2024-07-03") is True
    assert unpriced.unpriced_class("INFO", "2022-01-03").startswith("permanent")
    assert unpriced.unpriced_after_import("VLTO", "2023-10-02") is True
    assert unpriced.unpriced_class("VLTO", "2023-10-02") == "permanent: gateway gap"
    assert "VLTO" in unpriced.MEMBER_GAPS


def test_a_delivered_line_before_its_tail_is_tiingo_dependent() -> None:
    unpriced.MANIFEST_LAST.update({"CMA": "2024-06-21"})
    assert unpriced.unpriced_class("CMA", "2023-03-31") == "tiingo-dependent"
    assert unpriced.unpriced_class("CMA", "2024-06-24").startswith("permanent: exit tail")


def test_the_trigger_needs_a_bound_in_the_band_and_the_bounds_to_differ() -> None:
    """The WTW/BNY pair alone moves the share: in the band and equal is not a trigger."""
    assert unpriced.trigger([("2022-10-12", 0.0537, 0.0537)]) == []  # in the band, equal
    assert unpriced.trigger([("2022-01-03", 0.0733, 0.0693)]) == []  # differ, outside the band
    assert unpriced.trigger([("2022-01-03", 0.0536, 0.0497)]) == [("2022-01-03", 0.0536, 0.0497)]
    assert unpriced.BAND == (0.046, 0.054)
    assert unpriced.BAND_VERDICT == 0.05
