"""Strict grading: a substitute counts only if it is the item's total (taxonomy meaning), or the sole member of a
component family filed for the period. Sums only where both parts are filed.

T-159a: the reviewer B script of the T-143 part A finding, copied verbatim from
``/workspaces/thesis/handoff/pilot/b_review_p2b/``.

    python scripts/pilot_a/grade3.py CANDIDATES.json EMPTY_SPLIT.csv GRADED.csv DB
"""

import json, csv, re, sys, collections, sqlite3

CAND, CSV6, OUT, DB = sys.argv[1:5]
cand = {tuple(k.split("|")): v for k, v in json.load(open(CAND)).items()}
TOTAL = {  # the same total under another standard concept
    "receivables": [
        "AccountsReceivableNet",
        "AccountsNotesAndLoansReceivableNetCurrent",
        "AccountsAndOtherReceivablesNetCurrent",
    ],
    "cogs": [
        "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization"
    ],  # flag cogs_excludes_da
    "inventory": [
        "InventoryNetOfAllowancesCustomerAdvancesAndProgressBillings",
        "RetailRelatedInventoryMerchandise",
    ],
    "redeemable_nci": ["RedeemableNoncontrollingInterestEquityFairValue"],
    "interest_expense": ["InterestExpenseOperating", "InterestExpenseBorrowings"],
    "minority_interest": ["MinorityInterestInOperatingPartnerships"],
    "net_income": [
        "NetIncomeLossAvailableToCommonStockholdersBasic"
    ],  # only without preferred dividends
}
FAMILY = {  # components: accepted only when the sole family member filed (then it is the total)
    "short_term_investments": r"AvailableForSaleSecuritiesDebtSecuritiesCurrent|DebtSecuritiesAvailableForSaleExcludingAccruedInterestCurrent|OtherShortTermInvestments|TradingSecurities|TradingSecuritiesDebt|TradingSecuritiesEquity|HeldToMaturitySecuritiesCurrent",
    "inventory": r"EnergyRelatedInventory\w*|Inventory(PartsAndComponents|FinishedGoods|RawMaterials|WorkInProcess|Supplies)\w*|OtherInventory\w*",
    "cash": r"Cash|CashEquivalentsAtCarryingValue",
}
FAMILY_SAME = [
    {
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        "DebtSecuritiesAvailableForSaleExcludingAccruedInterestCurrent",
    },
    {"TradingSecurities", "TradingSecuritiesDebt", "TradingSecuritiesEquity"},
]
DEBT = {  # debt components: added to the MET-07 sums (no sole rule; the sum rule decides)
    "long_term_debt": [
        "SeniorNotes",
        "ConvertibleLongTermNotesPayable",
        "LongTermNotesPayable",
        "OtherLongTermDebtNoncurrent",
    ],
    "short_term_debt": [
        "OtherShortTermBorrowings",
        "ConvertibleDebtCurrent",
        "ConvertibleNotesPayableCurrent",
        "LinesOfCreditCurrent",
    ],
}
SUMS = {
    "depreciation_amortization": ("Depreciation", "AmortizationOfIntangibleAssets"),
    "pretax_income": (
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesForeign",
    ),
}


def families(names):
    out = []
    for n in names:
        if not any(n in s and any(m in out for m in s) for s in FAMILY_SAME):
            out.append(n)
    return out


def grade(acc, item):
    sec, stored = cand.get((acc, item), ([], []))
    st = {x.split("_", 1)[-1] for x in stored}
    hit = None
    if any(c in sec for c in TOTAL.get(item, [])):
        hit = next(c for c in TOTAL[item] if c in sec)
    elif item in FAMILY:
        fam = families([c for c in sec if re.fullmatch(FAMILY[item], c)])
        if item == "cash":
            fam = [c for c in fam if c == "Cash"] if fam == ["Cash"] else []
        if len(fam) == 1:
            hit = fam[0]
    elif any(c in sec for c in DEBT.get(item, [])):
        hit = next(c for c in DEBT[item] if c in sec)
    elif item in SUMS and all(p in sec for p in SUMS[item]):
        hit = "+".join(SUMS[item])
    if hit:
        served = all(p in st for p in hit.split("+"))
        return ("strong-served" if served else "strong-notes"), hit
    if stored and not sec:
        return "custom", None
    if sec or stored:
        return "weak", None
    return "none", None


pg = {k: grade(*k) for k in cand}
filled = collections.Counter()
for m, j in sqlite3.connect(f"file:{DB}?mode=ro", uri=True).execute(
    "select metric_name, inputs_json from fundamental_metrics where value is not null"
):
    d = json.loads(j) if j else {}
    p = d.get("provenance") or {}
    for i, l in (p.get("items") or {}).items():
        if l and l[0].get("concept") is None and d.get(i) is None:
            filled[(m, i)] += 1
ORDER = ["strong-served", "strong-notes", "custom", "weak", "none"]
out = collections.Counter()
hits = collections.defaultdict(collections.Counter)
rows = []
for r in csv.DictReader(open(CSV6)):
    if r["bucket"] != "notreported":
        continue
    its = [
        x.rsplit(":", 1)[0]
        for x in filter(None, r["missing_items"].split(";"))
        if x.endswith(":notreported")
    ]
    its = [i for i in its if filled[(r["metric"], i)] == 0] or its
    gs = {pg[(r["accession"], i)][0] for i in its}
    b = next(g for g in ORDER if g in gs)
    out[b] += 1
    rows.append(
        (
            r["ticker"],
            r["accession"],
            r["form"],
            r["period_end"],
            r["metric"],
            b,
            ";".join(
                f"{i}:{pg[(r['accession'], i)][0]}:{pg[(r['accession'], i)][1] or ''}" for i in its
            ),
        )
    )
for (a, i), (g, h) in pg.items():
    if h:
        hits[(i, g)][h] += 1
print("bucket 6 per empty metric value (strict):")
[print(f"  {out[g]:6d}  {g}") for g in ORDER]
print("\nsubstitutes that matched (distinct filing-items):")
for i, g in sorted(hits):
    [print(f"  {i:24s} {g:14s} {n:5d}  {h}") for h, n in hits[(i, g)].most_common()]
with open(OUT, "w", newline="") as h:
    w = csv.writer(h)
    w.writerow(["ticker", "accession", "form", "period_end", "metric", "grade", "items"])
    w.writerows(rows)
