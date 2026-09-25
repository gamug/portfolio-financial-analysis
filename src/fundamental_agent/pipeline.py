"""Batch driver: walk the universe, analyze each filing, persist, resume, report.

One :func:`run` call processes every ``(asset, form, filing-year)`` task in scope. A
task whose snapshot already exists is skipped, so re-running continues from wherever
the last run stopped. Progress and ETA come from a ``tqdm`` bar.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from portfolio_common.db import Database, Row
from tqdm import tqdm

from fundamental_agent import db, quality
from fundamental_agent.agents import FilingContext, FundamentalAnalyst, build_model
from fundamental_agent.config import Settings
from fundamental_agent.db import FilingKey, FilingMeta, RunError, SnapshotRow
from fundamental_agent.edgar_client import (
    EdgarClient,
    EdgarError,
    EdgarNotFoundError,
    FilingRef,
    normalize_ticker,
)
from fundamental_agent.filing_text import fetch_primary_document
from fundamental_agent.metrics.base import TTMFlow
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
    # Accessions already scored: a resumed run skips their ``financials`` call.
    completed_accessions: set[str] = field(default_factory=set)


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
        accessions: set[str] = set() if params.fresh else db.completed_accessions(conn)

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
            engine = _Engine(
                conn, edgar, analyst, params, report, completed, completed_accessions=accessions
            )
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
    """One (ticker, form, filing-year): list its filings, then ingest each one."""
    try:
        ticker_api, refs = _list_filings(engine, task)
    except EdgarNotFoundError:
        _count(engine, skipped=1)  # the company filed no such form that year
        return
    except Exception as exc:  # one bad ticker/year must not stop the batch
        _record_failure(engine, task, None, exc)
        return
    for ref in _select_filings(task, refs):
        _run_filing(engine, task, ticker_api, ref)


def _run_filing(engine: _Engine, task: _YearTask, ticker_api: str, ref: FilingRef) -> None:
    try:
        done, skipped = _process_filing(engine, task, ticker_api, ref)
    except EdgarNotFoundError:
        _count(engine, skipped=1)
        return
    except Exception as exc:  # one bad filing must not stop its siblings or the batch
        _record_failure(engine, task, ref, exc)
        return
    _count(engine, completed=done, skipped=skipped)


def _count(engine: _Engine, *, completed: int = 0, skipped: int = 0) -> None:
    report = engine.report
    report.completed += completed
    report.skipped += skipped
    for _ in range(completed):
        db.bump_run_counter(engine.conn, report.run_id, "completed_units")
    for _ in range(skipped):
        db.bump_run_counter(engine.conn, report.run_id, "skipped_units")


def _record_failure(
    engine: _Engine, task: _YearTask, ref: FilingRef | None, exc: Exception
) -> None:
    report = engine.report
    where = f" [{ref.accession_number}]" if ref else ""
    report.failed += 1
    report.errors.append(f"{task.ticker} {task.form} {task.year}{where}: {exc}")
    db.bump_run_counter(engine.conn, report.run_id, "failed_units")
    db.record_error(
        engine.conn,
        report.run_id,
        RunError(
            task.ticker,
            task.form,
            str(task.year),
            "process",
            f"{ref.accession_number}: {exc}" if ref else str(exc),
        ),
    )


def _list_filings(engine: _Engine, task: _YearTask) -> tuple[str, list[FilingRef]]:
    """The ticker spelling EDGAR knows and its filings for the task's form and year.

    Raises :class:`EdgarNotFoundError` when the company exists but filed nothing that year
    (a skip), and a plain :class:`EdgarError` when no spelling matches a company at all (a
    failure worth seeing)."""
    candidates = (
        [engine.resolved[task.ticker]]
        if task.ticker in engine.resolved
        else normalize_ticker(task.ticker)
    )
    company_found = False
    last: EdgarNotFoundError | None = None
    for candidate in candidates:
        try:
            refs = engine.edgar.filing_by_year(candidate, task.form, task.year)
        except EdgarNotFoundError as exc:
            last = exc
            continue
        company_found = True
        if refs:
            engine.resolved[task.ticker] = candidate
            return candidate, refs
    if company_found:
        raise EdgarNotFoundError(f"no {task.form} filing for {task.ticker} in {task.year}")
    raise EdgarError(f"no EDGAR match for {task.ticker!r}") from last


def _select_filings(task: _YearTask, refs: Sequence[FilingRef]) -> list[FilingRef]:
    """The filings to ingest, **oldest first**.

    Chronological order matters: F4's trailing-twelve-month figure for a 10-Q reads the
    prior quarters' already-recorded values (``db.ttm_flows``), so Q1 must be recorded
    before Q2 and Q2 before Q3. The gateway lists most-recent-first, hence the re-sort.
    A 10-K reports one fiscal year, so if several match (an amendment) only the most
    recent is kept."""
    ordered = sorted(refs, key=lambda r: (r.filing_date or "", r.accession_number))
    return ordered[-1:] if task.form == "10-K" else ordered


def _process_filing(
    engine: _Engine, task: _YearTask, ticker_api: str, ref: FilingRef
) -> tuple[int, int]:
    as_of = engine.params.analysis_date
    if ref.filing_date and ref.filing_date > as_of:
        return 0, 1  # no lookahead: this filing was filed after the analysis date
    if not engine.params.fresh and ref.accession_number in engine.completed_accessions:
        return 0, 1  # already ingested and scored by an earlier run

    payload = engine.edgar.financials(
        ticker_api, task.form, task.year, accession_number=ref.accession_number
    )
    stmts = Statements.from_payload(payload)
    meta = FilingMeta(filing_date=ref.filing_date, accession_number=ref.accession_number)

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
        engine.completed_accessions.add(ref.accession_number)
        done += 1
    return done, skipped


def _targets(stmts: Statements, task: _YearTask) -> list[_Target]:
    """The filing's **own** reporting period, and nothing else.

    A payload carries comparative columns (the prior quarter, the prior year) beside the
    period the filing reports. Those are facts about earlier filings; recording them as a
    filing of their own would stamp them with *this* filing's accession and date (T-091:
    45 asset-accession pairs carried several fiscal periods). The own period is the latest
    fiscal year (10-K) or the latest quarter (10-Q) the payload holds. The old
    ``period.year == task.year`` filter is gone: ``task.year`` is the *filing* year, so it
    also dropped a January 10-Q for a December quarter."""
    if task.form == "10-K":
        period = stmts.latest_fy()
        if period is None:
            return []
        return [_Target(period, f"FY{period.year}", stmts.prior_of(period))]

    quarters = stmts.quarter_periods()
    if not quarters:
        return []
    period = max(quarters, key=lambda p: p.date)
    return [_Target(period, f"{period.year}Q{period.tag[1]}", stmts.prior_of(period))]


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


_TTM_ITEMS = (
    "net_income",
    "revenue",
    "cogs",
    # T-105: FCF yield, net debt / EBITDA and ROIC divide these by a stock or a price level too
    "operating_cash_flow",
    "capital_expenditure",
    "operating_income",
    "depreciation_amortization",
    "interest_expense",
    "stock_based_compensation",
)
# A year-to-date column "one year earlier" can sit a few days off on a 52/53-week calendar.
_YEAR_TOLERANCE_DAYS = 20


def _ytd_columns(stmts: Statements, target: _Target) -> tuple[str | None, str | None, str | None]:
    """``(this year's YTD column, last year's same YTD column, that column's end date)`` of a
    10-Q. A first quarter's year-to-date *is* its quarter, so a Q1 filing with no ``(YTD)``
    columns uses its ``(Q1)`` columns (T-105)."""
    end = date.fromisoformat(target.period.date)
    year_ago = end - timedelta(days=365)
    tags = ("YTD", target.period.tag) if target.period.tag == "Q1" else ("YTD",)
    for tag in tags:
        periods = [p for p in stmts.periods if p.tag == tag]
        current = next((p for p in periods if p.date == target.period.date), None)
        prior = next(
            (
                p
                for p in periods
                if abs((date.fromisoformat(p.date) - year_ago).days) <= _YEAR_TOLERANCE_DAYS
            ),
            None,
        )
        if current is not None and prior is not None:
            return current.key, prior.key, prior.date
    return None, None, None


def _ttm_flows(
    engine: _Engine, task: _YearTask, stmts: Statements, target: _Target
) -> dict[str, TTMFlow]:
    """This filing's flow inputs, annualized to trailing-twelve-months on a 10-Q
    (F4, T-105, docs/model_fixes.md) -- empty for a 10-K, which already reports an
    annual flow and needs no adjustment."""
    if task.form != "10-Q":
        return {}
    current = {
        item: value
        for item in _TTM_ITEMS
        if (value := stmts.get(item, target.period.key)) is not None
    }
    cur_key, prior_key, prior_end = _ytd_columns(stmts, target)
    ytd = (
        {
            item: db.YTDPair(stmts.get(item, cur_key), stmts.get(item, prior_key), prior_end)
            for item in _TTM_ITEMS
        }
        if cur_key and prior_key
        else {}
    )
    if not current and not ytd:
        return {}
    return db.ttm_detail(
        engine.conn, task.asset_id, period_end=target.period.date, current=current, ytd=ytd
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

    # This filing's own share-count facts are already in `financial_facts` (just
    # appended above) -- cross-check them against this asset's prior filings for a
    # known SEC XBRL scale/tagging defect (docs/model_fixes.md, F1) before valuation
    # multiplies a possibly-mis-scaled share count by price.
    share_scale_factors = db.detect_share_scale_factors(
        engine.conn, task.asset_id, stmts, target.period.key, exclude_filing_id=filing_id
    )

    ttm = _ttm_flows(engine, task, stmts, target)
    ctx = FilingContext(
        ticker=task.ticker,
        company_name=task.company_name,
        form=task.form,
        fiscal_period=target.fiscal_period,
        stmts=stmts,
        period_key=target.period.key,
        prior_key=target.prior.key if target.prior else None,
        price=close_on_or_before(engine.conn, task.asset_id, target.period.date),
        share_scale_factors=share_scale_factors,
        ttm_flows=ttm,
    )
    result = engine.analyst.analyze(ctx)
    db.record_metrics(
        engine.conn, filing_id, result.metrics, event_time=target.period.date, run_id=run_id
    )
    # Ring-1 data-quality gates (T-065) over what was just stored, so every newly analysed
    # filing is gated; `python -m fundamental_agent quality` backfills older ones.
    quality.gate_version(engine.conn, db.METRICS_ENGINE_VERSION, filing_id=filing_id, run_id=run_id)
    # T-105 review: where the TTM identity and four recorded quarters disagree, a SOFT review.
    quality.record_ttm_crosscheck(
        engine.conn,
        filing_id,
        task.asset_id,
        ttm,
        engine_version=db.METRICS_ENGINE_VERSION,
        run_id=run_id,
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
