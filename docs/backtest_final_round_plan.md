# Final round of system experiments — plan, fixed before any run

Author: reviewer A, 2026-11-04. Committed verbatim as `docs/backtest_final_round_plan.md`, as the first commit of
`feat/backtest-final-round` (branched from master at or after `93576f5`, the #192 merge); that commit is its
timestamp. After it, §2–§7 change only through a dated entry in §11, written before the affected result is seen.

This is the **last** round. Its output is the system's final configuration (**FINAL**) and the generated report that
justifies the choice against the benchmarks. After this round the system is frozen. It supersedes the unsent round-5
draft (`V5_EXPERIMENTS_PLAN.md`), whose stage A is folded in below.

## 1. Why, and what is declared

State after round 4: the system's configuration is **Y2**.
- Y2 = core 0.5 SPY, pool 60, trend filter with exit 0.5, Basing Points stop bp4, min-variance satellite.
- TOPN_MV_GUARD_NO_NLP: mean MDD 0.076108, mean cumulative 0.064284, R 0.8446.

Reviewer A's measurements on the round-4 run copies motivate three stages:
- **GUARD costs more than it saves.**
  - The NOGUARD arm of the same configuration has R 0.872.
  - Layer 4 scales exposure on almost every session: 196/196 sessions in P1, 103–235 in the other windows; mean
    scale 0.80–0.98.
  - Layer 3 never acted: `sigma_star` 0.2455 is an equal-weight member volatility, and the book runs at about 9%.
  - → **Stage A**: which GUARD layers pay for themselves.
- **The drawdown cut comes from lower market exposure, not from the weighting.**
  - Y2 has beta 0.44 to SPY, Jensen alpha −0.59%/yr (t −0.28), and up/down capture 50% / 49%.
  - The min-variance satellite has never been compared with the other weighting families in the literature.
  - → **Stage B**: satellite weighting methods.
- **The choice must be defended against selection bias.**
  - Over the 21 distinct configurations tried so far, the probability of backtest overfitting is 0.893.
  - The in-sample-best configuration's mean annualised Sharpe goes from 0.66 in-sample to 0.02 out-of-sample.
  - → **Stage C**: the final measurement, with overfitting diagnostics.

All six windows have been seen: every result is in-sample, and the number of configurations is reported.

Fixed for every run, as Y2 unless a stage changes it:
- LLM scores (`scored_llm.db` sha256 `2187c36b…`), blend B0·T0;
- name cap 1.5/30, sector cap 0.30, min-variance with `ledoit_wolf_cc` over 756 sessions, 10 bps, cash at French RF,
  12 warm-up steps;
- core_weight 0.5, core_source spy, pool_m 60, trend filter on with filter_exit 0.5;
- stop_rule bp4 for the Top-N GUARD arm;
- the SPY file `58b91870…`, one fresh copy per run;
- GUARD layer-1 and layer-4 parameters from `params.frozen.toml`.

`MARKOWITZ_BASE`, `MARKOWITZ_BASE_GUARD` and `TOPN_MV_NOGUARD` are untouched by stages A and B.

## 2. Stage A — GUARD layers (5 configurations × 6 windows = 30 runs)

**(a) `guard_layers`** of the TOPN_MV_GUARD_NO_NLP arm: a subset of {1, 3, 4} that always contains 1.
- Default `[1, 3, 4]` = Y2.
- Layer 1 stays in every configuration: GUARD is part of the system's design. A configuration without any layer is
  the NOGUARD arm, which is reported but is not a candidate.

**(b) `vol_target` ∈ {fixed, trailing}** (layer 3).
- `fixed` = today: `s_sigma = min(1, sigma_star / sigma_hat_21)`.
- `trailing`: `s_sigma = min(1, sigma_long / sigma_hat_21)`.
  - `sigma_long` is the annualised std (ddof=1, × sqrt(252)) of the last 252 returns of the current reference
    weights held fixed, including the SPY core, ending at the close of t.
  - With fewer than 252 returns, use all available if there are at least 63; otherwise `s_sigma` = 1.
  - A non-finite return in the span is "no signal" (fail open).

Source and declared deviation:
- Moreira & Muir (2017) scale returns by c / σ̂²_t, the previous month's realised variance. c is chosen so that the
  managed portfolio has the unconditional standard deviation of the buy-and-hold portfolio, using the full sample.
  Their footnote 6 notes that this does not affect the Sharpe ratio, because the scaling is linear (PDF p. 6,
  eq. (1)–(2)).
- Our layer differs in three ways: it scales by inverse volatility, it is capped at 1 (de-risk only), and it runs
  daily. With the cap, the target level does matter.
- `trailing` is the point-in-time version of "target = the managed book's own long-run volatility". The 252-session
  window is a declared design choice with no literature value.
- The critique of real-time implementation (Cederburg, O'Doherty, Wang & Yan 2020) could not be read (PDF not
  available) and is declared as not consulted.

| id | guard_layers | vol_target | expectation |
|---|---|---|---|
| Z0 | [1, 3, 4] | fixed | must equal Y2 (identity) |
| Z1 | [1] | – | the stop alone |
| Z2 | [1, 4] | – | expected identical to Z0 (layer 3 never acted in Y2); any difference is reported |
| Z3 | [1, 3] | trailing | stop + book-relative volatility target |
| Z4 | [1, 3, 4] | trailing | Y2 with the book-relative target |

**Adoption A.** A configuration improves on Y2 only if R > 0.8446 **and** mean MDD ≤ 0.0831.
- Among improvements, the highest R wins; ties within 0.001 → lower mean MDD.
- The winner is **A\*** (Z0 if none qualifies). A\* is resolved and committed as a §11 entry before any stage-B run.

## 3. Stage B — satellite weighting (6 configurations × 6 windows = 36 runs, on A\*)

**Common rules for every method.**
- **The set.** At each decision date, the method weights the set S of names that A\*'s min-variance solution holds
  (weight > 1e-6, at most 30), as round 4's risk parity did. The selection is unchanged; only the weighting changes.
- **Inputs.**
  - Σ is the run's own risk model (Ledoit-Wolf constant-correlation, 756 sessions) restricted to S.
  - σ_i = sqrt(diag Σ).
  - The return scenarios (min-CVaR) are the same 756-session return window the risk model was built from,
    restricted to S, with the risk model's own missing-data handling (declared in §11 readings).
- **IPS caps.** Every final satellite weight vector satisfies the same caps as min-variance: name cap 1.5/|S|, sector
  cap 0.30, long only, sum 1. When the caps are infeasible, they are relaxed exactly as min-variance relaxes them.
  - The optimisation methods (B3, B5) include the caps as constraints.
  - The heuristic methods (B1, B2, B4) are mapped onto the feasible set by the Euclidean projection
    `argmin ||w − w_raw||²` subject to the caps. This is a declared design choice with no literature value; López de
    Prado (2016, p. 8) notes that constraints can be introduced in stage 3, and we project after it instead.
  - B6 needs no projection: an average of feasible vectors is feasible.
- **Solvers.** Use quant's existing stack (cvxpy / clarabel; scipy for clustering). The tolerances are fixed in the
  §11 readings before any run. A method that fails to solve at a decision date is a STOP AND ASK, not a silent
  fallback.

| id | method | rule | source (PDF page) |
|---|---|---|---|
| B0 | min-variance | = A\* | control (identity) |
| B1 | equal weight (1/N) | w_i = 1/\|S\| | DeMiguel, Garlappi & Uppal (2009) — not read, declared; discussed in Kirby & Ostdiek (2012, pp. 1–2) and Rodriguez Dominguez et al. (2025, abstract: the arithmetic combiner yields the equal-weighted portfolio) |
| B2 | volatility timing VT(η = 1) | w_i ∝ (1/σ_i²)^η, η = 1 | Kirby & Ostdiek (2012), eq. (12)/(14), pp. 14–15; "η = 1 is a natural choice", p. 24 |
| B3 | maximum diversification | max D(w) = w′σ / sqrt(w′Σw) under the caps (solved as the equivalent convex QP) | Choueifaty & Coignard (2008), eq. (1), p. 2 |
| B4 | hierarchical risk parity | d_ij = sqrt(½(1 − ρ_ij)); d̃ = Euclidean distance between columns of D; single linkage ("nearest point"); quasi-diagonalisation; recursive bisection with inverse-variance split α = 1 − Ṽ₁/(Ṽ₁+Ṽ₂) | López de Prado (2016), pp. 5–8 |
| B5 | minimum CVaR (β = 0.95) | min over (w, a): a + 1/(q(1−β)) Σ_k u_k, with u_k ≥ 0, u_k ≥ −w′y_k − a, plus the caps | Rockafellar & Uryasev (2000), eq. (17) and the LP below it, p. 8; β ∈ {0.90, 0.95, 0.99} in their tables, p. 12 |
| B6 | ensemble (simple average) | arithmetic mean of the weights of B0, B1, B2, B3, B4, B5 and round 4's risk parity on S | Ang & Kim (2026), "simple average" ensemble, printed p. 15; critique: Kahl (2026) |

Evidence, as found (none of it is out-of-sample evidence for our setting):
- Fernandes & Desell (2026), Table I: 50 S&P 500 stocks over 2022–2023. Global minimum variance had the lowest MDD
  (16.84%) and CVaR (2.10%) of their benchmarks; HRP 19.80% / 2.24%; equal weight 21.23% / 2.55%.
- Jaiswal et al. (2026): mean-CVaR suits prudent investment and was stable in COVID. Their out-of-sample span
  (2017–2025) overlaps their training span (2013–2023), and their stocks were chosen by current market cap.
- López de Prado (2016, p. 10): in a Monte Carlo test, HRP had lower out-of-sample variance than CLA's minimum
  variance.

**Adoption B.** A method improves on A\* only if R > R(A\*) **and** mean MDD ≤ 0.0831.
- The highest R wins; ties within 0.001 → lower mean MDD.
- The winner is **FINAL** (A\* if none qualifies). FINAL is committed as a §11 entry before stage D and before the
  stage-C report is generated.

**Count.** Configurations tried after v1 = 45 + 4 (Z1–Z4) + 6 (B1–B6) = **55**.

## 4. Stage C — final measurement and the selection report (generated; reporting only)

Every stage-A and stage-B configuration × window × arm is reported as in rounds 3–4, plus per configuration:
- layer-3 / layer-4 sessions scaled, mean and minimum scale;
- GUARD turnover and cost;
- for stage B, the number of names, the maximum weight, and how often the projection moved the weights.

Tests per window, TOPN_MV_GUARD_NO_NLP: HAC and bootstrap ΔMDD against (i) the stage's control (Z0 or B0) and (ii)
MARKOWITZ_BASE.

**`docs/backtest_selection_summary.md`**, generated by a committed script from the run JSONs (read-only).

Series:
- v1 (V0 of round 2), V3, X5, Y2, A\* (if ≠ Y2), FINAL (if ≠ A\*);
- FINAL's NOGUARD arm;
- MARKOWITZ_BASE, MARKOWITZ_BASE_GUARD, SP500_EW_INTERNAL and SPY.

(a) **Per window:** cumulative / MDD for each of the six windows, mean cumulative, mean MDD and R.

(b) **Full period**, the six windows' daily net returns chained in calendar order (P1, H1, P3, H2, P2, H3;
2022-01-03 → 2026-10-15). Declared: each window starts from its own formation, so the chain has six formation costs.
- CAGR, annualised volatility, Sharpe (excess over French RF);
- **Sortino**: mean excess × 252 / (sqrt(mean(min(excess, 0)²)) × sqrt(252)). Minimum acceptable return = the daily
  risk-free rate; downside risk below a minimum acceptable return as in Sortino & van der Meer (1991).
