"""Fetch each sampled filing's XBRL instance from EDGAR (rate-limited, research User-Agent) into its own directory.

T-159a: the reviewer B script of the T-143 part A finding, copied verbatim from
``/workspaces/thesis/handoff/pilot/b_review_p2b/``.

    python scripts/pilot_a/fetch_xbrl.py SAMPLE.json DB OUTDIR
"""

import json, sys, time, sqlite3, urllib.request, os

SAMPLE, DB, OUTDIR = sys.argv[1:4]
UA = {"User-Agent": "portfolio-thesis research research@example.com", "Accept-Encoding": "identity"}
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def get(url):
    time.sleep(0.4)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return r.read()


for t, acc, form, end, item, g in json.load(open(SAMPLE)):
    dest = os.path.join(OUTDIR, acc + ".xml")
    if os.path.exists(dest):
        continue
    cik = int(
        c.execute(
            "select a.cik from sec_filings f join assets a on a.id=f.asset_id where f.accession_number=?",
            (acc,),
        ).fetchone()[0]
    )
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/"
    try:
        idx = json.loads(get(base + "index.json"))
        names = [i["name"] for i in idx["directory"]["item"]]
        inst = [n for n in names if n.endswith("_htm.xml")] or [
            n
            for n in names
            if n.endswith(".xml")
            and not n.startswith(("FilingSummary", "R", "Financial"))
            and "_" not in n[-8:]
        ]
        if not inst:
            print("NO INSTANCE", t, acc, names[:20])
            continue
        open(dest, "wb").write(get(base + inst[0]))
        print("ok", t, acc, inst[0])
    except Exception as e:
        print("ERR", t, acc, e)
