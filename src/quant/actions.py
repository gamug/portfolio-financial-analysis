"""Corporate-actions backfill: the pricing gateway, and only the pricing gateway.

Dividends and splits are data acquisition, and acquiring data is
``portfolio-data-mining``'s job -- its yfinance-backed
``GET /pricing/{ticker}/actions`` route (that repo's ``T-020``-``T-025``). This
package *consumes* it and mines nothing itself (T-085): there is no fallback that
re-derives dividends from filings, and no other source. The XBRL-derived engines
(``corpact-v0-approx``, ``corpact-v1-derived``) that used to live here were removed
with that scope decision; rows they wrote earlier stay in the append-only table but
:func:`quant.db.load_actions` no longer reads them.

Consequently a gateway that cannot serve is a failure, not a degraded run:

- the run **fails** (:class:`GatewayUnavailable`, ``quant_run.status = 'failed'``,
  CLI exit 1) when the probe of the first asset fails, or when the circuit breaker
  opens after ``gateway_max_consecutive_failures`` consecutive gateway *errors* --
  each of those has already paid ``max_retries`` x the timeout, so continuing would
  cost hours to write nothing. Rows already written stay (``INSERT OR IGNORE``), so a
  re-run resumes cheaply once the gateway is back;
- a single asset the gateway cannot serve (an ``ActionsUnavailable`` -- upstream's
  200 + empty lists + ``warning`` when yfinance failed -- or ``ActionsNotSupported``,
  or one isolated error) writes **no rows** and is listed in ``ActionsReport.errors``;
  the run still completes, and the CLI exits 1 because the data is incomplete.
"""

from __future__ import annotations

from portfolio_common.db import Database

from kg_schema import connect
from kg_schema.provenance import code_version
from quant.config import QuantSettings
from quant.db import (
    ActionsReport,
    CorporateAction,
    ensure_schema,
    load_assets,
    upsert_corporate_actions,
)
from quant.pricing_client import (
    ActionsNotSupported,
    ActionsUnavailable,
    GatewayError,
    QuantPricingClient,
)
from quant.state import fail_run, finish_run, open_run, set_run_params

_MAX_ERROR_MESSAGES = 20  # per-run cap on the messages kept in quant_run.params_json


class GatewayUnavailable(RuntimeError):
    """The pricing gateway cannot serve corporate actions, and there is no other source."""


def fetch_corporate_actions_gateway(
    client: QuantPricingClient, asset_id: int, ticker: str, start: str, end: str
) -> list[CorporateAction]:
    raw = client.actions(ticker, start, end)
    out: list[CorporateAction] = [
        CorporateAction(asset_id, "DIVIDEND", d.ex_date, d.value, source="pricing-gateway")
        for d in raw.dividends
    ]
    out += [
        CorporateAction(asset_id, "SPLIT", s.ex_date, s.value, source="pricing-gateway")
        for s in raw.splits
    ]
    return out


class _GatewayFeed:
    """The gateway client plus the run's circuit breaker.

    Only :class:`GatewayError` -- the slow failure, where every call has already
    paid ``max_retries`` x the timeout -- counts toward the breaker. A per-ticker
    :class:`ActionsUnavailable` / :class:`ActionsNotSupported` means the gateway
    *answered* (it is up; that one ticker has no usable data), so it resets the
    streak instead of extending it, and a global yfinance outage -- which answers
    fast -- lists every asset as an error without tripping the breaker.
    """

    def __init__(self, client: QuantPricingClient, max_consecutive_failures: int) -> None:
        self._client = client
        self._max_failures = max_consecutive_failures
        self._consecutive_failures = 0

    @property
    def is_open(self) -> bool:
        return self._consecutive_failures >= self._max_failures

    def fetch(self, asset_id: int, ticker: str, start: str, end: str) -> list[CorporateAction]:
        try:
            rows = fetch_corporate_actions_gateway(self._client, asset_id, ticker, start, end)
        except GatewayError:
            self._consecutive_failures += 1
            raise
        except (ActionsUnavailable, ActionsNotSupported):
            self._consecutive_failures = 0
            raise
        self._consecutive_failures = 0
        return rows


