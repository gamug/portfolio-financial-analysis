"""L-43, step 3 (reviewer A's MKT-03 rule, 2026-10-29): the rows of cover_class_conversion -- per multi-class cover
filing of the companies whose filings print a conversion rate, each unlisted class's rate into the traded class, the
accession that prints it and the printed sentence. The citation is the filing itself when its document prints the rate;
otherwise the latest earlier filing of the company that prints it (point in time); otherwise the earliest later one
(the charter term in force; noted). Visa: each filing's own "as-converted" table (visa_conversion.json). Classes with no
printed rate are not written (their market caps stay flagged), and listed classes valued at another class's price are
listed apart. Read-only. usage: l43_rates.py <db> <l43_read.jsonl> <visa_conversion.json> <out_rates.json> <out_table.json>"""

import collections
import json
import re
import sqlite3
import sys

DB, READ, VISA, OUT, TABLE = sys.argv[1:6]
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
B1 = r"Class B common stock (?:is|are|will|shall|may)[^.]{0,80}?(?:convertible|convert|converted|exchangeable)[^.]{0,120}?into one share of (?:our )?Class A"
RULES = {  # ticker -> [(member, rate, regex of the printed statement)]
    "ABNB": [
        ("us-gaap:CommonClassBMember", B1),
        ("abnb:CommonClassHMember", r"Class H common stock[^.]{0,200}share-for-share"),
    ],
    "AOS": [
        (
            "us-gaap:CommonClassAMember",
            r"Class A Common Stock is convertible to Common Stock on a one for one basis",
        )
    ],
    "APP": [("us-gaap:CommonClassBMember", B1)],
    "BE": [
        (
            "us-gaap:CommonClassBMember",
            r"Class B common stock[^.]{0,80}convertible into one share of Class A",
        )
    ],
    "CMCSA": [
        (
            "us-gaap:CommonClassBMember",
            r"Class B common stock can be converted, on a share for share basis, into Class A",
        )
    ],
    "CRWD": [
        (
            "us-gaap:CommonClassBMember",
            r"Class B common stock[^.]{0,80}convertible into one share of Class A",
        )
    ],
    "CVNA": [
        (
            "us-gaap:CommonClassBMember",
            r"Class B common stock is exchangeable for an equivalent number of shares of Class A common stock",
        )
    ],
    "DASH": [
        (
            "us-gaap:CommonClassBMember",
            r"Class B common stock (?:will|shall) convert automatically into one share of Class A|"
            + B1,
        )
    ],
    "DDOG": [("us-gaap:CommonClassBMember", B1)],
    "DELL": [
        (
            m,
            r"Class A Common Stock[^.]{0,120}Class B Common Stock[^.]{0,80}convertible into shares of Class C Common Stock at any time on a one-to-one basis",
        )
        for m in ("us-gaap:CommonClassAMember", "us-gaap:CommonClassBMember")
    ],
    "DISH": [
        (
            "us-gaap:CommonClassBMember",
            r"Class B and Class C common stock is convertible, at the option of the holder, into one share of Class\s*A",
        )
    ],
    "ECHO": [
        (
            "us-gaap:CommonClassBMember",
            r"Class B and Class C common stock is convertible, at the option of the holder, into one share of Class\s*A",
        )
    ],
    "EL": [
        (
            "us-gaap:CommonClassBMember",
            r"Class B Common Stock is convertible into Class A Common Stock[^.]{0,160}one share of Class A Common Stock for each share of Class B",
        )
    ],
    "EXPE": [
        (
            "us-gaap:CommonClassBMember",
            r"Class B common stockholders may, at any time, convert their shares into common stock, on a one for one share basis",
        )
    ],
    "F": [
        (
            "us-gaap:CommonClassBMember",
            r"Class B Stock may be converted at any time into an equal number of shares of Common Stock",
        )
    ],
    "GDDY": [
        (
            "us-gaap:CommonClassBMember",
            r"Class B common stock is exchangeable for one share of Class A common stock",
        )
    ],
    "HSY": [
        (
            "us-gaap:CommonClassBMember",
            r"Class B Stock can be converted into Common Stock on a share-for-share basis",
        )
    ],
    "MA": [
        (
            "us-gaap:CommonClassBMember",
            r"Class B common stock are convertible on a one-for-one basis into shares of Class A",
        )
    ],
    "META": [
        (
            "us-gaap:CommonClassBMember",
            r"Class B common stock are convertible into an equivalent number of shares of our Class A",
        )
    ],
    "NKE": [
        (
            "us-gaap:CommonClassAMember",
            r"Class A Common Stock is convertible into one share of Class B Common Stock",
        )
    ],
    "RDDT": [
        (
            "us-gaap:CommonClassBMember",
            r"Class B common stock will convert automatically into one share of Class A",
        ),
        (
            "us-gaap:CommonClassCMember",
            r"Class C common stock shall automatically convert into one share of our Class A",
        ),
    ],
    "REGN": [
        (
            "us-gaap:CommonClassAMember",
            r"Class A Stock are convertible[^.]{0,80}into shares of Common Stock on a share-for-share basis",
        )
    ],
    "RL": [
        (
            "us-gaap:CommonClassBMember",
            r"Class B common stock may be converted immediately into Class A common stock on a one-for-one basis",
        )
    ],
    "SPG": [
        (
            "us-gaap:CommonClassBMember",
            r"Class B common stock convert automatically into an equal number of shares of common stock",
        )
    ],
    "TKO": [
        (
            "us-gaap:CommonClassBMember",
            r"Class B common stock are exchangeable, on a one-for-one basis, into shares of TKO Class A",
        )
    ],
    "TSN": [
        (
            "us-gaap:CommonClassBMember",
            r"Class B stock is convertible into Class A stock on a share-for-share basis",
        )
    ],
    "TTD": [("us-gaap:CommonClassBMember", B1)],
    "UHS": [
        (
            m,
            r"Class A, C and D Common Stock are convertible on a share for share basis into Class B|Class A, Class C and Class D Common Stock[^.]{0,80}convertible into shares of our Class B Common Stock on a share-for-share basis",
        )
        for m in (
            "us-gaap:CommonClassAMember",
            "us-gaap:CommonClassCMember",
            "uhs:CommonClassDMember",
        )
    ],
    "UPS": [
        (
            "us-gaap:CommonClassAMember",
            r"class A common stock is convertible into one share of the registrant.s class B common stock",
        )
    ],
    "VEEV": [
        (
            "us-gaap:CommonClassBMember",
            r"Class B common stock converts into Class A common stock on a one-for-one basis",
        )
    ],
}
LISTED_ELSEWHERE = {
    "BF.B": "Class A (BF.A)",
    "BIO": "Class B (BIO.B)",
    "DISCK": "Series A (DISCA), Series B (DISCB)",
    "GOOG": "Class A (GOOGL)",
    "LEN": "Class B (LEN.B)",
    "MKC": "Common Stock (MKC.V)",
    "SKYD": "Paramount Global Class A (PARAA) / Class B (PARA)",
    "STZ": "Class B (STZ.B)",
    "TAP": "Class A (TAP.A)",
    "UA": "Class A (UAA)",
}
recs = [json.loads(l) for l in open(READ)]
by = collections.defaultdict(list)
for r in recs:
    by[r["t"]].append(r)
