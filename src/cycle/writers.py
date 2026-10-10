"""Persist cycle outputs: score_snapshot (TECH/VALOR), veto, cycle_ranking, positions."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime

from portfolio_common.db import Database

from cycle.rules.base import VetoHit
from cycle.scores.sector import SectorAggregate
from kg_schema.queries import (
    active_soft_vetoes,
    hard_vetoed_as_of,
    require_veto_stint_columns,
    veto_out_of_order_reason,
)
from kg_schema.trading_calendar import add_trading_days

__all__ = [
    "OutOfOrderCycle",
    "active_soft_vetoes",
    "apply_normalized",
    "hard_vetoed_as_of",
    "out_of_order_reason",
    "restore_expiries_from",
    "sync_positions",
    "veto_out_of_order_reason",
    "write_ranking",
    "write_scores",
    "write_sector_aggregates",
    "write_vetoes",
]

# Weights closer than this are the same weight (float noise, not a re-weight).
WEIGHT_EPS = 1e-9


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


class OutOfOrderCycle(RuntimeError):
    """``select`` was asked to write the live ``portfolio_position`` book at a date older
    than a date it has already written (T-097)."""


def out_of_order_reason(conn: Database, cycle_date: str) -> str | None:
    """Why writing the live book at *cycle_date* would be an out-of-order (backdated) run --
    or ``None`` when it is safe (T-097).

    ``sync_positions`` always writes the live book unconditionally: it closes whatever is
    open (``valid_to = cycle_date``) and opens the new targets (``valid_from = cycle_date``),
    with no check that *cycle_date* is not older than a date it has already run at. A run for
    an earlier date, after a later one, silently closes positions early and backdates new
    ones -- found live (T-088's own validation run) rather than guessed at. Safe means:
    *cycle_date* is not older than the latest ``valid_from`` already recorded on
    ``portfolio_position`` -- read across *every* row (open or closed), because a closed
    position's ``valid_from`` still marks a date this book has already moved past. No rows at
    all (a fresh book) is always safe: there is nothing yet for any date to be "older" than.
    """
    row = conn.execute("SELECT MAX(valid_from) AS latest FROM portfolio_position").fetchone()
    latest = row["latest"] if row else None
    if latest is None or cycle_date >= str(latest):
        return None
    return (
        f"cycle_date {cycle_date} is older than the live book's latest valid_from {latest}; "
        "pass --allow-backdated for a deliberate historical backfill"
    )


def write_scores(  # noqa: PLR0913, PLR0917 - a wide row writer; splitting hurts clarity
    conn: Database,
    score_type: str,
    cycle_date: str,
    raw: dict[int, float],
    normalized: dict[int, float],
    components: dict[int, dict[str, float | None]],
    *,
    run_id: int,
    model: str = "deterministic",
) -> int:
    now = _now()
    rows = [
        (
            aid,
            score_type,
            raw[aid],
            normalized.get(aid),
            cycle_date,
            now,
            model,
            json.dumps(components.get(aid, {})),
            run_id,
        )
        for aid in raw
    ]
    conn.executemany(
        """
        INSERT INTO score_snapshot
            (asset_id, score_type, raw_value, normalized_score, event_time, computed_at,
             model, inputs_json, run_id, run_kind)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'cycle')
        ON CONFLICT (asset_id, score_type, event_time) DO UPDATE SET
            raw_value = excluded.raw_value, normalized_score = excluded.normalized_score,
            inputs_json = excluded.inputs_json, computed_at = excluded.computed_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def apply_normalized(
    conn: Database, score_type: str, cycle_date: str, normalized: dict[int, float]
) -> None:
    conn.executemany(
        "UPDATE score_snapshot SET normalized_score = ? "
        "WHERE score_type = ? AND event_time = ? AND asset_id = ?",
        [(v, score_type, cycle_date, aid) for aid, v in normalized.items()],
    )
    conn.commit()


