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
open design work. (Closed Work items 1, 3, 5, 6, 7, 10, 11, 13, 14, 15, 16 and 17 are in
`CHANGELOG.md`.) **Work item 14 (P0/P1, the second audit's live defects) is done
2026-09-30** — its blockers on Work item 8, `T-105` (corrects metrics the LLM
re-run/`T-079` consumes) and `T-113`, are both closed. **This does not mean Work
item 8 starts next (PR #104 review):** the priority order (re-set by the user 2026-10-02) is
**Work item 18 (`T-134`–`T-139`, the N-ticker weight heuristic — below) → Work item 8 → Work item 9
→ Work item 3 → Work item 4 → Work item 12 (`T-100`), the full-universe run, last of all** (which
itself needs a *fresh* `financial.db` — see `T-100`'s own entry). Work item 17 (`T-131`, `T-132`,
`T-133`, PRs #109/#110/#111) is closed 2026-10-02, see `CHANGELOG.md`. **Pilot-1 (2026-10-04) ran
at 2 FAIL with the original verification (`T-133`, explained by `T-140`); `T-079` stays blocked
until a pilot-2 on a fresh database, run after `T-140` and Work item 18 have merged, reaches 0
FAIL.** Two things in the user's order
need a call (see `PLAN.md`'s Priority Override): Work item 3 is **superseded** by `T-077`
(Work item 8) and stays "do not implement" unless the user un-supersedes it, so its slot is
empty; and Work item 2 (orchestrator) was not named, so it is parked after Work item 4 until placed. See `PLAN.md`'s
"🔴 Priority Override" section for the full rationale — the source audit markdowns
(`feedback_plan.md`, `upstream_data_mining.md`,
`upstream_portfolio_common.md`) were deleted per the auditor's instruction
after being fully incorporated into `PLAN.md`/this file, which are now the
only durable record.

## Work item 18 — P0/P1: asset-weight heuristic driven by the number of tickers the user wants to hold

Added 2026-10-02, at the user's direction; **first in the new order** (18 → 8 → 9 → 3 → 4). Source: the
2026-10-02 read-only audit's cap finding (recorded in full here, no external file) — with `top_n = 10` the default
`max_name_weight = 0.10` forces every name to exactly 0.10, four Financials then sum to 0.40 and no
weight vector satisfies the 0.30 sector cap; `cycle.construction.target_weights` runs 8 rounds and
returns whatever it has, silently. Production's live book (`cycle_run` 1, 2026-09-22) holds six names at
0.1167 against a 0.10 cap and Energy at 0.35 against 0.30. The caps are constants, but whether they are
feasible, and how much room a score tilt has, depends on **N**, the number of tickers the user wants to
hold (`--top-n`, default 30). This work item makes the weighting rule a function of N, and refuses to
return a book that breaks a cap without saying so. → `PLAN.md` Work item 18.

- [ ] **T-134** Decision + spec, **needs the user before code**: (1) N is the existing `--top-n`
      (confirm, or add `--n-tickers`); (2) the proposed rule, to confirm or replace:
      `max_name_weight(N) = max(0.10, 1.5 / N)` and `max_sector_weight(N) = max(0.30, 1.5 / N)`
      (never tighter than today's defaults, never tighter than 1.5x an equal weight, so a score tilt always
      has room: N=3 -> 0.50, N=5 -> 0.30, N=10 -> 0.15, N>=15 -> 0.10), explicit
      `--max-name-weight`/`--max-sector-weight` still win; (3) when a cap cannot hold (too few sectors,
      or fewer eligible names than N) which gives way — the proposal is the **name** cap relaxes first and
      the **sector** cap stays binding, with the relaxation recorded; (4) whether selection may skip a
      ranked name whose sector is full and take the next one (proposal: yes, at most
      `floor(max_sector_weight x N)` names per sector, so the book still has N names), and whether a
      cash weight is acceptable (proposal: no, always fully invested); (5) the scheme per N
      (`score_proportional` default; `equal` as the degenerate case when the cap is `1/N`). Record the
      decision in `PLAN.md` Work item 18 and `SPEC.md` FR-007. → step 1.
- [ ] **T-135** Implement in `src/cycle/construction.py`: a pure `resolve_caps(n, ...)` per `T-134`, the
      sector-aware fill, and **infeasibility detection** — after the cap loop, any residual breach (or a
      cap that cannot sum to 1) is returned in a result object and logged, never silent. `target_weights`
      keeps its signature or gains a result type; callers updated. Tests: N in `{1, 2, 3, 5, 10, 20, 30}`,
      few-sector and single-sector universes, the production shape (10 names / 4 Financials / 3 Energy),
      explicit-override precedence, sum-to-1 and both caps hold or the breach is reported. → step 2.
- [ ] **T-136** Wire it through `cycle`: `CycleSettings`/`--top-n`, the **effective** caps and any
      recorded relaxation persisted in `cycle_run.params_json` (so `v_weight_scheme`, which reads
      `$.max_name_weight`/`$.max_sector_weight`, reports what was applied; no view change), N larger than
      the eligible, non-vetoed count recorded as a shortfall rather than padded. Production re-select is
      a write and stays deferred until the user directs it. → step 3.
- [ ] **T-137** Align `quant`'s benchmark with the same N: `QuantSettings.max_name_weight` (0.05) and
      `max_sector_weight` (0.30) are constants too, and a benchmark with a different effective N is not
      comparable to the live book. `quant` must not import `cycle` (isolation test), so copy the rule the way
      `quant.state` copies `cycle.state` and pin the two against each other with a test; leave the
      benchmark's universe/liquidity gate untouched (it must stay independent of any score). Needs `T-134`'s
      answer on whether the benchmark follows N or keeps its own caps. → step 4.
- [ ] **T-138** Verify on real data, on a **scratch copy** of `financial.db` (production is not written):
      reproduce `cycle_run` 1's failing shape, then `cycle select` for N in `{3, 5, 10, 20}` on the
      20-ticker sample (`universe_sample20.db`); every book sums to 1 and satisfies both caps, or records
      its relaxation. Methodology fix, so a `docs/model_fixes.md` entry with before/after numbers and a
      reference (constitution AI behavior #12; e.g. a long-only mean-variance/constraint-feasibility
      reference such as the 1/N benchmark, DeMiguel, Garlappi & Uppal 2009). → step 5.
- [ ] **T-139** Docs and artifacts: `docs/cycle.md` (replace "caps are approximate"), `docs/quant.md` if
      `T-137` lands, `SPEC.md` FR-007, and both architecture artifacts (constitution AI behavior #11,
      reconcile, never rename). → step 6.

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
      (audit F9). The as-of market cap itself comes from `T-132`'s shared reader.)* *(2026-10-02,
      `T-133` review, scope addition: sector-appropriate cash-flow measures -- capex for utilities
      (`PaymentsForConstructionInProcess`, utility/non-utility split lines) and REITs, and for filers
      whose capex is `PaymentsToAcquireOtherPropertyPlantAndEquipment`/`...OtherProductiveAssets`, which
      `T-133`'s single-line lookup leaves empty rather than partial; see `docs/model_fixes.md` T-133
      "Residual scope".)*
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
- [ ] **T-140** (P0, pilot findings 2026-10-04; blocks `T-079` and `T-100`) Two ingestion defects
      found by the 20-asset pilot on a fresh database. → `PLAN.md` Work item 8, step 8.
      **Implemented in PR #115 (engine `metrics-v5`); acceptance pending pilot-2** -- the box stays
      open until the criteria below are measured on a fresh database after Work item 18 merges.
      Record: `docs/model_fixes.md`, T-140. Known before pilot-2: APO's Q1-2023 10-Q stays unstored
      (the gateway returns only the prior fiscal year's column; recorded as a run error, upstream
      note drafted), so a "no gap over 110 days" check still sees APO's 181-day hole.
      **(a) Revenue the gateway drops.** The gateway's `T-042` rule (`no_filed_nondimensional_fact`)
      removes revenue that a filer tags only with a dimension. APA 2021Q1–2023Q3 (11 filings) then
      has no revenue: FY2022 "Total revenues" $11,075M and production revenues $9,220M are dropped,
      although production stored FY2021 $7,988M (`T-095`'s path, $3M off) and FY2022 $11,075M. Every revenue ratio is
      empty, and `DQ_REVENUE_POS` (HARD) fired 77 times and HARD-vetoes APA (replay: 2024-01-05 to
      2024-02-23). Fix: when no revenue total survives, rebuild it from the "Total revenues and
      other" line minus the rows between it and the missing total, using `T-117`'s label mechanism
      (the rows are already stored in `financial_facts`). Refuse rather than guess when they aren't
      there.
      **(b) Quarters dropped for a repeated label.** The resume key `(ticker, form, fiscal_period)`
      uses the gateway's quarter tag, which is wrong for shifted fiscal calendars. WAT's quarter
      ending 2023-07-01 (fiscal Q2) is tagged `(Q3)`, the same as the real Q3 (2023-09-30), so the
      second filing is skipped as "already done" (`pipeline.py:343-347`) with no error logged.
      Production has 40 names with a 10-Q labelled "Q4" (JNJ, PFE, TMO, AAPL, INTC, DIS, TGT…). Fix:
      key the resume on period end or accession, and derive the quarter label from the fiscal year
      end. Record a run error with a reason whenever a filing yields no quarter of its own (APO
      Q1-2023: the gateway returns only the prior fiscal year's column; note this for
      `portfolio-data-mining`).
      **Acceptance:**
      - On the pilot-2 run (fresh database, after `T-140` and Work item 18 merge):
        - APA FY2021 revenue $7,985M, FY2022 revenue $11,075M and 2021Q1–2023Q3 non-null.
        - `DQ_REVENUE_POS` = 0.
        - WAT's 2023-09-30 10-Q stored.
        - No gap over 110 days between consecutive 10-K/10-Q period ends, and no 10-Q labelled Q4.
        - The pilot verification at 0 FAIL.
      - At `T-100`: the number of filings with rebuilt revenue and of corrected labels, counted
        across the full universe and inspected.
      - A `docs/model_fixes.md` entry (methodology change, constitution AI behavior #12).
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

**🔴 Current priority order (user's, 2026-10-02): Work item 18 (`T-134`–`T-139`, the N-ticker
weight heuristic) → Work item 8 → Work item 9 → Work item 3 (superseded by `T-077`, see the top of this
file) → Work item 4.** Pilot-1 (2026-10-04) ran at 2 FAIL with the original verification (`T-133`,
explained by `T-140`); `T-079` stays blocked until a pilot-2 on a fresh database, run after `T-140`
and Work item 18 have merged, reaches 0 FAIL. Work item 17 (`T-131`, `T-132`, `T-133`, PRs #109/#110/#111)
closed 2026-10-02, see `CHANGELOG.md`. Work item 14 (the
second forensic audit) is fully closed 2026-09-30, `T-118` last — see
`CHANGELOG.md`. Its `T-121`/`T-122`/`T-123`/`T-124` production actions were all
applied at the user's direction (each task's own entry in `CHANGELOG.md` records
its production-apply date and verification), and `T-125`'s own `migrate` remains
the one deliberately-deferred production write left over from it (still pending
explicit user direction, same as `T-104`/`T-107`/`T-120`'s own precedent); a
production `dq-v2` re-gate for `T-116` is deferred the same way. **Do not start
`T-079`'s LLM re-run (Work item 8) until a pilot-2 on a fresh database, run after `T-140`
and Work item 18 have merged, reaches 0 FAIL**; within Work item 8, `T-070`–`T-079` (P1, `T-078`
deprecated — `T-074` needs
`T-041`; run only after Work item 7's F1/F2/F4 fixes so the one bundled LLM
re-run scores already-corrected ratios) is followed by **Work item 9, `T-080`–`T-084`
(P2 — `T-082` needs `T-043`, `T-083` needs `T-042`; the production
`entity_resolution` re-graph is additionally blocked on a `urls.db`
transfer independent of any task here)**.

Work items 1 (done), 3 (superseded by `T-077` — do not implement), 6 (done
upstream + `T-052`), 5 (done 2026-09-25, `T-044` deprecated), 7 (done 2026-09-25), 10 (done),
11 (done 2026-09-25), 13 (done 2026-09-25), 14 (done 2026-09-30), 15 (done 2026-10-01, PR #106) and 16 (done 2026-10-01) are
closed — see `CHANGELOG.md`.

Work items 2 and 4 are unaffected by the audit: nothing in either has started and both are
unblocked. Work item 4 is placed last-but-one by the user's order; Work item 2 was not named in it and is
parked after Work item 4 until the user places it.

**Work item 12 (`T-100`, the full-universe production run) runs last of all**, after
every other task in this file — current and future.
