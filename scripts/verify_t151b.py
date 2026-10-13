"""T-151 part 2 verification and measurements (docs/model_fixes.md, T151bc_overnight_plan.md).

Sub-commands (all read-only; ``--control`` is the database recomputed with master's code, ``--after``
the one recomputed with this branch's):

    uv run python scripts/verify_t151b.py changes --control DB --after DB --out out/changes.json
    uv run python scripts/verify_t151b.py flags --control DB --after DB --out out/flags.json
    uv run python scripts/verify_t151b.py counts --control DB --after DB --out out/counts.json
    uv run python scripts/verify_t151b.py audit-diff --control-json J --after-json J --out out/audit.txt
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sec_xcheck.common import open_ro

STATUTORY_TAX_RATE = 0.21  # MET-03's 21% federal rate
_MIN_DENOM = 1e-12  # a relative-change denominator never divides by zero

# The metrics whose definition this task changes (plus the two new ones).
_METRICS = (
    "return_on_assets",
    "return_on_equity",
    "return_on_invested_capital",
    "return_on_invested_capital_effective_rate",
    "effective_tax_rate",
    "nopat",
    "ebitda",
    "net_debt_to_ebitda",
    "debt_to_equity",
    "debt_to_assets",
    "interest_coverage",
    "free_cash_flow_conversion",
    "free_cash_flow_margin",
    "free_cash_flow_yield",
    "sbc_adjusted_fcf_yield",
    "enterprise_value",
    "enterprise_fcf_yield",
    "revenue_growth",
    "operating_income_growth",
    "net_income_growth",
    "free_cash_flow_growth",
    "revenue_cagr",
    "net_income_cagr",
    "operating_cash_flow_cagr",
)

_METRICS_SQL = (
    "SELECT m.filing_id, m.metric_name, m.value, m.inputs_json, f.asset_id "
    "FROM fundamental_metrics m JOIN sec_filings f ON f.id = m.filing_id "
    "WHERE m.engine_version = ? AND m.metric_name IN ({})"
)


def _load(conn: Any, engine: str) -> dict[tuple[int, str], dict[str, Any]]:
    """``{(filing_id, metric_name): {"value", "inputs", "asset_id"}}`` for the changed metrics."""
    placeholders = ",".join("?" for _ in _METRICS)
    sql = _METRICS_SQL.format(placeholders)
    out: dict[tuple[int, str], dict[str, Any]] = {}
    for r in conn.execute(sql, (engine, *_METRICS)):
        out[(int(r["filing_id"]), str(r["metric_name"]))] = {
            "value": r["value"],
            "inputs": json.loads(r["inputs_json"] or "{}"),
            "asset_id": int(r["asset_id"]),
        }
    return out


def _tickers(conn: Any) -> dict[int, str]:
    return {int(r["id"]): str(r["ticker"]) for r in conn.execute("SELECT id, ticker FROM assets")}


def _accessions(conn: Any) -> dict[int, str]:
    return {
        int(r["id"]): str(r["accession_number"] or "")
        for r in conn.execute("SELECT id, accession_number FROM sec_filings")
    }


def cmd_changes(args: argparse.Namespace) -> None:  # noqa: C901 - a report: one linear pass
    """(a) per metric, control vs after: filled / emptied / changed, distinct filings and companies,
    and the largest relative changes with the filing's own inputs."""
    control = open_ro(args.control)
    after = open_ro(args.after)
    engine = args.engine
    c = _load(control, engine)
    a = _load(after, engine)
    tick = _tickers(after)
    acc = _accessions(after)

    keys = set(c) | set(a)
    per_metric: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"filled": [], "emptied": [], "changed": []}
    )
    for key in keys:
        name = key[1]
        cv = c.get(key, {}).get("value")
        av = a.get(key, {}).get("value")
        if cv is None and av is not None:
            per_metric[name]["filled"].append(key)
        elif cv is not None and av is None:
            per_metric[name]["emptied"].append(key)
        elif cv is not None and av is not None and cv != av:
            per_metric[name]["changed"].append(key)

    def _assets(pairs: list[tuple[int, str]]) -> int:
        return len({(a.get(key) or c.get(key, {})).get("asset_id") for key in pairs})

    report: dict[str, Any] = {}
    for name in sorted(per_metric):
        bucket = per_metric[name]
        entry: dict[str, Any] = {}
        for kind in ("filled", "emptied", "changed"):
            pairs = bucket[kind]
            entry[kind] = {"filings": len(pairs), "companies": _assets(pairs)}
        report[name] = entry

    # the largest relative changes, with ticker / accession / old and new / the after inputs
    changed_all: list[tuple[float, tuple[int, str]]] = []
    for bucket in per_metric.values():
        for key in bucket["changed"]:
            cv = c[key]["value"]
            av = a[key]["value"]
            denom = abs(cv) if abs(cv) > _MIN_DENOM else 1.0
            changed_all.append((abs(av - cv) / denom, key))
    changed_all.sort(key=lambda t: t[0], reverse=True)
    top: list[dict[str, Any]] = []
    for rel, key in changed_all[: args.top]:
        fid, name = key
        top.append(
            {
                "ticker": tick.get(a[key]["asset_id"], "?"),
                "accession": acc.get(fid, ""),
                "filing_id": fid,
                "metric": name,
                "control": c[key]["value"],
                "after": a[key]["value"],
                "relative_change": rel,
                "after_inputs": a[key]["inputs"],
            }
        )
    payload = {"per_metric": report, "largest_changes": top}
    Path(args.out).write_text(json.dumps(payload, indent=2))

    print(f"{'metric':<42}{'filled f/c':>14}{'emptied f/c':>14}{'changed f/c':>14}")

    def cell(entry: dict[str, Any], kind: str) -> str:
        return f"{entry[kind]['filings']}/{entry[kind]['companies']}"

    for name in sorted(report):
        e = report[name]
        print(f"{name:<42}{cell(e, 'filled'):>14}{cell(e, 'emptied'):>14}{cell(e, 'changed'):>14}")
    print(f"\nTop {len(top)} relative changes:")
    for row in top:
        print(
            f"  {row['ticker']:<6}{row['metric']:<38}{row['control']!r:>18} -> {row['after']!r:<18}"
            f" rel {row['relative_change']:.3f}  {row['accession']}"
        )
    print(f"\nWrote {args.out}")


