"""Total-return daily series: split-adjusted close with cash dividends folded in.

``price_daily.close`` is split-adjusted but not dividend-adjusted. For a day *t*
with cash dividend ``D_t`` on its ex-date:

    price_log_return_t = ln(C_t / C_{t-1})
    tr_log_return_t    = ln((C_t + D_t) / C_{t-1})       <- the optimizer input
    tr_index_t         = tr_index_{t-1} * (C_t + D_t) / C_{t-1},  tr_index_0 = C_0

``adj_close`` is the back-adjusted total-return price (most recent day = raw close):

    adj_close_t = C_t * prod_{s: ex_date(s) > date(t)} (1 - D_s / C_s)
"""

from __future__ import annotations

import math

from portfolio_common.db import Database

from kg_schema import connect
from kg_schema.provenance import code_version
from quant.config import QuantSettings
from quant.db import (
    ReturnRow,
    ensure_schema,
    load_actions,
    load_assets,
    load_daily_closes,
    upsert_return_daily,
)
from quant.state import fail_run, finish_run, open_run


def _snap_to_calendar(dates: list[str], events: dict[str, float]) -> dict[str, float]:
    """Move each event to the first trading day on or after its date (needed for
    the derive path's synthetic ex-dates, which rarely land on a trading day);
    accumulate when several snap to the same day."""
    if not events:
        return {}
    out: dict[str, float] = {}
    for raw, val in sorted(events.items()):
        snapped = next((d for d in dates if d >= raw), None)
        if snapped is not None:
            out[snapped] = out.get(snapped, 0.0) + val
    return out


def build_total_return_series(
    closes: list[tuple[str, float]],
    dividends: dict[str, float],
    splits: dict[str, float],
) -> list[ReturnRow]:
    """*closes* is ``(date, split-adjusted close)`` ascending. Returns one
    :class:`ReturnRow` per input day. Dividend / split dates that miss the trading
    calendar are snapped forward to the next trading day."""
    if not closes:
        return []

    cal = [d for d, _ in closes]
    dividends = _snap_to_calendar(cal, dividends)
    splits = _snap_to_calendar(cal, splits)

    # back-adjustment factors: walk from the end accumulating (1 - D_s / C_s)
    factors: list[float] = [1.0] * len(closes)
    acc = 1.0
    for i in range(len(closes) - 1, -1, -1):
        factors[i] = acc
        d = closes[i][0]
        c = closes[i][1]
        div = dividends.get(d, 0.0)
        if div and c > 0:
            acc *= 1.0 - div / c

    rows: list[ReturnRow] = []
    tr_index = closes[0][1]
    for i, (d, c) in enumerate(closes):
        div = dividends.get(d, 0.0)
        split = splits.get(d, 1.0)
        if i == 0:
            rows.append(ReturnRow(d, c, c * factors[0], tr_index, div, split, None, None))
            continue
        prev_c = closes[i - 1][1]
        price_lr = math.log(c / prev_c) if prev_c > 0 else None
        tr_lr = math.log((c + div) / prev_c) if prev_c > 0 else None
        if tr_lr is not None:
            tr_index *= (c + div) / prev_c
        rows.append(ReturnRow(d, c, c * factors[i], tr_index, div, split, price_lr, tr_lr))
    return rows


class ReturnsReport:
    def __init__(self, engine_version: str) -> None:
        self.engine_version = engine_version
        self.assets = 0
        self.rows_written = 0
        self.assets_with_dividends = 0


def run_build_returns(
    settings: QuantSettings,
    *,
    date_from: str,
    date_to: str,
    conn: Database | None = None,
) -> ReturnsReport:
    owns = conn is None
    conn = conn or connect(settings.db_path)
    try:
        ensure_schema(conn)
        report = ReturnsReport(settings.return_engine_version)
        run_id = open_run(
            conn,
            "build-returns",
            as_of=date_to,
            params={"analysis_date": date_to, "date_from": date_from, "date_to": date_to},
            code_version=code_version(),
        )
        try:
            for asset_id, _ticker in load_assets(
                conn,
                universe=settings.universe,
                as_of=date_to,
                universe_db_path=settings.universe_db_path,
            ):
                closes = load_daily_closes(conn, asset_id, start=date_from, end=date_to)
                if not closes:
                    continue
                dividends = load_actions(conn, asset_id, "DIVIDEND", start=date_from, end=date_to)
                splits = load_actions(conn, asset_id, "SPLIT", start=date_from, end=date_to)
                rows = build_total_return_series(closes, dividends, splits)
                report.assets += 1
                if dividends:
                    report.assets_with_dividends += 1
                report.rows_written += upsert_return_daily(
                    conn, asset_id, rows, engine_version=settings.return_engine_version
                )
            finish_run(conn, run_id)
        except Exception as exc:
            fail_run(conn, run_id, str(exc))
            raise
        return report
    finally:
        if owns:
            conn.close()
