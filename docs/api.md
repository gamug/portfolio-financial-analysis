# `api/`

A thin, **read-only** FastAPI surface over this repo's outputs — the `v_*`
read-contract views in `KG_FINANCIAL_DB` and the point-in-time universe in
`universe.db`. It exists so `portfolio-reports` / `portfolio-app` (and ad-hoc
callers) have a stable network boundary instead of opening the SQLite files
directly.

The agents stay CLI-driven and own every write. **Nothing in `api/` mutates a
database** — it only ever opens them `mode=ro`.

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
| `GET /portfolio/ranking?cycle_type=SELECTION` | the ranked cohort of the most recent cycle of that type (`v_cycle_ranking`) |
| `GET /contract` | the `v_*` view contract the running code defines — `contract_version`, `code_version`, every view with its `frozen` flag and columns in order. Metadata only, no rows ([below](#the-view-contract-contract-t-152)) |
| `GET /contract/database` | what the connected database has of that contract — `schema_version`, `views_present`, `views_missing` |

`GET /` redirects to `/docs`.

## Structure

```
src/api/
  app.py            create_app(settings=None) -> FastAPI   (the factory)
  __main__.py       python -m api  ->  uvicorn.run(create_app, factory=True)
  config.py         ApiSettings
  db.py             connect_ro() + rows() helper (missing view -> [])
  dependencies.py   get_settings / get_db / get_universe_db / page_params
  models.py         response models (views passed through verbatim return dicts)
  contract.py       build_contract(): the v_* view contract, built once per process (T-152)
  routers/          health · runs · universe · scores · portfolio · contract
```

## The view contract (`/contract`, T-152)

Two read routes that publish the `v_*` contract as **metadata, never rows**, so a consumer (the
knowledge-graph repo) can check a running service instead of a checkout of this repo. They are
ordinary read routes: the database is opened `mode=ro` through the same dependency as the others, nothing is
written, and they carry no authentication beyond what the other read routes have (access control is the
deferred Work item 20). Unlike the pass-through data routes, their responses are pydantic models
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

## Extending it

- **New read**: add a router module under `routers/`, register it in
  `routers/__init__.py:ALL`. Prefer returning the `v_*` view verbatim (a `dict`)
  and adding a concrete model in `models.py` only where the shape is stable.
- **Triggering agent runs over HTTP** is deliberately *not* here — that is
  `portfolio-reports`' job. If ever added it must be a separate, explicitly
  auth-guarded router, and it would shell out to the CLIs / call the pipelines
  rather than duplicate their logic.
