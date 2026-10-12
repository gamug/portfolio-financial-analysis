"""The read-only FastAPI surface: health, runs, universe, coverage, scores, positions."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from conftest import seed_fundamental_score
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
    seed_fundamental_score(conn, aid, 71.0, event_time="2024-03-31", normalized_score=0.7)
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


def test_scores_as_of_reads_availability_not_period_end(client: TestClient) -> None:
    # The seeded FY score's period ends 2024-03-31 but its filing is only available ~45 days
    # later: as of 2024-04-30 it was not yet known (constitution AI behavior #4, no lookahead).
    before = client.get("/api/v1/scores", params={"ticker": "AAPL", "as_of": "2024-04-30"})
    assert before.json() == []
    after = client.get("/api/v1/scores", params={"ticker": "AAPL", "as_of": "2024-06-30"})
    assert len(after.json()) == 1


def test_positions_as_of_is_honoured_without_open_only(client: TestClient) -> None:
    # The only stint opens 2024-06-30; an earlier as_of must not return it, even with the
    # default open_only=true.
    assert client.get("/api/v1/portfolio/positions", params={"as_of": "2024-01-01"}).json() == []
    held = client.get("/api/v1/portfolio/positions", params={"as_of": "2024-07-01"}).json()
    assert [p["ticker"] for p in held] == ["AAPL"]


def test_cycle_ranking_returns_only_most_recent_completed_run(
    tmp_path: Path, universe_db: _UDB
) -> None:
    fin = tmp_path / "fin_ranking.db"
    _seed_fin(fin)
    udb = universe_db([("AAPL", "2020-01-01", None), ("MSFT", "2020-01-01", None)])
    settings = ApiSettings(db_path=fin, universe_db_path=udb)

    raw = sqlite3.connect(fin)
    raw.row_factory = sqlite3.Row
    conn = Database(raw)
    aid = int(conn.execute("SELECT id FROM assets WHERE ticker = 'AAPL'").fetchone()["id"])

    # Older completed run
    r1 = int(
        conn.execute(
            "INSERT INTO cycle_run (cycle_type, cycle_date, started_at, finished_at, status, params_json, code_version) "
            "VALUES ('SELECTION', '2024-06-01', '2024-06-01T00:00:00Z', '2024-06-01T00:05:00Z', 'completed', '{}', 'abc') "
            "RETURNING id"
        ).fetchone()["id"]
    )
    # Newer failed run
    r2 = int(
        conn.execute(
            "INSERT INTO cycle_run (cycle_type, cycle_date, started_at, finished_at, status, params_json, code_version) "
            "VALUES ('SELECTION', '2024-07-01', '2024-07-01T00:00:00Z', '2024-07-01T00:05:00Z', 'failed', '{}', 'abc') "
            "RETURNING id"
        ).fetchone()["id"]
    )
    # Even newer running run
    r3 = int(
        conn.execute(
            "INSERT INTO cycle_run (cycle_type, cycle_date, started_at, finished_at, status, params_json, code_version) "
            "VALUES ('SELECTION', '2024-08-01', '2024-08-01T00:00:00Z', NULL, 'running', '{}', 'abc') "
            "RETURNING id"
        ).fetchone()["id"]
    )
    # Failed MONITORING run with ranking row (verifies failed run is ignored when no completed run exists)
    r4 = int(
        conn.execute(
            "INSERT INTO cycle_run (cycle_type, cycle_date, started_at, finished_at, status, params_json, code_version) "
            "VALUES ('MONITORING', '2024-09-01', '2024-09-01T00:00:00Z', '2024-09-01T00:05:00Z', 'failed', '{}', 'abc') "
            "RETURNING id"
        ).fetchone()["id"]
    )

    conn.execute(
        "INSERT INTO cycle_ranking (cycle_run_id, asset_id, rank, blended_score, components_json, vetoed, veto_rules_json, selected) "
        "VALUES (?, ?, 1, 80.0, '{}', 0, '[]', 1)",
        (r1, aid),
    )
    conn.execute(
        "INSERT INTO cycle_ranking (cycle_run_id, asset_id, rank, blended_score, components_json, vetoed, veto_rules_json, selected) "
        "VALUES (?, ?, 1, 90.0, '{}', 0, '[]', 1)",
        (r2, aid),
    )
    conn.execute(
        "INSERT INTO cycle_ranking (cycle_run_id, asset_id, rank, blended_score, components_json, vetoed, veto_rules_json, selected) "
        "VALUES (?, ?, 1, 95.0, '{}', 0, '[]', 1)",
        (r3, aid),
    )
    conn.execute(
        "INSERT INTO cycle_ranking (cycle_run_id, asset_id, rank, blended_score, components_json, vetoed, veto_rules_json, selected) "
        "VALUES (?, ?, 1, 70.0, '{}', 0, '[]', 1)",
        (r4, aid),
    )
    conn.commit()
    conn.close()

    with TestClient(create_app(settings)) as c:
        res = c.get("/api/v1/portfolio/ranking").json()
        assert len(res) == 1
        assert res[0]["cycle_date"] == "2024-06-01"
        assert res[0]["status"] == "completed"
        assert res[0]["blended_score"] == 80.0

        # Failed run exists for MONITORING, but no completed run exists -> returns []
        res_mon = c.get("/api/v1/portfolio/ranking", params={"cycle_type": "MONITORING"}).json()
        assert res_mon == []


def _seed_quant_vs_live(path: Path) -> tuple[int, int, int]:
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    conn = Database(raw)
    conn.execute("PRAGMA foreign_keys = ON")
    fdb.ensure_schema(conn)
    pdb.ensure_schema(conn)
    qdb.ensure_schema(conn)

    aid_aapl = int(
        conn.execute("INSERT INTO assets (ticker) VALUES ('AAPL') RETURNING id").fetchone()["id"]
    )
    aid_msft = int(
        conn.execute("INSERT INTO assets (ticker) VALUES ('MSFT') RETURNING id").fetchone()["id"]
    )
    aid_goog = int(
        conn.execute("INSERT INTO assets (ticker) VALUES ('GOOG') RETURNING id").fetchone()["id"]
    )

    conn.execute(
        "INSERT INTO portfolio_position (asset_id, valid_from, valid_to, weight) VALUES "
        "(?, '2024-01-01', NULL, 0.5), (?, '2024-01-01', NULL, 0.3)",
        (aid_aapl, aid_goog),
    )

    def _add_book(
        kind: str,
        as_of: str,
        weights: dict[int, float],
        engine_version: str = "opt-v2+tag1",
    ) -> None:
        pid = qdb.insert_portfolio(
            conn,
            qdb.PortfolioRow(
                as_of=as_of,
                kind=kind,
                objective=kind,
                solver="CLARABEL",
                status="optimal",
                expected_return=0.05,
                expected_vol=0.1,
                sharpe=0.5,
                rf_annual=0.04,
                n_positions=len(weights),
                engine_version=engine_version,
            ),
        )
        qdb.sync_positions(conn, pid, as_of, weights)

    # Date 1 (2024-06-01): min_var holds AAPL and MSFT; GOOG is held by none (LIVE_ONLY)
    _add_book("min_var", "2024-06-01", {aid_aapl: 0.6, aid_msft: 0.4})

    # Date 2 (2024-07-01):
    # - current min_var holds AAPL (0.7) and MSFT (0.3)
    # - current tangency holds AAPL (0.4) and MSFT (0.6)
    # - variant min_var holds AAPL (0.5) and MSFT (0.5) with is_current = 0
    # GOOG is held by no current book -> LIVE_ONLY on 2024-07-01
    _add_book("min_var", "2024-07-01", {aid_aapl: 0.7, aid_msft: 0.3})
    _add_book("tangency", "2024-07-01", {aid_aapl: 0.4, aid_msft: 0.6})
    _add_book(
        "min_var",
        "2024-07-01",
        {aid_aapl: 0.5, aid_msft: 0.5},
        engine_version="opt-v2.variant+tag1",
    )

    conn.commit()
    conn.close()
    return aid_aapl, aid_msft, aid_goog


def test_quant_vs_live_route(tmp_path: Path, universe_db: _UDB) -> None:
    fin = tmp_path / "fin_qvl.db"
    _, _, aid_goog = _seed_quant_vs_live(fin)

    udb = universe_db([("AAPL", "2020-01-01", None), ("MSFT", "2020-01-01", None)])
    settings = ApiSettings(db_path=fin, universe_db_path=udb)

    with TestClient(create_app(settings)) as c:
        # 1. Omitted as_of picks newest book date (2024-07-01)
        res_default = c.get("/api/v1/portfolio/quant-vs-live")
        assert res_default.status_code == 200
        rows = res_default.json()
        assert len(rows) == 5  # 2 min_var + 2 tangency + 1 LIVE_ONLY
        assert all(r["as_of"] == "2024-07-01" for r in rows)

        # Check ordering: kind, then ticker
        keys = [(r["kind"], r["ticker"]) for r in rows]
        assert keys == sorted(keys)

        # Check LIVE_ONLY row content
        live_only = [r for r in rows if r["kind"] == "LIVE_ONLY"]
        assert len(live_only) == 1
        assert live_only[0] == {
            "as_of": "2024-07-01",
            "kind": "LIVE_ONLY",
            "ticker": "GOOG",
            "asset_id": aid_goog,
            "benchmark_weight": None,
            "live_weight": 0.3,
            "active_weight": -0.3,
            "engine_version": None,
            "is_current": 1,
        }

        # 2. as_of between two dates picks earlier one (2024-06-01)
        res_mid = c.get("/api/v1/portfolio/quant-vs-live", params={"as_of": "2024-06-15"}).json()
        assert len(res_mid) == 3  # 2 min_var + 1 LIVE_ONLY
        assert all(r["as_of"] == "2024-06-01" for r in res_mid)
        goog_mid = next(r for r in res_mid if r["ticker"] == "GOOG")
        assert goog_mid["kind"] == "LIVE_ONLY"
        assert goog_mid["as_of"] == "2024-06-01"
        assert goog_mid["live_weight"] == 0.3
        assert goog_mid["active_weight"] == -0.3

        # as_of before both dates returns []
        res_before = c.get("/api/v1/portfolio/quant-vs-live", params={"as_of": "2024-05-01"}).json()
        assert res_before == []

        # 3. kind filtering
        # kind=min_var returns min_var rows plus LIVE_ONLY rows
        res_mv = c.get("/api/v1/portfolio/quant-vs-live", params={"kind": "min_var"}).json()
        assert {r["kind"] for r in res_mv} == {"LIVE_ONLY", "min_var"}
        assert [r["ticker"] for r in res_mv if r["kind"] == "LIVE_ONLY"] == ["GOOG"]
        assert {r["ticker"] for r in res_mv if r["kind"] == "min_var"} == {"AAPL", "MSFT"}

        # kind=LIVE_ONLY returns only LIVE_ONLY rows
        res_lo = c.get("/api/v1/portfolio/quant-vs-live", params={"kind": "LIVE_ONLY"}).json()
        assert len(res_lo) == 1
        assert res_lo[0]["kind"] == "LIVE_ONLY"
        assert res_lo[0]["ticker"] == "GOOG"

        # 4. current_only flag
        # current_only=true (default) hides variant book
        res_curr = c.get("/api/v1/portfolio/quant-vs-live", params={"current_only": "true"}).json()
        assert all(r["is_current"] == 1 for r in res_curr)
        assert len(res_curr) == 5

        # current_only=false reveals variant book (is_current = 0)
        res_all = c.get("/api/v1/portfolio/quant-vs-live", params={"current_only": "false"}).json()
        variant_rows = [r for r in res_all if r["is_current"] == 0]
        assert len(variant_rows) == 2  # AAPL and MSFT from variant book
        assert all(r["engine_version"] == "opt-v2.variant+tag1" for r in variant_rows)


def test_quant_vs_live_partial_database(tmp_path: Path, universe_db: _UDB) -> None:
    fin = tmp_path / "fin_partial.db"
    raw = sqlite3.connect(fin)
    raw.row_factory = sqlite3.Row
    conn = Database(raw)
    fdb.ensure_schema(conn)  # No qdb.ensure_schema! So v_quant_vs_live does not exist.
    conn.commit()
    conn.close()

    udb = universe_db([("AAPL", "2020-01-01", None)])
    settings = ApiSettings(db_path=fin, universe_db_path=udb)
    with TestClient(create_app(settings)) as c:
        res = c.get("/api/v1/portfolio/quant-vs-live")
        assert res.status_code == 200
        assert res.json() == []
