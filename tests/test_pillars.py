"""T-073 (D1/D2): the four pillars, the statuses the code computes, the model's answer and the total.

D1's statuses are the code's, never the model's; D2's total is the code's too -- the model only rates
the pillars it is asked about. Every fixture here is built from `MetricResult`s, so the tests read
the same audit inputs the metrics layer writes.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from fundamental_agent import agents, pillars
from fundamental_agent.agents import FilingContext, FundamentalAnalyst
from fundamental_agent.metrics import CORE_GROUPS, OPTIONAL_GROUPS, provenance
from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.skills import GROUP_METRICS
from fundamental_agent.statements import Statements

# -- the pillar membership (D1) --------------------------------------------------------------------


def test_every_pillar_metric_is_a_computed_metric() -> None:
    """A pillar names the code's own metrics: no invented member, no renamed one."""
    computed = {name for group in (*CORE_GROUPS, *OPTIONAL_GROUPS) for name in GROUP_METRICS[group]}
    members = {name for names in pillars.PILLARS.values() for name in names}
    assert members <= computed, sorted(members - computed)


def test_the_building_blocks_and_the_valuation_group_are_not_pillar_members() -> None:
    """D1: `ebitda`, `nopat`, `effective_tax_rate` and the robustness ROIC variant are inputs and
    derivations beside the ratios, and valuation is out of the score (value is VALORIZATION's own
    stored component). None of them may become a pillar member."""
    members = {name for names in pillars.PILLARS.values() for name in names}
    excluded = {
        "ebitda",
        "nopat",
        "effective_tax_rate",
        "return_on_invested_capital_effective_rate",
        *GROUP_METRICS["valuation"],
    }
    assert not (members & excluded), sorted(members & excluded)


def test_the_four_pillars_are_the_four_planned_groups() -> None:
    assert pillars.PILLARS["profitability"] == (
        "gross_margin",
        "operating_margin",
        "net_margin",
        "return_on_assets",
        "return_on_equity",
        "return_on_invested_capital",
        "asset_turnover",
        "inventory_turnover",
        "receivables_turnover",
    )
    assert pillars.PILLARS["financial_strength"] == (
        "debt_to_equity",
        "debt_to_assets",
        "interest_coverage",
        "net_debt_to_ebitda",
        "current_ratio",
        "quick_ratio",
        "cash_ratio",
    )
    assert pillars.PILLARS["cash_flow"] == (
        "operating_cash_flow_margin",
        "free_cash_flow_margin",
        "free_cash_flow_conversion",
        "capex_intensity",
    )
    assert pillars.PILLARS["growth"] == (
        "revenue_growth",
        "operating_income_growth",
        "net_income_growth",
        "free_cash_flow_growth",
        "revenue_cagr",
        "net_income_cagr",
        "operating_cash_flow_cagr",
    )


def test_the_liquidity_metrics_are_app_09s_three() -> None:
    """APP-09 names exactly current, quick and cash: the only metrics an `absent_by_form` pillar
    may drop. Pinned to the metrics that cite APP-09, so a new APP-09 metric cannot silently join
    them and one of the three cannot leave."""
    app09 = {name for name, rules in provenance.RULE_IDS.items() if "APP-09" in rules}
    assert app09 == set(pillars.LIQUIDITY_METRICS)


# -- the statuses (D1) -----------------------------------------------------------------------------


def _metric(
    name: str,
    value: float | None = 1.0,
    *,
    not_applicable: bool = False,
    reason: str | None = None,
    inputs: dict[str, Any] | None = None,
) -> MetricResult:
    audit: dict[str, Any] = dict(inputs or {})
    if not_applicable:
        audit[f"flag_{name}_not_applicable"] = 1.0
    if reason is not None:
        audit["provenance"] = {"empty_reason": reason}
    return MetricResult(name, value, "ratio", audit)


