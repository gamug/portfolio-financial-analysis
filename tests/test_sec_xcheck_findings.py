"""``scripts/sec_xcheck/findings.py``: the one shape every measurement reports in (T-147)."""

from __future__ import annotations

from sec_xcheck.findings import Finding, from_json, markdown, spread, to_json


def test_spread_shows_five_companies_before_a_second_filing_of_one() -> None:
    examples = [("AAA", f"acc-{i}", "") for i in range(4)] + [(t, "acc", "") for t in "BCDEF"]
    shown = spread(examples)
    assert [t for t, _a, _d in shown] == [
        "AAA",
        "B",
        "C",
        "D",
        "E",
    ]  # AAA once, then the other companies


def test_finding_round_trips_through_json_and_renders_a_row() -> None:
    f = Finding(
        "K",
        ["ID-02a"],
        "title",
        "b",
        3,
        10,
        2,
        examples=[("AAA", "acc-1", "10-K FY2022")],
        command="cmd",
    )
    back = from_json(to_json([f]))[0]
    assert back == f
    assert f.share() == 0.3
    row = f.row()
    assert (
        "`K`" in row
        and "3 / 10 filings" in row
        and "AAA acc-1 10-K FY2022" in row
        and "| production |" in row
    )


def test_a_rule_without_a_source_measure_is_flagged_never_zero() -> None:
    f = Finding("ID09.consolidation_axis", ["ID-09"], "t", "a", 0, 0, 0, incomplete=True)
    assert "source prevalence incomplete" in f.row()


def test_markdown_has_a_header_and_one_row_per_finding() -> None:
    table = markdown(
        [Finding("A", ["R"], "t", "a", 1, 2, 1), Finding("B", ["R"], "t", "b", 0, 2, 0)]
    )
    assert table.count("\n") == 3
    assert table.startswith("| Measurement | Rules | Level | Database |")
