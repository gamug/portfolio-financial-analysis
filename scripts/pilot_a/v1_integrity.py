#!/usr/bin/env python3
"""V1 (T-143 part A): the integrity checks ``verify_pilot.py`` does not make, on the pilot database.

    uv run python scripts/pilot_a/v1_integrity.py \
        --db /workspaces/thesis/pilot_a/financial.db \
        --universe-db /workspaces/thesis/data/universe_history_gics.db \
        --analysis-date 2026-10-18 \
        --manifest /workspaces/thesis/data/private/tiingo/leaver_prices.manifest.json \
        --out /workspaces/thesis/scratch/t143a/out/p3_v1_integrity.json

Read-only (``mode=ro``). The plan's V1 list, minus what ``verify_pilot.py`` already covers:

* **Point in time** -- no metric reads a filing whose ``available_at`` is after the date it is used
  for; the counts are given per quarter end.
* **Identity** -- one asset per CIK line; no filing stored under the wrong CIK (T-149).
* **CNP** -- the plan's prerequisite acceptance case: ``0001130310-22-000063`` stored as 2022Q1 and
  ``0001130310-22-000123`` as 2022Q3, with no ``FilingLabelCollision``.
* **Coverage** -- every line has its filings, or a recorded reason.
* **Observations** -- every member-session in the window has a ``priceobs-v2`` observation, except the
  declared permanent gaps (INFO/WRK, each Tiingo line's exit tail, VLTO's first two sessions).
* **The market-cap refusals** -- why a filing with a stored price has no market cap (K1: the prior
  filing's cover count is absent because it lies outside the window).

Counts, dates and names only; no values.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from kg_schema.queries import WINDOW_START, connect_ro, members_over_range
from kg_schema.trading_calendar import is_trading_day

#: The two permanent gaps the manifest declares (docs/model_fixes.md, T-153 part 3c).
DECLARED_NO_PRICE = ("INFO", "WRK")
#: VLTO's first two sessions are a declared gap too.
DECLARED_EARLY = {"VLTO": 2}
#: How many examples to keep per list.
EXAMPLES_LIMIT = 15


def _sessions(start: str, end: str) -> list[str]:
    day, last = date.fromisoformat(start), date.fromisoformat(end)
    out: list[str] = []
    while day <= last:
        if is_trading_day(day):
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, args))


def point_in_time(conn: sqlite3.Connection) -> dict[str, Any]:
    """No metric reads a filing that was not yet available on the date it is used for.

    Two rules, both measurable on the stored data:

    * a metric cannot be usable **before** its own filing was available (T-107's rule, per row);
    * the context a filing's ratios read -- its prior **10-K** (``prior_capital``) and, for a 10-Q,
      its prior-year **10-Q** (``prior_quarter_capital``) -- must have been available **no later than
      the filing that carries the metric**: a metric becomes usable with its own filing, so a
      context filing that arrives later is a look-ahead.

    A filing's *own* ``available_at`` is after its period end by construction (a filing is filed
    after the period it reports); that is the normal look-back, not a look-ahead.
    """
    early = _rows(
        conn,
        "SELECT COUNT(*) n FROM fundamental_metrics m JOIN sec_filings f ON f.id = m.filing_id "
        "WHERE m.available_at IS NULL OR m.available_at < f.available_at",
    )
    context = _rows(
        conn,
        "WITH ctx AS ("
        " SELECT f.id filing_id, f.asset_id, f.form, f.period_end, f.available_at,"
        "  (SELECT p.available_at FROM sec_filings p WHERE p.asset_id = f.asset_id"
        "    AND p.form = '10-K' AND p.period_end < f.period_end"
        "    ORDER BY p.period_end DESC LIMIT 1) prior_10k,"
        "  (SELECT p.available_at FROM sec_filings p WHERE p.asset_id = f.asset_id"
        "    AND p.form = '10-Q' AND p.period_end BETWEEN date(f.period_end, '-1 year', '-10 day')"
        "    AND date(f.period_end, '-1 year', '+10 day')"
        "    ORDER BY p.period_end DESC LIMIT 1) prior_10q"
        " FROM sec_filings f)"
        " SELECT c.*, a.ticker FROM ctx c JOIN assets a ON a.id = c.asset_id"
        " WHERE (c.prior_10k IS NOT NULL AND c.prior_10k > c.available_at)"
        "    OR (c.form = '10-Q' AND c.prior_10q IS NOT NULL AND c.prior_10q > c.available_at)"
        " ORDER BY c.period_end",
    )
    by_date: dict[str, int] = {}
    for row in context:
        by_date[str(row["period_end"])] = by_date.get(str(row["period_end"]), 0) + 1
    return {
        "metrics_usable_before_their_filing": int(early[0]["n"]) if early else 0,
        "filings_reading_a_context_filing_that_was_not_yet_available": len(context),
        "by_period_end": by_date,
        "examples": [
            {
                "ticker": r["ticker"],
                "form": r["form"],
                "period_end": r["period_end"],
                "filing_available_at": r["available_at"],
                "prior_10k_available_at": r["prior_10k"],
                "prior_10q_available_at": r["prior_10q"],
            }
            for r in context[:20]
        ],
    }


def identity(conn: sqlite3.Connection) -> dict[str, Any]:
    """One asset per CIK line; no filing stored under the wrong CIK.

    A filing's CIK may differ from its asset's when it was filed **before a recorded CIK
    succession** (the tracked ``cik_succession`` table: XOM, BG, BLK, SKYD) -- then the filing
    carries the predecessor's CIK and the line's asset the successor's. Anything else is a filing
    under the wrong CIK.
    """
    predecessors: dict[str, set[str]] = {}
    for row in _rows(conn, "SELECT successor_cik, predecessor_cik FROM cik_succession"):
        predecessors.setdefault(str(row["successor_cik"]), set()).add(str(row["predecessor_cik"]))
    mismatched = _rows(
        conn,
        "SELECT f.id, a.ticker, f.cik filing_cik, a.cik asset_cik, f.filing_date, f.form, "
        "f.fiscal_period FROM sec_filings f JOIN assets a ON a.id = f.asset_id "
        "WHERE f.cik IS NOT NULL AND a.cik IS NOT NULL AND f.cik <> a.cik ORDER BY a.ticker",
    )
    explained = []
    unexplained = []
    for row in mismatched:
        item = {
            "ticker": row["ticker"],
            "filing_cik": row["filing_cik"],
            "asset_cik": row["asset_cik"],
            "form": row["form"],
            "fiscal_period": row["fiscal_period"],
            "filing_date": row["filing_date"],
        }
        if str(row["filing_cik"]) in predecessors.get(str(row["asset_cik"]), set()):
            explained.append(item)
        else:
            unexplained.append(item)
    keyless = _rows(conn, "SELECT id, ticker FROM assets WHERE cik IS NULL OR TRIM(cik) = ''")
    multi = _rows(
        conn,
        "SELECT cik, COUNT(DISTINCT id) n, GROUP_CONCAT(ticker) tickers FROM assets "
        "WHERE cik IS NOT NULL GROUP BY cik HAVING n > 1",
    )
    canonical: dict[str, set[str]] = {}
    for row in _rows(conn, "SELECT cik, canonical_symbol FROM ticker_history"):
        canonical.setdefault(str(row["cik"]), set()).add(str(row["canonical_symbol"]))
    multi_detail = []
    for row in multi:
        syms = canonical.get(str(row["cik"]), set())
        multi_detail.append(
            {
                "cik": row["cik"],
                "tickers": row["tickers"],
                "canonical_symbols": sorted(syms),
                "one_line": len(syms) == 1,
            }
        )
    return {
        "filings_under_the_wrong_cik": len(unexplained),
        "filings_whose_cik_is_a_recorded_predecessor": len(explained),
        "assets_without_a_cik": len(keyless),
        "ciks_with_more_than_one_asset": len(multi_detail),
        "multi_asset_detail": multi_detail,
        "wrong_examples": unexplained[:20],
        "succession_examples": explained[:8],
    }


def cnp(conn: sqlite3.Connection) -> dict[str, Any]:
    """The plan's prerequisite acceptance case (T-151 part 1)."""
    want = {
        "0001130310-22-000063": "2022Q1",
        "0001130310-22-000123": "2022Q3",
    }
    got = {}
    for accession, expected in want.items():
        rows = _rows(
            conn,
            "SELECT f.id, f.form, f.fiscal_period, f.period_end, f.filing_date, f.available_at, "
            "a.ticker FROM sec_filings f JOIN assets a ON a.id = f.asset_id "
            "WHERE f.accession_number = ?",
            (accession,),
        )
        got[accession] = {
            "expected_period": expected,
            "stored": [
                {
                    "form": r["form"],
                    "fiscal_period": r["fiscal_period"],
                    "period_end": r["period_end"],
                    "filing_date": r["filing_date"],
                    "ticker": r["ticker"],
                }
                for r in rows
            ],
            "ok": any(r["fiscal_period"] == expected for r in rows),
        }
    collisions = _rows(
        conn,
        "SELECT d.id, d.rule_id, a.ticker, f.accession_number FROM data_quality_issue d "
        "JOIN sec_filings f ON f.id = d.filing_id JOIN assets a ON a.id = f.asset_id "
        "WHERE a.ticker = 'CNP' AND d.rule_id LIKE '%Collision%'",
    )
    label_rows = _rows(
        conn,
        "SELECT stage, message FROM analysis_run_error WHERE ticker = 'CNP' ORDER BY id",
    )
    return {
        "accessions": got,
        "filing_label_collisions": len(collisions),
        "cnp_error_rows": [{"stage": r["stage"], "message": r["message"]} for r in label_rows],
    }


