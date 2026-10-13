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
from dataclasses import dataclass
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


# -- module names and imports (C6) ---------------------------------------------------------------


def _module_of(path: Path) -> tuple[str, bool]:
    """(dotted module name, is_package) for a ``*.py`` file under ``src/``."""
    parts = list(path.relative_to(_SRC).with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join(parts), is_package


def _package_of(path: Path) -> str:
    module, is_package = _module_of(path)
    return module if is_package else module.rpartition(".")[0]


def _relative_base(package: str, level: int) -> str:
    """The dotted package a relative import of *level* resolves against (Python's rule: level 1
    is *package* itself, level 2 its parent, ...)."""
    parts = package.split(".") if package else []
    keep = len(parts) - (level - 1)
    return ".".join(parts[:keep]) if keep > 0 else ""


def _import_target(package: str, node: ast.ImportFrom) -> str:
    """The absolute dotted module an ``ImportFrom`` names, relative imports resolved against
    *package* (e.g. ``from .. import orchestrator`` in ``api.routers.health`` -> ``api``)."""
    base = _relative_base(package, node.level) if node.level else ""
    if node.module:
        return f"{base}.{node.module}" if base else node.module
    return base


def _imported_modules(module: str, is_package: bool, source: str) -> set[str]:
    """Every dotted module *source* imports, absolute or relative. ``import a.b`` imports
    ``a.b``; ``from a.b import c`` imports ``a.b`` and the submodule ``a.b.c``; a relative import
    resolves against *module*'s package."""
    package = module if is_package else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            target = _import_target(package, node)
            if target:
                found.add(target)
                found.update(f"{target}.{alias.name}" for alias in node.names)
    return found


def _import_graph(modules: dict[str, tuple[bool, str]]) -> dict[str, set[str]]:
    """{module: (is_package, source)} -> {module: the modules of *modules* it imports}."""
    return {
        name: {t for t in _imported_modules(name, is_package, source) if t in modules and t != name}
        for name, (is_package, source) in modules.items()
    }


def _reachable(graph: dict[str, set[str]], start: str) -> set[str]:
    """Every module reachable from *start* in *graph*."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        for nxt in graph.get(stack.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _api_sources() -> dict[str, tuple[bool, str]]:
    sources: dict[str, tuple[bool, str]] = {}
    for path in sorted(_API.rglob("*.py")):
        name, is_package = _module_of(path)
        sources[name] = (is_package, path.read_text())
    return sources


def test_c6_no_read_router_reaches_the_run_path() -> None:
    graph = _import_graph(_api_sources())
    # The read routers: every router module except the run router. ``api.routers`` itself is the
    # registry that imports all of them (including ``run``) to build ``ALL`` -- app wiring, not a
    # router -- so the walk starts from the router modules, and the registry's own edge to the run
    # path is asserted next, so the exclusion is tested rather than assumed.
    read_routers = sorted(
        name for name in graph if name.startswith("api.routers.") and name != "api.routers.run"
    )
    assert read_routers, "expected read routers to exist"
    assert "api.routers.run" in _reachable(graph, "api.routers")
    for start in read_routers:
        seen = _reachable(graph, start)
        assert "api.orchestrator" not in seen, f"{start} reaches api.orchestrator"
        assert "api.routers.run" not in seen, f"{start} reaches api.routers.run"


def test_c6_control_follows_a_relative_import_and_an_intermediate_module() -> None:
    """The relative-import resolution and the transitive walk, on synthetic graphs."""
    # (a) a relative import from a read router resolves to the run path.
    for source in ("from .. import orchestrator\n", "from ..orchestrator import start_run\n"):
        via_relative = _import_graph(
            {"api.routers.health": (False, source), "api.orchestrator": (False, "x = 1\n")}
        )
        assert "api.orchestrator" in _reachable(via_relative, "api.routers.health"), source
    # (b) one intermediate module between the read router and the run path.
    via_middle = {
        "api.routers.health": {"api.helpers"},
        "api.helpers": {"api.orchestrator"},
        "api.orchestrator": set(),
    }
    assert "api.orchestrator" in _reachable(via_middle, "api.routers.health")


# -- C7: a write connection exists only on the run path -----------------------------------------

# The one api/ module allowed a file write connection, and the one that builds the view contract in
# a private in-memory database. Compared by path (never by file name), so api/routers/contract.py
# -- a read router -- is not confused with api/contract.py.
_RUN_PATH = "api/orchestrator.py"
_IN_MEMORY_CONTRACT = "api/contract.py"

# A call's full dotted origin, whatever the local alias: connect/ensure under kg_schema (any
# depth: ``kg_schema.connect``, ``kg_schema.db.connect``), Database under portfolio_common.
_WRITE_ORIGINS = {
    "kg_schema.connect",
    "kg_schema.db.connect",
    "kg_schema.ensure",
    "kg_schema.db.ensure",
}
_DATABASE_ORIGINS = {"portfolio_common.Database", "portfolio_common.db.Database"}
_MEMORY_CONNECT_ORIGINS = {"kg_schema.connect", "kg_schema.db.connect"}


@dataclass(frozen=True)
class _DbScan:
    """What a source's database calls resolve to. ``unsafe_writes`` are the write calls that are
    not in-memory-safe: a ``connect`` whose first argument is not the literal ``":memory:"``, or an
    ``ensure`` whose first argument is not a name bound in the same function from such a call."""

    writes: tuple[str, ...]
    databases: tuple[str, ...]
    sqlite: tuple[str, ...]
    unsafe_writes: tuple[str, ...]


def _bindings(tree: ast.AST, package: str) -> dict[str, str]:
    """{local name: full dotted origin}. ``import a.b`` binds ``a`` -> ``a``; ``import a.b as x``
    and ``from a import b as x`` bind ``x`` -> ``a.b``; ``from a.b import f as g`` binds ``g`` ->
    ``a.b.f``."""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound[alias.asname] = alias.name
                else:
                    bound[alias.name.split(".")[0]] = alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            target = _import_target(package, node)
            for alias in node.names:
                bound[alias.asname or alias.name] = (
                    f"{target}.{alias.name}" if target else alias.name
                )
    return bound


def _dotted(node: ast.expr, bound: dict[str, str]) -> str | None:
    """Resolve an expression to its dotted origin (``kdb.connect`` -> ``kg_schema.db.connect``)."""
    if isinstance(node, ast.Name):
        return bound.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value, bound)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _memory_literal(call: ast.Call) -> bool:
    first = call.args[0] if call.args else None
    return isinstance(first, ast.Constant) and first.value == ":memory:"


def _function_of(tree: ast.AST) -> dict[int, ast.FunctionDef | None]:
    """{id(node): the innermost ``FunctionDef`` enclosing it, or None at module level}."""
    enclosing: dict[int, ast.FunctionDef | None] = {}

    def visit(node: ast.AST, current: ast.FunctionDef | None) -> None:
        for child in ast.iter_child_nodes(node):
            nxt = child if isinstance(child, ast.FunctionDef) else current
            enclosing[id(child)] = nxt
            visit(child, nxt)

    visit(tree, None)
    return enclosing


def _memory_bound_names(
    tree: ast.AST, bound: dict[str, str], enclosing: dict[int, ast.FunctionDef | None]
) -> dict[int | None, set[str]]:
    """{id(function) or None: names assigned in it from a ``connect(":memory:")`` call}."""
    names: dict[int | None, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            origin = _dotted(node.value.func, bound)
            if origin in _MEMORY_CONNECT_ORIGINS and _memory_literal(node.value):
                func = enclosing.get(id(node))
                key = id(func) if func is not None else None
                names.setdefault(key, set()).update(
                    t.id for t in node.targets if isinstance(t, ast.Name)
                )
    return names


def _sqlite_imports(tree: ast.AST, package: str) -> tuple[str, ...]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(a.name for a in node.names if a.name.split(".")[0] == "sqlite3")
        elif isinstance(node, ast.ImportFrom):
            target = _import_target(package, node)
            if target.split(".")[0] == "sqlite3":
                found.append(target)
    return tuple(found)


def _scan_db_calls(source: str, package: str = "") -> _DbScan:
    """Resolve every ``connect``/``ensure`` and ``Database()`` call in *source* to its dotted
    origin, with its in-memory-safety, plus any ``sqlite3`` import."""
    tree = ast.parse(source)
    bound = _bindings(tree, package)
    enclosing = _function_of(tree)
    memory = _memory_bound_names(tree, bound, enclosing)
    writes: list[str] = []
    databases: list[str] = []
    unsafe: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        origin = _dotted(node.func, bound)
        if origin in _WRITE_ORIGINS:
            writes.append(origin)
            if origin in _MEMORY_CONNECT_ORIGINS:
                safe = _memory_literal(node)
            else:  # ensure: its first argument must name a :memory: connect in the same function
                first = node.args[0] if node.args else None
                func = enclosing.get(id(node))
                key = id(func) if func is not None else None
                safe = isinstance(first, ast.Name) and first.id in memory.get(key, set())
            if not safe:
                unsafe.append(origin)
        elif origin in _DATABASE_ORIGINS:
            databases.append(origin)
    return _DbScan(tuple(writes), tuple(databases), _sqlite_imports(tree, package), tuple(unsafe))


def test_c7_write_connections_only_on_the_run_path() -> None:
    for path in sorted(_API.rglob("*.py")):
        relative = path.relative_to(_SRC).as_posix()
        scan = _scan_db_calls(path.read_text(), _package_of(path))
        assert scan.sqlite == (), f"{relative} imports sqlite3: {scan.sqlite}"
        assert scan.databases == (), f"{relative} constructs a Database directly: {scan.databases}"
        if relative == _RUN_PATH:
            continue  # the run path: the one api/ module allowed a file write connection
        if relative == _IN_MEMORY_CONTRACT:
            assert scan.unsafe_writes == (), f"{relative} writes to a file: {scan.unsafe_writes}"
            continue
        assert scan.writes == (), f"{relative} opens a write-capable connection: {scan.writes}"


_EVASIONS = (
    'import kg_schema.db\nkg_schema.db.connect("f.db")\n',
    'from kg_schema import db as kdb\nkdb.connect("f.db")\n',
    'from portfolio_common import db\ndb.Database("f.db")\n',
    'from portfolio_common.db import Database\nDatabase("f.db")\n',
    'from kg_schema import connect as c\nc("f.db")\n',
    'import kg_schema\nkg_schema.connect("f.db")\n',
    "import kg_schema\nkg_schema.ensure(conn)\n",
    "import sqlite3\n",
)


@pytest.mark.parametrize("source", _EVASIONS)
def test_c7_control_flags_each_evasion(source: str) -> None:
    scan = _scan_db_calls(source)
    assert scan.unsafe_writes or scan.databases or scan.sqlite, source


def test_c7_control_allows_the_in_memory_contract() -> None:
    scan = _scan_db_calls(
        "import kg_schema\n"
        "def build():\n"
        '    db = kg_schema.connect(":memory:")\n'
        "    kg_schema.ensure(db, run_migrations=True)\n"
    )
    assert scan.writes == ("kg_schema.connect", "kg_schema.ensure")
    assert scan.unsafe_writes == ()


def test_c7_the_read_dependency_opens_read_only() -> None:
    deps = _API / "dependencies.py"
    source = deps.read_text()
    package = _package_of(deps)
    assert _scan_db_calls(source, package) == _DbScan((), (), (), ())
    tree = ast.parse(source)
    bound = _bindings(tree, package)
    get_db = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "get_db"
    )
    origins = {_dotted(call.func, bound) for call in ast.walk(get_db) if isinstance(call, ast.Call)}
    assert "kg_schema.connect_ro" in origins
    assert not any(origin in _WRITE_ORIGINS or origin in _DATABASE_ORIGINS for origin in origins)


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
