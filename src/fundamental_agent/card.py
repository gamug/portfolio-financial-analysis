"""The company card (T-151 part 3, D15).

``python -m fundamental_agent card --db PATH --cik CIK --date YYYY-MM-DD [--out FILE]`` prints, for
one company and one as-of date, read-only (``mode=ro``):

* the profile -- ticker, sector, sub-industry, REIT yes/no, negative equity yes/no;
* the filing used as of the date -- the latest one whose availability date (PER-01) is on or before
  the date;
* each line item with its concept, value and how it was read (T-148's ``Resolution``);
* each metric with its value, flags, rule IDs and provenance;
* the empty metrics with their reason (D14's codes).

Used for the thesis and for debugging.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fundamental_agent import db
from fundamental_agent.agents import FilingContext, FundamentalAnalyst
from fundamental_agent.pipeline import _TTM_ITEMS, _Target, _ytd_columns
from fundamental_agent.pricing import close_on_or_before
from fundamental_agent.statements import REGISTRY, Statements
from kg_schema.market_cap import market_caps_as_of


@dataclass(frozen=True)
class Card:
    """One company's card: the profile, the filing used and its line items and metrics."""

    ticker: str
    cik: str
    company_name: str
    sector: str | None
    sub_industry: str | None
    is_reit: bool
    negative_equity: bool
    as_of: str
    filing: dict[str, Any]
    line_items: list[dict[str, Any]]
    metrics: list[dict[str, Any]]
    empty: list[dict[str, Any]]


def _own_period_key(stmts: Statements, form: str, period_end: str) -> str | None:
    """The column a filing's own period sits in: the FY column of a 10-K, the quarter column of a
    10-Q (``scripts/sec_xcheck/common.py``'s rule)."""
    for period in stmts.periods:
        if period.date != period_end:
            continue
        if (form.startswith("10-K") and period.is_fy) or (
            form.startswith("10-Q") and period.is_quarter
        ):
            return str(period.key)
    return None


