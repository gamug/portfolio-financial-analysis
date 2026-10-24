#!/usr/bin/env python3
"""T-161 r2 §1: where does the amortization-only D&A rule fire, on every pilot_a3 filing?

The D&A is re-resolved for every filing and every stored column, twice: with master's
``Statements`` and with the PR's. The offline filings are rebuilt from ``financial_facts``
(``sec_xcheck.common.statements_from_db``, no filed facts). The filings whose D&A the engine read
from the gateway's ``filed_facts`` **with an aggregate concept** are rebuilt from a cached gateway
payload, because that list is not stored; there are 150 of them (the only place the filed-fact
side of the guard can fire — a filing with no accepted aggregate cannot trigger it).

Usage::

    T161_SRC=/tmp/t161/master/src python scripts/t161_rule_fires.py resolve --tag master \
        --db /workspaces/thesis/pilot_a3/financial.db --out /tmp/t161/resolve_master.json \
        --cache /tmp/t161/payloads --fetch
    python scripts/t161_rule_fires.py resolve --tag pr ... --out /tmp/t161/resolve_pr.json
    python scripts/t161_rule_fires.py compare --master /tmp/t161/resolve_master.json \
        --pr /tmp/t161/resolve_pr.json --db ... --out out/t161_rule_fires.csv

Read-only on the database. The gateway payloads are cached under *cache*.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_SRC = os.environ.get("T161_SRC")
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))
if _SRC:
    sys.path.insert(0, _SRC)  # the chosen code version wins
sys.path.insert(0, str(_ROOT / "scripts"))

import csv  # noqa: E402
from dataclasses import replace  # noqa: E402

from sec_xcheck.common import statements_from_db  # noqa: E402

from fundamental_agent.config import DEFAULT_EDGAR_BASE_URL  # noqa: E402
from fundamental_agent.edgar_client import EdgarClient  # noqa: E402
from fundamental_agent.statements import (  # noqa: E402
    DA_AGGREGATE_CONCEPTS,
    REGISTRY,
    G,
    Statements,
    _filed_facts,
    _filed_facts_error,
)
from kg_schema.db import connect_ro  # noqa: E402


def _filed_aggregate_filings(conn) -> set[int]:
    """The filings whose resolved D&A is an aggregate concept read from the gateway's filed facts:
    the only filings where the filed-fact side of the guard can fire."""
    out: set[int] = set()
    for r in conn.execute(
        "SELECT filing_id, inputs_json FROM fundamental_metrics WHERE metric_group='leverage' "
        "AND metric_name='net_debt_to_ebitda' AND engine_version='metrics-v10'"
    ):
        if not r["inputs_json"]:
            continue
        data = json.loads(r["inputs_json"])
        entries = (data.get("provenance") or {}).get("items", {}).get(
            "depreciation_amortization"
        ) or []
        if not entries:
            continue
        e = entries[0]
        if e.get("how") == "filed_fact" and e.get("concept") in DA_AGGREGATE_CONCEPTS:
            out.add(int(r["filing_id"]))
    return out


def _payload(conn, filing: dict[str, Any], cache: Path, fetch: bool) -> dict[str, Any] | None:
    path = cache / f"{filing['accession_number']}.json"
    if path.exists():
        return json.loads(path.read_text())
    if not fetch:
        return None
    cik = filing["cik"] or filing["asset_cik"]
    # the gateway lists a filing by the year it was *filed*, not the year its period ends
    year = int((filing.get("filing_date") or filing["period_end"])[:4])
    try:
        with EdgarClient(DEFAULT_EDGAR_BASE_URL) as client:
            payload = client.financials(
                cik,
                filing["form"],
                year,
                accession_number=filing["accession_number"],
            )
    except Exception as exc:
        print(f"  ! {filing['ticker']} {filing['form']} {filing['accession_number']}: {exc}")
        return None
    cache.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return payload


def _facts(stmts: Statements, column: str) -> dict[str, Any]:
    """The D&A facts of *column* as the resolver sees them: the statements first, then the filed
    facts (for the payload-backed filings)."""
    spec = REGISTRY["depreciation_amortization"]
    period = next((p for p in stmts.periods if p.key == column), None)
    out: dict[str, Any] = {
        "amortization": stmts._concept_value(spec, column, G + "AmortizationOfIntangibleAssets"),
        "depreciation": stmts._concept_value(spec, column, G + "Depreciation"),
        "aggregate": {c: stmts._concept_value(spec, column, c) for c in DA_AGGREGATE_CONCEPTS},
    }
    if period is not None and stmts.filed_facts:
        filed = {}
        for name, concept in (
            ("amortization", G + "AmortizationOfIntangibleAssets"),
            ("depreciation", G + "Depreciation"),
            *((c, c) for c in DA_AGGREGATE_CONCEPTS),
        ):
            value, _status = stmts._filed_fact_value(spec, period, concept)
            if value is not None:
                filed[name] = value
        out["filed"] = filed
    return out


def resolve(db: str, out: str, cache: str, fetch: bool) -> int:
    conn = connect_ro(db)
    filed_aggregates = _filed_aggregate_filings(conn)
    print(f"filed-fact aggregate filings: {len(filed_aggregates)}")
    filings = {
        int(r["id"]): dict(r)
        for r in conn.execute(
            "SELECT sf.id, sf.asset_id, sf.form, sf.fiscal_year, sf.fiscal_period, sf.period_end, "
            "sf.filing_date, sf.accession_number, sf.cik, a.ticker, a.cik AS asset_cik "
            "FROM sec_filings sf "
            "JOIN assets a ON a.id = sf.asset_id"
        )
    }
    results: dict[str, Any] = {}
    for fid, filing in filings.items():
        payload = _payload(conn, filing, Path(cache), fetch) if fid in filed_aggregates else None
        stmts = statements_from_db(conn, fid)
        if payload is not None:
            # the stored statements (the pilot's input) + the gateway's filed facts (not stored)
            stmts = replace(
                stmts,
                filed_facts=_filed_facts(payload),
                filed_facts_error=_filed_facts_error(payload),
            )
        for period in stmts.periods:
            res = stmts.resolve("depreciation_amortization", period.key)
            results[f"{fid}|{period.key}"] = {
                "filing_id": fid,
                "column": period.key,
                "tag": period.tag,
                "value": res.value,
                "concept": res.concept,
                "how": res.how,
                "flags": list(res.flags),
                "facts": _facts(stmts, period.key),
                "payload": payload is not None,
            }
    Path(out).write_text(json.dumps(results))
    print(f"wrote {out}: {len(results)} (filing, column) resolutions")
    return 0


def _ten_q(conn, filing: dict[str, Any], column_date: str) -> str:
    """For a 10-K column: that fiscal year's 10-Qs' D&A (the YTD value when stored), period:value."""
    rows = conn.execute(
        "SELECT sf.period_end, fm.inputs_json FROM sec_filings sf JOIN fundamental_metrics fm "
        "ON fm.filing_id = sf.id AND fm.metric_group='leverage' "
        "AND fm.metric_name='net_debt_to_ebitda' AND fm.engine_version='metrics-v10' "
        "WHERE sf.asset_id=? AND sf.form='10-Q' AND sf.period_end <= ? "
        "ORDER BY sf.period_end",
        (filing["asset_id"], column_date),
    ).fetchall()
    # the fiscal year's quarters: the last three before the 10-K end (and the Q1 of the same FY)
    picked = rows[-3:]
    out = []
    for r in picked:
        data = json.loads(r["inputs_json"]) if r["inputs_json"] else {}
        value = data.get("ytd_depreciation_amortization")
        if value is None:
            value = data.get("depreciation_amortization")
        out.append(
            f"{r['period_end']}:{value:,.0f}"
            if isinstance(value, (int, float))
            else f"{r['period_end']}:"
        )
    return "; ".join(out)


def compare(master: str, pr: str, db: str, out: str) -> int:
    old = json.loads(Path(master).read_text())
    new = json.loads(Path(pr).read_text())
    conn = connect_ro(db)
    filings = {
        int(r["id"]): dict(r)
        for r in conn.execute(
            "SELECT sf.id, sf.asset_id, sf.form, sf.fiscal_year, sf.fiscal_period, sf.period_end, "
            "sf.accession_number, a.ticker FROM sec_filings sf JOIN assets a ON a.id = sf.asset_id"
        )
    }
    rows = []
    for key, a in old.items():
        b = new.get(key)
        if b is None:
            continue
        new_flags = [f for f in b["flags"] if f.startswith("da_amortization_only")]
        if a["value"] == b["value"] and not new_flags:
            continue
        f = filings[b["filing_id"]]
        facts = b["facts"]
        filed = facts.get("filed") or {}
        amort = filed.get("amortization", facts.get("amortization"))
        dep = filed.get("depreciation", facts.get("depreciation"))
        rows.append(
            {
                "company": f["ticker"],
                "form": f["form"],
                "period": f["fiscal_period"],
                "column": b["column"],
                "aggregate_concept": a["concept"] or "",
                "aggregate_value": f"{a['value']:,.0f}" if a["value"] is not None else "",
                "amortization_concept": "us-gaap_AmortizationOfIntangibleAssets",
                "amortization_value": f"{amort:,.0f}" if isinstance(amort, (int, float)) else "",
                "depreciation": f"{dep:,.0f}" if isinstance(dep, (int, float)) else "",
                "new_value": f"{b['value']:,.0f}" if b["value"] is not None else "",
                "flag": ",".join(new_flags) or "(same value)",
                "ten_q_da": (_ten_q(conn, f, b["column"][:10]) if f["form"] == "10-K" else ""),
                "class": "A" if f["form"] == "10-K" else "B",
            }
        )
    with open(out, "w", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=list(rows[0])
            if rows
            else [
                "company",
                "form",
                "period",
                "column",
                "aggregate_concept",
                "aggregate_value",
                "amortization_concept",
                "amortization_value",
                "depreciation",
                "new_value",
                "flag",
                "ten_q_da",
                "class",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {out}: {len(rows)} rows; companies {len({r['company'] for r in rows})}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("resolve", "compare"))
    ap.add_argument("--db", default="/workspaces/thesis/pilot_a3/financial.db")
    ap.add_argument("--tag")
    ap.add_argument("--out")
    ap.add_argument("--cache", default="/tmp/t161/payloads")  # noqa: S108 - scratch
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--master")
    ap.add_argument("--pr")
    args = ap.parse_args()
    if args.mode == "resolve":
        assert args.out
        return resolve(args.db, args.out, args.cache, args.fetch)
    assert args.master and args.pr and args.out
    return compare(args.master, args.pr, args.db, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