- **Calmar**: CAGR / chained MDD, as reported by Fernandes & Desell (2026) and Lin et al. (2022);
- chained MDD and the longest time under water, in sessions;
- **CVaR 95%**: the mean of the daily returns at or below the 5% quantile, sign flipped (Rockafellar & Uryasev 2000);
- **beta and Jensen's alpha vs SPY**: OLS of excess returns on SPY's excess returns (Jensen 1968); alpha × 252, with
  a Newey-West t-stat (declared);
- information ratio vs SPY and vs SP500_EW_INTERNAL;
- up/down capture vs SPY: the mean return on SPY-up days over SPY's mean on those days, and the same for SPY-down
  days. Descriptive only; there is no academic source;
- turnover and cost.

(c) **Full-period tests**, FINAL against every other series:
- HAC mean difference (Newey-West, lag floor(4(n/100)^(2/9)));
- Jobson–Korkie with Memmel's correction;
- circular block bootstrap ΔMDD (block 10, 2,999 resamples, seed 7);
- the minimum detectable annualised mean difference at 80% power, 2.8 × HAC s.e. × 252.

(d) **Pareto table** on {CAGR, annual vol, chained MDD, CVaR 95%, Sharpe}: for FINAL against every other series,
which metrics it wins and loses, and whether either series dominates the other.

