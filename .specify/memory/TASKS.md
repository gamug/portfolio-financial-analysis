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

**🔴 Priority override (2026-09-08 forensic audit; order re-set by the user 2026-10-05)**: Work item 8
(`T-141`, `T-070`–`T-077`) is the current top priority — a direct audit against production data
(`data/financial.db`) found live correctness bugs, not open design work. (Closed Work items 1, 3, 5, 6, 7,
10, 11, 13, 14, 15, 16, 17 and 18 are in `CHANGELOG.md`.) Work item 14 (P0/P1, the second audit's live
defects) is done 2026-09-30; Work item 17 (`T-131`–`T-133`) is closed 2026-10-02; Work item 18 (the N-ticker
weight heuristic, `T-134`–`T-139`) is closed 2026-10-05.

**Order (user, 2026-10-05; scope: finish this repo first):** Work item 8 (`T-141` → `T-072` → `T-076` →
`T-073` → `T-074` → `T-070` → `T-071` → `T-077`) → **Work item 19** (local follow-ups: `T-083`, `T-142`) →
Work item 2 (orchestrator) → **`T-143`, the final pilot** (Work item 12) → **`T-100`**, the full-universe
run, last of all, on a *fresh* `financial.db` (see `T-100`'s own entry). Work item 4 (SEMANTIC; depends on
`portfolio-nlp`) and Work item 9's `T-080`/`T-081`/`T-082`/`T-084` (they change what the knowledge graph
consumes; `T-084` waits on data-mining's `urls.db`) are **deferred until after `T-100`** and are no longer
prerequisites of it. Work item 3 stays superseded by `T-077` (do not implement). One task per PR throughout,
each ending with its status commit after approval.

