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
open design work. (Closed Work items 1, 3, 6, 7, 10 and 11 are in `CHANGELOG.md`.)
**Work item 13 (`T-102`/`T-103`, P0)** fixes the data defects `T-065`'s gates found. Work
item 5 continues in parallel (now local, see its note) → **8 (P1, supersedes Work item
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
- [ ] **T-041** Add nullable `score_snapshot.forensic_flags_json` column
      via the existing missing-columns mechanism. → step 2.
- [ ] **T-042** Rewrite `v_quant_vs_live` as the existing benchmark-side
      `LEFT JOIN` `UNION`ed with a `kind='LIVE_ONLY'` branch for live
      positions absent from every quant benchmark. → step 3.
- [ ] **T-043** Add `media_cooccurrence` table (same shape/grain as
      `shared_executive_edge`, different table). → step 4.
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

## Work item 13 — P0: data defects surfaced by the Ring-1 gates

Added 2026-09-25 by `T-065`: its verification on a copy of production found two live data
defects the gates now quarantine and veto, but do not fix. Each is a methodology change
(constitution AI behavior #12: verified, cited, recorded in `docs/model_fixes.md`). →
`PLAN.md` Work item 13.

- [ ] **T-102** Resolve NEE's revenue: its income statement reports
      `us-gaap_RegulatedAndUnregulatedOperatingRevenue` ("OPERATING REVENUES", a utility
      tag), which `statements.REGISTRY["revenue"]` does not list, so revenue is NULL in all
      19 NEE filings and every revenue-denominated ratio with it; `DQ_REVENUE_POS` HARD-vetoes
      NEE until this lands. Decide how the tag joins the registry (total vs. component,
      against `T-095`'s plausibility floor), check other utilities for it. **Acceptance**:
      NEE's revenue resolves to the reported operating revenues; a `quality` re-gate of the
      new metrics version clears `DQ_REVENUE_POS` for NEE.
- [ ] **T-103** Fix F1's residual on MCD FY2023–2025Q2: seven consecutive filings store
      `shares` in millions (`732.3` … `717.6`), so market cap is ~10⁻⁶ of the real value
      (`DQ_MCAP_SCALE` + `DQ_FCF_YIELD`). F1's overlapping-history anchor is itself
      mis-scaled inside such a run, and its EPS corroboration only covers
      `diluted_shares`. **Acceptance**: those filings' market cap within the gate's range
      under a new metrics version, F1's existing tests unchanged, and a `quality` re-gate
      clears them.

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

**🔴 Current top priority (2026-09-08 forensic audit): Work items 5, 8, 9 and 13.**
Work item 7 is closed (2026-09-25, `T-065` last) — see `CHANGELOG.md`. `T-040` (Work
item 5) is done; `T-041`–`T-084` have not started. Execute **Work item 13, `T-102`/`T-103`
(P0 — the defects `T-065`'s gates found)**, with Work item 5 (now local) in parallel →
**Work item 8, `T-070`–`T-079` (P1, `T-078` deprecated — `T-074` needs
`T-041`; run only after Work item 7's F1/F2/F4 fixes so the one bundled LLM
re-run scores already-corrected ratios)** → **Work item 9, `T-080`–`T-084`
(P2 — `T-082` needs `T-043`, `T-083` needs `T-042`; the production
`entity_resolution` re-graph is additionally blocked on a `urls.db`
transfer independent of any task here)**.

Work items 1 (done), 3 (superseded by `T-077` — do not implement), 6 (done
upstream + `T-052`), 7 (done 2026-09-25), 10 (done) and 11 (done 2026-09-25) are closed — see
`CHANGELOG.md`.

Work items 2 and 4 are unaffected by the audit and keep their original,
lower priority (after Work items 5–9 above): nothing in either has
started; both are unblocked.

**Work item 12 (`T-100`, the full-universe production run) runs last of all**, after
every other task in this file — current and future.
