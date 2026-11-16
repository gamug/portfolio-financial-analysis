# pilot_a3 — the empty metric values: what they are, which rule or cause empties each, what could fill them

Measured 2026-10-29 on the real database `pilot_a3/financial.db`, sha256 `1de09ad4a28daa0973e93c0822caba0def2e24a39ab956d8eab72c5af74932c7`
(state after FIXES #10). Every count comes from the database itself and from
`audit/checks/empty_split_v3.py`, which tests each empty value's missing inputs against the SEC companyfacts of the
same accession. The per-value list (one row per empty value: ticker, accession, form, period end, metric, recorded
empty reason, bucket, missing items) is `audit/r10/empty_split_after10.csv`.

## 1. The numbers

| | values |
|---|---|
| metric values in the database | **357,231** |
| filled | 283,219 |
| **empty** | **74,012 (20.7 %)** |
| empty before round #10 (FIXES #10) | 84,852 (23.8 %) |
| companies with at least one empty value | 580 |

### 1a. Empty reason the engine recorded (`provenance.empty_reason`)

| recorded reason | values | rule |
|---|---|---|
| `not_reported` | 28,949 | MET-00: an input the formula needs was not found |
| `not_applicable` | 24,955 | the applicability matrix (APP-00…APP-09): the metric does not apply to the company type |
| `missing_input` | 5,824 | MET-00 (an input missing on another basis: TTM, prior year) |
| `prior_year_missing` | 4,704 | MET-02: the prior-year capital / comparative is not available |
| `growth_base_not_positive` | 3,545 | MET-04: growth over a base ≤ 0 |
| `denominator_not_positive` | 2,579 | MET-09: denominator ≤ 0 |
| `capture_failure` | 2,100 | ID-02b: the input exists but was refused or could not be formed |
| (none recorded) | 1,154 | 788 `utility_cash_coverage_3y` (D-13 missing bound), 366 `value_inputs` |
| `denominator_not_meaningful` | 202 | D-14 (round #10, Q9): ROIC on invested capital < 5 % of total assets |

### 1b. The real cause, per empty value (the SEC data tested input by input)

| bucket | values | what it means |
|---|---|---|
| `not_filed` | 30,321 | the SEC has facts for the filing, but none under the input's concepts (and no face line was read) |
| `rule_not_applicable` | 24,955 | the applicability matrix |
| `rule_math` | 6,124 | MET-04 / MET-09 (negative base or denominator) |
| `prior_before_db_window` | 4,265 | the prior period's filing is before the database window (before 2022) |
| `has_refused` | 1,904 | the SEC has the input; a rule refused it |
| `no_item_recorded` | 1,718 | empty, no missing input recorded on the row (mostly yields and EBITDA whose TTM or market cap failed) |
| `ttm_term_missing` | 1,162 | a 10-Q's TTM: the filing has the value, a prior term (prior 10-K / prior YTD) is missing |
| `prior_input_other` | 800 | a prior-period input missing for another reason |
| `no_reason:missing_bound` | 788 | `utility_cash_coverage_3y` (D-13: a bound missing) |
| `ytd_only` | 529 | a 10-Q flow filed only year-to-date and not derivable (no previous 10-Q, or an item missing in one of the two) |
| `prior_gap_in_window` | 439 | the prior filing is missing inside the window |
| `no_reason:value_inputs` | 366 | the VALORIZATION value-input pseudo-row |
| `no_facts` | 288 | no SEC facts for the accession in our copy (2026 filings newer than the download) |
| `no_reason:return_on_invested_capital*` | 202 | Q9 not meaningful (see 1a) |
| `has_missed` | 88 | the SEC has the input, no refusal recorded — candidates for errors |
| `prior_input_before_window` | 63 | a prior input before the window |

## 2. Empty because a standard rule says so — correct, should stay empty (31,079)

* **24,955 not applicable** (APP matrix): a bank has no current ratio (1,603), no debt-to-equity in the industrial
  sense (1,672), no enterprise FCF yield (1,567), no cash ratio (1,422), no receivables turnover (1,024) …
* **6,124 rule math**: free-cash-flow growth over a negative base (1,596), net income growth over a loss (864), tax rate
  on a pre-tax loss (733), ROIC at the effective rate (673), FCF conversion (650), operating income growth (578) …

These are what every careful database does. Not a problem.

## 3. Empty because of a strict rule of ours — the problem (decisions needed)

### 3.1 Operating income not printed on the face — the biggest single cause

* **What:** the registry reads operating income only from `us-gaap_OperatingIncomeLoss`
  (`src/fundamental_agent/statements.py`, `REGISTRY["operating_income"]`). Companies with a **single-step** income
  statement (revenues, a list of costs, then "income before income taxes") print no operating-income line, so the
  item is empty and MET-00 empties every metric that uses it: operating margin, operating income growth, EBITDA,
  interest coverage, NOPAT, ROIC (both rates), return on assets (NOPAT-based), net debt / EBITDA, enterprise FCF yield.
* **Scope:** **16,350 empty values** list operating income as not filed — **2,221 filings, 138 companies**; for
  **10,682** of them it is the only missing input. By sector: Financials 4,084, Real Estate 2,031, Industrials 1,960,
  Health Care 1,728, Consumer Discretionary 1,692, Energy 1,530, Materials 1,355, Consumer Staples 740, IT 498,
  Communication Services 380, Utilities 352.
* **Companies (non-financial, values affected):** NKE 200; ADM, ADP, AVY, CASY, COP, CTVA, CVX, DD, DHI, DVN, EMR, ETN,
  FOX, GPC, IBM, JNJ, KLAC, LEN, LLY, MRK, MTD, NUE, NVR, NWS, OXY, PCAR, PHM, PSX, TXT, XOM, ZTS 190 each; TJX 188, PFE
  186, BMY 182, GE 182, BG, CLX, HCA, JCI, SRE 180; NEM 174, AES 172, DOW 172; REITs ARE, CPT, DOC, INVH, O, VICI 171 …
* **Not missing data:** the SEC has no `OperatingIncomeLoss` for these filings because the company does not present
  it. The cause is our rule (the item exists only as an explicit line).
* **The notes figure (L-26):** some of these companies tag `OperatingIncomeLoss` in the segment note. Round #10 did
  not read it: it does not reconcile with the face (HON 2026-Q1: 2,129 M against a pre-tax income of 886 M; RF up to
  64 % of revenue; AVB 39 %). **Decided by the user 2026-10-29 (C12): not read** — HON's 2,129 M is ASC 280
  "Segment profit", which excludes 904 M of GAAP operating expenses (Note 18 reconciliation).
* **Market practice:** data vendors compute **EBIT = pre-tax income + interest expense** when no operating line exists
  (non-financial companies). Not used for banks and insurers, where interest is the business and EBITDA/operating
  margin are not meaningful.
* **Measured on our own data** (6,520 non-financial filings that *do* print operating income, pre-tax income and
  interest expense): |operating income − (pre-tax + interest)| / revenue — median **0.66 %**, p75 1.88 %, p90 4.28 %,
  p95 6.89 %; within 1 % of revenue 60 %, within 2 % 76 %, within 5 % 92 %. By sector, median / p90: Industrials
  0.38 % / 2.1 %, Consumer Staples 0.31 % / 2.2 %, Consumer Discretionary 0.40 % / 3.5 %, Health Care 0.59 % / 4.2 %,
  IT 0.82 % / 4.2 %, Materials 0.58 % / 4.4 %, Communication 0.71 % / 3.6 %, Energy 0.99 % / 6.0 %, Utilities
  2.46 % / 5.6 %, Real Estate 2.08 % / 10.0 % (gains on property sales sit below operating income).
* **Coverage if adopted:** of the 2,221 filings, 1,087 non-financial filings have both pre-tax income and interest
  expense; 112 have pre-tax income but no interest expense; 63 interest but no pre-tax; 959 are financial.
* **Decision needed:** adopt EBIT = pre-tax + interest expense for non-financial filings without an operating line
  (flagged "derived"), and mark the operating metrics not applicable for banks/insurers? Real Estate and Utilities
  included or not?

### 3.2 Quick ratio requires an inventory line

* **What:** `src/fundamental_agent/metrics/liquidity.py`: `quick_assets` is formed only `if current_assets is not None
  and inventory is not None`. A company with no inventory (software, services, banks) gets no quick ratio.
* **Scope:** **2,110 empty quick ratios** whose only missing input is inventory (IT 451, Financials 453, Industrials 338,
  Communication 279, Consumer Discretionary 166, Health Care 144, Real Estate 123, Utilities 73, Energy 58, Materials 25).
  2,777 quick ratios are `not_filed` in all (667 more where current assets/liabilities are also not filed: unclassified
  balance sheets).
* **Market practice:** quick ratio = (current assets − inventory) / current liabilities with inventory 0 when the
  company has none.
* **Decision needed:** inventory = 0 for the quick ratio only when the filing proves there is none (no inventory line,
  no inventory fact in the filing — the same proof as D-11 for zero debt)? Inventory turnover stays empty.

### 3.3 The first year in the window has no prior year (data window)

* **What:** the database starts in 2022. A 10-Q's trailing twelve months (PER-06: FY(prior 10-K) − YTD(last year) +
  YTD(this year)) needs the prior 10-K. For the 10-Qs of the first year that 10-K is FY2021, which is not stored.
* **Scope:** **4,328 values** (`prior_before_db_window` 4,265 + `prior_input_before_window` 63), almost all 10-Qs with
  period end 2021-11 to 2022-10: enterprise FCF yield 1,165, asset turnover 255, receivables turnover 255, FCF margin
  243, FCF conversion 243, OCF margin 242, capex intensity 242, ROE 241, inventory turnover 221, ROA 204, ROIC 196+204,
  net debt / EBITDA 196, SBC-adjusted FCF yield 163 …
* **Not fixable from later filings:** the FY2022 10-K's comparative column holds FY2021, but it was published after
  these 10-Qs — using it is look-ahead (forbidden).
* **Fix:** load each company's FY2021 10-K (≈ 500 filings, gateway GETs only, a multi-hour live run), used only as the
  prior year. **Decision needed** (scope and run time).

### 3.4 Figures the SEC has but a rule refused (`has_refused`, 1,904)

* operating income from the notes: 1,208 values where it is the only refused input (3.1, L-26);
* debt, short-term investments, NCI, gross profit from the notes: refused on purpose in round #10 (L-28) — the face's
  other lines can contain them (TGT's short-term investments inside its cash, PHM's senior notes inside notes payable,
  BE's verified debt lines): read from the notes they would be counted twice;
* the rest: calibration, ambiguity, zero values (T-159c rules B–E).

### 3.5 Smaller rule-related buckets

* `ttm_term_missing` 1,162: 524 where the operating income TTM term is the gap (3.1 again);
* `ytd_only` 529: a 10-Q quarter not derivable (first 10-Q of a fiscal year in the window; REITs ESS, FRT, REG, PSA,
  KIM … whose capex is missing in one of the two filings — not yet root-caused);
* `prior_input_other` 800, `prior_gap_in_window` 439: prior-period inputs (short-term investments, redeemable NCI,
  cash) missing in the prior column or a prior filing missing inside the window — not yet root-caused one by one;
* `no_item_recorded` 1,718: the row lists no missing input (FCF yield 299, EBITDA 297, SBC-adjusted FCF yield 187,
  interest coverage 151 …) — the empty reason is not traceable from the row; to be classified;
* `has_missed` 88: the SEC has the input and no refusal was recorded — error candidates (inventory 34, PSX-type revenue
  19 …), to be read one by one;
* `no_facts` 288: 2026 filings newer than our SEC download — refresh pending.

## 4. Empty because the item does not exist for the company (mostly correct)

From `not_filed`, where it is the only missing input:

| metric | values | why |
|---|---|---|
| gross margin | 2,657 | no cost of sales and no gross profit on the face (services, banks, REITs) |
| inventory turnover | 1,399 (no cost of sales), 1,395 (no inventory), 1,353 (neither) | the company has no inventory or no cost of sales |
| SBC-adjusted FCF yield | 924 | no stock-based compensation line or fact |
| capex-based metrics | 862 | no capital expenditure line or fact |
| interest coverage | 547 | no interest expense (most have no debt) |
| EBITDA | 325 | no D&A line or fact |
| receivables turnover | 281 | no receivables line or fact |

These are correctly empty, but they are **labelled `not_reported`**, as if data were missing. Labelling them "not
applicable to this company" would not change a value, only the label.

## 5. What is not the cause

* **The data-mining repo** (news, `urls.db`) has nothing to do with financial statements.
* **The EDGAR gateway**: its problems found in the audit were fixed in rounds #3–#10 — dropped face cells (T-042) restored
  from the filing's own facts, 52/53-week cash flows arriving untagged (R2), lines tagged by the filer under another
  element (R1), debt lines verified per sector (overrides). For the gaps still open, the SEC's own companyfacts have no
  value under the item's concepts in **30,321** of the empty values: the data is not there in that form.

## 6. Decisions to take (what would fill what)

| # | decision | values it could fill (upper bound) | evidence |
|---|---|---|---|
| D-A | EBIT = pre-tax + interest expense when no operating-income line (non-financial; flagged) | up to ~16,350 (≈ 1,087 non-financial filings with both inputs) | §3.1 accuracy table |
| D-B | banks/insurers: operating metrics "not applicable" instead of "not reported" | 0 values (labels only; ~4,084) | §3.1 |
| D-C | inventory = 0 for the quick ratio when the filing proves none | 2,110 | §3.2 |
| D-D | load FY2021 10-Ks as prior year | 4,328 | §3.3 |
| D-E | ~~operating income from the segment notes~~ | **decided 2026-10-29: not read** (ASC 280 segment profit, C12) | §3.1 |
| D-F | relabel "item does not exist for the company" as not applicable | 0 values (labels only; ~9,000) | §4 |

Still on the reviewer's side, no decision needed: refresh the SEC download (288 `no_facts`), read the 88 `has_missed`
and the 1,718 `no_item_recorded` one by one, root-cause the 529 `ytd_only` and the 1,239 prior-input gaps.

## 6b. Decided 2026-10-29 (the user with reviewer A) — see `ROUND11_BRIEF.md`

D-A → **D-17 yes**, with the non-operating elimination (not "pre-tax + interest" alone; the "data vendors" sentence of
§3.1 had no source and is withdrawn). D-C → **D-18 yes** (inventory = 0 for the quick ratio only, with the zero-debt
proof). **D-B, D-D, D-E, D-F: no.** Also to do in round #11: the 88 `has_missed` one by one; every empty value records
its reason; table 1b's 202 ROIC values do have a recorded reason (`denominator_not_meaningful`) and are not "no_reason";
causes of `ytd_only` and the prior-period gaps; the companyfacts refresh. Not started.

## 7. Files

* `audit/r10/empty_split_after10.csv` — every empty value with its bucket and missing inputs;
* `pilot_a3/DATA_GAPS.md`, `pilot_a3/DATA_GAPS_inventory_after10.csv` — the per-filing, per-input gap inventory;
* `pilot_a3/LIMITATIONS.md` (sections A–G), `pilot_a3/DECISIONS.md` (D-1…D-16), `pilot_a3/FIXES.md` (#1…#10),
  `audit/FINDINGS.md` (F-A1…F-A23).

## After round #11 (FIXES #11, 2026-10-29) — before / after

The same classification (`audit/checks/empty_split_v4.py`: v3 plus D-14's `rule_not_meaningful`, the traced cause of an
empty value whose missing input the provenance does not name, the rule-input rows' own reason) on the database before
the round and on the applied copy. The stored rows are the same 357,231; the 2020-21 filings are new rows.

| cause (empty_split_v4) | before (357,231 stored rows) | after (same rows) | new 2020-21 filings (154,624 rows) |
|---|---|---|---|
| `not_filed` | 30,321 | 19,030 | 8,709 |
| `rule_not_applicable` | 24,955 | 25,002 | 10,890 |
| `rule_math` | 6,124 | 6,285 | 3,792 |
| `prior_before_db_window` | 4,265 | 415 | 7,398 |
| `has_refused` | 1,904 | 485 | 717 |
| `ttm_term_missing` | 1,162 | 157 | 154 |
| `no reason recorded` | 1,154 | 0 | 0 |
| `trace: TTM not formed` | 1,032 | 1,060 | 615 |
| `prior_input_other` | 800 | 885 | 566 |
| `ytd_only` | 529 | 468 | 332 |
| `trace: reason only / refused` | 514 | 505 | 447 |
| `prior_gap_in_window` | 439 | 618 | 191 |
| `no_facts` | 288 | 150 | 0 |
| `rule_not_meaningful` | 202 | 202 | 62 |
| `trace: market cap` | 172 | 104 | 163 |
| `has_missed` | 88 | 33 | 29 |
| `prior_input_before_window` | 63 | 0 | 62 |
| **total empty** | **74,012** | **55,399** | **34,127** |

What changed on the stored rows, by rule (`audit/r11/review_b11.csv`, engine 71b12a0): D-17 derived operating income 12,562 empty → value (`not_filed` / `has_refused` operating income, `ttm_term_missing`; rule (a)'s net-interest filings among them); D-D the 2020+ load 4,923 (`prior_before_db_window` 4,265 → 415, `prior_input_before_window` 63 → 0, the rule inputs' three-year bound and trailing years, market caps from the previous filing's cover count); D-18 1,276 quick ratios; Part 2 XOM's inventory 74; the companyfacts refresh 13 (`no_facts` 288 → 150); and 235 value → empty where F-A11 now sees a perimeter change (L-38) — which is why `prior_gap_in_window`, `prior_input_other` and `rule_math` grow a little (the newly formed values meet a negative base, a recast prior year, a missing comparative item). On the 2020-21 filings, D-19 gives the 487 filings whose period ends in 2019 their APP-00 type (`rule_not_applicable` there now includes the banks' and insurers' NA cells). **No empty value is left without a recorded reason** (the 1,154 rule inputs). What stays empty and why: LIMITATIONS H (L-32 … L-39).
