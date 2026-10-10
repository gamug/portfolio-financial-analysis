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
uv run python -m quant backfill-actions [--from 2022-01-01] [--analysis-date TODAY]
uv run python -m quant build-returns    [--from 2022-01-01] [--analysis-date TODAY]
uv run python -m quant build-risk-model --analysis-date 2026-08-27 [--lookback 756] [--min-history 504]
                                        [--cov ledoit_wolf_cc|ledoit_wolf_diag|sample] [--no-store-cov]
                                        [--top-n 30] [--max-name-weight F] [--max-sector-weight 0.30]  # the caps follow N (T-137)
                                        [--allow-stale-prices] [--allow-dirty]  # T-110 / T-114 guard overrides
uv run python -m quant optimize --analysis-date 2026-08-27
                                [--objectives min_var,risk_parity,tangency,target_vol,frontier]
                                [--mu equilibrium|james_stein|hist_mean|carhart] [--frontier-k 15] [--target-vol 0.15]
                                [--top-n 30] [--max-name-weight F] [--max-sector-weight 0.30] [--turnover-cap F]
                                [--allow-stale-prices]
uv run python -m quant benchmark --from 2026-06-30 --analysis-date TODAY     # --from required: the panel is gated as of it
uv run python -m quant load-benchmark --csv spy_tr.csv --benchmark SPY_TR      # columns: date,total_return_level
uv run python -m quant evaluate  [--from 2026-06-01] --analysis-date TODAY [--benchmark SP500_EW_INTERNAL|SPY_TR]
```

`evaluate`'s `--from` defaults to the earliest persisted `quant_portfolio.as_of`
when omitted (Q2, docs/model_fixes.md) — deliberately **not** the same date
used for `optimize --analysis-date`/`build-risk-model --analysis-date` above:
that date is, by construction, the *most recent* date with any price data at
all, so evaluating from it leaves no forward window to realize a return
over. Pass `--from` explicitly only to narrow the evaluated range to books
optimized on or after that date.

Every subcommand takes `--analysis-date YYYY-MM-DD` (default: today). For
`build-risk-model` / `optimize` it is the as-of date (`--as-of` is kept as an
alias; disagreeing values error). For the `--from`/`--to` subcommands it is the
range upper bound — `--to` is clamped to it and `quant_run.as_of` is stamped with it.
Every `quant_run` records `code_version`.

`build-risk-model`/`optimize` refuse an as-of past `price_daily`'s last stored date (the
price spine) — proceeding would silently build a model that claims to be "as of" a date the
price data doesn't actually reach yet (T-110) — unless `--allow-stale-prices`, which records
why on the run (`quant_run.params_json`, and a CLI `WARNING`). `optimize` checks its own
`as_of` against the spine unconditionally — whether it reuses an already-stored risk model
or has to build one — since a stored model may itself have been built past the spine under
`--allow-stale-prices`, and a later `optimize` at that same stale date must still be recorded
as such, not silently waved through by the reuse lookup (PR #87 review finding).

Every subcommand also refuses to write its `quant_run` row at all when its own
`code_version()` is dirty (uncommitted changes) — its results would come from code `HEAD`
alone can't reproduce (T-114) — unless `--allow-dirty`, which records why on the run and a
CLI `WARNING`, the same convention as `--allow-stale-prices`.

`QuantSettings.load()` needs `KG_FINANCIAL_DB`; `KG_UNIVERSE_DB` is optional (the
point-in-time universe reads — `load_universe_asset_ids` / `load_assets` /
`liquidity_data_gate` — hit `universe.db`, no longer `financial.db`
`universe_membership`, and now fail loudly on an empty as-of universe). Every other
knob is a CLI flag.

## The pipeline

`backfill-actions → build-returns → build-risk-model → optimize → (benchmark) → evaluate`

### `actions.py` — corporate actions

`price_daily.close` is split-adjusted but **not** dividend-adjusted. Dividends and
splits come from **one source only: the pricing gateway** (T-085) —
`portfolio-data-mining`'s yfinance-backed
`GET /pricing/{ticker}/actions?start_date=&end_date=`, rows landing in
`corporate_action` under `engine_version = corpact-v1`. Acquiring data is that
repo's job, so `quant` consumes it and mines nothing itself: there is no `--source`
flag and no fallback that re-derives dividends from `financial_facts` (the
`corpact-v0-approx` / `corpact-v1-derived` engines were removed; rows they wrote
before T-085 stay in the append-only table as history and `load_actions` never reads
them). `quant.db.load_actions` uses the newest gateway engine per asset
(`corpact-v2` > `corpact-v1`).

A gateway that cannot serve is therefore a **failure**, not a degraded run:

- **the run fails** — `GatewayUnavailable`, `quant_run.status = 'failed'`, CLI exit 1 —
  when the probe of the first asset fails (route missing, gateway down, or yfinance
  failing upstream), or when the circuit breaker opens after
  `gateway_max_consecutive_failures` (default 3) consecutive gateway *errors*. Each of
  those has already paid `max_retries` × the timeout, so continuing would spend hours
  writing nothing. Rows already written are kept (`INSERT OR IGNORE`), so a re-run
  after the gateway is back is cheap.
- **an asset the gateway cannot serve gets no rows** and is listed in the report — an
  `ActionsUnavailable` (upstream answers `200` + empty lists + a non-null `warning`
  when yfinance failed; a genuine "no dividends in range" arrives with
  `warning: null` and is recorded as such), an unsupported route, or one isolated
  error. The run still completes, and the CLI exits **1** because the dividend series
  would be incomplete. Only gateway *errors* trip the breaker; a per-ticker warning
  means the gateway answered, so it resets the streak.

Share classes are requested in yfinance's spelling (`BF.B` → `BF-B`): the route only
upper-cases what it is given, and yfinance answers an unknown symbol with a clean empty
result — the one silent failure this consumer cannot see. Point `PRICING_BASE_URL` at
the **deployed** gateway (its `/pricing` mount); live verification, `T-052`, passed 2026-09-21.
`quant_run.params_json` records `assets_seen` / `assets_fetched` / `assets_errored` and
the first error messages.

**`build-returns` refuses to run without a clean gateway backfill behind it** (T-086).
`quant_return_daily` is `INSERT OR IGNORE` per `(asset, day, engine_version)`, so a series
built while dividends are missing is price-only *and* locks in under that version. It
proceeds only if a **completed** `backfill-actions` run whose window covers the build
window fetched **every** asset (`assets_errored == 0`, both recorded in that run's
`params_json`) and `corporate_action` holds gateway rows. Any such run will do — a later
run that failed does not undo the rows an earlier clean one wrote. Otherwise it exits 1
naming the reason (no run, a failed run, errored assets, a window that is not covered, a
run recorded before T-085, or no gateway rows) and writes nothing; a refusal is not
recorded as a run. `--allow-no-dividends` builds a knowingly price-only series anyway: it
is loud on stderr and recorded in the `build-returns` run's `params_json`
(`allow_no_dividends`, `dividends_guard_bypassed`), and the rows still lock in under
`return_engine_version`.

Splits are recorded for provenance only and never re-applied.

### `returns.py` — total-return daily series

`build_total_return_series` folds each day's cash dividend into the return —
`tr_log_return_t = ln((C_t + D_t) / C_{t-1})` — compounds a forward `tr_index`,
and carries a dividend-back-adjusted `adj_close`. Rows land in `quant_return_daily`
under `engine_version = qret-v2` (`INSERT OR IGNORE`; re-runs are a no-op *for
that version* — bump `QuantSettings.return_engine_version` whenever an upstream
input the return series depends on changes, e.g. `qret-v1` → `qret-v2` when the
Q3 dividend fix landed, or the corrected values never make it past the
`INSERT OR IGNORE`; see `actions.py` above for the sequencing this implies). This is a dedicated table, **not** `price_observation` rows
under a new engine_version — `v_price_observation` resolves the latest engine
per (asset, day), so writing there would silently move `cycle`'s
technical/veto path onto quant's rows.

### `universe.py` — the score-independent gate

`liquidity_data_gate` keeps a name iff it (a) is an index member as of the date,
(b) has ≥ `min_history_days` return observations, (c) clears a median
dollar-volume floor (both read at the **pinned** `return_engine_version` /
`observation_engine_version`, never "latest" — T-131; `priceobs-v2` since T-070, the same series with five more columns — a database needs one `pricing_agent run --store-daily --observations` pass before the gate can read it), and (optionally) (d) is not under a T-1 HARD veto —
`kg_schema.queries.hard_vetoed_as_of`, the same point-in-time stint predicate
`cycle` reads (T-125; `quant.db.hard_vetoed_as_of` re-exports it, no longer its
own copy). It reads **no** `score_snapshot` / `cycle_ranking` / blended score, so the gate stays an
independent control (pinned by `tests/test_quant_gate.py`: adding or removing
score rows does not change the gate output). `settings_gate` applies it with
*settings*' own `exclude_hard_vetoed` (default `True`) for a book; `benchmark_gate`
(T-108) forces `exclude_hard_vetoed=False` for the benchmark's panel, since a
book's own veto exclusion must never also shrink the yardstick it's graded against.

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
- **Expected returns**: `equilibrium` (reverse-optimized from cap weights,
  `Π = δ·Σ·w_market` — **the default**), `james_stein` (shrink the sample mean
  toward the grand mean), `hist_mean` (raw), and `carhart` (T-077: the four-factor
  estimator below, `factors.py`). All are computed and stored per
  model (`carhart` only when the factor data covers the as-of); `ret_estimator` / `optimize --mu` picks which the return-aware objectives
  and the reported expected-return/Sharpe use. `equilibrium` is the default because
  it carries real cross-sectional dispersion with near-zero estimation noise;
  `james_stein` over ~5 y of daily data shrinks μ almost flat, which collapses the
  frontier onto `min_var` (see the note below).

### `factors.py` — the Carhart four-factor estimator (T-077)

`carhart` is `μ_i = rf + Σ_k β^shrunk_i,k · λ̄_k` over `{MKT, SMB, HML, MOM}`. It reads **no** market cap and no
score, so it is available wherever the panel and the factor files overlap, and it is a second, independent
source of cross-sectional dispersion beside `equilibrium`. `equilibrium` stays the default.

**The factor data** is vendored, version-pinned and read offline. `src/quant/data/` holds Kenneth R. French's daily
"Fama/French 3 Factors" (Mkt-RF, SMB, HML, RF) and daily "Momentum Factor" (Mom) files **byte-for-byte** (a
`.gitattributes` `-text` rule keeps git from touching the line endings, so the hash below is the published file's),
beside `manifest.json`: the library URL, each file's URL, the download date (2026-10-10), the library's own version
statement (*"This file was created by using the 202608 CRSP database"*), the SHA-256 of each file and of the zip it came
in, and each file's first and last date (1926-07-01 / 1926-11-03 to 2026-08-31). `load_factors(as_of)`:

- checks every file's SHA-256 against the manifest and **refuses on a mismatch** (`FactorIntegrityError`, which fails
  `build-risk-model` outright — it is not downgraded to "carhart unavailable": data that is not the pinned data is not
  estimated from);
- converts the files' **percent** to decimals and drops a row carrying French's missing-data sentinel (none today);
- is **point-in-time**: the files are ascending and parsing stops at the first row dated after the as-of, so no later
  row is read into the result; the two files are joined on date.

**The regression.** For each panel asset, over the panel's last 756 trading days (the existing lookback): the panel holds
total-return **log** returns (`tr_log_return`), converted with `expm1` to simple returns because the factors are simple
returns; the excess return is `r_i,t − RF_t` with French's own daily RF (the series `Mkt-RF` is measured against); OLS of
the excess return on `[1, Mkt-RF, SMB, HML, Mom]` over the dates where both exist. Each beta's sampling variance is
`se²_i,k = s²_e,i · [(X'X)⁻¹]_kk`. The intercept `a_i` is estimated and **dropped**: no alpha enters `μ`.

**Minimum observations** are 504 (two thirds of the window, `carhart_min_obs`). An asset below it takes the prior mean beta
(shrinkage weight 0) and is flagged in the risk model's manifest. The panel is dense (`build_return_panel` keeps a name
only with ≥ 98% coverage and zero-fills the few holes), so on a production-shaped panel every asset has the same count
and the flag is a guard, not an event: what bites is the **overlap**. A panel whose last dates run past the factor file's
last date is regressed on the overlap only and the gap is recorded (`panel_gap_after_factor_end`); an overlap under 504
dates leaves `carhart` unbuilt — the other three estimators are built, the reason is stored in the model's manifest
(`carhart.unavailable`), and `optimize --mu carhart` refuses with that reason.

**Vasicek (1973) shrinkage**, per factor `k` separately (a declared simplification of the multivariate prior):

```
β̄_k  = the equal-weighted cross-sectional mean of β̂_i,k over the panel       (the prior mean)
σ²_k  = the cross-sectional variance of β̂_i,k  (ddof = 1)                        (the prior variance)
w_i,k = σ²_k / (σ²_k + se²_i,k)          β^shrunk_i,k = w_i,k · β̂_i,k + (1 − w_i,k) · β̄_k
```

This is `PLAN.md`'s `w = 1 − Var(β̂_i) / (Var(β̂_i) + Var(β̄))` written out: with `Var(β̂_i) = se²_i` the sampling variance of
the asset's own estimate and `Var(β̄) = σ²_k` the dispersion of the prior, `1 − se²/(se² + σ²) = σ²/(σ² + se²)`. A noisy
beta (large `se²`) is pulled to the prior mean; a precise one is left alone.

**The premia** `λ̄_k` are the arithmetic mean of factor `k`'s daily returns from **1963-07-01** (the Fama–French / Carhart
sample start) to the as-of, times `periods_per_year` (252). With `rf` the same annualized as-of risk-free rate the other
estimators use (T-109's total-return convention), `μ_i = rf + Σ_k β^shrunk_i,k λ̄_k`. On the 202608 vintage, from 1963:
Mkt-RF 7.33%, SMB 1.11%, HML 3.71%, Mom 7.15% a year.

**Stored.** `μ` lands in `quant_expected_return` under `mu_model = 'carhart'`; the model is `rm-v2` (bumped from `rm-v1`
because the stored rows change; an `rm-v1` model holds no `carhart` rows and `optimize --mu carhart` refuses it with a
message saying so). `quant_risk_model.manifest_json` gains a `carhart` object: the factor library version, each file's
SHA-256, the files' last date, the regression's first/last date and count, the panel's dates past the factors, `λ̄` per
factor, `β̄_k` and `σ²_k`, the distribution of `w_i,k` (min, median, max) per factor, and the flagged asset ids — or
`{"unavailable": reason}`. The same object goes on the `build-risk-model` run's `params_json`. **No schema change, no new
table.**

**Declared choices and limitations.**

- *Factor vintage.* The library is rebuilt every year (CRSP restates, the breakpoints and the one-month bill series are
  revised), so a vintage downloaded later can differ slightly from what was public on a past as-of. The model records the
  vintage it used (202608); the point-in-time cut stops lookahead in *dates*, not in the data's revisions. **The factor
  files' version is not part of the risk model's key** (`model_version` = `rm-v2` + the input-manifest tag), so replacing the
  vendored files and rebuilding an as-of would overwrite carhart μ under the same `model_version`: **a new factor vintage requires
  a new `risk_model_version`** (`rm-v3`, …). `tests/test_quant_factors.py` pins `rm-v2` to library version `202608 CRSP`, so
  updating the files without bumping the version fails CI.
- *Per-factor shrinkage* ignores the covariance between the four betas (a multivariate prior would shrink them jointly); a
  declared simplification.
- *Equal-weighted prior*: the prior mean is the plain cross-sectional mean over the panel, not a cap-weighted one, so it is
  closer to the typical stock than to the index.
- *The prior variance is the raw dispersion of the estimates*, which already contains the sampling noise `mean(se²)`; a
  Vasicek prior would subtract it. So `w` errs low (a little more shrinkage than the textbook prior would give).
- *Alpha dropped*: `μ` carries factor premia only.
- *The premia are a 60-year average.* Their standard error is large (a factor's daily mean over ~15,000 days still has a
  standard error of several percent a year), and the 1926 start or the last 756 days give materially different `λ̄` (see the
  T-077 entry in `model_fixes.md`); `carhart` ranks assets by factor exposure far better than it prices them.
- *Short sample.* Whatever a comparison over the ~20 months the database can evaluate shows is descriptive: no significance
  claim is made (T-078 was deprecated for this reason).

**Market caps (`T-132`).** One company counts once: panel assets sharing a CIK (GOOG/GOOGL, FOX/FOXA, NWS/NWSA) split the company cap equally in `w_market`. `w_market` is built from `kg_schema.market_cap.market_caps_as_of` at
the as-of date: the latest cover-page share count usable that day × the close on or before it,
refusing a count older than `--market-cap-max-share-age-days` (200) or a close older than 10
days. A panel asset with no cap makes `build-risk-model` (and `optimize`, which builds one)
**refuse** with `MissingMarketCaps`, naming the asset ids and reasons — a missing cap is no
longer silently weight 0, and an all-missing panel no longer falls back to equal weights.
`--allow-missing-caps` builds anyway, those assets at weight 0. Either way the run records
`quant_run.params_json["market_caps"]` (`n_with_cap`, `missing{asset_id: reason}`,
`share_age_days_median/max`, `n_multi_class`, `dual_listed{cik: [asset_ids]}`) and the same object lands in the risk model's
`params_json`; the CLI prints it.

Persisted as `quant_risk_model` metadata + `quant_expected_return` (μ per model)
+ `quant_covariance` (the annualized lower triangle, `N(N+1)/2` rows;
`--no-store-cov` skips it and keeps only the reproducible `panel_spec_json`).

### `optimize.py` / `objective.py` — the metric family

`OBJECTIVES` is a registry: a new metric is one builder function plus one dict
entry. v1 ships four objectives plus the full frontier — `--objectives` picks
which run, `headline_objective` (default `min_var`) names the primary comparator
for reports.

| objective | formulation |
|---|---|
| `min_var` | `minimize wᵀΣw` — μ-free, the robust headline base case |
| `risk_parity` | equal risk contribution (Spinu 2013: `minimize 0.5 wᵀΣw − Σ log wᵢ`, then normalize); μ-free, more diversified than `min_var`. The caps rarely bind on a ~500-name book; when the ERC book breaches either (a small panel, where the name cap is `1.5/n_held`) it is projected onto both caps (`quant.optimize._project_onto_caps`, T-137 — before, a water-fill handled the name cap alone and the sector cap not at all). |
| `tangency` | y-space transform `minimize yᵀΣy s.t. (μ−rf)ᵀy = 1, y ≥ 0`, `w = y/Σy`; falls back to a frontier scan when there is no long-only tangency or a turnover cap is set |
| `target_vol` | SOCP `maximize μᵀw s.t. wᵀΣw ≤ target_vol²`; falls back to `min_var` (`status = vol_infeasible`) when the target is below the min-var vol. `target_vol` defaults to 1.25× the min-var vol when unset |
| `frontier` | k-point sweep from the min-var return to the max feasible return; if μ has no cross-sectional signal the frontier collapses and one point — the min-var portfolio, `status = "degenerate"` — represents it, not k copies mislabelled `optimal` as if a real sweep had run (T-112) |

**Why the frontier can collapse.** `μ` estimated from ~5 y of daily returns has a
standard error (~`σ/√T` ≈ 11 pp/name) far larger than the true spread in expected
returns, so `james_stein` shrinks it nearly flat. A near-constant `μ` makes every
portfolio's expected return ≈ the same, so `tangency` and the frontier both
reduce to the global minimum-variance portfolio — `efficient_frontier` detects
this (no feasible return range above `r_min`) and returns that one point, marked
`degenerate`, rather than a `k`-point sweep that never actually happened (T-112).
This is the estimator refusing to bet on noise, not a bug — and it is why
`min_var` (which never touches `μ`) is the headline. The `equilibrium` default
sidesteps it: equilibrium `μ` has real dispersion by construction, so the
frontier fans out and `tangency` becomes a capped cap-weight tilt. `risk_parity`
and `min_var` are unaffected either way.

Shared hard constraints: fully invested (`Σw = 1`), long only (`w ≥ 0`), per-name
box (`w ≤` the effective name cap), per-GICS-sector caps (`Σ_{i∈s} wᵢ ≤` the effective
sector cap — the same *values* as the thesis book's (`cycle.construction.build_book`, T-137),
but hard cvxpy constraints rather than an exact projection of a target), optional turnover cap
(next section).
Solver: Clarabel, falling back to OSQP then SCS. **Every** objective —
`min_var`, `risk_parity`, `tangency`, `target_vol` and the `frontier` — runs under the effective caps.

### `turnover.py` — the turnover cap and the chain (T-077)

`optimize --turnover-cap C` (`C` in (0, 2]; **off by default, so today's books are unchanged**) bounds the book's trade
against the previous one: `Σ|w − w_prev| ≤ C`. Until T-077 the cap was inert — `Constraints.w_prev` existed and nothing
populated it.

- **The chain.** `w_prev` is the previous book of the same *chain*: the same objective (`kind`), `frontier_k`,
  expected-return estimator, optimizer engine version, input manifest and turnover cap, with the newest `as_of` strictly
  before the book's. The chain is carried by `quant_portfolio.engine_version`: the default configuration (`equilibrium`, no
  cap) keeps its key (`opt-v2+<tag>`); a variant puts its marks **before the first `+`**: `opt-v2.mu-<estimator>.to-<cap>+<tag>`
  (`.mu-…` for a non-default estimator, `.to-…` for a cap). `v_quant_portfolio.is_current` reads `opt-v<N>` up to the first `+`
  and requires `N` all digits, so a variant book is **never current**, whatever the compute order. So a
  `carhart` book and an `equilibrium` book at one as-of no longer overwrite each other (`--mu james_stein` and
  `--mu hist_mean` books, which used to overwrite the default book, get their own key too), and the chain is a
  *configuration over time*: change the estimator, the cap or the inputs and a new chain starts.
- **No previous book, no constraint.** The first book of a chain is unconstrained and the book says why
  (`params_json["turnover"]["reason_not_applied"]`).
- **A name that left the panel still counts.** The previous book's weight in a name outside today's panel is sold in full
  and adds to the turnover as a constant: `norm1(w − w_prev over the panel) + Σ w_prev(outside) ≤ C`.
- **Relaxation.** If no book satisfying the name and sector caps trades that little, the cap is relaxed to the smallest
  feasible value (an LP, `optimize.min_turnover`, plus 1e-6) and the relaxation is recorded beside the cap relaxations
  (`relaxations`: `cap = "turnover"`, `requested`, `effective`, `reason`).
- **Recorded.** `params_json` carries `ret_estimator` and `turnover`: `cap_requested`, `cap_effective`, `applied`,
  `reason_not_applied`, `previous_portfolio_id`, `previous_as_of`, `outside_panel_weight` and `realized`; `quant_portfolio.turnover`
  holds the realized `Σ|w − w_prev|` (NULL for the first book of a chain). The book's `manifest_json` gains
  `ret_estimator` / `turnover_cap` only when they differ from the default, so a default book's manifest is unchanged.
- **Who it reaches.** `min_var`, `target_vol` and (through its frontier scan) `tangency`. **`risk_parity` ignores it** (a
  point, not an optimum — as before, it is projected onto the name and sector caps only), and so does **the `frontier`
  sweep** (its points are keyed by risk model, not by chain, so no previous frontier point exists to trade from).
  **`tangency` falls back to the frontier** whenever the cap binds (the turnover constraint cannot be linearised in the
  max-Sharpe y-space): its book is the best-Sharpe point of a 25-point scan, status `from_frontier`. The default
  `target_vol` (1.25× the minimum-variance volatility) is computed without the turnover constraint, so a cap changes the
  book and not the target.
- **Audit Q5.** `max_sharpe` also falls back to that frontier scan when the y-space program is infeasible under the caps
  (it used to raise `OptimizeError`).

### `caps.py` — the caps follow N (T-137)

The benchmark and the live book must be comparable at the same N, so the caps are a function of N
exactly as in `cycle` (T-134 decision 7). `quant` must not import `cycle`, so the rule is **copied**
into `quant.caps` (the way `quant.state` copies `cycle.state`) and pinned against `cycle`'s by
`tests/test_quant_caps.py`.

| | rule |
|---|---|
| `n_held` | `min(N, assets in the gated panel)` — the same meaning as `cycle`'s `n_held` |
| name cap | `1.5 / n_held`, no 0.10 floor (0.05 at `N = 30` with a panel of 30 or more — the old constant; 0.075 for a 20-asset panel). `--max-name-weight` wins; one below `1/n_held` cannot sum to 1 and is relaxed to `1/n_held`, **recorded**. `--max-name-weight 1.0` means no per-name cap |
| sector cap | 0.30, `--max-sector-weight` wins (`QuantSettings.max_sector_weight = None`, not reachable from a flag, means no sector cap at all). If the panel's sectors cannot hold it under the name cap (`Σ min(c, k·cap) + (assets with no sector)·cap < 1`, `k` the sector's asset count) it is relaxed to the smallest feasible value (`quant.caps.feasible_sector_cap`, `cycle`'s `_feasible_sector_cap` with a band floor of 0), **recorded** |
| number of names | **not limited** (no integer programming): the optimizer decides how many to hold; `N` only sizes the caps. The universe and liquidity gate are untouched and independent of every score |

`QuantSettings.max_name_weight` defaults to `None` (derive); passing today's `0.05` explicitly would
override the rule silently. `QuantSettings.top_n` defaults to 30. `build-risk-model` and `optimize`
take `--top-n`, `--max-name-weight` and `--max-sector-weight` (same names and meaning as `cycle`'s). Assets
with no sector are uncapped (as they always were) rather than one more capped group — the one place the
two copies differ, and `cycle`'s band floor `0.5/n_held` has no counterpart here.

**Recorded.** `quant_run.params_json["caps"]` (both `build-risk-model` and `optimize`) and each
`quant_portfolio.params_json` carry `top_n`, `n_held`, the **effective** `max_name_weight` /
`max_sector_weight`, and `relaxations` (`cap`, `requested`, `effective`, `reason`).

**Engine version `opt-v2`** (was `opt-v1`). A default-configuration book can now differ on a panel
smaller than `N = 30` (and a few-sector panel no longer fails on an infeasible sector cap); with a panel
of 30 or more the caps are the old 0.05 / 0.30 and a feasible `min_var` / `tangency` / `target_vol` /
`frontier` book is unchanged. Books under `opt-v1` stay beside the new ones (the version is part of
their key).

**The pilot-1 degeneracy.** The 20-asset pilot panel at the old constant cap of 0.05 (`20 × 0.05 = 1`)
forced every `min_var` / `tangency` / `target_vol` book to exactly 0.05 in every name — equal weight,
whatever the covariance. On a scratch copy of the pilot database at 2026-07-09, with the default `N = 30`
(`n_held = 20`, name cap 0.075, sector cap 0.30, no relaxation):

| book | `opt-v1` min / max weight | `opt-v1` vol | `opt-v2` min / max weight | `opt-v2` vol | names held |
|---|---|---|---|---|---|
| `min_var` | 0.0500 / 0.0500 | 0.1425 | 0.0080 / 0.0750 | 0.1250 | 18 |
| `tangency` | 0.0500 / 0.0500 | 0.1425 | 0.0091 / 0.0750 | 0.1331 | 20 |
| `target_vol` | 0.0500 / 0.0500 | 0.1425 | 0.0068 / 0.0750 | 0.1562 | 18 |
| `risk_parity` | 0.0466 / 0.0503 | 0.1418 | 0.0261 / 0.0750 | 0.1310 | 20 |

The largest sector sum is 0.30 or below in every book and the 15 frontier points stay within both caps.

### `persist.py` — the benchmark books

`run_optimize` loads the risk model for the as-of date **of its own manifest** (auto-building
it when absent), then writes one `quant_portfolio` row + a `quant_position` stint per
objective, plus a `quant_frontier_point` sweep when `frontier` is requested.
`quant_position` is keyed `(portfolio_id, asset_id, valid_from)` — many concurrent
books at one as-of, unlike `portfolio_position`'s `(asset_id, valid_from)`.

### `manifest.py` — the input versions of a run (T-090)

`quant` reads one versioned input, the return series named by `return_engine_version`.
(Until `T-132` it read a second, the `valuation` metric group's market capitalisation; the
caps now come from `kg_schema.market_cap`, which is not versioned, so `--metrics-version` no
longer changes any number — the manifest keeps its `metrics` entry so existing risk-model keys
stay comparable.) `resolve_quant_manifest`
resolves `--metrics-version` against what `fundamental_metrics` actually stores (via
`kg_schema.versions`) and returns a `QuantManifest` whose 8-hex **tag** is folded into the
keys the outputs already use: `quant_risk_model.model_version` (`rm-v2` → `rm-v2+3f9a1c2b`;
`rm-v1` before T-077)
and `quant_portfolio.engine_version` (`opt-v2` → `opt-v2+3f9a1c2b`). Because those tables
were already unique on those columns, **no schema change** is needed, and:

- the **same inputs give the same tag**, so a re-run refreshes the same risk model in place
  — and the same books in place too (fixed by `T-101`; before that a NULL `frontier_k`
  defeated their conflict key and every identical re-run added another copy);
- **different inputs give a different tag**, so the run writes *parallel* rows beside the first
  instead of overwriting it (they used to be `ON CONFLICT … DO UPDATE` on a constant key);
- `quant_expected_return` / `quant_covariance` / `quant_position` / `quant_frontier_point` hang
  off the model or portfolio id, so they follow automatically;
- `quant evaluate` already evaluates every book in its window, so books built on different
  inputs are compared side by side with no extra flag.

`--metrics-version` (on `build-risk-model` and `optimize`) takes a version (`metrics-v1`) or
`GROUP=VERSION` pairs; the default is the newest stored. An unstored version, or a group `quant`
does not read, is an error **before any run row is written** (CLI exit 1). Every run records its
manifest and tag in `quant_run.params_json`, and each model and book carries `manifest_json`
(exposed on `v_quant_risk_model` / `v_quant_portfolio`). The `live_book` snapshot is not
manifest-dependent and keeps the base version. **Note for consumers of the read contract:**
`v_quant_risk_model` / `v_quant_portfolio` are one row per stored model / book, so once runs at
two manifests exist for an as-of there are two rows — filter on `manifest_json` (or the version
string) to pick one.

### Version constraints, profiles and dry runs (T-093)

T-090's selection is exact. T-093 lets the user **constrain** every versioned input `quant`
reads, and resolves each constraint strictly against what is stored:

| Input | Flag | Commands | No flag (unchanged from before) |
|---|---|---|---|
| metric groups (`valuation`) | `--metrics-version` | `build-risk-model`, `optimize` | newest stored |
| return series (`qret-v<N>`) | `--returns-version` | `build-risk-model`, `optimize` | the configured `return_engine_version` |
| risk model (`rm-v<N>`) | `--risk-model-version` | `optimize` | `--model-version` (loaded, or built when missing) |
| corporate actions (`corpact-v<N>`) | `--corpact-version` | `build-returns` | per asset, the newest gateway engine it has |

**Grammar** (`kg_schema.versions.parse_constraint`): a bare version or `=version` is exact;
`>=version` is a minimum; `!=version` excludes one; comma-separated clauses combine
(`>=metrics-v1,!=metrics-v3`); `latest` is the newest stored. A constraint resolves to the
**highest stored version satisfying every clause**. For `--metrics-version` a clause may name a
group (`valuation>=metrics-v2`); a clause without one continues the group before it, or applies
to every group when it comes first. Every T-090 form (`metrics-v1`,
`valuation=metrics-v1,...`) parses and resolves exactly as before — pinned by
`tests/test_quant_version_constraints.py` against T-090's own resolver. `cycle` keeps T-090's
exact selection.

**Strict, never a fallback.** An unsatisfiable constraint is an error before any run row exists
(CLI exit 1), and it names why each stored candidate was rejected (`qret-v2 is older than
qret-v4; qret-v3 is older than qret-v4`). A version of the wrong family (`--returns-version
metrics-v2`) is rejected. Stored strings that are not versions `quant` reads — the pre-T-085
`corpact-v0-approx` / `corpact-v1-derived` history — are ignored. A risk-model constraint is
matched among the models stored for that as-of **over the same inputs** (`<label>+<tag>`); with
none it tells you to run `build-risk-model` first.

**Recorded, but keyed by the result.** The constraints as given and the profile name join the
manifest JSON (`quant_run.params_json`, `manifest_json` on models and books) under
`constraints` / `profile`. They are **not** part of the tag: two spellings that resolve to the
same versions are the same inputs. A book optimized from a non-default risk model folds that
model into its own tag (`book_tag`), so books from two risk models never overwrite each other;
a book from the default risk model (`rm-v2`; `rm-v1` before T-077) keeps T-090's key, so no stored book changed key.

**Profiles.** `--version-profile FILE` loads a TOML file of named constraint sets and
`--profile NAME` picks one (optional when the file holds exactly one); a flag on the command
line wins over the same key in the profile. Each command uses the keys for what it reads.

```toml
[profiles.baseline]
metrics = "metrics-v2"
returns = "qret-v2"

[profiles.pre_fix]
metrics = "valuation=metrics-v1"
returns = ">=qret-v1,!=qret-v3"
risk_model = "latest"
corpact = "corpact-v1"
```

**Tuning aids.** `python -m quant versions` lists, per input, every stored version with its row
count and first/last write time, marking the newest and the configured one. `--dry-run` on
`build-risk-model` / `optimize` opens the database read-only, prints the resolved manifest, the
risk-model version it would read or build and (optimize) the book version it would write, and
writes nothing.

**The corporate-action caveat.** `quant_return_daily` is append-only per return engine, so a
`--corpact-version` different from the one a series was built with only takes effect under a
new return engine version; `build-returns` says so when a pinned run writes no new rows.

### `benchmark.py` / `evaluate.py` — forward comparison

`build_internal_benchmark(conn, asset_ids=…)` synthesizes `SP500_EW_INTERNAL`
(`bench-v2`, T-108): an equal-weight, daily-rebalanced index over the **gated panel**,
the names `quant.universe.benchmark_gate` admits as of the window's start. That is the
same liquidity/history gate, with the same settings, that every book is built from --
but never a book's own hard-veto exclusion (`exclude_hard_vetoed` forced `False`), since
the benchmark is the investable universe, not the strategy's own filtered picture of it;
a veto can't also shrink the yardstick the strategy is graded against. Each day's return is
the mean of the panel's **simple** total returns (`expm1(tr_log_return)`); a name with no
row that day is left out of that day's mean. The level compounds `(1 + r)`, and
`log_return = log1p(r)`. `bench-v1` averaged *log* returns over every name with a row,
which gives the geometric mean, lower by about half the cross-sectional variance each day.
On production's 20-name panel it compounded at 5.98 %/yr against a true equal weight of
10.32 %.

`load_benchmark_csv` (`quant load-benchmark`) loads an external total-return series,
such as a cap-weighted `SPY_TR`, from a CSV with `date` and `total_return_level`
columns, as `csv-v1`. It refuses anything but ISO dates in strictly increasing order
and positive levels. Obtaining the file is a data-acquisition step outside this repo.

`run_evaluate` rebuilds `SP500_EW_INTERNAL` over the gate as of `--from`, or, for
any other `--benchmark`, reads a loaded series and never overwrites it (refusing
when none is loaded for the window). It records the benchmark version and panel on
the `quant_run` row. It freezes each persisted book's weights at its as-of date, walks
forward trading days, computes the weighted simple total return, compounds it,
subtracts the return of the benchmark version it just built or chose, and writes
`quant_benchmark_performance` under `perf-v3`. A held name missing one day's forward return
but present again later (a `price_daily` gap — its move is folded into the return of the day
it reappears, since the return engine bridges the gap from the last available close) counts
as a 0% that day with its weight kept, not renormalized away; only a name with no later return
at all (delisted, or its series ends) is dropped, with the remaining names' weights
renormalized from that day on (T-111; `build_internal_benchmark` applies the same rule).
`perf-v1` and `perf-v2` rows stay stored under their version, and
`v_quant_benchmark_performance` shows the latest version per (book, date).

**`perf-v3`: net of a turnover cost (T-077).** Each optimized book pays `TURNOVER_COST_BPS / 10⁴ × Σ|w − w_prev|` with
`TURNOVER_COST_BPS = 10` (a constant beside `PERF_ENGINE_VERSION` in `evaluate.py`: **`perf-v3` means 10 bps by definition**; there is no flag or
setting, and a different cost is a new perf engine version) **once, on its first forward day**; the later days are untouched. `w_prev` is the previous
book of its chain (`turnover.py`), so the first book of a chain pays `bps × Σ|w|`, i.e. `bps`. The cost is charged on
**target** weights, not on weights drifted by a month of returns — a declared simplification that slightly overstates the
trade a daily-rebalanced book makes and understates one that drifts. **`perf-v3` `realized_return`,
`cumulative_return` and `active_return` are therefore net of that cost**; the benchmark (an equal-weight index) and the
`live_book` snapshot (which has no previous book to trade from) pay none, so the live-vs-benchmark active returns are not like for like. Each book's cost (turnover, previous book, bps, cost) is recorded in the `evaluate` run's own
`quant_run.params_json["turnover_cost"]`, keyed by portfolio id — `evaluate` never rewrites a book row `optimize` wrote. The 15 bps sensitivity lives in `scripts/verify_t077.py` only. `perf-v2` (gross) rows are untouched. The live
`cycle` book (`portfolio_position`) is snapshotted into
`quant_portfolio(kind='live_book')` so `v_quant_vs_live` and
`v_quant_benchmark_performance` make the system-vs-base-case comparison a single
join. Run `optimize --as-of <cycle_date>` for each `cycle` SELECTION so the
benchmark and the live book share as-of dates.

### `repair.py` — voiding a stale `live_book` snapshot (T-121)

If the live book was ever corrupted and later repaired (T-104), a `live_book` snapshot taken
before the repair is a permanent, stale copy of the wrong book -- nothing re-derives it, and any
`evaluate`/dry run that reads it produces meaningless active-return figures.
`quant void-portfolio --portfolio-id N [--apply]` deletes one: `plan_void` reads the row, its
`quant_position` stints and `quant_benchmark_performance` row count (read-only; refuses a
`kind` other than `live_book` -- an optimized book nothing here should ever delete), and
`apply_void` deletes it in one transaction (`quant_position`/`quant_benchmark_performance`
cascade; `quant_frontier_point` is deleted explicitly first, since a real book's own frontier
points must never vanish as a side effect). Dry run (print the plan) unless `--apply`, the same
convention as `cycle undo-run`.

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
| — | live book vs each optimized book, per name (+ `LIVE_ONLY` rows for live names no book holds, T-042) | `v_quant_vs_live` |

## Read contract for the knowledge-graph views (T-077)

No column of any `v_*` view changes, no view definition changes, `kg_schema.ensure` and `schema_version` are untouched (no
migration). What a consumer sees differently:

| view | change |
|---|---|
| `v_quant_risk_model` | New models are `rm-v2+<tag>` (`rm-v1` models stay as they are). `manifest_json` gains a `carhart` object (or `{"unavailable": reason}`). `ret_estimator` is **unchanged in meaning**: the estimator configured when the model was built (normally `equilibrium`), not the list of μ it stores — the four μ vectors are in `quant_expected_return`, which is not projected. |
| `v_quant_portfolio` | **There is no `ret_estimator` column here, and none was added.** A book under a non-default estimator or a turnover cap is a *parallel row* beside the default at the same `(as_of, kind)`: `engine_version` is `opt-v2.mu-carhart.to-0.5+<tag>` (default books keep their key), `manifest_json` carries `ret_estimator` / `turnover_cap` for those books, and `turnover` is now filled (the realized `Σ|w − w_prev|`, NULL for the first book of a chain). `is_current` marks one row per `(as_of, kind, frontier_k)` and **a variant book is never current** (its `opt-v<N>` is not all digits before the first `+`), whatever the compute order; `v_quant_vs_live`'s `LIVE_ONLY` rows follow the default book only. |
| `v_quant_benchmark_performance` | New rows are `perf-v3` (net of the turnover cost); the view shows the newest version per `(book, date)`, so a re-evaluated book switches from `perf-v2` to `perf-v3`. |
| `v_quant_frontier_point` | unchanged; see the caveat below on what the points belong to. |

## Known gaps / caveats

- **`equilibrium` needs cover-page share counts that production does not hold yet (found in T-077's verification).** The
  as-of market cap (T-132) rests on `filing_cover_shares`, which `fundamental_agent` fills as it ingests a filing. The
  production database's `filing_cover_shares` is empty (its filings predate T-132), so `build-risk-model` refuses there
  (`no panel asset has a market cap`) until the fundamental ingestion is re-run (`T-100`). The T-077 verification filled
  the table on a scratch copy from the EDGAR gateway, with `fundamental_agent`'s own client and parser.
- **The `frontier` sweep is keyed by risk model, not by book.** `quant_frontier_point` is unique on `(model_id, k)`, so
  two `optimize` runs on one model with different `--mu` overwrite each other's points (and the sweep takes no turnover
  cap). Read it right after the run, or keep one database per estimator.
- **Several books at one `(as_of, kind)`.** A non-default estimator or a turnover cap makes a parallel book beside the default
  (the marks in its `engine_version`); `v_quant_portfolio.is_current` never flags a variant, so the default book stays current
  whatever the compute order. A consumer that wants a variant reads it by `engine_version` / `manifest_json`.

- **Total-return quality** hinges on the gateway. Dividends come only from
  `portfolio-data-mining`'s yfinance-backed endpoint, which is unofficial and has no
  SLA. It is live and verified (`T-052`, 2026-09-21: every asset fetched), but a
  yfinance symbol it does not recognise still returns a clean empty result, and
  dividends with ex-dates after the last `price_daily` bar cannot fold into the
  series until prices are refreshed. A series built while `corporate_action` has no
  gateway rows would be price-only — `build-returns` now refuses to do that (`T-086`).
- **Survivorship bias (partly addressed).** The universe is now read point-in-time
  from `universe.db`, which carries real `valid_from` / `valid_to` stints, so
  `build-risk-model --analysis-date D` gates to the constituents that were in the
  index on `D`. `price_daily` still holds no delisted names, so a name that left
  the index before `D` but was a member then still contributes no returns — a
  residual upward bias. Mitigations: `quant_risk_model.panel_spec_json` freezes
  each run's exact universe + dates + sha256; never delete `price_daily` rows for a
  name that later leaves the index; backfill delisted-name prices.
- **Book key and NULL `frontier_k` (`T-101`, fixed 2026-09-25).** `quant_portfolio` is keyed
  `(as_of, kind, frontier_k, engine_version)` with `frontier_k` NULL for every non-frontier
  book; SQLite NULLs never conflict, so its `UNIQUE` / `ON CONFLICT` never matched and each
  identical `optimize` / `evaluate` re-run inserted another copy of every book (the read-back
  then returned the oldest copy, which kept the positions). `insert_portfolio` now matches
  `frontier_k IS ?` and updates the stored book; migration **m007** (run via `migrate`) merges
  any existing duplicates and adds the NULL-safe unique index `ux_quant_portfolio_book` on
  `IFNULL(frontier_k, -1)`. The code fix works without the migration; the migration makes the
  database itself refuse a duplicate.
- **No vendor risk-free / benchmark series** yet — a constant rf and an internal
  equal-weight benchmark are the v1 defaults; the tables + CSV loaders are in place.
- `price_daily.event_time` / `ingested_at` are NULL for every row — a
  `pricing_agent` cleanup, out of scope here. `quant` reads `price_daily.date` /
  `price_observation.obs_date`, which are never NULL.
- Re-introducing a `QUANTITATIVE` `score_type` is deliberately **not** done — the
  Markowitz output is a portfolio, not a per-asset score, and lives entirely in the
  `quant_*` tables. `score_snapshot` is untouched.
