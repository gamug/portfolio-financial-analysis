"""Verification of T-070 (priceobs-v2, the three price vetoes, TECHNICAL v2) on scratch databases.

    uv run python scripts/verify_t070.py rebuild --db SCRATCH.db
    uv run python scripts/verify_t070.py vetoes --db SCRATCH.db --out DIR [--from D] [--to D]
    uv run python scripts/verify_t070.py relative --db SCRATCH.db --out DIR
    uv run python scripts/verify_t070.py coverage --db SCRATCH.db --out DIR
    uv run python scripts/verify_t070.py compare --old OLD.db --new NEW.db --out DIR
    uv run python scripts/verify_t070.py liquidity --db PILOT.db --out DIR

Every command that writes (``rebuild``, ``vetoes``) **refuses the production database** (the path
``KG_FINANCIAL_DB`` names, or anything under ``/workspaces/thesis/data/``): they run on scratch
copies only. ``relative``, ``coverage``, ``compare`` and ``liquidity`` open ``mode=ro``.

``rebuild``    migrates the copy to the current contract and rebuilds ``price_observation``
               (``priceobs-v2``) for every asset from ``price_daily`` with today's code.
``vetoes``     runs the three price rules through ``cycle``'s own rule classes and
               ``write_vetoes`` -- one daily cycle per trading day -- and reports, per rule, the
               fire counts per date, the HARD-vetoed share of the universe (stints held by the
               10-session hold), the shares by sector on the worst dates, and the stint lengths.
``relative``   the same HARD-vetoed share if VOLATILITY_SHOCK and CRASH_Z_SCORE were market- or
               sector-relative (the statistic standardized across the names of the date).
``coverage``   how many names have each TECHNICAL v2 input on the first trading day of each month.
``compare``    TECHNICAL v1 vs v2 (rank correlation per date) and the blended-rank change, from
               two ``cycle backfill`` databases.
``liquidity``  LIQUIDITY_DISTRESS before and after on the latest filings of a pilot database, a
               sensitivity table and the counts by sector.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from portfolio_common.db import Database

import kg_schema
from cycle import data, writers
from cycle.rules import RuleContext, enabled_rules, seed_catalog
from cycle.rules.base import hold_trading_days
from cycle.rules.builtin import _CrashZRule, _VolatilityShockRule
from cycle.scores import technical
from cycle.scores.technical import MIN_SECTOR_NAMES
from kg_schema.queries import connect_ro
from kg_schema.trading_calendar import is_trading_day
from pricing_agent import db as pricing_db
from pricing_agent.observations import build_observations

ENGINE = pricing_db.PRICE_OBSERVATION_ENGINE_VERSION
HARD_RULES = ("VOLATILITY_SHOCK", "CRASH_Z_SCORE")
PRODUCTION_PREFIX = "/workspaces/thesis/data/"
CHECKPOINT_SHARE = 0.20


class ProductionRefused(RuntimeError):
    """A writing command was pointed at the production database."""


def refuse_production(path: str | Path) -> None:
    resolved = Path(path).expanduser().resolve()
    env = os.environ.get("KG_FINANCIAL_DB")
    if str(resolved).startswith(PRODUCTION_PREFIX) or (
        env and resolved == Path(env).expanduser().resolve()
    ):
        raise ProductionRefused(f"{resolved} is the production database; use a scratch copy")


# -- stats helpers (pure; tested) ------------------------------------------------------------


def median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile of *values* (``q`` in [0, 1])."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def trading_days_between(start: str, end: str) -> int:
    """The NYSE sessions after *start* up to and including *end*."""
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    n = 0
    d = d0
    while d < d1:
        d = date.fromordinal(d.toordinal() + 1)
        if is_trading_day(d):
            n += 1
    return n


def spearman(a: Sequence[float], b: Sequence[float]) -> float | None:
    """Spearman rank correlation (average ranks for ties); ``None`` under 3 pairs or no spread."""
    n = len(a)
    if n != len(b) or n < 3:  # noqa: PLR2004
        return None

    def ranks(values: Sequence[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: values[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[order[j + 1]] == values[order[i]]:
                j += 1
            for k in range(i, j + 1):
                out[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return out

    ra, rb = ranks(a), ranks(b)
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else None


class StintBook:
    """The temporal stint semantics (hold H sessions, extension while the condition persists),
    kept in memory -- ``writers.write_vetoes`` re-implemented without a database, to evaluate the
    relative variants on the same daily cycles. Checked against the real writer by ``vetoes``."""

    def __init__(self, hold: int) -> None:
        self.hold = hold
        self.expires: dict[int, int] = {}  # asset -> session index the stint cannot clear before

    def step(self, day: int, hit: set[int], evaluated: set[int]) -> set[int]:
        """Apply cycle *day* (a session index); returns the assets whose stint is open after it."""
        for a in evaluated:
            if a in hit:
                if a not in self.expires or day >= self.expires[a]:
                    self.expires[a] = day + self.hold
            elif a in self.expires and day >= self.expires[a]:
                del self.expires[a]
        return set(self.expires)


# -- rebuild -----------------------------------------------------------------------------------


def cmd_rebuild(args: argparse.Namespace) -> int:
    refuse_production(args.db)
    conn = kg_schema.connect(args.db)
    try:
        pricing_db.ensure_schema(conn)
        applied = kg_schema.ensure(conn, run_migrations=True)
        print(f"migrations applied: {applied}")
        asset_ids = [int(r[0]) for r in conn.execute("SELECT DISTINCT asset_id FROM price_daily")]
        rows = 0
        for i, aid in enumerate(sorted(asset_ids), start=1):
            candles = pricing_db.load_daily_candles(conn, aid, end="9999-12-31")
            obs = build_observations(candles, engine_version=ENGINE)
            rows += pricing_db.upsert_price_observations(conn, aid, obs, engine_version=ENGINE)
            if i % 50 == 0:
                print(f"  {i}/{len(asset_ids)} assets, {rows} rows")
        print(f"rebuilt {rows} {ENGINE} rows for {len(asset_ids)} assets")
    finally:
        conn.close()
    return 0


# -- vetoes ------------------------------------------------------------------------------------


def _sectors(conn: Database) -> dict[int, str]:
    return {
        int(r["id"]): str(r["name"])
        for r in conn.execute(
            "SELECT a.id, s.name FROM assets a JOIN sectors s ON s.id = a.sector_id"
        )
    }


def _trading_dates(conn: Database, start: str, end: str) -> list[str]:
    return [
        str(r[0])
        for r in conn.execute(
            "SELECT DISTINCT obs_date FROM price_observation WHERE engine_version = ? "
            "AND obs_date BETWEEN ? AND ? ORDER BY obs_date",
            (ENGINE, start, end),
        )
    ]


def simulate_vetoes(conn: Database, dates: Sequence[str]) -> dict[str, Any]:
    """One daily cycle per date through the real rule classes and ``write_vetoes``."""
    seed_catalog(conn)
    conn.execute("DELETE FROM veto")
    conn.commit()
    price_rules = [r for r in enabled_rules(conn) if r.RULE_ID in {*HARD_RULES, "BREAK_TREND_200"}]
    hold = {r.RULE_ID: n for r in price_rules if (n := hold_trading_days(r)) is not None}
    sectors = _sectors(conn)
    per_date: list[dict[str, Any]] = []
    for cycle_no, d in enumerate(dates, start=1):
        obs = data.latest_price_observation(conn, d, ENGINE)
        ctx = RuleContext(
            cycle_date=d, metrics={}, price_obs=obs, last_fundamental={}, sectors=dict(sectors)
        )
        results = [(r.RULE_ID, r.evaluate(ctx)) for r in price_rules]
        hits = [h for _, res in results for h in res]
        writers.write_vetoes(
            conn,
            d,
            hits,
            {rid: res.evaluated for rid, res in results},
            set(),
            run_id=0,
            hold_days=hold,
        )
        held: dict[str, list[int]] = defaultdict(list)
        for r in conn.execute(
            "SELECT rule_id, asset_id FROM veto WHERE cleared_on IS NULL AND severity = 'HARD'"
        ):
            held[str(r["rule_id"])].append(int(r["asset_id"]))
        fired: dict[str, list[int]] = defaultdict(list)
        for h in hits:
            fired[h.rule_id].append(h.asset_id)
        per_date.append(
            {
                "date": d,
                "observed": len(obs),
                "fired": {k: sorted(v) for k, v in fired.items()},
                "held": {k: sorted(v) for k, v in held.items()},
            }
        )
        if cycle_no % 200 == 0:
            print(f"  {cycle_no}/{len(dates)} cycles", flush=True)
    return {"per_date": per_date, "sectors": sectors}


def summarize_vetoes(sim: Mapping[str, Any], universe: int, dates: Sequence[str]) -> dict[str, Any]:
    per_date = sim["per_date"]
    sectors: Mapping[int, str] = sim["sectors"]
    sector_size = Counter(sectors.values())
    out: dict[str, Any] = {"universe": universe, "cycles": len(per_date), "rules": {}}
    for rule in (*HARD_RULES, "BREAK_TREND_200"):
        fires = [len(p["fired"].get(rule, [])) for p in per_date]
        out["rules"][rule] = {
            "fire_median": median(fires),
            "fire_p95": percentile(fires, 0.95),
            "fire_max": max(fires, default=0),
            "fire_max_date": per_date[fires.index(max(fires))]["date"] if fires else None,
        }
    union_held = [sorted({a for r in HARD_RULES for a in p["held"].get(r, [])}) for p in per_date]
    union_fired = [sorted({a for r in HARD_RULES for a in p["fired"].get(r, [])}) for p in per_date]
    share_held = [len(u) / universe for u in union_held]
    share_fired = [len(u) / universe for u in union_fired]
    out["hard_share_held"] = {
        "over_10pct": sum(1 for x in share_held if x > 0.10),  # noqa: PLR2004
        "median": median(share_held),
        "p95": percentile(share_held, 0.95),
        "max": max(share_held, default=0),
        "dates_over_checkpoint": [
            {"date": per_date[i]["date"], "share": share_held[i]}
            for i in range(len(per_date))
            if share_held[i] > CHECKPOINT_SHARE
        ],
    }
    out["hard_share_fired"] = {
        "median": median(share_fired),
        "p95": percentile(share_fired, 0.95),
        "max": max(share_fired, default=0),
        "dates_over_checkpoint": [
            {"date": per_date[i]["date"], "share": share_fired[i]}
            for i in range(len(per_date))
            if share_fired[i] > CHECKPOINT_SHARE
        ],
    }
    worst = sorted(range(len(per_date)), key=lambda i: -share_held[i])[:12]
    out["worst_dates"] = []
    for i in worst:
        by_sector = Counter(sectors.get(a, "?") for a in union_held[i])
        out["worst_dates"].append(
            {
                "date": per_date[i]["date"],
                "held": len(union_held[i]),
                "share_held": share_held[i],
                "fired": len(union_fired[i]),
                "share_fired": share_fired[i],
                "by_sector": {
                    s: {"names": n, "share_of_sector": n / sector_size[s]}
                    for s, n in sorted(by_sector.items(), key=lambda kv: -kv[1])
                },
            }
        )
    out["share_by_year"] = {}
    for year in sorted({d[:4] for d in dates}):
        sh = [share_held[i] for i, p in enumerate(per_date) if p["date"][:4] == year]
        out["share_by_year"][year] = {"median": median(sh), "max": max(sh, default=0)}
    return out


def stint_lengths(conn: Database) -> dict[str, Any]:
    """Length, in sessions, of every cleared stint per price rule; the open ones counted."""
    out: dict[str, Any] = {}
    for rule in (*HARD_RULES, "BREAK_TREND_200"):
        lengths = []
        extended = 0
        total = open_ = 0
        for r in conn.execute(
            "SELECT raised_on, cleared_on, expiry_history_json FROM veto WHERE rule_id = ?",
            (rule,),
        ):
            total += 1
            if r["expiry_history_json"]:
                extended += 1
            if r["cleared_on"] is None:
                open_ += 1
            else:
                lengths.append(trading_days_between(str(r["raised_on"]), str(r["cleared_on"])))
        out[rule] = {
            "stints": total,
            "open": open_,
            "extended": extended,
            "length_median": median(lengths),
            "length_p90": percentile(lengths, 0.9),
            "length_max": max(lengths, default=None),
            "length_min": min(lengths, default=None),
        }
    return out


def cmd_vetoes(args: argparse.Namespace) -> int:
    refuse_production(args.db)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = kg_schema.connect(args.db)
    try:
        dates = _trading_dates(conn, args.date_from, args.date_to)
        universe = int(
            conn.execute("SELECT COUNT(DISTINCT asset_id) FROM price_daily").fetchone()[0]
        )
        print(f"{len(dates)} trading dates, universe {universe}")
        sim = simulate_vetoes(conn, dates)
        summary = summarize_vetoes(sim, universe, dates)
        summary["stints"] = stint_lengths(conn)
        (out_dir / "vetoes_summary.json").write_text(json.dumps(summary, indent=1, default=str))
        slim = [
            {
                "date": p["date"],
                "observed": p["observed"],
                "fired": {k: len(v) for k, v in p["fired"].items()},
                "held": {k: len(v) for k, v in p["held"].items()},
            }
            for p in sim["per_date"]
        ]
        (out_dir / "vetoes_per_date.json").write_text(json.dumps(slim))
        print(json.dumps({k: summary[k] for k in ("rules", "hard_share_held")}, indent=1))
        over = summary["hard_share_held"]["dates_over_checkpoint"]
        print(f"CHECKPOINT: {len(over)} date(s) with more than {CHECKPOINT_SHARE:.0%} HARD-held")
    finally:
        conn.close()
    return 0


# -- absolute vs sector-relative: the breadth series, fallback, systematic moves, events -----------


def _load_matrix(
    conn: Database,
) -> tuple[list[str], dict[str, dict[int, dict[str, Any]]]]:
    """Every priceobs-v2 row's price-veto inputs, by date then asset (obs_date included)."""
    by_date: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for r in conn.execute(
        "SELECT asset_id, obs_date, vol_5d, vol_60d_base, ret_5d, mu_60d_base "
        "FROM price_observation WHERE engine_version = ?",
        (ENGINE,),
    ):
        by_date[str(r["obs_date"])][int(r["asset_id"])] = {
            "obs_date": str(r["obs_date"]),
            "vol_5d": r["vol_5d"],
            "vol_60d_base": r["vol_60d_base"],
            "ret_5d": r["ret_5d"],
            "mu_60d_base": r["mu_60d_base"],
        }
    return sorted(by_date), by_date


