"""Cycle-level measurements on the whole universe: the data-quality gates and veto rules, replayed in memory.

The production cycles ran over 20 assets, so the S&P 500 effect of the vetoes cannot be read from ``veto``. Instead, for
a cycle date D, each asset's latest **10-K** available on D (``available_at <= D``) is taken, its metrics replayed
(:func:`sec_xcheck.features.replay_metrics`, annual basis: a 10-Q's TTM flows are not replayable from one filing),
and today's own ``fundamental_agent.quality.evaluate`` and ``cycle.rules.builtin.RULES`` are run over them exactly as
the orchestrator does (quarantined metrics read as NULL; HARD gates feed ``DATA_QUALITY``). Nothing is written.

Not replayed: ``DQ_MCAP_SCALE`` and ``DQ_FCF_YIELD`` (they need market capitalisation), ``PRICE_CRASH`` (prices) and
``EARNINGS_MISSING`` (the FUNDAMENTAL score dates). The result is therefore a lower bound on the vetoes of a real cycle.
"""

from __future__ import annotations

import collections
import datetime as dt
from typing import Any

from cycle.rules.base import RuleContext
from cycle.rules.builtin import RULES
from fundamental_agent import quality
from sec_xcheck.findings import Finding, spread
from sec_xcheck.records import Rec

CMD = "uv run python scripts/audit_sec_checklist.py measure --only cycle"
REPLAYED_RULES = {"LEVERAGE_EXTREME", "NEGATIVE_FCF", "LIQUIDITY_DISTRESS", "DATA_QUALITY"}
NA_FOR_FINANCIALS = {
    "NEGATIVE_FCF",
    "LEVERAGE_EXTREME",
}  # they read FCF margin / debt to equity (APP-02, APP-02b)


