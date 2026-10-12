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
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sec_xcheck import extra_measures, records
from sec_xcheck.common import open_ro, statements_from_db

from fundamental_agent import db as fundamental_db
from fundamental_agent import fiscal as fa_fiscal
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
                nci_operand=quality.nci_operand(ff),
                skip_con19=quality.con19_sign_missing(ff),
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


# The identity rules' operands and their column kind (True = the instant column), for the change
# table's cause classification. CON_06 is left out: it is a new rule (F1b), so on the pre-F1b
# "before" code it was never evaluated.
_RULE_OPERANDS: dict[str, tuple[tuple[str, ...], bool]] = {
    "CON_01": ((quality._ASSETS, quality._LIAB_EQ), True),
    "CON_02": ((quality._ASSETS, quality._ASSETS_CURRENT, quality._ASSETS_NONCURRENT), True),
    "CON_03": (
        (quality._LIABILITIES, quality._LIABILITIES_CURRENT, quality._LIABILITIES_NONCURRENT),
        True,
    ),
    "CON_04": (
        (
            quality._EQUITY_INCL_NCI,
            quality._STOCKHOLDERS_EQUITY,
            quality._MINORITY_INTEREST,
        ),
        True,
    ),
    "CON_07": ((*quality._CHANGE_IN_CASH, quality._OCF, quality._ICF, quality._FCF), False),
    "CON_08": ((quality._OCF, quality._OCF_CONT, quality._OCF_DISC), False),
    "CON_19": ((quality._PROFIT_LOSS, quality._NET_INCOME, quality._NCI_INCOME), False),
}


def _displayed(
    conn: Any, filing_id: int, concept: str, column: str
) -> tuple[float, float | None] | None:
    """``(displayed value, preferred_sign or None)`` of the concept's own-statement row."""
    statement = quality._CONCEPT_STATEMENT[concept]
    sign_col = "preferred_sign" if quality._has_preferred_sign(conn) else "NULL AS preferred_sign"
    row = conn.execute(
        f"SELECT value, {sign_col} FROM financial_facts "  # noqa: S608 - fixed literal column
        "WHERE filing_id = ? AND statement = ? AND concept = ? AND period_key = ? "
        "ORDER BY id LIMIT 1",
        (filing_id, statement, concept, column),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0]), row[1]


def _operand_rows(
    conn: Any, filing_id: int, operands: tuple[str, ...], instant: bool
) -> dict[str, tuple[float, float | None] | None] | None:
    facts = quality.load_facts(conn, [filing_id]).get(filing_id)
    if facts is None:
        return None
    column = facts.instant_column if instant else facts.flow_column
    if column is None:
        return None
    return {c: _displayed(conn, filing_id, c, column) for c in operands}


def _cause(  # noqa: PLR0911 - one classification per cause
    conn: Any, before_conn: Any, afid: int, bfid: int, rule: str
) -> str:
    """F4's classification of one level change for *rule*: 'operand row absent after', 'operand row
    absent before', 'filed sign', 'value differs', or 'other' (a tolerance/base/rule change)."""
    if rule not in _RULE_OPERANDS:  # CON_06 is a new rule: no pre-F1b operand shape
        return "other"
    operands, instant = _RULE_OPERANDS[rule]
    before = _operand_rows(before_conn, bfid, operands, instant)
    after = _operand_rows(conn, afid, operands, instant)
    if before is None or after is None:
        return "other"
    if any(before.get(c) is not None and after.get(c) is None for c in operands):
        return "operand row absent after"
    if any(before.get(c) is None and after.get(c) is not None for c in operands):
        return "operand row absent before"
    if any(
        (b := before.get(c)) is not None
        and (a := after.get(c)) is not None
        and b[0] == a[0]
        and b[1] != a[1]
        for c in operands
    ):
        return "filed sign"
    if any(
        (b := before.get(c)) is not None and (a := after.get(c)) is not None and b[0] != a[0]
        for c in operands
    ):
        return "value differs"
    return "other"


