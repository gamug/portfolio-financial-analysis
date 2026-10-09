"""Is a re-run of today's resolver over the stored ``financial_facts`` faithful to what was stored?

    uv run python scripts/sec_xcheck/fidelity.py [--db PATH] [--examples N]

Level (b) of T-147 re-runs ``statements.py`` over stored rows. Before using it for any rule, this replays every
stored filing: rebuild ``Statements`` from the rows in their insertion order, run today's metric groups at the
filing's own period (the same ``compute`` functions the pipeline calls, without TTM), and compare each result's
``inputs`` with the ``inputs_json`` stored for the metric of the same name.

Classes of a (filing, metric, input) triple:

* ``MATCH``         -- equal to a part per million;
* ``STORED_ONLY``   -- the stored metric used a value, the replay resolves none;
* ``REPLAY_ONLY``   -- the replay resolves a value the stored metric did not have;
* ``VALUE_DIFFERS`` -- both resolve, different values;
* ``TTM_PATH``      -- an input only the TTM path produces (``*_ttm``, ``annualized_x4``): it needs the filing's
  earlier quarters, so it cannot be replayed from one filing's rows and is out of the rate;
* ``DRIFT:<task>``  -- a difference traced, by a structural detector (:func:`drift_for`), to a documented engine fix
  newer than the stored rows (``db.py``'s ``metrics-v3``..``v5`` history): T-102 (a utility's total operating
  revenue), T-117 (a revenue total its statement contradicts), T-133 (oil & gas / custom capex tiers), T-140 (a
  revenue total rebuilt from the statement), and T-128 (a revenue total that duplicates a dimensional slice,
  which storage cannot replay because it keeps no dimensional rows). Not a failure of the replay.

The match rate counts ``MATCH`` over the first three non-``MATCH`` classes together with ``MATCH`` (``REPLAY_ONLY``
is reported apart as the strict rate's extra denominator). The known structural gap: ``financial_facts`` stores no
dimensional or abstract rows, so T-128's check for a total that duplicates a dimensional slice
(``_duplicates_a_dimensional_slice``) cannot be replayed from storage. Read-only.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path
from typing import Any

from fundamental_agent.metrics import COMPUTERS
from fundamental_agent.statements import REGISTRY, Statements

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sec_xcheck.common import filings, match_value, open_ro, own_period_key, statements_from_db

CLASSES = ("MATCH", "STORED_ONLY", "REPLAY_ONLY", "VALUE_DIFFERS", "TTM_PATH")
TTM_INPUTS = {"annualized_x4"}


def is_ttm_input(name: str) -> bool:
    return name.endswith("_ttm") or name in TTM_INPUTS


def drift_for(stmts: Statements, item: str, column: str | None) -> str | None:
    """The documented engine fix (newer than the stored rows) that explains a difference of *item* at *column*."""
    if column is None:
        return None
    if item in ("revenue", "revenue_prior", "gross_profit"):
        return revenue_drift(stmts, column)
    if item in ("capital_expenditure", "free_cash_flow", "free_cash_flow_prior"):
        spec = REGISTRY["capital_expenditure"]
        used_fallback = (
            stmts._first_component_match(spec, column) is None
            and stmts.get("capital_expenditure", column) is not None
        )
        return "T-133" if used_fallback else None
    return None


def first_revenue_drift(stmts: Statements) -> str | None:
    """A revenue CAGR reads the filing's fiscal-year columns: any of them carrying a documented-drift revenue."""
    return next((t for p in stmts.fy_periods() if (t := revenue_drift(stmts, p.key))), None)


def revenue_drift(stmts: Statements, column: str) -> str | None:
    spec = REGISTRY["revenue"]
    total = stmts._first_total_match(spec, column)
    if total is None:
        rebuild = stmts.rebuild_total(spec, column)
        return "T-140" if rebuild is not None and rebuild.value is not None else None
    corrected, contradicted = stmts._label_total_correction(spec, column, total)
    if corrected is not None or contradicted:
        return "T-117"
    largest = stmts._largest_component_value(spec, column)
    if largest is not None and total < largest:
        return "T-128"  # a component above the total: the dimensional-twin test needs rows storage lacks
    utility = any(
        row.get("concept") == "us-gaap_RegulatedAndUnregulatedOperatingRevenue"
        for row in stmts._rows_for(spec)
    )
    return "T-102" if utility else None


def prior_key_of(stmts: Statements, key: str) -> str | None:
    period = next((p for p in stmts.periods if p.key == key), None)
    prior = stmts.prior_of(period) if period else None
    return prior.key if prior else None


def compare_inputs(stored: dict[str, Any], replay: dict[str, float]) -> dict[str, str]:
    """``{input: class}`` over the union of the stored and replayed input names."""
    out: dict[str, str] = {}
    for name in stored.keys() | replay.keys():
        s, r = stored.get(name), replay.get(name)
        if not isinstance(s, (int, float)) and s is not None:
            continue
        if s is None:
            out[name] = "REPLAY_ONLY"
        elif r is None:
            out[name] = "STORED_ONLY"
        else:
            out[name] = "MATCH" if match_value(r, float(s)) else "VALUE_DIFFERS"
    return out


