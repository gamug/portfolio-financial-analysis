"""For every empty metric whose missing inputs the SEC does not file under the item's own concepts (bucket 6),
search the filing for a concept that looks like the item: in SEC companyfacts (standard taxonomies) and in the
stored financial_facts (the gateway's rows, custom concepts included). Read-only."""

import collections
import csv
import datetime as dt
import json
import re
import sqlite3
import sys

DB, PR, CACHE, SPLIT, OUT = sys.argv[1:6]
sys.path[:0] = [f"{PR}/scripts", f"{PR}/src"]
from sec_xcheck import itemcheck as ic
from sec_xcheck.common import SecClient, cik_family

from fundamental_agent.statements import REGISTRY

PAT = {
    "short_term_investments": r"ShortTermInvestment|MarketableSecuritiesCurrent|AvailableForSale\w*Current|HeldToMaturity\w*Current|TradingSecurities",
    "redeemable_nci": r"RedeemableNoncontrolling|TemporaryEquity",
    "inventory": r"Inventor",
    "cogs": r"^CostOf|CostOfRevenue|CostOfGoods|CostOfServices|CostOfSales",
    "gross_profit": r"GrossProfit",
    "receivables": r"Receivable",
    "operating_income": r"OperatingIncome|OperatingProfit|IncomeFromOperations",
    "interest_expense": r"Interest\w*Expense|InterestAndDebtExpense|InterestCost",
    "short_term_debt": r"DebtCurrent|ShortTermBorrowing|CommercialPaper|NotesPayableCurrent|LinesOfCreditCurrent|ShortTermDebt",
    "minority_interest": r"MinorityInterest|NoncontrollingInterest",
    "depreciation_amortization": r"Depreciation|Amortization",
    "long_term_debt": r"LongTermDebt|SeniorNotes|NotesPayable|DebtNoncurrent|ConvertibleNotes|LongTermBorrowing|Debentures",
    "stock_based_compensation": r"ShareBasedCompensation|StockBasedCompensation|StockCompensation|EquityCompensation",
    "current_assets": r"AssetsCurrent",
    "current_liabilities": r"LiabilitiesCurrent",
    "pretax_income": r"BeforeIncomeTax|PreTax|Pretax",
    "cash": r"^Cash|CashAndCashEquivalents|CashAndDueFromBanks",
    "capital_expenditure": r"PaymentsToAcquire\w*(Property|Productive|RealEstate|Equipment|Plant)|CapitalExpenditure|PurchasesOfProperty",
    "income_tax": r"IncomeTax\w*Expense|IncomeTaxesPaid|ProvisionForIncomeTax",
    "net_income": r"NetIncome|ProfitLoss|NetEarnings",
    "revenue": r"Revenue|Sales",
    "total_assets": r"^Assets$",
    "operating_cash_flow": r"OperatingActivities",
    "equity": r"StockholdersEquity|PartnersCapital|MembersEquity",
}
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
client = SecClient(ic.cache_dir(CACHE), offline=True)
fil = {
    r["accession_number"]: r
    for r in c.execute(
        "select f.id,f.form,f.period_end,f.accession_number,a.ticker,a.cik from sec_filings f join assets a on a.id=f.asset_id"
    )
}
rows6 = [r for r in csv.DictReader(open(SPLIT)) if r["bucket"] == "notreported"]
need = collections.defaultdict(set)
for r in rows6:
    for x in filter(None, r["missing_items"].split(";")):
        i, v = x.rsplit(":", 1)
        if v == "notreported":
            need[fil[r["accession"]]["cik"]].add((r["accession"], i))


def days(f):
    return (
        (dt.date.fromisoformat(f["end"]) - dt.date.fromisoformat(f["start"])).days
        if f.get("start")
        else None
    )


cand = {}
for cik, pairs in need.items():
    idx = collections.defaultdict(list)
    for d in [d for cc in cik_family(cik) if (d := client.get("companyfacts", cc))]:
        for _t, concept, f in ic.iter_us_gaap(d, ("us-gaap", "ifrs-full", "srt")):
            idx[(f["accn"], f["end"])].append((concept, days(f), f["val"]))
    for acc, item in pairs:
        f = fil[acc]
        bs = "balance_sheet" in REGISTRY[item].statements
        q = f["form"].startswith("10-Q")
        ok = lambda dd: (dd is None) if bs else (dd is not None and (dd <= 371 if q else dd >= 351))
        pat = re.compile(PAT.get(item, r"$^"))
        sec = sorted(
            {
                cc
                for cc, dd, v in idx.get((acc, f["period_end"]), [])
                if pat.search(cc) and ok(dd) and v
            }
        )
        stored = sorted(
            {
                str(cc)
                for (cc, pk, v) in c.execute(
                    "select concept, period_key, value from financial_facts where filing_id=? and value is not null",
                    (f["id"],),
                )
                if str(pk).split(" ", 1)[0] == f["period_end"]
                and pat.search(str(cc).split("_", 1)[-1])
                and v
            }
        )
        cand[(acc, item)] = (sec, stored)
out = collections.Counter()
item_c = collections.defaultdict(collections.Counter)
top = collections.defaultdict(collections.Counter)
rows = []
for r in rows6:
    its = [
        x.rsplit(":", 1)[0]
        for x in filter(None, r["missing_items"].split(";"))
        if x.endswith(":notreported")
    ]
    hit = [(i, cand[(r["accession"], i)]) for i in its if any(cand[(r["accession"], i)])]
    b = "candidate concept found" if hit else "no candidate anywhere"
    out[b] += 1
    rows.append(
        (
            r["ticker"],
            r["accession"],
            r["form"],
            r["period_end"],
            r["metric"],
            b,
            "|".join(f"{i}=sec:{','.join(s[:3])};stored:{','.join(t[:3])}" for i, (s, t) in hit),
        )
    )
for (acc, item), (s, t) in cand.items():
    k = "SEC candidate" if s else ("stored-only candidate" if t else "none")
    item_c[item][k] += 1
    for cc in s[:2]:
        top[item][cc] += 1
    for cc in t[:1]:
        top[item]["stored:" + cc.split("_", 1)[0] + "_*" if not s else "x"] += 0
print("bucket-6 empty metric values:", len(rows6))
for k, v in out.most_common():
    print(f"  {v:7d}  {k}")
print(
    "\nper missing item (distinct filing-items): SEC candidate | stored-only | none ; top SEC candidate concepts"
)
for i in sorted(item_c, key=lambda i: -sum(item_c[i].values())):
    ic_ = item_c[i]
    print(
        f"  {i:26s} {sum(ic_.values()):5d} | {ic_['SEC candidate']:5d} | {ic_['stored-only candidate']:5d} | {ic_['none']:5d} ; {top[i].most_common(4)}"
    )
with open(OUT, "w", newline="") as h:
    w = csv.writer(h)
    w.writerow(["ticker", "accession", "form", "period_end", "metric", "verdict", "candidates"])
    w.writerows(rows)
json.dump(
    {f"{a}|{i}": v for (a, i), v in cand.items()}, open(OUT.replace(".csv", "_pairs.json"), "w")
)
