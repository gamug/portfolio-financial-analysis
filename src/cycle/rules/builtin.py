"""The seed rule catalog."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from cycle.rules.base import Rule, RuleContext, RuleResult, VetoHit


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

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        hits = []
        evaluated = set()
        for aid, metrics in ctx.metrics.items():
            value = metrics.get(self.metric)
            if value is None:
                continue
            evaluated.add(aid)
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
        return RuleResult(hits, frozenset(evaluated))


@dataclass
class _LeverageRule:
    """LEVERAGE_EXTREME, with a negative-book-equity guard (C2,
    docs/model_fixes.md; recalibrated T-116): a plain ``debt_to_equity >
    threshold`` check lets a negative-equity firm's *negative* ratio (large
    buyback-driven negative equity -- MCD, SBUX, PM, ...) trivially evade the
    veto, even though it's actually maximally leveraged.
    ``leverage.py::_total_debt`` sums only non-negative balance-sheet items,
    so a negative ratio here is itself reliable evidence of non-positive
    book equity -- no separate ``equity`` metric is needed. When that
    happens, gate on the standard credit pair, ``net_debt_to_ebitda``/
    ``interest_coverage``, instead: ``debt_to_assets`` (C2's original guard)
    is a balance-sheet solvency ratio, not a leverage-capacity one, and on
    real production data it stays inert for a buyback-heavy but genuinely
    investment-grade name (MCD: 0.665-0.72 across 22 filings, never near the
    0.8 guard) while telling you nothing about debt-service capacity.
    ``net_debt_to_ebitda`` -- debt relative to cash-flow-generating capacity
    -- is what corporate credit analysis actually keys leverage bands on
    (S&P Global Ratings, "Corporate Methodology," 2013: >5.0x is the
    "highly leveraged" band); paired with ``interest_coverage`` (debt-
    service capacity), thresholds reuse PLAN.md's Ring-1 ``DQ_NEG_EQUITY``
    calibration, recomputed under T-105's now-annualized
    ``net_debt_to_ebitda`` (docs/model_fixes.md, T-116) rather than
    inventing new, unverified numbers. A negative-equity asset with neither
    metric available is routed to SOFT review, not silently passed. A
    *negative* ``net_debt_to_ebitda`` is itself ambiguous -- it can mean a
    genuine net-cash position (healthy) or negative EBITDA with positive net
    debt (the most distressed profile of all), and this rule sees only the
    already-divided ratio, never the raw EBITDA/net-debt it came from -- so
    it is treated as unresolved (PR #95 review), falling back to
    ``interest_coverage`` alone, the same as a missing value.
    """

    RULE_ID = "LEVERAGE_EXTREME"
    SEVERITY = "HARD"
    DESCRIPTION = "debt/equity above the threshold, or negative book equity with a high debt burden"
    debt_to_equity_threshold: float = 3.0
    neg_equity_net_debt_to_ebitda_threshold: float = 5.0
    neg_equity_interest_coverage_threshold: float = 1.5

    @property
    def PARAMS(self) -> dict[str, Any]:
        return {
            "metric": "leverage.debt_to_equity",
            "op": ">",
            "threshold": self.debt_to_equity_threshold,
            "neg_equity_net_debt_to_ebitda_threshold": self.neg_equity_net_debt_to_ebitda_threshold,
            "neg_equity_interest_coverage_threshold": self.neg_equity_interest_coverage_threshold,
        }

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        hits = []
        evaluated = set()
        for aid, metrics in ctx.metrics.items():
            dte = metrics.get("leverage.debt_to_equity")
            if dte is None:
                continue
            evaluated.add(aid)
            if dte < 0:
                # Non-negative debt means a negative ratio implies non-positive
                # book equity -- the plain threshold comparison below is
                # meaningless here, so gate on the standard credit pair
                # instead. Neither metric available: can't tell, route to
                # SOFT review rather than pass silently (T-116).
                ndte_raw = metrics.get("leverage.net_debt_to_ebitda")
                cov = metrics.get("leverage.interest_coverage")
                # T-116 (PR #95 review): net_debt_to_ebitda goes *negative* when EBITDA itself
                # is negative, even with substantial positive net debt -- the most distressed
                # profile, not a healthy one. Unlike fundamental_agent's DQ_NEG_EQUITY, this
                # rule only ever sees the already-divided ratio (never the raw EBITDA/net-debt
                # it was built from), so it cannot tell that case apart from a genuine net-cash
                # position (also negative). A negative ratio is therefore unresolved here, not
                # evidence of health -- it falls back to interest_coverage alone, same as if
                # net_debt_to_ebitda were missing.
                ndte = ndte_raw if ndte_raw is not None and ndte_raw >= 0 else None
                if ndte is None and cov is None:
                    hits.append(
                        VetoHit(
                            aid,
                            self.RULE_ID,
                            "SOFT",
                            {
                                "metric": "leverage.debt_to_equity",
                                "value": dte,
                                "net_debt_to_ebitda": ndte_raw,
                                "interest_coverage": None,
                                "reason": "negative book equity, no corroborating leverage metric",
                            },
                        )
                    )
                    continue
                breached = (
                    ndte is not None and ndte > self.neg_equity_net_debt_to_ebitda_threshold
                ) or (cov is not None and cov < self.neg_equity_interest_coverage_threshold)
                if breached:
                    hits.append(
                        VetoHit(
                            aid,
                            self.RULE_ID,
                            self.SEVERITY,
                            {
                                "metric": "leverage.debt_to_equity",
                                "value": dte,
                                "net_debt_to_ebitda": ndte_raw,
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
        return RuleResult(hits, frozenset(evaluated))


@dataclass
class _DrawdownRule:
    RULE_ID = "PRICE_CRASH"
    SEVERITY = "SOFT"
    DESCRIPTION = "90-day max drawdown worse than the threshold"
    threshold: float = -0.35

    @property
    def PARAMS(self) -> dict[str, Any]:
        return {"threshold": self.threshold}

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        hits = []
        evaluated = set()
        for aid, obs in ctx.price_obs.items():
            dd = obs.get("max_drawdown_90d")
            if dd is None:
                continue
            evaluated.add(aid)
            if dd < self.threshold:
                hits.append(VetoHit(aid, self.RULE_ID, self.SEVERITY, {"max_drawdown_90d": dd}))
        return RuleResult(hits, frozenset(evaluated))


@dataclass
class _StaleFundamentalRule:
    """A SOFT penalty for a FUNDAMENTAL score that *exists* but has aged out. ``ctx.
    last_fundamental`` only ever has an asset as a key when it has a score at all (T-119:
    ``data.last_fundamental_dates``'s own docstring) -- an asset with *no* score, ever, never
    reaches this rule's ``for`` loop at all, and deliberately so: that case is ineligible, not
    penalized (PR #78 review), handled directly in ``orchestrator._rank`` instead of a SOFT
    veto here."""

    RULE_ID = "EARNINGS_MISSING"
    SEVERITY = "SOFT"
    DESCRIPTION = "a FUNDAMENTAL score exists but has aged past the lookback window"
    max_age_days: int = 400

    @property
    def PARAMS(self) -> dict[str, Any]:
        return {"max_age_days": self.max_age_days}

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        cutoff = date.fromisoformat(ctx.cycle_date) - timedelta(days=self.max_age_days)
        hits = []
        for aid, last in ctx.last_fundamental.items():
            if last is None or date.fromisoformat(last[:10]) < cutoff:
                hits.append(VetoHit(aid, self.RULE_ID, self.SEVERITY, {"last_fundamental": last}))
        # Every key here already has a FUNDAMENTAL score (T-119) -- the rule can always
        # render a verdict (aged out, or not) once an asset appears at all.
        return RuleResult(hits, frozenset(ctx.last_fundamental))


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

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        hits = [
            VetoHit(
                aid,
                self.RULE_ID,
                self.SEVERITY,
                {"gates": sorted({i["rule_id"] for i in issues}), "issues": issues},
            )
            for aid, issues in sorted(ctx.data_quality.items())
            if issues and aid in ctx.metrics
        ]
        # ctx.data_quality only carries assets with a HARD issue (the orchestrator seeds it
        # from dq.hard alone) -- it cannot itself say "Ring-1 looked and found nothing". Ring-1
        # runs against the same usable filing latest_metrics reads (T-106/T-107), so a non-empty
        # ctx.metrics entry is what "this asset's latest filing was gated this cycle" means here.
        evaluated = {aid for aid, m in ctx.metrics.items() if m}
        return RuleResult(hits, frozenset(evaluated))


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
