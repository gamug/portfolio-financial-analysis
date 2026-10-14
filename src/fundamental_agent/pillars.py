"""The four pillars of the FUNDAMENTAL score (T-073, D1-D4).

The model rates each pillar; the code decides which pillars exist for a company, weighs them and
totals them (constitution, AI behavior #1: the deterministic part never sits with the model). This
module holds the pillar membership (D1), the status the code computes for every filing (D1), the
total (D2) and the block stored with the row (D4) -- one place, so `agents.py` and `cycle` read the
same definitions.

D1 -- the four statuses, computed by the code, never by the model:

* ``not_applicable`` -- every member metric is NA for the company type (the matrix through T-071
  part 1's profile, APP-00/01/02/02b/03/07). The pillar drops out by company type, which MET-00
  allows: "A structural absence is declared by company type (APP) or left empty."
* ``absent_by_form`` -- only for ``financial_strength``'s three liquidity ratios (APP-09), when
  ``AssetsCurrent`` and ``LiabilitiesCurrent`` are not filed: they drop out and the pillar rests on
  the leverage ratios. The form signal is APP-09's own wording. It is **not** MD-38's
  ``unclassified_balance_sheet`` flag, which is a consequence signal -- it also fires when a
  working-capital term is refused or only partially mapped (measured 2026-10-14: 304 classified
  filings; see ``docs/model_fixes.md`` T-073). The status is `absent_by_form` when the pillar still
  has a value on the remaining metrics, `empty` when it has none, `not_applicable` when they were
  its only applicable metrics.
* ``empty`` -- an applicable pillar with no value: every remaining metric is empty for another
  reason (missing input, capture failure, prior year missing, a base or denominator not positive).
* ``present`` -- at least one applicable metric has a value.

D2 -- the total. ``total = sum(weight * rating)`` over the pillars that enter the score, rounded to
one decimal. Every applicable pillar is present (D1's coverage rule: an applicable pillar that is
`empty` leaves the score empty, so it never reaches the total), so the sum runs over the pillars the
company has. The weights are **normalized over those pillars**: a `not_applicable` pillar drops out
by company type, and scoring it as a zero contribution would both break the scale D2 declares
("50 = the pillar of an average S&P 500 company") and impute the structurally absent factor as 0,
which MET-00 forbids. That is not the renormalization MET-00 rejects: there, an *empty* pillar
(missing data) would be imputed with the others' average, and D1 leaves the score empty instead.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from fundamental_agent.metrics.base import MetricResult

# T-073 (D4): the stored contract -- the four pillar names, the default weights, the block's keys
# and the reader `cycle` uses -- lives in `kg_schema` (the shared package, like the applicability
# matrix), so `cycle` recomputes a total without importing `fundamental_agent`. Re-exported here so
# this module stays the one place `fundamental_agent` names them.
from kg_schema.pillar_score import (
    COVERAGE_KEY,
    DEFAULT_PILLAR_WEIGHTS,
    PILLAR_NAMES,
    PILLAR_WEIGHTS_KEY,
    PILLAR_WEIGHTS_VERSION,
    PILLAR_WEIGHTS_VERSION_KEY,
    PILLARS_KEY,
)

# D1: the four pillars, from computed ratios only (TASKS.md's T-073 redesign). The building blocks
# (`ebitda`, `nopat`, `effective_tax_rate`, `return_on_invested_capital_effective_rate`) are inputs
# and derivations beside the ratios -- the model still sees them as context, but no pillar reads
# them. The valuation group is out of the score: value is already its own stored component
# (VALORIZATION), so scoring it here would count the same signal twice. "Moat" is dropped: no
# computed input supports it.
PILLARS: dict[str, tuple[str, ...]] = {
    "profitability": (
        "gross_margin",
        "operating_margin",
        "net_margin",
        "return_on_assets",
        "return_on_equity",
        "return_on_invested_capital",
        "asset_turnover",
        "inventory_turnover",
        "receivables_turnover",
    ),
    "financial_strength": (
        "debt_to_equity",
        "debt_to_assets",
        "interest_coverage",
        "net_debt_to_ebitda",
        "current_ratio",
        "quick_ratio",
        "cash_ratio",
    ),
    "cash_flow": (
        "operating_cash_flow_margin",
        "free_cash_flow_margin",
        "free_cash_flow_conversion",
        "capex_intensity",
    ),
    "growth": (
        "revenue_growth",
        "operating_income_growth",
        "net_income_growth",
        "free_cash_flow_growth",
        "revenue_cagr",
        "net_income_cagr",
        "operating_cash_flow_cagr",
    ),
}

# APP-09's three liquidity ratios: the only metrics an `absent_by_form` pillar drops, and only for
# `financial_strength`. Pinned to the matrix by `tests/test_pillars.py`.
LIQUIDITY_METRICS: tuple[str, ...] = ("current_ratio", "quick_ratio", "cash_ratio")

# metric name -> the pillar that carries it (D3: the fallback's nudges go to their metric's pillar;
# the valuation nudge has no pillar and so leaves the score).
PILLAR_OF_METRIC: dict[str, str] = {
    metric: pillar for pillar, metrics in PILLARS.items() for metric in metrics
}

# The pillar that carries APP-09's form rule.
FINANCIAL_STRENGTH = "financial_strength"

NOT_APPLICABLE = "not_applicable"
ABSENT_BY_FORM = "absent_by_form"
EMPTY = "empty"
PRESENT = "present"

STATUSES: tuple[str, ...] = (PRESENT, EMPTY, ABSENT_BY_FORM, NOT_APPLICABLE)

# The default empty reason when a metric records none (G4's step 5).
_DEFAULT_EMPTY_REASON = "missing_input"


@dataclass(frozen=True)
class PillarStatus:
    """One pillar's D1 status for one filing, and the metrics that decided it."""

    pillar: str
    status: str
    # The metrics that entered the status: the applicable ones, less APP-09's three when they drop
    # out by form. Empty for `not_applicable`.
    metrics: tuple[str, ...] = ()
    # The distinct empty reasons of those metrics, sorted and comma-joined (`empty` only).
    empty_reason: str | None = None

    @property
    def has_a_reading(self) -> bool:
        """Whether the pillar enters the score: `present`, or `absent_by_form` with a value."""
        return self.status in (PRESENT, ABSENT_BY_FORM)

    def as_dict(self) -> dict[str, Any]:
        block: dict[str, Any] = {"status": self.status, "metrics": list(self.metrics)}
        if self.empty_reason is not None:
            block["empty_reason"] = self.empty_reason
        return block


