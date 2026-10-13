"""``python -m pricing_agent pending``: does a member lack the last session's data? (T-017)

Read-only (``connect_ro``), gateway GETs only, no write. One line, ``pending: yes|no <evidence>``.

Let S be the last NYSE session on or before the analysis date D. Pending iff any of:

1. a member has no ``price_daily`` bar at S **and the gateway has a candle at S for it**
   (``daily_any_spelling``, the step's own spelling fallback; the window is S..S). A member the
   gateway has no candle for is *dead*: running the step would change nothing for it, so it is
   listed in the evidence and does not count (P6);
2. a member has its bar at S but no ``price_observation`` row at ``(S, PRICE_OBSERVATION_ENGINE_VERSION)``;
3. ``has_pre_split_rows`` -- a split quant recorded after the bars were stored (T-131). A member
   whose latest ``pricing_run_error`` is a ``split_jump`` refusal is *refused*: the step would
   write nothing for it again, so it is listed and does not count (P6);
4. S is a session that is not final yet. The step refuses it (``SessionNotClosed``), so the
   check says yes and the refusal stays visible, as it is today.

``price_window`` is not part of the signal: nothing outside ``pricing_agent`` reads it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from portfolio_common.db import Database, DatabaseError

from kg_schema.db import connect_ro
from kg_schema.queries import UniverseMember, members_asof
from kg_schema.trading_calendar import last_session_on_or_before, session_is_open_or_pending
from pricing_agent.config import Settings
from pricing_agent.db import PRICE_OBSERVATION_ENGINE_VERSION, has_pre_split_rows
from pricing_agent.pricing_client import PricingClient

_SHOWN = 3


def _named(items: list[str]) -> str:
    return ", ".join(items[:_SHOWN]) + (", ..." if len(items) > _SHOWN else "")


def _refused(conn: Database, ticker: str) -> bool:
    """The member's latest recorded pricing error is a ``split_jump`` refusal (T-131)."""
    if not conn.relation_exists("pricing_run_error"):
        return False
    row = conn.execute(
        "SELECT stage FROM pricing_run_error WHERE ticker = ? ORDER BY id DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    return row is not None and row["stage"] == "split_jump"


@dataclass
class _Scan:
    lack: list[str] = field(default_factory=list)  # no bar at S
    no_obs: list[str] = field(default_factory=list)  # a bar but no observation
    split: list[str] = field(default_factory=list)  # pre-split bars, re-fetchable
    refused: list[str] = field(default_factory=list)  # pre-split bars, re-fetch refused


def _scan(conn: Database, members: list[UniverseMember], s: str, analysis_date: str) -> _Scan:
    ids = {
        str(r["ticker"]).upper(): int(r["id"])
        for r in conn.execute("SELECT id, ticker FROM assets")
    }
    scan = _Scan()
    for m in members:
        asset_id = ids.get(m.symbol.upper())
        if (
            asset_id is None
            or not conn.execute(
                "SELECT 1 FROM price_daily WHERE asset_id = ? AND date = ?", (asset_id, s)
            ).fetchone()
        ):
            scan.lack.append(m.symbol)
        elif not conn.execute(
            "SELECT 1 FROM price_observation WHERE asset_id = ? AND obs_date = ? "
            "AND engine_version = ?",
            (asset_id, s, PRICE_OBSERVATION_ENGINE_VERSION),
        ).fetchone():
            scan.no_obs.append(m.symbol)
        if asset_id is not None and has_pre_split_rows(conn, asset_id, end=analysis_date):
            (scan.refused if _refused(conn, m.symbol) else scan.split).append(m.symbol)
    return scan


def check(
    conn: Database,
    members: list[UniverseMember],
    analysis_date: str,
    *,
    client: PricingClient,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """``(pending, evidence)``. *client* is only read for clause 1."""
    session = last_session_on_or_before(date.fromisoformat(analysis_date))
    s = session.isoformat()
    if session_is_open_or_pending(session, now or datetime.now(tz=UTC)):
        return True, f"session {s} is not final yet (the step refuses it)"
    for table in ("assets", "price_daily", "price_observation"):
        if not conn.relation_exists(table):
            return True, f"no {table} table (fresh database)"
    scan = _scan(conn, members, s, analysis_date)
    if scan.no_obs:
        return True, f"{len(scan.no_obs)} members lack the {s} observation ({_named(scan.no_obs)})"
    if scan.split:
        return True, (
            f"{len(scan.split)} members hold bars from before a recorded split "
            f"({_named(scan.split)})"
        )
    dead: list[str] = []
    for symbol in scan.lack:
        if any(c.date == s for c in client.daily_any_spelling(symbol, s, s).candles):
            return True, (
                f"{len(scan.lack)} members lack {s} ({_named(scan.lack)}); "
                f"the gateway has it for {symbol}"
            )
        dead.append(symbol)
    extras = []
    if dead:
        extras.append(f"{len(dead)} without a gateway candle ({_named(dead)})")
    if scan.refused:
        extras.append(f"{len(scan.refused)} refused split jump ({_named(scan.refused)})")
    tail = f"; {'; '.join(extras)}" if extras else ""
    return False, f"{len(members) - len(dead)}/{len(members)} members at {s}{tail}"


def pending_line(
    settings: Settings, analysis_date: str, *, client: PricingClient | None = None
) -> str:
    if not Path(settings.db_path).exists():
        return f"pending: yes {settings.db_path} does not exist (fresh database)"
    conn = connect_ro(settings.db_path)
    universe = connect_ro(settings.universe_db_path)
    owns = client is None
    gateway = client or PricingClient(settings.pricing_base_url)
    try:
        members = members_asof(universe, analysis_date)
        if not members:
            return f"pending: yes universe has no members as of {analysis_date}"
        try:
            pending, evidence = check(conn, members, analysis_date, client=gateway)
        except DatabaseError as exc:  # a partial schema is a P2 doubt
            return f"pending: yes cannot read the database ({exc})"
        return f"pending: {'yes' if pending else 'no'} {evidence}"
    finally:
        if owns:
            gateway.close()
        universe.close()
        conn.close()