(e) **Overfitting diagnostics.**

The trial set T: the TOPN_MV_GUARD_NO_NLP daily series, chained over the six windows, of every distinct configuration
tried with LLM scores:
- round 2: V0–V5;
- round 3: X1–X11 (X0 = V3, excluded as a duplicate);
- round 4: Y1–Y4 (the fixed reruns; Y0 = X5);
- this round: Z1, Z3, Z4, Z2 only if it differs from Z0, and B1–B6.

|T| is reported.
1. **PBO by CSCV** (Bailey, Borwein, López de Prado & Zhu 2017; PDF = 2015 revised version, Algorithm 2.3,
   pp. 11–12).
   - S = 16 contiguous blocks; all C(16,8) splits.
   - Primary metric: Sharpe of excess returns. Secondary: Calmar on each half.
   - Report PBO and the mean annualised Sharpe of the in-sample best, in-sample and out-of-sample.
2. **Deflated Sharpe ratio of FINAL** (Bailey & López de Prado 2014, eq. (2), p. 8).
   - Inputs: daily non-annualised SR̂; T = number of sessions; skewness and (non-excess) kurtosis of FINAL's daily
     excess returns; V[SR̂_n] across T; N = |T|.
   - Also report the conservative variant with N = 55, the declared count.
3. **Expected maximum Sharpe under no skill** (Bailey et al. 2014, Proposition 1 and Theorem 2, p. 5):
   - E[max_N] for N = |T| and N = 55;
   - the implied annualised luck threshold E[max_N] / sqrt(years);
   - MinBTL for an annualised Sharpe of 1.
