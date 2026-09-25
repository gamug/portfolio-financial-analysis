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
open design work. (Closed Work items 1, 3, 6 and 10 are in `CHANGELOG.md`.)
**Next up (2026-09-21): Work item 11 (P0), in this order —
`T-052` (the live check of the gateway dividends — **done 2026-09-21**), `T-086` (the
`build-returns` guard — **done 2026-09-21**), `T-092` (CRITICAL: integrate `portfolio-data-mining`'s
multi-filing `sec_edgar` endpoints — **done 2026-09-21**), `T-094` (F4 fiscal-calendar mismatch — **done 2026-09-21**), `T-087` (the constitution
amendment — **done 2026-09-21**), `T-090` (metric-version selection and run manifests for `cycle`/`quant` — **done 2026-09-21**), `T-088`
(purge the malformed Fundamental/Quant data and run the 20-ticker deep validation —
**done 2026-09-22**; its own acceptance audit found `T-095`/`T-096`/`T-097`), then, at the
user's explicit direction (2026-09-22), **`T-095`/`T-096`/`T-097` are promoted above `T-089`** —
they are prioritized fixes now, not unprioritized findings, and `T-089` moves to the very end
of the fixing process (its first pass still runs any time after the `T-085` PR merges, but the
delta pass that covers `T-088`/`T-095`/`T-096`/`T-097` waits until all three are done). `T-095`
(revenue mis-resolution — **done 2026-09-22**; also corrected the PM misattribution in its
own PLAN.md/TASKS.md write-up), `T-096` (10-Q filing gaps — **done 2026-09-22**; fixed
WAT's `net_income` concept gap locally, routed APO's gap upstream, and corrected the original
PG/BF.B/STZ tally) and `T-097` (the out-of-order-cycle guard — **done 2026-09-22**; scoped to
`select` only, `monitor` was never at risk) are done. All three prioritized findings are
closed; **`T-089` is next, and last, in the fixing process.**
Work item 7's `T-068` is done (the small-sample Phase A validation). Work item 5
continues in parallel as an external prerequisite → **8 (P1, supersedes Work item
3/`T-020`–`T-026`)** → Work items 2/4 (unaffected, original priority) →
**9 (P2)** → **the low-priority path (2026-09-21): `T-093`**, the user-tunable version
constraints for `quant`'s Markowitz runs, deferred on purpose — to be tackled late, not now
(`T-091` is superseded by `T-092`) → **Work item 12 (`T-100`), the full-universe run, last of all**. See `PLAN.md`'s "🔴 Priority Override"
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
- [ ] **T-078** Compute IC (Spearman, Newey-West SE) + quintile
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

## Work item 11 — P0: follow-ups to the gateway-only cutover — guard, constitution, data purge + 20-ticker validation, artifacts

