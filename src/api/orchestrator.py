"""The cross-module orchestrator (T-011): a step table, a runner, and a CLI wrapper.

Sequencing ``pricing_agent`` -> ``fundamental_agent`` -> ``entity_resolution`` ->
``cycle`` -> ``quant`` for one ``--analysis-date``, as the run endpoints of
``api/routers/run.py`` and the ``python -m api orchestrate`` CLI both call.

Every step runs as a subprocess built from fixed pieces: ``[sys.executable, "-m",
<package>, <command>, <literal flags...>]``. The package, the command and every flag
name are literals in :data:`_PIPELINE`; the only values that enter are the validated
request fields (an ISO date, an int, two floats, one scheme name) as separate list
items, never concatenated into a command. The preference flags (``--pin``,
``--exclude``, ...), every ``--allow-*`` override, ``--force``/``--fresh`` and the
``migrate``/``backfill``/``undo-run`` commands have no slot in the table or in
:func:`build_argv` -- they cannot be produced, not merely rejected at the request.

The API process never imports a package's run code and never opens a write-capable
connection: a step's guards (the clean-tree guard, the stale-price guard, the DQ gate
version) stay exactly as on the CLI, and the only database this module opens is a
read-only one to read a step's own run-log row back.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from portfolio_common.db import DatabaseError

from api.config import news_database_path
from kg_schema import connect_ro
from kg_schema.env import DB_ENV_VAR, database_path, universe_database_path
from kg_schema.rundate import parse_analysis_date

# A copy of ``cycle.construction.SCHEMES``, not an import: importing that package runs
# its ``__init__``, which pulls ``cycle.orchestrator`` (run code) onto ``api/``'s import
# path (T-011 D2). tests/test_orchestrator.py pins the two together.
WEIGHT_SCHEMES = ("score_tilt", "equal", "score_proportional", "inverse_vol")

_FLAG_ANALYSIS_DATE = "--analysis-date"
_FLAG_DB = "--db"
_FLAG_UNIVERSE_DB = "--universe-db"
_FLAG_NEWS_DB = "--news-db"

# Setting name -> its literal flag. The builder only ever looks a *name* up here, so no
# flag outside this table can reach an argv.
_SETTING_FLAG: dict[str, str] = {
    "top_n": "--top-n",
    "weight_scheme": "--weight-scheme",
    "max_name_weight": "--max-name-weight",
    "max_sector_weight": "--max-sector-weight",
}

_OUTPUT_TAIL_LINES = 40


@dataclass(frozen=True)
class RunPaths:
    """The three database paths a run passes to every child (D2, D7)."""

    db_path: Path
    universe_db_path: Path
    news_db_path: Path


@dataclass(frozen=True)
class RunContext:
    """One run's fixed inputs: the paths, the date and the construction settings."""

    paths: RunPaths
    analysis_date: str
    top_n: int | None = None
    weight_scheme: str | None = None
    max_name_weight: float | None = None
    max_sector_weight: float | None = None


@dataclass(frozen=True)
class StepSpec:
    """One subprocess step. ``command``/``flags``/``settings`` are the only pieces; there
    is deliberately no field for a preference or override flag (D2)."""

    name: str
    package: str
    command: tuple[str, ...]
    flags: tuple[str, ...] = ()
    settings: tuple[str, ...] = ()
    with_news_db: bool = False
    run_id_regex: str | None = None
    quant_command: str | None = None


_PIPELINE: tuple[StepSpec, ...] = (
    StepSpec(
        "pricing",
        "pricing_agent",
        ("run",),
        flags=("--store-daily", "--observations"),
        run_id_regex=r"^run (\d+):",
    ),
    StepSpec("fundamental", "fundamental_agent", ("run",), run_id_regex=r"^run (\d+):"),
    StepSpec(
        "entity_resolution",
        "entity_resolution",
        ("build",),
        with_news_db=True,
        run_id_regex=r"^cycle (\d+):",
    ),
    StepSpec(
        "cycle_select",
        "cycle",
        ("select",),
        settings=("top_n", "weight_scheme", "max_name_weight", "max_sector_weight"),
        run_id_regex=r"^select (\d+)\s",
    ),
    StepSpec("quant_actions", "quant", ("backfill-actions",), quant_command="backfill-actions"),
    StepSpec("quant_returns", "quant", ("build-returns",), quant_command="build-returns"),
    StepSpec(
        "quant_risk_model",
        "quant",
        ("build-risk-model",),
        settings=("top_n", "max_name_weight", "max_sector_weight"),
        quant_command="build-risk-model",
    ),
    StepSpec(
        "quant_optimize",
        "quant",
        ("optimize",),
        settings=("top_n", "max_name_weight", "max_sector_weight"),
        quant_command="optimize",
    ),
    StepSpec("quant_evaluate", "quant", ("evaluate",), quant_command="evaluate"),
)

