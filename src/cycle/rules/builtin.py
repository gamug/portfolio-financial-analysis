"""The seed rule catalog."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from cycle.rules.base import Rule, RuleContext, RuleResult, VetoHit
from cycle.scores.technical import MIN_SECTOR_NAMES
from kg_schema.versions import UTILITY_COVERAGE_THRESHOLD

# The price vetoes (T-070) read the latest price_observation as of the cycle date. An observation
# older than this many calendar days (a long weekend is 4) is not a reading of today: a halted or
# delisted name would otherwise be re-confirmed forever by a stale row, so it is "could not tell".
MAX_OBSERVATION_AGE_DAYS = 7

# LIQUIDITY_DISTRESS does not apply to a financial firm (no classified balance sheet) or a regulated
# utility (its balance sheet is shaped by its rate base and commercial-paper funding) -- APP-09's two
# cases, keyed on the APP-00 company profile's ``financial_firm`` / ``regulated_utility`` since
# T-071 part 2a (D5), never on the GICS sector (a payment processor, an exchange or an insurance
# broker in GICS Financials is an operating company under APP-00 and *is* evaluated; so is a
# Utilities-sector company outside the overlay, e.g. AES or NRG).


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
class _NegativeFcfRule:
    """NEGATIVE_FCF: ``cashflow.free_cash_flow_margin < 0`` -- the trailing-twelve-month free cash
    flow (MD-04: operating cash flow, CON-08, less capex, MET-08) over revenue (MD-27; the checklist
    leaves that measure's source pending). It was a plain :class:`_ThresholdRule` until T-071 part 2a.

    Two overlays are *evaluated and never hit*: a **regulated utility** (D-13) and a **REIT**
    (D-14, the user's decision of 2026-10-15). Both keep the ``LIQUIDITY_DISTRESS`` exemption
    pattern -- the asset is evaluated, so a stint opened before the overlay applied closes, but it
    is never hit. **The profile is the APP-00 one** (``profile_as_of`` on the cycle date), never the
    GICS sector. Every other rule still applies to a REIT: ``LEVERAGE_EXTREME`` (including the
    negative-equity screen), the price vetoes and ``DATA_QUALITY``.

    **D-14's sources, in order.** (1) **APP-04b** (checklist v1.3; C1 pp.7-8): REIT capex includes
    acquisitions and development, and "FCF-based metrics are flagged as non-standard for REITs".
    (2) **C6 p.10** (APP-04's source): "Equity REITs' financial viability is based on GAAP net
    income from continuing operations, and/or Funds From Operations (FFO), if reported". (3) **C5
    p.54** (APP-04c's source): REITs are companies "where majority of the company's taxable Income
    is paid out as a dividend", so growth capex is funded externally -- D-13's argument, an
    inference declared as D-13's was. (4) the checklist Scope's D-14 paragraph
    (``phase3_decisions.md``): "D-13's argument (negative free cash flow as the normal state, funded
    externally) applies to REITs too, and APP-04b puts acquisitions in REIT capex." (5) the
    measurement (``docs/model_fixes.md`` "T-071 part 2b, D-14"): the REITs' acquisition share of
    capex -- O 97.3%, VTR 92.5%, FRT 69.9%, CPT 52.3%; ARE, DLR and IRM 0% -- and the per-date
    counts.

    **Declared: no replacement veto.** No loaded source gives a REIT cash-coverage threshold (C13 is
    utilities only), and a threshold is never invented (MET-00). A regulated utility's replacement
    test is :class:`_UtilityCashCoverageRule`."""

    RULE_ID = "NEGATIVE_FCF"
    SEVERITY = "HARD"
    DESCRIPTION = (
        "negative trailing-twelve-month free-cash-flow margin; not applied to a regulated utility "
        "or a REIT"
    )
    metric: str = "cashflow.free_cash_flow_margin"
    op: str = "<"
    threshold: float = 0.0

    @property
    def PARAMS(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "op": self.op,
            "threshold": self.threshold,
            "exempt": (
                "the regulated-utility overlay (D-13) and the REIT overlay (D-14) -- the APP-00 "
                "profile, never the GICS sector"
            ),
            "replacement": "none for a REIT (no source gives a threshold, MET-00)",
        }

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        hits = []
        evaluated = set()
        for aid, metrics in ctx.metrics.items():
            profile = ctx.profiles.get(aid)
            if profile is not None and (profile.regulated_utility or profile.reit):
                if metrics:
                    evaluated.add(aid)  # never a hit: closes a stint opened before the exemption
                continue
            value = metrics.get(self.metric)
            if value is None:
                continue
            evaluated.add(aid)
            if value < self.threshold:
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
    """LEVERAGE_EXTREME, in two branches.

    **Positive book equity:** ``debt_to_equity > debt_to_equity_threshold`` (3.0), unchanged (C2,
    T-116).

    **Non-positive book equity (D-04, T-071 part 2a):** keyed on the *equity sign*, never on a
    negative D/E. Since T-071 part 1's APP-07, D/E is NA (quarantined) when equity <= 0, so a plain
    ``dte > 3.0`` check would let a maximally leveraged firm -- a large buyback-driven negative
    equity (MCD, SBUX, PM, ...) -- pass silently. The equity sign and the credit inputs come from
    the ``DQ_NEG_EQUITY`` record (:attr:`RuleContext.negative_equity`), which the gate fills for
    every latest filing with equity <= 0. The rule reads TTM EBITDA and net debt from that record
    rather than reconstructing them from the ratio's sign: a negative ``net_debt_to_ebitda`` is
    either a genuine net-cash position (healthy) or negative EBITDA with positive net debt (the most
    distressed profile of all), and only the raw inputs tell them apart (T-116, PR #95 review).

    HARD when any of T-116's conditions holds (thresholds unchanged):

    - EBITDA <= 0 with net debt > 0 (burning cash with debt);
    - net debt/EBITDA > 5.0, with EBITDA > 0 (T-116's declared calibration -- no rating
      methodology is loaded);
    - interest coverage < 1.5.

    A filing with no usable credit input at all is routed to SOFT review, never silently passed
    (T-116). The screen **does not apply to a financial firm** (APP-00/APP-02, the profile's
    ``financial_firm``): such an asset is evaluated and never hit, so a stint opened before the
    exemption closes -- the ``LIQUIDITY_DISTRESS`` pattern.
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
            "negative_equity": "keyed on the equity sign (the DQ_NEG_EQUITY record, D-04)",
            "neg_equity_net_debt_to_ebitda_threshold": self.neg_equity_net_debt_to_ebitda_threshold,
            "neg_equity_interest_coverage_threshold": self.neg_equity_interest_coverage_threshold,
            "not_applied_to": "financial firms (the profile's financial_firm, APP-00/APP-02)",
        }

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        hits = []
        evaluated = set()
        for aid, metrics in ctx.metrics.items():
            record = ctx.negative_equity.get(aid)
            if record is not None:
                evaluated.add(aid)
                profile = ctx.profiles.get(aid)
                if profile is not None and profile.financial_firm:
                    continue  # APP-02: the distress screen does not apply to a financial firm
                hit = self._negative_equity_hit(aid, metrics, record)
                if hit is not None:
                    hits.append(hit)
                continue
            dte = metrics.get("leverage.debt_to_equity")
            if dte is None:
                continue
            evaluated.add(aid)
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

    def _negative_equity_hit(
        self, aid: int, metrics: dict[str, float | None], record: dict[str, float | None]
    ) -> VetoHit | None:
        """D-04's screen for one non-positive-equity filing: its HARD hit, its SOFT review when no
        credit input is usable, or ``None`` (evaluated, not distressed)."""
        ebitda = record.get("ebitda")
        net_debt = record.get("net_debt")
        ndte_raw = metrics.get("leverage.net_debt_to_ebitda")
        cov = metrics.get("leverage.interest_coverage")
        # T-116 (PR #95 review): a negative ratio is ambiguous (net cash vs burning cash with debt)
        # and is not used; a non-negative one is reliable on its own. With EBITDA <= 0 the ratio is
        # not meaningful at all (the plan's "(EBITDA > 0)") -- condition 1 covers that profile.
        ndte = ndte_raw if ndte_raw is not None and ndte_raw >= 0 else None
        if ebitda is not None and ebitda <= 0:
            ndte = None
        burning_cash_with_debt = (
            ebitda is not None and ebitda <= 0 and net_debt is not None and net_debt > 0
        )
        failed = [
            name
            for name, fired in (
                ("ebitda_nonpositive_with_net_debt", burning_cash_with_debt),
                (
                    "net_debt_to_ebitda",
                    ndte is not None and ndte > self.neg_equity_net_debt_to_ebitda_threshold,
                ),
                (
                    "interest_coverage",
                    cov is not None and cov < self.neg_equity_interest_coverage_threshold,
                ),
            )
            if fired
        ]
        evidence: dict[str, Any] = {
            "equity": record.get("equity"),
            "net_debt_to_ebitda": ndte_raw,
            "interest_coverage": cov,
            "ebitda": ebitda,
            "net_debt": net_debt,
        }
        if failed:
            return VetoHit(aid, self.RULE_ID, self.SEVERITY, {**evidence, "failed": failed})
        if ndte is None and cov is None and not (ebitda is not None and net_debt is not None):
            # No usable credit input at all: could not tell (T-116), never a silent pass.
            return VetoHit(
                aid,
                self.RULE_ID,
                "SOFT",
                {**evidence, "reason": "negative book equity, no corroborating leverage metric"},
            )
        return None


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
    to a financial firm or a regulated utility (the APP-00 profile, D5): they are evaluated and never
    hit, so a stint opened before the exemption closes."""

    RULE_ID = "LIQUIDITY_DISTRESS"
    SEVERITY = "SOFT"
    DESCRIPTION = (
        "current ratio below 1.0 and weak cash coverage (interest coverage or operating cash "
        "flow margin); not applied to a financial firm or a regulated utility"
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
            "exempt": "a financial firm or the regulated-utility overlay (the APP-00 profile)",
        }

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        hits = []
        evaluated = set()
        for aid, metrics in ctx.metrics.items():
            profile = ctx.profiles.get(aid)
            if profile is not None and (profile.financial_firm or profile.regulated_utility):
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
class _UtilityCashCoverageRule:
    """UTILITY_CASH_COVERAGE (HARD, T-071 part 2a, D-13): a regulated utility whose **three-year
    average** operating cash flow *before working-capital changes*, over its total debt, is below
    5% -- C13 p.7's Ba band lower bound (the band is 5%-13%). It replaces ``NEGATIVE_FCF`` for the
    overlay (C13 p.11, p.14, p.19, p.21).

    The input (:func:`cycle.data.utility_cash_coverage`) is the newest **10-K** usable on the cycle
    date, never the latest filing: a 10-Q does not carry a three-year cash-flow average. It holds two
    bounds (R2, 2026-10-14): the three-year average with the *ambiguous* working-capital lines left
    in the cash flow, and with them treated as working capital. The rule is HARD **only when both
    bounds are below the cutoff**; a pair that straddles it, or a missing bound, is *evaluated and
    never hit*, so no veto row exists for it -- the flag
    (``flag_utility_cash_coverage_ambiguous_working_capital``) and both values stay in the rule
    input's ``inputs_json``, which is this rule's evidence (D-13's "missing inputs: no veto,
    flagged"). A utility whose newest 10-K carries no rule input at all is not evaluated ("could not
    tell"), so an open stint stays open.

    The classification that decides which lines are working capital, the C13 quotes and the three
    declared inferences are in ``docs/model_fixes.md`` "T-071 part 2a"; the table itself is in
    ``fundamental_agent.utility_coverage``."""

    RULE_ID = "UTILITY_CASH_COVERAGE"
    SEVERITY = "HARD"
    DESCRIPTION = (
        "regulated utility whose three-year average operating cash flow before working capital "
        "covers less than 5% of total debt (C13 p.7)"
    )
    threshold: float = UTILITY_COVERAGE_THRESHOLD

    @property
    def PARAMS(self) -> dict[str, Any]:
        return {
            "metric": "rule_input.utility_cash_coverage_3y",
            "op": "<",
            "threshold": self.threshold,
            "bounds": "both the ambiguous-in and the ambiguous-as-working-capital averages",
            "applies_to": "the regulated-utility overlay (the APP-00 profile, D-13)",
            "missing_input": "no veto, not evaluated",
            "straddling_or_missing_bound": "no veto, evaluated, flagged in the rule input",
            "source": "C13 p.7 (the Ba band, 5%-13%); C13 p.11, p.14, p.19, p.21",
        }

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        hits = []
        evaluated = set()
        for aid, record in ctx.utility_cash_coverage.items():
            profile = ctx.profiles.get(aid)
            if profile is None or not profile.regulated_utility:
                continue  # only the overlay; an unknown profile is never guessed
            evaluated.add(aid)
            bound_in = record.get("ambiguous_in")
            bound_wc = record.get("ambiguous_as_wc")
            if bound_in is None or bound_wc is None:
                continue  # a missing bound: no veto (R2); the flag is in the rule input
            if bound_in < self.threshold and bound_wc < self.threshold:
                hits.append(
                    VetoHit(
                        aid,
                        self.RULE_ID,
                        self.SEVERITY,
                        {
                            "utility_cash_coverage_3y_ambiguous_in": bound_in,
                            "utility_cash_coverage_3y_ambiguous_as_wc": bound_wc,
                            "threshold": self.threshold,
                            "ambiguous_working_capital": bool(
                                record.get("ambiguous_working_capital")
                            ),
                        },
                    )
                )
        return RuleResult(hits, frozenset(evaluated))


@dataclass
class _TrendBreakRule:
    """BREAK_TREND_200 (SOFT, flag-only, T-070): the close is more than 5% under its 200-day simple
    moving average. SOFT, not HARD: a HARD veto here would purge half the index in any broad
    pullback. It also takes **no points** off the blended score (``PENALTY_POINTS = 0``): it
    fires on about a fifth of the index on a typical day (up to 72%), the 15-point penalty would
    dominate the ranking, and it repeats the trend TECHNICAL v2 already scores (12-1 momentum,
    drawdown). Its stints and evidence stay in ``veto`` / ``v_veto`` for review."""

    RULE_ID = "BREAK_TREND_200"
    SEVERITY = "SOFT"
    PENALTY_POINTS = 0.0
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
    _NegativeFcfRule(),
    _UtilityCashCoverageRule(),
    _LiquidityRule(),
    _DrawdownRule(),
    _StaleFundamentalRule(),
    _DataQualityRule(),
    _TrendBreakRule(),
    _VolatilityShockRule(),
    _CrashZRule(),
]
