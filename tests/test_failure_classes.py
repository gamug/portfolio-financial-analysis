"""One failure classifier for the exit code and the pending check (T-017, point 11)."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from test_pipeline_multi_filing import (
    Q1_JUN,
    Q2_OCT,
    _Edgar,
    _q1_payload,
    _run,
    _stz_q2_payload,
)

from fundamental_agent import cli
from fundamental_agent.edgar_client import EdgarError
from fundamental_agent.failures import DATA_MESSAGE_MARKERS, DATA_STAGES, classify
from fundamental_agent.pipeline import RunReport

FIXTURES = Path(__file__).parent / "fixtures"
UNSUCCESSFUL_EXTRACTION = (
    "/financials/1 -> unsuccessful payload: {'success': False, 'error': "
    "\"Failed to extract financials: 'NoneType' object has no attribute 'to_dataframe'\"}"
)


@pytest.mark.parametrize("stage", sorted(DATA_STAGES))
def test_a_data_stage_is_data_whatever_it_says(stage: str) -> None:
    assert classify(stage, "anything at all") == "data"


@pytest.mark.parametrize("marker", DATA_MESSAGE_MARKERS)
def test_a_data_message_is_data(marker: str) -> None:
    assert classify("process", f"0000000001-23-000001: x -> {marker}: {{...}}") == "data"


@pytest.mark.parametrize(
    "message",
    [
        "0000000001-23-000001: /financials/0000000001 failed after 3 attempts",  # transport
        "0000000001-23-000001: Client error '429 Too Many Requests' for url 'x'",  # HTTP status
        "0000000001-23-000001: openai.RateLimitError: rate limit",  # LLM provider
        "0000000001-23-000001: ZeroDivisionError: division by zero",  # unknown
        "no EDGAR match for 'X' (CIK 0000000001)",
        "",
    ],
)
def test_everything_else_including_an_unknown_error_is_service(message: str) -> None:
    assert classify("process", message) == "service"
    assert classify(None, None) == "service"


def test_a_filing_the_gateway_does_not_have_yet_is_service_not_data() -> None:
    """271 of production's 366 unsuccessful-payload rows say "No '10-K'/'10-Q' filing found": the
    filing may appear later, so it is retried (pending) and fails the step when nothing completed."""
    message = (
        "0000000001-26-000001: /financials/0000000001 -> unsuccessful payload: "
        "{'success': False, 'error': \"No '10-Q' filing found for ticker 'X' in 2026\"}"
    )
    assert classify("process", message) == "service"
    extraction = message.replace("No '10-Q' filing found", "Failed to extract financials")
    assert classify("process", extraction) == "data"


def test_the_label_and_succession_stages_are_not_data() -> None:
    assert classify("label", "x") == "service"
    assert classify("succession", "x") == "service"


def test_every_production_known_failure_classifies_as_data() -> None:
    """The 22 rows of reviewer A's full run at 2fc9a4b (18 period, 4 process)."""
    with (FIXTURES / "t017_known_failures.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 22
    assert {r["stage"] for r in rows} == {"period", "process"}
    assert [r for r in rows if classify(r["stage"], r["message"]) != "data"] == []


# -- the exit code -----------------------------------------------------------------------------


def _exit(monkeypatch: pytest.MonkeyPatch, report: RunReport) -> int:
    monkeypatch.setattr(cli, "run", lambda _s, _p: report)
    monkeypatch.setattr(cli.Settings, "load", classmethod(lambda _c, *_a, **_k: object()))
    return cli.main(["run", "--analysis-date", "2026-10-02"])


def test_only_known_data_failures_and_nothing_completed_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _exit(monkeypatch, RunReport(run_id=1, planned=3, failed=2, service_failed=0)) == 0


def test_a_service_failure_with_nothing_completed_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _exit(monkeypatch, RunReport(run_id=1, planned=3, failed=1, service_failed=1)) == 1


def test_a_service_failure_beside_a_completed_filing_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = RunReport(run_id=1, planned=3, completed=1, failed=1, service_failed=1)
    assert _exit(monkeypatch, report) == 0


def test_a_run_through_the_pipeline_counts_each_failure_by_its_class(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    edgar = _Edgar(
        {("10-Q", 2023): [Q2_OCT, Q1_JUN]},
        {Q1_JUN.accession_number: _q1_payload(), Q2_OCT.accession_number: _stz_q2_payload()},
        raises={
            Q1_JUN.accession_number: EdgarError(UNSUCCESSFUL_EXTRACTION),
            Q2_OCT.accession_number: EdgarError("/financials/1 failed after 3 attempts"),
        },
    )
    report = _run(monkeypatch, tmp_path, edgar)
    assert (report.completed, report.failed, report.service_failed, report.data_failed) == (
        0,
        2,
        1,
        1,
    )
    assert any("[data]" in e and Q1_JUN.accession_number in e for e in report.errors)
    assert any("[service]" in e and Q2_OCT.accession_number in e for e in report.errors)


def test_a_gateway_outage_on_every_filing_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    edgar = _Edgar(
        {("10-Q", 2023): [Q2_OCT, Q1_JUN]},
        {},
        raises={
            Q1_JUN.accession_number: EdgarError("/financials/1 failed after 3 attempts"),
            Q2_OCT.accession_number: EdgarError("/financials/1 failed after 3 attempts"),
        },
    )
    report = _run(monkeypatch, tmp_path, edgar)
    assert (report.completed, report.service_failed) == (0, 2)
    assert _exit(monkeypatch, report) == 1


def test_an_unknown_exception_on_one_filing_with_nothing_completed_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    edgar = _Edgar(
        {("10-Q", 2023): [Q1_JUN]},
        {},
        raises={Q1_JUN.accession_number: ZeroDivisionError("boom")},  # type: ignore[dict-item]
    )
    report = _run(monkeypatch, tmp_path, edgar)
    assert (report.completed, report.failed, report.service_failed) == (0, 1, 1)
    assert _exit(monkeypatch, report) == 1


def test_a_quiet_day_with_only_known_data_failures_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    edgar = _Edgar(
        {("10-Q", 2023): [Q2_OCT, Q1_JUN]},
        {},
        raises={
            Q1_JUN.accession_number: EdgarError("/financials/1 reconciliation failed: balance"),
            Q2_OCT.accession_number: EdgarError(UNSUCCESSFUL_EXTRACTION),
        },
    )
    report = _run(monkeypatch, tmp_path, edgar)
    assert (report.completed, report.failed, report.service_failed) == (0, 2, 0)
    assert _exit(monkeypatch, report) == 0
