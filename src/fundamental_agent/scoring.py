"""``python -m fundamental_agent score``: the FUNDAMENTAL rows, from the stored metrics (WP-F).

The backtest database is built with ``run --metrics-only``: the facts, the metrics, the data-quality
gates and the rule inputs are stored, but no filing carries a FUNDAMENTAL ``score_snapshot`` row.
Re-running ``run`` would re-fetch every filing from the EDGAR gateway (36-80 s per filing), so this
command scores the filings from what is already stored and makes **no gateway call at all**.

**Nothing is recomputed.** A filing's stored metric rows (``fundamental_metrics``, one engine
version) are read back as :class:`~fundamental_agent.metrics.base.MetricResult` -- value, unit and the
stored ``inputs_json`` -- and only the *scoring* step runs:
:meth:`fundamental_agent.agents.FundamentalAnalyst.score` for the LLM path and
:meth:`~fundamental_agent.agents.FundamentalAnalyst.score_rule_based` for ``--rule-based``. Both write
their row through :func:`fundamental_agent.pipeline.store_score`, the function ``run`` writes with, so
the two paths cannot shape or label a row differently.

``--rule-based`` is the deterministic variant a run itself writes when the model cannot answer
(``rule-based-fallback-v1``): no model call, no LLM configuration, and the same row a run's fallback
writes for the same filing. The real LLM is never called by a test; tests monkeypatch the model.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from portfolio_common.db import Database, Row
from tqdm import tqdm

from fundamental_agent import db
from fundamental_agent.agents import (
    FALLBACK_MODEL_LABEL,
    AnalysisResult,
    FundamentalAnalyst,
    build_model,
    estimated_model_calls,
    estimated_prompt_chars,
    withheld_map,
)
from fundamental_agent.config import Settings
from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.pipeline import store_score
from fundamental_agent.pricing import ClosePrice, close_on_or_before
from kg_schema import connect, identity
from kg_schema.market_cap import MarketCap, market_caps_as_of
from kg_schema.provenance import DirtyTree, code_version, dirty_tree_reason
from kg_schema.queries import connect_ro, members_asof, metric_versions_present
from kg_schema.versions import MetricVersions, resolve_metric_versions

#: Characters per token for the prompt-size estimate (no tokenizer is run: this is a rough figure).
CHARS_PER_TOKEN = 4

#: Every database under this root is read-only (AGENTS.md), and `score` is a writer: it must be
#: pointed at a scratch copy. WP-A's driver refuses the same path for the same reason.
PRODUCTION_DATA_ROOT = Path("/workspaces/thesis/data")


def _refuse_production_path(path: Path) -> None:
    """Refuse to open a database under the read-only production root (a copy is one ``cp`` away)."""
    resolved = path.expanduser().resolve()
    if resolved == PRODUCTION_DATA_ROOT or PRODUCTION_DATA_ROOT in resolved.parents:
        raise ValueError(
            f"refusing to write {resolved}: every database under {PRODUCTION_DATA_ROOT} is "
            "read-only; point --db at a scratch copy"
        )


@dataclass(frozen=True)
class ScoreParams:
    """``python -m fundamental_agent score``'s knobs."""

    rule_based: bool = False  # the deterministic fallback; no model call
    workers: int = 1  # concurrent model calls; DB writes stay on the caller's thread
    for_dates: tuple[str, ...] = ()  # each member's latest filing available at each date
    accessions: tuple[str, ...] = ()  # named filings, reachable by the selectors or not
    limit: int | None = (
        None  # cap on the filings this run attempts (already-scored ones do not count)
    )
    dry_run: bool = False  # count the filings and the model calls; write nothing, call nothing
    # None resolves the newest stored version per metric group (``kg_schema.versions``, T-090); a
    # string pins one, and a version a group does not store is refused rather than mixed in.
    metrics_version: str | None = None


