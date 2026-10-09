"""Level (a) measurements: the SEC ``companyfacts`` of each stored filing's own accession.

``companyfacts`` carries only non-dimensional facts of standard taxonomies, so every count here is scoped to those; a
rule that depends on a dimensional or issuer-extension fact is reported ``source prevalence incomplete``, never zero.
Facts are matched to a stored filing by accession and period end: an instant at the period end, or a duration of a
fiscal year (10-K) / a quarter (10-Q).
"""

from __future__ import annotations

import collections
from collections.abc import Callable, Iterable
from typing import Any

from sec_xcheck.common import SecClient, cik_family, days_between, index_by_accession
from sec_xcheck.extra_measures import CASH_CHANGE, IDENTITIES, identity_result
from sec_xcheck.features import IDENTITY_BALANCE, IDENTITY_FLOW
from sec_xcheck.findings import Finding, spread
from sec_xcheck.itemcheck import classify, own_concepts
from sec_xcheck.records import Rec

CMD = "uv run python scripts/audit_sec_checklist.py measure --only source"
Index = dict[str, list[tuple[str, str, dict[str, Any]]]]
G = ""  # companyfacts concepts carry no taxonomy prefix inside the us-gaap block

CHECKED_ITEMS: dict[str, bool] = {  # item -> instant?
    "revenue": False,
    "cogs": False,
    "operating_income": False,
    "pretax_income": False,
    "income_tax": False,
    "net_income": False,
    "interest_expense": False,
    "depreciation_amortization": False,
    "operating_cash_flow": False,
    "capital_expenditure": False,
    "stock_based_compensation": False,
    "total_assets": True,
    "equity": True,
    "cash": True,
    "long_term_debt": True,
    "short_term_debt": True,
}
DEAD = (
    "CostOfGoodsSold",
    "CostOfServices",
    "ShareBasedCompensationExpense",
    "AvailableForSaleSecuritiesCurrent",
)


DURATION_DAYS = {"fy": (340, 380), "q": (75, 105), "ytd": (160, 290)}


def column_end(r: Rec) -> str:
    """The period end of the column a record resolves (the filing's own period unless it is a further column)."""
    return r.column_end or r.period_end


def duration_ok(r: Rec, start: str | None) -> bool:
    """A duration fact belongs to the record's column: a year, a quarter or a year-to-date length."""
    if not start:
        return False
    kind = r.column_kind or ("fy" if r.form.startswith("10-K") else "q")
    lo, hi = DURATION_DAYS[kind]
    return lo <= days_between(start, column_end(r)) <= hi


def load_index(client: SecClient, cik: str) -> Index:
    """The ``us-gaap`` facts of a CIK (and its predecessors) grouped by accession."""
    index: Index = {}
    for c in cik_family(cik):
        doc = client.get("companyfacts", c)
        for accn, facts in (index_by_accession(doc) if doc else {}).items():
            index.setdefault(accn, []).extend(facts)
    return index


def own_facts(index: Index, r: Rec, *, instant: bool) -> dict[float, set[str]]:
    """``{value: {concepts}}`` of the accession's facts at the filing's period end."""
    out: dict[float, set[str]] = collections.defaultdict(set)
    for _tax, concept, fact in index.get(r.accession, []):
        if fact["end"] != column_end(r):
            continue
        if (fact.get("start") is None) if instant else duration_ok(r, fact.get("start")):
            out[fact["val"]].add(concept)
    return out


def concept_map(
    index: Index, r: Rec, concepts: Iterable[str], *, instant: bool
) -> dict[str, float]:
    """``{concept: value}`` of the named concepts in the accession's own period."""
    wanted = set(concepts)
    out: dict[str, float] = {}
    for _tax, concept, fact in index.get(r.accession, []):
        if concept in wanted and fact["end"] == column_end(r) and concept not in out:
            ok = (fact.get("start") is None) if instant else duration_ok(r, fact.get("start"))
            if ok:
                out[concept] = fact["val"]
    return out


