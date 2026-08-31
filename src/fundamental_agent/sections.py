"""Deterministic Item splitter for 10-K / 10-Q filing HTML.

Pulls the narrative sections the graph cares about (MD&A, Risk Factors, Business,
Legal Proceedings) out of a primary filing document. Pure and fixture-testable --
no network, no LLM.

Two heading conventions are handled, because SEC filers split evenly between them:

* ``Item 7.`` line markers -- the common case; and
* descriptive titles only (``MANAGEMENT'S DISCUSSION AND ANALYSIS OF ...``), used by
  most banks and several large industrials, whose ``Item`` numbers live only in a
  front cross-reference table.

For each wanted section every candidate heading -- an ``Item`` marker *or* a title
line -- is scored by how much text follows it before the next *different* section
heading; the richest occurrence wins, which rejects table-of-contents rows (each is
followed immediately by the next row) and page running-headers alike.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, NavigableString, Tag

# form -> ordered {item_number: (section_type, title-core regex)}. The title core is
# matched case-insensitively; "." stands in for the straight/curly apostrophe.
_SPECS: dict[str, dict[str, tuple[str, str]]] = {
    "10-K": {
        "1": ("BUSINESS", r"business"),
        "1A": ("RISK_FACTORS", r"risk factors"),
        "3": ("LEGAL_PROCEEDINGS", r"legal proceedings"),
        "7": ("MD&A", r"management.s discussion and analysis"),
    },
    "10-Q": {
        "1A": ("RISK_FACTORS", r"risk factors"),
        "2": ("MD&A", r"management.s discussion and analysis"),
    },
}

_MIN_SECTION_CHARS = 400
# A handful of large financial-sector filers carry no ``Item`` line markers at all,
# and put the next section's title too deep to bound the current one -- clamp the
# body so a mis-bounded section can never swallow the rest of the document. Real
# 10-K sections top out well under this (~45k words).
_MAX_SECTION_CHARS = 400_000

# Block-level tags force a newline when flattening; every other tag (span, a, b, i,
# font, sup, ...) is joined tight, so a heading chopped mid-word across inline tags
# -- a filer trick, e.g. "Ite" + "m 2." -- is glued back into "Item 2.".
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "caption",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
_HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
_IX_DROP_TAGS = frozenset({"ix:header", "ix:hidden", "ix:references", "ix:resources"})

_NON_NL_WS_RE = re.compile(r"[^\S\n]+")
_BLANK_LINE_RE = re.compile(r"\n[ \t]*(?:\n[ \t]*)+")

# Optional "Part II." / "Part IV" prefix that some filers put on the same line.
_PART_PREFIX = r"(?:part[ \t]+(?:[ivx]+|\d)[ \t]*[.:\u2013\u2014)-]?[ \t]*)?"
_ITEM_PREFIX = r"(?:item[ \t]*\d{1,2}[a-z]?[ \t]*[.:\u2013\u2014)-]?[ \t]*)?"
# Runs only to the end of the heading's own line -- no trailing "[ \t\n]*" that
# could swallow the newline and the first line of the section body.
_TITLE_SUFFIX = (
    r"(?:[ \t\n]+of[ \t\n]+financial[ \t\n]+condition"
    r"(?:[ \t\n]+and[ \t\n]+results[ \t\n]+of[ \t\n]+operations)?)?"
    r"[ \t]*(?:\((?:item[ \t]*\d{1,2}[a-z]?|md&a|continued|unaudited|a)\))?"
    r"[ \t]*[.:]?[ \t]*(?:\d+)?$"
)

# A line that opens with an Item marker: "Item 7.", "ITEM 1A -", "Item 2.Management".
_ITEM_LINE_RE = re.compile(
    r"(?im)^[ \t>]*"
    + _PART_PREFIX
    + r"item[ \t]*(\d{1,2}[a-z]?)\b[ \t]*[.:\u2013\u2014)\-]?[ \t]*.*$"
)
# A structural "PART I / PART II" divider line -- always a hard section boundary.
_PART_LINE_RE = re.compile(r"(?im)^[ \t>]*part[ \t]+(?:[ivx]+|\d)\b")


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
    """Flatten filing HTML to text, one visible line per block-level element."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", *_IX_DROP_TAGS]):
        tag.decompose()
    for tag in soup.find_all(style=_HIDDEN_STYLE_RE):
        tag.decompose()

    parts: list[str] = []
    for node in soup.descendants:
        if isinstance(node, NavigableString):
            chunk = str(node)
            if chunk.strip():
                parts.append(chunk.replace("\xa0", " "))
        elif isinstance(node, Tag) and node.name in _BLOCK_TAGS:
            parts.append("\n")

    text = _NON_NL_WS_RE.sub(" ", "".join(parts))
    text = _BLANK_LINE_RE.sub("\n", text)
    return "\n".join(line.strip() for line in text.split("\n") if line.strip())


