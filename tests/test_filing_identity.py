"""T-149: a filing belongs to one company, identified by its CIK, and an original filing is final.

Covers APP-10a (fetch by CIK, never by ticker), the write-once ``assets.cik``, the database
invariant (every asset with filings has a CIK; one accession spans only assets of one CIK), PER-02
(amendments are never ingested and never replace an original) and the filer check that needs the
gateway's ``cik`` (portfolio-data-mining T-045). The gateway rows are real captures
(``fixtures/edgar_filing_identity_responses.json``).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from conftest import cik_for, write_universe_db
from portfolio_common.db import Database
from test_pipeline_multi_filing import (
    Q1_JUN,
    Q2_OCT,
    _Edgar,
    _q1_payload,
    _settings,
    _StubAnalyst,
    _stz_q2_payload,
)

import kg_schema
from fundamental_agent import db, pipeline
from fundamental_agent.edgar_client import EdgarNotFoundError, FilingRef, _unwrap
from fundamental_agent.pipeline import MissingCikError, RunParams, _select_filings, _YearTask
from kg_schema import connect, identity, migrations, queries
from kg_schema.identity import CikConflict, IdentityViolation
from kg_schema.queries import UniverseMember
from pricing_agent import db as pricing_db

FIXTURES = Path(__file__).parent / "fixtures"
IDENTITY = json.loads(
    (Path(__file__).parent / "fixtures" / "edgar_filing_identity_responses.json").read_text()
)


def _member(symbol: str, cik: str | None) -> UniverseMember:
    return UniverseMember(
        symbol, f"{symbol} Inc.", cik, "Tech", "Software", None, None, None, "", None
    )


def _refs(key: str) -> list[FilingRef]:
    return [
        FilingRef(
            r["form"],
            r["filing_date"],
            r["accession_number"],
            r.get("cik"),
            r.get("period_of_report"),
        )
        for r in IDENTITY[key]["data"]
    ]


def _task(form: str = "10-K", year: int = 2024, cik: str = "0000007084") -> _YearTask:
    return _YearTask(1, "ADM", "ADM", form, year, cik)


# -- the asset's CIK is write-once ----------------------------------------------------------


@pytest.mark.parametrize("sync", [db.sync_universe, pricing_db.sync_universe])
def test_a_ticker_arriving_with_another_cik_is_refused_and_nothing_is_written(
    memory_db: Database, sync: Any
) -> None:
    sync(memory_db, [_member("XYZ", "0000000009"), _member("AAA", "0000000001")])
    with pytest.raises(CikConflict) as info:
        sync(memory_db, [_member("AAA", "0000000002"), _member("XYZ", "0000000009")])
    assert "AAA: stored CIK 0000000001, incoming CIK 0000000002" in str(info.value)
    rows = {r["ticker"]: r["cik"] for r in memory_db.execute("SELECT ticker, cik FROM assets")}
    assert rows == {"XYZ": "0000000009", "AAA": "0000000001"}


def test_every_conflicting_ticker_is_named(memory_db: Database) -> None:
    db.sync_universe(memory_db, [_member("AAA", "1"), _member("BBB", "2")])
    with pytest.raises(CikConflict) as info:
        db.sync_universe(memory_db, [_member("AAA", "3"), _member("BBB", "4")])
    assert "AAA" in str(info.value) and "BBB" in str(info.value)


@pytest.mark.parametrize("sync", [db.sync_universe, pricing_db.sync_universe])
def test_a_stored_cik_is_never_overwritten_or_emptied(memory_db: Database, sync: Any) -> None:
    sync(memory_db, [_member("AAA", "320193")])
    # a spelling of the same number is no change; neither is a member that carries no CIK
    sync(memory_db, [_member("AAA", "0000320193")])
    sync(memory_db, [_member("AAA", None)])
    sync(memory_db, [_member("AAA", "")])
    assert memory_db.execute("SELECT cik FROM assets").fetchone()["cik"] == "0000320193"


@pytest.mark.parametrize("sync", [db.sync_universe, pricing_db.sync_universe])
def test_a_missing_cik_is_filled_in_once_the_universe_has_it(
    memory_db: Database, sync: Any
) -> None:
    memory_db.execute("INSERT INTO assets (ticker, cik) VALUES ('AAA', '')")
    memory_db.execute("UPDATE assets SET cik = NULL")
    sync(memory_db, [_member("AAA", "320193")])
    assert memory_db.execute("SELECT cik FROM assets").fetchone()["cik"] == "0000320193"


def test_a_cik_is_normalized_and_a_malformed_one_refused() -> None:
    assert identity.normalize_cik(320193) == "0000320193"
    assert identity.normalize_cik(" ") is None and identity.normalize_cik(None) is None
    with pytest.raises(ValueError, match="not a CIK"):
        identity.normalize_cik("AAPL")


# -- the database invariant -----------------------------------------------------------------


def _asset(conn: Database, asset_id: int, ticker: str, cik: str | None) -> None:
    conn.execute("INSERT INTO assets (id, ticker, cik) VALUES (?, ?, ?)", (asset_id, ticker, cik))
    if cik is None:  # the fixture's default-CIK trigger filled it
        conn.execute("UPDATE assets SET cik = NULL WHERE id = ?", (asset_id,))


def _filing(conn: Database, asset_id: int, accession: str | None, period: str = "FY2023") -> int:
    return int(
        conn.execute(
            "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, filing_date, "
            "available_at, accession_number, period_end, retrieved_at) "
            "VALUES (?, '10-K', 2023, ?, '2024-02-02', '2024-02-05', ?, '2023-12-31', 'now') "
            "RETURNING id",
            (asset_id, period, accession),
        ).fetchone()[0]
    )


def test_a_filing_of_an_asset_with_no_cik_is_refused(memory_db: Database) -> None:
    _asset(memory_db, 1, "AAA", None)
    with pytest.raises(sqlite3.IntegrityError, match="the asset has no CIK"):
        _filing(memory_db, 1, "0000000001-24-000001")
    memory_db.execute("UPDATE assets SET cik = '' WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError, match="the asset has no CIK"):
        _filing(memory_db, 1, None)


def test_a_filing_cannot_move_to_an_asset_with_no_cik(memory_db: Database) -> None:
    _asset(memory_db, 1, "AAA", "0000000001")
    _asset(memory_db, 2, "BBB", None)
    fid = _filing(memory_db, 1, "0000000001-24-000001")
    with pytest.raises(sqlite3.IntegrityError, match="the asset has no CIK"):
        memory_db.execute("UPDATE sec_filings SET asset_id = 2 WHERE id = ?", (fid,))


def test_the_share_classes_of_one_issuer_share_an_accession_and_nobody_else_does(
    memory_db: Database,
) -> None:
    """Alphabet's two classes are one CIK and one 10-K; another company's asset may not take it."""
    alphabet = "0001652044-24-000022"
    _asset(memory_db, 1, "GOOGL", "0001652044")
    _asset(memory_db, 2, "GOOG", "0001652044")
    _asset(memory_db, 3, "META", "0001326801")
    _filing(memory_db, 1, alphabet)
    goog = _filing(memory_db, 2, alphabet)  # the same CIK: accepted
    with pytest.raises(sqlite3.IntegrityError, match="attached to an asset of another CIK"):
        _filing(memory_db, 3, alphabet)
    other = _filing(memory_db, 3, "0001326801-24-000012", period="FY2022")
    with pytest.raises(sqlite3.IntegrityError, match="attached to an asset of another CIK"):
        memory_db.execute(
            "UPDATE sec_filings SET accession_number = ? WHERE id = ?", (alphabet, other)
        )
    with pytest.raises(sqlite3.IntegrityError, match="attached to an asset of another CIK"):
        memory_db.execute("UPDATE sec_filings SET asset_id = 3 WHERE id = ?", (goog,))
    assert not identity.violations(memory_db)


def test_the_cik_of_an_asset_with_filings_cannot_change_or_empty(memory_db: Database) -> None:
    _asset(memory_db, 1, "AAA", "0000000001")
    _asset(memory_db, 2, "BBB", "0000000002")
    _filing(memory_db, 1, "0000000001-24-000001")
    for value in ("0000000009", None, ""):
        with pytest.raises(sqlite3.IntegrityError, match="cannot change"):
            memory_db.execute("UPDATE assets SET cik = ? WHERE id = 1", (value,))
    memory_db.execute("UPDATE assets SET cik = '0000000009' WHERE id = 2")  # no filings yet: fine
    memory_db.execute("UPDATE assets SET cik = '0000000001' WHERE id = 1")  # unchanged: fine


def test_an_empty_cik_can_be_filled_unless_it_splits_a_shared_accession(
    memory_db: Database,
) -> None:
    identity.drop_triggers(memory_db)  # legacy rows, written before T-149
    _asset(memory_db, 1, "AAA", None)
    _asset(memory_db, 2, "BBB", "0000000002")
    _filing(memory_db, 1, "0000000002-24-000001")
    _filing(memory_db, 2, "0000000002-24-000001")  # shared by an asset with no CIK: legacy
    identity.ensure_triggers(memory_db)
    with pytest.raises(sqlite3.IntegrityError, match="differs from an asset sharing"):
        memory_db.execute("UPDATE assets SET cik = '0000000001' WHERE id = 1")
    memory_db.execute("UPDATE assets SET cik = '0000000002' WHERE id = 1")


def test_rows_written_before_the_triggers_are_found_and_refuse_the_migration(
    memory_db: Database,
) -> None:
    identity.drop_triggers(memory_db)
    _asset(memory_db, 1, "AAA", None)
    _asset(memory_db, 2, "BBB", "0000000002")
    _asset(memory_db, 3, "CCC", "0000000003")
    _filing(memory_db, 1, "x-1")
    _filing(memory_db, 2, "x-2")
    _filing(memory_db, 3, "x-2")
    found = identity.violations(memory_db)
    assert found.no_cik == ["AAA"]
    assert found.cross_cik == ["x-2: BBB (0000000002), CCC (0000000003)"]
    with pytest.raises(IdentityViolation, match=r"AAA.*x-2"):
        identity.require(memory_db)
    memory_db.execute("DELETE FROM schema_version WHERE version >= 12")
    memory_db.commit()
    with pytest.raises(IdentityViolation):
        migrations.apply_migrations(memory_db)
    assert queries.current_version(memory_db) == 11  # no floor was recorded


def test_the_migration_installs_the_triggers_on_a_clean_database(memory_db: Database) -> None:
    kg_schema.ensure(memory_db, run_migrations=True)
    identity.drop_triggers(memory_db)
    memory_db.execute("DELETE FROM schema_version WHERE version >= 12")
    memory_db.commit()
    assert migrations.apply_migrations(memory_db) == [12, 13, 14, 15, 16, 17, 18]
    names = {r[0] for r in memory_db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {
        "trg_sf_cik_insert",
        "trg_sf_cross_cik_insert",
        "trg_assets_cik_keep",
    } <= names


def test_a_run_refuses_to_start_over_rows_that_break_the_invariant(tmp_path: Path) -> None:
    conn = connect(tmp_path / "kg.db")
    db.ensure_schema(conn)
    identity.drop_triggers(conn)
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    _filing(conn, 1, "0000000001-24-000001")
    conn.commit()
    conn.close()
    with pytest.raises(IdentityViolation, match="AAA"):
        pipeline.run(_settings(tmp_path), RunParams(analysis_date="2026-09-21"))


# -- the pipeline fetches by CIK ------------------------------------------------------------


class _CikEdgar(_Edgar):
    """The fake gateway, recording who each call named."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.financials_named: list[str] = []

    def financials(
        self, ticker: str, form: str, year: int, accession_number: str | None = None
    ) -> dict[str, Any]:
        self.financials_named.append(ticker)
        return super().financials(ticker, form, year, accession_number)


