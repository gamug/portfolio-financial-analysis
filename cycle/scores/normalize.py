"""Turn a cohort of raw scores into a 0-100 ``normalized_score``."""

from __future__ import annotations

import statistics


def _winsorize(values: list[float], frac: float) -> list[float]:
    if frac <= 0 or len(values) < 5:  # noqa: PLR2004 - too few to trim meaningfully
        return values
    ordered = sorted(values)
    k = max(1, int(len(ordered) * frac))
    lo, hi = ordered[k], ordered[-1 - k]
    return [min(max(v, lo), hi) for v in values]


def cross_sectional_z(values: list[float], *, winsor: float = 0.02) -> list[float]:
    """z-score *values* against their own cohort (winsorized). Zeros if degenerate."""
    if not values:
        return []
    trimmed = _winsorize(values, winsor)
    mean = statistics.fmean(trimmed)
    sd = statistics.pstdev(trimmed)
    if sd == 0:
        return [0.0 for _ in values]
    return [(v - mean) / sd for v in values]


def z_to_score(z: float) -> float:
    """Map a z-score to a bounded 0-100 point score (50 = cohort average)."""
    return max(0.0, min(100.0, 50.0 + 10.0 * z))


def normalized_scores(raw: list[float], *, winsor: float = 0.02) -> list[float]:
    return [z_to_score(z) for z in cross_sectional_z(raw, winsor=winsor)]


def rank_pct(values: list[float | None], *, higher_is_better: bool = True) -> list[float | None]:
    """Cross-sectional percentile rank in ``[0, 1]``; ``None`` in -> ``None`` out."""
    known = sorted(v for v in values if v is not None)
    if len(known) < 2:  # noqa: PLR2004
        return [None if v is None else 0.5 for v in values]
    span = len(known) - 1
    out: list[float | None] = []
    for v in values:
        if v is None:
            out.append(None)
            continue
        # index of the first element >= v, averaged for ties
        lo = next(i for i, k in enumerate(known) if k >= v)
        hi = next((i for i, k in enumerate(known) if k > v), len(known)) - 1
        pct = (lo + hi) / 2 / span
        out.append(pct if higher_is_better else 1.0 - pct)
    return out
