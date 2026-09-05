"""The analysis_date CLI seam: parse, default-to-today, argparse wiring."""

from __future__ import annotations

import argparse

import pytest

from kg_schema import rundate


def test_parse_analysis_date_accepts_iso_rejects_other() -> None:
    assert rundate.parse_analysis_date("2021-06-03") == "2021-06-03"
    for bad in ("06/03/2021", "2021-6-3", "yesterday"):
        with pytest.raises(argparse.ArgumentTypeError, match="ISO date"):
            rundate.parse_analysis_date(bad)


def test_resolve_defaults_to_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rundate, "today", lambda: "2030-01-01")
    assert rundate.resolve(None) == "2030-01-01"
    assert rundate.resolve("2021-06-30") == "2021-06-30"


def test_add_analysis_date_argument() -> None:
    p = argparse.ArgumentParser()
    rundate.add_analysis_date_argument(p)
    assert p.parse_args([]).analysis_date is None
    assert p.parse_args(["--analysis-date", "2022-02-02"]).analysis_date == "2022-02-02"
    with pytest.raises(SystemExit):
        p.parse_args(["--analysis-date", "nope"])
