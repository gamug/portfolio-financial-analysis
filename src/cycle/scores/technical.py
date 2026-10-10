"""TECHNICAL score, version 2 (T-070): 12-1 momentum, 90-day volatility and 90-day max drawdown,
each standardized within its GICS sector on the cycle date.

Reads the latest ``price_observation`` row per asset (``priceobs-v2``) as of the cycle date.

- ``mom_12_1`` = ``close(t-21) / close(t-252) - 1`` -- twelve-month momentum that skips the most
  recent month (Jegadeesh & Titman 1993, *Journal of Finance* 48(1)), to avoid the one-month
  reversal. Higher is better. It is derived from the observation's ``momentum_21d`` and
  ``momentum_252d`` (``(1 + m252) / (1 + m21) - 1`` is exactly ``close(t-21) / close(t-252) - 1``),
  so ``price_observation`` stays the one source of every price signal; it needs 253 closes.
- ``realized_vol_90d``: lower is better. ``max_drawdown_90d``: less negative is better.
- Sector-Z: ``z = (x - sector mean) / sector sd`` among the assets of the same GICS sector that
  have the signal. A sector with fewer than ``MIN_SECTOR_NAMES`` such assets uses the whole
  cross-section's mean and sd instead -- at a 20-name cohort, sector-Z alone is degenerate.
  The sector is today's GICS sector (checklist L-03); the sd is the population sd, as in
  ``cycle.scores.normalize``; a signal with zero spread has z = 0.
- ``raw = 0.50 z_mom - 0.30 z_vol + 0.20 z_dd``, renormalized over the signals present (divided
  by the sum of the weights of the signals the asset has).
- Coverage floor (audit C5): no score when fewer than ``MIN_SIGNALS`` of the 3 signals exist.
  An asset without a TECHNICAL score is simply absent from the blend, which renormalizes over the
  components present (T-141).

The ``normalize`` step then maps ``raw`` to ``50 + 10 z`` across the cohort, as for every score.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field

VERSION = "technical-v2"
SCORE_TYPE = "TECHNICAL"

# signal -> weight in the raw blend (a "lower is better" signal enters with a negative sign)
_WEIGHTS: dict[str, float] = {
    "mom_12_1": 0.50,
    "realized_vol_90d": -0.30,
    "max_drawdown_90d": 0.20,
}
MIN_SECTOR_NAMES = 5
MIN_SIGNALS = 2


@dataclass
class RawScore:
    asset_id: int
    raw_value: float
    components: dict[str, float | None] = field(default_factory=dict)


def mom_12_1(obs: Mapping[str, float | None]) -> float | None:
    """``close(t-21) / close(t-252) - 1`` from the observation's 21- and 252-day momentum."""
    m21, m252 = obs.get("momentum_21d"), obs.get("momentum_252d")
    if m21 is None or m252 is None or m21 <= -1.0:
        return None
    return (1.0 + m252) / (1.0 + m21) - 1.0


def _signals(obs: Mapping[str, float | None]) -> dict[str, float | None]:
    return {
        "mom_12_1": mom_12_1(obs),
        "realized_vol_90d": obs.get("realized_vol_90d"),
        "max_drawdown_90d": obs.get("max_drawdown_90d"),
    }


def _mean_sd(values: list[float]) -> tuple[float, float]:
    return statistics.fmean(values), statistics.pstdev(values)


def sector_z(
    values: Mapping[int, float | None], sectors: Mapping[int, int | None]
) -> tuple[dict[int, float | None], dict[int, int]]:
    """``(z, group size)`` per asset for one signal.

    The group is the asset's sector when at least ``MIN_SECTOR_NAMES`` assets of that sector have
    the signal, else every asset that has it (the returned size says which). ``z`` is ``None``
    for an asset without the signal, and 0.0 where the group has no spread."""
    known = {a: v for a, v in values.items() if v is not None}
    by_sector: dict[int, list[int]] = {}
    for a in known:
        sid = sectors.get(a)
        if sid is not None:
            by_sector.setdefault(int(sid), []).append(a)
    whole = _mean_sd(list(known.values())) if known else None
    stats_by_sector = {
        sid: _mean_sd([known[a] for a in members])
        for sid, members in by_sector.items()
        if len(members) >= MIN_SECTOR_NAMES
    }
    z: dict[int, float | None] = {}
    size: dict[int, int] = {}
    for a, v in values.items():
        if v is None or whole is None:
            z[a] = None
            continue
        sid = sectors.get(a)
        if sid is not None and int(sid) in stats_by_sector:
            mean, sd = stats_by_sector[int(sid)]
            size[a] = len(by_sector[int(sid)])
        else:
            mean, sd = whole
            size[a] = len(known)
        z[a] = (v - mean) / sd if sd > 0 else 0.0
    return z, size


def compute(
    observations: Mapping[int, Mapping[str, float | None]],
    sectors: Mapping[int, int | None] | None = None,
) -> list[RawScore]:
    """*observations* maps asset_id -> the asset's latest price_observation row; *sectors* maps
    asset_id -> its GICS sector id (an asset without one is standardized against the whole
    cross-section). Assets with fewer than ``MIN_SIGNALS`` signals get no score."""
    if not observations:
        return []
    sectors = sectors or {}
    per_asset = {aid: _signals(obs) for aid, obs in observations.items()}
    z_by_signal: dict[str, dict[int, float | None]] = {}
    n_by_signal: dict[str, dict[int, int]] = {}
    for name in _WEIGHTS:
        z_by_signal[name], n_by_signal[name] = sector_z(
            {aid: sig[name] for aid, sig in per_asset.items()}, sectors
        )

    out: list[RawScore] = []
    for aid, signals in per_asset.items():
        num = den = 0.0
        present = 0
        parts: dict[str, float | None] = {}
        for name, weight in _WEIGHTS.items():
            z = z_by_signal[name][aid]
            parts[name] = signals[name]
            parts[f"z_{name}"] = z
            parts[f"n_{name}"] = float(n_by_signal[name][aid]) if aid in n_by_signal[name] else None
            if z is not None:
                num += weight * z
                den += abs(weight)
                present += 1
        if present < MIN_SIGNALS:
            continue
        out.append(RawScore(asset_id=aid, raw_value=num / den, components=parts))
    return out
