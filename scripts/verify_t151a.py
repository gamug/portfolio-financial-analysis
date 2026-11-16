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
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sec_xcheck.common import cache_dir, filings, open_ro, statements_from_db

from fundamental_agent import db, fiscal, quality
from fundamental_agent.statements import (
    DA_SOURCED_CHILDREN,
    LONG_TERM_DEBT_CHILDREN,
    LONG_TERM_DEBT_NONCURRENT_CHILDREN,
    REGISTRY,
    SHORT_TERM_DEBT_LAST,
    SHORT_TERM_DEBT_MAP,
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
        (G + "DepreciationDepletionAndAmortization",),
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
    """3.2 (superseded by 3.2', see the plan's "Director decision after hard stop 4"): a filed total
    on the face is not the decomposition of the lines beside it, so this measurement is report-only;
    3.2'' classifies its 72 cases."""
    conn = open_ro(args.db)
    rows = _validation_cases(conn)
    lines = [
        "T-151 part 1, 3.2 face validation (SUPERSEDED by 3.2' -- report only; see 3.2''):",
        f"db={args.db}",
        f"cases where a filed total and >=1 of its C1/C2 children are both on the same face: {len(rows)}",
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


def _reaches_net_element(stmts: Statements, spec: Any, column: str) -> bool:
    """Whether the net-element tier is reached in *column*: every earlier tier empty. C3's diff only
    touches the net tier, so the branch's code answers this for the pre-C3 rule too."""
    if stmts._capex_tier1(spec, column) is not None:
        return False
    if stmts._capex_ppe_and_og(spec, column) is not None:
        return False
    for concept in (
        G + "PaymentsToAcquireOilAndGasPropertyAndEquipment",
        G + "PaymentsToAcquireProductiveAssets",
        G + "PaymentsToAcquireOtherProductiveAssets",
    ):
        if stmts._concept_value(spec, column, concept) is not None:
            return False
    return True


def cmd_capex(args: argparse.Namespace) -> int:
    """3.4 C3: every filing that read the net-element tier under the pre-C3 rule, with its caption,
    the value it gave then and the tier it resolves to now."""
    conn = open_ro(args.after)
    spec = REGISTRY["capital_expenditure"]
    lines: list[str] = []
    for f in filings(conn):
        stmts = _statements_or_none(conn, int(f["id"]))
        if stmts is None:
            continue
        column = _own_column(stmts, str(f["form"]), str(f["period_end"]))
        if column is None:
            continue
        before_value, caption = stmts._net_element(spec, column)
        if before_value is None or not _reaches_net_element(stmts, spec, column):
            continue
        res_after = stmts.resolve("capital_expenditure", column)
        now = (
            f"{res_after.value:,.0f} ({res_after.concept}, {res_after.how})"
            if res_after.value is not None
            else f"empty ({','.join(res_after.flags)})"
        )
        lines.append(
            f"  {f['ticker']} filing {f['id']} {f['form']} {f['period_end']}: net element "
            f"{before_value:,.0f} caption {caption!r} -> {now}"
        )
    # the review of PR #144: a rejected net element must not be re-taken by the caption fallback
    retaken: list[str] = []
    for f in filings(conn):
        stmts = _statements_or_none(conn, int(f["id"]))
        if stmts is None:
            continue
        column = _own_column(stmts, str(f["form"]), str(f["period_end"]))
        if column is None:
            continue
        res = stmts.resolve("capital_expenditure", column)
        rejected = bool(
            {"net_element_net_proceeds", "net_element_captioned_proceeds"} & set(res.flags)
        )
        if not rejected:
            continue
        old_val, old_concept, _how = stmts._label_fallback_row(
            REGISTRY["capital_expenditure"], column
        )
        if old_concept != G + "PaymentsForProceedsFromProductiveAssets":
            continue
        now = f"{res.value:,.0f} ({res.concept}, {res.how})" if res.value is not None else "empty"
        retaken.append(
            f"  {f['ticker']} filing {f['id']} {f['form']} {f['period_end']}: the caption fallback "
            f"used to re-take {old_val:,.0f} ({old_concept}); now {now}"
        )
    head = [
        f"T-151 part 1, 3.4 C3: every filing that read the net-element tier under the pre-C3 rule "
        f"(the branch's code on {args.after}'s facts; C3 changes only the net tier), and where it "
        "resolves now",
        f"filings that used the net-element tier before: {len(lines)}",
        "",
        "the review of PR #144's fix -- a rejected net element the caption fallback used to re-take: "
        f"{len(retaken)} filings",
    ]
    out = head + retaken + [""] + lines
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


# -- 3.2' the filled sums against the filer's own total in SEC companyfacts -----------------------

# ID-01 (checklist C13 p.25): a company-specific (extension) concept is read only through a
# documented fallback, and the debt resolver has none, so such an element is **excluded** from the
# sum. The filer's own total then exceeds our sum by exactly that element: the difference is the
# declared exclusion, not a failure. Each entry is ``(ticker, element, why)``.
_DECLARED_EXCLUSIONS: tuple[tuple[str, str, str], ...] = (
    (
        "CNP",
        "cnp_TransitionAndSystemRestorationBondsLongTermDebtNoncurrent",
        "ID-01 (C13 p.25): a company extension is read only through a documented fallback, and "
        "the debt resolver has none, so the element is excluded from the long-term debt sum",
    ),
)


def _declared_exclusion(
    ticker: str, column: str, stmts: Any, diff: float, rounding: float
) -> str | None:
    """The declared exclusion that explains a difference, when there is one (ID-01, C13 p.25).

    The excluded element must be filed on the case's own face and be worth exactly the
    difference; then the difference is the exclusion the checklist declares, and the check lists
    it as such instead of as an unexplained (or material) mismatch.

    The element's name is compared without case: the filing writes it
    ``cnp_TransitionAndSystemRestorationBondsLongTermDebtNonCurrent`` and the checklist writes it
    ``...Noncurrent``.
    """
    if not ticker or stmts is None:
        return None
    spec = REGISTRY["long_term_debt"]
    filed: dict[str, str] = {}
    for row in stmts._rows_for(spec):
        name = str(row.get("concept") or "")
        if name:
            filed.setdefault(name.lower(), name)
    for holder, concept, why in _DECLARED_EXCLUSIONS:
        if holder != ticker:
            continue
        name = filed.get(concept.lower())
        if name is None:
            continue
        value = stmts._concept_value(spec, str(column), name)
        if value is None or abs(diff - abs(value)) > rounding:
            continue
        return f"the declared exclusion {concept} ({why})"
    return None


def _case_exclusion(case: dict[str, Any], diff: float, rounding: float) -> str | None:
    """The declared exclusion of a 3.2' case, if one explains its difference."""
    return _declared_exclusion(
        str(case.get("ticker") or ""),
        str(case.get("column") or ""),
        case.get("stmts"),
        diff,
        rounding,
    )


_COMPANYFACTS = cache_dir() / "companyfacts"
# the parent concept(s) of each filled map, in the order the resolver reads them
_CF_PARENTS = {
    "da": (
        G + "DepreciationDepletionAndAmortization",
        G + "DepreciationAmortizationAndAccretionNet",
        G + "DepreciationAndAmortization",
    ),
    "cls": (G + "LongTermDebtNoncurrent",),
    "dbo": (G + "LongTermDebt",),
}
# PER-08's length classes: a duration fact matches a column when its end rounds to the same month end
# and its length is in the class the column's tag names.
_LENGTH_CLASSES = {
    "FY": (fiscal.YEAR_DAYS,),
    "Q1": (fiscal.QUARTER_DAYS,),
    "Q2": (fiscal.QUARTER_DAYS,),
    "Q3": (fiscal.QUARTER_DAYS,),
    "YTD": (fiscal.NINE_MONTH_DAYS, fiscal.SIX_MONTH_DAYS, fiscal.QUARTER_DAYS),
}


def _companyfacts(cik: str | None) -> dict[str, Any] | None:
    if not cik:
        return None
    path = _COMPANYFACTS / f"CIK{int(cik):010d}.json"
    if not path.exists():
        return None
    return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))


