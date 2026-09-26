"""``available_at`` on filings, metrics and FUNDAMENTAL scores (T-107): written, guarded,
backfilled, and the only date an as-of reader filters fundamentals on."""

from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

import pytest
from portfolio_common.db import Database

from cycle.config import CycleSettings
from cycle.orchestrator import run_selection
from fundamental_agent import db
from fundamental_agent.db import FilingKey, FilingMeta, SnapshotRow
from fundamental_agent.metrics.base import MetricResult
from kg_schema import apply_migrations, availability, queries
from kg_schema.availability import AvailabilityMissing
from kg_schema.versions import resolve_metric_versions
from quant.db import load_market_caps

SRC = Path(__file__).resolve().parents[1] / "src"


def _snapshot(asset_id: int, filing_id: int) -> SnapshotRow:
    return SnapshotRow(
        asset_id=asset_id,
        filing_id=filing_id,
        form="10-K",
        fiscal_period="FY2025",
        score=70.0,
        rating="neutral",
        narrative="n",
        strengths=(),
        risks=(),
        model="rule-based",
        metrics={},
        event_time="2025-12-31",
    )


@pytest.fixture
def scored(memory_db: Database) -> tuple[Database, int]:
    """One 10-K filed Friday 2026-02-20, with a metric and a FUNDAMENTAL score, written by the
    agent's own writers."""
    conn = memory_db
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    fid = db.upsert_filing(
        conn,
        FilingKey(1, "10-K", 2025, "FY2025"),
        FilingMeta(filing_date="2026-02-20", accession_number="0001-26-1", period_end="2025-12-31"),
    )
    db.record_metrics(
        conn,
        fid,
        [("profitability", MetricResult("net_margin", 0.2, "ratio"))],
        event_time="2025-12-31",
    )
    db.insert_snapshot(conn, _snapshot(1, fid))
    return conn, fid


def _available(conn: Database) -> set[tuple[str, str | None]]:
    rows = [
        ("filing", conn.execute("SELECT available_at FROM sec_filings").fetchone()[0]),
        ("metric", conn.execute("SELECT available_at FROM fundamental_metrics").fetchone()[0]),
        (
            "score",
            conn.execute(
                "SELECT available_at FROM score_snapshot WHERE score_type = 'FUNDAMENTAL'"
            ).fetchone()[0],
        ),
    ]
    return set(rows)


# -- writers ----------------------------------------------------------------------------------


def test_the_writers_stamp_the_next_trading_day(scored: tuple[Database, int]) -> None:
    conn, _ = scored
    assert _available(conn) == {
        ("filing", "2026-02-23"),
        ("metric", "2026-02-23"),
        ("score", "2026-02-23"),
    }
    # event_time keeps its meaning: what the row is about
    assert conn.execute("SELECT event_time FROM score_snapshot").fetchone()[0] == "2025-12-31"


def test_a_re_dated_filing_carries_its_metrics_and_score_along(
    scored: tuple[Database, int],
) -> None:
    conn, _ = scored
    db.upsert_filing(
        conn,
        FilingKey(1, "10-K", 2025, "FY2025"),
        FilingMeta(filing_date="2026-02-24", accession_number="0001-26-1", period_end="2025-12-31"),
    )
    assert {v for _, v in _available(conn)} == {"2026-02-25"}


# -- guards -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filing_date", "available_at"),
    [
        ("2026-02-20", None),  # dated, but no available_at
        ("2026-02-20", "2026-02-20"),  # usable the day it was filed
        ("2026-02-20", "2026-02-19"),  # usable before it was filed
        ("2026-02-20", "2026-03-02"),  # more than a week later: not a next session
        (None, "2026-02-23"),  # usable without ever being filed
    ],
)
def test_a_filing_s_available_at_is_refused_unless_it_follows_its_filing_date(
    memory_db: Database, filing_date: str | None, available_at: str | None
) -> None:
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    with pytest.raises(sqlite3.IntegrityError, match="sec_filings: available_at"):
        memory_db.execute(
            "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, filing_date, "
            "available_at, retrieved_at) VALUES (1, '10-K', 2025, 'FY2025', ?, ?, 'now')",
            (filing_date, available_at),
        )


