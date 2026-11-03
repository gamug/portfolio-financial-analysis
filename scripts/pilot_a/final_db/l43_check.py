"""FIXES #15 check (L-43): the working copy against the installed database -- only the planned metric rows differ (value,
inputs_json; every other column unchanged); cover_class_conversion holds exactly the rates file; every other table identical;
schema = old + that table; foreign keys ok; quick_check ok. usage: l43_check.py <installed_db> <working_copy.db> <plan.jsonl> <l43_rates.json>"""

import hashlib
import json
import sqlite3
import sys

OLD, NEW, PLAN, COVERS = sys.argv[1:5]
o = sqlite3.connect(f"file:{OLD}?mode=ro", uri=True)
n = sqlite3.connect(f"file:{NEW}?mode=ro", uri=True)
plan = {p["id"]: p for p in map(json.loads, open(PLAN)) if p["kind"] == "apply"}


def h(con, t, where=""):
    x = hashlib.sha256()
    k = 0
    for row in con.execute(f'select * from "{t}" {where} order by rowid'):
        x.update(repr(row).encode())
        k += 1
    return k, x.hexdigest()


cols = [r[1] for r in o.execute("pragma table_info(fundamental_metrics)")]
iv, ij = cols.index("value"), cols.index("inputs_json")
sel = f"select {','.join(cols)} from fundamental_metrics order by id"
changed, bad = set(), []
for a, b in zip(o.execute(sel), n.execute(sel), strict=True):
    if a == b:
        continue
    changed.add(a[0])
    p = plan.get(a[0])
    other = [cols[i] for i in range(len(cols)) if i not in (iv, ij) and a[i] != b[i]]
    if p is None or other or b[iv] != p["new"] or b[ij] != p["inputs_json"]:
        bad.append((a[0], other, p is None))
assert not bad, bad[:5]
assert changed == set(plan), ("planned rows not changed", set(plan) - changed)
print(f"fundamental_metrics: {len(changed)} rows changed, all planned, none else")
rates = json.load(open(COVERS))
new_t = sorted(
    n.execute(
        "select accession, class_member, rate, source_accession, source_text, rule from cover_class_conversion"
    )
)
assert new_t == sorted(
    (
        r["accession"],
        r["class_member"],
        r["rate"],
        r["source_accession"],
        r["source_text"],
        r["rule"],
    )
    for r in rates
), "rates table"
print(f"cover_class_conversion: {len(new_t)} rows, exactly the rates file")
skip = {"fundamental_metrics"}
to = [
    t
    for (t,) in o.execute(
        "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"
    )
]
tn = [
    t
    for (t,) in n.execute(
        "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"
    )
]
assert sorted(tn) == sorted(to + ["cover_class_conversion"]), "table set"
for t in to:
    if t not in skip:
        assert h(o, t) == h(n, t), t
print(f"other tables: {len(to) - 1} identical")
so = {r for r in o.execute("select type, name, sql from sqlite_master")}
sn = {r for r in n.execute("select type, name, sql from sqlite_master")}
assert so <= sn and {r[1] for r in sn - so} <= {
    "cover_class_conversion",
    "sqlite_autoindex_cover_class_conversion_1",
}, "schema"
assert not n.execute("pragma foreign_key_check").fetchall(), "foreign keys"
qc = n.execute("pragma quick_check").fetchone()[0]
assert qc == "ok", qc
print("schema = old + cover_class_conversion; foreign keys ok; quick_check ok")