def _cf_matches(entry: dict[str, Any], end: date, tag: str) -> bool:
    try:
        fact_end = date.fromisoformat(str(entry["end"]))
    except (KeyError, ValueError):
        return False
    if not fiscal.same_period_end(fact_end, end):
        return False
    if tag == "INSTANT" or "start" not in entry:
        return True
    try:
        start = date.fromisoformat(str(entry["start"]))
    except ValueError:
        return False
    length = (fact_end - start).days + 1
    return any(fiscal.within(length, bounds) for bounds in _LENGTH_CLASSES.get(tag, ()))


def _cf_total(
    facts: dict[str, Any] | None, concepts: tuple[str, ...], accession: str, end: date, tag: str
) -> tuple[float | None, str | None, str | None]:
    """The filer's own total for *concepts* with the same accession and period, from companyfacts.

    The same ``accn`` is what makes it *the same instance*; the period matches by PER-08's month-end
    rounding and the tag's length class. When several entries match (a 10-Q tags its quarter and its
    year-to-date fact on the same end), the longest is the year-to-date figure the column names."""
    gaap = ((facts or {}).get("facts") or {}).get("us-gaap") or {}
    for concept in concepts:
        # the companyfacts ``facts`` map is keyed by the bare concept name, not ``us-gaap_<name>``
        best = _cf_best_entry(gaap.get(concept.split("_", 1)[-1]), accession, end, tag)
        if best is not None:
            return best[1], concept, best[2]
    return None, None, None


