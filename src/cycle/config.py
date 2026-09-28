"""Config for the selection / monitoring cycle."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from kg_schema.env import DB_ENV_VAR, database_path, universe_database_path

_DEFAULT_WEIGHTS = {"FUNDAMENTAL": 0.4, "VALORIZATION": 0.3, "TECHNICAL": 0.2, "SEMANTIC": 0.1}


class CycleSettings(BaseModel):
    """Everything a cycle needs: the shared DB, optional LLM creds, and the knobs."""

    db_path: Path
    universe_db_path: Path = Field(
        default_factory=lambda: Path(universe_database_path()).expanduser()
    )
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_url: str | None = None

    universe: str = "SP500"
    top_n: int = 30
    score_weights: dict[str, float] = Field(default_factory=lambda: dict(_DEFAULT_WEIGHTS))
    weight_scheme: str = "score_proportional"  # equal | score_proportional | inverse_vol
    max_name_weight: float = 0.10
    max_sector_weight: float = 0.30
    soft_veto_penalty: float = 15.0  # points knocked off blended score per active soft veto
    # Which fundamental_metrics engine version(s) the cycle reads (T-090): None = the newest
    # stored per group; "metrics-v1" = that one; "valuation=metrics-v1" = per group. A cycle_run
    # is unique per (type, date), so it records the manifest and refuses to be resumed under
    # different inputs rather than mixing them.
    metrics_version: str | None = None
    # T-097: `select`'s "positions" step refuses to write the live `portfolio_position` book at
    # a `cycle_date` older than one it has already written (a backdated run silently closes
    # recent positions early and reopens a stale book), unless this is set. Meant for a
    # deliberate historical backfill/validation run, not routine use; MONITORING never reaches
    # the positions step, so this has no effect there.
    allow_backdated_positions: bool = False
    # T-110: a cycle refuses a --analysis-date/--date past price_daily's last stored date
    # (the price spine) -- proceeding would score TECHNICAL/veto against prices that are, at
    # best, weeks stale while the cycle claims to be as of a later date. Meant for a
    # deliberate run ahead of the spine, not routine use.
    allow_stale_prices: bool = False
    # T-114: a cycle refuses to write its run row when code_version() is dirty (uncommitted
    # changes) -- its results would come from code HEAD alone can't reproduce. Meant for a
    # deliberate run from a work-in-progress checkout, not routine use.
    allow_dirty: bool = False
    # T-116 (PR #95 review): a cycle refuses to run when data_quality_issue holds rows under an
    # older Ring-1 gate version but none under the current one -- a gate-methodology bump
    # landed without its one-time re-gate, which would silently zero out every quarantine and
    # HARD DQ_*/DATA_QUALITY veto. Meant for a deliberate run before re-gating, not routine use.
    allow_stale_dq_gate: bool = False
    # T-119 (PR #78 review): a universe member with no FUNDAMENTAL score at all (not merely a
    # stale one) is ineligible for selection the same cycle it is detected, not through the T-1
    # veto lag -- but rank refuses outright, rather than silently building a portfolio blind on
    # most of the universe, once more than this share has no score at all.
    unscored_max_share: float = 0.05

    @classmethod
    def load(cls, env_file: str | os.PathLike[str] | None = None) -> CycleSettings:
        load_dotenv(env_file, override=False)
        db_path = database_path()
        if not db_path:
            raise RuntimeError(f"missing required environment variable: {DB_ENV_VAR}")
        return cls(
            db_path=Path(db_path).expanduser(),
            universe_db_path=Path(universe_database_path()).expanduser(),
            llm_api_key=os.environ.get("LLM_API_KEY"),
            llm_model=os.environ.get("LLM_MODEL"),
            llm_url=os.environ.get("LLM_URL"),
        )
