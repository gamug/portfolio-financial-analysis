"""Config for the selection / monitoring cycle."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

_DEFAULT_WEIGHTS = {"FUNDAMENTAL": 0.4, "QUANTITATIVE": 0.3, "TECHNICAL": 0.2, "SEMANTIC": 0.1}


class CycleSettings(BaseModel):
    """Everything a cycle needs: the shared DB, optional LLM creds, and the knobs."""

    db_path: Path
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

    @classmethod
    def load(cls, env_file: str | os.PathLike[str] | None = None) -> CycleSettings:
        load_dotenv(env_file, override=False)
        db_path = os.environ.get("KG_FINANTIAL_DB")
        if not db_path:
            raise RuntimeError("missing required environment variable: KG_FINANTIAL_DB")
        return cls(
            db_path=Path(db_path).expanduser(),
            llm_api_key=os.environ.get("LLM_API_KEY"),
            llm_model=os.environ.get("LLM_MODEL"),
            llm_url=os.environ.get("LLM_URL"),
        )
