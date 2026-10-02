"""Batch driver for the pricing collector.

This is NOT an integration pipeline -- it only fills the ``price_window`` /
``price_daily`` tables. Cross-module orchestration lives on a separate branch.

One :func:`run` makes a single daily-candles request per ticker for the whole date
range, then derives the ``full`` window summary (and per-calendar-year summaries with
``--by-year``). Windows already stored are skipped, so re-runs resume.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from portfolio_common.db import Database
from tqdm import tqdm

from kg_schema import connect, rundate
from kg_schema.provenance import DirtyTree, code_version, dirty_tree_reason
from kg_schema.queries import UniverseMember, connect_ro, members_asof
from kg_schema.trading_calendar import last_final_session, session_is_open_or_pending
from pricing_agent import db
from pricing_agent.config import Settings
from pricing_agent.db import PriceWindowRow, RunError
from pricing_agent.integrity import (
    Jump,
    describe,
    matches_recorded_split,
    split_shaped_jumps,
)
from pricing_agent.observations import build_observations
from pricing_agent.pricing_client import Candle, DailyPrices, PricingClient
from pricing_agent.stats import WindowStats, slice_year, summarize

DEFAULT_START_DATE = "2022-01-01"
FULL_LABEL = "full"

_Window = tuple[str, str, str, str]  # (ticker, start, end, label)


class SessionNotClosed(RuntimeError):
    """The run's end date is a trading day whose session has not closed (T-131): its bar would
    be a partial intraday one, and the observation/return rows derived from it are permanent."""


class _SplitJumpRefused(RuntimeError):
    """A series with a split-shaped close jump this run could not explain (T-131)."""


@dataclass
class RunParams:
    start_date: str = DEFAULT_START_DATE
    end_date: str | None = None  # clamped to analysis_date; defaults to analysis_date
    limit: int | None = None
    tickers: Sequence[str] | None = None
    by_year: bool = False
    store_daily: bool = False
    observations: bool = False
    fresh: bool = False
    refresh_universe: bool = False  # accepted for back-compat; no longer meaningful
    allow_split_jumps: bool = False  # T-131: store a series despite an unexplained x2/x0.5 jump
    analysis_date: str = field(default_factory=rundate.today)

    def resolved_end(self) -> str:
        """The fetch/no-lookahead upper bound: ``--end`` clamped to ``analysis_date``."""
        return min(self.end_date, self.analysis_date) if self.end_date else self.analysis_date

    def years(self) -> list[int]:
        if not self.by_year:
            return []
        return list(range(int(self.start_date[:4]), int(self.resolved_end()[:4]) + 1))


@dataclass
class RunReport:
    run_id: int
    planned: int
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    dirty_tree_bypassed: str | None = None  # T-114: why, if --allow-dirty overrode it
    full_refetches: list[str] = field(default_factory=list)  # T-131: tickers re-fetched in full


@dataclass(frozen=True)
class _Task:
    asset_id: int
    ticker: str


@dataclass
class _Engine:
    conn: Database
    client: PricingClient
    params: RunParams
    report: RunReport
    completed: set[_Window]


def run(settings: Settings, params: RunParams) -> RunReport:
    if params.observations and not params.store_daily:
        raise ValueError(
            "observations needs store_daily: price_observation rows are per-day analytics "
            "over a specific price_daily bar, and writing them without also storing that bar "
            "leaves an orphan observation date (T-110)"
        )
    _require_closed_session(params.resolved_end())
    conn = connect(settings.db_path)
    try:
        db.ensure_schema(conn)
        cv = code_version()
        dirty_reason = dirty_tree_reason(cv)
        if dirty_reason is not None and not settings.allow_dirty:
            raise DirtyTree(
                f"{dirty_reason}; pass --allow-dirty for a deliberate run from an uncommitted tree"
            )
        members = _load_members(settings, params.analysis_date)
        db.sync_universe(conn, members)
        symbols = [m.symbol for m in members]
        with PricingClient(settings.pricing_base_url) as client:
            assets = db.load_universe(
                conn, tickers=params.tickers, symbols=symbols, limit=params.limit
            )
            if not assets:
                raise RuntimeError(
                    f"no S&P 500 members as of {params.analysis_date} resolved to an asset row"
                )
            tasks = [_Task(int(a["id"]), str(a["ticker"])) for a in assets]
            completed: set[_Window] = set() if params.fresh else db.completed_windows(conn)

            run_id = db.start_run(
                conn,
                params_json=_params_json(params, dirty_reason),
                as_of=params.analysis_date,
                code_version=cv,
            )
            db.update_run_plan(conn, run_id, universe_size=len(assets), planned_units=len(tasks))
            report = RunReport(run_id=run_id, planned=len(tasks), dirty_tree_bypassed=dirty_reason)
            engine = _Engine(conn, client, params, report, completed)

            try:
                bar = tqdm(tasks, desc="s&p 500 pricing", unit="ticker")
                for task in bar:
                    bar.set_postfix_str(task.ticker)
                    _run_task(engine, task)
            except BaseException:  # an interrupt too: never leave the run log 'running'
                conn.rollback()  # drop any half-written statement before recording the outcome
                db.finish_run(conn, run_id, status="failed")
                raise

            db.finish_run(conn, run_id, status="completed")
            return report
    finally:
        conn.close()


def _require_closed_session(end: str) -> None:
    """Refuse an *end* whose session is still open or not yet started (T-131).

    The 2026-09-29 runs stored a 10:04 ET bar as that day's close (363M shares across 503 names
    against a normal 2.7-3.4B); the price bar heals on the next fetch but the observation and
    return rows built from it never do. A weekend or holiday end is fine -- it has no session."""
    now = rundate.now()
    if session_is_open_or_pending(date.fromisoformat(end), now):
        raise SessionNotClosed(
            f"{end} is an NYSE session that has not closed and settled yet (now {now:%Y-%m-%d %H:%M} "
            f"UTC), so its bar would be partial; the latest final session is "
            f"{last_final_session(now).isoformat()} -- pass --analysis-date/--end no later than it"
        )


def _load_members(settings: Settings, analysis_date: str) -> list[UniverseMember]:
    """The S&P 500 constituents as of *analysis_date*, from ``universe.db``."""
    uconn = connect_ro(settings.universe_db_path)
    try:
        members = members_asof(uconn, analysis_date)
    finally:
        uconn.close()
    if not members:
        raise RuntimeError(
            f"universe.db ({settings.universe_db_path}) has no members as of {analysis_date}"
        )
    tqdm.write(f"universe: {len(members)} S&P 500 members as of {analysis_date}")
    return members


def _expected_labels(params: RunParams) -> set[str]:
    return {FULL_LABEL, *(str(year) for year in params.years())}


def _run_task(engine: _Engine, task: _Task) -> None:
    params, report, conn = engine.params, engine.report, engine.conn
    start, end = params.start_date, params.resolved_end()
    wanted = _expected_labels(params)
    have = {label for label in wanted if (task.ticker, start, end, label) in engine.completed}
    if not params.fresh and wanted <= have and not params.store_daily and not params.observations:
        report.skipped += 1
        db.bump_run_counter(conn, report.run_id, "skipped_units")
        return

    try:
        prices = engine.client.daily_any_spelling(task.ticker, start, end)
    except Exception as exc:  # a dead ticker must not stop the batch
        report.failed += 1
        report.errors.append(f"{task.ticker}: {exc}")
        db.bump_run_counter(conn, report.run_id, "failed_units")
        db.record_error(conn, report.run_id, RunError(task.ticker, None, "fetch", str(exc)))
        return

    if prices.is_empty:
        report.skipped += 1
        db.bump_run_counter(conn, report.run_id, "skipped_units")
        db.record_error(
            conn,
            report.run_id,
            RunError(task.ticker, None, "no_data", prices.warning or "no candles"),
        )
        return

    try:
        _store(engine, task, prices)
    except _SplitJumpRefused as exc:  # nothing was written for this ticker
        report.failed += 1
        report.errors.append(f"{task.ticker}: {exc}")
        db.bump_run_counter(conn, report.run_id, "failed_units")
        db.record_error(conn, report.run_id, RunError(task.ticker, None, "split_jump", str(exc)))
        return
    report.completed += 1
    db.bump_run_counter(conn, report.run_id, "completed_units")


def _store(engine: _Engine, task: _Task, prices: DailyPrices) -> None:
    params, conn = engine.params, engine.conn
    start, end = params.start_date, params.resolved_end()
    run_id = engine.report.run_id

    # No lookahead: drop any candle dated after the analysis date.
    candles = [c for c in prices.candles if c.date <= end]
    # What reaches price_daily: the fetched window, or the full history if it had to be re-fetched.
    # Decided before any write, so a refused series leaves the ticker untouched.
    to_store = _reconcile_history(engine, task, candles) if params.store_daily else candles

    windows: list[tuple[str, WindowStats]] = []
    full = summarize(candles)
    if full is not None:
        windows.append((FULL_LABEL, full))
    for year in params.years():
        year_stats = summarize(slice_year(candles, year))
        if year_stats is not None:
            windows.append((str(year), year_stats))

    for label, stats in windows:
        db.upsert_price_window(
            conn,
            PriceWindowRow(
                asset_id=task.asset_id,
                start_date=start,
                end_date=end,
                label=label,
                stats=stats,
                source=prices.source,
                warning=prices.warning,
            ),
            run_id=run_id,
        )
    if params.store_daily:
        db.replace_daily_prices(conn, task.asset_id, to_store, run_id=run_id)
    if params.observations:
        # From the asset's whole stored history, not this run's window: a 252-day momentum needs
        # 252 earlier bars, and a refresh over a few weeks left those fields NULL (T-131).
        history = db.load_daily_candles(conn, task.asset_id, end=end)
        db.upsert_price_observations(
            conn,
            task.asset_id,
            build_observations(history, engine_version=db.PRICE_OBSERVATION_ENGINE_VERSION),
            run_id=run_id,
        )


def _reconcile_history(engine: _Engine, task: _Task, fetched: list[Candle]) -> list[Candle]:
    """The candles to store for *task*, after checking they join the stored history cleanly.

    The gateway adjusts history for a split as of the fetch date, so a window fetched after a
    split sits on a different basis from older stored rows. Two ways that is caught (T-131):

    * a recorded split postdates a bar stored before it (:func:`db.has_pre_split_rows`);
    * the stored-plus-fetched series has a split-shaped close jump at the seam that matches a
      recorded split's ratio.

    Either re-fetches the asset's full history once, so every bar is on the same basis. A
    split-shaped jump that is still there afterwards -- or that no recorded split explains -- is
    refused rather than stored, unless ``allow_split_jumps`` accepts it as a real move."""
    params, conn = engine.params, engine.conn
    end = params.resolved_end()
    stored = db.load_daily_candles(conn, task.asset_id, end=end)
    splits = db.recorded_split_values(conn, task.asset_id, end=end)

    jumps = _seam_jumps(stored, fetched)
    if db.has_pre_split_rows(conn, task.asset_id, end=end) or any(
        matches_recorded_split(j, splits) for j in jumps
    ):
        fetched = _refetch_full(engine, task, stored, fetched)
        jumps = _seam_jumps(stored, fetched)
    if jumps and not params.allow_split_jumps:
        why = (
            "a recorded split matches it but the gateway still returns it unadjusted"
            if any(matches_recorded_split(j, splits) for j in jumps)
            else "no recorded split explains it (run `quant backfill-actions` if one is missing)"
        )
        raise _SplitJumpRefused(
            f"split-shaped close jump in the series to store: {describe(jumps)}; {why}. "
            "Refused so a mis-adjusted series is not stored; --allow-split-jumps accepts a real move"
        )
    return fetched


def _seam_jumps(stored: list[Candle], fetched: list[Candle]) -> list[Jump]:
    """Split-shaped jumps in the stored series with *fetched* laid over it, restricted to those
    this run's bars take part in -- an old jump the run did not touch is not its to refuse."""
    merged = {c.date: c for c in stored}
    merged.update((c.date, c) for c in fetched)
    touched = {c.date for c in fetched}
    return [
        j
        for j in split_shaped_jumps(merged.values())
        if j.date in touched or j.prev_date in touched
    ]


def _refetch_full(
    engine: _Engine, task: _Task, stored: list[Candle], fetched: list[Candle]
) -> list[Candle]:
    """Re-fetch the asset's whole history (from its first stored bar, or ``--start`` if earlier),
    so a split adjusts every bar at once. Returns *fetched* unchanged if the gateway has nothing."""
    params = engine.params
    end = params.resolved_end()
    start = min(stored[0].date, params.start_date) if stored else params.start_date
    full = engine.client.daily_any_spelling(task.ticker, start, end)
    engine.report.full_refetches.append(task.ticker)
    rewritten = [c for c in full.candles if c.date <= end]
    return rewritten or fetched


def _params_json(params: RunParams, dirty_reason: str | None) -> str:
    return json.dumps(
        {
            "analysis_date": params.analysis_date,
            "start_date": params.start_date,
            "end_date": params.resolved_end(),
            "limit": params.limit,
            "tickers": list(params.tickers) if params.tickers else None,
            "by_year": params.by_year,
            "store_daily": params.store_daily,
            "observations": params.observations,
            "fresh": params.fresh,
            "allow_split_jumps": params.allow_split_jumps,
            "dirty_tree_bypassed": dirty_reason,
        }
    )
