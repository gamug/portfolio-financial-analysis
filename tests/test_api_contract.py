"""``GET /api/v1/contract`` and ``/contract/database`` (T-152, SPEC FR-014): the ``v_*`` view
contract published as metadata, built from the code, never from a second list."""

from __future__ import annotations

import ast
import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import _memory_database
from fastapi.testclient import TestClient
from portfolio_common.db import Database

import api.contract
import kg_schema
from api.app import create_app
from api.config import ApiSettings
from fundamental_agent import db as fundamental_db
from kg_schema.migrations import MIGRATIONS
from kg_schema.provenance import code_version
from kg_schema.views import FROZEN_VIEWS, VIEWS
from pricing_agent import db as pricing_db
from quant import db as quant_db

_SRC = Path(__file__).resolve().parent.parent / "src"
_ROUTER = _SRC / "api" / "routers" / "contract.py"


def _full_database() -> Database:
    """All three agents' tables plus the shared schema, migrated: every view builds."""
    db = _memory_database()
    fundamental_db.ensure_schema(db)
    pricing_db.ensure_schema(db)
    quant_db.ensure_schema(db)
    kg_schema.ensure(db, run_migrations=True)
    return db


def _view_columns(db: Database) -> dict[str, list[str]]:
    """Independent of the code under test: ``PRAGMA table_info`` per view, in ``VIEWS`` order."""
    return {name: [r[1] for r in db.execute(f"PRAGMA table_info({name})")] for name in VIEWS}


def _app(db_path: Path, tmp_path: Path) -> TestClient:
    settings = ApiSettings(db_path=db_path, universe_db_path=tmp_path / "universe.db")
    return TestClient(create_app(settings))


@pytest.fixture
def migrated_path(tmp_path: Path) -> Path:
    """A migrated database file on disk (schema_version 11, every view present)."""
    path = tmp_path / "fin.db"
    conn = kg_schema.connect(path)
    fundamental_db.ensure_schema(conn)
    pricing_db.ensure_schema(conn)
    quant_db.ensure_schema(conn)
    kg_schema.ensure(conn, run_migrations=True)
    conn.close()
    return path


@pytest.fixture
def client(migrated_path: Path, tmp_path: Path) -> Iterator[TestClient]:
    with _app(migrated_path, tmp_path) as c:
        yield c


# -- GET /contract ----------------------------------------------------------------------------


def test_contract_lists_exactly_the_views_and_columns_of_an_independent_build(
    client: TestClient,
) -> None:
    expected = _view_columns(_full_database())
    body = client.get("/api/v1/contract").json()
    assert [v["name"] for v in body["views"]] == list(VIEWS)  # every view, in VIEWS order
    for view in body["views"]:
        assert [c["name"] for c in view["columns"]] == expected[view["name"]], view["name"]
        assert view["columns"], f"{view['name']} has no columns"


