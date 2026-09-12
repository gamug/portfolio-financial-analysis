# TASKS.md — `portfolio-financial-analysis`

Discrete, checkable task breakdown for `.specify/memory/PLAN.md`. Each task
references the plan work item and the `SPEC.md` section it closes. Check a
box only when its acceptance criterion (in `PLAN.md`) is actually met — not
when the code is merely written.

Task IDs are stable, same rule as `SPEC.md`'s `FR-0xx`/`NR-0xx`: don't
renumber; mark a cancelled/superseded task in place instead.

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

## Work item 3 — `quant`: a factor-aware μ estimator

- [ ] **T-020** Add a factor-based `ret_estimator` (`mu_i = rf + beta_i ·
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

## Status

Work item 1 (T-001–T-008) is **done** — landed via PR #32
(`refactor/engine-agnostic`), fully re-verified 2026-09-12 (quality gates,
`SPEC.md`, and both architecture artifacts all confirmed current). Nothing
in Work items 2–4 has started; all three are unblocked now that Work item 1
has landed (Work item 4 is independent of Work item 1 entirely and could
always start any time). T-021 (loading risk-free/benchmark data) is Work
item 3's own long pole — start it early within that work item since
T-020/T-023/T-024 depend on it.
