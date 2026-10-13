# `api/`

A thin FastAPI surface over this repo's outputs — the `v_*` read-contract views
in `KG_FINANCIAL_DB` and the point-in-time universe in `universe.db` on the read
side, plus (from Work item 2, `T-011`) the run endpoints that sequence the agent
CLIs. It exists so `portfolio-reports` / `portfolio-app` (and ad-hoc callers) have
a stable network boundary instead of opening the SQLite files directly.

The agents stay CLI-driven and own every write. The read endpoints — and the run
**status** route — only ever open a database `mode=ro`; a run endpoint starts each
step as a subprocess. The API process's one write path is the orchestrator recording
each run's own provenance (`orchestrator_run` / `orchestrator_step`, T-012); it never
imports a package's run code and never writes a package's tables. Access control for
the run endpoints is deferred (SPEC FR-015): they are for the dev container / pilot only.

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
| `GET /runs` | the per-agent run log — `run_id`, `kind`, `as_of`, `code_version`, `status`, timings. `?kind=analysis\|pricing\|quant\|cycle\|orchestrator`, `?status=`, `?limit`/`?offset`. Reads `v_analysis_run` / `v_pricing_run` / `v_quant_run` / `v_cycle_run` / `v_orchestrator_run` (T-012) |
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
  orchestrator.py   the step table, the runner, run-id capture, provenance, the CLI wrapper (T-011/T-012)
  routers/          health · runs · universe · scores · portfolio · contract ·
                    run (the POST run endpoints) · run_status (GET recorded state)
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
process writes only the run's own provenance (`orchestrator_run` / `orchestrator_step`, T-012),
one short write per state change, and reads a step's own run-log row back (`mode=ro`) to report
its `run_id`.

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

Returns the run's **recorded** state (T-012): `handle`, `kind`, `analysis_date`, `settings` (only
the values given), `status` (`running` / `completed` / `failed` / `abandoned`), `started_at`/`finished_at`, and
`steps[]` — each step's `name`, `argv` (without the interpreter path), `status`
(`pending`/`running`/`completed`/`failed`/`skipped`/`blocked`/`not_run`/`abandoned`), timings, `exit_code`, `run_id`,
`reason` and the last 40 lines of combined output. It reads `v_orchestrator_run` /
`v_orchestrator_step` through the read-only `get_db` dependency, so it answers from the database
and **survives an API restart**. An unknown handle — or a database without the provenance views —
is **`404`**. It is a read router: it starts no run and imports no run router. The views' columns
are unchanged, but both can now carry the value `abandoned` (T-015, as `blocked` for `T-013`).

### Current limits (until later Work item 2 tasks)

