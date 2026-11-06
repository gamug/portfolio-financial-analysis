"""Phase 1 statistics: per-window/per-series metrics, the overfitting family, PBO, DSR, RC
and walk-forward.  Pure arithmetic over the collected JSON files; no database, no network.

    uv run python scripts/phase1_stats.py --out DIR

Every input is a ``runs/<run_id>.json`` collector; every series is a portfolio aggregate.  The
family of §10 is the 30 unique corrected configurations: B0-B6, V0-V5, X1-X11, Y1-Y4 and
Z1-Z4, with Y2 = B0 and Z2 = Z0 collapsed (checked, not assumed).  The GUARD arm
(TOPN_MV_GUARD_NO_NLP) is the family's series; the NOGUARD arm is reported beside it.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

# --------------------------------------------------------------------------------------
# series and metrics
# --------------------------------------------------------------------------------------

WINDOWS = ("P1", "H1", "P3", "H2", "P2", "H3")
PERIODS = 252
MIN_OBS_CI = 3
MIN_TRIALS = 2
LEVEL_90 = 0.90
SPY_FILE = Path("/workspaces/thesis/scratch/final/data/spy/spy_tr.json")


def load_run(out: Path, run_id: str) -> dict[str, Any]:
    path = out / "runs" / f"{run_id}.json"
    if not path.is_file():
        raise SystemExit(f"missing {path}")
    return dict(json.loads(path.read_text()))


def arm_daily(payload: dict[str, Any], arm: str, field: str = "net") -> dict[str, float]:
    if field == "net":
        return {str(day): float(value) for day, value in payload["daily"][arm].items()}
    return {str(day): float(row[field]) for day, row in payload["daily_detail"][arm].items()}


def _ordered(series: dict[str, float]) -> list[float]:
    return [series[day] for day in sorted(series)]


def _max_drawdown(returns: np.ndarray) -> float:
    nav = np.cumprod(1.0 + returns)
    peak = np.maximum.accumulate(nav)
    return float(np.max(1.0 - nav / peak)) if len(nav) else 0.0


def metrics(returns: dict[str, float], rf: dict[str, float]) -> dict[str, float]:
    days = sorted(returns)
    r = np.asarray([returns[day] for day in days], dtype=float)
    if len(r) == 0:
        return {
            key: 0.0
            for key in (
                "n",
                "cumulative_return",
                "cagr",
                "annualized_vol",
                "sharpe",
                "max_drawdown",
                "calmar",
                "cvar95",
                "days_under_water",
                "mean_daily",
                "worst_day",
            )
        }
    rf_arr = np.asarray([float(rf.get(day, 0.0)) for day in days], dtype=float)
    excess = r - rf_arr
    nav = np.cumprod(1.0 + r)
    years = len(r) / PERIODS
    vol = float(np.std(r, ddof=1) * math.sqrt(PERIODS)) if len(r) > 1 else 0.0
    std_excess = float(np.std(excess, ddof=1)) if len(excess) > 1 else 0.0
    sharpe = float(np.mean(excess) / std_excess * math.sqrt(PERIODS)) if std_excess > 0 else 0.0
    mdd = _max_drawdown(r)
    peak = np.maximum.accumulate(nav)
    cagr = float(nav[-1] ** (1.0 / years) - 1.0) if years > 0 and nav[-1] > 0 else -1.0
    tail_n = max(1, math.floor(0.05 * len(r)))
    cvar = float(-np.mean(np.sort(r)[:tail_n]))
    return {
        "n": float(len(r)),
        "cumulative_return": float(nav[-1] - 1.0),
        "cagr": cagr,
        "annualized_vol": vol,
        "sharpe": sharpe,
        "max_drawdown": mdd,
        "calmar": cagr / mdd if mdd > 0 else 0.0,
        "cvar95": cvar,
        "days_under_water": int(np.sum(nav < peak - 1e-15)),
        "mean_daily": float(np.mean(r)),
        "worst_day": float(np.min(r)),
    }


def concatenate(parts: list[dict[str, float]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in parts:
        for day, value in part.items():
            if day in out:
                raise SystemExit(f"duplicate session {day} in the concatenation")
            out[day] = value
    return {day: out[day] for day in sorted(out)}


def hac_mean_ci(returns: dict[str, float], level: float = 0.90) -> dict[str, float]:
    r = _ordered(returns)
    x = np.asarray(r, dtype=float)
    n = len(x)
    if n < MIN_OBS_CI:
        return {"mean": float(x.mean()) if n else 0.0, "se": 0.0, "low": 0.0, "high": 0.0}
    lags = math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0))
    z = x - x.mean()
    gamma0 = float(np.dot(z, z) / n)
    var = gamma0
    for lag in range(1, lags + 1):
        weight = 1.0 - lag / (lags + 1.0)
        cov = float(np.dot(z[lag:], z[:-lag]) / n)
        var += 2.0 * weight * cov
    se = math.sqrt(max(var, 0.0) / n)
    zcrit = 1.6448536269514722 if level == LEVEL_90 else 1.959963984540054
    mean = float(x.mean())
    return {"mean": mean, "se": se, "low": mean - zcrit * se, "high": mean + zcrit * se}


def _bootstrap_p_values(observed: float, diffs: np.ndarray) -> dict[str, float]:
    """Pivot, mean-centred and legacy p-values from the same bootstrap draws.

    ``pivot`` recentres the replicate differences on the observed one (the bootstrap
    distribution under H0), ``mean_centred`` recentres on the replicate mean, and ``legacy``
    is the old uncentred |d*| >= |d_obs| count.  The first is the primary test."""
    n_boot = len(diffs)
    threshold = abs(observed) - 1e-15

    def _p(values: np.ndarray) -> float:
        return float((np.sum(np.abs(values) >= threshold) + 1) / (n_boot + 1))

    return {
        "p_value": _p(diffs - observed),
        "p_value_pivot": _p(diffs - observed),
        "p_value_mean_centred": _p(diffs - float(diffs.mean())),
        "p_value_legacy": _p(diffs),
    }


def mdd_bootstrap_distribution(
    a: dict[str, float], b: dict[str, float], *, block: int, n_boot: int = 4999, seed: int = 7
) -> tuple[float, np.ndarray]:
    """The paired fixed-block bootstrap draws behind :func:`block_mdd_diff`."""
    days = sorted(set(a) & set(b))
    ra = np.asarray([a[day] for day in days], dtype=float)
    rb = np.asarray([b[day] for day in days], dtype=float)
    observed = _max_drawdown(ra) - _max_drawdown(rb)
    rng = np.random.default_rng(seed)
    n = len(days)
    diffs = np.empty(n_boot, dtype=float)
    for i in range(n_boot):
        starts = rng.integers(0, n, size=math.ceil(n / block))
        index = np.concatenate([(np.arange(s, s + block) % n) for s in starts])[:n]
        diffs[i] = _max_drawdown(ra[index]) - _max_drawdown(rb[index])
    return float(observed), diffs


def hac_mean_diff(a: dict[str, float], b: dict[str, float], *, lag: int = 10) -> dict[str, float]:
    """Newey-West (Bartlett) test of the mean daily difference ``a - b``.

    ``lag`` is fixed, not the automatic rule used by :func:`hac_mean_ci`.  The p-value is
    two-sided normal."""
    days = sorted(set(a) & set(b))
    diff = np.asarray([a[day] - b[day] for day in days], dtype=float)
    n = len(diff)
    mean = float(diff.mean())
    z = diff - mean
    var = float(np.dot(z, z) / n)
    for offset in range(1, lag + 1):
        weight = 1.0 - offset / (lag + 1.0)
        var += 2.0 * weight * float(np.dot(z[offset:], z[:-offset]) / n)
    se = math.sqrt(max(var, 0.0) / n)
    t = mean / se if se > 0 else 0.0
    p = 2.0 * (1.0 - _norm_cdf(abs(t)))
    return {
        "mean_daily": mean,
        "mean_annual_pp": mean * PERIODS * 100.0,
        "t": t,
        "p_value": p,
        "n": float(n),
        "lag": float(lag),
    }


def block_mdd_diff(
    a: dict[str, float], b: dict[str, float], *, block: int, n_boot: int = 4999, seed: int = 7
) -> dict[str, float]:
    observed, diffs = mdd_bootstrap_distribution(a, b, block=block, n_boot=n_boot, seed=seed)
    return {
        "observed": observed,
        "bootstrap_mean": float(diffs.mean()),
        "block": float(block),
        "n_boot": float(n_boot),
        "seed": float(seed),
        **_bootstrap_p_values(observed, diffs),
    }


def stationary_indices(rng: np.random.Generator, n: int, mean_block: float) -> np.ndarray:
    p = 1.0 / mean_block
    out = np.empty(n, dtype=int)
    out[0] = int(rng.integers(0, n))
    for i in range(1, n):
        if rng.random() < p:
            out[i] = int(rng.integers(0, n))
        else:
            out[i] = (out[i - 1] + 1) % n
    return out


def stationary_mdd_diff(
    a: dict[str, float],
    b: dict[str, float],
    *,
    mean_block: float,
    n_boot: int = 4999,
    seed: int = 7,
) -> dict[str, float]:
    days = sorted(set(a) & set(b))
    ra = np.asarray([a[day] for day in days], dtype=float)
    rb = np.asarray([b[day] for day in days], dtype=float)
    observed = _max_drawdown(ra) - _max_drawdown(rb)
    rng = np.random.default_rng(seed)
    n = len(days)
    diffs = np.empty(n_boot, dtype=float)
    for i in range(n_boot):
        index = stationary_indices(rng, n, mean_block)
        diffs[i] = _max_drawdown(ra[index]) - _max_drawdown(rb[index])
    return {
        "observed": float(observed),
        "bootstrap_mean": float(diffs.mean()),
        "mean_block": float(mean_block),
        "n_boot": float(n_boot),
        "seed": float(seed),
        **_bootstrap_p_values(observed, diffs),
    }


# --------------------------------------------------------------------------------------
# the 30-configuration family
# --------------------------------------------------------------------------------------

FAMILY = [
    *(f"R1-{config}" for config in ("B0", "B1", "B2", "B3", "B4", "B5", "B6")),
    *(f"R2-{config}" for config in ("V0", "V1", "V2", "V3", "V4", "V5")),
    *(
        f"R2-{config}"
        for config in (
            "X1",
            "X2",
            "X3",
            "X4",
            "X5",
            "X6",
            "X7",
            "X8",
            "X9",
            "X10",
            "X11",
        )
    ),
    *(f"R2-{config}" for config in ("Y1", "Y2", "Y3", "Y4")),
    *(f"R2-{config}" for config in ("Z1", "Z2", "Z3", "Z4")),
]
#: Y2 = B0 and Z2 = Z0 = B0; the family keeps one of each after the identity check.
_DUPLICATES = {"R2-Y2": "R1-B0", "R2-Z2": "R1-B0"}


def family_series(out: Path, arm: str) -> tuple[list[str], dict[str, dict[str, float]]]:
    series: dict[str, dict[str, float]] = {}
    names: list[str] = []
    for config in FAMILY:
        if config in _DUPLICATES:
            continue
        parts = []
        for window in WINDOWS:
            payload = load_run(out, f"{config}-{window}")
            parts.append(arm_daily(payload, arm))
        series[config] = concatenate(parts)
        names.append(config)
    return names, series


def _sharpe_vector(returns: np.ndarray) -> float:
    std = float(np.std(returns, ddof=1))
    return float(np.mean(returns) / std * math.sqrt(PERIODS)) if std > 0 else 0.0


def pbo_cscv(matrix: np.ndarray, metric: str = "sharpe", s_blocks: int = 16) -> dict[str, Any]:
    """Bailey et al. CSCV: all C(S, S/2) splits, the IS-best config's OOS rank.

    Each half is the **concatenation** of its blocks (one series per config), so the metric is
    the Sharpe / Calmar of the half, not an average of block metrics.  The rank counts the
    configs whose OOS metric is at or below the IS-best's (1 = worst)."""
    n_obs, n_configs = matrix.shape
    if s_blocks % 2:
        raise SystemExit("PBO needs an even block count")
    edges = np.linspace(0, n_obs, s_blocks + 1, dtype=int)
    blocks = [matrix[edges[i] : edges[i + 1]] for i in range(s_blocks)]
    half = s_blocks // 2
    logits: list[float] = []
    is_best: list[float] = []
    oos_best: list[float] = []
    for chosen in itertools.combinations(range(s_blocks), half):
        other = [b for b in range(s_blocks) if b not in chosen]
        is_matrix = np.concatenate([blocks[b] for b in chosen], axis=0)
        oos_matrix = np.concatenate([blocks[b] for b in other], axis=0)
        if metric == "sharpe":
            is_score = _sharpe_columns(is_matrix)
            oos_score = _sharpe_columns(oos_matrix)
        else:
            is_score = np.asarray([-_max_drawdown(is_matrix[:, c]) for c in range(n_configs)])
            oos_score = np.asarray([-_max_drawdown(oos_matrix[:, c]) for c in range(n_configs)])
        best = int(np.argmax(is_score))
        rank = int(np.sum(oos_score <= oos_score[best]))
        omega = (rank + 0.5) / (n_configs + 1)
        logits.append(math.log(omega / (1.0 - omega)))
        is_best.append(float(is_score[best]))
        oos_best.append(float(oos_score[best]))
    return {
        "metric": metric,
        "blocks": s_blocks,
        "splits": len(logits),
        "pbo": float(np.mean(np.asarray(logits) <= 0.0)),
        "mean_is_best": float(np.mean(is_best)),
        "mean_oos_best": float(np.mean(oos_best)),
    }


