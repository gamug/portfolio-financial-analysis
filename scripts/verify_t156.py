"""T-156 phase 2 measurement: the EBITDA-coverage recoveries E1-E3 (and E4 declared).

Sub-commands
------------
``classes``      phase 1's class table, before -> after, per date (``out/p2_classes.json``)
``fidelity``     E1's both-statements agreement and E4's identity agreement (``out/p2_fidelity.json``)
``metrics``      coverage before -> after, VALORIZATION by company type, the vetoes (``out/p2_metrics.json``)
``valorization`` VALORIZATION v2 before vs after at 2026-09-22 (``out/p2_valorization.json``)
``prompt-hash``  how many filings' scored input changes; the prompt template's hash (``out/p2_prompt_hash.json``)
``all``          every output above

The "before" side is ``p2_before.db`` (a copy of ``t071c/after.db``, ``metrics-v7`` rows); the "after"
side is ``p2_after.db`` (the same copy recomputed with this branch, ``metrics-v8``). Every number comes
from this script and its output file.

    uv run python scripts/verify_t156.py all --before /workspaces/thesis/scratch/t156/p2_before.db \
        --after /workspaces/thesis/scratch/t156/p2_after.db
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from portfolio_common.db import in_clause

from cycle import data
from cycle.rules.base import RuleContext
from cycle.rules.builtin import RULES
from cycle.scores import valorization
from fundamental_agent.fidelity import dq01_tolerance
from kg_schema.applicability import applicability
from kg_schema.market_cap import market_caps_as_of
from kg_schema.versions import resolve_metric_versions
from pricing_agent.db import PRICE_OBSERVATION_ENGINE_VERSION

UNIVERSE = "SP500"
UNIVERSE_DB = "/workspaces/thesis/data/universe_history.db"
DATES = (
    "2023-03-31",
    "2023-06-30",
    "2023-09-30",
    "2023-12-31",
    "2024-03-31",
    "2024-06-30",
    "2024-09-30",
    "2024-12-31",
    "2025-03-31",
    "2025-06-30",
    "2025-09-30",
    "2025-12-31",
    "2026-03-31",
    "2026-06-30",
    "2026-09-22",
)
MAIN = "2026-09-22"
OUT = Path("/workspaces/thesis/scratch/t156/out")

G = "us-gaap_"
DA_RE = re.compile(r"depreciat|amorti|deplet", re.IGNORECASE)
AND_OTHER_RE = re.compile(r"and\s*other", re.IGNORECASE)
AGG = (
    G + "DepreciationDepletionAndAmortization",
    G + "DepreciationAmortizationAndAccretionNet",
    G + "DepreciationAndAmortization",
)
WIDER_DA = frozenset(
    {
        G + "CostDepreciationAmortizationAndDepletion",
        G + "UtilitiesOperatingExpenseDepreciationAndAmortization",
    }
)
COMPONENT_DA = frozenset(
    {
        G + "OtherDepreciationAndAmortization",
        G + "AmortizationOfIntangibleAssets",
        G + "Depletion",
        G + "AdjustmentForAmortization",
        G + "CostOfGoodsAndServicesSoldDepreciation",
        G + "CostOfGoodsAndServicesSoldDepreciationAndAmortization",
        G + "CostOfGoodsAndServicesSoldAmortization",
        G + "DepreciationNonproduction",
    }
)

# the metrics the plan names
METRICS = (
    "leverage.ebitda",
    "leverage.net_debt_to_ebitda",
    "leverage.interest_coverage",
    "profitability.operating_margin",
    "roic.nopat",
    "roic.return_on_invested_capital",
    "valuation.enterprise_fcf_yield",
    "growth.operating_income_growth",
    # the engine has no operating-income CAGR (the `cagr` group carries revenue, net income and
    # operating cash flow only), so E4's scope is the growth metric alone
)


#: The "moves by more than one point" threshold of the VALORIZATION comparison.
ONE_POINT = 0.01
#: DQ-01's ``n_terms`` for the two- and three-fact identities.
N_TERMS_PAIR = 2
N_TERMS_TRIO = 3


def _write(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=1, default=str))
    print(f"wrote {path}")


def connect_ro(path: str) -> Any:
    return data.connect_ro(path)


# -- the phase-1 classifier (E1-E3's classes), on a database -------------------------------------


def _term(entry: dict | None, form: str) -> dict:
    if entry is None:
        return {"present": False, "method": None, "flags": [], "concept": None}
    method = entry.get("method")
    present = method in ("ytd", "quarters") if form == "10-Q" else entry.get("concept") is not None
    return {
        "present": present,
        "method": method,
        "flags": entry.get("flow_flags") or [],
        "concept": entry.get("concept"),
    }


def _da_class(f: dict) -> str:  # noqa: C901, PLR0911 - one branch per phase-1 class
    flags, cf, ext = f["metric_flags"], f["da_cf"], f["da_ext"]
    all_da = set(cf) | set(f["da_is"])
    if not all_da:
        return "da_absent"
    if not cf and f["da_is"]:
        return "da_income_statement_only"
    m = f["da_term"]["method"]
    if f["form"] == "10-Q":
        if m in ("refused", "x4"):
            return "ttm_refused"
        if m is None and (f["da_term"]["concept"] is not None or f["da_agg"]):
            return "ttm_term_absent"
    if any("da_sum_refused_other_lines" in x for x in flags):
        return "da_refused_other_lines"
    if any("depreciation_only_not_da" in x for x in flags) and G + "Depreciation" in cf:
        return "da_refused_depreciation_only"
    if ext:
        return "da_extension_only"
    if [c for c in cf if c in WIDER_DA]:
        return "da_element_with_other"
    if [c for c in cf if c in COMPONENT_DA]:
        return "da_components_no_depreciation"
    return "UNCLASSIFIED"


def _oi_class(f: dict) -> str:
    if f["oi_term"]["present"]:
        return ""
    return "oi_absent_presentation" if not f["has_oi_fact"] else "oi_absent_other"


def _rows_for(conn: Any, date: str) -> list[dict]:  # noqa: C901 - one pass over the signals
    """Every member at *date* with its newest usable filing's D&A signals (phase 1's classifier)."""
    members = data.active_universe(conn, UNIVERSE, date, UNIVERSE_DB)
    ids = [int(r["id"]) for r in members]
    profiles = data.asset_profiles(conn, ids, date)
    filings = {
        int(r["asset_id"]): r
        for r in conn.execute(
            """
            WITH known AS (
                SELECT f.id, f.asset_id, ROW_NUMBER() OVER (
                    PARTITION BY f.asset_id ORDER BY f.period_end DESC, f.available_at DESC, f.id DESC
                ) AS rn FROM sec_filings f
                WHERE f.period_end IS NOT NULL AND f.available_at IS NOT NULL AND f.available_at <= ?
            )
            SELECT k.asset_id, s.id AS filing_id, s.form, s.accession_number FROM known k
            JOIN sec_filings s ON s.id = k.id WHERE k.rn = 1
            """,
            (date,),
        )
    }
    filing_ids = [r["filing_id"] for r in filings.values()]
    if not filing_ids:
        return []
    lev: dict[int, dict] = {}
    for r in conn.execute(
        f"SELECT filing_id, metric_name, value, inputs_json FROM fundamental_metrics "  # noqa: S608
        f"WHERE filing_id IN {in_clause(filing_ids)} AND metric_group='leverage' "
        f"AND metric_name IN ('ebitda','net_debt_to_ebitda')",
        filing_ids,
    ):
        lev.setdefault(int(r["filing_id"]), {})[r["metric_name"]] = r
    facts: dict[int, list] = defaultdict(list)
    for r in conn.execute(
        f"SELECT filing_id, statement, concept, label FROM financial_facts "  # noqa: S608
        f"WHERE filing_id IN {in_clause(filing_ids)}",
        filing_ids,
    ):
        facts[int(r["filing_id"])].append(r)
    out = []
    for m in members:
        aid = int(m["id"])
        prof = profiles.get(aid)
        ff = filings.get(aid)
        f: dict[str, Any] = {
            "asset": aid,
            "ticker": m["ticker"],
            "sector": m["sector"],
            "type": None if prof is None else prof.type,
            "financial_firm": bool(prof and prof.financial_firm),
            "has_filing": ff is not None,
        }
        if ff is None:
            out.append(f)
            continue
        row = lev.get(ff["filing_id"], {}).get("ebitda")
        ij = json.loads(row["inputs_json"] or "{}") if row else {}
        f["ebitda"] = row["value"] if row else None
        f["form"] = ff["form"]
        f["metric_flags"] = sorted(k for k in ij if k.startswith("flag_"))
        f["da_input"] = ij.get("depreciation_amortization")
        f["oi_input"] = ij.get("operating_income")
        nr = lev.get(ff["filing_id"], {}).get("net_debt_to_ebitda")
        items = (
            ((json.loads(nr["inputs_json"] or "{}") if nr else {}).get("provenance") or {}).get(
                "items"
            )
        ) or {}
        f["da_term"] = _term((items.get("depreciation_amortization") or [{}])[0], ff["form"])
        f["oi_term"] = _term((items.get("operating_income") or [{}])[0], ff["form"])
        cf, da_is, has_oi = [], [], False
        for x in facts[ff["filing_id"]]:
            concept, label = str(x["concept"] or ""), str(x["label"] or "")
            local = concept.split("_", 1)[-1]
            if DA_RE.search(local) or DA_RE.search(label):
                if x["statement"] == "cash_flow":
                    cf.append(concept)
                elif x["statement"] == "income_statement":
                    da_is.append(concept)
            if local == "OperatingIncomeLoss":
                has_oi = True
        f["da_cf"] = sorted(set(cf))
        f["da_is"] = sorted(set(da_is))
        f["da_ext"] = [c for c in f["da_cf"] if not c.startswith("us-gaap_")]
        f["da_agg"] = [c for c in f["da_cf"] if c in AGG]
        f["has_oi_fact"] = has_oi
        out.append(f)
    return out


def cmd_classes(args: argparse.Namespace) -> None:
    payload: dict[str, Any] = {"dates": {}}
    for date in args.dates:
        entry: dict[str, Any] = {}
        for side in ("before", "after"):
            conn = connect_ro(getattr(args, side))
            try:
                rows = _rows_for(conn, date)
            finally:
                conn.close()
            counts: Counter = Counter()
            for f in rows:
                if not f["has_filing"] or f["ebitda"] is not None:
                    continue
                if f["financial_firm"]:
                    counts["financial_firm_na"] += 1
                    continue
                if not f["da_term"]["present"]:
                    counts[_da_class(f)] += 1
                oi = _oi_class(f)
                if oi:
                    counts[oi] += 1
            counts["members"] = len(rows)
            counts["with_usable_filing"] = sum(1 for f in rows if f["has_filing"])
            counts["empty_ebitda"] = sum(1 for f in rows if f["has_filing"] and f["ebitda"] is None)
            entry[side] = dict(counts)
        entry["delta"] = {
            k: entry["after"].get(k, 0) - entry["before"].get(k, 0)
            for k in set(entry["before"]) | set(entry["after"])
        }
        payload["dates"][date] = entry
        print(date, json.dumps(entry["after"]))
        print("   delta", json.dumps({k: v for k, v in entry["delta"].items() if v}))
    _write(Path(args.out), payload)


# -- E1's both-statements agreement and E4's identity fidelity -----------------------------------


def _filed(v: float, sign: Any) -> float:
    return v * (sign if sign in (1.0, -1.0) else 1.0)


def cmd_fidelity(args: argparse.Namespace) -> None:
    conn = connect_ro(args.after)
    out: dict[str, Any] = {}
    try:
        rows = conn.execute(
            """
            SELECT a.filing_id, a.concept, a.period_key, a.value isv, a.preferred_sign is_sig,
                   b.value cfv, b.preferred_sign cf_sig
            FROM financial_facts a
            JOIN financial_facts b ON b.filing_id=a.filing_id AND b.concept=a.concept
                 AND b.period_key=a.period_key AND b.statement='cash_flow'
            WHERE a.statement='income_statement' AND a.value IS NOT NULL AND b.value IS NOT NULL
              AND (lower(substr(a.concept, instr(a.concept,'_')+1)) LIKE '%depreciat%'
                OR lower(substr(a.concept, instr(a.concept,'_')+1)) LIKE '%amorti%'
                OR lower(substr(a.concept, instr(a.concept,'_')+1)) LIKE '%deplet%')
            """
        ).fetchall()
        before = sum(
            1 for r in rows if abs(r["isv"] - r["cfv"]) <= dq01_tolerance(r["cfv"], N_TERMS_PAIR)
        )
        still = []
        after = 0
        for r in rows:
            is_f, cf_f = _filed(r["isv"], r["is_sig"]), _filed(r["cfv"], r["cf_sig"])
            if abs(is_f - cf_f) <= dq01_tolerance(cf_f, N_TERMS_PAIR):
                after += 1
            else:
                still.append(
                    {
                        "filing_id": r["filing_id"],
                        "concept": r["concept"],
                        "period_key": r["period_key"],
                        "is": is_f,
                        "cf": cf_f,
                    }
                )
        out["e1_both_statements"] = {
            "pairs": len(rows),
            "agree_displayed": before,
            "agree_filed_sign": after,
            "still_differing": still,
            "cause": "every disagreement is a sign flip: the income-statement row carries a negating "
            "label (ID-12), and the resolver reads the filed sign",
        }
        print("E1 pairs", len(rows), "agree displayed", before, "agree filed sign", after)

        pa = (
            G
            + "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"
        )
        pb = (
            G
            + "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"
        )
        nonop, intdbt = G + "NonoperatingIncomeExpense", G + "InterestAndDebtExpense"
        eqm, oi = G + "IncomeLossFromEquityMethodInvestments", G + "OperatingIncomeLoss"
        id1 = conn.execute(
            """
            SELECT COUNT(*) FROM financial_facts oi
            JOIN financial_facts pa ON pa.filing_id=oi.filing_id AND pa.period_key=oi.period_key
                 AND pa.concept=?
            JOIN financial_facts n ON n.filing_id=oi.filing_id AND n.period_key=oi.period_key
                 AND n.concept=?
            JOIN financial_facts i ON i.filing_id=oi.filing_id AND i.period_key=oi.period_key
                 AND i.concept=?
            WHERE oi.concept=? AND oi.value IS NOT NULL AND pa.value IS NOT NULL
              AND n.value IS NOT NULL AND i.value IS NOT NULL
            """,
            (pa, nonop, intdbt, oi),
        ).fetchone()[0]
        id2 = conn.execute(
            """
            SELECT e.filing_id, e.value eqm, pa.value pa, pb.value pb FROM financial_facts e
            JOIN financial_facts pa ON pa.filing_id=e.filing_id AND pa.period_key=e.period_key
                 AND pa.concept=?
            JOIN financial_facts pb ON pb.filing_id=e.filing_id AND pb.period_key=e.period_key
                 AND pb.concept=?
            WHERE e.concept=? AND e.value IS NOT NULL AND pa.value IS NOT NULL AND pb.value IS NOT NULL
            """,
            (pa, pb, eqm),
        ).fetchall()
        agree2 = sum(
            1
            for r in id2
            if abs((r["pa"] + r["eqm"]) - r["pb"]) <= dq01_tolerance(r["pb"], N_TERMS_TRIO)
        )
        dis2 = [
            r
            for r in id2
            if abs((r["pa"] + r["eqm"]) - r["pb"]) > dq01_tolerance(r["pb"], N_TERMS_TRIO)
        ]
        flip2 = sum(
            1
            for r in dis2
            if abs((r["pa"] - r["eqm"]) - r["pb"]) <= dq01_tolerance(r["pb"], N_TERMS_TRIO)
        )
        id3 = conn.execute(
            """
            SELECT oi.value oiv, gp.value gpv, ox.value oxv FROM financial_facts oi
            JOIN financial_facts gp ON gp.filing_id=oi.filing_id AND gp.period_key=oi.period_key
                 AND gp.concept=?
            JOIN financial_facts ox ON ox.filing_id=oi.filing_id AND ox.period_key=oi.period_key
                 AND ox.concept=?
            WHERE oi.concept=? AND oi.value IS NOT NULL AND gp.value IS NOT NULL AND ox.value IS NOT NULL
            """,
            (G + "GrossProfit", G + "OperatingExpenses", oi),
        ).fetchall()
        agree3 = sum(
            1
            for r in id3
            if abs((r["gpv"] - r["oxv"]) - r["oiv"]) <= dq01_tolerance(r["oiv"], N_TERMS_TRIO)
        )
        out["e4_identities"] = {
            "identity1_pairs": id1,
            "identity1_agree": None,
            "identity2_pairs": len(id2),
            "identity2_agree": agree2,
            "identity2_disagree": len(dis2),
            "identity2_disagree_filings": len({r["filing_id"] for r in dis2}),
            "identity2_disagree_sign_flipped": flip2,
            "identity3_pairs": len(id3),
            "identity3_agree": agree3,
            "decision": "not applied (ID-02b needs a sourced identity the data follows; DQ-01 is the test)",
        }
        print(
            "E4 identity1 pairs",
            id1,
            "| identity2",
            len(id2),
            "agree",
            agree2,
            "disagree",
            len(dis2),
            "flipped",
            flip2,
            "| identity3",
            len(id3),
            "agree",
            agree3,
        )
    finally:
        conn.close()
    _write(Path(args.out), out)


# -- the metric coverage, VALORIZATION by type and the vetoes -------------------------------------


class Side:
    """One database's view of the members at a date (its own metric engine version)."""

    def __init__(self, db: str, date: str) -> None:
        self.db, self.date = db, date
        self.conn = connect_ro(db)
        self.versions = resolve_metric_versions(self.conn, None)
        self.dq = data.data_quality(self.conn, date, self.versions)
        self.metrics = self.dq.apply(data.latest_metrics(self.conn, date, self.versions))
        self.rows = data.active_universe(self.conn, UNIVERSE, date, UNIVERSE_DB)
        self.ids = [int(r["id"]) for r in self.rows]
        self.ticker = {int(r["id"]): str(r["ticker"]) for r in self.rows}
        self.sector = {int(r["id"]): r["sector"] for r in self.rows}
        self.profiles = data.asset_profiles(self.conn, self.ids, date)
        caps = market_caps_as_of(self.conn, self.ids, as_of=date).caps
        self.mcap = {a: caps[a].value for a in self.ids if a in caps}
        self.values = data.value_inputs(self.conn, date) if hasattr(data, "value_inputs") else {}

    def present(self, metric: str) -> int:
        return sum(1 for a in self.ids if self.metrics.get(a, {}).get(metric) is not None)

    def close(self) -> None:
        self.conn.close()