_MONITOR = StepSpec("cycle_monitor", "cycle", ("monitor",), run_id_regex=r"^monitor (\d+)\s")

# Endpoint kind -> the ordered steps it runs.
_PIPELINE_KIND = "pipeline"
_SINGLE_STEPS: dict[str, tuple[StepSpec, ...]] = {
    "pricing-agent": (_PIPELINE[0],),
    "fundamental-agent": (_PIPELINE[1],),
    "entity-resolution": (_PIPELINE[2],),
    "cycle-select": (_PIPELINE[3],),
    "cycle-monitor": (_MONITOR,),
    "quant-backfill-actions": (_PIPELINE[4],),
    "quant-build-returns": (_PIPELINE[5],),
    "quant-build-risk-model": (_PIPELINE[6],),
    "quant-optimize": (_PIPELINE[7],),
    "quant-evaluate": (_PIPELINE[8],),
}


def steps_for(kind: str) -> tuple[StepSpec, ...]:
    """The ordered steps an endpoint/CLI kind runs."""
    if kind == _PIPELINE_KIND:
        return _PIPELINE
    return _SINGLE_STEPS[kind]


# -- argv ---------------------------------------------------------------------------------------


def build_argv(spec: StepSpec, ctx: RunContext) -> list[str]:
    """The exact subprocess argv for *spec*, from literals plus the validated context."""
    argv = [sys.executable, "-m", spec.package, *spec.command]
    argv += list(spec.flags)
    values: dict[str, object] = {
        "top_n": ctx.top_n,
        "weight_scheme": ctx.weight_scheme,
        "max_name_weight": ctx.max_name_weight,
        "max_sector_weight": ctx.max_sector_weight,
    }
    for key in spec.settings:  # fixed order, so an argv is reproducible
        value = values[key]
        if value is not None:
            argv += [_SETTING_FLAG[key], str(value)]
    argv += [
        _FLAG_ANALYSIS_DATE,
        ctx.analysis_date,
        _FLAG_DB,
        str(ctx.paths.db_path),
        _FLAG_UNIVERSE_DB,
        str(ctx.paths.universe_db_path),
    ]
    if spec.with_news_db:
        argv += [_FLAG_NEWS_DB, str(ctx.paths.news_db_path)]
    return argv


def _child_env(paths: RunPaths) -> dict[str, str]:
    """The child's environment: the run's three paths, spelled out (D2). Everything else
    (``LLM_*``, gateway URLs) is inherited; no secret is stored or logged."""
    env = dict(os.environ)
    env["KG_FINANCIAL_DB"] = str(paths.db_path)
    env["KG_UNIVERSE_DB"] = str(paths.universe_db_path)
    env["KG_NEWS_DB"] = str(paths.news_db_path)
    return env


@dataclass(frozen=True)
class CommandResult:
    """What :func:`run_command` returns: the exit code and the combined output."""

    returncode: int
    output: str


