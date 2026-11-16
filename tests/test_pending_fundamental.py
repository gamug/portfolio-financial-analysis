"""``fundamental_agent pending`` (T-017): read-only, no LLM, the step's own listing and filters."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from conftest import cik_for, seed_fundamental_score, write_universe_db

from fundamental_agent import cli, pending, pipeline
from fundamental_agent.db import ensure_schema
from fundamental_agent.edgar_client import EdgarError, EdgarNotFoundError, FilingRef
from fundamental_agent.pending import Verdict, _Lister, _Plan
from kg_schema import connect
from kg_schema.queries import UniverseMember
from kg_schema.trading_calendar import available_from

D = "2026-10-02"
KNOWN = "0000072971-23-000142"  # production's shape: three `Failed to extract financials` rows


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
        self, *, as_of: str | None = D, status: str = "completed", params: dict | None = None
    ) -> int:
        body = {
            "analysis_date": as_of,
            "forms": ["10-K", "10-Q"],
            "since_year": 2022,
            "until_year": int((as_of or D)[:4]),
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

    def line(
        self, edgar: FakeEdgar | None = None, date: str = D, *, metrics_only: bool = False
    ) -> str:
        self.conn.close()
        edgar = edgar or FakeEdgar()
        return pending.pending_line(
            self.db,
            self.udb,
            date,
            edgar=edgar,  # type: ignore[arg-type]  # FakeEdgar stands in for EdgarClient
            metrics_only=metrics_only,
        )


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


# -- a rename: the member's symbol is not the asset's (T-153 part 2, F1) ----------------------

_RENAME_DAY = "2022-06-08"  # a Wednesday; FB is the member that day


def _fb_meta(tmp_path: Path) -> tuple[Path, Path, int]:
    """A member FB (CIK 0001326801) whose one asset is META."""
    udb = write_universe_db(tmp_path / "universe.db", [("FB", "2022-01-03", None)])
    raw = sqlite3.connect(udb)
    raw.execute("UPDATE universe_membership SET cik = '0001326801'")
    raw.commit()
    raw.close()
    db_path = tmp_path / "fin.db"
    conn = connect(db_path)
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO assets (ticker, company_name, cik) VALUES ('META', 'Meta Platforms', "
        "'0001326801')"
    )
    asset_id = int(conn.execute("SELECT id FROM assets").fetchone()["id"])
    conn.commit()
    conn.close()
    return db_path, udb, asset_id


def test_a_check_before_a_rename_sees_the_canonical_asset(tmp_path: Path) -> None:
    """F1: FB on 2022-06-08 is META's line; META's stored, scored filing is current."""
    db_path, udb, asset_id = _fb_meta(tmp_path)
    conn = connect(db_path)
    seed_fundamental_score(conn, asset_id, 50.0, event_time="2022-03-31", filing_date="2022-05-01")
    conn.execute(
        "UPDATE sec_filings SET accession_number = '0001326801-22-000001', form = '10-Q', "
        "cik = '0001326801' WHERE id = (SELECT MAX(id) FROM sec_filings)"
    )
    conn.commit()
    conn.close()
    assert pending.pending_line(db_path, udb, _RENAME_DAY, edgar=FakeEdgar()).startswith(  # type: ignore[arg-type]
        "pending: no"
    )


def test_a_check_before_a_rename_finds_a_stored_unscored_filing(tmp_path: Path) -> None:
    """F1: the same member with META's filing stored but unscored must read as pending. Before
    the fix the plan was keyed on FB, so clause 1 skipped META's row and the check said no."""
    db_path, udb, asset_id = _fb_meta(tmp_path)
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, filing_date, "
        "accession_number, period_end, retrieved_at, cik, available_at) "
        "VALUES (?, '10-Q', 2022, '2022Q1', '2022-05-01', '0001326801-22-000001', '2022-03-31', "
        "'now', '0001326801', '2022-05-02')",
        (asset_id,),
    )
    conn.commit()
    conn.close()
    line = pending.pending_line(db_path, udb, _RENAME_DAY, edgar=FakeEdgar())  # type: ignore[arg-type]
    assert line.startswith("pending: yes") and "0001326801-22-000001" in line


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


