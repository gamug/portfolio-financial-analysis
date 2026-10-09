"""Verify every quote of a checklist version against its source file, and generate the totals.

    uv run python scripts/sec_xcheck/quotecheck.py [CHECKLIST] [--sources DIR] [--write] [--quiet]

``CHECKLIST`` defaults to ``docs/checklist_sec/checklist_v1.2.2.md``. The source documents (``sources_register.md``:
URL, version, SHA-256) are read from ``--sources`` / ``SEC_XCHECK_SOURCES``; the PDFs are never committed.

For each rule row, each quoted fragment ("...") must be found verbatim (after normalizing case, spacing and
punctuation) in one of the files the row cites, on one of the PDF pages it cites. Three extraction artifacts are
tolerated and reported: PDF ligatures ("fi", "ff", "fl") dropped by text extraction; "t" glyphs dropped in A1's
section headings; and a fragment that spans a page break. With ``--write``, the totals table replaces the TOTALS
marker in the checklist. Exit status 1 when any quote is not verified. Needs ``pypdf``.
"""

from __future__ import annotations

import argparse
import collections
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
LAYERS = ["ID", "PER", "CON", "DQ", "MET", "MKT", "APP"]
DEFAULT_CHECKLIST = REPO / "docs" / "checklist_sec" / "checklist_v1.2.2.md"

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


def totals_table(
    text: str,
    per_layer: collections.Counter[str],
    sev: collections.Counter[str],
    n_quotes: int,
    problems: int,
) -> str:
    lines = ["| Layer | Rules |", "|---|---|"]
    lines += [f"| {k} | {per_layer[k]} |" for k in LAYERS]
    lines += [f"| **All** | **{sum(per_layer.values())}** |", ""]
    limitations = len(re.findall(r"(?m)^\| L-\d+ \|", text))
    screens = len(re.findall(r"(?m)^\| A-\d+ \|", text))
    lines += [
        f"- **Quoted fragments:** {n_quotes}; verified against the files: {n_quotes - problems}.",
        "- **Severity:** " + ", ".join(f"{k} {v}" for k, v in sorted(sev.items())) + ". "
        '"DQ-01" means the identity\'s severity follows DQ-01 (WARN above rounding, BLOCK above materiality).',
        f"- **Outside the rules:** {limitations} declared limitations (section L) and "
        f"{screens} calibrated screens (Annex A).",
    ]
    return "\n".join(lines)


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
    per_layer: collections.Counter[str] = collections.Counter()
    sev: collections.Counter[str] = collections.Counter()
    n_quotes = 0
    problems = 0
    for rid, cells in rows(text):
        per_layer[rid.split("-")[0]] += 1
        sev[re.sub(r"\s*\(.*\)", "", cells[7])] += 1
        srcs = cited(cells[2], docs)
        for frag in re.findall(r'"([^"]+)"', cells[3]):
            n_quotes += 1
            status, where = find(frag, srcs)
            ok = status.startswith("OK")
            problems += not ok
            if not (args.quiet and ok):
                print(f"{rid:10s} {status:12s} {where:48s} {frag[:60]}")
    print(
        f"\nrules {sum(per_layer.values())}  quotes {n_quotes}  verified {n_quotes - problems}  problems {problems}"
    )
    table = totals_table(text, per_layer, sev, n_quotes, problems)
    print("\n" + table)
    if args.write:
        start = text.index(MARK)
        end = text.index("\n---", start)
        path.write_text(text[:start] + MARK + "\n\n" + table + "\n" + text[end:], encoding="utf-8")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
