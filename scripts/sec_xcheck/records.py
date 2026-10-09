"""The population of the resolver-level measurements: one :class:`Rec` per stored filing."""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from fundamental_agent.statements import YEAR_TOLERANCE_DAYS
from sec_xcheck.common import filings, own_period_key, statements_from_db
from sec_xcheck.features import extract
from sec_xcheck.findings import Finding, spread


@dataclass(frozen=True)
class Rec:
    """A stored filing and its audit record (:func:`sec_xcheck.features.extract`)."""

    filing_id: int
    ticker: str
    cik: str
    sector: str
    sub_industry: str
    form: str
    fiscal_period: str
    period_end: str
    accession: str
    available_at: str | None
    filing_date: str | None
    f: dict[str, Any]
    # the column of the filing this record resolves: ``target`` is the filing's own period; the others are the
    # columns the pipeline also reads (F2): ``prior``, ``ytd``, ``ytd_prior`` (a 10-Q's TTM identity), ``cagr_base``
    role: str = "target"
    column_end: str = ""
    column_kind: str = ""  # "fy" | "q" | "ytd"

    def item(self, name: str) -> dict[str, Any]:
        return dict(self.f["items"][name])

    def value(self, name: str) -> float | None:
        return self.f["items"][name]["value"]


def column_kind(tag: str) -> str:
    return "fy" if tag == "FY" else "ytd" if tag == "YTD" else "q"


def pipeline_columns(stmts: Any, form: str, target: Any) -> list[tuple[str, Any]]:
    """``(role, Period)`` of every column besides the target that the pipeline reads for this filing:

    * the prior column (growth: ``Statements.prior_of``);
    * a 10-K's earliest fiscal-year column (the CAGR base);
    * a 10-Q's current and prior-year year-to-date columns (the TTM identity, ``pipeline._ytd_columns``)."""
    out: list[tuple[str, Any]] = []
    prior = stmts.prior_of(target)
    if prior is not None:
        out.append(("prior", prior))
    if form == "10-K":
        fy = stmts.fy_periods()
        if fy and fy[0].date not in {target.date, getattr(prior, "date", None)}:
            out.append(("cagr_base", fy[0]))
    elif form == "10-Q":
        year_ago = dt.date.fromisoformat(target.date) - dt.timedelta(days=365)
        ytd = [p for p in stmts.periods if p.tag == "YTD"]
        current = next((p for p in ytd if p.date == target.date), None)
        before = next(
            (
                p
                for p in ytd
                if abs((dt.date.fromisoformat(p.date) - year_ago).days) <= YEAR_TOLERANCE_DAYS
            ),
            None,
        )
        if current is not None and before is not None:
            out += [("ytd", current), ("ytd_prior", before)]
    return out


def load(conn: Any, *, all_columns: bool = True) -> list[Rec]:
    """Every stored filing that has a column of its own, with its audit record; with *all_columns*, also one record
    per further column the pipeline reads (:func:`pipeline_columns`). Only the ``target`` record carries replayed
    metrics: the metric rules are about the filing's own period."""
    sectors = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM sectors")}
    assets = {
        r["id"]: (sectors.get(r["sector_id"], ""), r["sub_industry"] or "")
        for r in conn.execute("SELECT id, sector_id, sub_industry FROM assets")
    }
    out: list[Rec] = []
    for f in filings(conn):
        stmts = statements_from_db(conn, f["id"])
        key = own_period_key(stmts, f["form"], f["period_end"])
        if key is None:
            continue
        sector, sub = assets.get(f["asset_id"], ("", ""))
        head = (
            f["id"],
            f["ticker"],
            f["cik"],
            sector,
            sub,
            f["form"],
            f["fiscal_period"],
            f["period_end"],
        )
        tail = (f["accession_number"], f["available_at"], f["filing_date"])
        target = next(p for p in stmts.periods if p.key == key)
        out.append(
            Rec(*head, *tail, extract(stmts, key), "target", target.date, column_kind(target.tag))
        )
        if all_columns:
            for role, period in pipeline_columns(stmts, f["form"], target):
                out.append(
                    Rec(
                        *head,
                        *tail,
                        extract(stmts, period.key, metrics=False),
                        role,
                        period.date,
                        column_kind(period.tag),
                    )
                )
    return out


def targets(recs: Iterable[Rec]) -> list[Rec]:
    """The records of the filings' own periods (metric, period and cycle rules read these)."""
    return [r for r in recs if r.role == "target"]


def count(  # noqa: PLR0913 - a measurement is its key, rules, title, predicate and presentation
    key: str,
    rules: list[str],
    title: str,
    recs: Iterable[Rec],
    pred: Callable[[Rec], bool],
    *,
    detail: Callable[[Rec], str] = lambda r: (
        f"{r.form} {r.fiscal_period}"
        + ("" if r.role == "target" else f" [{r.role} {r.column_end}]")
    ),
    population: Callable[[Rec], bool] = lambda r: True,
    unit: str = "filings",
    note: str = "",
    level: str = "b",
    incomplete: bool = False,
    command: str = "",
) -> Finding:
    """A :class:`Finding` for the filings of *population* that satisfy *pred*."""
    pool = [r for r in recs if population(r)]
    hits = [r for r in pool if pred(r)]
    columns = any(r.role != "target" for r in pool)
    if columns and unit == "filings":
        unit = "filing-columns"
    return Finding(
        key,
        rules,
        title,
        level,
        len(hits),
        len(pool),
        len({r.ticker for r in hits}),
        unit=unit,
        examples=spread([(r.ticker, r.accession, detail(r)) for r in hits]),
        incomplete=incomplete,
        note=note,
        command=command,
        filings=len({r.filing_id for r in hits}),
        target_count=sum(1 for r in hits if r.role == "target") if columns else -1,
        target_total=sum(1 for r in pool if r.role == "target") if columns else -1,
        target_companies=len({r.ticker for r in hits if r.role == "target"}) if columns else -1,
    )


def amounts_note(values: list[float]) -> str:
    """A one-line summary of amounts (USD): count, median and maximum."""
    if not values:
        return ""
    return f"amount n={len(values)}, median {statistics.median(values) / 1e6:,.0f}M, max {max(values) / 1e6:,.0f}M"
