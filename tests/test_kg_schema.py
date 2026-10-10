"""Shared-schema DDL, migrations, views, and membership reconciliation."""

from __future__ import annotations

import sqlite3

import pytest
from portfolio_common.db import Database

import kg_schema
from fundamental_agent.sections import _SPECS, canonical_item_label
from kg_schema import migrations, queries
from kg_schema.env import database_path

# Pre-kg_schema table shapes, as the two agents shipped them originally.
_LEGACY_SQL = """
CREATE TABLE sectors (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE assets (
    id INTEGER PRIMARY KEY, ticker TEXT NOT NULL UNIQUE, company_name TEXT, cik TEXT,
    sector_id INTEGER REFERENCES sectors(id), sub_industry TEXT
);
CREATE TABLE sec_filings (
    id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES assets(id),
    form TEXT NOT NULL, fiscal_year INTEGER NOT NULL, fiscal_period TEXT NOT NULL,
    filing_date TEXT, accession_number TEXT, period_end TEXT, retrieved_at TEXT NOT NULL,
    UNIQUE (asset_id, form, fiscal_period)
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY,
    filing_id INTEGER NOT NULL REFERENCES sec_filings(id) ON DELETE CASCADE,
    statement TEXT NOT NULL, concept TEXT NOT NULL, standard_concept TEXT, label TEXT,
    period_key TEXT NOT NULL, value REAL,
    UNIQUE (filing_id, statement, concept, period_key)
);
CREATE TABLE fundamental_metrics (
    id INTEGER PRIMARY KEY,
    filing_id INTEGER NOT NULL REFERENCES sec_filings(id) ON DELETE CASCADE,
    metric_group TEXT NOT NULL, metric_name TEXT NOT NULL, value REAL, unit TEXT,
    inputs_json TEXT, computed_at TEXT NOT NULL,
    UNIQUE (filing_id, metric_group, metric_name)
);
CREATE TABLE fundamental_snapshot (
    id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES assets(id),
    filing_id INTEGER NOT NULL REFERENCES sec_filings(id), form TEXT NOT NULL,
    fiscal_period TEXT NOT NULL, score REAL NOT NULL, rating TEXT NOT NULL,
    narrative TEXT NOT NULL, strengths_json TEXT, risks_json TEXT, model TEXT NOT NULL,
    metrics_json TEXT, created_at TEXT NOT NULL,
    UNIQUE (asset_id, form, fiscal_period)
);
"""


def _legacy_conn() -> Database:
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    conn = Database(raw)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_LEGACY_SQL)
    conn.execute("INSERT INTO assets (ticker, company_name) VALUES ('AAPL', 'Apple')")
    conn.execute("INSERT INTO assets (ticker, company_name) VALUES ('MSFT', 'Microsoft')")
    conn.execute(
        "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, period_end, "
        "filing_date, accession_number, retrieved_at) VALUES (1, '10-K', 2023, 'FY2023', "
        "'2023-09-30', '2023-11-03', '0000320193-23-000106', '2024-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO financial_facts (filing_id, statement, concept, period_key, value) "
        "VALUES (1, 'income_statement', 'Revenues', '2023-09-30 (FY)', 383285000000.0)"
    )
    conn.execute(
        "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, unit, "
        "computed_at) VALUES (1, 'profitability', 'net_margin', 0.25, 'ratio', "
        "'2024-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO fundamental_snapshot (asset_id, filing_id, form, fiscal_period, score, "
        "rating, narrative, model, created_at) VALUES (1, 1, '10-K', 'FY2023', 72.0, "
        "'bullish', 'n', 'deepseek-chat', '2024-01-02T00:00:00Z')"
    )
    conn.commit()
    return conn


@pytest.fixture
def migrated_db() -> Database:
    conn = _legacy_conn()
    kg_schema.ensure(conn, run_migrations=True)
    return conn


def test_ensure_is_idempotent_and_additive() -> None:
    conn = _legacy_conn()
    kg_schema.ensure(conn)
    kg_schema.ensure(conn)  # second call must not raise
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"score_snapshot", "universe_membership", "veto", "price_observation"} <= tables
    assert {
        "corporate_action",
        "quant_return_daily",
        "risk_free_rate",
        "benchmark_series",
    } <= tables
    # additive columns present
    ff_cols = {r[1] for r in conn.execute("PRAGMA table_info(financial_facts)")}
    assert {"event_time", "filing_version"} <= ff_cols
    # additive-only path never advances the version
    assert queries.current_version(conn) == 0


