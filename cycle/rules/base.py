"""Rule protocol + shared context for veto evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class VetoHit:
    asset_id: int
    rule_id: str
    severity: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuleContext:
    cycle_date: str
    # asset_id -> {"group.name": value}
    metrics: dict[int, dict[str, float | None]]
    # asset_id -> latest price_observation row
    price_obs: dict[int, dict[str, float | None]]
    # asset_id -> most recent FUNDAMENTAL event_time (ISO date) or None
    last_fundamental: dict[int, str | None]


@runtime_checkable
class Rule(Protocol):
    RULE_ID: str
    SEVERITY: str
    DESCRIPTION: str

    @property
    def PARAMS(self) -> dict[str, Any]:
        """Serializable rule parameters (matches the impls' attribute name)."""
        ...

    def evaluate(self, ctx: RuleContext) -> list[VetoHit]: ...