class Tally:
    """Counts, companies and examples of one measurement over the filings of the source population."""

    def __init__(self) -> None:
        self.n = 0
        self.total = 0
        self.cos: set[str] = set()
        self.fids: set[int] = set()
        self.t_n = 0
        self.t_total = 0
        self.t_cos: set[str] = set()
        self.columns = False
        self.ex: list[tuple[str, str, str]] = []

    def add(self, r: Rec, hit: bool, detail: str = "") -> None:
        self.total += 1
        self.columns = self.columns or r.role != "target"
        self.t_total += r.role == "target"
        self.t_n += hit and r.role == "target"
        if hit:
            self.n += 1
            self.cos.add(r.ticker)
            self.fids.add(r.filing_id)
            if r.role == "target":
                self.t_cos.add(r.ticker)
            self.ex.append((r.ticker, r.accession, detail or f"{r.form} {r.fiscal_period}"))

    def finding(self, key: str, rules: list[str], title: str, **kw: Any) -> Finding:
        if self.columns:
            kw.setdefault("unit", "filing-columns")
        return Finding(
            key,
            rules,
            title,
            "a",
            self.n,
            self.total,
            len(self.cos),
            examples=spread(self.ex),
            command=CMD,
            filings=len(self.fids),
            target_count=self.t_n if self.columns else -1,
            target_total=self.t_total if self.columns else -1,
            target_companies=len(self.t_cos) if self.columns else -1,
            **kw,
        )


ITEM_CLASSES = (
    "FLIPPED",
    "OTHER_CONCEPT",
    "NOT_IN_SEC(rebuilt/derived)",
    "no_sec_facts_for_accn",
    "ok_own_concept",
)
OCF_LINES = (
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
)
DEBT_LINES = ("LongTermDebtNoncurrent", "LongTermDebt", "LongTermDebtCurrent")
CASH_LINES = (
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsAndShortTermInvestments",
    "ShortTermInvestments",
)
DA_LINES = (
    "DepreciationDepletionAndAmortization",
    "DepreciationAmortizationAndAccretionNet",
    "DepreciationAndAmortization",
    "Depreciation",
)
TAX_LINES = (
    "IncomeTaxExpenseBenefit",
    "CurrentIncomeTaxExpenseBenefit",
    "DeferredIncomeTaxExpenseBenefit",
)
ST_LINES = ("LongTermDebtCurrent", "ShortTermBorrowings", "DebtCurrent", "CommercialPaper")
TWO = 2
MIN_MAGNITUDE = 1e4


def item_checks(tallies: dict[str, Tally], r: Rec, index: Index, own: dict[str, set[str]]) -> None:
    """The resolver's value of each checked item against the SEC facts of the same accession and period (D1, D2)."""
    dur, ins = own_facts(index, r, instant=False), own_facts(index, r, instant=True)
    for item, is_instant in CHECKED_ITEMS.items():
        stored = r.value(item)
        if stored is None or abs(stored) < MIN_MAGNITUDE:
            continue  # small values coincide with unrelated facts (a par value, a rate)
        cls, extra = classify(stored, own.get(item, set()), ins if is_instant else dur)
        for k in ITEM_CLASSES:
            tallies[f"item.{item}.{k}"].add(
                r, cls == k, f"{r.form} {r.fiscal_period} {extra or ''}".strip()
            )


def content_checks(tallies: dict[str, Tally], r: Rec, index: Index) -> None:
    """What the filing carries at the source: the content conditions behind N3, N4, N5, N14, D4 and D-06(c)."""
    ocf = concept_map(index, r, OCF_LINES, instant=False)
    tallies["N3"].add(r, len(ocf) == TWO and len(set(ocf.values())) == TWO)
    lt = concept_map(index, r, DEBT_LINES, instant=True)
    tallies["N4"].add(
        r,
        "LongTermDebt" in lt and "LongTermDebtNoncurrent" not in lt and "LongTermDebtCurrent" in lt,
    )
    cash = concept_map(index, r, CASH_LINES, instant=True)
    plain = "CashAndCashEquivalentsAtCarryingValue" not in cash
    tallies["N5"].add(
        r,
        "CashCashEquivalentsAndShortTermInvestments" in cash
        and plain
        and "ShortTermInvestments" in cash,
    )
    tallies["N14"].add(r, set(concept_map(index, r, DA_LINES, instant=False)) == {"Depreciation"})
    tax = concept_map(index, r, TAX_LINES, instant=False)
    both = "CurrentIncomeTaxExpenseBenefit" in tax and "DeferredIncomeTaxExpenseBenefit" in tax
    tallies["D6c"].add(r, both and "IncomeTaxExpenseBenefit" not in tax)
    tallies["D4"].add(r, len(concept_map(index, r, ST_LINES, instant=True)) >= TWO)


