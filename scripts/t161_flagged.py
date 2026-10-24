#!/usr/bin/env python3
"""T-161: write ``out/t161_flagged.csv`` -- every flagged (company, fiscal year, item) with its
class and evidence, plus every flagged 10-Q term.

The flag set is the union of the two test-(i) definitions (option 1: prior FY > 0 and prior YTD >
0, B's scan; option 2: all three terms the same sign) and test (ii), so no flagged term is
dropped. Classes (brief §2):

* A -- a tag mix on the 10-K: the 10-K's D&A aggregate equals its amortization only and
  ``Depreciation`` is filed separately (NDSN).
* B -- the mirror on the 10-Qs (INTU).
* C -- genuine: a changed fiscal year, a restatement (the next 10-K's prior-year column differs),
  an acquisition/disposal, or seasonality (the same ratio in an adjacent year).
* D -- cannot tell.

Read-only. Run on the recomputed scratch copy so ``value_after`` is the engine's value.
"""

# ruff: noqa: C901, PLR0911, PLR0912, PLR0915, PLR2004 -- one linear evidence script

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import date
from typing import Any

sys.path.insert(0, "scripts")
sys.path.insert(0, "src")

import t161_scan

from fundamental_agent import db as agent_db
from kg_schema.db import connect_ro

_ACQ = (
    "PaymentsToAcquireBusinesses",
    "PaymentsToAcquireBusinessesNetOfCashAcquired",
    "ProceedsFromDivestitureOfBusinesses",
    "ProceedsFromDivestitureOfBusinessesNetOfCashDivested",
    "PaymentsForProceedsFromBusinesses",
)


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _interest_terms(conn, version: str) -> list[dict[str, Any]]:
    """The stored terms of interest expense, rebuilt from the stored inputs (no metric declares
    an interest-expense TTM provenance entry; the engine reads ``ytd_interest_expense``)."""
    out = []
    rows = conn.execute(
        "SELECT sf.id, sf.asset_id, sf.fiscal_period, sf.period_end, fm.inputs_json "
        "FROM sec_filings sf JOIN fundamental_metrics fm ON fm.filing_id = sf.id "
        "AND fm.metric_group='leverage' AND fm.metric_name='interest_coverage' "
        "WHERE sf.form='10-Q' AND fm.engine_version=?",
        (version,),
    ).fetchall()
    for row in rows:
        data = json.loads(row["inputs_json"]) if row["inputs_json"] else {}
        cur = data.get("ytd_interest_expense")
        if not isinstance(cur, (int, float)):
            continue
        prior = agent_db._filing_near(
            conn, int(row["asset_id"]), "10-Q", agent_db._months_before(row["period_end"], 12)
        )
        if prior is None:
            continue
        prior_ytd = agent_db._recorded_ytd(conn, prior[0], "interest_expense", version)
        fy_filing = agent_db._fiscal_year_between(
            conn, int(row["asset_id"]), prior[1], row["period_end"]
        )
        if prior_ytd is None or fy_filing is None:
            continue
        fy = agent_db._recorded_flow(conn, fy_filing, "interest_expense", version)
        if fy is None:
            continue
        out.append(
            {
                "filing_id": int(row["id"]),
                "item": "interest_expense",
                "prior_fiscal_year": fy,
                "prior_ytd": prior_ytd,
                "ytd": float(cur),
                "value": None,
                "columns": [],
                "period": f"{row['period_end']} ({row['fiscal_period'][-2:]})",
                "asset_id": int(row["asset_id"]),
                "fiscal_period": row["fiscal_period"],
                "period_end": row["period_end"],
                "fy_filing": int(fy_filing),
            }
        )
    return out


def _fy_meta(conn, fid: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT asset_id, fiscal_year, period_end, form, accession_number, fiscal_period "
        "FROM sec_filings WHERE id=?",
        (fid,),
    ).fetchone()
    return dict(row) if row else None


def _prior_10k(conn, asset_id: int, period_end: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, period_end, fiscal_year FROM sec_filings WHERE asset_id=? AND form='10-K' "
        "AND period_end < ? ORDER BY period_end DESC LIMIT 1",
        (asset_id, period_end),
    ).fetchone()
    return dict(row) if row else None


def _next_10k(conn, asset_id: int, period_end: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, period_end, fiscal_year FROM sec_filings WHERE asset_id=? AND form='10-K' "
        "AND period_end > ? ORDER BY period_end LIMIT 1",
        (asset_id, period_end),
    ).fetchone()
    return dict(row) if row else None


def _fact_at(conn, filing_id: int, period_key: str, concept: str | None) -> float | None:
    if concept is None:
        return None
    row = conn.execute(
        "SELECT value FROM financial_facts WHERE filing_id=? AND period_key=? AND concept=?",
        (filing_id, period_key, concept),
    ).fetchone()
    return float(row["value"]) if row is not None and row["value"] is not None else None


