"""FIXES #12 (D-20): the metric rows the cover corrections change, as plan rows for the installed database. From the two
subset runs (fix12_compare.py's base and fix): every (accession, metric) whose value differs, or whose inputs differ as
JSON (not as text: key order is not a change). Each row's new value and inputs are the fix run's, plus reviewer A's flag
flag_market_capitalization_cover_count_from_document when the company's cover was corrected from its document, or
flag_market_capitalization_shell_cover_removed when the shell's cover was removed (MKT-04: the value is empty, L-40).
The target is the installed row (accession + metric); its current value must equal the base run's (the base reproduces
run B). Read-only. usage: fix12_plan.py <installed_db> <base.db> <fix.db> <cover_fix12.json> <out_plan.jsonl>"""

import collections
import json
import sqlite3
import sys

DB, BASE, FIX, COVERS, OUT = sys.argv[1:6]
T = tuple(open("/home/vscode/audit_run/shard_fix12.txt").read().strip().split(","))
fixes = json.load(open(COVERS))
removed = {
    f["t"] for f in fixes if f["action"] == "delete"
}  # VTRS, SW: every changed row of theirs reads no cover now
SQL = (
    "select a.ticker, f.accession_number, m.metric_name, m.value, m.inputs_json from fundamental_metrics m join sec_filings f "
    f"on f.id = m.filing_id join assets a on a.id = f.asset_id where m.engine_version = 'metrics-v11' and a.ticker in "
    f"({','.join('?' * len(T))})"
)


def rows(db):
    return {
        (r[1], r[2]): r for r in sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(SQL, T)
    }


def same(x, y):
    return x == y or (
        x is not None and y is not None and abs(x - y) <= 1e-9 * max(1.0, abs(x), abs(y))
    )


b, f = rows(BASE), rows(FIX)
db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
plan, kinds, bad = [], collections.Counter(), []
for k in sorted(b):
    ib, iff = json.loads(b[k][4] or "{}"), json.loads(f[k][4] or "{}")
    if same(b[k][3], f[k][3]) and ib == iff:
        continue
    tgt = db.execute(
        "select m.id, m.value from fundamental_metrics m join sec_filings s on s.id = m.filing_id join assets a on a.id = s.asset_id "
        "where s.accession_number = ? and m.metric_name = ? and a.ticker = ?",
        k + (b[k][0],),
    ).fetchall()
    if len(tgt) != 1 or not same(tgt[0][1], b[k][3]):
        bad.append((k, tgt))
        continue
    shell = (
        b[k][0] in removed
        and f[k][3] is None
        and ("market_capitalization" not in iff or iff.get("market_capitalization") is None)
    )
    iff[
        "flag_market_capitalization_shell_cover_removed"
        if shell
        else "flag_market_capitalization_cover_count_from_document"
    ] = 1.0
    plan.append(
        dict(
            id=tgt[0][0],
            kind="apply",
            ticker=b[k][0],
            accession=k[0],
            metric=k[1],
            old=tgt[0][1],
            new=f[k][3],
            inputs_json=json.dumps(iff),
            rule="D-20 " + ("shell cover removed" if shell else "cover count from document"),
        )
    )
    kinds[
        ("value " if not same(b[k][3], f[k][3]) else "inputs only ")
        + ("shell" if shell else "corrected")
    ] += 1
assert not bad, bad[:5]
with open(OUT, "w") as fh:
    for p in plan:
        fh.write(json.dumps(p) + "\n")
print(
    len(plan),
    "plan rows",
    dict(kinds),
    "companies",
    dict(collections.Counter(p["ticker"] for p in plan)),
)
