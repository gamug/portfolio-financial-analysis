# `cycle/`

Strands-driven selection & monitoring cycles (roadmap steps 6 & 8). Produces
TECHNICAL / QUANTITATIVE / SECTOR `score_snapshot` rows (plus per-sector
`sector_aggregate_snapshot`), normalizes the score types cross-sectionally,
evaluates the `rule_catalog` into `veto` rows with a **T-1 contagion lag**, ranks
the universe, and (for a selection cycle) writes `portfolio_position` targets and
`cycle_ranking`.

```bash
uv run python -m cycle select  --date 2026-06-30 [--top-n 30] [--dry-run]
uv run python -m cycle monitor --date 2026-07-31
uv run python -m cycle backfill --from 2024-01-01 --to 2026-01-01 --step-days 7
```

Runs as a **checkpointed topological runner** — `cycle_checkpoint` (relational),
not the framework, is the source of truth for resume. A Strands
`multiagent.GraphBuilder` can drive the same step graph later without changing that
contract.

## Configuration (`config.py`)

`CycleSettings.load()` needs `KG_FINANCIAL_DB`; LLM creds optional. Knobs:
`universe` (`"SP500"`), `top_n` (30), `score_weights` (FUND .4 / QUANT .3 / TECH
.2 / SEM .1), `weight_scheme` (`equal` | `score_proportional` | `inverse_vol`),
`max_name_weight` (.10), `max_sector_weight` (.30), `soft_veto_penalty` (15 pts).

## Files

### `db.py`

`connect` (no-WAL) + `ensure_schema` → `kg_schema.ensure`.

### `state.py` — resume bookkeeping

`open_cycle(conn, cycle_type, cycle_date, params) -> id` (upsert on
`(cycle_type, cycle_date)` → resume). `checkpoint(conn, id, step, status,
detail)`. `done_steps(conn, id) -> set[str]` — steps with status `done` are
skipped on re-run. `finish_cycle(conn, id, status)`.

### `data.py` — read helpers (plain SQL, no agent imports)

`active_universe` (open `universe_membership` as of the date; falls back to all
`assets`), `latest_metrics` (newest filing with `period_end ≤ date`, keyed
`"group.name"`), `latest_price_observation`, `last_fundamental_dates`,
`latest_fundamental_score`, `latest_semantic_score`, `market_cap_estimates` (reads
the stored `valuation.market_capitalization` metric inputs).

### `scores/normalize.py`

`cross_sectional_z(values, winsor=0.02)` — winsorized z-score against the cohort
(zeros if degenerate). `z_to_score(z)` → `clamp(50 + 10z, 0, 100)`.
`normalized_scores(raw)` composes both. `rank_pct(values, higher_is_better=True)` —
cross-sectional percentile rank in `[0,1]`, `None` in → `None` out.

### `scores/technical.py` — `SCORE_TYPE = "TECHNICAL"`, `compute(observations) -> list[RawScore]`

Proposed definition (user to refine). Cross-sectionally ranks each sub-signal and
blends by weight: 12-1-ish `momentum_63d` (.35, ↑), `momentum_21d` (.15, ↑),
`realized_vol_90d` (.20, ↓), `atr_14/close` (.15, ↓), `max_drawdown_90d` (.15,
less-negative-is-better). `raw_value` = 100 × weighted mean of available
percentiles.

### `scores/quantitative.py` — `SCORE_TYPE = "QUANTITATIVE"`, `compute(metrics) -> list[RawScore]`

Proposed. Factor blend: **value** .45 (`valuation.free_cash_flow_yield`,
`valuation.enterprise_fcf_yield`, synthetic `earnings_yield`), **quality** .40
(`profitability.return_on_equity`, `roic.return_on_invested_capital`,
`cashflow.free_cash_flow_margin`, low `leverage.debt_to_equity`), **size** .15
(synthetic `neg_log_market_cap`). Each factor = mean of its available sub-metric
percentiles.

### `scores/sector.py` — `SCORE_TYPE = "SECTOR"`, `roll_up(sector_of, technical_raw, technical_norm)`

Returns `(list[SectorAggregate], momentum: dict[asset_id, float])`. Per GICS
sector: `member_count`, `mean_raw`, `mean_normalized` of members' TECHNICAL score.
`momentum[asset_id]` = own raw − sector mean raw (the `SectorRelativeMomentum`
signal). Sector-less assets and assets with no TECHNICAL score this cycle are
dropped. Pure derivation — nothing fetched.

### `rules/`

- `base.py` — `VetoHit(asset_id, rule_id, severity, evidence)`, `RuleContext`
  (`metrics`, `price_obs`, `last_fundamental` per asset), `Rule` protocol
  (`RULE_ID`, `SEVERITY`, `DESCRIPTION`, `PARAMS` property, `evaluate(ctx)`).
