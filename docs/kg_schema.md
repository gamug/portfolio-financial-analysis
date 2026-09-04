# `kg_schema`

> Lives in the external [`portfolio-common`](https://github.com/gamug/portfolio-common)
> repo, imported as **`portfolio_common.kg_schema`** and pinned by git tag in
> `pyproject.toml`'s `[tool.uv.sources]` (editable-path override for local work).
> Module paths below are written unqualified (`env`, `views`, `cli`, …); the real
> dotted path is `portfolio_common.kg_schema.<module>`. Bump the tag to adopt a
> new schema/universe contract.

Passive, behaviour-free schema shared by every repo that touches
`KG_FINANCIAL_DB`. Owns the additive DDL, the non-additive migrations, the
`schema_version` floor, and the `v_*` read-contract views the integration repo
consumes. It never touches `assets` / `sectors` (owned elsewhere).

Both agents call `kg_schema.ensure(conn)` at the end of their own `ensure_schema`.

It also holds the shared run seams every agent uses: `env.universe_database_path`
(`KG_UNIVERSE_DB`, default `/workspaces/thesis/data/universe.db`),
`universe_source` (point-in-time reads over `universe.db` — `members_asof`,
`symbols_asof`, `resolve_asset_ids`, `connect_ro`), `rundate` (the
`--analysis-date` argparse type + default-to-today), and `provenance.code_version`
(git short SHA `+ -dirty`, falling back to the package version then `"unknown"`).

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
agent tables — `event_time` / `ingested_at` / `filing_version` on the fact tables,
plus **run provenance**: `run_id` on `sec_filings` / `financial_facts` /
`fundamental_metrics` / `price_window` / `price_daily`, `as_of` + `code_version` on
`analysis_run` / `pricing_run`, and `code_version` on `quant_run` / `cycle_run`.
`m002` / `m003` carry `run_id` forward in their rebuilds so a not-yet-migrated dev
DB does not drop it (no new migration, no `schema_version` bump).

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
| m005 | rebuild `score_snapshot` with its `score_type` CHECK widened to admit `'SECTOR'` (guard: skipped when the CHECK already lists it); drops + recreates `v_score_snapshot` and the `fundamental_snapshot` compat view around the swap |

### `views.py` — `VIEWS`, `ensure_views(conn)`

`ensure_views` drops and recreates every view on each call (cheap, always current).
Because SQLite's `CREATE VIEW` does not validate its base tables, each view is
probed with a zero-row `SELECT` right after creation and **dropped** if a base
table is absent in this (partial / single-agent) DB — a dangling view would
otherwise break the view re-parse a later `ALTER TABLE … RENAME` in a migration
performs. The module docstring is the **projection contract** — column list +
semantics per view. Views:
`v_score_snapshot`, `v_sector` (GICS sector rollup with
member/sub-industry counts), `v_industry` (sub-industry → sector; the scrape has
no middle industry-group tier), `v_price_observation` (newest `engine_version` per
asset/day), `v_sec_filing` (one row per filing), `v_sec_filing_section` (carries
`item_label`, the ontology `itemLabel` token — `ITEM_1A_RISK_FACTORS`, `ITEM_7_MDA`,
… — built in SQL, kept identical to
`fundamental_agent.sections.canonical_item_label`), `v_veto`,
`v_rule_catalog` (veto rules as data — `params_json` verbatim plus unpacked
`param_metric` / `param_operator` / `param_threshold`), `v_portfolio_position`,
`v_shared_executive_edge` (pair-level aggregate), `v_cycle_ranking`,
`v_weight_scheme` (one row per `cycle_run` that recorded a blend — scheme id +
scalar knobs), `v_weight_component` (that blend exploded to one row per
`(cycle_run, score_type)`), `v_sector_aggregate_snapshot` (per-cycle mean of
members' TECHNICAL score per sector).

`quant/` (Markowitz benchmark) adds: `v_corporate_action`, `v_quant_return_daily`,
`v_risk_free_rate`, `v_benchmark_series` (each newest `engine_version` per key),
`v_quant_risk_model` (model metadata; μ / Σ stay internal), `v_quant_portfolio`,
`v_quant_position` (book weight stints), `v_quant_frontier_point`,
`v_quant_benchmark_performance`, and `v_quant_vs_live` (each optimized book beside
the live `portfolio_position` weights, per name).

**Run-log views** for `portfolio-reports`: `v_analysis_run`, `v_pricing_run`,
`v_quant_run`, `v_cycle_run` — one row per agent run with `run_id`, `as_of`
(`cycle_date` for cycle), `code_version`, status, timings and `params_json`.

**`v_universe_membership` is frozen.** The agents no longer write
`universe_membership` (the universe is read point-in-time from `universe.db`), so
this view is stale unless something else populates the table. Downstream readers
(the KG projection) should move to `universe.db` / `kg_schema.universe_source`.

### `coverage.py` + `coverage` command — core-data coverage for a dated universe

`check_coverage(fin_conn, universe_conn, as_of, *, universe="SP500",
min_observation_days=504) -> CoverageReport`: for each member as of `as_of`, does it
have an `assets` identity row, a FUNDAMENTAL `score_snapshot` (`event_time <= D`),
`fundamental_metrics` via a filing (`period_end <= D`), `price_daily` (`date <= D`),
`>= min_observation_days` `price_observation` rows (`obs_date <= D`), and a
`quant_return_daily` series. `covered` = every *required* check
(`assets`, `fundamental`, `pricing`, `observations`) passed. `persist_coverage`
upserts one `universe_coverage` row per `(as_of, universe, symbol)`.

Exposed as `python -m {fundamental_agent,pricing_agent,quant} coverage
--analysis-date D [--strict] [--min-fraction F] [--print-fill-commands]` (shared
impl `kg_schema.cli.run_coverage`). Read-only; default **warn** (report + exit 0),
`--strict` exits 1 when the covered fraction is below `F`. This is the guard for the
gap that reading agents (`cycle`, `quant`) otherwise hit silently — a member of the
as-of universe with no data to analyse.

### `universe_source.py` — point-in-time reads over `universe.db`

`connect_ro(path)` (read-only URI), `members_asof(conn, analysis_date, *,
universe="SP500") -> list[UniverseMember]` (predicate `valid_from <= D AND
(valid_to IS NULL OR valid_to > D)`, deduped by symbol keeping the latest stint;
rejects any universe other than `SP500`), `symbols_asof(...)`, and
`resolve_asset_ids(financial_conn, symbols) -> (mapping, missing)` (pure read,
case-insensitive `assets.ticker` match — creating a brand-new `assets` row stays
with the two agents' `sync_universe`). The legacy `universe_membership.reconcile`
is retained in the tree but is no longer on any write path.

### `cli.py` — `run_migrate(db_path) -> int`

Shared implementation of the `migrate` subcommand. Opens a connection, calls
`ensure(conn, run_migrations=True)`, prints the `schema_version` table.

## Tables added

| Table | Purpose | Immutability key |
|---|---|---|
| `universe_membership` | S&P 500 membership history (**frozen** — superseded by `universe.db`) | `UNIQUE(asset_id, universe, valid_from)` |
| `universe_coverage` | per-member core-data coverage for a dated universe (from `coverage`) | `UNIQUE(as_of, universe, symbol)` |
| `score_snapshot` | `ScoreSnapshot` types FUNDAMENTAL / VALORIZATION / TECHNICAL / SEMANTIC / SECTOR | `UNIQUE(asset_id, score_type, event_time)` |
| `rule_catalog` | veto rule definitions | `rule_id` PK |
| `veto` | rule hits, cleared not deleted | `UNIQUE(asset_id, rule_id, cycle_date)` |
| `portfolio_position` | position stints | `UNIQUE(asset_id, valid_from)` |
| `cycle_run` / `cycle_checkpoint` | orchestrator provenance + resume | `UNIQUE(cycle_type, cycle_date)` / `UNIQUE(cycle_run_id, step)` |
| `cycle_ranking` | the ranked cohort of a cycle | `UNIQUE(cycle_run_id, asset_id)` |
| `price_observation` | derived per-day price analytics | `UNIQUE(asset_id, obs_date, engine_version)` |
| `sec_filing_section` | narrative filing text | `UNIQUE(filing_id, section_type, ordinal, engine_version)` |
| `shared_executive_edge` | `sharedExecutiveWith` candidates | `UNIQUE(asset_id_a, asset_id_b, person_name, method)` |
| `sector_aggregate_snapshot` | per-cycle GICS-sector roll-up of members' TECHNICAL score | `UNIQUE(sector_id, cycle_date, metric_type)` |
| `corporate_action` | dividends / splits (gateway or XBRL-derived) | `UNIQUE(asset_id, action_type, ex_date, engine_version)` |
| `quant_return_daily` | total-return daily series (dividends folded in) | `UNIQUE(asset_id, obs_date, engine_version)` |
| `risk_free_rate` / `benchmark_series` | rf curve + benchmark index for `quant/` | `UNIQUE(curve, rate_date, engine_version)` / `UNIQUE(benchmark, obs_date, engine_version)` |
| `quant_risk_model` / `quant_expected_return` / `quant_covariance` | Markowitz μ / Σ per as-of model | `UNIQUE(as_of, model_version)` / `…(model_id, asset_id, mu_model)` / `…(model_id, asset_id_i, asset_id_j)` |
| `quant_portfolio` / `quant_position` / `quant_frontier_point` | optimized benchmark books + frontier | `UNIQUE(as_of, kind, frontier_k, engine_version)` / `…(portfolio_id, asset_id, valid_from)` / `…(model_id, k)` |
| `quant_benchmark_performance` | forward realized / active return of a frozen book | `UNIQUE(portfolio_id, date, engine_version)` |

## Gotchas

- **`ensure` is a hard dependency of both agents.** Keep it idempotent and
  additive-only; `ensure_views` swallows `OperationalError` so a view bug can't
  brick a batch run.
- **Shared-DB migration runbook:** quiesce all writers → `cp financial.db{,.bak}` →
  `python -m fundamental_agent migrate` once → check `SELECT * FROM schema_version`
  → resume. `-wal` / `-shm` files may exist even though this code forces rollback
  journal; standardise journal mode across writers before running m002–m004.
- **Never drop the `fundamental_snapshot` compat view** until every external
  consumer has moved to `v_score_snapshot`.
