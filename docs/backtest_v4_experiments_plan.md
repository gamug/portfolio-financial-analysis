# System experiments round 4 — plan, fixed before any run

Author: reviewer A, 2026-11-04. Committed verbatim as `docs/backtest_v4_experiments_plan.md`, as the first commit of
`feat/backtest-v4-experiments` (branched from master after the merge of PR #191); that commit is its timestamp. After
it, §2–§5 change only through a dated entry in §7, written before the affected result is seen.

## 1. Why, and what is declared

Round 3 (PR #191) adopted **X5** (pool 60, SPY core 0.5, trend filter with exit fraction 0.5): mean
TOPN_MV_GUARD_NO_NLP MDD 7.91%, mean cumulative 6.34%, R = 0.8014 — above V3 (0.6548) and MARKOWITZ_BASE_GUARD (0.736),
still below MARKOWITZ_BASE (0.850); its gain is concentrated in H1 and P1 is worse than V3. This round tests two levers,
each taken from the thesis references and checked against the PDFs:

- **The per-stock stop.** Layer 1 today is a cumulative-return stop (sum of the last 21 returns below −0.1229) with a
  one-session re-entry (return ≥ 0.0108), inspired by Kaminski & Lo (2014), thresholds calibrated on 2019–2021. It is
  replaced by the **Basing Points** procedure of Edwards, Magee & Bassetti (2007): stops under confirmed minor lows,
  only ever raised, at a distance set by the stock's price and sensitivity.
- **The satellite's weighting.** Minimum variance concentrates the satellite (beta 0.84 vs base 0.53 in P1).
  **Risk parity** (equal risk contribution; AlHalaseh & Al Shawawreh, 2024, §3.6, from Feng & Palomar) spreads the
  risk over the same names.

Evidence status, declared: Edwards reports no performance test of the procedure and warns "By no means will every
issue be amenable to this kind of analysis ... on other issues it will appear to be useless" (p. 447). Reilly & Brown
(2002, p. 181) report that small filter rules earn above-average profits before commissions and losses after them.
AlHalaseh & Al Shawawreh's comparison is in-sample (weights estimated and evaluated on 2018–2022, Amman exchange, no
drawdown reported); their Mann–Whitney test compares weight distributions, not performance. These sources supply the
rules, not evidence that they work here. All six windows have been seen: every result is in-sample and the count of
configurations is reported (§4).

Fixed for every run (as X5): LLM scores (`scored_llm.db` sha256 `2187c36b…`), blend B0·T0, GUARD `params.frozen.toml`
(layers 1, 3, 4), name cap 1.5/30, sector cap 0.30, `ledoit_wolf_cc`, 10 bps, cash at French RF, 12 warm-up steps,
pool_m 60, core_source spy, core_weight 0.5, trend filter on (210 sessions, decision dates only), filter_exit 0.5,
the SPY file `58b91870…`, one fresh copy per run.

## 2. The changes (each a run parameter; defaults reproduce X5 bit for bit)

### (a) `stop_rule` ∈ {kl, bp3, bp4}

`kl` = today's layer 1 (default). `bp3` = Basing Points rules 1–3 (the version of Fig. 210.1, which "uses only bottoms
or lows in stop setting ... to keep the example simple for the general investor", p. 447). `bp4` = rules 1–4 (the
version of Fig. 210, which also advances stops on new highs 3% above the previous high).

**Scope.** TOPN_MV_GUARD_NO_NLP only, satellite names only. Under bp3/bp4 the SPY core has no per-asset stop (the
procedure is for individual stocks and the SPY file has no daily high/low); the core stays under the trend filter and
layers 3 and 4. MARKOWITZ_BASE_GUARD keeps `kl`. Layers 3 and 4 and the combination are unchanged: names cut by the
stop go to zero, then every remaining weight is multiplied by min(1, s_sigma, s_dd).