def run_command(argv: list[str], env: dict[str, str]) -> CommandResult:
    """The single subprocess seam -- tests replace this, never the real agents. No shell,
    no string concatenation: *argv* is a list built by :func:`build_argv`."""
    proc = subprocess.run(  # noqa: S603 - argv is a list of literals from the step table
        argv,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return CommandResult(returncode=proc.returncode, output=f"{proc.stdout}{proc.stderr}")


# -- run state ----------------------------------------------------------------------------------


@dataclass
class StepState:
    name: str
    argv: list[str]
    status: str = "pending"  # pending|running|completed|failed|skipped|not_run
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    run_id: int | None = None
    reason: str | None = None
    output_tail: list[str] = field(default_factory=list)


@dataclass
class RunState:
    handle: str
    kind: str
    analysis_date: str
    settings: dict[str, object]
    status: str = "pending"  # pending|running|completed|failed
    started_at: str | None = None
    finished_at: str | None = None
    steps: list[StepState] = field(default_factory=list)


@dataclass
class _Registry:
    """The process's run state (D4: in memory, lost on restart; one active run)."""

    runs: dict[str, RunState] = field(default_factory=dict)
    active: str | None = None


_REGISTRY = _Registry()
_LOCK = threading.Lock()


class RunInProgress(RuntimeError):
    """A second run was requested while one is active (D4)."""

    def __init__(self, handle: str) -> None:
        super().__init__(f"a run is already in progress: {handle}")
        self.handle = handle


def get_run(handle: str) -> RunState | None:
    with _LOCK:
        return _REGISTRY.runs.get(handle)


def clear_runs() -> None:
    """Test seam: drop every in-memory run (the D4 state is lost on restart)."""
    with _LOCK:
        _REGISTRY.runs.clear()
        _REGISTRY.active = None


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _tail(output: str) -> list[str]:
    return output.splitlines()[-_OUTPUT_TAIL_LINES:]


def _provided_settings(ctx: RunContext) -> dict[str, object]:
    pairs: dict[str, object] = {
        "top_n": ctx.top_n,
        "weight_scheme": ctx.weight_scheme,
        "max_name_weight": ctx.max_name_weight,
        "max_sector_weight": ctx.max_sector_weight,
    }
    return {key: value for key, value in pairs.items() if value is not None}


def new_run(kind: str, ctx: RunContext) -> RunState:
    """Build a run's initial state, with every step's argv precomputed and recorded."""
    specs = steps_for(kind)
    return RunState(
        handle=str(uuid.uuid4()),
        kind=kind,
        analysis_date=ctx.analysis_date,
        settings=_provided_settings(ctx),
        steps=[StepState(name=spec.name, argv=build_argv(spec, ctx)[1:]) for spec in specs],
    )


def _set(state: StepState, **changes: object) -> None:
    with _LOCK:
        for key, value in changes.items():
            setattr(state, key, value)


# -- run-id capture -----------------------------------------------------------------------------


def _quant_max_id(db_path: Path, *, command: str | None, after: int) -> int | None:
    """Read ``quant_run`` (read-only, bound values): the newest id, or the newest id of
    *command* greater than *after*. ``None`` when the table/file does not exist yet."""
    if not Path(db_path).exists():
        return None
    conn = connect_ro(db_path)
    try:
        if command is None:
            row = conn.execute("SELECT MAX(id) FROM quant_run").fetchone()
        else:
            row = conn.execute(
                "SELECT MAX(id) FROM quant_run WHERE id > ? AND command = ?",
                (after, command),
            ).fetchone()
    except DatabaseError:
        return None
    finally:
        conn.close()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def _capture_run_id(
    spec: StepSpec, result: CommandResult, ctx: RunContext, before: int
) -> tuple[int | None, str | None]:
    """This step's own run-log id, or ``None`` plus why when it was expected but missing."""
    if spec.run_id_regex is not None:
        match = re.search(spec.run_id_regex, result.output, re.MULTILINE)
        if match:
            return int(match.group(1)), None
        return None, "run_id not captured"
    if spec.quant_command is not None:
        found = _quant_max_id(ctx.paths.db_path, command=spec.quant_command, after=before)
        if found is not None:
            return found, None
        return None, "run_id not captured"
    return None, None


# -- the runner ---------------------------------------------------------------------------------


def _news_available(path: Path) -> bool:
    return Path(path).exists()


def _run_one(spec: StepSpec, state: StepState, ctx: RunContext) -> None:
    _set(state, status="running", started_at=_now())
    before = (
        _quant_max_id(ctx.paths.db_path, command=None, after=0)
        if spec.quant_command is not None
        else 0
    )
    result = run_command([sys.executable, *state.argv], _child_env(ctx.paths))
    _set(
        state,
        exit_code=result.returncode,
        finished_at=_now(),
        output_tail=_tail(result.output),
    )
    run_id, capture_reason = _capture_run_id(spec, result, ctx, before or 0)
    _set(state, run_id=run_id)
    if result.returncode != 0:
        _set(state, status="failed")
        if capture_reason is not None:
            _set(state, reason=capture_reason)
        return
    _set(state, status="completed")
    if capture_reason is not None:
        _set(state, reason=capture_reason)


def execute_run(run: RunState, ctx: RunContext) -> None:
    """Run *run*'s steps in order, mutating its state. The first non-zero exit stops the
    run (D5); later steps are ``not_run``. A missing news source skips only
    ``entity_resolution`` (D6)."""
    specs = steps_for(run.kind)
    with _LOCK:
        run.status = "running"
        run.started_at = _now()
    failed = False
    try:
        for spec, state in zip(specs, run.steps, strict=True):
            if failed:
                _set(state, status="not_run")
                continue
            if spec.name == "entity_resolution" and not _news_available(ctx.paths.news_db_path):
                _set(
                    state,
                    status="skipped",
                    reason=(f"no news source configured ({ctx.paths.news_db_path} not found)"),
                )
                continue
            _run_one(spec, state, ctx)
            if state.status == "failed":
                failed = True
    finally:
        with _LOCK:
            run.finished_at = _now()
            run.status = "failed" if failed else "completed"
            if run.handle == _REGISTRY.active:
                _REGISTRY.active = None


def run_now(kind: str, ctx: RunContext) -> RunState:
    """The foreground path (the CLI wrapper): build and execute in this thread."""
    run = new_run(kind, ctx)
    execute_run(run, ctx)
    return run


def start_run(kind: str, ctx: RunContext) -> RunState:
    """The HTTP path: register the run and execute it in a background thread (D4)."""
    run = new_run(kind, ctx)
    with _LOCK:
        if _REGISTRY.active is not None:
            raise RunInProgress(_REGISTRY.active)
        _REGISTRY.runs[run.handle] = run
        _REGISTRY.active = run.handle
    thread = threading.Thread(
        target=execute_run, args=(run, ctx), name=f"orchestrator-{run.handle}", daemon=True
    )
    thread.start()
    return run


# -- CLI wrapper --------------------------------------------------------------------------------


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def _weight_cap(text: str) -> float:
    value = float(text)
    if not 0.0 < value <= 1.0:
        raise argparse.ArgumentTypeError("must be in (0, 1]")
    return value


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="api orchestrate",
        description="Run the pricing -> fundamental -> entity_resolution -> cycle -> quant "
        "pipeline for one analysis date.",
    )
    parser.add_argument(
        "--analysis-date",
        dest="analysis_date",
        required=True,
        type=parse_analysis_date,
        metavar="YYYY-MM-DD",
    )
    parser.add_argument("--top-n", dest="top_n", type=_positive_int)
    parser.add_argument("--weight-scheme", dest="weight_scheme", choices=WEIGHT_SCHEMES)
    parser.add_argument("--max-name-weight", dest="max_name_weight", type=_weight_cap)
    parser.add_argument("--max-sector-weight", dest="max_sector_weight", type=_weight_cap)
    parser.add_argument("--db", help="override KG_FINANCIAL_DB path")
    parser.add_argument("--universe-db", help="override KG_UNIVERSE_DB path")
    parser.add_argument("--news-db", help="override KG_NEWS_DB (urls.db) path")
    return parser


