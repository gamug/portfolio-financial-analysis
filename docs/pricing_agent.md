# `pricing_agent/`

Standalone S&P 500 daily-pricing collector. **Zero imports to/from
`fundamental_agent`.** One daily-candles request per ticker over the whole range,
then a `price_window` summary (+ optional per-year windows, raw OHLCV, and derived
per-day `price_observation` analytics).

```bash
uv run python -m pricing_agent run [--analysis-date 2021-06-30] [--tickers AAPL,NVDA] \
    [--start 2022-01-01] [--end DATE] [--by-year] [--store-daily] [--observations] [--fresh] \
    [--allow-dirty] [--allow-split-jumps]
uv run python -m pricing_agent migrate
```

`--analysis-date` (optional, default: today) drives the universe (from
`universe.db`) and the fetch upper bound: `resolved_end()` is `--end` clamped to
it, and any candle dated after it is dropped in `_store` before summarising. It is
stamped on `pricing_run.as_of` with `pricing_run.code_version`; `price_window` /
`price_daily` / `price_observation` rows carry the `run_id`. `--refresh-universe`
is a deprecated no-op.

`--observations` requires `--store-daily` (refused at the CLI and again in
`pipeline.run` itself, T-110): a `price_observation` row is a per-day analytic over a
specific stored `price_daily` bar, and `_store` builds both from the same fetched
`candles` in one call, so writing observations without also storing the matching daily
bars would leave an orphan observation date — no `price_daily` row for a day
`price_observation` claims to analyze. The observations are built from the asset's
**full stored `price_daily` history** (T-131), read back after the bars are written,
not from the run's fetched candles: a 252-day momentum needs 252 earlier bars, and
an incremental refresh used to leave 90-day vol / 252-day momentum / drawdown NULL on
every row it created.

### Price ingestion integrity (T-131)

- **Closed sessions only.** `run` refuses an end date (`--analysis-date`/`--end`) that is an
  NYSE trading day whose bar is not final — before 16:00 ET plus a 1-hour settle buffer
  (`kg_schema.trading_calendar.session_final_at`) — with `SessionNotClosed`, before the DB is
  touched. A weekend/holiday end is accepted (no session). The default `--analysis-date`
  (today, UTC) is therefore refused for most of a trading day: pass the latest final session,
  which the error names.
- **Splits.** The gateway adjusts history for a split as of the fetch date, so a window fetched
  after a split sits on a different basis from older stored rows. With `--store-daily`, a
  full-history re-fetch (from the first stored bar) happens **once** when a recorded
  `corporate_action` SPLIT postdates a stored bar written before it
  (`price_daily.ingested_at < ex_date`, or no stamp), or when the stored-plus-fetched series has
  a split-shaped jump at the run's seam that matches a recorded split. The first run after
  T-131 re-fetches every asset that has a split in its history (legacy bars have no
  `ingested_at`), once each. Re-run `quant build-returns` afterwards to rebuild their returns.
- **Refusal.** A split-shaped close jump (x2, x0.5, x3, x1/3, x4, x1/4, x10, x0.1, ±3%) in the
  series to store that survives the re-fetch — or that no recorded split explains (run
  `quant backfill-actions`) — fails that ticker (`pricing_run_error.stage = 'split_jump'`) and
  writes nothing for it. `--allow-split-jumps` accepts it, for a genuine move of that size. Only
  jumps involving a bar this run fetched are considered, so an old one is not every run's problem.

`run` refuses to write its `pricing_run` row at all when `code_version()` is dirty
(an uncommitted change under `src/`, `skills/`, `pyproject.toml`, or `uv.lock` — see
`docs/kg_schema.md`), unless `--allow-dirty`, which records why on
`pricing_run.params_json` and prints a CLI `WARNING` (T-114).

## Configuration (`config.py`)

Only `KG_FINANCIAL_DB` is required. Optional `KG_UNIVERSE_DB` (default
`/workspaces/thesis/data/universe.db`), `PRICING_BASE_URL` (default
`http://host.docker.internal:8000/pricing`).

## Files

### `pricing_client.py` — `PricingClient`, `Candle`, `DailyPrices`

- `GET /pricing/{ticker}?start_date=&end_date=` → plain object
  `{ticker, source, candles:[…], warning?}` (**not** a `{success,data}` envelope).
  The candle route is doubled (`/pricing/pricing/{t}`); `/universe` is single-prefixed.
- Unknown ticker / weekend-only range → **HTTP 200 with `candles: []` and a
  `warning`**, never 404. Callers check `DailyPrices.is_empty`.
- `normalize_ticker` / `daily_any_spelling`: `BRK.B` returns empty, `BRK-B` works.
- Retries 500/502/503/504 + transport errors (3×, backoff ≤ 8 s), 60 s timeout.
- `today_iso()` helper.

### universe source

`pipeline._load_members` reads `kg_schema.queries.members_asof` over
`universe.db` as of `--analysis-date`; `db.sync_universe` upserts those into
`assets` / `sectors`. The gateway `/universe` endpoint and the `parse_universe`
scraper are no longer used.

### `stats.py` — `summarize(candles) -> WindowStats`

