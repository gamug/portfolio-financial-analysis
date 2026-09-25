"""``cycle``'s readers see only filings public by the cycle date (T-106).

Before T-106 they picked each asset's filing by ``period_end <= cycle_date`` (and FUNDAMENTAL
scores by ``event_time``, which is the period end), so a cycle on D read a filing whose period
had ended but which was not filed until weeks later -- 48.7 days on average for a 10-K, up to
420 in production. ``market_cap_estimates`` had no date filter at all.

Every stored value below encodes the id of the filing it came from, so each read can be traced
back to that filing's ``filing_date``.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from portfolio_common.db import Database

from cycle import data as cycle_data
from cycle.config import CycleSettings
from cycle.orchestrator import run_selection
from kg_schema import apply_migrations
from kg_schema.versions import DATA_QUALITY_GATE_VERSION, resolve_metric_versions
from quant.db import load_market_caps

# (filing id, asset, form, fiscal period, period end, filing date)
FILINGS = [
    (1, 1, "10-Q", "Q3 2025", "2025-09-30", "2025-11-01"),
    (2, 1, "10-K", "FY2025", "2025-12-31", "2026-02-20"),  # 51 days after its period end
    (3, 1, "10-Q", "Q1 2026", "2026-03-31", "2026-05-05"),
    (4, 2, "10-K", "FY2024", "2024-12-31", "2025-02-15"),
    (5, 2, "10-K", "FY2025", "2025-12-31", "2027-02-25"),  # a late filer: 421 days
]
FILED = {fid: filed for fid, *_rest, filed in FILINGS}


@pytest.fixture
def filed(memory_db: Database) -> Database:
    conn = memory_db
    apply_migrations(conn)
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA'), (2, 'BBB')")
    for fid, aid, form, fp, pe, fd in FILINGS:
        conn.execute(
            "INSERT INTO sec_filings (id, asset_id, form, fiscal_year, fiscal_period, "
            "period_end, filing_date, retrieved_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'now')",
            (fid, aid, form, int(pe[:4]), fp, pe, fd),
        )
        for group, name in (
            ("profitability", "net_margin"),
            ("valuation", "market_capitalization"),
        ):
            conn.execute(
                "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, "
                "unit, inputs_json, computed_at, engine_version, event_time) "
                "VALUES (?, ?, ?, ?, 'x', ?, 'now', 'metrics-v2', ?)",
                (fid, group, name, float(fid), json.dumps({name: float(fid)}), pe),
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
            "event_time, computed_at, model, run_kind, filing_id) "
            "VALUES (?, 'FUNDAMENTAL', ?, ?, ?, 'now', 'seed', 'analysis', ?)",
            (aid, float(fid), float(fid), pe, fid),
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


def test_a_filing_whose_period_ended_but_is_not_yet_filed_is_unreachable(filed: Database) -> None:
    """2026-01-15: AAA's FY2025 period has ended (2025-12-31) but the 10-K is filed on
    2026-02-20. The cycle reads the Q3 10-Q; the day after the filing, the 10-K."""
    before = _read_everything(filed, "2026-01-15")
    for reader, values in before.items():
        assert values[1] == 1.0, reader  # the Q3 10-Q, not the unfiled 10-K
    assert cycle_data.last_fundamental_dates(filed, "2026-01-15")[1] == "2025-09-30"

    after = _read_everything(filed, "2026-02-21")
    for reader, values in after.items():
        assert values[1] == 2.0, reader
    assert cycle_data.last_fundamental_dates(filed, "2026-02-21")[1] == "2025-12-31"


def test_a_filing_is_readable_on_its_filing_date(filed: Database) -> None:
    for reader, values in _read_everything(filed, "2026-02-20").items():
        assert values[1] == 2.0, reader


def test_a_late_filer_keeps_its_prior_filing_for_over_a_year(filed: Database) -> None:
    """BBB's FY2025 10-K lands 421 days after its period end (the production maximum is 420):
    every cycle until then reads FY2024."""
    for reader, values in _read_everything(filed, "2027-02-24").items():
        assert values[2] == 4.0, reader


def test_no_fact_filed_after_the_cycle_date_is_reachable(filed: Database) -> None:
    """The acceptance sweep: every day from before the first filing to after the last, every
    value every reader returns comes from a filing public on that day."""
    day, end = date(2025, 10, 1), date(2027, 3, 31)
    reads = 0
    while day <= end:
        iso = day.isoformat()
        for reader, values in _read_everything(filed, iso).items():
            for aid, fid in values.items():
                assert fid is not None
                assert FILED[int(fid)] <= iso, f"{reader} read filing {fid} for {aid} on {iso}"
                reads += 1
        day += timedelta(days=1)
    assert reads > 0


def test_nothing_is_reachable_before_an_asset_s_first_filing(filed: Database) -> None:
    for reader, values in _read_everything(filed, "2025-10-31").items():
        assert 1 not in values, reader  # AAA's first filing is public on 2025-11-01
        assert values[2] == 4.0, reader
    assert 1 not in cycle_data.last_fundamental_dates(filed, "2025-10-31")


def test_a_filing_with_no_filing_date_cannot_be_shown_public(filed: Database) -> None:
    filed.execute("UPDATE sec_filings SET filing_date = NULL WHERE id = 3")
    filed.commit()
    for reader, values in _read_everything(filed, "2026-09-30").items():
        assert values[1] == 2.0, reader  # the undated Q1 10-Q is skipped, not read as known


def test_a_fundamental_score_with_no_filing_is_not_read(filed: Database) -> None:
    filed.execute(
        "INSERT INTO score_snapshot (asset_id, score_type, raw_value, event_time, computed_at, "
        "model, run_kind) VALUES (1, 'FUNDAMENTAL', 99.0, '2026-06-30', 'now', 'seed', 'x')"
    )
    filed.commit()
    assert cycle_data.latest_fundamental_score(filed, "2026-09-30")[1] == 3.0


# -- a whole cycle ---------------------------------------------------------------------------


def _selection(conn: Database, day: str) -> None:
    run_selection(CycleSettings(db_path=Path(":memory:"), top_n=3), day, conn=conn)


def test_a_cycle_before_the_filings_are_public_sees_no_fundamentals(cycle_seed: Database) -> None:
    """``cycle_seed``'s FY2025 10-Ks end 2025-12-31 and are filed 2026-02-01. A cycle on
    2026-01-20 used to read them (their period had ended); now it normalizes no FUNDAMENTAL
    score, and EEE's debt-to-equity of 5.5 -- not yet public -- cannot trip
    LEVERAGE_EXTREME. From the filing date on, both are back."""
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

    _selection(conn, "2026-01-20")
    assert "FUNDAMENTAL" not in normalized("2026-01-20")
    assert "LEVERAGE_EXTREME" not in fired("2026-01-20")

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
        "filing_date, retrieved_at) VALUES (1, '10-K', 2024, 'FY2024', '2024-12-31', "
        "'2025-02-01', 'now') RETURNING id"
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO score_snapshot (asset_id, score_type, raw_value, normalized_score, "
        "event_time, computed_at, model, run_kind, filing_id) "
        "VALUES (1, 'FUNDAMENTAL', 72.0, NULL, '2024-12-31', 'now', 'seed', 'analysis', ?)",
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
