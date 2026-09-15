"""Corporate-actions backfill: pricing-gateway probe, XBRL-derived fallbacks.

The gateway path (``--source gateway``) gives true ex-dates when the deployment
serves actions. The derive path (``--source derive``) runs two XBRL-derived
fallbacks, both coarse on purpose (no special dividends, no real ex-dates):

- :func:`derive_corporate_actions_from_facts` takes the fiscal-year cash
  dividend per share from a 10-K and spreads it evenly across four synthetic
  quarterly ex-dates -- wrong intra-year timing, but works for any filer with
  an annual DPS fact. Rows land under ``engine_version = 'corpact-v0-approx'``.
- :func:`derive_quarterly_dividends_from_10q_ytd` derives one dividend per
  10-Q filing from successive year-to-date differences (Q3,
  docs/model_fixes.md) -- closer to the real per-quarter cadence, but only
  for filers with 10-Q dividend facts at all (verified: only 301/503 assets
  have any ``corporate_action`` row from the FY-only path). Rows land under
  ``engine_version = 'corpact-v1-derived'``.

Both run unconditionally in the derive path; :func:`quant.db.load_actions`
picks whichever single engine is best for a given asset, never blending two
engines' ex-dates together. A better source (the pricing gateway, once it
serves actions) can supersede both later.
"""

from __future__ import annotations

import re
from datetime import date

from portfolio_common.db import Database, DatabaseError

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
from quant.pricing_client import ActionsNotSupported, QuantPricingClient
from quant.state import fail_run, finish_run, open_run

DERIVED_ENGINE_VERSION = "corpact-v0-approx"
DERIVED_10Q_ENGINE_VERSION = "corpact-v1-derived"

# Fiscal-year cash dividend per share, best signal first.
_DPS_CONCEPTS = (
    "us-gaap_CommonStockDividendsPerShareDeclared",
    "us-gaap_CommonStockDividendsPerShareCashPaid",
)
# Aggregate cash dividends paid, divided by a share count -> a per-share fallback
# for the many filers that tag only the total.
_DIV_PAID_CONCEPTS = (
    "us-gaap_PaymentsOfDividendsCommonStock",
    "us-gaap_PaymentsOfDividends",
    "us-gaap_PaymentsOfOrdinaryDividends",
)
_SHARES_CONCEPTS = (
    "us-gaap_WeightedAverageNumberOfDilutedSharesOutstanding",
    "us-gaap_WeightedAverageNumberOfSharesOutstandingBasic",
    "us-gaap_CommonStockSharesOutstanding",
)
_PERIOD_KEY_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_MAX_PERIOD_GAP_DAYS = 45  # a period_key within ~a fiscal quarter of the stated period_end


def _minus_months(d: date, months: int) -> date:
    m = d.month - 1 - months
    year = d.year + m // 12
    month = m % 12 + 1
    # clamp day to the shortest month we might land on
    day = min(d.day, 28)
    return date(year, month, day)


def _period_fact(
    conn: Database, filing_id: int, concepts: tuple[str, ...], pe: date, tag: str
) -> float | None:
    """The ``(tag)`` fact (e.g. ``"FY"``, ``"Q2"``, ``"YTD"``) for the first
    matching concept whose period_key date is within ~a fiscal quarter of the
    filing's stated period_end."""
    pattern = f"%({tag})%"
    for concept in concepts:
        best: tuple[int, float] | None = None
        for r in conn.execute(
            "SELECT period_key, value FROM financial_facts "
            "WHERE filing_id = ? AND concept = ? AND period_key LIKE ? "
            "AND value IS NOT NULL AND value <> 0",
            (filing_id, concept, pattern),
        ):
            m = _PERIOD_KEY_DATE.match(str(r["period_key"]))
            if not m:
                continue
            gap = abs((date.fromisoformat(m.group(1)) - pe).days)
            if best is None or gap < best[0]:
                best = (gap, abs(float(r["value"])))  # cash-flow lines are negative outflows
        if best is not None and best[0] <= _MAX_PERIOD_GAP_DAYS:
            return best[1]
    return None


def _fy_fact(conn: Database, filing_id: int, concepts: tuple[str, ...], pe: date) -> float | None:
    return _period_fact(conn, filing_id, concepts, pe, "FY")


def _fy_dps(conn: Database, filing_id: int, period_end: str) -> float | None:
    """Cash dividend per share for the filing's fiscal year: a tagged per-share
    fact if present, else aggregate dividends paid / a fiscal-year share count."""
    pe_match = _PERIOD_KEY_DATE.match(period_end)
    if not pe_match:
        return None
    pe = date.fromisoformat(pe_match.group(1))
    dps = _fy_fact(conn, filing_id, _DPS_CONCEPTS, pe)
    if dps is not None:
        return dps
    paid = _fy_fact(conn, filing_id, _DIV_PAID_CONCEPTS, pe)
    shares = _fy_fact(conn, filing_id, _SHARES_CONCEPTS, pe)
    if paid and shares:
        return paid / shares
    return None


def derive_corporate_actions_from_facts(
    conn: Database, asset_id: int, *, as_of: str | None = None
) -> list[CorporateAction]:
    """Four synthetic quarterly dividends per fiscal year with a recorded DPS.

    *as_of* caps the source filings to ``period_end <= as_of`` (no lookahead)."""
    if as_of:
        filings = conn.execute(
            "SELECT id, period_end FROM sec_filings "
            "WHERE asset_id = ? AND form = '10-K' AND period_end IS NOT NULL "
            "AND period_end <= ? ORDER BY period_end",
            (asset_id, as_of),
        ).fetchall()
    else:
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


