"""Persist cycle outputs: score_snapshot (TECH/QUANT), veto, cycle_ranking, positions."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from cycle.rules.base import VetoHit
from cycle.scores.sector import SectorAggregate


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def write_scores(  # noqa: PLR0913, PLR0917 - a wide row writer; splitting hurts clarity
    conn: sqlite3.Connection,
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
    conn: sqlite3.Connection, score_type: str, cycle_date: str, normalized: dict[int, float]
) -> None:
    conn.executemany(
        "UPDATE score_snapshot SET normalized_score = ? "
        "WHERE score_type = ? AND event_time = ? AND asset_id = ?",
        [(v, score_type, cycle_date, aid) for aid, v in normalized.items()],
    )
    conn.commit()


def write_sector_aggregates(
    conn: sqlite3.Connection,
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


def write_vetoes(
    conn: sqlite3.Connection, cycle_date: str, hits: list[VetoHit], *, run_id: int
) -> tuple[int, int]:
    """Insert new hits, clear ones whose condition no longer holds. Returns (opened, cleared)."""
    now = _now()
    active_keys = {(h.asset_id, h.rule_id) for h in hits}
    conn.executemany(
        """
        INSERT INTO veto (asset_id, rule_id, severity, detected_at, cycle_date, evidence_json, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (asset_id, rule_id, cycle_date) DO UPDATE SET
            evidence_json = excluded.evidence_json, cleared_at = NULL
        """,
        [
            (h.asset_id, h.rule_id, h.severity, now, cycle_date, json.dumps(h.evidence), run_id)
            for h in hits
        ],
    )
    # clear any still-open veto for this cycle_date that is not in the current hit set
    open_rows = conn.execute(
        "SELECT asset_id, rule_id FROM veto WHERE cycle_date = ? AND cleared_at IS NULL",
        (cycle_date,),
    ).fetchall()
    cleared = 0
    for r in open_rows:
        if (int(r["asset_id"]), str(r["rule_id"])) not in active_keys:
            conn.execute(
                "UPDATE veto SET cleared_at = ? WHERE cycle_date = ? AND asset_id = ? AND rule_id = ?",
                (now, cycle_date, r["asset_id"], r["rule_id"]),
            )
            cleared += 1
    conn.commit()
    return len(hits), cleared


def hard_vetoed_as_of(conn: sqlite3.Connection, cutoff_date: str) -> set[int]:
    """asset_ids carrying an uncleared HARD veto detected on or before *cutoff_date*
    -- the T-1 contagion filter (cycle on N reads cycle_date <= N-1)."""
    rows = conn.execute(
        """
        SELECT DISTINCT asset_id FROM veto
        WHERE severity = 'HARD' AND cleared_at IS NULL AND cycle_date <= ?
        """,
        (cutoff_date,),
    ).fetchall()
    return {int(r["asset_id"]) for r in rows}


def active_soft_vetoes(conn: sqlite3.Connection, cutoff_date: str) -> dict[int, list[str]]:
    rows = conn.execute(
        """
        SELECT asset_id, rule_id FROM veto
        WHERE severity = 'SOFT' AND cleared_at IS NULL AND cycle_date <= ?
        """,
        (cutoff_date,),
    ).fetchall()
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(int(r["asset_id"]), []).append(str(r["rule_id"]))
    return out


def write_ranking(
    conn: sqlite3.Connection, cycle_run_id: int, ranked: list[dict[str, object]]
) -> None:
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
    conn: sqlite3.Connection,
    cycle_date: str,
    targets: dict[int, float],
    closes: dict[int, float | None],
    *,
    cycle_run_id: int,
) -> tuple[int, int]:
    """Open positions for new targets, close vanished ones (history is immutable)."""
    open_rows = {
        int(r["asset_id"]): int(r["id"])
        for r in conn.execute("SELECT id, asset_id FROM portfolio_position WHERE valid_to IS NULL")
    }
    now_target = set(targets)
    opened = closed = 0
    for aid in set(open_rows) - now_target:
        conn.execute(
            "UPDATE portfolio_position SET valid_to = ? WHERE id = ?",
            (cycle_date, open_rows[aid]),
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
    # reweight incumbents that stay
    for aid in now_target & set(open_rows):
        conn.execute(
            "UPDATE portfolio_position SET weight = ? WHERE id = ?",
            (targets[aid], open_rows[aid]),
        )
    conn.commit()
    return opened, closed
