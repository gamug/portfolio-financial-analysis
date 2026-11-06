"""Phase 1 §3 offline benchmarks: BMK_EXPO, BMK_BETA and BMK_VOL.

    uv run python scripts/phase1_benchmarks.py --out DIR

Diagnostic benchmarks, not operational alternatives: their exposure decisions derive from
FINAL's.  SPY total returns come from the verified ``spy_tr.json`` (private scratch), cash from
FINAL's own French RF rows.  Every series pays the formation cost on D1 and 10 bp on the
turnover its own rule generates.  Only aggregates are written.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

WINDOWS = ("P1", "H1", "P3", "H2", "P2", "H3")
SPY_FILE = Path("/workspaces/thesis/scratch/final/data/spy/spy_tr.json")
COST = 10.0 / 10_000.0
BETA_WINDOW = 252
BETA_MIN = 126
VOL_WINDOW = 63


def _load(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text()))


def _series(values: dict[str, float]) -> dict[str, float]:
    return {day: float(value) for day, value in values.items()}


def _metrics(returns: dict[str, float], rf: dict[str, float]) -> dict[str, float]:
    days = sorted(returns)
    r = np.asarray([returns[d] for d in days], dtype=float)
    rf_arr = np.asarray([rf.get(d, 0.0) for d in days], dtype=float)
    nav = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(nav)
    excess = r - rf_arr
    vol = float(np.std(r, ddof=1) * math.sqrt(252))
    sharpe = (
        float(np.mean(excess) / np.std(excess, ddof=1) * math.sqrt(252))
        if len(r) > 1 and np.std(excess, ddof=1) > 0
        else 0.0
    )
    mdd = float(np.max(1.0 - nav / peak)) if len(nav) else 0.0
    years = len(r) / 252.0
    cagr = float(nav[-1] ** (1.0 / years) - 1.0) if years > 0 and nav[-1] > 0 else 0.0
    return {
        "cumulative_return": float(nav[-1] - 1.0) if len(nav) else 0.0,
        "cagr": cagr,
        "sharpe": sharpe,
        "max_drawdown": mdd,
        "calmar": cagr / mdd if mdd > 0 else 0.0,
        "annualized_vol": vol,
        "n": float(len(r)),
    }


def _decision_dates(payload: dict[str, Any]) -> list[str]:
    dates = [str(row["decision_date"]) for row in payload["params"]["pool_decisions"]]
    return sorted(set(dates))


def _execution_date(decision: str, sessions: list[str]) -> str:
    later = [day for day in sessions if day > decision]
    return later[0] if later else decision


def _exposure(payload: dict[str, Any], arm: str) -> dict[str, float]:
    cash = payload["target_cash"][arm]
    return {day: 1.0 - float(value) for day, value in cash.items()}


def _spy_returns(spy: dict[str, Any]) -> dict[str, float]:
    return {str(day): float(row["tr"]) for day, row in spy.items() if row.get("tr") is not None}


def _benchmark_expo(
    payload: dict[str, Any], arm: str, spy: dict[str, float], sessions: list[str]
) -> dict[str, dict[str, float]]:
    formation = str(payload["run"]["formation_date"])
    exposure = _exposure(payload, arm)
    decisions = _decision_dates(payload)
    updates: dict[str, float] = {}
    for decision in decisions:
        execution = _execution_date(decision, sessions)
        if decision in exposure:
            updates[execution] = exposure[decision]
    current = updates.get(formation)
    if current is None:
        prior = [d for d in decisions if d < formation]
        current = exposure.get(prior[-1], 1.0) if prior else 1.0
    net: dict[str, float] = {}
    gross: dict[str, float] = {}
    cost_series: dict[str, float] = {}
    exposure_series: dict[str, float] = {}
    weight = current
    for day in sessions:
        exposure_series[day] = weight
        if day == formation:
            cost = COST * abs(weight - 0.0)
            gross[day] = 0.0
            cost_series[day] = cost
            net[day] = -cost
            continue
        r_spy = spy.get(day, 0.0)
        port = weight * r_spy + (1.0 - weight) * 0.0
        drifted = weight * (1.0 + r_spy) / (1.0 + port) if 1.0 + port > 0 else weight
        cost = 0.0
        if day in updates:
            target = updates[day]
            cost = COST * abs(target - drifted)
            weight = target
        else:
            weight = drifted
        gross[day] = port
        cost_series[day] = cost
        net[day] = port - cost
    return {"net": net, "gross": gross, "cost": cost_series, "exposure": exposure_series}


def _ols_beta(asset: list[float], market: list[float]) -> float | None:
    x = np.asarray(market, dtype=float)
    y = np.asarray(asset, dtype=float)
    if len(x) < BETA_MIN or np.var(x) == 0:
        return None
    return float(np.cov(y, x, ddof=0)[0, 1] / np.var(x))


def _benchmark_beta(
    final: dict[str, Any],
    expo_weights: dict[str, float],
    spy: dict[str, float],
    rf: dict[str, float],
    sessions: list[str],
) -> dict[str, dict[str, float]]:
    formation = str(final["run"]["formation_date"])
    decisions = _decision_dates(final)
    guard = _series(final["daily"]["TOPN_MV_GUARD_NO_NLP"])
    updates: dict[str, float] = {}
    for decision in decisions:
        history = [day for day in sessions if day < decision]
        if len(history) >= BETA_MIN:
            window = history[-BETA_WINDOW:]
            a = [guard.get(day, 0.0) - rf.get(day, 0.0) for day in window]
            m = [spy.get(day, 0.0) - rf.get(day, 0.0) for day in window]
            beta = _ols_beta(a, m)
            number = 0.0 if beta is None or not math.isfinite(beta) else beta
            updates[_execution_date(decision, sessions)] = min(1.0, max(0.0, number))
        else:
            # Before 126 sessions of FINAL history the weight is BMK_EXPO(GUARD)'s.
            updates[_execution_date(decision, sessions)] = expo_weights.get(decision, 1.0)
    weight = expo_weights.get(formation, 1.0)
    net: dict[str, float] = {}
    gross: dict[str, float] = {}
    cost_series: dict[str, float] = {}
    exposure_series: dict[str, float] = {}
    for day in sessions:
        exposure_series[day] = weight
        if day == formation:
            cost = COST * abs(weight)
            gross[day] = 0.0
            cost_series[day] = cost
            net[day] = -cost
            continue
        r_spy = spy.get(day, 0.0)
        port = weight * r_spy + (1.0 - weight) * rf.get(day, 0.0)
        drifted = weight * (1.0 + r_spy) / (1.0 + port) if 1.0 + port > 0 else weight
        cost = 0.0
        if day in updates:
            target = updates[day]
            cost = COST * abs(target - drifted)
            weight = target
        else:
            weight = drifted
        gross[day] = port
        cost_series[day] = cost
        net[day] = port - cost
    return {"net": net, "gross": gross, "cost": cost_series, "exposure": exposure_series}


def _stdev(values: list[float]) -> float:
    return float(np.std(values, ddof=1)) if len(values) > 1 else 0.0


def _benchmark_vol(
    final: dict[str, Any],
    b0: dict[str, Any],
    spy: dict[str, float],
    rf: dict[str, float],
    sessions: list[str],
) -> tuple[dict[str, dict[str, float]], int]:
    formation = str(final["run"]["formation_date"])
    decisions = _decision_dates(final)
    guard = _series(final["daily"]["TOPN_MV_GUARD_NO_NLP"])
    mb = _series(b0["daily"]["MARKOWITZ_BASE"])
    updates: dict[str, float] = {}
    capped = 0
    for decision in decisions:
        history = [day for day in sessions if day < decision][-VOL_WINDOW:]
        if len(history) < VOL_WINDOW // 2:
            continue
        s_final = _stdev([guard.get(day, 0.0) for day in history])
        s_mb = _stdev([mb.get(day, 0.0) for day in history])
        k = 1.0 if s_mb <= 0 else min(1.0, s_final / s_mb)
        if k >= 1.0:
            capped += 1
        updates[_execution_date(decision, sessions)] = k
    current = updates.get(formation, 1.0)
    net: dict[str, float] = {formation: -COST * current}
    gross: dict[str, float] = {formation: 0.0}
    cost_series: dict[str, float] = {formation: COST * current}
    exposure_series: dict[str, float] = {formation: current}
    previous = current
    for day in sessions:
        if day == formation:
            continue
        target = updates.get(day, previous)
        cost = COST * abs(target - previous)
        gross[day] = target * mb.get(day, 0.0) + (1.0 - target) * rf.get(day, 0.0)
        cost_series[day] = cost
        net[day] = gross[day] - cost
        exposure_series[day] = target
        previous = target
    return (
        {"net": net, "gross": gross, "cost": cost_series, "exposure": exposure_series},
        capped,
    )


def _statistics(values: dict[str, dict[str, float]]) -> dict[str, float]:
    days = sorted(values["net"])
    net = np.asarray([values["net"][day] for day in days], dtype=float)
    gross = np.asarray([values["gross"][day] for day in days], dtype=float)
    cost = np.asarray([values["cost"][day] for day in days], dtype=float)
    exposure = np.asarray([values["exposure"][day] for day in days], dtype=float)
    return {
        "gross_return": float(np.prod(1.0 + gross) - 1.0),
        "net_return": float(np.prod(1.0 + net) - 1.0),
        "cost_bps": float(10_000.0 * cost.sum()),
        "turnover": float(cost.sum() / COST),
        "mean_exposure": float(exposure.mean()),
        "n": float(len(days)),
    }


def _sessions(payload: dict[str, Any]) -> list[str]:
    return sorted(payload["daily"][payload["arms"][0]])


def _window(
    final: dict[str, Any],
    b0: dict[str, Any],
    spy: dict[str, float],
    rf: dict[str, float],
) -> dict[str, dict[str, float]]:
    sessions = _sessions(final)
    expo_guard = _benchmark_expo(final, "TOPN_MV_GUARD_NO_NLP", spy, sessions)
    expo_noguard = _benchmark_expo(final, "TOPN_MV_NOGUARD", spy, sessions)
    beta = _benchmark_beta(final, _exposure(final, "TOPN_MV_GUARD_NO_NLP"), spy, rf, sessions)
    vol, capped = _benchmark_vol(final, b0, spy, rf, sessions)
    return {
        "BMK_EXPO_GUARD": expo_guard,
        "BMK_EXPO_NOGUARD": expo_noguard,
        "BMK_BETA": beta,
        "BMK_VOL": vol,
        "_vol_capped_sessions": {"n": float(capped)},  # type: ignore[dict-item]
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    spy_raw = _load(SPY_FILE)
    spy = _spy_returns(spy_raw)
    result: dict[str, Any] = {}
    for window in WINDOWS:
        final = _load(out / "runs" / f"R1-B5-{window}.json")
        b0 = _load(out / "runs" / f"R1-B0-{window}.json")
        rf = _series(final["rf"])
        series = _window(final, b0, spy, rf)
        result[window] = {
            "metrics": {
                name: _metrics(values["net"], rf)
                for name, values in series.items()
                if not name.startswith("_")
            },
            "statistics": {
                name: _statistics(values)
                for name, values in series.items()
                if not name.startswith("_")
            },
            "vol_capped_sessions": series["_vol_capped_sessions"]["n"],
            "series": {
                name: {day: value for day, value in values["net"].items()}
                for name, values in series.items()
                if not name.startswith("_")
            },
            "gross": {
                name: {day: value for day, value in values["gross"].items()}
                for name, values in series.items()
                if not name.startswith("_")
            },
            "cost": {
                name: {day: value for day, value in values["cost"].items()}
                for name, values in series.items()
                if not name.startswith("_")
            },
            "exposure": {
                name: {day: value for day, value in values["exposure"].items()}
                for name, values in series.items()
                if not name.startswith("_")
            },
        }
    c1_final = out / "runs" / "R8-FINAL-C1.json"
    c1_b0 = out / "runs" / "R8-B0-C1.json"
    if c1_final.is_file() and c1_b0.is_file():
        final_c1 = _load(c1_final)
        b0_c1 = _load(c1_b0)
        rf = _series(final_c1["rf"])
        series = _window(final_c1, b0_c1, spy, rf)
        result["C1"] = {
            "metrics": {
                name: _metrics(values["net"], rf)
                for name, values in series.items()
                if not name.startswith("_")
            },
            "statistics": {
                name: _statistics(values)
                for name, values in series.items()
                if not name.startswith("_")
            },
            "vol_capped_sessions": series["_vol_capped_sessions"]["n"],
            "series": {
                name: {day: value for day, value in values["net"].items()}
                for name, values in series.items()
                if not name.startswith("_")
            },
            "gross": {
                name: {day: value for day, value in values["gross"].items()}
                for name, values in series.items()
                if not name.startswith("_")
            },
            "cost": {
                name: {day: value for day, value in values["cost"].items()}
                for name, values in series.items()
                if not name.startswith("_")
            },
            "exposure": {
                name: {day: value for day, value in values["exposure"].items()}
                for name, values in series.items()
                if not name.startswith("_")
            },
        }
    (out / "benchmarks.json").write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps({window: result[window]["metrics"] for window in result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
