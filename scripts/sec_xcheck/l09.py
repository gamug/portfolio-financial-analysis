"""L-09: the register of every revenue value the project derives (T-117's correction, T-140's rebuild).

For every income-statement duration column of every stored filing, the revenue resolver either reads a filed total or
*derives* one:

* **T-140** -- no revenue total survived (the gateway dropped a total tagged only with a dimension): the value is the
  statement's own later "Total revenues and other" less the rows between;
* **T-117** -- a filed total is contradicted by a later, materially smaller total-labelled row: the value is that row
  less the rows between.

Guard (both): every row between is no larger than 25% of the anchor; T-140 also accepts the gateway's own record of the
dropped total when it agrees to the dollar. A value that fails the guard is **refused** (status ``refused``), never
derived. Per D-03 the guards are *calibrated*, and each derived value gets a hand check: ``verified_by`` and
``verified_on`` stay empty for the user.

The ex-post audit compares each value with the same period in a later filing's comparative column (validation only,
never an input, so no look-ahead); that column **may be restated**, and the register says so.
"""

from __future__ import annotations

import collections
import csv
from pathlib import Path
from typing import Any

from fundamental_agent.statements import REGISTRY, Statements
from fundamental_agent.statements import _numeric as numeric
from sec_xcheck.common import filings, statements_from_db
from sec_xcheck.findings import Finding, spread

CMD = "uv run python scripts/audit_sec_checklist.py l09"
BETWEEN_CEILING = 0.25
FIELDS = [
    "ticker", "cik", "accession", "form", "fiscal_period", "column_period_end", "column_tag", "column_kind", "method",
    "status", "derived_value", "resolver_value", "anchor_label", "anchor_value", "between_rows", "guard", "guard_pass", "filed_total",
    "also_derived_in_filings", "expost_accession", "expost_column", "expost_value", "expost_difference",
    "expost_difference_pct", "expost_note", "verified_by", "verified_on",
]  # fmt: skip


def t117_anchor(
    stmts: Statements, column: str, total: float
) -> tuple[str, float, list[float]] | None:
    """The later, smaller total-labelled row T-117 anchors on: ``(label, value, rows between)``."""
    spec = REGISTRY["revenue"]
    rows = list(stmts._rows_for(spec))
    winner = next(
        (
            i
            for i, r in enumerate(rows)
            if r.get("concept") in spec.total_concepts and numeric(r.get(column)) is not None
        ),
        None,
    )
    if winner is None:
        return None
    for i in range(winner + 1, len(rows)):
        label = str(rows[i].get("label") or "")
        later = numeric(rows[i].get(column))
        if not Statements._LABEL_TOTAL_RE.search(
            label
        ) or Statements._LABEL_TOTAL_EXCLUDE_RE.search(label):
            continue
        if (
            later is None
            or later <= 0
            or later >= total * Statements._LABEL_TOTAL_CONTRADICTION_RATIO
        ):
            continue
        between = [
            v for j in range(winner + 1, i) if (v := numeric(rows[j].get(column))) is not None
        ]
        return label, later, between
    return None


def derivations(stmts: Statements) -> list[dict[str, Any]]:
    """Every income-statement duration column of *stmts* whose revenue is derived (or refused)."""
    spec = REGISTRY["revenue"]
    out: list[dict[str, Any]] = []
    for p in (q for q in stmts.periods if not q.is_instant):
        total = stmts._first_total_match(spec, p.key)
        if total is None:
            rb = stmts.rebuild_total(spec, p.key)
            if rb is None:
                continue
            out.append(
                {
                    "p": p, "method": "T-140", "status": "derived" if rb.value is not None else "refused",
                    "resolver_value": stmts.get("revenue", p.key),
                    "value": rb.value, "anchor_label": rb.anchor_label, "anchor_value": rb.anchor_value,
                    "between": list(rb.between), "filed_total": None,
                    "guard": "gateway original agrees to the dollar" if rb.corroborated else "rows between <= 25% of the anchor",
                    "guard_pass": rb.value is not None,
                }
            )  # fmt: skip
            continue
        corrected, contradicted = stmts._label_total_correction(spec, p.key, total)
        if corrected is None and not contradicted:
            continue
        anchor = t117_anchor(stmts, p.key, total)
        label, value, between = anchor if anchor else ("", None, [])
        out.append(
            {
                "p": p, "method": "T-117", "status": "derived" if corrected is not None else "refused",
                "resolver_value": stmts.get("revenue", p.key),
                "value": corrected, "anchor_label": label, "anchor_value": value, "between": between,
                "filed_total": total, "guard": "rows between <= 25% of the anchor",
                "guard_pass": corrected is not None,
            }
        )  # fmt: skip
    return out


