"""Orchestrate gate -> panel -> risk model -> DB, one ``quant_run`` per command."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from portfolio_common.db import Database, DatabaseError

from kg_schema import connect
from kg_schema.market_cap import MarketCapResult, market_caps_as_of
from kg_schema.provenance import DirtyTree, code_version, dirty_tree_reason
from kg_schema.queries import StaleAsOf, stale_as_of_reason
from quant.caps import EffectiveCaps, resolve_caps
from quant.config import QuantSettings
from quant.db import (
    PortfolioRow,
    RiskModelMeta,
    ensure_schema,
    insert_covariance,
    insert_expected_returns,
    insert_frontier_points,
    insert_portfolio,
    insert_risk_model,
    load_covariance,
    load_expected_returns,
    load_risk_model,
    load_sector_of,
    sync_positions,
)
from quant.manifest import QuantManifest, resolve_quant_manifest
from quant.objective import ObjectiveContext, objective_param, resolve_objectives
from quant.optimize import Constraints, efficient_frontier, min_variance
from quant.panel import ReturnPanel, build_return_panel
from quant.rates import load_risk_free
from quant.risk import (
    CovTarget,
    equilibrium_returns,
    historical_mean,
    james_stein_mean,
    ledoit_wolf_covariance,
    sample_covariance,
)
from quant.state import fail_run, finish_run, merge_run_params, open_run
from quant.universe import settings_gate

_W_EPS = 1e-6  # sparsify: drop near-zero weights from the stored book


@dataclass
class RiskModelResult:
    model_id: int
    as_of: str
    n_assets: int
    cov_estimator: str
    cov_shrinkage: float | None
    cov_rows: int
    stored_cov: bool
    manifest_tag: str = ""
    stale_prices_bypassed: str | None = None  # T-110: why, if --allow-stale-prices overrode it
    dirty_tree_bypassed: str | None = None  # T-114: why, if --allow-dirty overrode it
    market_cap_coverage: dict[str, Any] = field(default_factory=dict)  # T-132: caps and their age


def _covariance(settings: QuantSettings, panel: ReturnPanel) -> tuple[np.ndarray, float | None]:
    ppy = settings.periods_per_year
    if settings.cov_estimator == "sample":
        return sample_covariance(panel.returns, periods_per_year=ppy), None
    target: CovTarget = (
        "diagonal" if settings.cov_estimator == "ledoit_wolf_diag" else "constant_correlation"
    )
    sigma, delta = ledoit_wolf_covariance(panel.returns, target=target, periods_per_year=ppy)
    return sigma, delta


class MissingMarketCaps(RuntimeError):
    """Panel assets have no market cap as of the date (T-132). The equilibrium prior weights
    assets by cap, so an unvalued one would silently get weight 0 -- dropped from the market
    portfolio, and every other asset's weight inflated -- unless the run says so out loud."""


def _split_dual_listings(res: MarketCapResult, asset_ids: list[int]) -> dict[int, float]:
    """Each panel asset's weight basis with one company counted once (PR #110 review).

    The reader gives every *listing* the company's whole cap (right for ``cycle``: yields and size
    are company-level), but two listings of one issuer (GOOG/GOOGL, FOX/FOXA, NWS/NWSA share a CIK)
    in the market portfolio would put the company's cap in twice. Panel assets sharing a CIK split
    the company cap equally; sibling classes are nearly collinear, so ``pi = delta * Sigma * w``
    barely depends on how."""
    groups: dict[str, list[int]] = {}
    for a in asset_ids:
        cap = res.caps.get(a)
        if cap is not None and cap.cik:
            groups.setdefault(cap.cik, []).append(a)
    basis = {a: res.caps[a].value for a in asset_ids if a in res.caps}
    for members in groups.values():
        for a in members:
            basis[a] = res.caps[a].value / len(members)
    return basis