def test_migrations_rebuild_and_preserve_rows(migrated_db: Database) -> None:
    conn = migrated_db
    assert queries.current_version(conn) == 11
    # score_type CHECK admits 'SECTOR' after m005
    conn.execute(
        "INSERT INTO score_snapshot (asset_id, score_type, raw_value, event_time, computed_at) "
        "VALUES (1, 'SECTOR', -4.0, '2026-08-05', '2026-08-05T00:00:00Z')"
    )
    # financial_facts rebuilt with filing_version in the unique key, backfilled
    ff = conn.execute("SELECT filing_version, event_time FROM financial_facts").fetchone()
    assert ff["filing_version"] == "0000320193-23-000106"
    assert ff["event_time"] == "2023-09-30"
    # fundamental_metrics likewise
    fm = conn.execute("SELECT engine_version, event_time FROM fundamental_metrics").fetchone()
    assert fm["engine_version"] == "pre-v1"
    assert fm["event_time"] == "2023-09-30"
    # snapshot migrated into score_snapshot as FUNDAMENTAL, old name now a view
    row = conn.execute("SELECT score_type, raw_value, event_time FROM score_snapshot").fetchone()
    assert (row["score_type"], row["raw_value"], row["event_time"]) == (
        "FUNDAMENTAL",
        72.0,
        "2023-09-30",
    )
    kind = conn.execute(
        "SELECT type FROM sqlite_master WHERE name = 'fundamental_snapshot'"
    ).fetchone()["type"]
    assert kind == "view"
    # the compatibility view still answers the README-style query
    compat = conn.execute(
        "SELECT a.ticker, s.form, s.fiscal_period, s.score, s.rating "
        "FROM fundamental_snapshot s JOIN assets a ON a.id = s.asset_id"
    ).fetchone()
    assert tuple(compat) == ("AAPL", "10-K", "FY2023", 72.0, "bullish")


def test_migrations_are_a_noop_second_time(migrated_db: Database) -> None:
    assert migrations.apply_migrations(migrated_db) == []


