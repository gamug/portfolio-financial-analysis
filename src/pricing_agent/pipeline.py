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

from portfolio_common.db import Database
from tqdm import tqdm

from kg_schema import rundate
from kg_schema.provenance import code_version
from kg_schema.queries import UniverseMember, connect_ro, members_asof
from pricing_agent import db
from pricing_agent.config import Settings
from pricing_agent.db import PriceWindowRow, RunError
from pricing_agent.observations import build_observations
from pricing_agent.pricing_client import DailyPrices, PricingClient
from pricing_agent.stats import WindowStats, slice_year, summarize

DEFAULT_START_DATE = "2022-01-01"
FULL_LABEL = "full"

_Window = tuple[str, str, str, str]  # (ticker, start, end, label)


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
    conn = db.connect(settings.db_path)
    try:
        db.ensure_schema(conn)
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
                params_json=_params_json(params),
                as_of=params.analysis_date,
                code_version=code_version(),
            )
            db.update_run_plan(conn, run_id, universe_size=len(assets), planned_units=len(tasks))
            report = RunReport(run_id=run_id, planned=len(tasks))
            engine = _Engine(conn, client, params, report, completed)

            bar = tqdm(tasks, desc="s&p 500 pricing", unit="ticker")
            for task in bar:
                bar.set_postfix_str(task.ticker)
                _run_task(engine, task)

            db.finish_run(conn, run_id, status="completed")
            return report
    finally:
        conn.close()


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

    _store(engine, task, prices)
    report.completed += 1
    db.bump_run_counter(conn, report.run_id, "completed_units")


def _store(engine: _Engine, task: _Task, prices: DailyPrices) -> None:
    params, conn = engine.params, engine.conn
    start, end = params.start_date, params.resolved_end()
    run_id = engine.report.run_id

    # No lookahead: drop any candle dated after the analysis date.
    candles = [c for c in prices.candles if c.date <= end]

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
        db.replace_daily_prices(conn, task.asset_id, candles, run_id=run_id)
    if params.observations:
        db.upsert_price_observations(
            conn,
            task.asset_id,
            build_observations(candles, engine_version=db.PRICE_OBSERVATION_ENGINE_VERSION),
            run_id=run_id,
        )


def _params_json(params: RunParams) -> str:
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
        }
    )
