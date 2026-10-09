"""MET-08 and APP-04b at level (b): the capex lines the rule adds, and what today's resolver reads.

``target_capex`` is the checklist's rule read as an algorithm (MET-08 (a)-(c), APP-04b); it is the audit's
*reading* of v1.2.2, written so the difference from the resolver can be counted. Where the rule is silent the
function says so in a flag instead of choosing. Differences are counted per cause; amounts are USD.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from typing import Any

from sec_xcheck.findings import Finding
from sec_xcheck.records import Rec, count

G = "us-gaap_"
PPE = G + "PaymentsToAcquirePropertyPlantAndEquipment"
PROD = G + "PaymentsToAcquireProductiveAssets"
CAPIMP = G + "PaymentsForCapitalImprovements"
OG_ED = G + "PaymentsToExploreAndDevelopOilAndGasProperties"
OG_PPE = G + "PaymentsToAcquireOilAndGasPropertyAndEquipment"
OG_PROP = G + "PaymentsToAcquireOilAndGasProperty"
OG_EQUIP = G + "PaymentsToAcquireOilAndGasEquipment"
NET = G + "PaymentsForProceedsFromProductiveAssets"
OTHER_PPE = G + "PaymentsToAcquireOtherPropertyPlantAndEquipment"
BUSINESSES = G + "PaymentsToAcquireBusinessesNetOfCashAcquired"
REAL_ESTATE = (
    G + "PaymentsToAcquireRealEstate",
    G + "PaymentsToAcquireRealEstateHeldForInvestment",
)
CMD = "uv run python scripts/audit_sec_checklist.py measure --only capex"
LARGE_DEAL_SHARE = (
    0.25  # an acquisition line above this share of operating cash flow is a "large deal"
)


def is_reit(r: Rec) -> bool:
    return r.sector == "Real Estate" and r.sub_industry.endswith("REITs")


GENERIC_TIERS = (
    (PPE, None),
    (PROD, "productive_assets_fallback"),
    (CAPIMP, "capital_improvements"),
)
MIN_LINES = 2


def _reit_capex(a: dict[str, float]) -> tuple[float | None, list[str]]:
    flags: list[str] = []
    parts = [a[k] for k in (PPE, *REAL_ESTATE, G + "PaymentsToDevelopRealEstateAssets") if k in a]
    if CAPIMP in a and not any(k in a for k in REAL_ESTATE):
        parts.append(a[CAPIMP])
    elif CAPIMP in a:
        flags.append("capital_improvements_overlap_not_added")
    if not parts and PROD in a:
        return a[PROD], ["productive_assets_fallback"]
    return (sum(parts), ["reit_components", *flags]) if parts else (None, flags)


def _oil_and_gas_capex(a: dict[str, float]) -> tuple[float | None, list[str]]:
    finer = [a[k] for k in (OG_ED, OG_EQUIP) if k in a]
    if finer:
        flag = "tie_break_c_added" if len(finer) > 1 else "og_finer_line"
        return sum(finer) + a.get(OTHER_PPE, 0.0), [flag]
    if OG_PPE in a:
        return a[OG_PPE], ["includes_acquisitions_contrary_to_a"]
    if NET in a:
        return a[NET], ["net_element_last_resort"]
    return None, []


def target_capex(c: dict[str, float], *, reit: bool = False) -> tuple[float | None, list[str]]:
    """MET-08 read as a procedure: ``(amount, flags)``; ``flags`` name the rule branch that produced it."""
    a = {
        k: abs(v) for k, v in c.items()
    }  # ID-11 would reject a wrong sign; magnitudes compare like with like
    if reit:
        return _reit_capex(a)
    for concept, flag in GENERIC_TIERS:
        if concept in a:
            return a[concept], [flag] if flag else []
    value, flags = _oil_and_gas_capex(a)
    if value is None and OTHER_PPE in a:
        return a[OTHER_PPE], [
            "other_ppe_only_rule_silent"
        ]  # MET-08 (b)(3) is silent when it is the only line
    return value, flags


def resolved_capex(r: Rec) -> float | None:
    v = r.value("capital_expenditure")
    return None if v is None else abs(v)


def cause(r: Rec) -> str | None:
    """Why the target and today's value differ, or ``None`` when they agree to the dollar."""
    target, flags = target_capex(r.f["capex"], reit=is_reit(r))
    now = resolved_capex(r)
    if target is None and now is None:
        return None
    if now is None:
        return "resolver_empty_" + (target_capex(r.f["capex"], reit=is_reit(r))[1] or ["rule"])[0]
    if target is None:
        return "rule_empty_resolver_has_value"
    if abs(target - now) <= 1.0:
        return None
    return flags[0] if flags else "other_difference"