def _results(**overrides: Any) -> list[tuple[str, MetricResult]]:
    """Every pillar metric with a value, then *overrides* keyed by metric name.

    ``classified=False`` drops `AssetsCurrent` / `LiabilitiesCurrent` from the liquidity inputs
    (APP-09's form signal)."""
    classified: bool = overrides.pop("classified", True)
    out: list[tuple[str, MetricResult]] = []
    for pillar, names in pillars.PILLARS.items():
        for name in names:
            spec = overrides.get(name, {})
            inputs = spec.get("inputs")
            if name == "current_ratio" and inputs is None:
                inputs = (
                    {"current_assets": 100.0, "current_liabilities": 50.0} if classified else {}
                )
            out.append(
                (
                    pillar,
                    _metric(
                        name,
                        spec.get("value", 1.0),
                        not_applicable=spec.get("not_applicable", False),
                        reason=spec.get("reason"),
                        inputs=inputs,
                    ),
                )
            )
    return out


def _statuses(**kwargs: Any) -> dict[str, pillars.PillarStatus]:
    return pillars.pillar_statuses(_results(**kwargs))


def test_a_full_filing_is_present_on_every_pillar() -> None:
    statuses = _statuses()
    assert {p: s.status for p, s in statuses.items()} == {
        "profitability": pillars.PRESENT,
        "financial_strength": pillars.PRESENT,
        "cash_flow": pillars.PRESENT,
        "growth": pillars.PRESENT,
    }
    assert pillars.scored_pillars(statuses) == tuple(pillars.PILLARS)


def test_an_applicable_pillar_with_no_value_is_empty_with_its_metrics_reasons() -> None:
    """D1's `empty`: every remaining metric is empty for another reason. The pillar records the
    distinct reasons, so a reader can tell a missing input from a refused capture."""
    statuses = _statuses(
        debt_to_equity={"value": None, "reason": "capture_failure"},
        debt_to_assets={"value": None, "reason": "capture_failure"},
        interest_coverage={"value": None, "reason": "missing_input"},
        net_debt_to_ebitda={"value": None, "reason": "missing_input"},
        current_ratio={"value": None, "reason": "missing_input"},
        quick_ratio={"value": None, "reason": "missing_input"},
        cash_ratio={"value": None, "reason": "missing_input"},
    )
    fs = statuses["financial_strength"]
    assert fs.status == pillars.EMPTY
    assert fs.empty_reason == "capture_failure,missing_input"
    assert pillars.empty_pillars(statuses) == ("financial_strength",)


def test_every_member_metric_na_is_not_applicable() -> None:
    """A bank's leverage ratios are all NA (APP-02): the pillar is not the company's, and MET-00
    allows exactly that -- a structural absence is declared by company type."""
    statuses = _statuses(
        **{
            name: {"value": None, "not_applicable": True, "reason": "not_applicable"}
            for name in pillars.PILLARS["financial_strength"]
        }
    )
    assert statuses["financial_strength"].status == pillars.NOT_APPLICABLE
    assert statuses["financial_strength"].metrics == ()
    assert pillars.scored_pillars(statuses) == ("profitability", "cash_flow", "growth")


def test_the_liquidity_ratios_drop_out_on_an_unclassified_balance_sheet() -> None:
    """D1's `absent_by_form`: APP-09's three drop out and the pillar rests on the leverage ratios."""
    statuses = _statuses(classified=False)
    fs = statuses["financial_strength"]
    assert fs.status == pillars.ABSENT_BY_FORM
    assert set(fs.metrics) == set(pillars.PILLARS["financial_strength"]) - set(
        pillars.LIQUIDITY_METRICS
    )
    assert fs.has_a_reading


def test_an_unclassified_balance_sheet_whose_leverage_is_empty_is_empty() -> None:
    """The REITs and homebuilders of the plan's 18: the three drop out and the leverage ratios have
    no value either, so the pillar is `empty` -- an applicable pillar with no value, which leaves
    the score empty (D1's coverage rule)."""
    statuses = _statuses(
        classified=False,
        **{
            name: {"value": None, "reason": "missing_input"}
            for name in pillars.PILLARS["financial_strength"]
        },
    )
    assert statuses["financial_strength"].status == pillars.EMPTY
    assert statuses["financial_strength"].empty_reason == "missing_input"