def absolute_flags(rows: Mapping[int, Mapping[str, Any]]) -> tuple[set[int], set[int]]:
    """The names the *absolute* rules (thresholds only, no relative leg) fire on."""
    vol: set[int] = set()
    crash: set[int] = set()
    for a, row in rows.items():
        if row["vol_5d"] is not None and row["vol_60d_base"]:
            vol.add(a) if row["vol_5d"] / row["vol_60d_base"] > 2.5 else None  # noqa: PLR2004
        if row["ret_5d"] is not None and row["mu_60d_base"] is not None and row["vol_60d_base"]:
            z = (row["ret_5d"] - 5 * row["mu_60d_base"]) / (row["vol_60d_base"] * math.sqrt(5))
            crash.add(a) if z < -2.5 else None  # noqa: PLR2004
    return vol, crash


def rule_flags(
    rows: Mapping[int, Mapping[str, Any]], day: str, sectors: Mapping[int, str | None]
) -> tuple[set[int], set[int]]:
    """The names the real VOLATILITY_SHOCK / CRASH_Z_SCORE classes fire on, given *sectors* (an
    empty mapping makes every group the whole cross-section: the market-relative variant)."""
    ctx = RuleContext(
        cycle_date=day,
        metrics={},
        price_obs={a: dict(r) for a, r in rows.items()},
        last_fundamental={},
        sectors=dict(sectors),
    )
    vol = {h.asset_id for h in _VolatilityShockRule().evaluate(ctx)}
    crash = {h.asset_id for h in _CrashZRule().evaluate(ctx)}
    return vol, crash