def _sharpe_columns(matrix: np.ndarray) -> np.ndarray:
    means = matrix.mean(axis=0)
    stds = matrix.std(axis=0, ddof=1)
    out = np.zeros(matrix.shape[1])
    good = stds > 0
    out[good] = means[good] / stds[good] * math.sqrt(PERIODS)
    return out


def deflated_sharpe(
    returns: np.ndarray, n_trials: float, trial_sharpes: np.ndarray | None = None
) -> dict[str, float]:
    """Bailey & López de Prado (2014) eq. (2): DSR at N trials."""
    t = len(returns)
    sr = float(np.mean(returns) / np.std(returns, ddof=1)) if np.std(returns, ddof=1) > 0 else 0.0
    skew = float(_skew(returns))
    kurt = float(_kurtosis(returns))
    if trial_sharpes is None or len(trial_sharpes) < MIN_TRIALS:
        v_sr = 1.0 / max(t - 1, 1)
    else:
        v_sr = float(np.var(trial_sharpes, ddof=1))
    euler = 0.5772156649015329
    if n_trials < MIN_TRIALS:
        sr0 = 0.0
    else:
        sr0 = math.sqrt(v_sr) * (
            (1.0 - euler) * _norm_ppf(1.0 - 1.0 / n_trials)
            + euler * _norm_ppf(1.0 - 1.0 / (n_trials * math.e))
        )
    denom = math.sqrt(max(1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr * sr, 1e-18) / max(t - 1, 1))
    dsr = _norm_cdf((sr - sr0) / denom) if denom > 0 else 0.0
    return {
        "sharpe_daily": sr,
        "sharpe_annual": sr * math.sqrt(PERIODS),
        "skew": skew,
        "kurtosis": kurt,
        "n_trials": float(n_trials),
        "sr0_daily": sr0,
        "dsr": float(dsr),
        "expected_max_annual": float(sr0 * math.sqrt(PERIODS)),
    }


