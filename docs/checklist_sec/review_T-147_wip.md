# Review of T-147's work in progress (uncommitted)

Claude, 2026-10-08. Branch `docs/t147-sec-checklist-audit`, working tree only: no commit, no PR.

## 1. Is it complete? No.

| Part | State |
|---|---|
| Phase 0: scripts promoted, quotecheck, baseline | **Mostly done.** Quotecheck re-run by me: 150/150. Baseline at `/Users/dova/thesis/data/baseline_2026-10/` with its manifest. **Missing:** the `git add -f` of the inputs, and the T-148/T-143 path updates in `TASKS.md`. |
| Phase 2: measurements | **Mostly done:** 320 measurements, the L-09 register (162 rows), the company-type table (500 CIKs), the golden set (23 rows), and the veto replay over 19 cycle dates. **Missing:** the D-07 sector-exposure replay (the script exists, no result recorded), and FANG/APA's year-by-year capex table. Some measurements need fixing (§3). |
| Phase 3: `sec_data_checklist.md`, prevalence table, defect map, v1.3 inputs, `TASKS.md` | **Not started.** |
| Commits, PR description | **Not started.** |

## 2. What I verified

- **Nothing changed under `src/`.** Production sha1 was `5c5c0642…` before and after my own read-only runs.
- **CI checks:**
  - `pytest -q`: 1,603 passed. No network calls in the new tests.
  - `ruff check`: the 46 errors are all in your own untracked folder `docs/md primera revision/`; the agent's files are clean.
  - mypy (`src tests`, as CI runs it): clean. The scripts aren't type-checked by CI; 67 mypy errors there. Not blocking, same as the older `verify_t*.py`.
- **Reproducible:** re-running `measure --only n1,extra` on production reproduces all 45 counts exactly.
- **Independent checks with my own SQL:**
  - **N1:** 8 stored amendments, with the same accessions.
  - **N2:** no `net_income` metric; production holds only `metrics-v2`.
  - **N3:** reconciles at 60 10-K plus 6 Q1 columns = 66, but see §3.
- **Fidelity method:** "drift" is assigned by structural detectors tied to specific documented fixes, not by "any mismatch", so it isn't circular. Raw fidelity of 99.34% on the 377 production filings that have metrics is acceptable.
- **SEC access:** `SEC_USER_AGENT` else `research@example.com`; throttled; cache under `.cache/sec_xcheck` (gitignored).
- **Company types:**
  - Article 9: 24 filers, each with a quoted 10-K statement and its accession.
  - Article 7: 17 insurers.
  - Other financial: 10.
  - Multi-sector holdings: BRK.B.
  - Payments, exchanges and insurance brokers: operating.

## 3. Findings to act on

### New results that change the plan