def _stz_gateway(listing: list[FilingRef] | None = None) -> _CikEdgar:
    return _CikEdgar(
        {("10-Q", 2023): listing or [Q2_OCT, Q1_JUN]},
        {Q1_JUN.accession_number: _q1_payload(), Q2_OCT.accession_number: _stz_q2_payload()},
    )


def _run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, edgar: _Edgar, **params: Any
) -> pipeline.RunReport:
    monkeypatch.setattr(pipeline, "EdgarClient", edgar)
    monkeypatch.setattr(pipeline, "build_model", lambda _s: None)
    monkeypatch.setattr(pipeline, "FundamentalAnalyst", _StubAnalyst)
    run_params = RunParams(
        forms=("10-Q",), since_year=2023, until_year=2023, analysis_date="2026-09-21", **params
    )
    return pipeline.run(_settings(tmp_path), run_params)


def test_every_gateway_call_names_the_cik_never_the_ticker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    edgar = _stz_gateway()
    report = _run(monkeypatch, tmp_path, edgar)
    assert (report.completed, report.failed) == (2, 0)
    assert edgar.listing_calls == [cik_for("STZ")]
    assert edgar.financials_named == [cik_for("STZ")] * 2
    assert all(len(c) == 10 and c.isdigit() for c in edgar.listing_calls + edgar.financials_named)