def test_an_unclassified_balance_sheet_whose_only_applicable_metrics_were_the_three_is_na() -> None:
    """An asset manager (`other_financial`): APP-02 makes the four leverage ratios NA, so once the
    three liquidity ratios drop out the pillar has no applicable metric left -- `not_applicable`,
    not `empty`."""
    statuses = _statuses(
        classified=False,
        **{
            name: {"value": None, "not_applicable": True, "reason": "not_applicable"}
            for name in (
                "debt_to_equity",
                "debt_to_assets",
                "interest_coverage",
                "net_debt_to_ebitda",
            )
        },
    )
    assert statuses["financial_strength"].status == pillars.NOT_APPLICABLE


def test_absent_by_form_touches_no_other_pillar() -> None:
    """APP-09 is `financial_strength`'s rule alone: an unclassified balance sheet leaves the other
    three pillars present."""
    statuses = _statuses(classified=False)
    assert statuses["profitability"].status == pillars.PRESENT
    assert statuses["cash_flow"].status == pillars.PRESENT
    assert statuses["growth"].status == pillars.PRESENT


def test_the_form_signal_is_the_filing_not_md_38s_flag() -> None:
    """D1: `AssetsCurrent` / `LiabilitiesCurrent` are not filed. MD-38's
    `unclassified_balance_sheet` flag is a consequence signal -- it also fires when a working-capital
    term is refused -- so a filing carrying it with both current items present is classified."""
    results = _results()
    index = {result.name: result for _g, result in results}
    index["enterprise_fcf_yield"] = MetricResult(
        "enterprise_fcf_yield",
        None,
        "ratio",
        {"flag_enterprise_fcf_yield_unclassified_balance_sheet": 1.0},
    )
    statuses = pillars.pillar_statuses(results)
    assert statuses["financial_strength"].status == pillars.PRESENT


# -- the total (D2) --------------------------------------------------------------------------------


def test_the_total_is_the_weighted_pillar_ratings() -> None:
    statuses = _statuses()
    ratings = {"profitability": 80, "financial_strength": 60, "cash_flow": 40, "growth": 20}
    assert pillars.total_from_ratings(ratings, statuses) == 50.0


def test_a_not_applicable_pillar_leaves_the_total_on_its_declared_scale() -> None:
    """D2 declares the scale ("50 = the pillar of an average S&P 500 company"). A company with two
    pillars is an average company when both are 50 -- so the weights normalize over the pillars the
    company has, rather than scoring the absent one as 0 (which MET-00 forbids)."""
    statuses = _statuses(
        **{
            name: {"value": None, "not_applicable": True, "reason": "not_applicable"}
            for name in (*pillars.PILLARS["financial_strength"], *pillars.PILLARS["cash_flow"])
        }
    )
    assert pillars.scored_pillars(statuses) == ("profitability", "growth")
    assert pillars.total_from_ratings({"profitability": 50, "growth": 50}, statuses) == 50.0
    assert pillars.total_from_ratings({"profitability": 100, "growth": 100}, statuses) == 100.0


def test_the_total_is_rounded_to_one_decimal() -> None:
    statuses = _statuses()
    ratings = {"profitability": 71, "financial_strength": 72, "cash_flow": 73, "growth": 74}
    assert pillars.total_from_ratings(ratings, statuses) == 72.5
    assert (
        pillars.total_from_ratings(
            {"profitability": 71, "financial_strength": 71, "cash_flow": 71, "growth": 71}, statuses
        )
        == 71.0
    )


def test_the_weights_are_equal_by_default_and_versioned() -> None:
    assert pillars.DEFAULT_PILLAR_WEIGHTS == {
        "profitability": 0.25,
        "financial_strength": 0.25,
        "cash_flow": 0.25,
        "growth": 0.25,
    }
    assert pillars.PILLAR_WEIGHTS_VERSION