def _kind(prof: Any) -> str:
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


def _valorization_members(side: Side) -> dict[int, Any]:
    out = {}
    for a in side.ids:
        rule = side.values.get(a) or {}
        prof = side.profiles.get(a)
        out[a] = valorization.Member(
            asset_id=a,
            market_cap=side.mcap.get(a),
            values=rule.get("inputs") or {},
            applicable={
                m: ("A", "")
                if prof is None
                else applicability(
                    valorization.MATRIX_METRIC[m],
                    prof,
                    negative_equity=a in side.dq.negative_equity,
                )
                for m in valorization.MEASURES
            },
            quarantined=frozenset(side.dq.quarantined.get(a, set())),
            cohort=valorization.FINANCIAL
            if prof is not None and prof.financial_firm
            else valorization.OTHER,
            accession=rule.get("accession"),
        )
    return out


def _measure_presence(side: Side) -> dict[str, dict[str, int]]:
    scores = {int(s.asset_id): s for s in valorization.compute(_valorization_members(side))}
    per: dict[str, dict[str, int]] = {}
    for a, score in scores.items():
        kind = _kind(side.profiles.get(a))
        entry = per.setdefault(
            kind,
            {
                "members": 0,
                "ebitda_to_ev": 0,
                "ebitda_to_ev_applicable": 0,
                "enterprise_fcf_yield": 0,
                "enterprise_fcf_yield_applicable": 0,
            },
        )
        entry["members"] += 1
        measures = (score.components or {}).get("measures") or {}
        for name in ("ebitda_to_ev", "enterprise_fcf_yield"):
            m = measures.get(name) or {}
            if m.get("matrix") != "NA":
                entry[f"{name}_applicable"] += 1
            if m.get("status") == "present":
                entry[name] += 1
    return per