def _skew(x: np.ndarray) -> float:
    m = x.mean()
    s = x.std(ddof=0)
    return float(np.mean(((x - m) / s) ** 3)) if s > 0 else 0.0


def _kurtosis(x: np.ndarray) -> float:
    m = x.mean()
    s = x.std(ddof=0)
    return float(np.mean(((x - m) / s) ** 4)) if s > 0 else 3.0


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    # Acklam's inverse normal; accuracy ~1e-9, enough for DSR's thresholds.
    a = [
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    ]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00]
    plow, phigh = 0.02425, 1.0 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    q = p - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    )


def expected_max(n_trials: float, v_sr: float) -> dict[str, float]:
    if n_trials < MIN_TRIALS:
        return {"e_max_daily": 0.0, "e_max_annual": 0.0}
    euler = 0.5772156649015329
    value = math.sqrt(v_sr) * (
        (1.0 - euler) * _norm_ppf(1.0 - 1.0 / n_trials)
        + euler * _norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    )
    return {"e_max_daily": value, "e_max_annual": value * math.sqrt(PERIODS)}


def effective_trials(series: dict[str, dict[str, float]]) -> dict[str, float]:
    names = list(series)
    days = sorted(set.intersection(*(set(series[name]) for name in names)))
    matrix = np.asarray([[series[name][day] for name in names] for day in days])
    corr = np.corrcoef(matrix, rowvar=False)
    eigenvalues = np.linalg.eigvalsh(corr)
    participation = float(eigenvalues.sum() ** 2 / np.sum(eigenvalues**2))
    threshold = 0.9
    adjacency = np.abs(corr) >= threshold
    clusters = 0
    seen: set[int] = set()
    for start in range(len(names)):
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        while stack:
            node = stack.pop()
            for nxt in np.where(adjacency[node])[0]:
                if int(nxt) not in seen:
                    seen.add(int(nxt))
                    stack.append(int(nxt))
        clusters += 1
    return {
        "eigen_participation": max(2.0, participation),
        "correlation_clusters_0.9": float(max(2, clusters)),
    }


