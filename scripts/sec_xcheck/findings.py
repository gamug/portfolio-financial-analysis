"""One shape for every measurement of the audit: a count, its denominator, up to five examples, a command.

A :class:`Finding` is the unit the prevalence table is built from. ``level`` keeps the two evidence levels apart
(T-147): ``"a"`` the SEC source (``companyfacts`` / ``submissions``), ``"b"`` our resolver over the stored
``financial_facts`` (or the stored metrics themselves). A rule whose source prevalence cannot be measured from
``companyfacts`` (dimensional or custom facts) carries ``incomplete=True`` and is never rendered as zero.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

MAX_EXAMPLES = 5


@dataclass
class Finding:
    key: str
    rules: list[str]
    title: str
    level: str  # "a" | "b"
    count: int  # affected units (filings, or the unit named in ``unit``)
    total: int  # the population those units are drawn from
    companies: int
    unit: str = "filings"
    examples: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # (ticker, accession, detail)
    incomplete: bool = False
    note: str = ""
    command: str = ""
    db: str = "production"
    filings: int = (
        0  # distinct filings behind a count of filing-columns (F2); 0 = same as ``count``
    )
    target_count: int = -1  # the same count on the filings' own period only (-1: not column-based)
    target_total: int = -1
    target_companies: int = -1  # companies behind ``target_count``

    def share(self) -> float:
        return self.count / self.total if self.total else float("nan")

    def row(self) -> str:
        """A markdown table row: key, rules, level, count/total, companies, examples."""
        shown = "; ".join(f"{t} {a} {d}".strip() for t, a, d in self.examples[:MAX_EXAMPLES])
        count = f"{self.count:,} / {self.total:,} {self.unit}"
        if self.filings and self.unit == "filing-columns":
            count += f" ({self.filings:,} filings"
            count += (
                f"; own period {self.target_count:,}/{self.target_total:,})"
                if self.target_count >= 0
                else ")"
            )
        flag = " **source prevalence incomplete**" if self.incomplete else ""
        return (
            f"| `{self.key}` | {', '.join(self.rules)} | {self.level} | {self.db} | {count} | {self.companies} | "
            f"{shown or '—'}{flag} |"
        )


def spread(examples: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Up to five examples, one per ticker first (so the sample shows five companies, not five filings of one)."""
    seen: set[str] = set()
    first: list[tuple[str, str, str]] = []
    rest: list[tuple[str, str, str]] = []
    for ex in examples:
        (rest if ex[0] in seen else first).append(ex)
        seen.add(ex[0])
    return (first + rest)[:MAX_EXAMPLES]


def to_json(findings: list[Finding]) -> str:
    return json.dumps([asdict(f) for f in findings], indent=1, default=str)


def from_json(text: str) -> list[Finding]:
    out = []
    for item in json.loads(text):
        item["examples"] = [tuple(e) for e in item["examples"]]
        out.append(Finding(**item))
    return out


TABLE_HEADER = (
    "| Measurement | Rules | Level | Database | Affected / population | Companies | Examples (ticker accession detail) |\n"
    "|---|---|---|---|---|---|---|"
)


def markdown(findings: list[Finding]) -> str:
    return "\n".join([TABLE_HEADER, *(f.row() for f in findings)])


def tally(items: list[Any], key: Any) -> dict[Any, int]:
    out: dict[Any, int] = {}
    for it in items:
        out[key(it)] = out.get(key(it), 0) + 1
    return out
