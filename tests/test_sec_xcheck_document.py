"""``docs/sec_data_checklist.md`` is rendered from the rule table and the recorded results (T-147): these tests keep the table
honest. They read the tracked inputs under ``docs/checklist_sec/`` and never the network or a database."""

from __future__ import annotations

from pathlib import Path

import pytest
from sec_xcheck import checklist_doc as cd
from sec_xcheck import defects
from sec_xcheck.rules_table import INCOMPLETE, LIMITATIONS, RULES

DIR = Path(__file__).resolve().parents[1] / "docs" / "checklist_sec"
pytestmark = pytest.mark.skipif(
    not (DIR / "prevalence_results.json").exists(), reason="the audit inputs are not checked out"
)


def test_every_rule_of_the_frozen_checklist_has_exactly_one_entry() -> None:
    ids = set(cd.severities())
    assert len(ids) == 78
    assert ids == set(RULES)


def test_every_check_mark_cites_the_test_that_pins_it() -> None:
    """The brief: a ✓ with no test is `partial (untested)`."""
    for rid, entry in RULES.items():
        if entry.status == "✓":
            assert entry.test != "none", f"{rid} is ✓ but no test is cited"


def test_statuses_are_from_the_documented_set() -> None:
    allowed = ("✓", "✗", "partial", "n/a", "L")
    for rid, entry in RULES.items():
        assert entry.status.startswith(allowed), f"{rid}: {entry.status}"


def test_every_measurement_a_rule_or_defect_names_was_run() -> None:
    res = cd.Results()
    keys = {f.key for f in res.findings}
    named = {k for e in RULES.values() for k in (*e.a, *e.b) if k != INCOMPLETE}
    named |= {k for _v, _t, ks in LIMITATIONS.values() for k in ks}
    named |= {k for d in defects.OPEN for k in d[3]}
    named |= {h[1] for h in cd.HEADLINES}
    assert named - keys == set()


def test_a_rule_without_a_source_measure_is_marked_incomplete_never_zero() -> None:
    for rid in ("ID-09", "ID-10", "ID-16", "ID-21"):
        assert RULES[rid].a == (INCOMPLETE,), rid


def test_the_rendered_document_has_a_row_per_rule_and_is_deterministic() -> None:
    first, second = cd.render(cd.Results()), cd.render(cd.Results())
    assert first == second
    for rid in RULES:
        assert f"\n| {rid} | " in first
    assert (
        "## 8. Where this audit disagrees" in first and "## 9. Inputs for checklist v1.3" in first
    )


def test_the_ranking_orders_by_severity_companies_and_reach() -> None:
    lines = cd.ranking(cd.Results())
    scores = [int(row.split("|")[2]) for row in lines[2:]]
    assert scores == sorted(scores, reverse=True)
    assert len(scores) == len(cd.HEADLINES)
