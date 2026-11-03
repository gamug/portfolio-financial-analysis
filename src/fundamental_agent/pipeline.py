"""Batch driver: walk the universe, analyze each filing, persist, resume, report.

One :func:`run` call processes every ``(asset, form, filing-year)`` task in scope. A
task whose snapshot already exists is skipped, so re-running continues from wherever
the last run stopped. Progress and ETA come from a ``tqdm`` bar.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from portfolio_common.db import Database, Row
from tqdm import tqdm

from fundamental_agent import db, quality, utility_coverage, value_inputs
from fundamental_agent.agents import (
    FALLBACK_MODEL_LABEL,
    UNSCORED_MODEL_LABEL,
    AnalysisResult,
    FilingContext,
    FundamentalAnalyst,
    build_model,
    compute_metrics,
)
from fundamental_agent.config import Settings
from fundamental_agent.db import FilingKey, FilingMeta, RunError, SnapshotRow
from fundamental_agent.edgar_client import (
    EdgarClient,
    EdgarError,
    EdgarNotFoundError,
    FilingRef,
)
from fundamental_agent.failures import classify
from fundamental_agent.filing_text import fetch_primary_document
from fundamental_agent.fiscal import (
    QUARTER_DAYS,
    is_year_apart,
    label_year,
    payload_fiscal_year_end,
    quarter_label,
    same_period_end,
    within,
)
from fundamental_agent.metrics.base import TTMFlow
from fundamental_agent.pricing import close_on_or_before
from fundamental_agent.sections import split_sections
from fundamental_agent.statements import EBIT_DERIVED_HOW, Period, Statements, iter_facts
from kg_schema import connect, identity, rundate, ticker_history
from kg_schema.market_cap import market_caps_as_of
from kg_schema.provenance import DirtyTree, code_version, dirty_tree_reason
from kg_schema.queries import (
    WINDOW_START,
    WindowMember,
    connect_ro,
    members_over_range,
)

DEFAULT_FORMS = ("10-K", "10-Q")
DEFAULT_SINCE_YEAR = 2022

# (ticker, form, period_end): the date, not the derived fiscal_period label (T-140)
_Unit = tuple[str, str, str]
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
    # T-143 part A, P1: compute the facts, metrics, gates and rule inputs but write no
    # FUNDAMENTAL score -- no model is built and none is called.
    metrics_only: bool = False

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
    service_failed: int = 0  # T-017: failures that may clear on a retry (``failures.classify``)
    errors: list[str] = field(default_factory=list)
    dirty_tree_bypassed: str | None = None  # T-114: why, if --allow-dirty overrode it

    @property
    def data_failed(self) -> int:
        """Failures that are about the filing itself and fail the same way on every run."""
        return self.failed - self.service_failed


@dataclass(frozen=True)
class _YearTask:
    asset_id: int
    ticker: str
    company_name: str
    form: str
    year: int
    cik: str  # the 10-digit string every EDGAR call carries (T-149); the ticker is for display
    # the asset's recorded predecessors (APP-10b, ``cik_succession``): their filings are the asset's too
    predecessors: tuple[str, ...] = ()
    reit: bool = False
    # T-153 part 3a: the last membership day of the company's line (None = still a member). No
    # filing filed after it is fetched -- "nothing after a company's exit", the symmetric rule.
    until: str | None = None

    @property
    def ciks(self) -> tuple[str, ...]:
        """Every CIK this asset's filings are fetched under: its own first."""
        return (self.cik, *self.predecessors)


@dataclass(frozen=True)
class _Target:
    period: Period
    fiscal_period: str
    prior: Period | None
    fiscal_year: int  # the year the label carries (fiscal.label_year), stored beside it
    # C4 (T-151 part 1): why the quarter label was overridden from the company's own fiscal year
    # end, or ``None`` when the payload's year end agreed. Logged by the caller as a warning.
    label_override: str | None = None


@dataclass
class _Engine:
    """Long-lived collaborators threaded through the per-task helpers."""

    conn: Database
    edgar: EdgarClient
    # None in metrics-only mode (T-143 part A, P1): the metrics come from
    # ``agents.compute_metrics`` and no model is built.
    analyst: FundamentalAnalyst | None
    params: RunParams
    report: RunReport
    completed: set[_Unit]
    # Accessions already scored: a resumed run skips their ``financials`` call.
    completed_accessions: set[str] = field(default_factory=set)


def run(settings: Settings, params: RunParams) -> RunReport:
    """Execute a batch run and return its report."""
    conn = connect(settings.db_path)
    try:
        db.ensure_schema(conn)
        _refuse_shared_accessions(conn)
        _refuse_identity_violations(conn)
        window = _load_window(settings, conn, params.analysis_date)
        members = [w.member for w in window]
        sync = db.sync_universe(conn, members)
        _report_adoptions(sync)
        # T-153 part 2: a renamed company is one asset -- load it under its line's canonical
        # symbol, so a run dated before the rename (FB) still finds the asset that carries the
        # current ticker (META). --tickers takes the symbol of the run's date too (F3).
        symbols = [
            ticker_history.canonical_for_member(conn, symbol=m.symbol, cik=m.cik) for m in members
        ]
        if params.tickers:
            params = replace(
                params,
                tickers=ticker_history.canonical_tickers(
                    conn,
                    params.tickers,
                    member_ciks={m.symbol.upper(): m.cik for m in members},
                    as_of=params.analysis_date,
                ),
            )
        assets = db.load_universe(conn, tickers=params.tickers, symbols=symbols, limit=params.limit)
        if not assets:
            raise RuntimeError(
                f"no S&P 500 members as of {params.analysis_date} resolved to an asset row"
            )
        chains = {str(a["ticker"]): identity.cik_chain(conn, a["cik"]) for a in assets}
        reits = {int(a["id"]): db.is_equity_reit(conn, int(a["id"])) for a in assets}
        tasks = _plan(assets, params, chains, reits=reits, until=_exit_by_ticker(window))
        completed: set[_Unit] = set() if params.fresh else db.completed_units(conn)
        if params.fresh:
            accessions: set[str] = set()
        else:
            accessions = db.completed_accessions(conn)
            if params.metrics_only:
                # T-143 part A, P1: a metrics-only filing has no FUNDAMENTAL row, so the
                # FUNDAMENTAL-keyed set above never names it. Add the metrics-only done-set
                # (``db.metrics_only_accessions``) so a re-run or a resume re-fetches nothing.
                accessions |= db.metrics_only_accessions(conn)

        # T-114: refuse before any run row exists when this run's own code_version() is dirty
        # (uncommitted changes) -- its results would come from code HEAD alone can't reproduce.
        cv = code_version()
        dirty_reason = dirty_tree_reason(cv)
        if dirty_reason is not None and not settings.allow_dirty:
            raise DirtyTree(
                f"{dirty_reason}; pass --allow-dirty for a deliberate run from an uncommitted tree"
            )
        run_id = db.start_run(
            conn,
            params={**_params_dict(params), "dirty_tree_bypassed": dirty_reason},
            as_of=params.analysis_date,
            code_version=cv,
        )
        db.update_run_plan(conn, run_id, universe_size=len(assets), planned_units=len(tasks))
        report = RunReport(run_id=run_id, planned=len(tasks), dirty_tree_bypassed=dirty_reason)

        try:
            analyst = (
                None
                if params.metrics_only
                else FundamentalAnalyst(build_model(settings), settings.llm_model)
            )
            with EdgarClient(settings.edgar_base_url) as edgar:
                engine = _Engine(
                    conn, edgar, analyst, params, report, completed, completed_accessions=accessions
                )
                _drive(engine, _verify_successions(engine, tasks))
        except BaseException:  # an interrupt too: never leave the run log 'running'
            conn.rollback()  # drop any half-written statement before recording the outcome
            db.finish_run(conn, run_id, status="failed")
            raise

        db.finish_run(conn, run_id, status="completed")
        return report
    finally:
        conn.close()


