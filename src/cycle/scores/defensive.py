"""The CHAMPION grid: five blends and five defensive tilts (preregistration §3).

Pure functions for the tilt arithmetic, plus the two readers that build the market index and
the per-asset return windows from ``quant_return_daily``. The cycle applies a tilt to the blended
ranking score at the close of a decision date; ``T0`` is a no-op and reproduces the plain ranking
exactly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

from portfolio_common.db import Database

from kg_schema import queries, trading_calendar

#: The five blends of FUNDAMENTAL / TECHNICAL / VALORIZATION weights (preregistration §3).
BLENDS: Final[dict[str, dict[str, float]]] = {
    "B0": {"FUNDAMENTAL": 1 / 3, "TECHNICAL": 1 / 3, "VALORIZATION": 1 / 3},
    "B1": {"FUNDAMENTAL": 0.50, "TECHNICAL": 0.25, "VALORIZATION": 0.25},
    "B2": {"FUNDAMENTAL": 0.60, "TECHNICAL": 0.20, "VALORIZATION": 0.20},
    "B3": {"FUNDAMENTAL": 0.45, "TECHNICAL": 0.15, "VALORIZATION": 0.40},
    "B4": {"FUNDAMENTAL": 0.45, "TECHNICAL": 0.40, "VALORIZATION": 0.15},
}

#: The market-return and beta windows (preregistration §3).
R_SHORT = 63
R_LONG = 126
BETA_WINDOW = 252
BETA_MIN_PAIRS = 126


@dataclass(frozen=True)
class TiltSpec:
    """One row of the tilt table: a trigger rule and its weight."""

    trigger: str  # never | r63_negative | min_r63_r126_below | always
    w: float


TILTS: Final[dict[str, TiltSpec]] = {
    "T0": TiltSpec("never", 0.0),
    "R1": TiltSpec("r63_negative", 0.25),
    "R2": TiltSpec("r63_negative", 0.40),
    "R3": TiltSpec("min_r63_r126_below", 0.35),
    "R4": TiltSpec("always", 0.15),
}
#: R3's threshold (preregistration §3).
R3_THRESHOLD = -0.02


def cumulative_return(values: list[float]) -> float:
    """``prod(1 + r) - 1`` over the window, in order."""
    level = 1.0
    for value in values:
        level *= 1.0 + value
    return level - 1.0


def r63_r126(market: dict[str, float], as_of: str) -> tuple[float | None, float | None]:
    """The market's cumulative return over the last 63 / 126 sessions ending at *as_of*.

    *market* is session -> simple return; only sessions ``<= as_of`` enter, so the read is
    causal. ``None`` when the window has no observations (the trigger then cannot fire)."""
    sessions = sorted(day for day in market if day <= as_of)
    if not sessions:
        return None, None
    short = (
        cumulative_return([market[s] for s in sessions[-R_SHORT:]])
        if len(sessions) >= R_SHORT
        else None
    )
    long = (
        cumulative_return([market[s] for s in sessions[-R_LONG:]])
        if len(sessions) >= R_LONG
        else None
    )
    return short, long


def trigger_fires(tilt: str, r63: float | None, r126: float | None) -> bool:
    """Whether *tilt*'s trigger holds at a decision date (preregistration §3)."""
    spec = TILTS[tilt]
    if spec.trigger == "never":
        return False
    if spec.trigger == "always":
        return True
    if spec.trigger == "r63_negative":
        return r63 is not None and r63 < 0.0
    if spec.trigger == "min_r63_r126_below":
        return r63 is not None and r126 is not None and min(r63, r126) < R3_THRESHOLD
    raise ValueError(f"unknown tilt trigger {spec.trigger!r}")


def beta(asset_returns: list[float], market_returns: list[float]) -> float | None:
    """``cov(r_i, r_m) / var(r_m)`` with at least :data:`BETA_MIN_PAIRS` paired returns.

    Population moments (``ddof=0``) are the natural estimator for a beta over the observed
    window; a degenerate market variance returns ``None`` (no defensive score)."""
    pairs = [
        (asset, market)
        for asset, market in zip(asset_returns, market_returns, strict=True)
        if math.isfinite(asset) and math.isfinite(market)
    ]
    if len(pairs) < BETA_MIN_PAIRS:
        return None
    mean_a = math.fsum(p[0] for p in pairs) / len(pairs)
    mean_m = math.fsum(p[1] for p in pairs) / len(pairs)
    var_m = math.fsum((p[1] - mean_m) ** 2 for p in pairs) / len(pairs)
    if var_m <= 0.0:
        return None
    cov = math.fsum((p[0] - mean_a) * (p[1] - mean_m) for p in pairs) / len(pairs)
    return cov / var_m


def defensive_scores(betas: dict[int, float]) -> dict[int, float]:
    """``S_def = 50 + 10 * z(-beta)`` over the names that have a beta (DQ-02 winsorizing)."""
    from cycle.scores.normalize import normalized_scores  # noqa: PLC0415 - one-way import

    if not betas:
        return {}
    ids = sorted(betas)
    values = normalized_scores([-float(betas[asset_id]) for asset_id in ids])
    return {asset_id: value for asset_id, value in zip(ids, values, strict=True)}


def tilted_scores(
    blended: dict[int, float],
    betas: dict[int, float],
    tilt: str,
    fired: bool,
) -> dict[int, float]:
    """The blended score after the tilt (preregistration §3).

    A name with no beta keeps its blended score; ``T0`` / an unfired trigger is a no-op."""
    spec = TILTS[tilt]
    if not fired or spec.w == 0.0 or not betas:
        return dict(blended)
    scores = defensive_scores(betas)
    out = dict(blended)
    for asset_id, s_def in scores.items():
        if asset_id in out:
            out[asset_id] = (1.0 - spec.w) * out[asset_id] + spec.w * s_def
    return out


def market_index_returns(
    conn: Database,
    universe_conn: Database,
    sessions: list[str],
    *,
    engine_version: str,
) -> dict[str, float]:
    """The equal-weight return of ``members_asof(s - 1)`` priced on *s*.

    The ``SP500_EW_INTERNAL`` definition: the mean of the member assets' simple total returns
    on *s* (a member with no return that session is not priced and drops out of that day's
    mean). Reads only ``quant_return_daily`` at *engine_version* and the read-only universe;
    no lookahead: the membership is the one covering the session before *s*.
    """
    if not sessions:
        return {}
    rows = conn.execute(
        "SELECT asset_id, obs_date, tr_log_return FROM quant_return_daily "
        "WHERE engine_version = ? AND obs_date >= ? AND obs_date <= ? "
        "AND tr_log_return IS NOT NULL ORDER BY obs_date",
        (engine_version, sessions[0], sessions[-1]),
    ).fetchall()
    by_day: dict[str, dict[int, float]] = {}
    for row in rows:
        by_day.setdefault(str(row["obs_date"]), {})[int(row["asset_id"])] = math.expm1(
            float(row["tr_log_return"])
        )
    out: dict[str, float] = {}
    for session in sessions:
        as_of = trading_calendar.last_session_on_or_before(
            date.fromisoformat(session) - timedelta(days=1)
        ).isoformat()
        members = queries.members_asof(universe_conn, as_of)
        mapping, _missing = queries.resolve_asset_ids(
            conn, [(m.symbol, m.cik) for m in members], as_of=as_of
        )
        day = by_day.get(session, {})
        priced = [day[a] for a in set(mapping.values()) if a in day]
        out[session] = math.fsum(priced) / len(priced) if priced else 0.0
    return out
