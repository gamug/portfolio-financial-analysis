#!/usr/bin/env python
"""Read-only check of a ``fundamental_agent score`` run against the database it scored (WP-F).

The ``score`` command writes a filing's FUNDAMENTAL row from the metric rows a run already stored.
This script compares the rows it wrote in *scored* with the FUNDAMENTAL rows the *reference*
database holds for the same ``(asset, event_time)``, and prints what agrees and what cannot:

* the flat metrics (``group.metric`` values) and the pillar *statuses* (``coverage`` and each
  pillar's ``status``/``metrics``/``empty_reason``/``weight``) must be identical -- they come from
  the stored metrics, not from the model;
* the ratings, the score and the narrative are the model's (or the fallback's) and are reported,
  never required to agree;
* ``filing_id`` and ``available_at`` must be the same row's.

It also reports, per cycle date, how many of ``members_asof``'s members have a FUNDAMENTAL score
available then -- what ``cycle``'s unscored brake reads (T-119).

Nothing is written: every connection is opened ``mode=ro``. Usage::

    python scripts/verify_score_command.py --scored COPY.db --reference ORIGINAL.db \
        [--universe-db universe.db] [--dates 2021-12-31,2023-10-26,2025-02-18]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kg_schema.pillar_score import COVERAGE_KEY, PILLARS_KEY
from kg_schema.queries import members_asof

# The pillar keys that come from the model (or the fallback), never from the stored metrics.
_RATING_KEYS = ("rating", "reason")


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def _score_rows(conn: sqlite3.Connection) -> dict[tuple[int, str], sqlite3.Row]:
    rows = _rows(
        conn,
        "SELECT asset_id, event_time, filing_id, available_at, model, raw_value, inputs_json "
        "FROM score_snapshot WHERE score_type = 'FUNDAMENTAL'",
    )
    return {(int(r["asset_id"]), str(r["event_time"])): r for r in rows}


def _inputs(row: sqlite3.Row) -> dict[str, Any]:
    try:
        block = json.loads(row["inputs_json"] or "{}")
    except ValueError:
        return {}
    return block if isinstance(block, dict) else {}


def _flat(block: dict[str, Any]) -> dict[str, Any]:
    """The flat metrics: every key that is not the pillar block's own."""
    reserved = {PILLARS_KEY, COVERAGE_KEY, "pillar_weights", "pillar_weights_version"}
    return {key: value for key, value in block.items() if key not in reserved}


def _pillar_state(block: dict[str, Any]) -> dict[str, Any]:
    """The pillars minus the model's ratings, plus ``coverage`` -- the metrics' own state."""
    pillars = block.get(PILLARS_KEY) or {}
    state = {
        pillar: {k: v for k, v in entry.items() if k not in _RATING_KEYS}
        for pillar, entry in pillars.items()
    }
    return {PILLARS_KEY: state, COVERAGE_KEY: block.get(COVERAGE_KEY)}


