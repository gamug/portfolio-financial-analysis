"""``python -m fundamental_agent pending``: is there a filing this step has not scored? (T-017)

Read-only (``connect_ro``), no LLM (neither ``build_model`` nor ``FundamentalAnalyst`` is
touched, and no ``LLM_*`` variable is needed) and no write. It answers with one line,
``pending: yes|no <evidence>``, and the orchestrator runs ``run`` only on a yes.

Pending iff an original 10-K/10-Q filed on or before the analysis date has no FUNDAMENTAL
``score_snapshot`` and is not excused. Three sources of such a filing, cheapest first:

1. **Stored but unscored** -- a ``sec_filings`` row with no snapshot (the LLM call failed after
   the facts were stored, or the run was killed). DB only.
2. **Errored** -- an accession named by an ``analysis_run_error`` row with no snapshot. DB only.
3. **Listed** -- the EDGAR gateway lists one the database has not seen. Same call, same
   filters as the step (:func:`fundamental_agent.pipeline._list_filings`, ``_select_filings``,
   the lookahead rule of ``_process_filing``), over the years D-1..D; over 2022..D for a CIK of
   the member's chain that has no stored filing, so a new member or a newly recorded
   succession is listed in full.

A filing whose *latest* error row is a known data failure (:mod:`fundamental_agent.failures`)
is not pending: it fails the same way on every run. A filing whose latest row is a service
failure is pending only while its unit is in this check's plan and no later completed run that
covered the unit left it without a newer error (decided from the run's ``params_json``; a run
missing a field does not count). No part of that needs the gateway. An error row from a run dated
after the analysis date is ignored, and a run with no ``as_of`` counts as in plan: an error row
never makes a filing less pending. A predecessor CIK the step does not link (its last annual
report is not listed, ``_verify_successions``) is dropped from the listing and named in the
evidence as an "unlinked predecessor".

Declared blind spot: a filing of an older year that failed before it was stored and left no
accession in an error row. ``/run/fundamental-agent`` (no check) lists every year and recovers it.
"""

from __future__ import annotations

import itertools
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from dotenv import load_dotenv
from portfolio_common.db import Database, DatabaseError

from fundamental_agent import pipeline
from fundamental_agent.config import DEFAULT_EDGAR_BASE_URL
from fundamental_agent.db import completed_accessions
from fundamental_agent.edgar_client import EdgarClient, EdgarError, EdgarNotFoundError
from fundamental_agent.failures import classify
from fundamental_agent.pipeline import DEFAULT_FORMS, DEFAULT_SINCE_YEAR
from kg_schema import identity
from kg_schema.db import connect_ro
from kg_schema.queries import UniverseMember, members_asof

_ACCESSION_PREFIX = re.compile(r"^(\d{10}-\d{2}-\d{6}): ")
# The stages that record a failed (or unusable) filing. ``label`` is a warning on a stored filing
# and ``succession`` has no accession.
_FILING_STAGES = ("process", "identity", "period")
_SHOWN = 3  # accessions named in the evidence line


@dataclass(frozen=True)
class _ErrorRow:
    run_id: int
    ticker: str
    form: str | None
    fiscal_period: str | None
    stage: str
    message: str


@dataclass
class Verdict:
    """The check's answer: pending items by kind, and the ones excused."""

    pending: list[str] = field(default_factory=list)
    data_failures: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    unlisted: list[str] = field(default_factory=list)
    unlinked: list[str] = field(default_factory=list)  # predecessor CIKs the step does not link
    reason: str | None = None  # a P2 reason (fresh database, missing table) -- yes without reading
    scanned: int = 0  # (cik, form, year) listings made
    partial: bool = False  # stopped at the first pending item

    @property
    def is_pending(self) -> bool:
        return self.reason is not None or bool(self.pending)

    def evidence(self, members: int) -> str:
        if self.reason is not None:
            return self.reason
        extras = []
        if self.data_failures:
            extras.append(
                f"{len(self.data_failures)} known data failure ({_named(self.data_failures)})"
            )
        if self.stale:
            extras.append(f"{len(self.stale)} stale ({_named(self.stale)})")
        if self.unlisted:
            extras.append(f"{len(self.unlisted)} unlisted ({_named(self.unlisted)})")
        if self.unlinked:
            extras.append(f"{len(self.unlinked)} unlinked predecessor ({_named(self.unlinked)})")
        tail = f"; {'; '.join(extras)}" if extras else ""
        if self.pending:
            more = "at least " if self.partial else ""
            return f"{more}{len(self.pending)} unscored filings ({_named(self.pending)}){tail}"
        return f"{members} members current, {self.scanned} listings{tail}"


