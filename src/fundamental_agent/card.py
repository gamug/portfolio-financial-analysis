"""The company card (T-151 part 3, D15; fix round 1, G3; T-143 part A, V5).

``python -m fundamental_agent card --db PATH --cik CIK --date YYYY-MM-DD [--metrics-version V]
[--out FILE] [--edgar]`` prints, for one company and one as-of date, read-only (``mode=ro``):

* the profile -- ticker, sector, sub-industry, REIT yes/no, negative equity yes/no, and (T-071 part 1)
  the APP-00 profile: the base company type, its overlays and the evidence accession;
* the filing used as of the date -- the latest one whose availability date (PER-01) is on or before
  the date -- with **its EDGAR link** (the filing index always, the primary document's URL with
  ``--edgar``, which reads the filing's index and never the document);
* each line item as the **stored** provenance records it (concept, how it was read, basis) **with
  its value** (the period / prior year / TTM value as the metric recorded it), **its accession** and
  the EDGAR link of the filing the value comes from;
* each metric as it is **stored** (value, flags, rule IDs, provenance) for the engine version;
* the empty metrics with their reason;
* **the vetoes that apply on the card's date**, evaluated exactly as the cycle's veto step
  evaluates them (the cycle's readers, the cycle's own rules).

G3: the card **never recomputes**. It reads the ``fundamental_metrics`` rows the cycle reads. A row
without a ``provenance`` block prints ``provenance: not stored (recompute needed)``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from kg_schema.company_profile import CompanyProfile, profile_as_of
from kg_schema.market_cap import cover_total
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
    # The rules that hit this company on the card's date (the cycle's veto step, as in V4's
    # system side): the rule id, its severity and the evidence the rule read.
    vetoes: list[dict[str, Any]] = field(default_factory=list)
    # The filing's own cover-page share counts (MKT-01): each class with its date. They are what
    # a person reads on the 10-K's cover, whatever the market cap ended up using.
    cover_shares: list[dict[str, Any]] = field(default_factory=list)
    # The count the market cap rested on and the guard's verdict on this filing's cover count:
    # which filing supplied the count, how old it was, and whether this filing's own cover count
    # was the one used (PKG's FY2025 cover count is stored x1,000; the cap rested on the prior
    # filing's count instead).
    market_cap_count: dict[str, Any] = field(default_factory=dict)


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


def _input_value(
    inputs: dict[str, Any], item: str, basis: str | None, entry: dict[str, Any], bases: list[str]
) -> Any:
    """The value the metric recorded for *item* on *basis* (the card never recomputes).

    The metric's inputs carry the resolved values under their own names.  Which name depends on
    what the metric read:

    * a metric that reads the item on **one** basis stores it under the item's own name (the
      ``prior_year`` value of ``equity`` in ``return_on_equity`` is its ``equity``);
    * a metric that reads the item on **two** bases stores the period value under the item's name
      and the prior-year one under ``prior_<item>`` (``return_on_assets``);
    * a TTM value is the provenance entry's own ``value`` (the TTM's value, with the columns it
      combined) or, when the metric reads the item on no other basis, the item's own name.
    """
    if "value" in entry:  # the TTM's own value (the provenance records it)
        return entry["value"]
    if basis in ("prior_year", "prior_quarter"):
        prior = inputs.get(f"prior_{item}")
        if prior is not None:
            return prior
        return inputs.get(item) if len(bases) == 1 else None
    if basis == "ttm":
        return inputs.get(f"{item}_ttm", inputs.get(item))
    return inputs.get(item)


def _cover_shares(conn: Any, filing_id: int, cik: str | None) -> list[dict[str, Any]]:
    """The filing's cover-page share counts: each class, its count and the date it was taken, and
    the total the pipeline reads (``kg_schema.market_cap.cover_total``: the filer's own total when
    it filed one, else its classes summed, with the non-common members excluded per MKT-04)."""
    entries = [
        (str(row["class_member"] or ""), float(row["value"] or 0.0), str(row["as_of_date"] or ""))
        for row in conn.execute(
            "SELECT class_member, value, as_of_date FROM filing_cover_shares WHERE filing_id = ? "
            "ORDER BY class_member",
            (filing_id,),
        )
    ]
    total = cover_total(entries, cik=cik) if entries else None
    return [
        {
            "class_member": member,
            "value": value,
            "as_of_date": as_of,
            "total": None if total is None else total.value,
            "total_as_of": None if total is None else total.as_of_date,
            "classes": None if total is None else total.n_classes,
            "excluded_members": None if total is None else total.n_excluded,
        }
        for member, value, as_of in entries
    ]


def _stored_fact(
    conn: Any, filing_id: int, concept: str | None, period_key: str | None
) -> float | None:
    """The filing's own stored value for a concept on its own column (read-only, no recomputation).

    The metric's recorded value is the card's first source; where no metric recorded the item on
    the filing's own period column, the filed fact itself is the value (the card never recomputes
    anything, it reads what is stored).
    """
    if not concept or not period_key:
        return None
    row = conn.execute(
        "SELECT value FROM financial_facts WHERE filing_id = ? AND concept = ? AND period_key = ? "
        "ORDER BY id LIMIT 1",
        (filing_id, concept, period_key),
    ).fetchone()
    if row is not None:
        value = row["value"]
        return None if value is None else float(value)
    # an instant's stored period key is the date alone ("2026-06-30"), a duration's carries its
    # tag ("2026-06-30 (FY)"): the filing's own column is the one that starts with the date.
    row = conn.execute(
        "SELECT value FROM financial_facts WHERE filing_id = ? AND concept = ? "
        "AND period_key LIKE ? || '%' ORDER BY id LIMIT 1",
        (filing_id, concept, period_key[:10]),
    ).fetchone()
    if row is None:
        return None
    value = row["value"]
    return None if value is None else float(value)


#: A mapped item is a sum of at least this many concepts (a map of one is the fact itself).
MIN_MAPPED_CONCEPTS = 1


def _stored_sum(
    conn: Any, filing_id: int, concept: str | None, period_key: str | None
) -> float | None:
    """The sum of a **mapped** item's concepts on the filing's own column.

    A ``[map]`` resolution is a sum of several concepts (T-151's debt and D&A sums): the card
    prints the sum of the filed values, which is what the metric recorded, rather than a dash.
    """
    if not concept or not period_key:
        return None
    parts = [part.strip() for part in concept.split("+") if part.strip()]
    if len(parts) < MIN_MAPPED_CONCEPTS:
        return None
    values = [_stored_fact(conn, filing_id, part, period_key) for part in parts]
    if any(value is None for value in values):
        return None
    return float(sum(value for value in values if value is not None))


def _market_cap_count(
    conn: Any, asset_id: int, cover_total: float | None, inputs: dict[str, Any] | None
) -> dict[str, Any]:
    """The count the market cap rested on and the guard's verdict on this filing's cover count.

    The count is the one the stored ``market_capitalization`` metric used (the cap's own input ✓);
    the filing it came from is the filing whose cover count it is ✓.  The verdict says whether
    this filing's own cover count was the one used ✓ (PKG's FY2025 cover count is stored x1,000 ✓
    and the cap rested on the prior filing's count ✓).
    """
    inputs = inputs or {}
    used = inputs.get("shares")
    age = inputs.get("share_age_days")
    out: dict[str, Any] = {
        "shares": used,
        "share_age_days": age,
        "filing_id": None,
        "accession": None,
        "verdict": "no market cap is stored for this filing",
    }
    if used is None:
        return out
    rows = conn.execute(
        "SELECT sf.id, sf.accession_number, c.value, c.as_of_date FROM filing_cover_shares c "
        "JOIN sec_filings sf ON sf.id = c.filing_id JOIN assets a ON a.id = sf.asset_id "
        "WHERE a.id = ? AND c.class_member = '' ORDER BY c.as_of_date DESC",
        (asset_id,),
    ).fetchall()
    for row in rows:
        if abs(float(row["value"]) - float(used)) < 1.0:
            out["filing_id"] = int(row["id"])
            out["accession"] = str(row["accession_number"])
            break
    if cover_total is not None and abs(float(cover_total) - float(used)) < 1.0:
        out["verdict"] = "this filing's own cover count is the one the cap rests on"
    else:
        out["verdict"] = (
            f"this filing's own cover count ({cover_total if cover_total is not None else '-'}) "
            f"was not used; the cap rests on {used}"
            + (f" (filing {out['accession']})" if out["accession"] else "")
            + (f", {age} days old" if age is not None else "")
        )
    return out


def _line_items(
    metrics: list[dict[str, Any]],
    edgar_url: str | None = None,
    *,
    is_annual: bool = False,
    fact_value: Any = None,
) -> list[dict[str, Any]]:
    """The line items the stored provenance records, one row per (item, basis).

    Each row carries the **value** the metric recorded for it, the **accession** of the filing it
    was read from and the EDGAR link of that filing's document (V5: a person must be able to open
    the 10-K and check the number).

    On a **10-K** the period column and the TTM column are the same column (MET-03: a 10-K's TTM
    is its own fiscal year), so a value one metric recorded on one of the two bases is shown on the
    other as well -- the card never recomputes it, it shows what was recorded.
    """
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for metric in metrics:
        block = metric.get("provenance") or {}
        inputs = metric.get("inputs") or {}
        accession = block.get("accession")
        for item, entries in (block.get("items") or {}).items():
            bases = [str(entry.get("basis")) for entry in entries]
            for entry in entries:
                basis = str(entry.get("basis"))
                key = (item, basis)
                row = seen.get(key)
                if row is None:
                    row = {
                        "item": item,
                        "basis": entry.get("basis"),
                        "concept": entry.get("concept"),
                        "how": entry.get("how"),
                        "source": entry.get("source"),
                        "method": entry.get("method"),
                        "flags": entry.get("flags") or [],
                        "value": _input_value(inputs, item, basis, entry, bases),
                        "accession": accession,
                        # the TTM combines several filings: the columns it used carry their own
                        # accessions (label, period key, accession).
                        "columns": entry.get("columns") or [],
                        "edgar_url": edgar_url,
                        "value_source": "recorded",
                        # the filing's own column the fact was read on (the period key)
                        "period": (block.get("period") or ""),
                    }
                    seen[key] = row
                elif row.get("value") is None:
                    row["value"] = _input_value(inputs, item, basis, entry, bases)
    if is_annual:
        _fill_annual(seen)
    if fact_value is not None:
        _fill_facts(seen, fact_value)
    return [seen[key] for key in sorted(seen)]


def _fill_annual(seen: dict[tuple[str, str], dict[str, Any]]) -> None:
    """On a 10-K the period column and the TTM column are the same column (MET-03): a value one
    metric recorded on one of the two bases is shown on the other as well."""
    for (item, basis), row in seen.items():
        if row.get("value") is not None or basis not in ("period", "ttm"):
            continue
        other = seen.get((item, "ttm" if basis == "period" else "period"))
        if other is not None and other.get("value") is not None:
            row["value"] = other["value"]
            row["value_source"] = "the same column on a 10-K (MET-03)"


def _fill_facts(seen: dict[tuple[str, str], dict[str, Any]], fact_value: Any) -> None:
    """Where no metric recorded the item on the filing's own column, the filing's own stored fact
    is the value (a mapped or fallback value is a combination: the card does not compute it)."""
    for row in seen.values():
        if row.get("value") is not None or row.get("basis") != "period":
            continue
        how = row.get("how")
        if how not in ("concept", "map"):
            continue
        value = fact_value(row)
        if value is not None:
            row["value"] = value
            row["value_source"] = (
                "the filing's own stored fact"
                if how == "concept"
                else "the filing's own stored facts, summed"
            )


def _filing_edgar(
    cik: str, accession: str, form: str, *, with_document: bool
) -> dict[str, str | None]:
    """The filing's EDGAR links: the folder (always, it is deterministic) and the primary
    document's URL (only with ``with_document``: it reads the filing's index over the network)."""
    from fundamental_agent.filing_text import (  # noqa: PLC0415 - the EDGAR helper is optional
        _accession_nodash,
        _archive_cik,
    )

    cik_int = _archive_cik(accession, cik)
    folder = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{_accession_nodash(accession)}"
    document: str | None = None
    if with_document:
        from fundamental_agent.filing_text import (  # noqa: PLC0415 - the EDGAR helper is optional
            primary_document_url,
        )

        try:
            document = primary_document_url(cik, accession, form=form)
        except Exception:  # the card still prints the folder when EDGAR is down
            document = None
    return {"edgar_folder": folder, "edgar_document_url": document}


def _vetoes(conn: Any, asset_id: int, as_of: str) -> list[dict[str, Any]]:
    """The rules that hit this company on *as_of*, evaluated as the cycle's veto step evaluates
    them: the cycle's readers (``cycle.data``), the cycle's own rules (``cycle.rules.builtin``)
    and the same ``RuleContext`` -- as in V4's system side.

    The context holds this one company: a rule's verdict for an asset never depends on the other
    assets, so the verdict is the cycle's own.
    """
    # The cycle is above this package (it is the orchestrator), so its rules are imported here
    # and not at the top of the module: the card stays usable without the cycle package.
    from cycle import data  # noqa: PLC0415
    from cycle.config import CycleSettings  # noqa: PLC0415
    from cycle.rules.base import RuleContext  # noqa: PLC0415
    from cycle.rules.builtin import RULES  # noqa: PLC0415
    from kg_schema import versions as kg_versions  # noqa: PLC0415

    versions = kg_versions.resolve_metric_versions(conn)
    dq = data.data_quality(conn, as_of, versions)
    metrics = dq.apply(data.latest_metrics(conn, as_of, versions))
    engine_version = CycleSettings.model_fields["observation_engine_version"].default
    try:
        price_obs = data.latest_price_observation(conn, as_of, engine_version)
    except Exception:  # no observation at the cycle's engine version: no price rule
        price_obs = {}
    sector = conn.execute(
        "SELECT s.name FROM assets a LEFT JOIN sectors s ON s.id = a.sector_id WHERE a.id = ?",
        (asset_id,),
    ).fetchone()
    ctx = RuleContext(
        cycle_date=as_of,
        metrics={asset_id: metrics.get(asset_id, {})},
        price_obs={asset_id: price_obs[asset_id]} if asset_id in price_obs else {},
        last_fundamental=data.last_fundamental_dates(conn, as_of),
        data_quality={asset_id: dq.hard[asset_id]} if asset_id in dq.hard else {},
        sectors={asset_id: sector["name"] if sector else None},
        negative_equity=(
            {asset_id: dq.negative_equity[asset_id]} if asset_id in dq.negative_equity else {}
        ),
        profiles=data.asset_profiles(conn, [asset_id], as_of),
        utility_cash_coverage=data.utility_cash_coverage(conn, as_of),
    )
    hits: list[dict[str, Any]] = []
    for rule in RULES:
        for hit in rule.evaluate(ctx).hits:
            if hit.asset_id != asset_id:
                continue
            hits.append(
                {
                    "rule_id": hit.rule_id,
                    "severity": hit.severity,
                    "evidence": hit.evidence,
                }
            )
    return hits


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


def build_card(  # noqa: PLR0913 - the card's inputs, one per decision
    conn: Any,
    cik: str,
    as_of: str,
    *,
    metrics_version: str | None = None,
    with_edgar: bool = False,
    with_vetoes: bool = True,
) -> Card:
    """Build one company's card from the **stored** metrics (read-only; never recomputes).

    ``with_edgar`` reads the filing's index on EDGAR to name its primary document (the card then
    prints the link to it); without it the card prints the filing's folder, which is deterministic.
    """
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

    accession = str(filing["accession_number"] or "")
    edgar = _filing_edgar(cik, accession, str(filing["form"]), with_document=with_edgar)
    cover = _cover_shares(conn, int(filing["id"]), cik)
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
            "accession": accession,
            "period_end": str(filing["period_end"]),
            "available_at": str(filing["available_at"] or ""),
            "edgar_folder": edgar["edgar_folder"],
            "edgar_document_url": edgar["edgar_document_url"],
        },
        line_items=_line_items(
            all_entries,
            edgar["edgar_document_url"] or edgar["edgar_folder"],
            is_annual=str(filing["form"]).upper() == "10-K",
            fact_value=lambda row: (
                _stored_sum(conn, int(filing["id"]), row.get("concept"), (row.get("period") or ""))
                if row.get("how") == "map"
                else _stored_fact(
                    conn, int(filing["id"]), row.get("concept"), (row.get("period") or "")
                )
            ),
        ),
        metrics=metrics,
        empty=empty,
        vetoes=_vetoes(conn, asset_id, as_of) if with_vetoes else [],
        cover_shares=cover,
        market_cap_count=_market_cap_count(
            conn,
            asset_id,
            None if not cover else cover[0].get("total"),
            next(
                (
                    metric.get("inputs")
                    for metric in all_entries
                    if metric["name"] == "market_capitalization"
                ),
                None,
            ),
        ),
    )


def _cover_lines(card: Card) -> list[str]:
    """The cover-page share lines: each class and the total the market cap reads."""
    if not card.cover_shares:
        return ["  none stored"]
    lines = []
    for entry in card.cover_shares:
        member = entry["class_member"] or "(the company's common stock)"
        lines.append(f"  {member:<40}{entry['value']:>20,.0f}  as of {entry['as_of_date'] or '-'}")
    first = card.cover_shares[0]
    if first.get("total") is not None:
        lines.append(
            f"  {'the total the market cap reads':<40}{first['total']:>20,.0f}  "
            f"as of {first.get('total_as_of') or '-'} "
            f"({first.get('classes') or 0} class(es), "
            f"{first.get('excluded_members') or 0} non-common member(s) excluded)"
        )
    count = card.market_cap_count
    if count.get("shares") is not None:
        age = count.get("share_age_days")
        lines.append(
            f"  {'the count the cap used':<40}{count['shares']:>20,.0f}  "
            + (
                f"from filing {count['accession']}"
                if count.get("accession")
                else "from the fallback"
            )
            + (f", {age} days old" if age is not None else "")
        )
        lines.append(f"  the guard's verdict: {count.get('verdict') or '-'}")
    return lines


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
        f"EDGAR: {card.filing.get('edgar_document_url') or card.filing.get('edgar_folder')}",
        f"Stored metrics: {card.metrics_version}",
        "",
        "Line items (as the stored provenance records them):",
        "  the value is the one the stored metric recorded (the card never recomputes); on a 10-K "
        "the period and the TTM are the same column (MET-03);",
        "  '-' means no metric recorded that item on that basis,",
    ]
    for item in card.line_items:
        extra = item["source"] or item["method"] or ""
        flags = f"  flags: {', '.join(item['flags'])}" if item["flags"] else ""
        value = item.get("value")
        shown = "-" if value is None else f"{value:,.2f}"
        lines.append(
            f"  {item['item']:<28}{item['basis']:<14}{shown:>20}  "
            f"{item['concept'] or '-':<60}[{item['how'] or '-'}] {extra}{flags}"
        )
        lines.append(
            f"      accession {item.get('accession') or '-'}  {item.get('edgar_url') or '-'}"
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
    lines += ["", "Cover-page shares (MKT-01, the counts on the filing's cover):"]
    lines += _cover_lines(card)
    lines += ["", f"Vetoes on {card.as_of} (the cycle's rules, as the veto step evaluates them):"]
    if not card.vetoes:
        lines.append("  none")
    for veto in card.vetoes:
        evidence = json.dumps(veto.get("evidence") or {}, sort_keys=True, default=str)
        lines.append(f"  {veto['rule_id']:<24}{veto['severity']:<6}{evidence}")
    return "\n".join(lines) + "\n"


__all__ = ["Card", "build_card", "render"]