def quarter_ends(first: str, last: str) -> list[str]:
    """Calendar quarter-end dates (Mar 31, Jun 30, Sep 30, Dec 31) from *first* to *last* inclusive (ISO)."""
    out: list[str] = []
    for year in range(int(first[:4]), int(last[:4]) + 1):
        for month in (3, 6, 9, 12):
            end = (
                dt.date(year + month // 12, month % 12 + 1, 1) - dt.timedelta(days=1)
            ).isoformat()
            if first <= end <= last:
                out.append(end)
    return out


def latest_annual(recs: list[Rec], as_of: str) -> dict[str, Rec]:
    """Each CIK's newest 10-K whose ``available_at`` is on or before *as_of*."""
    best: dict[str, Rec] = {}
    for r in recs:
        if r.form != "10-K" or not r.available_at or r.available_at > as_of:
            continue
        if r.cik not in best or r.period_end > best[r.cik].period_end:
            best[r.cik] = r
    return best


def filing_metrics(r: Rec) -> quality.FilingMetrics:
    return {
        (m["group"], name): quality.StoredMetric(m["value"], m["inputs"])
        for name, m in r.f["metrics"].items()
    }


def replay_cycle(annual: dict[str, Rec], as_of: str) -> dict[str, Any]:
    """The orchestrator's quarantine + veto step over *annual* (cik -> Rec): ``{cik: {...}}`` and the hits."""
    metrics: dict[int, dict[str, float | None]] = {}
    hard: dict[int, list[dict[str, Any]]] = {}
    negative_equity: set[int] = set()
    ids = {cik: i for i, cik in enumerate(sorted(annual))}
    by_id = {i: cik for cik, i in ids.items()}
    for cik, r in annual.items():
        aid = ids[cik]
        fm = filing_metrics(r)
        issues = quality.evaluate(fm)
        quarantined = {f"{i.metric[0]}.{i.metric[1]}" for i in issues if i.quarantined}
        metrics[aid] = {
            f"{g}.{n}": (None if f"{g}.{n}" in quarantined else s.value) for (g, n), s in fm.items()
        }
        for i in issues:
            if i.severity == "HARD":
                hard.setdefault(aid, []).append(
                    {
                        "rule_id": i.rule_id,
                        "metric": f"{i.metric[0]}.{i.metric[1]}",
                        "value": i.value,
                    }
                )
            if i.rule_id == "DQ_NEG_EQUITY":
                negative_equity.add(aid)
    ctx = RuleContext(
        cycle_date=as_of, metrics=metrics, price_obs={}, last_fundamental={}, data_quality=hard
    )
    hits: list[tuple[str, str, str, dict[str, Any]]] = []
    for rule in RULES:
        if rule.RULE_ID not in REPLAYED_RULES:
            continue
        for h in rule.evaluate(ctx):
            hits.append((by_id[h.asset_id], h.rule_id, h.severity, h.evidence))
    return {
        "hits": hits,
        "negative_equity": {by_id[a] for a in negative_equity},
        "evaluated": len(annual),
    }


def distress_conditions(r: Rec) -> dict[str, bool | None]:
    """T-116's three distress conditions for a negative-equity filing, with the gate's own formulas
    (``quality._neg_equity``): ``c1`` net debt/EBITDA > 5, ``c2`` EBITDA <= 0 with positive net debt, ``c3`` coverage < 1.5."""
    nd = r.f["metrics"].get("net_debt_to_ebitda", {"value": None, "inputs": {}})
    i = nd["inputs"]
    ebitda = None
    if i.get("operating_income") is not None and i.get("depreciation_amortization") is not None:
        ebitda = i["operating_income"] + i["depreciation_amortization"]
    net_debt = (
        None
        if i.get("total_debt") is None or i.get("cash") is None
        else i["total_debt"] - i["cash"]
    )
    cov = r.f["metrics"].get("interest_coverage", {}).get("value")
    value = nd["value"]
    return {
        "c1_ndte_above_5": None if value is None else value > quality.NEG_EQUITY_NET_DEBT_TO_EBITDA,
        "c2_ebitda_nonpositive_with_net_debt": None
        if ebitda is None or net_debt is None
        else (ebitda <= 0 and net_debt > 0),
        "c3_coverage_below_1_5": None
        if cov is None
        else cov < quality.NEG_EQUITY_INTEREST_COVERAGE,
    }


def negative_equity_table(annual: dict[str, Rec]) -> list[dict[str, Any]]:
    """One row per negative-equity company: its equity, the ratios the conditions read, and each condition."""
    rows = []
    for r in sorted(annual.values(), key=lambda x: x.ticker):
        eq = r.value("equity")
        if eq is None or eq >= 0:
            continue
        c = distress_conditions(r)
        nd = r.f["metrics"].get("net_debt_to_ebitda", {"value": None})["value"]
        rows.append(
            {"ticker": r.ticker, "accession": r.accession, "equity": eq, "net_debt_to_ebitda": nd,
             "interest_coverage": r.f["metrics"].get("interest_coverage", {}).get("value"), **c,
             "flagged_with_c2": any(v for v in c.values()),
             "flagged_without_c2": bool(c["c1_ndte_above_5"]) or bool(c["c3_coverage_below_1_5"])}
        )  # fmt: skip
    return rows


def m_d04(annual: dict[str, Rec], as_of: str) -> list[Finding]:
    table = negative_equity_table(annual)
    n = len(annual)
    ex = [(t["ticker"], t["accession"], f"equity {t['equity'] / 1e9:,.1f}B") for t in table]
    with_c2 = [t for t in table if t["flagged_with_c2"]]
    without = [t for t in table if t["flagged_without_c2"]]
    only_c2 = [t for t in with_c2 if not t["flagged_without_c2"]]
    return [
        Finding("D04.negative_equity_companies", ["APP-07"], f"Companies with negative book equity in their latest 10-K on {as_of}", "b", len(table), n, len(table), unit="companies", examples=spread(ex), command=CMD),
        Finding("D04.flagged_with_condition_2", ["APP-07"], "...flagged by T-116's three conditions (net debt/EBITDA > 5, EBITDA <= 0 with positive net debt, coverage < 1.5)", "b", len(with_c2), len(table), len(with_c2), unit="companies", examples=spread([(t["ticker"], t["accession"], "") for t in with_c2]), command=CMD),
        Finding("D04.flagged_without_condition_2", ["APP-07"], "...flagged without the EBITDA <= 0 condition", "b", len(without), len(table), len(without), unit="companies", examples=spread([(t["ticker"], t["accession"], "") for t in without]), command=CMD),
        Finding("D04.flagged_only_by_condition_2", ["APP-07"], "...flagged only because of the EBITDA <= 0 condition", "b", len(only_c2), len(table), len(only_c2), unit="companies", examples=spread([(t["ticker"], t["accession"], "") for t in only_c2]), command=CMD),
    ]  # fmt: skip


def vetoes(annual: dict[str, Rec], as_of: str, financial: set[str]) -> dict[str, Any]:
    out = replay_cycle(annual, as_of)
    by_rule: collections.Counter[tuple[str, str]] = collections.Counter()
    by_sector: collections.Counter[tuple[str, str]] = collections.Counter()
    fin_rule: collections.Counter[str] = collections.Counter()
    fin_assets: set[str] = set()
    hard_assets: set[str] = set()
    for cik, rule, sev, _ev in out["hits"]:
        by_rule[(rule, sev)] += 1
        by_sector[(annual[cik].sector, sev)] += 1
        if sev == "HARD":
            hard_assets.add(cik)
        if cik in financial and sev == "HARD":
            fin_rule[rule] += 1
            fin_assets.add(cik)
    return {
        **out,
        "by_rule": by_rule,
        "by_sector": by_sector,
        "fin_rule": fin_rule,
        "fin_assets": fin_assets,
        "hard_assets": hard_assets,
    }


def veto_findings(
    annual: dict[str, Rec], as_of: str, types: dict[str, dict[str, str]]
) -> list[Finding]:
    """Annex A's sector report and the D-07 inputs at one cycle date: who is HARD-vetoed, by which rule.

    * F4: the Utilities sector (capex-heavy: ``NEGATIVE_FCF`` reads the free-cash-flow margin) is the largest cluster;
    * N8 / D-07: financial firms (APP-00) hard-vetoed, by rule;
    * F5: Article 9 filers vetoed by ``DQ_REVENUE_POS`` because their revenue does not resolve -- a resolver defect for
      banks (APP-01), not a judgment on the banks."""
    financial = {c for c, t in types.items() if t["financial_firm"] == "True"}
    out = replay_cycle(annual, as_of)
    hard: dict[str, list[tuple[str, dict[str, Any]]]] = collections.defaultdict(list)
    for cik, rule, sev, evidence in out["hits"]:
        if sev == "HARD":
            hard[cik].append((rule, evidence))

    def gates(cik: str) -> set[str]:
        return {g for rule, ev in hard[cik] for g in (ev.get("gates") or [rule])}

    def ex(ciks: set[str]) -> list[tuple[str, str, str]]:
        return spread(
            [
                (annual[c].ticker, annual[c].accession, ",".join(sorted(gates(c))))
                for c in sorted(ciks)
            ]
        )

    utilities = {c for c, r in annual.items() if r.sector == "Utilities"}
    util_hard = {c for c in utilities if c in hard}
    fin_in = financial & set(annual)
    fin_hard = {c for c in fin_in if c in hard}
    art9 = {c for c in fin_in if types[c]["type"] == "article_9"}
    rev_pos = {c for c in art9 if "DQ_REVENUE_POS" in gates(c)}
    by_gate = collections.Counter(g for c in fin_hard for g in gates(c))
    note_date = f"annual basis on {as_of}: each company's latest 10-K; DQ_MCAP_SCALE, DQ_FCF_YIELD, PRICE_CRASH and EARNINGS_MISSING are not replayed"
    return [
        Finding("F4.utilities_hard_vetoed", ["Annex A"], "Utilities hard-vetoed (the largest sector cluster)", "b", len(util_hard), len(utilities), len(util_hard), unit="companies", examples=ex(util_hard), command=CMD, note=f"{note_date}; rules: {dict(collections.Counter(g for c in util_hard for g in gates(c)))}"),
        Finding("N8.financial_firms_hard_vetoed", ["APP-02", "APP-02b", "D-07"], "Financial firms (APP-00) hard-vetoed", "b", len(fin_hard), len(fin_in), len(fin_hard), unit="companies", examples=ex(fin_hard), command=CMD, note=f"{note_date}; by gate: {dict(by_gate)}; LEVERAGE_EXTREME fires on none (debt/equity reads debt lines only, not deposits)"),
        Finding("F5.article9_vetoed_by_missing_revenue", ["APP-01", "CON-11"], "Article 9 filers vetoed by DQ_REVENUE_POS: their revenue does not resolve", "b", len(rev_pos), len(art9), len(rev_pos), unit="companies", examples=ex(rev_pos), command=CMD, note=note_date),
    ]  # fmt: skip
