"""WP-E: the backtest statistics (BACKTEST_SPEC §6, §8.4).

Closed-form cases, hand-computed paths, the published formulas cross-checked against a direct
numpy transcription, seed determinism and the input validation every function promises.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy.stats import norm

from cycle.backtest.stats import (
    MIN_OBS,
    hac_mean_diff,
    ledoit_wolf_sharpe_diff,
    memmel_jk,
    summarize,
)

# -- helpers -----------------------------------------------------------------------------------


def _alternating(n: int, up: float = 0.01, down: float = -0.01) -> list[float]:
    """``n`` sessions alternating *up* and *down*; even ``n`` ends on *down*."""
    return [up if i % 2 == 0 else down for i in range(n)]


def _standardised(rng: np.random.Generator, n: int) -> np.ndarray:
    """A deterministic draw with exactly zero mean and exactly unit sample std (``ddof=1``)."""
    e = rng.normal(size=n)
    e -= e.mean()
    return e / e.std(ddof=1)


def _two_arms(
    n: int = 252,
    mu_a: float = 0.003,
    mu_b: float = 0.0,
    sd: float = 0.01,
    *,
    seed: int = 5,
) -> tuple[np.ndarray, np.ndarray]:
    """Two exactly-standardised excess-return series (rf = 0), correlated through a shared shock."""
    rng = np.random.default_rng(seed)
    common = _standardised(rng, n)
    idio_a = _standardised(rng, n)
    idio_b = _standardised(rng, n)
    a = mu_a + sd * (0.7 * common + 0.714142842854285 * idio_a)
    b = mu_b + sd * (0.7 * common + 0.714142842854285 * idio_b)
    return a, b


def _jk_direct(a: np.ndarray, b: np.ndarray, rf: np.ndarray) -> tuple[float, float]:
    """A direct numpy transcription of Jobson & Korkie (1981) as corrected by Memmel (2003)."""
    x, y = a - rf, b - rf
    n = x.size
    sr_a = x.mean() / x.std(ddof=1)
    sr_b = y.mean() / y.std(ddof=1)
    rho = np.corrcoef(x, y)[0, 1]
    variance = (2.0 * (1.0 - rho) + 0.5 * (sr_a**2 + sr_b**2 - 2.0 * sr_a * sr_b * rho**2)) / n
    z = (sr_a - sr_b) / math.sqrt(variance)
    return float(z), float(2.0 * norm.sf(abs(z)))


# -- summarize ---------------------------------------------------------------------------------


def test_summarize_closed_form_on_an_alternating_series() -> None:
    """Every key against the exact formula for +1 %/-1 % over 40 sessions, rf = 0."""
    n = 40
    r = np.array(_alternating(n))
    rf = np.zeros(n)
    turnover = np.full(n, 0.01)
    cost = np.full(n, 0.0001)
    out = summarize(r, rf, turnover, cost)

    growth = 0.9999**20  # (1.01 * 0.99) ** 20
    assert out["cumulative_return"] == pytest.approx(growth - 1.0)
    assert out["annualized_return"] == pytest.approx(growth ** (252 / n) - 1.0)
    # sample std of the alternating series is 0.01 * sqrt(n / (n - 1))
    assert out["annualized_vol"] == pytest.approx(0.01 * math.sqrt(n / (n - 1)) * math.sqrt(252))
    assert out["ex_post_sharpe"] == pytest.approx(0.0)  # mean excess return is exactly zero
    # the first NAV (1.01) is the running peak; the last NAV (0.9999 ** 20) the trough
    assert out["max_drawdown"] == pytest.approx(1.0 - growth / 1.01)
    assert out["total_turnover"] == pytest.approx(0.01 * n)
    assert out["total_cost_bps"] == pytest.approx(0.0001 * n * 10_000)
    assert out["n_obs"] == n


def test_summarize_returns_exactly_the_interface_keys() -> None:
    n = 30
    out = summarize([0.001] * n, [0.0] * n, [0.0] * n, [0.0] * n)
    assert set(out) == {
        "cumulative_return",
        "annualized_return",
        "annualized_vol",
        "ex_post_sharpe",
        "max_drawdown",
        "total_turnover",
        "total_cost_bps",
        "n_obs",
    }


def test_summarize_max_drawdown_is_a_positive_hand_computed_path() -> None:
    """1 -> 1.10 -> 0.88: a 20 % decline from the peak."""
    r = [0.10, -0.20] + [0.0] * 28
    out = summarize(r, [0.0] * 30, [0.0] * 30, [0.0] * 30)
    assert out["max_drawdown"] == pytest.approx(0.20)
    assert out["max_drawdown"] > 0.0


def test_summarize_counts_a_loss_on_the_first_session_against_the_starting_nav() -> None:
    r = [-0.25] + [0.0] * 29
    out = summarize(r, [0.0] * 30, [0.0] * 30, [0.0] * 30)
    assert out["max_drawdown"] == pytest.approx(0.25)


def test_summarize_drawdown_recovers_after_a_new_high() -> None:
    """A 10 % dip, then a new high: the earlier dip still is the maximum."""
    r = [0.20, -0.10] + [0.0] * 13 + [0.5] + [0.0] * 14
    out = summarize(r, [0.0] * 30, [0.0] * 30, [0.0] * 30)
    # NAV 1 -> 1.2 -> 1.08 (drawdown 10 %) -> ... -> 1.62
    assert out["max_drawdown"] == pytest.approx(0.10)


def test_summarize_annualizes_with_the_requested_periods_per_year() -> None:
    n = 252
    r = [0.001] * n
    rf = [0.0] * n
    zeros = [0.0] * n
    daily = summarize(r, rf, zeros, zeros, periods_per_year=252)
    weekly = summarize(r, rf, zeros, zeros, periods_per_year=52)

    # a constant 0.1 % per session compounds to 1.001 ** 252 - 1 in one year of 252 sessions
    assert daily["annualized_return"] == pytest.approx(1.001**252 - 1.0)
    assert daily["cumulative_return"] == pytest.approx(1.001**252 - 1.0)
    assert weekly["annualized_return"] == pytest.approx(1.001**52 - 1.0)
    assert weekly["annualized_vol"] == pytest.approx(0.0)
    assert daily["annualized_vol"] == pytest.approx(0.0)
    assert weekly["annualized_vol"] == pytest.approx(daily["annualized_vol"] * math.sqrt(52 / 252))


def test_summarize_sharpe_is_the_annualized_mean_over_std_of_excess_returns() -> None:
    r = np.array(_alternating(60, up=0.02, down=-0.005))
    rf = np.full(60, 0.0002)
    out = summarize(r, rf, np.zeros(60), np.zeros(60))
    excess = r - rf
    expected = float(np.mean(excess)) / float(np.std(excess, ddof=1)) * math.sqrt(252)
    assert out["ex_post_sharpe"] == pytest.approx(expected)


def test_summarize_reports_nan_sharpe_when_the_excess_series_is_constant() -> None:
    """A cash book earning exactly the risk-free rate has no defined ex-post Sharpe."""
    out = summarize([0.0002] * 30, [0.0002] * 30, [0.0] * 30, [0.0] * 30)
    assert math.isnan(out["ex_post_sharpe"])
    assert out["cumulative_return"] > 0.0


def test_summarize_rejects_a_series_that_loses_more_than_everything() -> None:
    r = [0.0] * 29 + [-1.0]
    with pytest.raises(ValueError, match="non-positive growth factor"):
        summarize(r, [0.0] * 30, [0.0] * 30, [0.0] * 30)


def test_summarize_does_not_mutate_its_inputs() -> None:
    r = _alternating(40)
    rf = [0.0] * 40
    turnover = [0.1] * 40
    cost = [0.0] * 40
    before = (list(r), list(rf), list(turnover), list(cost))
    summarize(r, rf, turnover, cost)
    assert (r, rf, turnover, cost) == before


# -- validation shared by every function -------------------------------------------------------


def _valid() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    a, b = _two_arms(n=60)
    return a, b, np.zeros(60)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_every_function_rejects_non_finite_values(bad: float) -> None:
    a, b, rf = _valid()
    a = a.copy()
    a[7] = bad
    with pytest.raises(ValueError, match="non-finite"):
        summarize(a, rf, rf, rf)
    with pytest.raises(ValueError, match="non-finite"):
        memmel_jk(a, b, rf)
    with pytest.raises(ValueError, match="non-finite"):
        ledoit_wolf_sharpe_diff(a, b, rf, n_boot=10)
    with pytest.raises(ValueError, match="non-finite"):
        hac_mean_diff(a, b)


def test_every_function_requires_at_least_thirty_observations() -> None:
    a, b, rf = _valid()
    a, b, rf = a[: MIN_OBS - 1], b[: MIN_OBS - 1], rf[: MIN_OBS - 1]
    with pytest.raises(ValueError, match="at least 30"):
        summarize(a, rf, rf, rf)
    with pytest.raises(ValueError, match="at least 30"):
        memmel_jk(a, b, rf)
    with pytest.raises(ValueError, match="at least 30"):
        ledoit_wolf_sharpe_diff(a, b, rf, n_boot=10)
    with pytest.raises(ValueError, match="at least 30"):
        hac_mean_diff(a, b)


def test_every_function_requires_paired_lengths() -> None:
    a, b, rf = _valid()
    with pytest.raises(ValueError, match="has 59 observations, expected 60"):
        summarize(a, rf[:59], rf, rf)
    with pytest.raises(ValueError, match="has 59 observations, expected 60"):
        memmel_jk(a, b[:59], rf)
    with pytest.raises(ValueError, match="has 59 observations, expected 60"):
        ledoit_wolf_sharpe_diff(a, b, rf[:59], n_boot=10)
    with pytest.raises(ValueError, match="has 59 observations, expected 60"):
        hac_mean_diff(a, b[:59])


def test_non_numeric_input_is_a_value_error_not_a_type_error() -> None:
    with pytest.raises(ValueError, match="not a sequence of numbers"):
        summarize([0.0] * 29 + ["x"], [0.0] * 30, [0.0] * 30, [0.0] * 30)  # type: ignore[list-item]


def test_a_none_element_is_rejected_like_any_other_non_finite_value() -> None:
    """``np.asarray`` turns ``None`` into ``nan``; the finite-value check must catch it."""
    with pytest.raises(ValueError, match="non-finite"):
        summarize([0.0] * 29 + [None], [0.0] * 30, [0.0] * 30, [0.0] * 30)  # type: ignore[list-item]


def test_summarize_rejects_a_non_positive_periods_per_year() -> None:
    a, _, rf = _valid()
    with pytest.raises(ValueError, match="periods_per_year"):
        summarize(a, rf, rf, rf, periods_per_year=0)


# -- memmel_jk ---------------------------------------------------------------------------------


def test_memmel_jk_matches_a_direct_transcription_of_the_published_formula() -> None:
    rng = np.random.default_rng(11)
    n = 120
    common = rng.normal(0.0, 0.008, n)
    a = 0.0012 + common + rng.normal(0.0, 0.006, n)
    b = 0.0004 + common + rng.normal(0.0, 0.009, n)
    rf = np.full(n, 0.0001)

    z, p = memmel_jk(a, b, rf)
    z_direct, p_direct = _jk_direct(a, b, rf)
    assert z == pytest.approx(z_direct, rel=1e-12)
    assert p == pytest.approx(p_direct, rel=1e-12, abs=1e-15)


def test_memmel_jk_on_identical_series_is_no_evidence_of_a_difference() -> None:
    a, _, rf = _valid()
    assert memmel_jk(a, a, rf) == (0.0, 1.0)


def test_memmel_jk_finds_a_clearly_better_series() -> None:
    a, b = _two_arms(n=252, mu_a=0.003, mu_b=0.0, sd=0.01)
    z, p = memmel_jk(a, b, np.zeros(252))
    assert z > 4.0
    assert p < 1e-5


def test_memmel_jk_is_two_sided_and_symmetric_under_a_swap() -> None:
    a, b = _two_arms(n=180, mu_a=0.0012, mu_b=0.0003, sd=0.01)
    rf = np.zeros(180)
    z_ab, p_ab = memmel_jk(a, b, rf)
    z_ba, p_ba = memmel_jk(b, a, rf)
    assert z_ba == pytest.approx(-z_ab)
    assert p_ba == pytest.approx(p_ab)


def test_memmel_jk_rejects_a_zero_variance_series() -> None:
    _a, b, rf = _valid()
    with pytest.raises(ValueError, match="zero standard deviation"):
        memmel_jk(np.full(60, 0.001), b, rf)


# -- ledoit_wolf_sharpe_diff -------------------------------------------------------------------


def test_ledoit_wolf_returns_the_per_session_sharpe_difference() -> None:
    a, b = _two_arms(n=120, mu_a=0.002, mu_b=0.0005)
    rf = np.zeros(120)
    diff, p = ledoit_wolf_sharpe_diff(a, b, rf, block=5, n_boot=19)
    expected = float(np.mean(a) / np.std(a, ddof=1) - np.mean(b) / np.std(b, ddof=1))
    assert diff == pytest.approx(expected, rel=1e-12)
    assert 0.0 < p <= 1.0


def test_ledoit_wolf_on_identical_series_is_no_evidence_of_a_difference() -> None:
    a, _, rf = _valid()
    assert ledoit_wolf_sharpe_diff(a, a, rf, n_boot=19) == (0.0, 1.0)


def test_ledoit_wolf_finds_a_clearly_better_series() -> None:
    a, b = _two_arms(n=252, mu_a=0.003, mu_b=0.0, sd=0.01)
    diff, p = ledoit_wolf_sharpe_diff(a, b, np.zeros(252), n_boot=199)
    assert diff > 0.0
    # 199 usable draws put the smallest attainable p at 1/200
    assert p == pytest.approx(1.0 / 200.0)


def test_ledoit_wolf_is_deterministic_for_a_fixed_seed() -> None:
    a, b = _two_arms(n=150, mu_a=0.0015, mu_b=0.0002)
    rf = np.zeros(150)
    first = ledoit_wolf_sharpe_diff(a, b, rf, block=5, n_boot=99, seed=7)
    second = ledoit_wolf_sharpe_diff(a, b, rf, block=5, n_boot=99, seed=7)
    assert first == second


def test_ledoit_wolf_p_varies_with_the_seed() -> None:
    """The p-value is a bootstrap frequency, so another seed redraws it (and must not be frozen)."""
    a, b = _two_arms(n=150, mu_a=0.0012, mu_b=0.0002)
    rf = np.zeros(150)
    p_seeds = {
        ledoit_wolf_sharpe_diff(a, b, rf, block=5, n_boot=99, seed=seed)[1] for seed in (1, 2, 3)
    }
    assert len(p_seeds) > 1


def test_ledoit_wolf_default_seed_is_fixed() -> None:
    a, b = _two_arms(n=150, mu_a=0.0012, mu_b=0.0002)
    rf = np.zeros(150)
    implicit = ledoit_wolf_sharpe_diff(a, b, rf, n_boot=49)
    explicit = ledoit_wolf_sharpe_diff(a, b, rf, n_boot=49, seed=20261028)
    assert implicit == explicit


@pytest.mark.parametrize("block", [0, -1, 61, 10_000])
def test_ledoit_wolf_rejects_an_out_of_range_block(block: int) -> None:
    a, b, rf = _valid()
    with pytest.raises(ValueError, match="block must be an integer"):
        ledoit_wolf_sharpe_diff(a, b, rf, block=block, n_boot=10)


def test_ledoit_wolf_accepts_the_whole_series_as_one_block() -> None:
    a, b, rf = _valid()
    diff, p = ledoit_wolf_sharpe_diff(a, b, rf, block=60, n_boot=9)
    assert math.isfinite(diff)
    assert 0.0 < p <= 1.0


def test_ledoit_wolf_rejects_a_non_positive_n_boot() -> None:
    a, b, rf = _valid()
    with pytest.raises(ValueError, match="n_boot"):
        ledoit_wolf_sharpe_diff(a, b, rf, n_boot=0)


def test_ledoit_wolf_does_not_mutate_its_inputs() -> None:
    a, b, rf = _valid()
    before = (a.copy(), b.copy(), rf.copy())
    ledoit_wolf_sharpe_diff(a, b, rf, n_boot=9)
    for current, saved in zip((a, b, rf), before, strict=True):
        assert np.array_equal(current, saved)


# -- hac_mean_diff -----------------------------------------------------------------------------


def test_hac_mean_diff_closed_form_with_no_lags() -> None:
    """``t = mean * n / sqrt(sum((d - mean)^2))`` when the Bartlett weights are absent."""
    n = 40
    d = np.array([0.002, 0.0] * (n // 2))  # mean 0.001, every deviation +-0.001
    a = d.copy()
    b = np.zeros(n)
    t, p = hac_mean_diff(a, b, lags=0)
    assert t == pytest.approx(math.sqrt(n))
    assert p == pytest.approx(2.0 * norm.sf(math.sqrt(n)))


def test_hac_mean_diff_on_identical_series_is_no_evidence_of_a_difference() -> None:
    a, _, _ = _valid()
    assert hac_mean_diff(a, a) == (0.0, 1.0)


def test_hac_mean_diff_is_symmetric_under_a_swap() -> None:
    a, b = _two_arms(n=150, mu_a=0.0012, mu_b=0.0002)
    t_ab, p_ab = hac_mean_diff(a, b)
    t_ba, p_ba = hac_mean_diff(b, a)
    assert t_ba == pytest.approx(-t_ab)
    assert p_ba == pytest.approx(p_ab)


def test_hac_mean_diff_uses_the_newest_west_lag_rule_by_default() -> None:
    a, b = _two_arms(n=150, mu_a=0.0012, mu_b=0.0002)
    expected_lags = math.floor(4.0 * (150 / 100.0) ** (2.0 / 9.0))
    assert expected_lags == 4
    assert hac_mean_diff(a, b) == hac_mean_diff(a, b, lags=expected_lags)


def test_hac_mean_diff_widens_the_standard_error_of_a_positively_autocorrelated_difference() -> (
    None
):
    """Positive serial correlation inflates the long-run variance, so |t| falls against lags=0."""
    rng = np.random.default_rng(17)
    n = 500
    eps = rng.normal(0.0, 0.01, n)
    d = np.zeros(n)
    for i in range(1, n):
        d[i] = 0.6 * d[i - 1] + eps[i]
    d += 0.001  # a positive mean on top of the AR(1) noise
    a = d + 0.0005
    b = np.full(n, 0.0005)
    t_hac, _ = hac_mean_diff(a, b)
    t_white, _ = hac_mean_diff(a, b, lags=0)
    assert abs(t_hac) < abs(t_white)


def test_hac_mean_diff_rejects_a_constant_non_zero_difference() -> None:
    n = 30
    with pytest.raises(ValueError, match="zero HAC variance"):
        hac_mean_diff([0.001] * n, [0.0] * n)


@pytest.mark.parametrize("lags", [-1, 60, 100])
def test_hac_mean_diff_rejects_an_out_of_range_lag(lags: int) -> None:
    a, b, _ = _valid()
    with pytest.raises(ValueError, match="lags must be an integer"):
        hac_mean_diff(a, b, lags=lags)


def test_hac_mean_diff_default_lag_rule_is_the_published_one() -> None:
    for n in (30, 100, 150, 195, 252):
        assert (
            math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0))
            == {
                30: 3,
                100: 4,
                150: 4,
                195: 4,
                252: 4,
            }[n]
        )