def _named(items: Sequence[str]) -> str:
    shown = ", ".join(items[:_SHOWN])
    return shown + (", ..." if len(items) > _SHOWN else "")


def _years(analysis_date: str, *, full: bool) -> list[int]:
    last = int(analysis_date[:4])
    first = DEFAULT_SINCE_YEAR if full else max(DEFAULT_SINCE_YEAR, last - 1)
    return list(range(first, last + 1))


def _latest_errors(conn: Database, analysis_date: str) -> dict[str, _ErrorRow]:
    """The latest ``analysis_run_error`` row per accession (by id), filing stages only, among the
    rows of runs with ``as_of`` on or before *analysis_date* or with no ``as_of``. A row from a run
    dated after the analysis date says nothing about it (no lookahead), and an error row must never
    make a filing less pending."""
    rows = conn.execute(
        "SELECT e.run_id, e.ticker, e.form, e.fiscal_period, e.stage, e.message "
        "FROM analysis_run_error e LEFT JOIN analysis_run r ON r.id = e.run_id "
        "WHERE e.stage IN ('process', 'identity', 'period') "
        "AND (r.as_of IS NULL OR r.as_of <= ?) ORDER BY e.id",
        (analysis_date,),
    ).fetchall()
    out: dict[str, _ErrorRow] = {}
    for r in rows:
        match = _ACCESSION_PREFIX.match(str(r["message"] or ""))
        if match is None:
            continue
        out[match.group(1)] = _ErrorRow(
            int(r["run_id"]),
            str(r["ticker"] or ""),
            r["form"],
            r["fiscal_period"],
            str(r["stage"]),
            str(r["message"]),
        )
    return out


class _Judge:
    """Decides, offline, whether an accession without a snapshot is pending."""

    def __init__(
        self,
        conn: Database,
        universe: Database,
        analysis_date: str,
        errors: dict[str, _ErrorRow],
        windows: dict[str, list[int]],
    ) -> None:
        self._conn = conn
        self._universe = universe
        self._date = analysis_date
        self._errors = errors
        self._windows = windows  # member ticker -> the years this check covers for it
        self._runs: list[tuple[int, str | None, dict[str, object]]] | None = None
        self._members_by_date: dict[str, set[str]] = {}

    def verdict(self, accession: str) -> str:
        """``pending`` | ``data`` (known data failure) | ``stale``."""
        row = self._errors.get(accession)
        if row is None:
            return "pending"
        if classify(row.stage, row.message) == "data":
            return "data"
        if self._in_plan(row) and not self._covered_since(row):
            return "pending"
        return "stale"

    def _in_plan(self, row: _ErrorRow) -> bool:
        window = self._windows.get(row.ticker)
        if window is None or row.form not in DEFAULT_FORMS:
            return False
        try:
            year = int(str(row.fiscal_period))
        except ValueError:
            return False
        return year in window

    def _run_as_of(self, run_id: int) -> str | None:
        r = self._conn.execute("SELECT as_of FROM analysis_run WHERE id = ?", (run_id,)).fetchone()
        return str(r["as_of"]) if r and r["as_of"] else None

    def _members(self, as_of: str) -> set[str]:
        if as_of not in self._members_by_date:
            self._members_by_date[as_of] = {m.symbol for m in members_asof(self._universe, as_of)}
        return self._members_by_date[as_of]

    def _later_runs(self) -> list[tuple[int, str | None, dict[str, object]]]:
        if self._runs is None:
            rows = self._conn.execute(
                "SELECT id, as_of, params_json FROM analysis_run WHERE status = 'completed' "
                "ORDER BY id"
            ).fetchall()
            self._runs = []
            for r in rows:
                try:
                    params = json.loads(r["params_json"] or "{}")
                except ValueError:
                    params = {}
                self._runs.append(
                    (int(r["id"]), r["as_of"], params if isinstance(params, dict) else {})
                )
        return self._runs

    def _covered_since(self, row: _ErrorRow) -> bool:
        """A later completed run listed this unit (and, being the latest error row, left no
        newer error for the accession). A run missing a field we need does not count."""
        first_as_of = self._run_as_of(row.run_id)
        year = int(str(row.fiscal_period))
        for run_id, as_of, params in self._later_runs():
            if run_id <= row.run_id or as_of is None or first_as_of is None or as_of < first_as_of:
                continue
            if not _covers(params, row, year):
                continue
            tickers = params["tickers"]
            if tickers is None:
                if row.ticker not in self._members(str(as_of)):
                    continue
            elif row.ticker not in cast("list[str]", tickers):
                continue
            return True
        return False


