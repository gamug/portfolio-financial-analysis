"""Additive schema shared by both agents and the future cycle / entity-resolution code.

Everything here is ``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS`` plus
nullable ``ALTER TABLE ADD COLUMN`` -- safe to run against the shared
``KG_FINANCIAL_DB`` at any time, concurrently with the other repos. Non-additive
changes (widening a UNIQUE key, renaming a table) live in :mod:`kg_schema.migrations`
and only run via ``python -m <agent> migrate``.

``assets`` / ``sectors`` are still owned elsewhere -- this module never touches them.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# New tables (additive -- ship anytime)
# ---------------------------------------------------------------------------

ADDITIVE_DDL = """
-- Membership history sidecar for the write-once `assets` identity table.
CREATE TABLE IF NOT EXISTS universe_membership (
    id          INTEGER PRIMARY KEY,
    asset_id    INTEGER NOT NULL REFERENCES assets(id),
    universe    TEXT NOT NULL,                 -- e.g. 'SP500'
    valid_from  TEXT NOT NULL,
    valid_to    TEXT,                          -- NULL = still a member
    detected_at TEXT NOT NULL,
    run_id      INTEGER,
    run_kind    TEXT,                          -- 'analysis' | 'pricing' | 'cycle'
    source      TEXT NOT NULL,                 -- 'wikipedia' | 'pricing-gateway'
    UNIQUE (asset_id, universe, valid_from)
);
CREATE INDEX IF NOT EXISTS ix_um_open
    ON universe_membership (universe, asset_id) WHERE valid_to IS NULL;

-- Point-in-time core-data coverage for a dated universe (written by the
-- `coverage` command). One row per (as_of, universe, symbol): does that member
-- have an identity row / FUNDAMENTAL score / metrics / prices / observations /
-- returns as of `as_of`. `covered` = every required check passed.
-- T-149 (APP-10b): a successor registrant's predecessor, so the filings the predecessor made before the
-- succession belong to the successor's asset. Seeded from `kg_schema/data/cik_successions.csv`
-- (`identity.seed_successions`), one row per (successor, predecessor), each with the filing that
-- establishes the succession and a quote from it. One hop: a chain of two is two rows.
CREATE TABLE IF NOT EXISTS cik_succession (
    successor_cik      TEXT NOT NULL,
    predecessor_cik    TEXT NOT NULL,
    effective_date     TEXT NOT NULL,
    evidence_form      TEXT NOT NULL,
    evidence_accession TEXT NOT NULL,
    evidence_quote     TEXT NOT NULL,
    UNIQUE (successor_cik, predecessor_cik)
);

CREATE TABLE IF NOT EXISTS universe_coverage (
    id               INTEGER PRIMARY KEY,
    as_of            TEXT NOT NULL,
    universe         TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    asset_id         INTEGER,
    in_assets        INTEGER NOT NULL,
    has_fundamental  INTEGER NOT NULL,
    has_metrics      INTEGER NOT NULL,
    has_pricing      INTEGER NOT NULL,
    has_observations INTEGER NOT NULL,
    has_returns      INTEGER NOT NULL,
    covered          INTEGER NOT NULL,
    missing_json     TEXT,
    checked_at       TEXT NOT NULL,
    run_id           INTEGER,
    UNIQUE (as_of, universe, symbol)
);
CREATE INDEX IF NOT EXISTS ix_uc_asof
    ON universe_coverage (as_of, universe) WHERE covered = 0;

-- Unified, multi-dimensional score store. `fundamental_snapshot` migrates in here
-- (see migrations.m004) and afterwards survives as a compatibility VIEW.
CREATE TABLE IF NOT EXISTS score_snapshot (
    id               INTEGER PRIMARY KEY,
    asset_id         INTEGER NOT NULL REFERENCES assets(id),
    score_type       TEXT NOT NULL CHECK (score_type IN
                        ('FUNDAMENTAL', 'VALORIZATION', 'TECHNICAL', 'SEMANTIC', 'SECTOR')),
    raw_value        REAL,
    normalized_score REAL,
    event_time       TEXT NOT NULL,            -- what the score is *about*
    computed_at      TEXT NOT NULL,            -- when the row was written
    model            TEXT,
    inputs_json      TEXT,
    run_id           INTEGER,
    run_kind         TEXT,
    filing_id        INTEGER REFERENCES sec_filings(id),   -- FUNDAMENTAL only
    rating           TEXT,                                  -- FUNDAMENTAL carry-over
    narrative        TEXT,
    strengths_json   TEXT,
    risks_json       TEXT,
    UNIQUE (asset_id, score_type, event_time)
);
CREATE INDEX IF NOT EXISTS ix_score_snapshot_type_time
    ON score_snapshot (score_type, event_time);

-- Ring-1 deterministic data-quality gates (T-040 / T-065). One row per gated metric of a
-- filing, per metrics engine version it was evaluated on and per gate version: append-only,
-- a re-run of the same gate version over the same metrics is a no-op. `quarantined = 1`
-- means consumers read that metric as NULL; a HARD row also triggers `cycle`'s
-- DATA_QUALITY veto; a SOFT row that is not quarantined is a non-blocking review item.
CREATE TABLE IF NOT EXISTS data_quality_issue (
    id                    INTEGER PRIMARY KEY,
    filing_id             INTEGER NOT NULL REFERENCES sec_filings(id) ON DELETE CASCADE,
    asset_id              INTEGER NOT NULL REFERENCES assets(id),
    metric_group          TEXT NOT NULL,
    metric_name           TEXT NOT NULL,
    metric_engine_version TEXT NOT NULL,       -- the fundamental_metrics version gated
    rule_id               TEXT NOT NULL,       -- 'DQ_MARGIN', ...
    severity              TEXT NOT NULL CHECK (severity IN ('HARD', 'SOFT')),
    quarantined           INTEGER NOT NULL,
    value                 REAL,                -- the stored metric value that was gated
    evidence_json         TEXT,
    gate_version          TEXT NOT NULL,       -- 'dq-v1'
    created_at            TEXT NOT NULL,
    run_id                INTEGER,
    UNIQUE (filing_id, metric_group, metric_name, metric_engine_version, rule_id, gate_version)
);
CREATE INDEX IF NOT EXISTS ix_dqi_asset ON data_quality_issue (asset_id);
CREATE INDEX IF NOT EXISTS ix_dqi_rule ON data_quality_issue (rule_id, severity);

