"""``python -m quant pending``: is the quant chain behind its inputs? (T-017)

The orchestrator's quant chain is ``backfill-actions`` -> ``build-returns`` -> ``build-risk-model``
-> ``optimize`` -> ``evaluate``; this is the one check on its head. Read-only (``connect_ro``),
gateway GETs only, no write, no LLM. One line, ``pending: yes|no <evidence>``.

Let D be the analysis date and S the last NYSE session on or before it. Pending iff any of,
cheapest first (the gateway is read only when the others say no):

1. **Returns lag**: the newest member ``price_daily`` bar on or before D is later than the newest
   ``quant_return_daily`` row of the configured return engine.
2. **No book**: a default objective (``QuantSettings.objectives`` without ``frontier``) has no
   non-live ``quant_portfolio`` row with ``as_of`` in [S, D]. ``optimize`` runs with ``as_of = D``
   itself, so a book at S answers a later D that has no new session. Any status counts: an
   infeasible objective is an answer, not a gap.
3. **Evaluate lag**: a book ``evaluate`` would score has performance rows (``PERF_ENGINE_VERSION``)
   ending before its newest forward return on or before D. "Would score" mirrors
   ``quant.evaluate``: every book with ``as_of`` <= D (the window starts at the earliest book)
   that holds open positions. A book without positions is *unscorable*: listed, not pending (P6).
4. **Actions**: the pricing gateway lists a dividend or split (ex-date from ``DEFAULT_FROM`` to the
   line's last stored bar) that ``corporate_action`` lacks for the configured corporate-action
   engine. Selected over the **historical** universe (``members_over_range``, T-143 part A item 9):
   the same lines and spans ``backfill-actions`` fetches, so a leaver's missing action is reported
   rather than left to be fetched forever. A line the gateway cannot serve
   (``ActionsUnavailable``/``ActionsNotSupported``) is *unavailable*: the backfill would write
   nothing for it either, so it is listed and does not count (P6). A transport failure is not
   caught: the command exits non-zero and the orchestrator treats that as pending.

Clause 4 makes one GET per line -- about as many as a no-op ``backfill-actions``.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from portfolio_common.db import Database, DatabaseError

from kg_schema.db import connect_ro
from kg_schema.trading_calendar import last_session_on_or_before
from quant.actions import DEFAULT_FROM, fetch_corporate_actions_gateway
from quant.config import QuantSettings
from quant.db import (
    ActionTarget,
    load_action_targets,
    load_book_weights,
    load_forward_simple_returns,
)
from quant.evaluate import LIVE_BOOK_KIND, PERF_ENGINE_VERSION
from quant.pricing_client import ActionsNotSupported, ActionsUnavailable, QuantPricingClient

_SHOWN = 3
_TABLES = (
    "price_daily",
    "quant_return_daily",
    "quant_portfolio",
    "quant_position",
    "quant_benchmark_performance",
    "corporate_action",
)


def _named(items: list[str]) -> str:
    return ", ".join(items[:_SHOWN]) + (", ..." if len(items) > _SHOWN else "")


def _ids(asset_ids: list[int]) -> str:
    return json.dumps(sorted(asset_ids))


def _returns_lag(conn: Database, settings: QuantSettings, ids: list[int], to: str) -> str | None:
    prices_to = conn.execute(
        "SELECT MAX(date) AS d FROM price_daily WHERE date <= ? "
        "AND asset_id IN (SELECT value FROM json_each(?))",
        (to, _ids(ids)),
    ).fetchone()["d"]
    returns_to = conn.execute(
        "SELECT MAX(obs_date) AS d FROM quant_return_daily WHERE obs_date <= ? "
        "AND engine_version = ? AND asset_id IN (SELECT value FROM json_each(?))",
        (to, settings.return_engine_version, _ids(ids)),
    ).fetchone()["d"]
    if prices_to is None:
        return f"no member price_daily bar on or before {to}"
    if returns_to is None or returns_to < prices_to:
        return f"returns end {returns_to}, prices {prices_to}"
    return None


def _missing_books(conn: Database, settings: QuantSettings, session: str, to: str) -> str | None:
    wanted = [o for o in settings.objectives if o != "frontier"]
    have = {
        str(r["kind"])
        for r in conn.execute(
            "SELECT DISTINCT kind FROM quant_portfolio "
            "WHERE kind <> ? AND as_of >= ? AND as_of <= ?",
            (LIVE_BOOK_KIND, session, to),
        )
    }
    missing = [o for o in wanted if o not in have]
    return f"no book for {_named(missing)} in [{session}, {to}]" if missing else None


def _evaluate_lag(conn: Database, settings: QuantSettings, to: str) -> tuple[list[str], list[str]]:
    """``(lagging, unscorable)`` book labels, over the books ``evaluate`` would score."""
    lagging: list[str] = []
    unscorable: list[str] = []
    for book in conn.execute(
        "SELECT id, kind, as_of FROM quant_portfolio WHERE as_of <= ? ORDER BY id", (to,)
    ).fetchall():
        weights = load_book_weights(conn, int(book["id"]))
        label = f"{book['kind']}#{book['id']}"
        if not weights:
            unscorable.append(label)
            continue
        forward = load_forward_simple_returns(
            conn,
            list(weights),
            after=str(book["as_of"]),
            until=to,
            engine_version=settings.return_engine_version,
        )
        scored_to = conn.execute(
            "SELECT MAX(date) AS d FROM quant_benchmark_performance "
            "WHERE portfolio_id = ? AND engine_version = ?",
            (int(book["id"]), PERF_ENGINE_VERSION),
        ).fetchone()["d"]
        if forward and (scored_to is None or scored_to < max(forward)):
            lagging.append(label)
    return lagging, unscorable


def _gateway_actions(
    conn: Database,
    settings: QuantSettings,
    targets: list[ActionTarget],
    to: str,
    client: QuantPricingClient,
) -> tuple[str | None, list[str]]:
    """``(reason, unavailable)``: the first dividend or split the gateway lists and the database
    lacks, and the lines the gateway cannot serve. Each line is read over its own span (its last
    stored bar), the same span ``backfill-actions`` fetches."""
    stored: dict[int, set[tuple[str, str]]] = {}
    for r in conn.execute(
        "SELECT asset_id, action_type, ex_date FROM corporate_action "
        "WHERE engine_version = ? AND ex_date <= ?",
        (settings.corpact_engine_version, to),
    ):
        stored.setdefault(int(r["asset_id"]), set()).add((str(r["action_type"]), str(r["ex_date"])))
    unavailable: list[str] = []
    for target in targets:
        try:
            rows = fetch_corporate_actions_gateway(
                client, target.asset_id, target.ticker, target.start, target.end
            )
        except (ActionsUnavailable, ActionsNotSupported):
            unavailable.append(target.ticker)
            continue
        listed = {(r.action_type, r.ex_date) for r in rows}
        new = sorted(listed - stored.get(target.asset_id, set()))
        if new:
            kind, ex_date = new[0]
            return (
                f"{target.ticker} has {kind} {ex_date} at the gateway, not in corporate_action",
                [],
            )
    return None, unavailable


def check(
    conn: Database,
    settings: QuantSettings,
    analysis_date: str,
    *,
    client: QuantPricingClient,
) -> tuple[bool, str]:
    for table in _TABLES:
        if not conn.relation_exists(table):
            return True, f"no {table} table (fresh database)"
    try:
        targets = load_action_targets(
            conn,
            universe=settings.universe,
            date_from=DEFAULT_FROM,
            date_to=analysis_date,
            universe_db_path=settings.universe_db_path,
        )
    except RuntimeError as exc:  # no members, or none with an assets row yet
        return True, str(exc)
    session = last_session_on_or_before(date.fromisoformat(analysis_date)).isoformat()

    reason = _returns_lag(
        conn, settings, [t.asset_id for t in targets], analysis_date
    ) or _missing_books(conn, settings, session, analysis_date)
    if reason is not None:
        return True, reason
    lagging, unscorable = _evaluate_lag(conn, settings, analysis_date)
    if lagging:
        return True, f"evaluate lags for {len(lagging)} books ({_named(lagging)})"
    reason, unavailable = _gateway_actions(conn, settings, targets, analysis_date, client)
    if reason is not None:
        return True, reason

    extras = []
    if unavailable:
        extras.append(f"{len(unavailable)} unavailable ({_named(unavailable)})")
    if unscorable:
        extras.append(f"{len(unscorable)} unscorable ({_named(unscorable)})")
    tail = f"; {'; '.join(extras)}" if extras else ""
    wanted = [o for o in settings.objectives if o != "frontier"]
    return False, (
        f"returns, {len(wanted)} books and actions current at {session} "
        f"({len(targets)} members){tail}"
    )


def pending_line(
    settings: QuantSettings, analysis_date: str, *, client: QuantPricingClient | None = None
) -> str:
    if not Path(settings.db_path).exists():
        return f"pending: yes {settings.db_path} does not exist (fresh database)"
    conn = connect_ro(settings.db_path)
    owns = client is None
    gateway = client or QuantPricingClient(settings.pricing_base_url)
    try:
        try:
            pending, evidence = check(conn, settings, analysis_date, client=gateway)
        except DatabaseError as exc:  # a partial schema is a P2 doubt
            return f"pending: yes cannot read the database ({exc})"
        return f"pending: {'yes' if pending else 'no'} {evidence}"
    finally:
        if owns:
            gateway.close()
        conn.close()
