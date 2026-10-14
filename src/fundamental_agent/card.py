"""The company card (T-151 part 3, D15; fix round 1, G3).

``python -m fundamental_agent card --db PATH --cik CIK --date YYYY-MM-DD [--metrics-version V]
[--out FILE]`` prints, for one company and one as-of date, read-only (``mode=ro``):

* the profile -- ticker, sector, sub-industry, REIT yes/no, negative equity yes/no, and (T-071 part 1)
  the APP-00 profile: the base company type, its overlays and the evidence accession;
* the filing used as of the date -- the latest one whose availability date (PER-01) is on or before
  the date;
* each line item as the **stored** provenance records it (concept, how it was read, basis);
* each metric as it is **stored** (value, flags, rule IDs, provenance) for the engine version;
* the empty metrics with their reason.

G3: the card **never recomputes**. It reads the ``fundamental_metrics`` rows the cycle reads. A row
without a ``provenance`` block prints ``provenance: not stored (recompute needed)``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from kg_schema.company_profile import CompanyProfile, profile_as_of
from kg_schema.versions import VERSION_FILTER_SQL, resolve_metric_versions

# The stored-row reader, filtered by the per-group versions a consumer resolves (T-090): the same
# filter as ``kg_schema.versions.VERSION_FILTER_SQL``, written out so the source carries it
# (tests/test_metric_versions.py fails on any reader that lacks it).
_STORED_SQL = (
    "SELECT m.metric_group, m.metric_name, m.value, m.unit, m.inputs_json "
    "FROM fundamental_metrics m WHERE m.filing_id = ? "
    "AND (m.metric_group || '/' || m.engine_version) IN (SELECT value FROM json_each(?)) "
    "ORDER BY m.metric_group, m.metric_name"
)
assert VERSION_FILTER_SQL in _STORED_SQL  # the static fragment is the one every reader applies


@dataclass(frozen=True)
class Card:
    """One company's card: the profile, the filing used and its stored line items and metrics."""

    ticker: str
    cik: str
    company_name: str
    sector: str | None
    sub_industry: str | None
    is_reit: bool
    negative_equity: bool
    as_of: str
    metrics_version: str
    filing: dict[str, Any]
    # T-071 part 1 (P4): the profile the code reads (APP-00) -- the base type, its overlays and the
    # evidence accession; ``None`` when the CIK has no profile.
    company_type: str | None = None
    overlays: tuple[str, ...] = ()
    evidence_accession: str | None = None
    line_items: list[dict[str, Any]] = field(default_factory=list)
    metrics: list[dict[str, Any]] = field(default_factory=list)
    empty: list[dict[str, Any]] = field(default_factory=list)


def _asset_row(conn: Any, cik: str) -> Any:
    return conn.execute(
        "SELECT a.id, a.ticker, a.company_name, a.cik, s.name AS sector, a.sub_industry "
        "FROM assets a LEFT JOIN sectors s ON a.sector_id = s.id WHERE a.cik = ?",
        (cik,),
    ).fetchone()


def _filing_as_of(conn: Any, asset_id: int, as_of: str) -> Any:
    """The latest filing available on or before *as_of* (PER-01: ``available_at`` is the first
    trading day after the filing date)."""
    return conn.execute(
        "SELECT id, form, fiscal_year, fiscal_period, period_end, filing_date, accession_number, "
        "available_at FROM sec_filings WHERE asset_id = ? AND available_at IS NOT NULL "
        "AND available_at <= ? ORDER BY available_at DESC, period_end DESC LIMIT 1",
        (asset_id, as_of),
    ).fetchone()


def _stored_rows(conn: Any, filing_id: int, metrics_version: str | None) -> tuple[list[Any], str]:
    """The filing's stored rows for the resolved engine versions, and the manifest tag they name."""
    versions = resolve_metric_versions(conn, metrics_version)
    rows = list(conn.execute(_STORED_SQL, (filing_id, versions.json_param())))
    return rows, (metrics_version or ",".join(f"{g}={v}" for g, v in versions.manifest().items()))


