"""The objective registry resolves names and rejects unknown ones."""

from __future__ import annotations

import numpy as np
import pytest

from quant.objective import (
    OBJECTIVES,
    ObjectiveContext,
    objective_param,
    resolve_objectives,
)
from quant.optimize import Constraints


def test_standing_family() -> None:
    assert set(OBJECTIVES) == {"min_var", "tangency", "target_vol"}


def test_resolve_keeps_order_and_drops_frontier() -> None:
    got = resolve_objectives(["tangency", "frontier", "min_var"])
    assert [name for name, _ in got] == ["tangency", "min_var"]


def test_resolve_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unknown objective"):
        resolve_objectives(["min_var", "sortino"])


def test_builders_run_and_param_reported() -> None:
    rng = np.random.default_rng(0)
    a = rng.normal(size=(5, 5))
    sigma = a @ a.T / 5 + np.eye(5) * 0.05
    mu = np.linspace(0.03, 0.12, 5)
    ctx = ObjectiveContext(
        sigma=sigma,
        mu=mu,
        rf=0.02,
        constraints=Constraints(max_name_weight=None, max_sector_weight=None),
        target_volatility=0.25,
        solver="CLARABEL",
    )
    for _name, build in resolve_objectives(["min_var", "tangency", "target_vol"]):
        res = build(ctx)
        assert res.weights.shape == (5,)
        assert res.weights.sum() == pytest.approx(1.0, abs=1e-5)
    assert objective_param("target_vol", ctx) == 0.25
    assert objective_param("min_var", ctx) is None
