"""T-147: audit the data path against the SEC data-treatment checklist (v1.2.2). Read-only.

    uv run python scripts/audit_sec_checklist.py fetch                 # network: fill the SEC cache (once)
    uv run python scripts/audit_sec_checklist.py baseline --out DIR --date D     # the D-11 "before" export
    uv run python scripts/audit_sec_checklist.py company-types [--fetch]         # APP-00 table
    uv run python scripts/audit_sec_checklist.py l09                             # L-09 verification register
    uv run python scripts/audit_sec_checklist.py measure [--only n1,resolver]    # every count, both evidence levels
    uv run python scripts/audit_sec_checklist.py d07-replay                      # D-07 replay on scratch copies
    uv run python scripts/audit_sec_checklist.py fidelity                        # is the level (b) re-run faithful?

Production (``KG_FINANCIAL_DB``) and the pilot databases are opened ``mode=ro``; nothing here writes to them (the only
writer is ``d07-replay``, and it writes to temporary copies it deletes). SEC requests: ``SEC_USER_AGENT`` when set, else
``research@example.com``; at most 10 requests per second; every response cached under ``SEC_XCHECK_CACHE`` (gitignored).
The two evidence levels stay apart in every table: (a) the SEC source (``companyfacts`` / ``submissions``, scoped to
non-dimensional standard-taxonomy facts) and (b) today's resolver over the stored ``financial_facts``.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sec_xcheck import (
    baseline,
    checklist_doc,
    company_types,
    d07_replay,
    fidelity,
    findings,
    l09,
    records,
    runner,
    submissions,
)
from sec_xcheck.common import REPO, SecClient, cache_dir, cik_family, open_ro

from kg_schema.cli import resolve_db_path

SINCE = "2021-12-01"  # our filings start 2022-01-06
CHECKLIST_DIR = REPO / "docs" / "checklist_sec"
PILOT_DB = REPO / "data" / "pilot" / "financial_pilot.db"
PILOT_REPLAY_DB = REPO / "data" / "pilot" / "financial_pilot_replay.db"


def ciks_of(conn: object) -> list[str]:
    rows = conn.execute("SELECT DISTINCT cik FROM assets WHERE cik IS NOT NULL ORDER BY cik")  # type: ignore[attr-defined]
    seen: list[str] = []
    for (cik,) in rows:
        for c in cik_family(cik):
            if c not in seen:
                seen.append(c)
    return seen


def cmd_fetch(args: argparse.Namespace) -> int:
    conn = open_ro(args.db)
    client = SecClient(cache_dir(args.cache), offline=False)
    ciks = ciks_of(conn)[:: -1 if args.reverse else 1]
    print(
        f"fetching companyfacts and submissions for {len(ciks)} CIKs; User-Agent: {client.agent}",
        flush=True,
    )
    for i, cik in enumerate(ciks, 1):
        client.get("companyfacts", cik)
        submissions.all_filings(client, cik, SINCE)
        if i % 25 == 0:
            print(f"  {i}/{len(ciks)} CIKs, {client.requests} requests", flush=True)
    print(f"done: {client.requests} network requests (cached responses are free)")
    return 0


def cmd_baseline(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args.db)
    files = baseline.export_baseline(open_ro(args.db), db_path, Path(args.out))
    sha1 = baseline.file_digest(db_path, "sha1")
    text = baseline.manifest_markdown(files, db_path, sha1, Path(args.out), args.date)
    Path(args.manifest).write_text(text, encoding="utf-8")
    print(text)
    return 0


def cmd_company_types(args: argparse.Namespace) -> int:
    conn = open_ro(args.db)
    client = SecClient(cache_dir(args.cache), offline=not args.fetch)
    recs = records.load(conn)
    latest: dict[str, tuple[str, float]] = {}
    for r in recs:
        eq = r.value("equity")
        if (
            r.form == "10-K"
            and eq is not None
            and (r.cik not in latest or r.period_end > latest[r.cik][0])
        ):
            latest[r.cik] = (r.period_end, eq)
    rows = company_types.build_rows(conn, client, {c: v[1] for c, v in latest.items()})
    out = Path(args.out)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=company_types.FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    kinds: dict[str, int] = {}
    for r in rows:
        kinds[r["type"]] = kinds.get(r["type"], 0) + 1
    print(
        f"{len(rows)} CIKs -> {out}; types {kinds}; override needed: {[r['tickers'] for r in rows if r['needs_override']]}"
    )
    return 0


def cmd_l09(args: argparse.Namespace) -> int:
    conn = open_ro(args.db)
    rows = l09.build_register(conn)
    l09.write_csv(rows, Path(args.out))
    print(f"{len(rows)} register rows -> {args.out}")
    for f in l09.findings(rows, len(conn.execute("SELECT id FROM sec_filings").fetchall())):
        print(f"  {f.key}: {f.count}/{f.total} ({f.companies} companies)")
    return 0


def cmd_measure(args: argparse.Namespace) -> int:
    only = set(args.only.split(",")) if args.only else set(runner.GROUPS)
    client = SecClient(cache_dir(args.cache), offline=True)
    tables: dict[str, object] = {}
    result = runner.run_all(
        args.db, str(PILOT_DB) if args.pilot else None, client, Path(args.types), only, tables
    )
    Path(args.out_json).write_text(findings.to_json(result), encoding="utf-8")
    Path(args.out_tables).write_text(json.dumps(tables, indent=1, default=str), encoding="utf-8")
    print(f"{len(result)} findings -> {args.out_json}; supplement tables -> {args.out_tables}")
    return 0


def cmd_d07(args: argparse.Namespace) -> int:
    if not args.universe_db:
        raise SystemExit("pass --universe-db or set KG_UNIVERSE_DB (the pilot's universe.db)")
    types = runner.read_types(Path(args.types))
    pilot_tickers = {
        r[0] for r in open_ro(str(PILOT_REPLAY_DB)).execute("SELECT ticker FROM assets")
    }
    financial = sorted(
        t
        for r in types.values()
        if r["financial_firm"] == "True"
        for t in r["tickers"].split("/")
        if t in pilot_tickers
    )
    result = d07_replay.run(
        PILOT_REPLAY_DB, Path(args.universe_db), financial, args.start, args.end, args.top_n
    )
    result["excluded"] = financial
    Path(args.out).write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(
        f"excluded {financial}; {result['dates']} dates; mean sector weights written to {args.out}"
    )
    for s in sorted(set(result["with"]) | set(result["without"])):
        print(
            f"  {s:24s} {result['with'].get(s, 0) * 100:5.1f}% -> {result['without'].get(s, 0) * 100:5.1f}%"
        )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    out = checklist_doc.write(Path(args.out) if args.out else None)
    print(f"{out} written")
    return 0


def cmd_fidelity(args: argparse.Namespace) -> int:
    if args.out_json:
        result = {
            "production": fidelity.summary(open_ro(args.db)),
            "pilot": fidelity.summary(open_ro(str(PILOT_DB))),
        }
        Path(args.out_json).write_text(json.dumps(result, indent=1), encoding="utf-8")
        print(json.dumps(result, indent=1))
        return 0
    return fidelity.main(["--db", args.db] if args.db else [])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func: object, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--db", help="database to read, opened read-only (default: KG_FINANCIAL_DB)")
        p.add_argument("--cache", help="SEC cache directory (SEC_XCHECK_CACHE)")
        p.set_defaults(func=func)
        return p

    add("fetch", cmd_fetch, "download companyfacts and submissions into the cache").add_argument(
        "--reverse", action="store_true", help="walk the CIKs backwards (a second fetcher)"
    )
    bl = add(
        "baseline",
        cmd_baseline,
        "export the D-11 'before' baseline (read-only) and write its manifest",
    )
    bl.add_argument(
        "--out",
        required=True,
        help="directory outside the repo; existing files are never overwritten",
    )
    bl.add_argument("--date", required=True, help="export date for the manifest, YYYY-MM-DD")
    bl.add_argument("--manifest", default=str(CHECKLIST_DIR / "baseline_manifest.md"))
    ct = add("company-types", cmd_company_types, "build the APP-00 company-type table")
    ct.add_argument(
        "--fetch",
        action="store_true",
        help="download the 10-Ks the Article 9 evidence is read from",
    )
    ct.add_argument("--out", default=str(CHECKLIST_DIR / "company_types.csv"))
    add("l09", cmd_l09, "build the L-09 verification register").add_argument(
        "--out", default=str(CHECKLIST_DIR / "l09_verification.csv")
    )
    ms = add("measure", cmd_measure, "run the measurements (both evidence levels)")
    ms.add_argument("--only", help=f"comma-separated groups of {', '.join(runner.GROUPS)}")
    ms.add_argument(
        "--no-pilot", dest="pilot", action="store_false", help="skip the pilot database"
    )
    ms.add_argument("--types", default=str(CHECKLIST_DIR / "company_types.csv"))
    ms.add_argument("--out-json", default=str(CHECKLIST_DIR / "prevalence_results.json"))
    ms.add_argument("--out-tables", default=str(CHECKLIST_DIR / "prevalence_tables.json"))
    d7 = add(
        "d07-replay",
        cmd_d07,
        "D-07: sector exposure with and without the financial firms (scratch copies)",
    )
    d7.add_argument("--types", default=str(CHECKLIST_DIR / "company_types.csv"))
    d7.add_argument(
        "--universe-db",
        default=os.environ.get("KG_UNIVERSE_DB"),
        help="universe.db (KG_UNIVERSE_DB)",
    )
    d7.add_argument("--start", default="2024-01-05")
    d7.add_argument("--end", default="2026-10-02")
    d7.add_argument("--top-n", type=int, default=10)
    d7.add_argument("--out", default=str(CHECKLIST_DIR / "d07_replay.json"))
    add("report", cmd_report, "render docs/sec_data_checklist.md from the results").add_argument(
        "--out"
    )
    fid = add("fidelity", cmd_fidelity, "is the level (b) re-run faithful to the stored inputs?")
    fid.add_argument("--out-json", help="write production and pilot summaries here")
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
