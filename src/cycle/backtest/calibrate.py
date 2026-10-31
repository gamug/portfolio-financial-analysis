"""WP-G: calibrate the GUARD parameters on 2019-01-02 .. 2021-12-31 and freeze them.

The rule is AVANCE_GENERAL E4: literature values where they exist, otherwise a declared
calibration rule outside every backtest window -- never on P1-P3 data or results. The literature
PDFs are not on disk, so no literature value can be verified in this repository; every parameter
below is therefore a declared calibration rule, fixed here on the calibration sample only:

===============  =========  ==================================================================
layer / param    value      rule
===============  =========  ==================================================================
1 ``J``          21         one D1 rebalance interval (design choice, declared).
1 ``gamma``      -Q005      minus the 5 % quantile of every ``S_i,t(21)`` in C, where ``S`` is
                            the sum of the member's last 21 simple returns.
1 ``delta``      Q075       the 75 % quantile of every one-session member return in C.
3 ``sigma_star`` EW vol     annualized std (ddof=1, x sqrt(252)) of the equal-weight member
                            portfolio over C (declared proxy: the Top-30 min-var book cannot be
                            formed in C -- no risk model before late 2021 and no FUNDAMENTAL
                            scores before 2020; Moreira & Muir 2017, inspired by).
3 ``vol_window`` 21         BACKTEST_SPEC section 0.
4 ``alpha``      1 - MDD    one minus the worst drawdown of the EW portfolio over C
                            (Grossman & Zhou 1993 via Yang & Zhong 2012).
4 ``m``          1/(1-a)    exposure is exactly 1 at a new maximum (Yang & Zhong eq. 1 with
                            a = 1 - delta; AVANCE E1: m(1 - alpha) >= 1).
===============  =========  ==================================================================

Quantiles use ``numpy.quantile(..., method="linear")`` pooled over all (member, session) pairs.
Values are rounded to 4 decimals and the rounded values are frozen.

The command reads only (``connect_ro``), refuses a date range that reaches 2022-01-03 or later,
and refuses a path under the production data root.

Run::

    python -m cycle.backtest calibrate-guard --db base.db \\
        --universe-db /workspaces/thesis/data/universe_history_gics.db \\
        --from 2019-01-02 --to 2021-12-31 --out src/cycle/backtest/params.frozen.toml
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np

from kg_schema import connect_ro, queries, trading_calendar

#: WP-G's calibration sample C (BACKTEST_SPEC / FINISH_REPO section 6).
DEFAULT_FROM = "2019-01-02"
DEFAULT_TO = "2021-12-31"
#: The first session of P1's window: C must never reach it (rule: never calibrate on P1-P3).
FIRST_WINDOW_SESSION = "2022-01-03"
#: The production data root a calibration never takes as its input database.
PRODUCTION_DATA_ROOT = Path("/workspaces/thesis/data")
#: The returns engine version the panel reads (BACKTEST_SPEC section 0).
RETURNS_ENGINE_VERSION = "qret-v2"
#: WP-G's leaver calibration prices (optional inputs, never the final database).
LEAVERS_DIR = Path("/workspaces/thesis/data/calibration/leavers_2019_2021_gateway")
#: ``S_i,t(J)``: one D1 rebalance interval; also the layer-3 volatility window.
J = 21
#: Price-theoretic annualization of a daily standard deviation.
PERIODS_PER_YEAR = 252
#: The frozen values carry four decimals.
NDIGITS = 4
#: A drawdown standard deviation needs at least two EW sessions.
MIN_EW_SESSIONS = 2


class CalibrationError(RuntimeError):
    """A refusal or an input the calibration cannot use."""


@dataclass(frozen=True)
class LeaverPanel:
    """The optional leaver series from WP-G's calibration files: their daily total returns."""

    path: Path
    series: int
    bars: int
    returns: dict[tuple[str, str], float]  # (symbol, ISO date) -> simple total return


