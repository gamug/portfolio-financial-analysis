"""The run endpoints (T-011, SPEC FR-014): 202 + handle, polling, 409, 404, 422, the
argv each endpoint builds, and the read-router split. Hermetic: ``run_command`` is
replaced, no agent is spawned."""

from __future__ import annotations

import ast
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import kg_schema
from api import orchestrator as orch
from api.app import create_app
from api.config import ApiSettings

_SRC = Path(__file__).resolve().parent.parent / "src"
_ROUTERS = _SRC / "api" / "routers"


@pytest.fixture(autouse=True)
def _fresh_registry() -> Iterator[None]:
    orch.clear_runs()
    yield
    orch.clear_runs()


def _client(tmp_path: Path, *, news_exists: bool = True) -> TestClient:
    news = tmp_path / "news.db"
    if news_exists:
        news.write_text("")
    (tmp_path / "fin.db").touch()  # the read routes need the DB file to exist
    settings = ApiSettings(
        db_path=tmp_path / "fin.db",
        universe_db_path=tmp_path / "universe.db",
        news_db_path=news,
    )
    return TestClient(create_app(settings))


class FakeRunner:
    def __init__(
        self,
        answers: dict[tuple[str, str], tuple[int, str]] | None = None,
        errors: dict[tuple[str, str], BaseException] | None = None,
    ) -> None:
        self.answers = answers or {}
        self.errors = errors or {}
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.gate: threading.Event | None = None
        self.started = threading.Event()

    def __call__(self, argv: list[str], env: dict[str, str]) -> orch.CommandResult:
        self.calls.append((list(argv), dict(env)))
        self.started.set()
        if self.gate is not None:
            assert self.gate.wait(timeout=10)
        key = (argv[2], argv[3])
        if key in self.errors:
            raise self.errors[key]
        code, output = self.answers.get(key, (0, ""))
        return orch.CommandResult(returncode=code, output=output)


def _wait(client: TestClient, url: str, want: set[str], *, tries: int = 200) -> dict[str, Any]:
    for _ in range(tries):
        body: dict[str, Any] = client.get(url).json()
        if body["status"] in want:
            return body
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {want}; last={body}")


_IDS = {
    ("pricing_agent", "run"): (0, "\nrun 1: ok"),
    ("fundamental_agent", "run"): (0, "\nrun 2: ok"),
    ("entity_resolution", "build"): (0, "cycle 3: 0 edges"),
    ("cycle", "select"): (0, "select 4 2026-10-02: ok"),
}


def test_post_pipeline_returns_202_and_polls_to_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRunner(_IDS)
    monkeypatch.setattr(orch, "run_command", fake)
    with _client(tmp_path) as client:
        r = client.post("/api/v1/run/pipeline", json={"analysis_date": "2026-10-02"})
        assert r.status_code == 202
        body = r.json()
        assert body["status_url"] == f"/api/v1/run/{body['handle']}"
        done = _wait(client, body["status_url"], {"completed", "failed"})
        assert done["status"] == "completed"
        assert len(done["steps"]) == 9
        assert done["analysis_date"] == "2026-10-02"
        kinds = [s["name"] for s in done["steps"]]
        assert kinds[0] == "pricing" and kinds[-1] == "quant_evaluate"


def test_status_is_running_then_completed_and_second_post_is_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRunner(_IDS)
    fake.gate = threading.Event()
    monkeypatch.setattr(orch, "run_command", fake)
    with _client(tmp_path) as client:
        r = client.post("/api/v1/run/pipeline", json={"analysis_date": "2026-10-02"})
        handle = r.json()["handle"]
        # The first step is blocked inside run_command: the run is running and its request
        # was already answered with 202 (never held open).
        assert fake.started.wait(timeout=10)
        running = _wait(client, f"/api/v1/run/{handle}", {"running"})
        assert running["steps"][0]["status"] == "running"
        # A second run while active:
        conflict = client.post("/api/v1/run/pipeline", json={"analysis_date": "2026-10-02"})
        assert conflict.status_code == 409
        assert conflict.json()["handle"] == handle
        assert conflict.json()["status_url"] == f"/api/v1/run/{handle}"
        fake.gate.set()
        done = _wait(client, f"/api/v1/run/{handle}", {"completed", "failed"})
        assert done["status"] == "completed"