def _metric_index(results: Iterable[tuple[str, MetricResult]]) -> dict[str, MetricResult]:
    return {result.name: result for _group, result in results}


def _not_applicable(result: MetricResult | None) -> bool:
    """A metric with no result at all is not NA: the group did not compute it for this filing
    (`cagr` yields nothing on a 10-Q), which is an absent reading, not a company-type rule."""
    return result is not None and bool(result.inputs.get(f"flag_{result.name}_{NOT_APPLICABLE}"))


def _has_value(result: MetricResult | None) -> bool:
    return result is not None and result.value is not None


def _empty_reason_of(result: MetricResult | None) -> str:
    if result is None:
        return _DEFAULT_EMPTY_REASON
    block = result.inputs.get("provenance")
    if isinstance(block, Mapping):
        reason = block.get("empty_reason")
        if isinstance(reason, str) and reason:
            return reason
    return _DEFAULT_EMPTY_REASON


def balance_sheet_unclassified(results: Iterable[tuple[str, MetricResult]]) -> bool:
    """APP-09's form signal: neither ``AssetsCurrent`` nor ``LiabilitiesCurrent`` is filed.

    Read from the liquidity group's own audit inputs -- the metrics already resolved those two
    concepts, so the code never resolves them a second time. A filing whose liquidity group is
    absent is treated as classified (no metric drops out).
    """
    for _group, result in results:
        if result.name == "current_ratio":
            return (
                "current_assets" not in result.inputs and "current_liabilities" not in result.inputs
            )
    return False


