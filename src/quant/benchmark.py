"""Internal equal-weight benchmark synthesized from the same total-return panel.

``SP500_EW_INTERNAL`` is an equal-weight, daily-rebalanced index over the gated
universe -- a self-consistent yardstick for the Markowitz books (same names, same
return basis, no vendor dependency). A real SPX / SPY_TR series can be loaded into
``benchmark_series`` from a CSV later.
"""

from __future__ import annotations

import math

from portfolio_common.db import Database

from quant.db import upsert_benchmark_series

INTERNAL_EW = "SP500_EW_INTERNAL"


def build_internal_benchmark(  # noqa: PLR0913 - keyword-only knobs with defaults
    conn: Database,
    *,
    date_from: str,
    date_to: str,
    return_engine_version: str = "qret-v1",
    engine_version: str = "bench-v1",
    benchmark: str = INTERNAL_EW,
) -> int:
    """Equal-weight mean of ``tr_log_return`` across all names present each day."""
    rows = conn.execute(
        "SELECT obs_date, AVG(tr_log_return) AS lr FROM quant_return_daily "
        "WHERE engine_version = ? AND obs_date >= ? AND obs_date <= ? "
        "AND tr_log_return IS NOT NULL GROUP BY obs_date ORDER BY obs_date",
        (return_engine_version, date_from, date_to),
    ).fetchall()
    level = 1.0
    out: list[tuple[str, float, float]] = []
    for r in rows:
        lr = float(r["lr"])
        level *= math.exp(lr)
        out.append((str(r["obs_date"]), lr, level))
    return upsert_benchmark_series(
        conn, benchmark, out, engine_version=engine_version, source="quant-internal-ew"
    )
