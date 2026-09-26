"""``cycle``'s readers see only filings usable on the cycle date (T-106, T-107).

Before T-106 they picked each asset's filing by ``period_end <= cycle_date`` (and FUNDAMENTAL
scores by ``event_time``, which is the period end), so a cycle on D read a filing whose period
had ended but which was not filed until weeks later -- 48.7 days on average for a 10-K, up to
420 in production. ``market_cap_estimates`` had no date filter at all. T-106 keyed them on
``filing_date <= cycle_date``; T-107 on ``available_at``, the first NYSE trading day *after*
the filing date -- EDGAR dates an after-close submission with the same day, so a filing is not
safely known until the next session.

Every stored value below encodes the id of the filing it came from, so each read can be traced
back to that filing's ``filing_date`` and ``available_at``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest
from portfolio_common.db import Database

from cycle import data as cycle_data
from cycle.config import CycleSettings
from cycle.orchestrator import run_selection
from kg_schema import apply_migrations
from kg_schema.trading_calendar import available_from
from kg_schema.versions import DATA_QUALITY_GATE_VERSION, resolve_metric_versions
from quant.db import load_market_caps

# (filing id, asset, form, fiscal period, period end, filing date)
FILINGS = [
    (1, 1, "10-Q", "Q3 2025", "2025-09-30", "2025-10-31"),  # a Friday: usable Monday 11-03
    (2, 1, "10-K", "FY2025", "2025-12-31", "2026-02-20"),  # 51 days after its period end (Fri)
    (3, 1, "10-Q", "Q1 2026", "2026-03-31", "2026-05-05"),
    (4, 2, "10-K", "FY2024", "2024-12-31", "2025-02-14"),  # Fri before Presidents' Day: 02-18
    (5, 2, "10-K", "FY2025", "2025-12-31", "2027-02-25"),  # a late filer: 421 days
]
FILED = {fid: filed for fid, *_rest, filed in FILINGS}
AVAILABLE = {fid: str(available_from(filed)) for fid, filed in FILED.items()}


@pytest.fixture
def filed(memory_db: Database) -> Database:
    conn = memory_db
    apply_migrations(conn)
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA'), (2, 'BBB')")
    for fid, aid, form, fp, pe, fd in FILINGS:
        conn.execute(
            "INSERT INTO sec_filings (id, asset_id, form, fiscal_year, fiscal_period, "
            "period_end, filing_date, available_at, retrieved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'now')",
            (fid, aid, form, int(pe[:4]), fp, pe, fd, AVAILABLE[fid]),
        )
        for group, name in (
            ("profitability", "net_margin"),
            ("valuation", "market_capitalization"),
        ):
            conn.execute(
                "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, "
                "unit, inputs_json, computed_at, engine_version, event_time, available_at) "
                "VALUES (?, ?, ?, ?, 'x', ?, 'now', 'metrics-v2', ?, ?)",
                (fid, group, name, float(fid), json.dumps({name: float(fid)}), pe, AVAILABLE[fid]),
            )
        conn.execute(
            "INSERT INTO data_quality_issue (filing_id, asset_id, metric_group, metric_name, "
            "metric_engine_version, rule_id, gate_version, severity, quarantined, value, "
            "created_at) VALUES (?, ?, 'profitability', 'net_margin', 'metrics-v2', 'DQ_TEST', "
            "?, 'HARD', 1, ?, 'now')",
            (fid, aid, DATA_QUALITY_GATE_VERSION, float(fid)),
        )
        conn.execute(
            "INSERT INTO score_snapshot (asset_id, score_type, raw_value, normalized_score, "
            "event_time, computed_at, model, run_kind, filing_id, available_at) "
            "VALUES (?, 'FUNDAMENTAL', ?, ?, ?, 'now', 'seed', 'analysis', ?, ?)",
            (aid, float(fid), float(fid), pe, fid, AVAILABLE[fid]),
        )
    conn.commit()
    return conn


def _read_everything(conn: Database, day: str) -> dict[str, dict[int, float | None]]:
    """Every fundamental reader ``cycle`` (and ``quant``'s market cap) uses, as asset -> the
    filing id its value came from."""
    versions = resolve_metric_versions(conn)
    metrics = cycle_data.latest_metrics(conn, day, versions)
    dq = cycle_data.data_quality(conn, day, versions)
    return {
        "latest_metrics": {a: m["profitability.net_margin"] for a, m in metrics.items()},
        "market_cap": {
            a: v
            for a, v in cycle_data.market_cap_estimates(conn, day, metrics, versions).items()
            if v is not None
        },
        "quant_market_cap": {
            a: v for a, v in load_market_caps(conn, [1, 2], versions, as_of=day).items()
        },
        "data_quality": {a: issues[0]["value"] for a, issues in dq.hard.items()},
        "fundamental_score": cycle_data.latest_fundamental_score(conn, day),
    }


def test_the_fixture_s_filings_become_usable_on_the_next_trading_day() -> None:
    assert AVAILABLE == {
        1: "2025-11-03",  # Friday -> Monday
        2: "2026-02-23",  # Friday -> Monday
        3: "2026-05-06",  # Tuesday -> Wednesday
        4: "2025-02-18",  # Friday -> Tuesday: Monday 02-17 is Presidents' Day
        5: "2027-02-26",
    }


def test_a_filing_whose_period_ended_but_is_not_yet_filed_is_unreachable(filed: Database) -> None:
    """2026-01-15: AAA's FY2025 period has ended (2025-12-31) but the 10-K is filed on
    2026-02-20. The cycle reads the Q3 10-Q; from the next session on, the 10-K."""
    before = _read_everything(filed, "2026-01-15")
    for reader, values in before.items():
        assert values[1] == 1.0, reader  # the Q3 10-Q, not the unfiled 10-K
    assert cycle_data.last_fundamental_dates(filed, "2026-01-15")[1] == "2025-09-30"

    after = _read_everything(filed, "2026-02-23")
    for reader, values in after.items():
        assert values[1] == 2.0, reader
    assert cycle_data.last_fundamental_dates(filed, "2026-02-23")[1] == "2025-12-31"


def test_a_filing_is_not_usable_on_its_filing_date_nor_before_the_next_session(
    filed: Database,
) -> None:
    """Filed Friday 2026-02-20 (possibly after the close): not on Friday, not over the
    weekend -- from Monday's session."""
    for day in ("2026-02-20", "2026-02-21", "2026-02-22"):
        for reader, values in _read_everything(filed, day).items():
            assert values[1] == 1.0, (reader, day)
    for reader, values in _read_everything(filed, "2026-02-23").items():
        assert values[1] == 2.0, reader


def test_an_exchange_holiday_is_skipped(filed: Database) -> None:
    """BBB's FY2024 10-K is filed Friday 2025-02-14; Monday 02-17 is Presidents' Day, so the
    first session to know it is Tuesday."""
    for reader, values in _read_everything(filed, "2025-02-17").items():
        assert 2 not in values, reader
    for reader, values in _read_everything(filed, "2025-02-18").items():
        assert values[2] == 4.0, reader


def test_a_late_filer_keeps_its_prior_filing_for_over_a_year(filed: Database) -> None:
    """BBB's FY2025 10-K lands 421 days after its period end (the production maximum is 420):
    every cycle until the session after it reads FY2024."""
    for reader, values in _read_everything(filed, "2027-02-25").items():
        assert values[2] == 4.0, reader
    for reader, values in _read_everything(filed, "2027-02-26").items():
        assert values[2] == 5.0, reader


def test_no_fact_filed_on_or_after_the_cycle_date_is_reachable(filed: Database) -> None:
    """The acceptance sweep: every day from before the first filing to after the last, every
    value every reader returns comes from a filing usable that day -- filed strictly before it,
    and on or after the session following its filing date."""
    day, end = date(2025, 1, 1), date(2027, 3, 31)
    reads = 0
    while day <= end:
        iso = day.isoformat()
        for reader, values in _read_everything(filed, iso).items():
            for aid, fid in values.items():
                assert fid is not None
                assert FILED[int(fid)] < iso, f"{reader} read filing {fid} for {aid} on {iso}"
                assert AVAILABLE[int(fid)] <= iso, f"{reader} read {fid} early on {iso}"
                reads += 1
        day += timedelta(days=1)
    assert reads > 0


def test_nothing_is_reachable_before_an_asset_s_first_filing(filed: Database) -> None:
    for reader, values in _read_everything(filed, "2025-11-02").items():
        assert 1 not in values, reader  # AAA's first filing is usable on 2025-11-03
        assert values[2] == 4.0, reader
    assert 1 not in cycle_data.last_fundamental_dates(filed, "2025-11-02")


def test_a_filing_with_metrics_cannot_lose_its_date(filed: Database) -> None:
    """An undated filing is never usable, so nothing computed from one may be stored: the
    database refuses to undate a filing that has metrics and a score (T-107)."""
    with pytest.raises(sqlite3.IntegrityError, match="fundamental_metrics"):
        filed.execute("UPDATE sec_filings SET filing_date = NULL, available_at = NULL WHERE id = 3")


def test_a_fundamental_score_with_no_filing_is_refused(filed: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="score_snapshot"):
        filed.execute(
            "INSERT INTO score_snapshot (asset_id, score_type, raw_value, event_time, "
            "computed_at, model, run_kind, available_at) "
            "VALUES (1, 'FUNDAMENTAL', 99.0, '2026-06-30', 'now', 'seed', 'x', '2026-07-01')"
        )


# -- a whole cycle ---------------------------------------------------------------------------


def _selection(conn: Database, day: str) -> None:
    run_selection(CycleSettings(db_path=Path(":memory:"), top_n=3), day, conn=conn)


def test_a_cycle_before_the_filings_are_public_sees_no_fundamentals(cycle_seed: Database) -> None:
    """``cycle_seed``'s FY2025 10-Ks end 2025-12-31 and are filed Friday 2026-01-30. A cycle
    on 2026-01-20 used to read them (their period had ended); now it normalizes no FUNDAMENTAL
    score, and EEE's debt-to-equity of 5.5 -- not yet public -- cannot trip
    LEVERAGE_EXTREME; nor can a cycle on the filing date itself (T-107). From the next
    session, Monday 2026-02-02, both are back."""
    conn = cycle_seed

    def normalized(day: str) -> dict[str, int]:
        row = conn.execute(
            "SELECT c.detail_json FROM cycle_checkpoint c JOIN cycle_run r "
            "ON r.id = c.cycle_run_id WHERE r.cycle_date = ? AND c.step = 'normalize'",
            (day,),
        ).fetchone()
        return dict(json.loads(row["detail_json"]))

    def fired(day: str) -> set[str]:
        return {
            r["rule_id"]
            for r in conn.execute("SELECT rule_id FROM veto WHERE cycle_date = ?", (day,))
        }

    for day in ("2026-01-20", "2026-01-30"):
        _selection(conn, day)
        assert "FUNDAMENTAL" not in normalized(day)
        assert "LEVERAGE_EXTREME" not in fired(day)

    _selection(conn, "2026-02-02")
    assert normalized("2026-02-02")["FUNDAMENTAL"] == 5
    assert "LEVERAGE_EXTREME" in fired("2026-02-02")


def test_fundamental_normalization_lands_on_the_read_snapshot_alone(
    cycle_seed: Database,
) -> None:
    """The normalize step used to update every FUNDAMENTAL row of the asset sharing the raw
    score; an older snapshot with the same raw value was overwritten too."""
    conn = cycle_seed
    old = conn.execute(
        "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, period_end, "
        "filing_date, available_at, retrieved_at) VALUES (1, '10-K', 2024, 'FY2024', "
        "'2024-12-31', '2025-01-31', '2025-02-03', 'now') RETURNING id"
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO score_snapshot (asset_id, score_type, raw_value, normalized_score, "
        "event_time, computed_at, model, run_kind, filing_id, available_at) VALUES "
        "(1, 'FUNDAMENTAL', 72.0, NULL, '2024-12-31', 'now', 'seed', 'analysis', ?, '2025-02-03')",
        (old,),
    )
    conn.commit()
    _selection(conn, "2026-06-30")
    rows = dict(
        conn.execute(
            "SELECT event_time, normalized_score FROM score_snapshot "
            "WHERE asset_id = 1 AND score_type = 'FUNDAMENTAL'"
        ).fetchall()
    )
    assert rows["2024-12-31"] is None  # untouched
    assert rows["2025-12-31"] is not None
