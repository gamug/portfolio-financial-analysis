"""Risk-free rate for Sharpe / tangency.

v1 default: a single constant ``CONST`` curve point per as-of, from
``QuantSettings.risk_free_rate``. ``--rf-source csv`` loads a real short-rate
series (``date,annualized_rate`` rows, decimals) into ``risk_free_rate`` and picks
the point on or before the as-of date. The daily rate uses the geometric
convention ``(1 + annual) ** (1 / periods_per_year) - 1``.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from portfolio_common.db import Database

from quant.config import QuantSettings

RF_ENGINE_VERSION = "rf-v1"
_MIN_CSV_COLS = 2


@dataclass(frozen=True)
class RiskFree:
    curve: str
    rate_date: str
    annualized_rate: float
    daily_rate: float
    source: str


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _daily(annual: float, periods_per_year: int) -> float:
    return float((1.0 + annual) ** (1.0 / periods_per_year)) - 1.0


def _upsert(conn: Database, curve: str, rate_date: str, rate: float, source: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO risk_free_rate
            (curve, rate_date, annualized_rate, source, engine_version, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (curve, rate_date, rate, source, RF_ENGINE_VERSION, _now()),
    )


def _load_csv(path: Path) -> list[tuple[str, float]]:
    with path.open(newline="") as fh:
        reader = csv.reader(fh)
        rows: list[tuple[str, float]] = []
        for raw in reader:
            if len(raw) < _MIN_CSV_COLS or not raw[0][:4].isdigit():
                continue
            rows.append((raw[0].strip(), float(raw[1])))
    return sorted(rows)


def load_risk_free(settings: QuantSettings, *, as_of: str, conn: Database) -> RiskFree:
    """Resolve (and persist) the risk-free rate for *as_of*."""
    if settings.rf_source == "csv":
        if settings.rf_csv_path is None:
            raise RuntimeError("rf_source='csv' needs rf_csv_path")
        points = _load_csv(settings.rf_csv_path)
        for d, r in points:
            _upsert(conn, "US3M", d, r, f"csv:{settings.rf_csv_path.name}")
        conn.commit()
        usable = [p for p in points if p[0] <= as_of] or points
        rate_date, annual = usable[-1]
        return RiskFree("US3M", rate_date, annual, _daily(annual, settings.periods_per_year), "csv")

    annual = settings.risk_free_rate
    _upsert(conn, "CONST", as_of, annual, "constant-v1")
    conn.commit()
    return RiskFree(
        "CONST", as_of, annual, _daily(annual, settings.periods_per_year), "constant-v1"
    )