def test_a_view_the_build_cannot_create_is_an_error_not_a_shorter_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A view over a table the build does not have is dropped by ``ensure_views``; the contract
    must fail loudly rather than quietly omit it."""
    monkeypatch.setitem(
        VIEWS, "v_added_later", "CREATE VIEW v_added_later AS SELECT x FROM table_nobody_builds"
    )
    api.contract.build_contract.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="v_added_later"):
            api.contract.build_contract()
    finally:
        monkeypatch.undo()
        api.contract.build_contract.cache_clear()


def test_a_view_added_to_views_is_in_the_response(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(VIEWS, "v_added_later", "CREATE VIEW v_added_later AS SELECT 1 AS x")
    api.contract.build_contract.cache_clear()
    try:
        views = client.get("/api/v1/contract").json()["views"]
        assert views[-1] == {
            "name": "v_added_later",
            "frozen": False,
            "columns": [{"name": "x"}],
        }
    finally:
        monkeypatch.undo()
        api.contract.build_contract.cache_clear()


def test_frozen_is_true_exactly_for_frozen_views(client: TestClient) -> None:
    assert set(VIEWS) >= FROZEN_VIEWS  # a name in the set that is not a view fails here
    views = client.get("/api/v1/contract").json()["views"]
    assert (
        {v["name"] for v in views if v["frozen"]} == set(FROZEN_VIEWS) == {"v_universe_membership"}
    )


def test_contract_version_is_the_highest_migration_and_code_version_is_the_codes(
    client: TestClient,
) -> None:
    body = client.get("/api/v1/contract").json()
    assert body["contract_version"] == max(v for v, _, _ in MIGRATIONS) == 11
    assert body["code_version"] == code_version()


def test_the_contract_is_built_once_per_process(client: TestClient) -> None:
    api.contract.build_contract.cache_clear()
    first = client.get("/api/v1/contract").json()
    assert client.get("/api/v1/contract").json() == first
    assert api.contract.build_contract.cache_info().misses == 1


def test_the_declared_quant_run_table_matches_quants_own() -> None:
    """``api`` cannot import ``quant`` (NR-002), so it declares ``quant_run`` itself; this is
    what keeps the declaration from drifting from ``quant.db.SCHEMA``."""

    def table_info(ddl: str) -> list[tuple[object, ...]]:
        db = _memory_database()
        db.create_schema(ddl)
        return [tuple(r)[1:] for r in db.execute("PRAGMA table_info(quant_run)")]

    assert table_info(api.contract.QUANT_RUN_DDL) == table_info(quant_db.SCHEMA)


# -- GET /contract/database -------------------------------------------------------------------


def test_database_contract_of_a_complete_database(client: TestClient) -> None:
    body = client.get("/api/v1/contract/database").json()
    assert body == {
        "schema_version": 11,
        "views_present": list(VIEWS),
        "views_missing": [],
    }


def test_database_contract_of_a_partial_database_names_the_missing_views(tmp_path: Path) -> None:
    """Only the shared schema: the views over the agents' own tables cannot be built."""
    path = tmp_path / "partial.db"
    conn = kg_schema.connect(path)
    kg_schema.ensure(conn, run_migrations=True)
    present = [n for n in VIEWS if conn.relation_kind(n) == "view"]
    conn.close()
    assert 0 < len(present) < len(VIEWS)  # genuinely partial

    with _app(path, tmp_path) as c:
        r = c.get("/api/v1/contract/database")
    assert r.status_code == 200
    body = r.json()
    assert body["schema_version"] == 11
    assert body["views_present"] == present
    assert body["views_missing"] == [n for n in VIEWS if n not in present]
    assert "v_quant_run" in body["views_missing"]


def test_database_contract_reports_the_recorded_version_not_the_codes(
    migrated_path: Path, tmp_path: Path
) -> None:
    """A database that lags its code (migrate not run): the version is its own."""
    raw = sqlite3.connect(migrated_path)
    raw.execute("DELETE FROM schema_version WHERE version = 11")
    raw.commit()
    raw.close()
    with _app(migrated_path, tmp_path) as c:
        assert c.get("/api/v1/contract/database").json()["schema_version"] == 10
        assert c.get("/api/v1/contract").json()["contract_version"] == 11


def test_database_without_a_schema_version_table_reports_0_and_stays_empty(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bare.db"
    sqlite3.connect(path).close()
    with _app(path, tmp_path) as c:
        body = c.get("/api/v1/contract/database").json()
    assert body == {"schema_version": 0, "views_present": [], "views_missing": list(VIEWS)}
    raw = sqlite3.connect(path)  # the read did not create the table
    assert raw.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    raw.close()


# -- FR-014: read-only ------------------------------------------------------------------------


def _calls(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call):
            f = node.func
            names.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return names


def test_the_read_router_opens_no_write_capable_connection() -> None:
    assert "connect" not in _calls(_ROUTER)  # connect_ro is a different name
    source = _ROUTER.read_text()
    assert "sqlite3" not in source
    assert "api.routers.runs" not in source  # no run router is imported (none exists yet)
    # the connection a read route gets is the dependency's, and that one is mode=ro
    deps = _calls(_SRC / "api" / "dependencies.py")
    assert "connect_ro" in deps and "connect" not in deps


def test_every_connect_in_contract_is_memory_and_no_other_api_module_calls_connect() -> None:
    contract_py = _SRC / "api" / "contract.py"
    tree = ast.parse(contract_py.read_text())
    connect_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            getattr(node.func, "attr", "") == "connect" or getattr(node.func, "id", "") == "connect"
        )
    ]
    assert connect_calls, "expected at least one connect() call in contract.py"
    for call in connect_calls:
        assert call.args, "connect() called with no arguments"
        first = call.args[0]
        assert isinstance(first, ast.Constant) and first.value == ":memory:"

    for path in sorted((_SRC / "api").rglob("*.py")):
        if path.name == "contract.py":
            continue
        assert "connect" not in _calls(path), f"{path} calls connect()"


def test_both_routes_leave_the_databases_bytes_unchanged(
    migrated_path: Path, tmp_path: Path
) -> None:
    def snapshot() -> dict[str, str]:
        return {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(tmp_path.iterdir())
        }

    before = snapshot()
    with _app(migrated_path, tmp_path) as c:
        assert c.get("/api/v1/contract").status_code == 200
        assert c.get("/api/v1/contract/database").status_code == 200
    assert snapshot() == before  # no byte changed, no journal or sidecar file appeared
