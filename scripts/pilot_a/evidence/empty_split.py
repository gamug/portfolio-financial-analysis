"""Every empty metric value of a database, by cause. Read-only. Missing inputs are tested against SEC companyfacts.

T-159a: the reviewer B script of the T-143 part A finding (v2 -- an input that is *derived* is not
missing), copied verbatim from ``/workspaces/thesis/handoff/pilot/b_review_p2b/``.

    python scripts/pilot_a/empty_split.py DB REPO_ROOT SEC_XCHECK_CACHE OUT.csv

``DB`` is opened read-only, ``REPO_ROOT`` gives the repo (``scripts`` and ``src`` on the path),
``SEC_XCHECK_CACHE`` holds the SEC companyfacts cache and ``OUT.csv`` receives the per-value rows."""

import sqlite3, sys, json, collections, csv, datetime as dt

DB, PR, CACHE, OUT = sys.argv[1:5]
sys.path[:0] = [f"{PR}/scripts", f"{PR}/src"]
from sec_xcheck import itemcheck as ic
from sec_xcheck.common import SecClient, cik_family
from fundamental_agent.statements import REGISTRY

c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
client = SecClient(ic.cache_dir(CACHE), offline=True)
own = ic.own_concepts()
fil = {
    r["id"]: r
    for r in c.execute(
        "select f.id,f.form,f.period_end,f.accession_number,a.ticker,a.cik from sec_filings f join assets a on a.id=f.asset_id"
    )
}
total = c.execute(
    "select m.filing_id, a.ticker from fundamental_metrics m join sec_filings f on f.id=m.filing_id "
    "join assets a on a.id=f.asset_id"
).fetchall()
# T-159a: an optional ticker filter, so a pilot database can be measured on the sample's tickers
# only (the before/after pair of the T-159a measurement).
TICKERS = {t.strip().upper() for t in sys.argv[5].split(",")} if len(sys.argv) > 5 else set()
total = len([r for r in total if not TICKERS or r["ticker"] in TICKERS])
empties = []
need = collections.defaultdict(set)
for fid, m, j in c.execute(
    "select filing_id, metric_name, inputs_json from fundamental_metrics where value is null"
):
    if TICKERS and fil[fid]["ticker"] not in TICKERS:
        continue
    d = json.loads(j) if j else {}
    p = d.get("provenance") or {}
    reason = p.get("empty_reason")
    items = [
        i
        for i, l in (p.get("items") or {}).items()
        if l and l[0].get("concept") is None and l[0].get("how") is None and d.get(i) is None
    ]
    if not p and "missing" in d:
        reason = "missing_input(no provenance)"
        items = [k for k in d["missing"] if k in REGISTRY]
    empties.append((fid, m, reason, items, bool(p.get("applicability_rule"))))
    if reason and reason.startswith(("missing_input", "capture_failure")):
        for i in items:
            need[fil[fid]["cik"]].add((fid, i))


def days(f):
    return (
        (dt.date.fromisoformat(f["end"]) - dt.date.fromisoformat(f["start"])).days
        if f.get("start")
        else None
    )


verdict = {}
for cik, pairs in need.items():
    idx = collections.defaultdict(list)
    for d in [d for cc in cik_family(cik) if (d := client.get("companyfacts", cc))]:
        for _t, concept, f in ic.iter_us_gaap(d):
            idx[(f["accn"], f["end"])].append((concept, days(f)))
    for fid, item in pairs:
        f = fil[fid]
        facts = idx.get((f["accession_number"], f["period_end"]), [])
        hits = [dd for cc, dd in facts if cc in own.get(item, set())]
        if not facts:
            v = "nofacts"
        elif not hits:
            v = "notreported"
        elif "balance_sheet" in REGISTRY[item].statements:
            v = "has" if any(dd is None for dd in hits) else "notreported"
        else:
            q = f["form"].startswith("10-Q")
            if any(dd and ((80 <= dd <= 119) if q else dd >= 351) for dd in hits):
                v = "has"
            elif q and any(dd and dd > 119 for dd in hits):
                v = "ytdonly"
            else:
                v = "notreported"
        verdict[(fid, item)] = v
LABEL = {
    "na": "1. Does not apply by rule (e.g. a bank has no inventory)",
    "math": "2. Not defined mathematically (zero or negative base)",
    "prior": "3. Needs the prior year, which is not in the data",
    "has": "4. SEC HAS the input, we do not (a capture problem)",
    "ytdonly": "5. 10-Q quarter value not filed (cash flow is filed year-to-date only)",
    "notreported": "6. Company does not file the input under the item's concepts",
    "nofacts": "7. SEC has no data for the filing at all",
    "other": "8. Other / no recorded reason",
}
out = collections.Counter()
per = collections.defaultdict(collections.Counter)
rows = []
for fid, m, reason, items, app in empties:
    if reason == "not_applicable":
        b = "na"
    elif reason in ("growth_base_not_positive", "denominator_not_positive"):
        b = "math"
    elif reason == "prior_year_missing":
        b = "prior"
    elif reason and reason.startswith(("missing_input", "capture_failure")) and items:
        vs = {verdict[(fid, i)] for i in items}
        b = (
            next(x for x in ("has", "ytdonly", "notreported", "nofacts") if x in vs)
            if vs
            else "other"
        )
    else:
        b = "other"
    out[b] += 1
    per[m][b] += 1
    f = fil[fid]
    rows.append(
        (
            f["ticker"],
            f["accession_number"],
            f["form"],
            f["period_end"],
            m,
            reason,
            b,
            ";".join(f"{i}:{verdict.get((fid, i), '')}" for i in items),
        )
    )
n = sum(out.values())
print(f"metric values {total}, empty {n} ({100 * n / total:.1f}%)\n")
for b in LABEL:
    print(f"{out[b]:7d}  {100 * out[b] / total:5.1f}% of all  {LABEL[b]}")
print(
    "\nper metric: empty | 1 na | 2 math | 3 prior | 4 SEC has | 5 ytd-only | 6 not filed | 7 no facts | 8 other"
)
for m in sorted(per, key=lambda m: -sum(per[m].values())):
    print(f"{m:42s} {sum(per[m].values()):6d} | " + " | ".join(f"{per[m][b]:5d}" for b in LABEL))
with open(OUT, "w", newline="") as h:
    w = csv.writer(h)
    w.writerow(
        [
            "ticker",
            "accession",
            "form",
            "period_end",
            "metric",
            "empty_reason",
            "bucket",
            "missing_items",
        ]
    )
    w.writerows(rows)