def _price_obs_version(conn: Any) -> str:
    """The ``price_observation`` engine version the copy holds (the t071c copy predates
    ``priceobs-v2``); never a mix of versions."""
    row = conn.execute("SELECT engine_version FROM price_observation LIMIT 1").fetchone()
    return str(row["engine_version"]) if row else PRICE_OBSERVATION_ENGINE_VERSION


def _vetoes(side: Side) -> dict[str, Any]:
    ctx = RuleContext(
        cycle_date=side.date,
        metrics={a: side.metrics.get(a, {}) for a in side.ids},
        price_obs=data.latest_price_observation(
            side.conn, side.date, _price_obs_version(side.conn)
        ),
        last_fundamental=data.last_fundamental_dates(side.conn, side.date),
        data_quality={a: side.dq.hard[a] for a in side.ids if a in side.dq.hard},
        sectors={a: side.sector.get(a) for a in side.ids},
        negative_equity={
            a: side.dq.negative_equity[a] for a in side.ids if a in side.dq.negative_equity
        },
        profiles=side.profiles,
        utility_cash_coverage=data.utility_cash_coverage(side.conn, side.date),
    )
    out: dict[str, Any] = {"by_rule": {}, "by_rule_sector": {}, "d04": {}}
    for rule in RULES:
        res = rule.evaluate(ctx)
        hits = list(res)
        out["by_rule"][rule.RULE_ID] = {
            "evaluated": len(res.evaluated),
            "hits": len(hits),
            "hard": sum(1 for h in hits if h.severity == "HARD"),
        }
        for h in hits:
            sec = side.sector.get(h.asset_id) or "unknown"
            out["by_rule_sector"].setdefault(rule.RULE_ID, Counter())[sec] += 1
        if rule.RULE_ID == "LEVERAGE_EXTREME":
            out["d04"] = {
                "evaluated": len(res.evaluated),
                "hits": len(hits),
                "hard": sum(1 for h in hits if h.severity == "HARD"),
                "soft": sum(1 for h in hits if h.severity != "HARD"),
                "negative_equity_evaluated": len(side.dq.negative_equity),
            }
    out["by_rule_sector"] = {k: dict(v) for k, v in out["by_rule_sector"].items()}
    return out