def test_unknown_handle_is_404(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert client.get("/api/v1/run/does-not-exist").status_code == 404


def test_a_raising_step_fails_the_run_and_frees_the_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRunner(_IDS, errors={("pricing_agent", "run"): OSError("gateway died")})
    monkeypatch.setattr(orch, "run_command", fake)
    with _client(tmp_path) as client:
        r = client.post("/api/v1/run/pipeline", json={"analysis_date": "2026-10-02"})
        assert r.status_code == 202
        handle = r.json()["handle"]
        done = _wait(client, f"/api/v1/run/{handle}", {"failed", "completed"})
        assert done["status"] == "failed"
        statuses = {s["name"]: s["status"] for s in done["steps"]}
        assert statuses["pricing"] == "failed"
        assert done["steps"][0]["reason"] == "OSError: gateway died"
        assert statuses["fundamental"] == "completed"  # independent of pricing
        assert statuses["entity_resolution"] == "completed"
        assert statuses["cycle_select"] == "blocked"
        assert statuses["quant_evaluate"] == "blocked"
        # The active slot was cleared: a new run is accepted, not a stale 409.
        again = client.post("/api/v1/run/pipeline", json={"analysis_date": "2026-10-02"})
        assert again.status_code == 202
        _wait(client, f"/api/v1/run/{again.json()['handle']}", {"failed", "completed"})


def test_a_provenance_write_failure_inside_a_step_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRunner(_IDS)
    monkeypatch.setattr(orch, "run_command", fake)
    # A write inside fundamental's own _run_one fails once: the step's run must not swallow it
    # as an isolated failure (T-013).
    monkeypatch.setattr(orch, "_persist_step", _flaky_persist_in_run_one("fundamental"))
    with _client(tmp_path) as client:
        handle = client.post("/api/v1/run/pipeline", json={"analysis_date": "2026-10-02"}).json()[
            "handle"
        ]
        done = _wait(client, f"/api/v1/run/{handle}", {"failed", "completed"})
    assert done["status"] == "failed"
    statuses = {s["name"]: s["status"] for s in done["steps"]}
    assert statuses["pricing"] == "completed"
    assert statuses["fundamental"] == "failed"
    fundamental = next(s for s in done["steps"] if s["name"] == "fundamental")
    assert fundamental["reason"] == "provenance write failed: OSError: disk full"
    assert all(v == "not_run" for k, v in statuses.items() if k not in ("pricing", "fundamental"))
    assert len(fake.calls) == 2  # no further child started
    assert orch._REGISTRY.active is None  # the active handle was cleared


def test_blocked_rows_are_persisted_and_served_by_the_status_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRunner(_IDS, errors={("pricing_agent", "run"): OSError("gateway died")})
    monkeypatch.setattr(orch, "run_command", fake)
    with _client(tmp_path) as client:
        handle = client.post("/api/v1/run/pipeline", json={"analysis_date": "2026-10-02"}).json()[
            "handle"
        ]
        done = _wait(client, f"/api/v1/run/{handle}", {"failed", "completed"})
    blocked = {s["name"]: s for s in done["steps"] if s["status"] == "blocked"}
    assert set(blocked) == {
        "cycle_select",
        "quant_actions",
        "quant_returns",
        "quant_risk_model",
        "quant_optimize",
        "quant_evaluate",
    }
    assert blocked["cycle_select"]["reason"] == "blocked by pricing (failed)"
    assert blocked["cycle_select"]["started_at"] is None
    assert blocked["cycle_select"]["finished_at"] is None
    assert blocked["cycle_select"]["exit_code"] is None
    assert blocked["cycle_select"]["run_id"] is None


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/run/pipeline",
        "/api/v1/run/cycle/select",
        "/api/v1/run/quant/build-risk-model",
    ],
)
@pytest.mark.parametrize(
    "extra",
    [
        {"universe_db": "/tmp/x.db"},
        {"db": "/tmp/x.db"},
        {"universe": "SP500"},
        {"tickers": "AAPL"},
        {"allow_dirty": True},
        {"pins": "AAPL"},
        {"exclude": "AAPL"},
    ],
)
def test_extra_or_forbidden_fields_are_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, extra: dict[str, Any]
) -> None:
    monkeypatch.setattr(orch, "run_command", FakeRunner(_IDS))
    with _client(tmp_path) as client:
        r = client.post(path, json={"analysis_date": "2026-10-02", **extra})
        assert r.status_code == 422, (path, extra, r.json())


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/run/pipeline", {"analysis_date": "not-a-date"}),
        ("/api/v1/run/pipeline", {"analysis_date": "2026-10-02", "top_n": 0}),
        ("/api/v1/run/pipeline", {"analysis_date": "2026-10-02", "max_name_weight": 0}),
        ("/api/v1/run/pipeline", {"analysis_date": "2026-10-02", "max_sector_weight": 1.5}),
        ("/api/v1/run/pipeline", {"analysis_date": "2026-10-02", "weight_scheme": "bogus"}),
        ("/api/v1/run/quant/evaluate", {"analysis_date": "2026-10-02", "top_n": 30}),
        ("/api/v1/run/quant/evaluate", {"analysis_date": "2026-10-02", "weight_scheme": "equal"}),
        ("/api/v1/run/cycle/monitor", {"analysis_date": "2026-10-02", "top_n": 30}),
        (
            "/api/v1/run/quant/build-risk-model",
            {"analysis_date": "2026-10-02", "weight_scheme": "equal"},
        ),
        ("/api/v1/run/pipeline", {}),
    ],
)
def test_invalid_bodies_are_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, body: dict[str, Any]
) -> None:
    monkeypatch.setattr(orch, "run_command", FakeRunner(_IDS))
    with _client(tmp_path) as client:
        assert client.post(path, json=body).status_code == 422, (path, body)