def cmd_flags(args: argparse.Namespace) -> None:
    """(b) per new flag: filings and companies, control vs after."""
    control = open_ro(args.control)
    after = open_ro(args.after)
    engine = args.engine

    def scan(conn: Any) -> dict[str, tuple[int, set[int]]]:
        # F6: count distinct filings, not metric rows -- a flag shared by a group's inputs is one
        # per metric row otherwise (5 valuation rows, 3 CAGR series, ...).
        filings: dict[str, set[int]] = defaultdict(set)
        companies: dict[str, set[int]] = defaultdict(set)
        for r in conn.execute(
            "SELECT m.filing_id, m.inputs_json, f.asset_id FROM fundamental_metrics m "
            "JOIN sec_filings f ON f.id = m.filing_id WHERE m.engine_version = ?",
            (engine,),
        ):
            for flag in json.loads(r["inputs_json"] or "{}"):
                if flag.startswith("flag_"):
                    filings[flag].add(int(r["filing_id"]))
                    companies[flag].add(int(r["asset_id"]))
        return {flag: (len(filings[flag]), companies[flag]) for flag in filings}

    c = scan(control)
    a = scan(after)
    payload: dict[str, Any] = {}
    print(f"{'flag':<70}{'control':>16}{'after':>16}")
    for flag in sorted(set(c) | set(a)):
        cn, cc = c.get(flag, (0, set()))
        an, ac = a.get(flag, (0, set()))
        if cn == an and cc == ac:
            continue
        payload[flag] = {
            "control": {"filings": cn, "companies": len(cc)},
            "after": {"filings": an, "companies": len(ac)},
        }
        print(f"{flag:<70}{cn:>6}/{len(cc):<8}{an:>6}/{len(ac):<8}")
    Path(args.out).write_text(json.dumps(payload, indent=2))
    print(f"\nWrote {args.out} ({len(payload)} flags changed)")


# -- (c) the acceptance counts, from the stored metrics -------------------------------------------