def test_the_block_records_the_statuses_the_weights_and_the_coverage() -> None:
    """D4: what the row stores. The flat metrics keep their own keys beside it."""
    statuses = _statuses()
    block = pillars.score_block(
        statuses,
        {"profitability": 80, "financial_strength": 60, "cash_flow": 40, "growth": 20},
        {
            "profitability": "margins",
            "financial_strength": "leverage",
            "cash_flow": "cash",
            "growth": "top line",
        },
    )
    assert set(block) == {"pillars", "pillar_weights", "pillar_weights_version", "coverage"}
    assert block["pillars"]["profitability"] == {
        "status": "present",
        "metrics": list(pillars.PILLARS["profitability"]),
        "rating": 80,
        "reason": "margins",
        "weight": 0.25,
    }
    assert block["pillar_weights"] == pillars.DEFAULT_PILLAR_WEIGHTS
    assert block["pillar_weights_version"] == pillars.PILLAR_WEIGHTS_VERSION
    assert block["coverage"] == {
        "pillars": 4,
        "scored": ["profitability", "financial_strength", "cash_flow", "growth"],
        "empty": [],
        "absent_by_form": [],
        "not_applicable": [],
    }


def test_an_empty_pillar_stores_its_reason_and_no_rating() -> None:
    statuses = _statuses(
        **{
            name: {"value": None, "reason": "missing_input"}
            for name in pillars.PILLARS["cash_flow"]
        }
    )
    block = pillars.score_block(
        statuses, {"profitability": 50, "financial_strength": 50, "growth": 50}
    )
    cash = block["pillars"]["cash_flow"]
    assert cash["status"] == "empty"
    assert cash["empty_reason"] == "missing_input"
    assert "rating" not in cash
    assert block["coverage"]["empty"] == ["cash_flow"]


# -- the model's answer (D2) -----------------------------------------------------------------------


def _reply(ratings: dict[str, int], **extra: Any) -> str:
    body: dict[str, Any] = {
        "pillars": {
            name: {"rating": value, "reason": f"{name} reason"} for name, value in ratings.items()
        },
        "narrative": "a narrative",
        "strengths": ["s"],
        "risks": ["r"],
    }
    body.update(extra)
    return json.dumps(body)


def _orchestrator(text: str) -> Any:
    """A stand-in for the Strands agent: the synthesis only calls it with a prompt."""
    return lambda _prompt: text


def test_the_prompt_names_exactly_the_scored_pillars() -> None:
    prompt = agents.synthesis_prompt(("profitability", "growth"))
    assert '"profitability", "growth"' in prompt
    assert '"cash_flow"' not in prompt
    assert "integer 0-100" in prompt


def test_a_correct_reply_is_totalled_by_the_code() -> None:
    statuses = _statuses()
    synthesis = agents._synthesize(
        _orchestrator(
            _reply({"profitability": 80, "financial_strength": 60, "cash_flow": 40, "growth": 20})
        ),
        {},
        statuses,
    )
    assert synthesis.used_fallback is False
    assert synthesis.assessment.score == 50.0
    assert synthesis.assessment.rating == "neutral"
    assert synthesis.assessment.narrative == "a narrative"
    assert synthesis.pillars["pillars"]["profitability"]["rating"] == 80


def test_prose_around_the_json_is_still_parsed() -> None:
    """The existing free-text extraction stays (DeepSeek takes no `response_format`)."""
    statuses = _statuses()
    text = "Here is my answer:\n" + _reply({p: 60 for p in pillars.PILLARS}) + "\nHope it helps."
    synthesis = agents._synthesize(_orchestrator(text), {}, statuses)
    assert synthesis.used_fallback is False
    assert synthesis.assessment.score == 60.0


def test_a_missing_pillar_falls_back() -> None:
    statuses = _statuses()
    reply = _reply({"profitability": 80, "financial_strength": 60, "cash_flow": 40})
    synthesis = agents._synthesize(_orchestrator(reply), {}, statuses)
    assert synthesis.used_fallback is True
    assert synthesis.assessment.score is not None  # the fallback still scores (FR-002)


def test_an_extra_pillar_is_discarded_and_the_reply_is_still_used() -> None:
    """D2: a rating for a pillar the company does not have is discarded and logged -- never scored,
    which would impute an `empty` pillar (MET-00)."""
    statuses = _statuses(
        **{
            name: {"value": None, "not_applicable": True, "reason": "not_applicable"}
            for name in pillars.PILLARS["cash_flow"]
        }
    )
    reply = _reply({"profitability": 80, "financial_strength": 60, "growth": 20, "cash_flow": 99})
    synthesis = agents._synthesize(_orchestrator(reply), {}, statuses)
    assert synthesis.used_fallback is False
    assert synthesis.assessment.score == 53.3  # (80 + 60 + 20) / 3
    assert "rating" not in synthesis.pillars["pillars"]["cash_flow"]
    assert synthesis.pillars["pillars"]["profitability"]["rating"] == 80


