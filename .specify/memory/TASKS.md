# TASKS.md — `portfolio-financial-analysis`

Discrete, checkable task breakdown for `.specify/memory/PLAN.md`. Each task
references the plan work item and the `SPEC.md` section it closes. Check a
box only when its acceptance criterion (in `PLAN.md`) is actually met — not
when the code is merely written.

**Closed work items live in `.specify/memory/CHANGELOG.md`**, moved there verbatim
(task IDs unchanged) once every task in them is done, superseded, or moved elsewhere —
see constitution AI behavior #13. This file carries only open work items.

Task IDs are stable, same rule as `SPEC.md`'s `FR-0xx`/`NR-0xx`: don't
renumber; mark a cancelled/superseded task in place instead.

**🔴 Priority override (2026-09-08 forensic audit)**: Work items 8 and 9 below
(`T-070`–`T-084`) are the current top priority — a direct audit against
production data (`data/financial.db`) found live correctness bugs, not
open design work. (Closed Work items 1, 3, 5, 6, 7, 10, 11 and 13 are in `CHANGELOG.md`.)
**Work item 14 (P0/P1, the second audit's live defects) goes first** — `T-105` corrects
metrics the LLM re-run (`T-079`) consumes, and `T-113` must land before it → **8 (P1, supersedes Work item
3/`T-020`–`T-026`)** → Work items 2/4 (unaffected, original priority) →
**9 (P2)** → **Work item 12 (`T-100`), the full-universe run, last of all**. See `PLAN.md`'s "🔴 Priority Override"
section for the full rationale — the source audit markdowns
(`feedback_plan.md`, `upstream_data_mining.md`,
`upstream_portfolio_common.md`) were deleted per the auditor's instruction
after being fully incorporated into `PLAN.md`/this file, which are now the
only durable record.

## Work item 2 — Cross-module orchestrator

- [ ] **T-010** Design decision: a new `orchestrator/`-style package vs. a
      `python -m cycle run-all`-style entrypoint on an existing package —
      pick one, consistent with constitution: Project structure #1's bar
      for a new top-level package. → `PLAN.md` Work item 2, step 1.
- [ ] **T-011** Implement the sequencing runner: pricing_agent →
      fundamental_agent → entity_resolution → cycle → quant, one
      `--analysis-date` fanned out to each step's own CLI/entrypoint,
      relying on each package's existing idempotency rather than
      re-implementing skip logic. → step 2.
- [ ] **T-012** Record orchestrator-level provenance (step, run_id,
      start/end, status) reusing the existing run-log `v_*` pattern. →
      step 3.
- [ ] **T-013** Per-step failure isolation: an independent step's failure
      (e.g. `entity_resolution`) does not block steps with no real
      dependency on it; a dependent step (`cycle` on
      `fundamental_agent`/`pricing_agent`, `quant` on `pricing_agent`) does
      hard-block. → step 4.
- [ ] **T-014** Verify: a single command completes pricing → fundamental →
      entity_resolution → cycle → quant for one `--analysis-date` on a
      fresh universe with no pre-existing data. → `PLAN.md` acceptance
      criteria, first bullet.
- [ ] **T-015** Verify: killing the orchestrator mid-run and re-invoking it
      does not redo an already-completed step. → second acceptance
      criterion.
- [ ] **T-016** Update `SPEC.md` §13 item 3 and §2.2's "out of scope"
      hand-sequencing line to reflect the resolved state. Also update the
      two architecture artifacts per constitution AI behavior #11 —
      reconcile, never rename.

## Work item 4 — SEMANTIC boundary: this repo's half

- [ ] **T-030** Add a `KG_NLP_DB` config seam (mirror
      `entity_resolution/news_db.py`'s `KG_NEWS_DB` pattern), opened
      `mode=ro`, no write path. → `PLAN.md` Work item 4, step 1.
- [ ] **T-031** Implement `ticker` → `asset_id` resolution against the
      local `assets` table on read (option (c) in
      `docs/semantic-score-boundary.md`'s W2). → step 2.
- [ ] **T-032** Extend the `coverage` command with a SEMANTIC check (W14:
      article_count / confidence per as-of member) and add a freshness
      check to `cycle`'s preflight (max processed date vs.
      `--analysis-date`, warn/`--strict` like the existing gate). → step 3.
- [ ] **T-033** Leave the SEMANTIC blend weight at its current placeholder;
      do not flip it up as part of this work item — that's a cross-repo
      decision gated on `portfolio-nlp`'s labelled eval (W4 in the boundary
      doc). → step 4.
- [ ] **T-034** Add a test fixture for the `KG_NLP_DB` read adapter
      mirroring `entity_resolution`'s news-db test pattern. → `PLAN.md`
      acceptance criteria, first bullet.
- [ ] **T-035** Verify `coverage`/`cycle`'s preflight report SEMANTIC
      freshness/coverage the same way EDGAR/pricing coverage is already
      reported. → second acceptance criterion.
- [ ] **T-036** Update `SPEC.md` §13 item 4 to reflect this repo's half
      ready, still flagged not-cut-over pending `portfolio-nlp`. Also
      update the two architecture artifacts per constitution AI behavior
      #11 — reconcile, never rename.

## Work item 8 — P1: methodological redesign (supersedes Work item 3)

- [ ] **T-070** Technical score V2 in `src/cycle/scores/technical.py`
      (12-1 momentum, 90d realized vol, 90d max drawdown, sector-Z
      standardization, `0.50/0.30/0.20` weights) + `BREAK_TREND_200` (SOFT),
      `VOLATILITY_SHOCK`/`CRASH_Z_SCORE` (HARD-temporal, 10-trading-day
      re-evaluation, expiration persisted on `veto`) in
      `rules/builtin.py`. → `PLAN.md` Work item 8, step 1.
- [ ] **T-071** Valorization redesign in `src/cycle/scores/valorization.py`:
      EV-based multiples (`enterprise_fcf_yield` + new `EBITDA/EV`),
      ROIC replacing ROE in the quality factor, remove the size factor,
      robust median/MAD intra-sector standardization, sector-specific
      factors for financials/utilities. → step 2. *(2026-09-25, second audit: also add a
      coverage floor — `_factor_score` averages whichever factors exist, so a name scored on
      one of three is indistinguishable from one scored on all — persist per-factor coverage
      in `inputs_json`, and mark market cap at the cycle date rather than the filing's period
      end.)*
- [ ] **T-072** Persist EBITDA in `fundamental_metrics`
      (`metrics/leverage.py`/`statements.py`) as operating income + D&A. →
      step 2 (EBITDA sub-task).
- [ ] **T-073** Fundamental continuous scoring rubric in `agents.py`
      (4-pillar weighted, one-decimal-place score) eliminating the
      22-discrete-value quantization. → step 3.
- [ ] **T-074** `forensic_flags` end-to-end: extend
      `FundamentalAssessment`/synthesis JSON schema with the 4 booleans,
      write to `score_snapshot.forensic_flags_json`, consume
      `data_error_suspected` as a `cycle` data-quality veto. **Needs
      T-041.** → step 4.
- [ ] **T-075** Ring-2 zero-cost bridge: regex-mine the existing 4,844
      narratives for "data error"/"accounting artifact"/"nonsensical" and
      raise the data-quality veto immediately, ahead of T-073/T-074's full
      re-run. → step 4 (bridge sub-task).
- [ ] **T-076** Skills redesign: 10-Q frequency preamble, tightened
      valuation magnitude gate (`DATA_ERROR_SUSPECTED` on `|FCF yield| >
      50%` or scale mismatch), new dedicated `profitability` skill
      (DuPont, annualization, ROE→ROIC fallback), cost-hygiene trim of
      `SKILL.md` boilerplate. → step 5.
- [ ] **T-077** Carhart 4-factor `ret_estimator` with Vasicek beta
      shrinkage (`mu_i = rf + Σ_k β_i,k^shrunk · λ̄_k` over
      `{MKT,SMB,HML,MOM}`, vendored version-pinned Kenneth French factor
      CSV, 756-day rolling betas) in `risk.py`, registered in
      `optimize.py`'s `--mu` choices; `equilibrium` stays default. Enable
      `turnover_cap` in `optimize.py` + a 10-15bps turnover cost in
      `evaluate.py`. **Supersedes T-020–T-026.** → step 6. *(2026-09-25, second audit:
      `turnover_cap` is inert today — it needs `Constraints.w_prev`, which nothing populates;
      populating it from the prior book is part of this task. Its μ must follow `T-109`'s
      convention.)*
- [ ] **T-078** *(**DEPRECATED 2026-09-25, at the user's direction — not part of the
      development; do not implement.** Kept unchecked as the historical record, per this
      file's "mark cancelled in place, don't renumber" rule. Reason: a meaningful IC needs a
      full-universe, multi-year history of point-in-time rankings (the costly part), and the
      targets below were unlikely to be met in the ~20-month evaluation window even by a good
      signal.)* Compute IC (Spearman, Newey-West SE) + quintile
      monotonicity + strict out-of-sample validation
      (`2022-01-01→2024-12-31` calibration, `2025-01-01→2026-08-27`
      evaluation); record in `docs/quant.md`/`docs/cycle.md`. → step 7.
- [ ] **T-079** One bundled LLM re-run covering T-073+T-074+T-076 together
      (~4,844 filings) — do not re-run per prompt edit; full suite green
      afterward. → `PLAN.md` Work item 8 acceptance criteria.

## Work item 9 — P2: entity-resolution sanitization and `v_quant_vs_live` consumption

- [ ] **T-080** Require an explicit corporate title strictly linked to an
      S&P 500 constituent in `entity_resolution/cooccurrence.py` before
      recording a `shared_executive_edge`. → `PLAN.md` Work item 9, step 1.
- [ ] **T-081** Add a denylist of known media/analyst/wire-service names
      (Reuters, Bloomberg, ForexLive, …). → step 2.
- [ ] **T-082** Route non-executive-but-genuine media co-occurrence into
      `media_cooccurrence` instead of dropping it. **Needs T-043.** →
      step 3.
- [ ] **T-083** Update `api`/`kg_schema` read paths to consume the
      rewritten `v_quant_vs_live`. **Needs T-042.** → step 4.
- [ ] **T-084** Track the production `entity_resolution` re-graph as
      explicitly blocked pending the `urls.db` (`KG_NEWS_DB`, ~4.5GB)
      transfer — not silently treated as done once T-080–T-082 land as a
      code-level fix only. → `PLAN.md` Work item 9 "Blocked by missing
      data" note.

## Work item 14 — P0/P1: second forensic audit (`feedback_plan 1.md`) — defects still live

Added 2026-09-25 from the second audit (`feedback_plan 1.md`, a revised version of the
2026-09-08 audit with new temporal, benchmark and validation sections). Every claim was
re-checked against today's code and production (`KG_FINANCIAL_DB`, read-only); only defects
that **reproduce today** are listed. Items already fixed or tracked (F1/F2/F4-ROA/C1/C2/Q2/Q3/
Ring 1/`v_quant_vs_live`/E1/MD-1), claims that did not reproduce (Q6's current-weights
look-ahead — books are keyed per formation date; split handling — prices and dividends are
consistently split-adjusted), design proposals that are not defects (§4.9 trigger
rebalancing, equal composite weights) and the §5 validation layer (the scope `T-078` was
deprecated for) are left out — see `PLAN.md` Work item 14. → `PLAN.md` Work item 14.

- [x] **T-104** *(P0)* Repair the live book. `portfolio_position` still holds the backdated
      2026-06-30 `cycle select` run's book (the `T-097` incident was not reverted, despite its
      record): all ten weights are that run's 0.10 instead of cycle 1's (2026-09-22) targets
      (0.1167 / 0.075), BF.B is open since 2026-06-30, and WFC's stint closes before it opens
      (`valid_from` 2026-09-22, `valid_to` 2026-06-30). Restore the book to the latest cycle's
      selection, then prevent a recurrence: an integrity check that `valid_to >= valid_from`,
      and `writers.sync_positions` re-weighting by closing the stint and opening a new one
      (today it `UPDATE`s the weight in place, so the previous weight is lost and a revert
      cannot restore it). Correct `T-097`'s CHANGELOG record. **Acceptance**: the live book
      equals cycle 1's selection and weights; no stint with `valid_to < valid_from`; a
      re-weight leaves the old weight in history. **Code done 2026-09-25**: `sync_positions`
      re-weights as close + new stint (same-date re-run in place) and refuses to end a stint
      opened after the cycle date even with `--allow-backdated`; `kg_schema` triggers reject
      `valid_to < valid_from` on insert/update; `cycle undo-run --cycle-run N [--apply]`
      (`cycle/repair.py`) reverts a backdated run. On a copy of production, undoing run 2
      voids BF.B, reopens WFC, restores nine weights: the live book at 2026-09-22 equals
      cycle 1's selection (10 names, targets 0.1167/0.075), 0 inverted stints, run 2
      `reverted`, `quick_check` ok. `T-097`'s CHANGELOG record corrected. **Production repaired
      2026-09-25** (after merge, at the user's direction; no writers running; backup
      `financial.db.pre-t104-undo-backup-20260925`, byte-identical by md5 + `quick_check` ok):
      `cycle undo-run --cycle-run 2 --apply` voided BF.B, reopened WFC and restored nine
      weights — the live book at 2026-09-22 equals cycle 1's selection (10 names, weights sum
      1.0), nothing live at 2026-06-30, 0 inverted stints, run 2 `reverted`, both triggers
      installed, `quick_check` ok, all views query.
