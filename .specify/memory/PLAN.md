# PLAN.md — `portfolio-financial-analysis`

The implementation plan for the live backlog identified in
`.specify/memory/SPEC.md`. Where the constitution is principles and
`SPEC.md` is the requirements/architecture contract, this document is the
"how, and in what order" for the work that contract still leaves open.

`SPEC.md` §13 (Open Questions & Risks) lists eight items; §14 (Scope
Boundaries) marks most as **accepted** (permanent characteristics of this
project at its current, non-production scope). Three items were genuinely
actionable without expanding scope beyond what's already in motion on this
repo's own branches — those are Work items 1–3 below, of which Work item 1
has since landed (see its own section). The rest stay exactly as §14
disposed of them (see Non-goals). **A 2026-09-08 production forensic audit
(see the Priority Override section immediately below) has since found
live correctness bugs that take precedence over this original ordering —
read that section before picking up any work item in this file.**

## 🔴 Priority Override — 2026-09-08 Production Forensic Audit

A direct forensic audit against the production database
(`data/financial.db`, 1.48 GB, coverage `2022-01-03 → 2026-08-27`) and this
repo's own `src/*` found **five systemic correctness failures already
contaminating live output** — not open design questions like the rest of
this document, but bugs actively corrupting the fundamental scores, the
veto lane, and the Markowitz benchmark today. Two of the fixes need an
additive, backward-compatible upstream schema/API change first (Work items
5–6); everything else is entirely within this repo (Work items 7–9).

