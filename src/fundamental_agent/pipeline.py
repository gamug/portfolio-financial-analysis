"""Batch driver: walk the universe, analyze each filing, persist, resume, report.

One :func:`run` call processes every ``(asset, form, filing-year)`` task in scope. A
task whose snapshot already exists is skipped, so re-running continues from wherever
the last run stopped. Progress and ETA come from a ``tqdm`` bar.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from portfolio_common.db import Database, Row
from tqdm import tqdm

from fundamental_agent import db
from fundamental_agent.agents import FilingContext, FundamentalAnalyst, build_model
from fundamental_agent.config import Settings
from fundamental_agent.db import FilingKey, FilingMeta, RunError, SnapshotRow
from fundamental_agent.edgar_client import (
    EdgarClient,
    EdgarNotFoundError,
    normalize_ticker,
)
from fundamental_agent.filing_text import fetch_primary_document
from fundamental_agent.pricing import close_on_or_before
from fundamental_agent.sections import split_sections
from fundamental_agent.statements import Period, Statements, iter_facts
from kg_schema import connect, rundate
from kg_schema.provenance import code_version
from kg_schema.queries import UniverseMember, connect_ro, members_asof

DEFAULT_FORMS = ("10-K", "10-Q")
DEFAULT_SINCE_YEAR = 2022

_Unit = tuple[str, str, str]  # (ticker, form, fiscal_period)
_Payload = dict[str, Any]


@dataclass
class RunParams:
    """User-facing knobs for a single batch run."""

    forms: Sequence[str] = DEFAULT_FORMS
    since_year: int = DEFAULT_SINCE_YEAR
    until_year: int | None = None  # capped at the analysis_date's year
    limit: int | None = None
    tickers: Sequence[str] | None = None
    fresh: bool = False  # re-analyze even if a snapshot exists
    refresh_universe: bool = False  # accepted for back-compat; no longer meaningful
    sections: bool = False  # also extract narrative filing text (MD&A, risk factors)
    analysis_date: str = field(default_factory=rundate.today)

    def resolved_until(self) -> int:
        cap = int(self.analysis_date[:4])
        return min(self.until_year, cap) if self.until_year else cap


@dataclass
class RunReport:
    """What a run did -- also mirrored into the ``analysis_run`` table."""

    run_id: int
    planned: int
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _YearTask:
    asset_id: int
    ticker: str
    company_name: str
    form: str
    year: int


@dataclass(frozen=True)
class _Target:
    period: Period
    fiscal_period: str
    prior: Period | None


@dataclass
class _Engine:
    """Long-lived collaborators threaded through the per-task helpers."""

    conn: Database
    edgar: EdgarClient
    analyst: FundamentalAnalyst
    params: RunParams
    report: RunReport
    completed: set[_Unit]
    resolved: dict[str, str] = field(default_factory=dict)


def run(settings: Settings, params: RunParams) -> RunReport:
    """Execute a batch run and return its report."""
    conn = connect(settings.db_path)
    try:
        db.ensure_schema(conn)
        members = _load_members(settings, params.analysis_date)
        db.sync_universe(conn, members)
        symbols = [m.symbol for m in members]
        assets = db.load_universe(conn, tickers=params.tickers, symbols=symbols, limit=params.limit)
        if not assets:
            raise RuntimeError(
                f"no S&P 500 members as of {params.analysis_date} resolved to an asset row"
            )
        tasks = _plan(assets, params)
        completed: set[_Unit] = set() if params.fresh else db.completed_units(conn)

        run_id = db.start_run(
            conn,
            params=_params_dict(params),
            as_of=params.analysis_date,
            code_version=code_version(),
        )
        db.update_run_plan(conn, run_id, universe_size=len(assets), planned_units=len(tasks))
        report = RunReport(run_id=run_id, planned=len(tasks))

        analyst = FundamentalAnalyst(build_model(settings), settings.llm_model)
        with EdgarClient(settings.edgar_base_url) as edgar:
            engine = _Engine(conn, edgar, analyst, params, report, completed)
            _drive(engine, tasks)

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


def _plan(assets: Sequence[Row], params: RunParams) -> list[_YearTask]:
    years = range(params.since_year, params.resolved_until() + 1)
    return [
        _YearTask(
            asset_id=int(asset["id"]),
            ticker=str(asset["ticker"]),
            company_name=str(asset["company_name"] or asset["ticker"]),
            form=form,
            year=year,
        )
        for asset in assets
        for form in params.forms
        for year in years
    ]


def _drive(engine: _Engine, tasks: Sequence[_YearTask]) -> None:
    bar = tqdm(tasks, desc="fundamental analysis", unit="task")
    for task in bar:
        bar.set_postfix_str(f"{task.ticker} {task.form} {task.year}")
        _run_task(engine, task)


def _run_task(engine: _Engine, task: _YearTask) -> None:
    report = engine.report
    try:
        done, skipped = _process(engine, task)
    except EdgarNotFoundError:
        report.skipped += 1
        db.bump_run_counter(engine.conn, report.run_id, "skipped_units")
        return
    except Exception as exc:  # one bad filing must not stop the batch
        report.failed += 1
        report.errors.append(f"{task.ticker} {task.form} {task.year}: {exc}")
        db.bump_run_counter(engine.conn, report.run_id, "failed_units")
        db.record_error(
            engine.conn,
            report.run_id,
            RunError(task.ticker, task.form, str(task.year), "process", str(exc)),
        )
        return

    report.completed += done
    report.skipped += skipped
    for _ in range(done):
        db.bump_run_counter(engine.conn, report.run_id, "completed_units")
    for _ in range(skipped):
        db.bump_run_counter(engine.conn, report.run_id, "skipped_units")


def _process(engine: _Engine, task: _YearTask) -> tuple[int, int]:
    if (
        task.form == "10-K"
        and not engine.params.fresh
        and (task.ticker, "10-K", f"FY{task.year}") in engine.completed
    ):
        return 0, 1

    ticker_api, payload = _fetch_financials(engine, task)
    stmts = Statements.from_payload(payload)
    meta = _fetch_meta(engine, ticker_api, task)

    as_of = engine.params.analysis_date
    if meta.filing_date and meta.filing_date > as_of:
        return 0, 1  # no lookahead: this filing was filed after the analysis date

    done = skipped = 0
    for target in _targets(stmts, task):
        if target.period.date > as_of:
            skipped += 1
            continue
        unit = (task.ticker, task.form, target.fiscal_period)
        if not engine.params.fresh and unit in engine.completed:
            skipped += 1
            continue
        _analyze_one(engine, task, stmts, target, meta)
        engine.completed.add(unit)
        done += 1
    return done, skipped


def _fetch_financials(engine: _Engine, task: _YearTask) -> tuple[str, _Payload]:
    candidates = (
        [engine.resolved[task.ticker]]
        if task.ticker in engine.resolved
        else normalize_ticker(task.ticker)
    )
    last: EdgarNotFoundError | None = None
    for candidate in candidates:
        try:
            payload = engine.edgar.financials(candidate, task.form, task.year)
        except EdgarNotFoundError as exc:
            last = exc
            continue
        engine.resolved[task.ticker] = candidate
        return candidate, payload
    raise last or EdgarNotFoundError(f"no financials for {task.ticker}")


def _fetch_meta(engine: _Engine, ticker_api: str, task: _YearTask) -> FilingMeta:
    try:
        raw = engine.edgar.filing_by_year(ticker_api, task.form, task.year)
    except EdgarNotFoundError:
        return FilingMeta()
    return FilingMeta(
        filing_date=_as_str(raw.get("filing_date")),
        accession_number=_as_str(raw.get("accession_number")),
    )


def _targets(stmts: Statements, task: _YearTask) -> list[_Target]:
    if task.form == "10-K":
        period = stmts.latest_fy()
        if period is None:
            return []
        return [_Target(period, f"FY{period.year}", stmts.prior_of(period))]

    return [
        _Target(period, f"{period.year}Q{period.tag[1]}", stmts.prior_of(period))
        for period in stmts.quarter_periods()
        if period.year == task.year
    ]


def _extract_sections(
    engine: _Engine, task: _YearTask, filing_id: int, target: _Target, meta: FilingMeta
) -> None:
    """Best-effort narrative-text extraction. A failure here never fails the filing."""
    if not meta.accession_number:
        return
    row = engine.conn.execute("SELECT cik FROM assets WHERE id = ?", (task.asset_id,)).fetchone()
    cik = row["cik"] if row else None
    if not cik:
        return
    try:
        html, source_url = fetch_primary_document(cik, meta.accession_number, form=task.form)
        sections = split_sections(html, task.form)
        if sections:
            db.insert_filing_sections(
                engine.conn,
                filing_id,
                sections,
                event_time=meta.filing_date or target.period.date,
                source_url=source_url,
                run_id=engine.report.run_id,
            )
    except Exception as exc:  # non-fatal: section text is best-effort, recorded for triage
        db.record_error(
            engine.conn,
            engine.report.run_id,
            RunError(task.ticker, task.form, target.fiscal_period, "sections", str(exc)),
        )


def _analyze_one(
    engine: _Engine,
    task: _YearTask,
    stmts: Statements,
    target: _Target,
    meta: FilingMeta,
) -> None:
    run_id = engine.report.run_id
    filing_id = db.upsert_filing(
        engine.conn,
        FilingKey(task.asset_id, task.form, target.period.year, target.fiscal_period),
        FilingMeta(
            filing_date=meta.filing_date,
            accession_number=meta.accession_number,
            period_end=target.period.date,
        ),
        run_id=run_id,
    )
    db.append_financial_facts(
        engine.conn,
        filing_id,
        iter_facts(stmts),
        filing_version=meta.accession_number or db.FACTS_ENGINE_VERSION,
        event_time=target.period.date,
        run_id=run_id,
    )
    if engine.params.sections:
        _extract_sections(engine, task, filing_id, target, meta)

    ctx = FilingContext(
        ticker=task.ticker,
        company_name=task.company_name,
        form=task.form,
        fiscal_period=target.fiscal_period,
        stmts=stmts,
        period_key=target.period.key,
        prior_key=target.prior.key if target.prior else None,
        price=close_on_or_before(engine.conn, task.asset_id, target.period.date),
    )
    result = engine.analyst.analyze(ctx)
    db.record_metrics(
        engine.conn, filing_id, result.metrics, event_time=target.period.date, run_id=run_id
    )

    assessment = result.assessment
    db.insert_snapshot(
        engine.conn,
        SnapshotRow(
            asset_id=task.asset_id,
            filing_id=filing_id,
            form=task.form,
            fiscal_period=target.fiscal_period,
            score=assessment.score,
            rating=assessment.rating,
            narrative=assessment.narrative,
            strengths=assessment.strengths,
            risks=assessment.risks,
            model=engine.analyst.model_name,
            metrics=result.flat_metrics,
            event_time=target.period.date,
        ),
        run_id=run_id,
    )


def _params_dict(params: RunParams) -> _Payload:
    return {
        "analysis_date": params.analysis_date,
        "forms": list(params.forms),
        "since_year": params.since_year,
        "until_year": params.resolved_until(),
        "limit": params.limit,
        "tickers": list(params.tickers) if params.tickers else None,
        "fresh": params.fresh,
    }


def _as_str(value: object) -> str | None:
    return str(value) if isinstance(value, (str, int, float)) else None
