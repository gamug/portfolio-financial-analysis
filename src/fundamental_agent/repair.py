"""Replace the legacy pre-T-091 quarterly rows that share one 10-Q's accession (T-120).

Before T-091/T-092 the gateway returned one 10-Q per year, and every quarter column of its
payload was stored as a filing of its own: ALLE's Q3 2022 10-Q (accession
``0001579241-22-000063``, filed 2022-10-27) became rows ``2022Q1``, ``2022Q2`` and ``2022Q3``,
all with that accession, that filing date and that payload's facts. Only the row for the
filing's **own** period -- the latest, as :func:`pipeline._targets` picks it -- is genuine;
the others are *stale*: the wrong filing date (they read as public months late) and facts
that are the Q3 payload's, versioned under the Q3 accession.

The repair, per shared accession:

1. **Find** each stale quarter's own 10-Q on the gateway (which lists every 10-Q since T-092):
   the filing, other than the shared one, whose own period ends on the stale row's period end.
2. **Replace**, in one transaction: delete the stale rows (their facts and sections cascade)
   and write each replacement's filing row and facts -- as ``run`` would have, but without
   metrics or a score, which the stale rows never had either (the full recompute, ``T-100``,
   computes them).

A group is only touched once every stale quarter's replacement is found, so a gateway outage
leaves it as it was and the command can simply be re-run; ``drop_unresolved`` deletes stale
rows with no replacement instead. A stale row that anything was computed from (metrics, a
score, a data-quality verdict, a legacy snapshot) is refused, never deleted.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from portfolio_common.db import Database

from fundamental_agent import db
from fundamental_agent.db import FilingKey, FilingMeta
from fundamental_agent.edgar_client import (
    EdgarError,
    EdgarNotFoundError,
    FilingRef,
    normalize_ticker,
)
from fundamental_agent.pipeline import _targets, _YearTask
from fundamental_agent.statements import Statements, iter_facts


class Gateway(Protocol):
    """The two ``EdgarClient`` calls the repair makes."""

    def filing_by_year(self, ticker: str, form: str, year: int) -> list[FilingRef]: ...

    def financials(
        self, ticker: str, form: str, year: int, accession_number: str | None = None
    ) -> dict[str, list[dict[str, Any]]]: ...


class RepairRefused(RuntimeError):
    """A stale row has derived data; deleting it would orphan or silently drop that data."""


@dataclass(frozen=True)
class StaleRow:
    filing_id: int
    fiscal_period: str
    period_end: str
    facts: int
    sections: int


@dataclass
class SharedGroup:
    """One accession number carried by several filing rows of one asset."""

    asset_id: int
    ticker: str
    form: str
    accession: str
    filing_date: str | None
    keep_id: int  # the row for the filing's own (latest) period
    keep_period: str
    stale: list[StaleRow] = field(default_factory=list)


@dataclass(frozen=True)
class Replacement:
    """A stale quarter's own filing, fetched and parsed."""

    stale: StaleRow
    accession: str
    filing_date: str | None
    fiscal_year: int
    stmts: Statements


@dataclass
class GroupOutcome:
    group: SharedGroup
    replaced: list[Replacement] = field(default_factory=list)
    unresolved: list[StaleRow] = field(default_factory=list)
    applied: bool = False
    dropped: bool = False
    error: str | None = None  # the gateway failed: nothing found, nothing touched


_DERIVED = (
    ("fundamental_metrics", "metrics"),
    ("score_snapshot", "scores"),
    ("data_quality_issue", "data-quality verdicts"),
    ("fundamental_snapshot_legacy", "legacy snapshots"),
)


def _count(conn: Database, table: str, filing_id: int) -> int:
    if not conn.table_exists(table):
        return 0
    queries = {
        "fundamental_metrics": "SELECT COUNT(*) FROM fundamental_metrics WHERE filing_id = ?",
        "score_snapshot": "SELECT COUNT(*) FROM score_snapshot WHERE filing_id = ?",
        "data_quality_issue": "SELECT COUNT(*) FROM data_quality_issue WHERE filing_id = ?",
        "fundamental_snapshot_legacy": (
            "SELECT COUNT(*) FROM fundamental_snapshot_legacy WHERE filing_id = ?"
        ),
        "financial_facts": "SELECT COUNT(*) FROM financial_facts WHERE filing_id = ?",
        "sec_filing_section": "SELECT COUNT(*) FROM sec_filing_section WHERE filing_id = ?",
    }
    return int(conn.execute(queries[table], (filing_id,)).fetchone()[0])


