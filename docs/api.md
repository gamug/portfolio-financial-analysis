# `api/`

A thin FastAPI surface over this repo's outputs — the `v_*` read-contract views
in `KG_FINANCIAL_DB` and the point-in-time universe in `universe.db` on the read
side, plus (from Work item 2, `T-011`) the run endpoints that sequence the agent
CLIs. It exists so `portfolio-reports` / `portfolio-app` (and ad-hoc callers) have
a stable network boundary instead of opening the SQLite files directly.

The agents stay CLI-driven and own every write. The read endpoints — and the run
**status** route — only ever open a database `mode=ro`; a run endpoint starts each
step as a subprocess, so the API process itself opens no write-capable connection.
Access control for the run endpoints is deferred (SPEC FR-015): they are for the
dev container / pilot only.

```bash
uv run python -m api                                  # serve on API_HOST:API_PORT (default 0.0.0.0:8010)
uv run uvicorn --factory api.app:create_app --reload  # dev, autoreload
# interactive docs at  /docs  ·  OpenAPI at  /openapi.json
```

## Configuration (`config.py`)

`ApiSettings.load()` needs `KG_FINANCIAL_DB` (via `kg_schema.env.database_path`).
Optional: `KG_UNIVERSE_DB` (default `/workspaces/thesis/data/universe.db`),
`API_HOST` (default `0.0.0.0`), `API_PORT` (default `8010` — the data-mining
gateway owns `:8000`–`:8005`), `API_ROOT_PATH` (when mounted behind a proxy).

## Endpoints (`/api/v1`)