PRED = "0000000999"
PRED_LAST_10K = "0000000999-26-000001"


def _succession(fx: Fixture, *, linked: bool = True) -> FakeEdgar:
    """AAA succeeded PRED on 2026-01-01. *linked*: PRED's last 10-K (period 2025-12-31) is listed,
    so the step links the predecessor (``_last_annual_report_missing`` finds it); it is stored and
    scored, so listing it never makes the check pending."""
    fx.conn.execute(
        "INSERT INTO cik_succession (successor_cik, predecessor_cik, effective_date, "
        "evidence_form, evidence_accession, evidence_quote) "
        "VALUES (?, ?, '2026-01-01', '8-K12B', 'x', 'x')",
        (cik_for("AAA"), PRED),
    )
    fx.conn.commit()
    edgar = FakeEdgar()
    if linked:
        fx.scored("AAA", PRED_LAST_10K, "2026-02-15", form="10-K")
        edgar.listing[(PRED, "10-K", 2025)] = [
            FilingRef("10-K", "2026-02-15", PRED_LAST_10K, cik=PRED, period_of_report="2025-12-31")
        ]
    return edgar


def test_a_newly_recorded_succession_cik_is_listed_over_its_full_window(fx: Fixture) -> None:
    edgar = _succession(fx)
    fx.line(edgar)
    assert {y for c, f, y in edgar.calls if c == PRED and f == "10-Q"} == {
        2022,
        2023,
        2024,
        2025,
        2026,
    }
    assert {y for c, _, y in edgar.calls if c == cik_for("AAA")} == {2025, 2026}


def test_null_cik_rows_count_for_the_assets_own_cik_so_the_window_stays_narrow(fx: Fixture) -> None:
    """m012 does not backfill ``sec_filings.cik``: a row from before T-149 is NULL there."""
    fx.conn.execute("UPDATE sec_filings SET cik = NULL")
    fx.conn.commit()
    edgar = FakeEdgar()
    assert fx.line(edgar) == "pending: no 2 members current, 8 listings"
    assert {y for _, _, y in edgar.calls} == {2025, 2026}


def test_a_predecessor_cik_with_only_null_rows_gets_the_full_window(fx: Fixture) -> None:
    edgar = _succession(fx)
    fx.conn.execute("UPDATE sec_filings SET cik = NULL")
    fx.conn.commit()
    fx.line(edgar)
    assert {y for c, f, y in edgar.calls if c == PRED and f == "10-Q"} == {
        2022,
        2023,
        2024,
        2025,
        2026,
    }
    assert {y for c, _, y in edgar.calls if c == cik_for("AAA")} == {2025, 2026}


def test_a_predecessor_whose_last_10k_is_not_listed_is_unlinked_not_pending(fx: Fixture) -> None:
    """The step drops it (APP-10b-3) and never scores its filings: they must not keep the check
    pending forever."""
    edgar = _succession(fx, linked=False)
    edgar.listing[(PRED, "10-Q", 2025)] = [_ref("0000000999-25-000007", "2025-08-01", PRED)]
    line = fx.line(edgar)
    assert line.startswith("pending: no")
    assert f"1 unlinked predecessor (AAA {PRED})" in line
    assert not any(c == PRED and f == "10-Q" for c, f, _ in edgar.calls)  # never listed


def _successor_listing(
    fx: Fixture, edgar: FakeEdgar, *, form: str = "10-K", period: str = "2025-12-31"
) -> None:
    """The predecessor's last annual report is listed under the successor's (AAA's) CIK and stored
    (so the successor's own listing is not what makes the step pending)."""
    fx.scored("AAA", PRED_LAST_10K, "2026-02-15", form="10-K")
    edgar.listing[(cik_for("AAA"), "10-K", 2025)] = [
        FilingRef(form, "2026-02-15", PRED_LAST_10K, cik=cik_for("AAA"), period_of_report=period)
    ]


