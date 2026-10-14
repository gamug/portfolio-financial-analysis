"""The FUNDAMENTAL pillar score's stored contract (T-073, D4) -- readable by every package.

`fundamental_agent` computes the score: the model rates each pillar, the code weighs and totals
them, and the row carries the block (`pillars`, `pillar_weights`, `pillar_weights_version`,
`coverage`) inside `score_snapshot.inputs_json`. `cycle` reads that block back to recompute a row's
total under its own weights, without importing `fundamental_agent` -- the same reason the
applicability matrix lives in `kg_schema` (T-071 part 1). Only the contract lives here: the pillar
membership, the statuses, the total and the fallback are `fundamental_agent`'s.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# The four pillars, in the order they are reported (T-073, D1: profitability, financial_strength,
# cash_flow, growth). `fundamental_agent.pillars.PILLARS` carries each one's metrics.
PILLAR_NAMES: tuple[str, ...] = ("profitability", "financial_strength", "cash_flow", "growth")

# D2/D4: equal weights over the four pillars -- a declared calibration with no loaded source. They
# live in one setting; changing them costs no LLM call, because `cycle` recomputes a row's total
# from the pillar ratings that row stored.
DEFAULT_PILLAR_WEIGHTS: dict[str, float] = {name: 0.25 for name in PILLAR_NAMES}
# The version tag stored beside the weights, so a row says which calibration produced it.
PILLAR_WEIGHTS_VERSION = "pillar-weights-equal-v1"

# D4: the block's keys inside `score_snapshot.inputs_json` (no schema change). The row's flat
# metrics keep their own `group.metric` keys beside it.
PILLARS_KEY = "pillars"
PILLAR_WEIGHTS_KEY = "pillar_weights"
PILLAR_WEIGHTS_VERSION_KEY = "pillar_weights_version"
COVERAGE_KEY = "coverage"


def total_from_block(block: Mapping[str, Any], weights: Mapping[str, float]) -> float | None:
    """A stored block's total under *weights*, or ``None`` when the row carries no pillar ratings.

    The total is the weighted sum over the pillars the row *rated*, normalized by *weights*' own
    sum over those pillars -- the rule the code totals with (`pillars.total_from_ratings`), so the
    result stays on the scale D2 declares and only the relative weights change. A rated pillar
    *weights* does not name contributes nothing.
    """
    stored = block.get(PILLARS_KEY)
    if not isinstance(stored, Mapping):
        return None
    rated = {
        pillar: entry["rating"]
        for pillar, entry in stored.items()
        if isinstance(entry, Mapping) and isinstance(entry.get("rating"), (int, float))
    }
    if not rated:
        return None
    total = sum(float(weights.get(pillar, 0.0)) for pillar in rated)
    if total <= 0:
        return None
    return round(
        sum(float(weights.get(pillar, 0.0)) * float(rating) for pillar, rating in rated.items())
        / total,
        1,
    )


def weights_differ(stored: Mapping[str, Any], weights: Mapping[str, float]) -> bool:
    """Whether a row's stored ``pillar_weights`` differ from *weights* (D4's recompute trigger).

    ``False`` for a row with no weights recorded -- a legacy row, or one written before T-073 --
    which keeps its own ``raw_value``."""
    recorded = stored.get(PILLAR_WEIGHTS_KEY)
    if not isinstance(recorded, Mapping):
        return False
    return {str(k): float(v) for k, v in recorded.items()} != {
        str(k): float(v) for k, v in weights.items()
    }