def replay_metrics(stmts: Statements, key: str) -> dict[str, dict[str, float]]:
    """``{metric_name: inputs}`` over every computable group at *key* (valuation needs prices: not replayed)."""
    prior = prior_key_of(stmts, key)
    out: dict[str, dict[str, float]] = {}
    for compute in COMPUTERS.values():
        for result in compute(stmts, key, prior, None):
            out[result.name] = dict(result.inputs)
    return out


def refine(kind: str, stmts: Statements, key: str, metric: str, name: str) -> str:
    """A non-``MATCH`` class refined to ``TTM_PATH`` or ``DRIFT:<task>`` when a detector explains it."""
    if kind == "MATCH":
        return kind
    if is_ttm_input(name):
        return "TTM_PATH"
    column = prior_key_of(stmts, key) if name.endswith("_prior") else key
    task = drift_for(stmts, name, column)
    if task is None and metric == "revenue_cagr" and name in ("begin", "end"):
        task = first_revenue_drift(stmts)
    return f"DRIFT:{task}" if task else kind


def unexplained(kind: str) -> bool:
    return kind not in {"MATCH", "TTM_PATH"} and not kind.startswith("DRIFT:")


def run(
    conn: Any,
) -> tuple[collections.Counter[str], dict[str, collections.Counter[str]], list[tuple[Any, ...]]]:
    total: collections.Counter[str] = collections.Counter()
    by_input: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    examples: list[tuple[Any, ...]] = []
    for f in filings(conn):
        rows = conn.execute(
            "SELECT metric_name, inputs_json FROM fundamental_metrics WHERE filing_id = ? AND inputs_json IS NOT NULL",
            (f["id"],),
        ).fetchall()
        stmts = statements_from_db(conn, f["id"])
        key = own_period_key(stmts, f["form"], f["period_end"])
        if not rows or key is None:
            total["filings_skipped"] += 1
            continue
        total["filings_compared"] += 1
        replay = replay_metrics(stmts, key)
        bad = False
        for r in rows:
            if r["metric_name"] not in replay:
                continue  # valuation metrics need prices
            stored = json.loads(r["inputs_json"])
            for name, kind in compare_inputs(stored, replay[r["metric_name"]]).items():
                if not is_ttm_input(name):
                    total[f"raw:{kind}"] += 1  # before any drift class is assigned
                kind = refine(kind, stmts, key, r["metric_name"], name)  # noqa: PLW2901
                total[kind] += 1
                by_input[name][kind] += 1
                if unexplained(kind):
                    bad = True
                    examples.append(
                        (
                            kind,
                            f["ticker"],
                            f["form"],
                            f["period_end"],
                            r["metric_name"],
                            name,
                            stored.get(name),
                            replay[r["metric_name"]].get(name),
                        )
                    )
        total["filings_all_match"] += 0 if bad else 1
    return total, by_input, examples


def rates(counts: collections.Counter[str]) -> tuple[float, float]:
    """``(stored-side rate, strict rate)``: MATCH over MATCH+STORED_ONLY+VALUE_DIFFERS, and with REPLAY_ONLY too."""
    base = counts["MATCH"] + counts["STORED_ONLY"] + counts["VALUE_DIFFERS"]
    nan = float("nan")
    return (
        counts["MATCH"] / base if base else nan,
        counts["MATCH"] / (base + counts["REPLAY_ONLY"]) if base else nan,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db")
    parser.add_argument("--examples", type=int, default=15)
    args = parser.parse_args(argv)
    total, by_input, examples = run(open_ro(args.db))
    print(dict(total))
    stored_side, strict = rates(total)
    print(
        f"match rate (stored side): {stored_side:.4%}   strict (REPLAY_ONLY counted): {strict:.4%}"
    )
    for name, counts in sorted(by_input.items()):
        side, strict_rate = rates(counts)
        print(
            f"  {name:<26} {side:8.2%} {strict_rate:8.2%}  { {k: counts[k] for k in CLASSES if counts[k]} }"
        )
    for e in examples[: args.examples]:
        print("   ", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())


def summary(conn: Any) -> dict[str, Any]:
    """The numbers the audit reports for one database: raw and classified rates, the drift classes, the engine versions."""
    total, _by_input, examples = run(conn)
    raw = {k.removeprefix("raw:"): v for k, v in total.items() if k.startswith("raw:")}
    raw_base = raw.get("MATCH", 0) + raw.get("STORED_ONLY", 0) + raw.get("VALUE_DIFFERS", 0)
    stored_side, strict = rates(total)
    versions = [
        r[0]
        for r in conn.execute("SELECT DISTINCT engine_version FROM fundamental_metrics ORDER BY 1")
    ]
    return {
        "engine_versions": versions,
        "filings_compared": total["filings_compared"],
        "filings_all_match": total["filings_all_match"],
        "filings_without_metrics": total["filings_skipped"],
        "raw_counts": raw,
        "raw_match_rate": raw.get("MATCH", 0) / raw_base if raw_base else None,
        "classified_rate_stored_side": stored_side,
        "classified_rate_strict": strict,
        "drift": {k: v for k, v in total.items() if k.startswith("DRIFT:")},
        "ttm_path_pairs": total["TTM_PATH"],
        "unexplained": len(examples),
    }