def _load_window(settings: Settings, conn: Database, analysis_date: str) -> list[WindowMember]:
    """Every company that was a member at any time in ``[WINDOW_START, analysis_date]``.

    One entry per CIK line (T-153 part 3a): a run on ``analysis_date`` ingests what every
    company that was a member at any point in the backtest window needs, leavers included,
    and nothing more -- the line's ``window_to`` bounds its filings. ``universe.db`` is read
    strictly as membership; the CIK line comes from the tracked ticker history."""
    uconn = connect_ro(settings.universe_db_path)
    try:
        window = members_over_range(uconn, conn, WINDOW_START, analysis_date)
    finally:
        uconn.close()
    if not window:
        raise RuntimeError(
            f"universe.db ({settings.universe_db_path}) has no members in "
            f"[{WINDOW_START}, {analysis_date}]"
        )
    tqdm.write(f"universe: {len(window)} S&P 500 lines in [{WINDOW_START}, {analysis_date}]")
    return window


def _exit_by_ticker(window: Sequence[WindowMember]) -> dict[str, str | None]:
    """The line's last membership day per canonical ticker (``None`` = still a member)."""
    return {w.member.symbol.upper(): w.window_to for w in window}


def _report_adoptions(sync: ticker_history.SyncResult) -> None:
    """Print every recorded rename adopted in place (T-153 part 2, C4)."""
    for adoption in sync.adoptions:
        tqdm.write(
            f"universe: adopted {adoption.old_ticker} -> {adoption.new_ticker} "
            f"(asset {adoption.asset_id}, CIK {adoption.cik})"
        )


class MissingCikError(RuntimeError):
    """An asset in scope has no CIK, so its filings cannot be fetched (T-149)."""


def _plan(
    assets: Sequence[Row],
    params: RunParams,
    chains: Mapping[str, tuple[str, ...]] | None = None,
    reits: Mapping[int, bool] | None = None,
    until: Mapping[str, str | None] | None = None,
) -> list[_YearTask]:
    keyless = [str(a["ticker"]) for a in assets if identity.normalize_cik(a["cik"]) is None]
    if keyless:
        raise MissingCikError(
            f"{len(keyless)} assets have no CIK ({', '.join(keyless)}): filings are fetched by CIK, "
            "never by ticker (APP-10a, T-149)"
        )
    exits = until or {}
    tasks: list[_YearTask] = []
    for asset in assets:
        ticker = str(asset["ticker"])
        # T-153 part 3a: a company that left the index is listed only through its exit year --
        # and ``_process_filing`` drops any filing filed after the exit day itself.
        exit_date = exits.get(ticker.upper())
        last_year = params.resolved_until()
        if exit_date is not None:
            last_year = min(last_year, int(exit_date[:4]))
        for form in params.forms:
            for year in range(params.since_year, last_year + 1):
                tasks.append(
                    _YearTask(
                        asset_id=int(asset["id"]),
                        ticker=ticker,
                        company_name=str(asset["company_name"] or ticker),
                        form=form,
                        year=year,
                        cik=str(identity.normalize_cik(asset["cik"])),
                        predecessors=(chains or {}).get(ticker, ())[1:],
                        reit=bool((reits or {}).get(int(asset["id"]), False)),
                        until=exit_date,
                    )
                )
    return tasks


# A fiscal year is at most 53 weeks, so the last one to end before a succession ended within this many days.
_LAST_ANNUAL_WINDOW_DAYS = 371


def _verify_successions(engine: _Engine, tasks: Sequence[_YearTask]) -> list[_YearTask]:
    """Drop the predecessors whose last annual report is not listed (APP-10b-3, BLOCK).

    SEC Rule 12g-3 (A7 p.4) has the successor file the annual report of the predecessor's last full
    fiscal year; when it is missing the rule only leaves the multi-year metrics empty. Stricter here:
    the link is what makes them multi-year, so the predecessor's filings are not linked at all, and an
    ``analysis_run_error`` (``stage='succession'``) says which. The check is once per succession."""
    effective = {
        (task.cik, pred): date_
        for task in tasks
        for pred, date_ in identity.predecessors_of(engine.conn, task.cik)
        if pred in task.predecessors
    }
    verdicts: dict[tuple[str, str], str | None] = {}
    recorded: set[tuple[str, str]] = set()
    verified: list[_YearTask] = []
    for task in tasks:
        kept = []
        for pred in task.predecessors:
            key = (task.cik, pred)
            if key not in verdicts:
                verdicts[key] = _last_annual_report_missing(engine, pred, effective[key], task.cik)
            reason = verdicts[key]
            if reason is None:
                kept.append(pred)
            elif (task.ticker, pred) not in recorded:
                recorded.add((task.ticker, pred))
                message = f"predecessor {pred} not linked to {task.cik} (APP-10b-3): {reason}"
                engine.report.errors.append(f"{task.ticker} [succession]: {message}")
                db.record_error(
                    engine.conn,
                    engine.report.run_id,
                    RunError(task.ticker, "10-K", "", "succession", message),
                )
        verified.append(
            replace(task, predecessors=tuple(kept)) if kept != task.predecessors else task
        )
    return verified