def test_a_non_integer_rating_falls_back() -> None:
    statuses = _statuses()
    reply = _reply({p: 60 for p in pillars.PILLARS}).replace('"rating": 60', '"rating": 60.5', 1)
    assert agents._synthesize(_orchestrator(reply), {}, statuses).used_fallback is True


def test_a_rating_out_of_range_falls_back() -> None:
    statuses = _statuses()
    reply = _reply({p: 60 for p in pillars.PILLARS}).replace('"rating": 60', '"rating": 150', 1)
    assert agents._synthesize(_orchestrator(reply), {}, statuses).used_fallback is True


def test_an_unparseable_reply_reaches_the_fallback() -> None:
    statuses = _statuses()
    synthesis = agents._synthesize(_orchestrator("I cannot answer that."), {}, statuses)
    assert synthesis.used_fallback is True
    assert synthesis.assessment.score == 50.0
    assert synthesis.assessment.rating == "neutral"


def test_a_dead_model_reaches_the_fallback() -> None:
    def _raise(_prompt: str) -> Any:
        raise RuntimeError("network is down")

    statuses = _statuses()
    synthesis = agents._synthesize(cast("Any", _raise), {}, statuses)
    assert synthesis.used_fallback is True
    assert synthesis.assessment.score == 50.0


# -- D1's coverage rule, end to end -----------------------------------------------------------------


def test_an_empty_applicable_pillar_leaves_the_score_empty(
    monkeypatch: pytest.MonkeyPatch, aapl_10k: Statements
) -> None:
    """D1: the score is formed only when every applicable pillar is present. An applicable `empty`
    pillar leaves it empty -- the model is not asked, the row records why, and the member is
    unscored (T-119)."""
    analyst = FundamentalAnalyst(None, "deepseek-chat")
    results = _results(
        **{
            name: {"value": None, "reason": "missing_input"}
            for name in pillars.PILLARS["financial_strength"]
        }
    )
    monkeypatch.setattr(analyst, "_compute_all", lambda _ctx: results)
    ctx = FilingContext("AAPL", "Apple", "10-K", "FY2023", aapl_10k, "2023-09-30 (FY)", None)
    out = analyst.analyze(ctx)
    assert out.assessment.score is None
    assert out.assessment.rating is None
    assert out.used_fallback is False  # nothing was attempted: there is nothing to rate
    assert "financial_strength" in out.assessment.narrative
    assert out.pillars["coverage"]["empty"] == ["financial_strength"]
    assert out.pillars["pillars"]["financial_strength"]["status"] == "empty"


