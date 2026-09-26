"""Forward evaluation: each persisted book's realized return vs a benchmark and vs the live
``portfolio_position`` book.

The benchmark is the internal equal-weight index over the gated panel as of the window's
start (``SP500_EW_INTERNAL``, rebuilt on every run, T-108), or an external series already
loaded with ``quant load-benchmark`` (e.g. ``SPY_TR``), read -- never overwritten -- here.

Weights are frozen at the book's as-of date; realized daily return is the
weighted simple total return of the held names. The live cycle book is snapshotted
into ``quant_portfolio(kind='live_book')`` at *date_from* so the comparison is a
single join (``v_quant_vs_live`` / ``v_quant_benchmark_performance``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from portfolio_common.db import Database

from kg_schema import connect
from kg_schema.provenance import code_version
from quant.benchmark import INTERNAL_EW, build_internal_benchmark
from quant.config import QuantSettings
from quant.db import (
    PortfolioRow,
    benchmark_engine_versions,
    earliest_portfolio_as_of,
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
from quant.universe import settings_gate

# perf-v2 (T-108): active returns against bench-v2 (or a loaded external series); perf-v1's
# were against bench-v1's mean-of-log index, so they stay as written, under their version.
PERF_ENGINE_VERSION = "perf-v2"


@dataclass
class EvaluateResult:
    date_from: str  # the resolved value, even when the caller omitted --from
    benchmark_rows: int  # rows (re)built; 0 for a loaded external series
    benchmark_version: str
    panel: list[int]  # the internal benchmark's gated panel; empty for an external series
    books_evaluated: int
    perf_rows: int
    live_book_id: int | None


def _evaluate_book(  # noqa: PLR0913 - keyword-only knobs
    conn: Database,
    portfolio_id: int,
    as_of: str,
    *,
    date_to: str,
    benchmark: str,
    benchmark_version: str,
    return_engine_version: str,
) -> int:
    weights = load_book_weights(conn, portfolio_id)
    if not weights:
        return 0
    fwd = load_forward_simple_returns(
        conn, list(weights), after=as_of, until=date_to, engine_version=return_engine_version
    )
    bench = load_benchmark_returns(
        conn, benchmark, after=as_of, until=date_to, engine_version=benchmark_version
    )
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
    conn: Database, settings: QuantSettings, date_from: str, run_id: int
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


def _benchmark(
    conn: Database, settings: QuantSettings, benchmark: str, *, date_from: str, date_to: str
) -> tuple[int, str, list[int]]:
    """``(rows built, version to read, panel)`` for *benchmark* over the window. The internal
    index is rebuilt over the gate as of *date_from* -- the names a book built that day could
    hold; an external series must already be loaded and is only read."""
    if benchmark == INTERNAL_EW:
        gate = settings_gate(conn, settings, as_of=date_from)
        if not gate.asset_ids:
            raise ValueError(f"the benchmark's universe gate is empty as of {date_from}")
        rows = build_internal_benchmark(
            conn,
            asset_ids=gate.asset_ids,
            date_from=date_from,
            date_to=date_to,
            return_engine_version=settings.return_engine_version,
            engine_version=settings.benchmark_engine_version,
            benchmark=benchmark,
        )
        return rows, settings.benchmark_engine_version, gate.asset_ids
    versions = benchmark_engine_versions(conn, benchmark, after=date_from, until=date_to)
    if not versions:
        raise ValueError(
            f"no {benchmark} returns stored in ({date_from}, {date_to}] -- load the series "
            "with 'quant load-benchmark' first"
        )
    return 0, versions[0], []


def run_evaluate(
    settings: QuantSettings,
    *,
    date_from: str | None = None,
    date_to: str,
    conn: Database | None = None,
    benchmark: str = INTERNAL_EW,
) -> EvaluateResult:
    """*date_from* defaults to the earliest persisted ``quant_portfolio.as_of``
    (Q2, docs/model_fixes.md) when omitted -- maximizes the forward window
    actually evaluated, rather than a caller picking a date (e.g. the same
    ``as_of`` an `optimize` run just used) that leaves no room between it and
    whatever price data has actually been ingested. Raises ``ValueError`` if
    omitted and no book has been persisted yet."""
    owns = conn is None
    conn = conn or connect(settings.db_path)
    try:
        ensure_schema(conn)
        if date_from is None:
            date_from = earliest_portfolio_as_of(conn)
            if date_from is None:
                raise ValueError(
                    "no --from given and no quant_portfolio rows exist yet -- "
                    "run 'quant optimize' first, or pass --from explicitly"
                )
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
            bench_rows, bench_version, panel = _benchmark(
                conn, settings, benchmark, date_from=date_from, date_to=date_to
            )
            conn.execute(
                "UPDATE quant_run SET params_json = json_set(params_json, "
                "'$.benchmark_version', ?, '$.benchmark_panel', json(?)) WHERE id = ?",
                (bench_version, json.dumps(panel), run_id),
            )
            conn.commit()
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
                    benchmark_version=bench_version,
                    return_engine_version=settings.return_engine_version,
                )
                perf_rows += added
                evaluated += 1 if added else 0
            finish_run(conn, run_id)
        except Exception as exc:
            fail_run(conn, run_id, str(exc))
            raise
        return EvaluateResult(
            date_from=date_from,
            benchmark_rows=bench_rows,
            benchmark_version=bench_version,
            panel=panel,
            books_evaluated=evaluated,
            perf_rows=perf_rows,
            live_book_id=live_id,
        )
    finally:
        if owns:
            conn.close()
