"""Config for the selection / monitoring cycle."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from kg_schema.env import DB_ENV_VAR, database_path, universe_database_path

# T-141: equal weights over the three components on a common 50 + 10z scale (the 1/N argument).
# SEMANTIC stays out until Work item 4 writes it; `_blended` renormalizes over whatever is present.
_DEFAULT_WEIGHTS = {"FUNDAMENTAL": 1 / 3, "VALORIZATION": 1 / 3, "TECHNICAL": 1 / 3}


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
    # T-134/T-136: score_tilt is an equal-weight core with a bounded score tilt, each weight in
    # [0.5/N, 1.5/N]; the other three stay selectable so old runs remain reproducible.
    weight_scheme: str = "score_tilt"  # score_tilt | equal | score_proportional | inverse_vol
    # None derives the name cap (1.5/N for score_tilt; 0.10 for the legacy schemes). An explicit
    # value wins -- which is why the default must be None: 0.10 passed explicitly would override
    # 1.5/N silently.
    max_name_weight: float | None = None
    max_sector_weight: float = 0.30
    # User preferences (T-134 decision 5): decision support, never the thesis book. Pins are held
    # first (a HARD-vetoed pin is refused); `only_sectors` None means every sector. A writing
    # `select` refuses them; `select --dry-run` (a read-only preview, `dry_run_book`) and `backfill` (the
    # replay book) accept them.
    pins: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    exclude_sectors: tuple[str, ...] = ()
    only_sectors: tuple[str, ...] | None = None
    soft_veto_penalty: float = 15.0  # points off the blended score per active SOFT veto, unless the rule declares its own (T-070)
    # T-070: the one price_observation engine version the cycle reads (TECHNICAL v2 and the three
    # price vetoes need priceobs-v2's columns). Never "latest per day": a date holding rows of two
    # versions would mix them.
    observation_engine_version: str = "priceobs-v2"
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
    # T-125: a cycle refuses to write veto transitions at a cycle_date older than the latest
    # one already recorded (the same rule as allow_backdated_positions, but against the
    # shared `veto` stints table, which live select/monitor and REPLAY backfill runs alike
    # write into). Meant for a deliberate historical re-run, not routine use; a REPLAY run
    # instead resets the way past via `cycle backfill --force`.
    allow_backdated_veto: bool = False
    # T-119 (PR #78 review): a universe member with no FUNDAMENTAL score at all (not merely a
    # stale one) is ineligible for selection the same cycle it is detected, not through the T-1
    # veto lag -- but rank refuses outright, rather than silently building a portfolio blind on
    # most of the universe, once more than this share has no score at all.
    unscored_max_share: float = 0.05

    @property
    def has_preferences(self) -> bool:
        return bool(
            self.pins or self.exclude or self.exclude_sectors or self.only_sectors is not None
        )

    def construction(self) -> dict[str, object]:
        """The settings that decide the book -- what a resumed run must not change (T-136)."""

        def norm(values: tuple[str, ...]) -> list[str]:
            return sorted({v.strip().casefold() for v in values})

        return {
            "top_n": self.top_n,
            "weight_scheme": self.weight_scheme,
            "max_name_weight": self.max_name_weight,
            "max_sector_weight": self.max_sector_weight,
            "pins": norm(self.pins),
            "exclude": norm(self.exclude),
            "exclude_sectors": norm(self.exclude_sectors),
            "only_sectors": None if self.only_sectors is None else norm(self.only_sectors),
        }

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
