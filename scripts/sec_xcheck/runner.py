"""Run every measurement of the audit and collect the findings and the supplement tables.

``run_all`` is what ``audit_sec_checklist.py measure`` calls: it loads the stored filings once per database, reads the
company-type table (``company_types.csv``, built by ``audit_sec_checklist.py company-types``), and runs the groups
the caller asks for. Production is the population; the pilot database repeats the resolver-level (b) groups under its
own ``db`` label. Nothing is written to either database.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sec_xcheck import (
    capex_measures,
    cycle_measures,
    dqc_measures,
    extra_measures,
    l09,
    marketcap_measures,
    metric_measures,
    n1_amendments,
    period_measures,
    resolver_measures,
    source_measures,
    stored_measures,
)
from sec_xcheck.common import SecClient, open_ro, sources_dir
from sec_xcheck.findings import Finding
from sec_xcheck.records import Rec, load, targets

GROUPS = (
    "n1",
    "resolver",
    "capex",
    "metrics",
    "periods",
    "source",
    "dqc",
    "l09",
    "cycle",
    "marketcap",
    "stored",
    "extra",
)
REFERENCE_DATE = "2026-09-22"  # the production cycle date
UNTIL = "2026-09-02"  # the latest filing date stored


def read_types(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8") as fh:
        return {r["cik"]: r for r in csv.DictReader(fh)}


def label(findings: list[Finding], db: str) -> list[Finding]:
    for f in findings:
        f.db = db
    return findings


def quarter_table(recs: list[Rec], financial: set[str], dates: list[str]) -> list[dict[str, Any]]:
    """Per replay date: how many assets were evaluated, how many HARD-vetoed, and which rules hit financial firms."""
    rows = []
    for d in dates:
        annual = cycle_measures.latest_annual(recs, d)
        if len(annual) < 20:  # noqa: PLR2004 - too few annual filings available yet
            continue
        v = cycle_measures.vetoes(annual, d, financial)
        sector_hard: dict[str, int] = {}
        for (sector, sev), n in v["by_sector"].items():
            if sev == "HARD":
                sector_hard[sector] = n
        rows.append(
            {
                "date": d, "evaluated": v["evaluated"], "hard_assets": len(v["hard_assets"]),
                "financial_evaluated": len(financial & set(annual)), "financial_hard": len(v["fin_assets"]),
                "financial_by_rule": dict(v["fin_rule"]), "by_rule": {f"{r}/{s}": n for (r, s), n in v["by_rule"].items()},
                "hard_by_sector": sector_hard,
            }
        )  # fmt: skip
    return rows


def run_all(  # noqa: PLR0913, PLR0917 - the databases, the SEC cache, the type table and the groups wanted
    db: str | None,
    pilot_db: str | None,
    client: SecClient,
    types_csv: Path,
    only: set[str],
    tables: dict[str, Any],
) -> list[Finding]:
    findings: list[Finding] = []
    prod = open_ro(db)
    recs = load(prod)  # every column the pipeline reads (F2)
    dqc_dir = sources_dir() / "B_DQC_element_lists"
    own = targets(recs)  # the filing's own period: metric, period and cycle rules
    types = read_types(types_csv)
    type_of = {cik: r["type"] for cik, r in types.items()}
    financial = {cik for cik, r in types.items() if r["financial_firm"] == "True"}
    steps: dict[str, Callable[[], list[Finding]]] = {
        "n1": lambda: n1_amendments.measure(prod, client, UNTIL),
        "resolver": lambda: resolver_measures.all_measures(recs),
        "capex": lambda: capex_measures.m_capex(recs),
        "metrics": lambda: metric_measures.m_metrics(own),
        "periods": lambda: [*period_measures.m_periods(own), *period_measures.id07(prod, client, own), *period_measures.forms_among_ciks(prod, client)],
        "source": lambda: source_measures.measure(recs, client),
        "l09": lambda: l09.findings(l09.build_register(prod), len(own)),
        "dqc": lambda: dqc_measures.measure(recs, client, dqc_measures.load_lists(dqc_dir)),
        "stored": lambda: stored_measures.m_stored(prod, "production"),
        "extra": lambda: [
            *extra_measures.m_identities(own), *extra_measures.m_applicability(own, type_of),
            *extra_measures.m_reit_tax(own, type_of), *extra_measures.m_scores(prod, "production"),
            *extra_measures.m_winsor(prod, "production"), *extra_measures.full_universe_winsor(own, REFERENCE_DATE),
        ],
    }  # fmt: skip
    for name in GROUPS:
        if name in only and name in steps:
            findings += label(steps[name](), "production")
    if "marketcap" in only:
        caps = marketcap_measures.market_caps(prod, client, REFERENCE_DATE)
        tables["marketcap"] = {
            cik: {k: v for k, v in e.items() if k != "prices"} for cik, e in caps.items()
        }
        findings += label(
            marketcap_measures.share_by_type(caps, types, REFERENCE_DATE), "production"
        )
    if "capex" in only:
        tables["og_capex_years"] = capex_measures.og_year_table(own)
    if "cycle" in only:
        annual = cycle_measures.latest_annual(own, REFERENCE_DATE)
        findings += label(cycle_measures.m_d04(annual, REFERENCE_DATE), "production")
        findings += label(cycle_measures.veto_findings(annual, REFERENCE_DATE, types), "production")
        tables["negative_equity"] = cycle_measures.negative_equity_table(annual)
        dates = [*cycle_measures.quarter_ends("2022-03-31", "2026-06-30"), REFERENCE_DATE]
        tables["veto_replay"] = quarter_table(own, financial, dates)
    if pilot_db:
        pilot = open_ro(pilot_db)
        precs = load(pilot)
        pown = targets(precs)
        pilot_groups: dict[str, Callable[[], list[Finding]]] = {
            "resolver": lambda: resolver_measures.all_measures(precs),
            "capex": lambda: capex_measures.m_capex(precs),
            "metrics": lambda: metric_measures.m_metrics(pown),
            "stored": lambda: stored_measures.m_stored(pilot, "pilot"),
            "extra": lambda: [*extra_measures.m_identities(pown), *extra_measures.m_scores(pilot, "pilot"), *extra_measures.m_winsor(pilot, "pilot")],
        }  # fmt: skip
        for name, fn in pilot_groups.items():
            if name in only:
                findings += label(fn(), "pilot")
    tables["recs"] = {"target": len(own), "columns": len(recs)}
    return findings
