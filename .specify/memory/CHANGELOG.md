# CHANGELOG.md — `portfolio-financial-analysis`

Legacy record of **closed** work items, moved here verbatim from
`.specify/memory/TASKS.md` so that file carries only open work. A work item is
closed once every task in it is checked, or explicitly superseded/moved elsewhere.
Task IDs are stable and never reused; `PLAN.md` keeps each work item's plan and
acceptance criteria. Ordered by work item number.

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

## Work item 6 — Upstream: `portfolio-data-mining` corporate-actions endpoint (P0, external) — implementation MOVED, verification stays — CLOSED 2026-09-21

**Closed (re-verified 2026-09-24)**: the implementation shipped upstream
(`portfolio-data-mining` Work item 3, PR #36, `T-026` checked off there) and
this repo's consumer-side check `T-052` passed live on 2026-09-21; `T-085`
(Work item 10) then made the gateway `quant`'s only corporate-actions source.
Production `KG_FINANCIAL_DB` today satisfies every acceptance criterion:
7,481 `corpact-v1` rows over 426 assets, and no other `corporate_action`
engine version left (the XBRL-derived `corpact-v0-approx`/`corpact-v1-derived`
engines are removed from `src/quant/actions.py`); XOM/PG/T/NEE each have 19
`corpact-v1` dividends; `quant_return_daily.cash_dividend` sums are XOM 18.16,
PG 18.72, T 5.524, NEE 9.146 (all were `$0.00` when this work item opened).
`T-050`/`T-051` stay unchecked only as the "moved upstream" historical record.

**Moved 2026-09-19**: the yfinance-backed endpoint is data acquisition, not
analysis, so its implementation is tracked in `portfolio-data-mining`
(`.specify/memory/PLAN.md` Work item 3, `T-020`–`T-027`; PR
https://github.com/gamug/portfolio-data-mining/pull/30). `T-050`/`T-051`
stay unchecked here as the historical record, per this file's own "mark
superseded in place" rule — do not implement them in this repo. `T-052`
stays open: it is this repo's consumer-side verification and is now
**blocked on that upstream work landing and being redeployed**.

- [ ] **T-050** *(moved to `portfolio-data-mining` `T-020`–`T-022` — do not
      implement here)* Implement `GET /pricing/{ticker}/actions` (or
      `actions=true` on the existing route) returning
      `{dividends, splits, source}` per the probe's expected contract,
      backed by `yfinance`'s `Ticker(t).dividends`/`.splits`; empty lists
      (never 404) when no actions exist in range. → `PLAN.md` Work item 6,
      step 1.
- [ ] **T-051** *(moved to `portfolio-data-mining`, redeploy is its
      `T-026` handoff — do not implement here)* Redeploy the `:8000`
      gateway service. → step 3.
- [x] **T-052** *(was blocked on `portfolio-data-mining` `T-020`–`T-026`;
      the consumer code it exercises is `T-085` — now the **only** source)*
      Verify: `QuantPricingClient(...).probe('XOM')` returns
      `True`; after `quant backfill-actions` (priority `corpact-v1`),
      XOM/PG/T/NEE show `cash_dividend > 0` in `quant_return_daily`. Run
      it against the *deployed* `PRICING_BASE_URL` (the gateway's `/pricing`
      mount), not a local copy of the service — upstream's `T-026`
      verified the client against the latter only. →
      `PLAN.md` acceptance criteria. **Done 2026-09-21**, live against the
      deployed gateway (`http://host.docker.internal:8000/pricing`; the route is
      `/pricing/pricing/{ticker}/actions`, the single-segment path is a 404).
      `quant backfill-actions --analysis-date 2026-09-21` (run from the `T-085`
      branch, `quant_run` 6, `completed`, exit 0, backup taken first): **503 of
      503 assets fetched, 0 errored, 7,412 dividends + 69 splits = 7,481
      `corpact-v1` rows**; the only tables that changed versus the backup are
      `corporate_action` (+7,481) and `quant_run` (+1). XOM, PG, T and NEE each
      have 19 gateway dividends (all had $0 before); BF.B has 19 through the
      dotted-ticker spelling (`BF-B`); NVDA's 10-for-1 split (2024-06-10) is
      present. On a **scratch copy** of the DB, `build-returns` wrote 580,343
      `qret-v2` rows with 409 assets carrying dividends and
      `SUM(cash_dividend) > 0` for XOM (18.16), PG (18.72), T (5.524), NEE (9.146);
      production `build-returns` was deliberately not run (`T-086` not built,
      `T-088` will rebuild). Independent cross-check of the 77 assets with no
      gateway rows: only GNRC, BSX and APTV had filing-derived dividends, all
      phantom values ($0.001–$0.058) from the removed derivation — none of
      them pays a dividend — while the gateway found dividends for 111 assets
      the derivation had missed; BRK.B (dotted) is a genuine non-payer, and
      HOOD and WAT are non-payers. Notes: ex-dates after the last price bar
      (2026-08-27) — NEE's 2026-08-28, BF.B's 2026-09-03 — cannot fold into
      the return series until prices are refreshed; T shows a 2022-04-11
      `SPLIT` of 1.324 (yfinance's encoding of the WarnerMedia spin-off) —
      splits are recorded for provenance only and never re-applied.

## Work item 7 — P0: production data-integrity and correctness fixes (this repo, CRITICAL, highest priority) — DONE 2026-09-25

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
- [x] **T-065** Implement the 7 Ring-1 `DQ_*` deterministic gates
      (`DQ_FCF_YIELD`/`DQ_MARGIN`/`DQ_MARGIN_REVIEW`/`DQ_OCF_MARGIN`/
      `DQ_MCAP_SCALE`/`DQ_NEG_EQUITY`/`DQ_REVENUE_POS`, thresholds in
      `PLAN.md` Work item 7's table) writing to `data_quality_issue` +
      driving a new `cycle` data-quality veto on HARD. **Needs T-040.** →
      Ring-1 section. **Done 2026-09-25** (with `T-040`): `fundamental_agent/quality.py`
      gates each filing's stored metrics right after they are recorded, and
      `python -m fundamental_agent quality` backfills (LLM-free); `cycle` reads the
      verdicts on each asset's latest filing — quarantined metrics read as NULL in every
      score and rule, a HARD verdict raises the new `DATA_QUALITY` veto (T-1 lag), a
      negative-equity name keeps C2's worst-leverage rank. Verified on a copy of
      production (377 `metrics-v2` filings, 264 issue rows): `DQ_FCF_YIELD` 7 +
      `DQ_MCAP_SCALE` 7 (MCD FY2023–2025Q2, share counts in millions), `DQ_NEG_EQUITY`
      41 (19 HARD, SBAC), `DQ_REVENUE_POS` 19 (NEE, revenue unresolved),
      `DQ_MARGIN_REVIEW` 6, `DQ_OCF_MARGIN` 1, `DQ_MARGIN` 0; re-run inserts 0. A
      `cycle monitor` on the copy vetoes NEE (new) and SBAC (was `LEVERAGE_EXTREME`,
      same thresholds). Production's own backfill is `T-100`'s (per `T-068`); the two
      defects found are `T-102`/`T-103`. Full record in `docs/model_fixes.md`'s T-065
      entry.
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

## Work item 10 — P0: the `portfolio-data-mining` corporate-actions gateway as `quant`'s only source (this repo) — DONE

Added 2026-09-20, after PR #45 moved the endpoint's *implementation* to
`portfolio-data-mining` (`T-050`/`T-051`) and left the *consumption* here
un-tasked. **Scope, corrected the same day**: the gateway is the *only* source —
not the primary one with a derived fallback, which was the first draft. Only
`portfolio-data-mining` mines data; no other repository hosts that kind of
service, so `quant`'s XBRL-derived dividend engines are removed. No external
dependency for the code; live verification is `T-052`.

- [x] **T-085** Make the `portfolio-data-mining` pricing gateway `quant`'s
      **only** corporate-actions source, and remove the derivation:
      (1) delete `derive_corporate_actions_from_facts`,
      `derive_quarterly_dividends_from_10q_ytd` and their helpers/constants
      from `src/quant/actions.py`, the `--source` flag, and the report's
      per-source fields — `quant.actions` no longer reads `financial_facts`;
      `load_actions` reads only the gateway engines (`corpact-v2` >
      `corpact-v1`), and rows the retired `corpact-v0-approx` /
      `corpact-v1-derived` engines wrote stay as unread history;
      (2) **fail fast, never degrade**: a failed probe, or the circuit breaker
      opening after `K` consecutive gateway *errors*
      (`QuantSettings.gateway_max_consecutive_failures`, default 3), raises
      `GatewayUnavailable` — `quant_run.status = 'failed'`, CLI exit 1, rows
      already written kept; (3) an asset the gateway cannot serve
      (`ActionsUnavailable` — a non-null `warning`, which upstream returns
      with empty lists when yfinance fails —, `ActionsNotSupported`, or one
      isolated error) gets **no rows** and is listed in the report; the run
      completes and the CLI exits 1 because the data is incomplete; (4)
      `QuantPricingClient`: `GatewayError` (not a bare
      `HTTPStatusError`/`JSONDecodeError`) for an error status or unusable
      body, `probe()` `False` while yfinance is down, and dotted share classes
      requested in yfinance's spelling (`BF.B` → `BF-B`); (5) per-run counts in
      the summary and `quant_run.params_json`; (6) a hermetic
      `httpx.MockTransport` contract test, plus a test pinning that
      `quant.actions` neither reads `financial_facts` nor exposes a derive
      function; (7) update `docs/quant.md`, `README.md`, `SPEC.md` and
      `docs/model_fixes.md`'s Q3 entry (superseded), and correct
      `T-066`/`T-068`/`T-052`. **Live** verification is `T-052`, not this box.
      → `PLAN.md` Work item 10. **Done 2026-09-20**: only gateway *errors*
      count toward the breaker — a per-ticker warning means the gateway
      answered, so it resets the streak, and a global yfinance outage lists
      every asset without tripping it. 27 hermetic tests
      (`tests/test_quant_pricing_client.py`, `tests/test_quant_actions.py`);
      the fixtures that seeded dividends through the derive path now seed
      `corpact-v1` rows directly; full suite (263), ruff, mypy green.
      Mutation-checked: a probe or breaker that no longer fails the run, an
      exit code that ignores errored assets, `load_actions` reading the
      retired engines, ignoring the warning, dropping the dotted-ticker
      spelling, and a breaker that never opens or never resets each fail
      their own test. **Consequences** (deliberate, flagged): with no
      derive path, `quant` had **no dividends until `T-052`** passed *(it did,
      2026-09-21: 7,481 gateway rows in production)*, and
      `build-returns` must follow a successful `backfill-actions` (a
      dividend-less series locks in under `qret-v2`); yfinance is a single,
      unofficial source that cannot tell an unknown symbol from a name that
      paid nothing, so such a symbol stays at `$0` with no error until
      `T-052`'s live check (`BF.B` is the one dotted symbol in the current
      set); `v_corporate_action` still resolves legacy derived rows for the
      knowledge-graph repo (a separate decision); and the constitution's
      "Executable cmds" line still lists `--source derive|gateway` — that
      needs its own governed change (PATCH), not made here.

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
- [x] **T-093** *(**DEFERRED 2026-09-21 — low-priority path**: tackled late, after Work
      item 9, not in the current run of work; nothing in Work items 7–11 depends on it)*
      *(feature; builds on `T-090`; **trimmed 2026-09-25, at the user's direction, to only
      what `T-090` doesn't already deliver** — `T-090` already gives: latest-by-default, an
      exact version for every metric group or per `GROUP=VERSION`, a strict error listing the
      stored versions, the version-ordering rule, and the run manifest)* Extend `quant`'s
      version selection with: (1) **constraint operators** in `--metrics-version` — minimum
      (`>=metrics-v2`), exclusion (`!=metrics-v1`) and comma-separated combinations, resolving
      to the highest stored version that satisfies; a bare version and `GROUP=VERSION` keep
      their current exact meaning (backward compatible); on no match the error also says why
      each stored candidate was rejected; (2) **selection for the non-metric inputs** — the
      `corpact`, return and risk-model engines are fixed config values today with no check
      that they are stored: add flags accepting the same grammar (default latest stored) and
      resolve them through the same strict resolver; (3) **`--version-profile FILE`** — a TOML
      file of named, reusable constraint sets, flags winning over the file; (4) **tuning
      aids** — `quant versions` (per input: versions present, row counts, first/last
      `computed_at`) and `--dry-run` on `build-risk-model`/`optimize` (prints the resolved
      manifest, writes nothing); (5) the **constraints as given and the profile name** recorded
      in the `T-090` manifest alongside the resolved versions. `quant` only (`cycle` keeps
      `T-090`'s exact selection). Additive only. Acceptance in `PLAN.md`. → `PLAN.md` Work
      item 11, `T-093`.
      **Done 2026-09-25** (developed at the user's request, ahead of its low-priority slot).
      `kg_schema/versions.py`: `parse_constraint`/`pick_version` (`=`/bare, `>=`, `!=`,
      combinations, `latest`; highest satisfying stored version; per-candidate rejection
      reasons), `parse_metric_constraints`/`choose_constrained_versions` (per-group; every T-090
      form delegated unchanged — pinned against T-090's resolver on 80 input combinations),
      `engine_version_key`/`recognized_engine_versions`. `quant`: `--returns-version`
      (build-risk-model/optimize), `--risk-model-version` (optimize; exclusive with
      `--model-version`), `--corpact-version` (build-returns), `--version-profile FILE
      [--profile NAME]` (`quant/profiles.py`, stdlib `tomllib`, flags win), `quant versions`
      (`quant/versions_report.py`), `--dry-run` (read-only connection, writes nothing); the
      constraints and profile are recorded in the manifest JSON but **not** in the tag. **Decision
      for review (deviates from the text above):** with no flag each input keeps today's behaviour
      — the configured return engine, the per-asset corporate-action priority, the `rm-v1`
      label — rather than "latest stored", so no existing run or tag shifts; `latest` asks for the
      newest stored explicitly. A book from a non-default risk model folds it into a separate
      `book_tag` (books from two risk models would otherwise collide); the default keeps T-090's
      key. The metrics listing lives in `kg_schema.queries.metric_version_stats` (the allow-listed
      place for cross-version reads). 29 new test functions (`tests/test_quant_version_constraints.py`,
      121 cases with parametrization, + 1 strict xfail for `T-101`), mutation-checked four ways;
      suite, ruff, mypy green. Docs: `docs/quant.md`, `docs/kg_schema.md`.
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
- [x] **T-089** *(**moved to the very end of the fixing process, at the user's explicit
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
      **Done 2026-09-25**, content only, both titles unchanged. **Portfolio Financial Analysis**
      (v10): a new "What the audit found, and what changed" ledger (F1, F2, F4 + `T-094`, C1's
      corrected diagnosis, C2, Q2, Q3 superseded, `T-085`, `T-086`, `T-092`, `T-090`, `T-088` +
      `T-068`, `T-095`, `T-096`, `T-097`, plus the 2026-09-25 plan decisions on `T-078`/`T-093`/
      `T-100`); stale prose fixed: `kg_schema` described as vendored (it still said "external,
      `portfolio-common` v0.2.0"), the pricing gateway as serving corporate actions to `quant`,
      the "FY-derived dividends" gap replaced by the 20-of-503 coverage gap, and the plan rewritten
      to the current order with `T-100` last. **Portfolio Thesis** (v26): financial-analysis's
      band tooltips, band caption and status row, and data-mining's row (its "next for
      financial-analysis" step is done). `T-093` appears only as a low-priority plan step.

- [x] **T-101** *(found by `T-093`'s tests, 2026-09-25; **done 2026-09-25**)* Re-running `quant
      optimize` over the same inputs **duplicates books** instead of refreshing them.
      `quant_portfolio` is unique on `(as_of, kind, frontier_k, engine_version)`, but
      `frontier_k` is NULL for every non-frontier book and SQLite treats NULLs as distinct, so
      `insert_portfolio`'s `ON CONFLICT … DO UPDATE` never fires (verified on `master`: one run
      → 2 books, a second identical run → 4). Contradicts `T-090`'s "the same inputs update in
      place" for books (risk models are fine — no NULL in their key). **Fix** (not built): make
      the key NULL-safe (e.g. a `COALESCE(frontier_k, -1)` unique index via a gated migration, or
      an update-then-insert keyed with `frontier_k IS ?`), decide what to do with the duplicates
      already stored, and flip the strict `xfail` in `tests/test_quant_version_constraints.py`.
      → `PLAN.md` Work item 11, `T-101`.
      **Done 2026-09-25.** A second defect sat behind the first: the read-back after the insert
      returned the *oldest* duplicate, so `sync_positions` kept writing the latest weights into
      it while each newer copy had none. `quant.db.insert_portfolio` is now an update-or-insert
      keyed on `frontier_k IS ?` (oldest id wins if duplicates pre-exist) returning the right id;
      it covers `evaluate`'s `live_book` snapshot too. Migration **m007** merges existing
      duplicates (keep the oldest id with the positions, refresh it with the newest copy's
      metadata, re-point `quant_frontier_point`, drop the newer copies' positions and derived
      performance rows) and adds the NULL-safe unique index `ux_quant_portfolio_book` on
      `IFNULL(frontier_k, -1)`. Production held **no** duplicates (4 distinct books); `migrate`
      verified on a scratch copy of it (v7, index present, 4 books unchanged, all 31 views query,
      positions/performance intact). **Production migrated 2026-09-25** (after merge, per the
      `docs/kg_schema.md` runbook: no writers, backup
      `financial.db.pre-t101-migrate-backup-20260925` byte-identical + `quick_check` ok, `migrate`
      applied v7 only): `quick_check` ok, index present, the 4 books and every row count
      unchanged, all 31 views query, `optimize --dry-run` still resolves `9d34ff69`. 10 new tests
      (`tests/test_quant_book_key.py`), `T-093`'s strict `xfail` flipped to a pass, mutation-checked
      five ways; 512 passed, ruff, mypy green. Docs: `docs/quant.md`, `docs/kg_schema.md`.

## Work item 13 — P0: data defects surfaced by the Ring-1 gates — DONE 2026-09-25

Added 2026-09-25 by `T-065`: its verification on a copy of production found two live data
defects the gates now quarantine and veto, but do not fix. Each is a methodology change
(constitution AI behavior #12: verified, cited, recorded in `docs/model_fixes.md`). →
`PLAN.md` Work item 13.

- [x] **T-102** Resolve NEE's revenue: its income statement reports
      `us-gaap_RegulatedAndUnregulatedOperatingRevenue` ("OPERATING REVENUES", a utility
      tag), which `statements.REGISTRY["revenue"]` does not list, so revenue is NULL in all
      19 NEE filings and every revenue-denominated ratio with it; `DQ_REVENUE_POS` HARD-vetoes
      NEE until this lands. Decide how the tag joins the registry (total vs. component,
      against `T-095`'s plausibility floor), check other utilities for it. **Acceptance**:
      NEE's revenue resolves to the reported operating revenues; a `quality` re-gate of the
      new metrics version clears `DQ_REVENUE_POS` for NEE. **Done 2026-09-25**: the tag is
      the taxonomy's *total* operating revenue (parent of `Regulated-`/
      `UnregulatedOperatingRevenue`), so it joined `total_concepts` (still behind `T-095`'s
      floor). A survey of all 31 as-of S&P 500 utilities' latest 10-K via the gateway: 6
      resolved NULL — AWK, DTE, DUK, NEE, SRE, XEL, every one tagging its total only this
      way (the components sum to it exactly where reported) — and all 6 now resolve (net
      margins 9–22%); the other 25 are unchanged. NEE's 10-Qs resolve too (8 checked,
      2022–2026). The engine is bumped to **`metrics-v3`**. Gate check: the metrics computed
      from NEE's real FY2025 10-K raise no `DQ_REVENUE_POS`
      (`tests/test_data_quality.py`). Production still holds only `metrics-v2`; its `v3`
      rows come from `T-100`'s full recompute — until then, pin `--metrics-version
      metrics-v2` if anything writes `v3` rows for only part of the universe. Full record in
      `docs/model_fixes.md`'s T-102 entry.
- [x] **T-103** Fix F1's residual on MCD FY2023–2025Q2: seven consecutive filings store
      `shares` in millions (`732.3` … `717.6`), so market cap is ~10⁻⁶ of the real value
      (`DQ_MCAP_SCALE` + `DQ_FCF_YIELD`). F1's overlapping-history anchor is itself
      mis-scaled inside such a run, and its EPS corroboration only covers
      `diluted_shares`. **Acceptance**: those filings' market cap within the gate's range
      under a new metrics version, F1's existing tests unchanged, and a `quality` re-gate
      clears them. *(Version: `T-102` bumped to `metrics-v3`; this lands under `metrics-v3`
      too if no `v3` rows have been persisted in production by then, else it bumps again.)*
      **Done 2026-09-25**, under `metrics-v3` (production still had no `v3` row). The
      guessed mechanism above was wrong: reproduced on FY2023's real payload, the EPS signal
      *did* say ×10⁶, but `overlapping_history` also read **later** filings (a look-ahead),
      whose mis-scaled restatement of the same period read as first-hand proof it was
      clean and vetoed EPS. Fix: history is point in time (only filings filed before), the
      EPS numerator is net income available to common (ASC 260-10-45-11), EPS is used only
      in `[$0.10, $10,000]`, and `shares_outstanding` is checked against an EPS-confirmed
      diluted count of the same filing (±25% of a power of ten). Old vs. new over all 5,076
      production filings: 14 → 24 corrections — gained MCD ×7, DLR FY2022, ECHO 2024Q3
      (true) and AEP 2022Q3/2023Q3/2024Q3 (a mismatched 51.9M snapped within 1% of the
      diluted count); dropped AEP FY2021/FY2022's false ×0.1; ALL/MCHP/HAL/RTX FY2022/NVR
      false signals exposed by the point-in-time change are all guarded. With ×10⁶, MCD's
      seven filings (market cap $184–224B, 3.4–4.0× assets) clear `DQ_MCAP_SCALE` and
      `DQ_FCF_YIELD`. F1's tests unchanged; +10 tests. Full record in `docs/model_fixes.md`'s
      T-103 entry; production re-persist is `T-100`'s.