Added 2026-09-21. Order: `T-052` (Work item 6, live check), `T-086`, `T-092`, `T-094`, `T-087` and `T-090` (all
done 2026-09-21) → `T-088` → `T-095`/`T-096`/`T-097` → `T-089`; `T-093` is **deferred to the low-priority path**
(2026-09-21, at the user's direction). `T-092` was a
**P0 blocker** — the fundamental pipeline could not ingest anything against the live gateway —
and is done; it exposed `T-094` (now also done), which had to land before `T-088`'s re-ingest or
the clean re-run would have activated it. `T-088`'s backup and purge (steps 1–2) were executed early, on 2026-09-21.
`T-090` (added the same day) comes before `T-088` because its deep validation runs
on the versioned readers and on the 10-Q data `T-092` fixes; `T-088` needs `T-090`'s
`--metrics-version`, not `T-093`'s constraint language, so deferring `T-093` blocks nothing;
`T-091` is superseded by `T-092`. **`T-088` is done, 2026-09-22**; its acceptance audit
found `T-095`/`T-096`/`T-097` (revenue mis-resolution, a 10-Q filing gap, an out-of-order
`cycle` run guard). **At the user's explicit direction (2026-09-22), all three are now
prioritized ahead of `T-089`, which moves to the very end of the fixing process** (below,
`T-089`'s own entry). **`T-095` is done, 2026-09-22** — confirmed live for APA FY2021, but
did NOT reproduce for PM (its own PLAN.md/TASKS.md write-up corrected the earlier
misattribution). **`T-096` is done, 2026-09-22** — a precise reproduction of the real TTM
logic found three distinct root causes, correcting the original tally: APO's gap is one
upstream defect cascading (routed, not fixed here), WAT's is a local `net_income`-concept gap
(fixed), and PG/BF.B/STZ's original "×1 each" claim did not survive reproduction (no real
gap). **`T-097` is done, 2026-09-22** — `cycle select`'s "positions" step now refuses an
out-of-order `--analysis-date` unless `--allow-backdated` is given; scope corrected to `select`
only (`monitor` never writes `portfolio_position`). All three findings are closed; `T-089` is
next, and last.
→ `PLAN.md` Work item 11.

- [x] **T-086** Guard against false `build-returns` runs: `run_build_returns`
      (`src/quant/returns.py`) refuses — exit 1, clear message — unless a
      `backfill-actions` `quant_run` covering the build window is `completed` with
      `assets_errored == 0` (both recorded in its `params_json` by `T-085`) and gateway
      `corporate_action` rows exist; an explicit `--allow-no-dividends` override builds a
      knowingly price-only series and records that in `params_json`. Why: `quant_return_daily`
      is `INSERT OR IGNORE` per `(asset, day, engine_version)`, so a series built with no
      dividends is price-only *and* locks in under `qret-v2`. Hermetic tests: no backfill
      run, failed run, run with errored assets, window not covered, override; `pytest`/
      `ruff`/`mypy` green. → `PLAN.md` Work item 11, `T-086`. **Done 2026-09-21**:
      `quant.actions.dividends_not_ready_reason` (next to the `params_json` writer it
      reads) plus `DividendsNotReady`; `run_build_returns(..., allow_no_dividends=False)`
      checks it before `open_run`, so a refusal writes nothing and is not a run; the CLI
      gains `--allow-no-dividends` and exits 1 with the reason and the remedy. Any clean
      covering run suffices — a *later* failed run, or one that completed with errors, does
      not block, because the earlier run's rows are still there (`INSERT OR IGNORE`); a run
      recorded before `T-085` (no `assets_errored`) does not count. The override is loud on
      stderr and recorded in the run's `params_json`. 17 new hermetic tests
      (`tests/test_quant_dividends_guard.py`) including a real backfill-then-build through a
      mocked gateway; full suite (280); the `quant_seed(with_dividends=True)` fixture now also records the
      clean backfill run that would have written its dividends, and the two helpers that
      build price-only series on purpose (panel, risk model) pass the override. Mutation-
      checked: a guard that never refuses, an override that is ignored, errored assets
      tolerated, window coverage unchecked, pre-`T-085` runs accepted, failed runs counted,
      no check for gateway rows, and an exit code of 0 on refusal each fail their own test.
- [x] **T-092** **CRITICAL.** Integrate `portfolio-data-mining`'s multi-filing
      `sec_edgar` endpoints. Its PR #39 ("return all filings for a form+year", merged
      2026-09-21T16:32Z, live on the gateway) is a **breaking change** to two routes:
      `GET /edgar/filing_by_year/{ticker}` now returns a **list** of every matching filing
      (most-recent-first) instead of one object, and "not found" is `success: true, data: []`;
      `GET /edgar/financials/{ticker}` takes an optional `accession_number`, and for a form
      with several filings in the year (a 10-Q: three) an unqualified request now returns
      `success: false` — "Found 3 '10-Q' filings … pass the accession_number". **Verified
      2026-09-21 by calling the live gateway through our own `EdgarClient` (read-only):
      the fundamental pipeline cannot ingest anything.** `filing_by_year` returns a list for
      *every* form, so `pipeline._fetch_meta` calls `.get()` on a list (`AttributeError`,
      which only `EdgarNotFoundError` is caught around) and every unit — 10-K and 10-Q — fails
      at the `process` stage; and `financials(10-Q)` without an accession raises `EdgarError`.
      Build: (1) `EdgarClient` — `filing_by_year` returns the filing list (empty allowed; a
      pre-#39 object payload raises a clear `EdgarError`), `financials(…, accession_number=None)`,
      and the ambiguity reply becomes a distinct `EdgarAmbiguousError` carrying the candidates;
      (2) `pipeline.py` — list each (ticker, form, year)'s filings and ingest **each** one via
      its `accession_number`, oldest first, stamping a filing row only with **its own**
      accession, date and reporting period (comparative columns stay facts and never become a
      filing with a borrowed accession); an empty list is a quiet skip, not an error; (3) order
      chronologically within a ticker so `db.ttm_flows` finds the three prior quarters already
      recorded — F4's true TTM instead of the `current × 4` fallback; (4) captured-payload
      fixtures and tests for the new shapes, replacing the ones built on the old. This also
      resolves `T-091`'s root cause (one 10-Q per year, quarters stamped with a borrowed
      accession). Already-stored mis-attributed rows are repaired under `T-088`. Acceptance and
      the live check in `PLAN.md`. → `PLAN.md` Work item 11, `T-092`. **Done 2026-09-21.** `EdgarClient.filing_by_year` returns `list[FilingRef]` (an
      object payload raises a clear `EdgarError`; an empty list is valid), `financials` takes
      `accession_number`, and the ambiguity reply is `EdgarAmbiguousError(candidates)`;
      "Company not found" is now `EdgarNotFoundError`, which makes the spelling fallback
      work. The pipeline lists a year's filings, ingests **each** by accession **oldest
      first**, gives each **one** target — its own period — so comparatives never become a
      filing with a borrowed accession, skips already-scored accessions on resume without a
      `financials` call, skips a filing dated after the analysis date without fetching it,
      and one failing filing no longer stops its siblings; an empty year is a quiet skip,
      while a company no spelling matches is a visible failure. The old
      `period.year == task.year` filter is gone (`task.year` is the *filing* year, so it also
      dropped a January 10-Q for a December quarter). 18 new hermetic tests on real captured
      payloads (`tests/test_edgar_client.py`, `tests/test_pipeline_multi_filing.py`, fixtures
      `edgar_multi_filing_responses.json` and a real STZ 10-Q whose payload carries a Q1
      comparative), 2 existing tests updated; full suite (298), ruff, mypy green.
      Mutation-checked ten ways (comparatives as filings, no oldest-first sort, a 10-K keeping
      every match, no resume skip, no lookahead guard, only the first filing ingested, an
      unknown company skipped quietly, the old object payload accepted, `accession_number`
      never sent, the ambiguity reply treated as a generic error). **Live check** — the real
      client against the live gateway, on a **scratch copy** of the DB with a stub analyst
      (no LLM), XOM and STZ, 10-K and 10-Q, 2022–2026: 20 tasks → **38 filings ingested, 0
      failed, 0 skipped, 0 errors**; XOM now holds Q1–Q3 in every year (it held one a year),
      STZ likewise including its January-filed 10-Qs; **no (asset, accession) pair carries
      more than one fiscal period**, and STZ's 2022Q1/2022Q2, which used to share
      `0000016918-22-000181`, are now `-000143` and `-000181`. Production untouched; scratch
      copy deleted. F4: XOM's TTM is now real from 2023Q1 (TTM/quarter revenue 3.2–4.7×,
      ≈$334–460B); 2022 still falls back because no 2021 quarters are ingested. **Finding, not
      fixed here**: `_quarter_flow` labels a fiscal year by the calendar year it *ends* in
      (`FY2024` = STZ's year ending Feb-2024) but its quarters by the calendar year each
      *ends* in (`2023Q1`–`Q3` belong to FY2024), so its Q4 derivation `FY{y} − {y}Q1..Q3`
      reads the *next* fiscal year's quarters for a non-calendar filer — verified on real
      stored values: it would read 2024Q2 net income of −$1,199M, STZ's FY2025 Q2
      impairment. The scratch copy hid it only because its Q1 flows were not stored; a clean
      re-ingest would activate it for STZ, BF.B and every other non-December filer. The
      `--fresh` upsert on `(asset, form, fiscal_period)` heals the filing rows' accessions
      but metrics stay `INSERT OR IGNORE`, so `T-088`'s purge is still needed.
- [x] **T-094** **F4: TTM fiscal-calendar alignment for non-calendar filers.**
      Found during `T-092`'s acceptance: `db._quarter_flow`/`ttm_flows` assume a fiscal year's
      label and its quarters' labels line up on the calendar. `FY{y}` is the calendar year the
      fiscal year *ends* in; `{y}Q{n}` is the calendar year each quarter *ends* in — equal only
      for a December year-end. For STZ (year ends Feb) `FY2024` covers Mar-2023..Feb-2024 and
      its quarters are `2023Q1`–`Q3`, so the Q4 derivation `FY{y} − {y}Q1..Q3` reads `2024Q1`–`Q3`,
      the *next* fiscal year's — verified on real stored values: it would read 2024Q2 net income
      of −$1,199M (an FY2025 impairment quarter). The index arithmetic (`fiscal_year × 4 +
      quarter`) is also non-monotonic in time for such a filer (2023Q3 → 2024Q4 → 2024Q1), so
      the "three preceding quarters" are wrong even before Q4. Invisible while one 10-Q per year
      was stored; `T-092` makes it reachable, and a clean re-ingest would activate it for STZ,
      BF.B and every other non-December filer (AAPL, MSFT, NVDA, ORCL …). XOM (calendar) is
      correct. Build: anchor on `period_end` dates, never label arithmetic — the prior quarters
      are the asset's 10-Qs ordered by `period_end` and roughly three months apart, and a Q4 flow
      is the 10-K's FY flow minus the three 10-Qs whose `period_end` falls inside that fiscal
      year; `×4` only when a quarter is genuinely missing. Keep the stored `fiscal_period`
      labels (relabelling is out of scope) but do no arithmetic on them. Evaluate the
      calendar-agnostic alternative — TTM = last FY + current YTD − prior-year YTD, which the
      current 10-Q payload already carries — as a cross-check. Audit every other consumer of
      `FY{y}`/`{y}Q{n}` for the same assumption. A methodology change: constitution AI behavior
      #12 requires real-data verification and a `docs/model_fixes.md` entry. Acceptance and
      tests in `PLAN.md`. → `PLAN.md` Work item 11, `T-094`. **Done 2026-09-21.** `db.ttm_flows(conn, asset_id, *, period_end, current)` now finds a 10-Q's three
      prior quarters by **date** — those ending 3, 6 and 9 months before `period_end`, matched within a
      20-day window, month-end preserving — and a fiscal Q4 as the 10-K ending near that date minus the
      three 10-Qs at 3/6/9 months before *its own* period end, form-matched; `× 4` only when a quarter is
      genuinely missing. No arithmetic on the `fiscal_period` labels remains; the audit found no other
      label arithmetic in the codebase (every other use is an identity key or keys on dates/period
      columns). The stored labels are unchanged. **Real-data verification** (the real client, the live
      gateway, a scratch copy of the purged DB, a stub analyst; XOM Dec, STZ Feb, BF.B Apr, MSFT Jun,
      AAPL Sep 52/53-week; 95 filings ingested, 0 failed): against the independent definition TTM = last
      FY + current YTD − prior-year YTD from each 10-Q's own payload, **52 of 52 quarters agree, maximum
      difference 0.00%**; of the 44 quarters where the *old* code did not fall back to `× 4`, **33 read
      the wrong quarters** — every one a non-calendar filer (AAPL 4/4, MSFT 9/9, BF.B 10/10 up to 69%,
      STZ 10/10 up to 409%: STZ 2025Q1 old +$1,366.5M vs a correct −$442.3M) — while XOM (calendar) was
      right 11/11. Fallback (by design, early quarters of the 2022+ window): XOM 3/14, AAPL 3/15, MSFT
      5/14, BF.B 4/14, STZ 4/14. 13 new hermetic tests (24 in `tests/test_ttm.py` +
      `tests/test_pipeline.py`), incl. the STZ February case with the FY2025 quarters poisoned and
      Saturday quarter ends; full suite (311), ruff, mypy green; mutation-checked seven ways incl. one
      that re-creates the original bug. `docs/model_fixes.md` has the F4 addendum (constitution AI
      behavior #12). Residual: metrics recorded before this fix are wrong for non-calendar filers —
      the derived data was purged (`T-088` step 2), so the recompute is `T-088`'s re-run under
      `metrics-v2`; a transition-period short quarter is untested (it should fall back, not mis-sum).
- [x] **T-087** Amend the constitution: `.specify/memory/constitution.md` "Executable cmds"
      still lists `backfill-actions [--source derive|gateway]` and the flag no longer exists.
      Per its Governance section this is its own reviewed change — fix the line, bump PATCH
      (1.2.0 → 1.2.1), update "Last Amended" and the amendment log, and re-scan for any other
      claim that dividends are derived from filings. → `PLAN.md` Work item 11, `T-087`. **Done 2026-09-21** — constitution **1.2.0 → 1.2.1** (PATCH),
      "Last Amended" 2026-09-21, amendment-log entry added. `quant backfill-actions` no longer
      lists `--source` (annotated "dividends/splits from the pricing gateway (its only
      source)"), and `build-returns` is annotated with the precondition `T-086` made a hard
      rule. **Verification**: every `python -m` line in the block was re-checked against the real
      CLIs' `--help` — 17 of 18 already correct, the `--source` line the only stale one (and
      `quant backfill-actions --source derive` now really fails: `unrecognized arguments`); the
      rest of the document was re-scanned for a claim that dividends are derived from filings
      — none (its only mentions of `quant` are the import-isolation rules, the `quant_*` tables and
      the deterministic-numerics rule); no other reference to version 1.2.0 remains. Docs-only,
      its own change per Governance; no principle changed.
- [x] **T-090** Metric-version selection and run manifests ("version of versions") for `cycle`
      and `quant`, so several runs can coexist on different input versions. Today
      `cycle/data.py` (both metric reads) and `quant/db.py::load_market_caps` read
      `fundamental_metrics` with no `engine_version` filter, a run cannot choose which version
      it uses, and `quant`'s outputs are not keyed by their input versions, so a re-run over a
      new metrics version no-ops or collides (`T-088` step 3 only planned a minimal
      "make the readers engine-aware" — this supersedes it). Build: (1) one shared resolver
      (in `kg_schema`, which both packages may import) mapping a requested selection to a
      concrete `engine_version` per **metric group** (profitability, liquidity, leverage,
      efficiency, growth, cashflow, roic, cagr, valuation) — default *latest present*,
      per-group override, an error if the requested version is absent — with every reader
      going through it and a test that fails on any un-resolved read of `fundamental_metrics`;
      (2) a **run manifest** — the resolved input versions (metrics per group, corpact, return,
      risk-model engines) recorded in `cycle_run`/`quant_run.params_json` with a short hash, and
      carried by the outputs so runs at different manifests write parallel books instead of
      colliding; (3) `--metrics-version` on the `cycle` and `quant` subcommands (default
      latest), printing the manifest. **Settled 2026-09-21: the change is additive** — no
      non-additive `migrate`; the scope is to let the system run different version
      constraints on the quant agent, from user input (`T-093`). Still to confirm: the
      ordering rule for version strings (recommended in `PLAN.md`). Acceptance and tests in
      `PLAN.md`.
      → `PLAN.md` Work item 11, `T-090`. **Done 2026-09-21.** `kg_schema/versions.py` (the pure resolver:
      `resolve_metric_versions`/`choose_versions`/`parse_metric_selection`/`manifest_tag`; the SQL
      in `kg_schema/queries.py::metric_versions_present`), a **static** `VERSION_FILTER_SQL`
      readers apply with one JSON bound parameter (no interpolation — constitution Code & Git #10).
      Every reader now takes the resolved `MetricVersions`: `cycle.data.latest_metrics` and
      `market_cap_estimates`, `quant.db.load_market_caps`; a test fails on any raw
      `fundamental_metrics` read in `src/` lacking the filter, and no view resolves "latest" on its
      own. **quant**: `quant/manifest.py` — the manifest is the `valuation` metric version + the
      return engine (what `quant` actually reads); its 8-hex tag is folded into
      `quant_risk_model.model_version` (`rm-v1+<tag>`) and `quant_portfolio.engine_version`
      (`opt-v1+<tag>`), the keys those tables were **already** unique on, so runs over different
      inputs write parallel rows and the same inputs update in place — **no schema change beyond an
      additive nullable `manifest_json`** on both tables (exposed on their views); `evaluate` needed
      nothing, it already evaluates every book in its window. **cycle**: records the manifest in
      `cycle_run.params_json` and **refuses** to resume a `(type, date)` run built on a different
      manifest (`ManifestMismatch`, checked *before* `open_cycle` so the earlier run is untouched);
      forking cycle outputs would need non-additive key changes, which the scope rules out. CLI:
      `--metrics-version` on `cycle select/monitor/backfill` and `quant build-risk-model/optimize`
      (a version, or `GROUP=VERSION` pairs), printing the manifest; an unstored version or an unread
      group exits 1 before any run row exists. 57 new hermetic tests (368 in the suite), incl. two
      real risk models on different metrics versions with genuinely different equilibrium returns,
      parallel books each tied to its own model, and `evaluate` comparing both; mutation-checked
      twelve ways incl. removing the filter (the original defect) and running the cycle check after
      `open_cycle`. **Live smoke** on a scratch copy of the production DB: the additive columns were
      grafted onto the existing tables, all 31 views still query, and the real CLIs fail cleanly (exit
      1) on an unstored version and an unread group with no run rows created. **Decisions made in
      the build, for review**: (1) the version-ordering rule is the *recommended* one (`pre` <
      `metrics`, then by number) — adopted, not user-confirmed; (2) the manifest covers what each
      consumer reads (so a new version of an unrelated group does not fork `quant`'s books); (3) every
      run is tagged, not only explicit selections, so the keys `rm-v1`/`opt-v1` become
      `rm-v1+<tag>`/`opt-v1+<tag>` — a consumer that filters on the exact old string must filter on
      `manifest_json` instead; (4) one explicit version applies to every group that has rows, while
      `GROUP=VERSION` is strict. **Finding, not fixed**: `market_cap_estimates` and
      `load_market_caps` take no as-of date, so they read the most recent filing even when it is
      dated after the run's `as_of` — a look-ahead in any historical run. `market_cap_estimates` now
      orders by `period_end` (it relied on row order).
- [ ] **T-093** *(**DEFERRED 2026-09-21 — low-priority path**: tackled late, after Work
      item 9, not in the current run of work; nothing in Work items 7–11 depends on it)*
      *(feature; builds on `T-090`)* User-tunable **version constraints** for the
      `quant` agent: the user states which versions of each input `quant` may use, and it
      resolves them against what is stored and runs on the result. Per input — each metric
      group, and the `corpact`, return and risk-model engines — a constraint is `latest`
      (default), exact (`=metrics-v1`), a minimum (`>=metrics-v2`), an exclusion
      (`!=metrics-v1`), or a comma-separated combination. Given as repeated
      `--version-constraint GROUP=EXPR` flags (`--metrics-version EXPR` sets every metric group)
      and/or a `--version-profile FILE` (TOML) of named, reusable profiles; flags win over the
      file. It must resolve to exactly one present version per input (the highest that
      satisfies), otherwise a clear error lists what is present and why each candidate was
      rejected — never a silent fallback. Tuning aids: `quant versions` lists, per input, the
      versions present with row counts and first/last `computed_at`, and `--dry-run` on
      `build-risk-model`/`optimize` prints the resolved manifest and writes nothing. The
      constraints as given and their resolution are recorded in the `T-090` run manifest, so
      runs under different constraint sets are reproducible and comparable. Additive only.
      Acceptance in `PLAN.md`. → `PLAN.md` Work item 11, `T-093`.
- [x] **T-091** *(**SUPERSEDED 2026-09-21 by `T-092`**: `portfolio-data-mining` fixed the
      route (its PR #39, "return all filings for a form+year") while this task was still in
      review, so the upstream half is done and the consumer halves are now `T-092`. Kept
      unchanged below as the record of the root cause.)* Fix 10-Q ingestion. Verified
      2026-09-21 against the DB and the live gateway:
      (1) **one 10-Q per fiscal year** — the pipeline makes one
      `/edgar/financials/{ticker}?form=10-Q&year=Y` call per (ticker, form, year), the route
      takes only `form` and `year`, and it returns a single filing (XOM 2024 → its Q3 filing
      only; STZ 2023 → its Q2 filing), so Q1/Q2 are unreachable: 2,424 of 2,471 (asset, fiscal
      year) pairs hold one 10-Q; (2) **comparative columns become filings with a borrowed
      accession** — `_targets` turns every quarter column in the payload into a filing row
      stamped with the one filing's accession, so 45 (asset, accession) pairs carry several
      fiscal periods (STZ 2022Q1/Q2, ALLE, DAL, PNR, REGN, TT, NOC…) and are scored
      separately (STZ 2024: 48 vs 32 on the same filing); (3) F4's true TTM (`db.ttm_flows`)
      therefore falls back to `current × 4` for most 10-Qs. Build: (a) a consumer fix — a
      filing row only for the payload's own reporting period, comparatives as facts only —
      and repair the mis-attributed rows; (b) per-quarter access: an upstream route in
      `portfolio-data-mining` (`financials` by `quarter` or `accession`; the `filings` list
      route already enumerates accessions), tracked there since data acquisition is that
      repo's job, then ingest Q1–Q3 here; (c) evaluate the alternative that needs no extra
      filing — TTM = last FY + current YTD − prior-year YTD from the current payload plus the
      latest 10-K. Needs an upstream task opened in `portfolio-data-mining` (not yet filed).
      Acceptance and fixtures in `PLAN.md`. → `PLAN.md` Work item 11, `T-091`.
- [x] **T-088** Purge the malformed Fundamental/Quant derived data (**all 503 assets, derived
      data only**; raw EDGAR filings/facts/sections, prices, universe, benchmark, run logs and
      the gateway `corporate_action` rows are kept) and run the **20-ticker deep validation**
      (MCD, WAT, XOM, PG, T, NEE, MA, CPT, UDR, ESS, SBAC, APO, WFC, HUM, HOOD, APA, BF.B, PM,
      STZ, PSX). Steps: fresh backup → one-transaction purge with an explicit table list and
      before/after counts → bump `METRICS_ENGINE_VERSION` to `metrics-v2` (the readers are
      made version-aware by `T-090`, done) → Phase A on
      the 20 tickers (`--tickers`; a 20-member `universe.db` for `cycle`/`quant`) with the
      `T-086` guard active → re-run the data-quality audit. Acceptance in `PLAN.md`; record
      the fraction of 10-Q metric rows still on F4's `current × 4` fallback (the ingestion
      cause is fixed by `T-092`). Consequence: `cycle`, `quant` and the API's `v_*` views are
      empty for all 503 assets until a full re-run (`T-079`). Needs `T-085` merged, `T-052`
      passed, `T-086` built, `T-092` built and `T-090` built. `T-092` may also change the
      "keep the raw ingest" scope for the mis-attributed 10-Q rows; `T-094` must land before
      the re-run. → `PLAN.md` Work item 11, `T-088`. **Steps 1–2 (backup + purge) DONE 2026-09-21, ahead of the rest, at the user's
      direction** (the derived data is malformed regardless of what is built next): backup
      `financial.db.pre-t088-purge-backup-20260921` (byte-identical counts on all 38 tables,
      `quick_check` ok); one transaction, foreign keys enforced, rollback on any surprise —
      **857,387 rows across 19 tables** (`fundamental_metrics` 155,052, `score_snapshot`
      6,354, `fundamental_snapshot_legacy` 356, `quant_return_daily` 580,343,
      `quant_covariance` 106,491, `quant_expected_return` 1,383, `quant_position` 698,
      `quant_portfolio` 5, `quant_risk_model` 1, `cycle_ranking` 1,006, `cycle_checkpoint` 19,
      `cycle_run` 3, `veto` 202, `portfolio_position` 50, `sector_aggregate_snapshot` 11, the
      5,408 retired derived `corporate_action` rows, and `quant_run` runs 1–5); the other 19
      tables are unchanged (raw filings/facts/sections, prices, universe, benchmark, run logs,
      the 7,481 gateway dividends); `PRAGMA foreign_key_check` clean and all 31 read-contract
      views still query (empty). **One refinement of the approved list**: `quant_run` run **6**
      — the T-052 gateway backfill — was *kept*, because it is the provenance record the
      `T-086` guard reads; deleting it would have made `build-returns` refuse until another
      7-minute backfill. `cycle`, `quant` and the API's `v_*` views are now empty for all 503
      assets until the re-run. The file size is unchanged (freed pages are not reclaimed; a
      `VACUUM` is optional). Still open: the `metrics-v2` bump, the readers (`T-090`), the
      F4 fix (`T-094`), and the 20-ticker re-ingest and Phase A. **Steps 3–5 DONE 2026-09-22.**
      Fresh backup `financial.db.pre-t088-rerun-backup-20260921` (identical counts, clean
      `quick_check`). Repaired the 20 tickers' own borrowed-accession rows first (8 STZ
      `sec_filings` rows, 1,900 facts, 14 sections, foreign keys enforced — `T-092`'s repair
      obligation). `METRICS_ENGINE_VERSION` bumped to `metrics-v2` (`bae8355`, 3 tests,
      mutation-checked). 20-member `universe.db` built; Phase A run: `fundamental_agent run`
      (377/379 filings scored, 1 failed — WFC's 2023 10-Q, gateway payload-extraction error,
      not retried) → `cycle select` (10 selected, 2 hard-vetoed) → `quant backfill-actions`/
      `build-returns` (`qret-v2`, 23,340 rows, 18/20 assets with dividends, `T-086` guard
      passed) → `build-risk-model`/`optimize --max-name-weight 0.15` (3 non-degenerate books +
      a 15-point frontier; the 5% default box cap would have forced equal weight on 20 names)
      → `evaluate` (a second, backdated chain at `2026-06-30` gave it a forward window: 42
      benchmark rows, 4 books, 164 performance rows against `SP500_EW_INTERNAL`). **Acceptance**
      (`PLAN.md`): margins mostly plausible (9/754 flagged; 6 genuine, 3 a new distinct defect
      → `T-095`); 10-Q/10-K `asset_turnover` median 1.00 across 262 comparisons (F4 confirmed on
      real rebuilt data); `LEVERAGE_EXTREME` correctly fires for SBAC and correctly does not for
      MCD (C2's guard reads as designed); gateway dividends present for 18/20 (WAT/HOOD
      correctly have none). F4 fallback: 72/278 (25.9%) — 60 are the unavoidable first three
      quarters, 12 are a real filing-set gap → `T-096`. **Found while validating, not part of
      this task's own scope**: an out-of-order `cycle select` run silently mutated the live
      `portfolio_position` book; reverted by hand and confirmed restored → `T-097`. Full detail,
      including the APA/PM/WAT/APO specifics, in `PLAN.md`.
- [x] **T-095** *(found by `T-088`'s acceptance, 2026-09-22; **prioritized above `T-089` at the
      user's direction, 2026-09-22; done 2026-09-22**)* Revenue mis-resolution when a
      filer's own "total" tag is a sub-line, not the aggregate: `LineItem.total_concepts`
      (`statements.py`) lets a `us-gaap_Revenues`-family match win outright over every
      `concepts` candidate; confirmed live for APA FY2021 (a non-dimensional
      `us-gaap_Revenues` row mistagged with a dimensional equity-method-investee value,
      $1,082M, vs. a true $7,988M — net margin 121% instead of ≈16%), a
      different shape from the already-fixed C1 (CPT's 127×). **PM was NOT reproduced** —
      re-read live, PM tags no `total_concepts` at all in either FY2021 or FY2022; its
      resolution (the ExcludingAssessedTax figure, F2's already-verified rule) was correct
      all along, so the original flag on PM is corrected here as a misattribution. **Fix**:
      a new plausibility floor (`Statements._TOTAL_PLAUSIBILITY_FLOOR = 0.5`) rejects a
      `total_concepts` match under half the largest named `concepts` candidate, falling
      through to Tier 2; a no-comparison filer (JPM) is unaffected. 3 new tests
      (375 total), mutation-checked. `docs/model_fixes.md` entry added (constitution AI
      behavior #12). → `PLAN.md` Work item 11, `T-095`.
- [x] **T-096** *(found by `T-088`'s acceptance, 2026-09-22; **prioritized above `T-089` at the
      user's direction, 2026-09-22; done 2026-09-22**)* 10-Q filing gaps beyond F4's
      expected "first three quarters" fallback. A precise reproduction of the actual
      `db.ttm_flows`/`_quarter_flow_ending` logic (not a re-guess) found **three** distinct
      root causes, correcting the original "12: APO×8, one each PG/BF.B/STZ/WAT" tally: (1)
      **APO — confirmed upstream**: its 2023Q1 10-Q's `/financials` payload carries only the FY
      period for income/cash-flow, so it's silently never scored; all 8 of APO's flagged
      filings trace to this **one** gap cascading forward through the fiscal-Q4-derivation
      logic, not 8 independent gaps — recorded for `portfolio-data-mining` (no local checkout
      to file it in). (2) **WAT — corrected, fixed locally**: NOT a missing 10-Q (every quarter
      is present; "missing Q1" was a gateway period-tag labeling inconsistency across years).
      The real defect: `net_income`'s concept whitelist didn't include
      `us-gaap_NetIncomeLossAvailableToCommonStockholdersBasic`, WAT's real tag for 14/18
      `metrics-v2` filings — silently `None`, poisoning `net_margin`/ROA/ROE and, via
      `ttm_flows`, later quarters' TTM windows too. Fixed: added the concept, live-verified
      never to co-occur with the standard tags for WAT. (3) **PG/BF.B/STZ — corrected, no real
      gap**: all three have complete quarterly coverage every year; the original "×1 each" tally
      doesn't survive the precise simulation (their only fallback beyond "first 3" is an
      unavoidable fiscal-year-boundary case predating each ticker's own stored history). 2 new
      tests (377 total), mutation-checked. `docs/model_fixes.md` entry added (constitution AI
      behavior #12), including all three corrections.
      → `PLAN.md` Work item 11, `T-096`.
- [x] **T-097** *(found while validating `T-088`, 2026-09-22; **prioritized above `T-089` at the
      user's direction, 2026-09-22; done 2026-09-22**)* Guard `cycle select`
      against an out-of-order (backdated) `--analysis-date` silently mutating the live
      `portfolio_position` book — the same class of false-run hazard `T-086` closed for
      `build-returns`. **Scope corrected**: only `select`'s "positions" step ever writes
      `portfolio_position`; `monitor` never reaches it, so it was never actually at risk despite
      the original title. **Fix**: `cycle.writers.out_of_order_reason`/`OutOfOrderCycle` (mirrors
      `quant.actions.dividends_not_ready_reason`/`DividendsNotReady`, T-086) — refuses when
      `--analysis-date` is older than the live book's `MAX(valid_from)` across every row (open or
      closed); `--allow-backdated` (added only to `select`'s parser) overrides it for a
      deliberate historical run, recorded on the report. 4 new tests (380 total), mutation-
      checked. `docs/model_fixes.md` entry added (constitution AI behavior #12). → `PLAN.md` Work
      item 11, `T-097`.
- [ ] **T-089** *(**moved to the very end of the fixing process, at the user's explicit
      direction, 2026-09-22** — was next after `T-088`; now runs after `T-095`/`T-096`/`T-097`)*
      Reconcile the two architecture artifacts per constitution AI behavior #11 —
      [Portfolio Thesis](https://claude.ai/code/artifact/d3865a63-2894-4e20-b38a-7e50cf0d4040)
      and [Portfolio Financial Analysis](https://claude.ai/code/artifact/bfc6efde-aecd-4408-83b8-081bc3abccb0):
      **content only, never rename or change either `<title>`**. Cover F1, F2, F4, C1
      (diagnosis correction), C2, Q2, Q3 (superseded), `T-085` (gateway as the only
      corporate-actions source) and `T-086`/`T-092`/`T-090`/`T-088`/`T-095`/`T-096`/`T-097`
      (`T-093` is deferred, so it appears only as a
      low-priority plan step); close the gaps and plan
      steps they built and correct prose describing a fixed gap. Read the live artifact,
      republish in place by URL. Runs last, after `T-095`/`T-096`/`T-097` are done, so its
      pass covers the whole fixing process in one go. → `PLAN.md` Work item 11, `T-089`.

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
Nothing else in `T-040`–`T-084` has started. **Work item 11: `T-052`, `T-086`, `T-092`,
`T-094`, `T-087`, `T-090`, `T-088`, `T-095`, `T-096` and `T-097` are all done (2026-09-21/22);
`T-089` is next, and last** (found by `T-088`'s acceptance audit, `T-095`/`T-096`/`T-097`
promoted above `T-089` at the user's explicit direction, 2026-09-22 — `T-095` and `T-096` also
corrected their own earlier findings: PM did not reproduce T-095's defect; APO's/PG's/BF.B's/
STZ's original T-096 tally did not survive a precise reproduction, only WAT had a real,
now-fixed local gap; `T-097`'s own "select/monitor" title was corrected to `select` only).
`T-093` is on
the low-priority path.** There is no derive fallback. Execute, with Work item 5 (external, independent prerequisite) in
parallel → **Work item 7, `T-060`–`T-069`
(P0, this repo's highest priority — no external dependency for
`T-060`–`T-064`/`T-067`–`T-069`; `T-065` needs `T-040`)** → **Work item 8, `T-070`–`T-079` (P1 — `T-074` needs
`T-041`; run only after Work item 7's F1/F2/F4 fixes so the one bundled LLM
re-run scores already-corrected ratios)** → **Work item 9, `T-080`–`T-084`
(P2 — `T-082` needs `T-043`, `T-083` needs `T-042`; the production
`entity_resolution` re-graph is additionally blocked on a `urls.db`
transfer independent of any task here)**.

Work items 1 (done), 3 (superseded by `T-077` — do not implement), 6 (done
upstream + `T-052`) and 10 (done) are closed — see `CHANGELOG.md`.

Work items 2 and 4 are unaffected by the audit and keep their original,
lower priority (after Work items 5–9 above): nothing in either has
started; both are unblocked.

**Work item 12 (`T-100`, the full-universe production run) runs last of all**, after
every other task in this file — current and future.