_FISCAL_PERIOD_RE = re.compile(r"^(?P<year>\d{4})Q(?P<quarter>[1-4])$")


def _quarterly_dps_signal(
    conn: Database, filing_id: int, pe: date, quarter: int
) -> tuple[float | None, bool]:
    """This 10-Q's own dividend-per-share signal: ``(value, is_ytd)``. Prefers a
    fact already tagged for the filing's own discrete quarter (no differencing
    needed); falls back to a YTD-tagged fact (differenced against the prior
    quarter by the caller) when only that's available -- some filers tag
    ``CommonStockDividendsPerShareDeclared`` cumulatively in interim filings."""
    q_tag = f"Q{quarter}"
    dps_q = _period_fact(conn, filing_id, _DPS_CONCEPTS, pe, q_tag)
    if dps_q is not None:
        return dps_q, False
    dps_ytd = _period_fact(conn, filing_id, _DPS_CONCEPTS, pe, "YTD")
    if dps_ytd is not None:
        return dps_ytd, True
    paid_q = _period_fact(conn, filing_id, _DIV_PAID_CONCEPTS, pe, q_tag)
    shares_q = _period_fact(conn, filing_id, _SHARES_CONCEPTS, pe, q_tag)
    if paid_q and shares_q:
        return paid_q / shares_q, False
    paid_ytd = _period_fact(conn, filing_id, _DIV_PAID_CONCEPTS, pe, "YTD")
    shares_ytd = _period_fact(conn, filing_id, _SHARES_CONCEPTS, pe, "YTD")
    if paid_ytd and shares_ytd:
        return paid_ytd / shares_ytd, True
    return None, False


def derive_quarterly_dividends_from_10q_ytd(
    conn: Database, asset_id: int, *, as_of: str | None = None
) -> list[CorporateAction]:
    """One dividend per 10-Q filing, derived from successive year-to-date
    differences when the filer tags dividends-per-share cumulatively (Q3,
    docs/model_fixes.md): ``DPS_quarter(Qn) = DPS_YTD(Qn) - DPS_YTD(Qn-1)``,
    anchored to the filing's own ``period_end`` (coarse: no real ex-date at
    this level, same limitation as :func:`derive_corporate_actions_from_facts`).

    *as_of* caps the source filings to ``period_end <= as_of`` (no lookahead).
    """
    query = (
        "SELECT id, fiscal_period, period_end FROM sec_filings "
        "WHERE asset_id = ? AND form = '10-Q' AND period_end IS NOT NULL"
    )
    params: list[object] = [asset_id]
    if as_of:
        query += " AND period_end <= ?"
        params.append(as_of)
    query += " ORDER BY period_end"

    running_ytd: dict[int, float] = {}
    out: list[CorporateAction] = []
    for f in conn.execute(query, params):
        match = _FISCAL_PERIOD_RE.match(str(f["fiscal_period"]))
        pe_match = _PERIOD_KEY_DATE.match(str(f["period_end"]))
        if match is None or pe_match is None:
            continue
        year, quarter = int(match["year"]), int(match["quarter"])
        pe = date.fromisoformat(pe_match.group(1))
        value, is_ytd = _quarterly_dps_signal(conn, int(f["id"]), pe, quarter)
        if value is None:
            continue
        if is_ytd:
            quarterly = value - running_ytd.get(year, 0.0)
            running_ytd[year] = value
        else:
            quarterly = value
            running_ytd[year] = running_ytd.get(year, 0.0) + value
        if quarterly <= 0:
            continue  # a restatement/decrease artifact, not a real negative dividend
        out.append(
            CorporateAction(
                asset_id=asset_id,
                action_type="DIVIDEND",
                ex_date=str(f["period_end"]),
                value=round(quarterly, 6),
                frequency="quarterly",
                source="financial-facts-derived-10q-ytd",
            )
        )
    return out


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
    conn: Database | None = None,
) -> ActionsReport:
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
            as_of=date_to,
            params={
                "analysis_date": date_to,
                "date_from": date_from,
                "date_to": date_to,
                "source": report.source,
                "gateway_probe_failed": report.gateway_probe_failed,
            },
            code_version=code_version(),
        )
        try:
            for asset_id, ticker in assets:
                report.assets_seen += 1
                try:
                    if use_gateway and client is not None:
                        by_engine = [
                            (
                                fetch_corporate_actions_gateway(
                                    client, asset_id, ticker, date_from, date_to
                                ),
                                report.engine_version,
                            )
                        ]
                    else:
                        # Both derived fallbacks run unconditionally, tagged under
                        # their own engine_version -- quant.db.load_actions picks
                        # whichever single one is best for a given asset (Q3,
                        # docs/model_fixes.md), never blending the two.
                        by_engine = [
                            (
                                derive_corporate_actions_from_facts(conn, asset_id, as_of=date_to),
                                DERIVED_ENGINE_VERSION,
                            ),
                            (
                                derive_quarterly_dividends_from_10q_ytd(
                                    conn, asset_id, as_of=date_to
                                ),
                                DERIVED_10Q_ENGINE_VERSION,
                            ),
                        ]
                except (ActionsNotSupported, DatabaseError) as exc:
                    report.errors.append(f"{ticker}: {exc}")
                    continue
                for rows, engine_version in by_engine:
                    report.dividends += sum(1 for r in rows if r.action_type == "DIVIDEND")
                    report.splits += sum(1 for r in rows if r.action_type == "SPLIT")
                    report.inserted += upsert_corporate_actions(
                        conn, rows, engine_version=engine_version
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
