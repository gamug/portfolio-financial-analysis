# System experiments round 3 — plan, fixed before any run

Author: reviewer A, 2026-11-04. Committed verbatim as `docs/backtest_v3_experiments_plan.md`, as the first commit of
`feat/backtest-v3-experiments` (branched from master at or after `cc649d2`); that commit is its timestamp. After it,
§2–§5 change only through a dated entry in §7, written before the affected result is seen.

## 1. Why, and what is declared

v1 (pre-registered) and v2 (PR #190) are merged. v2's chosen configuration **V3** (core 0.5 SPY, Faber filter on) has
the lowest mean GUARD MDD of every book compared (7.81% over P1, P3, P2, H1, H2, H3), but a low return: mean cumulative
5.12%, against 7.33% for MARKOWITZ_BASE and 11.1% for SPY. Its ratio R = mean cumulative / mean MDD is 0.6549, below
MARKOWITZ_BASE (0.85). This round looks for a configuration that keeps V3's drawdown and recovers return. All six
windows have been seen: every result is in-sample and the total number of configurations tried is reported.

The three levers, each from a v1/v2 observation:
- **Concentration of the satellite** (Top-30 beta 0.84 vs base 0.53 in P1). The min-variance step only sees the 30
  ranked names; a wider ranked pool lets it pick less correlated names while the book keeps at most 30.
- **The core's composition.** MARKOWITZ_BASE had the best return per unit of drawdown of the system's books; it is
  tried as the core instead of SPY.
- **Filter whipsaw** (H1: V3 −5.6% while SPY +17.5%). A partial exit instead of 100% cash.

Fixed for every run (as v2): LLM scores (`scored_llm.db` sha256 `2187c36b…`), blend B0·T0, GUARD
`params.frozen.toml` (layers 1, 3, 4), name cap 1.5/30, sector cap 0.30, min-variance with `ledoit_wolf_cc`, 10 bps,
cash at French RF, 12 warm-up steps, core_weight 0.5, trend filter on (210 sessions, decision dates only), the SPY
file `58b91870…`, one fresh copy per run.

## 2. The changes (each a run parameter; defaults reproduce v2 bit for bit)

**(a) Candidate pool `pool_m` ∈ {30, 60, 100}.** cycle ranks and selects the top `pool_m` names (same blend, same
vetoes, same sector rule as today with `top_n` replaced by `pool_m`); quant runs min-variance on those `pool_m` names
with the unchanged caps (name 1.5/30 = 0.05, sector 0.30). If the solution holds more than 30 names (weight > 1e-6),
keep the 30 largest weights (ties: lower asset id) and re-run the same min-variance on those 30 only; that second
solution is the satellite. `pool_m = 30` is today's path exactly.

**(b) Core source `core_source` ∈ {spy, base}.** `spy` = v2. `base` = the core is c × the MARKOWITZ_BASE target of
the same decision date (the same weights the MARKOWITZ_BASE arm holds). Where a name is both in the core and the
satellite, the weights add; the combined weight is not re-capped (declared: up to 0.5 × 0.05 + 0.5 × 0.05 = 0.05).
GUARD (layer 1 per holding, layers 3/4 on the whole book) applies as in v2.

**(c) Filter exit fraction `filter_exit` ∈ {1.0, 0.5}.** When the filter triggers at a decision date, the target is
(1 − `filter_exit`) × the normal target; the rest is cash until the next decision date. 1.0 = v2.

MARKOWITZ_BASE and MARKOWITZ_BASE_GUARD are untouched by (a)–(c), as in v2.

## 3. The grid: 3 × 2 × 2 = 12 configurations × 6 windows = 72 runs

| id | pool_m | core_source | filter_exit |
|---|---|---|---|
| X0 | 30 | spy | 1.0 | ← must equal v2's V3 (identity check) |
| X1 | 30 | spy | 0.5 |
| X2 | 30 | base | 1.0 |
| X3 | 30 | base | 0.5 |
| X4 | 60 | spy | 1.0 |
| X5 | 60 | spy | 0.5 |
| X6 | 60 | base | 1.0 |
| X7 | 60 | base | 0.5 |
| X8 | 100 | spy | 1.0 |
| X9 | 100 | spy | 0.5 |
| X10 | 100 | base | 1.0 |
| X11 | 100 | base | 0.5 |

Windows: P1, P3, P2 (P2a/P2b as cuts), H1, H2, H3. Arms: MARKOWITZ_BASE, MARKOWITZ_BASE_GUARD, TOPN_MV_NOGUARD,
TOPN_MV_GUARD_NO_NLP. Context: SP500_EW_INTERNAL and SPY.

## 4. Reporting and the adoption rule

- Every configuration × window × arm is reported (cumulative and annualized return, volatility, Sharpe, MDD,
  turnover, cost, GUARD actions, filter states, number of names held, and for `pool_m` > 30 how many decisions needed
  the second pass). Nothing is dropped.
- Tests per window, TOPN_MV_GUARD_NO_NLP of each configuration: HAC mean difference and bootstrap ΔMDD (v2's
  function, same seed) against (i) X0 and (ii) MARKOWITZ_BASE.
- Per configuration, over the six windows of TOPN_MV_GUARD_NO_NLP: mean MDD, mean cumulative, R = mean cumulative /
  mean MDD, and k = number of windows with lower MDD than X0.
- **Adoption.** A configuration is an improvement over V3 only if R > 0.6549 **and** mean MDD ≤ 0.0831 (V3 + 0.5 pp).
  Among improvements, the one with the highest R is adopted; ties within 0.001 in R → lower mean MDD. If none
  qualifies, V3 stays the system's configuration and this round is reported as tried and not adopted.
- Declared count: configurations tried after v1 = 6 (v2) + 11 new here = 17.

## 5. Data

No new data. The SPY file and its manifest as in v2; LLM scores from `scored_llm.db` only (no LLM call; every pool
name at a decision date is a member, and members are scored or D1-unscored exactly as in v1/v2). Only aggregates are
committed.

## 6. References

Statman (1987) and Amenc, Malaise & Martellini (2004) as in v2; Faber (2007) for the filter; the partial exit is a
declared rule with no literature value (reported as such).

## 7. Deviations

Any change to §2–§5 after the commit is written here with its date and reason, before the result it affects is seen.

2026-11-04 — Implementation readings, recorded before any round-3 run (no result has been seen). (1) The candidate pool is the cycle's `top_n`: the driver sets `CycleSettings.top_n = pool_m`, so the same ranking, vetoes and sector rule select `pool_m` names. Quant receives `--top-n 30` (the unchanged name cap, 1.5/30) and a new `--pool-m` that reads the cycle's `pool_m` selected names; the min-variance runs over that pool, and if the solution holds more than 30 names (weight > 1e-6) the 30 largest (ties: lower asset id) are re-solved with the same caps and that second solution is the satellite. (2) The driver's `pool_m` defaults to the params' `top_n` (30), so every v1/v2 path is bit-for-bit unchanged; the CLI offers only 30/60/100. (3) X0's identity is checked against the stored v2 V3 result JSONs (summaries, tests and daily series) and, for weights and GUARD actions, against a fresh V3 run on the v2 code at `2ff9df7`; the round-2 V3 run copies were not kept (only V0's), and the fresh V3 rerun is first shown to reproduce the stored V3 JSONs.

2026-11-04 — The §4 declared count ("6 (v2) + 11 = 17") is corrected after the results by the ledger in `docs/backtest_v3_experiments_results.md`: CHAMPION (PR #188) 25 configurations, v2 (PR #190) 6 configurations of which 5 were new, and round 3 12 configurations of which 11 were new — 25 + 5 + 11 = 41 distinct configurations evaluated with LLM scores. The "17" counted v2 and round 3 only. This correction changes no rule, no result and no choice (X5 stands).