4. **White's Reality Check** (White 2000; stationary bootstrap of Politis & Romano, p. 8; resamples and smoothing
   parameter, p. 13).
   - Models = T; benchmark = MARKOWITZ_BASE, and separately SPY; f = the daily return differential.
   - V = max_k sqrt(n) f̄_k; 1,000 resamples; q = 0.1 (mean block 10) with q = 1 as sensitivity; seed 7.
   - Report the best model, its mean differential and the p-value.
5. **Walk-forward of the selection rule.** Windows in calendar order. After each window k ≥ 2, choose the
   configuration in T with the highest R over windows 1..k, and report its cumulative / MDD and its rank in window
   k+1.

(f) **Reproduction.** Before adding this round's configurations, the script reproduces reviewer A's figures in §10.
- Point estimates must match to the shown decimals.
- p-values, PBO and DSR may differ by implementation (lag rule, resampling): report both. A difference above 0.05 is
  a STOP AND ASK.

## 5. Stage D — robustness to costs (6 runs; reporting only)

FINAL at `cost_bps` = 20 (twice the base), all arms, six windows. Report R, mean MDD, Sharpe and the full-period
CAGR at 10 and 20 bps.
- Motivation: Kirby & Ostdiek (2012, abstract) find that an optimisation's advantage "can easily be eroded by
  transactions costs".
- The 10 bps base has no source (declared, FALTANTES C8).

## 6. Order of work

1. Plan commit (verbatim).
2. §11 implementation readings for stages A and B (parameters, solvers, tolerances, the projection, the CVaR
   scenario window, missing data), before any run.
3. Stage A runs; Z0 identity vs Y2 first.
4. §11 entry resolving A\*.
5. Stage B runs; a B0 control run on P1 must equal A\*.
6. §11 entry resolving FINAL.
7. Stage D runs.
8. Generated reports.
9. Draft PR.

The production hashes are recorded at start and end. Every run copy except FINAL's six is deleted after its JSON is
exported. Check that the free disk space is enough before every batch.

## 7. Data

No new data and no new calibration. The run-copy checks of round 4 §5 (amended) run again, counts only. No model
call. Only aggregates are committed.

## 8. References (PDFs in `documentacion/documentacion_portafolios/faltantes prioritarios`, read by reviewer A)

