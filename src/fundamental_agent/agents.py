"""Strands agent layer: a metrics-master orchestrator over per-group specialists.

Deterministic ratio math lives in :mod:`fundamental_agent.metrics`; the agents only
*interpret* those numbers. The orchestrator decides which specialist agents to consult
for a given filing, then a synthesis step turns the readings into a scored assessment.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, ValidationError
from strands import Agent, tool
from strands.models.openai import OpenAIModel

from fundamental_agent import pillars, quality
from fundamental_agent.applicability import apply_applicability
from fundamental_agent.config import Settings
from fundamental_agent.metrics import (
    CORE_GROUPS,
    OPTIONAL_GROUPS,
    MetricResult,
    compute_group,
    provenance,
)
from fundamental_agent.metrics import valuation as valuation_metrics
from fundamental_agent.metrics.base import TTMFlow
from fundamental_agent.pricing import ClosePrice
from fundamental_agent.skills import (
    GROUP_METRICS,
    group_applicability,
    group_definitions,
    load_skill,
)
from fundamental_agent.statements import Statements
from kg_schema import corporate_actions
from kg_schema.company_profile import profile_as_of
from kg_schema.market_cap import MarketCap

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_BULLISH_CUTOFF = 60.0
_NEUTRAL_CUTOFF = 40.0
_HIGH_LEVERAGE = 3.0
_HIGH_LEVERAGE_PENALTY = 12.0

log = logging.getLogger(__name__)

_ALL_GROUPS = (*CORE_GROUPS, *OPTIONAL_GROUPS)
# valuation needs a market price, so it is computed outside the uniform group loop;
# it still gets a specialist tool whenever a period-end close was resolved.
_SPECIALIST_GROUPS = (*_ALL_GROUPS, valuation_metrics.GROUP)

MASTER_PROMPT = """You are the metrics master for a fundamental equity analysis.
You are given the deterministically computed financial ratios for one SEC filing.
Call the specialist tool for every metric group that carries signal for this company
-- always consult profitability, liquidity, leverage and cash flow; add efficiency,
growth, roic and cagr when their numbers are meaningful; add valuation whenever
free-cash-flow-yield numbers are present.
The company type is not yours to decide: an empty metric whose reason is `not_applicable`
is the code's own decision that the metric does not apply to this company (APP-00), so
read that reason rather than reclassifying the company or skipping a group by hand.
After gathering the readings, give a short synthesis in plain language."""

# The fixed lead of every specialist's system prompt (T-076, D2): no recompute, and the company type comes
# from the code's own `not_applicable` reason, never from the model (APP-00, T-071).
_SPECIALIST_LEAD = (
    "You are the {group} specialist in a fundamental-analysis pipeline. The numbers are already "
    "computed for you -- do not recompute them and do not ask for filing text. Use only the numbers "
    "given. Give a concise 1-2 sentence assessment for the orchestrator, applying any caveat the "
    "procedure below would raise. The brief carries no company type: an empty metric whose reason is "
    "`not_applicable` is the code's own decision that the metric does not apply to this company, so read "
    "that reason as the company type -- never reclassify the company yourself. On a quarterly filing the "
    "flows are the trailing twelve months, never a single quarter read as a year (PER-06)."
)

# D3 (T-076): the basis of a group's metrics on a quarterly filing, from provenance.ITEMS. `prior`
# resolves to the prior year's same quarter on a 10-Q (H3, provenance._prior_basis).
_BASIS_LABELS: dict[str, str] = {
    "ttm": "trailing twelve months",
    "period": "this filing's own quarter",
    "prior_year": "the prior fiscal year end",
    "prior": "the same quarter a year earlier",
}


def basis_paragraph(group: str, results: list[MetricResult]) -> str:
    """One line per metric of *group*: its items, grouped by the basis they are read on (T-076, D3).

    From ``provenance.ITEMS`` alone, plus the metric's own unit (a ``usd`` metric read on the filing's own
    quarter is a USD amount for that quarter). A 10-K needs none of it: its flows are already annual.
    """
    units = {result.name: result.unit for result in results}
    lines: list[str] = []
    for name in GROUP_METRICS.get(group, ()):
        items = provenance.ITEMS.get(name)
        if not items:
            continue
        groups: dict[str, list[str]] = {}
        for item, bases in items.items():
            for basis in (bases,) if isinstance(bases, str) else bases:
                groups.setdefault(basis, []).append(item.replace("_", " "))
        parts = [
            f"{', '.join(group_items)} ({_BASIS_LABELS.get(basis, basis)})"
            for basis, group_items in groups.items()
        ]
        line = f"{name}: " + "; ".join(parts)
        if units.get(name) == "usd" and "period" in groups:
            line += " (a USD amount for the quarter)"
        lines.append(line)
    if not lines:
        return ""
    return (
        "Basis of the numbers below on this quarterly filing (PER-06): the flows are the trailing twelve "
        "months unless a line says otherwise.\n" + "\n".join(lines)
    )


def _metric_lines(
    group: str, results: list[MetricResult], withheld: Mapping[tuple[str, str], str] | None = None
) -> list[str]:
    """``name: value``, ``name: withheld (<rule>)`` or ``name: empty (<reason>)``.

    An empty value is never a 0 and never a neutral reading (MET-00; T-076, D4). A value a HARD data-quality
    rule quarantines is withheld from the model rather than read (T-076, D5)."""
    lines: list[str] = []
    for result in results:
        rule = (withheld or {}).get((group, result.name))
        if rule is not None:
            lines.append(f"{result.name}: withheld ({rule})")
        elif result.value is None:
            reason = (result.inputs.get("provenance") or {}).get("empty_reason") or "missing_input"
            lines.append(f"{result.name}: empty ({reason})")
        else:
            lines.append(f"{result.name}: {_round(result.value)}")
    return lines


def withheld_metrics(results: list[tuple[str, MetricResult]]) -> dict[tuple[str, str], str]:
    """``{(group, metric): rule_id}`` for the values a HARD DQ rule quarantines (T-076, D5).

    The gates are ``quality``'s own, run on the in-memory results with its own functions and constants
    (Annex A-01 to A-05, the identity gates), so the prompt withholds exactly what ``cycle`` would read as
    NULL -- never a second, hand-copied threshold. Only a metric that **has** a value is withheld: an empty
    value keeps its own reason (``empty (<reason>)``), which is the metric's own cause, not a data-quality
    one (fix round 1, K5)."""
    filing: quality.FilingMetrics = {
        (group, result.name): quality.StoredMetric(result.value, result.inputs)
        for group, result in results
    }
    return {
        issue.metric: issue.rule_id
        for issue in quality.evaluate(filing)
        if issue.severity == "HARD" and issue.value is not None
    }


#: The withheld-map key's reason for a valuation value built on the private import's close
#: (T-153 part 3d, A3). Read by nothing but the prompt, like every other entry in the map.
TIINGO_CLOSE_REASON = "TIINGO_CLOSE"


class FilingView(Protocol):
    """What the scoring step reads from one filing -- its identity and the prices it was valued on.

    The metrics are given to :meth:`FundamentalAnalyst.score` already computed, so the statements are
    not here: a score depends on the metric results, the filing's name in the prompts, and whether its
    period-end close is the private import's (the Tiingo withholding rule below). :class:`FilingContext`
    satisfies this protocol, and ``fundamental_agent score`` satisfies it from the stored rows alone.
    """

    @property
    def ticker(self) -> str: ...

    @property
    def company_name(self) -> str: ...

    @property
    def form(self) -> str: ...

    @property
    def fiscal_period(self) -> str: ...

    @property
    def price(self) -> ClosePrice | None: ...

    @property
    def market_cap(self) -> MarketCap | None: ...


def tiingo_withheld_metrics(
    ctx: FilingView, results: list[tuple[str, MetricResult]]
) -> dict[tuple[str, str], str]:
    """``{(group, metric): reason}`` for the valuation group when the private import priced the line.

    The licence behind the Tiingo file is internal-use-only, and a valuation value (a market cap,
    an enterprise value, a yield over either) is the close in another form: D4's rule is per row,
    so the *close's own* ``price_daily.source`` decides -- not a list of asset ids, and not
    ``ctx.market_cap`` read directly (the model never sees it; it reads these metrics). Every
    valuation-group metric **with a value** is withheld, so the brief and every specialist print
    ``name: withheld (TIINGO_CLOSE)`` instead of the value, exactly as T-076 D5's quarantined
    metrics do. The stored metrics are untouched: ``cycle``'s VALORIZATION and the cap readers
    still read them. This map is built separately from :func:`withheld_metrics` and merged after
    it, so which metrics the HARD DQ rules withhold does not change.
    """
    if not _close_is_from_tiingo(ctx):
        return {}
    return {
        (group, result.name): TIINGO_CLOSE_REASON
        for group, result in results
        if group == valuation_metrics.GROUP and result.value is not None
    }


def _close_is_from_tiingo(ctx: FilingView) -> bool:
    """True when the filing's close -- the market cap's and ``ctx.price``'s -- is the import's.

    Both come from the same ``price_daily`` row family: the market cap's reader records its
    close's source, and the period-end close carries its own."""
    if ctx.market_cap is not None and ctx.market_cap.close_source == _TIINGO_VENDOR:
        return True
    return ctx.price is not None and ctx.price.source == _TIINGO_VENDOR


