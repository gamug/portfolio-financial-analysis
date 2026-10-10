"""The seed rule catalog."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from cycle.rules.base import Rule, RuleContext, RuleResult, VetoHit
from cycle.scores.technical import MIN_SECTOR_NAMES

# The price vetoes (T-070) read the latest price_observation as of the cycle date. An observation
# older than this many calendar days (a long weekend is 4) is not a reading of today: a halted or
# delisted name would otherwise be re-confirmed forever by a stale row, so it is "could not tell".
MAX_OBSERVATION_AGE_DAYS = 7

# GICS sectors the LIQUIDITY_DISTRESS test does not apply to (checklist APP-09): a financial has no
# classified balance sheet (no current/non-current split), and a regulated utility's is shaped by
# its rate base and its commercial-paper funding. T-071's company profile (type + overlays)
# replaces this sector test; until then GICS stands in for it.
LIQUIDITY_EXEMPT_SECTORS = frozenset({"Financials", "Utilities"})


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


def _fresh(ctx: RuleContext, obs: dict[str, float | None]) -> bool:
    """Is the price observation *obs* (a ``price_observation`` row) recent enough to evaluate?"""
    obs_date = obs.get("obs_date")
    if not isinstance(obs_date, str):
        return True  # a hand-built row with no date: nothing to age
    age = date.fromisoformat(ctx.cycle_date) - date.fromisoformat(obs_date[:10])
    return age.days <= MAX_OBSERVATION_AGE_DAYS


@dataclass
class _LiquidityRule:
    """LIQUIDITY_DISTRESS, recalibrated (T-070, audit N7): ``current_ratio < 1.0`` alone flagged
    structurally healthy PG, NEE, PM, T, STZ, APA and SBAC (7 of the 20-asset pilot) -- a business
    with negative working capital by design (customer deposits, commercial paper rolled, receipts
    before payables) sits below 1.0 for years. SOFT only when the current ratio is below 1.0 AND a
    cash-coverage test also fails, using the metrics stored today (no new metric, T-151's scope):

    - ``leverage.interest_coverage < interest_coverage_floor`` (EBIT does not cover the interest);
    - ``cashflow.operating_cash_flow_margin < ocf_margin_floor`` (operations burn cash).

    Either failing is a failed test. A missing metric is not a failed test; when *neither* is
    available the asset is not evaluated (an open stint stays open: "could not tell"). Not applied
    to GICS Financials or Utilities (``LIQUIDITY_EXEMPT_SECTORS``): they are evaluated and never
    hit, so a stint opened before the exemption closes."""

    RULE_ID = "LIQUIDITY_DISTRESS"
    SEVERITY = "SOFT"
    DESCRIPTION = (
        "current ratio below 1.0 and weak cash coverage (interest coverage or operating cash "
        "flow margin); not applied to Financials or Utilities"
    )
    current_ratio_threshold: float = 1.0
    interest_coverage_floor: float = 1.5
    ocf_margin_floor: float = 0.0

    @property
    def PARAMS(self) -> dict[str, Any]:
        return {
            "metric": "liquidity.current_ratio",
            "op": "<",
            "threshold": self.current_ratio_threshold,
            "interest_coverage_floor": self.interest_coverage_floor,
            "ocf_margin_floor": self.ocf_margin_floor,
            "exempt_sectors": sorted(LIQUIDITY_EXEMPT_SECTORS),
        }

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        hits = []
        evaluated = set()
        for aid, metrics in ctx.metrics.items():
            if ctx.sectors.get(aid) in LIQUIDITY_EXEMPT_SECTORS:
                if metrics:
                    evaluated.add(aid)  # never a hit: closes a stint opened before the exemption
                continue
            ratio = metrics.get("liquidity.current_ratio")
            if ratio is None:
                continue
            if ratio >= self.current_ratio_threshold:
                evaluated.add(aid)
                continue
            coverage = metrics.get("leverage.interest_coverage")
            ocf_margin = metrics.get("cashflow.operating_cash_flow_margin")
            if coverage is None and ocf_margin is None:
                continue  # below 1.0 and nothing to corroborate it with: could not tell
            evaluated.add(aid)
            weak_coverage = coverage is not None and coverage < self.interest_coverage_floor
            burning_cash = ocf_margin is not None and ocf_margin < self.ocf_margin_floor
            if weak_coverage or burning_cash:
                hits.append(
                    VetoHit(
                        aid,
                        self.RULE_ID,
                        self.SEVERITY,
                        {
                            "current_ratio": ratio,
                            "interest_coverage": coverage,
                            "operating_cash_flow_margin": ocf_margin,
                            "failed": [
                                name
                                for name, failed in (
                                    ("interest_coverage", weak_coverage),
                                    ("operating_cash_flow_margin", burning_cash),
                                )
                                if failed
                            ],
                        },
                    )
                )
        return RuleResult(hits, frozenset(evaluated))


@dataclass
class _TrendBreakRule:
    """BREAK_TREND_200 (SOFT, T-070): the close is more than 5% under its 200-day simple moving
    average. SOFT, not HARD: a HARD veto here would purge half the index in any broad pullback."""

    RULE_ID = "BREAK_TREND_200"
    SEVERITY = "SOFT"
    DESCRIPTION = "close below 95% of the 200-day simple moving average"
    ratio: float = 0.95

    @property
    def PARAMS(self) -> dict[str, Any]:
        return {"metric": "close / sma_200", "op": "<", "threshold": self.ratio}

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        hits = []
        evaluated = set()
        for aid, obs in ctx.price_obs.items():
            close, sma = obs.get("close"), obs.get("sma_200")
            if close is None or not sma or not _fresh(ctx, obs):
                continue
            evaluated.add(aid)
            if close < self.ratio * sma:
                hits.append(
                    VetoHit(
                        aid,
                        self.RULE_ID,
                        self.SEVERITY,
                        {"close": close, "sma_200": sma, "ratio": close / sma},
                    )
                )
        return RuleResult(hits, frozenset(evaluated))


def _sector_medians(
    values: dict[int, float], sectors: dict[int, str | None]
) -> dict[int, tuple[float, str | None, int]]:
    """``asset_id -> (median, group, group size)`` of *values* within each asset's GICS sector
    among the assets that have a value (the same ones the rule evaluates). A sector with fewer
    than ``MIN_SECTOR_NAMES`` such assets -- or an asset with no sector -- takes the median of
    the whole cross-section instead (group ``None``): the fallback TECHNICAL's sector-Z uses."""
    by_sector: dict[str, list[float]] = {}
    for aid, v in values.items():
        sector = sectors.get(aid)
        if sector is not None:
            by_sector.setdefault(sector, []).append(v)
    whole = statistics.median(values.values()) if values else 0.0
    out: dict[int, tuple[float, str | None, int]] = {}
    for aid in values:
        sector = sectors.get(aid)
        peers = by_sector.get(sector, []) if sector is not None else []
        if len(peers) >= MIN_SECTOR_NAMES:
            out[aid] = (statistics.median(peers), sector, len(peers))
        else:
            out[aid] = (whole, None, len(values))
    return out


@dataclass
class _VolatilityShockRule:
    """VOLATILITY_SHOCK (HARD-temporal, T-070): the name's 5-day volatility is more than 2.5 times
    its own 60-day baseline's AND more than 2.5 times what its GICS sector's names show that day.
    ``ratio = vol_5d / vol_60d_base`` (the ``sd`` of the daily log returns; the baseline ends 5
    sessions before the cycle date, so the shock does not dilute its own baseline). The rule
    fires when both hold:

    - absolute: ``ratio > 2.5``;
    - relative: ``ratio > 2.5 * median(ratio over the sector's names)``, the sector's names being
      those the rule evaluates that day; a sector with fewer than 5 of them (or an asset with no
      sector) uses the median over the whole cross-section.

    The stint is held for ``HOLD_TRADING_DAYS`` sessions. The relative condition is the
    declared design choice that keeps a broad sell-off from HARD-vetoing most of the index (the
    absolute rule alone held up to 80% of the 503 names on 2025-04-10): a move a whole sector
    makes together no longer raises a HARD veto -- it is a systematic move, carried by the
    TECHNICAL score and the risk model, not idiosyncratic distress."""

    RULE_ID = "VOLATILITY_SHOCK"
    SEVERITY = "HARD"
    HOLD_TRADING_DAYS = 10
    DESCRIPTION = (
        "5-day volatility above 2.5x the 60-day baseline and above 2.5x the sector's median "
        "ratio; held 10 trading days"
    )
    ratio: float = 2.5
    relative_ratio: float = 2.5

    @property
    def PARAMS(self) -> dict[str, Any]:
        return {
            "metric": "vol_5d / vol_60d_base",
            "op": ">",
            "threshold": self.ratio,
            "relative": "ratio > relative_threshold * median(ratio of the GICS sector's names)",
            "relative_threshold": self.relative_ratio,
            "min_sector_names": MIN_SECTOR_NAMES,
            "sector_fallback": "whole cross-section",
            "hold_trading_days": self.HOLD_TRADING_DAYS,
        }

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        ratios = {
            aid: short / base
            for aid, obs in ctx.price_obs.items()
            if (short := obs.get("vol_5d")) is not None
            and (base := obs.get("vol_60d_base"))
            and _fresh(ctx, obs)
        }
        baseline = _sector_medians(ratios, ctx.sectors)
        hits = []
        for aid, ratio in ratios.items():
            median, group, names = baseline[aid]
            if ratio > self.ratio and ratio > self.relative_ratio * median:
                hits.append(
                    VetoHit(
                        aid,
                        self.RULE_ID,
                        self.SEVERITY,
                        {
                            "ratio": ratio,
                            "vol_5d": ctx.price_obs[aid]["vol_5d"],
                            "vol_60d_base": ctx.price_obs[aid]["vol_60d_base"],
                            "group_median_ratio": median,
                            "group": group or "cross-section",
                            "group_names": names,
                        },
                    )
                )
        return RuleResult(hits, frozenset(ratios))


@dataclass
class _CrashZRule:
    """CRASH_Z_SCORE (HARD-temporal, T-070): the name's 5-day log return is more than 2.5
    baseline standard deviations below what its baseline predicts for 5 days AND more than 2.5
    below its GICS sector's median 5-day return, in the same units. PLAN.md writes the absolute
    part as ``(R5d - mu60d) / sigma60d``; the units are made consistent here -- a 5-day return is
    compared with the daily baseline scaled to 5 days (mean x 5, sd x sqrt(5), independent daily
    returns). The rule fires when both hold:

    - absolute: ``z = (ret_5d - 5 * mu_60d_base) / (vol_60d_base * sqrt(5)) < -2.5``;
    - relative: ``(ret_5d - median(ret_5d of the GICS sector's names)) / (vol_60d_base *
      sqrt(5)) < -2.5`` -- the name's own volatility scales the gap to its sector; the sector's
      names being those the rule evaluates that day. A sector with fewer than 5 of them (or an
      asset with no sector) uses the whole cross-section's median.

    The stint is held for ``HOLD_TRADING_DAYS`` sessions. See :class:`_VolatilityShockRule` for
    why the relative condition exists and what it gives up."""

    RULE_ID = "CRASH_Z_SCORE"
    SEVERITY = "HARD"
    HOLD_TRADING_DAYS = 10
    DESCRIPTION = (
        "5-day return more than 2.5 baseline standard deviations below normal and below the "
        "sector's median 5-day return; held 10 trading days"
    )
    threshold: float = -2.5
    window: int = 5

    @property
    def PARAMS(self) -> dict[str, Any]:
        return {
            "metric": "(ret_5d - 5 * mu_60d_base) / (vol_60d_base * sqrt(5))",
            "op": "<",
            "threshold": self.threshold,
            "relative": (
                "(ret_5d - median(ret_5d of the GICS sector's names)) / (vol_60d_base * sqrt(5)) "
                "< relative_threshold"
            ),
            "relative_threshold": self.threshold,
            "min_sector_names": MIN_SECTOR_NAMES,
            "sector_fallback": "whole cross-section",
            "hold_trading_days": self.HOLD_TRADING_DAYS,
        }

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        returns = {
            aid: ret
            for aid, obs in ctx.price_obs.items()
            if (ret := obs.get("ret_5d")) is not None
            and obs.get("mu_60d_base") is not None
            and obs.get("vol_60d_base")
            and _fresh(ctx, obs)
        }
        baseline = _sector_medians(returns, ctx.sectors)
        scale = math.sqrt(self.window)
        hits = []
        for aid, ret in returns.items():
            obs = ctx.price_obs[aid]
            mu, sd = obs["mu_60d_base"], obs["vol_60d_base"]
            assert mu is not None and sd is not None  # filtered above
            median, group, names = baseline[aid]
            z = (ret - self.window * mu) / (sd * scale)
            relative_z = (ret - median) / (sd * scale)
            if z < self.threshold and relative_z < self.threshold:
                hits.append(
                    VetoHit(
                        aid,
                        self.RULE_ID,
                        self.SEVERITY,
                        {
                            "z": z,
                            "relative_z": relative_z,
                            "ret_5d": ret,
                            "mu_60d_base": mu,
                            "vol_60d_base": sd,
                            "group_median_ret_5d": median,
                            "group": group or "cross-section",
                            "group_names": names,
                        },
                    )
                )
        return RuleResult(hits, frozenset(returns))


RULES: list[Rule] = [
    _LeverageRule(),
    _ThresholdRule(
        "NEGATIVE_FCF",
        "HARD",
        "negative trailing-twelve-month free-cash-flow margin",
        "cashflow.free_cash_flow_margin",
        "<",
        0.0,
    ),
    _LiquidityRule(),
    _DrawdownRule(),
    _StaleFundamentalRule(),
    _DataQualityRule(),
    _TrendBreakRule(),
    _VolatilityShockRule(),
    _CrashZRule(),
]