Methods:
- Rockafellar & Uryasev (2000), J. of Risk 2(3);
- Choueifaty & Coignard (2008), JPM 35(1);
- López de Prado (2016), JPM 42(4) (SSRN 2708678 version);
- Kirby & Ostdiek (2012), JFQA 47(2) (2010 working-paper version: cite pages carefully);
- Moreira & Muir (2017), JF 72(4);
- Rodriguez Dominguez, Shahzad & Hong (2025), arXiv 2501.03919;
- Fernandes & Desell (2026), IEEE, arXiv 2605.28853.

Measurement:
- Bailey, Borwein, López de Prado & Zhu (2014), Notices of the AMS 61(5);
- Bailey, Borwein, López de Prado & Zhu (2017), J. Computational Finance (2015 revised version);
- Bailey & López de Prado (2014), JPM 40(5) (SSRN 2460551);
- White (2000), Econometrica 68(5);
- Jensen (1968), JF 23(2) (2002/2005 re-typeset version);
- Sortino & van der Meer (1991), JPM 17(4) (scanned; OCR'd by reviewer A);
- Zakamulin (2014), J. Asset Management 15(4). Only the abstract is in the PDF: the market-timing performance with
  moving averages is "highly overstated" once tested out of sample with costs. It is relevant to the trend filter.

Already loaded (refs_txt): Ang & Kim (2026), Kahl (2026), Jaiswal et al. (2026), Lin et al. (2022), MadEvolve (2026).

Not available, declared as not read: DeMiguel, Garlappi & Uppal (2009); Cederburg et al. (2020); Munenzon (2015).

## 9. What is not in scope

- NLP (GUARD layer 2): it waits for the partner's output and is a separate, later run.
- No change to selection, scores, vetoes, the core or the filter.
- No new data.

## 10. Reviewer A's figures to reproduce (round-4 series)

1,201 chained sessions, 4.77 years.

| Series | CAGR | Vol | Sharpe | Sortino | Calmar | MDD | Max days under water | CVaR95 | Beta | Alpha/yr (t) | Up/down capture |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Y2 (TOPN_MV_GUARD_NO_NLP) | 7.20% | 8.83% | 0.38 | 0.53 | 0.41 | 17.55% | 630 | 1.29% | 0.44 | −0.59% (−0.28) | 50% / 49% |
| Y2 NOGUARD | 7.96% | 10.18% | 0.41 | 0.57 | 0.44 | 18.00% | 629 | 1.49% | 0.52 | −0.46% (−0.20) | 57% / 56% |
| X5 | 7.06% | 9.11% | 0.36 | 0.49 | 0.41 | 17.37% | 631 | 1.33% | 0.44 | −0.67% (−0.29) | 51% / 50% |
| V3 | 5.73% | 8.82% | 0.23 | 0.31 | 0.33 | 17.28% | 655 | 1.38% | 0.31 | −0.79% (−0.25) | 43% / 43% |
| v1 (V0) GUARD | 5.99% | 11.78% | 0.22 | 0.30 | 0.24 | 25.10% | 659 | 1.69% | 0.48 | −1.77% (−0.46) | 47% / 46% |
| MARKOWITZ_BASE | 8.56% | 10.98% | 0.44 | 0.62 | 0.52 | 16.46% | 561 | 1.54% | 0.36 | +1.63% (0.39) | 34% / 29% |
| SP500_EW_INTERNAL | 9.43% | 16.30% | 0.39 | 0.56 | 0.43 | 22.1% | 488 | 2.26% | 0.86 | −1.36% (−0.43) | 84% / 85% |
| SPY | 12.21% | 17.27% | 0.52 | 0.75 | 0.50 | 24.50% | 488 | 2.47% | 1.00 | 0 | 100% / 100% |

The alpha t-stats above are plain OLS (reviewer A); the report uses Newey-West, so report both.

| Y2 vs | mean diff (pp/yr) | HAC p | JK-Memmel p | ΔMDD (pp) | bootstrap p | min. detectable (pp/yr) |
|---|---|---|---|---|---|---|
| Y2 NOGUARD | −0.83 | 0.300 | 0.671 | −0.45 | 0.816 | ±2.3 |
| X5 | +0.10 | 0.839 | 0.731 | +0.17 | 0.912 | ±1.4 |
| V3 | +1.38 | 0.517 | 0.523 | +0.27 | 0.934 | ±5.9 |
| v1 GUARD | +0.82 | 0.794 | 0.545 | −7.55 | 0.110 | ±8.8 |
| MARKOWITZ_BASE | −1.48 | 0.682 | 0.876 | +1.09 | 0.871 | ±10.1 |
| SPY | −5.67 | 0.202 | 0.563 | −6.95 | 0.204 | ±12.4 |

Overfitting diagnostics over the 21 configurations of rounds 2–4 (v2 V0–V5, X1–X11, Y1–Y4):
- **PBO** (Sharpe, S = 16): **0.893**. In-sample best's mean annualised Sharpe: 0.66 in-sample, 0.02 out-of-sample.
- **E[max_N]:**
  - N = 21: 1.922 → luck threshold 0.88 annualised; MinBTL for SR 1 = 3.69 years.
  - N = 55: 2.313 → luck threshold 1.06; MinBTL 5.35 years.
- **DSR of Y2:** SR̂ 0.02407 daily (0.382 annualised), skew −0.314, kurtosis 4.361.
  - N = 21: SR₀ 0.01064 daily → DSR 0.679.
  - N = 55: SR₀ 0.01280 → DSR 0.651.
- **White's Reality Check** (best of the 21 is v2 V4):
  - vs MARKOWITZ_BASE: +0.38 pp/yr, p 0.772 (q = 0.1) / 0.752 (q = 1).
  - vs SPY: −3.81 pp/yr, p 0.947 / 0.924.
- **Walk-forward:** the R rule picks v2 V4 after every window; its ranks in the next windows are 1, 1, 21, 13 of 21.

## 11. Deviations

Any change to §2–§7 after the commit is written here with its date and reason, before the result it affects is seen.

2026-11-05 — §11 implementation readings, written before any stage-A or stage-B run. These are the choices the code fixes; the plan's §2–§7 are unchanged.

**Where the new knobs enter.** `--guard-layers`, `--vol-target` and `--satellite-weighting` are `cycle.backtest run` arguments. `guard_layers` and `vol_target` are applied only to `TOPN_MV_GUARD_NO_NLP` in `_arm_specs`; `MARKOWITZ_BASE_GUARD` keeps the params file's `[1, 3, 4]` and `fixed`, and `MARKOWITZ_BASE` / `TOPN_MV_NOGUARD` are untouched. The effective values are written to `backtest_run.params_json` as top-level `guard_layers` and `vol_target`. `satellite_weighting` is passed through `_pipeline` to `quant.optimize`'s `run_optimize_topn`; the existing values `minvar` and `rp` and the new values `ew`, `vt`, `maxdiv`, `hrp`, `mincvar`, `ensemble` are allowed. Every default (`guard_layers` absent = the params file's layers; `vol_target="fixed"`; `satellite_weighting="minvar"`) reproduces round 4 bit for bit.

**The 252-session reference returns (stage A, `trailing`).** Layer 3's `sigma_hat` stays the annualised `ddof=1` standard deviation of the last `vol_window` = 21 book returns. For `trailing`, `sigma_long` is the annualised `ddof=1` standard deviation of the last 252 book returns: the current reference weights (the NOGUARD twin's target, including the 0.5 SPY core) held fixed over the sessions ending at the close of t, each session's book return being `sum_i w_i r_i`. With fewer than 252 but at least 63 returns, all available are used; with fewer than 63, `s_sigma = 1`. A non-finite held-name return anywhere in the 252-session span makes the book return non-finite and is "no signal" (`s_sigma = 1`): the engine's strict reference-return series propagates the non-finite value instead of replacing it by 0. The engine's session range is extended to at least 252 sessions before the formation decision so the full window exists at the first GUARD evaluation. The fixed path keeps the existing 21-session series and its 0.0 treatment of a missing return, so Y2's Z0 identity is preserved.

**Stage B: the set S and the inputs.** At each decision date the min-variance solution is computed exactly as in round 4 (pool 60, second pass keeping the 30 largest if needed); S is the resulting book's names with weight > 1e-6 (at most 30), sorted by asset id. Σ is the run's own stored risk model (`ledoit_wolf_cc`, 756 sessions), restricted to S; σ_i = sqrt(diag Σ). The cap inputs are `effective_caps(settings, S, sector_of(S))`, exactly the min-variance second pass's: `n_held = min(30, |S|)`, name cap `1.5 / n_held` (relaxed to `1 / n_held` if that is larger, recorded), sector cap 0.30 (relaxed to the smallest feasible value under the name cap over S, recorded), long only, fully invested. Every method's final vector satisfies these caps. B0 is the min-variance book (the control). B1 is `1/|S|`; B2 is `w_i ∝ (1/σ_i²)^1`; both are projected onto the caps. B3 is the max-diversification QP below; B5 the min-CVaR LP below; both carry the caps as constraints. B4 is HRP below and is projected. B6 is the arithmetic mean of B0, B1, B2, B3, B4, B5 and round 4's risk parity on S; an average of feasible vectors is feasible and is not projected.

**The min-CVaR scenario window and missing data.** The scenarios are the `tr_log_return` rows of the same 756-session window the risk model was built from (`quant.panel.build_return_panel` with the run's `as_of`, `lookback_days=756`, `min_history_days=740`, the same `qret-v2` engine, `min_coverage=0.98`, `max_gap_ffill=1`), restricted to S. That is the risk model's own missing-data handling: names below `max(740, 0.98 T)` observations are dropped (none can be, since every S name is already in the model panel); an interior run of at most one missing day is filled with 0.0 (a flat day); residual missing values are filled with 0.0 after the coverage gate. `q = 756` scenarios, `β = 0.95`, LP: `min_{w,a} a + 1/(q(1-β)) Σ_k u_k`, `u_k ≥ 0`, `u_k ≥ -w'y_k - a`, plus the caps.

**Euclidean projection and cap relaxation.** For B1, B2 and B4 the raw vector is mapped by `argmin ||v - w||²` subject to `sum v = 1`, `v ≥ 0`, the name cap and the sector caps (the same set min-variance uses, with no turnover cap), solved as a QP with CLARABEL at `tol_gap_abs = 1e-12`, `tol_gap_rel = 1e-12`, `tol_feas = 1e-12`. If the raw vector already satisfies the caps within `_CAP_TOL = 1e-9`, the projection returns it unchanged (the identity); after the solve the caps are checked again at `1e-7` and a breach raises rather than being silently kept. The caps are relaxed before any method by `quant.caps.effective_caps` exactly as min-variance relaxes them; no method relaxes them differently.

**HRP.** Correlation `ρ` from Σ; `D_ij = sqrt(0.5 (1 - ρ_ij))`; `d̃` is the Euclidean distance between the columns of D (`scipy.spatial.distance.pdist(D.T)`); `scipy.cluster.hierarchy.linkage(..., method="single")`; the quasi-diagonal order is the paper's Code snippet 1 expansion of the linkage matrix; recursive bisection splits each ordered cluster at `floor(n/2)`, computes each half's variance with inverse-variance weights (`w_i ∝ 1/Σ_ii`, normalised) and splits with `α = 1 - V_left/(V_left + V_right)`. HRP has no solver tolerance; scipy's linkage is exact for the given distances.

**Maximum diversification.** `D(w) = w'σ / sqrt(w'Σw)` is maximised in the scale-free y-space `w = y / sum(y)`: `min y'Σy` subject to `σ'y ≥ 1`, `y ≥ 0`, `y_i ≤ cap · sum(y)` and `sum_{i in sector} y_i ≤ 0.30 · sum(y)`; the homogeneous caps are exactly the caps on w after normalisation. CLARABEL at `tol_gap_abs = 1e-10`, `tol_gap_rel = 1e-10`, `tol_feas = 1e-10`; the solution is normalised and checked.

**Solver tolerances.** New projection: CLARABEL 1e-12 (above). Max-div and min-CVaR: CLARABEL 1e-10 (above). The existing risk-parity SLSQP keeps `ftol = 1e-12`, `maxiter = 1000`. Fallbacks, if CLARABEL fails, are OSQP then SCS with their library defaults (the existing `_solve` order); a method that still fails to solve raises and the run fails — it is a STOP AND ASK, never a silent fallback.

**Stage B reporting.** Each decision records `method`, `names`, `max_weight` and `projected` (whether the Euclidean projection moved the raw B1/B2/B4 vector by more than 1e-12); the run's params JSON keeps the list, and the generated report aggregates it. No per-asset value is written.
