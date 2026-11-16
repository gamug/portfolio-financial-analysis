"""T-156 phase 1 — M3: the effect of each recoverable class on what reads EBITDA at 2026-09-22.

before -> (if adopted): VALORIZATION EBITDA/EV coverage by company type, net_debt_to_ebitda
coverage, the D-04 (LEVERAGE_EXTREME) screen's evaluated count. Computed from the stored
value_inputs row (D1), the market cap reader and the applicability matrix; no code change.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path

from cycle import data
from kg_schema.applicability import applicability
from kg_schema.market_cap import market_caps_as_of
from kg_schema.versions import resolve_metric_versions

DB = "/workspaces/thesis/scratch/t156/work.db"
UNIVERSE = "SP500"
UNIVERSE_DB = "/workspaces/thesis/data/universe_history.db"
DATE = "2026-09-22"
OUT = Path("/workspaces/thesis/scratch/t156/out")

# the classes whose candidate would fill the ebitda term (so ebitda becomes present)
RECOVERABLE = (
    "da_income_statement_only",
    "da_extension_only",
    "da_element_with_other",
    "da_components_no_depreciation",
    "da_refused_depreciation_only",
    "ttm_refused",
    "ttm_term_absent",
    "oi_absent_presentation",
)


def kind(prof) -> str:
    if prof is None:
        return "unknown_profile"
    if prof.financial_firm:
        return "financial_firm"
    if prof.reit:
        return "reit"
    if prof.regulated_utility:
        return "regulated_utility"
    if prof.type == "multi_sector_holding":
        return "multi_sector_holding"
    return "operating"


def main() -> None:  # noqa: C901, PLR0915 - one pass over the members
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    dro = data.connect_ro(DB)
    versions = resolve_metric_versions(dro, None)
    members = data.active_universe(dro, UNIVERSE, DATE, UNIVERSE_DB)
    ids = [int(r["id"]) for r in members]
    profiles = data.asset_profiles(dro, ids, DATE)
    mcap = market_caps_as_of(dro, ids, as_of=DATE).caps
    metrics = data.latest_metrics(dro, DATE, versions)
    dq = data.data_quality(dro, DATE, versions)

    classes = {r["asset"]: r for r in csv.DictReader(OUT.joinpath("m1_rows.csv").open())}

    # the value_inputs rule_input row of each member's newest usable filing
    rows = conn.execute(
        """
        WITH known AS (
            SELECT f.id, f.asset_id, ROW_NUMBER() OVER (
                PARTITION BY f.asset_id ORDER BY f.period_end DESC, f.available_at DESC, f.id DESC
            ) AS rn FROM sec_filings f
            WHERE f.period_end IS NOT NULL AND f.available_at IS NOT NULL AND f.available_at <= ?
        )
        SELECT k.asset_id, m.value, m.inputs_json
        FROM known k LEFT JOIN fundamental_metrics m ON m.filing_id = k.id
             AND m.metric_group='rule_input' AND m.metric_name='value_inputs'
        WHERE k.rn = 1
        """,
        (DATE,),
    ).fetchall()
    values = {int(r["asset_id"]): json.loads(r["inputs_json"] or "{}") for r in rows}

    def num(v):
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    def ev_of(aid: int) -> float | None:
        v = values.get(aid, {})
        cap = mcap[aid].value if aid in mcap else None
        debt, nci, cash = num(v.get("total_debt")), num(v.get("nci")), num(v.get("cash"))
        if cap is None or debt is None or cash is None or nci is None:
            return None
        return cap + debt + nci - cash - (num(v.get("short_term_investments")) or 0.0)

    def ebitda_of(aid: int) -> float | None:
        return num(values.get(aid, {}).get("ebitda"))

    # D-04 evaluated: every negative-equity filing + every filing with a D/E (builtin._LeverageRule)
    def d04_evaluated(aid: int) -> bool:
        if aid in dq.negative_equity:
            return True
        return metrics.get(aid, {}).get("leverage.debt_to_equity") is not None

    def snapshot(extra_ebitda: set[int]) -> dict:
        per_kind: dict[str, Counter] = {}
        ndte_per_kind: dict[str, Counter] = {}
        d04 = 0
        for r in members:
            aid = int(r["id"])
            prof = profiles.get(aid)
            k = kind(prof)
            ebitda = ebitda_of(aid)
            if aid in extra_ebitda and ebitda is None:
                ebitda = 1.0  # the candidate fills the term; the value is not needed for presence
            ev = ev_of(aid)
            code = (
                "A"
                if prof is None
                else applicability(
                    "enterprise_value", prof, negative_equity=aid in dq.negative_equity
                )[0]
            )
            present = code != "NA" and ev is not None and ev > 0.0 and ebitda is not None
            per_kind.setdefault(k, Counter())["members"] += 1
            per_kind[k]["applicable"] += code != "NA"
            per_kind[k]["present"] += present
            ndte = metrics.get(aid, {}).get("leverage.net_debt_to_ebitda")
            if ndte is None and aid in extra_ebitda:
                # the candidate fills ebitda; the ratio also needs net debt, and MET-09 keeps an
                # EBITDA <= 0 empty. The recovered value is not recomputed in phase 1, so this is
                # an upper bound (counted as "ebitda formed and net debt present").
                v = values.get(aid, {})
                ndte = num(v.get("total_debt")) is not None and num(v.get("cash")) is not None
            ndte_per_kind.setdefault(k, Counter())["members"] += 1
            ndte_per_kind[k]["present"] += bool(ndte)
            if d04_evaluated(aid):
                d04 += 1
        return {
            "ebitda_to_ev": {k: dict(v) for k, v in per_kind.items()},
            "ndte": {k: dict(v) for k, v in ndte_per_kind.items()},
            "d04_evaluated": d04,
        }

    before = snapshot(set())
    report = {"date": DATE, "before": before, "classes": {}}
    by_class: dict[str, set[int]] = {}
    for a, r in classes.items():
        for c in (r["da_class"], r["oi_class"]):
            if c in RECOVERABLE:
                by_class.setdefault(c, set()).add(int(a))
    for c, aids in sorted(by_class.items()):
        after = snapshot(aids)
        report["classes"][c] = {
            "members": len(aids),
            "ebitda_to_ev_present_before": sum(
                v["present"] for v in before["ebitda_to_ev"].values()
            ),
            "ebitda_to_ev_present_after": sum(v["present"] for v in after["ebitda_to_ev"].values()),
            "ebitda_to_ev_gain": sum(v["present"] for v in after["ebitda_to_ev"].values())
            - sum(v["present"] for v in before["ebitda_to_ev"].values()),
            "ndte_gain": sum(v["present"] for v in after["ndte"].values())
            - sum(v["present"] for v in before["ndte"].values()),
            "d04_evaluated_before": before["d04_evaluated"],
            "d04_evaluated_after": after["d04_evaluated"],
            "after_by_kind": after["ebitda_to_ev"],
        }
    all_rec = set().union(*by_class.values()) if by_class else set()
    report["all_recoverable"] = {
        "members": len(all_rec),
        "after": snapshot(all_rec),
    }
    OUT.joinpath("m3_effect.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps(report["before"], indent=1))
    print(
        json.dumps(
            {
                c: {k: v for k, v in d.items() if k != "after_by_kind"}
                for c, d in report["classes"].items()
            },
            indent=1,
        )
    )
    conn.close()
    dro.close()


if __name__ == "__main__":
    main()