def _acquisition(conn, filing_id: int, period_end: str) -> tuple[str, float] | None:
    key = f"{period_end} (FY)"
    for concept in _ACQ:
        row = conn.execute(
            "SELECT concept, value FROM financial_facts WHERE filing_id=? AND period_key=? "
            "AND concept=? AND value IS NOT NULL",
            (filing_id, key, "us-gaap_" + concept),
        ).fetchone()
        if row is not None and abs(float(row["value"])) > 0.0:
            return str(row["concept"]), float(row["value"])
    return None


def _fy_id(conn, rec: dict[str, Any]) -> int | None:
    if rec.get("fy_filing"):
        return int(rec["fy_filing"])
    cols = {c[0]: c for c in (rec.get("columns") or [])}
    acc = cols.get("prior_fiscal_year", [None, None, None])[2]
    if not acc:
        return None
    row = conn.execute("SELECT id FROM sec_filings WHERE accession_number=?", (acc,)).fetchone()
    return int(row["id"]) if row else None


def _classify(conn, case: dict[str, Any], ab: dict, seasonal: dict) -> tuple[str, str]:
    item = case["item"]
    fy = _fy_meta(conn, case["fy_filing"])
    if fy is None:
        return "D", "the prior 10-K is not stored"
    fy_value = agent_db._recorded_flow(conn, case["fy_filing"], item)
    if item == "depreciation_amortization" and ("10-K", case["asset_id"], fy["fiscal_year"]) in ab:
        row = ab[("10-K", case["asset_id"], fy["fiscal_year"])]
        return "A", (
            f"10-K D&A {row['da']:,.0f} = its amortization only; Depreciation "
            f"{row['depreciation']:,.0f} filed separately; sum {row['sum']:,.0f}"
        )
    for fid in case["filing_ids"]:
        if item == "depreciation_amortization" and ("10-Q", fid) in ab:
            row = ab[("10-Q", fid)]
            return "B", (
                f"10-Q YTD D&A {row['da']:,.0f} = its amortization only; Depreciation "
                f"{row['depreciation']:,.0f} filed separately; sum {row['sum']:,.0f}"
            )
    prior = _prior_10k(conn, case["asset_id"], fy["period_end"])
    if prior is not None:
        days = (date.fromisoformat(fy["period_end"]) - date.fromisoformat(prior["period_end"])).days
        if not 330 <= days <= 400:
            return "C", f"changed fiscal year: the 10-K covers {days} days"
    nxt = _next_10k(conn, case["asset_id"], fy["period_end"])
    if nxt is not None and fy_value is not None:
        nxt_value = _fact_at(conn, nxt["id"], f"{fy['period_end']} (FY)", case.get("concept"))
        if nxt_value is not None and abs(nxt_value - fy_value) > 0.005 * max(abs(fy_value), 1.0):
            return "C", (
                f"restated: the next 10-K's prior-year column gives {nxt_value:,.0f}, this "
                f"10-K gave {fy_value:,.0f}"
            )
    acq = _acquisition(conn, case["fy_filing"], fy["period_end"])
    if acq is not None:
        return "C", f"acquisition/disposal filed: {acq[0]} {acq[1]:,.0f}"
    here = case.get("r")
    if here:
        for other_year in (case["fiscal_year"] - 1, case["fiscal_year"] + 1):
            other = seasonal.get((case["asset_id"], item, case.get("q"), other_year))
            if other is None:
                continue
            if abs(here - other) / abs(here) <= 0.30:
                return "C", (
                    f"seasonal: r={here:.3f} this year, {other:.3f} in FY{other_year} "
                    f"(same quarter position)"
                )
    return "D", "no structural tag mix, restatement, acquisition/disposal or adjacent-year pattern"


def _input_value(conn, filing_id: int, version: str, item: str) -> float | None:
    """The item's anchor input on a 10-K (a period value, not a TTM provenance value)."""
    group, metric = agent_db._TTM_FLOW_SOURCE[item]
    row = conn.execute(
        "SELECT inputs_json FROM fundamental_metrics WHERE filing_id=? AND metric_group=? "
        "AND metric_name=? AND engine_version=? ORDER BY id DESC LIMIT 1",
        (filing_id, group, metric, version),
    ).fetchone()
    if row is None or not row["inputs_json"]:
        return None
    value = json.loads(row["inputs_json"]).get(item)
    return float(value) if isinstance(value, (int, float)) else None


