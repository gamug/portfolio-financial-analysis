"""Apply the applicability matrix to the computed metrics (T-071 part 1, P4; APP-00, APP-01..09, MKT-03).

One place: :func:`apply_applicability` runs over the ``(group, result)`` pairs the metric groups have
just computed and **before** :func:`fundamental_agent.metrics.provenance.attach`, so the provenance
block's empty reason reads the NA cell:

* an **NA** cell empties the value and stamps ``flag_<metric>_not_applicable`` (the rule that decided
  the cell travels beside it as the ``applicability_rule`` input); the provenance block's
  ``empty_reason`` is then ``not_applicable`` (``provenance._FLAG_REASONS``);
* an **ADJ** cell keeps the value and stamps ``flag_<metric>_adj``;
* an **unknown profile** computes the metric and stamps ``flag_<metric>_company_type_unknown`` --
  never a guessed type (P4).

The negative-equity overlay is not a profile field: APP-07's condition is the one the metric's own
code already evaluated. That keeps ROE's denominator the prior year's equity, exactly as
``metrics/profitability.py`` computes it (R6, 2026-10-15: the matrix reads a metric's own
``flag_<metric>_negative_equity`` and never another metric's flag from the shared group inputs).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Final

from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.metrics.provenance import (
    ADJUSTED_FLAG,
    APPLICABILITY_RULE,
    NOT_APPLICABLE_FLAG,
    UNKNOWN_TYPE_FLAG,
)
from kg_schema import applicability
from kg_schema.company_profile import CompanyProfile

# The metrics whose own code tests the book equity **they** divide by, so the negative-equity column
# reads that metric's own flag and nothing else (R6, 2026-10-15). ``return_on_equity`` divides by the
# prior year's equity (MET-02; APP-07's source C7 p.12 ties "meaningless" to the book value the ratio
# divides by); ``debt_to_equity`` by the current equity (``metrics/leverage.py``). A metric with no
# own flag falls back to the current equity input -- ROIC, whose APP-07 cell is ADJ.
OWN_NEGATIVE_EQUITY_FLAG: Final[dict[str, str]] = {
    "return_on_equity": "flag_return_on_equity_negative_equity",
    "debt_to_equity": "flag_debt_to_equity_negative_equity",
}


def metric_negative_equity(result: MetricResult) -> bool:
    """APP-07's negative-book-equity condition, as the metric's own code evaluated it.

    A metric that carries its **own** denominator test is decided by its own flag alone -- never by a
    flag another metric of the shared group inputs left behind (a negative current equity beside a
    positive prior-year equity must not empty ROE). Every other metric that reads an ``equity`` input
    reads the same filing's current equity, so a negative one is that same overlay (ROIC, whose
    APP-07 cell is ADJ). The matrix makes no claim on the negative-equity column for any other
    metric, so flipping this on for them decides nothing.
    """
    own = OWN_NEGATIVE_EQUITY_FLAG.get(result.name)
    if own is not None:
        return bool(result.inputs.get(own))
    equity = result.inputs.get("equity")
    return equity is not None and equity < 0.0


def apply_applicability(
    results: list[tuple[str, MetricResult]], profile: CompanyProfile | None
) -> None:
    """Stamp the matrix's NA/ADJ cells onto *results* in place (P4).

    A ``None`` *profile* is the unknown CIK: every metric is kept and flagged
    ``company_type_unknown``. ``MetricResult`` is frozen, so the list entry is replaced, and each
    result gets its own copy of the inputs -- a group shares one inputs dict, as
    :func:`fundamental_agent.metrics.provenance.attach` also has to handle.
    """
    for index, (group, result) in enumerate(results):
        inputs = dict(result.inputs)
        if profile is None:
            inputs[f"flag_{result.name}_{UNKNOWN_TYPE_FLAG}"] = 1.0
            results[index] = (group, replace(result, inputs=inputs))
            continue
        code, rule = applicability.applicability(
            result.name, profile, negative_equity=metric_negative_equity(result)
        )
        if code == "A":
            results[index] = (group, replace(result, inputs=inputs))
            continue
        inputs[APPLICABILITY_RULE] = rule
        if code == "NA":
            inputs[f"flag_{result.name}_{NOT_APPLICABLE_FLAG}"] = 1.0
            results[index] = (group, replace(result, inputs=inputs, value=None))
        else:
            inputs[f"flag_{result.name}_{ADJUSTED_FLAG}"] = 1.0
            results[index] = (group, replace(result, inputs=inputs))


__all__ = ["apply_applicability", "metric_negative_equity"]
