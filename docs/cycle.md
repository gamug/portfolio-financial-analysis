# `cycle/`

Strands-driven selection & monitoring cycles (roadmap steps 6 & 8). Produces
TECHNICAL / VALORIZATION / SECTOR `score_snapshot` rows (plus per-sector
`sector_aggregate_snapshot`), normalizes the score types cross-sectionally,
evaluates the `rule_catalog` into `veto` stints with a **T-1 contagion lag**, ranks
the universe, and (for a selection cycle) writes `portfolio_position` targets and
`cycle_ranking`.

```bash
uv run python -m cycle select  --analysis-date 2026-06-30 [--top-n 30] [--weight-scheme score_tilt|equal|score_proportional|inverse_vol]
                               [--max-name-weight F] [--max-sector-weight 0.30]
                               [--pin A,B] [--exclude A,B] [--exclude-sectors S,T] [--only-sectors S,T]   # preferences: only with --dry-run
                               [--dry-run] [--allow-stale-prices] [--allow-dirty] [--allow-stale-dq-gate] [--allow-backdated-veto]
uv run python -m cycle monitor --analysis-date 2026-07-31 [--allow-stale-prices] [--allow-dirty] [--allow-stale-dq-gate] [--allow-backdated-veto]
uv run python -m cycle backfill --from 2024-01-01 --to 2026-01-01 --db /tmp/backfill.db --step-days 7 [--top-n 30] [--weight-scheme ...] [--max-name-weight F] [--max-sector-weight 0.30]
                                [--pin ...] [--exclude ...] [--exclude-sectors ...] [--only-sectors ...] [--allow-stale-prices] [--allow-dirty] [--allow-stale-dq-gate] [--force]
```

`--analysis-date` is the canonical name for the cycle date; `--date` is kept as an
alias (passing both with different values is a CLI error), and both default to
today. It is the `cycle_run.cycle_date`; `cycle_run.code_version` records the git
tag. `backfill` uses `--from`/`--to` (each stepped date is its own as-of) and
rejects `--analysis-date`.

`backfill` replays selection cycles through `run_replay`, `cycle_run.cycle_type = 'REPLAY'`
— a distinct run-log identity from a live `select`/`monitor` at the same date. Only the
`positions` step is isolated: it lands in `portfolio_position_replay` (`cycle/replay.py`), a
table with `portfolio_position`'s own shape and T-104 stint-immutability triggers, never read
by, or refused for conflicting with, the live book. Every other step (`score_snapshot`,
`veto`, `sector_aggregate_snapshot`, `cycle_ranking`) writes the *same shared tables* the live
`select`/`monitor` and `quant`'s universe gate read — a replayed date's veto stints, for
instance, are indistinguishable from a live cycle's own (T-115 review; T-125 gives `--force`
its own veto-transition reset for exactly this reason — see below). `--db` is therefore
**mandatory** and refused outright when it names the configured production database
(`KG_FINANCIAL_DB`) — by actual file, not string: `_same_database` (`cycle/cli.py`) uses
`os.path.samefile` when both paths exist, so a relative alias (`--db data/financial.db`) or a
symlink to the production file is refused exactly like the canonical path itself (T-115
review). Run `backfill` only against a throwaway copy (`cp "$KG_FINANCIAL_DB"
/tmp/backfill.db` first), never the live one.

Re-running `backfill` over an already-completed date resumes/skips it as usual
(`cycle_checkpoint`); pass `--force` to reset the replay book from `--from` onward first
instead — every replay stint and `cycle_run` row on or after `--from` is dropped/reopened, with
**no** upper bound at `--to`. A replay is path-dependent: leaving a later stint in place (from a
prior, further-reaching backfill) would immediately trip the out-of-order-replay guard against
it the moment `--from` redoes, half-deleted (T-115 review). `--force` also undoes veto
transitions on or after `--from` the same way (T-125) — `veto` stints raised there are deleted,
ones cleared there are reopened, and a surviving stint's `last_seen_on` is rolled back to its own
`raised_on` if a hit on or after `--from` had bumped it forward (PR #103 review: left in place,
that `last_seen_on` is itself a transition dated on or after `--from`, so the guard `--force`
exists to clear the way for still finds one and refuses the immediate redo) — since `veto` is not
isolated the way `portfolio_position` is (above). A temporal stint's `expires_on` (T-070) is put back too, **exactly**: each extension is recorded in `veto.expiry_history_json`, and the ones dated on or after `--from` are undone (a stint cleared there is reopened with the expiry it had), so a replay from `--from` reproduces the original stints. Nothing *before* `--from` is touched.

