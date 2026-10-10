"""The Carhart four-factor estimator with Vasicek-shrunk betas (T-077): the vendored Kenneth French
files, the OLS, the shrinkage, the premia and the mu -- on a fixture cut from the real files, never
the network."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest
from french_support import FF3, FIXTURE_DIR, MOM, replace_rows, write_manifest

from quant import factors
from quant.factors import (
    FACTOR_NAMES,
    FactorBetas,
    FactorCoverageError,
    FactorData,
    FactorIntegrityError,
    carhart_expected_returns,
    factor_premia,
    load_factors,
    ols_factor_betas,
    vasicek_shrink,
)

_LAST_FIXTURE_DAY = "2024-12-31"


def _raw_row(name: str, yyyymmdd: str) -> list[float]:
    for line in (FIXTURE_DIR / name).read_text().splitlines():
        if line.startswith(yyyymmdd):
            return [float(x) for x in line.split(",")[1:]]
    raise AssertionError(f"{yyyymmdd} not in {name}")


def _data(factor_dir: Path, as_of: str = _LAST_FIXTURE_DAY) -> FactorData:
    return load_factors(as_of, factor_dir)


def _panel(
    data: FactorData, betas: np.ndarray, *, noise: float = 0.0, seed: int = 3, n_dates: int = 600
) -> tuple[np.ndarray, list[str]]:
    """Log returns whose *simple* excess return is exactly ``0.0001 + factors @ beta (+ noise)``."""
    rng = np.random.default_rng(seed)
    dates = data.dates[-n_dates:]
    f = data.factors[-n_dates:]
    excess = 0.0001 + f @ betas.T + noise * rng.standard_normal((n_dates, betas.shape[0]))
    simple = excess + data.rf[-n_dates:, None]
    return np.log1p(simple), dates


# -- the data -----------------------------------------------------------------


def test_percent_values_are_converted_to_decimals(french_factor_dir: Path) -> None:
    data = _data(french_factor_dir)
    mkt, smb, hml, rf = _raw_row(FF3, "20220103")
    (mom,) = _raw_row(MOM, "20220103")
    i = data.dates.index("2022-01-03")
    assert data.factors[i] == pytest.approx([mkt / 100, smb / 100, hml / 100, mom / 100])
    assert data.rf[i] == pytest.approx(rf / 100)
    assert data.library_version == "202608 CRSP"


def test_no_factor_row_after_the_as_of_is_read(french_factor_dir: Path) -> None:
    as_of = "2023-06-30"
    base = _data(french_factor_dir, as_of)
    assert base.dates[-1] <= as_of and max(base.dates) <= as_of

    # Corrupt every row after the as-of -- one into text the parser would choke on -- and re-pin
    # the manifest: the loaded data, the premia and the estimator's mu must not move.
    replace_rows(french_factor_dir, FF3, after="20230630", fn=lambda ln: ln[:8] + ",9,9,9,9")
    replace_rows(french_factor_dir, MOM, after="20230630", fn=lambda ln: ln[:8] + ",not-a-number")
    write_manifest(french_factor_dir)
    again = _data(french_factor_dir, as_of)
    assert again.dates == base.dates
    assert np.array_equal(again.factors, base.factors)
    assert np.array_equal(again.rf, base.rf)
    assert factor_premia(again) == pytest.approx(factor_premia(base))
    # ... while a later as-of does read them
    with pytest.raises(FactorIntegrityError, match="unreadable row"):
        _data(french_factor_dir, "2023-07-31")


def test_a_sha256_mismatch_is_refused(french_factor_dir: Path) -> None:
    path = french_factor_dir / FF3
    path.write_bytes(path.read_bytes().replace(b"19630603", b"19630604", 1))
    with pytest.raises(FactorIntegrityError, match="does not match the manifest"):
        _data(french_factor_dir)


def test_the_manifest_sha256_is_the_files_own(french_factor_dir: Path) -> None:
    manifest = json.loads((french_factor_dir / "manifest.json").read_text())
    for spec in manifest["files"].values():
        digest = hashlib.sha256((french_factor_dir / spec["path"]).read_bytes()).hexdigest()
        assert digest == spec["sha256"]
    data = _data(french_factor_dir)
    assert data.sha256 == {
        FF3: manifest["files"]["ff3"]["sha256"],
        MOM: manifest["files"]["mom"]["sha256"],
    }


def test_a_missing_manifest_or_file_is_refused(french_factor_dir: Path) -> None:
    (french_factor_dir / MOM).unlink()
    with pytest.raises(FactorIntegrityError, match="unreadable"):
        _data(french_factor_dir)
    (french_factor_dir / "manifest.json").unlink()
    with pytest.raises(FactorIntegrityError, match="manifest"):
        _data(french_factor_dir)


def test_the_vendored_files_match_their_manifest() -> None:
    """The real vendored files (not the fixture) are exactly what the manifest pins."""
    data = load_factors("2026-08-31")
    manifest = json.loads((factors.DATA_DIR / "manifest.json").read_text())
    assert data.library_version == manifest["library_version"]
    assert data.last_date == data.file_last_date == manifest["files"]["ff3"]["last_date"]
    for spec in manifest["files"].values():
        assert spec["url"].startswith("https://mba.tuck.dartmouth.edu/")
        assert manifest["downloaded"]
        assert spec["version_statement"].startswith("This file was created by using the")


def test_an_as_of_before_the_files_start_is_a_coverage_refusal(french_factor_dir: Path) -> None:
    with pytest.raises(FactorCoverageError, match="no factor row"):
        _data(french_factor_dir, "1900-01-01")


def test_premia_use_the_sample_from_1963_07_01_only(french_factor_dir: Path) -> None:
    data = _data(french_factor_dir)
    rows = [i for i, d in enumerate(data.dates) if d >= "1963-07-01"]
    assert data.dates[0] == "1963-06-03" and len(rows) < len(data.dates)  # June 1963 is in the file
    expected = data.factors[rows].mean(axis=0) * 252
    assert factor_premia(data) == pytest.approx(expected)
    everything = data.factors.mean(axis=0) * 252
    assert not np.allclose(factor_premia(data), everything)
    assert factor_premia(data, start=None) == pytest.approx(everything)
    assert factor_premia(data, start=None, last_n=10) == pytest.approx(
        data.factors[-10:].mean(axis=0) * 252
    )


# -- the regression -----------------------------------------------------------


def test_ols_recovers_known_betas(french_factor_dir: Path) -> None:
    data = _data(french_factor_dir)
    true = np.array([[1.2, 0.4, -0.3, 0.1], [0.6, -0.2, 0.5, -0.4], [0.0, 0.0, 0.0, 0.0]])
    logs, dates = _panel(data, true, noise=0.002)
    f_ix = [data.dates.index(d) for d in dates]
    excess = np.expm1(logs) - data.rf[f_ix][:, None]
    fit = ols_factor_betas(excess, data.factors[f_ix], min_obs=504)
    assert fit.betas == pytest.approx(true, abs=0.05)
    assert (fit.n_obs == 600).all()
    assert fit.alpha == pytest.approx(0.0001, abs=2e-4)

    # the sampling variance is s2 * [(X'X)^-1]_kk, checked by hand for the first asset
    x = np.column_stack([np.ones(600), data.factors[f_ix]])
    resid = excess[:, 0] - x @ np.concatenate(([fit.alpha[0]], fit.betas[0]))
    s2 = resid @ resid / (600 - 5)
    assert fit.se2[0] == pytest.approx(s2 * np.diag(np.linalg.inv(x.T @ x))[1:])


def test_log_returns_are_converted_to_simple_before_the_regression(french_factor_dir: Path) -> None:
    data = _data(french_factor_dir)
    true = np.array([[1.5, 0.8, -0.6, 0.9]])
    logs, dates = _panel(data, true, noise=0.0)  # simple excess = a + f @ beta, exactly
    res = carhart_expected_returns(
        logs, dates, [1], data, rf_annual=0.04, min_obs=504, premia_start="1963-07-01"
    )
    assert res.raw.betas[0] == pytest.approx(true[0], abs=1e-9)  # exact only on simple returns

    # regressing the log returns directly would be off: the conversion is what makes it exact
    f_ix = [data.dates.index(d) for d in dates]
    wrong = ols_factor_betas(logs - data.rf[f_ix][:, None], data.factors[f_ix], min_obs=504)
    assert not np.allclose(wrong.betas[0], true[0], atol=1e-6)


def test_an_asset_with_too_few_observations_is_flagged_and_takes_the_prior_mean(
    french_factor_dir: Path,
) -> None:
    data = _data(french_factor_dir)
    true = np.array([[1.2, 0.4, -0.3, 0.1], [0.8, 0.0, 0.2, 0.3], [1.0, -0.3, 0.0, 0.2]])
    logs, dates = _panel(data, true, noise=0.004, seed=11)
    logs[:200, 2] = np.nan  # asset 3 has 400 observations: under 504
    res = carhart_expected_returns(
        logs, dates, [10, 20, 30], data, rf_annual=0.04, min_obs=504, premia_start="1963-07-01"
    )
    assert res.record["flagged_asset_ids"] == [30]
    assert res.betas.weights[2] == pytest.approx(0.0)
    prior = res.betas.prior_mean
    assert res.betas.betas[2] == pytest.approx(prior)
    # the prior is the equal-weighted mean of the two assets that have a fit, not of the flagged one
    assert prior == pytest.approx(res.raw.betas[:2].mean(axis=0))
    assert res.mu[2] == pytest.approx(0.04 + prior @ res.premia)


# -- Vasicek shrinkage --------------------------------------------------------


def _betas(b: list[list[float]], se2: list[list[float]], n_obs: list[int]) -> FactorBetas:
    n = len(b)
    return FactorBetas(
        betas=np.array(b, dtype=float),
        se2=np.array(se2, dtype=float),
        alpha=np.zeros(n),
        n_obs=np.array(n_obs, dtype=np.int64),
    )


def test_weight_is_near_one_when_sampling_noise_is_small_and_near_zero_when_large() -> None:
    spread = [[0.5] * 4, [1.0] * 4, [1.5] * 4, [2.0] * 4]
    tiny = vasicek_shrink(_betas(spread, [[1e-9] * 4] * 4, [756] * 4), min_obs=504)
    assert (tiny.weights > 0.999999).all()
    assert tiny.betas == pytest.approx(np.array(spread), abs=1e-6)

    huge = vasicek_shrink(_betas(spread, [[1e6] * 4] * 4, [756] * 4), min_obs=504)
    assert (huge.weights < 1e-6).all()
    assert huge.betas == pytest.approx(np.full((4, 4), 1.25), abs=1e-5)  # all at the prior mean


def test_weights_follow_sigma2_over_sigma2_plus_se2_per_factor() -> None:
    raw = _betas(
        [[0.5, 1, 1, 1], [1.5, 2, 2, 2], [1.0, 3, 3, 3]],
        [[0.1, 1, 1, 1], [0.3, 1, 1, 1], [0.5, 1, 1, 1]],
        [756, 756, 756],
    )
    shr = vasicek_shrink(raw, min_obs=504)
    var0 = np.var([0.5, 1.5, 1.0], ddof=1)
    assert shr.prior_var[0] == pytest.approx(var0)
    assert shr.prior_mean[0] == pytest.approx(1.0)
    assert shr.weights[:, 0] == pytest.approx(var0 / (var0 + np.array([0.1, 0.3, 0.5])))
    assert shr.betas[0, 0] == pytest.approx(shr.weights[0, 0] * 0.5 + (1 - shr.weights[0, 0]) * 1.0)


def test_with_equal_sampling_noise_the_mean_shrunk_beta_is_the_prior_mean() -> None:
    raw = _betas([[0.4, 1, 0, 2], [1.0, 2, 0, 1], [1.9, 4, 0, 3]], [[0.2] * 4] * 3, [756] * 3)
    shr = vasicek_shrink(raw, min_obs=504)
    assert shr.betas.mean(axis=0) == pytest.approx(shr.prior_mean)
    assert shr.prior_mean == pytest.approx(raw.betas.mean(axis=0))


def test_no_asset_with_enough_observations_is_a_coverage_refusal() -> None:
    with pytest.raises(FactorCoverageError):
        vasicek_shrink(_betas([[1.0] * 4], [[0.1] * 4], [100]), min_obs=504)


# -- the estimator ------------------------------------------------------------


def test_mu_is_rf_plus_shrunk_betas_times_premia_and_records_the_regression(
    french_factor_dir: Path,
) -> None:
    data = _data(french_factor_dir)
    true = np.array([[1.2, 0.4, -0.3, 0.1], [0.6, -0.2, 0.5, -0.4], [1.0, 0.1, 0.1, 0.1]])
    logs, dates = _panel(data, true, noise=0.003, seed=5)
    res = carhart_expected_returns(
        logs, dates, [7, 8, 9], data, rf_annual=0.05, min_obs=504, premia_start="1963-07-01"
    )
    premia = factor_premia(data, start="1963-07-01")
    assert res.mu == pytest.approx(0.05 + res.betas.betas @ premia)
    assert res.premia == pytest.approx(premia)
    rec = res.record
    assert rec["factor_library_version"] == "202608 CRSP"
    assert set(rec["factor_files_sha256"]) == {FF3, MOM}
    assert rec["regression_date_start"] == dates[0] and rec["regression_date_end"] == dates[-1]
    assert rec["regression_n_dates"] == 600 and rec["panel_gap_after_factor_end"] == 0
    assert list(rec["lambda_bar"]) == list(FACTOR_NAMES)
    assert rec["lambda_bar"]["MOM"] == pytest.approx(premia[3])
    assert rec["beta_bar"]["MKT"] == pytest.approx(res.raw.betas[:, 0].mean())
    assert rec["beta_prior_var"]["MKT"] == pytest.approx(res.raw.betas[:, 0].var(ddof=1))
    for name in FACTOR_NAMES:
        w = rec["shrinkage_weight"][name]
        assert 0.0 <= w["min"] <= w["median"] <= w["max"] <= 1.0
    assert rec["flagged_asset_ids"] == []
    assert rec["intercept_dropped"] is True
    # the intercept is estimated and dropped: shifting every return by a constant alpha moves mu not at all
    shifted = carhart_expected_returns(
        np.log1p(np.expm1(logs) + 0.0005),
        dates,
        [7, 8, 9],
        data,
        rf_annual=0.05,
        min_obs=504,
        premia_start="1963-07-01",
    )
    assert shifted.mu == pytest.approx(res.mu, abs=1e-12)


def test_a_panel_running_past_the_factor_files_is_regressed_on_the_overlap_and_the_gap_recorded(
    french_factor_dir: Path,
) -> None:
    data = _data(french_factor_dir)
    true = np.array([[1.2, 0.4, -0.3, 0.1], [0.6, -0.2, 0.5, -0.4]])
    logs, dates = _panel(data, true, noise=0.002, n_dates=600)
    extra = 15  # panel dates beyond the files' last date
    day = date.fromisoformat(dates[-1])
    more: list[str] = []
    while len(more) < extra:
        day += timedelta(days=1)
        if day.weekday() < 5:
            more.append(day.isoformat())
    rng = np.random.default_rng(1)
    logs = np.vstack([logs, 0.01 * rng.standard_normal((extra, 2))])
    res = carhart_expected_returns(
        logs, [*dates, *more], [1, 2], data, rf_annual=0.04, min_obs=504, premia_start="1963-07-01"
    )
    rec = res.record
    assert rec["regression_n_dates"] == 600 and rec["panel_n_dates"] == 615
    assert rec["regression_date_end"] == dates[-1] == rec["premia_last_date"]
    assert rec["panel_gap_after_factor_end"] == extra
    assert res.raw.betas == pytest.approx(true, abs=0.05)  # the junk past the file did not enter


def test_an_overlap_under_the_minimum_is_refused(french_factor_dir: Path) -> None:
    data = _data(french_factor_dir)
    logs, dates = _panel(data, np.array([[1.0, 0.0, 0.0, 0.0]]), n_dates=300)
    with pytest.raises(FactorCoverageError, match="overlap the factor data"):
        carhart_expected_returns(logs, dates, [1], data, rf_annual=0.04, min_obs=504)
    # a panel wholly past the files has no overlap at all
    late = [
        (date.fromisoformat(_LAST_FIXTURE_DAY) + timedelta(days=i + 1)).isoformat()
        for i in range(600)
    ]
    with pytest.raises(FactorCoverageError):
        carhart_expected_returns(np.zeros((600, 1)), late, [1], data, rf_annual=0.04, min_obs=504)
