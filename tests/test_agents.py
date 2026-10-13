"""T-113 (PR #91 review): the prompt-version hash identifies which prompts/model config
produced a score -- constant across every filing in a run, not a per-filing transcript hash."""

from __future__ import annotations

from typing import Any, cast

import pytest

from fundamental_agent import agents
from fundamental_agent.agents import FundamentalAnalyst, _prompt_version_hash


def _analyst(model_name: str = "deepseek-chat") -> FundamentalAnalyst:
    return FundamentalAnalyst(cast("Any", None), model_name)


def test_two_filings_in_one_run_share_the_same_prompt_hash() -> None:
    """One analyst instance serves every filing in a run; its prompt_hash is fixed at
    construction from the prompt templates + model config alone, never re-derived per filing,
    so any two filings it scores necessarily share it."""
    analyst = _analyst()
    assert analyst.prompt_hash == _prompt_version_hash("deepseek-chat")
    # re-deriving independently for a second "filing" gives the identical value
    assert _prompt_version_hash("deepseek-chat") == analyst.prompt_hash


def test_changing_one_specialist_prompt_changes_the_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """T-076: a specialist's prompt is its skill (and the generated blocks), so rewriting one skill
    changes the version hash. Replaces the T-113 version that patched the removed inline prompts."""
    before = _prompt_version_hash("deepseek-chat")
    real = agents.load_skill
    monkeypatch.setattr(
        agents,
        "load_skill",
        lambda group: (
            "a rewritten profitability specialist prompt"
            if group == "profitability"
            else real(group)
        ),
    )
    after = _prompt_version_hash("deepseek-chat")
    assert before != after


def test_changing_the_model_id_changes_the_hash() -> None:
    assert _prompt_version_hash("deepseek-chat") != _prompt_version_hash("deepseek-reasoner")
