"""VALORIZATION score: a cross-sectional value / quality / size factor blend.

Proposed definition (roadmap step 6, user to refine). Consumes the latest
``fundamental_metrics`` per asset plus a market-cap estimate.
"""

from __future__ import annotations

from collections.abc import Callable

from cycle.scores.normalize import rank_pct
from cycle.scores.technical import RawScore

SCORE_TYPE = "VALORIZATION"

# factor -> [(metric_key, higher_is_better), ...]  (metric_key = "group.name" or a synthetic)
_FACTORS: dict[str, tuple[float, list[tuple[str, bool]]]] = {
    "value": (
        0.45,
        [
            ("valuation.free_cash_flow_yield", True),
            ("valuation.enterprise_fcf_yield", True),
            ("earnings_yield", True),
        ],
    ),
    "quality": (
        0.40,
        [
            ("profitability.return_on_equity", True),
            ("roic.return_on_invested_capital", True),
            ("cashflow.free_cash_flow_margin", True),
            ("leverage.debt_to_equity", False),
        ],
    ),
    "size": (0.15, [("neg_log_market_cap", True)]),
}


def _leverage_for_ranking(value: float | None) -> float | None:
    """A negative debt/equity means negative book equity, not low leverage
    (C2, docs/model_fixes.md) -- without this, ``rank_pct``'s
    ``higher_is_better=False`` inversion would reward it as the BEST
    leverage in the cohort instead of the worst."""
    if value is not None and value < 0:
        return float("inf")
    return value


# metric_key -> a transform applied to each row's raw value before ranking,
# for a metric whose sign can flip in a way that would otherwise invert its
# percentile the wrong way (see _leverage_for_ranking).
_VALUE_TRANSFORMS: dict[str, Callable[[float | None], float | None]] = {
    "leverage.debt_to_equity": _leverage_for_ranking,
}


def _factor_score(
    rows: list[dict[str, float | None]], keys: list[tuple[str, bool]]
) -> list[float | None]:
    """Average of the available sub-metric percentiles for each row."""
    n = len(rows)
    pct_lists = []
    for key, higher in keys:
        transform = _VALUE_TRANSFORMS.get(key)
        values = [r.get(key) for r in rows]
        if transform is not None:
            values = [transform(v) for v in values]
        pct_lists.append(rank_pct(values, higher_is_better=higher))
    out: list[float | None] = []
    for i in range(n):
        vals = [v for v in (pl[i] for pl in pct_lists) if v is not None]
        out.append(sum(vals) / len(vals) if vals else None)
    return out


def compute(metrics: dict[int, dict[str, float | None]]) -> list[RawScore]:
    """*metrics* maps asset_id -> a dict of ``group.name`` metric values plus
    ``earnings_yield`` and ``neg_log_market_cap`` synthetics."""
    asset_ids = list(metrics)
    if not asset_ids:
        return []
    rows = [metrics[aid] for aid in asset_ids]
    factor_pcts = {name: _factor_score(rows, keys) for name, (_w, keys) in _FACTORS.items()}
    out: list[RawScore] = []
    for i, aid in enumerate(asset_ids):
        parts: dict[str, float | None] = {}
        num = den = 0.0
        for name, (weight, _keys) in _FACTORS.items():
            p = factor_pcts[name][i]
            parts[name] = p
            if p is not None:
                num += weight * p
                den += weight
        raw = 100.0 * (num / den) if den else 50.0
        out.append(RawScore(asset_id=aid, raw_value=raw, components=parts))
    return out