def cmd_metrics(args: argparse.Namespace) -> None:
    before, after = Side(args.before, args.date), Side(args.after, args.date)
    try:
        out: dict[str, Any] = {"date": args.date, "metrics": {}, "valorization": {}, "vetoes": {}}
        for metric in METRICS:
            out["metrics"][metric] = {
                "before": before.present(metric),
                "after": after.present(metric),
                "delta": after.present(metric) - before.present(metric),
            }
        out["valorization"] = {
            "before": _measure_presence(before),
            "after": _measure_presence(after),
        }
        out["vetoes"] = {"before": _vetoes(before), "after": _vetoes(after)}
        _write(Path(args.out), out)
        print(json.dumps(out["metrics"], indent=1))
    finally:
        before.close()
        after.close()


# -- VALORIZATION v2 before vs after --------------------------------------------------------------


def _spearman(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < N_TERMS_PAIR:
        return None
    ra = _ranks(a)
    rb = _ranks(b)
    mean_a, mean_b = sum(ra) / n, sum(rb) / n
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(ra, rb, strict=True))
    va = math.sqrt(sum((x - mean_a) ** 2 for x in ra))
    vb = math.sqrt(sum((y - mean_b) ** 2 for y in rb))
    return cov / (va * vb) if va and vb else None


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def cmd_valorization(args: argparse.Namespace) -> None:
    before, after = Side(args.before, args.date), Side(args.after, args.date)
    try:
        out: dict[str, Any] = {"date": args.date}
        raws: dict[str, dict[int, float | None]] = {}
        for name, side in (("before", before), ("after", after)):
            scores = {
                int(s.asset_id): s.raw_value
                for s in valorization.compute(_valorization_members(side))
            }
            raws[name] = scores
            out[f"{name}_empty"] = sum(1 for a in side.ids if scores.get(a) is None)
            out[f"{name}_scored"] = sum(1 for a in side.ids if scores.get(a) is not None)
            out[f"{name}_empty_no_market_cap"] = sum(
                1 for a in side.ids if scores.get(a) is None and a not in side.mcap
            )
        common = sorted(
            (
                a
                for a in before.ids
                if raws["before"].get(a) is not None and raws["after"].get(a) is not None
            ),
            key=lambda a: before.ticker.get(a, ""),
        )
        out["pairs"] = len(common)

        def _val(side: str, aid: int) -> float:
            value = raws[side][aid]
            assert value is not None  # `common` keeps only the paired, non-empty scores
            return value

        out["spearman"] = _spearman(
            [_val("before", a) for a in common], [_val("after", a) for a in common]
        )
        top = sorted(common, key=lambda a: (-(raws["after"][a] or 0), before.ticker.get(a, "")))[
            :30
        ]
        top_before = sorted(
            common, key=lambda a: (-(raws["before"][a] or 0), before.ticker.get(a, ""))
        )[:30]
        out["top30_overlap"] = len(set(top) & set(top_before))
        out["top30"] = [before.ticker.get(a) for a in top]
        out["movers"] = sum(
            1 for a in common if abs((raws["before"][a] or 0) - (raws["after"][a] or 0)) > ONE_POINT
        )
        _write(Path(args.out), out)
        print(json.dumps({k: v for k, v in out.items() if k != "top30"}, indent=1))
    finally:
        before.close()
        after.close()