def fallback_counts(
    rows: Mapping[int, Mapping[str, Any]], sectors: Mapping[int, str | None]
) -> dict[str, int]:
    """How the sector groups of one date resolve, per rule: the sector-date groups, how many fall
    back to the cross-section (fewer than ``MIN_SECTOR_NAMES`` evaluated names), and the evaluated
    names judged against the cross-section for that reason (or because they have no sector)."""
    out: dict[str, int] = {}
    inputs = {
        "VOLATILITY_SHOCK": {
            a for a, r in rows.items() if r["vol_5d"] is not None and r["vol_60d_base"]
        },
        "CRASH_Z_SCORE": {
            a
            for a, r in rows.items()
            if r["ret_5d"] is not None and r["mu_60d_base"] is not None and r["vol_60d_base"]
        },
    }
    for rule, names in inputs.items():
        sizes = Counter(sectors.get(a) for a in names if sectors.get(a) is not None)
        small = {s for s, n in sizes.items() if n < MIN_SECTOR_NAMES}
        out[f"{rule}.groups"] = len(sizes)
        out[f"{rule}.groups_fallback"] = len(small)
        out[f"{rule}.names"] = len(names)
        out[f"{rule}.names_fallback"] = sum(
            1 for a in names if sectors.get(a) is None or sectors.get(a) in small
        )
    return out


