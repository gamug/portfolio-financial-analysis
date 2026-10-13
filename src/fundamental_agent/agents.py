"""Strands agent layer: a metrics-master orchestrator over per-group specialists.

Deterministic ratio math lives in :mod:`fundamental_agent.metrics`; the agents only
*interpret* those numbers. The orchestrator decides which specialist agents to consult
for a given filing, then a synthesis step turns the readings into a scored assessment.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from strands import Agent, tool
from strands.models.openai import OpenAIModel

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
from fundamental_agent.skills import group_applicability, group_definitions, load_skill
from fundamental_agent.statements import Statements
from kg_schema.market_cap import MarketCap

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_BULLISH_CUTOFF = 60.0
_NEUTRAL_CUTOFF = 40.0
_HIGH_LEVERAGE = 3.0
_HIGH_LEVERAGE_PENALTY = 12.0

_ALL_GROUPS = (*CORE_GROUPS, *OPTIONAL_GROUPS)
# valuation needs a market price, so it is computed outside the uniform group loop;
# it still gets a specialist tool whenever a period-end close was resolved.
_SPECIALIST_GROUPS = (*_ALL_GROUPS, valuation_metrics.GROUP)

MASTER_PROMPT = """You are the metrics master for a fundamental equity analysis.
You are given the deterministically computed financial ratios for one SEC filing.
Call the specialist tool for every metric group that carries signal for this company
-- always consult profitability, liquidity, leverage and cash flow; add efficiency,
growth, roic and cagr when their numbers are meaningful (skip inventory-style metrics
for financial firms, skip growth/cagr when there is no multi-period comparison); add
valuation whenever free-cash-flow-yield numbers are present.
After gathering the readings, give a short synthesis in plain language."""

# The fixed lead of every specialist's system prompt (T-076, D2): no recompute, and the company type comes
# from the code's own `not_applicable` reason, never from the model (APP-00, T-071).
_SPECIALIST_LEAD = (
    "You are the {group} specialist in a fundamental-analysis pipeline. The numbers are already "
    "computed for you -- do not recompute them and do not ask for filing text. Use only the numbers "
    "given. Give a concise 1-2 sentence assessment for the orchestrator, applying any caveat the "
    "procedure below would raise. The brief carries no company type: an empty metric whose reason is "
    "`not_applicable` is the code's own decision that the metric does not apply to this company, so read "
    "that reason as the company type -- never reclassify the company yourself."
)


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
Reply with ONLY a raw JSON object -- no prose, no markdown fences -- with these keys:
  "score": number 0-100 (50 = an average S&P 500 company, higher = fundamentally stronger)
  "rating": one of "bullish" (score >= 60), "neutral" (40-59), "bearish" (< 40)
  "narrative": string, 3-5 sentences citing the specific ratios that drove the score
  "strengths": array of 2-4 short phrases
  "risks": array of 2-4 short phrases
Base everything only on the metrics and specialist readings gathered above."""

_REPAIR_PROMPT = (
    "That was not valid JSON. Reply again with ONLY the JSON object described above, nothing else."
)

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


class FundamentalAssessment(BaseModel):
    """The LLM's verdict on one filing -- persisted as an immutable snapshot."""

    score: float = Field(ge=0.0, le=100.0)
    rating: Literal["bullish", "neutral", "bearish"]
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