_TIINGO_VENDOR = corporate_actions.TIINGO_SOURCE


def _generated_definitions(group: str) -> str:
    """The group's metric definitions, from the generated checklist data (T-076, D1/D2)."""
    lines = [f"--- GENERATED: the {group} metrics' definitions (checklist Annex B) ---"]
    lines += [
        f"{row['metric']} [{row['md_id']}, {row['basis']}]: {row['formula']}"
        for row in group_definitions(group)
    ]
    return "\n".join(lines)


def _generated_applicability(group: str) -> str:
    """The group's applicability rows, from the generated checklist matrix (T-076, D1/D2)."""
    lines = [f"--- GENERATED: the {group} metrics' applicability (checklist matrix) ---"]
    lines += [
        f"{row['metric']}: {row['company_type']} {row['code']}"
        + (f" ({row['rule']})" if row["rule"] else "")
        for row in group_applicability(group)
    ]
    return "\n".join(lines)


def _specialist_system_prompt(group: str) -> str:
    """A specialist's system prompt: a fixed lead, the checklist's generated data, then its skill."""
    parts = [
        _SPECIALIST_LEAD.format(group=group),
        _generated_definitions(group),
        _generated_applicability(group),
    ]
    sop = load_skill(group)
    if sop is not None:
        parts.append(f"--- SKILL: {group} ---\n{sop}")
    return "\n\n".join(parts)