def test_pipeline_argv_has_no_forbidden_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRunner(_IDS)
    monkeypatch.setattr(orch, "run_command", fake)
    body = {
        "analysis_date": "2026-10-02",
        "top_n": 30,
        "weight_scheme": "equal",
        "max_name_weight": 0.1,
        "max_sector_weight": 0.3,
    }
    with _client(tmp_path) as client:
        handle = client.post("/api/v1/run/pipeline", json=body).json()["handle"]
        done = _wait(client, f"/api/v1/run/{handle}", {"completed", "failed"})
    for step in done["steps"]:
        blob = " ".join(step["argv"])
        for token in (
            "--pin",
            "--exclude",
            "--only-sectors",
            "--allow",
            "--force",
            "--fresh",
            "migrate",
        ):
            assert token not in blob, (step["name"], token)


def test_each_single_step_endpoint_builds_its_own_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orch, "run_command", FakeRunner(_IDS))
    expected = {
        "/api/v1/run/pricing-agent": [
            "-m",
            "pricing_agent",
            "run",
            "--store-daily",
            "--observations",
        ],
        "/api/v1/run/fundamental-agent": ["-m", "fundamental_agent", "run"],
        "/api/v1/run/entity-resolution": ["-m", "entity_resolution", "build"],
        "/api/v1/run/cycle/select": ["-m", "cycle", "select"],
        "/api/v1/run/cycle/monitor": ["-m", "cycle", "monitor"],
        "/api/v1/run/quant/backfill-actions": ["-m", "quant", "backfill-actions"],
        "/api/v1/run/quant/build-returns": ["-m", "quant", "build-returns"],
        "/api/v1/run/quant/build-risk-model": ["-m", "quant", "build-risk-model"],
        "/api/v1/run/quant/optimize": ["-m", "quant", "optimize"],
        "/api/v1/run/quant/evaluate": ["-m", "quant", "evaluate"],
    }
    with _client(tmp_path) as client:
        for path, head in expected.items():
            handle = client.post(path, json={"analysis_date": "2026-10-02"}).json()["handle"]
            done = _wait(client, f"/api/v1/run/{handle}", {"completed", "failed"})
            assert len(done["steps"]) == 1, path
            assert done["steps"][0]["argv"][: len(head)] == head, path