def _last_annual_report_missing(
    engine: _Engine, predecessor: str, effective: str, successor: str
) -> str | None:
    """Why the predecessor's last 10-K before *effective* cannot be confirmed; ``None`` when it is listed.

    The last full fiscal year ended within :data:`_LAST_ANNUAL_WINDOW_DAYS` before the effective date and
    its 10-K was filed that calendar year or the one before; an amendment does not count (L-04). SEC Rule
    12g-3 (A7 p.4) has the successor file that report "unless such report has been filed by the
    predecessor", so it is looked for under the predecessor's CIK first and, only when it is not there,
    under the successor's CIK with the same test."""
    end = date.fromisoformat(effective)
    first = end - timedelta(days=_LAST_ANNUAL_WINDOW_DAYS)
    for cik in (predecessor, successor):
        for year in (end.year, end.year - 1):
            try:
                refs = engine.edgar.filing_by_year(cik, "10-K", year)
            except EdgarNotFoundError:
                continue
            except Exception as exc:  # the report cannot be confirmed: do not link
                return f"its 10-K listing for {year} under {cik} could not be read ({exc})"
            for ref in refs:
                if (
                    _is_amendment(ref)
                    or ref.form.strip().upper() != "10-K"
                    or not ref.period_of_report
                ):
                    continue
                if first <= date.fromisoformat(ref.period_of_report) < end:
                    return None
    return (
        f"no 10-K with a period ending {first} to {end} is listed under {predecessor} "
        f"or {successor}; the predecessor's filings are not linked"
    )


def _drive(engine: _Engine, tasks: Sequence[_YearTask]) -> None:
    tasks_by_asset: dict[int, list[_YearTask]] = {}
    for task in tasks:
        tasks_by_asset.setdefault(task.asset_id, []).append(task)

    bar = tqdm(total=len(tasks), desc="fundamental analysis", unit="task")
    try:
        for asset_tasks in tasks_by_asset.values():
            asset_filings: list[tuple[_YearTask, FilingRef]] = []
            for task in asset_tasks:
                bar.set_postfix_str(f"{task.ticker} {task.form} {task.year}")
                for ref in _list_task_filings(engine, task):
                    asset_filings.append((task, ref))
                bar.update(1)

            asset_filings.sort(
                key=lambda pair: (pair[1].filing_date or "", pair[1].accession_number)
            )
            for task, ref in asset_filings:
                _run_filing(engine, task, ref)
    finally:
        bar.close()


#: Where the gateway's own corrections are logged (P3 round 1, item K). Every correction whose
#: ``corrected`` is null is a row the gateway **dropped**: the log is what makes "the gateway dropped
#: the row" measurable on the filings production never had. One JSON object per line, keyed by the
#: accession.
DROP_LOG_ENV = "KG_GATEWAY_DROP_LOG"
DEFAULT_DROP_LOG = "out/gateway_drops.jsonl"


