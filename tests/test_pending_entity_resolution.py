"""``entity_resolution pending`` (T-017): a stored edge set is stale against the news, read-only."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from conftest import write_universe_db
from test_entity_resolution import _NEWS_SCHEMA, _build_news_db

from entity_resolution import cli, news_db, pending, pipeline
from entity_resolution.config import Settings
from entity_resolution.pipeline import RunParams
from kg_schema import connect_ro

D = "2026-10-02"
TICKERS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"]


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()  # noqa: S324


class Fx:
    def __init__(self, tmp_path: Path) -> None:
        self.news = tmp_path / "urls.db"
        _build_news_db(self.news)
        self.settings = Settings(
            db_path=tmp_path / "kg.db",
            news_db_path=self.news,
            universe_db_path=write_universe_db(
                tmp_path / "universe.db", [(t, "2020-01-01", None) for t in TICKERS]
            ),
        )
        seed = sqlite3.connect(self.settings.db_path)
        seed.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT NOT NULL UNIQUE)")
        seed.commit()
        seed.close()

    def build(self, date: str = D) -> None:
        pipeline.run(self.settings, RunParams(analysis_date=date))

    def line(self, date: str = D) -> str:
        return pending.pending_line(self.settings, date)

    def sql(self, statement: str, *params: object, news: bool = False) -> None:
        path = self.news if news else self.settings.db_path
        conn = sqlite3.connect(path)
        conn.execute(statement, params)
        conn.commit()
        conn.close()


@pytest.fixture
def fx(tmp_path: Path) -> Fx:
    return Fx(tmp_path)


def test_no_news_source_is_not_pending(fx: Fx) -> None:
    missing = fx.settings.model_copy(update={"news_db_path": fx.news.parent / "nope.db"})
    line = pending.pending_line(missing, D)
    assert line == f"pending: no no news source configured ({fx.news.parent / 'nope.db'} not found)"


def test_a_fresh_database_is_pending(fx: Fx) -> None:
    assert fx.line().startswith("pending: yes")  # no cycle_run table / file
    fx.build("2026-09-01")
    fx.sql("DELETE FROM cycle_run")
    assert fx.line() == "pending: yes no ENTITY_RESOLUTION run recorded"


def test_the_run_records_the_watermark_and_a_repeat_check_says_no(fx: Fx) -> None:
    fx.build()
    params = json.loads(
        sqlite3.connect(fx.settings.db_path)
        .execute("SELECT params_json FROM cycle_run WHERE cycle_type = 'ENTITY_RESOLUTION'")
        .fetchone()[0]
    )
    assert set(params["news_watermark"]) == {"n_urls", "max_url_id", "max_entity_id"}
    line = fx.line()
    assert line.startswith("pending: no run 1 for 2026-10-02 saw the current news")


def test_a_run_from_before_t017_has_no_watermark_and_is_pending(fx: Fx) -> None:
    fx.build()
    fx.sql("UPDATE cycle_run SET params_json = json_remove(params_json, '$.news_watermark')")
    assert "built before T-017" in fx.line()


def test_a_killed_build_is_pending(fx: Fx) -> None:
    fx.build()
    fx.sql("UPDATE cycle_run SET status = 'running'")
    assert fx.line() == "pending: yes run 1 is running, not completed"


def test_edges_built_for_another_date_are_pending(fx: Fx) -> None:
    fx.build("2026-09-30")
    assert fx.line(D) == "pending: yes run 1 was built for 2026-09-30, not 2026-10-02"


def test_other_tuning_than_the_orchestrators_is_pending(fx: Fx) -> None:
    pipeline.run(fx.settings, RunParams(min_weight=1.0, analysis_date=D))
    assert "other tuning" in fx.line()


def test_an_edge_written_by_another_run_is_pending(fx: Fx) -> None:
    fx.sql("INSERT INTO assets (ticker) VALUES ('AAA'), ('BBB')")
    fx.build()
    fx.sql(
        "INSERT INTO shared_executive_edge (asset_id_a, asset_id_b, person_name, article_count_a, "
        "article_count_b, weight, method, computed_at, run_id) "
        "VALUES (1, 2, 'X', 3, 3, 3, ?, 'now', 99)",
        pending.METHOD,
    )
    assert "written by a run other than 1" in fx.line()


def test_a_new_article_makes_it_pending(fx: Fx) -> None:
    fx.build()
    fx.sql(
        "INSERT INTO discovered_urls (url, domain, company, ticker, source, discovered_at, "
        "pub_date) VALUES ('u', 'x', 'AAA', 'AAA', 's', 'now', '2026-09-30')",
        news=True,
    )
    assert "the news changed since run 1" in fx.line()


def test_ner_added_later_to_an_article_already_counted_makes_it_pending(fx: Fx) -> None:
    fx.build()
    fx.sql(
        "INSERT INTO article_entities (article_id, entity_type, text, start_char, end_char, "
        "score, model_name, processed_at) VALUES (1, 'PER', 'Late Person', 0, 5, 0.9, 'm', 'now')",
        news=True,
    )
    assert "the news changed since run 1" in fx.line()


def test_no_lookahead_an_article_after_the_date_or_without_one_does_not_move_the_count(
    fx: Fx,
) -> None:
    fx.build()
    for pub in ("2026-10-03", None):
        fx.sql(
            "INSERT INTO discovered_urls (url, domain, company, ticker, source, discovered_at, "
            "pub_date) VALUES ('u', 'x', 'AAA', 'AAA', 's', 'now', ?)",
            pub,
            news=True,
        )
    recorded = json.loads(
        sqlite3.connect(fx.settings.db_path)
        .execute("SELECT params_json FROM cycle_run")
        .fetchone()[0]
    )["news_watermark"]
    now = news_db.news_watermark(connect_ro(fx.news), TICKERS, until=D)
    assert (now["n_urls"], now["max_url_id"]) == (recorded["n_urls"], recorded["max_url_id"])
    assert fx.line().startswith("pending: no")


# -- the rowid guard ---------------------------------------------------------------------------


def _news_without_rowid_ids(path: Path, *, table: str) -> None:
    conn = sqlite3.connect(path)
    schema = _NEWS_SCHEMA
    if table == "article_entities":
        schema = schema.replace(
            "CREATE TABLE article_entities (\n    id INTEGER PRIMARY KEY AUTOINCREMENT,",
            "CREATE TABLE article_entities (\n    id INTEGER,",
        )
    else:
        schema = schema.replace(
            "CREATE TABLE discovered_urls (\n    id INTEGER PRIMARY KEY AUTOINCREMENT,",
            "CREATE TABLE discovered_urls (\n    id INTEGER,",
        )
    conn.executescript(schema)
    conn.commit()
    conn.close()


@pytest.mark.parametrize("table", ["article_entities", "discovered_urls"])
def test_an_id_that_is_not_a_rowid_key_is_pending_and_never_scanned(
    fx: Fx, tmp_path: Path, table: str
) -> None:
    odd = tmp_path / "odd.db"
    _news_without_rowid_ids(odd, table=table)
    ro = connect_ro(odd)
    seen: list[str] = []
    ro.raw.set_trace_callback(seen.append)
    assert news_db.news_watermark(ro, TICKERS, until=D) == {
        "unavailable": f"{table}.id is not a rowid key"
    }
    assert not any("MAX(id)" in s for s in seen)  # the guard answers before any MAX(id)
    odd_settings = fx.settings.model_copy(update={"news_db_path": odd})
    fx.build()  # a run against the normal db first, so the run row is complete
    line = pending.pending_line(odd_settings, D)
    assert line == f"pending: yes {table}.id is not a rowid key"


def test_the_rowid_shape_is_read_with_max_id(fx: Fx) -> None:
    ro = connect_ro(fx.news)
    assert news_db.id_is_rowid(ro, "article_entities") and news_db.id_is_rowid(
        ro, "discovered_urls"
    )
    seen: list[str] = []
    ro.raw.set_trace_callback(seen.append)
    news_db.news_watermark(ro, ["AAA"], until=D)
    assert any("MAX(id)" in s and "article_entities" in s for s in seen)


def test_the_watermark_never_names_the_articles_table_or_body_text(fx: Fx) -> None:
    ro = connect_ro(fx.news)
    seen: list[str] = []
    ro.raw.set_trace_callback(seen.append)
    news_db.news_watermark(ro, TICKERS, until=D)
    fx.build()
    pending.check(connect_ro(fx.settings.db_path), ro, TICKERS, D)
    joined = " ".join(seen).lower()
    assert "articles" not in joined and "body_text" not in joined


def test_the_check_writes_nothing(fx: Fx) -> None:
    fx.build()
    paths = (fx.settings.db_path, fx.settings.universe_db_path, fx.news)
    before = [_sha1(p) for p in paths]
    fx.line()
    assert [_sha1(p) for p in paths] == before


def test_the_subcommand_prints_exactly_one_line(
    fx: Fx, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fx.build()
    # Settings.load() calls load_dotenv() and requires KG_FINANCIAL_DB: no .env needed
    monkeypatch.setenv("KG_FINANCIAL_DB", str(fx.settings.db_path))
    monkeypatch.setenv("KG_UNIVERSE_DB", str(fx.settings.universe_db_path))
    monkeypatch.setenv("KG_NEWS_DB", str(fx.news))
    code = cli.main(
        [
            "pending",
            "--analysis-date",
            D,
            "--db",
            str(fx.settings.db_path),
            "--news-db",
            str(fx.news),
            "--universe-db",
            str(fx.settings.universe_db_path),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0 and out.count("\n") == 1 and out.startswith("pending: no run 1")
