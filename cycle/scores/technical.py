"""TECHNICAL score: momentum / low-volatility / low-ATR / shallow-drawdown blend.

Proposed definition (roadmap step 6, user to refine). Consumes the latest
``price_observation`` per asset as of the cycle date.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cycle.scores.normalize import rank_pct

SCORE_TYPE = "TECHNICAL"

# (field, higher_is_better, weight)
_SIGNALS: list[tuple[str, bool, float]] = [
    ("momentum_63d", True, 0.35),
    ("momentum_21d", True, 0.15),
    ("realized_vol_90d", False, 0.20),
    ("atr_ratio", False, 0.15),
    ("max_drawdown_90d", True, 0.15),  # less negative is better -> higher_is_better
]


@dataclass
class RawScore:
    asset_id: int
    raw_value: float
    components: dict[str, float | None] = field(default_factory=dict)


def _atr_ratio(obs: dict[str, float | None]) -> float | None:
    atr, close = obs.get("atr_14"), obs.get("close")
    if atr is None or not close:
        return None
    return atr / close


def compute(observations: dict[int, dict[str, float | None]]) -> list[RawScore]:
    """*observations* maps asset_id -> the asset's latest price_observation row."""
    asset_ids = list(observations)
    if not asset_ids:
        return []
    enriched = {aid: {**obs, "atr_ratio": _atr_ratio(obs)} for aid, obs in observations.items()}
    pct_by_signal: dict[str, list[float | None]] = {}
    for name, higher, _w in _SIGNALS:
        pct_by_signal[name] = rank_pct(
            [enriched[aid].get(name) for aid in asset_ids], higher_is_better=higher
        )

    out: list[RawScore] = []
    for i, aid in enumerate(asset_ids):
        parts: dict[str, float | None] = {}
        num = den = 0.0
        for name, _higher, weight in _SIGNALS:
            p = pct_by_signal[name][i]
            parts[name] = p
            if p is not None:
                num += weight * p
                den += weight
        raw = 100.0 * (num / den) if den else 50.0
        out.append(RawScore(asset_id=aid, raw_value=raw, components=parts))
    return out