def _covers(params: dict[str, object], row: _ErrorRow, year: int) -> bool:
    """The part of "covered" that ``params_json`` alone decides."""
    needed = ("forms", "since_year", "until_year", "limit", "tickers")
    if any(key not in params for key in needed) or params["limit"] is not None:
        return False
    forms = params["forms"]
    since, until = params["since_year"], params["until_year"]
    if not isinstance(forms, list) or not isinstance(since, int) or not isinstance(until, int):
        return False
    return row.form in forms and since <= year <= until


def _stored_ciks(conn: Database, asset_id: int) -> set[str]:
    """The CIKs the asset has a stored filing under. ``sec_filings.cik`` is NULL on rows from
    before T-149 and nothing backfills it (``kg_schema/ddl.py``, the additive column), so a NULL
    row belongs to its asset's own CIK: ``COALESCE(sec_filings.cik, assets.cik)``. A predecessor
    CIK with only NULL rows is therefore still "no stored filing" and gets the full window --
    conservative."""
    rows = conn.execute(
        "SELECT DISTINCT COALESCE(f.cik, a.cik) AS cik FROM sec_filings f "
        "JOIN assets a ON a.id = f.asset_id WHERE f.asset_id = ? AND COALESCE(f.cik, a.cik) IS NOT NULL",
        (asset_id,),
    ).fetchall()
    return {str(r["cik"]) for r in rows}


@dataclass
class _Plan:
    """What the check covers: per member, its CIK chain with the window of each CIK."""

    ciks: dict[str, list[tuple[str, bool]]]  # ticker -> [(cik, full window)]
    windows: dict[str, list[int]]  # ticker -> the filing years covered
    # (successor CIK, predecessor CIK) -> effective date, from ``identity.predecessors_of`` as the
    # step reads it (``pipeline._verify_successions``)
    effective: dict[tuple[str, str], str] = field(default_factory=dict)


def _plan(
    conn: Database, members: list[UniverseMember], assets: dict[str, object], analysis_date: str
) -> _Plan:
    plan = _Plan({}, {})
    for m in members:
        cik = identity.normalize_cik(_cik_of(m, assets))
        asset = assets.get(m.symbol)
        chain = identity.cik_chain(conn, cik) if asset is not None else (str(cik),)
        stored = _stored_ciks(conn, int(asset["id"])) if asset is not None else set()  # type: ignore[index]
        plan.ciks[m.symbol] = [(c, c not in stored) for c in chain]
        if asset is not None and cik is not None:
            for pred, effective in identity.predecessors_of(conn, cik):
                plan.effective[(cik, pred)] = effective
        plan.windows[m.symbol] = _years(
            analysis_date, full=any(full for _, full in plan.ciks[m.symbol])
        )
    return plan


