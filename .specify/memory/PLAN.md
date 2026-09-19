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
Execute in this order: Work item 5 ∥ Work item 6 (independent, external
prerequisites; Work item 6's implementation now lives in
`portfolio-data-mining`) → **Work item 7 (P0 — critical correctness fixes,
highest priority in this file)** → **Work item 8 (P1 — methodological
redesign; supersedes Work item 3's approach in place)** → Work items 2/4
(as already planned, unaffected by the audit) → **Work item 9 (P2 —
cleanup)**.

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
  10-Q-derived Level-1 dividend fix (Work item 7's Q3 task) are now in
  scope as P0, since both were found to be low-cost and already
  practically necessary. Still out of scope: a **paid vendor** total-return
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
  exist" and it keeps falling back to the derive path).
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
  `backfill-actions --source gateway` at priority `corpact-v1` only
  produces real data once this endpoint is live.

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
`docs/model_fixes.md`'s Q3 entry.** (needs Work
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
Ring-1 `DQ_*` backfill + Q3 Level-1 dividends → re-run `cycle` (scores +
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

- Work item 5 (`portfolio-common` v0.3.0) and Work item 6
  (`portfolio-data-mining` corporate-actions endpoint — implemented there
  as its `PLAN.md` Work item 3, verified here by `T-052`) are independent
  external prerequisites — develop concurrently.
- Work item 7 (P0 critical fixes) is the top priority in this repo. Its
  F1/F2/F4/C1/C2 fixes have no external dependency and should land first
  within it; its Ring-1 `DQ_*` gates need Work item 5 (A1); its dividend
  fix's Level-1 half is local-only, its final target needs Work item 6.
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

See `TASKS.md` for the discrete, checkable task breakdown.
