"""Corporate-actions backfill: pricing-gateway probe, XBRL-derived fallback.

The gateway path (``--source gateway``) gives true ex-dates when the deployment
serves actions. The derive path (``--source derive``) is coarse on purpose: it
takes the fiscal-year cash dividend per share from ``financial_facts`` and spreads
it across four synthetic quarterly ex-dates, so it has the wrong intra-year timing
and no special dividends. Rows land under ``engine_version = 'corpact-v0-approx'``
so a better source can supersede them later.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date

from quant.config import QuantSettings
from quant.db import (
    ActionsReport,
    CorporateAction,
    connect,
    ensure_schema,
    load_assets,
    upsert_corporate_actions,
)
from quant.pricing_client import ActionsNotSupported, QuantPricingClient
from quant.state import fail_run, finish_run, open_run

DERIVED_ENGINE_VERSION = "corpact-v0-approx"

# Fiscal-year cash dividend per share, best signal first.
_DPS_CONCEPTS = (
    "us-gaap_CommonStockDividendsPerShareDeclared",
    "us-gaap_CommonStockDividendsPerShareCashPaid",
)
_PERIOD_KEY_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _minus_months(d: date, months: int) -> date:
    m = d.month - 1 - months
    year = d.year + m // 12
    month = m % 12 + 1
    # clamp day to the shortest month we might land on
    day = min(d.day, 28)
    return date(year, month, day)


def _fy_dps(conn: sqlite3.Connection, filing_id: int, period_end: str) -> float | None:
    for concept in _DPS_CONCEPTS:
        row = conn.execute(
            "SELECT value FROM financial_facts "
            "WHERE filing_id = ? AND concept = ? AND period_key LIKE ? AND value IS NOT NULL "
            "ORDER BY period_key DESC LIMIT 1",
            (filing_id, concept, f"{period_end}%(FY)%"),
        ).fetchone()
        if row and row["value"] and float(row["value"]) > 0:
            return float(row["value"])
    return None


def derive_corporate_actions_from_facts(
    conn: sqlite3.Connection, asset_id: int
) -> list[CorporateAction]:
    """Four synthetic quarterly dividends per fiscal year with a recorded DPS."""
    filings = conn.execute(
        "SELECT id, period_end FROM sec_filings "
        "WHERE asset_id = ? AND form = '10-K' AND period_end IS NOT NULL ORDER BY period_end",
        (asset_id,),
    ).fetchall()
    by_ex_date: dict[str, CorporateAction] = {}
    for f in filings:
        period_end = str(f["period_end"])
        m = _PERIOD_KEY_DATE.match(period_end)
        if not m:
            continue
        annual_dps = _fy_dps(conn, int(f["id"]), period_end)
        if annual_dps is None:
            continue
        end = date.fromisoformat(m.group(1))
        per_quarter = round(annual_dps / 4.0, 6)
        for q in range(4):
            ex = _minus_months(end, 3 * q).isoformat()
            by_ex_date[ex] = CorporateAction(
                asset_id=asset_id,
                action_type="DIVIDEND",
                ex_date=ex,
                value=per_quarter,
                frequency="quarterly",
                source="financial-facts-derived",
            )
    return sorted(by_ex_date.values(), key=lambda a: a.ex_date)


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


def backfill_corporate_actions(
    settings: QuantSettings,
    *,
    date_from: str,
    date_to: str,
    source: str = "derive",
    conn: sqlite3.Connection | None = None,
) -> ActionsReport:
    owns = conn is None
    conn = conn or connect(settings.db_path)
    try:
        ensure_schema(conn)
        assets = load_assets(conn, universe=settings.universe, as_of=date_to)
        use_gateway = source == "gateway"
        client: QuantPricingClient | None = None
        report = ActionsReport(source=source, engine_version=DERIVED_ENGINE_VERSION)

        if use_gateway:
            client = QuantPricingClient(settings.pricing_base_url)
            if not (assets and client.probe(assets[0][1])):
                report.gateway_probe_failed = True
                use_gateway = False
                client.close()
                client = None

        report.source = "gateway" if use_gateway else "derive"
        report.engine_version = (
            settings.corpact_engine_version if use_gateway else DERIVED_ENGINE_VERSION
        )
        run_id = open_run(
            conn,
            "backfill-actions",
            params={
                "date_from": date_from,
                "date_to": date_to,
                "source": report.source,
                "gateway_probe_failed": report.gateway_probe_failed,
            },
        )
        try:
            for asset_id, ticker in assets:
                report.assets_seen += 1
                try:
                    rows = (
                        fetch_corporate_actions_gateway(
                            client, asset_id, ticker, date_from, date_to
                        )
                        if use_gateway and client is not None
                        else derive_corporate_actions_from_facts(conn, asset_id)
                    )
                except (ActionsNotSupported, sqlite3.Error) as exc:
                    report.errors.append(f"{ticker}: {exc}")
                    continue
                report.dividends += sum(1 for r in rows if r.action_type == "DIVIDEND")
                report.splits += sum(1 for r in rows if r.action_type == "SPLIT")
                report.inserted += upsert_corporate_actions(
                    conn, rows, engine_version=report.engine_version
                )
            finish_run(conn, run_id)
        except Exception as exc:
            fail_run(conn, run_id, str(exc))
            raise
        finally:
            if client is not None:
                client.close()
        return report
    finally:
        if owns:
            conn.close()
