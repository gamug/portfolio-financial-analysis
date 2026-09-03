"""Runtime configuration, sourced from the project ``.env`` file."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from kg_schema.env import DB_ENV_VAR, database_path, universe_database_path

# The EDGAR gateway mounts the SEC service under ``/edgar`` and the service itself
# prefixes its routes with ``/edgar`` again, hence the doubled segment.
DEFAULT_EDGAR_BASE_URL = "http://host.docker.internal:8000/edgar/edgar"

_REQUIRED_LLM_VARS = ("LLM_API_KEY", "LLM_MODEL", "LLM_URL")


class Settings(BaseModel):
    """Everything the agent needs to reach its data stores and the LLM."""

    db_path: Path
    universe_db_path: Path = Field(
        default_factory=lambda: Path(universe_database_path()).expanduser()
    )
    llm_api_key: str
    llm_model: str
    llm_url: str
    edgar_base_url: str = DEFAULT_EDGAR_BASE_URL

    @classmethod
    def load(cls, env_file: str | os.PathLike[str] | None = None) -> Settings:
        """Load settings from the environment, populating it from ``.env`` first."""
        load_dotenv(env_file, override=False)
        db_path = database_path()
        missing = [name for name in _REQUIRED_LLM_VARS if not os.environ.get(name)]
        if db_path is None:
            missing.insert(0, DB_ENV_VAR)
        if missing or db_path is None:
            raise RuntimeError(f"missing required environment variables: {', '.join(missing)}")
        return cls(
            db_path=Path(db_path).expanduser(),
            universe_db_path=Path(universe_database_path()).expanduser(),
            llm_api_key=os.environ["LLM_API_KEY"],
            llm_model=os.environ["LLM_MODEL"],
            llm_url=os.environ["LLM_URL"],
            edgar_base_url=os.environ.get("EDGAR_BASE_URL", DEFAULT_EDGAR_BASE_URL),
        )