**This overrides the priority order implied by the numbering below.**
Execute in this order: **Work item 10 (P0 — `T-085`; done 2026-09-20: the
pricing gateway is `quant`'s *only* corporate-actions source — data mining
belongs to `portfolio-data-mining` alone)** → **Work item 11 (P0, added
2026-09-21 — in this order: the `T-052` live check of the gateway dividends
(done 2026-09-21),
`T-086` the `build-returns` guard (done 2026-09-21), `T-092` the critical integration of the multi-filing
`sec_edgar` endpoints (done 2026-09-21),
`T-094` the F4 fiscal-calendar fix (done 2026-09-21), `T-087` the constitution amendment (done 2026-09-21), `T-090` metric-version selection and run manifests (done 2026-09-21),
`T-088` the malformed-data purge + 20-ticker deep validation run (**done 2026-09-22**;
its own acceptance surfaced three new findings — `T-095`/`T-096`/`T-097`, below —
**promoted above `T-089` at the user's explicit direction, 2026-09-22**), `T-095`
(**done 2026-09-22**), `T-096` (**done 2026-09-22**),
`T-097` (**done 2026-09-22** — see Work item 11), then, last in the fixing process, `T-089` the
architecture-artifact reconciliation)**, with Work item 5 ∥ Work item 6
(independent, external prerequisites; Work item 6's implementation now lives in
`portfolio-data-mining`) running in parallel → **Work item 7 (P0 — critical
correctness fixes, highest priority in this file; its remaining live run
`T-068` is re-scoped: Phase A runs first on the 20-ticker sample inside
`T-088`, and `T-068` keeps only the full-universe extension)** → **Work item 8 (P1 — methodological
redesign; supersedes Work item 3's approach in place)** → Work items 2/4
(as already planned, unaffected by the audit) → **Work item 9 (P2 —
cleanup)** → **the low-priority path: `T-093`** (user-tunable version constraints for
`quant`'s Markowitz runs — deferred on purpose, 2026-09-21; to be tackled late, not now).

The audit's own source documents (`feedback_plan.md`,
`upstream_data_mining.md`, `upstream_portfolio_common.md`) were reviewed in
full to write Work items 5–9 below, then **deleted per the auditor's own
instruction** — this plan and `TASKS.md` are now the only durable record of
their findings and verification queries; do not assume those filenames
still exist or try to re-open them.

**The five systemic failures** (full detail, file:line pointers, and
reproduction/expected-output numbers preserved in Work item 7 below):

1. **Corrupted data propagation** — a share-count/market-cap scaling bug
   (F1) and 40% of the universe (202/503 assets) missing dividends (Q3)
   directly contaminate the Markowitz benchmark's inputs.
2. **Inoperable portfolio protections** — a veto-cutoff bug (C1) caused
   `vetoed = 0` across all 503 assets in every `cycle_ranking` produced so
   far, letting HARD-vetoed names (e.g. Mastercard, an active
   `LEVERAGE_EXTREME` veto) into the live portfolio at a real weight.
3. **Accounting frequency mismatch** (F4) — 10-Q flow-over-stock ratios are
   never annualized, so the LLM scored large, profitable quarterly filers
   (e.g. JPMorgan) as structurally "weak" against thresholds calibrated on
   annual (10-K) figures.
4. **LLM-bypass** (F-N4) — the fundamental agent's own narrative explicitly
   flagged data errors in prose for at least 7 filings (verbatim quotes in
   Work item 8 below), but `cycle`/`quant` read only the raw numeric tables
   and ignored the warning entirely.
5. **Benchmark collapse** (Q1/Q4) — all three μ estimators collapse in
   practice (pure noise / one uniform constant across the whole panel /
   severe single-name distortion inherited from finding F1), so the
   efficient frontier exhibits zero genuine cross-sectional dispersion.

**SPEC.md cross-reference** (not amended in this pass — out of this pass's
explicit scope; flagged here for a follow-up SPEC.md correctness note once
Work item 7 lands): finding **C1** means FR-006's acceptance criterion ("a
veto row … does not exclude the asset … for date N itself, but does for
the next cycle run at N+1") is **currently violated in production** — the
T-1 filter excludes nothing at all, not even one cycle late as designed.

## Goal

Close the backlog items that are genuinely actionable right now:

1. ~~Land the `portfolio-common` engine-agnostic re-pin (`v1.0.0` →
   `v1.2.x`)~~ — SPEC.md §13 item 7. **Done**: landed 2026-09-05, PR #32
   (`refactor/engine-agnostic`, commit `8b1fd14`). — Work item 1.
2. Land the cross-module orchestrator so a report can request "everything
   as of D" with one invocation instead of five hand-sequenced commands —
   SPEC.md §13 item 3. — Work item 2.
3. Add a factor-aware expected-return (μ) estimator to `quant` so the
   return-aware objectives (`tangency`, `target_vol`, `frontier`) stop
   silently collapsing toward `min_var` — SPEC.md §13 item 1 (critical). —
   Work item 3.

Two items are tracked but explicitly **not** started by this plan because
they are cross-repo, not something this repo can finish alone (see the
Sequencing note): the SEMANTIC boundary cutover (§13 item 4,
`docs/semantic-score-boundary.md`) and a precise dividends/corporate-actions
series (§13 item 5). Work item 4 below covers the part of the SEMANTIC
cutover that *is* this repo's responsibility per the placement map in that
doc, so it can proceed in parallel without waiting on `portfolio-nlp`.

## Non-goals

Everything else in `SPEC.md` §13 stays exactly as §14 disposed of it —
**not** part of this plan:

- Item 2 — survivorship residuals (pre-2022 delisted-name price history,
  point-in-time EDGAR, an `as_of`-partitioned fact store): accepted at
  current scope; the root cause (point-in-time universe) is already fixed.
- Item 5's dividend-precision half beyond what Work items 6/7 now cover:
  **partially superseded** by the 2026-09-08 audit — the free,
  `yfinance`-backed gateway endpoint (Work item 6, built upstream in
  `portfolio-data-mining`) and the zero-cost
  10-Q-derived Level-1 dividend fix (Work item 7's Q3 task) were put in
  scope as P0, since both were found to be low-cost and already
  practically necessary. *(2026-09-20: the Level-1 derivation was then
  removed — Work item 10 / `T-085` makes the gateway `quant`'s only source,
  since mining data is `portfolio-data-mining`'s job alone.)* Still out of scope: a **paid vendor** total-return
  pull or a precise, split-adjusted historical corporate-actions series —
  that remains a separate, larger conversation about vendor cost/access,
  per SPEC.md §14.
- Item 6 — flat (non-`RuleClause`) veto rules: accepted; sufficient for the
  current rule set.
- Item 8 — no LLM synthesis accuracy measurement: accepted; the
  deterministic ratios and the rule-based fallback are the load-bearing
  correctness guarantee, not the narrative (constitution: AI behavior #1–2).

## Work item 1 — Adopt `portfolio-common` v1.2.x (engine-agnostic seam) — DONE

**Status: resolved 2026-09-05, PR #32** (`refactor/engine-agnostic`, commit
`8b1fd14`, merged as `13fcbf9`). Kept here, its approach struck through, only
so the item number stays stable and the record of how it was done isn't
lost — see `docs/portfolio-common-v1.2-engine-agnostic.md` for the full
account. **Do not re-open or re-plan this item**; treat any related future
work as a new, separately-scoped item instead.

**Why** (historical): `pyproject.toml` pinned `portfolio-common` at
`tag = "v1.0.0"` (DB-engine-only: `Database`/`in_clause`/`Allowlist`), but a
later release (`v1.2.x`) adds a `Dialect` seam, `Database.connect_url`, a
neutral `Row`/`DatabaseError`, and `table_columns`/`relation_exists`/
`relation_kind`/`relation_ddl`/`create_schema`/`ensure_columns` — the
primitives needed to get every non-test `import sqlite3` out of
`src/kg_schema/` (and the `sqlite3.OperationalError`/`sqlite3.Row` usages
scattered across `quant`, `fundamental_agent`, `pricing_agent`, `cycle`,
`api`).

~~**Approach**:~~

1. ~~Re-grep the current state of `refactor/engine-agnostic` against
   `master`/this branch's tip before resuming — the branch may predate
   recent work on `refactor/rewire-connect-call-sites` (the just-landed
   single `connect()`/`connect_ro()` factory in `kg_schema`), so rebase
   rather than assume it's current.~~
2. ~~Bump `[tool.uv.sources]`'s `portfolio-common` tag to the target `v1.2.x`
   release.~~
3. ~~Rewrite `src/kg_schema/db.py` (and any direct `sqlite3.Row` type hints
   elsewhere) to the neutral `Row`/`DatabaseError` types; replace
   `except sqlite3.OperationalError` "table absent in a partial DB" sites
   with `except DatabaseError`.~~
4. ~~Replace `PRAGMA table_info` with `db.table_columns`; replace
   `SELECT … FROM sqlite_master` probes (the m005/m006-style CHECK-widening
   guards) with `db.relation_exists`/`relation_ddl`; replace
   `executescript` calls with `db.create_schema`; replace
   `kg_schema._add_missing_columns`'s `PRAGMA`+`ALTER` loop with
   `db.ensure_columns`.~~
5. ~~Keep what's genuinely SQLite-flavoured as SQL text, not an engine
   import, per the `Dialect` seam: `ddl.py`'s dialect (`INTEGER PRIMARY
   KEY` rowid aliases, partial indexes, inline `CHECK … IN`), the
   `INSERT OR IGNORE`/`INSERT … ON CONFLICT … DO UPDATE SET … excluded.*`
   writes (→ `conn.dialect.insert_or_ignore`/`upsert`), and `json_extract`/
   `json_each` in the `v_*` view definitions (→ `conn.dialect.json_extract`).~~
6. ~~Run the full suite (`uv run pytest`, `ruff`, `mypy`, `pre-commit`) after
   each package's rewrite, not just at the end — this touches every
   package's `db.py`.~~

**Acceptance criteria** (all verified met, re-confirmed 2026-09-12):

- ✅ `grep -rn "import sqlite3" src/` (excluding tests) returns nothing (only
  a docstring mention in `src/kg_schema/__init__.py`).
- ✅ `pyproject.toml`'s `portfolio-common` source pins `tag = "v1.2.1"`, not a
  floating version.
- ✅ `uv run pytest` (203 passed), `ruff check`, `ruff format --check`, and
  `mypy` are all green.
- ✅ `SPEC.md` §13 item 7 already reflects the resolved state. Both
  architecture artifacts (Portfolio Thesis + Portfolio Financial Analysis)
  were re-read 2026-09-12 and already carry a dedicated section/pill
  documenting the `v1.2.1` re-pin in full — constitution AI behavior #11 is
  satisfied, nothing further to reconcile for this item.

## Work item 2 — Cross-module orchestrator

**Why**: `pricing_agent` → `fundamental_agent` → `entity_resolution` →
`cycle` → `quant` are sequenced by hand today (SPEC.md §13 item 3); a report
wanting "everything as of D" issues five separate commands with no shared
checkpoint or failure recovery across them, even though `cycle` itself is
already checkpointed internally (`cycle_run`/`cycle_checkpoint`).

**Approach**:

1. Design a thin top-level runner (a new package, e.g. `orchestrator/`, or a
   `python -m cycle run-all`-style entrypoint — decide which, consistent
   with constitution: Project structure #1's bar for a new top-level
   package) that takes one `--analysis-date` and sequences the five steps.
2. Reuse each package's own idempotency (FR-004/FR-001/FR-006/FR-013) rather
   than re-implementing skip logic — the orchestrator's job is sequencing
   and failure surfacing, not duplicating each package's resume state.
3. Record orchestrator-level provenance (which step ran, its own run-log
   row's `run_id`, start/end time, status) so a report can trace "was
   everything as of D actually rebuilt, and when" from one place — reuse the
   existing run-log `v_*` view pattern rather than inventing a new one.
4. Surface a per-step failure without aborting steps that don't depend on
   the failed one (`pricing_agent` and `fundamental_agent` are independent;
   `entity_resolution` doesn't depend on either) — only `cycle`/`quant`
   should hard-block on their real upstream dependencies.

**Acceptance criteria**:

- A single command runs pricing → fundamental → entity_resolution → cycle →
  quant for one `--analysis-date`, in the correct dependency order, and
  completes on a fresh universe with no pre-existing data.
- Killing the orchestrator mid-run and re-invoking it does not re-do a step
  that already completed and wrote its output (delegates to each package's
  own idempotency, per step 2 above).
- `SPEC.md` §13 item 3 updated to reflect the resolved state, and §2.2's
  "out of scope" line about hand-sequencing removed/updated to match. Also
  update the two architecture artifacts per constitution AI behavior #11.

## Work item 3 — `quant`: a factor-aware μ estimator — SUPERSEDED, see Work item 8

**Superseded 2026-09-12 by the forensic audit's Work item 8, step 6.** The
single-factor CAPM-style approach sketched below (`mu_i = rf + beta_i ·
ERP` against one market series) is replaced in place by a Carhart 4-factor
estimator with Vasicek beta shrinkage — kept here, struck through, only so
the item number/history stays stable per this repo's "mark superseded in
place, don't renumber" rule (`TASKS.md`'s own header rule). **Do not
implement the plan below** — implement Work item 8 step 6 instead; its
task is `T-077`, not `T-020`–`T-026` (those stay unchecked as a historical
record of the superseded plan, not open work).

*(Historical plan below, superseded — retained for record only.)*

**Why**: SPEC.md §13 item 1 (critical) — `equilibrium` is a safe default
precisely because `james_stein`/`hist_mean` over ~5 years of daily returns
have a standard error far larger than the true cross-sectional spread, so
they collapse toward `min_var` in practice. The return-aware objectives
(`tangency`, `target_vol`, `frontier`) need a μ input with real
cross-sectional signal to be meaningful, not just methodologically present.

**Approach**:

1. Add a factor-based estimator (`mu_i = rf + beta_i · ERP`, betas estimated
   from the existing `quant_return_daily` panel against a market/benchmark
   series) as a new `ret_estimator` choice alongside `equilibrium`/
   `james_stein`/`hist_mean` in `risk.py` — additive, not a replacement; the
   registry pattern `optimize.py`/`objective.py` already uses for objectives
   is the model to follow (one new estimator function, one new `--mu`
   choice).
2. Requires a real risk-free curve and a benchmark/index total-return series
   populated (`risk_free_rate`/`benchmark_series` — tables and CSV loaders
   already exist, unpopulated per SPEC.md §13 item 5) — load at least enough
   of these two to compute ERP over the `quant_return_daily` window before
   this estimator can run; loading a *precise, dividend-accurate* benchmark
   series is explicitly not blocking (§13 item 5's residual is a separate,
   larger conversation).
3. Keep `equilibrium` the default `--mu` choice; the new estimator is opt-in
   until it has at least one comparative `evaluate` run against the current
   default over the same as-of window.
4. Compare the new estimator's `optimize`/`evaluate` output (forward
   realized/active return, Sharpe) against the `equilibrium` baseline over
   the same historical window(s) before recommending a default change.

**Acceptance criteria**:

- A new `--mu` choice exists, is persisted the same way the other three are
  (`quant_expected_return`), and is covered by a unit test analogous to
  `tests/test_quant_lw.py`'s coverage of the covariance estimators.
- `tests/test_quant_gate.py`'s score-independence guarantee still holds (the
  new estimator reads only price/benchmark/rf data, never `score_snapshot`).
- A documented before/after comparison (forward realized return / Sharpe /
  frontier shape) exists for at least one as-of window, recorded in
  `docs/quant.md`.
- `SPEC.md` §13 item 1 updated with the dated result (resolved, or
  re-scoped if the comparison doesn't show an improvement — a legitimate
  outcome, not a required "success").

## Work item 4 — SEMANTIC boundary: this repo's half

**Why**: `docs/semantic-score-boundary.md`'s placement map assigns this
repo three pieces of the SEMANTIC cutover that don't depend on
`portfolio-nlp` shipping its aggregation stage first: the read adapter
shape, the `ticker` → `asset_id` resolution contract, and the `coverage` +
freshness-check extension. Building these now means the cutover isn't
blocked end-to-end on the other repo's timeline.

**Approach**:

1. Add a `KG_NLP_DB` config seam (mirroring `entity_resolution/news_db.py`'s
   `KG_NEWS_DB` pattern) — read-only, no schema assumptions beyond what the
   contract doc (`docs/semantic-score-boundary.md`'s "Contract to define"
   table) specifies once `portfolio-nlp` publishes it.
2. Implement `ticker` → `asset_id` resolution on this repo's side against
   the local `assets` table (option (c) in that doc's W2 — the recommended
   one), so identity resolution stays owned by the repo that owns `assets`.
3. Extend the `coverage` command with a SEMANTIC check (W14) and add a
   freshness check to `cycle`'s preflight (max processed date vs. the
   cycle's `--analysis-date` — warn/fail, mirroring the existing `coverage`
   gate's warn/`--strict` shape) so a stale or absent SEMANTIC feed is
   visible, not silently blended as zero.
4. Keep the blend weight at its current placeholder until `portfolio-nlp`
   ships a labelled/judged eval for its sentiment stage (the gate
   `docs/semantic-score-boundary.md`'s W4 already specifies) — this is a
   cross-repo decision, not something to flip unilaterally here.

**Acceptance criteria**:

- A `KG_NLP_DB`-backed read adapter exists, opened `mode=ro`, with a test
  fixture mirroring `entity_resolution`'s news-db test pattern.
- `coverage`/`cycle`'s preflight report SEMANTIC freshness/coverage the same
  way they already report EDGAR/pricing coverage.
- `SPEC.md` §13 item 4 updated to reflect this repo's half being ready,
  still flagged not-cut-over until `portfolio-nlp`'s side lands. Also update
  the two architecture artifacts per constitution AI behavior #11.

## Work item 5 — Upstream: `portfolio-common` v0.3.0 additive data contract (P0, external, blocking prerequisite)

**Why**: four fixes below (Work item 7's Ring-1 gates, Work item 8's
structured `forensic_flags`, and Work item 9's `v_quant_vs_live`/
entity-resolution cleanup) need new schema surface that must land in
`portfolio-common` first. All four changes are purely additive
(`CREATE TABLE IF NOT EXISTS`, a nullable `ADD COLUMN`, a view rebuild),
ship "anytime, concurrent" per `kg_schema/ddl.py`/`version.py`, and need
**no** `schema_version` bump (stays at 6). Current pin: `tag = "v1.2.1"`.
Target: **`v0.3.0`** (a minor bump — no non-additive migration involved
despite the version jump).

**Approach** (implement in `portfolio_common/kg_schema/ddl.py` +
`views.py`, exposed via the existing `ensure()`/`ensure_views()`):

1. **`data_quality_issue` table** (feeds Work item 7's Ring-1 gates):
   `id` PK, `filing_id`/`asset_id` FKs, `metric_name`, `rule_id`,
   `severity CHECK IN ('HARD','SOFT')`, `value` (the quarantined value,
   `NULL` if invalid), `created_at`, `run_id`,
   `UNIQUE(filing_id, metric_name, rule_id)`, plus indexes on `asset_id`
   and `(rule_id, severity)`.
2. **`score_snapshot.forensic_flags_json` column** (feeds Work item 8's
   structured flags): nullable `TEXT` (JSON object with
   `data_error_suspected`/`negative_equity_buyback`/
   `value_destroyer_sub_wacc`/`severe_sbc_dilution` booleans), added via
   the existing missing-columns mechanism; `UNIQUE(asset_id, score_type,
   event_time)` stays intact.
3. **`v_quant_vs_live` rewrite** (feeds Work item 9): the current view is a
   single `LEFT JOIN` anchored on `quant_portfolio`, so a live position
   held in `portfolio_position` but absent from every quant benchmark is
   dropped from the view **entirely**, not merely null-weighted — verified:
   MA and SNDK, 2 of 50 live positions, are completely missing from
   `v_quant_vs_live` today. Rewrite as the existing benchmark-anchored
   `LEFT JOIN` `UNION`ed with a live-only branch (`kind = 'LIVE_ONLY'`) for
   positions with no matching open `quant_position` (`UNION`, not `FULL
   OUTER JOIN`, for cross-SQLite-version compatibility); only
   `ensure_views()` needs to rerun, no migration required.
4. **`media_cooccurrence` table** (feeds Work item 9's entity-resolution
   cleanup): same shape/grain as `shared_executive_edge`
   (`asset_id_a`/`asset_id_b`/`person_name`/`method`/`weight`/
   `evidence_json`/`computed_at`/`run_id`,
   `UNIQUE(asset_id_a, asset_id_b, person_name, method)`) under a different
   table name, so press/analyst co-occurrences can be separated from
   genuine corporate-executive edges by table, not a boolean flag.

**Acceptance criteria** (structural, in `portfolio-common`'s own repo —
real-production-data counts are re-verified here in Work items 7/9 after
re-pinning, not asserted upstream):

- `ensure()` against an empty DB creates `data_quality_issue` and
  `media_cooccurrence` (with their indexes) and is a no-op on a second run.
- `PRAGMA table_info(score_snapshot)` lists `forensic_flags_json`.
- `ensure_views()` rebuilds `v_quant_vs_live` with a `LIVE_ONLY` kind
  present in a fixture that includes a live-only position; base tables
  unchanged.
- Tagged and released as `v0.3.0`.
- This repo's `pyproject.toml` (`[tool.uv.sources]`) bumped from `v1.2.1`
  → `v0.3.0`, `uv.lock` regenerated, `uv sync` run, and `ensure()`
  re-verified against the live `KG_FINANCIAL_DB` before any code in Work
  items 7–9 reads the new columns/tables.

## Work item 6 — Upstream: `portfolio-data-mining` corporate-actions endpoint (P0, external, blocking prerequisite) — implementation MOVED

**Why**: `src/quant/pricing_client.py::QuantPricingClient.actions` already
probes two request shapes for corporate actions (an `actions=true` query
flag, or a dedicated `/pricing/{ticker}/actions` route) and neither exists
on the gateway (`apps/pricing_api.py:127` in `portfolio-data-mining`
returns only OHLCV) — `probe()` always returns `False`, so `quant
backfill-actions` falls back to `corpact-v0-approx` (FY dividends spread
over 4 synthetic quarterly dates), leaving 40% of the universe (202/503
assets) with zero recorded dividends, despite the gateway already
depending on `yfinance` (`src/pricing/fetcher.py:22`), whose
`Ticker(t).dividends`/`.splits` return exact ex-dates and values for free.

**Moved 2026-09-19**: the endpoint itself is data acquisition, not
analysis, so its implementation is tracked in `portfolio-data-mining`
(`PLAN.md` Work item 3, `T-020`–`T-027`; PR
https://github.com/gamug/portfolio-data-mining/pull/30) and is **not
built in this repo**. Nothing under `src/` here changes for it —
`QuantPricingClient` is already the consumer. What stays here is the
contract that client depends on, and the verification:

- **Contract (unchanged)**: `GET /pricing/{ticker}/actions?start_date=&end_date=`
  (or `actions=true` on the existing route) returns `{"ticker", "dividends":
  [{"date": "YYYY-MM-DD", "value": float}], "splits": [...], "source":
  "yfinance"}` — `dividends[].value` is cash/share, `splits[].value` is a
  ratio (e.g. `4.0` for a 4:1 split). No corporate actions in range → empty
  lists, **never** a 404 (a 404 reads to the probe as "endpoint doesn't
  exist" and the client treats the route as absent).
- **Upstream decision (differs from this work item's original step 2)**: v1 is
  yfinance-only, not the candle endpoint's Finnhub-first/yfinance-fallback
  pattern — Finnhub's dividend/split endpoints are unverified as free-tier.
  The contract above is unaffected (`source` is `"yfinance"`).
- **Redeploy** of the `:8000` gateway is part of the upstream hand-off
  (`portfolio-data-mining` `T-026`), then `T-052` below runs from here.

**Acceptance criteria**:

- `curl "http://localhost:8000/pricing/XOM/actions?start_date=2022-01-01&end_date=2026-08-27&actions=true"`
  returns real XOM dividend history (a non-empty `dividends` list).
- From this repo: `QuantPricingClient(...).probe('XOM')` returns `True`
  (currently `False`).
- After `quant backfill-actions` (priority `corpact-v1`): `corporate_action`
  rows with `engine_version = 'corpact-v1'` exist for XOM/PG/T/NEE, and
  `quant_return_daily.cash_dividend` is `> 0` for those tickers (currently
  `$0.00` for all four — see Work item 7's Q3 finding).
- Sequencing: land after Work item 5, before Work item 7's dividend task —
  `backfill-actions` at priority `corpact-v1` only produces real data once
  this endpoint is live. *(2026-09-20: the consumer — the gateway as `quant`'s
  **only** dividend/split source, failing fast when it cannot serve — is Work
  item 10 / `T-085`; this section's acceptance criteria are what `T-052`
  verifies live.)*

## Work item 7 — P0: production data-integrity and correctness fixes (this repo, CRITICAL, highest priority)

**Why**: verified directly against `data/financial.db` (queries and
expected output preserved below since the source audit markdown is
deleted per instruction) — these are live bugs corrupting the fundamental
scores, the veto lane, and the quant benchmark today, not open design
questions.

**F1 — Share-count/market-cap scaling bug (Critical). RESOLVED 2026-09-14,
`T-060` — full record in `docs/model_fixes.md`'s F1 entry.**
`src/fundamental_agent/metrics/valuation.py::_share_count` multiplies raw
XBRL share counts by price with no unit-scale check. Verified: MCD market
cap stored as **$209,271** (should be ≈$209B) with
`free_cash_flow_yield = 31,882` (3,188,200%); WAT stored as **$37.2
trillion** (should be ≈$22B). The LLM's own narrative caught it verbatim:
*"a nonsensical market-cap FCF yield driven by a data error"* (MCD FY2025).
21 filings total have `|free_cash_flow_yield| > 50%` (18 positive, 3
negative). ~~**Fix**: add a unit-scale sanity check in `_share_count` (e.g.
cross-check against a plausible price × shares magnitude, or a declared
XBRL scale factor) before computing market cap.~~ **Correction (coherence
fix, 2026-09-14)**: this original framing assumed an XBRL `decimals`/`scale`
attribute existed to "apply" — it doesn't, anywhere in this repo's data
model (verified against real gateway payloads). The actual, shipped fix
instead cross-checks a filing's own reported share count against (1)
already-ingested history for an overlapping period, and (2) the same
filing's own `net_income ÷ as-filed diluted EPS` (no history needed) — full
root-cause correction and design rationale in `docs/model_fixes.md`.
**Acceptance** (re-verified against live data post-fix, see
`docs/model_fixes.md`): MCD's FY2025 10-K corrects to ≈$219B (was
$218,953.34); WAT's most recent 10-Q corrects to ≈$37.2B (was ≈$37.2T) —
note this is WAT's *current* filing, genuinely higher than the ≈$22B this
item's original estimate cited (that figure matches WAT's separate,
never-corrupted FY2025 10-K; WAT's real share count grew between the two
filings via the concurrent BD-Biosciences merger). The repo-wide "0 filings
with `|FCF yield| > 50%`" count was not re-verified in full (would need a
`--fresh` re-run across the universe, out of scope for a code-review pass —
see `docs/model_fixes.md`'s Verification section) and likely needs other
findings (F2, F4) too, not F1 alone.

**F2 — Revenue-concept resolution bug. RESOLVED 2026-09-15, `T-061` — full
record in `docs/model_fixes.md`'s F2 entry.**
`src/fundamental_agent/statements.py` picks a non-operating line item as
`revenue` for REITs (CPT, UDR, ESS, SBAC, …), dividing hundreds of millions
of net income by a recorded revenue of $3–5M. Verified: 124 filings with
`net_margin` outside `[-1, 1]` (CPT = 118.97); 54 filings with
`operating_cash_flow_margin` outside `[-1.5, 1.5]` (UDR = 163.3). LLM
narrative on CPT: *"the negative gross margin (-42.7%) is a REIT accounting
artifact from heavy depreciation rather than a true operational loss."*
~~**Fix**: correct the `revenue` XBRL-concept selection for REIT-classified
filers in `statements.py` to the actual top-line operating revenue
concept.~~ **Correction (coherence fix, 2026-09-15)**: this original
"REIT-classified filers" framing was too narrow — `Statements`/
`compute_group` carry no sector information at all, so a REIT-specific
special case was never actually possible, and live-DB verification found
the identical mechanism in non-REITs (`APO`, `WFC`, `HUM`, `HOOD`, `APA`).
The real, general mechanism: `Statements.get()` returns the first
document-order row matching any whitelisted revenue concept, with no
preference for an aggregate "Total revenues" tag over a smaller component
stream (ASC-606 contract revenue vs. ASC-842 lease income) when a filer
reports both. The shipped fix makes an explicit aggregate concept win
outright when tagged, and sums distinct component streams when no
aggregate exists — full root-cause correction and design rationale in
`docs/model_fixes.md`. **Acceptance** (re-verified against live data
post-fix, using the actual shipped code — see `docs/model_fixes.md`):
~~98 of 124 net_margin-outlier filings (79.0%)~~ **Correction (post-merge,
2026-09-15)**: a code-review bot caught two real double-counting gaps in the
`sum_components` path (mutually-exclusive ASC-606 tax-basis concept variants
being summed instead of treated as synonyms; a label-matched custom
"Total ..." extension concept being summed alongside real components) —
both confirmed live and fixed same-day. Re-verified count: **93 of 124
net_margin-outlier filings (75.0%)** and 46 of 54
operating_cash_flow_margin-outlier filings (85.2%, unaffected by the
correction) now resolve — a large majority, not "near-zero" as originally
claimed. The residual (mostly single-matching-concept filings, several of
them banks/brokers with revenue tagged via issuer-specific custom XBRL
extension concepts) is a distinct, larger investigation, explicitly
deferred, not silently dropped. Full record and the corrected verification
methodology: `docs/model_fixes.md`'s F2 entry.

**F4 — 10-Q flow/stock mismatch, never annualized (Accounting-critical).
RESOLVED 2026-09-15, `T-062` — full record in `docs/model_fixes.md`'s F4
entry.**
`metrics/profitability.py` (lines ~34-35) and `efficiency.py` (~26-29)
divide a 3-month quarterly flow (net income, revenue) by an instantaneous
balance-sheet stock (total assets, equity) with no annualization. Verified
(robust medians): `return_on_assets` 10-K median 6.01% vs. 10-Q median
1.56% (3.86×); `return_on_equity` 15.80% vs. 4.17% (3.79×); `asset_turnover`
0.5347× vs. 0.1395× (3.83×) — all converging on ≈4×, the expected quarterly
factor. LLM impact: JPM's 10-Q 0.42% ROA / 5.6% ROE read as *"structural to
the balance-sheet-heavy banking model"* when the TTM-annualized figure is a
strong 1.68% ROA. **Fix**: sum the trailing 4 quarters (TTM) for flow
numerators on a 10-Q filing where 4 prior quarters exist; fallback to ×4
when they don't. **Acceptance**: 10-Q vs. 10-K medians for
ROA/ROE/asset_turnover converge to within a small tolerance of each other
(not 3.8–3.9× apart) after the fix.

**C1 — T-1 veto cutoff bug: zero vetoes ever applied (Critical).
INVESTIGATED 2026-09-15, `T-063` — diagnosis corrected, no code fix; full
record in `docs/model_fixes.md`'s C1 entry.**
`src/cycle/orchestrator.py:264`'s `_rank()` computes `cutoff =
_t_minus_1(cycle_date)` and queries `hard_vetoed_as_of(cutoff)`, but vetoes
are written with `cycle_date` equal to the run's own date — so a query for
`<= cycle_date - 1 day` always returns empty. Verified: `cycle_ranking` has
**0 rows with `vetoed != 0`** out of 503, across every ranking produced so
far; Mastercard (MA), carrying an active HARD `LEVERAGE_EXTREME` veto,
ranks #13 and holds a **2.57%** live weight in `portfolio_position`. This
is a direct violation of SPEC.md FR-006's acceptance criterion (a veto
should exclude the vetoed asset starting the *next* cycle, not never).
**Fix**: correct the off-by-one/direction bug in the cutoff comparator (or
in how `veto.cycle_date` is stamped relative to the run it should first
apply to) so a HARD veto raised on cycle date N excludes the asset from
ranking on N+1, not from no date at all. **Acceptance**:
`SELECT COUNT(*) FROM cycle_ranking WHERE vetoed != 0` is `> 0` on the next
real cycle run following an active HARD veto; the vetoed name is excluded
from `portfolio_position`.

**C2 — Leverage-veto evasion via negative book equity (Conceptual).
RESOLVED 2026-09-15, `T-064` — full record in `docs/model_fixes.md`'s C2
entry.**
`src/cycle/rules/builtin.py:85-92`'s `LEVERAGE_EXTREME` rule tests
`debt_to_equity > 3.0`; firms with large buyback-driven negative book
equity (MCD, SBUX, PM, …) produce a *negative* ratio that trivially passes
the threshold (`-38.96 < 3.0`) and is then separately *rewarded* with a
high size/quality percentile in `scores/valorization.py`. Verified: MCD
`debt_to_equity = -38.96` (debt $39.8B, equity **-$1.02B**) evades the veto
and its `VALORIZATION` score inflates to 82.14 (partly a size-percentile
artifact of the still-uncorrected F1 market-cap bug). **Fix**:
special-case `equity <= 0` in the leverage rule (treat as maximally
leveraged, or gate on `debt_to_assets`/`interest_coverage` instead of
`debt_to_equity` when equity is non-positive) rather than letting a sign
flip evade the threshold; in `valorization.py`, don't let a
negative-equity name's ratio-derived quality percentile mask the leverage
risk (this also motivates Work item 8's ROE→ROIC substitution). **Acceptance**:
a fixture with negative book equity and high absolute debt triggers
`LEVERAGE_EXTREME` (HARD or SOFT per calibrated threshold), not a pass.

**Ring-1 deterministic data-quality gates (`DQ_*`) — needs Work item 5
(A1) first.** Add 7 threshold-based, zero-LLM-cost gates writing to the
new `data_quality_issue` table (quarantines a metric to `NULL` in the
consumption view + records the issue; HARD triggers a new `cycle`
data-quality veto, SOFT goes to a non-blocking review list). Calibrated,
verified counts against the current (pre-fix) DB — for regression-testing
the gate logic itself; expect these counts to shrink once F1/F2/F4 land,
but the gates should still exist as a permanent backstop against future
unknown errors:

| Rule | Trigger | Severity | Verified count |
|---|---|---|---|
| `DQ_FCF_YIELD` | `\|FCF yield\| > 0.50` | HARD | 21 filings |
| `DQ_MARGIN` | `\|net_margin\| > 5` | HARD | 50 filings |
| `DQ_MARGIN_REVIEW` | `\|net_margin\|` in `(1, 5]` | SOFT | 74 filings |
| `DQ_OCF_MARGIN` | `\|OCF margin\| > 3` | HARD | 39 filings |
| `DQ_MCAP_SCALE` | `market_cap / total_assets` outside `[0.001, 100]` | HARD | 11 filings |
| `DQ_NEG_EQUITY` | `equity <= 0` | invalidates D/E & ROE (NULL + flag); HARD veto only if also `debt_to_assets > 0.8` or `interest_coverage < 1.5` | 342 filings |
| `DQ_REVENUE_POS` | `revenue <= 0` (or NULL) with `net_income` present | HARD | 0 filings (protective, not currently triggered) |

**Fix location**: computation layer (`metrics/*`) or the `cycle` loader;
historical backfill is a pure deterministic recompute (minutes, $0 — no
LLM calls). A sector-level catch-all `DQ_SECTOR_Z` (`|robust intra-sector
z| > 6`) is a Work item 8 companion, once the robust-standardization
rewrite there exists. **Acceptance**: `data_quality_issue` populated with
the verified counts above (adjusted downward for whatever F1/F2/F4 fixes
already `NULL`'d); `cycle_ranking` reflects HARD `DQ_*` quarantines as an
active veto.

**Q3 — Dividend shortage: Level-1 quarterly derivation. RESOLVED
2026-09-15, `T-066` (Level-1/local half only) — full record in
`docs/model_fixes.md`'s Q3 entry. SUPERSEDED 2026-09-20 by Work item 10 /
`T-085`: the derivation below was removed from `src/` — dividends now come
only from the gateway, because no repository other than `portfolio-data-mining`
should mine data. The text is kept as the historical record.** (needs Work
item 6 for the final `corpact-v1` target, but this half is local-only and
zero-cost). `src/quant/actions.py`'s `corpact-v0-approx` divides annual
10-K DPS by 4 onto synthetic quarterly dates; verified: only 301/503
assets have any `corporate_action` row, and XOM/PG/T/NEE — all with 0 rows
— show `SUM(cash_dividend) = $0.00` in `quant_return_daily`. **Fix (Level
1, local, no upstream dependency)**: derive quarterly DPS from successive
10-Q YTD-dividend differences (`DPS_quarter(Qn) = DPS_YTD(Qn) -
DPS_YTD(Qn-1)`), anchored to each 10-Q's `period_end`, tagged
`engine_version = 'corpact-v1-derived'`; add engine-version priority in
`quant/db.py::load_actions` (`corpact-v2` > `corpact-v1` >
`corpact-v1-derived` > `corpact-v0-approx`). **Final target**: once Work
item 6's gateway endpoint is live, `backfill-actions --source gateway`
supersedes both with real `corpact-v1` ex-dates/values. **Acceptance**:
XOM/PG/T/NEE show `cash_dividend > 0` in `quant_return_daily` after the
Level-1 derivation (before the gateway endpoint even lands); `load_actions`
prefers `corpact-v1` over `corpact-v1-derived` over `corpact-v0-approx`
when more than one exists for the same asset.

**Q2 — Empty forward evaluation / date misalignment. RESOLVED 2026-09-15,
`T-067` (the `evaluate` half; the `frontier` half needed no code change) —
full record in `docs/model_fixes.md`'s Q2 entry.**
`quant_benchmark_performance` has **0 rows** and `quant_frontier_point` has
**0 rows** — verified `quant evaluate`'s `date_from`/`date_to` window
(`'2026-08-27'` → `'2026-09-03'`) starts exactly where `price_daily` ends
(`2026-08-27`), leaving no forward price data to evaluate against, and the
frontier objective was never invoked in the persisted run. **Fix**: align
`quant evaluate`'s default `--from` to a date with actual forward price
coverage relative to `--to`; ensure `optimize --objectives …,frontier` is
actually exercised end-to-end and persists `quant_frontier_point` rows.
**Acceptance**: `quant_benchmark_performance` and `quant_frontier_point`
both `> 0` rows after a corrected `evaluate`/`optimize` run.

**Sequencing note** (from the audit's own recommended re-run order —
preserve this since the source doc is deleted): run as **Phase A —
deterministic, $0, minutes-to-hours, no LLM calls**: F1/F2/F4 recompute →
Ring-1 `DQ_*` backfill + gateway dividends (Work item 10 / `T-085`, live check `T-052`) → re-run `cycle` (scores +
vetoes + ranking, exercising the C1 fix) → re-run the full `quant`
pipeline + `evaluate` (exercising the Q2 fix). This phase alone is enough
to produce a valid "iteration 2" that validates the entire deterministic
layer, independent of Work item 8's costly LLM re-run.

**Acceptance criteria (Work item 7, overall)**: every fix's per-finding
acceptance criterion above holds simultaneously against the live
`data/financial.db` after a Phase-A re-run; `uv run pytest`/`ruff`/`mypy`
green with new regression tests for F1/F2/F4/C1/C2 added under `tests/`.
**`T-069` AUDITED 2026-09-15** — each fix already shipped its own
regression coverage at fix time; full test-by-test inventory in
`TASKS.md`'s `T-069` entry. The live-DB Phase-A re-run itself remains
`T-068`, still open.

## Work item 8 — P1: methodological redesign (technical/valorization/fundamental scoring + μ estimator) — supersedes Work item 3's approach

**Why**: beyond the outright bugs in Work item 7, the audit found the
scoring *methodology* itself under-specified in ways that let those bugs
go undetected, and that limit the Markowitz benchmark's validity even once
the data is clean. **This supersedes Work item 3's originally-planned
single-factor CAPM-style μ estimator** (`mu_i = rf + beta_i · ERP` against
one market series) with the more rigorous spec in step 6 below — Work item
3's task numbering stays as the historical record per this repo's "mark
superseded in place, don't renumber" rule; its execution now happens here,
as task `T-077`.

**Approach**:

1. **Technical score V2** (`src/cycle/scores/technical.py`): keep 12-1
   momentum (Jegadeesh-Titman 1993 — skip the most recent 21 days to avoid
   short-term reversal noise), 90-day realized volatility, and 90-day max
   drawdown (asymmetric loss-clustering signal volatility alone misses),
   sector-Z-standardized (`(x - sector_mean) / sector_std`); weights
   `0.50·momentum + 0.30·vol⁻ + 0.20·drawdown`. Add three protective vetoes
   in `rules/builtin.py`: `BREAK_TREND_200` (`price < 0.95·SMA200` → SOFT,
   not HARD — a HARD veto here would purge half the index in any broad
   pullback), `VOLATILITY_SHOCK` (`σ5d/σ60d > 2.5` → HARD-**temporal**,
   auto-re-evaluated 10 trading days forward), `CRASH_Z_SCORE`
   (`(R5d - μ60d)/σ60d < -2.5` → HARD-temporal). Persist an explicit
   expiration timestamp on temporal vetoes in the `veto` table so re-entry
   semantics are defined, not implicit.
2. **Valorization redesign** (`src/cycle/scores/valorization.py`): replace
   book-to-market and `D/E` (both sign-broken under negative equity, see
   C2) with Enterprise-Value multiples — `enterprise_fcf_yield` (already
   computed) and a new `EBITDA/EV` factor (persist EBITDA in
   `fundamental_metrics`, computed as operating income + D&A, in
   `metrics/leverage.py`/`statements.py`); replace ROE with
   `return_on_invested_capital` (already computed) in the quality factor
   (ROE's sign inverts under negative equity, ROIC doesn't); remove the
   size factor (`neg_log_market_cap` — no size premium in large caps per
   Asness et al. 2018, and it was itself an F1-bug amplifier); add
   sector-specific factors for financials (P/tangible-book, P/E — EV is
   meaningless for banks/insurers whose debt is raw operating material)
   and utilities (regulated ROE, interest coverage); switch intra-sector
   standardization to a robust median/MAD z-score (`(x - median_s) /
   (1.4826 · MAD_s)`) instead of a mean/std z-score, so outliers (still
   possible even post-Work-item-7) don't dominate a sector's scale.
3. **Fundamental agent: continuous scoring rubric** (`agents.py`): the
   synthesis prompt currently asks for a bare `0-100` integer with no
   rubric, and the LLM anchors into 22 discrete round values (verified:
   4,844 scores, only 22 distinct values, top frequencies at 62/48/58/68/…)
   — replace with an explicit 4-pillar weighted rubric requiring one
   decimal place: `0.30·Profitability + 0.25·Solvency + 0.25·CashFlow +
   0.20·Moat`.
4. **Structured `forensic_flags` end-to-end** — needs Work item 5 (A2)
   first: extend `FundamentalAssessment`/the synthesis JSON schema with the
   4-boolean `forensic_flags` object (`data_error_suspected`,
   `negative_equity_buyback`, `value_destroyer_sub_wacc`,
   `severe_sbc_dilution`), write it to `score_snapshot.forensic_flags_json`,
   and have `cycle` consume `data_error_suspected = true` as an active
   data-quality veto — this is what closes the "LLM caught it, `cycle`/
   `quant` ignored it" architecture-bypass finding (verified: 7 filings —
   BKNG, META, KHC, CPT, MCD, PHM, LHX — where the saved narrative already
   says "data error"/"accounting artifact"/"nonsensical" in prose today).
   **Zero-cost immediate bridge**: before the full LLM re-run, regex-mine
   the existing 4,844 narratives for those same keywords and raise the
   data-quality veto from that match immediately (Ring 2 — $0, a SQL/regex
   scan, no LLM calls) rather than waiting on step 3+4's full re-run.
5. **Skills redesign** (`skills/*/SKILL.md`, `agents.py`): add a 10-Q
   frequency preamble (no skill currently mentions 10-Q vs. 10-K despite
   `form` reaching the prompt — this is the LLM-side companion to fix F4:
   "if this filing is a 10-Q, flow ratios are quarterly, don't score them
   against annual thresholds without annualizing"); tighten the valuation
   skill's magnitude gate (currently only `market_cap > 0`, which MCD's
   $209k and WAT's $37.2T both trivially pass — require
   `\|FCF yield\| > 50%` or a balance-sheet-scale mismatch to force
   `DATA_ERROR_SUSPECTED`); add a dedicated `profitability` skill (DuPont
   decomposition, annualization rule, ROE→ROIC fallback under
   `equity <= 0`) — currently profitability relies on 2 inline prompt
   lines while 5 other groups (cashflow, leverage, roic, cagr, valuation)
   already have a dedicated `skills/<name>/SKILL.md`; streamline
   each `SKILL.md`'s manual-extraction boilerplate (unused once ratios
   arrive precomputed in the automated pipeline) to cut per-filing token
   cost.
6. **`quant` μ estimator — Carhart 4-factor, supersedes Work item 3's
   single-factor plan**: `mu_i = rf + Σ_k β_i,k^shrunk · λ̄_k` over
   `{MKT, SMB, HML, MOM}`, with Vasicek shrinkage on each asset's OLS betas
   toward the cross-sectional mean beta for that factor (`w_i = 1 -
   Var(β̂_i,k) / (Var(β̂_i,k) + Var(β̄_k))`), betas estimated from
   `quant_return_daily` over a 756-trading-day (~3y) rolling window against
   a vendored, version-pinned Kenneth French factor CSV (no live-URL
   runtime dependency). Add as a new `ret_estimator` choice in `risk.py`,
   registered the same way `optimize.py`/`objective.py` already register
   objectives — additive, `equilibrium` stays the default until a
   comparative `evaluate` run justifies otherwise (Work item 3's original
   acceptance criteria on documenting a before/after comparison still
   apply, just against this richer estimator). Also enable `turnover_cap`
   in `optimize.py` and deduct a 10-15bps turnover cost in `evaluate.py` so
   the comparison isn't distorted by an unconstrained rebalance.
7. **Validation**: compute the cross-sectional Information Coefficient
   (`IC_t = Spearman(cycle_ranking.final_score_t, 21-trading-day-forward
   return)`) with Newey-West standard errors, targeting `mean(IC) > 0.03`
   at `t-stat > 2.0` and `IR = mean(IC)/std(IC) > 0.5`; confirm forward
   mean returns are monotonic across score quintiles; use a strict
   out-of-sample split (`2022-01-01→2024-12-31` calibration,
   `2025-01-01→2026-08-27` evaluation) rather than in-sample-only checks.

**Acceptance criteria**:

- Technical/valorization/fundamental changes each covered by a unit test
  fixture with the audit's own before/after numbers (e.g. MCD no longer
  gets a `debt_to_equity` veto-evasion pass; a 10-Q/10-K parity fixture for
  F4's annualization).
- `FundamentalAssessment`'s discrete-value count grows well past 22 on a
  fresh sample; `forensic_flags_json` populated end-to-end and consumed by
  a `cycle` data-quality veto in an integration test.
- New μ estimator covered analogously to `tests/test_quant_lw.py`'s
  covariance-estimator coverage; `tests/test_quant_gate.py`'s
  score-independence guarantee still holds.
- IC/quintile-monotonicity/out-of-sample validation numbers recorded in
  `docs/quant.md` and `docs/cycle.md` for at least one window.
- `SPEC.md` §13 item 1 (μ estimator) and §7 (business logic — technical/
  valorization formulas) updated to describe the shipped methodology, per
  this repo's own "spec/plan changes land with the requirement" rule —
  deferred to the PR(s) that actually implement this item, not done now.
- **One bundled LLM re-run** covers steps 3+4+5 together (~4,844 filings);
  do not re-run the full corpus once per prompt edit — batch prompt/skill
  changes into a single re-run per the audit's own cost-control note
  (marginal `forensic_flags` token cost inside that bundle is ≈$0).

## Work item 9 — P2: entity-resolution sanitization and `v_quant_vs_live` consumption (needs Work item 5's A3/A4 first)

**Why**: `entity_resolution`'s co-occurrence graph has severe false
positives — verified top entities by edge count are FX/commodity market
commentators (Tai Wong 45, Paul Bloxham 45, Gregory Faranello 45, Wang Tao
36, …), not corporate executives, plus incidental political figures (Rod
Blagojevich 10 edges, Jon Kyl 6) — because co-occurrence is derived with no
title or S&P-constituent-executive validation. Separately, `api`/
`kg_schema` should start consuming the corrected `v_quant_vs_live` from
Work item 5 (A3) once it exists.

**Approach**:

1. Require an explicit corporate title (CEO, CFO, Director, …) strictly
   linked to an S&P 500 constituent in `entity_resolution/cooccurrence.py`
   before recording a `shared_executive_edge`.
2. Add a denylist of known media/analyst/wire-service names (Reuters,
   Bloomberg, ForexLive, …) so market commentators quoted in passing don't
   generate edges.
3. Route anything that fails the corporate-title check but still reflects
   genuine media co-occurrence into the new `media_cooccurrence` table
   (Work item 5's A4) rather than dropping the signal outright — table
   separation, not a boolean flag, per the audit's own recommendation.
4. Update `api`/`kg_schema` read paths to consume the rewritten
   `v_quant_vs_live` (Work item 5's A3) so live-only positions (the MA/SNDK
   pattern) are no longer silently absent from the API surface.

**Blocked by missing data** (preserve this caveat since the source doc is
deleted): re-running `entity_resolution build` to actually re-derive the
graph needs `urls.db` (`KG_NEWS_DB`, ~4.5GB) transferred into this
environment — its default path points at a container location, not
anything under `data/`. Until then, steps 1–3 land as a code-level fix
verified against fixtures, not a re-graphed production `shared_executive_edge`.

**Acceptance criteria**:

- A fixture co-occurrence including a known media name (e.g. a Reuters
  byline) and a known politician produces zero `shared_executive_edge`
  rows for either, post-fix.
- `media_cooccurrence` receives the routed-away media/analyst signal
  instead of silently dropping it.
- `api`/`kg_schema` query `v_quant_vs_live` and correctly render a
  live-only position once Work item 5 (A3) is live — verified against the
  audit's own reconciliation query (`SELECT ticker FROM portfolio_position
  … WHERE NOT EXISTS (SELECT 1 FROM v_quant_vs_live …)` returns 0 rows).
- Production re-graph (not just the fixture-level fix) is explicitly
  tracked as blocked-pending-`urls.db`-transfer, not silently treated as
  done once the code fix lands.

## Work item 10 — P0: make the `portfolio-data-mining` corporate-actions gateway `quant`'s only source (this repo) — DONE 2026-09-20

**Scope (corrected 2026-09-20)**: the first version of this item made the gateway
the *primary* source and kept the XBRL-derived engines as a fallback. That was
wrong. **`portfolio-data-mining` is the only repository whose scope is mining
data; no other repository should host that kind of service.** Deriving dividends
from filings inside `quant` was data acquisition done in the wrong repo. So the
gateway is the **only** source and the derivation is **removed**, not kept.

**Why**: PR #45 (2026-09-19) moved the yfinance-backed endpoint's
*implementation* to `portfolio-data-mining` — built and merged there as its
PR #36 (`T-020`–`T-025`), awaiting only the operator's redeploy (`T-026`) — but
left this repo's *consumption* of it un-tasked. And `src/quant/actions.py` was
still shaped around a gateway that "may not serve actions":

1. **A second, in-repo source of the same data.** `derive_corporate_actions_from_facts`
   (`corpact-v0-approx`) and `derive_quarterly_dividends_from_10q_ytd`
   (`corpact-v1-derived`, `T-066`) mined dividends out of `financial_facts`, and the
   CLI defaulted to them (`--source derive`).
2. **One gateway failure aborted the run, and a dead gateway was slow.** Only the
   first asset was probed; a per-asset `GatewayError` was not in the
   `except (ActionsNotSupported, DatabaseError)` clause, so it propagated to
   `fail_run` and re-raised. Each dead call costs `max_retries` (3) × the 60 s
   timeout plus backoff, over a 503-asset universe — the same shape as the
   2026-09-19 `fundamental_agent` runs (`analysis_run` 5–9), which spent hours per
   run on gateway retries.
3. **An upstream failure looked like "no dividends".** Upstream's
   `get_corporate_actions` never raises: a yfinance failure yields empty lists plus
   a `warning` field (`portfolio-data-mining` `T-020`), which the client dropped.

**Approach**:

1. **Remove the derivation.** Delete `derive_corporate_actions_from_facts`,
   `derive_quarterly_dividends_from_10q_ytd`, their helpers and constants, the
   `--source` flag and the report's per-source fields. `quant.actions` no longer
   reads `financial_facts`. History is untouched (the table is append-only):
   rows written earlier under `corpact-v0-approx` / `corpact-v1-derived` stay, but
   `quant.db.load_actions` reads only gateway engines (`corpact-v2` > `corpact-v1`).
2. **Fail fast, never degrade.** A failed probe of the first asset, or the circuit
   breaker opening after `K` consecutive gateway *errors*
   (`QuantSettings.gateway_max_consecutive_failures`, default 3), raises
   `GatewayUnavailable`: `quant_run.status = 'failed'`, CLI exit 1. Only errors count —
   each has already paid `max_retries` × the timeout. Rows already written are kept,
   so a re-run resumes cheaply.
3. **An asset the gateway cannot serve gets no rows** and is listed in the report
   (a warning, an unsupported route, or one isolated error). The run completes and
   the CLI exits 1, because the dividend series would be incomplete. A warning
   means the gateway answered, so it resets the breaker's streak; a global yfinance
   outage (every ticker a fast warning) lists every asset without tripping it.
4. **Client.** `QuantPricingClient.actions` raises `ActionsUnavailable` on a non-null
   `warning` (upstream sets it only when yfinance failed), so `probe()` is `False`
   while yfinance is down and no caller can mistake an outage for an empty result; an
   error status or unparseable body becomes `GatewayError`. Dotted share classes are
   requested in yfinance's spelling (`BF.B` → `BF-B`): upstream's route only
   upper-cases the ticker and yfinance answers an unknown symbol with a clean empty
   result, so `BF.B` would otherwise read as a name that paid nothing.
5. **Report** `assets_seen` / `assets_fetched` / `assets_errored` and the first error
   messages in the summary and in `quant_run.params_json`.
6. **Pin the upstream contract** in a hermetic `httpx.MockTransport` test
   (`{ticker, start_date, end_date, source: "yfinance", dividends, splits, warning}`;
   illustrative values modelled on upstream's own live check), and pin the scope with
   a test that `quant.actions` neither reads `financial_facts` nor exposes a derive
   function.
7. **Docs/spec**: `docs/quant.md`, `README.md`, `SPEC.md` (§5 data flow, FR-009, §13
   item 5 and its §14 disposition), `docs/model_fixes.md`'s Q3 entry (superseded).

**Acceptance criteria**:

- Hermetic tests (`tests/test_quant_actions.py`, `tests/test_quant_pricing_client.py`)
  cover: no `--source` flag and no `source` parameter; `quant.actions` has no derive
  path; `load_actions` ignores the retired derived engines and prefers the newest
  gateway engine; a healthy run writes `corpact-v1` rows and is idempotent; an
  isolated gateway error and a `warning` each leave that asset without rows, list it,
  and let the run complete; warnings never open the breaker and a success resets the
  streak; a failed probe fails the run with nothing written and only the probe
  request made; the breaker fails the run after `K` consecutive errors, makes no
  further calls, and keeps earlier rows; the CLI exits 1 for an unavailable gateway
  and for any errored asset; the upstream contract parses; a dotted ticker is
  requested as `BF-B`.
- `uv run pytest`, `ruff check`, `ruff format --check` and `mypy` are green.
- **Not part of this task's box:** live verification. That is `T-052` — it needs
  upstream's `T-026` redeploy and must run against the deployed `PRICING_BASE_URL`
  (the gateway's `/pricing` mount), because upstream verified the client only against
  a local copy of the service.

**Consequences and residual risks** (deliberate, flagged for review):

- **No dividends until `T-052`.** *(Resolved 2026-09-21: `T-052` passed — 7,481
  gateway rows are in production `corporate_action`.)* With the derive path gone,
  `corporate_action` had no gateway rows and `quant` could not produce dividends
  until the endpoint was live. `build-returns` must follow a successful `backfill-actions`: it is
  `INSERT OR IGNORE` per `(asset, day, engine_version)`, so a series built with no
  dividends is price-only *and* locks in under `qret-v2`.
- **A single, unofficial source.** yfinance has no SLA and cannot tell an unknown
  symbol from a name that paid nothing, so a symbol it does not recognise stays at
  `$0` with no error — only `T-052`'s live check on real tickers (`BF.B` is the one
  dotted symbol in the current set) can catch that.
- **`v_corporate_action`** (the `kg_schema` read contract the knowledge-graph repo
  consumes) still resolves legacy derived rows; whether to hide the retired engines
  from that contract is a separate decision, not made here.
- **The constitution's "Executable cmds"** still lists
  `backfill-actions [--source derive|gateway]`; under its Governance section that
  needs its own reviewed change (PATCH), so it is not edited here.

**Sequencing**: the code has no external dependency and is done. `T-068`'s dividend
backfill and `quant` re-run wait for `T-052` (upstream's redeploy + the live check);
there is no `--source derive` escape hatch any more. `T-068`'s metrics-recompute and
`cycle` steps are not held up.

## Work item 11 — P0: follow-ups to the gateway-only cutover — guard, constitution, data purge + 20-ticker validation, artifacts

Added 2026-09-21, from review of `T-085`'s consequences. Order: `T-052` (live check,
Work item 6), `T-086`, `T-092`, `T-094`, `T-087` and `T-090` (all done 2026-09-21) → `T-088` →
`T-095`/`T-096`/`T-097` → `T-089`; `T-093` is **deferred to the low-priority path** (below).
`T-094` was found by `T-092`'s acceptance and had to land before `T-088`'s re-ingest. `T-088`'s backup
and purge (steps 1–2) were executed early, on 2026-09-21, at the user's direction.
`T-092` was a **P0 blocker** (below), now done. `T-090`/`T-093` were added the same
day, from review of `T-088`'s caveats. `T-090` sits before `T-088` because its deep validation
runs on the versioned readers and on the 10-Q data `T-092` fixes; `T-093` was deferred to the
low-priority path on 2026-09-21, and `T-088` needs only `T-090`'s `--metrics-version`, so the
deferral blocks nothing. `T-091` is superseded by `T-092`. **`T-088` is done, 2026-09-22**; its
own acceptance audit found `T-095`/`T-096`/`T-097`. **At the user's explicit direction
(2026-09-22), all three are now prioritized fixes, ordered ahead of `T-089`, which moves to the
very end of the fixing process** — its reconciliation pass now waits until `T-095`/`T-096`/`T-097`
are done, so it covers the whole process in one pass instead of needing a second delta.
**`T-095` is done, 2026-09-22** — the plausibility-floor fix, confirmed live against APA FY2021;
PM did not reproduce the defect on a live re-read, correcting this task's own earlier
misattribution (below). **`T-096` is done, 2026-09-22** — a precise reproduction of the live
`ttm_flows` logic found three distinct causes, correcting its own original tally: APO's gap is
one upstream defect cascading forward (routed, not fixed here — no local `portfolio-data-mining`
checkout to file it in), WAT's is a local `net_income`-concept gap (fixed), and the original
PG/BF.B/STZ "×1 each" claim did not survive reproduction (below). **`T-097` is done, 2026-09-22**
— `cycle select`'s "positions" step now refuses an out-of-order `--analysis-date` unless
`--allow-backdated` overrides it; scope corrected to `select` only (`monitor` never writes
`portfolio_position`, so it was never at risk despite the original title). **All three
prioritized findings are closed. `T-089` is next, and last, in the fixing process.**

**T-086 — Guard against false `build-returns` runs.** `quant_return_daily` is `INSERT OR
IGNORE` per `(asset, day, engine_version)`, so a series built while `corporate_action` has no
gateway rows is price-only *and* locks in under that version. `run_build_returns`
(`src/quant/returns.py`) must refuse — exit 1 with a clear message — unless a
`backfill-actions` `quant_run` covering the build window is `completed` with
`assets_errored == 0` (both already recorded in its `params_json` by `T-085`) and gateway
`corporate_action` rows exist. An explicit `--allow-no-dividends` override builds a knowingly
price-only series and records that in `params_json`. *Acceptance*: hermetic tests for no
backfill run, a failed run, a run with errored assets, a window the run does not cover, and the
override; `pytest`/`ruff`/`mypy` green. **Done 2026-09-21.** Any clean covering run is enough
(a later failed run, or one that completed with errors, does not undo the rows an earlier clean
one wrote); a run recorded before `T-085` has no `assets_errored` and does not count; a refusal
happens before `open_run`, so nothing is written and it is not recorded as a run.

**T-092 — CRITICAL: integrate `portfolio-data-mining`'s multi-filing `sec_edgar` endpoints.**
*What changed upstream.* `portfolio-data-mining` PR #39 ("return all filings for a form+year, not
just the first", merged 2026-09-21T16:32Z, live on the gateway) fixes the root cause `T-091`
found — the route used `next()` and silently dropped two of a company's three 10-Qs — and is a
**breaking change** with no API versioning:
- `GET /edgar/filing_by_year/{ticker}?form=&year=` — `data` is now a **list** of every match,
  most-recent-first, each `{form, filing_date, accession_number}` (was one object); "not found" is
  `{"success": true, "data": []}` (was `success: false`).
- `GET /edgar/financials/{ticker}?form=&year=&accession_number=` — the accession is optional; when
  several filings match and none is given it returns `{"success": false, "error": "Found 3 '10-Q'
  filings for 'XOM' in 2024; … pass the accession_number …  Available: …"}`. A single match
  (a 10-K) is unchanged.

*What it broke here* — verified 2026-09-21 by calling the live gateway through our own
`EdgarClient` (read-only, no DB): **no fundamental run can ingest anything.**
1. `filing_by_year` returns a list for *every* form, including 10-K. `pipeline._fetch_meta`
   calls `raw.get("filing_date")` on it → `AttributeError`; only `EdgarNotFoundError` is caught
   there, so every unit — 10-K and 10-Q — fails at the `process` stage.
2. `financials(…, "10-Q", year)` without an accession raises `EdgarError` ("unsuccessful
   payload: Found 3 '10-Q' filings …"), so 10-Qs fail even before (1).
3. The client's type hint (`-> dict`) and a `cast` hid the shape change from mypy.

*Approach.*
1. **`EdgarClient`** (`src/fundamental_agent/edgar_client.py`): `filing_by_year` returns the filing
   list (an empty list is valid; a pre-#39 object payload raises a clear `EdgarError` naming the
   old shape rather than being silently accepted); `financials(…, accession_number=None)`; the
   ambiguity reply becomes a distinct `EdgarAmbiguousError(EdgarError)` carrying the candidates.
2. **`pipeline.py`**: for each (ticker, form, year) list the filings and ingest **each** through
   its `accession_number`, oldest first. A filing row is stamped only with **its own** accession,
   filing date and reporting period — the payload's own latest period — never with a comparative
   column (`T-091` finding 2: 45 asset-accession pairs carried several fiscal periods). Comparatives
   stay facts. An empty list is a quiet skip, not an error (it replaces the old "No '10-K' filing
   found" payload error, e.g. APO FY2022). Where a form has one period per year (10-K) and several
   filings match (an amendment), take the most recent and log the rest.
3. **Ordering**: chronological within a ticker — 10-Ks first, then 10-Qs by year and, within a year,
   by period ascending (the gateway lists most-recent-first, so this must be re-sorted) — so
   `db.ttm_flows` finds the three prior quarters already recorded and F4 computes a true TTM
   instead of falling back to `current × 4`.
4. **Sections**: `_extract_sections` already takes the filing's accession; it now receives the
   right one per filing.
5. **Tests**: captured real payloads under `tests/fixtures/` (the 3-filing list, the ambiguity
   reply, a `financials` response by accession, an empty list, a Q2 payload carrying a Q1
   comparative such as STZ 2023); replace the tests and fixtures built on the old shapes
   (`tests/test_pipeline.py`, `tests/conftest.py`, and the `filing_date` fixtures).
6. **Not here**: repairing rows *already stored* with a borrowed accession (raw data) — that belongs
   to `T-088`'s purge/re-ingest scope, which this task may widen.

*Acceptance.* Hermetic: three 10-Qs in a year produce three `sec_filings` rows, each with its own
accession, filing date and period end; a comparative column produces no filing row; the three are
processed oldest-first; the fourth quarter's TTM uses recorded quarters (no `× 4` fallback); an empty
list is skipped without an error; a legacy object-shaped payload raises the clear error; the 10-K
path is unchanged; `pytest`/`ruff`/`mypy` green. **Live**, on a *scratch copy* of the DB with
`--tickers` (not production): XOM 2024 gains Q1, Q2 and Q3 rows with three distinct accessions; the
run reports no `AttributeError` and no ambiguity error; no (asset, accession) pair among the newly
ingested rows carries more than one fiscal period. *Order*: **P0, next in the row** — every
downstream task (`T-088`'s validation, the Phase A re-run) needs a pipeline that can ingest.

**T-092 — Done 2026-09-21.** Built as specified, plus: "Company not found" is now
`EdgarNotFoundError` so the ticker-spelling fallback works, and a resumed run skips the
`financials` call for an already-scored accession. 18 new hermetic tests on real captured payloads;
full suite 298; mutation-checked ten ways. **Live check** (real client, live gateway, scratch copy
of the DB, stub analyst): XOM and STZ, 10-K and 10-Q, 2022–2026 → 38 filings ingested, 0 failed;
XOM holds Q1–Q3 in every year; no (asset, accession) pair carries more than one fiscal period;
STZ's formerly shared accession is now two distinct filings. XOM's F4 TTM is real from 2023Q1.
**Finding for follow-up (not fixed here):** F4's `_quarter_flow` mismatches fiscal-year and quarter
labels for a **non-calendar** filer — `FY{y}` is keyed by the calendar year the fiscal year *ends*
in, `{y}Q1..Q3` by the calendar year each quarter ends in — so its Q4 derivation
`FY{y} − {y}Q1..Q3` reads the *next* fiscal year's quarters (verified on STZ: it would read a
−$1,199M net-income quarter from FY2025). It was invisible while only one 10-Q per year was stored;
ingesting Q1–Q3 makes it reachable, so a clean re-ingest (`T-088`) would activate it for STZ, BF.B
and every other non-December filer in the universe.

**T-094 — F4: TTM fiscal-calendar alignment for non-calendar filers.** *Found by `T-092`'s
acceptance (2026-09-21); recorded in `docs/model_fixes.md` (F4).*

*The defect.* F4 (`db.ttm_flows`, `db._quarter_flow`) computes a 10-Q's trailing-twelve-month flow from
the prior quarters already recorded, and derives a fiscal Q4 as the 10-K's FY flow minus that year's
three 10-Qs. It finds them by **label**: `FY{y}` and `{y}Q{n}`. The pipeline assigns those labels
from the calendar year the period *ends* in (`Period.year` is `int(date[:4])`) — `FY2024` for a year
ending Feb-2024, `2023Q1` for the quarter ending May-2023. The two agree only for a December
year-end. For STZ:

| fiscal year | 10-K label | its 10-Qs are labelled | `_quarter_flow(2024, 4)` reads |
|---|---|---|---|
| Mar-2023 … Feb-2024 | `FY2024` | `2023Q1`, `2023Q2`, `2023Q3` | `2024Q1`, `2024Q2`, `2024Q3` (**FY2025's**) |

Verified on real stored values: it would read `2024Q2` net income of **−$1,199M**, an FY2025
impairment quarter, as a quarter of FY2024. The `fiscal_year × 4 + quarter` index is also
non-monotonic in time for such a filer (2023Q3 → 2024Q4 → 2024Q1), so "the three preceding
quarters" are wrong even before Q4 comes into play. It stayed hidden because only about one 10-Q
per year was stored, so the quarters were never all present and the code fell back to `× 4`;
`T-092` stores all of them, and a clean re-ingest would activate it for STZ, BF.B and every other
non-December filer — AAPL (Sep), MSFT (Jun), NVDA (Jan), ORCL (May) and many more. XOM
(calendar) is correct: on a scratch re-ingest its TTM/quarter revenue is 3.2–4.7×.

*Approach.*
1. **Anchor on dates, not labels.** The prior quarters of a 10-Q are the asset's 10-Qs ordered by
   `period_end` strictly before it, roughly three months apart (reject a gap outside about 70–110
   days, which means a missing quarter); a Q4 flow is the 10-K's FY flow minus the three 10-Qs whose
   `period_end` falls inside that fiscal year (`(FYE − 1 year, FYE]`). Fall back to `× 4` only when
   a quarter is genuinely missing, and say so in the recorded inputs.
2. **Keep the stored labels** (`fiscal_period`, `FY{y}`) — relabelling changes unique keys and every
   consumer — but do no arithmetic on them.
3. **Cross-check** against the calendar-agnostic definition TTM = last FY + current YTD − prior-year
   YTD, which the current 10-Q payload (both YTD columns) plus the latest 10-K already carry; adopt
   it instead if the real-payload comparison favours it.
4. **Audit** every other consumer of the `FY{y}`/`{y}Q{n}` labels for the same assumption (growth,
   CAGR, `prior_of`, share-scale detection, sections) and record what is found.

*Acceptance.* Hermetic tests on real stored values, for a calendar filer (XOM) and non-calendar filers
(STZ year-end Feb, and one June or September filer): TTM equals the sum of the true trailing four
quarters; the STZ mix-up above cannot recur (a fiscal-year Q4 never reads another fiscal year's
quarters); the `× 4` fallback fires only when a quarter is truly absent. A live check on a scratch
copy for XOM, STZ and BF.B reports the share of 10-Qs on the fallback and the TTM/quarter ratios in a
sane band. A `docs/model_fixes.md` entry with the real-data verification (constitution AI behavior
#12); `pytest`/`ruff`/`mypy` green. *Consequence*: metrics computed under the old logic are wrong for
non-calendar filers, so the recompute is `T-088`'s re-run under `metrics-v2`. *Order*: next in the
row, before `T-088`'s re-ingest.

**T-094 — Done 2026-09-21.** Built as specified, with the quarter-sum method kept (the calendar-agnostic
FY + YTD − prior-YTD definition served as the independent check instead of a replacement, since the two
agree exactly and the quarter sum keeps the recorded-`inputs_json` contract). `db.ttm_flows` takes
`period_end` and locates quarters by date within a 20-day window, month-end preserving and form-matched;
a fiscal Q4 is the 10-K ending near the date minus the three 10-Qs 3/6/9 months before *its own* period
end. The audit found no other label arithmetic. **Real-data verification** (real client, live gateway,
scratch copy of the purged DB, stub analyst; XOM Dec / STZ Feb / BF.B Apr / MSFT Jun / AAPL Sep 52/53-week;
95 filings, 0 failed): 52 of 52 quarters agree with the independent definition to 0.00%; of 44 quarters
where the old code did not fall back to `× 4`, 33 read the wrong quarters (all non-calendar; STZ up to
409%, e.g. +$1,366.5M vs a correct −$442.3M), while XOM was right 11/11. 13 new hermetic tests, full suite
311, mutation-checked seven ways including the original bug. `docs/model_fixes.md` carries the F4
addendum. Residual: the pre-fix metrics were purged and are recomputed by `T-088`'s re-run under
`metrics-v2`.

**T-087 — Constitution amendment.** `.specify/memory/constitution.md` "Executable cmds" still
lists `backfill-actions [--source derive|gateway]`; the flag no longer exists. Per its
Governance section this is its own reviewed change: fix the line, bump the version PATCH
(1.2.0 → 1.2.1), update "Last Amended" and the amendment log, and re-scan the document for any
other claim that dividends are derived from filings.

**T-087 — Done 2026-09-21.** Constitution 1.2.0 → 1.2.1 (PATCH). `quant backfill-actions` no longer
lists `--source`; it and `build-returns` carry short factual annotations (the gateway is the only
source; `build-returns` refuses without a clean backfill, `T-086`). Every `python -m` line in the block
was re-checked against the real CLIs' `--help` — 17 of 18 already correct, the `--source` line the only
stale one; the rest of the document has no claim that dividends are derived from filings. No principle
changed.

**T-088 — Purge the malformed Fundamental/Quant data and run the 20-ticker deep validation.**
The stored derived data pre-dates the F1/F2/F4/C1/C2 fixes and the audit found it malformed
(e.g. CPT's 127× net margin from a mis-resolved revenue concept, 10-Q ratios ≈0.26× their 10-K
equivalents), and `fundamental_metrics` is `INSERT OR IGNORE` per engine version, so a re-run
could not replace it. Scope, decided 2026-09-21: **all 503 assets, derived data only**.
1. Fresh backup `financial.db.pre-t088-backup-<date>`; confirm it opens and row counts match.
2. One transaction, explicit table list, counts logged before and after. *Delete*:
   `fundamental_metrics`, `score_snapshot` (all types), `fundamental_snapshot_legacy`,
   `cycle_ranking`, `cycle_checkpoint`, `cycle_run`, `veto`, `portfolio_position`,
   `sector_aggregate_snapshot`, `quant_return_daily`, `quant_covariance`,
   `quant_expected_return`, `quant_risk_model`, `quant_position`, `quant_portfolio`,
   `quant_frontier_point`, `quant_benchmark_performance`, `quant_run`, and the retired derived
   `corporate_action` rows (`corpact-v0-approx`, `corpact-v1-derived`), children before parents.
   *Keep*: `assets`, `sectors`, the raw ingest (`sec_filings`, `financial_facts`,
   `sec_filing_section`), prices, universe tables, `benchmark_series`, `risk_free_rate`,
   `rule_catalog`, `schema_version`, `shared_executive_edge`, the run logs (`analysis_run*`,
   `pricing_run*`) and the gateway `corporate_action` rows from `T-052`.
3. Bump `METRICS_ENGINE_VERSION` to `metrics-v2` (`src/fundamental_agent/db.py`) with a test, so
   fixed-engine rows are distinguishable from any old copy. The readers are made
   version-aware by `T-090` (they filter on no `engine_version` today, so a parallel version
   would be read twice or won by row order; safe only because the purge leaves one version).
4. Run the validation on **20 tickers** (MCD, WAT, XOM, PG, T, NEE, MA, CPT, UDR, ESS, SBAC,
   APO, WFC, HUM, HOOD, APA, BF.B, PM, STZ, PSX): `fundamental_agent`/`pricing_agent` take
   `--tickers`; `cycle` and `quant` take `--universe-db`, so use a 20-member `universe.db`. Then
   Phase A: fundamental run → `cycle` → `quant backfill-actions` → `build-returns` (the `T-086`
   guard active) → risk model → `optimize` (including `frontier`) → `evaluate`.
5. *Acceptance*: re-run the data-quality audit on the 20 tickers — margins within plausible
   bounds, 10-Q ratios on the annual basis, `LEVERAGE_EXTREME` firing for negative-equity names,
   gateway dividends present in `quant_return_daily`.
**Steps 1–2 were executed early on 2026-09-21, at the user's direction:**
backup `financial.db.pre-t088-purge-backup-20260921` (identical counts on all 38 tables); one
transaction, foreign keys enforced, rollback on any surprise; **857,387 rows across 19 tables**
deleted, the other 19 tables unchanged, `foreign_key_check` clean, all 31 views still query. One
refinement of the list above: `quant_run` run 6 (the T-052 gateway backfill) was **kept** because the
`T-086` guard reads it; runs 1–5 (derive-era) were deleted. `cycle`, `quant` and the API's `v_*`
views are empty for all 503 assets until the re-run. Remaining: the `metrics-v2` bump (step 3, with
`T-090`), the 20-ticker re-ingest and Phase A (step 4), and the audit (step 5) — after `T-094`.

*Consequences*: the purge empties `cycle`, `quant` and the API's `v_*` views for all 503 assets
until a full re-run (`T-079`); it departs from the repo's append-only convention, so it is a
one-off, backed-up, transactional reset rather than a new command. *Validity caveat*: F4's true
TTM (`db.ttm_flows`) needs four consecutive quarters already recorded, and the data holds about
one 10-Q per fiscal year, so most 10-Qs take the `current × 4` fallback; `T-092` fixes the
cause. Record the remaining fallback fraction in the result. `T-092` may also change this
task's "keep the raw ingest" scope for the mis-attributed 10-Q rows.

**T-088 — Steps 3–5 done 2026-09-22.** Fresh backup `financial.db.pre-t088-rerun-backup-20260921`
(identical counts, `quick_check` ok) taken before touching anything. Repaired the 20 tickers' own
borrowed-accession rows ahead of the re-ingest (8 STZ `sec_filings` rows sharing an accession across
two fiscal periods, per `T-092`'s "already-stored mis-attributed rows are repaired under `T-088`" —
1,900 `financial_facts` and 14 `sec_filing_section` rows deleted with them, foreign keys enforced).
`METRICS_ENGINE_VERSION` bumped to `metrics-v2` (`bae8355`, 3 new tests, mutation-checked). Built a
20-member `universe.db` and ran Phase A: `fundamental_agent run --tickers …` (377 of 379 attempted
filings scored, 2 skipped, **1 failed** — WFC's 2023 10-Q, `0000072971-23-000142`: the gateway's
`/financials/WFC` payload extraction itself failed server-side, `'NoneType' object has no attribute
'to_dataframe'`; not retried) → `cycle select` (10 selected, 2 hard-vetoed, manifest `61a42091`) →
`quant backfill-actions`/`build-returns` (`qret-v2`, 23,340 rows, 18 of 20 assets with dividends;
the `T-086` guard passed) → `build-risk-model` (`rm-v1+9d34ff69`) → `optimize --max-name-weight 0.15`
(the 5% default box cap would have forced all three books to equal-weight on 20 names, testing
nothing; recorded as a run parameter) → 3 non-degenerate books (`min_var` 18 positions, `tangency` 7,
`target_vol` 13) plus a 15-point `frontier` → `evaluate` (a second, backdated `cycle`/`quant` chain
at `2026-06-30` gave it a forward window: 42 benchmark rows, 4 books, 164 performance rows against
`SP500_EW_INTERNAL`, real dispersion in daily active return, not a flat series).

*Acceptance, against the criteria above:*
- **Margins within plausible bounds**: 9 of 754 `net_margin`/`gross_margin` rows flagged
  (`|net_margin| > 1.0`, or `gross_margin` outside `[-0.5, 1.0]`). 6 are genuine — APO/HOOD real
  net losses on a small revenue base, CPT a real one-time joint-venture-acquisition gain
  (`cpt_GainOnAcquisitionOfUnconsolidatedJointVentureInterests`, $474M against $363M quarterly
  revenue). **3 are a new, distinct defect**: APA FY2021 and PM FY2021/FY2022 resolve `revenue` to
  a small sub-line (APA: $1,082M of a true ~$7,988M) because `LineItem.total_concepts` trusts
  `us-gaap_Revenues`/`us-gaap_RevenueFromContractWithCustomerIncludingAssessedTax` outright over
  every `concepts` candidate, and for these three filings that "total" concept is tagged on a
  sub-line, not the real aggregate. Not the CPT 127× bug (that was C1, already fixed) — recorded as
  **`T-095`** below, not fixed here (constitution AI behavior #12: a methodology fix needs its own
  real-data verification and a `docs/model_fixes.md` entry). The four audited REIT/tower names
  (CPT/ESS/UDR/SBAC) all fall inside `[-0.006, 1.375]`, no repeat of the 127× bug.
- **10-Q ratios on the annual basis**: `asset_turnover`, 10-Q vs. the same/prior fiscal year's 10-K,
  262 comparisons, median **1.00**, only 1.9% below 0.4 (vs. the ≈0.26× systemic ratio before F4).
  Confirms F4/`T-092`/`T-094` on real, purged-and-rebuilt data, not just the earlier scratch-copy check.
- **`LEVERAGE_EXTREME` on negative-equity names**: fires HARD for SBAC (`debt_to_equity` −2.75,
  `debt_to_assets` 1.08 > the 0.8 guard). Correctly does **not** fire for MCD, also negative-equity
  (`debt_to_equity` −38.97) but `debt_to_assets` 0.665 < 0.8 and `interest_coverage` 8.16 ≫ 1.5 — the
  C2 guard reads on real data exactly as designed, not merely absent for lack of a trigger.
- **Gateway dividends in `quant_return_daily`**: 18 of 20 tickers carry dividend days; WAT and HOOD
  carry none, correctly — neither pays a dividend.
- **F4 fallback fraction**: 72 of 278 10-Q filings (25.9%) still land on `current × 4`. The first
  three 10-Qs of every ticker's history account for 60 of those (expected — no prior quarters exist
  yet); the other 12, concentrated in APO (8) and PG/BF.B/STZ/WAT, are a **data gap in the stored
  filing set**, not F4 itself — recorded as **`T-096`** below.

*Found while validating, not part of T-088's own scope*: running `cycle select` for an *earlier*
`--analysis-date` after a later one silently overwrote the live `portfolio_position` book (closed
WFC's live position early, left a stray backdated `quant_portfolio` row) — `cycle` has no guard
against an out-of-order run mutating the live book. Reverted by hand (verified against
`portfolio_position`/`quant_portfolio` row counts and WFC's reopened position) before recording
these results; not itself part of the malformed data this task purged. Recorded as **`T-097`**.

*Residual, unchanged from before*: the purge still leaves `cycle`, `quant` and the API's `v_*` views
empty for the 483 assets outside the 20-ticker sample, closed by `T-079`'s full-universe re-run.

**T-095 — Revenue mis-resolution when a filer's own "total" tag is a sub-line, not the aggregate.
Done 2026-09-22** (branch `feat/t095-revenue-total-concepts-plausibility`). *Found by `T-088`'s
acceptance (2026-09-22).* `LineItem.total_concepts` (`statements.py`) let a match on
`us-gaap_Revenues` (or `…RevenuesNetOfInterestExpense`) win outright over every `concepts`
candidate, on the premise that a filer's own "Total revenue(s)" tag is authoritative.
**Confirmed live for APA FY2021** (read against the gateway, 2026-09-22, read-only): its
`us-gaap_Revenues` tag has **two** FY2021 rows valued identically at $1,082M — a non-dimensional
one (the one `total_concepts` matches) and a dimensional one
(`dimension_axis=us-gaap:EquityMethodInvestmentNonconsolidatedInvesteeAxis`, APA's equity-method
investee) — a filer-side tagging defect, not "Other, net" as first guessed. The real total sits in
`us-gaap_RevenueFromContractWithCustomerIncludingAssessedTax` ($7,988M, corroborated by
`apa_RevenuesAndOther`'s $7,928M "Total revenues and other") — net margin came out at 121% instead
of ≈16%. **PM did NOT reproduce** — read live for both FY2021 and FY2022, PM tags neither
`us-gaap_Revenues` nor `RevenuesNetOfInterestExpense` at all; `total_concepts` never fires, and the
`sum_components`/`synonym_groups` fallback already correctly picks the ExcludingAssessedTax figure
(the same rule F2 independently verified for PM). **This corrects the original finding's guess that
PM's case might be the synonym rule choosing wrong** — it wasn't; PM's resolution was correct all
along, no bug there. *Fix*: a new plausibility floor,
`Statements._TOTAL_PLAUSIBILITY_FLOOR = 0.5` — a `total_concepts` match under half the largest
first-matching-row value among `spec.concepts` is rejected, falling through to Tier 2; a filer with
no `concepts` rows to compare against (JPM) is unaffected, trusted exactly as before. *Acceptance*:
3 new hermetic tests on APA's real FY2021 values plus a parametrized pin of the exact floor boundary
(0.5 trusted / 0.49 rejected), mutation-checked (reverting the gate call fails the floor-boundary
assertions); every existing F2 regression test (including UDR's ratio-~1.0 "correct total" case and
the `jpm_10k`/`aapl_10k` fixtures) unchanged and green; `docs/model_fixes.md` T-095 entry
(constitution AI behavior #12, includes the PM correction); `uv run pytest -q` 375 passed (was 372);
`ruff`/`mypy` green. *Residual*: the 0.5 floor is a heuristic verified against one real defect (APA)
and one real correct case (UDR) — not swept across the rest of the 20-ticker sample or the full
503-asset universe; the corrected `fundamental_metrics` row for APA FY2021 is not re-persisted (needs
a production re-run, outside this change's authority).

**T-096 — 10-Q filing gaps beyond the expected "first three quarters" F4 fallback. Done
2026-09-22** (branch `feat/t096-10q-filing-gaps`). *Found by `T-088`'s acceptance (2026-09-22).*
Of 278 10-Q filings in the 20-ticker sample, 72 (25.9%) compute their flows via F4's
`current × 4` fallback; 60 are the unavoidable first three quarters of each ticker's stored
history. The original finding attributed the remaining 12 to APO (×8) and one each of
PG/BF.B/STZ/WAT. **A precise reproduction of the live logic itself** (`db.ttm_flows`/
`_quarter_flow_ending`/`_filing_near`/`_recorded_flow`, re-run against production, read-only,
2026-09-22 — not a re-check of which periods exist, which is what produced the original,
imprecise tally) found **three distinct causes, two of which correct the original framing**:

1. **APO — confirmed upstream, one gap cascading, not 8 independent ones.** Read live
   (`GET /financials/APO?form=10-Q&year=2023&accession_number=0001858681-23-000017`, the
   2023Q1 10-Q `filing_by_year` lists): `income_statement`/`cash_flow` carry only the
   `2022-12-31 (FY)` column, no quarterly duration period at all, while `balance_sheet`
   correctly carries `2023-03-31`. `_targets` finds nothing quarterly and the filing is never
   inserted into `sec_filings` at all — not an error, not a skip. Every one of APO's other 7
   flagged filings traces back to *this same gap*: the fiscal-Q4-derivation branch of
   `_quarter_flow_ending` needs 2023Q1 and cannot find it, cascading forward until (2025Q1) a
   full three years of complete quarters no longer reach back across that date. **Decision:
   upstream** (`portfolio-data-mining`'s `/financials` extraction for this one accession) — no
   local checkout of that repo exists in this workspace to file the task directly, so the
   reproduction above is recorded in full here instead.
2. **WAT — corrected: not a missing 10-Q, a `net_income`-concept registry gap. Fixed locally.**
   Re-read live for 2022–2026: every fiscal quarter *is* present; `filing_by_year` lists exactly
   3 10-Qs every year, matching `sec_filings`. "Missing Q1" was a **gateway period-tag labeling
   inconsistency**: the same real quarter (filed each May) is tagged `"(Q2)"` in WAT's
   2022/2023 filings and `"(Q1)"` in 2024/2025 — confirmed even *within one payload*, where the
   current period is tagged `"(Q1)"` but the identical relative prior-year comparative is
   tagged `"(Q2)"`. `_targets` correctly trusts the gateway's own tag rather than deriving one
   itself (F4/T-094's date-not-label lesson) — no bug there. **The real defect, found while
   reproducing**: `REGISTRY["net_income"]` (`us-gaap_NetIncomeLoss`/`us-gaap_ProfitLoss` only)
   silently resolves to `None` for **14 of WAT's 18 `metrics-v2` filings** (confirmed exclusive
   to WAT in the 20-ticker sample) — WAT tags neither concept for most of its history, only
   `us-gaap_NetIncomeLossAvailableToCommonStockholdersBasic` (live-verified: WAT's real
   2022-10-01 10-Q, $155,998,000; switches to the plain `NetIncomeLoss` tag only from 2025Q2,
   never co-tagging both). Missing `net_income` poisons `net_margin`/ROA/ROE directly and, via
   `ttm_flows`, later quarters' TTM windows too — the actual mechanism, not a filing gap. **Fix**:
   `us-gaap_NetIncomeLossAvailableToCommonStockholdersBasic` added as a third `net_income`
   concept.
3. **PG/BF.B/STZ — corrected: no extra gap.** All three have complete quarterly coverage every
   fiscal year; the precise simulation's only fallback beyond "first 3" for them is the
   fiscal-year-boundary Q4-derivation needing a quarter that predates each ticker's own stored
   history — the same *class* of unavoidable gap the "first 3" rule already covers, one filing
   deeper. The original "×1 each" tally does not survive this reproduction and is corrected here
   rather than left standing.

*Fix*: see WAT above (the only local code change). *Acceptance*: 2 new hermetic tests (WAT's
real value resolving via the new concept; a defensive regression pinning `NetIncomeLoss`'s
precedence when both are present), mutation-checked; confirmed via a live query that exactly 0
tickers other than WAT are affected within the sample; `docs/model_fixes.md` T-096 entry
(constitution AI behavior #12, includes all three findings and both corrections); `uv run
pytest -q` 377 passed (was 375); `ruff`/`mypy` green. *Residual*: APO's gap recorded, not filed
upstream (no local checkout); the `AvailableToCommonStockholdersBasic` document-order risk is
unverified beyond WAT; not swept for other registry items or the wider universe. **Prioritized
above `T-089` at the user's explicit direction, 2026-09-22** — second of the three.

**T-097 — Guard `cycle select` against an out-of-order (backdated) run mutating the live
book. Done 2026-09-22** (branch `feat/t097-cycle-out-of-order-guard`). *Found while validating
`T-088` (2026-09-22).* `cycle select --analysis-date D` always writes the live `portfolio_position`
book via `sync_positions`'s open/close logic with no check that `D` is not older than the book's
current `valid_from`. Running `select` for an earlier date after a later one silently closed a
live position early (`valid_to` set to the earlier date) and left a stray backdated
`quant_portfolio(kind='live_book')` row, exactly the kind of false, unnoticed corruption `T-086`
was built to prevent for `build-returns`. **Scope corrected**: `sync_positions` is called only
from the "positions" step, which `_SELECTION_STEPS` includes but `_MONITORING_STEPS` explicitly
excludes — `monitor` never writes `portfolio_position` at all, so it was never actually at risk;
the original title's "select/monitor" is corrected to `select` alone. *Fix*:
`cycle.writers.out_of_order_reason(conn, cycle_date) -> str | None` (a pure read of
`MAX(valid_from)` across every `portfolio_position` row, open or closed — a closed position's
`valid_from` still marks a date the book has already moved past) and `OutOfOrderCycle`, mirroring
`quant.actions.dividends_not_ready_reason`/`DividendsNotReady` (T-086) exactly:
`sync_positions` itself is untouched; `orchestrator.py`'s `_positions()` step checks the reason
before calling it, raising unless `CycleSettings.allow_backdated_positions` is set, and records
a bypass on `CycleReport.backdated_guard_bypassed` (mirrors `ReturnsReport.
dividends_guard_bypassed`) for the CLI to warn about. `--allow-backdated` was added only to
`select`'s argparse subparser (not `monitor`'s, where it would be a silent no-op);
`OutOfOrderCycle` joins `main()`'s existing generic exception-to-exit-1 handler. *Acceptance*: 4
new hermetic tests — the pure function (no rows/newer/same-date all safe, older date names both
dates and the override flag), a full `run_selection` refusing an older date with the book and the
refused run's own `cycle_run` left untouched (not partially applied), and the override succeeding
with the bypass recorded, plus confirming a closed position's `valid_from` still protects a
further attempt; mutation-checked. `uv run pytest -q` 380 passed (was 377); `ruff`/`mypy` green;
`docs/model_fixes.md` T-097 entry (constitution AI behavior #12). *Residual*: `cycle backfill` has
no override flag of its own and would hit the same refusal with no way past it if it ever ran
against a book with a later `valid_from`; not exercised by the original incident, not built here.
**Prioritized above `T-089` at the user's explicit direction, 2026-09-22** — third of the three,
and the last of the prioritized findings — `T-089` is next, and last, in the fixing process.

**T-090 — Metric-version selection and run manifests ("version of versions") for `cycle` and
`quant`.** *Problem.* `fundamental_metrics` is append-only per `engine_version`, so parallel
versions accumulate, but nothing chooses among them: `cycle/data.py` (both metric reads) and
`quant/db.py::load_market_caps` join it with no `engine_version` filter, so a second version is
read twice or won by row order. A run also cannot say which input versions it used, and
`quant`'s outputs are not keyed by them, so re-running over a new metrics version no-ops or
collides — the user cannot run `quant` several times against different versions and compare.
`T-088` step 3 planned only a minimal "make the three readers engine-aware"; this task
supersedes it. *Design.*
1. **One resolver**, in `kg_schema` (which `cycle` and `quant` may both import; they may not
   import each other): a requested selection → a concrete `engine_version` **per metric
   group** — profitability, liquidity, leverage, efficiency, growth, cashflow, roic, cagr,
   valuation. Default is the latest version present for each group; a per-group override is
   allowed (e.g. `valuation=metrics-v1,profitability=metrics-v2`); an absent requested
   version is an error, never a silent fallback.
2. **Every reader goes through it** — the three known readers, and any `v_*` view that resolves
   "latest" on its own — with a test that fails on any raw `fundamental_metrics` read in `src/`
   that bypasses the resolver, so a new reader cannot reintroduce the problem.
3. **Run manifest** — the resolved input versions (metrics per group, the `corpact`, return and
   risk-model engines) recorded in `cycle_run`/`quant_run.params_json` with a short hash, and
   carried by the outputs (`quant_risk_model`, `quant_portfolio`, …) so runs at different
   manifests write **parallel** books instead of colliding, and `evaluate` can compare them.
4. **CLI**: `--metrics-version` on the `cycle` and `quant` subcommands (default latest), printing
   the manifest it resolved.
*Settled 2026-09-21*: the change is **additive** — the scope is to let the system run different
version constraints on the quant agent from user input (`T-093`), not to rebuild tables. The
manifest hash is folded into the existing `*_version` key columns (parallel rows fall out of the
unique keys that already exist), with the full manifest in an additive nullable `manifest_json`
column in `kg_schema`'s additive DDL; `T-090` checks this against each `quant_*` table's actual
unique key, and a table whose key cannot express it additively is **raised**, not migrated.
*Still to confirm*: the ordering rule for version strings, without relying on `computed_at` —
recommended: parse `^(?P<family>.+)-v(?P<n>\d+)$`, order by an explicit family rank
(`pre` < `metrics`) and then by `n`, so `pre-v1` < `metrics-v1` < `metrics-v2`; an
unparseable string is an error, not a guess. *Acceptance*: hermetic tests
with `metrics-v1` and `metrics-v2` rows coexisting for the same filing — the default reads
`v2` only, an explicit `v1` reads `v1` only, a per-group mix works, an absent version errors,
the manifest is recorded and hashed deterministically, two `quant` runs at different manifests
coexist and neither no-ops the other, and the no-bypass test; `pytest`/`ruff`/`mypy` green.
*Not in scope*: changing the default (latest) behaviour of the `v_*` read contract the
knowledge-graph repo consumes.


**T-090 — Done 2026-09-21.** Built as specified, additive throughout. `kg_schema/versions.py` holds the
pure resolver (SQL in `queries.metric_versions_present`); every `fundamental_metrics` reader applies the
same **static** `VERSION_FILTER_SQL` with one JSON parameter (no interpolation, constitution Code & Git
#10), guarded by a test that fails on any unfiltered read in `src/`. `quant`'s manifest is the `valuation`
metric version + the return engine (what it actually reads); its tag is folded into
`quant_risk_model.model_version` and `quant_portfolio.engine_version` — the keys those tables were already
unique on — so different inputs write parallel rows and the same inputs update in place, with only an
additive nullable `manifest_json` added (and exposed on the two views). `cycle` records its manifest and
**refuses** to resume a `(type, date)` run built on a different one (checked before `open_cycle`), because
forking its date-keyed outputs would need non-additive key changes. `--metrics-version` on both CLIs.
57 new tests (368 total), mutation-checked twelve ways, and a live smoke on a scratch copy of the production
DB (additive columns grafted on, 31/31 views query, clean exit-1 failures with no run rows).
*Decisions made in the build, for review:* the ordering rule is the recommended one, adopted but not
user-confirmed; every run is tagged (so `rm-v1`/`opt-v1` become `rm-v1+<tag>`/`opt-v1+<tag>`); one explicit
version applies to every group that has rows while `GROUP=VERSION` is strict. *Finding, not fixed:*
`market_cap_estimates`/`load_market_caps` take no as-of date, so a historical run can read a market cap from
a filing dated after its `as_of`.
**T-093 — User-tunable version constraints for the `quant` agent** *(feature; builds on `T-090`;
**DEFERRED 2026-09-21 to the low-priority path** — to be tackled late, after Work item 9; nothing in
Work items 7–11 depends on it. The design below is kept as written)*.
`T-090` gives `cycle`/`quant` a resolver and a run manifest; this task lets the **user** steer it,
so the quant agent can be run under different version constraints and the results compared.
1. **Constraints.** Per input — each metric group, and the `corpact`, return and risk-model
   engines — a constraint is `latest` (the default), exact (`=metrics-v1`), a minimum
   (`>=metrics-v2`), an exclusion (`!=metrics-v1`), or a comma-separated combination
   (`>=metrics-v1,!=pre-v1`). Comparison uses `T-090`'s ordering rule.
2. **Where they come from.** Repeated `--version-constraint GROUP=EXPR` flags (`--metrics-version
   EXPR` sets every metric group) and/or `--version-profile FILE`, a TOML file of named, reusable
   profiles selected by name; flags win over the file.
3. **Resolution is strict.** Each input must resolve to exactly one present version — the highest
   that satisfies its constraint. If none does, the error lists the versions present and why each
   was rejected; there is no silent fallback to a different version.
4. **Tuning aids.** `quant versions` lists, per input, the versions present with row counts and
   first/last `computed_at`, so the user can see what there is to constrain; `--dry-run` on
   `build-risk-model`/`optimize` prints the resolved manifest and writes nothing.
5. **Recorded.** The constraints as given, their resolution and the profile name (if any) go into
   the `T-090` run manifest, so a run is reproducible and runs under different constraint sets are
   comparable in `evaluate`.
*Additive only*: no schema change beyond `T-090`'s additive DDL. *Acceptance*: hermetic tests for
each operator and for combinations; a per-group mix; an unsatisfiable constraint erroring with the
candidate list; profile-file parsing and flag-over-file precedence; `quant versions` output;
`--dry-run` writing nothing; and two runs under different constraints coexisting without either
no-opping the other; `pytest`/`ruff`/`mypy` green. *Not in scope*: constraint flags on `cycle`
(`T-090` covers version selection there).

**T-091 — Fix 10-Q ingestion. SUPERSEDED 2026-09-21 by `T-092`**: `portfolio-data-mining`
fixed the route (its PR #39, "return all filings for a form+year") while this task was in
review, so approach (b) below is done upstream and (a)/(c) are absorbed by `T-092`. The record
of the root cause is kept unchanged. *Findings (2026-09-21, read-only, against the DB and the
live gateway).*
1. **One 10-Q per fiscal year.** The pipeline makes one
   `GET /edgar/edgar/financials/{ticker}?form=10-Q&year=Y` per (ticker, form, year)
   (`fundamental_agent/pipeline.py::_fetch_financials`, `_targets`). The route accepts only
   `form` and `year` (gateway OpenAPI) and returns **one** filing: XOM 2024 → its Q3 filing
   only (`0000034088-24-000068`; columns Q3 and YTD for 2024/2023), STZ 2023 → its Q2 filing.
   Q1/Q2 10-Qs are unreachable through it. In the DB 2,424 of 2,471 (asset, fiscal-year) pairs
   hold a single 10-Q; for the 20 validation tickers the 10-Qs split Q3 70 / Q2 24 / Q1 10 /
   Q4 1.
2. **Comparative columns become filings with a borrowed accession.** `_targets` turns every
   quarter column of the payload (for the task's year) into a `sec_filings` row stamped with the
   one filing's accession and date, so 45 (asset, accession) pairs carry several fiscal
   periods (STZ 2022Q1/Q2 = `0000016918-22-000181`; ALLE, DAL, PNR, REGN, TT, NOC …) and are
   scored separately — STZ 2024's two rows scored 48 and 32 on the same filing.
3. **Consequence.** F4's true TTM (`db.ttm_flows`) needs four consecutive recorded quarters, so
   most 10-Qs fall back to `current × 4`.
*Approach.* (a) **Consumer fix, no upstream needed**: store a filing row only for the payload's
own reporting period (the one `filing_by_year` identifies); comparative columns stay facts and
never become a filing with a borrowed accession; repair the mis-attributed rows. (b)
**Per-quarter access, upstream**: `portfolio-data-mining`'s `sec_edgar` service needs
`financials` by `quarter` or `accession` (its `filings/{ticker}?form=10-Q` route already lists
accessions). Data acquisition is that repo's job, so the route is tracked and built there; this
repo then enumerates each fiscal year's 10-Qs and ingests Q1–Q3 through `EdgarClient`. An
upstream task **has not yet been filed**. (c) **Evaluate an alternative that needs no extra
filing** — TTM = last FY + current YTD − prior-year YTD, which the current 10-Q payload
(both YTD columns) plus the latest ingested 10-K already supply; verify it against real payloads
before adopting it. *Acceptance*: for the 20 validation tickers every fiscal year with filed
10-Qs has its Q1, Q2 and Q3 rows, each with its own accession, filing date and period end; no
(asset, accession) pair carries more than one fiscal period; the share of 10-Q metric rows on
the `× 4` fallback is reported and is near zero wherever four quarters exist; hermetic tests
on captured payloads (a Q3-only payload like XOM 2024, a Q2 payload with a Q1 comparative like
STZ 2023) added under `tests/fixtures/`. *Order*: before `T-088`; if the upstream half slips,
(a) and (c) still improve F4 and `T-088` may proceed with the fallback fraction recorded — the
user's call.
**T-089 — Reconcile the two architecture artifacts.** *Moved to the very end of the fixing
process, at the user's explicit direction (2026-09-22)*: it was next after `T-088`; now
`T-095`/`T-096`/`T-097` are prioritized ahead of it and it runs last, once all three are done.
Constitution AI behavior #11 requires it
at the close of every development effort, and no task covered the audit-era fixes. Update both
the system-wide [Portfolio Thesis](https://claude.ai/code/artifact/d3865a63-2894-4e20-b38a-7e50cf0d4040)
and the repository-specific [Portfolio Financial Analysis](https://claude.ai/code/artifact/bfc6efde-aecd-4408-83b8-081bc3abccb0)
artifact: **content only — never rename either or change its `<title>`**. Cover F1, F2, F4,
C1 (diagnosis correction), C2, Q2, Q3 (superseded), `T-085` (the gateway as the only
corporate-actions source) and `T-086`/`T-092`/`T-090`/`T-088`/`T-095`/`T-096`/`T-097`
(`T-093` is deferred, so it appears only as a
low-priority plan step); close the gaps and plan steps
they built, and correct prose that describes a fixed gap. Read the live artifact first and
republish in place by URL. A first pass can run any time after the `T-085` PR merges; the final,
comprehensive pass runs last, after `T-095`/`T-096`/`T-097` close — one pass covering the whole
fixing process rather than a second delta.

## Sequencing

Work item 1 (`portfolio-common` re-pin) touched every package's `db.py` and
needed to land **before** Work items 2/3 started new code in those same
files — rebasing a new orchestrator or a new `quant` estimator on top of a
mid-flight engine-agnostic rewrite would have doubled the merge-conflict
surface. It has already landed (PR #32), so that precondition is satisfied.

Work item 2 (orchestrator) and Work item 3 (μ estimator) are independent of
each other now that Work item 1 has landed — no ordering dependency between
them.

Work item 4 (SEMANTIC boundary, this repo's half) is independent of Work
items 1–3 and can proceed in parallel at any time; it does not touch
`kg_schema`'s connection layer or `quant`.

**Work items 5–9 (2026-09-08 forensic audit) override all of the above for
execution priority — see the Priority Override section near the top of
this document.** Their internal sequencing:

- **Work item 10 (`T-085`, consume the corporate-actions endpoint) is done
  (2026-09-20).** **Work item 11 follows immediately (2026-09-21):** `T-052`
  (live check — done 2026-09-21) → `T-086` (guard — done 2026-09-21) → `T-092` (critical: multi-filing
  `sec_edgar` integration — done 2026-09-21) → `T-094` (F4 fiscal-calendar fix — done 2026-09-21) → `T-087` (constitution — done 2026-09-21) → `T-090`
  (metric-version selection + run manifests — done 2026-09-21) → `T-088` (purge +
  20-ticker validation — **done 2026-09-22**) → `T-095` (**done 2026-09-22**) → `T-096`
  (**done 2026-09-22**) → `T-097` (**done 2026-09-22**) → `T-089`
  (artifacts; its first pass may run any time after `T-085` merges, but the final,
  comprehensive pass — covering `T-088`/`T-095`/`T-096`/`T-097` together — runs last).
  Work item 7's `T-068` is re-scoped behind
  `T-088`. `T-088`'s own acceptance audit found three further findings — `T-095`
  (revenue mis-resolution on a filer's own "total" tag), `T-096` (10-Q filing
  gaps beyond F4's expected fallback) and `T-097` (a `cycle select` guard
  against out-of-order runs, scope corrected from "select/monitor"). **At the user's explicit
  direction (2026-09-22), all
  three are prioritized ahead of `T-089`, which moves to the very end of the fixing
  process** — it no longer runs immediately after `T-088`. **`T-095` is done**: confirmed
  live for APA FY2021 (a plausibility floor now rejects a `total_concepts` tag mistagged
  on a small dimensional slice); PM did not reproduce the defect, correcting this task's
  own earlier misattribution. **`T-096` is done**: a precise reproduction of the live TTM
  logic found APO's gap is one upstream defect cascading (routed, not fixed here), WAT's is
  a local `net_income`-concept gap (fixed), and the original PG/BF.B/STZ tally did not
  survive reproduction. **`T-097` is done**: `select`'s "positions" step now refuses an
  out-of-order `--analysis-date` unless `--allow-backdated` overrides it; `monitor` was never
  at risk (it never writes `portfolio_position`). **All three prioritized findings are closed —
  `T-089` is next, and last.**
- Work item 5 (`portfolio-common` v0.3.0) and Work item 6
  (`portfolio-data-mining` corporate-actions endpoint — implemented there
  as its `PLAN.md` Work item 3, consumed here by Work item 10, verified
  here by `T-052`) are independent external prerequisites — develop
  concurrently.
- Work item 7 (P0 critical fixes) is the top priority in this repo. Its
  F1/F2/F4/C1/C2 fixes have no external dependency and should land first
  within it; its Ring-1 `DQ_*` gates need Work item 5 (A1); its dividend
  fix's Level-1 half was local-only but has since been removed — dividends
  now come only from the gateway (Work item 10), verified live by `T-052`.
  Follow the audit's own **Phase A → Phase B → Phase A-downstream**
  re-run order (documented in Work item 7's own Sequencing note) once the
  code fixes land — recomputation is $0/deterministic and can run
  immediately; the Work item 8 LLM re-run is costly and must be bundled
  into one pass, not repeated per prompt edit.
- Work item 8 (P1 methodological redesign, supersedes Work item 3) should
  follow Work item 7 in this repo, since its `forensic_flags` step needs
  Work item 5 (A2) and its "one bundled LLM re-run" should happen only
  once Work item 7's deterministic fixes (F1/F2/F4) are already in the
  data the LLM re-scores — re-running the LLM before the numeric bugs are
  fixed would just re-score the same corrupted ratios.
- Work item 9 (P2 cleanup) needs Work item 5's A3 (`v_quant_vs_live`) and
  A4 (`media_cooccurrence`); it has no ordering dependency on Work items
  7/8 and its `entity_resolution` half is additionally blocked on the
  `urls.db` transfer noted in its own section, independent of any other
  work item here.
- Work items 2 (orchestrator) and 4 (SEMANTIC boundary) are unaffected by
  the audit and keep their original priority — after Work items 5–9, per
  the Priority Override section.
- **Low-priority path (2026-09-21):** `T-093` (user-tunable version constraints for `quant`'s
  Markowitz runs) is deferred behind everything above, at the user's direction — "tackle this
  late, not this time". It keeps its Work item 11 home and ID (IDs are stable) but has no place
  in the current order; `T-088` and `T-089` do not wait on it (`T-090`'s `--metrics-version`
  already gives `T-088` the version selection it needs).

See `TASKS.md` for the discrete, checkable task breakdown.
