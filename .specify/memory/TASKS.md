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

**🔴 Priority override (2026-09-08 forensic audit)**: Work items 8 and 9 below
(`T-070`–`T-084`) are the current top priority — a direct audit against
production data (`data/financial.db`) found live correctness bugs, not
open design work. (Closed Work items 1, 3, 5, 6, 7, 10, 11, 13, 14, 15 and 16 are in
`CHANGELOG.md`.) **Work item 14 (P0/P1, the second audit's live defects) is done
2026-09-30** — its blockers on Work item 8, `T-105` (corrects metrics the LLM
re-run/`T-079` consumes) and `T-113`, are both closed. **This does not mean Work
item 8 starts next (PR #104 review):** the priority order is **Work item 17
(`T-131` ✅ done, PR #109; `T-132` ✅ code done, PR #PR → `T-133`) → the pilot (`docs/md primera revision/pilot_rerun_plan.md`,
`verify_pilot.py` at 0 FAIL) → Work item 8** — **do not start `T-079`'s LLM re-run until
all of it has landed.** Once
they have, priority runs **8 (P1, supersedes Work item
3/`T-020`–`T-026`)** → Work items 2/4 (unaffected, original priority) →
**9 (P2)** → **Work item 12 (`T-100`), the full-universe run, last of all** (which
itself needs a *fresh* `financial.db` — see `T-100`'s own entry). See `PLAN.md`'s
"🔴 Priority Override" section for the full rationale — the source audit markdowns
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

## Work item 8 — P1: methodological redesign (supersedes Work item 3)

- [ ] **T-070** Technical score V2 in `src/cycle/scores/technical.py`
      (12-1 momentum, 90d realized vol, 90d max drawdown, sector-Z
      standardization, `0.50/0.30/0.20` weights) + `BREAK_TREND_200` (SOFT),
      `VOLATILITY_SHOCK`/`CRASH_Z_SCORE` (HARD-temporal, 10-trading-day
      re-evaluation, expiration persisted on `veto`) in
      `rules/builtin.py`. → `PLAN.md` Work item 8, step 1. *(2026-09-29 system review, scope
      addition: recalibrate `LIQUIDITY_DISTRESS` (N7) — `current_ratio < 1.0` → SOFT flags
      structurally healthy PG, NEE, PM, T, STZ, APA and SBAC (7 of 20); combine it with cash
      coverage (e.g. OCF / current liabilities, interest coverage) and exempt utilities and
      financials. Also add a minimum signal-coverage floor to TECHNICAL (audit C5).)*
- [ ] **T-071** Valorization redesign in `src/cycle/scores/valorization.py`:
      EV-based multiples (`enterprise_fcf_yield` + new `EBITDA/EV`),
      ROIC replacing ROE in the quality factor, remove the size factor,
      robust median/MAD intra-sector standardization, sector-specific
      factors for financials/utilities. → step 2. *(2026-09-25, second audit: also add a
      coverage floor — `_factor_score` averages whichever factors exist, so a name scored on
      one of three is indistinguishable from one scored on all — persist per-factor coverage
      in `inputs_json`, and mark market cap at the cycle date rather than the filing's period
      end.)* *(2026-09-29 system review, scope addition: compute `earnings_yield` from TTM
      net income and the as-of market cap (N8 / audit C1) — `orchestrator._valorization` reads
      `profitability.net_income`/`income_statement.net_income`, neither a stored metric, so the
      value factor uses FCF yields only; keep FUNDAMENTAL normalization in `cycle_ranking`,
      not overwritten on the stored snapshot (N9); ROIC is None when invested capital ≤ 0
      (audit F9). The as-of market cap itself comes from `T-132`'s shared reader.)*
- [ ] **T-072** Persist EBITDA in `fundamental_metrics`
      (`metrics/leverage.py`/`statements.py`) as operating income + D&A. →
      step 2 (EBITDA sub-task). *(2026-09-29 system review, scope addition: net debt
      subtracts short-term investments (audit F7).)*
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
      `SKILL.md` boilerplate. → step 5. *(2026-09-29 system review, scope addition, before
      `T-079`'s LLM re-run: YoY growth returns None when the prior value is ≤ 0 (audit F8);
      quick ratio as (cash + ST investments + receivables) / current liabilities, and cash
      ratio tolerant of a missing cash line (audit F5, F6).)*
- [ ] **T-077** Carhart 4-factor `ret_estimator` with Vasicek beta
      shrinkage (`mu_i = rf + Σ_k β_i,k^shrunk · λ̄_k` over
      `{MKT,SMB,HML,MOM}`, vendored version-pinned Kenneth French factor
      CSV, 756-day rolling betas) in `risk.py`, registered in
      `optimize.py`'s `--mu` choices; `equilibrium` stays default. Enable
      `turnover_cap` in `optimize.py` + a 10-15bps turnover cost in
      `evaluate.py`. **Supersedes T-020–T-026.** → step 6. *(2026-09-25, second audit:
      `turnover_cap` is inert today — it needs `Constraints.w_prev`, which nothing populates;
      populating it from the prior book is part of this task. Its μ must follow `T-109`'s
      convention.)* *(2026-09-29 system review, scope addition: the risk-free CSV path
      refuses an as-of before the series instead of using a future rate (audit Q3);
      optionally, `max_sharpe` falls back to the frontier on solver infeasibility (audit
      Q5).)*
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

**Composite-weights decision (system review N11, 2026-09-29) — open, needs a decision before
`T-070`/`T-071` land.** `src/cycle/config.py` blends FUNDAMENTAL 0.4 / VALORIZATION 0.3 /
TECHNICAL 0.2 / SEMANTIC 0.1 (`_DEFAULT_WEIGHTS`), while the plan's §5.3 decision was equal
weights (1/3 each across the three score types present), and neither choice is recorded
here. Resolve it one way or the other: either adopt equal weights, or keep 0.4 / 0.3 / 0.2
and record its justification here (and in `PLAN.md` Work item 8). Until then `0.4/0.3/0.2/0.1`
is the shipped behaviour, not a documented decision.

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

## Work item 17 — P0/P1: data-integrity defects from the 2026-09-29 system review

Added 2026-10-01, from `docs/md primera revision/system_review_2026-09-29.md` §4 (a
whole-pipeline review of code and logic, checked against `data/financial-3.db`). Each task
below is verified on that database, affects results, and is not covered by an existing task.
They land **ahead of Work item 8** (see the priority note above): `T-131` first (done), then
`T-132` and `T-133`, then the pilot. The review's findings N1–N4 → `T-131`/`T-132`, N5/N6 →
`T-133`; N7–N9 and N11 became scope additions to `T-070`/`T-071`/Work item 8 (above), and N10
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

- [x] **T-132** *(P0)* Market capitalization. **Code done 2026-10-02 (PR #PR).**
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
      **Still open:** production filings carry no cover counts until re-ingested (`run --fresh` /
      `T-100`; no backfill command was built), `verify_pilot.py`'s T-132 check (in the pilot), and
      a follow-up to retire quant's now-vestigial `--metrics-version` (market caps were the only
      `fundamental_metrics` it read).

- [ ] **T-133** *(P1)* Quarterly cash flow and the NEGATIVE_FCF rule.
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

## Work item 12 — Final: full-universe production run (runs last of all)

Added 2026-09-25, at the user's direction. Every other task in this file is either
code, or a check on a **small sample** of assets (`T-068`, `T-088`). The full as-of
universe runs exactly once, after **everything else** is done and verified — so a
defect caught late never forces a second full-scale (and, with the LLM step, costly)
re-run. **This work item is always the last one in `TASKS.md`**; a new work item is
added above it, never below. → `PLAN.md` Work item 12.

`quant`'s own current 20-ticker sample (`APA, APO, BF.B, CPT, ESS, HOOD, HUM, MA, MCD, NEE, PG,
PM, PSX, SBAC, STZ, T, UDR, WAT, WFC, XOM` — the same 20 `quant_return_daily`/`qret-v2` covers)
is scoped via a checked-in `--universe-db /workspaces/thesis/data/universe_sample20.db` (copied
from the real `universe.db`, filtered to these 20 symbols; `T-122`, 2026-09-29) — a prior
scratchpad copy of the same idea was lost between sessions (an untracked `/tmp` path), which is
why this one lives under the production data directory instead. Pass it explicitly to every
`quant` command until `T-100` runs; omitting `--universe-db` defaults to the full 503-member
universe, which `pricing_agent`/`fundamental_agent` (unaffected by this scoping) already cover
but `quant`'s own `qret-v2`/risk-model chain does not, pending `T-100`.

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
      **Must run on a fresh `financial.db` (PR #104 review, 2026-09-30):** `financial_facts`
      is append-only, `INSERT OR IGNORE` on `(filing_id, statement, concept, period_key,
      filing_version = accession_number)` — a re-run against a database already carrying a
      filing's facts under the same accession changes nothing, even after `T-118`'s gateway
      redeploy. Reproduced directly: re-ingesting APA's FY2023 10-K post-redeploy still
      leaves revenue `16,558,000,000` and COGS `1,076,000,000` (both values `T-042` now
      drops) stored with `correction_rule = NULL`, because that accession's rows already
      existed from before 2026-09-30. Any database ingested before the gateway redeploy
      keeps its pre-`T-042`/pre-`T-118` synthesized facts regardless of how many times
      `fundamental_agent run` runs against it afterward — a new `filing_version` (full
      re-ingest under a bumped engine version, not a same-accession re-run) is the only way
      to replace them. `T-100`'s full-universe run must therefore start from a database with
      no pre-2026-09-30 `financial_facts` rows, not an existing one carried forward — the
      share-count scale cross-check (`detect_share_scale_factors`) and the market-cap
      reader (`T-132`) both read
      `financial_facts` directly and would otherwise silently keep reading synthesized
      values. The pilot is unaffected, since it already starts from a fresh database.

## Status

**🔴 Current top priority: Work item 17 (`T-131` ✅ done 2026-10-01 (PR #109), `T-132` ✅ code done 2026-10-02 (PR #PR) → `T-133`), then the pilot
(`docs/md primera revision/pilot_rerun_plan.md`, `verify_pilot.py` at 0 FAIL), then
Work item 8, then Work item 9 (PR #104 review, 2026-09-30 — corrects this
section's own earlier claim that Work item 8 was next).** Work item 14 (the
second forensic audit) is fully closed 2026-09-30, `T-118` last — see
`CHANGELOG.md`. Its `T-121`/`T-122`/`T-123`/`T-124` production actions were all
applied at the user's direction (each task's own entry in `CHANGELOG.md` records
its production-apply date and verification), and `T-125`'s own `migrate` remains
the one deliberately-deferred production write left over from it (still pending
explicit user direction, same as `T-104`/`T-107`/`T-120`'s own precedent); a
production `dq-v2` re-gate for `T-116` is deferred the same way. **Do not start
`T-079`'s LLM re-run (Work item 8) until Work item 17 and `verify_pilot` reaching
0 FAIL have both landed** —
once they have, priority runs **Work item 8, `T-070`–`T-079` (P1, `T-078`
deprecated — `T-074` needs
`T-041`; run only after Work item 7's F1/F2/F4 fixes so the one bundled LLM
re-run scores already-corrected ratios)** → **Work item 9, `T-080`–`T-084`
(P2 — `T-082` needs `T-043`, `T-083` needs `T-042`; the production
`entity_resolution` re-graph is additionally blocked on a `urls.db`
transfer independent of any task here)**.

Work items 1 (done), 3 (superseded by `T-077` — do not implement), 6 (done
upstream + `T-052`), 5 (done 2026-09-25, `T-044` deprecated), 7 (done 2026-09-25), 10 (done),
11 (done 2026-09-25), 13 (done 2026-09-25), 14 (done 2026-09-30), 15 (done 2026-10-01, PR #106) and 16 (done 2026-10-01) are
closed — see `CHANGELOG.md`.

Work items 2 and 4 are unaffected by the audit and keep their original,
lower priority (after Work items 8 and 9 above): nothing in either has
started; both are unblocked.

**Work item 12 (`T-100`, the full-universe production run) runs last of all**, after
every other task in this file — current and future.
