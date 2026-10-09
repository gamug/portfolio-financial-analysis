"""Hand-built audit records for ``tests/test_sec_xcheck_*.py`` (T-147): a :class:`Rec` with only the fields a test sets."""

from __future__ import annotations

from typing import Any

from sec_xcheck.records import Rec

from fundamental_agent.statements import REGISTRY


def rec(  # noqa: PLR0913 - one record, every part a test may set
    *,
    items: dict[str, float | tuple[float, str]] | None = None,
    metrics: dict[str, dict[str, Any]] | None = None,
    ticker: str = "T",
    cik: str = "1",
    sector: str = "Industrials",
    sub_industry: str = "Machinery",
    form: str = "10-K",
    period_end: str = "2023-12-31",
    available_at: str | None = "2024-02-15",
    accession: str | None = None,
    role: str = "target",
    column_end: str = "",
    column_kind: str = "",
    **parts: Any,
) -> Rec:
    """*items* maps a registry item to a value, or to ``(value, concept)``; *parts* sets feature keys such as ``ocf``."""
    resolved: dict[str, dict[str, Any]] = {
        name: {"value": None, "concept": None, "how": None} for name in REGISTRY
    }
    for name, v in (items or {}).items():
        value, concept = v if isinstance(v, tuple) else (v, None)
        resolved[name] = {"value": value, "concept": concept, "how": "concept" if concept else None}
    f: dict[str, Any] = {
        "key": f"{period_end} (FY)",
        "instant": period_end,
        "instant_exact": True,
        "items": resolved,
        "metrics": metrics or {},
        "periods": {"tag": "FY", "prior_key": None, "prior_gap_days": None, "fy_dates": []},
        "identity": {},
        **{
            k: {}
            for k in (
                "ocf",
                "lt",
                "st",
                "cash",
                "sti",
                "da",
                "equity",
                "ni",
                "tax",
                "capex",
                "capex_captions",
            )
        },
        "dead": {"income_statement": {}, "balance_sheet": {}, "cash_flow": {}},
    }
    f.update(parts)
    return Rec(
        1,
        ticker,
        cik,
        sector,
        sub_industry,
        form,
        f"FY{period_end[:4]}",
        period_end,
        accession or f"acc-{ticker}",
        available_at,
        "2024-02-01",
        f,
        role,
        column_end,
        column_kind,
    )


def metric(value: float | None, group: str = "g", **inputs: float) -> dict[str, Any]:
    return {"group": group, "value": value, "inputs": dict(inputs)}
