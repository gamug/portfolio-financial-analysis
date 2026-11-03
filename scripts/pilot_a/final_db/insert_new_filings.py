"""Round #11 / D-D revised: insert the filings the 2020+ run ingested (absent from the real database) into a copy of
it -- their sec_filings, financial_facts, filing_cover_shares, data_quality_issue and fundamental_metrics rows, read
from the run's compact shards (compact11.py). One new analysis_run row records the load; every inserted row carries
its id. Metric rows -- and the data_quality_issue rows that gate them -- are written under the database's one engine
label (metrics-v10, as every round's applied row).
Abort (nothing written) on any conflict.
usage: insert_new_filings.py <db_copy> <code_version> <out_inserted.json> <compact_0.db> [<compact_1.db> ...]"""

import json
import sqlite3
import sys
from datetime import UTC, datetime

DB, CODE, OUT, *SHARDS = sys.argv[1:]
con = sqlite3.connect(DB)
con.execute("PRAGMA foreign_keys=ON")
now = datetime.now(UTC).replace(microsecond=0).isoformat()
stored_acc = {a for (a,) in con.execute("select accession_number from sec_filings")}
asset_by_ticker = dict(con.execute("select ticker, id from assets"))


def cols(c, t):
    return [r[1] for r in c.execute(f"pragma table_info({t})")]


FCOLS = [c for c in cols(con, "sec_filings") if c != "id"]
inserted = {"filings": [], "counts": {}}
inserted_by: dict = {}
try:
    con.execute("BEGIN")
    params = {
        "analysis_date": "2026-10-18",
        "forms": ["10-K", "10-Q"],
        "since_year": 2020,
        "until_year": 2026,
        "metrics_only": True,
        "note": "audit round #11 D-D: filings filed from 2020-01-01 up to each company's "
        "first stored filing, from the gateway payloads (audit/r11/load2020_fetch.py, offline_run2020.py)",
    }
    run_id = con.execute(
        "insert into analysis_run (started_at, finished_at, universe_size, planned_units, completed_units, "
        "skipped_units, failed_units, params_json, status, as_of, code_version, fallback_units) "
        "values (?, ?, NULL, NULL, 0, 0, 0, ?, 'completed', '2026-10-18', ?, 0)",
        (now, now, json.dumps(params), CODE),
    ).lastrowid
    n = {
        "sec_filings": 0,
        "financial_facts": 0,
        "filing_cover_shares": 0,
        "data_quality_issue": 0,
        "fundamental_metrics": 0,
    }
    for shard in SHARDS:
        s = sqlite3.connect(f"file:{shard}?mode=ro", uri=True)
        s.row_factory = sqlite3.Row
        tick = dict(s.execute("select id, ticker from assets"))
        for f in s.execute("select * from sec_filings order by filing_date, id").fetchall():
            if f["accession_number"] in stored_acc:
                # review B2: one owner per filing -- never the same new accession under a second ticker
                assert (
                    inserted_by.get(f["accession_number"], tick[f["asset_id"]])
                    == tick[f["asset_id"]]
                ), ("a new filing under two tickers", f["accession_number"])
                continue
            inserted_by[f["accession_number"]] = tick[f["asset_id"]]
            row = dict(f)
            old_id = row.pop("id")
            row["asset_id"] = asset_by_ticker[tick[f["asset_id"]]]
            row["run_id"] = run_id
            new_id = con.execute(
                f"insert into sec_filings ({','.join(FCOLS)}) values ({','.join('?' * len(FCOLS))})",
                [row[c] for c in FCOLS],
            ).lastrowid
            stored_acc.add(f["accession_number"])
            n["sec_filings"] += 1
            inserted["filings"].append(
                {"accession": f["accession_number"], "id": new_id, "ticker": tick[f["asset_id"]]}
            )
            for t in (
                "financial_facts",
                "filing_cover_shares",
                "data_quality_issue",
                "fundamental_metrics",
            ):
                tc = [c for c in cols(s, t) if c != "id"]
                where = "filing_id = ?" + (
                    " and engine_version = 'metrics-v11'" if t == "fundamental_metrics" else ""
                )
                for r in s.execute(
                    f"select {','.join(tc)} from {t} where {where}", (old_id,)
                ).fetchall():
                    r = dict(zip(tc, r))
                    r["filing_id"] = new_id
                    if "run_id" in r:
                        r["run_id"] = run_id
                    if t == "data_quality_issue":
                        r["asset_id"] = row["asset_id"]
                        r["metric_engine_version"] = (
                            "metrics-v10"  # review M2: the label of the rows it gates
                        )
                    if t == "fundamental_metrics":
                        r["engine_version"] = "metrics-v10"
                    con.execute(
                        f"insert into {t} ({','.join(tc)}) values ({','.join('?' * len(tc))})",
                        [r[c] for c in tc],
                    )
                    n[t] += 1
    con.commit()
except BaseException:
    con.rollback()
    raise
inserted["counts"] = n
inserted["analysis_run_id"] = run_id
json.dump(inserted, open(OUT, "w"), indent=1)
print("inserted", n, "analysis_run", run_id)
