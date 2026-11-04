# System v2 — plan, fixed before any v2 run

Author: reviewer A, 2026-11-04 (Medellín night of 2026-11-03). Committed verbatim as `docs/backtest_v2_plan.md`
as the first commit of `feat/backtest-v2`; that commit is its timestamp. After it, §2–§5 change only through a dated
entry in §7, written before the affected result is seen.

## 1. Why, and what is declared

v1 (PR #188, pre-registered, LLM scores, official configuration B0·T0) is **frozen** and remains the thesis's
pre-registered result. v2 is designed **after** seeing v1's results on all six windows (P1, P3, P2, H1, H2, H3, 2022–2026);
no unseen data remains, so every v2 result is in-sample and is reported as "developed after observing v1".

What v1 showed (the reason for each change):
- **Concentration.** Min-variance over the whole eligible universe (MARKOWITZ_BASE) had a lower maximum drawdown than
  the Top-30 book in all three P windows (P1 16.2% vs 27.2%). → add a diversified **core** (S&P 500 index fund) and
  test GUARD on the base book.
- **Slow bear markets.** GUARD's layers are calibrated on 2019–2021, whose reference event is the fast 2020 crash;
  layer 4 reaches zero exposure only at a 39.3% drawdown. The 2022 decline was slow. → add a **market trend filter**
  from the literature (Faber, 2007), with the published rule, no tuning.

Fixed for every run: LLM scores (`scored_llm.db` sha256 `2187c36b…`, configuration A), blend B0·T0, GUARD
`params.frozen.toml` sha256 `ee22f352…` (layers 1, 3, 4), Top-30, name cap 1.5/30, sector cap 0.30, min-variance with
`ledoit_wolf_cc`, 10 bps, cash at French RF, 12 warm-up steps, one fresh copy per run.

## 2. The changes (each a run parameter; defaults reproduce v1 bit for bit)

**(a) Core-satellite** (Amenc, Malaise & Martellini, 2004). Parameter `core_weight` c. At each decision date the
target book is c in the core (SPY, total return) + (1 − c) × the v1 Top-30 min-variance target (its weights
multiplied by 1 − c). Between decisions the book drifts. Costs apply to core trades too. Name and sector caps apply to
the satellite's own weights (unchanged); the core counts in no sector cap (declared).

**(b) GUARD on the whole book.** Layer 1 applies to every holding, the core included; layers 3 and 4 are computed on
the whole book (reference = the NOGUARD twin of the same configuration); the exposure scale applies to every holding;
the residual goes to cash.

**(c) Market trend filter** (Faber, 2007), parameter `trend_filter` ∈ {off, on}. At each decision date t (data ≤ t):
if SPY's close at t is below the simple average of its last 210 session closes (≈ 10 months × 21 sessions), the book's
target is 100% cash until the next decision date; otherwise the normal target. Evaluated only at decision dates
(monthly, as Faber). Applies to both Top-N arms of the configuration, core included. Not applied to MARKOWITZ_BASE.

**(d) New arm `MARKOWITZ_BASE_GUARD`**: GUARD (layers 1, 3, 4) as an overlay on MARKOWITZ_BASE, exactly as
TOPN_MV_GUARD_NO_NLP is on TOPN_MV_NOGUARD. Identical across configurations of a window.

**(e) New statistic**: paired circular block bootstrap of the **maximum-drawdown difference** between two arms (same
sessions; B = 4999, block length 5, fixed seed `20261104`), two-sided p; reported beside the HAC test.

## 3. The grid: 3 × 2 = 6 configurations × 6 windows = 36 runs

| id | core_weight | trend_filter |
|---|---|---|
| V0 | 0 | off | ← must equal v1 (identity check) |
| V1 | 0 | on |
| V2 | 0.5 | off |
| V3 | 0.5 | on |
| V4 | 0.7 | off |
| V5 | 0.7 | on |

Windows: P1, P3, P2 (P2a/P2b as cuts), H1, H2, H3. Arms per run: MARKOWITZ_BASE, MARKOWITZ_BASE_GUARD,
TOPN_MV_NOGUARD, TOPN_MV_GUARD_NO_NLP (with the configuration's core and filter). Benchmarks for context:
SP500_EW_INTERNAL and SPY (total return).

## 4. Reporting and the one choice

- Every configuration × window × arm is reported (cumulative and annualized return, volatility, Sharpe, MDD,
  turnover, cost, GUARD actions, filter-off/on dates). Nothing is dropped.
- Tests per window: HAC mean difference and bootstrap ΔMDD for (i) each v2 GUARD arm vs v1's official GUARD arm,
  (ii) each v2 GUARD arm vs MARKOWITZ_BASE, (iii) MARKOWITZ_BASE_GUARD vs MARKOWITZ_BASE.
- **The v2 system** (if one is named) is chosen by: lowest mean MDD of the GUARD arm over the six windows; ties within
  0.01 pp → higher mean cumulative return. Declared in-sample. "Improvement" is claimed only as "lower MDD than v1 in
  k of 6 windows", with the tests, never as significant unless a test says so.
- Configurations are not added after results are seen; an addition is a §7 entry and is reported as such.

## 5. Data

SPY daily close (split-adjusted, not dividend-adjusted) and dividends at ex-date, 2018-01-02 … 2026-10-16, from Yahoo
Finance (the project's price source), built into a total-return series with `quant.returns`' convention
(r = (C_t + D_t)/C_{t−1} − 1). File and manifest (source, download time, rows, sha256) live in scratch, never in the
repository; the engine refuses a file whose sha256 differs from the manifest. Only aggregates are committed.

## 6. References

Amenc, N., Malaise, P., & Martellini, L. (2004). Revisiting core-satellite investing. *The Journal of Portfolio
Management, 31*(1), 64–75. · Faber, M. T. (2007). A quantitative approach to tactical asset allocation. *The Journal of
Wealth Management, 9*(4), 69–79. · Statman, M. (1987). How many stocks make a diversified portfolio? *Journal of
Financial and Quantitative Analysis, 22*(3), 353–363. · Politis & Romano (1992) and Ledoit & Wolf (2008) for the
bootstrap.

## 7. Deviations

Any change to §2–§5 after the commit is written here with its date and reason, before the result it affects is seen.
