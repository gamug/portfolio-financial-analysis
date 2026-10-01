"""Split-shaped jumps in a stored close series (T-131).

The gateway returns history split-adjusted *as of the fetch date*, so a refresh that rewrites only
its own window leaves the older stored rows on the previous basis and the series jumps by the split
ratio where the two meet (APH 2:1 on 2026-09-03 showed a fake -51%; MNST's stored closes alternated
between the two bases). A correct adjusted series has none. :func:`split_shaped_jumps` finds them;
the pipeline refuses to store a series that has one it cannot explain.

The ratio set and tolerance are the same ones ``verify_pilot.py`` counts, so the guard and the
acceptance check cannot disagree. A real one-day move of exactly x2 or x0.5 is rare enough that the
guard's cost is an operator override (``--allow-split-jumps``), not a silent bad series.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise

from pricing_agent.pricing_client import Candle

#: Day-over-day close ratios a split produces (2-for-1 halves the price; a reverse split doubles it).
SPLIT_RATIOS: tuple[float, ...] = (2.0, 0.5, 3.0, 1 / 3, 4.0, 0.25, 10.0, 0.1)
SPLIT_RATIO_TOLERANCE = 0.03  # relative, as in verify_pilot.py


@dataclass(frozen=True)
class Jump:
    """A split-shaped close-to-close move: *ratio* = close(date) / close(prev_date)."""

    prev_date: str
    date: str
    ratio: float


def is_split_shaped(ratio: float) -> bool:
    return any(abs(ratio - r) < r * SPLIT_RATIO_TOLERANCE for r in SPLIT_RATIOS)


def split_shaped_jumps(candles: Iterable[Candle]) -> list[Jump]:
    """Every consecutive-bar close ratio in date order that looks like a split."""
    ordered = sorted(candles, key=lambda c: c.date)
    jumps: list[Jump] = []
    for prev, cur in pairwise(ordered):
        if prev.close > 0 and cur.close > 0 and is_split_shaped(cur.close / prev.close):
            jumps.append(Jump(prev.date, cur.date, cur.close / prev.close))
    return jumps


def matches_recorded_split(jump: Jump, split_values: Iterable[float]) -> bool:
    """True when *jump*'s ratio is a recorded split's ratio, either way round (a 2-for-1 split
    is value 2.0; the unadjusted-to-adjusted seam shows 0.5, a reverse seam 2.0)."""
    for value in split_values:
        if value > 0 and any(
            math.isclose(jump.ratio, r, rel_tol=SPLIT_RATIO_TOLERANCE) for r in (value, 1 / value)
        ):
            return True
    return False


def describe(jumps: Iterable[Jump], *, limit: int = 5) -> str:
    items = list(jumps)
    shown = ", ".join(f"{j.prev_date}->{j.date} x{j.ratio:.3f}" for j in items[:limit])
    more = f" (+{len(items) - limit} more)" if len(items) > limit else ""
    return shown + more
