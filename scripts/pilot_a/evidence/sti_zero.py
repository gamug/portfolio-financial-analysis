import collections
import json
import re
import sqlite3
import sys

DB, PR, CACHE = sys.argv[1:4]
sys.path[:0] = [f"{PR}/scripts", f"{PR}/src"]
from sec_xcheck import itemcheck as ic
from sec_xcheck.common import SecClient, cik_family

c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
cl = SecClient(ic.cache_dir(CACHE), offline=True)
FAM = r"AvailableForSaleSecuritiesDebtSecuritiesCurrent|DebtSecuritiesAvailableForSaleExcludingAccruedInterestCurrent|OtherShortTermInvestments|TradingSecurities|TradingSecuritiesDebt|TradingSecuritiesEquity|HeldToMaturitySecuritiesCurrent"
SAME = [
    {
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        "DebtSecuritiesAvailableForSaleExcludingAccruedInterestCurrent",
    },
    {"TradingSecurities", "TradingSecuritiesDebt", "TradingSecuritiesEquity"},
]
zero = set()
for fid, j in c.execute("select filing_id,inputs_json from fundamental_metrics "):
    d = json.loads(j) if j else {}
    l = ((d.get("provenance") or {}).get("items") or {}).get("short_term_investments")
    if (
        l
        and l[0].get("concept") is None
        and l[0].get("how") is None
        and d.get("short_term_investments") == 0
    ):
        zero.add(fid)
f = {
    i: (a, pe, k, t)
    for i, a, pe, k, t in c.execute(
        "select f.id,f.accession_number,f.period_end,a.cik,a.ticker from sec_filings f join assets a on a.id=f.asset_id"
    )
}
bycik = collections.defaultdict(list)
for fid in zero:
    bycik[f[fid][2]].append(fid)
res = collections.Counter()
vals = []
comp = set()
for cik, fids in bycik.items():
    idx = collections.defaultdict(dict)
    for d in [d for cc in cik_family(cik) if (d := cl.get("companyfacts", cc))]:
        for _t, cc, fa in ic.iter_us_gaap(d):
            if not fa.get("start") and re.fullmatch(FAM, cc) and fa["val"]:
                idx[(fa["accn"], fa["end"])][cc] = fa["val"]
    for fid in fids:
        a, pe, _, t = f[fid]
        fam = idx.get((a, pe), {})
        names = []
        for n in fam:
            if not any(n in s and any(m in names for m in s) for s in SAME):
                names.append(n)
        if not fam:
            res["no short-term investment filed (0 is right)"] += 1
        elif len(names) == 1:
            res["one STI concept filed (the value is known)"] += 1
            vals.append(fam[names[0]])
            comp.add(t)
        else:
            res["several STI components filed (sum needed)"] += 1
            comp.add(t)
print("filings with STI defaulted to 0 (cash_ratio rows):", len(zero))
[print(f"  {n:5d}  {k}") for k, n in res.most_common()]
vals.sort()
print(
    "companies:",
    len(comp),
    "median value when one is filed:",
    vals[len(vals) // 2] if vals else None,
)