def _run_params(
    report: ActionsReport, *, date_from: str, date_to: str, max_failures: int
) -> dict[str, object]:
    return {
        "analysis_date": date_to,
        "date_from": date_from,
        "date_to": date_to,
        "source": "gateway",
        "gateway_max_consecutive_failures": max_failures,
        "assets_seen": report.assets_seen,
        "assets_fetched": report.assets_fetched,
        "assets_errored": len(report.errors),
        "errors": report.errors[:_MAX_ERROR_MESSAGES],
    }


def _fetch_all(  # noqa: PLR0913 - the run's identity + injected collaborators, all keyword-only
    conn: Database,
    client: QuantPricingClient,
    report: ActionsReport,
    assets: list[tuple[int, str]],
    *,
    date_from: str,
    date_to: str,
    max_failures: int,
) -> None:
    """Fetch every asset's actions into ``corporate_action``. Raises
    :class:`GatewayUnavailable` when the probe fails or the breaker opens."""
    if assets and not client.probe(assets[0][1]):
        raise GatewayUnavailable(
            f"the pricing gateway does not serve usable corporate actions (probe of "
            f"{assets[0][1]} failed): route missing, gateway down, or yfinance failing upstream"
        )
    feed = _GatewayFeed(client, max_failures)
    for asset_id, ticker in assets:
        report.assets_seen += 1
        try:
            rows = feed.fetch(asset_id, ticker, date_from, date_to)
        except (ActionsUnavailable, ActionsNotSupported) as exc:
            report.errors.append(f"{ticker}: {exc}")
            continue
        except GatewayError as exc:
            report.errors.append(f"{ticker}: {exc}")
            if feed.is_open:
                raise GatewayUnavailable(
                    f"circuit breaker opened after {max_failures} consecutive gateway errors "
                    f"(last: {ticker}); {len(assets) - report.assets_seen} of {len(assets)} "
                    f"assets not fetched. Re-run when the gateway is back -- rows already "
                    f"written are kept."
                ) from exc
            continue
        report.assets_fetched += 1
        report.dividends += sum(1 for r in rows if r.action_type == "DIVIDEND")
        report.splits += sum(1 for r in rows if r.action_type == "SPLIT")
        report.inserted += upsert_corporate_actions(
            conn, rows, engine_version=report.engine_version
        )


def backfill_corporate_actions(
    settings: QuantSettings,
    *,
    date_from: str,
    date_to: str,
    conn: Database | None = None,
    client: QuantPricingClient | None = None,
) -> ActionsReport:
    """Backfill dividends/splits into ``corporate_action`` from the pricing gateway.

    Raises :class:`GatewayUnavailable` (after recording the run as failed) when the
    gateway cannot serve at all; per-asset failures are returned in
    ``ActionsReport.errors``. *client* injects a gateway client (tests); one passed
    in is not closed here.
    """
    owns = conn is None
    conn = conn or connect(settings.db_path)
    try:
        ensure_schema(conn)
        assets = load_assets(
            conn,
            universe=settings.universe,
            as_of=date_to,
            universe_db_path=settings.universe_db_path,
        )
        report = ActionsReport(engine_version=settings.corpact_engine_version)
        max_failures = settings.gateway_max_consecutive_failures
        run_id = open_run(
            conn,
            "backfill-actions",
            as_of=date_to,
            params=_run_params(
                report, date_from=date_from, date_to=date_to, max_failures=max_failures
            ),
            code_version=code_version(),
        )
        owns_client = client is None
        client = client or QuantPricingClient(settings.pricing_base_url)
        try:
            _fetch_all(
                conn,
                client,
                report,
                assets,
                date_from=date_from,
                date_to=date_to,
                max_failures=max_failures,
            )
        except Exception as exc:
            set_run_params(
                conn,
                run_id,
                _run_params(
                    report, date_from=date_from, date_to=date_to, max_failures=max_failures
                ),
            )
            fail_run(conn, run_id, str(exc))
            raise
        finally:
            if owns_client:
                client.close()
        set_run_params(
            conn,
            run_id,
            _run_params(report, date_from=date_from, date_to=date_to, max_failures=max_failures),
        )
        finish_run(conn, run_id)
        return report
    finally:
        if owns:
            conn.close()
