"""CON identities (DQ-01), financial-firm applicability (APP-00 to APP-09), REIT tax (APP-04c) and the stored scores
(N2, MET-00 neutral 50, DQ-02 winsorizing).

Identity tolerance is DQ-01's: ``threshold = max(rounding, 0.5% x |base|)``; pass within rounding, WARN above rounding
and below the threshold, BLOCK above it. ``decimals`` is not captured, so *rounding* is taken as USD 1,000,000, the
unit in which large filers round; that assumption is part of every count's note.
"""

from __future__ import annotations

import json
import statistics
from typing import Any

from cycle.scores.normalize import normalized_scores
from sec_xcheck.cycle_measures import latest_annual
from sec_xcheck.findings import Finding, spread
from sec_xcheck.records import Rec, count

G = "us-gaap_"
CMD = "uv run python scripts/audit_sec_checklist.py measure --only extra"
ROUNDING = 1_000_000.0
DEFAULT_TAX_RATE = 0.21
MATERIALITY = 0.005
IDENTITIES: dict[str, tuple[str, tuple[str, ...], tuple[str, ...], str]] = {
    # rule: (title, lhs concepts, rhs concepts summed, base concept)
    "CON-01": ("Assets = Liabilities and equity", ("Assets",), ("LiabilitiesAndStockholdersEquity",), "Assets"),
    "CON-02": ("Assets = current + noncurrent", ("Assets",), ("AssetsCurrent", "AssetsNoncurrent"), "Assets"),
    "CON-03": ("Liabilities = current + noncurrent", ("Liabilities",), ("LiabilitiesCurrent", "LiabilitiesNoncurrent"), "Assets"),
    "CON-04": (
        "Equity incl. NCI = parent equity + MinorityInterest",
        ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",),
        ("StockholdersEquity", "MinorityInterest"),
        "Assets",
    ),
    "CON-19": ("ProfitLoss = NetIncomeLoss + NCI income", ("ProfitLoss",), ("NetIncomeLoss", "NetIncomeLossAttributableToNoncontrollingInterest"), "ProfitLoss"),
    "CON-08": (
        "Operating cash flow = continuing + discontinued",
        ("NetCashProvidedByUsedInOperatingActivities",),
        ("NetCashProvidedByUsedInOperatingActivitiesContinuingOperations", "CashProvidedByUsedInOperatingActivitiesDiscontinuedOperations"),
        "NetCashProvidedByUsedInOperatingActivities",
    ),
}  # fmt: skip
CASH_CHANGE = (
    G
    + "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect",
    G + "CashAndCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect",
)
OCF, ICF, FCF = (
    G + "NetCashProvidedByUsedInOperatingActivities",
    G + "NetCashProvidedByUsedInInvestingActivities",
    G + "NetCashProvidedByUsedInFinancingActivities",
)


def severity(diff: float, base: float) -> str:
    """DQ-01: ``pass`` within rounding, ``warn`` above rounding and below the threshold, ``block`` above it."""
    threshold = max(ROUNDING, MATERIALITY * abs(base))
    if diff <= ROUNDING:
        return "pass"
    return "warn" if diff <= threshold else "block"


def identity_result(values: dict[str, float], rule: str) -> str | None:
    """``pass``/``warn``/``block`` for *rule* over a filing's concept values, or ``None`` when an operand is not filed."""
    if rule == "CON-07":
        change = next((values[c] for c in CASH_CHANGE if c in values), None)
        if change is None or any(c not in values for c in (OCF, ICF, FCF)):
            return None
        return severity(abs(change - (values[OCF] + values[ICF] + values[FCF])), change)
    _title, lhs, rhs, base = IDENTITIES[rule]
    left = [G + c for c in lhs]
    right = [G + c for c in rhs]
    if any(c not in values for c in left):
        return None
    if rule == "CON-04" and G + "MinorityInterest" not in values:
        return None  # no NCI: parent equity is total equity, not an identity to test
    if (
        rule == "CON-08"
        and G + "CashProvidedByUsedInOperatingActivitiesDiscontinuedOperations" not in values
    ):
        right = right[:1]  # no discontinued operations filed: the identity reads total = continuing
    if any(c not in values for c in right):
        return None
    total = sum(values[c] for c in left)
    diff = abs(total - sum(values[c] for c in right))
    if (
        rule == "CON-19"
    ):  # the NCI income line is displayed as a deduction by the statement parsers (D1)
        nci = G + "NetIncomeLossAttributableToNoncontrollingInterest"
        diff = min(diff, abs(total - (values[G + "NetIncomeLoss"] - values[nci])))
    return severity(diff, values.get(G + base, total))