def _changes_by_accession(  # noqa: PLR0913, PLR0917 - the two databases, their results and maps
    conn: Any,
    before_conn: Any,
    after_results: dict[int, list[Any]],
    before_results: dict[int, list[Any]],
    after_map: dict[str, int],
    before_map: dict[str, int],
    shared: list[str],
    *,
    causes: bool,
) -> list[dict[str, Any]]:
    """Every level change between the two databases, keyed by **accession** and covering
    tested↔untested in both directions."""
    before_levels = {fid: _per_filing_rules(rows)[0] for fid, rows in before_results.items()}
    after_levels = {fid: _per_filing_rules(rows)[0] for fid, rows in after_results.items()}
    tickers = _tickers(conn)
    out: list[dict[str, Any]] = []
    for accession in shared:
        afid, bfid = after_map[accession], before_map[accession]
        a_rules = after_levels.get(afid, {})
        b_rules = before_levels.get(bfid, {})
        for rule in quality.CON_RULE_IDS:
            old = b_rules.get(rule, "untested")
            new = a_rules.get(rule, "untested")
            if old == new:
                continue
            change: dict[str, Any] = {
                "accession": accession,
                "ticker": tickers.get(afid, "?"),
                "rule": rule,
                "old": old,
                "new": new,
            }
            if causes:
                change["cause"] = _cause(conn, before_conn, afid, bfid, rule)
            out.append(change)
    return out


def cmd_measure(args: argparse.Namespace) -> int:
    conn = open_ro(args.db)
    before_conn = open_ro(args.before) if args.before else None
    after_map = _accession_ids(conn)
    before_map = _accession_ids(before_conn) if before_conn is not None else {}
    if before_conn is not None:
        shared = sorted(set(after_map) & set(before_map))
        ids_after = {after_map[a] for a in shared}
        ids_before = {before_map[a] for a in shared}
    else:
        shared = []
        ids_after = set(after_map.values())
        ids_before = set()
    after_results = _evaluate(conn, ids_after)
    counts = _counts(after_results, _tickers(conn))

    changes: list[dict[str, Any]] = []
    before_counts: dict[str, dict[str, Any]] | None = None
    if before_conn is not None:
        before_results = _evaluate(before_conn, ids_before)
        before_counts = _counts(before_results, _tickers(before_conn))
        changes = _changes_by_accession(
            conn,
            before_conn,
            after_results,
            before_results,
            after_map,
            before_map,
            shared,
            causes=args.causes,
        )

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

    lines = [f"T-150 Phase 3.1: gate counts (real parameters C1-C9), db={args.db}"]
    if before_conn is not None:
        lines.append(f"restricted to filings present in both (same accession): {len(shared)}")
    lines += ["", f"AFTER ({args.db})", header, *_rows(counts)]
    if before_counts is not None:
        lines += ["", f"BEFORE ({args.before})", header, *_rows(before_counts)]
    Path(args.out_txt).write_text("\n".join(lines) + "\n", encoding="utf-8")
    Path(args.out_json).write_text(
        json.dumps(
            {
                "db": args.db,
                "before": args.before,
                "counts": counts,
                "counts_before": before_counts,
            },
            indent=1,
        ),
        encoding="utf-8",
    )

    change_lines = [
        f"T-150 Phase 3.2: level changes by accession, before {args.before} -> {args.db}",
        "",
    ]
    change_lines.append(f"changes: {len(changes)}")
    if args.causes:
        by_cause: dict[str, int] = {}
        for ch in changes:
            by_cause[ch["cause"]] = by_cause.get(ch["cause"], 0) + 1
        change_lines.append(f"by cause: {by_cause}")
    change_lines.append(f"{'rule':7} {'ticker':8} {'accession':24} {'old':9} {'new':9} cause")
    for ch in sorted(changes, key=lambda c: (c["rule"], c["accession"])):
        change_lines.append(
            f"{ch['rule']:7} {ch['ticker']:8} {ch['accession']:24} {ch['old']:9} {ch['new']:9} "
            f"{ch.get('cause', '')}"
        )
    Path(args.out_changes).write_text("\n".join(change_lines) + "\n", encoding="utf-8")
    Path(args.out_changes_json).write_text(
        json.dumps({"db": args.db, "before": args.before, "changes": changes}, indent=1),
        encoding="utf-8",
    )
    print("\n".join(lines))
    print(f"\n{len(changes)} level changes -> {args.out_changes}")
    return 0