@dataclass
class ScoreReport:
    """What a ``score`` run did -- the tallies behind the final JSON line."""

    model: str
    run_id: int | None = None
    planned: int = 0  # filings this run attempted (scorable and not already scored)
    scored: int = 0  # FUNDAMENTAL rows written
    skipped: int = 0  # already had a FUNDAMENTAL row
    failed: int = 0  # could not be scored: no metrics, no period end, unknown accession, an error
    unscored: int = 0  # rows written whose applicable pillar has no value (D1's coverage rule)
    model_calls: int = 0
    prompt_chars: int = 0
    seconds: float = 0.0
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """The final JSON object: ``{scored, skipped, failed, model, seconds}``, the unscored tally,
        and -- for a dry run -- the counts it exists for."""
        out: dict[str, Any] = {
            "scored": self.scored,
            "skipped": self.skipped,
            "failed": self.failed,
            "model": self.model,
            "seconds": round(self.seconds, 1),
            "unscored": self.unscored,
        }
        if self.dry_run:
            out.update(
                {
                    "dry_run": True,
                    "filings": self.planned,
                    "model_calls": self.model_calls,
                    "estimated_prompt_tokens": self.prompt_chars // CHARS_PER_TOKEN,
                }
            )
        return out


@dataclass(frozen=True)
class _Filing:
    """One stored filing, as the score command reads it (no gateway, no facts)."""

    filing_id: int
    asset_id: int
    ticker: str
    company_name: str
    form: str
    fiscal_period: str
    period_end: str | None
    filing_date: str
    accession_number: str | None

    @property
    def label(self) -> str:
        return f"{self.ticker} {self.form} {self.fiscal_period}" + (
            f" ({self.accession_number})" if self.accession_number else ""
        )


@dataclass(frozen=True)
class _View:
    """The identity a score's prompt needs, from the stored rows (``agents.FilingView``)."""

    ticker: str
    company_name: str
    form: str
    fiscal_period: str
    price: ClosePrice | None = None
    market_cap: MarketCap | None = None


@dataclass(frozen=True)
class _Planned:
    """One filing ready to score: its identity, its stored metrics and its prompt's identity."""

    filing: _Filing
    event_time: str
    metrics: list[tuple[str, MetricResult]]
    view: _View


@dataclass(frozen=True)
class _Scorer:
    """The scoring path one run uses: the analyst and how many filings to run at once."""

    analyst: FundamentalAnalyst
    rule_based: bool
    workers: int

    def score(self, entry: _Planned) -> AnalysisResult:
        if self.rule_based:
            return self.analyst.score_rule_based(entry.view, entry.metrics)
        return self.analyst.score(entry.view, entry.metrics)


@dataclass(frozen=True)
class _Writer:
    """The one thread that writes: the connection, the run row and the report it counts into."""

    conn: Database
    run_id: int
    scoring_model: str
    report: ScoreReport

    def store(self, entry: _Planned, result: AnalysisResult) -> None:
        store_score(
            self.conn,
            asset_id=entry.filing.asset_id,
            form=entry.filing.form,
            fiscal_period=entry.filing.fiscal_period,
            event_time=entry.event_time,
            filing_id=entry.filing.filing_id,
            result=result,
            scoring_model=self.scoring_model,
            run_id=self.run_id,
        )
        self.report.scored += 1
        if result.assessment.score is None:
            self.report.unscored += 1
        db.bump_run_counter(self.conn, self.run_id, "completed_units")

    def fail(self, filing: _Filing | None, reason: str) -> None:
        """Count and record one filing this run could not score (never silent)."""
        self.report.failed += 1
        _note(self.report, f"fail  {filing.label if filing else '-'}: {reason}")
        db.record_error(
            self.conn,
            self.run_id,
            db.RunError(
                ticker=filing.ticker if filing else "",
                form=filing.form if filing else None,
                fiscal_period=filing.fiscal_period if filing else None,
                stage="score",
                message=reason,
            ),
        )
        db.bump_run_counter(self.conn, self.run_id, "failed_units")


_ALL_FILINGS_SQL = """
SELECT f.id AS filing_id, f.asset_id, f.form, f.fiscal_period, f.period_end, f.filing_date,
       f.accession_number, a.ticker, a.company_name, a.cik
FROM sec_filings f
JOIN assets a ON a.id = f.asset_id
WHERE f.filing_date IS NOT NULL
ORDER BY a.ticker, f.period_end, f.form, f.id
"""

_ACCESSION_SQL = """
SELECT f.id AS filing_id, f.asset_id, f.form, f.fiscal_period, f.period_end, f.filing_date,
       f.accession_number, a.ticker, a.company_name, a.cik
FROM sec_filings f
JOIN assets a ON a.id = f.asset_id
WHERE f.accession_number = ?
ORDER BY a.ticker, f.period_end, f.form, f.id
"""

