"""Measurements over what the pipeline *stored* (``fundamental_metrics``, ``score_snapshot``): PER-01, PER-06, MKT-01,
MKT-02, MKT-04, ID-18, MET-00 (D10) and N16.

These read the stored ``inputs_json`` and ``available_at``; they describe one database at the engine version it was
built with (production ``metrics-v2``, pilot ``metrics-v4``). Read-only SQL; every query is a literal string.
"""

from __future__ import annotations

import json
from typing import Any

from sec_xcheck.findings import Finding

A03_RANGE = (0.001, 100.0)  # Annex A-03: market_cap / total_assets
OFFSET_LIMIT_DAYS = 5


def one(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def finding(  # noqa: PLR0913, PLR0917 - a finding is its key, rules, title, counts and database label
    key: str, rules: list[str], title: str, n: int, total: int, label: str, **kw: Any
) -> Finding:
    return Finding(
        key,
        rules,
        f"{title} [{label}]",
        "b",
        n,
        total,
        kw.pop("companies", 0),
        command=kw.pop("command", ""),
        **kw,
    )


def m_availability(conn: Any, label: str) -> list[Finding]:
    """PER-01 / MKT-01: stored ratios and scores whose availability date is missing or not after the period end."""
    cmd = "uv run python scripts/audit_sec_checklist.py measure --only stored --db <db>"
    metrics = one(conn, "SELECT COUNT(*) FROM fundamental_metrics")
    null_m = one(conn, "SELECT COUNT(*) FROM fundamental_metrics WHERE available_at IS NULL")
    early_m = one(
        conn,
        "SELECT COUNT(*) FROM fundamental_metrics m JOIN sec_filings f ON f.id = m.filing_id "
        "WHERE m.available_at IS NOT NULL AND m.available_at <= f.period_end",
    )
    scores = one(conn, "SELECT COUNT(*) FROM score_snapshot WHERE score_type = 'FUNDAMENTAL'")
    null_s = one(
        conn,
        "SELECT COUNT(*) FROM score_snapshot WHERE score_type = 'FUNDAMENTAL' AND available_at IS NULL",
    )
    early_s = one(
        conn,
        "SELECT COUNT(*) FROM score_snapshot WHERE score_type = 'FUNDAMENTAL' AND available_at IS NOT NULL "
        "AND available_at <= event_time",
    )
    return [
        finding(
            "PER01.metrics_available_at_null",
            ["PER-01", "MKT-01"],
            "Stored ratios with a NULL available_at",
            null_m,
            metrics,
            label,
            unit="metric rows",
            command=cmd,
        ),
        finding(
            "PER01.metrics_available_at_not_after_period_end",
            ["PER-01"],
            "Stored ratios available on or before their period end",
            early_m,
            metrics,
            label,
            unit="metric rows",
            command=cmd,
        ),
        finding(
            "PER01.scores_available_at_null",
            ["PER-01"],
            "FUNDAMENTAL scores with a NULL available_at",
            null_s,
            scores,
            label,
            unit="scores",
            command=cmd,
        ),
        finding(
            "PER01.scores_available_at_not_after_period_end",
            ["PER-01"],
            "FUNDAMENTAL scores available on or before their period end",
            early_s,
            scores,
            label,
            unit="scores",
            command=cmd,
        ),
    ]


def m_annualization(conn: Any, label: str) -> list[Finding]:
    """PER-06: metrics whose inputs carry the x4 fallback (``annualized_x4``) or an annualization flag."""
    cmd = "uv run python scripts/audit_sec_checklist.py measure --only stored --db <db>"
    total = one(
        conn,
        "SELECT COUNT(*) FROM fundamental_metrics m JOIN sec_filings f ON f.id = m.filing_id WHERE f.form = '10-Q'",
    )
    x4 = one(
        conn,
        "SELECT COUNT(*) FROM fundamental_metrics m JOIN sec_filings f ON f.id = m.filing_id "
        "WHERE f.form = '10-Q' AND m.inputs_json LIKE '%annualized_x4%'",
    )
    ttm = one(
        conn,
        "SELECT COUNT(*) FROM fundamental_metrics m JOIN sec_filings f ON f.id = m.filing_id "
        "WHERE f.form = '10-Q' AND m.inputs_json LIKE '%annualized_ttm%'",
    )
    old_engine = (
        "not measurable on this engine: the TTM flags arrived in metrics-v3 (T-105), so 0 is not 'none'"
        if "metrics-v2" in label
        else ""
    )
    return [
        finding(
            "PER06.metrics_built_on_x4",
            ["PER-06"],
            "10-Q metric rows built on one quarter times four",
            x4,
            total,
            label,
            unit="metric rows",
            command=cmd,
            note=old_engine,
        ),
        finding(
            "PER06.metrics_built_on_ttm",
            ["PER-06"],
            "10-Q metric rows built on a TTM (identity or four quarters)",
            ttm,
            total,
            label,
            unit="metric rows",
            command=cmd,
            note=old_engine,
        ),
    ]


def valuation_rows(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT m.filing_id, a.ticker, f.accession_number, m.inputs_json FROM fundamental_metrics m "
        "JOIN sec_filings f ON f.id = m.filing_id JOIN assets a ON a.id = f.asset_id "
        "WHERE m.metric_name = 'market_capitalization' AND m.value IS NOT NULL"
    ).fetchall()
    return [
        {
            "ticker": r["ticker"],
            "accession": r["accession_number"],
            "inputs": json.loads(r["inputs_json"] or "{}"),
            "filing_id": r["filing_id"],
        }
        for r in rows
    ]


def m_valuation(conn: Any, label: str) -> list[Finding]:
    """MKT-01/02/04, ID-18 and D10 on the stored valuation rows."""
    cmd = "uv run python scripts/audit_sec_checklist.py measure --only stored --db <db>"
    rows = valuation_rows(conn)
    n = len(rows)
    diluted = [r for r in rows if r["inputs"].get("shares_are_diluted_average") == 1.0]
    not_cover = [r for r in rows if not r["inputs"].get("shares_from_cover_page")]
    corrected = [r for r in rows if r["inputs"].get("shares_scale_correction_factor") is not None]
    zero_filled = [r for r in rows if "total_debt" not in r["inputs"] or "cash" not in r["inputs"]]
    offsets = sorted(r["inputs"].get("price_date_offset_days", 0.0) for r in rows)
    caught = 0
    for r in corrected:
        i = r["inputs"]
        assets = conn.execute(
            "SELECT json_extract(inputs_json, '$.total_assets') FROM fundamental_metrics WHERE filing_id = ? "
            "AND metric_name = 'debt_to_assets' LIMIT 1",
            (r["filing_id"],),
        ).fetchone()
        if assets and assets[0] and i.get("shares") and i.get("share_price"):
            raw = i["shares"] / i["shares_scale_correction_factor"] * i["share_price"]
            ratio = raw / assets[0]
            caught += not (A03_RANGE[0] <= ratio <= A03_RANGE[1])
    ex = lambda rs: [(r["ticker"], r["accession"], "") for r in rs]  # noqa: E731
    return [
        finding(
            "MKT04.shares_not_from_cover_page",
            ["MKT-01", "MKT-04"],
            "Stored market cap built on a share count that is not the cover-page count (describes the stored engine, not today's code: cover counts arrived with T-132)",
            len(not_cover),
            n,
            label,
            unit="valuation filings",
            examples=ex(not_cover)[:5],
            command=cmd,
        ),
        finding(
            "MKT04.diluted_average_fallback",
            ["MKT-04"],
            "Stored market cap built on the weighted-average diluted share count",
            len(diluted),
            n,
            label,
            unit="valuation filings",
            examples=ex(diluted)[:5],
            command=cmd,
        ),
        finding(
            "ID18.scale_corrector_applied",
            ["ID-18"],
            "Stored share count rescaled by the detected power-of-ten factor (the rule: quarantine, never correct)",
            len(corrected),
            n,
            label,
            unit="valuation filings",
            examples=ex(corrected)[:5],
            command=cmd,
        ),
        finding(
            "ID18.a03_would_catch_without_corrector",
            ["ID-18", "A-03"],
            "...of which DQ_MCAP_SCALE (A-03) would flag the uncorrected market cap",
            caught,
            len(corrected),
            label,
            unit="valuation filings",
            command=cmd,
            note="uncorrected cap = stored shares / factor x price, against total assets",
        ),
        finding(
            "D10.ev_zero_filled_stored",
            ["MET-00"],
            "Stored enterprise value built with debt or cash absent (filled with 0)",
            len(zero_filled),
            n,
            label,
            unit="valuation filings",
            examples=ex(zero_filled)[:5],
            command=cmd,
        ),
        finding(
            "MKT01.price_date_offset_over_5_days",
            ["MKT-01"],
            "Stored market cap priced more than 5 days from the period end",
            sum(1 for o in offsets if o > OFFSET_LIMIT_DAYS),
            n,
            label,
            unit="valuation filings",
            command=cmd,
        ),
    ]


def m_fallback(conn: Any, label: str) -> list[Finding]:
    """N16: FUNDAMENTAL scores produced by the rule-based fallback, which starts from a neutral 50."""
    cmd = "uv run python scripts/audit_sec_checklist.py measure --only stored --db <db>"
    total = one(conn, "SELECT COUNT(*) FROM score_snapshot WHERE score_type = 'FUNDAMENTAL'")
    fb = one(
        conn,
        "SELECT COUNT(*) FROM score_snapshot WHERE score_type = 'FUNDAMENTAL' AND narrative LIKE 'Automated fallback%'",
    )
    return [
        finding(
            "N16.fundamental_fallback_scores",
            ["MET-00"],
            "FUNDAMENTAL scores from the rule-based fallback (starts at 50, skips missing metrics)",
            fb,
            total,
            label,
            unit="scores",
            command=cmd,
        )
    ]


def engine_versions(conn: Any) -> str:
    return ", ".join(
        r[0]
        for r in conn.execute("SELECT DISTINCT engine_version FROM fundamental_metrics ORDER BY 1")
    )


def m_stored(conn: Any, label: str) -> list[Finding]:
    """Every stored-level measure, labelled with the engine version that built the stored rows: production holds
    ``metrics-v2``, which predates the T-105 annualization flags and T-132's cover counts, so a 0 or a 100% there
    describes that engine, not today's code."""
    label = f"{label}, stored {engine_versions(conn)}"
    return [
        *m_availability(conn, label),
        *m_annualization(conn, label),
        *m_valuation(conn, label),
        *m_fallback(conn, label),
        *m_repro(conn, label),
    ]


def m_repro(conn: Any, label: str) -> list[Finding]:
    """The checklist's own coverage claims (ID-19, CON-10, L-01, MET-07), counted from the stored facts, to validate this
    audit's counting against the reviewer's: the checklist reports 147 / 212 / 11 / 196 / 1,265 / 2,109 / 1,329 of 5,075."""
    cmd = "uv run python scripts/audit_sec_checklist.py measure --only stored --db <db>"
    total = one(conn, "SELECT COUNT(*) FROM sec_filings")
    claims = {
        "ID19.common_stock_shares_outstanding_captured": (
            "us-gaap_CommonStockSharesOutstanding",
            "147",
        ),
        "CON10.common_stock_shares_authorized_captured": (
            "us-gaap_CommonStockSharesAuthorized",
            "212",
        ),
        "L01.operating_lease_cost_captured": ("us-gaap_OperatingLeaseCost", "11"),
        "L01.operating_lease_liability_captured": ("us-gaap_OperatingLeaseLiability", "196"),
        "L01.operating_lease_liability_current_captured": (
            "us-gaap_OperatingLeaseLiabilityCurrent",
            "1,265",
        ),
        "L01.operating_lease_liability_noncurrent_captured": (
            "us-gaap_OperatingLeaseLiabilityNoncurrent",
            "2,109",
        ),
    }
    out = []
    for key, (concept, reported) in claims.items():
        n = one(
            conn,
            "SELECT COUNT(DISTINCT filing_id) FROM financial_facts WHERE concept = ?",
            (concept,),
        )
        rule = "ID-19" if "ID19" in key else "CON-10" if "CON10" in key else "L-01"
        out.append(
            finding(
                key, [rule], f"Filings with {concept.split('_', 1)[1]} captured", n, total, label,
                unit="filings", command=cmd, note=f"checklist v1.2.2 reports {reported} (production)",
            )
        )  # fmt: skip
    only_combined = one(
        conn,
        "SELECT COUNT(*) FROM sec_filings f WHERE EXISTS (SELECT 1 FROM financial_facts x WHERE x.filing_id = f.id AND "
        "x.concept IN ('us-gaap_LongTermDebtAndCapitalLeaseObligations', "
        "'us-gaap_LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities')) AND NOT EXISTS "
        "(SELECT 1 FROM financial_facts y WHERE y.filing_id = f.id AND y.concept IN "
        "('us-gaap_LongTermDebtNoncurrent', 'us-gaap_LongTermDebt'))",
    )
    out.append(
        finding(
            "MET07.combined_element_is_only_long_term_debt_line", ["MET-07"],
            "Filings whose only long-term debt line is a combined debt-and-lease element",
            only_combined, total, label, unit="filings", command=cmd,
            note="checklist v1.2.2 reports 1,329 of 5,075 (production) and 113 of 449 (pilot); this count is 3.5% higher",
        )
    )  # fmt: skip
    return out
