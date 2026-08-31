# `kg_schema/`

Passive, behaviour-free schema shared by every other package. Owns the additive
DDL, the non-additive migrations, the `schema_version` floor, and the `v_*`
read-contract views the integration repo consumes. It never touches `assets` /
`sectors` (owned elsewhere).

Both agents call `kg_schema.ensure(conn)` at the end of their own `ensure_schema`.

## Files

### `__init__.py` — `ensure(conn, *, run_migrations=False) -> list[int]`

The single entrypoint. Sequence:

1. `version.ensure(conn)` — create `schema_version`.
2. `conn.executescript(ADDITIVE_DDL)` — all `CREATE TABLE/INDEX IF NOT EXISTS`.
3. `_add_missing_columns(conn)` — for each `REQUIRED_COLUMNS` entry, `ALTER TABLE …
   ADD COLUMN` if absent (nullable only). **No `schema_version` bump.**
4. `views.ensure_views(conn)` — drop + recreate every `v_*` view.
5. If `run_migrations`: `migrations.apply_migrations(conn)`, then rebuild views.

Steps 1–4 are safe to run against the shared production DB at any time,
concurrently with the other repos. Step 5 runs **only** via `python -m <agent>
migrate`.

### `ddl.py` — `ADDITIVE_DDL`, `REQUIRED_COLUMNS`

New tables (see the table below) plus `REQUIRED_COLUMNS`, a
`{table: {column: "TYPE"}}` map of nullable columns to graft onto the pre-existing
agent tables (`financial_facts.event_time/ingested_at/filing_version`,
`fundamental_metrics.event_time/engine_version`, `price_window.event_time`,
`price_daily.event_time/ingested_at`).

### `version.py`

`schema_version(version PK, applied_at, description)`. `current_version(conn)`
returns the max recorded version (0 if none). `record(conn, v, desc)` is
`INSERT OR IGNORE`, so re-recording is a no-op.

### `migrations.py` — `apply_migrations(conn) -> list[int]`

Numbered, non-additive rebuilds SQLite cannot do with `ALTER`. Each runs in one
transaction; on success its version is recorded. Re-running is a no-op once
recorded. Every migration is guarded (checks the table exists / additive columns
are present / it hasn't already run).

| # | Change |
|---|---|
| m001 | bootstrap `schema_version` at 1 |
| m002 | rebuild `financial_facts` with `UNIQUE(filing_id, statement, concept, period_key, filing_version)` + `event_time NOT NULL`; backfill from `sec_filings` |
| m003 | rebuild `fundamental_metrics` with `UNIQUE(…, engine_version)` + `event_time NOT NULL` |
| m004 | `INSERT … SELECT` `fundamental_snapshot` rows into `score_snapshot` as `FUNDAMENTAL`; rename the table to `fundamental_snapshot_legacy`; recreate `fundamental_snapshot` **as a compatibility VIEW** (joins `score_snapshot` → `sec_filings` for `form` / `fiscal_period`) so the README query and external consumers keep working until they move to `v_score_snapshot` |

### `views.py` — `VIEWS`, `ensure_views(conn)`

`ensure_views` drops and recreates every view on each call (cheap, always current);
a view whose base table is missing is silently skipped. The module docstring is the
**projection contract** — column list + semantics per view. Views:
`v_score_snapshot`, `v_universe_membership`, `v_price_observation` (newest
`engine_version` per asset/day), `v_sec_filing_section`, `v_veto`,
`v_portfolio_position`, `v_shared_executive_edge` (pair-level aggregate),
`v_cycle_ranking`.

### `universe_membership.py` — `reconcile(conn, universe, present_asset_ids, *, as_of, run_id=None, run_kind=None, source)`

Turns a mutable `assets` universe into append-only membership stints. Diffs
`present_asset_ids` against the currently-open rows: newcomers get
`INSERT … valid_to=NULL`, the departed get `UPDATE … SET valid_to = as_of`. Rows are
never deleted; a ticker that rejoins gets a fresh stint. Returns `(opened, closed)`.
Called by both agents' `sync_universe` (`source='wikipedia'` /
`'pricing-gateway'`).

### `cli.py` — `run_migrate(db_path) -> int`

Shared implementation of the `migrate` subcommand. Opens a connection, calls
`ensure(conn, run_migrations=True)`, prints the `schema_version` table.

## Tables added

| Table | Purpose | Immutability key |
|---|---|---|
| `universe_membership` | S&P 500 membership history | `UNIQUE(asset_id, universe, valid_from)` |
| `score_snapshot` | all four `ScoreSnapshot` types | `UNIQUE(asset_id, score_type, event_time)` |
| `rule_catalog` | veto rule definitions | `rule_id` PK |
| `veto` | rule hits, cleared not deleted | `UNIQUE(asset_id, rule_id, cycle_date)` |
| `portfolio_position` | position stints | `UNIQUE(asset_id, valid_from)` |
| `cycle_run` / `cycle_checkpoint` | orchestrator provenance + resume | `UNIQUE(cycle_type, cycle_date)` / `UNIQUE(cycle_run_id, step)` |
| `cycle_ranking` | the ranked cohort of a cycle | `UNIQUE(cycle_run_id, asset_id)` |
| `price_observation` | derived per-day price analytics | `UNIQUE(asset_id, obs_date, engine_version)` |
| `sec_filing_section` | narrative filing text | `UNIQUE(filing_id, section_type, ordinal, engine_version)` |
| `shared_executive_edge` | `sharedExecutiveWith` candidates | `UNIQUE(asset_id_a, asset_id_b, person_name, method)` |

## Gotchas

- **`ensure` is a hard dependency of both agents.** Keep it idempotent and
  additive-only; `ensure_views` swallows `OperationalError` so a view bug can't
  brick a batch run.
- **Shared-DB migration runbook:** quiesce all writers → `cp finantial.db{,.bak}` →
  `python -m fundamental_agent migrate` once → check `SELECT * FROM schema_version`
  → resume. `-wal` / `-shm` files may exist even though this code forces rollback
  journal; standardise journal mode across writers before running m002–m004.
- **Never drop the `fundamental_snapshot` compat view** until every external
  consumer has moved to `v_score_snapshot`.