#: The filing a member had on *date*: its newest period end among the filings already usable then
#: (``available_at``, T-107), the later-stored one first on a tie.
_LATEST_FILING_SQL = """
SELECT f.id AS filing_id, f.asset_id, f.form, f.fiscal_period, f.period_end, f.filing_date,
       f.accession_number, a.ticker, a.company_name, a.cik
FROM sec_filings f
JOIN assets a ON a.id = f.asset_id
WHERE f.asset_id = ? AND f.filing_date IS NOT NULL
  AND f.available_at IS NOT NULL AND f.available_at <= ?
ORDER BY f.period_end DESC, f.available_at DESC, f.id DESC
LIMIT 1
"""

#: The filing's stored metric rows, in the order they were computed: ``record_metrics`` inserts one
#: ``executemany`` in the results' own order, so ``id`` restores it (the pillars read the results in
#: that order). The filter is the one every reader applies (``kg_schema.versions.VERSION_FILTER_SQL``,
#: T-090: one engine version per group, never two at once); the ``rule_input`` pseudo-group is not in
#: ``METRIC_GROUPS``, so the filter leaves its rows out -- it is not a ratio the model reads.
_METRICS_SQL = """
SELECT m.metric_group, m.metric_name, m.value, m.unit, m.inputs_json
FROM fundamental_metrics m
WHERE m.filing_id = ?
  AND (m.metric_group || '/' || m.engine_version) IN (SELECT value FROM json_each(?))
ORDER BY m.id
"""

_METRICS_COUNT_SQL = """
SELECT m.filing_id, COUNT(*) AS n
FROM fundamental_metrics m
WHERE (m.metric_group || '/' || m.engine_version) IN (SELECT value FROM json_each(?))
GROUP BY m.filing_id
"""

_EXISTING_SCORE_SQL = """
SELECT model FROM score_snapshot
WHERE asset_id = ? AND score_type = 'FUNDAMENTAL' AND event_time = ?
"""


def _filing(row: Row) -> _Filing:
    return _Filing(
        filing_id=int(row["filing_id"]),
        asset_id=int(row["asset_id"]),
        ticker=str(row["ticker"]),
        company_name=str(row["company_name"] or row["ticker"]),
        form=str(row["form"]),
        fiscal_period=str(row["fiscal_period"]),
        period_end=str(row["period_end"]) if row["period_end"] else None,
        filing_date=str(row["filing_date"]),
        accession_number=str(row["accession_number"]) if row["accession_number"] else None,
    )


def _normalized_cik(value: object) -> str | None:
    try:
        return identity.normalize_cik(value)
    except ValueError:
        return None


def _assets(conn: Database) -> tuple[dict[str, int], dict[str, int]]:
    """``(ticker -> asset id, normalised CIK -> asset id)`` for the stored assets.

    The ticker wins: two share classes of one company share a CIK (GOOG/GOOGL) and the member's own
    ticker is the class. The CIK is the fallback, so a member listed under a symbol the database has
    since renamed (FB -> META) still finds its line.
    """
    by_ticker: dict[str, int] = {}
    by_cik: dict[str, int] = {}
    for row in conn.execute("SELECT id, ticker, cik FROM assets"):
        by_ticker[str(row["ticker"]).upper()] = int(row["id"])
        cik = _normalized_cik(row["cik"])
        if cik is not None:
            by_cik[cik] = int(row["id"])
    return by_ticker, by_cik


def parse_dates(lines: Sequence[str]) -> tuple[str, ...]:
    """The ``--for-dates`` entries, validated as ISO dates (a bad line is the caller's error)."""
    out: list[str] = []
    for line in lines:
        try:
            out.append(date.fromisoformat(line).isoformat())
        except ValueError:
            raise ValueError(f"--for-dates: not an ISO date: {line!r}") from None
    return tuple(out)


