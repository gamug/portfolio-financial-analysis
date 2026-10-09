# Phase 3 — decisions from the review of `code_comparison_v1.md`

Claude, 2026-10-08. This file answers the reviewer's comments on the first code comparison, including their answers
to Q1–Q6. It records each decision, and collects every checklist change in one list for **v1.3**. v1.3 is written once,
when Phase 3 closes, as the reviewer suggested. Each claim below was checked against the source files or the code; the
"Checked" column says how.

## 1. The reviewer's claims, verified

| # | Claim | Checked | Verdict |
|---|---|---|---|
| R1 | S-X 5-02 says the line items should appear "if applicable" | D2_RegSX_5-02 p.1: "the various line items and certain additional disclosures which, if applicable, and except as otherwise permitted by the Commission, should appear on the face of the balance sheets" | **Correct.** The sentence opens § 210.5-02; the PDF page also carries the end of 5-01. The same rule lists the debt captions separately: item 20 includes "the current portion of long-term debt", and item 22 is "Bonds, mortgages and other long-term debt, including capitalized leases" (p.3). |
| R2 | CON-11's quote gives a source for the 606 + non-606 sum | B4_DQC_Revenue_Guidance, already in CON-11: "The Revenues element would be used for the total revenue (ASC 606 and non-ASC 606 revenue)." | **Correct**, under a condition (D-02). |
| R3 | B6 probably defines income tax as current + deferred | B6 `IncomeTaxExpenseBenefit`: "Amount of current income tax expense (benefit) and deferred income tax expense (benefit) pertaining to continuing operations." Both components are defined "pertaining to … continuing operations". | **Correct.** T-148(c) has its source. |
| R4 | B6's `DebtCurrent` may describe its composition | B6 `DebtCurrent`: "Amount of debt and lease obligation, classified as current." `LongTermDebtCurrent` "… Excludes lease obligation." `ShortTermBorrowings`: "debt having initial terms less than one year". `CommercialPaper`: "short-term borrowings using unsecured obligations …". | **Partly.** It gives no list of components, and it **includes leases**. T-148(d) puts `DebtCurrent` first, which contradicts MET-07 (narrowest first; excludes leases). See D-06. |
| R5 | `LongTermDebt` vs current maturities (my N4) | B6 `LongTermDebt`: "… of long-term debt. Excludes lease obligation." (no classification), against `LongTermDebtNoncurrent`: "… classified as noncurrent." | **N4 confirmed by the definitions:** `LongTermDebt` includes current maturities, so adding `LongTermDebtCurrent` double counts. |
| R6 | "The comparison speaks of 2022–2026; if the first 10-K is from 2022, its three years are 2020–2022, after ASC 842 and CECL" | `pipeline.py`: `DEFAULT_SINCE_YEAR = 2022` is the **filing** year. The gateway matches `filing_date.year == year`. | **Not quite.** A 10-K filed in early 2022 reports FY2021, with income statements for FY2019–FY2021, and the CAGR base is FY2019. A calendar bank's FY2019→FY2021 net-income CAGR crosses CECL (2020). A September-year-end filer's FY2019 can precede ASC 842. **PER-13 and PER-14 still apply to the first filing year.** Phase 2 measures how many cases there are; they are not dropped. The window is still to be aligned (D-10). |
| R7 | "Vetoing on the sign of equity" | `quality.py`: `DQ_NEG_EQUITY` is HARD only when negative equity comes **with** `net_debt_to_ebitda > 5`, EBITDA ≤ 0 with positive net debt, or interest coverage < 1.5; otherwise SOFT. | **Inaccurate.** Today the sign alone never vetoes. The real question is different (D-04). |
| R8 | C7 covers invested capital, C10 covers EBITDA | C7 p.10: "dividing the operating income by the total book value of debt and equity will yield too low a return on capital for companies with significant cash balances" (the basis for netting out cash). C10 p.50 writes value in terms of "EBITDA (1-t) - DA (1-t)", i.e. EBITDA − DA = EBIT. | **Likely, to be confirmed** when the metric dictionary is built (D-05). Neither is quoted yet. |
| R9 | "CFO + interest × (1 − t) − capex" is the CFA formula for FCFF | Not in any loaded source. | **Plausible but unsourced.** The renaming is right (D-05). The formula needs a loaded source. |
| R10 | Financials are about 13% of the index weight | Not in any loaded source. | Not verified, and not needed for the decision. |

