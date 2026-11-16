"""Append the us-gaap working-capital ``IncreaseDecrease...`` elements the regulated utilities file to B6.

    uv run python scripts/add_b6_utility_elements.py --db <copy.db> [--zip-root PATH] [--check]

T-071 part 2a (D3, reviewer A, 2026-10-14). The plan's D-13 rule needs the working-capital
``IncreaseDecrease...`` lines of the regulated utilities' 10-Ks classified "current" or "left in the
cash flow" from their taxonomy definition -- but B6's extract held **no** such element, so the
classification had no source. The director authorized appending each one to B6, verbatim from the
pinned FASB 2026 zip, in the T-148/T-150 pattern (standard label, definition, type, balance, period;
the ASC references are not extracted, exactly as the T-148 N23 section).

This script reads the copy (read-only) for the distinct ``us-gaap_IncreaseDecrease...`` concepts the
29 regulated utilities' latest 10-Ks file, extracts each element's official text from the pinned zip,
appends a new section to ``docs/checklist_sec/sec documentation/B6_FASB_GAAP_Taxonomy_element_definitions.md``
and updates B6's SHA-256 and element count in ``docs/checklist_sec/sources_register.md``. Those two
files are the only ``docs/checklist_sec/`` edits it makes. Idempotent: an element already in B6 is
skipped. ``--check`` reports what it would do and writes nothing.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
B6 = (
    REPO
    / "docs"
    / "checklist_sec"
    / "sec documentation"
    / "B6_FASB_GAAP_Taxonomy_element_definitions.md"
)
REGISTER = REPO / "docs" / "checklist_sec" / "sources_register.md"
PROFILE_CSV = REPO / "src" / "kg_schema" / "data" / "company_profile.csv"
DEFAULT_ZIP_ROOT = Path("/workspaces/thesis/scratch/t148b/fasb2026/us-gaap-2026/elts")
ZIP_SHA = "f4c8b8b5697ba7d825a8614b159611cd25a46640e98a9737cda1e4a672bd4c81"
SECTION_HEADING = "## us-gaap — Working-capital Increase (Decrease) elements added for T-071 part 2a (D-13), 2026-10-14"
ANCHOR = "## Calculation relationships (US GAAP Taxonomy 2026 calculation linkbase)"

_ELEMENT_RE = re.compile(r"<xs:element\s(?P<attrs>[^>]*?)/>")
_ID_RE = re.compile(r"id='([^']*)'")
_TYPE_RE = re.compile(r"type='([^']*)'")
_BALANCE_RE = re.compile(r"balance='([^']*)'")
_PERIOD_RE = re.compile(r"xbrli:periodType='([^']*)'")
_LABEL_RE = re.compile(
    r"<link:label[^>]*xlink:label='lab_(?P<el>[^']*)'[^>]*"
    r"xlink:role='http://www\.xbrl\.org/2003/role/label'[^>]*>(?P<text>.*?)</link:label>",
    re.DOTALL,
)
_DOC_RE = re.compile(
    r"<link:label[^>]*xlink:label='lab_(?P<el>[^']*)'[^>]*"
    r"xlink:role='http://www\.xbrl\.org/2003/role/documentation'[^>]*>(?P<text>.*?)</link:label>",
    re.DOTALL,
)


def _regulated_utilities() -> dict[str, str]:
    """CIK (no leading zeros) -> ticker, from the tracked APP-00 profile."""
    lines = [
        line
        for line in PROFILE_CSV.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    return {
        (row["cik"].lstrip("0") or "0"): row["tickers"]
        for row in csv.DictReader(lines)
        if row.get("regulated_utility", "").strip().lower() == "true"
    }


def _elements(db_path: str) -> list[str]:
    """The distinct ``us-gaap_IncreaseDecrease...`` concepts the utilities' latest 10-Ks file."""
    utilities = _regulated_utilities()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cik_to_asset = {
            str(int(cik)): aid for aid, cik in conn.execute("SELECT id, cik FROM assets") if cik
        }
        found: set[str] = set()
        for cik in utilities:
            aid = cik_to_asset.get(cik)
            if aid is None:
                continue
            row = conn.execute(
                "SELECT id FROM sec_filings WHERE asset_id = ? AND form = '10-K' "
                "ORDER BY period_end DESC LIMIT 1",
                (aid,),
            ).fetchone()
            if row is None:
                continue
            found.update(
                concept
                for (concept,) in conn.execute(
                    "SELECT DISTINCT concept FROM financial_facts WHERE filing_id = ? "
                    "AND concept LIKE 'us-gaap_IncreaseDecrease%'",
                    (row[0],),
                )
            )
    finally:
        conn.close()
    return sorted(found)


def _attr(pattern: re.Pattern[str], attrs: str) -> str:
    match = pattern.search(attrs)
    return match.group(1) if match else ""


def _schema_attrs(xsd: Path) -> dict[str, dict[str, str]]:
    text = xsd.read_text(encoding="utf-8")
    out: dict[str, dict[str, str]] = {}
    for match in _ELEMENT_RE.finditer(text):
        attrs = match.group("attrs")
        element_id = _ID_RE.search(attrs)
        if element_id is None or not element_id.group(1).startswith("us-gaap_"):
            continue
        out[element_id.group(1).removeprefix("us-gaap_")] = {
            "type": _attr(_TYPE_RE, attrs),
            "balance": _attr(_BALANCE_RE, attrs),
            "period": _attr(_PERIOD_RE, attrs),
        }
    return out


