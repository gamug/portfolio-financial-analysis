"""``pricing_agent pending`` (T-017): read-only, gateway GETs only."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import cik_for, write_universe_db

from kg_schema import connect
from kg_schema.queries import members_asof
from pricing_agent import cli, db, pending
from pricing_agent.config import Settings
from pricing_agent.db import PRICE_OBSERVATION_ENGINE_VERSION
from pricing_agent.pricing_client import Candle, DailyPrices

D = "2026-10-09"  # a Friday session, final long before the test's clock
NOW = datetime(2026, 10, 13, 15, tzinfo=UTC)


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()  # noqa: S324


class FakeGateway:
    def __init__(self, have: dict[str, list[str]] | None = None) -> None:
        self.have = have or {}  # symbol -> the dates it has candles for
        self.calls: list[tuple[str, str, str]] = []

    def daily_any_spelling(self, ticker: str, start: str, end: str) -> DailyPrices:
        self.calls.append((ticker, start, end))
        dates = [d for d in self.have.get(ticker, []) if start <= d <= end]
        candles = [Candle(d, 1, 1, 1, 1, 1, "x") for d in dates]
        return DailyPrices(ticker, start, end, "x", candles, None)

    def close(self) -> None:
        return None


class Fx:
    def __init__(self, tmp_path: Path, members: list[str]) -> None:
        self.settings = Settings(
            db_path=tmp_path / "fin.db",
            universe_db_path=write_universe_db(
                tmp_path / "u.db", [(m, "2020-01-01", None) for m in members]
            ),
        )
        self.conn = connect(self.settings.db_path)
        db.ensure_schema(self.conn)
        self.ids: dict[str, int] = {}
        for m in members:
            cur = self.conn.execute(
                "INSERT INTO assets (ticker, company_name, cik) VALUES (?, ?, ?)",
                (m, m, cik_for(m)),
            )
            self.ids[m] = int(cur.lastrowid or 0)
        self.conn.commit()

    def bar(self, ticker: str, date_: str = D, *, obs: bool = True) -> None:
        self.conn.execute(
            "INSERT INTO price_daily (asset_id, date, close) VALUES (?, ?, 1.0)",
            (self.ids[ticker], date_),
        )
        if obs:
            self.conn.execute(
                "INSERT INTO price_observation (asset_id, obs_date, close, event_time, computed_at, "
                "engine_version) VALUES (?, ?, 1.0, ?, 'now', ?)",
                (self.ids[ticker], date_, date_, PRICE_OBSERVATION_ENGINE_VERSION),
            )
        self.conn.commit()

    def check(self, gateway: FakeGateway, date_: str = D) -> tuple[bool, str]:
        self.conn.commit()
        ro = connect(self.settings.db_path, read_only=True)
        uni = connect(self.settings.universe_db_path, read_only=True)
        try:
            return pending.check(
                ro,
                members_asof(uni, date_),
                date_,
                client=gateway,  # type: ignore[arg-type]
                now=NOW,  # type: ignore[arg-type]
            )
        finally:
            ro.close()
            uni.close()


@pytest.fixture
def fx(tmp_path: Path) -> Fx:
    f = Fx(tmp_path, ["AAA", "BBB"])
    f.bar("AAA")
    f.bar("BBB")
    return f


# -- a rename: the member's symbol is not the asset's (T-153 part 2, F1) --------------------

_RENAME_DAY = "2022-06-08"  # a Wednesday session; FB is the member that day


def _fb_meta(tmp_path: Path) -> tuple[Settings, int]:
    """A member FB (CIK 0001326801) whose one asset is META, with a bar + observation at the day."""
    udb = write_universe_db(tmp_path / "u.db", [("FB", "2022-01-03", None)])
    conn = sqlite3.connect(udb)
    conn.execute("UPDATE universe_membership SET cik = '0001326801'")
    conn.commit()
    conn.close()
    settings = Settings(db_path=tmp_path / "fin.db", universe_db_path=udb)
    writable = connect(settings.db_path)
    db.ensure_schema(writable)
    writable.execute(
        "INSERT INTO assets (ticker, company_name, cik) VALUES ('META', 'Meta', '0001326801')"
    )
    asset_id = int(writable.execute("SELECT id FROM assets").fetchone()["id"])
    writable.execute(
        "INSERT INTO price_daily (asset_id, date, close) VALUES (?, ?, 1.0)",
        (asset_id, _RENAME_DAY),
    )
    writable.execute(
        "INSERT INTO price_observation (asset_id, obs_date, close, event_time, computed_at, "
        "engine_version) VALUES (?, ?, 1.0, ?, 'now', ?)",
        (asset_id, _RENAME_DAY, _RENAME_DAY, PRICE_OBSERVATION_ENGINE_VERSION),
    )
    writable.commit()
    writable.close()
    return settings, asset_id


def _check(settings: Settings, gateway: FakeGateway) -> tuple[bool, str]:
    ro = connect(settings.db_path, read_only=True)
    uni = connect(settings.universe_db_path, read_only=True)
    try:
        return pending.check(
            ro,
            members_asof(uni, _RENAME_DAY),
            _RENAME_DAY,
            client=gateway,  # type: ignore[arg-type]
            now=NOW,  # type: ignore[arg-type]
        )
    finally:
        ro.close()
        uni.close()


def test_a_check_before_a_rename_finds_the_canonical_asset(tmp_path: Path) -> None:
    """F1: FB on 2022-06-08 is META's line -- the check must see META's bar, not 'lack'."""
    settings, _ = _fb_meta(tmp_path)
    gateway = FakeGateway()
    assert _check(settings, gateway) == (False, f"1/1 members at {_RENAME_DAY}")
    assert gateway.calls == []  # its bar is there; no gateway probe under the old symbol


def test_a_refusal_recorded_under_the_canonical_ticker_counts(tmp_path: Path) -> None:
    """F1: the refusal is recorded under META (the step's ticker); the check must read it there."""
    settings, asset_id = _fb_meta(tmp_path)
    writable = connect(settings.db_path)
    writable.execute(
        "INSERT INTO price_daily (asset_id, date, close, ingested_at) VALUES (?, '2022-05-01', 1, "
        "'2022-05-02')",
        (asset_id,),
    )
    writable.execute(
        "INSERT INTO corporate_action (asset_id, action_type, ex_date, value, source, "
        "engine_version, ingested_at) VALUES (?, 'SPLIT', '2022-05-15', 2.0, 'g', 'corpact-v1', 'now')",
        (asset_id,),
    )
    run = db.start_run(writable, params_json="{}")
    db.record_error(writable, run, db.RunError("META", None, "split_jump", "refused"))
    writable.commit()
    writable.close()
    assert _check(settings, FakeGateway()) == (
        False,
        f"1/1 members at {_RENAME_DAY}; 1 refused split jump (META)",
    )


def test_a_fresh_database_is_pending(tmp_path: Path) -> None:
    s = Settings(
        db_path=tmp_path / "missing.db",
        universe_db_path=write_universe_db(tmp_path / "u.db", [("AAA", "2020-01-01", None)]),
    )
    assert pending.pending_line(s, D, client=FakeGateway()).startswith("pending: yes")  # type: ignore[arg-type]
    empty = connect(tmp_path / "empty.db")
    empty.close()
    s2 = s.model_copy(update={"db_path": tmp_path / "empty.db"})
    line = pending.pending_line(s2, D, client=FakeGateway())  # type: ignore[arg-type]
    assert line.startswith("pending: yes no assets table")


def test_complete_is_not_pending_and_costs_no_gateway_call(fx: Fx) -> None:
    gateway = FakeGateway()
    assert fx.check(gateway) == (False, f"2/2 members at {D}")
    assert gateway.calls == []


def test_a_member_lacking_the_session_that_the_gateway_has_is_pending(fx: Fx) -> None:
    fx.conn.execute("DELETE FROM price_daily WHERE asset_id = ?", (fx.ids["BBB"],))
    fx.conn.execute("DELETE FROM price_observation WHERE asset_id = ?", (fx.ids["BBB"],))
    gateway = FakeGateway({"BBB": [D]})
    pending_, evidence = fx.check(gateway)
    assert pending_
    assert evidence == (
        f"1 members lack {D} (BBB); the gateway has candles since 2022-01-01 for BBB"
    )
    # no stored bar: the step's own start date, to S, in one call
    assert gateway.calls == [("BBB", "2022-01-01", D)]


def test_a_member_several_sessions_behind_is_pending_when_the_gateway_has_older_candles(
    tmp_path: Path,
) -> None:
    """An earlier run failed for BBB: it lacks S-1 and S. The gateway already holds S-1 but not
    S yet. The window runs from the day after its latest stored bar, so this is pending, not dead."""
    f = Fx(tmp_path, ["AAA", "BBB"])
    f.bar("AAA")
    f.bar("BBB", "2026-10-07")  # two sessions behind (Oct 8 and Oct 9 missing)
    gateway = FakeGateway({"BBB": ["2026-10-08"]})
    pending_, evidence = f.check(gateway)
    assert pending_
    assert evidence == (
        f"1 members lack {D} (BBB); the gateway has candles since 2026-10-08 for BBB"
    )
    assert gateway.calls == [("BBB", "2026-10-08", D)]
    assert f.check(FakeGateway())[0] is False  # the gateway has none of them: dead


def test_a_dead_member_the_gateway_has_no_candle_for_is_listed_not_pending(fx: Fx) -> None:
    fx.conn.execute("DELETE FROM price_daily WHERE asset_id = ?", (fx.ids["BBB"],))
    fx.conn.execute("DELETE FROM price_observation WHERE asset_id = ?", (fx.ids["BBB"],))
    pending_, evidence = fx.check(FakeGateway())
    assert not pending_
    assert evidence == f"1/2 members at {D}; 1 without a gateway candle (BBB)"


def test_a_bar_without_its_observation_is_pending_without_a_gateway_call(tmp_path: Path) -> None:
    f = Fx(tmp_path, ["AAA"])
    f.bar("AAA", obs=False)
    gateway = FakeGateway()
    assert f.check(gateway) == (True, f"1 members lack the {D} observation (AAA)")
    assert gateway.calls == []


def test_no_lookahead_a_bar_after_the_date_does_not_count(fx: Fx) -> None:
    fx.conn.execute("DELETE FROM price_daily WHERE asset_id = ?", (fx.ids["BBB"],))
    fx.conn.execute("DELETE FROM price_observation WHERE asset_id = ?", (fx.ids["BBB"],))
    fx.bar("BBB", "2026-10-12")  # after D
    gateway = FakeGateway({"BBB": [D]})
    assert fx.check(gateway)[0]  # still lacks D


def test_the_session_is_the_last_one_on_or_before_the_date(fx: Fx) -> None:
    gateway = FakeGateway()
    assert fx.check(gateway, "2026-10-10") == (False, "2/2 members at 2026-10-09")  # Saturday


def test_bars_from_before_a_recorded_split_are_pending(fx: Fx) -> None:
    fx.conn.execute(
        "INSERT INTO price_daily (asset_id, date, close, ingested_at) VALUES (?, '2026-09-01', 1, "
        "'2026-09-02')",
        (fx.ids["AAA"],),
    )
    fx.conn.execute(
        "INSERT INTO corporate_action (asset_id, action_type, ex_date, value, source, "
        "engine_version, ingested_at) VALUES (?, 'SPLIT', '2026-09-15', 2.0, 'g', 'corpact-v1', 'now')",
        (fx.ids["AAA"],),
    )
    pending_, evidence = fx.check(FakeGateway())
    assert pending_ and "before a recorded split (AAA)" in evidence


def test_a_member_whose_split_jump_refetch_was_refused_is_listed_not_pending(fx: Fx) -> None:
    fx.conn.execute(
        "INSERT INTO price_daily (asset_id, date, close, ingested_at) VALUES (?, '2026-09-01', 1, "
        "'2026-09-02')",
        (fx.ids["AAA"],),
    )
    fx.conn.execute(
        "INSERT INTO corporate_action (asset_id, action_type, ex_date, value, source, "
        "engine_version, ingested_at) VALUES (?, 'SPLIT', '2026-09-15', 2.0, 'g', 'corpact-v1', 'now')",
        (fx.ids["AAA"],),
    )
    run = db.start_run(fx.conn, params_json="{}")
    db.record_error(fx.conn, run, db.RunError("AAA", None, "split_jump", "refused"))
    pending_, evidence = fx.check(FakeGateway())
    assert not pending_
    assert evidence == f"2/2 members at {D}; 1 refused split jump (AAA)"
    # a later, different failure for the member is not a refusal: pending again
    db.record_error(fx.conn, run, db.RunError("AAA", None, "fetch", "timeout"))
    assert fx.check(FakeGateway())[0]


def test_a_session_that_is_not_final_yet_is_pending(fx: Fx) -> None:
    ro = connect(fx.settings.db_path, read_only=True)
    uni = connect(fx.settings.universe_db_path, read_only=True)
    early = datetime(2026, 10, 9, 14, tzinfo=UTC)  # mid-session
    result = pending.check(ro, members_asof(uni, D), D, client=FakeGateway(), now=early)  # type: ignore[arg-type]
    assert result == (True, f"session {D} is not final yet (the step refuses it)")


def test_the_check_writes_nothing(fx: Fx) -> None:
    fx.conn.commit()
    fx.conn.close()
    before = (_sha1(fx.settings.db_path), _sha1(fx.settings.universe_db_path))
    pending.pending_line(fx.settings, D, client=FakeGateway())  # type: ignore[arg-type]
    assert (_sha1(fx.settings.db_path), _sha1(fx.settings.universe_db_path)) == before


def test_the_subcommand_prints_exactly_one_line(
    fx: Fx, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fx.conn.close()
    # Settings.load() calls load_dotenv() and requires KG_FINANCIAL_DB before --db overrides it:
    # set the environment so the test needs no .env
    monkeypatch.setenv("KG_FINANCIAL_DB", str(fx.settings.db_path))
    monkeypatch.setenv("KG_UNIVERSE_DB", str(fx.settings.universe_db_path))
    monkeypatch.setattr(pending, "PricingClient", lambda _url: FakeGateway())
    monkeypatch.setattr(
        pending, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: NOW)})
    )
    code = cli.main(
        [
            "pending",
            "--analysis-date",
            D,
            "--db",
            str(fx.settings.db_path),
            "--universe-db",
            str(fx.settings.universe_db_path),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0 and out == f"pending: no 2/2 members at {D}\n"
