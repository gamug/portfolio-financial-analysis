# Scripts that built the final pilot database (FIXES #11–#15)

`pilot_a3/financial.db` is the real, final database. These are the scripts that applied rounds #11–#15 to it, kept as they
ran: stdlib only, every database or plan they touch is a command-line argument, and each one changes a **working copy**
(the installed file is read-only). Two things are not reproducible from this folder alone: the EDGAR / gateway reads they
take as input (`plan_*.jsonl`, `cover_fix*.json`, `l43_rates.json`) and four shard-file paths (`shard_fix12.txt`,
`shard_fix13.txt`) in the `*_plan`/`*_compare` scripts. The data that matters for a new run is already in the engine
(`kg_schema/data/*.csv`, `kg_schema/cover_audit.py`).

| step | scripts | what it does |
|---|---|---|
| #11 (D-17, D-18, D-19, the 2020+ load) | `plan_apply11.py`, `compact11.py`, `insert_new_filings.py`, `apply_plan.py`, `check_apply11.py` | plan the changed metric rows from a base and a fixed run, insert the 4,419 new filings, apply the plan, check that only planned rows changed |
| #12, #13 (D-20) | `fix12_*.py`, `fix13_*.py` | correct the cover rows a filing's EDGAR page contradicts and log each in `audit_cover_corrections` |
| #14 (L-44) | `repair14.py`, `id07_check.py`, `fix14_compare.py` | rebuild the 24 missing filings, check ID-07 (`dei:DocumentPeriodEndDate` = EDGAR `periodOfReport`) |
| #15 (L-43, D-21) | `l43_read.py`, `l43_classes.py`, `l43_rates.py`, `l43_apply.py`, `l43_check.py` | read each multi-class filing's printed conversion rate, load `cover_class_conversion`, recompute the market caps |

Every `*_check.py` compares the result with the backup: tables identical except the planned rows, schema identical,
integrity and foreign keys ok. The independent recomputation of every metric is `scripts/pilot_a/independent_metrics.py`
(V3: 512,636 agree; the 47 differences are FOX/NWS, declared as L-16).
