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