def test_the_last_annual_report_under_the_successor_links_the_predecessor(fx: Fixture) -> None:
    edgar = _succession(fx, linked=False)
    _successor_listing(fx, edgar)
    edgar.listing[(PRED, "10-Q", 2025)] = [_ref("0000000999-25-000007", "2025-08-01", PRED)]
    line = fx.line(edgar)
    assert "unlinked predecessor" not in line
    assert "0000000999-25-000007" in line


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"form": "10-K/A"}, "an amendment does not count"),
        ({"period": "2024-06-30"}, "a period outside the 371-day window does not count"),
    ],
)
def test_a_decoy_under_the_successor_does_not_count(
    fx: Fixture, kwargs: dict[str, str], why: str
) -> None:
    edgar = _succession(fx, linked=False)
    _successor_listing(fx, edgar, **kwargs)
    assert f"1 unlinked predecessor (AAA {PRED})" in fx.line(edgar)


def test_a_successor_listing_that_cannot_be_read_fails_the_check_not_unlinks(
    fx: Fixture,
) -> None:
    """An outage while checking the successor must never read as "unlinked, not pending" either."""
    edgar = _succession(fx, linked=False)

    def down(cik: str, form: str, year: int) -> list[FilingRef]:
        if cik == cik_for("AAA") and form == "10-K":
            raise EdgarError("/filing_by_year failed after 3 attempts")
        return []

    edgar.filing_by_year = down  # type: ignore[method-assign]
    with pytest.raises(EdgarError):
        fx.line(edgar)


def test_pending_fails_the_check_on_a_successor_listing_error() -> None:
    """The check itself reads the successor's listing: an outage there fails it (never "unlinked")."""
    succ, pred = "0000000002", "0000000001"

    class Down:
        def filing_by_year(self, cik: str, form: str, year: int) -> list[FilingRef]:
            if cik == succ and form == "10-K":
                raise EdgarError("/filing_by_year failed after 3 attempts")
            return []

    plan = _Plan(
        ciks={"AAA": [(succ, True), (pred, True)]},
        windows={"AAA": [2022, 2023, 2024, 2025, 2026]},
        effective={(succ, pred): "2026-01-01"},
    )
    member = UniverseMember(
        symbol="AAA",
        security="AAA Inc.",
        cik=succ,
        gics_sector=None,
        gics_sub_industry=None,
        hq_location=None,
        date_added=None,
        founded=None,
        valid_from="2020-01-01",
        valid_to=None,
    )
    lister = _Lister(Down(), D, plan, Verdict(), None)  # type: ignore[arg-type]
    with pytest.raises(EdgarError, match="could not be read"):
        lister._unlinked(member, pred)


def test_a_linked_predecessor_filing_not_stored_is_pending(fx: Fixture) -> None:
    edgar = _succession(fx)
    edgar.listing[(PRED, "10-Q", 2025)] = [_ref("0000000999-25-000007", "2025-08-01", PRED)]
    assert "0000000999-25-000007" in fx.line(edgar)


def test_a_predecessor_listing_that_cannot_be_read_fails_the_check_not_unlinks(
    fx: Fixture,
) -> None:
    """An outage while checking the predecessor must never read as "unlinked, not pending"."""
    edgar = _succession(fx)

    def down(cik: str, form: str, year: int) -> list[FilingRef]:
        if cik == PRED:
            raise EdgarError("/filing_by_year failed after 3 attempts")
        return []

    edgar.filing_by_year = down  # type: ignore[method-assign]
    with pytest.raises(EdgarError):
        fx.line(edgar)


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
            "/financials/0000072971 -> unsuccessful payload: {'success': False, 'error': "
            "\"Failed to extract financials: 'NoneType' object has no attribute 'to_dataframe'\"}",
        )
    fx.unscored("AAA", KNOWN, "2026-08-01")
    edgar = FakeEdgar()
    line = fx.line(edgar)
    assert line == (
        f"pending: no 2 members current, 8 listings; 1 known data failure (AAA 10-Q {KNOWN})"
    )
    assert len(edgar.calls) == 8  # the D window only: the error rows cost no call


