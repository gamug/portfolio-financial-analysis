# `quant/`

Markowitz mean-variance / Modern Portfolio Theory optimizer. Its output is the
**base-case benchmark portfolio** the blended-score `cycle` book is evaluated
against, so it is built to be methodologically independent of that system and to
persist enough (return basis, risk model, frontier, weights-over-time, realized
forward performance) that "system vs. base case" is a pure SQL join.

`quant/` is a leaf package: it reads shared tables via plain SQL and imports only
`kg_schema`. Nothing in `cycle` / `pricing_agent` / `fundamental_agent` /
`entity_resolution` imports it, so its numeric dependencies (numpy, scipy, cvxpy,
clarabel — the first in the repo) stay off their import path. This is pinned by
`tests/test_quant_import_isolation.py`.

```bash
uv run python -m quant backfill-actions [--source derive|gateway] [--from 2022-01-01] [--to TODAY]
uv run python -m quant build-returns    [--from 2022-01-01] [--to TODAY]
uv run python -m quant build-risk-model --as-of 2026-08-27 [--lookback 756] [--min-history 504]
                                        [--cov ledoit_wolf_cc|ledoit_wolf_diag|sample] [--no-store-cov]
uv run python -m quant optimize --as-of 2026-08-27 [--objectives min_var,tangency,target_vol,frontier]
                                [--frontier-k 15] [--target-vol 0.15]
                                [--max-name-weight 0.05] [--max-sector-weight 0.30] [--turnover-cap F]
uv run python -m quant benchmark --from 2026-08-27 --to TODAY
uv run python -m quant evaluate  --from 2026-08-27 --to TODAY [--benchmark SP500_EW_INTERNAL]
```

`QuantSettings.load()` needs `KG_FINANCIAL_DB`; every knob is a CLI flag.

## The pipeline

`backfill-actions → build-returns → build-risk-model → optimize → (benchmark) → evaluate`

### `actions.py` — corporate actions

`price_daily.close` is split-adjusted but **not** dividend-adjusted, and the
pricing gateway serves only OHLCV. Two sources, both append-only under a distinct
`corporate_action.engine_version`:

- `--source gateway` probes `GET /pricing/{ticker}?actions=true` (and
  `/pricing/{ticker}/actions`); when the deployment serves actions this gives true
  ex-dates (`engine_version = corpact-v1`).
- `--source derive` (the self-contained default) takes the fiscal-year cash
  dividend per share from `financial_facts`
  (`us-gaap_CommonStockDividendsPerShareDeclared`, then `…CashPaid`) and spreads it
  over four synthetic quarterly ex-dates (`engine_version = corpact-v0-approx`).
  **Coarse**: wrong intra-year timing, no special dividends. A gateway extension or
  an accepted small direct vendor pull is the way to a precise daily series.

Splits are recorded for provenance only and never re-applied.

### `returns.py` — total-return daily series

`build_total_return_series` folds each day's cash dividend into the return —
`tr_log_return_t = ln((C_t + D_t) / C_{t-1})` — compounds a forward `tr_index`,
and carries a dividend-back-adjusted `adj_close`. Rows land in `quant_return_daily`
under `engine_version = qret-v1` (`INSERT OR IGNORE`; re-runs are a no-op). This is
a dedicated table, **not** `price_observation` rows under a new engine_version —
`v_price_observation` resolves the latest engine per (asset, day), so writing there
would silently move `cycle`'s technical/veto path onto quant's rows.

### `universe.py` — the score-independent gate

`liquidity_data_gate` keeps a name iff it (a) is an index member as of the date,
(b) has ≥ `min_history_days` return observations, (c) clears a median
dollar-volume floor, and (optionally) (d) is not under a T-1 HARD veto. It reads
**no** `score_snapshot` / `cycle_ranking` / blended score, so the benchmark stays
an independent control (pinned by `tests/test_quant_gate.py`: adding or removing
score rows does not change the gate output). Set `exclude_hard_vetoed=False` for a
fully self-contained gate.

### `panel.py` — the return matrix

`build_return_panel` pivots `quant_return_daily` into a dense `(T, N)` numpy
matrix on a common trading calendar, drops names with `< min_history_days`
observations in the window (`on_short_history="exclude"`, the default;
`"shrink_window"` trims dates instead), fills single-day holes with a flat `0.0`,
raises `PanelError` if any NaN survives, and hashes `(asset_ids, dates)` so a risk
model built from it is reproducible. No pandas.

### `risk.py` — covariance and expected returns

- **Covariance**: hand-rolled Ledoit-Wolf (2004) linear shrinkage toward a
  constant-correlation target (`ledoit_wolf_cc`, the default; `ledoit_wolf_diag`
  and `sample` also available). Always symmetrized and eigenvalue-floored
  (`nearest_psd`) before cvxpy sees it. No scikit-learn.
- **Expected returns**: `hist_mean` (raw), `james_stein` (shrink toward the grand
  mean — the default; raw-mean tangency is unstable), and `equilibrium`
  (reverse-optimized from cap weights). All three are computed and stored per
  model; `ret_estimator` picks which the optimizer uses.

Persisted as `quant_risk_model` metadata + `quant_expected_return` (μ per model)
+ `quant_covariance` (the annualized lower triangle, `N(N+1)/2` rows;
`--no-store-cov` skips it and keeps only the reproducible `panel_spec_json`).

### `optimize.py` / `objective.py` — the metric family

