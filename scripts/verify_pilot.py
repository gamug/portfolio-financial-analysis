#!/usr/bin/env python3
"""Pilot re-run verification: one PASS / FAIL / WARN / SKIP line per fix, read-only.

    uv run python scripts/verify_pilot.py --db PILOT_DB [--replay-db REPLAY_DB]

Checks invariants each merged fix guarantees on a freshly built database. Nothing is written.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import sqlite3
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from api import orchestrator
from kg_schema.company_profile import profile_as_of
from kg_schema.db import connect_ro
from kg_schema.env import universe_database_path
from kg_schema.queries import (
    WINDOW_START,
    invalid_sec_filings_accessions,
    members_asof,
    members_over_range,
    resolve_asset_ids,
    unresolved_run_references,
)
from kg_schema.ticker_history import read_history

try:
    from fundamental_agent.db import METRICS_ENGINE_VERSION
except ImportError:
    METRICS_ENGINE_VERSION = ""

try:
    import verify_t138
except ImportError:
    verify_t138 = None  # type: ignore[assignment]

_MIN_DISTINCT_FRONTIER_PTS = 2
_FLOAT_EPSILON = 1e-9
_MIN_PRICE_HISTORY_SESSIONS = 2
_MIN_VOLUME_SHARE = 0.5
_MAX_MISSING_MARGIN_SHARE = 0.05

RESULTS: list[tuple[str, str, str]] = []


def report(task: str, status: str, detail: str) -> None:
    RESULTS.append((task, status, detail))
    print(f"[{status:4}] {task:6} {detail}")


def cols(c: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


def one(c: sqlite3.Connection, sql: str, params: tuple = ()) -> object:
    row = c.execute(sql, params).fetchone()
    return row[0] if row else None


def check(task: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except sqlite3.Error as exc:
        report(task, "SKIP", f"not checkable on this database ({exc})")


def members_asof_mismatches(fin_db_path: str, universe_path: str) -> tuple[int, list[str]]:
    """Dates where ``members_asof`` disagrees with a closed-interval read of the universe file (T-153a).

    The dates checked are every ``cycle_run.cycle_date``, every ``valid_to`` in the file, and each
    ``valid_to + 1`` day: a leave date and the day after are exactly what a ``>`` predicate gets wrong.
    Returns ``(dates_checked, mismatches)``; both connections are read-only."""
    with (
        contextlib.closing(connect_ro(fin_db_path)) as fin,
        contextlib.closing(connect_ro(universe_path)) as universe,
    ):
        dates = {
            str(r["cycle_date"]) for r in fin.execute("SELECT DISTINCT cycle_date FROM cycle_run")
        }
        for r in universe.execute(
            "SELECT DISTINCT valid_to FROM universe_membership WHERE valid_to IS NOT NULL"
        ):
            leave = date.fromisoformat(str(r["valid_to"]))
            dates.add(leave.isoformat())
            dates.add((leave + timedelta(days=1)).isoformat())
        mismatches: list[str] = []
        for as_of in sorted(dates):
            closed = {
                str(r["symbol"])
                for r in universe.execute(
                    "SELECT symbol FROM universe_membership WHERE valid_from <= ? "
                    "AND (valid_to IS NULL OR valid_to >= ?)",
                    (as_of, as_of),
                )
            }
            got = {m.symbol for m in members_asof(universe, as_of)}
            if got != closed:
                mismatches.append(f"{as_of}: members_asof={len(got)} closed={len(closed)}")
        return len(dates), mismatches


#: A CSV row the universe file does not show: CIK 0002041610 traded as PSKY from 2025-08-07 (the
#: Skydance/Paramount close) and as SKYD from 2026-10-06; ``universe_history.db`` records only the
#: current ticker, backfilled to 1994 (SA-2, T-153 part 2).
_T153B_EXCEPTIONS = frozenset({("0002041610", "PSKY")})


def _file_rows(universe: Any) -> list[Any]:
    return list(
        universe.execute(
            "SELECT cik, symbol, valid_from, valid_to FROM universe_membership "
            "WHERE cik IS NOT NULL"
        )
    )


def _adjacent_pairs(file_rows: list[Any], by_key: set[tuple[str, str]]) -> tuple[int, list[str]]:
    """Direction 1: every same-CIK adjacent pair since 2022 has both symbols in the file."""
    pairs, problems = 0, []
    for a in file_rows:
        if not a["valid_to"]:
            continue
        nxt = (date.fromisoformat(str(a["valid_to"])) + timedelta(days=1)).isoformat()
        if nxt < "2022-01-01":
            continue
        for b in file_rows:
            if b["cik"] == a["cik"] and b["valid_from"] == nxt and b["symbol"] != a["symbol"]:
                pairs += 1
                for cik, symbol in (
                    (str(a["cik"]), str(a["symbol"])),
                    (str(b["cik"]), str(b["symbol"])),
                ):
                    if (cik, symbol) not in by_key:
                        problems.append(
                            f"pair {cik} {a['symbol']}->{b['symbol']}: no row for {symbol}"
                        )
    return pairs, problems


def _tracked_row_problems(
    tracked: list[Any],
    stints: dict[tuple[str, str], list[tuple[str, str | None]]],
    open_ciks: frozenset[str],
    last_start: dict[str, str],
) -> list[str]:
    """Direction 2: every tracked row is inside the file's stint, or a post-exit row (D4).

    A rename that happens **after** a company left the index (GPS -> GAP on 2024-08-22, FBHS ->
    FBIN on 2022-12-15, T-153 part 3a) continues the line past its last membership day, so the
    row is not inside the file's stint for its ``(CIK, symbol)`` -- and for the new symbol the
    file has no stint at all. Such a row is allowed when its CIK has **no open stint** in the
    file and the row starts on or after that CIK's last stint begins. A row on a CIK that is
    still a member is *not* covered by this rule: PSKY (CIK 0002041610, an open SKYD stint) keeps
    its explicit exception, which flags the Paramount CIK succession and must not be absorbed."""
    problems = []
    for row in tracked:
        if (row.cik, row.symbol) in _T153B_EXCEPTIONS:
            continue
        if _inside(row.valid_from, row.valid_to, stints.get((row.cik, row.symbol), [])):
            continue
        if _post_exit_row(row, open_ciks, last_start):
            continue
        problems.append(
            f"row {row.cik} {row.symbol} {row.valid_from}..{row.valid_to} is not in the file"
        )
    return problems


def _post_exit_row(row: Any, open_ciks: frozenset[str], last_start: dict[str, str]) -> bool:
    """Whether *row* is a post-exit rename of a company the file shows as a leaver (D4)."""
    if row.cik in open_ciks:
        return False
    start = last_start.get(row.cik)
    return start is not None and row.valid_from >= start


def _asset_problems(fin: Any, tracked: list[Any]) -> list[str]:
    """Direction 3: no asset carries a non-canonical symbol of its own line."""
    non_canonical = {(r.cik, r.symbol) for r in tracked if r.symbol != r.canonical_symbol}
    return [
        f"asset {r['ticker']} (CIK {r['cik']}) carries an old symbol of its line"
        for r in fin.execute("SELECT cik, ticker FROM assets WHERE cik IS NOT NULL")
        if (str(r["cik"]), str(r["ticker"])) in non_canonical
    ]


def ticker_history_gaps(fin_db_path: str, universe_path: str) -> tuple[int, int, list[str]]:
    """The tracked ticker history against ``universe_history.db`` and the assets (T-153b).

    Direction 1: every same-CIK, adjacent stint pair in the file since 2022 (``valid_from ==
    valid_to + 1``) has both its symbols in ``ticker_history.csv``.
    Direction 2: every tracked row (bar the declared exception) sits inside the file's stint for
    its ``(CIK, symbol)`` -- or is a post-exit rename of a leaver (D4).
    Direction 3: no asset carries a non-canonical symbol of its own line -- a rename split into
    two assets. Returns ``(pairs, rows, problems)``; both connections are read-only."""
    tracked = read_history()
    by_key = {(r.cik, r.symbol) for r in tracked}
    stints: dict[tuple[str, str], list[tuple[str, str | None]]] = {}
    with (
        contextlib.closing(connect_ro(universe_path)) as universe,
        contextlib.closing(connect_ro(fin_db_path)) as fin,
    ):
        file_rows = _file_rows(universe)
        for r in file_rows:
            stints.setdefault((str(r["cik"]), str(r["symbol"])), []).append(
                (str(r["valid_from"]), str(r["valid_to"]) if r["valid_to"] else None)
            )
        open_ciks = frozenset(str(r["cik"]) for r in file_rows if not r["valid_to"])
        last_start: dict[str, str] = {}
        for r in file_rows:
            cik, start = str(r["cik"]), str(r["valid_from"])
            if cik not in last_start or start > last_start[cik]:
                last_start[cik] = start
        pairs, problems = _adjacent_pairs(file_rows, by_key)
        problems += _tracked_row_problems(tracked, stints, open_ciks, last_start)
        problems += _asset_problems(fin, tracked)
    return pairs, len(tracked), problems


def _inside(valid_from: str, valid_to: str | None, spans: list[tuple[str, str | None]]) -> bool:
    """Whether ``[valid_from, valid_to]`` sits inside one of the file's *spans*."""
    for start, end in spans:
        if start > valid_from:
            continue
        if valid_to is None:
            if end is None:
                return True
        elif end is None or end >= valid_to:
            return True
    return False


