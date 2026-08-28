"""The seed rule catalog."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from cycle.rules.base import Rule, RuleContext, VetoHit


@dataclass
class _ThresholdRule:
    RULE_ID: str
    SEVERITY: str
    DESCRIPTION: str
    metric: str
    op: str  # '>' or '<'
    threshold: float

    @property
    def PARAMS(self) -> dict[str, Any]:
        return {"metric": self.metric, "op": self.op, "threshold": self.threshold}

    def evaluate(self, ctx: RuleContext) -> list[VetoHit]:
        hits = []
        for aid, metrics in ctx.metrics.items():
            value = metrics.get(self.metric)
            if value is None:
                continue
            breached = value > self.threshold if self.op == ">" else value < self.threshold
            if breached:
                hits.append(
                    VetoHit(
                        aid,
                        self.RULE_ID,
                        self.SEVERITY,
                        {"metric": self.metric, "value": value, "threshold": self.threshold},
                    )
                )
        return hits


@dataclass
class _DrawdownRule:
    RULE_ID = "PRICE_CRASH"
    SEVERITY = "SOFT"
    DESCRIPTION = "90-day max drawdown worse than the threshold"
    threshold: float = -0.35

    @property
    def PARAMS(self) -> dict[str, Any]:
        return {"threshold": self.threshold}

    def evaluate(self, ctx: RuleContext) -> list[VetoHit]:
        hits = []
        for aid, obs in ctx.price_obs.items():
            dd = obs.get("max_drawdown_90d")
            if dd is not None and dd < self.threshold:
                hits.append(VetoHit(aid, self.RULE_ID, self.SEVERITY, {"max_drawdown_90d": dd}))
        return hits


@dataclass
class _StaleFundamentalRule:
    RULE_ID = "EARNINGS_MISSING"
    SEVERITY = "SOFT"
    DESCRIPTION = "no FUNDAMENTAL score within the lookback window"
    max_age_days: int = 400

    @property
    def PARAMS(self) -> dict[str, Any]:
        return {"max_age_days": self.max_age_days}

    def evaluate(self, ctx: RuleContext) -> list[VetoHit]:
        cutoff = date.fromisoformat(ctx.cycle_date) - timedelta(days=self.max_age_days)
        hits = []
        for aid, last in ctx.last_fundamental.items():
            if last is None or date.fromisoformat(last[:10]) < cutoff:
                hits.append(VetoHit(aid, self.RULE_ID, self.SEVERITY, {"last_fundamental": last}))
        return hits


RULES: list[Rule] = [
    _ThresholdRule(
        "LEVERAGE_EXTREME",
        "HARD",
        "debt/equity above the threshold",
        "leverage.debt_to_equity",
        ">",
        3.0,
    ),
    _ThresholdRule(
        "NEGATIVE_FCF",
        "HARD",
        "negative free-cash-flow margin",
        "cashflow.free_cash_flow_margin",
        "<",
        0.0,
    ),
    _ThresholdRule(
        "LIQUIDITY_DISTRESS",
        "SOFT",
        "current ratio below 1.0",
        "liquidity.current_ratio",
        "<",
        1.0,
    ),
    _DrawdownRule(),
    _StaleFundamentalRule(),
]