def cmd_untested(args: argparse.Namespace) -> int:
    """F4: the ``-> untested`` transitions grouped by cause, plus the fact-drop investigation."""
    conn = open_ro(args.db)
    before_conn = open_ro(args.before)
    after_map = _accession_ids(conn)
    before_map = _accession_ids(before_conn)
    shared = sorted(set(after_map) & set(before_map))
    changes = _changes_by_accession(
        conn,
        before_conn,
        _evaluate(conn, {after_map[a] for a in shared}),
        _evaluate(before_conn, {before_map[a] for a in shared}),
        after_map,
        before_map,
        shared,
        causes=True,
    )
    untested = [c for c in changes if c["new"] == "untested"]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for ch in untested:
        groups.setdefault((ch["cause"], ch["old"]), []).append(ch)
    ids_before = json.dumps(sorted(before_map[a] for a in shared))
    ids_after = json.dumps(sorted(after_map[a] for a in shared))
    before_facts = int(
        before_conn.execute(
            "SELECT COUNT(*) FROM financial_facts WHERE filing_id IN (SELECT value FROM json_each(?))",
            (ids_before,),
        ).fetchone()[0]
    )
    after_facts = int(
        conn.execute(
            "SELECT COUNT(*) FROM financial_facts WHERE filing_id IN (SELECT value FROM json_each(?))",
            (ids_after,),
        ).fetchone()[0]
    )
    concept = "us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"
    lost_filings = lost_rows = duration_rows = instant_rows = 0
    example: tuple[str, str, str, float] | None = None
    for accession in shared:
        before_keys = {
            str(r[0])
            for r in before_conn.execute(
                "SELECT period_key FROM financial_facts "
                "WHERE filing_id=? AND statement='cash_flow' AND concept=?",
                (before_map[accession], concept),
            )
        }
        after_keys = {
            str(r[0])
            for r in conn.execute(
                "SELECT period_key FROM financial_facts "
                "WHERE filing_id=? AND statement='cash_flow' AND concept=?",
                (after_map[accession], concept),
            )
        }
        lost = before_keys - after_keys
        if not lost:
            continue
        lost_filings += 1
        lost_rows += len(lost)
        ticker = _tickers(before_conn).get(before_map[accession], "?")
        for key in sorted(lost):
            if "(" in key:
                duration_rows += 1
            else:
                instant_rows += 1
            if example is None:
                value = before_conn.execute(
                    "SELECT value FROM financial_facts WHERE filing_id=? AND statement='cash_flow' "
                    "AND concept=? AND period_key=? ORDER BY id LIMIT 1",
                    (before_map[accession], concept, key),
                ).fetchone()
                example = (ticker, accession, key, float(value[0]) if value else float("nan"))

    lines = [
        f"T-150 F4 untested transitions and the fact drop, before {args.before} -> {args.db}",
        "",
        f"shared accessions: {len(shared)}; total changes: {len(changes)}; -> untested: {len(untested)}",
        f"stored facts: before {before_facts}, after {after_facts} (drop {before_facts - after_facts})",
        "",
        f"lost cash_flow {concept}: {lost_filings} filings, {lost_rows} rows "
        f"({duration_rows} on a duration column like (FY)/(Qn)/(YTD), {instant_rows} on an instant)",
    ]
    if example is not None:
        lines.append(f"  example {example[0]} {example[1]} {example[2]} = {example[3]:,.1f}")
    lines += [
        "",
        "-> untested grouped by (cause, old level):",
    ]
    for (cause, old), rows in sorted(groups.items()):
        companies = sorted({r["ticker"] for r in rows})
        lines.append(
            f"  {cause} | {old} -> untested: {len(rows)} filings, {len(companies)} companies "
            f"({', '.join(companies)})"
        )
        example = rows[0]
        lines.append(
            f"      example {example['ticker']} {example['accession']} ({example['rule']})"
        )
    text = "\n".join(lines)
    Path(args.out_txt).write_text(text + "\n", encoding="utf-8")
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


