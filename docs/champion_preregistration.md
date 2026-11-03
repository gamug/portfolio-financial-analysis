# Pre-registration — the defensive configuration search (CHAMPION)

Author: reviewer A. Written 2026-11-03, before any configuration below other than the baseline has been run with the
LLM scores and before any backtest on H1–H3. Status: **draft until committed** to `feat/backtest` (PR #188) as
`docs/champion_preregistration.md`, as that round's first commit; the commit sha and time are its timestamp. After that
commit nothing in §1–§6 changes; a change is a declared deviation (§7).

Declared before the commit: the baseline `B0·T0` (the official configuration) was run with LLM scores on P1 and P2a in
LLM stage 1 (2026-11-03), on the code before the cohort fix, and its results were seen. No other configuration of §3 has
been run with LLM scores. Every run of §4–§5 uses the code at the commit that adds the grid, after the FUNDAMENTAL cohort
fix (normalization over the decision date's universe members only).

Origin: the user's exploratory sweep of 2026-11-01/02 (`scratch/explore/out/SUMMARY.md`, OpenCode-simulated scores, ~29
configurations chosen by P1's drawdown on P1 itself). That sweep is reported as exploration only; this document defines
the search and the test that can support a claim.

## 1. Roles of the data

| role | segments | use |
|---|---|---|
| **Design / selection** | P1 (2022-01-03 → 2022-10-12), P2a (2025-02-19 → 2025-05-06, the first cut of the P2 run) | the selection rule of §4 reads only these |
| **Design / recorded** | P3, P2b | every metric is recorded and published (the bull-market cost); not used to select |
| **Test (unseen)** | H1 (D1 2022-10-13, D_end 2023-10-26), H2 (D1 2024-08-05, D_end 2025-02-18), H3 (D1 2025-09-24, D_end 2026-10-15) | used **once**, for the selected configuration and the baseline only (§5) |

Scoring H filings with the LLM is not a backtest and may happen before §4. Calendar for every window: the driver's own (`E_k = D1 + 21k` NYSE sessions, the decision at the session before `E_k`,
warm-up `W = 12`). Reviewer A's calendar gives 13 / 7 / 13 decision dates for H1 / H2 / H3. No backtest, cycle REPLAY,
return or performance computation may touch an H decision date before §5.

## 2. Fixed for every configuration

- FUNDAMENTAL scorer: the LLM run's configuration A (resolved model DeepSeek-V4.1-Flash, `LLM_THINKING=disabled`,
  temperature 0, prompts `anon-v1`), the same stored ratings for every configuration; filings not scored by the LLM are
  rule-scored on the run copy (warm-up reads). The rule-based scores are the robustness repeat of the selected config.
- GUARD: `params.frozen.toml` sha256 `ee22f352…`, layers 1, 3, 4; no re-freeze.
- Arms `MARKOWITZ_BASE`, `TOPN_MV_NOGUARD`, `TOPN_MV_GUARD_NO_NLP`; Top-30 (`M = 30`), name cap 1.5/30, sector cap 0.30,
  min-var with `ledoit_wolf_cc`, 10 bps costs, cash at French RF; one fresh database copy per run.

## 3. The grid: 5 blends × 5 tilts = 25 configurations

**Blends** (`FUNDAMENTAL, TECHNICAL, VALORIZATION` score weights):

| id | weights |
|---|---|
| B0 (baseline) | 1/3, 1/3, 1/3 |
| B1 | 0.50, 0.25, 0.25 |
| B2 | 0.60, 0.20, 0.20 |
| B3 | 0.45, 0.15, 0.40 |
| B4 | 0.45, 0.40, 0.15 |

**Tilts** (applied to the blended ranking score at each cycle date `t`, data `≤ t` only):

- Market index: the equal-weight return of `members_asof(s − 1)` priced on `s` (the `SP500_EW_INTERNAL` definition),
  computed in-cycle from `quant_return_daily` for sessions `s ≤ t`.
- `R63(t)`, `R126(t)`: its cumulative return over the last 63 / 126 sessions.
- `β_i(t)`: cov(r_i, r_m) / var(r_m) over the last 252 sessions (at least 126 paired returns; otherwise no defensive score
  and the name keeps its blended score).
- `S_def,i(t) = 50 + 10 · z(−β_i(t))`, `z` cross-sectional over the names with a beta.
- Tilted score = `(1 − w) · blended + w · S_def` when the trigger holds, else `blended`.

| id | trigger | w |
|---|---|---|
| T0 (baseline) | never | 0 |
| R1 | `R63 < 0` | 0.25 |
| R2 | `R63 < 0` | 0.40 |
| R3 | `min(R63, R126) < −0.02` | 0.35 |
| R4 | always | 0.15 |

Baseline = `B0·T0` (the official configuration). The code lives in the repository with hand-computed known-answer tests
of the trigger, `β`, `S_def` and the tilted ranking before any grid run.

## 4. Selection rule (design set only)

- Metric: **maximum drawdown**, averaged over 4 numbers: {P1, P2a} × {`TOPN_MV_NOGUARD`, `TOPN_MV_GUARD_NO_NLP`}.
- The selected configuration `C*` is the one with the lowest average; ties (within 0.01 pp) go to the higher average
  cumulative return over the same 4 numbers.
- No return constraint (user's decision 2026-11-03): cumulative return is **recorded**, not required.
- The selection result (all 25 rows and `C*`) is committed before any H run; that commit is the selection's timestamp.
- If `C* = B0·T0`, the search found nothing better; that is the result, and §5 runs `B0·T0` alone on H1–H3 as the
  official system's out-of-sample record.

## 5. The test (run once)

`C*` frozen (its commit sha recorded) against `B0·T0` on H1, H2 and H3, LLM scores (configuration A, the same LLM
database as P), each window on its own fresh copy, same GUARD parameters. Reported whatever the outcome: every metric of §6 per window, segment and arm; the drawdown difference;
the Newey–West HAC test of the daily return difference; the tilt's trigger dates. Hypothesis stated in advance: `C*` has
a lower maximum drawdown than `B0·T0` in the H windows where its trigger fires; low power is stated (≈ 130–270 daily
returns per window).

## 6. Recorded for every configuration, segment and arm (published in full)

Cumulative return, annualized return, annualized volatility, Sharpe (excess over French RF), maximum drawdown, turnover,
cost (bps), rebalances, GUARD actions, realized beta against `SP500_EW_INTERNAL`, the tilt's trigger dates, the number of
configurations tried (25). Aggregates only; no single-asset price or return. The database copies of the grid runs other
than `B0·T0` and `C*` may be deleted after export; each one's input sha256, parameters and exported metrics are kept.

## 7. Deviations

Any change to §1–§6 after the commit is written here with its date and reason, before the result it affects is seen.
