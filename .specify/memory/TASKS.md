# TASKS.md — `portfolio-financial-analysis`

Discrete, checkable task breakdown for `.specify/memory/PLAN.md`. Each task
references the plan work item and the `SPEC.md` section it closes. Check a
box only when its acceptance criterion (in `PLAN.md`) is actually met — not
when the code is merely written.

Task IDs are stable, same rule as `SPEC.md`'s `FR-0xx`/`NR-0xx`: don't
renumber; mark a cancelled/superseded task in place instead.

**🔴 Priority override (2026-09-08 forensic audit)**: Work items 5–9 below
(`T-040`–`T-084`) are the current top priority — a direct audit against
production data (`data/financial.db`) found live correctness bugs, not
open design work. Execute Work item 5 ∥ 6 → **7 (P0)** → **8 (P1,
supersedes Work item 3/`T-020`–`T-026`)** → Work items 2/4 (unaffected,
original priority) → **9 (P2)**. See `PLAN.md`'s "🔴 Priority Override"
section for the full rationale — the source audit markdowns
(`feedback_plan.md`, `upstream_data_mining.md`,
`upstream_portfolio_common.md`) were deleted per the auditor's instruction
after being fully incorporated into `PLAN.md`/this file, which are now the
only durable record.

## Work item 1 — Adopt `portfolio-common` v1.2.x (engine-agnostic seam) — DONE

Resolved 2026-09-05, PR #32 (`refactor/engine-agnostic`, commit `8b1fd14`,
merged as `13fcbf9`). Checked off below on re-verification 2026-09-12
(`uv run pytest` 203 passed; `ruff check`/`ruff format --check`/`mypy`
green; `grep -rn "import sqlite3" src/` clean). See `PLAN.md` Work item 1.

- [x] **T-001** Rebase/inspect `refactor/engine-agnostic` against the tip of
      `refactor/rewire-connect-call-sites` (the just-landed single
      `connect()`/`connect_ro()` factory) — confirm which parts are still
      current before resuming. → `PLAN.md` Work item 1, step 1.
- [x] **T-002** Bump `[tool.uv.sources]`'s `portfolio-common` tag from
      `v1.0.0` to the target `v1.2.x` release in `pyproject.toml`. → step 2.
- [x] **T-003** Rewrite `src/kg_schema/db.py`'s connection/row types to the
      neutral `Row`/`DatabaseError` primitives; replace
      `except sqlite3.OperationalError` sites with `except DatabaseError`
      across `kg_schema`/`quant`/`fundamental_agent`/`pricing_agent`/
      `cycle`/`api`. → step 3.
- [x] **T-004** Replace `PRAGMA table_info` with `db.table_columns`;
      replace `sqlite_master` CHECK-widening probes with
      `db.relation_exists`/`relation_ddl`; replace `executescript` with
      `db.create_schema`; replace the `PRAGMA`+`ALTER` missing-columns loop
      with `db.ensure_columns`. → step 4.
- [x] **T-005** Route the remaining SQLite-flavoured SQL (`INSERT OR
      IGNORE`, `INSERT … ON CONFLICT … DO UPDATE SET … excluded.*`,
      `json_extract`/`json_each`) through `conn.dialect.*` rather than
      inline SQL text where the new seam supports it. → step 5.
- [x] **T-006** Full suite green after each package's rewrite:
      `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`,
      `uv run mypy --config-file .code_quality/mypy.ini src tests`. → step 6
      / `PLAN.md` acceptance criteria.
- [x] **T-007** `grep -rn "import sqlite3" src/` (excluding tests) returns
      nothing. → `PLAN.md` Work item 1 first acceptance criterion.