**Data.** For each satellite name: `price_daily` high, low and close of the run copy (split-adjusted, as stored). A
session without a bar for the name neither updates its machine nor triggers (fail open, as GUARD's NaN rule); every
such name-session is counted and reported.

**The machine** — one per satellite name, evaluated after the close of session t; its decision applies to t+1 (the
timing of layer 1). The rules are Bassetti's codification (pp. 446–447) of Magee's procedure (pp. 441–443):

1. *Tracking start.* s0 = 63 sessions before the entry session e (declared; no reference). e = the session whose
   close-evaluation first sees the name in the reference weights.
2. *Running high.* H = the highest high since s0; h = its session.
3. *Candidate low.* L = the session with the lowest low strictly after max(h, b), where b is the session at which the
   last Basing Point was established (s0 if none). A new candidate needs a strictly lower low (ties keep the earlier
   session).
4. *Away days.* Sessions after L whose low is above the high of L: "The entire price range for 3 full days must be
   entirely above the top price for the day making the low" (p. 441). They need not be consecutive (Fig. 210: 19, 20
   and 28 September, p. 442).
5. *Rule 3.* When L has 3 away days, L becomes a Basing Point established at t: stop ← max(stop, low_L × (1 − d)).
6. *Rule 4 (bp4 only).* A peak P is recognized at the first session after a new running high whose high does not
   exceed it ("A high is made, being recognized by no higher prices occurring for the moment", rule 1). If
   close_t > 1.03 × high_P for the latest recognized peak not yet used, a Basing Point is established at t with low_t:
   stop ← max(stop, low_t × (1 − d)), and that peak is used. The close is compared with the peak's high as in
   Fig. 210 ("the new high closing ... more than 3% higher than the ... Minor Peak", p. 442).
7. *Initial stop at e.* stop = low_B × (1 − d) for the latest Basing Point B established in (s0, e] ("6% below the
   previous Minor Bottom", p. 442). If there is none, or that level is ≥ close_e: stop = (lowest low of the 21
   sessions ending at e) × (1 − d) (declared fallback; no reference).
8. *Only up.* "Protective stops for long stocks can move only up" (p. 443). The book's ex-dividend lowering is not
   applied (declared).
9. *Exit.* close_t < stop → the name is zero from t+1 ("get out ... on any closing violation", p. 445). The book's
   intraday stop order is approximated by the close and next-session execution (declared adaptation). The name stays
   out until the next decision date; if the reference still holds it then, it re-enters with a new machine (new s0,
   new initial stop).
10. *Persistence.* A name that stays in the reference across a decision date keeps its machine and stop; a name that
    leaves the reference drops it.

**Stop distance d** (Table of Stop Distances, p. 437), recomputed at each decision date for every satellite name and
used for the stops set until the next decision date (a stop already set never falls):

| price | conservative (s < 0.75) | median (0.75 ≤ s ≤ 1.25) | speculative (s > 1.25) |
|---|---|---|---|
| > 100 | 5% | 5% | 5% |
| 40 – 100 | 5% | 5% | 6% |
| 20 – < 40 | 5% | 5% | 8% |
| 10 – < 20 | 5% | 6% | 10% |
| 5 – < 10 | 5% | 7% | 12% |
| < 5 | 5% | 10% | 15% |

- *Price* = the name's unadjusted close at the decision date, rebuilt as the stored close × the product of the split
  ratios with ex-date after the decision date (`corporate_action` SPLIT rows). The split-adjusted close would carry
  later splits back into the class (a look-ahead).
- *Sensitivity* s = the OLS slope of the name's daily simple returns on SPY total returns over the 252 sessions
  ending at the decision date (at least 200 pairs, else the median class). Magee's Sensitivity Index is, "for all
  practical purposes", the beta (editor's preface, p. xix). The table's implied-volatility columns are the editor's
  conjecture (p. 437) and are not used.

### (b) `satellite_weighting` ∈ {minvar, rp}

`minvar` = today (default). `rp`: the satellite's names are the ones `minvar` holds at that decision date (after the
pool and the second pass); their weights are re-solved to equalize risk contributions (AlHalaseh & Al Shawawreh, 2024,
eq. 14):

minimize Σ_i Σ_j (w_i (Σw)_i − w_j (Σw)_j)² subject to w ≥ 0, Σ w = 1, name cap 0.05, sector cap 0.30,

with Σ the same `ledoit_wolf_cc` estimate `minvar` uses. Solver: `scipy.optimize.minimize(method="SLSQP")` from equal
weights, Σ scaled by 1 / mean(diag Σ) (the argmin does not change with the scale), `ftol` 1e-12, `maxiter` 1000; a
solve that does not converge stops the run. With the caps the contributions are not exactly equal (declared); per
decision the report gives the number of names, the largest weight and max/min relative risk contribution.

