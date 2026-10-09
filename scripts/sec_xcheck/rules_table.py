"""The audit's reading of each of the 78 rules of checklist v1.2.2 (T-147, Phase 3), as data.

One :class:`Entry` per rule: the implementing ``file:function`` (or ``none``), the status (✓ / partial / ✗ / n/a / L), the
test that pins it, the finding keys that carry the evidence at level (a) (the SEC source) and level (b) (our resolver or the
stored rows), the class (BUG / DECIDE / NOT-EXEC / CHECKLIST) and the task the fix goes to. ``docs/sec_data_checklist.md``
is rendered from this table and from ``prevalence_results.json``, so a number in the document is never typed by hand.

``note`` holds the audit's own reading where it differs from ``code_comparison_v1.md`` (nothing is dropped silently): both
readings are kept, in the order *Phase 1 said ... / the measurement shows ...*.

A rule's ``a``/``b`` entry ``"INCOMPLETE"`` marks **source prevalence incomplete**: the rule depends on a dimensional or
issuer-extension fact that ``companyfacts`` does not carry (and that ``financial_facts`` does not store), never a zero.
"""

from __future__ import annotations

from dataclasses import dataclass

INCOMPLETE = "INCOMPLETE"
T_LABELS = "tests/test_quarter_labels.py::test_the_quarter_is_counted_from_the_fiscal_year_end"
T_TTM = "tests/test_ttm_t105.py::test_the_year_to_date_identity"
T_AVAIL = "tests/test_available_at.py::test_the_writers_stamp_the_next_trading_day; tests/test_point_in_time_readers.py"
T_MEMBERS = "tests/test_kg_queries.py::test_members_asof_is_point_in_time"
T_GROWTH = "tests/test_metrics.py::test_growth_needs_a_prior_period; ::test_cagr_is_multi_year_from_the_fy_columns"
UNTESTED = "none"


@dataclass(frozen=True)
class Entry:
    impl: str
    status: str
    test: str
    a: tuple[str, ...] = ()
    b: tuple[str, ...] = ()
    cls: str = ""
    task: str = ""
    note: str = ""


def e(  # noqa: PLR0913 - an entry is its implementation, status, test, evidence keys, class, task and note
    impl: str,
    status: str,
    test: str = UNTESTED,
    *,
    a: tuple[str, ...] = (),
    b: tuple[str, ...] = (),
    cls: str = "",
    task: str = "",
    note: str = "",
) -> Entry:
    return Entry(impl, status, test, a, b, cls, task, note)