- [x] **T-008** Update `SPEC.md` §13 item 7 to note the re-pin resolved
      (annotate in place, keep the item number) — done in `SPEC.md` itself.
      The two architecture artifacts (Portfolio Thesis + Portfolio Financial
      Analysis) were verified 2026-09-12 to already document the resolved
      state in full (a dedicated section/pill on each, per constitution AI
      behavior #11) — no further edit needed. → `PLAN.md` Work item 1, last
      acceptance criterion.

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

## Work item 3 — `quant`: a factor-aware μ estimator — SUPERSEDED, see Work item 8

**Superseded 2026-09-12** by Work item 8 step 6 (`T-077`): a Carhart
4-factor estimator with Vasicek beta shrinkage replaces the single-factor
CAPM plan below. Task IDs `T-020`–`T-026` stay unchecked as the historical
record of the superseded plan per this file's own "mark superseded in
place, don't renumber" rule — **do not implement them**; implement `T-077`
instead.

- [ ] **T-020** *(superseded, see above — do not implement)* Add a
      factor-based `ret_estimator` (`mu_i = rf + beta_i ·
      ERP`, betas from the `quant_return_daily` panel vs. a
      market/benchmark series) in `risk.py`, wired into `optimize.py`'s
      `--mu` registry alongside `equilibrium`/`james_stein`/`hist_mean`. →
      `PLAN.md` Work item 3, step 1.
- [ ] **T-021** Load enough of `risk_free_rate` and `benchmark_series` to
      compute ERP over the panel window the new estimator needs — a
      precise, dividend-accurate series is explicitly not required here
      (SPEC.md §13 item 5 stays open). → step 2.
- [ ] **T-022** Keep `equilibrium` the default `--mu`; gate the new
      estimator as opt-in until a comparative `evaluate` run exists. →
      step 3.
- [ ] **T-023** Run `optimize`/`evaluate` with the new estimator against
      `equilibrium` over the same as-of window(s); record forward realized
      return, active return, and Sharpe for both. → step 4.
- [ ] **T-024** Add a unit test for the new estimator analogous to
      `tests/test_quant_lw.py`'s covariance-estimator coverage; confirm
      `tests/test_quant_gate.py`'s score-independence guarantee still
      holds (the estimator never reads `score_snapshot`). → `PLAN.md`
      acceptance criteria, first two bullets.
- [ ] **T-025** Document the before/after comparison in `docs/quant.md`. →
      third acceptance criterion.
- [ ] **T-026** Update `SPEC.md` §13 item 1 with the dated result —
      resolved, or explicitly re-scoped if the comparison doesn't show an
      improvement. → fourth acceptance criterion.

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

## Work item 6 — Upstream: `portfolio-data-mining` corporate-actions endpoint (P0, external)

- [ ] **T-050** Implement `GET /pricing/{ticker}/actions` (or
      `actions=true` on the existing route) returning
      `{dividends, splits, source}` per the probe's expected contract,
      backed by `yfinance`'s `Ticker(t).dividends`/`.splits`; empty lists
      (never 404) when no actions exist in range. → `PLAN.md` Work item 6,
      step 1.
- [ ] **T-051** Redeploy the `:8000` gateway service. → step 3.
- [ ] **T-052** Verify: `QuantPricingClient(...).probe('XOM')` returns
      `True`; after `quant backfill-actions` (priority `corpact-v1`),
      XOM/PG/T/NEE show `cash_dividend > 0` in `quant_return_daily`. →
      `PLAN.md` acceptance criteria.

## Work item 7 — P0: production data-integrity and correctness fixes (this repo, CRITICAL, highest priority)

- [ ] **T-060** Fix **F1** — add a unit-scale sanity check in
      `src/fundamental_agent/metrics/valuation.py::_share_count` before
      computing market cap. Verify: MCD market cap ≈ $209B (not $209k), WAT
      ≈ $22B (not $37.2T); 0 filings with `|FCF yield| > 50%` (was 21). →
      `PLAN.md` Work item 7, F1.
- [ ] **T-061** Fix **F2** — correct the `revenue` XBRL-concept selection
      for REIT-classified filers in `src/fundamental_agent/statements.py`.
      Verify: `net_margin` outside `[-1,1]` and `operating_cash_flow_margin`
      outside `[-1.5,1.5]` outlier counts (124 / 54 filings) go to
      (near-)zero for the affected cohort. → F2.
- [ ] **T-062** Fix **F4** — TTM-annualize 10-Q flow numerators (trailing 4
      quarters, fallback ×4) in `metrics/profitability.py`/`efficiency.py`.
      Verify: 10-Q vs. 10-K medians for ROA/ROE/asset_turnover converge
      (were 3.79–3.86× apart). → F4.
- [ ] **T-063** Fix **C1** — correct the T-1 veto cutoff bug in
      `src/cycle/orchestrator.py:264`'s `_rank()`. Verify:
      `SELECT COUNT(*) FROM cycle_ranking WHERE vetoed != 0` is `> 0` on
      the next real cycle run following an active HARD veto (was 0/503
      always); the vetoed name is excluded from `portfolio_position`. → C1.
- [ ] **T-064** Fix **C2** — special-case `equity <= 0` in
      `src/cycle/rules/builtin.py`'s `LEVERAGE_EXTREME` rule (or gate on
      `debt_to_assets`/`interest_coverage` instead of `debt_to_equity` when
      equity is non-positive); stop `valorization.py`'s quality percentile
      from masking the leverage risk. Verify: a negative-book-equity,
      high-absolute-debt fixture triggers the veto, not a pass. → C2.
