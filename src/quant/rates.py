"""Risk-free rate for Sharpe / tangency.

v1 default: a single constant ``CONST`` curve point per as-of, from
``QuantSettings.risk_free_rate``. ``--rf-source csv`` loads a real short-rate
series (``date,annualized_rate`` rows, decimals) into ``risk_free_rate`` and picks
the point on or before the as-of date. The daily rate uses the geometric
convention ``(1 + annual) ** (1 / periods_per_year) - 1``.

``load-rf`` (BACKTEST_SPEC §8.2) loads Kenneth French's own daily ``RF`` from the
vendored ``F-F_Research_Data_Factors_daily.csv`` into the ``FF_RF_DAILY`` curve:
one row per session, ``annualized_rate`` = the daily fraction x 252 (the flat
annualization the spec fixes, not the geometric convention above), source
``french-ff3-daily``, ``engine_version = rf-v2``. The backtest driver reads this
curve directly for the cash return. The load is idempotent: a re-run over an
unchanged file writes nothing.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from portfolio_common.db import Database

from quant.config import QuantSettings

RF_ENGINE_VERSION = "rf-v1"
FF_RF_CURVE = "FF_RF_DAILY"
FF_RF_ENGINE_VERSION = "rf-v2"
FF_RF_SOURCE = "french-ff3-daily"
FF_RF_FILE = "F-F_Research_Data_Factors_daily.csv"
FF_RF_MANIFEST = "manifest.json"
FF_RF_DAILY_DAYS_PER_YEAR = 252  # BACKTEST_SPEC §8.2: annualized_rate = daily fraction x 252
_DATA_DIR = Path(__file__).resolve().parent / "data"
_MIN_CSV_COLS = 2
_RF_COL = 4  # the FF3 file's columns are Date, Mkt-RF, SMB, HML, RF
_DATE_LEN = 8
_PCT = 100.0
_MISSING_SENTINEL = -99.0  # French marks missing data -99.99 / -999
_RF_SOURCES = ("constant", "csv")


class RiskFreeUnavailable(RuntimeError):
    """No risk-free rate was known as of the date (T-077, audit Q3)."""


class RiskFreeDataError(RuntimeError):
    """The French RF file is missing, malformed, or not the pinned one."""


@dataclass(frozen=True)
class RiskFree:
    curve: str
    rate_date: str
    annualized_rate: float
    daily_rate: float
    source: str


@dataclass(frozen=True)
class RfLoadReport:
    curve: str
    first_date: str
    last_date: str
    n_rows: int
    n_written: int  # inserted or changed by this call
    library_version: str
    sha256: str


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
    """Resolve (and persist) the risk-free rate for *as_of*.

    An ``rf_source`` this function does not know is an error, never a silent fall back to the
    constant: a typo would otherwise quietly price every Sharpe and tangency off 4.5 %."""
    if settings.rf_source not in _RF_SOURCES:
        raise ValueError(
            f"unknown rf_source {settings.rf_source!r}; expected one of "
            f"{', '.join(_RF_SOURCES)} (the backtest's French daily curve loads with "
            "`python -m quant load-rf` and is read from risk_free_rate directly)"
        )
    if settings.rf_source == "csv":
        if settings.rf_csv_path is None:
            raise RuntimeError("rf_source='csv' needs rf_csv_path")
        points = _load_csv(settings.rf_csv_path)
        usable = [p for p in points if p[0] <= as_of]
        if not usable:
            # the series starts after the as-of: any rate in it postdates the as-of (the old fallback
            # took the series' newest) and would be lookahead
            first = points[0][0] if points else "no rows"
            raise RiskFreeUnavailable(
                f"the risk-free series {settings.rf_csv_path.name} starts {first}, after the "
                f"as-of date {as_of}; refusing to use a rate that was not known then"
            )
        for d, r in points:
            _upsert(conn, "US3M", d, r, f"csv:{settings.rf_csv_path.name}")
        conn.commit()
        rate_date, annual = usable[-1]
        return RiskFree("US3M", rate_date, annual, _daily(annual, settings.periods_per_year), "csv")

    annual = settings.risk_free_rate
    _upsert(conn, "CONST", as_of, annual, "constant-v1")
    conn.commit()
    return RiskFree(
        "CONST", as_of, annual, _daily(annual, settings.periods_per_year), "constant-v1"
    )


# -- the French daily RF curve (load-rf, BACKTEST_SPEC §8.2) ------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pinned_rf_sha256(root: Path) -> tuple[str, str]:
    """The SHA-256 the vendored manifest pins for the FF3 file, and its library version."""
    manifest_path = root / FF_RF_MANIFEST
    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text())
        ff3 = manifest["files"]["ff3"]
        return str(ff3["sha256"]), str(manifest["library_version"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RiskFreeDataError(f"factor manifest {manifest_path} unusable: {exc}") from exc


def _rf_rows(path: Path) -> dict[str, float]:
    """``{iso date: daily RF as a fraction}`` from the FF3 file's own ``RF`` column.

    The file is ascending and percent-denominated; a ``-99.99`` sentinel row is skipped."""
    out: dict[str, float] = {}
    with path.open(newline="", encoding="latin-1") as fh:
        for raw in csv.reader(fh):
            key = raw[0].strip() if raw else ""
            if len(key) != _DATE_LEN or not key.isdigit() or len(raw) <= _RF_COL:
                continue
            day = f"{key[:4]}-{key[4:6]}-{key[6:]}"
            try:
                value = float(raw[_RF_COL])
            except ValueError as exc:
                raise RiskFreeDataError(f"{path.name}: unreadable RF row {raw!r}") from exc
            if value <= _MISSING_SENTINEL:
                continue
            out[day] = value / _PCT
    return out


def load_french_rf(conn: Database, *, data_dir: Path | None = None) -> RfLoadReport:
    """Load French's daily ``RF`` into ``risk_free_rate`` under the ``FF_RF_DAILY`` curve.

    One row per session dated in the file: ``annualized_rate = daily fraction x 252``,
    ``source = 'french-ff3-daily'``, ``engine_version = 'rf-v2'``. The vendored manifest's
    SHA-256 is verified first, so the curve is always the pinned file and never a silent
    substitute. Idempotent: a re-run over an unchanged file changes no row (``n_written`` 0);
    a corrected value under the same date updates the row in place."""
    root = data_dir or _DATA_DIR
    path = root / FF_RF_FILE
    pinned, version = _pinned_rf_sha256(root)
    try:
        got = _sha256(path)
    except OSError as exc:
        raise RiskFreeDataError(f"risk-free file {path} unreadable: {exc}") from exc
    if got != pinned:
        raise RiskFreeDataError(
            f"{path.name}: SHA-256 {got} does not match the manifest's {pinned}; refusing to "
            "load a risk-free series that is not the pinned one"
        )
    rows = _rf_rows(path)
    if not rows:
        raise RiskFreeDataError(f"{path.name}: no dated RF row")
    now = _now()
    written = 0
    for day in sorted(rows):
        cur = conn.execute(
            """
            INSERT INTO risk_free_rate
                (curve, rate_date, annualized_rate, source, engine_version, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (curve, rate_date, engine_version) DO UPDATE SET
                annualized_rate = excluded.annualized_rate,
                source = excluded.source,
                ingested_at = excluded.ingested_at
            WHERE risk_free_rate.annualized_rate IS NOT excluded.annualized_rate
            """,
            (
                FF_RF_CURVE,
                day,
                rows[day] * FF_RF_DAILY_DAYS_PER_YEAR,
                FF_RF_SOURCE,
                FF_RF_ENGINE_VERSION,
                now,
            ),
        )
        written += max(cur.rowcount, 0)
    conn.commit()
    dates = sorted(rows)
    return RfLoadReport(
        curve=FF_RF_CURVE,
        first_date=dates[0],
        last_date=dates[-1],
        n_rows=len(dates),
        n_written=written,
        library_version=version,
        sha256=got,
    )