def white_reality_check(
    series: dict[str, dict[str, float]],
    benchmark: dict[str, float],
    *,
    n_boot: int = 1000,
    mean_block: float = 10.0,
    seed: int = 7,
) -> dict[str, Any]:
    """White (2000) Reality Check with Politis & Romano's stationary bootstrap.

    ``V = max_k sqrt(n) fbar_k`` over the models' daily differentials against the benchmark;
    the null is resampled from the recentred differentials.  One-sided p (the best model is
    not better than the benchmark)."""
    names = list(series)
    days = sorted(set(benchmark) & set.intersection(*(set(series[name]) for name in names)))
    diffs = {
        name: np.asarray([series[name][day] - benchmark[day] for day in days], dtype=float)
        for name in names
    }
    means = {name: float(values.mean()) for name, values in diffs.items()}
    best = max(means, key=means.get)
    n = len(days)
    observed = math.sqrt(n) * means[best]
    centred = {name: values - values.mean() for name, values in diffs.items()}
    rng = np.random.default_rng(seed)
    count = 0
    for _ in range(n_boot):
        index = stationary_indices(rng, n, mean_block)
        stat = math.sqrt(n) * max(float(values[index].mean()) for values in centred.values())
        if stat >= observed - 1e-15:
            count += 1
    return {
        "best": best,
        "mean_differential_annual": float(means[best] * PERIODS * 100.0),
        "p_value": (count + 1) / (n_boot + 1),
        "mean_block": mean_block,
    }


