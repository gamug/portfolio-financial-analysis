# What to check on EDGAR — the corrected values, one by one

Database: `pilot_a3/financial.db`, sha256 `e1e56bd84f3f26cb…` (FIXES #8, 2026-10-28).

**Verified by the user on EDGAR, 2026-10-28: all 12 cases confirmed** (`USER_EDGAR_CHECK_2026-10-28.csv`).

Each case below says which filing to open, where to look in it, which numbers to find, and what the database
had before and has now. The link opens the filing's index page on sec.gov; click the main document (the `10-K` or
`10-Q` line). Amounts are in millions of dollars unless the filing itself reports in thousands (noted).

## Where the explanations are

| file | what it holds |
|---|---|
| `pilot_a3/FIXES.md` | one row per fix round (#1 … #8): what was wrong, how it was fixed, rows changed, checks |
| `audit/FINDINGS.md` | the cause of each error class, with the evidence (F-A1 … F-A20) |
| `pilot_a3/DECISIONS.md` | the judgment calls a rule did not settle (D-1 … D-10) |
| `pilot_a3/LIMITATIONS.md` | the values left empty or kept, and why (L-1 … L-25) |
| this file | the cases to check yourself |

## What my earlier report got wrong (corrected in FIXES #8)

* It said GM FY2021's enterprise value was computed. It was still empty. Now 171.9 B (case 1).
* It said Bloom Energy's FY2025 debt was fixed. The debt ratios and EV were still empty. Now filled (case 7).
* It said CEG FY2021's debt was restored. That filing was never processed. Now restored in full (case 12).
* It said only 9 Bloom Energy rows were left. The real list was 9 rows across BE and GM. All applied now.

The cause: the method compared rows only where the fix changed something, so rows that differed for other
reasons were never shown. Since #8, the whole database is compared row by row after every change.

---

## Debt (the balance sheet)

### 1. General Motors — FY2021 10-K
* Open: https://www.sec.gov/Archives/edgar/data/1467858/000146785822000034/0001467858-22-000034-index.htm
* Look at: Consolidated Balance Sheets, December 31, 2021. GM shows debt in two sections, Automotive and GM Financial.
* Find:
  * Short-term debt and current portion of long-term debt: Automotive **463**, GM Financial **33,257**
  * Long-term debt: Automotive **16,355**, GM Financial **59,304**
* Add up: 463 + 33,257 + 16,355 + 59,304 = **109,379**
* Database before: debt empty (debt ratios empty, enterprise value empty). Now: debt **109,379**; enterprise value
  **171,897** = market cap 85,123 + debt 109,379 − cash 20,067 − marketable securities 8,609 + noncontrolling
  interests 6,071.

### 2. Ford — FY2021 10-K
* Open: https://www.sec.gov/Archives/edgar/data/37996/000003799622000013/0000037996-22-000013-index.htm
* Look at: Consolidated Balance Sheets, December 31, 2021, sections "Company excluding Ford Credit" and "Ford Credit".
* Find:
  * Debt payable within one year: **3,175** and **46,517**
  * Long-term debt: **17,200** and **71,200**
* Add up: **138,092**
* Before: empty. Now: **138,092**.

### 3. Exelon — FY2023 10-K
* Open: https://www.sec.gov/Archives/edgar/data/1109357/000110935724000053/0001109357-24-000053-index.htm
* Look at: Exelon Corporation's own Consolidated Balance Sheets (the first one; the filing also contains each utility's).
* Find: Short-term borrowings **2,523**; Long-term debt due within one year **1,403**; Long-term debt **39,692**;
  Long-term debt to financing trusts **390**.
* Add up: **44,008**
* Before: empty. Now: **44,008**.

### 4. Consolidated Edison — 10-Q for March 31, 2025
* Open: https://www.sec.gov/Archives/edgar/data/1047862/000104786225000045/0001047862-25-000045-index.htm
* Look at: Con Edison, Inc.'s Consolidated Balance Sheet, March 31, 2025.
* Find: Notes payable **350**; Term loan **700**; Long-term debt **24,653**.
* Add up: **25,703**
* Before: **1,050** (the long-term debt line was missing: the filing tags it with an unusual element). Now: **25,703**.