def _asset_row(conn: Any, cik: str) -> Any:
    return conn.execute(
        "SELECT a.id, a.ticker, a.company_name, s.name AS sector, a.sub_industry "
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


def build_card(conn: Any, cik: str, as_of: str) -> Card:
    """Build one company's card from the stored facts and metrics (read-only)."""
    asset = _asset_row(conn, cik)
    if asset is None:
        raise SystemExit(f"no asset with CIK {cik}")
    asset_id = int(asset["id"])
    filing = _filing_as_of(conn, asset_id, as_of)
    if filing is None:
        raise SystemExit(f"no filing of CIK {cik} available on or before {as_of}")

    period_end = str(filing["period_end"])
    form = str(filing["form"])
    stmts = db.statements_from_facts(conn, int(filing["id"]))
    stmts.reit = db.is_equity_reit(conn, asset_id)
    own_key = _own_period_key(stmts, form, period_end)
    if own_key is None:
        raise SystemExit(f"filing {filing['accession_number']} has no own period column")
    db.attach_context(
        conn, asset_id, form, own_key, stmts, accession=str(filing["accession_number"] or "")
    )

    # A 10-Q's flow ratios need the trailing twelve months (F4 / T-105); build them the way the
    # pipeline does, from the stored quarters and the prior 10-K.
    target_period = next((p for p in stmts.periods if p.key == own_key), None)
    ttm: dict[str, Any] = {}
    prior_key: str | None = None
    if target_period is not None and form.startswith("10-Q"):
        target = _Target(
            period=target_period,
            fiscal_period=str(filing["fiscal_period"]),
            prior=stmts.prior_of(target_period),
            fiscal_year=int(filing["fiscal_year"]),
        )
        prior_key = target.prior.key if target.prior else None
        current = {
            item: value for item in _TTM_ITEMS if (value := stmts.get(item, own_key)) is not None
        }
        cur_key, prior_ytd_key, prior_end = _ytd_columns(stmts, target)
        ytd = (
            {
                item: db.YTDPair(
                    stmts.get(item, cur_key), stmts.get(item, prior_ytd_key), prior_end
                )
                for item in _TTM_ITEMS
            }
            if cur_key and prior_ytd_key
            else {}
        )
        if current or ytd:
            ttm = db.ttm_detail(
                conn, asset_id, period_end=target.period.date, current=current, ytd=ytd
            )
    elif target_period is not None:
        prior_period = stmts.prior_of(target_period)
        prior_key = prior_period.key if prior_period else None

    mc_res = market_caps_as_of(conn, [asset_id], as_of=period_end)
    analyst = FundamentalAnalyst(None, "rule-based")
    ctx = FilingContext(
        ticker=str(asset["ticker"]),
        company_name=str(asset["company_name"] or asset["ticker"]),
        form=form,
        fiscal_period=str(filing["fiscal_period"]),
        stmts=stmts,
        period_key=own_key,
        prior_key=prior_key,
        price=close_on_or_before(conn, asset_id, period_end),
        ttm_flows=ttm,
        cik=cik,
        market_cap=mc_res.caps.get(asset_id),
        market_cap_missing=mc_res.missing.get(asset_id),
    )
    computed = analyst._compute_all(ctx)

    line_items = [
        {
            "item": item,
            "value": stmts.resolve(item, own_key).value,
            "concept": stmts.resolve(item, own_key).concept,
            "how": stmts.resolve(item, own_key).how,
            "flags": list(stmts.resolve(item, own_key).flags),
        }
        for item in REGISTRY
    ]
    metrics: list[dict[str, Any]] = []
    empty: list[dict[str, Any]] = []
    for group, result in computed:
        block = result.inputs.get("provenance", {})
        entry = {
            "group": group,
            "name": result.name,
            "value": result.value,
            "unit": result.unit,
            "rule_ids": block.get("rule_ids", []),
            "flags": block.get("flags", []),
            "provenance": block,
        }
        if result.value is None:
            empty.append(
                {
                    "name": result.name,
                    "reason": block.get("empty_reason"),
                    "items": block.get("items", {}),
                }
            )
        else:
            metrics.append(entry)

    equity = stmts.resolve("equity", own_key).value
    return Card(
        ticker=str(asset["ticker"]),
        cik=cik,
        company_name=str(asset["company_name"] or asset["ticker"]),
        sector=asset["sector"],
        sub_industry=asset["sub_industry"],
        is_reit=stmts.reit,
        negative_equity=equity is not None and equity < 0.0,
        as_of=as_of,
        filing={
            "form": form,
            "fiscal_period": str(filing["fiscal_period"]),
            "accession": str(filing["accession_number"] or ""),
            "period_end": period_end,
            "available_at": str(filing["available_at"] or ""),
        },
        line_items=line_items,
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
        f"Filing: {card.filing['form']} {card.filing['fiscal_period']} "
        f"{card.filing['accession']} (period end {card.filing['period_end']}, "
        f"available {card.filing['available_at']})",
        "",
        "Line items:",
    ]
    for item in card.line_items:
        value = "-" if item["value"] is None else f"{item['value']:,.2f}"
        flags = f"  flags: {', '.join(item['flags'])}" if item["flags"] else ""
        lines.append(
            f"  {item['item']:<28}{value:>22}  {item['concept'] or '-'} [{item['how'] or '-'}]{flags}"
        )
    lines += ["", "Metrics:"]
    for metric in card.metrics:
        rules = ", ".join(metric["rule_ids"]) or "-"
        flags = ", ".join(metric["flags"]) or "-"
        lines.append(
            f"  {metric['group'] + '.' + metric['name']:<44}{metric['value']:>16.6f}  "
            f"{metric['unit']:<6} rules: {rules}  flags: {flags}"
        )
    lines += ["", "Empty metrics (with the reason):"]
    for entry in card.empty:
        concepts = ", ".join(
            f"{item}={info.get('concept') or '-'}" for item, info in entry["items"].items()
        )
        lines.append(f"  {entry['name']:<44}{entry['reason'] or '-':<26}{concepts}")
    return "\n".join(lines) + "\n"


__all__ = ["Card", "build_card", "render"]
