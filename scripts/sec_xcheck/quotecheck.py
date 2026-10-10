"""Verify every quote of a checklist version against its source file, and generate the totals.

    uv run python scripts/sec_xcheck/quotecheck.py [CHECKLIST] [--sources DIR] [--write] [--quiet]

``CHECKLIST`` defaults to ``docs/checklist_sec/checklist_v1.3.md``. The source documents (``sources_register.md``:
URL, version, SHA-256) are read from ``--sources`` / ``SEC_XCHECK_SOURCES``; the PDFs are never committed.
Because verifying against the local PDFs requires the local PDF collection, this script is not a pytest test.

For each rule row and Annex B metric-dictionary row (| MD-nn |), each quoted fragment ("...") must be found
verbatim (after normalizing case, spacing and punctuation) in one of the files the row cites, on one of the
PDF pages it cites. Quotes containing an ellipsis ("…" or "...") are split into fragments and each fragment is
verified. Three extraction artifacts are tolerated and reported: PDF ligatures ("fi", "ff", "fl") dropped by
text extraction; "t" glyphs dropped in A1's section headings; and a fragment that spans a page break. With
``--write``, the totals table replaces the TOTALS marker in the checklist. Exit status 1 when any quote is not
verified. Needs ``pypdf``.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import glob
import logging
import os
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sec_xcheck.common import REPO, sources_dir

MARK = "<!-- TOTALS -->"
MIN_FRAGMENT = 12  # shorter pieces of an elided quote are too common to locate a page
MIN_CELLS = 9  # a rule row has at least the nine columns of the checklist tables
MIN_MD_CELLS = 6  # an Annex B metric-dictionary row has at least six columns
LAYERS = ["ID", "PER", "CON", "DQ", "MET", "MKT", "APP"]
DEFAULT_CHECKLIST = REPO / "docs" / "checklist_sec" / "checklist_v1.3.md"

_cache: dict[str, list[str]] = {}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def norm_lig(s: str) -> str:
    return re.sub(r"f[ifl]", "", norm(s))


def norm_t(s: str) -> str:
    return norm(s).replace("t", "")


def pages(path: str) -> list[str]:
    if path not in _cache:
        if path.endswith(".md"):
            _cache[path] = [Path(path).read_text(encoding="utf-8")]
        else:
            import pypdf  # noqa: PLC0415 - optional dependency, needed only to read the PDFs

            logging.disable(logging.CRITICAL)
            _cache[path] = [p.extract_text() or "" for p in pypdf.PdfReader(path).pages]
    return _cache[path]


def resolve(token: str, docs: Path) -> str:
    files = sorted(os.path.basename(f) for f in glob.glob(str(docs / "*")))
    hits = [f for f in files if f.startswith(token + "_") or os.path.splitext(f)[0] == token]
    if len(hits) != 1:
        raise SystemExit(f"cannot resolve source token {token!r} in {docs}: {hits}")
    return str(docs / hits[0])


def cited(source: str, docs: Path) -> list[tuple[str, set[int]]]:
    """[(path, {pages})] in the order the source cell names them."""
    out: dict[str, set[int]] = {}
    last: str | None = None
    for m in re.finditer(r"\b([A-E]\d+(?:_[A-Za-z0-9\-]+)*)|pp?\.\s?(\d+)(?:[–-](\d+))?", source):  # noqa: RUF001 - the sources use an en dash
        if m.group(1):
            last = resolve(m.group(1), docs)
            out.setdefault(last, set())
        elif last:
            lo = int(m.group(2))
            hi = int(m.group(3) or lo)
            out[last].update(range(lo, hi + 1))
    return list(out.items())


def find(fragment: str, srcs: list[tuple[str, set[int]]]) -> tuple[str, str]:
    """Return (status, where). status: OK, OK-ligature, OK-glyph-t, OK-span, PAGE, MISSING."""
    parts = [p for p in re.split(r"…|\.\.\.", fragment) if len(norm(p)) > MIN_FRAGMENT] or [
        fragment
    ]
    best = ("MISSING", "")
    for path, want in srcs:
        P = pages(path)
        is_md = path.endswith(".md")
        for fn, tag in ((norm, "OK"), (norm_lig, "OK-ligature"), (norm_t, "OK-glyph-t")):
            found = [sorted(k + 1 for k, t in enumerate(P) if fn(part) in fn(t)) for part in parts]
            if all(found):
                pg = sorted({p for f in found for p in f})
                where = os.path.basename(path)[:28] + ("" if is_md else f" p{pg}")
                if is_md or not want or set(pg) & want:
                    return tag, where
                best = ("PAGE", where + f" (cited {sorted(want)})")
            # A fragment spanning two consecutive pages.
            if not is_md:
                for k in range(len(P) - 1):
                    if all(fn(part) in fn(P[k] + " " + P[k + 1]) for part in parts) and (
                        not want or {k + 1, k + 2} & want
                    ):
                        return "OK-span", os.path.basename(path)[:28] + f" p{k + 1}-{k + 2}"
    return best


def rows(text: str):
    for line in text.splitlines():
        m = re.match(rf"\|\s*((?:{'|'.join(LAYERS)})-\w+(?:-\d+)?)\s*\|", line)
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]]
        if m and len(cells) >= MIN_CELLS:
            yield m.group(1), cells


@dataclasses.dataclass
class RuleCheckSummary:
    per_layer: collections.Counter[str] = dataclasses.field(default_factory=collections.Counter)
    sev: collections.Counter[str] = dataclasses.field(default_factory=collections.Counter)
    n_quotes: int = 0
    problems: int = 0


@dataclasses.dataclass
class AnnexBCheckSummary:
    count: int = 0
    basis: collections.Counter[str] = dataclasses.field(default_factory=collections.Counter)
    quotes: int = 0
    bad: int = 0


def md_rows(text: str):
    for line in text.splitlines():
        if not re.match(r"\|\s*MD-\d+\s*\|", line):
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]]
        if len(cells) >= MIN_MD_CELLS:
            yield cells[0], cells


def totals_table(
    text: str,
    rules: RuleCheckSummary,
    annex_b: AnnexBCheckSummary | None = None,
) -> str:
    lines = ["| Layer | Rules |", "|---|---|"]
    lines += [f"| {k} | {rules.per_layer[k]} |" for k in LAYERS]
    lines += [f"| **All** | **{sum(rules.per_layer.values())}** |", ""]
    limitations = len(re.findall(r"(?m)^\| L-\d+ \|", text))
    screens = len(re.findall(r"(?m)^\| A-\d+ \|", text))
    lines += [
        f"- **Quoted fragments:** {rules.n_quotes}; verified against the files: {rules.n_quotes - rules.problems}.",
        "- **Severity:** " + ", ".join(f"{k} {v}" for k, v in sorted(rules.sev.items())) + ". "
        '"DQ-01" means the identity\'s severity follows DQ-01 (WARN above rounding, BLOCK above materiality).',
        f"- **Outside the rules:** {limitations} declared limitations (section L) and "
        f"{screens} calibrated screens (Annex A).",
    ]
    if annex_b and annex_b.count:
        md_basis_str = ", ".join(f"{k} {v}" for k, v in sorted(annex_b.basis.items()))
        lines.append(
            f"- **Annex B (metric dictionary):** {annex_b.count} rows ({md_basis_str}); quoted fragments: {annex_b.quotes}; verified: {annex_b.quotes - annex_b.bad}."
        )
    return "\n".join(lines)


def check_rules(text: str, docs: Path, quiet: bool) -> RuleCheckSummary:
    summary = RuleCheckSummary()
    for rid, cells in rows(text):
        summary.per_layer[rid.split("-")[0]] += 1
        summary.sev[re.sub(r"\s*\(.*\)", "", cells[7])] += 1
        srcs = cited(cells[2], docs)
        for frag in re.findall(r'"([^"]+)"', cells[3]):
            summary.n_quotes += 1
            status, where = find(frag, srcs)
            ok = status.startswith("OK")
            summary.problems += not ok
            if not (quiet and ok):
                print(f"{rid:10s} {status:12s} {where:48s} {frag[:60]}")
    return summary


def check_annex_b(text: str, docs: Path, quiet: bool) -> AnnexBCheckSummary:
    summary = AnnexBCheckSummary()
    for mid, cells in md_rows(text):
        summary.count += 1
        summary.basis[cells[5]] += 1
        if cells[2] == "—":
            continue
        srcs = cited(cells[2], docs)
        for frag in re.findall(r'"([^"]+)"', cells[3]):
            summary.quotes += 1
            status, where = find(frag, srcs)
            ok = status.startswith("OK")
            summary.bad += not ok
            if not (quiet and ok):
                print(f"{mid:10s} {status:12s} {where:48s} {frag[:60]}")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("checklist", nargs="?", default=str(DEFAULT_CHECKLIST))
    parser.add_argument("--sources", help="directory of the source documents (SEC_XCHECK_SOURCES)")
    parser.add_argument(
        "--write", action="store_true", help="rewrite the TOTALS block of the checklist"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="print only the problems and the summary"
    )
    args = parser.parse_args(argv)
    docs = sources_dir(args.sources)
    if not docs.is_dir():
        raise SystemExit(
            f"sources directory {docs} does not exist; download the register's documents there"
        )
    path = Path(args.checklist)
    text = path.read_text(encoding="utf-8")

    rules_res = check_rules(text, docs, args.quiet)
    annex_b_res = check_annex_b(text, docs, args.quiet)

    n_rules = sum(rules_res.per_layer.values())
    summary = (
        f"\nrules {n_rules}  quotes {rules_res.n_quotes}  "
        f"verified {rules_res.n_quotes - rules_res.problems}  problems {rules_res.problems}"
    )
    if annex_b_res.count:
        basis_str = ", ".join(f"{k} {v}" for k, v in sorted(annex_b_res.basis.items()))
        summary += (
            f"  | annex B rows {annex_b_res.count} ({basis_str})  "
            f"quotes {annex_b_res.quotes}  verified {annex_b_res.quotes - annex_b_res.bad}  problems {annex_b_res.bad}"
        )
    print(summary)

    table = totals_table(text, rules_res, annex_b_res)
    print("\n" + table)
    if args.write:
        start = text.index(MARK)
        end = text.index("\n---", start)
        path.write_text(text[:start] + MARK + "\n\n" + table + "\n" + text[end:], encoding="utf-8")
    return 1 if (rules_res.problems + annex_b_res.bad) else 0


if __name__ == "__main__":
    sys.exit(main())