**Veto lifecycle (T-125).** `veto` holds stints (`raised_on`/`cleared_on`/`last_seen_on`), not
per-cycle-date events: a HARD veto clears the first cycle its rule re-evaluates the asset and
finds it no longer breached (rather than staying permanent once raised), and a SOFT veto held
across many cycles is one open stint, charged `soft_veto_penalty` once, not once per cycle. A
rule that could not evaluate an asset this cycle (missing data) leaves any open stint untouched
— never mistaken for "cleared." `kg_schema.queries.veto_out_of_order_reason` refuses a cycle
date older than the latest veto transition already recorded, unless `--allow-backdated-veto` —
checked, like the positions guard, only when the `veto` step is actually about to run (not on a
plain resume of an already-completed date), and when it does override, the step runs read-only:
it evaluates the rules (so `vetoed`/the console output still reflect them) but never calls
`write_vetoes`, since applying an older date's transitions would delete or roll back later,
still-current ones instead of replaying history (PR #103 review) — `rank` already reads the
existing stints point-in-time regardless (a REPLAY run instead resets the way past via `--force`,
above, since it has no override flag of its own — the same reason `portfolio_position_replay`
has none).

Both cycle types refuse a `cycle_date` past `price_daily`'s last stored date (the price
spine) — TECHNICAL/veto read prices, so a stale as-of would silently score against data
that is, at best, weeks old (T-110) — unless `--allow-stale-prices`, which records why on
`cycle_run.params_json` and prints a CLI `WARNING`.

Every cycle also refuses to write its `cycle_run` row at all when its own `code_version()`
is dirty (uncommitted changes) — its results would come from code `HEAD` alone can't
reproduce (T-114) — unless `--allow-dirty`, which records why on the run and a CLI
`WARNING`, the same convention as `--allow-stale-prices`.

Every cycle also refuses to run when `data_quality_issue` holds rows under an older Ring-1
gate version than the current one but none under it yet (`kg_schema.queries.
stale_gate_version_reason`, T-116, PR #95 review) — a gate-methodology bump (e.g. `dq-v1` ->
`dq-v2`) landed without its one-time re-gate (`python -m fundamental_agent quality`) having
run, which would otherwise leave every quarantine and HARD `DQ_*`/`DATA_QUALITY` veto silently
reading as clean — unless `--allow-stale-dq-gate`, same recording convention as the other two
guards. Safe (no refusal) when `data_quality_issue` has no rows at all — Ring-1 simply hasn't
run yet, a bootstrap situation, not a version regression.

Runs as a **checkpointed topological runner** — `cycle_checkpoint` (relational),
not the framework, is the source of truth for resume. A Strands
`multiagent.GraphBuilder` can drive the same step graph later without changing that
contract.

## Price vetoes and TECHNICAL v2 (T-070)

Three vetoes read `price_observation` at `priceobs-v2` (`CycleSettings.observation_engine_version`: the
cycle reads that one version, never "latest per day"; it **refuses** with the `pricing_agent` command to run
when the table holds rows but none at that version). An observation older than 7 calendar days at the cycle
date is not evaluated (a halted or delisted name would otherwise be re-confirmed forever by a stale row).

| Rule | Severity | Fires when (all must hold) |
|---|---|---|
| `BREAK_TREND_200` | SOFT | `close < 0.95 · sma_200` |
| `VOLATILITY_SHOCK` | HARD, temporal (10 sessions) | **absolute** `vol_5d / vol_60d_base > 2.5` AND **relative** `ratio > 2.5 · median(ratio of the name's GICS sector)` |
| `CRASH_Z_SCORE` | HARD, temporal (10 sessions) | **absolute** `z = (ret_5d − 5·mu_60d_base) / (vol_60d_base·√5) < −2.5` AND **relative** `(ret_5d − median(ret_5d of the name's GICS sector)) / (vol_60d_base·√5) < −2.5` |

All comparisons are strict. `PLAN.md` writes the crash score as `(R5d − μ60d)/σ60d`; a 5-day return is compared
here with the daily baseline scaled to 5 days (mean × 5, sd × √5, independent daily returns), so the units
agree. The relative leg is in the same units: the gap to the sector's median 5-day return, over the name's own
`vol_60d_base·√5`. **Both legs are required** for both rules, and each rule's `PARAMS` in `rule_catalog`
carries the formulas, thresholds and the fallback below.

**The sector group.** The names the rule evaluates that day (it has the inputs, and the observation is
fresh) and that share the name's GICS sector (today's sector, checklist L-03). A sector with fewer than 5 such
names -- and a name with no sector -- uses the **whole cross-section** of that day instead: the same fallback
`TECHNICAL`'s sector-Z uses (`MIN_SECTOR_NAMES = 5`). On the 503-name copy it never applied (0 of 12,364
sector-dates; the smallest sector, Energy, has 21 names); on the 20-name pilot it **always** applies (9,016 of
9,016 sector-dates: no sector has more than 4 names), so there the relative leg is "against the pilot's
median".

**Why the relative leg: a declared limitation.** As first specified (absolute leg only) the two HARD rules held
up to **80%** of the 503 names under a veto at once (2025-04-10) and more than 20% on 157 of 1,189 dates --
a broad sell-off would have excluded most of the index (table below). The relative leg stops a move a whole
sector makes together from raising a HARD veto: **by design, a sector-wide shock no longer vetoes the names in
it.** Those are systematic moves, carried by the TECHNICAL score and by the risk model, not idiosyncratic
distress. The cost is measured in `docs/model_fixes.md` (the sector-dates the absolute rules fired on 50% or more of a sector and
the relative ones did not).

