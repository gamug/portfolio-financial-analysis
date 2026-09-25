"""SelectionCycle / MonitoringCycle -- a checkpointed topological runner.

Strands' ``multiagent.GraphBuilder`` could drive the same step graph; the plain
runner here keeps ``cycle_checkpoint`` as the single source of truth for resume,
so the framework stays swappable. Deterministic steps run inline; the FUNDAMENTAL
step is delegated to an optional hook (the Strands ``FundamentalAnalyst``).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from portfolio_common.db import Database

from cycle import data, writers
from cycle.config import CycleSettings
from cycle.construction import Candidate, target_weights
from cycle.db import ensure_schema
from cycle.rules import RuleContext, enabled_rules, seed_catalog
from cycle.scores import sector, technical, valorization
from cycle.scores.normalize import normalized_scores
from cycle.state import check_manifest, checkpoint, done_steps, finish_cycle, open_cycle
from cycle.writers import OutOfOrderCycle, out_of_order_reason
from kg_schema import connect
from kg_schema.provenance import code_version
from kg_schema.versions import (
    DATA_QUALITY_GATE_VERSION,
    manifest_tag,
    parse_metric_selection,
    resolve_metric_versions,
)

FundamentalHook = Callable[[Database, list[int], str], None]

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
    ensure_schema(conn)
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
    run_id = open_cycle(
        conn,
        cycle_type,
        cycle_date,
        {**settings.model_dump(), "manifest": manifest, "manifest_tag": tag},
        code_version=code_version(),
    )
    already = done_steps(conn, run_id)
    report = CycleReport(run_id, cycle_type, cycle_date, manifest_tag=tag)

    universe_rows = data.active_universe(
        conn, settings.universe, cycle_date, settings.universe_db_path
    )
    asset_ids = [int(r["id"]) for r in universe_rows]
    sector_of = {int(r["id"]): r["sector_id"] for r in universe_rows}
    # Ring-1 data-quality gates (T-065): quarantined metrics read as NULL everywhere below.
    dq = data.data_quality(conn, cycle_date, versions)
    metrics = dq.apply(data.latest_metrics(conn, cycle_date, versions))
    price_obs = data.latest_price_observation(conn, cycle_date)

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
            scores = technical.compute(obs_in)
            writers.write_scores(
                conn,
                "TECHNICAL",
                cycle_date,
                {s.asset_id: s.raw_value for s in scores},
                {},
                {s.asset_id: s.components for s in scores},
                run_id=run_id,
            )
            return {"scored": len(scores)}

        _do("technical", _technical)

        # -- valorization
        def _valorization() -> dict:
            mcap = data.market_cap_estimates(conn, cycle_date, metrics, versions)
            rows = {}
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
            return {"scored": len(scores)}

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
            seed_catalog(conn)
            ctx = RuleContext(
                cycle_date=cycle_date,
                metrics={a: metrics.get(a, {}) for a in asset_ids},
                price_obs={a: price_obs[a] for a in asset_ids if a in price_obs},
                last_fundamental=data.last_fundamental_dates(conn, cycle_date),
                data_quality={a: dq.hard[a] for a in asset_ids if a in dq.hard},
            )
            hits = [h for rule in enabled_rules(conn) for h in rule.evaluate(ctx)]  # type: ignore[attr-defined]
            opened, cleared = writers.write_vetoes(conn, cycle_date, hits, run_id=run_id)
            report.vetoed = len({h.asset_id for h in hits if h.severity == "HARD"})
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
            scored = []
            for a in asset_ids:
                base, parts = _blended(per_type, settings.score_weights, a)
                penalty = settings.soft_veto_penalty * len(soft.get(a, []))
                scored.append((a, base - penalty, parts))
            scored.sort(key=lambda t: t[1], reverse=True)
            ranked = []
            for rank, (a, blended, parts) in enumerate(scored, start=1):
                ranked.append(
                    {
                        "asset_id": a,
                        "rank": rank,
                        "blended_score": blended,
                        "components": parts,
                        "vetoed": a in hard,
                        "veto_rules": soft.get(a, []) + (["HARD"] if a in hard else []),
                        "selected": False,
                        "target_weight": None,
                    }
                )
            ranked_cache["rows"] = ranked
            writers.write_ranking(conn, run_id, ranked)
            return {"ranked": len(ranked), "hard_vetoed": len(hard)}

        _do("rank", _rank)

        # -- positions (SELECTION only)
        if "positions" in steps:

            def _positions() -> dict:
                rows = ranked_cache.get("rows") or _load_ranking(conn, run_id)
                eligible = [r for r in rows if not r["vetoed"]]
                cands = [
                    Candidate(
                        asset_id=int(r["asset_id"]),
                        blended_score=float(r["blended_score"]),
                        sector_id=sector_of.get(int(r["asset_id"])),
                        realized_vol_90d=(price_obs.get(int(r["asset_id"])) or {}).get(
                            "realized_vol_90d"
                        ),
                    )
                    for r in eligible
                ]
                weights = target_weights(
                    cands,
                    top_n=settings.top_n,
                    scheme=settings.weight_scheme,
                    max_name_weight=settings.max_name_weight,
                    max_sector_weight=settings.max_sector_weight,
                )
                reason = out_of_order_reason(conn, cycle_date)
                if reason is not None and not settings.allow_backdated_positions:
                    raise OutOfOrderCycle(reason)  # noqa: TRY301
                report.backdated_guard_bypassed = reason
                closes = {a: (price_obs.get(a) or {}).get("close") for a in weights}
                opened, closed = writers.sync_positions(
                    conn, cycle_date, weights, closes, cycle_run_id=run_id
                )
                # reflect selection back into cycle_ranking
                for r in rows:
                    r["selected"] = int(r["asset_id"]) in weights
                    r["target_weight"] = weights.get(int(r["asset_id"]))
                writers.write_ranking(conn, run_id, rows)
                report.selected = len(weights)
                return {"opened": opened, "closed": closed, "positions": len(weights)}

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