def _cf_best_entry(
    node: dict[str, Any] | None, accession: str, end: date, tag: str
) -> tuple[int, float, str] | None:
    """The longest matching USD entry of one concept: ``(length_days, value, end)``."""
    best: tuple[int, float, str] | None = None
    for unit, entries in (node or {}).get("units", {}).items():
        if unit != "USD":
            continue
        for entry in entries:
            if str(entry.get("accn")) != accession or entry.get("val") is None:
                continue
            if not _cf_matches(entry, end, tag):
                continue
            length = 0
            if "start" in entry:
                try:
                    length = (end - date.fromisoformat(str(entry["start"]))).days
                except ValueError:
                    length = 0
            if best is None or length > best[0]:
                best = (length, float(entry["val"]), str(entry.get("end")))
    return best


def _filled_cases(conn: Any) -> list[dict[str, Any]]:
    """Every filing and column where C1 or C2 filled the value (the sourced flag was raised).

    The value is the **map's own output**: C1's D&A sum, C2's **long-term** sum (never the total
    debt, which adds the short-term terms -- T-151 part 1, director decision after 4')."""
    spec_da, spec_lt = REGISTRY["depreciation_amortization"], REGISTRY["long_term_debt"]
    cases: list[dict[str, Any]] = []
    for f in filings(conn):
        stmts = _statements_or_none(conn, int(f["id"]))
        if stmts is None:
            continue
        base: dict[str, Any] = {
            "ticker": str(f["ticker"]),
            "accession": str(f["accession_number"]),
            "cik": f["cik"],
            "filing_id": int(f["id"]),
            "form": str(f["form"]),
            "period_end": str(f["period_end"]),
            "stmts": stmts,
        }
        for p in stmts.periods:
            if p.is_instant:
                continue
            res = stmts.resolve("depreciation_amortization", p.key)
            if "da_sourced_sum" not in res.flags:
                continue
            children = {
                c: v
                for c in DA_SOURCED_CHILDREN
                if (v := stmts._concept_value(spec_da, p.key, c)) is not None
            }
            cases.append(
                {
                    **base,
                    "kind": "da",
                    "column": p.key,
                    "end": p.date,
                    "tag": p.tag,
                    "value": res.value,
                    "value_concept": res.concept,
                    "children": children,
                }
            )
        own = _own_column(stmts, str(f["form"]), str(f["period_end"]))
        column = _balance_column(stmts, str(f["period_end"]))
        if own is None or column is None:
            continue
        long_term = stmts.resolve("long_term_debt", column)
        if "debt_sourced_sum" not in long_term.flags:
            continue
        counted = set((long_term.concept or "").split(" + "))
        kind = "cls" if counted & set(LONG_TERM_DEBT_NONCURRENT_CHILDREN) else "dbo"
        children = {
            c: v
            for c in (
                LONG_TERM_DEBT_NONCURRENT_CHILDREN if kind == "cls" else LONG_TERM_DEBT_CHILDREN
            )
            if (v := stmts._concept_value(spec_lt, column, c)) is not None
        }
        cases.append(
            {
                **base,
                "kind": kind,
                "column": column,
                "end": str(f["period_end"]),
                "tag": "INSTANT",
                "value": long_term.value,
                "value_concept": long_term.concept,
                "debt_total_concept": stmts.total_debt(own).concept,
                "children": children,
            }
        )
    return cases


def _is_own_column(case: dict[str, Any]) -> bool:
    """Whether the case's column is one the filing's own metrics read: its own quarter / fiscal-year
    column, its own year-to-date column (a 10-Q's TTM input), or the debt instant."""
    stmts = case["stmts"]
    form, period_end = str(case["form"]), str(case["period_end"])
    if case["kind"] != "da":
        return bool(case["column"] == _balance_column(stmts, period_end))
    if case["column"] == _own_column(stmts, form, period_end):
        return True
    return bool(form.startswith("10-Q") and case["tag"] == "YTD" and str(case["end"]) == period_end)