def write_sector_aggregates(
    conn: Database,
    cycle_date: str,
    aggregates: list[SectorAggregate],
    *,
    run_id: int,
) -> int:
    """Upsert one ``sector_aggregate_snapshot`` row per sector for this cycle date."""
    now = _now()
    conn.executemany(
        """
        INSERT INTO sector_aggregate_snapshot
            (sector_id, cycle_date, metric_type, member_count, mean_raw, mean_normalized,
             computed_at, run_id)
        VALUES (?, ?, 'ScoreTecnico', ?, ?, ?, ?, ?)
        ON CONFLICT (sector_id, cycle_date, metric_type) DO UPDATE SET
            member_count = excluded.member_count, mean_raw = excluded.mean_raw,
            mean_normalized = excluded.mean_normalized, computed_at = excluded.computed_at
        """,
        [
            (a.sector_id, cycle_date, a.member_count, a.mean_raw, a.mean_normalized, now, run_id)
            for a in aggregates
        ],
    )
    conn.commit()
    return len(aggregates)


def hold_until(cycle_date: str, trading_days: int) -> str:
    """The cycle date *trading_days* NYSE sessions after *cycle_date* (a temporal veto's expiry)."""
    return add_trading_days(date.fromisoformat(cycle_date), trading_days).isoformat()


def restore_expiries_from(conn: Database, date_from: str) -> int:
    """Put back every ``veto.expires_on`` that an extension on or after *date_from* moved (T-070).

    A temporal stint's ``expiry_history_json`` holds one ``{"on", "was"}`` entry per extension:
    the cycle date it moved the expiry on, and the expiry it replaced. Dropping the entries dated
    ``>= date_from`` and restoring the ``was`` of the earliest of them puts ``expires_on`` back
    exactly where it stood before *date_from*. The caller commits. Returns the stints restored."""
    restored = 0
    for r in conn.execute(
        "SELECT id, expiry_history_json FROM veto WHERE expiry_history_json IS NOT NULL"
    ).fetchall():
        history = json.loads(r["expiry_history_json"])
        kept = [h for h in history if h["on"] < date_from]
        if len(kept) == len(history):
            continue
        was = next(h["was"] for h in history if h["on"] >= date_from)
        conn.execute(
            "UPDATE veto SET expires_on = ?, expiry_history_json = ? WHERE id = ?",
            (was, json.dumps(kept) if kept else None, int(r["id"])),
        )
        restored += 1
    return restored