RULES: dict[str, Entry] = {
    # ---------------------------------------------------------------- ID
    "ID-01": e(
        "statements.py:_matches (standard, label), _fallback_match (caption, custom concept)",
        "partial",
        b=(
            "ID01.equity_label_or_standard_match",
            "ID01.income_tax_label_or_standard_match",
            "MET08.custom_caption_fallback",
        ),
        cls="BUG",
        task="T-148",
        note="a caption match is taken as capex with no software/intangibles check (see MET-08); no flag marks any extension, standard-concept or label match",
    ),
    "ID-02a": e(
        "statements.py:_first_component_match (document order over concepts, standard concepts and labels)",
        "✗",
        a=("src.equity.OTHER_CONCEPT",),
        b=("D2.equity_not_narrowest", "D2.net_income_profitloss_over_parent"),
        cls="BUG",
        task="T-148",
        note="D2 reproduced: equity read before treasury stock; net income read with NCI",
    ),
    "ID-02b": e(
        "REGISTRY component maps (revenue sum, debt LT+ST, income_tax standard match, short_term_debt first match); rebuild_total / _label_total_correction",
        "✗",
        "tests/test_revenue_rebuild.py, tests/test_statements.py (they pin the maps) ⚠",
        a=("src.income_tax.OTHER_CONCEPT", "src.D4"),
        b=(
            "D3.income_tax_component",
            "D4.short_term_debt_order_differs",
            "N4.debt_double_count",
            "N5.cash_double_count",
            "N14.da_depreciation_alone",
        ),
        cls="BUG / DECIDE (D-01, D-02)",
        task="T-148",
        note="no map cites a source; the D-06(d) order differs from today's first match in the filings counted",
    ),
    "ID-03": e(
        "REGISTRY net_income (NetIncomeLoss, ProfitLoss, available-to-common in document order)",
        "✗",
        b=("D2.net_income_profitloss_over_parent", "N13.net_income_available_to_common"),
        cls="BUG; N13 DECIDE",
        task="T-148",
        note="no ProfitLoss minus NCI path; WAT (and 14 other companies) fall back to the after-preferred-dividends line",
    ),
    "ID-04": e(
        "none: the payload carries no unit",
        "partial",
        a=("src.ID04.unit_mismatch",),
        cls="NOT-EXEC today",
        task="gateway PR (pass `unit`), then T-148",
        note="measurable at the source only: a registry concept filed with a unit other than USD / shares / USD per share",
    ),
    "ID-05": e(
        "gateway _instant_fact_values / _duration_fact_values ✓; statements.py:_instant_for ✗ (nearest earlier instant)",
        "partial",
        b=("N6.balance_from_other_date",),
        cls="BUG",
        task="T-148",
    ),
    "ID-06": e(
        "gateway column tags (Q = 80-100 days); pipeline._ytd_columns (year-to-date only inside the TTM identity)",
        "✓",
        T_TTM,
        cls="—",
        note="Phase 1 ✓ confirmed; the test pins the identity, the gateway tagging itself is tested in the gateway repo",
    ),
    "ID-07": e(
        "pipeline._resolve_target (latest column); never compared with periodOfReport",
        "✗",
        a=("ID07.period_end_differs_from_header",),
        cls="BUG",
        task="T-149",
        note="only EXE differs (stored FY2020 for a FY2021 10-K); the fix needs a gateway field, the audit read `reportDate` from the submissions API",
    ),
    "ID-08": e(
        "fiscal.py:label_year, quarter_label",
        "✓",
        T_LABELS,
        a=("N1.missing_cause.label_collision",),
        cls="—",
        note="✓ in today's code (T-140); production predates it: 9 10-Ks of 52/53-week filers were lost to a repeated FY label (N1 cause)",
    ),
    "ID-09": e(
        "gateway by_dimension(None); statements.py:_rows_for skips dimensional rows; _WHOLE_ENTITY_AXES",
        "partial",
        "tests/test_statements.py (dimensional-twin cases)",
        a=(INCOMPLETE,),
        b=(INCOMPLETE,),
        cls="DECIDE (flag per ID-16)",
        task="T-148",
        note="Phase 1: ConsolidationItemsAxis is a whole-entity axis only inside _total_is_plausible. `financial_facts` stores no dimensional rows, so T-128's twin check cannot be replayed from storage (the known fidelity class)",
    ),
    "ID-10": e(
        "non-dimensional only (gateway + resolver); cover shares skip LegalEntityAxis",
        "partial (untested here)",
        UNTESTED,
        a=(INCOMPLETE,),
        b=(INCOMPLETE,),
        cls="—",
        note="Phase 1 ✓; the only test is in the gateway repo (NEE), so it is `partial (untested)` here by the brief's rule",
    ),
    "ID-11": e(
        "abs() in cashflow._free_cash_flow, capex_intensity, valuation.fcf/SBC/_fcf_to_firm, leverage.interest_coverage",
        "✗",
        a=("src.capital_expenditure.FLIPPED", "src.interest_expense.FLIPPED"),
        b=("ID11.capital_expenditure_abs_changes_value", "ID11.interest_expense_abs_changes_value"),
        cls="BUG",
        task="T-148",
        note="the stored sign is the displayed one, so abs() is what makes capex and interest right today: removing it without ID-12 would flip FCF",
    ),
    "ID-12": e(
        "gateway passes preferred_sign; iter_facts drops it",
        "✗",
        a=("src.income_tax.FLIPPED", "src.cogs.FLIPPED", "src.interest_expense.FLIPPED"),
        b=("D1.income_tax_negative",),
        cls="BUG",
        task="T-148",
        note="D1 is much larger than the pilot suggested: the displayed income tax is the negative of the filed value in 1,237 production filings (134 companies)",
    ),
    "ID-13": e(
        "none",
        "✗",
        a=("src.ID-13.negative_listed_element", "src.ID13.negative_atypical_proxy"),
        cls="BUG (WARN)",
        task="T-148",
        note="measured on the registered DQC_0015 lists (2020-2026); the registry-concept proxy is kept as a cross-check",
    ),
    "ID-14": e(
        "none",
        "✗",
        a=("src.ID-14.negative_listed_element",),
        cls="BUG (WARN)",
        task="T-148",
        note="all 19 DQC_0013 elements are tax-rate reconciliation items; none is read by the resolver",
    ),
    "ID-15": e(
        "none",
        "✗",
        a=("src.ID-15.negative_listed_element",),
        cls="BUG (WARN)",
        task="T-148",
        note="34 listed elements; none is read by the resolver",
    ),
    "ID-16": e(
        "statements.py:_total_is_plausible (revenue only, no flag)",
        "partial",
        "tests/test_statements.py",
        a=(INCOMPLETE,),
        b=(INCOMPLETE,),
        cls="BUG (WARN)",
        task="T-148 (with ID-09)",
    ),
    "ID-17": e(
        "`decimals` is never read (structural)",
        "partial (untested)",
        UNTESTED,
        cls="—",
        note="Phase 1 ✓; nothing pins it, so the brief's rule makes it `partial (untested)`; a structural test goes with the task that touches the module",
    ),
    "ID-18": e(
        "db.py:detect_share_scale_factors + valuation._share_count multiply by the factor",
        "✗",
        "tests/test_share_scale.py ⚠ (pins the correction)",
        b=("ID18.scale_corrector_applied", "ID18.a03_would_catch_without_corrector"),
        cls="BUG",
        task="T-148 (with MKT-04)",
        note="the corrector rescales where the rule says quarantine. The question put to Phase 2 is answered: Annex A-03 catches the MCD 10^6 case without the correction (6 of 6 corrected filings). The cover-count path is never corrected",
    ),
    "ID-19": e(
        "none (L-08)",
        "L",
        b=("ID19.common_stock_shares_outstanding_captured",),
        cls="NOT-EXEC",
        task="—",
        note="147 of 5,076 production filings (2.9%): the checklist's figure reproduces exactly",
    ),
    "ID-20": e(
        "REGISTRY concept lists (four dead names)",
        "✗",
        a=("src.N12.dead_concept_facts_ours", "src.N12.dead_concept_facts_all_history"),
        b=("N12.dead_concepts_filed",),
        cls="BUG (WARN)",
        task="T-148 (cleanup)",
        note="the four names are dead but cost nothing in the window: no fact of ours uses them; all 25,709 historical facts were filed by 2021-11-22",
    ),
    "ID-21": e(
        "gateway reconcile_with_filed_facts (ambiguous → skipped); resolver first match",
        "partial",
        a=(INCOMPLETE,),
        b=(INCOMPLETE,),
        cls="BUG (gateway repo)",
        task="gateway PR",
        note="companyfacts collapses duplicates inside an instance, so the source cannot show them",
    ),
    # ---------------------------------------------------------------- PER
    "PER-01": e(
        "db.upsert_filing (available_from(filing_date)); kg_schema/availability triggers; cycle/data.py readers",
        "✓",
        T_AVAIL,
        b=(
            "PER01.metrics_available_at_null",
            "PER01.metrics_available_at_not_after_period_end",
            "PER01.scores_available_at_null",
            "PER01.scores_available_at_not_after_period_end",
        ),
        cls="—",
        note="0 of 11,878 stored ratios and 0 of 377 scores lack a date or carry one on or before the period end; the schema's triggers refuse a wrong one (tested)",
    ),
    "PER-02": e(
        "pipeline._select_filings (ordered[-1:]); db.upsert_filing ON CONFLICT DO UPDATE",
        "✗",
        "tests/test_pipeline_multi_filing.py::test_a_10k_keeps_only_the_most_recent_of_several_matches ⚠ (pins the replacement)",
        a=("N1.select_amendment_same_fy", "N1.missing_cause.amendment", "N1.10qa_overwrites_10q"),
        b=("N1.stored_is_amendment",),
        cls="BUG (high)",
        task="T-149",
        note="N1 confirmed. 8 stored accessions are amendments; 48 original 10-Ks of 30 companies are missing because a 10-K/A of the period exists (most had no statements: the run recorded an extraction failure)",
    ),
    "PER-03": e(
        "no `frames` call under src/ (structural)",
        "partial (untested)",
        UNTESTED,
        cls="—",
        note="Phase 1 ✓; nothing pins the absence",
    ),
    "PER-04": e(
        "metrics/growth.py (prior_of within the payload); metrics/cagr.py (the 10-K's own FY columns)",
        "✓",
        T_GROWTH,
        cls="—",
        note="no flag when a CAGR end is <= 0 beyond returning None (MET-04)",
    ),
    "PER-05": e(
        "db._quarter_flow_ending: Q4 = FY - the three recorded quarters",
        "partial",
        "tests/test_ttm.py",
        cls="DECIDE (D-08: FY - Q3 YTD)",
        task="T-149",
        note="not measured: it only runs on the four-quarter TTM path; the two readings agree only when CON-06 holds",
    ),
    "PER-06": e(
        "db.ttm_detail (identity ✓, four quarters, x4); agents._flag_annualization",
        "partial",
        "tests/test_ttm_t105.py; tests/test_cashflow_ttm.py",
        b=("PER06.metrics_built_on_x4", "PER06.metrics_built_on_ttm"),
        cls="BUG (D-09: x4 leaves the score)",
        task="T-148",
        note="production is metrics-v2, which predates the T-105 flags: its 0 means not measurable. The pilot (v4) has 288 of 10,036 10-Q metric rows on x4",
    ),
    "PER-08": e(
        "YEAR_TOLERANCE_DAYS = 20; db._PERIOD_TOLERANCE_DAYS = 20; fiscal._TOLERANCE_DAYS = 30; gateway buckets",
        "✗",
        b=("PER08.prior_gap_accepted_by_code_not_rule", "PER08.fy_prior_not_a_year"),
        cls="BUG",
        task="T-149",
        note="rare in the data: one quarter pairing and one fiscal-year pairing",
    ),
    "PER-09": e(
        "10-KT/10-QT never requested (structural)",
        "partial (untested)",
        UNTESTED,
        a=("PER09.transition_reports",),
        cls="—",
        note="one company changed fiscal year (FERG, 10-KT filed 2026-02-27): the series has a gap there",
    ),
    "PER-10": e(
        "kg_schema.queries.members_asof (stints)",
        "✓",
        T_MEMBERS,
        cls="—",
        note="✓ for the code. The production data is another matter, see PER-11",
    ),
    "PER-11": e(
        "pipeline._load_members(analysis_date): a run ingests only the members of its own date",
        "partial",
        a=("N1.stored_not_in_submissions",),
        cls="DECIDE",
        task="T-143 / universe",
        note="not measurable from the loaded data: production's universe is a single-day snapshot (503 rows valid from 2026-08-30, none left) and universe.db holds 20 members, so no leaver can be counted. Survivorship is structural for the 503",
    ),
    "PER-12": e(
        "none",
        "✗",
        cls="BUG (WARN)",
        task="T-151",
        note="no adoption flag; see PER-13 and PER-14 for the prevalence in the window",
    ),
    "PER-13": e(
        "none",
        "✗",
        b=("PER13.cagr_crosses_asc842",),
        cls="BUG (WARN, low)",
        task="T-151",
        note="only 4 CAGRs: calendar-year filers adopted ASC 842 in FY2019, the first column of the first 10-K; non-calendar filers (ADBE, CCL, LEN) cross it",
    ),
    "PER-14": e(
        "none",
        "✗",
        b=("PER14.net_income_cagr_crosses_cecl",),
        cls="BUG (WARN)",
        task="T-151",
        note="32 of 166 lenders' 10-Ks (FY2021, base FY2019) carry a net-income CAGR across CECL adoption",
    ),
    # ---------------------------------------------------------------- CON
    "CON-01": e(
        "none",
        "✗",
        a=("src.CON-01.block",),
        b=("CON-01.block",),
        cls="BUG",
        task="T-150",
        note="executable and almost always satisfied",
    ),
    "CON-02": e(
        "none",
        "✗",
        a=("src.CON-02.block",),
        b=("CON-02.block",),
        cls="BUG",
        task="T-150",
        note="AEP, JKHY and PPL file Assets ≠ current + noncurrent",
    ),
    "CON-03": e("none", "✗", a=("src.CON-03.block",), b=("CON-03.block",), cls="BUG", task="T-150"),
    "CON-04": e(
        "none",
        "✗",
        a=("src.CON-04.block",),
        b=("CON-04.block", "CON-04.warn"),
        cls="BUG",
        task="T-150",
        note="the stored-side failures (ARES) are not in the filed values: they come from the equity row the resolver chose (ID-02a)",
    ),
    "CON-05": e("none", "n/a", cls="—", note="INFO, not consumed"),
    "CON-06": e(
        "quality.record_ttm_crosscheck (1% relative, SOFT)",
        "partial",
        "tests/test_data_quality.py",
        cls="BUG (align to DQ-01)",
        task="T-150",
        note="not measured here: it needs the quarters of a fiscal year in one database",
    ),
    "CON-07": e(
        "none",
        "✗",
        a=("src.CON-07.block",),
        b=("CON-07.block",),
        cls="BUG",
        task="T-150",
        note="PPL, 5 filings, on stored rows only",
    ),
    "CON-08": e(
        "REGISTRY operating_cash_flow (total and continuing in document order)",
        "✗",
        a=("src.N3", "src.CON-08.block"),
        b=("N3.ocf_continuing_resolved",),
        cls="BUG",
        task="T-148 (+T-150)",
        note="N3 is wider than Phase 1 measured: besides the target columns, the YTD pair the TTM reads carries the continuing-operations line in many 10-Qs (F2)",
    ),
    "CON-09": e("none (L-07)", "L", cls="NOT-EXEC", task="—"),
    "CON-10": e(
        "none (L-08)",
        "L",
        b=("CON10.common_stock_shares_authorized_captured",),
        cls="NOT-EXEC",
        task="—",
        note="212 of 5,076: the checklist's figure reproduces exactly",
    ),
    "CON-11": e(
        "total_concepts first ✓; sum_components and the rebuild when no total is filed",
        "partial",
        "tests/test_revenue_rebuild.py",
        b=("L09.derived_by_company", "L09.refused_but_resolver_returns_a_value"),
        cls="DECIDE (D-02) + BUG",
        task="T-148",
        note="the component sum stands in for the total (D-02: only when the components are the only revenue lines). Bank revenue is a resolver defect: 10 of 24 Article 9 filers have none (F5)",
    ),
    "CON-12": e("none", "n/a", cls="—", note="INFO"),
    "CON-19": e(
        "none",
        "✗",
        a=("src.CON-19.block", "src.CON-19.warn"),
        b=("CON-19.block", "CON-19.warn"),
        cls="BUG",
        task="T-150",
        note="the stored-side check accepts either sign of the NCI line (the display sign is D1)",
    ),
    # ---------------------------------------------------------------- DQ
    "DQ-01": e(
        "none (the gateway's 1.0 absolute match is a float tolerance)",
        "✗",
        cls="BUG",
        task="T-150",
        note="the identity counts in this document use DQ-01's formula with rounding assumed USD 1M (decimals are not captured)",
    ),
    "DQ-02": e(
        "cycle/scores/normalize.py:cross_sectional_z (winsor=0.02)",
        "✗",
        b=("DQ02.winsor_2pct_vs_half_pct_universe", "DQ02.winsor_2pct_vs_half_pct_stored"),
        cls="BUG (one constant)",
        task="T-071",
        note="N17 confirmed: with n < 100 both fractions trim one value per tail, so the 20-asset cohorts so far are identical; on a real universe-wide cross-section (ROE, 2026-09-22) 245 of 495 assets move by more than a point (max 20)",
    ),
    # ---------------------------------------------------------------- MET
    "MET-00": e(
        "valorization.compute (50 when every factor is missing); valuation._enterprise_value (0 for missing debt/cash); sum_present; agents._fallback_assessment (starts at 50)",
        "✗",
        b=(
            "D10.ev_debt_or_cash_zero_filled",
            "N7.debt_absent",
            "D10.ev_zero_filled_stored",
            "MET00.valorization_neutral_50",
            "N16.fundamental_fallback_scores",
        ),
        cls="BUG",
        task="T-148 (EV, debt), T-071 (neutral 50), T-073 (fallback)",
        note="the neutral 50 never fires in the stored cycles (0 of 40: size is always present); the zero-filled EV is the live case",
    ),
    "MET-01": e(
        "profitability.py: net income TTM / ending assets",
        "✗",
        b=("APP02.return_on_assets",),
        cls="BUG",
        task="T-151",
        note="formula and timing both differ; ROA is also computed for financial firms (NA)",
    ),
    "MET-02": e(
        "profitability ROE and roic use ending capital",
        "✗",
        b=("N15.roe_on_negative_equity", "N15.roic_on_nonpositive_capital"),
        cls="BUG",
        task="T-151",
    ),
    "MET-03": e(
        "roic.effective_tax_rate (clamped [0, 0.5], 21% default); NOPAT on the filed rate",
        "✗",
        b=(
            "N10.tax_rate_21_on_nonpositive_pretax",
            "MET03.nopat_taxed_on_operating_loss",
            "MET03.nopat_on_filed_rate",
            "MET03.tax_rate_clamped",
        ),
        cls="BUG",
        task="T-151",
        note="N10 confirmed: the metric shows 21% when pre-tax income is missing or <= 0",
    ),
    "MET-04": e(
        "growth._yoy divides by |prior| (D9) ✗; cagr._cagr → None ✓",
        "✗ (growth) / ✓ (CAGR)",
        T_GROWTH,
        b=(
            "MET04.net_income_growth_base_nonpositive",
            "MET04.free_cash_flow_growth_base_nonpositive",
            "MET04.operating_income_growth_base_nonpositive",
        ),
        cls="BUG",
        task="T-151 (absorbs T-076's growth item)",
    ),
    "MET-05": e(
        "cycle/orchestrator.py:_valorization reads profitability.net_income / income_statement.net_income",
        "✗",
        b=("N2.metrics_named_net_income", "N2.valorization_value_factor_null"),
        cls="BUG (high)",
        task="T-149 (N2 moved there)",
        note="N2 confirmed: no stored metric has that name (0 of 34), so earnings_yield is never computed; in the stored cycles the whole value factor is empty in 35 of 40 rows. The fix also needs MET-05's dummy and a trailing-year net income",
    ),
    "MET-06": e("none (no multiple is computed)", "n/a", cls="—"),
    "MET-07": e(
        "REGISTRY long_term_debt / short_term_debt; leverage._total_debt",
        "partial",
        b=(
            "MET07.combined_debt_lease_element",
            "MET07.combined_element_is_only_long_term_debt_line",
            "N4.debt_double_count",
            "N7.st_debt_only",
        ),
        a=("src.N4",),
        cls="BUG",
        task="T-148 (+ flag)",
        note="production count of the combined element as the only long-term line is 1,375 against the checklist's 1,329 (3.5% higher; the reviewer's query is not in the repo)",
    ),
    "MET-08": e(
        "REGISTRY capital_expenditure (concepts, T-133 fallback tiers, caption fallback)",
        "✗",
        b=(
            "MET08.target_differs",
            "MET08.cause.includes_acquisitions_contrary_to_a",
            "MET08b.productive_assets_unflagged",
            "MET08.net_element_unflagged",
            "MET08.custom_caption_fallback",
            "MET08.tie_break_c_fires",
            "MET08a.og_property_with_ed",
        ),
        cls="BUG",
        task="T-148",
        note="see the O&G table (FANG and APA by year) and the capex census in prevalence_tables.json",
    ),
    "MET-09": e(
        "cashflow (fcf_conversion kept at net income <= 0); leverage (negative EBITDA kept); builtin T-116 (N9)",
        "✗",
        b=(
            "N9.net_cash_positive_ebitda",
            "MET09.net_debt_ebitda_on_nonpositive_ebitda",
            "MET09.fcf_conversion_on_nonpositive_income",
        ),
        cls="BUG",
        task="T-151",
        note="N9 confirmed: net-cash companies with positive EBITDA are discarded by T-116",
    ),
    # ---------------------------------------------------------------- MKT
    "MKT-01": e(
        "as-of reader kg_schema/market_cap.py ✓; stored valuation._share_count ✗",
        "partial",
        "tests/test_market_cap.py",
        a=("D07.cap_coverage",),
        b=("MKT04.shares_not_from_cover_page", "MKT01.price_date_offset_over_5_days"),
        cls="BUG (stored valuation)",
        task="T-148 (stored valuation, with ID-18)",
        note="production holds no cover counts (`filing_cover_shares` is pilot-only), so the as-of reader cannot run on it",
    ),
    "MKT-02": e(
        "reader: _factor splits after the count's date ✓; stored: no split adjustment",
        "partial",
        "tests/test_market_cap.py",
        cls="BUG (stored valuation, D6)",
        task="T-148 (stored valuation, with ID-18)",
    ),
    "MKT-03": e(
        "market_cap.cover_total, CLASS_CONVERSION",
        "partial",
        "tests/test_market_cap.py",
        a=("D07.cap_coverage",),
        cls="DECIDE (citations) + BUG (per-class price)",
        task="T-148 (stored valuation, with ID-18)",
        note="in the approximate cap estimate 50 of 500 CIKs have no non-dimensional cover total (multi-class issuers) and are left out",
    ),
    "MKT-04": e(
        "reader ✓; stored: the CSO x factor and diluted fallbacks",
        "partial",
        "tests/test_metrics_valuation.py ⚠ (pins the diluted fallback)",
        b=("MKT04.diluted_average_fallback", "MKT04.shares_not_from_cover_page"),
        cls="BUG",
        task="T-148 (stored valuation, with ID-18)",
        note="describes the stored metrics-v2 rows (238 of 258 valuation filings on the diluted average), not today's code, which reads the cover count first",
    ),
    # ---------------------------------------------------------------- APP
    "APP-00": e(
        "assets.sector_id / sub_industry (GICS, live) only",
        "✗",
        cls="BUG → built here",
        task="T-071 (scoring)",
        note="the company-type table is delivered: `company_types.csv`, 500 CIKs; 24 Article 9 filers, 17 Article 7, 10 other financial, 1 multi-sector holding, 448 operating; overlays REIT 28",
    ),
    "APP-01": e(
        "revenue total concepts incl. RevenuesNetOfInterestExpense ✓; COGS/current ratios not marked NA",
        "partial",
        b=(
            "APP01.liquidity_ratios_computed",
            "APP01.gross_margin_computed",
            "F5.article9_vetoed_by_missing_revenue",
        ),
        cls="BUG",
        task="T-148 (bank revenue) + T-071",
        note="the NA ratios are in fact empty (0 of 421 Article 9/7 filings); the defect is the other way: 10 of 24 Article 9 filers have no resolved revenue and are hard-vetoed by DQ_REVENUE_POS",
    ),
    "APP-02": e(
        "EV, debt metrics, NOPAT, ROIC and ROA computed and scored for financial firms",
        "✗",
        b=(
            "APP02.na_metrics_computed",
            "APP02.return_on_assets",
            "APP02.debt_to_equity",
            "APP02.return_on_invested_capital",
            "N8.financial_firms_hard_vetoed",
        ),
        cls="BUG",
        task="T-071",
        note="Phase 1 N8 said LEVERAGE_EXTREME vetoes banks at D/E > 3; the measurement shows it fires on none (D/E counts debt lines only, not deposits). The financial firms are vetoed by NEGATIVE_FCF (C, GS, MS), DQ_REVENUE_POS and margin gates instead",
    ),
    "APP-02b": e(
        "NEGATIVE_FCF veto; VALORIZATION FCF metrics",
        "✗",
        b=(
            "APP02.free_cash_flow_margin",
            "APP02.operating_cash_flow_margin",
            "APP02.capex_intensity",
        ),
        cls="BUG",
        task="T-071",
    ),
    "APP-03": e(
        "none",
        "✗",
        cls="BUG",
        task="T-071",
        note="TRV's revenue resolves to the filed total; premiums plus investment income are not checked and the NA metrics are not marked",
    ),
    "APP-04": e(
        "none",
        "✗",
        cls="BUG",
        task="T-071",
        note="no REIT flag; overlay available in company_types.csv",
    ),
    "APP-04b": e(
        "PaymentsForCapitalImprovements in the general capex list",
        "✗",
        b=(
            "APP04b.reit_acquisitions_left_out",
            "MET08.cause.reit_components",
            "APP04b.reit_improvements_and_acquisitions_overlap",
        ),
        cls="BUG",
        task="T-148 (MET-08 scope)",
    ),
    "APP-04c": e(
        "effective_tax_rate with a 21% default for REITs (D8)",
        "✗",
        b=("APP04c.reit_tax_rate_21pct", "APP04c.reit_filed_rate_clamped"),
        cls="BUG",
        task="T-151 (with MET-03)",
    ),
    "APP-07": e(
        "quality.DQ_NEG_EQUITY quarantines ROE and D/E ✓; cycle sets D/E = +inf for ranking; ROIC not guarded",
        "partial",
        "tests/test_data_quality.py",
        b=(
            "APP07.negative_equity_filings",
            "N15.roic_on_nonpositive_capital",
            "D04.negative_equity_companies",
            "D04.flagged_with_condition_2",
            "D04.flagged_without_condition_2",
        ),
        cls="DECIDE (D-04 accepted) + BUG (ROIC)",
        task="T-071 (D-04); T-151 (ROIC)",
        note="D-04's premise holds: LEVERAGE_EXTREME's negative-equity branch never runs because the rules read the quarantined D/E. Of 29 negative-equity companies only 4 are flagged by T-116's conditions, with or without the EBITDA <= 0 condition",
    ),
    "APP-09": e(
        "ratios empty when the concepts are absent",
        "partial (untested)",
        UNTESTED,
        b=("APP01.liquidity_ratios_computed",),
        cls="—",
        note="Phase 1 ✓; no test pins it. Evidence supports it: no Article 9/7 filing has a current ratio",
    ),
    "APP-10a": e(
        "pipeline._list_filings (ticker, normalize_ticker); the CIK is in assets but unused",
        "✗",
        a=("N1.stored_not_in_submissions",),
        cls="BUG",
        task="T-149",
        note="in this data every stored accession belongs to its asset's CIK (0 of 5,076 outside its submissions), so fetching by ticker has not mis-assigned a filing here",
    ),
    "APP-10b-1": e(
        "none",
        "✗",
        a=("APP10b.succession_8ks",),
        cls="BUG (WARN)",
        task="T-149 (detection)",
        note="18 of 503 companies filed an 8-K12B/8-K12G3/8-K15D5 since 2022",
    ),
    "APP-10b-2": e(
        "none",
        "✗",
        a=("APP10b.succession_8ks",),
        cls="BUG (WARN)",
        task="T-149",
        note="the link needs a hand reading of each succession 8-K",
    ),
    "APP-10b-3": e("none", "✗", a=("APP10b.succession_8ks",), cls="BUG (WARN)", task="T-149"),
}

