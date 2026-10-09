"""ID-13, ID-14 and ID-15 on the registered DQC element lists (level (a)): negative values in the SEC ``companyfacts``.

For every stored filing, the non-dimensional ``us-gaap`` facts of its accession (every period the filing reports):

* **ID-13 / DQC_0015**: a negative value of an element on the list of any us-gaap release 2020-2026;
* **ID-14 / DQC_0013**: a negative value of a listed element when the precondition holds (pre-tax income > 0 in the same
  period; all 19 listed elements share it);
* **ID-15 / DQC_0014**: a negative value of a listed element filed without dimensions (``companyfacts`` has none other).

Each rule's count is also split by whether the element is one the resolver reads (``REGISTRY``): a violation there is a wrong
input; elsewhere it is a defect of the filing the pipeline never uses. The proxy of ``source_measures`` stays as a
cross-check.
"""

from __future__ import annotations

import collections
from pathlib import Path
from typing import Any

from sec_xcheck import dqc_lists
from sec_xcheck.common import SecClient
from sec_xcheck.findings import Finding, spread
from sec_xcheck.itemcheck import own_concepts
from sec_xcheck.records import Rec
from sec_xcheck.source_measures import load_index

CMD = "uv run python scripts/audit_sec_checklist.py measure --only dqc"
PRETAX = (
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
)


def pretax_positive(
    facts: dict[tuple[str, str], float], end: str, start: str | None
) -> bool | None:
    """DQC_0013's precondition for one period: pre-tax income > 0 (``None`` when none of its definitions is filed)."""
    for name in PRETAX:
        if (name, f"{start}|{end}") in facts:
            value = facts[(name, f"{start}|{end}")]
            if name == PRETAX[1]:
                value += facts.get(("IncomeLossFromEquityMethodInvestments", f"{start}|{end}"), 0.0)
            return value > 0
    return None


class RuleTally:
    def __init__(self) -> None:
        self.filings: set[int] = set()
        self.cos: set[str] = set()
        self.registry_filings: set[int] = set()
        self.facts = 0
        self.concepts: collections.Counter[str] = collections.Counter()
        self.ex: list[tuple[str, str, str]] = []

    def add(self, r: Rec, concept: str, fact: dict[str, Any], registry: set[str]) -> None:
        self.filings.add(r.filing_id)
        self.cos.add(r.ticker)
        self.facts += 1
        self.concepts[concept] += 1
        if concept in registry:
            self.registry_filings.add(r.filing_id)
        self.ex.append(
            (
                r.ticker,
                r.accession,
                f"{concept} {fact.get('start') or ''}..{fact['end']} = {fact['val']:,.0f}",
            )
        )


def scan_filing(
    r: Rec, index: Any, lists: dict[str, Any], registry: set[str], tallies: dict[str, RuleTally]
) -> None:
    facts_in = index.get(r.accession, [])
    periods = {
        (c, f"{f.get('start')}|{f['end']}"): f["val"]
        for _t, c, f in facts_in
        if PRETAX[0] == c or c in PRETAX or c == "IncomeLossFromEquityMethodInvestments"
    }
    for _tax, concept, fact in facts_in:
        if fact["val"] >= 0:
            continue
        if concept in lists["dqc15"]:
            tallies["ID-13"].add(r, concept, fact, registry)
        if concept in lists["dqc14"]:
            tallies["ID-15"].add(r, concept, fact, registry)
        if concept in lists["dqc13"] and pretax_positive(periods, fact["end"], fact.get("start")):
            tallies["ID-14"].add(r, concept, fact, registry)


def measure(recs: list[Rec], client: SecClient, lists: dict[str, Any]) -> list[Finding]:
    registry = {c for cs in own_concepts().values() for c in cs}
    tallies = {rule: RuleTally() for rule in ("ID-13", "ID-14", "ID-15")}
    targets = [r for r in recs if r.role == "target"]
    by_cik: dict[str, list[Rec]] = collections.defaultdict(list)
    for r in targets:
        by_cik[r.cik].append(r)
    for cik, group in by_cik.items():
        index = load_index(client, cik)
        for r in group:
            scan_filing(r, index, lists, registry, tallies)
    titles = {
        "ID-13": "a negative value of an element on the DQC_0015 lists (us-gaap 2020-2026)",
        "ID-14": "a negative value of a DQC_0013 element when pre-tax income > 0",
        "ID-15": "a negative non-dimensional value of a DQC_0014 element",
    }
    out: list[Finding] = []
    for rule, t in tallies.items():
        top = ", ".join(f"{c} {n}" for c, n in t.concepts.most_common(5))
        out.append(
            Finding(
                f"src.{rule}.negative_listed_element", [rule], f"Source: filings with {titles[rule]}", "a", len(t.filings), len(targets),
                len(t.cos), examples=spread(t.ex), command=CMD,
                note=f"{t.facts} facts; most frequent: {top or 'none'}; {len(t.registry_filings)} of these filings are on an element the resolver reads",
            )
        )  # fmt: skip
    return out


def load_lists(directory: Path) -> dict[str, Any]:
    return {
        "dqc15": set().union(*dqc_lists.dqc15(directory).values()),
        "dqc14": dqc_lists.dqc14(directory),
        "dqc13": dqc_lists.dqc13(directory),
    }
