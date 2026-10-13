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
        assert done["steps"][0]["status"] == "failed"
        assert done["steps"][0]["reason"] == "OSError: gateway died"
        assert all(s["status"] == "not_run" for s in done["steps"][1:])
        # The active slot was cleared: a new run is accepted, not a stale 409.
        again = client.post("/api/v1/run/pipeline", json={"analysis_date": "2026-10-02"})
        assert again.status_code == 202


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
    # run_status reads the orchestrator state and nothing that starts a run.
    assert "api.orchestrator" in _imported_modules(_ROUTERS / "run_status.py")


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
