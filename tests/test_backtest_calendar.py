"""D1's calendar (BACKTEST_SPEC §0) and the driver's schedule helpers."""

from __future__ import annotations

from datetime import date
from itertools import pairwise

import pytest

from cycle.backtest.driver import (
    WINDOWS,
    Rebalance,
    next_session,
    previous_session,
    schedule_for,
    sessions_between,
    warmup_dates,
    window_spec,
)
from kg_schema import trading_calendar

# The D1 execution dates of the three windows, computed by hand from the NYSE calendar and
# fixed here so a calendar/rule change fails the build rather than silently moving a run.
EXPECTED_EXECUTIONS: dict[str, list[str]] = {
    "P1": [
        "2022-01-03",
        "2022-02-02",
        "2022-03-04",
        "2022-04-04",
        "2022-05-04",
        "2022-06-03",
        "2022-07-06",
        "2022-08-04",
        "2022-09-02",
        "2022-10-04",
    ],
    "P2": [
        "2025-02-19",
        "2025-03-20",
        "2025-04-21",
        "2025-05-20",
        "2025-06-20",
        "2025-07-22",
        "2025-08-20",
        "2025-09-19",
    ],
    "P3": [
        "2023-10-27",
        "2023-11-28",
        "2023-12-28",
        "2024-01-30",
        "2024-02-29",
        "2024-04-01",
        "2024-04-30",
        "2024-05-30",
        "2024-07-01",
        "2024-07-31",
    ],
    "H1": [
        "2022-10-13",
        "2022-11-11",
        "2022-12-13",
        "2023-01-13",
        "2023-02-14",
        "2023-03-16",
        "2023-04-17",
        "2023-05-16",
        "2023-06-15",
        "2023-07-18",
        "2023-08-16",
        "2023-09-15",
        "2023-10-16",
    ],
    "H2": [
        "2024-08-05",
        "2024-09-04",
        "2024-10-03",
        "2024-11-01",
        "2024-12-03",
        "2025-01-03",
        "2025-02-05",
    ],
    "H3": [
        "2025-09-24",
        "2025-10-23",
        "2025-11-21",
        "2025-12-23",
        "2026-01-26",
        "2026-02-25",
        "2026-03-26",
        "2026-04-27",
        "2026-05-27",
        "2026-06-26",
        "2026-07-28",
        "2026-08-26",
        "2026-09-25",
    ],
}


@pytest.mark.parametrize("window_id", sorted(WINDOWS))
def test_the_d1_execution_dates_are_exact(window_id: str) -> None:
    assert [r.execution_date for r in schedule_for(window_spec(window_id))] == (
        EXPECTED_EXECUTIONS[window_id]
    )


@pytest.mark.parametrize("window_id", sorted(WINDOWS))
def test_every_decision_is_the_previous_session_and_the_anchor_never_moves(window_id: str) -> None:
    window = window_spec(window_id)
    for index, rebalance in enumerate(schedule_for(window)):
        assert rebalance.decision_date == previous_session(rebalance.execution_date)
        assert (
            rebalance.execution_date
            == trading_calendar.add_trading_days(
                date.fromisoformat(window.d_start), 21 * index
            ).isoformat()
        )
        assert rebalance.execution_date < window.d_end


def test_p1_keeps_its_2021_decision_anchor() -> None:
    assert schedule_for(window_spec("P1"))[0] == Rebalance("2021-12-31", "2022-01-03")


def test_warmup_is_twelve_21_session_steps_before_the_first_decision() -> None:
    warmup = warmup_dates("2021-12-31", 12)
    assert len(warmup) == 12
    assert warmup == sorted(warmup)
    # each consecutive pair is exactly 21 sessions apart
    for earlier, later in pairwise(warmup):
        day = earlier
        for _ in range(21):
            day = next_session(day)
        assert day == later
    # and the last one precedes the first decision by one 21-session step
    day = warmup[-1]
    for _ in range(21):
        day = next_session(day)
    assert day == "2021-12-31"


def test_sessions_between_is_inclusive_and_refuses_a_non_session() -> None:
    sessions = sessions_between("2022-01-03", "2022-01-07")
    assert sessions == [
        "2022-01-03",
        "2022-01-04",
        "2022-01-05",
        "2022-01-06",
        "2022-01-07",
    ]
    with pytest.raises(Exception, match="not an NYSE session"):
        sessions_between("2022-01-08", "2022-01-10")


def test_next_session_skips_a_weekend() -> None:
    assert next_session("2022-01-07") == "2022-01-10"
