"""Generate the tracked metric dictionary, applicability matrix and T-076 data files from checklist v1.3.

Reads ``docs/checklist_sec/checklist_v1.3.md`` -- the frozen specification, git-excluded and local --
and writes four **tracked** files the code and the tests read:

* ``docs/metric_dictionary.md`` -- every Annex B row (ID, metric, formula, basis, source, quote, note),
  with a header naming this script, the checklist version and the checklist file's sha256;
* ``src/fundamental_agent/data/applicability_matrix.csv`` -- the applicability matrix (metric, company
  type, code, rule ID), one row per cell;
* ``src/fundamental_agent/data/metric_definitions.csv`` -- one row per Annex B row (metric, MD id,
  formula, basis); a specialist group's system prompt reads its own metrics' rows (T-076, D1);
* ``src/fundamental_agent/data/checklist_rules.csv`` -- every rule ID of the checklist (the seven
  layers, section L's limitations, Annex A's screens, Annex B's metrics) with its layer and the row's
  own descriptive text on one line (T-076, D1).

Annex B is never hand-written (the checklist says ``docs/metric_dictionary.md`` "is generated from it,
never written separately"). Run it with no arguments from the repo root.
"""

from __future__ import annotations

import csv
import hashlib
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CHECKLIST = REPO / "docs" / "checklist_sec" / "checklist_v1.3.md"
DICTIONARY = REPO / "docs" / "metric_dictionary.md"
MATRIX = REPO / "src" / "fundamental_agent" / "data" / "applicability_matrix.csv"
DEFINITIONS = REPO / "src" / "fundamental_agent" / "data" / "metric_definitions.csv"
RULES = REPO / "src" / "fundamental_agent" / "data" / "checklist_rules.csv"

# The six company-type columns of the matrix, in the checklist's order.
COMPANY_TYPES = (
    "art_9_filer",
    "art_7_insurer",
    "other_financial",
    "reit",
    "negative_equity",
    "multi_class",
)
COMPANY_TYPE_HEADERS = (
    "Art. 9 filers",
    "Art. 7 insurers",
    "Other financial",
    "REITs",
    "Neg. equity",
    "Multi-class",
)
ANNEX_COLUMNS = 7  # ID, Metric, Source, Quote, Formula, Basis, Note
MIN_RULE_CELLS = (
    2  # an ID and the row's own text (the "Verifiable rule" / Limitation / Screen / metric)
)
_MARKER_RE = re.compile(r"\s*\*\((?P<kind>[^)]*)\)\*")
_CELL_RE = re.compile(r"^(?P<code>A|ADJ|NA)\b\s*(?:\((?P<rule>.*)\))?")
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")
_MARKDOWN = ("**", "`")

# The header row of each checklist table whose rows go into ``checklist_rules.csv``; the row's own
# descriptive text is its second cell (the "Verifiable rule" of a layer, the Limitation, the Screen,
# the metric).
_RULE_HEADER = ("ID", "Verifiable rule")
_LIMITATION_HEADER = ("ID", "Limitation")
_SCREEN_HEADER = ("ID", "Screen")
_ANNEX_HEADER = ("ID", "Metric", "Source (PDF page)")
_RULE_ID_RE = re.compile(r"^(?:ID|PER|CON|DQ|MET|MKT|APP)-\d+[a-z0-9-]*$")
_LIMITATION_ID_RE = re.compile(r"^L-\d+$")
_SCREEN_ID_RE = re.compile(r"^A-\d+$")
_METRIC_ID_RE = re.compile(r"^MD-\d+$")


def _cells(line: str) -> list[str]:
    """The cells of one markdown table row, unescaping the checklist's ``\\|`` inside a cell."""
    return [c.strip().replace("\\|", "|") for c in _CELL_SPLIT_RE.split(line.strip().strip("|"))]


