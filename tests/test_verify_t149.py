"""``scripts/verify_t149.py``: the facts-only analyst (no LLM) and the production guard's fingerprint."""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest
from test_pipeline_multi_filing import (
    Q1_JUN,
    Q2_OCT,
    _Edgar,
    _q1_payload,
    _settings,
    _stz_q2_payload,
)

from fundamental_agent import pipeline
from fundamental_agent.agents import FALLBACK_MODEL_LABEL, FilingContext
from fundamental_agent.pipeline import RunParams
from fundamental_agent.statements import Statements

_SPEC = importlib.util.spec_from_file_location(
    "verify_t149", Path(__file__).resolve().parent.parent / "scripts" / "verify_t149.py"
)
assert _SPEC and _SPEC.loader
verify = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_t149"] = verify
_SPEC.loader.exec_module(verify)


def test_the_fingerprint_is_the_file_s_sha1(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    path.write_bytes(b"portfolio" * 500_000)  # more than one read block
    assert verify.sha1(path) == hashlib.sha1(b"portfolio" * 500_000).hexdigest()  # noqa: S324


def test_the_facts_only_analyst_computes_every_metric_and_scores_by_the_rule_based_fallback(
    aapl_10k: Statements,
) -> None:
    key = aapl_10k.latest_fy().key  # type: ignore[union-attr]
    ctx = FilingContext("AAPL", "Apple", "10-K", "FY2023", aapl_10k, key, None)
    result = verify._FactsOnlyAnalyst(None, "facts-only-no-llm").analyze(ctx)
    assert result.used_fallback and result.prompt_hash == "facts-only-no-llm"
    assert result.flat_metrics["profitability.net_margin"] is not None
    assert 0.0 <= result.assessment.score <= 100.0


def test_a_run_with_it_stores_scores_labelled_as_fallbacks_and_calls_no_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    edgar = _Edgar(
        {("10-Q", 2023): [Q2_OCT, Q1_JUN]},
        {Q1_JUN.accession_number: _q1_payload(), Q2_OCT.accession_number: _stz_q2_payload()},
    )
    monkeypatch.setattr(pipeline, "EdgarClient", edgar)
    monkeypatch.setattr(pipeline, "FundamentalAnalyst", verify._FactsOnlyAnalyst)
    monkeypatch.setattr(pipeline, "build_model", lambda _s: None)
    params = RunParams(
        forms=("10-Q",), since_year=2023, until_year=2023, analysis_date="2026-09-21"
    )
    report = pipeline.run(_settings(tmp_path), params)
    assert (report.completed, report.failed) == (2, 0)
    conn = sqlite3.connect(tmp_path / "kg.db")
    assert {r[0] for r in conn.execute("SELECT model FROM score_snapshot")} == {
        FALLBACK_MODEL_LABEL
    }
    assert conn.execute("SELECT fallback_units FROM analysis_run").fetchone()[0] == 2
