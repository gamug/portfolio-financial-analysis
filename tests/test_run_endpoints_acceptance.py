"""T-019 acceptance: the run endpoints.

The criteria are PLAN.md Work item 2, step 1 and the acceptance bullet "Run endpoints
(T-019)" (and SPEC FR-014). Every test is named after the clause it proves (C1..C13); the
clauses already proven by the existing tests are listed in the PR body. This module adds the
four gaps the review found -- C3/G1, C6/G2, C7/G3, C12/G4 -- plus C13/G5 (the CLI wrapper's
parser -> ``RunContext`` link).

Hermetic: no agent is spawned and no network is touched; the only database is a ``tmp_path``
file (C13 only writes the orchestrator's own provenance).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount, Route

from api import orchestrator as orch
from api.app import create_app
from api.config import ApiSettings

_SRC = Path(__file__).resolve().parent.parent / "src"
_API = _SRC / "api"

# PLAN.md step 1, in order: the orchestrator then one per step.
_RUN_ROUTES = (
    "/api/v1/run/pipeline",
    "/api/v1/run/pricing-agent",
    "/api/v1/run/fundamental-agent",
    "/api/v1/run/entity-resolution",
    "/api/v1/run/cycle/select",
    "/api/v1/run/cycle/monitor",
    "/api/v1/run/quant/backfill-actions",
    "/api/v1/run/quant/build-returns",
    "/api/v1/run/quant/build-risk-model",
    "/api/v1/run/quant/optimize",
    "/api/v1/run/quant/evaluate",
)

# Starlette adds HEAD to a GET route; OPTIONS is never a real operation here.
_IGNORED_METHODS = {"HEAD", "OPTIONS"}

# FastAPI's own routes, added by create_app: the only plain (non-API) routes allowed.
_ALLOWED_PLAIN_ROUTES = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

# The request contract (PLAN.md step 1): the only options the CLI wrapper accepts.
_CONTRACT_CLI_OPTIONS = (
    "--analysis-date",
    "--top-n",
    "--weight-scheme",
    "--max-name-weight",
    "--max-sector-weight",
    "--db",
    "--universe-db",
    "--news-db",
)

# Every preference flag and every override that must never reach the writing path.
_FORBIDDEN_CLI_FLAGS = (
    "--pin",
    "--exclude",
    "--exclude-sectors",
    "--only-sectors",
    "--allow-dirty",
    "--allow-stale-prices",
    "--allow-stale-dq-gate",
    "--allow-backdated",
    "--force",
    "--allow-missing-caps",
)

# kg_schema's write-capable entry points; a read path may only use ``connect_ro``.
_WRITE_SYMBOLS = {"connect", "ensure"}
_DB_SOURCES = {"kg_schema", "kg_schema.db", "portfolio_common.db"}


def _app(tmp_path: Path) -> FastAPI:
    return create_app(
        ApiSettings(
            db_path=tmp_path / "fin.db",
            universe_db_path=tmp_path / "universe.db",
            news_db_path=tmp_path / "news.db",
        )
    )


def _flatten(routes: list[Any], prefix: str = "") -> list[tuple[str, APIRoute]]:
    """Every APIRoute in *routes* with its effective path, descending into included routers.

    ``app.openapi()`` omits a route registered with ``include_in_schema=False`` and cannot see
    a mounted sub-app, so the route table is read from ``app.routes`` instead. FastAPI 0.141
    wraps ``include_router`` in an internal object whose routes live on
    ``include_context.included_router`` under ``include_context.prefix``; older FastAPI copies
    them straight into ``app.routes``. Both shapes are walked.
    """
    found: list[tuple[str, APIRoute]] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append((prefix + route.path, route))
        elif isinstance(route, Mount):
            continue  # asserted absent in C3; a mounted app's routes are out of this walk
        else:
            context = getattr(route, "include_context", None)
            included = getattr(context, "included_router", None) if context is not None else None
            if included is not None:
                found.extend(
                    _flatten(list(included.routes), prefix + getattr(context, "prefix", ""))
                )
            elif getattr(route, "routes", None):
                found.extend(_flatten(list(route.routes), prefix))
    return found


def test_c3_non_get_routes_are_exactly_the_eleven_run_routes(tmp_path: Path) -> None:
    app = _app(tmp_path)
    # The real table, not the OpenAPI document: no Mount, and no plain Route but FastAPI's own
    # docs/openapi routes (``/openapi.json``, ``/docs``, ``/docs/oauth2-redirect``, ``/redoc``).
    assert [route.path for route in app.routes if isinstance(route, Mount)] == []
    plain = {
        route.path
        for route in app.routes
        if isinstance(route, Route) and not isinstance(route, APIRoute)
    }
    assert plain <= _ALLOWED_PLAIN_ROUTES, plain

    operations = {
        (method, path)
        for path, route in _flatten(list(app.routes))
        for method in (route.methods or set())
        if method not in _IGNORED_METHODS
    }
    # Every non-GET operation of the whole app is one of the eleven run POSTs.
    assert {op for op in operations if op[0] != "GET"} == {("POST", path) for path in _RUN_ROUTES}
    # The only operation under /run that is not a POST is the read-only status GET.
    run_operations = {
        op for op in operations if op[1] == "/api/v1/run" or op[1].startswith("/api/v1/run/")
    }
    assert {op for op in run_operations if op[0] != "POST"} == {("GET", "/api/v1/run/{handle}")}

    # Second, weaker assertion: the OpenAPI document is a subset of the real table -- it can
    # only hide routes (the root redirect, ``include_in_schema=False``), never invent one.
    documented = {
        (method.upper(), path) for path, ops in app.openapi()["paths"].items() for method in ops
    }
    assert documented <= operations
    assert operations - documented == {("GET", "/")}


def test_c3_get_on_a_post_path_is_405_or_404_by_design(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GET never reaches a run. A path with two or more segments under ``/run`` matches a
    POST-only step route and answers 405; a single-segment path is shadowed by the status route
    ``GET /run/{handle}`` and answers 404 "unknown run handle". Either way, no run starts."""
    started: list[object] = []

    def forbidden_run_command(*args: object, **kwargs: object) -> object:
        started.append((args, kwargs))
        raise AssertionError("a GET must never start a run")

    monkeypatch.setattr(orch, "run_command", forbidden_run_command)

    db_path = tmp_path / "fin.db"
    conn = orch.connect(db_path)
    try:
        orch.ensure(conn, run_migrations=False)  # the orchestrator schema, with no run row
    finally:
        conn.close()

    client = TestClient(
        create_app(
            ApiSettings(
                db_path=db_path,
                universe_db_path=tmp_path / "universe.db",
                news_db_path=tmp_path / "news.db",
            )
        )
    )
    single: list[str] = []
    multiple: list[str] = []
    for path in _RUN_ROUTES:
        segments = path.removeprefix("/api/v1/run").strip("/").split("/")
        response = client.get(path)
        if len(segments) == 1:
            single.append(path)
            assert response.status_code == 404, path
            assert response.json()["detail"].startswith("unknown run handle"), path
        else:
            multiple.append(path)
            assert response.status_code == 405, path
    # The split, stated outright: the four single-segment POSTs are shadowed by the status
    # route; the other seven step paths are POST-only and answer 405.
    assert single == [
        "/api/v1/run/pipeline",
        "/api/v1/run/pricing-agent",
        "/api/v1/run/fundamental-agent",
        "/api/v1/run/entity-resolution",
    ]
    assert multiple == [
        "/api/v1/run/cycle/select",
        "/api/v1/run/cycle/monitor",
        "/api/v1/run/quant/backfill-actions",
        "/api/v1/run/quant/build-returns",
        "/api/v1/run/quant/build-risk-model",
        "/api/v1/run/quant/optimize",
        "/api/v1/run/quant/evaluate",
    ]
    assert started == []  # run_command was never called
    ro = orch.connect_ro(db_path)
    try:
        rows = ro.execute("SELECT COUNT(*) AS n FROM orchestrator_run").fetchall()
    finally:
        ro.close()
    assert rows[0]["n"] == 0  # no orchestrator_run row was written


