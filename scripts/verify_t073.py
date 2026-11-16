"""T-073 verification and measurements (docs/model_fixes.md, T073_plan.md D5/D6).

Every number this task reports comes from here, with its output file under the task scratch
``out/``. The database is read-only and is T-071 part 1's own scratch copy
(``/workspaces/thesis/scratch/t071a/after.db``, the one its ``out/sha1.txt`` records), so the
pillar statuses are read from the rows that recompute wrote -- nothing here recomputes a metric.

Sub-commands::

    uv run python scripts/verify_t073.py status    --db DB --out out/pillar_status.json
    uv run python scripts/verify_t073.py unscored  --db DB --out out/unscored.json
    uv run python scripts/verify_t073.py fallback  --db DB --out out/fallback_distribution.json
    uv run python scripts/verify_t073.py weights   --db DB --out out/weights_sensitivity.json
    uv run python scripts/verify_t073.py prompts   --db DB --out-dir out/
    uv run python scripts/verify_t073.py briefs    --db DB --out-dir out/briefs/
    uv run python scripts/verify_t073.py all       --db DB --out-dir out/
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from cycle.config import CycleSettings
from fundamental_agent import agents, pillars
from fundamental_agent import db as fdb
from fundamental_agent.metrics import CORE_GROUPS, OPTIONAL_GROUPS
from fundamental_agent.metrics import valuation as valuation_metrics
from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.statements import Statements
from kg_schema.company_profile import CompanyProfile, profile_as_of
from kg_schema.db import connect_ro
from kg_schema.pillar_score import DEFAULT_PILLAR_WEIGHTS, total_from_block

REPO = Path(__file__).resolve().parents[1]
CYCLE_DATE = "2026-09-22"
ENGINE = fdb.METRICS_ENGINE_VERSION
# The master the task started from (T-071 part 1 and T-153 part 1 merged): the pre-T-073 fallback
# is read from its own source, so "before" and "after" come from the same script (AGENTS.md).
MASTER_COMMIT = "2a17bf5"
# The sample D5 names for the rendered briefs, and one more unscored member beside them (O is
# itself one of the plan's unscored 18).
BRIEF_TICKERS = ("ABBV", "O", "KKR", "COP", "JPM", "PEP")
# The original 0.30 / 0.25 / 0.25 / 0.20, mapped profitability / financial_strength / cash_flow /
# growth (D5's sensitivity set).
OTHER_WEIGHTS = {
    "profitability": 0.30,
    "financial_strength": 0.25,
    "cash_flow": 0.25,
    "growth": 0.20,
}
# The groups in the order the pipeline computes them, so a rebuilt brief renders exactly like the
# one the pipeline sends (T-076's `_brief` walks `_group`'s insertion order).
GROUP_ORDER = (*CORE_GROUPS, *OPTIONAL_GROUPS, valuation_metrics.GROUP)


def _inputs(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


class Stored:
    """The scratch database, read once: assets, filings, their stored rows and the profiles."""

    def __init__(self, db_path: str) -> None:
        self.conn = connect_ro(db_path)
        self.assets = {
            int(r["id"]): {
                "ticker": r["ticker"],
                "company_name": r["company_name"],
                "cik": r["cik"],
                "sector": r["sector"],
                "sub_industry": r["sub_industry"],
            }
            for r in self.conn.execute(
                "SELECT a.id, a.ticker, a.company_name, a.cik, s.name AS sector, a.sub_industry "
                "FROM assets a LEFT JOIN sectors s ON a.sector_id = s.id"
            )
        }
        self.filings = {
            int(r["id"]): {
                "asset_id": int(r["asset_id"]),
                "cik": r["cik"],
                "form": r["form"],
                "fiscal_period": r["fiscal_period"],
                "period_end": r["period_end"],
                "available_at": r["available_at"],
            }
            for r in self.conn.execute(
                "SELECT id, asset_id, cik, form, fiscal_period, period_end, available_at "
                "FROM sec_filings"
            )
        }
        self.rows: dict[int, dict[str, tuple[str, float | None, str, dict[str, Any]]]] = (
            defaultdict(dict)
        )
        for r in self.conn.execute(
            "SELECT filing_id, metric_group, metric_name, value, unit, inputs_json "
            "FROM fundamental_metrics WHERE engine_version = ?",
            (ENGINE,),
        ):
            self.rows[int(r["filing_id"])][r["metric_name"]] = (
                r["metric_group"],
                r["value"],
                r["unit"],
                _inputs(r["inputs_json"]),
            )
        self.profile_of: dict[int, CompanyProfile | None] = {
            filing_id: profile_as_of(filing["cik"], filing["period_end"])
            for filing_id, filing in self.filings.items()
        }

    def results(self, filing_id: int) -> list[tuple[str, MetricResult]]:
        """The filing's stored metrics as the pipeline computed them: one `MetricResult` per row,
        in the pipeline's own group order."""
        rows = self.rows.get(filing_id, {})
        out: list[tuple[str, MetricResult]] = []
        for group in GROUP_ORDER:
            for name, (row_group, value, unit, audit) in rows.items():
                if row_group == group:
                    out.append((group, MetricResult(name, value, unit, audit)))
        return out

    def statuses(self, filing_id: int) -> dict[str, pillars.PillarStatus]:
        return pillars.pillar_statuses(self.results(filing_id))

    def type_of(self, filing_id: int) -> str | None:
        profile = self.profile_of.get(filing_id)
        return profile.type if profile else None

    def latest_filings(self) -> dict[int, int]:
        """Each asset's latest filing available by :data:`CYCLE_DATE` (PER-01)."""
        latest: dict[int, int] = {}
        for filing_id, filing in self.filings.items():
            available = filing["available_at"]
            if not available or available > CYCLE_DATE:
                continue
            asset_id = filing["asset_id"]
            current = latest.get(asset_id)
            if current is None or (self.filings[current]["available_at"], current) < (
                available,
                filing_id,
            ):
                latest[asset_id] = filing_id
        return latest


