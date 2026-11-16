#!/usr/bin/env python3
"""V6: every command of the plan's V6 list, run or N/A, on the pilot database (T-143 part A).

    .venv/bin/python scripts/pilot_a/v6_sweep.py \
        --db /workspaces/thesis/pilot_a2/financial.db \
        --out-dir /workspaces/thesis/scratch/t143a/out \
        --json /workspaces/thesis/scratch/t143a/out/p2b_v6_sweep.json \
        --markdown /workspaces/thesis/scratch/t143a/out/p2b_v6_sweep.md

The plan's V6 ("the earlier decisions' evidence, re-measured on the fresh database") names the
``verify_t*.py`` scripts; this runner runs **every** one of their sub-commands that can run on
one database, and records the others as **N/A** with the table each one needs -- the plan says an
N/A is never a pass, so the table says what is missing, not "fine".

Two kinds of N/A:

* the command needs a **control database** (the ``before``/``after`` pair the earlier task measured
  on) -- the copies' databases are not in this checkout, only their outputs;
* the command reads the **cycle's outputs** (``score_snapshot``, ``cycle_ranking``,
  ``latest_fundamental_rows``) -- part A runs no ``cycle`` and writes no FUNDAMENTAL row, so the
  plan moves those to part B.  Three of them are still *run* here, to show that their output is
  empty by design rather than silently skipped.

Read-only: every command opens the database with ``connect_ro``.  Nothing is written anywhere.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

#: The default timeout per command (seconds).  The two commands that timed out at 900 s on the
#: 9p-mounted copy are fast on local disk (one Snapshot load is ~11 s there).
TIMEOUT = 2400

#: The commands that run on one database: (id, the table it needs, the command's argv).
RUNS: list[tuple[str, str, list[str]]] = [
    (
        "verify_t071b/vetoes",
        "the per-date, per-rule, per-sector HARD hits (the vetoes' own table)",
        [
            "scripts/verify_t071b.py",
            "vetoes",
            "--side",
            "before",
            "--db",
            "{db}",
            "--out",
            "{out}/p2b_v6_verify_t071b_vetoes.json",
        ],
    ),
    (
        "verify_t071b/d13",
        "the D-13 utilities' table (the 5% check)",
        [
            "scripts/verify_t071b.py",
            "d13",
            "--db",
            "{db}",
            "--out",
            "{out}/p2b_v6_verify_t071b_d13.json",
        ],
    ),
    (
        "verify_t071b/debt",
        "the debt measure's population (the understated share by filing and company)",
        [
            "scripts/verify_t071b.py",
            "debt",
            "--db",
            "{db}",
            "--out",
            "{out}/p2b_v6_verify_t071b_debt.json",
        ],
    ),
    (
        "verify_t071c/multi-class",
        "the multi-class measure (T-071 part 2b)",
        [
            "scripts/verify_t071c.py",
            "multi-class",
            "--db",
            "{db}",
            "--out",
            "{out}/p2b_v6_verify_t071c_multi-class.json",
        ],
    ),
    (
        "verify_t071c/reit-capex",
        "the D-14 REIT capex table",
        [
            "scripts/verify_t071c.py",
            "reit-capex",
            "--db",
            "{db}",
            "--out",
            "{out}/p2b_v6_verify_t071c_reit-capex.json",
        ],
    ),
    (
        "verify_t071c/negative-fcf",
        "NEGATIVE_FCF's hits by sector and the REITs its exemption takes out (G1)",
        [
            "scripts/verify_t071c.py",
            "negative-fcf",
            "--db",
            "{db}",
            "--side",
            "after",
            "--out",
            "{out}/p2b_v6_verify_t071c_negative-fcf.json",
        ],
    ),
    (
        "verify_t071c/d14",
        "the D-14 table, decision-ready (the REITs vetoed by NEGATIVE_FCF, their capex split)",
        [
            "scripts/verify_t071c.py",
            "d14",
            "--db",
            "{db}",
            "--out",
            "{out}/p2b_v6_verify_t071c_d14.json",
        ],
    ),
    (
        "verify_t071c/book-equity",
        "D1's members filing PreferredStockValue > 0 and their share of parent equity",
        [
            "scripts/verify_t071c.py",
            "book-equity",
            "--db",
            "{db}",
            "--out",
            "{out}/p2b_v6_verify_t071c_book-equity.json",
        ],
    ),
    (
        "verify_t156/fidelity",
        "E1's both-statements agreement and E4's identity agreement",
        [
            "scripts/verify_t156.py",
            "fidelity",
            "--after",
            "{db}",
            "--out",
            "{out}/p2b_v6_verify_t156_fidelity.json",
        ],
    ),
    (
        "verify_t073/status",
        "the metric presence per pillar group",
        [
            "scripts/verify_t073.py",
            "status",
            "--db",
            "{db}",
            "--out",
            "{out}/p2b_v6_verify_t073_status.json",
        ],
    ),
    (
        "verify_t073/unscored",
        "D1's coverage rule over the metrics (the unscored members)",
        [
            "scripts/verify_t073.py",
            "unscored",
            "--db",
            "{db}",
            "--out",
            "{out}/p2b_v6_verify_t073_unscored.json",
        ],
    ),
    (
        "verify_t073/fallback",
        "the fallback distribution over every filing with metrics (no LLM call)",
        [
            "scripts/verify_t073.py",
            "fallback",
            "--db",
            "{db}",
            "--out",
            "{out}/p2b_v6_verify_t073_fallback.json",
        ],
    ),
    (
        "verify_t073/weights",
        "the weights' sensitivity (the spearman correlation and the decile moves)",
        [
            "scripts/verify_t073.py",
            "weights",
            "--db",
            "{db}",
            "--out",
            "{out}/p2b_v6_verify_t073_weights.json",
        ],
    ),
    (
        "verify_t151a/validation",
        "the face cases against the filers' own totals",
        [
            "scripts/verify_t151a.py",
            "validation",
            "--db",
            "{db}",
            "--out-txt",
            "{out}/p2b_v6_verify_t151a_validation.txt",
        ],
    ),
    (
        "verify_t151a/validate-cf",
        "the filled sums against companyfacts",
        [
            "scripts/verify_t151a.py",
            "validate-cf",
            "--db",
            "{db}",
            "--out-txt",
            "{out}/p2b_v6_verify_t151a_validate-cf.txt",
            "--out-json",
            "{out}/p2b_v6_verify_t151a_validate-cf.json",
        ],
    ),
    (
        "verify_t151a/face-cases",
        "the face cases, classified",
        [
            "scripts/verify_t151a.py",
            "face-cases",
            "--db",
            "{db}",
            "--out-txt",
            "{out}/p2b_v6_verify_t151a_face-cases.txt",
        ],
    ),
    (
        "verify_t151a/refusals",
        "the refused sums, by outside concept",
        [
            "scripts/verify_t151a.py",
            "refusals",
            "--db",
            "{db}",
            "--out-txt",
            "{out}/p2b_v6_verify_t151a_refusals.txt",
        ],
    ),
    (
        "verify_t151a/capex",
        "the net-element filings (C3)",
        [
            "scripts/verify_t151a.py",
            "capex",
            "--after",
            "{db}",
            "--out-txt",
            "{out}/p2b_v6_verify_t151a_capex.txt",
        ],
    ),
    (
        "verify_t151a/labels",
        "the labelling rule on the stored metadata (C4)",
        [
            "scripts/verify_t151a.py",
            "labels",
            "--db",
            "{db}",
            "--out-txt",
            "{out}/p2b_v6_verify_t151a_labels.txt",
        ],
    ),
    (
        "verify_t151b/roa",
        "the ROA measure's population",
        [
            "scripts/verify_t151b.py",
            "roa",
            "--db",
            "{db}",
            "--engine",
            "{engine}",
            "--out",
            "{out}/p2b_v6_verify_t151b_roa.json",
        ],
    ),
    (
        "verify_t155/classify",
        "the current members' CIK successions (APP-10b) -- the register's own classification",
        [
            "scripts/verify_t155.py",
            "classify",
            "--out",
            "{out}/p2b_v6_verify_t155_classify",
            "--cache",
            "{cache}",
            "--fetch",
        ],
    ),
    (
        "verify_t155/used",
        "which of the register's successions the pipeline used",
        [
            "scripts/verify_t155.py",
            "used",
            "--out",
            "{out}/p2b_v6_verify_t155_used",
            "--cache",
            "{cache}",
            "--fetch",
        ],
    ),
    (
        "verify_t155/split",
        "the successions' split between the members",
        [
            "scripts/verify_t155.py",
            "split",
            "--out",
            "{out}/p2b_v6_verify_t155_split.json",
            "--cache",
            "{cache}",
            "--fetch",
        ],
    ),
    (
        "verify_t151c/provenance",
        "the provenance rows and the empty rows' reasons",
        [
            "scripts/verify_t151c.py",
            "provenance",
            "--db",
            "{db}",
            "--engine",
            "{engine}",
            "--out",
            "{out}/p2b_v6_verify_t151c_provenance.json",
        ],
    ),
    (
        "verify_t151c/reasons",
        "the empty reasons, by metric (H7)",
        [
            "scripts/verify_t151c.py",
            "reasons",
            "--db",
            "{db}",
            "--engine",
            "{engine}",
            "--out",
            "{out}/p2b_v6_verify_t151c_reasons.json",
        ],
    ),
    (
        "verify_t071c/value-measures",
        "the VALORIZATION measures (EMPTY BY DESIGN: it reads the cycle's scores)",
        [
            "scripts/verify_t071c.py",
            "value-measures",
            "--db",
            "{db}",
            "--out",
            "{out}/p2b_v6_verify_t071c_value-measures.json",
        ],
    ),
    (
        "verify_t071c/dq02",
        "DQ-02's comparison (EMPTY BY DESIGN: it reads FUNDAMENTAL and the scores)",
        [
            "scripts/verify_t071c.py",
            "dq02",
            "--db",
            "{db}",
            "--out",
            "{out}/p2b_v6_verify_t071c_dq02.json",
        ],
    ),
    (
        "verify_t071c/scores",
        "the stored scores (EMPTY BY DESIGN: part A writes none)",
        [
            "scripts/verify_t071c.py",
            "scores",
            "--db",
            "{db}",
            "--side",
            "after",
            "--out",
            "{out}/p2b_v6_verify_t071c_scores.json",
        ],
    ),
]

#: The commands that do not run here, with the table each one needs.  ``kind`` is ``control`` (a
#: before/after pair) or ``cycle`` (the cycle's outputs, which part A does not write) or ``file``
#: (a tracked input this checkout does not hold).
NA: list[tuple[str, str, str, str]] = [
    (
        "verify_t071b/vetoes (the after side)",
        "the control's per-date result (master's code on the control database)",
        "control",
        "the before/after pair the earlier task measured on; the copies' databases are not in this "
        "checkout, only their outputs",
    ),
    (
        "verify_t071b/neg-equity",
        "D1/D2's companies and the ROE rows D1 stops quarantining (D-04)",
        "control",
        "the before/after pair",
    ),
    (
        "verify_t071b/d04",
        "D2's net debt/EBITDA x coverage grid, by sector (D-04)",
        "control",
        "the before/after pair",
    ),
    (
        "verify_t071b/d14",
        "the D-14 REIT table (the before/after reading)",
        "control",
        "the before/after pair",
    ),
    (
        "verify_t071b/liquidity",
        "LIQUIDITY_DISTRESS's reading, before vs after (D5)",
        "control",
        "the before/after pair",
    ),
    (
        "verify_t071b/all",
        "every measurement above, before vs after",
        "control",
        "the before/after pair",
    ),
    (
        "verify_t071c/compare",
        "the stored run vs the branch's run",
        "control",
        "the before/after pair",
    ),
    (
        "verify_t071c/blend",
        "the blend's components and the stored run's veto rules",
        "cycle",
        "the cycle's outputs (cycle_ranking); the command also DELETES rows, so it never runs "
        "against a pilot database",
    ),
    (
        "verify_t071c/coverage",
        "the applicable/present/empty counts per measure, per date and company type",
        "cycle",
        "the cycle's scores (score_snapshot); part A writes none, so the table would be empty",
    ),
    (
        "verify_t156/classes",
        "the class table, before vs after, per date",
        "control",
        "the before/after pair",
    ),
    (
        "verify_t156/metrics",
        "the coverage before vs after, VALORIZATION by company type",
        "control",
        "the before/after pair",
    ),
    (
        "verify_t156/valorization",
        "VALORIZATION v2 before vs after",
        "control",
        "the before/after pair",
    ),
    (
        "verify_t156/prompt-hash",
        "how many filings' scored input changes",
        "control",
        "the before/after pair",
    ),
    (
        "verify_t156/recoveries",
        "the EBITDA-coverage recoveries E1-E3",
        "control",
        "the before/after pair",
    ),
    (
        "verify_t073/prompts",
        "the prompt sizes and the prompt-version hash (the prompt artifacts)",
        "cycle",
        "part A writes no prompt; the artifact is measured for part B",
    ),
    (
        "verify_t073/briefs",
        "the rendered briefs (the scored and the unscored filing's)",
        "cycle",
        "part A writes no prompt; the artifact is measured for part B",
    ),
    (
        "verify_t151a/changes",
        "the control vs the branch's changes",
        "control",
        "the before/after pair",
    ),
    (
        "verify_t151a/audit-diff",
        "the audit keys that changed",
        "control",
        "the before/after pair",
    ),
    (
        "verify_t151a/cf-example",
        "one disagreement in full (the filed facts against the stored sum)",
        "cycle",
        "runnable, but it needs one accession chosen from a disagreement; the disagreements are "
        "opened by validate-cf (run above) and the example is opened there too",
    ),
    (
        "verify_t151b/changes",
        "the control vs the branch's changes",
        "control",
        "the before/after pair",
    ),
    (
        "verify_t151b/flags",
        "the flags' before vs after",
        "control",
        "the before/after pair",
    ),
    (
        "verify_t151b/counts",
        "the counts' before vs after",
        "control",
        "the before/after pair",
    ),
    (
        "verify_t151b/audit-diff",
        "the audit keys that changed",
        "control",
        "the before/after pair",
    ),
    (
        "verify_t155/measure",
        "the affected members before vs after",
        "control",
        "the before/after pair",
    ),
    (
        "verify_t155/check",
        "the successions' check against EDGAR",
        "cycle",
        "it writes to a scratch database and fetches EDGAR; the identity check is V1's (done)",
    ),
    (
        "verify_t155/run",
        "the pipeline's run over the affected members",
        "cycle",
        "it writes to a scratch database; part A writes no cycle outputs",
    ),
]


def _run_one(argv: list[str], cwd: Path, log: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    """One command: its exit code, its seconds, and its output (in the log file)."""
    cmd = [cfg["python"], *argv]
    started = time.time()
    with log.open("w") as handle:
        handle.write(f"$ {' '.join(cmd)}\n\n")
        handle.flush()
        try:
            proc = subprocess.run(  # noqa: S603 - the argv is this script's own table
                cmd,
                cwd=cwd,
                stdout=handle,
                stderr=subprocess.STDOUT,
                timeout=cfg["timeout"],
                env=cfg["env"],
                check=False,
            )
            code = proc.returncode
        except subprocess.TimeoutExpired:
            code = 124
    return {
        "command": " ".join(cmd),
        "exit": code,
        "seconds": round(time.time() - started, 1),
        "log": str(log),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--json", required=True)
    parser.add_argument("--markdown", required=True)
    parser.add_argument("--python", default=".venv/bin/python")
    parser.add_argument("--timeout", type=int, default=TIMEOUT)
    parser.add_argument("--engine", default="metrics-v8")
    parser.add_argument("--only", default="", help="comma-separated ids to run (default: all)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[2]
    only = {part.strip() for part in args.only.split(",") if part.strip()}
    cfg = {
        "python": args.python,
        "timeout": args.timeout,
        # the parent's environment (the proxies and the CA bundle) plus the pilot's own settings
        "env": {
            **os.environ,
            "PYTHONPATH": f"{repo / 'src'}",
            "SEC_USER_AGENT": "research@example.com",
            "SEC_XCHECK_CACHE": str(out_dir.parent / "sec_cache"),
            "KG_UNIVERSE_DB": "/workspaces/thesis/data/universe_history_gics.db",
        },
    }

    results: list[dict[str, Any]] = []
    for ident, table, argv in RUNS:
        if only and ident not in only:
            continue
        filled = [
            a.format(
                db=args.db,
                out=str(out_dir),
                engine=args.engine,
                cache=str(out_dir.parent / "sec_cache"),
            )
            for a in argv
        ]
        log = out_dir / (ident.replace("/", "_") + ".log")
        entry: dict[str, Any] = {"id": ident, "table": table, "status": "run"}
        entry.update(_run_one(filled, repo, log, cfg))
        entry["outputs"] = [
            str(out_dir / a.split("/")[-1])
            for a in filled
            if a.startswith("{out}") and a.endswith((".json", ".txt"))
        ]
        results.append(entry)
        print(
            f"=== {ident} exit={entry['exit']} seconds={entry['seconds']}",
            flush=True,
        )

    for ident, table, kind, reason in NA:
        results.append(
            {"id": ident, "table": table, "status": "N/A", "kind": kind, "reason": reason}
        )

    doc = {
        "db": args.db,
        "timeout_seconds": args.timeout,
        "ran": sum(1 for r in results if r["status"] == "run"),
        "na": sum(1 for r in results if r["status"] == "N/A"),
        "failed": [r["id"] for r in results if r["status"] == "run" and r["exit"] != 0],
        "results": results,
    }
    Path(args.json).write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")

    lines = [
        "# V6 on pilot_a2: every command of the plan's list, run or N/A",
        "",
        f"database: `{args.db}` (read-only) · timeout per command: {args.timeout} s",
        "",
        "| the command | the table it needs | status | the outcome |",
        "|---|---|---|---|",
    ]
    for entry in results:
        if entry["status"] == "N/A":
            status = f"N/A ({entry['kind']})"
            outcome = entry["reason"]
        else:
            status = "run"
            outcome = f"exit {entry['exit']}, {entry['seconds']} s"
        lines.append(f"| `{entry['id']}` | {entry['table']} | {status} | {outcome} |")
    lines.append("")
    lines.append(
        f"**{doc['ran']} run · {doc['na']} N/A** "
        f"(an N/A is never a pass; the reason names what is missing)."
    )
    Path(args.markdown).write_text("\n".join(lines) + "\n")
    print(json.dumps({k: v for k, v in doc.items() if k != "results"}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