| Method & path | Returns |
|---|---|
| `GET /health` | liveness (`status`, `version`) |
| `GET /health/db` | a read probe of both DBs — `ok`, `schema_version`, `universe_db_ok` |
| `GET /runs` | the per-agent run log — `run_id`, `kind`, `as_of`, `code_version`, `status`, timings. `?kind=analysis\|pricing\|quant\|cycle`, `?status=`, `?limit`/`?offset`. Reads `v_analysis_run` / `v_pricing_run` / `v_quant_run` / `v_cycle_run` |
| `GET /universe?as_of=YYYY-MM-DD` | the S&P 500 roster as of that date, from `universe.db` (`?universe=SP500`) |
| `GET /universe/coverage?as_of=YYYY-MM-DD` | per-member core-data coverage — the persisted `v_universe_coverage` rows if the `coverage` command has been run for that date, else computed live (never written). `source` is `persisted` or `computed` |
| `GET /scores` | `v_score_snapshot` rows. `?ticker=`, `?score_type=`, `?as_of=` (rows already available on D: `available_at <= D`, else `event_time <= D` for cycle scores), paged |
| `GET /portfolio/positions` | `v_portfolio_position` — `?open_only=true` (default) or `?as_of=` for the stint open then (`as_of` takes precedence over `open_only`) |
| `GET /portfolio/ranking?cycle_type=SELECTION` | the ranked cohort of the most recent completed cycle of that type (`v_cycle_ranking`) |
| `GET /portfolio/quant-vs-live` | optimized benchmark weights beside live positions (`v_quant_vs_live`). `?as_of=` (newest book date on or before, no lookahead), `?kind=` (that kind plus `LIVE_ONLY`), `?current_only=true` (default; `false` includes variant books). Live-only positions included ([below](#optimized-vs-live-books-portfolioquant-vs-live-t-083)) |
| `GET /contract` | the `v_*` view contract the running code defines — `contract_version`, `code_version`, every view with its `frozen` flag and columns in order. Metadata only, no rows ([below](#the-view-contract-contract-t-152)) |
| `GET /contract/database` | what the connected database has of that contract — `schema_version`, `views_present`, `views_missing` |

`GET /` redirects to `/docs`.

## Structure

```
src/api/
  app.py            create_app(settings=None) -> FastAPI   (the factory)
  __main__.py       python -m api  ->  uvicorn.run(create_app, factory=True);
                    python -m api orchestrate --analysis-date D  ->  the CLI wrapper
  config.py         ApiSettings (db_path / universe_db_path / news_db_path)
  db.py             connect_ro() + rows() helper (missing view -> [])
  dependencies.py   get_settings / get_db / get_universe_db / page_params
  models.py         response models (views passed through verbatim return dicts)
  contract.py       build_contract(): the v_* view contract, built once per process (T-152)
  orchestrator.py   the step table, the runner, run-id capture, the CLI wrapper (T-011)
  routers/          health · runs · universe · scores · portfolio · contract ·
                    run (the POST run endpoints) · run_status (GET state)
```

## The view contract (`/contract`, T-152)

Two read routes that publish the `v_*` contract as **metadata, never rows**, so a consumer (the
knowledge-graph repo) can check a running service instead of a checkout of this repo. They are
ordinary read routes: the database is opened `mode=ro` through the same dependency as the others, nothing is
written, and they carry no authentication beyond what the other read routes have (access control is the
deferred Work item 20). The only connection that isn't `mode=ro` on a read path is a private `:memory:`
database. It holds no data, is opened and closed inside `build_contract`, and never touches
`KG_FINANCIAL_DB` or any other file. Unlike the pass-through data routes, their responses are pydantic models
(`models.py`): the shape is itself the contract.

### `GET /api/v1/contract` — what the code defines

```json
{
  "contract_version": 10,
  "code_version": "6bf4d7e",
  "views": [
    {"name": "v_score_snapshot", "frozen": false, "columns": [{"name": "id"}, {"name": "ticker"}, "…"]},
    {"name": "v_universe_membership", "frozen": true, "columns": [{"name": "id"}, {"name": "ticker"}, "…"]}
  ]
}
```

| Field | Meaning |
|---|---|
| `contract_version` | the highest migration version in `kg_schema.migrations.MIGRATIONS` — the `schema_version` a database must reach to match this code |
| `code_version` | `kg_schema.provenance.code_version()`: the git short sha (`-dirty` if the tree has uncommitted changes) or the package version |
| `views` | **every** view of `kg_schema.views.VIEWS`, in that order |
| `views[].name` | the view's name |
| `views[].frozen` | `true` for a view no agent writes any more (`kg_schema.views.FROZEN_VIEWS`; today `v_universe_membership`) — kept for back-compat, do not build on it |
| `views[].columns` | the view's columns **in order**, each `{"name": …}` (no type: SQLite views are untyped) |

The columns are read from the code, not from a list kept beside it: the views are created in an in-memory
database (the agents' tables plus `kg_schema.ensure(run_migrations=True)`) and each one's columns are read back. If
a view of `VIEWS` cannot be built there, the route fails rather than answer with a shorter contract. The
result depends only on the code, so it is computed once per process. (`quant` is a leaf package `api` may not
import, so `api/contract.py` declares its one table a view reads, `quant_run`;
`tests/test_api_contract.py` fails if that declaration drifts from `quant.db.SCHEMA`.)

### `GET /api/v1/contract/database` — what the database has

```json
{
  "schema_version": 9,
  "views_present": ["v_score_snapshot", "v_universe_membership", "…"],
  "views_missing": ["v_fundamental_metric", "v_cycle_ranking_component"]
}
```

| Field | Meaning |
|---|---|
| `schema_version` | `MAX(version)` of the connected database's `schema_version` table; `0` only when that table is missing (or empty). The route never creates the table |
| `views_present` | the views of `VIEWS` that exist in the connected database, in `VIEWS` order |
| `views_missing` | the views of `VIEWS` that do not — a partial database (a package's tables not created yet) drops the views over them. Together with `views_present` they partition `VIEWS` |

### Reading the two together

- Within one `contract_version`, **columns are only added** — at the end of a view — and never renamed,
  removed or reordered. A consumer that reads columns by name, or by position up to the columns it knows, is
  unaffected by an addition.
- **Every migration raises `contract_version`**, whether or not it changes a view (`m010` changed no table; it
  only marked the contract).
- A database whose `schema_version` is **below** the code's `contract_version` **lags its code**: the views
  are rebuilt by every `ensure`, so it may already show the new views and columns, but the floor has not been
  raised (`migrate` has not run). A `schema_version` **above** the code's is a newer database than the code
  that is serving it.
- **`/contract/database` reports which views exist, not their columns.** A database that lags its code can
  have views whose columns differ from `/contract`'s lists. For example, on a production copy at
  `schema_version` 8:
  - `v_veto` there still has `cycle_date` and lacks `raised_on`, `cleared_on` and `last_seen_on`. Migration
    `m009` made that change, which is why it raised the version to 9.
  - Five views lack only T-144's appended columns.
  So a consumer of a lagging database reads only the columns its version has.

## Optimized vs live books (`/portfolio/quant-vs-live`, T-083)

`GET /api/v1/portfolio/quant-vs-live` passes through `v_quant_vs_live` verbatim to serve benchmark weights beside live portfolio positions, resolving the most recent book date on or before `as_of` (no lookahead; omitted -> newest book date in the view).

`LIVE_ONLY` rows represent names held in the live `portfolio_position` book that are **held by no current optimized book on that date**, not per kind. For example, a name held by `tangency` but not by `min_var` has no `min_var` row (its benchmark weight in `min_var` is zero and it is not `LIVE_ONLY`). To compare one book with the live book, read all rows of that book's `kind` plus `LIVE_ONLY`:

- Passing `?kind=<name>` returns that kind's rows **plus** that date's `LIVE_ONLY` rows, so live-only positions are never filtered out.
- Passing `?kind=LIVE_ONLY` returns only `LIVE_ONLY` rows.
- `current_only=true` (default) filters `is_current = 1` (`LIVE_ONLY` rows are always `is_current = 1`); passing `false` includes variant books as well.

## Run endpoints (Work item 2, T-011)

The repo's single entry point for orchestrated and remote runs (constitution Tech stack #3, SPEC
FR-014). Each step is started as the same `python -m <package>` subprocess the CLI uses — not an
import — so every child keeps its own guards (the clean tree T-114, the price spine T-110, the
data-quality gate version, the construction/resume checks) and its own write connection. The API
process opens no write-capable connection: it only reads a step's own run-log row back (`mode=ro`)
to report its `run_id`.

### `POST /api/v1/run/{pipeline,<step>}`

| Route | Runs | Body fields beyond `analysis_date` |
|---|---|---|
| `POST /api/v1/run/pipeline` | the 9 steps in order | `top_n`, `weight_scheme`, `max_name_weight`, `max_sector_weight` |
| `POST /api/v1/run/pricing-agent` | `pricing_agent run --store-daily --observations` | — |
| `POST /api/v1/run/fundamental-agent` | `fundamental_agent run` | — |
| `POST /api/v1/run/entity-resolution` | `entity_resolution build` | — |
| `POST /api/v1/run/cycle/select` | `cycle select` | `top_n`, `weight_scheme`, `max_name_weight`, `max_sector_weight` |
| `POST /api/v1/run/cycle/monitor` | `cycle monitor` | — |
| `POST /api/v1/run/quant/backfill-actions` | `quant backfill-actions` | — |
| `POST /api/v1/run/quant/build-returns` | `quant build-returns` | — |
| `POST /api/v1/run/quant/build-risk-model` | `quant build-risk-model` | `top_n`, `max_name_weight`, `max_sector_weight` |
| `POST /api/v1/run/quant/optimize` | `quant optimize` | `top_n`, `max_name_weight`, `max_sector_weight` |
| `POST /api/v1/run/quant/evaluate` | `quant evaluate` | — |

- `analysis_date` (`YYYY-MM-DD`) is required on every route. `top_n` is an int ≥ 1; the two caps are
  in `(0, 1]`; `weight_scheme` is one of cycle's schemes (`score_tilt`, `equal`, `score_proportional`,
  `inverse_vol`). A setting not given is not passed, so the CLI's own default applies. A value a
  route does not accept — and any other field (a preference flag, an `--allow-*` override, a `--db` /
  `--universe-db` / `--news-db`, `universe`, `tickers`) — is `422` (`extra="forbid"`). The preference
  flags, every `--allow-*` override, `--force`/`--fresh` and `migrate` cannot be built at all: the
  step table and its argv builder have no slot for them.
- **The database paths come from the server's settings, never the request** (D7): `KG_FINANCIAL_DB`
  (`ApiSettings.db_path`), `KG_UNIVERSE_DB` (`universe_db_path`) and `KG_NEWS_DB` (`news_db_path`,
  resolved exactly as `entity_resolution` does). So the final pilot's 20-ticker sample is run by
  starting the API with `KG_UNIVERSE_DB` at a 20-ticker `universe.db`; a caller cannot point a run at
  another database. The same three paths are exported to each child *and* passed as
  `--db`/`--universe-db`/`--news-db`. No secret is stored in the run state or logged.
- A route answers **`202`** immediately with
  `{"handle": "<uuid4>", "status_url": "/api/v1/run/<handle>"}` — the request is never held open; the
  run continues in a background thread.
- **One run at a time per process.** A `POST` while a run is active is **`409`** with the active
  `handle` and its `status_url`.
- There is **no** route for `migrate`, `cycle backfill`, `cycle undo-run` or `coverage`.

### `GET /api/v1/run/{handle}` — poll

Returns the run state: `handle`, `kind`, `analysis_date`, `settings` (only the values given),
`status` (`pending` / `running` / `completed` / `failed`), `started_at`/`finished_at`, and `steps[]`
— each step's `name`, `argv` (without the interpreter path), `status`
(`pending`/`running`/`completed`/`failed`/`skipped`/`not_run`), timings, `exit_code`, `run_id`,
`reason` and the last 40 lines of combined output. An unknown handle is **`404`**. It is a read
router: it starts no run and imports no run router.

### Current limits (until later Work item 2 tasks)

- **In-memory state (until `T-012`).** The run state lives in the API process and is lost on
  restart; `T-012` records the orchestrator-level provenance in the shared DB and makes this route
  read it.
- **First failure stops the run (until `T-013`).** The first step that exits non-zero ends the run;
  later steps are recorded `not_run`. `T-013` isolates an independent step's failure.
- **`entity_resolution` skip (D6).** If the resolved news path (`KG_NEWS_DB`, else
  `entity_resolution`'s default `/workspaces/thesis/data/urls.db`) does not exist, the step is
  recorded `skipped` with reason `no news source configured (<path> not found)`, is not run, and
  does not fail the run. The full incremental-run behaviour is `T-017`.
- **A step that exits 0 but prints no run id** (a changed CLI output format) is recorded with reason
  `run_id not captured`, so the drift shows up instead of a silent `null`.

### The data-quality gate

`cycle select` refuses a database whose `data_quality_issue` rows exist only under an older gate
version than the current one (`dq-v3`), so a cycle would read every quarantine and HARD `DQ_*` veto
as clean. The `quality` backfill is not an orchestrator step, so an orchestrated run against a
database last gated under `dq-v1`/`dq-v2` stops at `cycle_select`: the step is recorded `failed`
with the refusal (naming both versions) in its output tail, and the run stops. This is expected; the
orchestrator never passes `--allow-stale-dq-gate`. Fix a **scratch** database on the CLI —
`uv run python -m fundamental_agent quality --db <scratch db> --metrics-version <its engine version>`
— never production (re-gating production is the user's action). A run from an empty database is
unaffected: `fundamental_agent run` gates inline under `dq-v3`.

### CLI wrapper

`python -m api orchestrate --analysis-date D [--top-n N] [--weight-scheme S] [--max-name-weight F]
[--max-sector-weight F] [--db PATH] [--universe-db PATH] [--news-db PATH]` runs the same pipeline in
the foreground over the same code (the same step table and argv builder), printing one line per step
(name, status, `run_id`, seconds) and, on failure, the failing step's output tail. It exits `0` when
every step completed or was skipped, `1` otherwise. The paths default to the environment variables
the packages use (`KG_FINANCIAL_DB` / `KG_UNIVERSE_DB` / `KG_NEWS_DB`, the news path defaulting as
above). Plain `python -m api` (no arguments) still starts the server.

Access control for the run endpoints is **deferred** (SPEC FR-015, Work item 20): this repo is not a
production deployment, so they are for the dev container / pilot only and must not be reachable from
an untrusted network (the API binds `0.0.0.0` by default).

## Extending it

- **New read**: add a router module under `routers/`, register it in
  `routers/__init__.py:ALL`. Prefer returning the `v_*` view verbatim (a `dict`)
  and adding a concrete model in `models.py` only where the shape is stable.
- **Triggering agent runs**: see [Run endpoints](#run-endpoints-work-item-2-t-011). Add a step by
  adding a `StepSpec` to `api/orchestrator.py`'s table (and a route for it in `api/routers/run.py`);
  the builder has no slot for a preference or override flag, by design. Access control (SPEC
  FR-015) is still owed before any non-dev deployment.
