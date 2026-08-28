"""Strands agent layer: a metrics-master orchestrator over per-group specialists.

Deterministic ratio math lives in :mod:`fundamental_agent.metrics`; the agents only
*interpret* those numbers. The orchestrator decides which specialist agents to consult
for a given filing, then a synthesis step turns the readings into a scored assessment.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from strands import Agent, tool
from strands.models.openai import OpenAIModel

from fundamental_agent.config import Settings
from fundamental_agent.metrics import CORE_GROUPS, OPTIONAL_GROUPS, MetricResult, compute_group
from fundamental_agent.skills import load_skill
from fundamental_agent.statements import Statements

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_BULLISH_CUTOFF = 60.0
_NEUTRAL_CUTOFF = 40.0
_HIGH_LEVERAGE = 3.0
_HIGH_LEVERAGE_PENALTY = 12.0

_ALL_GROUPS = (*CORE_GROUPS, *OPTIONAL_GROUPS)

MASTER_PROMPT = """You are the metrics master for a fundamental equity analysis.
You are given the deterministically computed financial ratios for one SEC filing.
Call the specialist tool for every metric group that carries signal for this company
-- always consult profitability, liquidity, leverage and cash flow; add efficiency,
growth, roic and cagr when their numbers are meaningful (skip inventory-style metrics
for financial firms, skip growth/cagr when there is no multi-period comparison).
After gathering the readings, give a short synthesis in plain language."""

# Short fallback briefs, used when a group has no skills/<name>/SKILL.md SOP mapped.
_INLINE_SPECIALIST_PROMPTS = {
    "profitability": "You read profitability ratios (margins, ROA, ROE). "
    "State whether profitability is strong, adequate or weak and why.",
    "liquidity": "You read liquidity ratios (current, quick, cash). "
    "State whether short-term solvency looks comfortable or tight.",
    "leverage": "You read leverage ratios (debt/equity, debt/assets, interest coverage, "
    "net debt/EBITDA). State whether the balance sheet is conservative or stretched.",
    "cashflow": "You read cash-flow quality (operating cash flow margin, free cash flow "
    "margin and conversion, capex intensity). State whether cash generation backs earnings.",
    "efficiency": "You read efficiency ratios (asset, inventory, receivables turnover). "
    "State whether the asset base is used efficiently.",
    "growth": "You read year-over-year growth (revenue, operating income, net income, free "
    "cash flow). State whether the trajectory is improving or deteriorating.",
    "roic": "You read return on invested capital and the effective tax rate. "
    "State whether the company earns above its cost of capital.",
    "cagr": "You read multi-year compound annual growth rates (revenue, net income, "
    "operating cash flow). State whether long-run growth is strong, flat or declining.",
}


def _specialist_system_prompt(group: str) -> str:
    """Build a specialist's system prompt from its SKILL.md SOP when one exists."""
    lead = (
        f"You are the {group} specialist in a fundamental-analysis pipeline. The ratios "
        "are already computed for you -- do not recompute them and do not ask for filing "
        "text. Give a concise 1-2 sentence assessment for the orchestrator, applying any "
        "caveat the procedure below would raise (negative base, interest netting, "
        "unclassified balance sheet, lease/SBC distortions)."
    )
    sop = load_skill(group)
    if sop is None:
        return f"{lead}\n\n{_INLINE_SPECIALIST_PROMPTS[group]}"
    return f"{lead}\n\n--- STANDARD OPERATING PROCEDURE: {group} ---\n{sop}"


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


@dataclass(frozen=True)
class AnalysisResult:
    assessment: FundamentalAssessment
    metrics: list[tuple[str, MetricResult]]  # (group, result), value may be None
    flat_metrics: dict[str, float | None]  # "group.name" -> value


def build_model(settings: Settings) -> OpenAIModel:
    """Point Strands' OpenAI provider at the configured (DeepSeek) endpoint."""
    return OpenAIModel(
        client_args={"api_key": settings.llm_api_key, "base_url": settings.llm_url},
        model_id=settings.llm_model,
        params={"temperature": 0.2, "max_tokens": 1500},
    )


class FundamentalAnalyst:
    """Runs the orchestrator + synthesis for one filing at a time."""

    def __init__(self, model: OpenAIModel, model_name: str) -> None:
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
                self._make_specialist(name, by_group.get(name, []), ctx) for name in _ALL_GROUPS
            ],
        )
        orchestrator(_brief(ctx, by_group))
        flat = {f"{g}.{r.name}": r.value for g, r in computed}
        assessment = _synthesize(orchestrator, flat)
        return AnalysisResult(assessment=assessment, metrics=computed, flat_metrics=flat)

    def _compute_all(self, ctx: FilingContext) -> list[tuple[str, MetricResult]]:
        out: list[tuple[str, MetricResult]] = []
        for group in _ALL_GROUPS:
            for result in compute_group(group, ctx.stmts, ctx.period_key, ctx.prior_key):
                out.append((group, result))
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


def _synthesize(
    orchestrator: Agent, flat_metrics: dict[str, float | None]
) -> FundamentalAssessment:
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
            return parsed
    return _fallback_assessment(flat_metrics)


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
