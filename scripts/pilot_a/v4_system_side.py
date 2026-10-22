#!/usr/bin/env python3
"""V4, the system side: the cycle's own rule evaluation on the pilot database (T-143 part A).

    .venv/bin/python scripts/pilot_a/v4_system_side.py \
        --db /workspaces/thesis/pilot_a2/financial.db \
        --dates /workspaces/thesis/handoff/pilot/v4/dates.txt \
        --out /workspaces/thesis/handoff/pilot/v4/v4_system.csv \
        --universe-db /workspaces/thesis/data/universe_history_gics.db

The other side of V4 is an **independent session** that re-implements the rules from their texts
(``T143a_V4_independent_rules.md``); this script is the system side: it calls the cycle's own
readers and the cycle's own rules, so the two files can be compared row by row.

It is the evaluation the ``cycle`` performs in its veto step (``orchestrator.py``: the
``RuleContext`` built there, the same readers, ``cycle.rules.builtin.RULES``), with two differences
that the brief names:

* every rule in ``RULES`` is evaluated (the cycle would evaluate only the *enabled* ones, and the
  pilot database holds no rule catalog);
* the temporal holds are **not** applied: a rule's condition is reported on each date, the hold
  itself is the cycle's bookkeeping, not the rule's condition.

Read-only: the database is opened with ``connect_ro`` and nothing is written anywhere.

The output has exactly the columns the brief names::

    date, rule_id, asset_id, ticker, evaluated, hit, severity, inputs_json

one row per (date, rule, asset) of the universe at that date: ``evaluated=1`` when the rule could
resolve the asset's inputs (the rule's own ``RuleResult.evaluated``), ``hit=1`` when the rule
fired on it.  A row the rule could not evaluate carries the reason in ``inputs_json``.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cycle import data
from cycle.config import CycleSettings
from cycle.rules.base import RuleContext
from cycle.rules.builtin import RULES
from kg_schema import versions as kg_versions
from kg_schema.db import connect_ro

COLUMNS = (
    "date",
    "rule_id",
    "asset_id",
    "ticker",
    "evaluated",
    "hit",
    "severity",
    "inputs_json",
)


def _context(
    conn: Any, date: str, universe_db: str
) -> tuple[RuleContext, list[int], dict[int, str]]:
    """The cycle's veto-step context for one date, built exactly as the orchestrator builds it."""
    versions = kg_versions.resolve_metric_versions(conn)
    dq = data.data_quality(conn, date, versions)
    metrics = dq.apply(data.latest_metrics(conn, date, versions))
    # The cycle's own settings, without instantiating them (they need a database path; the
    # defaults are the two the veto step reads).
    engine_version = CycleSettings.model_fields["observation_engine_version"].default
    universe = CycleSettings.model_fields["universe"].default
    price_obs = data.latest_price_observation(conn, date, engine_version)
    rows = data.active_universe(conn, universe, date, universe_db)
    asset_ids = [int(r["id"]) for r in rows]
    ticker_of = {int(r["id"]): str(r["ticker"]) for r in rows}
    sector_name_of = {int(r["id"]): r["sector"] for r in rows}
    ctx = RuleContext(
        cycle_date=date,
        metrics={a: metrics.get(a, {}) for a in asset_ids},
        price_obs={a: price_obs[a] for a in asset_ids if a in price_obs},
        last_fundamental=data.last_fundamental_dates(conn, date),
        data_quality={a: dq.hard[a] for a in asset_ids if a in dq.hard},
        sectors={a: sector_name_of.get(a) for a in asset_ids},
        negative_equity={a: dq.negative_equity[a] for a in asset_ids if a in dq.negative_equity},
        profiles=data.asset_profiles(conn, list(asset_ids), date),
        utility_cash_coverage=data.utility_cash_coverage(conn, date),
    )
    return ctx, asset_ids, ticker_of


def _inputs_for(
    ctx: RuleContext, asset_id: int, evidence: dict[str, Any] | None, evaluated: bool
) -> dict[str, Any]:
    """What the row carries.

    A hit carries the rule's own evidence (the numbers the rule read).  An evaluated row that did
    not hit carries nothing: the file would grow to 120 MB with the whole metric dict on every
    row, and the inputs of a row the two sides disagree about are recomputed from the database by
    ``v4_compare.py`` (the same readers, the same date) when the disagreement is opened.  A row the
    rule could not evaluate carries the reason.
    """
    if evidence is not None:
        return evidence
    if not evaluated:
        return {"not_evaluated": "the rule could not resolve this asset's inputs"}
    return {}


def system_inputs(db: str, date: str, asset_id: int, universe_db: str) -> dict[str, Any]:
    """The system side's inputs for one (date, asset): the metrics, the price row, the negative
    equity evidence and the utility-cash-coverage rule input (what the rules can read).

    Used by ``v4_compare.py`` to open every disagreement with the inputs of both sides.
    """
    conn = connect_ro(db)
    ctx, asset_ids, _ticker = _context(conn, date, universe_db)
    if asset_id not in asset_ids:
        return {"not_in_universe": True}
    payload: dict[str, Any] = {"metrics": ctx.metrics.get(asset_id) or {}}
    obs = ctx.price_obs.get(asset_id)
    if obs:
        payload["price_obs"] = {
            k: v for k, v in obs.items() if k in ("close", "realized_vol_90d", "obs_date")
        }
    if asset_id in ctx.negative_equity:
        payload["negative_equity"] = ctx.negative_equity[asset_id]
    if asset_id in ctx.utility_cash_coverage:
        payload["utility_cash_coverage"] = ctx.utility_cash_coverage[asset_id]
    if asset_id in ctx.last_fundamental:
        payload["last_fundamental"] = ctx.last_fundamental[asset_id]
    if asset_id in ctx.data_quality:
        payload["data_quality"] = ctx.data_quality[asset_id]
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--dates", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--universe-db", default="/workspaces/thesis/data/universe_history_gics.db")
    args = parser.parse_args()

    dates = [
        line.strip()
        for line in Path(args.dates).read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    conn = connect_ro(args.db)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    counts = {"rows": 0, "hits": 0, "evaluated": 0}
    started = time.time()
    with out_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for date in dates:
            ctx, asset_ids, ticker_of = _context(conn, date, args.universe_db)
            for rule in RULES:
                result = rule.evaluate(ctx)
                evaluated = set(result.evaluated)
                hits = {h.asset_id: h for h in result.hits}
                for asset_id in asset_ids:
                    is_evaluated = asset_id in evaluated
                    hit = hits.get(asset_id)
                    writer.writerow(
                        [
                            date,
                            rule.RULE_ID,
                            asset_id,
                            ticker_of.get(asset_id, ""),
                            1 if is_evaluated else 0,
                            1 if hit is not None else 0,
                            # the hit's own severity (T-116's LEVERAGE_EXTREME review hit is SOFT,
                            # the rule's default is HARD); empty on a row that is not a hit --
                            # the independent side's convention.
                            "" if hit is None else hit.severity,
                            json.dumps(
                                _inputs_for(
                                    ctx, asset_id, hit.evidence if hit else None, is_evaluated
                                ),
                                sort_keys=True,
                                default=str,
                            ),
                        ]
                    )
                    counts["rows"] += 1
                    counts["evaluated"] += int(is_evaluated)
                    counts["hits"] += int(hit is not None)
            print(f"{date}: {len(asset_ids)} assets x {len(RULES)} rules", flush=True)
    print(
        f"wrote {out_path}: {counts['rows']} rows, {counts['evaluated']} evaluated, "
        f"{counts['hits']} hits, {round(time.time() - started, 1)} s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
