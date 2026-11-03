"""L-43, step 2: per company, its cover classes (stored rows, MKT-04 exclusions applied), the classes its covers list
(dei:TradingSymbol by class member, or the one symbol with no member), and per class the shortest sentence of its
filings that states a conversion into another class, with the filings (accession) that print one. Read-only.
usage: l43_classes.py <db> <l43_read.jsonl> <out.json>"""

import collections
import json
import re
import sqlite3
import sys

DB, READ, OUT = sys.argv[1:4]
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
EXCL = re.compile(r"Preferred|FullyDiluted|UnitsMember$")
RATE = re.compile(
    r"one-for-one|one for one|share-for-share|share for share|one-to-one|1:1|1-for-1|equivalent number|equal number|"
    r"into one share|into 1 share|into one fully paid|for one share|conversion (?:ratio|rate)|convertible into [\d,.]+ shares|"
    r"into [\d,.]+ shares of class",
    re.I,
)
recs = [json.loads(l) for l in open(READ)]
by = collections.defaultdict(list)
for r in recs:
    by[r["t"]].append(r)
out = {}
for t, rs in sorted(by.items()):
    rs.sort(key=lambda r: r["filed"])
    members = collections.Counter()
    for (m,) in c.execute(
        "select s.class_member from filing_cover_shares s join sec_filings f on f.id=s.filing_id join assets a on a.id=f.asset_id "
        "where a.ticker=? and s.class_member != ''",
        (t,),
    ):
        if not EXCL.search(m.split(":")[-1]):
            members[m] += 1
    listed = collections.Counter()
    for r in rs:
        for name, mem, val in r.get("dei", []):
            if name == "TradingSymbol":
                listed[(mem[-1] if mem else "", val)] += 1
    per = {}
    for m in members:
        word = re.sub(r"Member$", "", m.split(":")[-1])
        word = re.sub(r"^(CommonClass|CapitalClass)", "Class ", word)
        word = re.sub(r"([a-z])([A-Z0-9])", r"\1 \2", word).replace("Common Stock", "common stock")
        key = re.compile(
            re.escape(word.split()[-1])
            if word.startswith("Class ")
            else re.escape(word.split()[0]),
            re.I,
        )
        hits = []
        for r in rs:
            for s in r.get("sentences", []):
                if RATE.search(s) and (
                    ("Class " + word.split()[-1]).lower() in s.lower()
                    if word.startswith("Class ")
                    else key.search(s)
                ):
                    hits.append((len(s), r["acc"], r["form"], r["filed"], s))
        hits.sort()
        per[m] = dict(
            label=word,
            n_filings=len({h[1] for h in hits}),
            best=hits[0][4][:500] if hits else None,
            first=min((h[3] for h in hits), default=None),
            accessions=sorted({h[1] for h in hits}),
        )
    out[t] = dict(
        filings=len(rs),
        members=dict(members),
        listed=[f"{k[0].split(':')[-1] or '-'}={k[1]}" for k in listed],
        classes=per,
    )
json.dump(out, open(OUT, "w"), indent=1)
for t, v in out.items():
    print(f"== {t} ({v['filings']} filings) listed: {v['listed']}")
    for m, p in v["classes"].items():
        print(
            f"   {m.split(':')[-1]:38s} in {p['n_filings']:2d} filings | {(p['best'] or '-')[:260]}"
        )
