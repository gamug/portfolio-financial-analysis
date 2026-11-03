"""Round #11, after insert_new_filings.py and apply_plan.py: the applied copy vs the backup it was built on.
usage: check_apply11.py <backup_db> <applied_db> <plan_final.jsonl> <inserted.json>
* fundamental_metrics, the backup's rows (same ids): the rows that differ are exactly the plan's apply rows, each equal
  to the plan (value, inputs_json), every other column unchanged; the new rows all belong to the inserted filings,
  under engine label metrics-v10 and the load's analysis_run id;
* sec_filings / financial_facts / filing_cover_shares / data_quality_issue: the backup's rows unchanged (content hash
  in id order); every new row belongs to an inserted filing (sec_filings: exactly the inserted accessions);
* analysis_run: the backup's rows unchanged plus the one load row; every other table identical (content hash);
* PRAGMA integrity_check = ok and foreign_key_check empty on the applied copy."""

import hashlib
import json
import sqlite3
import sys

BK, AP, PLAN, INS = sys.argv[1:5]
b = sqlite3.connect(f"file:{BK}?mode=ro", uri=True)
a = sqlite3.connect(f"file:{AP}?mode=ro", uri=True)
plan = {r["id"]: r for r in map(json.loads, open(PLAN)) if r["kind"] == "apply"}
ins = json.load(open(INS))
run_id = ins["analysis_run_id"]
new_fids = {f["id"] for f in ins["filings"]}
assert len(new_fids) == len(ins["filings"]), "duplicate inserted filing ids"


def h(con, t, where=""):
    hh = hashlib.sha256()
    k = 0
    for row in con.execute(f'select * from "{t}" {where} order by rowid'):
        hh.update(repr(row).encode())
        k += 1
    return k, hh.hexdigest()


cols = [c[1] for c in b.execute("pragma table_info(fundamental_metrics)")]
iv, ij = cols.index("value"), cols.index("inputs_json")
maxid = b.execute("select max(id) from fundamental_metrics").fetchone()[0]
sel = f"select {','.join(cols)} from fundamental_metrics where id <= {maxid} order by id"
changed, bad, n = set(), [], 0
for rb, ra in zip(b.execute(sel), a.execute(sel), strict=True):
    n += 1
    assert rb[0] == ra[0], ("id order", rb[0], ra[0])
    if rb == ra:
        continue
    changed.add(rb[0])
    p = plan.get(rb[0])
    other = [c for i, c in enumerate(cols) if i not in (iv, ij) and rb[i] != ra[i]]
    if p is None or other or ra[iv] != p["new"] or ra[ij] != p["inputs_json"]:
        bad.append((rb[0], other, p is None))
print(f"stored metric rows {n}: changed {len(changed)}, planned {len(plan)}, bad {len(bad)}")
assert not bad, bad[:10]
for i in set(plan) - changed:
    r = b.execute("select value, inputs_json from fundamental_metrics where id=?", (i,)).fetchone()
    assert r[0] == plan[i]["new"] and r[1] == plan[i]["inputs_json"], i
newm = a.execute(
    f"select filing_id, engine_version, run_id from fundamental_metrics where id > {maxid}"
).fetchall()
assert all(f in new_fids and e == "metrics-v10" and r == run_id for f, e, r in newm), (
    "a new metric row outside the inserted filings"
)
print(f"new metric rows {len(newm)}, all of the {len(new_fids)} inserted filings")
for t in ("sec_filings", "financial_facts", "filing_cover_shares", "data_quality_issue"):
    mx = b.execute(f"select coalesce(max(id),0) from {t}").fetchone()[0]
    assert h(b, t) == h(a, t, f"where id <= {mx}"), (t, "backup rows changed")
    fcol = "id" if t == "sec_filings" else "filing_id"
    extra = a.execute(f"select {fcol} from {t} where id > {mx}").fetchall()
    assert all(x in new_fids for (x,) in extra), (t, "a new row outside the inserted filings")
    print(f"{t}: backup rows identical, {len(extra)} new rows, all of inserted filings")
got = {
    x
    for (x,) in a.execute(
        f"select accession_number from sec_filings where id in ({','.join(map(str, new_fids)) or 'null'})"
    )
}
assert got == {f["accession"] for f in ins["filings"]}, "inserted accessions differ"
mr = b.execute("select max(id) from analysis_run").fetchone()[0]
assert (
    h(b, "analysis_run") == h(a, "analysis_run", f"where id <= {mr}")
    and a.execute("select count(*) from analysis_run").fetchone()[0] == mr + 1
)
skip = {
    "fundamental_metrics",
    "sec_filings",
    "financial_facts",
    "filing_cover_shares",
    "data_quality_issue",
    "analysis_run",
}
tables = [
    t
    for (t,) in b.execute(
        "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"
    )
]
assert tables == [
    t
    for (t,) in a.execute(
        "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"
    )
], "table set differs"
for t in tables:
    if t in skip:
        continue
    assert h(b, t) == h(a, t), t
print(f"other tables: {len(tables) - len(skip)} identical; analysis_run +1 (id {run_id})")
# review minor 9: asserted, not only printed
assert (
    b.execute("select group_concat(sql, ';') from sqlite_master").fetchone()
    == a.execute("select group_concat(sql, ';') from sqlite_master").fetchone()
), "schema differs"
assert a.execute("pragma integrity_check").fetchone()[0] == "ok", "integrity"
fk = a.execute("pragma foreign_key_check").fetchall()
assert not fk, ("foreign keys", fk[:5])
cnt = ins["counts"]
assert cnt["sec_filings"] == len(new_fids) and cnt["fundamental_metrics"] == len(newm), (
    "inserted counts",
    cnt,
)
dqv = a.execute(
    f"select distinct metric_engine_version from data_quality_issue where filing_id in ({','.join(map(str, new_fids)) or 'null'})"
).fetchall()
assert all(v == "metrics-v10" for (v,) in dqv), ("DQ engine label", dqv)
print(
    "schema identical, integrity ok, foreign keys ok, inserted counts match, DQ label metrics-v10"
)