`OBJECTIVES` is a registry: a new metric is one builder function plus one dict
entry. v1 ships three mean-variance objectives plus the full frontier —
`--objectives` picks which run, `headline_objective` (default `min_var`) names the
primary comparator for reports.

| objective | formulation |
|---|---|
| `min_var` | `minimize wᵀΣw` — μ-free, the robust headline base case |
| `tangency` | y-space transform `minimize yᵀΣy s.t. (μ−rf)ᵀy = 1, y ≥ 0`, `w = y/Σy`; falls back to a frontier scan when there is no long-only tangency or a turnover cap is set |
| `target_vol` | SOCP `maximize μᵀw s.t. wᵀΣw ≤ target_vol²`; falls back to `min_var` (`status = vol_infeasible`) when the target is below the min-var vol. `target_vol` defaults to 1.25× the min-var vol when unset |
| `frontier` | k-point sweep from the min-var return to max feasible return; infeasible points are recorded, not raised |

Shared hard constraints: fully invested (`Σw = 1`), long only (`w ≥ 0`), per-name
box (`w ≤ max_name_weight`), per-GICS-sector caps (`Σ_{i∈s} wᵢ ≤ max_sector_weight`
— the same *intent* as `cycle.construction._cap_sectors` but a hard cvxpy
constraint, not water-fill redistribution), optional turnover cap. Solver:
Clarabel, falling back to OSQP then SCS.

### `persist.py` — the benchmark books

`run_optimize` loads the risk model for the as-of date (auto-building it when
absent), then writes one `quant_portfolio` row + a `quant_position` stint per
objective, plus a `quant_frontier_point` sweep when `frontier` is requested.
`quant_position` is keyed `(portfolio_id, asset_id, valid_from)` — many concurrent
books at one as-of, unlike `portfolio_position`'s `(asset_id, valid_from)`.

### `benchmark.py` / `evaluate.py` — forward comparison

`build_internal_benchmark` synthesizes `SP500_EW_INTERNAL`, an equal-weight,
daily-rebalanced index over the gated universe from the same total-return panel —
a self-consistent yardstick (same names, same return basis, no vendor
dependency). A real SPX / SPY_TR series loads into `benchmark_series` from a CSV
later.

`run_evaluate` freezes each persisted book's weights at its as-of date, walks
forward trading days, computes the weighted simple total return, compounds it,
subtracts the benchmark return, and writes `quant_benchmark_performance`. The live
`cycle` book (`portfolio_position`) is snapshotted into
`quant_portfolio(kind='live_book')` so `v_quant_vs_live` and
`v_quant_benchmark_performance` make the system-vs-base-case comparison a single
join. Run `optimize --as-of <cycle_date>` for each `cycle` SELECTION so the
benchmark and the live book share as-of dates.

## Tables and views

Additive `CREATE TABLE IF NOT EXISTS` in `kg_schema.ddl` — no migration, no
`schema_version` bump. `quant_run` is quant-private (owned by `quant/db.py`); the
rest carry read-contract views and live in `kg_schema` so `kg_schema.ensure`
creates them before it builds the views.

| table | contents | view |
|---|---|---|
| `corporate_action` | dividends / splits | `v_corporate_action` |
| `quant_return_daily` | total-return daily series | `v_quant_return_daily` |
| `risk_free_rate` | rf curve points | `v_risk_free_rate` |
| `benchmark_series` | benchmark index levels / returns | `v_benchmark_series` |
| `quant_run` | one row per CLI invocation (redacted `params_json`) | — |
| `quant_risk_model` | model metadata (estimators, shrinkage, panel spec, rf) | `v_quant_risk_model` |
| `quant_expected_return` | μ per (model, asset, mu_model) | — |
| `quant_covariance` | annualized Σ lower triangle | — |
| `quant_portfolio` | one optimized book per (as_of, kind) | `v_quant_portfolio` |
| `quant_position` | book weight stints | `v_quant_position` |
| `quant_frontier_point` | the frontier sweep per model | `v_quant_frontier_point` |
| `quant_benchmark_performance` | forward realized / cumulative / active return | `v_quant_benchmark_performance` |
| — | live book vs each optimized book, per name | `v_quant_vs_live` |

## Known gaps / caveats

- **Total-return quality** hinges on the dividend source. The `derive` fallback is
  FY-granular with synthetic ex-dates and no special dividends; a proper daily TR
  series needs the gateway to expose actions.
- **Survivorship bias.** `universe_membership` has only today's constituents and no
  closed stints, and `price_daily` holds no delisted names. Any pre-today
  evaluation of the Markowitz book over this universe is **biased upward**. This
  cannot be fixed from stored data. Mitigations: `quant_risk_model.panel_spec_json`
  freezes each run's exact universe + dates + sha256; from today forward, running a
  point-in-time `universe_membership` snapshot accrues honest stints; never delete
  `price_daily` rows for a name that later leaves the index.
- **No vendor risk-free / benchmark series** yet — a constant rf and an internal
  equal-weight benchmark are the v1 defaults; the tables + CSV loaders are in place.
- `price_daily.event_time` / `ingested_at` are NULL for every row — a
  `pricing_agent` cleanup, out of scope here. `quant` reads `price_daily.date` /
  `price_observation.obs_date`, which are never NULL.
- Re-introducing a `QUANTITATIVE` `score_type` is deliberately **not** done — the
  Markowitz output is a portfolio, not a per-asset score, and lives entirely in the
  `quant_*` tables. `score_snapshot` is untouched.
