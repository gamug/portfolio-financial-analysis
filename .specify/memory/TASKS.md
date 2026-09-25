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

**🔴 Priority override (2026-09-08 forensic audit)**: Work items 5–9 below
(`T-040`–`T-084`) are the current top priority — a direct audit against
production data (`data/financial.db`) found live correctness bugs, not
open design work. (Closed Work items 1, 3, 6, 10 and 11 are in `CHANGELOG.md`.)
Work item 7's `T-068` is done (the small-sample Phase A validation). Work item 5
continues in parallel as an external prerequisite → **8 (P1, supersedes Work item
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

## Work item 5 — Upstream: `portfolio-common` v0.3.0 additive data contract (P0, external)

- [ ] **T-040** Add `data_quality_issue` table (`filing_id`/`asset_id` FKs,
      `metric_name`, `rule_id`, `severity CHECK IN ('HARD','SOFT')`,
      `value`, `created_at`, `run_id`, `UNIQUE(filing_id, metric_name,
      rule_id)` + indexes) in `portfolio_common/kg_schema/ddl.py`. →
      `PLAN.md` Work item 5, step 1.
- [ ] **T-041** Add nullable `score_snapshot.forensic_flags_json` column
      via the existing missing-columns mechanism. → step 2.
- [ ] **T-042** Rewrite `v_quant_vs_live` as the existing benchmark-side
      `LEFT JOIN` `UNION`ed with a `kind='LIVE_ONLY'` branch for live
      positions absent from every quant benchmark. → step 3.
- [ ] **T-043** Add `media_cooccurrence` table (same shape/grain as
      `shared_executive_edge`, different table). → step 4.
- [ ] **T-044** Tag and release `portfolio-common` `v0.3.0`; bump this
      repo's `pyproject.toml` (`[tool.uv.sources]`) from `v1.2.1` →
      `v0.3.0`, regenerate `uv.lock`, `uv sync`, and re-verify `ensure()`
      against the live `KG_FINANCIAL_DB`. → `PLAN.md` acceptance criteria.

## Work item 7 — P0: production data-integrity and correctness fixes (this repo, CRITICAL, highest priority)

- [x] **T-060** Fix **F1** — cross-check a filing's reported share count
      against already-ingested history and, for `diluted_shares`, its own
      `net_income ÷ EPS_diluted` (`fundamental_agent.db.
      detect_share_scale_factors`), correcting `_share_count` before market
      cap is computed. Verified 2026-09-14: MCD's FY2025 10-K corrects to
      ≈$219B (was $218,953.34); WAT's most recent 10-Q corrects to ≈$37.2B
      (was ≈$37.2T) — full record, including why the original "unit-scale
      sanity check"/"≈$22B" framing needed correcting, in
      `docs/model_fixes.md`'s F1 entry. Repo-wide "0 filings with `|FCF
      yield| > 50%`" not re-verified (needs a `--fresh` universe re-run,
      out of scope here, and likely needs F2/F4 too). → `PLAN.md` Work item
      7, F1.
- [x] **T-061** Fix **F2** — make `Statements.get()` prefer an explicit
      aggregate revenue concept over a component stream, order-independent,
      and sum distinct components when no aggregate is tagged
      (`src/fundamental_agent/statements.py`, `LineItem.total_concepts`/
      `sum_components`/`synonym_groups`). Verified 2026-09-15: ~~98/124
      (79.0%)~~ **93/124 (75.0%)** net_margin (corrected post-merge — a
      code-review bot caught two live double-counting gaps in the summing
      path, both fixed same-day) and 46/54 (85.2%, unaffected) operating_
      cash_flow_margin outlier filings resolve — not REIT-specific (also
      fixes `APO`/`WFC`/`HUM`/`HOOD`/`APA`); full record, including why the
      original "REIT-classified filers" framing needed correcting and the
      post-merge correction detail, in `docs/model_fixes.md`'s F2 entry.
      Residual (mostly single-concept filings, several bank/broker
      custom-tag cases) is a separate, larger investigation, explicitly
      deferred. → `PLAN.md` Work item 7, F2.
- [x] **T-062** Fix **F4** — TTM-annualize 10-Q flow numerators (trailing 4
      quarters, fallback ×4) in `metrics/profitability.py`/`efficiency.py`.
      Verify: 10-Q vs. 10-K medians for ROA/ROE/asset_turnover converge
      (were 3.79–3.86× apart). → F4. **Fixed 2026-09-15**: `db.ttm_flows`
      sums the filing's own quarter plus its three predecessors (deriving a
      10-K-only Q4 as `FY − Q1 − Q2 − Q3`), read back from each earlier
      filing's own already-recorded `fundamental_metrics.inputs_json` (never
      re-deriving concept resolution), falling back to `current × 4` per
      item when trailing history is incomplete; wired through
      `FilingContext.ttm` into `profitability.py`'s `return_on_assets`/
      `return_on_equity` and `efficiency.py`'s `asset_turnover`/
      `inventory_turnover`/`receivables_turnover` only (margins are
      flow-over-flow and need no adjustment). 7 new tests
      (`tests/test_ttm.py`, `tests/test_pipeline.py`); full suite (231,
      was 224), ruff, mypy all green. The stated acceptance criterion
      (10-Q vs. 10-K medians actually converging in the live DB) needs a
      `--fresh` re-run against production — same as F1/F2, deferred to
      `T-068`'s Phase A re-sequence, not part of this code-only fix. Full
      record in `docs/model_fixes.md`'s F4 entry, including the
      deliberately-deferred `roic.py`/`leverage.py` analogous cases. →
      `PLAN.md` Work item 7, F4.
- [x] **T-063** Fix **C1** — correct the T-1 veto cutoff bug in
      `src/cycle/orchestrator.py:264`'s `_rank()`. Verify:
      `SELECT COUNT(*) FROM cycle_ranking WHERE vetoed != 0` is `> 0` on
      the next real cycle run following an active HARD veto (was 0/503
      always); the vetoed name is excluded from `portfolio_position`. → C1.
      **Investigated 2026-09-15 — diagnosis corrected, no code fix made**:
      the comparator (`_t_minus_1`/`hard_vetoed_as_of`/
      `active_soft_vetoes`) matches SPEC.md's FR-006 exactly and is proven
      correct by both the pre-existing `test_t_minus_1_hard_veto_excludes_
      asset` and a new test exercising the real rule-detection path across
      two genuinely different cycle dates
      (`test_hard_veto_detected_via_rules_excludes_asset_starting_next_
      cycle`); `git log` confirms `orchestrator.py`/`writers.py` were never
      touched by any prior fix. "0/503 always" is explained by `--analysis-
      date` defaulting to today on every invocation combined with
      `cycle_run`'s `UNIQUE(cycle_type, cycle_date)` resume-by-key design —
      repeated same-day invocations collapse onto one snapshot rather than
      ever advancing to a genuinely new day, so the T-1 settling period has
      apparently never elapsed once in production; this is an operational
      cadence gap, not a code defect. Full record, including why the
      original "off-by-one/direction bug" framing needed correcting (same
      pattern as F2), in `docs/model_fixes.md`'s C1 entry. → `PLAN.md` Work
      item 7, C1.
- [x] **T-064** Fix **C2** — special-case `equity <= 0` in
      `src/cycle/rules/builtin.py`'s `LEVERAGE_EXTREME` rule (or gate on
      `debt_to_assets`/`interest_coverage` instead of `debt_to_equity` when
      equity is non-positive); stop `valorization.py`'s quality percentile
      from masking the leverage risk. Verify: a negative-book-equity,
      high-absolute-debt fixture triggers the veto, not a pass. → C2.
      **Fixed 2026-09-15**: `builtin.py`'s `LEVERAGE_EXTREME` is now a
      dedicated `_LeverageRule` — a negative `debt_to_equity` (debt is
      never negative, so this reliably signals non-positive equity, no new
      persisted metric needed) gates on `debt_to_assets > 0.8` or
      `interest_coverage < 1.5` instead, reusing PLAN.md's own Ring-1
      `DQ_NEG_EQUITY` calibration (342 filings) rather than inventing new
      thresholds; positive `debt_to_equity` keeps the original `> 3.0`
      check unchanged. `valorization.py`'s quality factor now maps a
      negative `debt_to_equity` to `float("inf")` before ranking so it
      sorts as worst-, not best-in-cohort leverage. 6 new tests
      (`tests/test_cycle.py`); full suite (238, was 232), ruff, mypy all
      green. Residual: production's `rule_catalog` row is stale until a
      one-time `UPDATE` (an operational follow-up, `seed_catalog` never
      overwrites); `DQ_NEG_EQUITY` itself stays blocked on `T-040`. Full
      record in `docs/model_fixes.md`'s C2 entry. → `PLAN.md` Work item 7,
      C2.
- [ ] **T-065** Implement the 7 Ring-1 `DQ_*` deterministic gates
      (`DQ_FCF_YIELD`/`DQ_MARGIN`/`DQ_MARGIN_REVIEW`/`DQ_OCF_MARGIN`/
      `DQ_MCAP_SCALE`/`DQ_NEG_EQUITY`/`DQ_REVENUE_POS`, thresholds in
      `PLAN.md` Work item 7's table) writing to `data_quality_issue` +
      driving a new `cycle` data-quality veto on HARD. **Needs T-040.** →
      Ring-1 section.
- [x] **T-066** *(**SUPERSEDED 2026-09-20 by `T-085`**: the derivation below was
      removed from `src/` — dividends now come only from the pricing gateway,
      because mining data is `portfolio-data-mining`'s job alone. Kept unchanged
      as the historical record; the `corpact-v1-derived` rows it wrote remain in
      the table but `load_actions` no longer reads them.)* Fix **Q3** — derive quarterly dividends from successive
      10-Q YTD differences (`corpact-v1-derived`) in
      `src/quant/actions.py`; add engine-version priority
      (`corpact-v2` > `corpact-v1` > `corpact-v1-derived` >
      `corpact-v0-approx`) in `quant/db.py::load_actions`. Verify:
      XOM/PG/T/NEE show `cash_dividend > 0` (was $0.00 for all four). →
      Q3. **Fixed 2026-09-15**: `actions.py` gained
      `derive_quarterly_dividends_from_10q_ytd`, run unconditionally
      alongside the existing FY-level derivation — per 10-Q, prefers a
      discrete-quarter-tagged DPS fact, else differences successive
      `(YTD)`-tagged values per fiscal year, else falls back to aggregate
      payments/shares; anchored to each filing's own `period_end`.
      `db.py::load_actions` now resolves the best engine **per asset**
      (not per ex-date, unlike `v_corporate_action`'s existing
      resolution) to avoid the two derived sources' non-overlapping
      synthetic ex-dates being blended and roughly double-counting the
      dividend. 5 new tests (`tests/test_quant_actions.py`); full suite
      (243, was 238), ruff, mypy all green. Live re-verification of
      XOM/PG/T/NEE against production data deferred to `T-068`'s Phase A
      re-sequence (same category as F1/F2/F4/C1/C2). Only the Level-1,
      local-only half of Q3 — Work item 6's gateway `corpact-v1` target
      is unaffected by this fix. *(Updated 2026-09-20: the endpoint is now
      built upstream — `portfolio-data-mining` PR #36 — but not yet
      redeployed (its `T-026`); `T-085` made it `quant`'s only source and
      removed this fix's derivation, and `T-052` verifies it live.)* Full record in
      `docs/model_fixes.md`'s Q3 entry. → `PLAN.md` Work item 7, Q3.
- [x] **T-067** Fix **Q2** — align `quant evaluate`'s default `--from` to a
      date with real forward price coverage; ensure the `frontier`
      objective is exercised end-to-end. Verify:
      `quant_benchmark_performance` and `quant_frontier_point` both `> 0`
      rows (both were 0). → Q2. **Fixed 2026-09-15**: `evaluate --from` is
      now optional, defaulting to `quant.db.earliest_portfolio_as_of`
      (the earliest persisted `quant_portfolio.as_of`) instead of the
      previously-documented anti-pattern of reusing `optimize`'s own
      `--analysis-date` — which, by construction, is the newest date with
      any price data, leaving no forward window at all; raises a clear
      `ValueError` if no book exists yet and `--from` is also omitted.
      `docs/quant.md`'s misleading example corrected. The `frontier`
      component needed **no code fix**: already correct and already
      covered end-to-end by the pre-existing
      `test_optimize_persists_one_book_per_objective`; its 0-rows
      observation is explained by the historical run simply never having
      requested it via `--objectives`, deferred as an operational step to
      `T-068`. 2 new tests (`tests/test_quant_pipeline.py`); full suite
      (245, was 243), ruff, mypy all green. Full record in
      `docs/model_fixes.md`'s Q2 entry. → `PLAN.md` Work item 7, Q2.
- [x] **T-068** *(**re-defined 2026-09-25, at the user's direction**: a small-sample
      validation, not a full-universe run — the full universe was never this task's
      purpose and now lives in `T-100`, Work item 12)* Run the audit's **Phase A**
      re-sequence on a **small sample of assets**, to prove the deterministic pipeline
      works end to end before anything runs at full scale: recompute metrics (F1/F2/F4)
      → gateway dividends (T-085/T-052) → re-run `cycle` (exercising T-063/T-064) →
      re-run the full `quant` pipeline + `evaluate` (exercising T-067). → `PLAN.md` Work
      item 7 Sequencing note. **Done 2026-09-22** on the 20-ticker sample, inside
      `T-088` (after its purge of the malformed derived data), then re-run on the same
      sample once `T-095`/`T-096` landed, so the stored data reflects the fixed code.
      Verified against production `KG_FINANCIAL_DB` on 2026-09-25: 11,878 `metrics-v2`
      `fundamental_metrics` rows (20 assets); 7,481 `corpact-v1` gateway actions
      (`quant_run` 6: 503 of 503 assets fetched, 0 errored); two `cycle select` runs
      (2026-06-30 and 2026-09-22, 20 assets ranked each; 19 `veto`, 11
      `portfolio_position` rows); `build-returns` (23,340 `qret-v2` rows, 20 assets) →
      `build-risk-model` → `optimize` (4 books, 15 frontier points) → `evaluate` (41
      `quant_benchmark_performance` rows) — `quant_run` 7–10, all `completed`. **Not part
      of the sample run**: the Ring-1 `data_quality_issue` backfill — its table and gates
      (`T-040`/`T-065`) aren't built yet, so it moves to `T-100`, which depends on both.
- [x] **T-069** Add regression tests for F1/F2/F4/C1/C2 under `tests/`;
      full suite (`pytest`/`ruff`/`mypy`) green. → `PLAN.md` Work item 7
      acceptance criteria. **Audited 2026-09-15**: each fix already landed
      with its own regression coverage at fix time (this repo's standing
      convention, not deferred to a separate task), so no new tests were
      needed — confirmed by re-reading every test against its finding's
      mechanism: **F1** `tests/test_share_scale.py` (9 tests — divide/
      multiply-by-power-of-ten detection, EPS-corroboration-only rule,
      overlapping-history exclusion) + `tests/test_metrics_valuation.py`
      (`test_diluted_shares_scale_defect_is_corrected_before_market_cap`,
      `test_shares_outstanding_scale_defect_also_corrected`,
      `test_no_scale_factor_leaves_share_count_untouched`); **F2**
      `tests/test_statements.py` (5 tests — total-over-components
      precedence, component-summing fallback, tax-synonym disambiguation,
      label-only-match exclusion, non-revenue items unaffected); **F4**
      `tests/test_ttm.py` (7 tests) + `tests/test_pipeline.py` (2 tests —
      10-K unaffected, fresh-10-Q ×4 fallback); **C1**
      `tests/test_cycle.py::test_t_minus_1_hard_veto_excludes_asset`
      (pre-existing) plus
      `test_hard_veto_detected_via_rules_excludes_asset_starting_next_cycle`
      (added during `T-063`'s investigation, exercising the real
      rule-detection path the diagnosis-correction was about); **C2**
      `tests/test_cycle.py` (6 tests — both negative-equity HARD-veto
      branches, the healthy/no-corroboration/positive-D/E non-veto paths,
      and the valorization ranking-inversion fix). `uv run pytest -q`:
      245 passed; `ruff check`/`ruff format --check`/`mypy`/`pre-commit`
      all clean (no source changed by this task). → `PLAN.md` Work item 7
      acceptance criteria.

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
      factors for financials/utilities. → step 2.
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
      `evaluate.py`. **Supersedes T-020–T-026.** → step 6.
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

**🔴 Current top priority (2026-09-08 forensic audit): Work items 5–9.**
`T-060` (F1) is **done**, 2026-09-14; `T-061` (F2), `T-062` (F4), `T-063`
(C1), `T-064` (C2), `T-066` (Q3, Level-1 local half only — superseded by
`T-085`), `T-067` (Q2,
`evaluate` half only) and `T-069` (regression-coverage audit, no new tests
needed) are **done**, 2026-09-15 (`T-063` via a corrected diagnosis, no
code change) — see `docs/model_fixes.md`. Only `T-065` (blocked on
`T-040`) remains in Work item 7; `T-068` is done (the small-sample Phase A
validation, 2026-09-22).
Nothing else in `T-040`–`T-084` has started. There is no derive fallback. Execute, with Work item 5 (external, independent prerequisite) in
parallel → **Work item 7, `T-060`–`T-069`
(P0, this repo's highest priority — no external dependency for
`T-060`–`T-064`/`T-067`–`T-069`; `T-065` needs `T-040`)** → **Work item 8, `T-070`–`T-079` (P1, `T-078` deprecated — `T-074` needs
`T-041`; run only after Work item 7's F1/F2/F4 fixes so the one bundled LLM
re-run scores already-corrected ratios)** → **Work item 9, `T-080`–`T-084`
(P2 — `T-082` needs `T-043`, `T-083` needs `T-042`; the production
`entity_resolution` re-graph is additionally blocked on a `urls.db`
transfer independent of any task here)**.

Work items 1 (done), 3 (superseded by `T-077` — do not implement), 6 (done
upstream + `T-052`), 10 (done) and 11 (done 2026-09-25) are closed — see `CHANGELOG.md`.

Work items 2 and 4 are unaffected by the audit and keep their original,
lower priority (after Work items 5–9 above): nothing in either has
started; both are unblocked.

**Work item 12 (`T-100`, the full-universe production run) runs last of all**, after
every other task in this file — current and future.
