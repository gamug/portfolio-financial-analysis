"""ID-07 (T-149): the period end a filing is stored under must be the SEC's ``periodOfReport``
(listed by the gateway as ``period_of_report``, portfolio-data-mining T-045), to the month end
(PER-08). The unexplained cases of T-147's audit, with their real captures:

* **EXE FY2021** was stored as FY2020. Expand Energy's fresh-start accounting split 2021 into a
  predecessor and a successor period, so the 10-K's payload has no full-year 2021 column; the latest
  fiscal-year column, 2020-12-31, was stored under the FY2021 filing's accession.
* **SMCI FY2024** was lost, not mis-stored: that 10-K was filed late, on 2025-02-25, so two 10-Ks
  were filed in calendar 2025 and the old rule kept only the newest of a year's.
* **GEHC FY2022** fails at the gateway itself (its extraction finds no statements in the filing);
  **CSCO / SMCI FY2026** succeed today -- the production runs of 2026-08-28/30 predate those 10-Ks
  (filed 2026-09-02 and 2026-08-31).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from conftest import write_universe_db
from test_filing_identity import IDENTITY, _refs
from test_pipeline_multi_filing import _Edgar, _settings, _StubAnalyst

from fundamental_agent import pipeline
from fundamental_agent.edgar_client import EdgarError
from fundamental_agent.fiscal import same_period_end
from fundamental_agent.pipeline import RunParams, _select_filings, _YearTask

FIXTURES = Path(__file__).parent / "fixtures"
EXE_ACCESSION = "0000895126-22-000029"


def _exe_payload() -> dict[str, Any]:
    raw = json.loads((FIXTURES / f"financials_EXE_10-K_2022_acc-{EXE_ACCESSION}.json").read_text())
    return raw["data"]  # type: ignore[no-any-return]


def _run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, edgar: _Edgar, ticker: str, cik: str, **kw: Any
) -> pipeline.RunReport:
    udb = write_universe_db(tmp_path / "universe.db", [(ticker, "2020-01-01", None)])
    conn = sqlite3.connect(udb)
    conn.execute("UPDATE universe_membership SET cik = ?", (cik,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(pipeline, "EdgarClient", edgar)
    monkeypatch.setattr(pipeline, "build_model", lambda _s: None)
    monkeypatch.setattr(pipeline, "FundamentalAnalyst", _StubAnalyst)
    params = RunParams(
        forms=("10-K",), since_year=2022, until_year=2022, analysis_date="2026-09-21", **kw
    )
    return pipeline.run(_settings(tmp_path), params)


def test_expand_energy_fy2021_is_not_stored_as_fy2020(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (ref,) = _refs("filing_by_year_EXE_10-K_2022")
    assert ref.period_of_report == "2021-12-31"
    edgar = _Edgar({("10-K", 2022): [ref]}, {EXE_ACCESSION: _exe_payload()})
    report = _run(monkeypatch, tmp_path, edgar, "EXE", "0000895126")
    assert (report.completed, report.failed, report.skipped) == (0, 0, 1)
    conn = sqlite3.connect(tmp_path / "kg.db")
    assert conn.execute("SELECT COUNT(*) FROM sec_filings").fetchone()[0] == 0
    ((stage, message),) = conn.execute(
        "SELECT stage, message FROM analysis_run_error WHERE ticker = 'EXE'"
    ).fetchall()
    assert stage == "period"
    assert "periodOfReport 2021-12-31" in message and "2020-12-31 (FY)" in message
    assert EXE_ACCESSION in message


def test_a_filing_with_no_period_of_report_cannot_be_checked_and_is_stored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same payload from a gateway that sends none (before T-045): the old behaviour, which
    is why the field is the check -- the statements alone cannot tell."""
    (ref,) = _refs("filing_by_year_EXE_10-K_2022")
    blind = type(ref)(ref.form, ref.filing_date, ref.accession_number, ref.cik, None)
    edgar = _Edgar({("10-K", 2022): [blind]}, {EXE_ACCESSION: _exe_payload()})
    report = _run(monkeypatch, tmp_path, edgar, "EXE", "0000895126")
    assert report.completed == 1
    conn = sqlite3.connect(tmp_path / "kg.db")
    assert conn.execute("SELECT fiscal_period, period_end FROM sec_filings").fetchall() == [
        ("FY2020", "2020-12-31")
    ]


@pytest.mark.parametrize(
    ("stored", "reported", "same"),
    [
        ("2025-06-28", "2025-06-28", True),
        ("2025-06-30", "2025-06-28", True),  # a 52/53-week quarter, rounding to June
        ("2024-09-28", "2024-09-30", True),
        ("2023-01-01", "2022-12-31", True),
        ("2020-12-31", "2021-12-31", False),  # EXE
        ("2025-06-30", "2025-07-20", False),
    ],
)
def test_the_stored_and_the_reported_period_end_match_to_the_month_end(
    stored: str, reported: str, same: bool
) -> None:
    assert same_period_end(date.fromisoformat(stored), date.fromisoformat(reported)) is same


def test_super_micro_s_late_fy2024_10k_is_kept_beside_the_fy2025_one() -> None:
    """Real listing of calendar 2025: the FY2024 10-K (filed late, 2025-02-25) and the FY2025 one
    (2025-08-28). The old rule kept only the newest 10-K of a filing year."""
    listing = _refs("filing_by_year_SMCI_10-K_2025")
    picked = _select_filings(_YearTask(1, "SMCI", "SMCI", "10-K", 2025, "0001375365"), listing)
    assert [(r.period_of_report, r.filing_date) for r in picked] == [
        ("2024-06-30", "2025-02-25"),
        ("2025-06-30", "2025-08-28"),
    ]


def test_a_gateway_extraction_failure_is_recorded_not_stored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """GEHC's FY2022 10-K, as the gateway answers it today (real capture)."""
    (ref,) = _refs("filing_by_year_GEHC_10-K_2023")
    message = IDENTITY["financials_GEHC_10-K_2023_failure"]["error"]
    edgar = _Edgar({("10-K", 2022): [ref]}, {}, raises={ref.accession_number: EdgarError(message)})
    report = _run(monkeypatch, tmp_path, edgar, "GEHC", "0001932393")
    assert (report.completed, report.failed) == (0, 1)
    assert "to_dataframe" in report.errors[0]
    conn = sqlite3.connect(tmp_path / "kg.db")
    assert conn.execute("SELECT COUNT(*) FROM sec_filings").fetchone()[0] == 0