def _value(conn, filing_id: int, version: str, item: str) -> float | None:
    row = conn.execute(
        "SELECT inputs_json FROM fundamental_metrics WHERE filing_id=? AND "
        "metric_group=? AND metric_name=? AND engine_version=? ORDER BY id DESC LIMIT 1",
        (
            filing_id,
            agent_db._TTM_FLOW_SOURCE[item][0],
            agent_db._TTM_FLOW_SOURCE[item][1],
            version,
        ),
    ).fetchone()
    if row is None or not row["inputs_json"]:
        return None
    data = json.loads(row["inputs_json"])
    entries = (data.get("provenance") or {}).get("items", {}).get(item) or []
    for entry in entries:
        if entry.get("basis") == "ttm":
            return entry.get("value")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", default="out/t161_flagged.csv")
    args = ap.parse_args()
    conn = connect_ro(args.db)
    version = t161_scan._engine_version(conn)
    v10 = "metrics-v10"
    ticker = {int(r["id"]): r["ticker"] for r in conn.execute("SELECT id, ticker FROM assets")}
    terms = t161_scan.stored_terms(conn, v10) + _interest_terms(conn, v10)

    # every term's own r and its option flags
    seasonal: dict[tuple, float] = {}
    pairs: set[tuple[int, int, str]] = set()
    for rec in terms:
        fid = _fy_id(conn, rec)
        if fid is None:
            continue
        meta = _fy_meta(conn, fid)
        if meta is None:
            continue
        asset, fy_year = int(meta["asset_id"]), int(meta["fiscal_year"])
        pairs.add((asset, fid, rec["item"]))
        a, b, c = rec["prior_fiscal_year"], rec["prior_ytd"], rec["ytd"]
        if a is None or b is None or c is None or 0 in (a, b, c):
            rec["r"] = None
            continue
        r = (b / a) / (t161_scan._months(rec["fiscal_period"]) / 12)
        rec["r"] = r
        rec["fiscal_year"] = fy_year
        rec["fy_filing"] = fid
        rec["q"] = rec["fiscal_period"][-2:]
        if a > 0 and b > 0:
            seasonal[(asset, rec["item"], rec["q"], fy_year)] = r
        rec["opt1"] = a > 0 and b > 0 and not (0.6 <= r <= 1.5)
        rec["opt2"] = _sign(a) == _sign(b) == _sign(c) and not (0.6 <= r <= 1.5)

    # test (ii) over every (company, prior FY, item) pair, as the engine derives the fiscal Q4
    q4: dict[tuple, float] = {}
    q4_detail: dict[tuple, tuple[float, float]] = {}
    for asset, fid, item in sorted(pairs):
        meta = _fy_meta(conn, fid)
        if meta is None:
            continue
        fy = agent_db._recorded_flow(conn, fid, item, v10)
        ytd9 = agent_db._nine_month_ytd(conn, asset, meta["period_end"], item, v10)
        if fy is None or ytd9 is None:
            continue
        q4[(asset, int(meta["fiscal_year"]), item)] = fy - ytd9
        q4_detail[(asset, int(meta["fiscal_year"]), item)] = (fy, ytd9)

    # the cases
    cases: dict[tuple, dict[str, Any]] = {}
    for rec in terms:
        if not rec.get("opt1") and not rec.get("opt2"):
            continue
        key = (int(rec["asset_id"]), int(rec["fiscal_year"]), rec["item"])
        case = cases.setdefault(
            key,
            {
                "asset_id": key[0],
                "ticker": ticker.get(key[0]),
                "fiscal_year": key[1],
                "item": key[2],
                "fy_filing": int(rec["fy_filing"]),
                "terms": [],
                "filing_ids": [],
                "r": rec["r"],
                "q": rec["q"],
                "tests": set(),
                "rules": set(),
            },
        )
        case["terms"].append(rec)
        case["filing_ids"].append(rec["filing_id"])
        case["tests"].add("i")
        if rec["opt1"]:
            case["rules"].add("opt1")
        if rec["opt2"]:
            case["rules"].add("opt2")
    for key, value in q4.items():
        if value >= 0 or key[2] not in t161_scan.FIXED_SIGN:
            continue
        asset, fy_year, item = key
        row = conn.execute(
            "SELECT id FROM sec_filings WHERE asset_id=? AND form='10-K' AND fiscal_year=?",
            (asset, fy_year),
        ).fetchone()
        if row is None:
            continue
        case = cases.setdefault(
            (asset, fy_year, item),
            {
                "asset_id": asset,
                "ticker": ticker.get(asset),
                "fiscal_year": fy_year,
                "item": item,
                "fy_filing": int(row["id"]),
                "terms": [],
                "filing_ids": [],
                "r": None,
                "q": None,
                "tests": set(),
                "rules": set(),
            },
        )
        case["tests"].add("ii")

    # the structural A/B rows and each case's 10-K concept
    ab: dict[tuple, dict[str, Any]] = {}
    for row in t161_scan._structural_rows(conn, "10-K", v10):
        ab[("10-K", row["asset_id"], row["fiscal_year"])] = row
    for row in t161_scan._structural_rows(conn, "10-Q", v10):
        ab[("10-Q", row["filing_id"])] = row
    for case in cases.values():
        group, metric = agent_db._TTM_FLOW_SOURCE[case["item"]]
        prow = conn.execute(
            "SELECT inputs_json FROM fundamental_metrics WHERE filing_id=? AND metric_group=? "
            "AND metric_name=? AND engine_version=?",
            (case["fy_filing"], group, metric, v10),
        ).fetchone()
        case["concept"] = None
        if prow is not None and prow["inputs_json"]:
            prov = json.loads(prow["inputs_json"]).get("provenance") or {}
            entries = prov.get("items", {}).get(case["item"]) or []
            case["concept"] = entries[0].get("concept") if entries else None

    rows_out = []
    class_of: dict[tuple, str] = {}
    for key, case in cases.items():
        klass, evidence = _classify(conn, case, ab, seasonal)
        class_of[key] = klass
        if case["terms"]:
            for term in case["terms"]:
                before = _value(conn, term["filing_id"], v10, case["item"])
                after = _value(conn, term["filing_id"], version, case["item"])
                if after is None:
                    after = before  # not recomputed: the value is unchanged
                rows_out.append(
                    {
                        "company": case["ticker"],
                        "filing": term["period"],
                        "item": case["item"],
                        "terms": f"FY={term['prior_fiscal_year']:,.0f}, "
                        f"prior_ytd={term['prior_ytd']:,.0f}, ytd={term['ytd']:,.0f}",
                        "r": f"{term['r']:.6f}" if term["r"] is not None else "",
                        "derived_q4": (f"{q4[key]:,.0f}" if key in q4 else ""),
                        "class": klass,
                        "evidence": evidence,
                        "value_before": f"{before:,.0f}"
                        if isinstance(before, (int, float))
                        else "",
                        "value_after": f"{after:,.0f}" if isinstance(after, (int, float)) else "",
                        "tests": "+".join(sorted(case["tests"])),
                        "rules": ("opt1" if term["opt1"] else "")
                        + ("+opt2" if term["opt2"] else ""),
                        "note": (
                            "all three terms negative (option 2 only)"
                            if term["opt2"] and not term["opt1"]
                            else (
                                "mixed sign (option 1 only)"
                                if term["opt1"] and not term["opt2"]
                                else ""
                            )
                        ),
                    }
                )
        else:
            fy, ytd9 = q4_detail.get(key, (None, None))
            rows_out.append(
                {
                    "company": case["ticker"],
                    "filing": f"10-K FY{case['fiscal_year']}",
                    "item": case["item"],
                    "terms": "FY, 9M YTD, derived Q4",
                    "r": "",
                    "derived_q4": f"{q4[key]:,.0f}",
                    "class": klass,
                    "evidence": evidence,
                    "value_before": "",
                    "value_after": "",
                    "tests": "+".join(sorted(case["tests"])),
                    "rules": "",
                    "note": "",
                }
            )
    # The A case's own 10-K reading: the TTM rows above inherit it, and the 10-K's own
    # metrics (EBITDA, net debt / EBITDA, enterprise FCF yield) change with it.
    for key, case in cases.items():
        if class_of.get(key) != "A":
            continue
        before_da = _input_value(conn, case["fy_filing"], v10, "depreciation_amortization")
        after_da = _input_value(conn, case["fy_filing"], version, "depreciation_amortization")
        fy = _fy_meta(conn, case["fy_filing"]) or {}
        rows_out.append(
            {
                "company": case["ticker"],
                "filing": f"10-K FY{case['fiscal_year']} ({fy.get('period_end')})",
                "item": "depreciation_amortization",
                "terms": "the 10-K's FY reading (the TTM rows above inherit it)",
                "r": "",
                "derived_q4": "",
                "class": "A",
                "evidence": _classify(conn, case, ab, seasonal)[1],
                "value_before": f"{before_da:,.0f}" if isinstance(before_da, (int, float)) else "",
                "value_after": f"{after_da:,.0f}" if isinstance(after_da, (int, float)) else "",
                "tests": "structural",
                "rules": "",
                "note": "the 10-K's own metrics change with this reading",
            }
        )
    with open(args.out, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows_out[0]))
        writer.writeheader()
        writer.writerows(rows_out)
    print(f"wrote {args.out}: {len(rows_out)} rows, {len(cases)} cases")
    print("rows by class:", dict(Counter(r["class"] for r in rows_out)))
    print("cases by class:", dict(Counter(class_of.values())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
