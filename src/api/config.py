"""Runtime config for the API, sourced from ``.env`` -- same pattern as the agents."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

from kg_schema.env import DB_ENV_VAR, database_path, universe_database_path

DEFAULT_HOST = "0.0.0.0"  # noqa: S104 - dev container binds all interfaces
DEFAULT_PORT = 8010  # the data-mining gateway owns :8000-:8005; keep clear


class ApiSettings(BaseModel):
    """Where the API reads from and how it binds. It only ever opens DBs read-only."""

    db_path: Path
    universe_db_path: Path
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
            host=os.environ.get("API_HOST", DEFAULT_HOST),
            port=int(os.environ.get("API_PORT", str(DEFAULT_PORT))),
            root_path=os.environ.get("API_ROOT_PATH", ""),
        )