def breadth(shares: Sequence[float]) -> dict[str, Any]:
    return {
        "median": median(shares),
        "p95": percentile(shares, 0.95),
        "max": max(shares, default=0.0),
        "over_10pct": sum(1 for x in shares if x > 0.10),  # noqa: PLR2004
        "over_20pct": sum(1 for x in shares if x > CHECKPOINT_SHARE),
        "cycles": len(shares),
    }


def cmd_relative(args: argparse.Namespace) -> int:
    """Absolute vs market-relative vs sector-relative, from the same priceobs-v2 rows."""
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = connect_ro(args.db)
    try:
        sectors: dict[int, str | None] = dict(_sectors(conn))
        tickers = {
            int(r["id"]): str(r["ticker"]) for r in conn.execute("SELECT id, ticker FROM assets")
        }
        universe = int(
            conn.execute("SELECT COUNT(DISTINCT asset_id) FROM price_daily").fetchone()[0]
        )
        dates, by_date = _load_matrix(conn)
    finally:
        conn.close()
    hold = _VolatilityShockRule.HOLD_TRADING_DAYS
    variants = ("absolute", "market_relative", "sector_relative")
    cadence = {"daily": 1, "weekly": 5}
    books = {(v, c): (StintBook(hold), StintBook(hold)) for v in variants for c in cadence}
    held_share: dict[tuple[str, str], list[float]] = defaultdict(list)
    fired_share: dict[str, list[float]] = defaultdict(list)
    fired: dict[str, dict[str, set[int]]] = {v: {} for v in variants}
    fallback: Counter[str] = Counter()
    systematic: list[dict[str, Any]] = []
    sector_names = Counter(sectors.values())
    for day, d in enumerate(dates):
        rows = by_date[d]
        flags = {
            "absolute": absolute_flags(rows),
            "market_relative": rule_flags(rows, d, {}),
            "sector_relative": rule_flags(rows, d, sectors),
        }
        for key, count in fallback_counts(rows, sectors).items():
            fallback[key] += count
        fallback["dates"] += 1
        evaluated_vol = {
            a for a, r in rows.items() if r["vol_5d"] is not None and r["vol_60d_base"]
        }
        evaluated_crash = {
            a
            for a, r in rows.items()
            if r["ret_5d"] is not None and r["mu_60d_base"] is not None and r["vol_60d_base"]
        }
        for v in variants:
            vol, crash = flags[v]
            fired[v][d] = vol | crash
            fired_share[v].append(len(vol | crash) / universe)
            for c, every in cadence.items():
                if day % every:
                    continue
                vb, cb = books[(v, c)]
                held = vb.step(day, vol, evaluated_vol) | cb.step(day, crash, evaluated_crash)
                held_share[(v, c)].append(len(held) / universe)
        # systematic moves: >= 50% of a sector's names fire the absolute rules that day
        by_sector: dict[str, list[int]] = defaultdict(list)
        for a in evaluated_vol | evaluated_crash:
            if sectors.get(a) is not None:
                by_sector[str(sectors[a])].append(a)
        for sector, names in by_sector.items():
            abs_fired = fired["absolute"][d] & set(names)
            rel_fired = fired["sector_relative"][d] & set(names)
            if len(abs_fired) / len(names) >= 0.5 and len(rel_fired) / len(names) < 0.5:  # noqa: PLR2004
                systematic.append(
                    {
                        "date": d,
                        "sector": sector,
                        "sector_names": len(names),
                        "absolute_fired": len(abs_fired),
                        "sector_relative_fired": len(rel_fired),
                        "dropped": sorted(tickers[a] for a in abs_fired - rel_fired),
                        "kept": sorted(tickers[a] for a in rel_fired),
                    }
                )
    summary: dict[str, Any] = {
        "universe": universe,
        "dates": len(dates),
        "held": {f"{v}.{c}": breadth(held_share[(v, c)]) for v in variants for c in cadence},
        "fired": {v: breadth(fired_share[v]) for v in variants},
        "fallback": dict(fallback),
        "sector_sizes": dict(sector_names),
        "systematic_dates": len(systematic),
    }
    (out_dir / "relative_summary.json").write_text(json.dumps(summary, indent=1))
    (out_dir / "systematic_moves.json").write_text(json.dumps(systematic))
    events = event_report(dates, by_date, fired, tickers, sectors)
    (out_dir / "events.json").write_text(json.dumps(events, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


NAMED_EVENTS = (
    ("INTC", "2024-08-02"),
    ("UNH", "2025-04-17"),
    ("UNH", "2025-05-13"),
    ("CRWD", "2024-07-19"),
)


def event_report(
    dates: Sequence[str],
    by_date: Mapping[str, Mapping[int, Mapping[str, Any]]],
    fired: Mapping[str, Mapping[str, set[int]]],
    tickers: Mapping[int, str],
    sectors: Mapping[int, str | None],
) -> dict[str, Any]:
    """Which named single-name events the absolute and sector-relative rules fire on within three
    sessions of the date, and the sector-relative CRASH_Z_SCORE's strongest hits."""
    ids = {t: a for a, t in tickers.items()}
    named = []
    for ticker, d in NAMED_EVENTS:
        a = ids.get(ticker)
        if a is None or d not in dates:
            named.append({"ticker": ticker, "date": d, "in_data": False})
            continue
        i = dates.index(d)
        entry: dict[str, Any] = {"ticker": ticker, "date": d, "in_data": True}
        for variant in ("absolute", "sector_relative"):
            first = next(
                (
                    dates[j]
                    for j in range(i, min(i + 4, len(dates)))
                    if a in fired[variant][dates[j]]
                ),
                None,
            )
            entry[variant] = first
        named.append(entry)
    top = []
    for d in dates:
        rows = by_date[d]
        _vol, crash = rule_flags(rows, d, sectors)
        for a in crash:
            r = rows[a]
            z = (r["ret_5d"] - 5 * r["mu_60d_base"]) / (r["vol_60d_base"] * math.sqrt(5))
            top.append(
                {
                    "date": d,
                    "ticker": tickers[a],
                    "sector": sectors.get(a),
                    "ret_5d": math.expm1(r["ret_5d"]),
                    "z": z,
                }
            )
    top.sort(key=lambda e: e["z"])
    return {"named": named, "strongest_crash_hits": top[:40], "crash_hits": len(top)}


# -- coverage ----------------------------------------------------------------------------------


def cmd_coverage(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = connect_ro(args.db)
    try:
        dates = _trading_dates(conn, "0000-00-00", "9999-99-99")
        firsts: dict[str, str] = {}
        for d in dates:
            firsts.setdefault(d[:7], d)
        universe = int(
            conn.execute("SELECT COUNT(DISTINCT asset_id) FROM price_daily").fetchone()[0]
        )
        rows = []
        for month, d in sorted(firsts.items()):
            obs = data.latest_price_observation(conn, d, ENGINE)
            n = {"mom_12_1": 0, "realized_vol_90d": 0, "max_drawdown_90d": 0}
            ge2 = 0
            for row in obs.values():
                present = 0
                for name, v in (
                    ("mom_12_1", technical.mom_12_1(row)),
                    ("realized_vol_90d", row.get("realized_vol_90d")),
                    ("max_drawdown_90d", row.get("max_drawdown_90d")),
                ):
                    if v is not None:
                        n[name] += 1
                        present += 1
                ge2 += present >= technical.MIN_SIGNALS
            rows.append(
                {
                    "month": month,
                    "date": d,
                    "observed": len(obs),
                    **n,
                    "at_least_2": ge2,
                    "scored_share": ge2 / universe,
                }
            )
    finally:
        conn.close()
    (out_dir / "technical_coverage.json").write_text(json.dumps(rows, indent=1))
    print(f"universe {universe}")
    for r in rows:
        print(
            f"{r['date']} observed {r['observed']:3d} mom {r['mom_12_1']:3d} vol {r['realized_vol_90d']:3d} "
            f"dd {r['max_drawdown_90d']:3d} >=2: {r['at_least_2']:3d} ({r['scored_share']:.0%})"
        )
    return 0


# -- compare: two cycle backfill databases, old code vs new -------------------------------------


def _by_date_score(conn: Database, score_type: str) -> dict[str, dict[int, float]]:
    out: dict[str, dict[int, float]] = defaultdict(dict)
    for r in conn.execute(
        "SELECT event_time, asset_id, raw_value FROM score_snapshot "
        "WHERE score_type = ? AND raw_value IS NOT NULL",
        (score_type,),
    ):
        out[str(r["event_time"])][int(r["asset_id"])] = float(r["raw_value"])
    return out


def _replay_rankings(conn: Database) -> dict[str, dict[int, dict[str, Any]]]:
    out: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for r in conn.execute(
        "SELECT cr.cycle_date, k.asset_id, k.rank, k.blended_score, k.vetoed, k.selected, "
        "k.veto_rules_json FROM cycle_ranking k JOIN cycle_run cr ON cr.id = k.cycle_run_id "
        "WHERE cr.cycle_type = 'REPLAY'"
    ):
        out[str(r["cycle_date"])][int(r["asset_id"])] = {
            "rank": int(r["rank"]),
            "blended": float(r["blended_score"]),
            "vetoed": int(r["vetoed"]),
            "selected": int(r["selected"]),
            "rules": json.loads(r["veto_rules_json"] or "[]"),
        }
    return out


_VETO_STINTS_SQL = (
    "SELECT a.ticker, v.rule_id, v.severity, v.raised_on, v.cleared_on, v.last_seen_on, "
    "v.expires_on, v.expiry_history_json FROM veto v JOIN assets a ON a.id = v.asset_id "
    "ORDER BY v.raised_on, a.ticker"
)
_VETO_STINTS_PRE_T070_SQL = (
    "SELECT a.ticker, v.rule_id, v.severity, v.raised_on, v.cleared_on, v.last_seen_on, "
    "NULL AS expires_on, NULL AS expiry_history_json FROM veto v "
    "JOIN assets a ON a.id = v.asset_id ORDER BY v.raised_on, a.ticker"
)


def _veto_stints(conn: Database) -> list[dict[str, Any]]:
    """Every veto stint; a database that predates m011 has no expiry columns (read as NULL)."""
    sql = (
        _VETO_STINTS_SQL
        if "expires_on" in conn.table_columns("veto")
        else _VETO_STINTS_PRE_T070_SQL
    )
    return [dict(r) for r in conn.execute(sql)]


def cmd_compare(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    old, new = connect_ro(args.old), connect_ro(args.new)
    try:
        t_old, t_new = _by_date_score(old, "TECHNICAL"), _by_date_score(new, "TECHNICAL")
        r_old, r_new = _replay_rankings(old), _replay_rankings(new)
        dates = sorted(set(r_old) & set(r_new))
        tech: list[dict[str, Any]] = []
        for d in dates:
            common = sorted(set(t_old.get(d, {})) & set(t_new.get(d, {})))
            rho = spearman([t_old[d][a] for a in common], [t_new[d][a] for a in common])
            tech.append(
                {
                    "date": d,
                    "n_v1": len(t_old.get(d, {})),
                    "n_v2": len(t_new.get(d, {})),
                    "spearman": rho,
                }
            )
        rhos = [t["spearman"] for t in tech if t["spearman"] is not None]
        blended: list[dict[str, Any]] = []
        for d in dates:
            common = sorted(set(r_old[d]) & set(r_new[d]))
            rho = spearman(
                [r_old[d][a]["blended"] for a in common], [r_new[d][a]["blended"] for a in common]
            )
            top_old = {a for a in common if r_old[d][a]["selected"]}
            top_new = {a for a in common if r_new[d][a]["selected"]}
            union = top_old | top_new
            blended.append(
                {
                    "date": d,
                    "spearman": rho,
                    "book_old": len(top_old),
                    "book_new": len(top_new),
                    "book_overlap": len(top_old & top_new) / len(union) if union else None,
                    "hard_old": sum(1 for a in common if "HARD" in r_old[d][a]["rules"]),
                    "hard_new": sum(1 for a in common if "HARD" in r_new[d][a]["rules"]),
                }
            )
        stints_old, stints_new = _veto_stints(old), _veto_stints(new)
    finally:
        old.close()
        new.close()

    def per_rule(stints: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = defaultdict(lambda: {"stints": 0, "open": 0, "cleared": 0})
        for s in stints:
            out[s["rule_id"]]["stints"] += 1
            out[s["rule_id"]]["open" if s["cleared_on"] is None else "cleared"] += 1
        return dict(out)

    price = [s for s in stints_new if s["rule_id"] in (*HARD_RULES, "BREAK_TREND_200")]
    # a stint "held": it stayed open at least one cycle after its condition was last seen
    held = [
        s
        for s in price
        if s["rule_id"] in HARD_RULES and s["cleared_on"] and s["last_seen_on"] < s["cleared_on"]
    ]
    summary = {
        "dates": len(dates),
        "technical_v1_v2": {
            "median": median(rhos),
            "min": min(rhos, default=None),
            "max": max(rhos, default=None),
            "p10": percentile(rhos, 0.10),
            "by_year": {
                y: median(
                    [
                        t["spearman"]
                        for t in tech
                        if t["date"][:4] == y and t["spearman"] is not None
                    ]
                )
                for y in sorted({t["date"][:4] for t in tech})
            },
            "dates_with_fewer_scored": [t["date"] for t in tech if t["n_v2"] < t["n_v1"]],
        },
        "blended_old_vs_new": {
            "median": median([b["spearman"] for b in blended if b["spearman"] is not None]),
            "min": min((b["spearman"] for b in blended if b["spearman"] is not None), default=None),
            "book_overlap_median": median(
                [b["book_overlap"] for b in blended if b["book_overlap"] is not None]
            ),
            "book_overlap_min": min(
                (b["book_overlap"] for b in blended if b["book_overlap"] is not None), default=None
            ),
            "hard_vetoed_median": {
                "old": median([b["hard_old"] for b in blended]),
                "new": median([b["hard_new"] for b in blended]),
                "new_max": max(b["hard_new"] for b in blended),
            },
        },
        "vetoes_old": per_rule(stints_old),
        "vetoes_new": per_rule(stints_new),
        "temporal_stints": {
            "total": len([s for s in price if s["rule_id"] in HARD_RULES]),
            "extended": len([s for s in price if s["expiry_history_json"]]),
            "held_past_last_seen": len(held),
        },
        "sample_temporal": [s for s in price if s["rule_id"] in HARD_RULES][:12],
        "sample_held": held[:8],
    }
    (out_dir / "compare_summary.json").write_text(json.dumps(summary, indent=1, default=str))
    (out_dir / "compare_technical.json").write_text(json.dumps(tech))
    (out_dir / "compare_blended.json").write_text(json.dumps(blended))
    (out_dir / "pilot_vetoes_new.json").write_text(json.dumps(stints_new, default=str))
    (out_dir / "pilot_vetoes_old.json").write_text(json.dumps(stints_old, default=str))
    print(json.dumps(summary, indent=1, default=str))
    return 0


# -- liquidity: LIQUIDITY_DISTRESS before and after, with a sensitivity table ---------------------

COVERAGE_FLOORS = (1.0, 1.5, 2.0, 3.0)
OCF_FLOORS = (-0.05, 0.0, 0.05, 0.10)


def liquidity_flags(
    ratio: float,
    coverage: float | None,
    ocf_margin: float | None,
    ic_floor: float,
    ocf_floor: float,
) -> bool:
    """The new rule's verdict for one set of metrics at the given floors (None = not flagged)."""
    if ratio >= 1.0:
        return False
    weak = coverage is not None and coverage < ic_floor
    burning = ocf_margin is not None and ocf_margin < ocf_floor
    return weak or burning


def cmd_liquidity(args: argparse.Namespace) -> int:  # noqa: C901 - a report: one linear pass
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = connect_ro(args.db)
    try:
        version = str(
            conn.execute(
                "SELECT engine_version FROM fundamental_metrics ORDER BY engine_version DESC LIMIT 1"
            ).fetchone()[0]
        )
        sectors = _sectors(conn)
        wanted = {
            "liquidity.current_ratio",
            "leverage.interest_coverage",
            "cashflow.operating_cash_flow_margin",
        }
        filings: dict[int, dict[str, Any]] = {}
        for r in conn.execute(
            "SELECT f.id, f.asset_id, a.ticker, f.form, f.period_end, m.metric_group, m.metric_name, "
            "m.value FROM fundamental_metrics m JOIN sec_filings f ON f.id = m.filing_id "
            "JOIN assets a ON a.id = f.asset_id WHERE m.engine_version = ?",
            (version,),
        ):
            key = f"{r['metric_group']}.{r['metric_name']}"
            if key not in wanted:
                continue
            row = filings.setdefault(
                int(r["id"]),
                {
                    "ticker": r["ticker"],
                    "asset_id": int(r["asset_id"]),
                    "period_end": r["period_end"],
                },
            )
            row[key] = r["value"]
    finally:
        conn.close()
    exempt = {"Financials", "Utilities"}
    rows = [f for f in filings.values() if f.get("liquidity.current_ratio") is not None]
    table: list[dict[str, Any]] = []
    by_sector: dict[str, dict[str, int]] = defaultdict(
        lambda: {"filings": 0, "old": 0, "new": 0, "exempt": 0}
    )
    for f in rows:
        sector = sectors.get(f["asset_id"], "?")
        by_sector[sector]["filings"] += 1
        old = f["liquidity.current_ratio"] < 1.0
        by_sector[sector]["old"] += old
        if sector in exempt:
            by_sector[sector]["exempt"] += old
        elif liquidity_flags(
            f["liquidity.current_ratio"],
            f.get("leverage.interest_coverage"),
            f.get("cashflow.operating_cash_flow_margin"),
            1.5,
            0.0,
        ):
            by_sector[sector]["new"] += 1
    for ic_floor in COVERAGE_FLOORS:
        for ocf_floor in OCF_FLOORS:
            flagged = [
                f
                for f in rows
                if sectors.get(f["asset_id"], "?") not in exempt
                and liquidity_flags(
                    f["liquidity.current_ratio"],
                    f.get("leverage.interest_coverage"),
                    f.get("cashflow.operating_cash_flow_margin"),
                    ic_floor,
                    ocf_floor,
                )
            ]
            table.append(
                {
                    "interest_coverage_floor": ic_floor,
                    "ocf_margin_floor": ocf_floor,
                    "filings_flagged": len(flagged),
                    "tickers_ever": sorted({f["ticker"] for f in flagged}),
                }
            )
    # the latest filing of each asset: who is flagged before and after
    latest: dict[int, dict[str, Any]] = {}
    for f in rows:
        cur = latest.get(f["asset_id"])
        if cur is None or str(f["period_end"]) > str(cur["period_end"]):
            latest[f["asset_id"]] = f
    now: list[dict[str, Any]] = []
    for f in sorted(latest.values(), key=lambda x: x["ticker"]):
        sector = sectors.get(f["asset_id"], "?")
        cr = f["liquidity.current_ratio"]
        after = sector not in exempt and liquidity_flags(
            cr,
            f.get("leverage.interest_coverage"),
            f.get("cashflow.operating_cash_flow_margin"),
            1.5,
            0.0,
        )
        now.append(
            {
                "ticker": f["ticker"],
                "sector": sector,
                "period_end": f["period_end"],
                "current_ratio": cr,
                "interest_coverage": f.get("leverage.interest_coverage"),
                "ocf_margin": f.get("cashflow.operating_cash_flow_margin"),
                "before": cr < 1.0,
                "after": after,
                "exempt": sector in exempt,
            }
        )
    out = {
        "metrics_version": version,
        "filings_with_current_ratio": len(rows),
        "by_sector": dict(by_sector),
        "sensitivity": table,
        "latest": now,
    }
    (out_dir / "liquidity.json").write_text(json.dumps(out, indent=1, default=str))
    print(
        json.dumps(
            {k: out[k] for k in ("metrics_version", "filings_with_current_ratio", "by_sector")},
            indent=1,
        )
    )
    for n in now:
        print(n)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("rebuild")
    p.add_argument("--db", required=True)
    p = sub.add_parser("vetoes")
    p.add_argument("--db", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--from", dest="date_from", default="2022-01-01")
    p.add_argument("--to", dest="date_to", default="2026-12-31")
    for name in ("relative", "coverage", "liquidity"):
        p = sub.add_parser(name)
        p.add_argument("--db", required=True)
        p.add_argument("--out", required=True)
    p = sub.add_parser("compare")
    p.add_argument("--old", required=True)
    p.add_argument("--new", required=True)
    p.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    handler = {
        "rebuild": cmd_rebuild,
        "vetoes": cmd_vetoes,
        "relative": cmd_relative,
        "coverage": cmd_coverage,
        "compare": cmd_compare,
        "liquidity": cmd_liquidity,
    }.get(args.cmd)
    if handler is None:
        raise SystemExit(f"{args.cmd}: not implemented yet")
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