def test_m009_collapses_per_date_veto_hits_into_stints() -> None:
    """T-125: a database at floor 8 has ``veto``'s old per-(asset, rule, cycle_date) hit-event
    shape. Asset 1/R1 was hit on 2026-06-30 and 2026-07-07 (one continuous run), missed on
    2026-07-14 (an evaluated cycle that recorded no row for it -- the condition cleared), then
    hit again on 2026-07-21 (a fresh re-raise, still open). The migration must collapse this
    into exactly two stints, closing the first at the date it first went unrecorded."""
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    conn = Database(raw)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE sectors (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT NOT NULL UNIQUE,
                             sector_id INTEGER, sub_industry TEXT);
        CREATE TABLE rule_catalog (
            rule_id TEXT PRIMARY KEY, description TEXT NOT NULL, severity TEXT NOT NULL,
            params_json TEXT, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE cycle_run (
            id INTEGER PRIMARY KEY, cycle_type TEXT NOT NULL, cycle_date TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
            params_json TEXT, code_version TEXT, UNIQUE (cycle_type, cycle_date)
        );
        CREATE TABLE cycle_checkpoint (
            id INTEGER PRIMARY KEY,
            cycle_run_id INTEGER NOT NULL REFERENCES cycle_run(id) ON DELETE CASCADE,
            step TEXT NOT NULL, status TEXT NOT NULL, detail_json TEXT,
            updated_at TEXT NOT NULL, UNIQUE (cycle_run_id, step)
        );
        CREATE TABLE veto (
            id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES assets(id),
            rule_id TEXT NOT NULL REFERENCES rule_catalog(rule_id), severity TEXT NOT NULL,
            detected_at TEXT NOT NULL, cycle_date TEXT NOT NULL, cleared_at TEXT,
            evidence_json TEXT, run_id INTEGER,
            UNIQUE (asset_id, rule_id, cycle_date)
        );
        INSERT INTO assets (id, ticker) VALUES (1, 'AAA');
        INSERT INTO rule_catalog (rule_id, description, severity, created_at)
            VALUES ('R1', 'x', 'HARD', '2026-01-01');
        """
    )
    for i, cycle_date in enumerate(
        ("2026-06-30", "2026-07-07", "2026-07-14", "2026-07-21"), start=1
    ):
        conn.execute(
            "INSERT INTO cycle_run (id, cycle_type, cycle_date, started_at, status) "
            "VALUES (?, 'SELECTION', ?, ?, 'completed')",
            (i, cycle_date, cycle_date + "T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO cycle_checkpoint (cycle_run_id, step, status, updated_at) "
            "VALUES (?, 'veto', 'done', ?)",
            (i, cycle_date + "T00:00:00Z"),
        )
    for cycle_date in ("2026-06-30", "2026-07-07", "2026-07-21"):  # 07-14 has no row: it cleared
        conn.execute(
            "INSERT INTO veto (asset_id, rule_id, severity, detected_at, cycle_date) "
            "VALUES (1, 'R1', 'HARD', ?, ?)",
            (cycle_date + "T00:00:00Z", cycle_date),
        )
    queries.ensure(conn)
    queries.record(conn, 8, "pretend floor")
    conn.commit()

    applied = kg_schema.ensure(conn, run_migrations=True)
    assert 9 in applied
    # m009 rebuilds `veto` without T-070's columns; m011, which follows it, puts them back
    assert 11 in applied
    assert {"expires_on", "expiry_history_json"} <= set(conn.table_columns("veto"))

    rows = conn.execute(
        "SELECT raised_on, cleared_on, last_seen_on, expires_on FROM veto WHERE asset_id = 1 "
        "ORDER BY raised_on"
    ).fetchall()
    assert [r["expires_on"] for r in rows] == [None, None]  # collapsed history is not temporal
    assert len(rows) == 2
    first, second = rows
    assert (first["raised_on"], first["last_seen_on"], first["cleared_on"]) == (
        "2026-06-30",
        "2026-07-07",
        "2026-07-14",
    )
    assert (second["raised_on"], second["last_seen_on"], second["cleared_on"]) == (
        "2026-07-21",
        "2026-07-21",
        None,
    )
    # the point-in-time predicate agrees: cleared during the gap, active again after the re-raise
    assert queries.hard_vetoed_as_of(conn, "2026-07-10") == {1}
    assert queries.hard_vetoed_as_of(conn, "2026-07-15") == set()
    assert queries.hard_vetoed_as_of(conn, "2026-07-21") == {1}

    # idempotent: re-running the migration a second time is a no-op
    assert migrations.apply_migrations(conn) == []


def test_m009_ignores_a_row_the_old_writer_had_already_cleared_same_date() -> None:
    """PR #103 review: the pre-T-125 writer set ``cleared_at`` on a row when a same-date
    re-run no longer hit that (asset, rule) pair (``ON CONFLICT ... cleared_at = NULL`` on a
    fresh hit; left set otherwise) -- such a row's ``cycle_date`` was NOT a hit in that
    date's final, persisted verdict, and the migration must not read it as one. The date
    still counts as an evaluation date through ``cycle_checkpoint`` regardless, so a rule
    with only such a row for an asset must end up with zero open stints for it."""
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    conn = Database(raw)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE sectors (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT NOT NULL UNIQUE,
                             sector_id INTEGER, sub_industry TEXT);
        CREATE TABLE rule_catalog (
            rule_id TEXT PRIMARY KEY, description TEXT NOT NULL, severity TEXT NOT NULL,
            params_json TEXT, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE cycle_run (
            id INTEGER PRIMARY KEY, cycle_type TEXT NOT NULL, cycle_date TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
            params_json TEXT, code_version TEXT, UNIQUE (cycle_type, cycle_date)
        );
        CREATE TABLE cycle_checkpoint (
            id INTEGER PRIMARY KEY,
            cycle_run_id INTEGER NOT NULL REFERENCES cycle_run(id) ON DELETE CASCADE,
            step TEXT NOT NULL, status TEXT NOT NULL, detail_json TEXT,
            updated_at TEXT NOT NULL, UNIQUE (cycle_run_id, step)
        );
        CREATE TABLE veto (
            id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES assets(id),
            rule_id TEXT NOT NULL REFERENCES rule_catalog(rule_id), severity TEXT NOT NULL,
            detected_at TEXT NOT NULL, cycle_date TEXT NOT NULL, cleared_at TEXT,
            evidence_json TEXT, run_id INTEGER,
            UNIQUE (asset_id, rule_id, cycle_date)
        );
        INSERT INTO assets (id, ticker) VALUES (1, 'AAA');
        INSERT INTO rule_catalog (rule_id, description, severity, created_at)
            VALUES ('R1', 'x', 'HARD', '2026-01-01');
        INSERT INTO cycle_run (id, cycle_type, cycle_date, started_at, status)
            VALUES (1, 'SELECTION', '2026-06-30', '2026-06-30T00:00:00Z', 'completed');
        INSERT INTO cycle_checkpoint (cycle_run_id, step, status, updated_at)
            VALUES (1, 'veto', 'done', '2026-06-30T00:00:00Z');
        INSERT INTO veto (asset_id, rule_id, severity, detected_at, cycle_date, cleared_at)
            VALUES (1, 'R1', 'HARD', '2026-06-30T00:00:00Z', '2026-06-30', '2026-06-30T01:00:00Z');
        """
    )
    queries.ensure(conn)
    queries.record(conn, 8, "pretend floor")
    conn.commit()

    assert 9 in kg_schema.ensure(conn, run_migrations=True)

    assert conn.execute("SELECT COUNT(*) FROM veto WHERE asset_id = 1").fetchone()[0] == 0
    assert queries.hard_vetoed_as_of(conn, "2026-06-30") == set()