def identity_checks(tallies: dict[str, Tally], r: Rec, index: Index) -> None:
    """The CON identities on the filed (companyfacts) values of the accession, DQ-01's tolerance."""
    values = {
        f"us-gaap_{c}": v
        for c, v in {
            **concept_map(index, r, [c.split("_", 1)[1] for c in IDENTITY_BALANCE], instant=True),
            **concept_map(
                index,
                r,
                [c.split("_", 1)[1] for c in (*IDENTITY_FLOW, *CASH_CHANGE)],
                instant=False,
            ),
        }.items()
    }
    for rule in (*IDENTITIES, "CON-07"):
        result = identity_result(values, rule)
        if result is not None:
            tallies[f"id.{rule}.block"].add(r, result == "block")
            tallies[f"id.{rule}.warn"].add(r, result == "warn")


# ID-04: the unit a registry concept's facts must carry (monetary: USD; counts: shares; per share: USD/shares).
EXPECTED_UNITS: dict[str, str] = {
    "CommonStockSharesOutstanding": "shares",
    "WeightedAverageNumberOfDilutedSharesOutstanding": "shares",
    "EarningsPerShareDiluted": "USD/shares",
}
# ID-13..15 proxy: registry concepts for which a negative value is atypical (expenses, payments, assets, revenue).
# NOT the DQC_0015/0013/0014 lists: those are spreadsheets the registered sources only link to.
ATYPICAL_NEGATIVE = (
    "Revenues", "CostOfRevenue", "CostOfGoodsAndServicesSold", "Assets", "AssetsCurrent", "Liabilities",
    "LiabilitiesCurrent", "InventoryNet", "AccountsReceivableNetCurrent", "InterestExpense",
    "PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets", "ShareBasedCompensation",
    "DepreciationDepletionAndAmortization", "LongTermDebtNoncurrent", "LongTermDebtCurrent",
    "CashAndCashEquivalentsAtCarryingValue",
)  # fmt: skip


def fact_checks(
    tallies: dict[str, Tally], r: Rec, index: Index, registry_concepts: set[str]
) -> None:
    """ID-04 (units of the registry's concepts) and the ID-13..15 proxy (negative values), on the accession's facts."""
    bad_unit = negative = False
    for _tax, concept, fact in index.get(r.accession, []):
        if concept not in registry_concepts or fact["end"] != column_end(r):
            continue
        expected = EXPECTED_UNITS.get(concept, "USD")
        bad_unit = bad_unit or fact["_unit"] != expected
        negative = negative or (concept in ATYPICAL_NEGATIVE and fact["val"] < 0)
    tallies["ID04"].add(r, bad_unit)
    tallies["ID13proxy"].add(r, negative)


def measure(recs: list[Rec], client: SecClient) -> list[Finding]:
    """Run every level (a) check; each CIK's facts are loaded once."""
    own = own_concepts()
    registry = {c for cs in own.values() for c in cs}
    tallies: dict[str, Tally] = collections.defaultdict(Tally)
    by_cik: dict[str, list[Rec]] = collections.defaultdict(list)
    for r in recs:
        by_cik[r.cik].append(r)
    dead_facts = dead_all = 0
    dead_cos: set[str] = set()
    dead_latest = ""
    for cik, group in by_cik.items():
        index = load_index(client, cik)
        ours = {r.accession for r in group}
        dead = [f for facts in index.values() for _t, c, f in facts if c in DEAD]
        dead_all += len(dead)
        if dead:
            dead_cos.add(cik)
            dead_latest = max(dead_latest, *(f["filed"] for f in dead))
        dead_facts += sum(1 for a in ours for _t, c, _f in index.get(a, []) if c in DEAD)
        for r in group:
            item_checks(tallies, r, index, own)
            content_checks(tallies, r, index)
            fact_checks(tallies, r, index, registry)
            if (
                r.role == "target"
            ):  # the balance-sheet and cash-flow identities are the filing's own
                identity_checks(tallies, r, index)
    return build(tallies, dead_facts, (dead_all, len(dead_cos), dead_latest))


