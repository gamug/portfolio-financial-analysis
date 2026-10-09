# Checklist-to-repo review — plan v1

Claude, 2026-10-08. Input: `checklist_v1.2.2.md` (frozen, 78 rules) and `origin/master` at `5280e4b`.

## Objective

Every rule in the checklist ends in exactly one of four documented states:

| State | Meaning | What the jury sees |
|---|---|---|
| **Enforced** | Code implements the rule, and a test fails if the rule is broken. | Rule → source quote → `file:function` → test. |
| **Deviation, decided** | The code departs from the rule on purpose. | The decision and its reason, in `docs/model_fixes.md` or section L. |
| **Not executable** | The data the pipeline captures can't run the rule. | A declared limitation, with its measured prevalence. |
| **Violation → task** | A bug. | The task that fixes it, with a regression test and before/after numbers. |

The goal is that nothing is left "unknown" and nothing breaks a rule silently. This gives the thesis one traceable
chain per rule: **source → rule → code → test → measured prevalence → decision**. It also turns every violation into a
fix with a regression test.

## How this maps to the task list

This review **is `T-147`**, already in `TASKS.md` (merged with PR #126): docs, a script and tests, with **no change
under `src/`**. TASKS lets `T-147` start before `T-070` merges once the checklist is approved, and v1.2.2 is now
approved. `T-147`'s output is the prioritized fix plan. The fixes go first to existing tasks, then to new ones (`T-149`+)
only when no existing task covers them:

- `T-148` (D1–D4, D6, D10, `abs()`)
- `T-076` (growth, D9)
- `T-071` (financial firms' scoring)
- `T-072`, `T-073`, `T-074`

`T-147`'s text still says "approves `checklist_v1.md`". Its PR corrects the reference to v1.2.2. That is a text fix
inside the task's own PR, not a new task.

## Phases

### Phase 0 — Make the checklist reproducible from the repo (in `T-147`'s PR)

- **Track** `checklist_v1.2.2.md`, `sources_register.md`, `quotecheck_v11.py`, the two absence logs, and the
  cross-check scripts `T-147` already names (`itemcheck.py`, `signcheck.py`, `precedence.py` → `scripts/sec_xcheck/`).
- **Don't commit the source PDFs** (FASB, Damodaran, S&P, …: copyright). The register's URLs and SHA-256 hashes are
  how a reader gets the same files.
- **Acceptance:** from a clean checkout plus the downloaded sources, `quotecheck` reports 150/150 verified.

### Phase 1 — Static review, rule by rule (read-only, no database)

Read each rule against the code that should enforce it, across the whole data path, the other repo included:

| Rule group | Where it lives |
|---|---|
| ID-04/05/06, ID-09/10, ID-12, ID-16, ID-17, ID-21 (units, instants/durations, dimensions, co-registrants, `preferred_sign`, duplicates) | Gateway: `portfolio-data-mining/src/sec_edgar/` |
| ID-01, ID-02a/02b, ID-03, ID-11, ID-13–15, ID-20, CON-11, MET-07, MET-08, APP-04b | `fundamental_agent/statements.py` (concept lists, precedence, component maps, capex tiers) |
| ID-07, ID-08, PER-05/06, PER-08, PER-09 | `fiscal.py`, `statements.py`, `pipeline.py` (TTM, Q4, `YEAR_TOLERANCE_DAYS`) |
| PER-02/03, PER-10/11, APP-10a, APP-10b-1/2/3 | `edgar_client.py`, `pipeline.py`, `kg_schema` universe reads |
| PER-01, MKT-01 | `kg_schema/availability.py`, `fundamental_agent/db.py` (`filing_available_at`), `cycle/data.py` |
| ID-18, CON-01–12, CON-19, DQ-01, Annex A | `fundamental_agent/quality.py`, `db.py:930` (share corrector) |
| MET-00 to MET-09, PER-04, PER-12–14 | `fundamental_agent/metrics/*.py` |
| MKT-01–04 | `metrics/valuation.py`, `kg_schema/market_cap.py`, `quant/caps.py` |
| DQ-02, MET-00 (neutral 50), MET-05/06/09 in scoring, APP-00–09, the veto | `cycle/scores/normalize.py`, `valorization.py`, `rules/builtin.py` |
| What leaves the repo (does every value carry its flag and `available_at`?) | `kg_schema/views.py`, `api/` |

For each rule, record:
- the implementing `file:function`, or `none`;
- the status: ✓ / partial / ✗ / n/a;
- the test that enforces it, or "no test";
- for partial or ✗: the exact gap, and a **proposed classification** (bug, decided deviation, not executable).

Start from the "Input for the code comparison" list, then cover the remaining rules.

**Output:** `code_comparison_v1.md`, a working draft that becomes the brief for `T-147`'s measurement.

### Phase 2 — Measurement (`T-147`'s scripts, read-only)

Two evidence levels, kept separate, as `T-147` defines them:

- **(a) Prevalence at the source.** SEC `companyfacts` for the 503 CIKs, filtered to our accessions.
  - Limited to non-dimensional, standard-taxonomy facts.
  - Rules that depend on dimensional or custom facts are marked "source prevalence incomplete".
- **(b) Our resolver.** `statements.py` re-run over production's stored facts and the pilot database.
  - Production is opened `mode=ro`, with `shasum` before and after.

The measurements the checklist already asks for:
- **Capex:**
  - the lines rule (b) leaves out, and the amount;
  - for producers, `PaymentsToAcquireOilAndGasProperty` against exploration and development in years without
    mergers;
  - each large deal: which element carries it, and the FCF effect;
  - where the (c) tie-break fires;
  - the REIT `PaymentsForCapitalImprovements` / acquisition overlap.
- **Shares:**
  - whether the history check or A-03 catches the MCD 10⁶ case;
  - how many values the `db.py:930` corrector rescales;
  - how often the diluted fallback is used.
- **Scoring:**
  - companies given the neutral 50 in `valorization.py`;
  - what changes between winsorizing at 2% and at 0.5%;
  - periods `YEAR_TOLERANCE_DAYS = 20` accepts that PER-08's ranges reject, and the reverse.
- **Points in time:**
  - stored ratios whose `available_at` is NULL or equal to the period end;
  - filings fetched by ticker instead of by CIK, and the successions found.
- **Defects:** D1–D10 counts on both evidence levels.
- **Veto:** companies excluded per cycle, by sector (Annex A).

Every count is reproducible by a script in `scripts/sec_xcheck/`. A sample is re-run independently (`T-147`'s
acceptance).

### Phase 3 — Classification and priorities (decisions)

Each partial or ✗ rule goes to one bucket:

1. **Bug.** The code contradicts a sourced rule, and nothing in the project decided it. → A fix in an existing task,
   or `T-149`+.
2. **Deviation to decide.** Keeping the code is defensible (cost, data, scope). → The user decides; the decision is
   recorded in `model_fixes.md`. If it changes a rule, the checklist gets a reviewer round, because v1.2.2 is frozen.
3. **Not executable.** → A limitation in section L, with its measured prevalence.
4. **The checklist is wrong.** The code reveals a case the rule mishandles. → A reviewer round, never silently.

**Priority:** severity (BLOCK > WARN > INFO) × companies affected × whether it reaches a score or the veto. A BLOCK
rule broken in a value that feeds the ranking comes first.

**Output:** the `T-147` PR:
- `docs/sec_data_checklist.md`
- the prevalence table
- the map from defects to rules, including fixed defects no rule covers
- the golden-set semantic choices
- the fix plan

I review it on the whole data path. You merge.

### Phase 4 — Fixes (`T-148`, then the work-item 8 order)

- One task per PR.
- Each fix cites the rule IDs and sources in `model_fixes.md` (constitution #12), adds a regression test on a real
  captured fixture, and is verified on a rebuilt scratch database, never on production.
- `T-148`'s acceptance re-runs the Phase 2 scripts and reports 0 FLIPPED and no OTHER_CONCEPT.
- Phase 1's ✓ rows that have no test get a test as part of the task that touches their module, not as separate tasks.

### Phase 5 — Validation and thesis material

- **Golden set:** about 20 tickers plus an insurer, GOOGL and BRK.B, checked by hand against the 10-K for 5–8 metrics
  and the semantic choices.
- **The thesis-annex version**, generated by script: per rule, the rule, source, quote, measured prevalence, final
  state and decision. Plus the quote-check output and the source register.
- **Sensitivity analysis** for Annex A's calibrated screens: values removed, the book's change, and the veto by
  sector.
- **The final pilot (`T-143`)** runs on fresh databases after the fixes.

## Constraints

- No change under `src/` before `T-147` is approved.
- Production is read-only, with `shasum` before and after. Experiments use scratch copies.
- The gateway repo is only read here. Fixes there are that repo's PRs.
- The SEC User-Agent is `SEC_USER_AGENT`, else `research@example.com`; at most 10 requests per second; responses
  cached.
- No architecture artifacts. No merges by Claude.
- The checklist stays frozen. Any rule change goes through a reviewer round.

## Proposed sequence

1. **Now:** I run Phase 1 myself, read-only, and give you `code_comparison_v1.md`. I'm cheap here, and it gives the
   `T-147` agent a precise brief, so it doesn't rediscover the code.
2. You (and the reviewer, if you want) check it. I then write the `T-147` agent text: Phases 0, 2 and 3, using Phase 1
   as input.
3. The `T-147` PR → my review → your merge.
4. `T-148` and the rest of the work-item 8 order.
5. Phase 5.
