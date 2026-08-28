"""Turn a ranked, veto-filtered asset list into target portfolio weights.

Order of operations: HARD vetoes already removed -> take the top N -> assign raw
weights by scheme -> apply name and sector caps -> renormalize.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    asset_id: int
    blended_score: float
    sector_id: int | None
    realized_vol_90d: float | None


def _raw_weights(cands: list[Candidate], scheme: str) -> dict[int, float]:
    if scheme == "equal" or not cands:
        return {c.asset_id: 1.0 for c in cands}
    if scheme == "inverse_vol":
        inv = {c.asset_id: (1.0 / c.realized_vol_90d if c.realized_vol_90d else 0.0) for c in cands}
        if any(inv.values()):
            return inv
        return {c.asset_id: 1.0 for c in cands}
    # score_proportional (default): shift so the min score maps to a small positive weight
    lo = min(c.blended_score for c in cands)
    return {c.asset_id: (c.blended_score - lo) + 1.0 for c in cands}


def _cap_names(weights: dict[int, float], cap: float) -> dict[int, float]:
    """Water-fill: pin over-cap names at *cap*, spread the rest among the uncapped."""
    if not weights or cap * len(weights) < 1.0 - 1e-9:
        return _normalize(weights)  # cap too tight to sum to 1; best effort
    w = dict(weights)
    capped: set[int] = set()
    for _ in range(len(w) + 1):
        over = [aid for aid, v in w.items() if v > cap + 1e-12 and aid not in capped]
        if not over:
            break
        capped.update(over)
        for aid in over:
            w[aid] = cap
        used = sum(w[aid] for aid in capped)
        free = [aid for aid in w if aid not in capped]
        free_sum = sum(w[aid] for aid in free)
        if free_sum <= 0:
            break
        scale = (1.0 - used) / free_sum
        for aid in free:
            w[aid] *= scale
    return w


def _normalize(weights: dict[int, float]) -> dict[int, float]:
    total = sum(weights.values())
    if total <= 0:
        n = len(weights)
        return {aid: 1.0 / n for aid in weights} if n else {}
    return {aid: w / total for aid, w in weights.items()}


def target_weights(
    cands: list[Candidate],
    *,
    top_n: int,
    scheme: str = "score_proportional",
    max_name_weight: float = 0.10,
    max_sector_weight: float = 0.30,
) -> dict[int, float]:
    chosen = sorted(cands, key=lambda c: c.blended_score, reverse=True)[:top_n]
    if not chosen:
        return {}
    sector_of = {c.asset_id: c.sector_id for c in chosen}
    weights = _normalize(_raw_weights(chosen, scheme))

    # Alternate name and sector caps until both hold (or we give up and return the
    # closest feasible mix). Each cap step redistributes only to non-capped names.
    for _ in range(8):
        weights = _cap_names(weights, max_name_weight)
        before = dict(weights)
        weights = _cap_sectors(weights, sector_of, max_sector_weight)
        if (
            max(weights.values()) <= max_name_weight + 1e-9
            and _max_sector(weights, sector_of) <= max_sector_weight + 1e-9
        ):
            break
        if weights == before:
            break
    return weights


def _max_sector(weights: dict[int, float], sector_of: dict[int, int | None]) -> float:
    tot: dict[int | None, float] = {}
    for aid, w in weights.items():
        tot[sector_of[aid]] = tot.get(sector_of[aid], 0.0) + w
    return max(tot.values()) if tot else 0.0


def _cap_sectors(
    weights: dict[int, float], sector_of: dict[int, int | None], cap: float
) -> dict[int, float]:
    tot: dict[int | None, float] = {}
    for aid, wt in weights.items():
        tot[sector_of[aid]] = tot.get(sector_of[aid], 0.0) + wt
    over = {s for s, t in tot.items() if t > cap + 1e-12}
    if not over or all(sector_of[aid] in over for aid in weights):
        return _normalize(weights)
    w = dict(weights)
    for aid in w:
        if sector_of[aid] in over:
            w[aid] *= cap / tot[sector_of[aid]]
    used = sum(v for aid, v in w.items() if sector_of[aid] in over)
    free = [aid for aid in w if sector_of[aid] not in over]
    free_sum = sum(w[aid] for aid in free)
    if free_sum > 0:
        scale = (1.0 - used) / free_sum
        for aid in free:
            w[aid] *= scale
    return w
