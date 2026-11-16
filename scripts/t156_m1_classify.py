"""T-156 phase 1 — the M1 classifier (strict reading of the plan's class table).

Precedence, from the row's own provenance/flags, then the stored facts. A case that fits no class
is reported under ``UNCLASSIFIED`` (the plan's STOP AND ASK).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

FEAT = "/workspaces/thesis/scratch/t156/out/m1_features.json"
AGG = (
    "us-gaap_DepreciationDepletionAndAmortization",
    "us-gaap_DepreciationAmortizationAndAccretionNet",
    "us-gaap_DepreciationAndAmortization",
)
DA = "us-gaap_Depreciation"


def da_class(f: dict) -> tuple[str, dict]:  # noqa: C901, PLR0911 - one branch per class
    """The D&A class of a filing whose EBITDA D&A term is missing."""
    flags = f["metric_flags"]
    concepts = f["da_cf_concepts"]
    ext = f["da_ext"]
    other = f["da_other"]
    child = f["da_child"]
    agg = f["da_agg"]
    all_da = set(concepts) | set(f["da_is"])
    # 1. no D&A-nature fact at all
    if not all_da:
        return "da_absent", {"evidence": "no D&A-nature fact (cash-flow or income-statement)"}
    # 2. a 10-Q whose TTM D&A could not be formed while the filing does file D&A: the flow was
    #    refused / x4, or the own year-to-date column resolved a concept but no TTM flow exists
    #    (a prior-year column is not stored). The plan's `da_refused_ttm`: "say which term was
    #    missing".
    m = f["da_term"]["method"]
    if f["form"] == "10-Q":
        if m in ("refused", "x4"):
            return "da_refused_ttm", {
                "method": m,
                "flow_flags": f["da_term"]["flags"],
                "own_concept": f["da_term_concept"],
            }
        if m is None and f["da_term_concept"] is not None:
            return "da_refused_ttm", {
                "method": "not_formed",
                "own_concept": f["da_term_concept"],
                "details": "own year-to-date column resolved, no TTM flow built",
            }
    # 3. the sourced sum refused because other lines are present
    if any("da_sum_refused_other_lines" in x for x in flags):
        return "da_refused_other_lines", {"outside": other}
    # 4. only Depreciation is filed (N14/D7)
    if any("depreciation_only_not_da" in x for x in flags) and DA in concepts:
        return "da_refused_depreciation_only", {"filed": concepts}
    # 5. a US-GAAP D&A element other than the three aggregates is what is filed
    if agg and all("us-gaap_" not in c for c in []):
        pass
    if other:
        return "da_element_with_other", {"element": other}
    # 6. only company extension(s) carry D&A
    if ext and not other and not agg and DA not in concepts:
        return "da_extension_only", {"extensions": ext}
    # 7. children only, no aggregate and no Depreciation
    if child and not other and not ext:
        return "UNCLASSIFIED", {"child_only": child}
    return "UNCLASSIFIED", {
        "concepts": concepts,
        "agg": agg,
        "child": child,
        "ext": ext,
        "other": other,
    }


def oi_class(f: dict) -> tuple[str, dict]:
    if not f["oi_term"]["present"]:
        if not f["has_oi_fact"]:
            return "oi_absent_presentation", {"presents": f["subtotals"]}
        return "oi_absent_other", {
            "concept": f["oi_term_concept"],
            "method": f["oi_term"]["method"],
        }
    return "", {}


def main() -> None:
    feats = json.loads(Path(FEAT).read_text())
    rows = []
    counts: Counter = Counter()
    unclass: list = []
    for a, f in sorted(feats.items(), key=lambda kv: kv[1]["ticker"]):
        if not f.get("has_filing"):
            counts["no_usable_filing"] += 1
            continue
        if f["ebitda"] is not None:
            continue
        if f["financial_firm"]:
            counts["financial_firm_na"] += 1
            continue
        entry = {"asset": a, **f}
        if not f["da_term"]["present"]:
            c, ev = da_class(f)
            entry["da_class"], entry["da_evidence"] = c, ev
            counts[c] += 1
            if c == "UNCLASSIFIED":
                unclass.append(entry)
        if not f["oi_term"]["present"]:
            c, ev = oi_class(f)
            entry["oi_class"], entry["oi_evidence"] = c, ev
            counts[c] += 1
        rows.append(entry)
    print("class counts:", dict(counts))
    print("non-financial empties:", len(rows))
    print("UNCLASSIFIED:", len(unclass))
    for e in unclass:
        print("  ", e["ticker"], e["form"], e["da_evidence"])
    Path("/workspaces/thesis/scratch/t156/out/m1_classes_raw.json").write_text(
        json.dumps(rows, indent=1, default=str)
    )


if __name__ == "__main__":
    main()
