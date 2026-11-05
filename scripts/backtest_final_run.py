"""Run one final-round batch on a fresh copy per window (plan §6).

    uv run python scripts/backtest_final_run.py --mode z0|stagea|stageb|staged \
        --out /workspaces/thesis/scratch/final/out/final_round [--keep-db]

Each job copies the window's rule-filled template, runs the backtest, collects the JSON and
deletes the copy unless ``--keep-db`` is given (the Z0 / B0 controls keep theirs for the
identity check). FINAL's six copies are always kept. Only aggregates reach the JSON.
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

FINAL = Path("/workspaces/thesis/scratch/final")
OUT = FINAL / "out/final_round"
TEMPLATES = FINAL / "out/v3x"
REPO = Path("/workspaces/thesis/scratch/bt/final")
UNIVERSE = "/workspaces/thesis/data/universe_history_gics.db"
PARAMS = "src/cycle/backtest/params.frozen.toml"
SPY = "/workspaces/thesis/scratch/final/data/spy/spy_tr.json"
MARKET = "/workspaces/thesis/scratch/final/out/a2/market_ew.json"
ARMS = "MARKOWITZ_BASE,TOPN_MV_NOGUARD,TOPN_MV_GUARD_NO_NLP,MARKOWITZ_BASE_GUARD"
WINDOWS = ("P1", "P3", "P2", "H1", "H2", "H3")
ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin:/home/vscode/.local/bin", "HOME": "/home/vscode"}
MAX_WORKERS = 4
MIN_FREE = 25 * 1024**3
KEEP_FINAL = {"FINAL", "FINAL_D20"}

STAGE_A: dict[str, tuple[str, str]] = {
    "Z0": ("1,3,4", "fixed"),
    "Z1": ("1", "fixed"),
    "Z2": ("1,4", "fixed"),
    "Z3": ("1,3", "trailing"),
    "Z4": ("1,3,4", "trailing"),
}
STAGE_B = {
    "B0": "minvar",
    "B1": "ew",
    "B2": "vt",
    "B3": "maxdiv",
    "B4": "hrp",
    "B5": "mincvar",
    "B6": "ensemble",
}


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"missing resolution file {path}")
    return dict(json.loads(path.read_text()))


def _jobs(  # noqa: C901, PLR0911, PLR0912 - one dispatch per named batch
    mode: str, out: Path
) -> list[tuple[str, str, str, str, str, float]]:
    if mode == "z0":
        layers, vol = STAGE_A["Z0"]
        return [(c, w, layers, vol, "minvar", 10.0) for c, _ in (("Z0", ""),) for w in WINDOWS]
    if mode == "stagea":
        return [
            (config, window, layers, vol, "minvar", 10.0)
            for window in WINDOWS
            for config, (layers, vol) in STAGE_A.items()
        ]
    if mode == "stageb":
        star = _read(out / "a_star.json")
        layers, vol = star["guard_layers"], star["vol_target"]
        return [
            (config, window, layers, vol, method, 10.0)
            for window in WINDOWS
            for config, method in STAGE_B.items()
        ]
    if mode == "stageb_control":
        star = _read(out / "a_star.json")
        return [("B0", "P1", star["guard_layers"], star["vol_target"], "minvar", 10.0)]
    if mode == "stageb_rest":
        star = _read(out / "a_star.json")
        return [
            (config, window, star["guard_layers"], star["vol_target"], method, 10.0)
            for window in WINDOWS
            for config, method in STAGE_B.items()
            if config != "B0"
        ]
    if mode == "stageb_all":
        star = _read(out / "a_star.json")
        return [
            (config, window, star["guard_layers"], star["vol_target"], method, 10.0)
            for window in WINDOWS
            for config, method in STAGE_B.items()
        ]
    if mode == "stagea_rest":
        return [
            (config, window, layers, vol, "minvar", 10.0)
            for window in WINDOWS
            for config, (layers, vol) in STAGE_A.items()
            if config != "Z0"
        ]
    if mode == "stagea_all":
        return [
            (config, window, layers, vol, "minvar", 10.0)
            for window in WINDOWS
            for config, (layers, vol) in STAGE_A.items()
        ]
    if mode == "stageb_select":
        star = _read(out / "a_star.json")
        final = _read(out / "final.json")
        return [
            (
                "FINAL",
                window,
                star["guard_layers"],
                star["vol_target"],
                final["satellite_weighting"],
                10.0,
            )
            for window in WINDOWS
        ]
    if mode == "stageb_verify":
        star = _read(out / "a_star.json")
        final = _read(out / "final.json")
        return [
            (
                "FINAL",
                window,
                star["guard_layers"],
                star["vol_target"],
                final["satellite_weighting"],
                10.0,
            )
            for window in WINDOWS
            if not (out / "runs" / f"FINAL_{window}.json").is_file()
        ]
    if mode == "stageb_rerun":
        star = _read(out / "a_star.json")
        final = _read(out / "final.json")
        return [
            (
                "FINAL",
                window,
                star["guard_layers"],
                star["vol_target"],
                final["satellite_weighting"],
                10.0,
            )
            for window in WINDOWS
        ]
    if mode == "stagec":
        return []
    if mode == "staged":
        star = _read(out / "a_star.json")
        final = _read(out / "final.json")
        return [
            (
                "FINAL_D20",
                window,
                star["guard_layers"],
                star["vol_target"],
                final["satellite_weighting"],
                20.0,
            )
            for window in WINDOWS
        ]
    raise SystemExit(f"unknown mode {mode!r}")


def _job(job: tuple[str, str, str, str, str, float], keep_db: bool) -> dict[str, Any]:
    config, window, layers, vol, method, cost = job
    runs = OUT / "runs"
    work = runs / f"{config}_{window}.db"
    if work.exists():
        raise SystemExit(f"{work} already exists: refusing to reuse a copy")
    shutil.copyfile(TEMPLATES / f"template_{window}.db", work)
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
        layers,
        "--vol-target",
        vol,
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
    keep = keep_db or config in KEEP_FINAL
    if not keep:
        work.unlink()
    return {
        "config": config,
        "window": window,
        "seconds": elapsed,
        "json": str(collect),
        "exit": 0,
        "db_kept": str(work) if keep else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True)
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--keep-db", action="store_true")
    args = parser.parse_args()
    out = Path(args.out).expanduser().resolve()
    (out / "runs").mkdir(parents=True, exist_ok=True)
    jobs = [
        job
        for job in _jobs(args.mode, out)
        if not (out / "runs" / f"{job[0]}_{job[1]}.json").is_file()
    ]
    results: list[dict[str, Any]] = []
    for start in range(0, len(jobs), MAX_WORKERS):
        free = shutil.disk_usage(out).free
        if free < MIN_FREE:
            print(json.dumps({"stop": "free space below 25 GB", "free": free}), flush=True)
            return 2
        batch = jobs[start : start + MAX_WORKERS]
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for future in concurrent.futures.as_completed(
                [pool.submit(_job, job, args.keep_db) for job in batch]
            ):
                result = future.result()
                results.append(result)
                print(json.dumps(result), flush=True)
                if result.get("exit") != 0:
                    return 1
    (out / f"run_index_{args.mode}.json").write_text(json.dumps(results, indent=1) + "\n")
    print(json.dumps({"done": len(results), "mode": args.mode}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