**Superseded by `T-100` (2026-10-05):** `T-079` (the bundled LLM re-run) — `T-100` starts from a fresh
database and runs the LLM on every filing, so a separate re-run would pay for the same calls twice — and
`T-075` (the Ring-2 narrative bridge) — a stop-gap for production's old narratives, and production stays
frozen until `T-100`, which re-scores everything with the final prompts. **The final pilot gates `T-100`**:
pilot-1 (2026-10-04) ran at 2 FAIL with the original verification (`T-133`, explained by `T-140`); `T-140`
merged 2026-10-05 (PR #115), and its acceptance is measured by `T-143`, which also doubles as `T-014`'s check.
The source audit markdowns (`feedback_plan.md`, `upstream_data_mining.md`, `upstream_portfolio_common.md`)
were deleted per the auditor's instruction after being fully incorporated into `PLAN.md`/this file, which are
now the only durable record. See `PLAN.md`'s "🔴 Priority Override" section for the rationale.

## Work item 2 — Cross-module orchestrator

Placed by the user 2026-10-05: after Work item 19, before the final pilot `T-143` (Work item 12).

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
      criteria, first bullet. *(2026-10-05: the final pilot, `T-143`, doubles as this check —
      one command runs everything on a fresh database.)*
- [ ] **T-015** Verify: killing the orchestrator mid-run and re-invoking it
      does not redo an already-completed step. → second acceptance
      criterion.
- [ ] **T-016** Update `SPEC.md` §13 item 3 and §2.2's "out of scope"
      hand-sequencing line to reflect the resolved state. Also update the
      two architecture artifacts per constitution AI behavior #11 —
      reconcile, never rename.

## Work item 4 — SEMANTIC boundary: this repo's half — DEFERRED until after `T-100`

**Deferred 2026-10-05 (user: finish this repo first).** Reason: it depends on `portfolio-nlp`, a different
repo, and `T-141` removes `SEMANTIC` from the composite weights until this work item lands. It is no longer
a prerequisite of `T-100`.

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

**Order (user, 2026-10-05):** `T-141` → `T-072` → `T-076` → `T-073` → `T-074` → `T-070` → `T-071` → `T-077`.
One task per PR, each ending with its status commit after approval. `T-075` and `T-079` are superseded by
`T-100` (see their entries); `T-078` stays deprecated; `T-140` is merged and its acceptance is measured by
the final pilot, `T-143`.

- [ ] **T-141** Equal composite weights (system review N11, user decision 2026-10-05; resolves the
      composite-weights note that stood before `T-070`/`T-071`). Set `_DEFAULT_WEIGHTS` in
      `src/cycle/config.py` to FUNDAMENTAL, VALORIZATION and TECHNICAL at 1/3 each, and remove
      SEMANTIC until Work item 4. **Why it is mathematically sound:** all three components are
      winsorized cross-sectional z-scores mapped to `50 + 10z` (`src/cycle/scores/normalize.py`), so
      they share one scale and equal weights give equal expected influence; the per-asset
      renormalization over the components present, and the `[0, 100]` clamp, are known effects, to be
      documented. Reference: the 1/N argument (DeMiguel, Garlappi & Uppal 2009), consistent with Work
      item 18. **Verification on scratch copies** (never production): report each component's
      cross-sectional standard deviation and the pairwise correlations; how many assets were blended
      from fewer than three components; and, old `0.4/0.3/0.2` vs the new weights, the rank
      correlation and the top-N overlap on the replay. Amend `SPEC.md` FR-007's default-weights text
      and `docs/cycle.md`, and add a `docs/model_fixes.md` entry (constitution AI behavior #12).
      → `PLAN.md` Work item 8, step 9.
- [ ] **T-070** Technical score V2 in `src/cycle/scores/technical.py`
      (12-1 momentum, 90d realized vol, 90d max drawdown, sector-Z
      standardization, `0.50/0.30/0.20` weights) + `BREAK_TREND_200` (SOFT),
      `VOLATILITY_SHOCK`/`CRASH_Z_SCORE` (HARD-temporal, 10-trading-day
      re-evaluation, expiration persisted on `veto`) in
      `rules/builtin.py`. → `PLAN.md` Work item 8, step 1. *(2026-09-29 system review, scope
      addition: recalibrate `LIQUIDITY_DISTRESS` (N7) — `current_ratio < 1.0` → SOFT flags
      structurally healthy PG, NEE, PM, T, STZ, APA and SBAC (7 of 20); combine it with cash
      coverage (e.g. OCF / current liabilities, interest coverage) and exempt utilities and
      financials. Also add a minimum signal-coverage floor to TECHNICAL (audit C5).)*
- [ ] **T-071** Valorization redesign in `src/cycle/scores/valorization.py`:
      EV-based multiples (`enterprise_fcf_yield` + new `EBITDA/EV`),
      ROIC replacing ROE in the quality factor, remove the size factor,
      robust median/MAD intra-sector standardization, sector-specific
      factors for financials/utilities. → step 2. *(2026-09-25, second audit: also add a
      coverage floor — `_factor_score` averages whichever factors exist, so a name scored on
      one of three is indistinguishable from one scored on all — persist per-factor coverage
      in `inputs_json`, and mark market cap at the cycle date rather than the filing's period
      end.)* *(2026-09-29 system review, scope addition: compute `earnings_yield` from TTM
      net income and the as-of market cap (N8 / audit C1) — `orchestrator._valorization` reads
      `profitability.net_income`/`income_statement.net_income`, neither a stored metric, so the
      value factor uses FCF yields only; keep FUNDAMENTAL normalization in `cycle_ranking`,
      not overwritten on the stored snapshot (N9); ROIC is None when invested capital ≤ 0
      (audit F9). The as-of market cap itself comes from `T-132`'s shared reader.)* *(2026-10-02,
      `T-133` review, scope addition: sector-appropriate cash-flow measures -- capex for utilities
      (`PaymentsForConstructionInProcess`, utility/non-utility split lines) and REITs, and for filers
      whose capex is `PaymentsToAcquireOtherPropertyPlantAndEquipment`/`...OtherProductiveAssets`, which
      `T-133`'s single-line lookup leaves empty rather than partial; see `docs/model_fixes.md` T-133
      "Residual scope".)*
- [ ] **T-072** Persist EBITDA in `fundamental_metrics`
      (`metrics/leverage.py`/`statements.py`) as operating income + D&A. →
      step 2 (EBITDA sub-task). *(2026-09-29 system review, scope addition: net debt
      subtracts short-term investments (audit F7).)*
- [ ] **T-073** Fundamental continuous scoring rubric in `agents.py`
      (4-pillar weighted, one-decimal-place score) eliminating the
      22-discrete-value quantization. → step 3.
- [ ] **T-074** `forensic_flags` end-to-end: extend
      `FundamentalAssessment`/synthesis JSON schema with the 4 booleans,
      write to `score_snapshot.forensic_flags_json`, consume
      `data_error_suspected` as a `cycle` data-quality veto. **Needs
      T-041.** → step 4.
- [ ] **T-075** *(**SUPERSEDED 2026-10-05, at the user's direction — do not implement.** Kept
      unchecked as the historical record, per this file's "mark cancelled in place" rule. Reason: it
      was a stop-gap for production's old narratives until the re-run, and production stays frozen
      until `T-100`, which re-scores everything with the final prompts.)* Ring-2 zero-cost bridge:
      regex-mine the existing 4,844
      narratives for "data error"/"accounting artifact"/"nonsensical" and
      raise the data-quality veto immediately, ahead of T-073/T-074's full
      re-run. → step 4 (bridge sub-task).
- [ ] **T-076** Skills redesign: 10-Q frequency preamble, tightened
      valuation magnitude gate (`DATA_ERROR_SUSPECTED` on `|FCF yield| >
      50%` or scale mismatch), new dedicated `profitability` skill
      (DuPont, annualization, ROE→ROIC fallback), cost-hygiene trim of
      `SKILL.md` boilerplate. → step 5. *(2026-09-29 system review, scope addition, before
      `T-100`'s LLM run: YoY growth returns None when the prior value is ≤ 0 (audit F8);
      quick ratio as (cash + ST investments + receivables) / current liabilities, and cash
      ratio tolerant of a missing cash line (audit F5, F6).)*
- [ ] **T-077** Carhart 4-factor `ret_estimator` with Vasicek beta
      shrinkage (`mu_i = rf + Σ_k β_i,k^shrunk · λ̄_k` over
      `{MKT,SMB,HML,MOM}`, vendored version-pinned Kenneth French factor
      CSV, 756-day rolling betas) in `risk.py`, registered in
      `optimize.py`'s `--mu` choices; `equilibrium` stays default. Enable
      `turnover_cap` in `optimize.py` + a 10-15bps turnover cost in
      `evaluate.py`. **Supersedes T-020–T-026.** → step 6. *(2026-09-25, second audit:
      `turnover_cap` is inert today — it needs `Constraints.w_prev`, which nothing populates;
      populating it from the prior book is part of this task. Its μ must follow `T-109`'s
      convention.)* *(2026-09-29 system review, scope addition: the risk-free CSV path
      refuses an as-of before the series instead of using a future rate (audit Q3);
      optionally, `max_sharpe` falls back to the frontier on solver infeasibility (audit
      Q5).)*
- [ ] **T-078** *(**DEPRECATED 2026-09-25, at the user's direction — not part of the
      development; do not implement.** Kept unchecked as the historical record, per this
      file's "mark cancelled in place, don't renumber" rule. Reason: a meaningful IC needs a
      full-universe, multi-year history of point-in-time rankings (the costly part), and the
      targets below were unlikely to be met in the ~20-month evaluation window even by a good
      signal.)* Compute IC (Spearman, Newey-West SE) + quintile
      monotonicity + strict out-of-sample validation
      (`2022-01-01→2024-12-31` calibration, `2025-01-01→2026-08-27`
      evaluation); record in `docs/quant.md`/`docs/cycle.md`. → step 7.
- [ ] **T-140** (P0, pilot findings 2026-10-04; blocks `T-100`) Two ingestion defects
      found by the 20-asset pilot on a fresh database. → `PLAN.md` Work item 8, step 8.
      **Implemented and approved: PR #115, merged 2026-10-05 (engine `metrics-v5`); acceptance
      pending the final pilot (`T-143`)** -- the box stays open until the criteria below are measured
      on a fresh database. Record: `docs/model_fixes.md`, T-140; `SPEC.md` FR-001.
      Beyond the brief, kept on review: periods ending in the first week of January label as the
      year before (J&J's FY ending 2023-01-01 and 2023-12-31 were both `FY2023`), and a quarter is
      paired with last year's by date (`Statements.prior_of`); the quarter is counted from the
      filing's own balance-sheet fiscal year end first, the stored 10-K as fallback (FERG changed
      its fiscal year). Two APA quarters over `T-117`'s ceiling are admitted only when the gateway's
      own figure agrees to the dollar. Known before the final pilot: APO's Q1-2023 10-Q stays unstored
      (the gateway returns only the prior fiscal year's column; recorded as a run error; the note
      for `portfolio-data-mining` is drafted, not filed), so a "no gap over 110 days" check sees
      APO's 181-day hole unless the recorded run error exempts it (the pilot verification now does).
      IBKR FY2025 and KVUE's first 10-Q after its IPO also get no label; both are recorded.
      **Review's production replay of `scripts/verify_t140.py` (5,076 filings, read-only), a
      pre-count for `T-100`, not its acceptance:** revenue 0 changed (rule fires and equals the
      existing value for MPC, NI, PSX; refused, existing value kept, for ADP, BNY, ICE); labels 95
      relabelled across 51 tickers (73 quarters, 22 January year ends), spot-checked against each
      company's fiscal calendar. `T-100` still counts both on its own fresh database.
      **(a) Revenue the gateway drops.** The gateway's `T-042` rule (`no_filed_nondimensional_fact`)
      removes revenue that a filer tags only with a dimension. APA 2021Q1–2023Q3 (11 filings) then
      has no revenue: FY2022 "Total revenues" $11,075M and production revenues $9,220M are dropped,
      although production stored FY2021 $7,988M (`T-095`'s path, $3M off) and FY2022 $11,075M. Every revenue ratio is
      empty, and `DQ_REVENUE_POS` (HARD) fired 77 times and HARD-vetoes APA (replay: 2024-01-05 to
      2024-02-23). Fix: when no revenue total survives, rebuild it from the "Total revenues and
      other" line minus the rows between it and the missing total, using `T-117`'s label mechanism
      (the rows are already stored in `financial_facts`). Refuse rather than guess when they aren't
      there.
      **(b) Quarters dropped for a repeated label.** The resume key `(ticker, form, fiscal_period)`
      uses the gateway's quarter tag, which is wrong for shifted fiscal calendars. WAT's quarter
      ending 2023-07-01 (fiscal Q2) is tagged `(Q3)`, the same as the real Q3 (2023-09-30), so the
      second filing is skipped as "already done" (`pipeline.py:343-347`) with no error logged.
      Production has 40 names with a 10-Q labelled "Q4" (JNJ, PFE, TMO, AAPL, INTC, DIS, TGT…). Fix:
      key the resume on period end or accession, and derive the quarter label from the fiscal year
      end. Record a run error with a reason whenever a filing yields no quarter of its own (APO
      Q1-2023: the gateway returns only the prior fiscal year's column; note this for
      `portfolio-data-mining`).
      **Acceptance:**
      - On the final pilot (`T-143`; fresh database):
        - APA FY2021 revenue $7,985M, FY2022 revenue $11,075M and 2021Q1–2023Q3 non-null.
        - `DQ_REVENUE_POS` = 0.
        - WAT's 2023-09-30 10-Q stored.
        - No gap over 110 days between consecutive 10-K/10-Q period ends, and no 10-Q labelled Q4.
        - The pilot verification at 0 FAIL.
      - At `T-100`: the number of filings with rebuilt revenue and of corrected labels, counted
        across the full universe and inspected.
      - A `docs/model_fixes.md` entry (methodology change, constitution AI behavior #12). **Done**
        in PR #115.
- [ ] **T-079** *(**SUPERSEDED by `T-100`, 2026-10-05, at the user's direction — do not
      implement.** Kept unchecked as the historical record. Reason: `T-100` starts from a fresh
      database and runs the LLM on every filing, so a separate bundled re-run would pay for the same
      calls twice; the final pilot (`T-143`) gates `T-100`.)* One bundled LLM re-run
      covering T-073+T-074+T-076 together
      (~4,844 filings) — do not re-run per prompt edit; full suite green
      afterward. → `PLAN.md` Work item 8 acceptance criteria.

## Work item 9 — P2: entity-resolution sanitization and `v_quant_vs_live` consumption — `T-080`/`T-081`/`T-082`/`T-084` DEFERRED until after `T-100`

**Deferred 2026-10-05 (user: finish this repo first).** Reason: `T-080`/`T-081`/`T-082`/`T-084` change what
the knowledge graph consumes, and `T-084` waits on data-mining's `urls.db`. They are no longer prerequisites
of `T-100`. `T-083` (local: our API reading `v_quant_vs_live`) moved to Work item 19.

- [ ] **T-080** Require an explicit corporate title strictly linked to an
      S&P 500 constituent in `entity_resolution/cooccurrence.py` before
      recording a `shared_executive_edge`. → `PLAN.md` Work item 9, step 1.
- [ ] **T-081** Add a denylist of known media/analyst/wire-service names
      (Reuters, Bloomberg, ForexLive, …). → step 2.
- [ ] **T-082** Route non-executive-but-genuine media co-occurrence into
      `media_cooccurrence` instead of dropping it. **Needs T-043.** →
      step 3.
- [ ] **T-083** *(**MOVED 2026-10-05 to Work item 19**, text unchanged there; kept unchecked here as
      the historical record.)* Update `api`/`kg_schema` read paths to consume the
      rewritten `v_quant_vs_live`. **Needs T-042.** → step 4.
- [ ] **T-084** Track the production `entity_resolution` re-graph as
      explicitly blocked pending the `urls.db` (`KG_NEWS_DB`, ~4.5GB)
      transfer — not silently treated as done once T-080–T-082 land as a
      code-level fix only. → `PLAN.md` Work item 9 "Blocked by missing
      data" note.

## Work item 19 — Local follow-ups before the final pilot

Added 2026-10-05, at the user's direction (scope: finish this repo first). Local work that needs nothing
from another repo, placed above Work item 12 so it merges before the final pilot (`T-143`). One task per PR,
each ending with its status commit after approval. → `PLAN.md` Work item 19. The knowledge-graph view
changes are not part of this work item yet: a task is added here once the user approves them.

- [ ] **T-083** *(moved from Work item 9, text unchanged)* Update `api`/`kg_schema` read paths to consume the
      rewritten `v_quant_vs_live`. **Needs T-042.** → `PLAN.md` Work item 9, step 4.
- [ ] **T-142** PR #112 review follow-ups, each with a regression test (item (d) is documentation):
      - (a) `cycle/orchestrator.py`: catch `BaseException` and call `conn.rollback()` before writing a
        failed checkpoint or a failed `cycle_run` (today a failure partway through a step saves its
        half-written writes, and a Ctrl-C leaves the run at `running`).
      - (b) `quant/db.py`: remove the inner `conn.commit()` from `insert_risk_model`,
        `insert_expected_returns`, `insert_covariance`, `insert_portfolio`, `sync_positions` and
        `insert_frontier_points`, so each run commits once at `finish_run`.
      - (c) `/portfolio/ranking`: take the latest cycle date only from runs with
        `status = 'completed'` (join `v_cycle_run`; no view change).
      - (d) Document the units of `planned_units`, `completed_units`/`skipped_units` and
        `failed_units` in `docs/fundamental_agent.md` and the `v_analysis_run` notes.

## Work item 12 — Final: full-universe production run (runs last of all)

Added 2026-09-25, at the user's direction. Every other task in this file is either
code, or a check on a **small sample** of assets (`T-068`, `T-088`). The full as-of
universe runs exactly once, after **everything else** is done and verified — so a
defect caught late never forces a second full-scale (and, with the LLM step, costly)
re-run. **This work item is always the last one in `TASKS.md`**; a new work item is
added above it, never below. → `PLAN.md` Work item 12. *(2026-10-05: the final pilot, `T-143`, is the
one sample-sized task placed before `T-100` in this work item; it gates `T-100`.)*

`quant`'s own current 20-ticker sample (`APA, APO, BF.B, CPT, ESS, HOOD, HUM, MA, MCD, NEE, PG,
PM, PSX, SBAC, STZ, T, UDR, WAT, WFC, XOM` — the same 20 `quant_return_daily`/`qret-v2` covers)
is scoped via a checked-in `--universe-db /workspaces/thesis/data/universe_sample20.db` (copied
from the real `universe.db`, filtered to these 20 symbols; `T-122`, 2026-09-29) — a prior
scratchpad copy of the same idea was lost between sessions (an untracked `/tmp` path), which is
why this one lives under the production data directory instead. Pass it explicitly to every
`quant` command until `T-100` runs; omitting `--universe-db` defaults to the full 503-member
universe, which `pricing_agent`/`fundamental_agent` (unaffected by this scoping) already cover
but `quant`'s own `qret-v2`/risk-model chain does not, pending `T-100`.

- [ ] **T-143** Final pilot (user, 2026-10-05): the 20-ticker sample on a **fresh database**, run through
      the Work item 2 orchestrator from a **tagged clean commit**, after Work items 8, 19 and 2 have
      merged. Accepted at `scripts/verify_pilot.py` **0 FAIL** (the reviewer extends the script for each
      merged change). It measures `T-140`'s acceptance and doubles as `T-014`'s check (one command runs
      everything on a fresh database). If it fails, fix and re-run before `T-100`. → `PLAN.md`
      Work item 12.
- [ ] **T-100** *(takes over `T-068`'s former full-universe scope; also takes over `T-079`'s LLM re-run
      and supersedes `T-075`: it runs the LLM on every filing, from a fresh database, with the final
      prompts)* Run the whole
      pipeline over the **entire as-of S&P 500 universe** (all 503 assets, not a
      sample) as the very last step of the repository setup: purge the sample-era
      derived data first if a version bump requires it (same mechanism as `T-088`) →
      `fundamental_agent run` for every asset → Ring-1 `data_quality_issue` backfill →
      `cycle select` → `quant backfill-actions`/`build-returns`/`build-risk-model`/
      `optimize`/`evaluate`. **Depends on every other task in `TASKS.md` — every task
      open today and every task added later — except those the user deferred or superseded on
      2026-10-05**: Work item 4 and Work item 9's `T-080`/`T-081`/`T-082`/`T-084` are deferred until
      after `T-100` and are not prerequisites; `T-075` and `T-079` are superseded by this task.
      `T-083` and `T-142` (Work item 19) and the final pilot `T-143` are prerequisites. A task added
      after this one is still a prerequisite of it, unless the user defers it the same way. `T-100`
      stays unchecked until every other box in this file is checked, or explicitly
      superseded/moved/deferred. **Acceptance**: `coverage` for
      `fundamental_agent`, `pricing_agent` and `quant` (`--strict`) reports every as-of
      universe member with core data (or an explained, recorded exception); `cycle`
      ranks the full universe; the `quant` books and `evaluate` run clean on it; the
      run is recorded on the `*_run` log rows with its `code_version`.
      **Must run on a fresh `financial.db` (PR #104 review, 2026-09-30):** `financial_facts`
      is append-only, `INSERT OR IGNORE` on `(filing_id, statement, concept, period_key,
      filing_version = accession_number)` — a re-run against a database already carrying a
      filing's facts under the same accession changes nothing, even after `T-118`'s gateway
      redeploy. Reproduced directly: re-ingesting APA's FY2023 10-K post-redeploy still
      leaves revenue `16,558,000,000` and COGS `1,076,000,000` (both values `T-042` now
      drops) stored with `correction_rule = NULL`, because that accession's rows already
      existed from before 2026-09-30. Any database ingested before the gateway redeploy
      keeps its pre-`T-042`/pre-`T-118` synthesized facts regardless of how many times
      `fundamental_agent run` runs against it afterward — a new `filing_version` (full
      re-ingest under a bumped engine version, not a same-accession re-run) is the only way
      to replace them. `T-100`'s full-universe run must therefore start from a database with
      no pre-2026-09-30 `financial_facts` rows, not an existing one carried forward — the
      share-count scale cross-check (`detect_share_scale_factors`) and the market-cap
      reader (`T-132`) both read
      `financial_facts` directly and would otherwise silently keep reading synthesized
      values. The pilot is unaffected, since it already starts from a fresh database.

## Status

**🔴 Current priority order (user's, 2026-10-05; scope: finish this repo first):** Work item 8 (`T-141` →
`T-072` → `T-076` → `T-073` → `T-074` → `T-070` → `T-071` → `T-077`) → Work item 19 (`T-083`, `T-142`) →
Work item 2 (orchestrator) → `T-143` (the final pilot) → `T-100` (the full-universe run, last of all).
Work item 4 and Work item 9's `T-080`/`T-081`/`T-082`/`T-084` are deferred until after `T-100`; Work item 3
stays superseded by `T-077` (do not implement). `T-079` and `T-075` are superseded by `T-100`. One task per
PR throughout, each ending with its status commit after approval. Work item 18 (the N-ticker weight
heuristic) closed 2026-10-05, see `CHANGELOG.md`.

Pilot-1 (2026-10-04) ran at 2 FAIL with the original verification (`T-133`, explained by `T-140`); `T-140`
merged 2026-10-05 (PR #115, acceptance pending the final pilot). **The final pilot, `T-143`, gates `T-100`**,
and `T-140` stays open until it measures that acceptance. Work item 17 (`T-131`, `T-132`, `T-133`, PRs
#109/#110/#111) closed 2026-10-02, see `CHANGELOG.md`. Work item 14 (the second forensic audit) is fully
closed 2026-09-30, `T-118` last — see `CHANGELOG.md`. Its `T-121`/`T-122`/`T-123`/`T-124` production actions
were all applied at the user's direction (each task's own entry in `CHANGELOG.md` records its
production-apply date and verification), and `T-125`'s own `migrate` remains the one deliberately-deferred
production write left over from it (still pending explicit user direction, same as
`T-104`/`T-107`/`T-120`'s own precedent); a production `dq-v2` re-gate for `T-116` is deferred the same way.
Production stays frozen until `T-100`.

Work items 1 (done), 3 (superseded by `T-077` — do not implement), 6 (done
upstream + `T-052`), 5 (done 2026-09-25, `T-044` deprecated), 7 (done 2026-09-25), 10 (done),
11 (done 2026-09-25), 13 (done 2026-09-25), 14 (done 2026-09-30), 15 (done 2026-10-01, PR #106) and 16 (done 2026-10-01) are
closed — see `CHANGELOG.md`.

**Work item 12 (`T-143` then `T-100`, the full-universe production run) runs last of all**, after
every other task in this file that is not deferred or superseded — current and future.
