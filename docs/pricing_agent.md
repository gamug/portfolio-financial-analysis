# `pricing_agent/`

Standalone S&P 500 daily-pricing collector. **Zero imports to/from
`fundamental_agent`.** One daily-candles request per ticker over the whole range,
then a `price_window` summary (+ optional per-year windows, raw OHLCV, and derived
per-day `price_observation` analytics).

```bash
uv run python -m pricing_agent run [--tickers AAPL,NVDA] [--start 2022-01-01] \
    [--by-year] [--store-daily] [--observations] [--fresh] [--refresh-universe]
uv run python -m pricing_agent migrate
```

## Configuration (`config.py`)

Only `KG_FINANTIAL_DB` is required. Optional `PRICING_BASE_URL` (default
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

### `universe.py`

`parse_universe(rows)` renames the gateway `/universe` Wikipedia-shaped columns
(`Symbol`, `Security`, `GICS Sector`, `GICS Sub-Industry`, `CIK`) into `Company`
models; CIK zero-padded to 10.

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

`ATR_PERIOD`, `VOL_SHORT`, `VOL_LONG`, `DRAWDOWN_WINDOW` are module constants.

### `db.py`

`connect` / `ensure_schema` (`SCHEMA` then `kg_schema.ensure`). Writers:

| Function | Notes |
|---|---|
| `sync_universe(conn, companies, *, as_of=None)` | upsert `assets` (COALESCE — never wipes an existing non-empty `company_name`/`cik`/`sector_id`), then `universe_membership.reconcile` (`source='pricing-gateway'`) |
| `completed_windows(conn)` | `(ticker, start, end, label)` resume set |
| `upsert_price_window(row)` | upsert on `(asset_id, start_date, end_date, label)`; sets `event_time = end_date` |
| `replace_daily_prices(conn, asset_id, candles)` | raw OHLCV; sets `event_time = date`, `ingested_at` |
| `upsert_price_observations(conn, asset_id, observations, *, engine_version, run_id)` | **immutable** per `(asset_id, obs_date, engine_version)`; `PRICE_OBSERVATION_ENGINE_VERSION = "priceobs-v1"` |

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
