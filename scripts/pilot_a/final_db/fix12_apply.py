"""FIXES #12 (D-20, reviewer A 2026-10-29) on a working copy of the installed database, in one transaction:
* the table audit_cover_corrections (append-only log): one row per cover change -- the table and row changed, the old
  and new value (NULL when removed), the filing's accession, the accession and EDGAR page the figure is read from, the
  rule, the date;
* the cover rows: corrected in place with the printed figure (cover_fix12.json), the shells' covers removed;
* the planned metric rows (plan_fix12.jsonl): value and inputs_json, each only when the row still holds the planned
  old value.
usage: fix12_apply.py <working_copy.db> <cover_fix12.json> <edgar_cover_23.json> <plan_fix12.jsonl>"""

import json
import sqlite3
import sys
from datetime import UTC, datetime

DB, COVERS, EDGAR, PLAN = sys.argv[1:5]
NOW = datetime.now(UTC).isoformat(timespec="seconds")
pages = {r["acc"]: r.get("document") for r in json.load(open(EDGAR))}
pages["0000072971-23-000142"] = (
    "https://www.sec.gov/Archives/edgar/data/72971/000007297123000142/wfc-20230630.htm"
)
RULE = {
    "update": "D-20: MKT-03 (cited from the company's own filing and recorded with its accession), MKT-01",
    "delete": "D-20: MKT-04 (a shell's cover: if absent, empty), L-40",
}
c = sqlite3.connect(DB)
c.execute("pragma foreign_keys = on")
with c:
    c.execute("""CREATE TABLE audit_cover_corrections (
        id INTEGER PRIMARY KEY, table_name TEXT NOT NULL, row_id INTEGER NOT NULL, accession TEXT NOT NULL,
        class_member TEXT, as_of_date TEXT, old_value REAL NOT NULL, new_value REAL, action TEXT NOT NULL,
        source_accession TEXT NOT NULL, edgar_page TEXT NOT NULL, rule TEXT NOT NULL, corrected_at TEXT NOT NULL)""")
    for f in json.load(open(COVERS)):
        src = f.get("source_acc") or f["acc"]
        rule = RULE[f["action"]] + (
            " -- WFC: MKT-01, the 10-Q/A is the latest filing available from 2023-08-07 (PER-01)"
            if f["t"] == "WFC"
            else ""
        )
        c.execute(
            "insert into audit_cover_corrections (table_name, row_id, accession, class_member, as_of_date, old_value, "
            "new_value, action, source_accession, edgar_page, rule, corrected_at) values (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f["table"],
                f["row_id"],
                f["acc"],
                f.get("class_member"),
                f["as_of_date"],
                f["old"],
                f["new"],
                f["action"],
                src,
                pages[src],
                rule,
                NOW,
            ),
        )
        if f["action"] == "update":
            n = c.execute(
                f"update {f['table']} set value = ? where id = ? and value = ?",
                (f["new"], f["row_id"], f["old"]),
            ).rowcount
        else:
            n = c.execute(
                f"delete from {f['table']} where id = ? and value = ?", (f["row_id"], f["old"])
            ).rowcount
        assert n == 1, (f, n)
    n_plan = 0
    for p in map(json.loads, open(PLAN)):
        n = c.execute(
            "update fundamental_metrics set value = ?, inputs_json = ? where id = ? and value is ?",
            (p["new"], p["inputs_json"], p["id"], p["old"]),
        ).rowcount
        assert n == 1, (p["id"], p["ticker"], p["metric"], n)
        n_plan += 1
print(
    "cover changes",
    c.execute("select count(*) from audit_cover_corrections").fetchone()[0],
    "| metric rows",
    n_plan,
)
