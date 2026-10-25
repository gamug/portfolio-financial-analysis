"""Parse the EDGAR ``financials`` payload into something metric code can query.

The payload has three lists of rows -- ``income_statement``, ``balance_sheet`` and
``cash_flow``. Income and cash-flow rows use *duration* columns keyed like
``"2023-09-30 (FY)"`` / ``"2024-06-29 (Q3)"`` / ``"2024-06-29 (YTD)"``; balance-sheet
rows use bare *instant* dates like ``"2023-09-30"``. :meth:`Statements.get` hides that
difference: ask for a duration period and balance-sheet items resolve against the
instant column with the matching (or nearest-earlier) date.
"""

from __future__ import annotations

import itertools
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from fundamental_agent import fiscal
from fundamental_agent.dq01 import dq01_tolerance

STATEMENT_KEYS = ("income_statement", "balance_sheet", "cash_flow")
_DURATION_RE = re.compile(r"^\s*(?P<date>\d{4}-\d{2}-\d{2})\s*\((?P<tag>[A-Za-z0-9]+)\)\s*$")
_INSTANT_RE = re.compile(r"^\s*(?P<date>\d{4}-\d{2}-\d{2})\s*$")
_QUARTER_RE = re.compile(r"^Q[1-4]$")
INSTANT = "INSTANT"

# T-159c: the period lengths a filed fact may match (T-159b's payload; the same A's rule the
# gateway's untagged-column reconciliation and itemcheck use). A 10-Q's quarter is 80-119 days
# (never a year-to-date fact), a year is >= 351 days, a year-to-date column 175-285 days, and an
# untagged duration column at most 371 days (PER-08's 52/53-week year).
_QUARTER_DAYS = (80, 119)
_YTD_DAYS = (175, 285)
_FY_MIN_DAYS = 351
_UNTAGGED_MAX_DAYS = 371
# The unit a monetary item's fact must carry, and the two items whose facts are share counts.
_MONETARY_UNIT = "USD"
_SHARE_ITEMS = frozenset({"shares_outstanding", "diluted_shares"})
# Rule A' (T-159c round 3): the face row a filed value equals must be the item's own line -- its
# label names the item. An item without a pattern has no face proof (calibration only).
_FACE_LABEL = {
    "pretax_income": r"before (provision for |\(provision\) for |benefit from )?(income )?tax|pre-?tax",
    "net_income": r"\bnet (income|earnings|loss|\(loss\)|\(income\)|\(earnings\))",
    "net_income_to_common": r"\bnet (income|earnings|loss|\(loss\)|\(income\)|\(earnings\))",
    "equity_incl_nci": r"equity|deficit",
    "receivables": r"receivable",
    "inventory": r"inventor",
    "short_term_debt": (
        r"debt|borrowing|notes payable|commercial paper|current maturities|current portion"
        r"|funds purchased|repurchase"
    ),
    "long_term_debt": r"debt|borrowing|notes payable|senior notes|\bnotes\b",
    "stock_based_compensation": (
        r"(stock|share|equity)[- ]?based|stock compensation|share compensation|stock option"
        r"|restricted stock|equity compensation"
    ),
    "depreciation_amortization": r"depreciation|amortization|depletion",
    "interest_expense": r"interest|financing charges",
    "short_term_investments": r"investments|securities|marketable|trading assets",
    "cash": r"\bcash\b",
    "cogs": r"cost of|costs of|operating expenses|operating costs",
    "redeemable_nci": r"redeemable|noncontrolling|non-controlling",
    "income_tax": r"\btax",
    "capital_expenditure": r"capital|property|plant|equipment|productive assets",
}
_FACE_LABEL_NOT = {
    "equity_incl_nci": r"accumulated other comprehensive|liabilities and",
    "net_income": r"per share|comprehensive",
    "net_income_to_common": r"per share|comprehensive|before",
    "interest_expense": r"in shares|attributable to noncontrolling|other income",
}
_OTHER_DEBT = {"long_term_debt": "short_term_debt", "short_term_debt": "long_term_debt"}


def _face_label_names_item(item: str, label: str) -> bool:
    pattern = _FACE_LABEL.get(item)
    if pattern is None or not re.search(pattern, label.lower()):
        return False
    exclude = _FACE_LABEL_NOT.get(item)
    return exclude is None or not re.search(exclude, label.lower())


_SHARE_UNIT = "shares"
# T-159c: registry concepts whose statement-face caption decides whether they are the item. A
# filed fact carries no caption, so the fallback never reads them: the cash-and-short-term-
# investments element is not cash without the caption check, and the net proceeds element is not
# capex when the caption names proceeds (C3, MET-08; F4's HOLX case).
_FILED_FACT_SKIP_CONCEPTS = frozenset(
    {
        "us-gaap_CashCashEquivalentsAndShortTermInvestments",
        "us-gaap_PaymentsForProceedsFromProductiveAssets",
    }
)


@dataclass(frozen=True)
class Period:
    """A single reporting-period column."""

    key: str  # raw column key, e.g. "2024-06-29 (Q3)" or "2024-06-29"
    date: str  # ISO period-end date
    tag: str  # "FY" | "Q1".."Q4" | "YTD" | "INSTANT"

    @property
    def year(self) -> int:
        return int(self.date[:4])

    @property
    def is_fy(self) -> bool:
        return self.tag == "FY"

    @property
    def is_quarter(self) -> bool:
        return bool(_QUARTER_RE.match(self.tag))

    @property
    def is_instant(self) -> bool:
        return self.tag == INSTANT


@dataclass(frozen=True)
class Substitute:
    """T-159a: the concepts that stand in for an item the filing reports under **another standard
    concept**, and the rules under which they may be read.

    The substitutes are read **only** when the item's own concepts found nothing (ID-02a
    precedence: the narrowest matching definition wins, so a concept the registry already reads
    always wins over a substitute and an item that resolves today resolves to the same value after
    T-159a). Every element below is a us-gaap element the taxonomy defines as the item's total
    (or, for a component, as a member of the item's family); the definition each one rests on is
    quoted in ``docs/model_fixes.md`` (T-159a).

    * ``totals`` -- the item's total under another standard concept, read in the declared order
      (the first filed wins);
    * ``families`` -- the component families: a component is accepted only when it is the **sole**
      filed member of **one** family (ID-02b: a component is never the total; it is one only when
      it is all the filing has). The names are concept **prefixes**, so a family covers its element
      and its variants (``EnergyRelatedInventory`` covers ``...GasStoredUnderground``);
    * ``classes`` -- the **disjoint** classes (ASC 320's short-term-investment classes): each class
      contributes one member (its own total first) and the classes are summed;
    * ``sums`` -- the exact two-part sums (ID-02b: a map with a source states the identity): both
      parts must be filed for the period, or the item stays empty.
    """

    totals: tuple[str, ...] = ()
    families: tuple[str, ...] = ()
    classes: tuple[tuple[str, ...], ...] = ()
    sums: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class LineItem:
    """How to find one economic quantity across possibly-inconsistent filings.

    ``statements`` scopes the search: a balance-sheet item must never match a
    similarly named cash-flow "increase/decrease in ..." row.

    ``total_concepts`` and ``sum_components`` exist for the case where a
    filer reports *multiple, distinct* non-dimensional rows for one line item
    -- e.g. a lessor's ASC-842 lease income alongside its ASC-606 contract
    revenue, with (or without) a separately tagged aggregate. Both default to
    a no-op, so every item that doesn't set them keeps today's exact
    first-document-order-match behavior (see :func:`Statements.get`, F2 --
    ``docs/model_fixes.md``).

    ``synonym_groups`` handles a third case the sum needs to guard against:
    some ``concepts`` entries are not separate, additive streams but
    *alternate encodings of the same one* -- e.g. an ASC-606 filer tags its
    revenue line as EITHER ``...ExcludingAssessedTax`` OR
    ``...IncludingAssessedTax``, never both as genuinely different amounts.
    Each group lists such mutually-exclusive concepts together; when summing
    components, at most one value per group counts, preferring whichever
    group member appears earliest in ``concepts`` -- see the F2 Sourcery
    follow-up in ``docs/model_fixes.md``.
    """

    name: str
    statements: tuple[str, ...]
    concepts: tuple[str, ...] = ()
    standard: tuple[str, ...] = ()
    label_contains: tuple[str, ...] = ()
    # An already-aggregated total: if any row matches one of these, it wins
    # outright over every `concepts` match, regardless of document order --
    # unless Statements._total_is_plausible (T-095/T-128) rejects it as a
    # duplicate of one dimensional slice that is smaller than a named
    # `concepts` candidate, a tagging defect rather than a real aggregate.
    total_concepts: tuple[str, ...] = ()
    # When no `total_concepts` row matches: sum one value per distinct
    # additive component (instead of just returning the first match) --
    # these represent genuinely separate streams when no total is tagged.
    # Matches only `concepts` -- never the `label_contains` fuzzy fallback,
    # which can hit an unrecognized aggregate/custom-extension "Total ..."
    # row and double-count it against the real components.
    sum_components: bool = False
    # Concepts that are alternate *encodings* of one stream, not separate
    # amounts -- see the class docstring. Only consulted by the
    # `sum_components` path.
    synonym_groups: tuple[tuple[str, ...], ...] = ()
    # Consulted only when the ``concepts``/``standard``/``label_contains`` pass found no value
    # (T-133): ordered tiers, each tried in turn, first row with a value wins. Unlike
    # ``concepts`` -- one pass in document order -- an earlier tier here beats a later one however
    # the rows are ordered, so a filer's real spend is not displaced by an acquisition line.
    fallback_concepts: tuple[str, ...] = ()
    # Concepts added to the value of one of the first ``fallback_addend_tiers`` fallback tiers, when
    # the filer reports them: a line that sits *beside* the tier's own rather than inside it, so a
    # sum cannot double count (T-133 review: EOG's "other property, plant and equipment" is a
    # separate line from its oil and gas additions).
    fallback_addends: tuple[str, ...] = ()
    fallback_addend_tiers: int = 0
    # The last resort: a row whose label matches this regex and none of ``label_fallback_exclude``
    # (both case-insensitive). A filer's custom-extension concept (PSX's
    # ``psx_CapitalExpendituresAndInvestments``) has no stable tag to list, only its caption.
    label_fallback: str = ""
    label_fallback_exclude: str = ""
    # ID-13 (T-148): concepts whose filed value must not be negative (the DQC_0015 elements the
    # resolver reads, listed in every release of the 2020-2026 window). A negative filed value of
    # one is a wrong sign: the row is rejected and the resolution flagged, never repaired with
    # abs() (ID-11). A caption-matched row (``label_fallback``) is rejected on the same test when
    # the item lists any: its caption names a payment.
    nonnegative: tuple[str, ...] = ()
    # The composite resolver this item uses instead of the generic concept lookup (T-148): its
    # name is a key of :data:`_COMPOSITE`. The ``concepts`` stay listed for the audit scripts.
    resolver: str = ""
    # MET-08 (T-148 part 1 limitation): the item keeps master's order -- the first matching row *in
    # document order*, whichever of its concepts it carries -- instead of the spec's concept order
    # (ID-02a). Capex only: MET-08 (b)(1) makes ``PaymentsToAcquireProductiveAssets`` "a flagged
    # fallback, never added to PP&E", which a concept-first pass would read before ETR's own
    # ``PaymentsForCapitalImprovements`` (106M against 5.07B); part 2 (f) writes capex's order.
    document_order: bool = False
    # (concept, flag): when the resolved value comes from that concept, raise the flag (T-148 step 2.1).
    concept_flags: tuple[tuple[str, str], ...] = ()
    # (concept, flag): when the item resolves empty and that concept is filed (valued,
    # non-dimensional) in the column, raise the flag (T-148 step 2.1).
    unread_flags: tuple[tuple[str, str], ...] = ()
    # T-159a: the substitute concepts -- the item's total under another standard concept, the
    # component families, the disjoint classes and the exact sums. Read only when the item's own
    # concepts found nothing; see :class:`Substitute`.
    substitute: Substitute | None = None


_INCOME = ("income_statement",)
_BALANCE = ("balance_sheet",)
_CASHFLOW = ("cash_flow",)

G = "us-gaap_"

# -- Debt (MET-07, ID-02b, D-01, D-06(d), N23; T-148) ---------------------------------------------
# Short-term debt is a map of two terms, each the first of its concepts that is filed. The terms
# are disjoint by definition (B6): ``LongTermDebtCurrent`` is long-term debt classified as current,
# ``ShortTermBorrowings`` debt with an initial term under a year, ``CommercialPaper`` "short-term
# borrowings using unsecured obligations" (so it is read only when ``ShortTermBorrowings`` is not).
# ``OtherLongTermDebtCurrent`` and ``NotesPayableCurrent`` (N23: Consolidated Edison files its
# "long-term debt due within one year" and its "notes payable" under them) take the place of the
# term they are a kind of when it is not filed; they are never added to it. ``DebtCurrent``
# ("debt and lease obligation, classified as current") is the last resort and is flagged
# "includes leases".
# T-159a rule 1: the current maturities a filer files as the combined debt-and-lease element
# (``LongTermDebtAndCapitalLeaseObligationsCurrent``: "Amount of long-term debt and lease
# obligation, classified as current") are read after the two above, flagged
# ``includes_finance_leases`` (the flag its non-current partner already carries).
SHORT_TERM_DEBT_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "current_maturities",
        (
            G + "LongTermDebtCurrent",
            G + "OtherLongTermDebtCurrent",
            G + "LongTermDebtAndCapitalLeaseObligationsCurrent",
        ),
    ),
    (
        "borrowings",
        (G + "ShortTermBorrowings", G + "CommercialPaper", G + "NotesPayableCurrent"),
    ),
)
# T-159a rules 2 and 3: the calculation children of the map's terms (the FASB 2026 sfp-cls arc).
# A family counts once: the node when it is filed, else the sum of its filed children. A child is
# never added beside its parent. ``SecuritiesSoldUnderAgreementsToRepurchase`` and the FHLB
# advances are not in the tree: they are securities financing and bank funding (APP-02 makes a
# bank's debt metrics not applicable).
SHORT_TERM_DEBT_CHILDREN: dict[str, tuple[str, ...]] = {
    G + "LongTermDebtCurrent": (
        G + "SecuredDebtCurrent",
        G + "ConvertibleDebtCurrent",
        G + "UnsecuredDebtCurrent",
        G + "SubordinatedDebtCurrent",
        G + "ConvertibleSubordinatedDebtCurrent",
        G + "LongTermCommercialPaperCurrent",
        G + "LongTermConstructionLoanCurrent",
        G + "LongtermTransitionBondCurrent",
        G + "LongtermPollutionControlBondCurrent",
        G + "OtherLongTermDebtCurrent",
        G + "LinesOfCreditCurrent",
        G + "NotesAndLoansPayableCurrent",
        G + "SpecialAssessmentBondCurrent",
    ),
    G + "NotesAndLoansPayableCurrent": (
        G + "NotesPayableCurrent",
        G + "LoansPayableCurrent",
    ),
    G + "NotesPayableCurrent": (
        G + "ConvertibleNotesPayableCurrent",
        G + "SeniorNotesCurrent",
        G + "OtherNotesPayableCurrent",
    ),
    G + "ShortTermBorrowings": (
        G + "BankOverdrafts",
        G + "CommercialPaper",
        G + "BridgeLoan",
        G + "ConstructionLoan",
        G + "ShortTermBankLoansAndNotesPayable",
        G + "ShortTermNonBankLoansAndNotesPayable",
        G + "WarehouseAgreementBorrowings",
        G + "OtherShortTermBorrowings",
    ),
}
# The node whose children the term sums when the term is filed only as them (rules 2 and 3).
SHORT_TERM_DEBT_ROOTS: dict[str, str] = {
    "current_maturities": G + "LongTermDebtCurrent",
    "borrowings": G + "ShortTermBorrowings",
}
# The filed line that contains the whole tree: when it is the term's own read, the children are
# inside it and are not added (``LongTermDebtAndCapitalLeaseObligationsCurrent`` contains
# ``LongTermDebtCurrent``; ``LongTermDebtCurrent`` contains its own children).
SHORT_TERM_DEBT_CONTAINERS: dict[str, tuple[str, ...]] = {
    "current_maturities": (
        G + "LongTermDebtCurrent",
        G + "LongTermDebtAndCapitalLeaseObligationsCurrent",
    ),
    "borrowings": (G + "ShortTermBorrowings",),
}
SHORT_TERM_DEBT_LAST = G + "DebtCurrent"
# Long-term lines that already contain the current maturities: a short-term term that is a kind of
# current maturity is not added to them (N4).
INCLUDES_CURRENT_MATURITIES = (
    G + "LongTermDebt",
    G + "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
)
# The sourced long-term-debt sums (C2, ID-02b; T-151 part 1). Each map is the calculation arc of a
# FASB 2026 total, read **only when no concept the resolver reads today is filed** in the column.
# The fasb zip ``docs/checklist_sec/sec_xcheck/fasb_2026_dl/us-gaap-2026.zip``
# (sha256 f4c8b8b5697ba7d825a8614b159611cd25a46640e98a9737cda1e4a672bd4c81) holds the arcs:
#   ``stm/us-gaap-stm-sfp-cls-cal-2026.xml`` -- LongTermDebtNoncurrent (15 children, orders 10-150):
#     LongTermLineOfCredit, CommercialPaperNoncurrent, ConstructionLoanNoncurrent,
#     SecuredLongTermDebt, SubordinatedLongTermDebt, UnsecuredLongTermDebt, ConvertibleDebtNoncurrent,
#     ConvertibleSubordinatedDebtNoncurrent, LongTermTransitionBond, LongTermPollutionControlBond,
#     JuniorSubordinatedDebentureOwedToUnconsolidatedSubsidiaryTrustNoncurrent, LongTermNotesAndLoans,
#     SpecialAssessmentBondNoncurrent, LongtermFederalHomeLoanBankAdvancesNoncurrent,
#     OtherLongTermDebtNoncurrent.
#   ``stm/us-gaap-stm-sfp-dbo-cal-2026.xml`` -- LongTermDebt (15 children, orders 10-150):
#     SecuredDebt, UnsecuredDebt, SubordinatedDebt, ConvertibleDebt, ConvertibleSubordinatedDebt,
#     JuniorSubordinatedDebentureOwedToUnconsolidatedSubsidiaryTrust, LineOfCredit,
#     LongtermCommercialPaperCurrentAndNoncurrent, LongtermTransitionBondCurrentAndNoncurrent,
#     LongtermPollutionControlBondCurrentAndNoncurrent,
#     LongtermConstructionLoanCurrentAndNoncurrent, NotesAndLoansPayable, SpecialAssessmentBond,
#     OtherLongTermDebt, FederalHomeLoanBankAdvancesLongTerm.
# B6 definitions (``elts/us-gaap-doc-2026.xml``): LongTermDebtNoncurrent "Amount, after deduction of
# unamortized premium (discount) and debt issuance cost, of long-term debt classified as
# noncurrent. Excludes lease obligation."; LongTermDebt the same "... of long-term debt. Excludes
# lease obligation."; SecuredDebt "Carrying value as of the balance sheet date, including the
# current and noncurrent portions, of collateralized debt obligations ..."; UnsecuredDebt
# "Including the current and noncurrent portions, carrying value as of the balance sheet date of
# uncollateralized debt obligations ..." -- the dbo tree carries the current maturities too, so its
# map is flagged ``includes_current_maturities`` (N4). The cls tree excludes them.
LONG_TERM_DEBT_NONCURRENT_CHILDREN: tuple[str, ...] = (
    G + "LongTermLineOfCredit",
    G + "CommercialPaperNoncurrent",
    G + "ConstructionLoanNoncurrent",
    G + "SecuredLongTermDebt",
    G + "SubordinatedLongTermDebt",
    G + "UnsecuredLongTermDebt",
    G + "ConvertibleDebtNoncurrent",
    G + "ConvertibleSubordinatedDebtNoncurrent",
    G + "LongTermTransitionBond",
    G + "LongTermPollutionControlBond",
    G + "JuniorSubordinatedDebentureOwedToUnconsolidatedSubsidiaryTrustNoncurrent",
    G + "LongTermNotesAndLoans",
    G + "SpecialAssessmentBondNoncurrent",
    G + "LongtermFederalHomeLoanBankAdvancesNoncurrent",
    G + "OtherLongTermDebtNoncurrent",
)
LONG_TERM_DEBT_CHILDREN: tuple[str, ...] = (
    G + "SecuredDebt",
    G + "UnsecuredDebt",
    G + "SubordinatedDebt",
    G + "ConvertibleDebt",
    G + "ConvertibleSubordinatedDebt",
    G + "JuniorSubordinatedDebentureOwedToUnconsolidatedSubsidiaryTrust",
    G + "LineOfCredit",
    G + "LongtermCommercialPaperCurrentAndNoncurrent",
    G + "LongtermTransitionBondCurrentAndNoncurrent",
    G + "LongtermPollutionControlBondCurrentAndNoncurrent",
    G + "LongtermConstructionLoanCurrentAndNoncurrent",
    G + "NotesAndLoansPayable",
    G + "SpecialAssessmentBond",
    G + "OtherLongTermDebt",
    G + "FederalHomeLoanBankAdvancesLongTerm",
)
# ID-02b "no debt line at all": a balance-sheet concept that names a debt obligation, and an
# income-statement concept that names interest expense, in any taxonomy (an extension counts: it
# is evidence the company has debt). Receivables, securities, gains and the like are not debt.
_DEBT_LINE_RE = re.compile(
    r"debt|notes|loans?payable|borrowing|linesofcredit|lineofcredit|commercialpaper|debenture"
    r"|mortgage|financingarrangement",
    re.IGNORECASE,
)
_NOT_A_DEBT_LINE_RE = re.compile(
    r"securities|receivable|investment|asset|gain|loss|expense|income|interest|issuance"
    r"|repayment|proceeds|fairvalue|deferred|held|discount|premium|realestate|payable.*related",
    re.IGNORECASE,
)
_INTEREST_EXPENSE_LINE_RE = re.compile(r"^Interest(Expense|AndDebtExpense)")

# -- Depreciation, depletion and amortization (MD-01, ID-02b; T-151 part 1, C1) --------------------
# The sourced D&A sum, from the FASB 2026 cash-flow calculation arc
# ``stm/us-gaap-stm-scf-indir-cal-2026.xml`` (same zip, sha256 f4c8b8b5...):
#   DepreciationDepletionAndAmortization -> Depreciation (order 10),
#     AdjustmentForAmortization (20), OtherDepreciationAndAmortization (30), Depletion (40);
#   AdjustmentForAmortization -> CostOfGoodsAndServicesSoldAmortization (10),
#     PresentValueOfFutureInsuranceProfitsAmortizationExpense1 (20),
#     AmortizationOfIntangibleAssets (30), DeferredPolicyAcquisitionCostAmortizationExpense (40),
#     DeferredSalesInducementsAmortizationExpense (50), AmortizationOfValueOfBusinessAcquiredVOBA
#     (60), AmortizationOfDeferredLoanOriginationFeesNet (70), AmortizationOfMortgageServicingRightsMSRs
#     (80), AccretionAmortizationOfDiscountsAndPremiumsInvestments (90), AmortizationOfAcquisitionCosts
#     (100), AmortizationOfDeferredSalesCommissions (110), FinanceLeaseRightOfUseAssetAmortization
#     (120), AmortizationOfPowerContractsEmissionCredits (130), AmortizationOfNuclearFuelLease (140),
#     AmortizationOfRegulatoryAsset (150), AmortizationOfAdvanceRoyalty (160),
#     AmortizationOfDeferredPropertyTaxes (170), AmortizationOfRateDeferral (180),
#     AmortizationOfDeferredHedgeGains (190), OtherAmortizationOfDeferredCharges (200),
#     AmortizationOfFinancingCostsAndDiscounts (210), AmortizationAndDepreciationOfDecontaminating-
#     AndDecommissioningAssets (220).
# B6 definitions (``elts/us-gaap-doc-2026.xml``): DepreciationDepletionAndAmortization "The
# aggregate expense recognized in the current period that allocates the cost of tangible assets,
# intangible assets, or depleting assets to periods that benefit from use of the assets.";
# Depreciation "The amount of expense recognized in the current period that reflects the allocation
# of the cost of tangible assets over the assets' useful lives."; Depletion "The noncash expense
# charged against earnings to recognize the consumption of natural resources.";
# OtherDepreciationAndAmortization "Amount of expense charged against earnings to allocate the cost
# of tangible and intangible assets over their remaining economic lives, classified as other.";
# AmortizationOfIntangibleAssets "Amount of amortization expense for finite-lived intangible asset.
# Excludes goodwill and capitalized cost for software to be sold, leased, or marketed.";
# AdjustmentForAmortization "The aggregate amount of recurring noncash expense charged against
# earnings in the period to allocate the cost of assets over their estimated remaining economic
# lives." The D&A-**nature** children are the four below; the other children of
# AdjustmentForAmortization (financing costs, debt discount or premium, deferred charges, loan fees,
# ...) are not D&A, and a filed one refuses the sum ("only lines", C1 (c)).
DA_SOURCED_CHILDREN: tuple[str, ...] = (
    G + "Depreciation",
    G + "Depletion",
    G + "OtherDepreciationAndAmortization",
    G + "AmortizationOfIntangibleAssets",
)
# A cash-flow row whose concept or caption names D&A: the C1 (c) "only lines" scan.
_DA_NAME_RE = re.compile(r"depreciat|amorti|deplet", re.IGNORECASE)
# E3 (T-156, ID-01 with MD-01 as the item's definition): the captions a company extension on the
# cash-flow statement may carry to be read as D&A. A1 5.6.1: "filers should normally assign an
# appropriate label for a concept already defined in a standard taxonomy", so the filer's own caption
# names the standard concept. The plain pair "Depreciation and amortization" (the element
# ``DepreciationAndAmortization``'s own name) and the standard labels of the three aggregate D&A
# elements the resolver accepts (``elts/us-gaap-lab-2026.xml``): ``DepreciationDepletionAndAmortization``
# "Depreciation, Depletion and Amortization"; ``DepreciationAndAmortization`` "Depreciation, Depletion
# and Amortization, Nonproduction"; ``DepreciationAmortizationAndAccretionNet`` "Depreciation,
# Amortization and Accretion, Net". A caption that adds anything -- "and other", "impairment",
# "accretion", "depletion" alone, a component's label -- is not read.
DA_CAPTION_ELEMENTS: dict[str, str] = {
    "depreciation and amortization": G + "DepreciationAndAmortization",
    "depreciation depletion and amortization": G + "DepreciationDepletionAndAmortization",
    "depreciation depletion and amortization nonproduction": G + "DepreciationAndAmortization",
    "depreciation amortization and accretion net": G + "DepreciationAmortizationAndAccretionNet",
}


def _normalize_caption(label: str) -> str:
    """A caption comparable across case, punctuation, "&" for "and" and a trailing "expense" (E3)."""
    text = label.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r" expense$", "", text)


# C3 (T-151 part 1): a net-element caption that names proceeds, a sale or a disposition. Such a line
# is net proceeds whatever its filed sign (COP: "Proceeds from asset dispositions" displayed
# -17,000,000 with ``preferred_sign`` -1, a filed +17,000,000).
_NET_ELEMENT_PROCEEDS_RE = re.compile(r"proceeds?|\bsales?\b|disposition", re.IGNORECASE)