def log_gateway_drops(payload: Mapping[str, Any], accession: str) -> int:
    """Log every gateway correction that nulled a value (statement, concept, column, original, reason).

    The gateway's payload carries its own ``corrections`` (T-118): each entry records a value it
    changed. An entry with ``corrected`` null is a row the gateway **nulled** -- the filed value
    is in ``original`` and the reason in ``reason`` (portfolio-data-mining 5c77262's
    ``no_filed_nondimensional_fact``).

    The ``original`` is an EDGAR value, and EDGAR values are what the log is for: the reader
    matches it against the SEC's facts (item H's class (a)). No Tiingo value ever reaches this log
    -- the log holds the gateway's own corrections, and the gateway serves no price.

    The log is **appended**: a retry or a resume fetches the same payload again and writes the same
    rows. The readers count the distinct ``(accession, concept, column, original)``.

    Returns the number of corrections logged.
    """
    rows = [
        c
        for c in (payload.get("corrections") or [])
        if isinstance(c, dict) and c.get("corrected") is None
    ]
    if not rows:
        return 0
    path = Path(os.environ.get(DROP_LOG_ENV, DEFAULT_DROP_LOG))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    {
                        "accession": accession,
                        "statement": row.get("statement"),
                        "concept": row.get("concept"),
                        "column": row.get("column"),
                        "original": row.get("original"),
                        "rule": row.get("rule"),
                        "reason": row.get("reason"),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
    return len(rows)


def _list_task_filings(engine: _Engine, task: _YearTask) -> list[FilingRef]:
    try:
        refs = _list_filings(engine, task)
    except EdgarNotFoundError:
        _count(engine, skipped=1)  # the company filed no such form that year
        return []
    except Exception as exc:  # one bad company/year must not stop the batch
        _record_failure(engine, task, None, exc)
        return []
    selected = _select_filings(task, refs)
    _count(engine, skipped=len(refs) - len(selected))  # amendments: never ingested (L-04)
    return selected


def _run_task(engine: _Engine, task: _YearTask) -> None:
    """One (ticker, form, filing-year): list its filings, then ingest each one."""
    for ref in _list_task_filings(engine, task):
        _run_filing(engine, task, ref)


#: The waits between the retries of a **service** failure (T-017, PR #178 review round 1): the
#: filing is retried in place, so the company's next filing reads the recovered one. 30 s, 1 min and
#: 2 min -- three more tries, all well under the supervisor's 20-minute stall window.
RETRY_WAITS_SECONDS: tuple[float, ...] = (30.0, 60.0, 120.0)


#: The reason the resolvers give when the gateway served a payload with no statement row at all
#: (item A). Such a payload is a **service** failure: the filing may serve a moment later.
EMPTY_PAYLOAD_REASON = "the gateway served an empty payload"


class _Unusable(Exception):
    """The payload does not hold a period of its own (T-140), or its period is not the filing's.

    The reason is recorded with the filing (never dropped silently). Only the **empty payload** is
    retried in place (reviewer A, P3 round 1); every other reason is a property of the filing and
    is recorded at once.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = reason == EMPTY_PAYLOAD_REASON


def _is_retryable(exc: Exception) -> bool:
    """Whether a raised failure is the **gateway's** (PR #178, review round 2).

    Only an :class:`EdgarError` that classifies as a *service* failure is retried: the client
    already wraps timeouts, DNS failures and connection resets into it, after its own retries, and
    the classifier separates those from the gateway's own extraction defects (GEHC's
    ``'NoneType' object has no attribute 'to_dataframe'``), which fail the same way on every run.

    A code error -- a ``KeyError``, a ``TypeError``, a database error -- is ours, not the
    gateway's: it is recorded at once. Retrying it would spend 3.5 minutes per filing and could
    add hours to an overnight run.
    """
    return isinstance(exc, EdgarError) and classify("process", str(exc)) == "service"


def _run_filing(engine: _Engine, task: _YearTask, ref: FilingRef) -> None:
    """Analyse one filing, retrying it **in place** when the failure is about the service.

    T-017 (PR #178, review round 1): a filing that fails for a **gateway** reason (an
    :class:`EdgarError`: an outage, a name-resolution failure, a connection reset -- or an empty
    payload) may serve a moment later, so it is retried right away, **before the company's next
    filing is analysed** -- the next filing's growth, TTM and rule inputs then read the recovered
    one. A filing that fails for a *data* reason fails the same way on every run, and a **code**
    error is ours, not the gateway's: both are recorded at once, and the run goes on.

    The waits (30 s, 1 min, 2 min) stay well under the supervisor's 20-minute stall window, even
    for a year-task with three failing 10-Qs (3 x 3.5 min = 10.5 min).
    """
    attempt = 0
    while True:
        try:
            done, skipped = _process_filing(engine, task, ref)
        except EdgarNotFoundError:
            _count(engine, skipped=1)
            return
        except FilingIdentityError as exc:  # another company's filing: never stored
            _record_failure(engine, task, ref, exc, stage="identity")
            return
        except _Unusable as exc:  # the payload does not hold a period of its own
            if exc.retryable and attempt < len(RETRY_WAITS_SECONDS):
                time.sleep(RETRY_WAITS_SECONDS[attempt])
                attempt += 1
                continue
            _record_unusable(engine, task, ref, exc.reason)
            _count(engine, skipped=1)
            return
        except Exception as exc:  # one bad filing must not stop its siblings or the batch
            if _is_retryable(exc) and attempt < len(RETRY_WAITS_SECONDS):
                time.sleep(RETRY_WAITS_SECONDS[attempt])
                attempt += 1
                continue
            _record_failure(engine, task, ref, exc)
            return
        _count(engine, completed=done, skipped=skipped)
        return


def _count(engine: _Engine, *, completed: int = 0, skipped: int = 0) -> None:
    report = engine.report
    report.completed += completed
    report.skipped += skipped
    for _ in range(completed):
        db.bump_run_counter(engine.conn, report.run_id, "completed_units")
    for _ in range(skipped):
        db.bump_run_counter(engine.conn, report.run_id, "skipped_units")


def _record_failure(
    engine: _Engine,
    task: _YearTask,
    ref: FilingRef | None,
    exc: Exception,
    *,
    stage: str = "process",
) -> None:
    report = engine.report
    where = f" [{ref.accession_number}]" if ref else ""
    message = f"{ref.accession_number}: {exc}" if ref else str(exc)
    failure_class = classify(stage, message)
    report.failed += 1
    if failure_class == "service":
        report.service_failed += 1
    report.errors.append(f"{task.ticker} {task.form} {task.year}{where} [{failure_class}]: {exc}")
    db.bump_run_counter(engine.conn, report.run_id, "failed_units")
    db.record_error(
        engine.conn,
        report.run_id,
        RunError(task.ticker, task.form, str(task.year), stage, message),
    )


def _list_filings(engine: _Engine, task: _YearTask) -> list[FilingRef]:
    """The task's filings for its form and year, listed under every CIK in the asset's chain.

    The asset's own CIK is listed first, then its recorded predecessors' (APP-10b: ExxonMobil Holdings
    Corp's filings before 2026-07-01 are its predecessor's). A filing listed under more than one -- XOM's
    2026-08-03 10-Q is listed under both -- is kept once, by accession, under the first. Each row carries
    the CIK it was listed under. Raises :class:`EdgarNotFoundError` when the company exists but filed
    nothing that year (a skip), and a plain :class:`EdgarError` when the gateway knows no company by
    any of the CIKs (a failure worth seeing). A CIK finds a company that left the index; a ticker would not."""
    found: dict[str, FilingRef] = {}
    known = False
    for cik in task.ciks:
        try:
            refs = engine.edgar.filing_by_year(cik, task.form, task.year)
        except EdgarNotFoundError:
            continue
        known = True
        for ref in refs:
            found.setdefault(ref.accession_number, replace(ref, cik=ref.cik or cik))
    if not known:
        raise EdgarError(f"no EDGAR match for {task.ticker!r} (CIK {', '.join(task.ciks)})")
    if not found:
        raise EdgarNotFoundError(f"no {task.form} filing for {task.ticker} in {task.year}")
    return list(found.values())


class SharedAccessionsError(RuntimeError):
    """The database still holds legacy rows that share one filing's accession (T-120)."""


def _refuse_shared_accessions(conn: Database) -> None:
    """Refuse to run over legacy pre-T-091 rows: several quarters stored from one 10-Q,
    sharing its accession, filing date and facts. A run would re-key such a row to its own
    accession in place and append the real facts beside the borrowed ones -- one filing
    reading two filings' numbers. ``repair-accessions`` replaces them first."""
    rows = db.shared_accession_filings(conn)
    if rows:
        accessions = {(r["asset_id"], r["accession_number"]) for r in rows}
        tickers = sorted({str(r["ticker"]) for r in rows})
        raise SharedAccessionsError(
            f"{len(rows)} filing rows share {len(accessions)} accession numbers "
            f"({', '.join(tickers)}) -- legacy pre-T-091 quarters; run "
            "`python -m fundamental_agent repair-accessions --apply` first (T-120)"
        )


def _is_amendment(ref: FilingRef) -> bool:
    return ref.form.strip().upper().endswith("/A")


def _refuse_identity_violations(conn: Database) -> None:
    """Refuse to run over stored filings that break the filing-identity invariant (T-149): the
    triggers keep new writes out, but never check the rows already there."""
    identity.require(conn)


def _select_filings(task: _YearTask, refs: Sequence[FilingRef]) -> list[FilingRef]:
    """The filings to ingest, **oldest first**, **originals only**.

    A 10-K/A or 10-Q/A is never ingested (PER-02, L-04): a period's value is the one in its
    original filing, and an amendment of the same period would replace it in every reader (they
    order a company's filings by period end, then publication time). The gateway lists amendments
    beside their originals, so a year whose newest filing is a 10-K/A keeps its original 10-K --
    and a Part III-only amendment, which has no statements, no longer costs the year its figures.

    Chronological order matters: F4's trailing-twelve-month figure for a 10-Q reads the prior
    quarters' already-recorded values (``db.ttm_flows``), so Q1 must be recorded before Q2 and Q2
    before Q3. The gateway lists most-recent-first, hence the re-sort."""
    originals = [r for r in refs if not _is_amendment(r)]
    return sorted(originals, key=lambda r: (r.filing_date or "", r.accession_number))