def pillar_statuses(results: Iterable[tuple[str, MetricResult]]) -> dict[str, PillarStatus]:
    """D1's status for each of the four pillars, from the computed results alone."""
    pairs = list(results)
    index = _metric_index(pairs)
    unclassified = balance_sheet_unclassified(pairs)
    out: dict[str, PillarStatus] = {}
    for pillar, metrics in PILLARS.items():
        applicable = tuple(name for name in metrics if not _not_applicable(index.get(name)))
        remaining = (
            tuple(name for name in applicable if name not in LIQUIDITY_METRICS)
            if pillar == FINANCIAL_STRENGTH and unclassified
            else applicable
        )
        if not remaining:
            # Every member metric is NA, or APP-09's three were the pillar's only applicable ones.
            out[pillar] = PillarStatus(pillar, NOT_APPLICABLE)
            continue
        if not any(_has_value(index.get(name)) for name in remaining):
            reasons = sorted({_empty_reason_of(index.get(name)) for name in remaining})
            out[pillar] = PillarStatus(pillar, EMPTY, remaining, ",".join(reasons))
            continue
        dropped = len(applicable) != len(remaining)
        out[pillar] = PillarStatus(pillar, ABSENT_BY_FORM if dropped else PRESENT, remaining)
    return out


def scored_pillars(statuses: Mapping[str, PillarStatus]) -> tuple[str, ...]:
    """The pillars that enter the score, in the declared order: those with a reading."""
    return tuple(pillar for pillar in PILLAR_NAMES if statuses[pillar].has_a_reading)


def empty_pillars(statuses: Mapping[str, PillarStatus]) -> tuple[str, ...]:
    """The applicable pillars with no value -- what leaves the score empty (D1's coverage rule)."""
    return tuple(pillar for pillar in PILLAR_NAMES if statuses[pillar].status == EMPTY)


def coverage(statuses: Mapping[str, PillarStatus]) -> dict[str, Any]:
    """D4's `coverage`: which pillars entered the score, and why the others did not."""
    return {
        "pillars": len(PILLARS),
        "scored": list(scored_pillars(statuses)),
        "empty": list(empty_pillars(statuses)),
        "absent_by_form": [
            pillar for pillar in PILLAR_NAMES if statuses[pillar].status == ABSENT_BY_FORM
        ],
        "not_applicable": [
            pillar for pillar in PILLAR_NAMES if statuses[pillar].status == NOT_APPLICABLE
        ],
    }


def normalized_weights(
    statuses: Mapping[str, PillarStatus], weights: Mapping[str, float]
) -> dict[str, float]:
    """*weights* over the pillars that enter the score, normalized to sum to 1.

    A pillar the company does not have (`not_applicable`) is not a zero contribution: the total
    stays on D2's declared scale, where 50 is an average company's pillar rating.
    """
    scored = scored_pillars(statuses)
    total = sum(float(weights[pillar]) for pillar in scored)
    if total <= 0:
        return {pillar: 0.0 for pillar in scored}
    return {pillar: float(weights[pillar]) / total for pillar in scored}


def total_from_ratings(
    ratings: Mapping[str, float],
    statuses: Mapping[str, PillarStatus],
    weights: Mapping[str, float] | None = None,
) -> float:
    """D2's total: the weighted sum of the pillar ratings, one decimal place."""
    applied = normalized_weights(statuses, weights or DEFAULT_PILLAR_WEIGHTS)
    return round(sum(applied[pillar] * float(ratings[pillar]) for pillar in applied), 1)


def score_block(
    statuses: Mapping[str, PillarStatus],
    ratings: Mapping[str, float] | None = None,
    reasons: Mapping[str, str] | None = None,
    weights: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """D4's block, stored inside ``score_snapshot.inputs_json``.

    ``pillars`` holds each pillar's status, the metrics that decided it, and -- for a pillar that
    entered the score -- the model's integer rating and its one-sentence reason. An `empty` pillar
    carries its metrics' empty reasons instead; a `not_applicable` one carries neither.
    """
    applied = normalized_weights(statuses, weights or DEFAULT_PILLAR_WEIGHTS)
    block: dict[str, Any] = {}
    for pillar in PILLAR_NAMES:
        entry = statuses[pillar].as_dict()
        if pillar in applied and ratings is not None:
            entry["rating"] = ratings[pillar]
            if reasons is not None and reasons.get(pillar):
                entry["reason"] = reasons[pillar]
            entry["weight"] = round(applied[pillar], 6)
        block[pillar] = entry
    return {
        PILLARS_KEY: block,
        PILLAR_WEIGHTS_KEY: dict(weights or DEFAULT_PILLAR_WEIGHTS),
        PILLAR_WEIGHTS_VERSION_KEY: PILLAR_WEIGHTS_VERSION,
        COVERAGE_KEY: coverage(statuses),
    }