# -- C6: no read router reaches the run path, directly or transitively --------------------------


def _api_modules() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in sorted(_API.rglob("*.py")):
        parts = list(path.relative_to(_SRC).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(parts)] = path
    return modules


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def test_c6_no_read_router_reaches_the_run_path() -> None:
    modules = _api_modules()
    graph = {
        name: {i for i in _imported_modules(path) if i in modules and i != name}
        for name, path in modules.items()
    }
    read_routers = sorted(
        name for name in modules if name.startswith("api.routers.") and name != "api.routers.run"
    )
    assert read_routers, "expected read routers to exist"
    for start in read_routers:
        seen: set[str] = set()
        stack = [start]
        while stack:
            for nxt in graph.get(stack.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        assert "api.orchestrator" not in seen, f"{start} reaches api.orchestrator"
        assert "api.routers.run" not in seen, f"{start} reaches api.routers.run"


# -- C7: a write connection exists only on the run path -----------------------------------------


def _imported_db_names(tree: ast.AST) -> tuple[dict[str, str], set[str], list[str]]:
    """Resolve *tree*'s imports of the database helpers: (local name -> original symbol, local
    names bound to a ``kg_schema`` / ``portfolio_common.db`` module, ``sqlite3`` imports)."""
    aliased: dict[str, str] = {}
    module_aliases: set[str] = set()
    sqlite: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "sqlite3":
                    sqlite.append(alias.name)
                if alias.name in _DB_SOURCES:
                    module_aliases.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            source = node.module or ""
            if source.split(".")[0] == "sqlite3":
                sqlite.append(source)
            if source in _DB_SOURCES:
                for alias in node.names:
                    aliased[alias.asname or alias.name] = alias.name
    return aliased, module_aliases, sqlite


def _db_write_calls(
    tree: ast.AST, aliased: dict[str, str], module_aliases: set[str]
) -> tuple[list[tuple[str, ast.Call]], list[ast.Call]]:
    """The write-capable ``connect``/``ensure`` calls and the direct ``Database()`` calls."""
    writes: list[tuple[str, ast.Call]] = []
    database_calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            symbol = aliased.get(func.id, func.id)
            if symbol in _WRITE_SYMBOLS:
                writes.append((symbol, node))
            elif symbol == "Database":
                database_calls.append(node)
        elif (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in module_aliases
            and func.attr in _WRITE_SYMBOLS
        ):
            writes.append((func.attr, node))
    return writes, database_calls


def _db_call_sites(path: Path) -> tuple[list[tuple[str, ast.Call]], list[ast.Call], list[str]]:
    """In *path*: (write-capable ``connect``/``ensure`` calls, direct ``Database()`` calls,
    ``sqlite3`` imports). Imports are resolved with their aliases, so ``from kg_schema import
    connect as c`` counts as ``connect``, whatever the local name is."""
    tree = ast.parse(path.read_text())
    aliased, module_aliases, sqlite = _imported_db_names(tree)
    writes, database_calls = _db_write_calls(tree, aliased, module_aliases)
    return writes, database_calls, sqlite


def _is_file_write(kind: str, call: ast.Call) -> bool:
    if kind == "ensure":
        return True  # ensure runs DDL/migrations against whatever database it is handed
    first = call.args[0] if call.args else None
    return not (isinstance(first, ast.Constant) and first.value == ":memory:")


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            names.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    return names


def test_c7_write_connections_only_on_the_run_path() -> None:
    for path in sorted(_API.rglob("*.py")):
        writes, database_calls, sqlite = _db_call_sites(path)
        assert sqlite == [], f"{path} imports sqlite3: {sqlite}"
        assert database_calls == [], f"{path} constructs a Database directly"
        if path.name == "orchestrator.py":
            continue  # the run path: the one api/ module allowed a file write connection
        if path.name == "contract.py":
            # The view contract is built in a private in-memory database; its ``ensure`` runs
            # on that database, and every ``connect`` must be the literal ":memory:".
            for kind, call in writes:
                if kind == "connect":
                    assert not _is_file_write(kind, call), f"{path} connects to a file"
            continue
        assert writes == [], f"{path} opens a write-capable connection: {[k for k, _ in writes]}"


def test_c7_the_read_dependency_opens_read_only() -> None:
    deps = _API / "dependencies.py"
    writes, database_calls, sqlite = _db_call_sites(deps)
    assert (writes, database_calls, sqlite) == ([], [], [])
    get_db = next(
        node
        for node in ast.walk(ast.parse(deps.read_text()))
        if isinstance(node, ast.FunctionDef) and node.name == "get_db"
    )
    called = _called_names(get_db)
    assert "connect_ro" in called
    assert called & _WRITE_SYMBOLS == set()


# -- C12: the CLI wrapper is the same contract --------------------------------------------------


@pytest.mark.parametrize("flag", _FORBIDDEN_CLI_FLAGS)
def test_c12_cli_parser_rejects_every_preference_and_override_flag(flag: str) -> None:
    parser = orch.build_cli_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--analysis-date", "2026-10-02", "--db", "/tmp/x.db", flag])
    assert excinfo.value.code == 2  # argparse's usage error, never a silent accept


def test_c12_cli_parser_accepts_exactly_the_contract_options() -> None:
    parser = orch.build_cli_parser()
    options = {opt for action in parser._actions for opt in action.option_strings}
    assert options - {"-h", "--help"} == set(_CONTRACT_CLI_OPTIONS)
    assert options & {"-h", "--help"} == {"-h", "--help"}  # argparse's own help is the only extra


# -- C13: the CLI wrapper forwards the construction settings ------------------------------------


def test_c13_cli_wrapper_forwards_construction_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    real_new_run = orch.new_run

    def spy(kind: str, ctx: orch.RunContext, **kwargs: Any) -> Any:
        captured["ctx"] = ctx
        return real_new_run(kind, ctx, **kwargs)

    monkeypatch.setattr(orch, "new_run", spy)
    monkeypatch.setattr(orch, "execute_run", lambda *a, **k: None)
    monkeypatch.setattr(orch, "print_run", lambda *a, **k: None)

    def run_cli(db: str, extra: list[str]) -> None:
        captured.clear()
        orch.cli_main(
            [
                "--analysis-date",
                "2026-10-02",
                "--db",
                str(tmp_path / db),
                "--universe-db",
                str(tmp_path / "universe.db"),
                "--news-db",
                str(tmp_path / "news.db"),
                *extra,
            ]
        )

    run_cli(
        "fin.db",
        [
            "--top-n",
            "30",
            "--weight-scheme",
            "equal",
            "--max-name-weight",
            "0.1",
            "--max-sector-weight",
            "0.3",
        ],
    )
    ctx = captured["ctx"]
    assert (ctx.top_n, ctx.weight_scheme) == (30, "equal")
    assert (ctx.max_name_weight, ctx.max_sector_weight) == (0.1, 0.3)
    assert ctx.analysis_date == "2026-10-02"
    assert ctx.paths.db_path == tmp_path / "fin.db"

    # The CLI's defaults are the orchestrator's: unset settings stay None.
    run_cli("fin2.db", [])
    defaults = captured["ctx"]
    assert defaults.top_n is None and defaults.weight_scheme is None
    assert defaults.max_name_weight is None and defaults.max_sector_weight is None