def test_entity_resolution_via_http_is_skipped_when_news_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orch, "run_command", FakeRunner(_IDS))
    with _client(tmp_path, news_exists=False) as client:
        handle = client.post(
            "/api/v1/run/entity-resolution", json={"analysis_date": "2026-10-02"}
        ).json()["handle"]
        done = _wait(client, f"/api/v1/run/{handle}", {"completed", "failed"})
    step = done["steps"][0]
    assert step["status"] == "skipped"
    assert step["reason"] is not None and step["reason"].startswith("no news source configured")


def test_no_route_for_migrate_backfill_or_undo_run(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        for path in (
            "/api/v1/run/migrate",
            "/api/v1/run/cycle/backfill",
            "/api/v1/run/cycle/undo-run",
            "/api/v1/run/coverage",
        ):
            assert client.post(path, json={"analysis_date": "2026-10-02"}).status_code in (404, 405)
        run_paths = client.get("/openapi.json").json()["paths"]
        for forbidden in (
            "/api/v1/run/migrate",
            "/api/v1/run/cycle/backfill",
            "/api/v1/run/cycle/undo-run",
            "/api/v1/run/coverage",
        ):
            assert forbidden not in run_paths


# -- provenance (T-012) -------------------------------------------------------------------------


def _fetch(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> list:
    conn = kg_schema.connect_ro(db_path)
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def _flaky_persist(fail_on: int) -> object:
    real = orch._persist_step
    state = {"n": 0}

    def flaky(step: orch.StepState) -> None:
        state["n"] += 1
        if state["n"] == fail_on:
            raise orch._ProvenanceWriteFailed(OSError("disk full"))
        real(step)

    return flaky


def _flaky_persist_in_run_one(step_name: str) -> object:
    """A ``_persist_step`` that fails once, on *step_name*'s first write inside ``_run_one``
    (``exit_code`` is set in memory before the write), then succeeds."""
    real = orch._persist_step
    state = {"failed": False}

    def flaky(step: orch.StepState) -> None:
        if not state["failed"] and step.name == step_name and step.exit_code is not None:
            state["failed"] = True
            raise orch._ProvenanceWriteFailed(OSError("disk full"))
        real(step)

    return flaky


def test_the_202_handle_is_readable_at_once_and_rows_are_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRunner(_IDS)
    fake.gate = threading.Event()
    monkeypatch.setattr(orch, "run_command", fake)
    with _client(tmp_path) as client:
        r = client.post("/api/v1/run/pipeline", json={"analysis_date": "2026-10-02"})
        handle = r.json()["handle"]
        assert fake.started.wait(timeout=10)
        # the run row exists before the first child returns: a GET finds it at once
        got = client.get(f"/api/v1/run/{handle}")
        assert got.status_code == 200 and got.json()["handle"] == handle
        assert got.json()["steps"][0]["status"] == "running"
        fake.gate.set()
        _wait(client, f"/api/v1/run/{handle}", {"completed", "failed"})
    runs = _fetch(tmp_path / "fin.db", "SELECT * FROM v_orchestrator_run")
    steps = _fetch(tmp_path / "fin.db", "SELECT * FROM v_orchestrator_step ORDER BY ordinal")
    assert len(runs) == 1 and runs[0]["handle"] == handle
    assert [s["ordinal"] for s in steps] == list(range(1, 10))


def test_a_single_step_writes_one_run_and_one_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orch, "run_command", FakeRunner(_IDS))
    with _client(tmp_path) as client:
        handle = client.post(
            "/api/v1/run/pricing-agent", json={"analysis_date": "2026-10-02"}
        ).json()["handle"]
        _wait(client, f"/api/v1/run/{handle}", {"completed", "failed"})
    assert len(_fetch(tmp_path / "fin.db", "SELECT * FROM v_orchestrator_run")) == 1
    steps = _fetch(tmp_path / "fin.db", "SELECT * FROM v_orchestrator_step")
    assert len(steps) == 1 and steps[0]["step"] == "pricing"


def test_status_survives_a_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orch, "run_command", FakeRunner(_IDS))
    with _client(tmp_path) as client:
        handle = client.post("/api/v1/run/pipeline", json={"analysis_date": "2026-10-02"}).json()[
            "handle"
        ]
        done = _wait(client, f"/api/v1/run/{handle}", {"completed", "failed"})
    orch.clear_runs()  # the in-memory registry is gone (a restart)
    with _client(tmp_path) as client:  # a new app instance
        again = client.get(f"/api/v1/run/{handle}").json()
    assert again == done


