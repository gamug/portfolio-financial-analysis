"""SelectionCycle / MonitoringCycle -- a checkpointed topological runner.

Strands' ``multiagent.GraphBuilder`` could drive the same step graph; the plain
runner here keeps ``cycle_checkpoint`` as the single source of truth for resume,
so the framework stays swappable. Deterministic steps run inline; the FUNDAMENTAL
step is delegated to an optional hook (the Strands ``FundamentalAnalyst``).
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from portfolio_common.db import Database

from cycle import data, writers
from cycle.config import CycleSettings
from cycle.construction import (
    BookCandidate,
    BookResult,
    VetoStatus,
    build_book,
    validate_settings,
)
from cycle.db import ensure_schema
from cycle.replay import out_of_order_replay_reason, sync_replay_positions
from cycle.rules import (
    RuleContext,
    disabled_rule_ids,
    enabled_rules,
    hold_trading_days,
    seed_catalog,
    soft_penalties,
)
from cycle.scores import sector, technical, valorization
from cycle.scores.normalize import normalized_scores
from cycle.state import (
    check_construction,
    check_manifest,
    check_score_weights,
    checkpoint,
    done_steps,
    finish_cycle,
    merge_params,
    open_cycle,
)
from cycle.writers import OutOfOrderCycle, out_of_order_reason
from kg_schema import availability, connect
from kg_schema.provenance import DirtyTree, code_version, dirty_tree_reason
from kg_schema.queries import (
    StaleAsOf,
    StaleGateVersion,
    connect_ro,
    stale_as_of_reason,
    stale_gate_version_reason,
    veto_out_of_order_reason,
)
from kg_schema.versions import (
    DATA_QUALITY_GATE_VERSION,
    manifest_tag,
    parse_metric_selection,
    resolve_metric_versions,
)

FundamentalHook = Callable[[Database, list[int], str], None]

log = logging.getLogger(__name__)

_SELECTION_STEPS = (
    "universe",
    "fundamental",
    "technical",
    "valorization",
    "semantic_read",
    "normalize",
    "sector",
    "veto",
    "rank",
    "positions",
)
_MONITORING_STEPS = tuple(s for s in _SELECTION_STEPS if s != "positions")


@dataclass
class CycleReport:
    cycle_run_id: int
    cycle_type: str
    cycle_date: str
    steps_run: list[str] = field(default_factory=list)
    steps_skipped: list[str] = field(default_factory=list)
    selected: int = 0
    vetoed: int = 0
    manifest_tag: str = ""
    # Why the out-of-order-cycle guard (T-097) would have refused, when --allow-backdated
    # overrode it. Always None for a MONITORING run -- it never reaches the positions step.
    backdated_guard_bypassed: str | None = None
    # Why the price-spine guard (T-110) would have refused, when --allow-stale-prices
    # overrode it. Set for either cycle type -- both read prices for TECHNICAL/veto.
    stale_price_bypassed: str | None = None
    # Why the clean-tree guard (T-114) would have refused, when --allow-dirty overrode it.
    dirty_tree_bypassed: str | None = None
    # Why the Ring-1 gate-version guard (T-116) would have refused, when --allow-stale-dq-gate
    # overrode it.
    stale_dq_gate_bypassed: str | None = None
    # Why the veto out-of-order guard (T-125) would have refused, when --allow-backdated-veto
    # overrode it. Set for either cycle type and for REPLAY -- `veto` is one shared table.
    veto_backdated_bypassed: str | None = None
    # Universe members with no FUNDAMENTAL score at all this cycle (T-119) -- ineligible for
    # selection, marked in cycle_ranking, never a count of HARD/SOFT vetoes (see `vetoed`).
    unscored: int = 0
    # Their tickers, so the CLI can name them without a separate cycle_ranking query
    # (PR #99 review).
    unscored_tickers: list[str] = field(default_factory=list)
    # The book `positions` built (T-136): its caps, relaxations, shortfall and pin notes, and each
    # held name's ``(ticker, sector, weight)`` in rank order. None on a MONITORING run, or when a
    # resume skipped the step.
    book: BookResult | None = None
    book_rows: list[tuple[str, str | None, float]] = field(default_factory=list)


class PreferencesNeedDryRun(RuntimeError):
    """A writing ``select`` was given book preferences (T-136, user decision 2026-10-05)."""


def _book_candidates(
    rows: list[dict],
    tickers: Mapping[int, str],
    sectors: Mapping[int, str | None],
    price_obs: Mapping[int, Mapping[str, float | None]],
) -> list[BookCandidate]:
    """The ranked cohort as ``build_book`` input. HARD-vetoed names (at the T-1 cutoff) are passed
    in, marked, so a HARD pin is refused with its reason; UNSCORED names are ineligible and left
    out (a pin on one is refused as "not among the candidates")."""
    cands: list[BookCandidate] = []
    for r in rows:
        aid = int(r["asset_id"])
        rules = r["veto_rules"]
        veto: VetoStatus = "HARD" if "HARD" in rules else "none"
        if veto == "none" and "UNSCORED" in rules:
            continue
        if veto == "none" and any(rule not in ("HARD", "UNSCORED") for rule in rules):
            veto = "SOFT"
        cands.append(
            BookCandidate(
                asset_id=aid,
                ticker=tickers[aid],
                blended_score=float(r["blended_score"]),
                sector=sectors.get(aid),
                realized_vol_90d=(price_obs.get(aid) or {}).get("realized_vol_90d"),
                veto=veto,
            )
        )
    return cands


def _call_build_book(settings: CycleSettings, cands: list[BookCandidate]) -> BookResult:
    return build_book(
        cands,
        n=settings.top_n,
        scheme=settings.weight_scheme,
        max_name_weight=settings.max_name_weight,
        max_sector_weight=settings.max_sector_weight,
        pins=settings.pins,
        exclude=settings.exclude,
        exclude_sectors=settings.exclude_sectors,
        only_sectors=settings.only_sectors,
    )


def _book_record(settings: CycleSettings, book: BookResult) -> dict[str, object]:
    """What ``cycle_run.params_json`` keeps about the book: the **effective** caps under the keys
    ``v_weight_scheme`` already reads (so it reports what was applied), what was requested under
    ``construction`` (what a resume must not change), and every decision ``build_book`` made."""
    return {
        "top_n": settings.top_n,
        "weight_scheme": book.scheme,
        "max_name_weight": book.max_name_weight,
        "max_sector_weight": book.max_sector_weight,
        "construction": settings.construction(),
        "n_held": book.n_held,
        "shortfall": book.shortfall,
        "relaxations": [asdict(r) for r in book.relaxations],
        "pins": list(settings.pins),
        "exclude": list(settings.exclude),
        "exclude_sectors": list(settings.exclude_sectors),
        "only_sectors": None if settings.only_sectors is None else list(settings.only_sectors),
        "refused_pins": [asdict(p) for p in book.refused_pins],
        "flagged_pins": [asdict(p) for p in book.flagged_pins],
        "overflow_tickers": list(book.overflow_tickers),
    }


def _log_book(cycle_type: str, cycle_date: str, book: BookResult) -> None:
    for r in book.relaxations:
        log.warning(
            "%s %s: %s cap relaxed %.6g -> %.6g (%s)",
            cycle_type,
            cycle_date,
            r.cap,
            r.requested,
            r.effective,
            r.reason,
        )
    for p in book.refused_pins:
        log.warning("%s %s: pin %s refused: %s", cycle_type, cycle_date, p.ticker, p.reason)
    for p in book.flagged_pins:
        log.warning("%s %s: pin %s flagged: %s", cycle_type, cycle_date, p.ticker, p.reason)
    if book.shortfall:
        log.warning(
            "%s %s: only %d of %d names are eligible; the book holds %d, never padded",
            cycle_type,
            cycle_date,
            book.n_held,
            book.requested_n,
            book.n_held,
        )


def _t_minus_1(cycle_date: str) -> str:
    return (date.fromisoformat(cycle_date) - timedelta(days=1)).isoformat()


def _blended(
    per_type: dict[str, dict[int, float | None]], weights: dict[str, float], asset_id: int
) -> tuple[float, dict[str, float | None]]:
    parts: dict[str, float | None] = {}
    num = den = 0.0
    for stype, w in weights.items():
        v = per_type.get(stype, {}).get(asset_id)
        parts[stype] = v
        if v is not None:
            num += w * v
            den += w
    return (num / den if den else 0.0), parts


def _run(  # noqa: C901, PLR0913, PLR0915 - one linear, checkpointed step sequence
    settings: CycleSettings,
    cycle_type: str,
    cycle_date: str,
    steps: tuple[str, ...],
    *,
    conn: Database,
    fundamental_hook: FundamentalHook | None,
) -> CycleReport:
    if cycle_type == "SELECTION" and settings.has_preferences:
        # The live book is the thesis book: preferences make it decision support (T-134 decision 6),
        # so a writing select refuses them -- `--dry-run` previews them, `backfill` replays with them.
        raise PreferencesNeedDryRun(
            "--pin / --exclude / --exclude-sectors / --only-sectors build a decision-support book, "
            "never the live thesis book: use `select --dry-run` to preview it, or `backfill` "
            "(which writes only portfolio_position_replay)"
        )
    ensure_schema(conn)
    # Every fundamental read below keys on `available_at` (T-107): refuse to run on rows that
    # predate its backfill rather than read none of them.
    availability.require(conn)
    # Resolve the metric versions this run reads (T-090) and refuse to resume an earlier run of
    # the same (type, date) that was built on different ones -- before touching that run.
    versions = resolve_metric_versions(conn, parse_metric_selection(settings.metrics_version))
    manifest = {
        "consumer": "cycle",
        "metrics": versions.manifest(),
        "quality": DATA_QUALITY_GATE_VERSION,
    }
    tag = manifest_tag(manifest)
    check_manifest(conn, cycle_type, cycle_date, tag)
    # T-141: params_json.score_weights is written once, by the first attempt; every ranking of
    # this run must use those weights, so refuse a resume under others (any cycle type).
    check_score_weights(conn, cycle_type, cycle_date, settings.score_weights)
    if "positions" in steps:
        # T-136: never resume onto a different book than the one the first attempt built.
        check_construction(conn, cycle_type, cycle_date, settings.construction())
    # T-110: refuse a cycle_date past the price spine before any cycle_run row exists, the
    # same way check_manifest refuses just above -- TECHNICAL/veto both read prices, so a
    # stale as_of would silently score against data that is, at best, weeks old.
    stale_reason = stale_as_of_reason(conn, cycle_date)
    if stale_reason is not None and not settings.allow_stale_prices:
        raise StaleAsOf(
            f"{stale_reason}; pass --allow-stale-prices for a deliberate run ahead of the "
            "price spine"
        )
    # T-114: refuse when this run's own code_version() is dirty (uncommitted changes) --
    # its results would come from code HEAD alone can't reproduce.
    cv = code_version()
    dirty_reason = dirty_tree_reason(cv)
    if dirty_reason is not None and not settings.allow_dirty:
        raise DirtyTree(
            f"{dirty_reason}; pass --allow-dirty for a deliberate run from an uncommitted tree"
        )
    # T-116 (PR #95 review): refuse before reading a moment away from Ring-1's quarantines --
    # data_quality_issue holding only an older gate version than DATA_QUALITY_GATE_VERSION
    # means a re-gate hasn't run since a gate-methodology bump, and every quarantine/HARD
    # DQ_*/DATA_QUALITY veto would silently vanish below, not because filings got cleaner.
    gate_reason = stale_gate_version_reason(conn, DATA_QUALITY_GATE_VERSION)
    if gate_reason is not None and not settings.allow_stale_dq_gate:
        raise StaleGateVersion(
            f"{gate_reason}; pass --allow-stale-dq-gate for a deliberate run before re-gating"
        )
    # T-125's own out-of-order guard is checked inside `_veto()` below, not here -- like the
    # positions guard (T-097), it must fire only when the veto step is actually about to write,
    # not on every resumed run that has already completed it (PR #103 review: checking it this
    # early refused a `cycle backfill --from F --to T` resume at F even when every step at F,
    # veto included, was already `done` and no write would happen).
    run_id = open_cycle(
        conn,
        cycle_type,
        cycle_date,
        {
            # Preferences are recorded by `positions` once a book is built, with the rest of it.
            **settings.model_dump(exclude={"pins", "exclude", "exclude_sectors", "only_sectors"}),
            "manifest": manifest,
            "manifest_tag": tag,
            "technical_version": technical.VERSION,
            "stale_as_of_bypassed": stale_reason,
            "dirty_tree_bypassed": dirty_reason,
            "stale_dq_gate_bypassed": gate_reason,
        },
        code_version=cv,
    )
    already = done_steps(conn, run_id)
    report = CycleReport(run_id, cycle_type, cycle_date, manifest_tag=tag)
    report.stale_price_bypassed = stale_reason
    report.dirty_tree_bypassed = dirty_reason
    report.stale_dq_gate_bypassed = gate_reason

    universe_rows = data.active_universe(
        conn, settings.universe, cycle_date, settings.universe_db_path
    )
    asset_ids = [int(r["id"]) for r in universe_rows]
    sector_of = {int(r["id"]): r["sector_id"] for r in universe_rows}
    ticker_of = {int(r["id"]): str(r["ticker"]) for r in universe_rows}
    sector_name_of = {int(r["id"]): r["sector"] for r in universe_rows}
    # Ring-1 data-quality gates (T-065): quarantined metrics read as NULL everywhere below.
    dq = data.data_quality(conn, cycle_date, versions)
    metrics = dq.apply(data.latest_metrics(conn, cycle_date, versions))
    price_obs = data.latest_price_observation(conn, cycle_date, settings.observation_engine_version)

    def _do(step: str, fn: Callable[[], dict]) -> None:
        if step in already:
            report.steps_skipped.append(step)
            return
        checkpoint(conn, run_id, step, "running")
        try:
            detail = fn() or {}
        except Exception as exc:
            # Leave a "failed" checkpoint rather than a stuck "running" one (T-097 review) --
            # e.g. the out-of-order-cycle guard refusing mid-step. Re-raised untouched; the
            # caller (_run) marks the parent cycle_run failed too.
            checkpoint(conn, run_id, step, "failed", {"error": str(exc)})
            raise
        checkpoint(conn, run_id, step, "done", detail)
        report.steps_run.append(step)

    # The whole step sequence below is one try block so that ANY exception -- the out-of-order
    # guard's refusal included -- leaves cycle_run "failed" rather than stuck "running" forever
    # (T-097 review: cycle_checkpoint's own DDL comment already documents 'failed' as a status,
    # but nothing wrote it). _do() marks the individual step's checkpoint "failed" first; this
    # marks the parent run, then re-raises unchanged for the caller (CLI / tests) to handle.
    try:
        # -- universe
        _do("universe", lambda: {"assets": len(asset_ids)})

        # -- fundamental (optional external hook)
        def _fundamental() -> dict:
            if fundamental_hook is None:
                return {
                    "skipped": "no hook",
                    "existing": len(data.latest_fundamental_score(conn, cycle_date)),
                }
            missing = [
                a for a in asset_ids if a not in data.last_fundamental_dates(conn, cycle_date)
            ]
            fundamental_hook(conn, missing, cycle_date)
            return {"scored": len(missing)}

        _do("fundamental", _fundamental)

        # -- technical
        def _technical() -> dict:
            obs_in = {a: price_obs[a] for a in asset_ids if a in price_obs}
            scores = technical.compute(obs_in, sector_of)
            writers.write_scores(
                conn,
                "TECHNICAL",
                cycle_date,
                {s.asset_id: s.raw_value for s in scores},
                {},
                {s.asset_id: s.components for s in scores},
                run_id=run_id,
                model=technical.VERSION,
            )
            return {"scored": len(scores)}

        _do("technical", _technical)

        # -- valorization
        def _valorization() -> dict:
            mcap = data.market_cap_estimates(conn, cycle_date, list(asset_ids))
            rows = {}
            no_cap = 0
            for a in asset_ids:
                m = dict(metrics.get(a, {}))
                quarantined = dq.quarantined.get(a, set())
                mc = None if "valuation.market_capitalization" in quarantined else mcap.get(a)
                if a in dq.negative_equity:
                    # D/E is quarantined, but negative book equity is still the worst leverage
                    # in the cohort, never a missing factor (C2, docs/model_fixes.md).
                    m["leverage.debt_to_equity"] = float("inf")
                ni = m.get("profitability.net_income") or m.get("income_statement.net_income")
                if ni is not None and mc:
                    m["earnings_yield"] = ni / mc
                if mc:
                    m["neg_log_market_cap"] = -math.log(mc)
                else:
                    no_cap += 1  # T-132: a missing size factor, reported rather than guessed
                rows[a] = m
            scores = valorization.compute(rows)
            writers.write_scores(
                conn,
                "VALORIZATION",
                cycle_date,
                {s.asset_id: s.raw_value for s in scores},
                {},
                {s.asset_id: s.components for s in scores},
                run_id=run_id,
            )
            return {"scored": len(scores), "market_cap_missing": no_cap}

        _do("valorization", _valorization)

        _do(
            "semantic_read",
            lambda: {
                "note": "SEMANTIC ScoreSnapshot aggregation runs in the integration repo",
                "existing": len(data.latest_semantic_score(conn, cycle_date)),
            },
        )

        # -- normalize each score_type across the cohort
        def _normalize() -> dict:
            done: dict[str, int] = {}
            for stype in ("TECHNICAL", "VALORIZATION", "SEMANTIC"):
                rows = conn.execute(
                    "SELECT asset_id, raw_value FROM score_snapshot "
                    "WHERE score_type = ? AND event_time = ? AND raw_value IS NOT NULL",
                    (stype, cycle_date),
                ).fetchall()
                if not rows:
                    continue
                aids = [int(r["asset_id"]) for r in rows]
                norm = normalized_scores([float(r["raw_value"]) for r in rows])
                writers.apply_normalized(
                    conn, stype, cycle_date, {aids[i]: norm[i] for i in range(len(aids))}
                )
                done[stype] = len(aids)
            # FUNDAMENTAL is normalized against each asset's latest *public* filing snapshot
            # (T-106), and the value lands on that snapshot row alone -- by id, not on every
            # row of the asset that happens to share its raw score.
            frows = [
                r
                for r in data.latest_fundamental_rows(conn, cycle_date)
                if r["raw_value"] is not None
            ]
            if frows:
                fn = normalized_scores([float(r["raw_value"]) for r in frows])
                conn.executemany(
                    "UPDATE score_snapshot SET normalized_score = ? WHERE id = ?",
                    [(fn[i], int(frows[i]["id"])) for i in range(len(frows))],
                )
                conn.commit()
                done["FUNDAMENTAL"] = len(frows)
            return done

        _do("normalize", _normalize)

        # -- sector roll-up (needs the normalized TECHNICAL scores from `normalize`)
        def _sector() -> dict:
            rows = conn.execute(
                "SELECT asset_id, raw_value, normalized_score FROM score_snapshot "
                "WHERE score_type = 'TECHNICAL' AND event_time = ? AND raw_value IS NOT NULL",
                (cycle_date,),
            ).fetchall()
            if not rows:
                return {"aggregates": 0, "momentum": 0}
            traw = {int(r["asset_id"]): float(r["raw_value"]) for r in rows}
            tnorm = {
                int(r["asset_id"]): float(r["normalized_score"])
                for r in rows
                if r["normalized_score"] is not None
            }
            aggregates, momentum = sector.roll_up(sector_of, traw, tnorm)
            writers.write_sector_aggregates(conn, cycle_date, aggregates, run_id=run_id)
            if momentum:
                aids = list(momentum)
                norm = normalized_scores([momentum[a] for a in aids])
                writers.write_scores(
                    conn,
                    sector.SCORE_TYPE,
                    cycle_date,
                    momentum,
                    {aids[i]: norm[i] for i in range(len(aids))},
                    {a: {"sector_id": sector_of[a]} for a in aids},
                    run_id=run_id,
                )
            return {"aggregates": len(aggregates), "momentum": len(momentum)}

        _do("sector", _sector)

        # -- veto
        def _veto() -> dict:
            # T-125: refuse a cycle_date older than the latest veto stint transition already
            # recorded -- `veto` is one shared table across live select/monitor and REPLAY
            # backfill runs alike (T-097's own out-of-order rule, applied to veto instead of
            # portfolio_position). Checked here, not at the top of `_run` (PR #103 review):
            # the guard must apply only when this step is actually about to write, the same
            # way the positions guard below only fires inside `_positions()`.
            veto_backdated_reason = veto_out_of_order_reason(conn, cycle_date)
            if veto_backdated_reason is not None and not settings.allow_backdated_veto:
                raise OutOfOrderCycle(  # noqa: TRY301
                    f"{veto_backdated_reason}; pass --allow-backdated-veto for a deliberate "
                    "historical re-run, or --force (cycle backfill) to reset the replay range "
                    "first"
                )
            report.veto_backdated_bypassed = veto_backdated_reason
            seed_catalog(conn, settings.soft_veto_penalty)
            ctx = RuleContext(
                cycle_date=cycle_date,
                metrics={a: metrics.get(a, {}) for a in asset_ids},
                price_obs={a: price_obs[a] for a in asset_ids if a in price_obs},
                last_fundamental=data.last_fundamental_dates(conn, cycle_date),
                data_quality={a: dq.hard[a] for a in asset_ids if a in dq.hard},
                sectors={a: sector_name_of.get(a) for a in asset_ids},
            )
            # RuleResult.evaluated (T-125 b): each rule's own could-resolve set, so a missing
            # cycle keeps a stint open instead of the writer misreading "no data" as "cleared".
            results = [(rule.RULE_ID, rule.evaluate(ctx)) for rule in enabled_rules(conn)]
            hits = [h for _, res in results for h in res]
            report.vetoed = len({h.asset_id for h in hits if h.severity == "HARD"})
            if veto_backdated_reason is not None:
                # A backdated override must never write: `write_vetoes` only ever undoes and
                # redoes the *latest* transition date's own rows (T-125 f) -- applied here, at
                # an older date, it would delete or reopen stints that later, still-current
                # transitions depend on, rewriting history rather than replaying it (PR #103
                # review). `_rank` already reads the existing stints point-in-time through
                # `hard_vetoed_as_of`/`active_soft_vetoes`, so a read-only veto step still
                # ranks correctly.
                return {"opened": 0, "cleared": 0, "backdated_readonly": True}
            evaluated = {rule_id: res.evaluated for rule_id, res in results}
            hold_days = {
                rule.RULE_ID: days
                for rule in enabled_rules(conn)
                if (days := hold_trading_days(rule)) is not None
            }
            opened, cleared = writers.write_vetoes(
                conn,
                cycle_date,
                hits,
                evaluated,
                disabled_rule_ids(conn),
                run_id=run_id,
                hold_days=hold_days,
            )
            # Open HARD stints, not this cycle's hits: a temporal stint is held after its
            # condition is gone (T-070) and the next ranking still excludes it.
            report.vetoed = len(writers.hard_vetoed_as_of(conn, cycle_date))
            return {"opened": opened, "cleared": cleared}

        _do("veto", _veto)

        # -- rank (T-1 veto filter + soft-veto penalty)
        ranked_cache: dict[str, list[dict]] = {}

        def _rank() -> dict:
            cutoff = _t_minus_1(cycle_date)
            hard = writers.hard_vetoed_as_of(conn, cutoff)
            soft = writers.active_soft_vetoes(conn, cutoff)
            per_type = {
                "FUNDAMENTAL": {
                    int(r["asset_id"]): r["normalized_score"]
                    for r in data.latest_fundamental_rows(conn, cycle_date)
                },
                "TECHNICAL": _norm_map(conn, "TECHNICAL", cycle_date),
                "VALORIZATION": _norm_map(conn, "VALORIZATION", cycle_date),
                "SEMANTIC": _norm_map(conn, "SEMANTIC", cycle_date),
            }
            # T-119: a universe member with no FUNDAMENTAL score at all -- distinct from
            # EARNINGS_MISSING's stale-but-present case -- is ineligible immediately (this
            # cycle's own ranking, not the T-1 veto lag); more than `unscored_max_share` of the
            # universe missing entirely refuses the run outright rather than build a portfolio
            # blind on most of it.
            unscored = data.unscored_assets(asset_ids, per_type["FUNDAMENTAL"])
            unscored_reason = data.too_many_unscored_reason(
                unscored, len(asset_ids), settings.unscored_max_share
            )
            if unscored_reason is not None:
                raise data.TooManyUnscored(unscored_reason)  # noqa: TRY301
            # each SOFT rule's own points (BREAK_TREND_200 is flag-only: 0), else the run's default
            default = settings.soft_veto_penalty
            points = soft_penalties(default)
            scored = []
            for a in asset_ids:
                base, parts = _blended(per_type, settings.score_weights, a)
                penalty = sum(points.get(rid, default) for rid in soft.get(a, []))
                scored.append((a, base - penalty, parts))
            scored.sort(key=lambda t: t[1], reverse=True)
            ranked = []
            for rank, (a, blended, parts) in enumerate(scored, start=1):
                ineligible = a in unscored
                ranked.append(
                    {
                        "asset_id": a,
                        "rank": rank,
                        "blended_score": blended,
                        "components": parts,
                        "vetoed": a in hard or ineligible,
                        "veto_rules": soft.get(a, [])
                        + (["HARD"] if a in hard else [])
                        + (["UNSCORED"] if ineligible else []),
                        "selected": False,
                        "target_weight": None,
                    }
                )
            ranked_cache["rows"] = ranked
            writers.write_ranking(conn, run_id, ranked)
            return {"ranked": len(ranked), "hard_vetoed": len(hard), "unscored": len(unscored)}

        _do("rank", _rank)
        # Read back from cycle_ranking, not the `unscored` local above -- it only exists when
        # `rank` actually ran this call; a resumed run that skips an already-`done` `rank` step
        # (T-097's own resume contract) would otherwise leave `report.unscored` at its default 0
        # even though the persisted ranking has UNSCORED rows (PR #99 review).
        unscored_rows = conn.execute(
            "SELECT a.ticker FROM cycle_ranking r JOIN assets a ON a.id = r.asset_id, "
            "json_each(r.veto_rules_json) je "
            "WHERE r.cycle_run_id = ? AND je.value = 'UNSCORED' ORDER BY a.ticker",
            (run_id,),
        ).fetchall()
        report.unscored = len(unscored_rows)
        report.unscored_tickers = [str(row["ticker"]) for row in unscored_rows]

        # -- positions (SELECTION only)
        if "positions" in steps:

            def _positions() -> dict:
                rows = ranked_cache.get("rows") or _load_ranking(conn, run_id)
                cands = _book_candidates(rows, ticker_of, sector_name_of, price_obs)
                book = _call_build_book(settings, cands)
                weights = book.weights
                closes = {a: (price_obs.get(a) or {}).get("close") for a in weights}
                if cycle_type == "REPLAY":
                    # T-115: a replay never touches, and is never refused for conflicting
                    # with, the live book -- `cycle backfill --force` resets its own
                    # (isolated) book's date range up front instead.
                    reason = out_of_order_replay_reason(conn, cycle_date)
                    if reason is not None:
                        raise OutOfOrderCycle(reason)  # noqa: TRY301
                else:
                    reason = out_of_order_reason(conn, cycle_date)
                    if reason is not None and not settings.allow_backdated_positions:
                        raise OutOfOrderCycle(reason)  # noqa: TRY301
                    report.backdated_guard_bypassed = reason
                # T-136: the effective caps, relaxations, shortfall and preferences, recorded just
                # before the write (a refused run records nothing, so a retry may change them).
                merge_params(conn, run_id, _book_record(settings, book))
                _log_book(cycle_type, cycle_date, book)
                if cycle_type == "REPLAY":
                    opened, closed = sync_replay_positions(
                        conn, cycle_date, weights, closes, cycle_run_id=run_id
                    )
                else:
                    opened, closed = writers.sync_positions(
                        conn, cycle_date, weights, closes, cycle_run_id=run_id
                    )
                # reflect selection back into cycle_ranking
                for r in rows:
                    r["selected"] = int(r["asset_id"]) in weights
                    r["target_weight"] = weights.get(int(r["asset_id"]))
                writers.write_ranking(conn, run_id, rows)
                report.selected = len(weights)
                report.book = book
                by_id = {c.asset_id: c for c in cands}
                report.book_rows = [
                    (by_id[a].ticker, by_id[a].sector, w) for a, w in weights.items()
                ]
                return {
                    "opened": opened,
                    "closed": closed,
                    "positions": len(weights),
                    "n_held": book.n_held,
                    "shortfall": book.shortfall,
                }

            _do("positions", _positions)

        finish_cycle(conn, run_id, "completed")
    except Exception:
        finish_cycle(conn, run_id, "failed")
        raise
    return report


# -- small query helpers ------------------------------------------------


def _norm_map(conn: Database, stype: str, cycle_date: str) -> dict[int, float | None]:
    rows = conn.execute(
        "SELECT asset_id, normalized_score FROM score_snapshot WHERE score_type=? AND event_time=?",
        (stype, cycle_date),
    ).fetchall()
    return {int(r["asset_id"]): r["normalized_score"] for r in rows}


def _load_ranking(conn: Database, run_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM cycle_ranking WHERE cycle_run_id = ? ORDER BY rank", (run_id,)
    ).fetchall()
    return [
        {
            "asset_id": int(r["asset_id"]),
            "rank": int(r["rank"]),
            "blended_score": float(r["blended_score"]),
            "components": json.loads(r["components_json"] or "{}"),
            "vetoed": bool(r["vetoed"]),
            "veto_rules": json.loads(r["veto_rules_json"] or "[]"),
            "selected": bool(r["selected"]),
            "target_weight": r["target_weight"],
        }
        for r in rows
    ]


# -- select --dry-run: a read-only preview (T-136, PR #119 review) ----------------------------------


class NoStoredRanking(RuntimeError):
    """``select --dry-run`` found no stored ranking for the date."""


@dataclass(frozen=True)
class DryRunBook:
    """The preview: the book, and which stored ranking it was built from."""

    cycle_date: str
    source_cycle_type: str
    source_cycle_run_id: int
    book: BookResult
    rows: list[tuple[str, str | None, float]]  # (ticker, sector, weight), in book order


def dry_run_book(
    settings: CycleSettings, cycle_date: str, *, conn: Database | None = None
) -> DryRunBook:
    """The book `select` would build at *cycle_date*, from the ranking already stored for it.

    **Strictly read-only**: it never opens, finishes or modifies a ``cycle_run`` (the earlier
    version re-ran the live run's steps, rewrote its ``finished_at`` and marked it ``failed`` when
    a preference was invalid), writes no checkpoint and no position, and runs no cycle step -- so
    `ensure_schema`, the guards and the fundamental hook are not involved either. Without *conn* it
    opens the database ``mode=ro``, so a write would raise. The ranking is the date's SELECTION
    run's, else its MONITORING run's (the same steps minus ``positions``); with neither it refuses.
    """
    # what is wrong whatever the data is: before anything is read
    validate_settings(
        settings.top_n,
        scheme=settings.weight_scheme,
        max_name_weight=settings.max_name_weight,
        max_sector_weight=settings.max_sector_weight,
        pins=settings.pins,
        exclude=settings.exclude,
        only_sectors=settings.only_sectors,
    )
    owned = conn is None
    if conn is None:
        conn = connect_ro(settings.db_path)
    try:
        source = conn.execute(
            "SELECT cr.id, cr.cycle_type FROM cycle_run cr WHERE cr.cycle_date = ? "
            "AND cr.cycle_type IN ('SELECTION', 'MONITORING') "
            "AND EXISTS (SELECT 1 FROM cycle_ranking r WHERE r.cycle_run_id = cr.id) "
            "ORDER BY CASE cr.cycle_type WHEN 'SELECTION' THEN 0 ELSE 1 END LIMIT 1",
            (cycle_date,),
        ).fetchone()
        if source is None:
            raise NoStoredRanking(
                f"no stored ranking for {cycle_date}: run `cycle monitor --analysis-date "
                f"{cycle_date}` first"
            )
        rows = _load_ranking(conn, int(source["id"]))
        labels = data.asset_labels(conn, [int(r["asset_id"]) for r in rows])
        tickers = {a: t for a, (t, _s) in labels.items()}
        sectors = {a: s for a, (_t, s) in labels.items()}
        cands = _book_candidates(
            rows,
            tickers,
            sectors,
            data.latest_price_observation(conn, cycle_date, settings.observation_engine_version),
        )
        book = _call_build_book(settings, cands)
        by_id = {c.asset_id: c for c in cands}
        return DryRunBook(
            cycle_date,
            str(source["cycle_type"]),
            int(source["id"]),
            book,
            [(by_id[a].ticker, by_id[a].sector, w) for a, w in book.weights.items()],
        )
    finally:
        if owned:
            conn.close()


# -- public entrypoints ----------------------------------------------


def run_selection(
    settings: CycleSettings,
    cycle_date: str,
    *,
    conn: Database | None = None,
    fundamental_hook: FundamentalHook | None = None,
) -> CycleReport:
    owned = conn is None
    if conn is None:
        conn = connect(settings.db_path)
    try:
        return _run(
            settings,
            "SELECTION",
            cycle_date,
            _SELECTION_STEPS,
            conn=conn,
            fundamental_hook=fundamental_hook,
        )
    finally:
        if owned:
            conn.close()


def run_monitoring(
    settings: CycleSettings,
    cycle_date: str,
    *,
    conn: Database | None = None,
    fundamental_hook: FundamentalHook | None = None,
) -> CycleReport:
    owned = conn is None
    if conn is None:
        conn = connect(settings.db_path)
    try:
        return _run(
            settings,
            "MONITORING",
            cycle_date,
            _MONITORING_STEPS,
            conn=conn,
            fundamental_hook=fundamental_hook,
        )
    finally:
        if owned:
            conn.close()


def run_replay(
    settings: CycleSettings,
    cycle_date: str,
    *,
    conn: Database | None = None,
    fundamental_hook: FundamentalHook | None = None,
) -> CycleReport:
    """``cycle backfill``'s entrypoint (T-115): the same step sequence and checkpointing as
    :func:`run_selection`, but ``cycle_type='REPLAY'`` keeps its ``cycle_run``/
    ``cycle_checkpoint`` rows and its positions (``portfolio_position_replay``, via
    ``cycle.replay``) entirely separate from a live ``select`` run at the same date."""
    owned = conn is None
    if conn is None:
        conn = connect(settings.db_path)
    try:
        return _run(
            settings,
            "REPLAY",
            cycle_date,
            _SELECTION_STEPS,
            conn=conn,
            fundamental_hook=fundamental_hook,
        )
    finally:
        if owned:
            conn.close()
