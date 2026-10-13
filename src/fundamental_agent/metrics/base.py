"""Shared helpers for the deterministic metric skills."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

_EPSILON = 1e-9


@dataclass(frozen=True)
class MetricResult:
    """One computed ratio plus the raw inputs it came from (for auditability).

    ``inputs`` holds the flat audit values (``flag_<item>_<flag>`` = 1.0 among them) and, from
    T-151 part 3 (D14), one ``provenance`` block per metric (a nested dict), so the value type is
    ``Any``."""

    name: str
    value: float | None
    unit: str  # "ratio" | "pct" | "x" | "usd"
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TTMFlow:
    """A trailing-twelve-month flow and how it was obtained (T-105): ``"ytd"`` (prior fiscal
    year + this year-to-date - last year's same year-to-date), ``"quarters"`` (four recorded
    quarters) or ``"x4"`` (this quarter times four -- crude annualization, not a TTM)."""

    value: float
    method: str
    # T-105 review: the four-quarter sum, when it was computable beside the identity -- kept
    # only for the cross-check, never used as the value.
    alt: float | None = None
    # T-149 (PER-05): the filing's own year-to-date value of the flow (three, six or nine months),
    # recorded so a later Q4 can be the fiscal year less the third quarter's nine months.
    # (not part of the flow's identity: it compares equal with or without it)
    ytd: float | None = field(default=None, compare=False)
    flags: tuple[str, ...] = ()


def safe_div(numerator: float | None, denominator: float | None) -> float | None:
    """Divide, returning ``None`` on missing operands or a zero/near-zero divisor."""
    if numerator is None or denominator is None or abs(denominator) < _EPSILON:
        return None
    return numerator / denominator


def present(**values: float | None) -> dict[str, float]:
    """Collect the non-``None`` inputs of a metric into an audit dict."""
    return {key: val for key, val in values.items() if val is not None}


def sum_present(*values: float | None) -> float | None:
    """Sum the operands that are not ``None``; ``None`` if every operand is missing."""
    known = [value for value in values if value is not None]
    return float(sum(known)) if known else None


def flag_inputs(item: str, flags: Iterable[str]) -> dict[str, float]:
    """The resolution flags of one line item as audit inputs (T-148): ``flag_<item>_<flag>`` = 1.0
    -- a partial debt map, a rejected sign, a line that includes leases. Absent when there is none,
    so an unflagged value carries no key."""
    return {f"flag_{item}_{flag}": 1.0 for flag in flags}