**Temporal stints (on top of T-125's stints and the unchanged T-1 lag).** A temporal rule's stint opens with
`expires_on = raised_on + 10 NYSE sessions` (`kg_schema.trading_calendar.add_trading_days`) and **cannot clear
before it**, even when the condition is gone: it stays open, untouched (`last_seen_on` keeps the last cycle the
condition was confirmed). At the first cycle on or after `expires_on`: the condition gone -> the stint clears
(`cleared_on` = that cycle); still holding -> it stays open and `expires_on` moves to that cycle + 10 sessions.
A rule that could not evaluate the asset leaves its stint alone, past the expiry too. `hard_vetoed_as_of`
is unchanged: a stint is active at cutoff `C` iff `raised_on <= C AND (cleared_on IS NULL OR cleared_on > C)`.
**With cycles further apart than 10 trading days the hold is effectively one cycle**: the next cycle is already
past the expiry, so a gone condition clears at once -- and the T-1 lag still keeps the name out of that cycle's
ranking. With the pilot's weekly cycles (5 sessions) a stint is held through the next cycle and
re-evaluated on the one after (14 calendar days). `veto.expiry_history_json` records each extension
(`{"on": cycle date, "was": the expiry it replaced}`) so that a same-date re-run of `write_vetoes` and
`reset_replay_range` (`cycle backfill --force`) restore `expires_on` exactly; a cleared stint keeps the expiry
it had. `cycle undo-run` repairs `portfolio_position` only and never touches `veto` (a test pins it).
`report.vetoed` (the console count) is now the open HARD stints after the cycle, not the cycle's hits: a held
stint is still excluded from the next ranking.

**TECHNICAL v2** (`scores/technical.py`, model `technical-v2`) replaces the five rank-percentile signals:
`mom_12_1 = close(t−21) / close(t−252) − 1` (Jegadeesh & Titman 1993: skip the last month; derived exactly as
`(1 + momentum_252d) / (1 + momentum_21d) − 1`, needs 253 closes), `realized_vol_90d` (lower is better) and
`max_drawdown_90d` (less negative is better). Each is standardized within its GICS sector on the cycle date,
`z = (x − sector mean) / sector sd` (population sd, as `cross_sectional_z`; no winsorizing at this stage; a
signal with no spread has z = 0), with the same < 5-name fallback to the cross-section.
`raw = 0.50·z_mom − 0.30·z_vol + 0.20·z_dd`, divided by the sum of the weights of the signals present.
**Coverage floor (audit C5):** fewer than 2 of the 3 signals -> **no TECHNICAL score** (the asset is absent
from the blend, which renormalizes over the components present, T-141); before this an empty asset scored a
neutral 50. `normalize` then maps `raw` to `50 + 10z` as for every score. A `cycle_run` recorded before T-070
holds `technical-v1` scores: re-run it with `cycle backfill --force` (the run records
`params_json.technical_version`).

**`LIQUIDITY_DISTRESS` (audit N7)** is SOFT only when `current_ratio < 1.0` **and** a cash-coverage test fails:
`interest_coverage < 1.5` or `operating_cash_flow_margin < 0` (either; a missing metric is not a failed test; with
neither available the asset is not evaluated). It does not apply to GICS **Financials** or **Utilities**
(APP-09: no classified balance sheet / a regulated one): those names are evaluated and never hit, so a stint
opened before the exemption closes. `T-071`'s company profile replaces the GICS test (a pointer sits in
`rules/builtin.py`). Operating cash flow over current liabilities is not a stored metric (adding one is `T-151`'s
scope), so the cash leg is the stored operating-cash-flow margin. The 1.5 floor is `LEVERAGE_EXTREME`'s existing
negative-equity coverage floor (T-116), not a new number; no source sets a current-ratio-conditioned test, so this
is a **declared calibrated policy** with the sensitivity table in `docs/model_fixes.md` (T-070). The broader
check repeats after `T-148` (it changes the interest-expense and cash inputs).

## Configuration (`config.py`)

