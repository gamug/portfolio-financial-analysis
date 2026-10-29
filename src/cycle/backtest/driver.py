"""The backtest driver: DB + cycle + quant subprocess + engine + persistence (WP-A).

``python -m cycle.backtest run --window P1 --db <scratch copy> --universe-db <ro>
--params <toml> [--warmup-steps 12] [--arms ...]``

Per window it runs, once, ``quant build-returns``; then for each decision date on the D1
21-session grid it builds the risk model (WP-B, subprocess), runs the cycle REPLAY with that
model and the same-day veto cutoff (WP-C, in-process), optimizes the two Markowitz books (WP-B,
subprocess), reads their positions, and finally simulates the four arms (WP-A engine) and
persists everything into the m018 tables. ``quant`` is never imported: its CLI is called with
``sys.executable -m quant ...`` and the last JSON line is parsed.

The database argument is refused when it resolves under ``/workspaces/thesis/data/``: the
production databases are read-only and every run works on a scratch copy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import tomllib
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from portfolio_common.db import Database

import kg_schema
from cycle import data as cycle_data
from cycle.backtest import engine
from cycle.config import CycleSettings
from cycle.guard import GuardParams, NewsCounts, NewsParams
from cycle.orchestrator import run_replay
from kg_schema import connect, connect_ro, queries, trading_calendar
from kg_schema.provenance import code_version

#: The three windows fixed by BACKTEST_SPEC §0 (inclusive end date, exclusive of E_k >= end).
WINDOWS: dict[str, tuple[str, str]] = {
    "P1": ("2022-01-03", "2022-10-12"),
    "P2": ("2025-02-19", "2025-09-23"),
    "P3": ("2023-10-27", "2024-08-02"),
}

#: D1's spacing: one execution every 21 NYSE sessions from the window's first session.
STEP_SESSIONS = 21
#: BACKTEST_SPEC §0's default warm-up.
DEFAULT_WARMUP_STEPS = 12
#: BACKTEST_SPEC §2 step 1.
RETURNS_FROM = "2019-01-02"
#: The production data root no run may write under.
DATA_ROOT = Path("/workspaces/thesis/data")
#: The last migration that rebuilds a table (m014). m015-m018 are additive/markers and are
#: created by ``ensure``, so a copy at this floor or above is safe to run on.
MIN_SCHEMA_VERSION = 14
#: P2's two reporting cuts (reporting only -- one continuous run).
P2_CUT_DATE = "2025-05-06"

BASE_ARM = engine.ARM_MARKOWITZ_BASE
NOGUARD_ARM = engine.ARM_TOPN_NOGUARD
GUARD_ARM = engine.ARM_TOPN_GUARD
GUARD_NO_NLP_ARM = engine.ARM_TOPN_GUARD_NO_NLP
GUARD_ARMS = (GUARD_ARM, GUARD_NO_NLP_ARM)
#: |sum of a book's weights - 1| tolerated before the run refuses a not-fully-invested book.
FULLY_INVESTED_TOL = 1e-4
#: WP-E's paired tests need at least this many common sessions (BACKTEST_SPEC §6).
MIN_TEST_OBS = 30


class BacktestError(RuntimeError):
    """A refusal or an integrity failure with a message for the operator."""


@dataclass(frozen=True)
class WindowSpec:
    window_id: str
    d_start: str
    d_end: str


@dataclass(frozen=True)
class Rebalance:
    decision_date: str
    execution_date: str


@dataclass(frozen=True)
class BacktestParams:
    """The params TOML, validated and with its sha256."""

    path: Path
    sha256: str
    cost_bps: float
    top_n: int
    lookback: int
    min_history: int
    cov_estimator: str
    returns_engine_version: str
    rf_curve: str
    news_db: str | None
    news_model: str | None
    shadow_from: str | None
    guard: dict[str, GuardParams]
    raw: dict[str, Any]


def require_migrated(conn: Database) -> None:
    """Refuse a scratch copy that predates the last rebuilding migration.

    ``ensure`` creates every additive table/column, but the non-additive rebuilds (m009's veto
    stints, m014's AUTOINCREMENT run tables) only run through ``migrate``. Running on an older
    copy would silently use the old shapes; name the command instead."""
    current = queries.current_version(conn)
    if current < MIN_SCHEMA_VERSION:
        raise BacktestError(
            f"the scratch copy is at schema_version {current}, below {MIN_SCHEMA_VERSION}: run "
            "`python -m fundamental_agent migrate` (or `python -m quant migrate`) on the copy "
            "first"
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def refuse_production_db(path: Path) -> None:
    """The driver never writes the production databases (BACKTEST_SPEC §7)."""
    resolved = path.expanduser().resolve()
    root = DATA_ROOT.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return
    raise BacktestError(
        f"refusing --db {resolved}: it is under {root}, which is read-only; work on a copy "
        "under /workspaces/thesis/scratch/bt/<wp>-data/"
    )


def _refuse_reused_database(
    conn: Database, warmup: Sequence[str], decisions: Sequence[Rebalance]
) -> None:
    """One backtest run = one fresh copy of the input DB (BACKTEST_SPEC §0).

    cycle keeps one REPLAY run per date and resumes it, and ``quant optimize`` overwrites the
    Top-30 book of the same as-of in place, so a second variant on the same copy would silently
    reuse the first one's ranking/book."""
    dates = sorted(set(warmup) | {rebalance.decision_date for rebalance in decisions})
    if not dates:
        return
    replay = [
        day
        for day in dates
        if conn.execute(
            "SELECT 1 FROM cycle_run WHERE cycle_type = 'REPLAY' AND cycle_date = ?",
            (day,),
        ).fetchone()
        is not None
    ]
    if replay:
        raise BacktestError(
            "cycle REPLAY run(s) already exist at "
            + ", ".join(replay)
            + "; use a fresh copy of the input database for each backtest run"
        )
    books = [
        day
        for day in dates
        if conn.execute(
            "SELECT 1 FROM quant_portfolio WHERE kind = 'min_var_topn' AND as_of = ?",
            (day,),
        ).fetchone()
        is not None
    ]
    if books:
        raise BacktestError(
            "a min_var_topn book already exists at "
            + ", ".join(books)
            + "; use a fresh copy of the input database for each backtest run"
        )


def _guard_params(raw: Mapping[str, Any], code: str) -> GuardParams:
    try:
        layers = frozenset(int(x) for x in raw["layers"])
        news_raw = raw.get("news")
        news = (
            NewsParams(
                k=int(news_raw["k"]),
                tau=float(news_raw["tau"]),
                tau_exit=float(news_raw["tau_exit"]),
                n_min=int(news_raw["n_min"]),
                cooldown=int(news_raw["cooldown"]),
                trim_fraction=float(news_raw.get("trim_fraction", 1.0)),
            )
            if news_raw is not None
            else None
        )
        return GuardParams(
            layers=layers,
            J=int(raw["J"]),
            gamma=float(raw["gamma"]),
            delta=float(raw["delta"]),
            sigma_star=float(raw["sigma_star"]),
            m=float(raw["m"]),
            alpha=float(raw["alpha"]),
            vol_window=int(raw.get("vol_window", 21)),
            periods_per_year=int(raw.get("periods_per_year", 252)),
            news=news,
        )
    except KeyError as exc:
        raise BacktestError(f"{code}: missing GuardParams key {exc}") from exc


def load_params(path: str | Path) -> BacktestParams:
    """Parse and validate the params TOML documented in ``params.example.toml``."""
    params_path = Path(path).expanduser()
    if not params_path.is_file():
        raise BacktestError(f"params file not found: {params_path}")
    raw = tomllib.loads(params_path.read_text())
    run = raw.get("run", {})
    guard_raw = raw.get("guard", {})
    arms_raw = guard_raw.get("arms", {})
    guard = {str(code): _guard_params(spec, str(code)) for code, spec in arms_raw.items()}
    if not guard:
        raise BacktestError(f"{params_path}: [guard.arms.<CODE>] defines no GUARD arm")
    return BacktestParams(
        path=params_path.resolve(),
        sha256=_sha256_file(params_path),
        cost_bps=float(run.get("cost_bps", 10.0)),
        top_n=int(run.get("top_n", 30)),
        lookback=int(run.get("lookback", 756)),
        min_history=int(run.get("min_history", 740)),
        cov_estimator=str(run.get("cov_estimator", "ledoit_wolf_cc")),
        returns_engine_version=str(run.get("returns_engine_version", "qret-v2")),
        rf_curve=str(run.get("rf_curve", "FF_RF_DAILY")),
        news_db=run.get("news_db"),
        news_model=run.get("news_model"),
        shadow_from=guard_raw.get("shadow_from"),
        guard=guard,
        raw=raw,
    )


def window_spec(window_id: str) -> WindowSpec:
    try:
        start, end = WINDOWS[window_id]
    except KeyError as exc:
        raise BacktestError(
            f"unknown window {window_id!r}: expected one of {', '.join(WINDOWS)}"
        ) from exc
    return WindowSpec(window_id, start, end)


def is_session(day: str) -> bool:
    return trading_calendar.is_trading_day(date.fromisoformat(day))


def previous_session(day: str) -> str:
    return trading_calendar.last_session_on_or_before(
        date.fromisoformat(day) - timedelta(days=1)
    ).isoformat()


def next_session(day: str) -> str:
    return trading_calendar.next_trading_day(date.fromisoformat(day)).isoformat()


def sessions_between(start: str, end: str) -> list[str]:
    """Every NYSE session in ``[start, end]``; *start* and *end* must be sessions."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if not trading_calendar.is_trading_day(first):
        raise BacktestError(f"{start} is not an NYSE session")
    if not trading_calendar.is_trading_day(last):
        raise BacktestError(f"{end} is not an NYSE session")
    out: list[str] = []
    day = first
    while day <= last:
        if trading_calendar.is_trading_day(day):
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def schedule_for(window: WindowSpec) -> list[Rebalance]:
    """D1: ``E_k = D1 + 21k`` NYSE sessions with ``E_k < D_end``; the decision is the session
    before ``E_k``. The anchor never moves: a refused early cycle moves the formation, not the
    schedule (BACKTEST_SPEC §0)."""
    anchor = date.fromisoformat(window.d_start)
    end = date.fromisoformat(window.d_end)
    out: list[Rebalance] = []
    k = 0
    while True:
        execution = trading_calendar.add_trading_days(anchor, STEP_SESSIONS * k)
        if execution >= end:
            break
        out.append(
            Rebalance(
                decision_date=previous_session(execution.isoformat()),
                execution_date=execution.isoformat(),
            )
        )
        k += 1
    return out


def warmup_dates(first_decision: str, steps: int) -> list[str]:
    """The ``W`` earlier grid dates of the same 21-session schedule, ascending."""
    if steps < 0:
        raise BacktestError("--warmup-steps must be >= 0")
    out: list[str] = []
    day = first_decision
    for _ in range(steps):
        for _ in range(STEP_SESSIONS):
            day = previous_session(day)
        out.append(day)
    return sorted(out)


# -- quant subprocess ---------------------------------------------------------------


def _last_json(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise BacktestError(f"quant printed no JSON line; stdout tail: {stdout[-1000:]}")


def run_quant(args: Sequence[str], db: Path, universe_db: Path) -> dict[str, Any]:
    """Run ``python -m quant <args>`` and return its last JSON line (WP-B's interface)."""
    command = [
        sys.executable,
        "-m",
        "quant",
        *args,
        "--db",
        str(db),
        "--universe-db",
        str(universe_db),
    ]
    # argv is our own fixed module invocation, never a shell string (the flags are validated
    # by the parser / typed above); S603's "untrusted input" does not apply.
    completed = subprocess.run(  # noqa: S603
        command, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout).strip()[-2000:]
        raise BacktestError(
            f"quant {args[0]} failed (exit {completed.returncode}) at {args}: {tail}"
        )
    return _last_json(completed.stdout)


def _model_id(payload: Mapping[str, Any]) -> int:
    try:
        return int(payload["model_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BacktestError(f"quant printed no model_id: {dict(payload)}") from exc


def _build_returns_version(payload: Mapping[str, Any], expected: str) -> None:
    """Refuse a returns build that wrote a different engine version than the params expect."""
    version = payload.get("engine_version")
    if not isinstance(version, str) or not version:
        raise BacktestError(f"quant build-returns printed no engine_version: {dict(payload)}")
    if version != expected:
        raise BacktestError(
            f"quant build-returns built {version}, but the params expect {expected}; "
            "refusing to simulate a different return series"
        )


def _min_var_portfolio_id(payload: Mapping[str, Any]) -> int:
    """Plain ``optimize``'s ``portfolio_ids`` map: read ``min_var`` by name, never position."""
    ids = payload.get("portfolio_ids")
    if not isinstance(ids, Mapping) or "min_var" not in ids:
        raise BacktestError(f"quant optimize printed no portfolio_ids['min_var']: {dict(payload)}")
    try:
        return int(ids["min_var"])
    except (TypeError, ValueError) as exc:
        raise BacktestError(
            f"quant optimize printed a non-integer portfolio_ids['min_var']: {dict(payload)}"
        ) from exc


def _topn_portfolio_id(payload: Mapping[str, Any]) -> int:
    """``optimize --cycle-run-id``'s single Top-30 book id."""
    try:
        return int(payload["portfolio_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BacktestError(f"quant optimize printed no portfolio_id: {dict(payload)}") from exc


# -- data loading ---------------------------------------------------------------------


def load_returns(
    conn: Database, engine_version: str, date_from: str, date_to: str
) -> dict[str, dict[int, float]]:
    """Simple total returns per session from ``quant_return_daily`` (the build's version)."""
    rows = conn.execute(
        "SELECT asset_id, obs_date, tr_log_return FROM quant_return_daily "
        "WHERE engine_version = ? AND obs_date >= ? AND obs_date <= ? "
        "AND tr_log_return IS NOT NULL",
        (engine_version, date_from, date_to),
    ).fetchall()
    if not rows:
        raise BacktestError(
            f"quant_return_daily has no {engine_version} rows in [{date_from}, {date_to}]: "
            "run `python -m quant build-returns` first"
        )
    out: dict[str, dict[int, float]] = {}
    for row in rows:
        out.setdefault(str(row["obs_date"]), {})[int(row["asset_id"])] = math.expm1(
            float(row["tr_log_return"])
        )
    return out


def load_rf(conn: Database, curve: str, sessions: Sequence[str]) -> tuple[dict[str, float], int]:
    """Daily RF (the stored annualized rate / 252) for each session, carried forward.

    Returns ``(by_session, carried_sessions)``. A missing curve refuses -- never a silent
    fallback to ``QuantSettings.risk_free_rate`` (WP-B's ``load-rf`` rule)."""
    rows = conn.execute(
        "SELECT rate_date, annualized_rate FROM risk_free_rate WHERE curve = ? ORDER BY rate_date",
        (curve,),
    ).fetchall()
    if not rows:
        raise BacktestError(
            f"risk_free_rate has no {curve} rows: run `python -m quant load-rf` first"
        )
    dates = [str(r["rate_date"]) for r in rows]
    daily = [float(r["annualized_rate"]) / 252.0 for r in rows]
    out: dict[str, float] = {}
    carried = 0
    for session in sessions:
        index = bisect_right(dates, session) - 1
        if index < 0:
            raise BacktestError(f"risk_free_rate has no observation on or before {session}")
        out[session] = daily[index]
        if dates[index] != session:
            carried += 1
    return out, carried


def _ticker_asset_map(
    conn: Database,
) -> tuple[dict[str, list[tuple[str, str, int]]], dict[str, int]]:
    """``symbol -> [(valid_from, valid_to, asset_id)]`` from the as-of ticker history, plus the
    canonical ``assets.ticker`` fallback."""
    by_symbol: dict[str, list[tuple[str, str, int]]] = {}
    rows = conn.execute(
        "SELECT th.symbol, th.valid_from, th.valid_to, a.id "
        "FROM ticker_history th JOIN assets a ON UPPER(a.ticker) = UPPER(th.canonical_symbol)"
    ).fetchall()
    for row in rows:
        if row["id"] is None:
            continue
        by_symbol.setdefault(str(row["symbol"]).upper(), []).append(
            (
                str(row["valid_from"]),
                str(row["valid_to"] or "9999-12-31"),
                int(row["id"]),
            )
        )
    canonical = {
        str(r["ticker"]).upper(): int(r["id"])
        for r in conn.execute("SELECT id, ticker FROM assets").fetchall()
    }
    for rows_for_symbol in by_symbol.values():
        rows_for_symbol.sort()
    return by_symbol, canonical


def _asset_for_article(
    ticker: str,
    day: str,
    by_symbol: Mapping[str, list[tuple[str, str, int]]],
    canonical: Mapping[str, int],
) -> int | None:
    key = ticker.upper()
    candidates = by_symbol.get(key, ())
    for valid_from, valid_to, asset_id in reversed(candidates):
        if valid_from <= day <= valid_to:
            return asset_id
    return canonical.get(key)


def _parse_pub_date(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _session_close(day: str) -> datetime:
    """The 16:00 ET close as an aware UTC instant (never the settle buffer: no lookahead)."""
    return trading_calendar.session_final_at(date.fromisoformat(day)) - (
        trading_calendar.SESSION_SETTLE_BUFFER
    )


def load_news(  # noqa: C901, PLR0913, PLR0917 - the reader's inputs, all named
    news_db: Path,
    news_model: str,
    finance: Database,
    sessions: Sequence[str],
    formation_index: int,
    k: int,
) -> dict[str, dict[int, NewsCounts]]:
    """Unique articles per asset in ``(close of t-k, close of t]``, from ``nlp.db`` read-only.

    ``articles.id`` is the unique-article key (the database stores no URL column; the import
    writes one row per article). The window's edges are the 16:00 ET closes, so an article
    published after a close is never read on that session."""
    first = max(0, formation_index - k)
    lower = _session_close(sessions[first])
    upper = _session_close(sessions[-1])
    connection = connect_ro(news_db)
    try:
        rows = connection.execute(
            "SELECT a.id, a.ticker, a.pub_date, s.label "
            "FROM articles a JOIN article_sentiment s ON s.article_id = a.id "
            "WHERE s.model_name = ? AND substr(a.pub_date, 1, 10) >= ? "
            "AND substr(a.pub_date, 1, 10) <= ?",
            (news_model, lower.date().isoformat(), upper.date().isoformat()),
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        raise BacktestError(
            f"nlp.db has no {news_model!r} articles in [{lower.date()}, {upper.date()}]"
        )
    by_symbol, canonical = _ticker_asset_map(finance)
    per_asset: dict[int, list[tuple[datetime, int]]] = {}
    seen: set[int] = set()
    for row in rows:
        article_id = int(row["id"])
        if article_id in seen:
            continue
        seen.add(article_id)
        published = _parse_pub_date(str(row["pub_date"]))
        if published is None:
            continue
        asset_id = _asset_for_article(
            str(row["ticker"]), published.date().isoformat(), by_symbol, canonical
        )
        if asset_id is None:
            continue
        negative = 1 if str(row["label"]).strip().lower() == "negative" else 0
        per_asset.setdefault(asset_id, []).append((published, negative))
    for rows_for_asset in per_asset.values():
        rows_for_asset.sort()
    times = {asset_id: [dt for dt, _ in rows] for asset_id, rows in per_asset.items()}
    prefix_negative = {
        asset_id: [0] + [cumulative for cumulative in _cumulative(negative for _, negative in rows)]
        for asset_id, rows in per_asset.items()
    }
    out: dict[str, dict[int, NewsCounts]] = {}
    for index in range(formation_index, len(sessions)):
        session = sessions[index]
        day_upper = _session_close(session)
        day_lower = _session_close(sessions[max(0, index - k)])
        counts: dict[int, NewsCounts] = {}
        for asset_id, asset_times in times.items():
            lo = bisect_right(asset_times, day_lower)
            hi = bisect_right(asset_times, day_upper)
            if hi <= lo:
                continue
            n_negative = prefix_negative[asset_id][hi] - prefix_negative[asset_id][lo]
            counts[asset_id] = NewsCounts(n_articles=hi - lo, n_negative=n_negative)
        if counts:
            out[session] = counts
    return out


def _cumulative(values: Iterable[int]) -> list[int]:
    total = 0
    out: list[int] = []
    for value in values:
        total += value
        out.append(total)
    return out


def load_benchmark_members(
    finance: Database,
    universe: Database,
    sessions: Sequence[str],
    formation_index: int,
) -> dict[str, list[int]]:
    """``members_asof(t-1)`` resolved to this database's asset ids, per session t."""
    out: dict[str, list[int]] = {}
    for session in sessions[formation_index:]:
        as_of = previous_session(session)
        members = queries.members_asof(universe, as_of)
        mapping, _missing = queries.resolve_asset_ids(
            finance, [(m.symbol, m.cik) for m in members], as_of=as_of
        )
        out[session] = sorted(set(mapping.values()))
    return out


# -- the pipeline ---------------------------------------------------------------------


def _selected_ids(conn: Database, cycle_run_id: int) -> set[int]:
    rows = conn.execute(
        "SELECT asset_id FROM cycle_ranking WHERE cycle_run_id = ? AND selected = 1",
        (cycle_run_id,),
    ).fetchall()
    return {int(r["asset_id"]) for r in rows}


def _cycle_completed(conn: Database, cycle_run_id: int) -> str:
    row = conn.execute("SELECT status FROM cycle_run WHERE id = ?", (cycle_run_id,)).fetchone()
    if row is None:
        raise BacktestError(f"cycle_run {cycle_run_id} does not exist")
    return str(row["status"])


def _positions(conn: Database, portfolio_id: int, label: str) -> dict[int, float]:
    rows = conn.execute(
        "SELECT asset_id, weight FROM quant_position WHERE portfolio_id = ? AND valid_to IS NULL",
        (portfolio_id,),
    ).fetchall()
    if not rows:
        raise BacktestError(f"{label}: portfolio {portfolio_id} has no open positions")
    weights = {int(r["asset_id"]): float(r["weight"]) for r in rows}
    if any(w < 0 for w in weights.values()):
        raise BacktestError(f"{label}: portfolio {portfolio_id} has a short position")
    total = sum(weights.values())
    if abs(total - 1.0) > FULLY_INVESTED_TOL:
        raise BacktestError(
            f"{label}: portfolio {portfolio_id} is not fully invested (sum {total:.9f})"
        )
    return weights


@dataclass(frozen=True)
class PipelineResult:
    formation_date: str
    targets: dict[str, dict[str, dict[int, float]]]
    rebalances: list[dict[str, Any]]
    warmup_refused: list[dict[str, str]]
    decisions_refused: list[dict[str, str]]


def _pipeline(  # noqa: C901, PLR0912, PLR0913 - spec §2's linear pipeline
    conn: Database,
    *,
    db_path: Path,
    universe_db_path: Path,
    window: WindowSpec,
    params: BacktestParams,
    schedule: Sequence[Rebalance],
    warmup: Sequence[str],
    settings: CycleSettings,
) -> PipelineResult:
    targets: dict[str, dict[str, dict[int, float]]] = {BASE_ARM: {}, NOGUARD_ARM: {}}
    rebalances: list[dict[str, Any]] = []
    warmup_refused: list[dict[str, str]] = []
    decisions_refused: list[dict[str, str]] = []
    formation: str | None = None

    for warm_date in warmup:
        try:
            run_replay(
                settings,
                warm_date,
                conn=conn,
                risk_model_id=None,
                veto_cutoff_same_day=True,
            )
        except cycle_data.TooManyUnscored as exc:
            warmup_refused.append({"decision_date": warm_date, "reason": str(exc)})
        except Exception as exc:  # name the refusing date, keep the cause
            raise BacktestError(f"cycle REPLAY failed at warm-up date {warm_date}: {exc}") from exc
    if warmup_refused:
        print(
            f"cycle.backtest: warm-up refused at {len(warmup_refused)}/{len(warmup)} dates",
            file=sys.stderr,
        )

    for index, rebalance in enumerate(schedule):
        decision = rebalance.decision_date
        model = run_quant(
            [
                "build-risk-model",
                "--analysis-date",
                decision,
                "--lookback",
                str(params.lookback),
                "--min-history",
                str(params.min_history),
                "--cov",
                params.cov_estimator,
                "--include-hard-vetoed",
            ],
            db_path,
            universe_db_path,
        )
        model_id = _model_id(model)
        try:
            report = run_replay(
                settings,
                decision,
                conn=conn,
                risk_model_id=model_id,
                veto_cutoff_same_day=True,
            )
        except cycle_data.TooManyUnscored as exc:
            if formation is not None:
                raise BacktestError(f"cycle refused after formation at {decision}: {exc}") from exc
            decisions_refused.append({"decision_date": decision, "reason": str(exc)})
            continue
        except Exception as exc:  # name the refusing date, keep the cause
            raise BacktestError(f"cycle REPLAY failed at {decision}: {exc}") from exc

        cycle_run_id = int(report.cycle_run_id)
        status = _cycle_completed(conn, cycle_run_id)
        if status != "completed":
            raise BacktestError(f"cycle_run {cycle_run_id} at {decision} is {status}")
        selected = _selected_ids(conn, cycle_run_id)
        if len(selected) != params.top_n:
            raise BacktestError(
                f"cycle_run {cycle_run_id} at {decision} selected {len(selected)} names, "
                f"expected {params.top_n}"
            )
        model_assets = queries.risk_model_asset_ids(conn, model_id)
        outside = selected - model_assets
        if outside:
            raise BacktestError(
                f"cycle_run {cycle_run_id} selected {len(outside)} names outside risk model "
                f"{model_id}: {sorted(outside)}"
            )

        base = run_quant(
            [
                "optimize",
                "--analysis-date",
                decision,
                "--objectives",
                "min_var",
                "--include-hard-vetoed",
                "--top-n",
                str(params.top_n),
            ],
            db_path,
            universe_db_path,
        )
        topn = run_quant(
            [
                "optimize",
                "--analysis-date",
                decision,
                "--objectives",
                "min_var",
                "--include-hard-vetoed",
                "--top-n",
                str(params.top_n),
                "--cycle-run-id",
                str(cycle_run_id),
                "--risk-model-id",
                str(model_id),
            ],
            db_path,
            universe_db_path,
        )
        base_portfolio = _min_var_portfolio_id(base)
        topn_portfolio = _topn_portfolio_id(topn)
        targets[BASE_ARM][decision] = _positions(conn, base_portfolio, BASE_ARM)
        targets[NOGUARD_ARM][decision] = _positions(conn, topn_portfolio, NOGUARD_ARM)

        if formation is None:
            formation = rebalance.execution_date
        rebalances.append(
            {
                "index": index,
                "decision_date": decision,
                "execution_date": rebalance.execution_date,
                "cycle_run_id": cycle_run_id,
                "risk_model_id": model_id,
                "base_portfolio_id": base_portfolio,
                "topn_portfolio_id": topn_portfolio,
            }
        )

    if formation is None:
        detail = "; ".join(r["reason"] for r in decisions_refused) or "no scheduled date"
        raise BacktestError(f"no formation: every scheduled decision was refused ({detail})")
    return PipelineResult(
        formation_date=formation,
        targets=targets,
        rebalances=rebalances,
        warmup_refused=warmup_refused,
        decisions_refused=decisions_refused,
    )


# -- persistence ----------------------------------------------------------------------


def _open_run(  # noqa: PLR0913, PLR0917 - the run row's fields, all named
    conn: Database,
    window: WindowSpec,
    params: BacktestParams,
    warmup_steps: int,
    input_sha: str,
    migrations_applied: Sequence[int],
) -> int:
    created = datetime.now(tz=UTC).isoformat(timespec="seconds")
    params_json = {
        "params_path": str(params.path),
        "params_sha256": params.sha256,
        "window": window.window_id,
        "migrations_applied": list(migrations_applied),
        "d_start": window.d_start,
        "d_end": window.d_end,
        "warmup_steps": warmup_steps,
        "cost_bps": params.cost_bps,
        "top_n": params.top_n,
        "lookback": params.lookback,
        "min_history": params.min_history,
        "cov_estimator": params.cov_estimator,
        "returns_engine_version": params.returns_engine_version,
        "rf_curve": params.rf_curve,
        "news_db": params.news_db,
        "news_model": params.news_model,
        "shadow_from": params.shadow_from,
        "guard": {
            code: {
                "layers": sorted(g.layers),
                "J": g.J,
                "gamma": g.gamma,
                "delta": g.delta,
                "sigma_star": g.sigma_star,
                "m": g.m,
                "alpha": g.alpha,
                "vol_window": g.vol_window,
                "periods_per_year": g.periods_per_year,
                "news": None
                if g.news is None
                else {
                    "k": g.news.k,
                    "tau": g.news.tau,
                    "tau_exit": g.news.tau_exit,
                    "n_min": g.news.n_min,
                    "cooldown": g.news.cooldown,
                    "trim_fraction": g.news.trim_fraction,
                },
            }
            for code, g in params.guard.items()
        },
    }
    ips_json = {
        "top_n": params.top_n,
        "name_cap": "1.5/n_held",
        "sector_cap": 0.30,
        "turnover_cap": None,
        "fully_invested_at_rebalance": True,
        "exclude_hard_vetoed": False,
    }
    cursor = conn.execute(
        "INSERT INTO backtest_run (window_id, d_start, d_end, formation_date, warmup_steps, "
        "params_json, ips_json, code_version, input_db_sha256, status, created_at) "
        "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 'running', ?)",
        (
            window.window_id,
            window.d_start,
            window.d_end,
            warmup_steps,
            json.dumps(params_json, sort_keys=True),
            json.dumps(ips_json, sort_keys=True),
            code_version(),
            input_sha,
            created,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def _update_run(conn: Database, run_id: int, *, status: str, extra: Mapping[str, Any]) -> None:
    row = conn.execute("SELECT params_json FROM backtest_run WHERE id = ?", (run_id,)).fetchone()
    params_json = json.loads(str(row["params_json"])) if row else {}
    params_json.update(extra)
    conn.execute(
        "UPDATE backtest_run SET status = ?, params_json = ? WHERE id = ?",
        (status, json.dumps(params_json, sort_keys=True), run_id),
    )
    conn.commit()


def _insert_books(conn: Database, run_id: int, arms: Sequence[engine.ArmSpec]) -> dict[str, int]:
    ids: dict[str, int] = {}
    for arm in arms:
        guard_layers = None if arm.guard is None else json.dumps(sorted(arm.guard.layers))
        cursor = conn.execute(
            "INSERT INTO strategy_book (backtest_run_id, arm_code, objective, "
            "exclude_hard_vetoed, guard_layers) VALUES (?, ?, ?, ?, ?)",
            (
                run_id,
                arm.code,
                "min_var" if arm.code != engine.BENCHMARK else "equal_weight",
                0,
                guard_layers,
            ),
        )
        ids[arm.code] = int(cursor.lastrowid or 0)
    conn.commit()
    return ids


def _insert_rebalances(
    conn: Database,
    book_ids: Mapping[str, int],
    rebalances: Sequence[Mapping[str, Any]],
) -> None:
    for rebalance in rebalances:
        rows = [
            (
                book_ids[BASE_ARM],
                rebalance["decision_date"],
                rebalance["execution_date"],
                rebalance["base_portfolio_id"],
                None,
                rebalance["risk_model_id"],
                "executed",
            ),
            (
                book_ids[NOGUARD_ARM],
                rebalance["decision_date"],
                rebalance["execution_date"],
                rebalance["topn_portfolio_id"],
                rebalance["cycle_run_id"],
                rebalance["risk_model_id"],
                "executed",
            ),
        ]
        for guard_arm in GUARD_ARMS:
            if guard_arm in book_ids:
                rows.append(
                    (
                        book_ids[guard_arm],
                        rebalance["decision_date"],
                        rebalance["execution_date"],
                        rebalance["topn_portfolio_id"],
                        rebalance["cycle_run_id"],
                        rebalance["risk_model_id"],
                        "executed",
                    )
                )
        conn.executemany(
            "INSERT INTO book_rebalance (book_id, decision_date, execution_date, "
            "quant_portfolio_id, cycle_run_id, risk_model_id, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    conn.commit()


def _tickers(conn: Database, asset_ids: Iterable[int]) -> dict[int, str]:
    ids = sorted({int(a) for a in asset_ids})
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT id, ticker FROM assets WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(ids),),
    ).fetchall()
    return {int(r["id"]): str(r["ticker"]) for r in rows}


def _insert_weights(
    conn: Database,
    book_id: int,
    result: engine.ArmResult,
    tickers: Mapping[int, str],
) -> None:
    rows: list[tuple[Any, ...]] = []
    for day in result.weights:
        keys = (
            set(day.markowitz_target)
            | set(day.drifted)
            | set(day.pre_guard)
            | set(day.final_target)
        )
        for asset_id in sorted(keys):
            rows.append(
                (
                    book_id,
                    day.date,
                    tickers.get(asset_id, str(asset_id)),
                    asset_id,
                    day.markowitz_target.get(asset_id, 0.0),
                    day.drifted.get(asset_id, 0.0),
                    day.pre_guard.get(asset_id, 0.0),
                    day.final_target.get(asset_id, 0.0),
                )
            )
        rows.append(
            (
                book_id,
                day.date,
                "CASH",
                None,
                max(0.0, 1.0 - sum(day.markowitz_target.values())),
                max(0.0, 1.0 - sum(day.drifted.values())),
                max(0.0, 1.0 - sum(day.pre_guard.values())),
                day.cash_weight,
            )
        )
    conn.executemany(
        "INSERT INTO book_weight_daily (book_id, date, holding_key, asset_id, "
        "markowitz_target_weight, drifted_weight, pre_guard_weight, final_target_weight) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def _insert_performance(
    conn: Database,
    book_id: int,
    result: engine.ArmResult,
    benchmark: Mapping[str, float],
) -> None:
    conn.executemany(
        "INSERT INTO book_daily_performance (book_id, date, gross_return, turnover, "
        "cost_return, net_return, nav, drawdown, cash_weight, n_equity, benchmark_return) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                book_id,
                day.date,
                day.gross_return,
                day.turnover,
                day.cost_return,
                day.net_return,
                day.nav,
                day.drawdown,
                day.cash_weight,
                day.n_equity,
                benchmark.get(day.date),
            )
            for day in result.performance
        ],
    )
    conn.commit()


def _insert_guard(
    conn: Database,
    run_id: int,
    book_ids: Mapping[str, int],
    results: Mapping[str, engine.ArmResult],
) -> None:
    alert_ids: dict[tuple[Any, ...], int] = {}
    actions: list[tuple[Any, ...]] = []
    for code, result in results.items():
        if code not in book_ids:
            continue
        book_id = book_ids[code]
        for event in result.guard_events:
            alert = event.alert
            key = (
                event.date,
                alert.layer,
                alert.asset_id,
                alert.rule_id,
                alert.state,
                round(alert.value, 12),
                round(alert.threshold, 12),
            )
            alert_id = alert_ids.get(key)
            if alert_id is None:
                cursor = conn.execute(
                    "INSERT INTO guard_alert (backtest_run_id, date, layer, asset_id, "
                    "rule_id, value, threshold, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        event.date,
                        alert.layer,
                        alert.asset_id,
                        alert.rule_id,
                        alert.value,
                        alert.threshold,
                        alert.state,
                    ),
                )
                alert_id = int(cursor.lastrowid or 0)
                alert_ids[key] = alert_id
            actions.append(
                (
                    book_id,
                    alert_id,
                    event.date,
                    next_session(event.date),
                    alert.asset_id,
                    event.status,
                    event.weight_before,
                    event.weight_after,
                )
            )
    conn.executemany(
        "INSERT INTO guard_action (book_id, alert_id, decision_date, effective_date, "
        "asset_id, status, weight_before, weight_after) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        actions,
    )
    conn.commit()


def _segment_dates(window: WindowSpec, formation: str) -> list[tuple[str, str, str]]:
    segments = [(window.window_id, max(window.d_start, formation), window.d_end)]
    if window.window_id == "P2":
        segments.extend(
            [
                ("P2a", max(window.d_start, formation), P2_CUT_DATE),
                ("P2b", max(next_session(P2_CUT_DATE), formation), window.d_end),
            ]
        )
    return segments


def _summaries_and_tests(  # noqa: PLR0913, PLR0917 - the run's outputs, all named
    conn: Database,
    run_id: int,
    window: WindowSpec,
    book_ids: Mapping[str, int],
    results: Mapping[str, engine.ArmResult],
    rebalances: Sequence[Mapping[str, Any]],
    rf: Mapping[str, float],
) -> None:
    from cycle.backtest import stats  # noqa: PLC0415 - the summaries and tests below

    segments = _segment_dates(window, next(iter(results.values())).performance[0].date)
    by_book_dates = {
        code: {day.date: day for day in result.performance} for code, result in results.items()
    }
    for code, result in results.items():
        book_id = book_ids.get(code)
        if book_id is None:
            continue
        for segment, date_from, date_to in segments:
            days = [p for p in result.performance if date_from <= p.date <= date_to]
            if not days:
                continue
            summary = stats.summarize(
                [p.net_return for p in days],
                [rf[p.date] for p in days],
                [p.turnover for p in days],
                [p.cost_return for p in days],
            )
            n_rebalances = sum(
                1 for r in rebalances if date_from <= str(r["execution_date"]) <= date_to
            )
            n_actions = sum(
                1
                for e in result.guard_events
                if e.status == "APPLIED" and date_from <= e.date <= date_to
            )
            conn.execute(
                "INSERT INTO backtest_summary (book_id, segment, date_from, date_to, "
                "cumulative_return, annualized_return, annualized_vol, ex_post_sharpe, "
                "max_drawdown, total_turnover, total_cost_bps, n_rebalances, "
                "n_guard_actions) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    book_id,
                    segment,
                    days[0].date,
                    days[-1].date,
                    summary["cumulative_return"],
                    summary["annualized_return"],
                    summary["annualized_vol"],
                    summary["ex_post_sharpe"],
                    summary["max_drawdown"],
                    summary["total_turnover"],
                    summary["total_cost_bps"],
                    n_rebalances,
                    n_actions,
                ),
            )

    pairs = _test_pairs(set(results))
    for segment, date_from, date_to in segments:
        for arm_a, arm_b in pairs:
            dates = sorted(set(by_book_dates[arm_a]) & set(by_book_dates[arm_b]))
            dates = [d for d in dates if date_from <= d <= date_to]
            if len(dates) < MIN_TEST_OBS:
                continue
            ra = [by_book_dates[arm_a][d].net_return for d in dates]
            rb = [by_book_dates[arm_b][d].net_return for d in dates]
            excess_rf = [rf[d] for d in dates]
            z_value, p_value = stats.memmel_jk(ra, rb, excess_rf)
            conn.execute(
                "INSERT INTO backtest_test (backtest_run_id, segment, arm_a, arm_b, test, "
                "statistic, p_value, n_obs, params_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, segment, arm_a, arm_b, "memmel_jk", z_value, p_value, len(dates), None),
            )
            diff, lw_p = stats.ledoit_wolf_sharpe_diff(ra, rb, excess_rf)
            conn.execute(
                "INSERT INTO backtest_test (backtest_run_id, segment, arm_a, arm_b, test, "
                "statistic, p_value, n_obs, params_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    segment,
                    arm_a,
                    arm_b,
                    "ledoit_wolf",
                    diff,
                    lw_p,
                    len(dates),
                    json.dumps({"block": 5, "n_boot": 4999, "seed": 20261028}),
                ),
            )
            t_value, hac_p = stats.hac_mean_diff(ra, rb)
            conn.execute(
                "INSERT INTO backtest_test (backtest_run_id, segment, arm_a, arm_b, test, "
                "statistic, p_value, n_obs, params_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, segment, arm_a, arm_b, "hac_mean_diff", t_value, hac_p, len(dates), None),
            )
    conn.commit()


def _test_pairs(codes: set[str]) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    if GUARD_ARM in codes:
        candidates.append((GUARD_ARM, NOGUARD_ARM))
    if GUARD_NO_NLP_ARM in codes:
        candidates.append((GUARD_NO_NLP_ARM, NOGUARD_ARM))
    if NOGUARD_ARM in codes and BASE_ARM in codes:
        candidates.append((NOGUARD_ARM, BASE_ARM))
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pair in candidates:
        if pair in seen or (pair[1], pair[0]) in seen:
            continue
        seen.add(pair)
        pairs.append(pair)
    return pairs


def resolve_arms(requested: Sequence[str] | None, params: BacktestParams) -> list[str]:
    """The arms a run simulates. ``None`` = BASE + NOGUARD + every guard arm the params define."""
    known = {BASE_ARM, NOGUARD_ARM, GUARD_ARM, GUARD_NO_NLP_ARM}
    if requested is None:
        arms = [BASE_ARM, NOGUARD_ARM] + [c for c in GUARD_ARMS if c in params.guard]
    else:
        arms = [code.strip() for code in requested if code.strip()]
    unknown = [code for code in arms if code not in known]
    if unknown:
        raise BacktestError(f"unknown arm(s) {unknown}; expected {sorted(known)}")
    for required in (BASE_ARM, NOGUARD_ARM):
        if required not in arms:
            raise BacktestError(f"{required} is required (it is the comparison baseline)")
    for code in arms:
        if code in GUARD_ARMS and code not in params.guard:
            raise BacktestError(f"{code} has no [guard.arms.{code}] in the params")
    return arms


def _arm_specs(
    codes: Sequence[str], params: BacktestParams, shadow_code: str | None
) -> list[engine.ArmSpec]:
    specs = [engine.ArmSpec(BASE_ARM, BASE_ARM), engine.ArmSpec(NOGUARD_ARM, NOGUARD_ARM)]
    shadow = params.guard.get(shadow_code) if shadow_code else None
    specs[1] = engine.ArmSpec(NOGUARD_ARM, NOGUARD_ARM, shadow_guard=shadow)
    for code in GUARD_ARMS:
        if code in codes:
            specs.append(engine.ArmSpec(code, NOGUARD_ARM, guard=params.guard[code]))
    return specs


# -- the run --------------------------------------------------------------------------


def run_backtest(  # noqa: PLR0913, PLR0915 - the run's inputs, one orchestration
    *,
    window_id: str,
    db_path: str | Path,
    universe_db_path: str | Path,
    params_path: str | Path,
    warmup_steps: int = DEFAULT_WARMUP_STEPS,
    arms: Sequence[str] | None = None,
) -> dict[str, Any]:
    window = window_spec(window_id)
    database = Path(db_path).expanduser().resolve()
    refuse_production_db(database)
    if not database.is_file():
        raise BacktestError(f"--db {database} does not exist (use a scratch copy)")
    universe = Path(universe_db_path).expanduser().resolve()
    if not universe.is_file():
        raise BacktestError(f"--universe-db {universe} does not exist")
    params = load_params(params_path)
    codes = resolve_arms(arms, params)
    schedule = schedule_for(window)
    if not schedule:
        raise BacktestError(f"window {window_id} has no execution date")
    warmup = warmup_dates(schedule[0].decision_date, warmup_steps)

    input_sha = _sha256_file(database)
    conn = connect(database)
    universe_conn = connect_ro(universe)
    try:
        # The scratch copy may predate the current migration floor: migrate it here (recorded
        # in params_json) rather than running on old table shapes.
        applied = kg_schema.ensure(conn, run_migrations=True)
        require_migrated(conn)
        _refuse_reused_database(conn, warmup, schedule)
        run_id = _open_run(conn, window, params, warmup_steps, input_sha, applied)
        payload: dict[str, Any] = {}
        try:
            returns_report = run_quant(
                ["build-returns", "--from", RETURNS_FROM, "--to", window.d_end],
                database,
                universe,
            )
            _build_returns_version(returns_report, params.returns_engine_version)
            settings = CycleSettings(
                db_path=database,
                universe_db_path=universe,
                top_n=params.top_n,
                max_sector_weight=0.30,
            )
            pipeline = _pipeline(
                conn,
                db_path=database,
                universe_db_path=universe,
                window=window,
                params=params,
                schedule=schedule,
                warmup=warmup,
                settings=settings,
            )
            sessions = _engine_sessions(pipeline.formation_date, window.d_end, params)
            formation_index = sessions.index(pipeline.formation_date)

            shadow_code = params.shadow_from or (
                GUARD_ARM if GUARD_ARM in codes else GUARD_NO_NLP_ARM
            )
            if shadow_code not in codes:
                shadow_code = GUARD_ARM if GUARD_ARM in codes else GUARD_NO_NLP_ARM
            notes: list[str] = []
            news: dict[str, dict[int, Any]] = {}
            needs_news = any(
                engine.NEWS_LAYER in params.guard[code].layers
                for code in codes
                if code in GUARD_ARMS
            )
            if needs_news:
                news_map, reason = _resolve_news(params, conn, sessions, formation_index, codes)
                if news_map is not None:
                    news = news_map
                else:
                    notes.append(f"layer 2 off: {reason}; {GUARD_ARM} not run")
                    codes = [code for code in codes if code != GUARD_ARM]
                    if shadow_code == GUARD_ARM:
                        shadow_code = GUARD_NO_NLP_ARM
            specs = _arm_specs(codes, params, shadow_code)

            returns = load_returns(conn, params.returns_engine_version, sessions[0], window.d_end)
            rf, carried = load_rf(conn, params.rf_curve, sessions)
            members = load_benchmark_members(conn, universe_conn, sessions, formation_index)
            engine_input = engine.EngineInput(
                sessions=sessions,
                returns=returns,
                rf=rf,
                targets=pipeline.targets,
                arms=specs,
                formation_date=pipeline.formation_date,
                decision_dates=[d for d in pipeline.targets[BASE_ARM] if d >= sessions[0]],
                cost_bps=params.cost_bps,
                benchmark_members=members,
                news=news,
            )
            simulated = engine.simulate(engine_input)

            book_ids = _insert_books(conn, run_id, specs)
            _insert_rebalances(conn, book_ids, pipeline.rebalances)
            all_assets = {
                asset_id
                for result in simulated.arms.values()
                for day in result.weights
                for asset_id in set(day.markowitz_target)
                | set(day.drifted)
                | set(day.pre_guard)
                | set(day.final_target)
            }
            tickers = _tickers(conn, all_assets)
            for code, result in simulated.arms.items():
                _insert_weights(conn, book_ids[code], result, tickers)
                _insert_performance(conn, book_ids[code], result, simulated.benchmark)
            _insert_guard(conn, run_id, book_ids, simulated.arms)
            _summaries_and_tests(
                conn,
                run_id,
                window,
                book_ids,
                simulated.arms,
                pipeline.rebalances,
                rf,
            )
            payload = {
                "backtest_run_id": run_id,
                "window_id": window.window_id,
                "formation_date": pipeline.formation_date,
                "books": book_ids,
                "status": "completed",
            }
            _update_run(
                conn,
                run_id,
                status="completed",
                extra={
                    "formation_date": pipeline.formation_date,
                    "arms": [spec.code for spec in specs],
                    "warmup_refused": pipeline.warmup_refused,
                    "decisions_refused": pipeline.decisions_refused,
                    "rf_carried_sessions": carried,
                    "notes": notes,
                },
            )
            conn.execute(
                "UPDATE backtest_run SET formation_date = ? WHERE id = ?",
                (pipeline.formation_date, run_id),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            _update_run(conn, run_id, status="failed", extra={})
            raise
        return payload
    finally:
        universe_conn.close()
        conn.close()


def _engine_sessions(formation: str, d_end: str, params: BacktestParams) -> list[str]:
    """Sessions from ``formation - max(J, vol_window)`` to ``d_end``: the engine's lookbacks
    need that much history for layer 1 / layer 3."""
    longest = max([max(g.J, g.vol_window) for g in params.guard.values()] or [1])
    start = formation
    for _ in range(longest + 2):
        start = previous_session(start)
    return sessions_between(start, d_end)


def _resolve_news(
    params: BacktestParams,
    conn: Database,
    sessions: Sequence[str],
    formation_index: int,
    codes: Sequence[str],
) -> tuple[dict[str, dict[int, Any]] | None, str]:
    if not params.news_db or not params.news_model:
        return None, "no news_db / news_model in the params"
    path = Path(params.news_db).expanduser()
    if not path.is_file():
        return None, f"news_db {path} does not exist"
    windows = [
        news.k
        for code in codes
        if code in GUARD_ARMS
        for news in (params.guard[code].news,)
        if news is not None
    ]
    if not windows:
        return None, "no GUARD arm carries news parameters"
    k = max(windows)
    try:
        news = load_news(path, params.news_model, conn, sessions, formation_index, k)
    except BacktestError as exc:
        return None, str(exc)
    if not news:
        return None, f"no article mapped to a held asset in the window ({params.news_model})"
    return news, ""


# -- CLI ------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cycle.backtest", description="Run one backtest window (WP-A driver)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run one window and persist its books")
    run.add_argument("--window", required=True, choices=sorted(WINDOWS))
    run.add_argument("--db", required=True, help="a scratch copy of the financial database")
    run.add_argument("--universe-db", required=True, help="the read-only universe database")
    run.add_argument("--params", required=True, help="the params TOML")
    run.add_argument("--warmup-steps", type=int, default=DEFAULT_WARMUP_STEPS)
    run.add_argument(
        "--arms",
        default=None,
        help="comma-separated arm codes (default: BASE, NOGUARD and every GUARD arm the "
        "params define)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    arms = args.arms.split(",") if args.arms else None
    try:
        result = run_backtest(
            window_id=args.window,
            db_path=args.db,
            universe_db_path=args.universe_db,
            params_path=args.params,
            warmup_steps=args.warmup_steps,
            arms=arms,
        )
    except BacktestError as exc:
        print(f"cycle.backtest: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
