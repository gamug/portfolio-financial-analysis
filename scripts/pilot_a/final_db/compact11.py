"""Round #11 storage: compact.py's reduction (assets, sec_filings, the run's metrics-v11 rows) plus, for the filings the
run ingested that the source database does not hold (the 2020+ load), their financial_facts, filing_cover_shares and
data_quality_issue rows -- what the apply inserts into the real database.
usage: compact11.py <out.db> <run.db> <source.db>"""

import sqlite3
import sys

OUT, RUN, SOURCE = sys.argv[1:4]
o = sqlite3.connect(OUT)
o.execute(f"attach database 'file:{RUN}?mode=ro' as r")
o.execute(f"attach database 'file:{SOURCE}?mode=ro' as s")
for t in ("assets", "sec_filings"):
    o.execute(o.execute("select sql from r.sqlite_master where name=?", (t,)).fetchone()[0])
    o.execute(f"insert into main.{t} select * from r.{t}")
o.execute(
    o.execute("select sql from r.sqlite_master where name='fundamental_metrics'").fetchone()[0]
)
o.execute(
    "insert into main.fundamental_metrics select * from r.fundamental_metrics where engine_version='metrics-v11'"
)
o.execute(
    "create temp table new_f as select id from r.sec_filings where accession_number not in (select accession_number from s.sec_filings)"
)
for t in ("financial_facts", "filing_cover_shares", "data_quality_issue"):
    o.execute(o.execute("select sql from r.sqlite_master where name=?", (t,)).fetchone()[0])
    o.execute(
        f"insert into main.{t} select * from r.{t} where filing_id in (select id from temp.new_f)"
    )
o.commit()
n = {
    t: o.execute(f"select count(*) from main.{t}").fetchone()[0]
    for t in (
        "sec_filings",
        "fundamental_metrics",
        "financial_facts",
        "filing_cover_shares",
        "data_quality_issue",
    )
}
print(OUT, "new filings", o.execute("select count(*) from temp.new_f").fetchone()[0], n, "rows")
