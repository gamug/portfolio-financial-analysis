"""``fundamental_agent pending`` (T-017): read-only, no LLM, the step's own listing and filters."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from conftest import cik_for, seed_fundamental_score, write_universe_db

from fundamental_agent import cli, pending, pipeline
from fundamental_agent.db import ensure_schema
from fundamental_agent.edgar_client import EdgarError, EdgarNotFoundError, FilingRef
from kg_schema import connect
from kg_schema.trading_calendar import available_from

D = "2026-10-02"
KNOWN = "0000072971-23-000142"  # production's shape: three `unsuccessful payload` rows


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()  # noqa: S324


class FakeEdgar:
    """Stands in for ``EdgarClient``: lists per (cik, form, year); counts the calls."""

    def __init__(self, listing: dict[tuple[str, str, int], list[FilingRef]] | None = None) -> None:
        self.listing = listing or {}
        self.calls: list[tuple[str, str, int]] = []
        self.unknown: set[str] = set()

    def filing_by_year(self, cik: str, form: str, year: int) -> list[FilingRef]:
        self.calls.append((cik, form, year))
        if cik in self.unknown:
            raise EdgarNotFoundError("Company not found")
        return self.listing.get((cik, form, year), [])

    def close(self) -> None:
        return None


class NoGateway(FakeEdgar):
    def filing_by_year(self, cik: str, form: str, year: int) -> list[FilingRef]:
        raise AssertionError("an error row must never reach the gateway")


class Fixture:
    def __init__(self, tmp_path: Path, members: list[str]) -> None:
        self.db = tmp_path / "fin.db"
        self.udb = write_universe_db(
            tmp_path / "universe.db", [(m, "2020-01-01", None) for m in members]
        )
        self.conn = connect(self.db)
        ensure_schema(self.conn)
        self.ids: dict[str, int] = {}
        for m in members:
            cur = self.conn.execute(
                "INSERT INTO assets (ticker, company_name, cik) VALUES (?, ?, ?)",
                (m, f"{m} Inc.", cik_for(m)),
            )
            self.ids[m] = int(cur.lastrowid or 0)
        self.conn.commit()
        self.run_id = 0

    def scored(self, ticker: str, accession: str, filed: str, form: str = "10-Q") -> None:
        """A stored, scored filing (its snapshot makes the accession `completed`)."""
        seed_fundamental_score(
            self.conn, self.ids[ticker], 50.0, event_time=_period_for(filed), filing_date=filed
        )
        self.conn.execute(
            "UPDATE sec_filings SET accession_number = ?, form = ?, cik = ? "
            "WHERE id = (SELECT MAX(id) FROM sec_filings)",
            (accession, form, cik_for(ticker)),
        )
        self.conn.commit()

    def unscored(self, ticker: str, accession: str, filed: str, form: str = "10-Q") -> None:
        """A stored filing with no snapshot (the LLM call failed after the facts were stored)."""
        self.conn.execute(
            "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, filing_date, "
            "accession_number, period_end, retrieved_at, cik, available_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'now', ?, ?)",
            (
                self.ids[ticker],
                form,
                int(filed[:4]),
                f"{filed[:4]}Q9",
                filed,
                accession,
                _period_for(filed),
                cik_for(ticker),
                available_from(filed),
            ),
        )
        self.conn.commit()

    def new_run(
        self, *, as_of: str = D, status: str = "completed", params: dict | None = None
    ) -> int:
        body = {
            "analysis_date": as_of,
            "forms": ["10-K", "10-Q"],
            "since_year": 2022,
            "until_year": int(as_of[:4]),
            "limit": None,
            "tickers": None,
            "fresh": False,
        }
        body.update(params or {})
        for key in [k for k, v in body.items() if v == "__drop__"]:
            del body[key]
        cur = self.conn.execute(
            "INSERT INTO analysis_run (started_at, status, as_of, params_json) VALUES ('now', ?, ?, ?)",
            (status, as_of, json.dumps(body)),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def error(  # noqa: PLR0913, PLR0917 - one error row, every column a test may pin
        self,
        run_id: int,
        ticker: str,
        accession: str,
        year: str,
        stage: str,
        text: str,
        form: str = "10-Q",
    ) -> None:
        self.conn.execute(
            "INSERT INTO analysis_run_error (run_id, ticker, form, fiscal_period, stage, message, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, 'now')",
            (run_id, ticker, form, year, stage, f"{accession}: {text}"),
        )
        self.conn.commit()

    def line(self, edgar: FakeEdgar | None = None, date: str = D) -> str:
        self.conn.close()
        edgar = edgar or FakeEdgar()
        return pending.pending_line(self.db, self.udb, date, edgar=edgar)  # type: ignore[arg-type]


def _period_for(filed: str) -> str:
    year, month, _ = (int(x) for x in filed.split("-"))
    return f"{year}-{max(month - 1, 1):02d}-28"


def _ref(accession: str, filed: str, cik: str, form: str = "10-Q") -> FilingRef:
    return FilingRef(form, filed, accession, cik=cik, period_of_report=_period_for(filed))


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    f = Fixture(tmp_path, ["AAA", "BBB"])
    f.scored("AAA", "0000000001-26-000001", "2026-05-01")
    f.scored("BBB", "0000000002-26-000001", "2026-05-02")
    return f


# -- the plain cases ---------------------------------------------------------------------------


def test_a_fresh_database_is_pending_without_a_gateway_call(tmp_path: Path) -> None:
    udb = write_universe_db(tmp_path / "u.db", [("AAA", "2020-01-01", None)])
    edgar = NoGateway()
    line = pending.pending_line(tmp_path / "missing.db", udb, D, edgar=edgar)  # type: ignore[arg-type]
    assert line.startswith("pending: yes") and "fresh database" in line
    empty = connect(tmp_path / "empty.db")
    empty.close()
    assert pending.pending_line(tmp_path / "empty.db", udb, D, edgar=edgar).startswith(  # type: ignore[arg-type]
        "pending: yes no assets table"
    )


def test_complete_is_not_pending_and_lists_only_the_narrow_window(fx: Fixture) -> None:
    edgar = FakeEdgar()
    line = fx.line(edgar)
    assert line == "pending: no 2 members current, 8 listings"
    assert {y for _, _, y in edgar.calls} == {2025, 2026}  # D-1..D for CIKs with stored filings


def test_a_new_filing_in_the_gateway_is_pending_and_stops_at_the_first(fx: Fixture) -> None:
    edgar = FakeEdgar(
        {
            (cik_for("AAA"), "10-Q", 2026): [
                _ref("0000000001-26-000099", "2026-08-01", cik_for("AAA"))
            ]
        }
    )
    line = fx.line(edgar)
    assert line == "pending: yes at least 1 unscored filings (AAA 10-Q 0000000001-26-000099)"
    assert not any(c[0] == cik_for("BBB") for c in edgar.calls)  # stopped at the first


def test_no_lookahead_a_filing_after_the_date_does_not_count(fx: Fixture) -> None:
    edgar = FakeEdgar(
        {
            (cik_for("AAA"), "10-Q", 2026): [
                _ref("0000000001-26-000098", "2026-10-03", cik_for("AAA")),  # filed after D
                FilingRef("10-Q", None, "0000000001-26-000097", cik=cik_for("AAA")),  # undated
                _ref(
                    "0000000001-26-000096", "2026-08-01", cik_for("AAA"), form="10-Q/A"
                ),  # amendment
            ]
        }
    )
    assert fx.line(edgar).startswith("pending: no")


def test_a_stored_but_unscored_filing_is_pending_from_the_database_alone(fx: Fixture) -> None:
    fx.unscored("AAA", "0000000001-26-000050", "2026-08-01")
    line = fx.line(NoGateway())
    assert line == "pending: yes 1 unscored filings (AAA 10-Q 0000000001-26-000050)"


def test_an_unscored_filing_dated_after_the_date_is_not_pending(fx: Fixture) -> None:
    fx.unscored("AAA", "0000000001-26-000050", "2026-10-05")
    assert fx.line().startswith("pending: no")


def test_a_new_member_is_listed_over_the_full_window(tmp_path: Path) -> None:
    f = Fixture(tmp_path, ["AAA"])
    edgar = FakeEdgar()
    line = f.line(edgar)
    assert line.startswith("pending: no")
    assert {y for _, _, y in edgar.calls} == {2022, 2023, 2024, 2025, 2026}


def test_a_newly_recorded_succession_cik_is_listed_over_its_full_window(fx: Fixture) -> None:
    fx.conn.execute(
        "INSERT INTO cik_succession (successor_cik, predecessor_cik, effective_date, evidence_form, "
        "evidence_accession, evidence_quote) VALUES (?, '0000000999', '2026-01-01', '8-K12B', 'x', 'x')",
        (cik_for("AAA"),),
    )
    fx.conn.commit()
    edgar = FakeEdgar()
    fx.line(edgar)
    assert {y for c, _, y in edgar.calls if c == "0000000999"} == {2022, 2023, 2024, 2025, 2026}
    assert {y for c, _, y in edgar.calls if c == cik_for("AAA")} == {2025, 2026}


def test_a_dead_member_the_gateway_has_no_match_for_is_listed_not_pending(fx: Fixture) -> None:
    edgar = FakeEdgar()
    edgar.unknown.add(cik_for("BBB"))
    line = fx.line(edgar)
    assert line.startswith("pending: no")
    assert f"1 unlisted (BBB {cik_for('BBB')})" in line


def test_a_transport_failure_is_not_swallowed(fx: Fixture) -> None:
    class Down(FakeEdgar):
        def filing_by_year(self, cik: str, form: str, year: int) -> list[FilingRef]:
            raise EdgarError("/filing_by_year failed after 3 attempts")

    with pytest.raises(EdgarError):  # the CLI exits non-zero; the runner counts that as pending
        fx.line(Down())


# -- error rows, offline -----------------------------------------------------------------------


def test_production_shape_three_unsuccessful_payloads_are_a_known_data_failure(fx: Fixture) -> None:
    run = fx.new_run(as_of="2026-09-22")
    for _ in range(3):
        fx.error(
            run,
            "AAA",
            KNOWN,
            "2026",
            "process",
            "/financials/0000072971 -> unsuccessful payload: {'success': False}",
        )
    fx.unscored("AAA", KNOWN, "2026-08-01")
    edgar = FakeEdgar()
    line = fx.line(edgar)
    assert line == (
        f"pending: no 2 members current, 8 listings; 1 known data failure (AAA 10-Q {KNOWN})"
    )
    assert len(edgar.calls) == 8  # the D window only: the error rows cost no call


def test_a_service_row_with_no_later_covering_run_is_pending(fx: Fixture) -> None:
    run = fx.new_run(as_of="2026-09-22")
    fx.error(
        run,
        "AAA",
        "0000000001-26-000060",
        "2026",
        "process",
        "/financials/1 failed after 3 attempts",
    )
    assert fx.line(NoGateway()) == "pending: yes 1 unscored filings (AAA 10-Q 0000000001-26-000060)"


def test_a_service_row_whose_year_is_outside_the_window_is_stale(fx: Fixture) -> None:
    run = fx.new_run(as_of="2024-03-01")
    fx.error(run, "AAA", "0000000001-23-000060", "2023", "process", "boom")
    line = fx.line()
    assert line.startswith("pending: no") and "1 stale" in line


def test_a_service_row_a_later_covering_run_did_not_fail_again_is_stale(fx: Fixture) -> None:
    first = fx.new_run(as_of="2026-09-22")
    fx.error(first, "AAA", "0000000001-26-000060", "2026", "process", "boom")
    fx.new_run(as_of="2026-09-25")  # completed, whole universe, forms and years, no newer error
    line = fx.line()
    assert line.startswith("pending: no") and "1 stale (AAA 10-Q 0000000001-26-000060)" in line


@pytest.mark.parametrize(
    "params",
    [
        {"forms": "__drop__"},  # a field missing: the run does not count as covering
        {"limit": 5},  # a capped run covered only the first tickers
        {"tickers": ["BBB"]},  # a run restricted to another ticker
        {"forms": ["10-K"]},  # another form
        {"since_year": 2027},  # years that do not contain the unit
    ],
)
def test_a_later_run_that_may_not_have_covered_the_unit_does_not_excuse_it(
    fx: Fixture, params: dict
) -> None:
    first = fx.new_run(as_of="2026-09-22")
    fx.error(first, "AAA", "0000000001-26-000060", "2026", "process", "boom")
    fx.new_run(as_of="2026-09-25", params=params)
    assert fx.line(NoGateway()).startswith("pending: yes 1 unscored")


def test_a_later_run_that_is_not_completed_or_is_earlier_does_not_excuse_it(fx: Fixture) -> None:
    first = fx.new_run(as_of="2026-09-22")
    fx.error(first, "AAA", "0000000001-26-000060", "2026", "process", "boom")
    fx.new_run(as_of="2026-09-25", status="failed")
    fx.new_run(as_of="2026-09-01")  # an earlier as_of cannot have listed a later filing
    assert fx.line(NoGateway()).startswith("pending: yes 1 unscored")


def test_an_error_row_for_a_non_member_or_after_the_date_is_not_pending(fx: Fixture) -> None:
    run = fx.new_run(as_of="2026-10-20")  # recorded by a run after D
    fx.error(run, "AAA", "0000000001-26-000060", "2026", "process", "boom")
    run2 = fx.new_run(as_of="2026-09-22")
    fx.error(run2, "ZZZ", "0000000001-26-000061", "2026", "process", "boom")  # not a member
    line = fx.line()
    assert line.startswith("pending: no")


def test_period_and_identity_rows_are_data_failures_whatever_the_text(fx: Fixture) -> None:
    run = fx.new_run(as_of="2026-09-22")
    fx.error(run, "AAA", "0000000001-26-000070", "2026", "period", "no quarter column of its own")
    fx.error(run, "BBB", "0000000002-26-000070", "2026", "identity", "listed under another CIK")
    assert "2 known data failure" in fx.line()


# -- read-only and no LLM ----------------------------------------------------------------------


def test_the_check_writes_nothing(fx: Fixture) -> None:
    run = fx.new_run(as_of="2026-09-22")
    fx.error(run, "AAA", "0000000001-26-000060", "2026", "process", "boom")
    fx.conn.commit()
    fx.conn.close()
    before = (_sha1(fx.db), _sha1(fx.udb))
    pending.pending_line(fx.db, fx.udb, D, edgar=FakeEdgar())  # type: ignore[arg-type]
    assert (_sha1(fx.db), _sha1(fx.udb)) == before


def test_the_subcommand_never_builds_the_llm_agent(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("the pending check must not touch the LLM")

    monkeypatch.setattr(pipeline, "build_model", boom)
    monkeypatch.setattr(pipeline, "FundamentalAnalyst", boom)
    for var in ("LLM_API_KEY", "LLM_MODEL", "LLM_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(pending, "EdgarClient", lambda _url: FakeEdgar())
    fx.conn.close()
    code = cli.main(
        ["pending", "--analysis-date", D, "--db", str(fx.db), "--universe-db", str(fx.udb)]
    )
    out = capsys.readouterr().out
    assert code == 0 and out.count("\n") == 1 and out.startswith("pending: no ")


def test_the_check_uses_the_steps_own_predicates() -> None:
    """One source of truth: the listing and the filters are the step's, not copies."""
    assert pending.pipeline._list_filings is pipeline._list_filings
    assert pending.pipeline._select_filings is pipeline._select_filings
    assert pending.completed_accessions.__module__ == "fundamental_agent.db"
    assert pending.DEFAULT_FORMS is pipeline.DEFAULT_FORMS
