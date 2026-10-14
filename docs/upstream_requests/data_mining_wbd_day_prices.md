# Request to `portfolio-data-mining`: daily prices for WBD and DAY over their S&P 500 membership

**From:** `portfolio-financial-analysis`, stream B (pricing), 2026-10-14. **Raised by:** T-153 part 2
(PR #161, finding C6). **Blocks:** this repo's `T-100` (the full-universe run on the historical universe).

## What is missing

The pricing gateway (`GET /pricing/pricing/{ticker}?start_date=&end_date=`) returns **no candles** for two
companies that were S&P 500 members inside the backtest window (2022-01-01 to 2026-10-05). It also returns
none under their earlier tickers.

| Company | CIK | Ticker over the membership | Membership in the index (`universe_history.db`) |
|---|---|---|---|
| Warner Bros. Discovery | 0001437107 | DISCA (Series A) to 2022-04-10, then WBD | WBD 2022-04-11 .. 2026-10-05 |
| Dayforce (Ceridian HCM before 2024-02-01) | 0001725057 | CDAY to 2024-01-31, then DAY | CDAY 2021-09-20 .. 2024-01-31, DAY 2024-02-01 .. 2026-02-08 |

Discovery's Series C line (DISCK, a member until 2022-04-10) is in the same situation.

## What was measured

Local gateway, `http://host.docker.internal:8000/pricing`, 2026-10-14, read-only:

```
WBD   2022-04-11..2026-10-05  candles=0  warning: Both Finnhub and yfinance failed to return data: yfinance returned no rows for this ticker/date range
WBD   2024-01-02..2024-01-31  candles=0  (same warning)
DAY   2024-02-01..2026-02-08  candles=0  (same warning)
DAY   2025-01-02..2025-01-31  candles=0  (same warning)
CDAY  2021-09-20..2024-01-31  candles=0  (same warning)
DISCA 2022-01-03..2022-04-08  candles=0  (same warning)
DISCK 2022-01-03..2022-04-08  candles=0  (same warning)
```

Controls from the same session: renamed companies that are still listed are served in full under their
current ticker, including the history from before the rename.

```
META  2022-06-01..2022-06-15  candles=11  (2022-06-01..2022-06-15; FB before 2022-06-09)
WTW   2022-01-03..2022-01-14  candles=10  (WLTW before 2022-01-10)
```

So the gap is not about renames. Both WBD and DAY have left the index (WBD on 2026-10-05, DAY on 2026-02-08),
and the gateway's sources (Finnhub's free tier, then yfinance) return nothing for either ticker.

## What we need

For each line, the **daily OHLCV series over its whole membership**, in the gateway's existing `/pricing`
response shape:

- **CIK 0001437107:** WBD from 2022-04-11 to 2026-10-05, and Discovery's DISCA and DISCK lines from
  2022-01-03 to 2022-04-08.
- **CIK 0001725057:** CDAY from 2022-01-03 to 2024-01-31, and DAY from 2024-02-01 to 2026-02-08.

Either way works for us:
- serve the series under the line's last ticker (WBD, DAY), as the gateway already does for META and WTW; or
- serve each ticker for its own dates.

This repo resolves both through its ticker history (`src/kg_schema/data/ticker_history.csv`).

If the series cannot be obtained, tell us. This repo then declares both companies unpriced in the replay,
as a recorded data gap.

## Why

`T-100` runs the whole pipeline over the **historical** S&P 500 universe, including the companies that
later left it (checklist PER-10 / PER-11 and L-11). It does this so the backtest has no survivorship bias.
Without prices, WBD (about 4.5 years in the index) and DAY (about 4.4 years, CDAY included) get no TECHNICAL
score, no price vetoes, no quant returns and no place in the benchmark book. The backtest would then leave
them out for lack of data, not on any signal, which is the bias PER-11 exists to prevent.