- **A killed process is reconciled by the next run (`T-015`).** A run that writes takes an exclusive
  `fcntl.flock` on `<KG_FINANCIAL_DB>.orchestrator.lock` before it records anything, and every child
  inherits the lock's descriptor: a step orphaned by a `kill -9` keeps holding the lock until it
  exits, so a re-invocation is refused (CLI exit `1`; HTTP `409` with the holder identity and "a
  previous run, or a step it started, still holds the run lock") and two runs never write the same
  step at once. Holding the lock, a run first reconciles every run a dead process left `running` /
  `pending` to `abandoned` (its running step `abandoned`, reason "the orchestrator process ended
  while this step ran (reconciled by run <handle>)"; its pending steps `not_run`) before it records
  its own row, so the first run after a crash cleans up the last one. A graceful stop -- `SIGTERM`
  to the CLI, or the API shutting down -- kills the current child and records the step `failed` with
  `stopped: SIGTERM` / `stopped: API shutdown` and the later steps `not_run`; a stop that lands
  before a step starts (during its pending check, or between two steps) records that step `not_run`
  with `<stop reason> (before this step started)`, so the reason is never lost -- T-012 accepts it as
  the failed run's cause. `SIGKILL` cannot be
  handled, so that case is the lock + reconcile above. A stopped run's `abandoned`/`failed` row is
  served by the status route as recorded -- the route itself never writes. (The `ddl.py` status
  comment still lists only `running | completed | failed`: it has been stale since T-013, which added
  `blocked`, and a DDL text change would alter `sqlite_master.sql` on fresh databases.)
- **A run that cannot record its provenance does not start.** If the `orchestrator_run` row cannot
  be written, the route answers **`500`** and the CLI exits `1`, and no child is started. If a
  write fails after the run started, the run stops (no further child starts), is recorded `failed`
  with reason `provenance write failed: <Type>: <message>`, and the active handle is cleared.
- **Per-step failure isolation (T-013).** Steps still run one at a time, in pipeline order, but a
  step that exits non-zero (or whose run raises) is `failed` and the run continues with the next
  step. A step is `blocked` — no child starts, timings/`exit_code`/`run_id` stay NULL — when a
  direct upstream ended `failed` or `blocked`: `cycle_select` and `quant_actions` ← `pricing` +
  `fundamental`; `quant_returns` ← `quant_actions`; `quant_risk_model` ← `quant_returns`;
  `quant_optimize` ← `quant_risk_model`; `quant_evaluate` ← `quant_optimize` + `cycle_select`;
  `entity_resolution` ← nothing. So `blocked` = an upstream failed; `not_run` = the run itself
  stopped (a failed provenance write, or `KeyboardInterrupt`/`SystemExit`). The run is `failed` if
  any step failed, else `completed`.
- **`entity_resolution` skip (D6).** If the resolved news path (`KG_NEWS_DB`, else
  `entity_resolution`'s default `/workspaces/thesis/data/urls.db`) does not exist, the step is
  recorded `skipped` with reason `no news source configured (<path> not found)`, is not run, and
  does not fail the run (see *The incremental run*, below).
- **A step that exits 0 but prints no run id** (a changed CLI output format) is recorded with reason
  `run_id not captured`, so the drift shows up instead of a silent `null`.

### The incremental run (T-017)

The **pipeline** kind (`POST /run/pipeline`, `python -m api orchestrate`) runs only what has new
upstream data; the ten single-step routes always run their step, with no check. Order and
dependencies stay T-013's. Before a step runs, the runner executes its read-only `pending`
subcommand (`python -m <package> pending --analysis-date D --db .. --universe-db ..`, plus
`--news-db` for entity_resolution): one stdout line, `pending: yes|no <evidence>`, exit 0. Each
check opens the database `mode=ro`, makes gateway GETs only, never calls the LLM, and writes nothing.

- A step **runs** when its own check says pending (reason `pending: <evidence>`), or when a direct
  upstream ran *because it was pending* and completed (reason `pending upstream: <names>`; such a
  step runs no check of its own). A check that exits non-zero, prints anything but one line, or
  outlives 3600 s is killed and counts as pending (reason `pending check failed: <cause>`); it
  never fails the run.
- Otherwise the step is `skipped` with `not pending: <evidence>`. A skipped upstream is satisfied:
  it does not block its dependents. A step with a failed/blocked upstream is `blocked`, as in T-013.
- **`cycle_select` always runs** (reason `always runs`, or `pending upstream: ...`), and its run
  does not make `quant_evaluate` pending. With nothing pending, only `cycle select` runs. Its
  reason also says `semantic: no semantic source configured` -- the semantic check has no source
  until Work item 4, does not fail and does not count as pending.
- **Properties of every signal:** it compares stored outputs with the inputs as of D (not "the last
  run succeeded", so a failed run recovers); any doubt -- a missing table, an empty database, a
  failed check -- is pending, so a fresh database makes every step pending; nothing dated after D
  counts; it never re-does its step's work.

| Step | Pending iff (as of D) |
|---|---|
| `pricing` | a member has no `price_daily` bar at the last session on or before D and the pricing gateway has any candle for it from the day after its latest stored bar (the step's start date if none) to that session; or a bar without its `priceobs-v2` observation; or bars from before a recorded split; or that session is not final yet. A member the gateway has no candle for in that window, and one whose split-jump re-fetch was refused, are listed and do not count. |
| `fundamental` | an original 10-K/10-Q filed on or before D has no FUNDAMENTAL score: stored but unscored, named by an error row, or newly listed by the EDGAR gateway (the step's own call and filters, years D-1..D, or 2022..D for a CIK with no stored filing). **A filing whose latest error is a known data failure** (`fundamental_agent/failures.py`) is not pending, and neither is a service failure that a later covering run left without a newer error (an error row from a run dated after D is ignored, and a run with no `as_of` counts as in plan: an error row never makes a filing less pending; a predecessor CIK the step does not link, because its last annual report is not listed, is dropped and shown as "unlinked predecessor"): the orchestrator does not retry a data failure; a single-step `/run/fundamental-agent` run does. |
| `entity_resolution` | the newest `ENTITY_RESOLUTION` run is not a completed build for D with the default tuning, with its edges, and with the news watermark (`discovered_urls` count/max id up to D, newest `article_entities` id) it recorded; a run from before T-017 has no watermark, so it is pending once. No news source: `skipped`. |
| `quant_actions` (the chain's one check) | returns end before the newest member bar; a default objective has no non-live book with `as_of` in [last session, D]; a book `evaluate` would score lags behind its newest forward return; or the pricing gateway lists a dividend/split that `corporate_action` lacks. Books without positions and members the gateway cannot serve are listed and do not count. |

`quant_returns` .. `quant_optimize` run exactly when `quant_actions` ran; `quant_evaluate` when
`quant_optimize` ran. The gateway checks cost about as much as their steps' no-op runs (the quant
actions check: one GET per member; the fundamental listing: two GETs per CIK-year).

**Declared blind spot (fundamental).** The check lists only the years D-1..D for a CIK that already
has stored filings. A filing of an older year that failed before it was stored and left no
accession in an error row is not seen; a single-step `POST /run/fundamental-agent` lists every year
and recovers it. The same single-step run is how a known data failure is retried.

### The data-quality gate

`cycle select` refuses a database whose `data_quality_issue` rows exist only under an older gate
version than the current one (`dq-v3`), so a cycle would read every quarantine and HARD `DQ_*` veto
as clean. The `quality` backfill is not an orchestrator step, so an orchestrated run against a
database last gated under `dq-v1`/`dq-v2` fails at `cycle_select`: the step is recorded `failed`
with the refusal (naming both versions) in its output tail, the quant steps that do not depend on it
(`quant_actions`..`quant_optimize`) still run, `quant_evaluate` is `blocked`, and the run ends
`failed`. This is expected; the orchestrator never passes `--allow-stale-dq-gate`. Fix a **scratch** database on the CLI —
`uv run python -m fundamental_agent quality --db <scratch db> --metrics-version <its engine version>`
— never production (re-gating production is the user's action). A run from an empty database is
unaffected: `fundamental_agent run` gates inline under `dq-v3`.

### CLI wrapper

`python -m api orchestrate --analysis-date D [--top-n N] [--weight-scheme S] [--max-name-weight F]
[--max-sector-weight F] [--db PATH] [--universe-db PATH] [--news-db PATH]` runs the same pipeline in
the foreground over the same code (the same step table and argv builder), printing a `<step>
running ...` line when each step starts and its result line (name, status, `run_id`, seconds) when it
ends — both flushed, so a multi-hour run is not silent — then a final summary and, on failure, the
failing step's output tail. It records the same provenance as the HTTP path (`surface = 'cli'`),
and exits `1` without starting any child if that cannot be written. It takes the same **run lock**
as the HTTP path (T-015): a second CLI run, or one alongside an HTTP run, on the same database is
refused with exit `1` and the lock-holder message, so two runs never write the same step. It reports
how many abandoned runs and steps it reconciled. A `SIGTERM` is handled: the current child is
killed, its step is recorded `failed` (`stopped: SIGTERM`), the later steps `not_run`, and the
process exits `1`; `SIGINT` keeps its own behaviour. It exits `0` when every step
completed or was skipped, `1` otherwise. The paths default to the environment variables
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