def read_lines(path: str | None) -> tuple[str, ...]:
    """A selector file's non-empty lines (one ISO date or one accession per line)."""
    if not path:
        return ()
    return tuple(
        line.strip()
        for line in Path(path).expanduser().read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def _select(
    conn: Database,
    settings: Settings,
    params: ScoreParams,
    unknown: list[str],
) -> list[_Filing]:
    """The filings the selectors name, deduplicated and in a stable order.

    Neither selector given: every stored filing (a metrics-only database's whole scope). With
    ``--accessions``, each named filing, whether it has metrics or not; an accession no filing
    carries is appended to *unknown* (never dropped). With ``--for-dates``, per date and per
    ``members_asof`` member, the filing that member's latest period had available on that date.
    The two selectors are unioned by filing id.
    """
    if not params.accessions and not params.for_dates:
        return [_filing(row) for row in conn.execute(_ALL_FILINGS_SQL).fetchall()]
    chosen: dict[int, _Filing] = {}
    for accession in params.accessions:
        rows = conn.execute(_ACCESSION_SQL, (accession,)).fetchall()
        if not rows:
            unknown.append(accession)
            continue
        for row in rows:
            filing = _filing(row)
            chosen[filing.filing_id] = filing
    for filing in _select_for_dates(conn, settings, params.for_dates).values():
        chosen[filing.filing_id] = filing
    return sorted(
        chosen.values(), key=lambda f: (f.ticker, f.period_end or "", f.form, f.filing_id)
    )


def _select_for_dates(
    conn: Database, settings: Settings, days: Sequence[str]
) -> dict[int, _Filing]:
    """Each date's members and the filing each one had available then, keyed by filing id."""
    if not days:
        return {}
    by_ticker, by_cik = _assets(conn)
    chosen: dict[int, _Filing] = {}
    uconn = connect_ro(settings.universe_db_path)
    try:
        for day in days:
            members = members_asof(uconn, day)
            found = 0
            for member in members:
                asset_id = by_ticker.get(member.symbol.upper()) or by_cik.get(
                    _normalized_cik(member.cik) or ""
                )
                if asset_id is None:
                    continue
                rows = conn.execute(_LATEST_FILING_SQL, (asset_id, day)).fetchall()
                if not rows:
                    continue
                filing = _filing(rows[0])
                chosen[filing.filing_id] = filing
                found += 1
            tqdm.write(f"for-dates: {day}: {len(members)} members, {found} with a filing available")
    finally:
        uconn.close()
    return chosen


def _load_metrics(
    conn: Database, filing_id: int, resolution: MetricVersions
) -> list[tuple[str, MetricResult]]:
    """The filing's stored metrics as the results the scoring step reads -- nothing recomputed."""
    out: list[tuple[str, MetricResult]] = []
    for row in conn.execute(_METRICS_SQL, (filing_id, resolution.json_param())).fetchall():
        try:
            inputs: Any = json.loads(row["inputs_json"] or "{}")
        except ValueError:
            inputs = {}
        out.append(
            (
                str(row["metric_group"]),
                MetricResult(
                    name=str(row["metric_name"]),
                    value=None if row["value"] is None else float(row["value"]),
                    unit=str(row["unit"] or ""),
                    inputs=inputs if isinstance(inputs, dict) else {},
                ),
            )
        )
    return out


def _view(conn: Database, filing: _Filing) -> _View:
    """The filing's identity plus the period-end close and cap it was valued on.

    The close is the last ``price_daily`` bar at or before the filing's period end, and the cap is
    ``market_caps_as_of`` on that date -- both causal reads, the same ones ``run`` made when it
    computed the filing (``pipeline._analyze_one``). They decide only whether the valuation group is
    withheld from the model (T-153 part 3d, A3); every metric value comes from the stored rows.
    """
    assert filing.period_end is not None  # a planned filing always has one (see `_plan`)
    return _View(
        ticker=filing.ticker,
        company_name=filing.company_name,
        form=filing.form,
        fiscal_period=filing.fiscal_period,
        price=close_on_or_before(conn, filing.asset_id, filing.period_end),
        market_cap=market_caps_as_of(conn, [filing.asset_id], as_of=filing.period_end).caps.get(
            filing.asset_id
        ),
    )


def _existing_model(conn: Database, filing: _Filing) -> str | None:
    """The model label of the FUNDAMENTAL row this filing already has, or ``None``."""
    if filing.period_end is None:
        return None
    row = conn.execute(_EXISTING_SCORE_SQL, (filing.asset_id, filing.period_end)).fetchone()
    return None if row is None else (None if row["model"] is None else str(row["model"]))


def _counts(conn: Database, resolution: MetricVersions) -> dict[int, int]:
    """``{filing_id: stored metric rows}`` under the resolved versions."""
    return {
        int(row["filing_id"]): int(row["n"])
        for row in conn.execute(_METRICS_COUNT_SQL, (resolution.json_param(),)).fetchall()
    }


def _resolved_text(resolution: MetricVersions) -> str:
    return ", ".join(
        f"{group}={version}" for group, version in sorted(resolution.manifest().items())
    )


def _stored_text(conn: Database) -> str:
    """What the database holds, for the message of a filing the resolution does not reach."""
    present = metric_versions_present(conn)
    return ", ".join(f"{g}: {'/'.join(sorted(v))}" for g, v in sorted(present.items())) or "none"


def _note(report: ScoreReport, message: str) -> None:
    report.errors.append(message)
    tqdm.write(message)


def _plan(  # noqa: PLR0913, PLR0917 - the selection, its versions and the report it counts into
    conn: Database,
    filings: Sequence[_Filing],
    params: ScoreParams,
    resolution: MetricVersions,
    unknown: Sequence[str],
    report: ScoreReport,
) -> tuple[list[_Planned], list[tuple[_Filing | None, str]]]:
    """Split the selected filings into the ones this run scores and the ones it cannot.

    Already scored: skipped (``score_snapshot`` is append-only per asset and event time, so a row
    under another model label is not overwritten either -- it is reported, never replaced). A
    filing with no stored metrics under the resolved engine versions, or no period end, is rejected
    with the reason. ``--limit`` caps the planned filings *after* the skips, so a resumed run makes
    progress.
    """
    counts = _counts(conn, resolution)
    stored = _stored_text(conn)
    planned: list[_Planned] = []
    rejected: list[tuple[_Filing | None, str]] = [
        (None, f"no filing carries accession {accession}") for accession in unknown
    ]
    for filing in filings:
        already = _existing_model(conn, filing)
        if already is not None:
            report.skipped += 1
            _note(
                report,
                f"skip  {filing.label}: already has a FUNDAMENTAL row (model {already}); "
                "score_snapshot is append-only per (asset, score_type, event_time)",
            )
            continue
        if filing.period_end is None:
            rejected.append((filing, "the filing has no stored period_end"))
            continue
        if counts.get(filing.filing_id, 0) == 0:
            rejected.append(
                (
                    filing,
                    f"no stored metrics under the resolved versions ({_resolved_text(resolution)}); "
                    f"the database holds {stored}",
                )
            )
            continue
        planned.append(
            _Planned(
                filing=filing,
                event_time=filing.period_end,
                metrics=_load_metrics(conn, filing.filing_id, resolution),
                view=_view(conn, filing),
            )
        )
    if params.limit is not None:
        planned = planned[: params.limit]
    return planned, rejected


def _dry_run(
    planned: Sequence[_Planned],
    rejected: Sequence[tuple[_Filing | None, str]],
    params: ScoreParams,
    report: ScoreReport,
) -> None:
    """Print one line per filing the run would score, with the prompt it would send."""
    for entry in planned:
        calls = 0 if params.rule_based else estimated_model_calls(entry.metrics)
        chars = estimated_prompt_chars(
            entry.view, entry.metrics, withheld_map(entry.view, entry.metrics)
        )
        report.model_calls += calls
        report.prompt_chars += chars
        print(
            f"{entry.filing.label:<64} prompt~{chars // CHARS_PER_TOKEN:>7} tok "
            f"({chars:>9} ch)  calls={calls}"
        )
    for filing, reason in rejected:
        _note(report, f"fail  {filing.label if filing else '-'}: {reason}")
    tqdm.write(
        f"dry run: {len(planned)} filing(s), {report.model_calls} model call(s), "
        f"~{report.prompt_chars // CHARS_PER_TOKEN} prompt tokens "
        f"({report.prompt_chars} chars); nothing written"
    )


def _score_all(writer: _Writer, scorer: _Scorer, planned: Sequence[_Planned]) -> None:
    """Score every planned filing; only the caller's thread writes (``_Writer``).

    A single worker, or the rule-based path (no model call at all), runs inline. Otherwise the
    model calls run on a thread pool and each result is stored here as it arrives -- one writer, so
    a worker never touches the connection.
    """
    if not planned:
        return
    if scorer.rule_based or scorer.workers <= 1:
        for entry in tqdm(planned, desc="fundamental score", unit="filing"):
            writer.store(entry, scorer.score(entry))
        return
    with ThreadPoolExecutor(max_workers=min(scorer.workers, len(planned))) as pool:
        futures = {pool.submit(scorer.score, entry): entry for entry in planned}
        for future in tqdm(as_completed(futures), total=len(futures), desc="fundamental score"):
            entry = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # one bad filing must not stop the batch
                writer.fail(entry.filing, f"score failed: {exc}")
                continue
            writer.store(entry, result)


def _params(params: ScoreParams, resolution: MetricVersions) -> dict[str, Any]:
    return {
        "command": "score",
        "rule_based": params.rule_based,
        "workers": params.workers,
        "for_dates": list(params.for_dates),
        "accessions": list(params.accessions),
        "limit": params.limit,
        "metrics_version": params.metrics_version,
        "metrics_versions": resolution.manifest(),
    }


def _start_run(  # noqa: PLR0913, PLR0917 - the run row's whole provenance
    conn: Database,
    settings: Settings,
    params: ScoreParams,
    resolution: MetricVersions,
    filings: int,
    planned: int,
) -> int:
    """Refuse a dirty tree (T-114), then open the run row. Returns its id."""
    cv = code_version()
    dirty = dirty_tree_reason(cv)
    if dirty is not None and not settings.allow_dirty:
        raise DirtyTree(
            f"{dirty}; pass --allow-dirty for a deliberate run from an uncommitted tree"
        )
    run_id = db.start_run(
        conn,
        params={**_params(params, resolution), "dirty_tree_bypassed": dirty},
        as_of=None,
        code_version=cv,
    )
    db.update_run_plan(conn, run_id, universe_size=filings, planned_units=planned)
    return run_id


def run_score(settings: Settings, params: ScoreParams) -> ScoreReport:
    """Score the stored filings and return the run's tallies.

    *params.rule_based* selects the deterministic path (no model is built); otherwise the configured
    LLM scores every planned filing, ``params.workers`` at a time. The database is written from this
    thread only: a worker returns an :class:`AnalysisResult` and the caller stores it. The metric
    engine version is resolved once (:func:`kg_schema.versions.resolve_metric_versions`): a
    ``--metrics-version`` the database does not hold for a group raises rather than mixing versions.
    """
    started = time.monotonic()
    model = FALLBACK_MODEL_LABEL if params.rule_based else (settings.llm_model or "llm")
    report = ScoreReport(model=model, dry_run=params.dry_run)
    if params.workers < 1:
        raise ValueError("--workers must be at least 1")
    if params.limit is not None and params.limit < 0:
        raise ValueError("--limit must not be negative")
    _refuse_production_path(settings.db_path)

    conn = connect(settings.db_path)
    try:
        if not params.dry_run:
            db.ensure_schema(conn)
        resolution = resolve_metric_versions(conn, params.metrics_version)
        unknown: list[str] = []
        filings = _select(conn, settings, params, unknown)
        planned, rejected = _plan(conn, filings, params, resolution, unknown, report)
        report.planned = len(planned)
        if params.dry_run:
            _dry_run(planned, rejected, params, report)
            report.failed = len(rejected)
            report.seconds = time.monotonic() - started
            return report
        run_id = _start_run(conn, settings, params, resolution, len(filings), len(planned))
        report.run_id = run_id
        analyst = (
            FundamentalAnalyst(None, FALLBACK_MODEL_LABEL)
            if params.rule_based
            else FundamentalAnalyst(build_model(settings), settings.llm_model)
        )
        writer = _Writer(conn=conn, run_id=run_id, scoring_model=analyst.model_name, report=report)
        scorer = _Scorer(analyst=analyst, rule_based=params.rule_based, workers=params.workers)
        try:
            for filing, reason in rejected:
                writer.fail(filing, reason)
            _score_all(writer, scorer, planned)
        except BaseException:  # an interrupt too: never leave the run log 'running'
            db.finish_run(conn, run_id, status="failed")
            raise
        db.finish_run(conn, run_id, status="completed")
        report.seconds = time.monotonic() - started
        return report
    finally:
        conn.close()
