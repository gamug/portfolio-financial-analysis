#!/usr/bin/env python3
"""T-161: the TTM consistency scan (read-only).

The YTD-method TTM of every 10-Q flow is ``FY(prior 10-K) - YTD(prior) + YTD(this year)``. When a
filer tags the same flow differently on the two forms, that identity mixes two meanings. The scan
has two tests (the brief's §1):

* (i) ``r = (prior_ytd / prior_FY) / (m / 12)``, m the YTD's length in months, flagged outside
  [0.6, 1.5]. Applied when the prior fiscal year and prior YTD are positive -- B's scan, which
  reproduces 66 of 5,844 D&A terms (T-161's pre-flight). A term whose three terms are all negative
  is reported separately (option 2), never dropped.
* (ii) the derived fiscal Q4 ``prior_FY - prior 9-month YTD``; flagged when the result has the
  wrong sign for an item that cannot change sign.

The ratio alone also flags genuine cases, so it is a verify_pilot WARN, never a FAIL. The engine
guard uses structural evidence only: a D&A aggregate equal to the filing's
``AmortizationOfIntangibleAssets`` with ``Depreciation`` filed separately (the NDSN 10-K shape).
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from fundamental_agent import db as agent_db
from kg_schema.db import connect_ro

TTM_ITEMS = (
    "net_income",
    "revenue",
    "cogs",
    "operating_cash_flow",
    "capital_expenditure",
    "operating_income",
    "depreciation_amortization",
    "interest_expense",
    "stock_based_compensation",
    "income_tax",
    "pretax_income",
)
# The items that cannot change sign, stored positive (brief §1(ii)).
FIXED_SIGN = (
    "revenue",
    "cogs",
    "capital_expenditure",
    "depreciation_amortization",
    "interest_expense",
    "stock_based_compensation",
)
_QUARTER_MONTHS = {"Q1": 3, "Q2": 6, "Q3": 9}
_AMORTIZATION = ("us-gaap_AdjustmentForAmortization", "us-gaap_AmortizationOfIntangibleAssets")
_SAME = 1.0
_TERMS = 3
_R_LOW = 0.6
_R_HIGH = 1.5


def _engine_version(conn) -> str:
    row = conn.execute(
        "SELECT engine_version FROM fundamental_metrics ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return str(row["engine_version"]) if row else agent_db.METRICS_ENGINE_VERSION


def stored_terms(conn) -> list[dict[str, Any]]:  # noqa: C901 - one provenance pass
    """Every 10-Q's stored YTD-method TTM terms, one record per (filing, item).

    The terms live in the anchor metric row's ``inputs_json.provenance.items[item]`` (H4). An item
    with more than one declaring metric row is de-duplicated; the extractor never saw two rows
    disagree on the terms."""
    version = _engine_version(conn)
    filings = {
        int(r["id"]): dict(r)
        for r in conn.execute(
            "SELECT id, asset_id, fiscal_period, period_end FROM sec_filings WHERE form='10-Q'"
        )
    }
    found: dict[tuple[int, str], dict[str, Any]] = {}
    rows = conn.execute(
        "SELECT id, filing_id, metric_group, metric_name, inputs_json FROM fundamental_metrics "
        "WHERE filing_id IN (SELECT id FROM sec_filings WHERE form='10-Q') "
        "AND engine_version=? ORDER BY id",
        (version,),
    )
    for r in rows:
        if not r["inputs_json"]:
            continue
        try:
            data = json.loads(r["inputs_json"])
        except (TypeError, ValueError):
            continue
        prov = data.get("provenance")
        items = prov.get("items") if isinstance(prov, dict) else None
        if not isinstance(items, dict):
            continue
        for item, entries in items.items():
            if item not in TTM_ITEMS or not isinstance(entries, list):
                continue
            for e in entries:
                if not isinstance(e, dict) or e.get("basis") != "ttm" or e.get("method") != "ytd":
                    continue
                terms = e.get("terms") or []
                if len(terms) != _TERMS:
                    continue
                d = dict(terms)
                if set(d) != {"prior_fiscal_year", "prior_ytd", "ytd"}:
                    continue
                key = (int(r["filing_id"]), item)
                found.setdefault(
                    key,
                    {
                        "filing_id": key[0],
                        "item": item,
                        "prior_fiscal_year": d["prior_fiscal_year"],
                        "prior_ytd": d["prior_ytd"],
                        "ytd": d["ytd"],
                        "value": e.get("value"),
                        "columns": e.get("columns") or [],
                        "period": prov.get("period"),
                        **{
                            k: filings[key[0]][k]
                            for k in ("asset_id", "fiscal_period", "period_end")
                        },
                    },
                )
    return sorted(found.values(), key=lambda x: (x["filing_id"], x["item"]))


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _months(fiscal_period: str) -> int:
    return _QUARTER_MONTHS[fiscal_period[-2:]]


def ratio_flagged(terms: list[dict[str, Any]]) -> dict[str, Any]:
    """Test (i): option 1 (B's scan) and option 2 (all three terms the same sign), separately."""
    tested = Counter()
    tested_opt1 = Counter()
    tested_opt2 = Counter()
    flagged: dict[str, list[dict[str, Any]]] = {"opt1": [], "opt2": [], "negative": []}
    for rec in terms:
        a, b, c = rec["prior_fiscal_year"], rec["prior_ytd"], rec["ytd"]
        if a is None or b is None or c is None or 0 in (a, b, c):
            continue
        m = _months(rec["fiscal_period"])
        r = (b / a) / (m / 12)
        term = {**rec, "m": m, "r": r}
        tested[term["item"]] += 1
        if a > 0 and b > 0:
            tested_opt1[term["item"]] += 1
        if _sign(a) == _sign(b) == _sign(c):
            tested_opt2[term["item"]] += 1
        if not (_R_LOW <= r <= _R_HIGH):
            if a > 0 and b > 0:
                flagged["opt1"].append(term)
            if _sign(a) == _sign(b) == _sign(c):
                flagged["opt2"].append(term)
        if a < 0 and b < 0 and c < 0:
            flagged["negative"].append(term)
    for value in flagged.values():
        value.sort(key=lambda x: (x["filing_id"], x["item"]))
    return {
        "tested": dict(tested),
        "tested_opt1": dict(tested_opt1),
        "tested_opt2": dict(tested_opt2),
        "flagged": flagged,
    }


def _structural_rows(conn, form: str) -> list[dict[str, Any]]:
    """The NDSN shape on *form*: the resolved D&A (or its YTD) equals the amortization fact of the
    same period and a ``Depreciation`` fact is filed separately. A proxy over ``financial_facts``
    for the verify check; the engine's guard reads the filing's own filed facts."""
    ticker = {int(r["id"]): r["ticker"] for r in conn.execute("SELECT id, ticker FROM assets")}
    version = _engine_version(conn)
    rows = conn.execute(
        "SELECT fm.filing_id, sf.asset_id, sf.period_end, sf.fiscal_period, sf.fiscal_year, "
        "sf.accession_number, fm.inputs_json FROM fundamental_metrics fm "
        "JOIN sec_filings sf ON sf.id=fm.filing_id "
        "WHERE sf.form=? AND fm.metric_group='leverage' AND fm.metric_name='net_debt_to_ebitda' "
        "AND fm.engine_version=?",
        (form, version),
    ).fetchall()
    out = []
    for r in rows:
        data = json.loads(r["inputs_json"]) if r["inputs_json"] else {}
        key = "ytd_depreciation_amortization" if form == "10-Q" else "depreciation_amortization"
        da = data.get(key)
        if not isinstance(da, (int, float)):
            continue
        if form == "10-Q":
            pk = (
                f"{r['period_end']} (Q1)"
                if r["fiscal_period"][-2:] == "Q1"
                else f"{r['period_end']} (YTD)"
            )
        else:
            pk = f"{r['period_end']} (FY)"
        facts = {
            x["concept"]: x["value"]
            for x in conn.execute(
                "SELECT concept, value FROM financial_facts WHERE filing_id=? AND period_key=?",
                (r["filing_id"], pk),
            )
            if x["value"] is not None
        }
        dep = facts.get("us-gaap_Depreciation")
        am = next((facts[c] for c in _AMORTIZATION if c in facts), None)
        if dep is None or am is None or abs(da - am) > _SAME:
            continue
        out.append(
            {
                "ticker": ticker.get(r["asset_id"]),
                "asset_id": int(r["asset_id"]),
                "filing_id": int(r["filing_id"]),
                "form": form,
                "period_end": r["period_end"],
                "fiscal_period": r["fiscal_period"],
                "fiscal_year": int(r["fiscal_year"]),
                "da": da,
                "amortization": am,
                "depreciation": dep,
                "sum": dep + am,
            }
        )
    return out


def scan(conn) -> dict[str, Any]:
    terms = stored_terms(conn)
    ratios = ratio_flagged(terms)
    ab = _structural_rows(conn, "10-K") + _structural_rows(conn, "10-Q")
    return {
        "terms": terms,
        "ratios": ratios,
        "structural": ab,
        "tested": ratios["tested"],
        "tested_opt1": ratios["tested_opt1"],
        "tested_opt2": ratios["tested_opt2"],
        "flagged_opt1": len(ratios["flagged"]["opt1"]),
        "flagged_opt2": len(ratios["flagged"]["opt2"]),
        "negative": len(ratios["flagged"]["negative"]),
    }


def summary(path: str) -> dict[str, Any]:
    """What verify_pilot reports: the D&A pre-flight, the full scan and the structural A/B list."""
    conn = connect_ro(path)
    result = scan(conn)
    da_tested = result["tested_opt1"].get("depreciation_amortization", 0)
    da_flagged = sum(
        1 for x in result["ratios"]["flagged"]["opt1"] if x["item"] == "depreciation_amortization"
    )
    return {
        "da_tested": da_tested,
        "da_flagged": da_flagged,
        "tested": sum(result["tested"].values()),
        "flagged_opt1": result["flagged_opt1"],
        "flagged_opt2": result["flagged_opt2"],
        "negative": result["negative"],
        "structural": result["structural"],
    }