def _con19_counts(conn: Any) -> dict[str, int]:
    """CON-19 on one database: tested (pass/warn/block), operand not filed, and "filed sign not
    stored" (F2) -- the last two are not tested."""
    ids = {int(r[0]) for r in conn.execute("SELECT id FROM sec_filings")}
    facts = quality.load_facts(conn, ids)
    out = {
        "filings": len(ids),
        "with_identity_facts": len(facts),
        "tested": 0,
        "pass": 0,
        "warn": 0,
        "block": 0,
        "not_tested_operand_not_filed": 0,
        "not_tested_sign_not_stored": 0,
        "not_tested_nci_tree_incomplete": 0,
    }
    for ff in facts.values():
        operand = quality.nci_operand(ff)
        if operand.incomplete:
            out["not_tested_nci_tree_incomplete"] += 1
            continue
        if quality.con19_sign_missing(ff):
            out["not_tested_sign_not_stored"] += 1
            continue
        flow = {c: ff.value(c, ff.flow_column) for c in quality._FLOW_CONCEPTS}
        result = quality.con19_result(flow, ff.flow_column, operand)
        if result is None:
            out["not_tested_operand_not_filed"] += 1
        else:
            out["tested"] += 1
            out[result.level] += 1
    return out


def _old_con19_level(ff: quality.FilingFacts) -> str | None:
    """CON-19 as it stood before F3: the single generic NCI concept, no filed sign -> not tested."""
    column = ff.flow_column
    if column is None:
        return None
    if ff.value(quality._NCI_INCOME, column) is not None and not ff.has_sign(
        quality._NCI_INCOME, column
    ):
        return None
    flow = {c: ff.value(c, column) for c in quality._FLOW_CONCEPTS}
    result = quality.identity_result(
        "CON_19",
        (quality._PROFIT_LOSS,),
        (quality._NET_INCOME, quality._NCI_INCOME),
        quality._NET_INCOME,
        flow,
        column,
    )
    return result.level if result is not None else None


def _new_con19(ff: quality.FilingFacts) -> quality.IdentityResult | None:
    """CON-19 under F2 + F3 (the tree operand, no filed sign -> not tested)."""
    if quality.con19_sign_missing(ff):
        return None
    flow = {c: ff.value(c, ff.flow_column) for c in quality._FLOW_CONCEPTS}
    return quality.con19_result(flow, ff.flow_column, quality.nci_operand(ff))


def cmd_con19fasb(args: argparse.Namespace) -> int:
    conn = open_ro(args.db)
    meta = _filing_meta(conn)
    facts = quality.load_facts(conn, set(meta))
    old_blocks = new_blocks = 0
    changes: list[tuple[str, str, str, str]] = []
    regressions: list[str] = []
    incomplete_filings: set[int] = set()
    incomplete_companies: set[str] = set()
    for fid, ff in facts.items():
        old = _old_con19_level(ff)
        operand = quality.nci_operand(ff)
        if operand.incomplete:
            incomplete_filings.add(fid)
            incomplete_companies.add(meta.get(fid, ("?", "?"))[0])
        new_result = None if operand.incomplete else _new_con19(ff)
        new = new_result.level if new_result is not None else None
        old_blocks += 1 if old == "block" else 0
        new_blocks += 1 if new == "block" else 0
        if old == new:
            continue
        concepts = ",".join(new_result.concepts) if new_result is not None else "-"
        ticker, fiscal_period = meta.get(fid, ("?", "?"))
        changes.append((fid, ticker, fiscal_period, f"{old} -> {new} [{concepts}]"))
        if old == "pass" and new in ("warn", "block"):
            regressions.append(f"{ticker} {fiscal_period} {old} -> {new}")
    lines = [
        f"T-150 F3b CON-19 per the FASB NCI tree, db={args.db}",
        "",
        f"CON-19 blocks: before (single generic NCI) {old_blocks}, after (tree) {new_blocks}",
        f"changed filings: {len(changes)}",
        f"not tested, NCI tree incomplete (F3b): {len(incomplete_filings)} filings "
        f"({len(incomplete_companies)} companies: {', '.join(sorted(incomplete_companies))})",
        "",
        f"regressions pass -> warn/block: {len(regressions)}",
        *[f"  {r}" for r in regressions[:20]],
        "",
        "every changed filing (id, ticker, period, old -> new [concepts summed]):",
    ]
    for fid, ticker, fiscal_period, change in sorted(changes, key=lambda c: (c[1], c[2])):
        lines.append(f"  {ticker} {fiscal_period} filing {fid}: {change}")
    text = "\n".join(lines)
    Path(args.out_txt).write_text(text + "\n", encoding="utf-8")
    print("\n".join(lines[:12]))
    hard_stop = bool(regressions) or new_blocks > old_blocks
    print(f"\nhard stop (regression or block count rose): {hard_stop}")
    return 1 if hard_stop else 0