def coverage(
    conn: sqlite3.Connection, universe_db: str, fin_db: str, analysis_date: str
) -> dict[str, Any]:
    """Every line has its filings, or a recorded reason."""
    with (
        contextlib.closing(connect_ro(universe_db)) as universe,
        contextlib.closing(connect_ro(fin_db)) as fin,
    ):
        window = members_over_range(universe, fin, WINDOW_START, analysis_date)
    lines = {w.member.symbol: w for w in window}
    counts = {
        str(r["ticker"]): int(r["n"])
        for r in _rows(
            conn,
            "SELECT a.ticker, COUNT(f.id) n FROM assets a LEFT JOIN sec_filings f "
            "ON f.asset_id = a.id GROUP BY a.ticker",
        )
    }
    reasons: dict[str, list[str]] = {}
    for row in _rows(
        conn,
        "SELECT DISTINCT ticker, fiscal_period, stage FROM analysis_run_error ORDER BY ticker",
    ):
        reasons.setdefault(str(row["ticker"]), []).append(
            f"{row['fiscal_period']} ({row['stage']})"
        )
    without = sorted(t for t in lines if counts.get(t, 0) == 0)
    with_reason = sorted(t for t in lines if reasons.get(t))
    return {
        "lines": len(lines),
        "lines_without_any_filing": without,
        "lines_with_a_recorded_reason": with_reason,
        "reasons_by_line": {t: reasons[t] for t in with_reason},
        "filings_total": sum(counts.get(t, 0) for t in lines),
    }