class FilingIdentityError(RuntimeError):
    """The gateway listed another company's filing under the asset's CIK (T-149)."""


def _check_filer(task: _YearTask, ref: FilingRef) -> None:
    """Refuse a filing whose own CIK is not in the asset's chain -- its own or a recorded predecessor's
    (``cik_succession``): a ticker reassigned, or a listing keyed wrongly, would otherwise attach one
    company's statements to another's history. A listing that carries no CIK (a gateway before
    portfolio-data-mining T-045) cannot be checked."""
    if ref.cik is not None and ref.cik not in task.ciks:
        raise FilingIdentityError(
            f"{ref.accession_number} is listed under CIK {ref.cik}, not {task.ticker}'s "
            f"{' or '.join(task.ciks)}; not stored"
        )


def _process_filing(engine: _Engine, task: _YearTask, ref: FilingRef) -> tuple[int, int]:
    _check_filer(task, ref)
    as_of = engine.params.analysis_date
    # No lookahead: never a filing dated after the analysis date, and never one filed after the
    # company's exit (T-153 part 3a) -- the same rule for a member and a leaver.
    limit = min(as_of, task.until) if task.until else as_of
    if ref.filing_date and ref.filing_date > limit:
        return 0, 1  # no lookahead: this filing was filed after the analysis date (or the exit)
    if not ref.filing_date:
        return 0, 1  # T-107: an undated filing is never usable -- no metrics, no score
    if not engine.params.fresh and ref.accession_number in engine.completed_accessions:
        return 0, 1  # already ingested and scored by an earlier run

    filer = (
        ref.cik or task.cik
    )  # the CIK it was listed under: a predecessor's for XOM before 2026-07-01
    payload = engine.edgar.financials(
        filer, task.form, task.year, accession_number=ref.accession_number
    )
    log_gateway_drops(payload, ref.accession_number)
    stmts = Statements.from_payload(payload, reit=task.reit)
    meta = FilingMeta(filing_date=ref.filing_date, accession_number=ref.accession_number, cik=filer)

    resolved = _resolve_target(stmts, task, _fye_lookup(engine, task))
    if isinstance(resolved, str):
        # T-140: a filing with no quarter (or year) of its own is never dropped silently
        raise _Unusable(resolved)

    mismatch = _period_end_mismatch(resolved, ref)
    if mismatch is not None:
        raise _Unusable(mismatch)  # ID-07 is a BLOCK: not stored, recorded

    if resolved.label_override is not None:
        # C4 (T-151 part 1): not a failure -- the filing is stored with the corrected label; the
        # run's list carries it as a warning, as T-149 records its checks.
        _record_label_override(engine, task, resolved, ref)

    done = skipped = 0
    for target in [resolved]:
        if target.period.date > as_of:
            skipped += 1
            continue
        unit = (task.ticker, task.form, target.period.date)
        if not engine.params.fresh and unit in engine.completed:
            skipped += 1
            continue
        _analyze_one(engine, task, stmts, target, meta)
        engine.completed.add(unit)
        engine.completed_accessions.add(ref.accession_number)
        done += 1
    return done, skipped


def _period_end_mismatch(target: _Target, ref: FilingRef) -> str | None:
    """Why the period end the payload gave is not the filing's own, or ``None`` (ID-07, PER-08).

    The key a filing is stored under is read off the statements (the latest fiscal-year or quarter
    column); the SEC says which period the filing reports (``periodOfReport``, the header field the
    gateway lists as ``period_of_report``). The two must end at the same month end. Expand Energy's
    FY2021 10-K is the case: fresh-start accounting split the year into a predecessor and a
    successor period, the payload has no full-year 2021 column, and the latest one -- FY2020 -- was
    stored under the FY2021 filing's accession. A filing with no ``period_of_report`` (the SEC gives
    none, or the gateway predates portfolio-data-mining T-045) cannot be checked."""
    if ref.period_of_report is None:
        return None
    try:
        reported = date.fromisoformat(ref.period_of_report)
    except ValueError:
        return f"the SEC's periodOfReport {ref.period_of_report!r} is not a date"
    if same_period_end(date.fromisoformat(target.period.date), reported):
        return None
    return (
        f"the payload's {target.period.key} ends {target.period.date}, not at the SEC's "
        f"periodOfReport {ref.period_of_report} (ID-07): the statements do not hold this "
        "filing's own period"
    )


def _fye_lookup(engine: _Engine, task: _YearTask) -> Callable[[str], date | None]:
    """The asset's latest stored 10-K period end before a date: the fallback fiscal year end its
    quarters are counted from when the payload's balance sheet carries none (T-140)."""

    def lookup(before: str) -> date | None:
        stored = db.latest_fiscal_year_end(engine.conn, task.asset_id, before)
        return date.fromisoformat(stored) if stored else None

    return lookup


def _record_unusable(engine: _Engine, task: _YearTask, ref: FilingRef, reason: str) -> None:
    """A filing the run cannot place in a period: counted as skipped, and recorded with its reason
    in ``analysis_run_error`` and the run report -- before T-140 it was neither (APO's Q1-2023
    10-Q, whose payload carries only the prior fiscal year's column)."""
    message = f"{ref.accession_number}: {reason}"
    engine.report.errors.append(f"{task.ticker} {task.form} {task.year} [skipped]: {message}")
    db.record_error(
        engine.conn,
        engine.report.run_id,
        RunError(task.ticker, task.form, str(task.year), "period", message),
    )


def _record_label_override(
    engine: _Engine, task: _YearTask, target: _Target, ref: FilingRef
) -> None:
    """C4 (PER-08, T-151 part 1): a quarter label the payload's fiscal year end disagreed with,
    overridden from the company's own year end. The filing **is** stored, with the corrected label,
    so this is a warning in the run's list, never a skipped filing."""
    message = f"{ref.accession_number}: {target.label_override}"
    engine.report.errors.append(
        f"{task.ticker} {task.form} {target.fiscal_period} [warning] label: {message}"
    )
    db.record_error(
        engine.conn,
        engine.report.run_id,
        RunError(task.ticker, task.form, target.fiscal_period, "label", message),
    )


def _targets(
    stmts: Statements,
    task: _YearTask,
    fye_lookup: Callable[[str], date | None] | None = None,
) -> list[_Target]:
    """The filing's **own** reporting period, and nothing else -- an empty list when it has none
    (:func:`_resolve_target` says why).

    A payload carries comparative columns (the prior quarter, the prior year) beside the
    period the filing reports. Those are facts about earlier filings; recording them as a
    filing of their own would stamp them with *this* filing's accession and date (T-091:
    45 asset-accession pairs carried several fiscal periods). The own period is the latest
    fiscal year (10-K) or the latest quarter (10-Q) the payload holds. The old
    ``period.year == task.year`` filter is gone: ``task.year`` is the *filing* year, so it
    also dropped a January 10-Q for a December quarter."""
    target = _resolve_target(stmts, task, fye_lookup)
    return [] if isinstance(target, str) else [target]