CREATE TABLE IF NOT EXISTS rule_catalog (
    rule_id     TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    severity    TEXT NOT NULL,                 -- 'HARD' | 'SOFT'
    params_json TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

-- T-125: a veto is a stint (raised_on .. cleared_on), not a per-cycle-date event -- a
-- HARD veto raised on one cycle and never seen again must clear once the rule re-evaluates
-- the asset and finds it no longer breached, and a SOFT rule held across N cycles is one
-- open stint, not N separate hits. `raised_on`/`cleared_on`/`last_seen_on` are cycle dates
-- (what the veto is *about*); `detected_at`/`cleared_at` are wall-clock metadata only, never
-- read by the point-in-time predicate. At most one open stint per (asset_id, rule_id) --
-- a partial unique index, added separately by `kg_schema.ensure` once the columns exist
-- (`_ensure_veto_indexes`) -- never inline here: unlike a plain nullable `ADD COLUMN`, an
-- index naming these new column names would abort `CREATE TABLE IF NOT EXISTS`'s no-op
-- safety on a pre-T-125 database that still has the old, per-cycle-date shape and hasn't
-- run `migrate` yet.
CREATE TABLE IF NOT EXISTS veto (
    id            INTEGER PRIMARY KEY,
    asset_id      INTEGER NOT NULL REFERENCES assets(id),
    rule_id       TEXT NOT NULL REFERENCES rule_catalog(rule_id),
    severity      TEXT NOT NULL,
    raised_on     TEXT NOT NULL,           -- cycle date the stint opened
    cleared_on    TEXT,                    -- cycle date the stint closed; NULL = still open
    last_seen_on  TEXT NOT NULL,           -- cycle date the condition was last confirmed hit
    detected_at   TEXT NOT NULL,
    cleared_at    TEXT,
    evidence_json TEXT,
    run_id        INTEGER,
    -- T-070 (m011): a temporal veto (VOLATILITY_SHOCK, CRASH_Z_SCORE) cannot clear before this
    -- cycle date; NULL on every non-temporal veto. `expiry_history_json` is bookkeeping only (not
    -- in `v_veto`): the extensions that moved `expires_on`, so a same-date re-run and the replay
    -- reset can put it back exactly.
    expires_on    TEXT,
    expiry_history_json TEXT,
    UNIQUE (asset_id, rule_id, raised_on)
);

CREATE TABLE IF NOT EXISTS portfolio_position (
    id              INTEGER PRIMARY KEY,
    asset_id        INTEGER NOT NULL REFERENCES assets(id),
    valid_from      TEXT NOT NULL,
    valid_to        TEXT,
    weight          REAL,
    cost_basis      REAL,
    opened_by_cycle INTEGER,
    run_id          INTEGER,
    UNIQUE (asset_id, valid_from)
);
CREATE INDEX IF NOT EXISTS ix_pp_open ON portfolio_position (asset_id) WHERE valid_to IS NULL;
-- T-104: a stint never ends before it starts, whoever writes it (a backdated run once closed
-- WFC's 2026-09-22 stint at 2026-06-30). valid_to = valid_from is allowed: a stint held for no
-- day at all, which is how a reverted run's positions are voided without deleting history.
CREATE TRIGGER IF NOT EXISTS trg_pp_range_insert BEFORE INSERT ON portfolio_position
WHEN NEW.valid_to IS NOT NULL AND NEW.valid_to < NEW.valid_from
BEGIN SELECT RAISE(ABORT, 'portfolio_position: valid_to before valid_from'); END;
CREATE TRIGGER IF NOT EXISTS trg_pp_range_update
BEFORE UPDATE OF valid_from, valid_to ON portfolio_position
WHEN NEW.valid_to IS NOT NULL AND NEW.valid_to < NEW.valid_from
BEGIN SELECT RAISE(ABORT, 'portfolio_position: valid_to before valid_from'); END;

-- T-115: `cycle backfill`'s simulated book. Same shape and same T-104 stint-immutability
-- rules as `portfolio_position`, but a separate table so a historical replay never mutates
-- (or is refused for conflicting with) the one production book every other repo reads as
-- ground truth. `cycle_run.cycle_type = 'REPLAY'` is the run-log side of the same isolation.
CREATE TABLE IF NOT EXISTS portfolio_position_replay (
    id              INTEGER PRIMARY KEY,
    asset_id        INTEGER NOT NULL REFERENCES assets(id),
    valid_from      TEXT NOT NULL,
    valid_to        TEXT,
    weight          REAL,
    cost_basis      REAL,
    opened_by_cycle INTEGER,
    run_id          INTEGER,
    UNIQUE (asset_id, valid_from)
);
CREATE INDEX IF NOT EXISTS ix_ppr_open ON portfolio_position_replay (asset_id) WHERE valid_to IS NULL;
CREATE TRIGGER IF NOT EXISTS trg_ppr_range_insert BEFORE INSERT ON portfolio_position_replay
WHEN NEW.valid_to IS NOT NULL AND NEW.valid_to < NEW.valid_from
BEGIN SELECT RAISE(ABORT, 'portfolio_position_replay: valid_to before valid_from'); END;
CREATE TRIGGER IF NOT EXISTS trg_ppr_range_update
BEFORE UPDATE OF valid_from, valid_to ON portfolio_position_replay
WHEN NEW.valid_to IS NOT NULL AND NEW.valid_to < NEW.valid_from
BEGIN SELECT RAISE(ABORT, 'portfolio_position_replay: valid_to before valid_from'); END;

CREATE TABLE IF NOT EXISTS cycle_run (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_type   TEXT NOT NULL,                -- 'SELECTION'|'MONITORING'|'ENTITY_RESOLUTION'|'REPLAY'
    cycle_date   TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,
    params_json  TEXT,
    code_version TEXT,                         -- code tag that produced the run
    UNIQUE (cycle_type, cycle_date)
);

CREATE TABLE IF NOT EXISTS cycle_checkpoint (
    id           INTEGER PRIMARY KEY,
    cycle_run_id INTEGER NOT NULL REFERENCES cycle_run(id) ON DELETE CASCADE,
    step         TEXT NOT NULL,
    status       TEXT NOT NULL,                -- 'pending'|'running'|'done'|'failed'
    detail_json  TEXT,
    updated_at   TEXT NOT NULL,
    UNIQUE (cycle_run_id, step)
);

CREATE TABLE IF NOT EXISTS cycle_ranking (
    id             INTEGER PRIMARY KEY,
    cycle_run_id   INTEGER NOT NULL REFERENCES cycle_run(id) ON DELETE CASCADE,
    asset_id       INTEGER NOT NULL REFERENCES assets(id),
    rank           INTEGER NOT NULL,
    blended_score  REAL NOT NULL,
    components_json TEXT,
    vetoed         INTEGER NOT NULL DEFAULT 0,
    veto_rules_json TEXT,
    selected       INTEGER NOT NULL DEFAULT 0,
    target_weight  REAL,
    UNIQUE (cycle_run_id, asset_id)
);

-- Step 3: enriched per-(asset, day) price series. Raw OHLCV stays in `price_daily`.
CREATE TABLE IF NOT EXISTS price_observation (
    id               INTEGER PRIMARY KEY,
    asset_id         INTEGER NOT NULL REFERENCES assets(id),
    obs_date         TEXT NOT NULL,
    close            REAL,
    prev_close       REAL,
    log_return       REAL,
    true_range       REAL,
    atr_14           REAL,
    realized_vol_21d REAL,
    realized_vol_90d REAL,
    max_drawdown_90d REAL,
    momentum_21d     REAL,
    momentum_63d     REAL,
    momentum_252d    REAL,
    dollar_volume    REAL,
    source           TEXT,
    event_time       TEXT NOT NULL,
    computed_at      TEXT NOT NULL,
    engine_version   TEXT NOT NULL,
    run_id           INTEGER,
    run_kind         TEXT,
    -- priceobs-v2 (T-070, m011): see `REQUIRED_COLUMNS["price_observation"]`
    sma_200          REAL,
    ret_5d           REAL,
    vol_5d           REAL,
    mu_60d_base      REAL,
    vol_60d_base     REAL,
    UNIQUE (asset_id, obs_date, engine_version)
);
CREATE INDEX IF NOT EXISTS ix_price_observation_asset_date
    ON price_observation (asset_id, obs_date);

-- Step 4: narrative filing sections (MD&A, risk factors...). Numeric facts stay
-- in `financial_facts`; this is a separate extraction path.
CREATE TABLE IF NOT EXISTS sec_filing_section (
    id                INTEGER PRIMARY KEY,
    filing_id         INTEGER NOT NULL REFERENCES sec_filings(id) ON DELETE CASCADE,
    section_type      TEXT NOT NULL,           -- 'MD&A'|'RISK_FACTORS'|'BUSINESS'|'LEGAL_PROCEEDINGS'
    item_number       TEXT,
    heading           TEXT,
    ordinal           INTEGER NOT NULL,
    char_start        INTEGER,
    char_end          INTEGER,
    text              TEXT NOT NULL,
    text_sha256       TEXT NOT NULL,
    word_count        INTEGER,
    extraction_method TEXT NOT NULL,
    source_url        TEXT,
    event_time        TEXT NOT NULL,
    retrieved_at      TEXT NOT NULL,
    engine_version    TEXT NOT NULL,
    run_id            INTEGER,
    UNIQUE (filing_id, section_type, ordinal, engine_version)
);
CREATE INDEX IF NOT EXISTS ix_sfs_filing ON sec_filing_section (filing_id);

-- T-132: the filing's own cover-page share count (`dei:EntityCommonStockSharesOutstanding`, via
-- the gateway's `data["cover"]["shares_outstanding"]`, portfolio-data-mining T-043): a
-- point-in-time count of the shares actually outstanding on `as_of_date` (a date near the
-- filing's, never its period end). One row per class; `class_member = ''` is the filer's own
-- non-dimensional total, only present when it filed one -- nothing here is ever summed upstream.
-- Never `CommonStockSharesIssued` (that counts treasury stock) and never a weighted average.
CREATE TABLE IF NOT EXISTS filing_cover_shares (
    id           INTEGER PRIMARY KEY,
    filing_id    INTEGER NOT NULL REFERENCES sec_filings(id) ON DELETE CASCADE,
    class_member TEXT NOT NULL DEFAULT '',
    value        REAL NOT NULL CHECK (value > 0),
    as_of_date   TEXT NOT NULL,
    event_time   TEXT NOT NULL,                -- the filing's period end, as for its facts
    ingested_at  TEXT NOT NULL,
    run_id       INTEGER,
    UNIQUE (filing_id, class_member, as_of_date)
);
CREATE INDEX IF NOT EXISTS ix_fcs_filing ON filing_cover_shares (filing_id);

-- Step 7: candidate shared-executive edges from news PER co-occurrence.
CREATE TABLE IF NOT EXISTS shared_executive_edge (
    id              INTEGER PRIMARY KEY,
    asset_id_a      INTEGER NOT NULL REFERENCES assets(id),   -- asset_id_a < asset_id_b
    asset_id_b      INTEGER NOT NULL REFERENCES assets(id),
    person_name     TEXT NOT NULL,
    article_count_a INTEGER NOT NULL,
    article_count_b INTEGER NOT NULL,
    weight          REAL NOT NULL,
    first_seen      TEXT,
    last_seen       TEXT,
    method          TEXT NOT NULL,             -- 'news-per-cooccurrence-v1'
    evidence_json   TEXT,
    computed_at     TEXT NOT NULL,
    run_id          INTEGER,
    UNIQUE (asset_id_a, asset_id_b, person_name, method)
);
CREATE INDEX IF NOT EXISTS ix_shared_exec_a ON shared_executive_edge (asset_id_a);
CREATE INDEX IF NOT EXISTS ix_shared_exec_b ON shared_executive_edge (asset_id_b);

-- T-043: the same shape and grain as shared_executive_edge, for co-occurrences that are real
-- but not corporate executives (press, analysts, wire services). A separate table rather than
-- a flag, so every reader of shared_executive_edge keeps meaning executives only. Written by
-- entity_resolution once T-082 routes those pairs here.
CREATE TABLE IF NOT EXISTS media_cooccurrence (
    id              INTEGER PRIMARY KEY,
    asset_id_a      INTEGER NOT NULL REFERENCES assets(id),   -- asset_id_a < asset_id_b
    asset_id_b      INTEGER NOT NULL REFERENCES assets(id),
    person_name     TEXT NOT NULL,
    article_count_a INTEGER NOT NULL,
    article_count_b INTEGER NOT NULL,
    weight          REAL NOT NULL,
    first_seen      TEXT,
    last_seen       TEXT,
    method          TEXT NOT NULL,
    evidence_json   TEXT,
    computed_at     TEXT NOT NULL,
    run_id          INTEGER,
    UNIQUE (asset_id_a, asset_id_b, person_name, method)
);
CREATE INDEX IF NOT EXISTS ix_media_cooc_a ON media_cooccurrence (asset_id_a);
CREATE INDEX IF NOT EXISTS ix_media_cooc_b ON media_cooccurrence (asset_id_b);

-- Per-cycle GICS-sector roll-up of members' TECHNICAL score. The per-asset
-- deviation from this mean is written to `score_snapshot` as score_type 'SECTOR'.
CREATE TABLE IF NOT EXISTS sector_aggregate_snapshot (
    id              INTEGER PRIMARY KEY,
    sector_id       INTEGER NOT NULL REFERENCES sectors(id),
    cycle_date      TEXT NOT NULL,
    metric_type     TEXT NOT NULL DEFAULT 'ScoreTecnico',
    member_count    INTEGER NOT NULL,
    mean_raw        REAL,
    mean_normalized REAL,
    computed_at     TEXT NOT NULL,
    run_id          INTEGER,
    UNIQUE (sector_id, cycle_date, metric_type)
);
CREATE INDEX IF NOT EXISTS ix_sector_agg_date ON sector_aggregate_snapshot (cycle_date);

-- Corporate actions (dividends / splits). Feeds the quant/ total-return series and
-- fundamental_agent's valuation lane. `price_daily.close` is already split-adjusted,
-- so SPLIT rows are provenance only and are never re-applied.
CREATE TABLE IF NOT EXISTS corporate_action (
    id             INTEGER PRIMARY KEY,
    asset_id       INTEGER NOT NULL REFERENCES assets(id),
    action_type    TEXT NOT NULL CHECK (action_type IN ('DIVIDEND', 'SPLIT')),
    ex_date        TEXT NOT NULL,                 -- what the row is about (event_time)
    value          REAL NOT NULL,                 -- DIVIDEND: cash/share USD; SPLIT: ratio (2-for-1 -> 2.0)
    currency       TEXT NOT NULL DEFAULT 'USD',
    declared_date  TEXT,
    record_date    TEXT,
    pay_date       TEXT,
    frequency      TEXT,                          -- 'quarterly'|'annual'|'special'|NULL
    source         TEXT NOT NULL,                 -- 'pricing-gateway' (legacy: 'financial-facts-derived')
    engine_version TEXT NOT NULL,                 -- 'corpact-v1' (gateway; legacy: 'corpact-v0-approx', 'corpact-v1-derived')
    ingested_at    TEXT NOT NULL,
    UNIQUE (asset_id, action_type, ex_date, engine_version)
);
CREATE INDEX IF NOT EXISTS ix_corpact_asset_date ON corporate_action (asset_id, ex_date);

-- quant/ total-return daily series: split-adjusted close + cash dividends folded in.
-- A dedicated table, NOT `price_observation` rows under a new engine_version --
-- `v_price_observation` resolves the latest engine per (asset, day), so writing there
-- would silently move cycle's technical/veto path onto quant's rows.
CREATE TABLE IF NOT EXISTS quant_return_daily (
    id               INTEGER PRIMARY KEY,
    asset_id         INTEGER NOT NULL REFERENCES assets(id),
    obs_date         TEXT NOT NULL,               -- event_time
    close_split_adj  REAL NOT NULL,               -- copied from price_daily.close
    adj_close        REAL NOT NULL,               -- dividend-back-adjusted (total-return price)
    tr_index         REAL NOT NULL,               -- forward TR index, tr_index[0] = close_split_adj[0]
    cash_dividend    REAL NOT NULL DEFAULT 0.0,   -- per-share dividend with ex_date == obs_date
    split_factor     REAL NOT NULL DEFAULT 1.0,   -- provenance only; close is already split-adjusted
    price_log_return REAL,                        -- ln(C_t / C_{t-1})  (== price_observation.log_return)
    tr_log_return    REAL,                        -- ln((C_t + D_t) / C_{t-1})  <- the optimizer input
    source           TEXT NOT NULL,               -- 'quant-tr-v1'
    engine_version   TEXT NOT NULL,               -- 'qret-v2' (was 'qret-v1')
    computed_at      TEXT NOT NULL,
    UNIQUE (asset_id, obs_date, engine_version)
);
CREATE INDEX IF NOT EXISTS ix_qret_asset_date ON quant_return_daily (asset_id, obs_date);

-- Risk-free rate series for Sharpe / tangency. A single 'CONST' curve row per as-of
-- is acceptable for v1; a real T-bill curve loads via a CSV later.
CREATE TABLE IF NOT EXISTS risk_free_rate (
    id              INTEGER PRIMARY KEY,
    curve           TEXT NOT NULL,                -- 'CONST' | 'US3M' | 'US1Y'
    rate_date       TEXT NOT NULL,                -- event_time
    annualized_rate REAL NOT NULL,                -- decimal, e.g. 0.045
    source          TEXT NOT NULL,               -- 'constant-v1' | 'fred-DGS3MO' | 'manual-csv'
    engine_version  TEXT NOT NULL,               -- 'rf-v1'
    ingested_at     TEXT NOT NULL,
    UNIQUE (curve, rate_date, engine_version)
);

-- Benchmark index series to evaluate the optimized books against. quant/ synthesizes
-- an internal equal-/cap-weight one over the same gated universe; a real SPX/SPY_TR
-- series loads via a CSV later.
CREATE TABLE IF NOT EXISTS benchmark_series (
    id                 INTEGER PRIMARY KEY,
    benchmark          TEXT NOT NULL,             -- 'SP500_EW_INTERNAL' | 'SP500_CAPW_INTERNAL' | 'SPX' | 'SPY_TR'
    obs_date           TEXT NOT NULL,             -- event_time
    level              REAL,                      -- index / price level
    total_return_level REAL,                      -- TR index if known
    log_return         REAL,
    source             TEXT NOT NULL,             -- 'quant-internal-ew' | 'vendor-csv' | ...
    engine_version     TEXT NOT NULL,             -- 'bench-v1'
    ingested_at        TEXT NOT NULL,
    UNIQUE (benchmark, obs_date, engine_version)
);

-- quant/ Markowitz risk model. quant_run itself is quant-private; these three are
-- here (not in quant/db.py) because they carry read-contract views -- a view over a
-- table kg_schema does not create is invalid until quant runs, and a later
-- migration then trips on it.
CREATE TABLE IF NOT EXISTS quant_risk_model (
    id                   INTEGER PRIMARY KEY,
    quant_run_id         INTEGER,
    as_of                TEXT NOT NULL,
    model_version        TEXT NOT NULL,
    lookback_days        INTEGER NOT NULL,
    min_history_days     INTEGER NOT NULL,
    n_assets             INTEGER NOT NULL,
    cov_estimator        TEXT NOT NULL,
    cov_shrinkage        REAL,
    ret_estimator        TEXT NOT NULL,
    periods_per_year     INTEGER NOT NULL DEFAULT 252,
    panel_engine_version TEXT NOT NULL,
    panel_spec_json      TEXT NOT NULL,           -- {asset_ids, date_start, date_end, n_dates, sha256}
    rf_annual            REAL,
    computed_at          TEXT NOT NULL,
    params_json          TEXT,
    manifest_json        TEXT,                    -- the input versions this model was built on (T-090)
    UNIQUE (as_of, model_version)
);

CREATE TABLE IF NOT EXISTS quant_expected_return (
    model_id  INTEGER NOT NULL REFERENCES quant_risk_model(id) ON DELETE CASCADE,
    asset_id  INTEGER NOT NULL REFERENCES assets(id),
    mu_model  TEXT NOT NULL,                      -- 'hist_mean' | 'james_stein' | 'equilibrium' | 'carhart' (T-077)
    mu        REAL NOT NULL,                      -- annualized
    UNIQUE (model_id, asset_id, mu_model)
);

CREATE TABLE IF NOT EXISTS quant_covariance (
    model_id   INTEGER NOT NULL REFERENCES quant_risk_model(id) ON DELETE CASCADE,
    asset_id_i INTEGER NOT NULL,                  -- lower triangle only (i <= j)
    asset_id_j INTEGER NOT NULL,
    value      REAL NOT NULL,                     -- annualized covariance
    UNIQUE (model_id, asset_id_i, asset_id_j)
);
CREATE INDEX IF NOT EXISTS ix_quant_cov_model ON quant_covariance (model_id);

-- One optimized book per (as_of, objective). kind: min_var | tangency | target_vol
-- | frontier_k | equal_weight | cap_weight | live_book. target_param carries the
-- objective's scalar input (target_vol's vol target, ...) when it has one.
CREATE TABLE IF NOT EXISTS quant_portfolio (
    id              INTEGER PRIMARY KEY,
    quant_run_id    INTEGER,
    model_id        INTEGER REFERENCES quant_risk_model(id),
    as_of           TEXT NOT NULL,
    kind            TEXT NOT NULL,
    frontier_k      INTEGER,                      -- non-null only for kind='frontier_k'
    objective       TEXT NOT NULL,
    solver          TEXT NOT NULL,
    status          TEXT NOT NULL,
    expected_return REAL,
    expected_vol    REAL,
    sharpe          REAL,
    rf_annual       REAL,
    n_positions     INTEGER NOT NULL,
    turnover        REAL,                         -- vs the prior open book of the same kind
    target_param    REAL,
    engine_version  TEXT NOT NULL,                -- 'opt-v1+<manifest tag>' (T-090)
    computed_at     TEXT NOT NULL,
    params_json     TEXT,
    manifest_json   TEXT,                         -- the input versions this book was built on (T-090)
    UNIQUE (as_of, kind, frontier_k, engine_version)
);

-- Stint history mirroring portfolio_position, but keyed per book (many concurrent
-- books at one as_of), not per asset.
CREATE TABLE IF NOT EXISTS quant_position (
    id           INTEGER PRIMARY KEY,
    portfolio_id INTEGER NOT NULL REFERENCES quant_portfolio(id) ON DELETE CASCADE,
    asset_id     INTEGER NOT NULL REFERENCES assets(id),
    weight       REAL NOT NULL,
    valid_from   TEXT NOT NULL,
    valid_to     TEXT,
    UNIQUE (portfolio_id, asset_id, valid_from)
);
CREATE INDEX IF NOT EXISTS ix_quant_position_open
    ON quant_position (asset_id) WHERE valid_to IS NULL;

CREATE TABLE IF NOT EXISTS quant_frontier_point (
    id              INTEGER PRIMARY KEY,
    model_id        INTEGER NOT NULL REFERENCES quant_risk_model(id) ON DELETE CASCADE,
    k               INTEGER NOT NULL,
    target_return   REAL NOT NULL,
    expected_return REAL NOT NULL,
    expected_vol    REAL NOT NULL,
    sharpe          REAL,
    status          TEXT NOT NULL,
    weights_json    TEXT NOT NULL,                -- {asset_id: weight} sparse (> 1e-6)
    portfolio_id    INTEGER REFERENCES quant_portfolio(id),
    UNIQUE (model_id, k)
);

CREATE TABLE IF NOT EXISTS quant_benchmark_performance (
    id                INTEGER PRIMARY KEY,
    portfolio_id      INTEGER NOT NULL REFERENCES quant_portfolio(id) ON DELETE CASCADE,
    date              TEXT NOT NULL,              -- forward trading day
    realized_return   REAL NOT NULL,             -- daily simple TR of the frozen weights
    cumulative_return REAL NOT NULL,             -- since as_of
    benchmark         TEXT,
    benchmark_return  REAL,
    active_return     REAL,                      -- realized - benchmark
    engine_version    TEXT NOT NULL,             -- 'perf-v1'
    computed_at       TEXT NOT NULL,
    UNIQUE (portfolio_id, date, engine_version)
);

-- T-012: the orchestrator's own provenance (api/orchestrator.py). One row per orchestrated
-- run, one per step, so GET /run/{handle} reads recorded state that survives a restart and a
-- report can trace "was everything as of D rebuilt, and when". Not one of T-145's five run
-- tables (orchestrator_run has AUTOINCREMENT from creation), so it is a RUN_ID_EXCLUSIONS
-- target, not a TARGET_RUN_TABLES one.
CREATE TABLE IF NOT EXISTS orchestrator_run (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    handle        TEXT NOT NULL UNIQUE,        -- the uuid4 the run endpoints return
    kind          TEXT NOT NULL,               -- pipeline | pricing-agent | ... (T-011)
    as_of         TEXT NOT NULL,               -- the analysis date
    surface       TEXT NOT NULL,               -- 'http' | 'cli'
    settings_json TEXT,                        -- JSON object of the given settings, or NULL
    status        TEXT NOT NULL,               -- running | completed | failed
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    code_version  TEXT                         -- kg_schema.provenance.code_version(), as is
);

CREATE TABLE IF NOT EXISTS orchestrator_step (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    orchestrator_run_id INTEGER NOT NULL REFERENCES orchestrator_run(id),
    ordinal             INTEGER NOT NULL,      -- 1-based, the step's position in the run
    step                TEXT NOT NULL,         -- the T-011 step name
    argv_json           TEXT NOT NULL,         -- JSON array (no interpreter path)
    status              TEXT NOT NULL,         -- pending|running|completed|failed|skipped|not_run
    started_at          TEXT,
    finished_at         TEXT,
    exit_code           INTEGER,
    run_kind            TEXT,                  -- pricing|analysis|cycle|quant (NULL if none)
    run_id              INTEGER,               -- the step's own run-log id (T-011 captures it)
    reason              TEXT,
    output_tail         TEXT,                  -- JSON array of the last 40 lines
    UNIQUE (orchestrator_run_id, ordinal)
);
"""


# ---------------------------------------------------------------------------
# Nullable columns to add to pre-existing agent tables (additive)
#   {table: {column: "SQLITE TYPE"}}
# ---------------------------------------------------------------------------

REQUIRED_COLUMNS: dict[str, dict[str, str]] = {
    "financial_facts": {
        "event_time": "TEXT",
        "ingested_at": "TEXT",
        "filing_version": "TEXT",
        "run_id": "INTEGER",  # the analysis_run that wrote this row
        # T-118: the gateway's own `data["corrections"]` rule id (e.g. "T-118") when this
        # value was derived by the gateway rather than filed as-is; NULL for every filed fact.
        "correction_rule": "TEXT",
        # T-148 (m013), ID-12: edgartools' ``preferred_sign`` (+1 or -1) of the payload row. The
        # displayed value times it is the value in the sign it was filed in (ID-11); the stored
        # ``value`` stays the displayed one, as it always was. NULL on every row written before
        # T-148 (no backfill) and where the gateway sent none.
        "preferred_sign": "REAL",
    },
    "fundamental_metrics": {
        "event_time": "TEXT",
        "engine_version": "TEXT",
        "run_id": "INTEGER",
        "available_at": "TEXT",  # T-107: its filing's
    },
    "sec_filings": {
        "run_id": "INTEGER",
        # T-107: the first NYSE trading day after `filing_date` -- when the filing is usable
        # (`kg_schema.trading_calendar.available_from`). Copied onto the filing's metrics and
        # FUNDAMENTAL scores; guarded by `AVAILABILITY_TRIGGERS`.
        "available_at": "TEXT",
        # T-149: the CIK the filing was listed and fetched under -- the asset's own or a recorded
        # predecessor's (`cik_succession`); guarded by `IDENTITY_TRIGGERS`. NULL on older rows.
        "cik": "TEXT",
    },
    # T-041: the fundamental synthesis's structured forensic flags (Work item 8, T-074) --
    # a JSON object of four booleans (data_error_suspected, negative_equity_buyback,
    # value_destroyer_sub_wacc, severe_sbc_dilution). NULL until T-074 writes it.
    "score_snapshot": {
        "forensic_flags_json": "TEXT",
        "available_at": "TEXT",  # T-107: FUNDAMENTAL rows only -- the scored filing's
        # T-113: sha256 of the LLM interaction (or, on fallback, the failed attempt) that
        # produced this row -- FUNDAMENTAL only; NULL for every other score_type.
        "prompt_hash": "TEXT",
    },
    "price_window": {
        "event_time": "TEXT",
        "run_id": "INTEGER",
    },
    # T-070 (m011), priceobs-v2: the price signals the cycle's TECHNICAL v2 and three price vetoes
    # read. NULL until each window is full, and NULL on every priceobs-v1 row.
    "price_observation": {
        "sma_200": "REAL",  # mean of the last 200 closes
        "ret_5d": "REAL",  # ln(close_t / close_{t-5})
        "vol_5d": "REAL",  # sd of the last 5 daily log returns (daily, not annualized)
        "mu_60d_base": "REAL",  # mean of the 60 daily log returns ending 5 trading days earlier
        "vol_60d_base": "REAL",  # their sd
    },
    # T-070 (m011): a temporal veto's earliest clearing cycle date (see the `veto` DDL comment).
    "veto": {
        "expires_on": "TEXT",
        "expiry_history_json": "TEXT",
    },
    "price_daily": {
        "event_time": "TEXT",
        "ingested_at": "TEXT",
        "run_id": "INTEGER",
    },
    # Run-log tables (agent-owned; entries are skipped where the table is absent).
    # `analysis_date` provenance + the code tag that produced the run, so
    # portfolio-reports can trace an old run back to its inputs and its code.
    "analysis_run": {
        "as_of": "TEXT",
        "code_version": "TEXT",
        # T-113: how many of this run's FUNDAMENTAL scores fell back to the rule-based
        # synthesis (the LLM's JSON verdict never came back parseable).
        "fallback_units": "INTEGER NOT NULL DEFAULT 0",
    },
    "pricing_run": {
        "as_of": "TEXT",
        "code_version": "TEXT",
    },
    "quant_run": {
        "code_version": "TEXT",  # `as_of` already exists on this table
    },
    # T-090: which input versions a risk model / book was built on. Additive and nullable, so
    # a database created before it simply has NULLs there.
    "quant_risk_model": {
        "manifest_json": "TEXT",
    },
    "quant_portfolio": {
        "manifest_json": "TEXT",
    },
    "cycle_run": {
        "code_version": "TEXT",  # `cycle_date` is this table's as-of
    },
}


# ---------------------------------------------------------------------------
# T-145: Non-reusable IDs (AUTOINCREMENT) & run-reference map
# ---------------------------------------------------------------------------

TARGET_RUN_TABLES: tuple[str, ...] = (
    "cycle_run",
    "analysis_run",
    "pricing_run",
    "quant_run",
    "sec_filings",
)

# Explicit map of (referencing_table, referencing_column, run_kind_filter) -> target_table
# where target_table is one of TARGET_RUN_TABLES.
RUN_REFERENCE_MAP: dict[tuple[str, str, str | None], str] = {
    ("analysis_run_error", "run_id", None): "analysis_run",
    ("cycle_checkpoint", "cycle_run_id", None): "cycle_run",
    ("cycle_ranking", "cycle_run_id", None): "cycle_run",
    ("data_quality_issue", "filing_id", None): "sec_filings",
    (
        "data_quality_issue",
        "run_id",
        None,
    ): "analysis_run",  # nullable: the standalone gate command (cli.py:119) writes NULL
    ("filing_cover_shares", "filing_id", None): "sec_filings",
    ("filing_cover_shares", "run_id", None): "analysis_run",
    ("financial_facts", "filing_id", None): "sec_filings",
    ("financial_facts", "run_id", None): "analysis_run",
    ("fundamental_metrics", "filing_id", None): "sec_filings",
    ("fundamental_metrics", "run_id", None): "analysis_run",
    ("fundamental_snapshot", "filing_id", None): "sec_filings",
    ("fundamental_snapshot_legacy", "filing_id", None): "sec_filings",
    ("media_cooccurrence", "run_id", None): "cycle_run",  # no writer yet
    ("portfolio_position", "opened_by_cycle", None): "cycle_run",
    ("portfolio_position", "run_id", None): "cycle_run",
    ("portfolio_position_replay", "opened_by_cycle", None): "cycle_run",
    ("portfolio_position_replay", "run_id", None): "cycle_run",
    ("price_daily", "run_id", None): "pricing_run",
    ("price_observation", "run_id", "analysis"): "analysis_run",
    ("price_observation", "run_id", "pricing"): "pricing_run",
    ("price_observation", "run_id", "cycle"): "cycle_run",
    ("price_window", "run_id", None): "pricing_run",
    ("pricing_run_error", "run_id", None): "pricing_run",
    ("quant_portfolio", "quant_run_id", None): "quant_run",
    ("quant_risk_model", "quant_run_id", None): "quant_run",
    ("score_snapshot", "run_id", "analysis"): "analysis_run",
    ("score_snapshot", "run_id", "pricing"): "pricing_run",
    ("score_snapshot", "run_id", "cycle"): "cycle_run",
    ("score_snapshot", "filing_id", None): "sec_filings",
    ("sec_filing_section", "filing_id", None): "sec_filings",
    ("sec_filing_section", "run_id", None): "analysis_run",
    ("sec_filings", "run_id", None): "analysis_run",
    ("sector_aggregate_snapshot", "run_id", None): "cycle_run",
    ("shared_executive_edge", "run_id", None): "cycle_run",
    ("universe_membership", "run_id", "analysis"): "analysis_run",
    ("universe_membership", "run_id", "pricing"): "pricing_run",
    ("universe_membership", "run_id", "cycle"): "cycle_run",
    ("veto", "run_id", None): "cycle_run",
    # T-012: an orchestrator step's own run-log id, by the run_kind it recorded
    ("orchestrator_step", "run_id", "analysis"): "analysis_run",
    ("orchestrator_step", "run_id", "cycle"): "cycle_run",
    ("orchestrator_step", "run_id", "pricing"): "pricing_run",
    ("orchestrator_step", "run_id", "quant"): "quant_run",
}

# The set of cycle_type values allowed for each column pointing to cycle_run, derived from
# the code that writes the column.
ALLOWED_CYCLE_TYPES: dict[tuple[str, str, str | None], tuple[str, ...]] = {
    ("shared_executive_edge", "run_id", None): ("ENTITY_RESOLUTION",),
    ("media_cooccurrence", "run_id", None): ("ENTITY_RESOLUTION",),
    ("portfolio_position_replay", "run_id", None): ("REPLAY",),
    ("portfolio_position_replay", "opened_by_cycle", None): ("REPLAY",),
    ("portfolio_position", "run_id", None): ("SELECTION",),
    ("portfolio_position", "opened_by_cycle", None): ("SELECTION",),
    ("veto", "run_id", None): ("SELECTION", "MONITORING", "REPLAY"),
    ("cycle_checkpoint", "cycle_run_id", None): ("SELECTION", "MONITORING", "REPLAY"),
    ("cycle_ranking", "cycle_run_id", None): ("SELECTION", "MONITORING", "REPLAY"),
    ("sector_aggregate_snapshot", "run_id", None): ("SELECTION", "MONITORING", "REPLAY"),
    ("score_snapshot", "run_id", "cycle"): ("SELECTION", "MONITORING", "REPLAY"),
    ("price_observation", "run_id", "cycle"): ("SELECTION", "MONITORING", "REPLAY"),
    ("universe_membership", "run_id", "cycle"): ("SELECTION", "MONITORING", "REPLAY"),
    # T-012: a cycle-kind orchestrator step is cycle select (SELECTION), cycle monitor
    # (MONITORING) or entity_resolution (ENTITY_RESOLUTION).
    ("orchestrator_step", "run_id", "cycle"): ("SELECTION", "MONITORING", "ENTITY_RESOLUTION"),
}

# Matching columns in schema that do NOT hold an ID of one of the five target tables.
RUN_ID_EXCLUSIONS: dict[tuple[str, str], str] = {
    ("universe_coverage", "run_id"): "nullable, no writer today (Work item 2 decision)",
    ("orchestrator_step", "orchestrator_run_id"): (
        "references orchestrator_run (AUTOINCREMENT since creation, FK declared), not one of "
        "the five T-145 tables"
    ),
}

# Static literal SQL queries (Constitution Code & Git #10)
MAX_ID_QUERIES: dict[str, str] = {
    "cycle_run": "SELECT COALESCE(MAX(id), 0) FROM cycle_run",
    "analysis_run": "SELECT COALESCE(MAX(id), 0) FROM analysis_run",
    "pricing_run": "SELECT COALESCE(MAX(id), 0) FROM pricing_run",
    "quant_run": "SELECT COALESCE(MAX(id), 0) FROM quant_run",
    "sec_filings": "SELECT COALESCE(MAX(id), 0) FROM sec_filings",
}

MAX_REFERENCE_QUERIES: dict[tuple[str, str, str | None], str] = {
    (
        "analysis_run_error",
        "run_id",
        None,
    ): "SELECT COALESCE(MAX(run_id), 0) FROM analysis_run_error",
    (
        "cycle_checkpoint",
        "cycle_run_id",
        None,
    ): "SELECT COALESCE(MAX(cycle_run_id), 0) FROM cycle_checkpoint",
    (
        "cycle_ranking",
        "cycle_run_id",
        None,
    ): "SELECT COALESCE(MAX(cycle_run_id), 0) FROM cycle_ranking",
    (
        "data_quality_issue",
        "filing_id",
        None,
    ): "SELECT COALESCE(MAX(filing_id), 0) FROM data_quality_issue",
    (
        "data_quality_issue",
        "run_id",
        None,
    ): "SELECT COALESCE(MAX(run_id), 0) FROM data_quality_issue",
    (
        "filing_cover_shares",
        "filing_id",
        None,
    ): "SELECT COALESCE(MAX(filing_id), 0) FROM filing_cover_shares",
    (
        "filing_cover_shares",
        "run_id",
        None,
    ): "SELECT COALESCE(MAX(run_id), 0) FROM filing_cover_shares",
    (
        "financial_facts",
        "filing_id",
        None,
    ): "SELECT COALESCE(MAX(filing_id), 0) FROM financial_facts",
    ("financial_facts", "run_id", None): "SELECT COALESCE(MAX(run_id), 0) FROM financial_facts",
    (
        "fundamental_metrics",
        "filing_id",
        None,
    ): "SELECT COALESCE(MAX(filing_id), 0) FROM fundamental_metrics",
    (
        "fundamental_metrics",
        "run_id",
        None,
    ): "SELECT COALESCE(MAX(run_id), 0) FROM fundamental_metrics",
    (
        "fundamental_snapshot",
        "filing_id",
        None,
    ): "SELECT COALESCE(MAX(filing_id), 0) FROM fundamental_snapshot",
    ("fundamental_snapshot_legacy", "filing_id", None): (
        "SELECT COALESCE(MAX(filing_id), 0) FROM fundamental_snapshot_legacy"
    ),
    (
        "media_cooccurrence",
        "run_id",
        None,
    ): "SELECT COALESCE(MAX(run_id), 0) FROM media_cooccurrence",
    ("portfolio_position", "opened_by_cycle", None): (
        "SELECT COALESCE(MAX(opened_by_cycle), 0) FROM portfolio_position"
    ),
    (
        "portfolio_position",
        "run_id",
        None,
    ): "SELECT COALESCE(MAX(run_id), 0) FROM portfolio_position",
    ("portfolio_position_replay", "opened_by_cycle", None): (
        "SELECT COALESCE(MAX(opened_by_cycle), 0) FROM portfolio_position_replay"
    ),
    ("portfolio_position_replay", "run_id", None): (
        "SELECT COALESCE(MAX(run_id), 0) FROM portfolio_position_replay"
    ),
    ("price_daily", "run_id", None): "SELECT COALESCE(MAX(run_id), 0) FROM price_daily",
    ("price_observation", "run_id", "analysis"): (
        "SELECT COALESCE(MAX(run_id), 0) FROM price_observation WHERE run_kind = 'analysis'"
    ),
    ("price_observation", "run_id", "pricing"): (
        "SELECT COALESCE(MAX(run_id), 0) FROM price_observation WHERE run_kind = 'pricing'"
    ),
    ("price_observation", "run_id", "cycle"): (
        "SELECT COALESCE(MAX(run_id), 0) FROM price_observation WHERE run_kind = 'cycle'"
    ),
    ("price_window", "run_id", None): "SELECT COALESCE(MAX(run_id), 0) FROM price_window",
    ("pricing_run_error", "run_id", None): "SELECT COALESCE(MAX(run_id), 0) FROM pricing_run_error",
    (
        "quant_portfolio",
        "quant_run_id",
        None,
    ): "SELECT COALESCE(MAX(quant_run_id), 0) FROM quant_portfolio",
    (
        "quant_risk_model",
        "quant_run_id",
        None,
    ): "SELECT COALESCE(MAX(quant_run_id), 0) FROM quant_risk_model",
    ("score_snapshot", "run_id", "analysis"): (
        "SELECT COALESCE(MAX(run_id), 0) FROM score_snapshot WHERE run_kind = 'analysis'"
    ),
    ("score_snapshot", "run_id", "pricing"): (
        "SELECT COALESCE(MAX(run_id), 0) FROM score_snapshot WHERE run_kind = 'pricing'"
    ),
    ("score_snapshot", "run_id", "cycle"): (
        "SELECT COALESCE(MAX(run_id), 0) FROM score_snapshot WHERE run_kind = 'cycle'"
    ),
    ("score_snapshot", "filing_id", None): "SELECT COALESCE(MAX(filing_id), 0) FROM score_snapshot",
    (
        "sec_filing_section",
        "filing_id",
        None,
    ): "SELECT COALESCE(MAX(filing_id), 0) FROM sec_filing_section",
    (
        "sec_filing_section",
        "run_id",
        None,
    ): "SELECT COALESCE(MAX(run_id), 0) FROM sec_filing_section",
    ("sec_filings", "run_id", None): "SELECT COALESCE(MAX(run_id), 0) FROM sec_filings",
    ("sector_aggregate_snapshot", "run_id", None): (
        "SELECT COALESCE(MAX(run_id), 0) FROM sector_aggregate_snapshot"
    ),
    (
        "shared_executive_edge",
        "run_id",
        None,
    ): "SELECT COALESCE(MAX(run_id), 0) FROM shared_executive_edge",
    ("universe_membership", "run_id", "analysis"): (
        "SELECT COALESCE(MAX(run_id), 0) FROM universe_membership WHERE run_kind = 'analysis'"
    ),
    ("universe_membership", "run_id", "pricing"): (
        "SELECT COALESCE(MAX(run_id), 0) FROM universe_membership WHERE run_kind = 'pricing'"
    ),
    ("universe_membership", "run_id", "cycle"): (
        "SELECT COALESCE(MAX(run_id), 0) FROM universe_membership WHERE run_kind = 'cycle'"
    ),
    ("veto", "run_id", None): "SELECT COALESCE(MAX(run_id), 0) FROM veto",
    # T-012: the orchestrator's step rows
    ("orchestrator_step", "run_id", "analysis"): (
        "SELECT COALESCE(MAX(run_id), 0) FROM orchestrator_step WHERE run_kind = 'analysis'"
    ),
    ("orchestrator_step", "run_id", "cycle"): (
        "SELECT COALESCE(MAX(run_id), 0) FROM orchestrator_step WHERE run_kind = 'cycle'"
    ),
    ("orchestrator_step", "run_id", "pricing"): (
        "SELECT COALESCE(MAX(run_id), 0) FROM orchestrator_step WHERE run_kind = 'pricing'"
    ),
    ("orchestrator_step", "run_id", "quant"): (
        "SELECT COALESCE(MAX(run_id), 0) FROM orchestrator_step WHERE run_kind = 'quant'"
    ),
}

SQLITE_SEQUENCE_QUERIES: dict[str, str] = {
    "select": "SELECT seq FROM sqlite_sequence WHERE name = ?",
    "delete": "DELETE FROM sqlite_sequence WHERE name = ?",
    "insert": "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
}

UNRESOLVED_REFERENCE_QUERIES: dict[tuple[str, str, str | None], str] = {
    ("analysis_run_error", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM analysis_run_error r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id)"
    ),
    ("cycle_checkpoint", "cycle_run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.cycle_run_id), 0) FROM cycle_checkpoint r "
        "WHERE r.cycle_run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.cycle_run_id)"
    ),
    ("cycle_ranking", "cycle_run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.cycle_run_id), 0) FROM cycle_ranking r "
        "WHERE r.cycle_run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.cycle_run_id)"
    ),
    ("data_quality_issue", "filing_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.filing_id), 0) FROM data_quality_issue r "
        "WHERE r.filing_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM sec_filings t WHERE t.id = r.filing_id)"
    ),
    ("data_quality_issue", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM data_quality_issue r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id)"
    ),
    ("filing_cover_shares", "filing_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.filing_id), 0) FROM filing_cover_shares r "
        "WHERE r.filing_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM sec_filings t WHERE t.id = r.filing_id)"
    ),
    ("filing_cover_shares", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM filing_cover_shares r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id)"
    ),
    ("financial_facts", "filing_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.filing_id), 0) FROM financial_facts r "
        "WHERE r.filing_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM sec_filings t WHERE t.id = r.filing_id)"
    ),
    ("financial_facts", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM financial_facts r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id)"
    ),
    ("fundamental_metrics", "filing_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.filing_id), 0) FROM fundamental_metrics r "
        "WHERE r.filing_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM sec_filings t WHERE t.id = r.filing_id)"
    ),
    ("fundamental_metrics", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM fundamental_metrics r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id)"
    ),
    ("fundamental_snapshot", "filing_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.filing_id), 0) FROM fundamental_snapshot r "
        "WHERE r.filing_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM sec_filings t WHERE t.id = r.filing_id)"
    ),
    ("fundamental_snapshot_legacy", "filing_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.filing_id), 0) FROM fundamental_snapshot_legacy r "
        "WHERE r.filing_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM sec_filings t WHERE t.id = r.filing_id)"
    ),
    ("media_cooccurrence", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM media_cooccurrence r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.run_id)"
    ),
    ("portfolio_position", "opened_by_cycle", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.opened_by_cycle), 0) FROM portfolio_position r "
        "WHERE r.opened_by_cycle IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.opened_by_cycle)"
    ),
    ("portfolio_position", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM portfolio_position r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.run_id)"
    ),
    ("portfolio_position_replay", "opened_by_cycle", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.opened_by_cycle), 0) FROM portfolio_position_replay r "
        "WHERE r.opened_by_cycle IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.opened_by_cycle)"
    ),
    ("portfolio_position_replay", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM portfolio_position_replay r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.run_id)"
    ),
    ("price_daily", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM price_daily r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pricing_run t WHERE t.id = r.run_id)"
    ),
    ("price_observation", "run_id", "analysis"): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM price_observation r "
        "WHERE r.run_kind = 'analysis' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id)"
    ),
    ("price_observation", "run_id", "cycle"): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM price_observation r "
        "WHERE r.run_kind = 'cycle' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.run_id)"
    ),
    ("price_observation", "run_id", "pricing"): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM price_observation r "
        "WHERE r.run_kind = 'pricing' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pricing_run t WHERE t.id = r.run_id)"
    ),
    ("price_window", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM price_window r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pricing_run t WHERE t.id = r.run_id)"
    ),
    ("pricing_run_error", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM pricing_run_error r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pricing_run t WHERE t.id = r.run_id)"
    ),
    ("quant_portfolio", "quant_run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.quant_run_id), 0) FROM quant_portfolio r "
        "WHERE r.quant_run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM quant_run t WHERE t.id = r.quant_run_id)"
    ),
    ("quant_risk_model", "quant_run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.quant_run_id), 0) FROM quant_risk_model r "
        "WHERE r.quant_run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM quant_run t WHERE t.id = r.quant_run_id)"
    ),
    ("score_snapshot", "filing_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.filing_id), 0) FROM score_snapshot r "
        "WHERE r.filing_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM sec_filings t WHERE t.id = r.filing_id)"
    ),
    ("score_snapshot", "run_id", "analysis"): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM score_snapshot r "
        "WHERE r.run_kind = 'analysis' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id)"
    ),
    ("score_snapshot", "run_id", "cycle"): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM score_snapshot r "
        "WHERE r.run_kind = 'cycle' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.run_id)"
    ),
    ("score_snapshot", "run_id", "pricing"): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM score_snapshot r "
        "WHERE r.run_kind = 'pricing' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pricing_run t WHERE t.id = r.run_id)"
    ),
    ("sec_filing_section", "filing_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.filing_id), 0) FROM sec_filing_section r "
        "WHERE r.filing_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM sec_filings t WHERE t.id = r.filing_id)"
    ),
    ("sec_filing_section", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM sec_filing_section r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id)"
    ),
    ("sec_filings", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM sec_filings r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id)"
    ),
    ("sector_aggregate_snapshot", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM sector_aggregate_snapshot r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.run_id)"
    ),
    ("shared_executive_edge", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM shared_executive_edge r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.run_id)"
    ),
    ("universe_membership", "run_id", "analysis"): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM universe_membership r "
        "WHERE r.run_kind = 'analysis' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id)"
    ),
    ("universe_membership", "run_id", "cycle"): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM universe_membership r "
        "WHERE r.run_kind = 'cycle' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.run_id)"
    ),
    ("universe_membership", "run_id", "pricing"): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM universe_membership r "
        "WHERE r.run_kind = 'pricing' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pricing_run t WHERE t.id = r.run_id)"
    ),
    ("veto", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM veto r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.run_id)"
    ),
    ("orchestrator_step", "run_id", "analysis"): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM orchestrator_step r "
        "WHERE r.run_kind = 'analysis' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id)"
    ),
    ("orchestrator_step", "run_id", "cycle"): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM orchestrator_step r "
        "WHERE r.run_kind = 'cycle' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.run_id)"
    ),
    ("orchestrator_step", "run_id", "pricing"): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM orchestrator_step r "
        "WHERE r.run_kind = 'pricing' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pricing_run t WHERE t.id = r.run_id)"
    ),
    ("orchestrator_step", "run_id", "quant"): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM orchestrator_step r "
        "WHERE r.run_kind = 'quant' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM quant_run t WHERE t.id = r.run_id)"
    ),
}

UNRESOLVED_REFERENCE_SAMPLE_QUERIES: dict[tuple[str, str, str | None], str] = {
    ("analysis_run_error", "run_id", None): (
        "SELECT DISTINCT r.run_id FROM analysis_run_error r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("cycle_checkpoint", "cycle_run_id", None): (
        "SELECT DISTINCT r.cycle_run_id FROM cycle_checkpoint r "
        "WHERE r.cycle_run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.cycle_run_id) "
        "ORDER BY r.cycle_run_id LIMIT 5"
    ),
    ("cycle_ranking", "cycle_run_id", None): (
        "SELECT DISTINCT r.cycle_run_id FROM cycle_ranking r "
        "WHERE r.cycle_run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.cycle_run_id) "
        "ORDER BY r.cycle_run_id LIMIT 5"
    ),
    ("data_quality_issue", "filing_id", None): (
        "SELECT DISTINCT r.filing_id FROM data_quality_issue r "
        "WHERE r.filing_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM sec_filings t WHERE t.id = r.filing_id) "
        "ORDER BY r.filing_id LIMIT 5"
    ),
    ("data_quality_issue", "run_id", None): (
        "SELECT DISTINCT r.run_id FROM data_quality_issue r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("filing_cover_shares", "filing_id", None): (
        "SELECT DISTINCT r.filing_id FROM filing_cover_shares r "
        "WHERE r.filing_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM sec_filings t WHERE t.id = r.filing_id) "
        "ORDER BY r.filing_id LIMIT 5"
    ),
    ("filing_cover_shares", "run_id", None): (
        "SELECT DISTINCT r.run_id FROM filing_cover_shares r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("financial_facts", "filing_id", None): (
        "SELECT DISTINCT r.filing_id FROM financial_facts r "
        "WHERE r.filing_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM sec_filings t WHERE t.id = r.filing_id) "
        "ORDER BY r.filing_id LIMIT 5"
    ),
    ("financial_facts", "run_id", None): (
        "SELECT DISTINCT r.run_id FROM financial_facts r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("fundamental_metrics", "filing_id", None): (
        "SELECT DISTINCT r.filing_id FROM fundamental_metrics r "
        "WHERE r.filing_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM sec_filings t WHERE t.id = r.filing_id) "
        "ORDER BY r.filing_id LIMIT 5"
    ),
    ("fundamental_metrics", "run_id", None): (
        "SELECT DISTINCT r.run_id FROM fundamental_metrics r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("fundamental_snapshot", "filing_id", None): (
        "SELECT DISTINCT r.filing_id FROM fundamental_snapshot r "
        "WHERE r.filing_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM sec_filings t WHERE t.id = r.filing_id) "
        "ORDER BY r.filing_id LIMIT 5"
    ),
    ("fundamental_snapshot_legacy", "filing_id", None): (
        "SELECT DISTINCT r.filing_id FROM fundamental_snapshot_legacy r "
        "WHERE r.filing_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM sec_filings t WHERE t.id = r.filing_id) "
        "ORDER BY r.filing_id LIMIT 5"
    ),
    ("media_cooccurrence", "run_id", None): (
        "SELECT DISTINCT r.run_id FROM media_cooccurrence r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("portfolio_position", "opened_by_cycle", None): (
        "SELECT DISTINCT r.opened_by_cycle FROM portfolio_position r "
        "WHERE r.opened_by_cycle IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.opened_by_cycle) "
        "ORDER BY r.opened_by_cycle LIMIT 5"
    ),
    ("portfolio_position", "run_id", None): (
        "SELECT DISTINCT r.run_id FROM portfolio_position r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("portfolio_position_replay", "opened_by_cycle", None): (
        "SELECT DISTINCT r.opened_by_cycle FROM portfolio_position_replay r "
        "WHERE r.opened_by_cycle IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.opened_by_cycle) "
        "ORDER BY r.opened_by_cycle LIMIT 5"
    ),
    ("portfolio_position_replay", "run_id", None): (
        "SELECT DISTINCT r.run_id FROM portfolio_position_replay r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("price_daily", "run_id", None): (
        "SELECT DISTINCT r.run_id FROM price_daily r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pricing_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("price_observation", "run_id", "analysis"): (
        "SELECT DISTINCT r.run_id FROM price_observation r "
        "WHERE r.run_kind = 'analysis' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("price_observation", "run_id", "cycle"): (
        "SELECT DISTINCT r.run_id FROM price_observation r "
        "WHERE r.run_kind = 'cycle' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("price_observation", "run_id", "pricing"): (
        "SELECT DISTINCT r.run_id FROM price_observation r "
        "WHERE r.run_kind = 'pricing' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pricing_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("price_window", "run_id", None): (
        "SELECT DISTINCT r.run_id FROM price_window r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pricing_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("pricing_run_error", "run_id", None): (
        "SELECT DISTINCT r.run_id FROM pricing_run_error r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pricing_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("quant_portfolio", "quant_run_id", None): (
        "SELECT DISTINCT r.quant_run_id FROM quant_portfolio r "
        "WHERE r.quant_run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM quant_run t WHERE t.id = r.quant_run_id) "
        "ORDER BY r.quant_run_id LIMIT 5"
    ),
    ("quant_risk_model", "quant_run_id", None): (
        "SELECT DISTINCT r.quant_run_id FROM quant_risk_model r "
        "WHERE r.quant_run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM quant_run t WHERE t.id = r.quant_run_id) "
        "ORDER BY r.quant_run_id LIMIT 5"
    ),
    ("score_snapshot", "filing_id", None): (
        "SELECT DISTINCT r.filing_id FROM score_snapshot r "
        "WHERE r.filing_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM sec_filings t WHERE t.id = r.filing_id) "
        "ORDER BY r.filing_id LIMIT 5"
    ),
    ("score_snapshot", "run_id", "analysis"): (
        "SELECT DISTINCT r.run_id FROM score_snapshot r "
        "WHERE r.run_kind = 'analysis' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("score_snapshot", "run_id", "cycle"): (
        "SELECT DISTINCT r.run_id FROM score_snapshot r "
        "WHERE r.run_kind = 'cycle' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("score_snapshot", "run_id", "pricing"): (
        "SELECT DISTINCT r.run_id FROM score_snapshot r "
        "WHERE r.run_kind = 'pricing' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pricing_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("sec_filing_section", "filing_id", None): (
        "SELECT DISTINCT r.filing_id FROM sec_filing_section r "
        "WHERE r.filing_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM sec_filings t WHERE t.id = r.filing_id) "
        "ORDER BY r.filing_id LIMIT 5"
    ),
    ("sec_filing_section", "run_id", None): (
        "SELECT DISTINCT r.run_id FROM sec_filing_section r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("sec_filings", "run_id", None): (
        "SELECT DISTINCT r.run_id FROM sec_filings r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("sector_aggregate_snapshot", "run_id", None): (
        "SELECT DISTINCT r.run_id FROM sector_aggregate_snapshot r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("shared_executive_edge", "run_id", None): (
        "SELECT DISTINCT r.run_id FROM shared_executive_edge r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("universe_membership", "run_id", "analysis"): (
        "SELECT DISTINCT r.run_id FROM universe_membership r "
        "WHERE r.run_kind = 'analysis' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("universe_membership", "run_id", "cycle"): (
        "SELECT DISTINCT r.run_id FROM universe_membership r "
        "WHERE r.run_kind = 'cycle' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("universe_membership", "run_id", "pricing"): (
        "SELECT DISTINCT r.run_id FROM universe_membership r "
        "WHERE r.run_kind = 'pricing' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pricing_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("veto", "run_id", None): (
        "SELECT DISTINCT r.run_id FROM veto r "
        "WHERE r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("orchestrator_step", "run_id", "analysis"): (
        "SELECT DISTINCT r.run_id FROM orchestrator_step r "
        "WHERE r.run_kind = 'analysis' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM analysis_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("orchestrator_step", "run_id", "cycle"): (
        "SELECT DISTINCT r.run_id FROM orchestrator_step r "
        "WHERE r.run_kind = 'cycle' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cycle_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("orchestrator_step", "run_id", "pricing"): (
        "SELECT DISTINCT r.run_id FROM orchestrator_step r "
        "WHERE r.run_kind = 'pricing' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pricing_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("orchestrator_step", "run_id", "quant"): (
        "SELECT DISTINCT r.run_id FROM orchestrator_step r "
        "WHERE r.run_kind = 'quant' AND r.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM quant_run t WHERE t.id = r.run_id) "
        "ORDER BY r.run_id LIMIT 5"
    ),
}

WRONG_CYCLE_TYPE_QUERIES: dict[tuple[str, str, str | None], str] = {
    ("cycle_checkpoint", "cycle_run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.cycle_run_id), 0) FROM cycle_checkpoint r "
        "JOIN cycle_run t ON t.id = r.cycle_run_id "
        "WHERE t.cycle_type NOT IN ('SELECTION', 'MONITORING', 'REPLAY')"
    ),
    ("cycle_ranking", "cycle_run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.cycle_run_id), 0) FROM cycle_ranking r "
        "JOIN cycle_run t ON t.id = r.cycle_run_id "
        "WHERE t.cycle_type NOT IN ('SELECTION', 'MONITORING', 'REPLAY')"
    ),
    ("media_cooccurrence", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM media_cooccurrence r "
        "JOIN cycle_run t ON t.id = r.run_id "
        "WHERE t.cycle_type NOT IN ('ENTITY_RESOLUTION')"
    ),
    ("portfolio_position", "opened_by_cycle", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.opened_by_cycle), 0) FROM portfolio_position r "
        "JOIN cycle_run t ON t.id = r.opened_by_cycle "
        "WHERE t.cycle_type NOT IN ('SELECTION')"
    ),
    ("portfolio_position", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM portfolio_position r "
        "JOIN cycle_run t ON t.id = r.run_id "
        "WHERE t.cycle_type NOT IN ('SELECTION')"
    ),
    ("portfolio_position_replay", "opened_by_cycle", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.opened_by_cycle), 0) FROM portfolio_position_replay r "
        "JOIN cycle_run t ON t.id = r.opened_by_cycle "
        "WHERE t.cycle_type NOT IN ('REPLAY')"
    ),
    ("portfolio_position_replay", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM portfolio_position_replay r "
        "JOIN cycle_run t ON t.id = r.run_id "
        "WHERE t.cycle_type NOT IN ('REPLAY')"
    ),
    ("price_observation", "run_id", "cycle"): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM price_observation r "
        "JOIN cycle_run t ON t.id = r.run_id "
        "WHERE r.run_kind = 'cycle' AND t.cycle_type NOT IN ('SELECTION', 'MONITORING', 'REPLAY')"
    ),
    ("score_snapshot", "run_id", "cycle"): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM score_snapshot r "
        "JOIN cycle_run t ON t.id = r.run_id "
        "WHERE r.run_kind = 'cycle' AND t.cycle_type NOT IN ('SELECTION', 'MONITORING', 'REPLAY')"
    ),
    ("sector_aggregate_snapshot", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM sector_aggregate_snapshot r "
        "JOIN cycle_run t ON t.id = r.run_id "
        "WHERE t.cycle_type NOT IN ('SELECTION', 'MONITORING', 'REPLAY')"
    ),
    ("shared_executive_edge", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM shared_executive_edge r "
        "JOIN cycle_run t ON t.id = r.run_id "
        "WHERE t.cycle_type NOT IN ('ENTITY_RESOLUTION')"
    ),
    ("universe_membership", "run_id", "cycle"): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM universe_membership r "
        "JOIN cycle_run t ON t.id = r.run_id "
        "WHERE r.run_kind = 'cycle' AND t.cycle_type NOT IN ('SELECTION', 'MONITORING', 'REPLAY')"
    ),
    ("veto", "run_id", None): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM veto r "
        "JOIN cycle_run t ON t.id = r.run_id "
        "WHERE t.cycle_type NOT IN ('SELECTION', 'MONITORING', 'REPLAY')"
    ),
    ("orchestrator_step", "run_id", "cycle"): (
        "SELECT COUNT(*), COALESCE(MAX(r.run_id), 0) FROM orchestrator_step r "
        "JOIN cycle_run t ON t.id = r.run_id "
        "WHERE r.run_kind = 'cycle' AND t.cycle_type NOT IN ('SELECTION', 'MONITORING', 'ENTITY_RESOLUTION')"
    ),
}

WRONG_CYCLE_TYPE_SAMPLE_QUERIES: dict[tuple[str, str, str | None], str] = {
    ("cycle_checkpoint", "cycle_run_id", None): (
        "SELECT DISTINCT r.cycle_run_id, t.cycle_type FROM cycle_checkpoint r "
        "JOIN cycle_run t ON t.id = r.cycle_run_id "
        "WHERE t.cycle_type NOT IN ('SELECTION', 'MONITORING', 'REPLAY') "
        "ORDER BY r.cycle_run_id LIMIT 5"
    ),
    ("cycle_ranking", "cycle_run_id", None): (
        "SELECT DISTINCT r.cycle_run_id, t.cycle_type FROM cycle_ranking r "
        "JOIN cycle_run t ON t.id = r.cycle_run_id "
        "WHERE t.cycle_type NOT IN ('SELECTION', 'MONITORING', 'REPLAY') "
        "ORDER BY r.cycle_run_id LIMIT 5"
    ),
    ("media_cooccurrence", "run_id", None): (
        "SELECT DISTINCT r.run_id, t.cycle_type FROM media_cooccurrence r "
        "JOIN cycle_run t ON t.id = r.run_id "
        "WHERE t.cycle_type NOT IN ('ENTITY_RESOLUTION') "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("portfolio_position", "opened_by_cycle", None): (
        "SELECT DISTINCT r.opened_by_cycle, t.cycle_type FROM portfolio_position r "
        "JOIN cycle_run t ON t.id = r.opened_by_cycle "
        "WHERE t.cycle_type NOT IN ('SELECTION') "
        "ORDER BY r.opened_by_cycle LIMIT 5"
    ),
    ("portfolio_position", "run_id", None): (
        "SELECT DISTINCT r.run_id, t.cycle_type FROM portfolio_position r "
        "JOIN cycle_run t ON t.id = r.run_id "
        "WHERE t.cycle_type NOT IN ('SELECTION') "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("portfolio_position_replay", "opened_by_cycle", None): (
        "SELECT DISTINCT r.opened_by_cycle, t.cycle_type FROM portfolio_position_replay r "
        "JOIN cycle_run t ON t.id = r.opened_by_cycle "
        "WHERE t.cycle_type NOT IN ('REPLAY') "
        "ORDER BY r.opened_by_cycle LIMIT 5"
    ),
    ("portfolio_position_replay", "run_id", None): (
        "SELECT DISTINCT r.run_id, t.cycle_type FROM portfolio_position_replay r "
        "JOIN cycle_run t ON t.id = r.run_id "
        "WHERE t.cycle_type NOT IN ('REPLAY') "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("price_observation", "run_id", "cycle"): (
        "SELECT DISTINCT r.run_id, t.cycle_type FROM price_observation r "
        "JOIN cycle_run t ON t.id = r.run_id "
        "WHERE r.run_kind = 'cycle' AND t.cycle_type NOT IN ('SELECTION', 'MONITORING', 'REPLAY') "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("score_snapshot", "run_id", "cycle"): (
        "SELECT DISTINCT r.run_id, t.cycle_type FROM score_snapshot r "
        "JOIN cycle_run t ON t.id = r.run_id "
        "WHERE r.run_kind = 'cycle' AND t.cycle_type NOT IN ('SELECTION', 'MONITORING', 'REPLAY') "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("sector_aggregate_snapshot", "run_id", None): (
        "SELECT DISTINCT r.run_id, t.cycle_type FROM sector_aggregate_snapshot r "
        "JOIN cycle_run t ON t.id = r.run_id "
        "WHERE t.cycle_type NOT IN ('SELECTION', 'MONITORING', 'REPLAY') "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("shared_executive_edge", "run_id", None): (
        "SELECT DISTINCT r.run_id, t.cycle_type FROM shared_executive_edge r "
        "JOIN cycle_run t ON t.id = r.run_id "
        "WHERE t.cycle_type NOT IN ('ENTITY_RESOLUTION') "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("universe_membership", "run_id", "cycle"): (
        "SELECT DISTINCT r.run_id, t.cycle_type FROM universe_membership r "
        "JOIN cycle_run t ON t.id = r.run_id "
        "WHERE r.run_kind = 'cycle' AND t.cycle_type NOT IN ('SELECTION', 'MONITORING', 'REPLAY') "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("veto", "run_id", None): (
        "SELECT DISTINCT r.run_id, t.cycle_type FROM veto r "
        "JOIN cycle_run t ON t.id = r.run_id "
        "WHERE t.cycle_type NOT IN ('SELECTION', 'MONITORING', 'REPLAY') "
        "ORDER BY r.run_id LIMIT 5"
    ),
    ("orchestrator_step", "run_id", "cycle"): (
        "SELECT DISTINCT r.run_id, t.cycle_type FROM orchestrator_step r "
        "JOIN cycle_run t ON t.id = r.run_id "
        "WHERE r.run_kind = 'cycle' AND t.cycle_type NOT IN ('SELECTION', 'MONITORING', 'ENTITY_RESOLUTION') "
        "ORDER BY r.run_id LIMIT 5"
    ),
}

UNMAPPED_RUN_KIND_QUERIES: dict[str, str] = {
    "score_snapshot": (
        "SELECT COUNT(*), COALESCE(MAX(run_id), 0) FROM score_snapshot "
        "WHERE run_id IS NOT NULL AND (run_kind IS NULL OR run_kind NOT IN ('analysis', 'cycle', 'pricing'))"
    ),
    "price_observation": (
        "SELECT COUNT(*), COALESCE(MAX(run_id), 0) FROM price_observation "
        "WHERE run_id IS NOT NULL AND (run_kind IS NULL OR run_kind NOT IN ('analysis', 'cycle', 'pricing'))"
    ),
    "universe_membership": (
        "SELECT COUNT(*), COALESCE(MAX(run_id), 0) FROM universe_membership "
        "WHERE run_id IS NOT NULL AND (run_kind IS NULL OR run_kind NOT IN ('analysis', 'cycle', 'pricing'))"
    ),
    # T-012: an orchestrator step's run_kind is a literal from the step table, so a stored row
    # with a run_id and a NULL/unexpected kind is invisible to every other check.
    "orchestrator_step": (
        "SELECT COUNT(*), COALESCE(MAX(run_id), 0) FROM orchestrator_step "
        "WHERE run_id IS NOT NULL AND (run_kind IS NULL OR run_kind NOT IN ('pricing', 'analysis', 'cycle', 'quant'))"
    ),
}

UNMAPPED_RUN_KIND_SAMPLE_QUERIES: dict[str, str] = {
    "score_snapshot": (
        "SELECT DISTINCT run_id, COALESCE(run_kind, 'NULL') FROM score_snapshot "
        "WHERE run_id IS NOT NULL AND (run_kind IS NULL OR run_kind NOT IN ('analysis', 'cycle', 'pricing')) "
        "ORDER BY run_id LIMIT 5"
    ),
    "price_observation": (
        "SELECT DISTINCT run_id, COALESCE(run_kind, 'NULL') FROM price_observation "
        "WHERE run_id IS NOT NULL AND (run_kind IS NULL OR run_kind NOT IN ('analysis', 'cycle', 'pricing')) "
        "ORDER BY run_id LIMIT 5"
    ),
    "universe_membership": (
        "SELECT DISTINCT run_id, COALESCE(run_kind, 'NULL') FROM universe_membership "
        "WHERE run_id IS NOT NULL AND (run_kind IS NULL OR run_kind NOT IN ('analysis', 'cycle', 'pricing')) "
        "ORDER BY run_id LIMIT 5"
    ),
    "orchestrator_step": (
        "SELECT DISTINCT run_id, COALESCE(run_kind, 'NULL') FROM orchestrator_step "
        "WHERE run_id IS NOT NULL AND (run_kind IS NULL OR run_kind NOT IN ('pricing', 'analysis', 'cycle', 'quant')) "
        "ORDER BY run_id LIMIT 5"
    ),
}

INVALID_SEC_FILINGS_ACCESSIONS_QUERY: str = """
SELECT id, asset_id, form, fiscal_year, fiscal_period, filing_date, accession_number,
       CASE WHEN accession_number IS NULL THEN 'null_accession'
            WHEN accession_number NOT GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]'
            THEN 'invalid_format'
            ELSE 'duplicate_per_asset' END AS issue
FROM sec_filings
WHERE accession_number IS NULL
   OR accession_number NOT GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]'
   OR (asset_id, accession_number) IN (
       SELECT asset_id, accession_number
       FROM sec_filings
       WHERE accession_number IS NOT NULL
       GROUP BY asset_id, accession_number
       HAVING COUNT(*) > 1
   )
