"""The company profile (APP-00), keyed by the CIK (APP-10a) and read as of a date.

One reader for every package: the profile is the tracked ``data/company_profile.csv`` a generated
copy of the checklist's company-type table (``scripts/build_company_profile.py``); no package reads
``docs/checklist_sec/`` and no package derives the type from GICS a second way.

The profile carries APP-00's **one base type** (``operating``, ``article_9``, ``article_7``,
``other_financial``, ``multi_sector_holding`` -- with ``financial_firm`` APP-00's own label for the
first three financial types), the **overlays** (REIT, multi-class, regulated utility, multi-sector)
and the evidence accession the classification was made from. The **negative-equity** overlay is not
here: the metrics evaluate it from the filing, as APP-07 says (T-071 part 1, P2).

The file is the *current* classification, one interval covering the code's whole window: L-03
declares that snapshot applied backwards, so ``valid_to`` is empty (still current) and ``valid_from``
is the data window's start. `T-153` adds the leavers and the changes, each with its own interval.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from .identity import normalize_cik

PROFILE_CSV = Path(__file__).resolve().parent / "data" / "company_profile.csv"
GENERATOR = "scripts/build_company_profile.py"
SOURCE_CSV = "docs/checklist_sec/company_types.csv"

# APP-00: "financial firm" means Article 9 filers, Article 7 filers and other financial firms.
FINANCIAL_TYPES = frozenset({"article_9", "article_7", "other_financial"})
BASE_TYPES = ("operating", "article_9", "article_7", "other_financial", "multi_sector_holding")


@dataclass(frozen=True)
class CompanyProfile:
    """One company's APP-00 profile: its base type, its overlays and the evidence accession."""

    cik: str
    ticker: str
    company_name: str
    type: str
    financial_firm: bool
    reit: bool
    multi_class: bool
    regulated_utility: bool
    sector: str | None
    sub_industry: str | None
    evidence_accession: str | None
    valid_from: str
    valid_to: str  # empty = still current (L-03)

    @property
    def multi_sector(self) -> bool:
        """APP-00's multi-sector holding: every metric's cell is ADJ."""
        return self.type == "multi_sector_holding"

    def covers(self, as_of: str) -> bool:
        """Whether the profile's interval contains *as_of* (a ``YYYY-MM-DD`` date or timestamp)."""
        day = str(as_of)[:10]
        return self.valid_from <= day and (not self.valid_to or day < self.valid_to)


def _data_lines(text: str) -> list[str]:
    """The file's rows: the CSV header plus its records, without the ``#`` header comments."""
    return [line for line in text.splitlines() if line and not line.startswith("#")]


def _truthy(value: str) -> bool:
    return str(value).strip().lower() == "true"


@cache
def profiles() -> dict[str, CompanyProfile]:
    """Every profile in the tracked file, keyed by its 10-digit CIK."""
    text = PROFILE_CSV.read_text(encoding="utf-8")
    out: dict[str, CompanyProfile] = {}
    for row in csv.DictReader(_data_lines(text)):
        cik = normalize_cik(row["cik"])
        if cik is None:
            raise ValueError(f"{PROFILE_CSV.name}: a row has no CIK")
        out[cik] = CompanyProfile(
            cik=cik,
            ticker=row.get("tickers") or "",
            company_name=row.get("company_name") or "",
            type=row["type"],
            financial_firm=_truthy(row["financial_firm"]),
            reit=_truthy(row["reit"]),
            multi_class=_truthy(row["multi_class_listed"]),
            regulated_utility=_truthy(row["regulated_utility"]),
            sector=row.get("gics_sector") or None,
            sub_industry=row.get("gics_sub_industry") or None,
            evidence_accession=row.get("evidence_accession") or None,
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
        )
    return out


def profile_as_of(cik: str | None, as_of: str | None = None) -> CompanyProfile | None:
    """The profile of *cik* as of *as_of* (``YYYY-MM-DD``), or ``None`` when there is none.

    ``None`` is the **unknown profile**: no row for that CIK, or a date outside the row's interval.
    A caller never guesses the type -- it records the unknown and computes the metric anyway
    (T-071 part 1, P4). ``as_of=None`` asks for the current snapshot without the interval check (the
    REIT predicate and the card have no filing date of their own).
    """
    try:
        key = normalize_cik(cik)
    except ValueError:
        return None
    if key is None:
        return None
    profile = profiles().get(key)
    if profile is None:
        return None
    if as_of is not None and not profile.covers(as_of):
        return None
    return profile


__all__ = [
    "BASE_TYPES",
    "FINANCIAL_TYPES",
    "GENERATOR",
    "PROFILE_CSV",
    "SOURCE_CSV",
    "CompanyProfile",
    "profile_as_of",
    "profiles",
]
