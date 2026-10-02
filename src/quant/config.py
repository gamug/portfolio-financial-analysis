"""Config for the Markowitz benchmark build."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from kg_schema.env import DB_ENV_VAR, database_path, universe_database_path

_DEFAULT_OBJECTIVES = ["min_var", "tangency", "target_vol", "risk_parity"]
DEFAULT_PRICING_BASE_URL = "http://host.docker.internal:8000/pricing"


class QuantSettings(BaseModel):
    """The shared DB path plus every knob the gate / risk model / optimizer takes.

    ``.load()`` reads only ``KG_FINANCIAL_DB``; the CLI overlays the rest from flags
    via ``model_copy``.
    """

    db_path: Path
    universe_db_path: Path = Field(
        default_factory=lambda: Path(universe_database_path()).expanduser()
    )
    pricing_base_url: str = DEFAULT_PRICING_BASE_URL
    # backfill-actions circuit breaker (T-085): the gateway is the only corporate-actions
    # source, so after this many consecutive gateway *errors* (not per-ticker warnings)
    # the run fails fast instead of paying max_retries x timeout for every remaining asset.
    gateway_max_consecutive_failures: int = Field(default=3, ge=1)
    # Which ``fundamental_metrics`` engine version(s) the risk model reads (T-090): ``None`` =
    # the newest stored; ``"metrics-v1"`` = that one; ``"valuation=metrics-v1"`` = per group.
    # Resolved into a manifest whose tag is folded into the risk-model / optimizer version keys,
    # so runs over different inputs write parallel rows instead of overwriting each other.
    metrics_version: str | None = None
    # Version *constraints* for the non-metric inputs (T-093), same grammar as
    # ``metrics_version`` plus ``>=``/``!=``/``latest``. ``None`` keeps today's behaviour: the
    # configured ``return_engine_version``, the per-asset corporate-action priority, and the
    # ``risk_model_version`` label (loaded, or built when missing). A constraint resolves
    # strictly against what is stored -- an unsatisfiable one is an error, never a fallback.
    returns_version: str | None = None  # which quant_return_daily series build-risk-model reads
    risk_model_select: str | None = None  # which stored risk model optimize reads
    corpact_version: str | None = None  # which corporate_action engine build-returns reads
    # The named profile (``--version-profile FILE --profile NAME``) the constraints came from,
    # recorded on the run manifest; the flags themselves win over the profile.
    version_profile: str | None = None

    # --- score-independent universe gate ---
    universe: str = "SP500"
    min_history_days: int = 504
    liquidity_min_dollar_volume: float = 5_000_000.0
    liquidity_lookback_days: int = 21
    exclude_hard_vetoed: bool = True
    # T-110: build-risk-model refuses an --analysis-date/--as-of past price_daily's last
    # stored date (the price spine) -- proceeding would silently build a model claiming to
    # be "as of" a date the price data doesn't actually reach yet. Meant for a deliberate
    # run ahead of the spine (e.g. testing), not routine use.
    allow_stale_prices: bool = False
    # T-114: every quant command refuses to write a run whose own code_version() is dirty
    # (uncommitted changes) -- its results would come from code HEAD alone can't reproduce.
    # Meant for a deliberate run from a work-in-progress checkout, not routine use.
    allow_dirty: bool = False

    # --- return panel / risk model ---
    lookback_days: int = 756
    periods_per_year: int = 252
    cov_estimator: str = "ledoit_wolf_cc"  # ledoit_wolf_cc | ledoit_wolf_diag | sample
    # mu for the return-aware objectives (tangency / target_vol / frontier) and for the
    # expected-return / Sharpe reported on every book. All three estimators are total returns
    # (T-109): `equilibrium` is `rf + delta*Sigma*w_mkt`, not the excess `delta*Sigma*w_mkt`
    # alone -- it carries real cross-sectional dispersion with near-zero estimation noise;
    # `james_stein` over ~5y of daily data shrinks mu almost flat, which collapses the frontier
    # onto min_var.
    ret_estimator: str = "equilibrium"  # equilibrium | james_stein | hist_mean
    equilibrium_risk_aversion: float = 2.5
    risk_free_rate: float = 0.045
    rf_source: str = "constant"  # constant | csv | fred
    rf_csv_path: Path | None = None

    # --- optimizer ---
    objectives: list[str] = Field(default_factory=lambda: list(_DEFAULT_OBJECTIVES))
    headline_objective: str = "min_var"
    target_volatility: float | None = None  # None => match the live book's trailing realized vol
    max_name_weight: float | None = 0.05  # None => no per-name box cap
    min_name_weight: float = 0.0
    max_sector_weight: float | None = 0.30  # None => no per-sector cap
    frontier_k: int = 15
    turnover_cap: float | None = None
    solver: str = "CLARABEL"

    # --- append-only engine-version knobs ---
    corpact_engine_version: str = "corpact-v1"
    # Bumped from "qret-v1" when the Q3 fix (docs/model_fixes.md; the derivation it added
    # was later removed by T-085 -- dividends now come only from the gateway) landed:
    # quant_return_daily was append-only keyed on (asset_id, obs_date, engine_version), so
    # a static version silently no-op'd every re-run and never folded corrected dividends
    # into the series. Since T-131 a row is rewritten when its values change, so a rebuild
    # reaches it without a bump; the version still marks a change of method. build-returns refuses to run until a clean gateway
    # backfill-actions covers the window (T-086), because rows built without dividends
    # would be locked in under this version by INSERT OR IGNORE.
    return_engine_version: str = "qret-v2"
    # The price_observation series the universe gate reads (T-131 review). quant is a leaf and
    # cannot import pricing_agent's PRICE_OBSERVATION_ENGINE_VERSION, so the pin lives here.
    observation_engine_version: str = "priceobs-v1"
    risk_model_version: str = "rm-v1"
    optimizer_engine_version: str = "opt-v1"
    # bench-v2 (T-108): the mean of *simple* returns over the gated panel, compounded (1 + r);
    # bench-v1 averaged log returns over every name with a row.
    benchmark_engine_version: str = "bench-v2"

    @classmethod
    def load(cls, env_file: str | os.PathLike[str] | None = None) -> QuantSettings:
        load_dotenv(env_file, override=False)
        db_path = database_path()
        if not db_path:
            raise RuntimeError(f"missing required environment variable: {DB_ENV_VAR}")
        return cls(
            db_path=Path(db_path).expanduser(),
            universe_db_path=Path(universe_database_path()).expanduser(),
            pricing_base_url=os.environ.get("PRICING_BASE_URL", DEFAULT_PRICING_BASE_URL),
        )