def _line_items(metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The line items the stored provenance records, one row per (item, basis)."""
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for metric in metrics:
        block = metric.get("provenance") or {}
        for item, entries in (block.get("items") or {}).items():
            for entry in entries:
                seen.setdefault(
                    (item, str(entry.get("basis"))),
                    {
                        "item": item,
                        "basis": entry.get("basis"),
                        "concept": entry.get("concept"),
                        "how": entry.get("how"),
                        "source": entry.get("source"),
                        "method": entry.get("method"),
                        "flags": entry.get("flags") or [],
                    },
                )
    return [seen[key] for key in sorted(seen)]


def _negative_equity(metrics: list[dict[str, Any]]) -> bool:
    """The stored flag (APP-07's quarantine), not a recomputation."""
    for metric in metrics:
        flags = (metric.get("provenance") or {}).get("flags") or []
        if any("negative_equity" in flag for flag in flags):
            return True
        if metric["name"] == "debt_to_equity" and metric.get("inputs", {}).get(
            "flag_debt_to_equity_negative_equity"
        ):
            return True
    return False


def _overlays(profile: CompanyProfile | None) -> tuple[str, ...]:
    """The profile's overlays as the card prints them (T-071 part 1, P4)."""
    if profile is None:
        return ()
    names: list[str] = []
    if profile.reit:
        names.append("reit")
    if profile.multi_class:
        names.append("multi-class")
    if profile.regulated_utility:
        names.append("regulated utility")
    if profile.multi_sector:
        names.append("multi-sector")
    return tuple(names)


def build_card(conn: Any, cik: str, as_of: str, *, metrics_version: str | None = None) -> Card:
    """Build one company's card from the **stored** metrics (read-only; never recomputes)."""
    asset = _asset_row(conn, cik)
    if asset is None:
        raise SystemExit(f"no asset with CIK {cik}")
    asset_id = int(asset["id"])
    filing = _filing_as_of(conn, asset_id, as_of)
    if filing is None:
        raise SystemExit(f"no filing of CIK {cik} available on or before {as_of}")

    rows, version = _stored_rows(conn, int(filing["id"]), metrics_version)
    profile = profile_as_of(str(asset["cik"]), as_of) if asset["cik"] else None
    metrics: list[dict[str, Any]] = []
    empty: list[dict[str, Any]] = []
    all_entries: list[dict[str, Any]] = []
    for row in rows:
        inputs = json.loads(row["inputs_json"] or "{}")
        block = inputs.get("provenance")
        entry = {
            "group": row["metric_group"],
            "name": row["metric_name"],
            "value": row["value"],
            "unit": row["unit"],
            "rule_ids": (block or {}).get("rule_ids", []),
            "flags": (block or {}).get("flags", []),
            "provenance": block,
            "inputs": inputs,
        }
        all_entries.append(entry)
        if row["value"] is None:
            empty.append(
                {
                    "name": row["metric_name"],
                    "reason": (block or {}).get("empty_reason"),
                    "stored": block is not None,
                    "items": (block or {}).get("items", {}),
                }
            )
        else:
            metrics.append(entry)

    return Card(
        ticker=str(asset["ticker"]),
        cik=cik,
        company_name=str(asset["company_name"] or asset["ticker"]),
        sector=asset["sector"],
        sub_industry=asset["sub_industry"],
        is_reit=bool(profile and profile.reit),
        negative_equity=_negative_equity(all_entries),
        as_of=as_of,
        metrics_version=version,
        company_type=profile.type if profile else None,
        overlays=_overlays(profile),
        evidence_accession=profile.evidence_accession if profile else None,
        filing={
            "form": str(filing["form"]),
            "fiscal_period": str(filing["fiscal_period"]),
            "accession": str(filing["accession_number"] or ""),
            "period_end": str(filing["period_end"]),
            "available_at": str(filing["available_at"] or ""),
        },
        line_items=_line_items(metrics + empty),
        metrics=metrics,
        empty=empty,
    )


def render(card: Card) -> str:
    """The card as text."""
    lines = [
        f"Company card — {card.ticker} ({card.company_name}, CIK {card.cik}) as of {card.as_of}",
        f"Profile: sector {card.sector} | sub-industry {card.sub_industry} | REIT "
        f"{'yes' if card.is_reit else 'no'} | negative equity "
        f"{'yes' if card.negative_equity else 'no'}",
        f"Profile (APP-00): type {card.company_type or 'unknown'} | overlays "
        f"{', '.join(card.overlays) or 'none'} | evidence {card.evidence_accession or '-'}",
        f"Filing: {card.filing['form']} {card.filing['fiscal_period']} "
        f"{card.filing['accession']} (period end {card.filing['period_end']}, "
        f"available {card.filing['available_at']})",
        f"Stored metrics: {card.metrics_version}",
        "",
        "Line items (as the stored provenance records them):",
    ]
    for item in card.line_items:
        extra = item["source"] or item["method"] or ""
        flags = f"  flags: {', '.join(item['flags'])}" if item["flags"] else ""
        lines.append(
            f"  {item['item']:<28}{item['basis']:<14}{item['concept'] or '-':<60}"
            f"[{item['how'] or '-'}] {extra}{flags}"
        )
    lines += ["", "Metrics (stored):"]
    for metric in card.metrics:
        if metric["provenance"] is None:
            lines.append(
                f"  {metric['group'] + '.' + metric['name']:<44}{metric['value']:>16.6f}  "
                f"{metric['unit']:<6} provenance: not stored (recompute needed)"
            )
            continue
        rules = ", ".join(metric["rule_ids"]) or "-"
        flags = ", ".join(metric["flags"]) or "-"
        lines.append(
            f"  {metric['group'] + '.' + metric['name']:<44}{metric['value']:>16.6f}  "
            f"{metric['unit']:<6} rules: {rules}  flags: {flags}"
        )
    lines += ["", "Empty metrics (with the reason):"]
    for entry in card.empty:
        if not entry["stored"]:
            lines.append(f"  {entry['name']:<44}provenance: not stored (recompute needed)")
            continue
        concepts = ", ".join(
            f"{item}[{info.get('basis')}]={info.get('concept') or '-'}"
            for item, entries in entry["items"].items()
            for info in entries
        )
        lines.append(f"  {entry['name']:<44}{entry['reason'] or '-':<26}{concepts}")
    return "\n".join(lines) + "\n"


__all__ = ["Card", "build_card", "render"]
