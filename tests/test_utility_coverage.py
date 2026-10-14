"""``fundamental_agent.utility_coverage``: the D-13 working-capital classification (T-071 part 2a)."""

from __future__ import annotations

from fundamental_agent import utility_coverage as uc


def test_a_current_element_is_removed_from_cfo() -> None:
    for concept in (
        "us-gaap_IncreaseDecreaseInInventories",
        "us-gaap_IncreaseDecreaseInAccountsPayable",
        "us-gaap_IncreaseDecreaseInReceivables",
        "us-gaap_IncreaseDecreaseInPrepaidExpense",
        "us-gaap_IncreaseDecreaseInOtherCurrentAssets",
        "us-gaap_IncreaseDecreaseInIncomeTaxesReceivable",
    ):
        assert uc.classification(concept)[0] == uc.CURRENT, concept


def test_a_noncurrent_regulatory_pension_or_aro_element_stays_in_the_cash_flow() -> None:
    assert uc.classification("us-gaap_IncreaseDecreaseInOtherNoncurrentAssets") == (
        uc.IN,
        uc.NONCURRENT,
    )
    assert uc.classification("us-gaap_IncreaseDecreaseInRegulatoryLiabilities") == (
        uc.IN,
        uc.REGULATORY,
    )
    assert uc.classification("us-gaap_IncreaseDecreaseInPensionAndPostretirementObligations") == (
        uc.IN,
        uc.PENSION,
    )
    assert uc.classification("us-gaap_IncreaseDecreaseInAssetRetirementObligations") == (
        uc.IN,
        uc.ASSET_RETIREMENT,
    )


def test_a_definition_that_does_not_say_current_is_left_in_and_ambiguous() -> None:
    """The director's fallback (rule 3): not clearly a current item -> left in, and the sensitivity
    compares it in vs out."""
    assert uc.classification("us-gaap_IncreaseDecreaseInOtherOperatingCapitalNet") == (
        uc.IN,
        uc.AMBIGUOUS,
    )
    assert uc.classification("us-gaap_IncreaseDecreaseInDerivativeAssets") == (uc.IN, uc.AMBIGUOUS)
    assert uc.classification("ceg_IncreaseDecreaseInOptionPremiumsCollectedReceived") == (
        uc.IN,
        uc.AMBIGUOUS,
    )


def test_an_element_outside_the_table_is_left_in_and_unclassified() -> None:
    assert uc.classification("us-gaap_IncreaseDecreaseInSomethingNew") == (uc.IN, uc.UNCLASSIFIED)


def test_the_cash_reconciliation_is_not_a_working_capital_line() -> None:
    assert uc._is_cash_reconciliation(
        "us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"
        "PeriodIncreaseDecreaseIncludingExchangeRateEffect"
    )
    assert uc._is_cash_reconciliation(
        "us-gaap_CashAndCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect"
    )
    assert not uc._is_cash_reconciliation("us-gaap_IncreaseDecreaseInInventories")


def test_the_cutoff_is_the_ba_bands_lower_bound() -> None:
    assert uc.THRESHOLD == 0.05
    assert uc.YEARS == 3