## 3. The grid: 5 configurations × 6 windows = 30 runs

| id | stop_rule | satellite_weighting | note |
|---|---|---|---|
| Y0 | kl | minvar | must equal round-3 X5 (identity check) |
| Y1 | bp3 | minvar | |
| Y2 | bp4 | minvar | |
| Y3 | kl | rp | |
| Y4 | bp* | rp | bp* = whichever of Y1, Y2 has the higher R; ties within 0.001 → lower mean MDD |

Y4 runs after Y1 and Y2; bp* is resolved by this rule and recorded in §7 before Y4 runs. Windows: P1, P3, P2 (P2a/P2b
as cuts), H1, H2, H3. Arms: MARKOWITZ_BASE, MARKOWITZ_BASE_GUARD, TOPN_MV_NOGUARD, TOPN_MV_GUARD_NO_NLP. Context:
SP500_EW_INTERNAL and SPY.

## 4. Reporting and the adoption rule

- Everything round 3 reports, nothing dropped, plus: for bp3/bp4, stop-outs, mean sessions out per stop-out, Basing
  Points established (by rule 3 and rule 4), initial stops that used the fallback, name-sessions without a bar, and
  decision-name counts per distance cell; Y0's layer-1 actions on the SPY core (what bp3/bp4 remove); for rp, per
  decision the names, the largest weight and max/min relative risk contribution.
- Tests per window, TOPN_MV_GUARD_NO_NLP of each configuration: HAC mean difference and bootstrap ΔMDD (v2's function,
  same seed) against (i) Y0 and (ii) MARKOWITZ_BASE.
- Per configuration, over the six windows: mean MDD, mean cumulative, R = mean cumulative / mean MDD, and k = number of
  windows with lower MDD than Y0.
- **Adoption.** A configuration improves on X5 only if R > 0.8014 **and** mean MDD ≤ 0.0831 — round 3's bound (V3 +
  0.5 pp), not re-based on X5, so the drawdown bound does not rise round after round. Among improvements the highest R
  is adopted; ties within 0.001 → lower mean MDD. If none qualifies, X5 stays and the round is reported as tried and
  not adopted.
- Declared count: the round-3 ledger (41 distinct configurations with LLM scores) + 4 new here (Y1–Y4) = 45.

## 5. Data

No new download. `price_daily` high/low/close and `corporate_action` SPLIT rows from each run copy; the SPY file and
manifest as in v2; LLM scores from `scored_llm.db` only (no LLM call). Before any run, report: the share of (satellite
name, session) pairs in the tracking spans without a bar, and, for every name that is ever in a satellite, the number
of SPLIT rows against the number of `split_factor` ≠ 1 rows in the private Tiingo file (counts only). Only aggregates
are committed.

## 6. References (book pages; PDF pages of the thesis copy in brackets)

- Edwards, R. D., Magee, J., & Bassetti, W. H. C. (2007). *Technical Analysis of Stock Trends* (9th ed.). Stop-distance
  formula p. 436 [481]; Table of Stop Distances p. 437 [482]; "three days away" p. 441 [486]; Fig. 210 p. 442 [487];
  stops only up and Basing Points p. 443 [488]; closing violation p. 445 [490]; Bassetti's rules 1–4 and Fig. 210.1
  pp. 446–447 [491–492]; Sensitivity Index ≈ beta, editor's preface p. xix [20]. Ch. 28.1 and the codified rules are
  the editor's (Bassetti).
- AlHalaseh, R. H., & Al Shawawreh, F. K. (2024). Risk parity, §3.6, eq. 9–14 [PDF p. 6]; results Table 7 [p. 9].
- Reilly, F. K., & Brown, K. C. (2002). Filter rules before and after commissions, p. 181 [177].
- Faber (2007/2013), Statman (1987) and Amenc, Malaise & Martellini (2004) as in v2/round 3. The 63-session tracking
  start and the fallback initial stop have no literature value (declared).

## 7. Deviations

Any change to §2–§5 after the commit is written here with its date and reason, before the result it affects is seen.