def m_identities(recs: list[Rec]) -> list[Finding]:
    out: list[Finding] = []
    for rule in (*IDENTITIES, "CON-07"):
        results = [(r, identity_result(r.f["identity"], rule)) for r in recs]
        tested = [(r, x) for r, x in results if x is not None]
        for level in ("block", "warn"):
            hits = [r for r, x in tested if x == level]
            out.append(
                Finding(
                    f"{rule}.{level}", [rule, "DQ-01"], f"{rule} identity fails at DQ-01 level {level.upper()} (rounding assumed USD 1M)", "b",
                    len(hits), len(tested), len({r.ticker for r in hits}), unit="filings with all operands",
                    examples=spread([(r.ticker, r.accession, f"{r.form} {r.fiscal_period}") for r in hits]), command=CMD,
                    note="stored statement rows only; operands on notes or dimensions are not captured",
                )
            )  # fmt: skip
    return out


# ---- APP-02 / APP-09: metrics the matrix marks NA for financial firms, computed and scored anyway
NA_FOR_FINANCIAL = (
    "return_on_assets", "debt_to_equity", "debt_to_assets", "interest_coverage", "net_debt_to_ebitda", "nopat",
    "return_on_invested_capital", "free_cash_flow_margin", "free_cash_flow_conversion", "capex_intensity",
    "operating_cash_flow_margin", "free_cash_flow_growth", "operating_cash_flow_cagr",
)  # fmt: skip
NA_FOR_BANKS_AND_INSURERS = (
    "gross_margin",
    "current_ratio",
    "quick_ratio",
    "cash_ratio",
    "inventory_turnover",
)


def m_applicability(recs: list[Rec], types: dict[str, str]) -> list[Finding]:
    fin = {"article_9", "article_7", "other_financial"}
    out: list[Finding] = []
    pool = [r for r in recs if types.get(r.cik) in fin]

    def anyna(r: Rec) -> bool:
        return any(r.f["metrics"].get(m, {}).get("value") is not None for m in NA_FOR_FINANCIAL)

    out.append(
        count(
            "APP02.na_metrics_computed",
            ["APP-02", "APP-02b"],
            "Financial-firm filings with at least one metric the matrix marks NA computed",
            pool,
            anyna,
            command=CMD,
            note="metrics: " + ", ".join(NA_FOR_FINANCIAL),
        )
    )
    for m in NA_FOR_FINANCIAL:
        out.append(
            count(
                f"APP02.{m}",
                ["APP-02" if "cash" not in m and "capex" not in m else "APP-02b"],
                f"Financial-firm filings with {m} computed (NA)",
                pool,
                lambda r, m=m: r.f["metrics"].get(m, {}).get("value") is not None,
                command=CMD,
            )
        )
    banks = [r for r in recs if types.get(r.cik) in ("article_9", "article_7")]
    out.append(
        count(
            "APP01.liquidity_ratios_computed",
            ["APP-01", "APP-03", "APP-09"],
            "Article 9/7 filings with a current ratio computed (a classified balance sheet exists)",
            banks,
            lambda r: r.f["metrics"].get("current_ratio", {}).get("value") is not None,
            command=CMD,
        )
    )
    out.append(
        count(
            "APP01.gross_margin_computed",
            ["APP-01", "APP-03"],
            "Article 9/7 filings with a gross margin computed (no cost of sales exists)",
            banks,
            lambda r: r.f["metrics"].get("gross_margin", {}).get("value") is not None,
            command=CMD,
        )
    )
    return out


def m_reit_tax(recs: list[Rec], types: dict[str, str]) -> list[Finding]:
    reits = [r for r in recs if r.sector == "Real Estate" and r.sub_industry.endswith("REITs")]
    f = count(
        "APP04c.reit_tax_rate_21pct",
        ["APP-04c"],
        "REIT filings whose effective tax rate / NOPAT use the 21% default (pre-tax income <= 0 or missing)",
        reits,
        lambda r: r.f["metrics"].get("effective_tax_rate", {}).get("value") == DEFAULT_TAX_RATE,
        command=CMD,
    )
    g = count(
        "APP04c.reit_filed_rate_clamped",
        ["APP-04c", "MET-03"],
        "REIT filings whose NOPAT uses the filing's own effective rate (APP-04c: allowed when pre-tax > 0)",
        reits,
        lambda r: r.f["metrics"].get("effective_tax_rate", {}).get("value") != DEFAULT_TAX_RATE,
        command=CMD,
    )
    return [f, g]