def shared_groups(conn: Database) -> list[SharedGroup]:
    """Every shared accession, its genuine row and its stale rows.

    Raises :class:`RepairRefused` if any stale row has derived data."""
    groups: dict[tuple[int, str], SharedGroup] = {}
    rows = db.shared_accession_filings(conn)
    for r in rows:
        key = (int(r["asset_id"]), str(r["accession_number"]))
        g = groups.get(key)
        if g is None:
            g = groups[key] = SharedGroup(
                asset_id=key[0],
                ticker=str(r["ticker"]),
                form=str(r["form"]),
                accession=key[1],
                filing_date=r["filing_date"],
                keep_id=int(r["id"]),
                keep_period=str(r["period_end"]),
            )
        elif str(r["period_end"]) > g.keep_period:
            g.keep_id, g.keep_period = int(r["id"]), str(r["period_end"])
    refused: list[str] = []
    for r in rows:
        g = groups[(int(r["asset_id"]), str(r["accession_number"]))]
        fid = int(r["id"])
        if fid == g.keep_id:
            continue
        derived = [f"{n} {label}" for t, label in _DERIVED if (n := _count(conn, t, fid))]
        if derived:
            refused.append(f"{g.ticker} {r['fiscal_period']} (filing {fid}): {', '.join(derived)}")
        g.stale.append(
            StaleRow(
                fid,
                str(r["fiscal_period"]),
                str(r["period_end"]),
                _count(conn, "financial_facts", fid),
                _count(conn, "sec_filing_section", fid),
            )
        )
    if refused:
        raise RepairRefused(
            "stale rows carry derived data; not deleting them: " + "; ".join(refused)
        )
    return list(groups.values())


def find_replacements(edgar: Gateway, group: SharedGroup) -> GroupOutcome:
    """Look up each stale quarter's own filing on the gateway (read-only)."""
    outcome = GroupOutcome(group)
    wanted = {s.period_end: s for s in group.stale}
    years = sorted({int(p[:4]) for p in wanted} | {int(p[:4]) + 1 for p in wanted})
    found: dict[str, Replacement] = {}
    for candidate in normalize_ticker(group.ticker):
        for year in years:
            if len(found) == len(wanted):
                break
            try:
                refs = edgar.filing_by_year(candidate, group.form, year)
            except EdgarNotFoundError:
                continue
            for ref in refs:
                if ref.accession_number == group.accession or len(found) == len(wanted):
                    continue
                payload = edgar.financials(
                    candidate, group.form, year, accession_number=ref.accession_number
                )
                stmts = Statements.from_payload(payload)
                task = _YearTask(group.asset_id, group.ticker, group.ticker, group.form, year)
                for target in _targets(stmts, task):
                    stale = wanted.get(target.period.date)
                    if (
                        stale is not None
                        and target.fiscal_period == stale.fiscal_period
                        and stale.period_end not in found
                    ):
                        found[stale.period_end] = Replacement(
                            stale,
                            ref.accession_number,
                            ref.filing_date,
                            target.period.year,
                            stmts,
                        )
        if found:
            break  # this spelling is the one EDGAR knows
    outcome.replaced = [found[p] for p in sorted(found)]
    outcome.unresolved = [s for p, s in sorted(wanted.items()) if p not in found]
    return outcome


def apply_group(conn: Database, outcome: GroupOutcome, *, drop_unresolved: bool = False) -> None:
    """Delete the group's stale rows and write their replacements, atomically. A group with
    an unresolved quarter is left untouched unless *drop_unresolved*."""
    if outcome.unresolved and not drop_unresolved:
        return
    group = outcome.group
    with conn.transaction():
        for stale in group.stale:
            conn.execute("DELETE FROM sec_filings WHERE id = ?", (stale.filing_id,))
        for rep in outcome.replaced:
            filing_id = db.upsert_filing(
                conn,
                FilingKey(group.asset_id, group.form, rep.fiscal_year, rep.stale.fiscal_period),
                FilingMeta(
                    filing_date=rep.filing_date,
                    accession_number=rep.accession,
                    period_end=rep.stale.period_end,
                ),
                commit=False,
            )
            db.append_financial_facts(
                conn,
                filing_id,
                iter_facts(rep.stmts),
                filing_version=rep.accession,
                event_time=rep.stale.period_end,
                commit=False,
            )
    outcome.applied = True
    outcome.dropped = bool(outcome.unresolved)


def repair(
    conn: Database,
    edgar: Gateway,
    *,
    apply: bool = False,
    drop_unresolved: bool = False,
    groups: Sequence[SharedGroup] | None = None,
) -> list[GroupOutcome]:
    """Plan (and with *apply*, carry out) the repair of every shared accession."""
    outcomes = []
    for group in shared_groups(conn) if groups is None else groups:
        try:
            outcome = find_replacements(edgar, group)
        except EdgarError as exc:
            outcomes.append(GroupOutcome(group, unresolved=list(group.stale), error=str(exc)))
            continue
        if apply:
            apply_group(conn, outcome, drop_unresolved=drop_unresolved)
        outcomes.append(outcome)
    return outcomes