def _title_line_re(title_core: str) -> re.Pattern[str]:
    flexible = title_core.replace(" ", r"[ \t\n]+")
    return re.compile(r"(?im)^[ \t>]*" + _PART_PREFIX + _ITEM_PREFIX + flexible + _TITLE_SUFFIX)


_TOC_SCAN_CHARS = 1500
_TOC_MIN_MARKERS = 6
_TOC_MAX_BODY = 4000
_TOC_MARKER_RE = re.compile(r"(?im)^(?:item[ \t]*\d|part[ \t]+(?:[ivx]+|\d))\b")


def _looks_like_toc(body: str) -> bool:
    markers = len(_TOC_MARKER_RE.findall(body[:_TOC_SCAN_CHARS]))
    return markers >= _TOC_MIN_MARKERS and len(body) < _TOC_MAX_BODY


def _first_line(fragment: str) -> str:
    for line in fragment.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _clamp(body: str) -> str:
    if len(body) <= _MAX_SECTION_CHARS:
        return body
    cut = body.rfind("\n", _MAX_SECTION_CHARS // 2, _MAX_SECTION_CHARS)
    return body[: cut if cut != -1 else _MAX_SECTION_CHARS].rstrip()


def _best_body(
    text: str,
    candidates: list[tuple[int, int]],
    boundaries: list[int],
    own: set[int],
) -> tuple[int, int, int]:
    """Pick the (head_start, body_start, body_end) whose body -- the span to the next
    heading that isn't this section's -- holds the most text. That rejects
    table-of-contents rows and page running-headers, whose next heading is adjacent.
    """
    best: tuple[int, int, int, int] | None = None
    for head_start, body_start in candidates:
        body_end = len(text)
        for boundary in boundaries:
            if boundary > body_start and boundary not in own:
                body_end = boundary
                break
        body_len = len(text[body_start:body_end].strip())
        if best is None or body_len > best[0]:
            best = (body_len, head_start, body_start, body_end)
    assert best is not None
    return best[1], best[2], best[3]


def split_sections(html: str, form: str) -> list[Section]:
    """Extract the wanted narrative sections from *html* for *form* ('10-K'/'10-Q')."""
    spec = _SPECS.get(form.upper())
    if spec is None:
        return []
    text = html_to_text(html)
    if not text:
        return []

    item_marks = [(m.start(), m.end(), m.group(1).upper()) for m in _ITEM_LINE_RE.finditer(text)]
    title_marks = {
        number: [(m.start(), m.end()) for m in _title_line_re(core).finditer(text)]
        for number, (_section_type, core) in spec.items()
    }

    # Every heading of any wanted kind, plus structural PART dividers, is a boundary.
    boundaries = {s for s, _e, _n in item_marks}
    boundaries |= {m.start() for m in _PART_LINE_RE.finditer(text)}
    for marks in title_marks.values():
        boundaries |= {s for s, _e in marks}
    ordered_boundaries = sorted(boundaries)

    sections: list[Section] = []
    for number, (section_type, _core) in spec.items():
        candidates = [(s, e) for s, e, n in item_marks if n == number]
        candidates += title_marks[number]
        if not candidates:
            continue
        own = {s for s, _e, n in item_marks if n == number}
        own |= {s for s, _e in title_marks[number]}

        head_start, body_start, body_end = _best_body(text, candidates, ordered_boundaries, own)
        body = _clamp(text[body_start:body_end].strip())
        if len(body) < _MIN_SECTION_CHARS or _looks_like_toc(body):
            continue
        sections.append(
            Section(
                section_type=section_type,
                item_number=number,
                heading=_first_line(text[head_start:])[:200],
                ordinal=len(sections),
                char_start=head_start,
                char_end=body_start + len(body),
                text=body,
            )
        )
    return sections