@dataclass(frozen=True)
class Calibration:
    """The frozen values and the counts they were measured on."""

    gamma: float
    delta: float
    sigma_star: float
    alpha: float
    m: float
    j: int
    vol_window: int
    sessions: int
    member_sessions: int
    s_values: int
    delta_values: int
    ew_sessions: int
    ew_cumulative: float
    ew_max_drawdown: float
    missing_members_by_year: dict[str, int]
    pit_members_by_year: dict[str, int]
    leaver_source: str
    leaver_series: int
    leaver_pairs: int
    input_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "gamma": self.gamma,
            "delta": self.delta,
            "sigma_star": self.sigma_star,
            "alpha": self.alpha,
            "m": self.m,
            "J": self.j,
            "vol_window": self.vol_window,
            "sessions": self.sessions,
            "member_sessions": self.member_sessions,
            "s_values": self.s_values,
            "delta_values": self.delta_values,
            "ew_sessions": self.ew_sessions,
            "ew_cumulative_return": round(self.ew_cumulative, 6),
            "ew_max_drawdown": round(self.ew_max_drawdown, 6),
            "missing_members_by_year": self.missing_members_by_year,
            "pit_members_by_year": self.pit_members_by_year,
            "leaver_source": self.leaver_source,
            "leaver_series": self.leaver_series,
            "leaver_pairs": self.leaver_pairs,
            "input_sha256": self.input_sha256,
        }


def _sessions(date_from: str, date_to: str) -> list[str]:
    """Every NYSE session in ``[date_from, date_to]``; both ends must be sessions."""
    first, last = date.fromisoformat(date_from), date.fromisoformat(date_to)
    for day in (first, last):
        if not trading_calendar.is_trading_day(day):
            raise CalibrationError(f"{day.isoformat()} is not an NYSE session")
    out: list[str] = []
    day = first
    while day <= last:
        if trading_calendar.is_trading_day(day):
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def _refuse(out_of_sample: str, path: Path) -> None:
    """The two refusals: never past P1's first session, never the production data root."""
    resolved = path.expanduser().resolve()
    if resolved == PRODUCTION_DATA_ROOT or PRODUCTION_DATA_ROOT in resolved.parents:
        raise CalibrationError(
            f"refusing to calibrate from {resolved}: every database under {PRODUCTION_DATA_ROOT} "
            "is read-only; point --db at a scratch copy"
        )
    if out_of_sample >= FIRST_WINDOW_SESSION:
        raise CalibrationError(
            f"refusing a calibration range reaching {out_of_sample}: the sample must end before "
            f"P1's first session ({FIRST_WINDOW_SESSION}) -- GUARD is never calibrated on P1-P3"
        )


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    """A tiny CSV reader (no quoted commas in these files); the header and the data rows."""
    with path.open(encoding="utf-8") as fh:
        header = fh.readline().strip().split(",")
        rows = [line.rstrip("\n").split(",") for line in fh if line.strip()]
    return header, rows


def load_leavers(path: Path = LEAVERS_DIR) -> LeaverPanel | None:
    """The leaver calibration series, if the director placed them.

    ``prices.csv`` is the vendor's split-adjusted daily close: the two AIV events (2020-12-01,
    2020-12-15) and SLG's (2021-01-21) show no discontinuity in the series, so the factors in
    ``actions.csv`` are *not* applied a second time. The total return is
    ``(close_t + the dividend with ex_date t) / close_{t-1} - 1``.
    """
    prices = path / "prices.csv"
    actions = path / "actions.csv"
    if not prices.is_file() or not actions.is_file():
        return None
    header, rows = _read_csv(prices)
    col = {name: header.index(name) for name in ("symbol", "date", "close")}
    series: dict[str, list[tuple[str, float]]] = {}
    for parts in rows:
        if len(parts) <= max(col.values()):
            continue
        series.setdefault(parts[col["symbol"]], []).append(
            (parts[col["date"]], float(parts[col["close"]]))
        )
    aheader, arows = _read_csv(actions)
    acol = {name: aheader.index(name) for name in ("symbol", "action_type", "ex_date", "value")}
    dividends: dict[tuple[str, str], float] = {}
    for parts in arows:
        if len(parts) <= max(acol.values()):
            continue
        if parts[acol["action_type"]].upper() == "DIVIDEND":
            key = (parts[acol["symbol"]], parts[acol["ex_date"]])
            dividends[key] = dividends.get(key, 0.0) + float(parts[acol["value"]])
    returns: dict[tuple[str, str], float] = {}
    bars = 0
    for symbol, values in series.items():
        values.sort()
        bars += len(values)
        for (_, prev_close), (day, close) in pairwise(values):
            if prev_close == 0:
                continue
            returns[(symbol, day)] = (close + dividends.get((symbol, day), 0.0)) / prev_close - 1.0
    return LeaverPanel(path=path, series=len(series), bars=bars, returns=returns)


