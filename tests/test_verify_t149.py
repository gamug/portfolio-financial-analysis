"""``scripts/verify_t149.py``: the facts-only analyst (no LLM) and the production guard's fingerprint."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from conftest import cik_for
from test_pipeline_multi_filing import (
    Q1_JUN,
    Q2_OCT,
    _Edgar,
    _q1_payload,
    _settings,
    _stz_q2_payload,
)

from fundamental_agent import db as fdb
from fundamental_agent import pipeline
from fundamental_agent.agents import FALLBACK_MODEL_LABEL, UNSCORED_MODEL_LABEL, FilingContext
from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.pipeline import RunParams
from fundamental_agent.statements import Statements
from kg_schema import connect

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


def _seed_prior_fiscal_year(db_path: Path) -> None:
    """T-073 (D1): the prior fiscal year's recorded flows, the way a real run already holds them.

    A 10-Q's cash-flow ratios are trailing-twelve-month, and the identity is
    ``FY(prior 10-K) - YTD(last year) + YTD(this year)``: without the prior 10-K the Q2 fixture's
    ``cash_flow`` pillar has no value at all, and D1's coverage rule then leaves the filing
    unscored. Seeding the 10-K keeps that fixture scorable, so this test still covers the
    fallback-labelled path; Q1 is deliberately left without one (every one of its pillars is
    empty), so it is the unscored case."""
    conn = connect(db_path)
    try:
        fdb.ensure_schema(conn)
        conn.execute(
            "INSERT INTO assets (id, ticker, company_name, cik) VALUES (1, 'STZ', 'STZ Inc.', ?)",
            (cik_for("STZ"),),
        )
        filing_id = fdb.upsert_filing(
            conn,
            fdb.FilingKey(1, "10-K", 2022, "FY2022"),
            fdb.FilingMeta(filing_date="2023-04-15", period_end="2023-02-28", cik=cik_for("STZ")),
        )
        # the flow items the identity reads, on the rows whose audit inputs carry them
        fdb.record_metrics(
            conn,
            filing_id,
            [
                (
                    "cashflow",
                    MetricResult(
                        "free_cash_flow_margin",
                        None,
                        "ratio",
                        {"operating_cash_flow": 1_000.0, "capital_expenditure": 200.0},
                    ),
                ),
                (
                    "profitability",
                    MetricResult(
                        "return_on_assets",
                        None,
                        "ratio",
                        {"revenue": 9_000.0, "net_income": 900.0},
                    ),
                ),
            ],
            event_time="2023-02-28",
        )
    finally:
        conn.close()


def test_a_run_with_it_stores_scores_labelled_as_fallbacks_and_calls_no_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    edgar = _Edgar(
        {("10-Q", 2023): [Q2_OCT, Q1_JUN]},
        {Q1_JUN.accession_number: _q1_payload(), Q2_OCT.accession_number: _stz_q2_payload()},
    )
    monkeypatch.setattr(pipeline, "EdgarClient", edgar)
    # `build_model` returns None: the facts-only analyst has no model at all, so a call would fail
    monkeypatch.setattr(pipeline, "FundamentalAnalyst", verify._FactsOnlyAnalyst)
    monkeypatch.setattr(pipeline, "build_model", lambda _s: None)
    _seed_prior_fiscal_year(tmp_path / "kg.db")
    params = RunParams(
        forms=("10-Q",), since_year=2023, until_year=2023, analysis_date="2026-09-21"
    )
    report = pipeline.run(_settings(tmp_path), params)
    assert (report.completed, report.failed) == (2, 0)
    conn = sqlite3.connect(tmp_path / "kg.db")
    # T-073 (D1): the newest usable filing decides. Q2's pillars are all present (the seeded 10-K
    # gives the cash-flow ratios their trailing twelve months), so the code's total scores it and
    # the row is a fallback -- the model returned nothing usable because there is no model. Q1 has
    # an applicable `empty` pillar, so its score is empty: the row records why and counts no
    # fallback.
    rows = {
        r[0]: (r[1], r[2], r[3])
        for r in conn.execute(
            "SELECT f.accession_number, s.model, s.raw_value, s.inputs_json FROM score_snapshot s "
            "JOIN sec_filings f ON f.id = s.filing_id WHERE s.score_type = 'FUNDAMENTAL'"
        )
    }
    q2_model, q2_value, q2_block = rows[Q2_OCT.accession_number]
    assert q2_model == FALLBACK_MODEL_LABEL
    assert q2_value is not None
    assert json.loads(q2_block)["coverage"]["empty"] == []
    q1_model, q1_value, q1_block = rows[Q1_JUN.accession_number]
    assert q1_model == UNSCORED_MODEL_LABEL
    assert q1_value is None
    assert json.loads(q1_block)["coverage"]["empty"] == [
        "profitability",
        "financial_strength",
        "cash_flow",
        "growth",
    ]
    assert conn.execute("SELECT fallback_units FROM analysis_run").fetchone()[0] == 1
