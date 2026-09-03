"""``quant_run`` bookkeeping + the params-json secret scrubber.

Mirrors :mod:`cycle.state` deliberately -- importing ``cycle`` here would pull the
optimizer's numeric dependencies onto ``cycle``'s import path (see
``tests/test_quant_import_isolation``), so the ~15 lines are copied instead.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

# Field names whose values must never reach quant_run.params_json in the shared
# KG_FINANCIAL_DB that other repos read.
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


QUANT_ENGINE_VERSION = "quant-v1"


def open_run(
    conn: sqlite3.Connection,
    command: str,
    *,
    as_of: str | None = None,
    params: dict[str, Any] | None = None,
    code_version: str | None = None,
) -> int:
    """Insert a ``running`` ``quant_run`` row and return its id."""
    cur = conn.execute(
        """
        INSERT INTO quant_run
            (command, as_of, started_at, status, engine_version, params_json, code_version)
        VALUES (?, ?, ?, 'running', ?, ?, ?)
        """,
        (
            command,
            as_of,
            _now(),
            QUANT_ENGINE_VERSION,
            json.dumps(_redact(params or {}), default=str),
            code_version,
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str = "completed") -> None:
    conn.execute(
        "UPDATE quant_run SET status = ?, finished_at = ? WHERE id = ?",
        (status, _now(), run_id),
    )
    conn.commit()


def fail_run(conn: sqlite3.Connection, run_id: int, error: str) -> None:
    conn.execute(
        "UPDATE quant_run SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
        (_now(), error[:2000], run_id),
    )
    conn.commit()
