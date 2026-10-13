"""``quant pending`` (T-017): the quant chain behind its inputs, read-only, gateway GETs only."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from conftest import cik_for, write_universe_db

from kg_schema import connect
from pricing_agent import db as pricing_db
from quant import cli, pending
from quant.config import QuantSettings
from quant.db import (
    CorporateAction,
    PortfolioRow,
    ReturnRow,
    ensure_schema,
    insert_portfolio,
    sync_positions,
    upsert_benchmark_performance,
    upsert_corporate_actions,
    upsert_return_daily,
)
from quant.evaluate import LIVE_BOOK_KIND, PERF_ENGINE_VERSION
from quant.pricing_client import (
    ActionsUnavailable,
    GatewayError,
    RawAction,
    RawActions,
)

D = "2026-10-09"
PRIOR = "2026-10-08"


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()  # noqa: S324


class Gateway:
    def __init__(self) -> None:
        self.dividends: dict[str, list[RawAction]] = {}
        self.unavailable: set[str] = set()
        self.down = False
        self.calls: list[tuple[str, str, str]] = []

    def actions(self, ticker: str, start: str, end: str) -> RawActions:
        self.calls.append((ticker, start, end))
        if self.down:
            raise GatewayError("/pricing failed after 3 attempts")
        if ticker in self.unavailable:
            raise ActionsUnavailable(f"{ticker}: yfinance failed")
        return RawActions(ticker, self.dividends.get(ticker, []), [])

    def close(self) -> None:
        return None


class Fx:
    def __init__(self, tmp_path: Path) -> None:
        self.settings = QuantSettings(
            db_path=tmp_path / "fin.db",
            universe_db_path=write_universe_db(
                tmp_path / "u.db", [("AAA", "2020-01-01", None), ("BBB", "2020-01-01", None)]
            ),
        )
        self.conn = connect(self.settings.db_path)
        pricing_db.ensure_schema(self.conn)  # assets, price_daily (quant reads, never creates)
        ensure_schema(self.conn)
        self.ids = {}
        for t in ("AAA", "BBB"):
            cur = self.conn.execute(
                "INSERT INTO assets (ticker, company_name, cik) VALUES (?, ?, ?)",
                (t, t, cik_for(t)),
            )
            self.ids[t] = int(cur.lastrowid or 0)
        self.conn.commit()
        self.gateway = Gateway()

    def prices_and_returns(self, *dates: str, returns: bool = True) -> None:
        for aid in self.ids.values():
            for d in dates:
                self.conn.execute(
                    "INSERT INTO price_daily (asset_id, date, close) VALUES (?, ?, 1.0)", (aid, d)
                )
            if returns:
                upsert_return_daily(
                    self.conn,
                    aid,
                    [ReturnRow(d, 1, 1, 1, 0, 1, 0.01, 0.01) for d in dates],
                    engine_version=self.settings.return_engine_version,
                )
        self.conn.commit()

    def book(self, kind: str, as_of: str, *, status: str = "optimal", weights: bool = True) -> int:
        pid = insert_portfolio(
            self.conn,
            PortfolioRow(
                as_of=as_of,
                kind=kind,
                objective=kind,
                solver="x",
                status=status,
                expected_return=None,
                expected_vol=None,
                sharpe=None,
                rf_annual=None,
                n_positions=2,
                engine_version="opt-v2+x",
            ),
        )
        if weights:
            sync_positions(self.conn, pid, as_of, {self.ids["AAA"]: 0.5, self.ids["BBB"]: 0.5})
        self.conn.commit()
        return pid

    def books(self, as_of: str, **kw: object) -> None:
        for kind in ("min_var", "tangency", "target_vol", "risk_parity"):
            self.book(kind, as_of, **kw)  # type: ignore[arg-type]

    def dividend(self, ticker: str, ex_date: str) -> None:
        upsert_corporate_actions(
            self.conn,
            [CorporateAction(self.ids[ticker], "DIVIDEND", ex_date, 0.5)],
            engine_version=self.settings.corpact_engine_version,
        )

    def check(self, date: str = D) -> tuple[bool, str]:
        self.conn.commit()
        ro = connect(self.settings.db_path, read_only=True)
        try:
            return pending.check(ro, self.settings, date, client=self.gateway)  # type: ignore[arg-type]
        finally:
            ro.close()


@pytest.fixture
def fx(tmp_path: Path) -> Fx:
    f = Fx(tmp_path)
    f.prices_and_returns(PRIOR, D)
    f.books(D)
    return f


def test_a_fresh_database_is_pending(tmp_path: Path) -> None:
    s = QuantSettings(
        db_path=tmp_path / "x.db",
        universe_db_path=write_universe_db(tmp_path / "u.db", [("AAA", "2020-01-01", None)]),
    )
    assert pending.pending_line(s, D, client=Gateway()).startswith("pending: yes")  # type: ignore[arg-type]
    empty = connect(tmp_path / "e.db")
    empty.close()
    line = pending.pending_line(
        s.model_copy(update={"db_path": tmp_path / "e.db"}),
        D,
        client=Gateway(),  # type: ignore[arg-type]
    )  # type: ignore[arg-type]
    assert line.startswith("pending: yes no price_daily table")


def test_complete_is_not_pending(fx: Fx) -> None:
    assert fx.check() == (False, f"returns, 4 books and actions current at {D} (2 members)")
    assert fx.gateway.calls[0][1:] == ("2022-01-01", D)  # the step's window, clamped to D


def test_returns_that_lag_the_prices_are_pending_without_a_gateway_call(fx: Fx) -> None:
    fx.prices_and_returns("2026-10-12", returns=False)  # a later bar, no return yet
    assert fx.check("2026-10-12")[1].startswith(f"returns end {D}, prices 2026-10-12")
    assert fx.gateway.calls == []


def test_a_failed_chain_with_no_book_is_pending(fx: Fx) -> None:
    fx.conn.execute("DELETE FROM quant_portfolio WHERE kind = 'tangency'")
    pending_, evidence = fx.check()
    assert pending_ and evidence == f"no book for tangency in [{D}, {D}]"


def test_a_book_at_the_last_session_answers_a_later_date_without_a_new_session(fx: Fx) -> None:
    assert fx.check("2026-10-10")[0] is False  # a Saturday: S = D's Friday
    assert fx.check("2026-10-11")[0] is False


def test_a_book_before_the_last_session_does_not(tmp_path: Path) -> None:
    f = Fx(tmp_path)
    f.prices_and_returns(PRIOR, D)
    f.books(PRIOR)  # the chain last ran for the previous session
    pending_, evidence = f.check()
    assert pending_ and evidence.startswith("no book for min_var, tangency, target_vol")


def test_an_infeasible_book_is_an_answer_not_a_gap(fx: Fx) -> None:
    fx.conn.execute("UPDATE quant_portfolio SET status = 'infeasible' WHERE kind = 'target_vol'")
    assert fx.check()[0] is False


def test_evaluate_that_lags_a_scorable_book_is_pending(tmp_path: Path) -> None:
    f = Fx(tmp_path)
    f.prices_and_returns(PRIOR, D)
    f.books(PRIOR)
    f.books(D)  # the chain ran for D too
    pending_, evidence = f.check()
    assert pending_ and evidence.startswith("evaluate lags for 4 books (min_var#1")
    pid = int(f.conn.execute("SELECT MIN(id) FROM quant_portfolio").fetchone()[0])
    for book in f.conn.execute(
        "SELECT id FROM quant_portfolio WHERE as_of = ?", (PRIOR,)
    ).fetchall():
        upsert_benchmark_performance(
            f.conn,
            int(book[0]),
            [(D, 0.01, 0.01, "b", 0.0, 0.01)],
            engine_version=PERF_ENGINE_VERSION,
        )
    assert pid and f.check()[0] is False


def test_an_empty_or_infeasible_book_with_nothing_else_pending_is_unscorable_not_pending(
    tmp_path: Path,
) -> None:
    f = Fx(tmp_path)
    f.prices_and_returns(PRIOR, D)
    f.books(D)
    f.book(LIVE_BOOK_KIND, PRIOR, status="snapshot", weights=False)  # no weights: nothing to score
    f.book("min_var", PRIOR, status="infeasible", weights=False)
    pending_, evidence = f.check()
    assert not pending_
    assert "2 unscorable (live_book#5, min_var#6)" in evidence


def test_a_dividend_the_gateway_lists_and_the_database_lacks_is_pending(fx: Fx) -> None:
    fx.dividend("AAA", "2026-08-01")
    fx.gateway.dividends["AAA"] = [RawAction("2026-08-01", 0.5), RawAction(D, 0.6)]
    pending_, evidence = fx.check()
    assert pending_ and evidence == f"AAA has DIVIDEND {D} at the gateway, not in corporate_action"


def test_no_lookahead_the_gateway_is_asked_only_up_to_the_date(tmp_path: Path) -> None:
    f = Fx(tmp_path)
    f.prices_and_returns(PRIOR, D)
    f.books(PRIOR)
    assert f.check(PRIOR)[0] is False  # the later bar and return at D are not counted
    assert {end for _, _, end in f.gateway.calls} == {PRIOR}


def test_an_unavailable_member_is_listed_not_pending(fx: Fx) -> None:
    fx.gateway.unavailable.add("BBB")
    assert fx.check() == (
        False,
        f"returns, 4 books and actions current at {D} (2 members); 1 unavailable (BBB)",
    )


def test_a_gateway_outage_is_not_swallowed(fx: Fx) -> None:
    fx.gateway.down = True
    with pytest.raises(GatewayError):  # the CLI exits non-zero; the runner counts that as pending
        fx.check()


def test_the_check_writes_nothing(fx: Fx) -> None:
    fx.conn.commit()
    fx.conn.close()
    paths = (fx.settings.db_path, fx.settings.universe_db_path)
    before = [_sha1(p) for p in paths]
    pending.pending_line(fx.settings, D, client=fx.gateway)  # type: ignore[arg-type]
    assert [_sha1(p) for p in paths] == before


def test_the_subcommand_prints_exactly_one_line(
    fx: Fx, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fx.conn.commit()
    fx.conn.close()
    # QuantSettings.load() calls load_dotenv() and requires KG_FINANCIAL_DB before --db overrides
    # it: set the environment so the test needs no .env
    monkeypatch.setenv("KG_FINANCIAL_DB", str(fx.settings.db_path))
    monkeypatch.setenv("KG_UNIVERSE_DB", str(fx.settings.universe_db_path))
    monkeypatch.setattr(pending, "QuantPricingClient", lambda _url: fx.gateway)
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
    assert code == 0
    assert (
        capsys.readouterr().out
        == f"pending: no returns, 4 books and actions current at {D} (2 members)\n"
    )


def test_the_versions_come_from_the_settings_not_from_literals() -> None:
    source = Path(pending.__file__).read_text()
    for literal in ("qret-v", "corpact-v", "priceobs-v", "perf-v"):
        assert literal not in source
