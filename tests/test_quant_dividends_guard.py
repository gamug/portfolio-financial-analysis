"""``build-returns`` refuses to run without a clean gateway backfill behind it (T-086).

``quant_return_daily`` is ``INSERT OR IGNORE`` per ``(asset, day, engine_version)``, so a
series built while dividends are missing is price-only *and* locks in under that version.
Hermetic -- the gateway is an ``httpx.MockTransport``.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from portfolio_common.db import Database

from quant.actions import (
    DividendsNotReady,
    backfill_corporate_actions,
    dividends_not_ready_reason,
)
from quant.cli import _run_build_returns, build_parser
from quant.config import QuantSettings
from quant.pricing_client import QuantPricingClient
from quant.returns import ReturnsReport, run_build_returns

_WINDOW = {"date_from": "2024-01-01", "date_to": "2024-12-31"}


def _settings() -> QuantSettings:
    return QuantSettings(db_path=Path(":memory:"))


def _record_backfill(
    conn: Database,
    *,
    date_from: str = "2024-01-01",
    date_to: str = "2024-12-31",
    errored: int | None = 0,
    status: str = "completed",
) -> None:
    """A ``backfill-actions`` quant_run row. ``errored=None`` omits the key, as a run
    recorded before T-085 (the derive path) would."""
    params: dict[str, Any] = {"source": "gateway", "date_from": date_from, "date_to": date_to}
    if errored is not None:
        params.update(assets_seen=3, assets_fetched=3 - errored, assets_errored=errored)
    conn.execute(
        "INSERT INTO quant_run (command, as_of, started_at, status, engine_version, params_json) "
        "VALUES ('backfill-actions', ?, '2026-09-21T00:00:00Z', ?, 'quant-v1', ?)",
        (date_to, status, json.dumps(params)),
    )
    conn.commit()


def _add_gateway_row(conn: Database) -> None:
    conn.execute(
        "INSERT INTO corporate_action (asset_id, action_type, ex_date, value, source, "
        "engine_version, ingested_at) "
        "VALUES (1, 'DIVIDEND', '2024-03-15', 0.5, 'pricing-gateway', 'corpact-v1', "
        "'2024-03-16T00:00:00Z')"
    )
    conn.commit()


def _build(conn: Database, *, allow_no_dividends: bool = False) -> ReturnsReport:
    return run_build_returns(
        _settings(),
        date_from=_WINDOW["date_from"],
        date_to=_WINDOW["date_to"],
        conn=conn,
        allow_no_dividends=allow_no_dividends,
    )


def _price_only_db(memory_quant_db: Database, quant_seed: Callable[..., Database]) -> Database:
    return quant_seed(memory_quant_db, n_assets=3, n_days=260, with_dividends=False)


def _build_runs(conn: Database) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM quant_run WHERE command = 'build-returns'").fetchone()[0]
    )


def _return_rows(conn: Database) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM quant_return_daily").fetchone()[0])


# -- refusals: nothing may be written ---------------------------------------------------


@pytest.mark.parametrize(
    ("runs", "gateway_row", "expected"),
    [
        pytest.param([], False, "no completed `backfill-actions` run", id="no-run"),
        pytest.param(
            [{"status": "failed"}], False, "no completed `backfill-actions` run", id="failed-run"
        ),
        pytest.param([{"errored": 2}], True, "left 2 asset(s) without data", id="errored-assets"),
        pytest.param(
            [{"date_to": "2024-06-30"}], True, "covers 2024-01-01..2024-12-31", id="ends-too-early"
        ),
        pytest.param(
            [{"date_from": "2024-03-01"}],
            True,
            "covers 2024-01-01..2024-12-31",
            id="starts-too-late",
        ),
        pytest.param(
            [{"errored": None}], True, "predate the gateway-only backfill", id="pre-T-085-run"
        ),
        pytest.param([{}], False, "holds no gateway rows", id="run-but-no-rows"),
    ],
)
def test_it_refuses_and_writes_nothing(
    memory_quant_db: Database,
    quant_seed: Callable[..., Database],
    runs: list[dict[str, Any]],
    gateway_row: bool,
    expected: str,
) -> None:
    conn = _price_only_db(memory_quant_db, quant_seed)
    for kwargs in runs:
        _record_backfill(conn, **kwargs)
    if gateway_row:
        _add_gateway_row(conn)
    with pytest.raises(DividendsNotReady, match=re.escape(expected)):
        _build(conn)
    assert _return_rows(conn) == 0
    assert _build_runs(conn) == 0  # a refusal is not a run


# -- allowed ------------------------------------------------------------------------------


def test_a_clean_covering_backfill_lets_the_build_through(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = _price_only_db(memory_quant_db, quant_seed)
    _record_backfill(conn)
    _add_gateway_row(conn)
    assert dividends_not_ready_reason(conn, **_WINDOW) is None
    report = _build(conn)
    assert report.assets == 3 and report.assets_with_dividends == 1
    assert report.dividends_guard_bypassed is None
    assert _return_rows(conn) > 0


def test_a_later_failed_run_does_not_undo_an_earlier_clean_one(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    """The earlier run's rows are still there (INSERT OR IGNORE keeps them), so a later
    run that failed -- or one that completed with errors -- must not block the build."""
    conn = _price_only_db(memory_quant_db, quant_seed)
    _record_backfill(conn)
    _add_gateway_row(conn)
    _record_backfill(conn, status="failed")
    _record_backfill(conn, errored=1)
    assert dividends_not_ready_reason(conn, **_WINDOW) is None


def test_a_backfill_wider_than_the_build_window_covers_it(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = _price_only_db(memory_quant_db, quant_seed)
    _record_backfill(conn, date_from="2022-01-01", date_to="2026-09-21")
    _add_gateway_row(conn)
    assert dividends_not_ready_reason(conn, **_WINDOW) is None


# -- end to end through the real backfill --------------------------------------------------


def _gateway(*, fail: set[str] | None = None) -> QuantPricingClient:
    fail = fail or set()

    def handler(request: httpx.Request) -> httpx.Response:
        ticker = request.url.path.split("/")[2]
        body: dict[str, Any] = {
            "ticker": ticker,
            "source": "yfinance",
            "splits": [],
            "warning": None,
        }
        if request.url.params["start_date"] == "1900-01-01":
            return httpx.Response(200, json={**body, "dividends": []})
        if ticker in fail:
            body["warning"] = "yfinance failed to return corporate actions: boom"
            return httpx.Response(200, json={**body, "dividends": []})
        return httpx.Response(
            200, json={**body, "dividends": [{"date": "2024-03-15", "value": 0.5}]}
        )

    http = httpx.Client(base_url="http://gateway.test", transport=httpx.MockTransport(handler))
    return QuantPricingClient("http://gateway.test", max_retries=1, client=http)


def test_backfill_then_build_folds_the_dividends_in(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = _price_only_db(memory_quant_db, quant_seed)
    backfill_corporate_actions(_settings(), conn=conn, client=_gateway(), **_WINDOW)
    report = _build(conn)
    assert report.assets_with_dividends == 3


def test_a_backfill_that_left_an_asset_without_data_blocks_the_build(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = _price_only_db(memory_quant_db, quant_seed)
    backfill_corporate_actions(_settings(), conn=conn, client=_gateway(fail={"AS02"}), **_WINDOW)
    with pytest.raises(DividendsNotReady, match="left 1 asset"):
        _build(conn)
    assert _return_rows(conn) == 0


# -- the explicit override ------------------------------------------------------------------


def test_the_override_builds_a_price_only_series_and_records_why(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = _price_only_db(memory_quant_db, quant_seed)
    report = _build(conn, allow_no_dividends=True)
    assert report.assets == 3 and report.assets_with_dividends == 0
    assert report.dividends_guard_bypassed == "no completed `backfill-actions` run is recorded"
    assert _return_rows(conn) > 0
    params = json.loads(
        conn.execute(
            "SELECT params_json FROM quant_run WHERE command = 'build-returns'"
        ).fetchone()[0]
    )
    assert params["allow_no_dividends"] is True
    assert params["dividends_guard_bypassed"] == report.dividends_guard_bypassed


def test_the_override_is_not_recorded_as_a_bypass_when_nothing_was_bypassed(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = _price_only_db(memory_quant_db, quant_seed)
    _record_backfill(conn)
    _add_gateway_row(conn)
    report = _build(conn, allow_no_dividends=True)
    assert report.dividends_guard_bypassed is None


# -- the CLI ---------------------------------------------------------------------------------


def _args(*, allow: bool = False) -> argparse.Namespace:
    return argparse.Namespace(date_from="2024-01-01", allow_no_dividends=allow)


def test_the_flag_exists_and_defaults_off() -> None:
    parser = build_parser()
    assert parser.parse_args(["build-returns"]).allow_no_dividends is False
    assert parser.parse_args(["build-returns", "--allow-no-dividends"]).allow_no_dividends is True


def test_the_cli_exits_1_with_a_remedy_when_the_guard_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*_a: object, **_k: object) -> ReturnsReport:
        raise DividendsNotReady("no completed `backfill-actions` run is recorded")

    monkeypatch.setattr("quant.cli.run_build_returns", refuse)
    assert _run_build_returns(_settings(), _args(), "2024-12-31") == 1
    err = capsys.readouterr().err
    assert "refusing to build" in err and "no completed" in err
    assert "backfill-actions" in err and "--allow-no-dividends" in err


def test_the_cli_warns_loudly_when_the_override_was_used(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def build(*_a: object, **kw: Any) -> ReturnsReport:
        seen.update(kw)
        rep = ReturnsReport("qret-v2")
        rep.dividends_guard_bypassed = "no completed `backfill-actions` run is recorded"
        return rep

    monkeypatch.setattr("quant.cli.run_build_returns", build)
    assert _run_build_returns(_settings(), _args(allow=True), "2024-12-31") == 0
    assert seen["allow_no_dividends"] is True
    captured = capsys.readouterr()
    assert "WARNING" in captured.err and "locked in under qret-v2" in captured.err
    assert "build-returns [qret-v2]" in captured.out