def test_an_undated_filing_is_accepted_without_available_at(memory_db: Database) -> None:
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    fid = db.upsert_filing(memory_db, FilingKey(1, "10-K", 2025, "FY2025"), FilingMeta())
    row = memory_db.execute("SELECT available_at FROM sec_filings WHERE id = ?", (fid,)).fetchone()
    assert row[0] is None


@pytest.mark.parametrize("available_at", [None, "2026-02-24"])
def test_a_metric_must_carry_its_filing_s_available_at(
    scored: tuple[Database, int], available_at: str | None
) -> None:
    conn, fid = scored
    with pytest.raises(sqlite3.IntegrityError, match="fundamental_metrics: available_at"):
        conn.execute(
            "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, "
            "computed_at, engine_version, available_at) "
            "VALUES (?, 'leverage', 'debt_to_equity', 1.0, 'now', 'metrics-v2', ?)",
            (fid, available_at),
        )
    with pytest.raises(sqlite3.IntegrityError, match="fundamental_metrics: available_at"):
        conn.execute("UPDATE fundamental_metrics SET available_at = ?", (available_at,))


@pytest.mark.parametrize("available_at", [None, "2026-02-24"])
def test_a_fundamental_score_must_carry_its_filing_s_available_at(
    scored: tuple[Database, int], available_at: str | None
) -> None:
    conn, fid = scored
    with pytest.raises(sqlite3.IntegrityError, match="score_snapshot: a FUNDAMENTAL"):
        conn.execute(
            "INSERT INTO score_snapshot (asset_id, score_type, raw_value, event_time, "
            "computed_at, filing_id, available_at) "
            "VALUES (1, 'FUNDAMENTAL', 1.0, '2024-12-31', 'now', ?, ?)",
            (fid, available_at),
        )


def test_other_score_types_need_no_available_at(scored: tuple[Database, int]) -> None:
    conn, _ = scored
    conn.execute(
        "INSERT INTO score_snapshot (asset_id, score_type, raw_value, event_time, computed_at) "
        "VALUES (1, 'TECHNICAL', 50.0, '2026-03-02', 'now')"
    )


def test_metrics_of_an_undated_filing_are_refused(memory_db: Database) -> None:
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    fid = db.upsert_filing(memory_db, FilingKey(1, "10-K", 2025, "FY2025"), FilingMeta())
    with pytest.raises(sqlite3.IntegrityError, match="fundamental_metrics: available_at"):
        db.record_metrics(memory_db, fid, [("profitability", MetricResult("net_margin", 0.2, "r"))])


# -- the backfill (m008) ----------------------------------------------------------------------


def _pre_t107(conn: Database) -> None:
    """Put *conn* back to how a database written before T-107 looks: no guards, no values,
    schema floor 7."""
    availability.drop_triggers(conn)
    conn.execute("UPDATE sec_filings SET available_at = NULL")
    conn.execute("UPDATE fundamental_metrics SET available_at = NULL")
    conn.execute("UPDATE score_snapshot SET available_at = NULL")
    conn.execute("DELETE FROM schema_version WHERE version >= 8")
    conn.commit()


def test_m008_backfills_every_row_and_restores_the_guards(scored: tuple[Database, int]) -> None:
    conn, _ = scored
    apply_migrations(conn)
    _pre_t107(conn)
    assert availability.missing(conn) == {
        "sec_filings": 1,
        "fundamental_metrics": 1,
        "score_snapshot": 1,
    }
    assert apply_migrations(conn) == [8]
    assert queries.current_version(conn) == 8
    assert {v for _, v in _available(conn)} == {"2026-02-23"}
    assert availability.missing(conn) == {}
    triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert set(availability.TRIGGER_NAMES) <= triggers


