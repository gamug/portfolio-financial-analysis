"""The golden set: the pilot's 20 tickers plus an insurer (TRV), GOOGL and BRK.B (Phase 5 of ``review_plan_v1.md``).

It records **semantic choices only**, never numbers: for each company, which statement line the resolver takes as
revenue, net income, equity, debt and capex in its latest 10-K, what the checklist expects for that company type, and
whether the two agree. The user checks each row by hand against the 10-K (5 to 8 metrics and these choices).
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from sec_xcheck.records import Rec

PILOT = (
    "APA",
    "APO",
    "BF.B",
    "CPT",
    "ESS",
    "HOOD",
    "HUM",
    "MA",
    "MCD",
    "NEE",
    "PG",
    "PM",
    "PSX",
    "SBAC",
    "STZ",
    "T",
    "UDR",
    "WAT",
    "WFC",
    "XOM",
)
GOLDEN = (*PILOT, "TRV", "GOOGL", "BRK.B")
FIELDS = [
    "ticker", "company", "type", "overlays", "gics_sub_industry", "latest_10k", "revenue_line", "revenue_expected", "net_income_line",
    "equity_line", "long_term_debt_line", "short_term_debt_lines", "debt_expected", "capex_resolved", "capex_lines_filed",
    "capex_expected", "ffo", "share_basis", "agree", "to_check_by_hand",
]  # fmt: skip
# What the checklist expects, by company type or by name; the text names the rule that says so.
REVENUE_EXPECTED = {
    "article_9": "the total net revenue concept (Revenues or RevenuesNetOfInterestExpense); no cost of sales (APP-01)",
    "article_7": "premiums plus investment income (APP-03); no cost of sales",
    "other_financial": "the filed total revenue concept (CON-11); fees for managers and brokers",
    "operating": "the filed total (Revenues / RevenueFromContractWithCustomer...) -- never a component (CON-11)",
}
CAPEX_EXPECTED = {
    "utility": "PP&E payments (PaymentsToAcquirePropertyPlantAndEquipment); a utility's construction expenditures, no acquisitions (MET-08)",
    "oil_gas": "exploration & development, plus equipment lines when separate (MET-08 (c) tie-break); never mineral-interest purchases or business acquisitions (MET-08 (a))",
    "reit": "corporate items + existing properties + development + acquisitions (APP-04b); PaymentsForCapitalImprovements never added to an acquisition line",
    "financial": "not applicable (APP-02b)",
    "default": "PP&E payments; productive assets only as a flagged fallback (MET-08)",
}
DEBT_EXPECTED = "narrowest long-term line first, current maturities not counted twice, operating leases excluded; the combined debt-and-lease element only when it is the only line, flagged (MET-07)"


def capex_kind(r: Rec, ctype: str) -> str:
    if ctype in ("article_9", "article_7", "other_financial"):
        return "financial"
    if r.sector == "Real Estate" and r.sub_industry.endswith("REITs"):
        return "reit"
    if r.sector == "Utilities":
        return "utility"
    if r.sub_industry in (
        "Oil & Gas Exploration & Production",
        "Integrated Oil & Gas",
        "Oil & Gas Refining & Marketing",
    ):
        return "oil_gas" if r.sub_industry.startswith("Oil & Gas Explor") else "default"
    return "default"


def line(item: dict[str, Any]) -> str:
    if item["value"] is None:
        return "(none resolved)"
    concept = (item["concept"] or "").replace("us-gaap_", "")
    return f"{concept or item['how']} [{item['how']}]"


def overlays(t: dict[str, str]) -> str:
    """The APP-00 overlays of a company: REIT, negative equity (latest 10-K), several listed tickers."""
    names = [
        ("reit", "REIT"),
        ("negative_equity_latest", "negative equity"),
        ("multi_class_listed", "multi-class"),
    ]
    return ", ".join(label for key, label in names if t.get(key) == "True") or "none"


def row(r: Rec, ctype: str, company: str, overlay: str = "none") -> dict[str, Any]:
    items = r.f["items"]
    capex_kind_ = capex_kind(r, ctype)
    lt, st = r.f["lt"], r.f["st"]
    combined = any("CapitalLease" in c for c in lt)
    notes: list[str] = []
    if ctype in ("article_9", "article_7") and items["revenue"]["value"] is None:
        notes.append("revenue not resolved for a financial filer")
    if combined:
        notes.append(
            "long-term debt is a combined debt-and-lease element: must be flagged (MET-07)"
        )
    if items["capital_expenditure"]["how"] in ("label_fallback", "fallback3", "fallback4"):
        notes.append(f"capex via {items['capital_expenditure']['how']}")
    if items["net_income"]["concept"] == "us-gaap_NetIncomeLossAvailableToCommonStockholdersBasic":
        notes.append("net income is after preferred dividends, not parent net income (N13: DECIDE)")
    if items["net_income"]["concept"] == "us-gaap_ProfitLoss":
        notes.append("net income is ProfitLoss (includes NCI): parent earnings expected (ID-03)")
    if capex_kind_ == "financial" and items["capital_expenditure"]["value"] is not None:
        notes.append(
            "a capex is resolved although FCF metrics are NA for financial firms (APP-02b)"
        )
    return {
        "ticker": r.ticker, "company": company, "type": ctype, "overlays": overlay, "gics_sub_industry": r.sub_industry,
        "latest_10k": f"{r.accession} (FY ending {r.period_end})", "revenue_line": line(items["revenue"]),
        "revenue_expected": REVENUE_EXPECTED.get(ctype, REVENUE_EXPECTED["operating"]),
        "net_income_line": line(items["net_income"]), "equity_line": line(items["equity"]),
        "long_term_debt_line": line(items["long_term_debt"]),
        "short_term_debt_lines": ", ".join(c.replace("us-gaap_", "") for c in st) or "(none filed)",
        "debt_expected": DEBT_EXPECTED, "capex_resolved": line(items["capital_expenditure"]),
        "capex_lines_filed": ", ".join(f"{c.replace('us-gaap_', '')}" for c in r.f["capex"]) or "(none)",
        "capex_expected": CAPEX_EXPECTED[capex_kind_],
        "ffo": "not computed (L-02)" if capex_kind_ == "reit" else "n/a",
        "share_basis": "cover-page total, all classes (MKT-03/04); one price per class",
        "agree": "no" if notes else "yes (by the resolver's tags; confirm by hand)",
        "to_check_by_hand": "; ".join(notes) or "5-8 metrics against the 10-K",
    }  # fmt: skip


def build(
    recs: list[Rec], types: dict[str, dict[str, str]], tickers: tuple[str, ...] = GOLDEN
) -> list[dict[str, Any]]:
    latest: dict[str, Rec] = {}
    for r in recs:
        if (
            r.ticker in tickers
            and r.form == "10-K"
            and (r.ticker not in latest or r.period_end > latest[r.ticker].period_end)
        ):
            latest[r.ticker] = r
    by_ticker = {
        t: (c, row_["type"]) for c, row_ in types.items() for t in row_["tickers"].split("/")
    }
    out = []
    for t in tickers:
        if t in latest:
            cik, ctype = by_ticker[t]
            out.append(row(latest[t], ctype, types[cik]["company_name"], overlays(types[cik])))
    return out


def write(rows: list[dict[str, Any]], csv_path: Path, md_path: Path) -> None:
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    lines = [
        "# Golden set — semantic choices (T-147)",
        "",
        "The pilot's 20 tickers plus TRV (an insurer), GOOGL and BRK.B. **Semantic choices only**: which line is revenue, "
        "which debt and capex lines count, and whether FFO is computable. No number is recorded. The resolver's choice is read "
        "from the stored facts of each company's latest 10-K (`scripts/sec_xcheck/golden.py`); the *expected* column is what "
        "checklist v1.2.2 says for that company type. `agree = no` rows are the ones to check first against the 10-K by hand; "
        "`yes` means only that the resolver's tag matches the expectation, not that a human has confirmed the line.",
        "",
        "| Ticker | Type | Overlays | Latest 10-K | Revenue line (resolver) | Net income | Equity | Long-term debt | Short-term debt lines filed | Capex resolved | Capex lines filed | Agree | To check by hand |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['ticker']} | {r['type']} | {r['overlays']} | {r['latest_10k']} | {r['revenue_line']} | {r['net_income_line']} | {r['equity_line']} | "
            f"{r['long_term_debt_line']} | {r['short_term_debt_lines']} | {r['capex_resolved']} | {r['capex_lines_filed']} | {r['agree']} | {r['to_check_by_hand']} |"
        )
    lines += [
        "",
        "## What the checklist expects, by type",
        "",
        "| Choice | Expectation |",
        "|---|---|",
    ]
    lines += [f"| Revenue, {k} | {v} |" for k, v in REVENUE_EXPECTED.items()]
    lines += [f"| Capex, {k} | {v} |" for k, v in CAPEX_EXPECTED.items()]
    lines += [f"| Debt | {DEBT_EXPECTED} |", "| FFO | not computed for any REIT (L-02) |", ""]
    md_path.write_text("\n".join(lines), encoding="utf-8")
