"""APP-10b (T-149): a successor registrant keeps its predecessor's filings.

ExxonMobil Holdings Corp (CIK 0002115436) became XOM's registrant on 2026-07-01; every filing from
2022 to 2026-06 was made by Exxon Mobil Corp (CIK 0000034088), and the 2026-08-03 10-Q is listed under
both. Fetched by the successor's CIK alone, XOM had one filing of 19. The succession table
(``kg_schema/data/cik_successions.csv``) names the predecessor and cites the 8-K12B that establishes
it. The listings are real captures (``fixtures/edgar_xom_succession_listings.json``); the statements
the fake gateway serves are minimal payloads built from each row's own ``period_of_report``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from conftest import write_universe_db
from portfolio_common.db import Database
from test_pipeline_multi_filing import _settings, _StubAnalyst

from fundamental_agent import db, pipeline
from fundamental_agent.edgar_client import EdgarNotFoundError, FilingRef
from fundamental_agent.pipeline import RunParams
from kg_schema import identity

SUCCESSOR, PREDECESSOR = "0002115436", "0000034088"
JOINT_10Q = "0000034088-26-000093"
LISTINGS = json.loads(
    (Path(__file__).parent / "fixtures" / "edgar_xom_succession_listings.json").read_text()
)


def _listing(cik: str, form: str, year: int) -> list[FilingRef]:
    return [
        FilingRef(
            r["form"], r["filing_date"], r["accession_number"], r["cik"], r["period_of_report"]
        )
        for r in LISTINGS[f"{cik}/{form}/{year}"]["data"]
    ]


def _payload(ref: FilingRef) -> dict[str, Any]:
    """Statements for *ref*'s own period, as small as the pipeline accepts: a 10-K's fiscal year and
    its prior; a 10-Q's quarter and its year-ago twin, with the balance sheet's year end beside."""
    assert ref.period_of_report
    end = date.fromisoformat(ref.period_of_report)
    prior = end.replace(year=end.year - 1)
    if ref.form == "10-K":
        cols = [f"{end} (FY)", f"{prior} (FY)"]
        instants = [prior.isoformat(), end.isoformat()]
    else:
        quarter = (end.month - 1) // 3 + 1
        cols = [f"{end} (Q{quarter})", f"{prior} (Q{quarter})"]
        instants = [date(end.year - 1, 12, 31).isoformat(), end.isoformat()]
    return {
        "income_statement": [
            {"concept": "us-gaap_Revenues", "label": "Revenues", **{c: 100.0 for c in cols}}
        ],
        "balance_sheet": [
            {"concept": "us-gaap_Assets", "label": "Total assets", **{i: 500.0 for i in instants}}
        ],
        "cash_flow": [],
    }


class _XomGateway:
    """The gateway as captured: listings per (CIK, form, year); statements for whichever filing."""

    def __init__(self) -> None:
        self.listing_calls: list[tuple[str, str, int]] = []
        self.financials_calls: list[tuple[str, str]] = []
        self._by_accession = {
            r.accession_number: r
            for key, v in LISTINGS.items()
            if key != "_comment"
            for r in _listing(*_key(key))
        }

    def __call__(self, *_a: object, **_k: object) -> _XomGateway:
        return self

    def __enter__(self) -> _XomGateway:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def filing_by_year(self, cik: str, form: str, year: int) -> list[FilingRef]:
        self.listing_calls.append((cik, form, year))
        if cik not in (SUCCESSOR, PREDECESSOR):
            raise EdgarNotFoundError(f"Company not found: {cik!r}")
        return _listing(cik, form, year)

    def financials(
        self, cik: str, form: str, year: int, accession_number: str | None = None
    ) -> dict[str, Any]:
        assert accession_number is not None
        self.financials_calls.append((cik, accession_number))
        return _payload(self._by_accession[accession_number])


def _key(key: str) -> tuple[str, str, int]:
    cik, form, year = key.split("/")
    return cik, form, int(year)


def _run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gateway: _XomGateway, forms: tuple[str, ...]
) -> pipeline.RunReport:
    udb = write_universe_db(tmp_path / "universe.db", [("XOM", "2020-01-01", None)])
    conn = sqlite3.connect(udb)
    conn.execute("UPDATE universe_membership SET cik = ?", (SUCCESSOR,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(pipeline, "EdgarClient", gateway)
    monkeypatch.setattr(pipeline, "build_model", lambda _s: None)
    monkeypatch.setattr(pipeline, "FundamentalAnalyst", _StubAnalyst)
    params = RunParams(forms=forms, since_year=2022, until_year=2026, analysis_date="2026-09-22")
    return pipeline.run(_settings(tmp_path), params)


# -- the table ------------------------------------------------------------------------------


def test_the_table_holds_the_one_succession_with_its_evidence() -> None:
    (row,) = identity.read_successions()
    assert (row["successor_cik"], row["predecessor_cik"]) == (SUCCESSOR, PREDECESSOR)
    assert row["effective_date"] == "2026-07-01"
    assert (row["evidence_form"], row["evidence_accession"]) == ("8-K12B", "0001193125-26-291990")
    assert "successor registrant" in row["evidence_quote"]
    assert "Rule 12g-3(a)" in row["evidence_quote"]


def test_a_row_without_its_evidence_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "s.csv"
    path.write_text(
        "successor_cik,predecessor_cik,effective_date,evidence_form,evidence_accession,evidence_quote\n"
        "2,1,2026-07-01,8-K12B,,\n"
    )
    with pytest.raises(ValueError, match="without its evidence"):
        identity.read_successions(path)


def test_the_database_is_seeded_once_and_the_chain_lists_the_predecessor(
    memory_db: Database,
) -> None:
    assert identity.seed_successions(memory_db) == 0  # ensure already seeded it
    rows = memory_db.execute("SELECT * FROM cik_succession").fetchall()
    assert [(r["successor_cik"], r["predecessor_cik"]) for r in rows] == [(SUCCESSOR, PREDECESSOR)]
    assert identity.cik_chain(memory_db, 2115436) == (SUCCESSOR, PREDECESSOR)
    assert identity.cik_chain(memory_db, "0000320193") == ("0000320193",)  # no succession
    assert identity.cik_chain(memory_db, "") == ()


# -- the pipeline fetches under every CIK of the chain ---------------------------------------


def test_xom_keeps_its_five_10ks_and_fourteen_10qs_and_the_joint_10q_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gateway = _XomGateway()
    report = _run(monkeypatch, tmp_path, gateway, ("10-K", "10-Q"))
    assert (report.completed, report.failed) == (19, 0)
    conn = sqlite3.connect(tmp_path / "kg.db")
    rows = conn.execute(
        "SELECT form, fiscal_period, period_end, accession_number, cik FROM sec_filings ORDER BY period_end"
    ).fetchall()
    assert [r[0] for r in rows].count("10-K") == 5
    assert [r[0] for r in rows].count("10-Q") == 14
    accessions = [r[3] for r in rows]
    assert len(accessions) == len(set(accessions)) == 19  # no duplicate of the joint 10-Q
    assert accessions.count(JOINT_10Q) == 1
    # each filing is stored under the CIK it was listed first under: the successor's own first
    by_acc = {r[3]: r[4] for r in rows}
    assert by_acc[JOINT_10Q] == SUCCESSOR
    assert {c for a, c in by_acc.items() if a != JOINT_10Q} == {PREDECESSOR}
    assert {r[1] for r in rows if r[0] == "10-K"} == {f"FY{y}" for y in range(2021, 2026)}


def test_each_filing_is_fetched_under_the_cik_it_was_listed_under(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gateway = _XomGateway()
    _run(monkeypatch, tmp_path, gateway, ("10-Q",))
    fetched = dict((a, c) for c, a in gateway.financials_calls)
    assert fetched[JOINT_10Q] == SUCCESSOR
    assert {c for a, c in fetched.items() if a != JOINT_10Q} == {PREDECESSOR}
    assert {c for c, _f, _y in gateway.listing_calls} == {SUCCESSOR, PREDECESSOR}
    assert len(gateway.financials_calls) == 14


def test_a_filing_listed_under_a_cik_outside_the_chain_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Foreign(_XomGateway):
        def filing_by_year(self, cik: str, form: str, year: int) -> list[FilingRef]:
            refs = super().filing_by_year(cik, form, year)
            if (cik, form, year) == (PREDECESSOR, "10-K", 2026):
                return [
                    FilingRef(
                        r.form, r.filing_date, r.accession_number, "0000789019", r.period_of_report
                    )
                    for r in refs
                ]
            return refs

    gateway = Foreign()
    report = _run(monkeypatch, tmp_path, gateway, ("10-K",))
    assert (report.completed, report.failed) == (4, 1)
    assert "listed under CIK 0000789019" in report.errors[0]
    assert "0000034088-26-000045" not in {a for _c, a in gateway.financials_calls}
    conn = sqlite3.connect(tmp_path / "kg.db")
    assert conn.execute("SELECT stage FROM analysis_run_error").fetchall() == [("identity",)]


# -- the database holds the same line --------------------------------------------------------


def _filing(conn: Database, asset_id: int, accession: str, cik: str | None) -> None:
    conn.execute(
        "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, filing_date, "
        "available_at, accession_number, period_end, retrieved_at, cik) "
        "VALUES (?, '10-K', 2023, 'FY2023', '2024-02-02', '2024-02-05', ?, '2023-12-31', 'now', ?)",
        (asset_id, accession, cik),
    )


def test_a_filing_cik_outside_the_asset_s_chain_aborts(memory_db: Database) -> None:
    memory_db.execute("INSERT INTO assets (id, ticker, cik) VALUES (1, 'XOM', ?)", (SUCCESSOR,))
    memory_db.execute("INSERT INTO assets (id, ticker, cik) VALUES (2, 'AAA', '0000000002')")
    _filing(memory_db, 1, "a-1", PREDECESSOR)  # the recorded predecessor: accepted
    memory_db.execute("DELETE FROM sec_filings")
    _filing(memory_db, 1, "a-2", SUCCESSOR)  # its own: accepted
    memory_db.execute("DELETE FROM sec_filings")
    _filing(memory_db, 1, "a-3", None)  # a row from before T-149: unchecked
    memory_db.execute("DELETE FROM sec_filings")
    for asset, cik in (
        (1, "0000789019"),
        (2, PREDECESSOR),
    ):  # any other CIK; XOM's predecessor on another asset
        with pytest.raises(
            sqlite3.IntegrityError, match="neither its asset's nor a recorded predecessor's"
        ):
            _filing(memory_db, asset, "a-4", cik)


def test_moving_a_filing_or_changing_its_cik_is_checked_too(memory_db: Database) -> None:
    memory_db.execute("INSERT INTO assets (id, ticker, cik) VALUES (1, 'XOM', ?)", (SUCCESSOR,))
    memory_db.execute("INSERT INTO assets (id, ticker, cik) VALUES (2, 'AAA', '0000000002')")
    _filing(memory_db, 1, "a-1", PREDECESSOR)
    with pytest.raises(sqlite3.IntegrityError, match="recorded predecessor"):
        memory_db.execute("UPDATE sec_filings SET asset_id = 2")
    with pytest.raises(sqlite3.IntegrityError, match="recorded predecessor"):
        memory_db.execute("UPDATE sec_filings SET cik = '0000789019'")


def test_rows_written_before_the_guard_are_found_and_refuse_the_migration(
    memory_db: Database,
) -> None:
    identity.drop_triggers(memory_db)
    memory_db.execute("INSERT INTO assets (id, ticker, cik) VALUES (1, 'AAA', '0000000001')")
    _filing(memory_db, 1, "x-1", PREDECESSOR)
    assert identity.violations(memory_db).outside_chain == [f"AAA x-1 ({PREDECESSOR})"]
    with pytest.raises(identity.IdentityViolation, match="outside their asset's chain"):
        identity.require(memory_db)


def test_upsert_filing_records_the_cik_and_keeps_the_first(memory_db: Database) -> None:
    memory_db.execute("INSERT INTO assets (id, ticker, cik) VALUES (1, 'XOM', ?)", (SUCCESSOR,))
    key = db.FilingKey(1, "10-Q", 2026, "2026Q2")
    meta = db.FilingMeta("2026-08-03", JOINT_10Q, "2026-06-30", SUCCESSOR)
    db.upsert_filing(memory_db, key, meta)
    db.upsert_filing(
        memory_db, key, db.FilingMeta("2026-08-03", JOINT_10Q, "2026-06-30", PREDECESSOR)
    )
    assert memory_db.execute("SELECT cik FROM sec_filings").fetchone()["cik"] == SUCCESSOR
