"""Legacy quarters that share one 10-Q's accession are replaced, and cannot come back (T-120).

Before T-091/T-092 one 10-Q per year was stored as a filing row for every quarter column of
its payload, all with its accession, filing date and facts -- in production, 41 accessions
over 13 tickers (e.g. ALLE 2022: 2022Q1/Q2/Q3, one accession, filed 2022-10-27). The fixture
rebuilds that shape from the real STZ Q2 2023 10-Q (filed 2023-10-05), whose payload carries
a Q1 comparative: rows 2023Q1 and 2023Q2 with its accession. STZ's real Q1 10-Q was filed
2023-06-30 under its own accession.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from portfolio_common.db import Database
from test_pipeline_multi_filing import Q1_JUN, Q2_OCT, _Edgar, _q1_payload, _stz_q2_payload

from fundamental_agent import cli, db, pipeline, repair
from fundamental_agent.config import Settings
from fundamental_agent.edgar_client import EdgarError
from fundamental_agent.pipeline import RunParams
from fundamental_agent.statements import Statements, iter_facts
from kg_schema import connect

SHARED = Q2_OCT.accession_number


def _legacy(conn: Database, q1_end: str = "2023-05-31") -> dict[str, int]:
    """STZ's Q2 10-Q stored the pre-T-091 way: a Q1 row and a Q2 row, both carrying its
    accession, filing date and payload (written with the new triggers dropped)."""
    db.ensure_schema(conn)
    conn.execute("DROP TRIGGER trg_sf_accession_insert")
    conn.execute("DROP TRIGGER trg_sf_accession_update")
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'STZ')")
    stmts = Statements.from_payload(_stz_q2_payload())
    ids = {}
    for fp, pe in (("2023Q1", q1_end), ("2023Q2", "2023-08-31")):
        fid = db.upsert_filing(
            conn,
            db.FilingKey(1, "10-Q", 2023, fp),
            db.FilingMeta(filing_date=Q2_OCT.filing_date, accession_number=SHARED, period_end=pe),
        )
        db.append_financial_facts(conn, fid, iter_facts(stmts), filing_version=SHARED)
        ids[fp] = fid
    conn.execute(
        "INSERT INTO sec_filing_section (filing_id, section_type, ordinal, text, text_sha256, "
        "extraction_method, event_time, retrieved_at, engine_version) "
        "VALUES (?, 'MD&A', 0, 'the Q2 filing text', 'x', 'test', 'now', 'now', 'sec-v1')",
        (ids["2023Q1"],),
    )
    conn.commit()
    db.ensure_schema(conn)  # the triggers come back, as on production after this change
    return ids


@pytest.fixture
def legacy(memory_db: Database) -> tuple[Database, dict[str, int]]:
    return memory_db, _legacy(memory_db)


def _gateway(**kw: Any) -> _Edgar:
    return _Edgar(
        {("10-Q", 2023): [Q2_OCT, Q1_JUN]},
        {SHARED: _stz_q2_payload(), Q1_JUN.accession_number: _q1_payload()},
        **kw,
    )


def _filings(conn: Database) -> dict[str, tuple[str, str]]:
    return {
        str(r["fiscal_period"]): (str(r["accession_number"]), str(r["filing_date"]))
        for r in conn.execute(
            "SELECT fiscal_period, accession_number, filing_date FROM sec_filings"
        )
    }


def _versions(conn: Database, filing_id: int) -> set[str]:
    return {
        str(r[0])
        for r in conn.execute(
            "SELECT DISTINCT filing_version FROM financial_facts WHERE filing_id = ?", (filing_id,)
        )
    }


# -- the invariant -------------------------------------------------------------------------


def test_the_legacy_rows_are_found(legacy: tuple[Database, dict[str, int]]) -> None:
    conn, ids = legacy
    rows = db.shared_accession_filings(conn)
    assert [(r["fiscal_period"], r["accession_number"]) for r in rows] == [
        ("2023Q1", SHARED),
        ("2023Q2", SHARED),
    ]
    (group,) = repair.shared_groups(conn)
    assert (group.keep_id, group.keep_period) == (ids["2023Q2"], "2023-08-31")  # own period
    assert [s.fiscal_period for s in group.stale] == ["2023Q1"]
    assert group.stale[0].sections == 1


def test_the_database_refuses_a_second_row_with_the_same_accession(memory_db: Database) -> None:
    conn = memory_db
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'STZ'), (2, 'OTH')")
    q2 = db.FilingKey(1, "10-Q", 2023, "2023Q2")
    meta = db.FilingMeta(Q2_OCT.filing_date, SHARED, "2023-08-31")
    fid = db.upsert_filing(conn, q2, meta)
    assert db.upsert_filing(conn, q2, meta) == fid  # re-ingesting the same filing: fine
    with pytest.raises(sqlite3.IntegrityError, match="accession_number already used"):
        db.upsert_filing(conn, db.FilingKey(1, "10-Q", 2023, "2023Q1"), meta)
    q1 = db.upsert_filing(
        conn,
        db.FilingKey(1, "10-Q", 2023, "2023Q1"),
        db.FilingMeta(Q1_JUN.filing_date, Q1_JUN.accession_number, "2023-05-31"),
    )
    with pytest.raises(sqlite3.IntegrityError, match="accession_number already used"):
        conn.execute("UPDATE sec_filings SET accession_number = ? WHERE id = ?", (SHARED, q1))
    # Another asset may carry the same string (it is per company), and NULL is never shared.
    db.upsert_filing(conn, db.FilingKey(2, "10-Q", 2023, "2023Q2"), meta)
    for fp in ("2022Q1", "2022Q2"):
        db.upsert_filing(conn, db.FilingKey(1, "10-Q", 2022, fp), db.FilingMeta(None, None, None))
    assert db.shared_accession_filings(conn) == []


def test_a_run_refuses_to_start_over_legacy_rows(tmp_path: Path) -> None:
    """``T-100`` cannot resume past them: a run would re-key the Q1 row to its own accession
    in place and append its real facts beside the Q2 payload's."""
    conn = connect(tmp_path / "kg.db")
    _legacy(conn)
    conn.close()
    settings = Settings(
        db_path=tmp_path / "kg.db",
        universe_db_path=tmp_path / "universe.db",
        llm_api_key="k",
        llm_model="m",
        llm_url="http://llm.test",
        edgar_base_url="http://edgar.test",
    )
    with pytest.raises(pipeline.SharedAccessionsError, match=r"2 filing rows share 1 .*STZ"):
        pipeline.run(settings, RunParams(analysis_date="2026-09-25"))