def _filed_short_term(case: dict[str, Any]) -> dict[str, float]:
    """The short-term map concepts filed on the case's own face (MET-07)."""
    spec = REGISTRY["short_term_debt"]
    concepts = [c for _term, cs in SHORT_TERM_DEBT_MAP for c in cs] + [SHORT_TERM_DEBT_LAST]
    return {
        c: v
        for c in concepts
        if (v := case["stmts"]._concept_value(spec, str(case["column"]), c)) is not None
    }


def _agreement_rounding(case: dict[str, Any], total: float) -> float:
    """The agreement band (director decision after 4'): the filer's own total's rounding unit
    (L-10's rule applied to the total alone) plus L-10's rounding term of the children."""
    return 0.5 * quality.l10_unit([total]) + quality.l10_rounding(list(case["children"].values()))


def _case_class(case: dict[str, Any]) -> str:
    """The mismatch class of a material disagreement (director decision after 4')."""
    diff = case["diff"]
    rounding = case["rounding"]
    if case["value"] < case["total"]:
        return "understatement: the map's sum is below the filer's own total"
    for concept, value in _filed_short_term(case).items():
        if abs(diff - value) <= rounding:
            return (
                "double count: the sum exceeds the total by the filed short-term term "
                f"{concept.split('_', 1)[-1]}"
            )
    for concept in case["children"]:
        if abs(case["children"][concept] - case["total"]) <= rounding:
            return (
                "the filer's own total equals the filed child "
                f"{concept.split('_', 1)[-1]} (a partial tag)"
            )
    return "overlap: the sum exceeds the filer's own total and no single term explains it"


def _double_count(case: dict[str, Any]) -> str | None:
    """A concept in **both** the long-term sum and the short-term terms of the same total debt.

    The structural hard stop of the director's decision after 4': the two concept sets are disjoint
    by construction, so this can only fire if a future edit breaks that."""
    long_term = set((case["value_concept"] or "").split(" + "))
    total = set((case["debt_total_concept"] or "").split(" + "))
    shared = long_term & (total - long_term)
    return " + ".join(sorted(shared)) if shared else None


def _evaluate_cf_cases(
    cases: list[dict[str, Any]], facts_cache: dict[str, dict[str, Any] | None]
) -> tuple[int, int, list[dict[str, Any]]]:
    """``(covered, agree, disagreements)``: each case's own sum against the filer's own total."""
    covered = 0
    agree = 0
    disagreements: list[dict[str, Any]] = []
    for case in cases:
        if case["kind"] in ("cls", "dbo"):
            # the structural hard stop runs on EVERY filled C2 case, covered or not
            case["double_count"] = _double_count(case)
        cik = str(case["cik"] or "")
        if cik not in facts_cache:
            facts_cache[cik] = _companyfacts(cik)
        total, concept, _end = _cf_total(
            facts_cache[cik],
            _CF_PARENTS[case["kind"]],
            case["accession"],
            date.fromisoformat(str(case["end"])[:10]),
            case["tag"],
        )
        case["total"] = total
        case["total_concept_used"] = concept
        if total is None or case["value"] is None:
            continue
        covered += 1
        rounding = _agreement_rounding(case, total)
        diff = abs(case["value"] - total)
        case["diff"] = diff
        case["rounding"] = rounding
        if diff <= rounding:
            agree += 1
            continue
        explained = None
        if case["kind"] in ("cls", "dbo") and total > case["value"]:
            for concept_st, value in _filed_short_term(case).items():
                if abs(diff - value) <= rounding:
                    explained = f"the total exceeds the long-term sum by {concept_st}"
        if explained is None:
            explained = _case_exclusion(case, diff, rounding)
        case["explained"] = explained
        case["material"] = diff > max(rounding, quality.MATERIALITY * abs(total))
        disagreements.append(case)
    return covered, agree, disagreements