ORDER BY asset_id, id
"""

M014_REBUILD_COLUMNS: dict[str, tuple[str, ...]] = {
    "cycle_run": (
        "id",
        "cycle_type",
        "cycle_date",
        "started_at",
        "finished_at",
        "status",
        "params_json",
        "code_version",
    ),
    "analysis_run": (
        "id",
        "started_at",
        "finished_at",
        "universe_size",
        "planned_units",
        "completed_units",
        "skipped_units",
        "failed_units",
        "params_json",
        "status",
        "as_of",
        "code_version",
        "fallback_units",
    ),
    "pricing_run": (
        "id",
        "started_at",
        "finished_at",
        "universe_size",
        "planned_units",
        "completed_units",
        "skipped_units",
        "failed_units",
        "params_json",
        "status",
        "as_of",
        "code_version",
    ),
    "quant_run": (
        "id",
        "command",
        "as_of",
        "started_at",
        "finished_at",
        "status",
        "engine_version",
        "params_json",
        "error",
        "code_version",
    ),
    "sec_filings": (
        "id",
        "asset_id",
        "form",
        "fiscal_year",
        "fiscal_period",
        "filing_date",
        "accession_number",
        "period_end",
        "retrieved_at",
        "run_id",
        "available_at",
        "cik",
    ),
}

M014_TABLE_REBUILD_STATEMENTS: dict[str, tuple[str, ...]] = {
    "cycle_run": (
        """CREATE TABLE cycle_run__new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_type   TEXT NOT NULL,
    cycle_date   TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,
    params_json  TEXT,
    code_version TEXT,
    UNIQUE (cycle_type, cycle_date)
)""",
        """INSERT INTO cycle_run__new
    (id, cycle_type, cycle_date, started_at, finished_at, status, params_json, code_version)
