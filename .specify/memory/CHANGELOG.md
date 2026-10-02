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

## Work item 5 — Upstream: `portfolio-common` v0.3.0 additive data contract (P0, external) — DONE 2026-09-25 (built in this repo; `T-044` deprecated)

*(Note 2026-09-25: `kg_schema` is vendored in this repo (`src/kg_schema/`, see
`docs/kg_schema.md`), so these additive changes land **here**, not in `portfolio-common` —
`T-040` was built that way, and `T-044` (the upstream release-and-bump) is deprecated.)*

- [x] **T-040** Add `data_quality_issue` table (`filing_id`/`asset_id` FKs,
      `metric_name`, `rule_id`, `severity CHECK IN ('HARD','SOFT')`,
      `value`, `created_at`, `run_id`, `UNIQUE(filing_id, metric_name,
      rule_id)` + indexes) in `portfolio_common/kg_schema/ddl.py`. →
      `PLAN.md` Work item 5, step 1. **Done 2026-09-25, inside `T-065`** — in this
      repo's vendored `src/kg_schema/ddl.py` (`kg_schema` left `portfolio-common` at its
      v1.0.0, so there is no upstream release to wait for), with the read-contract view
      `v_data_quality_issue`. Key widened to `UNIQUE(filing_id, metric_group,
      metric_name, metric_engine_version, rule_id, gate_version)` plus a `quarantined`
      flag and `evidence_json`, so a verdict names the T-090 metric version it judged
      and a future threshold change writes parallel rows — rationale in
      `docs/model_fixes.md`'s T-065 entry.