def cmd_con19(args: argparse.Namespace) -> int:
    conn = open_ro(args.db)
    counts = _con19_counts(conn)
    lines = [f"T-150 F2 CON-19 filed-sign, db={args.db}", "", _con19_line(counts)]
    if args.other:
        other = _con19_counts(open_ro(args.other))
        lines += ["", f"db={args.other}", _con19_line(other)]
    text = "\n".join(lines)
    Path(args.out_txt).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


def _con19_line(counts: dict[str, int]) -> str:
    return (
        f"  filings {counts['filings']}, with identity facts {counts['with_identity_facts']}; "
        f"tested {counts['tested']} (pass {counts['pass']}, warn {counts['warn']}, "
        f"block {counts['block']}); not tested: operand not filed "
        f"{counts['not_tested_operand_not_filed']}, filed sign not stored "
        f"{counts['not_tested_sign_not_stored']}, NCI tree incomplete "
        f"{counts['not_tested_nci_tree_incomplete']}"
    )


_EXACT_EPSILON = 1e-9


def _is_multiple(value: float, unit: float) -> bool:
    return value == 0.0 or abs(value / unit - round(value / unit)) < _EXACT_EPSILON


def _rule_terms(ff: quality.FilingFacts, rule: str) -> list[float] | None:
    spec = _RULE_OPERANDS.get(rule)
    if spec is None:
        return None
    operands, instant = spec
    column = ff.instant_column if instant else ff.flow_column
    if column is None:
        return None
    values = [ff.value(c, column) for c in operands]
    if any(v is None for v in values):
        return None
    return [float(v) for v in values]


def _limits_a(conn: Any, results: dict[int, list[Any]]) -> list[str]:
    """(a) hits whose every term is a multiple of USD 100,000 and diff within 0.5 x 100,000 x
    n_terms (tenths-of-millions filers)."""
    facts = quality.load_facts(conn, set(results))
    groups: dict[tuple[str, str], set[int]] = {}
    for fid, rows in results.items():
        ff = facts.get(fid)
        if ff is None:
            continue
        for r in rows:
            if r.level == "pass" or r.rule_id not in _RULE_OPERANDS:
                continue
            terms = _rule_terms(ff, r.rule_id)
            if terms is None or not all(_is_multiple(t, 100_000.0) for t in terms):
                continue
            if r.diff <= 0.5 * 100_000.0 * r.n_terms:
                groups.setdefault((r.rule_id, r.level), set()).add(fid)
    return [
        f"  (a) tenths-of-millions rounding: {rule} {level}: {len(fids)} filings, "
        f"{len({_tickers(conn).get(f, '?') for f in fids})} companies"
        for (rule, level), fids in sorted(groups.items())
    ]


def _raw_value(
    conn: Any, filing_id: int, concept: str, column: str, statement: str
) -> float | None:
    """A concept's stored value, read directly (it may not be an identity concept)."""
    row = conn.execute(
        "SELECT value FROM financial_facts WHERE filing_id=? AND statement=? AND concept=? "
        "AND period_key=? ORDER BY id LIMIT 1",
        (filing_id, statement, concept, column),
    ).fetchone()
    return None if row is None or row[0] is None else float(row[0])


def _limits_b(conn: Any, results: dict[int, list[Any]]) -> list[str]:
    """(b) CON-08 hits where the all-activities discontinued operating cash flow, not the
    checklist's operand, is filed and continuing + it = total."""
    discontinued = "us-gaap_NetCashProvidedByUsedInDiscontinuedOperations"
    facts = quality.load_facts(conn, set(results))
    filings = set()
    for fid, rows in results.items():
        if not any(r.rule_id == "CON_08" and r.level != "pass" for r in rows):
            continue
        ff = facts.get(fid)
        if ff is None or ff.flow_column is None:
            continue
        d = _raw_value(conn, fid, discontinued, ff.flow_column, "cash_flow")
        total = ff.value(quality._OCF, ff.flow_column)
        cont = ff.value(quality._OCF_CONT, ff.flow_column)
        if d is None or total is None or cont is None:
            continue
        if abs(total - (cont + d)) <= quality.l10_rounding([total, cont, d]):
            filings.add(fid)
    tickers = _tickers(conn)
    return [
        f"  (b) CON-08 with {discontinued} filed and continuing + it = total: "
        f"{len(filings)} filings, {len({tickers.get(f, '?') for f in filings})} companies"
    ]