SELECT id, cycle_type, cycle_date, started_at, finished_at, status, params_json, code_version
FROM cycle_run""",
        "DROP TABLE cycle_run",
        "ALTER TABLE cycle_run__new RENAME TO cycle_run",
    ),
    "analysis_run": (
        """CREATE TABLE analysis_run__new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    universe_size   INTEGER,
    planned_units   INTEGER,
    completed_units INTEGER DEFAULT 0,
    skipped_units   INTEGER DEFAULT 0,
    failed_units    INTEGER DEFAULT 0,
    params_json     TEXT,
    status          TEXT NOT NULL,
    as_of           TEXT,
    code_version    TEXT,
    fallback_units  INTEGER NOT NULL DEFAULT 0
)""",
        """INSERT INTO analysis_run__new
    (id, started_at, finished_at, universe_size, planned_units, completed_units,
     skipped_units, failed_units, params_json, status, as_of, code_version, fallback_units)
SELECT id, started_at, finished_at, universe_size, planned_units, completed_units,
       skipped_units, failed_units, params_json, status, as_of, code_version, fallback_units
FROM analysis_run""",
        "DROP TABLE analysis_run",
        "ALTER TABLE analysis_run__new RENAME TO analysis_run",
    ),
    "pricing_run": (
        """CREATE TABLE pricing_run__new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    universe_size   INTEGER,
    planned_units   INTEGER,
    completed_units INTEGER DEFAULT 0,
    skipped_units   INTEGER DEFAULT 0,
    failed_units    INTEGER DEFAULT 0,
    params_json     TEXT,
    status          TEXT NOT NULL,
    as_of           TEXT,
    code_version    TEXT
)""",
        """INSERT INTO pricing_run__new
    (id, started_at, finished_at, universe_size, planned_units, completed_units,
     skipped_units, failed_units, params_json, status, as_of, code_version)
