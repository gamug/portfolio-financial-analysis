"""System v2: the SPY core-satellite blend and the market trend filter (V2_PLAN §2).

Pure arithmetic plus the one file reader. The core is a single holding (``SPY``, total return);
the trend filter reads SPY's split-adjusted close, never a return. The caller (the driver)
resolves the SPY asset id and hands the series in, so nothing here touches the database.

The SPY file is produced outside the repository; the engine refuses it unless its sha256 equals
the ``spy_tr_sha256`` declared in ``manifest.json`` beside it. Only aggregates of the series are
ever reported: no price or return value is written to a committed file.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: The core holding's ticker (the ETF, total return).
CORE_TICKER: Final = "SPY"
#: Faber's rule: the close against the mean of the last 210 session closes (10 months x 21).
TREND_WINDOW: Final = 210
FILTER_OFF: Final = "off"
FILTER_ON: Final = "on"
FILTERS: Final = (FILTER_OFF, FILTER_ON)
#: Phase 1 §1.3: the series the 210-session signal reads.  ``tr`` is the total-return index
#: (Faber 2013, p. 15); ``price`` is the split-adjusted close and exists only for the ladder.
FILTER_SERIES: Final = ("tr", "price")
#: The core's two sources (V3 plan §2b): the SPY ETF, or the MARKOWITZ_BASE target itself.
CORE_SOURCES: Final = ("spy", "base")
#: The manifest key the file's sha256 must equal.
MANIFEST_KEY: Final = "spy_tr_sha256"
#: A sha256 in hexadecimal.
SHA256_HEX_LENGTH: Final = 64


class CoreSeriesError(RuntimeError):
    """The SPY file or its manifest is missing, malformed, or does not match its declared sha."""


@dataclass(frozen=True)
class CoreSeries:
    """The verified SPY series: split-adjusted close and total return, keyed by session."""

    path: Path
    sha256: str
    declared_sha256: str
    close: Mapping[str, float]
    total_return: Mapping[str, float]

    @property
    def first_session(self) -> str:
        return min(self.close)

    @property
    def last_session(self) -> str:
        return max(self.close)


def sha256_file(path: Path) -> str:
    """The sha256 of *path*, read in chunks (the SPY file is small; the rule is general)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _declared_sha(series_path: Path) -> str:
    """The ``spy_tr_sha256`` declared in ``manifest.json`` beside *series_path*."""
    manifest_path = series_path.with_name("manifest.json")
    if not manifest_path.is_file():
        raise CoreSeriesError(f"core series manifest {manifest_path} does not exist")
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        raise CoreSeriesError(f"core series manifest {manifest_path} is not JSON: {exc}") from exc
    declared = manifest.get(MANIFEST_KEY)
    if not isinstance(declared, str) or len(declared) != SHA256_HEX_LENGTH:
        raise CoreSeriesError(f"core series manifest has no {MANIFEST_KEY}: {manifest_path}")
    return declared


def _parse_series(series_path: Path) -> tuple[dict[str, float], dict[str, float]]:
    """``(close, total_return)`` from the verified SPY file."""
    try:
        payload = json.loads(series_path.read_text())
    except json.JSONDecodeError as exc:
        raise CoreSeriesError(f"core series {series_path} is not JSON: {exc}") from exc
    if not isinstance(payload, dict) or not payload:
        raise CoreSeriesError(f"core series {series_path} is empty")
    close: dict[str, float] = {}
    total_return: dict[str, float] = {}
    for day, row in payload.items():
        if not isinstance(row, dict) or "close" not in row:
            raise CoreSeriesError(f"core series {series_path}: {day} has no close")
        value = float(row["close"])
        if not math.isfinite(value) or value <= 0.0:
            raise CoreSeriesError(f"core series {series_path}: {day} close {value!r} is invalid")
        close[str(day)] = value
        raw_return = row.get("tr")
        if raw_return is not None:
            value_return = float(raw_return)
            if not math.isfinite(value_return):
                raise CoreSeriesError(
                    f"core series {series_path}: {day} total return {value_return!r} is invalid"
                )
            total_return[str(day)] = value_return
    return close, total_return


def load_core_series(path: str | Path) -> CoreSeries:
    """Read and verify the SPY file; *path*'s sha256 must equal the manifest beside it."""
    series_path = Path(path).expanduser().resolve()
    if not series_path.is_file():
        raise CoreSeriesError(f"core series {series_path} does not exist")
    declared = _declared_sha(series_path)
    digest = sha256_file(series_path)
    if digest != declared:
        raise CoreSeriesError(f"core series {series_path} sha256 {digest} != manifest {declared}")
    close, total_return = _parse_series(series_path)
    return CoreSeries(
        path=series_path,
        sha256=digest,
        declared_sha256=declared,
        close=close,
        total_return=total_return,
    )


def total_return_index(total_return: Mapping[str, float]) -> dict[str, float]:
    """The total-return index built from ``spy_tr.json``'s daily total returns.

    ``TR_t = TR_{t-1} (1 + r_t)`` with ``TR`` starting at 1.0 on the first session.  Faber
    (2013, p. 15) computes the 10-month signal on total-return series; the level's base does
    not affect the comparison with its own mean, so 1.0 is arbitrary but fixed.  A missing
    return (the file's first session has none) contributes 0.0 and keeps the level.
    """
    if not total_return:
        raise CoreSeriesError("total_return_index needs at least one session")
    out: dict[str, float] = {}
    level = 1.0
    for day in sorted(total_return):
        value = float(total_return[day])
        if not math.isfinite(value):
            raise CoreSeriesError(f"total return {value!r} on {day} is not finite")
        level *= 1.0 + value
        out[day] = level
    return out