def _limits_c(conn: Any, results: dict[int, list[Any]]) -> list[str]:
    """(c) CON-19 hits whose diff equals a filed DividendsPreferredStock, or a filed extension
    income-statement line naming a noncontrolling interest (IVZ; VMRK)."""
    facts = quality.load_facts(conn, set(results))
    pref = set()
    ext = set()
    for fid, rows in results.items():
        hits = [r for r in rows if r.rule_id == "CON_19" and r.level != "pass"]
        if not hits:
            continue
        ff = facts.get(fid)
        if ff is None or ff.flow_column is None:
            continue
        column = ff.flow_column
        dividends = _raw_value(
            conn, fid, "us-gaap_DividendsPreferredStock", column, "income_statement"
        )
        diff = hits[0].diff
        if dividends is not None and abs(diff - abs(dividends)) <= 1.0:
            pref.add(fid)
            continue
        for row in conn.execute(
            "SELECT concept, label, value FROM financial_facts WHERE filing_id=? "
            "AND statement='income_statement' AND period_key=? ORDER BY id",
            (fid, column),
        ):
            concept = str(row["concept"])
            if concept.startswith("us-gaap_") or row["value"] is None:
                continue
            text = f"{concept} {row['label'] or ''}"
            if ("NoncontrollingInterest" in text or "Noncontrolling" in text) and abs(
                diff - abs(float(row["value"]))
            ) <= 1.0:
                ext.add(fid)
                break
    tickers = _tickers(conn)
    companies = lambda s: len({tickers.get(f, "?") for f in s})  # noqa: E731
    return [
        f"  (c) CON-19 hit diff = a filed DividendsPreferredStock (IVZ): {len(pref)} filings, "
        f"{companies(pref)} companies",
        f"  (c) CON-19 hit diff = a filed extension NCI line (VMRK); matched by concept name and "
        f"label: {len(ext)} filings, {companies(ext)} companies",
    ]


def _label_mismatches(conn: Any) -> tuple[set[int], dict[int, str]]:
    """F5: filings whose ``fiscal_period`` label disagrees with their ``period_end`` under the
    company's fiscal-year end (its 10-K period ends), PER-08's month-end rounding."""
    meta = _filing_meta(conn)
    tenk: dict[int, list[str]] = {}
    assets = {int(r[0]): int(r[1]) for r in conn.execute("SELECT id, asset_id FROM sec_filings")}
    for row in conn.execute(
        "SELECT asset_id, period_end FROM sec_filings WHERE form='10-K' AND period_end IS NOT NULL"
    ):
        tenk.setdefault(int(row["asset_id"]), []).append(str(row["period_end"]))
    for ends in tenk.values():
        ends.sort()
    out: dict[int, str] = {}
    for fid, (_ticker, fiscal_period) in meta.items():
        row = conn.execute("SELECT period_end FROM sec_filings WHERE id=?", (fid,)).fetchone()
        index = _label_index(fiscal_period)
        ends = tenk.get(assets.get(fid, 0))
        if row is None or row[0] is None or not ends or index is None:
            continue
        period_end = str(row[0])
        end = date.fromisoformat(period_end)
        # the company's fiscal-year end of the year the filing belongs to: the nearest stored 10-K
        # period end on/after the filing's own period end (the filing's own, for a 10-K).
        later = [e for e in ends if date.fromisoformat(e) >= end]
        if not later:
            continue
        fy_end = date.fromisoformat(later[0])
        start = fa_fiscal.months_before(fy_end, 12)
        months = (end.year - start.year) * 12 + (end.month - start.month)
        stored_index = round(months / 3)
        if stored_index != index:
            out[fid] = (
                f"{fiscal_period} label (quarter {index}) but period_end {period_end} is quarter "
                f"{stored_index} of the fiscal year ending {fy_end}"
            )
    return set(out), out


