"""FIXES #12 check: the working copy against the installed database it was made from. Asserts: fundamental_metrics --
only the planned rows differ, each equal to the plan (value, inputs_json), every other column unchanged; the cover
tables -- only the logged rows changed (the new value) or removed; audit_cover_corrections -- one row per cover change;
every other table identical (content hash); the schema is the old one plus that table; foreign_key_check empty;
quick_check ok. usage: fix12_check.py <installed_db> <working_copy.db> <plan_fix12.jsonl> <cover_fix12.json>"""

import hashlib
import json
import sqlite3
import sys

OLD, NEW, PLAN, COVERS = sys.argv[1:5]
o = sqlite3.connect(f"file:{OLD}?mode=ro", uri=True)
n = sqlite3.connect(f"file:{NEW}?mode=ro", uri=True)
plan = {p["id"]: p for p in map(json.loads, open(PLAN))}
fixes = json.load(open(COVERS))


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
for t in ("filing_cover_shares", "audit_prior_cover_shares"):
    fx = {f["row_id"]: f for f in fixes if f["table"] == t}
    old = {r[0]: r for r in o.execute(f"select * from {t}")}
    new = {r[0]: r for r in n.execute(f"select * from {t}")}
    vcol = [r[1] for r in o.execute(f"pragma table_info({t})")].index("value")
    assert set(old) - set(new) == {i for i, f in fx.items() if f["action"] == "delete"}, (
        t,
        "removed rows",
    )
    assert set(new) <= set(old), (t, "added rows")
    for i in new:
        if i in fx:
            assert new[i][vcol] == fx[i]["new"] and old[i][vcol] == fx[i]["old"], (t, i)
            assert [x for j, x in enumerate(new[i]) if j != vcol] == [
                x for j, x in enumerate(old[i]) if j != vcol
            ], (t, i)
        else:
            assert new[i] == old[i], (t, i)
    print(
        f"{t}: {sum(f['action'] == 'update' for f in fx.values())} corrected, {sum(f['action'] == 'delete' for f in fx.values())} removed, nothing else"
    )
log = n.execute(
    "select table_name, row_id, action, old_value, new_value from audit_cover_corrections order by id"
).fetchall()
assert sorted(log) == sorted(
    (f["table"], f["row_id"], f["action"], f["old"], f["new"]) for f in fixes
), "log differs"
print(f"audit_cover_corrections: {len(log)} rows, one per change")
skip = {
    "fundamental_metrics",
    "filing_cover_shares",
    "audit_prior_cover_shares",
    "audit_cover_corrections",
}
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
assert sorted(tn) == sorted(to + ["audit_cover_corrections"]), "table set"
for t in to:
    if t not in skip:
        assert h(o, t) == h(n, t), t
print(f"other tables: {len(to) - 3} identical")
so = {r for r in o.execute("select type, name, sql from sqlite_master")}
sn = {r for r in n.execute("select type, name, sql from sqlite_master")}
assert sn - so == {r for r in sn if r[1] == "audit_cover_corrections"} and not so - sn, "schema"
assert not n.execute("pragma foreign_key_check").fetchall(), "foreign keys"
qc = n.execute("pragma quick_check").fetchone()[0]
assert qc == "ok", qc
print("schema = old + audit_cover_corrections; foreign keys ok; quick_check ok")
