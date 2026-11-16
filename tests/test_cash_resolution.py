"""T-148 part 2 (k): Cash composite resolver _cash (checklist ID-02a, B4, B6).

* Tier 1: CashAndCashEquivalentsAtCarryingValue -> concept.
* Tier 2: CashCashEquivalentsAndShortTermInvestments -> concept, caption checked
  (names only cash -> cash_sti_tag_cash_caption; names investments -> includes_short_term_investments).
* Tier 3: CashAndDueFromBanks -> concept.
* Tier 4: ES map (Cash + CashEquivalentsAtCarryingValue) -> map, cash_plus_cash_equivalents_map.
* Tier 5: (k)(1) CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents on balance sheet,
  caption checked (no 'restrict') and no other balance sheet row with 'restrict'
  -> concept, cash_tag_includes_restricted_cash_possible.
* Tier 6: (k)(3) Cash alone on classified balance sheet (current_assets and current_liabilities present)
  -> concept, cash_element_only.
* Otherwise empty.
"""

from __future__ import annotations

from fundamental_agent.metrics import liquidity, valuation
from fundamental_agent.pricing import ClosePrice
from fundamental_agent.statements import Statements


def test_csx_k1_cash_taken_when_caption_is_cash_and_no_bs_restriction() -> None:
    """CSX FY2023 10-K (accession 0000277948-24-000010):
    Row tagged us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents
    with caption "Cash and Cash Equivalents" (1,421M) and no other balance-sheet restriction
    row is taken, flagged cash_tag_includes_restricted_cash_possible."""
    payload = {
        "balance_sheet": [
            {
                "concept": "us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
                "label": "Cash and Cash Equivalents",
                "2023-12-31": 1_421_000_000.0,
                "preferred_sign": 1.0,
            },
            {
                "concept": "us-gaap_AccountsReceivableNetCurrent",
                "label": "Accounts receivable",
                "2023-12-31": 1_200_000_000.0,
                "preferred_sign": 1.0,
            },
        ]
    }
    stmts = Statements.from_payload(payload)
    res = stmts.resolve("cash", "2023-12-31")
    assert res.value == 1_421_000_000.0
    assert res.concept == "us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"
    assert res.how == "concept"
    assert res.flags == ("cash_tag_includes_restricted_cash_possible",)


def test_k1_refused_when_caption_names_restricted_cash() -> None:
    """(k)(1) refused when caption contains 'restrict': caption
    "Cash, cash equivalents and restricted cash" indicates restricted cash is included."""
    payload = {
        "balance_sheet": [
            {
                "concept": "us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
                "label": "Cash, cash equivalents and restricted cash",
                "2023-12-31": 1_421_000_000.0,
                "preferred_sign": 1.0,
            }
        ]
    }
    stmts = Statements.from_payload(payload)
    res = stmts.resolve("cash", "2023-12-31")
    assert res.value is None
    assert res.flags == ()


def test_k1_refused_when_balance_sheet_has_separate_restricted_cash_row() -> None:
    """(k)(1) refused when another non-abstract, non-dimensional balance-sheet row has
    'restrict' in its concept local name or caption (e.g. RestrictedCashCurrent)."""
    payload = {
        "balance_sheet": [
            {
                "concept": "us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
                "label": "Cash and Cash Equivalents",
                "2023-12-31": 1_421_000_000.0,
                "preferred_sign": 1.0,
            },
            {
                "concept": "us-gaap_RestrictedCashCurrent",
                "label": "Restricted cash, current",
                "2023-12-31": 25_000_000.0,
                "preferred_sign": 1.0,
            },
        ]
    }
    stmts = Statements.from_payload(payload)
    res = stmts.resolve("cash", "2023-12-31")
    assert res.value is None
    assert res.flags == ()


