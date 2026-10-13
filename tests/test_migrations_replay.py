"""T-154: ``migrate`` never replays a rebuild on a database that already has the current shape.

A database created by ``kg_schema.ensure`` alone -- the path both agents' ``ensure_schema`` take --
has the current columns but no ``schema_version`` rows, so ``python -m <agent> migrate`` replayed
m001..m014 on it. Each rebuilding migration copies only the columns it itself knows, so every column
a later release added came back NULL. Reviewer A's reproduction: a fact stored with
``preferred_sign = -1`` and ``correction_rule = 'T-042'`` came back ``(None, None)`` (T-148's ID-12,
T-118).

The fix is two-sided:

* every rebuilding migration checks its own target shape first and returns early when it is already
  there (D1). ``tests/test_kg_schema.py``'s old-shape databases still migrate exactly as before -- the
  versioned key is still widened and ``filing_version`` still backfilled from the accession;
* a database that is already at the current shape records its floor (D2), so ``migrate`` applies
  nothing there at all.

Both sides are also what keeps a rebuild that legitimately runs -- on a database whose key is still
the pre-migration one -- from dropping ``preferred_sign``/``correction_rule``/``available_at``.
"""

from __future__ import annotations

from portfolio_common.db import Database

import kg_schema
from fundamental_agent import db as fundamental_db
from kg_schema import migrations, queries
from kg_schema.migrations import STAMPED_DESCRIPTION, at_current_shape, stamp_current_shape
from pricing_agent import db as pricing_db
from quant import db as quant_db

# Every table a rebuilding migration touches.
REBUILT_TABLES = (
    "financial_facts",
    "fundamental_metrics",
    "score_snapshot",
    "veto",
    "quant_portfolio",
    "cycle_run",
    "analysis_run",
    "pricing_run",
    "quant_run",
    "sec_filings",
)


def _full_db() -> Database:
    """A database exactly as the three agents' ``ensure_schema`` leave it: the current columns, no
    migration floor."""
    conn = kg_schema.connect(":memory:", create_parents=False)
    fundamental_db.ensure_schema(conn)
    pricing_db.ensure_schema(conn)
    quant_db.ensure_schema(conn)
    kg_schema.ensure(conn)
    return conn


