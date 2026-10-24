#!/usr/bin/env python3
"""A3 vs A2: the metric and item changes between pilot_a2 and pilot_a3 (T-143 part A).

    uv run --no-sync python scripts/pilot_a/a3_vs_a2.py \
        --a2-db /workspaces/thesis/pilot_a2/financial.db \
        --a3-db /home/vscode/pilot_a3_run/financial.db \
        --companyfacts-dir /workspaces/thesis/scratch/t143a/sec_cache/companyfacts \
        --out-metrics out/a3_vs_a2_metrics.csv \
        --out-items out/a3_vs_a2_items.csv \
        --out-summary out/a3_vs_a2_summary.md

Read-only on both databases.  One metric row per ``(accession, metric_group, metric_name)`` present
in either database, with the value and the empty reason on each side and the class:

``same`` / ``fill`` / ``emptied`` / ``changed`` / ``reason_only`` / ``only_a2`` / ``only_a3``.

The item table is built from each metric row's recorded provenance (``inputs_json.provenance``):
one row per ``(accession, item)`` whose value, concept or ``how`` differs.  The item value is the
recorded top-level input when the row carries it; otherwise it is rebuilt from the filing's stored
facts at the provenance period (a ``+`` concept is a sum of its parts).  The SEC companyfacts value
is then read from the local cache for the item's concept and period, so a difference can be checked
against the SEC's own number.  Counts and values only; never a price.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import math
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from kg_schema.db import connect_ro

PRICE_GROUP = "valuation"
CLASSES = ("same", "fill", "emptied", "changed", "reason_only", "only_a2", "only_a3")
METRIC_COLUMNS = (
    "ticker",
    "accession",
    "form",
    "period_end",
    "metric_group",
    "metric_name",
    "value_a2",
    "value_a3",
    "empty_reason_a2",
    "empty_reason_a3",
    "class",
)
ITEM_COLUMNS = (
    "ticker",
    "accession",
    "form",
    "period_end",
    "item",
    "value_a2",
    "value_a3",
    "concept_a2",
    "concept_a3",
    "how_a2",
    "how_a3",
    "flags_a3",
    "sec_value_a2",
    "sec_value_a3",
)


def _number(value: Any) -> float | None:
    """A recorded number, or None; bool is not a number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isfinite(number):
            return number
    return None


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _empty_reason(inputs: dict[str, Any], value: Any) -> str:
    if value is not None:
        return ""
    provenance = inputs.get("provenance") or {}
    return str(provenance.get("empty_reason") or "")


def _filings(db: str) -> dict[str, dict[str, Any]]:
    with contextlib.closing(connect_ro(db)) as conn:
        rows = conn.execute(
            "SELECT f.accession_number, f.form, f.period_end, f.cik, f.id AS filing_id, "
            "a.ticker FROM sec_filings f LEFT JOIN assets a ON a.id = f.asset_id"
        ).fetchall()
    return {
        str(row["accession_number"]): {
            "ticker": str(row["ticker"] or ""),
            "form": str(row["form"] or ""),
            "period_end": str(row["period_end"] or ""),
            "cik": str(row["cik"] or ""),
            "filing_id": int(row["filing_id"]),
        }
        for row in rows
    }


def _metrics(db: str) -> dict[tuple[str, str, str], dict[str, Any]]:
    with contextlib.closing(connect_ro(db)) as conn:
        rows = conn.execute(
            "SELECT f.accession_number, m.metric_group, m.metric_name, m.value, m.inputs_json "
            "FROM fundamental_metrics m JOIN sec_filings f ON f.id = m.filing_id"
        ).fetchall()
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        inputs = _json(row["inputs_json"])
        value = _number(row["value"])
        out[(str(row["accession_number"]), str(row["metric_group"]), str(row["metric_name"]))] = {
            "value": value,
            "reason": _empty_reason(inputs, value),
            "inputs": inputs,
        }
    return out


def _period_date(period: Any) -> str:
    return str(period or "").split(" ", 1)[0]


