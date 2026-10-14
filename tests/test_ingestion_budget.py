"""The R1 ingestion-budget script (T-153 part 3a): the never-latest count over real membership."""

from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path

from conftest import write_universe_db

from kg_schema.queries import connect_ro

_SPEC = importlib.util.spec_from_file_location(
    "ingestion_budget",
    Path(__file__).resolve().parent.parent / "scripts" / "ingestion_budget.py",
)
assert _SPEC and _SPEC.loader
budget = importlib.util.module_from_spec(_SPEC)
sys.modules["ingestion_budget"] = budget
_SPEC.loader.exec_module(budget)


class FakeSubmissions:
    """Stands in for SecClient: ``cik -> [(filing_date, form)]``."""

    def __init__(self, filings: dict[str, list[tuple[str, str]]]) -> None:
        self.filings = filings

    def get(self, kind: str, cik: str) -> dict | None:
        rows = self.filings.get(cik)
        if rows is None:
            return None
        return {
            "filings": {
                "recent": {
                    "accessionNumber": [f"acc-{i}" for i in range(len(rows))],
                    "form": [f for _, f in rows],
                    "filingDate": [d for d, _ in rows],
                    "reportDate": [d for d, _ in rows],
                    "acceptanceDateTime": [d for d, _ in rows],
                    "primaryDocument": ["x.htm" for _ in rows],
                },
                "files": [],
            }
        }


def test_membership_sessions_are_the_stints_clamped_to_the_window() -> None:
    """APO joined 2024-12-23: its sessions start there, not at the window's first day."""
    company = budget.Company(
        "0001858681", True, [("2024-12-23", None), ("2022-01-03", "2022-03-01")], ["APO"]
    )
    days = budget.membership_sessions(company, "2026-10-14", "2022-01-03")
    assert days[0] == "2022-01-03"  # its earlier stint, clamped to the window
    assert "2024-12-23" in days
    assert "2024-12-20" not in days  # before the second stint
    assert days[-1] == "2026-10-14"


def test_never_latest_ignores_dates_the_company_was_not_a_member() -> None:
    """A filing is used only on a session its company was actually in the index."""
    filings = [("2022-02-01", "2022-02-02"), ("2022-05-01", "2022-05-02")]
    # a member on 2022-02-03 only: the February filing is the latest usable then, the May one
    # is not yet available -- exactly one is used
    assert budget.never_latest(filings, ["2022-02-03"]) == 1
    # a member on both days: each filing is the latest usable on its own session
    assert budget.never_latest(filings, ["2022-02-03", "2022-05-03"]) == 0
    # a member that joins after both filings: only the newest is ever the latest usable
    assert budget.never_latest(filings, ["2026-01-05"]) == 1


def test_never_latest_uses_the_next_available_at() -> None:
    """A filing is the latest from its own available_at up to the next filing's."""
    filings = [("2022-02-01", "2022-02-02"), ("2022-02-02", "2022-02-03")]
    assert budget.never_latest(filings, ["2022-02-03"]) == 1  # the first is superseded that day
    assert budget.never_latest(filings, ["2022-02-02", "2022-02-03"]) == 0  # each has its own day


def test_measure_apart_current_and_leaver(tmp_path: Path) -> None:
    udb = write_universe_db(
        tmp_path / "u.db",
        [
            ("APO", "2024-12-23", None),  # a late joiner
            ("OLD", "2020-01-01", "2022-06-30"),  # a leaver
        ],
    )
    client = FakeSubmissions(
        {
            budget_company_cik(udb, "APO"): [
                ("2022-02-01", "10-Q"),
                ("2024-11-01", "10-Q"),
                ("2025-02-01", "10-K"),
            ],
            budget_company_cik(udb, "OLD"): [("2022-02-01", "10-K"), ("2022-05-01", "10-Q")],
        }
    )
    report = budget.measure(str(udb), client, "2026-10-14", "2022-01-03")
    assert report["groups"]["current"]["companies"] == 1
    assert report["groups"]["leaver"]["companies"] == 1
    assert report["groups"]["current"]["never_latest"] == 1  # APO's 2022 filing, before it joined
    assert report["groups"]["leaver"]["never_latest"] == 0  # both of OLD's filings are used
    assert report["totals"]["never_latest"] == 1
    assert report["groups"]["leaver"]["filings"] == 2  # nothing after its 2022-06-30 exit


def budget_company_cik(udb: Path, symbol: str) -> str:
    with contextlib.closing(connect_ro(udb)) as universe:
        row = universe.execute(
            "SELECT cik FROM universe_membership WHERE symbol = ?", (symbol,)
        ).fetchone()
    return str(row["cik"])