- [ ] **T-065** Implement the 7 Ring-1 `DQ_*` deterministic gates
      (`DQ_FCF_YIELD`/`DQ_MARGIN`/`DQ_MARGIN_REVIEW`/`DQ_OCF_MARGIN`/
      `DQ_MCAP_SCALE`/`DQ_NEG_EQUITY`/`DQ_REVENUE_POS`, thresholds in
      `PLAN.md` Work item 7's table) writing to `data_quality_issue` +
      driving a new `cycle` data-quality veto on HARD. **Needs T-040.** →
      Ring-1 section.
- [ ] **T-066** Fix **Q3** — derive quarterly dividends from successive
      10-Q YTD differences (`corpact-v1-derived`) in
      `src/quant/actions.py`; add engine-version priority
      (`corpact-v2` > `corpact-v1` > `corpact-v1-derived` >
      `corpact-v0-approx`) in `quant/db.py::load_actions`. Verify:
      XOM/PG/T/NEE show `cash_dividend > 0` (was $0.00 for all four). →
      Q3.
- [ ] **T-067** Fix **Q2** — align `quant evaluate`'s default `--from` to a
      date with real forward price coverage; ensure the `frontier`
      objective is exercised end-to-end. Verify:
      `quant_benchmark_performance` and `quant_frontier_point` both `> 0`
      rows (both were 0). → Q2.
- [ ] **T-068** Run the audit's **Phase A** re-sequence: recompute metrics
      (F1/F2/F4) → backfill `data_quality_issue` (T-065) + Q3 dividends
      (T-066) → re-run `cycle` (exercising T-063/T-064) → re-run the full
      `quant` pipeline + `evaluate` (exercising T-067). → `PLAN.md` Work
      item 7 Sequencing note.
- [ ] **T-069** Add regression tests for F1/F2/F4/C1/C2 under `tests/`;
      full suite (`pytest`/`ruff`/`mypy`) green. → `PLAN.md` Work item 7
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

## Status

**🔴 Current top priority (2026-09-08 forensic audit): Work items 5–9.**
Nothing in `T-040`–`T-084` has started. Execute Work item 5 ∥ 6 (external,
independent prerequisites) → **Work item 7, `T-060`–`T-069` (P0, this
repo's highest priority — no external dependency for `T-060`–`T-064`/
`T-067`–`T-069`; `T-065` needs `T-040`, `T-066`'s final target needs
`T-050`–`T-052`)** → **Work item 8, `T-070`–`T-079` (P1 — `T-074` needs
`T-041`; run only after Work item 7's F1/F2/F4 fixes so the one bundled LLM
re-run scores already-corrected ratios)** → **Work item 9, `T-080`–`T-084`
(P2 — `T-082` needs `T-043`, `T-083` needs `T-042`; the production
`entity_resolution` re-graph is additionally blocked on a `urls.db`
transfer independent of any task here)**.

Work item 3 (`T-020`–`T-026`) is **superseded** by Work item 8's `T-077` —
do not implement it.

Work item 1 (T-001–T-008) is **done** — landed via PR #32
(`refactor/engine-agnostic`), fully re-verified 2026-09-12 (quality gates,
`SPEC.md`, and both architecture artifacts all confirmed current).

Work items 2 and 4 are unaffected by the audit and keep their original,
lower priority (after Work items 5–9 above): nothing in either has
started; both are unblocked (Work item 1 already landed; Work item 4 was
always independent of it).