def _seed_one_row_per_rebuilt_table(conn: Database) -> None:
    """One row in every rebuilt table, every column filled -- including the columns a later release
    added (``preferred_sign``, ``correction_rule``, ``forensic_flags_json``, ``available_at``,
    ``prompt_hash``, ``expires_on``) and the run tables' own."""
    conn.execute(
        "INSERT INTO assets (id, ticker, company_name, cik) VALUES (1, 'AAA', 'Asset A', "
        "'0000000001')"
    )
    conn.execute(
        "INSERT INTO rule_catalog (rule_id, description, severity, params_json, enabled, "
        "created_at) VALUES ('R1', 'rule one', 'HARD', '{}', 1, '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO sec_filings (id, asset_id, form, fiscal_year, fiscal_period, filing_date, "
        "accession_number, period_end, retrieved_at, run_id, available_at, cik) "
        "VALUES (1, 1, '10-K', 2025, 'FY2025', '2026-02-20', '0000000001-26-000001', "
        "'2025-12-31', '2026-02-21T00:00:00Z', 1, '2026-02-23', '0000000001')"
    )
    conn.execute(
        "INSERT INTO financial_facts (id, filing_id, statement, concept, standard_concept, label, "
        "period_key, value, filing_version, event_time, ingested_at, run_id, correction_rule, "
        "preferred_sign) VALUES (1, 1, 'cash_flow', 'us-gaap_Depreciation', 'Depreciation', "
        "'Depreciation', '2025-12-31 (FY)', 12.5, 'facts-v1', '2025-12-31', "
        "'2026-02-21T00:00:00Z', 1, 'T-042', -1.0)"
    )
    conn.execute(
        "INSERT INTO fundamental_metrics (id, filing_id, metric_group, metric_name, value, unit, "
        "inputs_json, computed_at, engine_version, event_time, run_id, available_at) "
        "VALUES (1, 1, 'profitability', 'net_margin', 0.25, 'ratio', '{\"a\": 1}', "
        "'2026-02-21T00:00:00Z', 'metrics-v7', '2025-12-31', 1, '2026-02-23')"
    )
    conn.execute(
        "INSERT INTO score_snapshot (id, asset_id, score_type, raw_value, normalized_score, "
        "event_time, computed_at, model, inputs_json, run_id, run_kind, filing_id, rating, "
        "narrative, strengths_json, risks_json, forensic_flags_json, available_at, prompt_hash) "
        "VALUES (1, 1, 'FUNDAMENTAL', 72.0, 0.5, '2025-12-31', '2026-02-21T00:00:00Z', "
        "'rule-based', '{\"b\": 2}', 1, 'analysis', 1, 'neutral', 'narrative', '[]', '[]', "
        "'{\"data_error_suspected\": false}', '2026-02-23', 'abc123')"
    )
    conn.execute(
        "INSERT INTO veto (id, asset_id, rule_id, severity, raised_on, cleared_on, last_seen_on, "
        "detected_at, cleared_at, evidence_json, run_id, expires_on, expiry_history_json) "
        "VALUES (1, 1, 'R1', 'HARD', '2026-01-31', '2026-02-28', '2026-02-28', "
        "'2026-01-31T00:00:00Z', '2026-02-28T00:00:00Z', '{\"c\": 3}', 1, '2026-03-31', '[]')"
    )
    conn.execute(
        "INSERT INTO quant_portfolio (id, quant_run_id, model_id, as_of, kind, frontier_k, "
        "objective, solver, status, expected_return, expected_vol, sharpe, rf_annual, n_positions, "
        "turnover, target_param, engine_version, computed_at, params_json, manifest_json) "
        "VALUES (1, 1, NULL, '2026-01-31', 'frontier_k', 1, 'min_var', 'CLARABEL', 'optimal', "
        "0.05, 0.12, 0.4, 0.045, 2, 0.1, 0.15, 'opt-v1+abcd1234', '2026-02-01T00:00:00Z', "
        "'{}', '{\"metrics\": \"metrics-v7\"}')"
    )
    conn.execute(
        "INSERT INTO cycle_run (id, cycle_type, cycle_date, started_at, finished_at, status, "
        "params_json, code_version) VALUES (1, 'SELECTION', '2026-01-31', "
        "'2026-01-31T00:00:00Z', '2026-01-31T01:00:00Z', 'completed', '{}', 'abc1234')"
    )
    conn.execute(
        "INSERT INTO analysis_run (id, started_at, finished_at, universe_size, planned_units, "
        "completed_units, skipped_units, failed_units, params_json, status, as_of, code_version, "
        "fallback_units) VALUES (1, '2026-01-31T00:00:00Z', '2026-01-31T01:00:00Z', 500, 500, 499, "
        "1, 0, '{}', 'completed', '2026-01-31', 'abc1234', 2)"
    )
    conn.execute(
        "INSERT INTO pricing_run (id, started_at, finished_at, universe_size, planned_units, "
        "completed_units, skipped_units, failed_units, params_json, status, as_of, code_version) "
        "VALUES (1, '2026-01-31T00:00:00Z', '2026-01-31T01:00:00Z', 500, 500, 500, 0, 0, '{}', "
        "'completed', '2026-01-31', 'abc1234')"
    )
    conn.execute(
        "INSERT INTO quant_run (id, command, as_of, started_at, finished_at, status, "
        "engine_version, params_json, error, code_version) VALUES (1, 'optimize', '2026-01-31', "
        "'2026-01-31T00:00:00Z', '2026-01-31T01:00:00Z', 'completed', 'opt-v1', '{}', NULL, "
        "'abc1234')"
    )
    conn.commit()


def _snapshot(conn: Database) -> dict[str, dict[object, dict[str, object]]]:
    """Every rebuilt table's rows, each keyed by its ``id`` and its column names -- so a rebuild
    that recreates the table in its own target column order still compares equal."""
    out: dict[str, dict[object, dict[str, object]]] = {}
    for table in REBUILT_TABLES:
        if not conn.relation_exists(table):
            continue
        columns = conn.table_columns(table)
        out[table] = {
            r["id"]: {c: r[c] for c in columns}
            for r in conn.execute(f"SELECT * FROM {table}")  # noqa: S608 - a test constant
        }
    return out


def _column_sets(conn: Database) -> dict[str, set[str]]:
    return {t: set(conn.table_columns(t)) for t in REBUILT_TABLES if conn.relation_exists(t)}


# -- (a) every value survives the replay --------------------------------------


