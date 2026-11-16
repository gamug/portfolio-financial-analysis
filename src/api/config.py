"""Runtime config for the API, sourced from ``.env`` -- same pattern as the agents."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from kg_schema.env import DB_ENV_VAR, database_path, universe_database_path

DEFAULT_HOST = "0.0.0.0"  # noqa: S104 - dev container binds all interfaces
DEFAULT_PORT = 8010  # the data-mining gateway owns :8000-:8005; keep clear

NEWS_DB_ENV_VAR = "KG_NEWS_DB"
# A copy of entity_resolution.config.DEFAULT_NEWS_DB, not an import: importing that
# package runs its __init__, which pulls its pipeline (run code) onto api/'s import
# path (T-011 D2). tests/test_orchestrator.py pins the two together so they cannot drift.
DEFAULT_NEWS_DB = "/workspaces/thesis/data/urls.db"


def news_database_path(explicit: str | None = None) -> str:
    """The news DB path exactly as ``entity_resolution`` resolves it: *explicit*, else
    ``KG_NEWS_DB``, else :data:`DEFAULT_NEWS_DB`. Always a usable path."""
    if explicit:
        return explicit
    return os.environ.get(NEWS_DB_ENV_VAR) or DEFAULT_NEWS_DB


class ApiSettings(BaseModel):
    """Where the API reads from and how it binds. The read paths open DBs read-only; the
    run endpoints (T-011) start the agent CLIs as subprocesses from the paths here."""

    db_path: Path
    universe_db_path: Path
    news_db_path: Path = Field(default_factory=lambda: Path(news_database_path()).expanduser())
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    title: str = "portfolio-financial-analysis API"
    root_path: str = ""  # set when mounted behind a reverse proxy / gateway

    @classmethod
    def load(cls, env_file: str | os.PathLike[str] | None = None) -> ApiSettings:
        load_dotenv(env_file, override=False)
        db = database_path()
        if not db:
            raise RuntimeError(f"missing required environment variable: {DB_ENV_VAR}")
        return cls(
            db_path=Path(db).expanduser(),
            universe_db_path=Path(universe_database_path()).expanduser(),
            news_db_path=Path(news_database_path()).expanduser(),
            host=os.environ.get("API_HOST", DEFAULT_HOST),
            port=int(os.environ.get("API_PORT", str(DEFAULT_PORT))),
            root_path=os.environ.get("API_ROOT_PATH", ""),
        )