def _rows_by_filing(conn: Any, engine: str) -> dict[int, dict[str, dict[str, Any]]]:
    out: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in conn.execute(
        "SELECT filing_id, metric_name, value, inputs_json FROM fundamental_metrics "
        "WHERE engine_version = ?",
        (engine,),
    ):
        out[int(r["filing_id"])][str(r["metric_name"])] = {
            "value": r["value"],
            "inputs": json.loads(r["inputs_json"] or "{}"),
        }
    return out


def _reit_assets(conn: Any) -> set[int]:
    return {
        int(r["id"])
        for r in conn.execute(
            "SELECT a.id FROM assets a JOIN sectors s ON a.sector_id = s.id "
            "WHERE s.name = 'Real Estate' AND a.sub_industry LIKE '%REITs'"
        )
    }


def _counts(  # noqa: C901, PLR0912, PLR0915 - one predicate per rule, a linear pass
    conn: Any, engine: str
) -> dict[str, tuple[int, int]]:
    """Each acceptance condition over one database: ``{key: (filings, companies)}``."""
    rows = _rows_by_filing(conn, engine)
    assets = {
        int(r["id"]): int(r["asset_id"])
        for r in conn.execute("SELECT id, asset_id FROM sec_filings")
    }
    forms = {int(r["id"]): str(r["form"]) for r in conn.execute("SELECT id, form FROM sec_filings")}
    reits = _reit_assets(conn)

    def metric(filing: dict[str, dict[str, Any]], name: str) -> float | None:
        row = filing.get(name)
        return None if row is None else row["value"]

    def inp(filing: dict[str, dict[str, Any]], name: str) -> dict[str, float]:
        row = filing.get(name)
        return {} if row is None else row["inputs"]

    counts: dict[str, int] = defaultdict(int)
    asset_sets: dict[str, set[int]] = defaultdict(set)
    for fid, filing in rows.items():
        asset = assets.get(fid, 0)
        rate_metric = metric(filing, "effective_tax_rate")
        pretax = inp(filing, "effective_tax_rate").get("pretax_income")
        nopat_inputs = inp(filing, "nopat")
        nopat = metric(filing, "nopat")
        operating = inp(filing, "nopat").get("operating_income")

        def add(key: str, asset_id: int) -> None:
            counts[key] += 1
            asset_sets[key].add(asset_id)

        if rate_metric == STATUTORY_TAX_RATE and (pretax is None or pretax <= 0.0):
            add("N10.tax_rate_21_on_nonpositive_pretax", asset)
        if rate_metric == STATUTORY_TAX_RATE and pretax is not None and pretax <= 0.0:
            add("N10.tax_rate_21_on_loss", asset)
        # NOPAT's own rate: master stores `effective_tax_rate` (the rate used); this branch stores
        # `nopat_tax_rate` and the `nopat_tax_rate_reit_effective` flag.
        if "nopat_tax_rate" in nopat_inputs:
            # the four legitimate reasons: 21%, a REIT's own rate (APP-04c), 0 on a REIT with no
            # pre-tax income, and 0 on an operating loss (MET-03)
            reasons = (
                "nopat_tax_rate_statutory_21",
                "nopat_tax_rate_reit_effective",
                "nopat_tax_rate_reit_no_rate",
                "nopat_tax_rate_operating_loss",
            )
            if not any(nopat_inputs.get(reason) == 1.0 for reason in reasons):
                add("MET03.nopat_on_filed_rate", asset)
            if asset in reits and nopat_inputs.get("nopat_tax_rate_statutory_21") == 1.0:
                add("APP04c.reit_nopat_at_21", asset)
        else:
            used = nopat_inputs.get("effective_tax_rate")
            if (
                used is not None
                and used != STATUTORY_TAX_RATE
                and pretax is not None
                and pretax > 0.0
            ):
                add("MET03.nopat_on_filed_rate", asset)
            if asset in reits and used == STATUTORY_TAX_RATE:
                add("APP04c.reit_nopat_at_21", asset)
        if operating is not None and operating < 0.0 and nopat is not None and nopat > operating:
            add("MET03.nopat_taxed_on_operating_loss", asset)

        # F1: the NOPAT tax follows the flow it taxes. The TTM operating income the ratios tax is
        # `operating_income_ttm` (this branch); master's inputs have only `nopat_ttm` and the rate
        # it used, so it is derived from those.
        ttm_oi = nopat_inputs.get("operating_income_ttm")
        nopat_ttm = nopat_inputs.get("nopat_ttm")
        rate_used = nopat_inputs.get("nopat_ttm_tax_rate_value")
        if rate_used is None:
            rate_used = nopat_inputs.get("effective_tax_rate")  # master's inputs
        if ttm_oi is None and nopat_ttm is not None and rate_used is not None and rate_used < 1.0:
            ttm_oi = nopat_ttm / (1.0 - rate_used)
        if ttm_oi is not None and nopat_ttm is not None:
            if ttm_oi < 0.0 and nopat_ttm > ttm_oi:
                add("F1.ttm_operating_loss_taxed", asset)
            if ttm_oi > 0.0 and abs(nopat_ttm - ttm_oi) <= 1e-6 * max(1.0, abs(ttm_oi)):
                add("F1.ttm_operating_profit_untaxed", asset)
        # a REIT 10-Q taxed at its own period rate instead of its TTM rate
        if (
            asset in reits
            and forms.get(fid, "").startswith("10-Q")
            and nopat_ttm is not None
            and rate_used is not None
            and rate_used == rate_metric
        ):
            add("F1.reit_10q_on_the_quarters_rate", asset)

        ic = inp(filing, "return_on_invested_capital")
        ic_value = ic.get("invested_capital")
        if ic_value is None:  # master's inputs: invested capital = equity + debt - cash
            eq, debt, cash = ic.get("equity"), ic.get("total_debt"), ic.get("cash")
            if eq is not None or debt is not None:
                ic_value = (eq or 0.0) + (debt or 0.0) - (cash or 0.0)
        if (
            ic_value is not None
            and ic_value <= 0.0
            and metric(filing, "return_on_invested_capital") is not None
        ):
            add("N15.roic_on_nonpositive_capital", asset)
        prior_eq = ic.get("prior_equity")
        if (
            prior_eq is not None
            and prior_eq < 0.0
            and metric(filing, "return_on_equity") is not None
        ):
            add("N15.roe_on_negative_equity", asset)

        nd_inputs = inp(filing, "net_debt_to_ebitda")
        ebitda = nd_inputs.get("ebitda", nd_inputs.get("ebitda_ttm"))
        if ebitda is None:  # master's inputs carry the two terms only
            op, da = nd_inputs.get("operating_income"), nd_inputs.get("depreciation_amortization")
            ebitda = None if op is None or da is None else op + da
        nd = metric(filing, "net_debt_to_ebitda")
        if ebitda is not None and ebitda > 0.0 and nd is not None and nd < 0.0:
            add("N9.net_cash_positive_ebitda", asset)
        if ebitda is not None and ebitda <= 0.0 and nd is not None:
            add("MET09.net_debt_ebitda_on_nonpositive_ebitda", asset)
        fcf_inputs = inp(filing, "free_cash_flow_conversion")
        # the denominator is the annual (TTM on a 10-Q) net income the metric actually used
        ni = fcf_inputs.get("net_income_ttm", fcf_inputs.get("net_income"))
        if ni is not None and ni <= 0.0 and metric(filing, "free_cash_flow_conversion") is not None:
            add("MET09.fcf_conversion_on_nonpositive_income", asset)

        for name, base_key in (
            ("revenue_growth", "revenue_prior"),
            ("operating_income_growth", "operating_income_prior"),
            ("net_income_growth", "net_income_prior"),
            ("free_cash_flow_growth", "free_cash_flow_prior"),
        ):
            base = inp(filing, name).get(base_key)
            if base is not None and base <= 0.0 and metric(filing, name) is not None:
                add(f"MET04.{name}_base_nonpositive", asset)

        # PER-13: T-147 counted the revenue CAGRs; the other two are reported beside them.
        for name in ("revenue_cagr", "net_income_cagr", "operating_cash_flow_cagr"):
            if inp(filing, name).get("flag_crosses_asc842") == 1.0:
                add(f"PER13.{name}_crosses_asc842", asset)
        if inp(filing, "net_income_cagr").get("flag_crosses_cecl") == 1.0:
            add("PER14.net_income_cagr_crosses_cecl", asset)

    return {key: (counts[key], len(asset_sets[key])) for key in counts}


