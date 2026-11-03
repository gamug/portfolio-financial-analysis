"""FIXES #14 (L-44 (a)(b), reviewer A 2026-10-29): two offline runs of the affected companies on a subset -- base (the
2020+ payloads as loaded in round #11) and fix (plus the 18 filings the load missed, load14/) -- checked like FIXES #12:
base against run B's shards (it must reproduce them), then every metric row of the stored filings compared base vs fix
(value and inputs as JSON), and the new filings' rows counted. Read-only.
usage: fix14_compare.py <shard_tickers.txt> <base.db> <fix.db> <out.json> <b11_*.db> ..."""

import collections
import json
import sqlite3
import sys

TF, BASE, FIX, OUT, *SHARDS = sys.argv[1:]
T = tuple(open(TF).read().strip().split(","))
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
    full.update(rows(db))
base_vs_b11 = [k for k in b if k in full and not same(b[k][5], full[k][5])]
missing_in_base = [k for k in full if k not in b]
assert set(b) <= set(f), ("base rows missing in fix", len(set(b) - set(f)))
new = [k for k in f if k not in b]
changed = [k for k in b if not same(b[k][5], f[k][5])]
inputs_only = [
    k
    for k in b
    if same(b[k][5], f[k][5]) and json.loads(b[k][6] or "{}") != json.loads(f[k][6] or "{}")
]
kind = collections.Counter(
    (
        "value->value"
        if b[k][5] is not None and f[k][5] is not None
        else "value->empty"
        if b[k][5] is not None
        else "empty->value"
    )
    for k in changed
)
out = dict(
    rows_base=len(b),
    base_vs_b11=dict(
        compared=sum(k in full for k in b),
        mismatches=len(base_vs_b11),
        in_b11_not_in_base=len(missing_in_base),
        examples=base_vs_b11[:5],
    ),
    new_rows=len(new),
    new_filings=len({k[0] for k in new}),
    new_by_company=dict(collections.Counter(f[k][0] for k in new)),
    new_values=sum(f[k][5] is not None for k in new),
    changed=len(changed),
    by_kind=dict(kind),
    by_company=dict(collections.Counter(b[k][0] for k in changed)),
    by_metric=dict(collections.Counter(k[1] for k in changed)),
    inputs_only=len(inputs_only),
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
)
json.dump(out, open(OUT, "w"), indent=1)
print(json.dumps({k: v for k, v in out.items() if k != "rows_changed"}, indent=1))
