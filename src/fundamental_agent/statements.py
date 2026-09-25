"""Parse the EDGAR ``financials`` payload into something metric code can query.

The payload has three lists of rows -- ``income_statement``, ``balance_sheet`` and
``cash_flow``. Income and cash-flow rows use *duration* columns keyed like
``"2023-09-30 (FY)"`` / ``"2024-06-29 (Q3)"`` / ``"2024-06-29 (YTD)"``; balance-sheet
rows use bare *instant* dates like ``"2023-09-30"``. :meth:`Statements.get` hides that
difference: ask for a duration period and balance-sheet items resolve against the
instant column with the matching (or nearest-earlier) date.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

STATEMENT_KEYS = ("income_statement", "balance_sheet", "cash_flow")
_DURATION_RE = re.compile(r"^\s*(?P<date>\d{4}-\d{2}-\d{2})\s*\((?P<tag>[A-Za-z0-9]+)\)\s*$")
_INSTANT_RE = re.compile(r"^\s*(?P<date>\d{4}-\d{2}-\d{2})\s*$")
_QUARTER_RE = re.compile(r"^Q[1-4]$")
INSTANT = "INSTANT"


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
    # unless Statements._total_is_plausible (T-095) rejects it as far smaller
    # than a named `concepts` candidate, a tagging defect rather than a real
    # aggregate.
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


_INCOME = ("income_statement",)
_BALANCE = ("balance_sheet",)
_CASHFLOW = ("cash_flow",)

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
    ),
    "cogs": LineItem(
        "cogs",
        _INCOME,
        concepts=(
            "us-gaap_CostOfGoodsAndServicesSold",
            "us-gaap_CostOfRevenue",
            "us-gaap_CostOfGoodsSold",
            "us-gaap_CostOfServices",
        ),
    ),
    "gross_profit": LineItem("gross_profit", _INCOME, concepts=("us-gaap_GrossProfit",)),
    "operating_income": LineItem(
        "operating_income", _INCOME, concepts=("us-gaap_OperatingIncomeLoss",)
    ),
    "net_income": LineItem(
        "net_income",
        _INCOME,
        concepts=(
            "us-gaap_NetIncomeLoss",
            "us-gaap_ProfitLoss",
            # T-096, docs/model_fixes.md: WAT tags no NetIncomeLoss/ProfitLoss row at
            # all for most of its filing history (14/18 metrics-v2 filings in the
            # 20-ticker sample) -- only this "available to common" variant. Live-
            # verified never to co-occur with the two above in the same WAT filing
            # (WAT switched tagging convention outright starting 2025Q2), so
            # document-order (this item sets no `total_concepts`/`sum_components`,
            # Tier 2's plain first-match) carries no live-verified ambiguity risk for
            # it; a filer that genuinely tags both, with a different preferred-
            # dividend-adjusted value, is a residual, unverified risk (see the
            # docs entry).
            "us-gaap_NetIncomeLossAvailableToCommonStockholdersBasic",
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
    ),
    "income_tax": LineItem(
        "income_tax",
        _INCOME,
        concepts=("us-gaap_IncomeTaxExpenseBenefit",),
        standard=("IncomeTaxes",),
    ),
    "interest_expense": LineItem(
        "interest_expense",
        _INCOME,
        concepts=(
            "us-gaap_InterestExpense",
            "us-gaap_InterestExpenseNonoperating",
            "us-gaap_InterestAndDebtExpense",
        ),
    ),
    "depreciation_amortization": LineItem(
        "depreciation_amortization",
        _CASHFLOW,
        concepts=(
            "us-gaap_DepreciationDepletionAndAmortization",
            "us-gaap_DepreciationAmortizationAndAccretionNet",
            "us-gaap_DepreciationAndAmortization",
            "us-gaap_Depreciation",
        ),
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
        concepts=(
            "us-gaap_PaymentsToAcquirePropertyPlantAndEquipment",
            "us-gaap_PaymentsToAcquireProductiveAssets",
            "us-gaap_PaymentsForCapitalImprovements",
        ),
    ),
    "stock_based_compensation": LineItem(
        "stock_based_compensation",
        _CASHFLOW,
        concepts=(
            "us-gaap_ShareBasedCompensation",
            "us-gaap_ShareBasedCompensationExpense",
            "us-gaap_AllocatedShareBasedCompensationExpense",
        ),
        standard=("StockBasedCompensationExpense",),
    ),
    # Point-in-time common shares outstanding, for market cap (price x shares at the
    # period-end date). Not always present -- callers fall back to `diluted_shares`.
    "shares_outstanding": LineItem(
        "shares_outstanding",
        _BALANCE,
        concepts=(
            "us-gaap_CommonStockSharesOutstanding",
            "us-gaap_CommonStockSharesIssued",
        ),
        standard=("SharesYearEnd",),
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
    "total_assets": LineItem("total_assets", _BALANCE, concepts=("us-gaap_Assets",)),
    "current_assets": LineItem("current_assets", _BALANCE, concepts=("us-gaap_AssetsCurrent",)),
    "total_liabilities": LineItem("total_liabilities", _BALANCE, concepts=("us-gaap_Liabilities",)),
    "current_liabilities": LineItem(
        "current_liabilities", _BALANCE, concepts=("us-gaap_LiabilitiesCurrent",)
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
    ),
    "short_term_investments": LineItem(
        "short_term_investments",
        _BALANCE,
        concepts=(
            "us-gaap_ShortTermInvestments",
            "us-gaap_MarketableSecuritiesCurrent",
            "us-gaap_AvailableForSaleSecuritiesCurrent",
        ),
    ),
    "inventory": LineItem("inventory", _BALANCE, concepts=("us-gaap_InventoryNet",)),
    "receivables": LineItem(
        "receivables",
        _BALANCE,
        concepts=(
            "us-gaap_AccountsReceivableNetCurrent",
            "us-gaap_ReceivablesNetCurrent",
        ),
    ),
    "long_term_debt": LineItem(
        "long_term_debt",
        _BALANCE,
        concepts=(
            "us-gaap_LongTermDebtNoncurrent",
            "us-gaap_LongTermDebt",
            "us-gaap_LongTermDebtAndCapitalLeaseObligations",
            "us-gaap_LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
        ),
    ),
    "short_term_debt": LineItem(
        "short_term_debt",
        _BALANCE,
        concepts=(
            "us-gaap_LongTermDebtCurrent",
            "us-gaap_ShortTermBorrowings",
            "us-gaap_DebtCurrent",
            "us-gaap_CommercialPaper",
        ),
    ),
}


@dataclass
class Statements:
    """A parsed ``financials`` payload."""

    raw: dict[str, list[dict[str, Any]]]
    periods: list[Period] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Statements:
        raw = {key: list(payload.get(key) or []) for key in STATEMENT_KEYS}
        seen: dict[str, Period] = {}
        for rows in raw.values():
            for row in rows:
                for column in row:
                    if column not in seen:
                        parsed = _parse_period(str(column))
                        if parsed is not None:
                            seen[column] = parsed
        periods = sorted(seen.values(), key=lambda p: (p.date, p.tag))
        return cls(raw=raw, periods=periods)

    def fy_periods(self) -> list[Period]:
        return [p for p in self.periods if p.is_fy]

    def quarter_periods(self) -> list[Period]:
        return [p for p in self.periods if p.is_quarter]

    def instant_periods(self) -> list[Period]:
        return [p for p in self.periods if p.is_instant]

    def latest_fy(self) -> Period | None:
        fy = self.fy_periods()
        return fy[-1] if fy else None

    def prior_of(self, period: Period) -> Period | None:
        """The same-tag column one step earlier in time, if the payload carries it."""
        earlier = [p for p in self.periods if p.tag == period.tag and p.date < period.date]
        return earlier[-1] if earlier else None

    def _rows_for(self, spec: LineItem) -> Iterator[dict[str, Any]]:
        """Every non-abstract, non-dimensional row across *spec*'s statements,
        in raw document order."""
        for statement in spec.statements:
            for row in self.raw.get(statement, []):
                if row.get("abstract") or row.get("dimension"):
                    continue
                yield row

    def get(self, item: str, period_key: str) -> float | None:
        """Return the value of registry *item* for *period_key*, or ``None``.

        Two-tier resolution (F2, ``docs/model_fixes.md``): **Tier 1** -- if
        ``spec.total_concepts`` is set, any row tagged with one of them wins
        outright over every ``concepts`` match, regardless of document
        order -- *unless* :meth:`_total_is_plausible` rejects it (T-095,
        ``docs/model_fixes.md``): a ``total_concepts`` tag is sometimes
        mistagged on one small dimensional slice of the real breakdown
        rather than the consolidated aggregate (APA FY2021's real shape --
        ``us-gaap_Revenues`` = $1,082M, matching only its "Equity Method
        Investment, Nonconsolidated Investee" dimensional row, while the
        real total is $7,988M), and such a value can never be the genuine
        aggregate. Absent a trusted match there, **Tier 2** falls back to
        the original single-row lookup: the first ``concepts``-matching row
        in document order (unchanged default for every item that doesn't
        set ``sum_components``), or, when ``spec.sum_components`` is set,
        the sum of the first row per distinct matching concept -- multiple
        co-reported streams with no separately tagged total.
        """
        spec = REGISTRY[item]
        column = period_key
        if spec.statements == _BALANCE:
            instant = self._instant_for(period_key)
            if instant is None:
                return None
            column = instant

        if spec.total_concepts:
            total_value = self._first_total_match(spec, column)
            if total_value is not None and self._total_is_plausible(spec, column, total_value):
                return total_value

        if spec.sum_components:
            return self._sum_matching_components(spec, column)
        return self._first_component_match(spec, column)

    def _first_total_match(self, spec: LineItem, column: str) -> float | None:
        """Tier 1: the first row tagged with one of ``spec.total_concepts``."""
        for row in self._rows_for(spec):
            if row.get("concept") in spec.total_concepts:
                value = _numeric(row.get(column))
                if value is not None:
                    return value
        return None

    # A `total_concepts` match must be at least this fraction of the largest
    # `spec.concepts` candidate to be trusted outright (T-095, docs/model_fixes.md).
    # A genuine aggregate is never far smaller than one of its own named
    # components; a total this much smaller is a tagging defect (a filer's
    # generic "total" concept landing on one small dimensional slice instead of
    # the consolidated figure -- APA FY2021's real shape, see :meth:`get`).
    # Picked to sit well above real, correct cases (the total exactly equals or
    # slightly exceeds the largest component -- UDR's real shape, ratio ~1.0)
    # and well below the observed defect (ratio ~0.14); pinned exactly by
    # ``tests/test_statements.py::test_revenue_total_concepts_plausibility_floor_is_pinned``.
    _TOTAL_PLAUSIBILITY_FLOOR = 0.5

    def _total_is_plausible(self, spec: LineItem, column: str, total_value: float) -> bool:
        """Sanity-check a Tier 1 ``total_concepts`` match against the largest
        named ``spec.concepts`` candidate -- see :meth:`get` and
        :data:`_TOTAL_PLAUSIBILITY_FLOOR`. A filer with no ``concepts`` rows at
        all (e.g. a bank's ``RevenuesNetOfInterestExpense`` total, JPM's real
        shape) has nothing to compare against, so its total is trusted as
        before -- this only ever *rejects*, never invents a floor where none
        of ``spec.concepts`` is tagged."""
        largest = self._largest_component_value(spec, column)
        if largest is None:
            return True
        return total_value >= largest * self._TOTAL_PLAUSIBILITY_FLOOR

    def _largest_component_value(self, spec: LineItem, column: str) -> float | None:
        """The largest single first-matching-row value among ``spec.concepts``
        -- used only by :meth:`_total_is_plausible`. Shares its one-row-per-
        concept collection with :meth:`_sum_matching_components`."""
        values = self._matching_component_values(spec, column)
        return max(values.values()) if values else None

    def _first_component_match(self, spec: LineItem, column: str) -> float | None:
        """Tier 2 (default): the original single-row, first-document-order
        lookup, unchanged for every item that doesn't set ``sum_components``."""
        for row in self._rows_for(spec):
            if _matches(row, spec):
                value = _numeric(row.get(column))
                if value is not None:
                    return value
        return None

    def _matching_component_values(self, spec: LineItem, column: str) -> dict[str, float]:
        """One value per distinct ``spec.concepts`` member, first occurrence in
        document order. Matches only ``spec.concepts`` (never the fuzzy
        ``label_contains`` fallback :func:`_matches` also checks -- an
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
            value = _numeric(row.get(column))
            if value is not None:
                values[concept] = value
        return values

    def _sum_matching_components(self, spec: LineItem, column: str) -> float | None:
        """Sum one value per distinct additive component -- the
        ``sum_components`` branch of :meth:`get`.

        Concepts sharing a ``spec.synonym_groups`` entry are alternate
        encodings of ONE stream, not separate amounts: at most one value per
        group counts, preferring whichever member is listed earliest in
        ``spec.concepts``.
        """
        values = self._matching_component_values(spec, column)
        if not values:
            return None

        total = 0.0
        counted: set[str] = set()
        for concept in spec.concepts:
            if concept not in values or concept in counted:
                continue
            group = next((g for g in spec.synonym_groups if concept in g), (concept,))
            total += values[concept]
            counted.update(group)
        return total

    def get_all(self, item: str) -> dict[str, float]:
        """Every period-column value on registry *item*'s first-matching row --
        not just one period, the full reported series for that concept in this
        payload, keyed by its raw period_key exactly as ``iter_facts``/
        ``financial_facts`` store it. Unlike :meth:`get`, this does not resolve
        a balance-sheet item through :meth:`_instant_for` -- it returns the row's
        own columns as reported, which is what a cross-filing scale/tagging-defect
        check (:func:`fundamental_agent.db.detect_share_scale_factors`) needs: the
        same overlapping historical periods this filing itself restates.
        """
        spec = REGISTRY[item]
        for statement in spec.statements:
            for row in self.raw.get(statement, []):
                if row.get("abstract") or row.get("dimension"):
                    continue
                if _matches(row, spec):
                    out: dict[str, float] = {}
                    for column, value in row.items():
                        if _parse_period(str(column)) is None:
                            continue
                        number = _numeric(value)
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
        target = period_key[:10]
        instants = self.instant_periods()
        exact = [p for p in instants if p.date == target]
        if exact:
            return exact[0].key
        earlier = [p for p in instants if p.date <= target]
        return earlier[-1].key if earlier else None


def iter_facts(stmts: Statements) -> Iterator[dict[str, Any]]:
    """Yield one flat fact per (non-abstract, non-dimensional row, period column)."""
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
                }


def _parse_period(column: str) -> Period | None:
    duration = _DURATION_RE.match(column)
    if duration:
        return Period(key=column, date=duration["date"], tag=duration["tag"])
    instant = _INSTANT_RE.match(column)
    if instant:
        return Period(key=column, date=instant["date"], tag=INSTANT)
    return None


def _matches(row: dict[str, Any], spec: LineItem) -> bool:
    if row.get("concept") in spec.concepts:
        return True
    if spec.standard and row.get("standard_concept") in spec.standard:
        return True
    label = str(row.get("label") or "").lower()
    return any(hint in label for hint in spec.label_contains)


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None