# Section L: what the pipeline does and whether the audit confirmed the stated prevalence.
LIMITATIONS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "L-01": (
        "✓ declared",
        "OperatingLeaseCost in 11 of 5,076 production filings (reproduced), liabilities 196 / 1,265 / 2,109",
        ("L01.operating_lease_cost_captured", "L01.operating_lease_liability_noncurrent_captured"),
    ),
    "L-02": ("✓ declared", "REIT FFO is not computed (the golden set records it)", ()),
    "L-03": ("✓ declared", "GICS is a live snapshot; the company-type table uses it", ()),
    "L-04": (
        "✗ contradicted",
        "amendments ARE ingested (N1): the limitation does not describe the code",
        ("N1.stored_is_amendment",),
    ),
    "L-05": (
        "✗ contradicted",
        "effective_tax_rate shows 21% when pre-tax income is <= 0 (N10)",
        ("N10.tax_rate_21_on_nonpositive_pretax",),
    ),
    "L-06": ("moot", "the code uses the effective rate, not the 21% federal rate (MET-03)", ()),
    "L-07": ("✓ declared", "the calculation linkbase is not read", ()),
    "L-08": (
        "✓ declared",
        "ID-19 / CON-10 cannot run: 147 and 212 of 5,076 (reproduced)",
        (
            "ID19.common_stock_shares_outstanding_captured",
            "CON10.common_stock_shares_authorized_captured",
        ),
    ),
    "L-09": (
        "new (D-03)",
        "revenue derived by T-117/T-140: five companies, 110 values; the guards are calibrated; ICE fails the ex-post audit",
        ("L09.derived_by_company", "L09.expost_fails.ICE"),
    ),
}

ANNEX_A: dict[str, tuple[str, str]] = {
    "A-01": ("DQ_MARGIN", "implemented in fundamental_agent/quality.py"),
    "A-02": ("DQ_OCF_MARGIN", "implemented"),
    "A-03": (
        "DQ_MCAP_SCALE",
        "implemented; catches the MCD 10^6 case without the corrector (6 of 6)",
    ),
    "A-04": (
        "DQ_REVENUE_POS",
        "implemented; the gate that hard-vetoes 10 Article 9 filers whose revenue does not resolve (F5)",
    ),
    "A-05": (
        "DQ_FCF_YIELD",
        "implemented; not replayable on the whole universe (needs market cap)",
    ),
}

# The sensitivity analysis Annex A asks for needs the per-cycle veto count by sector: prevalence_tables.json["veto_replay"].
