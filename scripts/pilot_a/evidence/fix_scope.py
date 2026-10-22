import collections
import csv
import json
import sqlite3
import sys

DB, CSV, PR, LOG, GR = sys.argv[1:6]
sys.path[:0] = [f"{PR}/scripts", f"{PR}/src"]
from sec_xcheck import itemcheck as ic

from fundamental_agent.statements import REGISTRY

own = ic.own_concepts()
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
f = {a: (i, pe) for i, a, pe in c.execute("select id,accession_number,period_end from sec_filings")}
drops = collections.defaultdict(set)
for line in open(LOG):
    d = json.loads(line)
    drops[d["accession"]].add(d["concept"].split("_", 1)[-1])
memo = {}


def cause(a, i):
    if (a, i) in memo:
        return memo[(a, i)]
    fid, pe = f[a]
    st = [
        (s, str(cc).split("_", 1)[-1], pk)
        for s, cc, pk in c.execute(
            "select statement,concept,period_key from financial_facts where filing_id=? and value is not null",
            (fid,),
        )
    ]
    mine = [(s, pk) for s, cc, pk in st if cc in own[i] and pk.split(" ", 1)[0] == pe]
    if any(s not in REGISTRY[i].statements for s, _ in mine) and not any(
        s in REGISTRY[i].statements for s, _ in mine
    ):
        k = "F2 served on another statement"
    elif mine and all(pk.endswith("(YTD)") for _, pk in mine):
        k = "F3 10-Q: served only as year-to-date"
    elif mine:
        k = "F4 served in the read column but refused by a rule"
    elif own[i] & drops[a]:
        k = "F0 dropped by the gateway"
    else:
        k = "F1 never served: notes only"
    memo[(a, i)] = k
    return k


out = collections.Counter()
per = collections.defaultdict(collections.Counter)
for r in csv.DictReader(open(CSV)):
    if r["bucket"] != "has":
        continue
    ks = sorted(
        {
            cause(r["accession"], x.rsplit(":", 1)[0])
            for x in filter(None, r["missing_items"].split(";"))
            if x.endswith(":has")
        }
    )
    out[ks[0]] += 1
    per[ks[0]][r["metric"]] += 1
gm = {
    "strong-served": "F5a another standard concept, served (registry alone)",
    "strong-notes": "F5b another standard concept, notes only (fallback)",
    "custom": "C  company-specific concept",
    "weak": "R  related quantity only",
    "none": "OK genuinely not reported",
}
for r in csv.DictReader(open(GR)):
    out[gm[r["grade"]]] += 1
    per[gm[r["grade"]]][r["metric"]] += 1
for k in sorted(out):
    print(f"{out[k]:7d}  {k}   top: {per[k].most_common(3)}")
print("sum", sum(out.values()))
