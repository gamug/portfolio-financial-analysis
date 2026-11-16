#!/usr/bin/env python3
"""T-161: compare the recomputed scratch copy's latest rows against the pilot_a3 state (v10).

Per (filing, metric): same / changed / emptied / filled. A filing with no newer row is unchanged by
construction (the recompute wrote no row for it). Read-only.
"""

from __future__ import annotations

import argparse
from collections import Counter

from kg_schema.db import connect_ro
from kg_schema.versions import version_key


def _rows(conn, version: str) -> dict[tuple[int, str, str], float | None]:
    out: dict[tuple[int, str, str], float | None] = {}
    for r in conn.execute(
        "SELECT filing_id, metric_group, metric_name, value FROM fundamental_metrics "
        "WHERE engine_version=?",
        (version,),
    ):
        out[(int(r["filing_id"]), str(r["metric_group"]), str(r["metric_name"]))] = r["value"]
    return out


def main() -> int:  # noqa: C901 - one comparison pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--before", default="metrics-v10")
    ap.add_argument("--out", default="out/t161_compare.csv")
    args = ap.parse_args()
    conn = connect_ro(args.db)
    versions = [
        str(r["engine_version"])
        for r in conn.execute("SELECT DISTINCT engine_version FROM fundamental_metrics")
        if r["engine_version"]
    ]
    latest = max(versions, key=version_key)
    before = _rows(conn, args.before)
    after = _rows(conn, latest)
    recomputed = {key[0] for key in after}
    changed: list[tuple[int, str, str, float | None, float | None]] = []
    emptied: list[tuple[int, str, str, float | None, None]] = []
    filled: list[tuple[int, str, str, None, float]] = []
    for key, old in before.items():
        if key[0] not in recomputed:
            continue  # not recomputed: unchanged by construction
        if key not in after:
            if old is not None:
                emptied.append((*key, old, None))
            continue
        new = after[key]
        if old is None and new is not None:
            filled.append((*key, new))
        elif old is not None and new is None:
            emptied.append((*key, old, None))
        elif old != new:
            changed.append((*key, old, new))
    for key, new in after.items():
        if key not in before and new is not None:
            filled.append((*key, new))
    ticker = {int(r["id"]): r["ticker"] for r in conn.execute("SELECT id, ticker FROM assets")}
    filings = {
        int(r["id"]): dict(r)
        for r in conn.execute(
            "SELECT id, asset_id, form, fiscal_period, period_end FROM sec_filings"
        )
    }
    fmt = lambda v: f"{v:.10g}" if isinstance(v, float) else ""  # noqa: E731
    with open(args.out, "w") as fh:
        fh.write(
            "ticker,form,fiscal_period,period_end,metric_group,metric_name,before,after,kind\n"
        )
        for kind, rows in (("changed", changed), ("emptied", emptied), ("filled", filled)):
            for row in rows:
                fid, group, name = row[0], row[1], row[2]
                f = filings.get(fid, {})
                fh.write(
                    f"{ticker.get(f.get('asset_id'))},{f.get('form')},{f.get('fiscal_period')},"
                    f"{f.get('period_end')},{group},{name},{fmt(row[3])},{fmt(row[4])},{kind}\n"
                )
    print(
        f"latest={latest}; recomputed filings={len(recomputed)}; "
        f"same={len(before) - len(changed) - len(emptied)}"
    )
    print(f"changed={len(changed)} emptied={len(emptied)} filled={len(filled)}")
    by_metric = Counter((r[1], r[2]) for r in changed)
    print("changed by metric:", dict(by_metric))
    print(
        "changed filings:",
        sorted(
            {
                (
                    ticker.get(filings.get(r[0], {}).get("asset_id")),
                    filings.get(r[0], {}).get("period_end"),
                )
                for r in changed
            }
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
