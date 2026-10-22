"""Print the non-dimensional XBRL facts of a sampled filing that match an item's concept pattern.

T-159a: the reviewer B script of the T-143 part A finding, copied verbatim from
``/workspaces/thesis/handoff/pilot/b_review_p2b/``.

    python scripts/pilot_a/inspect_xbrl.py SAMPLE.json XBRL_DIR
"""

import json
import re
import sys
import datetime as dt
import xml.etree.ElementTree as ET

SAMPLE, DIR = sys.argv[1:3]
PAT = {
    "operating_income": r"Operating(Income|Profit|Earnings)|IncomeFromOperations|OperatingLoss",
    "inventory": r"Inventor",
    "gross_profit": r"GrossProfit|GrossMargin",
    "cogs": r"CostOf|CostsOf",
    "short_term_investments": r"ShortTermInvest|MarketableSecurities|AvailableForSale|TradingSecurities|HeldToMaturity|Investments",
    "stock_based_compensation": r"ShareBased|StockBased|StockCompensation|EquityCompensation|ShareBasedCompensation",
    "depreciation_amortization": r"Depreciation|Amortization",
    "receivables": r"Receivable",
    "long_term_debt": r"Debt|Notes|Borrowing|Debenture",
    "net_income": r"NetIncome|ProfitLoss|NetEarnings",
    "short_term_debt": r"Debt|Borrowing|CommercialPaper|NotesPayable",
    "minority_interest": r"Noncontrolling|MinorityInterest",
    "capital_expenditure": r"PaymentsToAcquire|CapitalExpend|PaymentsFor|Development|Improvement",
    "interest_expense": r"Interest",
}
X = "{http://www.xbrl.org/2003/instance}"
for t, acc, form, end, item, g in json.load(open(SAMPLE)):
    root = ET.parse(f"{DIR}/{acc}.xml").getroot()
    ctx = {}
    for cx in root.iter(X + "context"):
        p = cx.find(X + "period")
        seg = cx.find(f"{X}entity/{X}segment")
        ins = p.find(X + "instant")
        s = p.find(X + "startDate")
        e = p.find(X + "endDate")
        ctx[cx.get("id")] = (
            ins.text if ins is not None else e.text,
            None
            if ins is not None
            else (dt.date.fromisoformat(e.text) - dt.date.fromisoformat(s.text)).days,
            seg is not None,
        )
    pat = re.compile(PAT[item])
    nd = {}
    dim = set()
    for el in root:
        if not el.tag.startswith("{") or el.get("contextRef") not in ctx:
            continue
        ns, local = el.tag[1:].split("}")
        pre = "us-gaap" if "fasb.org/us-gaap" in ns else ns.rsplit("/", 2)[-2] if "/" in ns else ns
        e_, days, hasdim = ctx[el.get("contextRef")]
        if e_ != end or not pat.search(local) or not (el.text or "").strip():
            continue
        if hasdim:
            dim.add(f"{pre}:{local}")
        else:
            nd.setdefault(f"{pre}:{local}", set()).add((days, el.text.strip()[:16]))
    print(f"\n== {t} {form} {end} item={item} grade={g}")
    for k, v in sorted(nd.items())[:14]:
        print("   ", k, sorted(v, key=str)[:3])
    if len(nd) > 14:
        print(f"    ... {len(nd) - 14} more non-dimensional")
    if not nd:
        print("    (no non-dimensional fact matches)")
    print(f"    dimensional-only concepts: {sorted(dim - set(nd))[:6]}")