rows, table = [], {}
for t, rules in RULES.items():
    rs = sorted(by[t], key=lambda r: r["filed"])
    covers = {
        acc: {
            m
            for (m,) in c.execute(
                "select s.class_member from filing_cover_shares s join sec_filings f on f.id = s.filing_id "
                "where f.accession_number = ?",
                (acc,),
            )
        }
        for acc in (r["acc"] for r in rs)
    }
    for member, pat in rules:
        rx = re.compile(pat, re.I)
        hits = {
            r["acc"]: next((s for s in r.get("sentences", []) if rx.search(s)), None) for r in rs
        }
        printed = [(r["filed"], r["acc"]) for r in rs if hits[r["acc"]]]
        n_own = n_earlier = n_later = 0
        for r in rs:
            if member not in covers[r["acc"]]:
                continue
            if hits[r["acc"]]:
                src, note = r["acc"], "printed in this filing"
                n_own += 1
            else:
                earlier = [p for p in printed if p[0] <= r["filed"]]
                if earlier:
                    src, note = earlier[-1][1], "printed in the latest earlier filing"
                    n_earlier += 1
                elif printed:
                    src, note = (
                        printed[0][1],
                        "printed in a later filing (the charter term then in force)",
                    )
                    n_later += 1
                else:
                    continue
            m = rx.search(hits[src])
            rows.append(
                dict(
                    accession=r["acc"],
                    class_member=member,
                    rate=1.0,
                    source_accession=src,
                    source_text=hits[src][max(0, m.start() - 60) : m.end() + 80],
                    rule=f"L-43 MKT-03: {note}",
                )
            )
        table.setdefault(t, []).append(
            dict(
                member=member,
                rate=1.0,
                filings_printing=len(printed),
                cited_own=n_own,
                cited_earlier=n_earlier,
                cited_later=n_later,
            )
        )
MV = {
    "B": "us-gaap:CommonClassBMember",
    "B-1": "v:CommonClassB1Member",
    "B-2": "v:CommonClassB2Member",
    "B-3": "v:CommonClassB3Member",
    "C": "us-gaap:CommonClassCMember",
}
nv = 0
for r in json.load(open(VISA)):
    present = {m for m, _, _ in r["cover"]}
    for k, v in r.get("classes", {}).items():
        if k in MV and v.get("rate") and MV[k] in present:
            rows.append(
                dict(
                    accession=r["acc"],
                    class_member=MV[k],
                    rate=v["rate"],
                    source_accession=r["acc"],
                    source_text=f"As-converted class A common stock: {k} {v['shares_m']:g} M shares, conversion rate into class A {v['rate']}",
                    rule="L-43 MKT-03: printed in this filing (Note Stockholders' Equity)",
                )
            )
            nv += 1
    missing = [
        m
        for m in present
        if m not in {MV.get(k) for k in r.get("classes", {}) if r["classes"][k].get("rate")}
        and m != "us-gaap:CommonClassAMember"
    ]
    if missing:
        table.setdefault("V-missing", []).append((r["acc"], missing))
table["V"] = [dict(member="class B / B-1 / B-2 / B-3 / C", rate="per filing", rows=nv)]
json.dump(rows, open(OUT, "w"), indent=1)
json.dump(dict(rated=table, listed_elsewhere=LISTED_ELSEWHERE), open(TABLE, "w"), indent=1)
print(
    len(rows), "rows;", len({r["accession"] for r in rows}), "filings;", len(RULES) + 1, "companies"
)
for t, v in table.items():
    print(
        t,
        v
        if t.startswith("V")
        else [
            (
                x["member"].split(":")[-1],
                x["filings_printing"],
                x["cited_own"],
                x["cited_earlier"],
                x["cited_later"],
            )
            for x in v
        ],
    )