def test_tgt_k2_cash_sti_caption_check() -> None:
    """Target FY2021 10-K (accession 0000027419-22-000007):
    Row tagged us-gaap_CashCashEquivalentsAndShortTermInvestments with caption
    "Cash and cash equivalents" (5,911M) names only cash (B4 mis-tag) ->
    flagged cash_sti_tag_cash_caption."""
    payload_tgt = {
        "balance_sheet": [
            {
                "concept": "us-gaap_CashCashEquivalentsAndShortTermInvestments",
                "label": "Cash\xa0and\xa0cash\xa0equivalents",
                "2022-01-29": 5_911_000_000.0,
                "preferred_sign": 1.0,
            }
        ]
    }
    stmts_tgt = Statements.from_payload(payload_tgt)
    res_tgt = stmts_tgt.resolve("cash", "2022-01-29")
    assert res_tgt.value == 5_911_000_000.0
    assert res_tgt.concept == "us-gaap_CashCashEquivalentsAndShortTermInvestments"
    assert res_tgt.flags == ("cash_sti_tag_cash_caption",)

    # When caption names investments -> flagged includes_short_term_investments
    payload_inv = {
        "balance_sheet": [
            {
                "concept": "us-gaap_CashCashEquivalentsAndShortTermInvestments",
                "label": "Cash, cash equivalents and short-term investments",
                "2022-01-29": 5_911_000_000.0,
                "preferred_sign": 1.0,
            }
        ]
    }
    stmts_inv = Statements.from_payload(payload_inv)
    res_inv = stmts_inv.resolve("cash", "2022-01-29")
    assert res_inv.value == 5_911_000_000.0
    assert res_inv.flags == ("includes_short_term_investments",)


def test_es_cash_plus_cash_equivalents_map() -> None:
    """Eversource Energy FY2022 10-K (accession 0000072741-23-000004):
    Files us-gaap_Cash (50M) and us-gaap_CashEquivalentsAtCarryingValue (150M) without
    CashAndCashEquivalentsAtCarryingValue: sum is read as map, flagged
    cash_plus_cash_equivalents_map."""
    payload = {
        "balance_sheet": [
            {
                "concept": "us-gaap_Cash",
                "label": "Cash",
                "2022-12-31": 50_000_000.0,
                "preferred_sign": 1.0,
            },
            {
                "concept": "us-gaap_CashEquivalentsAtCarryingValue",
                "label": "Cash equivalents",
                "2022-12-31": 150_000_000.0,
                "preferred_sign": 1.0,
            },
        ]
    }
    stmts = Statements.from_payload(payload)
    res = stmts.resolve("cash", "2022-12-31")
    assert res.value == 200_000_000.0
    assert res.concept == "us-gaap_Cash + us-gaap_CashEquivalentsAtCarryingValue"
    assert res.how == "map"
    assert res.flags == ("cash_plus_cash_equivalents_map",)


def test_slb_k3_cash_element_only_on_classified_balance_sheet() -> None:
    """Schlumberger (SLB) FY2021 10-K: files us-gaap_Cash alone (3,149M) and the
    balance sheet is classified (current assets and liabilities present) ->
    taken, flagged cash_element_only."""
    payload = {
        "balance_sheet": [
            {
                "concept": "us-gaap_Cash",
                "label": "Cash",
                "2021-12-31": 3_149_000_000.0,
                "preferred_sign": 1.0,
            },
            {
                "concept": "us-gaap_AssetsCurrent",
                "label": "Total current assets",
                "2021-12-31": 15_000_000_000.0,
                "preferred_sign": 1.0,
            },
            {
                "concept": "us-gaap_LiabilitiesCurrent",
                "label": "Total current liabilities",
                "2021-12-31": 10_000_000_000.0,
                "preferred_sign": 1.0,
            },
        ]
    }
    stmts = Statements.from_payload(payload)
    res = stmts.resolve("cash", "2021-12-31")
    assert res.value == 3_149_000_000.0
    assert res.concept == "us-gaap_Cash"
    assert res.how == "concept"
    assert res.flags == ("cash_element_only",)