`CycleSettings.load()` needs `KG_FINANCIAL_DB`; `KG_UNIVERSE_DB` and LLM creds
optional. Knobs:
`universe` (`"SP500"`), `top_n` (30 — N, the number of names the book is sized for),
`score_weights` (FUNDAMENTAL, VALORIZATION and TECHNICAL at 1/3 each — T-141; SEMANTIC is out of
the defaults until Work item 4, see *Composite weights* below), `weight_scheme` (`score_tilt`
by default | `equal` | `score_proportional` | `inverse_vol`), `max_name_weight` (**`None`**:
derived — `1.5/n_held` for `score_tilt`, 0.10 for the legacy schemes; an explicit value wins, which
is why the default is `None`: a `0.10` default would silently override `1.5/N`),
`max_sector_weight` (.30), the **preferences** `pins`, `exclude`, `exclude_sectors` (tuples of
names, empty by default) and `only_sectors` (`None` = every sector), `soft_veto_penalty` (15 pts),
`observation_engine_version` (`priceobs-v2`, T-070: the one `price_observation` version read),
`unscored_max_share` (.05 — T-119: `rank` refuses outright past this share of the universe
with no FUNDAMENTAL score at all, up to and including 100%, PR #99 review).
`CycleSettings.construction()` is the subset that decides the book (N, scheme, caps,
preferences — normalized: trimmed, case-folded, sorted); a resume must not change it (below),
nor `score_weights` (T-141, `check_score_weights`).

## Files

### `db.py`

`connect` (no-WAL) + `ensure_schema` → `kg_schema.ensure`.

### `state.py` — resume bookkeeping

`open_cycle(conn, cycle_type, cycle_date, params, *, code_version=None) -> id`
(upsert on `(cycle_type, cycle_date)` → resume). `checkpoint(conn, id, step,
status, detail)`. `done_steps(conn, id) -> set[str]` — steps with status `done`
are skipped on re-run. `finish_cycle(conn, id, status)`.

`check_construction(conn, cycle_type, cycle_date, construction)` (T-136) raises
`ConstructionMismatch` when the run for that (type, date) already recorded other
book-construction settings (see *Resuming*, below); `merge_params(conn, id, updates)` merges keys
into `cycle_run.params_json` (secrets stay redacted).

`check_score_weights(conn, cycle_type, cycle_date, score_weights)` (T-141) raises
`ScoreWeightsMismatch` when the run for that (type, date) recorded other composite weights. `open_cycle`
writes `params_json.score_weights` once, on the run's first attempt, and a resume never rewrites it;
`v_weight_scheme`, `v_weight_component` and `v_cycle_ranking_component` read that value, so a resume that
ranked under other weights would leave the read contract disagreeing with the blend. It applies to every
cycle type (a `MONITORING` run ranks too), runs before `open_cycle`, and leaves a run that recorded no
weights alone. The weights have no CLI flag: a run recorded under the old `0.4/0.3/0.2/0.1` is refused by
the new defaults, so re-run its date from a fresh run (for a `REPLAY`, `backfill --force` resets the range)
or choose another date.

`check_manifest(conn, cycle_type, cycle_date, tag)` (T-090) raises `ManifestMismatch` when a
`cycle_run` for that (type, date) already exists **built on a different manifest**. A cycle
run is unique per (type, date) and its outputs are keyed by date, so — unlike `quant`'s books —
a second run over other input versions cannot sit beside the first; resuming it would mix the
two. It runs *before* `open_cycle`, which would otherwise flip the earlier run back to
`running`. A run recorded before T-090 has no manifest and is left alone.

### `data.py` — read helpers (plain SQL, no agent imports)

`active_universe(conn, universe, cycle_date, universe_db_path=None)` — reads
`universe.db` point-in-time (`members_asof` → `resolve_asset_ids`) and returns the
matching `assets` rows; raises loudly if `universe.db` yields nothing or nothing
resolves. `latest_metrics(conn, date, versions)` (the newest filing *usable* on the date —
`available_at ≤ date`, T-106/T-107 — keyed `"group.name"`), `latest_price_observation(conn, cycle_date, engine_version)` (each asset's newest row at that one `engine_version`; `MissingObservations` when the table has rows but none at it, T-070),
`latest_fundamental_rows` (each asset's newest FUNDAMENTAL snapshot usable on the date; `last_fundamental_dates` and `latest_fundamental_score` read it — an asset with none at all is
simply absent as a key, never present with a `None` value), `unscored_assets(asset_ids, scored)`
(the universe's own key-membership diff against that, T-119 — includes every asset when none is
scored at all, PR #99 review: a cycle dated before any filing is public yet, T-106/T-107, is not
exempt), `too_many_unscored_reason`/`TooManyUnscored` (refuses `rank` when more than
`CycleSettings.unscored_max_share` of the universe has no score at all, up to and including
100% — not overridable, no `--allow-*` flag, since it isn't a "deliberate run despite a known
gap" case the way the other guards are),
`latest_semantic_score`, `market_cap_estimates(conn, date, asset_ids)` (T-132: each asset's
cap from the shared `kg_schema.market_cap` reader — the latest cover-page share count usable
on the date × the close on or before it; `None` for an asset it refuses to value, which only
drops that asset's `earnings_yield` and size factor; the valorization step records
`market_cap_missing`), `data_quality(conn, date, versions) -> DataQuality` (T-065: the
`data_quality_issue` verdicts on the same latest filing, for the run's metric versions and the
current gate version — `quarantined` keys, `hard` issues, `negative_equity` assets; `apply()`
blanks quarantined values). **Point in time (T-106, T-107)**: a filing's period end is not when it
became known — production's 10-Ks were filed 48.7 days after it on average, up to 420 — and
neither is its filing date, which EDGAR gives to an after-close submission too. Every
fundamental reader keys on `available_at`, the first NYSE trading day after the filing date
(`kg_schema.trading_calendar`), stored on the filing and copied onto its metrics and
FUNDAMENTAL scores; a filing with no date has none and is never read. A run refuses to start
(`kg_schema.availability.require`) while rows predate the `m008` backfill. Both metric readers take the `MetricVersions` the run resolved
(`kg_schema.versions`, T-090) and read **only** those engine versions — they used to join
`fundamental_metrics` unfiltered and let row order pick among versions.

### `scores/normalize.py`

`cross_sectional_z(values, winsor=0.02)` — winsorized z-score against the cohort
(zeros if degenerate). `z_to_score(z)` → `clamp(50 + 10z, 0, 100)`.
`normalized_scores(raw)` composes both. `rank_pct(values, higher_is_better=True)` —
cross-sectional percentile rank in `[0,1]`, `None` in → `None` out.

### Composite weights — known effects (T-141)

`rank` blends FUNDAMENTAL, VALORIZATION and TECHNICAL at 1/3 each (the 1/N argument, DeMiguel, Garlappi &
Uppal 2009). Equal weights are equal influence only on a common scale: the three `normalized_score`s are each
`clamp(50 + 10z, 0, 100)` of a winsorized cross-sectional z (`normalized_scores`; FUNDAMENTAL over each asset's
latest public filing snapshot), and the evidence is in `docs/model_fixes.md` (T-141). Known effects:

- **Renormalization.** `_blended` divides by the weights of the components an asset has, so an asset missing
  one is the mean of the other two (1/2 each), never scored a third lower; `v_cycle_ranking_component`'s
  `effective_weight` for that asset is 0.5/0.5 and always sums to 1. Its blended score is then on the same
  50-centred scale but not built from the same inputs as a three-component name.
- **The clamp.** A component is clamped at 0 and 100 (|z| ≥ 5): the order of two names beyond the clamp is
  lost in that component. It does not bind on a small cohort (0% of the pilot's values); a large, heavy-tailed
  cross-section is where it could.
- **Dispersion is not exactly 10.** The z uses the winsorized mean and sd but the values themselves are not
  winsorized, so a component's sd is not exactly 10 (the pilot: FUNDAMENTAL 11.9, VALORIZATION 10.6,
  TECHNICAL 10.6). The component with more spread carries more of the blend's variance, and correlated or
  anti-correlated components shift that again: weights are equal, variance shares are not.
- **A degenerate cohort.** When almost every name shares one raw score (a component with data for few names),
  the winsorized sd is 0 and `cross_sectional_z` returns zeros: every name sits at 50 and that component
  discriminates nothing, while still carrying its 1/3 weight.
- **SEMANTIC** is not weighted; `rank` still loads stored SEMANTIC rows but `_blended` only iterates the
  configured weights, so they are ignored until the weights name it (Work item 4). From T-141 a new run's
  `v_weight_component` has three rows and its `components_json` has no SEMANTIC key; `v_cycle_ranking_component`
  is unchanged (SEMANTIC was always null, and the view skips nulls). Runs stored earlier keep their four rows.

### `scores/technical.py` — `SCORE_TYPE = "TECHNICAL"`, `VERSION = "technical-v2"`, `compute(observations, sectors) -> list[RawScore]`

Version 2 (T-070): `0.50·z(mom_12_1) − 0.30·z(realized_vol_90d) + 0.20·z(max_drawdown_90d)`, each z within the
asset's GICS sector (cross-section when the sector has fewer than 5 names with the signal), renormalized over
the signals present, no score under 2 of 3 signals. `components` carries each signal, its z and the size of the
group it was standardized against (`n_<signal>`: 5 or more is the sector, otherwise the cross-section). Full
definition in "Price vetoes and TECHNICAL v2 (T-070)" above. `mom_12_1(obs)` and `sector_z(values, sectors)`
are public and tested on their own.

### `scores/valorization.py` — `SCORE_TYPE = "VALORIZATION"`, `compute(metrics) -> list[RawScore]`

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
  (`metrics`, `price_obs`, `last_fundamental`, `data_quality`, `sectors` per asset), `Rule` protocol
  (`RULE_ID`, `SEVERITY`, `DESCRIPTION`, `PARAMS` property, `evaluate(ctx)`).
- `builtin.py` — `RULES`: `LEVERAGE_EXTREME` (`debt_to_equity > 3`, HARD),
  `NEGATIVE_FCF` (`free_cash_flow_margin < 0`, HARD; the margin is trailing-twelve-month on a 10-Q, T-133), `LIQUIDITY_DISTRESS`
  (`current_ratio < 1` and weak cash coverage, SOFT, not for Financials/Utilities; T-070), `PRICE_CRASH` (`max_drawdown_90d < −0.35`, SOFT),
  `BREAK_TREND_200` (SOFT), `VOLATILITY_SHOCK` and `CRASH_Z_SCORE` (HARD, temporal; T-070),
  `EARNINGS_MISSING` (a FUNDAMENTAL score *exists but has aged* past 400 days, SOFT — an asset
  with no score at all never reaches this rule at all, see `unscored` below, T-119),
  `DATA_QUALITY` (a HARD Ring-1 `DQ_*` gate fired on the latest filing — read from
  `RuleContext.data_quality`; the evidence names the gates, T-065).
- `__init__.py` — `seed_catalog(conn)` (inserts the missing rules; an existing row keeps its `enabled`
  flag and `created_at` but takes the code's current `description`/`severity`/`params_json`, T-070),
  `enabled_rules(conn)`, `hold_trading_days(rule)` (a temporal rule's hold; `None` for the others).
- The rule catalog and the per-run blend (`score_weights` + knobs in
  `cycle_run.params_json`) are read-projected by `kg_schema` as `v_rule_catalog`,
  `v_weight_scheme`, `v_weight_component` — no separate export step.

### `writers.py`

`write_scores` (TECH/VALOR → `score_snapshot`, upsert on the natural key),
`apply_normalized`, `write_vetoes` (T-125: applies this cycle's stint transitions — open,
extend, close, or leave an unevaluated asset's open stint untouched — self-undoing this same
cycle date's own prior transitions first, so a re-run is idempotent; never deletes a stint). With
`hold_days` (rule id -> sessions; `rules.hold_trading_days`, T-070) a temporal stint opens with `expires_on`, is
held past a gone condition until it, and is extended while the condition persists (see "Price vetoes" above);
`restore_expiries_from(conn, date_from)` undoes the extensions dated on or after `date_from`.
`hard_vetoed_as_of(conn, cutoff)` / `active_soft_vetoes` (re-exported from
`kg_schema.queries`, the one point-in-time predicate `cycle` and `quant` both read: a stint is
active at cutoff `C` iff `raised_on <= C AND (cleared_on IS NULL OR cleared_on > C)`).
`write_ranking`
(replaces `cycle_ranking` for the run), `sync_positions` (open new / close vanished
`portfolio_position` stints — history immutable; a re-weighted incumbent gets a new stint from
the cycle date and its old one closes there, so every weight stays on record — only a re-run on
the stint's own start date updates in place (T-104); refuses to end a stint opened after the
cycle date, even with `--allow-backdated`). The database itself (a `kg_schema` trigger) rejects
any stint with `valid_to < valid_from`.

### `replay.py` — `backfill`'s isolated position book (T-115)

`out_of_order_replay_reason` / `sync_replay_positions`: the same shape as
`writers.out_of_order_reason` / `writers.sync_positions`, against `portfolio_position_replay`
instead of the live `portfolio_position` — kept as their own literal-SQL functions (Code & Git
#10) rather than a table-name-parameterized version of the live ones. `reset_replay_range(conn,
date_from)` is `--force`'s implementation: drops every replay stint opened on or after
`date_from`, reopens every one it closed on or after `date_from`, and drops every `cycle_run`
row on or after `date_from` (cascading to their checkpoints/ranking) — with no upper bound,
since a stint past the caller's `--to` still shapes whether `date_from` redoes cleanly. Nothing
*before* `date_from` is touched. Positions are the only step this module (or any per-table
isolation) covers — see the CLI section above for why `--db` (a whole separate database) is
mandatory for everything else `backfill` writes.

### `repair.py` — `plan_undo(conn, cycle_run_id)` / `apply_undo(conn, plan)` (T-104)

Reverts a backdated `select` run's writes to the live book: voids the stints it opened at its
date (`valid_to = valid_from`, kept on record), reopens the newer stints it closed early, and
restores every still-open stint's weight to its own opening run's `cycle_ranking.target_weight`;
marks the run `reverted`, all in one transaction. Refused (`NotBackdated`) for a run that was not
dated before the rest of the book.

### `construction.py` — `build_book(candidates, *, n, scheme, max_name_weight, max_sector_weight, pins, exclude, exclude_sectors, only_sectors) -> BookResult`

One **pure, deterministic** function (T-135; the T-134 decisions, SPEC FR-007) that replaces
`target_weights` and its 8-round cap loop, which could return a book that broke a cap without saying
so (production `cycle_run` 1: 0.1167 against a 0.10 cap, Energy 0.35 against 0.30 —
`docs/model_fixes.md`, Work item 18). It touches no database; the orchestrator feeds it
`BookCandidate(asset_id, ticker, blended_score, sector, realized_vol_90d, veto)` (`veto` is
`none | SOFT | HARD`) and gets a frozen `BookResult`. Every input order gives the same book (ties in score
break by ticker).

**Who is held.** (1) Excluded tickers and sectors are dropped, or only `only_sectors` is kept (names
are trimmed and case-insensitive; a candidate with no sector is dropped by `only_sectors`).
(2) HARD-vetoed candidates are never held; a HARD-vetoed pin is **refused** with its reason, a
SOFT-vetoed pin is **held and flagged**, an unknown ticker is refused ("not among the candidates").
(3) Pins first, then the rest by blended score descending. (4) The **sector-aware fill**: a ranked
name whose sector is *full* is skipped for the next-ranked one; a sector is full when it already
holds `max(1, floor(sector cap × n_held))` names — 9 at N = 30, 3 at N = 10, 1 at N = 5 and N = 3 —
where `n_held = min(N, eligible names)` (eligible: not excluded, not HARD-vetoed, in an allowed
sector). Pins count toward their sector's fullness but are never skipped for it. If fullness leaves
the book short of `n_held`, the skipped names fill it by rank (**overflow**, listed in
`overflow_tickers`) and the sector cap relaxes (below). (5) **Shortfall**: with fewer eligible names
than N the book holds them all, `shortfall = N − n_held` is recorded, and the book is never padded;
with none eligible the book is empty and the whole N is the shortfall. Contradictory inputs
(a pinned ticker that is also excluded, a pin in an excluded sector or outside `only_sectors`, more
pins than N, an empty `only_sectors`, a cap outside (0, 1], an unknown scheme) raise
`BookInputError` (a `ValueError`; the CLI reports it without a traceback).

**`score_tilt` weights** (the default scheme: an equal-weight core with a bounded score tilt). The
band is `[0.5/n_held, name cap]` with the name cap **`1.5/n_held`** — no 0.10 floor: 0.05 at N = 30,
0.15 at N = 10, 0.50 at N = 3 (capped at 1.0). The **target** maps the held scores linearly onto
`[0.5/n_held, 1.5/n_held]` — the lowest held score to the floor, the highest to the ceiling, all
scores equal → `1/n_held` each — so it spans the held names' own score range whatever its spread: a
weight says where a name sits inside the held set, not how large the score gap is (measured in
`docs/model_fixes.md`). The **final weights are the exact Euclidean projection of that target onto
`{Σw = 1, band, every sector's sum ≤ sector cap}`** (`_project_book`): a common shift of the target,
clipped to the band (`_project_sum` solves the piecewise-linear shift exactly, no bisection), then
every sector over its cap is fixed at the cap and projected inside it and the rest re-projected,
repeating — at most one round per sector, finitely many steps, no tolerance to tune. Where no bound
binds the weights stay linear in the score. The sums and bounds hold within 1e-9.

**Relaxations — nothing is silent.** An explicit `max_name_weight` below `1/n_held` cannot sum to 1:
it is relaxed to `1/n_held` and recorded. An explicit cap above `1.5/n_held` wins (the band's upper
bound is the cap). The **sector cap** is 0.30 (or the explicit value); if the chosen sectors cannot
hold it under the name cap — `Σ min(c, k·cap) < 1`, or a sector's names cannot each take the band
floor (`k·0.5/n_held > c`) — it relaxes to the **smallest feasible value** (`_feasible_sector_cap`,
solved exactly: N = 3 with one name per sector → 1/3; one sector → 1.0; two sectors → 0.5) and the
relaxation is recorded. `BookResult` carries `weights` (rank order, pins first), `scheme`, `requested_n`,
`n_held`, `shortfall`, the **effective** `max_name_weight`/`max_sector_weight`, `relaxations`
(`cap`, `requested`, `effective`, `reason`), `refused_pins`, `flagged_pins` (`ticker`, `reason`) and
`overflow_tickers`.

**Legacy schemes** (`equal`, `score_proportional`, `inverse_vol`) stay selectable but are no longer
the default. Their raw weights are the target and go through the same exact projection with a band
floor of 0 and the name cap `max_name_weight`, else **0.10** (relaxed to `1/n_held`, and recorded,
when `n_held < 10`); `inverse_vol` gives a name with no usable volatility the mean inverse volatility
(the old code gave it weight 0). A run stored by `target_weights` is therefore not reproducible
bit-for-bit: its cap breaches are gone by construction.

### `orchestrator.py` — `run_selection` / `run_monitoring(settings, cycle_date, *, conn=None, fundamental_hook=None)`

`_run` executes the step list, each wrapped by `_do` which skips it if already
`done`:

```
universe → fundamental → technical → valorization → semantic_read →
normalize → sector → veto → rank → [positions]   (positions is SELECTION only)
```

- **metrics** (before any step) — `latest_metrics` with the Ring-1 quarantine applied
  (`data_quality().apply`): a quarantined metric reads as NULL in every score and rule; its
  market cap too. A negative-equity name keeps the worst leverage rank in VALORIZATION (C2).
  The manifest records `"quality": "dq-v2"`.
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
  SOFT veto. T-1 HARD-veto assets are marked `vetoed` (excluded from selection). An asset
  with no FUNDAMENTAL score at all (not merely a stale one) is marked `vetoed` with an
  `"UNSCORED"` `veto_rules` entry immediately, this same cycle — not through the T-1 lag, and
  not a SOFT veto/`veto` table row (T-119, PR #78 review); it still appears in `cycle_ranking`,
  just excluded from `positions`. More than `unscored_max_share` of the universe unscored —
  including 100% (PR #99 review) — refuses the whole run (`TooManyUnscored`) instead.
- **positions** (`select` and `backfill`) — build `BookCandidate`s from the ranked cohort — ticker,
  sector **name** (`data.active_universe` returns it), `realized_vol_90d`, and the veto status at the T-1
  cutoff read from the rank rows' own `veto_rules` (HARD-vetoed names are passed in, marked, so a HARD
  pin is refused with its reason; SOFT is marked; UNSCORED names are ineligible and left out) → `build_book`
  (the same call on the live and the REPLAY path) → the out-of-order guards → record the book in
  `params_json` (below) → `sync_positions` / `sync_replay_positions`; reflect selection back into
  `cycle_ranking`. The refused pins and every relaxation are logged (WARNING) and printed by
  `select`/`backfill` as a one-line `book:` summary (effective caps, `RELAXED …`, `SHORTFALL`,
  `REFUSED pin …`, flagged pins, overflow). `CycleReport.book` / `book_rows` hold the book.

**What `params_json` records (T-136).** Just before the write, `cycle_run.params_json` gets the
**effective** `max_name_weight` and `max_sector_weight` — under the keys `v_weight_scheme` already reads, so
it reports what was applied, no view change — plus `weight_scheme`, `top_n`, `n_held`, `shortfall`,
`relaxations`, the preferences (`pins`, `exclude`, `exclude_sectors`, `only_sectors`), `refused_pins` and
`flagged_pins` (with their reasons), `overflow_tickers`, and a `construction` block with what was
*requested* (the settings a resume must not change). A refused run (the out-of-order guard) records
nothing, so a retry may change them. A run recorded before T-136 has none of these keys; `scripts/verify_t138.py`
checks it against the legacy caps it did record.

**Resuming.** `check_construction` runs before `open_cycle`, like `check_manifest`: resuming a
`SELECTION` or `REPLAY` run of the same date with a different N, scheme, cap or preference raises
`ConstructionMismatch` (exit 1) — it never silently mixes two books. A run with nothing recorded (it never
reached `positions`, or predates T-136) and a MONITORING run are not constrained.
`check_score_weights` (T-141) is the same guard for the composite weights, and applies to every cycle type
including `MONITORING`: a resume with other `score_weights` than the run's first attempt recorded raises
`ScoreWeightsMismatch` (exit 1), because `params_json.score_weights` is written once and is what the weight views
report.

`CycleReport` records `steps_run` / `steps_skipped`, `selected`, `vetoed`, `unscored`/
`unscored_tickers` (T-119: universe members with no FUNDAMENTAL score at all this cycle,
distinct from `vetoed`'s HARD-veto count; printed by `select`/`monitor`/`backfill` whenever
nonzero, PR #99 review — restored from `cycle_ranking` after `rank` runs *or* is skipped on
resume, not a step-local variable).

### `fundamental_hook.py` — `make_hook(settings) -> FundamentalHook | None`

Wired slot for running the Strands `FundamentalAnalyst` inside a cycle. Currently
returns `None` (re-scoring a filing on demand needs the fundamental pipeline's
fetch+analyze path exposed as a reusable call). Until then the cycle consumes
whatever FUNDAMENTAL `score_snapshot` rows `fundamental_agent run` already wrote.

### `cli.py`

`select` / `monitor` / `backfill` / `undo-run --cycle-run N [--apply]` (T-104: prints what
reverting that backdated run would change; writes only with `--apply`; needs no model settings).
`--metrics-version` (T-090; a version
like `metrics-v1`, or `GROUP=VERSION` pairs) chooses which `fundamental_metrics` engine version
the cycle reads — default the newest stored per group. The resolved manifest and its tag are
recorded in `cycle_run.params_json` and printed. An unstored version exits 1 before a run is
created; a run of the same type and date built on other versions exits 1 rather than being mixed.
Abbreviated flags are not accepted (the parsers set `allow_abbrev=False`, so `--pin` cannot match a longer flag), and a falsy value
such as `--max-sector-weight 0` or `--top-n 0` reaches validation instead of being ignored.

**Construction flags** (`select` and `backfill`; `--top-n` is also on `select`/`monitor`). `--top-n N`
(default 30), `--weight-scheme {score_tilt,equal,score_proportional,inverse_vol}`,
`--max-name-weight` (default derived: `1.5/n_held`, no 0.10 floor; one below `1/n_held` is relaxed and
recorded), `--max-sector-weight` (default 0.30; relaxed to feasibility and recorded). **Preferences**,
comma-separated: `--pin TICKERS` (held first, count toward N, obey the band and the caps; a HARD-vetoed pin
is refused with its reason, a SOFT-vetoed pin is held and flagged), `--exclude TICKERS`,
`--exclude-sectors SECTORS`, `--only-sectors SECTORS` (the sector cap relaxes to feasibility). A book built
with preferences is **decision support, never the thesis book**: a **writing `select` refuses them** (a
clear message pointing at `--dry-run` and `backfill`; `PreferencesNeedDryRun`, raised in the orchestrator
before anything is written, so a library caller cannot bypass it), `select --dry-run` previews them, and
`backfill` accepts them (it writes only `portfolio_position_replay`). `--help` documents each flag.

**`select --dry-run` is strictly read-only (T-136).** It is not a cycle: `dry_run_book` first validates
what is wrong whatever the data is (an out-of-range value, a pin that is also excluded, an empty
`--only-sectors`: reported before anything is read), then builds
the book from the **ranking already stored for the date** — the date's SELECTION run's, else its
MONITORING run's — on a connection opened `mode=ro`, and prints ticker / sector / weight, the total, the
effective caps, the relaxations and the pin notes. It opens, finishes and modifies no `cycle_run`, writes
no checkpoint, position or ranking, runs no step and no hook, and with no stored ranking refuses:
*run `cycle monitor --analysis-date D` first* (exit 1). (The earlier `--dry-run` set `top_n = 0`, which
made the old `target_weights` return `{}` and `sync_positions` **close every open position**.) On a
`monitor` the flag is ignored.

## Gotchas

- `cycle/` may import both agents; only `cli.py` should call their `pipeline.run`
  directly. The score/step modules read shared tables via SQL, never by importing
  `pricing_agent`.
- TECH / VALOR use `cycle_date` as `event_time` → exactly one row per
  `(asset, score_type, day)`, so same-day re-runs upsert rather than duplicate.
- The `_run` function is deliberately one long linear sequence (`# noqa: C901`);
  its structure is the step list, and `cycle_checkpoint` makes it restartable.