def test_database_path_prefers_canonical_then_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KG_FINANCIAL_DB", raising=False)
    monkeypatch.delenv("KG_FINANTIAL_DB", raising=False)
    assert database_path() is None
    assert database_path("/explicit.db") == "/explicit.db"

    monkeypatch.setenv("KG_FINANTIAL_DB", "/legacy.db")
    assert database_path() == "/legacy.db"  # misspelled fallback still works

    monkeypatch.setenv("KG_FINANCIAL_DB", "/canonical.db")
    assert database_path() == "/canonical.db"  # canonical wins when both are set
    assert database_path("/explicit.db") == "/explicit.db"  # explicit beats both


def test_m005_widens_score_type_check_and_keeps_rows_and_view() -> None:
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    conn = Database(raw)
    conn.execute("PRAGMA foreign_keys = ON")
    # a database already at floor 4: score_snapshot with the pre-SECTOR CHECK + compat view
    conn.executescript(
        """
        CREATE TABLE sectors (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT NOT NULL UNIQUE,
                             sector_id INTEGER, sub_industry TEXT);
        CREATE TABLE sec_filings (id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL,
                                  form TEXT NOT NULL, fiscal_year INTEGER NOT NULL,
                                  fiscal_period TEXT NOT NULL, filing_date TEXT,
                                  accession_number TEXT, period_end TEXT,
                                  retrieved_at TEXT NOT NULL);
        INSERT INTO assets (id, ticker) VALUES (1, 'AAPL');
        INSERT INTO sec_filings (id, asset_id, form, fiscal_year, fiscal_period, filing_date,
                                 retrieved_at)
        VALUES (1, 1, '10-K', 2023, 'FY2023', '2023-11-03', '2024-01-01T00:00:00Z');
        CREATE TABLE score_snapshot (
            id INTEGER PRIMARY KEY,
            asset_id INTEGER NOT NULL REFERENCES assets(id),
            score_type TEXT NOT NULL CHECK (score_type IN
                ('FUNDAMENTAL', 'QUANTITATIVE', 'TECHNICAL', 'SEMANTIC')),
            raw_value REAL, normalized_score REAL, event_time TEXT NOT NULL,
            computed_at TEXT NOT NULL, model TEXT, inputs_json TEXT, run_id INTEGER,
            run_kind TEXT, filing_id INTEGER REFERENCES sec_filings(id),
            rating TEXT, narrative TEXT, strengths_json TEXT, risks_json TEXT,
            UNIQUE (asset_id, score_type, event_time)
        );
        INSERT INTO score_snapshot (asset_id, score_type, raw_value, event_time, computed_at,
                                    filing_id, rating, narrative)
        VALUES (1, 'FUNDAMENTAL', 72.0, '2023-09-30', '2024-01-02T00:00:00Z', 1, 'bullish', 'n');
        CREATE VIEW fundamental_snapshot AS
        SELECT s.id, s.asset_id, s.filing_id, f.form, f.fiscal_period, s.raw_value AS score,
               s.rating, s.narrative, s.strengths_json, s.risks_json, s.model,
               s.inputs_json AS metrics_json, s.computed_at AS created_at
        FROM score_snapshot s JOIN sec_filings f ON f.id = s.filing_id
        WHERE s.score_type = 'FUNDAMENTAL';
        """
    )
    queries.ensure(conn)
    queries.record(conn, 4, "pretend floor")
    conn.commit()

    assert 5 in kg_schema.ensure(conn, run_migrations=True)

    # SECTOR now accepted, the FUNDAMENTAL row survived the rebuild
    conn.execute(
        "INSERT INTO score_snapshot (asset_id, score_type, raw_value, event_time, computed_at) "
        "VALUES (1, 'SECTOR', -3.5, '2026-08-05', '2026-08-05T00:00:00Z')"
    )
    kept = conn.execute(
        "SELECT score_type, raw_value FROM score_snapshot WHERE score_type = 'FUNDAMENTAL'"
    ).fetchone()
    assert (kept["score_type"], kept["raw_value"]) == ("FUNDAMENTAL", 72.0)
    # the compat view is still a working view
    assert (
        conn.execute(
            "SELECT type FROM sqlite_master WHERE name = 'fundamental_snapshot'"
        ).fetchone()[0]
        == "view"
    )
    assert conn.execute("SELECT score FROM fundamental_snapshot").fetchone()["score"] == 72.0


