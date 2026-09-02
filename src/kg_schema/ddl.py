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

CREATE TABLE IF NOT EXISTS rule_catalog (
    rule_id     TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    severity    TEXT NOT NULL,                 -- 'HARD' | 'SOFT'
    params_json TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS veto (
    id            INTEGER PRIMARY KEY,
    asset_id      INTEGER NOT NULL REFERENCES assets(id),
    rule_id       TEXT NOT NULL REFERENCES rule_catalog(rule_id),
    severity      TEXT NOT NULL,
    detected_at   TEXT NOT NULL,
    cycle_date    TEXT NOT NULL,
    cleared_at    TEXT,
    evidence_json TEXT,
    run_id        INTEGER,
    UNIQUE (asset_id, rule_id, cycle_date)
);
CREATE INDEX IF NOT EXISTS ix_veto_active ON veto (cycle_date) WHERE cleared_at IS NULL;

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

CREATE TABLE IF NOT EXISTS cycle_run (
    id          INTEGER PRIMARY KEY,
    cycle_type  TEXT NOT NULL,                 -- 'SELECTION'|'MONITORING'|'ENTITY_RESOLUTION'
    cycle_date  TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    params_json TEXT,
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
    },
    "fundamental_metrics": {
        "event_time": "TEXT",
        "engine_version": "TEXT",
    },
    "price_window": {
        "event_time": "TEXT",
    },
    "price_daily": {
        "event_time": "TEXT",
        "ingested_at": "TEXT",
    },
}