### 5. T-Mobile — FY2021 10-K
* Open: https://www.sec.gov/Archives/edgar/data/1283699/000128369922000018/0001283699-22-000018-index.htm
* Look at: Consolidated Balance Sheets, December 31, 2021.
* Find: Short-term debt **3,378**; Short-term debt to affiliates **2,245**; Long-term debt **67,076**; Long-term debt to
  affiliates **1,494**.
* Add up: **74,193**
* Before: **70,454** (the two "to affiliates" lines, debt owed to Deutsche Telekom, were missing). Now: **74,193**.

### 6. PulteGroup — FY2023 10-K (reported in thousands)
* Open: https://www.sec.gov/Archives/edgar/data/822416/000082241624000010/0000822416-24-000010-index.htm
* Look at: Consolidated Balance Sheets, December 31, 2023.
* Find: Notes payable **1,962,218**; Financial Services debt **499,627** (thousands).
* Add up: **2,461,845** thousand = 2,462 million
* Before: **500** million (notes payable was missing). Now: **2,462**.

### 7. Bloom Energy — FY2025 10-K (reported in thousands)
* Open: https://www.sec.gov/Archives/edgar/data/1664703/000162828026006516/0001628280-26-006516-index.htm
* Look at: Consolidated Balance Sheets, December 31, 2025.
* Find: Recourse debt (non-current) **2,613,726**; Non-recourse debt (current) **4,153** (thousands).
* Add up: **2,617,879** thousand = 2,618 million
* Not counted, on purpose: "Financing obligations" (51,308 and 192,460) — these are sale-leaseback obligations,
  treated like leases (DECISIONS D-6).
* Before: debt ratios and enterprise value empty (an earlier rebuild had counted the debt twice, 5,236). Now: debt
  2,618; debt/equity **3.41**; enterprise value **20,739**.

### 8. Dominion Energy — FY2024 10-K, the December 31, 2023 column
* Open: https://www.sec.gov/Archives/edgar/data/715957/000095017025028387/0000950170-25-028387-index.htm
* Look at: Dominion Energy's Consolidated Balance Sheets, the 2023 column.
* Find: Securities due within one year **6,589**; Short-term debt **3,956**; Supplemental credit facility borrowings
  **450**; Total long-term debt **33,248**.
* Add up: **44,243**
* Before: **43,793** (the 450 credit-facility line was missing). Now: **44,243** (used in the 3-year cash coverage).

## Net income and growth (the income statement)

### 9. Entergy — FY2021 10-K (reported in thousands)
* Open: https://www.sec.gov/Archives/edgar/data/65984/000006598422000017/0000065984-22-000017-index.htm
* Look at: Entergy Corporation's Consolidated Income Statements, 2021.
* Find: Net income attributable to Entergy Corporation **1,118,492**; operating revenues **11,742,896** (thousands).
* Before: net income empty (net margin, ROE, growth empty in 2021–2023: 38 values). The filing reports the same
  net income twice, once rounded (1,118.5 million), and the engine read the two as a conflict. Now: net income
  **1,118.5 million**; net margin **9.5 %**.

### 10. AMETEK — FY2021 10-K (reported in thousands)
* Open: https://www.sec.gov/Archives/edgar/data/1037868/000103786822000009/0001037868-22-000009-index.htm
* Look at: Consolidated Statement of Income, the 2021 and 2019 columns.
* Find: Net sales 2021 **5,546,514**; Net sales 2019 **5,158,557** (thousands).
* Compute: (5,546,514 / 5,158,557)^(1/2) − 1 = **3.69 %** per year.
* Before: revenue CAGR empty (the engine looked for a 2018 column that is not in the statement). Now: **3.69 %**.
  The same error was fixed for CCI, HON, LH, MOS, MRO, REGN, WHR, WRK (43 values).

## A company that changed its name

### 11. Warner Bros. Discovery — 10-Q for March 31, 2022
* Open the 10-Q: https://www.sec.gov/Archives/edgar/data/1437107/000143710722000138/0001437107-22-000138-index.htm
* And Discovery's FY2021 10-K (same company, before the name change; stored under ticker DISCK):
  https://www.sec.gov/Archives/edgar/data/1437107/000143710722000031/0001437107-22-000031-index.htm
* Find: revenue FY2021 **12,191** (10-K); revenue Q1 2022 **3,159** and Q1 2021 **2,792** (10-Q).
* Compute the last 12 months: 12,191 + 3,159 − 2,792 = **12,558**.
* Also on the 10-K's cover page: Series A **169,580,151**, Series B **6,511,917**, Series C **330,153,753** shares
  (as of February 10, 2022) → 506.2 million shares → market cap **12,616**.
