"""Render ``docs/sec_data_checklist.md`` (T-147, Phase 3) from the rule table, the findings and the supplement tables.

    uv run python scripts/audit_sec_checklist.py report

Every number comes from ``prevalence_results.json`` (the findings), ``prevalence_tables.json``, ``fidelity_results.json``,
``d07_replay.json``, ``company_types.csv`` or the checklist itself, so the document is regenerated, not retyped. A finding key
that a rule names but the results lack stops the render: a dangling reference never reaches the thesis.
"""

from __future__ import annotations

import collections
import csv
import json
import re
from pathlib import Path
from typing import Any

from sec_xcheck import defects, quotecheck
from sec_xcheck.common import REPO
from sec_xcheck.findings import Finding, from_json
from sec_xcheck.rules_table import ANNEX_A, INCOMPLETE, LIMITATIONS, RULES

CHECKLIST = REPO / "docs" / "checklist_sec"
SEV_WEIGHT = {"BLOCK": 3, "WARN": 2, "INFO": 1, "DQ-01": 2}
REACH_WEIGHT = {"veto": 3, "score": 2, "none": 1}
REACH_TEXT = {"veto": "a veto", "score": "a score", "none": "neither (today)"}


class Results:
    """The findings by ``(key, db)`` and the supplement tables."""

    def __init__(self, directory: Path = CHECKLIST) -> None:
        self.findings = from_json(
            (directory / "prevalence_results.json").read_text(encoding="utf-8")
        )
        self.by: dict[tuple[str, str], Finding] = {(f.key, f.db): f for f in self.findings}
        self.tables: dict[str, Any] = json.loads(
            (directory / "prevalence_tables.json").read_text(encoding="utf-8")
        )
        self.fidelity: dict[str, Any] = json.loads(
            (directory / "fidelity_results.json").read_text(encoding="utf-8")
        )
        self.d07: dict[str, Any] = json.loads(
            (directory / "d07_replay.json").read_text(encoding="utf-8")
        )
        with (directory / "company_types.csv").open(encoding="utf-8") as fh:
            self.types = list(csv.DictReader(fh))
        with (directory / "l09_verification.csv").open(encoding="utf-8") as fh:
            self.l09 = list(csv.DictReader(fh))

    def get(self, key: str, db: str = "production") -> Finding:
        try:
            return self.by[(key, db)]
        except KeyError:
            raise KeyError(
                f"no finding {key!r} for {db}: a rule or defect names a measurement that was not run"
            ) from None

    def has(self, key: str, db: str) -> bool:
        return (key, db) in self.by


def n(x: float) -> str:
    return f"{int(x):,}"


def tickers(f: Finding) -> str:
    seen: list[str] = []
    for t, _a, _d in f.examples:
        if t not in seen:
            seen.append(t)
    return ", ".join(seen[:5])


def fmt(f: Finding) -> str:
    """One finding as a cell fragment: key, affected / population, companies and up to five tickers."""
    unit = {
        "filings": "f",
        "filing-columns": "cols",
        "companies": "cos",
        "10-K filings": "10-Ks",
        "company-years": "co-yrs",
    }.get(f.unit, f.unit)
    base = f"`{f.key}` {n(f.count)}/{n(f.total)} {unit}"
    if f.filings and f.unit == "filing-columns":
        base += f" ({n(f.filings)} filings)"
    if f.companies and f.unit != "companies":
        base += f", {n(f.companies)} cos"
    who = tickers(f)
    return base + (f" [{who}]" if who and f.count else "")


def evidence(res: Results, keys: tuple[str, ...], level: str) -> str:
    if not keys:
        return "—"
    if keys == (INCOMPLETE,):
        return (
            "**source prevalence incomplete** (dimensional or extension facts; not counted as zero)"
            if level == "a"
            else "**not replayable from storage** (no dimensional rows are stored)"
        )
    parts = []
    for key in keys:
        if key == INCOMPLETE:
            parts.append("**source prevalence incomplete**")
            continue
        parts.append(fmt(res.get(key)))
        if res.has(key, "pilot") and res.get(key, "pilot").count:
            p = res.get(key, "pilot")
            parts.append(f"pilot {n(p.count)}/{n(p.total)}")
    return "; ".join(parts)


def severities() -> dict[str, tuple[str, str]]:
    text = (CHECKLIST / "checklist_v1.2.2.md").read_text(encoding="utf-8")
    return {rid: (cells[7], cells[1]) for rid, cells in quotecheck.rows(text)}


def cell(value: object) -> str:
    """A table cell: a literal ``|`` would split the row on GitHub, so it is escaped (an escaped one is left alone)."""
    return re.sub(r"(?<!\\)\|", r"\\|", str(value))


def row(*cells: object) -> str:
    return "| " + " | ".join(cell(c) for c in cells) + " |"