# -- the repair ----------------------------------------------------------------------------


def test_the_dry_run_finds_the_quarter_s_own_filing_and_changes_nothing(
    legacy: tuple[Database, dict[str, int]],
) -> None:
    conn, _ids = legacy
    before = _filings(conn)
    gateway = _gateway()
    (outcome,) = repair.repair(conn, gateway)
    assert gateway.financials_calls == [Q1_JUN.accession_number]  # the shared one is known
    assert [(r.stale.fiscal_period, r.accession, r.filing_date) for r in outcome.replaced] == [
        ("2023Q1", Q1_JUN.accession_number, Q1_JUN.filing_date)
    ]
    assert not outcome.applied and not outcome.unresolved
    assert _filings(conn) == before


def test_apply_replaces_the_stale_quarter_with_its_own_filing(
    legacy: tuple[Database, dict[str, int]],
) -> None:
    conn, ids = legacy
    q2_facts = conn.execute(
        "SELECT COUNT(*) FROM financial_facts WHERE filing_id = ?", (ids["2023Q2"],)
    ).fetchone()[0]
    (outcome,) = repair.repair(conn, _gateway(), apply=True)
    assert outcome.applied

    assert _filings(conn) == {
        "2023Q1": (Q1_JUN.accession_number, Q1_JUN.filing_date),  # its own, filed in June
        "2023Q2": (SHARED, Q2_OCT.filing_date),
    }
    assert db.shared_accession_filings(conn) == []
    q1 = int(
        conn.execute("SELECT id FROM sec_filings WHERE fiscal_period = '2023Q1'").fetchone()[0]
    )
    assert q1 != ids["2023Q1"]
    assert _versions(conn, q1) == {Q1_JUN.accession_number}  # none of the Q2 payload's facts
    keys = {
        str(r[0])
        for r in conn.execute(
            "SELECT DISTINCT period_key FROM financial_facts WHERE filing_id = ?", (q1,)
        )
    }
    assert keys == {"2023-05-31 (Q1)", "2022-05-31 (Q1)"}
    stale_left = conn.execute(
        "SELECT (SELECT COUNT(*) FROM financial_facts WHERE filing_id = ?) + "
        "(SELECT COUNT(*) FROM sec_filing_section WHERE filing_id = ?)",
        (ids["2023Q1"], ids["2023Q1"]),
    ).fetchone()[0]
    assert stale_left == 0  # the stale row's facts and borrowed text are gone with it
    # The genuine row is untouched.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM financial_facts WHERE filing_id = ?", (ids["2023Q2"],)
        ).fetchone()[0]
        == q2_facts
    )
    assert repair.shared_groups(conn) == []  # idempotent: nothing left to repair


