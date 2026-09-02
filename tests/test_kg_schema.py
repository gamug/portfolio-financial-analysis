"""Shared-schema DDL, migrations, views, and membership reconciliation."""

from __future__ import annotations

import sqlite3

import pytest

import kg_schema
from fundamental_agent.sections import _SPECS, canonical_item_label
from kg_schema import migrations, universe_membership, version
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


def _legacy_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_LEGACY_SQL)
    conn.execute("INSERT INTO assets (ticker, company_name) VALUES ('AAPL', 'Apple')")
    conn.execute("INSERT INTO assets (ticker, company_name) VALUES ('MSFT', 'Microsoft')")
    conn.execute(
        "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, period_end, "
        "accession_number, retrieved_at) VALUES (1, '10-K', 2023, 'FY2023', '2023-09-30', "
        "'0000320193-23-000106', '2024-01-01T00:00:00Z')"
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
def migrated_db() -> sqlite3.Connection:
    conn = _legacy_conn()
    kg_schema.ensure(conn, run_migrations=True)
    return conn


def test_ensure_is_idempotent_and_additive() -> None:
    conn = _legacy_conn()
    kg_schema.ensure(conn)
    kg_schema.ensure(conn)  # second call must not raise
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"score_snapshot", "universe_membership", "veto", "price_observation"} <= tables
    # additive columns present
    ff_cols = {r[1] for r in conn.execute("PRAGMA table_info(financial_facts)")}
    assert {"event_time", "filing_version"} <= ff_cols
    # additive-only path never advances the version
    assert version.current_version(conn) == 0


def test_migrations_rebuild_and_preserve_rows(migrated_db: sqlite3.Connection) -> None:
    conn = migrated_db
    assert version.current_version(conn) == 6
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


def test_migrations_are_a_noop_second_time(migrated_db: sqlite3.Connection) -> None:
    assert migrations.apply_migrations(migrated_db) == []


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
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
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
        INSERT INTO sec_filings (id, asset_id, form, fiscal_year, fiscal_period, retrieved_at)
        VALUES (1, 1, '10-K', 2023, 'FY2023', '2024-01-01T00:00:00Z');
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
    version.ensure(conn)
    version.record(conn, 4, "pretend floor")
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
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
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
    version.ensure(conn)
    version.record(conn, 5, "pretend floor")
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


def test_views_select_cleanly(migrated_db: sqlite3.Connection) -> None:
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
        "v_weight_scheme",
        "v_weight_component",
        "v_sector_aggregate_snapshot",
    ):
        migrated_db.execute(f"SELECT * FROM {name} LIMIT 1").fetchall()  # noqa: S608 - fixed view names


def test_v_sec_filing_is_one_row_per_filing(migrated_db: sqlite3.Connection) -> None:
    rows = migrated_db.execute(
        "SELECT ticker, form, fiscal_period, accession_number, period_end FROM v_sec_filing"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("AAPL", "10-K", "FY2023", "0000320193-23-000106", "2023-09-30")
    ]


def test_v_sec_filing_section_item_label_matches_python_vocab(
    migrated_db: sqlite3.Connection,
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


def test_v_sector_and_v_industry_roll_up_assets(migrated_db: sqlite3.Connection) -> None:
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


def test_v_rule_catalog_unpacks_threshold_params(migrated_db: sqlite3.Connection) -> None:
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


def test_weight_scheme_views_explode_the_blend(migrated_db: sqlite3.Connection) -> None:
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
    opened, closed = universe_membership.reconcile(
        conn, "SP500", {1, 2}, as_of="2026-01-01", source="wikipedia"
    )
    assert (opened, closed) == (2, 0)
    # MSFT leaves, NVDA(3) joins
    conn.execute("INSERT INTO assets (ticker) VALUES ('NVDA')")
    conn.commit()
    opened, closed = universe_membership.reconcile(
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
    universe_membership.reconcile(conn, "SP500", {1, 2, 3}, as_of="2026-07-01", source="wikipedia")
    msft = conn.execute(
        "SELECT valid_from, valid_to FROM universe_membership WHERE asset_id = 2 "
        "ORDER BY valid_from"
    ).fetchall()
    assert [tuple(r) for r in msft] == [("2026-01-01", "2026-04-01"), ("2026-07-01", None)]