def _market_caps(
    settings: QuantSettings, panel: ReturnPanel, conn: Database, *, as_of: str
) -> tuple[np.ndarray, dict[str, Any]]:
    """The panel's market caps from the shared as-of reader. A panel asset the reader cannot value
    refuses the model (:class:`MissingMarketCaps`) unless ``settings.allow_missing_caps`` -- then it
    gets weight 0 and the run records who and why. A panel with no cap at all always refuses: there
    is no market portfolio to be equilibrium to, and an equal-weight stand-in would pass for one."""
    res = market_caps_as_of(
        conn,
        panel.asset_ids,
        as_of=as_of,
        max_share_age_days=settings.market_cap_max_share_age_days,
        max_price_age_days=settings.market_cap_max_price_age_days,
        corpact_engine_version=settings.corpact_engine_version,
    )
    if not res.caps:
        raise MissingMarketCaps(f"no panel asset has a market cap as of {as_of}")
    if res.missing and not settings.allow_missing_caps:
        shown = ", ".join(f"{a} ({r})" for a, r in sorted(res.missing.items())[:10])
        more = f" (+{len(res.missing) - 10} more)" if len(res.missing) > 10 else ""  # noqa: PLR2004
        raise MissingMarketCaps(
            f"{len(res.missing)} of {panel.n_assets} panel assets have no market cap as of "
            f"{as_of}: asset_id {shown}{more}; pass --allow-missing-caps to build with them "
            "weighted 0 in the market portfolio, recorded on the run"
        )
    basis = _split_dual_listings(res, panel.asset_ids)
    caps = np.array([basis.get(a, 0.0) for a in panel.asset_ids], dtype=np.float64)
    coverage = res.coverage()
    by_cik: dict[str, list[int]] = {}
    for a in panel.asset_ids:
        if a in res.caps and res.caps[a].cik:
            by_cik.setdefault(str(res.caps[a].cik), []).append(a)
    # companies listed more than once in the panel, each counted once in the market portfolio
    coverage["dual_listed"] = {cik: ids for cik, ids in sorted(by_cik.items()) if len(ids) > 1}
    return caps, coverage


def _expected_returns(
    settings: QuantSettings,
    panel: ReturnPanel,
    sigma: np.ndarray,
    caps: np.ndarray,
    *,
    rf: float,
) -> dict[str, dict[int, float]]:
    """All three estimators are total returns (T-109): ``hist_mean``/``james_stein`` are means
    of the panel's own total-return series, so ``equilibrium`` must add back *rf* -- otherwise
    it stores the excess return ``lambda*Sigma*w_mkt`` alone, and every downstream Sharpe/
    tangency (which subtracts *rf* once, expecting a total-return mu) would subtract it twice."""
    ppy = settings.periods_per_year
    hist = historical_mean(panel.returns, periods_per_year=ppy)
    js = james_stein_mean(panel.returns, periods_per_year=ppy)
    eq = equilibrium_returns(sigma, caps, risk_aversion=settings.equilibrium_risk_aversion, rf=rf)
    return {
        "hist_mean": dict(zip(panel.asset_ids, hist.tolist(), strict=True)),
        "james_stein": dict(zip(panel.asset_ids, js.tolist(), strict=True)),
        "equilibrium": dict(zip(panel.asset_ids, eq.tolist(), strict=True)),
    }