def write_vetoes(  # noqa: PLR0913 - one wide writer; splitting hurts clarity
    conn: Database,
    cycle_date: str,
    hits: list[VetoHit],
    evaluated: dict[str, frozenset[int]],
    disabled_rule_ids: Iterable[str],
    *,
    run_id: int,
    hold_days: Mapping[str, int] | None = None,
) -> tuple[int, int]:
    """Apply this cycle's veto transitions as stints (T-125), not per-date rows.

    *evaluated* is rule_id -> the asset_ids that rule could actually resolve this cycle
    (hit or not) -- ``cycle.rules.base.RuleResult.evaluated``; an asset missing from its
    rule's evaluated set (missing data) is left untouched: its open stint, if any, stays
    open, and no new one opens. *disabled_rule_ids* (``cycle.rules.disabled_rule_ids``)
    close every open stint of a rule turned off in ``rule_catalog``, since a disabled rule
    is never asked to evaluate anything and so would otherwise never clear.

    Idempotent re-run (T-125 f): before applying anything, this *cycle_date*'s own prior
    transitions are undone -- stints it opened are deleted, stints it closed are reopened --
    so re-running the same date recomputes cleanly rather than compounding. A genuinely
    older *cycle_date* than the latest recorded transition is the caller's job to refuse
    (:func:`kg_schema.queries.veto_out_of_order_reason`) before this is ever called.

    *hold_days* maps a temporal rule's id to its hold in NYSE trading days (T-070). Such a stint
    opens with ``expires_on = cycle_date + hold`` and cannot clear before it, even when its
    condition is gone (it stays open, untouched). At the first cycle on or after ``expires_on``:
    the condition gone -> the stint clears; still holding -> it stays open and ``expires_on``
    moves to that cycle + hold (the move is recorded in ``expiry_history_json`` so a re-run of
    the date, or a replay reset, can put it back). A stint of any other rule never has an
    ``expires_on``.

    Raises :class:`kg_schema.queries.VetoSchemaStale` against a ``veto`` table that exists
    but predates m009's stint columns, rather than failing mid-write on a missing-column
    ``DatabaseError`` once production `migrate` (deliberately deferred by this same PR) is
    still pending (PR #103 review).

    Returns ``(opened, cleared)``.
    """
    require_veto_stint_columns(conn)
    now = _now()
    conn.execute("DELETE FROM veto WHERE raised_on = ?", (cycle_date,))
    conn.execute(
        "UPDATE veto SET cleared_on = NULL, cleared_at = NULL WHERE cleared_on = ?",
        (cycle_date,),
    )
    restore_expiries_from(conn, cycle_date)
    hold_days = hold_days or {}
    open_rows = {
        (int(r["asset_id"]), str(r["rule_id"])): r
        for r in conn.execute(
            "SELECT id, asset_id, rule_id, expires_on, expiry_history_json FROM veto "
            "WHERE cleared_on IS NULL"
        )
    }
    hit_by_key = {(h.asset_id, h.rule_id): h for h in hits}
    opened = cleared = 0
    for rule_id, asset_ids in evaluated.items():
        for aid in asset_ids:
            key = (aid, rule_id)
            hit = hit_by_key.get(key)
            existing = open_rows.get(key)
            hold = hold_days.get(rule_id)
            expires = existing["expires_on"] if existing is not None else None
            # A temporal stint is held until its expiry; one with no expiry recorded is expired.
            held = hold is not None and expires is not None and cycle_date < str(expires)
            if hit is not None:
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO veto (asset_id, rule_id, severity, raised_on, cleared_on,
                                          last_seen_on, detected_at, evidence_json, run_id,
                                          expires_on)
                        VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                        """,
                        (
                            aid,
                            rule_id,
                            hit.severity,
                            cycle_date,
                            cycle_date,
                            now,
                            json.dumps(hit.evidence),
                            run_id,
                            hold_until(cycle_date, hold) if hold is not None else None,
                        ),
                    )
                    opened += 1
                else:
                    conn.execute(
                        "UPDATE veto SET last_seen_on = ?, severity = ?, evidence_json = ?, "
                        "run_id = ? WHERE id = ?",
                        (
                            cycle_date,
                            hit.severity,
                            json.dumps(hit.evidence),
                            run_id,
                            int(existing["id"]),
                        ),
                    )
                    if hold is not None and not held:
                        history = json.loads(existing["expiry_history_json"] or "[]")
                        history.append({"on": cycle_date, "was": expires})
                        conn.execute(
                            "UPDATE veto SET expires_on = ?, expiry_history_json = ? WHERE id = ?",
                            (
                                hold_until(cycle_date, hold),
                                json.dumps(history),
                                int(existing["id"]),
                            ),
                        )
            elif existing is not None and not held:
                conn.execute(
                    "UPDATE veto SET cleared_on = ?, cleared_at = ? WHERE id = ?",
                    (cycle_date, now, int(existing["id"])),
                )
                cleared += 1
    for rule_id in disabled_rule_ids:
        for r in conn.execute(
            "SELECT id FROM veto WHERE rule_id = ? AND cleared_on IS NULL", (rule_id,)
        ).fetchall():
            conn.execute(
                "UPDATE veto SET cleared_on = ?, cleared_at = ? WHERE id = ?",
                (cycle_date, now, int(r["id"])),
            )
            cleared += 1
    conn.commit()
    return opened, cleared


def write_ranking(conn: Database, cycle_run_id: int, ranked: list[dict[str, object]]) -> None:
    conn.execute("DELETE FROM cycle_ranking WHERE cycle_run_id = ?", (cycle_run_id,))
    conn.executemany(
        """
        INSERT INTO cycle_ranking (cycle_run_id, asset_id, rank, blended_score, components_json,
                                   vetoed, veto_rules_json, selected, target_weight)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                cycle_run_id,
                r["asset_id"],
                r["rank"],
                r["blended_score"],
                json.dumps(r["components"]),
                int(bool(r["vetoed"])),
                json.dumps(r["veto_rules"]),
                int(bool(r["selected"])),
                r.get("target_weight"),
            )
            for r in ranked
        ],
    )
    conn.commit()


