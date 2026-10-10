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

**Order (user, 2026-10-05, Work item 8's part re-set 2026-10-07, `T-144`/`T-152` moved first 2026-10-09; scope:
finish this repo first):** **`T-144` → `T-152`** (the knowledge-graph view contract and its endpoint, moved first 2026-10-09: the knowledge-graph repo waits on them) → Work item 8 (`T-141` → `T-077` → `T-070` → `T-147` → `T-149` → `T-148` → `T-150` → `T-151` → `T-076` → `T-073` → `T-074` → `T-071`) →
**Work item 19** (local follow-ups: `T-083` → `T-142` → `T-145`) → Work item 2 (orchestrator) → **`T-143`, the final pilot** (Work item 12) → **`T-100`**, the
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

**Order (user, 2026-10-07; re-set 2026-10-08 after `T-147`'s audit and `phase3_decisions.md`):** `T-141` → `T-077` →
`T-070` → `T-147` → `T-149` → `T-148` → `T-150` → `T-151` → `T-076` → `T-073` → `T-074` → `T-071`.
- **Why:** `T-077` (the Carhart estimator over prices and Kenneth French factors) and `T-070` (the price-based
  technical score) read no SEC statement data, so they run while the user builds the SEC data-treatment checklist
  (`T-147`'s input). The audit (`T-147`) then sorts the defects into four tasks, in the order of their dependencies:
  **`T-149`** (which filing and which period: amendments, CIK, period end, period ranges, and `earnings_yield`, the
  one fix that changes results already produced) → **`T-148`** (which line item and which sign) → **`T-150`** (the
  accounting identities, which check `T-148`'s equity and net-income choices, so they run once, after it) →
  **`T-151`** (the metric definitions, which absorb `T-072`'s EBITDA and `T-076`'s growth item). The tasks that
  build on SEC-derived metrics (`T-076`, `T-073`, `T-074`, `T-071`, `T-153`) wait for them.
- **`T-072` and `T-076`:** `T-072` (EBITDA persisted; net debt net of short-term investments) is fully absorbed by
  `T-151` and is not implemented separately. `T-076` is **not** absorbed: only its YoY-growth item (audit F8) moves
  to `T-151`; the rest (the 10-Q preamble, the valuation magnitude gate, the profitability skill, the quick-ratio
  definition, the `SKILL.md` trim) runs as its own task after `T-151`, and its quick-ratio item follows `T-151`'s
  dictionary (D-05: "source pending").
- **Decisions in force:** `docs/checklist_sec/phase3_decisions.md` (D-01 to D-14 and §7–§10); the frozen checklist
  is `checklist_v1.3.md`; the audit's numbers are in `docs/sec_data_checklist.md`.

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
- [x] **T-147** — **done, PR #127; checklist v1.3 follows from `docs/sec_data_checklist.md` §9.** SEC data-treatment checklist audit (user, 2026-10-07; docs, a script and tests -- **no change
      under `src/`**). Audit the data path (EDGAR → `portfolio-data-mining` gateway → `fundamental_agent` →
      `cycle`/`quant`) against the SEC data-treatment checklist. **Audits `checklist_v1.2.2.md`** (frozen and approved
      2026-10-08), built from SEC, FASB, XBRL US DQC, Nareit, academic and edgartools sources (the user
      owns the sources and the NotebookLM rounds; Claude reviews the draft). → `PLAN.md` Work item 8, step 10.
      **Delivered 2026-10-08 on `docs/t147-sec-checklist-audit`; approved 2026-10-09 after the review's R1-R6** (PR #127):
      `docs/sec_data_checklist.md`, `scripts/audit_sec_checklist.py`,
      `scripts/sec_xcheck/` (the cross-check scripts, promoted and tracked), `tests/test_sec_xcheck_*.py`, the
      company-type table, the L-09 register, the golden set and the fix plan below.
      **Deliverables:**
      - `docs/sec_data_checklist.md` (tracked). Per rule ID: the implementing file/function, or `none`; status
        ✓ / partial / ✗ / not applicable (with the reason); evidence (the companies and filings violating it, up
        to 5 example tickers); the check that tests it.
      - `scripts/audit_sec_checklist.py`: network; read-only; at most 10 requests per second; every response
        cached. SEC User-Agent: `SEC_USER_AGENT` when set (the convention of
        `fundamental_agent/filing_text.py`), else the default `research@example.com`; the PR records the
        contact used for the real run.
      - The cross-check scripts the acceptance of `T-148` and `T-143` rely on (`itemcheck.py`, `signcheck.py`,
        `precedence.py`, untracked under `docs/checklist_sec/sec_xcheck/` until `T-147`) are promoted to **tracked**
        `scripts/sec_xcheck/`, with the paths `T-148` and `T-143` name updated, so a clean checkout can reproduce
        their results (`uv run python scripts/audit_sec_checklist.py fetch` fills the gitignored SEC cache; the
        scripts take `--db` and `--cache`, no path is hard-coded). Their SEC data is not committed.
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
- [ ] **T-148** Line-item resolution and sign correctness (engine `metrics-v6`; `fundamental_agent`; the gateway repo
      for the `preferred_sign` and `unit` fields). D1–D4 below, plus what `T-147`'s audit measured
      (`docs/sec_data_checklist.md`). **Follows `T-149`.** → `PLAN.md` Work item 8, step 11. *Text amended
      2026-10-08 by `T-147`'s PR: D-01, D-02, D-06 and D-09 of `phase3_decisions.md`, and the audit's measured scope;
      2026-10-09 by the PR #127 review: (f) gains N22 and the residual capex item of `T-071`, and D6 is carried by (g).
      Numbers are production's 5,075 filings and the 17,350 filing-columns the pipeline reads (the target, the prior,
      a 10-K's CAGR base, a 10-Q's year-to-date pair); a filing is counted once.*
      - (a) **Filed sign (ID-11, ID-12).** Store edgartools' `preferred_sign` (an additive column on `financial_facts`).
        `Statements.get` returns line items in the filed XBRL sign; the `T-117`/`T-140` rebuild arithmetic stays on
        displayed values. **Reject a wrong sign and flag it; do not repair it with `abs()`** (D-06(a); this replaces
        the earlier "keep the `abs()` guards", which contradicted ID-11): the `abs()` in `cashflow`, `valuation`
        and `leverage` go once every item is on its filed sign. Measured against the SEC: the displayed income tax is
        the negative of the filed value in 1,237 filings (134 companies),
        cost of goods in 211 (27), interest expense in 1,639
        (201); capex is displayed negative in 4,351, so `abs()` is what makes it right
        today and removing it first would flip free cash flow. (Pilot, 2026-10-06: 86 filings with the wrong income-tax sign,
        `effective_tax_rate` forced to 0, feeding ROIC and enterprise FCF yield; `preferred_sign = −1` matched the flipped
        rows 200/200 on live payloads.) **Existing databases:** `financial_facts` is append-only on `(filing_id,
        statement, concept, period_key, filing_version)`, so a same-accession re-run leaves old rows without
        `preferred_sign` (NULL). There is no in-place backfill: a NULL reads as today's displayed sign (recorded), the
        fix takes effect on a database rebuilt with the branch, and the pilot `T-143` and `T-100` both start from fresh
        databases. The `model_fixes.md` entry states this.
      - (b) **Precedence (ID-02a, ID-03).** Exact concepts first, in the spec's order, then `standard`, then
        `label_contains`. Net income: `NetIncomeLoss` (attributable to the parent) before `ProfitLoss`, a methodology
        decision, recorded, with its own regression test (a filing that files both); `ProfitLoss` minus
        `NetIncomeLossAttributableToNoncontrollingInterest` when only `ProfitLoss` is filed. Equity:
        `StockholdersEquity` first. Measured: equity read from another row in 38 filings
        (5 companies; PM +22.6B vs −12.6B); net income includes NCI in 2,247
        filings (263 companies; 237 on the pilot). **N13, to decide:**
        83 filings (15 companies, WAT among them) take the
        available-to-common line, which is after preferred dividends and is not parent net income.
      - (c) **Income tax (ID-02b; D-06(c)).** Never a single component through `standard`. Current + Deferred when both
        are on the statement face and the total is absent, else None (the default rate applies); the source is B6's
        definition of `IncomeTaxExpenseBenefit` (current plus deferred, both "pertaining to continuing operations").
        Measured: a component or standard-concept match in 55 filings (9 companies);
        current and deferred both filed without the total in 19 filings (APA).
      - (d) **Debt and cash (MET-07, ID-02b, MET-00; D-01, D-06(d), N23).** Short-term debt: `LongTermDebtCurrent` +
        `ShortTermBorrowings` first (disjoint by definition); `CommercialPaper` only when `ShortTermBorrowings` is
        absent; `DebtCurrent` last, flagged "includes leases". A map with one term missing (D-01): the filed terms are
        accepted, flagged "partial map", only when long-term debt is present and the rest of the balance sheet
        resolved; short-term debt alone leaves debt empty; **missing cash never qualifies** (EV is empty), and
        EV / ROIC stop filling a missing debt or cash with 0 (D10, MET-00). Long-term debt: the combined debt-and-lease
        element only when it is the only long-term line, and then flagged "includes finance leases"; a line that
        includes current maturities is not added to `LongTermDebtCurrent` (N4). **ID-02b:** debt = 0 only with no debt line
        at all, no interest concept, and a complete balance sheet (Regulation S-X 5-02 p.1); otherwise a capture failure.
        **N23 (v1.3):** debt and interest concepts not read today: REIT debt, `OtherLongTermDebtCurrent`, `NotesPayableCurrent`
        (filed by ED) and `InterestExpenseDebt`; plus interest expense is stored with mixed signs (202 negative of 319 in
        production), to be checked against MET-09's coverage rule. Measured: the D-06(d) order changes 344 filings (53 companies);
        on the filing's own period 840 of 5,075 have no debt line (663 non-financial; 319 file a read interest concept and 177 an unread one;
        Real Estate is 212), 1,206 long-term only, 85 short-term only; a missing debt or cash term would be filled with 0 in 4,994 filings;
        N4 25 filings (7 companies); the combined element is the long-term line in 1,380 filings (155 companies); N5 (cash counted twice)
        4 filings; N6 (a balance sheet read from another date) 8 filings.
      - (e) **Revenue, cash flow and the other line items.** *Operating cash flow (CON-08, N3):* the total concept
        before the continuing-operations one; 191 filings (34 companies) read the
        continuing line, 66 of them on their own period and the rest in the year-to-date pair and the prior
        the TTM reads. *D&A (N14):* `Depreciation` alone is not D&A: 878 filings (95 companies).
        *Revenue as a sum (D-02, CON-11):* a sourced map, allowed only when the components are the only revenue lines
        in the statement's revenue section (B4: "The Revenues element would be used for the total revenue (ASC 606 and
        non-ASC 606 revenue)"); otherwise revenue stays empty. Today 73 filings (5 companies) take a sum.
        *T-140's rebuild (L-09, D-03b):* real-time net-anchor test (PER-01, no lookahead). An anchor is
        refused when its value less the rows between is below a revenue-concept row of the same filing and
        column (the test uses only the filing itself), not by its label; measured: it rejects ICE's 26 columns and no
        other company's. The ex-post audit only reports and never removes a value (PER-01: the next year's column did
        not exist on the date of use). **N20, high:** 17 ICE values rest on the net-revenue line "Total revenues, less
        transaction-based expenses" and every checkable one disagrees with the next year's comparative column
        (derived 1,235M against 2,387M for 2022Q3); the rebuild also fires for PSX, NI and MPC, not only APA.
        **N19:** the label exclusion `\bcost\b` does not match "costs" ("TOTAL COSTS OF REVENUES" anchored 16 ADP columns
        before the guard refused them): use `\bcosts?\b`. *Banks' revenue (APP-01, F5):* 10 of the
        24 Article 9 filers have no resolved revenue and are hard-vetoed by `DQ_REVENUE_POS` (BNY: 33 refused
        rebuilds anchored on "Total fee revenue"); read those statements by hand first and decide, as D-02 does, whether
        a sourced map from Regulation S-X 9-04's lines exists.
      - (f) **Capex (MET-08, APP-04b).** Scope from the measurements: the target differs from today's value in
        180 filings (25 companies); capex is `PaymentsToAcquireProductiveAssets` unflagged in
        829 filings (99 companies), a caption (a custom concept, software not excluded: VZ −20.3B,
        COP −11.2B) in 125 (16), the net element in 56; mineral-interest
        purchases are capex contrary to (a) in 10 filings (EOG); (c)'s tie-break fires in
        5 (FANG); REIT acquisition lines are left out in 121 filings (14 companies) and
        `PaymentsForCapitalImprovements` overlaps an acquisition line in 4; capex is absent where a cash-flow statement is
        present in 490 filings. Capex of DTE, ED, LNT and NEE does not resolve today (company extensions,
        `PaymentsToAcquireOther...`) and is resolved under MET-08. The FANG/APA table of `docs/sec_data_checklist.md` is the cost of excluding routine
        leasehold purchases.
        **N22 (review of PR #127; DOW).** The oil & gas fallback tiers fire before the caption fallback, so a filer that
        also files a PP&E-family line resolves the wrong capex: DOW FY2025 (`0001751788-26-000018`) resolves 0.157B
        (`PaymentsToExploreAndDevelopOilAndGasProperties`, "Investment in gas field developments") against 2.479B filed as
        `PaymentsToAcquireMachineryAndEquipment` ("Capital expenditures"), so its free cash flow is overstated by about
        2.3B (1.4B to 2.7B a year over FY2021 to FY2025) and the error reaches `NEGATIVE_FCF` and the FCF yields; KKR's
        FY2021 and FY2022 10-Ks resolve 0 from a zero oil & gas line beside a filed `PaymentsToAcquireFurnitureAndFixtures`.
        Moving the tiers behind the caption would not be enough: DOW's FY2025 statement carries a second
        "capital expenditures" caption (a cash-flow hedging row), and the caption fallback refuses two. Measured
        (`audit_sec_checklist.py measure --only capex`, `N22.*`): 31 filing-columns (7 own-period) of DOW and KKR where the
        oil & gas tiers displace a line; 165 filing-columns (11 companies: ADP, BAX, CPT, INCY, INVH, ROP, RL, VTR, VZ,
        WDAY and WEC) where the same kind of line reaches the right value only through the caption fallback, unflagged,
        and 252 (12 companies) where it resolves no capex at all. **Fix:**
        - the oil & gas tiers apply only when no PP&E-family line is filed;
        - add `PaymentsToAcquireMachineryAndEquipment`, `PaymentsToAcquireOtherProductiveAssets`,
          `PaymentsToAcquireOtherPropertyPlantAndEquipment` (alone; the oil & gas tiers keep adding it, EOG) and
          `PaymentsToAcquireFurnitureAndFixtures` under MET-08 (b), each with its B6 definition (B6 holds all four since
          2026-10-09, from the FASB 2026 release; a concept whose definition is missing from B6 is added there first,
          no rule without a source);
        - under MET-08 (c), a PP&E-family line beside a separate oil & gas line on the same statement is added and flagged
          (e.g. DOW: USD 2.48B + 0.157B), not chosen one over the other;
        - the residual capex item that stood under `T-071` since `T-133`'s review (2026-10-02: filers whose capex is
          `PaymentsToAcquireOtherPropertyPlantAndEquipment` / `...OtherProductiveAssets`, which `T-133`'s single-line
          lookup leaves empty rather than partial) is now this bullet's, because capex is `T-148`'s scope.
      - (g) **Share counts and the other flags (ID-18, MKT-01 to MKT-04, ID-01, ID-09/16, ID-20, ID-13 to ID-15, PER-06).**
        The share-scale corrector **quarantines instead of correcting** (ID-18): Annex A-03 catches the MCD 10⁶ case
        without the correction (6 of 6 corrected filings). Stored valuation: remove the balance-sheet and diluted
        fallbacks (MKT-04; production's `metrics-v2` rows use the diluted average in 238 of 258), put the share count
        and the price on one split basis (MKT-02, D6), value a multi-class company class by class (MKT-03; 50 of 500
        CIKs have no non-dimensional cover total). Flag every extension, standard-concept or label match (ID-01), a
        total equal to a dimensional member (ID-09, ID-16; not replayable from storage, so a gateway-side flag), and
        remove the four dead names (ID-20; no effect in the window). Reject a negative value of an element the
        resolver reads that DQC_0013/0014/0015 list (ID-13 to ID-15; one filing). **PER-06 (D-09):** values built on
        `x4` leave the score (empty), as `T-133` already does for the cash-flow metrics, and the rule's severity becomes
        BLOCK for x4.
      - (h) **Records.** Engine `metrics-v6`; a `docs/model_fixes.md` entry citing the checklist rule IDs and their
        sources (constitution AI behavior #12); regression tests: PM equity, the STZ/APO tax sign, the APA tax
        component, the STZ COGS sign, net income parent-vs-NCI, short-term debt, the N3 total-before-continuing
        order, the ICE case (left empty), a bank's revenue, **DOW's capex (N22: `tests/test_sec_xcheck_resolver_measures.py` pins the
        wrong 0.157B today; the resolver test asserts 2.479B)**, and the tests that **today pin the wrong behaviour** and are
        replaced: `tests/test_share_scale.py` (it pins the correction), `tests/test_metrics_valuation.py` (it pins the
        diluted fallback), and the map-pinning cases of `tests/test_revenue_rebuild.py` and `tests/test_statements.py`.
      - (i) **Acceptance.** On a database rebuilt with the branch, the tracked cross-check scripts
        (`uv run python scripts/sec_xcheck/itemcheck.py --db <rebuilt>`, `signcheck.py`, `precedence.py`; the SEC cache from
        `audit_sec_checklist.py fetch`) report 0 FLIPPED and no OTHER_CONCEPT for income tax, equity and net income
        (the 237 NCI filings resolve to `NetIncomeLoss`), and `uv run python scripts/audit_sec_checklist.py measure
        --only resolver,capex,source` shows 0 for N3, N4, N5, N6, N14, N22 (`N22.og_tier_displaces_capex_line`) and the D2/D3 rows, with every number in the PR.
        The L-09 register (`audit_sec_checklist.py l09`) has no value that fails its ex-post audit.
      - (j) **Overlap with `T-070`.** `T-070` (earlier in the order) recalibrates `LIQUIDITY_DISTRESS`. `T-147` states
        whether any input of that rule, or of another score or veto `T-070` touches, is changed by `T-148`: yes. The
        current ratio's own inputs (current assets and liabilities) are not touched, but the cash-coverage inputs
        `T-070` adds are: operating cash flow (N3), interest expense (the filed sign, `abs()` removed) and cash (N5);
        the debt inputs of `LEVERAGE_EXTREME` change too. `T-148` therefore repeats `T-070`'s calibration check on
        the rebuilt data and reports it.
- [ ] **T-149** Filing identity (engine `facts-v1` bump if stored facts change; `fundamental_agent`, `cycle`; the
      gateway repo for the period-end field). New from `T-147`; precedes `T-148`. **Must be done before T-153.**
      → `PLAN.md` Work item 8, step 12. Rules PER-02, PER-05, PER-08, ID-07, ID-10, APP-10a, APP-10b, MET-05 (N2).
      These points ensure a company's metrics can never use another company's data. Measured on production:
      - (a) **Amendments (N1, PER-02, L-04).** Original filings are never replaced: the gateway lists a company's
        10-Ks with amendments, `_select_filings` keeps the most recent, and `upsert_filing` is `ON CONFLICT DO UPDATE`.
        On the SEC history the rule would keep a 10-K/A of the same fiscal year in 55 company-years
        (36 companies); 8 stored accessions are amendments; 48 original 10-Ks of
        30 companies are missing because a 10-K/A exists (a Part III-only amendment has no statements, so the run recorded an
        extraction failure and the year was lost); 18 10-Q/A filings of 15 companies share a
        quarter label with their original. **10-K/A and 10-Q/A are never ingested** (L-04: filings are taken as originally
        reported; a restatement is out of scope): `_select_filings` drops a reference whose form ends in `/A`, and the
        original filing of the period is selected even when an amendment of the same year exists. Storing an amendment
        under its own key would not work: `cycle/data.py`'s `latest_metrics` and `data_quality` order a company's
        filings by `period_end DESC, available_at DESC`, so an amendment of the same period end with a later
        `available_at` would still replace the original in every read. **Test:** replaces
        `tests/test_pipeline_multi_filing.py::test_a_10k_keeps_only_the_most_recent_of_several_matches`, which pins the
        replacement.
      - (b) **Lost filings that are not amendments (N21).** 9 10-Ks of 52/53-week filers (AVY, CDNS, DPZ, JNJ, RVTY,
        SNA, SWK, TDY, TXT) were lost to a repeated `FY` label: `T-140` fixed the labelling and this database predates
        it, so the final pilot's fresh database is the check. Two remain unexplained (EXE: a FY2021 10-K stored as
        FY2020; SMCI FY2024) and three have a recorded gateway failure (GEHC, CSCO, SMCI FY2026): find the causes.
      - (c) **Fetch by CIK, never by ticker (APP-10a, ✗ today).** Today every stored accession belongs to its asset's CIK
        (5,076 filings, 0 outside its submissions), so fetching by ticker has not mis-assigned a filing here, but only
        because today's members don't reuse tickers. With the historical universe (renames such as FB→META, reused tickers
        such as 21st Century Fox's FOXA and FOX, companies that left the index) fetching by ticker would mix companies.
        **A database invariant:** every stored filing belongs to its asset's CIK, checked against the SEC's submissions list,
        and no filing is attached to two assets. A write that breaks either one fails.
      - (d) **Co-registrants (ID-10, partial and untested today).** A test with a real combined utility 10-K (parent and
        subsidiaries in one filing) proving that only the parent's facts, the ones without a legal-entity member, are used.
      - (e) **Period end (ID-07) and ranges (PER-08).** Compare the stored period end with `dei:DocumentPeriodEndDate` /
        the header `periodOfReport` (needs a gateway field); match periods by the rule's month-end rounding and the day
        ranges [364, 371] year, [89, 98] quarter, [181, 189] six months, [273, 280] nine months instead of the 20-day
        tolerances. Measured: 1 filing differs (EXE); 1 quarter pairing is
        accepted by the code and rejected by the rule, 1 fiscal-year pairing is not a year apart.
      - (f) **PER-05 (D-08).** Q4 = FY − the Q3 10-Q's nine-month year-to-date, which needs no three recorded quarters.
      - (g) **N2: `earnings_yield` (MET-05).** `orchestrator._valorization` reads `profitability.net_income` /
        `income_statement.net_income`, neither a stored metric (0 of 34 metric names), so every real cycle ranked
        VALORIZATION without it (the whole value factor is empty in 35 of 40 stored rows). Compute it
        from the trailing-year net income and the as-of market cap, with MET-05's negative-earnings dummy
        (Fama and French; the positive part is used). **Effect reported separately** from the data-treatment
        changes, as a scoring bug (D-11 decomposition: (0) production as it is, (1) this fix alone on `metrics-v5`,
        (2) plus the new metric engine).
      - (h) **Successions (APP-10b), low priority.** Detect `8-K12B` / `8-K12G3` / `8-K15D5`: 18 of 503
        companies filed one since 2022; linking a successor to its predecessor needs a hand reading of each 8-K.
      **Acceptance:** each bullet's measurement re-run on a rebuilt database (`audit_sec_checklist.py measure --only
      n1,periods,extra`) shows 0 amendments stored, 0 originals lost to an amendment, the N2 effect separately; regression
      tests for (a), (c), (d), (e), (f), (g); the replaced test named above; database invariant enforced on writes; co-registrant
      utility test passes.
- [ ] **T-150** Accounting identities with DQ-01 and L-10 (`fundamental_agent/quality.py`; gate version bump). New from `T-147`;
      runs after `T-148`, whose equity and net-income choices CON-04 and CON-19 verify, so the identities are measured once.
      → `PLAN.md` Work item 8, step 13. Rules CON-01 to CON-04, CON-06, CON-07, CON-19, CON-08 (as a check on `T-148`'s
      choice) and DQ-01 (**threshold = max(rounding, 0.5% × |base|)**: within rounding passes, above rounding and below
      the threshold WARNs, above it is material and the inputs are blocked). **L-10 / DQ-01 rounding term:** `0.5 × unit × terms`,
      where the unit is inferred when `decimals` is not captured (L-10). The identities read the stored facts of one
      filing and period (no dimensions), not the metrics, so they are a new gate beside `DQ_*`.
      Measured (DQ-01 BLOCK, own period): CON-19 44 filings of 2,045 at the source (8 companies) and
      36 on the stored rows; CON-02 24 of 237 (3 companies: AEP, JKHY, PPL); CON-01 0 at the source,
      3 stored (KKR); CON-03 and CON-07 nothing at the source; CON-04 4 stored, none at the source (the equity row the
      resolver chose). CON-06 (the TTM cross-check, 1% relative, SOFT today) aligns to DQ-01. **Acceptance:** the gates
      reproduce these counts on the same data before `T-148` and show the change after it; hermetic tests on fixtures of
      a failing and a passing identity per rule, and on the tolerance boundary.
- [ ] **T-151** Metric dictionary and definitions (engine `metrics-v7`; `fundamental_agent/metrics`). New from `T-147`;
      **absorbs `T-072`'s EBITDA** (persist EBITDA = operating income + D&A; net debt subtracts short-term
      investments, audit F7) **and `T-076`'s growth item** (YoY growth empty when the base is ≤ 0, audit F8; `T-076`
      keeps its skills redesign). → `PLAN.md` Work item 8, step 14. Delivers `docs/metric_dictionary.md`: generated
      directly from Annex B of `checklist_v1.3.md` (formula, one source, inference flag, 17 formulas "source pending" per D-05
      until the second author is loaded); renames the audit key `free_cash_flow_to_equity` to `free_cash_flow` (OCF − capex, not FCFE).
      Definitions and the measured scope:
      - **MD-02 and MD-35:** invested capital and EV include noncontrolling interest (as MD-02 defines it: redeemable NCI
        included between liabilities and equity; CON-04's identity when no balance is filed; 0 only with no NCI income;
        otherwise a capture failure) and net short-term investments (when filed apart from cash); **MD-38:** (NOPAT (MD-05,
        MET-03's 21%) − (capex (MET-08) − D&A (MD-01) + Δ non-cash working capital)) / EV (MD-35), with non-cash working capital
        = (current assets − cash − short-term investments) − (current liabilities − short-term debt), its change between the ends
        of the prior and the current year; empty when a term is missing or the balance sheet is not classified (REITs, banks).
      - **The applicability matrix as data:** v1.3's matrix (35 metrics × 6 company types → applies, adjusted, or not applicable,
        with the reason) becomes a data file the code reads, instead of conditions scattered through the code.
      - **Traceability test:** fails when a metric in the dictionary has no implementation, when an implementation, map entry or
        veto has no rule ID, or when the code and the matrix disagree.
      - **Provenance on every metric:** its filing (accession), the XBRL concept of each line item, the rule IDs applied, the
        flags, and the reason when it is left empty.
      - **Company card:** for a CIK and a date it prints the profile, the filing used, each line item's concept and value, the
        rules applied and the result. Used for the thesis and for debugging.
      - **Gate after every rebuild:** T-147's comparison with the SEC's own data (same CIK and filing) and the golden set
        (`golden_set.csv`), with a minimum fidelity. A value that doesn't match its own company's SEC data can't reach the scores.
      - **MET-01 / MET-02:** ROA = operating income × (1 − t) / total assets at the **start** of the year, not
        applicable to financial firms; ROIC and ROE divide by capital at the end of the prior year, never the ending
        capital (ROE for banks stays, C2). A 10-Q's "end of the prior year" is the prior 10-K's balance.
      - **MET-03 / APP-04c:** NOPAT's tax is the 21% federal marginal rate for everyone except REITs (their own
        effective rate when pre-tax income is positive, else 0), tax 0 on an operating loss. Measured:
        NOPAT uses the filing's own rate in 4,320 filings (471 companies); it is reduced by a tax on an operating
        loss in 198 of 203 loss filings; **N10:** `effective_tax_rate` shows 21% when pre-tax income is
        missing or ≤ 0 in 743 filings (172 companies; L-05 says empty); the 21% default hits
        125 of 314 REIT filings (15 companies). MET-03 also reports ROIC at the effective rate as robustness.
      - **MET-09 / N9:** a negative denominator is left out of the continuous score and flagged, not discarded with the
        sign: **N9** — 177 filings (63 companies) have net cash and positive EBITDA, which T-116 treats as unresolved;
        net debt/EBITDA with EBITDA ≤ 0 in 41 filings and FCF conversion with net income ≤ 0 in 160.
      - **MET-04 / PER-12 to PER-14:** growth from a base ≤ 0 is empty and flagged (net income 394 filings,
        free cash flow 328, operating income 242); a growth or CAGR across the adoption of
        a standard without restated comparatives is flagged: 4 CAGRs cross ASC 842 and 32 lenders'
        net-income CAGRs cross CECL.
      - **N15 / APP-07:** ROIC only when invested capital > 0 (74 filings today); ROE and D/E are not
        meaningful on negative equity (316 filings, 51 companies). The metric's value is empty; the cycle's
        ranking decision (D/E = +∞) is `T-071`'s.
      **Acceptance:** `docs/metric_dictionary.md` generated from Annex B; applicability matrix data file loaded;
      traceability test passes; metric provenance and company card delivered; post-rebuild SEC fidelity gate enforced;
      the audit's `metrics` group re-run on a rebuilt database shows 0 for N10, N15 and the MET-03/MET-09 rows;
      regression tests per definition on real captured fixtures; a `docs/model_fixes.md` entry.
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
      "Residual scope". **The last item (the `OtherPropertyPlantAndEquipment` / `OtherProductiveAssets` filers) moved
      to `T-148`(f) on 2026-10-09, with N22: capex is `T-148`'s scope; the utility and REIT items stay here.**)*
      *(2026-10-08, `T-147`'s audit and `phase3_decisions.md` D-04, D-07, D-13, D-14: scope addition.)*
      **A company profile as of the date (APP-00, ✗ today; code uses today's GICS sector only):** one profile per
      `(CIK, date)`, read from `docs/checklist_sec/company_types.csv` (delivered by `T-147`). The CSV has ONE base type
      per company, the `type` column: `operating`, `article_9`, `article_7`, `other_financial`, `multi_sector_holding`.
      On top of it come OVERLAYS: REIT (`reit`), multi-class (`multi_class_listed`), negative equity (evaluated from the
      filing as of the date, not `negative_equity_latest`), and regulated utility (GICS sub-industry Electric, Gas, Multi-
      or Water Utilities, D-13). The CSV has no validity dates today (it is one snapshot): `T-071` adds them, and `T-153` adds
      the leavers' rows. Every package reads the company profile (type + overlays) from it and nowhere else.
      **D-13 (regulated utilities exemption from `NEGATIVE_FCF`; see `docs/checklist_sec/checklist_v1.3.md` Scope, D-13):**
      regulated utilities (GICS Electric, Gas, Multi- and Water Utilities) are exempt from `NEGATIVE_FCF`. In its place, a HARD
      veto when the three-year average of operating cash flow before working-capital changes over total debt is below 5% (Moody's
      scorecard, C13 PDF p.7: Ba 5%–13%, B 1%–5%; C13 p.11, p.14, p.19, p.21; C14 p.38; C15 p.2). Each year's ratio uses only
      data public on D (the latest 10-K's three years of cash flow, the third year-end debt from the previous 10-K); working
      capital is current items only, regulatory assets and liabilities stay in the cash flow (C13 p.14); the debt is MET-07's
      (not Moody's adjusted debt, declared); missing inputs: no veto, flagged; CEG and VST fall inside the exemption through the
      GICS proxy, AES and NRG do not; report the 5%–12% sensitivity.
      **D-14 (REITs and `NEGATIVE_FCF`, open):** measure how many REITs `NEGATIVE_FCF` vetoes in the replay first, then
      decide (APP-04b puts acquisitions in REIT capex, and external funding of negative free cash flow is industry practice).
      **D-04 (accepted):** `DQ_NEG_EQUITY` becomes **quarantine only** (D/E and ROE read NA, as APP-07 says) and is
      never a HARD data-quality veto; the distress test for negative-equity firms moves to `LEVERAGE_EXTREME`, keyed on
      the **equity sign**, never on a negative D/E (the rules read the quarantined metrics, so today the
      negative-equity branch never runs). Four conditions: (1) **valid inputs only** — the rule reads the equity sign,
      interest coverage and net debt/EBITDA directly; (2) **EBITDA ≤ 0 is a distress signal** — a net debt/EBITDA that
      MET-09 leaves empty never reads as "no signal"; T-116's calibrated condition (EBITDA ≤ 0 with positive net debt)
      is kept and handled explicitly; (3) **not applied to financial firms** (APP-02; needs the company-type table,
      `docs/checklist_sec/company_types.csv`, APP-00); (4) **the thresholds are a calibrated cycle policy**, declared
      with a sensitivity analysis and the veto count by sector. Needs `T-151`'s EBITDA. Measured (latest 10-Ks,
      2026-09-22): 29 companies have negative equity and T-116's three conditions flag 4 of them (CCI, IRM, SBAC, WYNN),
      with or without the EBITDA ≤ 0 condition; no other fires.
      **D-07 (decide after the audit):** exclude the financial firms from the universe explicitly (C4 p.3), or keep them
      with sector-specific factors and corrected vetoes — the current accidental state is kept in neither case.
      Audit input: 51 financial firms (24 Article 9, 17 Article 7, 10 other) are about 7.5% of the approximate market
      capitalization; 14 of 51 are hard-vetoed (10 Article 9 filers by `DQ_REVENUE_POS`, 3 by `NEGATIVE_FCF`, KEY by two
      margin gates), so the vetoes do not exclude almost all of them. The decision rests on those universe-level figures (the
      market-capitalization share and the veto counts): the pilot replay (the Financials sector averages 22.8% of its
      book, 13.7% without them) covers 20 names, 3 of them financial firms, so it is not representative of the
      universe, and production has stored metrics for only 377 filings, so a full-universe replay is not possible
      before `T-100`. The decision rule (§7 of `phase3_decisions.md`): if the vetoes already
      exclude almost all and the sector factors are costly, exclude; if the thesis benchmarks against the full S&P
      500, keep them.
      **Also here:** the APP-00 overlays and the NA matrix in scoring (APP-01 to APP-04c); DQ-02's one constant,
      winsorizing at 0.5% per tail: ⌊0.005 × n⌋ values per tail (at n = 495, 245 assets move by more than a point; at n = 20
      the two fractions are identical); MET-00's neutral 50 in `valorization.compute` (never fires in the stored cycles,
      0 of 40; keep the guard); and the stored-valuation market cap reading the as-of reader's cover count.
      N2 (`earnings_yield`) moved to `T-149`.
- [ ] **T-072** *(**Absorbed by `T-151` 2026-10-08 — implement there, not here.**)* Persist EBITDA in `fundamental_metrics`
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
- [ ] **T-076** *(Its YoY-growth item, audit F8, is absorbed by `T-151` 2026-10-08; the rest stays and runs after `T-151` (PR #127 review, 2026-10-09), its quick-ratio item following `T-151`'s dictionary: D-05, "source pending".)* Skills redesign: 10-Q frequency preamble, tightened
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
- [ ] **T-153** The historical universe (after `T-071`, the last task of Work item 8, and before Work item 19; a prerequisite of `T-100`; needed for PER-10 and PER-11;
      review points 1 and 4 of `phase3_decisions.md` §9–§10 and `checklist_v1.3.md` L-11). → `PLAN.md` Work item 8, step 15.
      - Point `KG_UNIVERSE_DB` at `/workspaces/thesis/data/universe_history.db`, built by data-mining PR #49.
        Never overwrite the pilot's `universe.db`.
      - Read `valid_to` as the last day of membership: `kg_schema/queries.py:124` changes from `valid_to > ?`
        to `valid_to >= ?` (today `valid_to > D` drops a leaver on its last day).
      - For the 86 companies that left the index in the window: their filings by CIK, their type in
        `company_types.csv`, and their GICS classification (the historical source is still to be decided).
      - Then run the replay's figures (vetoes by sector, financial firms, utilities, the D-13 three-year average)
        again on the historical universe before they go into the thesis.

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
each ending with its status commit after approval. → `PLAN.md` Work item 19. Order: `T-144` → `T-152` first (moved ahead of Work
item 8 on 2026-10-09: the knowledge-graph repo is waiting on the contract), then `T-083` → `T-142` → `T-145` after
Work item 8. `T-144` and `T-145` record the knowledge-graph view changes the user approved 2026-10-06
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
- [x] **T-144** — **done, PR #129.** The knowledge-graph view contract, one additive change (`kg_schema` views, DDL notes and
      `docs/kg_schema.md`). → `PLAN.md` Work item 19, step 3.
      **Moved first, 2026-10-09** (with `T-152`, ahead of Work item 8; nothing in Work item 8 changes a view).
      Production is frozen until `T-100` and stays at `schema_version` 8 (`m009`, the veto stints, was never
      applied to it): the acceptance builds the views on a *copy* of it and on the pilot, and the knowledge-graph
      repo keeps its floor at or below the database's own version for the production database. `v_quant_vs_live` loses two filters, so it returns more rows: the PR lists every reader
      (today only tests, e.g. `tests/test_quant_pipeline.py`) and shows each one updated or unaffected.
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
      - **Status (2026-10-09): done, PR #129** (approved 2026-10-09). Marker migration is `m010` (`schema_version` 10).
        Production's `financial.db` is at `schema_version` **8**, not 9 (`m009`, the veto stints, was never
        applied to it); it stays frozen until `T-100`. Work item 19 still has open tasks, so nothing moves to
        `CHANGELOG.md`.
- [x] **T-152** Publish the `v_*` view contract over the API (knowledge-graph request, local
      `docs/kg_requirements/kg_handoff_view_contract_endpoint.md`; accepted by the user 2026-10-09; SPEC FR-014).
      Metadata only, never rows. **Follows `T-144`.** → `PLAN.md` Work item 19, step 5.
      - `GET /api/v1/contract`: `contract_version` (the highest migration version the running code knows,
        `kg_schema.migrations.MIGRATIONS`), `code_version` (`kg_schema.provenance.code_version()`), and `views`:
        every view of `kg_schema.views.VIEWS`, in its order, each with `name`, `frozen` and its `columns` in order
        (names; a type only if it is free). Built from the code, by building the views in an in-memory database the
        way `tests/test_kg_schema.py` does and reading each view's columns, never from a hand-kept list. `frozen`
        comes from a machine-readable set in `views.py` (today `v_universe_membership` is frozen only in a
        docstring).
      - `GET /api/v1/contract/database`: `schema_version` (`MAX(version)` of `schema_version`; 0 when the table is
        missing or empty), `views_present` and `views_missing` against `VIEWS`, the database opened `mode=ro`.
      - Both in a read router (FR-014): `mode=ro`, no side effects, the read routes' authentication and nothing more.
      - The rule, in `docs/api.md` and `docs/kg_schema.md`: within one `contract_version` columns are only added,
        never renamed, removed or reordered; every migration raises `contract_version`, whether or not it changes a
        view.
      - Tests: the response lists exactly the views and columns of the in-memory build, in order (a view added to
        `VIEWS` and missing from the response fails); `contract_version` equals the highest migration;
        `/contract/database` on a partial database names the missing views; no write-capable connection in the
        router module (FR-014's check).
      - The PR carries a sample response of each route; the knowledge-graph repo gets the commit, the routes and
        the sample.
      - **Status (2026-10-09): done, PR #130** (approved 2026-10-09). Both routes (`/contract` and
        `/contract/database`), 10-column `quant_run` pin, hermetic tests. Work item 19 still has open tasks
        (`T-083`, `T-142`, `T-145`), so nothing moves to `CHANGELOG.md`.
      **Acceptance:** the tests above; a `GET /api/v1/contract` against a running instance returns every view with
      its columns in order and `contract_version` (10 once `T-144`'s marker migration has landed).
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
      delivers: `itemcheck.py`, `signcheck.py`, `precedence.py` with `--db`, and `audit_sec_checklist.py fetch` for the SEC cache). **FAIL** on any value mismatch, on any
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
      `T-083`, `T-142`, `T-144`, `T-152` and `T-145` (Work item 19), and the final pilot `T-143` are prerequisites; `T-100` keeps `T-153` as a prerequisite. A task added
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

**🔴 Current priority order (user's, 2026-10-05; Work item 8's part re-set 2026-10-07; `T-144`/`T-152` moved first
2026-10-09; scope: finish this repo first):** **`T-144` → `T-152`** (the knowledge-graph view contract and its endpoint, moved first 2026-10-09: the knowledge-graph repo waits on them) → Work item 8 (`T-141` → `T-077` → `T-070` → `T-147` → `T-149` → `T-148` → `T-150` → `T-151` → `T-076` → `T-073` → `T-074` → `T-071`) → `T-153` (historical universe) → Work item 19 (`T-083` → `T-142` →
`T-145`) → Work item 2 (orchestrator) → `T-143` (the final pilot) → `T-100` (the full-universe run, last of all).
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

**Work item 12 (`T-143` then `T-100`, the full-universe production run) runs last of all**, after every other task in this file that is not deferred or superseded — current and future — so after Work item 8 (… → `T-071`), `T-153`, Work item 19 and Work item 2. `T-100` keeps `T-153` as a prerequisite.
