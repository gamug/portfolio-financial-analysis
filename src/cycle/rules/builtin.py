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
class _LeverageRule:
    """LEVERAGE_EXTREME, with a negative-book-equity guard (C2,
    docs/model_fixes.md): a plain ``debt_to_equity > threshold`` check lets a
    negative-equity firm's *negative* ratio (large buyback-driven negative
    equity -- MCD, SBUX, PM, ...) trivially evade the veto, even though it's
    actually maximally leveraged. ``leverage.py::_total_debt`` sums only
    non-negative balance-sheet items, so a negative ratio here is itself
    reliable evidence of non-positive book equity -- no separate ``equity``
    metric is needed. When that happens, gate on ``debt_to_assets``/
    ``interest_coverage`` instead; thresholds reuse PLAN.md's Ring-1
    ``DQ_NEG_EQUITY`` calibration (342 filings verified) rather than
    inventing new, unverified numbers.
    """

    RULE_ID = "LEVERAGE_EXTREME"
    SEVERITY = "HARD"
    DESCRIPTION = "debt/equity above the threshold, or negative book equity with a high debt burden"
    debt_to_equity_threshold: float = 3.0
    neg_equity_debt_to_assets_threshold: float = 0.8
    neg_equity_interest_coverage_threshold: float = 1.5

    @property
    def PARAMS(self) -> dict[str, Any]:
        return {
            "metric": "leverage.debt_to_equity",
            "op": ">",
            "threshold": self.debt_to_equity_threshold,
            "neg_equity_debt_to_assets_threshold": self.neg_equity_debt_to_assets_threshold,
            "neg_equity_interest_coverage_threshold": self.neg_equity_interest_coverage_threshold,
        }

    def evaluate(self, ctx: RuleContext) -> list[VetoHit]:
        hits = []
        for aid, metrics in ctx.metrics.items():
            dte = metrics.get("leverage.debt_to_equity")
            if dte is None:
                continue
            if dte < 0:
                # Non-negative debt means a negative ratio implies non-positive
                # book equity -- the plain threshold comparison below is
                # meaningless here, so gate on debt_to_assets/interest_coverage
                # instead (accepted limitation: no hit if both are missing).
                dta = metrics.get("leverage.debt_to_assets")
                cov = metrics.get("leverage.interest_coverage")
                breached = (dta is not None and dta > self.neg_equity_debt_to_assets_threshold) or (
                    cov is not None and cov < self.neg_equity_interest_coverage_threshold
                )
                if breached:
                    hits.append(
                        VetoHit(
                            aid,
                            self.RULE_ID,
                            self.SEVERITY,
                            {
                                "metric": "leverage.debt_to_equity",
                                "value": dte,
                                "debt_to_assets": dta,
                                "interest_coverage": cov,
                            },
                        )
                    )
                continue
            if dte > self.debt_to_equity_threshold:
                hits.append(
                    VetoHit(
                        aid,
                        self.RULE_ID,
                        self.SEVERITY,
                        {
                            "metric": "leverage.debt_to_equity",
                            "value": dte,
                            "threshold": self.debt_to_equity_threshold,
                        },
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


@dataclass
class _DataQualityRule:
    """A HARD Ring-1 ``DQ_*`` gate hit on the asset's latest filing (T-065). The gates run
    in ``fundamental_agent`` and are read from ``data_quality_issue``; this rule only turns
    their HARD verdicts into a veto, so the evidence names the gates that fired."""

    RULE_ID = "DATA_QUALITY"
    SEVERITY = "HARD"
    DESCRIPTION = "a HARD Ring-1 data-quality gate (DQ_*) fired on the latest filing"

    @property
    def PARAMS(self) -> dict[str, Any]:
        return {"source": "data_quality_issue", "severity": "HARD"}

    def evaluate(self, ctx: RuleContext) -> list[VetoHit]:
        return [
            VetoHit(
                aid,
                self.RULE_ID,
                self.SEVERITY,
                {"gates": sorted({i["rule_id"] for i in issues}), "issues": issues},
            )
            for aid, issues in sorted(ctx.data_quality.items())
            if issues and aid in ctx.metrics
        ]


RULES: list[Rule] = [
    _LeverageRule(),
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
    _DataQualityRule(),
]