def sync_positions(
    conn: Database,
    cycle_date: str,
    targets: dict[int, float],
    closes: dict[int, float | None],
    *,
    cycle_run_id: int,
) -> tuple[int, int]:
    """Open positions for new targets, close vanished ones (history is immutable).

    A held name whose weight changes gets a new stint from *cycle_date* (the old one closes
    there) rather than an in-place update, so every weight the book ever held stays on record
    (T-104: the in-place update is what made a backdated run impossible to undo). Only a
    re-run on the stint's own start date updates it in place -- it is the same book, recomputed.
    """
    open_rows = {
        int(r["asset_id"]): r
        for r in conn.execute(
            "SELECT id, asset_id, valid_from, weight, cost_basis FROM portfolio_position "
            "WHERE valid_to IS NULL"
        )
    }
    now_target = set(targets)
    newer = sorted(
        str(r["valid_from"]) for r in open_rows.values() if str(r["valid_from"]) > cycle_date
    )
    if newer:
        # Even with --allow-backdated: closing or re-weighting these would end a stint before
        # it started (T-104) -- the database refuses that too, this just says why.
        raise OutOfOrderCycle(
            f"cycle_date {cycle_date} would end positions opened later ({newer[-1]}); a "
            "historical replay must not write the live book"
        )
    opened = closed = 0
    for aid in set(open_rows) - now_target:
        conn.execute(
            "UPDATE portfolio_position SET valid_to = ? WHERE id = ?",
            (cycle_date, int(open_rows[aid]["id"])),
        )
        closed += 1
    for aid in now_target - set(open_rows):
        conn.execute(
            """
            INSERT INTO portfolio_position
                (asset_id, valid_from, valid_to, weight, cost_basis, opened_by_cycle, run_id)
            VALUES (?, ?, NULL, ?, ?, ?, ?)
            """,
            (aid, cycle_date, targets[aid], closes.get(aid), cycle_run_id, cycle_run_id),
        )
        opened += 1
    # reweight incumbents that stay: a new stint, the old one closed (same-date re-run: in place)
    for aid in now_target & set(open_rows):
        row = open_rows[aid]
        if row["weight"] is not None and abs(float(row["weight"]) - targets[aid]) <= WEIGHT_EPS:
            continue
        if str(row["valid_from"]) == cycle_date:
            conn.execute(
                "UPDATE portfolio_position SET weight = ? WHERE id = ?", (targets[aid], row["id"])
            )
            continue
        conn.execute(
            "UPDATE portfolio_position SET valid_to = ? WHERE id = ?", (cycle_date, row["id"])
        )
        conn.execute(
            """
            INSERT INTO portfolio_position
                (asset_id, valid_from, valid_to, weight, cost_basis, opened_by_cycle, run_id)
            VALUES (?, ?, NULL, ?, ?, ?, ?)
            """,
            # the holding continues: its cost basis carries over, only the weight changes
            (aid, cycle_date, targets[aid], row["cost_basis"], cycle_run_id, cycle_run_id),
        )
    conn.commit()
    return opened, closed
