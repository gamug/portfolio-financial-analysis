"""T-156 phase 1 — M1 audit dump at 2026-09-22.

One row per SP500 member as of the date: its newest usable filing, the stored leverage inputs,
and the D&A / operating-income facts that filing carries (from `financial_facts`).

Read-only. Output: out/p4_audit_MS.json
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from cycle import data
from kg_schema.market_cap import market_caps_as_of
from kg_schema.versions import resolve_metric_versions

DB = "/workspaces/thesis/scratch/t156/work.db"
UNIVERSE = "SP500"
UNIVERSE_DB = "/workspaces/thesis/data/universe_history.db"
DATE = "2026-09-22"

DA_RE = re.compile(r"depreciat|amorti|deplet", re.IGNORECASE)
SUB_RE = re.compile(
    r"OperatingIncomeLoss$|IncomeLossFromContinuingOperations|IncomeLossBeforeIncomeTaxes"
    r"|IncomeTaxExpenseBenefit$|ProfitLoss$|NetIncomeLoss$|GrossProfit$|OperatingExpenses$"
    r"|CostsAndExpenses$|Revenues$|RevenueFromContractWithCustomerExcludingAssessedTax$"
    r"|InterestExpense$|NonoperatingIncomeExpense$|IncomeLossFromEquityMethodInvestments$"
)


def newest_filings(conn: sqlite3.Connection, date: str) -> dict[int, sqlite3.Row]:
    rows = conn.execute(
        """
        WITH known AS (
            SELECT f.id, f.asset_id, ROW_NUMBER() OVER (
                PARTITION BY f.asset_id ORDER BY f.period_end DESC, f.available_at DESC, f.id DESC
            ) AS rn
            FROM sec_filings f
            WHERE f.period_end IS NOT NULL AND f.available_at IS NOT NULL AND f.available_at <= ?
        )
        SELECT k.asset_id, s.id AS filing_id, s.form, s.accession_number, s.period_end,
               s.fiscal_year, s.fiscal_period, s.filing_date, s.available_at
        FROM known k JOIN sec_filings s ON s.id = k.id WHERE k.rn = 1
        """,
        (date,),
    ).fetchall()
    return {int(r["asset_id"]): r for r in rows}


def main() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    dro = data.connect_ro(DB)
    versions = resolve_metric_versions(dro, None)
    members = data.active_universe(dro, UNIVERSE, DATE, UNIVERSE_DB)
    ids = [int(r["id"]) for r in members]
    profiles = data.asset_profiles(dro, ids, DATE)
    mcap = market_caps_as_of(dro, ids, as_of=DATE).caps
    metrics = data.latest_metrics(dro, DATE, versions)
    filings = newest_filings(conn, DATE)

    payload: dict[str, object] = {"date": DATE, "members": {}}
    for r in members:
        aid = int(r["id"])
        f = filings.get(aid)
        prof = profiles.get(aid)
        vals = metrics.get(aid, {})
        entry: dict[str, object] = {
            "ticker": r["ticker"],
            "sector": r["sector"],
            "type": None if prof is None else prof.type,
            "financial_firm": bool(prof and prof.financial_firm),
            "reit": bool(prof and prof.reit),
            "regulated_utility": bool(prof and prof.regulated_utility),
            "has_usable_filing": f is not None,
            "market_cap": (mcap[aid].value if aid in mcap else None),
            "ebitda": vals.get("leverage.ebitda"),
            "net_debt_to_ebitda": vals.get("leverage.net_debt_to_ebitda"),
        }
        if f is not None:
            entry.update(
                {
                    "filing_id": f["filing_id"],
                    "form": f["form"],
                    "accession": f["accession_number"],
                    "period_end": f["period_end"],
                    "fiscal_year": f["fiscal_year"],
                    "fiscal_period": f["fiscal_period"],
                    "filing_date": f["filing_date"],
                    "available_at": f["available_at"],
                }
            )
            # inputs_json is shared across the group; read the ebitda row's
            row = conn.execute(
                "SELECT value, inputs_json FROM fundamental_metrics WHERE filing_id=? AND "
                "metric_group='leverage' AND metric_name='ebitda'",
                (f["filing_id"],),
            ).fetchone()
            entry["inputs"] = json.loads(row["inputs_json"] or "{}") if row else None
            # facts
            facts = conn.execute(
                "SELECT concept, label, statement, period_key, value, preferred_sign "
                "FROM financial_facts WHERE filing_id=?",
                (f["filing_id"],),
            ).fetchall()
            da_facts = []
            sub_facts = []
            oi_facts = []
            for x in facts:
                concept = str(x["concept"] or "")
                label = str(x["label"] or "")
                local = concept.split("_", 1)[-1]
                if DA_RE.search(local) or DA_RE.search(label):
                    da_facts.append(
                        {
                            "concept": concept,
                            "label": label,
                            "statement": x["statement"],
                            "period_key": x["period_key"],
                            "value": x["value"],
                        }
                    )
                if concept.endswith("OperatingIncomeLoss"):
                    oi_facts.append(
                        {
                            "concept": concept,
                            "label": label,
                            "period_key": x["period_key"],
                            "value": x["value"],
                        }
                    )
                if SUB_RE.search(local):
                    sub_facts.append(
                        {
                            "concept": concept,
                            "label": label,
                            "period_key": x["period_key"],
                            "value": x["value"],
                        }
                    )
            entry["da_facts"] = da_facts
            entry["oi_facts"] = oi_facts
            entry["income_subtotals"] = sub_facts
        payload["members"][str(aid)] = entry

    Path("/workspaces/thesis/scratch/t156/out/p4_audit_MS.json").write_text(
        json.dumps(payload, indent=1, default=str)
    )
    print("wrote out/p4_audit_MS.json", len(payload["members"]))
    conn.close()
    dro.close()


if __name__ == "__main__":
    main()