def run_build_risk_model(
    settings: QuantSettings,
    *,
    as_of: str,
    conn: Database | None = None,
    store_cov: bool = True,
) -> RiskModelResult:
    owns = conn is None
    conn = conn or connect(settings.db_path)
    try:
        ensure_schema(conn)
        # Resolve the input versions first: a selection naming a version that is not stored
        # is a user error and should fail before any run row is written.
        manifest = resolve_quant_manifest(conn, settings)
        stale_reason = stale_as_of_reason(conn, as_of)
        cv = code_version()
        dirty_reason = dirty_tree_reason(cv)
        run_id = open_run(
            conn,
            "build-risk-model",
            as_of=as_of,
            params={
                **settings.model_dump(mode="json"),
                "manifest": manifest.record(),
                "manifest_tag": manifest.tag,
                "stale_as_of_bypassed": stale_reason,
                "dirty_tree_bypassed": dirty_reason,
            },
            code_version=cv,
        )
        try:
            if dirty_reason is not None and not settings.allow_dirty:
                raise DirtyTree(  # noqa: TRY301
                    f"{dirty_reason}; pass --allow-dirty for a deliberate run from an "
                    "uncommitted tree"
                )
            if stale_reason is not None and not settings.allow_stale_prices:
                raise StaleAsOf(  # noqa: TRY301
                    f"{stale_reason}; pass --allow-stale-prices for a deliberate run ahead "
                    "of the price spine"
                )
            gate = settings_gate(
                conn, settings, as_of=as_of, return_engine_version=manifest.return_engine_version
            )
            if not gate.asset_ids:
                raise RuntimeError(f"universe gate is empty as of {as_of}")  # noqa: TRY301
            panel = build_return_panel(
                conn,
                as_of=as_of,
                lookback_days=settings.lookback_days,
                min_history_days=settings.min_history_days,
                universe_asset_ids=gate.asset_ids,
                return_engine_version=manifest.return_engine_version,
            )
            sigma, delta = _covariance(settings, panel)
            rf = load_risk_free(settings, as_of=as_of, conn=conn)
            caps, coverage = _market_caps(settings, panel, conn, as_of=as_of)
            merge_run_params(conn, run_id, {"market_caps": coverage})
            merge_run_params(
                conn,
                run_id,
                {
                    "caps": effective_caps(
                        settings, panel.asset_ids, load_sector_of(conn, panel.asset_ids)
                    ).record()
                },
            )
            mu_by_model = _expected_returns(settings, panel, sigma, caps, rf=rf.annualized_rate)

            spec = {
                "asset_ids": panel.asset_ids,
                "date_start": panel.dates[0],
                "date_end": panel.dates[-1],
                "n_dates": len(panel.dates),
                "sha256": panel.spec_sha256,
            }
            model_id = insert_risk_model(
                conn,
                RiskModelMeta(
                    as_of=as_of,
                    model_version=manifest.tagged(settings.risk_model_version),
                    lookback_days=settings.lookback_days,
                    min_history_days=settings.min_history_days,
                    n_assets=panel.n_assets,
                    cov_estimator=settings.cov_estimator,
                    cov_shrinkage=delta,
                    ret_estimator=settings.ret_estimator,
                    periods_per_year=settings.periods_per_year,
                    panel_engine_version=manifest.return_engine_version,
                    panel_spec_json=json.dumps(spec, separators=(",", ":")),
                    rf_annual=rf.annualized_rate,
                    params_json=json.dumps(
                        {**settings.model_dump(mode="json"), "market_caps": coverage}, default=str
                    ),
                    quant_run_id=run_id,
                    manifest_json=manifest.json(),
                ),
            )
            insert_expected_returns(conn, model_id, mu_by_model)
            cov_rows = insert_covariance(conn, model_id, panel.asset_ids, sigma) if store_cov else 0
            finish_run(conn, run_id)
        except BaseException as exc:  # an interrupt too: never leave 'running'
            conn.rollback()  # drop uncommitted writes: fail_run commits
            fail_run(conn, run_id, str(exc))
            raise
        return RiskModelResult(
            model_id=model_id,
            as_of=as_of,
            n_assets=panel.n_assets,
            cov_estimator=settings.cov_estimator,
            cov_shrinkage=delta,
            cov_rows=cov_rows,
            stored_cov=store_cov,
            manifest_tag=manifest.tag,
            stale_prices_bypassed=stale_reason,
            dirty_tree_bypassed=dirty_reason,
            market_cap_coverage=coverage,
        )
    finally:
        if owns:
            conn.close()


# -- optimize ---------------------------------------------------------------


@dataclass
class OptimizeRunResult:
    model_id: int
    as_of: str
    books: dict[str, int]  # kind -> quant_portfolio.id
    frontier_points: int
    manifest_tag: str = ""
    stale_prices_bypassed: str | None = None  # T-110: set whenever this as_of is past the spine
    dirty_tree_bypassed: str | None = None  # T-114: why, if --allow-dirty overrode it


def _weights_json(ids: list[int], w: np.ndarray) -> str:
    return json.dumps(
        {str(ids[i]): round(float(v), 8) for i, v in enumerate(w) if abs(float(v)) > _W_EPS},
        separators=(",", ":"),
    )


def effective_caps(
    settings: QuantSettings, asset_ids: list[int], sector_of: dict[int, int | None]
) -> EffectiveCaps:
    """The caps this panel runs under (T-137): N-derived name cap, 0.30 sector cap, each relaxed to
    feasibility and recorded (``quant.caps``). *asset_ids* is the gated panel."""
    by_sector: dict[int, int] = {}
    for a in asset_ids:
        sid = sector_of.get(int(a))
        if sid is not None:
            by_sector[int(sid)] = by_sector.get(int(sid), 0) + 1
    return resolve_caps(
        settings.top_n,
        len(asset_ids),
        list(by_sector.values()),
        max_name_weight=settings.max_name_weight,
        max_sector_weight=settings.max_sector_weight,
    )


