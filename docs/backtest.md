# `backtest/`

Roadmap step 9 — **PENDING, not implemented.** The package exists only to reserve
the name and record the intended shape.

## Reserved design

- **No new production data.** Reads what the other steps wrote.
- **Inputs:** `portfolio_position` stint history + `price_observation`.
- **Outputs:** time-weighted returns vs SPX, turnover, drawdown, per-`score_type`
  return attribution.
- **Tables (sketch):** `backtest_run`, `backtest_return` (per-date NAV),
  `backtest_metric`.
- **Depends on** Phases 4 + 6 having produced `portfolio_position` history across
  many `cycle_date` values (run `cycle backfill` first).

Deferred to a future branch.