# T-071 part 2b (D6): a caption naming a proceeds, a sale, a disposition or a **gain** is not a
# capex payment, whatever its sign (the C3 net-element test, plus "gain").
_NOT_A_CAPEX_PAYMENT_RE = re.compile(r"proceeds?|\bsales?\b|disposition|gain", re.IGNORECASE)

# T-071 part 2b (D6, ruling of 2026-10-15): the REIT extension path takes only a *cash* capex line.
# A concept's local name or caption naming an accrual, an amount still unpaid or a period-end mirror
# is refused -- the filer's own wording is the discriminator (PSA's "…RemainingUnpaidAtPeriodEnd"
# lines carry the *same caption* as their cash counterpart, so only the concept name separates them).
_NOT_AN_EXTENSION_CAPEX_RE = re.compile(
    r"accrual|accrued|unpaid|remaining|payable|at period end", re.IGNORECASE
)

# -- Cash (T-148 (k), B4, B6) ----------------------------------------------------------------------
_CASH_PAREN_RE = re.compile(r"\([^)]*\)")
_CASH_NOTE_RE = re.compile(r"\(note\s+\w+\)", re.IGNORECASE)
_ALLOWED_K1_CAPTIONS = frozenset(
    {
        "cash",
        "cash and cash equivalents",
        "cash and equivalents",
        "cash, cash equivalents",
    }
)
_INVESTMENT_KEYWORDS = ("invest", "marketable", "securit", "short-term", "short term")

# -- Revenue D-02 condition (T-148 (e), D-02, B4) --------------------------------------------------
_REV_CONCEPT_RE = re.compile(r"Revenue", re.IGNORECASE)
_REV_CAPTION_RE = re.compile(r"\b(revenues?|sales)\b", re.IGNORECASE)
_COST_EXPENSE_RE = re.compile(r"\b(costs?|expenses?)\b", re.IGNORECASE)

# -- Parent net income (ID-03, CON-19; T-148) -------------------------------------------------------
# The us-gaap elements for income attributable to the noncontrolling interest: the total and its
# redeemable / nonredeemable parts (B6).
_NCI_INCOME_CONCEPTS = (
    G + "NetIncomeLossAttributableToNoncontrollingInterest",
    G + "NetIncomeLossAttributableToRedeemableNoncontrollingInterest",
    G + "NetIncomeLossAttributableToNonredeemableNoncontrollingInterest",
)
# The conservative inference for any other row (an extension, or a us-gaap line with a filer's own
# caption): its concept name and caption together name a noncontrolling (minority) interest, speak
# of an income, and are not a total that *includes* the interest (``...IncludingPortionAttributable
# ToNoncontrollingInterest``), the pre-tax lines the taxonomy names after it
# (``...BeforeIncomeTaxes...NoncontrollingInterest``) or a flow with it (dividends, contributions,
# payments, proceeds).
_NCI_NAME_RE = re.compile(r"non-?controlling|minority\s*interest", re.IGNORECASE)
_NCI_INCOME_WORD_RE = re.compile(r"income|loss|earnings|profit|allocation", re.IGNORECASE)
_NCI_NOT_AN_INCOME_RE = re.compile(
    r"including|before|payments?|proceeds|dividends?|distributions?|contributions?", re.IGNORECASE
)

# -- Noncontrolling interest (MD-02, MD-35; D6, T-151 part 2) ---------------------------------------
# NCI = the NCI in equity (B6: ``MinorityInterest``) + the redeemable NCI presented between
# liabilities and equity. The FASB 2026 sfp-cls calculation arc
# ``stm/us-gaap-stm-sfp-cls-cal-2026.xml`` (orders 10/20/30) makes
# ``RedeemableNoncontrollingInterestEquityCarryingAmount`` =
# ``...CommonCarryingAmount`` + ``...PreferredCarryingAmount`` + ``...OtherCarryingAmount``, so the
# total is read when filed and the three parts summed only when all three are filed (ID-02b).
# ``StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`` = ``StockholdersEquity``
# + ``MinorityInterest`` (CON-04, the same arc, order 20) is the identity MD-02 falls back to when no
# NCI balance is filed.
REDEEMABLE_NCI_CONCEPTS: tuple[str, ...] = (
    "us-gaap_RedeemableNoncontrollingInterestEquityCarryingAmount",
    "us-gaap_RedeemableNoncontrollingInterestEquityCommonCarryingAmount",
    "us-gaap_RedeemableNoncontrollingInterestEquityPreferredCarryingAmount",
    "us-gaap_RedeemableNoncontrollingInterestEquityOtherCarryingAmount",
)
# The short-term investments the MD-02/MD-03/MD-35 netting reads, when filed apart from cash
# (D6). ``AvailableForSaleSecuritiesDebtSecuritiesCurrent`` (named by the checklist) is declared in
# no taxonomy release of the 2020-2026 window (ID-20, ``sec_xcheck/taxonomy_absence_log.md``).
SHORT_TERM_INVESTMENT_CONCEPTS: tuple[str, ...] = (
    "us-gaap_ShortTermInvestments",
    "us-gaap_MarketableSecuritiesCurrent",
)

# -- T-159a: the registry substitutes ---------------------------------------------------------------
# The item's total under **another standard concept** (ID-02a precedence: these are read only
# after the item's own concepts found nothing), the component families whose sole filed member is
# the total, the ASC 320 classes that are summed, and the exact two-part sums. Every element
# below is a us-gaap element; the taxonomy definition each one rests on is quoted in
# ``docs/model_fixes.md`` (T-159a), and the elements the brief rejects are listed there too.

# Receivables: the current receivables total, under the three names the taxonomy gives it.
RECEIVABLES_SUBSTITUTES: tuple[str, ...] = (
    # "Accounts and notes receivable, net" -- trade receivables with the notes receivable a filer
    # presents beside them.
    G + "AccountsNotesAndLoansReceivableNetCurrent",
    # the same total for a filer that presents other receivables with trade receivables.
    G + "AccountsAndOtherReceivablesNetCurrent",
    # "Accounts receivable, net" -- the total of an **unclassified** balance sheet (no
    # current/noncurrent split), so it is the receivables and not a current-asset component.
    G + "AccountsReceivableNet",
)
# Cost of goods and services sold excluding the depreciation, depletion and amortization the filer
# presents on its own line: the total cost of sales, less the D&A (flagged ``cogs_excludes_da``).
COGS_SUBSTITUTES: tuple[str, ...] = (
    G + "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
)
# The inventory total, under the two names the taxonomy gives it.
INVENTORY_SUBSTITUTES: tuple[str, ...] = (
    G + "InventoryNetOfAllowancesCustomerAdvancesAndProgressBillings",
    G + "RetailRelatedInventoryMerchandise",
)
# The inventory component families (concept **prefixes**): a family covers its element and its
# variants (``InventoryFinishedGoods`` covers ``...NetOfReserves``). A component is accepted only
# when it is the sole filed member of one family.
INVENTORY_FAMILIES: tuple[str, ...] = (
    G + "EnergyRelatedInventory",
    G + "InventoryRawMaterialsAndSupplies",
    G + "InventoryFinishedGoods",
    G + "InventoryWorkInProcess",
    G + "InventoryPartsAndComponents",
    G + "OtherInventory",
)
# The redeemable NCI at fair value -- read only when no carrying amount is filed.
REDEEMABLE_NCI_SUBSTITUTES: tuple[str, ...] = (
    G + "RedeemableNoncontrollingInterestEquityFairValue",
)
# Gross interest expense, under the two names the taxonomy gives it. Never a net element
# (``InterestIncomeExpense...Net``): net interest is not interest expense.
INTEREST_EXPENSE_SUBSTITUTES: tuple[str, ...] = (
    G + "InterestExpenseOperating",
    G + "InterestExpenseBorrowings",
)
# The NCI in operating partnerships -- the minority interest a partnership-structured filer files.
MINORITY_INTEREST_SUBSTITUTES: tuple[str, ...] = (G + "MinorityInterestInOperatingPartnerships",)
# Net income available to common stockholders: the same quantity as parent net income when no
# preferred dividend is filed for the period (flagged ``net_income_to_common``).
NET_INCOME_SUBSTITUTES: tuple[str, ...] = (G + "NetIncomeLossAvailableToCommonStockholdersBasic",)
# The ASC 320 short-term-investment classes. They are **disjoint** (trading, available for sale,
# held to maturity, other), so several filed classes are summed -- each class counts once, its own
# total first (``TradingSecurities`` before its ``...Debt``/``...Equity`` components).
STI_CLASSES: tuple[tuple[str, ...], ...] = (
    (
        G + "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        G + "DebtSecuritiesAvailableForSaleExcludingAccruedInterestCurrent",
    ),
    (G + "TradingSecurities", G + "TradingSecuritiesDebt", G + "TradingSecuritiesEquity"),
    (G + "HeldToMaturitySecuritiesCurrent",),
    (G + "OtherShortTermInvestments",),
)
# The long-term debt components a filer presents instead of a tree total (MET-07 / ID-02b): they
# enter the sourced sum, never a free alternative. Each is a **node of the FASB 2026 tree**, so a
# component's filed children count when the component itself is not filed (the family rule, the
# same as the short-term map's terms): EA files ``SeniorLongTermNotes`` -- "Carrying value as of
# the balance sheet date of Notes with the highest claim on the assets of the issuer in case of
# bankruptcy or liquidation (with maturities initially due after one year or beyond the operating
# cycle, if longer), **excluding current portion**" -- and the gateway serves that element, not
# the total. The current portion of a component is never added to the long-term debt (N4): it
# belongs to the current-maturities term.
# The components are read **widest first** (a component that contains another comes before it), so
# a child is never added beside its parent.
LONG_TERM_DEBT_COMPONENTS: tuple[str, ...] = (
    G + "SeniorNotes",
    G + "LongTermNotesPayable",
    G + "ConvertibleLongTermNotesPayable",
    G + "OtherLongTermDebtNoncurrent",
)
# The children of the components: the sfp-cls arc's
# ``LongTermNotesAndLoans`` -> ``LongTermNotesPayable`` -> {``MediumtermNotesNoncurrent``,
# ``JuniorSubordinatedLongTermNotes``, ``SeniorLongTermNotes``, ``ConvertibleLongTermNotesPayable``,
# ``NotesPayableToBankNoncurrent``, ``OtherLongTermNotesPayable``} and the sfp-dbo arc's
# ``NotesPayable`` -> ``SeniorNotes`` -> {``SeniorNotesCurrent``, ``SeniorLongTermNotes``}. The
# current portions are the current-maturities term's (N4).
LONG_TERM_DEBT_COMPONENT_CHILDREN: dict[str, tuple[str, ...]] = {
    G + "SeniorNotes": (G + "SeniorLongTermNotes",),
    G + "LongTermNotesPayable": (
        G + "MediumtermNotesNoncurrent",
        G + "JuniorSubordinatedLongTermNotes",
        G + "SeniorLongTermNotes",
        G + "ConvertibleLongTermNotesPayable",
        G + "NotesPayableToBankNoncurrent",
        G + "OtherLongTermNotesPayable",
    ),
}
# The exact two-part sums (ID-02b: a map with a source states the identity). Both parts must be
# filed for the period, or the item stays empty.
DA_EXACT_SUM: tuple[str, str] = (G + "Depreciation", G + "AmortizationOfIntangibleAssets")
# The filed D&A aggregates (T-159a rule 4): a filer that presents the intangible amortization as
# a separate add-back line files them apart.
DA_AGGREGATE_CONCEPTS: tuple[str, ...] = (
    G + "DepreciationDepletionAndAmortization",
    G + "DepreciationAndAmortization",
    G + "DepreciationAmortizationAndAccretionNet",
)
# Audit F-A5 (E3's source, A1 5.6.1: the filer's caption names the concept): standard elements the
# registry does not read as D&A, accepted only as the **sole** cash-flow line captioned exactly as a D&A
# aggregate (``DA_CAPTION_ELEMENTS``) -- AEP's utility element, PEG's and IP's cost elements, and
# ``OtherDepreciationAndAmortization`` when it is a filer's only D&A line (ABNB, AMD, MRVL, WDAY ...).
DA_CAPTION_US_GAAP: frozenset[str] = frozenset(
    {
        G + "UtilitiesOperatingExpenseDepreciationAndAmortization",
        G + "CostDepreciationAmortizationAndDepletion",
        G + "CostOfGoodsAndServicesSoldDepreciationAndAmortization",
        G + "OtherDepreciationAndAmortization",
    }
)
# Audit F-A9: a separate face line of intangible amortization (a standard or an extension concept);
# never an impairment, financing-cost, inventory step-up, contract-cost or lease line (MD-01).
_INTANGIBLE_AMORT_RE = re.compile(
    r"^amortization of (acquisition[- ]related |acquired |purchased |other )?intangible",
    re.IGNORECASE,
)
_NOT_DA_AMORT_RE = re.compile(
    r"impairment|financing|debt|discount|premium|inventory|contract|lease|deferred", re.IGNORECASE
)
# Audit F-A4: the filer's own filed aggregate that calibrates a sum of D&A-nature face lines.
_DA_CALIBRATION_CONCEPTS: tuple[str, ...] = DA_AGGREGATE_CONCEPTS
# The tolerance of rule 4's HLT exception: the income statement and the cash flow carry the same
# fact, so the two values are the same to the unit.
_SAME_VALUE = 0.5
PRETAX_EXACT_SUM: tuple[str, str] = (
    G + "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
    G + "IncomeLossFromContinuingOperationsBeforeIncomeTaxesForeign",
)
# The preferred dividends that make "net income available to common" a **different** quantity
# from parent net income (N13).
PREFERRED_DIVIDEND_CONCEPTS: tuple[str, ...] = (
    G + "PreferredStockDividendsAndOtherAdjustments",
    G + "PreferredStockDividends",
)