def _resolve_target(
    stmts: Statements,
    task: _YearTask,
    fye_lookup: Callable[[str], date | None] | None = None,
) -> _Target | str:
    """The filing's own period as a :class:`_Target`, or the reason it has none.

    A 10-K is labelled ``FY<year>`` (:func:`fundamental_agent.fiscal.label_year`: a year ending in
    the first week of January is the year before's, so J&J's 2023-01-01 and 2023-12-31 differ). A 10-Q is labelled ``<year>Q<n>`` with *n* counted from the
    fiscal year end (T-140, :mod:`fundamental_agent.fiscal`) -- never the gateway's column tag,
    which a 52/53-week calendar shifts a quarter ahead. The year end is the balance sheet's own
    comparative column, else the asset's latest stored 10-K before the period (*fye_lookup*)."""
    if task.form == "10-K":
        if stmts.is_empty:
            return EMPTY_PAYLOAD_REASON
        period = stmts.latest_fy()
        if period is None:
            return f"the payload has no fiscal-year column (periods: {_columns(stmts)})"
        year = label_year(date.fromisoformat(period.date))
        return _Target(period, f"FY{year}", stmts.prior_of(period), year)

    return _resolve_quarter(stmts, fye_lookup)


def _resolve_quarter(
    stmts: Statements, fye_lookup: Callable[[str], date | None] | None
) -> _Target | str:
    """A 10-Q's own quarter as a :class:`_Target`, or why the payload has none."""
    if stmts.is_empty:
        return EMPTY_PAYLOAD_REASON
    quarters = stmts.quarter_periods()
    if not quarters:
        # T-159c: a 16/24-week filer's untagged duration columns are its quarter columns (T-159b's
        # F0 fix keeps them; the gateway tags none). The latest one is the filing's own period,
        # exactly as the latest (Qx) column is for a tagged payload.
        quarters = stmts.untagged_periods()
    if not quarters:
        return (
            f"the payload has no quarter column of its own (periods: {_columns(stmts)}); "
            "the gateway returned only another period"
        )
    period = max(quarters, key=lambda p: p.date)
    end = date.fromisoformat(period.date)
    instants = [p.date for p in stmts.instant_periods()]
    latest = max(instants, default=None)
    # The filing's own quarter column ends on its balance sheet's date (the same month end, PER-08);
    # one that ends before it is a comparative.
    if (
        latest is not None
        and date.fromisoformat(latest) > end
        and not same_period_end(date.fromisoformat(latest), end)
    ):
        return (
            f"the latest quarter column {period.key} ends before the balance sheet's {latest}: "
            f"a comparative, not the filing's own quarter (periods: {_columns(stmts)})"
        )
    # The filing's own balance sheet first: it is the fiscal year end *this* quarter is counted
    # from, even when the company changed its fiscal year (FERG: July -> December, so its stored
    # 10-K of 2025-07-31 would put 2026-06-30 at no quarter). The stored 10-K is the fallback.
    fye = payload_fiscal_year_end(instants, end) or (
        fye_lookup(period.date) if fye_lookup else None
    )
    if fye is None:
        return f"no fiscal year end to count the quarter ending {period.date} from"
    label = quarter_label(end, fye)
    if label is None:
        return f"{period.date} is not a quarter end of the fiscal year ending {fye}"
    # C4 (PER-08, T-151 part 1). A 10-Q's quarter label follows its period end and the *company's*
    # fiscal year end -- the latest stored 10-K before the period. ``payload_fiscal_year_end`` reads
    # the earliest balance-sheet instant 30-330 days back, so a payload whose face carries stray
    # note instants selects a quarter end for the year end and labels the filing a quarter it is
    # not: CSX 0000277948-24-000047 (period end 2024-06-30, the payload's 2023-09-30 -> 2024Q3, its
    # own year end 2024-12-31 -> 2024Q2) and CNP's Q1-2022 (a stray 2021-07-20 -> 2022Q3, Dec year
    # end -> 2022Q1). The company's own year end wins; a disagreement is flagged and logged by the
    # caller. A company with no stored 10-K, or a changed fiscal year whose new year end is not
    # recorded, keeps the payload's label.
    label_override = None
    known = fye_lookup(period.date) if fye_lookup else None
    if known is not None:
        preferred = quarter_label(end, known)
        if preferred is not None and preferred != label:
            label_override = (
                f"label {label} overridden to {preferred}: the payload's fiscal year end {fye} "
                f"disagrees with the company's own {known} for period end {period.date} (C4/PER-08)"
            )
            label = preferred
    return _Target(
        period, label, stmts.prior_of(period), label_year(end), label_override=label_override
    )


def _columns(stmts: Statements) -> str:
    return ", ".join(p.key for p in stmts.periods if not p.is_instant) or "none"


def _asset_cik(engine: _Engine, asset_id: int) -> str | None:
    row = engine.conn.execute("SELECT cik FROM assets WHERE id = ?", (asset_id,)).fetchone()
    return str(row["cik"]) if row and row["cik"] else None


def _store_cover_shares(
    engine: _Engine, task: _YearTask, filing_id: int, stmts: Statements, target: _Target
) -> None:
    """Record the filing's cover-page share count (T-132). A filing the gateway returned no count
    for is left without one -- never given a balance-sheet or weighted-average stand-in -- and a
    failed cover read is recorded for triage, never failing the filing."""
    if stmts.cover_error:
        db.record_error(
            engine.conn,
            engine.report.run_id,
            RunError(task.ticker, task.form, target.fiscal_period, "cover", stmts.cover_error),
        )
    if stmts.cover_shares:
        db.insert_cover_shares(
            engine.conn,
            filing_id,
            stmts.cover_shares,
            event_time=target.period.date,
            run_id=engine.report.run_id,
        )


def _extract_sections(
    engine: _Engine, task: _YearTask, filing_id: int, target: _Target, meta: FilingMeta
) -> None:
    """Best-effort narrative-text extraction. A failure here never fails the filing."""
    if not meta.accession_number:
        return
    cik = meta.cik or _asset_cik(engine, task.asset_id)  # the archive folder is the filer's
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
    # F1 (T-151 part 2 fix 1): a REIT's own rate on a 10-Q is its TTM tax / TTM pre-tax income, on
    # the same basis as the TTM operating income the ratios tax.
    "income_tax",
    "pretax_income",
)