class _Sorter:
    """Files each accession without a snapshot under pending / known data failure / stale."""

    def __init__(self, verdict: Verdict, done: set[str], judge: _Judge) -> None:
        self._buckets = {
            "pending": verdict.pending,
            "data": verdict.data_failures,
            "stale": verdict.stale,
        }
        self._done = done
        self._judge = judge
        self._seen: set[str] = set()

    def add(self, accession: str, label: str) -> None:
        if accession in self._done or accession in self._seen:
            return
        self._seen.add(accession)
        self._buckets[self._judge.verdict(accession)].append(f"{label} {accession}")


def _database_clauses(
    conn: Database, analysis_date: str, plan: _Plan, errors: dict[str, _ErrorRow], sorter: _Sorter
) -> None:
    """Clauses 1 and 2: stored-but-unscored filings and errored accessions. DB only."""
    for r in conn.execute(
        "SELECT a.ticker, f.form, f.accession_number, f.filing_date FROM sec_filings f "
        "JOIN assets a ON a.id = f.asset_id "
        "WHERE f.accession_number IS NOT NULL AND f.filing_date IS NOT NULL AND f.filing_date <= ?",
        (analysis_date,),
    ).fetchall():
        in_scope = r["ticker"] in plan.windows and r["form"] in DEFAULT_FORMS
        if in_scope and int(str(r["filing_date"])[:4]) >= DEFAULT_SINCE_YEAR:
            sorter.add(str(r["accession_number"]), f"{r['ticker']} {r['form']}")
    for accession, row in errors.items():
        if row.ticker in plan.windows and row.form in DEFAULT_FORMS:
            sorter.add(accession, f"{row.ticker} {row.form}")


class _Lister:
    """Clause 3: list with the step's own call and filters; stop at the first pending filing."""

    def __init__(
        self, edgar: EdgarClient, analysis_date: str, plan: _Plan, verdict: Verdict, sorter: _Sorter
    ) -> None:
        self._engine = cast("pipeline._Engine", SimpleNamespace(edgar=edgar))
        self._date = analysis_date
        self._plan = plan
        self._verdict = verdict
        self._sorter = sorter
        self._linked: dict[tuple[str, str], bool] = {}

    def run(self, members: list[UniverseMember]) -> None:
        for m in members:
            for cik, full in self._plan.ciks[m.symbol]:
                if self._unlinked(m, cik):
                    continue
                for form, year in itertools.product(DEFAULT_FORMS, _years(self._date, full=full)):
                    self._list(m, cik, form, year)
                    if self._verdict.pending:
                        self._verdict.partial = True
                        return

    def _unlinked(self, m: UniverseMember, cik: str) -> bool:
        """``True`` for a predecessor CIK the step drops: its last annual report is not listed
        (``pipeline._last_annual_report_missing``, APP-10b-3), so its filings are never scored and
        must not keep the step pending. Gateway GETs only; once per succession. A listing that
        cannot be read raises (the step would just not link, but an outage must never read as
        "not pending") and the check fails -> pending."""
        chain = {c for c, _ in self._plan.ciks[m.symbol]}
        # The successor is the CIK whose ``predecessors_of`` returned this predecessor -- the row the
        # succession lookup recorded -- never the chain's first position: ``pipeline`` passes its
        # ``_YearTask.cik`` the same way (T-017's one source of truth).
        key = next((k for k in self._plan.effective if k[1] == cik and k[0] in chain), None)
        if key is None:
            return False  # the member's own CIK (no succession row carries it as a predecessor)
        if key not in self._linked:
            successor, predecessor = key
            reason = pipeline._last_annual_report_missing(
                self._engine, predecessor, self._plan.effective[key], successor
            )
            if reason is not None and "could not be read" in reason:
                raise EdgarError(reason)
            self._linked[key] = reason is None
            if reason is not None:
                self._verdict.unlinked.append(f"{m.symbol} {cik}")
        return not self._linked[key]

    def _list(self, m: UniverseMember, cik: str, form: str, year: int) -> None:
        task = pipeline._YearTask(
            asset_id=0, ticker=m.symbol, company_name=m.security, form=form, year=year, cik=cik
        )
        self._verdict.scanned += 1
        try:
            refs = pipeline._select_filings(task, pipeline._list_filings(self._engine, task))
        except EdgarNotFoundError:
            return
        except EdgarError as exc:
            if not str(exc).startswith("no EDGAR match"):
                raise
            label = f"{m.symbol} {cik}"
            if label not in self._verdict.unlisted:
                self._verdict.unlisted.append(label)
            return
        chain = tuple(c for c, _ in self._plan.ciks[m.symbol])
        for ref in refs:
            if not ref.filing_date or ref.filing_date > self._date:
                continue  # undated, or filed after the analysis date (no lookahead)
            if ref.cik is not None and ref.cik not in chain:
                continue  # another company's filing: the step records it as "identity"
            self._sorter.add(ref.accession_number, f"{m.symbol} {form}")


