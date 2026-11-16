"""Turn a cohort of raw scores into a 0-100 ``normalized_score``."""

from __future__ import annotations

import statistics
from typing import Final

# DQ-02: winsorize 0.5% per tail -- ``floor(0.005 x n)`` values each side -- and nothing at all
# when the cohort is smaller than :data:`MIN_FOR_WINSOR`. The count is a **floor**, never
# ``max(1, ...)``: a 20-name cohort has no 0.5% tail to trim, and forcing one value per tail there
# would winsorize 5% of it.
DEFAULT_WINSOR: Final[float] = 0.005
MIN_FOR_WINSOR: Final[int] = 200


def winsorize(values: list[float], frac: float = DEFAULT_WINSOR) -> list[float]:
    """DQ-02: trim ``floor(frac x n)`` values per tail, nothing when ``n < MIN_FOR_WINSOR``.

    At n = 495 this trims two values per tail (``floor(0.005 x 495) = 2``); a cohort below
    :data:`MIN_FOR_WINSOR` is returned untouched. Both score normalization (:func:`cross_sectional_z`)
    and VALORIZATION's per-measure ranking (T-071 part 2b, D2) winsorize with this one function."""
    if frac <= 0 or len(values) < MIN_FOR_WINSOR:
        return values
    ordered = sorted(values)
    k = int(len(ordered) * frac)
    if k <= 0:
        return values
    lo, hi = ordered[k], ordered[-1 - k]
    return [min(max(v, lo), hi) for v in values]


def cross_sectional_z(values: list[float], *, winsor: float = DEFAULT_WINSOR) -> list[float]:
    """z-score *values* against their own cohort (winsorized). Zeros if degenerate."""
    if not values:
        return []
    trimmed = winsorize(values, winsor)
    mean = statistics.fmean(trimmed)
    sd = statistics.pstdev(trimmed)
    if sd == 0:
        return [0.0 for _ in values]
    return [(v - mean) / sd for v in values]


def z_to_score(z: float) -> float:
    """Map a z-score to a bounded 0-100 point score (50 = cohort average)."""
    return max(0.0, min(100.0, 50.0 + 10.0 * z))


def normalized_scores(raw: list[float], *, winsor: float = DEFAULT_WINSOR) -> list[float]:
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
