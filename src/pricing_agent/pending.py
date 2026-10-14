"""``python -m pricing_agent pending``: does a member lack the last session's data? (T-017)

Read-only (``connect_ro``), gateway GETs only, no write. One line, ``pending: yes|no <evidence>``.

Let S be the last NYSE session on or before the analysis date D. Pending iff any of:

1. a member has no ``price_daily`` bar at S **and the gateway has a candle for it in the window
   from the day after its latest stored bar (the step's own start date when it has none) to S**
   (one ``daily_any_spelling`` call, the step's own spelling fallback). A member behind by several
   sessions is pending as soon as the gateway holds any of them. A member the gateway has no
   candle for in that window is *dead*: running the step would change nothing for it, so it is
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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from portfolio_common.db import Database, DatabaseError

from kg_schema.db import connect_ro
from kg_schema.queries import WINDOW_START, UniverseMember, members_over_range
from kg_schema.ticker_history import canonical_for_member
from kg_schema.trading_calendar import last_session_on_or_before, session_is_open_or_pending
from pricing_agent.config import Settings
from pricing_agent.db import PRICE_OBSERVATION_ENGINE_VERSION, has_pre_split_rows
from pricing_agent.pipeline import DEFAULT_START_DATE
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
    lack: dict[str, str] = field(
        default_factory=dict
    )  # no bar at S -> where the gateway window starts
    session: dict[str, str] = field(
        default_factory=dict
    )  # ticker -> the session the line must reach (its exit for a leaver)
    no_obs: list[str] = field(default_factory=list)  # a bar but no observation
    split: list[str] = field(default_factory=list)  # pre-split bars, re-fetchable
    refused: list[str] = field(default_factory=list)  # pre-split bars, re-fetch refused


def _window_start(conn: Database, asset_id: int | None, s: str) -> str:
    """The day after the member's latest stored bar on or before S, or the step's own start date
    when it has none: the gateway window that decides pending versus dead."""
    if asset_id is None:
        return DEFAULT_START_DATE
    row = conn.execute(
        "SELECT MAX(date) AS d FROM price_daily WHERE asset_id = ? AND date <= ?", (asset_id, s)
    ).fetchone()
    if row is None or row["d"] is None:
        return DEFAULT_START_DATE
    return (date.fromisoformat(str(row["d"])) + timedelta(days=1)).isoformat()


def _scan(
    conn: Database,
    members: list[UniverseMember],
    s: str,
    analysis_date: str,
    exits: dict[str, str | None] | None = None,
) -> _Scan:
    ids = {
        str(r["ticker"]).upper(): int(r["id"])
        for r in conn.execute("SELECT id, ticker FROM assets")
    }
    scan = _Scan()
    for m in members:
        # T-153 part 2: resolve the member by CIK to its line's canonical ticker, exactly as the
        # step does -- the bars, the split refusal and the run's errors are all under it (F1).
        ticker = canonical_for_member(conn, symbol=m.symbol, cik=m.cik, as_of=analysis_date)
        # T-153 part 3a: a line that left the index must reach its own exit session, not the run's
        # -- nothing after its exit is fetched for it, so the run's session would read as missing.
        until = (exits or {}).get(m.symbol.upper())
        member_s = last_session_on_or_before(date.fromisoformat(until)).isoformat() if until else s
        asset_id = ids.get(ticker.upper())
        if (
            asset_id is None
            or not conn.execute(
                "SELECT 1 FROM price_daily WHERE asset_id = ? AND date = ?", (asset_id, member_s)
            ).fetchone()
        ):
            scan.lack[ticker] = _window_start(conn, asset_id, member_s)
        elif not conn.execute(
            "SELECT 1 FROM price_observation WHERE asset_id = ? AND obs_date = ? "
            "AND engine_version = ?",
            (asset_id, member_s, PRICE_OBSERVATION_ENGINE_VERSION),
        ).fetchone():
            scan.no_obs.append(ticker)
        scan.session[ticker] = member_s
        if asset_id is not None and has_pre_split_rows(conn, asset_id, end=analysis_date):
            (scan.refused if _refused(conn, ticker) else scan.split).append(ticker)
    return scan


def check(  # noqa: PLR0913 - keyword-only ``exits`` with a default, like ``reconcile``'s fields
    conn: Database,
    members: list[UniverseMember],
    analysis_date: str,
    *,
    client: PricingClient,
    now: datetime | None = None,
    exits: dict[str, str | None] | None = None,
) -> tuple[bool, str]:
    """``(pending, evidence)``. *client* is only read for clause 1.

    *exits* (T-153 part 3a) maps a canonical ticker to its line's last membership day: a leaver
    is checked against its own exit session, not the run's. Without it every member is checked
    against the run's session, as before."""
    session = last_session_on_or_before(date.fromisoformat(analysis_date))
    s = session.isoformat()
    if session_is_open_or_pending(session, now or datetime.now(tz=UTC)):
        return True, f"session {s} is not final yet (the step refuses it)"
    for table in ("assets", "price_daily", "price_observation"):
        if not conn.relation_exists(table):
            return True, f"no {table} table (fresh database)"
    scan = _scan(conn, members, s, analysis_date, exits)
    if scan.no_obs:
        when = ", ".join(sorted({scan.session[t] for t in scan.no_obs}))
        return (
            True,
            f"{len(scan.no_obs)} members lack the {when} observation ({_named(scan.no_obs)})",
        )
    if scan.split:
        return True, (
            f"{len(scan.split)} members hold bars from before a recorded split "
            f"({_named(scan.split)})"
        )
    dead: list[str] = []
    for symbol, since in scan.lack.items():
        if client.daily_any_spelling(symbol, since, scan.session[symbol]).candles:
            return True, (
                f"{len(scan.lack)} members lack {scan.session[symbol]} "
                f"({_named(list(scan.lack))}); the gateway has candles since {since} for {symbol}"
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
        window = members_over_range(universe, conn, WINDOW_START, analysis_date)
        members = [w.member for w in window]
        exits = {w.member.symbol.upper(): w.window_to for w in window}
        if not members:
            return f"pending: yes universe has no members in [{WINDOW_START}, {analysis_date}]"
        try:
            pending, evidence = check(conn, members, analysis_date, client=gateway, exits=exits)
        except DatabaseError as exc:  # a partial schema is a P2 doubt
            return f"pending: yes cannot read the database ({exc})"
        return f"pending: {'yes' if pending else 'no'} {evidence}"
    finally:
        if owns:
            gateway.close()
        universe.close()
        conn.close()
