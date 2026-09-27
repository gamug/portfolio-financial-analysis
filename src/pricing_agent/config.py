"""Runtime configuration for the pricing collector, sourced from ``.env``.

Deliberately independent of :mod:`fundamental_agent.config` -- this module needs no
LLM credentials, only the shared database and the pricing gateway URL.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from kg_schema.env import DB_ENV_VAR, database_path, universe_database_path

# The gateway mounts the pricing service at /pricing; its own price route is
# /pricing/{ticker}, hence the doubled segment for candle requests. The /universe
# route sits at the mount root (single segment).
DEFAULT_PRICING_BASE_URL = "http://host.docker.internal:8000/pricing"


class Settings(BaseModel):
    """Everything the collector needs: the shared SQLite DB and the gateway URL."""

    db_path: Path
    universe_db_path: Path = Field(
        default_factory=lambda: Path(universe_database_path()).expanduser()
    )
    pricing_base_url: str = DEFAULT_PRICING_BASE_URL
    # T-114: a run refuses to write a pricing_run row when code_version() is dirty
    # (uncommitted changes) -- its results would come from code HEAD alone can't
    # reproduce. Meant for a deliberate run from a work-in-progress checkout, not
    # routine use.
    allow_dirty: bool = False

    @classmethod
    def load(cls, env_file: str | os.PathLike[str] | None = None) -> Settings:
        load_dotenv(env_file, override=False)
        db_path = database_path()
        if not db_path:
            raise RuntimeError(f"missing required environment variable: {DB_ENV_VAR}")
        return cls(
            db_path=Path(db_path).expanduser(),
            universe_db_path=Path(universe_database_path()).expanduser(),
            pricing_base_url=os.environ.get("PRICING_BASE_URL", DEFAULT_PRICING_BASE_URL),
        )