def walk_forward(
    series: dict[str, dict[str, float]], windows: dict[str, list[str]]
) -> dict[str, Any]:
    names = list(series)
    order = [w for w in WINDOWS if w in windows]
    picks: list[dict[str, Any]] = []
    for k in range(1, len(order)):
        past = order[:k]
        scores = {
            name: float(
                np.prod([1.0 + _window_return(series[name], windows[w]) for w in past]) - 1.0
            )
            for name in names
        }
        best = max(scores, key=scores.get)
        next_window = order[k]
        next_scores = {name: _window_return(series[name], windows[next_window]) for name in names}
        ranked = sorted(next_scores, key=next_scores.get, reverse=True)
        picks.append(
            {
                "after": past,
                "picked": best,
                "picked_rank_next": ranked.index(best) + 1,
                "of": len(names),
                "next_return": next_scores[best],
                "next_mdd": _window_mdd(series[best], windows[next_window]),
            }
        )
    return {"picks": picks}


def _window_return(series: dict[str, float], days: list[str]) -> float:
    values = [series[day] for day in days if day in series]
    return float(np.prod(1.0 + np.asarray(values)) - 1.0)


def _window_mdd(series: dict[str, float], days: list[str]) -> float:
    values = np.asarray([series[day] for day in days if day in series], dtype=float)
    return _max_drawdown(values)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    payload: dict[str, Any] = {}

    # Per-run, per-window metrics (GUARD and NOGUARD arms), 10 bp.
    window_metrics: dict[str, Any] = {}
    for run_id in sorted(path.stem for path in (out / "runs").glob("*.json")):
        data = load_run(out, run_id)
        rf = data["rf"]
        per_arm = {}
        for arm in data["arms"]:
            daily = arm_daily(data, arm)
            per_arm[arm] = metrics(daily, rf)
        window_metrics[run_id] = per_arm
    payload["window_metrics"] = window_metrics

    # Concatenated family series (GUARD arm) and the overfitting diagnostics.
    names, series = family_series(out, "TOPN_MV_GUARD_NO_NLP")
    days = sorted(set.intersection(*(set(series[name]) for name in names)))
    matrix = np.asarray([[series[name][day] for name in names] for day in days])
    # The family's Sharpe must be the same excess Sharpe the report's metrics() uses: net
    # return minus that session's French RF.  RF is common to every configuration; take it
    # from the B0 run (the same curve/dates for all runs).
    rf_family = concatenate([load_run(out, f"R1-B0-{window}")["rf"] for window in WINDOWS])
    excess_matrix = np.asarray(
        [[series[name][day] - float(rf_family.get(day, 0.0)) for name in names] for day in days]
    )
    windows = {
        window: sorted(load_run(out, f"R1-B0-{window}")["daily"]["TOPN_MV_GUARD_NO_NLP"])
        for window in WINDOWS
    }
    sharpes = np.asarray(
        [_sharpe_vector(excess_matrix[:, i]) for i in range(excess_matrix.shape[1])]
    )
    # The DSR / E[max] algebra is in daily Sharpe units; the family table reports annualised.
    sharpes_daily = sharpes / math.sqrt(PERIODS)
    trial_variance = float(np.var(sharpes_daily, ddof=1))
    effective = effective_trials(series)
    final_excess = excess_matrix[:, names.index("R1-B5")]
    n_eff_eigen = effective["eigen_participation"]
    n_eff_clusters = effective["correlation_clusters_0.9"]
    payload["family"] = {
        "configurations": names,
        "n": len(names),
        "sessions": len(days),
        "sharpes_annual": dict(zip(names, sharpes.tolist(), strict=True)),
        "trial_variance_daily": trial_variance,
        "effective_trials": effective,
        "dsr": {
            "N_eff_eigen": deflated_sharpe(final_excess, n_eff_eigen, sharpes_daily),
            "N_eff_clusters": deflated_sharpe(final_excess, n_eff_clusters, sharpes_daily),
            "N30": deflated_sharpe(final_excess, len(names), sharpes_daily),
            "N55": deflated_sharpe(final_excess, 55, sharpes_daily),
        },
        "expected_max": {
            "N_eff_eigen": expected_max(n_eff_eigen, trial_variance),
            "N_eff_clusters": expected_max(n_eff_clusters, trial_variance),
            "N30": expected_max(len(names), trial_variance),
            "N55": expected_max(55, trial_variance),
        },
        "pbo": {
            "S16_sharpe": pbo_cscv(excess_matrix, "sharpe", 16),
            "S8_sharpe": pbo_cscv(excess_matrix, "sharpe", 8),
            "S10_sharpe": pbo_cscv(excess_matrix, "sharpe", 10),
            "S12_sharpe": pbo_cscv(excess_matrix, "sharpe", 12),
            "S20_sharpe": pbo_cscv(excess_matrix, "sharpe", 20),
            # Calmar is on total (net) returns, as before.
            "S16_calmar": pbo_cscv(matrix, "calmar", 16),
        },
        "walk_forward": {
            "21": walk_forward(
                {
                    n: series[n]
                    for n in names
                    if n.split("-")[0] == "R2"
                    and n.split("-")[1]
                    in {f"X{i}" for i in range(1, 12)}
                    | {f"V{i}" for i in range(6)}
                    | {f"Y{i}" for i in range(1, 5)}
                },
                windows,
            ),
            "30": walk_forward(series, windows),
        },
    }
    # White RC vs MARKOWITZ_BASE (B0) and vs SPY (benchmark series is EW? SPY file is not a
    # run; the collector's benchmark is SP500_EW_INTERNAL.  Use B0's benchmark for the EW
    # comparison and leave the SPY comparison to the report script, which has the SPY file.)
    markowitz = _markowitz_series(out)
    payload["family"]["white_rc_vs_markowitz"] = {
        "q10": white_reality_check(series, markowitz, mean_block=10.0),
        "q1": white_reality_check(series, markowitz, mean_block=1.0),
    }
    spy = _spy_series()
    payload["family"]["white_rc_vs_spy"] = {
        "q10": white_reality_check(series, spy, mean_block=10.0),
        "q1": white_reality_check(series, spy, mean_block=1.0),
    }
    (out / "stats.json").write_text(json.dumps(payload, indent=1) + "\n")
    print(json.dumps({"family": len(names), "sessions": len(days)}))
    return 0


def _markowitz_series(out: Path) -> dict[str, float]:
    parts = []
    for window in WINDOWS:
        payload = load_run(out, f"R1-B0-{window}")
        parts.append(arm_daily(payload, "MARKOWITZ_BASE"))
    return concatenate(parts)


def _spy_series() -> dict[str, float]:
    raw = json.loads(SPY_FILE.read_text())
    return {str(day): float(row["tr"]) for day, row in raw.items() if row.get("tr") is not None}


if __name__ == "__main__":
    raise SystemExit(main())