def test_a_filing_not_found_at_the_gateway_is_service_and_pending(fx: Fixture) -> None:
    run = fx.new_run(as_of="2026-09-22")
    fx.error(
        run,
        "AAA",
        "0000000001-26-000061",
        "2026",
        "process",
        "/financials/1 -> unsuccessful payload: {'success': False, 'error': \"No '10-Q' filing found\"}",
    )
    assert fx.line(NoGateway()) == "pending: yes 1 unscored filings (AAA 10-Q 0000000001-26-000061)"


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


def test_a_service_row_from_a_run_with_no_as_of_is_pending_not_stale(fx: Fixture) -> None:
    """An error row must never make a filing less pending than no row at all."""
    run = fx.new_run(as_of=None)
    fx.error(run, "AAA", "0000000001-26-000060", "2026", "process", "failed after 3 attempts")
    assert fx.line(NoGateway()) == "pending: yes 1 unscored filings (AAA 10-Q 0000000001-26-000060)"


def test_a_row_from_a_run_dated_after_the_date_is_ignored_so_the_filing_stays_pending(
    fx: Fixture,
) -> None:
    fx.unscored("AAA", "0000000001-26-000060", "2026-08-01")  # stored unscored, filed <= D
    later = fx.new_run(as_of="2026-10-20")
    fx.error(later, "AAA", "0000000001-26-000060", "2026", "process", "boom")
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
    monkeypatch.setenv("KG_FINANCIAL_DB", str(fx.db))
    monkeypatch.setenv("KG_UNIVERSE_DB", str(fx.udb))
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


# -- a leaver: covered through its exit only (T-153 part 3a) ----------------------------------


def _leaver(tmp_path: Path, valid_to: str) -> tuple[Path, Path, int]:
    """A one-line universe whose member left on *valid_to*, with its asset created."""
    db_path = tmp_path / "fin.db"
    udb = write_universe_db(tmp_path / "universe.db", [("AAA", "2020-01-01", valid_to)])
    conn = connect(db_path)
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO assets (ticker, company_name, cik) VALUES ('AAA', 'AAA Inc.', ?)",
        (cik_for("AAA"),),
    )
    asset_id = int(conn.execute("SELECT id FROM assets").fetchone()["id"])
    conn.commit()
    conn.close()
    return db_path, udb, asset_id


def _store_unscored(db_path: Path, asset_id: int, accession: str, filed: str) -> None:
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, filing_date, "
        "accession_number, period_end, retrieved_at, cik, available_at) "
        "VALUES (?, '10-Q', ?, '2022Q9', ?, ?, ?, 'now', ?, ?)",
        (
            asset_id,
            int(filed[:4]),
            filed,
            accession,
            _period_for(filed),
            cik_for("AAA"),
            available_from(filed),
        ),
    )
    conn.commit()
    conn.close()


def test_a_leaver_is_pending_for_a_filing_filed_before_its_exit(tmp_path: Path) -> None:
    """T-153 part 3a: the window covers a company that left, so its unscored filing counts."""
    db_path, udb, asset_id = _leaver(tmp_path, "2022-06-15")
    _store_unscored(db_path, asset_id, "0000000001-22-000001", "2022-05-01")
    line = pending.pending_line(db_path, udb, D, edgar=FakeEdgar())  # type: ignore[arg-type]
    assert line.startswith("pending: yes")
    assert "AAA 10-Q" in line


def test_a_filing_after_the_exit_does_not_make_the_leaver_pending(tmp_path: Path) -> None:
    """T-153 part 3a: nothing filed after the company's exit is fetched, so it is not pending."""
    db_path, udb, asset_id = _leaver(tmp_path, "2022-06-15")
    _store_unscored(db_path, asset_id, "0000000001-22-000002", "2022-09-01")
    line = pending.pending_line(db_path, udb, D, edgar=FakeEdgar())  # type: ignore[arg-type]
    assert line.startswith("pending: no")


