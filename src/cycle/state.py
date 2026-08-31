"""``cycle_run`` / ``cycle_checkpoint`` bookkeeping.

This -- not the orchestration framework -- is the source of truth for resumability:
a step whose checkpoint is ``done`` for a given ``cycle_run`` is skipped on re-run.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

# Field names whose values must never reach params_json / detail_json. The cycle
# settings carry LLM_API_KEY, and json.dumps would otherwise persist it in the
# shared KG_FINANCIAL_DB that other repos read.
_SECRET_KEY_RE = re.compile(r"api[_-]?key|secret|token|password|passwd|credential", re.IGNORECASE)
_REDACTED = "***REDACTED***"


def _redact(value: Any) -> Any:
    """Deep-copy *value*, masking any dict entry whose key looks like a secret."""
    if isinstance(value, dict):
        return {
            key: (_REDACTED if val is not None and _SECRET_KEY_RE.search(key) else _redact(val))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def open_cycle(
    conn: sqlite3.Connection, cycle_type: str, cycle_date: str, params: dict[str, Any]
) -> int:
    """Create or resume the ``cycle_run`` row for ``(cycle_type, cycle_date)``."""
    conn.execute(
        """
        INSERT INTO cycle_run (cycle_type, cycle_date, started_at, status, params_json)
        VALUES (?, ?, ?, 'running', ?)
        ON CONFLICT (cycle_type, cycle_date) DO UPDATE SET status = 'running'
        """,
        (cycle_type, cycle_date, _now(), json.dumps(_redact(params), default=str)),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM cycle_run WHERE cycle_type = ? AND cycle_date = ?",
        (cycle_type, cycle_date),
    ).fetchone()
    return int(row["id"])


def checkpoint(
    conn: sqlite3.Connection,
    cycle_run_id: int,
    step: str,
    status: str,
    detail: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO cycle_checkpoint (cycle_run_id, step, status, detail_json, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (cycle_run_id, step) DO UPDATE SET
            status = excluded.status, detail_json = excluded.detail_json,
            updated_at = excluded.updated_at
        """,
        (cycle_run_id, step, status, json.dumps(_redact(detail or {}), default=str), _now()),
    )
    conn.commit()


def done_steps(conn: sqlite3.Connection, cycle_run_id: int) -> set[str]:
    return {
        str(r["step"])
        for r in conn.execute(
            "SELECT step FROM cycle_checkpoint WHERE cycle_run_id = ? AND status = 'done'",
            (cycle_run_id,),
        )
    }


def finish_cycle(conn: sqlite3.Connection, cycle_run_id: int, status: str) -> None:
    conn.execute(
        "UPDATE cycle_run SET status = ?, finished_at = ? WHERE id = ?",
        (status, _now(), cycle_run_id),
    )
    conn.commit()