def check(
    conn: Database,
    universe: Database,
    analysis_date: str,
    *,
    edgar: EdgarClient | None,
) -> tuple[Verdict, int]:
    """The verdict and the member count. *edgar* is only used for clause 3 (and may be
    ``None`` in tests that stop before it)."""
    verdict = Verdict()
    members = members_asof(universe, analysis_date)
    for table in ("assets", "sec_filings", "score_snapshot"):
        if not conn.relation_exists(table):
            verdict.reason = f"no {table} table (fresh database)"
            return verdict, len(members)
    if not members:
        verdict.reason = f"universe has no members as of {analysis_date}"
        return verdict, 0
    assets = {str(r["ticker"]): r for r in conn.execute("SELECT id, ticker, cik FROM assets")}
    keyless = [m.symbol for m in members if identity.normalize_cik(_cik_of(m, assets)) is None]
    if keyless:
        verdict.reason = f"{len(keyless)} members lack a CIK ({_named(keyless)})"
        return verdict, len(members)

    plan = _plan(conn, members, assets, analysis_date)
    errors = (
        _latest_errors(conn, analysis_date) if conn.relation_exists("analysis_run_error") else {}
    )
    sorter = _Sorter(
        verdict,
        completed_accessions(conn),
        _Judge(conn, universe, analysis_date, errors, plan.windows),
    )
    _database_clauses(conn, analysis_date, plan, errors, sorter)
    if not verdict.pending and edgar is not None:
        _Lister(edgar, analysis_date, plan, verdict, sorter).run(members)
    return verdict, len(members)


def _cik_of(member: UniverseMember, assets: dict[str, object]) -> object:
    asset = assets.get(member.symbol)
    if asset is not None:
        return asset["cik"]  # type: ignore[index]
    return member.cik


def pending_line(
    db_path: Path, universe_db_path: Path, analysis_date: str, *, edgar: EdgarClient | None = None
) -> str:
    """The single ``pending: yes|no <evidence>`` line."""
    if not Path(db_path).exists():
        return f"pending: yes {db_path} does not exist (fresh database)"
    conn = connect_ro(db_path)
    universe = connect_ro(universe_db_path)
    owns = edgar is None
    client = edgar or EdgarClient(os.environ.get("EDGAR_BASE_URL", DEFAULT_EDGAR_BASE_URL))
    try:
        try:
            verdict, n = check(conn, universe, analysis_date, edgar=client)
        except DatabaseError as exc:  # a partial schema is a P2 doubt
            return f"pending: yes cannot read the database ({exc})"
        return f"pending: {'yes' if verdict.is_pending else 'no'} {verdict.evidence(n)}"
    finally:
        if owns:
            client.close()
        universe.close()
        conn.close()


def run_pending(db_path: Path, universe_db_path: Path, analysis_date: str) -> int:
    load_dotenv(override=False)  # the step reads the same .env (Settings.load)
    print(pending_line(db_path, universe_db_path, analysis_date))
    return 0
