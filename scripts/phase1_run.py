"""Phase 1 run matrix (plan §2): one fresh copy of a corrected window template per run.

    uv run python scripts/phase1_run.py --block R1 [--workers 4] [--out DIR]

Every job copies ``template_<window>_pitfix.db`` (C1 uses ``template_C1_pitfix.db``), runs
``python -m cycle.backtest run`` with the block's parameters, collects the aggregates with
``scripts/backtest_final_collect.py``, records the template/params/output sha256 in
``runs_manifest.csv`` and deletes the copy unless it is a FINAL run (the six windows and the
three C1 runs keep theirs).  A run directory that already exists is refused: the phase never
overwrites an existing run.  The production databases are never opened here.

No per-asset price or return is written: the collector stores portfolio aggregates, the
manifest stores hashes and parameters, and the logs are the driver's own standard output.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = Path("/workspaces/thesis/scratch/final/out/pitfix/templates")
DEFAULT_OUT = Path("/workspaces/thesis/scratch/final/out/phase1")
UNIVERSE = "/workspaces/thesis/data/universe_history_gics.db"
PARAMS = "src/cycle/backtest/params.frozen.toml"
SPY = "/workspaces/thesis/scratch/final/data/spy/spy_tr.json"
MARKET = "/workspaces/thesis/scratch/final/out/a2/market_ew.json"
W6 = ("P1", "H1", "P3", "H2", "P2", "H3")
ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin:/home/vscode/.local/bin",
    "HOME": "/home/vscode",
}
ARMS4 = "MARKOWITZ_BASE,TOPN_MV_NOGUARD,TOPN_MV_GUARD_NO_NLP,MARKOWITZ_BASE_GUARD"
ARMS3 = "MARKOWITZ_BASE,TOPN_MV_NOGUARD,TOPN_MV_GUARD_NO_NLP"
FINAL_WINDOWS = set(W6) | {"C1"}
BASE_COST = 10.0
UV = "/usr/local/bin/uv"

#: The v2 family's fixed flags (everything else is a configuration override).
V2 = {
    "blend": "B0",
    "tilt": "T0",
    "core_weight": "0.5",
    "trend_filter": "on",
    "pool_m": "60",
    "core_source": "spy",
    "filter_exit": "0.5",
    "filter_frequency": "daily",
    "stop_rule": "bp4",
    "satellite_weighting": "minvar",
    "guard_layers": "1,3,4",
    "vol_target": "fixed",
    "arms": ARMS4,
}
#: The v1 family of the CHAMPION grid (no core, no filter, pool 30, K-L stop).
V1 = {
    "blend": "B0",
    "tilt": "T0",
    "core_weight": None,
    "trend_filter": "off",
    "pool_m": "30",
    "core_source": "spy",
    "filter_exit": "1.0",
    "stop_rule": "kl",
    "satellite_weighting": "minvar",
    "guard_layers": "1,3,4",
    "vol_target": "fixed",
    "arms": ARMS3,
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
STAGE_A = {
    "Z1": ("1", "fixed"),
    "Z2": ("1,4", "fixed"),
    "Z3": ("1,3", "trailing"),
    "Z4": ("1,3,4", "trailing"),
}
V_GRID = {
    "V0": (None, "off"),
    "V1": (None, "on"),
    "V2": ("0.5", "off"),
    "V3": ("0.5", "on"),
    "V4": ("0.7", "off"),
    "V5": ("0.7", "on"),
}
X_GRID = {
    "X1": ("30", "spy", "0.5"),
    "X2": ("30", "base", "1.0"),
    "X3": ("30", "base", "0.5"),
    "X4": ("60", "spy", "1.0"),
    "X5": ("60", "spy", "0.5"),
    "X6": ("60", "base", "1.0"),
    "X7": ("60", "base", "0.5"),
    "X8": ("100", "spy", "1.0"),
    "X9": ("100", "spy", "0.5"),
    "X10": ("100", "base", "1.0"),
    "X11": ("100", "base", "0.5"),
}
Y_GRID = {
    "Y1": ("bp3", "minvar"),
    "Y2": ("bp4", "minvar"),
    "Y3": ("kl", "rp"),
    "Y4": ("bp4", "rp"),
}
BLENDS = ("B0", "B1", "B2", "B3", "B4")
TILTS = ("T0", "R1", "R2", "R3", "R4")


def _job(  # noqa: PLR0913 - the job's fields, all named
    block: str,
    config: str,
    window: str,
    overrides: dict[str, str | None],
    cost: float,
    *,
    diagnostic_only: bool = False,
) -> dict[str, Any]:
    return {
        "block": block,
        "config": config,
        "window": window,
        "overrides": overrides,
        "cost": cost,
        "diagnostic_only": diagnostic_only,
    }


def jobs_for(  # noqa: C901, PLR0911, PLR0912 - one branch per block
    block: str, out: Path
) -> list[dict[str, Any]]:
    if block == "R1":
        return [
            _job("R1", config, window, {**V2, "satellite_weighting": method}, 10.0)
            for config, method in STAGE_B.items()
            for window in W6
        ]
    if block == "R2":
        jobs = [
            # The v2 grid predates the pool/source/exit variants: M = 30, SPY core, filter
            # exit 1.0, min-variance satellite.  Its V3 is X0's identity.
            _job(
                "R2",
                config,
                window,
                {
                    **V2,
                    "core_weight": core,
                    "trend_filter": filt,
                    "pool_m": "30",
                    "filter_exit": "1.0",
                    "stop_rule": "kl",
                },
                10.0,
            )
            for config, (core, filt) in V_GRID.items()
            for window in W6
        ]
        jobs += [
            _job(
                "R2",
                config,
                window,
                {
                    **V2,
                    "pool_m": pool,
                    "core_source": source,
                    "filter_exit": exit_fraction,
                    # The v3 grid predates round 4's Basing Points stop: its GUARD is the
                    # frozen K-L parameter set.
                    "stop_rule": "kl",
                },
                10.0,
            )
            for config, (pool, source, exit_fraction) in X_GRID.items()
            for window in W6
        ]
        jobs += [
            _job(
                "R2",
                config,
                window,
                {**V2, "stop_rule": stop, "satellite_weighting": weighting},
                10.0,
            )
            for config, (stop, weighting) in Y_GRID.items()
            for window in W6
        ]
        jobs += [
            _job(
                "R2",
                config,
                window,
                {**V2, "guard_layers": layers, "vol_target": vol},
                10.0,
                diagnostic_only=(config == "Z2"),
            )
            for config, (layers, vol) in STAGE_A.items()
            for window in W6
        ]
        return jobs
    if block == "R3":
        return [
            _job("R3", config, window, {**V2, "satellite_weighting": method}, 20.0)
            for config, method in (("FINAL", "mincvar"), ("B0", "minvar"))
            for window in W6
        ]
    if block == "R4":
        return [
            _job("R4", f"{blend}{tilt}", window, {**V1, "blend": blend, "tilt": tilt}, 10.0)
            for blend in BLENDS
            for tilt in TILTS
            for window in ("P1", "P3", "P2")
        ]
    if block == "R5":
        selection = _read_json(out / "champion_selection_phase1.json")
        selected = selection["selected"]
        configs: list[tuple[str, str, str]] = [
            ("B0T0", "B0", "T0"),
            (f"{selected['blend']}{selected['tilt']}", selected["blend"], selected["tilt"]),
        ]
        if (selected["blend"], selected["tilt"]) != ("B3", "R2"):
            configs.append(("B3R2", "B3", "R2"))
        return [
            _job("R5", config, window, {**V1, "blend": blend, "tilt": tilt}, 10.0)
            for config, blend, tilt in configs
            for window in ("H1", "H2", "H3")
        ]
    if block == "R6":
        return [_job("R6", "MCF", window, {**V2, "pool_mode": "universe"}, 10.0) for window in W6]
    if block == "R7":
        return [
            _job(
                "R7",
                config,
                window,
                {**V2, "guard_layers": "4", "satellite_weighting": method},
                10.0,
            )
            for config, method in (("B0", "minvar"), ("B5", "mincvar"))
            for window in W6
        ]
    if block == "R8":
        return [
            _job("R8", config, "C1", {**V2, "satellite_weighting": method, "pool_mode": pool}, 10.0)
            for config, method, pool in (
                ("FINAL", "mincvar", "cycle"),
                ("B0", "minvar", "cycle"),
                ("MCF", "minvar", "universe"),
            )
        ]
    if block == "R9":
        monthly = [
            _job(
                "R9",
                config,
                window,
                {
                    **V2,
                    "satellite_weighting": method,
                    "filter_frequency": "monthly",
                },
                10.0,
            )
            for config, method in (("B5", "mincvar"), ("B0", "minvar"))
            for window in (*W6, "C1")
        ]
        control = _job(
            "R9",
            "B5",
            "P1",
            {
                **V2,
                "satellite_weighting": "mincvar",
                "filter_frequency": "daily",
            },
            10.0,
        )
        control["tag"] = "DAILY"
        return [*monthly, control]
    if block == "R10":
        return [
            _job(
                "R10",
                "MARKOWITZ_SAMPLE",
                window,
                {
                    **V2,
                    "satellite_weighting": "minvar",
                    "cov_estimator": "sample",
                },
                10.0,
                diagnostic_only=True,
            )
            for window in (*W6, "C1")
        ]
    if block == "L1":
        return [
            _job(
                "L1",
                "FINAL",
                window,
                {
                    **V2,
                    "satellite_weighting": "mincvar",
                    "cvar_scenarios": "log",
                    "filter_series": "price",
                },
                10.0,
            )
            for window in W6
        ]
    if block == "L2":
        return [
            _job(
                "L2",
                "FINAL",
                window,
                {
                    **V2,
                    "satellite_weighting": "mincvar",
                    "cvar_scenarios": "simple",
                    "filter_series": "price",
                },
                10.0,
            )
            for window in W6
        ]
    if block == "L3":
        return [
            _job(
                "L3",
                "B0",
                window,
                {**V2, "satellite_weighting": "minvar", "filter_series": "price"},
                10.0,
            )
            for window in W6
        ]
    raise SystemExit(f"unknown block {block!r}")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"missing {path}: run the preceding block first")
    return dict(json.loads(path.read_text()))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _command(window: str, db: Path, job: dict[str, Any]) -> list[str]:
    o = job["overrides"]
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
        str(db),
        "--universe-db",
        UNIVERSE,
        "--params",
        PARAMS,
        "--warmup-steps",
        "12",
        "--arms",
        str(o["arms"]),
        "--blend",
        str(o["blend"]),
        "--tilt",
        str(o["tilt"]),
        "--trend-filter",
        str(o["trend_filter"]),
        "--pool-m",
        str(o["pool_m"]),
        "--core-source",
        str(o["core_source"]),
        "--filter-exit",
        str(o["filter_exit"]),
        "--stop-rule",
        str(o["stop_rule"]),
        "--satellite-weighting",
        str(o["satellite_weighting"]),
        "--vol-target",
        str(o["vol_target"]),
        "--cost-bps",
        str(job["cost"]),
        "--market-series",
        MARKET,
        "--filter-series",
        str(o.get("filter_series", "tr")),
        "--filter-frequency",
        str(o.get("filter_frequency", "daily")),
        *(["--cov-estimator", str(o["cov_estimator"])] if o.get("cov_estimator") else []),
        "--cvar-scenarios",
        str(o.get("cvar_scenarios", "simple")),
        "--pool-mode",
        str(o.get("pool_mode", "cycle")),
        "--exit-events",
        "docs/exit_events.json",
        "--returns-members",
        "range",
    ]
    if o["core_weight"] is not None:
        command += ["--core-weight", str(o["core_weight"])]
    if o["guard_layers"] is not None:
        command += ["--guard-layers", str(o["guard_layers"])]
    if o["trend_filter"] == "on" or (o["core_weight"] not in (None, "0")):
        command += ["--core-series", SPY]
    return command


def run_id_of(job: dict[str, Any]) -> str:
    tag = f"-{job['tag']}" if job.get("tag") else ""
    return f"{job['block']}-{job['config']}-{job['window']}{tag}" + (
        f"-{int(job['cost'])}bp" if job["cost"] != BASE_COST else ""
    )


def run_one(job: dict[str, Any], out: Path, keep_db: bool) -> dict[str, Any]:
    block = job["block"]
    config = job["config"]
    window = job["window"]
    run_id = run_id_of(job)
    runs = out / "runs"
    logs = out / "logs"
    dbs = out / "dbs"
    for directory in (runs, logs, dbs):
        directory.mkdir(parents=True, exist_ok=True)
    final_json = runs / f"{run_id}.json"
    if final_json.exists():
        raise SystemExit(f"{final_json} already exists: refusing to overwrite a run")
    template = TEMPLATES / (
        "template_C1_pitfix.db" if window == "C1" else f"template_{window}_pitfix.db"
    )
    if not template.is_file():
        raise SystemExit(f"missing template {template}")
    work = dbs / f"{run_id}.db"
    if work.exists():
        raise SystemExit(f"{work} already exists: refusing to reuse a copy")
    # A reflink copy is O(1) on this filesystem (2.4 s vs ~60 s for 3 GB); the fallback is
    # the ordinary copy when the filesystem cannot reflink.
    copied = subprocess.run(  # noqa: S603 - fixed argv, internal paths
        ["/bin/cp", "--reflink=auto", str(template), str(work)],
        capture_output=True,
        text=True,
        check=False,
    )
    if copied.returncode != 0:
        shutil.copyfile(template, work)
    work.chmod(0o644)
    log = logs / f"{run_id}.log"
    command = _command(window, work, job)
    started = time.monotonic()
    with log.open("w") as handle:
        proc = subprocess.run(  # noqa: S603
            command,
            cwd=REPO,
            stdout=handle,
            stderr=subprocess.STDOUT,
            env={**os.environ, **ENV},
            check=False,
        )
    elapsed = round(time.monotonic() - started, 1)
    if proc.returncode != 0:
        return {
            "run_id": run_id,
            "block": block,
            "config": config,
            "window": window,
            "cost_bps": job["cost"],
            "exit": proc.returncode,
            "seconds": elapsed,
            "log": str(log),
            "db_kept": str(work),
        }
    collect = subprocess.run(  # noqa: S603
        [UV, "run", "python", "scripts/backtest_final_collect.py", str(work), str(final_json)],
        cwd=REPO,
        capture_output=True,
        text=True,
        env={**os.environ, **ENV},
        check=False,
    )
    if collect.returncode != 0:
        return {
            "run_id": run_id,
            "block": block,
            "config": config,
            "window": window,
            "cost_bps": job["cost"],
            "exit": collect.returncode,
            "seconds": elapsed,
            "log": str(log),
            "db_kept": str(work),
            "collect_error": (collect.stderr or collect.stdout).strip()[-1500:],
        }
    row = {
        "run_id": run_id,
        "block": block,
        "config": config,
        "window": window,
        "cost_bps": job["cost"],
        "params_sha256": _sha256(REPO / PARAMS),
        "template_sha256": _sha256(template),
        "output_sha256": _sha256(final_json),
        "exit": 0,
        "seconds": elapsed,
        "diagnostic_only": bool(job["diagnostic_only"]),
        "output_json": str(final_json),
        "log": str(log),
        "db_kept": str(work) if keep_db else "",
    }
    if not keep_db:
        work.unlink()
    return row


def main(  # noqa: PLR0915 - one CLI path
    argv: list[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--block",
        required=True,
        choices=("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9", "R10", "L1", "L2", "L3"),
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="run only the first N jobs (smoke)")
    parser.add_argument("--configs", default=None, help="comma-separated config filter")
    parser.add_argument("--windows", default=None, help="comma-separated window filter")
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    jobs = jobs_for(args.block, out)
    if args.configs:
        wanted = {value.strip() for value in args.configs.split(",")}
        jobs = [job for job in jobs if job["config"] in wanted]
    if args.windows:
        wanted = {value.strip() for value in args.windows.split(",")}
        jobs = [job for job in jobs if job["window"] in wanted]
    if args.limit:
        jobs = jobs[: args.limit]
    # Resume: a run whose JSON already exists was collected and is never overwritten.
    jobs = [job for job in jobs if not (out / "runs" / f"{run_id_of(job)}.json").is_file()]
    print(json.dumps({"block": args.block, "jobs": len(jobs), "workers": args.workers}))

    manifest = out / f"runs_manifest_{args.block}.csv"
    fields = [
        "run_id",
        "block",
        "config",
        "window",
        "cost_bps",
        "params_sha256",
        "template_sha256",
        "output_sha256",
        "exit",
        "seconds",
        "diagnostic_only",
        "output_json",
        "log",
        "db_kept",
    ]
    lock = threading.Lock()
    index: list[dict[str, Any]] = []
    new_file = not manifest.exists()
    with manifest.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {}
            for job in jobs:
                keep = (
                    (job["block"] == "R1" and job["config"] == "B5")
                    or (job["block"] == "R8" and job["config"] == "FINAL")
                    # F2-1: the monthly FINAL (B5) copies are kept; the B0 and the daily
                    # control copies are deleted after their JSON is collected.
                    or (job["block"] == "R9" and job["config"] == "B5" and not job.get("tag"))
                    # L1/L2 keep their copy only until the CVaR diagnostic reads the
                    # per-decision scenario matrices; the diagnostic deletes them.
                    or (job["block"] in {"L1", "L2"} and job["config"] == "FINAL")
                )
                futures[pool.submit(run_one, job, out, keep)] = job
            for future in concurrent.futures.as_completed(futures):
                job = futures[future]
                try:
                    row = future.result()
                except Exception as exc:  # a refused copy or a missing template
                    raise SystemExit(
                        f"{job['block']} {job['config']} {job['window']}: {exc}"
                    ) from exc
                with lock:
                    writer.writerow(row)
                    handle.flush()
                    index.append(row)
                print(
                    f"{row['run_id']}: exit {row['exit']} in {row['seconds']}s",
                    flush=True,
                )
                if row["exit"] != 0:
                    print(json.dumps(row, indent=1), file=sys.stderr)
    previous = out / f"run_index_{args.block}.json"
    old = json.loads(previous.read_text()) if previous.is_file() else []
    previous.write_text(json.dumps(old + index, indent=1) + "\n")
    failures = [row for row in index if row["exit"] != 0]
    print(json.dumps({"done": len(index), "failures": len(failures)}))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
