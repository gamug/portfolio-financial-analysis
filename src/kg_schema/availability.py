"""``available_at``: when a filing -- and every metric and FUNDAMENTAL score computed from it --
becomes usable (T-107).

``sec_filings.available_at`` is the first NYSE trading day after ``filing_date``
(:func:`kg_schema.trading_calendar.available_from`); ``fundamental_metrics`` and FUNDAMENTAL
``score_snapshot`` rows carry a copy of their filing's. ``event_time`` keeps its meaning (the
period end -- what the row is *about*), so ``UNIQUE(asset_id, score_type, event_time)`` and
every ``v_*`` reader stay as they were. Every as-of reader keys on ``available_at``.

:data:`kg_schema.ddl.AVAILABILITY_TRIGGERS` keeps it that way: a dated filing must carry its
day, a metric or FUNDAMENTAL score must carry its filing's (never NULL), and a re-dated filing
carries its rows along. Rows written before T-107 are filled by :func:`backfill` (migration
``m008``); until then :func:`require` refuses the as-of readers' callers, rather than let them
read nothing.
"""

from __future__ import annotations

from portfolio_common.db import Database

from .ddl import AVAILABILITY_TABLES, AVAILABILITY_TRIGGERS
from .trading_calendar import available_from

TRIGGER_NAMES = (
    "trg_sf_available_insert",
    "trg_sf_available_update",
    "trg_sf_available_cascade",
    "trg_fm_available_insert",
    "trg_fm_available_update",
    "trg_ss_available_insert",
    "trg_ss_available_update",
)


class AvailabilityMissing(RuntimeError):
    """Rows predate T-107's backfill: an as-of read would silently skip them."""


def _has(db: Database, table: str) -> bool:
    return db.relation_exists(table) and "available_at" in db.table_columns(table)


def _ready(db: Database) -> bool:
    return all(_has(db, t) for t in AVAILABILITY_TABLES)


def ensure_triggers(db: Database) -> None:
    """Create the T-107 guards, once all three tables carry ``available_at``."""
    if _ready(db):
        db.create_schema(AVAILABILITY_TRIGGERS)
        db.commit()


def drop_triggers(db: Database) -> None:
    """Drop the T-107 guards. ``apply_migrations`` does so before a table rebuild: the cascade
    trigger names ``fundamental_metrics`` and ``score_snapshot``, and SQLite refuses the
    rebuild's ``RENAME`` while a trigger names a table that is momentarily gone."""
    for name in TRIGGER_NAMES:
        db.execute(f"DROP TRIGGER IF EXISTS {name}")


def backfill(db: Database) -> dict[str, int]:
    """Fill every missing ``available_at``: each dated filing's from its ``filing_date``, then
    its metrics' and FUNDAMENTAL scores' from the filing's. Idempotent; never changes a value
    already set. Does not commit (``m008`` runs it inside the migration)."""
    if not _has(db, "sec_filings"):
        return {}
    filings = db.execute(
        "SELECT id, filing_date FROM sec_filings "
        "WHERE filing_date IS NOT NULL AND available_at IS NULL"
    ).fetchall()
    for r in filings:
        db.execute(
            "UPDATE sec_filings SET available_at = ? WHERE id = ?",
            (available_from(r["filing_date"]), int(r["id"])),
        )
    done = {"sec_filings": len(filings)}
    # The cascade trigger has copied onto existing rows already; this covers a database where
    # the triggers were not in place yet.
    if _has(db, "fundamental_metrics"):
        done["fundamental_metrics"] = db.execute(
            """
            UPDATE fundamental_metrics
            SET available_at = (SELECT f.available_at FROM sec_filings f
                                WHERE f.id = fundamental_metrics.filing_id)
            WHERE available_at IS NULL AND EXISTS (
                SELECT 1 FROM sec_filings f
                WHERE f.id = fundamental_metrics.filing_id AND f.available_at IS NOT NULL)
            """
        ).rowcount
    if _has(db, "score_snapshot"):
        done["score_snapshot"] = db.execute(
            """
            UPDATE score_snapshot
            SET available_at = (SELECT f.available_at FROM sec_filings f
                                WHERE f.id = score_snapshot.filing_id)
            WHERE score_type = 'FUNDAMENTAL' AND available_at IS NULL AND EXISTS (
                SELECT 1 FROM sec_filings f
                WHERE f.id = score_snapshot.filing_id AND f.available_at IS NOT NULL)
            """
        ).rowcount
    return done


# Rows that must carry `available_at` -- with the column, and without it (every such row is
# then missing one). Fully literal SQL (constitution Code & Git #10).
_MISSING_SQL = {
    "sec_filings": (
        "SELECT COUNT(*) FROM sec_filings WHERE filing_date IS NOT NULL AND available_at IS NULL",
        "SELECT COUNT(*) FROM sec_filings WHERE filing_date IS NOT NULL",
    ),
    "fundamental_metrics": (
        "SELECT COUNT(*) FROM fundamental_metrics WHERE available_at IS NULL",
        "SELECT COUNT(*) FROM fundamental_metrics",
    ),
    "score_snapshot": (
        "SELECT COUNT(*) FROM score_snapshot "
        "WHERE score_type = 'FUNDAMENTAL' AND available_at IS NULL",
        "SELECT COUNT(*) FROM score_snapshot WHERE score_type = 'FUNDAMENTAL'",
    ),
}


def missing(db: Database) -> dict[str, int]:
    """Rows with no ``available_at`` that must have one, per table (only non-zero counts): a
    dated filing, any metric, any FUNDAMENTAL score. A table that does not exist has none."""
    out: dict[str, int] = {}
    for table, (with_column, without_column) in _MISSING_SQL.items():
        if not db.relation_exists(table):
            continue
        sql = with_column if "available_at" in db.table_columns(table) else without_column
        n = int(db.execute(sql).fetchone()[0])
        if n:
            out[table] = n
    return out


def require(db: Database) -> None:
    """Raise :class:`AvailabilityMissing` unless every row an as-of reader keys on has its
    ``available_at``."""
    gaps = missing(db)
    if gaps:
        detail = ", ".join(f"{t}: {n}" for t, n in gaps.items())
        raise AvailabilityMissing(
            f"rows without available_at ({detail}); as-of reads would skip them -- run "
            "`python -m fundamental_agent migrate` to backfill (T-107)"
        )
