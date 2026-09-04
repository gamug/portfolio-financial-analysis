"""Forward evaluation: each persisted book's realized return vs the internal
benchmark and vs the live ``portfolio_position`` book.

Weights are frozen at the book's as-of date; realized daily return is the
weighted simple total return of the held names. The live cycle book is snapshotted
into ``quant_portfolio(kind='live_book')`` at *date_from* so the comparison is a
single join (``v_quant_vs_live`` / ``v_quant_benchmark_performance``).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from portfolio_common.kg_schema import connect
from portfolio_common.kg_schema.provenance import code_version

from quant.benchmark import INTERNAL_EW, build_internal_benchmark
from quant.config import QuantSettings
from quant.db import (
    PortfolioRow,
    ensure_schema,
    insert_portfolio,
    load_benchmark_returns,
    load_book_weights,
    load_forward_simple_returns,
    load_live_book,
    sync_positions,
    upsert_benchmark_performance,
)
from quant.state import fail_run, finish_run, open_run

PERF_ENGINE_VERSION = "perf-v1"


@dataclass
class EvaluateResult:
    benchmark_rows: int
    books_evaluated: int
    perf_rows: int
    live_book_id: int | None


def _evaluate_book(  # noqa: PLR0913 - keyword-only knobs
    conn: sqlite3.Connection,
    portfolio_id: int,
    as_of: str,
    *,
    date_to: str,
    benchmark: str,
    return_engine_version: str,
) -> int:
    weights = load_book_weights(conn, portfolio_id)
    if not weights:
        return 0
    fwd = load_forward_simple_returns(
        conn, list(weights), after=as_of, until=date_to, engine_version=return_engine_version
    )
    bench = load_benchmark_returns(conn, benchmark, after=as_of, until=date_to)
    cumulative = 1.0
    rows: list[tuple[str, float, float, str | None, float | None, float | None]] = []
    for d in sorted(fwd):
        realized = sum(w * fwd[d].get(a, 0.0) for a, w in weights.items())
        cumulative *= 1.0 + realized
        br = bench.get(d)
        active = None if br is None else realized - br
        rows.append((d, realized, cumulative - 1.0, benchmark, br, active))
    return upsert_benchmark_performance(
        conn, portfolio_id, rows, engine_version=PERF_ENGINE_VERSION
    )


def _snapshot_live_book(
    conn: sqlite3.Connection, settings: QuantSettings, date_from: str, run_id: int
) -> int | None:
    live = load_live_book(conn, date_from)
    if not live:
        return None
    pid = insert_portfolio(
        conn,
        PortfolioRow(
            as_of=date_from,
            kind="live_book",
            objective="live",
            solver="n/a",
            status="snapshot",
            expected_return=None,
            expected_vol=None,
            sharpe=None,
            rf_annual=None,
            n_positions=len(live),
            engine_version=settings.optimizer_engine_version,
            quant_run_id=run_id,
        ),
    )
    sync_positions(conn, pid, date_from, live)
    return pid


def run_evaluate(
    settings: QuantSettings,
    *,
    date_from: str,
    date_to: str,
    conn: sqlite3.Connection | None = None,
    benchmark: str = INTERNAL_EW,
) -> EvaluateResult:
    owns = conn is None
    conn = conn or connect(settings.db_path)
    try:
        ensure_schema(conn)
        run_id = open_run(
            conn,
            "evaluate",
            as_of=date_to,
            params={
                "analysis_date": date_to,
                "date_from": date_from,
                "date_to": date_to,
                "benchmark": benchmark,
            },
            code_version=code_version(),
        )
        try:
            bench_rows = build_internal_benchmark(
                conn,
                date_from=date_from,
                date_to=date_to,
                return_engine_version=settings.return_engine_version,
                engine_version=settings.benchmark_engine_version,
                benchmark=benchmark,
            )
            live_id = _snapshot_live_book(conn, settings, date_from, run_id)

            books = conn.execute(
                "SELECT id, as_of FROM quant_portfolio WHERE as_of >= ? AND as_of <= ?",
                (date_from, date_to),
            ).fetchall()
            perf_rows = 0
            evaluated = 0
            for b in books:
                added = _evaluate_book(
                    conn,
                    int(b["id"]),
                    str(b["as_of"]),
                    date_to=date_to,
                    benchmark=benchmark,
                    return_engine_version=settings.return_engine_version,
                )
                perf_rows += added
                evaluated += 1 if added else 0
            finish_run(conn, run_id)
        except Exception as exc:
            fail_run(conn, run_id, str(exc))
            raise
        return EvaluateResult(
            benchmark_rows=bench_rows,
            books_evaluated=evaluated,
            perf_rows=perf_rows,
            live_book_id=live_id,
        )
    finally:
        if owns:
            conn.close()
