"""Assemble an aligned ``(T, N)`` total-return matrix for an as-of date.

Reads ``quant_return_daily`` (``tr_log_return``), pivots to a dense matrix on a
common trading calendar, drops short-history names (or shrinks the window), heals
single-day holes, and hashes the exact ``(asset_ids, dates)`` so a risk model
built from it is reproducible. No pandas -- plain numpy plus an explicit
``asset_ids`` ordering vector.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt
from portfolio_common.db import Database

Vec = npt.NDArray[np.float64]


class PanelError(RuntimeError):
    """The gated universe cannot form a rectangular, gap-free return block."""


@dataclass(frozen=True)
class ReturnPanel:
    asset_ids: list[int]  # column order, ascending
    dates: list[str]  # row order, ascending ISO (length T)
    returns: Vec  # (T, N) total-return log returns, no NaN
    adj_close: Vec  # (T, N) TR-adjusted close
    coverage: Vec  # (N,) fraction of non-missing obs in the raw window
    as_of: str
    lookback_days: int
    return_engine_version: str
    spec_sha256: str

    @property
    def n_assets(self) -> int:
        return len(self.asset_ids)


def _forward_fill_small_gaps(col: Vec, max_gap: int) -> Vec:
    """Fill runs of <= *max_gap* consecutive interior NaNs with 0.0 (a flat day)."""
    out = col.copy()
    nan = np.isnan(out)
    i, n = 0, len(out)
    while i < n:
        if not nan[i]:
            i += 1
            continue
        j = i
        while j < n and nan[j]:
            j += 1
        interior = i > 0
        if interior and j - i <= max_gap:
            out[i:j] = 0.0
        i = j
    return out


def align_matrix(
    series_by_asset: dict[int, dict[str, float]], dates: list[str], asset_ids: list[int]
) -> Vec:
    m = np.full((len(dates), len(asset_ids)), np.nan, dtype=np.float64)
    date_ix = {d: i for i, d in enumerate(dates)}
    for j, aid in enumerate(asset_ids):
        for d, v in series_by_asset.get(aid, {}).items():
            i = date_ix.get(d)
            if i is not None:
                m[i, j] = v
    return m


def _load_calendar(
    conn: Database, *, as_of: str, engine_version: str, lookback_days: int
) -> list[str]:
    return [
        str(r["obs_date"])
        for r in conn.execute(
            "SELECT DISTINCT obs_date FROM quant_return_daily "
            "WHERE obs_date <= ? AND engine_version = ? ORDER BY obs_date DESC LIMIT ?",
            (as_of, engine_version, lookback_days),
        )
    ][::-1]


def _load_series(
    conn: Database,
    *,
    engine_version: str,
    start: str,
    as_of: str,
    asset_ids: Sequence[int] | None,
) -> tuple[dict[int, dict[str, float]], dict[int, dict[str, float]]]:
    params: list[object] = [engine_version, start, as_of]
    clause = ""
    if asset_ids is not None:
        ids = list(asset_ids)
        if not ids:
            raise PanelError("empty universe passed to build_return_panel")
        clause = f" AND asset_id IN ({','.join('?' * len(ids))})"
        params += ids
    base = (
        "SELECT asset_id, obs_date, tr_log_return, adj_close FROM quant_return_daily "
        "WHERE engine_version = ? AND obs_date >= ? AND obs_date <= ?"
    )
    query = base + clause  # `clause` holds only "?" bound-param placeholders
    tr: dict[int, dict[str, float]] = {}
    px: dict[int, dict[str, float]] = {}
    for row in conn.execute(query, params):
        aid = int(row["asset_id"])
        if row["tr_log_return"] is not None:
            tr.setdefault(aid, {})[str(row["obs_date"])] = float(row["tr_log_return"])
        if row["adj_close"] is not None:
            px.setdefault(aid, {})[str(row["obs_date"])] = float(row["adj_close"])
    return tr, px


def build_return_panel(  # noqa: PLR0913 - all keyword-only knobs with defaults
    conn: Database,
    *,
    as_of: str,
    lookback_days: int = 756,
    min_history_days: int = 504,
    universe_asset_ids: Sequence[int] | None = None,
    return_engine_version: str = "qret-v1",
    on_short_history: Literal["exclude", "shrink_window"] = "exclude",
    max_gap_ffill: int = 1,
    min_coverage: float = 0.98,
) -> ReturnPanel:
    dates = _load_calendar(
        conn, as_of=as_of, engine_version=return_engine_version, lookback_days=lookback_days
    )
    if len(dates) < min_history_days:
        raise PanelError(
            f"only {len(dates)} trading days <= {as_of} at engine {return_engine_version}; "
            f"need {min_history_days}"
        )

    tr, px = _load_series(
        conn,
        engine_version=return_engine_version,
        start=dates[0],
        as_of=as_of,
        asset_ids=universe_asset_ids,
    )
    candidates = sorted(tr)
    if not candidates:
        raise PanelError(f"no total-return rows in the window ending {as_of}")
    raw = align_matrix(tr, dates, candidates)
    coverage_raw = 1.0 - np.isnan(raw).mean(axis=0)

    if on_short_history == "shrink_window":
        first_full = np.where(~np.isnan(raw).any(axis=1))[0]
        if first_full.size:
            dates = dates[int(first_full[0]) :]
            raw = align_matrix(tr, dates, candidates)
        keep_ix = list(range(len(candidates)))
    else:
        counts = (~np.isnan(raw)).sum(axis=0)
        floor = max(min_history_days, int(min_coverage * len(dates)))
        keep_ix = [j for j in range(len(candidates)) if counts[j] >= floor]
    if not keep_ix:
        raise PanelError(f"no asset has >= {min_history_days} obs in the window ending {as_of}")

    asset_ids = [candidates[j] for j in keep_ix]
    returns = np.column_stack([_forward_fill_small_gaps(raw[:, j], max_gap_ffill) for j in keep_ix])
    # kept columns cleared the coverage bar; zero-fill their few residual interior
    # holes (a flat day), but bail if any column is still materially incomplete.
    residual = np.isnan(returns).mean(axis=0)
    bad = {
        asset_ids[j]: float(residual[j])
        for j in range(len(asset_ids))
        if residual[j] > (1.0 - min_coverage)
    }
    if bad:
        raise PanelError(f"columns still materially incomplete after gating: {bad}")
    returns = np.nan_to_num(returns, nan=0.0)

    spec = json.dumps({"asset_ids": asset_ids, "dates": dates}, separators=(",", ":"))
    return ReturnPanel(
        asset_ids=asset_ids,
        dates=dates,
        returns=returns,
        adj_close=align_matrix(px, dates, asset_ids),
        coverage=np.asarray([coverage_raw[j] for j in keep_ix], dtype=np.float64),
        as_of=as_of,
        lookback_days=lookback_days,
        return_engine_version=return_engine_version,
        spec_sha256=hashlib.sha256(spec.encode()).hexdigest(),
    )
