"""Deterministic Item splitter for 10-K / 10-Q filing HTML.

Pulls the narrative sections the graph cares about (MD&A, Risk Factors, Business,
Legal Proceedings) out of a primary filing document. Pure and fixture-testable --
no network, no LLM. Table-of-contents hits are rejected by preferring, for each
Item number, the occurrence with the most text before the next Item header.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

# form -> {item_number: (section_type, title keyword)}
_SPECS: dict[str, dict[str, tuple[str, str]]] = {
    "10-K": {
        "1": ("BUSINESS", "business"),
        "1A": ("RISK_FACTORS", "risk factors"),
        "3": ("LEGAL_PROCEEDINGS", "legal proceedings"),
        "7": ("MD&A", "management's discussion"),
    },
    "10-Q": {
        "1A": ("RISK_FACTORS", "risk factors"),
        "2": ("MD&A", "management's discussion"),
    },
}

# Canonical ``itemLabel`` vocabulary for the KG projection: section_type -> the
# token that follows ``ITEM_<n>_`` in the label. ``v_sec_filing_section`` builds
# the same string in SQL; keep the two in sync (a test pins them together).
_ITEM_LABEL_TOKENS: dict[str, str] = {
    "MD&A": "MDA",
    "RISK_FACTORS": "RISK_FACTORS",
    "BUSINESS": "BUSINESS",
    "LEGAL_PROCEEDINGS": "LEGAL_PROCEEDINGS",
}


def canonical_item_label(item_number: str | None, section_type: str) -> str:
    """Map a stored ``(item_number, section_type)`` to the ontology's ``itemLabel``.

    ``("1A", "RISK_FACTORS") -> "ITEM_1A_RISK_FACTORS"``;
    ``("7", "MD&A") -> "ITEM_7_MDA"``. With no item number the label is just the
    section token. Deterministic and total -- an unknown ``section_type`` is
    upper-cased with ``&`` -> ``AND`` and spaces -> ``_``.
    """
    token = _ITEM_LABEL_TOKENS.get(
        section_type, section_type.upper().replace(" ", "_").replace("&", "AND")
    )
    number = (item_number or "").strip().upper()
    return f"ITEM_{number}_{token}" if number else token


# "Item 7." / "ITEM 1A" / "Item 2:" at a line start. The separator class covers
# ".", ":", ")", hyphen, and the U+2013 / U+2014 dash code points filers use.
_SEP = "[.:)\u2013\u2014-]?"
_ITEM_RE = re.compile(r"(?im)^[ \t>]*item\s+(\d{1,2}[a-z]?)\s*" + _SEP + r"\s*(.*)$")
_MIN_SECTION_CHARS = 400


@dataclass(frozen=True)
class Section:
    """One extracted narrative section."""

    section_type: str
    item_number: str
    heading: str
    ordinal: int
    char_start: int
    char_end: int
    text: str

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def html_to_text(html: str) -> str:
    """Flatten filing HTML to newline-separated visible text."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n")
    # collapse runs of blank lines / trailing spaces
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line.strip() != "")


def _all_item_marks(text: str) -> list[tuple[int, str, str, int]]:
    """(start_offset, item_number, heading_line, match_end) for every Item header."""
    marks = []
    for m in _ITEM_RE.finditer(text):
        marks.append((m.start(), m.group(1).upper(), m.group(0).strip(), m.end()))
    return marks


def split_sections(html: str, form: str) -> list[Section]:
    """Extract the wanted narrative sections from *html* for *form* ('10-K'/'10-Q')."""
    spec = _SPECS.get(form.upper())
    if spec is None:
        return []
    text = html_to_text(html)
    marks = _all_item_marks(text)
    if not marks:
        return []

    starts = [m[0] for m in marks]
    # candidate spans: from each header's end to the next header's start
    by_item: dict[str, list[tuple[int, int, str, int]]] = {}
    for idx, (start, number, heading, end) in enumerate(marks):
        next_start = starts[idx + 1] if idx + 1 < len(marks) else len(text)
        by_item.setdefault(number, []).append((end, next_start, heading, start))

    sections: list[Section] = []
    ordinal = 0
    for number, (section_type, _kw) in spec.items():
        candidates = by_item.get(number, [])
        if not candidates:
            continue
        # the real section is the occurrence with the most following text
        body_start, body_end, heading, header_start = max(candidates, key=lambda c: c[1] - c[0])
        body = text[body_start:body_end].strip()
        if len(body) < _MIN_SECTION_CHARS:
            continue
        sections.append(
            Section(
                section_type=section_type,
                item_number=number,
                heading=heading[:200],
                ordinal=ordinal,
                char_start=header_start,
                char_end=body_end,
                text=body,
            )
        )
        ordinal += 1
    return sections
