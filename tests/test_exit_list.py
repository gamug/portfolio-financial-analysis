"""The exit list's rules (T-153 part 3c, brief section 4): the terms filing and its order.

The script reads the SEC's cached submissions pages; what is pinned here is the rule, so a
re-run cannot silently pick an annual-meeting proxy or a filing that belongs to another line.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "exit_list",
    Path(__file__).resolve().parent.parent / "scripts" / "exit_list.py",
)
assert _SPEC and _SPEC.loader
exit_list = importlib.util.module_from_spec(_SPEC)
sys.modules["exit_list"] = exit_list
_SPEC.loader.exec_module(exit_list)


def page(*filings: tuple[str, str, str | None]) -> list[dict[str, object]]:
    """A hand-made submissions page: (form, filingDate, items)."""
    return [
        {
            "form": f,
            "filingDate": d,
            "reportDate": d,
            "items": items,
            "accessionNumber": f"0000000000-26-0000{i:02d}",
        }
        for i, (f, d, items) in enumerate(filings, start=1)
    ]


def test_the_merger_proxy_wins_over_an_annual_meeting_proxy() -> None:
    """A DEF 14A is an annual-meeting proxy, never the terms filing. The forms match exactly."""
    rows = page(
        ("DEF 14A", "2026-04-01", None),  # the annual proxy, filed later
        ("DEFM14A", "2026-02-01", None),  # the merger proxy
    )
    assert exit_list.terms_accession(rows, "2026-12-31") == ("0000000000-26-000002", "DEFM14A")


def test_a_tender_offer_reads_the_sc_14d9() -> None:
    rows = page(("SC 14D9", "2022-01-19", None), ("DEF 14A", "2022-03-01", None))
    assert exit_list.terms_accession(rows, "2022-12-31") == ("0000000000-26-000001", "SC 14D9")


def test_two_merger_proxies_for_one_deal_read_the_completion_8k() -> None:
    """WBD's shape: two DEFM14As six weeks apart, then the completion 8-K with Item 2.01."""
    rows = page(
        ("DEFM14A", "2026-02-17", None),
        ("DEFM14A", "2026-03-26", None),
        ("8-K", "2026-10-06", "1.02,2.01,3.01"),
    )
    assert exit_list.terms_accession(rows, "2026-10-15") == (
        "0000000000-26-000003",
        "completion 8-K Item 2.01",
    )


def test_two_merger_proxies_for_different_deals_keep_the_latest() -> None:
    """NLSN's shape: a 2020 proxy for another deal, then the exit's own 2022 DEFM14A."""
    rows = page(("DEFM14A", "2020-12-23", None), ("DEFM14A", "2022-07-08", None))
    assert exit_list.terms_accession(rows, "2022-12-31") == ("0000000000-26-000002", "DEFM14A")


def test_an_s4_is_not_the_terms_filing() -> None:
    """WRK's shape: an S-4 after the exit is not the DEFM14A filed before it."""
    rows = page(("DEFM14A", "2024-04-26", None), ("S-4", "2025-06-01", None))
    assert exit_list.terms_accession(rows, "2024-07-17")[1] == "DEFM14A"
    only_s4 = page(("S-4", "2025-06-01", None))
    assert exit_list.terms_accession(only_s4, "2024-07-17") == ("not cached", "not cached")


def test_the_failure_lines_have_no_terms_filing() -> None:
    assert "failure: no deal terms" not in exit_list.TERMS_ORDER
    for line in ("SIVB", "SBNY", "FRC"):
        assert line in exit_list.FAILURES
    assert exit_list.RECEIVERSHIP == {"SIVB": "0001193125-23-067777"}
    assert {"SBNY", "FRC"} == exit_list.FDIC