def period_class(tag: str) -> str:
    return "FY" if tag == "FY" else "YTD" if tag == "YTD" else "Q"


def later_value(
    later: list[dict[str, Any]], period_end: str, tag_class: str, after: str
) -> tuple[str, str, float] | None:
    """The first filing filed after *after* whose comparative column ends on *period_end* (same kind of period)."""
    for entry in later:
        if entry["filing_date"] <= after:
            continue
        for p in entry["stmts"].periods:
            if p.is_instant or p.date != period_end or period_class(p.tag) != tag_class:
                continue
            v = entry["stmts"].get("revenue", p.key)
            if v is not None:
                return entry["accession"], p.key, v
    return None


def build_register(conn: Any) -> list[dict[str, Any]]:
    """One row per derived (or refused) revenue value, with its ex-post comparison."""
    by_asset: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for f in filings(conn):
        by_asset[f["ticker"]].append(
            {"f": f, "stmts": statements_from_db(conn, f["id"]), "accession": f["accession_number"], "filing_date": f["filing_date"] or ""}
        )  # fmt: skip
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = collections.defaultdict(list)
    for ticker, entries in by_asset.items():
        for e in entries:
            for d in derivations(e["stmts"]):
                p = d["p"]
                kind = "own" if p.date == e["f"]["period_end"] else "comparative"
                key = (
                    e["f"]["cik"],
                    p.date,
                    period_class(p.tag),
                    d["method"],
                    round(d["value"] or 0),
                )
                groups[key].append({**d, "kind": kind, "entry": e, "ticker": ticker})
    rows: list[dict[str, Any]] = []
    for key, members in groups.items():
        rep = sorted(members, key=lambda m: (m["kind"] != "own", m["entry"]["filing_date"]))[0]
        e, p = rep["entry"], rep["p"]
        asset_entries = sorted(by_asset[rep["ticker"]], key=lambda x: x["filing_date"])
        post = (
            later_value(asset_entries, p.date, key[2], e["filing_date"])
            if rep["status"] == "derived"
            else None
        )
        diff = None if post is None or rep["value"] is None else post[2] - rep["value"]
        rows.append(
            {
                "ticker": rep["ticker"], "cik": e["f"]["cik"], "accession": e["accession"], "form": e["f"]["form"],
                "fiscal_period": e["f"]["fiscal_period"], "column_period_end": p.date, "column_tag": p.tag,
                "column_kind": rep["kind"], "method": rep["method"], "status": rep["status"],
                "derived_value": "" if rep["value"] is None else rep["value"],
                "resolver_value": "" if rep["resolver_value"] is None else rep["resolver_value"], "anchor_label": rep["anchor_label"],
                "anchor_value": "" if rep["anchor_value"] is None else rep["anchor_value"],
                "between_rows": ";".join(f"{v:.0f}" for v in rep["between"]), "guard": rep["guard"],
                "guard_pass": rep["guard_pass"], "filed_total": "" if rep["filed_total"] is None else rep["filed_total"],
                "also_derived_in_filings": len(members) - 1,
                "expost_accession": post[0] if post else "", "expost_column": post[1] if post else "",
                "expost_value": post[2] if post else "", "expost_difference": "" if diff is None else diff,
                "expost_difference_pct": "" if diff is None or not rep["value"] else f"{diff / rep['value']:.6f}",
                "expost_note": "comparative column of a later filing; may be restated" if post else "no later comparative column stored",
                "verified_by": "", "verified_on": "",
            }
        )  # fmt: skip
    return sorted(rows, key=lambda r: (r["ticker"], r["column_period_end"], r["method"]))


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def findings(rows: list[dict[str, Any]], filings_total: int) -> list[Finding]:
    return [*summary_findings(rows, filings_total), *per_company_findings(rows)]