def _member_rows(
    conn: Any,
    universe: Any,
    sessions: Sequence[str],
    row_of_asset: dict[int, int],
    leaver_row: dict[str, int],
) -> tuple[list[list[int]], dict[str, int], dict[str, int]]:
    """Per session, the matrix rows of its members, resolved as the driver resolves them.

    Returns ``(member_rows, pit_by_year, missing_by_year)``. A member the database has no asset
    row for -- the L-42 leavers, which is what WP-G's files exist for -- falls back to its leaver
    matrix row.
    """
    rows: list[list[int]] = []
    pit_by_year: dict[str, int] = {}
    missing_by_year: dict[str, int] = {}
    for session in sessions:
        members = queries.members_asof(universe, session)
        mapping, missing = queries.resolve_asset_ids(
            conn, [(m.symbol, m.cik) for m in members], as_of=session
        )
        year = session[:4]
        pit_by_year[year] = pit_by_year.get(year, 0) + len(members)
        missing_by_year[year] = missing_by_year.get(year, 0) + len(missing)
        picks: list[int] = []
        for symbol, asset_id in mapping.items():
            row = row_of_asset.get(asset_id)
            if row is not None:
                picks.append(row)
            elif symbol in leaver_row:
                picks.append(leaver_row[symbol])
        rows.append(sorted(set(picks)))
    return rows, pit_by_year, missing_by_year


def _matrix(
    conn: Any, sessions: Sequence[str], leaver_row: dict[str, int]
) -> tuple[np.ndarray, dict[int, int]]:
    """``[asset row, session]`` simple returns (a session with no return is NaN) and its index."""
    rows = conn.execute(
        "SELECT asset_id, obs_date, tr_log_return FROM quant_return_daily "
        "WHERE engine_version = ? AND obs_date >= ? AND obs_date <= ?",
        (RETURNS_ENGINE_VERSION, sessions[0], sessions[-1]),
    ).fetchall()
    asset_ids = sorted({int(r["asset_id"]) for r in rows})
    index = {asset_id: i for i, asset_id in enumerate(asset_ids)}
    position = {session: j for j, session in enumerate(sessions)}
    matrix = np.full((len(asset_ids) + len(leaver_row), len(sessions)), np.nan)
    for r in rows:
        value = r["tr_log_return"]
        if value is None:
            continue
        matrix[index[int(r["asset_id"])], position[str(r["obs_date"])]] = float(
            np.expm1(float(value))
        )
    return matrix, index


def _leaver_rows(
    matrix: np.ndarray,
    sessions: Sequence[str],
    leaver_row: dict[str, int],
    leavers: LeaverPanel | None,
) -> int:
    """Fill the leaver rows; returns how many (series, session) returns the files contributed."""
    if leavers is None:
        return 0
    position = {session: j for j, session in enumerate(sessions)}
    filled = 0
    for (symbol, day), value in leavers.returns.items():
        i = leaver_row.get(symbol.upper())
        j = position.get(day)
        if i is None or j is None:
            continue
        matrix[i, j] = value
        filled += 1
    return filled