* Before: every 12-month metric and the market cap were empty (the engine looked for the 10-K under "WBD" only).
  Now: filled (44 values in 2022). Decision D-9.

## A company whose first annual report belongs to its predecessor

### 12. Constellation Energy — FY2021 10-K
* Open the FY2021 10-K: https://www.sec.gov/Archives/edgar/data/1868275/000186827522000020/0001868275-22-000020-index.htm
  — its statements are those of Constellation Energy Generation, LLC (CEG became a separate company on
  February 1, 2022).
* Or, simpler, CEG's FY2022 10-K, which shows the same 2021 figures as CEG's own:
  https://www.sec.gov/Archives/edgar/data/1868275/000186827523000014/0001868275-23-000014-index.htm
* Find (2021): Total operating revenues **19,649**; Net loss **−205**; Total assets **48,086**; Long-term debt
  **4,575**; Long-term debt due within one year **1,220**; Short-term borrowings **2,082**; Total shareholders'
  equity **11,219**.
* Debt: 4,575 + 1,220 + 2,082 = **7,877**; debt/equity = 7,877 / 11,219 = **0.70**.
* Before: the whole filing was empty (the gateway delivered no statements, 38 values). Now: 25 of 38 filled. The
  other 13 are empty by a rule: market cap, EV and the yields (the only share count is dated after the year end, the
  same rule as every first filing, L-7), gross margin and inventory turnover (not reported by a utility), quick ratio
  (an input not on the face), and the growth rates whose base is negative. The 2022 quarters' 12-month metrics (EBITDA, ROIC …) are now filled too. Decision D-10.

---

## L-21 — debt-free companies: check before you decide

Today these filings store debt as **empty**, so their debt ratios, enterprise value and ROIC are empty too. The
choice is whether to store **0** instead. What to check: the balance sheet has **no line for debt** — no "debt",
"borrowings", "notes payable", "convertible notes" or "commercial paper". Leases are not debt here (MET-07), and
neither are payables, accrued liabilities, deferred revenue or contingent consideration.

Six of the 135 filings, with every liability line the database holds for them (millions):

| # | company, filing | link | liability lines on the balance sheet |
|---|---|---|---|
| L1 | Intuitive Surgical, FY2025 10-K | https://www.sec.gov/Archives/edgar/data/1035267/000103526726000010/0001035267-26-000010-index.htm | Accounts payable 255.1; Accrued compensation 648.4; Deferred revenue 506.7; Other accrued 596.0; Other long-term 510.8; **Total liabilities 2,517.0** |
| L2 | Vertex, FY2025 10-K | https://www.sec.gov/Archives/edgar/data/875320/000087532026000056/0000875320-26-000056-index.htm | Accounts payable 461.7; Accrued expenses 2,971.2; Other current 428.3; Operating lease 1,846.5; Other long-term 1,269.5; **Total liabilities 6,977.2** |
| L3 | Palantir, FY2023 10-K | https://www.sec.gov/Archives/edgar/data/1321655/000132165524000022/0001321655-24-000022-index.htm | Accounts payable 12.1; Accrued 223.0; Deferred revenue 246.9 + 28.0; Customer deposits 209.8 + 1.5 (missing from my first list — your check found it); Operating lease 54.2 + 175.2; Other 10.7; **Total liabilities 961.5** |
| L4 | Copart, FY2026 10-K (July 31, 2026) | https://www.sec.gov/Archives/edgar/data/900075/000119312526405731/0001193125-26-405731-index.htm | Payables and accrued 634.7; Deferred revenue 32.5; Income taxes 21.4 + 9.1; Lease liabilities 15.2 + 73.2; Deferred taxes 127.9; **Total liabilities 913.9** |
| L5 | Monster Beverage, FY2025 10-K | https://www.sec.gov/Archives/edgar/data/865752/000110465926020831/0001104659-26-020831-index.htm | Accounts payable 566.0; Accrued 306.1; Promotional allowances 384.1; Deferred revenue 45.3 + 160.0; Compensation 114.0; Income taxes 32.3; Other 127.1; **Long-term debt "—" for 2025** (373.95 in 2024; repaid) — your check |
| L6 | Incyte, FY2025 10-K | https://www.sec.gov/Archives/edgar/data/879169/000087916926000010/0000879169-26-000010-index.htm | Accounts payable 209.9; Compensation 228.1; Accrued and other 1,031.5; Finance lease 4.5 + 30.2; Contingent consideration 41.1 + 79.9; Other 165.3; **Total liabilities 1,790.5** |