#: D2's lower bounds (T-153 part 3a): prices from 2019-01-02, filings filed from 2022-01-01.
PRICES_SINCE = "2019-01-02"
FILINGS_SINCE = "2022-01-01"


def _ranking_out_of_universe(fin: Any, universe: Any) -> tuple[list[str], list[str]]:
    """(cycle_dates, problems): every ranked asset was a member on its run's ``cycle_date``."""
    problems: list[str] = []
    dates = [
        str(r["cycle_date"])
        for r in fin.execute(
            "SELECT DISTINCT cycle_date FROM cycle_run WHERE cycle_date IS NOT NULL "
            "ORDER BY cycle_date"
        )
    ]
    for cycle_date in dates:
        members = members_asof(universe, cycle_date)
        ids, _missing = resolve_asset_ids(
            fin, [(m.symbol, m.cik) for m in members], as_of=cycle_date
        )
        member_ids = set(ids.values())
        ranked = fin.execute(
            "SELECT DISTINCT r.asset_id FROM cycle_ranking r "
            "JOIN cycle_run c ON c.id = r.cycle_run_id WHERE c.cycle_date = ?",
            (cycle_date,),
        ).fetchall()
        for row in ranked:
            if int(row["asset_id"]) not in member_ids:
                problems.append(
                    f"{cycle_date}: asset {row['asset_id']} ranked but not a member that day"
                )
    return dates, problems


def _outside_scope(fin: Any, universe: Any, end: str) -> tuple[int, list[str]]:
    """(leaver_lines, problems): no leaver line has a price or filing after its exit (D2)."""
    problems: list[str] = []
    leavers = 0
    for w in members_over_range(universe, fin, WINDOW_START, end):
        if w.window_to is None:
            continue
        leavers += 1
        row = fin.execute(
            "SELECT id FROM assets WHERE UPPER(ticker) = ?", (w.member.symbol.upper(),)
        ).fetchone()
        if row is None:
            continue  # the line has no asset yet: nothing was ingested for it
        aid = int(row["id"])
        late = fin.execute(
            "SELECT COUNT(*) AS n, MAX(date) AS d FROM price_daily WHERE asset_id = ? AND date > ?",
            (aid, w.window_to),
        ).fetchone()
        if late["n"]:
            problems.append(
                f"{w.member.symbol}: {late['n']} price bars after its exit "
                f"{w.window_to} (last {late['d']})"
            )
        late_filings = fin.execute(
            "SELECT COUNT(*) AS n, MAX(filing_date) AS d FROM sec_filings "
            "WHERE asset_id = ? AND filing_date > ?",
            (aid, w.window_to),
        ).fetchone()
        if late_filings["n"]:
            problems.append(
                f"{w.member.symbol}: {late_filings['n']} filings after its exit "
                f"{w.window_to} (last {late_filings['d']})"
            )
    return leavers, problems


def _early_rows(fin: Any) -> list[str]:
    """Problems for a price before PRICES_SINCE or a filing before FILINGS_SINCE (D2's bounds)."""
    problems: list[str] = []
    early = fin.execute(
        "SELECT COUNT(*) AS n, MIN(date) AS d FROM price_daily WHERE date < ?", (PRICES_SINCE,)
    ).fetchone()
    if early["n"]:
        problems.append(f"{early['n']} price bars before {PRICES_SINCE} (first {early['d']})")
    early_filings = fin.execute(
        "SELECT COUNT(*) AS n, MIN(filing_date) AS d FROM sec_filings "
        "WHERE filing_date IS NOT NULL AND filing_date < ?",
        (FILINGS_SINCE,),
    ).fetchone()
    if early_filings["n"]:
        problems.append(
            f"{early_filings['n']} filings before {FILINGS_SINCE} (first {early_filings['d']})"
        )
    return problems


def leaver_profile_problems(fin_db_path: str, universe_path: str) -> tuple[int, int, list[str]]:
    """The leavers' company profiles and sectors (T-153d).

    (a) every leaver line that has a stored filing has an APP-00 profile that **covers each of that
    filing's period ends** -- the leaver's own interval (``T-153`` part 3b), so a period end before
    2022 (a 10-K filed in 2022) and one up to its exit are both inside it;
    (b) no ``sectors`` row has an empty name (``sync_universe`` must not create one for a leaver the
    delivered universe file leaves unclassified), and every leaver asset with a stored filing has a
    sector -- a leaver with neither (MBC, FTRE, PHIN) is allowed to have none.
    Returns ``(leaver_lines, filings, problems)``; both connections are read-only."""
    with (
        contextlib.closing(connect_ro(fin_db_path)) as fin,
        contextlib.closing(connect_ro(universe_path)) as universe,
    ):
        problems: list[str] = []
        empty = int(fin.execute("SELECT COUNT(*) FROM sectors WHERE name = ''").fetchone()[0])
        if empty:
            problems.append(f"{empty} sectors rows have an empty name")
        end = date.today().isoformat()
        lines = filings = 0
        for w in members_over_range(universe, fin, WINDOW_START, end):
            if w.window_to is None:
                continue
            lines += 1
            row = fin.execute(
                "SELECT id, cik, sector_id FROM assets WHERE UPPER(ticker) = ?",
                (w.member.symbol.upper(),),
            ).fetchone()
            if row is None:
                continue  # the line has no asset yet: nothing was ingested for it
            stored = fin.execute(
                "SELECT period_end FROM sec_filings WHERE asset_id = ?", (int(row["id"]),)
            ).fetchall()
            # The delivered file classifies this line, so the asset must carry its sector -- the
            # one path from the file to the SECTOR score (T-153 part 3b, D5). A file that carries
            # no label for the line (a scratch universe) asks nothing of it.
            if stored and w.member.gics_sector and row["sector_id"] is None:
                problems.append(
                    f"{w.member.symbol}: the universe file gives it a sector but the asset has none"
                )
            for filing in stored:
                filings += 1
                period_end = str(filing["period_end"])
                profile = profile_as_of(row["cik"], period_end)
                if profile is None or not profile.covers(period_end):
                    problems.append(f"{w.member.symbol}: no profile covering {period_end}")
        return lines, filings, problems