def amount_left_out(r: Rec) -> float:
    target, _ = target_capex(r.f["capex"], reit=is_reit(r))
    return (target or 0.0) - (resolved_capex(r) or 0.0)


def fcf_effect(r: Rec) -> float | None:
    """The difference as a share of operating cash flow (what a free-cash-flow metric would move by)."""
    ocf = r.value("operating_cash_flow")
    return None if not ocf else amount_left_out(r) / abs(ocf)


def summarize(values: list[float]) -> str:
    if not values:
        return ""
    return f"|amount| n={len(values)}, median {statistics.median(values) / 1e6:,.0f}M, max {max(values) / 1e6:,.0f}M"


def differs(cause_name: str) -> Callable[[Rec], bool]:
    return lambda r: cause(r) == cause_name


def m_capex(recs: list[Rec]) -> list[Finding]:
    out: list[Finding] = []
    pool = [
        r
        for r in recs
        if r.value("capital_expenditure") is not None or target_capex(r.f["capex"])[0] is not None
    ]
    differing = [r for r in pool if cause(r) is not None]
    out.append(
        count(
            "MET08.target_differs",
            ["MET-08"],
            "Capex under MET-08 (a)-(c) differs from today's resolved capex",
            recs,
            lambda r: cause(r) is not None,
            population=lambda r: r in pool,
            command=CMD,
            note=summarize([abs(amount_left_out(r)) for r in differing]),
        )
    )
    causes = sorted({cause(r) for r in differing} - {None})
    for c in causes:
        subset = [r for r in differing if cause(r) == c]
        out.append(
            count(
                f"MET08.cause.{c}",
                ["MET-08"],
                f"MET-08 vs resolver, cause: {c}",
                recs,
                differs(c),
                command=CMD,
                note=summarize([abs(amount_left_out(r)) for r in subset]),
            )
        )
    out.append(
        count(
            "MET08.left_out_lines",
            ["MET-08"],
            "Filings with two or more capex lines that today's resolver does not add",
            recs,
            lambda r: (
                sum(
                    1
                    for k, v in r.f["capex"].items()
                    if k in (PPE, PROD, CAPIMP, OG_ED, OG_PPE, OG_EQUIP, OTHER_PPE, NET) and v
                )
                >= MIN_LINES
            ),
            command=CMD,
            note="census of filings carrying 2+ capex lines among PP&E, productive assets, improvements, O&G and 'other' PP&E",
        )
    )
    out.append(
        count(
            "MET08a.og_property_is_capex",
            ["MET-08"],
            "Capex resolved to PaymentsToAcquireOilAndGasProperty (acquisitions of mineral interests), contrary to (a)",
            recs,
            lambda r: (
                r.item("capital_expenditure")["concept"] == OG_PROP
                or r.item("capital_expenditure")["how"] == "fallback3"
            ),
            command=CMD,
        )
    )
    out.append(
        count(
            "MET08a.og_property_with_ed",
            ["MET-08"],
            "Producers filing mineral-interest purchases beside exploration & development (cost of excluding routine leasehold purchases)",
            recs,
            lambda r: OG_PROP in r.f["capex"] and OG_ED in r.f["capex"],
            command=CMD,
            note="year-by-year table in the report; 'years without mergers' needs a hand reading of each company",
        )
    )
    out.append(
        count(
            "MET08.tie_break_c_fires",
            ["MET-08"],
            "(c) tie-break: exploration & development beside O&G equipment as separate face lines",
            recs,
            lambda r: OG_ED in r.f["capex"] and OG_EQUIP in r.f["capex"],
            command=CMD,
        )
    )
    out.append(
        count(
            "MET08b.capital_improvements_non_reit",
            ["MET-08", "APP-04b"],
            "Non-REIT capex resolved through PaymentsForCapitalImprovements",
            recs,
            lambda r: r.item("capital_expenditure")["concept"] == CAPIMP and not is_reit(r),
            command=CMD,
        )
    )
    out.append(
        count(
            "MET08b.productive_assets_unflagged",
            ["MET-08"],
            "Capex resolved to PaymentsToAcquireProductiveAssets (a flagged fallback under (b)(1)); today unflagged",
            recs,
            lambda r: r.item("capital_expenditure")["concept"] == PROD,
            command=CMD,
        )
    )
    out.append(
        count(
            "MET08.net_element_unflagged",
            ["MET-08"],
            "Capex resolved to the net element PaymentsForProceedsFromProductiveAssets (last resort, flagged under the rule)",
            recs,
            lambda r: r.item("capital_expenditure")["how"] == "fallback4",
            command=CMD,
        )
    )
    out.append(
        count(
            "MET08.custom_caption_fallback",
            ["MET-08", "ID-01"],
            "Capex found by its caption (a custom concept)",
            recs,
            lambda r: r.item("capital_expenditure")["how"] == "label_fallback",
            command=CMD,
        )
    )
    out.append(
        count(
            "MET08.capex_absent",
            ["MET-08"],
            "No capex resolved while the cash-flow statement is present",
            recs,
            lambda r: r.value("capital_expenditure") is None,
            population=lambda r: r.value("operating_cash_flow") is not None,
            command=CMD,
        )
    )
    out.append(
        count(
            "APP04b.reit_acquisitions_left_out",
            ["APP-04b"],
            "Equity REITs filing real-estate acquisitions that today's capex excludes",
            recs,
            lambda r: is_reit(r) and any(k in r.f["capex"] for k in REAL_ESTATE),
            population=is_reit,
            command=CMD,
        )
    )
    out.append(
        count(
            "APP04b.reit_improvements_and_acquisitions_overlap",
            ["APP-04b"],
            "Equity REITs filing both PaymentsForCapitalImprovements and an acquisition line (the overlap)",
            recs,
            lambda r: (
                is_reit(r)
                and CAPIMP in r.f["capex"]
                and any(k in r.f["capex"] for k in REAL_ESTATE)
            ),
            population=is_reit,
            command=CMD,
        )
    )
    out.append(
        count(
            "MET08.large_deal_business_acquisitions",
            ["MET-08"],
            "Business acquisitions above 25% of operating cash flow (excluded from capex: Compustat precedent)",
            recs,
            lambda r: (
                bool(r.value("operating_cash_flow"))
                and abs(r.f["capex"].get(BUSINESSES, 0.0))
                > LARGE_DEAL_SHARE * abs(r.value("operating_cash_flow") or 0.0)
            ),
            command=CMD,
            note="carried by PaymentsToAcquireBusinessesNetOfCashAcquired; not in today's capex tiers, so no change",
        )
    )
    return out


