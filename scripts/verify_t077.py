"""Read-only verification of T-077 (Carhart mu, turnover cap, turnover cost) on scratch databases.

    uv run python scripts/verify_t077.py diagnostics --db PATH --out DIR [--rf 0.045]
    uv run python scripts/verify_t077.py chains --end 2026-09-29 --out DIR [--rf 0.045] \\
        --run equilibrium:none=PATH --run equilibrium:0.5=PATH --run carhart:none=PATH ...

Both open every database ``mode=ro`` and write only markdown / JSON under ``--out``.

``diagnostics`` reads one database's risk models (one per monthly as-of) and reports, per date,
the cross-sectional mean / sd / range of mu for each estimator, the correlation of ``carhart`` mu
with ``equilibrium`` and ``james_stein``, lambda-bar, the prior mean / variance of each beta, the
median shrinkage weight and the assets flagged under the minimum; then, re-estimating the Carhart
betas from the stored panel, mu under three premia samples (the 1963 start the model uses, the
factor file's own 1926 start, and the 756-day window's mean): the median change and the rank
correlation.

``chains`` computes, **here and not in the product**, one chained series per (objective, estimator,
cap): each monthly book is held at its target weights (daily-rebalanced, as ``evaluate`` does)
until the next as-of, the last until ``--end``, net of ``bps / 1e4 * sum |w - w_prev|`` on each
book's first day (the first book pays ``sum |w|``), at 10 and at 15 bps. It reports the
annualized return (geometric), volatility, Sharpe ``(mean * 252 - rf) / vol``, maximum drawdown and
the mean monthly turnover, and checks that ``min_var`` (mu-free) is identical under both estimators.
The ``frontier`` series is the stored frontier's best-Sharpe point.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from kg_schema.queries import connect_ro
from quant.config import QuantSettings
from quant.factors import carhart_expected_returns, factor_premia, load_factors
from quant.panel import PanelError, build_return_panel
from quant.risk import historical_mean, james_stein_mean
from quant.turnover import turnover_between
from quant.universe import settings_gate

PPY = 252
_CONST_SD = 1e-9  # a cross-sectional sd below this is a constant vector
_AGREE_TOL = 1e-9  # the stored carhart mu and a direct call must agree this closely
OBJECTIVES = ("min_var", "tangency", "target_vol", "frontier")
ESTIMATORS = ("hist_mean", "james_stein", "equilibrium", "carhart")
FACTORS = ("MKT", "SMB", "HML", "MOM")
SQL_BOOKS = (
    "SELECT id, kind, engine_version, model_id FROM quant_portfolio "
    "WHERE as_of = ? AND frontier_k IS NULL AND kind = ?"
)

Weights = dict[int, float]


# -- the chain (pure) ----------------------------------------------------------------------


@dataclass(frozen=True)
class ChainStats:
    ann_return: float
    ann_vol: float
    sharpe: float
    max_drawdown: float
    mean_turnover: float  # mean monthly turnover, rebalances after the first
    first_turnover: float
    n_days: int


def chain_returns(
    books: Sequence[tuple[str, Weights]],
    daily: Mapping[str, Mapping[int, float]],
    end: str,
    *,
    bps: float,
) -> tuple[list[tuple[str, float]], list[float]]:
    """``[(date, net daily return)]`` and each book's turnover, for books held in turn.

    Book ``i`` (dated ``D_i``) is held on the trading days in ``(D_i, D_{i+1}]``, the last until
    *end*, at its target weights (a name with no return that day contributes 0, its weight kept).
    On the first of those days the book pays ``bps / 1e4 * sum |w - w_prev|``."""
    dates = sorted(daily)
    out: list[tuple[str, float]] = []
    turnovers: list[float] = []
    prev: Weights | None = None
    for i, (as_of, w) in enumerate(books):
        stop = books[i + 1][0] if i + 1 < len(books) else end
        turn = turnover_between(w, prev)
        turnovers.append(turn)
        cost = bps / 10_000.0 * turn
        first = True
        for d in dates:
            if d <= as_of or d > stop:
                continue
            r = sum(weight * daily[d].get(a, 0.0) for a, weight in w.items())
            out.append((d, r - cost if first else r))
            first = False
        prev = w
    return out, turnovers


def chain_stats(
    series: Sequence[tuple[str, float]], turnovers: Sequence[float], rf: float
) -> ChainStats:
    r = np.array([x for _, x in series], dtype=np.float64)
    wealth = np.cumprod(1.0 + r)
    n = len(r)
    ann_return = float(wealth[-1] ** (PPY / n) - 1.0)
    vol = float(r.std(ddof=1) * math.sqrt(PPY))
    sharpe = float((r.mean() * PPY - rf) / vol) if vol > 0 else float("nan")
    peak = np.maximum.accumulate(np.concatenate(([1.0], wealth)))[1:]
    mdd = float((wealth / peak - 1.0).min())
    later = list(turnovers[1:])
    mean_turn = float(np.mean(later)) if later else float("nan")
    return ChainStats(ann_return, vol, sharpe, mdd, mean_turn, float(turnovers[0]), n)


# -- reading the databases -----------------------------------------------------------------


def _all_daily(conn: Any, first: str, end: str) -> dict[str, dict[int, float]]:
    out: dict[str, dict[int, float]] = {}
    for row in conn.execute(
        "SELECT asset_id, obs_date, tr_log_return FROM quant_return_daily "
        "WHERE engine_version = 'qret-v2' AND obs_date > ? AND obs_date <= ? "
        "AND tr_log_return IS NOT NULL",
        (first, end),
    ):
        out.setdefault(str(row["obs_date"]), {})[int(row["asset_id"])] = math.expm1(
            float(row["tr_log_return"])
        )
    return out


def _book_weights(conn: Any, portfolio_id: int) -> Weights:
    return {
        int(r["asset_id"]): float(r["weight"])
        for r in conn.execute(
            "SELECT asset_id, weight FROM quant_position WHERE portfolio_id = ? "
            "AND valid_to IS NULL",
            (portfolio_id,),
        )
    }


def _frontier_best(conn: Any, model_id: int) -> Weights | None:
    row = conn.execute(
        "SELECT weights_json FROM quant_frontier_point WHERE model_id = ? "
        "AND status IN ('optimal', 'optimal_inaccurate') AND sharpe IS NOT NULL "
        "ORDER BY sharpe DESC, k LIMIT 1",
        (model_id,),
    ).fetchone()
    return {int(a): float(w) for a, w in json.loads(row["weights_json"]).items()} if row else None


def _suffix(estimator: str, cap: str) -> list[str]:
    return ([] if estimator == "equilibrium" else [f"mu-{estimator}"]) + (
        [] if cap == "none" else [f"to-{float(cap):g}"]
    )


def load_books(
    db: str, estimator: str, cap: str, as_ofs: Sequence[str]
) -> dict[str, list[tuple[str, Weights]]]:
    """``objective -> [(as_of, weights)]`` of the books one configuration wrote."""
    conn = connect_ro(db)
    try:
        out: dict[str, list[tuple[str, Weights]]] = {o: [] for o in OBJECTIVES}
        want = _suffix(estimator, cap)
        for as_of in as_ofs:
            for obj in OBJECTIVES:
                if obj == "frontier":
                    m = conn.execute(
                        "SELECT id FROM quant_risk_model WHERE as_of = ?", (as_of,)
                    ).fetchone()
                    w = _frontier_best(conn, int(m["id"])) if m else None
                else:
                    rows = [r for r in conn.execute(SQL_BOOKS, (as_of, obj)) if _extras(r) == want]
                    w = _book_weights(conn, int(rows[0]["id"])) if rows else None
                if w is None:
                    raise SystemExit(f"{db}: no {obj} book at {as_of} for {estimator}:{cap}")
                out[obj].append((as_of, w))
        return out
    finally:
        conn.close()


def _extras(row: Any) -> list[str]:
    """The suffixes after ``opt-vN+<tag>``: ``[]`` for a default book, else ``mu-X`` / ``to-C``."""
    return str(row["engine_version"]).split("+")[2:]


def _as_ofs(db: str) -> list[str]:
    conn = connect_ro(db)
    try:
        return [
            str(r[0]) for r in conn.execute("SELECT as_of FROM quant_risk_model ORDER BY as_of")
        ]
    finally:
        conn.close()


# -- chains --------------------------------------------------------------------------------


def run_chains(
    runs: Mapping[tuple[str, str], str], end: str, rf: float
) -> tuple[str, dict[str, Any]]:
    any_db = next(iter(runs.values()))
    as_ofs = _as_ofs(any_db)
    conn = connect_ro(any_db)
    try:
        daily = _all_daily(conn, as_ofs[0], end)
    finally:
        conn.close()
    books = {key: load_books(path, key[0], key[1], as_ofs) for key, path in runs.items()}
    results: dict[str, Any] = {}
    lines = [
        f"Chained monthly books, {as_ofs[0]} .. {end} ({len(as_ofs)} books), rf {rf:.4f}.",
        "",
        "| objective | estimator | cap | bps | ann. return | vol | Sharpe | max DD | mean monthly turnover | first turnover |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for obj in OBJECTIVES:
        for (est, cap), per_obj in sorted(books.items()):
            for bps in (10.0, 15.0):
                series, turns = chain_returns(per_obj[obj], daily, end, bps=bps)
                s = chain_stats(series, turns, rf)
                results[f"{obj}|{est}|{cap}|{bps:g}"] = s.__dict__
                lines.append(
                    f"| {obj} | {est} | {cap} | {bps:g} | {s.ann_return:.2%} | {s.ann_vol:.2%} | "
                    f"{s.sharpe:.2f} | {s.max_drawdown:.2%} | {s.mean_turnover:.3f} | "
                    f"{s.first_turnover:.3f} |"
                )
    lines += ["", "### min_var is mu-free: identical under both estimators", ""]
    for cap in sorted({c for _, c in books}):
        ests = sorted(e for e, c in books if c == cap)
        if len(ests) < 2:  # noqa: PLR2004
            continue
        a, b = books[(ests[0], cap)]["min_var"], books[(ests[1], cap)]["min_var"]
        worst = max(
            max(abs(wa.get(k, 0.0) - wb.get(k, 0.0)) for k in set(wa) | set(wb))
            for (_, wa), (_, wb) in zip(a, b, strict=True)
        )
        sa, ta = chain_returns(a, daily, end, bps=10.0)
        sb, tb = chain_returns(b, daily, end, bps=10.0)
        gap = max(abs(x - y) for (_, x), (_, y) in zip(sa, sb, strict=True))
        results[f"min_var_identity|{cap}"] = {"max_weight_diff": worst, "max_daily_diff": gap}
        lines.append(
            f"- cap {cap}: `{ests[0]}` vs `{ests[1]}` — max |weight difference| {worst:.2e}, "
            f"max |daily return difference| {gap:.2e}, turnovers equal: "
            f"{np.allclose(ta, tb, atol=1e-9)}"
        )
    return "\n".join(lines) + "\n", results


# -- diagnostics ---------------------------------------------------------------------------


def _rank(x: np.ndarray) -> np.ndarray:
    order = np.argsort(x, kind="stable")
    ranks = np.empty(len(x))
    ranks[order] = np.arange(len(x))
    return ranks


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.corrcoef(_rank(a), _rank(b))[0, 1])


def _stats(v: np.ndarray) -> dict[str, float]:
    return {
        "mean": float(v.mean()),
        "sd": float(v.std(ddof=1)),
        "min": float(v.min()),
        "max": float(v.max()),
    }


def _corr(a: np.ndarray, b: np.ndarray) -> dict[str, float]:
    """Pearson and Spearman; NaN when either vector is constant (james_stein shrinks to a single
    value when the cross-sectional dispersion of the sample means is below its noise)."""
    if a.std() < _CONST_SD or b.std() < _CONST_SD:
        return {"pearson": float("nan"), "spearman": float("nan")}
    return {"pearson": float(np.corrcoef(a, b)[0, 1]), "spearman": _spearman(a, b)}


def estimate_date(conn: Any, settings: QuantSettings, as_of: str) -> dict[str, Any]:
    """The Carhart estimator called directly on the panel the risk model would be built from at
    *as_of* (the same gate, panel and factor data), beside ``james_stein`` and ``hist_mean`` from
    the same panel, and ``equilibrium`` when a stored model has it. Needs no market caps."""
    gate = settings_gate(
        conn, settings, as_of=as_of, return_engine_version=settings.return_engine_version
    )
    ids = list(gate.asset_ids)
    dropped: list[int] = []
    while True:
        try:
            panel = build_return_panel(
                conn,
                as_of=as_of,
                lookback_days=settings.lookback_days,
                min_history_days=settings.min_history_days,
                universe_asset_ids=ids,
                return_engine_version=settings.return_engine_version,
            )
            break
        except PanelError as exc:
            # a name with a gap longer than the panel heals (the product's build refuses the whole
            # model): drop it here and record it, so the estimator can still run on the rest
            bad = {int(a) for a in re.findall(r"(\d+): [\d.]+", str(exc))} & set(ids)
            if not bad:
                raise
            dropped += sorted(bad)
            ids = [a for a in ids if a not in bad]
    data = load_factors(as_of)
    rf = settings.risk_free_rate
    res = carhart_expected_returns(
        panel.returns,
        panel.dates,
        panel.asset_ids,
        data,
        rf_annual=rf,
        min_obs=settings.carhart_min_obs,
    )
    mus: dict[str, np.ndarray] = {
        "hist_mean": historical_mean(panel.returns, periods_per_year=PPY),
        "james_stein": james_stein_mean(panel.returns, periods_per_year=PPY),
        "carhart": res.mu,
    }
    model = conn.execute(
        "SELECT id FROM quant_risk_model WHERE as_of = ? ORDER BY id DESC LIMIT 1", (as_of,)
    ).fetchone()
    if model is not None:
        stored = {
            int(r["asset_id"]): float(r["mu"])
            for r in conn.execute(
                "SELECT asset_id, mu FROM quant_expected_return WHERE model_id = ? "
                "AND mu_model = 'equilibrium'",
                (model["id"],),
            )
        }
        if stored:
            mus["equilibrium"] = np.array([stored[a] for a in panel.asset_ids])
        carhart_stored = {
            int(r["asset_id"]): float(r["mu"])
            for r in conn.execute(
                "SELECT asset_id, mu FROM quant_expected_return WHERE model_id = ? "
                "AND mu_model = 'carhart'",
                (model["id"],),
            )
        }
        if carhart_stored:  # the stored model and this direct call must agree
            diff = max(
                abs(carhart_stored[a] - m) for a, m in zip(panel.asset_ids, res.mu, strict=True)
            )
            assert diff < _AGREE_TOL, (
                f"stored carhart mu differs from the direct estimate by {diff}"
            )
    rec = res.record
    out: dict[str, Any] = {
        "as_of": as_of,
        "n_assets": panel.n_assets,
        "dropped_for_gaps": dropped,
        "regression_n_dates": rec["regression_n_dates"],
        "panel_gap_after_factor_end": rec["panel_gap_after_factor_end"],
        "flagged": len(rec["flagged_asset_ids"]),
        "lambda_bar": rec["lambda_bar"],
        "beta_bar": rec["beta_bar"],
        "beta_prior_var": rec["beta_prior_var"],
        "shrinkage_weight": rec["shrinkage_weight"],
        "mu": {k: _stats(v) for k, v in mus.items()},
        "corr_carhart": {k: _corr(res.mu, v) for k, v in mus.items() if k != "carhart"},
        "rf": rf,
        "premia_sensitivity": {},
    }
    base = res.mu
    for label, prem in (
        ("1963", factor_premia(data)),
        ("1926", factor_premia(data, start=None)),
        ("756d", factor_premia(data, start=None, last_n=756)),
    ):
        alt = rf + res.betas.betas @ prem
        out["premia_sensitivity"][label] = {
            "lambda": [float(x) for x in prem],
            "median_delta_pp": float(np.median(alt - base)) * 100,
            "spearman": _spearman(alt, base),
            "mu_mean": float(alt.mean()),
        }
    return out


def run_diagnostics(db: str, universe_db: str, rf: float) -> dict[str, Any]:
    conn = connect_ro(db)
    try:
        settings = QuantSettings(
            db_path=Path(db), universe_db_path=Path(universe_db), risk_free_rate=rf
        )
        dates = _month_ends(conn)
        return {
            "db": db,
            "universe_db": universe_db,
            "dates": [estimate_date(conn, settings, d) for d in dates],
        }
    finally:
        conn.close()


def _month_ends(conn: Any) -> list[str]:
    """The last return date of each month, from the first date with 756 days of history to the
    last month-end the factor file covers."""
    days = [
        str(r[0])
        for r in conn.execute(
            "SELECT DISTINCT obs_date FROM quant_return_daily WHERE engine_version = 'qret-v2' ORDER BY 1"
        )
    ]
    last_factor = load_factors("2100-01-01").last_date
    by_month: dict[str, str] = {}
    for i, d in enumerate(days):
        if i + 1 >= 756 and d <= last_factor:  # noqa: PLR2004
            by_month[d[:7]] = d
    full = {d[:7] for d in days}
    return [d for m, d in sorted(by_month.items()) if m in full and d == _last_of_month(days, m)]


def _last_of_month(days: Sequence[str], month: str) -> str:
    return max(d for d in days if d.startswith(month))


_FAC = ("MKT", "SMB", "HML", "MOM")


def _fmt_corr(c: dict[str, float]) -> str:
    return (
        "n/a (constant)"
        if math.isnan(c["pearson"])
        else f"{c['pearson']:.3f} / {c['spearman']:.3f}"
    )


def render_diagnostics(rec: dict[str, Any], title: str) -> str:
    rows = rec["dates"]
    has_eq = "equilibrium" in rows[0]["mu"]
    est = [e for e in ("hist_mean", "james_stein", "equilibrium", "carhart") if e in rows[0]["mu"]]
    out = [
        f"## {title}",
        "",
        f"{len(rows)} monthly as-of dates, {rows[0]['as_of']} .. {rows[-1]['as_of']}; "
        f"N {min(r['n_assets'] for r in rows)}..{max(r['n_assets'] for r in rows)} assets; "
        f"rf {rows[0]['rf']:.4f}."
        + (
            ""
            if has_eq
            else " **`equilibrium` is not available on this panel** (it needs market caps, i.e. cover-page share counts the production copy does not hold)."
        ),
        "",
    ]
    out += [
        "### mu by estimator (cross-sectional mean / sd / min / max, annualized)",
        "",
        "| as-of | N | " + " | ".join(f"{e} mean | {e} sd | {e} range" for e in est) + " |",
        "|---|---|" + "---|---|---|" * len(est),
    ]
    for r in rows:
        cells = " | ".join(
            f"{r['mu'][e]['mean']:.4f} | {r['mu'][e]['sd']:.4f} | {r['mu'][e]['min']:.3f} .. {r['mu'][e]['max']:.3f}"
            for e in est
        )
        out.append(f"| {r['as_of']} | {r['n_assets']} | {cells} |")
    others = [e for e in est if e != "carhart"]
    out += [
        "",
        "### correlation of carhart mu with the other estimators (Pearson / Spearman)",
        "",
        "| as-of | " + " | ".join(others) + " |",
        "|---|" + "---|" * len(others),
    ]
    for r in rows:
        out.append(
            f"| {r['as_of']} | "
            + " | ".join(
                f"{r['corr_carhart'][e]['pearson']:.3f} / {r['corr_carhart'][e]['spearman']:.3f}"
                for e in others
            )
            + " |"
        )
    out += [
        "",
        "### premia, beta prior, shrinkage and flags",
        "",
        "| as-of | reg. dates | gap | flagged | "
        + " | ".join(f"λ̄ {f}" for f in _FAC)
        + " | "
        + " | ".join(f"β̄ {f}" for f in _FAC)
        + " | "
        + " | ".join(f"σ² {f}" for f in _FAC)
        + " | "
        + " | ".join(f"w {f} min/med/max" for f in _FAC)
        + " |",
        "|" + "---|" * (4 + 16),
    ]
    for r in rows:
        w = r["shrinkage_weight"]
        out.append(
            f"| {r['as_of']} | {r['regression_n_dates']} | {r['panel_gap_after_factor_end']} | {r['flagged']} | "
            + " | ".join(f"{r['lambda_bar'][f]:.4f}" for f in _FAC)
            + " | "
            + " | ".join(f"{r['beta_bar'][f]:.3f}" for f in _FAC)
            + " | "
            + " | ".join(f"{r['beta_prior_var'][f]:.4f}" for f in _FAC)
            + " | "
            + " | ".join(f"{w[f]['min']:.2f}/{w[f]['median']:.2f}/{w[f]['max']:.2f}" for f in _FAC)
            + " |"
        )
    out += [
        "",
        "### premia sensitivity (the same shrunk betas; median change in mu vs the 1963 start, and rank correlation)",
        "",
        "| as-of | "
        + " | ".join(f"{k} λ̄ (MKT, SMB, HML, MOM)" for k in ("1963", "1926", "756d"))
        + " | Δμ 1926 (pp) | rank corr 1926 | Δμ 756d (pp) | rank corr 756d |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        ps = r["premia_sensitivity"]
        lam = " | ".join(
            ", ".join(f"{x:.3f}" for x in ps[k]["lambda"]) for k in ("1963", "1926", "756d")
        )
        out.append(
            f"| {r['as_of']} | {lam} | {ps['1926']['median_delta_pp']:+.2f} | {ps['1926']['spearman']:.3f} | "
            f"{ps['756d']['median_delta_pp']:+.2f} | {ps['756d']['spearman']:.3f} |"
        )
    mean = lambda k, f: float(np.mean([r["premia_sensitivity"][k][f] for r in rows]))  # noqa: E731
    out.append(
        f"| **mean over dates** | | | | {mean('1926', 'median_delta_pp'):+.2f} | {mean('1926', 'spearman'):.3f} | "
        f"{mean('756d', 'median_delta_pp'):+.2f} | {mean('756d', 'spearman'):.3f} |"
    )
    return "\n".join(out) + "\n"


def render_prior_comparison(small: dict[str, Any], big: dict[str, Any]) -> str:
    """How far the small panel's beta prior (mean and variance per factor) is from the large one's."""
    by_date = {r["as_of"]: r for r in big["dates"]}
    rows = [(r, by_date[r["as_of"]]) for r in small["dates"] if r["as_of"] in by_date]
    out = [
        "## beta prior: small panel vs large panel",
        "",
        "| as-of | N small / large | "
        + " | ".join(f"β̄ {f} small / large" for f in _FAC)
        + " | "
        + " | ".join(f"σ² {f} small / large (ratio)" for f in _FAC)
        + " |",
        "|" + "---|" * (2 + 8),
    ]
    d_mean = {f: [] for f in _FAC}
    ratio = {f: [] for f in _FAC}
    for a, b in rows:
        cells = []
        for f in _FAC:
            cells.append(f"{a['beta_bar'][f]:.3f} / {b['beta_bar'][f]:.3f}")
            d_mean[f].append(abs(a["beta_bar"][f] - b["beta_bar"][f]))
        for f in _FAC:
            q = a["beta_prior_var"][f] / b["beta_prior_var"][f]
            ratio[f].append(q)
            cells.append(f"{a['beta_prior_var'][f]:.4f} / {b['beta_prior_var'][f]:.4f} ({q:.2f})")
        out.append(
            f"| {a['as_of']} | {a['n_assets']} / {b['n_assets']} | " + " | ".join(cells) + " |"
        )
    out += [
        "",
        "| factor | mean abs(β̄ small - β̄ large) | median σ² ratio (small / large) | min .. max ratio |",
        "|---|---|---|---|",
    ]
    for f in _FAC:
        out.append(
            f"| {f} | {np.mean(d_mean[f]):.3f} | {np.median(ratio[f]):.2f} | {min(ratio[f]):.2f} .. {max(ratio[f]):.2f} |"
        )
    return "\n".join(out) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("diagnostics")
    d.add_argument("--db", required=True)
    d.add_argument("--universe-db", required=True)
    d.add_argument("--label", required=True)
    d.add_argument("--out", type=Path, required=True)
    d.add_argument("--rf", type=float, default=0.045)
    k = sub.add_parser("compare-priors")
    k.add_argument("--small", type=Path, required=True)
    k.add_argument("--large", type=Path, required=True)
    k.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("chains")
    c.add_argument("--run", action="append", required=True, help="ESTIMATOR:CAP=PATH")
    c.add_argument("--end", required=True)
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--rf", type=float, default=0.045)
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    if args.cmd == "diagnostics":
        rec = run_diagnostics(args.db, args.universe_db, args.rf)
        text = render_diagnostics(rec, args.label)
        (args.out / f"diagnostics_{args.label}.md").write_text(text)
        (args.out / f"diagnostics_{args.label}.json").write_text(json.dumps(rec, indent=1))
    elif args.cmd == "compare-priors":
        small, big = json.loads(args.small.read_text()), json.loads(args.large.read_text())
        text = render_prior_comparison(small, big)
        (args.out / "prior_comparison.md").write_text(text)
    else:
        runs = {}
        for spec in args.run:
            key, _, path = spec.partition("=")
            est, _, cap = key.partition(":")
            runs[(est, cap)] = path
        text, rec = run_chains(runs, args.end, args.rf)
        (args.out / "chains.md").write_text(text)
        (args.out / "chains.json").write_text(json.dumps(rec, indent=1))
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
