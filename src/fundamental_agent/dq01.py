"""DQ-01's tolerance, in one leaf module (T-071 part 2b, D6).

DQ-01 compares two filed values with ``threshold = max(L-10 rounding, 0.5% x |value|)``; the
rounding term is ``0.5 x unit x n_terms`` (L-10: ``decimals`` is not captured, so the unit is the
largest of USD 1 million and USD 1 thousand that divides every term exactly, else USD 1).

The helper lives here -- a leaf with no project imports -- because **three** layers need it:
``statements`` (a resolver comparing a filer's own total with the parts it presents, T-071 part 2b's
D6 no-total check), ``quality`` (the accounting-identity gates, T-150) and ``fidelity`` (the
companyfacts comparison). Importing it from ``quality`` would close the cycle
``statements -> quality -> metrics -> cashflow -> statements``; ``quality`` and ``fidelity``
re-export it so their existing callers are unchanged.
"""

from __future__ import annotations

from typing import Final

#: DQ-01's materiality: 0.5% of the base.
MATERIALITY: Final[float] = 0.005


def dq01_tolerance(value: float, n_terms: int = 1) -> float:
    """DQ-01's threshold: ``max(L-10 rounding, 0.5% x |value|)``.

    *n_terms* is the number of filed values entering the comparison -- 1 for a single fact, the
    parts + 1 for a sourced sum against the filer's own total."""
    unit = 1.0
    for candidate in (1_000_000.0, 1_000.0):
        if value % candidate == 0.0:
            unit = candidate
            break
    return max(0.5 * unit * n_terms, MATERIALITY * abs(value))


__all__ = ["MATERIALITY", "dq01_tolerance"]
