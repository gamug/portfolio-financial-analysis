"""Config for the entity-resolution step -- the shared DB plus the news DB path."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from portfolio_common.kg_schema.env import DB_ENV_VAR, database_path, universe_database_path
from pydantic import BaseModel, Field

DEFAULT_NEWS_DB = "/workspaces/thesis/data/urls.db"


class Settings(BaseModel):
    db_path: Path
    news_db_path: Path
    universe_db_path: Path = Field(
        default_factory=lambda: Path(universe_database_path()).expanduser()
    )

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
