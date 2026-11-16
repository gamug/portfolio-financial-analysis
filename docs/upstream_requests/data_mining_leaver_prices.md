# Request to `portfolio-data-mining`: daily prices for the S&P 500 leavers the gateway does not serve

**From:** `portfolio-financial-analysis`, stream B, 2026-10-14. **Raised by:** T-153 parts 2 and 3 (PR #161's
finding C6, widened by the measurement below). **Replaces** `data_mining_wbd_day_prices.md`, which was never
sent; WBD and DAY are in this request. **Sent with** `data_mining_leaver_gics.md`. **Blocks:** this repo's
`T-100` (the full-universe run on the historical universe). It does not block the final pilot (`T-143`).

## What is missing

The pricing gateway (`GET /pricing/pricing/{ticker}?start_date=&end_date=`) returns **no candles at all** for
**37 of the 90 membership stints** of the 86 companies that left the S&P 500 inside the backtest window
(a stint that ended on or after 2022-01-01, per `universe_history.db`). Those 37 stints make 35 price
lines of 34 companies. The other 53 stints are priced on every trading day of their membership.

The 37 look like companies whose ticker no longer trades (acquired, merged or failed). The gateway's sources
(Finnhub's free tier, then yfinance) keep no history for a delisted ticker. Two companies that were renamed
**after** they left the index (Gap: GPS, now GAP; Fortune Brands: FBHS, now FBIN) are served in full under the
new ticker. This repo maps those two itself (`src/kg_schema/data/ticker_history.csv`), so they are **not** in
this request.

## What was measured

Local gateway, `http://host.docker.internal:8000/pricing`, 2026-10-14, read-only. For each stint, one request from
`max(valid_from, 2022-01-03)` to `valid_to`, under the stint's own symbol (GPS and FBHS under GAP and FBIN). The
trading calendar is the dates of AAPL's candles from the same gateway: 1,200 days, 2022-01-03 .. 2026-10-14.

```
stints: 90    priced on every membership day: 53    partly priced: 0    no candles: 37
warning on every empty one: Both Finnhub and yfinance failed to return data: yfinance returned no rows
GPS  under GAP:  22 of 22 days (2022-01-03 .. 2022-02-02)
FBHS under FBIN: 242 of 242 days (2022-01-03 .. 2022-12-18)
```

`universe_history.db` sha1 `fddbdccb71ab4243a0fb76b3027774474d352b99`.

## What we need

For each line in the table, the **daily OHLCV series from 2019-01-02 to the line's exit date** (the "Series"
column), in the gateway's existing `/pricing` response shape, served **under the line's last ticker in the
index**, the ticker in the "Ask for" column. This repo keeps a company as one asset per CIK line and asks the gateway for that line's last ticker
only (`src/kg_schema/data/ticker_history.csv`; `pricing_agent` fetches by the line's canonical ticker), the way
the gateway already serves META's history from before the FB rename. A series served under any other ticker
would not be read.

**Why the series starts in 2019.** Every line is in the index on the window's first day, 2022-01-03, and the scores
read history from before the day they are computed. The longest such read is `quant`'s estimation window,
`lookback_days = 756` trading days (`src/quant/config.py:77`), longer than the 200-day moving average and the
252-day momentum. On the gateway's own calendar (AAPL's candles), the 756th trading day before 2022-01-03 is
2019-01-03, so we ask from the first trading day of 2019. Nothing after the exit date is needed. If a stock
listed later than 2019-01-02, start at its first trading day.

1. **Prices as traded, through the last trading day.** Give the prices the stock actually traded at, down to its
   last trading day in the range, including a final day at the deal price, a halt or a crash. That final stretch
   is the reason for this request. Adjust for splits the same way the gateway's other series are adjusted, and
   say how.
2. **The same security.** Each series must be the security of the CIK shown, under that ticker. Where the ticker
   has since been reused by another company, the series must still be the old company's.
3. **Discovery's two classes are two separate lines.** Series A (DISCA) continues as WBD: the S&P DJI notice of
   2022-04-07 and the company's own FAQ of 2022-04-10 put every class into one WBD line, and the FAQ names that
   line Series A. So the WBD series must be Series A's prices before 2022-04-11, never Series C's. Series C
   (DISCK) is asked for separately and ends 2022-04-10.
4. **What could not be obtained.** List any line or date range you cannot serve, with the reason. Do not fill a
   gap from another company's series.

Each line counts on its own, so a partial delivery helps too.

35 lines (20,714 trading days inside the index, 2022-01-03 onward), by exit date:

| Ask for | CIK | Company | Series | Covers |
|---|---|---|---|---|
| `/pricing/XLNX` | 0000743988 | Xilinx | 2019-01-02 .. 2022-02-14 |  |
| `/pricing/INFO` | 0001598014 | IHS Markit | 2019-01-02 .. 2022-03-01 |  |
| `/pricing/PBCT` | 0001378946 | People's United Financial | 2019-01-02 .. 2022-04-03 |  |
| `/pricing/DISCK` | 0001437107 | Discovery, Inc. | 2019-01-02 .. 2022-04-10 | Series C, its own line |
| `/pricing/CERN` | 0000804753 | Cerner | 2019-01-02 .. 2022-06-07 |  |
| `/pricing/CTXS` | 0000877890 | Citrix Systems | 2019-01-02 .. 2022-10-02 |  |
| `/pricing/DRE` | 0000783280 | Duke Realty | 2019-01-02 .. 2022-10-02 |  |
| `/pricing/NLSN` | 0001492633 | Nielsen Holdings | 2019-01-02 .. 2022-10-11 |  |
| `/pricing/TWTR` | 0001418091 | Twitter | 2019-01-02 .. 2022-10-31 |  |
| `/pricing/ABMD` | 0000815094 | Abiomed | 2019-01-02 .. 2022-12-21 |  |
| `/pricing/SBNY` | 0001288784 | Signature Bank | 2019-01-02 .. 2023-03-14 |  |
| `/pricing/SIVB` | 0000719739 | SVB Financial Group | 2019-01-02 .. 2023-03-14 |  |
| `/pricing/FRC` | 0001132979 | First Republic Bank | 2019-01-02 .. 2023-05-03 |  |
| `/pricing/DISH` | 0001001082 | Dish Network | 2019-01-02 .. 2023-06-19 |  |
| `/pricing/ATVI` | 0000718877 | Activision Blizzard | 2019-01-02 .. 2023-10-17 |  |
| `/pricing/SEE` | 0001012100 | Sealed Air | 2019-01-02 .. 2023-12-17 |  |
| `/pricing/PXD` | 0001038357 | Pioneer Natural Resources | 2019-01-02 .. 2024-05-07 |  |
| `/pricing/CMA` | 0000028412 | Comerica | 2019-01-02 .. 2024-06-23 |  |
| `/pricing/WRK` | 0001732845 | WestRock | 2019-01-02 .. 2024-07-07 |  |
| `/pricing/MRO` | 0000101778 | Marathon Oil | 2019-01-02 .. 2024-11-25 |  |
| `/pricing/CTLT` | 0001596783 | Catalent | 2019-01-02 .. 2024-12-22 |  |
| `/pricing/QRVO` | 0001604778 | Qorvo | 2019-01-02 .. 2024-12-22 |  |
| `/pricing/DFS` | 0001393612 | Discover Financial | 2019-01-02 .. 2025-05-18 |  |
| `/pricing/JNPR` | 0001043604 | Juniper Networks | 2019-01-02 .. 2025-07-08 |  |
| `/pricing/ANSS` | 0001013462 | Ansys | 2019-01-02 .. 2025-07-17 |  |
| `/pricing/HES` | 0000004447 | Hess Corporation | 2019-01-02 .. 2025-07-22 |  |
| `/pricing/WBA` | 0001618921 | Walgreens Boots Alliance | 2019-01-02 .. 2025-08-27 |  |
| `/pricing/IPG` | 0000051644 | Interpublic Group | 2019-01-02 .. 2025-11-27 |  |
| `/pricing/K` | 0000055067 | Kellanova | 2019-01-02 .. 2025-12-10 |  |
| `/pricing/DAY` | 0001725057 | Dayforce | 2019-01-02 .. 2026-02-08 | CDAY to 2024-01-31, then DAY |
| `/pricing/HOLX` | 0000859737 | Hologic | 2019-01-02 .. 2026-04-08 |  |
| `/pricing/CTRA` | 0000858470 | Coterra Energy | 2019-01-02 .. 2026-05-06 |  |
| `/pricing/EA` | 0000712515 | Electronic Arts | 2019-01-02 .. 2026-08-04 |  |
| `/pricing/AVB` | 0000915912 | AvalonBay Communities | 2019-01-02 .. 2026-08-17 |  |
| `/pricing/WBD` | 0001437107 | Warner Bros. Discovery | 2019-01-02 .. 2026-10-05 | DISCA to 2022-04-10, then WBD |

## If they cannot be obtained

This repo then declares the missing lines under its survivorship rule (checklist PER-11), with the index weight
the unpriced names would have had and the direction of the bias. The missing set mixes acquisition targets, whose
price often rose before exit, with failures (SIVB, SBNY, FRC), whose price crashed. Leaving either kind out biases
the backtest, in opposite directions.

## Why

`T-100` runs the whole pipeline over the **historical** S&P 500 universe, including the companies that later left
it (checklist PER-10 / PER-11 and L-11), so the backtest has no survivorship bias. An unpriced member gets no
TECHNICAL score, no price vetoes, no quant return and no place in the benchmark book. It would drop out for lack of
data, not on any signal, which is exactly the bias PER-11 exists to prevent. The 37 stints are not a random sample
of leavers: they are the ones that stopped trading, and their final months carry the takeover premiums and the
bank failures of 2023.