Window-aggregate summary: first/last date & close, `period_return`,
`trading_days`, `daily_return_std` (sample stdev of daily **log** returns),
`annualized_volatility` (`std · √252`, `TRADING_DAYS_PER_YEAR = 252`), min/max
close, avg volume. `None` on an empty series; volatility `None` with < 2 returns.
`slice_year(candles, year)` filters by `"YYYY-"` prefix.

### `observations.py` — `build_observations(candles, *, engine_version) -> list[Observation]`

Per-`(asset, day)` analytics (roadmap `PriceObservation`). Pure functions over a
`Candle` list:

| Field | Definition | Warm-up |
|---|---|---|
| `log_return` | `ln(close_t / close_{t-1})` | first row `None` |
| `true_range` | `max(h−l, |h−pc|, |pc−l|)` (falls back to `h−l` with no prior close) | — |
| `atr_14` | Wilder: `(ATR_{t-1}·13 + TR_t)/14`, seeded with the mean of the first 14 TRs | `None` for the first 13 rows |
| `realized_vol_21d` / `_90d` | stdev of the trailing N log returns · √252 | `None` until index ≥ 21 / 90 |
| `max_drawdown_90d` | worst `close/peak − 1` over the trailing 90 closes (≤ 0) | `None` until index ≥ 89 |
| `momentum_21d/63d/252d` | `close_t / close_{t−lag} − 1` | `None` if `t < lag` |
| `dollar_volume` | `close · volume` (`None` if volume 0) | — |
| `sma_200` (v2) | mean of the last 200 closes | `None` until index ≥ 199 |
| `ret_5d` (v2) | `ln(close_t / close_{t−5})` | `None` until index ≥ 5 |
| `vol_5d` (v2) | sample sd (n − 1) of the last 5 daily log returns; **daily, not annualized** | `None` until index ≥ 5 |
| `mu_60d_base` / `vol_60d_base` (v2) | mean and sample sd (n − 1) of the **60 daily log returns that end 5 sessions before `t`** (returns `t−64 … t−5`); the recent 5 are excluded on purpose, so a shock does not dilute its own baseline; daily, not annualized | `None` until index ≥ 65 |

The five `(v2)` fields (`priceobs-v2`, `T-070`) feed `cycle`'s `BREAK_TREND_200`, `VOLATILITY_SHOCK` and
`CRASH_Z_SCORE`; every field is `None` until its window is full, and a window holding an undefined log
return (a non-positive close) leaves the field `None`. The closes are the gateway's **split-adjusted,
not dividend-adjusted** bars (checked on NVDA 2024-06-10, AVGO 2024-07-15 and WMT 2024-02-26: continuous
across the split). `ATR_PERIOD`, `VOL_SHORT`, `VOL_LONG`, `DRAWDOWN_WINDOW`, `SMA_LONG`, `SHOCK_WINDOW`,
`BASELINE_WINDOW` are module constants.

### `db.py`

`connect` / `ensure_schema` (`SCHEMA` then `kg_schema.ensure`). Writers:

| Function | Notes |
|---|---|
| `sync_universe(conn, members)` | upsert `assets` from `UniverseMember`s (COALESCE — never wipes an existing non-empty `company_name`/`cik`/`sector_id`); identity write path only, no `universe_membership` write |
| `completed_windows(conn)` | `(ticker, start, end, label)` resume set |
| `upsert_price_window(row)` | upsert on `(asset_id, start_date, end_date, label)`; sets `event_time = end_date` |
| `replace_daily_prices(conn, asset_id, candles)` | raw OHLCV; sets `event_time = date`, `ingested_at` |
| `upsert_price_observations(conn, asset_id, observations, *, engine_version, run_id)` | keyed `(asset_id, obs_date, engine_version)`; **rewritten only when a derived value differs** (T-131), a no-op on unchanged prices; `PRICE_OBSERVATION_ENGINE_VERSION = "priceobs-v2"` (`T-070`: adds `sma_200`, `ret_5d`, `vol_5d`, `mu_60d_base`, `vol_60d_base` -- definitions in `docs/kg_schema.md`; every `priceobs-v1` field is computed exactly as before; a consumer reads one version, so a database needs one `pricing_agent run --store-daily --observations` pass before `cycle`/`quant` can read `priceobs-v2`) |
| `load_daily_candles(conn, asset_id, *, end)` | the asset's full stored `price_daily` history up to `end` — what observations and the split-seam check read (T-131) |
| `recorded_split_values` / `has_pre_split_rows` | the asset's `corporate_action` SPLIT ratios, and whether one postdates a bar stored before it (T-131) |

### `pipeline.py`

`run(settings, params)` → per ticker: `daily_any_spelling` → `_store`. `_store`
writes the `full` window (+ per-year with `--by-year`), then `price_daily` with
`--store-daily`, then `price_observation` with `--observations`. Empty result →
skipped + logged (`stage='no_data'`); fetch exception → failed + logged
(`stage='fetch'`); neither stops the batch. A ticker is skipped only if all
expected window labels are present **and** neither `--store-daily` nor
`--observations` nor `--fresh` is set.

### `cli.py`

`run` (flags above) + `migrate`.

## Gotchas

- `--observations` roughly doubles the `--store-daily` row footprint; opt-in.
- Warm-up rows carry NULLs for the rolling fields — confined to early 2022 since
  the collector always fetches from `2022-01-01`.
- Finnhub free tier has no history, so `source` is almost always `yfinance`.