def test_a_run_row_that_cannot_be_written_is_500_and_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRunner(_IDS)
    monkeypatch.setattr(orch, "run_command", fake)
    not_a_db = tmp_path / "not_a_db"
    not_a_db.mkdir()
    settings = ApiSettings(
        db_path=not_a_db,
        universe_db_path=tmp_path / "universe.db",
        news_db_path=tmp_path / "news.db",
    )
    with TestClient(create_app(settings)) as client:
        r = client.post("/api/v1/run/pipeline", json={"analysis_date": "2026-10-02"})
    assert r.status_code == 500
    assert "detail" in r.json()
    assert fake.calls == []  # no child started


def test_a_provenance_write_failure_stops_the_run_and_frees_the_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orch, "run_command", FakeRunner(_IDS))
    monkeypatch.setattr(orch, "_persist_step", _flaky_persist(3))
    with _client(tmp_path) as client:
        handle = client.post("/api/v1/run/pipeline", json={"analysis_date": "2026-10-02"}).json()[
            "handle"
        ]
        done = _wait(client, f"/api/v1/run/{handle}", {"failed", "completed"})
        assert done["status"] == "failed"
        failed = next(s for s in done["steps"] if s["status"] == "failed")
        assert failed["reason"] == "provenance write failed: OSError: disk full"
        assert all(s["status"] == "not_run" for s in done["steps"][1:])
        again = client.post("/api/v1/run/pipeline", json={"analysis_date": "2026-10-02"})
        assert again.status_code == 202  # the slot was freed
        _wait(client, f"/api/v1/run/{again.json()['handle']}", {"failed", "completed"})


def test_get_runs_lists_orchestrator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orch, "run_command", FakeRunner(_IDS))
    with _client(tmp_path) as client:
        handle = client.post("/api/v1/run/pipeline", json={"analysis_date": "2026-10-02"}).json()[
            "handle"
        ]
        _wait(client, f"/api/v1/run/{handle}", {"completed", "failed"})
        only = client.get("/api/v1/runs?kind=orchestrator").json()
        everything = client.get("/api/v1/runs").json()
    assert [r["kind"] for r in only] == ["orchestrator"]
    assert only[0]["as_of"] == "2026-10-02"
    assert only[0]["status"] in ("completed", "failed")
    assert only == everything  # this database has only the one orchestrator run


# -- the read/run router split ------------------------------------------------------------------


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules.update(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_no_read_router_imports_the_run_router() -> None:
    for path in sorted(_ROUTERS.glob("*.py")):
        if path.name == "run.py":
            continue
        assert "api.routers.run" not in _imported_modules(path), path
    # run_status reads the recorded provenance (T-012) and nothing that starts a run: it no
    # longer imports api.orchestrator.
    assert "api.orchestrator" not in _imported_modules(_ROUTERS / "run_status.py")


def test_runs_router_has_no_f_string_sql() -> None:
    """Code & Git #10: runs.py routes the view name through a static literal-SQL dict, never
    an f-string (the T-012 fix)."""
    for node in ast.walk(ast.parse((_ROUTERS / "runs.py").read_text())):
        if isinstance(node, ast.JoinedStr):
            text = "".join(
                v.value
                for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            )
            assert "SELECT" not in text.upper(), "runs.py builds SQL with an f-string"


def test_no_read_router_opens_a_write_capable_connection() -> None:
    for path in sorted(_ROUTERS.glob("*.py")):
        if path.name == "run.py":
            continue  # the run router starts subprocesses; it opens no connection either
        names = {
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Call)
        }
        assert "connect" not in names, path
