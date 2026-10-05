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
isolated the way `portfolio_position` is (above). Nothing *before* `--from` is touched.

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

## Configuration (`config.py`)

`CycleSettings.load()` needs `KG_FINANCIAL_DB`; `KG_UNIVERSE_DB` and LLM creds
optional. Knobs:
`universe` (`"SP500"`), `top_n` (30 — N, the number of names the book is sized for),
`score_weights` (FUND .4 / VALOR .3 / TECH .2 / SEM .1), `weight_scheme` (`score_tilt`
by default | `equal` | `score_proportional` | `inverse_vol`), `max_name_weight` (**`None`**:
derived — `1.5/n_held` for `score_tilt`, 0.10 for the legacy schemes; an explicit value wins, which
is why the default is `None`: a `0.10` default would silently override `1.5/N`),
`max_sector_weight` (.30), the **preferences** `pins`, `exclude`, `exclude_sectors` (tuples of
names, empty by default) and `only_sectors` (`None` = every sector), `soft_veto_penalty` (15 pts),
`unscored_max_share` (.05 — T-119: `rank` refuses outright past this share of the universe
with no FUNDAMENTAL score at all, up to and including 100%, PR #99 review).
`CycleSettings.construction()` is the subset that decides the book (N, scheme, caps,
preferences — normalized: trimmed, case-folded, sorted); a resume must not change it (below).

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
`available_at ≤ date`, T-106/T-107 — keyed `"group.name"`), `latest_price_observation`,
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

### `scores/technical.py` — `SCORE_TYPE = "TECHNICAL"`, `compute(observations) -> list[RawScore]`

Proposed definition (user to refine). Cross-sectionally ranks each sub-signal and
blends by weight: 12-1-ish `momentum_63d` (.35, ↑), `momentum_21d` (.15, ↑),
`realized_vol_90d` (.20, ↓), `atr_14/close` (.15, ↓), `max_drawdown_90d` (.15,
less-negative-is-better). `raw_value` = 100 × weighted mean of available
percentiles.

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
  (`metrics`, `price_obs`, `last_fundamental`, `data_quality` per asset), `Rule` protocol
  (`RULE_ID`, `SEVERITY`, `DESCRIPTION`, `PARAMS` property, `evaluate(ctx)`).
- `builtin.py` — `RULES`: `LEVERAGE_EXTREME` (`debt_to_equity > 3`, HARD),
  `NEGATIVE_FCF` (`free_cash_flow_margin < 0`, HARD; the margin is trailing-twelve-month on a 10-Q, T-133), `LIQUIDITY_DISTRESS`
  (`current_ratio < 1`, SOFT), `PRICE_CRASH` (`max_drawdown_90d < −0.35`, SOFT),
  `EARNINGS_MISSING` (a FUNDAMENTAL score *exists but has aged* past 400 days, SOFT — an asset
  with no score at all never reaches this rule at all, see `unscored` below, T-119),
  `DATA_QUALITY` (a HARD Ring-1 `DQ_*` gate fired on the latest filing — read from
  `RuleContext.data_quality`; the evidence names the gates, T-065).
- `__init__.py` — `seed_catalog(conn)` (`INSERT OR IGNORE` into `rule_catalog`,
  never overwrites), `enabled_rules(conn)`.
- The rule catalog and the per-run blend (`score_weights` + knobs in
  `cycle_run.params_json`) are read-projected by `kg_schema` as `v_rule_catalog`,
  `v_weight_scheme`, `v_weight_component` — no separate export step.

### `writers.py`

`write_scores` (TECH/VALOR → `score_snapshot`, upsert on the natural key),
`apply_normalized`, `write_vetoes` (T-125: applies this cycle's stint transitions — open,
extend, close, or leave an unevaluated asset's open stint untouched — self-undoing this same
cycle date's own prior transitions first, so a re-run is idempotent; never deletes a stint).
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
