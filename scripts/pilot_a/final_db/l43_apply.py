"""FIXES #15 (L-43, reviewer A 2026-10-29: MKT-03 for the 54 companies) on a working copy of the installed database, in one
transaction: the table cover_class_conversion (accession, class_member, rate, source_accession, source_text, rule) with the
rates the filings print (l43_rates.json), and the planned metric rows (value and inputs_json, each only when the row still
holds the planned old value). usage: l43_apply.py <working_copy.db> <l43_rates.json> <plan.jsonl>"""

import json
import sqlite3
import sys

DB, RATES, PLAN = sys.argv[1:4]
c = sqlite3.connect(DB)
c.execute("pragma foreign_keys = on")
with c:
    c.execute(
        "CREATE TABLE cover_class_conversion (accession TEXT NOT NULL, class_member TEXT NOT NULL, rate REAL NOT NULL, "
        "source_accession TEXT NOT NULL, source_text TEXT NOT NULL, rule TEXT, PRIMARY KEY (accession, class_member))"
    )
    rates = json.load(open(RATES))
    c.executemany(
        "INSERT INTO cover_class_conversion VALUES (:accession, :class_member, :rate, :source_accession, :source_text, :rule)",
        rates,
    )
    n = 0
    for p in map(json.loads, open(PLAN)):
        if p["kind"] != "apply":
            continue
        k = c.execute(
            "update fundamental_metrics set value = ?, inputs_json = ? where id = ? and value is ?",
            (p["new"], p["inputs_json"], p["id"], p["old"]),
        ).rowcount
        assert k == 1, (p["id"], p.get("ticker"), p.get("metric"), k)
        n += 1
print("cover_class_conversion rows", len(rates), "| metric rows", n)
