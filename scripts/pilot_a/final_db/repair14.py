"""FIXES #14 part B (reviewer A, 2026-10-29, point 3): the filings the runs skipped with a period reason pass ID-07
(dei:DocumentPeriodEndDate = EDGAR periodOfReport, A1 §3.1.7; `id07_check.json`); the gateway's payload put another
period's values in their income and cash-flow columns. Their statement columns are rebuilt from the filing's own XBRL
facts as the SEC publishes them (companyfacts, us-gaap, non-dimensional, same accession -- the source offline_run2020.py
already stands in for every stored filing): per statement row of the gateway's payload (its concepts, labels, levels kept),
the filing's own columns -- its quarter and year to date, the same periods a year earlier, the balance sheet at its own date
and at the previous fiscal year end -- each labelled as the gateway labels them (PER-08 lengths; the quarter number counted
from the previous fiscal year end). Dimensional and abstract rows keep no value; the gateway's corrections (drop log) of the replaced
columns are dropped. The rebuilt payload is written beside the original, which is kept. Read-only on every database.
usage: repair14.py <in_dir> <out_dir> <cf_dir:cf_dir:...> <cik:accession> ..."""

import json
import os
import re
import sys
from datetime import date

IN, OUTD, CF, *ITEMS = sys.argv[1:]
DATE = re.compile(r"^\d{4}-\d\d-\d\d")


def cf_doc(cik):
    for d in CF.split(":"):
        p = os.path.join(d, f"CIK{cik}.json")
        if os.path.exists(p):
            return json.load(open(p))
    raise SystemExit(f"no companyfacts for {cik}")


def days(s, e):
    return (date.fromisoformat(e) - date.fromisoformat(s)).days + 1


report = []
for it in ITEMS:
    cik, acc = it.split(":")
    src = json.load(open(os.path.join(IN, cik, f"{acc}.json")))
    facts = {}
    fp = None
    for name, body in cf_doc(cik)["facts"].get("us-gaap", {}).items():
        for unit, xs in body["units"].items():
            for x in xs:
                if x["accn"] == acc:
                    facts.setdefault(name, {})[(x.get("start"), x["end"])] = x["val"]
                    fp = fp or x.get("fp")
    durs = {k for v in facts.values() for k in v if k[0]}
    inst = {k[1] for v in facts.values() for k in v if not k[0]}
    own_end = max(e for s, e in durs if 89 <= days(s, e) <= 98)
    q = [(s, e) for s, e in durs if 89 <= days(s, e) <= 98]
    ytd = [(s, e) for s, e in durs if 181 <= days(s, e) <= 189 or 273 <= days(s, e) <= 280]
    own_q = max(q, key=lambda k: k[1])
    prior_q = [
        k for k in q if 358 <= (date.fromisoformat(own_q[1]) - date.fromisoformat(k[1])).days <= 371
    ]
    own_ytd = [k for k in ytd if k[1] == own_q[1]]
    prior_ytd = [
        k
        for k in ytd
        if 358 <= (date.fromisoformat(own_q[1]) - date.fromisoformat(k[1])).days <= 371
    ]
    fy_start = own_ytd[0][0] if own_ytd else own_q[0]
    prev_fye = max((d for d in inst if d < fy_start), default=None)
    # the quarter number counted from the previous fiscal year end (some filers tag DocumentFiscalPeriodFocus "FY" on a 10-Q)
    n = round((date.fromisoformat(own_q[1]) - date.fromisoformat(prev_fye)).days / 91.3)
    assert 1 <= n <= 3, (acc, n)
    cols = {}  # label -> (start, end)
    for k in [own_q] + prior_q[:1]:
        cols[f"{k[1]} (Q{n})"] = k
    for k in own_ytd[:1] + prior_ytd[:1]:
        cols[f"{k[1]} (YTD)"] = k
    bs_cols = {own_q[1]: (None, own_q[1])}
    if prev_fye:
        bs_cols[prev_fye] = (None, prev_fye)
    data = json.loads(json.dumps(src["data"]))
    filled = {}
    for st, cmap in (("income_statement", cols), ("cash_flow", cols), ("balance_sheet", bs_cols)):
        rows = data.get(st) or []
        for r in rows:
            for k in [k for k in r if DATE.match(k)]:
                del r[k]
            name = r["concept"].split("_", 1)[1] if "_" in r["concept"] else r["concept"]
            for label, key in cmap.items():
                r[label] = (
                    None
                    if (r.get("abstract") or r.get("dimension"))
                    else facts.get(name, {}).get(key)
                )
                if r[label] is not None:
                    filled[(st, label)] = filled.get((st, label), 0) + 1
    data["corrections"] = [
        c
        for c in data.get("corrections") or []
        if c.get("statement") not in ("income_statement", "cash_flow", "balance_sheet")
    ]
    data["audit_repair"] = (
        "FIXES #14 part B: statement columns rebuilt from the filing's own companyfacts facts (same accession); "
        "the gateway's payload put another period's values in them"
    )
    os.makedirs(os.path.join(OUTD, cik), exist_ok=True)
    json.dump(dict(src, data=data), open(os.path.join(OUTD, cik, f"{acc}.json"), "w"))
    rep = dict(
        cik=cik,
        accession=acc,
        fiscal_period=fp,
        columns=sorted(cols),
        balance_sheet=sorted(bs_cols),
        filled={f"{a}|{b}": v for (a, b), v in sorted(filled.items())},
    )
    report.append(rep)
    print(
        acc,
        fp,
        sorted(cols),
        sorted(bs_cols),
        {f"{a[:2]}|{b}": v for (a, b), v in sorted(filled.items())},
        flush=True,
    )
json.dump(report, open(os.path.join(OUTD, "repair14.json"), "w"), indent=1)