def _flat(results: list[tuple[str, MetricResult]]) -> dict[str, float | None]:
    return {f"{group}.{result.name}": result.value for group, result in results}


def _by_group(results: list[tuple[str, MetricResult]]) -> dict[str, list[MetricResult]]:
    out: dict[str, list[MetricResult]] = {}
    for group, result in results:
        out.setdefault(group, []).append(result)
    return out


def _reason(stored: Stored, filing_id: int, metric: str) -> str:
    row = stored.rows.get(filing_id, {}).get(metric)
    if row is None:
        return "not recorded"
    if row[1] is not None:
        return "has a value"
    return str((row[3].get("provenance") or {}).get("empty_reason") or "not recorded")


# -- the pre-T-073 fallback, from the master commit's own source ------------------------------------


def master_agents() -> Any:
    """The master commit's ``agents`` module, imported from its own source.

    The pre-T-073 fallback is no longer in the branch, so the "before" side is the old code itself,
    executed by this same script (AGENTS.md: never explain a change from what the code is supposed
    to do -- run the old code through the same method)."""
    source = subprocess.run(  # noqa: S603 - a fixed `git show`, no shell and no caller input
        ["git", "show", f"{MASTER_COMMIT}:src/fundamental_agent/agents.py"],  # noqa: S607 - from PATH
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    cache = REPO / ".cache" / "t073"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / "master_agents.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location("t073_master_agents", path)
    if spec is None or spec.loader is None:  # pragma: no cover - a local file always loads
        raise RuntimeError("cannot load the master agents module")
    module = importlib.util.module_from_spec(spec)
    sys.modules["t073_master_agents"] = module
    spec.loader.exec_module(module)
    return module


def _master_synthesis_prompt(module: Any) -> str:
    """The master's synthesis prompt template, verbatim from its source."""
    source = (REPO / ".cache" / "t073" / "master_agents.py").read_text()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "_SYNTHESIS_PROMPT" for t in node.targets
        ):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
    raise RuntimeError("the master synthesis prompt was not found")


