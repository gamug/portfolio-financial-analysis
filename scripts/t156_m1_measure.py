"""T-156 phase 1 — M1 over every date: the empty-EBITDA counts and (at 2026-09-22) the classes.

Outputs: out/m1_trend.json, out/m1_rows.csv, out/m1_classes.json.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path

from cycle import data
from kg_schema.market_cap import market_caps_as_of
from kg_schema.versions import resolve_metric_versions

DB = "/workspaces/thesis/scratch/t156/work.db"
UNIVERSE = "SP500"
UNIVERSE_DB = "/workspaces/thesis/data/universe_history.db"
OUT = Path("/workspaces/thesis/scratch/t156/out")
DATES = (
    "2023-03-31",
    "2023-06-30",
    "2023-09-30",
    "2023-12-31",
    "2024-03-31",
    "2024-06-30",
    "2024-09-30",
    "2024-12-31",
    "2025-03-31",
    "2025-06-30",
    "2025-09-30",
    "2025-12-31",
    "2026-03-31",
    "2026-06-30",
    "2026-09-22",
)
MAIN = "2026-09-22"

DA_RE = re.compile(r"depreciat|amorti|deplet", re.IGNORECASE)
AGG = (
    "us-gaap_DepreciationDepletionAndAmortization",
    "us-gaap_DepreciationAmortizationAndAccretionNet",
    "us-gaap_DepreciationAndAmortization",
)
DA = "us-gaap_Depreciation"
# Ruling (c): a us-gaap element whose definition covers D&A in another scope or beside a further
# item, so reading it as MD-01's D&A is not exact (definitions quoted in out/m2_definitions.txt).
WIDER_DA = frozenset(
    {
        "us-gaap_CostDepreciationAmortizationAndDepletion",
        "us-gaap_UtilitiesOperatingExpenseDepreciationAndAmortization",
    }
)
# Ruling (b): a D&A-nature us-gaap element that is a component, not the total.
COMPONENT_DA = frozenset(
    {
        "us-gaap_OtherDepreciationAndAmortization",
        "us-gaap_AmortizationOfIntangibleAssets",
        "us-gaap_Depletion",
        "us-gaap_AdjustmentForAmortization",
        "us-gaap_CostOfGoodsAndServicesSoldDepreciation",
        "us-gaap_CostOfGoodsAndServicesSoldDepreciationAndAmortization",
        "us-gaap_CostOfGoodsAndServicesSoldAmortization",
        "us-gaap_DepreciationNonproduction",
    }
)
# Ruling (c): an extension whose name or caption says "and other".
AND_OTHER_RE = re.compile(r"and\s*other", re.IGNORECASE)


def term_of(entry: dict | None, form: str) -> dict:
    if entry is None:
        return {"present": False, "method": None, "flags": [], "concept": None}
    method = entry.get("method")
    present = method in ("ytd", "quarters") if form == "10-Q" else entry.get("concept") is not None
    return {
        "present": present,
        "method": method,
        "flags": entry.get("flow_flags") or [],
        "concept": entry.get("concept"),
    }


def da_class(f: dict) -> tuple[str, dict]:  # noqa: C901, PLR0911 - one branch per class
    """Rulings (a)-(c), (e): the D&A class of a filing whose EBITDA D&A term is missing."""
    flags = f["metric_flags"]
    cf = f["da_cf_concepts"]
    concepts = set(cf)
    ext, other, child, agg = f["da_ext"], f["da_other"], f["da_child"], f["da_agg"]
    all_da = concepts | set(f["da_is"])
    # 1. no D&A-nature fact at all
    if not all_da:
        return "da_absent", {"evidence": "no D&A-nature fact (cash-flow or income-statement)"}
    # (a) the only D&A fact is a us-gaap element on the income statement
    if not cf and f["da_is"]:
        return "da_income_statement_only", {"income_statement": sorted(set(f["da_is"]))}
    m = f["da_term"]["method"]
    if f["form"] == "10-Q":
        # (e) ttm_refused: G2 refused a term; ttm_term_absent: a prior column / prior 10-K is not
        if m in {"refused", "x4"}:
            return "ttm_refused", {
                "method": m,
                "flow_flags": f["da_term"]["flags"],
                "own_concept": f["da_term_concept"],
            }
        if m is None and (f["da_term_concept"] is not None or agg):
            return "ttm_term_absent", {"own_concept": f["da_term_concept"], "aggregate_filed": agg}
    if any("da_sum_refused_other_lines" in x for x in flags):
        return "da_refused_other_lines", {"outside": other}
    if any("depreciation_only_not_da" in x for x in flags) and DA in concepts:
        return "da_refused_depreciation_only", {"filed": sorted(concepts)}
    wider = [c for c in cf if c in WIDER_DA]
    comp = [c for c in cf if c in COMPONENT_DA]
    # (c) a company extension carries the D&A
    if ext:
        sub = [c for c in ext if AND_OTHER_RE.search(c.split("_", 1)[-1])] or [
            c
            for c, lab in f.get("da_labels", {}).items()
            if c in ext and AND_OTHER_RE.search(lab or "")
        ]
        return "da_extension_only", {"extensions": ext, "caption_and_other": bool(sub)}
    if wider:
        return "da_element_with_other", {"element": wider}
    if comp:
        return "da_components_no_depreciation", {"components": comp}
    return "UNCLASSIFIED", {"concepts": cf, "agg": agg, "child": child, "ext": ext, "other": other}


def oi_class(f: dict) -> tuple[str, dict]:
    if f["oi_term"]["present"]:
        return "", {}
    if not f["has_oi_fact"]:
        return "oi_absent_presentation", {"presents": f["subtotals"]}
    return "oi_absent_other", {"concept": f["oi_term_concept"], "method": f["oi_term"]["method"]}


def build(date: str, conn: sqlite3.Connection, dro) -> tuple[list[dict], dict]:  # noqa: PLR0915
    versions = resolve_metric_versions(dro, None)
    members = data.active_universe(dro, UNIVERSE, date, UNIVERSE_DB)
    ids = [int(r["id"]) for r in members]
    profiles = data.asset_profiles(dro, ids, date)
    caps = market_caps_as_of(dro, ids, as_of=date).caps
    metrics = data.latest_metrics(dro, date, versions)
    filings = {
        int(r["asset_id"]): r
        for r in conn.execute(
            """
            WITH known AS (
                SELECT f.id, f.asset_id, ROW_NUMBER() OVER (
                    PARTITION BY f.asset_id ORDER BY f.period_end DESC, f.available_at DESC, f.id DESC
                ) AS rn FROM sec_filings f
                WHERE f.period_end IS NOT NULL AND f.available_at IS NOT NULL AND f.available_at <= ?
            )
            SELECT k.asset_id, s.id AS filing_id, s.form, s.accession_number, s.period_end,
                   s.fiscal_year, s.fiscal_period
            FROM known k JOIN sec_filings s ON s.id = k.id WHERE k.rn = 1
            """,
            (date,),
        )
    }
    rows: list[dict] = []
    for r in members:
        aid = int(r["id"])
        prof = profiles.get(aid)
        vals = metrics.get(aid, {})
        f = {
            "asset": aid,
            "ticker": r["ticker"],
            "sector": r["sector"],
            "type": None if prof is None else prof.type,
            "financial_firm": bool(prof and prof.financial_firm),
            "reit": bool(prof and prof.reit),
            "regulated_utility": bool(prof and prof.regulated_utility),
            "has_filing": aid in filings,
            "market_cap": (caps[aid].value if aid in caps else None),
            "ebitda": vals.get("leverage.ebitda"),
        }
        ff = filings.get(aid)
        if ff is not None:
            f.update(
                filing_id=ff["filing_id"],
                form=ff["form"],
                acc=ff["accession_number"],
                period_end=ff["period_end"],
            )
            row = conn.execute(
                "SELECT value, inputs_json FROM fundamental_metrics WHERE filing_id=? AND "
                "metric_group='leverage' AND metric_name='ebitda'",
                (ff["filing_id"],),
            ).fetchone()
            ij = json.loads(row["inputs_json"] or "{}") if row else {}
            f["oi_input"] = ij.get("operating_income")
            f["da_input"] = ij.get("depreciation_amortization")
            f["metric_flags"] = sorted(k for k in ij if k.startswith("flag_"))
            nr = conn.execute(
                "SELECT value, inputs_json FROM fundamental_metrics WHERE filing_id=? AND "
                "metric_group='leverage' AND metric_name='net_debt_to_ebitda'",
                (ff["filing_id"],),
            ).fetchone()
            items = (
                ((json.loads(nr["inputs_json"] or "{}") if nr else {}).get("provenance") or {}).get(
                    "items"
                )
            ) or {}
            f["ndte_value"] = nr["value"] if nr else None
            f["da_term"] = term_of((items.get("depreciation_amortization") or [{}])[0], ff["form"])
            f["oi_term"] = term_of((items.get("operating_income") or [{}])[0], ff["form"])
            f["da_term_concept"] = (items.get("depreciation_amortization") or [{}])[0].get(
                "concept"
            )
            f["oi_term_concept"] = (items.get("operating_income") or [{}])[0].get("concept")
            facts = conn.execute(
                "SELECT concept, label, statement, period_key, value FROM financial_facts WHERE filing_id=?",
                (ff["filing_id"],),
            ).fetchall()
            by_col: dict[str, list[str]] = {}
            da_labels: dict[str, str] = {}
            da_is, oi, subs = [], [], set()
            for x in facts:
                concept, label = str(x["concept"] or ""), str(x["label"] or "")
                local = concept.split("_", 1)[-1]
                if DA_RE.search(local) or DA_RE.search(label):
                    if x["statement"] == "cash_flow":
                        by_col.setdefault(x["period_key"], []).append(concept)
                        da_labels.setdefault(concept, label)
                    elif x["statement"] == "income_statement":
                        da_is.append(concept)
                if local == "OperatingIncomeLoss":
                    oi.append(concept)
                if re.search(
                    r"IncomeLossFromContinuingOperations|IncomeTaxExpenseBenefit$|ProfitLoss$|NetIncomeLoss$|GrossProfit$|OperatingExpenses$|CostsAndExpenses$|Revenues$",
                    local,
                ):
                    subs.add(local)
            f["da_by_col"] = by_col
            f["da_labels"] = da_labels
            f["da_is"] = sorted(set(da_is))
            concepts = sorted({c for v in by_col.values() for c in v})
            f["da_cf_concepts"] = concepts
            f["da_agg"] = [c for c in concepts if c in AGG]
            f["da_child"] = [
                c
                for c in concepts
                if c
                in (
                    "us-gaap_Depletion",
                    "us-gaap_OtherDepreciationAndAmortization",
                    "us-gaap_AmortizationOfIntangibleAssets",
                    DA,
                )
            ]
            f["da_ext"] = [c for c in concepts if not c.startswith("us-gaap_")]
            f["da_other"] = [
                c
                for c in concepts
                if c not in AGG and c not in f["da_child"] and c.startswith("us-gaap_")
            ]
            f["has_oi_fact"] = bool(oi)
            f["subtotals"] = sorted(subs)
        rows.append(f)
    counts = {
        "date": date,
        "members": len(rows),
        "with_usable_filing": sum(1 for f in rows if f["has_filing"]),
        "empty_ebitda": sum(1 for f in rows if f["has_filing"] and f["ebitda"] is None),
        "financial_firm_members": sum(1 for f in rows if f["financial_firm"]),
        "financial_firm_empty": sum(
            1 for f in rows if f["has_filing"] and f["ebitda"] is None and f["financial_firm"]
        ),
        "da_input_missing": sum(
            1 for f in rows if f["has_filing"] and f["ebitda"] is None and f.get("da_input") is None
        ),
        "oi_input_missing": sum(
            1 for f in rows if f["has_filing"] and f["ebitda"] is None and f.get("oi_input") is None
        ),
        "da_term_missing": sum(
            1
            for f in rows
            if f["has_filing"]
            and f["ebitda"] is None
            and not f.get("financial_firm")
            and not f["da_term"]["present"]
        ),
        "oi_term_missing": sum(
            1
            for f in rows
            if f["has_filing"]
            and f["ebitda"] is None
            and not f.get("financial_firm")
            and not f["oi_term"]["present"]
        ),
        "operating_members": sum(1 for f in rows if not f["financial_firm"]),
    }
    return rows, counts


def main() -> None:  # noqa: C901, PLR0912, PLR0915 - one branch per class
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    dro = data.connect_ro(DB)
    trend, main_rows = [], []
    class_trend: dict[str, dict] = {}
    for date in DATES:
        rows, counts = build(date, conn, dro)
        trend.append(counts)
        cc: dict = {}
        for f in rows:
            if not f["has_filing"] or f["ebitda"] is not None:
                continue
            if f["financial_firm"]:
                cc["financial_firm_na"] = cc.get("financial_firm_na", 0) + 1
                continue
            if not f["da_term"]["present"]:
                k = da_class(f)[0]
                cc[k] = cc.get(k, 0) + 1
            if not f["oi_term"]["present"]:
                k = oi_class(f)[0]
                cc[k] = cc.get(k, 0) + 1
        class_trend[date] = cc
        print(json.dumps(counts))
        print("   classes:", json.dumps(cc))
        if date == MAIN:
            main_rows = rows
    OUT.joinpath("m1_trend.json").write_text(json.dumps(trend, indent=1))
    # classes at MAIN
    class_counts: dict = {}
    period_input: dict = {}
    out_rows = []
    for f in main_rows:
        if not f["has_filing"]:
            continue
        if f["ebitda"] is not None:
            continue
        rec = {
            "ticker": f["ticker"],
            "asset": f["asset"],
            "type": f["type"],
            "form": f["form"],
            "accession": f["acc"],
            "period_end": f["period_end"],
            "financial_firm": f["financial_firm"],
            "market_cap": f["market_cap"],
            "oi_input": f["oi_input"],
            "da_input": f["da_input"],
        }
        if f["financial_firm"]:
            rec["da_class"] = rec["oi_class"] = "financial_firm_na"
            class_counts["financial_firm_na"] = class_counts.get("financial_firm_na", 0) + 1
        else:
            rec["da_class"], rec["da_evidence"] = (
                da_class(f) if not f["da_term"]["present"] else ("", {})
            )
            rec["oi_class"], rec["oi_evidence"] = oi_class(f)
        for k in ("da_class", "oi_class"):
            if rec[k] and not f["financial_firm"]:
                class_counts[rec[k]] = class_counts.get(rec[k], 0) + 1
                # ruling (d): the period-input basis as a second column
                if f["da_input"] is None:
                    period_input[rec[k]] = period_input.get(rec[k], 0) + 1
                if f["oi_input"] is None:
                    period_input[rec[k]] = period_input.get(rec[k], 0) + 1
        out_rows.append(rec)
    OUT.joinpath("m1_classes.json").write_text(
        json.dumps(
            {
                "date": MAIN,
                "counts": class_counts,
                "period_input_basis": period_input,
                "class_trend": class_trend,
                "trend": trend,
            },
            indent=1,
        )
    )
    print("period-input basis per class:", json.dumps(period_input))
    with OUT.joinpath("m1_rows.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        for rec in out_rows:
            w.writerow(
                {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in rec.items()}
            )
    print("class counts", json.dumps(class_counts))
    conn.close()
    dro.close()


if __name__ == "__main__":
    main()