def _label_index(fiscal_period: str) -> int | None:
    """The fiscal quarter a label names: ``2024Q3`` -> 3, ``FY2024`` -> 4."""
    if fiscal_period[-2:] in ("Q1", "Q2", "Q3", "Q4"):
        return int(fiscal_period[-1])
    if fiscal_period.startswith("FY"):
        return 4
    return None


def cmd_limits(args: argparse.Namespace) -> int:
    conn = open_ro(args.db)
    results = _evaluate(conn, {int(r[0]) for r in conn.execute("SELECT id FROM sec_filings")})
    lines = [f"T-150 F5 limitations, db={args.db} (full set)", ""]
    lines += _limits_a(conn, results)
    lines += _limits_b(conn, results)
    lines += _limits_c(conn, results)
    Path(args.out_txt).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))

    mismatched, detail = _label_mismatches(conn)
    tickers = _tickers(conn)
    lm = [
        f"T-150 F5 label mismatch (report only), db={args.db}",
        "",
        f"filings whose fiscal_period disagrees with period_end under the company's fiscal-year end: "
        f"{len(mismatched)} filings, {len({tickers.get(f, '?') for f in mismatched})} companies",
        f"companies: {', '.join(sorted({tickers.get(f, '?') for f in mismatched}))}",
    ]
    for fid in sorted(mismatched, key=lambda f: tickers.get(f, "?")):
        lm.append(f"  {tickers.get(fid, '?')} filing {fid}: {detail[fid]}")
    Path(args.out_label).write_text("\n".join(lm) + "\n", encoding="utf-8")
    print("\n".join(lm[:6]))
    return 0


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
    meas.add_argument("--causes", action="store_true", help="classify every change (F4)")
    meas.add_argument("--out-txt", default="/workspaces/thesis/scratch/t150/out/r1_counts.txt")
    meas.add_argument("--out-json", default="/workspaces/thesis/scratch/t150/out/r1_counts.json")
    meas.add_argument("--out-changes", default="/workspaces/thesis/scratch/t150/out/r1_changes.txt")
    meas.add_argument(
        "--out-changes-json", default="/workspaces/thesis/scratch/t150/out/r1_changes.json"
    )
    meas.set_defaults(func=cmd_measure)
    unt = sub.add_parser("untested", help="the -> untested transitions by cause (F4)")
    unt.add_argument("--db", required=True)
    unt.add_argument("--before", required=True)
    unt.add_argument(
        "--out-txt", default="/workspaces/thesis/scratch/t150/out/r1_untested_causes.txt"
    )
    unt.set_defaults(func=cmd_untested)
    lim = sub.add_parser("limits", help="F5 limitations and the label-mismatch report")
    lim.add_argument("--db", required=True)
    lim.add_argument("--out-txt", default="/workspaces/thesis/scratch/t150/out/r1_limits.txt")
    lim.add_argument(
        "--out-label", default="/workspaces/thesis/scratch/t150/out/r1_label_mismatch.txt"
    )
    lim.set_defaults(func=cmd_limits)
    con06 = sub.add_parser("con06", help="CON-06: the within-instance rule and the cross-check")
    con06.add_argument("--db", required=True)
    con06.add_argument(
        "--out-txt", default="/workspaces/thesis/scratch/t150/out/r1_con06_proper.txt"
    )
    con06.set_defaults(func=cmd_con06)
    con19 = sub.add_parser("con19", help="CON-19's filed-sign measure (F2)")
    con19.add_argument("--db", required=True)
    con19.add_argument("--other", help="a second database to report beside --db")
    con19.add_argument("--out-txt", default="/workspaces/thesis/scratch/t150/out/r1_con19_sign.txt")
    con19.set_defaults(func=cmd_con19)
    con19fasb = sub.add_parser("con19fasb", help="CON-19 per the FASB NCI tree (F3)")
    con19fasb.add_argument("--db", required=True)
    con19fasb.add_argument(
        "--out-txt", default="/workspaces/thesis/scratch/t150/out/r1_con19_fasb.txt"
    )
    con19fasb.set_defaults(func=cmd_con19fasb)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