def _plain(text: str) -> str:
    """A checklist cell as one plain line: markdown emphasis and code spans removed, whitespace
    collapsed. Underscores and single asterisks are kept -- they are part of metric names
    (``net_margin``), not emphasis."""
    for mark in _MARKDOWN:
        text = text.replace(mark, "")
    return " ".join(text.split())


def _split_kind(metric: str) -> tuple[str, str]:
    """``"ebitda *(building block)*"`` -> ``("ebitda", "building block")``."""
    match = _MARKER_RE.search(metric)
    kind = match.group("kind") if match else ""
    return _MARKER_RE.sub("", metric).strip(), kind


def parse_annex_b(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        if not line.startswith("| MD-"):
            continue
        cells = _cells(line)
        if len(cells) < ANNEX_COLUMNS:
            continue
        metric, kind = _split_kind(cells[1])
        rows.append(
            {
                "id": cells[0],
                "metric": metric,
                "kind": kind,
                "source": cells[2],
                "quote": cells[3],
                "formula": cells[4],
                "basis": cells[5],
                "note": cells[6],
            }
        )
    return rows


def parse_matrix(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    in_matrix = False
    for line in text.splitlines():
        if line.startswith("| Metric | Art. 9"):
            in_matrix = True
            continue
        if not in_matrix:
            continue
        if not line.startswith("|"):
            break
        cells = _cells(line)
        if len(cells) != len(COMPANY_TYPES) + 1 or cells[0].startswith("---"):
            continue
        metric, _kind = _split_kind(cells[0])
        for company_type, cell in zip(COMPANY_TYPES, cells[1:], strict=True):
            match = _CELL_RE.match(cell)
            rows.append(
                {
                    "metric": metric,
                    "company_type": company_type,
                    "code": match.group("code") if match else cell,
                    "rule": (match.group("rule") or "") if match else "",
                }
            )
    return rows


def _table_rows(lines: list[str], header: tuple[str, ...]) -> list[list[str]]:
    """The rows of the table whose header row starts with *header*, in order."""
    rows: list[list[str]] = []
    inside = False
    for line in lines:
        if not line.startswith("|"):
            inside = False
            continue
        cells = _cells(line)
        if tuple(cells[: len(header)]) == header:
            inside = True
            continue
        if inside:
            rows.append(cells)
    return rows


def parse_rules(text: str) -> list[dict[str, str]]:
    """Every rule ID of the checklist, with its layer and the row's own text on one line (T-076, D1).

    The seven rule layers (ID, PER, CON, DQ, MET, MKT, APP), section L's declared limitations, Annex
    A's calibrated screens and Annex B's metric rows. ``rule`` is the row's own descriptive cell
    whole -- the "Verifiable rule" of a layer, the Limitation, the Screen, the metric name.
    """
    lines = text.splitlines()
    rows: list[dict[str, str]] = []
    for cells in _table_rows(lines, _RULE_HEADER):
        if len(cells) >= MIN_RULE_CELLS and _RULE_ID_RE.match(cells[0]):
            rows.append(
                {"id": cells[0], "layer": cells[0].split("-", 1)[0], "rule": _plain(cells[1])}
            )
    for cells in _table_rows(lines, _LIMITATION_HEADER):
        if len(cells) >= MIN_RULE_CELLS and _LIMITATION_ID_RE.match(cells[0]):
            rows.append({"id": cells[0], "layer": "L", "rule": _plain(cells[1])})
    for cells in _table_rows(lines, _SCREEN_HEADER):
        if len(cells) >= MIN_RULE_CELLS and _SCREEN_ID_RE.match(cells[0]):
            rows.append({"id": cells[0], "layer": "A", "rule": _plain(cells[1])})
    for cells in _table_rows(lines, _ANNEX_HEADER):
        if len(cells) >= MIN_RULE_CELLS and _METRIC_ID_RE.match(cells[0]):
            metric, _kind = _split_kind(cells[1])
            rows.append({"id": cells[0], "layer": "MD", "rule": _plain(metric)})
    return rows


def write_dictionary(annex: list[dict[str, str]], checklist_sha: str, version: str) -> None:
    lines = [
        "# Metric dictionary",
        "",
        "**Generated by `scripts/build_metric_dictionary.py` from Annex B of",
        f"`docs/checklist_sec/checklist_v1.3.md` — do not edit by hand.** Checklist version {version};",
        f"checklist file sha256 `{checklist_sha}`.",
        "",
        f"Annex B: {len(annex)} rows. **Basis:** *Sourced* — the source states the formula; *Inference*",
        "— it follows from a source (the note says how); *Source pending* — no loaded source states it,",
        "so the code keeps today's formula until the second author is loaded (checklist D-05). The",
        "rows marked *(building block)* are shared by several metrics; the rows marked *(derived)* or",
        "*(VALORIZATION)* are not standalone `fundamental_metrics` names.",
        "",
        "| ID | Metric | Kind | Formula | Basis | Source | Quote | Note |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in annex:
        cells = [
            row["id"],
            f"`{row['metric']}`",
            row["kind"],
            row["formula"].replace("|", "\\|"),
            f"**{row['basis']}**",
            row["source"],
            row["quote"].replace("|", "\\|"),
            row["note"].replace("|", "\\|"),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        "## Source pending (the second author's brief)",
        "",
        "The rows whose **Basis** is *Source pending*: the code keeps today's formula until the second",
        "author is loaded, before the thesis-annex version is generated (checklist D-05).",
        "",
    ]
    pending = [r for r in annex if r["basis"] == "Source pending"]
    lines += [f"- `{r['metric']}` — {r['formula'].replace('|', chr(92) + '|')}" for r in pending]
    lines.append("")
    DICTIONARY.write_text("\n".join(lines))


def write_matrix(matrix: list[dict[str, str]]) -> None:
    MATRIX.parent.mkdir(parents=True, exist_ok=True)
    with MATRIX.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["metric", "company_type", "code", "rule"])
        writer.writeheader()
        writer.writerows(matrix)


def write_definitions(annex: list[dict[str, str]]) -> None:
    """``metric_definitions.csv`` -- one row per Annex B row (T-076, D1)."""
    with DEFINITIONS.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["metric", "md_id", "formula", "basis"], lineterminator="\n"
        )
        writer.writeheader()
        for row in annex:
            writer.writerow(
                {
                    "metric": _plain(row["metric"]),
                    "md_id": row["id"],
                    "formula": _plain(row["formula"]),
                    "basis": _plain(row["basis"]),
                }
            )


def write_rules(rules: list[dict[str, str]]) -> None:
    """``checklist_rules.csv`` -- every rule ID with its layer and its own one-line text (T-076, D1)."""
    with RULES.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "layer", "rule"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(rules)


def main() -> None:
    text = CHECKLIST.read_text()
    annex = parse_annex_b(text)
    matrix = parse_matrix(text)
    sha = hashlib.sha256(CHECKLIST.read_bytes()).hexdigest()
    version = ""
    for line in text.splitlines():
        if line.startswith("# SEC data-treatment checklist"):
            version = line.split("—")[-1].split("(")[0].strip()
            break
    write_dictionary(annex, sha, version)
    write_matrix(matrix)
    write_definitions(annex)
    rules = parse_rules(text)
    write_rules(rules)
    bases: dict[str, int] = {}
    for row in annex:
        bases[row["basis"]] = bases.get(row["basis"], 0) + 1
    layers: dict[str, int] = {}
    for row in rules:
        layers[row["layer"]] = layers.get(row["layer"], 0) + 1
    print(f"Annex B: {len(annex)} rows {bases}")
    print(f"matrix: {len(matrix)} rows, {len({r['metric'] for r in matrix})} metrics")
    print(f"rules: {len(rules)} rows {layers}")
    print(f"wrote {DICTIONARY}, {MATRIX}, {DEFINITIONS} and {RULES} (checklist sha256 {sha[:16]}…)")


if __name__ == "__main__":
    main()
