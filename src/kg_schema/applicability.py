"""The applicability matrix (APP-00, APP-01 to APP-09, MKT-03), readable by every package.

``data/applicability_matrix.csv`` is generated from the checklist's own matrix by
``scripts/build_metric_dictionary.py`` (one row per metric x company type: ``A``, ``ADJ`` or ``NA``,
with the rule that decides the cell). It lives in ``kg_schema`` so ``cycle`` reads it in T-071 part 2
without importing ``fundamental_agent``.

The matrix's six columns are not a company type each: three are base types (Article 9 filers,
Article 7 insurers, other financial firms) and three are overlays (REIT, negative equity,
multi-class). A company's cell is decided by the **precedence NA > ADJ > A** over the columns it
activates (APP-00: "the Scope's precedence over the active overlays"), and a multi-sector holding is
ADJ for every metric. An operating company activates no column, so every cell is A.

A metric with **no row** in the matrix has no claim on it: ``ebitda`` (a building block) and the
robustness metric ``return_on_invested_capital_effective_rate`` are the two the code computes.
They are treated as A -- not a second map without a source (AGENTS.md) -- and recorded as a declared
limitation in ``docs/model_fixes.md`` (T-071 part 1).
"""

from __future__ import annotations

import csv
from functools import cache
from pathlib import Path

from .company_profile import CompanyProfile

MATRIX_CSV = Path(__file__).resolve().parent / "data" / "applicability_matrix.csv"

# The matrix's columns, in the checklist's own order.
COLUMNS = (
    "art_9_filer",
    "art_7_insurer",
    "other_financial",
    "reit",
    "negative_equity",
    "multi_class",
)
CODES = ("A", "ADJ", "NA")
# NA > ADJ > A (APP-00).
PRECEDENCE = {"NA": 0, "ADJ": 1, "A": 2}
MULTI_SECTOR_CELL = ("ADJ", "APP-00")


@cache
def _matrix() -> dict[str, dict[str, tuple[str, str]]]:
    """``{metric: {column: (code, rule)}}`` from the generated matrix."""
    out: dict[str, dict[str, tuple[str, str]]] = {}
    with MATRIX_CSV.open() as handle:
        for row in csv.DictReader(handle):
            out.setdefault(row["metric"], {})[row["company_type"]] = (row["code"], row["rule"])
    return out


def metrics() -> tuple[str, ...]:
    """The matrix's metrics, in file order."""
    return tuple(_matrix())


def cells(metric: str) -> dict[str, tuple[str, str]]:
    """One metric's six cells, ``{}`` when the matrix makes no claim on it."""
    return _matrix().get(metric, {})


def active_columns(profile: CompanyProfile, *, negative_equity: bool) -> tuple[str, ...]:
    """The matrix columns *profile* activates, in the checklist's order.

    The base type (one of the three financial ones; an operating company activates none), then the
    overlays: REIT, negative equity (the metrics' own APP-07 evaluation, passed in) and multi-class.
    A multi-sector holding activates none of them -- :func:`applicability` gives it ADJ everywhere.
    """
    columns: list[str] = []
    if profile.type == "article_9":
        columns.append("art_9_filer")
    elif profile.type == "article_7":
        columns.append("art_7_insurer")
    elif profile.type == "other_financial":
        columns.append("other_financial")
    if profile.reit:
        columns.append("reit")
    if negative_equity:
        columns.append("negative_equity")
    if profile.multi_class:
        columns.append("multi_class")
    return tuple(columns)


def applicability(
    metric: str, profile: CompanyProfile, *, negative_equity: bool
) -> tuple[str, str]:
    """``(code, rule)`` for *metric* on *profile*: NA > ADJ > A over the active columns.

    A multi-sector holding is ADJ for every metric (APP-00). A metric the matrix does not cover is
    ``("A", "")``. The rule is the winning cells' rules, distinct, in the checklist's column order --
    so a company that is both an Article 9 filer and negative-equity names both APP-02 and APP-07.
    """
    if profile.multi_sector:
        return MULTI_SECTOR_CELL
    found = [
        cells(metric).get(column)
        for column in active_columns(profile, negative_equity=negative_equity)
    ]
    present = [cell for cell in found if cell is not None]
    if not present:
        return ("A", "")
    code = min((cell[0] for cell in present), key=lambda value: PRECEDENCE[value])
    rules = [cell[1] for cell in present if cell[0] == code and cell[1]]
    return (code, ", ".join(dict.fromkeys(rules)))


__all__ = ["CODES", "COLUMNS", "MATRIX_CSV", "active_columns", "applicability", "cells", "metrics"]