- **F1: T-140 derives wrong revenue for ICE.**
  - The ex-post audit (next year's 10-K comparative) passes exactly for APA, PSX, NI and MPC (51 checks, 0% difference).
  - It fails for **all 5** ICE checks: −54% to −93%, e.g. 2022Q3 derived $1,235M against $2,387M.
  - The anchor is "Total revenues, less transaction-based expenses", a *net* revenue line, and the rebuild subtracts more rows from it.
  - **17 ICE values are wrong in the data.**
  - Also, T-140 derives revenue for **PSX, NI, MPC and ICE**, not only APA as `model_fixes.md` describes.
  - → BUG, high, T-148. Values that fail the audit are left empty (D-03 amended below).
- **F2: N3's reach is twice what was measured.** Besides the 66 target columns, **67 10-Qs** read the continuing-operations line in the year-to-date columns the TTM uses. Every resolver measure on 10-Qs must cover the columns the pipeline actually reads: the target, the YTD pair and the prior.
- **F3: 14 original 10-Ks are missing with no amendment** (AVY FY2022, CDNS FY2022, CSCO FY2026, …). The cause isn't identified. Find it (an "unusable" period? a failure?) before classifying it.
- **F4: Utilities take 22–24 HARD vetoes per cycle**, the largest sector cluster in the replay (Financials 15).
  - Not a checklist rule, but exactly the sector bias Annex A asks to report.
  - → Prevalence table and sensitivity analysis; flag for T-070/T-071 (`NEGATIVE_FCF` on capex-heavy utilities).
- **F5: D-07 numbers so far:**
  - financial firms are about 7.5% of the approximate market cap;
  - 14 of 47 evaluated are vetoed per cycle;
  - 10 of them are **Article 9 filers vetoed by DATA_QUALITY because their revenue doesn't resolve** (BNY: 33 refused rebuilds).

  That last point is a resolver defect for banks (APP-01), not a judgment on the banks.

### Measurement fixes

- **M1:** `N2.net_income_metric_exists` reads 1/1 while meaning "no such metric exists". Re-encode it as the count of metrics named `net_income` (0), so a thesis table can't be misread.
- **M2: label the engine version on every stored-level record.** Production is `metrics-v2`, which predates the T-105 flags and T-132's cover count, so:
  - `PER06.*` = 0/8592 means "not measurable on v2", not zero;
  - `MKT04.*` (258/258, 238 diluted) describes the old engine, not today's code.
- **M3:** `src.N12.dead_concept_facts_all_history` (25,709 / 25,709, 0 companies, no note): explain it or drop it.
- **M4:** run the **D-07 replay** (sector exposure with and without financial firms); record it.
- **M5:** write the **FANG/APA year-by-year table** (`PaymentsToAcquireOilAndGasProperty` against exploration and development) that the note promises.
- **M6: golden set:**
  - WAT's net income from `…AvailableToCommonStockholdersBasic` is marked "agree: yes"; under N13 it is "decide".
  - Show the REIT overlay (CPT, ESS, UDR, SBAC) in the `.md` table, not only in the CSV's `capex_expected`.
- **M7:** two defaults still hard-code `/Users/dova/…` (`--universe-db`, the baseline docstring). Use `KG_UNIVERSE_DB`, or require the argument.

## 4. The agent's question (the D-03 premise)

**Not a stop condition. Carry on.** The premise was mine and is factually wrong:
- `Statements.get` falls back to the component sum when the rebuild is refused.
- Whether that sum may stand is already decided by **D-02**: only when the components are the only revenue lines.
- For ADP the sum equals the true revenue.

The `cost`/`costs` label bug (16 refused columns anchored on "TOTAL COSTS OF REVENUES") is a BUG for T-148. No derived value rests on a cost anchor today.

## 5. DQC lists (ID-13 to ID-15)

Right not to fetch an unregistered file. **Decision:**
- Register the official DQC element-list files (XBRL US's published rule resources, which B1 and B2 link to) in `sources_register.md` Block B, with URL, version and SHA-256.
- Then measure ID-13 to ID-15 on the real lists, and keep the proxy only as a cross-check.
- This adds a source; it changes no rule.

---

## Message for the agent (paste below the line)

---

Good work. The review is in `docs/checklist_sec/review_T-147_wip.md`; read §3–§5. Answers and instructions:

1. **The D-03 premise:** not a stop condition. Carry on. The premise was the reviewer's and was wrong. D-03 is amended in
   `phase3_decisions.md` (D-03b): a refused rebuild falls back to the component sum, which stands only under D-02's
   condition; a derived value whose ex-post audit fails is left empty. Classify the `cost`/`costs` regex as a T-148 bug.
2. **ICE (F1):** report it as a high-severity bug. 17 wrong revenue values, all 5 ex-post checks −54% to −93%, anchored
   on a net-revenue line. Note that T-140 also derives revenue for PSX, NI and MPC, which `model_fixes.md` doesn't say.
3. **Fix the measurements M1–M7** and **extend F2:** every resolver measure on 10-Qs covers every column the pipeline
   reads (target, YTD pair, prior). Find F3's cause.
4. **Run the D-07 replay** and record it. Report F4 (utilities' HARD vetoes by rule and cycle) in the prevalence table.
5. **DQC lists:** register the official element-list files in `sources_register.md` Block B (URL, version, SHA-256),
   then measure ID-13 to ID-15 on them. Keep the proxy as a cross-check.
6. **Then Phase 3, as briefed:**
   - `docs/sec_data_checklist.md` (all 78 rules);
   - the prevalence table, including F1–F5;
   - the defect map, adding N19 (the `costs` regex), N20 (ICE) and N21 (the unexplained missing 10-Ks, once its cause is known);
   - the v1.3 inputs;
   - the `TASKS.md` changes (T-149, T-150, T-151; T-148 and T-071 amended; Work item 8's order; the T-148/T-143 script paths). Put F1 and the regex in T-148, and F5's bank revenue in T-148 under APP-01.
7. **Fidelity:** write the method and numbers into the PR: no database holds `metrics-v5`; production is `v2` (377 filings with metrics), the pilot `v4`; 99.34% raw, 100% after the structural drift classes. **Label the engine version on every stored-level measure.**
8. **Commit and PR:**
   - `git add -f` only the inputs listed in the brief, plus `company_types.csv`, `l09_verification.csv`, `golden_set.csv`, `prevalence_results.json`, `prevalence_tables.json`, `baseline_manifest.md` and `review_T-147_wip.md`.
   - Never add `docs/md primera revision/`, `AUDIT_DECISIONS.md`, `fixes_feedback.md` or `docs/pilot_rerun_report.md` (the user's own files), nor the PDFs or the cache.
   - Split the commits logically; open the PR; don't merge.
   - The PR description carries production's sha1 before and after, the SEC contact used, and the commands for an independent re-run of N1, N2, N3 and D-07.