def _build_settings(settings: QuantSettings, manifest: QuantManifest) -> QuantSettings:
    """The settings a risk model for *manifest* is built with: its resolved label and return
    series, so the model lands under exactly the version ``optimize`` then reads."""
    return settings.model_copy(
        update={
            "risk_model_version": manifest.risk_model_version or settings.risk_model_version,
            "return_engine_version": manifest.return_engine_version,
            "returns_version": None,
        }
    )


def _model_version(settings: QuantSettings, manifest: QuantManifest) -> str:
    return manifest.tagged(manifest.risk_model_version or settings.risk_model_version)


def _resolve_model_id(
    settings: QuantSettings, as_of: str, conn: Database, run_id: int, manifest: QuantManifest
) -> int:
    """Reuse an already-stored risk model for *as_of*, or build one. The staleness check
    against the price spine is ``run_optimize``'s own responsibility (T-110): it applies
    whether or not a fresh model is built here, so it is not this function's concern."""
    row = load_risk_model(conn, as_of=as_of, model_version=_model_version(settings, manifest))
    if row is not None:
        return int(row["id"])
    build = run_build_risk_model(_build_settings(settings, manifest), as_of=as_of, conn=conn)
    conn.execute("UPDATE quant_run SET status = 'running' WHERE id = ?", (run_id,))
    return build.model_id


def _target_vol(
    settings: QuantSettings,
    sigma: np.ndarray,
    mu: np.ndarray,
    cons: Constraints,
) -> float:
    if settings.target_volatility is not None:
        return settings.target_volatility
    mv = min_variance(sigma, constraints=cons, mu=mu, solver=settings.solver)
    return mv.expected_vol * 1.25


