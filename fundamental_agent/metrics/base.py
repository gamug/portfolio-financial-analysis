"""Shared helpers for the deterministic metric skills."""

from __future__ import annotations

from dataclasses import dataclass, field

_EPSILON = 1e-9


@dataclass(frozen=True)
class MetricResult:
    """One computed ratio plus the raw inputs it came from (for auditability)."""

    name: str
    value: float | None
    unit: str  # "ratio" | "pct" | "x" | "usd"
    inputs: dict[str, float] = field(default_factory=dict)


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
