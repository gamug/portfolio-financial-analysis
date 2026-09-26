"""The benchmarks the optimized books are graded against.

``SP500_EW_INTERNAL`` is an equal-weight, daily-rebalanced index over the **gated panel** --
the names :func:`quant.universe.liquidity_data_gate` admits as of the window's start, the
same gate every book is built from -- on the same total-return basis, with no vendor
dependency. Each day it returns the mean of the panel's **simple** total returns, and its level
compounds ``(1 + r)`` (T-108). ``bench-v1`` averaged *log* returns over every name with a row,
which is the geometric, not the arithmetic, mean: lower by about half the cross-sectional
variance every day (-4.34 pp/yr on the 20-name production panel).

An external series -- a cap-weighted total-return index such as SPY_TR -- is loaded from a
CSV by :func:`load_benchmark_csv` (``quant load-benchmark``); obtaining the file is a
data-acquisition step outside this repo.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from portfolio_common.db import Database, in_clause

from quant.db import upsert_benchmark_series

INTERNAL_EW = "SP500_EW_INTERNAL"
EXTERNAL_ENGINE_VERSION = "csv-v1"


class BenchmarkCsvError(ValueError):
    """The CSV is not a clean, dated, positive total-return level series."""


def build_internal_benchmark(  # noqa: PLR0913 - keyword-only knobs with defaults
    conn: Database,
    *,
    asset_ids: Sequence[int],
    date_from: str,
    date_to: str,
    return_engine_version: str = "qret-v2",
    engine_version: str = "bench-v2",
    benchmark: str = INTERNAL_EW,
) -> int:
    """Equal-weight, daily-rebalanced index over *asset_ids* (the gated panel).

    Day *t*'s return is the mean of ``expm1(tr_log_return)`` over the panel names with a
    return that day (a name with none that day is left out of that day's mean, not counted
    as 0%); the stored ``log_return`` is ``log1p`` of it and the level compounds
    ``(1 + r)`` from 1.0."""
    if not asset_ids:
        raise ValueError("the benchmark panel is empty")
    ids = sorted({int(a) for a in asset_ids})
    rows = conn.execute(
        "SELECT obs_date, tr_log_return FROM quant_return_daily "  # noqa: S608 - placeholders only
        "WHERE engine_version = ? AND obs_date >= ? AND obs_date <= ? "
        f"AND tr_log_return IS NOT NULL AND asset_id IN {in_clause(ids)} "
        "ORDER BY obs_date",
        (return_engine_version, date_from, date_to, *ids),
    ).fetchall()
    by_day: dict[str, list[float]] = {}
    for r in rows:
        by_day.setdefault(str(r["obs_date"]), []).append(math.expm1(float(r["tr_log_return"])))
    level = 1.0
    out: list[tuple[str, float | None, float]] = []
    for day in sorted(by_day):
        simple = by_day[day]
        mean = math.fsum(simple) / len(simple)
        level *= 1.0 + mean
        out.append((day, math.log1p(mean), level))
    return upsert_benchmark_series(
        conn, benchmark, out, engine_version=engine_version, source="quant-internal-ew"
    )


def read_benchmark_csv(path: str | Path) -> list[tuple[str, float, float | None]]:
    """``(obs_date, total_return_level, log_return)`` rows from a CSV with a ``date`` and a
    ``total_return_level`` column; the first row has no return. Refuses anything but ISO
    dates in strictly increasing order and positive, finite levels."""
    with Path(path).open(newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or not {"date", "total_return_level"} <= set(reader.fieldnames):
            raise BenchmarkCsvError("the CSV needs 'date' and 'total_return_level' columns")
        out: list[tuple[str, float, float | None]] = []
        prev: tuple[str, float] | None = None
        for line, row in enumerate(reader, start=2):
            try:
                day = date.fromisoformat(row["date"].strip()).isoformat()
                level = float(row["total_return_level"])
            except (ValueError, AttributeError) as exc:
                raise BenchmarkCsvError(f"line {line}: {exc}") from exc
            if not math.isfinite(level) or level <= 0:
                raise BenchmarkCsvError(f"line {line}: level {level} is not positive")
            if prev is not None and day <= prev[0]:
                raise BenchmarkCsvError(f"line {line}: {day} does not follow {prev[0]}")
            out.append((day, level, None if prev is None else math.log(level / prev[1])))
            prev = (day, level)
    if not out:
        raise BenchmarkCsvError("the CSV has no rows")
    return out


def load_benchmark_csv(
    conn: Database,
    path: str | Path,
    *,
    benchmark: str,
    engine_version: str = EXTERNAL_ENGINE_VERSION,
) -> int:
    """Load an external total-return series (e.g. ``SPY_TR``) into ``benchmark_series``."""
    if benchmark == INTERNAL_EW:
        raise ValueError(f"{INTERNAL_EW} is synthesized, not loaded")
    rows = [(d, lr, level) for d, level, lr in read_benchmark_csv(path)]
    return upsert_benchmark_series(
        conn,
        benchmark,
        rows,
        engine_version=engine_version,
        source=f"csv:{Path(path).name}",
    )