def _labels(path: Path, pattern: re.Pattern[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in pattern.finditer(path.read_text(encoding="utf-8")):
        out[match.group("el")] = match.group("text").strip()
    return out


def _entry(
    element: str, attrs: dict[str, str], labels: dict[str, str], docs: dict[str, str]
) -> str:
    lines = [f"### us-gaap:{element} (US GAAP Taxonomy 2026)", ""]
    label = labels.get(element)
    if label:
        lines.append(f"- **Standard label:** {label}")
    definition = docs.get(element)
    if definition:
        lines.append(f'- **Definition:** "{definition}"')
    type_ = attrs.get("type") or "—"
    balance = attrs.get("balance") or "—"
    period = attrs.get("period") or "—"
    lines.append(f"- **Type:** `{type_}` · **Balance:** {balance} · **Period:** {period}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", required=True, help="the copy to read the utilities' 10-K concepts from"
    )
    parser.add_argument(
        "--zip-root", default=str(DEFAULT_ZIP_ROOT), help="the pinned FASB 2026 elts/ dir"
    )
    parser.add_argument("--check", action="store_true", help="report only; write nothing")
    args = parser.parse_args()

    zip_root = Path(args.zip_root)
    xsd = zip_root / "us-gaap-2026.xsd"
    lab = zip_root / "us-gaap-lab-2026.xml"
    doc = zip_root / "us-gaap-doc-2026.xml"
    for path in (xsd, lab, doc):
        if not path.exists():
            print(f"error: {path} does not exist", file=sys.stderr)
            return 1

    text = B6.read_text(encoding="utf-8")
    attrs = _schema_attrs(xsd)
    labels = _labels(lab, _LABEL_RE)
    docs = _labels(doc, _DOC_RE)
    wanted = [e.removeprefix("us-gaap_") for e in _elements(args.db)]
    missing = [e for e in wanted if e not in attrs]
    if missing:
        print(
            f"error: {len(missing)} elements are not in the schema: {missing[:5]}", file=sys.stderr
        )
        return 1
    todo = [e for e in wanted if f"### us-gaap:{e} (US GAAP Taxonomy 2026)" not in text]
    if not todo:
        print("B6 already holds every element; nothing to do")
        return 0

    before_headings = len(re.findall(r"^### us-gaap:|^### dei:", text, flags=re.MULTILINE))
    section = [SECTION_HEADING, "", _intro(len(todo)), ""]
    section.extend(_entry(e, attrs[e], labels, docs) for e in todo)
    addition = "\n".join(section).rstrip("\n") + "\n\n"
    if ANCHOR not in text:
        print(f"error: anchor {ANCHOR!r} not found", file=sys.stderr)
        return 1
    text = text.replace(ANCHOR, addition + ANCHOR, 1)
    after_headings = before_headings + len(todo)

    if args.check:
        print(f"would add {len(todo)} elements ({before_headings} -> {after_headings} headings)")
        print("\n".join(section)[:2000])
        return 0

    B6.write_text(text, encoding="utf-8")
    sha = hashlib.sha256(B6.read_bytes()).hexdigest()
    _update_register(before_headings, after_headings, sha)
    print(
        f"added {len(todo)} elements: {before_headings} -> {after_headings} headings; B6 sha256 {sha}"
    )
    return 0


def _intro(count: int) -> str:
    return (
        "Added from the same release file as the rest of this extract (`us-gaap-2026.zip`, SHA-256\n"
        f"`{ZIP_SHA}`, re-verified on download; `elts/us-gaap-2026.xsd`,\n"
        "`elts/us-gaap-lab-2026.xml`, `elts/us-gaap-doc-2026.xml`). These are the "
        f"{count} `IncreaseDecrease...`\n"
        "working-capital elements the 29 regulated utilities' latest 10-Ks file\n"
        "(`scripts/add_b6_utility_elements.py`); the ASC references were not extracted for them (the\n"
        "T-148 N23 pattern)."
    )


def _update_register(before: int, after: int, sha: str) -> None:
    text = REGISTER.read_text(encoding="utf-8")
    added = after - before
    text, count = re.subn(
        r"for the \d+ elements the checklist and the audit use",
        f"for the {after} elements the checklist and the audit use ({added} working-capital "
        "`IncreaseDecrease...` elements added 2026-10-14 for T-071 part 2a / D-13)",
        text,
        count=1,
    )
    if count == 0:
        print("warning: the register's element count was not found; update it by hand")
    # the B6 row's SHA-256 is the only 64-hex value on that row; replace the pinned one.
    old_sha = "3115c856825061441f8b2f5c0573a2b8a60c7041cebe135b3fb280193a2abfc9"
    if old_sha in text:
        text = text.replace(old_sha, sha, 1)
    else:
        print("warning: the register's B6 sha256 was not found; update it by hand")
    REGISTER.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