def test_a_fresh_ensure_database_keeps_every_value_across_apply_migrations() -> None:
    """(a) A fresh ``ensure()`` database with one row in every rebuilt table, every column filled:
    ``apply_migrations`` leaves every value and every column exactly as it was."""
    conn = _full_db()
    _seed_one_row_per_rebuilt_table(conn)
    before = _snapshot(conn)
    columns_before = _column_sets(conn)

    applied = migrations.apply_migrations(conn)

    assert applied == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    assert _snapshot(conn) == before
    assert _column_sets(conn) == columns_before  # no column lost
    # the columns a later release added are the ones that used to come back NULL
    fact = conn.execute(
        "SELECT preferred_sign, correction_rule FROM financial_facts WHERE id = 1"
    ).fetchone()
    assert (fact["preferred_sign"], fact["correction_rule"]) == (-1.0, "T-042")
    metric = conn.execute("SELECT available_at FROM fundamental_metrics WHERE id = 1").fetchone()
    assert metric["available_at"] == "2026-02-23"
    veto = conn.execute("SELECT expires_on, expiry_history_json FROM veto WHERE id = 1").fetchone()
    assert (veto["expires_on"], veto["expiry_history_json"]) == ("2026-03-31", "[]")


# -- (b) the floor is recorded and a second run applies nothing ---------------


def test_the_same_database_records_every_version_and_a_second_run_applies_nothing() -> None:
    """(b) After (a) the database records 1..14 and ``apply_migrations`` returns ``[]``."""
    conn = _full_db()
    _seed_one_row_per_rebuilt_table(conn)
    migrations.apply_migrations(conn)

    assert queries.current_version(conn) == max(v for v, _, _ in migrations.MIGRATIONS)
    assert [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")] == [
        v for v, _, _ in migrations.MIGRATIONS
    ]
    assert migrations.apply_migrations(conn) == []


def test_a_database_at_the_current_shape_is_stamped_by_ensure_and_replays_nothing() -> None:
    """D2: a database that already has the current shape and no floor records every version with
    :data:`STAMPED_DESCRIPTION` -- ``migrate`` applies nothing there, not even a rebuild whose guard
    would have skipped it."""
    conn = _full_db()
    migrations.apply_migrations(conn)
    conn.execute("DELETE FROM schema_version")  # the floor is lost; the shape is current
    conn.commit()
    assert at_current_shape(conn)

    assert stamp_current_shape(conn) == len(migrations.MIGRATIONS)
    descriptions = {str(r[0]) for r in conn.execute("SELECT description FROM schema_version")}
    assert descriptions == {STAMPED_DESCRIPTION}
    assert queries.current_version(conn) == max(v for v, _, _ in migrations.MIGRATIONS)
    assert migrations.apply_migrations(conn) == []
    # idempotent: a second ensure stamps nothing
    assert stamp_current_shape(conn) == 0


def test_a_skipped_rebuild_is_still_recorded() -> None:
    """D1: a rebuild that is skipped because its target shape is already there still records its
    version, so the floor ends up correct."""
    conn = _full_db()
    migrations.apply_migrations(conn)
    before = _snapshot(conn)
    conn.execute("DELETE FROM schema_version WHERE version >= 13")
    conn.commit()

    assert migrations.apply_migrations(conn) == [13, 14]  # 14 skips its rebuild, still records
    assert _snapshot(conn) == before


# -- (c) an old-shape database still migrates as before -----------------------


_LEGACY_DDL = """
CREATE TABLE assets (
    id INTEGER PRIMARY KEY, ticker TEXT NOT NULL UNIQUE, company_name TEXT, cik TEXT,
    sector_id INTEGER, sub_industry TEXT
);
CREATE TABLE sec_filings (
    id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES assets(id),
    form TEXT NOT NULL, fiscal_year INTEGER NOT NULL, fiscal_period TEXT NOT NULL,
    filing_date TEXT, accession_number TEXT, period_end TEXT, retrieved_at TEXT NOT NULL,
    UNIQUE (asset_id, form, fiscal_period)
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY, filing_id INTEGER NOT NULL REFERENCES sec_filings(id) ON DELETE CASCADE,
    statement TEXT NOT NULL, concept TEXT NOT NULL, standard_concept TEXT, label TEXT,
    period_key TEXT NOT NULL, value REAL,
    UNIQUE (filing_id, statement, concept, period_key)
);
CREATE TABLE fundamental_metrics (
    id INTEGER PRIMARY KEY, filing_id INTEGER NOT NULL REFERENCES sec_filings(id) ON DELETE CASCADE,
    metric_group TEXT NOT NULL, metric_name TEXT NOT NULL, value REAL, unit TEXT,
    inputs_json TEXT, computed_at TEXT NOT NULL,
    UNIQUE (filing_id, metric_group, metric_name)
);
INSERT INTO assets (id, ticker, cik) VALUES (1, 'AAA', '0000000001');
INSERT INTO sec_filings (id, asset_id, form, fiscal_year, fiscal_period, filing_date,
                         accession_number, period_end, retrieved_at)
    VALUES (1, 1, '10-K', 2025, 'FY2025', '2026-02-20', '0000000001-26-000001', '2025-12-31',
            '2026-02-21T00:00:00Z');
INSERT INTO financial_facts (id, filing_id, statement, concept, period_key, value)
    VALUES (1, 1, 'cash_flow', 'us-gaap_Depreciation', '2025-12-31 (FY)', 12.5);
INSERT INTO fundamental_metrics (id, filing_id, metric_group, metric_name, value, unit,
                                 computed_at)
    VALUES (1, 1, 'profitability', 'net_margin', 0.25, 'ratio', '2026-02-21T00:00:00Z');
"""