_SYNTHESIS_PROMPT = """Produce the final fundamental assessment for this filing.
The code has already decided which pillars this company has: {pillars}. Rate exactly those, and no
other -- a pillar the company does not have is the code's own decision (APP-00), never yours to add.
Reply with ONLY a raw JSON object -- no prose, no markdown fences -- with these keys:
  "pillars": an object whose keys are exactly {pillars}; each value is an object with
    "rating": integer 0-100 (50 = that pillar of an average S&P 500 company, higher = stronger)
    "reason": one sentence naming the metrics of that pillar that drove the rating
  "narrative": string, 3-5 sentences citing the specific ratios that drove the ratings
  "strengths": array of 2-4 short phrases
  "risks": array of 2-4 short phrases
Base everything only on the metrics and specialist readings gathered above."""

_REPAIR_PROMPT = (
    "That was not valid JSON. Reply again with ONLY the JSON object described above, nothing else."
)


def synthesis_prompt(pillars_to_rate: Sequence[str]) -> str:
    """D2's synthesis prompt, naming the pillars this filing's code decided it has.

    Only the *prompt* is per filing: ``_prompt_version_hash`` hashes the template above, so the
    version a row records stays constant across every filing of a run (T-113)."""
    named = ", ".join(f'"{pillar}"' for pillar in pillars_to_rate)
    return _SYNTHESIS_PROMPT.format(pillars=named or "none")


# (flat metric key, low bound, high bound, points) -- score nudges for the offline
# fallback used only when the LLM cannot return usable JSON.
_SCORE_RULES: tuple[tuple[str, float, float, float], ...] = (
    ("profitability.net_margin", 0.05, 0.15, 12.0),
    ("profitability.return_on_equity", 0.10, 0.20, 10.0),
    ("cashflow.free_cash_flow_margin", 0.0, 0.10, 12.0),
    ("growth.revenue_growth", 0.0, 0.10, 8.0),
    ("liquidity.current_ratio", 1.0, 1.5, 6.0),
    ("valuation.free_cash_flow_yield", 0.0, 0.05, 8.0),
)


class PillarRating(BaseModel):
    """One pillar's rating as the model returns it (D2): an integer 0-100 and one sentence."""

    rating: int = Field(ge=0, le=100)
    reason: str


class SynthesisReply(BaseModel):
    """The model's JSON answer (D2): the pillar ratings, and the narrative around them.

    The score and the rating are **not** the model's -- the code computes both from these ratings
    (constitution, AI behavior #1: the deterministic part never sits with the model)."""

    pillars: dict[str, PillarRating]
    narrative: str
    strengths: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class FundamentalAssessment(BaseModel):
    """The code's verdict on one filing -- persisted as an immutable snapshot.

    T-073: the model rates each pillar, the code weighs and totals them, so ``score`` and ``rating``
    are the code's. Both are ``None`` when an applicable pillar has no value (D1's coverage rule,
    MET-00): the row records why and the member is unscored (T-119)."""

    score: float | None = Field(default=None, ge=0.0, le=100.0)
    rating: Literal["bullish", "neutral", "bearish"] | None = None
    narrative: str
    strengths: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class FilingContext:
    """Everything the agents need about the filing under analysis."""

    ticker: str
    company_name: str
    form: str
    fiscal_period: str
    stmts: Statements
    period_key: str
    prior_key: str | None
    price: ClosePrice | None = None  # period-end close, when the pricing table has one
    # {"shares_outstanding" | "diluted_shares": factor} -- multiply the reported
    # value by this to correct a detected XBRL scale/tagging defect (see
    # docs/model_fixes.md, F1; fundamental_agent.db.detect_share_scale_factors).
    share_scale_factors: dict[str, float] = field(default_factory=dict)
    # T-159c: the cover-page count is >= 50x / <= 1/50x the prior filing's count for its class.
    # A stored flag only: the MKT guard stays the value rule, so the market cap is unchanged.
    cover_scale_suspect: bool = False
    # {flow: TTMFlow} on a 10-Q filing -- the trailing-twelve-month flows that ratios over a
    # stock or a price level need instead of the raw 3-month value (F4, T-105,
    # docs/model_fixes.md; fundamental_agent.db.ttm_detail). Empty for a 10-K, which already
    # reports annual flows.
    ttm_flows: dict[str, TTMFlow] = field(default_factory=dict)
    # the issuer's zero-padded CIK: a multi-class share count is converted to the traded class by
    # `kg_schema.market_cap.CLASS_CONVERSION` (T-132: BRK's A = 1,500 B)
    cik: str | None = None
    market_cap: MarketCap | None = None
    market_cap_missing: str | None = None

    @property
    def ttm(self) -> dict[str, float]:
        return {
            item: flow.value for item, flow in self.ttm_flows.items() if flow.method != "refused"
        }

    @property
    def real_ttm(self) -> dict[str, float]:
        """The flows whose trailing twelve months were actually built (T-105 ``ytd`` or
        ``quarters``), without the ``x4`` fallback -- one quarter times four is that quarter's
        ratio again, not a trailing year."""
        return {
            item: f.value for item, f in self.ttm_flows.items() if f.method in ("ytd", "quarters")
        }