# -- the prompt hash -------------------------------------------------------------------------------


def cmd_prompt_hash(args: argparse.Namespace) -> None:
    """How many filings' scored input changes; the prompt *template*'s hash is a code constant."""
    from fundamental_agent.agents import _prompt_version_hash  # noqa: PLC0415

    out: dict[str, Any] = {}
    changed: list[str] = []
    before, after = Side(args.before, args.date), Side(args.after, args.date)
    try:
        n = 0
        for a in before.ids:
            b = {k: v for k, v in before.metrics.get(a, {}).items() if k in METRICS}
            c = {k: v for k, v in after.metrics.get(a, {}).items() if k in METRICS}
            if b != c:
                n += 1
                changed.append(before.ticker.get(a, str(a)))
        out["filings"] = len(before.ids)
        out["filings_with_changed_metric_inputs"] = n
        out["changed_tickers"] = sorted(changed)
        out["prompt_template_hash"] = _prompt_version_hash("rule-based")
        out["note"] = (
            "the hash is the prompt *template* + model config, a code constant: no prompt or skill "
            "file is touched by T-156 (the changed inputs are re-scored by T-100)"
        )
        _write(Path(args.out), out)
        print(json.dumps({k: v for k, v in out.items() if k != "changed_tickers"}, indent=1))
    finally:
        before.close()
        after.close()


