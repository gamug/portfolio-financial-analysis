"""QuantSettings.load() reads KG_FINANCIAL_DB; overrides via model_copy."""

from __future__ import annotations

from pathlib import Path

import pytest

from quant.config import QuantSettings


def test_load_requires_db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KG_FINANCIAL_DB", raising=False)
    monkeypatch.delenv("KG_FINANTIAL_DB", raising=False)
    with pytest.raises(RuntimeError, match="KG_FINANCIAL_DB"):
        QuantSettings.load(env_file=Path("/nonexistent/.env"))


def test_load_reads_db_and_pricing_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KG_FINANCIAL_DB", "/tmp/x/financial.db")
    monkeypatch.setenv("PRICING_BASE_URL", "http://gw:9/pricing")
    s = QuantSettings.load(env_file=Path("/nonexistent/.env"))
    assert s.db_path == Path("/tmp/x/financial.db")
    assert s.pricing_base_url == "http://gw:9/pricing"
    # defaults land as documented
    assert s.headline_objective == "min_var"
    assert s.objectives == ["min_var", "tangency", "target_vol"]
    assert s.max_name_weight == 0.05
    assert s.solver == "CLARABEL"


def test_model_copy_override() -> None:
    s = QuantSettings(db_path=Path(":memory:"))
    s2 = s.model_copy(update={"db_path": Path("/other.db"), "frontier_k": 5})
    assert s2.db_path == Path("/other.db")
    assert s2.frontier_k == 5
    assert s.frontier_k == 15  # original untouched