def _rolling_sums(matrix: np.ndarray, window: int) -> np.ndarray:
    """The sum of each row's last *window* returns; NaN when any of them is missing."""
    out = np.full(matrix.shape, np.nan)
    if matrix.shape[1] < window:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(matrix, window, axis=1)
    sums = np.sum(windows, axis=2)
    out[:, window - 1 :] = np.where(np.isnan(windows).any(axis=2), np.nan, sums)
    return out


def _pools(
    matrix: np.ndarray, s_matrix: np.ndarray, member_rows: Sequence[Sequence[int]]
) -> tuple[np.ndarray, np.ndarray, int]:
    """``(S(21) pool, one-session-return pool, member-sessions)`` over the sample's members."""
    s_values: list[np.ndarray] = []
    r_values: list[np.ndarray] = []
    member_sessions = 0
    for j, rows in enumerate(member_rows):
        if not rows:
            continue
        picks = np.asarray(rows)
        r = matrix[picks, j]
        present = ~np.isnan(r)
        if not present.any():
            continue
        member_sessions += int(present.sum())
        r_values.append(r[present])
        s = s_matrix[picks, j]
        armed = present & ~np.isnan(s)
        if armed.any():
            s_values.append(s[armed])
    s_all = np.concatenate(s_values) if s_values else np.array([])
    r_all = np.concatenate(r_values) if r_values else np.array([])
    return s_all, r_all, member_sessions


def _ew_returns(matrix: np.ndarray, member_rows: Sequence[Sequence[int]]) -> np.ndarray:
    """The equal-weight member portfolio: mean over ``members_asof(t-1)`` priced on t."""
    out: list[float] = []
    for j in range(1, matrix.shape[1]):
        rows = member_rows[j - 1]
        if not rows:
            continue
        r = matrix[np.asarray(rows), j]
        r = r[~np.isnan(r)]
        if r.size:
            out.append(float(r.mean()))
    return np.asarray(out)


def _max_drawdown(returns: np.ndarray) -> float:
    """The worst peak-to-trough loss of the NAV path that starts at 1.0."""
    nav = np.cumprod(1.0 + returns)
    peaks = np.maximum.accumulate(np.concatenate(([1.0], nav)))[1:]
    return float((1.0 - nav / peaks).max())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate(values: dict[str, float]) -> None:
    """Section 6's validations: a value outside its domain is a hard stop, never improvised."""
    if values["gamma"] <= 0:
        raise CalibrationError(f"gamma = {values['gamma']} is not > 0")
    if values["delta"] <= 0:
        raise CalibrationError(f"delta = {values['delta']} is not > 0")
    if not 0.0 < values["alpha"] < 1.0:
        raise CalibrationError(f"alpha = {values['alpha']} is not in (0, 1)")
    if values["m"] <= 0:
        raise CalibrationError(f"m = {values['m']} is not > 0")


def _check_inputs(db_path: Path, universe_db_path: Path, date_from: str, date_to: str) -> None:
    _refuse(date_to, db_path)
    if date_to < date_from:
        raise CalibrationError(f"--to {date_to} is before --from {date_from}")
    if not db_path.is_file():
        raise CalibrationError(f"database not found: {db_path}")
    if not universe_db_path.is_file():
        raise CalibrationError(f"universe database not found: {universe_db_path}")