def test_a_stale_row_with_a_borrowed_period_end_is_matched_by_fiscal_period(
    memory_db: Database,
) -> None:
    """NOC's shape in production: the stale "2023Q2" row was dated 2023-04-25, a stray
    "(Q2)" column of the Q3 10-Q, while its own Q2 10-Q ends 2023-06-30. Here the stale Q1
    row is dated 2023-04-25; STZ's own Q1 ends 2023-05-31."""
    conn = memory_db
    _legacy(conn, q1_end="2023-04-25")
    (outcome,) = repair.repair(conn, _gateway(), apply=True)
    assert [(r.stale.period_end, r.period_end) for r in outcome.replaced] == [
        ("2023-04-25", "2023-05-31")
    ]
    row = conn.execute(
        "SELECT period_end, accession_number FROM sec_filings WHERE fiscal_period = '2023Q1'"
    ).fetchone()
    assert (row["period_end"], row["accession_number"]) == ("2023-05-31", Q1_JUN.accession_number)
    assert db.shared_accession_filings(conn) == []


def test_a_gateway_outage_leaves_the_group_as_it_was(
    legacy: tuple[Database, dict[str, int]],
) -> None:
    conn, _ids = legacy
    before = _filings(conn)
    down = _gateway(listing_raises=EdgarError("Temporary failure in name resolution"))
    (outcome,) = repair.repair(conn, down, apply=True)
    assert outcome.error and "name resolution" in outcome.error
    assert not outcome.applied and _filings(conn) == before
    (retried,) = repair.repair(conn, _gateway(), apply=True)  # back up: the re-run repairs it
    assert retried.applied


def test_an_unfound_quarter_is_left_unless_dropping_is_asked_for(
    legacy: tuple[Database, dict[str, int]],
) -> None:
    conn, ids = legacy
    nothing = _Edgar({("10-Q", 2023): [Q2_OCT]}, {SHARED: _stz_q2_payload()})
    (outcome,) = repair.repair(conn, nothing, apply=True)
    assert [s.fiscal_period for s in outcome.unresolved] == ["2023Q1"] and not outcome.applied
    assert "2023Q1" in _filings(conn)

    (dropped,) = repair.repair(conn, nothing, apply=True, drop_unresolved=True)
    assert dropped.applied and dropped.dropped
    assert _filings(conn) == {"2023Q2": (SHARED, Q2_OCT.filing_date)}
    assert (
        conn.execute("SELECT COUNT(*) FROM sec_filings WHERE id = ?", (ids["2023Q1"],)).fetchone()[
            0
        ]
        == 0
    )


def test_a_stale_row_with_derived_data_is_refused(
    legacy: tuple[Database, dict[str, int]],
) -> None:
    conn, ids = legacy
    conn.execute(
        "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, unit, "
        "computed_at, engine_version, available_at) VALUES (?, 'profitability', 'net_margin', "
        "0.1, 'x', 'now', 'metrics-v2', (SELECT available_at FROM sec_filings WHERE id = ?))",
        (ids["2023Q1"], ids["2023Q1"]),
    )
    conn.commit()
    with pytest.raises(repair.RepairRefused, match=r"STZ 2023Q1 .*1 metrics"):
        repair.shared_groups(conn)


# -- the command ---------------------------------------------------------------------------


def test_the_command_is_a_dry_run_unless_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "kg.db"
    conn = connect(path)
    _legacy(conn)
    conn.close()
    monkeypatch.setattr(cli, "EdgarClient", _gateway())

    assert cli.main(["repair-accessions", "--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "would replace (dry run)" in out and Q1_JUN.accession_number in out
    check = connect(path)
    assert len(db.shared_accession_filings(check)) == 2
    check.close()

    assert cli.main(["repair-accessions", "--db", str(path), "--apply"]) == 0
    assert "1 quarters replaced; 0 accessions left unresolved" in capsys.readouterr().out
    assert cli.main(["repair-accessions", "--db", str(path)]) == 0
    assert "nothing to repair" in capsys.readouterr().out