def test_m008_refuses_to_leave_a_metric_without_available_at(
    scored: tuple[Database, int],
) -> None:
    """A metric whose filing has no date would be skipped by every as-of reader: the migration
    refuses and rolls back rather than record the floor."""
    conn, _ = scored
    apply_migrations(conn)
    _pre_t107(conn)
    conn.execute("UPDATE sec_filings SET filing_date = NULL")
    conn.commit()
    with pytest.raises(AvailabilityMissing, match="fundamental_metrics: 1"):
        apply_migrations(conn)
    assert queries.current_version(conn) == 7


# -- the readers' guard -----------------------------------------------------------------------


def test_a_cycle_refuses_to_run_before_the_backfill(cycle_seed: Database) -> None:
    conn = cycle_seed
    _pre_t107(conn)
    with pytest.raises(AvailabilityMissing, match="fundamental_agent migrate"):
        run_selection(CycleSettings(db_path=Path(":memory:"), top_n=3), "2026-06-30", conn=conn)
    assert conn.execute("SELECT COUNT(*) FROM cycle_run").fetchone()[0] == 0


def test_quant_refuses_to_read_market_caps_before_the_backfill(cycle_seed: Database) -> None:
    conn = cycle_seed
    _pre_t107(conn)
    with pytest.raises(AvailabilityMissing):
        load_market_caps(conn, [1], resolve_metric_versions(conn), as_of="2026-06-30")


# -- no as-of reader keys fundamentals on anything else ----------------------------------------

_DATE_FILTER = r"\s*(?:<=|>=|<|>|BETWEEN)"
_EVENT_TIME_FILTER = re.compile(r"event_time" + _DATE_FILTER, re.IGNORECASE)
_FILING_DATE_FILTER = re.compile(r"filing_date" + _DATE_FILTER, re.IGNORECASE)
_PERIOD_END_FILTER = re.compile(r"period_end" + _DATE_FILTER, re.IGNORECASE)
# The as-of consumers: they read what the fundamental agent wrote, as of a date.
_AS_OF_PACKAGES = ("cycle", "quant", "api", "kg_schema")


def _sql_strings(path: Path) -> list[str]:
    """Every string literal in *path* (adjacent literals arrive joined), f-string parts
    included."""
    return [
        node.value
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_no_as_of_reader_filters_fundamental_scores_or_metrics_by_event_time() -> None:
    offenders = [
        f"{path.relative_to(SRC)}: {sql.strip()[:80]!r}"
        for path in sorted(SRC.rglob("*.py"))
        for sql in _sql_strings(path)
        if ("fundamental_metrics" in sql or "'FUNDAMENTAL'" in sql)
        and _EVENT_TIME_FILTER.search(sql)
    ]
    assert not offenders, offenders


def test_the_as_of_consumers_filter_filings_by_available_at_alone() -> None:
    """``filing_date`` and ``period_end`` may still be selected and ordered on, never compared
    to a date: the only as-of predicate on a filing is ``available_at``."""
    offenders = [
        f"{path.relative_to(SRC)}: {sql.strip()[:80]!r}"
        for package in _AS_OF_PACKAGES
        for path in sorted((SRC / package).rglob("*.py"))
        for sql in _sql_strings(path)
        if _FILING_DATE_FILTER.search(sql)
        or (_PERIOD_END_FILTER.search(sql) and "sec_filings" in sql)
    ]
    assert not offenders, offenders


def test_the_scan_sees_the_readers_it_guards() -> None:
    """Guard the guard: the known as-of readers are string literals the scan parses."""
    cycle_sql = _sql_strings(SRC / "cycle" / "data.py")
    assert sum("available_at <= ?" in s for s in cycle_sql) == 4
    quant_sql = _sql_strings(SRC / "quant" / "db.py")
    assert sum("available_at <= ?" in s for s in quant_sql) == 1
