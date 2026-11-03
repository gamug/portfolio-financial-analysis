# Scripts that built the final pilot database (FIXES #11–#15)

`pilot_a3/financial.db` is the real, final database. These are the scripts that applied rounds #11–#15 to it. **They are
not in this repository**: they build SQL from table and column names held in variables (a generic "every table is
identical" loop over `sqlite_master`, `select {cols} from {t}`), which constitution Code & Git #10 forbids even when it is
safe, so they were taken out rather than rewritten without a way to re-run them on the real data. Each one is archived
exactly as it ran, in the audit archive of the thesis workspace (`/workspaces/thesis/audit/`), and the sha256 below
identifies that file. Stdlib only; every database or plan a script touches is a command-line argument and it changes a
**working copy** (the installed file is read-only).

What a new run needs is already in the engine: `kg_schema/data/*.csv` and `kg_schema/cover_audit.py`. Two inputs are not
in the archive folder alone: the EDGAR / gateway reads (`plan_*.jsonl`, `cover_fix*.json`, `l43_rates.json`) and the shard
files `shard_fix12.txt` and `shard_fix13.txt` that four of the scripts read.

| step | scripts | what it does |
|---|---|---|
| #11 (D-17, D-18, D-19, the 2020+ load) | `plan_apply11.py`, `compact11.py`, `insert_new_filings.py`, `apply_plan.py`, `check_apply11.py` | plan the changed metric rows from a base and a fixed run, insert the 4,419 new filings, apply the plan, check that only planned rows changed |
| #12, #13 (D-20) | `fix12_*.py`, `fix13_*.py` | correct the cover rows a filing's EDGAR page contradicts and log each in `audit_cover_corrections` |
| #14 (L-44) | `repair14.py`, `id07_check.py`, `fix14_compare.py` | rebuild the 24 missing filings, check ID-07 (`dei:DocumentPeriodEndDate` = EDGAR `periodOfReport`) |
| #15 (L-43, D-21) | `l43_read.py`, `l43_classes.py`, `l43_rates.py`, `l43_apply.py`, `l43_check.py` | read each multi-class filing's printed conversion rate, load `cover_class_conversion`, recompute the market caps |

Every `*_check.py` compares the result with the backup: tables identical except the planned rows, schema identical,
integrity and foreign keys ok. The independent recomputation of every metric is `scripts/pilot_a/independent_metrics.py`
(V3: 512,636 agree; the 47 differences are FOX/NWS, declared as L-16).

## The archived files

| script | archived at | sha256 |
|---|---|---|
| `apply_plan.py` | `audit/r11/apply_plan.py` | `21c71efb0d9c3e6fa5c8afa04c5fdcd96cd0cec6148b4e008b43cf8d866ec1b7` |
| `check_apply11.py` | `audit/r11/check_apply11.py` | `d39c116dd3131c6fd96367bda3fef8b2df62105f5ab313be9971737b06ca26ec` |
| `compact11.py` | `audit/r11/compact11.py` | `d2b5410891aad71e6e2993f61590f640de30789aaee14c1be41ed7a16e2b6d1b` |
| `fix12_apply.py` | `audit/r11/fix12_apply.py` | `de381c722aa688d41d469b96631873b872aadbedd325b9b538bbb1d304e6b202` |
| `fix12_check.py` | `audit/r11/fix12_check.py` | `e08dab1fd7638d0aba37368877c03c00da56d62fff585fcef4150eb8f0590e5d` |
| `fix12_compare.py` | `audit/r11/fix12_compare.py` | `87ff9f6d978c0e7ba845be0c38ded51dcb086e891cc23ae98caff9f1a54c3936` |
| `fix12_plan.py` | `audit/r11/fix12_plan.py` | `b7e9fe1f37816f2526f3962007ee7d553f46c5193a44d1e39708d58c8cc38173` |
| `fix13_apply.py` | `audit/r13/fix13_apply.py` | `50fde1559ab9b988ef3512680d3b0a011af187ff217a0d1d08d3c10d246c9bc5` |
| `fix13_check.py` | `audit/r13/fix13_check.py` | `56b836398934da1ae2ca9bbb20518b4a40154c17cebf2fc3bc26447ad143333a` |
| `fix13_compare.py` | `audit/r13/fix13_compare.py` | `ddfe2a89f8ae40e17e0b0a3af10d116027dc910b6f5ea1d5e897aa7d12612325` |
| `fix13_plan.py` | `audit/r13/fix13_plan.py` | `7459a05a9e7e62fc87569a2284b57cfe80f5747c5180bd64e3a8f61ddb27f4f6` |
| `fix14_compare.py` | `audit/r14/fix14_compare.py` | `1b02875287001722fbfd7ffc2c1030c34039df32f6387d5970dee5f35ded1235` |
| `id07_check.py` | `audit/r14/id07_check.py` | `bfe0276e1053710178bcb9dc6bffd3209993f828844416e10868fe17dff93dda` |
| `insert_new_filings.py` | `audit/r11/insert_new_filings.py` | `d0b929fce39559b5e0463f96c57b096be63dbc7f4bb95480770b9a7dcbaf9ed9` |
| `l43_apply.py` | `audit/r14/l43_apply.py` | `c4453fd63dc6a20ce3863e8ca909c7ee0f5db5809a611c7df40dc47f9ba932cc` |
| `l43_check.py` | `audit/r14/l43_check.py` | `6e72bf7d5aba66bf610f949970ab5acd6de11f8bed59ad60e5a2b8af8741037c` |
| `l43_classes.py` | `audit/r14/l43_classes.py` | `c2cac19180c05156b1e04de6f7f959d0f40c9afcacdd7b05ca89b8a10e28ed00` |
| `l43_rates.py` | `audit/r14/l43_rates.py` | `9ecc99e84ea27667ddb3d3e145355fbb0a66f5399ee727bfbd5026e45c232de0` |
| `l43_read.py` | `audit/r14/l43_read.py` | `27a9e5ebfdff445d0b595ec35bd55dac799a9bc796235f95ed0b9ac810d31a25` |
| `plan_apply11.py` | `audit/r11/plan_apply11.py` | `cae6736ead7f3bd787ab4c9eb825275b877dabf2d041d823beaa605890afc929` |
| `repair14.py` | `audit/r14/repair14.py` | `35595577520508b4f51bec71495def4daa970745e2ecc1bc266ae45fc13eb6d7` |