def test_the_bare_cash_line_is_the_cash_on_an_unclassified_balance_sheet() -> None:
    """T-159a: the bare ``us-gaap_Cash`` line ("Amount of currency on hand as well as demand
    deposits with banks or financial institutions", the FASB 2026 taxonomy) is the cash of a filer
    that files no cash-equivalents fact -- read on an unclassified balance sheet too, flagged
    ``cash_element_only`` (the reader must know the cash excludes the equivalents)."""
    payload = {
        "balance_sheet": [
            {
                "concept": "us-gaap_Cash",
                "label": "Cash",
                "2021-12-31": 500_000_000.0,
                "preferred_sign": 1.0,
            },
            {
                "concept": "us-gaap_Assets",
                "label": "Total assets",
                "2021-12-31": 50_000_000_000.0,
                "preferred_sign": 1.0,
            },
        ]
    }
    stmts = Statements.from_payload(payload)
    res = stmts.resolve("cash", "2021-12-31")
    assert res.value == 500_000_000.0
    assert res.concept == "us-gaap_Cash"
    assert res.how == "substitute"
    assert res.flags == ("cash_element_only",)


def test_the_bare_cash_line_is_refused_when_a_cash_equivalents_fact_is_filed() -> None:
    """T-159a: the substitute is read only when no ``CashEquivalentsAtCarryingValue`` is filed --
    with one, the cash and cash equivalents are the map's own read (ES map), never the bare line."""
    payload = {
        "balance_sheet": [
            {
                "concept": "us-gaap_Cash",
                "label": "Cash",
                "2021-12-31": 500_000_000.0,
                "preferred_sign": 1.0,
            },
            {
                "concept": "us-gaap_CashEquivalentsAtCarryingValue",
                "label": "Cash equivalents",
                "2021-12-31": 20_000_000.0,
                "preferred_sign": 1.0,
            },
        ]
    }
    stmts = Statements.from_payload(payload)
    res = stmts.resolve("cash", "2021-12-31")
    assert res.value == 520_000_000.0
    assert res.concept == "us-gaap_Cash + us-gaap_CashEquivalentsAtCarryingValue"
    assert res.how == "map"


def test_cash_flags_reach_liquidity_and_valuation_inputs() -> None:
    """Check that flag_inputs('cash', ...) reaches both liquidity and valuation inputs."""
    payload = {
        "balance_sheet": [
            {
                "concept": "us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
                "label": "Cash and Cash Equivalents",
                "2023-12-31": 1_000_000_000.0,
                "preferred_sign": 1.0,
            },
            {
                "concept": "us-gaap_AssetsCurrent",
                "label": "Total current assets",
                "2023-12-31": 3_000_000_000.0,
                "preferred_sign": 1.0,
            },
            {
                "concept": "us-gaap_LiabilitiesCurrent",
                "label": "Total current liabilities",
                "2023-12-31": 2_000_000_000.0,
                "preferred_sign": 1.0,
            },
            {
                "concept": "us-gaap_StockholdersEquity",
                "label": "Common stockholders equity",
                "2023-12-31": 5_000_000_000.0,
                "preferred_sign": 1.0,
            },
            {
                "concept": "us-gaap_LongTermDebtNoncurrent",
                "label": "Long-term debt",
                "2023-12-31": 2_000_000_000.0,
                "preferred_sign": 1.0,
            },
            {
                "concept": "dei_EntityCommonStockSharesOutstanding",
                "label": "Shares outstanding",
                "2023-12-31": 100_000_000.0,
                "preferred_sign": 1.0,
            },
        ]
    }
    stmts = Statements.from_payload(payload)
    key = "2023-12-31"

    # Liquidity check
    liq_results = {r.name: r for r in liquidity.compute(stmts, key)}
    assert (
        liq_results["cash_ratio"].inputs["flag_cash_cash_tag_includes_restricted_cash_possible"]
        == 1.0
    )

    # Valuation check
    price = ClosePrice(date="2023-12-31", close=50.0)
    val_results = {
        r.name: r
        for r in valuation.compute(
            stmts,
            key,
            price=price,
        )
    }
    assert (
        val_results["enterprise_value"].inputs[
            "flag_cash_cash_tag_includes_restricted_cash_possible"
        ]
        == 1.0
    )
