"""FIXES #13 (D-20, the rule reviewer A set for WFC in FIXES #12) on a working copy of the installed database, in one
transaction:
* audit_cover_corrections (the append-only log FIXES #12 created): one new row per cover change -- the table and row
  changed, the old and new value, the filing's accession, the accession and EDGAR page the figure is read from (the
  amendment that corrects the cover), the rule, the date;
* the cover rows (cover_fix13.json): corrected in place, each only when it still holds the old value;
* the planned metric rows (plan_fix13.jsonl): value and inputs_json, each only when the row still holds the planned old
  value.
usage: fix13_apply.py <working_copy.db> <cover_fix13.json> <plan_fix13.jsonl>"""

import json
import sqlite3
import sys
from datetime import UTC, datetime

DB, COVERS, PLAN = sys.argv[1:4]
NOW = datetime.now(UTC).isoformat(timespec="seconds")
TABLE = {
    "filing_cover_shares": "update filing_cover_shares set value = ? where id = ? and value = ?",
    "audit_prior_cover_shares": "update audit_prior_cover_shares set value = ? where id = ? and value = ?",
}
c = sqlite3.connect(DB)
c.execute("pragma foreign_keys = on")
with c:
    for f in json.load(open(COVERS)):
        assert f["action"] == "update", f
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
                f["source_acc"],
                f["edgar_page"],
                f["rule"],
                NOW,
            ),
        )
        n = c.execute(TABLE[f["table"]], (f["new"], f["row_id"], f["old"])).rowcount
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
    "cover changes logged",
    c.execute("select count(*) from audit_cover_corrections").fetchone()[0],
    "in all | metric rows",
    n_plan,
)
