"""T-151 part 1 verification: the sourced D&A and debt sums, the net capex element, the label rule.

Every number this script prints comes from a database it reads read-only (``--db`` / ``--control`` /
``--after``). It writes nothing but its ``--out-*`` files under the task's scratch ``out/``.

Subcommands
-----------
``changes``    control (master's ``metrics-v6`` rows) vs after (the branch's ``metrics-v7`` rows) by
               filing id: the D&A, debt and capex inputs and the derived metrics that moved.
``validation`` the sourced sums against the filers' own totals: every filing and column where a
               total and its C1/C2 children are both filed, and how many agree within L-10.
``refusals``   the filings where C1 or C2 refused the sum, grouped by the outside concept.
``capex``      every filing that used the net-element tier before, and where it resolves now.
``labels``     the new C4 labelling rule on the stored metadata: old function vs new vs stored.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sec_xcheck.common import filings, open_ro, statements_from_db

from fundamental_agent import db, fiscal, quality
from fundamental_agent.statements import (
    DA_SOURCED_CHILDREN,
    LONG_TERM_DEBT_CHILDREN,
    LONG_TERM_DEBT_NONCURRENT_CHILDREN,
    REGISTRY,
    G,
    Statements,
)

_EPSILON = 1e-9

# item -> (metric_group, metric_name, input key) whose stored inputs carry the item's value
ITEM_INPUTS = {
    "depreciation_amortization": ("leverage", "debt_to_assets", "depreciation_amortization"),
    "total_debt": ("leverage", "debt_to_assets", "total_debt"),
    "long_term_debt": ("roic", "return_on_invested_capital", "total_debt"),
    "capital_expenditure": ("cashflow", "capex_intensity", "capital_expenditure"),
    "ebitda": ("leverage", "net_debt_to_ebitda", "ebitda_ttm"),
}
# the derived metrics 3.1 reports
MOVED_METRICS = (
    "net_debt_to_ebitda",
    "enterprise_value",
    "enterprise_fcf_yield",
    "return_on_invested_capital",
    "free_cash_flow_margin",
    "free_cash_flow_conversion",
    "free_cash_flow_yield",
    "sbc_adjusted_fcf_yield",
    "capex_intensity",
    "debt_to_assets",
    "debt_to_equity",
    "interest_coverage",
)
_ITEM_FLAGS = (
    "flag_depreciation_amortization_da_sourced_sum",
    "flag_depreciation_amortization_da_sum_refused_other_lines",
    "flag_depreciation_amortization_depreciation_only_not_da",
    "flag_total_debt_debt_sourced_sum",
    "flag_total_debt_debt_sum_refused_other_lines",
    "flag_total_debt_includes_current_maturities",
    "flag_capital_expenditure_net_element_net_proceeds",
    "flag_capital_expenditure_net_element_last_resort",
    "flag_capital_expenditure_caption_fallback",
)


def _tickers(conn: Any) -> dict[int, str]:
    """``filing_id -> ticker`` (the identifiers every report here is keyed on)."""
    return {
        int(r[0]): str(r[1])
        for r in conn.execute(
            "SELECT f.id, a.ticker FROM sec_filings f JOIN assets a ON a.id = f.asset_id"
        )
    }


def _statements_or_none(conn: Any, filing_id: int) -> Statements | None:
    """The stored facts of one filing as ``Statements``, or ``None`` when they cannot be rebuilt."""
    try:
        return cast("Statements", statements_from_db(conn, filing_id))
    except Exception:  # a filing whose rows cannot be rebuilt is skipped, never fatal here
        return None


def _load_metrics(conn: Any, version: str) -> dict[int, dict[str, Any]]:
    """``filing_id -> {"values": {metric_name: value}, "inputs": {key: value}}`` for *version*."""
    out: dict[int, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT filing_id, metric_name, value, inputs_json FROM fundamental_metrics "
        "WHERE engine_version = ?",
        (version,),
    ):
        entry = out.setdefault(int(row["filing_id"]), {"values": {}, "inputs": {}})
        entry["values"][str(row["metric_name"])] = row["value"]
        if row["inputs_json"]:
            for key, value in json.loads(row["inputs_json"]).items():
                entry["inputs"].setdefault(key, value)
    return out


def _transition(before: float | None, after: float | None) -> str:
    if before is None and after is not None:
        return "filled"
    if before is not None and after is None:
        return "emptied"
    if before is not None and after is not None and abs(before - after) > _EPSILON:
        return "changed"
    return "same"


def _own_column(stmts: Statements, form: str, period_end: str) -> str | None:
    for p in stmts.periods:
        if p.date != period_end:
            continue
        if (form.startswith("10-K") and p.is_fy) or (form.startswith("10-Q") and p.is_quarter):
            return str(p.key)
    return None


def _balance_column(stmts: Statements, period_end: str) -> str | None:
    return next((p.key for p in stmts.periods if p.is_instant and p.date == period_end), None)


# -- 3.1 changes ---------------------------------------------------------------------------------


def _item_transitions(
    shared: list[int], control: dict[int, Any], after: dict[int, Any], key: str
) -> dict[str, list[int]]:
    buckets: dict[str, list[int]] = defaultdict(list)
    for fid in shared:
        buckets[_transition(control[fid]["inputs"].get(key), after[fid]["inputs"].get(key))].append(
            fid
        )
    return buckets


def _item_lines(
    item: str, key: str, buckets: dict[str, list[int]], state: dict[str, Any]
) -> list[str]:
    control, after, tickers = state["control"], state["after"], state["tickers"]
    lines = [f"  {item}: " + ", ".join(f"{k} {len(v)}" for k, v in sorted(buckets.items()))]
    for label in ("filled", "emptied", "changed"):
        fids = buckets.get(label, [])
        lines.append(
            f"    {label}: {len(fids)} filings, {len({tickers.get(f, '?') for f in fids})} companies"
        )
        if label in ("emptied", "changed"):
            for fid in fids[:10]:
                lines.append(
                    f"      {tickers.get(fid, '?')} filing {fid}: "
                    f"{control[fid]['inputs'].get(key)} -> {after[fid]['inputs'].get(key)}"
                )
    return lines


def cmd_changes(args: argparse.Namespace) -> int:
    control_conn, after_conn = open_ro(args.control), open_ro(args.after)
    tickers = _tickers(after_conn)
    control = _load_metrics(control_conn, args.control_version)
    after = _load_metrics(after_conn, args.after_version)
    shared = sorted(set(control) & set(after))
    lines = [
        f"T-151 part 1, 3.1 changes: control {args.control} ({args.control_version}) -> "
        f"after {args.after} ({args.after_version})",
        f"shared filings: {len(shared)}; control-only {len(set(control) - set(after))}; "
        f"after-only {len(set(after) - set(control))}",
        "",
    ]
    payload: dict[str, Any] = {"shared_filings": len(shared), "items": {}, "metrics": {}}
    state = {"control": control, "after": after, "tickers": tickers}
    for item, (_group, _metric, key) in ITEM_INPUTS.items():
        buckets = _item_transitions(shared, control, after, key)
        lines += _item_lines(item, key, buckets, state)
        payload["items"][item] = {k: len(v) for k, v in buckets.items()}
        payload["items"][item]["changed_filings"] = [
            {
                "filing_id": f,
                "ticker": tickers.get(f, "?"),
                "before": control[f]["inputs"].get(key),
                "after": after[f]["inputs"].get(key),
            }
            for f in buckets.get("changed", [])
        ]
    lines += ["", "  flags raised after and not before (item inputs):"]
    flag_counts: Counter[str] = Counter()
    for fid in shared:
        for flag in _ITEM_FLAGS:
            if after[fid]["inputs"].get(flag) and not control[fid]["inputs"].get(flag):
                flag_counts[flag] += 1
    lines += [f"    {flag}: {n} filings" for flag, n in sorted(flag_counts.items())]
    payload["flags_after_not_before"] = dict(flag_counts)
    lines += ["", "  derived metrics moved (value transitions):"]
    for name in MOVED_METRICS:
        buckets = defaultdict(list)
        for fid in shared:
            buckets[
                _transition(control[fid]["values"].get(name), after[fid]["values"].get(name))
            ].append(fid)
        lines.append(
            f"    {name}: " + ", ".join(f"{k} {len(v)}" for k, v in sorted(buckets.items()))
        )
        payload["metrics"][name] = {k: len(v) for k, v in buckets.items()}
        for label in ("emptied", "changed"):
            for fid in buckets.get(label, [])[:6]:
                lines.append(
                    f"      {label} {tickers.get(fid, '?')} filing {fid}: "
                    f"{control[fid]['values'].get(name)} -> {after[fid]['values'].get(name)}"
                )
    Path(args.out_txt).write_text("\n".join(lines) + "\n", encoding="utf-8")
    Path(args.out_json).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print("\n".join(lines[:40]))
    return 0


# -- 3.2 validation ------------------------------------------------------------------------------

_VALIDATION_TREES = {
    "da": (
        (
            G + "DepreciationDepletionAndAmortization",
            G + "DepreciationAndAmortization",
            G + "DepreciationAmortizationAndAccretionNet",
        ),
        DA_SOURCED_CHILDREN,
        "cash_flow",
    ),
    "cls": ((G + "LongTermDebtNoncurrent",), LONG_TERM_DEBT_NONCURRENT_CHILDREN, "balance_sheet"),
    "dbo": ((G + "LongTermDebt",), LONG_TERM_DEBT_CHILDREN, "balance_sheet"),
}


def _validation_cases(conn: Any) -> list[tuple[str, str, str, float, float, str]]:
    rows: list[tuple[str, str, str, float, float, str]] = []
    for f in filings(conn):
        stmts = _statements_or_none(conn, int(f["id"]))
        if stmts is None:
            continue
        for kind, (total_concepts, children, statement) in _VALIDATION_TREES.items():
            spec = REGISTRY[
                "depreciation_amortization" if statement == "cash_flow" else "long_term_debt"
            ]
            if statement == "balance_sheet":
                column = _balance_column(stmts, str(f["period_end"]))
            else:
                column = _own_column(stmts, str(f["form"]), str(f["period_end"]))
            if column is None:
                continue
            total = next(
                (
                    v
                    for c in total_concepts
                    if (v := stmts._concept_value(spec, column, c)) is not None
                ),
                None,
            )
            parts = {
                c: v for c in children if (v := stmts._concept_value(spec, column, c)) is not None
            }
            if total is None or not parts:
                continue
            rows.append(
                (
                    str(f["ticker"]),
                    str(f["accession_number"]),
                    kind,
                    total,
                    sum(parts.values()),
                    column,
                )
            )
    return rows


def cmd_validation(args: argparse.Namespace) -> int:
    """3.2: a filed total vs the sum of its filed children, per filing and column."""
    conn = open_ro(args.db)
    rows = _validation_cases(conn)
    lines = [
        f"T-151 part 1, 3.2 validation of the sourced sums against the filers' own totals, db={args.db}",
        f"cases where a total and >=1 of its children are filed on the same face: {len(rows)}",
        "",
    ]
    agree = 0
    worst: list[tuple[float, tuple[Any, ...], float]] = []
    for ticker, acc, kind, total, child_sum, column in rows:
        rounding = quality.l10_rounding([total, child_sum])
        diff = abs(total - child_sum)
        if diff <= rounding:
            agree += 1
        else:
            worst.append((diff, (ticker, acc, kind, total, child_sum, column), rounding))
    disagree = len(rows) - agree
    lines.append(
        f"agree within L-10's rounding: {agree} ({100.0 * agree / max(1, len(rows)):.2f}%)"
    )
    lines.append(
        f"disagree: {disagree} ({100.0 * disagree / max(1, len(rows)):.2f}%)   [hard stop 4 if > 2%]"
    )
    lines += ["", "largest differences:"]
    worst.sort(key=lambda x: -x[0])
    for diff, case, rounding in worst[:25]:
        ticker, acc, kind, total, child_sum, column = case
        lines.append(
            f"  {ticker} {acc} [{kind}] {column}: total {total:,.0f} vs children {child_sum:,.0f} "
            f"(diff {diff:,.0f}, rounding {rounding:,.0f})"
        )
    Path(args.out_txt).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


# -- 3.3 refusals --------------------------------------------------------------------------------


def _da_refusals(conn: Any) -> list[tuple[int, str, list[str]]]:
    spec = REGISTRY["depreciation_amortization"]
    out: list[tuple[int, str, list[str]]] = []
    for f in filings(conn):
        stmts = _statements_or_none(conn, int(f["id"]))
        if stmts is None:
            continue
        column = _own_column(stmts, str(f["form"]), str(f["period_end"]))
        if column is None:
            continue
        outside = stmts.da_lines_outside_sourced(spec)
        if outside and stmts._concept_value(spec, column, G + "Depreciation") is not None:
            out.append((int(f["id"]), str(f["ticker"]), outside))
    return out


def _debt_refusals(conn: Any) -> list[tuple[int, str, list[str]]]:
    spec = REGISTRY["long_term_debt"]
    out: list[tuple[int, str, list[str]]] = []
    for f in filings(conn):
        stmts = _statements_or_none(conn, int(f["id"]))
        if stmts is None:
            continue
        column = _balance_column(stmts, str(f["period_end"]))
        if column is None:
            continue
        for children in (LONG_TERM_DEBT_NONCURRENT_CHILDREN, LONG_TERM_DEBT_CHILDREN):
            filed = [c for c in children if stmts._concept_value(spec, column, c) is not None]
            if not filed:
                continue
            outside = stmts.debt_lines_outside(children)
            if outside:
                out.append((int(f["id"]), str(f["ticker"]), outside))
            break
    return out


def _refusal_lines(kind: str, entries: list[tuple[int, str, list[str]]]) -> list[str]:
    companies = sorted({e[1] for e in entries})
    lines = [f"{kind}: {len(entries)} filings, {len(companies)} companies"]
    by_concept: Counter[str] = Counter()
    for _fid, _ticker, outside in entries:
        for concept in outside:
            by_concept[concept] += 1
    lines += [f"    {n:5}  {concept}" for concept, n in by_concept.most_common(20)]
    seen: set[str] = set()
    for fid, ticker, outside in entries:
        if outside[0] in seen:
            continue
        seen.add(outside[0])
        lines.append(f"    e.g. {ticker} filing {fid}: {', '.join(outside)}")
    return [*lines, ""]


def cmd_refusals(args: argparse.Namespace) -> int:
    """3.3: the filings where C1 or C2 refused the sum, grouped by the outside concept."""
    conn = open_ro(args.db)
    lines = [f"T-151 part 1, 3.3 refusals of the sourced sums, db={args.db}", ""]
    lines += _refusal_lines("da", _da_refusals(conn))
    lines += _refusal_lines("debt", _debt_refusals(conn))
    Path(args.out_txt).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:60]))
    return 0


# -- 3.4 C3 --------------------------------------------------------------------------------------


def cmd_capex(args: argparse.Namespace) -> int:
    """3.4 C3: every filing that used the net-element tier before, and where it resolves now."""
    control_conn, after_conn = open_ro(args.control), open_ro(args.after)
    tickers = _tickers(after_conn)
    spec = REGISTRY["capital_expenditure"]
    net = G + "PaymentsForProceedsFromProductiveAssets"
    lines: list[str] = []
    for f in filings(control_conn):
        fid = int(f["id"])
        stmts = _statements_or_none(control_conn, fid)
        if stmts is None:
            continue
        column = _own_column(stmts, str(f["form"]), str(f["period_end"]))
        if column is None:
            continue
        net_value = stmts._concept_value(spec, column, net)
        res_before = stmts.resolve("capital_expenditure", column)
        if net_value is None or not (
            {"net_element_last_resort", "net_element_net_proceeds"} & set(res_before.flags)
        ):
            continue
        caption = next(
            (
                str(r.get("label") or "")
                for r in stmts.raw.get("cash_flow", [])
                if r.get("concept") == net
            ),
            "",
        )
        after_stmts = _statements_or_none(after_conn, fid)
        res_after = after_stmts.resolve("capital_expenditure", column) if after_stmts else None
        now = (
            f"{res_after.value:,.0f} ({res_after.concept}, {res_after.how})"
            if res_after is not None and res_after.value is not None
            else f"empty ({','.join(res_after.flags) if res_after else '?'})"
        )
        lines.append(
            f"  {tickers.get(fid, '?')} filing {fid} {f['form']} {f['period_end']}: "
            f"net element {net_value:,.0f} caption {caption!r} -> {now}"
        )
    head = [
        f"T-151 part 1, 3.4 C3: filings that read the net element before ({args.control}) and their "
        f"resolution after ({args.after})",
        f"filings that used the net-element tier before: {len(lines)}",
        "",
    ]
    out = head + lines
    Path(args.out_txt).write_text("\n".join(out) + "\n", encoding="utf-8")
    print("\n".join(out[:60]))
    return 0


# -- 3.4 C4 --------------------------------------------------------------------------------------


def _label_check(conn: Any, *, override: bool) -> dict[int, tuple[str | None, str | None]]:
    """``filing_id -> (label under the rule, label under the pre-C4 rule)`` for every stored 10-Q.

    *override* selects the C4 rule (the company's own year end wins) or the pre-C4 one (the payload
    balance-sheet instant wins)."""
    out: dict[int, tuple[str | None, str | None]] = {}
    for f in filings(conn):
        if not str(f["form"]).startswith("10-Q") or not f["period_end"]:
            continue
        stmts = _statements_or_none(conn, int(f["id"]))
        if stmts is None:
            continue
        end = date.fromisoformat(str(f["period_end"]))
        instants = [p.date for p in stmts.instant_periods()]
        known = db.latest_fiscal_year_end(conn, int(f["asset_id"]), str(f["period_end"]))
        known_d = date.fromisoformat(known) if known else None
        payload_fye = fiscal.payload_fiscal_year_end(instants, end) or known_d
        payload_label = fiscal.quarter_label(end, payload_fye) if payload_fye else None
        company_label = fiscal.quarter_label(end, known_d) if known_d else None
        label = payload_label
        if override and company_label is not None and company_label != payload_label:
            label = company_label
        out[int(f["id"])] = (label, payload_label)
    return out


def cmd_labels(args: argparse.Namespace) -> int:
    """3.4 C4: the new labelling rule on the stored metadata (labels are not recomputed)."""
    lines = [
        "T-151 part 1, 3.4 C4: the labelling rule on the stored metadata",
        "(labels are not recomputed by recompute; this is the function's result on the stored rows)",
        "",
    ]
    for path in args.db:
        conn = open_ro(path)
        tickers = _tickers(conn)
        meta = {
            int(r[0]): (str(r[1]), str(r[2]))
            for r in conn.execute("SELECT id, fiscal_period, period_end FROM sec_filings")
        }
        new = _label_check(conn, override=True)
        old = _label_check(conn, override=False)
        fix_changes = [fid for fid in new if new[fid][0] != old[fid][0]]
        vs_stored = [fid for fid in new if new[fid][0] is not None and new[fid][0] != meta[fid][0]]
        old_vs_stored = [
            fid for fid in old if old[fid][0] is not None and old[fid][0] != meta[fid][0]
        ]
        lines.append(f"### {path}: {len(new)} stored 10-Qs")
        lines.append(
            f"  the C4 rule's own effect (new function vs old function): {len(fix_changes)} filings"
        )
        for fid in sorted(fix_changes, key=lambda f: tickers.get(f, "?")):
            lines.append(
                f"    {tickers.get(fid, '?')} filing {fid} {meta[fid][1]}: stored {meta[fid][0]} "
                f"-> old rule {old[fid][0]} -> new rule {new[fid][0]}"
            )
        lines.append(f"  new function vs the stored label: {len(vs_stored)} filings")
        for fid in sorted(vs_stored, key=lambda f: tickers.get(f, "?"))[:30]:
            lines.append(
                f"    {tickers.get(fid, '?')} filing {fid} {meta[fid][1]}: stored {meta[fid][0]} "
                f"-> new {new[fid][0]}"
            )
        lines.append(
            f"  control (old function vs the stored label): {len(old_vs_stored)} filings -- the fix's "
            "own effect is the list above"
        )
        lines.append("")
    Path(args.out_txt).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    ch = sub.add_parser("changes", help="3.1 control vs after")
    ch.add_argument("--control", required=True)
    ch.add_argument("--after", required=True)
    ch.add_argument("--control-version", default="metrics-v6")
    ch.add_argument("--after-version", default="metrics-v7")
    ch.add_argument("--out-txt", default="/workspaces/thesis/scratch/t151a/out/p3_changes.txt")
    ch.add_argument("--out-json", default="/workspaces/thesis/scratch/t151a/out/p3_changes.json")
    ch.set_defaults(func=cmd_changes)
    va = sub.add_parser("validation", help="3.2 the sums against the filers' own totals")
    va.add_argument("--db", required=True)
    va.add_argument("--out-txt", default="/workspaces/thesis/scratch/t151a/out/p3_validation.txt")
    va.set_defaults(func=cmd_validation)
    re_ = sub.add_parser("refusals", help="3.3 the refused sums by outside concept")
    re_.add_argument("--db", required=True)
    re_.add_argument("--out-txt", default="/workspaces/thesis/scratch/t151a/out/p3_refusals.txt")
    re_.set_defaults(func=cmd_refusals)
    ca = sub.add_parser("capex", help="3.4 C3 the net-element filings")
    ca.add_argument("--control", required=True)
    ca.add_argument("--after", required=True)
    ca.add_argument("--out-txt", default="/workspaces/thesis/scratch/t151a/out/p3_capex_net.txt")
    ca.set_defaults(func=cmd_capex)
    la = sub.add_parser("labels", help="3.4 C4 the labelling rule on the stored metadata")
    la.add_argument("--db", nargs="+", required=True)
    la.add_argument("--out-txt", default="/workspaces/thesis/scratch/t151a/out/p3_labels.txt")
    la.set_defaults(func=cmd_labels)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
