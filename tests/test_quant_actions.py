"""Corporate-actions backfill: the pricing gateway is the only source (T-085).

Hermetic -- an ``httpx.MockTransport`` stands in for ``portfolio-data-mining``'s
``GET /pricing/{ticker}/actions``. There is no fallback to derive dividends from filings:
a gateway that cannot serve fails the run, and an asset it cannot serve gets no rows.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from portfolio_common.db import Database

import quant.actions
from quant.actions import GatewayUnavailable, backfill_corporate_actions
from quant.cli import _run_backfill_actions, build_parser
from quant.config import QuantSettings
from quant.db import ActionsReport, load_actions
from quant.pricing_client import QuantPricingClient

_WINDOW = {"date_from": "2024-01-01", "date_to": "2024-12-31"}


def _settings(**over: Any) -> QuantSettings:
    return QuantSettings(db_path=Path(":memory:"), **over)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("quant.pricing_client.time.sleep", lambda _s: None)


def _gateway(
    behaviour: dict[str, str] | None = None, *, probe_ok: bool = True
) -> tuple[QuantPricingClient, httpx.Client, list[str]]:
    """A gateway client on a MockTransport. *behaviour* maps ticker -> ``ok`` (one
    dividend), ``warn`` (yfinance failed: empty lists + a ``warning``) or ``error``
    (HTTP 503); unlisted tickers are ``ok``. The 1900 probe range is answered ``ok``
    unless *probe_ok* is false. Also returns the httpx client and the request paths seen."""
    behaviour = behaviour or {}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        ticker = request.url.path.split("/")[2]
        body: dict[str, Any] = {
            "ticker": ticker,
            "source": "yfinance",
            "splits": [],
            "warning": None,
        }
        if request.url.params["start_date"] == "1900-01-01":  # QuantPricingClient.probe
            if not probe_ok:
                return httpx.Response(503)
            return httpx.Response(200, json={**body, "dividends": []})
        how = behaviour.get(ticker, "ok")
        if how == "error":
            return httpx.Response(503)
        if how == "warn":
            body["warning"] = "yfinance failed to return corporate actions: boom"
            return httpx.Response(200, json={**body, "dividends": []})
        return httpx.Response(
            200, json={**body, "dividends": [{"date": "2024-03-15", "value": 0.5}]}
        )

    http = httpx.Client(base_url="http://gateway.test", transport=httpx.MockTransport(handler))
    return QuantPricingClient("http://gateway.test", max_retries=1, client=http), http, seen


def _engine_counts(conn: Database) -> dict[tuple[int, str], int]:
    return {
        (int(r["asset_id"]), str(r["engine_version"])): int(r["n"])
        for r in conn.execute(
            "SELECT asset_id, engine_version, COUNT(*) AS n FROM corporate_action "
            "GROUP BY asset_id, engine_version"
        )
    }


def _last_run(conn: Database) -> tuple[str, str | None, dict[str, Any]]:
    row = conn.execute(
        "SELECT status, error, params_json FROM quant_run ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return str(row[0]), row[1], dict(json.loads(row[2]))


def _insert_action(conn: Database, asset_id: int, ex_date: str, value: float, engine: str) -> None:
    conn.execute(
        "INSERT INTO corporate_action (asset_id, action_type, ex_date, value, source, "
        "engine_version, ingested_at) VALUES (?, 'DIVIDEND', ?, ?, 'test', ?, ?)",
        (asset_id, ex_date, value, engine, ex_date + "T00:00:00Z"),
    )
    conn.commit()


# -- the scope: one source ---------------------------------------------------------------


def test_the_gateway_is_the_only_source() -> None:
    assert "source" not in inspect.signature(backfill_corporate_actions).parameters
    with pytest.raises(SystemExit):  # the flag is gone, not merely defaulted
        build_parser().parse_args(["backfill-actions", "--source", "derive"])
    assert build_parser().parse_args(["backfill-actions"]).command == "backfill-actions"


def test_quant_does_not_mine_dividends_from_filings() -> None:
    """Data acquisition belongs to portfolio-data-mining; pin that this package does not
    grow a second source back."""
    source = Path(quant.actions.__file__).read_text()
    assert "financial_facts" not in source
    assert not hasattr(quant.actions, "derive_corporate_actions_from_facts")
    assert not hasattr(quant.actions, "derive_quarterly_dividends_from_10q_ytd")


def test_load_actions_reads_only_the_gateway_engines_newest_first(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=1, n_days=260, with_dividends=False)
    args = {"start": "2024-01-01", "end": "2024-12-31"}
    # rows the retired XBRL-derived engines wrote before T-085: history, never read
    _insert_action(conn, 1, "2024-03-31", 9.0, "corpact-v0-approx")
    _insert_action(conn, 1, "2024-06-30", 8.0, "corpact-v1-derived")
    assert load_actions(conn, 1, "DIVIDEND", **args) == {}

    _insert_action(conn, 1, "2024-03-15", 0.5, "corpact-v1")
    assert load_actions(conn, 1, "DIVIDEND", **args) == {"2024-03-15": 0.5}

    _insert_action(conn, 1, "2024-03-14", 0.6, "corpact-v2")  # a newer gateway engine wins
    assert load_actions(conn, 1, "DIVIDEND", **args) == {"2024-03-14": 0.6}


# -- a healthy gateway -------------------------------------------------------------------


def test_gateway_run_writes_corpact_v1_rows_and_completes(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=4, n_days=260, with_dividends=False)
    client, _http, _seen = _gateway()
    report = backfill_corporate_actions(_settings(), conn=conn, client=client, **_WINDOW)
    assert report.engine_version == "corpact-v1"
    assert (report.assets_seen, report.assets_fetched, report.errors) == (4, 4, [])
    assert (report.dividends, report.splits, report.inserted) == (4, 0, 4)
    assert _engine_counts(conn) == {(a, "corpact-v1"): 1 for a in (1, 2, 3, 4)}
    assert load_actions(conn, 1, "DIVIDEND", start="2024-01-01", end="2024-12-31") == {
        "2024-03-15": 0.5
    }
    status, error, params = _last_run(conn)
    assert (status, error) == ("completed", None)
    assert params["source"] == "gateway" and params["assets_fetched"] == 4

    again = backfill_corporate_actions(_settings(), conn=conn, client=client, **_WINDOW)
    assert again.inserted == 0  # INSERT OR IGNORE on the versioned key


def test_a_dotted_ticker_reaches_the_gateway_in_the_yfinance_spelling(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=2, n_days=260, with_dividends=False)
    conn.execute("UPDATE assets SET ticker = 'BF.B' WHERE id = 1")
    conn.commit()
    # The point-in-time universe is resolved by symbol, so rename it there too.
    universe = sqlite3.connect(os.environ["KG_UNIVERSE_DB"])
    universe.execute("UPDATE universe_membership SET symbol = 'BF.B' WHERE symbol = 'AS01'")
    universe.commit()
    universe.close()
    client, _http, seen = _gateway()
    backfill_corporate_actions(_settings(), conn=conn, client=client, **_WINDOW)
    assert "/pricing/BF-B/actions" in seen
    assert "/pricing/BF.B/actions" not in seen


def test_an_injected_client_is_left_open_for_its_owner(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=2, n_days=260, with_dividends=False)
    client, http, _seen = _gateway()
    backfill_corporate_actions(_settings(), conn=conn, client=client, **_WINDOW)
    assert http.is_closed is False
    client.close()


# -- a gateway that cannot serve one asset: no rows, no fallback --------------------------


def test_an_isolated_gateway_error_writes_no_rows_for_that_asset_only(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=4, n_days=260, with_dividends=False)
    client, _http, _seen = _gateway({"AS02": "error"})
    report = backfill_corporate_actions(_settings(), conn=conn, client=client, **_WINDOW)
    assert (report.assets_seen, report.assets_fetched) == (4, 3)
    assert len(report.errors) == 1 and report.errors[0].startswith("AS02")
    assert set(_engine_counts(conn)) == {(1, "corpact-v1"), (3, "corpact-v1"), (4, "corpact-v1")}
    status, _error, params = _last_run(conn)
    assert status == "completed" and params["assets_errored"] == 1


def test_a_warning_is_an_error_for_that_asset_never_a_clean_empty(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    """yfinance failing upstream arrives as 200 + empty lists + a warning; recording it
    as "paid nothing" is the silent failure this consumer exists to prevent."""
    conn = quant_seed(memory_quant_db, n_assets=3, n_days=260, with_dividends=False)
    client, _http, _seen = _gateway({"AS03": "warn"})
    report = backfill_corporate_actions(_settings(), conn=conn, client=client, **_WINDOW)
    assert (report.assets_fetched, len(report.errors)) == (2, 1)
    assert "boom" in report.errors[0]
    assert (3, "corpact-v1") not in _engine_counts(conn)


def test_warnings_never_open_the_breaker(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    """A global yfinance outage answers fast for every ticker: every asset is listed as an
    error, but the breaker -- meant for the slow failure -- stays closed."""
    conn = quant_seed(memory_quant_db, n_assets=4, n_days=260, with_dividends=False)
    client, _http, seen = _gateway({f"AS0{i}": "warn" for i in range(1, 5)})
    report = backfill_corporate_actions(
        _settings(gateway_max_consecutive_failures=1), conn=conn, client=client, **_WINDOW
    )
    assert (report.assets_seen, report.assets_fetched, len(report.errors)) == (4, 0, 4)
    assert len(seen) == 1 + 4
    assert _last_run(conn)[0] == "completed"


def test_a_success_resets_the_streak_so_scattered_errors_never_open_the_breaker(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=4, n_days=260, with_dividends=False)
    client, _http, seen = _gateway({"AS01": "error", "AS03": "error"})
    report = backfill_corporate_actions(
        _settings(gateway_max_consecutive_failures=2), conn=conn, client=client, **_WINDOW
    )
    assert (report.assets_fetched, len(report.errors)) == (2, 2)
    assert len(seen) == 1 + 4  # the probe, then every asset was still tried


# -- a gateway that cannot serve at all: the run fails -----------------------------------


def test_a_failed_probe_fails_the_run_and_writes_nothing(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=3, n_days=260, with_dividends=False)
    client, _http, seen = _gateway(probe_ok=False)
    with pytest.raises(GatewayUnavailable, match="probe"):
        backfill_corporate_actions(_settings(), conn=conn, client=client, **_WINDOW)
    assert len(seen) == 1  # the probe; no asset was tried
    assert _engine_counts(conn) == {}  # and nothing was derived in its place
    status, error, _params = _last_run(conn)
    assert status == "failed" and error and "probe" in error


def test_the_breaker_fails_the_run_after_k_consecutive_errors_and_keeps_earlier_rows(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=5, n_days=260, with_dividends=False)
    client, _http, seen = _gateway({"AS02": "error", "AS03": "error"})
    with pytest.raises(GatewayUnavailable, match="circuit breaker"):
        backfill_corporate_actions(
            _settings(gateway_max_consecutive_failures=2), conn=conn, client=client, **_WINDOW
        )
    assert len(seen) == 4  # the probe, AS01, AS02, AS03 -- AS04 and AS05 were never called
    assert set(_engine_counts(conn)) == {(1, "corpact-v1")}  # AS01's row is kept
    status, error, params = _last_run(conn)
    assert status == "failed" and error and "2 of 5 assets not fetched" in error
    assert params["assets_seen"] == 3 and params["assets_fetched"] == 1
    assert params["assets_errored"] == 2 and params["gateway_max_consecutive_failures"] == 2


# -- the CLI: exit codes ------------------------------------------------------------------


def _args() -> argparse.Namespace:
    return argparse.Namespace(date_from="2024-01-01")


def test_the_cli_exits_1_when_the_gateway_cannot_serve(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def down(*_a: object, **_k: object) -> ActionsReport:
        raise GatewayUnavailable("no gateway")

    monkeypatch.setattr("quant.cli.backfill_corporate_actions", down)
    assert _run_backfill_actions(_settings(), _args(), "2024-12-31") == 1
    assert "no gateway" in capsys.readouterr().err


@pytest.mark.parametrize(("errors", "expected"), [([], 0), (["AS02: boom"], 1)])
def test_the_cli_exits_1_when_any_asset_has_no_data(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    errors: list[str],
    expected: int,
) -> None:
    report = ActionsReport(
        engine_version="corpact-v1", assets_seen=4, assets_fetched=4 - len(errors)
    )
    report.errors = errors
    monkeypatch.setattr("quant.cli.backfill_corporate_actions", lambda *_a, **_k: report)
    assert _run_backfill_actions(_settings(), _args(), "2024-12-31") == expected
    assert "backfill-actions [gateway]" in capsys.readouterr().out
