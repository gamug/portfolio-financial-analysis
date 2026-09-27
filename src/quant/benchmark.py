"""The benchmarks the optimized books are graded against.

``SP500_EW_INTERNAL`` is an equal-weight, daily-rebalanced index over the **gated panel** --
the names :func:`quant.universe.liquidity_data_gate` admits as of the window's start, on the
same liquidity and history knobs a book is built from, but never a book's own hard-veto
exclusions (:func:`quant.universe.benchmark_gate`, T-108): the benchmark is the investable
universe, not the strategy's own filtered picture of it, so a veto can never show up as alpha
against the yardstick it's graded against -- on the same total-return basis, with no vendor
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

    Day *t*'s return is the mean of ``expm1(tr_log_return)`` over the panel names still
    "alive" that day -- present that day, or with a later return anywhere in the window (a
    one-day price-data gap contributes 0% that day, since its move is folded into the return
    of the day it next reappears, T-111) -- while a name with no later return at all (gone for
    good) is dropped from the panel and the divisor from that day on, the same convention
    ``evaluate.py``'s book-level return uses. The stored ``log_return`` is ``log1p`` of the
    mean and the level compounds ``(1 + r)`` from 1.0."""
    if not asset_ids:
        raise ValueError("the benchmark panel is empty")
    ids = sorted({int(a) for a in asset_ids})
    rows = conn.execute(
        "SELECT asset_id, obs_date, tr_log_return FROM quant_return_daily "  # noqa: S608
        "WHERE engine_version = ? AND obs_date >= ? AND obs_date <= ? "
        f"AND tr_log_return IS NOT NULL AND asset_id IN {in_clause(ids)} "
        "ORDER BY obs_date",
        (return_engine_version, date_from, date_to, *ids),
    ).fetchall()
    by_day: dict[str, dict[int, float]] = {}
    last_seen: dict[int, str] = {}
    for r in rows:
        day = str(r["obs_date"])
        aid = int(r["asset_id"])
        by_day.setdefault(day, {})[aid] = math.expm1(float(r["tr_log_return"]))
        if aid not in last_seen or day > last_seen[aid]:
            last_seen[aid] = day
    level = 1.0
    out: list[tuple[str, float | None, float]] = []
    for day in sorted(by_day):
        survivors = [a for a in ids if last_seen.get(a, "") >= day]
        mean = math.fsum(by_day[day].get(a, 0.0) for a in survivors) / len(survivors)
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