def _is_first_quarter(stmts: Statements, target: _Target) -> bool:
    """Whether *target* is its fiscal year's first quarter, so that its quarter column *is* its
    year-to-date. The gateway's ``(Q1)`` tag says so for a calendar-aligned filer, but it tags a
    column by the calendar quarter its date falls in: Waters' first quarter ends 2026-04-04 and
    arrives as ``(Q2)`` with no ``(YTD)`` column at all (T-133). The balance sheet's comparative
    column -- the last fiscal year end -- is what places it."""
    if target.period.tag == "Q1":
        return True
    end = date.fromisoformat(target.period.date)
    # a first quarter ends one quarter (PER-08: [89, 98] days, 52/53-week calendars included) after
    # the last fiscal year end; a later quarter's sits two or three
    return any(
        within((end - date.fromisoformat(p.date)).days, QUARTER_DAYS)
        for p in stmts.periods
        if p.is_instant
    )


def _ytd_columns(stmts: Statements, target: _Target) -> tuple[str | None, str | None, str | None]:
    """``(this year's YTD column, last year's same YTD column, that column's end date)`` of a
    10-Q. A first quarter's year-to-date *is* its quarter, so a Q1 filing with no ``(YTD)``
    columns uses its ``(Q1)`` columns (T-105)."""
    end = date.fromisoformat(target.period.date)
    tags = ("YTD", target.period.tag) if _is_first_quarter(stmts, target) else ("YTD",)
    for tag in tags:
        # a first quarter's prior-year column can carry another quarter tag than its own (Waters:
        # (Q2) beside (Q1)), so any quarter column stands in for it
        periods = [p for p in stmts.periods if (p.is_quarter if tag != "YTD" else p.tag == tag)]
        current = next((p for p in periods if p.date == target.period.date), None)
        prior = next((p for p in periods if is_year_apart(date.fromisoformat(p.date), end)), None)
        if current is not None and prior is not None:
            return current.key, prior.key, prior.date
    return None, None, None


# Audit round #10 (Q8, user-approved): a 10-Q's quarter cash flow, filed year-to-date only, derived as this filing's
# year-to-date less the previous 10-Q's year-to-date of the same fiscal year -- and the prior-year quarter from the two
# filings' own comparative columns -- each value read from its own filing. Flagged ``quarter_derived``.
_DERIVED_QUARTER_ITEMS = ("operating_cash_flow", "capital_expenditure")
# the previous 10-Q closes one fiscal quarter earlier: 12 to 16 weeks for a 52/53-week calendar included
_PREVIOUS_QUARTER_DAYS = (75, 125)


def _ytd_pair_keys(stmts: Statements, end: str) -> tuple[str | None, str | None]:
    """The cash-flow statement's year-to-date column at *end* and its comparative one year earlier: the
    ``(YTD)``-tagged pair, else a quarter-tagged pair (a first quarter's year to date is its quarter), else the
    untagged pair (R2)."""
    end_date = date.fromisoformat(end)
    keys = {str(k) for row in stmts.raw.get("cash_flow", []) for k in row}
    picks: tuple[Callable[[Period], bool], ...] = (
        lambda p: p.tag == "YTD",
        lambda p: p.is_quarter,
        lambda p: p.tag == "",
    )
    for pick in picks:
        periods = [p for p in stmts.periods if pick(p) and p.key in keys]
        current = next((p for p in periods if p.date == end), None)
        prior = next(
            (p for p in periods if is_year_apart(date.fromisoformat(p.date), end_date)), None
        )
        if current is not None and prior is not None:
            return current.key, prior.key
    return None, None


def _derived_quarter_flows(  # noqa: PLR0911 - one return per missing term
    engine: _Engine, task: _YearTask, stmts: Statements, target: _Target
) -> dict[str, tuple[float, float]]:
    """Q8: ``{item: (this quarter, the prior-year quarter)}`` for the FCF items of a 10-Q that files them year to
    date only; empty when the quarter is filed, on a first quarter, or when either filing lacks a value."""
    if task.form != "10-Q" or target.prior is None or _is_first_quarter(stmts, target):
        return {}
    if any(stmts.get(item, target.period.key) is not None for item in _DERIVED_QUARTER_ITEMS):
        return {}
    fy_key = stmts.prior_fiscal_year_end_key(target.period.key)
    if fy_key is None:
        return {}
    row = engine.conn.execute(
        "SELECT id, period_end FROM sec_filings WHERE asset_id = ? AND form = '10-Q' "
        "AND period_end < ? AND period_end > ? ORDER BY period_end DESC LIMIT 1",
        (task.asset_id, target.period.date, fy_key[:10]),
    ).fetchone()
    if row is None:
        return {}
    gap = (date.fromisoformat(target.period.date) - date.fromisoformat(row["period_end"])).days
    if not within(gap, _PREVIOUS_QUARTER_DAYS):
        return {}
    cur_key, prior_key = _ytd_pair_keys(stmts, target.period.date)
    previous = db.statements_from_facts(engine.conn, row["id"])
    prev_cur, prev_prior = _ytd_pair_keys(previous, row["period_end"])
    if not (cur_key and prior_key and prev_cur and prev_prior):
        return {}
    out: dict[str, tuple[float, float]] = {}
    for item in _DERIVED_QUARTER_ITEMS:
        this_ytd = stmts.get(item, cur_key)
        prior_ytd = stmts.get(item, prior_key)
        prev_ytd = previous.get(item, prev_cur)
        prev_prior_ytd = previous.get(item, prev_prior)
        if this_ytd is None or prior_ytd is None or prev_ytd is None or prev_prior_ytd is None:
            return {}
        out[item] = (this_ytd - prev_ytd, prior_ytd - prev_prior_ytd)
    return out


# the TTM items the cash-flow statement carries (R2)
_CASH_FLOW_TTM_ITEMS = (
    "operating_cash_flow",
    "capital_expenditure",
    "depreciation_amortization",
    "stock_based_compensation",
)