# The flows each group annualizes on a 10-Q (F4, T-105): its ratios' TTM numerators.
_GROUP_TTM_ITEMS: dict[str, tuple[str, ...]] = {
    "profitability": ("net_income",),
    "efficiency": ("revenue", "cogs"),
    "leverage": ("operating_income", "depreciation_amortization"),
    "roic": ("operating_income",),
    "valuation": (
        "operating_cash_flow",
        "capital_expenditure",
        "stock_based_compensation",
        "interest_expense",
    ),
}

# F2 (T-151 part 2 fix 1): the items part 2 added to a group. The group's ``annualized_x4`` stamp
# keeps master's item lists -- the earnings-yield gate (MET-05) reads profitability's stamp, and a
# crude operating-income TTM must not drop a filing whose net-income TTM is real. Each added item
# carries its own ``flag_<item>_annualized_x4`` instead.
_GROUP_TTM_EXTRA_ITEMS: dict[str, tuple[str, ...]] = {
    "profitability": ("operating_income", "income_tax", "pretax_income"),
    "roic": ("income_tax", "pretax_income"),
    "valuation": ("operating_income", "depreciation_amortization", "income_tax", "pretax_income"),
}


def _group_ttm(group: str, ctx: FilingContext) -> dict[str, float] | None:
    """The *ttm* argument for *group*'s ``compute``. On a 10-Q every group takes
    the real trailing-twelve-month flows only (:attr:`FilingContext.real_ttm`, PER-06/D-09);
    on a 10-K, ``None`` says "already annual"."""
    if ctx.form != "10-Q":
        return None
    res = dict(ctx.real_ttm)
    # F1: mark a flow the group needs but whose TTM could not be built (x4 or refused), so a metric
    # can tell "not built" from "not filed" (a REIT's TTM rate).
    for item in (*_GROUP_TTM_ITEMS.get(group, ()), *_GROUP_TTM_EXTRA_ITEMS.get(group, ())):
        flow = ctx.ttm_flows.get(item)
        if flow is not None and flow.method in ("x4", "refused"):
            res[f"flag_{item}_ttm_not_built"] = 1.0
    if group == "leverage":
        da_flow = ctx.ttm_flows.get("depreciation_amortization")
        if da_flow and "depreciation_only_not_da" in da_flow.flags:
            res["flag_depreciation_amortization_depreciation_only_not_da"] = 1.0
        if da_flow and "da_amortization_only_refused" in da_flow.flags:
            res["flag_depreciation_amortization_da_amortization_only_refused"] = 1.0
    return res


def _flag_annualization(results: list[tuple[str, MetricResult]], flows: dict[str, TTMFlow]) -> None:
    """Stamp each annualizing group's audit inputs with how its TTM numerators were obtained
    (T-105): ``annualized_ttm = 1`` when every one is a real trailing twelve months,
    ``annualized_x4 = 1`` when any fell back to quarter x 4 -- crude annualization a consumer
    can exclude. A group's results share one inputs dict, so one stamp covers the group."""
    for group, result in results:
        items = [flows[i] for i in _GROUP_TTM_ITEMS.get(group, ()) if i in flows]
        if not items:
            continue
        real_items = [f for f in items if f.method != "refused"]
        if real_items:
            if any(f.method == "x4" for f in real_items):
                result.inputs["annualized_x4"] = 1.0
            else:
                result.inputs["annualized_ttm"] = 1.0
        if group == "leverage":
            da_flow = flows.get("depreciation_amortization")
            if da_flow and "depreciation_only_not_da" in da_flow.flags:
                result.inputs["flag_depreciation_amortization_depreciation_only_not_da"] = 1.0
            if da_flow and "da_amortization_only_refused" in da_flow.flags:
                result.inputs["flag_depreciation_amortization_da_amortization_only_refused"] = 1.0
        # F2: an added item's x4 fallback gets its own flag, never the group's stamp.
        for item in _GROUP_TTM_EXTRA_ITEMS.get(group, ()):
            flow = flows.get(item)
            if flow is not None and flow.method == "x4":
                result.inputs[f"flag_{item}_annualized_x4"] = 1.0


@dataclass(frozen=True)
class AnalysisResult:
    assessment: FundamentalAssessment
    metrics: list[tuple[str, MetricResult]]  # (group, result), value may be None
    flat_metrics: dict[str, float | None]  # "group.name" -> value
    # T-113: the LLM's own JSON verdict never came back parseable -- assessment is the
    # deterministic, rule-based fallback, not model output.
    used_fallback: bool
    # T-113: sha256 identifying the *prompt version* that produced this score -- constant
    # across every filing scored under the same code/skills/model config, and changing only
    # when a prompt, skill SOP, or model config actually changes (PR #91 review). See
    # `_prompt_version_hash`.
    prompt_hash: str
    # T-073 (D4): the pillar block stored with the row, inside `score_snapshot.inputs_json`.
    pillars: dict[str, Any] = field(default_factory=dict)


