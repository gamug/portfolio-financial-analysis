"""T-076's measurements: prompt sizes, the withheld values, the rendered briefs, the prompt hash.

    uv run python scripts/verify_t076.py sizes --db PATH
    uv run python scripts/verify_t076.py withheld --db PATH
    uv run python scripts/verify_t076.py briefs --db PATH [--tickers ABBV,O,KKR,COP,JPM]
    uv run python scripts/verify_t076.py hash
    uv run python scripts/verify_t076.py quotes

Every number T-076 reports comes from here. The database is opened read-only (``connect_ro``); no model is
called and nothing is written to the repository -- the output goes under the task's scratch ``out/``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from fundamental_agent import agents, quality  # noqa: E402
from fundamental_agent.metrics.base import MetricResult  # noqa: E402
from fundamental_agent.skills import GROUP_METRICS  # noqa: E402
from kg_schema import db as kg_db  # noqa: E402
from kg_schema.versions import MetricVersions  # noqa: E402

OUT = Path("/workspaces/thesis/scratch/t076/out")
MASTER = "dcd498f"  # the master commit T-076 branches from (PR #151 merged)
MODEL = "deepseek-chat"  # the model id the version hash is computed for (no call is made)
_FORMS = ("10-K", "10-Q")  # the two form types the per-filing prompt is measured for
_SOURCES_COLUMNS = 4  # Source | Page | Quote | Backs

# The master side: the old directory per group, and the old lead / inline briefs (agents.py at MASTER).
_OLD_DIR = {
    "cashflow": "fcf_margin",
    "leverage": "interest_coverage_ratio",
    "roic": "roic",
    "cagr": "cagr",
    "valuation": "free_cash_flow_yield",
}
_OLD_LEAD = (
    "You are the {group} specialist in a fundamental-analysis pipeline. The ratios "
    "are already computed for you -- do not recompute them and do not ask for filing "
    "text. Give a concise 1-2 sentence assessment for the orchestrator, applying any "
    "caveat the procedure below would raise (negative base, interest netting, "
    "unclassified balance sheet, lease/SBC distortions)."
)
_OLD_INLINE = {
    "profitability": "You read profitability ratios (margins, ROA, ROE). "
    "State whether profitability is strong, adequate or weak and why.",
    "liquidity": "You read liquidity ratios (current, quick, cash). "
    "State whether short-term solvency looks comfortable or tight.",
    "leverage": "You read leverage ratios (debt/equity, debt/assets, interest coverage, "
    "net debt/EBITDA). State whether the balance sheet is conservative or stretched.",
    "cashflow": "You read cash-flow quality (operating cash flow margin, free cash flow "
    "margin and conversion, capex intensity). State whether cash generation backs earnings.",
    "efficiency": "You read efficiency ratios (asset, inventory, receivables turnover). "
    "State whether the asset base is used efficiently.",
    "growth": "You read year-over-year growth (revenue, operating income, net income, free "
    "cash flow). State whether the trajectory is improving or deteriorating.",
    "roic": "You read return on invested capital and the effective tax rate. "
    "State whether the company earns above its cost of capital.",
    "cagr": "You read multi-year compound annual growth rates (revenue, net income, "
    "operating cash flow). State whether long-run growth is strong, flat or declining.",
    "valuation": "You read free-cash-flow yield (equity and enterprise), its "
    "SBC-adjusted variant and market capitalisation. State whether the price paid for "
    "the company's cash generation looks cheap, fair or rich, and flag SBC dilution.",
}
_OLD_MASTER_PROMPT = (
    "You are the metrics master for a fundamental equity analysis.\n"
    "You are given the deterministically computed financial ratios for one SEC filing.\n"
    "Call the specialist tool for every metric group that carries signal for this company\n"
    "-- always consult profitability, liquidity, leverage and cash flow; add efficiency,\n"
    "growth, roic and cagr when their numbers are meaningful (skip inventory-style metrics\n"
    "for financial firms, skip growth/cagr when there is no multi-period comparison); add\n"
    "valuation whenever free-cash-flow-yield numbers are present.\n"
    "After gathering the readings, give a short synthesis in plain language."
)


def _git_show(path: str) -> str | None:
    result = subprocess.run(  # noqa: S603 - a fixed `git show`, no shell and no caller input
        ["git", "-C", str(REPO), "show", path],  # noqa: S607 - resolved from PATH
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _master_specialist_prompt(group: str) -> str:
    lead = _OLD_LEAD.format(group=group)
    name = _OLD_DIR.get(group, group)
    sop = _git_show(f"{MASTER}:skills/{name}/SKILL.md")
    if sop is None:
        return f"{lead}\n\n{_OLD_INLINE[group]}"
    return f"{lead}\n\n--- STANDARD OPERATING PROCEDURE: {group} ---\n{sop}"


def _old_prompt_hash() -> str:
    payload = {
        "master_prompt": _OLD_MASTER_PROMPT,
        "specialist_prompts": {g: _master_specialist_prompt(g) for g in GROUP_METRICS},
        "synthesis_prompt": agents._SYNTHESIS_PROMPT,
        "repair_prompt": agents._REPAIR_PROMPT,
        "model_id": MODEL,
        "temperature": agents._TEMPERATURE,
        "seed": agents._SEED,
        "max_tokens": agents._MAX_TOKENS,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _versions(conn: Any) -> MetricVersions:
    rows = conn.execute(
        "SELECT DISTINCT metric_group, engine_version FROM fundamental_metrics"
    ).fetchall()
    return MetricVersions({str(r["metric_group"]): str(r["engine_version"]) for r in rows})


def _filing_ids(conn: Any) -> list[int]:
    rows = conn.execute(
        "SELECT DISTINCT filing_id FROM fundamental_metrics ORDER BY filing_id"
    ).fetchall()
    return [int(r["filing_id"]) for r in rows]


def _load_one(
    conn: Any, versions: MetricVersions, fid: int
) -> tuple[int, dict[tuple[str, str], quality.StoredMetric]] | None:
    """One filing's stored metrics. Loading every filing at once holds the whole `inputs_json` in
    memory and is killed; one filing at a time is a small indexed query."""
    return quality._load(conn, versions, fid).get(fid)


def _tickers(conn: Any) -> dict[int, str]:
    rows = conn.execute("SELECT id, ticker FROM assets").fetchall()
    return {int(r["id"]): str(r["ticker"]) for r in rows}


def _forms(conn: Any) -> dict[int, tuple[str, str, str]]:
    """``filing_id -> (ticker, form, fiscal_period)``."""
    tickers = _tickers(conn)
    rows = conn.execute("SELECT id, asset_id, form, fiscal_period FROM sec_filings").fetchall()
    return {
        int(r["id"]): (
            tickers.get(int(r["asset_id"]), "?"),
            str(r["form"]),
            str(r["fiscal_period"]),
        )
        for r in rows
    }


def _by_group(
    metrics: dict[tuple[str, str], quality.StoredMetric],
) -> dict[str, list[MetricResult]]:
    out: dict[str, list[MetricResult]] = {}
    for (group, name), stored in metrics.items():
        out.setdefault(group, []).append(
            MetricResult(name, stored.value, "ratio", dict(stored.inputs))
        )
    for results in out.values():
        results.sort(key=lambda result: result.name)
    return out


# -- sizes ------------------------------------------------------------------------------------------------


def cmd_sizes(args: argparse.Namespace) -> None:
    lines = [
        "# T-076 prompt sizes (characters)",
        "",
        f"Master side: {MASTER}. Branch side: the working tree. Model: {MODEL} (no call is made).",
        "No tokenizer is installed offline, so token counts are omitted.",
        "",
        "## Each specialist's system prompt",
        "",
        "| group | master | branch | delta |",
        "|---|---|---|---|",
    ]
    for group in GROUP_METRICS:
        old = len(_master_specialist_prompt(group))
        new = len(agents._specialist_system_prompt(group))
        lines.append(f"| {group} | {old} | {new} | {new - old:+d} |")
    old_total = sum(len(_master_specialist_prompt(g)) for g in GROUP_METRICS)
    new_total = sum(len(agents._specialist_system_prompt(g)) for g in GROUP_METRICS)
    lines += [
        "",
        f"All nine, master {old_total} -> branch {new_total} ({new_total - old_total:+d}).",
        "",
    ]
    if args.db:
        conn = kg_db.connect_ro(args.db)
        versions = _versions(conn)
        forms = _forms(conn)
        picked: dict[str, tuple[int, str]] = {}
        for fid in _filing_ids(conn):
            ticker, form, _fp = forms.get(fid, ("?", "?", "?"))
            if form in _FORMS and form not in picked:
                picked[form] = (fid, ticker)
            if len(picked) == len(_FORMS):
                break
        lines += ["## One filing's whole prompt, per form", ""]
        for form, (fid, ticker) in sorted(picked.items()):
            loaded = _load_one(conn, versions, fid)
            if loaded is None:
                continue
            by_group = _by_group(loaded[1])
            withheld = agents.withheld_metrics(
                [(group, result) for group, results in by_group.items() for result in results]
            )
            _t, _f, fiscal_period = forms[fid]
            ctx = agents.FilingContext(
                ticker,
                ticker,
                form,
                fiscal_period,
                None,
                "",
                None,  # type: ignore[arg-type]
            )
            brief = agents._brief(ctx, by_group, withheld)
            master_side = len(_OLD_MASTER_PROMPT) + sum(
                len(_master_specialist_prompt(g)) for g in by_group
            )
            branch_side = (
                len(agents.MASTER_PROMPT)
                + sum(len(agents._specialist_system_prompt(g)) for g in by_group)
                + len(brief)
            )
            lines.append(
                f"- {form} {ticker} ({fid}): master {master_side} -> branch {branch_side} "
                f"({branch_side - master_side:+d}); the brief alone is {len(brief)} characters."
            )
        lines.append("")
    text = "\n".join(lines) + "\n"
    (OUT / "sizes.txt").write_text(text)
    print(text)


# -- withheld ---------------------------------------------------------------------------------------------


def cmd_withheld(args: argparse.Namespace) -> None:
    conn = kg_db.connect_ro(args.db)
    versions = _versions(conn)
    tickers = _tickers(conn)
    per_rule: dict[str, dict[str, Any]] = {}
    fallback_changes: list[dict[str, Any]] = []
    filings_with_a_withheld_value = 0
    for fid in _filing_ids(conn):
        loaded = _load_one(conn, versions, fid)
        if loaded is None:
            continue
        asset_id, metrics = loaded
        by_group = _by_group(metrics)
        pairs = [(group, result) for group, results in by_group.items() for result in results]
        withheld = agents.withheld_metrics(pairs)
        if not withheld:
            continue
        filings_with_a_withheld_value += 1
        flat = {f"{group}.{result.name}": result.value for group, result in pairs}
        before = agents._fallback_assessment(flat).score
        after = agents._fallback_assessment(flat, withheld).score
        for (_group, name), rule in withheld.items():
            entry = per_rule.setdefault(rule, {"filings": 0, "companies": set(), "metrics": set()})
            entry["filings"] += 1
            entry["companies"].add(tickers.get(asset_id, "?"))
            entry["metrics"].add(name)
        if before != after:
            fallback_changes.append(
                {
                    "filing_id": fid,
                    "ticker": tickers.get(asset_id, "?"),
                    "before": before,
                    "after": after,
                    "withheld": sorted(f"{g}.{n}" for (g, n) in withheld),
                }
            )
    report = {
        "db": str(args.db),
        "filings_with_a_withheld_value": filings_with_a_withheld_value,
        "per_rule": {
            rule: {
                "rows": entry["filings"],
                "companies": len(entry["companies"]),
                "metrics": sorted(entry["metrics"]),
            }
            for rule, entry in sorted(per_rule.items())
        },
        "fallback_scores_changed": len(fallback_changes),
        "fallback_changes": fallback_changes[:50],
    }
    (OUT / "withheld.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


# -- briefs -----------------------------------------------------------------------------------------------


def cmd_briefs(args: argparse.Namespace) -> None:
    conn = kg_db.connect_ro(args.db)
    versions = _versions(conn)
    forms = _forms(conn)
    out_dir = OUT / "briefs"
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = [t for t in args.tickers.split(",") if t]
    latest: dict[str, int] = {}
    for fid in _filing_ids(conn):
        ticker = forms.get(fid, ("?",))[0]
        if ticker in wanted:
            latest[ticker] = fid
    picked: list[int] = [latest[t] for t in wanted if t in latest]
    for fid in _filing_ids(conn):
        loaded = _load_one(conn, versions, fid)
        if loaded is None:
            continue
        by_group = _by_group(loaded[1])
        pairs = [(group, result) for group, results in by_group.items() for result in results]
        if agents.withheld_metrics(pairs):
            picked.append(fid)
            break
    written = 0
    for fid in picked:
        loaded = _load_one(conn, versions, fid)
        if loaded is None:
            continue
        by_group = _by_group(loaded[1])
        pairs = [(group, result) for group, results in by_group.items() for result in results]
        withheld = agents.withheld_metrics(pairs)
        ticker, form, fiscal_period = forms[fid]
        ctx = agents.FilingContext(
            ticker,
            ticker,
            form,
            fiscal_period,
            None,
            "",
            None,  # type: ignore[arg-type]
        )
        parts = [
            f"### MASTER PROMPT\n{agents.MASTER_PROMPT}",
            "### BRIEF",
            agents._brief(ctx, by_group, withheld),
        ]
        for group in by_group:
            parts.append(
                f"### SPECIALIST {group} (system prompt)\n{agents._specialist_system_prompt(group)}"
            )
            parts.append(
                f"### SPECIALIST {group} (per-call prompt)\n"
                + _specialist_call(ctx, group, by_group, withheld)
            )
        (out_dir / f"{ticker}_{fid}.txt").write_text("\n\n".join(parts) + "\n")
        written += 1
    print(f"wrote {written} briefs to {out_dir}")


def _specialist_call(
    ctx: agents.FilingContext,
    group: str,
    by_group: dict[str, list[MetricResult]],
    withheld: dict[tuple[str, str], str],
) -> str:
    results = by_group.get(group, [])
    paragraph = agents.basis_paragraph(group, results) if ctx.form == "10-Q" else ""
    return "\n\n".join(
        part
        for part in (
            f"{ctx.ticker} ({ctx.company_name}) {ctx.form} {ctx.fiscal_period}",
            paragraph,
            f"{group}:\n" + "\n".join(agents._metric_lines(group, results, withheld)),
        )
        if part
    )


# -- hash -------------------------------------------------------------------------------------------------


def cmd_hash(_args: argparse.Namespace) -> None:
    old = _old_prompt_hash()
    new = agents._prompt_version_hash(MODEL)
    text = (
        f"master  {old}\nbranch  {new}\nmodel   {MODEL}\nchanged {'yes' if old != new else 'no'}\n"
    )
    (OUT / "prompt_hash.txt").write_text(text)
    print(text)


# -- quotes -----------------------------------------------------------------------------------------------


def cmd_quotes(_args: argparse.Namespace) -> None:
    """Verify every quote of `skills/SOURCES.md` with quotecheck's own resolution and matching."""
    sys.path.insert(0, str(REPO / "scripts"))
    from sec_xcheck import quotecheck as qc  # noqa: PLC0415

    docs = qc.sources_dir(None)
    rows = []
    for line in (REPO / "skills" / "SOURCES.md").read_text().splitlines():
        if not line.startswith("| ") or line.startswith(("| Source |", "|---")):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == _SOURCES_COLUMNS:
            rows.append(cells)
    problems = 0
    report: list[str] = []
    for source, page, quote, _backs in rows:
        try:
            srcs = qc.cited(f"{source} {page}", docs)
        except SystemExit:
            # The register's C13 stem contains spaces and shares its prefix with a PNG, so its token
            # cannot be resolved; resolve it here by its own stem.
            pdf = str(docs / "C13_rating methodology_regulated electric and gas utilities.pdf")
            srcs = [(pdf, {int(page.removeprefix("p."))})]
        for fragment in qc.re.findall(r'"([^"]+)"', quote):
            status, where = qc.find(fragment, srcs)
            ok = status.startswith("OK")
            problems += not ok
            report.append(f"{source:6s} {page:6s} {status:12s} {where:48s} {fragment[:60]}")
    report.append(f"\nquotes {len(rows)}  problems {problems} (quotecheck.py's own cited/find)")
    text = "\n".join(report) + "\n"
    print(text)
    (OUT / "quotecheck_skills.txt").write_text(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sizes = sub.add_parser("sizes")
    sizes.add_argument("--db")
    sizes.set_defaults(func=cmd_sizes)
    withheld = sub.add_parser("withheld")
    withheld.add_argument("--db", required=True)
    withheld.set_defaults(func=cmd_withheld)
    briefs = sub.add_parser("briefs")
    briefs.add_argument("--db", required=True)
    briefs.add_argument("--tickers", default="ABBV,O,KKR,COP,JPM")
    briefs.set_defaults(func=cmd_briefs)
    hash_cmd = sub.add_parser("hash")
    hash_cmd.set_defaults(func=cmd_hash)
    quotes = sub.add_parser("quotes")
    quotes.set_defaults(func=cmd_quotes)
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
