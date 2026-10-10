"""m011 (T-070, contract 11): the priceobs-v2 columns and ``veto.expires_on``, on a database that
is at schema version 10 and holds rows."""

from __future__ import annotations

from portfolio_common.db import Database

from fundamental_agent import db as fundamental_db
from kg_schema import ensure, migrations, queries
from kg_schema.ddl import REQUIRED_COLUMNS
from kg_schema.views import VIEWS, ensure_views
from pricing_agent import db as pricing_db

NEW_PRICE_COLUMNS = ("sma_200", "ret_5d", "vol_5d", "mu_60d_base", "vol_60d_base")
NEW_VETO_COLUMNS = ("expires_on", "expiry_history_json")


def _version_10_database(conn: Database) -> Database:
    """What a migrated pre-T-070 database looks like: schema_version 10, neither table has the
    new columns, no view mentions them, and both tables hold rows."""
    fundamental_db.ensure_schema(conn)
    pricing_db.ensure_schema(conn)
    ensure(conn, run_migrations=True)
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    conn.execute(
        "INSERT INTO rule_catalog (rule_id, description, severity, enabled, created_at) "
        "VALUES ('R1', 'x', 'HARD', 1, '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO price_observation (asset_id, obs_date, close, event_time, computed_at, "
        "engine_version) VALUES (1, '2026-07-30', 100.0, '2026-07-30', '2026-07-30T00:00:00Z', "
        "'priceobs-v1')"
    )
    conn.execute(
        "INSERT INTO veto (asset_id, rule_id, severity, raised_on, last_seen_on, detected_at) "
        "VALUES (1, 'R1', 'HARD', '2026-07-01', '2026-07-01', '2026-07-01T00:00:00Z')"
    )
    conn.execute("DROP VIEW v_price_observation")
    conn.execute("DROP VIEW v_veto")
    for column in NEW_PRICE_COLUMNS:
        conn.execute(f"ALTER TABLE price_observation DROP COLUMN {column}")
    for column in NEW_VETO_COLUMNS:
        conn.execute(f"ALTER TABLE veto DROP COLUMN {column}")
    conn.execute("DELETE FROM schema_version WHERE version = 11")
    conn.commit()
    ensure_views(conn)  # at version 10 the code of that day built the views without them
    return conn


def test_the_fixture_is_a_version_10_database(memory_db: Database) -> None:
    conn = _version_10_database(memory_db)
    assert queries.current_version(conn) == 10
    assert not set(NEW_PRICE_COLUMNS) & set(conn.table_columns("price_observation"))
    assert not set(NEW_VETO_COLUMNS) & set(conn.table_columns("veto"))


def test_m011_adds_the_columns_keeps_the_rows_and_leaves_them_null(memory_db: Database) -> None:
    conn = _version_10_database(memory_db)
    assert migrations.apply_migrations(conn) == [11]
    assert queries.current_version(conn) == 11
    assert set(NEW_PRICE_COLUMNS) <= set(conn.table_columns("price_observation"))
    assert set(NEW_VETO_COLUMNS) <= set(conn.table_columns("veto"))
    obs = conn.execute("SELECT * FROM price_observation").fetchone()
    assert obs["close"] == 100.0 and obs["engine_version"] == "priceobs-v1"
    assert all(obs[c] is None for c in NEW_PRICE_COLUMNS)
    veto = conn.execute("SELECT * FROM veto").fetchone()
    assert (veto["raised_on"], veto["cleared_on"]) == ("2026-07-01", None)
    assert veto["expires_on"] is None and veto["expiry_history_json"] is None  # not temporal


def test_m011_is_idempotent(memory_db: Database) -> None:
    conn = _version_10_database(memory_db)
    assert migrations.apply_migrations(conn) == [11]
    assert migrations.apply_migrations(conn) == []
    migrations._m011_temporal_vetoes(conn)  # running its body again changes nothing either
    assert [c for c in conn.table_columns("veto") if c == "expires_on"] == ["expires_on"]


def test_the_views_gain_the_new_columns_at_the_end_after_the_migration(
    memory_db: Database,
) -> None:
    conn = _version_10_database(memory_db)
    before = {v: [r[1] for r in conn.execute(f"PRAGMA table_info({v})")] for v in VIEWS}
    assert "sma_200" not in before["v_price_observation"] and "expires_on" not in before["v_veto"]
    ensure(conn, run_migrations=True)
    after = {v: [r[1] for r in conn.execute(f"PRAGMA table_info({v})")] for v in VIEWS}
    assert (
        after["v_price_observation"][: len(before["v_price_observation"])]
        == before["v_price_observation"]
    )
    assert after["v_price_observation"][-5:] == list(NEW_PRICE_COLUMNS)
    assert after["v_veto"][: len(before["v_veto"])] == before["v_veto"]
    assert after["v_veto"][-1] == "expires_on"
    assert "expiry_history_json" not in after["v_veto"]  # bookkeeping, not contract
    assert queries.current_version(conn) == 11
    row = conn.execute("SELECT expires_on FROM v_veto").fetchone()
    assert row["expires_on"] is None
    assert conn.execute("SELECT sma_200 FROM v_price_observation").fetchone()["sma_200"] is None


def test_a_fresh_database_has_the_columns_without_a_migration(memory_db: Database) -> None:
    fundamental_db.ensure_schema(memory_db)
    pricing_db.ensure_schema(memory_db)
    assert set(NEW_PRICE_COLUMNS) <= set(memory_db.table_columns("price_observation"))
    assert set(NEW_VETO_COLUMNS) <= set(memory_db.table_columns("veto"))


def test_the_required_columns_name_exactly_what_m011_adds() -> None:
    assert tuple(REQUIRED_COLUMNS["price_observation"]) == NEW_PRICE_COLUMNS
    assert tuple(REQUIRED_COLUMNS["veto"]) == NEW_VETO_COLUMNS
    assert max(v for v, _, _ in migrations.MIGRATIONS) == 11