def _material_report(
    cases: list[dict[str, Any]], covered: int, agree: int, disagreements: list[dict[str, Any]]
) -> list[str]:
    unexplained = [c for c in disagreements if not c["explained"]]
    material = [c for c in unexplained if c["material"]]
    lines = [
        f"filing/column cases where C1 or C2 filled the value: {len(cases)}",
        "the value compared: C1's D&A sum; C2's **long-term** sum (never total debt)",
        "agreement band: 0.5 x unit(filer's total) + L-10 rounding(children)",
        f"coverage: {covered} ({100.0 * covered / max(1, len(cases)):.1f}% of filled) carry the "
        "filer's own total in the same accession",
        f"agree: {agree} ({100.0 * agree / max(1, covered):.1f}% of covered)",
        f"disagree: {len(disagreements)} (explained by a short-term term: "
        f"{len(disagreements) - len(unexplained)})",
        f"UNEXPLAINED: {len(unexplained)} ({100.0 * len(unexplained) / max(1, covered):.2f}% of "
        f"covered); of which MATERIAL (DQ-01: beyond max(rounding, 0.5% x |total|)): {len(material)} "
        f"({100.0 * len(material) / max(1, covered):.2f}% of covered)",
        f"double counts (a concept in the long-term sum and the short-term terms): "
        f"{len([c for c in cases if c.get('double_count')])}   [HARD STOP if any]",
        "",
        "per map: filled / covered / agree / material disagreements",
    ]
    for kind in ("da", "cls", "dbo"):
        group = [c for c in cases if c["kind"] == kind]
        cov = [c for c in group if c.get("total") is not None and c["value"] is not None]
        ag = [c for c in cov if abs(c["value"] - c["total"]) <= _agreement_rounding(c, c["total"])]
        mat = [c for c in unexplained if c["kind"] == kind and c["material"]]
        lines.append(f"  {kind}: {len(group)} / {len(cov)} / {len(ag)} / {len(mat)}")
    classes: Counter[str] = Counter()
    for case in material:
        classes[f"{case['kind']}: {_case_class(case)}"] += 1
    if classes:
        lines += ["", "the material disagreements' classes:"]
        lines += [f"  {n:4}  {cause}" for cause, n in classes.most_common()]
    lines += ["", "material disagreements by company (one example each):"]
    seen: set[str] = set()
    for case in sorted(material, key=lambda c: -c["diff"]):
        if case["ticker"] in seen:
            continue
        seen.add(case["ticker"])
        lines.append(
            f"  {case['ticker']} {case['accession']} [{case['kind']}] {case['column']}: sum "
            f"{case['value']:,.0f} vs total {case['total']:,.0f} "
            f"({case['total_concept_used']}) diff {case['diff']:,.0f} "
            f"band {case['rounding']:,.0f} -- {_case_class(case)}"
        )
    return lines