- [x] **T-041** Add nullable `score_snapshot.forensic_flags_json` column
      via the existing missing-columns mechanism. → step 2. **Done 2026-09-25**: a
      `REQUIRED_COLUMNS` entry in `src/kg_schema/ddl.py` (this repo's vendored `kg_schema`),
      so every package's next `ensure()` adds it — additive, no `schema_version` bump, the
      `UNIQUE(asset_id, score_type, event_time)` key untouched. m005/m006 rebuild
      `score_snapshot` from an explicit column list, so both now carry the column (and add it
      first when called directly) — flags written on a not-yet-migrated database survive.
      Verified on a copy of production: column added, 497 rows kept, `schema_version` 7,
      all views query, `quick_check` ok, second `ensure()` a no-op. Not exposed in
      `v_score_snapshot` yet — that and writing it are `T-074`'s.
- [x] **T-042** Rewrite `v_quant_vs_live` as the existing benchmark-side
      `LEFT JOIN` `UNION`ed with a `kind='LIVE_ONLY'` branch for live
      positions absent from every quant benchmark. → step 3. **Done 2026-09-25** in the
      vendored `src/kg_schema/views.py` (views rebuild on every `ensure()`; no migration).
      The benchmark branch is byte-for-byte the old view; `UNION ALL` (the branches are
      disjoint by `kind`) adds, per as-of date that has optimized books, one row per live
      position open that date and held in none of them (reference books `live_book`/
      `equal_weight`/`cap_weight` don't count): `benchmark_weight` NULL, `active_weight =
      -live_weight`. On a copy of production: APA (2026-09-22, live weight 0.10) — the one
      live name the old view dropped — now appears as `LIVE_ONLY`; all 10 live names show,
      their weights sum to 1.0; the 38 benchmark rows are identical. 4 tests in
      `tests/test_quant_vs_live.py`, mutation-checked.
- [x] **T-043** Add `media_cooccurrence` table (same shape/grain as
      `shared_executive_edge`, different table). → step 4. **Done 2026-09-25** in the
      vendored `src/kg_schema/ddl.py`: identical columns, key
      `UNIQUE(asset_id_a, asset_id_b, person_name, method)` and the two per-asset indexes
      (`ix_media_cooc_a`/`_b`); additive, no `schema_version` bump. A test pins its columns
      to `shared_executive_edge`'s so the two cannot drift. On a copy of production: created
      empty, the 14,172 executive edges untouched, `schema_version` 7, `quick_check` ok.
      No writer or view yet — `T-082` writes it.
- [ ] **T-044** *(**DEPRECATED 2026-09-25, at the user's direction — do not implement.**
      Kept unchecked as the historical record, per this file's "mark cancelled in place,
      don't renumber" rule. Reason: `kg_schema` left `portfolio-common` at its v1.0.0 and
      is vendored here (`src/kg_schema/`), so `T-040`–`T-043` land in this repo and there
      is nothing to release upstream or re-pin; `portfolio-common` stays at `v1.2.1`.
      Each of `T-041`–`T-043` checks `ensure()` in its own tests, as `T-040` did.)*
      Tag and release `portfolio-common` `v0.3.0`; bump this
      repo's `pyproject.toml` (`[tool.uv.sources]`) from `v1.2.1` →
      `v0.3.0`, regenerate `uv.lock`, `uv sync`, and re-verify `ensure()`
      against the live `KG_FINANCIAL_DB`. → `PLAN.md` acceptance criteria.

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
- [x] **T-097** *(**Correction 2026-09-25, `T-104`**: the incident below was **not** fully reverted —
      the live book kept the backdated run's weights, its BF.B stint and an inverted WFC stint
      until `cycle undo-run` (T-104) repaired it on 2026-09-25. Record otherwise kept verbatim.)*
      *(found while validating `T-088`, 2026-09-22; **prioritized above `T-089` at the
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

## Work item 14 — P0/P1: second forensic audit (`feedback_plan 1.md`) — DONE 2026-09-30

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
- [x] **T-107** *(P0 — **decided 2026-09-25: option (b)**, PR #78 review; after `T-120`)* — **DONE 2026-09-26**
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
      **Code done 2026-09-26 (PR #82); production `migrate` (m008) applied 2026-09-26.**
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
      **Production, 2026-09-26** (at the user's direction; 4 min 15 s): backup
      `financial.db.pre-t107-migrate-backup-20260926` (md5 `6a3bb9ffc697532cfbe60e9a98753c0d`,
      `quick_check` ok, schema v7). After: schema v8; 5,076 filings, 11,878 metrics and 377
      FUNDAMENTAL scores carry `available_at`, 0 NULL, 0 differing from their filing, none on
      or before its filing date; gaps 1 day ×3,912, 2 ×18, 3 ×1,002, 4 ×144; all 5,057 values up
      to the price spine's end are spine sessions; the 7 guards in place; `v_score_snapshot` /
      `v_sec_filing` expose the column; facts (1,206,001), scores (497), sections (11,526)
      unchanged; `quick_check` ok, FK clean; `availability.missing` empty, so `cycle` and
      `quant` run. The live 2026-09-22 cycle reads the same 20 scores and 16 market caps as
      before.
- [x] **T-108** *(P0)* Fix the internal benchmark (`quant/benchmark.py`). It compounds the
      cross-sectional **mean of log returns**, not `ln(1 + mean simple return)`, so it is lower
      every day by about half the cross-sectional variance: on today's 20-asset panel it
      compounds at 5.98%/yr against a true equal weight of 10.32% (−4.34 pp/yr; the audit
      measured −6.98 pp/yr on the full universe). It also averages every name that has a row
      rather than the gated panel. Every `active_return` in `quant_benchmark_performance`
      inherits the bias. New `bench-v2`; a loader for an external cap-weighted total-return
      series (the series itself is a data-acquisition step). **Acceptance**: the recomputed
      index matches an independent equal-weight calculation to 1e-9.
      **Code done 2026-09-26 (PR #84); production re-evaluation pending approval.**
      `bench-v2`: the mean of simple returns over the investable universe as of the window's
      start (`universe.benchmark_gate` -- `build-risk-model`'s `settings_gate` knobs, but never
      a book's own hard-veto exclusion, so a veto can't also shrink the yardstick a book is
      graded against; a review caught the panel still using `settings_gate` pre-merge),
      compounded `(1 + r)`. `evaluate` grades against the version it built, writes `perf-v2`,
      records version and panel on its run, and only reads an external series
      (`quant load-benchmark`, `csv-v1`). `v_quant_benchmark_performance` shows the latest
      version per (book, date). Tests `tests/test_benchmark.py` (23) and
      `tests/test_quant_gate.py` (3); 9 mutations caught. The pre-correction dry run's
      "7.43 % vs 6.63 %, summed daily active return −6.16 % → −6.91 %" is withdrawn: its
      "live book" was a stale T-104 snapshot (`quant_portfolio` id 4), and active return
      should be compounded, not summed daily (review, 2026-09-26). Void that snapshot and its
      41 perf rows, then re-run the dry run against a current production copy with the
      corrected panel, before any production `quant evaluate`. Record: `docs/model_fixes.md`
      "T-108".
- [x] **T-109** *(P0)* One expected-return convention across `quant`. `risk.equilibrium_returns`
      computes `rf + λΣw`, but `persist.py` never passes `rf`, so the stored `equilibrium` μ
      (the default, used by production's risk model) is an *excess* return, while `hist_mean`
      and `james_stein` are total returns — and `max_sharpe`/tangency and every Sharpe subtract
      `rf` again. Pick one convention (total), apply it to every estimator and to `T-077`'s
      Carhart projection. **Acceptance**: a test pins μ as total for all three estimators;
      tangency/Sharpe subtract `rf` exactly once.
      **Code done 2026-09-27.** Convention: total return, matching `hist_mean`/`james_stein`
      (already means of the panel's total-return series) rather than making those excess.
      `persist.py::_expected_returns` now threads the `rf` it already loads (for
      `quant_risk_model.rf_annual`) into `equilibrium_returns(..., rf=rf)`; no change to
      `equilibrium_returns` itself (its docstring already gave the total-return formula) or to
      `optimize.py::_stats` (already subtracts `rf` exactly once, given a total-return μ) —
      only the wiring between them was wrong. `T-077`'s Carhart estimator (not yet
      implemented) already plans `mu_i = rf + Σ_k β_i,k^shrunk · λ̄_k`, consistent with this
      fix. Tests `tests/test_quant_risk_model.py` (+2): building the same risk model at two
      `risk_free_rate`s 0.05 apart, `hist_mean`/`james_stein` are unchanged while `equilibrium`
      shifts by exactly 0.05 uniformly; a `min_var` book's `expected_return` shifts by the same
      0.05 while its `sharpe` stays invariant (rf-independent once μ is genuinely total) — both
      confirmed to fail on the pre-fix code with the bug's exact signature (`expected_return`
      flat instead of shifting). Full suite 692 passed (was 690); ruff, format, mypy clean.
      Record: `docs/model_fixes.md` "T-109". **Every risk model built before this fix carries
      an excess-return `equilibrium` μ; re-persisting corrected values for the live universe
      needs a production `build-risk-model`/`optimize` re-run — pending, same as F1/F2/F4's own
      deferred production re-runs.**
- [x] **T-110** *(P1)* Validate as-of dates against the price spine. Production `quant_run`s
      7–10 and both `cycle_run`s are dated 2026-09-21/22 while `price_daily` ends 2026-08-27;
      `price_observation` has 503 rows at 2026-08-28 (no `price_daily` bar that day) and all
      503 `price_window` "full" rows of one run end 2026-08-28 — a run with `--observations`
      but without `--store-daily` wrote analytics for a bar it never stored. Warn (or refuse
      with an override) when a `quant`/`cycle` as-of is past the last stored price; make
      `pricing_agent` never write observations or windows past `price_daily`'s last date;
      clean the orphan rows. **Acceptance**: zero orphan observation dates; a run past the
      price cutoff is recorded as such.
      **Code done 2026-09-27.** Shared `kg_schema.queries.stale_as_of_reason`/`last_price_date`
      (`MAX(date) FROM price_daily`, tolerant of a missing table). `quant`'s
      `build-risk-model`/`optimize` and `cycle`'s `select`/`monitor`/`backfill` all refuse a
      stale as-of via the new `StaleAsOf`, unless `--allow-stale-prices`, which records the
      bypass reason on the run (`params_json`, `RiskModelResult`/`OptimizeRunResult`/
      `CycleReport`) for the CLI's `WARNING`. `pricing_agent`'s `--observations` now requires
      `--store-daily` (refused at the CLI and again in `pipeline.run` itself), closing the
      orphan-observation-date root cause going forward — `price_window` itself is left
      ungated, since it's documented as pricing_agent's standalone base product, and the
      acceptance criterion only asked for zero orphan *observation* dates. `evaluate`/
      `benchmark` are deliberately not guarded (already degrade gracefully on missing forward
      data, FR-010). Tests: `tests/test_kg_schema.py` (+2), `tests/test_quant_risk_model.py`
      (+5), `tests/test_cycle.py` (+4), `tests/test_pricing_pipeline.py` (+3). Full suite 706
      passed (was 692); ruff, format, mypy, pre-commit clean. Record: `docs/model_fixes.md`
      "T-110".
      **PR #87 review (`@eldova1702`) found `optimize`'s model-reuse path skipped the check
      entirely** (a stale model built once under `--allow-stale-prices` let a later `optimize`
      at that same stale as-of reuse it unchecked, with no `stale_as_of_bypassed` recorded on
      that `optimize` run) — fixed 2026-09-27 by having `run_optimize` compute and gate on
      `stale_as_of_reason` itself, unconditionally, before resolving the model either way;
      regression test added (`test_optimize_re_checks_staleness_even_when_reusing_a_stored_model`).
      Same review also corrected the T-109 Black-Litterman citation in `docs/model_fixes.md`
      (Π is the implied excess return; `quant` stores the total return `rf + Π`) and the
      matching `risk.equilibrium_returns` docstring. Full suite 707 passed after the fix.
      **Merged via PR #87.** Cleaning the production orphan rows themselves is a separate,
      deferred production action — see `T-123`.
- [x] **T-111** *(P1)* `quant evaluate`: a missing asset-day counts as a 0% return
      (`fwd[d].get(a, 0.0)`), dragging the book toward zero in proportion to missing names.
      Renormalize the day's weights over names that have a return. (Transaction costs belong
      to `T-077`.) **Acceptance**: a book with one name missing a day earns the other names'
      renormalized return.
      **Done 2026-09-27; corrected 2026-09-27 (PR #89 review, `@eldova1702`).** The first cut
      renormalized *every* missing asset-day, which review found double counts a genuine
      one-day `price_daily` gap: the return engine bridges the gap by computing the next
      available day's return from the last available close, so that next return already
      contains the gap day's move -- renormalizing the gap day imputes an extra return on top.
      Corrected: `_evaluate_book` (and `benchmark.build_internal_benchmark`, for the same
      convention) now tracks each name's `last_seen` date in the window; a name missing *today*
      but with a later return stays a "survivor" contributing 0% today, weight kept (its move
      lands, once, on the day it reappears); only a name with no later return at all (delisted,
      series ends) is dropped and the remaining weights renormalized, from that day on. Tests:
      `tests/test_quant_pipeline.py::test_evaluate_matches_the_no_gap_result_across_a_one_day_price_data_gap`,
      `tests/test_quant_pipeline.py::test_evaluate_renormalizes_from_a_names_permanent_end_of_data`,
      and the matching pair in `tests/test_benchmark.py`. Full suite 710 passed (was 707); ruff,
      format, mypy, pre-commit clean. `SPEC.md` FR-010 and `docs/quant.md` updated. Record:
      `docs/model_fixes.md` "T-111".
- [x] **T-112** *(P1)* `optimize.efficient_frontier` returns *k* identical copies of the
      min-variance point, all labelled `optimal`, when the feasible return range collapses.
      Return that one point with an explicit `degenerate` status. **Acceptance**: a collapsed
      frontier yields one point marked degenerate.
      **Done 2026-09-27.** The collapse path (`r_max is None or r_max <= r_min + 1e-9`) now
      returns a single `FrontierPoint(0, r_min, r_min, lo.expected_vol, lo.sharpe, "degenerate",
      lo.weights)` instead of `k` copies stamped `"optimal"`; `insert_frontier_points` already
      deletes any stale higher-`k` rows before inserting, so no schema/persistence change is
      needed. Test: `tests/test_quant_optimize.py::test_frontier_collapses_on_flat_mu_but_spans_on_dispersed_mu`
      strengthened to assert `len(flat_pts) == 1` and `status == "degenerate"` (fails on the
      pre-fix code: 6 points, all `"optimal"`). Full suite 710 passed (test count unchanged,
      existing test strengthened); ruff, format, mypy, pre-commit clean. `SPEC.md` FR-009 and
      `docs/quant.md` updated. Record: `docs/model_fixes.md` "T-112".
- [x] **T-113** *(P1 — before `T-079`)* LLM score provenance. The rule-based fallback score is
      stored under the model's own name (1 of production's 377 FUNDAMENTAL scores is a fallback
      labelled `deepseek-chat`); `temperature` is 0.2 with no seed; no prompt hash is recorded.
      Label fallback rows as such, set `temperature = 0` (and a seed if the API honours one),
      stamp a prompt hash per score, and record the fallback count on `analysis_run`.
      **Acceptance**: fallbacks are distinguishable from model output in `score_snapshot`;
      every new score carries its prompt hash.
      **Done 2026-09-27; corrected 2026-09-27 (PR #91 review, `@eldova1702`).** `build_model`
      sets `temperature=0`/`seed=0` (forwarded verbatim by Strands' `OpenAIModel`, safe whether
      or not the endpoint honours `seed`; docs say "reduces variance," not "reproducible" --
      no provider guarantees determinism at `temperature=0`). The first cut computed
      `prompt_hash` per filing, as a sha256 of the orchestrator's whole message history; review
      found this unverifiable (the transcript itself isn't stored) and unable to group scores
      by the prompt version that produced them (what `T-079` needs, to separate scores from
      before/after the `T-073`/`T-074`/`T-076` prompt edits) -- a per-filing hash is unique by
      construction. Corrected: new `_prompt_version_hash(model_name)` hashes `MASTER_PROMPT`,
      every specialist prompt, the synthesis/repair prompts, and the model config; computed
      **once** in `FundamentalAnalyst.__init__` and reused for every filing that analyst
      scores, so two filings in one run share one hash and it changes only when a prompt/SOP/
      model config actually changes. `pipeline.py` sets `SnapshotRow.model =
      agents.FALLBACK_MODEL_LABEL` ("`rule-based-fallback-v1`") when `used_fallback`, never the
      real model's own id, and bumps a new `analysis_run.fallback_units` counter. New additive
      columns: `score_snapshot.prompt_hash` (TEXT), `analysis_run.fallback_units` (INTEGER
      DEFAULT 0). Tests: `tests/test_agents.py` (two filings share a hash; changing a
      specialist prompt or the model id changes it) and
      `tests/test_pipeline.py::test_run_labels_a_fallback_score_and_records_it_on_the_run`.
      Full suite 714 passed (was 710); ruff, format, mypy, pre-commit clean. `SPEC.md` FR-002
      and `docs/fundamental_agent.md` updated. Record: `docs/model_fixes.md` "T-113".
      **Confirming the live DeepSeek endpoint actually accepts `seed` is left as an explicit
      step before `T-079` begins (this environment's network egress policy blocks reaching
      it).** **The one existing production fallback row keeps its stale `model` label and a NULL
      `prompt_hash` until relabeled — see `T-124`.**
- [x] **T-114** *(P1)* Clean-tree provenance. Production runs carry `code_version`
      `359797e-dirty` (cycle, quant, analysis runs): results come from uncommitted code. Refuse
      production writes from a dirty checkout unless `--allow-dirty` is passed (and recorded).
      **Acceptance**: a dirty checkout cannot write a run without the explicit override.
      **Done 2026-09-27.** New `kg_schema.provenance.dirty_tree_reason(version=None)`/
      `DirtyTree`: `None` if `code_version()` doesn't end in `-dirty`, else a reason naming
      it. Wired into every run-writing driver across all three packages the audit named --
      `fundamental_agent.pipeline.run`, `quant.persist.run_build_risk_model`/`run_optimize`,
      `quant.returns.run_build_returns`, `quant.actions.backfill_corporate_actions`,
      `quant.evaluate.run_evaluate`, `cycle.orchestrator._run` (shared by
      `select`/`monitor`/`backfill`) -- each raising `DirtyTree` unless
      `settings.allow_dirty`, at the same point in that function its own T-110/T-097/T-086
      guard already raises. `--allow-dirty` added to every affected CLI subcommand, recording
      the bypass reason on the run's `params_json` and a CLI `WARNING`. Test suite hardening:
      `tests/conftest.py` gained a session-scoped fixture pinning `code_version()` to a
      clean, deterministic value (hermetic per NR-006 -- the ambient repo legitimately has
      uncommitted changes during active development, which must not spuriously trip the new
      guard in every unrelated test). Tests: `tests/test_provenance.py` (+4),
      `tests/test_quant_risk_model.py` (+3), `tests/test_quant_pipeline.py` (+1),
      `tests/test_quant_dividends_guard.py` (+2), `tests/test_quant_actions.py` (+1),
      `tests/test_cycle.py` (+4), `tests/test_pipeline.py` (+3). Full suite 732 passed (was
      714); ruff, format, mypy, pre-commit clean. `SPEC.md` FR-012 and `docs/quant.md`/
      `docs/cycle.md`/`docs/fundamental_agent.md`/`docs/kg_schema.md` updated. Record:
      `docs/model_fixes.md` "T-114".
- [x] **T-115** *(P1)* `cycle backfill` cannot replay history: it calls the live
      `run_selection`, which mutates `portfolio_position` — since `T-097` it is refused as
      soon as a newer live book exists, and it has no override — and the checkpoint guard skips
      any already-completed (type, date) with no force option. Route replay positions to a
      separate simulated-book table and add a force/re-run flag. **Acceptance**: a full replay
      leaves the live book untouched and can be re-run after a fix.
      **Done 2026-09-27.** New `portfolio_position_replay` table (`kg_schema.ddl`) — same shape
      and T-104 triggers as `portfolio_position`, never read by or refused for conflicting with
      it; `cycle_run.cycle_type` gains `'REPLAY'`. New `cycle/replay.py`:
      `out_of_order_replay_reason`/`sync_replay_positions` (the live guard/writer's own shape,
      against the replay table) and `reset_replay_range(conn, date_from)` (`--force`'s
      implementation — drops/reopens every replay stint and `cycle_run` row on or after
      `date_from`, unconditionally, never touching an earlier date). `cycle.orchestrator.run_replay`
      is `backfill`'s new entrypoint (`cycle_type='REPLAY'`, same step sequence as
      `run_selection`); the `positions` step branches on `cycle_type` to call the replay path
      instead of `writers.sync_positions`, with no `--allow-backdated`-style override (nothing
      to override once `--force` resets from the range's start up front). `cycle/cli.py`'s
      `backfill` now calls `run_replay`; new `--force` flag calls `reset_replay_range` once
      before the date loop; `--db` is now mandatory and refused when it resolves to the
      configured production path by actual file (`os.path.samefile`, falling back to a
      resolved-path comparison), not a literal string match — a relative alias or a symlink to
      it is refused too (PR #94 review, second round; every step but `positions` still writes
      the shared database, so only a whole separate copy is truly isolated).
      14 new tests in `tests/test_cycle.py` (8 + 4 + 2 across two review rounds). Full suite
      753 passed (was 739); ruff, format, mypy clean. `docs/cycle.md`, `docs/kg_schema.md`,
      `SPEC.md`'s schema table updated. PR #94 review also opened `T-125` (veto lifecycle,
      P0, unrelated pre-existing gap — not fixed here). Record: `docs/model_fixes.md` "T-115".
- [x] **T-116** *(P1 — after `T-105`)* Recalibrate the negative-equity distress screen
      (`LEVERAGE_EXTREME` negative-equity branch, `DQ_NEG_EQUITY`'s HARD condition). Both use
      `debt_to_assets > 0.8` or `interest_coverage < 1.5`; the audit shows the first never
      reaches the buyback cohort (MCD 0.665) and proposes the standard credit pair
      `net_debt_to_ebitda` + `interest_coverage`, with NULL on both routed to SOFT review.
      `net_debt_to_ebitda` is only usable once `T-105` annualizes it. Methodology change:
      `docs/model_fixes.md` record. **Acceptance**: thresholds calibrated on annualized data;
      NULL-on-both never passes silently.
      **Done 2026-09-28.** Both gates now read `net_debt_to_ebitda > 5.0` (S&P Global Ratings'
      "Corporate Methodology" "highly leveraged" band, cited) in place of `debt_to_assets >
      0.8`; `interest_coverage < 1.5` unchanged. Recomputed over all 41 negative-equity filings
      in the 20-asset production sample (MCD 19, SBAC 19, APA 2, APO 1; annualized
      `net_debt_to_ebitda` via T-105's TTM method): MCD 2.53x-2.96x (spared, unchanged), SBAC
      6.91x-8.16x (HARD, unchanged) — the new metric reclassifies nothing in-sample, closing
      the gap for a future name `debt_to_assets` would have missed for the wrong reason.
      `_LeverageRule`'s NULL-on-both case (APA, APO: neither metric resolvable) now emits a
      SOFT `VetoHit` instead of silently dropping the hit entirely; `DQ_NEG_EQUITY` already
      handled this correctly (unchanged). `DATA_QUALITY_GATE_VERSION` bumped `dq-v1` -> `dq-v2`
      (append-only re-gate, T-065's own convention for a threshold change). Tests:
      `tests/test_cycle.py` (4 leverage-rule tests updated/replaced),
      `tests/test_data_quality.py` (2 tests updated). Full suite 753 passed (unchanged count —
      existing coverage reparametrized, not net-new); ruff, format, mypy clean. Record:
      `docs/model_fixes.md` "T-116". **Production re-gate under `dq-v2`** (a `python -m
      fundamental_agent quality` re-run) **is deferred, pending explicit user direction** — the
      same category as every other pending production action in this file.
      **PR #95 review (`@eldova1702`) found two real bugs, fixed 2026-09-28**: (1) bumping to
      `dq-v2` alone would have silently zeroed every quarantine/HARD issue until the production
      re-gate ran — new `kg_schema.queries.StaleGateVersion`/`stale_gate_version_reason` refuses
      `cycle select`/`monitor`/`backfill` when `data_quality_issue` holds an older gate version
      but none under the current one, unless `--allow-stale-dq-gate` (`CycleSettings.
      allow_stale_dq_gate`); (2) `net_debt_to_ebitda` reads negative EBITDA with positive net
      debt (the most distressed profile) as healthy, since the ratio itself goes negative —
      reproduced on real stored data (WAT 10-Q 2026-04-04, ratio -399.36). `_neg_equity`
      (`fundamental_agent`) now reconstructs EBITDA/net debt from the same raw inputs the ratio
      was divided from; `_LeverageRule` (`cycle`, no raw inputs available) treats a negative
      ratio as unresolved, not healthy, falling back to `interest_coverage`. Neither fix
      reclassifies any of the 41 in-sample filings (0 have the EBITDA<=0-with-debt shape today;
      forward-looking correctness fixes). Also corrected two docs claims: this entry's
      "APA/APO now surface a SOFT review hit" does not hold on the live path (`DQ_NEG_EQUITY`
      quarantines `debt_to_equity` before `_LeverageRule` ever sees it; `_LeverageRule`'s branch
      is a backstop for filings Ring-1 hasn't gated yet), and the S&P `>5.0x` citation is noted
      as a limitation (its band is on lease/pension-adjusted debt; this screen's is plain
      balance-sheet debt minus cash, more lenient for lease-heavy names). +12 tests (765 total).
      Record: `docs/model_fixes.md` "T-116", "PR #95 review".

- [x] **T-117** *(P0 — added 2026-09-25 from PR #77's review)* Guard revenue against a
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
      **Done 2026-09-28.** `Statements._label_total_correction` (`statements.py`): scans the
      same statement, document order, for a later row whose *label* (never a filer's own
      concept name) reads as a revenue total and is materially smaller than the Tier 1
      `total_concepts` match — a well-formed statement's later, broader total is never smaller
      than an earlier one labeled the same way, so finding one is itself the contradiction.
      Corrects by subtracting the rows between the two when they're individually small enough
      to trust; refuses to guess (returns no value, not the bad total) when they're not.
      `scripts/verify_t117.py` (new) scanned every stored filing's own period across
      production's **full 503-asset, 5,076-filing universe** (not a sample): 16 filing-periods
      flagged, all APA, all safely corrected, 0 false positives — after the first cut's naive
      `r"total...revenue"` regex was itself found (by that same full-universe scan, inspected
      one by one) to false-positive on 81 filing-periods across ADBE/STE/TER/TSLA/URI/XYZ, all
      "Total cost of revenues" (a near-universal COGS label matching "total"/"revenue" as bare
      substrings) — fixed with an explicit "cost" exclusion before any number here was final.
      APA's `revenue` recomputed for all 5 stored 10-Ks: FY2021 $7,988,000,000 (T-095's
      existing path, unchanged), FY2022 $11,075,000,000 (untouched, correctly not
      contradicted), FY2023 $8,279,000,000, FY2024 $9,737,000,000, FY2025 $8,920,000,000 —
      exact acceptance-criterion match, derived purely from each filing's own facts, no
      APA-specific code. `T-095`'s diagnosis corrected in `docs/model_fixes.md` (its own
      "Correction (T-117)" section): re-verified live against SEC's `companyfacts`/
      `companyconcept` APIs, APA has never filed `us-gaap:Revenues` at all — its real FY2023
      10-K income statement has no "Total revenues" line, only "Total revenues and other";
      the original "filer-side tagging defect" explanation does not hold. Tests:
      `tests/test_statements.py` (+5). Full suite 769 passed (was 765 post-T-116); ruff,
      format, mypy clean. Record: `docs/model_fixes.md` "T-117", "T-095" (Correction).
- [x] **T-118** *(P1 — upstream, `portfolio-data-mining`; added 2026-09-25 from PR #77's
      review)* Fix the root cause of `T-117`: the EDGAR gateway (`sec_edgar`) presents
      breakdown figures as company totals. Implemented upstream (same pattern as Work item 6);
      this task tracks it and verifies it here once deployed. `T-117` stays as the local guard
      until then. **Acceptance**: the gateway returns APA's statement-level totals; `T-117`'s
      guard no longer rejects APA.
      **Code done upstream 2026-09-28** (`portfolio-data-mining` PR #44,
      `gamug/portfolio-data-mining@fix/t118-sec-edgar-revenue-total-contradiction`).
      **Mechanism confirmed 2026-09-28** by that PR's review (`@eldova1702`, superseding
      the earlier, hedged "Oil and gas" corroborating-observation note): `edgartools==5.44.1`'s
      `xbrl.statements.income_statement().to_dataframe()` — the only source `get_financials`
      reads income-statement rows from — *synthesizes* a non-dimensional "total" row for a
      concept by summing that concept's dimensional axis members, and double-counts whenever
      one member is itself a parent whose value already includes its own children. APA's case
      is exactly this: FY2023's synthesized `us-gaap_Revenues` is `8279 (parent "Oil and gas")
      + 7385 + 894 (its two children, which already sum to 8279) = 16558`; FY2024 is
      `9737 + 8196 + 1541 = 19474` (`8196 + 1541 = 9737`, same shape). Confirmed against
      `data.sec.gov`'s `companyconcept` API:
      APA has never filed a real `us-gaap:Revenues` fact at all (`404 NoSuchKey`) — not a
      filer-side defect either. **This is a general `edgartools` synthesis defect, not an
      APA/revenue-specific one**: a full-universe scan across the 500-company stored universe
      (the 61 `us-gaap` concepts `fundamental_agent` reads, compared against each company's SEC
      `companyfacts`) found 163 stored values matching no SEC-filed value at that period end
      (e.g. SNA Q3 2024 operating income 2x, CTVA FY2021 D&A 2x, AEP Q2 2025 revenue 270.5 vs.
      5,086.9, CEG FY2021 revenue 55,588 vs. 19,649, MNST Q3 2023 net income to common 4.7 vs.
      452.7), and 105 (company, concept) pairs — 1,101 rows total — that were never filed
      non-dimensionally at all, so every value for them is synthesized (e.g. APA COGS,
      STZ/KKR diluted EPS and shares, WFC/UNH/SCHW contract revenue). New
      `sec_edgar.agent.correct_revenue_totals`, ported from this repo's own
      `Statements._label_total_correction` (`T-117`), applied to `get_financials`'
      `income_statement` before it's returned — **fixes APA's revenue instance only**, not the
      general defect. Live-verified there (no mocking) against every available APA 10-K
      (FY2021-FY2025) and every 2024 10-Q: resolves to the exact `T-117` acceptance figures
      (FY2023 $8,279M, FY2024 $9,737M, FY2025 $8,920M), FY2021/FY2022 correctly untouched. As of
      PR #44's latest commit, `get_financials` also returns a top-level `data["corrections"]`
      list (`{"concept", "column", "original", "corrected", "rule": "T-118"}` per entry)
      recording every value it corrects or drops, so a derived number never reaches this repo's
      database looking like a filed fact. +8 tests updated (242 total there);
      ruff/format/mypy/pre-commit clean.
      **Still open, and stays open past PR #44's merge**: that PR explicitly does not close
      this task. Remaining, in order: (1) PR #44 merging and the `sec_edgar` service
      redeploying, then re-verifying here that `T-117`'s local guard finds nothing left to
      correct on APA specifically (this task's original, narrower acceptance criterion, the
      same operational pattern as Work item 6's `T-052` handoff); (2) the upstream general fix
      (tracked there as `portfolio-data-mining` `T-042`, not yet started) — validating every
      synthesized non-dimensional row against the filing's own default-context facts for that
      concept/period, passing it through when filed, replacing it with the filed value or
      marking it synthesized (via the same `corrections` mechanism) otherwise, then
      re-ingesting the affected filings under a new facts version — is what this task's P1
      priority is actually about: APA/revenue was only ever the first-discovered instance of a
      universe-wide defect, not the whole of it; (3) *(this repo, added 2026-09-28 from PR #98's
      review)* when re-ingesting against the redeployed `sec_edgar`, `fundamental_agent` must
      persist `data["corrections"]` — today `Statements.from_payload` reads only the three
      statement keys (`income_statement`/`balance_sheet`/`cash_flow`), so a derived value like
      APA's corrected revenue would land in `financial_facts` indistinguishable from a real
      filed fact. Needs a flag or provenance field on the affected `financial_facts` row(s)
      recording that the value was derived/corrected, not filed as-is. Not yet designed or
      implemented.
      **Step (3) done 2026-09-30**: `Statements.corrections` (from `payload.get("corrections")`,
      `[]` for a pre-PR-#44 gateway) + `iter_facts` tagging the matching `(concept, column)`
      fact's `correction_rule`; new nullable `financial_facts.correction_rule` column
      (`kg_schema.ddl.REQUIRED_COLUMNS`, additive, no migration — same mechanism as
      `filing_version`/`run_id`); `db.append_financial_facts` persists it, gated on the column
      existing (mirrors the existing `has_versioned` fallback). `repair.py`'s re-ingestion path
      covered for free (already calls `iter_facts`). Tests: `tests/test_statements.py` (+2),
      `tests/test_db.py` (+1, round-trips through a real `memory_db`). Full suite 802 passed
      (was 799); ruff, format, mypy clean. Record: `docs/model_fixes.md`, this task's own entry
      ("Step (3) done" subsection); `docs/fundamental_agent.md`, `docs/kg_schema.md`, `SPEC.md`
      (`financial_facts` row) updated.
      **Steps (1) and (2) done 2026-09-30 — task closed.** The user raised the gateway
      (`http://host.docker.internal:8000`, unreachable from the sandbox as of the previous
      entry, confirmed reachable now) and merged the upstream general fix
      (`portfolio-data-mining` PR #45, `T-042`, plus PR #46 isolating its per-statement
      failures) the same day.
      **Step (1) — live re-verification against the redeployed gateway (real SEC data, no
      mocking):** APA's FY2023/FY2024/FY2025 10-Ks now resolve `revenue` to exactly `T-117`'s
      acceptance figures ($8,279M / $9,737M / $8,920M), each carrying `iter_facts.
      correction_rule = "T-118"`; end-to-end ingestion through the real
      `EdgarClient`/`Statements`/`db.append_financial_facts` path (a scratch in-memory DB, no
      production write) persists `correction_rule = 'T-118'` on those rows. **`T-117`'s guard
      itself never fires on any of these** — the corrected value already arrives inside
      `total_concepts`'s Tier 1 match, well above `_total_is_plausible`'s floor and with
      nothing later to contradict it — meeting this task's original acceptance criterion.
      **A further finding, not a regression:** APA's FY2021/FY2022 10-Ks (each filing's *own*
      target period) now resolve `revenue` to `None`, where `T-095`'s original fix had trusted
      $7,988M/$11,075M. Traced live: both were themselves uncaught instances of the *same*
      synthesis defect, just on `us-gaap_RevenueFromContractWithCustomerIncludingAssessedTax`
      instead of `us-gaap_Revenues` — the FY2021 filing's own dimensional breakdown rows sum
      exactly to $7,988M (`$6,501M` "Oil and Gas, Exploration and Production" +
      `$1,487M` "Oil and gas, purchased", themselves further sums of per-region/segment rows),
      confirming it was a synthesized rollup, not a filed fact; `T-042`'s
      `reconcile_with_filed_facts` correctly drops it (`corrections`: `rule: "T-042"`,
      `reason: "no_filed_nondimensional_fact"`) rather than reconstructing a number `T-118`'s
      revenue-specific layer has no later-contradicting-total to reconstruct it from. SEC's own
      `companyconcept` API confirms directly: APA has no FY-period value at all under that
      concept (only four stray zero/`Q1`/`Q2` 2021 rows). `_revenue_pos`
      (`fundamental_agent/quality.py`) already HARD-quarantines a filing with `net_income` set
      and `revenue` `None` — this repo needs no code change for it, only the production
      re-verification recorded here (and in `docs/model_fixes.md`) so it isn't mistaken for a
      new defect on the next audit.
      **Step (2)** is upstream's own closed task; nothing further for this repo to implement.
      Tests, lint, mypy unaffected (no code change, live-data verification only). Full record:
      `docs/model_fixes.md`'s `T-118` entry ("Steps (1) and (2), live re-verification" section).
      **PR #104 review (`@eldova1702`), fixed 2026-09-30**: (1) `EdgarClient.financials` now
      raises `EdgarError` when the gateway's own reconciliation failed for a statement
      (`data["reconciliation_errors"]`, upstream PR #46) — such a statement comes back
      rendered-but-unvalidated, and without this check it would have stored with
      `correction_rule = NULL` (indistinguishable from filed-as-is), exactly what this task's
      step (3) exists to prevent. The existing per-filing failure path (`_run_filing`) already
      does the right thing once it raises: unscored, `failed_units` incremented, retried on the
      next run (never in `engine.completed`). Tests: `tests/test_edgar_client.py` (+2),
      `tests/test_pipeline.py` (+1, full-run integration: a reconciliation failure writes no
      `financial_facts`/`score_snapshot` rows and counts one failed unit). (2) `iter_facts`'s
      correction lookup now keys on `(statement, concept, column)`, not `(concept, column)` —
      once `T-042` started reconciling all three statements, a concept like `NetIncomeLoss` can
      appear (and be corrected) on more than one; keying by concept and column alone could tag
      the wrong statement's fact. No live clash found in the reviewer's 8-filing sample, but
      fixed for correctness. Test: `tests/test_statements.py` (+1, same `(concept, column)` on
      two statements, only one corrected). (3) **Stored facts are not replaced by a plain
      re-run**: `financial_facts` is append-only, `INSERT OR IGNORE` on `(filing_id, statement,
      concept, period_key, filing_version = accession_number)` — reproduced directly
      (reviewer's own local database, `/Users/dova/thesis/data/financial.db`): APA FY2023 still
      stores revenue `16,558,000,000` and COGS `1,076,000,000` (both values `T-042` now drops)
      under `correction_rule = NULL`, because that accession's rows were already ingested
      before the gateway redeploy. `T-100`'s own `TASKS.md` entry now says it must run against
      a fresh `financial.db`, not one carried forward from before 2026-09-30; the priority note
      here and in `PLAN.md` no longer says "Work item 8 next" outright — the system-review
      follow-up tasks (a forthcoming docs PR) and the pilot reaching `verify_pilot` 0 FAIL come
      first, so `T-079`'s LLM re-run does not start early. (4, non-blocking) Confirmed in the PR
      thread: `T-121`–`T-124`'s production actions were genuinely already applied at the time
      this entry says so — each task's own record above (`T-121` "Production void applied
      2026-09-29", `T-122` "Second pass ... Post-run `PRAGMA quick_check` → `ok`", `T-123`
      "Post-write `PRAGMA quick_check` → `ok`", `T-124` "Post-write: zero ... rows") predates
      and is unaffected by this review. Full suite 806 passed (was 802, +4: `tests/test_edgar_
      client.py` ×2, `tests/test_pipeline.py` ×1, `tests/test_statements.py` ×1); ruff, format,
      mypy clean.
- [x] **T-119** *(P1 — found 2026-09-25 while testing `T-106`)* — **DONE 2026-09-29**
      `EARNINGS_MISSING` never fires for an asset with no FUNDAMENTAL score at all: the rule
      iterates `last_fundamental_dates`, which holds only assets that have one, so its `last is
      None` branch is unreachable. No effect today (all 20 ranked assets are scored); on the
      full-universe run (`T-100`) every unscored member would escape the check. **Decision
      (PR #78 review): an unscored asset is ineligible, not penalized** — no SOFT veto. A
      selection change: #12 record (`docs/model_fixes.md`, this task's own entry).
      **Acceptance**: a universe member with no public FUNDAMENTAL score is ineligible for
      selection in the same cycle it is detected (not through the T-1 veto lag); it stays in
      the ranking, marked with the reason in `veto_rules_json`, and is listed in the cycle's
      output; if more than 5% of the universe is unscored, the selection cycle stops with an
      error instead of building a portfolio.
      New `cycle.data.unscored_assets`/`TooManyUnscored`/`too_many_unscored_reason`; new
      `CycleSettings.unscored_max_share` (0.05, no `--allow-*` override); `orchestrator._rank`
      raises `TooManyUnscored` over that share of the universe, otherwise marks each unscored
      asset's `cycle_ranking` row `vetoed=True` with `"UNSCORED"` in `veto_rules` directly in
      this same step — never the `veto` table, never the T-1 cutoff, so exclusion from
      `positions` is immediate. `_StaleFundamentalRule`'s scope narrowed in its own docstring to
      "score exists but aged," no behavior change.
      **PR #99 review (`@eldova1702`, Sourcery), fixed same day**: (1) the first pass exempted a
      *whole*-unscored universe from `TooManyUnscored` (reasoning: a cycle dated before any
      filing is public yet, T-106/T-107, has no scored peer to be missing relative to) — this
      inverted PR #78's own decision ("more than 5% unscored stops the cycle" includes 100%);
      reproduced live against `cycle_seed` (deleting all 5 FUNDAMENTAL rows built a portfolio on
      TECHNICAL/VALORIZATION alone with zero `UNSCORED` marks and no warning); dropped the
      exemption from both functions, updated `test_a_cycle_before_the_filings_are_public_sees_
      no_fundamentals` to expect `TooManyUnscored`, added
      `test_all_unscored_also_refuses_the_selection_cycle`. (2) `select`/`monitor`/`backfill`
      never printed the unscored tickers, only a count nobody saw — new `CycleReport.
      unscored_tickers`, `cli.py`'s `_print_unscored`. (3) `report.unscored` misreported 0 on a
      resumed run that skips an already-`done` `rank` step — now read back from `cycle_ranking`
      (via `json_each(veto_rules_json)`) after `_do("rank", ...)` regardless of whether it ran
      or was skipped, not a step-local variable.
      Tests: `tests/test_cycle.py` (+10 net across both rounds), `tests/test_point_in_time_
      readers.py` (1 updated). Full suite 779 passed; ruff, format, mypy clean. Record:
      `docs/model_fixes.md` "T-119" (includes a "PR #99 review" section).
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
- [x] **T-121** *(P0 — before `T-122`; production DB action, at the user's direction only)* — **DONE 2026-09-29** Void
      the stale live-book snapshot `T-108`'s and `T-109`'s reviews both flagged: `quant_portfolio`
      id 4 (`as_of` 2026-06-30, `BF.B` weight 0.10) is a `T-104` leftover from the reverted
      2026-06-30 run, not the current live book, and its 41 `perf-v1`/`perf-v2` rows in
      `quant_benchmark_performance` are graded against it. Any dry run or `evaluate` reading that
      snapshot (as `T-108`'s pre-correction PR body did) produces meaningless active-return
      figures. **Acceptance**: `quant_portfolio` id 4 and its 41 dependent
      `quant_benchmark_performance` rows are voided (not merely ignored) on production; a
      post-void `quick_check` is clean.
      **Code done 2026-09-28**: `quant void-portfolio --portfolio-id N [--apply]`
      (`quant/repair.py`) — `plan_void` reads the row, its `quant_position` stints and
      `quant_benchmark_performance` count read-only, refusing anything but `kind='live_book'`
      (never a real optimized book); `apply_void` deletes it in one transaction
      (`quant_position`/`quant_benchmark_performance` cascade via `ON DELETE CASCADE`;
      `quant_frontier_point` deleted explicitly first, since it carries no cascade action and a
      real book's frontier points must never vanish as a side effect). Dry run unless `--apply`,
      same convention as `cycle undo-run` (T-104). Exercised end to end on a throwaway copy of
      production (never the live file): dry run printed exactly the documented shape (id 4,
      `as_of` 2026-06-30, `BF.B` weight 0.10, 41 performance rows); `--apply` deleted them,
      `PRAGMA quick_check` ok, `PRAGMA foreign_key_check` clean, the other 3 `quant_portfolio`
      rows untouched. Tests: `tests/test_quant_repair.py` (6, incl. refusing an unknown id and a
      non-`live_book` kind). Full suite 759 passed (was 753); ruff, format, mypy clean.
      **Production void applied 2026-09-29** (PR #96 merged, then run at explicit user
      direction): `financial.db` backed up first
      (`/workspaces/thesis/data/financial.db.pre-t121-void-backup-20260929`, the same naming
      convention as `T-052`/`T-068`/`T-088`/`T-101`/`T-104`/`T-107`/`T-120`'s own backups). Dry
      run against `KG_FINANCIAL_DB` matched the documented shape exactly (id 4, `as_of`
      2026-06-30, `BF.B` weight 0.10, 41 performance rows); `--apply` deleted them. Post-void:
      `PRAGMA quick_check` -> `ok`, `PRAGMA foreign_key_check` -> no rows, the other 3
      `quant_portfolio` rows (ids 1-3, `min_var`/`tangency`/`target_vol`, `as_of` 2026-09-22)
      untouched, a repeat dry run correctly reports id 4 no longer exists.
- [x] **T-122** *(P0 — after `T-121`; production DB action, at the user's direction only)* —
      **DONE 2026-09-29** Re-persist `T-108`'s and `T-109`'s fixes to production. Every risk
      model and benchmark series built before those fixes landed still carries the pre-fix
      numbers: the benchmark's geometric mean-of-log-returns index over a hard-veto-filtered
      panel (`T-108`), and an excess-return `equilibrium` μ that understates every book's
      `expected_return`/`sharpe` whenever `ret_estimator = "equilibrium"` (the default) —
      `tangency`'s weights themselves, not only its reported stats, are wrong today (PR #85
      review: 66% of its weight moves, Sharpe 0.038 → 0.325, once μ is corrected).
      **Acceptance**: a fresh production `build-risk-model` → `optimize` → `evaluate` run over
      the current live book and universe, with `T-121` already applied; the dry-run figures in
      `docs/model_fixes.md`'s `T-108`/`T-109` entries are replaced with real post-fix production
      numbers, and any `tangency` book read afterward reflects the corrected μ.
      **Scope note**: this session's quant/cycle testing deliberately stays on the 20-ticker
      development sample (`APA, APO, BF.B, CPT, ESS, HOOD, HUM, MA, MCD, NEE, PG, PM, PSX, SBAC,
      STZ, T, UDR, WAT, WFC, XOM` — the same 20 `quant_return_daily`/`qret-v2` already covers),
      not the full 503-member universe (`T-100`'s eventual full-universe cutover, not yet run).
      A custom, checked-in `/workspaces/thesis/data/universe_sample20.db` (copied from the real
      `universe.db`, filtered to these 20 symbols) is passed as `--universe-db` to every `quant`
      command below, so nothing widens back to the full universe by omission.
      **First pass (`--analysis-date 2026-08-27`) was incomplete**: `price_daily`'s spine capped
      there at the time (T-110's own last `--store-daily` backfill), three weeks *before* the
      live book's only stints ever opened (`valid_from = 2026-09-22`) — no date had both a live
      book and forward price data at once, so `evaluate` could rebuild the benchmark but
      snapshotted no live book and evaluated 0 perf rows (`quant_run` id 14, harmless, left in
      place). `build-risk-model`/`optimize` still produced real, T-109-fixed numbers at that
      date (model id 2; `quant_portfolio` ids 4/5/6): tangency Sharpe `0.038 → 0.325`,
      `expected_return` `0.0528 → 0.0890`, `expected_vol` `0.2056 → 0.1355` — matching PR #85
      review's own prediction almost exactly; `min_var` Sharpe `-0.089 → 0.288`, `target_vol`
      `0.0068 → 0.319`.
      **Pricing gateway raised 2026-09-29, at the user's direction**: `pricing_agent run
      --analysis-date 2026-09-29 --start 2026-08-20 --store-daily --observations` refreshed
      `price_daily`/`price_observation` through today. Run without `--limit`/`--tickers` by
      mistake, so it covered the full 503-member universe rather than the 20-ticker sample
      (`pricing_run` id 8, 503/503) — caught and corrected (this entry's own scope note, above);
      the extra pricing coverage for the other 483 names is harmless and additive (`price_daily`/
      `price_observation` rows only, nothing quant-side reads), left in place rather than
      reverted, but not built on further.
      **Second pass, properly scoped via `--universe-db universe_sample20.db`**:
      `backfill-actions --from 2022-01-01 --to 2026-09-29` (20/20 fetched) → `build-returns
      --from 2022-01-01` (20 assets, 440 new `qret-v2` rows) → `build-risk-model
      --analysis-date 2026-09-29` (model id 3, 17 assets — `MA` still fails the risk model's own
      liquidity/history gate, unrelated to T-108/T-109) → `optimize --max-name-weight 0.15`
      (matching the first pass's own override; the default `0.05` cap makes a 17-name book
      infeasible, `17*0.05=0.85 < 1`, unrelated to this task) → `min_var`/`tangency`/
      `target_vol`/15-point `frontier` books (`quant_portfolio` ids 7/8/9), confirming the same
      fixed numbers as the first pass (tangency Sharpe `0.3253`, consistent day-to-day drift
      from `0.3249`, both real post-fix figures) → `evaluate --from 2026-09-22
      --analysis-date 2026-09-29 --benchmark SP500_EW_INTERNAL`: **live_book #10 snapshotted (10
      positions, matching the live book exactly) and evaluated against `SP500_EW_INTERNAL` over
      5 real forward trading days (2026-09-23 → 2026-09-29)** — cumulative_return `-0.88%`
      vs. the rebuilt (T-108-fixed) benchmark, daily `active_return` ranging `-0.46%` to
      `+0.91%` (`quant_benchmark_performance`, `perf-v2`, `portfolio_id = 10`). Post-run
      `PRAGMA quick_check` → `ok`, `PRAGMA foreign_key_check` → no rows.
      Acceptance now fully met: a real `build-risk-model` → `optimize` → `evaluate` production
      run over the current live book and (sample) universe, `T-121` already applied, with
      genuine post-fix numbers for both `T-108` (the live book's own benchmark comparison) and
      `T-109` (tangency's corrected μ). `docs/model_fixes.md`'s `T-108`/`T-109` entries updated
      with these final numbers, replacing the dry-run figures.
- [x] **T-123** *(P1 — after `T-110`'s code fix; production DB action, at the user's direction
      only)* — **DONE 2026-09-29** Clean the production orphan rows `T-110` found: 503
      `price_observation` rows dated 2026-08-28 with no matching `price_daily` bar, and
      whichever of `quant_run`s 7–10 and the two `cycle_run`s were built at an as-of past the
      price spine then in effect (each now individually assessed — a run may simply need
      re-recording as stale-as-of rather than voided, if its own inputs were otherwise fine).
      The going-forward guard (`T-110`, code done) prevents a recurrence; this is the one-time
      cleanup of what already exists. **Acceptance**: zero `price_observation` rows without a
      same-day `price_daily` row; each flagged `quant_run`/`cycle_run` is either re-run within
      the price spine or explicitly annotated as a known-stale historical run, not left silently
      ambiguous.
      **Orphan rows: already zero**, a side effect of `T-122`'s own pricing refresh
      (`pricing_agent run --store-daily --observations` together this time, unlike the original
      `--observations`-without-`--store-daily` run that created the orphans) — verified directly
      (`price_observation` LEFT JOIN `price_daily` on `(asset_id, date)`, zero unmatched rows),
      no separate cleanup needed.
      **`quant_run`s 7/10 (`build-returns`/`evaluate`) individually assessed as not applicable**:
      T-110's stale-as-of guard was never wired into either command by design — `build-returns`
      builds over an explicit `--from`/`--to` range, not a point-in-time universe resolution;
      `evaluate` "deliberately not guarded (already degrade gracefully on missing forward
      data)" per `T-110`'s own record. Neither claimed a freshness its inputs didn't have; no
      annotation needed.
      **`quant_run`s 8/9 (`build-risk-model`/`optimize`, as-of `2026-09-22`) and `cycle_run` 1
      (`SELECTION`, `2026-09-22`) individually assessed as genuinely stale-as-of** (`price_daily`
      capped at `2026-08-27` when each ran) **and retroactively annotated, not re-run**: each
      `params_json` now carries `"stale_as_of_bypassed"` naming the as-of, the spine then in
      effect, and that it was annotated after the fact under `T-123` rather than re-run.
      **Precision (PR #101 review, Sourcery):** this key means something different on each
      table, and the annotation is not "the same thing the guard would have written" on
      `cycle_run` specifically. `quant`'s `open_run` runs *before* its `StaleAsOf` check
      (`persist.py`), so every `quant_run` row -- refused or not -- already carries this key
      going forward; the `quant_run` 8/9 annotation matches that existing behavior exactly.
      `cycle`'s own check runs *before* `open_cycle` (`orchestrator.py`), so a normal refused
      stale `select`/`monitor` never creates a `cycle_run` row at all going forward -- the key
      only ever appears there when `--allow-stale-prices` explicitly permitted the run. `cycle_
      run` 1's annotation is therefore a separate, one-off retroactive database write (this
      `cycle_run` row already existed, pre-dating the guard) mimicking the shape an
      `--allow-stale-prices`-permitted run's own `params_json` would carry, not something the
      going-forward guard itself would ever produce for a row that didn't already exist.
      A deliberate, lower-risk choice over re-running `cycle select` specifically, since that
      would reopen/close live positions with real portfolio consequences, well beyond a
      metadata cleanup task; the user can ask for a fresh `select` separately if an updated live
      book is wanted. `quant_run` 8/9's own outputs (`quant_risk_model` id 1, `quant_portfolio`
      ids 1-3) are additionally already superseded by `T-122`'s fresh, correctly-scoped
      2026-09-29 re-run (`quant_risk_model` ids 2/3, `quant_portfolio` ids 4-9) -- kept, not
      voided (`quant void-portfolio`, T-121's tool, refuses anything but `kind='live_book'` by
      design; a real optimized book's history stays on record).
      **`cycle_run` 2 (`SELECTION`, `2026-06-30`) assessed as not applicable**: a deliberate
      backdated run (`cycle_date` far before any spine concern either then or now), already
      `status='reverted'` via `T-104`'s own `undo-run` for an unrelated reason — no stale-as-of
      claim to annotate.
      Backed up `financial.db` first
      (`financial.db.pre-t123-annotate-backup-20260929`). Post-write `PRAGMA quick_check` →
      `ok`, `PRAGMA foreign_key_check` → no rows.
- [x] **T-124** *(P1 — after `T-113`'s code fix; production DB action, at the user's direction
      only)* — **DONE 2026-09-29** Relabel the one production `score_snapshot[FUNDAMENTAL]` row
      `T-113` found stored under `model = 'deepseek-chat'` but actually a rule-based fallback, to
      `agents.FALLBACK_MODEL_LABEL`. `prompt_hash` stays `NULL` for this and every other
      pre-fix row — the original LLM interaction (or failed attempt) that produced them no
      longer exists to hash retroactively; that is expected, not a defect to correct.
      **Acceptance**: zero production `score_snapshot[FUNDAMENTAL]` rows with `model` equal to
      a real configured model id whose score actually came from `_fallback_assessment`.
      **Identified precisely, not by inference**: `_fallback_assessment` (`agents.py`) writes a
      fixed `narrative` ("Automated fallback: the language model could not return a usable JSON
      verdict, so this score is derived directly from the computed ratios"), which no real LLM
      reply ever produces verbatim — `score_snapshot` id 171 (asset 373, `PG`, `filing_id`
      3529), of 377 total FUNDAMENTAL rows, is the sole match. Relabeled `model` from
      `'deepseek-chat'` to `'rule-based-fallback-v1'` (`agents.FALLBACK_MODEL_LABEL`);
      `prompt_hash` left `NULL` as expected. `fundamental_snapshot_legacy` (the frozen, pre-`m004`
      migration table) checked and has zero rows matching that narrative -- it predates this
      fallback row entirely, out of this task's scope, not touched. Backed up `financial.db`
      first (`financial.db.pre-t124-relabel-backup-20260929`). Post-write: zero
      `score_snapshot[FUNDAMENTAL]` rows have that narrative under any label but
      `rule-based-fallback-v1`; `PRAGMA quick_check` → `ok`, `PRAGMA foreign_key_check` → no
      rows.
- [x] **T-125** *(P0 — added 2026-09-27 from PR #94's review; before any `cycle backfill` run
      and before the next live `cycle select`)* — **Code done 2026-09-29.** Veto lifecycle: a veto is a stint, not a
      per-date event. Today `writers.write_vetoes` clears only rows `WHERE cycle_date = ?` (the
      run's own date), and `hard_vetoed_as_of` / `active_soft_vetoes` /
      `quant.db.hard_vetoed_as_of` read every uncleared row `<= cutoff`. As a result (all
      reproduced): a HARD veto is permanent once raised, even with the condition false on 3
      later cycles; the same SOFT rule counts once per cycle it held (4 weekly cycles = -60,
      not -15); production (`financial-2.db`) still carries WAT's HARD `NEGATIVE_FCF` from the
      reverted 2026-06-30 run as active, and 7 names carry the same SOFT rule on two dates.
      **Fix**: (a) schema — `veto` holds stints: `raised_on`, `cleared_on` (cycle dates, not
      wall-clock), `last_seen_on`, `severity`, `evidence_json`, `run_id`, at most one open
      stint per `(asset_id, rule_id)` (partial unique index `WHERE cleared_on IS NULL`);
      `detected_at`/`cleared_at` stay as wall-clock metadata only. (b) three-state evaluation —
      each rule returns its hits and the set of assets it could evaluate; an open stint closes
      only for an asset that was evaluated and not hit; an asset the rule could not evaluate
      (missing data) keeps its open stint and never opens a new one. (c) transitions at cycle
      date N: hit + no open stint → open (`raised_on = N`); hit + open stint →
      `last_seen_on = N`; evaluated, not hit, open stint → `cleared_on = N`; a rule disabled in
      `rule_catalog` → its open stints close at N. (d) one point-in-time predicate in
      `kg_schema`, used by both `cycle` (HARD filter, SOFT penalty) and `quant` (universe
      gate): active at cutoff C ⇔ `raised_on <= C AND (cleared_on IS NULL OR cleared_on > C)`,
      C = N-1 (T-1 lag for both raising and clearing). (e) SOFT penalty =
      `soft_veto_penalty × count(distinct active SOFT rules)`. (f) idempotent re-run:
      re-running date N first undoes N's own transitions (deletes stints raised on N, reopens
      stints cleared on N), then re-applies them; a live veto evaluation at a date older than
      the latest transition is refused, the same rule as `T-097`. (g) replay — `reset_replay_range`
      (`T-115`'s `--force`) also undoes veto transitions on or after `--from` in the replay
      database. (h) migration — collapse existing per-date rows into stints using the
      completed veto-step dates already recorded in `cycle_checkpoint`.
      **Acceptance**: a HARD veto clears the first cycle its condition is false and the asset
      was evaluated (not permanent); a SOFT rule penalizes once while its stint stays open, not
      once per cycle it holds; re-running a past date is idempotent; `cycle backfill --force`
      resets veto transitions the same way `T-115` resets positions. Changes live behavior
      (`SPEC.md` FR-006) — its own PR, separate from any other in-flight work.
      **Done 2026-09-29.** All eight sub-items (a)-(h) implemented as specified above:
      `veto` rebuilt to stints (`kg_schema/ddl.py`); `Rule.evaluate()` now returns a
      `RuleResult(hits, evaluated)` (`cycle/rules/base.py`/`builtin.py`) so `write_vetoes`
      (`cycle/writers.py`) can tell "evaluated, no longer hit" from "couldn't evaluate" per
      rule per asset; transitions, disabled-rule closure, and the self-undo-then-reapply
      idempotent re-run all live in `write_vetoes`; the shared point-in-time predicate
      (`hard_vetoed_as_of`/`active_soft_vetoes`) moved to `kg_schema.queries`, replacing the
      two independent copies in `cycle.writers` and `quant.db` (the latter now re-exports it);
      `veto_out_of_order_reason` (new, `kg_schema.queries`) mirrors `T-097`'s guard, wired into
      `cycle.orchestrator._run` behind a new `--allow-backdated-veto` (`select`/`monitor`);
      `cycle.replay.reset_replay_range` (`--force`) now also deletes/reopens veto transitions
      from `--from` onward; migration `m009` (`kg_schema/migrations.py`) collapses old
      per-date hit rows into stints using the `"veto"` checkpoint step's own completed cycle
      dates as the evaluation timeline. Verified against a scratch copy of
      `data/financial-2.db` (never the tracked file): before, WAT's HARD `NEGATIVE_FCF`
      (raised 2026-06-30, reverted-run leftover) reads active forever and 7 SOFT-vetoed names
      would double-count at a projected next cutoff (`penalty x 2`); after `migrate`, WAT's
      stint correctly closes `2026-09-22` (the cycle that re-evaluated it clean), the 7 names
      collapse to one open stint each (`penalty x 1`), and `hard_vetoed_as_of` returns exactly
      `{MA, SBAC}` at that cutoff. +9 tests (794 total; `test_cycle.py` ×8 unit-level
      `write_vetoes`/guard/replay-reset tests, `test_kg_schema.py`'s `m009` migration test
      reproducing the exact WAT/re-raise shape found in production); ruff, format, mypy clean.
      Record: `docs/model_fixes.md` "T-125". `SPEC.md` FR-006 and the `veto` schema row,
      `docs/cycle.md`, `docs/kg_schema.md`, `docs/quant.md` updated.
      **Production `migrate` (the real `data/financial.db`'s `m009` rebuild) is deferred,
      pending explicit user direction** — the same category as `T-121`-`T-124`.
      **Known, accepted residual scope** (see `docs/model_fixes.md`'s "Design decisions"):
      `veto` stays one table shared between live and REPLAY runs, not mirrored into a
      `veto_replay` table the way `T-115` isolated `portfolio_position` — `cycle backfill`'s
      `--db` being mandatory-and-always-a-copy (already true since `T-115`) is what actually
      protects the live book, not per-cycle-type partitioning of veto stints, which would
      conflict with (a)'s single global "at most one open stint" constraint.

## Work item 15 — Dev environment: parameterize devcontainer mount path — DONE 2026-10-01

- [x] **T-126** `.devcontainer/devcontainer.json`'s `mounts` entry hardcodes a
      contributor-specific host path (`source=/Users/dova/thesis`), so the
      bind mount only works on that one machine's filesystem layout.
      Replace it with a `${localEnv:VAR_NAME}` substitution (devcontainer's
      host-environment variable syntax, resolved before the container is
      created — not `${containerEnv:...}`, which reads a variable already
      set inside the container), optionally with a fallback default
      (`${localEnv:VAR_NAME:/Users/dova/thesis}`), so each contributor sets
      their own path via an env var instead of editing the committed file.
      **Done 2026-10-01 (PR #106, `16c3a05`).** The mount is now
      `source=${localEnv:THESIS_HOST_DIR},target=/workspaces/thesis,...`, with no fallback
      default; `THESIS_HOST_DIR` is the shared variable name used across this repo's
      `portfolio-*` siblings that mount the same directory, and is documented in `README.md`.

## Work item 16 — Fine-tuning follow-ups: second-iteration review (`fixes_feedback.md`) — DONE 2026-10-01

Added 2026-10-01, from `fixes_feedback.md`'s independent empirical verification of the
Work item 7/13/14 batch of fixes (76 commits, `562081f..3d2d47c`, verified against the
20-ticker sample in `data/financial-2.db`). Every item below refines an **already-closed**
task — `T-060`, `T-095`, `T-062`/`T-094` and `T-067` all verified correct against their
original acceptance criteria; these are edge cases the broader universe surfaced that the
original fix's scope didn't cover, not regressions. → `fixes_feedback.md` §5.

- [x] **T-127** *(F1 follow-up to `T-060`)* `src/fundamental_agent/metrics/valuation.py`:
      7 historical MCD filings (2023-12-31–2025-06-30) keep an unscaled ~$200K market cap
      because `T-092`'s tripled 10-Q ingestion caused later filings to restate the same
      unscaled comparative share count, and the detector's "agreement with historical
      filings proves it's clean" heuristic treats that restated agreement as confirmation,
      suppressing the EPS-implied $10^6$ correction. Let the EPS-implied share signal
      override historical agreement whenever `mcap / total_assets < 0.001`. **Execution
      order matters**: this calculation-engine fix must land and be verified before any
      Ring-1 `DQ_MCAP_SCALE` re-gate (`T-040`/`T-065`) runs against the affected filings —
      otherwise the gate quarantines/vetoes MCD instead of the value being corrected.
      **Acceptance**: all 7 MCD filings correct to the ~$190B–$225B range (consistent with
      the filings already fixed); `T-065`'s gate passes MCD with 0 vetoes afterward.
      **Done 2026-10-01 — already fixed by `T-103` (closed 2026-09-25), no code change here.**
      The review's data (`data/financial-2.db`, batch `562081f..3d2d47c`, last commit 2026-09-22)
      predates `T-103`, which fixed exactly this: the seven MCD filings were poisoned by
      `overlapping_history` reading *later* filings (a look-ahead) whose mis-scaled restatement of the
      same period read as proof the period was clean. `T-103` made the history point-in-time and added
      the EPS guards, so the EPS-implied ×10⁶ is no longer overruled; the `mcap / total_assets <
      0.001` override proposed here was therefore not added (it would put a magnitude heuristic
      on top of the same cause). Re-verified: `tests/test_share_scale.py` (19 passed, incl. the
      MCD-with-a-later-mis-scaled-restatement case); `docs/model_fixes.md` T-103 records all seven MCD
      filings at $184–224B (3.4–4.0× assets) clearing `DQ_MCAP_SCALE`. Production still holds the
      `metrics-v2` values the review saw (checked read-only: MCD 2023-12-31..2025-06-30 at ~$200k); its
      re-persist is `T-100`'s, per `T-103`. The execution-order point stands and is already respected:
      the calculation fix landed before any `DQ_MCAP_SCALE` re-gate.
- [x] **T-128** *(follow-up to `T-095`)* `src/fundamental_agent/statements.py`
      (`Statements._total_is_plausible`): the 50% magnitude floor
      (`_TOTAL_PLAUSIBILITY_FLOOR`), calibrated against APA's FY2021 defect, false-positives
      on natural-gas producers whose top-line revenue legitimately nets hedging-contract
      adjustments below 50% of their largest gross component (EQT FY2021/FY2024/FY2025,
      EXE FY2022/FY2024 — 5 of 985 totals checked in the full universe). Replace the
      percentage heuristic with a structural XBRL duplicate-slice check (does an
      un-dimensional revenue line exactly duplicate a dimensional sub-line) before `T-100`'s
      full-universe run. **Acceptance**: APA FY2021 still resolves to $7.988B; EQT/EXE's
      5 flagged totals are no longer rejected.
      **Done 2026-10-01.** `_total_is_plausible` now rejects a total only when it exactly duplicates
      a *dimensional* row of the same concept (on a non-whole-entity axis) **and** a named component is
      larger than it; `_TOTAL_PLAUSIBILITY_FLOOR` is gone. Verified against the real 10-K inline-XBRL
      instances on sec.gov (the gateway was unreachable from here): APA FY2021 `us-gaap:Revenues`
      exists *only* on the equity-method-investee dimension ($1,082M) → still rejected → $7,988M; EQT
      FY2021/FY2024/FY2025 and EXE FY2022/FY2024 totals have no dimensional twin → all five kept. `T-095`/`T-117`'s
      statement that APA "never filed `us-gaap:Revenues`" is corrected in `docs/model_fixes.md`'s
      T-128 entry (SEC's `companyfacts` API omits dimensional facts; the filing has one).
      Single-segment filers, whose segment revenue legitimately equals the total, are exempted by axis.
      Five new test functions replace the floor-boundary pin (2 existing tests updated),
      mutation-checked on all three guards; 810 passed, `ruff`/`mypy` green. Production re-persist is `T-100`'s.
- [x] **T-129** *(F4 residual, follow-up to `T-062`/`T-094`)*
      `src/fundamental_agent/metrics/roic.py` and `leverage.py`:
      `return_on_invested_capital` and `net_debt_to_ebitda` were explicitly left
      unannualized when `T-094` TTM-annualized ROA/ROE/asset_turnover
      (`docs/model_fixes.md` lines 643–646) — 10-Q/10-K median ratios are 3.54x and 0.25x
      respectively, vs. ~1.0x for the already-fixed ratios. Consume the existing `ttm`
      dict for `operating_income` in `roic.py`; annualize `ebitda` in `leverage.py` via TTM
      difference or ×4 fallback, same convention as `T-094`. Matters because ROIC feeds
      `cycle/scores/valorization.py`'s VALORIZATION quality factor directly, so 10-Q filers
      are currently quality-scored unfairly low relative to 10-K filers. **Acceptance**:
      10-K/10-Q median ratio for both metrics falls within the ~1.0–1.1x band `T-094`
      achieved for ROA/ROE/asset_turnover.
      **Done 2026-10-01 — already fixed by `T-105` (closed 2026-09-25), no code change here.**
      `roic.py` and `leverage.py` already take the `ttm` dict (`operating_income`; `operating_income`
      + `depreciation_amortization`), built by `db.ttm_detail` via the standard identity `FY(prior
      10-K) − YTD(prior) + YTD(current)` → four quarters → ×4. The review's data predates it.
      `docs/model_fixes.md` T-105 records the 10-K/10-Q median ratios recomputed over the 20-asset
      sample: ROIC 3.54 → **0.95**, net debt / EBITDA 0.25 → **0.95** (10-K ÷ 10-Q; marginally under the
      acceptance's "~1.0–1.1×" band's low edge, i.e. 10-Qs now run ~5% higher, which is parity
      within noise rather than an annualization gap). Production still holds `metrics-v2`; re-persist is `T-100`'s.
- [x] **T-130** *(operational, follow-up to `T-067`)* `src/quant/cli.py` (`quant optimize`):
      the Markowitz books (`min_var`/`tangency`/`target_vol`) were formed 2026-09-22, after
      the available price series ends (2026-08-27), so `quant evaluate` can't forward-track
      them even though `T-067`'s `--from` default fix works correctly (`live_book` gets 41
      daily rows). Anchor future optimizer formation runs to a date with subsequent price
      coverage (e.g. `--as-of 2026-06-30`, matching `live_book`) as part of `T-068`'s Phase-A
      re-sequence. **Acceptance**: `quant evaluate` produces forward daily rows for all four
      books, not just `live_book`.
      **Done 2026-10-01 — satisfied without a new production run; no code change here.** The
      failure mode (books formed past the end of the price series) is now blocked at the source by
      `T-110`'s price-spine guard (`--allow-stale-prices` is the only override), and production moved
      on: `price_daily` now ends 2026-09-29, and read-only on `financial.db` each book kind already has
      forward rows from earlier `evaluate` runs (none re-run here) — `min_var`/`tangency`/`target_vol` @ 2026-08-27: 22 daily rows each (2026-08-28 →
      2026-09-29), `live_book` @ 2026-09-22: 5 (2026-09-23 → 2026-09-29), plus the @ 2026-09-22 optimizer
      books at 5. (`min_var`/`tangency`/`target_vol` @ 2026-09-29 have none yet, correctly: no price after
      their as-of.) `optimize --as-of` already exists, so no flag was needed. For `T-100`: `T-110`
      blocks an as-of *past* the spine's last date; to be forward-trackable, form the books strictly
      before it.

## Work item 17 — P0/P1: data-integrity defects from the 2026-09-29 system review — DONE 2026-10-02

Added 2026-10-01, from `docs/md primera revision/system_review_2026-09-29.md` §4 (a
whole-pipeline review of code and logic, checked against `data/financial-3.db`). Each task
below is verified on that database, affects results, and is not covered by an existing task.
They landed **ahead of Work item 8**: `T-131` first, then
`T-132` and `T-133`, then the pilot. The review's findings N1–N4 → `T-131`/`T-132`, N5/N6 →
`T-133`; N7–N9 and N11 became scope additions to `T-070`/`T-071`/Work item 8 (in `TASKS.md`), and N10
(the `dq-v2` production re-gate) stays a user-direction action, not a task.

> **Note — `T-122`'s moving-average note:** `T-122`'s record in `CHANGELOG.md` says MA was
> excluded by the liquidity/history gate. It was excluded by a **HARD veto** (`T-125`), not
> the liquidity gate: MA has full history (1,189 days). `T-122` is closed, so the correction
> lives here rather than in its record.
>
> **Note — `T-132`(a)'s upstream change has landed:** the cover-page share count
> (`dei:EntityCommonStockSharesOutstanding`) is exposed by the `portfolio-data-mining` gateway
> since 2026-10-02 (its `T-043`, PR #48: `data["cover"]["shares_outstanding"]`), the same pattern
> as `T-042`/`T-118`. `T-133` is local and never waited for it.

- [x] **T-131** *(P0)* Price ingestion integrity (`pricing_agent`). **Code done 2026-10-01 (PR #109, approved).**
      - (a) Never store a bar for a session that hasn't closed: using the NYSE calendar,
        refuse `--analysis-date`/`--end` of today before the close plus a buffer.
      - (b) Build `price_observation` from the asset's **full stored `price_daily` history**,
        not the fetched window, and recompute (upsert) every observation date whose inputs
        changed.
      - (c) When `corporate_action` shows a split dated after an asset's stored history
        began, re-fetch that asset's full history and rebuild its observations and returns.
        Refuse to store a series with a split-shaped jump (×2, ×0.5, ×3 …) that doesn't match
        a recorded split.
      - (d) Filter by `engine_version` in `quant.universe._history_counts` and
        `_median_dollar_volume`, which today would double-count history as soon as a second
        returns or observation version exists (forensic audit Q4).

      **Why (system review N1–N3)**: pricing runs on 2026-09-29 at 14:04–14:10 UTC stored a
      partial intraday session as the day's close (363M shares across 503 names against a
      normal 2,684–3,371M); `price_observation` and `quant_return_daily` are `INSERT OR
      IGNORE`, so it is permanent and feeds the 09-29 benchmark, evaluation rows and risk
      model 3. Observations computed only over a refresh's fetched window left
      `realized_vol_90d`, `momentum_252d`, `max_drawdown_90d` and `momentum_63d` NULL on
      10,500 rows (all 503 names since 2026-08-31). APH (2:1, 2026-09-03) and MNST (2:1,
      2026-08-11) have split-shaped jumps from a partial refresh (7 in total; real moves such
      as FISV −44% are not errors).

      **Clean-up** *(production DB action, at the user's direction only; not needed if
      production is never used again before `T-100`, since the pilot and `T-100` both start from
      a fresh database)*: delete the 2026-09-29 rows from `price_observation` and
      `quant_return_daily` and the benchmark, perf and risk-model rows built on them; rebuild
      observations for all 503 names from full history; re-fetch APH and MNST in full.

      **Acceptance**: an intraday run for today is refused; an incremental refresh produces
      observations identical to a full recompute; 0 split-shaped jumps; `verify_pilot.py`
      T-131 checks pass.

      **Done 2026-10-01** (branch `fix/t131-price-ingestion-integrity`). (a)
      `kg_schema.trading_calendar.session_final_at`/`session_is_open_or_pending`/`last_final_session`
      (16:00 ET + 1 h, US DST by rule) and `pipeline._require_closed_session`: a trading-day end whose bar
      is not final raises `SessionNotClosed` before the DB is opened (the default `--analysis-date` is
      therefore refused most of a trading day — pass the latest final session, which the error names).
      (b) observations are built from the asset's full stored `price_daily` history and
      `upsert_price_observations` rewrites a row only when a value differs. (c) `_reconcile_history`
      (`--store-daily`): a full re-fetch once when a recorded SPLIT postdates a bar stored before it, or a
      split-shaped seam jump matches a recorded split; a jump that survives or has no recorded split is
      refused, writing nothing for the ticker (`pricing_run_error.stage = 'split_jump'`), unless
      `--allow-split-jumps`. `quant.db.upsert_return_daily` follows the same update-on-change rule, so
      `quant build-returns` rebuilds a re-adjusted series. (d) `quant.universe` filters by the pinned
      `return_engine_version` / new `observation_engine_version` (PR #109 review). **Verified on production `financial.db` (read-only):**
      the detector finds exactly the 7 jumps (APH 1, MNST 6), each matching its recorded 2:1 split, and a
      full recompute fills all 10,500 NULL observation rows; the gateway was unreachable from this
      container, so the re-fetch was exercised against a stub — then verified live by the PR #109 review
      (real gateway, scratch copy of the DB: 09-29 bar healed, APH/MNST re-fetched in full, jumps 7 → 0,
      NULL vol-90d 21 → 0 per ticker, second run changed 0 rows). +50 tests (860 total), mutation-checked; ruff, format, mypy
      clean. Record: `docs/model_fixes.md` "T-131"; `SPEC.md` FR-004 and NR-007 (the two derived series
      are rewritten on a changed input), `docs/pricing_agent.md` updated.
      **Still open:** the production clean-up above (user direction only) and `verify_pilot.py`'s T-131
      checks, which run in the pilot. The first `pricing_agent --store-daily` run after this re-fetches
      every asset that has a split in its history, once each.

- [x] **T-132** *(P0)* Market capitalization. **Code done 2026-10-02 (PR #110, approved).**
      - (a) Point-in-time share count from the cover page
        (`dei:EntityCommonStockSharesOutstanding`): gateway (upstream) plus
        `fundamental_agent`. *(Upstream `portfolio-data-mining` `T-043`, PR #48, merged
        2026-10-02.)*
      - (b) Never use `CommonStockSharesIssued` as shares outstanding.
      - (c) One shared reader in `kg_schema`, used by `cycle` and `quant`: the latest
        point-in-time count times the **close at the as-of date**, refusing a count older
        than a set age.
      - (d) `quant` refuses a risk model, or records it explicitly, when any panel asset
        lacks a cap, never weighting it 0 silently. Record cap coverage and age on
        `quant_run`.

      **Why (system review N4)**: `statements.REGISTRY["shares_outstanding"]` falls back to
      `us-gaap_CommonStockSharesIssued`, which includes treasury stock (PG: $650B cap on
      4.009B issued vs SEC's 2.324B outstanding, about 1.7× too high; 139 stored filings have
      only "issued"). XOM, PM, NEE and HUM have no share concept stored; 101 of 359 sample
      filings (28%) have no cap, and 238 of the 258 that do use the diluted weighted average.
      `quant.db.load_market_caps` keeps the latest non-null cap with no age limit (PG's
      2024-03-31 value is used on 2026-09-29) while `cycle.data.market_cap_estimates` takes
      the latest filing even when NULL, so the two modules disagree. A missing cap is silently
      weighted 0 in the equilibrium market portfolio (`caps_by_id.get(a, 0.0)`): XOM gets 0
      and PG about 40% of the market weight in risk model 3. Caps are valued at the filing's
      period-end price, not the as-of price. `T-122`'s post-fix quant numbers rest on this
      vector, so they are not thesis-usable yet. *(Not the same as `T-127`, MCD's market-cap
      scale, already fixed by `T-103`.)*

      **Acceptance**: every sample name has a cap from a point-in-time count; PG within 5% of
      SEC's cover count × price; a test with one missing cap refuses; `verify_pilot.py` T-132
      passes.

      **Closure (2026-10-02)**: the cover count is stored in `filing_cover_shares`; the registry no
      longer reads `CommonStockSharesIssued`; `kg_schema.market_cap.market_caps_as_of` is the one
      reader `cycle` and `quant` call (latest usable cover count × the close on the date,
      split-basis adjusted, refusing a count older than 200 days or a close older than 10);
      `build-risk-model` refuses on a missing cap (`--allow-missing-caps` records and weights 0) and
      records coverage and age on `quant_run`; `METRICS_ENGINE_VERSION` → `metrics-v4`. Verified live
      on a scratch copy of production: 20 of 20 sample names have a cap (6 had none), PG = SEC's
      2,324,433,060 × the close, XOM's equilibrium return 1.99% → 9.98%; see `docs/model_fixes.md`.
      PR #110 review follow-ups: the cover failure is read from `data["cover"]["error"]`; BRK's class A is
      converted (`CLASS_CONVERSION`); `quant` counts a dual-listed issuer once (same CIK).
      **Still open:** production filings carry no cover counts until re-ingested (`run --fresh` /
      `T-100`; no backfill command was built), `verify_pilot.py`'s T-132 check (in the pilot), and
      a follow-up to retire quant's now-vestigial `--metrics-version` (market caps were the only
      `fundamental_metrics` it read).

- [x] **T-133** *(P1)* Quarterly cash flow and the NEGATIVE_FCF rule. **Code done 2026-10-02 (PR #111, approved).**
      - (a) On 10-Qs, derive the quarter's cash flows (YTD minus the prior quarter's YTD), or
        compute cash-flow margins on the TTM basis `T-105` already builds (TTM FCF / TTM
        revenue), consistently for every filing.
      - (b) NEGATIVE_FCF reads TTM FCF, not one quarter.
      - (c) Recognize oil & gas capex (ASC 932: `PaymentsToExploreAndDevelopOilAndGasProperties`,
        `PaymentsToAcquireOilAndGasProperty`) and a filer's custom capex concept through a
        cash-flow-statement-only label fallback. Today APA and PSX have no FCF on any 10-K
        (forensic audit F2; PSX uses `psx_CapitalExpendituresAndInvestments`).

      **Why (system review N5/N6)**: a 10-Q cash-flow statement reports only year-to-date
      columns and the cashflow group reads the quarter column, so FCF margin is missing for
      172 of 182 Q2/Q3 10-Qs (95%); the cashflow group, VALORIZATION's quality factor and
      NEGATIVE_FCF work only on Q1 10-Qs and 10-Ks. NEGATIVE_FCF (HARD) reads a single
      quarter: WAT is vetoed for Q1 2026 alone (FCF −$42M on revenue of $1,267M) while its
      TTM FCF is positive, and `T-125` makes the veto permanent.

      **Acceptance**: < 5% of Q2/Q3 FCF margins missing; APA and PSX 10-K FCF present; WAT's
      Q1 2026 no longer triggers the HARD veto while its TTM FCF is positive;
      `verify_pilot.py` T-133 checks pass.

      **Closure (2026-10-02)**: `cashflow.compute` measures a 10-Q over the trailing twelve months
      (`T-105`'s `ytd`/`quarters` flows only -- never `quarter x 4`, which would reproduce N6 for a
      filing whose year cannot be built) and a 10-K over its fiscal year, so `NEGATIVE_FCF`, which still
      reads `free_cash_flow_margin`, now reads a trailing year; `capital_expenditure` recognises oil & gas
      development and acquisition spend (ordered tiers), the "net" additions line and a single
      "capital expenditure(s)" cash-flow caption, and is absent rather than partial when a filer reports it
      in parts (NEE). Found while verifying: a first quarter the gateway tags `(Q2)` (Waters) had no
      year-to-date pair; `_is_first_quarter` now also reads the balance sheet. Folded into `metrics-v4`.
      Verified live (APA -2,740M, PSX -2,233M, EOG, FANG development not acquisitions) and by replaying a
      scratch copy of `financial-3.db` through the real TTM code: Q2/Q3 FCF margin missing **94.5% ->
      24.7%** on the 20-ticker sample, WAT's first quarter of 2026 **-3.3% -> +7.0%**; see
      `docs/model_fixes.md`.
      **Acceptance**: read as written ("< 5% missing") it is not reached -- 41 of the remaining 45 are
      five names with no recognised capex line (APO, WFC, HOOD: financials; ESS: a REIT; NEE: a utility),
      4 are the first quarters stored before any 10-K. The PR #111 review recalibrated the criterion to the
      population where an FCF is defined (companies with a 10-K FCF, 10-Qs after their first 10-K), where
      only the earliest quarters with no prior year remain, and treats it as met; `verify_pilot.py`'s
      T-133 check measures that population. PR #111 review also added an oil & gas filer's "other PP&E"
      line to its oil & gas capex (EOG capex -6,115M -> -6,594M, FCF -14%).
      **Still open:** utility and REIT capex (utility construction lines, split lines, "other PP&E"
      variants) -- recorded as scope of `T-071`, not a new task; `verify_pilot.py`'s T-133 checks (in the
      pilot); and the production re-run (`T-100`).