def observations(
    conn: sqlite3.Connection, universe_db: str, fin_db: str, analysis_date: str, manifest_path: str
) -> dict[str, Any]:
    """Every member-session in the window has a ``priceobs-v2`` observation, except the declared gaps."""
    with (
        contextlib.closing(connect_ro(universe_db)) as universe,
        contextlib.closing(connect_ro(fin_db)) as fin,
    ):
        window = members_over_range(universe, fin, WINDOW_START, analysis_date)
    sessions = _sessions(WINDOW_START, analysis_date)
    manifest = json.loads(Path(manifest_path).read_text())
    exit_tails: dict[str, set[str]] = {}
    for line in manifest["lines"]:
        for missing in line.get("missing_sessions") or []:
            exit_tails.setdefault(line["ask"], set()).add(missing)
    observed: dict[int, set[str]] = {}
    for asset in _rows(conn, "SELECT id FROM assets"):
        asset_id = int(asset["id"])
        observed[asset_id] = {
            str(row["obs_date"])
            for row in _rows(
                conn,
                "SELECT obs_date FROM price_observation WHERE asset_id = ? "
                "AND engine_version = 'priceobs-v2'",
                (asset_id,),
            )
        }
    ticker_to_asset = {
        str(r["ticker"]): int(r["id"]) for r in _rows(conn, "SELECT id, ticker FROM assets")
    }
    gaps: list[dict[str, Any]] = []
    for w in sorted(window, key=lambda w: w.member.symbol):
        ticker = w.member.symbol
        start = max(WINDOW_START, w.window_from)
        end = w.window_to or analysis_date
        expected = [s for s in sessions if start <= s <= end]
        got = observed.get(ticker_to_asset.get(ticker, -1), set())
        missing = sorted(set(expected) - got)
        declared = set(exit_tails.get(ticker, set()))
        early = DECLARED_EARLY.get(ticker, 0)
        if early:
            declared |= set(expected[:early])
        if ticker in DECLARED_NO_PRICE:
            declared |= set(missing)
        unexplained = [m for m in missing if m not in declared]
        if unexplained:
            gaps.append(
                {
                    "ticker": ticker,
                    "span": f"{start}..{end}",
                    "missing_sessions": len(unexplained),
                    "examples": unexplained[:5],
                    "declared_exceptions": len(declared),
                }
            )
    return {
        "lines_checked": len(window),
        "lines_with_unexplained_missing_sessions": len(gaps),
        "detail": gaps,
    }