# -- T-143 part A, P1: only --metrics-only excuses a metrics-only filing ---------------------------


def _store_metrics_only_filing(  # noqa: PLR0913 - one filing row, every column a test may pin
    fx: Fixture, ticker: str, accession: str, filed: str, run_id: int, *, value_inputs: bool
) -> None:
    """A filing stored by a metrics-only run, with (or without) its ``value_inputs`` row."""
    fx.conn.execute(
        "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, filing_date, "
        "accession_number, period_end, retrieved_at, cik, available_at, run_id) "
        "VALUES (?, '10-Q', ?, ?, ?, ?, ?, 'now', ?, ?, ?)",
        (
            fx.ids[ticker],
            int(filed[:4]),
            f"{filed[:4]}Q9",
            filed,
            accession,
            _period_for(filed),
            cik_for(ticker),
            available_from(filed),
            run_id,
        ),
    )
    if value_inputs:
        filing_id = int(fx.conn.execute("SELECT MAX(id) FROM sec_filings").fetchone()[0])
        fx.conn.execute(
            "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, unit, "
            "inputs_json, computed_at, run_id, available_at) "
            "VALUES (?, 'rule_input', 'value_inputs', 1.0, 'usd', '{}', 'now', ?, ?)",
            (filing_id, run_id, available_from(filed)),
        )
    fx.conn.commit()


def test_a_metrics_only_filing_is_pending_by_default(fx: Fixture) -> None:
    """Reviewer B, item 1: plain ``pending`` keeps master's meaning -- a metrics-only filing
    (metrics and no FUNDAMENTAL row) is pending, so the orchestrator still runs ``run`` for it and
    part B scores it."""
    run = fx.new_run(params={"metrics_only": True})
    _store_metrics_only_filing(
        fx, "AAA", "0000000001-26-000050", "2026-06-01", run, value_inputs=True
    )
    line = fx.line(FakeEdgar())
    assert line.startswith("pending: yes")
    assert "0000000001-26-000050" in line


def test_a_completed_metrics_only_filing_is_done_only_under_the_flag(fx: Fixture) -> None:
    """``pending --metrics-only`` treats a completed metrics-only filing as done; plain ``pending``
    leaves it pending, so the two modes differ on the same database."""
    run = fx.new_run(params={"metrics_only": True})
    _store_metrics_only_filing(
        fx, "AAA", "0000000001-26-000050", "2026-06-01", run, value_inputs=True
    )
    plain = fx.line(FakeEdgar())
    flagged = fx.line(FakeEdgar(), metrics_only=True)
    assert plain.startswith("pending: yes")
    assert flagged.startswith("pending: no")
    assert "0000000001-26-000050" not in flagged


def test_a_metrics_only_filing_stopped_before_value_inputs_is_pending(fx: Fixture) -> None:
    """Reviewer B, item 2: without its value_inputs row the sequence did not finish -- pending even
    under ``--metrics-only``."""
    run = fx.new_run(params={"metrics_only": True})
    _store_metrics_only_filing(
        fx, "AAA", "0000000001-26-000051", "2026-06-01", run, value_inputs=False
    )
    line = fx.line(FakeEdgar(), metrics_only=True)
    assert line.startswith("pending: yes")
    assert "0000000001-26-000051" in line


def test_a_normal_runs_unscored_filing_is_still_pending(fx: Fixture) -> None:
    """A normal run's filing with metrics and no score is pending in both modes."""
    run = fx.new_run(params={"metrics_only": False})
    _store_metrics_only_filing(
        fx, "AAA", "0000000001-26-000052", "2026-06-01", run, value_inputs=True
    )
    assert fx.line(FakeEdgar()).startswith("pending: yes")
    assert fx.line(FakeEdgar(), metrics_only=True).startswith("pending: yes")