## 2. Decisions

| ID | Topic | Decision | Basis | Goes to |
|---|---|---|---|---|
| **D-01** | Q1: a map with one term missing | **Debt:** accept the filed terms, flagged "partial map", only when (i) long-term debt is present (short-term debt alone → empty, which suggests an unrecognized concept) and (ii) the rest of the balance sheet resolved. **Cash never qualifies:** missing cash is a capture failure, so EV is empty. | Inference from S-X 5-02 "if applicable" (R1). | v1.3 (ID-02b/MET-07 note), T-148 |
| **D-02** | Q2: the 606 + non-606 sum | **A sourced map** under ID-02b, allowed only when the components are the only revenue lines in the statement's revenue section. Otherwise it is a partial total, and revenue stays empty. | R2 | v1.3 (ID-02b/CON-11), T-148 |
| **D-03** | Q2: the T-117 rebuild and the T-140 correction | **New limitation L-09:** a project derivation, with prevalence. Its existing guards (every row between ≤ 25% of the anchor, or the gateway's own figure agreeing to the dollar) are declared **calibrated**, like Annex A. Phase 2 lists every derived value, and each one is checked by hand. A value that fails its guard is already left empty. **I don't adopt** the reviewer's extra condition of reconciling with gross profit: the affected filers (APA, oil & gas) file no gross profit, so the condition would empty exactly the values it is meant to check. The by-hand check of every case serves the same purpose, verifying beyond the reviewed cases. | The reviewer's principle (verified cases ≠ future filings) is kept. | v1.3 (L-09), T-147 measures |
| **D-04** | Q4: negative equity and the veto | **Recommended (your call):** `DQ_NEG_EQUITY` becomes **quarantine only** (D/E and ROE read NA, as APP-07 says), never HARD, so it is not a data-quality veto. The distress test for negative-equity firms moves to `LEVERAGE_EXTREME`. That is a cycle credit rule, already calibrated (T-116, S&P's corporate methodology), declared once in the thesis with its sensitivity, and outside the SEC checklist like the other portfolio rules. **It must key on the equity sign, not on a negative D/E:** today the rules read the quarantined metrics (`orchestrator.py` builds `RuleContext` from `dq.apply(...)`), so `LEVERAGE_EXTREME`'s negative-equity branch never runs. The distress rule uses `net_debt_to_ebitda` per MET-09: net cash with positive EBITDA counts as low leverage. **Result:** McDonald's, AutoZone and the like are vetoed only when distressed, through a rule named for what it is. **Alternative:** drop the distress test entirely, as the reviewer leans. | APP-07; C4 p.15 stays the precedent if you prefer an exclusion. | `T-070` or `T-071` (cycle rules); no A-06 |
| **D-05** | Q5: formulas without a rule | **A metric dictionary annex** in v1.3: metric, formula, **one** source, and whether it is an inference. It replaces adding 35 MET rules. Rename the audit key `free_cash_flow_to_equity` to `free_cash_flow`, because it is OCF − capex, not FCFE. FCF to the firm (FCF + interest × (1 − t)) needs a loaded source, or switches to Damodaran's EBIT-based form. The quick and cash ratios also need a source. This is the concrete use of the optional "second author" (a standard financial-statement-analysis text): **you choose whether to load one.** | R8, R9 | v1.3 annex; new T-151 implements it |
| **D-06** | Q6: T-148's text | (a) Replace "keep the `abs()` guards" with "**reject a wrong sign and flag it**" (ID-11). (c) Income tax = current + deferred has its source (R3). It applies only when both are on the statement face; Phase 2 measures how often. (d) **Reorder, per MET-07 and R4:** `LongTermDebtCurrent` + `ShortTermBorrowings` (disjoint by definition) first; `CommercialPaper` only when `ShortTermBorrowings` is absent (its definition makes it a short-term borrowing); `DebtCurrent` last, flagged "includes leases". Completed by D-01. | B6, MET-07, ID-11 | The T-147 PR amends T-148's text |
| **D-07** | N8: financial firms | **Measure first** (Phase 2 measurement 4), then you choose: (a) exclude them from the universe explicitly (C4 p.3), or (b) keep them with sector-specific factors (T-071) and corrected vetoes. The current, accidental state is not kept either way. | The reviewer's framing | Decision after Phase 2 |
| **D-08** | PER-05 | Align the code to the rule: Q4 = FY − the Q3 10-Q's 9-month YTD. | The reviewer: more robust, needs no three quarters. | T-148 or T-149 |
| **D-09** | PER-06, the ×4 fallback | **Values built on ×4 leave the score** (empty), the same treatment T-133 already gives the cash-flow metrics. Propagating a flag nobody acts on would change nothing. PER-06's severity becomes BLOCK for ×4. | The reviewer's either/or; T-133 precedent | v1.3 (PER-06), T-148 |
| **D-10** | The data window | v1.3 states three windows separately: **filings** filed from 2022; **fiscal years** covered from FY2019 (the CAGR base of the first 10-K); **taxonomy releases** 2020–2026 (ID-20). Phase 2 counts filings whose CAGR or growth crosses ASC 842 or CECL (R6). | R6 | v1.3 (Scope) |
| **D-11** | A "before" baseline for the thesis | **Adopted.** Production is frozen (read-only, sha1 `5c5c0642…`), so it already *is* the "before" state, as long as it stays unchanged. Two further steps make the comparison clean: (1) T-147 exports, read-only, the current rankings, portfolios and key metrics to a dated file with the database checksum; (2) the "after" comparison reruns **the same cycle code** over `metrics-v5` and the new engine version on a scratch copy. The metric-version pin (T-090, `MetricVersions`) already allows this. Without (2), "after" would mix the data fixes with T-141, T-070 and the new prompts, and the thesis could not attribute the change to the data treatment. | The reviewer's suggestion | T-147 Phase 0 and the final pilot |
| **D-12** | N9 | A bug, no decision needed (MET-09 already settles it). | The reviewer | T-151 |

## 3. Task order after T-147

The reviewer proposed: filing identity → line items → metric definitions → identities → scoring. **Adopted with one
change:** the identity gates (T-150) come right after T-148, before the metric definitions. They check line items, not
metrics: CON-04 and CON-19 verify T-148's equity and net-income choices. Running them then measures T-148's result
once, which is the reviewer's own "no measuring twice" argument.

T-147 → **T-149** (filing identity: amendments N1, CIK, period end, the PER-08 ranges) → **T-148** (line items and signs,
amended per D-06) → **T-150** (CON identities with DQ-01) → **T-151** (metric dictionary and definitions: MET-01/02/03/09,
N2/MET-05, N10, N15; absorbs T-072's EBITDA and T-076's growth item) → T-073 → T-074 → **T-071** (scoring:
MET-00's neutral 50, APP overlays, D-04, D-07, the DQ-02 constant).

**N2 is the first priority among the fixes.** Every real cycle ranked VALORIZATION without `earnings_yield`. If the
task order above makes it wait too long, it can move into T-149 as an isolated fix (the orchestrator's key, MET-05's
dummy, TTM net income). **Recommended:** put it in T-149, since it is small and changes results already produced.

The new tasks (T-149, T-150, T-151) and the new order go into `TASKS.md` through the T-147 PR or a docs PR, one task
per PR.

## 4. Checklist v1.3 change list (written when Phase 3 closes)

1. Scope: the three windows (D-10).
2. ID-02b / MET-07: the partial debt map under S-X 5-02 (D-01); the 606 + non-606 sourced map and its condition (D-02).
3. L-09: the revenue derivations (D-03).
4. PER-06: ×4 → BLOCK (D-09).
5. Metric dictionary annex (D-05).
6. Annex A: unchanged if D-04 is accepted (no A-06). If you keep a HARD negative-equity veto instead, A-06 with C4 p.15 and its sensitivity analysis.
7. Rule text where Phase 2 changes a prevalence statement (for example ID-20's dead concepts).

## 5. Added to the Phase 2 measurement list

- **N1 first** (the reviewer): years whose 10-K is missing because of a Part III-only amendment.
- Filings whose growth or CAGR crosses ASC 842 or CECL (D-10).
- Every revenue value derived by T-117 or T-140, listed for the by-hand check (D-03).
- How often current and deferred tax are both on the statement face without the total (D-06c).
- The "before" export (D-11).

## 6. Waiting on you

- **D-04:** keep the distress veto as a `LEVERAGE_EXTREME` cycle rule (recommended), or drop it.
- **D-05:** whether to load a second author (a financial-statement-analysis text) for the quick ratio, the cash ratio and the CFO-based FCF to the firm.
- **D-07:** after Phase 2, exclude financial firms or score them with their own factors.

## 7. User decisions and the reviewer's conditions (2026-10-08, round 2)

**D-04 is accepted** (the agent's option), with the reviewer's four conditions:
1. **Valid inputs only.** The distress rule reads the equity sign, interest coverage and net debt/EBITDA directly, never the quarantined D/E.
2. **EBITDA ≤ 0 is a distress signal.** A net debt/EBITDA that MET-09 leaves empty never reads as "no signal". T-116's calibrated condition (EBITDA ≤ 0 with positive net debt) is kept, and the rule handles it explicitly.
3. **Not applied to financial firms** (APP-02). It needs the company-type table, which T-147 builds (APP-00).
4. **The thresholds are calibrated.** They are a cycle policy, declared with a sensitivity analysis and the veto count by sector.

The implementation goes to **T-071**, the cycle rules task, beside D-07. It needs T-151's EBITDA.

**D-05 is accepted:** the second author is loaded later.
- The metric dictionary marks three formulas **"source pending"**: the quick ratio, the cash ratio, and the CFO-based FCF to the firm.
- Deadline: before the thesis-annex version is generated (after T-147).

**D-07 is accepted:** decide after Phase 2. The measurement brings:
- financial firms vetoed per cycle;
- their share of the index (market-cap share from the as-of reader; declared approximate, since it is total and not float-adjusted);
- the portfolio's sector exposure with and without them, from a replay on a scratch copy.

**Decision rule:** if the vetoes already exclude almost all of them and T-071's sector factors are costly, exclude them explicitly (C4 p.3). If the thesis benchmarks against the full S&P 500, keep them.

**L-09 (D-03), the reviewer's condition is accepted:** verification is recorded **per filing**, with the accession, the date and who verified it.
- A rebuilt value without a verification record is left empty, or flagged "unverified", and never used silently.
- An **ex-post audit** is added: compare each rebuilt value with the comparative column of the next year's 10-K. This is validation only, never an input, so there is no look-ahead. The comparative column may be restated, and the audit says so.

**N2 is in T-149** (agreed). The before/after analysis (D-11) reports its effect **separately**, as a scoring bug, not a data-treatment change. The decomposition:
- (0) production as it is;
- (1) the N2 fix alone, on `metrics-v5`;
- (2) plus the new metric engine.

**The task order is confirmed:** T-147 → T-149 → T-148 → T-150 → T-151 → T-073 → T-074 → T-071.

## 8. Correction from T-147's measurements (2026-10-08)

**D-03b (amends D-03's premise).** D-03 said a value that fails its guard "is already left empty". **That is wrong.**
- `Statements.get` falls back to the component sum when the rebuild is refused: 19 of 52 refused columns, ADP and ICE.
- The fallback stands only under **D-02's condition** (the components are the only revenue lines); otherwise revenue is empty.

**The ex-post audit is now part of L-09:**
- APA, PSX, NI and MPC: exact on 51 checks.
- ICE: fails all 5 (−54% to −93%), because the rebuild anchors on a net-revenue line ("Total revenues, less transaction-based expenses").
- **A derived value whose audit fails is left empty**, and the derivation must refuse a "less …" (net) anchor.

**Two new defects for T-148:**
- N19: `_LABEL_TOTAL_EXCLUDE_RE` matches "cost" but not "costs".
- N20: ICE's derived revenue.