def short(text: str, width: int = 130) -> str:
    text = text.replace("|", "/")
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def rule_table(res: Results) -> list[str]:
    sev = severities()
    lines = [
        "| Rule | Sev. | What the rule says | Implemented in | Status | Test that pins it | Evidence (a) the SEC source | Evidence (b) our resolver / stored rows | Class | Task |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for rid in sev:
        entry = RULES[rid]
        severity, text = sev[rid]
        a = evidence(res, entry.a, "a")
        b = evidence(res, entry.b, "b")
        note = f" *{entry.note}*" if entry.note else ""
        lines.append(
            row(
                rid, severity, short(text), entry.impl, entry.status, entry.test, a, f"{b}{note}",
                entry.cls or "—", entry.task or "—",
            )
        )  # fmt: skip
    missing = sorted(set(sev) - set(RULES)) + sorted(set(RULES) - set(sev))
    if missing:
        raise KeyError(f"rules out of step with the checklist: {missing}")
    return lines


def status_summary() -> list[str]:
    layers = ["ID", "PER", "CON", "DQ", "MET", "MKT", "APP"]
    cols = ["✓", "partial", "✗", "n/a", "L"]
    count: dict[str, collections.Counter[str]] = {k: collections.Counter() for k in layers}
    for rid, entry in RULES.items():
        status = entry.status
        key = (
            "partial" if status.startswith("partial") else "✗" if status.startswith("✗") else status
        )
        count[rid.split("-")[0]][key] += 1
    lines = [
        "| Layer | Rules | ✓ | partial (incl. untested) | ✗ | n/a | L |",
        "|---|---|---|---|---|---|---|",
    ]
    for layer in layers:
        c = count[layer]
        lines.append(
            f"| {layer} | {sum(c.values())} | " + " | ".join(str(c[k]) for k in cols) + " |"
        )
    total: collections.Counter[str] = sum(count.values(), collections.Counter())
    lines.append(
        f"| **All** | **{sum(total.values())}** | "
        + " | ".join(f"**{total[k]}**" for k in cols)
        + " |"
    )
    classes: collections.Counter[str] = collections.Counter()
    for entry in RULES.values():
        base = entry.cls.split(" ")[0].strip("(").rstrip(",;") if entry.cls else "—"
        classes[base] += 1
    lines += [
        "",
        "Class of the 78 rules (the first class named): "
        + ", ".join(f"{k} {v}" for k, v in sorted(classes.items())),
    ]
    return lines


# ---- the prevalence ranking: severity x companies affected x whether the value reaches a score or a veto
HEADLINES: list[tuple[str, str, str, str, str, str, str, int]] = [
    # (what, finding key, rule, severity, reach, task, what it feeds, companies override or 0)
    (
        "Income tax displayed with the opposite sign of the filed value",
        "src.income_tax.FLIPPED",
        "ID-12",
        "BLOCK",
        "score",
        "T-148",
        "effective tax rate → NOPAT, ROIC, enterprise FCF yield",
        0,
    ),
    (
        "Net income read with NCI (ProfitLoss) while the parent line is filed",
        "D2.net_income_profitloss_over_parent",
        "ID-03",
        "BLOCK",
        "score",
        "T-148",
        "net margin, ROE, ROA, earnings yield, growth",
        0,
    ),
    (
        "EV and ROIC fill a missing debt or cash term with 0",
        "D10.ev_debt_or_cash_zero_filled",
        "MET-00",
        "BLOCK",
        "score",
        "T-148",
        "enterprise value, enterprise FCF yield, ROIC",
        0,
    ),
    (
        "NOPAT taxed at the filing's own effective rate, not the 21% federal rate",
        "MET03.nopat_on_filed_rate",
        "MET-03",
        "BLOCK",
        "score",
        "T-151",
        "ROIC",
        0,
    ),
    (
        "Growth computed from a base <= 0 (divides by |prior|)",
        "MET04.net_income_growth_base_nonpositive",
        "MET-04",
        "BLOCK",
        "score",
        "T-151",
        "growth metrics read by the FUNDAMENTAL score",
        0,
    ),
    (
        "Effective tax rate shows 21% when pre-tax income is missing or <= 0",
        "N10.tax_rate_21_on_nonpositive_pretax",
        "MET-03",
        "BLOCK",
        "score",
        "T-151",
        "effective_tax_rate, NOPAT",
        0,
    ),
    (
        "Capex resolved to PaymentsToAcquireProductiveAssets, unflagged",
        "MET08b.productive_assets_unflagged",
        "MET-08",
        "BLOCK",
        "veto",
        "T-148",
        "FCF → NEGATIVE_FCF, FCF yields",
        0,
    ),
    (
        "Capex found by a caption (a custom concept), software not excluded",
        "MET08.custom_caption_fallback",
        "MET-08",
        "BLOCK",
        "veto",
        "T-148",
        "FCF → NEGATIVE_FCF, FCF yields",
        0,
    ),
    (
        "An oil & gas fallback tier displaces the filer's real capital-expenditures line (DOW: 0.157B for 2.48B)",
        "N22.og_tier_displaces_capex_line",
        "MET-08",
        "BLOCK",
        "veto",
        "T-148",
        "FCF → NEGATIVE_FCF, FCF yields",
        0,
    ),
    (
        "D&A resolved to Depreciation alone: EBITDA understated",
        "N14.da_depreciation_alone",
        "ID-02b",
        "BLOCK",
        "veto",
        "T-148",
        "net debt/EBITDA → LEVERAGE_EXTREME, DQ_NEG_EQUITY",
        0,
    ),
    (
        "Short-term debt: today's first match differs from D-06(d)'s order",
        "D4.short_term_debt_order_differs",
        "ID-02b",
        "BLOCK",
        "veto",
        "T-148",
        "D/E → LEVERAGE_EXTREME, net debt/EBITDA, EV",
        0,
    ),
    (
        "Operating cash flow resolves to the continuing-operations line",
        "N3.ocf_continuing_resolved",
        "CON-08",
        "BLOCK",
        "veto",
        "T-148",
        "FCF margin → NEGATIVE_FCF",
        0,
    ),
    (
        "A net-cash company with positive EBITDA is discarded (N9)",
        "N9.net_cash_positive_ebitda",
        "MET-09",
        "BLOCK",
        "veto",
        "T-151",
        "net debt/EBITDA in the distress rules",
        0,
    ),
    (
        "Combined debt-and-lease element used as long-term debt",
        "MET07.combined_debt_lease_element",
        "MET-07",
        "WARN",
        "veto",
        "T-148",
        "D/E, net debt/EBITDA, EV",
        0,
    ),
    (
        "Financial firms: metrics the matrix marks NA are computed",
        "APP02.na_metrics_computed",
        "APP-02",
        "BLOCK",
        "score",
        "T-071",
        "ROA, D/E, ROIC, FCF metrics in VALORIZATION",
        0,
    ),
    (
        "Financial firms hard-vetoed (14 of 51)",
        "N8.financial_firms_hard_vetoed",
        "APP-02",
        "BLOCK",
        "veto",
        "T-071",
        "company exclusion",
        0,
    ),
    (
        "Article 9 filers vetoed because their revenue does not resolve",
        "F5.article9_vetoed_by_missing_revenue",
        "APP-01",
        "BLOCK",
        "veto",
        "T-148",
        "DQ_REVENUE_POS → DATA_QUALITY veto",
        0,
    ),
    (
        "Utilities hard-vetoed by NEGATIVE_FCF (the largest sector cluster)",
        "F4.utilities_hard_vetoed",
        "Annex A",
        "WARN",
        "veto",
        "T-070 / T-071",
        "company exclusion",
        0,
    ),
    (
        "Negative equity: ROE / D/E computed (quarantined downstream)",
        "N15.roe_on_negative_equity",
        "APP-07",
        "BLOCK",
        "veto",
        "T-071 / T-151",
        "distress rules, ranking",
        0,
    ),
    (
        "An amended 10-K replaces or loses the original 10-K",
        "N1.missing_cause.amendment",
        "PER-02",
        "BLOCK",
        "score",
        "T-149",
        "every metric of the lost year",
        0,
    ),
    (
        "earnings_yield is never computed (the value factor)",
        "N2.valorization_value_factor_null",
        "MET-05",
        "BLOCK",
        "score",
        "T-149",
        "VALORIZATION",
        20,
    ),
    (
        "Stored valuation built on the diluted average share count (metrics-v2)",
        "MKT04.diluted_average_fallback",
        "MKT-04",
        "BLOCK",
        "score",
        "T-148",
        "market cap → every yield",
        20,
    ),
    (
        "Winsorizing at 2% instead of 0.5% (universe-wide cross-section)",
        "DQ02.winsor_2pct_vs_half_pct_universe",
        "DQ-02",
        "WARN",
        "score",
        "T-071",
        "every normalized score",
        495,
    ),
    (
        "T-140 derives revenue that fails the ex-post audit (ICE)",
        "L09.expost_fails.ICE",
        "ID-02b",
        "BLOCK",
        "score",
        "T-148",
        "every revenue-based metric of ICE",
        1,
    ),
    (
        "REIT capex leaves out real-estate acquisitions",
        "APP04b.reit_acquisitions_left_out",
        "APP-04b",
        "WARN",
        "score",
        "T-148",
        "FCF metrics of REITs",
        0,
    ),
    (
        "REIT NOPAT on the 21% default rate",
        "APP04c.reit_tax_rate_21pct",
        "APP-04c",
        "BLOCK",
        "score",
        "T-151",
        "NOPAT, ROIC",
        0,
    ),
    (
        "Net income identity (ProfitLoss = NetIncomeLoss + NCI) fails",
        "src.CON-19.block",
        "CON-19",
        "DQ-01",
        "score",
        "T-150",
        "parent earnings",
        0,
    ),
    (
        "Assets ≠ current + noncurrent assets",
        "src.CON-02.block",
        "CON-02",
        "DQ-01",
        "score",
        "T-150",
        "current/quick ratios",
        0,
    ),
    (
        "Lenders' net-income CAGR across the CECL adoption",
        "PER14.net_income_cagr_crosses_cecl",
        "PER-14",
        "WARN",
        "score",
        "T-151",
        "net_income_cagr",
        0,
    ),
    (
        "52/53-week filers' 10-Ks lost to a repeated FY label (fixed in code, absent from this database)",
        "N1.missing_cause.label_collision",
        "PER-02",
        "BLOCK",
        "score",
        "rebuild (T-143)",
        "the lost fiscal years",
        0,
    ),
    (
        "A prior fiscal-year column that is not a year earlier",
        "PER08.fy_prior_not_a_year",
        "PER-08",
        "WARN",
        "score",
        "T-149",
        "growth, CAGR",
        0,
    ),
    (
        "Share-count scale corrector rescales a filed value",
        "ID18.scale_corrector_applied",
        "ID-18",
        "BLOCK",
        "score",
        "T-148",
        "market cap (MCD)",
        1,
    ),
    (
        "Negative value of a DQC_0015-listed element",
        "src.ID-13.negative_listed_element",
        "ID-13",
        "BLOCK",
        "none",
        "T-148",
        "nothing the resolver reads (1 filing)",
        0,
    ),
    (
        "Stored period end differs from the EDGAR header",
        "ID07.period_end_differs_from_header",
        "ID-07",
        "BLOCK",
        "score",
        "T-149",
        "the period of one filing (EXE)",
        0,
    ),
    (
        "Companies with a succession 8-K (8-K12B / 12G3 / 15D5)",
        "APP10b.succession_8ks",
        "APP-10b-1",
        "WARN",
        "none",
        "T-149",
        "series continuity",
        0,
    ),
]


def ranking(res: Results) -> list[str]:
    rows = []
    for what, key, rule, sev, reach, task, feeds, override in HEADLINES:
        f = res.get(key)
        own = f.target_companies if f.target_companies >= 0 else f.companies
        companies = override or own or (f.count if f.unit in ("companies", "assets") else 0)
        score = SEV_WEIGHT[sev] * max(companies, 1) * REACH_WEIGHT[reach]
        rows.append((score, what, f, rule, sev, reach, task, feeds, companies))
    rows.sort(key=lambda r: -r[0])
    lines = [
        "| # | Score | What | Rule | Sev. | Companies | Reaches | Feeds | Evidence | Task |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, (score, what, f, rule, sev, reach, task, feeds, companies) in enumerate(rows, 1):
        lines.append(
            row(
                i, score, what, rule, sev, n(companies), REACH_TEXT[reach], feeds,
                f"{fmt(f)} ({f.level}, {f.db})", task,
            )
        )  # fmt: skip
    return lines


def defect_tables(res: Results) -> list[str]:
    lines = [
        "### Open defects",
        "",
        "| ID | Defect | Rules | Evidence | What the measurement says | Task |",
        "|---|---|---|---|---|---|",
    ]
    for did, text, rules, keys, verdict, task in defects.OPEN:
        ev = "; ".join(fmt(res.get(k)) for k in keys) or "—"
        lines.append(row(did, text, rules, ev, verdict, task))
    lines += [
        "",
        "### Defects fixed so far (`docs/model_fixes.md`) and the rules they support",
        "",
        "| Fix | Rules | What it fixed |",
        "|---|---|---|",
    ]
    lines += [row(a, b, c) for a, b, c in defects.FIXED]
    lines += [
        "",
        "### Fixed defects that no checklist rule covers",
        "",
        "| Fix | Why no rule |",
        "|---|---|",
    ]
    lines += [row(a, b) for a, b in defects.UNCOVERED]
    return lines


def supplement_tables(res: Results) -> list[str]:
    t = res.tables
    lines = ["### Vetoes replayed per cycle date (Annex A: companies excluded, by sector)", ""]
    lines += [
        "Annual basis: each company's latest 10-K available on the date, today's `quality.evaluate` and `RULES`; `DQ_MCAP_SCALE`, `DQ_FCF_YIELD`, `PRICE_CRASH` and `EARNINGS_MISSING` are not replayed, so these are lower bounds.",
        "",
        "| Date | Evaluated | Hard-vetoed | Financial firms evaluated | ...hard-vetoed | ...by rule | Utilities hard-vetoed |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in t["veto_replay"]:
        lines.append(
            f"| {r['date']} | {r['evaluated']} | {r['hard_assets']} | {r['financial_evaluated']} | {r['financial_hard']} | {r['financial_by_rule']} | {r['hard_by_sector'].get('Utilities', 0)} |"
        )
    lines += [
        "",
        "### Negative-equity companies and T-116's distress conditions (D-04), latest 10-K on 2026-09-22",
        "",
        "| Ticker | Equity (USD bn) | Net debt/EBITDA | Interest coverage | c1: ND/EBITDA > 5 | c2: EBITDA <= 0 with net debt | c3: coverage < 1.5 | Flagged with c2 | Flagged without c2 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in t["negative_equity"]:
        nd = "—" if r["net_debt_to_ebitda"] is None else f"{r['net_debt_to_ebitda']:.2f}"
        ic = "—" if r["interest_coverage"] is None else f"{r['interest_coverage']:.2f}"
        c = lambda v: "—" if v is None else ("yes" if v else "no")  # noqa: E731
        lines.append(
            f"| {r['ticker']} | {r['equity'] / 1e9:,.1f} | {nd} | {ic} | {c(r['c1_ndte_above_5'])} | {c(r['c2_ebitda_nonpositive_with_net_debt'])} | {c(r['c3_coverage_below_1_5'])} | {'yes' if r['flagged_with_c2'] else 'no'} | {'yes' if r['flagged_without_c2'] else 'no'} |"
        )
    lines += [
        "",
        "### D-07: the replayed book's sector weights with and without the financial firms (pilot replay, scratch copies)",
        "",
    ]
    base, excl = res.d07["with"], res.d07["without"]
    lines += [
        f"Excluded: {', '.join(res.d07['excluded'])} (the pilot's financial firms under APP-00; MA is a payment processor, an operating company). {res.d07['dates'][0]} weekly replay dates, top 10 names; the original databases were not opened for writing.",
        "",
        f"**This table is the pilot's {len(res.d07['excluded'])} financial firms among its 20 names, so it is not representative of the S&P 500 book.** Production has stored metrics for only {res.get('N16.fundamental_fallback_scores').total} filings, so a full-universe replay is not possible before `T-100`. The universe-level inputs for the decision are the market-capitalization share ({res.get('D07.financial_firms_cap_share').count / 100:.1f}%) and the veto counts ({res.get('N8.financial_firms_hard_vetoed').count} of {res.get('N8.financial_firms_hard_vetoed').total}).",
        "",
        "| Sector | With | Without |",
        "|---|---|---|",
    ]
    lines += [
        f"| {s} | {base.get(s, 0) * 100:.1f}% | {excl.get(s, 0) * 100:.1f}% |"
        for s in sorted(set(base) | set(excl))
    ]
    lines += [
        "",
        "### MET-08 (a): mineral-interest purchases beside exploration & development, by producer and year",
        "",
        "DOW and KKR appear here although neither is a producer: each files a small `PaymentsToExploreAndDevelopOilAndGasProperties` line (DOW: 'Investment in gas field developments'; KKR: zero), which the resolver's oil & gas tier reads as capex. That is the symptom of N22, not a producer's recurring spend.",
        "",
        "| Ticker | FY end | Exploration & development (USD bn) | O&G equipment | Mineral-interest purchases | Business acquisitions | Mineral / development | Resolved capex | Resolved concept |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in t["og_capex_years"]:
        cap = "—" if r["resolved_capex"] is None else f"{r['resolved_capex'] / 1e9:.2f}"
        ratio = "—" if r["mineral_to_development"] is None else f"{r['mineral_to_development']:.2f}"
        lines.append(
            row(
                r["ticker"],
                r["fiscal_year_end"],
                f"{r['exploration_development'] / 1e9:.2f}",
                f"{r['oil_gas_equipment'] / 1e9:.2f}",
                f"{r['mineral_interest_purchases'] / 1e9:.2f}",
                f"{r['business_acquisitions'] / 1e9:.2f}",
                ratio,
                cap,
                r["resolved_concept"],
            )
        )
    return lines


def fidelity_text(res: Results) -> list[str]:
    p, q = res.fidelity["production"], res.fidelity["pilot"]

    def drift(d: dict[str, int]) -> str:
        return ", ".join(f"{k.removeprefix('DRIFT:')}: {v}" for k, v in sorted(d.items())) or "none"

    return [
        "Level (b) re-runs today's `statements.py` and metric groups over the stored `financial_facts`. It is only used where the re-run is shown faithful to what the pipeline stored. **No database holds `metrics-v5`** (today's engine): production is `"
        + "`, `".join(p["engine_versions"])
        + "` and the pilot `"
        + "`, `".join(q["engine_versions"])
        + "`. The check therefore rebuilds `Statements` from the stored rows in insertion order, runs the same `compute` functions the pipeline runs (without TTM) and compares each result's inputs with the stored `inputs_json`.",
        "",
        "| Database | Filings with stored metrics | Raw match rate | After the documented drift classes | Unexplained pairs |",
        "|---|---|---|---|---|",
        f"| production (`metrics-v2`) | {p['filings_compared']} of {p['filings_compared'] + p['filings_without_metrics']} | {p['raw_match_rate']:.2%} | {p['classified_rate_strict']:.2%} | {p['unexplained']} |",
        f"| pilot (`metrics-v4`) | {q['filings_compared']} of {q['filings_compared'] + q['filings_without_metrics']} | {q['raw_match_rate']:.2%} | {q['classified_rate_strict']:.2%} | {q['unexplained']} |",
        "",
        f"The raw rate is above 99% on both, so the brief's stop condition is not met. Every non-matching pair is assigned by a **structural detector tied to one documented engine fix newer than the stored rows** (`db.py`'s version history), never by \"any mismatch\": production {drift(p['drift'])}; pilot {drift(q['drift'])}. T-128 (a revenue total that duplicates a dimensional slice) cannot be replayed from storage, which keeps no dimensional rows: it is the one known class and is named as such. `TTM_PATH` inputs (`*_ttm`, `annualized_x4`) need a filing's earlier quarters and are out of the rate ({n(q['ttm_path_pairs'])} pairs on the pilot).",
        "",
        f"**Population limit.** Production stores metrics for only {p['filings_compared']} of its {p['filings_compared'] + p['filings_without_metrics']} filings (the 20-asset runs); the other {n(p['filings_without_metrics'])} have facts but no `inputs_json` to compare. The replay of those rests on the same table and the same code path.",
    ]


def render(res: Results) -> str:
    g = res.get
    types = collections.Counter(r["type"] for r in res.types)
    lines = [
        "# SEC data-treatment checklist — audit of the code against v1.2.2 (T-147)",
        "",
        "Generated by `scripts/audit_sec_checklist.py report` from `docs/checklist_sec/prevalence_results.json`; do not edit by hand. "
        "Nothing under `src/` changed: a bug found here is in the fix plan (`.specify/memory/TASKS.md`), not in code.",
        "",
        "**Inputs.** The frozen checklist `checklist_v1.2.2.md` (78 rules, section L, Annex A), `code_comparison_v1.md` (Phase 1, the reviewer's static reading) and `phase3_decisions.md` (decisions D-01 to D-12, binding). Where this audit's reading differs from Phase 1, both are kept (section 8).",
        "",
        "## 1. Result",
        "",
        *status_summary(),
        "",
        "The rule statuses are Phase 1's, re-checked against the code and the data. A Phase 1 ✓ with no test in this repo is `partial (untested)` (the brief's rule): ID-10, ID-17, PER-03, PER-09, APP-09. The ✓ rows that remain cite the code and the test that pins them.",
        "",
        "What the measurements add, in the order they matter (the full ranked table is section 5):",
        "",
        f"- **The filed sign (D1) is far larger than the pilot suggested.** The displayed income tax is the negative of the filed value in {n(g('src.income_tax.FLIPPED').filings)} production filings ({g('src.income_tax.FLIPPED').companies} companies); interest expense in {n(g('src.interest_expense.FLIPPED').filings)}; cost of goods in {n(g('src.cogs.FLIPPED').filings)}. Capex is displayed negative in {n(g('src.capital_expenditure.FLIPPED').filings)} filings and `abs()` is what makes it right today.",
        f"- **N1 is real.** {g('N1.stored_is_amendment').count} stored accessions are amendments and {g('N1.missing_cause.amendment').count} original 10-Ks of {g('N1.missing_cause.amendment').companies} companies are missing because a 10-K/A of the period exists.",
        f"- **N2 is real.** No stored metric is named `net_income` ({g('N2.metrics_named_net_income').count} of {g('N2.metrics_named_net_income').total}), so `earnings_yield` is never computed; the VALORIZATION value factor is empty in {g('N2.valorization_value_factor_null').count} of {g('N2.valorization_value_factor_null').total} stored rows.",
        f"- **T-140 derives revenue for five companies, not only APA, and one of them is wrong.** ICE: {g('L09.expost_fails.ICE').count} derived values, every checkable one fails the ex-post audit.",
        f"- **Financial firms are excluded by accident.** {g('N8.financial_firms_hard_vetoed').count} of {g('N8.financial_firms_hard_vetoed').total} are hard-vetoed on 2026-09-22, and {g('F5.article9_vetoed_by_missing_revenue').count} of the {g('F5.article9_vetoed_by_missing_revenue').total} Article 9 filers because their revenue does not resolve (a resolver defect, APP-01).",
        f"- **Utilities are the largest sector cluster of vetoes** ({g('F4.utilities_hard_vetoed').count} of {g('F4.utilities_hard_vetoed').total}, almost all `NEGATIVE_FCF`).",
        f"- **The 10-Ks lost to a repeated period label are explained** ({g('N1.missing_cause.label_collision').count} companies, 52/53-week calendars: T-140's fix, absent from this older database).",
        "",
        "## 2. Method and evidence levels",
        "",
        "Two evidence levels, kept apart in every table. **(a) the SEC source**: `companyfacts` and `submissions` for the 500 CIKs of production's `assets` plus the predecessor CIK of XOM, filtered to the accessions of our filings; `companyfacts` carries only non-dimensional, standard-taxonomy facts, so a rule that depends on a dimensional or extension fact is marked **source prevalence incomplete** and never counted as zero. **(b) our resolver**: today's `statements.py` and metric groups re-run over production's stored `financial_facts` (read-only), the pilot database, and the stored `fundamental_metrics` rows themselves.",
        "",
        f"**Population.** Production: 503 assets, {n(res.tables['recs']['target'])} filings with a column of their own, and **{n(res.tables['recs']['columns'])} filing-columns**: for each filing the resolver measures read, besides the target period, every column the pipeline reads (the prior; a 10-K's earliest fiscal year, the CAGR base; a 10-Q's current and prior-year year-to-date columns, the TTM identity). A finding on filing-columns prints both counts (\"own period n/N\" is the target column alone). Metric, period, identity and cycle rules are about a filing's own period and use it only.",
        "",
        "**Engine versions are labelled on every stored-level measure.** Production is `metrics-v2`, which predates the T-105 annualization flags and T-132's cover counts: a 0 for `PER06.*` there means *not measurable*, and the `MKT04.*` numbers describe the old engine, not today's code.",
        "",
        "### Fidelity of the level (b) re-run",
        "",
        *fidelity_text(res),
        "",
        "**SEC access.** `SEC_USER_AGENT` when set, else `research@example.com` (the contact used for the real run); at most 10 requests per second; every response is cached under the gitignored `.cache/sec_xcheck/`. The official DQC element lists (ID-13 to ID-15) were registered in `sources_register.md` Block B with their commit and SHA-256 and are used only to measure prevalence.",
        "",
        "## 3. The 78 rules",
        "",
        "`Status`: ✓ enforced and pinned; partial; ✗ not enforced or contradicted; n/a; L declared limitation. `Test`: the test that fails if the rule breaks (⚠: a test that pins the **wrong** behaviour). Evidence: count / population, companies and up to five tickers; examples with accessions are in `prevalence_tables.json` and the findings file. *Italic* text is the audit's own reading where it adds to or differs from Phase 1.",
        "",
        *rule_table(res),
        "",
        "## 4. Declared limitations (section L) and calibrated screens (Annex A)",
        "",
        "| Item | Verdict | What the audit found | Evidence |",
        "|---|---|---|---|",
    ]
    for lid, (verdict, text, keys) in LIMITATIONS.items():
        lines.append(row(lid, verdict, text, "; ".join(fmt(g(k)) for k in keys) or "—"))
    lines += ["", "| Annex A | Project gate | State |", "|---|---|---|"]
    lines += [row(k, f"`{gate}`", state) for k, (gate, state) in ANNEX_A.items()]
    lines += [
        "",
        "Annex A asks for the per-cycle count of vetoed companies by sector: it is the first table of section 7. `DQ_NEG_EQUITY` and `DQ_MARGIN_REVIEW` and `TTM_CROSSCHECK` are project gates outside Annex A (Phase 1); with D-04 accepted, `DQ_NEG_EQUITY` becomes quarantine-only and needs no A-06.",
        "",
        "## 5. Prevalence, ranked",
        "",
        "Rank = severity (BLOCK 3, WARN/DQ-01 2, INFO 1) x companies affected x reach (a veto 3, a score 2, neither 1). It orders the work; it is not a measure of harm. The evidence column names the level, (a) the source or (b) the resolver, and the database.",
        "",
        *ranking(res),
        "",
        "**Escalated by hand, whatever the rank.** A product underweights a defect that is wrong in few companies but wrong in the data, or that changes results already produced: **N20 (ICE: 17 derived revenue values that fail the ex-post audit, high)**, **N1** (amendments) and **N2** (`earnings_yield`, which every real cycle has ranked without). The Companies column counts companies with the problem on a filing's own period.",
        "",
        "## 6. Defect map",
        "",
        "D1-D10 and N1-N18 map to rule IDs; N19, N20, N21 and N22 are this audit's own.",
        "",
        *defect_tables(res),
        "",
        "## 7. Supporting tables",
        "",
        *supplement_tables(res),
        "",
        "## 8. Where this audit disagrees with Phase 1 or with a decision (nothing dropped)",
        "",
        f"1. **N8 (financial firms and LEVERAGE_EXTREME).** Phase 1 said LEVERAGE_EXTREME vetoes banks at D/E > 3. *Measured:* it fires on none of the {g('N8.financial_firms_hard_vetoed').total} financial firms, because debt/equity counts debt lines only (no deposits). They are vetoed instead by `DQ_REVENUE_POS` ({g('F5.article9_vetoed_by_missing_revenue').count} Article 9 filers whose revenue does not resolve), `NEGATIVE_FCF` (C, GS, MS) and two margin gates (KEY). The conclusion (they are excluded on rules that do not apply to them) stands; the mechanism differs.",
        "2. **D-03's premise (\"a value that fails its guard is already left empty\").** *Measured:* a refused T-140 rebuild falls through to the component sum in 19 of 52 refused columns (ADP, ICE), and to nothing for BNY (33). For ADP the sum is the true revenue. The reviewer resolved this as D-03b: the sum stands only under D-02's condition, and a derived value whose ex-post audit fails is left empty. Nothing was applied here.",
        "3. **ID-18.** Phase 1: BUG (the code corrects where the rule says quarantine) and an open question whether A-03 catches the MCD case. *Measured:* it does, for all 6 corrected filings; the BUG classification stands.",
        f"4. **MET-07 count.** The checklist reports 1,329 of 5,075 production filings where the combined debt-and-lease element is the only long-term debt line. *Reproduced:* {n(g('MET07.combined_element_is_only_long_term_debt_line').count)}, 3.5% higher; the pilot's 113 reproduces exactly, and so do ID-19 (147), CON-10 (212) and the L-01 counts. The reviewer's exact query is not in the repo.",
        "5. **Phase 1 ✓ without a test** (ID-10, ID-17, PER-03, PER-09, APP-09): `partial (untested)` by the brief's rule; the tests belong to the tasks that touch those modules.",
        f'6. **PER-12 to PER-14.** Phase 1: "NOT-EXEC in the window?" *Measured:* executable and small: {g("PER13.cagr_crosses_asc842").count} CAGRs cross ASC 842 (non-calendar filers only) and {g("PER14.net_income_cagr_crosses_cecl").count} lenders\' net-income CAGRs cross CECL.',
        f"7. **APP-01.** Phase 1: COGS and the liquidity ratios are not marked NA for banks. *Measured:* they are empty anyway (0 of {g('APP01.liquidity_ratios_computed').total}); the real defect is the missing bank revenue.",
        "8. **N12.** Four dead names, confirmed, with no effect in the window: no fact of ours uses them and the last historical use was filed 2021-11-22. The checklist's ID-20 note counts five names in its log; the registry holds four of them.",
        "9. **N3 is wider than Phase 1 measured.** Besides the target columns, the year-to-date pair the TTM reads carries the continuing-operations line in many 10-Qs (F2): see the own-period and all-column counts.",
        "10. **L-04 and L-05 are contradicted by the code** (amendments are ingested; the effective tax rate shows 21% when pre-tax income is <= 0).",
        "11. **`model_fixes.md` describes T-140's rebuild as APA's.** It fires for APA, PSX, NI, MPC and ICE.",
        "12. **PER-10/PER-11 survivorship is structural for the stored data.** Production's universe is one day's snapshot (503 rows, all valid from 2026-08-30, none with an end) and `universe.db` holds 20 members, so no leaver can be counted: the rule is ✓ in the code and unmeasurable in the data.",
        "13. **The O&G figures** of MET-08's note (FANG: 1.68B and 2.01B beside 1.94B and 2.70B for 2022 and 2023) are within 6% of the table in section 7, which shows each year's own 10-K; the checklist's figures were taken from a different read of the same filings.",
        "14. **The brief's `metrics-v5` comparison** cannot be literal: see the fidelity method in section 2.",
        "",
        "## 9. Inputs for checklist v1.3 (the checklist itself is not edited; the reviewer writes v1.3)",
        "",
        f"1. **Scope, three windows (D-10).** Filings stored: 2022-01-06 to 2026-09-02. Fiscal-year columns reach back to FY2019 (the CAGR base of the first 10-Ks): {g('PER13.cagr_crosses_asc842').count} CAGRs cross ASC 842 and {g('PER14.net_income_cagr_crosses_cecl').count} lenders' CAGRs cross CECL. Taxonomy releases 2020-2026 (the DQC lists are registered for the same window).",
        f"2. **ID-02b / MET-07 partial debt map (D-01).** Own period of the {n(g('N7.lt_debt_only').target_total)} stored filings: {n(g('N7.lt_debt_only').target_count)} have a long-term debt line and no short-term line, {n(g('N7.st_debt_only').target_count)} the reverse, {n(g('N7.debt_absent').target_count)} no debt line at all. **ID-02b / CON-11 sourced revenue map (D-02):** {n(g('D02.revenue_component_sum').target_count)} filings' revenue is a sum of components with no filed total ({g('D02.revenue_component_sum').companies} companies; lessors and ASC 606 filers beside lease income).",
        f"3. **L-09 (D-03, D-03b).** {g('L09.derived_by_company').count} companies, {n(g('L09.derived_values').count)} distinct derived values, {g('L09.refused_values').count} refused; the ex-post audit passes for APA, PSX, NI and MPC and fails for ICE (rebuild anchored on a net-revenue line). The register is `l09_verification.csv`, with `verified_by` and `verified_on` empty for the hand check.",
        f"4. **PER-06 (D-09).** On the pilot (`metrics-v4`) {n(g('PER06.metrics_built_on_x4', 'pilot').count)} of {n(g('PER06.metrics_built_on_x4', 'pilot').total)} 10-Q metric rows are built on x4; production's engine predates the flags.",
        "5. **Metric dictionary annex (D-05).** Three formulas stay `source pending`: the quick ratio, the cash ratio and the CFO-based FCF to the firm.",
        f"6. **Annex A / D-04.** {g('D04.negative_equity_companies').count} companies have negative equity (latest 10-Ks, 2026-09-22); T-116's conditions flag {g('D04.flagged_with_condition_2').count}, and the same {g('D04.flagged_without_condition_2').count} without the EBITDA <= 0 condition. The per-cycle veto count by sector is in section 7: utilities are the largest cluster ({g('F4.utilities_hard_vetoed').count} of {g('F4.utilities_hard_vetoed').total}).",
        f"7. **D-07.** Financial firms (APP-00: {types['article_9']} Article 9, {types['article_7']} Article 7, {types['other_financial']} other) are about {g('D07.financial_firms_cap_share').count / 100:.1f}% of the approximate market capitalization ({g('D07.cap_coverage').count} of {g('D07.cap_coverage').total} CIKs covered; total, not float-adjusted). {g('N8.financial_firms_hard_vetoed').count} of {g('N8.financial_firms_hard_vetoed').total} are hard-vetoed. The decision rests on those two universe-level figures: the market-capitalization share and the veto counts. The pilot replay (20 names, {len(res.d07['excluded'])} of them financial firms) is not representative of the universe and is shown only as an illustration: the Financials sector averages {res.d07['with'].get('Financials', 0) * 100:.1f}% of its book, {res.d07['without'].get('Financials', 0) * 100:.1f}% without them. By the decision rule in `phase3_decisions.md` §7: the vetoes do not exclude almost all of them, so excluding them is a choice, not a consequence.",
        "8. **Rule text where a prevalence statement changes.** ID-20 (the dead names cost nothing in the window); L-04 and L-05 (contradicted); MET-07's 1,329 (reproduced as 1,375); PER-12 to PER-14 (executable, small); APP-01 (the defect is missing revenue); PER-11 (unmeasurable); ID-13 to ID-15 (lists registered; measured).",
        "",
        "## 10. Files",
        "",
        "| File | What it is |",
        "|---|---|",
        "| `docs/checklist_sec/prevalence_results.json` | every measurement, both levels, with examples (ticker, accession) |",
        "| `docs/checklist_sec/prevalence_tables.json` | the supplement tables of section 7 |",
        "| `docs/checklist_sec/company_types.csv` | the APP-00 table: one row per CIK, evidence and overlays |",
        "| `docs/checklist_sec/l09_verification.csv` | the L-09 register, for the hand check |",
        "| `docs/checklist_sec/golden_set.csv`, `docs/sec_golden_set.md` | the golden set: semantic choices |",
        "| `docs/checklist_sec/fidelity_results.json`, `d07_replay.json` | the fidelity and D-07 replay results |",
        '| `docs/checklist_sec/baseline_manifest.md` | the D-11 "before" export (files outside the repo) |',
        "| `scripts/audit_sec_checklist.py`, `scripts/sec_xcheck/` | the measurements; tests in `tests/test_sec_xcheck_*.py` |",
        "",
    ]
    return "\n".join(lines)


def write(path: Path | None = None) -> Path:
    out = path or REPO / "docs" / "sec_data_checklist.md"
    out.write_text(render(Results()), encoding="utf-8")
    return out
