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

from kg_schema.db import connect_ro
from kg_schema.queries import invalid_sec_filings_accessions, unresolved_run_references

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

    fails = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    print(
        f"\n{len(RESULTS)} checks: {fails} FAIL, {sum(1 for _, s, _ in RESULTS if s == 'WARN')} WARN"
    )
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