def test_m006_renames_quantitative_score_type_to_valorization() -> None:
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    conn = Database(raw)
    conn.execute("PRAGMA foreign_keys = ON")
    # a database already at floor 5: SECTOR admitted, but the blend factor is still
    # called 'QUANTITATIVE' -- in the score_type CHECK, the rows, and the blend JSON.
    conn.executescript(
        """
        CREATE TABLE sectors (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT NOT NULL UNIQUE,
                             sector_id INTEGER, sub_industry TEXT);
        CREATE TABLE sec_filings (id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL,
                                  form TEXT NOT NULL, fiscal_year INTEGER NOT NULL,
                                  fiscal_period TEXT NOT NULL, filing_date TEXT,
                                  accession_number TEXT, period_end TEXT,
                                  retrieved_at TEXT NOT NULL);
        INSERT INTO assets (id, ticker) VALUES (1, 'AAPL');
        CREATE TABLE score_snapshot (
            id INTEGER PRIMARY KEY,
            asset_id INTEGER NOT NULL REFERENCES assets(id),
            score_type TEXT NOT NULL CHECK (score_type IN
                ('FUNDAMENTAL', 'QUANTITATIVE', 'TECHNICAL', 'SEMANTIC', 'SECTOR')),
            raw_value REAL, normalized_score REAL, event_time TEXT NOT NULL,
            computed_at TEXT NOT NULL, model TEXT, inputs_json TEXT, run_id INTEGER,
            run_kind TEXT, filing_id INTEGER REFERENCES sec_filings(id),
            rating TEXT, narrative TEXT, strengths_json TEXT, risks_json TEXT,
            UNIQUE (asset_id, score_type, event_time)
        );
        INSERT INTO score_snapshot (asset_id, score_type, raw_value, event_time, computed_at)
        VALUES (1, 'QUANTITATIVE', 63.0, '2026-06-30', '2026-07-01T00:00:00Z'),
               (1, 'TECHNICAL',    55.0, '2026-06-30', '2026-07-01T00:00:00Z');
        CREATE TABLE cycle_run (
            id INTEGER PRIMARY KEY, cycle_type TEXT NOT NULL, cycle_date TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
            params_json TEXT, UNIQUE (cycle_type, cycle_date)
        );
        CREATE TABLE cycle_ranking (
            id INTEGER PRIMARY KEY, cycle_run_id INTEGER NOT NULL REFERENCES cycle_run(id),
            asset_id INTEGER NOT NULL, rank INTEGER NOT NULL, blended_score REAL NOT NULL,
            components_json TEXT, vetoed INTEGER NOT NULL DEFAULT 0, veto_rules_json TEXT,
            selected INTEGER NOT NULL DEFAULT 0, target_weight REAL,
            UNIQUE (cycle_run_id, asset_id)
        );
        INSERT INTO cycle_run (id, cycle_type, cycle_date, started_at, status, params_json)
        VALUES (1, 'SELECTION', '2026-06-30', '2026-07-01T00:00:00Z', 'completed',
                '{"score_weights": {"FUNDAMENTAL": 0.4, "QUANTITATIVE": 0.3, "TECHNICAL": 0.2, "SEMANTIC": 0.1}}');
        INSERT INTO cycle_ranking (cycle_run_id, asset_id, rank, blended_score, components_json)
        VALUES (1, 1, 1, 61.0, '{"FUNDAMENTAL": 70.0, "QUANTITATIVE": 63.0}');
        """
    )
    queries.ensure(conn)
    queries.record(conn, 5, "pretend floor")
    conn.commit()

    assert 6 in kg_schema.ensure(conn, run_migrations=True)

    # the row was renamed, and the new CHECK now admits VALORIZATION (not QUANTITATIVE)
    types = {r[0] for r in conn.execute("SELECT DISTINCT score_type FROM score_snapshot")}
    assert types == {"VALORIZATION", "TECHNICAL"}
    conn.execute(
        "INSERT INTO score_snapshot (asset_id, score_type, raw_value, event_time, computed_at) "
        "VALUES (1, 'VALORIZATION', 40.0, '2026-07-31', '2026-08-01T00:00:00Z')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO score_snapshot (asset_id, score_type, raw_value, event_time, computed_at) "
            "VALUES (1, 'QUANTITATIVE', 1.0, '2026-08-31', '2026-09-01T00:00:00Z')"
        )
    # the recorded blend JSON was rekeyed too
    params = conn.execute("SELECT params_json FROM cycle_run WHERE id = 1").fetchone()[0]
    assert "QUANTITATIVE" not in params and '"VALORIZATION": 0.3' in params
    comps = conn.execute("SELECT components_json FROM cycle_ranking WHERE rank = 1").fetchone()[0]
    assert "QUANTITATIVE" not in comps and '"VALORIZATION": 63.0' in comps
    comp_types = {
        r["score_type"] for r in conn.execute("SELECT score_type FROM v_weight_component")
    }
    assert "VALORIZATION" in comp_types and "QUANTITATIVE" not in comp_types


