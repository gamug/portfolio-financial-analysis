"""Apply a checked plan to the real database (reviewer B; FIXES.md). usage: apply_plan.py <db> <plan.jsonl> [<prior_cover_src_db>]

Every ``apply`` row: UPDATE value + inputs_json by id, only if the row's current value is still the plan's ``old``
(else abort, nothing written). With <prior_cover_src_db>, the table ``audit_prior_cover_shares`` is copied from it
(same DDL, same rows). One transaction."""

import json
import sqlite3
import sys

DB, PLAN, *rest = sys.argv[1:]
con = sqlite3.connect(DB)
con.execute("PRAGMA foreign_keys=ON")


def eqv(a, b):
    return (a is None and b is None) or (
        a is not None and b is not None and abs(a - b) <= 1e-9 * max(1.0, abs(a))
    )


rows = [json.loads(l) for l in open(PLAN) if l.strip()]
rows = [r for r in rows if r["kind"] == "apply"]
assert len({r["id"] for r in rows}) == len(rows), "duplicate ids in plan"
try:
    con.execute("BEGIN")
    for r in rows:
        cur = con.execute(
            "select value, engine_version from fundamental_metrics where id=?", (r["id"],)
        ).fetchone()
        assert cur is not None and cur[1] == "metrics-v10" and eqv(cur[0], r["old"]), (
            "stale plan row",
            r["id"],
            cur,
            r["old"],
        )
        con.execute(
            "update fundamental_metrics set value=?, inputs_json=? where id=?",
            (r["new"], r["inputs_json"], r["id"]),
        )
    if rest:
        src = sqlite3.connect(f"file:{rest[0]}?mode=ro", uri=True)
        ddl = src.execute(
            "select sql from sqlite_master where name='audit_prior_cover_shares'"
        ).fetchone()[0]
        cols = [c[1] for c in src.execute("pragma table_info(audit_prior_cover_shares)")]
        con.execute(ddl)
        data = src.execute(f"select {','.join(cols)} from audit_prior_cover_shares").fetchall()
        con.executemany(
            f"insert into audit_prior_cover_shares ({','.join(cols)}) values ({','.join('?' * len(cols))})",
            data,
        )
        print("audit_prior_cover_shares rows", len(data))
    con.commit()
except BaseException:
    con.rollback()
    raise
print("applied", len(rows))