def run_optimize(
    settings: QuantSettings,
    *,
    as_of: str,
    conn: Database | None = None,
) -> OptimizeRunResult:
    owns = conn is None
    conn = conn or connect(settings.db_path)
    try:
        ensure_schema(conn)
        # fail on an unsatisfiable selection first, before any run row exists
        manifest = resolve_quant_manifest(conn, settings, optimize_as_of=as_of)
        stale_reason = stale_as_of_reason(conn, as_of)
        cv = code_version()
        dirty_reason = dirty_tree_reason(cv)
        run_id = open_run(
            conn,
            "optimize",
            as_of=as_of,
            params={
                **settings.model_dump(mode="json"),
                "manifest": manifest.record(),
                "manifest_tag": manifest.book_tag,
                "stale_as_of_bypassed": stale_reason,
                "dirty_tree_bypassed": dirty_reason,
            },
            code_version=cv,
        )
        try:
            if dirty_reason is not None and not settings.allow_dirty:
                raise DirtyTree(  # noqa: TRY301
                    f"{dirty_reason}; pass --allow-dirty for a deliberate run from an "
                    "uncommitted tree"
                )
            if stale_reason is not None and not settings.allow_stale_prices:
                raise StaleAsOf(  # noqa: TRY301
                    f"{stale_reason}; pass --allow-stale-prices for a deliberate run ahead "
                    "of the price spine"
                )
            model_id = _resolve_model_id(settings, as_of, conn, run_id, manifest)
            ids, sigma = load_covariance(conn, model_id)
            if not ids:
                raise RuntimeError(  # noqa: TRY301
                    "no stored covariance for this risk model; "
                    "re-run build-risk-model without --no-store-cov"
                )
            mu_map = load_expected_returns(conn, model_id, settings.ret_estimator)
            mu = np.array([mu_map[a] for a in ids], dtype=np.float64)
            rm = load_risk_model(
                conn, as_of=as_of, model_version=_model_version(settings, manifest)
            )
            rf = (
                float(rm["rf_annual"])
                if rm and rm["rf_annual"] is not None
                else (settings.risk_free_rate)
            )
            sector_of = load_sector_of(conn, ids)
            caps = effective_caps(settings, ids, sector_of)
            merge_run_params(conn, run_id, {"caps": caps.record()})
            cons = Constraints(
                max_name_weight=caps.max_name_weight,
                min_name_weight=settings.min_name_weight,
                max_sector_weight=caps.max_sector_weight,
                sector_of=sector_of,
                turnover_cap=settings.turnover_cap,
                asset_ids=ids,
            )
            tv = _target_vol(settings, sigma, mu, cons)
            ctx = ObjectiveContext(
                sigma=sigma,
                mu=mu,
                rf=rf,
                constraints=cons,
                target_volatility=tv,
                solver=settings.solver,
            )

            books: dict[str, int] = {}
            for name, build in resolve_objectives(settings.objectives):
                res = build(ctx)
                pid = insert_portfolio(
                    conn,
                    PortfolioRow(
                        as_of=as_of,
                        kind=name,
                        objective=res.objective,
                        solver=res.solver,
                        status=res.status,
                        expected_return=res.expected_return,
                        expected_vol=res.expected_vol,
                        sharpe=res.sharpe,
                        rf_annual=rf,
                        n_positions=int((np.abs(res.weights) > _W_EPS).sum()),
                        engine_version=manifest.book_tagged(settings.optimizer_engine_version),
                        target_param=objective_param(name, ctx),
                        model_id=model_id,
                        quant_run_id=run_id,
                        manifest_json=manifest.json(),
                        params_json=json.dumps(caps.record(), separators=(",", ":")),
                    ),
                )
                sync_positions(
                    conn,
                    pid,
                    as_of,
                    {ids[i]: float(v) for i, v in enumerate(res.weights) if abs(float(v)) > _W_EPS},
                )
                books[name] = pid

            frontier_points = 0
            if "frontier" in settings.objectives:
                pts = efficient_frontier(
                    mu,
                    sigma,
                    k=settings.frontier_k,
                    constraints=cons,
                    rf=rf,
                    solver=settings.solver,
                )
                frontier_points = insert_frontier_points(
                    conn,
                    model_id,
                    [
                        (
                            p.k,
                            p.target_return,
                            p.expected_return,
                            p.expected_vol,
                            p.sharpe,
                            p.status,
                            _weights_json(ids, p.weights),
                        )
                        for p in pts
                    ],
                )
            finish_run(conn, run_id)
        except BaseException as exc:  # an interrupt too: never leave 'running'
            conn.rollback()  # drop uncommitted writes: fail_run commits
            fail_run(conn, run_id, str(exc))
            raise
        return OptimizeRunResult(
            model_id=model_id,
            as_of=as_of,
            books=books,
            frontier_points=frontier_points,
            manifest_tag=manifest.book_tag,
            stale_prices_bypassed=stale_reason,
            dirty_tree_bypassed=dirty_reason,
        )
    finally:
        if owns:
            conn.close()


# -- dry runs (T-093) ---------------------------------------------------------


@dataclass
class DryRunPlan:
    """What a ``--dry-run`` would do: the resolved manifest and the rows it would key."""

    command: str
    as_of: str
    manifest: QuantManifest
    model_version: str
    model_stored: bool
    book_version: str | None = None  # optimize only


def plan_build_risk_model(settings: QuantSettings, *, as_of: str, conn: Database) -> DryRunPlan:
    """Resolve what ``build-risk-model`` would read and write; writes nothing."""
    manifest = resolve_quant_manifest(conn, settings)
    version = manifest.tagged(settings.risk_model_version)
    return DryRunPlan(
        "build-risk-model",
        as_of,
        manifest,
        version,
        _model_stored(conn, as_of, version),
    )


def plan_optimize(settings: QuantSettings, *, as_of: str, conn: Database) -> DryRunPlan:
    """Resolve what ``optimize`` would read and write; writes nothing."""
    manifest = resolve_quant_manifest(conn, settings, optimize_as_of=as_of)
    version = _model_version(settings, manifest)
    return DryRunPlan(
        "optimize",
        as_of,
        manifest,
        version,
        _model_stored(conn, as_of, version),
        manifest.book_tagged(settings.optimizer_engine_version),
    )


def _model_stored(conn: Database, as_of: str, version: str) -> bool:
    try:
        return load_risk_model(conn, as_of=as_of, model_version=version) is not None
    except DatabaseError:  # a database with no quant tables yet
        return False
