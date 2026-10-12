"""T-150 verification for the accounting-identity gate (CON-01..04, 07, 08, 19; DQ-01, L-10).

    uv run python scripts/verify_t150.py reproduce --db prod_copy.db
        Run the gate's pure identity core over T-147's stored-fact values with T-147's
        parameters (rounding fixed at USD 1,000,000, T-147's bases, CON-19's min over the two
        NCI signs done here) and compare the per-rule/per-level *sets of filings* with T-147's
        own measurement (:mod:`sec_xcheck.extra_measures`). Read-only.

    uv run python scripts/verify_t150.py measure --db after_t148.db [--before prod_copy.db]
        Run the gate's evaluation with the real parameters (C1-C9), without writing, and count
        per rule: tested / pass / warn / block / companies / YTD-column tests / base-missing
        caps (distinct filings). With *--before*, restrict both databases to the filings present
        in both (same accession) and report the level change between them.

Nothing here writes to a database.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sec_xcheck import extra_measures, records
from sec_xcheck.common import open_ro, statements_from_db

from fundamental_agent import db as fundamental_db
from fundamental_agent import quality
from fundamental_agent.pipeline import _TTM_ITEMS, _targets, _YearTask, _ytd_columns

G = "us-gaap_"
_BLOCK_STOP = 0.05  # F1b hard stop: a rule blocking more than 5% of the filings it tested
RULES = ("CON-01", "CON-02", "CON-03", "CON-04", "CON-07", "CON-08", "CON-19")
LEVELS = ("pass", "warn", "block")
_OCF = G + "NetCashProvidedByUsedInOperatingActivities"
_OCF_CONT = G + "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"
_OCF_DISC = G + "CashProvidedByUsedInOperatingActivitiesDiscontinuedOperations"
_ICF = G + "NetCashProvidedByUsedInInvestingActivities"
_FCF = G + "NetCashProvidedByUsedInFinancingActivities"
_NCI_INCOME = G + "NetIncomeLossAttributableToNoncontrollingInterest"
_T147_ROUNDING = 1_000_000.0


# -- T-147's parameters over MY core -----------------------------------------------------------


def _my_t147_level(rule: str, values: dict[str, float]) -> str | None:  # noqa: PLR0911
    """My core's level for *rule* with T-147's parameters: rounding USD 1M, T-147's bases, and
    (CON-19) the min over the two NCI signs, computed here, never in ``src``."""
    if rule == "CON-07":
        if any(c not in values for c in (_OCF, _ICF, _FCF)):
            return None
        result = quality.con07_result(values, "C", rounding=_T147_ROUNDING)
        return result.level if result is not None else None
    if rule == "CON-08":
        if _OCF not in values or _OCF_CONT not in values:
            return None
        result = quality.con08_result(values, "C", base_concept=_OCF, rounding=_T147_ROUNDING)
        return result.level if result is not None else None
    _title, lhs, rhs, base = extra_measures.IDENTITIES[rule]
    left = tuple(G + c for c in lhs)
    right = tuple(G + c for c in rhs)
    if rule == "CON-04" and G + "MinorityInterest" not in values:
        return None
    if (
        rule == "CON-08"
        and G + "CashProvidedByUsedInOperatingActivitiesDiscontinuedOperations" not in values
    ):
        right = right[:1]
    if any(c not in values for c in (*left, *right)):
        return None
    base_c = G + base
    vals = dict(values)
    if vals.get(base_c) is None:  # T-147: `values.get(G+base, sum(left))`
        vals[base_c] = sum(values[c] for c in left)
    if rule == "CON-19":  # T-147's min over the two NCI signs
        flipped = dict(vals)
        flipped[_NCI_INCOME] = -flipped[_NCI_INCOME]
        results = [
            r
            for r in (
                quality.identity_result(
                    rule, left, right, base_c, vals, "C", rounding=_T147_ROUNDING
                ),
                quality.identity_result(
                    rule, left, right, base_c, flipped, "C", rounding=_T147_ROUNDING
                ),
            )
            if r is not None
        ]
        return min(results, key=lambda r: r.diff).level if results else None
    result = quality.identity_result(rule, left, right, base_c, vals, "C", rounding=_T147_ROUNDING)
    return result.level if result is not None else None


def _my_merged_values(facts: quality.FilingFacts) -> dict[str, float]:
    """T-147's merged identity dict: balance concepts at the instant column, flow concepts at the
    filing's own column, each read from any of the three statements (T-147's ``any_statement``)."""
    merged: dict[str, float] = {}
    for concept in quality._BALANCE_CONCEPTS:
        value = facts.value(concept, facts.instant_column, any_statement=True)
        if value is not None:
            merged[concept] = value
    for concept in quality._FLOW_CONCEPTS:
        value = facts.value(concept, facts.flow_column, any_statement=True)
        if value is not None:
            merged[concept] = value
    return merged


def cmd_reproduce(args: argparse.Namespace) -> int:
    conn = open_ro(args.db)
    recs = [r for r in records.load(conn) if r.role == "target"]
    facts = quality.load_facts(conn, [r.filing_id for r in recs])

    # per rule: {level: {filing_id}} for T-147's measurement and for my core over my reader.
    ref: dict[str, dict[str, set[int]]] = {r: defaultdict(set) for r in RULES}
    mine: dict[str, dict[str, set[int]]] = {r: defaultdict(set) for r in RULES}
    value_diffs: list[dict[str, Any]] = []
    for rec in recs:
        t147_values = {k: float(v) for k, v in rec.f["identity"].items()}
        my_values = _my_merged_values(facts[rec.filing_id])
        for concept, value in t147_values.items():
            if my_values.get(concept) != value:
                value_diffs.append(
                    {
                        "ticker": rec.ticker,
                        "filing_id": rec.filing_id,
                        "concept": concept,
                        "t147": value,
                        "mine": my_values.get(concept),
                    }
                )
        for rule in RULES:
            ref_level = extra_measures.identity_result(t147_values, rule)
            my_level = _my_t147_level(rule, my_values)
            if ref_level is not None:
                ref[rule][ref_level].add(rec.filing_id)
            if my_level is not None:
                mine[rule][my_level].add(rec.filing_id)

    equal = all(ref[rule][level] == mine[rule][level] for rule in RULES for level in LEVELS)
    lines = [
        "T-150 Phase 2.2 reproduce: my core (T-147 parameters) vs T-147's measurement",
        f"db={args.db}  target filings={len(recs)}",
        "",
        f"{'rule':7} {'level':6} {'T-147':>7} {'mine':>7}  sets_equal",
    ]
    for rule in RULES:
        for level in LEVELS:
            r, m = len(ref[rule][level]), len(mine[rule][level])
            lines.append(
                f"{rule:7} {level:6} {r:>7} {m:>7}  {ref[rule][level] == mine[rule][level]}"
            )
    lines += ["", f"ALL SETS EQUAL: {equal}", f"reader value mismatches: {len(value_diffs)}"]
    text = "\n".join(lines)
    Path(args.out_txt).write_text(text + "\n", encoding="utf-8")
    Path(args.out_json).write_text(
        json.dumps(
            {
                "db": args.db,
                "target_filings": len(recs),
                "sets_equal": equal,
                "ref": {r: {lv: sorted(s) for lv, s in ref[r].items()} for r in RULES},
                "mine": {r: {lv: sorted(s) for lv, s in mine[r].items()} for r in RULES},
                "value_mismatches": value_diffs,
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    print(text)
    return 0 if equal else 1


# -- the gate's real counts (C1-C9), without writing -------------------------------------------


def _evaluate(conn: Any, filing_ids: set[int]) -> dict[int, list[Any]]:
    """Every CON identity result of each filing (CON-01..04/07/08/19 from the balance/flow columns
    and CON-06 proper from the sub-period sets). Each carries ``rule_id`` and ``level``."""
    facts = quality.load_facts(conn, filing_ids)
    subperiods = quality.load_subperiods(conn, filing_ids)
    out: dict[int, list[Any]] = {}
    for fid, ff in facts.items():
        balance = {c: ff.value(c, ff.instant_column) for c in quality._BALANCE_CONCEPTS}
        flow = {c: ff.value(c, ff.flow_column) for c in quality._FLOW_CONCEPTS}
        ytd = (
            {c: ff.value(c, ff.ytd_column) for c in quality._FLOW_CONCEPTS}
            if ff.ytd_column is not None
            else None
        )
        rows: list[Any] = list(
            quality.evaluate_identities(
                balance,
                flow,
                instant_column=ff.instant_column,
                flow_column=ff.flow_column,
                ytd=ytd,
                ytd_column=ff.ytd_column,
            )
        )
        sp = subperiods.get(fid)
        if sp is not None:
            rows.extend(quality.subperiod_results(sp)[0])
        out[fid] = rows
    return out


def _tickers(conn: Any) -> dict[int, str]:
    return {
        int(r[0]): str(r[1])
        for r in conn.execute(
            "SELECT f.id, a.ticker FROM sec_filings f JOIN assets a ON a.id = f.asset_id"
        )
    }


def _accession_ids(conn: Any) -> dict[str, int]:
    return {
        str(r["accession_number"]): int(r["id"])
        for r in conn.execute(
            "SELECT id, accession_number FROM sec_filings WHERE accession_number IS NOT NULL"
        )
    }


_LEVEL_ORDER = {"pass": 0, "warn": 1, "block": 2}


def _counts(results: dict[int, list[Any]], tickers: dict[int, str]) -> dict[str, dict[str, Any]]:
    """Per rule, the distinct filings (a filing with several CON-06 sets counts once, at its worst
    level) and the companies of its non-pass filings."""
    counts: dict[str, dict[str, Any]] = {
        rule: {
            "tested": 0,
            "pass": 0,
            "warn": 0,
            "block": 0,
            "companies": set(),
            "ytd": 0,
            "base_missing_caps": 0,
        }
        for rule in quality.CON_RULE_IDS
    }
    for fid, rows in results.items():
        worst, ytd_rules, base_missing_rules = _per_filing_rules(rows)
        for rule, level in worst.items():
            c = counts[rule]
            c["tested"] += 1
            c[level] += 1
            if level != "pass":
                c["companies"].add(tickers.get(fid, "?"))
            if rule in ytd_rules:
                c["ytd"] += 1
            if rule in base_missing_rules:
                c["base_missing_caps"] += 1
    for c in counts.values():
        c["companies"] = sorted(c["companies"])
    return counts


def _per_filing_rules(rows: list[Any]) -> tuple[dict[str, str], set[str], set[str]]:
    """``(rule -> worst level, rules with a YTD test, rules with a base-missing cap)`` of one
    filing's results."""
    worst: dict[str, str] = {}
    ytd_rules: set[str] = set()
    base_missing_rules: set[str] = set()
    for result in rows:
        if (
            result.rule_id not in worst
            or _LEVEL_ORDER[result.level] > _LEVEL_ORDER[worst[result.rule_id]]
        ):
            worst[result.rule_id] = result.level
        if result.ytd:
            ytd_rules.add(result.rule_id)
        if result.base_missing and result.level != "pass":
            base_missing_rules.add(result.rule_id)
    return worst, ytd_rules, base_missing_rules