def build(
    tallies: dict[str, Tally], dead_facts: int, dead_history: tuple[int, int, str]
) -> list[Finding]:
    out: list[Finding] = []
    specs: list[tuple[str, list[str], str]] = [
        (
            "N3",
            ["CON-08"],
            "Source: operating cash flow filed both in total and as continuing operations, with different values",
        ),
        (
            "N4",
            ["MET-07"],
            "Source: LongTermDebt (incl. current maturities) and LongTermDebtCurrent filed, no LongTermDebtNoncurrent",
        ),
        (
            "N5",
            ["ID-02a"],
            "Source: cash-and-short-term-investments filed beside ShortTermInvestments, no plain cash line",
        ),
        ("N14", ["ID-02b"], "Source: Depreciation is the only D&A concept filed"),
        ("D6c", ["ID-02b"], "Source: current and deferred income tax filed, no total"),
        ("D4", ["ID-02b", "MET-07"], "Source: two or more short-term debt concepts filed"),
    ]
    for key, rules, title in specs:
        out.append(tallies[key].finding(f"src.{key}", rules, title))
    out.append(
        tallies["ID04"].finding(
            "src.ID04.unit_mismatch",
            ["ID-04"],
            "Source: a registry concept's fact at the period end carries an unexpected unit (not USD / shares / USD per share)",
        )
    )
    out.append(
        tallies["ID13proxy"].finding(
            "src.ID13.negative_atypical_proxy",
            ["ID-13", "ID-14", "ID-15"],
            "Source: a registry concept for which a negative value is atypical is negative at the period end (PROXY, not the DQC lists)",
            incomplete=True,
            note="the DQC_0015/0013/0014 element lists are spreadsheets the registered sources only link to; this counts registry concepts instead",
        )
    )
    for key, t in sorted(tallies.items()):
        if key.startswith("id."):
            _, rule, level = key.split(".")
            out.append(
                t.finding(
                    f"src.{rule}.{level}",
                    [rule, "DQ-01"],
                    f"Source: {rule} identity fails at DQ-01 level {level.upper()} on the filed values (rounding assumed USD 1M)",
                    unit="filings with all operands",
                )
            )
            continue
        if not key.startswith("item."):
            continue
        _, item, cls = key.split(".", 2)
        if cls == "FLIPPED":
            out.append(
                t.finding(
                    f"src.{item}.FLIPPED",
                    ["ID-12", "ID-11"],
                    f"Resolver value for {item} is the negative of the source fact",
                )
            )
        elif cls == "OTHER_CONCEPT":
            out.append(
                t.finding(
                    f"src.{item}.OTHER_CONCEPT",
                    ["ID-02a", "ID-02b"],
                    f"Resolver value for {item} equals a source fact of a concept the registry does not list for it",
                )
            )
    dead_all, dead_ciks, dead_latest = dead_history
    history_note = (
        f"filed by {dead_ciks} CIKs, the latest on {dead_latest}: all before our first filing (2022-01-06), "
        "so no value of ours was ever read through a dead name; the names were retired before the window"
    )
    for key, title, n, ciks, note in (
        (
            "ours",
            "Facts of the four dead registry concepts in the accessions of our stored filings",
            dead_facts,
            0,
            "",
        ),
        (
            "all_history",
            "Facts of the four dead registry concepts in any filing of the 501 CIKs (any year)",
            dead_all,
            dead_ciks,
            history_note,
        ),
    ):
        out.append(
            Finding(
                f"src.N12.dead_concept_facts_{key}", ["ID-20"], title, "a", n, n, ciks, unit="facts", command=CMD, note=note
            )
        )  # fmt: skip
    return out


Predicate = Callable[[Rec], bool]