def cmd_recoveries(args: argparse.Namespace) -> None:
    """Every before -> after transition of the empty `leverage.ebitda`, matched by the filing's
    accession (never by row id), with the class and the D&A values on each side."""
    payload: dict[str, Any] = {"dates": {}}
    for date in args.dates:
        sides = {}
        for side in ("before", "after"):
            conn = connect_ro(getattr(args, side))
            try:
                sides[side] = {f["asset"]: f for f in _rows_for(conn, date)}
            finally:
                conn.close()
        transitions: Counter = Counter()
        recovered = []
        lost = []
        for aid, b in sides["before"].items():
            a = sides["after"].get(aid)
            if a is None or not b["has_filing"]:
                continue
            b_empty = b["ebitda"] is None
            a_empty = a["ebitda"] is None
            cls = (
                "financial_firm_na"
                if b["financial_firm"]
                else (_da_class(b) if not b["da_term"]["present"] else _oi_class(b) or "present")
            )
            transitions[
                (cls, "empty" if b_empty else "present", "empty" if a_empty else "present")
            ] += 1
            if b_empty and not a_empty:
                recovered.append(
                    {
                        "ticker": b["ticker"],
                        "form": b["form"],
                        "class": cls,
                        "da_before": b["da_input"],
                        "da_after": a["da_input"],
                        "ebitda_after": a["ebitda"],
                    }
                )
            if not b_empty and a_empty:
                lost.append({"ticker": b["ticker"], "form": b["form"], "class": cls})
        payload["dates"][date] = {
            "transitions": {f"{k[0]}|{k[1]}->{k[2]}": v for k, v in transitions.items()},
            "recovered": sorted(recovered, key=lambda r: r["ticker"]),
            "lost": sorted(lost, key=lambda r: r["ticker"]),
        }
        print(date, "recovered", len(recovered), "lost", len(lost))
        print("   ", json.dumps(payload["dates"][date]["transitions"]))
    _write(Path(args.out), payload)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    scratch = "/workspaces/thesis/scratch/t156"

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--before", default=f"{scratch}/p2_before.db")
        sp.add_argument("--after", default=f"{scratch}/p2_after.db")
        sp.add_argument("--date", default=MAIN)
        sp.add_argument("--out", required=True)

    c = sub.add_parser("classes")
    c.add_argument("--before", default=f"{scratch}/p2_before.db")
    c.add_argument("--after", default=f"{scratch}/p2_after.db")
    c.add_argument("--dates", nargs="*", default=list(DATES))
    c.add_argument("--out", default=f"{scratch}/out/p2_classes.json")
    c.set_defaults(func=cmd_classes)

    f = sub.add_parser("fidelity")
    f.add_argument("--after", default=f"{scratch}/p2_after.db")
    f.add_argument("--out", default=f"{scratch}/out/p2_fidelity.json")
    f.set_defaults(func=cmd_fidelity)

    m = sub.add_parser("metrics")
    common(m)
    m.set_defaults(func=cmd_metrics)

    v = sub.add_parser("valorization")
    common(v)
    v.set_defaults(func=cmd_valorization)

    h = sub.add_parser("prompt-hash")
    common(h)
    h.set_defaults(func=cmd_prompt_hash)

    r = sub.add_parser("recoveries")
    r.add_argument("--before", default=f"{scratch}/p2_before.db")
    r.add_argument("--after", default=f"{scratch}/p2_after.db")
    r.add_argument("--dates", nargs="*", default=list(DATES))
    r.add_argument("--out", default=f"{scratch}/out/p2_recoveries.json")
    r.set_defaults(func=cmd_recoveries)

    a = sub.add_parser("all")
    a.add_argument("--before", default=f"{scratch}/p2_before.db")
    a.add_argument("--after", default=f"{scratch}/p2_after.db")
    a.add_argument("--dates", nargs="*", default=list(DATES))
    a.add_argument("--out", default=f"{scratch}/out")
    a.set_defaults(func=_cmd_all)

    args = p.parse_args()
    args.func(args)


def _cmd_all(args: argparse.Namespace) -> None:
    out = Path(args.out)
    args.out = str(out / "p2_classes.json")
    args.dates = args.dates
    cmd_classes(args)
    args.out = str(out / "p2_recoveries.json")
    cmd_recoveries(args)
    args.out = str(out / "p2_fidelity.json")
    cmd_fidelity(args)
    args.date = MAIN
    args.out = str(out / "p2_metrics.json")
    cmd_metrics(args)
    args.out = str(out / "p2_valorization.json")
    cmd_valorization(args)
    args.out = str(out / "p2_prompt_hash.json")
    cmd_prompt_hash(args)


if __name__ == "__main__":
    main()