def _shared_filing_ids(conn: Any, before_conn: Any) -> tuple[set[int], set[int], dict[int, str]]:
    """``(after ids, before ids, after_id -> accession)`` for the accessions present in both."""
    after_map = _accession_ids(conn)
    before_map = _accession_ids(before_conn)
    shared = set(after_map) & set(before_map)
    return (
        {after_map[a] for a in shared},
        {before_map[a] for a in shared},
        {after_map[a]: a for a in shared},
    )


def _level_changes(
    after_results: dict[int, list[quality.IdentityResult]],
    before_results: dict[int, list[quality.IdentityResult]],
    after_by_acc: dict[int, str],
    tickers: dict[int, str],
) -> list[dict[str, Any]]:
    before_levels = {
        fid: {r.rule_id: r.level for r in rows} for fid, rows in before_results.items()
    }
    changes = []
    for fid, rows in after_results.items():
        old_levels = before_levels.get(fid, {})
        for result in rows:
            old = old_levels.get(result.rule_id)
            if old is not None and old != result.level:
                changes.append(
                    {
                        "accession": after_by_acc.get(fid),
                        "ticker": tickers.get(fid, "?"),
                        "rule": result.rule_id,
                        "old": old,
                        "new": result.level,
                        "column": result.column,
                    }
                )
    return changes


