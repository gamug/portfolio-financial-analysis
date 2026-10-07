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

**🔴 Priority override (2026-09-08 forensic audit; order re-set by the user 2026-10-05)**: Work item 8
(`T-141`, `T-070`–`T-077`, `T-147`, `T-148`) is the current top priority — a direct audit against production data
(`data/financial.db`) found live correctness bugs, not open design work. (Closed Work items 1, 3, 5, 6, 7,
10, 11, 13, 14, 15, 16, 17 and 18 are in `CHANGELOG.md`.) Work item 14 (P0/P1, the second audit's live
defects) is done 2026-09-30; Work item 17 (`T-131`–`T-133`) is closed 2026-10-02; Work item 18 (the N-ticker
weight heuristic, `T-134`–`T-139`) is closed 2026-10-05.

**Order (user, 2026-10-05, Work item 8's part re-set 2026-10-07; scope: finish this repo first):** Work item 8
(`T-141` → `T-077` → `T-070` → `T-147` → `T-148` → `T-072` → `T-076` → `T-073` → `T-074` → `T-071`) →
**Work item 19** (local follow-ups: `T-083` → `T-142` → `T-144` → `T-145`) → Work item 2 (orchestrator) → **`T-143`, the final pilot** (Work item 12) → **`T-100`**, the
full-universe run, last of all, on a *fresh* `financial.db` (see `T-100`'s own entry). Work item 20
(run-endpoint access control; the repo is not a production version yet) and Work item 4 (SEMANTIC; depends on
`portfolio-nlp`) and Work item 9's `T-080`/`T-081`/`T-082`/`T-084` (they change what the knowledge graph
consumes; `T-084` waits on data-mining's `urls.db`) are **deferred until after `T-100`** and are no longer
prerequisites of it. Work item 3 stays superseded by `T-077` (do not implement). One task per PR throughout,
each ending with its status commit after approval.

**Superseded by `T-100` (2026-10-05):** `T-079` (the bundled LLM re-run) — `T-100` starts from a fresh
database and runs the LLM on every filing, so a separate re-run would pay for the same calls twice — and
`T-075` (the Ring-2 narrative bridge) — a stop-gap for production's old narratives, and production stays
frozen until `T-100`, which re-scores everything with the final prompts. **The final pilot gates `T-100`**:
pilot-1 (2026-10-04) ran at 2 FAIL with the original verification (`T-133`, explained by `T-140`); `T-140`
merged 2026-10-05 (PR #115), and its acceptance is measured by `T-143`, which also doubles as `T-014`'s check.
The source audit markdowns (`feedback_plan.md`, `upstream_data_mining.md`, `upstream_portfolio_common.md`)
were deleted per the auditor's instruction after being fully incorporated into `PLAN.md`/this file, which are
now the only durable record. See `PLAN.md`'s "🔴 Priority Override" section for the rationale.

## Work item 2 — Cross-module orchestrator

Placed by the user 2026-10-05: after Work item 19, before the final pilot `T-143` (Work item 12).

- [x] **T-010** Design decision: where the orchestrator is exposed and how it relates to the per-package CLIs.
      → `PLAN.md` Work item 2, step 1. *(Closed 2026-10-06, user: `api/` is the repo's single entry point for
      orchestrated and remote runs, via FastAPI/Swagger, with the orchestrator and each listed step as an
      endpoint; the per-package CLIs stay; acceptance is through both HTTP and a CLI wrapper. The package
      location question moved to `T-011`; `T-018` must land before it.)*
- [x] **T-018** Amend the constitution (Tech stack #3's "read-only `api/`", plus pointers in Project structure
      #2 and "Executable cmds") and `SPEC.md` FR-014 ("never triggers an agent run", all connections
      `mode=ro`) so `api/` may host the run endpoints; the read endpoints stay read-only and FR-014's
      acceptance check is re-scoped to the read routers. Constitution 2.0.0 (MAJOR). Must land before `T-011`
      (a plan that conflicts with the constitution amends it first). → `PLAN.md` Work item 2, step 1
      *(added 2026-10-06; done 2026-10-06, PR #124, constitution 2.0.0, SPEC 1.2.0)*.
- [ ] **T-011** Implement the sequencing runner: pricing_agent → fundamental_agent → entity_resolution →
      cycle → quant, one `--analysis-date`, exposed as the orchestrator endpoint, the per-step endpoints listed
      in `PLAN.md` step 1 (run handle + a read-only status endpoint), and a CLI wrapper over the same code.
      Relies on each package's existing idempotency rather than re-implementing skip logic. Implements step 1's
      request contract (`analysis_date` + optional `top_n`/`weight_scheme`/`max_name_weight`/`max_sector_weight`,
      the same on the CLI wrapper; preference flags rejected on the writing path; no `--allow-*` override or
      `migrate` over HTTP). **Also decides where the orchestration code lives** (a new top-level package vs.
      inside `api/`, against Project structure #1's bar). → steps 1 and 2.
- [ ] **T-012** Record orchestrator-level provenance (step, run_id, start/end, status) reusing the existing
      run-log `v_*` pattern; every run endpoint, single-step included, writes a row; the status endpoint reads
      it. → step 3.
- [ ] **T-013** Per-step failure isolation: an independent step's failure (e.g. `entity_resolution`) does not
      block steps with no real dependency on it; a dependent step (`cycle` on
      `fundamental_agent`/`pricing_agent`, `quant` on `pricing_agent` and `fundamental_agent`, the latter for
      the cover-page share count behind market cap) does hard-block. → step 4.
- [ ] **T-017** Implement the incremental, upstream-aware run: define each step's pending signal (including
      `entity_resolution`'s news and `quant`'s corporate actions), run the pending steps plus their downstream
      dependents, append only (NR-007); when nothing is pending, run only `cycle select` (the orchestrator's
      cycle step is always `select`). The semantic check degrades to "no source configured" until Work item
      4's `KG_NLP_DB` seam exists: the writer is not in question (per `docs/semantic-score-boundary.md`'s
      target column and the knowledge graph's D14, `portfolio-nlp` computes the per-(asset, day) measure and
      this repo writes `score_snapshot[SEMANTIC]`, Work item 4); KG `T-158` is only the `score_method` value.
      Likewise, when `KG_NEWS_DB` is not configured or does not exist, `entity_resolution` reports "no news
      source configured", is not pending, and does not fail. → steps 2, 4 and 5; `PLAN.md` acceptance criteria,
      "Incremental, upstream-aware run" *(added 2026-10-06)*.
- [ ] **T-019** Verify the run endpoints: one endpoint per step in `PLAN.md` step 1; run handle, no held
      request; the status endpoint is a read-only `GET`; write connections only in the run routers and no read
      router imports a run router (`grep`); no `migrate`/`cycle backfill`/`cycle undo-run` endpoint; the request
      contract holds (preference flags rejected, no `--allow-*` override reachable). → `PLAN.md` acceptance
      criteria, "Run endpoints"; `SPEC.md` FR-014 *(added 2026-10-06)*.
- [ ] **T-014** Verify: a single call (the orchestrator endpoint + poll, and the CLI wrapper) completes
      pricing → fundamental → entity_resolution → cycle → quant for one `--analysis-date` on a fresh universe
      with no pre-existing data. → `PLAN.md` acceptance criteria, first bullet. *(2026-10-05: the final pilot,
      `T-143`, doubles as this check — one orchestrator run on a fresh database, through both surfaces.)*
- [ ] **T-015** Verify: killing the orchestrator mid-run and re-invoking it (over HTTP and the CLI) does not
      redo an already-completed step. → second acceptance criterion.
- [ ] **T-016** Update `SPEC.md` §13 item 3 to the resolved state and reconcile every statement that is true
      only before the orchestrator ships, in the same change as the code: `SPEC.md` §3 diagram label
      (`read-only FastAPI`) and §11 step 3 (`no scheduler or orchestrator is wired in today… read-only
      service`); `README.md` "Read-only HTTP API" section and `docs/README.md`'s `api/` row; `docs/api.md`.
      Also update `SPEC.md` §1's "portfolio-reports (as-of run engine)" to: `portfolio-app` triggers runs (the
      orchestrator's run endpoints or its CLI wrapper) and `portfolio-reports` reads (the knowledge-graph
      agreement, 2026-10-06).
      Also update the two architecture artifacts per constitution AI behavior #11 — reconcile, never rename.

## Work item 4 — SEMANTIC boundary: this repo's half — DEFERRED until after `T-100`

**Deferred 2026-10-05 (user: finish this repo first).** Reason: it depends on `portfolio-nlp`, a different
repo, and `T-141` removes `SEMANTIC` from the composite weights until this work item lands. It is no longer
a prerequisite of `T-100`.

**Also in this work item (knowledge-graph request, 2026-10-06):** name the SEMANTIC `score_method` value for the
knowledge graph.

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

**Order (user, 2026-10-07; replaces the 2026-10-05 order):** `T-141` → `T-077` → `T-070` → `T-147` → `T-148` →
`T-072` → `T-076` → `T-073` → `T-074` → `T-071`.
- **Why:** `T-077` (the Carhart estimator over prices and Kenneth French factors) and `T-070` (the price-based
  technical score) read no SEC statement data, so they run while the user builds the SEC data-treatment checklist
  (`T-147`'s input). The tasks that build on SEC-derived metrics (`T-072`, `T-076`, `T-073`, `T-074`, `T-071`) wait
  for `T-148`'s fixes.
- **`T-147` may start earlier** if the user approves `checklist_v1.md` before `T-070` merges: it then follows the
  task in progress.
- **Safety valve:** if the checklist is not ready when `T-070` merges, `T-148` goes ahead with D1–D4 only
  (confirmed against the SEC), and the checklist's later findings become `T-149`+.

One task per PR, each ending with its status commit after approval. `T-075` and `T-079` are superseded by
`T-100` (see their entries); `T-078` stays deprecated; `T-140` is merged and its acceptance is measured by
the final pilot, `T-143`.

- [ ] **T-141** Equal composite weights (system review N11, user decision 2026-10-05; resolves the
      composite-weights note that stood before `T-070`/`T-071`). Set `_DEFAULT_WEIGHTS` in
      `src/cycle/config.py` to FUNDAMENTAL, VALORIZATION and TECHNICAL at 1/3 each, and remove
      SEMANTIC until Work item 4. **Why it is mathematically sound:** all three components are
      winsorized cross-sectional z-scores mapped to `50 + 10z` (`src/cycle/scores/normalize.py`), so
      they share one scale and equal weights give equal expected influence; the per-asset
      renormalization over the components present, and the `[0, 100]` clamp, are known effects, to be
      documented. Reference: the 1/N argument (DeMiguel, Garlappi & Uppal 2009), consistent with Work
      item 18. **Verification on scratch copies** (never production): report each component's
      cross-sectional standard deviation and the pairwise correlations; how many assets were blended
      from fewer than three components; and, old `0.4/0.3/0.2` vs the new weights, the rank
      correlation and the top-N overlap on the replay. Amend `SPEC.md` FR-007's default-weights text
      and `docs/cycle.md`, and add a `docs/model_fixes.md` entry (constitution AI behavior #12).
      → `PLAN.md` Work item 8, step 9.
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
- [ ] **T-147** SEC data-treatment checklist audit (user, 2026-10-07; docs, a script and tests -- **no change
      under `src/`**). Audit the data path (EDGAR → `portfolio-data-mining` gateway → `fundamental_agent` →
      `cycle`/`quant`) against the SEC data-treatment checklist. **Starts when the user approves
      `checklist_v1.md`**, built from SEC, FASB, XBRL US DQC, Nareit, academic and edgartools sources (the user
      owns the sources and the NotebookLM rounds; Claude reviews the draft). → `PLAN.md` Work item 8, step 10.
      **Deliverables:**
      - `docs/sec_data_checklist.md` (tracked). Per rule ID: the implementing file/function, or `none`; status
        ✓ / partial / ✗ / not applicable (with the reason); evidence (the companies and filings violating it, up
        to 5 example tickers); the check that tests it.
      - `scripts/audit_sec_checklist.py`: network; read-only; at most 10 requests per second; every response
        cached. SEC User-Agent: `SEC_USER_AGENT` when set (the convention of
        `fundamental_agent/filing_text.py`), else the default `research@example.com`; the PR records the
        contact used for the real run.
      - The cross-check scripts the acceptance of `T-148` and `T-143` rely on (`itemcheck.py`, `signcheck.py`,
        `precedence.py`, today untracked under `docs/checklist_sec/sec_xcheck/`) are promoted to **tracked**
        `scripts/sec_xcheck/`, with the paths `T-148` and `T-143` name updated, so a clean checkout can reproduce
        their results. Their SEC data is fetched into the cache, not committed.
      - Hermetic tests of the check logic, on fixtures (no network in `tests/`).
      **Two evidence levels, kept separate in every table:**
      - (a) Prevalence at the source: SEC `companyfacts` for the 503 CIKs in production `assets` (plus
        predecessor CIKs), filtered to the accessions of our filings. `companyfacts` carries only
        non-dimensional facts of standard taxonomies, so the counts are scoped to those. A rule that depends on
        a dimensional fact or on an issuer-specific (custom) concept is marked **source prevalence incomplete**
        in the table, never counted as zero; the custom concepts present in our own `financial_facts`
        (9,271 on the pilot) are reported at level (b), and a filing-level XBRL sample may measure them.
      - (b) Our resolver: today's `statements.py` re-run over production's stored `financial_facts`, read-only
        (`mode=ro`) with `shasum` before and after, plus the pilot database and a live-gateway sample.
        Caveat, recorded: production predates some gateway fixes and stores no `preferred_sign`.
      **Also delivers:**
      - Conflicts with how facts are selected today: point in time vs latest-filed, growth comparatives, the TTM
        vintage mix, edgartools' `preferred_sign` and standardization.
      - Rules the agent disagrees with, with reasoning (nothing dropped silently).
      - A map of every defect fixed so far to a rule ID, and the fixed defects no rule covers.
      - A golden set: the pilot's 20 tickers plus an insurer, GOOGL and BRK.B. It checks semantic choices (which
        line is revenue for a bank, whether FFO is computable, which capex lines count for a utility), not
        numbers.
      - Type-based (GICS/SIC) treatment; a ticker-override format (ticker, rule ID, treatment, reason, source,
        valid-from date) only as a last resort, each entry justified.
      - A prevalence table sorted by severity × companies affected.
      - A prioritized fix plan for `T-148`+.
      **Acceptance:** the PR delivers all of the above; the `shasum` of production's database before and after is
      in the PR; a sample of the counts re-runs independently; every ✓ cites code that enforces the rule.
- [ ] **T-148** Line-item resolution and sign correctness (engine `metrics-v6`; `fundamental_agent`). D1–D4 below,
      plus whatever `T-147` prioritizes. **Follows `T-147`'s approval**, except under the safety valve (`T-148`
      then goes ahead with D1–D4 only). → `PLAN.md` Work item 8, step 11.
      - (a) **Filed sign.** Store edgartools' `preferred_sign` (an additive column on `financial_facts`).
        `Statements.get` returns line items in the filed XBRL sign; the `T-117`/`T-140` rebuild arithmetic stays
        on displayed values; keep the `abs()` guards. Evidence (2026-10-06): 86 pilot filings have income tax
        with the wrong sign (`effective_tax_rate` forced to 0, feeding ROIC and enterprise FCF yield), and one
        company's COGS has the wrong sign. `preferred_sign = −1` matched the flipped rows 200/200 on live
        payloads. **Existing databases:** `financial_facts` is append-only on `(filing_id, statement, concept,
        period_key, filing_version)`, so a same-accession re-run leaves old rows without `preferred_sign`
        (NULL). There is no in-place backfill: a NULL reads as today's displayed sign (recorded), the fix takes
        effect on a database rebuilt with the branch, and the pilot `T-143` and `T-100` both start from fresh
        databases. The `model_fixes.md` entry states this.
      - (b) **Precedence.** Exact concepts first, in the spec's order, then `standard`, then `label_contains`.
        Net income: `NetIncomeLoss` (attributable to the parent) before `ProfitLoss`, a methodology decision,
        recorded, with its own regression test (a filing that files both). Equity: `StockholdersEquity` first. Evidence: 37 filings read equity "before treasury stock"
        (PM +22.6B vs −12.6B); 237 filings read net income including non-controlling interests.
      - (c) **Income tax.** Never a single component through `standard`. When the total is absent: Current +
        Deferred when both are filed, else None (the default rate applies). Evidence: APA, 22 filings, deferred
        only.
      - (d) **Short-term debt.** `DebtCurrent` when it is filed; else `LongTermDebtCurrent` +
        `ShortTermBorrowings`, plus `CommercialPaper` only when it is not already included in
        `ShortTermBorrowings`. Verified against the SEC. Evidence: 60 filings read one partial line.
      - (e) **Records.** Engine `metrics-v6`; a `docs/model_fixes.md` entry citing the checklist rule IDs and
        their sources (constitution AI behavior #12); regression tests (PM equity, the STZ/APO tax sign, the APA
        tax component, the STZ COGS sign, net income parent-vs-NCI, short-term debt).
      - (f) **Acceptance.** On a database rebuilt with the branch, the tracked cross-check scripts (`T-147`
        promotes `itemcheck.py`, `signcheck.py` and `precedence.py` from `docs/checklist_sec/sec_xcheck/` to
        `scripts/sec_xcheck/`) report 0 FLIPPED and no OTHER_CONCEPT for income tax, equity and net income
        (the 237 NCI filings resolve to `NetIncomeLoss`), with every number in the PR.
      - (g) **Overlap with `T-070`.** `T-070` (earlier in the order) recalibrates `LIQUIDITY_DISTRESS`. `T-147`
        states whether any input of that rule, or of another score or veto `T-070` touches, is changed by
        `T-148`; if so, `T-148` repeats `T-070`'s calibration check on the rebuilt data and reports it.
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
      `data_error_suspected` as a `cycle` data-quality veto. `forensic_flags_json` is NULL when the flags
      were not evaluated (and on every row other than FUNDAMENTAL), and holds the four-boolean object
      (`data_error_suspected`, `negative_equity_buyback`, `value_destroyer_sub_wacc`,
      `severe_sbc_dilution`) when they were; all four `false` means evaluated, none fired (not `[]`).
      **Needs T-041.** → step 4.
- [ ] **T-075** *(**SUPERSEDED 2026-10-05, at the user's direction — do not implement.** Kept
      unchecked as the historical record, per this file's "mark cancelled in place" rule. Reason: it
      was a stop-gap for production's old narratives until the re-run, and production stays frozen
      until `T-100`, which re-scores everything with the final prompts.)* Ring-2 zero-cost bridge:
      regex-mine the existing 4,844
      narratives for "data error"/"accounting artifact"/"nonsensical" and
      raise the data-quality veto immediately, ahead of T-073/T-074's full
      re-run. → step 4 (bridge sub-task).
- [ ] **T-076** Skills redesign: 10-Q frequency preamble, tightened
      valuation magnitude gate (`DATA_ERROR_SUSPECTED` on `|FCF yield| >
      50%` or scale mismatch), new dedicated `profitability` skill
      (DuPont, annualization, ROE→ROIC fallback), cost-hygiene trim of
      `SKILL.md` boilerplate. → step 5. *(2026-09-29 system review, scope addition, before
      `T-100`'s LLM run: YoY growth returns None when the prior value is ≤ 0 (audit F8);
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
- [ ] **T-140** (P0, pilot findings 2026-10-04; blocks `T-100`) Two ingestion defects
      found by the 20-asset pilot on a fresh database. → `PLAN.md` Work item 8, step 8.
      **Implemented and approved: PR #115, merged 2026-10-05 (engine `metrics-v5`); acceptance
      pending the final pilot (`T-143`)** -- the box stays open until the criteria below are measured
      on a fresh database. Record: `docs/model_fixes.md`, T-140; `SPEC.md` FR-001.
      Beyond the brief, kept on review: periods ending in the first week of January label as the
      year before (J&J's FY ending 2023-01-01 and 2023-12-31 were both `FY2023`), and a quarter is
      paired with last year's by date (`Statements.prior_of`); the quarter is counted from the
      filing's own balance-sheet fiscal year end first, the stored 10-K as fallback (FERG changed
      its fiscal year). Two APA quarters over `T-117`'s ceiling are admitted only when the gateway's
      own figure agrees to the dollar. Known before the final pilot: APO's Q1-2023 10-Q stays unstored
      (the gateway returns only the prior fiscal year's column; recorded as a run error; the note
      for `portfolio-data-mining` is drafted, not filed), so a "no gap over 110 days" check sees
      APO's 181-day hole unless the recorded run error exempts it (the pilot verification now does).
      IBKR FY2025 and KVUE's first 10-Q after its IPO also get no label; both are recorded.
      **Review's production replay of `scripts/verify_t140.py` (5,076 filings, read-only), a
      pre-count for `T-100`, not its acceptance:** revenue 0 changed (rule fires and equals the
      existing value for MPC, NI, PSX; refused, existing value kept, for ADP, BNY, ICE); labels 95
      relabelled across 51 tickers (73 quarters, 22 January year ends), spot-checked against each
      company's fiscal calendar. `T-100` still counts both on its own fresh database.
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
      - On the final pilot (`T-143`; fresh database):
        - APA FY2021 revenue $7,985M, FY2022 revenue $11,075M and 2021Q1–2023Q3 non-null.
        - `DQ_REVENUE_POS` = 0.
        - WAT's 2023-09-30 10-Q stored.
        - No gap over 110 days between consecutive 10-K/10-Q period ends, and no 10-Q labelled Q4.
        - The pilot verification at 0 FAIL.
      - At `T-100`: the number of filings with rebuilt revenue and of corrected labels, counted
        across the full universe and inspected.
      - A `docs/model_fixes.md` entry (methodology change, constitution AI behavior #12). **Done**
        in PR #115.
- [ ] **T-079** *(**SUPERSEDED by `T-100`, 2026-10-05, at the user's direction — do not
      implement.** Kept unchecked as the historical record. Reason: `T-100` starts from a fresh
      database and runs the LLM on every filing, so a separate bundled re-run would pay for the same
      calls twice; the final pilot (`T-143`) gates `T-100`.)* One bundled LLM re-run
      covering T-073+T-074+T-076 together
      (~4,844 filings) — do not re-run per prompt edit; full suite green
      afterward. → `PLAN.md` Work item 8 acceptance criteria.

## Work item 9 — P2: entity-resolution sanitization and `v_quant_vs_live` consumption — `T-080`/`T-081`/`T-082`/`T-084` DEFERRED until after `T-100`

**Deferred 2026-10-05 (user: finish this repo first).** Reason: `T-080`/`T-081`/`T-082`/`T-084` change what
the knowledge graph consumes, and `T-084` waits on data-mining's `urls.db`. They are no longer prerequisites
of `T-100`. `T-083` (local: our API reading `v_quant_vs_live`) moved to Work item 19.

- [ ] **T-080** Require an explicit corporate title strictly linked to an
      S&P 500 constituent in `entity_resolution/cooccurrence.py` before
      recording a `shared_executive_edge`. → `PLAN.md` Work item 9, step 1.
- [ ] **T-081** Add a denylist of known media/analyst/wire-service names
      (Reuters, Bloomberg, ForexLive, …). → step 2.
- [ ] **T-082** Route non-executive-but-genuine media co-occurrence into
      `media_cooccurrence` instead of dropping it. **Needs T-043.** →
      step 3. Also add `v_media_cooccurrence_edge` (the knowledge-graph repo's request, 2026-10-06), and fill
      `first_seen`/`last_seen` or document that they stay NULL.
- [ ] **T-083** *(**MOVED 2026-10-05 to Work item 19**, text unchanged there; kept unchecked here as
      the historical record.)* Update `api`/`kg_schema` read paths to consume the
      rewritten `v_quant_vs_live`. **Needs T-042.** → step 4.
- [ ] **T-084** Track the production `entity_resolution` re-graph as
      explicitly blocked pending the `urls.db` (`KG_NEWS_DB`, ~4.5GB)
      transfer — not silently treated as done once T-080–T-082 land as a
      code-level fix only. → `PLAN.md` Work item 9 "Blocked by missing
      data" note.

## Work item 19 — Local follow-ups before the final pilot

Added 2026-10-05, at the user's direction (scope: finish this repo first). Local work that needs nothing
from another repo, placed above Work item 12 so it merges before the final pilot (`T-143`). One task per PR,
each ending with its status commit after approval. → `PLAN.md` Work item 19. Order: `T-083` → `T-142` →
`T-144` → `T-145`. `T-144` and `T-145` record the knowledge-graph view changes the user approved 2026-10-06
(the knowledge-graph repo already references these two ids); they are the schema and `v_*` contract changes
that constitution AI behavior #10 asks to be approved first.

- [ ] **T-083** *(moved from Work item 9, text unchanged)* Update `api`/`kg_schema` read paths to consume the
      rewritten `v_quant_vs_live`. **Needs T-042.** → `PLAN.md` Work item 9, step 4.
- [ ] **T-142** PR #112 review follow-ups, each with a regression test (item (d) is documentation):
      - (a) `cycle/orchestrator.py`: catch `BaseException` and call `conn.rollback()` before writing a
        failed checkpoint or a failed `cycle_run` (today a failure partway through a step saves its
        half-written writes, and a Ctrl-C leaves the run at `running`).
      - (b) `quant/db.py`: remove the inner `conn.commit()` from `insert_risk_model`,
        `insert_expected_returns`, `insert_covariance`, `insert_portfolio`, `sync_positions` and
        `insert_frontier_points`, so each run commits once at `finish_run`.
      - (c) `/portfolio/ranking`: take the latest cycle date only from runs with
        `status = 'completed'` (join `v_cycle_run`; no view change).
      - (d) Document the units of `planned_units`, `completed_units`/`skipped_units` and
        `failed_units` in `docs/fundamental_agent.md` and the `v_analysis_run` notes.
- [ ] **T-144** The knowledge-graph view contract, one additive change (`kg_schema` views, DDL notes and
      `docs/kg_schema.md`). → `PLAN.md` Work item 19, step 3.
      - `v_fundamental_metric` (new): `ticker`, `asset_id`, `filing_id`, `metric_group`, `metric_name`,
        `metric_id` (`metric_group || '.' || metric_name`, joins to `v_rule_catalog.param_metric`), `unit`,
        `value`, `engine_version`, `is_current`, `event_time`, `available_at`, `run_id`.
      - `v_score_snapshot`: add `forensic_flags_json` and `prompt_hash`; `available_at` returns the cycle
        date for TECHNICAL, VALORIZATION and SECTOR rows (the stored column and FUNDAMENTAL rows are
        unchanged).
      - `v_shared_executive_edge`: add `computed_at` (`MAX`) and `run_id`.
      - `v_cycle_ranking`: add `cr.status`, and fix its docstring.
      - `v_quant_vs_live`: add `engine_version` and `is_current`, and drop `equal_weight`/`cap_weight` from
        its filters. `v_quant_portfolio`: add `is_current`.
      - `is_current`: for metrics, the version `resolve_metric_versions` picks with no explicit selection; for
        quant, the newest `opt-v*` per `(as_of, kind)`. At most one current row per key.
      - `v_cycle_ranking_component` (new): `cycle_run_id`, `asset_id`, `score_type`, `component_value`,
        `configured_weight`, `effective_weight`; one row for every non-null component of every ranking row,
        vetoed or not.
      - `docs/kg_schema.md` documents: `blended_score` is 0–100 minus the run's `soft_veto_penalty` (15 by
        default) per SOFT veto, and 0.0 minus that deduction with no components; SECTOR `raw_value` is in
        TECHNICAL points, [−100, 100]; `normalized_score` is 50 + 10z over the cohort, clamped to [0, 100], the same for SECTOR; FUNDAMENTAL's `normalized_score` is
        rewritten by every cycle, so the per-cycle value is `component_value`; the units (`ratio`, `x`,
        `usd`); weights and caps are fractions of the book, caps are effective on SELECTION runs and
        configured (possibly NULL) on MONITORING runs; `scheme_id` is the position-weighting rule, not the
        blend; no component means all components null; `vetoed` is HARD or UNSCORED only; the timezones of
        the `*_at` columns; the forensic-flag keys.
      - A marker migration that bumps `schema_version` (run through `migrate`, FR-011). `ensure_views` drops
        and recreates every view from the running code's definitions, so an older process that runs `ensure`
        against a migrated database would remove the new columns and views: every process that opens the
        database is upgraded (or stopped) before `migrate` runs, and `ensure_views` skips its rebuild when
        the database's `schema_version` is above the highest migration the code knows (protects later code
        from the next contract change; it cannot retrofit code that predates it).
      - **Acceptance**: the views build on a copy of production and on the pilot; `available_at` is
        non-NULL and ≥ `event_time` on every row of `v_fundamental_metric` and of `v_score_snapshot`'s
        FUNDAMENTAL, TECHNICAL, VALORIZATION and SECTOR rows (SEMANTIC rows are not written until Work item
        4); at most one `is_current` row per key; for a (run, asset) with
        at least one component, the effective weights sum to 1 and Σ(effective weight × `component_value`) −
        `soft_veto_penalty` × (number of SOFT vetoes) = `blended_score`, where `soft_veto_penalty` is the
        run's (`params_json`, `v_weight_scheme`) and the SOFT count is the `veto_rules_json` entries other than
        `HARD` and `UNSCORED`; every non-null component has a row; existing view columns are unchanged in
        names, order and values, except `v_score_snapshot.available_at` on TECHNICAL, VALORIZATION and SECTOR
        rows (NULL → the cycle date).
- [ ] **T-145** Non-reusable ids. → `PLAN.md` Work item 19, step 4.
      - `AUTOINCREMENT` on `cycle_run`, `analysis_run`, `pricing_run`, `quant_run` and `sec_filings`: in the
        DDL, plus a migration for existing databases (`migrate`, FR-011), tested on a copy — foreign keys,
        `T-107` triggers and views intact. The rebuild seeds `sqlite_sequence` with the highest id referenced
        anywhere (each table's own rows and every `run_id`/`filing_id` column that points to it), so an id
        deleted before the migration is not reused; the test deletes a referenced highest row and checks that
        the next id exceeds it. The seeding and `verify_pilot.py`'s run-type check share one explicit map
        from each `run_id` column to its run table (`score_snapshot.run_id` depends on `run_kind`;
        `shared_executive_edge.run_id` points to `cycle_run`).
      - `scripts/verify_pilot.py` checks that every `run_id` resolves to a run of the right type, and that
        `sec_filings.accession_number` is never NULL and is unique.
      - No production write: the orphaned edge run ids disappear with `T-100`'s fresh database.

## Work item 20 — Run-endpoint access control — DEFERRED until after `T-100`

**Deferred 2026-10-06 (user: not a production version yet).** → `PLAN.md` Work item 20; `SPEC.md` FR-015.

- [ ] **T-146** Gate the `api/` run routers: mounted only when `API_ENABLE_RUNS` is set, bearer token from
      `API_RUN_TOKEN`, refuse when none is configured; read endpoints unaffected; tests per FR-015's acceptance
      column; document in `docs/api.md`. *(Added 2026-10-06; the mechanism is confirmed when this starts.
      Numbered `T-144` in PR #124; renumbered 2026-10-06, before any work started, because the
      knowledge-graph repo already referenced `T-144`/`T-145` as the view tasks — a one-time exception to the
      stable-ID rule.)*

## Work item 12 — Final: full-universe production run (runs last of all)

Added 2026-09-25, at the user's direction. Every other task in this file is either
code, or a check on a **small sample** of assets (`T-068`, `T-088`). The full as-of
universe runs exactly once, after **everything else** is done and verified — so a
defect caught late never forces a second full-scale (and, with the LLM step, costly)
re-run. **This work item is always the last one in `TASKS.md`**; a new work item is
added above it, never below. → `PLAN.md` Work item 12. *(2026-10-05: the final pilot, `T-143`, is the
one sample-sized task placed before `T-100` in this work item; it gates `T-100`.)*

`quant`'s own current 20-ticker sample (`APA, APO, BF.B, CPT, ESS, HOOD, HUM, MA, MCD, NEE, PG,
PM, PSX, SBAC, STZ, T, UDR, WAT, WFC, XOM` — the same 20 `quant_return_daily`/`qret-v2` covers)
is scoped via a checked-in `--universe-db /workspaces/thesis/data/universe_sample20.db` (copied
from the real `universe.db`, filtered to these 20 symbols; `T-122`, 2026-09-29) — a prior
scratchpad copy of the same idea was lost between sessions (an untracked `/tmp` path), which is
why this one lives under the production data directory instead. Pass it explicitly to every
`quant` command until `T-100` runs; omitting `--universe-db` defaults to the full 503-member
universe, which `pricing_agent`/`fundamental_agent` (unaffected by this scoping) already cover
but `quant`'s own `qret-v2`/risk-model chain does not, pending `T-100`.

- [ ] **T-143** Final pilot (user, 2026-10-05): the 20-ticker sample on a **fresh database**, run through
      the Work item 2 orchestrator from a **tagged clean commit**, after Work items 8, 19 and 2 have
      merged. Accepted at `scripts/verify_pilot.py` **0 FAIL** (the reviewer extends the script for each
      merged change). It measures `T-140`'s acceptance and doubles as `T-014`'s check (one orchestrator
      run, through both HTTP and the CLI wrapper, runs everything on a fresh database). If it fails, fix
      and re-run before `T-100`. → `PLAN.md` Work item 12.
      **SEC cross-check (user, 2026-10-07, with `T-147`/`T-148`):** the pilot also runs a cross-check against the
      SEC, with network allowed, beside the offline `verify_pilot.py` (built on the tracked `scripts/sec_xcheck/` that `T-147`
      delivers). **FAIL** on any value mismatch, on any
      resolved income tax/equity/net income/short-term debt that differs from its intended SEC concept, or on
      any flipped sign. **WARN** on documented not-applicable cases.
- [ ] **T-100** *(takes over `T-068`'s former full-universe scope; also takes over `T-079`'s LLM re-run
      and supersedes `T-075`: it runs the LLM on every filing, from a fresh database, with the final
      prompts)* Run the whole
      pipeline over the **entire as-of S&P 500 universe** (all 503 assets, not a
      sample) as the very last step of the repository setup: purge the sample-era
      derived data first only if a version bump requires it (same mechanism as `T-088`; outside the
      orchestrator, and moot on the fresh database this task needs anyway) → **one orchestrator run through
      the CLI wrapper**: `pricing_agent run` → `fundamental_agent run` for every asset → `entity_resolution
      build` → `cycle select` → `quant backfill-actions`/`build-returns`/`build-risk-model`/`optimize`/
      `evaluate`. The Ring-1 `data_quality_issue` gates run inline in `fundamental_agent run`
      (`quality.gate_version`), so no separate backfill step; `fundamental_agent quality` stays a CLI-only
      maintenance command for older filings. **Depends on every other task in `TASKS.md` — every task
      open today and every task added later — except those the user deferred or superseded
      (2026-10-05, 2026-10-06)**: Work item 4, Work item 9's `T-080`/`T-081`/`T-082`/`T-084` and Work item
      20's `T-146` are deferred until after `T-100` and are not prerequisites; `T-075` and `T-079` are
      superseded by this task.
      `T-083`, `T-142`, `T-144` and `T-145` (Work item 19) and the final pilot `T-143` are prerequisites. A task added
      after this one is still a prerequisite of it, unless the user defers it the same way. `T-100`
      stays unchecked until every other box in this file is checked, or explicitly
      superseded/moved/deferred. **Acceptance**: `coverage` for
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

**🔴 Current priority order (user's, 2026-10-05; Work item 8's part re-set 2026-10-07; scope: finish this repo
first):** Work item 8 (`T-141` → `T-077` → `T-070` → `T-147` → `T-148` → `T-072` → `T-076` → `T-073` → `T-074` →
`T-071`) → Work item 19 (`T-083` → `T-142` →
`T-144` → `T-145`) → Work item 2 (orchestrator) → `T-143` (the final pilot) → `T-100` (the full-universe run, last of all).
Work item 4, Work item 9's `T-080`/`T-081`/`T-082`/`T-084` and Work item 20 (`T-146`) are deferred until after
`T-100`; Work item 3
stays superseded by `T-077` (do not implement). `T-079` and `T-075` are superseded by `T-100`. One task per
PR throughout, each ending with its status commit after approval. Work item 18 (the N-ticker weight
heuristic) closed 2026-10-05, see `CHANGELOG.md`.

Pilot-1 (2026-10-04) ran at 2 FAIL with the original verification (`T-133`, explained by `T-140`); `T-140`
merged 2026-10-05 (PR #115, acceptance pending the final pilot). **The final pilot, `T-143`, gates `T-100`**,
and `T-140` stays open until it measures that acceptance. Work item 17 (`T-131`, `T-132`, `T-133`, PRs
#109/#110/#111) closed 2026-10-02, see `CHANGELOG.md`. Work item 14 (the second forensic audit) is fully
closed 2026-09-30, `T-118` last — see `CHANGELOG.md`. Its `T-121`/`T-122`/`T-123`/`T-124` production actions
were all applied at the user's direction (each task's own entry in `CHANGELOG.md` records its
production-apply date and verification), and `T-125`'s own `migrate` remains the one deliberately-deferred
production write left over from it (still pending explicit user direction, same as
`T-104`/`T-107`/`T-120`'s own precedent); a production `dq-v2` re-gate for `T-116` is deferred the same way.
Production stays frozen until `T-100`.

Work items 1 (done), 3 (superseded by `T-077` — do not implement), 6 (done
upstream + `T-052`), 5 (done 2026-09-25, `T-044` deprecated), 7 (done 2026-09-25), 10 (done),
11 (done 2026-09-25), 13 (done 2026-09-25), 14 (done 2026-09-30), 15 (done 2026-10-01, PR #106) and 16 (done 2026-10-01) are
closed — see `CHANGELOG.md`.

**Work item 12 (`T-143` then `T-100`, the full-universe production run) runs last of all**, after
every other task in this file that is not deferred or superseded — current and future.
