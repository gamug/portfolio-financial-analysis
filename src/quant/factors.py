"""The Carhart four-factor expected-return estimator with Vasicek-shrunk betas (T-077).

``mu_i = rf + sum_k beta_shrunk[i, k] * lambda_bar[k]`` over ``{MKT, SMB, HML, MOM}``.

* **Factors.** Kenneth French's daily "Fama/French 3 Factors" and "Momentum Factor" files,
  vendored byte-for-byte under ``quant/data/`` beside a manifest (URL, download date, the
  library's own version statement, SHA-256, first and last date). :func:`load_factors` refuses a
  file whose SHA-256 differs from the manifest, converts percent to decimals, and keeps only rows
  dated on or before the as-of date (nothing later is parsed into the result). No network at
  runtime.
* **Betas.** Per asset, OLS of the daily excess simple return (the panel's log returns, converted
  with ``expm1``, minus French's daily RF) on ``[1, Mkt-RF, SMB, HML, Mom]`` over the dates where
  the panel and both factor files exist. Each beta's sampling variance is
  ``s2_e * [(X'X)^-1]_kk``. An asset with fewer than ``min_obs`` observations takes the prior mean
  (shrinkage weight 0) and is flagged.
* **Vasicek (1973)**, per factor ``k`` separately (a declared simplification of the multivariate
  prior): prior mean = the equal-weighted cross-sectional mean of the raw betas, prior variance =
  their cross-sectional variance, ``w = var / (var + se2)``,
  ``beta_shrunk = w * beta_hat + (1 - w) * prior_mean``.
* **Premia.** ``lambda_bar[k]`` = the arithmetic mean of factor ``k``'s daily returns from
  ``1963-07-01`` to the as-of date, times ``periods_per_year``.
* The regression intercept is estimated and dropped: no alpha enters ``mu``.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

Mat = npt.NDArray[np.float64]
Vec = npt.NDArray[np.float64]

FACTOR_NAMES: tuple[str, ...] = ("MKT", "SMB", "HML", "MOM")
DATA_DIR = Path(__file__).resolve().parent / "data"
MANIFEST_NAME = "manifest.json"
PREMIA_START = "1963-07-01"  # the Fama-French / Carhart sample start
MIN_OBS = 504  # two thirds of the 756-day window
_PCT = 100.0
_MISSING_SENTINEL = -99.0  # French marks missing data -99.99 / -999
_DATE_LEN = 8
_N_FACTORS = len(FACTOR_NAMES)
_N_REG = _N_FACTORS + 1  # intercept + the four factors


class FactorDataError(RuntimeError):
    """The factor data cannot support the estimator."""


class FactorIntegrityError(FactorDataError):
    """The vendored files are not what the manifest pins (a SHA-256 mismatch, a missing or
    malformed file). A hard failure: nothing is estimated from data that is not the data pinned."""


class FactorCoverageError(FactorDataError):
    """The files are intact but do not cover the as-of date / panel (too little overlap). The risk
    model builds the other estimators anyway and records why this one is missing."""


@dataclass(frozen=True)
class FactorData:
    """Daily factor returns in **decimals**, dates ascending, all ``<= as_of``."""

    as_of: str
    dates: list[str]
    factors: Mat  # (T, 4): Mkt-RF, SMB, HML, Mom
    rf: Vec  # (T,) French's daily risk-free return
    library_version: str
    sha256: dict[str, str]  # file name -> SHA-256 (verified)
    file_last_date: str  # the files' last date per the manifest (may be after as_of)

    @property
    def last_date(self) -> str:
        return self.dates[-1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


def _read_rows(path: Path, n_values: int, *, as_of: str) -> dict[str, list[float]]:
    """``{iso date: [decimal values]}`` for the rows dated ``<= as_of``. Files are ascending, so
    parsing stops at the first later date. A row with a missing-data sentinel is skipped."""
    out: dict[str, list[float]] = {}
    last = ""
    with path.open(newline="", encoding="latin-1") as fh:
        for raw in csv.reader(fh):
            key = raw[0].strip() if raw else ""
            if len(key) != _DATE_LEN or not key.isdigit() or len(raw) < n_values + 1:
                continue
            day = _iso(key)
            if day <= last:
                raise FactorIntegrityError(f"{path.name}: dates not strictly ascending at {day}")
            last = day
            if day > as_of:
                break
            try:
                vals = [float(x) for x in raw[1 : n_values + 1]]
            except ValueError as exc:
                raise FactorIntegrityError(f"{path.name}: unreadable row {raw!r}") from exc
            if any(v <= _MISSING_SENTINEL for v in vals):
                continue
            out[day] = [v / _PCT for v in vals]
    return out


def load_factors(as_of: str, data_dir: Path | None = None) -> FactorData:
    """The vendored factors as of *as_of* (point in time: rows dated after it are not kept).

    Raises :class:`FactorIntegrityError` on a missing/malformed manifest or file or a SHA-256 that
    differs from the manifest's, and :class:`FactorCoverageError` when no row is dated on or
    before *as_of*."""
    root = data_dir or DATA_DIR
    try:
        manifest: dict[str, Any] = json.loads((root / MANIFEST_NAME).read_text())
        files = manifest["files"]
        ff3, mom = files["ff3"], files["mom"]
        version = str(manifest["library_version"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise FactorIntegrityError(
            f"factor manifest {root / MANIFEST_NAME} unusable: {exc}"
        ) from exc
    digests: dict[str, str] = {}
    for spec in (ff3, mom):
        path = root / str(spec["path"])
        try:
            got = _sha256(path)
        except OSError as exc:
            raise FactorIntegrityError(f"factor file {path} unreadable: {exc}") from exc
        if got != spec["sha256"]:
            raise FactorIntegrityError(
                f"{path.name}: SHA-256 {got} does not match the manifest's {spec['sha256']}; "
                "refusing to estimate from a factor file that is not the pinned one"
            )
        digests[path.name] = got
    rows3 = _read_rows(root / str(ff3["path"]), 4, as_of=as_of)
    rowsm = _read_rows(root / str(mom["path"]), 1, as_of=as_of)
    dates = sorted(set(rows3) & set(rowsm))
    if not dates:
        raise FactorCoverageError(
            f"no factor row is dated on or before {as_of} (files start "
            f"{min(str(ff3['first_date']), str(mom['first_date']))})"
        )
    factors = np.array(
        [[rows3[d][0], rows3[d][1], rows3[d][2], rowsm[d][0]] for d in dates], dtype=np.float64
    )
    rf = np.array([rows3[d][3] for d in dates], dtype=np.float64)
    return FactorData(
        as_of=as_of,
        dates=dates,
        factors=factors,
        rf=rf,
        library_version=version,
        sha256=digests,
        file_last_date=min(str(ff3["last_date"]), str(mom["last_date"])),
    )


def factor_premia(
    data: FactorData,
    *,
    start: str | None = PREMIA_START,
    last_n: int | None = None,
    periods_per_year: int = 252,
) -> Vec:
    """Annualized arithmetic-mean premium of each factor: the mean of the daily returns on dates
    ``>= start`` (or of the last *last_n* rows), times *periods_per_year*."""
    ix = [i for i, d in enumerate(data.dates) if start is None or d >= start]
    if last_n is not None:
        ix = ix[-last_n:]
    if not ix:
        raise FactorCoverageError(
            f"no factor row on or after {start} and on or before {data.as_of}"
        )
    return np.asarray(data.factors[ix].mean(axis=0) * periods_per_year, dtype=np.float64)


# -- the regression -----------------------------------------------------------


@dataclass(frozen=True)
class FactorBetas:
    """Raw OLS output per asset. Rows of an under-observed asset are NaN."""

    betas: Mat  # (N, 4) raw betas
    se2: Mat  # (N, 4) sampling variances s2_e * [(X'X)^-1]_kk
    alpha: Vec  # (N,) intercept (estimated, dropped from mu)
    n_obs: npt.NDArray[np.int64]  # (N,) observations used


def ols_factor_betas(excess: Mat, factors: Mat, *, min_obs: int = MIN_OBS) -> FactorBetas:
    """OLS of each column of *excess* ``(T, N)`` on ``[1, factors]`` ``(T, 4)``. A NaN in a column
    drops that row for that asset only; an asset left with fewer than *min_obs* rows (or no
    residual degrees of freedom) gets NaN betas."""
    t, n = excess.shape
    if factors.shape != (t, _N_FACTORS):
        raise ValueError(f"factors must be ({t}, {_N_FACTORS}), got {factors.shape}")
    betas = np.full((n, _N_FACTORS), np.nan)
    se2 = np.full((n, _N_FACTORS), np.nan)
    alpha = np.full(n, np.nan)
    finite = np.isfinite(excess)
    n_obs = finite.sum(axis=0).astype(np.int64)
    groups: dict[bytes, list[int]] = {}
    for j in range(n):
        if n_obs[j] >= max(min_obs, _N_REG + 1):
            groups.setdefault(finite[:, j].tobytes(), []).append(j)
    for cols in groups.values():
        rows = finite[:, cols[0]]
        x = np.column_stack([np.ones(int(rows.sum())), factors[rows]])
        y = excess[np.ix_(rows, cols)]
        xtx_inv = np.linalg.inv(x.T @ x)
        coef = xtx_inv @ (x.T @ y)  # (5, len(cols))
        resid = y - x @ coef
        s2 = (resid**2).sum(axis=0) / (x.shape[0] - _N_REG)
        diag = np.diag(xtx_inv)[1:]  # the four factor coefficients
        alpha[cols] = coef[0]
        betas[cols] = coef[1:].T
        se2[cols] = np.outer(s2, diag)
    return FactorBetas(betas=betas, se2=se2, alpha=alpha, n_obs=n_obs)


@dataclass(frozen=True)
class Shrinkage:
    betas: Mat  # (N, 4) shrunk
    weights: Mat  # (N, 4) w = var / (var + se2); 0 for a flagged asset
    prior_mean: Vec  # (4,)
    prior_var: Vec  # (4,)
    flagged: list[int]  # column indices of assets under min_obs


def vasicek_shrink(raw: FactorBetas, *, min_obs: int = MIN_OBS) -> Shrinkage:
    """Per-factor Vasicek (1973) shrinkage toward the cross-sectional equal-weighted mean beta,
    with the cross-sectional variance of the raw betas as the prior variance (``ddof=1``)."""
    n = raw.betas.shape[0]
    ok = np.isfinite(raw.betas).all(axis=1) & (raw.n_obs >= min_obs)
    if not ok.any():
        raise FactorCoverageError(f"no asset has {min_obs} observations against the factors")
    prior_mean = raw.betas[ok].mean(axis=0)
    prior_var = raw.betas[ok].var(axis=0, ddof=1) if ok.sum() > 1 else np.zeros(_N_FACTORS)
    weights = np.zeros((n, _N_FACTORS))
    shrunk = np.tile(prior_mean, (n, 1))
    denom = prior_var + raw.se2[ok]
    w_ok = np.divide(prior_var, denom, out=np.zeros_like(denom), where=denom > 0)
    weights[ok] = w_ok
    shrunk[ok] = w_ok * raw.betas[ok] + (1.0 - w_ok) * prior_mean
    flagged = [int(j) for j in np.flatnonzero(~ok)]
    return Shrinkage(shrunk, weights, prior_mean, prior_var, flagged)


# -- the estimator ------------------------------------------------------------


@dataclass(frozen=True)
class CarhartResult:
    mu: Vec  # (N,) annualized total-return mu, aligned with the panel's columns
    record: dict[str, Any]  # what the risk model's manifest keeps
    betas: Shrinkage
    raw: FactorBetas
    premia: Vec


def _quantiles(w: Vec) -> dict[str, float]:
    return {
        "min": float(np.min(w)),
        "median": float(np.median(w)),
        "max": float(np.max(w)),
    }


def carhart_expected_returns(  # noqa: PLR0913 - keyword-only estimator knobs
    log_returns: Mat,
    panel_dates: list[str],
    asset_ids: list[int],
    data: FactorData,
    *,
    rf_annual: float,
    periods_per_year: int = 252,
    min_obs: int = MIN_OBS,
    premia_start: str = PREMIA_START,
) -> CarhartResult:
    """``mu_i = rf + sum_k beta_shrunk[i, k] * lambda_bar[k]`` for a panel.

    *log_returns* is the panel's ``(T, N)`` total-return **log** matrix on *panel_dates*; it is
    converted to simple returns before the regression because the factors are simple returns.
    The regression runs on the overlap of the panel's dates and the factor dates only; a panel
    that runs past the files' last date is regressed on what overlaps and the gap is recorded.
    Raises :class:`FactorCoverageError` when the overlap is under *min_obs* dates."""
    t, n = log_returns.shape
    if len(panel_dates) != t or len(asset_ids) != n:
        raise ValueError("panel_dates / asset_ids do not match the return matrix")
    pos = {d: i for i, d in enumerate(data.dates)}
    rows = [i for i, d in enumerate(panel_dates) if d in pos]
    if len(rows) < min_obs:
        raise FactorCoverageError(
            f"only {len(rows)} of the panel's {t} dates overlap the factor data (last factor "
            f"date {data.last_date}); need {min_obs}"
        )
    f_ix = [pos[panel_dates[i]] for i in rows]
    factors = data.factors[f_ix]
    simple = np.expm1(np.asarray(log_returns, dtype=np.float64)[rows])
    excess = simple - data.rf[f_ix][:, None]
    raw = ols_factor_betas(excess, factors, min_obs=min_obs)
    shr = vasicek_shrink(raw, min_obs=min_obs)
    premia = factor_premia(data, start=premia_start, periods_per_year=periods_per_year)
    mu = rf_annual + shr.betas @ premia
    reg_dates = [panel_dates[i] for i in rows]
    flagged = set(shr.flagged)
    kept = [j for j in range(n) if j not in flagged]
    record: dict[str, Any] = {
        "factor_library_version": data.library_version,
        "factor_files_sha256": dict(sorted(data.sha256.items())),
        "factor_file_last_date": data.file_last_date,
        "regression_date_start": reg_dates[0],
        "regression_date_end": reg_dates[-1],
        "regression_n_dates": len(rows),
        "panel_n_dates": t,
        "panel_dates_without_factor": t - len(rows),
        "panel_gap_after_factor_end": sum(1 for d in panel_dates if d > data.last_date),
        "min_obs": min_obs,
        "premia_start": premia_start,
        "premia_last_date": data.last_date,
        "lambda_bar": dict(zip(FACTOR_NAMES, (float(x) for x in premia), strict=True)),
        "beta_bar": dict(zip(FACTOR_NAMES, (float(x) for x in shr.prior_mean), strict=True)),
        "beta_prior_var": dict(zip(FACTOR_NAMES, (float(x) for x in shr.prior_var), strict=True)),
        "shrinkage_weight": {
            name: _quantiles(shr.weights[kept, k]) for k, name in enumerate(FACTOR_NAMES)
        },
        "flagged_asset_ids": [asset_ids[j] for j in shr.flagged],
        "intercept_dropped": True,
    }
    return CarhartResult(
        mu=np.asarray(mu, dtype=np.float64), record=record, betas=shr, raw=raw, premia=premia
    )