def trend_trigger(
    close: Mapping[str, float], decision: str, *, window: int = TREND_WINDOW
) -> bool | None:
    """Whether the trend filter sends the book to cash at *decision* (V2_PLAN §2c).

    ``True`` = the close at *decision* is **below** the simple mean of the last *window*
    session closes ending there (filter triggered, target 100% cash); ``False`` = at or above;
    ``None`` = fewer than *window* sessions are available, so the filter is not evaluable and
    stays off (the caller records it). The read is causal: only sessions ``<= decision`` enter.
    A decision date missing from *close* is a data gap and raises, never "off".
    """
    if decision not in close:
        raise CoreSeriesError(f"core series has no close at decision date {decision}")
    sessions = sorted(day for day in close if day <= decision)
    if len(sessions) < window:
        return None
    mean = math.fsum(close[day] for day in sessions[-window:]) / window
    return close[decision] < mean


def trend_states(
    close: Mapping[str, float], decisions: Sequence[str], *, window: int = TREND_WINDOW
) -> list[dict[str, str]]:
    """One record per decision date: ``cash`` (triggered), ``invested``, or ``not_evaluable``."""
    out: list[dict[str, str]] = []
    for decision in decisions:
        trigger = trend_trigger(close, decision, window=window)
        state = "not_evaluable" if trigger is None else ("cash" if trigger else "invested")
        out.append({"date": decision, "state": state})
    return out


def core_target(
    core_weight: float, satellite: Mapping[int, float], core_asset_id: int
) -> dict[int, float]:
    """The core-satellite target: ``c`` in SPY + ``(1 - c)`` times the satellite's weights.

    ``core_weight`` 0 returns the satellite unchanged (v1), so the default path is exact.
    The satellite's own name and sector caps were already applied by the cycle / quant book.
    """
    if not math.isfinite(core_weight) or not 0.0 <= core_weight < 1.0:
        raise ValueError(f"core_weight must be in [0, 1), got {core_weight!r}")
    if core_weight == 0.0:
        return dict(satellite)
    scale = 1.0 - core_weight
    out: dict[int, float] = {int(core_asset_id): core_weight}
    for asset_id, weight in satellite.items():
        value = float(weight) * scale
        if value > 0.0:
            out[int(asset_id)] = value
    return out


@dataclass(frozen=True)
class ConfigTargets:
    """One decision date's two targets under a v2 configuration (V2_PLAN §2a/§2c)."""

    base: dict[int, float]
    topn: dict[int, float]
    trend_state: str | None  # None when the filter is off; else cash/invested/not_evaluable


def base_core_target(
    core_weight: float, base: Mapping[int, float], satellite: Mapping[int, float]
) -> dict[int, float]:
    """``c`` times the base book plus ``(1 - c)`` times the satellite; overlaps add (§2b).

    The combined weight is not re-capped (declared): the satellite's name and sector caps were
    applied to its own weights, and the base book's own caps to the core.
    """
    if not math.isfinite(core_weight) or not 0.0 <= core_weight < 1.0:
        raise ValueError(f"core_weight must be in [0, 1), got {core_weight!r}")
    out: dict[int, float] = {}
    for asset_id, weight in base.items():
        value = core_weight * float(weight)
        if value > 0.0:
            out[int(asset_id)] = out.get(int(asset_id), 0.0) + value
    for asset_id, weight in satellite.items():
        value = (1.0 - core_weight) * float(weight)
        if value > 0.0:
            out[int(asset_id)] = out.get(int(asset_id), 0.0) + value
    return out


def config_targets(  # noqa: PLR0913 - the configuration's inputs, all named
    base: Mapping[int, float],
    satellite: Mapping[int, float],
    *,
    core_weight: float,
    core_asset_id: int | None,
    trend_filter: str,
    close: Mapping[str, float] | None,
    decision: str,
    core_source: str = "spy",
    filter_exit: float = 1.0,
) -> ConfigTargets:
    """The base and Top-N targets at *decision* under the configuration's core and filter.

    The base book is returned unchanged: V2_PLAN §2c excludes it from the filter and §2d says
    the base GUARD arm is identical across configurations, so neither the core nor the filter
    touches it. The Top-N target gets the core blend, then -- if the filter triggers -- becomes
    empty (100% cash) until the next decision date.
    """
    if trend_filter not in FILTERS:
        raise ValueError(f"unknown trend_filter {trend_filter!r}: expected one of {FILTERS}")
    if core_source not in CORE_SOURCES:
        raise ValueError(f"unknown core_source {core_source!r}: expected one of {CORE_SOURCES}")
    if not math.isfinite(filter_exit) or not 0.0 <= filter_exit <= 1.0:
        raise ValueError(f"filter_exit must be in [0, 1], got {filter_exit!r}")
    if core_weight > 0.0:
        if core_source == "spy":
            if core_asset_id is None:
                raise ValueError("core_source='spy' with core_weight > 0 needs the SPY asset id")
            topn = core_target(core_weight, satellite, core_asset_id)
        else:
            topn = base_core_target(core_weight, base, satellite)
    else:
        topn = dict(satellite)
    state: str | None = None
    if trend_filter == FILTER_ON:
        trigger = trend_trigger(close or {}, decision)
        state = "not_evaluable" if trigger is None else ("cash" if trigger else "invested")
        if trigger:
            if filter_exit >= 1.0:
                topn = {}
            else:
                topn = {
                    asset_id: value * (1.0 - filter_exit)
                    for asset_id, value in topn.items()
                    if value * (1.0 - filter_exit) > 0.0
                }
    return ConfigTargets(base=dict(base), topn=topn, trend_state=state)