def cmd_measure(args: argparse.Namespace) -> int:
    conn = open_ro(args.db)
    before_conn = open_ro(args.before) if args.before else None
    if before_conn is not None:
        ids_after, ids_before, after_by_acc = _shared_filing_ids(conn, before_conn)
    else:
        ids_after = {int(r[0]) for r in conn.execute("SELECT id FROM sec_filings")}
        ids_before, after_by_acc = set(), {}
    after_results = _evaluate(conn, ids_after)
    counts = _counts(after_results, _tickers(conn))

    changes: list[dict[str, Any]] = []
    before_counts: dict[str, dict[str, Any]] | None = None
    if before_conn is not None:
        before_results = _evaluate(before_conn, ids_before)
        before_counts = _counts(before_results, _tickers(before_conn))
        changes = _level_changes(after_results, before_results, after_by_acc, _tickers(conn))

    header = (
        f"{'rule':7} {'tested':>7} {'pass':>7} {'warn':>7} {'block':>6} {'ytd':>5} "
        f"{'basemiss':>9} companies"
    )

    def _rows(source: dict[str, dict[str, Any]]) -> list[str]:
        out = []
        for rule in quality.CON_RULE_IDS:
            c = source[rule]
            out.append(
                f"{rule:7} {c['tested']:>7} {c['pass']:>7} {c['warn']:>7} {c['block']:>6} "
                f"{c['ytd']:>5} {c['base_missing_caps']:>9} {', '.join(c['companies'])}"
            )
        return out

    lines = [f"T-150 Phase 3.1/3.2: gate counts (real parameters C1-C9), db={args.db}"]
    if before_conn is not None:
        lines.append(f"restricted to filings present in both (same accession): {len(ids_after)}")
    lines += ["", f"AFTER ({args.db})", header, *_rows(counts)]
    if before_counts is not None:
        lines += ["", f"BEFORE ({args.before})", header, *_rows(before_counts)]
    text = "\n".join(lines)
    if args.before:
        lines += ["", f"level changes (before {args.before} -> after {args.db}): {len(changes)}"]
        for ch in sorted(changes, key=lambda x: (x["rule"], x["accession"]))[:50]:
            lines.append(
                f"  {ch['rule']} {ch['ticker']} {ch['accession']} {ch['old']} -> {ch['new']} {ch['column']}"
            )
        text = "\n".join(lines)
    Path(args.out_txt).write_text(text + "\n", encoding="utf-8")
    Path(args.out_json).write_text(
        json.dumps(
            {
                "db": args.db,
                "before": args.before,
                "counts": counts,
                "counts_before": before_counts,
                "changes": changes,
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    print(text)
    return 0


# -- CON-06 (F1b: proper; F1: the cross-check) ---------------------------------------------------


def _filing_meta(conn: Any) -> dict[int, tuple[str, str]]:
    return {
        int(r[0]): (str(r[1]), str(r[2]))
        for r in conn.execute(
            "SELECT f.id, a.ticker, f.fiscal_period FROM sec_filings f "
            "JOIN assets a ON a.id = f.asset_id"
        )
    }


def _round1_sets(
    subperiods: dict[int, Any],
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    """The round-1 query's two sets (F1b "redo your query"): *structural* = a chain of >= 2
    quarters into an aggregate without requiring completeness; *genuine* = that chain also sums to
    the aggregate. The structural-but-not-genuine filings are explained in the output."""
    structural: dict[int, dict[str, Any]] = {}
    genuine: dict[int, dict[str, Any]] = {}
    for fid, sp in subperiods.items():
        for (statement, concept), columns in sorted(sp.duration.items()):
            quarters: dict[str, str] = {}
            aggregates: dict[str, str] = {}
            for column in columns:
                parts = quality._column_parts(column)
                if parts is None or parts[1] == "INSTANT":
                    continue
                if parts[1] in ("Q1", "Q2", "Q3", "Q4"):
                    quarters.setdefault(parts[0], column)
                elif parts[1] in ("FY", "YTD"):
                    aggregates.setdefault(parts[0], column)
            for agg_date, agg_column in sorted(aggregates.items()):
                chain, _gap = quality._subperiod_chain(quarters, agg_date, 999)
                if len(chain) < quality._SUB_MIN_QUARTERS:
                    continue
                quarter_values = [columns[k] for k in chain]
                agg_value = columns[agg_column]
                diff = abs(agg_value - sum(quarter_values))
                rounds = diff <= quality.l10_rounding([*quarter_values, agg_value])
                detail = {
                    "statement": statement,
                    "concept": concept,
                    "agg": agg_column,
                    "quarters": chain,
                    "quarters_sum": sum(quarter_values),
                    "agg_value": agg_value,
                    "diff": diff,
                    "rounds": rounds,
                }
                structural.setdefault(fid, detail)
                if rounds:
                    genuine.setdefault(fid, detail)
    return structural, genuine


def _crosscheck_counts(conn: Any) -> tuple[int, dict[str, dict[str, int]]]:
    """Per TTM item: tested (an identity and four quarters were both computable), warn and block
    under DQ-01 (as :func:`quality.record_ttm_crosscheck` computes it)."""
    per_item: dict[str, dict[str, int]] = defaultdict(lambda: {"tested": 0, "warn": 0, "block": 0})
    filings = 0
    for f in conn.execute(
        "SELECT f.id, f.asset_id, f.fiscal_year, f.fiscal_period, a.ticker, a.cik "
        "FROM sec_filings f JOIN assets a ON a.id = f.asset_id WHERE f.form = '10-Q'"
    ):
        stmts = statements_from_db(conn, int(f["id"]))
        task = _YearTask(
            int(f["asset_id"]),
            f["ticker"],
            f["ticker"],
            "10-Q",
            int(f["fiscal_year"]),
            str(f["cik"] or ""),
        )
        target = next(
            (t for t in _targets(stmts, task) if t.fiscal_period == f["fiscal_period"]), None
        )
        if target is None:
            continue
        filings += 1
        current = {
            item: v for item in _TTM_ITEMS if (v := stmts.get(item, target.period.key)) is not None
        }
        cur_key, prior_key, prior_end = _ytd_columns(stmts, target)
        ytd = (
            {
                item: fundamental_db.YTDPair(
                    stmts.get(item, cur_key), stmts.get(item, prior_key), prior_end
                )
                for item in _TTM_ITEMS
            }
            if cur_key and prior_key
            else {}
        )
        if not current and not ytd:
            continue
        flows = fundamental_db.ttm_detail(
            conn, int(f["asset_id"]), period_end=target.period.date, current=current, ytd=ytd
        )
        ni = flows.get("net_income")
        for item, flow in flows.items():
            if flow.method != "ytd" or flow.alt is None:
                continue
            per_item[item]["tested"] += 1
            base = ni.value if item in quality._INCOME_FLOW_ITEMS and ni is not None else None
            rounding = (
                0.5 * quality.l10_unit([flow.value, flow.alt]) * quality._TTM_CROSSCHECK_TERMS
            )
            level = quality.dq01_level(abs(flow.value - flow.alt), rounding, base)
            if level != "pass":
                per_item[item][level] += 1
    return filings, per_item


def _crosscheck_lines(conn: Any) -> list[str]:
    filings, per_item = _crosscheck_counts(conn)
    lines = [
        "",
        f"TTM cross-check (F1; compares this filing's YTD identity with four quarters from earlier "
        f"filings): 10-Q filings with a target = {filings}",
        f"{'item':26} {'tested':>7} {'warn':>5} {'block':>6} {'rows written':>13} {'quarantine rows':>16}",
    ]
    total = {"tested": 0, "warn": 0, "block": 0}
    for item in sorted(per_item):
        c = per_item[item]
        lines.append(
            f"{item:26} {c['tested']:>7} {c['warn']:>5} {c['block']:>6} "
            f"{c['warn'] + c['block']:>13} {0:>16}"
        )
        for k in total:
            total[k] += c[k]
    lines.append(
        f"{'TOTAL':26} {total['tested']:>7} {total['warn']:>5} {total['block']:>6} "
        f"{total['warn'] + total['block']:>13} {0:>16}"
    )
    return lines


def cmd_con06(args: argparse.Namespace) -> int:
    conn = open_ro(args.db)
    meta = _filing_meta(conn)
    subperiods = quality.load_subperiods(conn, set(meta))

    order = {"pass": 0, "warn": 1, "block": 2}
    worst: dict[int, str] = {}  # filing -> its worst set level (a filing may hold several sets)
    incomplete: dict[int, list[dict[str, Any]]] = defaultdict(list)
    base_missing = {"filings": set(), "companies": set()}
    not_tested = {"type_not_in_schema": 0, "not_additive": 0}
    non_pass: list[dict[str, Any]] = []
    for fid, sp in subperiods.items():
        results, incomplete_sets, clean_not_tested = quality.subperiod_results(sp)
        for key in clean_not_tested:
            not_tested[key] += clean_not_tested[key]
        for inc in incomplete_sets:
            incomplete[fid].append(inc)
        for r in results:
            if fid not in worst or order[r.level] > order[worst[fid]]:
                worst[fid] = r.level
            if r.base_missing and r.level != "pass":
                base_missing["filings"].add(fid)
                base_missing["companies"].add(meta[fid][0])
            if r.level != "pass":
                non_pass.append(
                    {
                        "ticker": meta[fid][0],
                        "fiscal_period": meta[fid][1],
                        "statement": r.statement,
                        "concept": r.concept,
                        "agg": r.agg_column,
                        "quarters": list(r.quarter_columns),
                        "values": list(r.values),
                        "level": r.level,
                        "diff": r.diff,
                        "rounding": r.rounding,
                        "threshold": r.threshold,
                        "base": r.base,
                        "base_missing": r.base_missing,
                    }
                )
    structural, genuine = _round1_sets(subperiods)
    by_level = {
        level: {fid for fid, lv in worst.items() if lv == level}
        for level in ("pass", "warn", "block")
    }
    block_share = (len(by_level["block"]) / len(worst)) if worst else 0.0
    lines = [
        f"T-150 CON-06, db={args.db}",
        "",
        "CON-06 proper (DQC_0084, within one instance): a complete contiguous quarter set matching "
        "its aggregate column of the same filing (quarters == the aggregate's length, PER-08 gaps)",
        f"  tested (>= 1 complete set): {len(worst)} filings",
        f"  pass  {len(by_level['pass'])} filings "
        f"({len({meta[f][0] for f in by_level['pass']})} companies)",
        f"  warn  {len(by_level['warn'])} filings "
        f"({len({meta[f][0] for f in by_level['warn']})} companies)",
        f"  block {len(by_level['block'])} filings "
        f"({len({meta[f][0] for f in by_level['block']})} companies)"
        f"  = {block_share:.1%} of tested",
        f"  incomplete sets (a partial chain, not tested): {sum(len(v) for v in incomplete.values())} "
        f"sets across {len(incomplete)} filings",
        f"  not tested, type not in the 2026 schema (extension / pre-2026): "
        f"{not_tested['type_not_in_schema']} sets",
        f"  not tested, not an additive amount (shares / per share / other): "
        f"{not_tested['not_additive']} sets",
        f"  base-missing caps (WARN; the aggregate column has no base): "
        f"{len(base_missing['filings'])} filings ({len(base_missing['companies'])} companies)",
        "",
        f"every non-pass set ({len(non_pass)}):",
    ]
    for row in sorted(non_pass, key=lambda r: (r["ticker"], r["concept"], r["agg"])):
        lines.append(
            f"  {row['ticker']} {row['fiscal_period']} {row['statement']} {row['concept']} "
            f"{row['agg']} quarters={row['quarters']} values={row['values']} level={row['level']} "
            f"diff={row['diff']} rounding={row['rounding']} threshold={row['threshold']} "
            f"base={row['base']}"
        )
    lines += ["", "round-1 query re-explained (F1b 'redo your query'):"]
    lines.append(
        f"  structural (a chain of >= 2 quarters, completeness not required): {len(structural)} filings"
    )
    lines.append(f"  genuine (that chain also sums to the aggregate): {len(genuine)} filings")
    for fid in sorted(set(structural) - set(genuine)):
        detail = structural[fid]
        lines.append(
            f"    {meta[fid][0]} filing {fid}: {detail['statement']} {detail['concept']} "
            f"{detail['agg']} quarters={detail['quarters']} sum={detail['quarters_sum']} vs "
            f"{detail['agg_value']} (diff {detail['diff']}) -> incomplete: not tested"
        )
    lines += _crosscheck_lines(conn)
    text = "\n".join(lines)
    Path(args.out_txt).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 1 if block_share > _BLOCK_STOP else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    repro = sub.add_parser("reproduce", help="my core with T-147's parameters vs T-147")
    repro.add_argument("--db", required=True)
    repro.add_argument("--out-txt", default="/workspaces/thesis/scratch/t150/out/p2_reproduce.txt")
    repro.add_argument(
        "--out-json", default="/workspaces/thesis/scratch/t150/out/p2_reproduce.json"
    )
    repro.set_defaults(func=cmd_reproduce)
    meas = sub.add_parser("measure", help="the gate's real counts (C1-C9), without writing")
    meas.add_argument("--db", required=True)
    meas.add_argument(
        "--before", help="a second database: restrict to shared accessions, report changes"
    )
    meas.add_argument("--out-txt", default="/workspaces/thesis/scratch/t150/out/p3_counts.txt")
    meas.add_argument("--out-json", default="/workspaces/thesis/scratch/t150/out/p3_counts.json")
    meas.set_defaults(func=cmd_measure)
    con06 = sub.add_parser("con06", help="CON-06: the within-instance rule and the cross-check")
    con06.add_argument("--db", required=True)
    con06.add_argument(
        "--out-txt", default="/workspaces/thesis/scratch/t150/out/r1_con06_proper.txt"
    )
    con06.set_defaults(func=cmd_con06)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