Metadata = dict[str, Any]


def og_year_table(recs: list[Rec]) -> list[dict[str, Any]]:
    """MET-08 (a): per producer and fiscal year, mineral-interest purchases (``PaymentsToAcquireOilAndGasProperty``) beside
    exploration and development, so the cost of excluding routine leasehold purchases is visible year by year. A year with
    a large business acquisition is marked: only a hand reading separates routine purchases from a merger."""
    rows = []
    for r in sorted(
        (x for x in recs if x.form == "10-K" and x.role == "target"),
        key=lambda x: (x.ticker, x.period_end),
    ):
        c = r.f["capex"]
        if OG_PROP not in c and OG_ED not in c:
            continue
        prop, ed = abs(c.get(OG_PROP, 0.0)), abs(c.get(OG_ED, 0.0))
        rows.append(
            {
                "ticker": r.ticker, "accession": r.accession, "fiscal_year_end": r.period_end,
                "exploration_development": ed, "oil_gas_equipment": abs(c.get(OG_EQUIP, 0.0)),
                "oil_gas_property_and_equipment": abs(c.get(OG_PPE, 0.0)), "mineral_interest_purchases": prop,
                "business_acquisitions": abs(c.get(BUSINESSES, 0.0)),
                "mineral_to_development": round(prop / ed, 3) if ed else None,
                "resolved_capex": resolved_capex(r),
                "resolved_concept": (r.item("capital_expenditure")["concept"] or r.item("capital_expenditure")["how"]),
            }
        )  # fmt: skip
    return rows
