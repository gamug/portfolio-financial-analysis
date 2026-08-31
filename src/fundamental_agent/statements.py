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
    """

    name: str
    statements: tuple[str, ...]
    concepts: tuple[str, ...] = ()
    standard: tuple[str, ...] = ()
    label_contains: tuple[str, ...] = ()


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
            "us-gaap_RevenuesNetOfInterestExpense",
            "us-gaap_Revenues",
            "us-gaap_RevenueFromContractWithCustomerIncludingAssessedTax",
        ),
        label_contains=("net sales", "total net revenue", "total revenue"),
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
        "net_income", _INCOME, concepts=("us-gaap_NetIncomeLoss", "us-gaap_ProfitLoss")
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

    def get(self, item: str, period_key: str) -> float | None:
        """Return the value of registry *item* for *period_key*, or ``None``."""
        spec = REGISTRY[item]
        column = period_key
        if spec.statements == _BALANCE:
            instant = self._instant_for(period_key)
            if instant is None:
                return None
            column = instant
        for statement in spec.statements:
            for row in self.raw.get(statement, []):
                if row.get("abstract") or row.get("dimension"):
                    continue
                if _matches(row, spec):
                    value = _numeric(row.get(column))
                    if value is not None:
                        return value
        return None

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