def _report_rows(scored: sqlite3.Connection, reference: sqlite3.Connection) -> None:
    mine = _score_rows(scored)
    theirs = _score_rows(reference)
    print(f"FUNDAMENTAL rows: scored={len(mine)}, reference={len(theirs)}")
    print(f"  scored models   : {dict(Counter(str(r['model']) for r in mine.values()))}")
    print(f"  reference models: {dict(Counter(str(r['model']) for r in theirs.values()))}")
    shared = sorted(set(mine) & set(theirs))
    print(f"  in both         : {len(shared)}")
    if not shared:
        return
    same_flat = same_state = same_meta = 0
    without_block = 0
    differing: list[str] = []
    for key in shared:
        a, b = mine[key], theirs[key]
        block_a, block_b = _inputs(a), _inputs(b)
        flat_ok = _flat(block_a) == _flat(block_b)
        has_block = PILLARS_KEY in block_b
        state_ok = _pillar_state(block_a) == _pillar_state(block_b) if has_block else None
        meta_ok = (a["filing_id"], a["available_at"]) == (b["filing_id"], b["available_at"])
        same_flat += flat_ok
        same_meta += meta_ok
        if not has_block:
            without_block += 1
        else:
            same_state += bool(state_ok)
        if not (flat_ok and meta_ok) or state_ok is False:
            differing.append(f"{key[0]} {key[1]}: flat={flat_ok} pillars={state_ok} meta={meta_ok}")
    print(f"  same flat metrics        : {same_flat}/{len(shared)}")
    print(f"  same pillar state        : {same_state}/{len(shared) - without_block}")
    print(f"  reference without pillars: {without_block} (written before T-073's block)")
    print(f"  same filing/available_at : {same_meta}/{len(shared)}")
    for line in differing[:20]:
        print(f"  ! {line}")
    scored_both = [key for key in shared if mine[key]["raw_value"] is not None]
    print(f"  rows carrying a score    : {len(scored_both)}/{len(shared)}")
    unscored = [key for key in shared if mine[key]["raw_value"] is None]
    print(f"  rows unscored (D1)       : {len(unscored)}")
    if unscored:
        reasons = Counter(
            f"{pillar}:{entry.get('empty_reason')}"
            for key in unscored
            for pillar, entry in (_inputs(mine[key]).get(PILLARS_KEY) or {}).items()
            if entry.get("status") == "empty"
        )
        print(f"  empty pillars            : {dict(sorted(reasons.items()))}")


def _report_coverage(conn: sqlite3.Connection, universe_db: Path, dates: Sequence[str]) -> None:
    """Per date, how many members have a FUNDAMENTAL score ``cycle`` could read then."""
    if not dates:
        return
    uconn = _connect(universe_db)
    try:
        for day in dates:
            members = members_asof(uconn, day)
            ciks = {m.cik for m in members if m.cik}
            names = {m.symbol.upper() for m in members}
            assets = {
                int(r["id"]): str(r["ticker"]).upper()
                for r in _rows(conn, "SELECT id, ticker FROM assets")
            }
            ids = [a for a, ticker in assets.items() if ticker in names]
            scored = _rows(
                conn,
                """
                SELECT DISTINCT s.asset_id FROM score_snapshot s
                WHERE s.score_type = 'FUNDAMENTAL' AND s.raw_value IS NOT NULL
                  AND s.available_at IS NOT NULL AND s.available_at <= ?
                  AND s.asset_id IN (SELECT id FROM assets)
                """,
                (day,),
            )
            scored_ids = {int(r["asset_id"]) for r in scored} & set(ids)
            with_any = _rows(
                conn,
                """
                SELECT DISTINCT s.asset_id FROM score_snapshot s
                WHERE s.score_type = 'FUNDAMENTAL'
                  AND s.available_at IS NOT NULL AND s.available_at <= ?
                """,
                (day,),
            )
            any_ids = {int(r["asset_id"]) for r in with_any} & set(ids)
            print(
                f"coverage {day}: {len(members)} members ({len(ciks)} CIKs), "
                f"{len(ids)} resolved to an asset, {len(any_ids)} with a FUNDAMENTAL row, "
                f"{len(scored_ids)} with a score"
            )
    finally:
        uconn.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scored", required=True, help="the database the score run wrote")
    parser.add_argument("--reference", required=True, help="the database it was scored from")
    parser.add_argument("--universe-db", help="universe.db, to report coverage per cycle date")
    parser.add_argument("--dates", help="comma-separated ISO dates for the coverage report")
    args = parser.parse_args(argv)

    scored = _connect(Path(args.scored))
    reference = _connect(Path(args.reference))
    try:
        _report_rows(scored, reference)
    finally:
        scored.close()
        reference.close()
    if args.universe_db:
        conn = _connect(Path(args.scored))
        try:
            dates = tuple(d.strip() for d in (args.dates or "").split(",") if d.strip())
            _report_coverage(conn, Path(args.universe_db), dates)
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