# T-113 (PR #91 review): temperature=0/seed=0 reduce run-to-run variance -- no LLM provider
# guarantees deterministic output at temperature 0, so this is not a claim of reproducibility.
_TEMPERATURE = 0
_SEED = 0
_MAX_TOKENS = 1500


def build_model(settings: Settings) -> OpenAIModel:
    """Point Strands' OpenAI provider at the configured (DeepSeek) endpoint.

    T-113: ``temperature = 0`` (was ``0.2``, an unexplained source of run-to-run variance);
    ``seed`` is the standard OpenAI-schema field for a deterministic sample -- passed
    optimistically (forwarded verbatim to the client, never validated against a strict
    schema), honoured or silently ignored depending on whether the configured endpoint's
    backend supports it (unverified against the live DeepSeek endpoint -- this environment's
    network egress policy blocks reaching it; confirm before `T-079`)."""
    return OpenAIModel(
        client_args={"api_key": settings.llm_api_key, "base_url": settings.llm_url},
        model_id=settings.llm_model,
        params={"temperature": _TEMPERATURE, "seed": _SEED, "max_tokens": _MAX_TOKENS},
    )


def _prompt_version_hash(model_name: str) -> str:
    """sha256 of everything that determines what gets sent to the LLM for *every* filing:
    the prompt templates themselves and the model config -- not any one filing's own
    transcript (T-113, PR #91 review: a per-filing hash is unique per row, unverifiable
    since the transcript itself isn't stored, and can't group scores by the prompt version
    that produced them, which is what `T-079` needs to separate scores from before/after a
    prompt edit). Constant across every filing scored under the same code/skills/model
    config; changes only when a prompt, a skill SOP file, or the model config changes."""
    payload = {
        "master_prompt": MASTER_PROMPT,
        "specialist_prompts": {g: _specialist_system_prompt(g) for g in _SPECIALIST_GROUPS},
        "synthesis_prompt": _SYNTHESIS_PROMPT,
        "repair_prompt": _REPAIR_PROMPT,
        "model_id": model_name,
        "temperature": _TEMPERATURE,
        "seed": _SEED,
        "max_tokens": _MAX_TOKENS,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def compute_metrics(ctx: FilingContext) -> list[tuple[str, MetricResult]]:
    """Every computed metric for one filing, **without any model** (T-143 part A, P1).

    The one place the metric groups are computed: ``FundamentalAnalyst.analyze`` calls it (through
    :meth:`FundamentalAnalyst._compute_all`) for a scored run, and
    ``fundamental_agent run --metrics-only`` calls it directly, so a metrics-only filing's rows are
    exactly a scored filing's."""
    out: list[tuple[str, MetricResult]] = []
    for group in _ALL_GROUPS:
        for result in compute_group(
            group, ctx.stmts, ctx.period_key, ctx.prior_key, _group_ttm(group, ctx)
        ):
            out.append((group, result))
    if ctx.price is not None or ctx.market_cap is not None or ctx.market_cap_missing is not None:
        ttm_val = _group_ttm(valuation_metrics.GROUP, ctx)
        for result in valuation_metrics.compute(
            ctx.stmts,
            ctx.period_key,
            ctx.price,
            ctx.share_scale_factors,
            ttm_val,
            cik=ctx.cik,
            market_cap=ctx.market_cap,
            market_cap_missing=ctx.market_cap_missing,
            cover_scale_suspect=ctx.cover_scale_suspect,
        ):
            out.append((valuation_metrics.GROUP, result))

    _flag_annualization(out, ctx.ttm_flows)
    # T-071 part 1 (P4): the applicability matrix, in this one place -- after the groups and
    # before `provenance.attach`, so the empty reason reads the NA cell. A filing with no CIK at
    # all (a hermetic fixture) carries no profile: the layer is skipped, never guessed.
    if ctx.cik:
        apply_applicability(out, profile_as_of(ctx.cik, ctx.period_key[:10]))
    # T-151 part 3 (D14): the provenance block on every metric, in one place (G2: the TTM
    # method and columns come from the filing's own flows).
    provenance.attach(out, ctx.stmts, ctx.period_key, ctx.ttm_flows)
    return out


class FundamentalAnalyst:
    """Runs the orchestrator + synthesis for one filing at a time."""

    def __init__(self, model: OpenAIModel | None, model_name: str) -> None:
        # ``model`` is None for the deterministic path (``_compute_all`` and the recompute script,
        # which never call the LLM); ``analyze`` needs a real model.
        self.prompt_hash = _prompt_version_hash(model_name)
        self._model = model
        self.model_name = model_name

    def analyze(self, ctx: FilingContext) -> AnalysisResult:
        """Compute this filing's metrics, then score them (the run's own path)."""
        return self.score(ctx, self._compute_all(ctx))

    def score(self, ctx: FilingView, computed: list[tuple[str, MetricResult]]) -> AnalysisResult:
        """The scoring step for **already-computed** metrics -- the model's part, once.

        Split out of :meth:`analyze` so ``fundamental_agent score`` scores the metric rows a run
        already stored without recomputing a ratio, and the run and the ``score`` command write the
        same row from the same code. Nothing here reads the statements: every value comes from
        *computed*, and *ctx* only names the filing in the prompts and decides the Tiingo
        withholding (T-153 part 3d, A3).
        """
        statuses = pillars.pillar_statuses(computed)
        flat = _flat_metrics(computed)
        if pillars.empty_pillars(statuses):
            # D1's coverage rule (MET-00): an applicable pillar with no value leaves the score
            # empty, so there is nothing to rate and the model is not asked (nothing it returned
            # would be scored). The block records the statuses and every empty reason.
            return self._result(computed, flat, facts_only_synthesis(computed))
        by_group = _group(computed)
        withheld = withheld_map(ctx, computed)
        orchestrator = Agent(
            model=self._model,
            system_prompt=MASTER_PROMPT,
            callback_handler=None,
            tools=[
                self._make_specialist(name, by_group.get(name, []), ctx, withheld)
                for name in _SPECIALIST_GROUPS
            ],
        )
        orchestrator(_brief(ctx, by_group, withheld))
        return self._result(computed, flat, _synthesize(orchestrator, flat, statuses, withheld))

    def score_rule_based(
        self, ctx: FilingView, computed: list[tuple[str, MetricResult]]
    ) -> AnalysisResult:
        """The deterministic verdict on already-computed metrics, with **no model call**.

        It is exactly what :meth:`score` returns when the model cannot answer (``_synthesize``'s
        fallback, HARD-withheld metrics included), so a ``fundamental_agent score --rule-based`` row
        is the row a run writes when every filing falls back: ``rule-based-fallback-v1``.
        """
        statuses = pillars.pillar_statuses(computed)
        flat = _flat_metrics(computed)
        if pillars.empty_pillars(statuses):
            return self._result(computed, flat, facts_only_synthesis(computed))
        verdict = _fallback_synthesis(flat, statuses, withheld_map(ctx, computed))
        return self._result(computed, flat, verdict)

    def _result(
        self,
        computed: list[tuple[str, MetricResult]],
        flat: dict[str, float | None],
        verdict: _Synthesis,
    ) -> AnalysisResult:
        return AnalysisResult(
            assessment=verdict.assessment,
            metrics=computed,
            flat_metrics=flat,
            used_fallback=verdict.used_fallback,
            prompt_hash=self.prompt_hash,
            pillars=verdict.pillars,
        )

    def _compute_all(self, ctx: FilingContext) -> list[tuple[str, MetricResult]]:
        """:func:`compute_metrics`, kept as a method for the existing callers (T-143 part A, P1)."""
        return compute_metrics(ctx)

    def _make_specialist(
        self,
        group: str,
        results: list[MetricResult],
        ctx: FilingView,
        withheld: Mapping[tuple[str, str], str],
    ) -> Any:
        computed = [result for result in results if result.value is not None]
        lines = _metric_lines(group, results, withheld)
        paragraph = basis_paragraph(group, results) if ctx.form == "10-Q" else ""
        model = self._model

        @tool(name=f"{group}_specialist")
        def specialist() -> str:
            """Get a specialist reading of this metric group for the filing."""
            if not computed:
                return f"No {group} metric has a value for this filing."
            reader = Agent(
                model=model,
                system_prompt=_specialist_system_prompt(group),
                callback_handler=None,
            )
            prompt = "\n\n".join(
                part
                for part in (
                    f"{ctx.ticker} ({ctx.company_name}) {ctx.form} {ctx.fiscal_period}",
                    paragraph,
                    f"{group}:\n" + "\n".join(lines),
                )
                if part
            )
            try:
                return str(reader(prompt)).strip()
            except Exception as exc:  # a dead specialist must not abort the filing
                return f"({group} reading unavailable: {exc})"

        return specialist


def _group(pairs: list[tuple[str, MetricResult]]) -> dict[str, list[MetricResult]]:
    out: dict[str, list[MetricResult]] = {}
    for group, result in pairs:
        out.setdefault(group, []).append(result)
    return out


def _flat_metrics(pairs: Sequence[tuple[str, MetricResult]]) -> dict[str, float | None]:
    """``"group.metric" -> value`` -- the flat block a score row stores beside its pillars."""
    return {f"{group}.{result.name}": result.value for group, result in pairs}


def withheld_map(
    ctx: FilingView, computed: list[tuple[str, MetricResult]]
) -> dict[tuple[str, str], str]:
    """Every reason a metric is withheld from the model: the HARD DQ rules, then the Tiingo close.

    T-153 part 3d (A3): the private import's valuation values are withheld from the model too. The
    Tiingo map is built separately and merged after the DQ map, and a key the HARD DQ rules already
    withhold keeps their own reason -- so which metrics they withhold, and why, is unchanged.
    """
    withheld = withheld_metrics(computed)
    withheld.update(
        {
            key: reason
            for key, reason in tiingo_withheld_metrics(ctx, computed).items()
            if key not in withheld
        }
    )
    return withheld


def estimated_prompt_chars(
    ctx: FilingView,
    computed: list[tuple[str, MetricResult]],
    withheld: Mapping[tuple[str, str], str] | None = None,
) -> int:
    """The characters the LLM path would send for one filing, from its metric results.

    The brief, the synthesis prompt, and every specialist request: a specialist is consulted only
    when its group has a value (an all-empty group answers from the code, with no model call), and
    its request is its system prompt (the skill and the generated blocks) plus the filing's lines.
    This is the *input* size of the requests, not of the accumulating conversation, and it prices an
    estimate -- no tokenizer is run here (``len // 4`` is the usual rough conversion).
    """
    by_group = _group(computed)
    statuses = pillars.pillar_statuses(computed)
    total = len(_brief(ctx, by_group, withheld)) + len(
        synthesis_prompt(pillars.scored_pillars(statuses))
    )
    for group, results in by_group.items():
        if not any(result.value is not None for result in results):
            continue
        paragraph = basis_paragraph(group, results) if ctx.form == "10-Q" else ""
        prompt = "\n\n".join(
            part
            for part in (
                f"{ctx.ticker} ({ctx.company_name}) {ctx.form} {ctx.fiscal_period}",
                paragraph,
                f"{group}:\n" + "\n".join(_metric_lines(group, results, withheld)),
            )
            if part
        )
        total += len(_specialist_system_prompt(group)) + len(prompt)
    return total


def estimated_model_calls(computed: list[tuple[str, MetricResult]]) -> int:
    """The model calls the LLM path would make for one filing, from its metric results.

    One per specialist group with a value, plus the orchestrator's opening call and the synthesis
    call. A repair round and a second tool round are not counted; the master prompt asks for every
    group with signal, so this is the planned count, not a guarantee.
    """
    groups = {group for group, result in computed if result.value is not None}
    return 2 + len(groups)


def _brief(
    ctx: FilingView,
    by_group: dict[str, list[MetricResult]],
    withheld: Mapping[tuple[str, str], str] | None = None,
) -> str:
    parts = [f"Filing: {ctx.ticker} ({ctx.company_name}) {ctx.form} {ctx.fiscal_period}"]
    for group, results in by_group.items():
        if ctx.form == "10-Q":
            paragraph = basis_paragraph(group, results)
            if paragraph:
                parts.append(paragraph)
        parts.append(f"{group}:\n" + "\n".join(_metric_lines(group, results, withheld)))
    return "\n\n".join(parts)


def _round(value: Any) -> Any:
    return round(value, 4) if isinstance(value, float) else value


@dataclass(frozen=True)
class _Parsed:
    """A usable model reply: the pillar ratings and the narrative (D2)."""

    ratings: dict[str, int]
    reasons: dict[str, str]
    narrative: str
    strengths: list[str]
    risks: list[str]


@dataclass(frozen=True)
class _Synthesis:
    assessment: FundamentalAssessment
    # D4: the block stored with the row, inside `score_snapshot.inputs_json`.
    pillars: dict[str, Any]
    used_fallback: bool


# T-113: distinct from any real LLM's own model_id, so a fallback row is never mistaken for
# that model's output in score_snapshot.model.
FALLBACK_MODEL_LABEL = "rule-based-fallback-v1"
# T-073 (D1): a filing whose applicable pillar has no value is not scored at all -- the row records
# why and the member is unscored (T-119). Distinct again, so the row is never read as a model's
# own verdict on a filing the model was not asked about.
UNSCORED_MODEL_LABEL = "unscored-pillar-coverage-v1"


def unscored_narrative(statuses: Mapping[str, pillars.PillarStatus]) -> str:
    """D1's coverage rule: why this filing has no score, naming the applicable empty pillar(s)."""
    empty = pillars.empty_pillars(statuses)
    named = ", ".join(f"{pillar} ({statuses[pillar].empty_reason})" for pillar in empty)
    return (
        "No fundamental score: an applicable pillar has no value (D1's coverage rule, MET-00 -- "
        f"an empty metric is never a neutral reading): {named}. The member is unscored (T-119)."
    )


def _synthesize(
    orchestrator: Agent,
    flat_metrics: dict[str, float | None],
    statuses: Mapping[str, pillars.PillarStatus],
    withheld: Mapping[tuple[str, str], str] | None = None,
) -> _Synthesis:
    """Ask the orchestrator to rate the pillars; fall back to a rule-based score.

    DeepSeek does not currently accept OpenAI ``response_format`` json-schema, so we
    parse free text rather than using ``Agent.structured_output``.
    """
    scored = pillars.scored_pillars(statuses)
    for prompt in (synthesis_prompt(scored), _REPAIR_PROMPT):
        try:
            reply = str(orchestrator(prompt))
        except Exception:  # network/model error -- use the deterministic fallback
            break
        parsed = _coerce(reply, scored)
        if parsed is not None:
            total = pillars.total_from_ratings(parsed.ratings, statuses)
            return _Synthesis(
                FundamentalAssessment(
                    score=total,
                    rating=_rating_for(total),
                    narrative=parsed.narrative,
                    strengths=parsed.strengths,
                    risks=parsed.risks,
                ),
                pillars.score_block(statuses, parsed.ratings, parsed.reasons),
                used_fallback=False,
            )
    return _fallback_synthesis(flat_metrics, statuses, withheld)


def _coerce(text: str, scored: Sequence[str]) -> _Parsed | None:
    """The model's reply, or ``None`` when it is unusable (D2).

    A rating for a pillar the company does not have is **discarded and logged** -- it is never
    scored, which would impute an `empty` pillar (MET-00). What is left must be exactly the pillars
    the code decided this company has; a missing one counts as unparseable and the fallback runs.
    """
    match = _JSON_OBJECT_RE.search(text)
    if match is None:
        return None
    try:
        parsed = SynthesisReply.model_validate_json(match.group(0))
    except ValidationError:
        return None
    wanted = set(scored)
    for extra in sorted(set(parsed.pillars) - wanted):
        log.warning("discarding a rating for a pillar this company does not have: %s", extra)
    rated = {name: value for name, value in parsed.pillars.items() if name in wanted}
    if set(rated) != wanted:
        return None
    return _Parsed(
        ratings={name: value.rating for name, value in rated.items()},
        reasons={name: value.reason for name, value in rated.items()},
        narrative=parsed.narrative,
        strengths=parsed.strengths,
        risks=parsed.risks,
    )


def facts_only_synthesis(
    computed: Sequence[tuple[str, MetricResult]],
    withheld: Mapping[tuple[str, str], str] | None = None,
) -> _Synthesis:
    """D1's coverage rule and D3's fallback, without the model.

    The verdict a filing gets when there is no LLM: the measurement scripts
    (``verify_t148b.py``, ``verify_t149.py``, ``verify_t076.py``) reconstruct a score this way, and
    it is the same path ``analyze`` takes on an unparseable reply. An applicable `empty` pillar
    leaves the score empty here too, exactly as in ``analyze``."""
    statuses = pillars.pillar_statuses(computed)
    if pillars.empty_pillars(statuses):
        return _Synthesis(
            FundamentalAssessment(narrative=unscored_narrative(statuses)),
            pillars.score_block(statuses),
            used_fallback=False,
        )
    flat = {f"{group}.{result.name}": result.value for group, result in computed}
    return _fallback_synthesis(flat, statuses, withheld)


def _fallback_synthesis(
    flat: dict[str, float | None],
    statuses: Mapping[str, pillars.PillarStatus],
    withheld: Mapping[tuple[str, str], str] | None = None,
) -> _Synthesis:
    """D3: the rule-based fallback on the same pillars, with its declared calibrated nudges.

    Each nudge goes to its metric's pillar, so a present pillar's rating is 50 plus and minus its
    own metrics' points; the valuation nudge (`free_cash_flow_yield`) has no pillar and leaves the
    score (valuation is out). The total uses the same weights and rounding as the model's answer,
    and the block stores the same structure, so a fallback row and a model row are read alike."""
    ratings: dict[str, float] = {pillar: 50.0 for pillar in pillars.scored_pillars(statuses)}
    strengths: list[str] = []
    risks: list[str] = []
    # A value a HARD DQ rule quarantines never scores, in the fallback as in the prompt (T-076, D5).
    blocked = set(withheld or {})
    for key, low, high, points in _SCORE_RULES:
        group, short = key.split(".", 1)
        pillar = pillars.PILLAR_OF_METRIC.get(short)
        if pillar is None or pillar not in ratings:
            continue
        if (group, short) in blocked:
            continue
        value = flat.get(key)
        if value is None:
            continue
        if value >= high:
            ratings[pillar] += points
            strengths.append(f"{short} {value:.2f}")
        elif value < low:
            ratings[pillar] -= points
            risks.append(f"{short} {value:.2f}")

    leverage = (
        None if ("leverage", "debt_to_equity") in blocked else flat.get("leverage.debt_to_equity")
    )
    if leverage is not None and leverage > _HIGH_LEVERAGE and "financial_strength" in ratings:
        ratings["financial_strength"] -= _HIGH_LEVERAGE_PENALTY
        risks.append(f"debt_to_equity {leverage:.2f}")

    # D2's declared scale is 0-100 and a rating is an integer: clamp the nudged pillar.
    clamped = {pillar: float(max(0, min(100, round(value)))) for pillar, value in ratings.items()}
    total = pillars.total_from_ratings(clamped, statuses)
    return _Synthesis(
        FundamentalAssessment(
            score=total,
            rating=_rating_for(total),
            narrative=(
                "Automated fallback: the language model could not return a usable JSON "
                "verdict, so this score is derived directly from the computed ratios."
            ),
            strengths=strengths or ["no standout strengths in the computed ratios"],
            risks=risks or ["no standout risks in the computed ratios"],
        ),
        pillars.score_block(statuses, clamped),
        used_fallback=True,
    )


def _rating_for(score: float) -> Literal["bullish", "neutral", "bearish"]:
    if score >= _BULLISH_CUTOFF:
        return "bullish"
    if score >= _NEUTRAL_CUTOFF:
        return "neutral"
    return "bearish"