def market_cap_refusals(conn: sqlite3.Connection) -> dict[str, Any]:
    """Why a filing with a stored price has no market cap (K1: the prior filing's cover count)."""
    rows = _rows(
        conn,
        "SELECT DISTINCT f.id, a.ticker, f.form, f.fiscal_period, f.period_end, f.filing_date, "
        "f.asset_id FROM sec_filings f JOIN assets a ON a.id = f.asset_id JOIN fundamental_metrics m "
        "ON m.filing_id = f.id WHERE m.metric_group = 'valuation' AND m.value IS NULL "
        "AND m.inputs_json LIKE '%market_cap_no_cover_shares%' ORDER BY a.ticker",
    )
    with_prior = 0
    without_prior = 0
    examples: list[dict[str, Any]] = []
    for row in rows:
        prior = _rows(
            conn,
            "SELECT f.id, f.period_end, (SELECT COUNT(*) FROM filing_cover_shares cs "
            "WHERE cs.filing_id = f.id) cover_rows FROM sec_filings f "
            "WHERE f.asset_id = ? AND f.period_end < ? ORDER BY f.period_end DESC LIMIT 1",
            (row["asset_id"], row["period_end"]),
        )
        has_prior = bool(prior) and int(prior[0]["cover_rows"]) > 0
        if has_prior:
            with_prior += 1
        else:
            without_prior += 1
            if len(examples) < EXAMPLES_LIMIT:
                examples.append(
                    {
                        "ticker": row["ticker"],
                        "period": row["fiscal_period"],
                        "period_end": row["period_end"],
                        "prior_filing_with_a_cover_count": False,
                    }
                )
    return {
        "filings_with_no_market_cap": len(rows),
        "whose_prior_filing_has_a_cover_count": with_prior,
        "whose_prior_filing_has_none (K1: the earlier filing is outside the window)": without_prior,
        "examples": examples,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--universe-db", required=True)
    parser.add_argument("--analysis-date", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    with contextlib.closing(connect_ro(args.db)) as conn:
        conn.row_factory = sqlite3.Row
        doc = {
            "db": args.db,
            "analysis_date": args.analysis_date,
            "point_in_time": point_in_time(conn),
            "identity": identity(conn),
            "cnp": cnp(conn),
            "coverage": coverage(conn, args.universe_db, args.db, args.analysis_date),
            "observations": observations(
                conn, args.universe_db, args.db, args.analysis_date, args.manifest
            ),
            "market_cap_refusals": market_cap_refusals(conn),
        }
    Path(args.out).write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n")
    print(json.dumps(doc, indent=1, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