class FundamentalAnalyst:
    """Runs the orchestrator + synthesis for one filing at a time."""

    def __init__(self, model: OpenAIModel | None, model_name: str) -> None:
        # ``model`` is None for the deterministic path (``_compute_all`` and the recompute script,
        # which never call the LLM); ``analyze`` needs a real model.
        self.prompt_hash = _prompt_version_hash(model_name)
        self._model = model
        self.model_name = model_name

    def analyze(self, ctx: FilingContext) -> AnalysisResult:
        computed = self._compute_all(ctx)
        by_group = _group(computed)
        orchestrator = Agent(
            model=self._model,
            system_prompt=MASTER_PROMPT,
            callback_handler=None,
            tools=[
                self._make_specialist(name, by_group.get(name, []), ctx)
                for name in _SPECIALIST_GROUPS
            ],
        )
        orchestrator(_brief(ctx, by_group))
        flat = {f"{g}.{r.name}": r.value for g, r in computed}
        synthesis = _synthesize(orchestrator, flat)
        return AnalysisResult(
            assessment=synthesis.assessment,
            metrics=computed,
            flat_metrics=flat,
            used_fallback=synthesis.used_fallback,
            prompt_hash=self.prompt_hash,
        )

    def _compute_all(self, ctx: FilingContext) -> list[tuple[str, MetricResult]]:
        out: list[tuple[str, MetricResult]] = []
        for group in _ALL_GROUPS:
            for result in compute_group(
                group, ctx.stmts, ctx.period_key, ctx.prior_key, _group_ttm(group, ctx)
            ):
                out.append((group, result))
        if (
            ctx.price is not None
            or ctx.market_cap is not None
            or ctx.market_cap_missing is not None
        ):
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
            ):
                out.append((valuation_metrics.GROUP, result))

        _flag_annualization(out, ctx.ttm_flows)
        # T-151 part 3 (D14): the provenance block on every metric, in one place (G2: the TTM
        # method and columns come from the filing's own flows).
        provenance.attach(out, ctx.stmts, ctx.period_key, ctx.ttm_flows)
        return out

    def _make_specialist(self, group: str, results: list[MetricResult], ctx: FilingContext) -> Any:
        numbers = {r.name: r.value for r in results if r.value is not None}
        model = self._model

        @tool(name=f"{group}_specialist")
        def specialist() -> str:
            """Get a specialist reading of this metric group for the filing."""
            if not numbers:
                return f"No {group} metrics could be computed for this filing."
            reader = Agent(
                model=model,
                system_prompt=_specialist_system_prompt(group),
                callback_handler=None,
            )
            prompt = (
                f"{ctx.ticker} ({ctx.company_name}) {ctx.form} {ctx.fiscal_period}. "
                f"{group} metrics: {json.dumps(numbers, default=_round)}."
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


def _brief(ctx: FilingContext, by_group: dict[str, list[MetricResult]]) -> str:
    payload = {
        group: {r.name: r.value for r in results if r.value is not None}
        for group, results in by_group.items()
    }
    return (
        f"Filing: {ctx.ticker} ({ctx.company_name}) {ctx.form} {ctx.fiscal_period}\n"
        f"Computed metrics (nulls omitted):\n{json.dumps(payload, indent=2, default=_round)}"
    )


def _round(value: Any) -> Any:
    return round(value, 4) if isinstance(value, float) else value


@dataclass(frozen=True)
class _Synthesis:
    assessment: FundamentalAssessment
    used_fallback: bool


# T-113: distinct from any real LLM's own model_id, so a fallback row is never mistaken for
# that model's output in score_snapshot.model.
FALLBACK_MODEL_LABEL = "rule-based-fallback-v1"


def _synthesize(orchestrator: Agent, flat_metrics: dict[str, float | None]) -> _Synthesis:
    """Ask the orchestrator for a JSON verdict; fall back to a rule-based score.

    DeepSeek does not currently accept OpenAI ``response_format`` json-schema, so we
    parse free text rather than using ``Agent.structured_output``.
    """
    for prompt in (_SYNTHESIS_PROMPT, _REPAIR_PROMPT):
        try:
            reply = str(orchestrator(prompt))
        except Exception:  # network/model error -- use the deterministic fallback
            break
        parsed = _coerce(reply)
        if parsed is not None:
            return _Synthesis(parsed, used_fallback=False)
    return _Synthesis(_fallback_assessment(flat_metrics), used_fallback=True)


def _coerce(text: str) -> FundamentalAssessment | None:
    match = _JSON_OBJECT_RE.search(text)
    if match is None:
        return None
    try:
        parsed = FundamentalAssessment.model_validate_json(match.group(0))
    except ValidationError:
        return None
    # keep rating consistent with the score band the prompt defines.
    return FundamentalAssessment(
        score=parsed.score,
        rating=_rating_for(parsed.score),
        narrative=parsed.narrative,
        strengths=parsed.strengths,
        risks=parsed.risks,
    )


def _fallback_assessment(flat: dict[str, float | None]) -> FundamentalAssessment:
    score = 50.0
    strengths: list[str] = []
    risks: list[str] = []
    for key, low, high, points in _SCORE_RULES:
        value = flat.get(key)
        if value is None:
            continue
        short = key.split(".", 1)[1]
        if value >= high:
            score += points
            strengths.append(f"{short} {value:.2f}")
        elif value < low:
            score -= points
            risks.append(f"{short} {value:.2f}")

    leverage = flat.get("leverage.debt_to_equity")
    if leverage is not None and leverage > _HIGH_LEVERAGE:
        score -= _HIGH_LEVERAGE_PENALTY
        risks.append(f"debt_to_equity {leverage:.2f}")

    score = max(0.0, min(100.0, score))
    return FundamentalAssessment(
        score=score,
        rating=_rating_for(score),
        narrative=(
            "Automated fallback: the language model could not return a usable JSON "
            "verdict, so this score is derived directly from the computed ratios."
        ),
        strengths=strengths or ["no standout strengths in the computed ratios"],
        risks=risks or ["no standout risks in the computed ratios"],
    )


def _rating_for(score: float) -> Literal["bullish", "neutral", "bearish"]:
    if score >= _BULLISH_CUTOFF:
        return "bullish"
    if score >= _NEUTRAL_CUTOFF:
        return "neutral"
    return "bearish"