SELECT id, started_at, finished_at, universe_size, planned_units, completed_units,
       skipped_units, failed_units, params_json, status, as_of, code_version
FROM pricing_run""",
        "DROP TABLE pricing_run",
        "ALTER TABLE pricing_run__new RENAME TO pricing_run",
    ),
    "quant_run": (
        """CREATE TABLE quant_run__new (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    command        TEXT NOT NULL,
    as_of          TEXT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    params_json    TEXT,
    error          TEXT,
    code_version   TEXT
)""",
        """INSERT INTO quant_run__new
    (id, command, as_of, started_at, finished_at, status, engine_version, params_json, error, code_version)
SELECT id, command, as_of, started_at, finished_at, status, engine_version, params_json, error, code_version
FROM quant_run""",
        "DROP TABLE quant_run",
        "ALTER TABLE quant_run__new RENAME TO quant_run",
    ),
    "sec_filings": (
        """CREATE TABLE sec_filings__new (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id         INTEGER NOT NULL REFERENCES assets(id),
    form             TEXT NOT NULL,
    fiscal_year      INTEGER NOT NULL,
    fiscal_period    TEXT NOT NULL,
    filing_date      TEXT,
    accession_number TEXT,
    period_end       TEXT,
    retrieved_at     TEXT NOT NULL,
    run_id           INTEGER,
    available_at     TEXT,
    cik              TEXT,
    UNIQUE (asset_id, form, fiscal_period)
)""",
        """INSERT INTO sec_filings__new
    (id, asset_id, form, fiscal_year, fiscal_period, filing_date, accession_number,
     period_end, retrieved_at, run_id, available_at, cik)