- [x] **T-105** *(P0)* Finish F4 for the ratios it deferred. 10-Q metrics still divide one
      quarter's flow by a stock or a price level: FCF yield (and its enterprise and SBC
      variants) 10-Q median 0.9% vs 10-K 3.4% (×3.6–3.9 too small), `net_debt_to_ebitda`
      10.47× vs 2.62× (×4 too large; MCD ~10× on every 10-Q, 2.7× on its 10-K),
      `return_on_invested_capital` 4.0% vs 14.2% (×3.5). ROA and asset turnover, which F4
      fixed, agree within 7%. The FCF yields and ROIC feed `cycle`'s VALORIZATION, so a name
      whose latest filing is a 10-Q scores about 4× worse on value than one on a 10-K. Use the
      existing `db.ttm_flows` path in `valuation.py`, `leverage.py` and `roic.py`; new metrics
      version (`metrics-v3` if still unpersisted in production, else the next). Methodology
      change: `docs/model_fixes.md` record. **Acceptance**: those 10-Q medians within ~1.3× of
      10-K; F4's existing tests unchanged. **Done 2026-09-25**, under `metrics-v3` (still
      unpersisted in production). The TTM now prefers the standard identity `FY(prior 10-K) −
      YTD(last year) + YTD(this year)` — the filing's own YTD columns, so it also covers
      filers whose 10-Q cash flow has no quarterly column (XOM) — then F4's four quarters, then
      ×4, and stamps each group `annualized_ttm`/`annualized_x4`. Valuation, leverage and
      ROIC take the TTM flows (never mixing a raw quarter in); raw values stay in the inputs.
      Recomputed over every production filing of the 20-asset sample (stored facts, read-only):
      10-K/10-Q median ratios FCF yield 0.86, enterprise 0.95, SBC-adjusted 0.94, net debt /
      EBITDA 0.95, ROIC 0.95 (were 3.6–3.9, 0.25, 3.5); 10-Q FCF-yield coverage 97 filings;
      the identity used for 1,789 of 1,891 flows (94.6%), ×4 for 92; no recomputed |FCF yield| > 0.5.
      F4's tests unchanged; +12 tests, mutation-checked. Record: `docs/model_fixes.md` T-105.
      **Review follow-ups (PR #77)**: SBC and interest in the valuation group's provenance;
      TTM reads pinned to the running engine version; identity-vs-four-quarter cross-check
      recorded as a SOFT `DQ_TTM_CROSSCHECK` review (16 of 780 on the sample: AT&T's 2022
      restatement, APA's revenue, one WFC value); `scripts/verify_t105.py`. +4 tests.
- [x] **T-106** *(P0)* Make `cycle`'s readers point in time. `data.latest_metrics` and
      `data.data_quality` pick each asset's filing by `period_end <= cycle_date`, and
      `last_fundamental_dates`/`latest_fundamental_score` by `event_time` (= period end), so a
      cycle on date D reads filings not yet public: the filing gap averages 48.7 days (10-K)
      and 34.4 (10-Q), up to 420. `market_cap_estimates` has no date filter at all (a 2023
      cycle can read a 2026 market cap). Key every reader on `sec_filings.filing_date <=
      cycle_date`. **Acceptance**: a regression test over a historical date proves no fact
      filed after it is reachable.
      **Done 2026-09-25**: every fundamental reader keys on `filing_date <= cycle_date` —
      `latest_metrics`/`data_quality` (one filing per asset), the FUNDAMENTAL score readers
      through the score's own `filing_id` (the orchestrator's duplicate readers folded into
      `data.latest_fundamental_rows`), `market_cap_estimates` and its `quant` mirror
      `load_market_caps(..., as_of=)`; an undated filing is never read as public. The
      FUNDAMENTAL normalization now updates the snapshot it read, by id. Test
      `tests/test_point_in_time_readers.py`: a day-by-day sweep over 18 months finds no read of
      a filing filed after the day. Production: the live 2026-09-22 cycle's reads are
      unchanged; the (reverted) 2026-06-30 run read unfiled filings for 426/503 assets.
- [ ] **T-107** *(P0 — **decided 2026-09-25: option (b)**, PR #78 review; after `T-120`)*
      Give FUNDAMENTAL scores and metrics a publication timestamp. All 377 FUNDAMENTAL `score_snapshot` rows and 11,878
      `fundamental_metrics` rows have `event_time = period_end`, none `filing_date`. SPEC.md
      defines FUNDAMENTAL `event_time` as the period end ("what the score is about"), so the
      choice is either (a) re-key `event_time` to `filing_date`, as the audit proposes (a
      contract change for every `v_score_snapshot` reader and the `UNIQUE(asset_id,
      score_type, event_time)` key), or (b) keep `event_time` and add an explicit nullable
      availability column (`available_at` = `filing_date`), leaving the contract intact.
      Recommendation: (b). Deterministic backfill either way.
      **Decision (b)** — keep `event_time` as the period end and add `available_at`. Option (a)
      would put the filing date in `UNIQUE(asset_id, score_type, event_time)`: the reviewer's
      full-universe DB has 46 (asset, filing date) pairs with more than one FUNDAMENTAL score,
      which would collide and be silently dropped (production today: 42 (asset, filing date)
      pairs with more than one filing, none scored twice yet). Scope:
      - `available_at` on FUNDAMENTAL `score_snapshot` rows and on `fundamental_metrics`,
        backfilled from `sec_filings.filing_date`, and **required non-null** for those rows
        (a test or trigger), so "nullable" cannot become "silently missing".
      - Convention: a filing is usable from the **trading day after** its filing date (EDGAR
        dates an after-close submission with the same day). `available_at` holds that day,
        and `T-106`'s `filing_date <= cycle_date` readers move to it.
      - A test that no as-of reader filters FUNDAMENTAL scores or metrics by `event_time`.
      **Acceptance**: every FUNDAMENTAL score and metric row carries a non-null `available_at`
      (the trading day after its filing date); every as-of reader filters on it; SPEC.md
      updated.
      **Code done 2026-09-26 (PR pending); production `migrate` (m008) pending approval.**
      `available_at` is stored on `sec_filings` too, since the filing pickers and the
      data-quality verdicts key on the filing; metrics and FUNDAMENTAL scores copy it.
      `kg_schema.trading_calendar` is a rule-based NYSE calendar, identical to production's
      1,167-session price spine and the NYSE's 2022–2028 lists. Triggers require the value
      (a metric or FUNDAMENTAL score of an undated filing, or with no filing, is refused) and
      carry a re-dated filing's rows along. `m008` backfills and refuses to leave a gap.
      `cycle`/`quant` refuse to run until it has (`availability.require`). The pipeline skips
      undated filings. Readers: `latest_metrics`, `data_quality`, `latest_fundamental_rows`,
      `market_cap_estimates`, `load_market_caps`, `coverage`. Tests
      `tests/test_available_at.py`, `tests/test_trading_calendar.py`, plus the point-in-time
      readers now requiring the next session; 12 mutations all caught. Production-copy dry
      run: 5,076 filings / 11,878 metrics / 377 scores filled, 0 NULL, 0 mismatches,
      `quick_check` ok; the live 2026-09-22 cycle's reads are unchanged. Record:
      `docs/model_fixes.md` "T-107".
- [ ] **T-108** *(P0)* Fix the internal benchmark (`quant/benchmark.py`). It compounds the
      cross-sectional **mean of log returns**, not `ln(1 + mean simple return)`, so it is lower
      every day by about half the cross-sectional variance: on today's 20-asset panel it
      compounds at 5.98%/yr against a true equal weight of 10.32% (−4.34 pp/yr; the audit
      measured −6.98 pp/yr on the full universe). It also averages every name that has a row
      rather than the gated panel. Every `active_return` in `quant_benchmark_performance`
      inherits the bias. New `bench-v2`; a loader for an external cap-weighted total-return
      series (the series itself is a data-acquisition step). **Acceptance**: the recomputed
      index matches an independent equal-weight calculation to 1e-9.
- [ ] **T-109** *(P0)* One expected-return convention across `quant`. `risk.equilibrium_returns`
      computes `rf + λΣw`, but `persist.py` never passes `rf`, so the stored `equilibrium` μ
      (the default, used by production's risk model) is an *excess* return, while `hist_mean`
      and `james_stein` are total returns — and `max_sharpe`/tangency and every Sharpe subtract
      `rf` again. Pick one convention (total), apply it to every estimator and to `T-077`'s
      Carhart projection. **Acceptance**: a test pins μ as total for all three estimators;
      tangency/Sharpe subtract `rf` exactly once.
- [ ] **T-110** *(P1)* Validate as-of dates against the price spine. Production `quant_run`s
      7–10 and both `cycle_run`s are dated 2026-09-21/22 while `price_daily` ends 2026-08-27;
      `price_observation` has 503 rows at 2026-08-28 (no `price_daily` bar that day) and all
      503 `price_window` "full" rows of one run end 2026-08-28 — a run with `--observations`
      but without `--store-daily` wrote analytics for a bar it never stored. Warn (or refuse
      with an override) when a `quant`/`cycle` as-of is past the last stored price; make
      `pricing_agent` never write observations or windows past `price_daily`'s last date;
      clean the orphan rows. **Acceptance**: zero orphan observation dates; a run past the
      price cutoff is recorded as such.
- [ ] **T-111** *(P1)* `quant evaluate`: a missing asset-day counts as a 0% return
      (`fwd[d].get(a, 0.0)`), dragging the book toward zero in proportion to missing names.
      Renormalize the day's weights over names that have a return. (Transaction costs belong
      to `T-077`.) **Acceptance**: a book with one name missing a day earns the other names'
      renormalized return.
- [ ] **T-112** *(P1)* `optimize.efficient_frontier` returns *k* identical copies of the
      min-variance point, all labelled `optimal`, when the feasible return range collapses.
      Return that one point with an explicit `degenerate` status. **Acceptance**: a collapsed
      frontier yields one point marked degenerate.
- [ ] **T-113** *(P1 — before `T-079`)* LLM score provenance. The rule-based fallback score is
      stored under the model's own name (1 of production's 377 FUNDAMENTAL scores is a fallback
      labelled `deepseek-chat`); `temperature` is 0.2 with no seed; no prompt hash is recorded.
      Label fallback rows as such, set `temperature = 0` (and a seed if the API honours one),
      stamp a prompt hash per score, and record the fallback count on `analysis_run`.
      **Acceptance**: fallbacks are distinguishable from model output in `score_snapshot`;
      every new score carries its prompt hash.
- [ ] **T-114** *(P1)* Clean-tree provenance. Production runs carry `code_version`
      `359797e-dirty` (cycle, quant, analysis runs): results come from uncommitted code. Refuse
      production writes from a dirty checkout unless `--allow-dirty` is passed (and recorded).
      **Acceptance**: a dirty checkout cannot write a run without the explicit override.
- [ ] **T-115** *(P1)* `cycle backfill` cannot replay history: it calls the live
      `run_selection`, which mutates `portfolio_position` — since `T-097` it is refused as
      soon as a newer live book exists, and it has no override — and the checkpoint guard skips
      any already-completed (type, date) with no force option. Route replay positions to a
      separate simulated-book table and add a force/re-run flag. **Acceptance**: a full replay
      leaves the live book untouched and can be re-run after a fix.
- [ ] **T-116** *(P1 — after `T-105`)* Recalibrate the negative-equity distress screen
      (`LEVERAGE_EXTREME` negative-equity branch, `DQ_NEG_EQUITY`'s HARD condition). Both use
      `debt_to_assets > 0.8` or `interest_coverage < 1.5`; the audit shows the first never
      reaches the buyback cohort (MCD 0.665) and proposes the standard credit pair
      `net_debt_to_ebitda` + `interest_coverage`, with NULL on both routed to SOFT review.
      `net_debt_to_ebitda` is only usable once `T-105` annualizes it. Methodology change:
      `docs/model_fixes.md` record. **Acceptance**: thresholds calibrated on annualized data;
      NULL-on-both never passes silently.

- [ ] **T-117** *(P0 — added 2026-09-25 from PR #77's review)* Guard revenue against a
      breakdown figure presented as the company total. APA never filed a consolidated
      `us-gaap:Revenues` (per the reviewer, from SEC companyfacts — to be re-verified); the
      gateway presents a breakdown figure as the total: too small in FY2021 (T-095's case), and
      exactly 2× the consolidated **"Total revenues"** line every year since FY2023 — 16,558 /
      19,474 / 17,840 vs 8,279 / 9,737 / 8,920 ($M); FY2022 is correct (11,075). "Total
      revenues" is not stored as its own fact since FY2023; it is the statement's "Total
      revenues and other" less the lines between the two totals (derivative results,
      divestiture gains, losses on previously sold Gulf properties, other) — verified to the
      $M for FY2022–FY2025 (`scripts/verify_t105.py` derives and prints it). APA 2024Q1–Q3
      revenue also trips the TTM cross-check (11–25%). Reject a revenue total that the filing's own income statement
      contradicts. The rule must be validated on the **full universe** — the number of filings
      it rejects reported and each inspected — not tuned on APA (the mistake `T-095` made).
      Also correct `T-095`'s diagnosis in `docs/model_fixes.md`: APA FY2021 was not a
      filer-side tagging defect but this gateway issue. Methodology change: #12 record.
      **Acceptance**: APA revenue = "Total revenues" (FY2023 8,279; FY2024 9,737; FY2025
      8,920 $M) — not "Total revenues and other", which matches only in FY2024, where the
      in-between items net to zero; the full-universe rejection count reported and inspected;
      `T-095`'s entry corrected.
- [ ] **T-118** *(P1 — upstream, `portfolio-data-mining`; added 2026-09-25 from PR #77's
      review)* Fix the root cause of `T-117`: the EDGAR gateway (`sec_edgar`) presents
      breakdown figures as company totals. Implemented upstream (same pattern as Work item 6);
      this task tracks it and verifies it here once deployed. `T-117` stays as the local guard
      until then. **Acceptance**: the gateway returns APA's statement-level totals; `T-117`'s
      guard no longer rejects APA.
- [ ] **T-119** *(P1 — found 2026-09-25 while testing `T-106`)* `EARNINGS_MISSING` never fires
      for an asset with no FUNDAMENTAL score at all: the rule iterates
      `last_fundamental_dates`, which holds only assets that have one, so its `last is None`
      branch is unreachable. No effect today (all 20 ranked assets are scored); on the
      full-universe run (`T-100`) every unscored member would escape the check. **Decision
      (PR #78 review): an unscored asset is ineligible, not penalized** — no SOFT veto. A
      selection change: #12 record.
      **Acceptance**: a universe member with no public FUNDAMENTAL score is ineligible for
      selection in the same cycle it is detected (not through the T-1 veto lag); it stays in
      the ranking, marked with the reason in `veto_rules_json`, and is listed in the cycle's
      output; if more than 5% of the universe is unscored, the selection cycle stops with an
      error instead of building a portfolio.
- [x] **T-120** *(P0 — PR #78 review; before `T-107`'s backfill and `T-100`)* — **DONE 2026-09-26** Re-ingest the
      legacy pre-`T-091` quarterly rows. Before `T-092` fixed the gateway, one Q3 10-Q per
      year was stored as Q1, Q2 and Q3 rows sharing its accession number and filing date —
      e.g. ALLE 2022: three 10-Qs, accession `0001579241-22-000063`, all filed 2022-10-27.
      Production has 41 such accessions over 13 tickers (AEP, ALLE, AXON, BNY, CSX, DAL, ETR,
      FIS, HPQ, NOC, PNR, REGN, TT; the reviewer counted 15 on the full-universe DB); none has
      metrics yet. Under `T-106` such a Q1/Q2 row reads as public only at Q3's date (late,
      never early), but `T-107` would backfill that wrong date into `available_at`. Re-ingest
      them, make `T-100` unable to resume past them, and add an invariant (test or trigger):
      no two `sec_filings` rows of the same asset share an accession number.
      **Acceptance**: 0 shared accessions per asset in production; the invariant refuses a new
      one; the re-ingested quarters carry their own accession numbers and filing dates.
      **Code done 2026-09-25 (PR pending); production repair pending** — the gateway cannot
      reach SEC today (`Temporary failure in name resolution`). `fundamental_agent
      repair-accessions [--apply] [--drop-unresolved]` (`repair.py`): per shared accession it
      keeps the row for the filing's own (latest) period, finds each stale quarter's own 10-Q
      on the gateway, and only then deletes the stale rows (facts and sections cascade) and
      writes the replacement filing and facts in one transaction — a group whose replacement
      is not found is left as it was, so a gateway outage is safe to re-run; a stale row with
      derived data is refused. Triggers `trg_sf_accession_insert/update` refuse a second row
      of an asset with the same accession; `run` refuses to start while any remain
      (`SharedAccessionsError`), which is what stops `T-100` resuming past them. Production
      plan (read-only): 41 accessions, 13 tickers, 67 stale rows, 15,960 facts, 93 borrowed
      sections, nothing refused; exercised end to end on a production copy (0 shared left,
      metrics and scores untouched, no FK violations). Test `tests/test_shared_accessions.py`
      (real STZ 10-Qs).
      **2026-09-26, gateway back**: the dry run on a production copy found 65 of 67 stale
      quarters; NOC's 2023Q2 and 2024Q2 stale rows carried a period end borrowed from a stray
      "(Q2)" column of the Q3 10-Q (2023-04-25, 2024-05-01), so matching on the period end
      missed their own Q2 10-Qs (ending 06-30). Stale quarters are now matched on the fiscal
      period (the row's key) and take the replacement's own period end. Re-run on the copy:
      67 of 67 found and applied — 0 shared accessions, facts −15,960 borrowed / +13,341 own,
      93 borrowed sections gone, metrics and scores untouched, FK clean. Production backup
      `financial.db.pre-t120-repair-backup-20260926` (md5 `2a2743dff02f3d53892056a4388803e1`,
      `quick_check` ok).
      **Production applied 2026-09-26** (PRs #79, #80; at the user's direction, 2 min 15 s):
      41 shared accessions, 67 quarters replaced, 0 unresolved. Verified: 0 shared accessions;
      `financial_facts` 1,208,620 → 1,206,001; `sec_filing_section` 11,619 → 11,526;
      `fundamental_metrics` (11,878) and `score_snapshot` (497) unchanged; FK check clean;
      `quick_check` ok — identical to the copy. NOC 2023Q2/2024Q2 now end 06-30 (filed
      2023-07-27, 2024-07-25); ALLE 2022 reads as three 10-Qs filed 04-26, 07-28, 10-27.
      The replaced quarters' own narrative sections are not fetched yet (`run --sections`).

## Work item 12 — Final: full-universe production run (runs last of all)

Added 2026-09-25, at the user's direction. Every other task in this file is either
code, or a check on a **small sample** of assets (`T-068`, `T-088`). The full as-of
universe runs exactly once, after **everything else** is done and verified — so a
defect caught late never forces a second full-scale (and, with the LLM step, costly)
re-run. **This work item is always the last one in `TASKS.md`**; a new work item is
added above it, never below. → `PLAN.md` Work item 12.

- [ ] **T-100** *(takes over `T-068`'s former full-universe scope)* Run the whole
      pipeline over the **entire as-of S&P 500 universe** (all 503 assets, not a
      sample) as the very last step of the repository setup: purge the sample-era
      derived data first if a version bump requires it (same mechanism as `T-088`) →
      `fundamental_agent run` for every asset → Ring-1 `data_quality_issue` backfill →
      `cycle select` → `quant backfill-actions`/`build-returns`/`build-risk-model`/
      `optimize`/`evaluate`. **Depends on every other task in `TASKS.md` — every task
      open today and every task added later**: a task added after this one is still a
      prerequisite of it. `T-100` stays unchecked until every other box in this file is
      checked, or explicitly superseded/moved. **Acceptance**: `coverage` for
      `fundamental_agent`, `pricing_agent` and `quant` (`--strict`) reports every as-of
      universe member with core data (or an explained, recorded exception); `cycle`
      ranks the full universe; the `quant` books and `evaluate` run clean on it; the
      run is recorded on the `*_run` log rows with its `code_version`.

## Status

**🔴 Current top priority: Work item 14 (second forensic audit, P0 first), then Work items 8
and 9.** Work items 5, 7 and 13 are closed (2026-09-25) — see `CHANGELOG.md`. `T-070`–`T-084`
and `T-104`–`T-116` have not started. Execute **Work item 14** (`T-104`–`T-109` P0; `T-107`
needs a design decision; `T-113` before `T-079`; `T-116` after `T-105`) → **Work item 8, `T-070`–`T-079` (P1, `T-078` deprecated — `T-074` needs
`T-041`; run only after Work item 7's F1/F2/F4 fixes so the one bundled LLM
re-run scores already-corrected ratios)** → **Work item 9, `T-080`–`T-084`
(P2 — `T-082` needs `T-043`, `T-083` needs `T-042`; the production
`entity_resolution` re-graph is additionally blocked on a `urls.db`
transfer independent of any task here)**.

Work items 1 (done), 3 (superseded by `T-077` — do not implement), 6 (done
upstream + `T-052`), 5 (done 2026-09-25, `T-044` deprecated), 7 (done 2026-09-25), 10 (done),
11 (done 2026-09-25) and 13 (done 2026-09-25) are closed — see
`CHANGELOG.md`.

Work items 2 and 4 are unaffected by the audit and keep their original,
lower priority (after Work items 8 and 9 above): nothing in either has
started; both are unblocked.

**Work item 12 (`T-100`, the full-universe production run) runs last of all**, after
every other task in this file — current and future.