def _stored_metric_inputs(conn: Any, filing_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(inputs, values)`` of the filing's stored ``metrics-v7`` rows (first occurrence per key)."""
    inputs: dict[str, Any] = {}
    values: dict[str, Any] = {}
    for row in conn.execute(
        "SELECT metric_name, value, inputs_json FROM fundamental_metrics "
        "WHERE filing_id = ? AND engine_version = 'metrics-v7'",
        (filing_id,),
    ):
        values.setdefault(str(row["metric_name"]), row["value"])
        if row["inputs_json"]:
            for key, value in json.loads(row["inputs_json"]).items():
                inputs.setdefault(key, value)
    return inputs, values


def _metric_effect(conn: Any, case: dict[str, Any]) -> list[str]:
    """The mismatch's effect on EBITDA / net_debt_to_ebitda / enterprise_value, from the stored
    metric inputs of the same filing (the filer's own total substituted for the map's sum)."""
    inputs, values = _stored_metric_inputs(conn, int(case["filing_id"]))
    item = "depreciation_amortization" if case["kind"] == "da" else "total_debt"
    lines = [
        f"  {case['ticker']} {case['accession']} [{case['kind']}] {case['column']}: {item} "
        f"{case['value']:,.0f} (the map's sum) vs the filer's own total {case['total']:,.0f}"
    ]
    if case["kind"] == "da":
        gap = case["total"] - case["value"]
        ebitda = inputs.get("ebitda_ttm")
        if ebitda is not None and ebitda + gap != 0:
            lines.append(
                f"    EBITDA {ebitda:,.0f} -> {ebitda + gap:,.0f} ({100.0 * gap / ebitda:+.1f}%)"
            )
        else:
            lines.append("    EBITDA not stored for this filing (net_debt_to_ebitda empty)")
    else:
        gap = case["value"] - case["total"]
        enterprise = values.get("enterprise_value")
        if enterprise is not None:
            lines.append(
                f"    enterprise_value {enterprise:,.0f} -> {enterprise - gap:,.0f} "
                f"({100.0 * (-gap) / enterprise:+.1f}%)"
            )
    lines.append(f"    stored net_debt_to_ebitda: {values.get('net_debt_to_ebitda')}")
    return lines


def cmd_validate_cf(args: argparse.Namespace) -> int:
    """3.2': each filled sum against the filer's own total, from the local companyfacts cache."""
    conn = open_ro(args.db)
    all_cases = _filled_cases(conn)
    cases = [c for c in all_cases if _is_own_column(c)] if args.own_only else all_cases
    scope = "the filing's own columns (the 3.1 set)" if args.own_only else "every filled column"
    lines = [
        f"T-151 part 1, 3.2' the filled sums against the filer's own total (SEC companyfacts, "
        f"cache {_COMPANYFACTS}), db={args.db}",
        f"scope: {scope}",
        "",
    ]
    covered, agree, disagreements = _evaluate_cf_cases(cases, {})
    unexplained = [c for c in disagreements if not c["explained"]]
    material = [c for c in unexplained if c["material"]]
    lines += _material_report(cases, covered, agree, disagreements)
    lines += ["", "the material mismatches' effect on the metrics (input for part 3's gate):"]
    for case in material:
        lines += _metric_effect(conn, case)
    lines += ["", "every disagreement (ticker, accession, column, sum, total, diff):"]
    for case in sorted(disagreements, key=lambda c: -c["diff"])[:80]:
        lines.append(
            f"  {case['ticker']} {case['accession']} [{case['kind']}] {case['column']}: sum "
            f"{case['value']:,.0f} vs total {case['total']:,.0f} "
            f"({case['total_concept_used']}) diff {case['diff']:,.0f} band {case['rounding']:,.0f}"
            + (" [material]" if case["material"] else "")
            + (f" -- explained by {case['explained']}" if case["explained"] else "")
        )
    payload = {
        "scope": scope,
        "filled_cases": len(cases),
        "covered": covered,
        "agree": agree,
        "disagree": len(disagreements),
        "unexplained": len(unexplained),
        "material": len(material),
        "double_counts": [c["double_count"] for c in cases if c.get("double_count")],
        "material_by_company": sorted({c["ticker"] for c in material}),
        "disagreements": [{k: v for k, v in c.items() if k != "stmts"} for c in disagreements],
    }
    Path(args.out_txt).write_text("\n".join(lines) + "\n", encoding="utf-8")
    Path(args.out_json).write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print("\n".join(lines[:40]))
    return 0


# -- 3.2' examples -------------------------------------------------------------------------------


def _cf_entries(
    facts: dict[str, Any] | None, concepts: tuple[str, ...], accession: str
) -> list[str]:
    """Every companyfacts entry of *concepts* with *accession* (any period), as report lines."""
    gaap = ((facts or {}).get("facts") or {}).get("us-gaap") or {}
    out: list[str] = []
    for concept in concepts:
        for unit, items in (gaap.get(concept.split("_", 1)[-1]) or {}).get("units", {}).items():
            for entry in items:
                if str(entry.get("accn")) != accession:
                    continue
                out.append(
                    f"  {concept:60} [{unit}] {entry.get('start')} -> {entry.get('end')} = "
                    f"{entry.get('val')}"
                )
    return out


def cmd_cf_example(args: argparse.Namespace) -> int:
    """One disagreement, in full: the face rows of the item, the companyfacts entries of the parent
    and of each child for the same accession, and the sum vs the total."""
    conn = open_ro(args.db)
    row = conn.execute(
        "SELECT f.id, f.form, f.period_end, f.accession_number, a.ticker, a.cik "
        "FROM sec_filings f JOIN assets a ON a.id = f.asset_id WHERE f.accession_number = ?",
        (args.accession,),
    ).fetchone()
    if row is None:
        print(f"no filing with accession {args.accession}")
        return 1
    stmts = statements_from_db(conn, int(row["id"]))
    spec_da, spec_lt = REGISTRY["depreciation_amortization"], REGISTRY["long_term_debt"]
    if args.item == "da":
        children, spec = DA_SOURCED_CHILDREN, spec_da
        column = _own_column(stmts, str(row["form"]), str(row["period_end"]))
        kind = "da"
    else:
        bcol = _balance_column(stmts, str(row["period_end"]))
        cls = stmts._concept_value(spec_lt, bcol, G + "SecuredLongTermDebt") is not None
        children = LONG_TERM_DEBT_NONCURRENT_CHILDREN if cls else LONG_TERM_DEBT_CHILDREN
        spec, column, kind = spec_lt, bcol, "cls" if cls else "dbo"
    totals = _CF_PARENTS[kind]
    lines = [
        f"T-151 part 1, 3.2' example: {row['ticker']} {args.accession} {row['form']} "
        f"{row['period_end']} [{args.item}] column {column}",
        "",
        "the face's rows of the item (concept, caption, value in the column):",
    ]
    lines += [
        f"  {r.get('concept')!s:60} {str(r.get('label'))[:52]:52} {r.get(column)}"
        for r in stmts._rows_for(spec)
        if str(r.get("concept")) in set(children) | set(totals)
    ]
    lines += ["", "the filer's own total and its children (companyfacts, same accession):"]
    facts = _companyfacts(str(row["cik"]))
    lines += _cf_entries(facts, totals, args.accession)
    lines += _cf_entries(facts, children, args.accession)
    Path(args.out_txt).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


# -- 3.2'' the face cases, classified (report only) ----------------------------------------------

_EXCLUSION_RE = re.compile(r"exclud|exclusive of|\bnet of\b|\bbefore\b", re.IGNORECASE)


def _face_cases(conn: Any) -> list[dict[str, Any]]:
    """The 72 cases of 3.2, with the total's caption and its children, for classification."""
    cases: list[dict[str, Any]] = []
    for f in filings(conn):
        stmts = _statements_or_none(conn, int(f["id"]))
        if stmts is None:
            continue
        for kind, (total_concepts, children, statement) in _VALIDATION_TREES.items():
            spec = REGISTRY[
                "depreciation_amortization" if statement == "cash_flow" else "long_term_debt"
            ]
            column = (
                _balance_column(stmts, str(f["period_end"]))
                if statement == "balance_sheet"
                else _own_column(stmts, str(f["form"]), str(f["period_end"]))
            )
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
            caption = next(
                (
                    str(r.get("label") or "")
                    for r in stmts._rows_for(spec)
                    if r.get("concept") in total_concepts
                ),
                "",
            )
            outside = (
                stmts.debt_lines_outside(children)
                if statement == "balance_sheet"
                else stmts.da_lines_outside_sourced(spec)
            )
            cases.append(
                {
                    "kind": kind,
                    "ticker": str(f["ticker"]),
                    "accession": str(f["accession_number"]),
                    "column": column,
                    "stmts": stmts,
                    "total": total,
                    "sum": sum(parts.values()),
                    "max_child": max(parts.values()),
                    "n_children": len(parts),
                    "caption": caption,
                    "outside": outside,
                }
            )
    return cases


def _classify(case: dict[str, Any]) -> str:
    """(a) the total excludes a child its caption names; (b) the total is smaller than a child;
    (c) a partial breakdown; (d) other; (e) a declared exclusion (ID-01, C13 p.25).

    (e) is the check's own declaration, not a defect: a company-specific element the resolver
    excludes by rule (CNP's transition-and-system-restoration bonds), worth exactly the
    difference between the filer's total and the sum of the children."""
    if case["total"] < case["max_child"]:
        return "b"
    if _EXCLUSION_RE.search(case["caption"]):
        return "a"
    diff = abs(case["total"] - case["sum"])
    rounding = quality.l10_rounding([case["total"], case["sum"]])
    if _case_exclusion(case, diff, rounding):
        return "e"
    if case["sum"] < case["total"] and case["outside"]:
        return "c"
    return "d"


def cmd_face_cases(args: argparse.Namespace) -> int:
    """3.2'': classify the face cases of 3.2 (report only, no code change)."""
    conn = open_ro(args.db)
    cases = _face_cases(conn)
    classes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        classes[_classify(case)].append(case)
    labels = {
        "a": "(a) the filer's total excludes a child that its caption names",
        "b": "(b) the total is smaller than one of its own children",
        "c": "(c) a partial breakdown (other lines on the face are outside the tree)",
        "d": "(d) other",
        "e": "(e) a declared exclusion (ID-01, C13 p.25): a company-specific element the "
        "resolver excludes by rule",
    }
    lines = [
        f"T-151 part 1, 3.2'' the {len(cases)} face cases of 3.2, classified (report only), db={args.db}",
        "",
    ]
    for key in ("a", "b", "c", "d", "e"):
        group = classes.get(key, [])
        companies = sorted({c["ticker"] for c in group})
        lines.append(f"{labels[key]}: {len(group)} filings, {len(companies)} companies")
        for case in group[:1]:
            lines.append(
                f"    e.g. {case['ticker']} {case['accession']} [{case['kind']}] {case['column']}: "
                f"total {case['total']:,.0f} caption {case['caption']!r} vs children "
                f"{case['sum']:,.0f} ({case['n_children']} filed); outside: "
                f"{', '.join(case['outside'][:3]) or 'none'}"
            )
        lines.append("")
    Path(args.out_txt).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


# -- 3.5 audit groups ----------------------------------------------------------------------------


def cmd_audit_diff(args: argparse.Namespace) -> int:
    """3.5: the audit keys that changed between the control and after JSON reports."""
    control = {f["key"]: f for f in json.loads(Path(args.control).read_text(encoding="utf-8"))}
    after = {f["key"]: f for f in json.loads(Path(args.after).read_text(encoding="utf-8"))}
    lines = [
        f"T-151 part 1, 3.5 audit groups (resolver,capex,metrics): {args.control} -> {args.after}",
        f"keys: control {len(control)}, after {len(after)}",
        "",
        "keys whose count changed:",
    ]
    changed = 0
    for key in sorted(set(control) | set(after)):
        before = control.get(key, {}).get("count")
        now = after.get(key, {}).get("count")
        if before != now:
            changed += 1
            lines.append(
                f"  {key}: {before} -> {now} (total {after.get(key, control.get(key, {})).get('total')}, "
                f"companies {control.get(key, {}).get('companies')} -> "
                f"{after.get(key, {}).get('companies')})"
            )
    lines.insert(3, f"keys whose count changed: {changed}")
    lines += ["", "keys present in only one report:"]
    for key in sorted(set(control) - set(after)):
        lines.append(f"  control only: {key}")
    for key in sorted(set(after) - set(control)):
        lines.append(f"  after only: {key}")
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
    va = sub.add_parser("validation", help="3.2 the face cases against the filers' own totals")
    va.add_argument("--db", required=True)
    va.add_argument("--out-txt", default="/workspaces/thesis/scratch/t151a/out/p3_validation.txt")
    va.set_defaults(func=cmd_validation)
    vc = sub.add_parser("validate-cf", help="3.2' the filled sums against companyfacts")
    vc.add_argument("--db", required=True)
    vc.add_argument(
        "--out-txt", default="/workspaces/thesis/scratch/t151a/out/p3_validation_cf.txt"
    )
    vc.add_argument(
        "--out-json", default="/workspaces/thesis/scratch/t151a/out/p3_validation_cf.json"
    )
    vc.add_argument("--own-only", action="store_true", help="only the filing's own columns")
    vc.set_defaults(func=cmd_validate_cf)
    ce = sub.add_parser("cf-example", help="3.2' one disagreement in full")
    ce.add_argument("--db", required=True)
    ce.add_argument("--accession", required=True)
    ce.add_argument("--item", choices=("da", "debt"), default="da")
    ce.add_argument("--out-txt", default="/workspaces/thesis/scratch/t151a/out/p3_cf_example.txt")
    ce.set_defaults(func=cmd_cf_example)
    fc = sub.add_parser("face-cases", help="3.2'' classify the face cases (report only)")
    fc.add_argument("--db", required=True)
    fc.add_argument("--out-txt", default="/workspaces/thesis/scratch/t151a/out/p3_face_cases.txt")
    fc.set_defaults(func=cmd_face_cases)
    re_ = sub.add_parser("refusals", help="3.3 the refused sums by outside concept")
    re_.add_argument("--db", required=True)
    re_.add_argument("--out-txt", default="/workspaces/thesis/scratch/t151a/out/p3_refusals.txt")
    re_.set_defaults(func=cmd_refusals)
    ca = sub.add_parser("capex", help="3.4 C3 the net-element filings")
    ca.add_argument("--after", required=True)
    ca.add_argument("--out-txt", default="/workspaces/thesis/scratch/t151a/out/p3_capex_net.txt")
    ca.set_defaults(func=cmd_capex)
    la = sub.add_parser("labels", help="3.4 C4 the labelling rule on the stored metadata")
    la.add_argument("--db", nargs="+", required=True)
    la.add_argument("--out-txt", default="/workspaces/thesis/scratch/t151a/out/p3_labels.txt")
    la.set_defaults(func=cmd_labels)
    ad = sub.add_parser("audit-diff", help="3.5 the audit keys that changed")
    ad.add_argument("--control", required=True)
    ad.add_argument("--after", required=True)
    ad.add_argument("--out-txt", default="/workspaces/thesis/scratch/t151a/out/p3_audit_diff.txt")
    ad.set_defaults(func=cmd_audit_diff)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