def scope_problems(fin_db_path: str, universe_path: str) -> tuple[int, int, list[str]]:
    """The ingestion scope and the point-in-time ranking (T-153c).

    (a) every ``cycle_ranking`` row belongs to an asset that was a member on its run's
    ``cycle_date`` -- a leaver is never ranked outside its membership, and nothing puts a
    company in a ranking before it entered or after it left.
    (b) no line that left the index has a ``price_daily`` bar dated after its last membership
    day or a ``sec_filings`` row filed after it, and no asset has a price before
    :data:`PRICES_SINCE` or a filing filed before :data:`FILINGS_SINCE` (D2's bounds).
    Returns ``(cycle_dates, leaver_lines, problems)``; both connections are read-only."""
    with (
        contextlib.closing(connect_ro(fin_db_path)) as fin,
        contextlib.closing(connect_ro(universe_path)) as universe,
    ):
        dates, problems = _ranking_out_of_universe(fin, universe)
        end = max([*dates, date.today().isoformat()])
        leavers, scope = _outside_scope(fin, universe, end)
        return len(dates), leavers, [*problems, *scope, *_early_rows(fin)]


def main() -> int:  # noqa: C901, PLR0915
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--replay-db")
    args = ap.parse_args()
    c = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    def t114() -> None:
        bad = []
        for t in ("analysis_run", "pricing_run", "quant_run", "cycle_run"):
            n = one(
                c,
                f"SELECT COUNT(*) FROM {t} WHERE code_version IS NULL OR code_version LIKE '%-dirty'",  # noqa: S608 - table names are our own constants
            )
            if n:
                bad.append(f"{t}={n}")
        report(
            "T-114",
            "FAIL" if bad else "PASS",
            "runs from a dirty/unknown checkout: " + (", ".join(bad) or "none"),
        )

    def t107() -> None:
        parts = []
        for t, where in (
            ("sec_filings", "1=1"),
            ("fundamental_metrics", "1=1"),
            ("score_snapshot", "score_type='FUNDAMENTAL'"),
        ):
            if "available_at" not in cols(c, t):
                report("T-107", "FAIL", f"{t} has no available_at column")
                return
            parts.append(
                f"{t}={one(c, f'SELECT COUNT(*) FROM {t} WHERE {where} AND available_at IS NULL')}"  # noqa: S608 - table names and where clauses are our own constants
            )
        ok = all(p.endswith("=0") for p in parts)
        report("T-107", "PASS" if ok else "FAIL", "rows without available_at: " + ", ".join(parts))

    def metrics_version() -> None:
        v = dict(
            c.execute(
                "SELECT engine_version, COUNT(*) FROM fundamental_metrics GROUP BY 1"
            ).fetchall()
        )
        # The fresh pilot database holds one metrics engine version: the one the checked-out code
        # writes (metrics-v4 at pilot-1; metrics-v5 since T-140).
        current = METRICS_ENGINE_VERSION or next(iter(v), "")
        report(
            "metrics",
            "PASS" if set(v) == {current} else "FAIL",
            f"engine versions {v} (expected only {current})",
        )

    def t116() -> None:
        v = dict(
            c.execute("SELECT gate_version, COUNT(*) FROM data_quality_issue GROUP BY 1").fetchall()
        )
        status = "PASS" if set(v) <= {"dq-v2"} else "FAIL"
        report("T-116", status, f"data-quality gate versions {v or '{} (no issues found)'}")

    def t113() -> None:
        rows = c.execute(
            "SELECT model, COUNT(*), SUM(prompt_hash IS NULL), COUNT(DISTINCT prompt_hash) "
            "FROM score_snapshot WHERE score_type='FUNDAMENTAL' GROUP BY model"
        ).fetchall()
        per_run = one(
            c,
            "SELECT MAX(n) FROM (SELECT COUNT(DISTINCT prompt_hash) n FROM score_snapshot "
            "WHERE score_type='FUNDAMENTAL' GROUP BY run_id)",
        )
        fallbacks = sum(n for m, n, _, _ in rows if m == "rule-based-fallback-v1")
        counted = one(c, "SELECT COALESCE(SUM(fallback_units),0) FROM analysis_run")
        null_hash = sum(h or 0 for _, _, h, _ in rows)
        ok = null_hash == 0 and (per_run or 0) <= 1 and fallbacks == counted
        report(
            "T-113",
            "PASS" if ok else "FAIL",
            f"models {[(m, n) for m, n, _, _ in rows]}; null prompt_hash {null_hash}; "
            f"max prompt_hash per run {per_run}; fallback rows {fallbacks} vs analysis_run.fallback_units {counted}",
        )

    def t117() -> None:
        # FY2021/FY2022: the revenue the gateway drops (T-042, dimension-only), rebuilt by T-140 from
        # "Total revenues and other" less the rows between; FY2023-FY2025: T-117's label correction.
        expected = {
            "FY2021": 7_985_000_000,
            "FY2022": 11_075_000_000,
            "FY2023": 8_279_000_000,
            "FY2024": 9_737_000_000,
            "FY2025": 8_920_000_000,
        }
        got = {}
        for fp, want in expected.items():
            raw = one(
                c,
                "SELECT m.inputs_json FROM fundamental_metrics m JOIN sec_filings f ON f.id=m.filing_id "
                "JOIN assets a ON a.id=f.asset_id WHERE a.ticker='APA' AND f.form='10-K' "
                "AND f.fiscal_period=? AND m.metric_name='net_margin'",
                (fp,),
            )
            got[fp] = (json.loads(raw).get("revenue") if raw else None, want)
        if all(v is None for v, _ in got.values()):
            report("T-117", "SKIP", "APA not in this pilot")
            return
        ok = all(v is not None and abs(v - w) < 1 for v, w in got.values())
        report(
            "T-117", "PASS" if ok else "FAIL", f"APA revenue, T-117/T-140 (stored, expected): {got}"
        )

    def t105() -> None:
        n = one(
            c,
            "SELECT COUNT(*) FROM fundamental_metrics m JOIN sec_filings f ON f.id=m.filing_id "
            "WHERE f.form='10-Q' AND m.metric_name='net_debt_to_ebitda' AND m.value IS NOT NULL "
            "AND json_extract(m.inputs_json,'$.annualized_ttm') IS NULL "
            "AND json_extract(m.inputs_json,'$.annualized_x4') IS NULL",
        )
        x4 = one(
            c,
            "SELECT COUNT(*) FROM fundamental_metrics WHERE metric_name='net_debt_to_ebitda' "
            "AND json_extract(inputs_json,'$.annualized_x4') IS NOT NULL",
        )
        report(
            "T-105",
            "PASS" if n == 0 else "FAIL",
            f"10-Q net debt/EBITDA not annualized: {n}; quarter x4 fallbacks: {x4}",
        )

    def t110() -> None:
        n = one(
            c,
            "SELECT COUNT(*) FROM price_observation o WHERE NOT EXISTS (SELECT 1 FROM price_daily d "
            "WHERE d.asset_id=o.asset_id AND d.date=o.obs_date)",
        )
        report(
            "T-110",
            "PASS" if n == 0 else "FAIL",
            f"price observations without a stored daily bar: {n}",
        )

    def t109() -> None:
        rows = c.execute(
            "SELECT m.id, m.rf_annual, MIN(e.mu) FROM quant_risk_model m JOIN quant_expected_return e "
            "ON e.model_id=m.id AND e.mu_model='equilibrium' GROUP BY m.id"
        ).fetchall()
        if not rows:
            report("T-109", "SKIP", "no risk model built")
            return
        bad = [(mid, rf, mu) for mid, rf, mu in rows if rf is None or mu <= rf]
        mv = c.execute(
            "SELECT id, expected_return, rf_annual FROM quant_portfolio WHERE kind='min_var'"
        ).fetchall()
        mv_bad = [r for r in mv if r[1] is None or r[2] is None or r[1] <= r[2]]
        status = "PASS" if not bad else "FAIL"
        if status == "PASS" and mv_bad:
            status = "WARN"
        report(
            "T-109",
            status,
            f"models with equilibrium mu <= rf: {bad}; min_var books with E[r] <= rf: {mv_bad}",
        )

    def t112() -> None:
        bad = []
        for (mid,) in c.execute("SELECT DISTINCT model_id FROM quant_frontier_point"):
            pts = c.execute(
                "SELECT status, expected_vol FROM quant_frontier_point WHERE model_id=?", (mid,)
            ).fetchall()
            statuses = {s for s, _ in pts}
            if "degenerate" in statuses:
                if len(pts) != 1:
                    bad.append((mid, "degenerate with more than one point"))
            elif (
                len({round(v, 8) for s, v in pts if s != "infeasible"}) < _MIN_DISTINCT_FRONTIER_PTS
            ):
                bad.append((mid, "k identical points not marked degenerate"))
        report("T-112", "PASS" if not bad else "FAIL", f"frontier problems: {bad or 'none'}")

    def t108() -> None:
        bench = c.execute(
            "SELECT obs_date, log_return, level FROM benchmark_series "
            "WHERE benchmark='SP500_EW_INTERNAL' AND engine_version='bench-v2' ORDER BY obs_date"
        ).fetchall()
        if not bench:
            report("T-108", "FAIL", "no bench-v2 series")
            return
        daily = {
            d: m
            for d, m in c.execute(
                "SELECT obs_date, AVG(exp(tr_log_return)-1) FROM quant_return_daily "
                "WHERE tr_log_return IS NOT NULL GROUP BY obs_date"
            )
        }
        diffs = [
            abs(math.expm1(lr) - daily[d]) for d, lr, _ in bench if lr is not None and d in daily
        ]
        worst = max(diffs) if diffs else float("nan")
        status = "PASS" if diffs and worst < _FLOAT_EPSILON else "WARN"
        report(
            "T-108",
            status,
            f"bench-v2 days {len(bench)}; max |daily return - equal-weight mean of all pilot names| {worst:.2e} "
            "(WARN = the gate panel differs from the full pilot universe; confirm it isn't the hard-veto filter)",
        )

    def t111() -> None:
        v = dict(
            c.execute(
                "SELECT engine_version, COUNT(*) FROM quant_benchmark_performance GROUP BY 1"
            ).fetchall()
        )
        nulls = one(
            c, "SELECT COUNT(*) FROM quant_benchmark_performance WHERE realized_return IS NULL"
        )
        ok = "perf-v2" in v and nulls == 0
        report(
            "T-111", "PASS" if ok else "FAIL", f"perf versions {v}; null realized returns {nulls}"
        )

    def t119() -> None:
        run = c.execute(
            "SELECT id, status FROM cycle_run WHERE cycle_type='SELECTION' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not run:
            report("T-119", "SKIP", "no selection cycle")
            return
        unscored = [
            r[0]
            for r in c.execute(
                "SELECT a.ticker FROM cycle_ranking r JOIN assets a ON a.id=r.asset_id "
                "WHERE r.cycle_run_id=? AND r.veto_rules_json LIKE '%UNSCORED%'",
                (run[0],),
            )
        ]
        ok = run[1] == "completed"
        report(
            "T-119",
            "PASS" if ok else "FAIL",
            f"latest selection run {run[0]} {run[1]}; unscored (ineligible): {unscored or 'none'}",
        )

    def t125() -> None:
        vc = cols(c, "veto")
        if "cleared_on" not in vc or "raised_on" not in vc:
            report(
                "T-125",
                "FAIL",
                "veto table has no raised_on/cleared_on stints -- T-125 not applied",
            )
            return
        dup = one(
            c,
            "SELECT COUNT(*) FROM (SELECT asset_id, rule_id FROM veto WHERE cleared_on IS NULL "
            "GROUP BY 1,2 HAVING COUNT(*) > 1)",
        )
        cleared = one(c, "SELECT COUNT(*) FROM veto WHERE cleared_on IS NOT NULL")
        report(
            "T-125",
            "PASS" if dup == 0 else "FAIL",
            f"(asset, rule) with >1 open stint: {dup}; stints cleared so far: {cleared}",
        )

    def t131_session() -> None:
        rows = c.execute(
            "SELECT date, SUM(volume) FROM price_daily GROUP BY date ORDER BY date DESC LIMIT 21"
        ).fetchall()
        if len(rows) < _MIN_PRICE_HISTORY_SESSIONS:
            report("T-131", "SKIP", "not enough price history")
            return
        last_day, last_vol = rows[0]
        med = sorted(v for _, v in rows[1:])[len(rows[1:]) // 2]
        share = last_vol / med if med else 0.0
        report(
            "T-131",
            "PASS" if share >= _MIN_VOLUME_SHARE else "FAIL",
            f"last stored session {last_day}: volume {share:.0%} of the prior 20-day median (partial bar if well below 100%)",
        )

    def t131_windows() -> None:
        n = one(
            c,
            "SELECT COUNT(*) FROM (SELECT o.asset_id, o.obs_date, o.realized_vol_90d, o.momentum_252d, "
            "ROW_NUMBER() OVER (PARTITION BY o.asset_id ORDER BY o.obs_date) rn FROM price_observation o) "
            "WHERE rn > 260 AND (realized_vol_90d IS NULL OR momentum_252d IS NULL)",
        )
        report(
            "T-131",
            "PASS" if n == 0 else "FAIL",
            f"observations past day 260 of history with NULL vol-90d/momentum-252d: {n}",
        )

    def t131_splits() -> None:
        n = one(
            c,
            "WITH p AS (SELECT asset_id, close / LAG(close) OVER (PARTITION BY asset_id ORDER BY date) r "  # noqa: S608 - static SQL query with constant expressions
            "FROM price_daily) SELECT COUNT(*) FROM p WHERE r IS NOT NULL AND ("
            + " OR ".join(
                f"abs(r - {f}) < {f} * 0.03" for f in (2, 0.5, 3, 1 / 3, 4, 0.25, 10, 0.1)
            )
            + ")",
        )
        report(
            "T-131",
            "PASS" if n == 0 else "FAIL",
            f"split-shaped day-over-day jumps in stored closes: {n}",
        )

    def t132() -> None:
        # T-132: every sample asset's latest filing carries a cover-page share count (the shared
        # market-cap reader refuses a name without one), and no stored cap rests on "shares issued".
        rows = c.execute(
            "WITH l AS (SELECT f.id, f.asset_id, ROW_NUMBER() OVER (PARTITION BY f.asset_id "
            "ORDER BY f.period_end DESC, f.available_at DESC) rn FROM sec_filings f) "
            "SELECT COUNT(*), SUM(NOT EXISTS (SELECT 1 FROM filing_cover_shares s WHERE s.filing_id = l.id)) "
            "FROM l WHERE rn = 1"
        ).fetchone()
        not_cover = one(
            c,
            "SELECT COUNT(*) FROM fundamental_metrics WHERE metric_name='market_capitalization' "
            "AND value IS NOT NULL AND COALESCE(json_extract(inputs_json,'$.shares_from_cover_page'),0) = 0",
        )
        missing = rows[1] or 0
        report(
            "T-132",
            "PASS" if missing == 0 else "FAIL",
            f"assets whose latest filing has no cover-page share count: {missing}/{rows[0]}; stored caps "
            f"not from the cover count (balance sheet or diluted fallback): {not_cover}",
        )

    def t133() -> None:
        # FCF margin is measured only where it is defined: Q2/Q3 10-Qs whose latest prior 10-K has
        # an FCF margin. A trailing twelve months needs that fiscal year, and a company with no capex
        # line the registry recognises (banks, brokers, asset managers; HOOD's
        # PaymentsToAcquireOtherProductiveAssets and utilities' split capex, deferred to T-071) has
        # none -- a rule, not a ticker list (pilot review, 2026-10-04).
        rows = c.execute(
            "WITH q AS (SELECT f.id, (SELECT m.value FROM fundamental_metrics m WHERE m.filing_id=f.id "
            "AND m.metric_name='free_cash_flow_margin') v, (SELECT m.value FROM fundamental_metrics m "
            "JOIN sec_filings k ON k.id=m.filing_id WHERE k.asset_id=f.asset_id AND k.form='10-K' "
            "AND k.period_end < f.period_end AND m.metric_name='free_cash_flow_margin' "
            "ORDER BY k.period_end DESC LIMIT 1) prior_k FROM sec_filings f WHERE f.form='10-Q' "
            "AND substr(f.fiscal_period,5) IN ('Q2','Q3')) "
            "SELECT COUNT(*), SUM(v IS NULL) FROM q WHERE prior_k IS NOT NULL"
        ).fetchone()
        share = (rows[1] or 0) / rows[0] if rows[0] else 0.0
        report(
            "T-133",
            "PASS" if rows[0] and share < _MAX_MISSING_MARGIN_SHARE else "FAIL",
            f"Q2/Q3 10-Q TTM FCF margins missing (latest prior 10-K has an FCF margin): "
            f"{rows[1] or 0}/{rows[0]} ({share:.0%})",
        )
        # Capex extraction for the oil & gas concepts, read on the FCF itself: a margin can be
        # missing for lack of revenue, which t140_revenue reports on its own.
        energy = c.execute(
            "SELECT a.ticker, COUNT(*), SUM(json_extract(m.inputs_json,'$.free_cash_flow') IS NULL) "
            "FROM fundamental_metrics m JOIN sec_filings f ON f.id=m.filing_id JOIN assets a "
            "ON a.id=f.asset_id WHERE m.metric_name='free_cash_flow_margin' AND f.form='10-K' "
            "AND a.ticker IN ('APA','PSX') GROUP BY 1"
        ).fetchall()
        if energy:
            missing = {t: f"{n_null}/{n}" for t, n, n_null in energy if n_null}
            report(
                "T-133",
                "FAIL" if missing else "PASS",
                f"oil & gas 10-K FCF missing (capex concepts): {missing or 'none'}",
            )

    def t140_revenue() -> None:
        # Pilot F1: a filing with net income but no (or non-positive) revenue -- e.g. revenue the
        # gateway dropped because the filer tags it only with a dimension. DQ_REVENUE_POS is a HARD
        # gate, so every such filing HARD-vetoes its company while it is the latest.
        rows = c.execute(
            "SELECT COUNT(DISTINCT d.filing_id), GROUP_CONCAT(DISTINCT a.ticker) FROM data_quality_issue d "
            "JOIN sec_filings f ON f.id=d.filing_id JOIN assets a ON a.id=f.asset_id "
            "WHERE d.rule_id='DQ_REVENUE_POS'"
        ).fetchone()
        report(
            "T-140",
            "PASS" if not rows[0] else "FAIL",
            f"filings with net income but no revenue (DQ_REVENUE_POS): {rows[0]}"
            + (f" ({rows[1]})" if rows[0] else ""),
        )

    def t140_quarters() -> None:
        # Pilot F2: no filing silently dropped and no quarter label taken from a shifted fiscal
        # calendar. Consecutive 10-K/10-Q period ends more than 110 days apart mean a missing filing
        # (a quarter is 84-98 days, 52/53-week calendars included); a 10-Q labelled Q4 is impossible.
        # A gap the run recorded a reason for (analysis_run_error for that ticker in that window --
        # e.g. APO's Q1-2023, which the gateway returns without a quarter column, T-140) is not
        # silent: WARN, to be explained in the report. An unrecorded gap is a FAIL.
        gaps = c.execute(
            "WITH s AS (SELECT a.ticker, f.period_end, LAG(f.period_end) OVER (PARTITION BY f.asset_id "
            "ORDER BY f.period_end) prev FROM sec_filings f JOIN assets a ON a.id=f.asset_id "
            "WHERE f.form IN ('10-K','10-Q')) SELECT ticker, prev, period_end, EXISTS (SELECT 1 "
            "FROM analysis_run_error e WHERE e.ticker = s.ticker AND e.form IN ('10-K','10-Q') "
            "AND substr(e.fiscal_period,1,4) BETWEEN substr(s.prev,1,4) AND substr(s.period_end,1,4)) "
            "FROM s WHERE julianday(period_end) - julianday(prev) > 110 ORDER BY ticker, period_end"
        ).fetchall()
        q4 = c.execute(
            "SELECT a.ticker, f.period_end FROM sec_filings f JOIN assets a ON a.id=f.asset_id "
            "WHERE f.form='10-Q' AND f.fiscal_period LIKE '%Q4' ORDER BY 1, 2"
        ).fetchall()
        silent = [f"{t} {p}->{e}" for t, p, e, rec in gaps if not rec]
        recorded = [f"{t} {p}->{e}" for t, p, e, rec in gaps if rec]
        status = "FAIL" if silent or q4 else ("WARN" if recorded else "PASS")
        report(
            "T-140",
            status,
            f"quarters missing with no recorded reason: {silent or 'none'}; missing with a recorded "
            f"run error (explain in the report): {recorded or 'none'}; 10-Qs labelled Q4: "
            f"{[f'{t} {e}' for t, e in q4] or 'none'}",
        )

    def valuation_coverage() -> None:
        # Every filing whose period end has a stored price carries a market cap. A filing scored
        # before prices existed keeps none, and a resumed run skips it as done (the pilot-1 smoke
        # test ran before prices: APA FY2023 10-K).
        rows = c.execute(
            "SELECT a.ticker || ' ' || f.fiscal_period FROM sec_filings f JOIN assets a ON a.id=f.asset_id "
            "WHERE f.period_end >= (SELECT MIN(date) FROM price_daily) AND NOT EXISTS (SELECT 1 "
            "FROM fundamental_metrics m WHERE m.filing_id=f.id AND m.metric_name='market_capitalization' "
            "AND m.value IS NOT NULL) ORDER BY 1"
        ).fetchall()
        report(
            "valuation",
            "PASS" if not rows else "FAIL",
            f"filings with a stored price but no market cap: {[r[0] for r in rows] or 'none'}",
        )

    for task, fn in (
        ("T-114", t114),
        ("T-107", t107),
        ("metrics", metrics_version),
        ("T-116", t116),
        ("T-113", t113),
        ("T-117", t117),
        ("T-105", t105),
        ("T-110", t110),
        ("T-109", t109),
        ("T-112", t112),
        ("T-108", t108),
        ("T-111", t111),
        ("T-119", t119),
        ("T-125", t125),
        ("T-131", t131_session),
        ("T-131", t131_windows),
        ("T-131", t131_splits),
        ("T-132", t132),
        ("T-133", t133),
        ("T-140", t140_revenue),
        ("T-140", t140_quarters),
        ("valuation", valuation_coverage),
    ):
        check(task, fn)

    if args.replay_db:
        r = sqlite3.connect(f"file:{args.replay_db}?mode=ro", uri=True)

        def t115() -> None:
            live_pilot = one(c, "SELECT COUNT(*) FROM portfolio_position")
            live_replay = one(r, "SELECT COUNT(*) FROM portfolio_position")
            replay_rows = one(r, "SELECT COUNT(*) FROM portfolio_position_replay")
            runs = one(
                r, "SELECT COUNT(*) FROM cycle_run WHERE cycle_type='REPLAY' AND status='completed'"
            )
            ok = live_pilot == live_replay and replay_rows > 0
            report(
                "T-115",
                "PASS" if ok else "FAIL",
                f"live book rows pilot/replay-copy {live_pilot}/{live_replay}; replay stints {replay_rows}; completed replay cycles {runs}",
            )

        check("T-115", t115)

    def wi18_books() -> None:
        # Work item 18 (T-134..T-139): every live and replay book satisfies the score_tilt band and
        # both effective caps within 1e-9, and every opt-v2 quant book its caps -- checked by the
        # tracked scripts/verify_t138.py (validated on PR #121), against each run's own params_json.
        # A fresh pilot database holds only post-T-136 books (no legacy score_proportional run) and
        # only opt-v2 quant books. A recorded relaxation is legitimate but must be explained: WARN.
        if verify_t138 is None:
            report("WI-18", "SKIP", "scripts/verify_t138.py not found (checkout before PR #121)")
            return
        books = []
        with contextlib.redirect_stdout(io.StringIO()):
            for path in (args.db, args.replay_db):
                if path:
                    books += verify_t138.verify_db(path, quiet=True, tol=1e-9, quant_tol=1e-6)
        by_kind: dict[str, list] = {}
        for b in books:
            by_kind.setdefault(b.kind, []).append(b)
        bad = [b for b in books if b.violations]
        legacy = [f"{b.kind} {b.date}" for b in books if b.kind != "quant" and b.legacy]
        not_tilt = [
            f"{b.kind} {b.date} {b.scheme}"
            for b in books
            if b.kind != "quant" and b.scheme != "score_tilt"
        ]
        old_quant = one(
            c,
            "SELECT COUNT(*) FROM quant_portfolio WHERE kind <> 'live_book' "
            "AND engine_version NOT LIKE 'opt-v2%'",
        )
        relaxed = [f"{b.kind} {b.date}: {'; '.join(b.relaxations)}" for b in books if b.relaxations]
        missing = [k for k in ("live", "replay", "quant") if not by_kind.get(k)]
        counts = ", ".join(f"{k} {len(v)}" for k, v in sorted(by_kind.items()))
        if bad or legacy or not_tilt or missing or old_quant:
            status = "FAIL"
        elif relaxed:
            status = "WARN"
        else:
            status = "PASS"
        detail = (
            f"books checked: {counts or 'none'}; band/cap violations: "
            f"{[f'{b.kind} {b.date}: {v}' for b in bad for v in b.violations][:5] or 'none'}; "
            f"legacy (pre-T-136) books: {legacy[:5] or 'none'}; not score_tilt: {not_tilt[:5] or 'none'}; "
            f"missing kinds: {missing or 'none'}; relaxations (explain in the report): {relaxed[:5] or 'none'}"
        )
        detail += f"; quant books not opt-v2: {old_quant}"
        report("WI-18", status, detail)

    check("WI-18", wi18_books)

    def t145() -> None:
        paths = [("db", args.db)]
        if args.replay_db:
            paths.append(("replay-db", args.replay_db))
        bad: list[str] = []
        for label, path in paths:
            prefix = f"{label} " if len(paths) > 1 else ""
            db = connect_ro(path)
            unresolved = unresolved_run_references(db)
            invalid_accessions = invalid_sec_filings_accessions(db)
            for v in unresolved:
                bad.append(
                    f"{prefix}{v['table']}.{v['column']}={v['unresolved_count']} ({v['reason']})"
                )
            if invalid_accessions:
                bad.append(f"{prefix}invalid_sec_filings_accessions={len(invalid_accessions)}")
        report(
            "T-145",
            "FAIL" if bad else "PASS",
            "non-reusable IDs and reference integrity: " + (", ".join(bad) or "clean"),
        )

    check("T-145", t145)

    def t012() -> None:  # noqa: C901 - one branch per provenance invariant
        # The orchestrator's own provenance (T-012): every recorded run's steps are consistent
        # with its status. Read-only; a database with no orchestrator runs is SKIP, not FAIL.
        paths = [("db", args.db)]
        if args.replay_db:
            paths.append(("replay-db", args.replay_db))
        checked_runs = 0
        checked_steps = 0
        bad: list[str] = []
        for label, path in paths:
            prefix = f"{label} " if len(paths) > 1 else ""
            db = connect_ro(path)
            if not db.relation_exists("orchestrator_run"):
                continue
            run_rows = [
                tuple(r)
                for r in db.execute(
                    "SELECT id, status, finished_at FROM orchestrator_run"
                ).fetchall()
            ]
            if not run_rows:
                continue
            step_rows = [
                tuple(r)
                for r in db.execute(
                    "SELECT orchestrator_run_id, ordinal, status, run_kind, run_id, reason "
                    "FROM orchestrator_step ORDER BY orchestrator_run_id, ordinal"
                ).fetchall()
            ]
            by_run: dict[int, list[tuple]] = {}
            for s in step_rows:
                by_run.setdefault(int(s[0]), []).append(s)
            checked_runs += len(run_rows)
            checked_steps += len(step_rows)
            for run_id, status, finished_at in run_rows:
                steps = by_run.get(int(run_id), [])
                if status == "completed" and any(
                    s[2] not in ("completed", "skipped") for s in steps
                ):
                    bad.append(f"{prefix}run {run_id}: completed with a non-completed/skipped step")
                # T-013: a failed run may have several failed steps (independent failures).
                # T-015: or no failed step at all, when a graceful stop prevented the step it
                # landed on -- that step is `not_run` with reason `stopped: ...`.
                if status == "failed" and not (
                    any(s[2] == "failed" for s in steps)
                    or any(str(s[5] or "").startswith("stopped:") for s in steps)
                ):
                    bad.append(
                        f"{prefix}run {run_id}: failed without a failed step or a stop reason"
                    )
                if status in ("completed", "failed") and finished_at is None:
                    bad.append(f"{prefix}run {run_id}: finished run without finished_at")
                for s in steps:
                    if s[2] == "completed" and s[3] is not None and s[4] is None:
                        bad.append(
                            f"{prefix}run {run_id} step {s[1]}: completed but run_id is NULL"
                        )
        if checked_runs == 0:
            report("T-012", "SKIP", "no orchestrator runs recorded")
            return
        report(
            "T-012",
            "FAIL" if bad else "PASS",
            f"orchestrator provenance: {checked_runs} runs, {checked_steps} steps; "
            + ("; ".join(bad[:10]) or "clean"),
        )

    check("T-012", t012)

    def t013() -> None:  # noqa: C901 - one branch per isolation invariant
        # Per-step failure isolation (T-013): a step's direct upstreams (the runner's own
        # table, imported -- never a copy) decide whether it was blocked. Read-only; no
        # pipeline run recorded is SKIP, not FAIL.
        paths = [("db", args.db)]
        if args.replay_db:
            paths.append(("replay-db", args.replay_db))
        deps = {s.name: s.depends_on for s in orchestrator.steps_for("pipeline")}
        blocking = ("failed", "blocked")
        checked_runs = 0
        failed_steps = 0
        blocked_steps = 0
        bad: list[str] = []
        for label, path in paths:
            prefix = f"{label} " if len(paths) > 1 else ""
            db = connect_ro(path)
            if not db.relation_exists("orchestrator_run"):
                continue
            if not db.relation_exists("orchestrator_step"):
                continue
            run_rows = [
                tuple(r)
                for r in db.execute(
                    "SELECT id, status FROM orchestrator_run WHERE kind = 'pipeline'"
                ).fetchall()
            ]
            if not run_rows:
                continue
            by_run: dict[int, dict[str, str]] = {}
            for run_id, step, status in db.execute(
                "SELECT orchestrator_run_id, step, status FROM orchestrator_step"
            ).fetchall():
                by_run.setdefault(int(run_id), {})[str(step)] = str(status)
            checked_runs += len(run_rows)
            for run_id, run_status in run_rows:
                statuses = by_run.get(int(run_id), {})
                failed_steps += sum(1 for s in statuses.values() if s == "failed")
                blocked_steps += sum(1 for s in statuses.values() if s == "blocked")
                for step, status in statuses.items():
                    upstreams = deps.get(step, ())
                    if status in ("completed", "failed", "running") and any(
                        statuses.get(up) in blocking for up in upstreams
                    ):
                        bad.append(
                            f"{prefix}run {run_id}: {step} is {status} with a failed/blocked upstream"
                        )
                    if status == "blocked" and not any(
                        statuses.get(up) in blocking for up in upstreams
                    ):
                        bad.append(
                            f"{prefix}run {run_id}: {step} is blocked with no failed/blocked upstream"
                        )
                if run_status == "completed" and any(s == "blocked" for s in statuses.values()):
                    bad.append(f"{prefix}run {run_id}: completed with a blocked step")
        if checked_runs == 0:
            report("T-013", "SKIP", "no pipeline runs recorded")
            return
        report(
            "T-013",
            "FAIL" if bad else "PASS",
            f"per-step failure isolation: {checked_runs} runs, {failed_steps} failed steps, "
            f"{blocked_steps} blocked steps; " + ("; ".join(bad[:10]) or "clean"),
        )

    check("T-013", t013)

    def t015() -> None:  # noqa: C901 - one branch per T-015 invariant
        # The run lock and abandoned-run reconcile (T-015): at most one run per database is
        # `running`, an `abandoned` run has `finished_at` and no step left `running`/`pending`, and
        # an `abandoned` step belongs to an `abandoned` run. Read-only; no orchestrator run is SKIP.
        paths = [("db", args.db)]
        if args.replay_db:
            paths.append(("replay-db", args.replay_db))
        checked_runs = 0
        bad: list[str] = []
        for label, path in paths:
            prefix = f"{label} " if len(paths) > 1 else ""
            db = connect_ro(path)
            if not db.relation_exists("orchestrator_run"):
                continue
            runs = [
                tuple(r)
                for r in db.execute(
                    "SELECT id, status, finished_at FROM orchestrator_run"
                ).fetchall()
            ]
            if not runs:
                continue
            checked_runs += len(runs)
            run_status = {int(rid): str(st) for rid, st, _ in runs}
            abandoned = {int(rid) for rid, st, _ in runs if st == "abandoned"}
            running = [rid for rid, st, _ in runs if st == "running"]
            if len(running) > 1:
                bad.append(f"{prefix}{len(running)} runs are running at once: {running}")
            for rid, st, finished_at in runs:
                if st == "abandoned" and finished_at is None:
                    bad.append(f"{prefix}run {rid}: abandoned without finished_at")
            for run_id, step, status in db.execute(
                "SELECT orchestrator_run_id, step, status FROM orchestrator_step"
            ).fetchall():
                rid = int(run_id)
                if rid in abandoned and status in ("running", "pending"):
                    bad.append(f"{prefix}run {rid}: abandoned but step {step} is {status}")
                if status == "abandoned" and run_status.get(rid) != "abandoned":
                    bad.append(f"{prefix}step {step} of run {rid}: abandoned but its run is not")
        if checked_runs == 0:
            report("T-015", "SKIP", "no orchestrator runs recorded")
            return
        report(
            "T-015",
            "FAIL" if bad else "PASS",
            f"run lock / abandoned reconcile: {checked_runs} runs; "
            + ("; ".join(bad[:10]) or "clean"),
        )

    check("T-015", t015)

    def t017() -> None:  # noqa: C901 - one branch per incremental-run rule
        # The incremental run (T-017): a step is skipped "not pending" only when no direct upstream
        # ran on a pending signal, cycle_select is never skipped for being not pending, and every
        # skip says why. The dependency table and the always-run flag are the runner's own,
        # imported -- never a copy. Read-only; no pipeline run recorded is SKIP, not FAIL.
        paths = [("db", args.db)]
        if args.replay_db:
            paths.append(("replay-db", args.replay_db))
        specs = orchestrator.steps_for("pipeline")
        deps = {s.name: s.depends_on for s in specs}
        always = {s.name for s in specs if s.always_run}
        checked_runs = 0
        skipped_steps = 0
        bad: list[str] = []
        for label, path in paths:
            prefix = f"{label} " if len(paths) > 1 else ""
            db = connect_ro(path)
            if not db.relation_exists("orchestrator_run") or not db.relation_exists(
                "orchestrator_step"
            ):
                continue
            run_ids = [
                int(r[0])
                for r in db.execute(
                    "SELECT id FROM orchestrator_run WHERE kind = 'pipeline'"
                ).fetchall()
            ]
            by_run: dict[int, dict[str, tuple[str, str]]] = {}
            for run_id, step, status, reason in db.execute(
                "SELECT orchestrator_run_id, step, status, reason FROM orchestrator_step"
            ).fetchall():
                by_run.setdefault(int(run_id), {})[str(step)] = (str(status), str(reason or ""))
            checked_runs += len(run_ids)
            for run_id in run_ids:
                steps = by_run.get(run_id, {})
                ran_pending = {
                    step
                    for step, (status, reason) in steps.items()
                    if status == "completed" and reason.startswith("pending") and step not in always
                }
                for step, (status, reason) in steps.items():
                    if status != "skipped":
                        continue
                    skipped_steps += 1
                    where = f"{prefix}run {run_id}: {step}"
                    if reason.startswith("not pending") and any(
                        up in ran_pending for up in deps.get(step, ())
                    ):
                        bad.append(f"{where} skipped not pending with an upstream that ran pending")
                    if step in always and reason.startswith("not pending"):
                        bad.append(f"{where} is always-run but skipped not pending")
                    if not (
                        reason.startswith("not pending:")
                        or reason.startswith("no news source configured")
                    ):
                        bad.append(f"{where} skipped without a recognised reason ({reason!r})")
        if checked_runs == 0:
            report("T-017", "SKIP", "no pipeline runs recorded")
            return
        report(
            "T-017",
            "FAIL" if bad else "PASS",
            f"incremental run: {checked_runs} runs, {skipped_steps} skipped steps; "
            + ("; ".join(bad[:10]) or "clean"),
        )

    check("T-017", t017)

    def t083() -> None:
        paths = [("db", args.db)]
        if args.replay_db:
            paths.append(("replay-db", args.replay_db))
        bad: list[str] = []
        details: list[str] = []
        for label, path in paths:
            prefix = f"{label} " if len(paths) > 1 else ""
            db = connect_ro(path)
            sql_reconcile = (
                "SELECT b.as_of, a.ticker "
                "FROM (SELECT DISTINCT as_of FROM quant_portfolio WHERE kind <> 'live_book') b "
                "JOIN portfolio_position pp ON pp.valid_from <= b.as_of "
                "     AND (pp.valid_to IS NULL OR pp.valid_to > b.as_of) "
                "JOIN assets a ON a.id = pp.asset_id "
                "WHERE NOT EXISTS (SELECT 1 FROM v_quant_vs_live v "
                "                  WHERE v.as_of = b.as_of AND v.asset_id = pp.asset_id "
                "                    AND v.live_weight IS NOT NULL)"
            )
            rows = [tuple(r) for r in db.execute(sql_reconcile)]
            n_book_dates = int(
                db.execute(
                    "SELECT COUNT(DISTINCT as_of) FROM quant_portfolio WHERE kind <> 'live_book'"
                ).fetchone()[0]
            )
            sql_live_only = "SELECT COUNT(*) FROM v_quant_vs_live WHERE kind = 'LIVE_ONLY'"
            n_live_only = int(db.execute(sql_live_only).fetchone()[0])
            if rows:
                bad.append(f"{prefix}unreconciled={len(rows)} {rows[:10]}")
            item = f"{prefix}book dates={n_book_dates}, unreconciled={len(rows)}"
            if rows:
                item += f" {rows[:10]}"
            item += f", LIVE_ONLY={n_live_only}"
            details.append(item)
        report(
            "T-083",
            "FAIL" if bad else "PASS",
            "v_quant_vs_live reconciliation: " + ("; ".join(details) or "clean"),
        )

    check("T-083", t083)

    def t153a() -> None:
        universe_path = universe_database_path()
        paths = [("db", args.db)]
        if args.replay_db:
            paths.append(("replay-db", args.replay_db))
        dates_checked = 0
        bad: list[str] = []
        for label, path in paths:
            prefix = f"{label} " if len(paths) > 1 else ""
            n, mismatches = members_asof_mismatches(path, universe_path)
            dates_checked = max(dates_checked, n)
            bad += [f"{prefix}{m}" for m in mismatches]
        report(
            "T-153a",
            "FAIL" if bad else "PASS",
            f"members_asof vs the closed-interval read of {universe_path}: {dates_checked} dates; "
            + ("; ".join(bad[:6]) if bad else "agrees on every date"),
        )

    check("T-153a", t153a)

    def t153b() -> None:
        universe_path = universe_database_path()
        paths = [("db", args.db)]
        if args.replay_db:
            paths.append(("replay-db", args.replay_db))
        pairs = rows_checked = 0
        bad: list[str] = []
        for label, path in paths:
            prefix = f"{label} " if len(paths) > 1 else ""
            n_pairs, n_rows, problems = ticker_history_gaps(path, universe_path)
            pairs = max(pairs, n_pairs)
            rows_checked = max(rows_checked, n_rows)
            bad += [f"{prefix}{m}" for m in problems]
        report(
            "T-153b",
            "FAIL" if bad else "PASS",
            f"ticker history vs {universe_path}: {pairs} rename pairs, {rows_checked} rows; "
            + ("; ".join(bad[:6]) if bad else "no split asset, the file agrees on every row"),
        )

    check("T-153b", t153b)

    def t153c() -> None:
        universe_path = universe_database_path()
        paths = [("db", args.db)]
        if args.replay_db:
            paths.append(("replay-db", args.replay_db))
        dates = leavers = 0
        bad: list[str] = []
        for label, path in paths:
            prefix = f"{label} " if len(paths) > 1 else ""
            n_dates, n_leavers, problems = scope_problems(path, universe_path)
            dates = max(dates, n_dates)
            leavers = max(leavers, n_leavers)
            bad += [f"{prefix}{m}" for m in problems]
        report(
            "T-153c",
            "FAIL" if bad else "PASS",
            f"scope vs {universe_path}: {dates} cycle dates, {leavers} leaver lines; "
            + (
                "; ".join(bad[:6])
                if bad
                else "no leaver ranked outside its membership, nothing outside D2"
            ),
        )

    check("T-153c", t153c)

    def t153d() -> None:
        universe_path = universe_database_path()
        paths = [("db", args.db)]
        if args.replay_db:
            paths.append(("replay-db", args.replay_db))
        lines = filings = 0
        bad: list[str] = []
        for label, path in paths:
            prefix = f"{label} " if len(paths) > 1 else ""
            n_lines, n_filings, problems = leaver_profile_problems(path, universe_path)
            lines = max(lines, n_lines)
            filings = max(filings, n_filings)
            bad += [f"{prefix}{m}" for m in problems]
        report(
            "T-153d",
            "FAIL" if bad else "PASS",
            f"leaver profiles vs {universe_path}: {lines} leaver lines, {filings} filings; "
            + (
                "; ".join(bad[:6])
                if bad
                else "every filing is covered by its profile, no empty-named sector"
            ),
        )

    check("T-153d", t153d)

    fails = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    print(
        f"\n{len(RESULTS)} checks: {fails} FAIL, {sum(1 for _, s, _ in RESULTS if s == 'WARN')} WARN"
    )
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