def calibrate(
    db_path: str | Path,
    universe_db_path: str | Path,
    *,
    date_from: str = DEFAULT_FROM,
    date_to: str = DEFAULT_TO,
    leavers_dir: str | Path | None = LEAVERS_DIR,
) -> Calibration:
    """Measure the frozen GUARD values on the sample C; no window data is read."""
    db_path = Path(db_path)
    universe_db_path = Path(universe_db_path)
    _check_inputs(db_path, universe_db_path, date_from, date_to)
    sessions = _sessions(date_from, date_to)
    leavers = load_leavers(Path(leavers_dir)) if leavers_dir else None
    conn = connect_ro(db_path)
    universe = connect_ro(universe_db_path)
    try:
        leaver_symbols: list[str] = []
        if leavers is not None:
            known = {
                m.symbol.upper()
                for session in sessions
                for m in queries.members_asof(universe, session)
            }
            leaver_symbols = sorted({symbol.upper() for symbol, _ in leavers.returns} & known)
        n_assets = conn.execute(
            "SELECT COUNT(DISTINCT asset_id) FROM quant_return_daily "
            "WHERE engine_version = ? AND obs_date >= ? AND obs_date <= ?",
            (RETURNS_ENGINE_VERSION, sessions[0], sessions[-1]),
        ).fetchone()[0]
        leaver_row = {symbol: int(n_assets) + k for k, symbol in enumerate(leaver_symbols)}
        matrix, row_of_asset = _matrix(conn, sessions, leaver_row)
        leaver_pairs = _leaver_rows(matrix, sessions, leaver_row, leavers)
        member_rows, pit_by_year, missing_by_year = _member_rows(
            conn, universe, sessions, row_of_asset, leaver_row
        )
    finally:
        conn.close()
        universe.close()

    s_matrix = _rolling_sums(matrix, J)
    s_all, r_all, member_sessions = _pools(matrix, s_matrix, member_rows)
    if s_all.size == 0 or r_all.size == 0:
        raise CalibrationError("the calibration sample has no member return")

    ew = _ew_returns(matrix, member_rows)
    if ew.size < MIN_EW_SESSIONS:
        raise CalibrationError("the EW portfolio has fewer than two sessions")
    sigma_star = float(np.std(ew, ddof=1) * np.sqrt(PERIODS_PER_YEAR))
    max_drawdown = _max_drawdown(ew)
    alpha = 1.0 - max_drawdown
    if not 0.0 < alpha < 1.0:
        raise CalibrationError(f"alpha = {alpha} is outside (0, 1): the EW drawdown is degenerate")

    values = {
        "gamma": round(-float(np.quantile(s_all, 0.05, method="linear")), NDIGITS),
        "delta": round(float(np.quantile(r_all, 0.75, method="linear")), NDIGITS),
        "sigma_star": round(sigma_star, NDIGITS),
        "alpha": round(alpha, NDIGITS),
        "m": round(1.0 / (1.0 - alpha), NDIGITS),
    }
    _validate(values)

    return Calibration(
        gamma=values["gamma"],
        delta=values["delta"],
        sigma_star=values["sigma_star"],
        alpha=values["alpha"],
        m=values["m"],
        j=J,
        vol_window=J,
        sessions=len(sessions),
        member_sessions=member_sessions,
        s_values=int(s_all.size),
        delta_values=int(r_all.size),
        ew_sessions=int(ew.size),
        ew_cumulative=float(np.cumprod(1.0 + ew)[-1] - 1.0),
        ew_max_drawdown=max_drawdown,
        missing_members_by_year=dict(sorted(missing_by_year.items())),
        pit_members_by_year=dict(sorted(pit_by_year.items())),
        leaver_source=str(leavers.path) if leavers is not None else "none",
        leaver_series=leavers.series if leavers is not None else 0,
        leaver_pairs=leaver_pairs,
        input_sha256=_sha256(db_path),
    )