# -- pillar_status.json -----------------------------------------------------------------------------


def _status_counts(stored: Stored, filing_ids: list[int]) -> dict[str, Any]:
    """Per pillar x company type: how many filings hold each of D1's four statuses."""
    table: dict[str, dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    totals: dict[str, Counter[str]] = defaultdict(Counter)
    for filing_id in filing_ids:
        company_type = stored.type_of(filing_id) or "unknown"
        for pillar, status in stored.statuses(filing_id).items():
            table[pillar][company_type][status.status] += 1
            totals[pillar][status.status] += 1
    return {
        "by_pillar_and_type": {
            pillar: {
                company_type: {status: counts.get(status, 0) for status in pillars.STATUSES}
                for company_type, counts in sorted(types.items())
            }
            for pillar, types in sorted(table.items())
        },
        "by_pillar": {
            pillar: {status: counts.get(status, 0) for status in pillars.STATUSES}
            for pillar, counts in sorted(totals.items())
        },
    }


def cmd_status(args: argparse.Namespace) -> None:
    stored = Stored(args.db)
    latest = stored.latest_filings()
    summary = {
        "db": args.db,
        "engine": ENGINE,
        "cycle_date": CYCLE_DATE,
        "pillars": {pillar: list(metrics) for pillar, metrics in pillars.PILLARS.items()},
        "all_filings": {
            "filings": len(stored.rows),
            **_status_counts(stored, sorted(stored.rows)),
        },
        "latest_filing_per_member": {
            "members": len(latest),
            **_status_counts(stored, sorted(latest.values())),
        },
    }
    Path(args.out).write_text(json.dumps(summary, indent=2) + "\n")
    print(f"wrote {args.out}")
    for key in ("all_filings", "latest_filing_per_member"):
        entry = summary[key]
        print(f"  {key}: {entry['filings'] if 'filings' in entry else entry['members']} filings")
        for pillar, counts in entry["by_pillar"].items():
            print(f"    {pillar}: {counts}")


# -- unscored.json ----------------------------------------------------------------------------------


def _unscored_members(
    stored: Stored, latest: dict[int, int]
) -> tuple[list[dict[str, Any]], Counter[str]]:
    members: list[dict[str, Any]] = []
    by_sector: Counter[str] = Counter()
    by_pillar: Counter[str] = Counter()
    for asset_id, asset in sorted(stored.assets.items(), key=lambda item: item[1]["ticker"]):
        filing_id = latest.get(asset_id)
        if filing_id is None:
            continue
        statuses = stored.statuses(filing_id)
        empty = pillars.empty_pillars(statuses)
        if not empty:
            continue
        by_sector[asset["sector"] or "unknown"] += 1
        by_pillar.update(empty)
        members.append(
            {
                "ticker": asset["ticker"],
                "company_name": asset["company_name"],
                "sector": asset["sector"],
                "type": stored.type_of(filing_id),
                "filing_id": filing_id,
                "form": stored.filings[filing_id]["form"],
                "period_end": stored.filings[filing_id]["period_end"],
                "empty_pillars": {
                    pillar: {
                        metric: _reason(stored, filing_id, metric)
                        for metric in statuses[pillar].metrics
                    }
                    for pillar in empty
                },
            }
        )
    return members, by_sector


def cmd_unscored(args: argparse.Namespace) -> None:
    stored = Stored(args.db)
    latest = stored.latest_filings()
    members, by_sector = _unscored_members(stored, latest)
    max_share = float(CycleSettings.model_fields["unscored_max_share"].default)
    share = len(members) / len(stored.assets) if stored.assets else 0.0
    summary = {
        "db": args.db,
        "cycle_date": CYCLE_DATE,
        "rule": (
            "D1's coverage rule: the score is formed only when every applicable pillar is present; "
            "an applicable `empty` pillar leaves the score empty and the member unscored (T-119). "
            "`absent_by_form` (APP-09's three liquidity ratios, when AssetsCurrent and "
            "LiabilitiesCurrent are not filed) and `not_applicable` pillars do not block it."
        ),
        "members": len(stored.assets),
        "members_with_a_filing_by_the_date": len(latest),
        "members_without_a_score": len(members),
        "share": share,
        "cycle_unscored_max_share": max_share,
        "exceeds_cycle_limit": share > max_share,
        "by_sector": dict(sorted(by_sector.items())),
        "unscored_members": members,
    }
    Path(args.out).write_text(json.dumps(summary, indent=2) + "\n")
    print(f"wrote {args.out}")
    print(
        f"  {len(members)} of {len(stored.assets)} members have no score ({share:.1%}; cycle limit "
        f"{max_share:.0%}; over: {share > max_share})"
    )
    print(f"  by sector: {dict(sorted(by_sector.items()))}")


# -- fallback_distribution.json ---------------------------------------------------------------------


def _distribution(scores: list[float]) -> dict[str, Any]:
    counts = Counter(scores)
    top = counts.most_common(10)
    return {
        "filings": len(scores),
        "distinct_values": len(counts),
        "top_10": [{"score": score, "filings": n} for score, n in top],
        "share_at_the_most_common_value": (top[0][1] / len(scores)) if scores else 0.0,
    }


def cmd_fallback(args: argparse.Namespace) -> None:
    stored = Stored(args.db)
    master = master_agents()
    before: list[float] = []
    after: list[float] = []
    unscored = 0
    for filing_id in sorted(stored.rows):
        results = stored.results(filing_id)
        flat = _flat(results)
        withheld = agents.withheld_metrics(results)
        assessment = master._fallback_assessment(flat, withheld)
        before.append(round(float(assessment.score), 1))
        statuses = stored.statuses(filing_id)
        if pillars.empty_pillars(statuses):
            unscored += 1
            continue  # D1's coverage rule: no score at all, so no fallback value to compare
        after.append(agents._fallback_synthesis(flat, statuses, withheld).assessment.score or 0.0)
    summary = {
        "db": args.db,
        "master_commit": MASTER_COMMIT,
        "note": (
            "The master's own `_fallback_assessment` (loaded from its source) against this branch's "
            "`_fallback_synthesis`, over every filing with metrics-v7 rows. The branch leaves the "
            "filings D1's coverage rule does not score out of the distribution (they have no score "
            "at all); the master scored every filing unconditionally."
        ),
        "filings": len(stored.rows),
        "filings_without_a_score_under_the_branch": unscored,
        "master": _distribution(before),
        "branch": _distribution(after),
    }
    Path(args.out).write_text(json.dumps(summary, indent=2) + "\n")
    print(f"wrote {args.out}")
    for key in ("master", "branch"):
        entry = summary[key]
        print(
            f"  {key}: {entry['filings']} filings, {entry['distinct_values']} distinct values, "
            f"{entry['share_at_the_most_common_value']:.1%} at the most common"
        )
    print(f"  filings without a score under the branch: {unscored}")


# -- weights_sensitivity.json -----------------------------------------------------------------------


def _spearman(a: list[float], b: list[float]) -> float:
    """Spearman's rank correlation, from the ranks (no scipy: `quant` is a leaf)."""
    rank_a = _ranks(a)
    rank_b = _ranks(b)
    if len(a) < 2:  # noqa: PLR2004 - a single member has no correlation
        return 0.0
    mean_a, mean_b = statistics.fmean(rank_a), statistics.fmean(rank_b)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(rank_a, rank_b, strict=True))
    dev = (sum((x - mean_a) ** 2 for x in rank_a) * sum((y - mean_b) ** 2 for y in rank_b)) ** 0.5
    return cov / dev if dev else 0.0


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i], reverse=True)
    ranks = [0.0] * len(values)
    for position, index in enumerate(order, start=1):
        ranks[index] = float(position)
    return ranks


