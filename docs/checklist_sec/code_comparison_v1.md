# Code comparison v1 — checklist v1.2.2 against the code (Phase 1, static)

Claude, 2026-10-08. Phase 1 of `review_plan_v1.md`. **Static reading only:** no database was opened and nothing
was measured. Counts and "how often" questions go to Phase 2 (`T-147`'s scripts).

**Reviewed 2026-10-08.** The resolutions are in `phase3_decisions.md`. They confirm N4 by the B6 definitions, and add a correction to N18: T-148(d) also contradicts MET-07, because `DebtCurrent` includes leases.

**What was read:**
- This repo at `origin/master` `5280e4b` (identical to the working tree).
- The gateway `portfolio-data-mining` at `origin/master` `e5482bc`: `src/sec_edgar/agent.py`.
- `edgartools` 5.44.1 (the gateway's pinned version), only where the gateway's behaviour depends on it.

**Data path read:**
- gateway `get_financials` / `get_filing_by_year`
- `fundamental_agent`: `edgar_client`, `pipeline`, `fiscal`, `statements`, `metrics/*`, `db`, `quality`, `agents`
- `kg_schema`: `availability`, `market_cap`, `queries.members_asof`
- `cycle`: `data`, `orchestrator`, `scores/normalize`, `scores/valorization`, `rules/builtin`
- `quant` reads SEC data only through `kg_schema.market_cap.market_caps_as_of`.

## Legend

**Status:**

| Status | Meaning |
|---|---|
| ✓ | The code enforces the rule. |
| partial | It enforces part of the rule. |
| ✗ | The code contradicts the rule, or doesn't implement it. |
| n/a | Nothing in the code is concerned. |
| L | Declared not executable (section L). |

**Proposed class** (Phase 3 decides; these are proposals):

| Class | Meaning |
|---|---|
| **BUG** | The code contradicts a sourced rule and no project decision covers it. |
| **DECIDE** | A defensible deviation that needs your decision. |
| **NOT-EXEC** | The data can't run the rule. |
| **CHECKLIST** | The code shows a case the checklist doesn't handle; needs a reviewer round. |

"Test" names the test that pins the behaviour today, or "none". A test that pins the *wrong* behaviour is marked ⚠.

---

## 1. Summary

| Layer | Rules | ✓ | partial | ✗ | n/a or L |
|---|---|---|---|---|---|
| ID | 22 | 4 | 6 | 11 | 1 |
| PER | 13 | 5 | 3 | 5 | 0 |
| CON | 13 | 0 | 2 | 7 | 4 |
| DQ | 2 | 0 | 0 | 2 | 0 |
| MET | 10 | 0 | 1 | 8 | 1 |
| MKT | 4 | 0 | 4 | 0 | 0 |
| APP | 14 | 1 | 2 | 11 | 0 |
| **All** | **78** | **10** | **18** | **44** | **6** |

*Counted by script from the rule tables below.*

**Partial in MKT means the shared as-of reader passes, but the per-filing stored valuation metrics fail.**

**The main pattern:** the parts built recently are sound: point in time (`available_at`), the as-of market-cap
reader, the gateway's non-dimensional validation and fiscal labelling. The gaps cluster in:
1. Line-item selection: document order, component maps without sources, signs. This is `T-148`'s area.
2. Metric definitions: ROA, ROE and ROIC timing, NOPAT tax, negative denominators.
3. Company-type applicability: nothing identifies banks, insurers or REITs.
4. Missing consistency identities: no CON rule runs.

## 2. New findings (not in the checklist's "Input for the code comparison")

Ordered by expected impact. Each one is static evidence; Phase 2 measures it.

| # | Finding | Rule | Where | Class |
|---|---|---|---|---|
| N1 | **A 10-K/A replaces the original 10-K.** The gateway lists amendments with the form (`company.get_filings(form=…)`; edgartools defaults to `amendments=True`, and its filter adds `"10-K/A"` to `"10-K"`). `_select_filings` then keeps the **most recent** 10-K filed in the year. A Part III-only 10-K/A has no statements, so that year's 10-K is recorded as "unusable" and lost. One with statements replaces the original values, and dates them from the amendment. ⚠ `test_pipeline_multi_filing.py:91` pins this behaviour. Separately, a `--fresh` run re-ingests an amendment filed the next calendar year under the same label; `upsert_filing`'s `ON CONFLICT … DO UPDATE` then overwrites the original. | PER-02, L-04 | `pipeline.py` `_select_filings`; gateway `agent.py` `get_filing_by_year` | BUG |
| N2 | **`earnings_yield` is never computed.** The orchestrator reads `m.get("profitability.net_income") or m.get("income_statement.net_income")`, but no metric module stores a metric named `net_income` (it is only an audit input). The VALORIZATION "value" factor runs on two of its three metrics in every real cycle. The tests inject `earnings_yield` directly (`test_cycle.py:89`), so they don't see it. Once fixed, it would still break MET-05: it is continuous and keeps negative earnings, and on a 10-Q it would use the quarter's net income, not a trailing year. | MET-05, MET-00 | `cycle/orchestrator.py` `_valorization` | BUG |
| N3 | **Operating cash flow can be the continuing-operations line.** `operating_cash_flow` lists `NetCashProvidedByUsedInOperatingActivities` and `…ContinuingOperations` in **document order**. The continuing line usually comes first on the statement, so it wins whenever both are filed. | CON-08, ID-02b | `statements.py` REGISTRY `operating_cash_flow` | BUG |
| N4 | **Debt double counting.** `long_term_debt` can resolve to `LongTermDebt` or `…IncludingCurrentMaturities` (both include current maturities), and `short_term_debt` adds `LongTermDebtCurrent` again. | MET-07, ID-02b | REGISTRY `long_term_debt`, `short_term_debt`; `leverage._total_debt` | BUG |
| N5 | **Cash double counting.** `cash` can resolve to `CashCashEquivalentsAndShortTermInvestments`, and `liquidity.cash_ratio` then adds `short_term_investments`. | ID-02a/02b | REGISTRY `cash`; `liquidity.compute` | BUG |
| N6 | **Balance-sheet values from another date.** When no instant column matches the period end exactly, `_instant_for` takes the **nearest earlier** instant. That can be the prior year end, read as this period's balance. | ID-05 | `statements.py` `_instant_for` | BUG |
| N7 | **Missing components treated as 0.** `sum_present` adds whatever is present: debt = LT alone or ST alone (ROIC, EV); invested capital without cash. EV fills missing debt and cash with 0 (D10). | MET-00, ID-02b | `roic.py`, `valuation.py`, `leverage._total_debt` | BUG (but see Q1) |
| N8 | **Financial firms are vetoed on metrics that don't apply to them.** `NEGATIVE_FCF` (HARD) reads the FCF margin, which is not applicable to financial firms. `LEVERAGE_EXTREME` (HARD, D/E > 3) reads D/E, which is not applicable to them either, and a bank's balance sheet typically exceeds 3×. | APP-02, APP-02b | `cycle/rules/builtin.py` | BUG |
| N9 | **Net cash is discarded with negative EBITDA.** `cycle` treats any negative `net_debt_to_ebitda` as unresolved (T-116). That also drops net-cash companies with positive EBITDA, which MET-09 keeps; MET-09 drops only a denominator < 0. The stored metric keeps negative-EBITDA values unflagged. | MET-09 | `leverage.compute`; `builtin._LeverageRule` | BUG |
| N10 | **`effective_tax_rate` shows 21% when pre-tax income ≤ 0** (the metric, not only NOPAT's input). L-05 says it is empty. | MET-03, L-05 | `roic.effective_tax_rate` | BUG |
| N11 | **Duplicate facts are passed through unflagged.** When more than one non-dimensional fact matches a concept and period (`len(filed_values) > 1`), the gateway keeps the rendered value with no record. Agreeing duplicates take the same path. | ID-21 | gateway `reconcile_with_filed_facts` | BUG (gateway repo) |
| N12 | **Dead concepts.** Four registry concepts are declared in no taxonomy release 2020–2026: `CostOfGoodsSold`, `CostOfServices`, `ShareBasedCompensationExpense`, `AvailableForSaleSecuritiesCurrent`. | ID-20 | REGISTRY | BUG (WARN) |
| N13 | **Parent earnings fall back to `NetIncomeLossAvailableToCommonStockholdersBasic`** (T-096, WAT). That figure is after preferred dividends, so it is not parent net income under B6's definition. | ID-03 | REGISTRY `net_income` | DECIDE |
| N14 | **D&A can be `Depreciation` alone**, which is a component of D&A: EBITDA is understated. | ID-02b | REGISTRY `depreciation_amortization` | BUG |
| N15 | **ROIC is used with invested capital ≤ 0**, and ROE with negative equity reaches the stored metric (the gate quarantines it downstream). | APP-07 | `roic.compute`, `profitability.compute` | BUG |
| N16 | **The FUNDAMENTAL fallback score starts at 50** and skips missing metrics. A filing with no metrics scores a neutral 50. | MET-00 | `agents._fallback_assessment` | BUG (T-073 rewrites the rubric) |
| N17 | **Winsorizing with few assets.** `k = max(1, int(n × frac))`: with n = 20 (the pilot) any fraction trims 1 value per tail, which is 5%, not 0.5%. | DQ-02 | `normalize._winsorize` | DECIDE (only the pilot is affected) |
| N18 | **`T-148`'s text contradicts ID-11:** T-148(a) says "keep the `abs()` guards". ID-11 says no `abs()` repair: a wrong sign is rejected. T-148(c) (income tax = current + deferred) and (d) (the short-term debt map) are component maps, so ID-02b needs a source for each. | ID-11, ID-02b | `TASKS.md` T-148 | CHECKLIST (task text) |

## 3. Rule by rule

### Layer ID

| Rule | Status | Where | Test | Gap | Class → task |
|---|---|---|---|---|---|
| ID-01 | partial | `statements._matches` (standard, label), `_fallback_match` (caption, custom concept) | none | Extension, `standard_concept` and label matches are used with no flag. | BUG → T-148(b) + a flag |
| ID-02a | ✗ | `statements._first_component_match` | none | Document order over concepts ∪ standard ∪ label decides, not the concept order (D2). | BUG → T-148(b) |
| ID-02b | ✗ | revenue `sum_components`; `rebuild_total` / `_label_total_correction` (T-117/T-140); `gross = revenue − cogs`; debt LT + ST; `income_tax` standard (D3); `short_term_debt` first match (D4); `Depreciation`; OCF continuing (N3) | `test_revenue_rebuild.py`, `test_statements.py` (pin the maps) | No map cites a source. The revenue rebuild is a derivation (subtract the rows between) with no source identity. See Q2. | BUG / DECIDE (Q2) → T-148 |
| ID-03 | ✗ | REGISTRY `net_income` | none | `ProfitLoss` can win by document order (D2). No `ProfitLoss − NCI` path. The fallback to available-to-common (N13). | BUG → T-148(b); N13 DECIDE |
| ID-04 | partial | concept choice only; the payload carries no unit | none | No unit check anywhere. The gateway passes no unit field. | NOT-EXEC today; Phase 2 measures mismatches via companyfacts |
| ID-05 | partial | gateway `_instant_fact_values` / `_duration_fact_values` ✓; `statements._instant_for` ✗ | none | The nearest-earlier instant fallback (N6). | BUG → T-148 |
| ID-06 | ✓ | gateway column tags (edgartools buckets, Q = 80–100 days); `pipeline._ytd_columns` | `test_ttm_t105.py` | Year-to-date values are used only in the TTM identity. | — |
| ID-07 | ✗ | `pipeline._resolve_target` (latest column) | none | Never compared with `dei:DocumentPeriodEndDate` or `periodOfReport`; the gateway returns neither. | BUG → measure in Phase 2 (submissions API); fix needs a gateway field |
| ID-08 | ✓ | `fiscal.label_year`, `quarter_label` (from the period end and the fiscal year end) | `test_quarter_labels.py` | The optional cross-check against the focus tags isn't done. | — |
| ID-09 | partial | gateway T-042 (`by_dimension(None)`); `statements._rows_for` skips dimensional rows ✓; `_WHOLE_ENTITY_AXES` | `test_statements.py:464,490` | **Answer to the "Input" question:** `ConsolidationItemsAxis` is treated as a whole-entity axis, but only inside `_total_is_plausible`. A total equal to one of its members is accepted without a flag. | DECIDE (flag it, per ID-16) → T-148 |
| ID-10 | ✓ | non-dimensional only (gateway + resolver); cover shares skip `LegalEntityAxis` | gateway `test_agent.py` (NEE) | — | — |
| ID-11 | ✗ | `abs()` in `cashflow._free_cash_flow`, `capex_intensity`, `valuation.fcf`/SBC/`_fcf_to_firm`, `leverage.interest_coverage` | none | The displayed sign is used, then forced positive. | BUG → T-148(a), but see N18 |
| ID-12 | ✗ | the gateway passes `preferred_sign`; `iter_facts` drops it | none | D1. | BUG → T-148(a) |
| ID-13 | ✗ | — | — | The DQC_0015 list isn't applied. | BUG → after T-147 prevalence (T-149 candidate) |
| ID-14 | ✗ | — | — | DQC_0013. | as ID-13 |
| ID-15 | ✗ | — | — | DQC_0014. | as ID-13 |
| ID-16 | partial | `_total_is_plausible` (rejects a slice only when a component is larger; revenue only) | `test_statements.py` | No flag; other items not covered. | BUG (WARN) → with ID-09 |
| ID-17 | ✓ | `decimals` is never read | n/a | — | — |
| ID-18 | ✗ | `db.detect_share_scale_factors` + `valuation._share_count` multiply by the factor | `test_share_scale.py` ⚠ | Corrects, where the rule says quarantine. The cover-count path is never corrected; only the CSO and diluted fallbacks are, and MKT-04 removes those. | BUG → with MKT-04; Phase 2: does A-03 catch MCD without the correction? |
| ID-19 | L | — | — | L-08. | NOT-EXEC |
| ID-20 | ✗ | REGISTRY | none | N12: four dead concepts. Nothing checks the lists against the taxonomy files. | BUG (WARN) → T-148 |
| ID-21 | partial | gateway (ambiguous → skipped); resolver first match | none | N11. | BUG → gateway repo PR |

### Layer PER

| Rule | Status | Where | Test | Gap | Class → task |
|---|---|---|---|---|---|
| PER-01 | ✓ | `db.upsert_filing` (`available_from(filing_date)`); `kg_schema/availability` triggers; `cycle/data.py` readers | `test_available_at.py`, `test_point_in_time_readers.py` | Facts and cover shares carry the date only through their filing (accepted). | — |
| PER-02 | ✗ | `pipeline._select_filings`; `upsert_filing` `ON CONFLICT DO UPDATE` | ⚠ `test_pipeline_multi_filing.py:91` | N1. | BUG → **new task (T-149)**; not in T-148's scope |
| PER-03 | ✓ | no `frames` call in `src/` | n/a | — | — |
| PER-04 | ✓ | `growth` uses `prior_of` within the payload; `cagr.compute` uses the 10-K's own FY columns (2 years) | `test_metrics.py` | No flag when a CAGR end is ≤ 0 (MET-04). | — |
| PER-05 | partial | `db._quarter_flow_ending`: Q4 = FY − the three recorded quarters | `test_ttm.py` | Not FY − the 9-month YTD of the Q3 10-Q. They are equal only if CON-06 holds. Used only on the four-quarter TTM path. | DECIDE (minor) |
| PER-06 | partial | `db.ttm_detail` (identity ✓, four quarters, ×4); `agents._flag_annualization` | `test_ttm_t105.py`, `test_cashflow_ttm.py` | ×4 is flagged only in `inputs_json`. ROA, ROE, turnover, EBITDA, ROIC and the yields still use it, and no consumer reads the flag. | BUG (WARN) |
| PER-08 | ✗ | `YEAR_TOLERANCE_DAYS = 20`; `db._PERIOD_TOLERANCE_DAYS = 20`; `fiscal._TOLERANCE_DAYS = 30`; gateway buckets Q 80–100, YTD 175–285 (one bucket for 6 and 9 months), FY ≥ 351 (no upper bound); `prior_of` for FY takes the previous FY column with no date check | none for ranges | No length check against [364,371] / [89,98] / [181,189] / [273,280]. No month-end rounding. | BUG → T-148 or T-149; Phase 2 counts the disagreements |
| PER-09 | ✓ | 10-KT/10-QT are never requested (the form filter adds only `/A`) | none | Transition periods are dropped, so the series has a gap (FERG). | — (Phase 2 counts fiscal-year changes) |
| PER-10 | ✓ | `kg_schema.queries.members_asof` (stints); `cycle/data` universe | `test_kg_queries.py` | — | — |
| PER-11 | partial | `pipeline._load_members(analysis_date)` | none | A run ingests only the members on its own analysis date. Companies that left earlier are fetched only if a run is made at an earlier date. Fetched by ticker (APP-10a). | Phase 2: replay-universe assets with no filings |
| PER-12 | ✗ | — | — | No adoption flag. | NOT-EXEC in window? ASC 842 and CECL predate 2022 for large filers; Phase 2 confirms |
| PER-13 | ✗ | — | — | as PER-12 | as PER-12 |
| PER-14 | ✗ | — | — | as PER-12 | as PER-12 |

### Layer CON and DQ

| Rule | Status | Where | Gap | Class → task |
|---|---|---|---|---|
| CON-01 | ✗ | — | No identity is checked. The facts are stored (`financial_facts` keeps every non-dimensional row), so it is executable. | BUG → T-149 (identity gates with DQ-01) |
| CON-02 | ✗ | — | as CON-01 | as CON-01 |
| CON-03 | ✗ | — | as CON-01 | as CON-01 |
| CON-04 | ✗ | — | as CON-01 | as CON-01 |
| CON-05 | n/a | — | INFO, not consumed. | — |
| CON-06 | partial | `quality.record_ttm_crosscheck` (identity vs four quarters, 1% relative, SOFT) | Tolerance isn't DQ-01's; only where both TTM paths exist. | BUG (align to DQ-01) |
| CON-07 | ✗ | — | as CON-01 | as CON-01 |
| CON-08 | ✗ | REGISTRY `operating_cash_flow` | N3. | BUG → T-148 |
| CON-09 | L | — | L-07. | NOT-EXEC |
| CON-10 | L | — | L-08. | NOT-EXEC |
| CON-11 | partial | `total_concepts` first ✓; `sum_components` and the rebuild when no total is filed | A component sum stands in for the total (Q2). | DECIDE (Q2) |
| CON-12 | n/a | — | INFO. | — |
| CON-19 | ✗ | — | as CON-01; it would guard ID-03. | as CON-01 |
| DQ-01 | ✗ | — | No identity tolerance anywhere. The gateway's 1.0 absolute match is a float tolerance, not DQ-01. | BUG → T-149 |
| DQ-02 | ✗ | `normalize.cross_sectional_z(winsor=0.02)` | The 2% applies to the composite raw scores. VALORIZATION's ratios are rank-transformed (`rank_pct`), which is immune to winsorizing, so the ratios already meet the intent. Change to 0.5% (decided); N17. | BUG (one constant) → T-141 or T-071 |

### Layer MET

| Rule | Status | Where | Gap | Class → task |
|---|---|---|---|---|
| MET-00 | ✗ | `valorization.compute` (`raw = 50` when every factor is missing); `valuation._enterprise_value` (D10); `sum_present`; `rank_pct` (fewer than 2 known values → 0.5); `agents._fallback_assessment` (N16) | Neutral and zero imputation. | BUG → T-071 (VALORIZATION), T-148 (EV, debt), T-073 (fallback) |
| MET-01 | ✗ | `profitability`: ROA = TTM net income / ending assets | Formula and timing both differ. Financial firms aren't excluded. | BUG → new metric definition (T-076 adds a profitability skill, but the formula sits in `metrics/`) |
| MET-02 | ✗ | `profitability` ROE, `roic` use ending capital | `prior_key` is available but unused for balances. On a 10-Q, "the end of the prior year" needs the 10-K balance. | BUG |
| MET-03 | ✗ | `roic.effective_tax_rate` (clamped [0, 0.5], 21% default); NOPAT on a loss is reduced by 21%; N10 | 21% marginal; tax 0 on a loss; the metric empty when pre-tax ≤ 0. | BUG |
| MET-04 | ✗ (growth) / ✓ (CAGR) | `growth._yoy` divides by \|prior\| (D9); `cagr._cagr` → None | No flag in either. | BUG → T-076 (already scoped: audit F8) |
| MET-05 | ✗ | N2 | Never computed; no dummy. | BUG → T-071 |
| MET-06 | n/a | no multiple is computed | — | — |
| MET-07 | partial | REGISTRY `long_term_debt`: combined lease elements in document order, no flag; N4 | Operating leases excluded ✓. | BUG → T-148(d) + a flag |
| MET-08 | ✗ | REGISTRY `capital_expenditure`. Tier 3 `PaymentsToAcquireOilAndGasProperty` (acquisitions, contrary to (a)). The combined O&G element is unflagged. (c) isn't implemented: only the first tier is taken. `PaymentsToAcquireProductiveAssets` wins by document order, unflagged. `PaymentsForCapitalImprovements` applies to every company. The caption fallback (custom concept). The net element is unflagged. | Every point of (a)–(c). Phase 2 measurements as listed in the checklist. | BUG → T-148 (scope from T-147) |
| MET-09 | ✗ | `cashflow` (`fcf_conversion` with net income ≤ 0 kept); `leverage` (negative EBITDA kept; `abs(interest)`); `builtin` T-116 (N9) | — | BUG |

### Layer MKT

| Rule | Status | Where | Test | Gap | Class → task |
|---|---|---|---|---|---|
| MKT-01 | partial | as-of reader ✓ `kg_schema/market_cap.market_caps_as_of`; stored ✗ `valuation._share_count` (this filing's cover count, dated after the period end, × the period-end close) | `test_market_cap.py` | The stored count isn't the one public on the period end (D6). | BUG → T-148 or T-071 (stored valuation) |
| MKT-02 | partial | reader ✓ (`_factor` splits after the count's date); stored ✗ (no split adjustment) | `test_market_cap.py:158–195` | D6. | as MKT-01 |
| MKT-03 | partial | `market_cap.cover_total`, `CLASS_CONVERSION` | `test_market_cap.py:207–231` | One traded close for every class, not a price per class (GOOG/GOOGL). BRK's 1,500 isn't recorded with an accession. "Others 1:1" isn't cited. | DECIDE (citations) + BUG (per-class price) |
| MKT-04 | partial | reader ✓; stored ✗ (the CSO × factor and diluted fallbacks) | `test_metrics_valuation.py` ⚠ | Remove both fallbacks. | BUG |

### Layer APP

| Rule | Status | Where | Gap | Class → task |
|---|---|---|---|---|
| APP-00 | ✗ | `assets.sector_id` / `sub_industry` (GICS, live) only | Article 9/7 isn't identified; no type overlay exists. | BUG → T-071 (scoring) + identification (new, or T-147 builds the table) |
| APP-01 | partial | revenue: `RevenuesNetOfInterestExpense` in `total_concepts` ✓ | COGS, inventory turnover and liquidity aren't marked NA (they are mostly empty by absence). | BUG (with APP-00) |
| APP-02 | ✗ | EV, debt metrics, NOPAT, ROIC and ROA are computed and scored for financial firms; `LEVERAGE_EXTREME` (N8) | — | BUG → T-071 + rules |
| APP-02b | ✗ | `NEGATIVE_FCF` veto; VALORIZATION FCF metrics (N8) | — | BUG |
| APP-03 | ✗ | — | Revenue as premiums + investment income isn't checked; NA not marked. | BUG (with APP-00) |
| APP-04 | ✗ | — | No REIT flag. | BUG |
| APP-04b | ✗ | `PaymentsForCapitalImprovements` sits in the general capex list | REIT components aren't implemented (D7). | BUG → T-148 (MET-08 scope) |
| APP-04c | ✗ | `effective_tax_rate` with a 21% default for REITs (D8) | — | BUG → with MET-03 |
| APP-07 | partial | `quality.DQ_NEG_EQUITY` quarantines ROE and D/E ✓; `cycle` sets D/E = +∞ (worst) for ranking; ROIC isn't guarded (N15) | +∞ is a ranking decision, while the rule says "not meaningful". | DECIDE (Q3) + BUG (ROIC) |
| APP-09 | ✓ | the ratios are empty when the concepts are absent | Articles 7/9 aren't marked NA explicitly; Phase 2 checks banks that file `AssetsCurrent`. | — |
| APP-10a | ✗ | `pipeline._list_filings` (ticker, `normalize_ticker`) | The CIK is in `assets` but unused for fetching. | BUG → T-149 |
| APP-10b-1 | ✗ | — | Successions aren't detected. | BUG (WARN) → Phase 2 prevalence first |
| APP-10b-2 | ✗ | — | — | as APP-10b-1 |
| APP-10b-3 | ✗ | — | — | as APP-10b-1 |

### Section L and Annex A

**Section L:**

| Item | Status | Note |
|---|---|---|
| L-01 | ✓ | |
| L-02 | ✓ | |
| L-03 | ✓ | |
| L-04 | **✗** | Amendments *are* ingested (N1). |
| L-05 | **✗** | N10. |
| L-06 | moot | The code uses the effective rate, not 21% federal. |
| L-07 | ✓ | |
| L-08 | ✓ | |

**Annex A:**
- A-01 to A-05 are implemented in `quality.py` as `DQ_MARGIN`, `DQ_OCF_MARGIN`, `DQ_MCAP_SCALE`, `DQ_REVENUE_POS` and `DQ_FCF_YIELD`.
- **Three project gates aren't in Annex A:**
  - `DQ_NEG_EQUITY`, which can be HARD and so trigger the veto;
  - `DQ_MARGIN_REVIEW`;
  - `TTM_CROSSCHECK`.
- `DQ_NEG_EQUITY` HARD excludes companies, so it needs an Annex A entry and its own sensitivity analysis (CHECKLIST, Q4).
- The per-cycle veto count by sector isn't reported anywhere (planned in Annex A).

## 4. Questions for Phase 3 (your decisions, or a reviewer round)

- **Q1. A component map with one component missing.**
  - Today: debt = LT + ST adds whichever is filed.
  - ID-02b and MET-00 don't say what happens when one term of a sourced map is absent. A1 says "absent" ≠ 0.
  - Options:
    - (a) leave debt empty;
    - (b) accept the filed terms, flagged "partial map".
  - **Proposal: (b) for debt**, because a company with no short-term borrowings often files no line, and (a) would empty the debt of many sound companies. This is a checklist gap, so it needs a reviewer round.
- **Q2. The revenue derivations (T-117 rebuild, T-140 correction) and the component sum (606 + lease income).** They are verified to the dollar in `model_fixes.md`, but no source states the identity, so they break ID-02b and CON-11 as written.
  - Options:
    - (a) keep them as declared project derivations: a new L-09, with prevalence;
    - (b) leave revenue empty when the filed total is missing.
  - **Proposal: (a)**. It affects few filings (APA, lessors), and the thesis declares it.
- **Q3. Negative equity in ranking.** `cycle` ranks D/E = +∞ (worst). APP-07 says D/E is not meaningful, which means NA.
  - Options:
    - (a) NA, as the rule says;
    - (b) keep +∞ as a declared decision.
  - **Proposal: (a)**. Negative equity is already handled by `DQ_NEG_EQUITY`. Ranking it worst as well punishes MCD-type buyback firms twice.
- **Q4. `DQ_NEG_EQUITY` as HARD.** It isn't in Annex A, and it vetoes companies. Proposal: add it as A-06 with its thresholds (from T-116) and include it in the sensitivity analysis.
- **Q5. Metric definitions without a rule.** The checklist has no sourced rule for these formulas:
  - EBITDA (operating income + D&A);
  - invested capital (debt + equity − cash);
  - quick ratio (current assets − inventory);
  - cash ratio (cash + short-term investments);
  - FCF to the firm (FCFE + after-tax interest);
  - the gross-profit derivation.

  For the jury every formula needs a source, so this is a coverage gap.
  - **Proposal:** a short reviewer round adding MET rules for them (C7 and C2 likely cover most), or listing them as project definitions.
- **Q6. `T-148`'s text against the checklist.**
  - (a) "Keep the `abs()` guards" contradicts ID-11.
  - (c) and (d) need a cited source for their maps (ID-02b).
  - **Proposal:** the T-147 PR amends T-148's text. Replace `abs()` with "reject a wrong sign and flag it", and cite the B6 definitions for the income-tax and short-term-debt maps. If B6 doesn't state the identity, (c) leaves tax empty, and the 21% rule (MET-03) makes it irrelevant to NOPAT anyway.

## 5. Phase 2 measurement list (added to the checklist's own list)

Read-only, production with `shasum` before and after, plus the pilot.

1. **N1:** filings whose stored accession is a 10-K/A or 10-Q/A on the SEC submissions API, and years with no 10-K because the amendment had no statements.
2. **N2:** confirm no `*.net_income` metric key exists, and the share of VALORIZATION scores built from two value metrics.
3. **N3–N6, N14:**
   - filings where OCF resolved to continuing operations;
   - filings with LT debt incl. current + `LongTermDebtCurrent`;
   - cash broader than cash + short-term investments;
   - balance values from a non-exact instant;
   - D&A = `Depreciation`.
4. **N8:** HARD vetoes on companies GICS lists as financial firms (`NEGATIVE_FCF`, `LEVERAGE_EXTREME`), per cycle.
5. **N9:** net-cash companies (positive EBITDA) whose `net_debt_to_ebitda` was discarded.
6. **PER-08:** periods the 20-day tolerance accepts that the ranges reject, and the reverse; FY columns of more than 371 days.
7. **ID-07:** stored `period_end` against the SEC `periodOfReport`.
8. **ID-13 to ID-15:** companyfacts values < 0 for the DQC lists.
9. **ID-18:** the factors the corrector applied, and whether `DQ_MCAP_SCALE` catches MCD without them.
10. **APP-10b:** 8-K12B/12G3/15D5 filings among the 503 CIKs.
11. **PER-12 to PER-14:** standard adoptions inside 2022–2026.
12. **The checklist's own list:** capex (b) and (c), oil & gas acquisitions, REIT overlap, neutral 50, winsor, diluted fallback, `available_at`, D1–D10, and the veto by sector.

## 6. Proposed mapping to tasks (for Phase 3, not final)

- **T-148** (line items and signs):
  - ID-01, 02a, 02b, 03, 05, 09/16, 11, 12, 20;
  - CON-08;
  - MET-07, MET-08, APP-04b;
  - EV and debt imputation (MET-00, D10);
  - N3–N6, N12, N14.

  This is larger than T-148's D1–D4 today; T-147 may split it (T-148 = D1–D4 plus selection, T-149 = capex).
- **New T-149 (filing identity):** PER-02/N1 (amendments), APP-10a (CIK), ID-07 (period end), PER-08 (period ranges).
- **New T-150 (consistency gates):** CON-01–04, 06, 07, 19 with DQ-01.
- **T-076:** MET-04 (already scoped).
- **T-071:** MET-00 (neutral 50), MET-05 (N2), APP-00–04c in scoring, APP-07 ranking (Q3), DQ-02 constant, MKT stored metrics.
- **Metric definitions (MET-01, 02, 03, 09, N10, N15):** no current task owns them. **Proposal:** fold them into T-148's successor for `metrics/`, or a new T-151 (metric definitions), after Q5.
- **Gateway repo:** ID-21 (N11); pass `unit` and the period-end date if ID-04 and ID-07 are adopted.
