"""The comparison logic of ``itemcheck``, ``signcheck``, ``xcheck`` and ``taxid`` against SEC facts (T-147).

Pure functions on hand-built values; the SEC side is a ``companyfacts`` fixture, never the network."""

from __future__ import annotations

import pytest
from sec_xcheck import itemcheck, signcheck, taxid, xcheck
from sec_xcheck.common import SecClient

ACCN = "0000000001-24-000001"
DOC = {
    "facts": {
        "us-gaap": {
            "IncomeTaxExpenseBenefit": {
                "units": {
                    "USD": [{"start": "2023-01-01", "end": "2023-12-31", "val": 25.0, "accn": ACCN}]
                }
            },
            "DeferredIncomeTaxExpenseBenefit": {
                "units": {
                    "USD": [{"start": "2023-01-01", "end": "2023-12-31", "val": 7.0, "accn": ACCN}]
                }
            },
            "Assets": {"units": {"USD": [{"end": "2023-12-31", "val": 900.0, "accn": ACCN}]}},
        }
    }
}


def test_a_value_equal_to_its_own_concept_is_ok() -> None:
    own = {"IncomeTaxExpenseBenefit"}
    assert itemcheck.classify(25.0, own, {25.0: {"IncomeTaxExpenseBenefit"}})[0] == "ok_own_concept"


def test_the_negative_of_a_filed_value_is_flipped() -> None:
    own = {"IncomeTaxExpenseBenefit"}
    assert itemcheck.classify(-25.0, own, {25.0: {"IncomeTaxExpenseBenefit"}})[0] == "FLIPPED"


def test_a_value_that_equals_another_concept_is_other_concept_and_names_it() -> None:
    cls, extra = itemcheck.classify(
        7.0, {"IncomeTaxExpenseBenefit"}, {7.0: {"DeferredIncomeTaxExpenseBenefit"}}
    )
    assert (cls, extra) == ("OTHER_CONCEPT", ["DeferredIncomeTaxExpenseBenefit"])


def test_a_derived_value_matches_no_fact_and_an_empty_accession_is_its_own_class() -> None:
    assert itemcheck.classify(99.0, {"X"}, {1.0: {"Y"}})[0] == "NOT_IN_SEC(rebuilt/derived)"
    assert itemcheck.classify(99.0, {"X"}, {})[0] == "no_sec_facts_for_accn"
    assert itemcheck.classify(None, {"X"}, {})[0] == "none"


def test_facts_of_filters_by_accession_period_and_kind() -> None:
    durations = itemcheck.facts_of([DOC], ACCN, "2023-12-31", "10-K", instant=False)
    assert durations == {
        25.0: {"IncomeTaxExpenseBenefit"},
        7.0: {"DeferredIncomeTaxExpenseBenefit"},
    }
    assert itemcheck.facts_of([DOC], ACCN, "2023-12-31", "10-K", instant=True) == {
        900.0: {"Assets"}
    }
    assert itemcheck.facts_of([DOC], "other", "2023-12-31", "10-K", instant=False) == {}
    assert (
        itemcheck.facts_of([DOC], ACCN, "2023-12-31", "10-Q", instant=False) == {}
    )  # a year is not a quarter


@pytest.mark.parametrize(
    ("stored", "secs", "expected"),
    [(25.0, [25.0], "same_sign"), (-25.0, [25.0], "FLIPPED"), (3.0, [25.0], "other_value")],
)
def test_sign_class(stored: float, secs: list[float], expected: str) -> None:
    assert signcheck.sign_class(stored, secs) == expected


def test_sec_values_collect_the_listed_concepts_of_the_accession() -> None:
    vals = signcheck.sec_values(
        [DOC],
        ("IncomeTaxExpenseBenefit", "DeferredIncomeTaxExpenseBenefit"),
        ACCN,
        "2023-12-31",
        "10-K",
    )
    assert sorted(vals) == [7.0, 25.0]


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("2023-12-31", ("2023-12-31", "INST")),
        ("2023-12-31 (FY)", ("2023-12-31", "FY")),
        ("2023-09-30 (Q3)", ("2023-09-30", "Q3")),
        ("garbage", None),
    ],
)
def test_parse_period_key(key: str, expected: tuple[str, str] | None) -> None:
    assert xcheck.parse_period_key(key) == expected


def test_kind_ok_uses_the_duration_of_the_tag() -> None:
    assert xcheck.kind_ok("INST", None, "2023-12-31")
    assert not xcheck.kind_ok("INST", "2023-01-01", "2023-12-31")
    assert xcheck.kind_ok("FY", "2023-01-01", "2023-12-31")
    assert xcheck.kind_ok("Q3", "2023-07-01", "2023-09-30")
    assert xcheck.kind_ok("YTD", "2023-01-01", "2023-09-30")
    assert not xcheck.kind_ok("FY", "2023-07-01", "2023-09-30")


@pytest.mark.parametrize(
    ("value", "cands", "expected"),
    [
        (5.0, [], "no_sec_fact_same_accn_period"),
        (5.0, [5.0], "match"),
        (-5.0, [5.0], "sign_flip"),
        (5.0, [9.0], "VALUE_MISMATCH"),
    ],
)
def test_classify_fact(value: float, cands: list[float], expected: str) -> None:
    assert xcheck.classify_fact(value, cands) == expected


def test_candidates_for_reads_the_accession_concept_and_period() -> None:
    index = {
        ACCN: [
            (t, c, f)
            for t, c, f in __import__("sec_xcheck.common", fromlist=["x"]).iter_us_gaap(DOC)
        ]
    }
    assert xcheck.candidates_for(
        index, ACCN, "us-gaap_IncomeTaxExpenseBenefit", "2023-12-31", "FY"
    ) == [25.0]
    assert xcheck.candidates_for(index, ACCN, "us-gaap_Assets", "2023-12-31", "INST") == [900.0]
    assert xcheck.candidates_for(index, ACCN, "us-gaap_Assets", "2023-12-31", "FY") == []


def test_tax_identity_picks_the_one_sign_that_balances() -> None:
    # pre-tax 100, after-tax 75: the tax is +25 whatever sign the statement parser displayed
    assert taxid.identity_signs(100e6, -25e6, 75e6, 0.0) == [25e6]
    assert taxid.identity_signs(100e6, 25e6, 75e6, 0.0) == [25e6]
    assert taxid.identity_signs(100e6, 25e6, 40e6, 0.0) == []  # nothing balances
    # equity-method income can balance it too
    assert taxid.identity_signs(100e6, 25e6, 80e6, 5e6) == [25e6]


def test_offline_client_is_the_only_mode_the_check_scripts_use(tmp_path: object) -> None:
    client = SecClient(tmp_path, offline=True)  # type: ignore[arg-type]
    with pytest.raises(FileNotFoundError):
        client.get("companyfacts", 1)