- `builtin.py` — `RULES`: `LEVERAGE_EXTREME` (`debt_to_equity > 3`, HARD),
  `NEGATIVE_FCF` (`free_cash_flow_margin < 0`, HARD), `LIQUIDITY_DISTRESS`
  (`current_ratio < 1`, SOFT), `PRICE_CRASH` (`max_drawdown_90d < −0.35`, SOFT),
  `EARNINGS_MISSING` (no FUNDAMENTAL score within 400 days, SOFT).
- `__init__.py` — `seed_catalog(conn)` (`INSERT OR IGNORE` into `rule_catalog`,
  never overwrites), `enabled_rules(conn)`.
- The rule catalog and the per-run blend (`score_weights` + knobs in
  `cycle_run.params_json`) are read-projected by `kg_schema` as `v_rule_catalog`,
  `v_weight_scheme`, `v_weight_component` — no separate export step.

### `writers.py`

`write_scores` (TECH/QUANT → `score_snapshot`, upsert on the natural key),
`apply_normalized`, `write_vetoes` (insert new hits, `UPDATE … cleared_at` for
lapsed ones — never delete), `hard_vetoed_as_of(conn, cutoff)` / `active_soft_vetoes`
(the T-1 filter: `cycle_date ≤ cutoff`, `cleared_at IS NULL`), `write_ranking`
(replaces `cycle_ranking` for the run), `sync_positions` (open new / close vanished
`portfolio_position` stints — history immutable — reweight incumbents).

### `construction.py` — `target_weights(cands, *, top_n, scheme, max_name_weight, max_sector_weight)`

`Candidate(asset_id, blended_score, sector_id, realized_vol_90d)`. Take the top
`top_n` → raw weights by scheme → **alternate** name-cap (water-fill: pin over-cap
names, spread the rest among uncapped) and GICS-sector-cap until both hold or it
converges. Caps are approximate (converge to within rounding).

### `orchestrator.py` — `run_selection` / `run_monitoring(settings, cycle_date, *, conn=None, fundamental_hook=None)`

`_run` executes the step list, each wrapped by `_do` which skips it if already
`done`:

```
universe → fundamental → technical → quantitative → semantic_read →
normalize → sector → veto → rank → [positions]   (positions is SELECTION only)
```

- **fundamental** — delegates to `fundamental_hook`; with no hook it just reports
  the count of existing FUNDAMENTAL scores.
- **semantic_read** — a no-op that records a checkpoint noting the aggregation runs
  in the integration repo; `rank` still picks up any pre-existing SEMANTIC rows.
- **normalize** — per score_type, z-score the cohort → `z_to_score` →
  `normalized_score`. FUNDAMENTAL is normalized against each asset's latest filing
  snapshot.
- **sector** — `scores/sector.roll_up`: per GICS sector, mean of members'
  TECHNICAL score → one `sector_aggregate_snapshot` row; each asset's own raw
  minus that mean → a `score_snapshot` row of type `SECTOR` (`SectorRelativeMomentum`;
  negative = lagging its sector). Not in the blend — a standalone observation.
- **rank** — blended score = weighted mean of available `normalized_score`s
  (weights renormalized over present types), minus `soft_veto_penalty` per active
  SOFT veto. T-1 HARD-veto assets are marked `vetoed` (excluded from selection).
- **positions** — build `Candidate`s from the non-vetoed ranked rows →
  `target_weights` → `sync_positions`; reflect selection back into `cycle_ranking`.

`CycleReport` records `steps_run` / `steps_skipped`, `selected`, `vetoed`.

### `fundamental_hook.py` — `make_hook(settings) -> FundamentalHook | None`

Wired slot for running the Strands `FundamentalAnalyst` inside a cycle. Currently
returns `None` (re-scoring a filing on demand needs the fundamental pipeline's
fetch+analyze path exposed as a reusable call). Until then the cycle consumes
whatever FUNDAMENTAL `score_snapshot` rows `fundamental_agent run` already wrote.

### `cli.py`

`select` / `monitor` / `backfill`. `--dry-run` on `select` sets `top_n = 0` so
`rank` / `cycle_ranking` run but no positions are touched.

## Gotchas

- `cycle/` may import both agents; only `cli.py` should call their `pipeline.run`
  directly. The score/step modules read shared tables via SQL, never by importing
  `pricing_agent`.
- TECH / QUANT use `cycle_date` as `event_time` → exactly one row per
  `(asset, score_type, day)`, so same-day re-runs upsert rather than duplicate.
- The `_run` function is deliberately one long linear sequence (`# noqa: C901`);
  its structure is the step list, and `cycle_checkpoint` makes it restartable.
