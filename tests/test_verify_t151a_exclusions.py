"""The declared exclusions of the 3.2' face check (ID-01, checklist C13 p.25).

A company-specific (extension) concept is read only through a documented fallback, and the debt
resolver has none: such an element is **excluded** from the sum, so the filer's own total exceeds
our sum by exactly that element. The difference is the declared exclusion, not a failure
(reviewer A, P3 round 1, 2026-10-20: CNP's ``cnp_TransitionAndSystemRestorationBondsLongTermDebtNoncurrent``).
"""

from __future__ import annotations

from typing import Any, cast

from verify_t151a import _declared_exclusion

ELEMENT = "cnp_TransitionAndSystemRestorationBondsLongTermDebtNoncurrent"
FILED = "cnp_TransitionAndSystemRestorationBondsLongTermDebtNonCurrent"


class _Statements:
    """The face the check reads: the excluded element's filed value, under the filed spelling."""

    def __init__(self, value: float | None) -> None:
        self._value = value

    def _rows_for(self, _spec: Any) -> list[dict[str, Any]]:
        if self._value is None:
            return []
        return [{"concept": FILED}]

    def _concept_value(self, _spec: Any, _column: str, concept: str) -> float | None:
        return self._value if concept == FILED else None


def _exclusion(
    value: float | None, *, ticker: str = "CNP", diff: float = 664_000_000.0
) -> str | None:
    return cast(
        "str | None",
        _declared_exclusion(ticker, "2025-12-31", _Statements(value), diff, 1000.0),
    )


def test_the_declared_exclusion_explains_the_difference() -> None:
    explained = _exclusion(664_000_000.0)
    assert explained is not None
    assert ELEMENT in explained
    assert "ID-01" in explained


def test_a_difference_the_excluded_element_does_not_explain_is_not_explained() -> None:
    assert _exclusion(1_000.0) is None
    assert _exclusion(None) is None


def test_another_company_is_not_covered_by_cnps_exclusion() -> None:
    assert _exclusion(664_000_000.0, ticker="AAPL") is None
