"""Metric-version selection (T-090): the resolver, its SQL filter, and the no-bypass guard.

``fundamental_metrics`` is append-only per ``engine_version``, so parallel versions accumulate.
These tests pin how a consumer chooses among them -- and that no reader can skip choosing.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
from portfolio_common.db import Database

from fundamental_agent.metrics import CORE_GROUPS, OPTIONAL_GROUPS
from kg_schema import apply_migrations, queries
from kg_schema.versions import (
    FAMILY_RANK,
    METRIC_GROUPS,
    VERSION_FILTER_SQL,
    MetricVersions,
    VersionError,
    choose_versions,
    manifest_tag,
    parse_metric_selection,
    resolve_metric_versions,
    sort_versions,
    version_key,
)

SRC = Path(__file__).parent.parent / "src"

# -- ordering --------------------------------------------------------------------------------


def test_versions_sort_by_family_rank_then_number_not_lexically() -> None:
    shuffled = ["metrics-v10", "pre-v1", "metrics-v2", "metrics-v1"]
    assert sort_versions(shuffled) == ["pre-v1", "metrics-v1", "metrics-v2", "metrics-v10"]
    # a plain string sort would have picked the wrong "newest"
    assert sorted(shuffled)[-1] == "pre-v1"


@pytest.mark.parametrize("bad", ["metrics", "metrics-1", "v2", "metrics-vX", "", "metrics-v"])
def test_an_unparseable_version_is_an_error_not_a_guess(bad: str) -> None:
    with pytest.raises(VersionError, match="unparseable"):
        version_key(bad)


def test_an_unregistered_family_is_an_error() -> None:
    with pytest.raises(VersionError, match="unknown engine-version family"):
        version_key("mystery-v1")
    assert set(FAMILY_RANK) == {"pre", "metrics"}


def test_the_group_list_matches_what_the_agent_writes() -> None:
    """kg_schema may not import an agent, so it keeps its own list; this pins the two together."""
    assert set(METRIC_GROUPS) == {*CORE_GROUPS, *OPTIONAL_GROUPS, "valuation"}


# -- choosing --------------------------------------------------------------------------------

PRESENT = {
    "profitability": ["metrics-v1", "metrics-v2"],
    "valuation": ["metrics-v1", "metrics-v2"],
    "leverage": ["metrics-v1"],
}


def test_the_default_is_the_newest_stored_per_group() -> None:
    chosen = choose_versions(PRESENT)
    assert chosen.manifest() == {
        "leverage": "metrics-v1",
        "profitability": "metrics-v2",
        "valuation": "metrics-v2",
    }


def test_an_explicit_version_selects_it_for_every_group_the_consumer_reads() -> None:
    chosen = choose_versions(PRESENT, "metrics-v1", groups=("profitability", "valuation"))
    assert chosen.manifest() == {"profitability": "metrics-v1", "valuation": "metrics-v1"}


def test_a_per_group_mix_names_some_and_defaults_the_rest() -> None:
    chosen = choose_versions(PRESENT, {"valuation": "metrics-v1"})
    assert chosen.get("valuation") == "metrics-v1"
    assert chosen.get("profitability") == "metrics-v2"  # not named -> newest
    assert chosen.get("leverage") == "metrics-v1"


def test_a_version_that_is_not_stored_is_an_error_listing_what_is_stored() -> None:
    with pytest.raises(VersionError, match=r"'metrics-v3' is not stored .*profitability.*v1.*v2"):
        choose_versions(PRESENT, {"profitability": "metrics-v3"})
    # leverage has only v1 -- asking every group the consumer reads for v2 must not quietly skip it
    with pytest.raises(VersionError, match="leverage"):
        choose_versions(PRESENT, "metrics-v2", groups=("profitability", "leverage", "valuation"))


def test_naming_a_group_the_consumer_does_not_read_is_rejected() -> None:
    with pytest.raises(VersionError, match="not read by this consumer"):
        choose_versions(PRESENT, {"profitability": "metrics-v1"}, groups=("valuation",))


def test_an_unknown_group_is_rejected() -> None:
    with pytest.raises(VersionError, match="unknown metric group"):
        choose_versions(PRESENT, {"vibes": "metrics-v1"})


def test_an_empty_database_resolves_to_nothing_but_a_request_for_a_version_errors() -> None:
    assert choose_versions({}).manifest() == {}  # a purged DB is not an error by default
    with pytest.raises(VersionError, match="not stored for any group"):
        choose_versions({}, "metrics-v2")
    with pytest.raises(VersionError, match="not stored for group 'profitability'"):
        choose_versions(PRESENT, "metrics-v9")  # exists nowhere, and groups do have other versions


def test_one_version_skips_a_group_with_no_rows_but_not_one_that_lacks_that_version() -> None:
    """PRESENT holds no rows for e.g. liquidity: a single explicit version must not fail on it."""
    chosen = choose_versions(PRESENT, "metrics-v1")
    assert chosen.manifest() == {
        "leverage": "metrics-v1",
        "profitability": "metrics-v1",
        "valuation": "metrics-v1",
    }
    # ...but a per-group request is strict: naming a group that has nothing stored is an error
    with pytest.raises(VersionError, match="liquidity"):
        choose_versions(PRESENT, {"liquidity": "metrics-v1"})


# -- parsing a user's text ---------------------------------------------------------------------


@pytest.mark.parametrize(("text", "expected"), [(None, None), ("", None), ("  ", None)])
def test_blank_means_no_selection(text: str | None, expected: None) -> None:
    assert parse_metric_selection(text) is expected


def test_a_single_version_and_pairs_parse() -> None:
    assert parse_metric_selection(" metrics-v2 ") == "metrics-v2"
    assert parse_metric_selection("valuation=metrics-v1, profitability=metrics-v2") == {
        "valuation": "metrics-v1",
        "profitability": "metrics-v2",
    }


@pytest.mark.parametrize(
    "bad", ["valuation=", "=metrics-v1", "valuation=metrics-v1,valuation=metrics-v2", "metrics-9"]
)
def test_a_malformed_selection_fails_before_any_query(bad: str) -> None:
    with pytest.raises(VersionError):
        parse_metric_selection(bad)


# -- the manifest tag -------------------------------------------------------------------------


def test_the_tag_is_deterministic_order_independent_and_input_sensitive() -> None:
    a = {"consumer": "quant", "metrics": {"valuation": "metrics-v2"}, "returns": "qret-v2"}
    reordered = {"returns": "qret-v2", "metrics": {"valuation": "metrics-v2"}, "consumer": "quant"}
    assert manifest_tag(a) == manifest_tag(reordered)
    assert re.fullmatch(r"[0-9a-f]{8}", manifest_tag(a))
    assert manifest_tag(a) != manifest_tag({**a, "metrics": {"valuation": "metrics-v1"}})
    assert manifest_tag(a) != manifest_tag({**a, "returns": "qret-v3"})


# -- the SQL filter, against a real table -----------------------------------------------------


def _seed_metric_versions(conn: Database) -> None:
    # The base DDL keys a metric on (filing, group, name); it is the gated `migrate` step that
    # adds engine_version to the key so parallel versions can coexist -- as in production.
    apply_migrations(conn)
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    conn.execute(
        "INSERT INTO sec_filings (id, asset_id, form, fiscal_year, fiscal_period, period_end, "
        "retrieved_at) VALUES (1, 1, '10-K', 2025, 'FY2025', '2025-12-31', '2026-01-01T00:00:00Z')"
    )
    for group, name in (("valuation", "market_capitalization"), ("profitability", "net_margin")):
        for version, value in (("metrics-v1", 1.0), ("metrics-v2", 2.0)):
            conn.execute(
                "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, "
                "unit, computed_at, engine_version) VALUES (1, ?, ?, ?, 'x', "
                "'2026-01-01T00:00:00Z', ?)",
                (group, name, value, version),
            )
    conn.commit()


def _read(conn: Database, versions: MetricVersions) -> dict[str, float]:
    rows = conn.execute(
        f"SELECT m.metric_group AS g, m.value AS v FROM fundamental_metrics m "  # noqa: S608
        f"WHERE {VERSION_FILTER_SQL}",
        (versions.json_param(),),
    ).fetchall()
    return {str(r["g"]): float(r["v"]) for r in rows}


def test_the_filter_returns_exactly_the_resolved_versions(memory_db: Database) -> None:
    _seed_metric_versions(memory_db)
    present = {k: sorted(v) for k, v in queries.metric_versions_present(memory_db).items()}
    assert present == {
        "valuation": ["metrics-v1", "metrics-v2"],
        "profitability": ["metrics-v1", "metrics-v2"],
    }

    newest = resolve_metric_versions(memory_db)
    assert _read(memory_db, newest) == {
        "valuation": 2.0,
        "profitability": 2.0,
    }  # v2 only, once each

    v1 = resolve_metric_versions(memory_db, "metrics-v1")
    assert _read(memory_db, v1) == {"valuation": 1.0, "profitability": 1.0}

    mixed = resolve_metric_versions(memory_db, {"valuation": "metrics-v1"})
    assert _read(memory_db, mixed) == {"valuation": 1.0, "profitability": 2.0}


def test_an_empty_selection_reads_nothing_rather_than_everything(memory_db: Database) -> None:
    _seed_metric_versions(memory_db)
    assert _read(memory_db, MetricVersions({})) == {}


def test_versions_with_a_null_engine_version_are_ignored(memory_db: Database) -> None:
    """Only an un-migrated database can hold one (migrating makes the column NOT NULL): such a
    row predates versioning and cannot be selected by version, so it is not listed."""
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    memory_db.execute(
        "INSERT INTO sec_filings (id, asset_id, form, fiscal_year, fiscal_period, period_end, "
        "retrieved_at) VALUES (1, 1, '10-K', 2025, 'FY2025', '2025-12-31', '2026-01-01T00:00:00Z')"
    )
    memory_db.execute(
        "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, unit, "
        "computed_at, engine_version) VALUES (1, 'leverage', 'debt_to_equity', 9.0, 'x', "
        "'2026-01-01T00:00:00Z', NULL)"
    )
    memory_db.commit()
    assert queries.metric_versions_present(memory_db) == {}


def test_a_database_without_the_table_has_no_versions() -> None:
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    assert queries.metric_versions_present(Database(raw)) == {}


# -- the no-bypass guard ----------------------------------------------------------------------

# Files that legitimately read ``fundamental_metrics`` without the version filter, and why.
_UNFILTERED_OK = {
    "fundamental_agent/db.py": "the writer's own TTM lookup, pinned to the engine version it writes",
    "kg_schema/queries.py": "the version listing itself, and existence-only coverage checks",
    "kg_schema/migrations.py": "schema rebuilds copy the whole table",
}
_READ = re.compile(r"\b(?:FROM|JOIN)\s+fundamental_metrics\b", re.IGNORECASE)


def test_every_reader_of_fundamental_metrics_applies_the_version_filter() -> None:
    """A reader that joins the table unfiltered reads every engine version at once and lets
    row order pick the winner -- the defect T-090 exists to prevent. This fails on any new
    reader that skips the resolver, so it cannot be reintroduced quietly."""
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in _UNFILTERED_OK:
            continue
        text = path.read_text()
        reads = len(_READ.findall(text))
        if reads and text.count(VERSION_FILTER_SQL) < reads:
            offenders.append(f"{rel}: {reads} read(s), {text.count(VERSION_FILTER_SQL)} filtered")
    assert not offenders, "unfiltered fundamental_metrics reader(s): " + "; ".join(offenders)


def test_the_known_readers_are_actually_covered_by_that_guard() -> None:
    """Guard the guard: the two consumer modules must really be scanned and really filtered."""
    for rel, reads in (("cycle/data.py", 2), ("quant/db.py", 1)):
        text = (SRC / rel).read_text()
        assert len(_READ.findall(text)) == reads, rel
        assert text.count(VERSION_FILTER_SQL) == reads, rel


def test_no_view_reads_fundamental_metrics_on_its_own() -> None:
    """A view that resolved "latest" itself would be a second, hidden resolver."""
    assert "fundamental_metrics" not in (SRC / "kg_schema" / "views.py").read_text()


def test_the_allow_list_only_names_files_that_exist() -> None:
    for rel in _UNFILTERED_OK:
        assert (SRC / rel).exists(), rel
    assert json.loads(MetricVersions({"valuation": "metrics-v2"}).json_param()) == [
        "valuation/metrics-v2"
    ]
