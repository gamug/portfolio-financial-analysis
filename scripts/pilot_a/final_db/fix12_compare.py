"""FIXES #12 (D-20): the values the cover corrections change. Two offline runs of the affected companies on a subset
(subset_source.py): base (as installed) and fix (the corrected covers: cover_fix12.json in the subset's rows and, through
AUDIT_COVER_FIX, in the payloads the run reads); base is checked against the full run B shards (it must reproduce them),
then every metric row of fix compared with base -- value and inputs. Read-only.
usage: fix12_compare.py <base.db> <fix.db> <out.json> <b11_*.db> ..."""

import collections
import json
import sqlite3
import sys

BASE, FIX, OUT, *SHARDS = sys.argv[1:]
T = tuple(open("/home/vscode/audit_run/shard_fix12.txt").read().strip().split(","))
SQL = (
    "select a.ticker, f.accession_number, f.form, f.period_end, m.metric_name, m.value, m.inputs_json from fundamental_metrics m "
    f"join sec_filings f on f.id = m.filing_id join assets a on a.id = f.asset_id where m.engine_version = 'metrics-v11' "
    f"and a.ticker in ({','.join('?' * len(T))})"
)


def rows(db):
    return {
        (r[1], r[4]): r for r in sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(SQL, T)
    }


def same(x, y):
    return x == y or (
        x is not None and y is not None and abs(x - y) <= 1e-9 * max(1.0, abs(x), abs(y))
    )


b, f = rows(BASE), rows(FIX)
full = {}
for db in SHARDS:
    full.update({k: v for k, v in rows(db).items()})
base_vs_b11 = [k for k in b if k in full and not same(b[k][5], full[k][5])]
assert set(b) == set(f), ("row sets differ", len(set(b) ^ set(f)))
changed = [k for k in b if not same(b[k][5], f[k][5])]
inputs_only = [k for k in b if same(b[k][5], f[k][5]) and b[k][6] != f[k][6]]
kind = collections.Counter(
    (
        "value->value"
        if b[k][5] is not None and f[k][5] is not None
        else "value->empty"
        if b[k][5] is not None
        else "empty->value"
    )
    + (":" + k[1])
    for k in changed
)
out = dict(
    rows=len(b),
    base_vs_b11=dict(
        compared=sum(k in full for k in b), mismatches=len(base_vs_b11), examples=base_vs_b11[:5]
    ),
    changed=len(changed),
    by_kind=dict(kind),
    by_company=dict(collections.Counter(b[k][0] for k in changed)),
    inputs_only=len(inputs_only),
    inputs_only_by_metric=dict(collections.Counter(k[1] for k in inputs_only)),
    rows_changed=[
        dict(
            t=b[k][0],
            acc=k[0],
            form=b[k][2],
            period_end=b[k][3],
            metric=k[1],
            old=b[k][5],
            new=f[k][5],
        )
        for k in changed
    ],
    rows_inputs_only=[dict(t=b[k][0], acc=k[0], metric=k[1]) for k in inputs_only],
)
json.dump(out, open(OUT, "w"), indent=1)
print(json.dumps({k: v for k, v in out.items() if not k.startswith("rows_")}, indent=1))