def summary_findings(rows: list[dict[str, Any]], filings_total: int) -> list[Finding]:
    derived = [r for r in rows if r["status"] == "derived"]
    refused = [r for r in rows if r["status"] == "refused"]
    refused_nonempty = [r for r in refused if r["resolver_value"] != ""]
    checked = [r for r in derived if r["expost_difference"] != ""]
    off = [r for r in checked if abs(float(r["expost_difference"])) > 1.0]

    def ex(rs: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
        return spread(
            [
                (
                    r["ticker"],
                    r["accession"],
                    f"{r['column_tag']} {r['column_period_end']} {r['method']}",
                )
                for r in rs
            ]
        )

    return [
        Finding("L09.derived_values", ["L-09"], "Distinct revenue values derived by T-117/T-140", "b", len(derived), filings_total, len({r["ticker"] for r in derived}), unit="values (population: stored filings)", examples=ex(derived), command=CMD),
        Finding("L09.refused_values", ["L-09"], "Columns where the guard refused the derivation", "b", len(refused), filings_total, len({r["ticker"] for r in refused}), unit="values (population: stored filings)", examples=ex(refused), command=CMD),
        Finding("L09.refused_but_resolver_returns_a_value", ["L-09", "CON-11"], "Refused columns where the resolver still returns a revenue (the component sum), not an empty value", "b", len(refused_nonempty), len(refused), len({r["ticker"] for r in refused_nonempty}), unit="values", examples=ex(refused_nonempty), command=CMD, note="D-03 says a value that fails its guard is already left empty; for these it is not"),
        Finding("L09.expost_checked", ["L-09"], "Derived values with a later comparative column to check against", "b", len(checked), len(derived), len({r["ticker"] for r in checked}), unit="values", command=CMD),
        Finding("L09.expost_differs", ["L-09"], "...whose later comparative column differs by more than a dollar (may be restated)", "b", len(off), len(checked), len({r["ticker"] for r in off}), unit="values", examples=ex(off), command=CMD),
    ]  # fmt: skip


def per_company_findings(rows: list[dict[str, Any]]) -> list[Finding]:
    """Which companies T-140/T-117 derive revenue for, and for which of them the ex-post audit fails.

    ``model_fixes.md`` describes the rebuild as APA's; the register shows every company it fires for. A company whose
    later comparative column disagrees with the derived value has a wrong value in the data (the anchor is a net-revenue
    line, not the revenue total): the derived values of that company are left empty under D-03b."""
    derived = [r for r in rows if r["status"] == "derived"]
    by: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for r in derived:
        by[r["ticker"]].append(r)
    out = [
        Finding(
            "L09.derived_by_company", ["L-09"],
            "Companies for which T-117/T-140 derive a revenue value (model_fixes.md describes APA only)", "b",
            len(by), len(by), len(by), unit="companies", command=CMD,
            note="; ".join(f"{t} {len(v)}" for t, v in sorted(by.items())),
        )
    ]  # fmt: skip
    for ticker, values in sorted(by.items()):
        checked = [r for r in values if r["expost_difference"] != ""]
        bad = [r for r in checked if abs(float(r["expost_difference"])) > 1.0]
        if not bad:
            continue
        out.append(
            Finding(
                f"L09.expost_fails.{ticker}", ["L-09", "ID-02b"],
                f"{ticker}: derived revenue values whose later comparative column disagrees (ex-post audit failed)", "b",
                len(values), len(values), 1, unit="derived values (all rest on the same anchor)",
                examples=[(ticker, r["accession"], f"{r['column_tag']} {r['column_period_end']} derived {float(r['derived_value']) / 1e6:,.0f}M vs {float(r['expost_value']) / 1e6:,.0f}M ({float(r['expost_difference_pct']) * 100:+.0f}%)") for r in bad[:5]],
                command=CMD,
                note=f"{len(bad)} of {len(checked)} checkable values differ; anchors: {sorted({r['anchor_label'] for r in values})}",
            )
        )  # fmt: skip
    return out