class _StubAgent:
    """A stand-in for the Strands orchestrator: the test drives `_synthesize` itself."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __call__(self, _prompt: str) -> str:
        return ""


def test_a_not_applicable_pillar_still_scores(
    monkeypatch: pytest.MonkeyPatch, aapl_10k: Statements
) -> None:
    """A structural absence does not block the score: the model rates the pillars the company has."""
    analyst = FundamentalAnalyst(None, "deepseek-chat")
    results = _results(
        **{
            name: {"value": None, "not_applicable": True, "reason": "not_applicable"}
            for name in pillars.PILLARS["cash_flow"]
        }
    )
    monkeypatch.setattr(analyst, "_compute_all", lambda _ctx: results)
    seen: list[str] = []

    def _fake_synthesize(orchestrator: Any, flat: Any, statuses: Any, withheld: Any = None) -> Any:
        seen.append("called")
        return agents._Synthesis(
            agents.FundamentalAssessment(score=50.0, rating="neutral", narrative="n"),
            pillars.score_block(statuses),
            used_fallback=False,
        )

    monkeypatch.setattr(agents, "_synthesize", _fake_synthesize)
    # the orchestrator is built before `_synthesize` is called: a stub keeps this hermetic
    monkeypatch.setattr(agents, "Agent", _StubAgent)
    ctx = FilingContext("AAPL", "Apple", "10-K", "FY2023", aapl_10k, "2023-09-30 (FY)", None)
    out = analyst.analyze(ctx)
    assert seen == ["called"]
    assert out.assessment.score == 50.0
    assert out.pillars["coverage"]["not_applicable"] == ["cash_flow"]


# -- D3: the rule-based fallback on the same pillars ------------------------------------------------


def test_the_fallback_rates_each_pillar_fifty_plus_its_own_nudges() -> None:
    """D3: each nudge goes to its metric's pillar. `net_margin` (12 points, profitability) and
    `current_ratio` (6, financial_strength) lift their own pillars and leave the other two at 50."""
    statuses = _statuses()
    synthesis = agents._fallback_synthesis(
        {"profitability.net_margin": 0.20, "liquidity.current_ratio": 1.6}, statuses
    )
    ratings = {p: e["rating"] for p, e in synthesis.pillars["pillars"].items() if "rating" in e}
    assert ratings == {
        "profitability": 62,
        "financial_strength": 56,
        "cash_flow": 50,
        "growth": 50,
    }
    assert synthesis.used_fallback is True
    assert synthesis.assessment.score == 54.5  # (62 + 56 + 50 + 50) / 4
    assert synthesis.assessment.rating == "neutral"


def test_a_low_metric_takes_its_points_off_its_own_pillar() -> None:
    statuses = _statuses()
    synthesis = agents._fallback_synthesis({"growth.revenue_growth": -0.05}, statuses)
    ratings = {p: e["rating"] for p, e in synthesis.pillars["pillars"].items() if "rating" in e}
    assert ratings["growth"] == 42
    assert ratings["profitability"] == 50
    assert synthesis.assessment.score == 48.0
    assert any("revenue_growth" in risk for risk in synthesis.assessment.risks)


def test_the_valuation_nudge_leaves_the_score() -> None:
    """D3: valuation is out of the score, so its nudge has no pillar and moves nothing."""
    statuses = _statuses()
    synthesis = agents._fallback_synthesis({"valuation.free_cash_flow_yield": 0.06}, statuses)
    ratings = {p: e["rating"] for p, e in synthesis.pillars["pillars"].items() if "rating" in e}
    assert set(ratings.values()) == {50}
    assert synthesis.assessment.score == 50.0


def test_the_high_leverage_penalty_hits_financial_strength() -> None:
    statuses = _statuses()
    synthesis = agents._fallback_synthesis({"leverage.debt_to_equity": 4.0}, statuses)
    ratings = {p: e["rating"] for p, e in synthesis.pillars["pillars"].items() if "rating" in e}
    assert ratings["financial_strength"] == 38
    assert ratings["profitability"] == 50


def test_a_pillar_the_company_does_not_have_is_never_rated_by_the_fallback() -> None:
    """D3: a pillar that is `not_applicable` gets nothing -- the fallback rates the same present
    pillars the model would, and the total stays on the declared scale."""
    statuses = _statuses(
        **{
            name: {"value": None, "not_applicable": True, "reason": "not_applicable"}
            for name in pillars.PILLARS["cash_flow"]
        }
    )
    synthesis = agents._fallback_synthesis({"profitability.net_margin": 0.20}, statuses)
    assert "rating" not in synthesis.pillars["pillars"]["cash_flow"]
    assert synthesis.assessment.score == 54.0  # (62 + 50 + 50) / 3


def test_the_fallback_stores_the_same_block_shape_as_the_model() -> None:
    """D4: a fallback row and a model row are read alike -- same keys, same weights, same coverage."""
    statuses = _statuses()
    fallback = agents._fallback_synthesis({}, statuses)
    model = agents._synthesize(
        _orchestrator(_reply({p: 50 for p in pillars.PILLARS})), {}, statuses
    )
    assert set(fallback.pillars) == set(model.pillars)
    assert fallback.pillars["pillar_weights"] == model.pillars["pillar_weights"]
    assert fallback.pillars["coverage"] == model.pillars["coverage"]
    assert set(fallback.pillars["pillars"]) == set(model.pillars["pillars"])