**Verified by the user 2026-10-28: all six debt 0** (`USER_EDGAR_CHECK_L21_2026-10-28.csv`); applied as FIXES #9 / D-11 — 148 filings, 21 companies. The proposal was: store **0** only where both hold for that filing (no
debt line on the face, and no SEC debt fact), flagged `debt_absent_verified`, re-checked per filing before applying.
That fills EV, ROIC and the debt ratios for the 135 filings (ISRG 19, INCY 19, MKTX 15, ERIE 14, DECK 9, PAYC 9,
CPRT 8, PLTR 8, VRTX 7, GEV 6, TER 6, DASH 5, EXPD 5, EME 2, PANW 2, MNST 1). If not, they stay empty.

## Round #10 (FIXES #10, 2026-10-29) — the new readings, one filing each

**Verified by the user 2026-10-29: all 18 confirmed** (R1–R6 approved, C1–C11 confirmed, C12 kept not read);
`USER_EDGAR_CHECK_R10_2026-10-29.md`.

Applied after your answers Q1–Q9 and E1–E7. These are **samples to confirm** the rules read the right line; the values
are read back from the installed database (sha256 1de09ad4…). Open the link, click the 10-K / 10-Q document, find the
statement and the line. **R1–R6 are my own calls (not in your Q1–Q9 answers): please check those first.**