def _item_records(
    metrics: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """One record per (accession, item), preferring the provenance's period basis."""
    out: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for (accession, _group, _name), record in metrics.items():
        inputs = record["inputs"]
        provenance = inputs.get("provenance") or {}
        period = provenance.get("period")
        for item, entries in (provenance.get("items") or {}).items():
            if not isinstance(entries, list):
                continue
            chosen = [entry for entry in entries if entry.get("basis") == "period"]
            if not chosen:
                continue
            for entry in chosen:
                out[(accession, str(item))].append(
                    {
                        "value": _number(inputs.get(item)),
                        "concept": str(entry.get("concept") or ""),
                        "how": str(entry.get("how") or ""),
                        "flags": tuple(str(flag) for flag in entry.get("flags") or []),
                        "period": str(period or ""),
                    }
                )
    return out


def _dedupe(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not records:
        return None
    values = [record for record in records if record["value"] is not None]
    chosen = values[0] if values else records[0]
    concepts = sorted({record["concept"] for record in records if record["concept"]})
    hows = sorted({record["how"] for record in records if record["how"]})
    flags = tuple(sorted({flag for record in records for flag in record["flags"]}))
    value = chosen["value"]
    if len({record["value"] for record in values}) > 1:
        value = None  # inconsistent across metrics: leave the value opened in the CSV
    return {
        "value": value,
        "concepts": concepts,
        "hows": hows,
        "flags": flags,
        "period": chosen["period"],
    }


def _facts_for(conn: Any, filing_id: int) -> dict[tuple[str, str], float]:
    rows = conn.execute(
        "SELECT period_key, concept, value FROM financial_facts WHERE filing_id = ?",
        (filing_id,),
    ).fetchall()
    out: dict[tuple[str, str], float] = {}
    for row in rows:
        value = _number(row["value"])
        if value is not None:
            out[(str(row["period_key"] or ""), str(row["concept"] or ""))] = value
    return out


def _value_from_facts(
    facts: dict[tuple[str, str], float], record: dict[str, Any] | None
) -> float | None:
    if record is None or record["value"] is not None:
        return None if record is None else record["value"]
    concepts = record["concepts"]
    if len(concepts) != 1:
        return None
    parts = concepts[0].split(" + ")
    period = _period_date(record["period"])
    values = [
        facts.get((period, part))
        if (period, part) in facts
        else facts.get((record["period"], part))
        for part in parts
    ]
    if any(value is None for value in values):
        return None
    return float(sum(value for value in values if value is not None))


@lru_cache(maxsize=32)
def _companyfacts(path: str) -> dict[str, Any]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def _sec_values(
    companyfacts_dir: str, cik: str, accession: str, period: str, concepts: list[str]
) -> list[str]:
    """The SEC's values for one concept (a ``+`` sum is rebuilt) at the accession and period."""
    if not concepts or not cik or not period:
        return []
    path = str(Path(companyfacts_dir) / f"CIK{int(cik):010d}.json") if cik.isdigit() else ""
    document = _companyfacts(path) if path else {}
    facts = document.get("facts") or {}
    out: list[str] = []
    for concept in concepts:
        parts = concept.split(" + ")
        values: list[float] = []
        missing = False
        for part in parts:
            taxonomy, _, tag = part.partition("_")
            units = ((facts.get(taxonomy) or {}).get(tag) or {}).get("units") or {}
            found: list[float] = []
            for entries in units.values():
                for entry in entries or []:
                    if str(entry.get("accn")) != accession or str(entry.get("end")) != period:
                        continue
                    value = _number(entry.get("val"))
                    if value is not None:
                        found.append(value)
            unique = sorted(set(found))
            if len(unique) != 1:
                missing = True
                break
            values.append(unique[0])
        if missing:
            continue
        out.append(_format(sum(values)))
    return out


def _format(value: float | None) -> str:
    return "" if value is None else repr(float(value))


def _join(values: list[str]) -> str:
    return "|".join(values)


def _same_value(first: float | None, second: float | None) -> bool:
    if first is None or second is None:
        return first is None and second is None
    return first == second or abs(first - second) <= 1e-12 * max(1.0, abs(first), abs(second))


def _classify(first: dict[str, Any] | None, second: dict[str, Any] | None) -> str:
    if first is None:
        return "only_a3"
    if second is None:
        return "only_a2"
    first_value, second_value = first["value"], second["value"]
    if first_value is None and second_value is None:
        return "same" if first["reason"] == second["reason"] else "reason_only"
    if first_value is None:
        return "fill"
    if second_value is None:
        return "emptied"
    return "same" if _same_value(first_value, second_value) else "changed"


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _empty_causes(
    metrics_a2: dict[tuple[str, str, str], dict[str, Any]],
    metrics_a3: dict[tuple[str, str, str], dict[str, Any]],
    shared: set[str],
) -> dict[str, Counter[tuple[str, str]]]:
    common = {key for key in metrics_a2.keys() & metrics_a3.keys() if key[0] in shared}
    out: dict[str, Counter[tuple[str, str]]] = {
        "price": Counter(),
        "nonprice": Counter(),
    }
    for key in common:
        group = key[1]
        bucket = "price" if group == PRICE_GROUP else "nonprice"
        for side, metrics in (("a2", metrics_a2), ("a3", metrics_a3)):
            value = metrics[key]["value"]
            if value is None:
                out[bucket][(side, metrics[key]["reason"] or "(none)")] += 1
    return out


def _summary(  # noqa: PLR0913, PLR0917 - the report needs both sides and the counts
    path: Path,
    metrics_a2: dict[tuple[str, str, str], dict[str, Any]],
    metrics_a3: dict[tuple[str, str, str], dict[str, Any]],
    filings_a2: dict[str, dict[str, Any]],
    filings_a3: dict[str, dict[str, Any]],
    classes: Counter[str],
    items: int,
) -> None:
    shared = set(filings_a2) & set(filings_a3)
    only_a2 = sorted(set(filings_a2) - set(filings_a3))
    only_a3 = sorted(set(filings_a3) - set(filings_a2))
    causes = _empty_causes(metrics_a2, metrics_a3, shared)
    lines = [
        "# A3 vs A2 — summary",
        "",
        f"* metric rows in a2: {len(metrics_a2)}",
        f"* metric rows in a3: {len(metrics_a3)}",
        f"* filings in a2: {len(filings_a2)}; in a3: {len(filings_a3)}; shared: {len(shared)}",
        "",
        "## Metric classes",
        "",
        "| class | rows |",
        "|---|---:|",
    ]
    for name in CLASSES:
        lines.append(f"| `{name}` | {classes[name]} |")
    lines.append(f"| **total** | **{sum(classes.values())}** |")
    lines += [
        "",
        "## Empty values by cause on the shared filings",
        "",
        "Common metric rows only; `(none)` is an empty value with no recorded reason.",
        "",
        "| row | side | cause | count |",
        "|---|---|---|---:|",
    ]
    for bucket in ("nonprice", "price"):
        for (side, cause), count in causes[bucket].most_common():
            lines.append(f"| {bucket} | {side} | `{cause}` | {count} |")
    lines += [
        "",
        f"## Filings only in a2 ({len(only_a2)})",
        "",
    ]
    lines += [
        f"* `{accession}` {filings_a2[accession]['ticker']} {filings_a2[accession]['form']} "
        f"{filings_a2[accession]['period_end']}"
        for accession in only_a2
    ] or ["* none"]
    lines += ["", f"## Filings only in a3 ({len(only_a3)})", ""]
    lines += [
        f"* `{accession}` {filings_a3[accession]['ticker']} {filings_a3[accession]['form']} "
        f"{filings_a3[accession]['period_end']}"
        for accession in only_a3
    ] or ["* none"]
    lines += ["", f"## Item rows whose value, concept or how differs: {items}", ""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:  # noqa: C901, PLR0915 - one pass over the two databases, then the three outputs
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a2-db", required=True)
    parser.add_argument("--a3-db", required=True)
    parser.add_argument("--companyfacts-dir", required=True)
    parser.add_argument("--out-metrics", required=True)
    parser.add_argument("--out-items", required=True)
    parser.add_argument("--out-summary", required=True)
    args = parser.parse_args()

    filings_a2 = _filings(args.a2_db)
    filings_a3 = _filings(args.a3_db)
    metrics_a2 = _metrics(args.a2_db)
    metrics_a3 = _metrics(args.a3_db)
    shared = set(filings_a2) & set(filings_a3)

    classes: Counter[str] = Counter()
    metric_rows: list[dict[str, Any]] = []
    for key in sorted(set(metrics_a2) | set(metrics_a3)):
        accession, group, name = key
        first, second = metrics_a2.get(key), metrics_a3.get(key)
        classification = _classify(first, second)
        classes[classification] += 1
        filing = filings_a3.get(accession) or filings_a2[accession]
        metric_rows.append(
            {
                "ticker": filing["ticker"],
                "accession": accession,
                "form": filing["form"],
                "period_end": filing["period_end"],
                "metric_group": group,
                "metric_name": name,
                "value_a2": _format(None if first is None else first["value"]),
                "value_a3": _format(None if second is None else second["value"]),
                "empty_reason_a2": "" if first is None else first["reason"],
                "empty_reason_a3": "" if second is None else second["reason"],
                "class": classification,
            }
        )

    items_a2 = _item_records(metrics_a2)
    items_a3 = _item_records(metrics_a3)
    by_accession: dict[str, list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]]] = (
        defaultdict(list)
    )
    for accession, item in sorted(set(items_a2) | set(items_a3)):
        if accession not in shared:
            continue
        first = _dedupe(items_a2.get((accession, item), []))
        second = _dedupe(items_a3.get((accession, item), []))
        if first is None and second is None:
            continue
        by_accession[accession].append((item, first, second))

    item_rows: list[dict[str, Any]] = []
    for accession, entries in sorted(by_accession.items()):
        needs_a2 = any(first is not None and first["value"] is None for _, first, _ in entries)
        needs_a3 = any(second is not None and second["value"] is None for _, _, second in entries)
        facts_a2: dict[tuple[str, str], float] = {}
        facts_a3: dict[tuple[str, str], float] = {}
        if needs_a2:
            with contextlib.closing(connect_ro(args.a2_db)) as conn_a2:
                facts_a2 = _facts_for(conn_a2, filings_a2[accession]["filing_id"])
        if needs_a3:
            with contextlib.closing(connect_ro(args.a3_db)) as conn_a3:
                facts_a3 = _facts_for(conn_a3, filings_a3[accession]["filing_id"])
        for item, first, second in entries:
            first_value = None if first is None else first["value"]
            second_value = None if second is None else second["value"]
            if first is not None and first_value is None:
                first_value = _value_from_facts(facts_a2, first)
            if second is not None and second_value is None:
                second_value = _value_from_facts(facts_a3, second)
            first_concepts = [] if first is None else first["concepts"]
            second_concepts = [] if second is None else second["concepts"]
            first_hows = [] if first is None else first["hows"]
            second_hows = [] if second is None else second["hows"]
            if (
                _same_value(first_value, second_value)
                and first_concepts == second_concepts
                and first_hows == second_hows
            ):
                continue
            filing = filings_a3[accession]
            sec_a2 = (
                _sec_values(
                    args.companyfacts_dir,
                    filing["cik"],
                    accession,
                    _period_date(first["period"]),
                    first_concepts,
                )
                if first is not None
                else []
            )
            sec_a3 = (
                _sec_values(
                    args.companyfacts_dir,
                    filing["cik"],
                    accession,
                    _period_date(second["period"]),
                    second_concepts,
                )
                if second is not None
                else []
            )
            item_rows.append(
                {
                    "ticker": filing["ticker"],
                    "accession": accession,
                    "form": filing["form"],
                    "period_end": filing["period_end"],
                    "item": item,
                    "value_a2": _format(first_value),
                    "value_a3": _format(second_value),
                    "concept_a2": "|".join(first_concepts),
                    "concept_a3": "|".join(second_concepts),
                    "how_a2": "|".join(first_hows),
                    "how_a3": "|".join(second_hows),
                    "flags_a3": "" if second is None else "|".join(second["flags"]),
                    "sec_value_a2": _join(sec_a2),
                    "sec_value_a3": _join(sec_a3),
                }
            )

    _write_csv(Path(args.out_metrics), METRIC_COLUMNS, metric_rows)
    _write_csv(Path(args.out_items), ITEM_COLUMNS, item_rows)
    _summary(
        Path(args.out_summary),
        metrics_a2,
        metrics_a3,
        filings_a2,
        filings_a3,
        classes,
        len(item_rows),
    )
    print(
        json.dumps(
            {
                "metrics": dict(classes),
                "metric_rows": len(metric_rows),
                "item_rows": len(item_rows),
                "filings_only_a2": len(set(filings_a2) - set(filings_a3)),
                "filings_only_a3": len(set(filings_a3) - set(filings_a2)),
            },
            indent=1,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