def cmd_counts(args: argparse.Namespace) -> None:
    """(c) the T-151 acceptance counts, control vs after."""
    control = _counts(open_ro(args.control), args.engine)
    after = _counts(open_ro(args.after), args.engine)
    payload: dict[str, Any] = {}
    print(f"{'key':<56}{'control':>16}{'after':>16}")
    for key in sorted(set(control) | set(after)):
        cf, cc = control.get(key, (0, 0))
        af, ac = after.get(key, (0, 0))
        payload[key] = {
            "control": {"filings": cf, "companies": cc},
            "after": {"filings": af, "companies": ac},
        }
        print(f"{key:<56}{cf:>6}/{cc:<8}{af:>6}/{ac:<8}")
    Path(args.out).write_text(json.dumps(payload, indent=2))
    print(f"\nWrote {args.out}")


def cmd_roa(args: argparse.Namespace) -> None:
    """R-4's breakdown: the empty ``return_on_assets`` rows that file no operating income, by GICS
    sector (the director's R-4 answer asks for the numbers re-taken with a committed script)."""
    conn = open_ro(args.db)
    rows = _rows_by_filing(conn, args.engine)
    sectors = {
        int(r["filing_id"]): (r["sector"], int(r["asset_id"]))
        for r in conn.execute(
            "SELECT f.id AS filing_id, COALESCE(s.name, '?') AS sector, f.asset_id "
            "FROM sec_filings f JOIN assets a ON a.id = f.asset_id "
            "LEFT JOIN sectors s ON s.id = a.sector_id"
        )
    }
    filings: dict[str, set[int]] = defaultdict(set)
    companies: dict[str, set[int]] = defaultdict(set)
    total = 0
    for fid, filing in rows.items():
        row = filing.get("return_on_assets")
        if row is None or row["value"] is not None:
            continue
        if row["inputs"].get("operating_income") is not None:
            continue
        sector, asset_id = sectors.get(fid, ("?", 0))
        filings[sector].add(fid)
        companies[sector].add(asset_id)
        total += 1
    payload = {
        sector: {"filings": len(filings[sector]), "companies": len(companies[sector])}
        for sector in sorted(filings)
    }
    payload["TOTAL"] = {
        "filings": total,
        "companies": len(set().union(*companies.values()) if companies else set()),
    }
    Path(args.out).write_text(json.dumps(payload, indent=2))
    for sector in sorted(payload):
        entry = payload[sector]
        print(f"{sector:<28}{entry['filings']:>6} filings {entry['companies']:>5} companies")
    print(f"Wrote {args.out}")


