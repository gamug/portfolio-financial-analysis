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
disposed of them (see Non-goals).

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
- Item 5's dividend-precision half (a vendor total-return pull or gateway
  corporate-actions extension) beyond the read adapter Work item 4 needs:
  not planned here — a separate, larger conversation about vendor cost/
  access, per SPEC.md §14.
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

## Work item 3 — `quant`: a factor-aware μ estimator

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

See `TASKS.md` for the discrete, checkable task breakdown.
