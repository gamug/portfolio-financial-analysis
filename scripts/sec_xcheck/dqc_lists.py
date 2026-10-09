"""The XBRL US Data Quality Committee element lists behind ID-13, ID-14 and ID-15 (registered in ``sources_register.md``,
Block B, with the commit they were taken from).

* DQC_0015 (ID-13): elements that must not be negative; one CSV per us-gaap taxonomy release (``dqc_15_usgaap_<year>_concepts.csv``);
* DQC_0013 (ID-14): elements negative only when a precondition on another element holds (``DQC_0013_ListOfElements.xlsx``);
* DQC_0014 (ID-15): elements that must not be negative when filed without dimensions (``DQC_0014_ListOfElements.xlsx``).

``companyfacts`` carries only non-dimensional facts, so DQC_0015's member exclusions (which allow a negative value with a
listed member) never apply to it. The spreadsheets are read with the standard library (a workbook is a zip of XML).
"""

from __future__ import annotations

import csv
import re
import zipfile
from pathlib import Path
from xml.etree import (
    ElementTree as ET,
)

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
MAX_XML_BYTES = 50_000_000
YEARS = range(2020, 2027)


def _column(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref)
    index = 0
    for ch in letters.group(0) if letters else "":
        index = index * 26 + ord(ch) - 64
    return index - 1


def read_xlsx(path: Path) -> list[list[str]]:
    """The first sheet of a workbook as rows of strings (shared strings resolved, gaps empty)."""
    with zipfile.ZipFile(path) as z:
        for name in ("xl/sharedStrings.xml", "xl/worksheets/sheet1.xml"):
            if z.getinfo(name).file_size > MAX_XML_BYTES:
                raise ValueError(f"{path}: {name} is too large")
        strings = [
            "".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t"))
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS)  # noqa: S314
        ]
        sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))  # noqa: S314
    rows: list[list[str]] = []
    for row in sheet.iter(f"{{{NS['m']}}}row"):
        cells: dict[int, str] = {}
        for c in row.findall("m:c", NS):
            v = c.find("m:v", NS)
            if v is None or v.text is None:
                continue
            cells[_column(c.get("r", "A"))] = strings[int(v.text)] if c.get("t") == "s" else v.text
        rows.append([cells.get(i, "") for i in range(max(cells, default=-1) + 1)])
    return rows


def table(rows: list[list[str]]) -> list[dict[str, str]]:
    """Rows keyed by the header row (the first row that names an element column)."""
    start = next(
        i for i, r in enumerate(rows) if any(h.lower() in ("elementname", "element") for h in r)
    )
    header = [h.strip() for h in rows[start]]
    return [
        dict(zip(header, r + [""] * (len(header) - len(r)), strict=False))
        for r in rows[start + 1 :]
    ]


def dqc15(directory: Path) -> dict[int, set[str]]:
    """``{taxonomy year: element names}`` of DQC_0015 from the per-release CSVs."""
    out: dict[int, set[str]] = {}
    for year in YEARS:
        path = directory / f"dqc_15_usgaap_{year}_concepts.csv"
        with path.open(encoding="utf-8") as fh:
            out[year] = {row[1] for row in csv.reader(fh) if len(row) > 1}
    return out


def dqc14(directory: Path) -> set[str]:
    rows = table(read_xlsx(directory / "DQC_0014_ListOfElements.xlsx"))
    return {r["element"] for r in rows if r.get("namespace") == "us-gaap" and r.get("element")}


def dqc13(directory: Path) -> dict[str, tuple[str, str]]:
    """``{element: (precondition value, precondition criteria)}`` of DQC_0013 (us-gaap)."""
    rows = table(read_xlsx(directory / "DQC_0013_ListOfElements.xlsx"))
    return {
        r["elementName"]: (r.get("preconditionValue", ""), r.get("preconditionCriteria", ""))
        for r in rows
        if r.get("namespace") == "us-gaap" and r.get("elementName")
    }