def render_params(calib: Calibration, *, unscored_max_share: float | None = None) -> str:
    """The params TOML in the driver's schema, with a comment per declared calibration rule."""
    lines = [
        "# Backtest params -- GUARD frozen on the calibration sample C (2019-01-02 .. 2021-12-31).",
        "#",
        "# Generated by `python -m cycle.backtest calibrate-guard` (WP-G, FINISH_REPO section 6).",
        "# The sample is outside every backtest window: no P1-P3 data or result entered these",
        "# numbers. Literature values could not be verified (the PDFs are not on disk), so every",
        "# parameter is a declared calibration rule:",
        "#   layer 1  J = 21: one D1 rebalance interval (design choice, declared).",
        "#   layer 1  gamma = -Q0.05 of every S_i,t(21) in C (S = the member's last 21 simple",
        "#            returns): the stop fires on the worst 5 % of 21-session runs in C.",
        "#   layer 1  delta = Q0.75 of every one-session member return in C.",
        "#   layer 3  sigma_star = annualized std (ddof=1, x sqrt(252)) of the equal-weight member",
        "#            portfolio over C (declared proxy: no Top-30 min-var book can be formed there).",
        "#   layer 4  alpha = 1 - MDD of the EW portfolio over C; m = 1/(1-alpha).",
        '# Quantiles: numpy.quantile(method="linear"), pooled over all (member, session) pairs;',
        "# values rounded to 4 decimals and frozen.",
        "#",
        f"# Input database sha256: {calib.input_sha256}",
        f"# Sample: {calib.sessions} sessions, {calib.member_sessions} member-sessions,",
        f"#   {calib.s_values} S(21) values, {calib.delta_values} one-session returns,",
        f"#   {calib.ew_sessions} EW-portfolio sessions, EW cumulative "
        f"{calib.ew_cumulative:+.6f}, EW MDD {calib.ew_max_drawdown:.6f}.",
        f"#   PIT members per year: {calib.pit_members_by_year}",
        f"#   PIT members with no return per year (declared limitation of C): "
        f"{calib.missing_members_by_year}",
        f"#   Leaver series: {calib.leaver_series} from {calib.leaver_source} "
        f"({calib.leaver_pairs} (series, session) returns).",
        "",
        "[run]",
        "cost_bps = 10.0",
        "top_n = 30",
        "lookback = 756",
        "min_history = 740",
        'cov_estimator = "ledoit_wolf_cc"',
        'returns_engine_version = "qret-v2"',
        'rf_curve = "FF_RF_DAILY"',
    ]
    if unscored_max_share is not None:
        lines.append(
            f"unscored_max_share = {unscored_max_share}  "
            "# Case B of FINISH_REPO section 5 step 7 (measured maximum over k>=0, "
            "rounded up to 0.005)"
        )
    lines += [
        "",
        "[guard]",
        'shadow_from = "TOPN_MV_GUARD_NO_NLP"',
        "",
        "[guard.arms.TOPN_MV_GUARD_NO_NLP]",
        "layers = [1, 3, 4]   # layer 2 (news) is off: F1, no admissible sentiment model",
        f"J = {calib.j}",
        f"gamma = {calib.gamma}",
        f"delta = {calib.delta}",
        f"sigma_star = {calib.sigma_star}",
        f"m = {calib.m}",
        f"alpha = {calib.alpha}",
        f"vol_window = {calib.vol_window}",
        f"periods_per_year = {PERIODS_PER_YEAR}",
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cycle.backtest calibrate-guard",
        description="Calibrate and freeze the GUARD parameters on 2019-01-02 .. 2021-12-31.",
    )
    parser.add_argument("--db", required=True, help="a scratch copy of the financial database")
    parser.add_argument("--universe-db", required=True, help="the read-only universe database")
    parser.add_argument("--from", dest="date_from", default=DEFAULT_FROM)
    parser.add_argument("--to", dest="date_to", default=DEFAULT_TO)
    parser.add_argument("--out", required=True, help="the params TOML to write")
    parser.add_argument(
        "--unscored-max-share",
        type=float,
        default=None,
        help="write the [run] unscored_max_share key (Case B of FINISH_REPO section 5 step 7)",
    )
    parser.add_argument(
        "--no-leavers",
        action="store_true",
        help="ignore the optional leaver calibration files",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        calib = calibrate(
            args.db,
            args.universe_db,
            date_from=args.date_from,
            date_to=args.date_to,
            leavers_dir=None if args.no_leavers else LEAVERS_DIR,
        )
        out = Path(args.out)
        out.write_text(
            render_params(calib, unscored_max_share=args.unscored_max_share), encoding="utf-8"
        )
    except CalibrationError as exc:
        print(f"cycle.backtest calibrate-guard: {exc}", file=sys.stderr)
        return 2
    payload = calib.as_dict()
    payload["out"] = str(out)
    if args.unscored_max_share is not None:
        payload["unscored_max_share"] = args.unscored_max_share
    print(json.dumps(payload, sort_keys=True))
    return 0
