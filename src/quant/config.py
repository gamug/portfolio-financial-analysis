"""Config for the Markowitz benchmark build."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from kg_schema.env import DB_ENV_VAR, database_path

_DEFAULT_OBJECTIVES = ["min_var", "tangency", "target_vol"]
DEFAULT_PRICING_BASE_URL = "http://host.docker.internal:8000/pricing"


class QuantSettings(BaseModel):
    """The shared DB path plus every knob the gate / risk model / optimizer takes.

    ``.load()`` reads only ``KG_FINANCIAL_DB``; the CLI overlays the rest from flags
    via ``model_copy``.
    """

    db_path: Path
    pricing_base_url: str = DEFAULT_PRICING_BASE_URL

    # --- score-independent universe gate ---
    universe: str = "SP500"
    min_history_days: int = 504
    liquidity_min_dollar_volume: float = 5_000_000.0
    liquidity_lookback_days: int = 21
    exclude_hard_vetoed: bool = True

    # --- return panel / risk model ---
    lookback_days: int = 756
    periods_per_year: int = 252
    cov_estimator: str = "ledoit_wolf_cc"  # ledoit_wolf_cc | ledoit_wolf_diag | sample
    ret_estimator: str = "james_stein"  # james_stein | hist_mean | equilibrium
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
    return_engine_version: str = "qret-v1"
    risk_model_version: str = "rm-v1"
    optimizer_engine_version: str = "opt-v1"
    benchmark_engine_version: str = "bench-v1"

    @classmethod
    def load(cls, env_file: str | os.PathLike[str] | None = None) -> QuantSettings:
        load_dotenv(env_file, override=False)
        db_path = database_path()
        if not db_path:
            raise RuntimeError(f"missing required environment variable: {DB_ENV_VAR}")
        return cls(
            db_path=Path(db_path).expanduser(),
            pricing_base_url=os.environ.get("PRICING_BASE_URL", DEFAULT_PRICING_BASE_URL),
        )