def cmd_weights(args: argparse.Namespace) -> None:
    stored = Stored(args.db)
    latest = stored.latest_filings()
    rows: list[dict[str, Any]] = []
    for asset_id, asset in sorted(stored.assets.items(), key=lambda item: item[1]["ticker"]):
        filing_id = latest.get(asset_id)
        if filing_id is None:
            continue
        results = stored.results(filing_id)
        statuses = stored.statuses(filing_id)
        if pillars.empty_pillars(statuses):
            continue  # no score: nothing to rank
        synthesis = agents._fallback_synthesis(_flat(results), statuses)
        ratings = {
            pillar: entry["rating"]
            for pillar, entry in synthesis.pillars["pillars"].items()
            if "rating" in entry
        }
        rows.append(
            {
                "ticker": asset["ticker"],
                "ratings": ratings,
                "equal": total_from_block(synthesis.pillars, DEFAULT_PILLAR_WEIGHTS),
                "other": total_from_block(synthesis.pillars, OTHER_WEIGHTS),
            }
        )
    equal = [float(r["equal"]) for r in rows]
    other = [float(r["other"]) for r in rows]
    n = len(rows)
    deciles_equal = [min(9, 10 * rank // n) for rank in _ranks(equal)]
    deciles_other = [min(9, 10 * rank // n) for rank in _ranks(other)]
    moved = sum(1 for a, b in zip(deciles_equal, deciles_other, strict=True) if a != b)
    summary = {
        "db": args.db,
        "cycle_date": CYCLE_DATE,
        "rule": (
            "Over this branch's fallback pillar ratings for each member's latest filing: the total "
            "under the equal weights against the original 0.30 / 0.25 / 0.25 / 0.20, mapped "
            "profitability / financial_strength / cash_flow / growth. Members D1 does not score are "
            "excluded -- there is no rating to weigh."
        ),
        "members_ranked": n,
        "equal_weights": DEFAULT_PILLAR_WEIGHTS,
        "other_weights": OTHER_WEIGHTS,
        "spearman": _spearman(equal, other),
        "members_whose_decile_moves": moved,
        "share_whose_decile_moves": (moved / n) if n else 0.0,
        "examples": sorted(
            (
                {
                    "ticker": row["ticker"],
                    "equal": row["equal"],
                    "other": row["other"],
                    "ratings": row["ratings"],
                }
                for row in rows
                if row["equal"] != row["other"]
            ),
            key=lambda r: abs(float(r["other"]) - float(r["equal"])),
            reverse=True,
        )[:10],
    }
    Path(args.out).write_text(json.dumps(summary, indent=2) + "\n")
    print(f"wrote {args.out}")
    print(f"  members ranked: {n}; spearman {summary['spearman']:.4f}")
    print(f"  deciles that move: {moved} ({summary['share_whose_decile_moves']:.1%})")


# -- prompts / briefs -------------------------------------------------------------------------------


def _scored_and_brief(stored: Stored, filing_id: int) -> tuple[list[str], str]:
    """The filing's scored pillars and the brief the orchestrator reads.

    T-076's `_brief` reads only the filing's header, the metric lines and (on a 10-Q) the basis
    paragraph -- all of which the stored rows carry -- so the brief is rebuilt exactly as the
    pipeline rendered it, with an empty `Statements` standing in for the filing's own."""
    results = stored.results(filing_id)
    statuses = stored.statuses(filing_id)
    filing = stored.filings[filing_id]
    asset = stored.assets[filing["asset_id"]]
    ctx = agents.FilingContext(
        ticker=asset["ticker"],
        company_name=asset["company_name"],
        form=filing["form"],
        fiscal_period=filing["fiscal_period"],
        stmts=Statements.from_payload({}),
        period_key=filing["period_end"],
        prior_key=None,
    )
    brief = agents._brief(ctx, _by_group(results), agents.withheld_metrics(results))
    return list(pillars.scored_pillars(statuses)), brief


def cmd_prompts(args: argparse.Namespace) -> None:
    """Master vs branch: the synthesis prompt's size and the prompt-version hash, and the token
    estimate per filing from the rendered prompts (no call)."""
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stored = Stored(args.db)
    master = master_agents()
    master_prompt = _master_synthesis_prompt(master)
    branch_template = agents._SYNTHESIS_PROMPT

    sizes: list[dict[str, Any]] = []
    for filing_id in sorted(stored.rows):
        scored, brief = _scored_and_brief(stored, filing_id)
        synthesis = agents.synthesis_prompt(scored)
        sizes.append(
            {
                "filing_id": filing_id,
                "scored_pillars": scored,
                "brief_chars": len(brief),
                "synthesis_chars": len(synthesis),
                "prompt_chars": len(agents.MASTER_PROMPT) + len(brief) + len(synthesis),
            }
        )
    estimates = [row["prompt_chars"] / 4 for row in sizes]
    summary = {
        "db": args.db,
        "master_commit": MASTER_COMMIT,
        "token_estimate_rule": "characters / 4 (the usual rough ratio for English text)",
        "master_synthesis_prompt_chars": len(master_prompt),
        "branch_synthesis_prompt_chars": len(branch_template),
        "master_prompt_version_hash": master._prompt_version_hash("deepseek-chat"),
        "branch_prompt_version_hash": agents._prompt_version_hash("deepseek-chat"),
        "filings": len(sizes),
        "prompt_chars_mean": statistics.fmean([row["prompt_chars"] for row in sizes])
        if sizes
        else 0.0,
        "tokens_per_filing_mean": statistics.fmean(estimates) if estimates else 0.0,
        "tokens_per_filing_min": min(estimates) if estimates else 0.0,
        "tokens_per_filing_max": max(estimates) if estimates else 0.0,
    }
    (out / "prompt_sizes.json").write_text(
        json.dumps({**summary, "per_filing": sizes}, indent=2) + "\n"
    )
    lines = [
        "# T-073 -- prompt sizes and the prompt-version hash, master vs branch (D5/D6)",
        f"# database: {args.db}",
        f"# master commit: {MASTER_COMMIT}",
        "",
        f"master synthesis prompt: {len(master_prompt)} chars",
        f"branch synthesis prompt: {len(branch_template)} chars",
        f"master prompt-version hash: {master._prompt_version_hash('deepseek-chat')}",
        f"branch prompt-version hash: {agents._prompt_version_hash('deepseek-chat')}",
        "",
        f"filings: {summary['filings']}",
        f"rendered prompt (master prompt + brief + synthesis), mean: "
        f"{summary['prompt_chars_mean']:.0f} chars",
        f"token estimate per filing (chars / 4): mean {summary['tokens_per_filing_mean']:.0f}, "
        f"min {summary['tokens_per_filing_min']:.0f}, max {summary['tokens_per_filing_max']:.0f}",
        "",
        "The brief and the specialist prompts are T-076's and unchanged; only the synthesis prompt "
        "changed, so the hash moves with it. The estimate covers the orchestrator's prompt (the "
        "master prompt, the brief) plus the synthesis prompt: the specialist calls add their own "
        "system prompt (the skill) and a group-sized user prompt on top.",
    ]
    (out / "prompt_sizes.txt").write_text("\n".join(lines) + "\n")
    (out / "prompt_hash.txt").write_text(
        "\n".join(
            [
                "# T-073 -- the prompt-version hash (D5)",
                f"# master commit: {MASTER_COMMIT}",
                f"master: {master._prompt_version_hash('deepseek-chat')}",
                f"branch: {agents._prompt_version_hash('deepseek-chat')}",
                "",
            ]
        )
    )
    print(
        f"wrote {out / 'prompt_sizes.txt'}, {out / 'prompt_sizes.json'}, {out / 'prompt_hash.txt'}"
    )
    print(
        f"  tokens per filing (chars/4): mean {summary['tokens_per_filing_mean']:.0f}, "
        f"min {summary['tokens_per_filing_min']:.0f}, max {summary['tokens_per_filing_max']:.0f}"
    )
    print(
        f"  prompt-version hash master {summary['master_prompt_version_hash'][:12]} -> branch "
        f"{summary['branch_prompt_version_hash'][:12]}"
    )


def cmd_briefs(args: argparse.Namespace) -> None:
    """The rendered prompts (no model) for D5's sample: the master prompt, the brief, and either
    the synthesis prompt (a scored filing) or the coverage rule's own narrative (an unscored one)."""
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stored = Stored(args.db)
    by_ticker = {asset["ticker"]: asset_id for asset_id, asset in stored.assets.items()}
    latest = stored.latest_filings()
    written: list[str] = []
    for ticker in BRIEF_TICKERS:
        asset_id = by_ticker.get(ticker)
        filing_id = latest.get(asset_id) if asset_id is not None else None
        if filing_id is None:
            continue
        scored, brief = _scored_and_brief(stored, filing_id)
        statuses = stored.statuses(filing_id)
        parts = [
            f"# {ticker} -- filing {filing_id}, {stored.filings[filing_id]['form']} "
            f"{stored.filings[filing_id]['period_end']}, type {stored.type_of(filing_id)}",
            f"# pillars: {', '.join(f'{p}={s.status}' for p, s in statuses.items())}",
            "",
            "--- MASTER PROMPT ---",
            agents.MASTER_PROMPT,
            "",
            "--- BRIEF ---",
            brief,
            "",
        ]
        if pillars.empty_pillars(statuses):
            parts += [
                "--- NO SYNTHESIS PROMPT: D1's coverage rule ---",
                agents.unscored_narrative(statuses),
            ]
        else:
            parts += ["--- SYNTHESIS PROMPT ---", agents.synthesis_prompt(scored)]
        path = out / f"{ticker}.txt"
        path.write_text("\n".join(parts) + "\n")
        written.append(str(path))
    print(f"wrote {len(written)} briefs into {out}: {', '.join(Path(w).name for w in written)}")


# -- all --------------------------------------------------------------------------------------------


def cmd_all(args: argparse.Namespace) -> None:
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, handler in (
        ("pillar_status.json", cmd_status),
        ("unscored.json", cmd_unscored),
        ("fallback_distribution.json", cmd_fallback),
        ("weights_sensitivity.json", cmd_weights),
    ):
        handler(argparse.Namespace(db=args.db, out=str(out / name)))
    cmd_prompts(argparse.Namespace(db=args.db, out_dir=str(out)))
    cmd_briefs(argparse.Namespace(db=args.db, out_dir=str(out / "briefs")))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, handler in (
        ("status", cmd_status),
        ("unscored", cmd_unscored),
        ("fallback", cmd_fallback),
        ("weights", cmd_weights),
    ):
        sub = subparsers.add_parser(name, help=handler.__doc__)
        sub.add_argument("--db", required=True, help="the task's scratch copy (read-only)")
        sub.add_argument("--out", required=True)
        sub.set_defaults(func=handler)
    for name, handler in (("prompts", cmd_prompts), ("briefs", cmd_briefs)):
        sub = subparsers.add_parser(name, help=handler.__doc__)
        sub.add_argument("--db", required=True)
        sub.add_argument("--out-dir", required=True)
        sub.set_defaults(func=handler)
    p_all = subparsers.add_parser("all", help="every measurement, into --out-dir")
    p_all.add_argument("--db", required=True)
    p_all.add_argument("--out-dir", required=True)
    p_all.set_defaults(func=cmd_all)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
