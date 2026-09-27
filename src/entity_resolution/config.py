"""Config for the entity-resolution step -- the shared DB plus the news DB path."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from kg_schema.env import DB_ENV_VAR, database_path, universe_database_path

DEFAULT_NEWS_DB = "/workspaces/thesis/data/urls.db"


class Settings(BaseModel):
    db_path: Path
    news_db_path: Path
    universe_db_path: Path = Field(
        default_factory=lambda: Path(universe_database_path()).expanduser()
    )
    # T-114: a run refuses to write a cycle_run row when code_version() is dirty
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
            news_db_path=Path(os.environ.get("KG_NEWS_DB", DEFAULT_NEWS_DB)).expanduser(),
            universe_db_path=Path(universe_database_path()).expanduser(),
        )