def _seconds(started_at: str | None, finished_at: str | None) -> str:
    if not started_at or not finished_at:
        return "-"
    delta = datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)
    return f"{delta.total_seconds():.1f}s"


def print_run(run: RunState) -> None:
    """One line per step (name, status, run_id, seconds), then the failing step's tail."""
    for state in run.steps:
        run_id = "-" if state.run_id is None else str(state.run_id)
        line = (
            f"{state.name} {state.status} run_id={run_id} "
            f"{_seconds(state.started_at, state.finished_at)}"
        )
        if state.reason:
            line += f" ({state.reason})"
        print(line)
    failed = next((s for s in run.steps if s.status == "failed"), None)
    if failed is not None:
        print(f"\n{failed.name} failed (exit {failed.exit_code}); last output lines:")
        for line in failed.output_tail:
            print(f"  {line}")


def cli_main(argv: list[str]) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    db = args.db or database_path()
    if not db:
        parser.error(f"no database: pass --db or set {DB_ENV_VAR}")
    ctx = RunContext(
        paths=RunPaths(
            db_path=Path(db).expanduser(),
            universe_db_path=Path(args.universe_db or universe_database_path()).expanduser(),
            news_db_path=Path(news_database_path(args.news_db)).expanduser(),
        ),
        analysis_date=args.analysis_date,
        top_n=args.top_n,
        weight_scheme=args.weight_scheme,
        max_name_weight=args.max_name_weight,
        max_sector_weight=args.max_sector_weight,
    )
    run = run_now(_PIPELINE_KIND, ctx)
    print_run(run)
    return 0 if run.status == "completed" else 1