# US-GAAP concept tags are stable across filers and are matched first; ``standard``
# and ``label`` hints are conservative fallbacks scoped to the same statement.
REGISTRY: dict[str, LineItem] = {
    "revenue": LineItem(
        "revenue",
        _INCOME,
        concepts=(
            "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
            "us-gaap_RevenueFromContractWithCustomerIncludingAssessedTax",
            # ASC-842 lease income -- a lessor's (e.g. a REIT/tower company)
            # dominant revenue stream, distinct from ASC-606 contract revenue
            # above; see docs/model_fixes.md, F2.
            "us-gaap_OperatingLeaseLeaseIncome",
        ),
        # Aggregate/"Total revenue(s)" concepts: if present, used alone, never
        # summed with the component concepts above (F2).
        # ``RegulatedAndUnregulatedOperatingRevenue`` is a utility's total operating
        # revenues -- the taxonomy parent of Regulated-/UnregulatedOperatingRevenue.
        # AWK/DTE/DUK/NEE/SRE/XEL tag their income-statement total only with it, so
        # without it their revenue resolved to NULL (T-102, docs/model_fixes.md).
        total_concepts=(
            "us-gaap_Revenues",
            "us-gaap_RevenuesNetOfInterestExpense",
            "us-gaap_RegulatedAndUnregulatedOperatingRevenue",
        ),
        sum_components=True,
        # ExcludingAssessedTax / IncludingAssessedTax are the SAME line
        # reported two ways (net of vs. gross of pass-through sales/excise
        # tax an agent collects on a principal's behalf, out of scope of the
        # ASC 606-10-32-2 transaction price) -- verified against live filings
        # (BF.B, STZ, TAP, PM: both concepts present for every period, same
        # value pair every time, e.g. STZ FY2019 "Net revenues" $29.8B vs.
        # "Revenues including excise taxes" $77.9B). Summing them as if
        # additive would nearly triple revenue. Excluding-tax is preferred:
        # it is the actual income-statement "Net sales"/"Net revenues" line;
        # Including-tax is the supplemental gross disclosure. See the F2
        # Sourcery follow-up in docs/model_fixes.md.
        synonym_groups=(
            (
                "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
                "us-gaap_RevenueFromContractWithCustomerIncludingAssessedTax",
            ),
        ),
        # ID-13/ID-15: DQC_0014 and DQC_0015 non-negative revenue concepts
        nonnegative=(
            "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
            "us-gaap_RevenueFromContractWithCustomerIncludingAssessedTax",
            "us-gaap_OperatingLeaseLeaseIncome",
        ),
    ),
    "cogs": LineItem(
        "cogs",
        _INCOME,
        # ID-20: CostOfGoodsSold and CostOfServices removed (sec_xcheck/taxonomy_absence_log.md)
        concepts=(
            "us-gaap_CostOfGoodsAndServicesSold",
            "us-gaap_CostOfRevenue",
        ),
        # ID-13: DQC_0015 non-negative concepts
        nonnegative=(
            "us-gaap_CostOfGoodsAndServicesSold",
            "us-gaap_CostOfRevenue",
            "us-gaap_CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
        ),
        # T-159a: the cost of sales without the D&A the filer presents on its own line -- the total
        # cost of sales, flagged ``cogs_excludes_da`` (the reader must know the D&A is not in it).
        substitute=Substitute(totals=COGS_SUBSTITUTES),
        concept_flags=(
            (
                "us-gaap_CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
                "cogs_excludes_da",
            ),
        ),
    ),
    "gross_profit": LineItem("gross_profit", _INCOME, concepts=("us-gaap_GrossProfit",)),
    "operating_income": LineItem(
        "operating_income", _INCOME, concepts=("us-gaap_OperatingIncomeLoss",)
    ),
    # ID-03 (T-148): parent earnings. ``NetIncomeLoss`` (attributable to the parent) first;
    # ``ProfitLoss`` (including the noncontrolling interest) less
    # ``NetIncomeLossAttributableToNoncontrollingInterest`` when only ``ProfitLoss`` is filed
    # (:meth:`Statements._net_income`). The "available to common" line is after preferred
    # dividends and is never parent net income (N13): T-096's WAT fallback is gone.
    "net_income": LineItem(
        "net_income",
        _INCOME,
        concepts=("us-gaap_NetIncomeLoss", "us-gaap_ProfitLoss"),
        resolver="net_income",
        # T-159a: "net income available to common" -- the same quantity as parent net income when
        # no preferred dividend is filed for the period (flagged ``net_income_to_common``).
        concept_flags=(
            (
                "us-gaap_NetIncomeLossAvailableToCommonStockholdersBasic",
                "net_income_to_common",
            ),
        ),
    ),
    "pretax_income": LineItem(
        "pretax_income",
        _INCOME,
        concepts=(
            "us-gaap_IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "us-gaap_IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        ),
        standard=("PretaxIncomeLoss",),
        # T-159a: the exact two-part sum -- the domestic and the foreign pre-tax income of a filer
        # that splits them. Both parts must be filed for the period.
        substitute=Substitute(sums=(PRETAX_EXACT_SUM,)),
    ),
    # ID-02b / D-06(c) (T-148): the total, else current + deferred when both are on the statement
    # face (B6's definition of IncomeTaxExpenseBenefit); never a single component, so no
    # ``standard`` match (the gateway maps CurrentIncomeTaxExpenseBenefit to "IncomeTaxes" too).
    "income_tax": LineItem(
        "income_tax",
        _INCOME,
        concepts=("us-gaap_IncomeTaxExpenseBenefit",),
        resolver="income_tax",
    ),
    "interest_expense": LineItem(
        "interest_expense",
        _INCOME,
        concepts=(
            "us-gaap_InterestExpense",
            "us-gaap_InterestExpenseNonoperating",
            "us-gaap_InterestAndDebtExpense",
            # N23 (T-148): a filer that files only the debt-specific variant
            "us-gaap_InterestExpenseDebt",
        ),
        # ID-13: DQC_0015 lists these two in every release of the window
        nonnegative=(
            "us-gaap_InterestAndDebtExpense",
            "us-gaap_InterestExpenseDebt",
            "us-gaap_InterestExpenseBorrowings",
        ),
        # T-159a: gross interest expense under the other names the taxonomy gives it. Never a net
        # element (``InterestIncomeExpense...Net``): net interest is not interest expense.
        substitute=Substitute(totals=INTEREST_EXPENSE_SUBSTITUTES),
    ),
    "depreciation_amortization": LineItem(
        "depreciation_amortization",
        _CASHFLOW,
        concepts=(
            "us-gaap_DepreciationDepletionAndAmortization",
            "us-gaap_DepreciationAmortizationAndAccretionNet",
            "us-gaap_DepreciationAndAmortization",
        ),
        unread_flags=(("us-gaap_Depreciation", "depreciation_only_not_da"),),
        concept_flags=(("us-gaap_DepreciationAndAmortization", "da_element_nonproduction"),),
        nonnegative=(
            "us-gaap_DepreciationDepletionAndAmortization",
            "us-gaap_DepreciationAndAmortization",
        ),
        # C1 (T-151 part 1): when none of the three concepts is filed, the sourced sum of the
        # filed D&A-nature children (``DA_SOURCED_CHILDREN``) or a refusal.
        resolver="da",
    ),
    "operating_cash_flow": LineItem(
        "operating_cash_flow",
        _CASHFLOW,
        concepts=(
            "us-gaap_NetCashProvidedByUsedInOperatingActivities",
            "us-gaap_NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        ),
    ),
    "capital_expenditure": LineItem(
        "capital_expenditure",
        _CASHFLOW,
        resolver="capex",
        concepts=(
            "us-gaap_PaymentsToAcquirePropertyPlantAndEquipment",
            "us-gaap_PaymentsForCapitalImprovements",
            "us-gaap_PaymentsToAcquireMachineryAndEquipment",
            "us-gaap_PaymentsToAcquireFurnitureAndFixtures",
            "us-gaap_PaymentsToAcquireOtherPropertyPlantAndEquipment",
            "us-gaap_PaymentsToExploreAndDevelopOilAndGasProperties",
            "us-gaap_PaymentsToAcquireOilAndGasEquipment",
            "us-gaap_PaymentsToAcquireOilAndGasPropertyAndEquipment",
            "us-gaap_PaymentsToAcquireProductiveAssets",
            "us-gaap_PaymentsToAcquireOtherProductiveAssets",
            "us-gaap_PaymentsForProceedsFromProductiveAssets",
        ),
        label_fallback=r"\bcapital expenditures?\b",
        label_fallback_exclude=(
            r"\b(accrued|unpaid|not (yet )?paid|incurred|included|financed|non-?cash|payable"
            r"|change|increase|decrease|proceeds|reimburse\w*|sales?)\b"
        ),
        nonnegative=(
            "us-gaap_PaymentsToAcquirePropertyPlantAndEquipment",
            "us-gaap_PaymentsForCapitalImprovements",
            "us-gaap_PaymentsToAcquireMachineryAndEquipment",
            "us-gaap_PaymentsToAcquireFurnitureAndFixtures",
            "us-gaap_PaymentsToAcquireOtherPropertyPlantAndEquipment",
            "us-gaap_PaymentsToExploreAndDevelopOilAndGasProperties",
            "us-gaap_PaymentsToAcquireOilAndGasEquipment",
            "us-gaap_PaymentsToAcquireOilAndGasPropertyAndEquipment",
            "us-gaap_PaymentsToAcquireProductiveAssets",
            "us-gaap_PaymentsToAcquireOtherProductiveAssets",
            "us-gaap_PaymentsToAcquireRealEstate",
            "us-gaap_PaymentsToAcquireRealEstateHeldForInvestment",
            "us-gaap_PaymentsToDevelopRealEstateAssets",
        ),
    ),
    "stock_based_compensation": LineItem(
        "stock_based_compensation",
        _CASHFLOW,
        # ID-20: ShareBasedCompensationExpense removed (sec_xcheck/taxonomy_absence_log.md)
        concepts=(
            "us-gaap_ShareBasedCompensation",
            "us-gaap_AllocatedShareBasedCompensationExpense",
        ),
        standard=("StockBasedCompensationExpense",),
    ),
    # Point-in-time common shares outstanding, for market cap (price x shares at the
    # period-end date). Not always present -- callers fall back to `diluted_shares`.
    # Never `CommonStockSharesIssued` (T-132): it includes treasury stock, so it overstates a
    # buyback-heavy filer's count (PG: 4.009B issued vs 2.324B outstanding) -- an absent concept
    # is a missing count, which is honest, not an inflated one. The cover-page count
    # (`Statements.cover_shares`) is preferred over this balance-sheet figure.
    "shares_outstanding": LineItem(
        "shares_outstanding",
        _BALANCE,
        concepts=("us-gaap_CommonStockSharesOutstanding",),
        standard=("SharesYearEnd",),
        nonnegative=("us-gaap_CommonStockSharesOutstanding",),
    ),
    # Weighted-average diluted share count from the income statement -- always
    # reported; use for per-share metrics and as the shares_outstanding fallback.
    # Diluted only: the basic line sits earlier in the statement and would win the
    # first-match lookup, so it is intentionally not listed here.
    "diluted_shares": LineItem(
        "diluted_shares",
        _INCOME,
        concepts=("us-gaap_WeightedAverageNumberOfDilutedSharesOutstanding",),
        standard=("SharesFullyDilutedAverage",),
        nonnegative=("us-gaap_WeightedAverageNumberOfDilutedSharesOutstanding",),
    ),
    # As-filed diluted EPS -- a GAAP-required disclosure, independent of
    # `diluted_shares` (net income divided by it, as reported by the filer
    # itself). Cross-checking `net_income / diluted_shares` against this value
    # is a same-filing, no-history-needed scale/tagging-defect signal (see
    # fundamental_agent.db.detect_share_scale_factors, docs/model_fixes.md F1).
    "eps_diluted": LineItem("eps_diluted", _INCOME, concepts=("us-gaap_EarningsPerShareDiluted",)),
    # EPS's own numerator: net income *available to common*, i.e. after preferred
    # dividends. Only the share-scale EPS cross-check reads it (T-103): with preferred
    # dividends, plain net income / EPS is not a share count (ALL 2023Q3: net income
    # -$5M, available to common -$41M).
    "net_income_to_common": LineItem(
        "net_income_to_common",
        _INCOME,
        concepts=(
            "us-gaap_NetIncomeLossAvailableToCommonStockholdersDiluted",
            "us-gaap_NetIncomeLossAvailableToCommonStockholdersBasic",
        ),
    ),
    "total_assets": LineItem(
        "total_assets", _BALANCE, concepts=("us-gaap_Assets",), nonnegative=("us-gaap_Assets",)
    ),
    "current_assets": LineItem(
        "current_assets",
        _BALANCE,
        concepts=("us-gaap_AssetsCurrent",),
        nonnegative=("us-gaap_AssetsCurrent",),
    ),
    "total_liabilities": LineItem(
        "total_liabilities",
        _BALANCE,
        concepts=("us-gaap_Liabilities",),
        nonnegative=("us-gaap_Liabilities",),
    ),
    "current_liabilities": LineItem(
        "current_liabilities",
        _BALANCE,
        concepts=("us-gaap_LiabilitiesCurrent",),
        nonnegative=("us-gaap_LiabilitiesCurrent",),
    ),
    "equity": LineItem(
        "equity",
        _BALANCE,
        concepts=(
            "us-gaap_StockholdersEquity",
            "us-gaap_StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ),
        label_contains=("total stockholders", "total shareholders"),
    ),
    "cash": LineItem(
        "cash",
        _BALANCE,
        concepts=(
            "us-gaap_CashAndCashEquivalentsAtCarryingValue",
            "us-gaap_CashCashEquivalentsAndShortTermInvestments",
            "us-gaap_CashAndDueFromBanks",
        ),
        nonnegative=(
            "us-gaap_CashAndCashEquivalentsAtCarryingValue",
            "us-gaap_CashCashEquivalentsAndShortTermInvestments",
            "us-gaap_CashAndDueFromBanks",
            # T-159a: the bare ``Cash`` line the substitute reads (ID-13: a DQC_0015 element).
            "us-gaap_Cash",
        ),
        resolver="cash",
    ),
    "short_term_investments": LineItem(
        "short_term_investments",
        _BALANCE,
        # ID-20: AvailableForSaleSecuritiesCurrent removed (sec_xcheck/taxonomy_absence_log.md)
        concepts=SHORT_TERM_INVESTMENT_CONCEPTS,
        nonnegative=(
            *SHORT_TERM_INVESTMENT_CONCEPTS,
            # T-159a: the ASC 320 classes the resolver now reads (ID-13: the DQC_0015 elements).
            "us-gaap_AvailableForSaleSecuritiesDebtSecuritiesCurrent",
            "us-gaap_DebtSecuritiesAvailableForSaleExcludingAccruedInterestCurrent",
            "us-gaap_TradingSecurities",
            "us-gaap_TradingSecuritiesDebt",
            "us-gaap_TradingSecuritiesEquity",
            "us-gaap_HeldToMaturitySecuritiesCurrent",
            "us-gaap_OtherShortTermInvestments",
        ),
        # T-159a: the ASC 320 classes (trading, available for sale, held to maturity, other). They
        # are disjoint, so several filed classes are summed, each counted once.
        substitute=Substitute(classes=STI_CLASSES),
    ),
    # NCI (MD-02, MD-35; D6): the NCI in equity, the redeemable NCI between liabilities and equity,
    # and the equity-including-NCI total CON-04's identity needs. See the constants above.
    "minority_interest": LineItem(
        "minority_interest",
        _BALANCE,
        concepts=("us-gaap_MinorityInterest",),
        # T-159a: the NCI in operating partnerships -- the minority interest a filer with
        # partnerships structures files.
        substitute=Substitute(totals=MINORITY_INTEREST_SUBSTITUTES),
    ),
    "redeemable_nci": LineItem(
        "redeemable_nci",
        _BALANCE,
        concepts=REDEEMABLE_NCI_CONCEPTS,
        resolver="redeemable_nci",
    ),
    "equity_incl_nci": LineItem(
        "equity_incl_nci",
        _BALANCE,
        concepts=(
            "us-gaap_StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ),
    ),
    "inventory": LineItem(
        "inventory",
        _BALANCE,
        concepts=("us-gaap_InventoryNet",),
        nonnegative=(
            "us-gaap_InventoryNet",
            "us-gaap_InventoryNetOfAllowancesCustomerAdvancesAndProgressBillings",
            "us-gaap_RetailRelatedInventoryMerchandise",
        ),
        # T-159a: the total under the other names the taxonomy gives it, else the sole filed member
        # of one component family (a component is never the total: it is one only when it is all
        # the filing has -- XOM's two components leave the item empty).
        substitute=Substitute(totals=INVENTORY_SUBSTITUTES, families=INVENTORY_FAMILIES),
    ),
    "receivables": LineItem(
        "receivables",
        _BALANCE,
        concepts=(
            "us-gaap_AccountsReceivableNetCurrent",
            "us-gaap_ReceivablesNetCurrent",
        ),
        nonnegative=(
            "us-gaap_AccountsReceivableNetCurrent",
            "us-gaap_ReceivablesNetCurrent",
            "us-gaap_AccountsNotesAndLoansReceivableNetCurrent",
            "us-gaap_AccountsAndOtherReceivablesNetCurrent",
            "us-gaap_AccountsReceivableNet",
        ),
        # T-159a: the same total under the other names the taxonomy gives it.
        substitute=Substitute(totals=RECEIVABLES_SUBSTITUTES),
    ),
    # MET-07 / ID-02b (T-148). The debt resolvers are composite (:meth:`Statements._long_term_debt`,
    # :meth:`Statements._short_term_debt`, :meth:`Statements.total_debt`); the concepts below are
    # the exact, single-line ones in the spec's order -- narrowest first (ID-02a), a line that
    # includes current maturities (``LongTermDebt``) before the combined debt-and-lease elements,
    # which are used only when they are the only long-term line.
    "long_term_debt": LineItem(
        "long_term_debt",
        _BALANCE,
        concepts=(
            "us-gaap_LongTermDebtNoncurrent",
            "us-gaap_LongTermDebt",
            "us-gaap_LongTermDebtAndCapitalLeaseObligations",
            "us-gaap_LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
        ),
        nonnegative=(
            "us-gaap_LongTermDebtNoncurrent",
            "us-gaap_LongTermDebt",
            "us-gaap_LongTermDebtAndCapitalLeaseObligations",
            "us-gaap_LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
            # T-159a: the debt components the sourced sum now reads (ID-13: DQC_0015 elements).
            "us-gaap_SeniorNotes",
            "us-gaap_ConvertibleLongTermNotesPayable",
            "us-gaap_LongTermNotesPayable",
            "us-gaap_OtherLongTermDebtNoncurrent",
            "us-gaap_SeniorLongTermNotes",
            "us-gaap_MediumtermNotesNoncurrent",
            "us-gaap_JuniorSubordinatedLongTermNotes",
            "us-gaap_NotesPayableToBankNoncurrent",
            "us-gaap_OtherLongTermNotesPayable",
        ),
        resolver="long_term_debt",
    ),
    "short_term_debt": LineItem(
        "short_term_debt",
        _BALANCE,
        concepts=(*(c for _term, cs in SHORT_TERM_DEBT_MAP for c in cs), SHORT_TERM_DEBT_LAST),
        nonnegative=(
            *(c for _term, cs in SHORT_TERM_DEBT_MAP for c in cs),
            SHORT_TERM_DEBT_LAST,
            # T-159a: the calculation children the map now reads (ID-13: DQC_0015 elements).
            *(c for kids in SHORT_TERM_DEBT_CHILDREN.values() for c in kids),
        ),
        resolver="short_term_debt",
    ),
}


@dataclass(frozen=True)
class CoverShares:
    """One entry of the gateway's ``data["cover"]["shares_outstanding"]``: the filing's own
    cover-page count of shares outstanding on ``as_of_date`` (T-132). ``class_member`` is ``""``
    for a single-class filer's count or a filed total, else the share class it counts."""

    value: float
    as_of_date: str
    class_member: str = ""


def _cover_shares(payload: dict[str, Any]) -> list[CoverShares]:
    cover = payload.get("cover")
    entries = cover.get("shares_outstanding") if isinstance(cover, dict) else None
    out: list[CoverShares] = []
    for item in entries or []:
        if not isinstance(item, dict):
            continue
        value, as_of = _numeric(item.get("value")), item.get("as_of_date")
        if value is None or value <= 0 or not isinstance(as_of, str) or not as_of:
            continue  # never store a count the filing did not date
        member = item.get("class_member")
        out.append(CoverShares(value, as_of[:10], str(member) if member else ""))
    return out


def _restore_dropped_cells(raw: dict[str, list[dict[str, Any]]], payload: dict[str, Any]) -> None:
    """Audit (reviewer B, 2026-10-25): undo a gateway drop the filing's own facts disprove.

    The gateway nulls a face cell it cannot match to a non-dimensional filed fact (T-042,
    ``no_filed_nondimensional_fact``) and records the original in ``corrections``. The audit found
    real face rows dropped although the SEC holds the fact (CAT's 46.1 B and TMUS's 70.1 B long-term
    debt, VLO's capex, NDAQ's COGS, YUM's revenue; the filers' own presentation linkbases list them
    on the face). A dropped cell is restored -- its original displayed value back in the row -- only
    when the filing's own non-dimensional filed fact of the same concept, or of another concept of
    the same registry item (not debt, cash or short-term investments), for the column's period
    equals it in magnitude within 0.5 % (T-159c rule B). Never a dimensional row; never a guess."""
    facts: dict[tuple[str, str], list[tuple[str | None, float]]] = {}
    for fact in _filed_facts(payload):
        facts.setdefault((fact.concept.replace(":", "_"), fact.period_end), []).append(
            (fact.period_start, fact.value)
        )
    # the concepts of the same registry item: a dropped row of one is proven by another's filed fact
    # (YUM's ``RevenueFromContract...`` row by its ``Revenues`` fact, COGS by ``CostOfRevenue``).
    # Never the debt, cash or short-term investment items: there an equal fact may be another line.
    family: dict[str, set[str]] = {}
    for name, item in REGISTRY.items():
        if name in ("long_term_debt", "short_term_debt", "cash", "short_term_investments"):
            continue
        members = {*item.concepts, *item.total_concepts, *item.fallback_concepts}
        for concept_name in members:
            family.setdefault(concept_name, set()).update(members)
    for corr in payload.get("corrections") or []:
        if not isinstance(corr, dict) or corr.get("corrected") is not None:
            continue
        statement, concept, column = corr.get("statement"), corr.get("concept"), corr.get("column")
        original = _numeric(corr.get("original"))
        if statement not in raw or not concept or not column or original is None or original == 0:
            continue
        period = _parse_period(str(column))
        if period is None:
            continue
        end = str(column)[:10]
        # The same concept within 0.5 % (T-159c rule B); another concept of the item only when equal:
        # a near value there is another line (BA's cost of products vs its total cost of revenue,
        # a combined filer's subsidiary row -- AEE's 993 M vs the parent's 994 M), not a proof.
        candidates = [
            (member == str(concept), *c)
            for member in sorted(family.get(str(concept), set()) | {str(concept)})
            for c in facts.get((member, end), [])
        ]
        matched = None
        for same, start, value in candidates:
            if statement == "balance_sheet":
                if start is not None:
                    continue
            else:
                if start is None:
                    continue
                days = (date.fromisoformat(end) - date.fromisoformat(start[:10])).days
                tag = period.tag
                if tag == "FY" and not 340 <= days <= 380:
                    continue
                if tag.startswith("Q") and not 75 <= days <= 120:
                    continue
                if tag == "YTD" and not 120 < days < 340:
                    continue
            tolerance = 0.005 if same else 1e-9
            if abs(abs(value) - abs(original)) <= tolerance * abs(value):
                matched = value
                break
        if matched is None:
            continue
        rows = [r for r in raw[statement] if r.get("concept") == concept and not r.get("dimension")]
        if rows:
            if _numeric(rows[0].get(column)) is None:
                rows[0][column] = original
                rows[0].setdefault("audit_restored", []).append(column)
        else:
            raw[statement].append(
                {
                    "concept": concept,
                    "label": str(concept),
                    "standard_concept": None,
                    "abstract": False,
                    "dimension": False,
                    column: matched,
                    "audit_restored": [column],
                }
            )


def _filed_facts(payload: dict[str, Any]) -> tuple[FiledFact, ...]:
    """The gateway's ``filed_facts`` list (T-159b), keeping only well-formed numeric facts."""
    out: list[FiledFact] = []
    for entry in payload.get("filed_facts") or []:
        if not isinstance(entry, dict):
            continue
        concept = entry.get("concept")
        value = _numeric(entry.get("value"))
        period_end = entry.get("period_end")
        if not isinstance(concept, str) or not concept or value is None:
            continue
        if not isinstance(period_end, str) or not period_end:
            continue
        period_start = entry.get("period_start")
        out.append(
            FiledFact(
                concept=concept,
                value=float(value),
                unit=str(entry.get("unit") or ""),
                period_start=str(period_start) if period_start else None,
                period_end=period_end[:10],
            )
        )
    return tuple(out)


def _filed_facts_error(payload: dict[str, Any]) -> str | None:
    """Why the gateway's filed-fact read failed, if it did (``filed_facts_error``)."""
    value = payload.get("filed_facts_error")
    return str(value) if value else None


def _cover_errors(payload: dict[str, Any]) -> list[str]:
    """Why the gateway's cover read failed, if it did. The final contract (portfolio-data-mining
    PR #48) reports it as ``data["cover"]["error"]`` (``null`` on success); an earlier build put a
    ``{"statement": "cover", "error"}`` entry in ``reconciliation_errors``, still read for
    compatibility."""
    errors: list[str] = []
    cover = payload.get("cover")
    if isinstance(cover, dict) and cover.get("error"):
        errors.append(str(cover["error"]))
    for entry in payload.get("reconciliation_errors") or []:
        if isinstance(entry, dict) and entry.get("statement") == "cover" and entry.get("error"):
            errors.append(str(entry["error"]))
    return errors


@dataclass(frozen=True)
class TotalRebuild:
    """How :meth:`Statements.rebuild_total` read one column (T-140): the anchor row it found,
    the in-between rows it would subtract, and the value -- or, when it declined, why."""

    anchor_label: str
    anchor_value: float
    between: tuple[float, ...]
    value: float | None
    refusal: str | None = None
    # True when a row between exceeded the ceiling and the gateway's own record of the dropped
    # total (``corrections[].original``) equals the value to the dollar: two derivations agree.
    corroborated: bool = False


MIN_SUMMED = 2  # a sum needs at least two components

# Composite resolvers: ``LineItem.resolver`` -> the Statements method that resolves the item.
_COMPOSITE = {
    "net_income": "_net_income",
    "income_tax": "_income_tax",
    "long_term_debt": "_long_term_debt",
    "short_term_debt": "_short_term_debt",
    "cash": "_cash",
    "capex": "_capex",
    "da": "_depreciation_amortization",
    "redeemable_nci": "_redeemable_nci",
}


# The rule IDs of every resolver map and tier (T-151 part 3, D13 (c)): each map and each composite
# resolver cites the checklist rule it implements, so a map without a source is visible. The test
# ``tests/test_traceability.py`` fails when a name in :data:`RESOLVER_MAPS` is missing here.
RESOLVER_RULE_IDS: dict[str, tuple[str, ...]] = {
    "revenue.total_concepts": ("CON-11", "B4_DQC_Revenue_Guidance"),
    "revenue.sum_components": ("ID-02b", "B4_DQC_Revenue_Guidance"),
    "revenue.synonym_groups": ("ID-02a", "B4_DQC_Revenue_Guidance"),
    "net_income": ("ID-03", "CON-19", "N13"),
    "income_tax": ("ID-02b", "D-06(c)"),
    "cash": ("ID-02b", "B4_DQC_Revenue_Guidance", "D-06(a)"),
    "capex": ("MET-08", "APP-04b"),
    "da": ("MD-01", "ID-02b"),
    "long_term_debt": ("MET-07", "ID-02a", "ID-02b"),
    "short_term_debt": ("MET-07", "D-06(d)", "N4"),
    "redeemable_nci": ("MD-02", "CON-04", "D6"),
    "SHORT_TERM_DEBT_MAP": ("MET-07", "D-06(d)", "N4"),
    "SHORT_TERM_DEBT_LAST": ("MET-07", "D-06(d)"),
    "INCLUDES_CURRENT_MATURITIES": ("N4",),
    "LONG_TERM_DEBT_NONCURRENT_CHILDREN": ("ID-02b", "MET-07"),
    "LONG_TERM_DEBT_CHILDREN": ("ID-02b", "MET-07", "N4"),
    "DA_SOURCED_CHILDREN": ("ID-02b", "MD-01"),
    "REDEEMABLE_NCI_CONCEPTS": ("MD-02", "CON-04", "D6"),
    "SHORT_TERM_INVESTMENT_CONCEPTS": ("MD-02", "MD-03", "MD-35", "D6"),
    "RECEIVABLES_SUBSTITUTES": ("ID-02a", "MD-21"),
    "COGS_SUBSTITUTES": ("ID-02a", "MD-06"),
    "INVENTORY_SUBSTITUTES": ("ID-02a", "MD-20"),
    "INVENTORY_FAMILIES": ("ID-02b", "MD-20"),
    "REDEEMABLE_NCI_SUBSTITUTES": ("ID-02a", "MD-02"),
    "INTEREST_EXPENSE_SUBSTITUTES": ("ID-02a", "MD-17"),
    "MINORITY_INTEREST_SUBSTITUTES": ("ID-02a", "MD-02"),
    "NET_INCOME_SUBSTITUTES": ("ID-03", "N13"),
    "STI_CLASSES": ("ID-02b", "MD-03", "MD-35", "D6"),
    "LONG_TERM_DEBT_COMPONENTS": ("ID-02b", "MET-07", "N4"),
    "SHORT_TERM_DEBT_CHILDREN": ("ID-02b", "MET-07", "N4"),
    "SHORT_TERM_DEBT_ROOTS": ("ID-02b", "MET-07", "N4"),
    "SHORT_TERM_DEBT_CONTAINERS": ("ID-02b", "MET-07", "N4"),
    "DA_EXACT_SUM": ("ID-02b", "MD-01"),
    "DA_AGGREGATE_CONCEPTS": ("ID-02b", "MD-01"),
    "PRETAX_EXACT_SUM": ("ID-02b", "MD-30"),
    "PREFERRED_DIVIDEND_CONCEPTS": ("ID-03", "N13"),
    "_NCI_INCOME_CONCEPTS": ("ID-03", "CON-19"),
    "_COMPOSITE": ("ID-02b",),
}

# Every map or tier constant a reader must find in :data:`RESOLVER_RULE_IDS`.
RESOLVER_MAPS: tuple[str, ...] = (
    "SHORT_TERM_DEBT_MAP",
    "SHORT_TERM_DEBT_LAST",
    "INCLUDES_CURRENT_MATURITIES",
    "LONG_TERM_DEBT_NONCURRENT_CHILDREN",
    "LONG_TERM_DEBT_CHILDREN",
    "DA_SOURCED_CHILDREN",
    "REDEEMABLE_NCI_CONCEPTS",
    "SHORT_TERM_INVESTMENT_CONCEPTS",
    "RECEIVABLES_SUBSTITUTES",
    "COGS_SUBSTITUTES",
    "INVENTORY_SUBSTITUTES",
    "INVENTORY_FAMILIES",
    "REDEEMABLE_NCI_SUBSTITUTES",
    "INTEREST_EXPENSE_SUBSTITUTES",
    "MINORITY_INTEREST_SUBSTITUTES",
    "NET_INCOME_SUBSTITUTES",
    "STI_CLASSES",
    "LONG_TERM_DEBT_COMPONENTS",
    "SHORT_TERM_DEBT_CHILDREN",
    "SHORT_TERM_DEBT_ROOTS",
    "SHORT_TERM_DEBT_CONTAINERS",
    "DA_EXACT_SUM",
    "DA_AGGREGATE_CONCEPTS",
    "PRETAX_EXACT_SUM",
    "PREFERRED_DIVIDEND_CONCEPTS",
    "_NCI_INCOME_CONCEPTS",
    "_COMPOSITE",
    "net_income",
    "income_tax",
    "cash",
    "capex",
    "da",
    "long_term_debt",
    "short_term_debt",
    "redeemable_nci",
    "revenue.total_concepts",
    "revenue.sum_components",
    "revenue.synonym_groups",
)


@dataclass(frozen=True)
class Resolution:
    """What :meth:`Statements.resolve` read for one line item and one period (T-148).

    ``concept`` -- the concept the value came from (``a + b`` for a map; ``None`` when it was
    derived from the statement's layout); ``how`` -- the tier (``concept``, ``standard``,
    ``label``, ``total``, ``sum``, ``map``, ``fallbackN``, ``label_fallback``, ``derived``,
    ``none_filed``); ``flags`` -- what a reader must know about the value (``sign_unverified``,
    ``wrong_sign_rejected``, ``partial_map``, ``includes_leases``, ``capture_failure``, ...).
    """

    value: float | None
    concept: str | None = None
    how: str | None = None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Capital:
    """The balance-sheet building blocks at one instant column (T-151 part 2).

    ``source`` names where the column came from: ``"filing"`` (the filing's own comparative),
    ``"prior_10k"`` / ``"prior_10q"`` (a stored earlier filing of the same company), or ``None``
    when the filing was resolved on its own (a hermetic fixture). Each item carries its own
    :class:`Resolution` (value, concept, how, flags) so a metric can record provenance."""

    source: str | None
    total_assets: Resolution
    equity: Resolution
    cash: Resolution
    short_term_investments: Resolution
    minority_interest: Resolution
    redeemable_nci: Resolution
    equity_incl_nci: Resolution
    current_assets: Resolution
    current_liabilities: Resolution
    debt: Resolution
    short_term_debt: Resolution


@dataclass(frozen=True)
class FiledFact:
    """One non-dimensional fact as the gateway's ``filed_facts`` payload carries it (T-159b).

    ``concept`` is spelled with a colon (``us-gaap:LongTermDebt``); the registry spells the same
    element with an underscore. ``value`` is the filed value (the sign the filing carries), and
    ``period_start`` is ``None`` for an instant.
    """

    concept: str
    value: float
    unit: str
    period_start: str | None
    period_end: str


@dataclass
class Statements:
    """A parsed ``financials`` payload."""

    raw: dict[str, list[dict[str, Any]]]
    periods: list[Period] = field(default_factory=list)
    # T-118: the gateway's own `data["corrections"]` -- entries of
    # `{"concept", "column", "original", "corrected", "rule"}` recording a value the
    # gateway derived (e.g. from a synthesized-total defect) rather than returning as
    # filed. Empty for a payload from a gateway version that predates this, or for any
    # filing the gateway found nothing to correct in. Consumed by :func:`iter_facts` so
    # the derived value is marked in `financial_facts`, not stored indistinguishably
    # from a filed fact.
    corrections: list[dict[str, Any]] = field(default_factory=list)
    # T-132: the cover-page share count(s), and why there are none when the gateway's cover read
    # failed (it fails in isolation, so the statements above are still validated).
    cover_shares: list[CoverShares] = field(default_factory=list)
    cover_error: str | None = None
    # T-159c: the filing's own non-dimensional filed facts (T-159b's ``filed_facts``), read only
    # when the statements resolve an item to nothing; and why the list is empty when the gateway's
    # read failed (``filed_facts_error``).
    filed_facts: tuple[FiledFact, ...] = ()
    filed_facts_error: str | None = None
    # Audit (reviewer B, 2026-10-25): the debt lines the sector review verified as omitted for this
    # filing (``audit/debt_overrides.json``) -- added to the total debt (MET-07 "the filed debt lines").
    audit_debt_add: tuple[str, ...] = ()
    # MET-08 / APP-04b (T-148): equity REITs treat real estate acquisitions and developments as capex
    reit: bool = False
    # T-151 part 2 (D3/D4): the company's GICS sector and sub-industry -- today's snapshot (L-03),
    # read for APP-04c (REIT tax) and PER-14 (CECL: banks and lenders). The Article type is T-071's;
    # the code cannot know it here (D13). ``None`` in a hermetic fixture.
    sector: str | None = None
    sub_industry: str | None = None
    # T-151 part 3 (D14): the filing's accession, recorded in every metric's ``provenance`` block.
    # Set by the pipeline / recompute (:func:`fundamental_agent.db.attach_context`).
    accession: str | None = None
    # T-151 part 2 (MET-01/02, MD-02/35/38): the capital at the prior fiscal year end, and (a 10-Q
    # only) at the prior year's same quarter. Resolved by ``fundamental_agent.db.prior_capital``
    # because the second reads another filing's stored facts; ``None`` in a hermetic fixture, where
    # the filing's own comparative column is used instead (:meth:`prior_year_capital`).
    prior_capital: Capital | None = None
    prior_quarter_capital: Capital | None = None
    # The flags of the resolution in progress (see :meth:`resolve`); never part of the payload.
    _flags: list[str] = field(default_factory=list, init=False, repr=False, compare=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, reit: bool = False) -> Statements:
        raw = {key: list(payload.get(key) or []) for key in STATEMENT_KEYS}
        _restore_dropped_cells(raw, payload)
        # T-159c: a bare date on the income statement or the cash-flow statement is an **untagged
        # duration column** (the 16/24-week filers T-159b's F0 fix keeps), not an instant; only
        # the balance sheet's bare date is an instant. The key is shared, so both periods are kept
        # and the statement decides which one a reader gets.
        seen: dict[tuple[str, str], Period] = {}
        for statement, rows in raw.items():
            for row in rows:
                for column in row:
                    parsed = _parse_period(str(column))
                    if parsed is None:
                        continue
                    if parsed.is_instant and statement != "balance_sheet":
                        parsed = Period(key=parsed.key, date=parsed.date, tag="")
                    seen[(parsed.key, parsed.tag)] = parsed
        periods = sorted(seen.values(), key=lambda p: (p.date, p.tag))
        corrections = [c for c in payload.get("corrections") or [] if isinstance(c, dict)]
        cover_errors = _cover_errors(payload)
        return cls(
            raw=raw,
            periods=periods,
            corrections=corrections,
            cover_shares=_cover_shares(payload),
            cover_error="; ".join(cover_errors) or None,
            filed_facts=_filed_facts(payload),
            filed_facts_error=_filed_facts_error(payload),
            reit=reit,
            audit_debt_add=tuple(payload.get("_audit_debt_add") or ()),
        )

    def fy_periods(self) -> list[Period]:
        return [p for p in self.periods if p.is_fy]

    def quarter_periods(self) -> list[Period]:
        return [p for p in self.periods if p.is_quarter]

    def untagged_periods(self) -> list[Period]:
        """The duration columns the gateway carries without a ``(Qx)``/``(YTD)``/``(FY)`` tag
        (T-159c: the 16/24-week filers). The statement that carries the key decides its kind."""
        return [p for p in self.periods if not p.is_instant and not p.tag]

    @property
    def is_empty(self) -> bool:
        """Whether the payload carries **no statement row at all**.

        A gateway payload that comes back with ``success: true`` and empty statements is a
        **service** failure, not a property of the filing (reviewer A, P3 round 1, 2026-10-20:
        AEP ``0000004904-23-000072``): the same filing served in full minutes later. The
        resolvers name it, so the classifier can call it retryable.
        """
        return not any(self.raw.values())

    def instant_periods(self) -> list[Period]:
        return [p for p in self.periods if p.is_instant]

    def latest_fy(self) -> Period | None:
        fy = self.fy_periods()
        return fy[-1] if fy else None

    def prior_of(self, period: Period) -> Period | None:
        """The same period one year earlier, if the payload carries it (PER-08).

        A column is the prior of *period* only when its end date is one year earlier: within
        [364, 371] days and rounding to the same month end as the date a calendar year before
        (:func:`fundamental_agent.fiscal.is_year_apart`). A 52/53-week filer's year-ago end sits
        days off the calendar one and still matches; a column 345 or 385 days away -- a changed
        fiscal year, a stray comparative -- no longer does (it was a +-20-day window around 365).

        A quarter is paired with the quarter column that ended a year before it (T-140): the
        gateway's tag is a function of the end date's calendar month, so a 52/53-week filer's
        quarter and its prior-year twin can carry neighbouring tags -- Waters' 2023-09-30 is
        ``(Q3)`` beside 2022-10-01 ``(Q4)`` -- and a same-tag match found no prior year at all.
        Any other period takes the same-tag column that is a year earlier."""
        end = date.fromisoformat(period.date)
        twins = [
            p
            for p in self.periods
            if (p.is_quarter if period.is_quarter else p.tag == period.tag)
            and fiscal.is_year_apart(date.fromisoformat(p.date), end)
        ]
        # several (a gateway tag repeated on neighbouring columns): the one closest to a year
        return min(
            twins,
            key=lambda p: (abs((end - date.fromisoformat(p.date)).days - 365), p.date),
            default=None,
        )

    def _rows_for(self, spec: LineItem) -> Iterator[dict[str, Any]]:
        """Every non-abstract, non-dimensional row across *spec*'s statements,
        in raw document order."""
        for statement in spec.statements:
            for row in self.raw.get(statement, []):
                if row.get("abstract") or row.get("dimension"):
                    continue
                yield row

    # -- the filed value of a row (ID-11, ID-12, ID-13; T-148) -----------------------------------

    def _flag(self, name: str) -> None:
        if name not in self._flags:
            self._flags.append(name)

    def _read(self, row: dict[str, Any], column: str, spec: LineItem | None = None) -> float | None:
        """The value of *row* in *column* **in the sign it was filed in**: the displayed value
        times the row's ``preferred_sign`` (ID-12: edgartools renders a value with a negating label
        as the negative of the filed one; ``preferred_sign = -1`` marks it). A row without a
        ``preferred_sign`` (a payload that predates the field, a stored fact written before T-148)
        reads as displayed and flags ``sign_unverified``.

        A negative filed value of a concept in ``spec.nonnegative`` (ID-13, DQC_0015) is a wrong
        sign: the row is **rejected** -- ``None`` -- and ``wrong_sign_rejected`` flagged. It is never
        repaired with ``abs()`` (ID-11). A negative on a row with no hint is not known to be the
        filed sign (a payment is displayed negative); it is rejected the same way and flagged
        ``sign_unknown_negative_rejected``: the item is empty, not a guess either way."""
        displayed = _numeric(row.get(column))
        if displayed is None:
            return None
        sign = _preferred_sign(row)
        if sign is None:
            self._flag("sign_unverified")
            value = displayed
        else:
            value = displayed * sign
        if spec is not None and value < 0 and self._must_not_be_negative(spec, row):
            # a negative with no sign hint is not known to be the filed sign (a payment is displayed
            # negative): it is still not used -- never made positive -- but named for what it is
            self._flag(
                "wrong_sign_rejected" if sign is not None else "sign_unknown_negative_rejected"
            )
            return None
        return value

    @staticmethod
    def _must_not_be_negative(spec: LineItem, row: dict[str, Any]) -> bool:
        if not spec.nonnegative:
            return False
        concept = row.get("concept")
        if concept in spec.nonnegative:
            return True
        # a caption-matched row (``label_fallback``): a custom concept, found by its label
        return bool(
            spec.label_fallback
            and concept not in spec.concepts
            and concept not in spec.fallback_concepts
        )

    # -- resolution ------------------------------------------------------------------------------

    def resolve(self, item: str, period_key: str) -> Resolution:
        """Registry *item* for *period_key*: its value and where the value came from.

        ``concept`` is the concept the value was read from (``a + b`` for a map, ``None`` for a
        value derived from the statement's own layout), ``how`` the tier that supplied it
        (``concept``, ``standard``, ``label``, ``total``, ``sum``, ``map``, ``fallbackN``,
        ``label_fallback``, ``derived``, ``filed_fact``) and ``flags`` what the reader must know
        (a wrong sign rejected, a partial map, a line that includes leases, ...). Re-entrant: a
        composite resolver calls it for the items it depends on.

        T-159c: when the statements resolve the item to nothing, the filing's own
        ``filed_facts`` are read (statements first, always); a value they supply carries
        ``how = "filed_fact"`` and the ``filed_fact`` flag."""
        spec = REGISTRY[item]
        outer, self._flags = self._flags, []
        try:
            column = period_key
            instant = None
            if spec.statements == _BALANCE:
                instant = self._instant_for(period_key)
                column = instant or period_key
            value, concept, how = self._resolve_column(spec, column)
            if value is None:
                fallback = self._filed_fact_row(spec, column, instant=spec.statements == _BALANCE)
                if fallback is not None:
                    value, concept, how = fallback
                    self._flag("filed_fact")
            if value is None and spec.name == "revenue":
                # F-A13: a dropped revenue row whose value equals another own revenue concept's
                # filed fact (YUM: the face row ``RevenueFromContract...``, the filed ``Revenues``).
                restored = self._restore_dropped_revenue(spec, column)
                if restored is not None:
                    value, concept, how = restored
            if value is None and spec.name == "short_term_investments":
                # Audit F-A1: last, after the filed facts (T-159c's proven tier keeps precedence).
                caption_row = self._sti_caption_row(spec, column)
                if caption_row is not None:
                    value, concept, how = caption_row
            self._apply_resolution_flags(spec, column, value, concept, how)
            return Resolution(value, concept, how, tuple(self._flags))
        finally:
            self._flags = outer

    def _apply_resolution_flags(
        self,
        spec: LineItem,
        column: str,
        value: float | None,
        concept: str | None,
        how: str | None,
    ) -> None:
        if value is not None and concept is not None and spec.concept_flags:
            for c, flag in spec.concept_flags:
                if concept == c or c in [p.strip() for p in re.split(r"\s+[+-]\s+", concept)]:
                    self._flag(flag)
        if value is None and spec.unread_flags:
            for c, flag in spec.unread_flags:
                if self._is_concept_filed_in_column(spec, column, c):
                    self._flag(flag)
        if (
            spec.name in ("revenue", "net_income", "equity")
            and value is not None
            and concept is not None
            and self._equals_dimensional_member(spec, column, concept, value)
        ):
            self._flag("equals_dimensional_member")
        self._apply_id01_flags(how, concept, value)

    def _apply_id01_flags(self, how: str | None, concept: str | None, value: float | None) -> None:
        if how == "standard":
            self._flag("standard_concept_match")
        elif how in ("label", "label_fallback"):
            self._flag("label_match")
        if value is not None and concept is not None:
            parts = [p.strip() for p in re.split(r"\s+[+-]\s+", concept) if p.strip()]
            if any(not p.startswith(("us-gaap_", "dei_")) for p in parts):
                self._flag("extension_concept")

    def get(self, item: str, period_key: str) -> float | None:
        """Return the value of registry *item* for *period_key*, or ``None`` (:meth:`resolve`'s
        value).

        Values are in the **filed sign** (ID-11/12: :meth:`_read`). Concept precedence (ID-02a,
        T-148): the item's exact ``concepts`` first, **in the spec's order**, then its
        ``standard`` concepts, then ``label_contains`` -- a label or standard-concept match never
        outranks an exact concept (:meth:`_first_component_match`).

        Two-tier resolution (F2, ``docs/model_fixes.md``): **Tier 1** -- if
        ``spec.total_concepts`` is set, any row tagged with one of them wins
        outright over every ``concepts`` match, regardless of document
        order -- *unless* :meth:`_total_is_plausible` rejects it (T-095,
        ``docs/model_fixes.md``): a ``total_concepts`` tag is sometimes
        mistagged on one small dimensional slice of the real breakdown
        rather than the consolidated aggregate (T-128: detected structurally, as a
        non-dimensional row that exactly duplicates a dimensional row of the same
        concept; APA FY2021's real shape --
        ``us-gaap_Revenues`` = $1,082M, matching only its "Equity Method
        Investment, Nonconsolidated Investee" dimensional row, while the
        real total is $7,988M), and such a value can never be the genuine
        aggregate; or *unless* :meth:`_label_total_correction` finds it
        contradicted the other way -- implausibly *large* (T-117,
        ``docs/model_fixes.md``): a breakdown figure the gateway mislabeled
        with the aggregate's own concept, not the consolidated total either
        (APA FY2023-2025's shape -- ``us-gaap_Revenues`` roughly double the
        statement's own later, smaller "Total revenues and other" line).
        Absent a trusted match there, **Tier 2** falls back to the single-row lookup: the first
        match in precedence order (unchanged default for every item that doesn't set
        ``sum_components``), or, when ``spec.sum_components`` is set, the
        sum of the first row per distinct matching concept -- multiple
        co-reported streams with no separately tagged total.

        When **no** ``total_concepts`` row has a value at all (the gateway's T-042 drops a
        revenue total a filer tags only with a dimension -- APA 2021Q1-2023Q3), the total is
        rebuilt from the statement's own later "Total revenues and other" line
        (:meth:`_rebuild_missing_total`, T-140) when that is safely derivable.

        The items with ``spec.resolver`` set (net income, income tax, debt) use their composite
        resolver instead: :data:`_COMPOSITE`.
        """
        return self.resolve(item, period_key).value

    def _resolve_column(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None]:
        composite = _COMPOSITE.get(spec.resolver)
        if composite is not None:
            resolved: tuple[float | None, str | None, str | None] = getattr(self, composite)(
                spec, column
            )
            return resolved
        total_value = None
        rebuild_missing_total = False
        if spec.total_concepts:
            total_value, total_concept = self._first_total_row(spec, column)
            if total_value is None:
                rebuild_missing_total = True
            else:
                corrected, contradicted = self._label_total_correction(spec, column, total_value)
                if corrected is not None:
                    return corrected, None, "derived"
                if not contradicted and self._total_is_plausible(spec, column, total_value):
                    return total_value, total_concept, "total"

        value, concept, how = self._component_row(spec, column)
        if spec.name == "revenue" and total_value is None:
            # Audit F-A7: on an Article 9 statement (net interest income and noninterest income both
            # on the face) a revenue-concept row smaller than the map is one stream of noninterest
            # income (KEY's lease income, FITB's and HBAN's ASC 606 fees), never the total (ID-02b).
            bank_rev = self._bank_net_revenue(spec, column)
            if bank_rev is not None and (value is None or abs(value) < abs(bank_rev[0])):
                return bank_rev
        if rebuild_missing_total:
            rebuilt = self._rebuild_missing_total(spec, column)
            # A surviving component (APA 2021Q1: a $0 "production revenues" row) is a partial
            # stream, never the total; only a component *above* the rebuilt total contradicts it.
            if rebuilt is not None and (value is None or rebuilt >= value):
                return rebuilt, None, "derived"
        return value, concept, how

    def _bank_net_revenue(self, spec: LineItem, column: str) -> tuple[float, str, str] | None:
        """D2 bank map (APP-01, FASB 2026 calculation linkbase soi1 / soi4):
        Revenue = InterestIncomeExpenseNet + NoninterestIncome.
        soi1: RevenuesNetOfInterestExpense = Revenues - InterestExpenseOperating;
        soi4: InterestIncomeExpenseNet = Interest income lines - InterestExpenseOperating.
        """
        nii = self._concept_value(spec, column, G + "InterestIncomeExpenseNet")
        nonii = self._concept_value(spec, column, G + "NoninterestIncome")
        if nii is not None and nonii is not None:
            self._flag("bank_net_revenue_map")
            concept = f"{G}InterestIncomeExpenseNet + {G}NoninterestIncome"
            return nii + nonii, concept, "map"
        return None

    def _sum_reads_as_only_revenue(self, spec: LineItem, column: str, total: float) -> bool:
        """D-02 condition: sum of >= 2 streams is returned only when no other valued,
        non-abstract, non-dimensional income-statement row reads as revenue.
        B4: 'The Revenues element would be used for the total revenue (ASC 606 and non-ASC 606 revenue)'.
        """
        for row in self._rows_for(spec):
            concept = str(row.get("concept") or "")
            if concept in spec.concepts:
                continue
            value = _numeric(row.get(column))
            if value is None:
                continue
            if abs(value - total) <= 0.005 * abs(total):
                continue
            local = concept.rsplit("_", 1)[-1]
            label = str(row.get("label") or "")
            if (
                _REV_CONCEPT_RE.search(local) or _REV_CAPTION_RE.search(label)
            ) and not _COST_EXPENSE_RE.search(label):
                return False
        return True

    def _component_row(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None]:
        """Tier 2 of :meth:`get`: the summed components, or the first matching row (plus the
        T-133 fallback tiers when that finds nothing)."""
        if spec.sum_components:
            total, used = self._sum_components(spec, column)
            if total is None:
                return None, None, None
            if len(used) >= MIN_SUMMED:
                if self._sum_reads_as_only_revenue(spec, column, total):
                    self._flag("revenue_606_plus_non606_map")
                    return total, " + ".join(sorted(used)), "map"
                self._flag("revenue_sum_not_only_revenue_lines")
                return None, None, None
            return total, used[0], "concept"
        value, concept, how = self._first_component_row(spec, column)
        if value is None:
            value, concept, how = self._last_resort(spec, column)
        return value, concept, how

    def _last_resort(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None]:
        """The tiers after the item's own concepts found nothing: the T-133 fallback tiers (the
        filer's own caption) and then the T-159a substitutes."""
        if spec.fallback_concepts or spec.label_fallback:
            return self._fallback_row(spec, column)
        return self._substitute_row(spec, column)

    # -- the substitutes (T-159a) -------------------------------------------------------------------

    def _substitute_row(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None]:
        """The item's value from a **substitute** concept (T-159a): the item's total under another
        standard concept, the sole member of a component family, the disjoint classes summed, or an
        exact two-part sum.

        Reached only when the item's own concepts found nothing (ID-02a precedence), so an item
        that resolves today resolves to the same value after T-159a. The order is the brief's: the
        total first, then the component families, then the classes, then the exact sums."""
        substitute = spec.substitute
        if substitute is None:
            return None, None, None
        for concept in substitute.totals:
            value = self._concept_value(spec, column, concept)
            if value is not None:
                return value, concept, "substitute"
        family = self._sole_family_member(spec, column, substitute.families)
        if family is not None:
            concept, value = family
            return value, concept, "substitute"
        if substitute.classes:
            total, concept, counted = self._class_sum(spec, column, substitute.classes)
            if counted:
                return total, concept, "map"
        for left, right in substitute.sums:
            first = self._concept_value(spec, column, left)
            second = self._concept_value(spec, column, right)
            if first is not None and second is not None:
                return first + second, f"{left} + {right}", "sourced_map"
        return None, None, None

    # -- the filed-fact fallback (T-159c) -----------------------------------------------------------

    def _filed_fact_row(
        self, spec: LineItem, column: str, *, instant: bool
    ) -> tuple[float | None, str | None, str | None] | None:
        """The fallback's tiers; a kept value carries its proof only -- an earlier tier's refusal
        flag stays only on an item left empty (T-159c round 3)."""
        row = self._filed_fact_tiers(spec, column, instant=instant)
        if row is not None and row[0] is not None:
            self._flags = [
                raised for raised in self._flags if not raised.startswith("filed_fact_refused")
            ]
        return row

    def _filed_fact_tiers(  # noqa: C901, PLR0911 - one return per fallback kind
        self, spec: LineItem, column: str, *, instant: bool
    ) -> tuple[float | None, str | None, str | None] | None:
        """The item's value from the filing's own ``filed_facts`` when the statements gave nothing.

        Statements first, always (ID-02a): this is reached only when :meth:`_resolve_column`
        returned no value. The period is the read column's own (an instant at the column date; a
        quarter 80-119 days, a year >= 351 days, a year-to-date column 175-285 days, an untagged
        duration column <= 371 days). Exactly one distinct value for a concept is used; two
        different values for the same concept and period leave the item empty
        (``filed_fact_ambiguous``). A failed gateway read leaves the item as it is today and is
        flagged ``filed_facts_unavailable``.
        """
        if not self.filed_facts:
            if self.filed_facts_error:
                self._flag("filed_facts_unavailable")
            return None
        if spec.name in _SHARE_ITEMS:
            # The pipeline reads the cover count and the weighted diluted count elsewhere; a
            # filed-fact share count is not this fallback's item (T-159c round 2).
            return None
        period = self._filed_fact_period(column, instant=instant)
        if period is None:
            return None
        if spec.name == "long_term_debt" and "debt_sum_refused_other_lines" in self._flags:
            # Rule E: the statements refused the sum because the face carries other debt lines.
            self._filed_fact_refuse("other_debt_lines")
            return None
        if spec.name == "net_income":
            return self._net_income_filed_fact(spec, period)
        if spec.name == "cash":
            return self._cash_filed_fact(spec, period)
        if spec.name == "short_term_debt":
            return self._short_term_debt_filed_fact(spec, period)
        if spec.name == "long_term_debt":
            return self._long_term_debt_filed_fact(spec, period)
        if spec.name == "depreciation_amortization":
            return self._da_filed_fact(spec, period)
        return self._generic_filed_fact(spec, period)

    def _filed_fact_period(self, column: str, *, instant: bool) -> Period | None:
        """The :class:`Period` the read column names, in the statement's own kind.

        A balance-sheet column is an instant; a flow whose key is a bare date is an untagged
        duration column even when the balance sheet also carries that date as an instant. A
        balance-sheet column always exists as a period, even when the payload carries no statement
        row for it."""
        candidates = [period for period in self.periods if period.key == column]
        for period in candidates:
            if period.is_instant == instant:
                return period
        if not column[:4].isdigit():
            return None
        return Period(key=column, date=column[:10], tag=INSTANT if instant else "")

    @staticmethod
    def _filed_concept(concept: str) -> str:
        """The registry spelling of a filed-fact concept (``us-gaap:X`` -> ``us-gaap_X``)."""
        return concept.replace(":", "_", 1)

    @staticmethod
    def _filed_fact_days(fact: FiledFact) -> int | None:
        if fact.period_start is None:
            return None
        try:
            return (
                date.fromisoformat(fact.period_end) - date.fromisoformat(fact.period_start)
            ).days
        except ValueError:
            return None

    def _filed_fact_matches(  # noqa: PLR0911 - one return per period rule
        self, fact: FiledFact, period: Period
    ) -> bool:
        """Whether *fact* is the read column's own period (T-159b's rules)."""
        if period.is_instant:
            return fact.period_start is None and fact.period_end == period.date
        if fact.period_start is None or fact.period_end != period.date:
            return False
        days = self._filed_fact_days(fact)
        if days is None:
            return False
        if period.tag == "FY":
            return days >= _FY_MIN_DAYS
        if _QUARTER_RE.match(period.tag):
            return _QUARTER_DAYS[0] <= days <= _QUARTER_DAYS[1]
        if period.tag == "YTD":
            return _YTD_DAYS[0] <= days <= _YTD_DAYS[1]
        return 1 <= days <= _UNTAGGED_MAX_DAYS

    def _filed_fact_expected_unit(self, spec: LineItem) -> str:
        return _SHARE_UNIT if spec.name in _SHARE_ITEMS else _MONETARY_UNIT

    def _filed_facts_for(self, spec: LineItem, period: Period, concept: str) -> list[FiledFact]:
        wanted = self._filed_concept(concept)
        unit = self._filed_fact_expected_unit(spec)
        return [
            fact
            for fact in self.filed_facts
            if fact.unit == unit
            and self._filed_concept(fact.concept) == wanted
            and self._filed_fact_matches(fact, period)
        ]

    def _filed_fact_value(
        self, spec: LineItem, period: Period, concept: str
    ) -> tuple[float | None, str | None]:
        """The one distinct value of *concept* in *period*: ``(value, None)``, or
        ``(None, "ambiguous")`` when two different values are filed (never guessed). A negative
        value of an ID-13 non-negative concept is rejected, as :meth:`_read` rejects it."""
        facts = self._filed_facts_for(spec, period, concept)
        if not facts:
            return None, None
        values = {fact.value for fact in facts}
        if len(values) > 1:
            self._flag("filed_fact_ambiguous")
            return None, "ambiguous"
        value = next(iter(values))
        if value < 0 and concept in spec.nonnegative:
            self._flag("wrong_sign_rejected")
            return None, None
        if value == 0.0:
            # Rule C: a zero filed value is never the item.
            self._filed_fact_refuse("zero")
            return None, "zero"
        return value, None

    def _filed_fact_present(self, spec: LineItem, period: Period, concept: str) -> bool:
        return bool(self._filed_facts_for(spec, period, concept))

    def _filed_fact_refuse(self, reason: str) -> None:
        """Rule A-E: the value is not used; name the rule that refused it."""
        self._flag("filed_fact_refused")
        self._flag(f"filed_fact_refused_{reason}")

    def _filed_fact_face_values(self, spec: LineItem, period: Period) -> dict[int, list[str]]:
        """The item's own face statement's non-zero values at *period*, rounded to the dollar, with
        the labels of the rows that carry each."""
        out: dict[int, list[str]] = {}
        for statement in spec.statements:
            for row in self.raw.get(statement, []):
                if row.get("abstract") or row.get("dimension"):
                    continue
                value = _numeric(row.get(period.key))
                if value is not None and value != 0.0:
                    out.setdefault(round(abs(value)), []).append(str(row.get("label") or ""))
        return out

    def _filed_fact_statement_value(self, spec: LineItem, column: str) -> float | None:
        """The **statements'** value of *item* at *column*, without the filed-fact fallback."""
        outer_facts, outer_flags = self.filed_facts, self._flags
        self.filed_facts, self._flags = (), []
        try:
            read = column
            if spec.statements == _BALANCE:
                instant = self._instant_for(column)
                if instant is None:
                    return None
                read = instant
            return self._resolve_column(spec, read)[0]
        finally:
            self.filed_facts, self._flags = outer_facts, outer_flags

    def _filed_fact_path_value(self, spec: LineItem, period: Period, concept: str) -> float | None:
        """The same filed path (one concept or a ``+``/``-`` sum) in *period*, without flags."""
        outer_flags = self._flags
        self._flags = []
        try:
            total = 0.0
            sign = 1.0
            for part in re.split(r" ([+-]) ", concept):
                if part in "+-":
                    sign = 1.0 if part == "+" else -1.0
                    continue
                value, _status = self._filed_fact_value(spec, period, part)
                if value is None:
                    return None
                total += sign * value
            return total
        finally:
            self._flags = outer_flags

    def _filed_fact_proof(  # noqa: C901 - A's label check and B's calibration in one place
        self, spec: LineItem, period: Period, concept: str, value: float
    ) -> str | None:
        """Rule A/B: ``"on_face"``, ``"calibrated"``, or the refusal reason (``not_on_face`` /
        ``calibration``). A candidate on the face needs no calibration; with at least one other
        comparable column, the same filed path must agree within 0.5 % at every one."""
        if value == 0.0:
            return "zero"
        balance = spec.statements == _BALANCE
        compared = agreed = 0
        for other_period in self.periods:
            if other_period.is_instant != balance or other_period.key == period.key:
                continue
            statement_value = self._filed_fact_statement_value(spec, other_period.key)
            if statement_value is None or statement_value == 0.0:
                continue
            path_value = self._filed_fact_path_value(spec, other_period, concept)
            if path_value is None:
                continue
            compared += 1
            if abs(path_value - statement_value) <= 0.005 * abs(statement_value):
                agreed += 1
        if compared:
            return "calibrated" if agreed == compared else "calibration"
        labels = self._filed_fact_face_values(spec, period).get(round(abs(value)))
        if not labels:
            return "not_on_face"
        if not any(_face_label_names_item(spec.name, label) for label in labels):
            return "face_row_not_item"
        other_item = _OTHER_DEBT.get(spec.name)
        if other_item is not None:
            other_value = self._filed_fact_statement_value(REGISTRY[other_item], period.key)
            if other_value is not None and round(abs(other_value)) == round(abs(value)):
                return "face_row_other_debt"
        return "on_face"

    def _filed_fact_accept(
        self, spec: LineItem, period: Period, concept: str, value: float
    ) -> bool:
        """Whether a filed candidate is proved to be the item; adds its proof/refusal flag."""
        proof = self._filed_fact_proof(spec, period, concept, value)
        if proof in ("on_face", "calibrated"):
            self._flag(f"filed_fact_{proof}")
            return True
        self._filed_fact_refuse(proof or "not_on_face")
        return False

    def _generic_filed_fact(  # noqa: C901, PLR0911, PLR0912 - the registry tiers, one pass
        self, spec: LineItem, period: Period
    ) -> tuple[float | None, str | None, str | None] | None:
        """The item's own concepts in the registry's order, then its T-133 fallback concepts,
        then T-159a's substitutes (totals, sole family members, disjoint classes, exact sums)."""
        own = [*spec.total_concepts, *spec.concepts, *spec.fallback_concepts]
        for concept in own:
            if concept in _FILED_FACT_SKIP_CONCEPTS:
                continue
            value, status = self._filed_fact_value(spec, period, concept)
            if status == "ambiguous":
                return None
            if value is not None and self._filed_fact_accept(spec, period, concept, value):
                return value, concept, "filed_fact"
        substitute = spec.substitute
        if substitute is None:
            return None
        for concept in substitute.totals:
            value, status = self._filed_fact_value(spec, period, concept)
            if status == "ambiguous":
                return None
            if value is not None and self._filed_fact_accept(spec, period, concept, value):
                return value, concept, "filed_fact"
        family = self._filed_fact_family(spec, period, substitute.families)
        if family is not None:
            concept, value = family
            if self._filed_fact_accept(spec, period, concept, value):
                return value, concept, "filed_fact"
        if substitute.classes:
            classes = self._filed_fact_classes(spec, period, substitute.classes)
            if classes is not None:
                value, concept, _how = classes
                if self._filed_fact_accept(spec, period, concept, value):
                    return value, concept, "filed_fact"
        for left, right in substitute.sums:
            first, first_status = self._filed_fact_value(spec, period, left)
            second, second_status = self._filed_fact_value(spec, period, right)
            if first_status == "ambiguous" or second_status == "ambiguous":
                return None
            if first is not None and second is not None:
                concept = f"{left} + {right}"
                value = first + second
                if self._filed_fact_accept(spec, period, concept, value):
                    return value, concept, "filed_fact"
        return None

    def _filed_fact_family(  # noqa: C901 - one family's members, then the sole-member rule
        self, spec: LineItem, period: Period, families: tuple[str, ...]
    ) -> tuple[str, float] | None:
        """The sole filed member of the sole filled family, from the filed facts (T-159a)."""
        filled: list[tuple[str, list[tuple[str, float]]]] = []
        for family in families:
            members: list[tuple[str, float]] = []
            for fact in self.filed_facts:
                concept = self._filed_concept(fact.concept)
                if not concept.startswith(family) or fact.unit != self._filed_fact_expected_unit(
                    spec
                ):
                    continue
                if not self._filed_fact_matches(fact, period):
                    continue
                if fact.value < 0 and concept in spec.nonnegative:
                    self._flag("wrong_sign_rejected")
                    continue
                members.append((concept, fact.value))
            if not members:
                continue
            values = {value for _concept, value in members}
            if len(values) > 1:
                self._flag("filed_fact_ambiguous")
                return None
            distinct = {concept for concept, _value in members}
            if len(distinct) > 1:
                return None  # two members of one family are not the total (ID-02b)
            concept = next(iter(distinct))
            filled.append((family, [(concept, next(iter(values)))]))
        if len(filled) != 1:
            return None
        members = filled[0][1]
        if len(members) != 1:
            return None
        return members[0]

    def _filed_fact_classes(
        self, spec: LineItem, period: Period, classes: tuple[tuple[str, ...], ...]
    ) -> tuple[float, str, str] | None:
        """The disjoint classes' values, one member per class, summed, from the filed facts."""
        total = 0.0
        used: list[str] = []
        for members in classes:
            present: list[tuple[str, float]] = []
            for concept in members:
                value, status = self._filed_fact_value(spec, period, concept)
                if status == "ambiguous":
                    return None
                if value is not None:
                    present.append((concept, value))
            if not present:
                continue
            if len(present) > 1:
                self._flag("substitute_class_multiple")
            concept, value = present[0]
            total += value
            used.append(concept)
        if not used:
            return None
        return total, " + ".join(sorted(used)), "filed_fact"

    def _net_income_filed_fact(  # noqa: C901, PLR0911, PLR0912 - ID-03/CON-19 tiers
        self, spec: LineItem, period: Period
    ) -> tuple[float | None, str | None, str | None] | None:
        """Parent net income from the filed facts (ID-03, CON-19, N13).

        ``NetIncomeLoss`` first. ``ProfitLoss`` only when no NCI income is filed, or less the
        filed NCI total. When only NCI **parts** are filed the ``ProfitLoss`` refusal stands
        (``profit_loss_nci_income_not_total``); the T-159a "available to common" substitute is
        still read when no preferred dividend is filed, because it is then the same quantity.
        """
        parent, status = self._filed_fact_value(spec, period, G + "NetIncomeLoss")
        if status == "ambiguous":
            return None
        if parent is not None and self._filed_fact_accept(
            spec, period, G + "NetIncomeLoss", parent
        ):
            return parent, G + "NetIncomeLoss", "filed_fact"
        profit, status = self._filed_fact_value(spec, period, G + "ProfitLoss")
        if status == "ambiguous":
            return None
        if profit is not None:
            nci_total = G + "NetIncomeLossAttributableToNoncontrollingInterest"
            if not self._nci_income_filed_fact(spec, period):
                if self._filed_fact_accept(spec, period, G + "ProfitLoss", profit):
                    self._flag("no_nci_income_filed")
                    return profit, G + "ProfitLoss", "filed_fact"
            else:
                nci, nci_status = self._filed_fact_value(spec, period, nci_total)
                if nci_status == "ambiguous":
                    return None
                if nci is not None:
                    concept = f"{G}ProfitLoss - {nci_total}"
                    combined = profit - nci
                    if self._filed_fact_accept(spec, period, concept, combined):
                        return combined, concept, "filed_fact"
        for concept in NET_INCOME_SUBSTITUTES:
            value, status = self._filed_fact_value(spec, period, concept)
            if status == "ambiguous":
                return None
            if value is None:
                continue
            if self._preferred_dividends_filed(period.key) or any(
                self._filed_fact_present(spec, period, c) for c in PREFERRED_DIVIDEND_CONCEPTS
            ):
                self._flag("net_income_to_common_refused_preferred_dividends")
                return None
            if self._filed_fact_accept(spec, period, concept, value):
                self._flag("net_income_to_common")
                return value, concept, "filed_fact"
        return None

    def _nci_income_filed_fact(self, spec: LineItem, period: Period) -> bool:
        """Whether income attributable to NCI is filed for the period, in the statements or the
        filed facts (the same conservative reading as :meth:`_nci_income_filed`)."""
        if self._nci_income_filed(period.key):
            return True
        for fact in self.filed_facts:
            if not self._filed_fact_matches(fact, period):
                continue
            concept = self._filed_concept(fact.concept)
            if concept in _NCI_INCOME_CONCEPTS:
                return True
            local = concept.split("_", 1)[-1]
            if "NoncontrollingInterest" in local and "NetIncomeLoss" in local:
                return True
        return False

    def _cash_filed_fact(
        self, spec: LineItem, period: Period
    ) -> tuple[float | None, str | None, str | None] | None:
        """Cash from the filed facts: the registry's own concepts, then ``Cash`` plus
        ``CashEquivalentsAtCarryingValue``, then ``Cash`` alone when no cash-equivalents fact is
        filed (T-148 (k), T-159a)."""
        for concept in spec.concepts:
            if concept in _FILED_FACT_SKIP_CONCEPTS:
                continue
            value, status = self._filed_fact_value(spec, period, concept)
            if status == "ambiguous":
                return None
            if value is not None and self._filed_fact_accept(spec, period, concept, value):
                return value, concept, "filed_fact"
        cash = G + "Cash"
        equivalents = G + "CashEquivalentsAtCarryingValue"
        cash_value, cash_status = self._filed_fact_value(spec, period, cash)
        ce_value, ce_status = self._filed_fact_value(spec, period, equivalents)
        if cash_status == "ambiguous" or ce_status == "ambiguous":
            return None
        if cash_value is not None and ce_value is not None:
            concept = f"{cash} + {equivalents}"
            value = cash_value + ce_value
            if self._filed_fact_accept(spec, period, concept, value):
                self._flag("cash_plus_cash_equivalents_map")
                return value, concept, "filed_fact"
        if (
            cash_value is not None
            and ce_value is None
            and self._filed_fact_accept(spec, period, cash, cash_value)
        ):
            self._flag("cash_element_only")
            return cash_value, cash, "filed_fact"
        return None

    def _short_term_debt_filed_fact(  # noqa: C901 - the map and N4
        self, spec: LineItem, period: Period
    ) -> tuple[float | None, str | None, str | None] | None:
        """The short-term debt map from the filed facts, with N4: current maturities are never
        added to a long-term line that already contains them."""
        in_long_term = bool(
            {"includes_current_maturities", "current_maturities_in_long_term"} & set(self._flags)
        )
        parts: list[tuple[float, str]] = []
        for term, concepts in SHORT_TERM_DEBT_MAP:
            if term == "current_maturities" and in_long_term:
                continue
            for concept in concepts:
                value, status = self._filed_fact_value(spec, period, concept)
                if status == "ambiguous":
                    return None
                if value is not None and self._filed_fact_accept(spec, period, concept, value):
                    parts.append((value, concept))
                    break
        if parts:
            concept = " + ".join(concept for _value, concept in parts)
            value = sum(value for value, _concept in parts)
            if self._filed_fact_accept(spec, period, concept, value):
                return value, concept, "filed_fact"
        if not in_long_term:
            value, status = self._filed_fact_value(spec, period, SHORT_TERM_DEBT_LAST)
            if status == "ambiguous":
                return None
            if value is not None and self._filed_fact_accept(
                spec, period, SHORT_TERM_DEBT_LAST, value
            ):
                self._flag("includes_leases")
                return value, SHORT_TERM_DEBT_LAST, "filed_fact"
        return None

    def _long_term_debt_filed_fact(  # noqa: C901, PLR0911, PLR0912 - concepts then components
        self, spec: LineItem, period: Period
    ) -> tuple[float | None, str | None, str | None] | None:
        """Long-term debt from the filed facts: the registry's own concepts, then T-159a's
        components (widest first, a component's filed children only when it is not filed)."""
        for concept in spec.concepts:
            value, status = self._filed_fact_value(spec, period, concept)
            if status == "ambiguous":
                return None
            if value is None:
                continue
            includes_current = concept in INCLUDES_CURRENT_MATURITIES
            if includes_current:
                short = self._filed_fact_statement_value(REGISTRY["short_term_debt"], period.key)
                if short is not None and short != 0.0:
                    # Rule D: the statements already gave short-term debt. Keep the long-term
                    # value, but do not let it set N4's ``current_maturities_in_long_term`` --
                    # that would remove or change the statement's short-term value.
                    includes_current = False
            finance_leases = concept in (
                G + "LongTermDebtAndCapitalLeaseObligations",
                G + "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
            )
            if self._filed_fact_accept(spec, period, concept, value):
                if includes_current:
                    self._flag("includes_current_maturities")
                if finance_leases:
                    self._flag("includes_finance_leases")
                return value, concept, "filed_fact"
        filed: dict[str, float] = {}
        blocked: set[str] = set()
        for component in LONG_TERM_DEBT_COMPONENTS:
            if component in blocked:
                continue
            value, status = self._filed_fact_value(spec, period, component)
            if status == "ambiguous":
                return None
            if value is not None:
                filed[component] = value
                blocked.update(self._component_descendants(component))
                continue
            for child in LONG_TERM_DEBT_COMPONENT_CHILDREN.get(component, ()):
                if child in filed or child in blocked:
                    continue
                child_value, child_status = self._filed_fact_value(spec, period, child)
                if child_status == "ambiguous":
                    return None
                if child_value is not None:
                    filed[child] = child_value
                    blocked.update(self._component_descendants(child))
        if not filed:
            return None
        concept = " + ".join(sorted(filed))
        value = sum(filed.values())
        if not self._filed_fact_accept(spec, period, concept, value):
            return None
        self._flag("debt_sourced_sum")
        return value, concept, "filed_fact"

    def _da_filed_fact(
        self, spec: LineItem, period: Period
    ) -> tuple[float | None, str | None, str | None] | None:
        """D&A from the filed facts: the filed aggregate, else the exact two-part sum
        ``Depreciation`` + ``AmortizationOfIntangibleAssets`` (both parts, ID-02b)."""
        for concept in spec.concepts:
            value, status = self._filed_fact_value(spec, period, concept)
            if status == "ambiguous":
                return None
            if value is not None and self._filed_fact_accept(spec, period, concept, value):
                # T-161: the aggregate is the amortization alone (the NDSN 10-K shape).
                amortization_only = self._da_amortization_only_sum_filed(
                    spec, period, value, concept
                )
                if amortization_only is not None:
                    return amortization_only
                return value, concept, "filed_fact"
        first, first_status = self._filed_fact_value(spec, period, DA_EXACT_SUM[0])
        second, second_status = self._filed_fact_value(spec, period, DA_EXACT_SUM[1])
        if first_status == "ambiguous" or second_status == "ambiguous":
            return None
        if first is not None and second is not None:
            concept = f"{DA_EXACT_SUM[0]} + {DA_EXACT_SUM[1]}"
            value = first + second
            if self._filed_fact_accept(spec, period, concept, value):
                self._flag("da_sourced_map")
                return value, concept, "filed_fact"
        return None

    def _restore_dropped_revenue(
        self, spec: LineItem, column: str
    ) -> tuple[float, str, str] | None:
        """Audit F-A13: the gateway dropped the revenue face row (T-042 / T-140: its value did not
        reconcile with a non-dimensional filed fact to the dollar -- YUM's differs by $1 M of
        rounding). The payload's ``corrections`` keep the dropped row's original value: when it
        calibrates (0.5 %, T-159c rule B) against the filing's own filed fact of the same revenue
        concept for the column's period, the filed fact is the revenue. Flagged
        ``revenue_dropped_row_restored``."""
        period = next((p for p in self.periods if p.key == column), None)
        if period is None or not self.filed_facts:
            return None
        names = set(spec.concepts) | set(spec.total_concepts)
        for corr in self.corrections:
            if corr.get("corrected") is not None or corr.get("column") != column:
                continue
            concept = str(corr.get("concept") or "")
            original = corr.get("original")
            if concept not in names or not isinstance(original, (int, float)) or original == 0:
                continue
            # the dropped row's concept first, then the item's other own concepts: YUM's face row is
            # ``RevenueFromContractWithCustomer...``, its filed total ``Revenues`` (the same 1,547 M)
            for candidate in (concept, *sorted(names - {concept})):
                filed, status = self._filed_fact_value(spec, period, candidate)
                if filed is None or status is not None:
                    continue
                if abs(abs(filed) - abs(original)) <= 0.005 * abs(filed):
                    self._flag("revenue_dropped_row_restored")
                    return filed, candidate, "filed_fact"
        return None

    def _sti_caption_row(self, spec: LineItem, column: str) -> tuple[float, str, str] | None:
        """Audit F-A1 (MD-02 / MD-03 / MD-35: "cash and marketable securities", C9 p.7): a current-asset
        face row captioned as short-term investments or marketable securities, filed under a concept
        the registry does not list (DXCM's extension "Short-term marketable securities", KLAC's,
        EBAY's, EA's generic AFS element). Only rows above "Total current assets"; never a restricted,
        fiduciary, client or held-for-sale row, or one that names cash. Several such rows are summed
        (disjoint face lines). Flagged ``sti_caption_row``."""
        caption = re.compile(
            r"^(short[- ]term (investments?|marketable securities)|marketable securities|"
            r"short[- ]term deposits|time deposits)\b",
            re.IGNORECASE,
        )
        excluded = re.compile(
            r"restrict|fiduciar|client|held for sale|noncurrent|long[- ]term|cash|structured finance",
            re.IGNORECASE,
        )
        found: list[tuple[str, float]] = []
        for row in self.raw.get("balance_sheet", []):
            if row.get("abstract") or row.get("dimension"):
                continue
            if row.get("concept") == G + "AssetsCurrent":
                break
            label = str(row.get("label") or "").strip()
            if not caption.search(label) or excluded.search(label):
                continue
            value = self._read(row, column, spec)
            if value is not None and value > 0:
                found.append((str(row.get("concept")), value))
        if not found:
            return None
        self._flag("sti_caption_row")
        return sum(v for _c, v in found), " + ".join(c for c, _v in found), "label_fallback"

    def _sole_family_member(
        self, spec: LineItem, column: str, families: tuple[str, ...]
    ) -> tuple[str, float] | None:
        """The one filed member of the one filled component family, or nothing (T-159a).

        A component is never the total (ID-02b): it is one only when it is all the filing has. So
        exactly one family may be filled, and that family may hold exactly one filed member --
        two families (XOM's ``EnergyRelatedInventory`` beside ``InventoryPartsAndComponents...``)
        or two members of one family leave the item empty: a partial figure is worse than none."""
        filled: list[tuple[str, list[tuple[str, float]]]] = []
        for family in families:
            members: list[tuple[str, float]] = []
            for row in self._rows_for(spec):
                concept = str(row.get("concept") or "")
                if not concept.startswith(family):
                    continue
                value = self._read(row, column, spec)
                if value is not None:
                    members.append((concept, value))
            if members:
                filled.append((family, members))
        if len(filled) != 1:
            return None
        members = filled[0][1]
        if len(members) != 1:
            return None
        if spec.name == "inventory" and self._other_inventory_row(spec, column, members[0][0]):
            # Audit F-A3: "only when it is all the filing has" -- another inventory face row outside
            # the families (ETR's utility inventory, AEP's and PEG's fuel, FCX's stockpiles and
            # product) means the member is one component, never the total (the XOM precedent).
            self._flag("inventory_component_not_total")
            return None
        return members[0]

    def _other_inventory_row(self, spec: LineItem, column: str, member: str) -> bool:
        """Audit F-A3: an inventory-like face row (concept or caption) with a value in *column*,
        other than *member*: ``Inventor*`` concepts, fuel, stockpiles, materials and supplies, product."""
        caption = re.compile(
            r"inventor|stockpile|materials and supplies|^fuel\b|^product$", re.IGNORECASE
        )
        prefixes = (
            "Inventor",
            "EnergyRelatedInventory",
            "PublicUtilitiesInventory",
            "OtherInventory",
            "MaterialsSupplies",
        )
        for row in self._rows_for(spec):
            concept = str(row.get("concept") or "")
            if concept == member or "Noncurrent" in concept or "Regulatory" in concept:
                continue
            label = str(row.get("label") or "").strip()
            if re.search(r"prepa|long[- ]term|total", label, re.IGNORECASE):
                continue
            if not (concept.split("_", 1)[-1].startswith(prefixes) or caption.search(label)):
                continue
            if self._read(row, column, spec) is not None:
                return True
        return False

    def _class_sum(
        self, spec: LineItem, column: str, classes: tuple[tuple[str, ...], ...]
    ) -> tuple[float, str, int]:
        """The disjoint classes' values, one member per class, summed (T-159a).

        Each class contributes the first of its members that is filed (its own total first), so a
        class is counted once; a class with several filed members is flagged
        ``substitute_class_multiple`` -- the reader must know the class held more than one line."""
        total = 0.0
        used: list[str] = []
        for members in classes:
            for concept in members:
                value = self._concept_value(spec, column, concept)
                if value is None:
                    continue
                if (
                    len([m for m in members if self._concept_value(spec, column, m) is not None])
                    > 1
                ):
                    self._flag("substitute_class_multiple")
                total += value
                used.append(concept)
                break
        return total, " + ".join(sorted(used)), len(used)

    def _component_value(self, spec: LineItem, column: str) -> float | None:
        return self._component_row(spec, column)[0]

    def _first_total_row(self, spec: LineItem, column: str) -> tuple[float | None, str | None]:
        """Tier 1: the first row tagged with one of ``spec.total_concepts``, and its concept."""
        for row in self._rows_for(spec):
            if row.get("concept") in spec.total_concepts:
                value = self._read(row, column, spec)
                if value is not None:
                    return value, str(row.get("concept"))
        return None, None

    def _first_total_match(self, spec: LineItem, column: str) -> float | None:
        return self._first_total_row(spec, column)[0]

    # -- the composite resolvers (T-148) ---------------------------------------------------------

    def _net_income(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None]:
        """Parent net income (ID-03, CON-19; N13), in this order:

        1. ``NetIncomeLoss`` (attributable to the parent), read from any stored statement of the
           filing for the same period column: it is the same fact wherever the filer shows it;
        2. ``ProfitLoss`` (including the noncontrolling interest) when **no** income attributable to
           the noncontrolling interest is filed on any stored statement of the period
           (:meth:`_nci_income_filed`): ID-03's "NCI income is absent", then CON-19 makes the two
           equal. Flagged ``no_nci_income_filed``;
        3. ``ProfitLoss`` less ``NetIncomeLossAttributableToNoncontrollingInterest`` when that
           total is filed (a filed zero included);
        4. empty. ``ProfitLoss`` with NCI income filed only in parts (APA's FY2021 10-K: the
           redeemable variant alone, 162M) is flagged ``profit_loss_nci_income_not_total``; neither
           concept, ``net_income_not_filed``.

        The "available to common" line is after preferred dividends and is never parent net income
        (N13)."""
        parent = self._filed_anywhere(column, G + "NetIncomeLoss")
        if parent is not None:
            return parent, G + "NetIncomeLoss", "concept"
        profit = self._filed_anywhere(column, G + "ProfitLoss")
        if profit is None:
            substitute = self._net_income_substitute(spec, column)
            if substitute[0] is None:
                self._flag("net_income_not_filed")
            return substitute
        if not self._nci_income_filed(column):
            self._flag("no_nci_income_filed")
            return profit, G + "ProfitLoss", "concept"
        nci_concept = G + "NetIncomeLossAttributableToNoncontrollingInterest"
        nci = self._filed_anywhere(column, nci_concept)
        if nci is None:
            self._flag("profit_loss_nci_income_not_total")
            return None, None, None
        return profit - nci, f"{G}ProfitLoss - {nci_concept}", "map"

    def _net_income_substitute(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None]:
        """T-159a: "net income available to common" as parent net income.

        The line is after the preferred dividends, so it is the same quantity as parent net income
        only when **no** preferred dividend is filed for the period (N13). The flag
        ``net_income_to_common`` (on the item) names the substitution for the reader."""
        for concept in NET_INCOME_SUBSTITUTES:
            value = self._concept_value(spec, column, concept)
            if value is None:
                continue
            if self._preferred_dividends_filed(column):
                self._flag("net_income_to_common_refused_preferred_dividends")
                return None, None, None
            return value, concept, "substitute"
        return None, None, None

    def _preferred_dividends_filed(self, column: str) -> bool:
        """Whether a preferred dividend is filed for the period (N13): then "net income available
        to common" is a different quantity from parent net income."""
        return any(
            self._filed_anywhere(column, concept) is not None
            for concept in PREFERRED_DIVIDEND_CONCEPTS
        )

    def _valued_rows(self, column: str) -> Iterator[dict[str, Any]]:
        """Every non-abstract, non-dimensional row of the stored statements that has a value in
        *column*: the income statement first, then the others. A column key names one period (its
        date and tag), so a row of another statement with it is the same period of the same
        filing."""
        for statement in STATEMENT_KEYS:
            for row in self.raw.get(statement, []):
                if (
                    not row.get("abstract")
                    and not row.get("dimension")
                    and _numeric(row.get(column)) is not None
                ):
                    yield row

    def _filed_anywhere(self, column: str, concept: str) -> float | None:
        """The first stored statement's row tagged *concept* that has a value in *column*, in the
        sign it was filed in."""
        for row in self._valued_rows(column):
            if row.get("concept") == concept:
                return self._read(row, column)
        return None

    def _nci_income_filed(self, column: str) -> bool:
        """Whether income attributable to the noncontrolling interest is filed for the period, on
        any stored statement (ID-03): ``NetIncomeLossAttributableToNoncontrollingInterest`` or its
        redeemable / nonredeemable variants, **or** -- a conservative inference, declared in
        ``docs/model_fixes.md`` -- any other row :func:`_names_nci_income` reads as one."""
        return any(_names_nci_income(row) for row in self._valued_rows(column))

    def _income_tax(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None]:
        """Income tax (ID-02b, D-06(c)): ``IncomeTaxExpenseBenefit``; else current + deferred, only
        when both are on the statement face (B6: the total "current income tax expense (benefit)
        and deferred income tax expense (benefit) pertaining to continuing operations"); else
        empty -- a single component is never the total."""
        total = self._concept_value(spec, column, G + "IncomeTaxExpenseBenefit")
        if total is not None:
            return total, G + "IncomeTaxExpenseBenefit", "concept"
        current = self._concept_value(spec, column, G + "CurrentIncomeTaxExpenseBenefit")
        deferred = self._concept_value(spec, column, G + "DeferredIncomeTaxExpenseBenefit")
        if current is not None and deferred is not None:
            self._flag("current_plus_deferred")
            return (
                current + deferred,
                f"{G}CurrentIncomeTaxExpenseBenefit + {G}DeferredIncomeTaxExpenseBenefit",
                "map",
            )
        if current is not None or deferred is not None:
            self._flag("tax_component_not_total")
        return None, None, None

    def _cash_sti(
        self, spec: LineItem, column: str, val: float, concept: str
    ) -> tuple[float, str, str]:
        row = next(
            (
                r
                for r in self._rows_for(spec)
                if r.get("concept") == concept and _numeric(r.get(column)) is not None
            ),
            None,
        )
        cap = str(row.get("label") or "").lower() if row else ""
        if any(w in cap for w in _INVESTMENT_KEYWORDS):
            self._flag("includes_short_term_investments")
        else:
            self._flag("cash_sti_tag_cash_caption")
        return val, concept, "concept"

    def _cash_k1(
        self, spec: LineItem, column: str, val: float, concept: str
    ) -> tuple[float, str, str] | None:
        row = next(
            (
                r
                for r in self._rows_for(spec)
                if r.get("concept") == concept and _numeric(r.get(column)) is not None
            ),
            None,
        )
        cap = str(row.get("label") or "") if row else ""
        cap_clean = _CASH_NOTE_RE.sub("", cap)
        # T-162 (FIXES #2): a parenthetical that names restricted cash ("(includes restricted cash of
        # ...)", ERIE) says the line includes it; test before the parentheses are stripped.
        if "restrict" in cap_clean.lower():
            return None
        cap_clean = _CASH_PAREN_RE.sub("", cap_clean).strip().lower()
        if cap_clean in _ALLOWED_K1_CAPTIONS:
            for r in self._rows_for(spec):
                if r.get("concept") == concept:
                    continue
                c_local = str(r.get("concept") or "").rsplit("_", 1)[-1].lower()
                c_cap = str(r.get("label") or "").lower()
                if ("restrict" in c_local or "restrict" in c_cap) and _numeric(
                    r.get(column)
                ) is not None:
                    return None
            self._flag("cash_tag_includes_restricted_cash_possible")
            return val, concept, "concept"
        return None

    def _cash_classified(
        self, spec: LineItem, column: str, val: float, concept: str
    ) -> tuple[float, str, str] | None:
        ca = self.get("current_assets", column)
        cl = self.get("current_liabilities", column)
        if ca is not None and cl is not None:
            self._flag("cash_element_only")
            return val, concept, "concept"
        return None

    def _cash_primary(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None]:
        c1 = G + "CashAndCashEquivalentsAtCarryingValue"
        val1 = self._concept_value(spec, column, c1)
        if val1 is not None:
            return val1, c1, "concept"

        c2 = G + "CashCashEquivalentsAndShortTermInvestments"
        val2 = self._concept_value(spec, column, c2)
        if val2 is not None:
            return self._cash_sti(spec, column, val2, c2)

        c3 = G + "CashAndDueFromBanks"
        val3 = self._concept_value(spec, column, c3)
        if val3 is not None:
            return val3, c3, "concept"

        c_cash = G + "Cash"
        c_ce = G + "CashEquivalentsAtCarryingValue"
        val_cash = self._concept_value(spec, column, c_cash)
        val_ce = self._concept_value(spec, column, c_ce)
        if val_cash is not None and val_ce is not None:
            self._flag("cash_plus_cash_equivalents_map")
            return val_cash + val_ce, f"{c_cash} + {c_ce}", "map"

        return None, None, None

    def _cash(self, spec: LineItem, column: str) -> tuple[float | None, str | None, str | None]:
        """Cash composite resolver (T-148 (k), B4, B6):

        1. CashAndCashEquivalentsAtCarryingValue;
        2. CashCashEquivalentsAndShortTermInvestments, caption checked;
        3. CashAndDueFromBanks;
        4. ES map: Cash + CashEquivalentsAtCarryingValue;
        5. (k)(1): CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents,
           caption checked and no other balance-sheet restricted line;
        6. (k)(3): Cash alone on a classified balance sheet;
        7. (T-159a) Cash alone on any other balance sheet -- the bare ``Cash`` line, read only when
           no ``CashEquivalentsAtCarryingValue`` is filed (the ES map of step 4 has it then).
        """
        primary = self._cash_primary(spec, column)
        if primary[0] is not None:
            return primary

        c_k1 = G + "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"
        val_k1 = self._concept_value(spec, column, c_k1)
        if val_k1 is not None:
            res_k1 = self._cash_k1(spec, column, val_k1, c_k1)
            return res_k1 if res_k1 is not None else (None, None, None)

        val_cash = self._concept_value(spec, column, G + "Cash")
        if val_cash is not None:
            res_c = self._cash_classified(spec, column, val_cash, G + "Cash")
            if res_c is not None:
                return res_c
            # T-159a: the bare ``Cash`` line is the cash of a filer that files no cash-equivalents
            # fact (the ES map of step 4 covers the case where it does).
            if self._concept_value(spec, column, G + "CashEquivalentsAtCarryingValue") is None:
                self._flag("cash_element_only")
                return val_cash, G + "Cash", "substitute"

        return None, None, None

    def _depreciation_amortization(  # noqa: PLR0911 - one return per reading tier
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None]:
        """D&A (MD-01, ID-02b; C1, T-151 part 1): the filed aggregate first, unchanged, else the
        sourced sum of the filed D&A-nature children, else a refusal.

        E1 (T-156): when the cash-flow statement files **no** D&A fact at all, the same elements are
        read from the income statement instead, flagged ``da_income_statement``. B6's definitions
        name no statement, so the element decides and not the statement: the pinned FASB 2026 text
        (``elts/us-gaap-doc-2026.xml``, B6) reads ``DepreciationDepletionAndAmortization`` "The
        aggregate expense recognized in the current period that allocates the cost of tangible
        assets, intangible assets, or depleting assets to periods that benefit from use of the
        assets", ``Depreciation`` "The amount of expense recognized in the current period that
        reflects the allocation of the cost of tangible assets over the assets' useful lives",
        ``OtherDepreciationAndAmortization`` "Amount of expense charged against earnings to allocate
        the cost of tangible and intangible assets over their remaining economic lives, classified as
        other" and ``AmortizationOfIntangibleAssets`` "Amount of amortization expense for
        finite-lived intangible asset" -- none names a statement. The two statements are never mixed
        in one value: the income statement is read only when the cash-flow statement has no D&A fact
        at all, so a cash-flow D&A fact in any column keeps today's behaviour."""
        value, concept, how = self._component_row(spec, column)
        if value is not None:
            # T-161: the mirror of T-151's ``depreciation_only_not_da`` -- the filed aggregate is
            # the intangible amortization alone and depreciation is filed separately.
            amortization_only = self._da_amortization_only_sum(spec, column, value, concept, how)
            if amortization_only is not None:
                return amortization_only
            # T-159a rule 4 (F7): D&A filed as two face lines.
            plus = self._da_plus_intangibles_row(spec, column, value, concept)
            return plus if plus is not None else (value, concept, how)
        result = self._da_sourced_sum(spec, column)
        if result[0] is not None:
            return result
        cross = self._da_cross_statement_sum(spec, column)
        if cross[0] is not None:
            return cross
        if not self._da_filed_on_statement("cash_flow"):
            income_spec = replace(spec, statements=_INCOME)
            value, concept, how = self._component_row(income_spec, column)
            if value is None:
                value, concept, how = self._da_sourced_sum(income_spec, column)
            if value is not None:
                self._flag("da_income_statement")
                return value, concept, how
        caption = self._da_caption_fallback(spec, column)
        if caption[0] is not None:
            # Audit F-A9: a caption-read D&A ("D&A excluding intangible amortization", CRWD, IEX)
            # beside its own separate intangible-amortization line.
            plus = self._da_plus_intangibles_row(
                spec, column, caption[0], caption[1], caption_read=True
            )
            return plus if plus is not None else caption
        calibrated = self._da_calibrated_sum(spec, column)
        if calibrated is not None:
            return calibrated
        # T-159a: the exact two-part sum (ID-02b: a map with a source states the identity) --
        # ``Depreciation`` + ``AmortizationOfIntangibleAssets``, when both are filed for the period.
        # It is the last tier, so every read that resolves today resolves to the same value.
        exact = self._da_exact_sum(spec, column)
        return exact if exact[0] is not None else result

    def _da_calibrated_sum(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None] | None:
        """Audit F-A4: the D&A-nature face lines of the cash-flow statement (a depreciation line plus
        amortization / depletion lines, standard or extension, ``AdjustmentForAmortization`` captioned
        "Amortization" included) whose sum equals the filer's **own** filed aggregate D&A fact for
        the same period (``DepreciationDepletionAndAmortization`` & co., T-159b's filed facts). The
        filer's aggregate is the identity's source (the T-159c rule B calibration principle): AVY
        2025Q1 "Depreciation" 48.8 M + "Amortization" 29.1 M = its filed 77.9 M. Impairment,
        financing-cost and similar lines are never terms (MD-01). Exactly one matching subset, else
        nothing. Flagged ``da_calibrated_sum``."""
        period = next((p for p in self.periods if p.key == column), None)
        if period is None or not self.filed_facts:
            return None
        targets = set()
        for c in _DA_CALIBRATION_CONCEPTS:
            value, _status = self._filed_fact_value(spec, period, c)
            if value is not None:
                targets.add(value)
        if not targets:
            return None
        terms: list[tuple[str, float, bool]] = []
        for row in self._rows_for(spec):
            concept = str(row.get("concept") or "")
            label = str(row.get("label") or "")
            if not (_DA_NAME_RE.search(concept.split("_", 1)[-1]) or _DA_NAME_RE.search(label)):
                continue
            if _NOT_DA_AMORT_RE.search(label) or _NOT_DA_AMORT_RE.search(concept.split("_", 1)[-1]):
                continue
            value = self._read(row, column, spec)
            if value is None or value == 0:
                continue
            is_dep = concept == G + "Depreciation" or label.lower().startswith("depreciation")
            terms.append((concept, value, is_dep))
        matches = []
        for n in range(2, min(4, len(terms)) + 1):
            for combo in itertools.combinations(terms, n):
                if not any(t[2] for t in combo):
                    continue
                total = sum(t[1] for t in combo)
                if any(abs(total - x) <= _SAME_VALUE for x in targets):
                    matches.append(combo)
        if len(matches) != 1:
            return None
        self._flag("da_calibrated_sum")
        combo = matches[0]
        return sum(t[1] for t in combo), " + ".join(sorted(t[0] for t in combo)), "sourced_map"

    def _da_plus_intangibles_row(
        self,
        spec: LineItem,
        column: str,
        value: float,
        concept: str | None,
        *,
        caption_read: bool = False,
    ) -> tuple[float | None, str | None, str | None] | None:
        """T-159a rule 4 (F7): D&A filed as two face lines.

        When the D&A is the filed aggregate (``DepreciationDepletionAndAmortization``,
        ``DepreciationAndAmortization`` or ``DepreciationAmortizationAndAccretionNet``) **and**
        ``AmortizationOfIntangibleAssets`` is a **separate row of the served cash-flow statement**
        in the same column, the two are separate add-back lines of one reconciliation and the D&A
        is their sum (flagged ``da_plus_intangibles_row``). A row that is a child of the D&A row --
        an "of which" line, already inside it -- is not added.

        **HLT's exception** (reviewer B, 2026-10-22): the rule does not apply when the income
        statement of the same column carries the cash-flow D&A concept (or ``Depreciation``) at
        the same value and no ``AmortizationOfIntangibleAssets`` line: the D&A row is then the
        whole D&A expense and the other row lies outside it (HLT's "Amortization of contract
        acquisition costs"), so adding it would overstate the D&A and the EBITDA."""
        if concept not in DA_AGGREGATE_CONCEPTS and not caption_read:
            return None
        for row in self._rows_for(spec):
            row_concept = str(row.get("concept") or "")
            if row_concept == concept:
                continue
            label = str(row.get("label") or "")
            # Audit F-A9: the standard element, or an extension captioned as intangible amortization
            # (LHX, VRSK, BR) -- never an impairment, financing, inventory or contract-cost line.
            extension = (
                not row_concept.startswith("us-gaap_")
                and _INTANGIBLE_AMORT_RE.search(label) is not None
                and _NOT_DA_AMORT_RE.search(label) is None
            )
            if row_concept != G + "AmortizationOfIntangibleAssets" and not extension:
                continue
            if row.get("parent_concept") == concept:
                continue  # an "of which" line: it is already inside the D&A
            intangibles = self._read(row, column, spec)
            if intangibles is None:
                continue
            # T-161 r2: if the aggregate already equals Depreciation + amortization, it is the
            # full D&A and adding the amortization again would double it.
            depreciation = self._concept_value(spec, column, G + "Depreciation")
            if (
                depreciation is not None
                and abs(value - (depreciation + intangibles)) <= _SAME_VALUE
            ):
                return None
            if self._income_da_is_the_full_expense(column, value):
                return None
            self._flag("da_plus_intangibles_row")
            return (
                value + intangibles,
                f"{concept} + {row_concept}",
                "sourced_map",
            )
        return None

    def _da_amortization_only_sum(
        self, spec: LineItem, column: str, value: float, concept: str | None, how: str | None
    ) -> tuple[float | None, str | None, str | None] | None:
        """T-161: the mirror of ``depreciation_only_not_da`` (T-151 part 1), on the *other* partial
        reading.

        A filer tags its whole D&A on the 10-Qs but only the intangible amortization on the 10-K
        under an aggregate concept (NDSN FY2025: ``DepreciationDepletionAndAmortization`` =
        ``AmortizationOfIntangibleAssets`` = 79,264,000, with ``Depreciation`` 71,259,000 filed
        separately). The aggregate is then **not** the item: D&A = ``Depreciation`` +
        ``AmortizationOfIntangibleAssets``, both filed facts of the same filing and column.

        Only ``AmortizationOfIntangibleAssets`` is accepted: ``AdjustmentForAmortization`` is a
        parent that also carries financing-cost and debt-discount amortization, so it is never
        the D&A component (``DA_SOURCED_CHILDREN``'s comment, T-151 part 1). When the aggregate
        already equals ``Depreciation + amortization`` it **is** the full D&A and is kept.

        Returns ``None`` when the pattern does not hold (the caller keeps the value), and
        ``(None, None, None)`` when it holds but the depreciation fact is ambiguous (the caller
        refuses the mixed value, flagged ``da_amortization_only_refused``). Only an aggregate
        concept is considered, so a substitute or caption value that happens to equal the
        amortization cannot trigger it."""
        if concept not in DA_AGGREGATE_CONCEPTS:
            return None
        amortization = self._da_amortization_matching(spec, column, value)
        if amortization is None:
            return None
        depreciation = self._concept_value(spec, column, G + "Depreciation")
        if depreciation is None:
            return None
        new_value = depreciation + amortization
        if abs(new_value - value) <= _SAME_VALUE:
            # The aggregate already is Depreciation + amortization: the full D&A. Return it so
            # T-159a's rule 4 cannot add the amortization a second time.
            return value, concept, how
        self._flag("da_amortization_only_sum")
        return (
            new_value,
            f"{G}Depreciation + {G}AmortizationOfIntangibleAssets",
            "sourced_map",
        )

    def _da_amortization_matching(self, spec: LineItem, column: str, value: float) -> float | None:
        """The intangible amortization whose filed value equals *value*, if any."""
        amortization = self._concept_value(spec, column, G + "AmortizationOfIntangibleAssets")
        if amortization is not None and abs(value - amortization) <= _SAME_VALUE:
            return amortization
        return None

    def _da_amortization_only_sum_filed(  # noqa: PLR0911 - one return per guard
        self, spec: LineItem, period: Period, value: float, concept: str
    ) -> tuple[float | None, str | None, str | None] | None:
        """T-161, the ``filed_facts`` side of :meth:`_da_amortization_only_sum`.

        The same structural test against the filing's own non-dimensional filed facts: the
        aggregate D&A equals ``AmortizationOfIntangibleAssets`` for the period and
        ``Depreciation`` is filed separately. Two different ``Depreciation`` values for the
        period leave the sum unsourced: the item is refused, never stored as the amortization
        alone. When the aggregate already equals ``Depreciation + amortization`` it is kept."""
        if concept not in DA_AGGREGATE_CONCEPTS:
            return None
        amortization, status = self._filed_fact_value(
            spec, period, G + "AmortizationOfIntangibleAssets"
        )
        if status == "ambiguous" or amortization is None:
            return None
        if abs(value - amortization) > _SAME_VALUE:
            return None
        depreciation, dep_status = self._filed_fact_value(spec, period, G + "Depreciation")
        if dep_status == "ambiguous":
            self._flag("da_amortization_only_refused")
            return None, None, None
        if depreciation is None:
            return None
        new_value = depreciation + amortization
        if abs(new_value - value) <= _SAME_VALUE:
            return value, concept, "filed_fact"  # the aggregate already is the full D&A
        self._flag("da_amortization_only_sum")
        return (
            new_value,
            f"{G}Depreciation + {G}AmortizationOfIntangibleAssets",
            "sourced_map",
        )

    def _income_da_is_the_full_expense(self, column: str, da_value: float) -> bool:
        """Whether the income statement of *column* shows the cash-flow D&A row as the whole D&A
        expense (T-159a rule 4's HLT exception): it carries the D&A concept (or ``Depreciation``)
        at the same value and no ``AmortizationOfIntangibleAssets`` line."""
        income = self._income_statement_concepts(column)
        if G + "AmortizationOfIntangibleAssets" in income:
            return False
        return any(
            income.get(c) is not None and abs(income[c] - da_value) < _SAME_VALUE
            for c in (*DA_AGGREGATE_CONCEPTS, G + "Depreciation")
        )

    def _income_statement_concepts(self, column: str) -> dict[str, float]:
        """The income statement's concepts and their values in *column* (the non-dimensional,
        non-abstract rows)."""
        out: dict[str, float] = {}
        for row in self.raw.get("income_statement", []):
            if row.get("abstract") or row.get("dimension"):
                continue
            concept = str(row.get("concept") or "")
            value = _numeric(row.get(column))
            if concept and value is not None:
                out.setdefault(concept, value)
        return out

    def _da_exact_sum(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None]:
        """T-159a: D&A = ``Depreciation`` + ``AmortizationOfIntangibleAssets``.

        The two parts are the whole of the D&A a filer that presents them presents (ID-02b: a
        component map with a source states the identity), so the sum is read when **both** are
        filed for the period; one part alone leaves the item empty (a partial D&A is not D&A,
        N14/D7). The "only lines" condition of :meth:`_da_sourced_sum` does not apply here: the
        sum is of the two named parts, not of the statement's D&A-named lines. Flagged
        ``da_sourced_map``."""
        first = self._concept_value(spec, column, DA_EXACT_SUM[0])
        second = self._concept_value(spec, column, DA_EXACT_SUM[1])
        if first is None or second is None:
            return None, None, None
        self._flag("da_sourced_map")
        return (
            first + second,
            f"{DA_EXACT_SUM[0]} + {DA_EXACT_SUM[1]}",
            "sourced_map",
        )

    def _da_caption_fallback(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None]:
        """E3 (T-156, ID-01): a company extension on the cash-flow statement whose own caption names
        exactly depreciation and amortization, or equals the standard label of an aggregate D&A
        element the resolver accepts (:data:`DA_CAPTION_ELEMENTS`). Exactly one matching line, as the
        shared caption fallback requires; a negative value of an element ID-13 lists is rejected,
        never repaired (ID-11). Flagged ``caption_fallback``; ``extension_concept`` rides on the
        concept itself, and ``da_element_nonproduction`` when the caption names
        ``DepreciationAndAmortization`` -- the element's own flag, as if the standard element had
        been filed."""
        found: list[tuple[str, float, str]] = []
        for row in self._rows_for(spec):
            concept = str(row.get("concept") or "")
            if not concept or (
                concept.startswith(("us-gaap_", "dei_")) and concept not in DA_CAPTION_US_GAAP
            ):
                continue
            element = DA_CAPTION_ELEMENTS.get(_normalize_caption(str(row.get("label") or "")))
            if element is None:
                continue
            value = self._read(row, column)
            if value is None:
                continue
            if value < 0 and element in spec.nonnegative:
                self._flag(
                    "wrong_sign_rejected"
                    if _preferred_sign(row) is not None
                    else "sign_unknown_negative_rejected"
                )
                continue
            found.append((concept, value, element))
        if len(found) != 1:
            return None, None, None
        concept, value, element = found[0]
        self._flag("caption_fallback")
        if element == G + "DepreciationAndAmortization":
            self._flag("da_element_nonproduction")
        return value, concept, "label_fallback"

    def _da_filed_on_statement(self, statement: str) -> bool:
        """Whether *statement* carries any D&A-nature row with a value in any column (E1, T-156): a
        non-abstract, non-dimensional row whose concept's local name or caption names depreciation,
        amortization or depletion (:data:`_DA_NAME_RE`), the same scan
        :meth:`da_lines_outside_sourced` makes over the cash-flow lines."""
        for row in self.raw.get(statement, []):
            if row.get("abstract") or row.get("dimension"):
                continue
            concept = str(row.get("concept") or "")
            label = str(row.get("label") or "")
            if not (_DA_NAME_RE.search(concept.split("_", 1)[-1]) or _DA_NAME_RE.search(label)):
                continue
            if any(_numeric(row.get(period.key)) is not None for period in self.periods):
                return True
        return False

    def _da_sourced_sum(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None]:
        """The sourced D&A sum (C1, ID-02b; T-151 part 1). Read only when none of the
        ``depreciation_amortization`` concepts is filed in the column.

        Conditions: (a) ``Depreciation`` is filed -- without it the sum is not D&A and
        ``depreciation_only_not_da`` / empty stays as today (:data:`DA_SOURCED_CHILDREN`'s
        ``unread_flags``); (b) at least one amortization or depletion child is filed
        (``Depletion``, ``OtherDepreciationAndAmortization``, ``AmortizationOfIntangibleAssets``) --
        depreciation alone stays refused; (c) **only lines**: every other cash-flow line whose
        concept or caption names depreciation, amortization or depletion must be one of the four
        :data:`DA_SOURCED_CHILDREN` -- any other (an outside amortization of
        ``AdjustmentForAmortization``, ``AdjustmentForAmortization`` itself, an extension D&A line)
        refuses the sum, flagged ``da_sum_refused_other_lines``. Each child keeps its filed sign
        (ID-12). Flagged ``da_sourced_sum``."""
        children = DA_SOURCED_CHILDREN
        depreciation = self._concept_value(spec, column, G + "Depreciation")
        if depreciation is None:
            return None, None, None
        amortization_or_depletion = [
            c
            for c in children
            if c != G + "Depreciation" and self._concept_value(spec, column, c) is not None
        ]
        if not amortization_or_depletion:
            return None, None, None
        if self.da_lines_outside_sourced(spec):
            self._flag("da_sum_refused_other_lines")
            return None, None, None
        filed = {c: self._concept_value(spec, column, c) for c in children}
        self._flag("da_sourced_sum")
        return (
            sum(v for v in filed.values() if v is not None),
            " + ".join(sorted(c for c, v in filed.items() if v is not None)),
            "map",
        )

    def _da_cross_statement_sum(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None]:
        """E2 (T-156): the sourced D&A sum with its amortization term read on the income statement.

        N14/D7 stays -- depreciation alone is never D&A -- so the sum is formed only when a
        D&A-nature amortization or depletion child is filed (condition (b), read on the income
        statement: E1's reading) and the cash-flow statement's other D&A-named lines are the four
        children (condition (c), exactly as :meth:`_da_sourced_sum` states it). ``Depreciation``
        itself stays the cash-flow statement's row. Flagged ``da_sum_cross_statement``; each term
        keeps its filed sign (ID-12). A refusal here raises no flag: the filing is simply not
        recovered."""
        depreciation = self._concept_value(spec, column, G + "Depreciation")
        if depreciation is None:
            return None, None, None
        income_spec = replace(spec, statements=_INCOME)
        amortization = [
            c
            for c in DA_SOURCED_CHILDREN
            if c != G + "Depreciation" and self._concept_value(income_spec, column, c) is not None
        ]
        if not amortization:
            return None, None, None
        if self.da_lines_outside_sourced(spec):
            return None, None, None
        filed: dict[str, float | None] = {G + "Depreciation": depreciation}
        for child in amortization:
            filed[child] = self._concept_value(income_spec, column, child)
        self._flag("da_sum_cross_statement")
        return (
            sum(v for v in filed.values() if v is not None),
            " + ".join(sorted(c for c, v in filed.items() if v is not None)),
            "map",
        )

    def da_lines_outside_sourced(self, spec: LineItem) -> list[str]:
        """The cash-flow lines whose concept or caption names depreciation, amortization or
        depletion yet are not one of :data:`DA_SOURCED_CHILDREN` (C1 (c), "only lines"): a
        non-empty list refuses the sourced sum. Matched on the concept's local name or the row's
        caption, case-insensitively."""
        outside: list[str] = []
        for row in self._rows_for(spec):
            concept = str(row.get("concept") or "")
            if concept in DA_SOURCED_CHILDREN:
                continue
            local = concept.split("_", 1)[-1]
            label = str(row.get("label") or "")
            if (_DA_NAME_RE.search(local) or _DA_NAME_RE.search(label)) and concept not in outside:
                outside.append(concept)
        return outside

    def _capex(self, spec: LineItem, column: str) -> tuple[float | None, str | None, str | None]:
        """Capex composite resolver under MET-08 and APP-04b (T-148 (f))."""
        if self.reit:
            return self._capex_reit(spec, column)
        return self._capex_non_reit(spec, column)

    def _capex_reit(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None]:
        """Equity-REIT capex (APP-04b, T-148 (f); extended by T-071 part 2b's D6).

        APP-04b: capex = corporate items + existing properties + development + acquisitions, summed
        under MET-08 (c). The **real-estate acquisition part** follows the FASB 2026 calculation tree
        (``docs/checklist_sec/sec_xcheck/fasb_2026_dl/x/us-gaap-2026``,
        ``stm/us-gaap-stm-scf-indir-cal-2026.xml``): ``PaymentsToAcquireRealEstate`` is the **parent**
        of ``PaymentsToAcquireCommercialRealEstate`` and ``PaymentsForCapitalImprovements``. So when
        the parent is filed it is used **alone** -- APP-04b's own sentence, capital improvements are
        inside it -- and a filed child is flagged ``capital_improvements_overlap_not_added``; when the
        parent is not filed, the filed children are added, because siblings in a calculation sum to
        the parent and are therefore explicitly disjoint (MET-08 (b)), flagged ``reit_sibling_sum``.

        The **extension sum** reads the filer's own "capital expenditures" lines the concept map has
        no element for -- :meth:`_reit_extension_capex`, flagged ``reit_extension_sum``."""
        used = self._reit_components(spec, column)
        if used:
            extensions = self._reit_extension_capex(spec, column)
            if extensions and self._extension_is_a_total(extensions, list(used.values())):
                self._flag("reit_extension_total_present")
                extensions = {}
            if extensions:
                self._flag("caption_fallback")
                self._flag("reit_extension_sum")
                used.update({c: v for c, v in extensions.items() if c not in used})
            self._flag("reit_components")
            self._flag("fcf_non_standard_reit")
            total = sum(used.values())
            concepts_str = " + ".join(sorted(used.keys()))
            how = "map" if len(used) >= MIN_SUMMED else "concept"
            return total, concepts_str, how

        # nothing filed -> PaymentsToAcquireProductiveAssets as in tier 5
        for prod_c in (
            G + "PaymentsToAcquireProductiveAssets",
            G + "PaymentsToAcquireOtherProductiveAssets",
        ):
            prod = self._concept_value(spec, column, prod_c)
            if prod is not None:
                self._flag("productive_assets_fallback")
                self._flag("fcf_non_standard_reit")
                return prod, prod_c, "fallback"

        return None, None, None

    def _reit_components(self, spec: LineItem, column: str) -> dict[str, float]:
        """APP-04b's us-gaap REIT components (corporate, development, held-for-investment) and the
        real-estate acquisition part, by the FASB 2026 calculation tree.

        ``PaymentsToAcquireRealEstate`` is the **parent** of ``PaymentsToAcquireCommercialRealEstate``
        and ``PaymentsForCapitalImprovements``: a filed parent is used alone (APP-04b's sentence --
        capital improvements are inside it) and a filed child is flagged
        ``capital_improvements_overlap_not_added``; when the parent is not filed, the filed children
        are added, because siblings in a calculation sum to the parent and are therefore explicitly
        disjoint (MET-08 (b)), flagged ``reit_sibling_sum``."""
        parent = G + "PaymentsToAcquireRealEstate"
        children = (
            G + "PaymentsToAcquireCommercialRealEstate",
            G + "PaymentsForCapitalImprovements",
        )
        used: dict[str, float] = {}
        for c in (
            G + "PaymentsToAcquirePropertyPlantAndEquipment",
            G + "PaymentsToAcquireRealEstateHeldForInvestment",
            G + "PaymentsToDevelopRealEstateAssets",
        ):
            val = self._concept_value(spec, column, c)
            if val is not None and val != 0:
                used[c] = val

        parent_value = self._concept_value(spec, column, parent)
        if parent_value is not None and parent_value != 0:
            used[parent] = parent_value
            if any(self._concept_value(spec, column, c) is not None for c in children):
                self._flag("capital_improvements_overlap_not_added")
        else:
            siblings = {
                c: v
                for c in children
                if (v := self._concept_value(spec, column, c)) is not None and v != 0
            }
            if siblings:
                self._flag("reit_sibling_sum")
                used.update(siblings)
        return used

    @staticmethod
    def _extension_is_a_total(extensions: dict[str, float], taken: list[float]) -> bool:
        """DQ-01 (T-071 part 2b, D6's ruling): whether a us-gaap line the composite already takes
        **is** the extension lines' total -- it equals their sum, or that sum plus another taken
        line -- in which case the extension lines are its *parts*, not separate outflows, and must
        not be added. The tolerance is :func:`dq01_tolerance` (``max(L-10 rounding, 0.5% x |base|)``)
        with the parts' count as ``n_terms``."""
        ext_sum = sum(extensions.values())
        parts = len(extensions)
        for i, value in enumerate(taken):
            if abs(value - ext_sum) <= dq01_tolerance(value, parts + 1):
                return True
            for j, other in enumerate(taken):
                if i != j and abs(value - (ext_sum + other)) <= dq01_tolerance(value, parts + 2):
                    return True
        return False

    def _reit_extension_capex(self, spec: LineItem, column: str) -> dict[str, float]:
        """The REIT-only sum of the capex caption's **company-extension** lines (T-071 part 2b, D6).

        The shared caption fallback (:meth:`_label_fallback_row`) refuses more than one matching line
        -- "one caption, one line": a partial figure is worse than none. Inside the REIT composite the
        opposite holds: APP-04b's capex is the sum of the components the filer presents, and FRT's
        "Capital expenditures - development and redevelopment" beside "Capital expenditures - other"
        are two of them. So when the caption matches several lines, every match is summed that is

        * a **company extension** -- never a ``us-gaap``/``dei`` concept, which stays with the concept
          map (VTR's "Other capital expenditures" = ``PaymentsToAcquireOtherProductiveAssets`` is
          therefore not taken here);
        * a line of this cash-flow statement's own face (``_rows_for``: non-abstract,
          non-dimensional -- the store records no section, so "the investing section" is the capex
          spec's cash-flow scope) whose caption is not one of proceeds, a sale, a disposition or a
          gain;
        * **presented as an outflow**: ``preferred_sign = -1``, the negated label the investing
          outflows carry. This refuses a non-cash accrual (CPT's
          ``cpt_AccrualAssociatedWithConstructionAndCapitalExpenditures``, ``+1``) without a new
          caption word;
        * **cash and not a mirror**: neither the concept's local name nor its caption names an
          accrual, an amount still unpaid or a period-end mirror
          (:data:`_NOT_AN_EXTENSION_CAPEX_RE`);

        and only when no us-gaap line already taken is their total (:meth:`_extension_is_a_total`).

        Flagged ``reit_extension_sum`` (and ``caption_fallback``; ``extension_concept`` rides from the
        resolution). The one-line rule is unchanged for every other filer."""
        if not spec.label_fallback:
            return {}
        wanted = re.compile(spec.label_fallback, re.IGNORECASE)
        unwanted = re.compile(spec.label_fallback_exclude or r"(?!)", re.IGNORECASE)
        out: dict[str, float] = {}
        for row in self._rows_for(spec):
            concept = str(row.get("concept") or "")
            if concept.startswith(("us-gaap_", "dei_")):
                continue
            label = str(row.get("label") or "")
            if not wanted.search(label) or unwanted.search(label):
                continue
            if _NOT_A_CAPEX_PAYMENT_RE.search(label):
                continue
            if _preferred_sign(row) != -1.0:
                continue
            if _NOT_AN_EXTENSION_CAPEX_RE.search(concept.split("_", 1)[-1]):
                continue
            if _NOT_AN_EXTENSION_CAPEX_RE.search(label):
                continue
            value = self._read(row, column, spec)
            if value is not None:
                out.setdefault(concept, value)
        return out

    def _capex_tier1(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None] | None:
        tier1_concepts = (
            G + "PaymentsToAcquirePropertyPlantAndEquipment",
            G + "PaymentsForCapitalImprovements",
        )
        for row in self._rows_for(spec):
            concept = row.get("concept")
            if concept in tier1_concepts:
                val = self._read(row, column, spec)
                if val is not None:
                    if concept == G + "PaymentsForCapitalImprovements":
                        # Audit F-A8 (MET-08 (c)): beside an oil & gas development line the capital
                        # improvements line is the "other fixed asset additions" of the same
                        # statement (CTRA 2022-24: 10 M beside 1,700 M), so the two are added.
                        og = self._concept_value(
                            spec, column, G + "PaymentsToExploreAndDevelopOilAndGasProperties"
                        )
                        if og is not None and og != 0:
                            self._flag("ppe_family_plus_og_added")
                            used = (
                                G + "PaymentsForCapitalImprovements",
                                G + "PaymentsToExploreAndDevelopOilAndGasProperties",
                            )
                            return val + og, " + ".join(sorted(used)), "map"
                    return val, str(concept), "concept"
        return None

    def _capex_ppe_and_og(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None] | None:
        ppe_concepts = (
            G + "PaymentsToAcquireMachineryAndEquipment",
            G + "PaymentsToAcquireFurnitureAndFixtures",
            G + "PaymentsToAcquireOtherPropertyPlantAndEquipment",
        )
        ppe_values = {
            c: v
            for c in ppe_concepts
            if (v := self._concept_value(spec, column, c)) is not None and v != 0
        }
        og_finer_concepts = (
            G + "PaymentsToExploreAndDevelopOilAndGasProperties",
            G + "PaymentsToAcquireOilAndGasEquipment",
        )
        og_values = {
            c: v
            for c in og_finer_concepts
            if (v := self._concept_value(spec, column, c)) is not None and v != 0
        }
        if not og_values and ppe_values:
            val_og_ppe = self._concept_value(
                spec, column, G + "PaymentsToAcquireOilAndGasPropertyAndEquipment"
            )
            if val_og_ppe is not None and val_og_ppe != 0:
                og_values[G + "PaymentsToAcquireOilAndGasPropertyAndEquipment"] = val_og_ppe
        if ppe_values and og_values:
            self._flag("ppe_family_plus_og_added")
            if G + "PaymentsToAcquireOilAndGasPropertyAndEquipment" in og_values:
                self._flag("includes_acquisitions_contrary_to_a")
            all_used = {**ppe_values, **og_values}
            return sum(all_used.values()), " + ".join(sorted(all_used.keys())), "map"
        if ppe_values:
            if len(ppe_values) > 1:
                self._flag("ppe_family_lines_added")
                return sum(ppe_values.values()), " + ".join(sorted(ppe_values.keys())), "map"
            c, v = next(iter(ppe_values.items()))
            self._flag("ppe_family_line")
            return v, c, "concept"
        if og_values:
            if len(og_values) > 1:
                self._flag("og_tie_break_c_added")
                return sum(og_values.values()), " + ".join(sorted(og_values.keys())), "map"
            c, v = next(iter(og_values.items()))
            return v, c, "concept"
        return None

    def _capex_fallback_tiers(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None] | None:
        val4 = self._concept_value(
            spec, column, G + "PaymentsToAcquireOilAndGasPropertyAndEquipment"
        )
        if val4 is not None:
            self._flag("includes_acquisitions_contrary_to_a")
            return val4, G + "PaymentsToAcquireOilAndGasPropertyAndEquipment", "fallback"
        for prod_c in (
            G + "PaymentsToAcquireProductiveAssets",
            G + "PaymentsToAcquireOtherProductiveAssets",
        ):
            val5 = self._concept_value(spec, column, prod_c)
            if val5 is not None:
                self._flag("productive_assets_fallback")
                return val5, prod_c, "fallback"
        val6, caption6 = self._net_element(spec, column)
        if val6 is not None:
            if _NET_ELEMENT_PROCEEDS_RE.search(caption6):
                # C3 addition (T-151 part 1): a line captioned as proceeds, a sale or a disposition
                # is net proceeds whatever its filed sign -- COP's 2021-03-31 comparative is
                # displayed -17,000,000 with ``preferred_sign`` -1, a filed +17,000,000 the sign
                # test alone would read as capex. Skipped for the caption fallback.
                self._flag("net_element_captioned_proceeds")
            elif val6 <= 0:
                # C3 (MET-08, T-151 part 1): a net element of zero or below is net **proceeds**,
                # not capex ("PaymentsForProceedsFrom..." is a net line). The tier is skipped and
                # the caption fallback is tried; never repaired with abs() (ID-11).
                self._flag("net_element_net_proceeds")
            else:
                self._flag("net_element_last_resort")
                return val6, G + "PaymentsForProceedsFromProductiveAssets", "fallback"
        val7, concept7, how7 = self._label_fallback_row(
            spec, column, exclude=(G + "PaymentsForProceedsFromProductiveAssets",)
        )
        if val7 is not None:
            self._flag("caption_fallback")
            return val7, concept7, how7
        return None

    def _net_element(self, spec: LineItem, column: str) -> tuple[float | None, str]:
        """The filed value of ``PaymentsForProceedsFromProductiveAssets`` in *column* and its
        caption (C3, T-151 part 1): the caption is what tells a net **payment** from net proceeds
        when the sign alone cannot (a negating label displays a payment negative)."""
        for row in self._rows_for(spec):
            if row.get("concept") == G + "PaymentsForProceedsFromProductiveAssets":
                value = self._read(row, column, spec)
                if value is not None:
                    return value, str(row.get("label") or "")
        return None, ""

    def _capex_non_reit(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None]:
        res = self._capex_tier1(spec, column)
        if res is not None:
            return res
        res = self._capex_ppe_and_og(spec, column)
        if res is not None:
            return res
        res = self._capex_fallback_tiers(spec, column)
        if res is not None:
            return res
        return None, None, None

    def _long_term_debt(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None]:
        """Long-term debt (MET-07, ID-02a, C2; T-151 part 1).

        The narrowest filed line first (ID-02a): ``LongTermDebtNoncurrent`` (excludes the current
        maturities), then ``LongTermDebt`` (they are included, flagged
        ``includes_current_maturities``), then the combined debt-and-lease elements, used only
        because no narrower line is filed and flagged ``includes_finance_leases``.

        When **none** of those four is filed, the two sourced sums of :data:`LONG_TERM_DEBT_CHILDREN`
        are tried (:meth:`_long_term_debt_sum`): the filed children of ``LongTermDebtNoncurrent``
        (the sfp-cls arc, excluding current maturities), else the filed children of ``LongTermDebt``
        (the sfp-dbo arc, including them, flagged ``includes_current_maturities``). Both need
        **every** debt-named balance-sheet line to be a child of the tree used or a short-term map
        concept, else the sum is refused (``debt_sum_refused_other_lines``) and the debt stays
        empty. A line that includes the current maturities raises ``includes_current_maturities``:
        no current-maturity term is added to it (:meth:`_short_term_parts`, N4)."""
        for concept in (G + "LongTermDebtNoncurrent", G + "LongTermDebt"):
            value = self._concept_value(spec, column, concept)
            if value is not None:
                if concept in INCLUDES_CURRENT_MATURITIES:
                    self._flag("includes_current_maturities")
                return value, concept, "concept"
        for concept in (
            G + "LongTermDebtAndCapitalLeaseObligations",
            G + "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
        ):
            value = self._concept_value(spec, column, concept)
            if value is not None:
                self._flag("includes_finance_leases")
                if concept in INCLUDES_CURRENT_MATURITIES:
                    self._flag("includes_current_maturities")
                return value, concept, "concept"
        return self._long_term_debt_sum(spec, column)

    def _long_term_debt_sum(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None]:
        """The two sourced long-term-debt sums (C2, ID-02b; T-151 part 1).

        Read in order only when no concept :meth:`_long_term_debt` reads today is filed:
        (i) the filed children of ``LongTermDebtNoncurrent`` (the sfp-cls arc; excludes the current
        maturities); else (ii) the filed children of ``LongTermDebt`` (the sfp-dbo arc; includes
        them, flagged ``includes_current_maturities``). "Only lines": every debt-named balance-sheet
        line (:data:`_DEBT_LINE_RE` and not :data:`_NOT_A_DEBT_LINE_RE`, as the zero rule uses) must
        be a child of the tree used or a short-term map concept, else the sum is refused
        (``debt_sum_refused_other_lines``) and the debt stays empty as today. Flagged
        ``debt_sourced_sum``."""
        for children, current_maturities in (
            (LONG_TERM_DEBT_NONCURRENT_CHILDREN, False),
            (LONG_TERM_DEBT_CHILDREN, True),
        ):
            filed = {
                c: v for c in children if (v := self._concept_value(spec, column, c)) is not None
            }
            if not filed:
                continue
            if self.debt_lines_outside(children):
                self._flag("debt_sum_refused_other_lines")
                return None, None, None
            self._flag("debt_sourced_sum")
            if current_maturities:
                self._flag("includes_current_maturities")
            return sum(filed.values()), " + ".join(sorted(filed)), "map"
        # T-159a: the debt components a filer presents instead of a tree total (MET-07 / ID-02b).
        # They enter the same sum -- never a free alternative -- and the "only lines" condition
        # holds for them as it holds for the tree's children. A component's filed children count
        # when the component itself is not filed (the family rule); each concept counts once.
        filed = self._long_term_components(spec, column)
        if not filed:
            return None, None, None
        if self.debt_lines_outside(LONG_TERM_DEBT_COMPONENTS):
            self._flag("debt_sum_refused_other_lines")
            return None, None, None
        self._flag("debt_sourced_sum")
        return sum(filed.values()), " + ".join(sorted(filed)), "map"

    def _long_term_components(self, spec: LineItem, column: str) -> dict[str, float]:
        """The filed debt components of the long-term debt sum (T-159a): the components themselves
        and, for a component that is not filed, its filed children (the family rule). Each concept
        counts once, and a child is never added beside its parent (the components are read widest
        first)."""
        filed: dict[str, float] = {}
        blocked: set[str] = set()
        for component in LONG_TERM_DEBT_COMPONENTS:
            if component in blocked:
                continue
            value = self._concept_value(spec, column, component)
            if value is not None:
                filed[component] = value
                blocked.update(self._component_descendants(component))
                continue
            for child in LONG_TERM_DEBT_COMPONENT_CHILDREN.get(component, ()):
                if child in filed or child in blocked:
                    continue
                child_value = self._concept_value(spec, column, child)
                if child_value is not None:
                    filed[child] = child_value
                    blocked.update(self._component_descendants(child))
        return filed

    def _component_descendants(self, node: str) -> set[str]:
        """The components' children, transitively: a filed node's descendants are inside it and are
        never added beside it (the taxonomy's arcs)."""
        out: set[str] = set()
        for child in LONG_TERM_DEBT_COMPONENT_CHILDREN.get(node, ()):
            if child in out:
                continue
            out.add(child)
            out.update(self._component_descendants(child))
        return out

    def debt_lines_outside(self, children: tuple[str, ...]) -> list[str]:
        """The debt-named balance-sheet lines that are not a child of *children* nor a short-term
        map concept (C2's "only lines", ID-02b): an empty list is what lets the sourced sum stand.
        A line the filer put on the balance sheet counts whatever the gateway left of its value,
        the same scan the debt = 0 rule uses (:meth:`_has_debt_line`)."""
        allowed = (
            set(children)
            | {c for _term, concepts in SHORT_TERM_DEBT_MAP for c in concepts}
            | {c for kids in SHORT_TERM_DEBT_CHILDREN.values() for c in kids}
            | {c for kids in LONG_TERM_DEBT_COMPONENT_CHILDREN.values() for c in kids}
            | {SHORT_TERM_DEBT_LAST}
        )
        outside: list[str] = []
        for row in self._statement_rows("balance_sheet"):
            concept = str(row.get("concept") or "")
            local = concept.split("_", 1)[-1]
            if not _DEBT_LINE_RE.search(local) or _NOT_A_DEBT_LINE_RE.search(local):
                continue
            if concept not in allowed and concept not in outside:
                outside.append(concept)
        return outside

    def _short_term_parts(
        self, spec: LineItem, column: str, *, current_maturities_in_long_term: bool
    ) -> dict[str, tuple[float, str]]:
        """The filed terms of the short-term debt map (D-06(d), D-01; MET-07): for each term of
        :data:`SHORT_TERM_DEBT_MAP` the first of its concepts that is filed, as
        ``{term: (value, concept)}``. The current-maturities term is left out when the long-term
        line already contains the current maturities (N4).

        T-159a rule 1: the combined debt-and-lease element is one of the current-maturities
        concepts (flagged ``includes_finance_leases``). A child is never read beside its parent:
        ``NotesPayableCurrent`` is not read when ``NotesAndLoansPayableCurrent`` is filed (the
        parent contains it)."""
        parts: dict[str, tuple[float, str]] = {}
        for term, concepts in SHORT_TERM_DEBT_MAP:
            if term == "current_maturities" and current_maturities_in_long_term:
                continue
            for concept in concepts:
                if concept == G + "NotesPayableCurrent" and (
                    self._concept_value(spec, column, G + "NotesAndLoansPayableCurrent") is not None
                ):
                    continue  # a child is never added beside its parent
                value = self._concept_value(spec, column, concept)
                if value is not None:
                    if concept == G + "LongTermDebtAndCapitalLeaseObligationsCurrent":
                        self._flag("includes_finance_leases")
                    parts[term] = (value, concept)
                    break
        return parts

    def _short_term_components(
        self,
        spec: LineItem,
        column: str,
        parts: dict[str, tuple[float, str]],
        *,
        current_maturities_in_long_term: bool,
    ) -> dict[str, tuple[float, str]]:
        """T-159a rules 2 and 3: the term's calculation children.

        When no concept the term reads today is filed, the term is the **sum of its filed
        calculation children** (:data:`SHORT_TERM_DEBT_CHILDREN`); when the term is read from one of
        them (``CommercialPaper``), the other filed children are added beside it. Each family counts
        once (the node when it is filed, else the sum of its filed children) and a child is never
        added beside its parent. Flagged ``debt_components_sum``."""
        out = dict(parts)
        for term, root in SHORT_TERM_DEBT_ROOTS.items():
            if term == "current_maturities" and current_maturities_in_long_term:
                continue
            own = parts.get(term)
            if own is not None and own[1] in SHORT_TERM_DEBT_CONTAINERS[term]:
                continue  # the filed line contains the whole tree
            blocked: set[str] = set()
            if own is not None:
                blocked.add(own[1])  # the line already read
            if term == "current_maturities" and parts.get("borrowings", ("", ""))[1] == (
                G + "NotesPayableCurrent"
            ):
                # the notes payable are the borrowings term's line: the family that contains them
                # is not added to the current maturities (a child is never added beside its parent)
                blocked.add(G + "NotesAndLoansPayableCurrent")
            total, used = self._tree_total(spec, column, root, blocked)
            if not used:
                continue
            self._flag("debt_components_sum")
            if own is None:
                out[term] = (total, " + ".join(sorted(used)))
            else:
                out[term] = (
                    own[0] + total,
                    f"{own[1]} + " + " + ".join(sorted(used)),
                )
        return out

    def _tree_total(
        self, spec: LineItem, column: str, node: str, blocked: set[str]
    ) -> tuple[float, list[str]]:
        """The value of a calculation node: the node itself when it is filed, else the sum of its
        filed children (each family once, T-159a rules 2 and 3)."""
        if node in blocked:
            return 0.0, []
        value = self._concept_value(spec, column, node)
        if value is not None:
            return value, [node]
        total = 0.0
        used: list[str] = []
        for child in SHORT_TERM_DEBT_CHILDREN.get(node, ()):
            child_value, child_used = self._tree_total(spec, column, child, blocked)
            total += child_value
            used.extend(child_used)
        return total, used

    def _long_term_with_flags(
        self, column: str
    ) -> tuple[float | None, str | None, str | None, list[str]]:
        """The long-term debt resolution and the flags it raised (kept apart from the caller's).

        T-159c: when the statements give nothing, the filed-fact fallback fills the term here too,
        so ``total_debt`` (which calls this method directly, not ``resolve``) sees it."""
        outer, self._flags = self._flags, []
        try:
            value, concept, how = self._long_term_debt(REGISTRY["long_term_debt"], column)
            if value is None:
                fallback = self._filed_fact_row(REGISTRY["long_term_debt"], column, instant=True)
                if fallback is not None:
                    value, concept, how = fallback
                    self._flag("filed_fact")
            return value, concept, how, list(self._flags)
        finally:
            self._flags = outer

    def _short_term(
        self, spec: LineItem, column: str, *, in_long_term: bool
    ) -> tuple[float | None, str | None, dict[str, tuple[float, str]], bool]:
        """The short-term side of the debt map, shared by :meth:`_short_term_debt` and
        :meth:`_total_debt`: ``(value, concept, filed terms, used DebtCurrent)``. The filed terms
        of :data:`SHORT_TERM_DEBT_MAP` added, with their calculation children (T-159a rules 2 and
        3); ``DebtCurrent`` -- "debt and lease obligation" -- only when no term is filed, flagged
        ``includes_leases``. Against a long-term line that includes the current maturities
        (*in_long_term*) neither the current-maturities term nor ``DebtCurrent`` (which contains
        them) is added (N4)."""
        parts = self._short_term_parts(spec, column, current_maturities_in_long_term=in_long_term)
        parts = self._short_term_components(
            spec, column, parts, current_maturities_in_long_term=in_long_term
        )
        if parts:
            values = [v for v, _c in parts.values()]
            concepts = [c for _v, c in parts.values()]
            return sum(values), " + ".join(concepts), parts, False
        if not in_long_term:
            value = self._concept_value(spec, column, SHORT_TERM_DEBT_LAST)
            if value is not None:
                self._flag("includes_leases")
                return value, SHORT_TERM_DEBT_LAST, {}, True
        # T-159c: the filed facts fill the map when the served rows give nothing. The term the
        # concept belongs to is kept, so total_debt's coverage test reads the same map.
        if in_long_term:
            self._flag("current_maturities_in_long_term")
        fallback = self._filed_fact_row(REGISTRY["short_term_debt"], column, instant=True)
        if fallback is not None:
            value, concept, _how = fallback
            self._flag("filed_fact")
            return value, concept or "", self._filed_fact_short_term_terms(concept or ""), False
        return None, None, {}, False

    @staticmethod
    def _filed_fact_short_term_terms(concept: str) -> dict[str, tuple[float, str]]:
        """The term(s) a filed-fact short-term concept belongs to, for total_debt's coverage."""
        terms: dict[str, tuple[float, str]] = {}
        parts = [part.strip() for part in re.split(r"\s+\+\s+", concept) if part.strip()]
        for part in parts:
            for term, concepts in SHORT_TERM_DEBT_MAP:
                if part in concepts or part in SHORT_TERM_DEBT_CHILDREN.get(
                    SHORT_TERM_DEBT_ROOTS[term], ()
                ):
                    terms[term] = (0.0, part)
                    break
        return terms

    def _short_term_debt(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None]:
        """Short-term debt (MET-07, D-06(d)): :meth:`_short_term` against the long-term line the
        resolver reads (N4: ``current_maturities_in_long_term`` flagged when it includes them)."""
        *_lt, lt_flags = self._long_term_with_flags(column)
        in_long_term = "includes_current_maturities" in lt_flags
        if in_long_term:
            self._flag("current_maturities_in_long_term")
        value, concept, _parts, _used_debt_current = self._short_term(
            spec, column, in_long_term=in_long_term
        )
        if value is None or concept is None:
            return None, None, None
        if "filed_fact" in self._flags:
            # T-159c: the map's value came from the filing's own filed facts, not the served rows.
            return value, concept, "filed_fact"
        return value, concept, "map" if " + " in concept else "concept"

    def total_debt(self, period_key: str) -> Resolution:
        """Total debt for *period_key* (MET-07, ID-02b, D-01; MET-00): long-term plus short-term.

        * both filed -> their sum; one term of the map not filed -> the filed terms, flagged
          ``partial_map``, **only** when long-term debt is present and the rest of the balance
          sheet resolved (total assets and equity); otherwise empty, ``partial_map_rejected``;
        * short-term debt alone -> empty (``short_term_debt_only``): it suggests an unrecognized
          long-term concept;
        * no debt line at all -> **0**, flagged ``no_debt_lines``, only when the balance sheet
          resolved completely (total assets, equity and cash), no debt concept is filed on it and
          no interest-expense concept on the income statement (the variants the registry does not
          read included); otherwise
          empty, flagged ``capture_failure`` (N23). A missing debt is never read as 0 (MET-00).
        """
        outer, self._flags = self._flags, []
        try:
            column = self._instant_for(period_key)
            if column is None:
                return Resolution(None)
            value, concept, how = self._total_debt(period_key, column)
            if self.audit_debt_add:
                extra = {}
                for row in self.raw.get("balance_sheet", []):
                    con = str(row.get("concept") or "")
                    if con in self.audit_debt_add and con not in (concept or "").split(" + "):
                        v = _numeric(row.get(column))
                        if v is not None:
                            extra.setdefault(con, abs(v))
                if extra:
                    if value is None:
                        # the verified lines are the total: the reading's refusal flags no longer hold
                        self._flags = [
                            f
                            for f in self._flags
                            if f
                            not in (
                                "capture_failure",
                                "partial_map_rejected",
                                "short_term_debt_only",
                                "debt_sum_refused_other_lines",
                            )
                        ]
                    self._flag("audit_debt_lines_added")
                    value = (value or 0.0) + sum(extra.values())
                    concept = " + ".join([c for c in [concept] if c] + sorted(extra))
                    how = "map"
            return Resolution(value, concept, how, tuple(self._flags))
        finally:
            self._flags = outer

    def _balance_sheet_resolved(self, period_key: str, *, with_cash: bool = False) -> bool:
        """The rest of the balance sheet resolved (D-01 (ii), ID-02b): total assets and equity -- the
        denominators the debt ratios divide by -- and, for the zero of "no debt line at all", cash too
        (a company with no debt must show a whole balance sheet). A missing cash is a capture failure
        of its own: it empties the enterprise value and ROIC (MET-00) but not the debt."""
        items = ("total_assets", "equity", "cash") if with_cash else ("total_assets", "equity")
        return all(self.resolve(item, period_key).value is not None for item in items)

    def _total_debt(
        self, period_key: str, column: str
    ) -> tuple[float | None, str | None, str | None]:
        lt_value, lt_concept, _how, lt_flags = self._long_term_with_flags(column)
        for flag in lt_flags:
            self._flag(flag)
        in_long_term = "includes_current_maturities" in lt_flags
        if in_long_term:
            self._flag("current_maturities_in_long_term")
        st_value, st_concept, parts, used_debt_current = self._short_term(
            REGISTRY["short_term_debt"], column, in_long_term=in_long_term
        )

        if lt_value is None and st_value is None:
            return self._no_debt_line(period_key)
        if lt_value is None:
            self._flag("short_term_debt_only")
            self._flag("capture_failure")
            return None, st_concept, None
        covered = {
            "current_maturities": in_long_term
            or "current_maturities" in parts
            or used_debt_current,
            "borrowings": "borrowings" in parts or used_debt_current,
        }
        concept = " + ".join(c for c in (lt_concept, st_concept) if c)
        if all(covered.values()):
            return lt_value + (st_value or 0.0), concept, "map"
        if not self._balance_sheet_resolved(period_key):
            self._flag("partial_map_rejected")
            self._flag("capture_failure")
            return None, concept, None
        self._flag("partial_map")
        return lt_value + (st_value or 0.0), concept, "map"

    def _no_debt_line(self, period_key: str) -> tuple[float | None, str | None, str | None]:
        """ID-02b: no long-term and no short-term debt resolved."""
        if (
            self._balance_sheet_resolved(period_key, with_cash=True)
            and not self._has_debt_line()
            and not self._has_interest_expense_line()
        ):
            self._flag("no_debt_lines")
            return 0.0, None, "none_filed"
        self._flag("capture_failure")
        return None, None, None

    def _statement_rows(self, statement: str) -> Iterator[dict[str, Any]]:
        """Every non-abstract row of *statement*: dimensional or not, with a value or not. A concept
        the filer put on the statement is *filed* whatever the gateway left of its value -- the
        gateway's T-042 empties the non-dimensional row of a total the filer tags only with
        dimensions (GM's ``DebtCurrent`` and ``LongTermDebtAndCapitalLeaseObligations``: 35.7B and
        94.6B, now ``None`` beside the dimensional rows)."""
        for row in self.raw.get(statement, []):
            if not row.get("abstract"):
                yield row

    def _has_debt_line(self) -> bool:
        """A balance-sheet concept that names a debt obligation is on the statement (any period, any
        row, valued or not): the company has debt the resolver did not read, or the gateway emptied."""
        for row in self._statement_rows("balance_sheet"):
            local = str(row.get("concept") or "").split("_", 1)[-1]
            if _DEBT_LINE_RE.search(local) and not _NOT_A_DEBT_LINE_RE.search(local):
                return True
        return False

    def _has_interest_expense_line(self) -> bool:
        """An interest-expense concept -- any taxonomy, ``InterestExpenseDebt`` and the like
        included -- is on the income statement (any row, valued or not; see :meth:`_statement_rows`)."""
        for row in self._statement_rows("income_statement"):
            local = str(row.get("concept") or "").split("_", 1)[-1]
            if _INTEREST_EXPENSE_LINE_RE.match(local):
                return True
        return False

    # -- capital at the prior fiscal year end (MET-01/02, MD-02/03/35/38; D1/D2/D6/D7/D8, T-151 part 2)

    def _redeemable_nci(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None]:
        """Redeemable NCI between liabilities and equity (MD-02, D6): the filed total, else the three
        carrying-amount parts when all three are filed (the FASB 2026 sfp-cls arc, orders 10/20/30;
        ID-02b -- a component is never the total)."""
        total = self._concept_value(spec, column, REDEEMABLE_NCI_CONCEPTS[0])
        if total is not None:
            return total, REDEEMABLE_NCI_CONCEPTS[0], "concept"
        # F3: when the total is not filed, the parts that ARE filed are the redeemable-NCI lines the
        # filer presents on the face (``financial_facts`` holds the face lines only), so they are
        # summed -- the same only-lines reasoning as the sourced D&A sum (ID-02b). The total is
        # still preferred when it is filed.
        parts = {c: self._concept_value(spec, column, c) for c in REDEEMABLE_NCI_CONCEPTS[1:]}
        filed = {c: v for c, v in parts.items() if v is not None}
        if not filed:
            # T-159a: the redeemable NCI at fair value -- read only when no carrying amount is
            # filed (the total and the three parts are carrying amounts).
            for concept in REDEEMABLE_NCI_SUBSTITUTES:
                value = self._concept_value(spec, column, concept)
                if value is not None:
                    return value, concept, "substitute"
            return None, None, None
        if len(filed) == len(parts):
            self._flag("redeemable_nci_sum")
        else:
            self._flag("redeemable_nci_filed_parts")
        return sum(filed.values()), " + ".join(sorted(filed)), "map"

    def nci_income_filed(self, period_key: str) -> bool:
        """Whether income attributable to NCI is filed for the period (ID-03/CON-19): the public
        wrapper of :meth:`_nci_income_filed` MD-02's zero rule reads (D6)."""
        return self._nci_income_filed(period_key)

    def prior_fiscal_year_end_key(self, period_key: str) -> str | None:
        """The instant column at the end of the fiscal year before the one *period_key* closes
        (MET-01/MET-02, MD-02/35; D1, T-151 part 2).

        A 10-K carries it as its comparative balance sheet: the instant one year before the period
        end (PER-08, :func:`fiscal.is_year_apart`). A 10-Q's balance sheet shows the prior fiscal
        year end beside the current column (Regulation S-X 10-01;
        :func:`fiscal.payload_fiscal_year_end`). ``None`` when the payload carries neither."""
        try:
            target = date.fromisoformat(period_key[:10])
        except ValueError:
            return None
        instants = self.instant_periods()
        period = next((p for p in self.periods if p.key == period_key), None)
        if period is not None and period.is_fy:
            cands = [
                p for p in instants if fiscal.is_year_apart(date.fromisoformat(p.date), target)
            ]
        else:
            fye = fiscal.payload_fiscal_year_end((p.date for p in instants), target)
            cands = (
                [p for p in instants if fiscal.same_period_end(date.fromisoformat(p.date), fye)]
                if fye is not None
                else []
            )
        if not cands:
            return None
        return min(
            cands, key=lambda p: (abs((date.fromisoformat(p.date) - target).days), p.date)
        ).key

    def capital_at(self, column: str | None) -> Capital:
        """The balance-sheet building blocks at the instant *column* (T-151 part 2). ``None`` (no
        column, a missing comparative) leaves every item empty, flagged ``prior_year_bs_missing``."""
        if column is None:
            miss = Resolution(None, None, None, ("prior_year_bs_missing",))
            return Capital(None, *([miss] * 11))
        return Capital(
            source=None,
            total_assets=self.resolve("total_assets", column),
            equity=self.resolve("equity", column),
            cash=self.resolve("cash", column),
            short_term_investments=self.resolve("short_term_investments", column),
            minority_interest=self.resolve("minority_interest", column),
            redeemable_nci=self.resolve("redeemable_nci", column),
            equity_incl_nci=self.resolve("equity_incl_nci", column),
            current_assets=self.resolve("current_assets", column),
            current_liabilities=self.resolve("current_liabilities", column),
            debt=self.total_debt(column),
            short_term_debt=self.resolve("short_term_debt", column),
        )

    def prior_year_capital(self, period_key: str) -> Capital:
        """Capital at the end of the prior fiscal year (MET-01/MET-02; D1/D2, T-151 part 2): the
        pipeline's resolved value when it set one (a stored prior 10-K fills a missing comparative),
        else the filing's own comparative column."""
        if self.prior_capital is not None:
            return self.prior_capital
        key = self.prior_fiscal_year_end_key(period_key)
        if key is None:
            return self.capital_at(None)
        cap = self.capital_at(key)
        return replace(cap, source="filing")

    def prior_quarter_capital_at(self, period_key: str) -> Capital:
        """Capital at the prior year's same quarter end (MD-38's Δ, a 10-Q; D8, T-151 part 2). Only
        the pipeline can resolve it: the 10-Q's own balance sheet carries the prior fiscal year end,
        not the prior-year quarter (the stored prior-year 10-Q does). Empty in a hermetic fixture."""
        if self.prior_quarter_capital is not None:
            return self.prior_quarter_capital
        return self.capital_at(None)

    # Dimension axes whose slice may legitimately equal the consolidated total: a
    # single-segment filer discloses its one segment's revenue, which *is* the total. A
    # duplicate on any other axis (APA FY2021: the equity-method-investee axis) is a
    # slice promoted to a total, not a coincidence (T-128).
    _WHOLE_ENTITY_AXES = frozenset(
        {
            "srt:ConsolidationItemsAxis",
            "us-gaap:StatementBusinessSegmentsAxis",
        }
    )

    # B3_DQC_0011 line 22: NetIncomeLoss "should be equal to" the
    # StatementEquityComponentsAxis=RetainedEarningsMember fact, so that equality is
    # expected, not a promoted slice.
    _ID16_WHOLE_ENTITY_AXES = _WHOLE_ENTITY_AXES | frozenset(
        {"us-gaap:StatementEquityComponentsAxis"}
    )

    def _total_is_plausible(self, spec: LineItem, column: str, total_value: float) -> bool:
        """Sanity-check a Tier 1 ``total_concepts`` match -- see :meth:`get`.

        T-128 (``docs/model_fixes.md``): a total is a tagging defect, not a consolidated
        aggregate, when **both** hold -- (1) it exactly duplicates a *dimensional* row of the
        same concept in the same column, on an axis that is not a whole-entity one
        (:data:`_WHOLE_ENTITY_AXES`): the gateway promoted one breakdown slice to a
        non-dimensional total (APA FY2021's ``us-gaap_Revenues`` is filed only with the
        equity-method-investee dimension, $1,082M); and (2) a named ``spec.concepts``
        component is larger than it, which a real aggregate never is. This replaced T-095's
        50%-of-the-largest-component floor, which rejected legitimate gas-producer totals
        that net hedging losses below half their gross sales (EQT, EXE). A filer with no
        ``concepts`` rows at all (a bank's ``RevenuesNetOfInterestExpense``, JPM's real shape)
        has nothing to compare against, so its total is trusted as before -- this only ever
        *rejects*, never invents a floor."""
        largest = self._largest_component_value(spec, column)
        if largest is None or total_value >= largest:
            return True
        return not self._duplicates_a_dimensional_slice(spec, column, total_value)

    def _duplicates_a_dimensional_slice(
        self, spec: LineItem, column: str, total_value: float
    ) -> bool:
        """Whether a dimensional row of a ``total_concepts`` concept, on a non-whole-entity
        axis, carries exactly *total_value* in *column* (:meth:`_total_is_plausible`)."""
        for statement in spec.statements:
            for row in self.raw.get(statement, []):
                if (
                    row.get("abstract")
                    or not row.get("dimension")
                    or row.get("concept") not in spec.total_concepts
                    or row.get("dimension_axis") in self._WHOLE_ENTITY_AXES
                ):
                    continue
                value = _numeric(row.get(column))
                if value is not None and math.isclose(value, total_value, rel_tol=1e-9):
                    return True
        return False

    def _equals_dimensional_member(
        self, spec: LineItem, column: str, concept: str, value: float
    ) -> bool:
        """Whether a dimensional row of *concept*, on an axis outside
        _ID16_WHOLE_ENTITY_AXES, carries exactly *value* in *column* (ID-16 / ID-09)."""
        concepts_to_check = {concept} if " + " not in concept else set()
        if not concepts_to_check:
            return False
        for statement in spec.statements:
            for row in self.raw.get(statement, []):
                if (
                    row.get("abstract")
                    or not row.get("dimension")
                    or row.get("concept") not in concepts_to_check
                    or row.get("dimension_axis") in self._ID16_WHOLE_ENTITY_AXES
                ):
                    continue
                v = _numeric(row.get(column))
                if v is not None and math.isclose(v, value, rel_tol=1e-9):
                    return True
        return False

    # T-117: a later candidate must be no more than this fraction of the Tier 1 total to
    # count as a contradiction, not rounding/immaterial noise -- APA's ratio is ~0.42-0.50
    # (too-large case); its exact-match FY2022 (not a defect) sits at ~1.10, well above.
    _LABEL_TOTAL_CONTRADICTION_RATIO = 0.75
    # Each row between the two totals must be no larger than this fraction of the later,
    # trusted total, or the derivation is too uncertain to trust -- reject rather than guess.
    _BETWEEN_ROW_CEILING = 0.25
    # A row's label reads as a revenue total independent of `total_concepts`'s fixed concept
    # list -- lets a later, more-authoritative subtotal contradict an earlier match without
    # naming any filer's own custom-taxonomy extension concept. Excludes "Total cost of
    # revenue(s)"/"...cost of revenues, net" -- a near-universal COGS-line label that
    # otherwise matches "total ... revenue" trivially (T-117 full-universe validation: the
    # single false-positive shape found across every non-APA hit in the 5,076-filing stored
    # set -- ADBE, STE, TER, TSLA, URI, XYZ, all this exact phrase, none a real total).
    _LABEL_TOTAL_RE = re.compile(r"\btotal\b.{0,40}\brevenues?\b", re.IGNORECASE)
    _LABEL_TOTAL_EXCLUDE_RE = re.compile(r"\bcosts?\b", re.IGNORECASE)

    def _label_total_correction(
        self, spec: LineItem, column: str, total_value: float
    ) -> tuple[float | None, bool]:
        """T-117 (``docs/model_fixes.md``): a Tier 1 ``total_concepts`` match can also be
        implausibly *large* -- a component/breakdown value the gateway mislabeled with the
        aggregate's own concept or role, not merely too small (T-095's case, guarded by
        :meth:`_total_is_plausible`). Detected structurally, not by filer: scan the same
        statement, in document order, for a row *after* the Tier 1 match whose label also
        reads as a revenue total (:data:`_LABEL_TOTAL_RE`, independent of
        ``spec.total_concepts``'s fixed concept list) and is materially smaller
        (:data:`_LABEL_TOTAL_CONTRADICTION_RATIO`) -- a well-formed statement's later,
        broader total ("Total revenues *and other*") is never smaller than its own earlier
        "Total revenues" component, so a later, smaller "total"-labeled row is only ever
        reachable here when the *earlier* one is the wrong, inflated figure.

        When found, and every row between the two is individually small enough to trust as
        an adjustment item (:data:`_BETWEEN_ROW_CEILING`), the true total is the later row's
        value less those in-between rows -- APA: "Total revenues and other" less derivative
        gains, divestiture gains, property-sale losses, and other, net, recovers "Total
        revenues" to the dollar for FY2023-FY2025 (verified, ``docs/model_fixes.md``, T-117).
        Otherwise contradicted but not safely derivable: the caller must not trust
        ``total_value`` either, but this returns no guess.

        Returns ``(corrected_value_or_None, contradicted)``."""
        rows = list(self._rows_for(spec))
        winner_index = next(
            (
                i
                for i, row in enumerate(rows)
                if row.get("concept") in spec.total_concepts
                and _numeric(row.get(column)) is not None
            ),
            None,
        )
        if winner_index is None:
            return None, False
        for i in range(winner_index + 1, len(rows)):
            label = str(rows[i].get("label") or "")
            if not self._LABEL_TOTAL_RE.search(label) or self._LABEL_TOTAL_EXCLUDE_RE.search(label):
                continue
            later_value = _numeric(rows[i].get(column))
            if later_value is None or later_value <= 0:
                continue
            if later_value >= total_value * self._LABEL_TOTAL_CONTRADICTION_RATIO:
                continue  # not materially smaller -- not a contradiction, keep scanning
            between = [
                v
                for j in range(winner_index + 1, i)
                if (v := _numeric(rows[j].get(column))) is not None
            ]
            if any(abs(v) > later_value * self._BETWEEN_ROW_CEILING for v in between):
                return None, True  # contradicted, but too uncertain to derive
            return later_value - sum(between), True
        return None, False

    def _rebuild_missing_total(self, spec: LineItem, column: str) -> float | None:
        """T-140 (``docs/model_fixes.md``): rebuild a ``total_concepts`` total the gateway
        dropped. Its T-042 rule (``no_filed_nondimensional_fact``) removes a value a filer
        tags only with a dimension, so a statement can reach us with every revenue line empty
        and only the *later*, broader subtotal ("Total revenues and other") still valued --
        APA FY2022: ``us-gaap_Revenues`` $11,075M dropped, "Total revenues and other"
        $12,132M kept. See :meth:`rebuild_total` for the rule; returns its value, or ``None``."""
        rebuild = self.rebuild_total(spec, column)
        return rebuild.value if rebuild else None

    def rebuild_total(self, spec: LineItem, column: str) -> TotalRebuild | None:
        """The structural reading behind :meth:`_rebuild_missing_total`, by label only (never a
        filer's own concept names), the same as :meth:`_label_total_correction`'s: the first
        valued row *after the revenue section* whose label reads as a revenue total
        (:data:`_LABEL_TOTAL_RE`, never a cost line) is the anchor, and the revenue is that
        value less the valued rows between the section and it -- the statement's own
        non-revenue adjustment lines (derivative gains, divestiture gains, ...). The section
        ends at the last valued row of a registry revenue concept
        (``spec.concepts``/``spec.total_concepts``): a revenue component that did survive is
        never subtracted, and a total *above* one is not a later, broader total at all. The trust rule is T-117's: every row between must be individually
        small against the anchor (:data:`_BETWEEN_ROW_CEILING`), or the derivation is too
        uncertain and the value is ``None`` -- no value, never a guess -- unless the gateway's own
        figure for the dropped total corroborates it (:meth:`_gateway_original_is`); a
        non-positive result is refused either way. ``None`` itself means the column has no
        anchor row at all."""
        rows = list(self._rows_for(spec))
        revenue_concepts = set(spec.concepts) | set(spec.total_concepts)
        section_end = max(
            (
                i
                for i, row in enumerate(rows)
                if row.get("concept") in revenue_concepts and _numeric(row.get(column)) is not None
            ),
            default=-1,
        )
        for i in range(section_end + 1, len(rows)):
            value = _numeric(rows[i].get(column))
            label = str(rows[i].get("label") or "")
            if (
                value is None
                or value <= 0
                or not self._LABEL_TOTAL_RE.search(label)
                or self._LABEL_TOTAL_EXCLUDE_RE.search(label)
            ):
                continue
            between = tuple(
                v
                for j in range(section_end + 1, i)
                if (v := _numeric(rows[j].get(column))) is not None
            )
            rebuilt = value - sum(between)
            if rebuilt <= 0:
                return TotalRebuild(label, value, between, None, "not positive")
            if any(
                comp_val > rebuilt
                for row in rows
                if row.get("concept") in spec.concepts
                and (comp_val := _numeric(row.get(column))) is not None
            ):
                return TotalRebuild(label, value, between, None, "net anchor")
            if any(abs(v) > value * self._BETWEEN_ROW_CEILING for v in between):
                if self._gateway_original_is(spec, column, rebuilt):
                    return TotalRebuild(label, value, between, rebuilt, corroborated=True)
                return TotalRebuild(label, value, between, None, "a row between is too large")
            return TotalRebuild(label, value, between, rebuilt)
        return None

    def _gateway_original_is(self, spec: LineItem, column: str, value: float) -> bool:
        """Whether the gateway's own ``corrections`` record the dropped total of *column* with
        exactly *value* as its ``original`` (T-140). A one-off gain in a small quarter can put a
        between row above :data:`_BETWEEN_ROW_CEILING` (APA 2022Q1: a $1,176M divestiture gain
        against a $3,828M total); the derivation is then trusted only when the gateway's
        independent figure agrees to the dollar -- never on the strength of either alone.
        The ``original`` is not trusted by itself: it can be the promoted dimensional slice
        (APA FY2021: $1,082M, the equity-method investee row), which no rebuild equals."""
        return any(
            c.get("statement") == "income_statement"
            and c.get("concept") in spec.total_concepts
            and c.get("column") == column
            and c.get("corrected") is None
            and (original := _numeric(c.get("original"))) is not None
            and math.isclose(original, value, rel_tol=1e-9)
            for c in self.corrections
        )

    def _largest_component_value(self, spec: LineItem, column: str) -> float | None:
        """The largest single first-matching-row value among ``spec.concepts``
        -- used only by :meth:`_total_is_plausible`. Shares its one-row-per-
        concept collection with :meth:`_sum_matching_components`."""
        values = self._matching_component_values(spec, column)
        return max(values.values()) if values else None

    def _candidates(self, spec: LineItem) -> Iterator[tuple[str, dict[str, Any]]]:
        """``(tier, row)`` for every row that can represent *spec*, in **precedence order**
        (ID-02a, T-148): the exact ``concepts`` one concept after the other in the spec's order
        (``concept``), then the rows whose gateway ``standard_concept`` is listed (``standard``),
        then the rows whose label contains a ``label_contains`` hint (``label``). A label or a
        standard-concept match never outranks an exact concept, wherever it sits in the statement.

        An item with ``document_order`` (capex, MET-08) keeps master's reading instead: the rows
        that match by concept, standard concept or label, in the order the statement lists them."""
        rows = list(self._rows_for(spec))
        if spec.document_order:
            yield from self._document_order_candidates(spec, rows)
        else:
            yield from self._precedence_candidates(spec, rows)

    @staticmethod
    def _precedence_candidates(
        spec: LineItem, rows: list[dict[str, Any]]
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        for concept in spec.concepts:
            for row in rows:
                if row.get("concept") == concept:
                    yield "concept", row
        if spec.standard:
            for row in rows:
                if row.get("concept") not in spec.concepts and (
                    row.get("standard_concept") in spec.standard
                ):
                    yield "standard", row
        if spec.label_contains:
            for row in rows:
                label = str(row.get("label") or "").lower()
                if (
                    row.get("concept") not in spec.concepts
                    and not (spec.standard and row.get("standard_concept") in spec.standard)
                    and any(hint in label for hint in spec.label_contains)
                ):
                    yield "label", row

    @staticmethod
    def _document_order_candidates(
        spec: LineItem, rows: list[dict[str, Any]]
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        """:meth:`_candidates` for an item that keeps master's document order (MET-08, capex)."""
        for row in rows:
            if row.get("concept") in spec.concepts:
                yield "concept", row
            elif spec.standard and row.get("standard_concept") in spec.standard:
                yield "standard", row
            elif any(h in str(row.get("label") or "").lower() for h in spec.label_contains):
                yield "label", row

    def _first_component_row(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None]:
        """Tier 2 (default): the first row in precedence order (:meth:`_candidates`) with a value
        in *column*: ``(value, concept, tier)``."""
        for tier, row in self._candidates(spec):
            value = self._read(row, column, spec)
            if value is not None:
                return value, str(row.get("concept")), tier
        return None, None, None

    def _first_component_match(self, spec: LineItem, column: str) -> float | None:
        return self._first_component_row(spec, column)[0]

    def _fallback_row(
        self, spec: LineItem, column: str
    ) -> tuple[float | None, str | None, str | None]:
        """The T-133 fallback tiers, in order: each ``spec.fallback_concepts`` entry, then the
        ``spec.label_fallback`` caption. Reached only when :meth:`_first_component_row` found
        nothing, so it can never displace a value the ordinary lookup returns."""
        for tier, concept in enumerate(spec.fallback_concepts):
            value = self._concept_value(spec, column, concept)
            if value is None:
                continue
            if tier < spec.fallback_addend_tiers:
                for extra in spec.fallback_addends:
                    value += self._concept_value(spec, column, extra) or 0.0
            return value, concept, f"fallback{tier + 1}"
        return self._label_fallback_row(spec, column)

    def _label_fallback_row(
        self, spec: LineItem, column: str, exclude: tuple[str, ...] = ()
    ) -> tuple[float | None, str | None, str | None]:
        """The single caption-matched row, or nothing. *exclude* names concepts the caller has
        already decided against (capex excludes the net element C3 rejected, so a caption like
        "Capital expenditures, net" cannot re-take it)."""
        if not spec.label_fallback:
            return None, None, None
        wanted = re.compile(spec.label_fallback, re.IGNORECASE)
        # an empty pattern would match every label, so "no exclusion" must not compile to it
        unwanted = re.compile(spec.label_fallback_exclude or r"(?!)", re.IGNORECASE)
        found: dict[str, float] = {}
        for row in self._rows_for(spec):
            if str(row.get("concept")) in exclude:
                continue
            label = str(row.get("label") or "")
            if not wanted.search(label) or unwanted.search(label):
                continue
            value = self._read(row, column, spec)
            if value is not None:
                found.setdefault(str(row.get("concept")), value)
        # One caption, one line. Two lines that both read as the item are a filer reporting it in
        # parts (NEE: "Capital expenditures of FPL" beside "Other capital expenditures" and the
        # independent-power line), and either alone would understate it -- a partial figure is
        # worse than none, so the item stays missing.
        if len(found) == 1:
            concept, value = next(iter(found.items()))
            return value, concept, "label_fallback"
        return None, None, None

    def _fallback_match(self, spec: LineItem, column: str) -> float | None:
        return self._fallback_row(spec, column)[0]

    def _concept_value(self, spec: LineItem, column: str, concept: str) -> float | None:
        """The first non-dimensional row tagged *concept* that has a value in *column*."""
        for row in self._rows_for(spec):
            if row.get("concept") == concept:
                value = self._read(row, column, spec)
                if value is not None:
                    return value
        return None

    def _is_concept_filed_in_column(self, spec: LineItem, column: str, concept: str) -> bool:
        """Whether a non-abstract, non-dimensional row tagged *concept* has a value in *column*."""
        for row in self._rows_for(spec):
            if row.get("concept") == concept and _numeric(row.get(column)) is not None:
                return True
        return False

    def _matching_component_values(self, spec: LineItem, column: str) -> dict[str, float]:
        """One value per distinct ``spec.concepts`` member, first occurrence in
        document order. Matches only ``spec.concepts`` (never the fuzzy
        ``label_contains`` fallback -- an
        unrecognized custom "Total ..." extension concept can match by label
        text alone and would double-count against the real components, or
        wrongly inflate the plausibility floor). Shared by
        :meth:`_sum_matching_components` (the sum) and
        :meth:`_largest_component_value` (the max, for :meth:`_total_is_plausible`)."""
        values: dict[str, float] = {}
        for row in self._rows_for(spec):
            concept = row.get("concept")
            if concept is None or concept not in spec.concepts or concept in values:
                continue
            value = self._read(row, column, spec)
            if value is not None:
                values[concept] = value
        return values

    def _sum_matching_components(self, spec: LineItem, column: str) -> float | None:
        return self._sum_components(spec, column)[0]

    def _sum_components(self, spec: LineItem, column: str) -> tuple[float | None, list[str]]:
        """Sum one value per distinct additive component -- the
        ``sum_components`` branch of :meth:`get` -- and name the components it counted.

        Concepts sharing a ``spec.synonym_groups`` entry are alternate
        encodings of ONE stream, not separate amounts: at most one value per
        group counts, preferring whichever member is listed earliest in
        ``spec.concepts``.
        """
        values = self._matching_component_values(spec, column)
        if not values:
            return None, []

        total = 0.0
        counted: set[str] = set()
        used: list[str] = []
        for concept in spec.concepts:
            if concept not in values or concept in counted:
                continue
            group = next((g for g in spec.synonym_groups if concept in g), (concept,))
            total += values[concept]
            used.append(concept)
            counted.update(group)
        return total, used

    def get_all(self, item: str) -> dict[str, float]:
        """Every period-column value on registry *item*'s first-matching row (in precedence
        order, :meth:`_candidates`) -- not just one period, the full reported series for that
        concept in this payload, keyed by its raw period_key exactly as ``iter_facts``/
        ``financial_facts`` store it. Unlike :meth:`get`, this does not resolve
        a balance-sheet item through :meth:`_instant_for` -- it returns the row's
        own columns as reported (in the filed sign), which is what a cross-filing scale/tagging-defect
        check (:func:`fundamental_agent.db.detect_share_scale_factors`) needs: the
        same overlapping historical periods this filing itself restates.
        """
        spec = REGISTRY[item]
        for _tier, row in self._candidates(spec):
            out: dict[str, float] = {}
            for column in row:
                if _parse_period(str(column)) is None:
                    continue
                number = self._read(row, column, spec)
                if number is not None:
                    out[column] = number
            return out
        return {}

    def resolve_column(self, item: str, period_key: str) -> str | None:
        """The raw column key :meth:`get_all` uses for *period_key* on
        registry *item* -- a balance-sheet item resolves through the same
        instant-date translation :meth:`get` uses internally (``None`` if no
        instant column exists); any other item uses *period_key* as-is. Lets
        a caller holding :meth:`get_all`'s result (keyed by raw column, not by
        the duration key balance-sheet items are normally asked for) look up
        "this filing's own target period" consistently across item kinds --
        see :func:`fundamental_agent.db.detect_share_scale_factors`.
        """
        if REGISTRY[item].statements == _BALANCE:
            return self._instant_for(period_key)
        return period_key

    def _instant_for(self, period_key: str) -> str | None:
        """The balance-sheet column of the period *period_key* ends (ID-05: a balance is an
        instant fact at the period end). The column of that very date; failing it, the one whose
        date rounds to the same month end (PER-08 -- a 52/53-week filer's balance sheet is dated
        days from the calendar month end), the closest if several. **Never the nearest earlier
        date** (N6): that read another period's balance sheet -- the prior year end of a 10-Q
        whose own column was missing -- as this one."""
        try:
            target = date.fromisoformat(period_key[:10])
        except ValueError:
            return None
        instants = self.instant_periods()
        exact = [p for p in instants if p.date == period_key[:10]]
        if exact:
            return exact[0].key
        near = [p for p in instants if fiscal.same_period_end(date.fromisoformat(p.date), target)]
        if not near:
            return None
        return min(
            near, key=lambda p: (abs((date.fromisoformat(p.date) - target).days), p.date)
        ).key


def iter_facts(stmts: Statements) -> Iterator[dict[str, Any]]:
    """Yield one flat fact per (non-abstract, non-dimensional row, period column).

    T-118: a fact whose ``(statement, concept, column)`` matches one of
    ``stmts.corrections`` -- the gateway's own record of a value it derived rather than
    returned as filed -- carries that entry's ``rule`` id as ``correction_rule``; every
    other fact gets ``None``, meaning "filed as-is". Keyed by statement too (PR #98
    review): once the general reconciliation (T-042) started validating all three
    statements, not just revenue on the income statement, a concept like
    ``NetIncomeLoss`` or ``DepreciationDepletionAndAmortization`` can appear -- and be
    corrected independently -- on more than one of them; `(concept, column)` alone could
    then tag the wrong statement's fact.
    """
    corrected_rule = {
        (c.get("statement"), c.get("concept"), c.get("column")): c.get("rule")
        for c in stmts.corrections
    }
    for statement, rows in stmts.raw.items():
        for row in rows:
            if row.get("abstract") or row.get("dimension"):
                continue
            concept = row.get("concept")
            if not concept:
                continue
            for column, value in row.items():
                if _parse_period(str(column)) is None:
                    continue
                number = _numeric(value)
                if number is None:
                    continue
                yield {
                    "statement": statement,
                    "concept": concept,
                    "standard_concept": row.get("standard_concept"),
                    "label": row.get("label"),
                    "period_key": column,
                    "value": number,
                    "correction_rule": corrected_rule.get((statement, concept, column)),
                    # ID-12 (T-148): edgartools' presentation hint -- the displayed value times it
                    # is the filed value. ``None`` when the row carries none.
                    "preferred_sign": _preferred_sign(row),
                }


def _parse_period(column: str) -> Period | None:
    duration = _DURATION_RE.match(column)
    if duration:
        return Period(key=column, date=duration["date"], tag=duration["tag"])
    instant = _INSTANT_RE.match(column)
    if instant:
        return Period(key=column, date=instant["date"], tag=INSTANT)
    return None


def _names_nci_income(row: dict[str, Any]) -> bool:
    """Whether *row* is income attributable to a noncontrolling interest (ID-03): one of the three
    ``us-gaap`` elements, or -- the conservative inference of :data:`_NCI_NAME_RE` -- a row whose
    concept name and caption name the interest, speak of an income and are not a total that
    includes it, a pre-tax line or a flow. ``ProfitLoss`` and ``NetIncomeLoss`` are the rows under
    test, never evidence."""
    concept = str(row.get("concept") or "")
    if concept in _NCI_INCOME_CONCEPTS:
        return True
    if concept in (G + "ProfitLoss", G + "NetIncomeLoss"):
        return False
    text = f"{concept.split('_', 1)[-1]} {row.get('label') or ''}"
    return bool(
        _NCI_NAME_RE.search(text)
        and _NCI_INCOME_WORD_RE.search(text)
        and not _NCI_NOT_AN_INCOME_RE.search(text)
    )


def _preferred_sign(row: dict[str, Any]) -> float | None:
    """The row's ``preferred_sign`` (+1 or -1), or ``None`` when it carries none (ID-12)."""
    sign = _numeric(row.get("preferred_sign"))
    return sign if sign in (1.0, -1.0) else None


def filed_value(row: dict[str, Any], column: str) -> float | None:
    """The value of *row* in *column* in the sign it was filed in (ID-12): the displayed value
    times the row's ``preferred_sign``; as displayed when the row carries none. The audit scripts
    read the other candidate concepts of a filing through it, on the resolver's own sign."""
    displayed = _numeric(row.get(column))
    if displayed is None:
        return None
    sign = _preferred_sign(row)
    return displayed if sign is None else displayed * sign


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None
