"""T-073 (D4): the pillar weights cost nothing to change.

A row stores the weights it was scored under; when `cycle`'s own differ, it recomputes that row's
total from the stored pillar ratings -- no LLM call, and the stored row is never rewritten. A row
written before T-073 (no pillar block) keeps its own `raw_value`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from portfolio_common.db import Database

from cycle import data, orchestrator
from cycle.config import CycleSettings
from cycle.scores.normalize import normalized_scores
from kg_schema.pillar_score import (
    DEFAULT_PILLAR_WEIGHTS,
    PILLAR_WEIGHTS_VERSION,
    total_from_block,
)

DAY = "2026-06-30"
# the original 0.30 / 0.25 / 0.25 / 0.20, mapped profitability / financial_strength / cash_flow /
# growth (D5's sensitivity set)
OTHER_WEIGHTS = {
    "profitability": 0.30,
    "financial_strength": 0.25,
    "cash_flow": 0.25,
    "growth": 0.20,
}
RATINGS = {"profitability": 80, "financial_strength": 60, "cash_flow": 40, "growth": 20}


def _settings(**kw: Any) -> CycleSettings:
    return CycleSettings(db_path=Path(":memory:"), top_n=3, **kw)


def _block(ratings: dict[str, int], weights: dict[str, float] | None = None) -> str:
    applied = weights or DEFAULT_PILLAR_WEIGHTS
    return json.dumps(
        {
            "pillars": {
                pillar: {"status": "present", "metrics": [], "rating": rating}
                for pillar, rating in ratings.items()
            },
            "pillar_weights": applied,
            "pillar_weights_version": PILLAR_WEIGHTS_VERSION,
            "coverage": {
                "pillars": 4,
                "scored": list(ratings),
                "empty": [],
                "absent_by_form": [],
                "not_applicable": [],
            },
        }
    )


def _give_aaa_a_block(conn: Database, ratings: dict[str, int] | None = None) -> Any:
    """AAA's FUNDAMENTAL row carries a pillar block, the way T-073's pipeline writes it: its
    ``raw_value`` *is* the block's total under the weights stored beside it."""
    block = _block(ratings or RATINGS)
    total = total_from_block(json.loads(block), DEFAULT_PILLAR_WEIGHTS)
    conn.execute(
        "UPDATE score_snapshot SET inputs_json = ?, raw_value = ?, normalized_score = ? "
        "WHERE asset_id = 1 AND score_type = 'FUNDAMENTAL'",
        (block, total, total),
    )
    conn.commit()
    return next(r for r in data.latest_fundamental_rows(conn, DAY) if int(r["asset_id"]) == 1)


def test_the_default_pillar_weights_are_equal_versioned_and_copied() -> None:
    weights = _settings().fundamental_pillar_weights
    assert weights == DEFAULT_PILLAR_WEIGHTS
    assert weights == {
        "profitability": 0.25,
        "financial_strength": 0.25,
        "cash_flow": 0.25,
        "growth": 0.25,
    }
    assert PILLAR_WEIGHTS_VERSION
    assert _settings().fundamental_pillar_weights is not DEFAULT_PILLAR_WEIGHTS


def test_two_weight_settings_over_the_same_row_give_the_two_totals(cycle_seed: Database) -> None:
    conn = cycle_seed
    row = _give_aaa_a_block(conn)
    assert data.fundamental_value_under(row, DEFAULT_PILLAR_WEIGHTS) == 50.0
    assert data.fundamental_value_under(row, OTHER_WEIGHTS) == 53.0

    # the stored row is unchanged: still its own raw_value and its own weights
    stored = conn.execute(
        "SELECT raw_value, normalized_score, inputs_json FROM score_snapshot "
        "WHERE asset_id = 1 AND score_type = 'FUNDAMENTAL'"
    ).fetchone()
    assert stored["raw_value"] == 50.0
    assert json.loads(stored["inputs_json"])["pillar_weights"] == DEFAULT_PILLAR_WEIGHTS


def test_a_row_with_no_pillar_block_keeps_its_raw_value(cycle_seed: Database) -> None:
    """A legacy row -- written before T-073, or one the code left unscored -- has no ratings to
    recompute from, so no weight setting can move it."""
    conn = cycle_seed
    row = next(r for r in data.latest_fundamental_rows(conn, DAY) if int(r["asset_id"]) == 1)
    assert row["inputs_json"] is None
    assert data.fundamental_value_under(row, OTHER_WEIGHTS) == float(row["raw_value"]) == 72.0


def test_a_cycle_under_other_weights_normalizes_the_recomputed_total(
    cycle_seed: Database,
) -> None:
    conn = cycle_seed
    _give_aaa_a_block(conn)
    orchestrator.run_monitoring(_settings(fundamental_pillar_weights=OTHER_WEIGHTS), DAY, conn=conn)

    # the run records the weights it ranked under
    recorded = json.loads(
        conn.execute(
            "SELECT params_json FROM cycle_run WHERE cycle_type = 'MONITORING' AND cycle_date = ?",
            (DAY,),
        ).fetchone()["params_json"]
    )
    assert recorded["fundamental_pillar_weights"] == OTHER_WEIGHTS

    # AAA is normalized on 53 (the recomputed total), not on its stored 72; the other four rows
    # have no pillar block and keep their raw values
    totals = [53.0, *[float(r["raw_value"]) for r in data.latest_fundamental_rows(conn, DAY)][1:]]
    expected = normalized_scores(totals)[0]
    got = conn.execute(
        "SELECT normalized_score FROM score_snapshot WHERE asset_id = 1 AND score_type = 'FUNDAMENTAL'"
    ).fetchone()["normalized_score"]
    assert got == expected

    # and the stored row is still untouched
    assert (
        conn.execute(
            "SELECT raw_value FROM score_snapshot WHERE asset_id = 1 AND score_type = 'FUNDAMENTAL'"
        ).fetchone()["raw_value"]
        == 50.0
    )
