"""Data-coverage report for a point-in-time universe.

The S&P 500 universe is now a mutable, dated list (``universe.db``). A run for an
`analysis_date` selects the constituents as of that date -- but the core data
stores (EDGAR filings, pricing, derived observations) are filled incrementally by
prior agent runs, so a member as of ``D`` may have **no** rows to analyse. The
ingesting agents (`fundamental_agent`, `pricing_agent`) fetch their own data and
log a skip; the reading agents (`cycle`, `quant`) silently run on whatever subset
happens to have data.

:func:`check_coverage` makes that explicit: per member, does it have an ``assets``
identity row, a FUNDAMENTAL score, fundamental metrics, daily prices, enough
derived observations, and a total-return series -- all as of ``D``.
:func:`persist_coverage` writes the result to ``universe_coverage``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from kg_schema.universe_source import resolve_asset_ids, symbols_asof

# A member counts as "covered" when every one of these checks passes. `metrics`
# and `returns` are reported but not required (cycle's VALORIZATION uses metrics
# when present; quant's own gate already enforces the return history).
REQUIRED_CHECKS = ("assets", "fundamental", "pricing", "observations")
ALL_CHECKS = ("assets", "fundamental", "metrics", "pricing", "observations", "returns")

DEFAULT_MIN_OBSERVATION_DAYS = 504  # ~2y of trading days; mirrors quant's min_history_days


@dataclass(frozen=True)
class SymbolCoverage:
    symbol: str
    asset_id: int | None
    checks: dict[str, bool]

    @property
    def covered(self) -> bool:
        return all(self.checks[c] for c in REQUIRED_CHECKS)

    @property
    def missing(self) -> list[str]:
        return [c for c in ALL_CHECKS if not self.checks.get(c, False)]

    @property
    def missing_required(self) -> list[str]:
        return [c for c in REQUIRED_CHECKS if not self.checks.get(c, False)]


@dataclass
class CoverageReport:
    as_of: str
    universe: str
    min_observation_days: int
    rows: list[SymbolCoverage] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def covered(self) -> int:
        return sum(1 for r in self.rows if r.covered)

    @property
    def fraction(self) -> float:
        return self.covered / self.total if self.total else 1.0

    @property
    def uncovered(self) -> list[SymbolCoverage]:
        return [r for r in self.rows if not r.covered]

    def missing_for(self, check: str) -> list[str]:
        return [r.symbol for r in self.rows if not r.checks.get(check, False)]


def _distinct_ids(conn: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> set[int]:
    try:
        return {int(r[0]) for r in conn.execute(sql, params)}
    except sqlite3.OperationalError:  # table not created in this DB yet
        return set()


def check_coverage(
    fin_conn: sqlite3.Connection,
    universe_conn: sqlite3.Connection,
    as_of: str,
    *,
    universe: str = "SP500",
    min_observation_days: int = DEFAULT_MIN_OBSERVATION_DAYS,
) -> CoverageReport:
    """Per-member core-data coverage for *universe* as of *as_of*."""
    symbols = symbols_asof(universe_conn, as_of, universe=universe)
    id_by_symbol, _missing = resolve_asset_ids(fin_conn, symbols)

    fundamental_ids = _distinct_ids(
        fin_conn,
        "SELECT DISTINCT asset_id FROM score_snapshot "
        "WHERE score_type = 'FUNDAMENTAL' AND event_time <= ?",
        (as_of,),
    )
    metric_ids = _distinct_ids(
        fin_conn,
        "SELECT DISTINCT f.asset_id FROM fundamental_metrics m "
        "JOIN sec_filings f ON f.id = m.filing_id "
        "WHERE f.period_end IS NOT NULL AND f.period_end <= ?",
        (as_of,),
    )
    price_ids = _distinct_ids(
        fin_conn, "SELECT DISTINCT asset_id FROM price_daily WHERE date <= ?", (as_of,)
    )
    return_ids = _distinct_ids(
        fin_conn, "SELECT DISTINCT asset_id FROM quant_return_daily WHERE obs_date <= ?", (as_of,)
    )
    obs_counts: dict[int, int] = {}
    try:
        for r in fin_conn.execute(
            "SELECT asset_id, COUNT(*) FROM price_observation WHERE obs_date <= ? GROUP BY asset_id",
            (as_of,),
        ):
            obs_counts[int(r[0])] = int(r[1])
    except sqlite3.OperationalError:
        pass

    rows: list[SymbolCoverage] = []
    for symbol in symbols:
        aid = id_by_symbol.get(symbol.upper())
        checks = {
            "assets": aid is not None,
            "fundamental": aid in fundamental_ids,
            "metrics": aid in metric_ids,
            "pricing": aid in price_ids,
            "observations": obs_counts.get(aid or -1, 0) >= min_observation_days,
            "returns": aid in return_ids,
        }
        rows.append(SymbolCoverage(symbol=symbol, asset_id=aid, checks=checks))

    return CoverageReport(
        as_of=as_of,
        universe=universe,
        min_observation_days=min_observation_days,
        rows=rows,
    )


def persist_coverage(
    fin_conn: sqlite3.Connection, report: CoverageReport, *, run_id: int | None = None
) -> int:
    """Upsert one ``universe_coverage`` row per member (keyed by
    ``(as_of, universe, symbol)``). Returns the row count written."""
    now = datetime.now(tz=UTC).isoformat(timespec="seconds")
    payload = [
        (
            report.as_of,
            report.universe,
            r.symbol,
            r.asset_id,
            int(r.checks["assets"]),
            int(r.checks["fundamental"]),
            int(r.checks["metrics"]),
            int(r.checks["pricing"]),
            int(r.checks["observations"]),
            int(r.checks["returns"]),
            int(r.covered),
            json.dumps(r.missing),
            now,
            run_id,
        )
        for r in report.rows
    ]
    fin_conn.executemany(
        """
        INSERT INTO universe_coverage
            (as_of, universe, symbol, asset_id, in_assets, has_fundamental, has_metrics,
             has_pricing, has_observations, has_returns, covered, missing_json, checked_at, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (as_of, universe, symbol) DO UPDATE SET
            asset_id = excluded.asset_id, in_assets = excluded.in_assets,
            has_fundamental = excluded.has_fundamental, has_metrics = excluded.has_metrics,
            has_pricing = excluded.has_pricing, has_observations = excluded.has_observations,
            has_returns = excluded.has_returns, covered = excluded.covered,
            missing_json = excluded.missing_json, checked_at = excluded.checked_at,
            run_id = excluded.run_id
        """,
        payload,
    )
    fin_conn.commit()
    return len(payload)