def _untagged_cash_flow_ytd(
    stmts: Statements, target: _Target
) -> tuple[str | None, str | None, str | None]:
    """Audit round #10 (R2): the cash-flow statement's own year-to-date pair when it arrives in **untagged** columns
    only -- a 52/53-week filer's 24- or 28-week cash flow (COST, DPZ, PEP, KR, AZO, AAP, LHX), which the gateway tags
    neither ``(Qn)`` nor ``(YTD)``. A 10-Q's statement of cash flows covers the year to date (Reg. S-X 10-01(c)(4)),
    so its untagged columns at the period end and one year earlier are that pair. ``(None, None, None)`` when the
    cash-flow statement carries a tagged column at the period end."""
    keys = {str(k) for row in stmts.raw.get("cash_flow", []) for k in row}
    if any(k.startswith(target.period.date + " (") for k in keys):
        return None, None, None
    end = date.fromisoformat(target.period.date)
    untagged = [p for p in stmts.periods if p.tag == "" and p.key in keys]
    current = next((p for p in untagged if p.date == target.period.date), None)
    prior = next((p for p in untagged if is_year_apart(date.fromisoformat(p.date), end)), None)
    if current is None or prior is None:
        return None, None, None
    return current.key, prior.key, prior.date


def _ttm_flows(
    engine: _Engine,
    task: _YearTask,
    stmts: Statements,
    target: _Target,
    *,
    accession: str | None = None,
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
    cf_cur, cf_prior, cf_prior_end = _untagged_cash_flow_ytd(stmts, target)
    if cf_cur and cf_prior:
        for item in _CASH_FLOW_TTM_ITEMS:
            pair = ytd.get(item)
            if pair is None or (pair.current is None and pair.prior is None):
                ytd[item] = db.YTDPair(
                    stmts.get(item, cf_cur), stmts.get(item, cf_prior), cf_prior_end
                )
    if not current and not ytd:
        return {}
    return db.ttm_detail(
        engine.conn,
        task.asset_id,
        period_end=target.period.date,
        current=current,
        ytd=ytd,
        accession=accession,
        # D-17 (audit round #11): the TTM's one-method rule for a derived operating income
        derived=frozenset(
            item
            for item in ("operating_income",)
            if stmts.resolve(item, target.period.key).how == EBIT_DERIVED_HOW
        ),
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
        FilingKey(task.asset_id, task.form, target.fiscal_year, target.fiscal_period),
        FilingMeta(
            filing_date=meta.filing_date,
            accession_number=meta.accession_number,
            period_end=target.period.date,
            cik=meta.cik,
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
    _store_cover_shares(engine, task, filing_id, stmts, target)
    if engine.params.sections:
        _extract_sections(engine, task, filing_id, target, meta)

    # This filing's own share-count facts are already in `financial_facts` (just
    # appended above) -- cross-check them against this asset's prior filings for a
    # known SEC XBRL scale/tagging defect (docs/model_fixes.md, F1) before valuation
    # multiplies a possibly-mis-scaled share count by price.
    share_scale_factors = db.detect_share_scale_factors(
        engine.conn, task.asset_id, stmts, target.period.key, exclude_filing_id=filing_id
    )

    mc_res = market_caps_as_of(engine.conn, [task.asset_id], as_of=target.period.date)
    market_cap_obj = mc_res.caps.get(task.asset_id)
    market_cap_missing = mc_res.missing.get(task.asset_id)

    # T-151 part 2: the company profile (D3/D4) and the prior-year capital the return, leverage and
    # valuation ratios divide by (MET-01/02, MD-02/35/38). The prior-year quarter's balance sheet is
    # another filing's, so it is read here, never inside a metric.
    db.attach_context(
        engine.conn,
        task.asset_id,
        task.form,
        target.period.key,
        stmts,
        accession=meta.accession_number,
    )

    ttm = _ttm_flows(engine, task, stmts, target, accession=meta.accession_number)
    derived_quarter = _derived_quarter_flows(engine, task, stmts, target)
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
        cover_scale_suspect=db.cover_share_scale_suspect(
            engine.conn,
            task.asset_id,
            stmts.cover_shares,
            filing_id=filing_id,
            filing_date=meta.filing_date,
        ),
        ttm_flows=ttm,
        derived_quarter=derived_quarter,
        cik=_asset_cik(engine, task.asset_id),
        market_cap=market_cap_obj,
        market_cap_missing=market_cap_missing,
    )

    analyst = engine.analyst
    scoring_model = ""  # only read when there is a score, i.e. the analyst branch below
    if engine.params.metrics_only or analyst is None:
        # T-143 part A, P1: compute the metrics without the analyst and write no score. The
        # identical write sequence below runs for both paths, from the same computed results.
        result: AnalysisResult | None = None
        computed = compute_metrics(ctx)
    else:
        result = analyst.analyze(ctx)
        computed = result.metrics
        scoring_model = analyst.model_name
    db.stamp_ytd(computed, ttm)  # PER-05: this 10-Q's year-to-date, for a later Q4
    db.record_metrics(
        engine.conn, filing_id, computed, event_time=target.period.date, run_id=run_id
    )
    # T-071 part 2a (D-13): the regulated-utility cash-coverage rule input, on a 10-K only. A rule
    # input outside the metric dictionary and the prompt (a pseudo-group row).
    utility_coverage.write(engine.conn, filing_id, task.asset_id, stmts)
    # T-071 part 2b (D1): the VALORIZATION value inputs, on every filing. Same kind of row: read
    # from the results just recorded, so every value is the one its metric row carries.
    value_inputs.write(engine.conn, filing_id, computed)
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

    if result is None:
        # T-143 part A, P1: metrics-only writes everything but the FUNDAMENTAL score -- no
        # score_snapshot row, not even the fallback (the gate/rule-input writes above stand).
        return

    assessment = result.assessment
    # T-113: a fallback row is labelled as such, never under the LLM's own model_id -- 1 of
    # production's 377 FUNDAMENTAL scores was previously indistinguishable from real model output
    # this way. T-073 (D1): a row whose applicable pillar has no value is not scored at all, and is
    # labelled as such too -- the model was never asked about it.
    if assessment.score is None:
        model = UNSCORED_MODEL_LABEL
    elif result.used_fallback:
        model = FALLBACK_MODEL_LABEL
    else:
        model = scoring_model
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
            model=model,
            metrics=result.flat_metrics,
            event_time=target.period.date,
            prompt_hash=result.prompt_hash,
            pillars=result.pillars,
        ),
        run_id=run_id,
    )
    if result.used_fallback:
        db.bump_run_counter(engine.conn, run_id, "fallback_units")


def _params_dict(params: RunParams) -> _Payload:
    return {
        "analysis_date": params.analysis_date,
        "forms": list(params.forms),
        "since_year": params.since_year,
        "until_year": params.resolved_until(),
        "limit": params.limit,
        "tickers": list(params.tickers) if params.tickers else None,
        "fresh": params.fresh,
        "metrics_only": params.metrics_only,
    }


def _as_str(value: object) -> str | None:
    return str(value) if isinstance(value, (str, int, float)) else None
