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
| `GET /scores` | `v_score_snapshot` rows. `?ticker=`, `?score_type=`, `?as_of=` (`event_time <= D`), paged |
| `GET /portfolio/positions` | `v_portfolio_position` — `?open_only=true` (default) or `?as_of=` for the stint open then |
| `GET /portfolio/ranking?cycle_type=SELECTION` | the ranked cohort of the most recent cycle of that type (`v_cycle_ranking`) |

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
  routers/          health · runs · universe · scores · portfolio
```

## Extending it

- **New read**: add a router module under `routers/`, register it in
  `routers/__init__.py:ALL`. Prefer returning the `v_*` view verbatim (a `dict`)
  and adding a concrete model in `models.py` only where the shape is stable.
- **Triggering agent runs over HTTP** is deliberately *not* here — that is
  `portfolio-reports`' job. If ever added it must be a separate, explicitly
  auth-guarded router, and it would shell out to the CLIs / call the pipelines
  rather than duplicate their logic.
