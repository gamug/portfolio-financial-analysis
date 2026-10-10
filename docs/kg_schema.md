# `kg_schema`

> Vendored at `src/kg_schema/` (a top-level package alongside `api`, `cycle`,
> `entity_resolution`, `fundamental_agent`, `pricing_agent`, `quant`). It used
> to live in the external
> [`portfolio-common`](https://github.com/gamug/portfolio-common) repo as
> `portfolio_common.kg_schema`, but as of that repo's **v1.0.0** release
> (a DB-engine/business-logic split — see its `CHANGELOG.md`), the domain
> logic was extracted back into each owning repo; `portfolio-common` is now
> DB-engine-only. `portfolio-common` stays a dependency here for exactly two
> primitives `kg_schema` (and this repo's own `db.py`-style modules) build on:
> `portfolio_common.db.Database` (the connection class `kg_schema.db.connect`
> wraps) and the injection-safety helpers `in_clause` / `Allowlist`. See
> `docs/portfolio-common-v1-migration-plan.md` for the migration itself.

Passive, behaviour-free schema shared by every package that touches
`KG_FINANCIAL_DB`. Owns the additive DDL, the non-additive migrations, the
`schema_version` floor, and the `v_*` read-contract views the integration repo
consumes. It never touches `assets` / `sectors` (owned elsewhere).

Every package calls `kg_schema.ensure(conn)` at the end of its own
`ensure_schema`.

It also holds the shared run seams every agent uses: `env.universe_database_path`
(`KG_UNIVERSE_DB`, default `/workspaces/thesis/data/universe.db`),
`queries` (point-in-time reads over `universe.db` — `members_asof`,
`symbols_asof`, `resolve_asset_ids`, `connect_ro`), `rundate` (the
`--analysis-date` argparse type + default-to-today), and `provenance.code_version`
(git short SHA `+ -dirty`, falling back to the package version then `"unknown"`).
"Dirty" is scoped, not "any uncommitted file in the checkout": `_git_version` runs
`git status --porcelain -- src skills pyproject.toml uv.lock` (`_DIRTY_SCOPE`), so an
untracked file outside those paths (a scratch note, a local review doc) leaves
`code_version` clean, while an uncommitted change under any of them — tracked or not —
still marks it `-dirty` (T-114, PR #92 review; the tag and every guard derived from it
agree by construction, since both read the same scoped `git status`).
`provenance.dirty_tree_reason(version=None)` turns that `-dirty` suffix into a guard:
`None` if clean, else a reason naming it; `DirtyTree` is the exception `fundamental_agent`/
`quant`/`cycle`/`entity_resolution`/`pricing_agent` each raise unless `--allow-dirty`
(T-114).

## Files

### `db.py` — `connect(path, *, read_only=False, create_parents=True)`, `connect_ro(path)`

The one connection factory for the analysis-workstream databases. A thin
wrapper over `portfolio_common.db.Database.connect`: rows come back as
`sqlite3.Row`; read/write opens with `foreign_keys=ON` + a busy_timeout
(reapplied every connection); `wal=False` always — `KG_FINANCIAL_DB` may sit
on a bind mount whose `-shm` support is unreliable, so this domain never
turns on WAL. `connect_ro` opens `file:{path}?mode=ro` (no pragmas, no
directory creation).

### `__init__.py` — `ensure(db, *, run_migrations=False) -> list[int]`

The single entrypoint. Sequence:

1. `queries.ensure(db)` — create `schema_version`.
2. `db.executescript(ADDITIVE_DDL)` — all `CREATE TABLE/INDEX IF NOT EXISTS`.
3. `_add_missing_columns(db)` — for each `REQUIRED_COLUMNS` entry, `ALTER TABLE …
   ADD COLUMN` if absent (nullable only). **No `schema_version` bump.**
4. `availability.ensure_triggers(db)` — the `T-107` `available_at` guards
   (`ddl.AVAILABILITY_TRIGGERS`), once `sec_filings`, `fundamental_metrics` and
   `score_snapshot` all carry the column. They check only rows written from then on;
   existing rows are filled by `m008`.
5. `views.ensure_views(db)` — drop + recreate every `v_*` view. **Skipped, with a warning,
   when the database's `schema_version` is above the highest migration this code knows**
   (T-144): a later contract may have added columns and views this code does not define, and
   rebuilding would remove them. It protects later code from the next contract change; it
   cannot retrofit code that predates it.
6. If `run_migrations`: `migrations.apply_migrations(db)`, then rebuild views.

Steps 1–5 are safe to run against the shared production DB at any time,
concurrently with the other packages. Step 6 runs **only** via `python -m
<agent> migrate`.

### `ddl.py` — `ADDITIVE_DDL`, `REQUIRED_COLUMNS`

New tables (see the table below) plus `REQUIRED_COLUMNS`, a
`{table: {column: "TYPE"}}` map of nullable columns to graft onto the pre-existing
agent tables — `event_time` / `ingested_at` / `filing_version` on the fact tables,
plus **run provenance**: `run_id` on `sec_filings` / `financial_facts` /
`fundamental_metrics` / `price_window` / `price_daily`, `as_of` + `code_version` on
`analysis_run` / `pricing_run`, `code_version` on `quant_run` / `cycle_run`,
`correction_rule` on `financial_facts` (T-118: the EDGAR gateway's own `data["corrections"]`
rule id when a value was derived rather than filed, e.g. `"T-118"`; `NULL` for a filed fact —
`fundamental_agent.statements.iter_facts` sets it, `db.append_financial_facts` persists it), and
`manifest_json` on `quant_risk_model` / `quant_portfolio` (T-090: the input versions a
model or book was built on; NULL on rows written before it), and `forensic_flags_json` on
`score_snapshot` (T-041: the fundamental synthesis's four forensic booleans, written by
T-074; m005/m006 carry it through their table rebuilds).
`m002` / `m003` carry `run_id` forward in their rebuilds so a not-yet-migrated dev
DB does not drop it (no new migration, no `schema_version` bump).

### `queries.py` — every SQL query in the domain, in one place

Consolidates what used to be split across `version.py` / `universe_source.py` /
`universe_membership.py`:

- **`universe.db` reads (read-only, never written here):** `connect_ro`
  (re-exports `kg_schema.db.connect_ro`), `members_asof(universe_db,
  analysis_date, *, universe="SP500") -> list[UniverseMember]` (predicate
  `valid_from <= D AND (valid_to IS NULL OR valid_to > D)`, deduped by symbol
  keeping the latest stint; rejects any universe other than `SP500`),
  `symbols_asof(...)`, and `resolve_asset_ids(financial_db, symbols) ->
  (mapping, missing)` (pure read, case-insensitive `assets.ticker` match,
  chunked `in_clause` — creating a brand-new `assets` row stays with the
  agents' `sync_universe`).
- **Data-coverage report:** `check_coverage(fin_db, universe_db, as_of, *,
  universe="SP500", min_observation_days=504) -> CoverageReport` and
  `persist_coverage(fin_db, report) -> int` (upserts one `universe_coverage`
  row per member). The report *shape* (`SymbolCoverage` / `CoverageReport`
  dataclasses, no SQL) lives in `coverage.py`.
- **Metric versions:** `metric_versions_present(db) -> {group: [engine_version, ...]}` —
  the versions actually stored in `fundamental_metrics` (a NULL `engine_version` predates
  versioning and is ignored; empty for a database without the table). The input to
  `versions.py`'s resolver.
- **`schema_version`:** `ensure(db)`, `current_version(db) -> int`,
  `record(db, version, description)` — the monotonic floor other repos assert
  against.
- **`universe_membership`:** `reconcile(db, universe, present_asset_ids, *,
  as_of, run_id=None, run_kind=None, source) -> (opened, closed)` — opens
  memberships for newcomers, closes them for the departed. No longer on any
  write path (see `v_universe_membership` below), kept for compatibility.

### `market_cap.py` — market capitalisation as of a date (T-132)

The one reader `cycle` and `quant` share: `market_caps_as_of(db, asset_ids, *, as_of,
max_share_age_days=200, max_price_age_days=10, corpact_engine_version="corpact-v1") ->
MarketCapResult` = the latest `filing_cover_shares` count from a filing already usable on
`as_of` (`available_at`, T-107) × the last stored `price_daily.close` on or before it.

- Refuses, with a reason and never a number: `no_cover_shares`, `stale_shares` (count older
  than the limit), `no_recent_price`. `result.caps[asset_id]` is a `MarketCap` with every
  input (count, its date and age, filing, close and its date, classes summed, split factor);
  `result.missing[asset_id]` is the reason code; `result.coverage()` is the JSON summary a run
  records.
- The count is put on the price's split basis: multiplied by every `corpact-v1` SPLIT dated
  after the count and no later than the asset's newest stored bar (the date the gateway's
  adjustment reaches).
- `cover_total(entries)` is the aggregation of one filing's entries — the filer's own total
  when it filed one, else each class's newest value summed — shared with the stored valuation
  metric so the two cannot disagree. Classes are summed at the traded close, after
  `CLASS_CONVERSION` (`{cik: {class_member: units of the traded class}}`; today Berkshire's
  class A = 1,500 class B) converts a class that is not 1:1 with the traded one. `MarketCap.cik`
  identifies the issuer, so two listings of one company (GOOG/GOOGL) can be told apart.
- Raises `AvailabilityMissing` on an un-backfilled database, like every as-of reader.

### `versions.py` — metric-version selection and run manifests (T-090)

`fundamental_metrics` is append-only per `engine_version`, so parallel versions of a
metric accumulate. This module is the **one place that chooses among them**; every
reader goes through it (`cycle.data.latest_metrics`; market caps no longer read a stored
metric, see `market_cap.py` below), and `tests/test_metric_versions.py` fails on any raw
`fundamental_metrics` read in `src/` that lacks the filter. The one allowed exception is
`v_fundamental_metric` (T-144): it returns every version, flags the one this resolver picks
as `is_current`, and `tests/test_kg_view_contract.py` fails if the two ever disagree.

- `resolve_metric_versions(db, selection=None, *, groups=METRIC_GROUPS) -> MetricVersions`
  resolves a user selection against what is stored, **per metric group** (profitability,
  liquidity, leverage, efficiency, growth, cashflow, roic, cagr, valuation): no selection →
  the newest stored version of each group; `"metrics-v1"` → that version for every group
  the consumer reads that has any rows; `{"valuation": "metrics-v1"}` → per-group overrides,
  newest for the rest. **A requested version that is not stored is an error**
  (`VersionError`), never a silent fallback; so is a group the consumer does not read.
  `choose_versions(present, ...)` is the pure core; `parse_metric_selection(text)` parses
  a `--metrics-version` string (`metrics-v1` or `valuation=metrics-v1,profitability=metrics-v2`).
- **Ordering is an explicit rule, not `computed_at`:** `<family>-v<N>` sorts by the family's
  rank in `FAMILY_RANK` (`pre` < `metrics`) then by `N`, so `pre-v1` < `metrics-v1` <
  `metrics-v2` < `metrics-v10`. An unparseable string or an unregistered family is an error.
- `MetricVersions.json_param()` is the single bound parameter of `VERSION_FILTER_SQL` — a
  **static** SQL fragment (`(m.metric_group || '/' || m.engine_version) IN (SELECT value FROM
  json_each(?))`), so readers interpolate nothing (constitution Code & Git #10). An empty
  selection reads nothing, never everything.
- `manifest_tag(manifest)` — 8 hex chars of a stable hash of the run's input versions.
  The same inputs give the same tag; different inputs give a different one.
- **Constraints (T-093).** `parse_constraint(text, key=...)` parses `=v` / bare `v` (exact),
  `>=v`, `!=v`, comma-separated combinations and `latest` into a `Constraint`;
  `pick_version(stored, constraint, key=..., what=...)` returns the highest stored version
  satisfying it, or raises a `VersionError` naming why each candidate was rejected.
  `parse_metric_constraints` / `choose_constrained_versions` extend the metric selection with
  per-group constraints, and hand every T-090 form to `parse_metric_selection` /
  `choose_versions` unchanged. `engine_version_key(family)` orders one engine's versions
  (`qret-v2` < `qret-v10`) and rejects another family; `recognized_engine_versions` separates
  them from history strings like `corpact-v1-derived`. `quant` uses all of these; `cycle`
  still uses only the exact T-090 functions.

Passive like the rest of the package: it reads and computes, never writes.

### `coverage.py` — pure business logic, no SQL

`SymbolCoverage` / `CoverageReport` dataclasses and their roll-up properties
(`covered`, `missing`, `missing_required`, `fraction`, `uncovered`,
`missing_for`). The query layer that fills these in lives in `queries.py`.

### `migrations.py` — `apply_migrations(db) -> list[int]`

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
| m006 | rename `score_snapshot.score_type` `'QUANTITATIVE'` → `'VALORIZATION'` everywhere it is persisted: the CHECK, the stored rows, and the score-type keys inside `cycle_run.params_json` / `cycle_ranking.components_json` |
| m007 | `quant_portfolio` book key made NULL-safe (`T-101`): per duplicate `(as_of, kind, IFNULL(frontier_k, -1), engine_version)` group keep the oldest id (it holds the positions), refresh it with the newest copy's metadata, move `quant_frontier_point` references onto it, drop the newer copies with their positions and forward-performance rows; then `CREATE UNIQUE INDEX ux_quant_portfolio_book` on that NULL-safe key |
| m008 | `available_at` (`T-107`): re-adds the column to `sec_filings` / `fundamental_metrics` / `score_snapshot` (older rebuilds drop it), backfills each dated filing with the first NYSE trading day after its `filing_date` (`kg_schema.trading_calendar.available_from`) and copies it onto the filing's metrics and FUNDAMENTAL scores, then restores the `AVAILABILITY_TRIGGERS` guards. Refuses (rolls back) if a metric or FUNDAMENTAL score is left without one. `apply_migrations` drops those guards while any migration runs and restores them afterwards |
| m009 | `veto` stints (`T-125`): collapses the old per-`(asset, rule, cycle_date)` hit rows into `raised_on`/`cleared_on`/`last_seen_on` stints, using the cycle dates the `"veto"` checkpoint step actually completed on (any `cycle_run`, any `cycle_type`) as the evaluation timeline — a stint opens on a pair's first hit date, extends across consecutive hit dates, and closes at the first evaluation date with no hit row for that pair, reopening a new stint if hit again later. `kg_schema._ensure_veto_indexes` (not this migration) then creates the two indexes naming the new columns, since they cannot ship inline in `ADDITIVE_DDL` without breaking its no-op safety against a still-unmigrated database |
| m010 | marker for the knowledge-graph view contract (`T-144`): changes no table. The new views and appended columns come from `views.py` on every `ensure`; this raises the `schema_version` floor to 10 so the knowledge-graph repo can assert it and `ensure_views` can tell a database that is ahead of the running code (see "The knowledge-graph view contract" below) |

### `views.py` — `VIEWS`, `ensure_views(db)`

`ensure_views` drops and recreates every view on each call (cheap, always current).
Because SQLite's `CREATE VIEW` does not validate its base tables, each view is
probed with a zero-row `SELECT` right after creation and **dropped** if a base
table is absent in this (partial / single-agent) DB — a dangling view would
otherwise break the view re-parse a later `ALTER TABLE … RENAME` in a migration
performs. The module docstring is the **projection contract** — column list +
semantics per view. Views:
`v_score_snapshot`, `v_fundamental_metric` (every stored metric with `is_current`, T-144),
`v_sector` (GICS sector rollup with
member/sub-industry counts), `v_industry` (sub-industry → sector; the scrape has
no middle industry-group tier), `v_price_observation` (newest `engine_version` per
asset/day), `v_sec_filing` (one row per filing), `v_sec_filing_section` (carries
`item_label`, the ontology `itemLabel` token — `ITEM_1A_RISK_FACTORS`, `ITEM_7_MDA`,
… — built in SQL, kept identical to
`fundamental_agent.sections.canonical_item_label`), `v_veto`,
`v_rule_catalog` (veto rules as data — `params_json` verbatim plus unpacked
`param_metric` / `param_operator` / `param_threshold`), `v_data_quality_issue` (Ring-1
`DQ_*` gate hits with the filing's form / period, T-065), `v_portfolio_position`,
`v_shared_executive_edge` (pair-level aggregate), `v_cycle_ranking`,
`v_cycle_ranking_component` (one row per non-null component of every ranking row, T-144),
`v_weight_scheme` (one row per `cycle_run` that recorded a blend — scheme id +
scalar knobs), `v_weight_component` (that blend exploded to one row per
`(cycle_run, score_type)`), `v_sector_aggregate_snapshot` (per-cycle mean of
members' TECHNICAL score per sector).

`quant/` (Markowitz benchmark) adds: `v_corporate_action`, `v_quant_return_daily`,
`v_risk_free_rate`, `v_benchmark_series` (each newest `engine_version` per key),
`v_quant_risk_model` (model metadata; μ / Σ stay internal), `v_quant_portfolio`,
`v_quant_position` (book weight stints), `v_quant_frontier_point`,
`v_quant_benchmark_performance`, and `v_quant_vs_live` (each optimized book beside
the live `portfolio_position` weights, per name, plus a `kind = 'LIVE_ONLY'` row for each
live name that no optimized book of that as-of date holds — T-042).

**Run-log views** for `portfolio-reports`: `v_analysis_run`, `v_pricing_run`,
`v_quant_run`, `v_cycle_run` — one row per agent run with `run_id`, `as_of`
(`cycle_date` for cycle), `code_version`, status, timings and `params_json`.

**`v_universe_membership` is frozen.** The agents no longer write
`universe_membership` (the universe is read point-in-time from `universe.db`), so
this view is stale unless something else populates the table. Downstream readers
(the KG projection) should move to `universe.db` / `kg_schema.queries`. The set is machine-readable as
`kg_schema.views.FROZEN_VIEWS` (a test fails on a name that is not in `VIEWS`) and is published as `frozen` by
the API's `/contract`.

`schema_version(db)` is the public read of the recorded floor: `MAX(version)`, `0` when the table is missing or
empty, and it never creates the table (`queries.current_version` does), so it works on a `mode=ro`
connection. `ensure_views` and the API's `/contract/database` both use it.

### `coverage` command

Exposed as `python -m {fundamental_agent,pricing_agent,quant} coverage
--analysis-date D [--strict] [--min-fraction F] [--print-fill-commands]` (shared
impl `kg_schema.cli.run_coverage`, registered via `kg_schema.cli.add_coverage_parser`).
Read-only; default **warn** (report + exit 0), `--strict` exits 1 when the
covered fraction is below `F`. This is the guard for the gap that reading
agents (`cycle`, `quant`) otherwise hit silently — a member of the as-of
universe with no data to analyse.

### `cli.py` — `run_migrate(db_path) -> int`, `run_coverage(...) -> int`

Shared `migrate` / `coverage` implementations. `run_migrate` opens a
connection, calls `ensure(db, run_migrations=True)`, prints the
`schema_version` table.

## The knowledge-graph view contract (T-144)

One additive change to the `v_*` views, for the knowledge-graph repo. **Rule:** every column a
view already had keeps its name, position and values; new columns go at the end. The one value
change is `v_score_snapshot.available_at` on TECHNICAL, VALORIZATION and SECTOR rows (below).
`tests/test_kg_view_contract.py` pins every pre-existing view's column list and order.

### The contract's versioning rule

Published by the API as `GET /api/v1/contract` and `/contract/database` (`docs/api.md`, T-152):

- Within one `contract_version` (the highest version in `MIGRATIONS`), a view's columns are **only added, at
  the end**; never renamed, removed or reordered. A change that does any of those needs a new version.
- **Every migration raises `contract_version`**, whether or not it changes a view — so a consumer asserting
  a floor always has a version to assert.
- A database whose `schema_version` is below the code's `contract_version` **lags its code**. The views follow
  the code on every `ensure`, the floor follows `migrate`; `/contract/database` shows the difference.

### Versions and deployment

- `schema_version` **10** (`m010`, a marker: no table changes). The views themselves are rebuilt
  by every `ensure`, so a database gets them as soon as any process on the new code opens it,
  whether or not it has been migrated; `migrate` is what raises the floor.
- **Every process that opens the database is upgraded (or stopped) before `migrate` runs.**
  An older process that runs `ensure` against a migrated database drops and recreates the views
  from its own, older definitions and so removes the new columns and views. The new code's
  `ensure_views` guard (skip when `schema_version` is above the highest migration it knows)
  protects *later* code from the *next* contract change; it cannot retrofit code that predates it.
- The production database (`/workspaces/thesis/data/financial.db`) is frozen until `T-100` and
  stays at its current `schema_version`; a consumer must not raise its floor to 10 for it.

### New views

- **`v_fundamental_metric`**: `ticker`, `asset_id`, `filing_id`, `metric_group`, `metric_name`,
  `metric_id` (`metric_group || '.' || metric_name`; joins to `v_rule_catalog.param_metric`),
  `unit`, `value`, `engine_version`, `is_current`, `event_time`, `available_at`, `run_id`. One row
  per (filing, metric_group, metric_name, engine_version); `available_at` is the filing's.
  `is_current` is 1 for the engine version that `kg_schema.versions.resolve_metric_versions` picks
  with no explicit selection: the newest version **per metric group** by the explicit order
  (`pre-v1` < `metrics-v1` < `metrics-v2` < `metrics-v10`), never by `computed_at`. A filing that
  was not recomputed under it has no current row. Only exactly `<family>-v<digits>` of a group in
  `versions.METRIC_GROUPS` can be current: a string with trailing text, an unregistered family, or a
  group the resolver does not read never is. Two spellings of one number (`metrics-v02`,
  `metrics-v2`) tie-break on the string. At most one current version per group, so at most one current row per
  (filing, metric_group, metric_name).
- **`v_cycle_ranking_component`**: `cycle_run_id`, `asset_id`, `score_type`, `component_value`,
  `configured_weight`, `effective_weight`. One row for every **non-null** component of every
  `v_cycle_ranking` row, vetoed or not (`rank` excludes no one; exclusion happens later, in
  `positions`). `configured_weight` is the run's weight for that score type
  (`v_weight_component.weight`; NULL when the run recorded no blend). `effective_weight` is
  `configured_weight` over the sum of the configured weights of that row's non-null components,
  so the row's effective weights sum to 1 (NULL when that sum is 0). The identity, for a row with
  at least one component:
  `blended_score = sum(effective_weight * component_value) - soft_veto_penalty * (SOFT vetoes)`,
  where `soft_veto_penalty` is the run's (`v_weight_scheme.soft_veto_penalty`) and the SOFT vetoes
  are the `veto_rules_json` entries other than `HARD` and `UNSCORED`.

### Columns added to existing views

| View | New columns (at the end) |
|---|---|
| `v_score_snapshot` | `forensic_flags_json`, `prompt_hash` (FUNDAMENTAL only; the view returns NULL on every other `score_type`) |
| `v_shared_executive_edge` | `computed_at` (`MAX` over the pair's person rows), `run_id` (`MAX`; one `entity_resolution build` writes one `run_id` per `method`) |
| `v_cycle_ranking` | `status` (the `cycle_run`'s) |
| `v_quant_vs_live` | `engine_version`, `is_current` |
| `v_quant_portfolio` | `is_current` |

- `v_score_snapshot.available_at` is the cycle date (= `event_time`) for TECHNICAL, VALORIZATION and
  SECTOR rows: a cycle-computed score is usable from its cycle date. The stored column (NULL on those
  rows) and FUNDAMENTAL rows (the filing's, `T-107`) are unchanged; SEMANTIC rows are not written
  until Work item 4.
- `v_quant_portfolio.is_current` is 1 for the newest `opt-v<N>` book per (`as_of`, `kind`,
  `frontier_k`): by `N` numerically, then `computed_at`, then `id`. `frontier_k` is part of the key
  because every `frontier_k` book of one `as_of` is its own book; for every other kind it is NULL.
  A book whose `engine_version` is not exactly `opt-v<N>` or `opt-v<N>+<tag>` is never current: in particular a
  **variant book** (a non-default estimator or a turnover cap, T-077: `opt-v2.mu-carhart.to-0.5+<tag>`) is never current,
  whatever the compute order, so only the default configuration (`equilibrium`, no cap) can be, and `v_quant_vs_live`'s
  `LIVE_ONLY` rows follow it. At most one current book
  per key.
- `v_quant_vs_live` is now every book except `kind = 'live_book'`: the dead `equal_weight` and
  `cap_weight` filters are gone (no code writes those kinds, so no existing row appears or goes).
  `engine_version` / `is_current` are the book's. A `LIVE_ONLY` row belongs to no book version: its
  `engine_version` is NULL and `is_current` is 1. A name is `LIVE_ONLY` when no **current** book of
  that `as_of` holds it (an older version that still holds it does not count), so a reader of
  `is_current = 1` rows never loses a live position. Filter `is_current = 1` for one row per
  (`as_of`, `kind`, name).
- `v_cycle_ranking` is **every** run's ranking, whatever its `status`, not "the latest cycle per
  `cycle_type`" as its old docstring said. Filter `status = 'completed'` and choose the run.

### Definitions

- **`blended_score`** is 0-100 minus the run's `soft_veto_penalty` (15 by default) per SOFT veto, so
  it can go below 0. With no components (all null) it is `0.0` minus that deduction. It is a weighted
  mean over the non-null components, i.e. weights renormalized (`effective_weight` above).
- **`normalized_score`** is `50 + 10z` over the cohort (z against the 2%-winsorized mean and standard
  deviation), clamped to [0, 100]; the same function for SECTOR. The cohort mean is near 50, not
  exactly 50. FUNDAMENTAL's `normalized_score` is **rewritten by every cycle** (`T-106`), so the stored
  value is the last cycle's; the per-cycle value is `component_value`.
- **SECTOR `raw_value`** is the asset's TECHNICAL raw score minus its sector's mean, in TECHNICAL
  points, [-100, 100] (TECHNICAL raw is 0-100).
- **Units** (`fundamental_metrics.unit`, `v_fundamental_metric.unit`): `ratio` is a fraction (0.05 =
  5%), `x` a multiple, `usd` US dollars.
- **Weights and caps are fractions of the book** (0.05 = 5%). `max_name_weight` /
  `max_sector_weight` in `v_weight_scheme` are the **effective** caps on SELECTION runs (since Work
  item 18) and the **configured**, possibly NULL, ones on MONITORING runs.
- **`scheme_id`** (`v_weight_scheme`) is the position-weighting rule (`score_proportional`,
  `score_tilt`, ...), not the blend. A blend is identified by its `cycle_run`; its configured weights
  are `v_weight_component`.
- **No component** means all components are null (`blended_score` 0.0 before the penalty). Such a
  ranking row has no `v_cycle_ranking_component` rows.
- **`vetoed`** is true only for a HARD veto (with the T-1 lag) or `UNSCORED`; a SOFT veto only lowers
  `blended_score`.
- **Timestamps.** Every `*_at` timestamp that a view exposes (`computed_at`, `detected_at`,
  `cleared_at`, `retrieved_at`, `started_at`, `finished_at`, `ingested_at`, `created_at`) is ISO 8601
  UTC written with a `+00:00` offset (`2026-10-04T18:28:56+00:00`). `event_time`, `available_at`,
  `raised_on`, `cleared_on`, `last_seen_on`, `cycle_date`, `as_of` and `obs_date` are plain dates
  (`YYYY-MM-DD`): `available_at` is a trading date, not a moment.
- **Forensic flags** (`forensic_flags_json`, `T-041`; written by Work item 8's `T-074`, NULL until
  then): a JSON object of four booleans, `data_error_suspected`, `negative_equity_buyback`,
  `value_destroyer_sub_wacc`, `severe_sbc_dilution`. The codes of a row are the keys that are `true`.

## Tables added

| Table | Purpose | Immutability key |
|---|---|---|
| `universe_membership` | S&P 500 membership history (**frozen** — superseded by `universe.db`) | `UNIQUE(asset_id, universe, valid_from)` |
| `universe_coverage` | per-member core-data coverage for a dated universe (from `coverage`) | `UNIQUE(as_of, universe, symbol)` |
| `score_snapshot` | `ScoreSnapshot` types FUNDAMENTAL / VALORIZATION / TECHNICAL / SEMANTIC / SECTOR | `UNIQUE(asset_id, score_type, event_time)` |
| `rule_catalog` | veto rule definitions | `rule_id` PK |
| `data_quality_issue` | Ring-1 `DQ_*` gate hits (T-040 / T-065): severity, `quarantined`, the gated value; written by `fundamental_agent`, read by `cycle` | `UNIQUE(filing_id, metric_group, metric_name, metric_engine_version, rule_id, gate_version)` |
| `veto` | rule-hit stints (`raised_on`/`cleared_on`/`last_seen_on`, T-125), closed not deleted; at most one open stint per `(asset_id, rule_id)` (partial unique `WHERE cleared_on IS NULL`) | `UNIQUE(asset_id, rule_id, raised_on)` |
| `portfolio_position` | the live position stints; triggers reject `valid_to < valid_from` (T-104) | `UNIQUE(asset_id, valid_from)` |
| `portfolio_position_replay` | `cycle backfill`'s simulated book -- same shape/triggers as `portfolio_position`, never read by or refused for conflicting with it (T-115) | `UNIQUE(asset_id, valid_from)` |
| `cycle_run` / `cycle_checkpoint` | orchestrator provenance + resume; `cycle_type` is `'SELECTION'`\|`'MONITORING'`\|`'ENTITY_RESOLUTION'`\|`'REPLAY'` | `UNIQUE(cycle_type, cycle_date)` / `UNIQUE(cycle_run_id, step)` |
| `cycle_ranking` | the ranked cohort of a cycle | `UNIQUE(cycle_run_id, asset_id)` |
| `price_observation` | derived per-day price analytics | `UNIQUE(asset_id, obs_date, engine_version)` |
| `filing_cover_shares` | the filing's own cover-page share count (`dei:EntityCommonStockSharesOutstanding`), one row per class and date; `class_member = ''` is a single-class count or a filer's own total (T-132) | `UNIQUE(filing_id, class_member, as_of_date)` |
| `sec_filing_section` | narrative filing text | `UNIQUE(filing_id, section_type, ordinal, engine_version)` |
| `shared_executive_edge` | `sharedExecutiveWith` candidates | `UNIQUE(asset_id_a, asset_id_b, person_name, method)` |
| `media_cooccurrence` | the same shape for press / analyst co-occurrences, kept apart from executive edges (T-043; written once T-082 lands) | `UNIQUE(asset_id_a, asset_id_b, person_name, method)` |
| `sector_aggregate_snapshot` | per-cycle GICS-sector roll-up of members' TECHNICAL score | `UNIQUE(sector_id, cycle_date, metric_type)` |
| `corporate_action` | dividends / splits (the pricing gateway only; legacy derived rows are unread history) | `UNIQUE(asset_id, action_type, ex_date, engine_version)` |
| `quant_return_daily` | total-return daily series (dividends folded in) | `UNIQUE(asset_id, obs_date, engine_version)` |
| `risk_free_rate` / `benchmark_series` | rf curve + benchmark index for `quant/` | `UNIQUE(curve, rate_date, engine_version)` / `UNIQUE(benchmark, obs_date, engine_version)` |
| `quant_risk_model` / `quant_expected_return` / `quant_covariance` | Markowitz μ / Σ per as-of model; `model_version` = base + manifest tag (`rm-v2+3f9a1c2b`, T-090; `rm-v1` before T-077) | `UNIQUE(as_of, model_version)` / `…(model_id, asset_id, mu_model)` / `…(model_id, asset_id_i, asset_id_j)` |
| `quant_portfolio` / `quant_position` / `quant_frontier_point` | optimized benchmark books + frontier; `engine_version` = base + manifest tag (`opt-v1+3f9a1c2b`), `manifest_json` records the inputs | `UNIQUE(as_of, kind, frontier_k, engine_version)` / `…(portfolio_id, asset_id, valid_from)` / `…(model_id, k)` |
| `quant_benchmark_performance` | forward realized / active return of a frozen book | `UNIQUE(portfolio_id, date, engine_version)` |

## Gotchas

- **`ensure` is a hard dependency of every package.** Keep it idempotent and
  additive-only; `ensure_views` swallows `OperationalError` so a view bug can't
  brick a batch run.
- **Shared-DB migration runbook:** quiesce all writers → `cp financial.db{,.bak}` →
  `python -m fundamental_agent migrate` once → check `SELECT * FROM schema_version`
  → resume. `-wal` / `-shm` files may exist even though this code forces rollback
  journal; standardise journal mode across writers before running m002–m008.
- **Never drop the `fundamental_snapshot` compat view** until every external
  consumer has moved to `v_score_snapshot`.
- **`Database` is not a `sqlite3.Connection` subclass** (composition, not
  inheritance) — a connection-specific escape hatch (`set_trace_callback`,
  etc.) goes through `db.raw`, not `db` directly.
