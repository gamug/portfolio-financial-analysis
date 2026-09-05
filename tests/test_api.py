"""The read-only FastAPI surface: health, runs, universe, coverage, scores, positions."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from portfolio_common.db import Database

from api.app import create_app
from api.config import ApiSettings
from fundamental_agent import db as fdb
from pricing_agent import db as pdb
from quant import db as qdb

_UDB = Callable[..., Path]


def _seed_fin(path: Path) -> None:
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    conn = Database(raw)
    conn.execute("PRAGMA foreign_keys = ON")
    fdb.ensure_schema(conn)  # assets, sectors, sec_filings, analysis_run + kg_schema
    pdb.ensure_schema(conn)  # price_* + pricing_run
    qdb.ensure_schema(conn)  # quant_run + (re-)builds kg_schema views

    aid = int(
        conn.execute("INSERT INTO assets (ticker) VALUES ('AAPL') RETURNING id").fetchone()["id"]
    )
    conn.execute(
        "INSERT INTO analysis_run (started_at, status, as_of, code_version, params_json) "
        "VALUES ('2024-07-01T00:00:00Z', 'completed', '2024-06-30', 'abc1234', '{}')"
    )
    conn.execute(
        "INSERT INTO score_snapshot (asset_id, score_type, raw_value, normalized_score, "
        "event_time, computed_at, model, run_kind) VALUES (?, 'FUNDAMENTAL', 71.0, 0.7, "
        "'2024-03-31', '2024-04-15T00:00:00Z', 'seed', 'analysis')",
        (aid,),
    )
    conn.execute(
        "INSERT INTO portfolio_position (asset_id, valid_from, valid_to, weight) "
        "VALUES (?, '2024-06-30', NULL, 0.05)",
        (aid,),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def client(tmp_path: Path, universe_db: _UDB) -> Iterator[TestClient]:
    fin = tmp_path / "fin.db"
    _seed_fin(fin)
    udb = universe_db([("AAPL", "2020-01-01", None), ("MSFT", "2020-01-01", None)])
    settings = ApiSettings(db_path=fin, universe_db_path=udb)
    with TestClient(create_app(settings)) as c:
        yield c


def test_health(client: TestClient) -> None:
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    r = client.get("/api/v1/health/db")
    body = r.json()
    assert r.status_code == 200
    assert body["ok"] is True
    assert body["universe_db_ok"] is True
    # None on a fresh DB (no `migrate` run), an int once migrations are recorded.
    assert body["schema_version"] is None or isinstance(body["schema_version"], int)


def test_root_redirects_to_docs(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"] == "/docs"


def test_runs(client: TestClient) -> None:
    r = client.get("/api/v1/runs")
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) == 1
    assert runs[0] == {
        "run_id": 1,
        "kind": "analysis",
        "as_of": "2024-06-30",
        "code_version": "abc1234",
        "status": "completed",
        "started_at": "2024-07-01T00:00:00Z",
        "finished_at": None,
    }
    assert client.get("/api/v1/runs?kind=quant").json() == []


def test_universe_as_of(client: TestClient) -> None:
    r = client.get("/api/v1/universe", params={"as_of": "2024-06-30"})
    assert r.status_code == 200
    assert [m["symbol"] for m in r.json()] == ["AAPL", "MSFT"]

    assert client.get("/api/v1/universe", params={"as_of": "nope"}).status_code == 422


def test_universe_coverage_computed(client: TestClient) -> None:
    r = client.get("/api/v1/universe/coverage", params={"as_of": "2024-06-30"})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "computed"
    assert body["total"] == 2
    by_sym = {row["symbol"]: row for row in body["rows"]}
    # AAPL has a FUNDAMENTAL score but no pricing/observations; MSFT has nothing.
    assert "pricing" in by_sym["AAPL"]["missing"]
    assert by_sym["MSFT"]["covered"] is False


def test_scores_and_positions(client: TestClient) -> None:
    scores = client.get("/api/v1/scores", params={"ticker": "aapl"}).json()
    assert len(scores) == 1
    assert scores[0]["score_type"] == "FUNDAMENTAL"
    assert scores[0]["ticker"] == "AAPL"

    positions = client.get("/api/v1/portfolio/positions").json()
    assert len(positions) == 1
    assert positions[0]["ticker"] == "AAPL"
    assert positions[0]["weight"] == 0.05
