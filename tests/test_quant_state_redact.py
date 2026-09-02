"""quant.state._redact masks secrets before they reach quant_run.params_json."""

from __future__ import annotations

import json
import sqlite3

from quant.state import _redact, fail_run, finish_run, open_run


def test_redact_masks_nested_secret_keys() -> None:
    raw = {
        "db_path": "/x/financial.db",
        "llm_api_key": "sk-secret",
        "nested": {"Authorization_Token": "abc", "keep": 1},
        "list": [{"password": "p"}, {"plain": "ok"}],
    }
    out = _redact(raw)
    assert out["db_path"] == "/x/financial.db"
    assert out["llm_api_key"] == "***REDACTED***"
    assert out["nested"]["Authorization_Token"] == "***REDACTED***"
    assert out["nested"]["keep"] == 1
    assert out["list"][0]["password"] == "***REDACTED***"
    assert out["list"][1]["plain"] == "ok"


def test_open_finish_fail_run_roundtrip() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE quant_run (id INTEGER PRIMARY KEY, command TEXT NOT NULL, as_of TEXT, "
        "started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, "
        "engine_version TEXT NOT NULL, params_json TEXT, error TEXT)"
    )
    rid = open_run(conn, "optimize", as_of="2026-08-27", params={"token": "t", "k": 3})
    row = conn.execute("SELECT * FROM quant_run WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "running"
    assert json.loads(row["params_json"])["token"] == "***REDACTED***"
    assert json.loads(row["params_json"])["k"] == 3

    finish_run(conn, rid)
    assert conn.execute("SELECT status FROM quant_run WHERE id = ?", (rid,)).fetchone()[0] == (
        "completed"
    )

    rid2 = open_run(conn, "optimize")
    fail_run(conn, rid2, "boom")
    r2 = conn.execute("SELECT status, error FROM quant_run WHERE id = ?", (rid2,)).fetchone()
    assert r2["status"] == "failed"
    assert r2["error"] == "boom"