def test_views_select_cleanly(migrated_db: Database) -> None:
    for name in (
        "v_score_snapshot",
        "v_universe_membership",
        "v_sector",
        "v_industry",
        "v_price_observation",
        "v_sec_filing",
        "v_sec_filing_section",
        "v_veto",
        "v_rule_catalog",
        "v_data_quality_issue",
        "v_weight_scheme",
        "v_weight_component",
        "v_sector_aggregate_snapshot",
        "v_corporate_action",
        "v_quant_return_daily",
        "v_risk_free_rate",
        "v_benchmark_series",
        "v_quant_risk_model",
        "v_quant_portfolio",
        "v_quant_position",
        "v_quant_frontier_point",
        "v_quant_benchmark_performance",
        "v_quant_vs_live",
    ):
        migrated_db.execute(f"SELECT * FROM {name} LIMIT 1").fetchall()  # noqa: S608 - fixed view names


def test_v_sec_filing_is_one_row_per_filing(migrated_db: Database) -> None:
    rows = migrated_db.execute(
        "SELECT ticker, form, fiscal_period, accession_number, period_end FROM v_sec_filing"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("AAPL", "10-K", "FY2023", "0000320193-23-000106", "2023-09-30")
    ]


def test_v_sec_filing_section_item_label_matches_python_vocab(
    migrated_db: Database,
) -> None:
    conn = migrated_db
    cases: list[tuple[str | None, str]] = [
        (num, stype) for form in _SPECS.values() for num, (stype, _) in form.items()
    ]
    cases.append((None, "MD&A"))  # missing item number -> bare token
    rows = [
        (i, 1, stype, num, i, "t" * 500, "sha", 10, "regex", "2023-09-30", "2024-01-01", "v1")
        for i, (num, stype) in enumerate(cases)
    ]
    conn.executemany(
        "INSERT INTO sec_filing_section (id, filing_id, section_type, item_number, ordinal, "
        "text, text_sha256, word_count, extraction_method, event_time, retrieved_at, "
        "engine_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()

    got = {
        r["id"]: r["item_label"]
        for r in conn.execute("SELECT id, item_label FROM v_sec_filing_section")
    }
    for i, (num, stype) in enumerate(cases):
        assert got[i] == canonical_item_label(num, stype)
    assert got[0] == "ITEM_1_BUSINESS"
    assert got[len(cases) - 1] == "MDA"


def test_v_sector_and_v_industry_roll_up_assets(migrated_db: Database) -> None:
    conn = migrated_db
    conn.execute("INSERT INTO sectors (id, name) VALUES (1, 'Information Technology')")
    conn.executemany(
        "UPDATE assets SET sector_id = 1, sub_industry = ? WHERE ticker = ?",
        [("Technology Hardware", "AAPL"), ("Systems Software", "MSFT")],
    )
    conn.commit()
    sec = conn.execute(
        "SELECT sector_name, asset_count, sub_industry_count FROM v_sector WHERE sector_id = 1"
    ).fetchone()
    assert tuple(sec) == ("Information Technology", 2, 2)
    inds = {
        r["industry_name"]: (r["sector_name"], r["asset_count"])
        for r in conn.execute("SELECT * FROM v_industry")
    }
    assert inds == {
        "Technology Hardware": ("Information Technology", 1),
        "Systems Software": ("Information Technology", 1),
    }


def test_v_rule_catalog_unpacks_threshold_params(migrated_db: Database) -> None:
    conn = migrated_db
    conn.executemany(
        "INSERT INTO rule_catalog (rule_id, description, severity, params_json, enabled, "
        "created_at) VALUES (?, ?, ?, ?, ?, '2026-01-01T00:00:00Z')",
        [
            (
                "LEVERAGE_EXTREME",
                "debt/equity too high",
                "HARD",
                '{"metric": "leverage.debt_to_equity", "op": ">", "threshold": 3.0}',
                1,
            ),
            ("PRICE_CRASH", "deep drawdown", "SOFT", '{"threshold": -0.35}', 0),
        ],
    )
    conn.commit()
    rows = {
        r["rule_id"]: (
            r["severity"],
            r["enabled"],
            r["param_metric"],
            r["param_operator"],
            r["param_threshold"],
        )
        for r in conn.execute("SELECT * FROM v_rule_catalog")
    }
    assert rows["LEVERAGE_EXTREME"] == ("HARD", 1, "leverage.debt_to_equity", ">", 3.0)
    assert rows["PRICE_CRASH"] == ("SOFT", 0, None, None, -0.35)


def test_weight_scheme_views_explode_the_blend(migrated_db: Database) -> None:
    conn = migrated_db
    conn.execute(
        "INSERT INTO cycle_run (cycle_type, cycle_date, started_at, status, params_json) "
        "VALUES ('SELECTION', '2026-08-05', '2026-08-05T00:00:00Z', 'done', ?)",
        (
            '{"weight_scheme": "score_proportional", "top_n": 30, "max_name_weight": 0.1, '
            '"max_sector_weight": 0.3, "soft_veto_penalty": 15.0, '
            '"score_weights": {"FUNDAMENTAL": 0.4, "VALORIZATION": 0.3, "TECHNICAL": 0.2, '
            '"SEMANTIC": 0.1}}',
        ),
    )
    # a run with no blend recorded must not appear in either view
    conn.execute(
        "INSERT INTO cycle_run (cycle_type, cycle_date, started_at, status, params_json) "
        "VALUES ('ENTITY_RESOLUTION', '2026-08-06', '2026-08-06T00:00:00Z', 'done', '{}')"
    )
    conn.commit()

    scheme = conn.execute(
        "SELECT scheme_id, top_n, max_name_weight, soft_veto_penalty FROM v_weight_scheme"
    ).fetchall()
    assert [tuple(r) for r in scheme] == [("score_proportional", 30, 0.1, 15.0)]

    comps = {
        r["score_type"]: r["weight"]
        for r in conn.execute("SELECT score_type, weight FROM v_weight_component")
    }
    assert comps == {
        "FUNDAMENTAL": 0.4,
        "VALORIZATION": 0.3,
        "TECHNICAL": 0.2,
        "SEMANTIC": 0.1,
    }


def test_universe_membership_lifecycle() -> None:
    conn = _legacy_conn()
    kg_schema.ensure(conn)
    # AAPL(1), MSFT(2) present
    opened, closed = queries.reconcile(
        conn, "SP500", {1, 2}, as_of="2026-01-01", source="wikipedia"
    )
    assert (opened, closed) == (2, 0)
    # MSFT leaves, NVDA(3) joins
    conn.execute("INSERT INTO assets (ticker) VALUES ('NVDA')")
    conn.commit()
    opened, closed = queries.reconcile(
        conn, "SP500", {1, 3}, as_of="2026-04-01", source="wikipedia"
    )
    assert (opened, closed) == (1, 1)
    rows = {
        (r["asset_id"], r["valid_from"], r["valid_to"])
        for r in conn.execute("SELECT asset_id, valid_from, valid_to FROM universe_membership")
    }
    assert rows == {
        (1, "2026-01-01", None),
        (2, "2026-01-01", "2026-04-01"),
        (3, "2026-04-01", None),
    }
    # MSFT rejoins -> a fresh open stint, old one stays closed
    queries.reconcile(conn, "SP500", {1, 2, 3}, as_of="2026-07-01", source="wikipedia")
    msft = conn.execute(
        "SELECT valid_from, valid_to FROM universe_membership WHERE asset_id = 2 "
        "ORDER BY valid_from"
    ).fetchall()
    assert [tuple(r) for r in msft] == [("2026-01-01", "2026-04-01"), ("2026-07-01", None)]


# -- T-110: the price-spine guard's shared helper ----------------------------


def test_last_price_date_and_stale_as_of_reason(memory_quant_db: Database) -> None:
    conn = memory_quant_db
    assert queries.last_price_date(conn) is None  # no rows yet -- nothing to guard
    assert queries.stale_as_of_reason(conn, "2026-01-01") is None

    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    conn.execute(
        "INSERT INTO price_daily (asset_id, date, open, high, low, close, volume) "
        "VALUES (1, '2026-06-30', 100, 101, 99, 100, 1000000)"
    )
    conn.commit()

    assert queries.last_price_date(conn) == "2026-06-30"
    assert queries.stale_as_of_reason(conn, "2026-06-29") is None  # earlier -- safe
    assert queries.stale_as_of_reason(conn, "2026-06-30") is None  # equal -- safe
    reason = queries.stale_as_of_reason(conn, "2026-07-01")
    assert reason is not None
    assert "2026-07-01" in reason
    assert "2026-06-30" in reason


def test_last_price_date_tolerates_a_database_with_no_price_daily_table(
    memory_db: Database,
) -> None:
    """A DB that never ran pricing_agent (e.g. cycle's own test schema) has no price_daily
    table at all -- a different problem than a stale as_of, so this stays silent (T-110)."""
    assert queries.last_price_date(memory_db) is None
    assert queries.stale_as_of_reason(memory_db, "2026-01-01") is None


# -- T-116 (PR #95 review): the Ring-1 gate-version guard's shared helper ----------------------


def _dq_issue(conn: Database, gate_version: str) -> None:
    conn.execute("INSERT OR IGNORE INTO assets (id, ticker) VALUES (1, 'AAA')")
    row = conn.execute("SELECT id FROM sec_filings WHERE asset_id = 1").fetchone()
    fid = (
        row[0]
        if row is not None
        else conn.execute(
            "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, period_end, "
            "filing_date, available_at, retrieved_at) VALUES (1, '10-K', 2025, 'FY2025', "
            "'2025-12-31', '2026-01-30', '2026-02-02', '2026-02-01T00:00:00Z') RETURNING id"
        ).fetchone()[0]
    )
    conn.execute(
        "INSERT INTO data_quality_issue (filing_id, asset_id, metric_group, metric_name, "
        "metric_engine_version, rule_id, severity, quarantined, gate_version, created_at) "
        "VALUES (?, 1, 'leverage', 'debt_to_equity', 'metrics-v1', 'DQ_NEG_EQUITY', 'SOFT', 1, "
        "?, '2026-01-01T00:00:00Z')",
        (fid, gate_version),
    )
    conn.commit()


def test_stale_gate_version_reason_is_none_with_no_issues_at_all(memory_db: Database) -> None:
    """Ring-1 has simply never run -- a bootstrap situation, not a version regression."""
    assert queries.stale_gate_version_reason(memory_db, "dq-v2") is None


def test_stale_gate_version_reason_is_none_once_the_current_version_has_rows(
    memory_db: Database,
) -> None:
    _dq_issue(memory_db, "dq-v1")
    _dq_issue(memory_db, "dq-v2")
    assert queries.stale_gate_version_reason(memory_db, "dq-v2") is None


def test_stale_gate_version_reason_names_the_older_version_left_behind(
    memory_db: Database,
) -> None:
    _dq_issue(memory_db, "dq-v1")
    reason = queries.stale_gate_version_reason(memory_db, "dq-v2")
    assert reason is not None
    assert "dq-v1" in reason
    assert "dq-v2" in reason
