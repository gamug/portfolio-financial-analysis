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


def test_a_definition_that_states_the_period_settles_the_class() -> None:
    """Rule 1: a B6 definition that states the period makes the class -- DerivativeAssets says
    "due to be disposed of within one year", so it is current, not ambiguous."""
    assert uc.classification("us-gaap_IncreaseDecreaseInDerivativeAssets") == (
        uc.CURRENT,
        "definition: within one year",
    )
    assert uc.classification("us-gaap_IncreaseDecreaseInDerivativeAssetsAndLiabilities")[0] == (
        uc.CURRENT
    )
    assert uc.classification("us-gaap_IncreaseDecreaseInInsuranceSettlementsReceivable")[0] == (
        uc.CURRENT
    )


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


def test_a_silent_definition_is_classified_from_the_filers_own_caption() -> None:
    """Rule 1: a standard element whose definition is silent reads the filer's caption for that
    line, as an extension does."""
    concept = "us-gaap_IncreaseDecreaseInOtherOperatingAssets"
    assert uc.classification(concept, "Other current assets") == (uc.CURRENT, "caption")
    assert uc.classification(concept, "Change in other long-term assets") == (uc.IN, uc.NONCURRENT)
    assert uc.classification(concept, "Other — net assets") == (uc.IN, uc.AMBIGUOUS)


def test_a_caption_that_mixes_current_and_noncurrent_is_ambiguous() -> None:
    assert uc.classification(
        "us-gaap_IncreaseDecreaseInOtherOperatingCapitalNet",
        "Other current and noncurrent assets and liabilities",
    ) == (uc.IN, uc.AMBIGUOUS)


def test_a_regulatory_caption_stays_in_the_cash_flow_even_when_it_says_current() -> None:
    assert uc.classification(
        "us-gaap_IncreaseDecreaseInSomethingSilent", "Decrease in current regulatory asset"
    ) == (uc.IN, uc.REGULATORY)


def test_an_extension_is_classified_from_its_caption() -> None:
    assert uc.classification("evrg_IncreaseDecreaseInEnergyRelatedInventory")[0] == uc.CURRENT
    assert uc.classification("pnw_IncreaseDecreaseDeferredFuelAndPurchasedPowerAmortization") == (
        uc.IN,
        uc.REGULATORY,
    )
    assert uc.classification("pcg_IncreaseDecreaseInContributions") == (uc.IN, uc.AMBIGUOUS)


def test_the_cash_reconciliation_is_not_a_working_capital_line() -> None:
    assert uc._is_cash_reconciliation(
        "us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"
        "PeriodIncreaseDecreaseIncludingExchangeRateEffect"
    )
    assert uc._is_cash_reconciliation(
        "us-gaap_CashAndCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect"
    )
    assert not uc._is_cash_reconciliation("us-gaap_IncreaseDecreaseInInventories")


def test_the_two_bounds_and_the_cutoff_are_declared() -> None:
    assert uc.THRESHOLD == 0.05
    assert uc.SENSITIVITY == (0.05, 0.12)
    assert uc.YEARS == 3
    assert (uc.AMBIGUOUS_IN, uc.AMBIGUOUS_AS_WC) == (
        "average_ambiguous_in",
        "average_ambiguous_as_wc",
    )
