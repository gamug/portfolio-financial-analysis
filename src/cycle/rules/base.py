"""Rule protocol + shared context for veto evaluation."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class VetoHit:
    asset_id: int
    rule_id: str
    severity: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleResult:
    """A rule's verdict for one cycle: its hits, plus the asset_ids it could actually
    resolve (T-125 b) -- hit or not. ``write_vetoes`` needs the *evaluated* set to tell
    "condition no longer holds" (closes an open stint) apart from "couldn't tell this
    cycle" (missing data -- an open stint must stay open, never silently clear). Iterating
    a ``RuleResult`` directly yields its hits, so existing ``for h in rule.evaluate(ctx)``
    call sites keep working unchanged."""

    hits: list[VetoHit] = field(default_factory=list)
    evaluated: frozenset[int] = frozenset()

    def __iter__(self) -> Iterator[VetoHit]:
        return iter(self.hits)


@dataclass
class RuleContext:
    cycle_date: str
    # asset_id -> {"group.name": value}
    metrics: dict[int, dict[str, float | None]]
    # asset_id -> latest price_observation row
    price_obs: dict[int, dict[str, float | None]]
    # asset_id -> most recent FUNDAMENTAL event_time (ISO date) or None
    last_fundamental: dict[int, str | None]
    # asset_id -> HARD Ring-1 data-quality issues on its latest filing (T-065)
    data_quality: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    # asset_id -> its GICS sector name (today's sector, L-03). For a rule that does not apply to a
    # kind of company; T-071's company profile replaces it.
    sectors: dict[int, str | None] = field(default_factory=dict)


@runtime_checkable
class Rule(Protocol):
    RULE_ID: str
    SEVERITY: str
    DESCRIPTION: str

    @property
    def PARAMS(self) -> dict[str, Any]:
        """Serializable rule parameters (matches the impls' attribute name)."""
        ...

    def evaluate(self, ctx: RuleContext) -> RuleResult: ...


def penalty_points(rule: object, default: float) -> float | None:
    """The points a SOFT rule's active stint takes off the blended score (T-070): its own
    ``PENALTY_POINTS`` when it declares one (``0.0`` = flag only), else *default* (the run's
    ``soft_veto_penalty``). ``None`` for a HARD rule, which excludes instead of penalizing."""
    if getattr(rule, "SEVERITY", None) != "SOFT":
        return None
    declared = getattr(rule, "PENALTY_POINTS", None)
    return float(declared) if declared is not None else default


def hold_trading_days(rule: object) -> int | None:
    """A temporal rule's hold, in NYSE trading days (T-070): its stint cannot clear before
    ``raised_on`` plus this many sessions. ``None`` on every other rule."""
    days = getattr(rule, "HOLD_TRADING_DAYS", None)
    return int(days) if days is not None else None