SELECT id, asset_id, form, fiscal_year, fiscal_period, filing_date, accession_number,
       period_end, retrieved_at, run_id, available_at, cik
FROM sec_filings""",
        "DROP TABLE sec_filings",
        "ALTER TABLE sec_filings__new RENAME TO sec_filings",
        """CREATE TRIGGER IF NOT EXISTS trg_sf_accession_insert BEFORE INSERT ON sec_filings
WHEN NEW.accession_number IS NOT NULL AND EXISTS (
    SELECT 1 FROM sec_filings o
    WHERE o.asset_id = NEW.asset_id AND o.accession_number = NEW.accession_number
      AND NOT (o.form = NEW.form AND o.fiscal_period = NEW.fiscal_period)
)
BEGIN SELECT RAISE(ABORT, 'sec_filings: accession_number already used by another filing of this asset'); END;""",
        """CREATE TRIGGER IF NOT EXISTS trg_sf_accession_update
BEFORE UPDATE OF asset_id, accession_number ON sec_filings
WHEN NEW.accession_number IS NOT NULL AND EXISTS (
    SELECT 1 FROM sec_filings o
    WHERE o.asset_id = NEW.asset_id AND o.accession_number = NEW.accession_number
      AND o.id <> NEW.id
)
BEGIN SELECT RAISE(ABORT, 'sec_filings: accession_number already used by another filing of this asset'); END;""",
    ),
}


# ---------------------------------------------------------------------------
# T-107: publication-time guards. Created by  (and migration m008) once
# ,  and  all exist with .
# ---------------------------------------------------------------------------

AVAILABILITY_TABLES = ("sec_filings", "fundamental_metrics", "score_snapshot")

AVAILABILITY_TRIGGERS = """
-- A dated filing is usable from a trading day 1-7 days after its filing date (the longest
-- NYSE gap since 2000 is 9/11's); an undated one never is. Which day exactly -- weekends and
-- holidays skipped -- is `kg_schema.trading_calendar.available_from`'s, which every writer uses.
CREATE TRIGGER IF NOT EXISTS trg_sf_available_insert BEFORE INSERT ON sec_filings
WHEN (NEW.filing_date IS NULL AND NEW.available_at IS NOT NULL)
  OR (NEW.filing_date IS NOT NULL AND (NEW.available_at IS NULL
      OR NEW.available_at <= NEW.filing_date
      OR NEW.available_at > date(NEW.filing_date, '+7 days')))
BEGIN SELECT RAISE(ABORT, 'sec_filings: available_at must be the trading day after filing_date'); END;
CREATE TRIGGER IF NOT EXISTS trg_sf_available_update
BEFORE UPDATE OF filing_date, available_at ON sec_filings
WHEN (NEW.filing_date IS NULL AND NEW.available_at IS NOT NULL)
  OR (NEW.filing_date IS NOT NULL AND (NEW.available_at IS NULL
      OR NEW.available_at <= NEW.filing_date
      OR NEW.available_at > date(NEW.filing_date, '+7 days')))
BEGIN SELECT RAISE(ABORT, 'sec_filings: available_at must be the trading day after filing_date'); END;
-- A re-dated filing carries its metrics and FUNDAMENTAL scores along.
CREATE TRIGGER IF NOT EXISTS trg_sf_available_cascade
AFTER UPDATE OF available_at ON sec_filings
WHEN NEW.available_at IS NOT OLD.available_at
BEGIN
    UPDATE fundamental_metrics SET available_at = NEW.available_at WHERE filing_id = NEW.id;
    UPDATE score_snapshot SET available_at = NEW.available_at
    WHERE filing_id = NEW.id AND score_type = 'FUNDAMENTAL';
END;
-- Every metric and FUNDAMENTAL score carries its filing's non-null `available_at`: a row of
-- an undated filing, or with no filing, is refused rather than silently unreadable.
CREATE TRIGGER IF NOT EXISTS trg_fm_available_insert BEFORE INSERT ON fundamental_metrics
WHEN NEW.available_at IS NULL OR NEW.available_at IS NOT
    (SELECT f.available_at FROM sec_filings f WHERE f.id = NEW.filing_id)
BEGIN SELECT RAISE(ABORT, 'fundamental_metrics: available_at must be its filing''s'); END;
CREATE TRIGGER IF NOT EXISTS trg_fm_available_update
BEFORE UPDATE OF filing_id, available_at ON fundamental_metrics
WHEN NEW.available_at IS NULL OR NEW.available_at IS NOT
    (SELECT f.available_at FROM sec_filings f WHERE f.id = NEW.filing_id)
BEGIN SELECT RAISE(ABORT, 'fundamental_metrics: available_at must be its filing''s'); END;
CREATE TRIGGER IF NOT EXISTS trg_ss_available_insert BEFORE INSERT ON score_snapshot
WHEN NEW.score_type = 'FUNDAMENTAL' AND (NEW.available_at IS NULL OR NEW.available_at IS NOT
    (SELECT f.available_at FROM sec_filings f WHERE f.id = NEW.filing_id))
BEGIN SELECT RAISE(ABORT, 'score_snapshot: a FUNDAMENTAL row''s available_at must be its filing''s'); END;
CREATE TRIGGER IF NOT EXISTS trg_ss_available_update
BEFORE UPDATE OF score_type, filing_id, available_at ON score_snapshot
WHEN NEW.score_type = 'FUNDAMENTAL' AND (NEW.available_at IS NULL OR NEW.available_at IS NOT
    (SELECT f.available_at FROM sec_filings f WHERE f.id = NEW.filing_id))
BEGIN SELECT RAISE(ABORT, 'score_snapshot: a FUNDAMENTAL row''s available_at must be its filing''s'); END;
"""


# ---------------------------------------------------------------------------
# T-149: the filing-identity invariant. Created by `kg_schema.ensure` (and migration m012) once
# `assets` (with `cik`) and `sec_filings` exist.
# ---------------------------------------------------------------------------

IDENTITY_TABLES = ("assets", "sec_filings", "cik_succession")

IDENTITY_TRIGGERS = """
-- A filing is fetched by its asset's CIK (APP-10a), so an asset with filings has one.
CREATE TRIGGER IF NOT EXISTS trg_sf_cik_insert BEFORE INSERT ON sec_filings
WHEN COALESCE((SELECT a.cik FROM assets a WHERE a.id = NEW.asset_id), '') = ''
BEGIN SELECT RAISE(ABORT, 'sec_filings: the asset has no CIK'); END;
CREATE TRIGGER IF NOT EXISTS trg_sf_cik_update BEFORE UPDATE OF asset_id ON sec_filings
WHEN COALESCE((SELECT a.cik FROM assets a WHERE a.id = NEW.asset_id), '') = ''
BEGIN SELECT RAISE(ABORT, 'sec_filings: the asset has no CIK'); END;
-- One accession reports one company, so it may be attached to several assets only when they
-- are one CIK (the share classes of one issuer: GOOGL/GOOG, NWSA/NWS, FOXA/FOX).
CREATE TRIGGER IF NOT EXISTS trg_sf_cross_cik_insert BEFORE INSERT ON sec_filings
WHEN NEW.accession_number IS NOT NULL AND EXISTS (
    SELECT 1 FROM sec_filings o JOIN assets oa ON oa.id = o.asset_id
    WHERE o.accession_number = NEW.accession_number AND o.asset_id <> NEW.asset_id
      AND oa.cik IS NOT (SELECT a.cik FROM assets a WHERE a.id = NEW.asset_id)
)
BEGIN SELECT RAISE(ABORT, 'sec_filings: accession_number already attached to an asset of another CIK'); END;
CREATE TRIGGER IF NOT EXISTS trg_sf_cross_cik_update
BEFORE UPDATE OF asset_id, accession_number ON sec_filings
WHEN NEW.accession_number IS NOT NULL AND EXISTS (
    SELECT 1 FROM sec_filings o JOIN assets oa ON oa.id = o.asset_id
    WHERE o.accession_number = NEW.accession_number AND o.asset_id <> NEW.asset_id
      AND o.id <> NEW.id
      AND oa.cik IS NOT (SELECT a.cik FROM assets a WHERE a.id = NEW.asset_id)
)
BEGIN SELECT RAISE(ABORT, 'sec_filings: accession_number already attached to an asset of another CIK'); END;
-- A filing's own CIK (the one it was listed and fetched under) is its asset's or a recorded
-- predecessor's of it (`cik_succession`), never any other. NULL (rows from before T-149) is unchecked.
CREATE TRIGGER IF NOT EXISTS trg_sf_chain_insert BEFORE INSERT ON sec_filings
WHEN NEW.cik IS NOT NULL
  AND NEW.cik IS NOT (SELECT a.cik FROM assets a WHERE a.id = NEW.asset_id)
  AND NOT EXISTS (
    SELECT 1 FROM cik_succession s JOIN assets a ON a.id = NEW.asset_id
    WHERE s.successor_cik = a.cik AND s.predecessor_cik = NEW.cik)
BEGIN SELECT RAISE(ABORT, 'sec_filings: the filing''s CIK is neither its asset''s nor a recorded predecessor''s'); END;
CREATE TRIGGER IF NOT EXISTS trg_sf_chain_update BEFORE UPDATE OF asset_id, cik ON sec_filings
WHEN NEW.cik IS NOT NULL
  AND NEW.cik IS NOT (SELECT a.cik FROM assets a WHERE a.id = NEW.asset_id)
  AND NOT EXISTS (
    SELECT 1 FROM cik_succession s JOIN assets a ON a.id = NEW.asset_id
    WHERE s.successor_cik = a.cik AND s.predecessor_cik = NEW.cik)
BEGIN SELECT RAISE(ABORT, 'sec_filings: the filing''s CIK is neither its asset''s nor a recorded predecessor''s'); END;
-- The CIK of an asset that has filings never changes or goes empty: its filings were fetched
-- by the old one. A CIK may still be set on an asset that has none yet, or filled in where it
-- was empty, as long as it keeps agreeing with the assets sharing an accession with it.
CREATE TRIGGER IF NOT EXISTS trg_assets_cik_keep BEFORE UPDATE OF cik ON assets
WHEN NEW.cik IS NOT OLD.cik
  AND (COALESCE(NEW.cik, '') = '' OR COALESCE(OLD.cik, '') <> '')
  AND EXISTS (SELECT 1 FROM sec_filings f WHERE f.asset_id = OLD.id)
BEGIN SELECT RAISE(ABORT, 'assets: the CIK of an asset with filings cannot change'); END;
CREATE TRIGGER IF NOT EXISTS trg_assets_cik_share BEFORE UPDATE OF cik ON assets
WHEN NEW.cik IS NOT OLD.cik AND EXISTS (
    SELECT 1 FROM sec_filings f JOIN sec_filings o ON o.accession_number = f.accession_number
    JOIN assets oa ON oa.id = o.asset_id
    WHERE f.asset_id = OLD.id AND o.asset_id <> OLD.id AND oa.cik IS NOT NEW.cik
)
BEGIN SELECT RAISE(ABORT, 'assets: the new CIK differs from an asset sharing one of its accessions'); END;
"""
