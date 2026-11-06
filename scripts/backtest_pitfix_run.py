"""Corrective PIT-panel run: FINAL (B5) at 10/20 bp and B0 (Y2), six windows.

    uv run python scripts/backtest_pitfix_run.py [--windows P1,H1,...] [--keep-db]

Each job copies its window's fixed-panel template, runs the backtest with the final round's
frozen flags, collects the aggregates and keeps the run copy (the exit analysis reads its
weights). Never reuses an existing run copy.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

OUT = Path("/workspaces/thesis/scratch/final/out/pitfix")
TEMPLATES = OUT / "templates"
REPO = Path("/workspaces/pfa-pitfix")
UNIVERSE = "/workspaces/thesis/data/universe_history_gics.db"
PARAMS = "src/cycle/backtest/params.frozen.toml"
SPY = "/workspaces/thesis/scratch/final/data/spy/spy_tr.json"
MARKET = "/workspaces/thesis/scratch/final/out/a2/market_ew.json"
ARMS = "MARKOWITZ_BASE,TOPN_MV_NOGUARD,TOPN_MV_GUARD_NO_NLP,MARKOWITZ_BASE_GUARD"
WINDOWS = ("P1", "H1", "P3", "H2", "P2", "H3")
ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin:/home/vscode/.local/bin", "HOME": "/home/vscode"}
MAX_WORKERS = 4
MIN_FREE = 60 * 1024**3

CONFIGS: dict[str, tuple[str, float]] = {
    "PITFIX_FINAL": ("mincvar", 10.0),
    "PITFIX_FINAL_D20": ("mincvar", 20.0),
    "PITFIX_B0": ("minvar", 10.0),
}


def _jobs(wanted: set[str] | None, keep_db: bool) -> list[tuple[str, str, str, float, bool]]:
    return [
        (config, window, method, cost, keep_db)
        for config, (method, cost) in CONFIGS.items()
        for window in WINDOWS
        if wanted is None or window in wanted
    ]


def _job(job: tuple[str, str, str, float, bool]) -> dict[str, Any]:
    config, window, method, cost, keep_db = job
    runs = OUT / "runs"
    work = runs / f"{config}_{window}.db"
    if work.exists():
        raise SystemExit(f"{work} already exists: refusing to reuse a copy")
    shutil.copyfile(TEMPLATES / f"template_{window}_pitfix.db", work)
    work.chmod(0o644)
    log = OUT / "logs" / f"{config}_{window}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "uv",
        "run",
        "python",
        "-m",
        "cycle.backtest",
        "run",
        "--window",
        window,
        "--db",
        str(work),
        "--universe-db",
        UNIVERSE,
        "--params",
        PARAMS,
        "--warmup-steps",
        "12",
        "--arms",
        ARMS,
        "--blend",
        "B0",
        "--tilt",
        "T0",
        "--market-series",
        MARKET,
        "--core-weight",
        "0.5",
        "--trend-filter",
        "on",
        "--core-series",
        SPY,
        "--pool-m",
        "60",
        "--core-source",
        "spy",
        "--filter-exit",
        "0.5",
        "--stop-rule",
        "bp4",
        "--satellite-weighting",
        method,
        "--guard-layers",
        "1,3,4",
        "--vol-target",
        "fixed",
        "--cost-bps",
        str(cost),
    ]
    started = time.monotonic()
    with log.open("w") as handle:
        proc = subprocess.run(  # noqa: S603
            command,
            cwd=REPO,
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=ENV,
            check=False,
        )
    elapsed = round(time.monotonic() - started, 1)
    if proc.returncode != 0:
        return {
            "config": config,
            "window": window,
            "exit": proc.returncode,
            "log": str(log),
            "seconds": elapsed,
        }
    collect = runs / f"{config}_{window}.json"
    subprocess.run(  # noqa: S603
        [  # noqa: S607 - the interpreter and script are this repository's
            "uv",
            "run",
            "python",
            "scripts/backtest_final_collect.py",
            str(work),
            str(collect),
        ],
        cwd=REPO,
        check=True,
        env=ENV,
    )
    if not keep_db:
        work.unlink()
    return {
        "config": config,
        "window": window,
        "seconds": elapsed,
        "json": str(collect),
        "exit": 0,
        "db_kept": str(work) if keep_db else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--windows", default=None, help="optional comma-separated subset")
    parser.add_argument("--keep-db", action="store_true")
    args = parser.parse_args()
    (OUT / "runs").mkdir(parents=True, exist_ok=True)
    wanted = None if args.windows is None else set(args.windows.split(","))
    jobs = [
        job
        for job in _jobs(wanted, args.keep_db)
        if not (OUT / "runs" / f"{job[0]}_{job[1]}.json").is_file()
    ]
    results: list[dict[str, Any]] = []
    for start in range(0, len(jobs), MAX_WORKERS):
        free = shutil.disk_usage(OUT).free
        if free < MIN_FREE:
            print(json.dumps({"stop": "free space below 60 GB", "free": free}), flush=True)
            return 2
        batch = jobs[start : start + MAX_WORKERS]
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for future in concurrent.futures.as_completed(
                [pool.submit(_job, job) for job in batch]
            ):
                result = future.result()
                results.append(result)
                print(json.dumps(result), flush=True)
                if result.get("exit") != 0:
                    return 1
    (OUT / "run_index.json").write_text(json.dumps(results, indent=1) + "\n")
    print(json.dumps({"done": len(results)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