def _legacy_db() -> Database:
    """The pre-kg_schema shape the existing migration tests build, with the columns a later
    release added (``preferred_sign``, ``correction_rule``, ``available_at``) in place -- exactly
    the database reviewer A reproduced on."""
    conn = kg_schema.connect(":memory:", create_parents=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_LEGACY_DDL)
    conn.commit()
    fundamental_db.ensure_schema(conn)  # additive: adds the columns, leaves the old keys
    conn.execute("UPDATE financial_facts SET preferred_sign = -1, correction_rule = 'T-042'")
    conn.execute(
        "UPDATE fundamental_metrics SET engine_version = 'metrics-v7', event_time = '2025-12-31'"
    )
    conn.execute("UPDATE sec_filings SET available_at = '2026-02-23'")
    conn.execute("UPDATE fundamental_metrics SET available_at = '2026-02-23'")
    conn.commit()
    return conn


def test_an_old_shape_database_still_widens_its_key_and_backfills_the_version() -> None:
    """(c) The old-shape database still migrates exactly as before: m002/m003 widen the key and
    backfill ``filing_version``/``engine_version`` from the filing (the assertions
    ``tests/test_kg_schema.py`` has always made), and the columns a later release added survive."""
    conn = _legacy_db()
    assert not migrations._has_unique_key(conn, "financial_facts")

    applied = migrations.apply_migrations(conn)

    assert applied == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    assert migrations._has_unique_key(conn, "financial_facts")
    assert migrations._has_unique_key(conn, "fundamental_metrics")
    fact = conn.execute(
        "SELECT filing_version, event_time, preferred_sign, correction_rule FROM financial_facts"
    ).fetchone()
    assert fact["filing_version"] == "0000000001-26-000001"  # backfilled from the accession
    assert fact["event_time"] == "2025-12-31"
    assert (fact["preferred_sign"], fact["correction_rule"]) == (-1.0, "T-042")  # not dropped
    metric = conn.execute("SELECT engine_version, available_at FROM fundamental_metrics").fetchone()
    assert (metric["engine_version"], metric["available_at"]) == ("metrics-v7", "2026-02-23")


# -- (d) reviewer A's reproduction --------------------------------------------


def test_reviewer_a_reproduction_a_stored_sign_and_correction_survive_apply_migrations() -> None:
    """(d) The reproduction from ``/workspaces/thesis/scratch/t150/reviewA/mig_repro.py``: a fact
    stored with ``preferred_sign = -1`` and ``correction_rule = 'T-042'`` used to come back
    ``(None, None)``."""
    conn = _full_db()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO financial_facts (filing_id, statement, concept, period_key, value, "
        "preferred_sign, correction_rule) "
        "VALUES (1, 'income_statement', 'us-gaap_X', '2024-12-31 (FY)', 5.0, -1, 'T-042')"
    )
    conn.commit()
    before = conn.execute("SELECT preferred_sign, correction_rule FROM financial_facts").fetchall()
    assert [tuple(r) for r in before] == [(-1.0, "T-042")]

    migrations.apply_migrations(conn)

    after = conn.execute("SELECT preferred_sign, correction_rule FROM financial_facts").fetchall()
    assert [tuple(r) for r in after] == [(-1.0, "T-042")]


# -- the data-carrying migrations change no row -------------------------------


def test_replaying_the_data_carrying_migrations_changes_no_row() -> None:
    """D1: m001, m008 and m010-m013 are additive or trigger-only. Replaying them on a database whose
    rows already carry their values changes no row."""
    conn = _full_db()
    _seed_one_row_per_rebuilt_table(conn)
    migrations.apply_migrations(conn)
    before = _snapshot(conn)
    conn.execute("DELETE FROM schema_version WHERE version >= 8")
    conn.commit()

    assert migrations.apply_migrations(conn) == [8, 9, 10, 11, 12, 13, 14]
    assert _snapshot(conn) == before
