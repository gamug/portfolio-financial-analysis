"""Round #11 copy of audit/plan_apply.py: a stored row with no provenance block (the rule-input rows) takes the fixed
run's whole block when the fix adds one (Part 2's empty reasons) -- plan_apply.py counted it as provenance_not_dict and
dropped it. Review B2: rows are keyed by (ticker, accession) -- two assets can share a CIK (DISCK / WBD) -- and no stored
id may be planned twice.

Build the apply plan of a fix batch (read-only): for each shard, the rows the fix changes.

    plan_apply.py <stored_db> <out_plan.jsonl> <out_summary.csv> base_0.db:fix_0.db [base_1.db:fix_1.db ...]

A metric row (filing, group, metric) is **in the plan** when the fixed run differs from the master run (value, or
inputs other than provenance) **and** the master run reproduces the stored value (so the change is the fix's, not
the offline rebuild's). The planned row = the stored row with the fix's delta applied: the fixed value; the stored
inputs with every key whose value differs between the master and the fixed run set to the fixed run's (removed
when the fixed run drops it); the stored provenance with each item entry that differs replaced. Rows the fix
changes where the master run does not reproduce the stored value are written as ``review`` (never applied).
"""

import ast
import collections
import csv
import json
import sqlite3
import sys

STORED, PLAN, SUMMARY, *PAIRS = sys.argv[1:]
st = sqlite3.connect(f"file:{STORED}?mode=ro", uri=True)


def load(j):
    d = json.loads(j) if j else {}
    p = d.pop("provenance", None)
    as_str = isinstance(p, str)
    if as_str:
        try:
            p = ast.literal_eval(p)
        except Exception:
            pass
    return d, p, as_str


def eqv(a, b):
    return (a is None and b is None) or (
        a is not None and b is not None and abs(a - b) <= 1e-9 * max(1.0, abs(a))
    )


plan = open(PLAN, "w")
cnt = collections.Counter()
bym = collections.Counter()
byt = collections.defaultdict(collections.Counter)
for pair in PAIRS:
    bdb, fdb = pair.split(":")
    b = sqlite3.connect(f"file:{bdb}?mode=ro", uri=True)
    f = sqlite3.connect(f"file:{fdb}?mode=ro", uri=True)
    acc2id = {
        (t, a): i
        for t, a, i in st.execute(
            "select x.ticker, s.accession_number, s.id from sec_filings s join assets x on x.id=s.asset_id"
        )
    }

    def rows(con):
        out = {}
        for t, acc, g, n, v, j in con.execute(
            "select x.ticker, s.accession_number, m.metric_group, m.metric_name, m.value, m.inputs_json from fundamental_metrics m join sec_filings s on s.id=m.filing_id join assets x on x.id=s.asset_id where m.engine_version='metrics-v11'"
        ):
            out[((t, acc), g, n)] = (v, j)
        return out

    B, F = rows(b), rows(f)
    for key in B.keys() | F.keys():
        acc, g, n = key
        if key not in B or key not in F:
            cnt["row_only_in_one_run"] += 1
            continue
        (vb, jb), (vf, jf) = B[key], F[key]
        db_, pb, _ = load(jb)
        df, pf, _ = load(jf)
        if eqv(vb, vf) and db_ == df and pb == pf:
            continue
        sid = acc2id.get(acc)
        acc = acc[1]
        srow = st.execute(
            "select id, value, inputs_json from fundamental_metrics where filing_id=? and metric_group=? and metric_name=? and engine_version='metrics-v10'",
            (sid, g, n),
        ).fetchone()
        if srow is None:
            cnt["no_stored_row"] += 1
            continue
        rid, vs, js = srow
        ds, ps, ps_str = load(js)
        ticker = st.execute(
            "select a.ticker from sec_filings s join assets a on a.id=s.asset_id where s.id=?",
            (sid,),
        ).fetchone()[0]
        value_change = not eqv(vb, vf)
        if not eqv(vb, vs):
            cnt["review_master_differs_from_stored"] += 1
            plan.write(
                json.dumps(
                    {
                        "kind": "review",
                        "id": rid,
                        "ticker": ticker,
                        "acc": acc,
                        "metric": n,
                        "stored": vs,
                        "master": vb,
                        "fixed": vf,
                    }
                )
                + "\n"
            )
            continue
        new = dict(ds)
        for k in db_.keys() | df.keys():
            if db_.get(k) != df.get(k):
                if k in df:
                    new[k] = df[k]
                else:
                    new.pop(k, None)
        newp = ps
        if pb != pf and isinstance(ps, dict) and isinstance(pf, dict) and isinstance(pb, dict):
            newp = dict(ps)
            items = dict(ps.get("items") or {})
            for it in set(pb.get("items") or {}) | set(pf.get("items") or {}):
                if (pb.get("items") or {}).get(it) != (pf.get("items") or {}).get(it):
                    if it in (pf.get("items") or {}):
                        items[it] = pf["items"][it]
                    else:
                        items.pop(it, None)
            newp["items"] = items
            # every other top-level key the fix changes (empty_reason, flags, the market_cap record, ...)
            for k in (set(pb) | set(pf)) - {"items"}:
                if pb.get(k) != pf.get(k):
                    if k in pf:
                        newp[k] = pf[k]
                    else:
                        newp.pop(k, None)
        elif pb != pf and ps is None and pb is None and isinstance(pf, dict):
            newp = pf
            cnt["provenance_added"] += 1
        elif pb != pf:
            cnt["provenance_not_dict"] += 1
        if newp is not None:
            new["provenance"] = repr(newp) if ps_str else newp
        cls = "value" if value_change else "inputs_only"
        cnt["apply_" + cls] += 1
        bym[(n, cls)] += 1
        byt[ticker][cls] += 1
        plan.write(
            json.dumps(
                {
                    "kind": "apply",
                    "id": rid,
                    "ticker": ticker,
                    "acc": acc,
                    "group": g,
                    "metric": n,
                    "old": vs,
                    "new": vf,
                    "inputs_json": json.dumps(new),
                }
            )
            + "\n"
        )
plan.close()
ids = [json.loads(l)["id"] for l in open(PLAN) if json.loads(l)["kind"] == "apply"]
assert len(ids) == len(set(ids)), ("a stored id planned twice", len(ids) - len(set(ids)))
print(dict(cnt))
with open(SUMMARY, "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["ticker", "value_changes", "inputs_only"])
    for t, c in sorted(byt.items(), key=lambda x: -x[1]["value"]):
        w.writerow([t, c["value"], c["inputs_only"]])
print(
    "by metric (value changes):",
    sorted(((m, k) for (m, c), k in bym.items() if c == "value"), key=lambda x: -x[1])[:25],
)
print("companies with value changes:", sum(1 for c in byt.values() if c["value"]))