def test_an_asset_with_no_cik_is_refused_before_any_run_and_never_fetched_by_ticker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    udb = write_universe_db(tmp_path / "universe.db", [("STZ", "2020-01-01", None)])
    sqlite3.connect(udb).execute("UPDATE universe_membership SET cik = NULL").connection.commit()
    edgar = _stz_gateway()
    with pytest.raises(MissingCikError, match=r"STZ.*never by ticker"):
        _run(monkeypatch, tmp_path, edgar)
    assert edgar.listing_calls == [] and edgar.financials_named == []
    conn = sqlite3.connect(tmp_path / "kg.db")
    assert conn.execute("SELECT COUNT(*) FROM analysis_run").fetchone()[0] == 0


def test_a_gateway_with_no_company_for_the_cik_is_a_recorded_failure_not_a_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    edgar = _stz_gateway()
    edgar.listing_raises = EdgarNotFoundError("/filing_by_year -> Company not found")
    report = _run(monkeypatch, tmp_path, edgar)
    assert report.failed == 1 and report.skipped == 0
    assert "no EDGAR match for 'STZ' (CIK" in report.errors[0]


class _ByCikOnly(_CikEdgar):
    """The live gateway's behaviour for a company that left the index: only its CIK resolves."""

    def __init__(self, known_cik: str, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.known_cik = known_cik

    def filing_by_year(self, ticker: str, form: str, year: int) -> list[FilingRef]:
        if ticker != self.known_cik:
            raise EdgarNotFoundError(f"Company not found: {ticker!r}")
        return super().filing_by_year(ticker, form, year)


def test_a_company_that_left_the_index_is_ingested_by_its_cik(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Twitter (CIK 1418091): its ticker no longer resolves at the gateway (real capture), its CIK
    does. Its FY2021 10-K, filed 2022-02-16, is ingested under the CIK alone -- the path T-153
    takes for the 86 companies that left the index."""
    with pytest.raises(EdgarNotFoundError, match="Company not found"):
        _unwrap("/filing_by_year/TWTR", IDENTITY["filing_by_year_TWTR_10-K_2022"])
    payload = json.loads(
        (FIXTURES / "financials_TWTR_10-K_2022_acc-0001418091-22-000029.json").read_text()
    )["data"]
    (ref,) = _refs("filing_by_year_1418091_10-K_2022")
    edgar = _ByCikOnly("0001418091", {("10-K", 2022): [ref]}, {ref.accession_number: payload})
    write_universe_db(tmp_path / "universe.db", [("TWTR", "2020-01-01", None)])
    conn = sqlite3.connect(tmp_path / "universe.db")
    conn.execute("UPDATE universe_membership SET cik = '1418091'")
    conn.commit()
    conn.close()
    monkeypatch.setattr(pipeline, "EdgarClient", edgar)
    monkeypatch.setattr(pipeline, "build_model", lambda _s: None)
    monkeypatch.setattr(pipeline, "FundamentalAnalyst", _StubAnalyst)
    params = RunParams(
        forms=("10-K",), since_year=2022, until_year=2022, analysis_date="2026-09-21"
    )
    report = pipeline.run(_settings(tmp_path), params)
    assert (report.completed, report.failed) == (1, 0)
    assert edgar.listing_calls == ["0001418091"]
    stored = sqlite3.connect(tmp_path / "kg.db").execute(
        "SELECT a.ticker, a.cik, f.fiscal_period, f.period_end, f.accession_number "
        "FROM sec_filings f JOIN assets a ON a.id = f.asset_id"
    )
    assert stored.fetchall() == [
        ("TWTR", "0001418091", "FY2021", "2021-12-31", "0001418091-22-000029")
    ]


# -- amendments are never ingested and never replace an original (PER-02, L-04) ----------------


def test_a_10ka_never_replaces_the_original_of_its_year() -> None:
    """ADM's real 2024 listing: the 10-K/A (a restatement, filed in November) is listed first; the
    original 10-K of the same fiscal year, filed in March, is what the pipeline ingests."""
    listing = _refs("filing_by_year_ADM_10-K_2024")
    assert [r.form for r in listing] == ["10-K/A", "10-K"]
    assert listing[0].period_of_report == listing[1].period_of_report == "2023-12-31"
    picked = _select_filings(_task(), listing)
    assert [(r.form, r.accession_number) for r in picked] == [("10-K", "0000007084-24-000009")]


def test_a_year_with_only_an_amendment_ingests_nothing() -> None:
    amendment = FilingRef("10-K/A", "2023-09-01", "acc-amend")
    assert _select_filings(_task(), [amendment]) == []


def test_a_10qa_is_not_ingested_beside_its_quarter() -> None:
    original = FilingRef("10-Q", "2023-06-30", "acc-q1")
    amendment = FilingRef("10-Q/A", "2023-08-01", "acc-q1a")
    assert _select_filings(_task("10-Q", 2023), [amendment, original]) == [original]


def test_two_originals_filed_in_one_year_are_both_kept_oldest_first() -> None:
    """A late filer files two fiscal years' 10-Ks in one calendar year: neither is an amendment,
    so neither is dropped (the old rule kept only the newest)."""
    older = FilingRef("10-K", "2023-03-01", "acc-fy21")
    newer = FilingRef("10-K", "2023-09-01", "acc-fy22")
    assert _select_filings(_task(), [newer, older]) == [older, newer]


def test_a_run_over_an_amended_quarter_fetches_and_stores_only_the_original(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    amendment = FilingRef("10-Q/A", "2023-11-20", "0000016918-23-000999")
    edgar = _stz_gateway([amendment, Q2_OCT, Q1_JUN])
    report = _run(monkeypatch, tmp_path, edgar)
    assert (report.completed, report.failed, report.skipped) == (2, 0, 1)  # the amendment, skipped
    assert amendment.accession_number not in edgar.financials_calls
    conn = sqlite3.connect(tmp_path / "kg.db")
    stored = {r[0] for r in conn.execute("SELECT accession_number FROM sec_filings")}
    assert stored == {Q1_JUN.accession_number, Q2_OCT.accession_number}


def test_upsert_filing_refuses_to_replace_a_stored_original(memory_db: Database) -> None:
    _asset(memory_db, 1, "ADM", "0000007084")
    key = db.FilingKey(1, "10-K", 2023, "FY2023")
    original = db.FilingMeta("2024-03-12", "0000007084-24-000009", "2023-12-31")
    fid = db.upsert_filing(memory_db, key, original)
    assert db.upsert_filing(memory_db, key, original) == fid  # a re-run of the same filing
    amendment = db.FilingMeta("2024-11-18", "0000007084-24-000051", "2023-12-31")
    with pytest.raises(db.FilingReplacementRefused, match="original filing 0000007084-24-000009"):
        db.upsert_filing(memory_db, key, amendment)
    row = memory_db.execute("SELECT accession_number, filing_date FROM sec_filings").fetchone()
    assert (row["accession_number"], row["filing_date"]) == ("0000007084-24-000009", "2024-03-12")


def test_a_row_without_an_accession_takes_its_first_one(memory_db: Database) -> None:
    _asset(memory_db, 1, "ADM", "0000007084")
    key = db.FilingKey(1, "10-K", 2023, "FY2023")
    fid = db.upsert_filing(memory_db, key, db.FilingMeta(None, None, None))
    assert (
        db.upsert_filing(memory_db, key, db.FilingMeta("2024-03-12", "acc-1", "2023-12-31")) == fid
    )


# -- the filer check (needs the gateway's `cik`, portfolio-data-mining T-045) -----------------


def test_a_filing_listed_under_another_cik_is_not_stored_and_is_a_recorded_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    foreign = FilingRef("10-Q", "2023-10-05", Q2_OCT.accession_number, "0000789019", "2023-08-31")
    own = FilingRef(
        "10-Q", Q1_JUN.filing_date, Q1_JUN.accession_number, cik_for("STZ"), "2023-05-31"
    )
    edgar = _stz_gateway([foreign, own])
    report = _run(monkeypatch, tmp_path, edgar)
    assert (report.completed, report.failed) == (1, 1)  # the sibling filing is unaffected
    assert Q2_OCT.accession_number not in edgar.financials_calls  # never even fetched
    conn = sqlite3.connect(tmp_path / "kg.db")
    assert [r[0] for r in conn.execute("SELECT accession_number FROM sec_filings")] == [
        Q1_JUN.accession_number
    ]
    (error,) = conn.execute(
        "SELECT ticker, stage, message FROM analysis_run_error WHERE stage = 'identity'"
    ).fetchall()
    assert error[0] == "STZ"
    assert f"listed under CIK 0000789019, not STZ's {cik_for('STZ')}" in error[2]


def test_a_listing_without_a_cik_cannot_be_checked_and_is_stored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A gateway before T-045 sends no `cik`: nothing to compare, nothing refused."""
    report = _run(monkeypatch, tmp_path, _stz_gateway())
    assert (report.completed, report.failed) == (2, 0)


def test_the_real_listing_carries_the_filer_the_asset_expects() -> None:
    (apple,) = _refs("filing_by_year_0000320193_10-K_2024")
    assert apple.cik == "0000320193"
    assert apple.period_of_report == "2024-09-28"
