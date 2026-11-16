#!/usr/bin/env python3
"""TASK 1 (2026-10-20): which values does the pilot database miss although the company filed them?

    uv run python scripts/pilot_a/loss_measurement.py \
        --db /workspaces/thesis/pilot_a/financial.db \
        --cache /home/vscode/pilot_a_run/sec_cache \
        --out-json /workspaces/thesis/scratch/t143a/out/p3_loss_measurement.json \
        --out-csv /workspaces/thesis/scratch/t143a/out/p3_loss_measurements.csv

**Read-only.** The pilot database is opened ``mode=ro`` and its sha256 is checked by the caller
before and after; the SEC ``companyfacts`` cache is read from disk (no new SEC request is made).

For every stored filing and every line item of ``fundamental_agent.statements.REGISTRY``:

1. **SEC side** -- the companyfacts cache holds a fact for one of the item's concepts with
   ``accn`` = the filing's accession, ``end`` = the filing's period end and the **same kind**: an
   instant for a balance-sheet item, a duration (the quarter's or the year-to-date one, per
   ``sec_xcheck.common.duration_matches``) for an income or cash-flow item.
2. **Pilot side** -- ``financial_facts`` holds **no non-null** value for any of the item's concepts at
   that period (the ``period_key``'s date = the filing's period end).
3. Both true -> a **LOSS**: the filing lost that value. For each loss the item level is then read the
   way ``scripts/sec_xcheck/itemcheck.py`` reads stored values (the filing's
   ``fundamental_metrics.inputs_json``): a value there means the item still **resolved elsewhere**,
   no value means it is **lost**.

The item's concept set is the union of ``concepts``, ``total_concepts``, ``fallback_concepts`` and
``fallback_addends`` of the registry, plus -- for the eight composite resolvers -- the concepts the
resolver method itself reads, which are listed in :data:`COMPOSITE_CONCEPTS` below with the
``file:line`` each list came from. A concept name is ``<prefix>_<Name>``; the prefixes ``us-gaap``,
``dei``, ``srt`` and ``ifrs-full`` map to the taxonomy of the same name, and any other prefix is a
**company-specific** concept that companyfacts never holds -- those cases are counted on their own
line, never skipped.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from fundamental_agent.statements import REGISTRY

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from sec_xcheck.common import cache_dir, cik_family, duration_matches

#: The taxonomies the SEC's companyfacts holds; the code writes ``<taxonomy>_<Name>``.
TAXONOMIES = ("us-gaap", "dei", "srt", "ifrs-full")
STANDARD_PREFIXES = frozenset(TAXONOMIES)

#: The concepts the eight composite resolvers read, with the ``file:line`` each list came from
#: (``src/fundamental_agent/statements.py`` unless stated otherwise). The registry's own lists stay
#: in the registry; these are the extra concepts the resolver methods read.
COMPOSITE_CONCEPTS: dict[str, tuple[tuple[str, str], ...]] = {
    "net_income": (
        ("us-gaap_NetIncomeLoss", 'statements.py:1296 (G + "NetIncomeLoss")'),
        ("us-gaap_ProfitLoss", 'statements.py:1299 (G + "ProfitLoss")'),
        (
            "us-gaap_NetIncomeLossAttributableToNoncontrollingInterest",
            'statements.py:1306 (G + "NetIncomeLossAttributableToNoncontrollingInterest")',
        ),
    ),
    "income_tax": (
        ("us-gaap_IncomeTaxExpenseBenefit", "statements.py:1349"),
        ("us-gaap_CurrentIncomeTaxExpenseBenefit", "statements.py:1352"),
        ("us-gaap_DeferredIncomeTaxExpenseBenefit", "statements.py:1353"),
    ),
    "depreciation_amortization": (
        ("us-gaap_Depreciation", "statements.py:279 (DA_SOURCED_CHILDREN)"),
        ("us-gaap_Depletion", "statements.py:279 (DA_SOURCED_CHILDREN)"),
        ("us-gaap_OtherDepreciationAndAmortization", "statements.py:279 (DA_SOURCED_CHILDREN)"),
        ("us-gaap_AmortizationOfIntangibleAssets", "statements.py:279 (DA_SOURCED_CHILDREN)"),
        (
            "us-gaap_DepreciationAndAmortization",
            "statements.py:297 (DA_CAPTION_ELEMENTS)",
        ),
        (
            "us-gaap_DepreciationDepletionAndAmortization",
            "statements.py:297 (DA_CAPTION_ELEMENTS)",
        ),
        (
            "us-gaap_DepreciationAmortizationAndAccretionNet",
            "statements.py:297 (DA_CAPTION_ELEMENTS)",
        ),
    ),
    "capital_expenditure": (
        ("us-gaap_PaymentsToAcquireRealEstate", "statements.py:1726 (_reit_components)"),
        (
            "us-gaap_PaymentsToAcquireCommercialRealEstate",
            "statements.py:1729 (_reit_components)",
        ),
        ("us-gaap_PaymentsForCapitalImprovements", "statements.py:1730 (_reit_components)"),
        (
            "us-gaap_PaymentsToAcquireRealEstateHeldForInvestment",
            "statements.py:1734 (_reit_components)",
        ),
        ("us-gaap_PaymentsToDevelopRealEstateAssets", "statements.py:1735 (_reit_components)"),
    ),
    "cash": (
        ("us-gaap_CashEquivalentsAtCarryingValue", "statements.py:1440"),
        (
            "us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            "statements.py:1464",
        ),
        ("us-gaap_Cash", "statements.py:1470"),
    ),
    "redeemable_nci": (
        (
            "us-gaap_RedeemableNoncontrollingInterestEquityCarryingAmount",
            "statements.py:378 (REDEEMABLE_NCI_CONCEPTS)",
        ),
        (
            "us-gaap_RedeemableNoncontrollingInterestEquityCommonCarryingAmount",
            "statements.py:378 (REDEEMABLE_NCI_CONCEPTS)",
        ),
        (
            "us-gaap_RedeemableNoncontrollingInterestEquityPreferredCarryingAmount",
            "statements.py:378 (REDEEMABLE_NCI_CONCEPTS)",
        ),
        (
            "us-gaap_RedeemableNoncontrollingInterestEquityOtherCarryingAmount",
            "statements.py:378 (REDEEMABLE_NCI_CONCEPTS)",
        ),
    ),
    "long_term_debt": (
        (
            "us-gaap_LongTermDebtNoncurrent",
            "statements.py:197 (LONG_TERM_DEBT_NONCURRENT_CHILDREN)",
        ),
        ("us-gaap_LongTermLineOfCredit", "statements.py:197"),
        ("us-gaap_CommercialPaperNoncurrent", "statements.py:197"),
        ("us-gaap_ConstructionLoanNoncurrent", "statements.py:197"),
        ("us-gaap_SecuredLongTermDebt", "statements.py:197"),
        ("us-gaap_SubordinatedLongTermDebt", "statements.py:197"),
        ("us-gaap_UnsecuredLongTermDebt", "statements.py:197"),
        ("us-gaap_ConvertibleDebtNoncurrent", "statements.py:197"),
        ("us-gaap_ConvertibleSubordinatedDebtNoncurrent", "statements.py:197"),
        ("us-gaap_LongTermTransitionBond", "statements.py:197"),
        ("us-gaap_LongTermPollutionControlBond", "statements.py:197"),
        (
            "us-gaap_JuniorSubordinatedDebentureOwedToUnconsolidatedSubsidiaryTrustNoncurrent",
            "statements.py:197",
        ),
        ("us-gaap_LongTermNotesAndLoans", "statements.py:197"),
        ("us-gaap_SpecialAssessmentBondNoncurrent", "statements.py:197"),
        (
            "us-gaap_LongtermFederalHomeLoanBankAdvancesNoncurrent",
            "statements.py:197",
        ),
        ("us-gaap_OtherLongTermDebtNoncurrent", "statements.py:197"),
        ("us-gaap_SecuredDebt", "statements.py:214 (LONG_TERM_DEBT_CHILDREN)"),
        ("us-gaap_UnsecuredDebt", "statements.py:214"),
        ("us-gaap_SubordinatedDebt", "statements.py:214"),
        ("us-gaap_ConvertibleDebt", "statements.py:214"),
        ("us-gaap_ConvertibleSubordinatedDebt", "statements.py:214"),
        (
            "us-gaap_JuniorSubordinatedDebentureOwedToUnconsolidatedSubsidiaryTrust",
            "statements.py:214",
        ),
        ("us-gaap_LineOfCredit", "statements.py:214"),
        ("us-gaap_LongtermCommercialPaperCurrentAndNoncurrent", "statements.py:214"),
        ("us-gaap_LongtermTransitionBondCurrentAndNoncurrent", "statements.py:214"),
        ("us-gaap_LongtermPollutionControlBondCurrentAndNoncurrent", "statements.py:214"),
        ("us-gaap_LongtermConstructionLoanCurrentAndNoncurrent", "statements.py:214"),
        ("us-gaap_NotesAndLoansPayable", "statements.py:214"),
        ("us-gaap_SpecialAssessmentBond", "statements.py:214"),
        ("us-gaap_OtherLongTermDebt", "statements.py:214"),
        ("us-gaap_FederalHomeLoanBankAdvancesLongTerm", "statements.py:214"),
    ),
    "short_term_debt": (
        ("us-gaap_LongTermDebtCurrent", "statements.py:158 (SHORT_TERM_DEBT_MAP)"),
        ("us-gaap_OtherLongTermDebtCurrent", "statements.py:158 (SHORT_TERM_DEBT_MAP)"),
        ("us-gaap_ShortTermBorrowings", "statements.py:161 (SHORT_TERM_DEBT_MAP)"),
        ("us-gaap_CommercialPaper", "statements.py:161 (SHORT_TERM_DEBT_MAP)"),
        ("us-gaap_NotesPayableCurrent", "statements.py:161 (SHORT_TERM_DEBT_MAP)"),
        ("us-gaap_DebtCurrent", "statements.py:164 (SHORT_TERM_DEBT_LAST)"),
    ),
}

#: Items whose resolver reads a concept that cannot be listed as data (a caption-driven route), so
#: they are measured at the item level only.
ITEM_LEVEL_ONLY = {
    "capital_expenditure": (
        "the REIT extension route (_reit_extension_capex) matches the filer's own captions, so its "
        "concepts are not a fixed list -- the item is measured at the item level only"
    ),
}


def split_concept(concept: str) -> tuple[str | None, str]:
    """``us-gaap_Revenues`` -> ``("us-gaap", "Revenues")``; a company prefix -> ``(None, name)``."""
    prefix, _, name = concept.partition("_")
    if not name:
        return None, concept
    if prefix in STANDARD_PREFIXES:
        return prefix, name
    return None, concept


def item_concepts() -> dict[str, dict[str, Any]]:
    """Every registry item: its concept set (with the sources), its statement kind, its resolver."""
    out: dict[str, dict[str, Any]] = {}
    for name, item in REGISTRY.items():
        sources: list[tuple[str, str]] = []
        concepts: set[str] = set()
        for field in ("concepts", "total_concepts", "fallback_concepts", "fallback_addends"):
            values = getattr(item, field)
            if values:
                sources.append((field, f"REGISTRY[{name!r}].{field}"))
            concepts.update(values)
        if item.resolver:
            for concept, where in COMPOSITE_CONCEPTS.get(item.resolver, ()):
                sources.append((concept, where))
            concepts.update(c for c, _w in COMPOSITE_CONCEPTS.get(item.resolver, ()))
        out[name] = {
            "concepts": sorted(concepts),
            "sources": sources,
            "statements": item.statements,
            "kind": "instant" if "balance_sheet" in item.statements else "duration",
            "resolver": item.resolver,
        }
    return out


def main() -> int:  # noqa: C901, PLR0912, PLR0915 - one linear measurement pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-csv", required=True)
    args = parser.parse_args()

    items = item_concepts()
    # (taxonomy, name) sets per item, and the company-specific concepts per item
    sec_sets: dict[str, set[tuple[str, str]]] = {}
    company_specific: dict[str, set[str]] = {}
    for name, spec in items.items():
        pairs: set[tuple[str, str]] = set()
        comp: set[str] = set()
        for concept in spec["concepts"]:
            tax, cname = split_concept(concept)
            if tax is None:
                comp.add(concept)
            else:
                pairs.add((tax, cname))
        sec_sets[name] = pairs
        company_specific[name] = comp

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    filings = [
        dict(r)
        for r in conn.execute(
            "SELECT f.id, f.asset_id, f.form, f.fiscal_period, f.accession_number, f.period_end, "
            "a.ticker, a.cik FROM sec_filings f JOIN assets a ON a.id = f.asset_id"
        )
    ]
    by_cik: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in filings:
        by_cik[str(f["cik"])].append(f)

    # -- the pilot side: a non-null value for the item's concepts at the filing's period end --
    have: dict[tuple[int, str], set[str]] = defaultdict(set)
    for row in conn.execute(
        "SELECT ff.filing_id, ff.concept, ff.period_key FROM financial_facts ff "
        "WHERE ff.value IS NOT NULL"
    ):
        filing_id = int(row["filing_id"])
        period = str(row["period_key"]).split(" ", 1)[0]
        have[(filing_id, period)].add(str(row["concept"]))

    # -- the SEC side: the cached companyfacts of every CIK family --
    cache = cache_dir(args.cache)

    def facts_for(cik: str) -> dict[tuple[str, str], list[dict[str, Any]]]:
        """``{(accn, end): [fact, ...]}`` of the CIK family's cached companyfacts.

        No document is kept: the cache is 2.2 GB of JSON and every CIK is visited once.
        """
        out: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for member in cik_family(cik):
            path = cache / "companyfacts" / f"CIK{member}.json"
            if not path.exists():
                continue
            doc = json.loads(path.read_text())
            for tax in TAXONOMIES:
                for concept, group in (doc.get("facts") or {}).get(tax, {}).items():
                    for _unit, unit_facts in group.get("units", {}).items():
                        for fact in unit_facts:
                            out.setdefault(
                                (str(fact.get("accn")), str(fact.get("end"))), []
                            ).append({**fact, "_tax": tax, "_concept": concept})
        return out

    losses: list[dict[str, Any]] = []
    company_specific_cases: list[dict[str, Any]] = []
    control: dict[str, Any] = {}

    for cik, group in sorted(by_cik.items()):
        facts = facts_for(cik)
        for filing in group:
            accn, end = str(filing["accession_number"]), str(filing["period_end"])
            rows = facts.get((accn, end), [])
            for name, spec in items.items():
                pairs = sec_sets[name]
                if not pairs:
                    continue
                kind = spec["kind"]
                sec_hit: dict[str, Any] | None = None
                for fact in rows:
                    if (fact["_tax"], fact["_concept"]) not in pairs:
                        continue
                    if kind == "instant":
                        if fact.get("start") is not None:
                            continue
                    elif not duration_matches(str(filing["form"]), fact.get("start"), end):
                        continue
                    sec_hit = {
                        "taxonomy": fact["_tax"],
                        "concept": fact["_concept"],
                        "value": fact.get("val"),
                        "start": fact.get("start"),
                    }
                    break
                if sec_hit is None:
                    continue
                # the pilot side: a non-null value for any of the item's concepts at that period
                stored = have.get((int(filing["id"]), end), set())
                if stored & set(spec["concepts"]):
                    continue
                losses.append(
                    {
                        "ticker": filing["ticker"],
                        "cik": str(filing["cik"]),
                        "form": filing["form"],
                        "fiscal_period": filing["fiscal_period"],
                        "accession_number": accn,
                        "period_end": end,
                        "item": name,
                        "sec_taxonomy": sec_hit["taxonomy"],
                        "sec_concept": sec_hit["concept"],
                        "sec_value": sec_hit["value"],
                    }
                )
                for concept in sorted(company_specific.get(name, ())):
                    company_specific_cases.append(
                        {
                            "ticker": filing["ticker"],
                            "cik": str(filing["cik"]),
                            "form": filing["form"],
                            "fiscal_period": filing["fiscal_period"],
                            "accession_number": accn,
                            "item": name,
                            "concept": concept,
                        }
                    )

    # -- the item level: does the item still resolve a value? (as itemcheck.py reads it) --
    loss_filings = {int(f["id"]) for f in filings}
    resolved: dict[int, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT filing_id, metric_name, inputs_json FROM fundamental_metrics WHERE inputs_json IS NOT NULL"
    ):
        filing_id = int(row["filing_id"])
        if filing_id not in loss_filings:
            continue
        inputs = json.loads(row["inputs_json"] or "{}")
        provenance = inputs.get("provenance") or {}
        items_read = provenance.get("items") or {}
        for item_name, entries in items_read.items():
            value = inputs.get(item_name)
            concept = None
            if isinstance(entries, list) and entries:
                first = entries[0]
                concept = first.get("concept") if isinstance(first, dict) else None
            if value is None and concept is None:
                continue
            bucket = resolved.setdefault(filing_id, {})
            if item_name not in bucket or (
                bucket[item_name].get("value") is None and value is not None
            ):
                bucket[item_name] = {
                    "value": value,
                    "concept": concept,
                    "metric": row["metric_name"],
                }

    # The cover-page count is the pipeline's other route for ``shares_outstanding``.
    cover_counts: dict[int, int] = {
        int(r["filing_id"]): int(r["n"])
        for r in conn.execute(
            "SELECT filing_id, COUNT(*) n FROM filing_cover_shares GROUP BY filing_id"
        )
    }
    for loss in losses:
        filing = next(
            f
            for f in filings
            if f["accession_number"] == loss["accession_number"]
            and f["ticker"] == loss["ticker"]
            and f["fiscal_period"] == loss["fiscal_period"]
        )
        entry = resolved.get(int(filing["id"]), {}).get(loss["item"])
        # The cover-page count is the pipeline's other route for ``shares_outstanding``: the
        # ``filing_cover_shares`` table, not ``financial_facts``.
        via_metric = bool(entry and entry.get("value") is not None)
        via_cover = (
            loss["item"] == "shares_outstanding" and cover_counts.get(int(filing["id"]), 0) > 0
        )
        loss["resolved_elsewhere"] = via_metric or via_cover
        loss["resolved_route"] = (
            "metric input" if via_metric else ("cover-page count" if via_cover else "")
        )
        loss["resolved_concept"] = (entry or {}).get("concept")
        loss["resolved_metric"] = (entry or {}).get("metric")
        concept = loss.get("resolved_concept")
        if concept:
            tax, _name = split_concept(str(concept))
            if tax is None:
                company_specific_cases.append(
                    {
                        "ticker": loss["ticker"],
                        "cik": loss["cik"],
                        "form": loss["form"],
                        "fiscal_period": loss["fiscal_period"],
                        "accession_number": loss["accession_number"],
                        "item": loss["item"],
                        "concept": concept,
                        "route": "resolved elsewhere (the value the pipeline stored)",
                    }
                )

    # -- the control cases --
    def control_case(ticker: str, accession: str, item: str) -> dict[str, Any]:
        hits = [
            loss
            for loss in losses
            if loss["ticker"] == ticker
            and loss["accession_number"] == accession
            and loss["item"] == item
        ]
        return {
            "ticker": ticker,
            "accession_number": accession,
            "item": item,
            "is_loss": bool(hits),
            "detail": hits[:3],
        }

    control = {
        "PHM_10Q_2023Q3_revenue": control_case("PHM", "0000822416-23-000042", "revenue"),
        "LEN_10K_FY2022_revenue": control_case("LEN", "0001628280-23-001606", "revenue"),
    }

    by_item: dict[str, dict[str, Any]] = {}
    for loss in losses:
        row = by_item.setdefault(
            loss["item"],
            {"pairs": 0, "filings": set(), "companies": set(), "lost": 0, "resolved": 0},
        )
        row["pairs"] += 1
        row["filings"].add((loss["ticker"], loss["accession_number"]))
        row["companies"].add(loss["ticker"])
        if loss["resolved_elsewhere"]:
            row["resolved"] += 1
        else:
            row["lost"] += 1

    per_company = Counter(loss["ticker"] for loss in losses)
    doc = {
        "db": args.db,
        "cache": str(cache),
        "filings": len(filings),
        "companies": len({f["cik"] for f in filings}),
        "items": len(items),
        "item_concepts": {
            name: {"concepts": spec["concepts"], "kind": spec["kind"], "sources": spec["sources"]}
            for name, spec in items.items()
        },
        "item_level_only": ITEM_LEVEL_ONLY,
        "control": control,
        "losses": len(losses),
        "losses_lost": sum(1 for loss in losses if not loss["resolved_elsewhere"]),
        "losses_resolved_elsewhere": sum(1 for loss in losses if loss["resolved_elsewhere"]),
        "loss_filings": len({(loss["ticker"], loss["accession_number"]) for loss in losses}),
        "loss_companies": len(per_company),
        "by_item": {
            name: {
                "pairs": row["pairs"],
                "filings": len(row["filings"]),
                "companies": len(row["companies"]),
                "lost": row["lost"],
                "resolved_elsewhere": row["resolved"],
            }
            for name, row in sorted(by_item.items(), key=lambda kv: -kv[1]["pairs"])
        },
        "company_specific_cases": len(company_specific_cases),
        "company_specific_by_item": dict(
            Counter(case["item"] for case in company_specific_cases).most_common()
        ),
        "per_company": dict(per_company.most_common()),
        "loss_rows": losses,
    }
    Path(args.out_json).write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n")
    with open(args.out_csv, "w", newline="") as handle:
        handle.write(
            "ticker,cik,form,fiscal_period,accession_number,period_end,item,"
            "sec_taxonomy,sec_concept,sec_value,lost_or_resolved_elsewhere,resolved_concept\n"
        )
        for loss in losses:
            handle.write(
                f"{loss['ticker']},{loss['cik']},{loss['form']},{loss['fiscal_period']},"
                f"{loss['accession_number']},{loss['period_end']},{loss['item']},"
                f"{loss['sec_taxonomy']},{loss['sec_concept']},{loss['sec_value']},"
                f"{'resolved_elsewhere' if loss['resolved_elsewhere'] else 'lost'},"
                f"{loss.get('resolved_concept') or ''}\n"
            )
    print(
        json.dumps(
            {
                "filings": doc["filings"],
                "losses": doc["losses"],
                "lost": doc["losses_lost"],
                "resolved_elsewhere": doc["losses_resolved_elsewhere"],
                "loss_filings": doc["loss_filings"],
                "loss_companies": doc["loss_companies"],
                "company_specific_cases": doc["company_specific_cases"],
                "control": {k: v["is_loss"] for k, v in control.items()},
            },
            indent=1,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