def cmd_audit_diff(args: argparse.Namespace) -> None:
    """(d) the keys of ``audit_sec_checklist measure --only metrics`` that changed, control vs after."""

    def by_key(path: str) -> dict[str, dict[str, Any]]:
        return {str(f["key"]): f for f in json.loads(Path(path).read_text())}

    c = by_key(args.control_json)
    a = by_key(args.after_json)
    lines: list[str] = []
    for key in sorted(set(c) | set(a)):
        cf, cc = c.get(key, {}).get("count"), c.get(key, {}).get("companies")
        af, ac = a.get(key, {}).get("count"), a.get(key, {}).get("companies")
        if (cf, cc) != (af, ac):
            lines.append(f"{key}: {cf}/{cc} -> {af}/{ac}")
    text = "\n".join(lines) + "\n"
    Path(args.out).write_text(text)
    print(text)
    print(f"Wrote {args.out} ({len(lines)} keys changed)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (
        ("changes", cmd_changes),
        ("flags", cmd_flags),
        ("counts", cmd_counts),
    ):
        p = sub.add_parser(name)
        p.add_argument("--control", required=True)
        p.add_argument("--after", required=True)
        p.add_argument("--engine", default="metrics-v7")
        p.add_argument("--out", required=True)
        if name == "changes":
            p.add_argument("--top", type=int, default=20)
        p.set_defaults(func=func)
    p = sub.add_parser("roa")
    p.add_argument("--db", required=True)
    p.add_argument("--engine", default="metrics-v7")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_roa)
    p = sub.add_parser("audit-diff")
    p.add_argument("--control-json", required=True)
    p.add_argument("--after-json", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_audit_diff)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
