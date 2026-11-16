# Data gaps — the empty values caused by data not found (not by our rules)

## AFTER ROUND #10 (2026-10-29) — done, and what is left

Your answers Q1–Q9 and your E1–E7 check were applied as FIXES #10 (database sha256 `1de09ad4…`; D-12–D-16). **10,976
empty values were filled**, 136 ROIC values became "not meaningful" (Q9), 519 existing values changed, almost all
because redeemable or minority NCI is now in EV and ROIC (DLR, WBA, RTX). Empty values: 84,852 → **74,012** (20.7 %).
The data-type gaps of section 2 went from 31,390 (recounted: the first count included 422 *derived* values that are
stored — PSX's and APA's revenue among them; E3's revenue was already right) to **18,071**:

| class | before | after #10 | what is left |
|---|---|---|---|
| no SEC fact, no line on the face | 17,166 | 15,849 | the company does not report it |
| quarter filed year-to-date only (Q8) | 8,193 | 550 | no previous 10-Q in the window, or an item missing in one of the two filings (REITs' capex: ESS, FRT, REG, PSA, KIM …) |
| SEC has it, refused (Q7) | 2,710 | 782 | operating income from segment notes (281 gaps, **L-26 — your decision, check C12**); debt, short-term investments, NCI, gross profit from notes (L-28: they would be counted twice) |
| a line under another element (R1, RULE groups) | 2,974 | 729 | LOOK 336 (correctly empty); RULE 220 (your Q5/Q6/Q2a: parts, restricted cash, D&A with impairment or "other"); REAL 127 — debt lines the total debt already contains through the verified overrides (SLB, BXP, VTR, BRK.B, DOC, ARE, MTD, WELL, SMCI), EPAM's contract assets, PODD's restricted cash, TKO's "and other", first filings in the window; REVIEW 46 |
| no SEC facts in the audit's copy | 166 | 147 | 2026 filings newer than the SEC download (refresh pending) |

**What you can still do (none of it is needed for the database to be correct):** the round's check list in
`CHECK_ON_EDGAR.md` (round #10): R1–R6 are my own calls (KMI and PNW receivables, NEM's ore stockpiles, SRE's NCI
parts, CPT's tax line, LUV's interest line); C1–C11 confirm one filing per rule; C12 is a decision (Honeywell's operating
income in the notes). Full list after #10: `DATA_GAPS_inventory_after10.csv`; the list below is the state before #10.

---

## START HERE — what this document says, and the only two things you need to do

**In one paragraph.** About a quarter of the database's values are empty. Two thirds of those are empty on purpose: a
rule says the metric does not apply (a bank has no current ratio) or cannot be computed (a negative base). The rest
are empty because an input was not found. I tested every one of them against the SEC's data and against the filing's
own statements. Result: most are genuinely not reported by the company; but in **about 1,150 filing-items the number
IS printed in the filing**, under a label the engine does not recognise — data we did not take. And in about 1,450
more, the filing prints a *related* line where a rule decides whether we may use it. That is where your input is
needed.

**You only need to do two things. Everything below "1." is supporting evidence; you do not need to read it.**

### Task 1 — answer 9 yes/no questions (each one decides a whole group at once)

| # | the question in plain words | how many | my recommendation |
|---|---|---|---|
| Q1 | A company shows **"Interest expense, net"** (interest paid minus interest earned). Use it as its interest expense? | 985 filings, 86 companies | **Yes** (marked as "net") |
| Q2a | A line says **"Depreciation, amortization *and impairment*"** (or "… and other"). Use it as D&A? | 118 filings, 31 companies | **Only in years with no impairment** |
| Q2b | Depreciation and amortization are shown on **two separate lines**. Add them up? | 59 filings, 15 companies | **Yes** |
| Q3 | Inventory is shown on **several lines with no total** (e.g. "Natural gas" + "Materials and supplies"). Add them up? | 89 filings, 8 companies | **Yes** |
| Q4 | McCormick shows **"Total inventory"** at gross (no reserve line). Use it? | 15 filings | **Yes** |
| Q5 | Only **a part** is shown (one segment's capex, part of cost of sales). Keep it empty? | 156 filings, 12 companies | **Yes, keep empty** |
| Q6 | Cash that **includes restricted cash** in the same line. Keep refusing it as cash? | 29 filings, 3 companies | **Yes, keep refusing** |
| Q7 | The number is **only in the notes**, not on the statement (mostly interest expense). Use the company's own filed number? | 2,710 filings | **Yes** (marked "from notes") |
| Q8 | Quarterly cash flow is filed **only year-to-date**. Compute the quarter as this YTD minus last quarter's YTD? | 8,193 filings | **Yes** (marked "derived") |
| Q9 | ROIC comes out **extreme** when cash almost equals equity (Palantir 2023: 1,082 %). Mark such ROIC as "not meaningful"? | 174 values | **Yes** |

You can answer "all as recommended", or change any of them.

### Task 2 — check 7 examples on EDGAR (like the last two checks)

These are samples of the "number is printed but we did not take it" group. Open the link, find the line, confirm it
is that item and the value. If they match, I fix the whole group (about 1,150 filing-items) the same way.

| # | company, filing | where to look | the line to find | value |
|---|---|---|---|---|
| E1 | Procter & Gamble, FY2022 10-K — [open](https://www.sec.gov/Archives/edgar/data/80424/000008042422000064/0000080424-22-000064-index.htm) | income statement | "Earnings from Continuing Operations Before Income Taxes" (pretax income) | 17,995 M |
| E2 | NVR, FY2021 10-K — [open](https://www.sec.gov/Archives/edgar/data/906163/000090616322000008/0000906163-22-000008-index.htm) | income statement | "Profit before taxes" | 1,590.4 M |
| E3 | Phillips 66, FY2021 10-K — [open](https://www.sec.gov/Archives/edgar/data/1534701/000153470122000078/0001534701-22-000078-index.htm) | income statement | "Sales and other operating revenues" (revenue) | 111,476 M |
| E4 | Dominion Energy, FY2021 10-K — [open](https://www.sec.gov/Archives/edgar/data/715957/000156459022006589/0001564590-22-006589-index.htm) | cash flow statement | "Plant construction and other property additions (including nuclear fuel)" (capex) | 5,960 M |
| E5 | Delta Air Lines, 10-Q March 31, 2022 — [open](https://www.sec.gov/Archives/edgar/data/27904/000002790422000006/0000027904-22-000006-index.htm) | balance sheet | "Fuel, expendable parts and supplies inventories, net" (inventory) | 1,292 M |
| E6 | Illinois Tool Works, FY2024 10-K — [open](https://www.sec.gov/Archives/edgar/data/49826/000004982625000007/0000049826-25-000007-index.htm) | income statement | "Net Income" — and confirm there is **no** minority-interest line | 3,488 M |
| E7 | Mondelez, FY2021 10-K — [open](https://www.sec.gov/Archives/edgar/data/1103982/000110398222000003/0001103982-22-000003-index.htm) | balance sheet | "Cash and cash equivalents" | 3,546 M |

Today the metrics that need these lines are **empty** in the database: P&G's and NVR's effective tax rate and
ROIC, Phillips 66's operating margin (not "Total revenues and other income", 114,852 M — that adds other income),
Dominion's free-cash-flow metrics and capex intensity, Delta's inventory turnover and quick ratio, ITW's net margin
and ROE, Mondelez's cash ratio and enterprise value. After the fix they are computed from the values above.

---

Measured on the database after FIXES #8 (sha256 `e1e56bd8…`); since then FIXES #9 (D-11) filled 763 of these values — the debt-free
companies' debt, EV, ROIC and debt ratios (L-21) — so 84,852 values are empty now. Measured 2026-10-28 on the whole database
(`audit/checks/empty_split_v3.py`, `audit/checks/item_gaps.py`, `audit/checks/gap_report.py`). Full list, one row per
filing and input: `pilot_a3/DATA_GAPS_inventory.csv` (31,812 rows). Finding F-A21 in `audit/FINDINGS.md`.

## 1. All empty values, by why they are empty

85,615 of the 357,231 stored values are empty (24.0 %).

| why (the reason the engine recorded) | values | kind |
|---|---|---|
| not applicable (applicability matrix, e.g. a bank has no current ratio) | 25,222 | **rule** |
| growth base ≤ 0 (MET-04) | 2,891 | **rule** |
| denominator ≤ 0 (MET-09) | 2,417 | **rule** |
| not reported (the engine found no such input) | 32,334 | data |
| missing input | 14,118 | data |
| capture failure | 2,843 | data |
| prior year missing (the prior filing is not stored) | 4,629 | data (window) |
| no reason recorded (3-year utility coverage 788, `value_inputs` 373) | 1,161 | data (window) |

## 2. The data-type gaps, per filing and input

For every empty value of a data-type reason, the input it misses on the filing's own column, tested three ways: does
the SEC hold it for that filing; did the engine see it and refuse it; does the stored statement show a line with that
item's caption. **31,812 (filing, input) gaps**:

| class | gaps | what it means | yours to check? |
|---|---|---|---|
| no SEC fact, no line with that caption on the face | 17,550 | the company does not report it (inventory 2,863, gross profit 2,702, cost of sales 2,641, short-term investments 2,154, operating income 2,124, interest expense 936, current assets / liabilities 770 each — unclassified balance sheets) | spot-check only |
| 10-Q cash flow filed year-to-date only | 8,193 | the quarter value is not in the filing (operating cash flow 4,255, capex 3,925); a quarter could be derived as YTD − previous YTD — a methodology choice | decision (Q8) |
| **a line with the item's caption under another element** | **2,993** | **section 3** | **yes** |
| the SEC has it, the engine saw it and a rule refused it | 2,710 | mostly values only in the notes, not on the face (interest expense 1,066, D&A 309, SBC 232) | decision (Q7) |
| **the SEC has it, not taken** | **200** | **section 3** (pretax "domestic" 101, inventory lines 71, PSX revenue 19, D capex 7, ITW net income 2) | **yes** |
| no SEC facts for the filing in the audit's copy | 166 | 2026 filings newer than the SEC download | I refresh and re-test |

Checked so it is not a wrong value: where the face shows debt lines under other elements (SLB, BXP, MTD, ARE, PHM,
DISH, VTR, DOC, SMCI …), the stored **total debt already includes them** (e.g. SLB FY2021 909 + 13,286 = 14,195 M
stored). Only the separate short-term item is missing, which empties the working-capital part of the enterprise FCF
yield.

## 3. The 3,193 gaps where the data is in the filing — every group, with one example to open

Each group is one item + the element the filing used. My reading of the caption is the label:

* **REAL** — the line is the item, under an element the engine does not read. Data we did not take: I propose to read
  it (round #9). 1,156 gaps, 1,861 empty values.
* **RULE** — the line is related but a rule decides (questions Q1–Q6 below). 1,451 gaps.
* **LOOK** — the caption looks like the item but is another line; empty is correct. 503 gaps.
* **REVIEW** — groups of 1–2 filings I have not read one by one; I read them in round #9. 83 gaps.

To check a row: open the link (the filing index; click the 10-K / 10-Q document), find the caption on the statement,
confirm it is the item and the value.

### On the face under another element: data we did not take (REAL)

| item | element on the face | filings | companies | example: filing | caption | value |
|---|---|---|---|---|---|---|
| capital_expenditure | `us-gaap_PaymentsToAcquirePropertyPlantAndEquipment` | 10 | 4: AZO, COST, DPZ, LHX | [AZO 10-Q 2022-02-12](https://www.sec.gov/Archives/edgar/data/866787/000155837022003914/0001558370-22-003914-index.htm) | Capital expenditures | -208.1 M |
| capital_expenditure | `d_PaymentsToAcquirePropertyPlantAndEquipmentIncludingNuclearFuel` | 10 | 1: D | [D 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/715957/000156459022006589/0001564590-22-006589-index.htm) | Plant construction and other property additions (including nuclear fuel) | -5,960.0 M |
| capital_expenditure | `tko_PaymentsToAcquirePropertyPlantAndEquipmentAndOther` | 3 | 1: TKO | [TKO 10-K 2023-12-31](https://www.sec.gov/Archives/edgar/data/1973266/000156276224000038/0001562762-24-000038-index.htm) | Purchases of property, buildings and equipment and other assets | -48.6 M |
| capital_expenditure | `us-gaap_PaymentsToAcquireProductiveAssets` | 2 | 1: PEP | [PEP 10-Q 2022-06-11](https://www.sec.gov/Archives/edgar/data/77476/000007747622000043/0000077476-22-000043-index.htm) | Capital spending | -1,499.0 M |
| capital_expenditure | `us-gaap_PaymentsForConstructionInProcess` | 1 | 1: AEP | [AEP 10-K 2024-12-31](https://www.sec.gov/Archives/edgar/data/4904/000000490425000027/0000004904-25-000027-index.htm) | Construction Expenditures | -7,630.7 M |
| cash | `us-gaap_CashAndCashEquivalentsAtCarryingValueIncludingDiscontinuedOperations` | 25 | 2: FISV, MDLZ | [MDLZ 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1103982/000110398222000003/0001103982-22-000003-index.htm) | Cash and cash equivalents | 3,546.0 M |
| cash | `us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations` | 21 | 2: MMM, MRO | [MMM 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/66740/000006674022000010/0000066740-22-000010-index.htm) | Cash and cash equivalents | 4,564.0 M |
| cash | `podd_CashCashEquivalentsAndRestrictedCashCurrent` | 19 | 1: PODD | [PODD 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1145197/000114519722000012/0001145197-22-000012-index.htm) | Cash and cash equivalents | 791.6 M |
| cash | `cvx_CashAndCashEquivalentsExcludingTimeDeposits` | 8 | 1: CVX | [CVX 10-Q 2024-09-30](https://www.sec.gov/Archives/edgar/data/93410/000009341024000059/0000093410-24-000059-index.htm) | Cash and cash equivalents | 4,699.0 M |
| cogs | `us-gaap_DirectOperatingCosts` | 19 | 1: EXPD | [EXPD 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/746515/000156459022010381/0001564590-22-010381-index.htm) | Directly related cost of transportation and other expenses | 12,058.2 M |
| cogs | `wsm_CostOfGoodsSoldAndOccupancyExpenses` | 4 | 1: WSM | [WSM 10-K 2022-01-30](https://www.sec.gov/Archives/edgar/data/719955/000162828022007494/0001628280-22-007494-index.htm) | Cost of goods sold | 4,614.0 M |
| income_tax | `us-gaap_CurrentIncomeTaxExpenseBenefit` | 21 | 2: CPT, J | [CPT 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/906345/000090634522000009/0000906345-22-000009-index.htm) | Income tax expense | -1.9 M |
| interest_expense | `us-gaap_InterestCostsIncurred` | 27 | 3: ALK, LUV, UAL | [LUV 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/92380/000009238022000007/0000092380-22-000007-index.htm) | Interest expense | 467.0 M |
| interest_expense | `nee_InterestExpenseAggregateExpenses` | 19 | 1: NEE | [NEE 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/753308/000075330822000014/0000753308-22-000014-index.htm) | Interest Expense | -1,270.0 M |
| interest_expense | `us-gaap_InvestmentIncomeInvestmentExpense` | 19 | 1: SBAC | [SBAC 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1034054/000103405422000002/0001034054-22-000002-index.htm) | Interest expense | -352.9 M |
| interest_expense | `awk_InterestExpenseNonoperating` | 10 | 1: AWK | [AWK 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1410636/000141063622000048/0001410636-22-000048-index.htm) | Interest expense | -403.0 M |
| interest_expense | `deck_InterestAndOtherExpense` | 10 | 1: DECK | [DECK 10-Q 2021-12-31](https://www.sec.gov/Archives/edgar/data/910521/000091052122000007/0000910521-22-000007-index.htm) | Interest expense | 1.0 M |
| interest_expense | `sats_InterestExpenseNetOfAmountCapitalized` | 10 | 1: ECHO | [ECHO 10-Q 2024-03-31](https://www.sec.gov/Archives/edgar/data/1415404/000155837024007171/0001558370-24-007171-index.htm) | Interest expense, net of amounts capitalized (Note 2) | -99.4 M |
| interest_expense | `exr_InterestExpenseExcludingAmortizationOfDebtDiscountPremium` | 9 | 1: EXR | [EXR 10-Q 2024-06-30](https://www.sec.gov/Archives/edgar/data/1289490/000162828024034445/0001628280-24-034445-index.htm) | Interest expense | -137.1 M |
| interest_expense | `cnp_InterestAndDebtExpenseAndOtherCharges` | 4 | 1: CNP | [CNP 10-Q 2024-03-31](https://www.sec.gov/Archives/edgar/data/1130310/000113031024000066/0001130310-24-000066-index.htm) | Interest expense and other finance charges | -198.0 M |
| interest_expense | `dish_InterestExpenseNetOfAmountCapitalized` | 4 | 1: DISH | [DISH 10-Q 2022-03-31](https://www.sec.gov/Archives/edgar/data/1001082/000155837022007502/0001558370-22-007502-index.htm) | Interest expense, net of amounts capitalized | -4.6 M |
| inventory | `us-gaap_InventoryRealEstate` | 24 | 2: AVB, DHI | [AVB 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/915912/000091591222000005/0000915912-22-000005-index.htm) | For-sale condominium inventory | 146.5 M |
| inventory | `us-gaap_AirlineRelatedInventoryNet` | 19 | 1: DAL | [DAL 10-Q 2022-03-31](https://www.sec.gov/Archives/edgar/data/27904/000002790422000006/0000027904-22-000006-index.htm) | Fuel, expendable parts and supplies inventories, net of allowance for obsolescen | 1,292.0 M |
| inventory | `us-gaap_AirlineRelatedInventory` | 19 | 1: LUV | [LUV 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/92380/000009238022000007/0000092380-22-000007-index.htm) | Inventories of parts and supplies, at cost | 537.0 M |
| inventory | `nem_InventoryOtherThanOreStockpilesOnLeachpadsNetOfReserves` | 19 | 1: NEM | [NEM 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1164727/000116472722000007/0001164727-22-000007-index.htm) | Inventories (Note 17) | 930.0 M |
| inventory | `us-gaap_InventoryOperativeBuilders` | 19 | 1: PHM | [PHM 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/822416/000082241622000007/0000822416-22-000007-index.htm) | House and land inventory | 9,047.6 M |
| inventory | `us-gaap_PublicUtilitiesInventory` | 18 | 2: DUK, XEL | [XEL 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/72903/000007290322000010/0000072903-22-000010-index.htm) | Inventories | 631.0 M |
| inventory | `para_FilmCapitalizedCostCurrent` | 11 | 1: SKYD | [SKYD 10-K 2022-12-31](https://www.sec.gov/Archives/edgar/data/813828/000081382823000005/0000813828-23-000005-index.htm) | Programming and other inventory | 1,342.0 M |
| inventory | `us-gaap_PrepaidExpenseCurrent` | 6 | 1: T | [T 10-Q 2022-06-30](https://www.sec.gov/Archives/edgar/data/732717/000073271722000093/0000732717-22-000093-index.htm) | Inventories | 3,241.0 M |
| inventory | `len_InventoryOwned` | 3 | 1: LEN | [LEN 10-K 2023-11-30](https://www.sec.gov/Archives/edgar/data/920760/000162828024002371/0001628280-24-002371-index.htm) | Inventory owned | 15,360.2 M |
| long_term_debt | `us-gaap_SeniorNotes` | 15 | 2: BXP, DLR | [BXP 10-Q 2022-03-31](https://www.sec.gov/Archives/edgar/data/1037540/000165642322000020/0001656423-22-000020-index.htm) | Unsecured senior notes, net | 9,486.4 M |
| long_term_debt | `us-gaap_DebtLongtermAndShorttermCombinedAmount` | 14 | 1: VTR | [VTR 10-Q 2022-03-31](https://www.sec.gov/Archives/edgar/data/740260/000074026022000125/0000740260-22-000125-index.htm) | Senior notes payable and other debt | 12,413.7 M |
| minority_interest | `us-gaap_OtherMinorityInterests` | 19 | 1: WEC | [WEC 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/783325/000010781522000116/0000107815-22-000116-index.htm) | Noncontrolling interests | 169.7 M |
| minority_interest | `lh_NoncontrollingInterestMezzanineEquity` | 10 | 1: LH | [LH 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/920148/000092014822000015/0000920148-22-000015-index.htm) | Noncontrolling interest | 20.6 M |
| minority_interest | `us-gaap_TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests` | 3 | 1: TDY | [TDY 10-K 2023-01-01](https://www.sec.gov/Archives/edgar/data/1094285/000109428523000053/0001094285-23-000053-index.htm) | Redeemable Noncontrolling Interest | 3.7 M |
| net_income | `us-gaap_ProfitLoss` | 3 | 2: ITW, SRE | [ITW 10-K 2024-12-31](https://www.sec.gov/Archives/edgar/data/49826/000004982625000007/0000049826-25-000007-index.htm) | Net Income | 3,488.0 M |
| operating_cash_flow | `us-gaap_NetCashProvidedByUsedInOperatingActivities` | 14 | 4: AZO, COST, DPZ, PEP | [AZO 10-Q 2022-02-12](https://www.sec.gov/Archives/edgar/data/866787/000155837022003914/0001558370-22-003914-index.htm) | Net cash provided by operating activities | 1,139.7 M |
| operating_cash_flow | `chd_NetCashProvidedByOperatingActivities` | 1 | 1: CHD | [CHD 10-Q 2025-03-31](https://www.sec.gov/Archives/edgar/data/313927/000095017025061624/0000950170-25-061624-index.htm) | Net Cash Provided By Operating Activities, Total | 185.7 M |
| pretax_income | `us-gaap_IncomeLossAttributableToParent` | 117 | 12: BR, CASY, COP, COST, DECK, DOV, FLEX, LITE … | [CASY 10-K 2022-04-30](https://www.sec.gov/Archives/edgar/data/726958/000072695822000065/0000726958-22-000065-index.htm) | Income before income taxes | 440.7 M |
| pretax_income | `us-gaap_IncomeLossIncludingPortionAttributableToNoncontrollingInterest` | 104 | 9: CRL, CVNA, CZR, ITW, KMB, ON, PG, PNW … | [CVNA 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1690820/000169082022000080/0001690820-22-000080-index.htm) | Net loss before income taxes | -286.0 M |
| pretax_income | `us-gaap_IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic` | 99 | 7: CPT, ES, KVUE, NI, NVR, PHM, ZION | [NVR 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/906163/000090616322000008/0000906163-22-000008-index.htm) | Profit before taxes | 1,590.4 M |
| pretax_income | `all_IncomeLossFromOperationsBeforeIncomeTaxExpenseBenefit` | 18 | 1: ALL | [ALL 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/899051/000089905122000015/0000899051-22-000015-index.htm) | Income from operations before income tax expense | 6,448.0 M |
| pretax_income | `bk_IncomeLossFromContinuingOperationsBeforeIncomeTaxes` | 14 | 1: BNY | [BNY 10-Q 2022-03-31](https://www.sec.gov/Archives/edgar/data/1390777/000139077722000049/0001390777-22-000049-index.htm) | Income before income taxes | 918.0 M |
| pretax_income | `lh_IncomeLossfromContinuingOperationsBeforeIncomeTaxesandNoncontrollingInterest` | 14 | 1: LH | [LH 10-Q 2022-03-31](https://www.sec.gov/Archives/edgar/data/920148/000092014822000025/0000920148-22-000025-index.htm) | Earnings before income taxes | 640.1 M |
| pretax_income | `mcd_IncomeLossFromContinuingOperationsBeforeIncomeTaxes` | 14 | 1: MCD | [MCD 10-Q 2022-03-31](https://www.sec.gov/Archives/edgar/data/63908/000006390822000016/0000063908-22-000016-index.htm) | Income before provision for income taxes | 1,541.2 M |
| pretax_income | `orcl_IncomeLossFromContinuingOperationsIncludingNoncontrollingInterestBeforeIncomeTaxesExtraordinaryItems` | 14 | 1: ORCL | [ORCL 10-Q 2022-02-28](https://www.sec.gov/Archives/edgar/data/1341439/000156459022009859/0001564590-22-009859-index.htm) | Income before income taxes | 2,840.0 M |
| pretax_income | `pfg_IncomeLossFromContinuingOperationsBeforeIncomeTaxes` | 14 | 1: PFG | [PFG 10-Q 2022-03-31](https://www.sec.gov/Archives/edgar/data/1126328/000110465922055741/0001104659-22-055741-index.htm) | Income before income taxes | 428.0 M |
| pretax_income | `pg_IncomeLossFromContinuingOperationsBeforeIncomeTaxes` | 10 | 1: PG | [PG 10-K 2022-06-30](https://www.sec.gov/Archives/edgar/data/80424/000008042422000064/0000080424-22-000064-index.htm) | Earnings from Continuing Operations Before Income Taxes | 17,995.0 M |
| pretax_income | `cma_IncomeLossFromContinuingOperationsBeforeIncomeTaxes` | 8 | 1: CMA | [CMA 10-K 2022-12-31](https://www.sec.gov/Archives/edgar/data/28412/000002841223000094/0000028412-23-000094-index.htm) | Income before income taxes | 1,476.0 M |
| receivables | `us-gaap_AccountsReceivableGrossCurrent` | 38 | 2: KMI, PNW | [KMI 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1506307/000150630722000018/0001506307-22-000018-index.htm) | Accounts receivable | 1,611.0 M |
| receivables | `us-gaap_PremiumsAndOtherReceivablesNet` | 24 | 2: AJG, CNC | [AJG 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/354190/000156459022005714/0001564590-22-005714-index.htm) | Premiums and fees receivable | 11,753.1 M |
| receivables | `bro_CommissionFeesAndOtherReceivable` | 18 | 1: BRO | [BRO 10-Q 2022-03-31](https://www.sec.gov/Archives/edgar/data/79282/000095017022008555/0000950170-22-008555-index.htm) | Commission, fees and other receivables | 613.5 M |
| receivables | `us-gaap_AccountsAndNotesReceivableNet` | 18 | 1: PNR | [PNR 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/77360/000007736022000006/0000077360-22-000006-index.htm) | Accounts receivable, net of allowances of $9.1 and $8.4, respectively | 534.3 M |
| receivables | `us-gaap_ContractWithCustomerAssetNet` | 17 | 1: LITE | [LITE 10-K 2022-07-02](https://www.sec.gov/Archives/edgar/data/1633978/000162828022023617/0001628280-22-023617-index.htm) | Accounts receivable, net | 262.0 M |
| receivables | `epam_TradeReceivablesAndContractAssetsAfterAllowanceForCreditLossCurrent` | 15 | 1: EPAM | [EPAM 10-Q 2022-03-31](https://www.sec.gov/Archives/edgar/data/1352010/000135201022000036/0001352010-22-000036-index.htm) | Trade receivables and contract assets, net of allowance of $14,229 and $5,521, r | 901.3 M |
| receivables | `epam_TradeReceivablesandContractAssetsafterAllowanceforCreditLossCurrent` | 3 | 1: EPAM | [EPAM 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1352010/000135201022000020/0001352010-22-000020-index.htm) | Trade receivables and contract assets, net of allowance of $5,521 and $4,886, re | 768.9 M |
| redeemable_nci | `us-gaap_TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests` | 12 | 3: HAS, LKQ, TDY | [HAS 10-K 2021-12-26](https://www.sec.gov/Archives/edgar/data/46080/000004608022000023/0000046080-22-000023-index.htm) | Redeemable noncontrolling interests | 23.9 M |
| revenue | `psx_RevenuesAndOtherIncome` | 19 | 1: PSX | [PSX 10-K 2023-12-31](https://www.sec.gov/Archives/edgar/data/1534701/000153470124000078/0001534701-24-000078-index.htm) | Total Revenues and Other Income | 149,890.0 M |
| revenue | `apa_RevenuesAndOther` | 8 | 1: APA | [APA 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1841666/000178403122000009/0001784031-22-000009-index.htm) | Total revenues and other | 7,928.0 M |
| short_term_debt | `slb_ShortTermBorrowingsAndLongTermDebtCurrent` | 19 | 1: SLB | [SLB 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/87347/000156459022002421/0001564590-22-002421-index.htm) | Short-term borrowings and current portion of long-term debt | 909.0 M |
| short_term_debt | `us-gaap_LineOfCredit` | 14 | 2: DOC, WELL | [DOC 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/765880/000162828022002117/0001628280-22-002117-index.htm) | Bank line of credit and commercial paper | 1,166.0 M |
| short_term_debt | `us-gaap_DebtAndCapitalLeaseObligations` | 12 | 1: BRK.B | [BRK.B 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1067983/000156459022007322/0001564590-22-007322-index.htm) | Notes payable and other borrowings | 1,750.0 M |
| short_term_debt | `are_LongTermLineOfCreditAndCommercialPaper` | 5 | 1: ARE | [ARE 10-Q 2025-06-30](https://www.sec.gov/Archives/edgar/data/1035443/000103544325000179/0001035443-25-000179-index.htm) | Unsecured senior line of credit and commercial paper | 1,098.0 M |
| short_term_debt | `peak_BankLineOfCreditAndCommercialPaper` | 5 | 1: DOC | [DOC 10-Q 2025-06-30](https://www.sec.gov/Archives/edgar/data/765880/000162828025036063/0001628280-25-036063-index.htm) | Bank line of credit and commercial paper | 775.0 M |
| short_term_debt | `mtd_ShortTermDebtAndLongTermDebtCurrentMaturities` | 5 | 1: MTD | [MTD 10-Q 2022-03-31](https://www.sec.gov/Archives/edgar/data/1037646/000103764622000015/0001037646-22-000015-index.htm) | Short-term borrowings and current maturities of long-term debt | 105.3 M |
| short_term_debt | `smci_ShortTermDebtAndLongTermDebtCurrentMaturities` | 4 | 1: SMCI | [SMCI 10-Q 2024-12-31](https://www.sec.gov/Archives/edgar/data/1375365/000137536525000006/0001375365-25-000006-index.htm) | Lines of credit and current portion of term loans | 155.0 M |
| stock_based_compensation | `us-gaap_StockOptionPlanExpense` | 10 | 2: SO, TDY | [SO 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/92122/000009212222000003/0000092122-22-000003-index.htm) | Stock based compensation expense | 144.0 M |
| stock_based_compensation | `dlr_AmortizationDeferredStockBasedCompensation` | 5 | 1: DLR | [DLR 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1297996/000155837022002195/0001558370-22-002195-index.htm) | Amortization of share-based compensation | 84.1 M |
| stock_based_compensation | `be_SharebasedCompensationAndIssuanceOfStockAndWarrantsForServicesOrClaims` | 4 | 1: BE | [BE 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1664703/000166470322000034/0001664703-22-000034-index.htm) | Stock-based compensation expense | 73.3 M |
| stock_based_compensation | `us-gaap_RestrictedStockExpense` | 3 | 1: RHI | [RHI 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/315213/000031521322000018/0000315213-22-000018-index.htm) | Stock-based compensation | 55.9 M |

### Related line, a rule decides (RULE)

| item | element on the face | filings | companies | example: filing | caption | value |
|---|---|---|---|---|---|---|
| capital_expenditure | `nee_CapitalExpendituresOfFPL` | 8 | 1: NEE | [NEE 10-K 2022-12-31](https://www.sec.gov/Archives/edgar/data/753308/000075330823000019/0000753308-23-000019-index.htm) | Capital expenditures of FPL | -9,067.0 M |
| capital_expenditure | `ed_PaymentsNonUtilityConstructionExpenditures` | 7 | 1: ED | [ED 10-K 2023-12-31](https://www.sec.gov/Archives/edgar/data/1047862/000104786224000012/0001047862-24-000012-index.htm) | Non-utility construction expenditures | -141.0 M |
| capital_expenditure | `nee_CapitalExpendituresOfFPLSegment` | 1 | 1: NEE | [NEE 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/753308/000075330822000014/0000753308-22-000014-index.htm) | Capital expenditures of FPL Segment | -6,626.0 M |
| cash | `us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents` | 29 | 3: ERIE, HAS, SW | [HAS 10-K 2021-12-26](https://www.sec.gov/Archives/edgar/data/46080/000004608022000023/0000046080-22-000023-index.htm) | Cash and cash equivalents including restricted cash of $35.8 in 2021 and $73.2 i | 1,019.2 M |
| cogs | `us-gaap_OtherCostOfOperatingRevenue` | 39 | 3: INVH, T, XEL | [T 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/732717/000073271722000015/0000732717-22-000015-index.htm) | Other cost of revenues (exclusive of depreciation and amortization shown separat | 31,232.0 M |
| cogs | `us-gaap_CostDirectMaterial` | 19 | 1: CPRT | [CPRT 10-K 2022-07-31](https://www.sec.gov/Archives/edgar/data/900075/000090007522000050/0000900075-22-000050-index.htm) | Cost of vehicle sales | 585.2 M |
| depreciation_amortization | `us-gaap_AdjustmentForAmortization` | 69 | 20: AON, ECL, GILD, GRMN, HON, HUM, INTU, JKHY … | [AON 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/315293/000162828022003180/0001628280-22-003180-index.htm) | Amortization and impairment of intangible assets | 147.0 M |
| depreciation_amortization | `us-gaap_AmortizationOfIntangibleAssets` | 18 | 4: ACGL, BX, TFC, USB | [ACGL 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/947484/000094748422000015/0000947484-22-000015-index.htm) | Amortization of intangible assets | 83.0 M |
| depreciation_amortization | `us-gaap_Depreciation` | 13 | 5: BBWI, CSX, GOOG, SWKS, UNP | [CSX 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/277948/000027794822000009/0000277948-22-000009-index.htm) | Depreciation | 1,420.0 M |
| depreciation_amortization | `bro_AdjustmentForAmortizationExcludingAccretionAmortizationOfDebtDiscountsAndPremiumsInvestments` | 5 | 1: BRO | [BRO 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/79282/000095017022001654/0000950170-22-001654-index.htm) | Amortization | 119.6 M |
| depreciation_amortization | `itw_AmortizationandImpairmentofIntangibleAssets` | 5 | 1: ITW | [ITW 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/49826/000004982622000006/0000049826-22-000006-index.htm) | Amortization and impairment of intangible assets | 133.0 M |
| depreciation_amortization | `mar_DepreciationAmortizationAndOther` | 5 | 1: MAR | [MAR 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1048286/000162828022002666/0001628280-22-002666-index.htm) | Depreciation, amortization, and other | 295.0 M |
| depreciation_amortization | `msft_DepreciationAmortizationAndOther` | 5 | 1: MSFT | [MSFT 10-K 2022-06-30](https://www.sec.gov/Archives/edgar/data/789019/000156459022026876/0001564590-22-026876-index.htm) | Depreciation, amortization, and other | 14,460.0 M |
| depreciation_amortization | `us-gaap_AmortizationOfDeferredCharges` | 5 | 1: MTD | [MTD 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1037646/000103764622000008/0001037646-22-000008-index.htm) | Amortization | 63.1 M |
| depreciation_amortization | `pcg_DepreciationAmortizationDecommissioning` | 5 | 1: PCG | [PCG 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1004980/000100498022000009/0001004980-22-000009-index.htm) | Depreciation, amortization, and decommissioning | 3,403.0 M |
| depreciation_amortization | `stt_NoncashAdjustmentsForDepreciationAmortizationAccretion` | 5 | 1: STT | [STT 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/93751/000009375122000424/0000093751-22-000424-index.htm) | Other non-cash adjustments for depreciation, amortization and accretion, net | 1,312.0 M |
| depreciation_amortization | `tsla_DepreciationAmortizationAndImpairment` | 5 | 1: TSLA | [TSLA 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1318605/000095017022000796/0000950170-22-000796-index.htm) | Depreciation, amortization and impairment | 2,911.0 M |
| depreciation_amortization | `avgo_Amortizationofintangibleandrightofuseassets` | 4 | 1: AVGO | [AVGO 10-K 2022-10-30](https://www.sec.gov/Archives/edgar/data/1730168/000173016822000118/0001730168-22-000118-index.htm) | Amortization of intangible and right-of-use assets | 4,455.0 M |
| depreciation_amortization | `cien_AmortizationOfIntangibleAssetsExcludingAmortizationIncludedInPropertyPlantAndEquipment` | 4 | 1: CIEN | [CIEN 10-K 2022-10-29](https://www.sec.gov/Archives/edgar/data/936395/000093639522000065/0000936395-22-000065-index.htm) | Amortization of intangible assets | 44.3 M |
| depreciation_amortization | `cinf_DepreciationAmortizationAndOther` | 4 | 1: CINF | [CINF 10-K 2022-12-31](https://www.sec.gov/Archives/edgar/data/20286/000002028623000008/0000020286-23-000008-index.htm) | Depreciation, amortization and other | 127.0 M |
| depreciation_amortization | `isrg_DepreciationandGainLossonDispositionofPropertyPlantEquipment` | 4 | 1: ISRG | [ISRG 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1035267/000103526722000014/0001035267-22-000014-index.htm) | Depreciation and loss on disposal of property, plant, and equipment, net | 282.8 M |
| depreciation_amortization | `lite_AmortizationAndWriteOffOfAcquiredIntangibleAssets` | 4 | 1: LITE | [LITE 10-K 2023-07-01](https://www.sec.gov/Archives/edgar/data/1633978/000162828023030374/0001628280-23-030374-index.htm) | Amortization and write-off of acquired intangible assets | 149.0 M |
| depreciation_amortization | `trow_AmortizationOfAcquisitionRelatedAssetsAndRetentionArrangements` | 4 | 1: TROW | [TROW 10-K 2022-12-31](https://www.sec.gov/Archives/edgar/data/1113169/000111316923000007/0001113169-23-000007-index.htm) | Amortization and impairment of acquisition-related assets and retention arrangem | 420.1 M |
| depreciation_amortization | `us-gaap_CostOfGoodsAndServicesSoldAmortization` | 4 | 1: WTW | [WTW 10-K 2022-12-31](https://www.sec.gov/Archives/edgar/data/1140536/000095017023004303/0000950170-23-004303-index.htm) | Amortization | 312.0 M |
| depreciation_amortization | `mtb_DepreciationAndAmortization1` | 3 | 1: MTB | [MTB 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/36270/000156459022005400/0001564590-22-005400-index.htm) | Depreciation and amortization of premises and equipment | 224.3 M |
| depreciation_amortization | `nke_AmortizationImpairmentAndOther` | 3 | 1: NKE | [NKE 10-K 2022-05-31](https://www.sec.gov/Archives/edgar/data/320187/000032018722000038/0000320187-22-000038-index.htm) | Amortization, impairment and other | 123.0 M |
| depreciation_amortization | `swks_AmortizationOfIntangibleAssetsIncludingInventoryStepUp` | 3 | 1: SWKS | [SWKS 10-K 2023-09-29](https://www.sec.gov/Archives/edgar/data/4127/000000412723000030/0000004127-23-000030-index.htm) | Amortization of intangible assets | 225.9 M |
| interest_expense | `us-gaap_InterestIncomeExpenseNonoperatingNet` | 502 | 54: AAP, ABNB, AMTM, AZO, BLDR, CBRE, CDW, CHTR … | [LW 10-Q 2021-11-28](https://www.sec.gov/Archives/edgar/data/1679273/000155837022000090/0001558370-22-000090-index.htm) | Interest expense, net | 82.4 M |
| interest_expense | `us-gaap_InterestIncomeExpenseNet` | 474 | 47: BKR, CAG, CBRE, CDW, CHTR, CPAY, CPRT, CSX … | [CAG 10-Q 2021-11-28](https://www.sec.gov/Archives/edgar/data/23217/000156459022000559/0001564590-22-000559-index.htm) | Interest expense, net | 94.9 M |
| interest_expense | `us-gaap_InterestExpenseOther` | 12 | 1: F | [F 10-Q 2022-03-31](https://www.sec.gov/Archives/edgar/data/37996/000003799622000024/0000037996-22-000024-index.htm) | Interest expense on Company debt excluding Ford Credit | 308.0 M |
| interest_expense | `us-gaap_InterestRevenueExpenseNet` | 9 | 2: FMC, TJX | [FMC 10-Q 2022-03-31](https://www.sec.gov/Archives/edgar/data/37785/000003778522000040/0000037785-22-000040-index.htm) | Interest expense, net | 29.9 M |
| inventory | `EnergyRelatedInventory` | 23 | 2: DTE, XOM | [DTE 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/936340/000093634022000077/0000936340-22-000077-index.htm) | (SEC fact; several lines on the face) | EnergyRelatedInventory 368.0 M, InventoryRawMaterialsAndSupplies 490.0 M |
| inventory | `us-gaap_FIFOInventoryAmount` | 20 | 2: ABMD, KR | [KR 10-K 2022-01-29](https://www.sec.gov/Archives/edgar/data/56873/000155837022004595/0001558370-22-004595-index.htm) | FIFO inventory | 8,353.0 M |
| inventory | `us-gaap_EnergyRelatedInventoryNaturalGasInStorage` | 19 | 1: CNP | [CNP 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1130310/000113031022000023/0001130310-22-000023-index.htm) | Natural gas and coal inventory | 186.0 M |
| inventory | `us-gaap_InventoryGross` | 15 | 1: MKC | [MKC 10-Q 2022-02-28](https://www.sec.gov/Archives/edgar/data/63754/000006375422000015/0000063754-22-000015-index.htm) | Total inventory | 1,244.2 M |
| inventory | `etr_FuelInventoryAtAverageCost` | 11 | 1: ETR | [ETR 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/65984/000006598422000017/0000065984-22-000017-index.htm) | Fuel inventory - at average cost | 154.6 M |
| inventory | `dte_GasInventoryEqualizationCurrent` | 10 | 1: DTE | [DTE 10-Q 2022-03-31](https://www.sec.gov/Archives/edgar/data/936340/000093634022000123/0000936340-22-000123-index.htm) | Gas inventory equalization | 108.0 M |
| inventory | `InventoryRawMaterialsAndSupplies` | 6 | 2: D, LNT | [D 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/715957/000156459022006589/0001564590-22-006589-index.htm) | (SEC fact; several lines on the face) | InventoryRawMaterialsAndSupplies 1,167.0 M |
| pretax_income | `acgl_IncomeLossFromContinuingOperationsBeforeEarningsFromAffiliatesIncomeTaxesNoncontrollingInterest` | 14 | 1: ACGL | [ACGL 10-Q 2022-03-31](https://www.sec.gov/Archives/edgar/data/947484/000094748422000037/0000947484-22-000037-index.htm) | Income (loss) before income taxes and income (loss) from operating affiliates | 184.5 M |
| receivables | `us-gaap_OtherReceivables` | 20 | 2: FIS, TPL | [TPL 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1811074/000181107422000014/0001811074-22-000014-index.htm) | Accounts receivable and accrued receivables, net | 95.2 M |
| receivables | `us-gaap_BilledContractReceivables` | 19 | 1: FIX | [FIX 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1035983/000155837022001753/0001558370-22-001753-index.htm) | Billed accounts receivable, less allowance for credit losses of $8,808 and $9,08 | 773.7 M |
| receivables | `elv_SelfFundedReceivablesNet` | 17 | 1: ELV | [ELV 10-Q 2022-06-30](https://www.sec.gov/Archives/edgar/data/1156039/000115603922000081/0001156039-22-000081-index.htm) | Self-funded receivables | 3,844.0 M |

### Caption looks like the item but it is another line (correctly empty) (LOOK)

| item | element on the face | filings | companies | example: filing | caption | value |
|---|---|---|---|---|---|---|
| capital_expenditure | `us-gaap_ConstructionInProgressExpendituresIncurredButNotYetPaid` | 7 | 1: AEP | [AEP 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/4904/000000490422000024/0000004904-22-000024-index.htm) | Construction Expenditures Included in Current Liabilities as of December 31, | 1,180.4 M |
| capital_expenditure | `us-gaap_CapitalExpendituresIncurredButNotYetPaid` | 6 | 2: CMS, COST | [CMS 10-K 2023-12-31](https://www.sec.gov/Archives/edgar/data/811156/000081115624000044/0000811156-24-000044-index.htm) | Capital expenditures not paid | 265.0 M |
| capital_expenditure | `tpl_DecreaseIncreaseInAccountsPayableRelatedToCapitalExpenditures` | 5 | 1: TPL | [TPL 10-K 2023-12-31](https://www.sec.gov/Archives/edgar/data/1811074/000181107424000015/0001811074-24-000015-index.htm) | Increase in accounts payable related to capital expenditures | 0.4 M |
| capital_expenditure | `tko_CapitalExpendituresIncludedInCurrentLiabilities` | 3 | 1: TKO | [TKO 10-K 2025-12-31](https://www.sec.gov/Archives/edgar/data/1973266/000119312526071651/0001193125-26-071651-index.htm) | Capital expenditures included in current liabilities | 14.7 M |
| cogs | `us-gaap_LaborAndRelatedExpense` | 16 | 1: IBKR | [IBKR 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1381197/000138119722000010/0001381197-22-000010-index.htm) | Cost of Revenue | 399.0 M |
| cogs | `us-gaap_PublicUtilitiesAllowanceForFundsUsedDuringConstructionCapitalizedCostOfEquity` | 8 | 1: ETR | [ETR 10-K 2024-12-31](https://www.sec.gov/Archives/edgar/data/65984/000006598425000012/0000065984-25-000012-index.htm) | Public Utilities, Allowance for Funds Used During Construction, Capitalized Cost | 133.0 M |
| cogs | `ivz_PreferredStockRedemptionPremiumAndExciseTaxes` | 3 | 1: IVZ | [IVZ 10-K 2025-12-31](https://www.sec.gov/Archives/edgar/data/914208/000091420826000079/0000914208-26-000079-index.htm) | Cost of preferred share repurchase | -240.0 M |
| depreciation_amortization | `us-gaap_AmortizationOfFinancingCostsAndDiscounts` | 13 | 3: AVB, BE, VICI | [BE 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1664703/000166470322000034/0001664703-22-000034-index.htm) | Amortization of debt issuance costs and premium, net | 3.8 M |
| depreciation_amortization | `us-gaap_AmortizationOfDebtDiscountPremium` | 12 | 3: AVB, EG, INVH | [AVB 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/915912/000091591222000005/0000915912-22-000005-index.htm) | Amortization of debt discount | 2.7 M |
| depreciation_amortization | `us-gaap_AccretionAmortizationOfDiscountsAndPremiumsInvestments` | 12 | 3: CB, MET, PGR | [CB 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/896159/000089615922000005/0000896159-22-000005-index.htm) | Accretion (Amortization) of Discounts and Premiums, Investments | 332.0 M |
| depreciation_amortization | `us-gaap_DeferredPolicyAcquisitionCostAmortizationExpense` | 5 | 1: GL | [GL 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/320335/000032033522000006/0000320335-22-000006-index.htm) | Amortization of deferred policy acquisition costs | 603.8 M |
| depreciation_amortization | `eqr_OperatingAndFinanceLeaseRightOfUseAssetAmortization` | 5 | 1: VMRK | [VMRK 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/906107/000156459022005566/0001564590-22-005566-index.htm) | Amortization of right-of-use assets | 13.3 M |
| depreciation_amortization | `grmn_ChangeInMarketableSecuritiesRelatedToUnrealizedAppreciationDepreciation` | 4 | 1: GRMN | [GRMN 10-K 2021-12-25](https://www.sec.gov/Archives/edgar/data/1121788/000095017022001303/0000950170-22-001303-index.htm) | Change in marketable securities related to unrealized (depreciation) appreciatio | -32.6 M |
| long_term_debt | `us-gaap_LongTermDebtCurrent` | 4 | 1: FFIV | [FFIV 10-Q 2022-12-31](https://www.sec.gov/Archives/edgar/data/1048695/000104869523000011/0001048695-23-000011-index.htm) | Current portion of long-term debt | 0.0 M |
| minority_interest | `us-gaap_LiabilitiesAndStockholdersEquity` | 42 | 4: CPRT, DASH, MKTX, TDY | [DASH 10-Q 2022-09-30](https://www.sec.gov/Archives/edgar/data/1792789/000162828022028457/0001628280-22-028457-index.htm) | Total liabilities, redeemable non-controlling interests and stockholders’ equity | 9,552.0 M |
| minority_interest | `us-gaap_Liabilities` | 9 | 1: LH | [LH 10-Q 2022-03-31](https://www.sec.gov/Archives/edgar/data/920148/000092014822000025/0000920148-22-000025-index.htm) | Noncontrolling interest | 9,986.7 M |
| operating_cash_flow | `us-gaap_IncreaseDecreaseInOtherOperatingCapitalNet` | 1 | 1: LHX | [LHX 10-Q 2025-06-27](https://www.sec.gov/Archives/edgar/data/202058/000020205825000151/0000202058-25-000151-index.htm) | Other operating activities | -130.0 M |
| operating_income | `us-gaap_NoninterestIncomeOtherOperatingIncome` | 19 | 1: NTRS | [NTRS 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/73124/000007312422000071/0000073124-22-000071-index.htm) | Other Operating Income | 243.9 M |
| operating_income | `all_IncomeLossFromOperationsBeforeIncomeTaxExpenseBenefit` | 18 | 1: ALL | [ALL 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/899051/000089905122000015/0000899051-22-000015-index.htm) | Income from operations before income tax expense | 6,448.0 M |
| operating_income | `wfc_OperatingLosses` | 16 | 1: WFC | [WFC 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/72971/000007297122000096/0000072971-22-000096-index.htm) | Operating losses | 1,568.0 M |
| operating_income | `us-gaap_OtherNonoperatingIncomeExpense` | 11 | 3: ADM, LHX, MAA | [ADM 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/7084/000000708422000008/0000007084-22-000008-index.htm) | Other Nonoperating Income (Expense) | -94.0 M |
| operating_income | `hes_RevenuesAndOtherNonOperatingIncome` | 9 | 1: HES | [HES 10-K 2023-12-31](https://www.sec.gov/Archives/edgar/data/4447/000162828024006845/0001628280-24-006845-index.htm) | Total revenues and non-operating income | 10,645.0 M |
| operating_income | `us-gaap_IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments` | 9 | 1: REG | [REG 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/910606/000095017022001418/0000950170-22-001418-index.htm) | Income from operations before equity in income of investments in real estate par | 319.2 M |
| operating_income | `us-gaap_Revenues` | 5 | 1: HES | [HES 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/4447/000162828022004524/0001628280-22-004524-index.htm) | Total revenues and non-operating income | 7,583.0 M |
| operating_income | `us-gaap_NonoperatingIncomeExpense` | 4 | 1: KMX | [KMX 10-K 2022-02-28](https://www.sec.gov/Archives/edgar/data/1170010/000117001022000017/0001170010-22-000017-index.htm) | Nonoperating Income (Expense) | -34.6 M |
| operating_income | `us-gaap_OtherNonoperatingIncome` | 4 | 1: LHX | [LHX 10-Q 2022-04-01](https://www.sec.gov/Archives/edgar/data/202058/000020205822000029/0000202058-22-000029-index.htm) | Non-operating income | 106.0 M |
| operating_income | `us-gaap_IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest` | 2 | 2: ALL, OGN | [OGN 10-Q 2023-03-31](https://www.sec.gov/Archives/edgar/data/1821825/000182182523000006/0001821825-23-000006-index.htm) | Income From Operations Before Income Taxes | 235.0 M |
| operating_income | `us-gaap_DebtAndEquitySecuritiesGainLoss` | 1 | 1: EG | [EG 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1095073/000109507322000005/0001095073-22-000005-index.htm) | Non-Operating Income (Expense) | 285.9 M |
| receivables | `us-gaap_IncomeTaxesReceivable` | 19 | 1: DG | [DG 10-K 2022-01-28](https://www.sec.gov/Archives/edgar/data/29534/000155837022003921/0001558370-22-003921-index.htm) | Income taxes receivable | 97.4 M |
| receivables | `us-gaap_IncomeTaxReceivable` | 18 | 1: TSCO | [TSCO 10-K 2021-12-25](https://www.sec.gov/Archives/edgar/data/916365/000091636522000049/0000916365-22-000049-index.htm) | Income taxes receivable | 17.1 M |
| receivables | `us-gaap_FundsHeldForClients` | 17 | 1: ABNB | [ABNB 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1559720/000155972022000006/0001559720-22-000006-index.htm) | Funds receivable and amounts held on behalf of customers | 3,715.5 M |
| receivables | `us-gaap_LongTermAccountsNotesAndLoansReceivableNetNoncurrent` | 13 | 1: CMS | [CMS 10-Q 2023-06-30](https://www.sec.gov/Archives/edgar/data/811156/000081115623000066/0000811156-23-000066-index.htm) | Accounts receivable | 24.0 M |
| receivables | `ph_Nontradeandnotesreceivable` | 9 | 1: PH | [PH 10-K 2022-06-30](https://www.sec.gov/Archives/edgar/data/76334/000007633422000034/0000076334-22-000034-index.htm) | Non-trade and notes receivable | 543.8 M |
| receivables | `us-gaap_OtherReceivablesNetCurrent` | 7 | 1: FIS | [FIS 10-K 2024-12-31](https://www.sec.gov/Archives/edgar/data/1136893/000113689325000014/0001136893-25-000014-index.htm) | Other receivables | 160.0 M |
| receivables | `us-gaap_FinancingReceivableExcludingAccruedInterestAfterAllowanceForCreditLoss` | 5 | 1: AIG | [AIG 10-Q 2023-03-31](https://www.sec.gov/Archives/edgar/data/5272/000000527223000023/0000005272-23-000023-index.htm) | Mortgage and other loans receivable, net of allowance for credit losses of $38,4 | 50,830.0 M |
| redeemable_nci | `us-gaap_LiabilitiesAndStockholdersEquity` | 30 | 3: BRK.B, DASH, TDY | [DASH 10-Q 2022-09-30](https://www.sec.gov/Archives/edgar/data/1792789/000162828022028457/0001628280-22-028457-index.htm) | Total liabilities, redeemable non-controlling interests and stockholders’ equity | 9,552.0 M |
| short_term_debt | `us-gaap_NotesPayable` | 59 | 4: DHI, KIM, PHM, REG | [DHI 10-Q 2021-12-31](https://www.sec.gov/Archives/edgar/data/882184/000088218422000024/0000882184-22-000024-index.htm) | Notes payable | 5,255.3 M |
| short_term_debt | `cprt_OperatingAndFinanceLeaseLiabilityCurrent` | 12 | 1: CPRT | [CPRT 10-Q 2024-10-31](https://www.sec.gov/Archives/edgar/data/900075/000090007524000030/0000900075-24-000030-index.htm) | Current portion of operating and finance lease liabilities | 21.4 M |
| short_term_debt | `us-gaap_OperatingLeaseLiabilityCurrent` | 11 | 2: EXPD, TJX | [EXPD 10-K 2022-12-31](https://www.sec.gov/Archives/edgar/data/746515/000095017023005412/0000950170-23-005412-index.htm) | Current portion of operating lease liabilities | 95.6 M |
| short_term_debt | `us-gaap_SecuredDebt` | 8 | 1: BXP | [BXP 10-Q 2022-06-30](https://www.sec.gov/Archives/edgar/data/1037540/000165642322000032/0001656423-22-000032-index.htm) | Mortgage notes payable, net (amounts related to VIEs of $3,269,948 and $3,267,91 | 3,269.9 M |
| short_term_debt | `us-gaap_LongTermDebtAndCapitalLeaseObligations` | 3 | 1: JKHY | [JKHY 10-Q 2023-09-30](https://www.sec.gov/Archives/edgar/data/779152/000077915223000099/0000779152-23-000099-index.htm) | Debt, net of current maturities | 245.0 M |
| short_term_investments | `us-gaap_MarketableSecuritiesNoncurrent` | 19 | 1: ES | [ES 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/72741/000007274122000015/0000072741-22-000015-index.htm) | Marketable Securities | 460.3 M |
| short_term_investments | `us-gaap_RestrictedCashAndInvestmentsNoncurrent` | 16 | 1: RSG | [RSG 10-Q 2022-09-30](https://www.sec.gov/Archives/edgar/data/1060391/000106039122000027/0001060391-22-000027-index.htm) | Restricted cash and marketable securities | 121.7 M |
| stock_based_compensation | `eme_ExcessTaxBenefitFromShareBasedCompensation` | 4 | 1: EME | [EME 10-K 2022-12-31](https://www.sec.gov/Archives/edgar/data/105634/000010563423000005/0000105634-23-000005-index.htm) | Excess tax benefits from share-based compensation | -1.7 M |

### Small groups not read one by one (REVIEW)

| item | element on the face | filings | companies | example: filing | caption | value |
|---|---|---|---|---|---|---|
| cash | `bwa_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsContinuingOperations` | 2 | 1: BWA | [BWA 10-Q 2024-09-30](https://www.sec.gov/Archives/edgar/data/908255/000090825524000043/0000908255-24-000043-index.htm) | Cash and cash equivalents | 2,000.0 M |
| cash | `cvx_CashandCashEquivalentsExcludingTimeDepositsatCarryingValue` | 2 | 1: CVX | [CVX 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/93410/000009341022000019/0000093410-22-000019-index.htm) | Cash and cash equivalents | 5,640.0 M |
| cash | `us-gaap_CashEquivalentsAtCarryingValue` | 2 | 1: ICE | [ICE 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1571949/000157194922000006/0001571949-22-000006-index.htm) | Cash and cash equivalents | 607.0 M |
| cash | `ivz_CashAndCashEquivalentsExcludingCashAndCashEquivalentsFromConsolidatedInvestmentProducts` | 1 | 1: IVZ | [IVZ 10-K 2022-12-31](https://www.sec.gov/Archives/edgar/data/914208/000091420823000297/0000914208-23-000297-index.htm) | Cash and cash equivalents | 1,234.7 M |
| cash | `lkq_Cashandcashequivalentsatcarryingvalueexcludingrestrictedcash` | 1 | 1: LKQ | [LKQ 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1065696/000106569622000006/0001065696-22-000006-index.htm) | Cash and cash equivalents | 274.1 M |
| cogs | `us-gaap_CostOfServicesCatering` | 2 | 1: ALK | [ALK 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/766421/000076642122000009/0000766421-22-000009-index.htm) | Cost of Services, Catering | 139.0 M |
| cogs | `us-gaap_OperatingCostsAndExpenses` | 2 | 1: FDS | [FDS 10-Q 2021-11-30](https://www.sec.gov/Archives/edgar/data/1013237/000101323722000005/0001013237-22-000005-index.htm) | Cost of services | 207.1 M |
| cogs | `ipg_CostOfServicesGeneralExpenses` | 2 | 1: IPG | [IPG 10-Q 2025-06-30](https://www.sec.gov/Archives/edgar/data/51644/000005164425000051/0000051644-25-000051-index.htm) | Cost of services | 2,067.5 M |
| cogs | `us-gaap_DepreciationAndAmortization` | 2 | 1: TGT | [TGT 10-Q 2022-04-30](https://www.sec.gov/Archives/edgar/data/27419/000002741922000015/0000027419-22-000015-index.htm) | Depreciation and amortization (exclusive of depreciation included in cost of sal | 601.0 M |
| cogs | `us-gaap_GainsLossesOnExtinguishmentOfDebt` | 1 | 1: MRSH | [MRSH 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/62709/000006270922000009/0000062709-22-000009-index.htm) | Cost of extinguishment of debt | 0.0 M |
| cogs | `oxy_PurchasedCommoditiesAndMidstreamCostOfSales` | 1 | 1: OXY | [OXY 10-K 2025-12-31](https://www.sec.gov/Archives/edgar/data/797468/000162828026009059/0001628280-26-009059-index.htm) | Purchased commodities and midstream cost of sales | 176.0 M |
| cogs | `us-gaap_DirectCostsOfLeasedAndRentedPropertyOrEquipment` | 1 | 1: PSA | [PSA 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1393311/000139331122000010/0001393311-22-000010-index.htm) | Self-storage cost of operations | 852.0 M |
| depreciation_amortization | `acn_DepreciationAmortizationAndOther` | 2 | 1: ACN | [ACN 10-K 2025-08-31](https://www.sec.gov/Archives/edgar/data/1467373/000146737325000217/0001467373-25-000217-index.htm) | Depreciation, amortization and other | 2,441.6 M |
| depreciation_amortization | `aon_AmortizationAndImpairmentOfIntangibleAssets` | 2 | 1: AON | [AON 10-K 2024-12-31](https://www.sec.gov/Archives/edgar/data/315293/000162828025006093/0001628280-25-006093-index.htm) | Amortization and impairment of intangible assets | 503.0 M |
| depreciation_amortization | `fe_DepreciationDepletionAmortizationAndImpairments` | 2 | 1: FE | [FE 10-K 2023-12-31](https://www.sec.gov/Archives/edgar/data/1031296/000103129624000008/0001031296-24-000008-index.htm) | Depreciation, amortization and impairments | 1,280.0 M |
| depreciation_amortization | `fi_AmortizationOfAcquisitionRelatedIntangibleAssets` | 2 | 1: FISV | [FISV 10-K 2024-12-31](https://www.sec.gov/Archives/edgar/data/798354/000079835425000047/0000798354-25-000047-index.htm) | Amortization of acquisition-related intangible assets | 1,423.0 M |
| depreciation_amortization | `goog_AmortizationAndImpairmentOfIntangibleAssets` | 2 | 1: GOOG | [GOOG 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1652044/000165204422000019/0001652044-22-000019-index.htm) | Amortization and impairment of intangible assets | 886.0 M |
| depreciation_amortization | `lnc_AmortizationOfDeferredGainOnBusinessSoldThroughReinsurance` | 2 | 1: LNC | [LNC 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/59558/000005955822000008/0000059558-22-000008-index.htm) | Amortization of deferred gain on business sold through reinsurance | -46.0 M |
| depreciation_amortization | `mtch_ImpairmentAndAmortizationOfIntangibleAssets` | 2 | 1: MTCH | [MTCH 10-K 2024-12-31](https://www.sec.gov/Archives/edgar/data/891103/000089110325000027/0000891103-25-000027-index.htm) | Impairments and amortization of intangibles | 74.2 M |
| depreciation_amortization | `tfc_AmortizationOfIntangiblesFromContinuingAndDiscontinuedOperations` | 2 | 1: TFC | [TFC 10-K 2024-12-31](https://www.sec.gov/Archives/edgar/data/92230/000009223025000020/0000092230-25-000020-index.htm) | Amortization of intangibles | 366.0 M |
| depreciation_amortization | `us-gaap_CapitalizedComputerSoftwareAmortization1` | 2 | 1: TXN | [TXN 10-K 2024-12-31](https://www.sec.gov/Archives/edgar/data/97476/000009747625000007/0000097476-25-000007-index.htm) | Amortization of capitalized software | 72.0 M |
| depreciation_amortization | `ctva_BusinessCombinationsInventoryFairValueAdjustment` | 1 | 1: CTVA | [CTVA 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1755672/000175567222000005/0001755672-22-000005-index.htm) | Amortization of inventory step-up | 0.0 M |
| depreciation_amortization | `ctxs_AmortizationandImpairmentofIntangibleAssets` | 1 | 1: CTXS | [CTXS 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/877890/000087789022000019/0000877890-22-000019-index.htm) | Amortization and impairment of intangible assets | 157.7 M |
| depreciation_amortization | `isrg_AmortizationOfIntangibleAssetsContractAcquisitionAndOtherAssets` | 1 | 1: ISRG | [ISRG 10-K 2025-12-31](https://www.sec.gov/Archives/edgar/data/1035267/000103526726000010/0001035267-26-000010-index.htm) | Amortization of intangible and other assets | 62.4 M |
| depreciation_amortization | `pm_DepreciationAmortizationAndImpairment` | 1 | 1: PM | [PM 10-K 2022-12-31](https://www.sec.gov/Archives/edgar/data/1413329/000141332923000025/0001413329-23-000025-index.htm) | Depreciation, amortization and impairment of intangibles | 1,189.0 M |
| depreciation_amortization | `pru_DepreciationDepletionAndAmortizationNetOfAdjustmentForAccountingAdoption` | 1 | 1: PRU | [PRU 10-K 2023-12-31](https://www.sec.gov/Archives/edgar/data/1137774/000113777424000045/0001137774-24-000045-index.htm) | Depreciation and amortization | -70.0 M |
| depreciation_amortization | `trow_DepreciationAmortizationAndImpairmentOfPropertyEquipmentAndSoftware` | 1 | 1: TROW | [TROW 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1113169/000111316922000005/0001113169-22-000005-index.htm) | Depreciation, amortization and impairment of property, equipment and software | 204.8 M |
| income_tax | `us-gaap_IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest` | 1 | 1: SW | [SW 10-Q 2024-06-30](https://www.sec.gov/Archives/edgar/data/2005951/000141057824001252/0001410578-24-001252-index.htm) | Loss before income taxes | -0.0 M |
| inventory | `psky_FilmCapitalizedCostCurrent` | 2 | 1: SKYD | [SKYD 10-Q 2026-03-31](https://www.sec.gov/Archives/edgar/data/2041610/000204161026000026/0002041610-26-000026-index.htm) | Programming and other inventory | 1,000.0 M |
| inventory | `t_NoncurrentInventoriesAndFilmCosts` | 2 | 1: T | [T 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/732717/000073271722000015/0000732717-22-000015-index.htm) | Noncurrent Inventories and Theatrical Film and Television Production Costs | 18,983.0 M |
| inventory | `dal_AirlineRelatedInventoryAircraftPartsAndSuppliesNet` | 1 | 1: DAL | [DAL 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/27904/000002790422000003/0000027904-22-000003-index.htm) | Expendable parts and supplies inventories, net of an allowance for obsolescence  | 404.0 M |
| inventory | `us-gaap_EnergyRelatedInventory` | 1 | 1: ETR | [ETR 10-Q 2026-06-30](https://www.sec.gov/Archives/edgar/data/65984/000006598426000283/0000065984-26-000283-index.htm) | Energy Related Inventory | 137.7 M |
| long_term_debt | `us-gaap_ConvertibleDebt` | 2 | 1: BXP | [BXP 10-Q 2025-09-30](https://www.sec.gov/Archives/edgar/data/1037540/000103754025000013/0001037540-25-000013-index.htm) | Unsecured exchangeable senior notes, net | 975.1 M |
| long_term_debt | `dish_LongTermDebtAndFinanceLeaseObligationsNetOfCurrentPortion` | 1 | 1: DISH | [DISH 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1001082/000155837022001816/0001558370-22-001816-index.htm) | Long-term debt and finance lease obligations, net of current portion (Note 11) | 19,355.2 M |
| long_term_debt | `us-gaap_LoansPayableCurrent` | 1 | 1: TYL | [TYL 10-Q 2026-03-31](https://www.sec.gov/Archives/edgar/data/860731/000086073126000032/0000860731-26-000032-index.htm) | Current portion of convertible senior notes due 2026, net | 0.0 M |
| minority_interest | `us-gaap_RedeemableNoncontrollingInterestEquityCarryingAmount` | 1 | 1: INFO | [INFO 10-K 2021-11-30](https://www.sec.gov/Archives/edgar/data/1598014/000159801422000011/0001598014-22-000011-index.htm) | Redeemable noncontrolling interests | 13.0 M |
| pretax_income | `cma_CapitalMarketsIncome` | 1 | 1: CMA | [CMA 10-K 2023-12-31](https://www.sec.gov/Archives/edgar/data/28412/000002841224000185/0000028412-24-000185-index.htm) | Foreign Currency Transaction Gain, before Tax | 147.0 M |
| pretax_income | `dvn_IncomeLossFromContinuingOperationsBeforeNoncontrollingInterest` | 1 | 1: DVN | [DVN 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1090012/000156459022005321/0001564590-22-005321-index.htm) | Earnings (loss) from continuing operations before income taxes | 2,898.0 M |
| pretax_income | `well_IncomeLossFromContinuingOperationsPreTaxBookBeforeEquityMethodInvestmentsIncomeTaxesNoncontrollingInterest` | 1 | 1: WELL | [WELL 10-K 2025-12-31](https://www.sec.gov/Archives/edgar/data/766704/000076670426000010/0000766704-26-000010-index.htm) | Income (loss) from continuing operations before income taxes and other items | -480.0 M |
| receivables | `us-gaap_NotesAndLoansReceivableNetCurrent` | 2 | 1: DELL | [DELL 10-Q 2023-11-03](https://www.sec.gov/Archives/edgar/data/1571996/000157199623000046/0001571996-23-000046-index.htm) | Short-term financing receivables, net of allowance of $73 and $142 (Note 4) | 4,540.0 M |
| receivables | `antm_SelfFundedReceivablesNet` | 2 | 1: ELV | [ELV 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1156039/000115603922000009/0001156039-22-000009-index.htm) | Self-funded receivables | 4,010.0 M |
| receivables | `bro_PremiumsCommissionsAndFeesReceivable` | 1 | 1: BRO | [BRO 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/79282/000095017022001654/0000950170-22-001654-index.htm) | Premiums, commissions and fees receivable | 1,216.2 M |
| receivables | `us-gaap_AccruedInvestmentIncomeReceivable` | 1 | 1: HIG | [HIG 10-Q 2026-06-30](https://www.sec.gov/Archives/edgar/data/874766/000087476626000060/0000874766-26-000060-index.htm) | Accrued Investment Income Receivable | 487.0 M |
| receivables | `us-gaap_NotesAndLoansReceivableNetNoncurrent` | 1 | 1: TPL | [TPL 10-Q 2026-03-31](https://www.sec.gov/Archives/edgar/data/1811074/000181107426000035/0001811074-26-000035-index.htm) | Financing receivable, net | 20.0 M |
| short_term_debt | `us-gaap_PensionAndOtherPostretirementAndPostemploymentBenefitPlansLiabilitiesCurrent` | 2 | 1: HII | [HII 10-Q 2022-03-31](https://www.sec.gov/Archives/edgar/data/1501585/000150158522000013/0001501585-22-000013-index.htm) | Current portion of postretirement plan liabilities | 137.0 M |
| short_term_debt | `lkq_LongTermDebtandCapitalLeaseObligationsExcludingCurrentPortionNet` | 2 | 1: LKQ | [LKQ 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1065696/000106569622000006/0001065696-22-000006-index.htm) | Long-term obligations, excluding current portion | 2,777.2 M |
| short_term_debt | `ttwo_SoftwareDevelopmentCostsAndLicensesNoncurrent` | 2 | 1: TTWO | [TTWO 10-K 2022-03-31](https://www.sec.gov/Archives/edgar/data/946581/000162828022014580/0001628280-22-014580-index.htm) | Software development costs and licenses, net of current portion | 755.9 M |
| short_term_debt | `us-gaap_ContractWithCustomerLiabilityCurrent` | 1 | 1: AXON | [AXON 10-Q 2024-09-30](https://www.sec.gov/Archives/edgar/data/1069183/000106918324000061/0001069183-24-000061-index.htm) | Current portion of deferred revenue | 505.0 M |
| short_term_debt | `us-gaap_AssetRetirementObligationsNoncurrent` | 1 | 1: EXE | [EXE 10-K 2022-12-31](https://www.sec.gov/Archives/edgar/data/895126/000089512623000022/0000895126-23-000022-index.htm) | Asset retirement obligations, net of current portion | 323.0 M |
| short_term_debt | `us-gaap_LongTermDebtNoncurrent` | 1 | 1: JKHY | [JKHY 10-Q 2025-09-30](https://www.sec.gov/Archives/edgar/data/779152/000077915225000092/0000779152-25-000092-index.htm) | Debt, net of current maturities | 20.0 M |
| short_term_investments | `us-gaap_MarketableSecurities` | 2 | 1: DD | [DD 10-Q 2023-06-30](https://www.sec.gov/Archives/edgar/data/1666700/000166670023000082/0001666700-23-000082-index.htm) | Marketable securities | 0.0 M |
| short_term_investments | `us-gaap_DebtSecuritiesHeldToMaturityFairValueCurrent` | 2 | 1: HAS | [HAS 10-Q 2025-06-29](https://www.sec.gov/Archives/edgar/data/46080/000004608025000128/0000046080-25-000128-index.htm) | Short-term investments | 0.0 M |
| short_term_investments | `us-gaap_DebtSecuritiesCurrent` | 1 | 1: ADM | [ADM 10-Q 2025-09-30](https://www.sec.gov/Archives/edgar/data/7084/000000708425000052/0000007084-25-000052-index.htm) | Short-term marketable securities | 0.0 M |
| stock_based_compensation | `be_ShareBasedPaymentArrangementIncreaseDecreaseForCustomerContract` | 1 | 1: BE | [BE 10-K 2025-12-31](https://www.sec.gov/Archives/edgar/data/1664703/000162828026006516/0001628280-26-006516-index.htm) | Share-based consideration payable to customer’s customer (Note 3) | 15.9 M |
| stock_based_compensation | `bkng_ShareBasedCompensationAndOtherStockBasedExpense` | 1 | 1: BKNG | [BKNG 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/1075531/000107553122000008/0001075531-22-000008-index.htm) | Stock-based compensation expense and other stock-based payments | 376.0 M |
| stock_based_compensation | `us-gaap_PaymentsRelatedToTaxWithholdingForShareBasedCompensation` | 1 | 1: DRE | [DRE 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/783280/000078328022000010/0000783280-22-000010-index.htm) | Tax payments on stock-based compensation awards | -5.1 M |
| stock_based_compensation | `eme_ExcessTaxBenefitFromShareBasedCompensationOperatingActivities1` | 1 | 1: EME | [EME 10-K 2021-12-31](https://www.sec.gov/Archives/edgar/data/105634/000010563422000006/0000105634-22-000006-index.htm) | Excess tax benefits from share-based compensation | -0.8 M |

## 4. Questions only you can decide (the RULE groups and the two big classes)

| # | question | scope | my recommendation |
|---|---|---|---|
| Q1 | Interest expense shown as **"Interest expense, net"** (net of interest income) or under a *net* element: use it as interest expense, or keep empty? | 985 gaps, 86 companies (AAP, ABNB, CAG, CBRE, CDW, CHTR, DAL, EMR …) | use it, flagged `interest_net` — it is what the filer presents as its interest line |
| Q2a | D&A lines that include **impairment / write-offs / "other"** ("Amortization and impairment of intangible assets", TSLA "… and impairment", MSFT "… and other"): accept, or keep empty (MD-01; L-4 today names only MRVL)? | 118 gaps, 31 companies (AON, CINF, ECL, GILD, GRMN, HON, HUM, INTU, ISRG, ITW …) | accept only the years the filing's own impairment fact is 0; otherwise empty |
| Q2b | D&A shown as **one part only** on the line found (depreciation alone — CSX, UNP; amortization alone — the other part on another line or absent): use the line when it is the only D&A on the face, the sum when both parts are lines? | 59 gaps, 15 companies (ACGL, AVGO, BBWI, BRO, BX, CIEN, CSX, GOOG, MTB, MTD …) | yes, as the debt lines are summed |
| Q3 | Inventory shown as **several lines without a total** (CNP gas + materials, DTE, XOM, ETR, D, LNT; KR and ABMD FIFO inventory − LIFO reserve): use the sum of the face lines, as for debt? | 89 gaps, 8 companies | yes, the sum of the face's inventory lines |
| Q4 | MKC "Total inventory" (`InventoryGross`, no reserve line on the face): accept (L-2)? | MKC, 15 filings | yes — with no reserve line, gross is the total |
| Q5 | A **component** line only (one segment's capex: NEE FPL, ED non-utility; part of cost of revenue: T, CPRT, INVH, XEL; billed or self-funded receivables: FIX, ELV, FIS, TPL; F's interest without Ford Credit; ACGL pretax before operating affiliates): keep empty? | 156 gaps, 12 companies | keep empty — a part is not the total |
| Q6 | Cash captions that include restricted cash (HAS, ERIE, SW — L-5): keep the rule? | 3 companies | keep |
| Q7 | Values the SEC holds that are **only in the notes** (not on the face): keep refusing (face first, today's rule), or accept the filing's own fact? | 2,710 gaps (interest expense 1,066) | your call; accepting fills interest coverage for many companies |
| Q9 | ROIC on capital **net of cash** is extreme where cash almost equals equity (PLTR FY2023: NOPAT 94.8 M / capital 8.76 M = 1,082 %; DECK ~150 %, EXPD ~120 %; already 159 such values in 35 companies — OTIS, NTAP, ANET, NVDA, AAPL …): keep the formula's value, or mark a capital base below e.g. 5 % of total assets as not meaningful (empty, flagged)? | 159 values today + 15 from #9 | mark it not meaningful, flagged — a ratio on a near-zero base is not a return |
| Q8 | 10-Q quarter cash flows filed **year-to-date only**: derive the quarter as YTD − the previous 10-Q's YTD? | 8,193 gaps (mostly quarterly FCF growth) | yes, flagged `quarter_derived` |

## 5. Declared limitations that are about data, not rules

| # | what | data-type because | what you can check |
|---|---|---|---|
| L-7 | 53 first filings without a market cap | the SEC has no non-dimensional cover count before the first stored filing (multi-class covers are tagged by class) | the previous 10-Q's cover page lists the shares per class |
| L-17 | PCG / OKE inventory understated | the filer tags one component as its total | PCG FY2025: gas stored 75 M + materials and supplies 745 M |
| L-20 | short-term debt alone refused (IBKR, ULTA, TPL, FFIV, WSM, RHI, SNDK, TYL; DASH FY2021, TER FY2025 ×4, GEV FY2024–25) | the rule assumes a long-term line exists that was not read | whether the balance sheet has any long-term debt line |
| ~~L-21~~ | debt-free companies empty, not 0 | resolved in FIXES #9 after your check: 148 filings, 21 companies, debt 0 | — |
| L-23 | LEN 2026-Q3 total assets | the filing tags total assets by segment only | the balance sheet's total assets |
| L-6 | HWM SBC (extension line 41 M vs the SEC's 40 M) | which of two values is the face's | the cash-flow statement's SBC line |

Every other declared limitation (L-1, L-2, L-4, L-5, L-8, L-9, L-13–L-16, L-18, L-19, L-22, L-24, L-25) is decided by a
rule or is a recording difference, not missing data.
