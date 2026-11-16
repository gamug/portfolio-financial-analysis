"""The D6 SIC check (T-153 part 3b): the SEC's SIC code against the leaver's GICS label.

A check, never a source: a disagreement is reported and opened, and no label changes because of it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "leaver_sic_check",
    Path(__file__).resolve().parent.parent / "scripts" / "leaver_sic_check.py",
)
assert _SPEC and _SPEC.loader
sic_check = importlib.util.module_from_spec(_SPEC)
sys.modules["leaver_sic_check"] = sic_check
_SPEC.loader.exec_module(sic_check)


def test_the_label_class_reads_app00s_own_maps() -> None:
    assert sic_check.label_class("Real Estate", "Industrial REITs") == "REIT"
    assert sic_check.label_class("Utilities", "Electric Utilities") == "utility"
    assert sic_check.label_class("Financials", "Multi-line Insurance") == "insurer"
    assert sic_check.label_class("Financials", "Regional Banks") == "bank"
    assert sic_check.label_class("Financials", "Thrifts & Mortgage Finance") == "bank"
    # APP-00 puts these outside the four groups
    assert sic_check.label_class("Financials", "Insurance Brokers") is None
    assert sic_check.label_class("Health Care", "Managed Health Care") is None
    assert sic_check.label_class("Consumer Staples", "Brewers") is None


def test_the_sic_class_reads_the_code_bands() -> None:
    assert sic_check.sic_class("6798") == "REIT"
    assert sic_check.sic_class("4911") == "utility"
    assert sic_check.sic_class("6324") == "insurer"
    assert sic_check.sic_class("6021") == "bank"
    # 603x is a savings institution, outside the 602x band the request names
    assert sic_check.sic_class("6035") is None
    assert sic_check.sic_class("") is None
    assert sic_check.sic_class(None) is None


def test_the_check_reports_both_directions(tmp_path: Path) -> None:
    types = tmp_path / "types.csv"
    types.write_text(
        "cik,symbols,gics_sector,gics_sub_industry\n"
        "0000028412,CMA,Financials,Diversified Banks\n"  # bank/bank: agrees
        "0001179929,MOH,Health Care,Managed Health Care\n"  # none/insurer: disagrees
        "0001378946,PBCT,Financials,Thrifts & Mortgage Finance\n"  # bank/none (603x)
        "0001132979,FRC,Financials,Regional Banks\n"  # bank/no SIC at all
        "0000024545,TAP,Consumer Staples,Brewers\n"  # neither: not checked
    )
    sics = {
        "0000028412": ("6021", "National Commercial Banks"),
        "0001179929": ("6324", "Hospital & Medical Service Plans"),
        "0001378946": ("6035", "Savings Institution, Federally Chartered"),
        "0001132979": ("", ""),
        "0000024545": ("2082", "Malt Beverages"),
    }

    class FakeClient:
        def get(self, kind: str, cik: str) -> dict[str, str]:
            sic, described = sics[cik]
            return {"sic": sic, "sicDescription": described}

    report = sic_check.check(str(types), FakeClient())
    assert report["leavers"] == 5
    assert report["checked"] == 4
    assert report["disagreements"] == 3
    by_cik = {r["cik"]: r for r in report["rows"]}
    assert by_cik["0000028412"]["agrees"] is True
    assert by_cik["0000024545"]["checked"] is False
    assert by_cik["0000024545"]["agrees"] is True  # neither side names a type
    assert (
        by_cik["0001179929"]["label_class"] == "" and by_cik["0001179929"]["sic_class"] == "insurer"
    )
    assert by_cik["0001378946"]["sic"] == "6035"
    assert by_cik["0001132979"]["sic"] == ""