| # | rule | filing (index link) | where | what to confirm | value stored |
|---|---|---|---|---|---|
| R1 | Q4 extended to receivables (my call) | [Kinder Morgan FY2021 10-K](https://www.sec.gov/Archives/edgar/data/1506307/000150630722000018/0001506307-22-000018-index.htm) | balance sheet | "Accounts receivable" with **no** allowance line beside it | 1,611 M |
| R2 | gross receivables less the allowance line (my call) | [Pinnacle West FY2021 10-K](https://www.sec.gov/Archives/edgar/data/764622/000076462222000014/0000764622-22-000014-index.htm) | balance sheet | "Customer and other receivables" 391.9 M and "Allowance for doubtful accounts" (25.4) M | 366.6 M |
| R3 | Q3 incl. ore stockpiles (my call) | [Newmont FY2021 10-K](https://www.sec.gov/Archives/edgar/data/1164727/000116472722000007/0001164727-22-000007-index.htm) | balance sheet, current assets | "Inventories" 930 M **and** "Stockpiles and ore on leach pads" 857 M — are both inventory (Note 17 / 18)? | 1,787 M |
| R4 | NCI income in parts (D-16, my call) | [Sempra 10-Q 2026-03-31](https://www.sec.gov/Archives/edgar/data/1032208/000103220826000030/0001032208-26-000030-index.htm) | income statement | net income less "Earnings attributable to noncontrolling interests" and the "contingently redeemable" line = the earnings attributable to Sempra | 1,037 M |
| R5 | the only tax line, tagged "current" (my call) | [Camden Property FY2021 10-K](https://www.sec.gov/Archives/edgar/data/906345/000090634522000009/0000906345-22-000009-index.htm) | income statement | "Income tax expense" is the only tax line | 1.9 M (pretax 314.3 M) |
| R6 | "Interest expense" before capitalized interest (my call) | [Southwest FY2021 10-K](https://www.sec.gov/Archives/edgar/data/92380/000009238022000007/0000092380-22-000007-index.htm) | income statement, other expenses | "Interest expense" 467 M (a separate "Capitalized interest" line follows) | 467 M |
| C1 | Q1 net interest | [Conagra 10-Q 2021-11-28](https://www.sec.gov/Archives/edgar/data/23217/000156459022000559/0001564590-22-000559-index.htm) | income statement | "Interest expense, net" | 94.9 M |
| C2 | Q2b D&A parts | [Honeywell FY2021 10-K](https://www.sec.gov/Archives/edgar/data/773840/000077384022000018/0000773840-22-000018-index.htm) | cash flow statement | "Depreciation" + "Amortization" | 1,223 M |
| C3 | Q3 inventory lines | [DTE Energy FY2021 10-K](https://www.sec.gov/Archives/edgar/data/936340/000093634022000077/0000936340-22-000077-index.htm) | balance sheet | "Fuel and gas" 368 M + "Materials, supplies, and other" 490 M (not "Gas inventory equalization") | 858 M |
| C4 | Q3 FIFO less LIFO | [Kroger FY2021 10-K](https://www.sec.gov/Archives/edgar/data/56873/000155837022004595/0001558370-22-004595-index.htm) | balance sheet | "FIFO inventory" 8,353 M, "LIFO reserve" (1,570) M | 6,783 M |
| C5 | Q4 gross total | [McCormick 10-Q 2022-02-28](https://www.sec.gov/Archives/edgar/data/63754/000006375422000015/0000063754-22-000015-index.htm) | balance sheet | "Total inventory", no reserve line | 1,244.2 M |
| C6 | Q7 from the notes | [Microsoft 10-Q 2021-12-31](https://www.sec.gov/Archives/edgar/data/789019/000156459022002324/0001564590-22-002324-index.htm) | the note "Other income (expense), net" | "Interest expense" (the statement shows only "Other income (expense), net") | 525 M |
| C7 | Q7 cash beside a restricted-cash line | [HP Inc. 10-Q 2023-01-31](https://www.sec.gov/Archives/edgar/data/47217/000004721723000019/0000047217-23-000019-index.htm) | balance sheet / cash note | the face line is "Cash, cash equivalents and restricted cash" 1,769 M; cash and cash equivalents alone | 1,405 M |
| C8 | Q8 derived quarter | [Kellogg 10-Q 2022-07-02](https://www.sec.gov/Archives/edgar/data/55067/000162828022021083/0001628280-22-021083-index.htm) | cash flow statement (26 weeks) and the Q1 10-Q (13 weeks) | operating cash flow H1 − Q1, capex H1 − Q1 | OCF 478 M, capex 129 M (prior year 452 M / 128 M) |
| C9 | R1 face line under another element | [Costco 10-Q 2024-11-24](https://www.sec.gov/Archives/edgar/data/909832/000090983224000079/0000909832-24-000079-index.htm) | income statement | "INCOME BEFORE INCOME TAXES" | 2,306 M (tax rate 22.0 %) |
| C10 | redeemable NCI now in EV | [Digital Realty FY2021 10-K](https://www.sec.gov/Archives/edgar/data/1297996/000155837022002195/0001558370-22-002195-index.htm) | balance sheet, between liabilities and equity | "Redeemable noncontrolling interests" (NCI used = that + the equity NCI) | NCI 519.2 M |
| C11 | Q9 not meaningful | [Palantir FY2023 10-K](https://www.sec.gov/Archives/edgar/data/1321655/000132165524000022/0001321655-24-000022-index.htm) | balance sheet, 2022 column | invested capital 8.76 M (equity + debt − cash − marketable securities) against total assets 3,461 M | ROIC empty |
| C12 | L-26, **decided 2026-10-29: not read** (ASC 280 segment profit) — operating income in the notes | Honeywell 10-Q 2026-03-31 (`0000773840-26-000057`) | the segment note | is "Operating income" 2,129 M the consolidated GAAP operating income? (pre-tax income 886 M) | not read |

## Still yours to decide

* ~~**L-21**~~ — done (FIXES #9).
* **T-143 part B** needs LLM spend, so it waits for your approval.
* ~~**Round #10's checks R1–R6 and C12**~~ — done 2026-10-29: 18/18 confirmed, C12 kept not read (`USER_EDGAR_CHECK_R10_2026-10-29.md`).

## Round #11 (FIXES #11) — to check on EDGAR (the user)

Each value below is in the applied database; the arithmetic uses the filing's own face lines.

| # | filing | value in the database | check on the filing |
|---|---|---|---|
| E11-1 | REG 10-K FY2021 (0000950170-22-001418) | derived operating income 452.179 M (D-17) | Total revenues 1,166.161 M − Total operating expenses 713.982 M = 452.179 M |
| E11-2 | PFE 10-K FY2021 (0000078003-22-000027) | derived operating income **19,433 M** (was 20,724 M: the user found the interest counted twice; fixed, dc95a0a) | pre-tax 24,311 M − "Other (income)/deductions—net" income 4,878 M; the 1,291 M interest expense (notes) is inside that line, so it is not added back (`ebit_interest_inside_nonoperating`) — **confirmed by the user** (19,433 M) |
| E11-3 | OXY 10-K FY2021 (0000797468-22-000008) | derived operating income 4,566 M | Total revenues and other income 26,314 M − (Total costs 23,362 M − interest and debt expense 1,614 M) |
| E11-4 | XOM 10-K FY2021 (0000034088-22-000011) | derived operating income 26,310 M | pre-tax 31,234 M + interest 947 M + non-service pension 786 M − income from equity affiliates 6,657 M |
| E11-5 | XOM 10-Q 2023-Q1 (0000034088-23-000030) | inventory 23,642 M; quick ratio 1.104 | Crude oil, products and merchandise 19,458 M + Materials and supplies 4,184 M; (97,224 − 23,642) / 66,666 |
| E11-6 | ADBE 10-Q 2026-Q2 (0000796343-26-000112) | quick ratio 0.751 (D-18: no inventory line) | total current assets / total current liabilities; no inventory on the balance sheet |
| E11-7 | UNP 10-K FY2025 (0000100885-26-000037) | quick ratio empty (D-18 refused) | the balance sheet shows "Materials and supplies" (an inventory) |
| E11-8 | HON 10-Q 2026-Q2 (0000773840-26-000124) | derived operating income **1,737 M** (was 8,366 M with the gain kept; reviewer A added `DeconsolidationGainOrLossAmount` to the D-17 list, dc95a0a) | 8,366 M − the 6,629 M "Gain on deconsolidation of subsidiary" = 1,737 M — **the user's figure** |
| E11-9 | AAPL 10-K FY2021 (0000320193-21-000105, loaded) | net income 94,680 M, revenue 365,817 M | the FY2021 income statement |
| E11-10 | T 10-Q 2022-Q2 | trailing-year cash-flow ratios empty (F-A11, L-38) | the 2022 10-Q recasts the 2021 YTD for WarnerMedia's discontinued operations |
| E11-11 | BKR 10-K FY2025 (0001701605-26-000007) | derived operating income 3,342 M, flag `ebit_net_interest` (rule (a)) | the face prints no operating income and only "Interest expense, net" 222 M: income before income taxes 2,877 M + interest expense, net 222 M + "Other (income) expense, net" 243 M = 3,342 M (= total revenue 27,733 M − the operating costs and expenses) — **confirmed by the user 2026-10-29** (FY2025 statement p.12, no operating-income subtotal; top-down 27,733 − cost of revenue 21,189 − SG&A 2,387 − R&D 600 − restructuring 215 = 3,342 M) |
| E11-12 | OGN 10-Q 2021-Q1 (0001821825-21-000005, loaded) | derived operating income 467 M, flag `ebit_zero_interest_proven` (rule (b)) | no interest expense anywhere in the filing and no debt on the balance sheet at 2021-03-31 (before the April 2021 notes): income before income taxes 467 M + 0 — **confirmed by the user 2026-10-29** (Organon still part of Merck at 2021-03-31; the separation notes April 2021, the distribution 2021-06-02) |
| E11-13 | PFE 10-K FY2019 (0000078003-20-000014, loaded; filed 2020-02-27) | derived operating income 13,174 M (D-17 with the deconsolidation entry; D-19: the FY2019 period now reads PFE's APP-00 type — before, unknown type, empty) | revenues 51,750 M − cost of sales 10,219 − SI&A 14,350 − R&D 8,650 − amortization 4,610 − restructuring 747 = 13,174 M; the 8,086 M "(Gain) on completion of Consumer Healthcare JV transaction" is removed, and the interest sits inside "Other (income)/deductions—net" — **confirmed by the user 2026-10-29** (the 10-K face and Exhibit 99.1; the gain is Note 2C) |

## FIXES #12 (D-20) — cover counts corrected from the printed cover (checked on EDGAR by the user: all 16 confirmed, 2026-10-29)

Each figure is the one printed on the cover of the named document; the database held the old value.

| # | filing | old value in the database | now | check on the filing's cover |
|---|---|---|---|---|
| E12-1 | AJG 0001564590-20-021299 (filing_cover_shares) | 189,621,000,000,000 | 189,621,000 | the cover prints 189,621,000; its iXBRL tag has scale 6 — **confirmed by the user 2026-10-29** |
| E12-2 | AJG 0001564590-20-035149 (filing_cover_shares) | 191,469,000,000,000 | 191,469,000 | the cover prints 191,469,000; its iXBRL tag has scale 6 — **confirmed by the user 2026-10-29** |
| E12-3 | CCL 0000815097-21-000027 (filing_cover_shares) | 932,485,510,000 | 932,485,510 | the cover prints 932,485,510 (Carnival Corporation common stock); iXBRL scale 3 — **confirmed by the user 2026-10-29** |
| E12-4 | CMG 0001058090-22-000027 (filing_cover_shares) | 27,962 | 27,962,448 | the cover prints 27,962,448; the tag wraps only its first digits — **confirmed by the user 2026-10-29** |
| E12-5 | EIX 0000827052-20-000026 (filing_cover_shares) | 362,570,075,000,000 | 362,570,075 | the cover prints 362,570,075; iXBRL scale 6 — **confirmed by the user 2026-10-29** |
| E12-6 | NTAP 0000950170-24-023115 (filing_cover_shares) | 20,637,666 | 206,376,661 | the cover prints 206,376,661; the tag wraps only its first digits — **confirmed by the user 2026-10-29** |
| E12-7 | PKG 0000950170-23-016955 (filing_cover_shares) | 89,932,185,000 | 89,932,185 | the cover prints 89,932,185; iXBRL scale 3 — **confirmed by the user 2026-10-29** |
| E12-8 | PKG 0000950170-24-055165 (filing_cover_shares) | 89,797,979,000 | 89,797,979 | the cover prints 89,797,979; iXBRL scale 3 — **confirmed by the user 2026-10-29** |
| E12-9 | PKG 0001193125-26-074129 (filing_cover_shares) | 89,213,394,000 | 89,213,394 | the cover prints 89,213,394; iXBRL scale 3 — **confirmed by the user 2026-10-29** |
| E12-10 | RMD 0000943819-21-000017 (filing_cover_shares) | 145,681 | 145,681,186 | the cover prints 145,681,186; the tag wraps only its first digits — **confirmed by the user 2026-10-29** |
| E12-11 | BALL 0001558370-22-001251 (audit_prior_cover_shares) | 0 | 321,495,737 | the visible cover prints 321,495,737 shares at 2022-02-14 (the stored 0 is a hidden fact) — **confirmed by the user 2026-10-29** |
| E12-12 | VTRS 0001193125-20-140498 (filing_cover_shares) | 100 | removed (empty, MKT-04) | the cover prints 100 shares (Upjohn Inc., before the Mylan combination): removed — **confirmed by the user 2026-10-29** |
| E12-13 | VTRS 0001193125-20-216755 (filing_cover_shares) | 100 | removed (empty, MKT-04) | the cover prints 100 shares (Upjohn Inc., before the Mylan combination): removed — **confirmed by the user 2026-10-29** |
| E12-14 | VTRS 0001193125-20-290294 (filing_cover_shares) | 100 | removed (empty, MKT-04) | the cover prints 100 shares (Upjohn Inc., before the Mylan combination): removed — **confirmed by the user 2026-10-29** |
| E12-15 | SW 0001410578-24-000999 (audit_prior_cover_shares) | 100 | removed (empty, MKT-04) | the cover prints 100 shares (Smurfit WestRock plc before the combination): removed — **confirmed by the user 2026-10-29** |
| E12-16 | WFC 0000072971-23-000138 (filing_cover_shares) | 1,823,028,137 | 3,658,783,337 | the 10-Q printed 1,823,028,137; its 10-Q/A 0000072971-23-000142 (2023-08-04) corrects the cover to 3,658,783,337 — **confirmed by the user 2026-10-29** |

## FIXES #13 (D-20, the WFC rule) — the cover a 10-Q/A corrects (to check on EDGAR, the user)

| # | filing | old value in the database | now | check on EDGAR |
|---|---|---|---|---|
| E13-1 | SNDK 10-Q for 2024-12-27 (0002023554-25-000004, filed 2025-03-07; filing_cover_shares) | 114,863,251 | 144,863,251 | the 10-Q's cover prints 114,863,251 shares at 2025-02-27; its 10-Q/A 0002023554-25-000016 (filed 2025-03-17) says it is filed to "correct an error in the number of shares outstanding on the cover page of the Form 10-Q as of the last practicable date from 114,863,251 to 144,863,251"; the 2025-03-28 10-Q's market capitalization is now 7,094.0 M (144,863,251 shares) |