# ---- stored scores: N2, MET-00 neutral 50, DQ-02
def m_scores(conn: Any, label: str) -> list[Finding]:
    cmd = "uv run python scripts/audit_sec_checklist.py measure --only extra --db <db>"
    rows = conn.execute(
        "SELECT asset_id, event_time, raw_value, inputs_json FROM score_snapshot WHERE score_type = 'VALORIZATION'"
    ).fetchall()
    parts = [json.loads(r["inputs_json"] or "{}") for r in rows]
    n = len(rows)
    names = conn.execute(
        "SELECT DISTINCT metric_group || '.' || metric_name AS k FROM fundamental_metrics"
    ).fetchall()
    keys = {r["k"] for r in names}
    named_net_income = sum(
        1 for k in keys if k in ("profitability.net_income", "income_statement.net_income")
    )
    versions = ", ".join(
        r[0]
        for r in conn.execute("SELECT DISTINCT engine_version FROM fundamental_metrics ORDER BY 1")
    )
    value_null = sum(1 for p in parts if p.get("value") is None)
    neutral = sum(1 for p in parts if all(p.get(k) is None for k in ("value", "quality", "size")))
    return [
        Finding(
            "N2.metrics_named_net_income",
            ["MET-05", "MET-00"],
            f"[{label}; stored metrics {versions}] Stored metrics named profitability.net_income or income_statement.net_income (the key earnings_yield reads)",
            "b",
            named_net_income,
            len(keys),
            0,
            unit="metric names",
            command=cmd,
            note="0 means earnings_yield is never computed: net_income is only an audit input of the metrics, never a metric",
        ),
        Finding(
            "N2.valorization_value_factor_null",
            ["MET-05"],
            f"[{label}; scores built on metrics {versions}] VALORIZATION rows whose value factor is entirely missing",
            "b",
            value_null,
            n,
            0,
            unit="score rows",
            command=cmd,
        ),
        Finding(
            "MET00.valorization_neutral_50",
            ["MET-00"],
            f"[{label}; scores built on metrics {versions}] VALORIZATION rows with every factor missing (scored 50)",
            "b",
            neutral,
            n,
            0,
            unit="score rows",
            command=cmd,
        ),
    ]


def winsor_effect(raw: list[float]) -> tuple[int, float]:
    """``(assets whose score moves by more than 1 point, max move)`` between 2% and 0.5% winsorizing of *raw*."""
    a, b = normalized_scores(raw, winsor=0.02), normalized_scores(raw, winsor=0.005)
    moves = [abs(x - y) for x, y in zip(a, b, strict=True)]
    return sum(1 for m in moves if m > 1.0), max(moves, default=0.0)


def m_winsor(conn: Any, label: str) -> list[Finding]:
    """DQ-02 / N17: what changes between winsorizing at 2% and 0.5% on the stored cross-sections."""
    cmd = "uv run python scripts/audit_sec_checklist.py measure --only extra --db <db>"
    out: list[Finding] = []
    moved = total = 0
    sizes: set[int] = set()
    worst = 0.0
    for stype in ("VALORIZATION", "TECHNICAL"):
        dates = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT event_time FROM score_snapshot WHERE score_type = ?", (stype,)
            )
        ]
        for d in dates:
            raw = [
                float(r[0])
                for r in conn.execute(
                    "SELECT raw_value FROM score_snapshot WHERE score_type = ? AND event_time = ? AND raw_value IS NOT NULL",
                    (stype, d),
                )
            ]
            if len(raw) < 5:  # noqa: PLR2004
                continue
            m, mx = winsor_effect(raw)
            moved += m
            total += len(raw)
            sizes.add(len(raw))
            worst = max(worst, mx)
    out.append(
        Finding(
            "DQ02.winsor_2pct_vs_half_pct_stored",
            ["DQ-02"],
            f"[{label}] Stored cross-sections: assets whose normalized score moves by more than 1 point between 2% and 0.5% winsorizing",
            "b",
            moved,
            total,
            0,
            unit="asset-cross-sections",
            command=cmd,
            note=f"cohort sizes {sorted(sizes)}; max move {worst:.2f} points. With n < 100, int(n x frac) is 0 for both fractions and max(1, ...) trims one value per tail (5% at n = 20): N17",
        )
    )
    return out


def full_universe_winsor(recs: list[Rec], as_of: str) -> list[Finding]:
    """The same comparison on a real cross-section of the whole universe: the ROE of each asset's latest 10-K."""
    annual = latest_annual(recs, as_of)
    raw = [
        v
        for r in annual.values()
        if (v := r.f["metrics"].get("return_on_equity", {}).get("value")) is not None
    ]
    moved, worst = winsor_effect(raw)
    return [
        Finding(
            "DQ02.winsor_2pct_vs_half_pct_universe",
            ["DQ-02"],
            f"Whole-universe ROE cross-section on {as_of}: assets whose normalized score moves by more than 1 point between 2% and 0.5% winsorizing",
            "b",
            moved,
            len(raw),
            0,
            unit="assets",
            command=CMD,
            note=f"max move {worst:.2f} points; median |ROE| {statistics.median(abs(x) for x in raw):.3f}",
        )
    ]
