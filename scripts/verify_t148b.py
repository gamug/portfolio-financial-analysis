"""T-148 part 2 verification and measurements (docs/model_fixes.md, T148b_plan.md).

Sub-commands:
    uv run python scripts/verify_t148b.py m1 --db DB --out out/m1.json
    uv run python scripts/verify_t148b.py m2 --db DB --out out/m2.json
    uv run python scripts/verify_t148b.py m3 --db DB --out out/m3.json
    uv run python scripts/verify_t148b.py m4 --db DB --out out/m4.json
    uv run python scripts/verify_t148b.py m5 --db DB --out out/m5.json
    uv run python scripts/verify_t148b.py m6 --db DB --out out/m6.json
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sec_xcheck.common import filings, open_ro, own_period_key, statements_from_db
from sec_xcheck.dqc_lists import dqc14, dqc15

import fundamental_agent
from fundamental_agent import db, quality, utility_coverage
from fundamental_agent.agents import FilingContext, FundamentalAnalyst, facts_only_synthesis
from fundamental_agent.db import SnapshotRow
from fundamental_agent.edgar_client import EdgarClient
from fundamental_agent.pipeline import _Target, _ttm_flows, _YearTask
from fundamental_agent.pricing import close_on_or_before
from fundamental_agent.statements import REGISTRY, CoverShares, Statements
from kg_schema import connect
from kg_schema.market_cap import market_caps_as_of

# Regexes for D-02 condition (step 2.3)
REV_CONCEPT_RE = re.compile(r"Revenue", re.IGNORECASE)
REV_CAPTION_RE = re.compile(r"\b(revenues?|sales)\b", re.IGNORECASE)
COST_EXPENSE_RE = re.compile(r"\b(costs?|expenses?)\b", re.IGNORECASE)

# Regexes for Cash caption check (step 2.2 / M5)
PAREN_RE = re.compile(r"\([^)]*\)")
NOTE_RE = re.compile(r"\(note\s+\w+\)", re.IGNORECASE)
ALLOWED_K1_CAPTIONS = frozenset(
    {
        "cash",
        "cash and cash equivalents",
        "cash and equivalents",
        "cash, cash equivalents",
    }
)
INVESTMENT_KEYWORDS = ("invest", "marketable", "securit", "short-term", "short term")

MIN_STREAMS_FOR_SUM = 2
TOLERANCE_RATIO = 0.005
MIN_ABS_DIFF = 0.5


def check_d02_revenue(
    stmts: Any, col: str, spec: Any
) -> tuple[bool, float | None, list[str], list[dict[str, Any]]]:
    """Test D-02 condition: sum of >= 2 streams returned only when no other valued,
    non-abstract, non-dimensional income-statement row reads as revenue."""
    sum_val, used = stmts._sum_components(spec, col)
    blocking: list[dict[str, Any]] = []
    if sum_val is None or len(used) < MIN_STREAMS_FOR_SUM:
        return False, sum_val, used, blocking

    for r in stmts._rows_for(spec):
        c_name = str(r.get("concept") or "")
        c_local = c_name.rsplit("_", maxsplit=1)[-1]
        caption = str(r.get("label") or "")
        val = stmts._read(r, col, spec)
        if val is None or r.get("abstract") or r.get("dimension") or c_name in spec.concepts:
            continue
        # Row whose value equals the sum (within 0.5%) is the statement's own total and does not block
        if abs(val - sum_val) <= max(MIN_ABS_DIFF, TOLERANCE_RATIO * abs(sum_val)):
            continue
        reads_as_rev = (
            REV_CONCEPT_RE.search(c_local) or REV_CAPTION_RE.search(caption)
        ) and not COST_EXPENSE_RE.search(caption)
        if reads_as_rev:
            blocking.append({"concept": c_name, "caption": caption, "value": val})

    taken = len(blocking) == 0
    return taken, sum_val, used, blocking


def _distinct_streams_count(vals: dict[str, float], spec: Any) -> int:
    distinct_streams: set[tuple[str, ...] | str] = set()
    for c in vals:
        group = next((g for g in spec.synonym_groups if c in g), (c,))
        distinct_streams.add(group)
    return len(distinct_streams)


def cmd_m1(args: argparse.Namespace) -> None:
    conn = open_ro(args.db)
    spec = REGISTRY["revenue"]
    all_filings = filings(conn)

    results: list[dict[str, Any]] = []
    for f in all_filings:
        fid = f["id"]
        stmts = statements_from_db(conn, fid)
        for p in stmts.periods:
            col = str(p.key)
            total_val, _ = stmts._first_total_row(spec, col)
            if total_val is not None:
                continue
            vals = stmts._matching_component_values(spec, col)
            if _distinct_streams_count(vals, spec) < MIN_STREAMS_FOR_SUM:
                continue

            taken, sum_val, used, blocking = check_d02_revenue(stmts, col, spec)
            results.append(
                {
                    "filing_id": fid,
                    "ticker": f["ticker"],
                    "accession": f["accession_number"],
                    "form": f["form"],
                    "column": col,
                    "sum": sum_val,
                    "used": used,
                    "result": "taken" if taken else "refused",
                    "blocking": blocking,
                }
            )

    print(
        f"{'Ticker':6} {'Form':6} {'Accession':20} {'Column':18} {'Sum':>14} {'Result':>8} {'Blocking':>8}"
    )
    print("-" * 86)
    for r in results:
        b_str = str(len(r["blocking"])) if r["blocking"] else "None"
        s_str = f"{r['sum']:,.0f}" if r["sum"] is not None else "None"
        print(
            f"{r['ticker']:6} {r['form']:6} {r['accession']:20} {r['column']:18} {s_str:>14} {r['result']:>8} {b_str:>8}"
        )

    taken_tickers = sorted({r["ticker"] for r in results if r["result"] == "taken"})
    refused_tickers = sorted({r["ticker"] for r in results if r["result"] == "refused"})
    print("-" * 86)
    print(f"Total columns evaluated: {len(results)}")
    print(f"Taken: {sum(1 for r in results if r['result'] == 'taken')} (tickers: {taken_tickers})")
    print(
        f"Refused: {sum(1 for r in results if r['result'] == 'refused')} (tickers: {refused_tickers})"
    )

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(results, indent=2))


def _check_bank_identity(
    f: Any, col: str, sum_val: float, total: float, concept_name: str
) -> tuple[bool, dict[str, Any] | None]:
    is_match = abs(sum_val - total) <= max(MIN_ABS_DIFF, TOLERANCE_RATIO * abs(total))
    if is_match:
        return True, None
    failure = {
        "ticker": f["ticker"],
        "accession": f["accession_number"],
        "form": f["form"],
        "column": col,
        "concept": concept_name,
        "sum": sum_val,
        "total": total,
    }
    return False, failure


def _eval_bank_filing(
    stmts: Any, f: Any, col: str, spec: Any
) -> tuple[dict[str, tuple[int, int]], list[dict[str, Any]]]:
    counts: dict[str, tuple[int, int]] = {}
    failures: list[dict[str, Any]] = []
    nii = stmts._concept_value(spec, col, "us-gaap_InterestIncomeExpenseNet")
    nonii = stmts._concept_value(spec, col, "us-gaap_NoninterestIncome")
    if nii is None or nonii is None:
        return counts, failures

    tk = f["ticker"]
    sum_val = nii + nonii
    net_tot = stmts._concept_value(spec, col, "us-gaap_RevenuesNetOfInterestExpense")
    rev_tot = stmts._concept_value(spec, col, "us-gaap_Revenues")
    if net_tot is not None:
        ok, fail = _check_bank_identity(f, col, sum_val, net_tot, "RevenuesNetOfInterestExpense")
        counts["net_tot"] = (1 if ok else 0, 1)
        if fail:
            failures.append(fail)
    if rev_tot is not None:
        key = "stt" if tk == "STT" else "rev_tot"
        ok, fail = _check_bank_identity(f, col, sum_val, rev_tot, "Revenues")
        counts[key] = (1 if ok else 0, 1)
        if fail:
            failures.append(fail)
    return counts, failures


def cmd_m2(args: argparse.Namespace) -> None:
    conn = open_ro(args.db)
    all_filings = filings(conn)
    spec = REGISTRY["revenue"]

    nii_nonii_cols: dict[str, int] = defaultdict(int)
    nii_nonii_filings: dict[str, set[str]] = defaultdict(set)
    tots = {"net_tot": [0, 0], "rev_tot": [0, 0], "stt": [0, 0]}
    failing_filings: list[dict[str, Any]] = []

    for f in all_filings:
        fid = f["id"]
        tk = f["ticker"]
        stmts = statements_from_db(conn, fid)
        for p in stmts.periods:
            col = str(p.key)
            if stmts.get("revenue", col) is None:
                has_nii = (
                    stmts._concept_value(spec, col, "us-gaap_InterestIncomeExpenseNet") is not None
                )
                has_nonii = stmts._concept_value(spec, col, "us-gaap_NoninterestIncome") is not None
                if has_nii and has_nonii:
                    nii_nonii_cols[tk] += 1
                    nii_nonii_filings[tk].add(f["accession_number"])

            is_own = (f["form"].startswith("10-K") and p.is_fy) or (
                f["form"].startswith("10-Q") and p.is_quarter
            )
            if is_own:
                f_counts, f_fails = _eval_bank_filing(stmts, f, col, spec)
                for k, (m, t) in f_counts.items():
                    tots[k][0] += m
                    tots[k][1] += t
                failing_filings.extend(f_fails)

    print(f"{'Ticker':6} {'Columns':>10} {'Filings':>10}")
    print("-" * 30)
    for tk in sorted(nii_nonii_cols.keys()):
        print(f"{tk:6} {nii_nonii_cols[tk]:>10} {len(nii_nonii_filings[tk]):>10}")
    print("-" * 30)
    print(f"Total companies with NII+NonII and no revenue: {len(nii_nonii_cols)}")
    print(f"RevenuesNetOfInterestExpense 0.5% matches: {tots['net_tot'][0]}/{tots['net_tot'][1]}")
    print(f"Revenues (non-STT) 0.5% matches: {tots['rev_tot'][0]}/{tots['rev_tot'][1]}")
    print(f"STT Revenues 0.5% matches: {tots['stt'][0]}/{tots['stt'][1]}")
    print(f"Failing filings count: {len(failing_filings)}")

    payload = {
        "nii_nonii_unresolved": {
            tk: {"columns": nii_nonii_cols[tk], "filings": len(nii_nonii_filings[tk])}
            for tk in sorted(nii_nonii_cols.keys())
        },
        "identity_checks": {
            "RevenuesNetOfInterestExpense": {
                "matches": tots["net_tot"][0],
                "total": tots["net_tot"][1],
            },
            "Revenues_non_STT": {"matches": tots["rev_tot"][0], "total": tots["rev_tot"][1]},
            "STT": {"matches": tots["stt"][0], "total": tots["stt"][1]},
        },
        "failing_filings": failing_filings,
    }
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, indent=2))


def _load_multi_class_listed() -> dict[str, bool]:
    multi_class_listed = {}
    csv_path = Path("docs/checklist_sec/company_types.csv")
    if csv_path.exists():
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                cik10 = row["cik"].zfill(10)
                multi_class_listed[cik10] = (
                    row.get("multi_class_listed", "").strip().lower() == "true"
                )
    return multi_class_listed


def cmd_m3(args: argparse.Namespace) -> None:
    conn = open_ro(args.db)
    multi_class_listed = _load_multi_class_listed()

    assets_by_cik: dict[str, list[str]] = defaultdict(list)
    for r in conn.execute("SELECT cik, ticker FROM assets").fetchall():
        cik10 = str(r[0]).zfill(10)
        assets_by_cik[cik10].append(r[1])

    q = """
    WITH latest AS (
        SELECT f.id, a.cik, f.period_end, f.accession_number, f.form,
               ROW_NUMBER() OVER (PARTITION BY a.cik ORDER BY f.period_end DESC, f.id DESC) as rn
        FROM sec_filings f
        JOIN assets a ON a.id = f.asset_id
    )
    SELECT id, cik, period_end, accession_number, form FROM latest WHERE rn = 1
    """
    latest_filings = conn.execute(q).fetchall()

    rows: list[dict[str, Any]] = []
    for fid, cik, pe, accn, form in latest_filings:
        cik10 = str(cik).zfill(10)
        cs_rows = conn.execute(
            "SELECT class_member, value FROM filing_cover_shares WHERE filing_id = ?", (fid,)
        ).fetchall()
        members = [r[0] for r in cs_rows]
        has_total = "" in members
        distinct_members = [m for m in members if m]
        if len(distinct_members) > 1 or not has_total:
            tks = sorted(assets_by_cik.get(cik10, []))
            mc_listed = multi_class_listed.get(cik10, False)
            if len(distinct_members) <= 1:
                cls = "a"
            elif set(tks) in ({"FOX", "FOXA"}, {"NWS", "NWSA"}):
                cls = "b"
            else:
                cls = "c"
            rows.append(
                {
                    "cik": cik10,
                    "tickers": tks,
                    "latest_form": form,
                    "latest_period_end": pe,
                    "latest_accession": accn,
                    "has_total": has_total,
                    "members": distinct_members,
                    "multi_class_listed": mc_listed,
                    "classification": cls,
                }
            )

    rows.sort(key=lambda x: (x["classification"], x["tickers"]))
    print(f"{'CIK':10} {'Tickers':12} {'Cls':3} {'Total':5} {'MC Listed':9} {'Members':40}")
    print("-" * 86)
    for r in rows:
        m_str = ", ".join(m.split(":")[-1] for m in r["members"])
        t_str = ", ".join(r["tickers"])
        print(
            f"{r['cik']:10} {t_str:12} {r['classification']:3} {r['has_total']!s:5} {r['multi_class_listed']!s:9} {m_str[:40]:40}"
        )
    print("-" * 86)
    c_a = sum(1 for r in rows if r["classification"] == "a")
    c_b = sum(1 for r in rows if r["classification"] == "b")
    c_c = sum(1 for r in rows if r["classification"] == "c")
    print(f"Total CIKs reviewed: {len(rows)} (a: {c_a}, b: {c_b}, c: {c_c})")

    conversions = {
        "0001067983": {
            "ticker": "BRK.B",
            "accession": "0001193125-26-083899",
            "sentence": "Each share of Class A common stock is convertible at the option of the holder into 1,500 shares of Class B common stock.",
            "ratio": 1500.0,
        },
        "0000922621": {
            "ticker": "ERIE",
            "accession": "0001628280-26-010666",
            "sentence": "Class B shares are convertible into Class A shares at a conversion ratio of 2,400 to 1.",
            "ratio": 2400.0,
        },
        "0001512673": {
            "ticker": "XYZ",
            "accession": "0001628280-26-012254",
            "sentence": "Shares of the Company's Class B common stock are convertible into an equivalent number of shares of its Class A common stock and generally convert into shares of its Class A common stock upon transfer.",
            "ratio": 1.0,
        },
        "0001679788": {
            "ticker": "COIN",
            "accession": "0001679788-26-000015",
            "sentence": "In addition, each share of Class B common stock will automatically convert into a share of Class A common stock upon a sale or transfer.",
            "ratio": 1.0,
        },
        "0001783879": {
            "ticker": "HOOD",
            "accession": "0001783879-26-000023",
            "sentence": "Shares of Class B common stock will also automatically convert into shares of Class A common stock upon sale or transfer except for certain permitted transfers described in our Charter.",
            "ratio": 1.0,
        },
    }

    out_data = {
        "ciks": rows,
        "classification_counts": {"a": c_a, "b": c_b, "c": c_c},
        "known_conversions": conversions,
    }
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out_data, indent=2))


def cmd_m4(args: argparse.Namespace) -> None:
    conn = open_ro(args.db)

    q = """
    SELECT a.ticker, a.sub_industry, s.name as sector
    FROM assets a
    JOIN sectors s ON s.id = a.sector_id
    WHERE s.name = "Real Estate" AND a.sub_industry LIKE "%REITs"
    ORDER BY a.ticker
    """
    runtime_reits = [r[0] for r in conn.execute(q).fetchall()]

    csv_reits: list[str] = []
    csv_path = Path("docs/checklist_sec/company_types.csv")
    if csv_path.exists():
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("reit", "").strip().lower() == "true":
                    for raw_ticker in row.get("tickers", "").split(","):
                        cleaned_ticker = raw_ticker.strip()
                        if cleaned_ticker:
                            csv_reits.append(cleaned_ticker)
    csv_reits.sort()

    diff_rt_only = sorted(set(runtime_reits) - set(csv_reits))
    diff_csv_only = sorted(set(csv_reits) - set(runtime_reits))

    print(f"Runtime REITs count: {len(runtime_reits)}")
    print(f"CSV REITs count:     {len(csv_reits)}")
    print(f"Runtime only:        {diff_rt_only}")
    print(f"CSV only:            {diff_csv_only}")
    print("Runtime REIT list:  ", runtime_reits)

    out_data = {
        "runtime_reits": runtime_reits,
        "csv_reits": csv_reits,
        "runtime_only": diff_rt_only,
        "csv_only": diff_csv_only,
    }
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out_data, indent=2))


def _has_other_bs_restriction(bs_rows: list[dict[str, Any]], inst_col: str) -> bool:
    for r in bs_rows:
        if r.get("abstract") or r.get("dimension"):
            continue
        c_name = str(r.get("concept") or "")
        c_cap = str(r.get("label") or "")
        c_local = c_name.rsplit("_", maxsplit=1)[-1]
        if c_name == "us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents":
            continue
        if (
            ("restrict" in c_local.lower() or "restrict" in c_cap.lower())
            and inst_col in r
            and r[inst_col] is not None
        ):
            return True
    return False


def _eval_k1(bs_rows: list[dict[str, Any]], inst_col: str) -> bool:
    row_k1 = next(
        (
            r
            for r in bs_rows
            if r.get("concept")
            == "us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"
            and inst_col in r
            and r[inst_col] is not None
        ),
        None,
    )
    if not row_k1:
        return False
    cap = str(row_k1.get("label") or "")
    cap_clean = NOTE_RE.sub("", cap)
    cap_clean = PAREN_RE.sub("", cap_clean).strip().lower()
    if cap_clean in ALLOWED_K1_CAPTIONS and "restrict" not in cap_clean:
        return not _has_other_bs_restriction(bs_rows, inst_col)
    return False


def _eval_cash_period(
    stmts: Any,
    col: str,
    spec: Any,
    bs_rows: list[dict[str, Any]],
) -> dict[str, bool]:
    out = {
        "k1": False,
        "k1_rej": False,
        "k2_cash": False,
        "k2_invest": False,
        "k3": False,
        "es": False,
    }
    inst_col = stmts._instant_for(col)
    if not inst_col:
        return out

    val_std = stmts._concept_value(spec, inst_col, "us-gaap_CashAndCashEquivalentsAtCarryingValue")
    val_due = stmts._concept_value(spec, inst_col, "us-gaap_CashAndDueFromBanks")
    val_cash = stmts._concept_value(spec, inst_col, "us-gaap_Cash")
    val_ce = stmts._concept_value(spec, inst_col, "us-gaap_CashEquivalentsAtCarryingValue")
    val_k2 = stmts._concept_value(
        spec, inst_col, "us-gaap_CashCashEquivalentsAndShortTermInvestments"
    )
    val_k1 = stmts._concept_value(
        spec, inst_col, "us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"
    )

    if val_std is None and val_cash is not None and val_ce is not None:
        out["es"] = True

    if val_k2 is not None and val_std is None:
        row_k2 = next(
            (
                r
                for r in bs_rows
                if r.get("concept") == "us-gaap_CashCashEquivalentsAndShortTermInvestments"
                and inst_col in r
                and r[inst_col] is not None
            ),
            None,
        )
        cap = (row_k2.get("label") or "").lower() if row_k2 else ""
        if any(w in cap for w in INVESTMENT_KEYWORDS):
            out["k2_invest"] = True
        else:
            out["k2_cash"] = True

    if val_k1 is not None and val_std is None and val_k2 is None and val_due is None:
        if _eval_k1(bs_rows, inst_col):
            out["k1"] = True
        else:
            out["k1_rej"] = True

    if (
        val_std is None
        and val_k2 is None
        and val_due is None
        and not (val_cash is not None and val_ce is not None)
        and val_k1 is None
        and val_cash is not None
    ):
        ca = stmts.get("current_assets", col)
        cl = stmts.get("current_liabilities", col)
        if ca is not None and cl is not None:
            out["k3"] = True
    return out


def _print_m5_report(
    cols: dict[str, int],
    fls: dict[str, int],
    k1_cos: set[str],
    k3_cos: set[str],
    es_cos: set[str],
) -> None:
    print(f"{'Reading':25} {'Columns':>10} {'Own-period Filings':>20}")
    print("-" * 58)
    print(f"{'(k)(1) caption test':25} {cols['k1']:>10} {fls['k1']:>20}")
    print(f"{'(k)(1) rejected':25} {cols['k1_rej']:>10} {fls['k1_rej']:>20}")
    print(f"{'(k)(2) cash caption':25} {cols['k2_cash']:>10} {fls['k2_cash']:>20}")
    print(f"{'(k)(2) invest caption':25} {cols['k2_invest']:>10} {fls['k2_invest']:>20}")
    print(f"{'(k)(3) Cash alone classified':25} {cols['k3']:>10} {fls['k3']:>20}")
    print(f"{'ES map':25} {cols['es']:>10} {fls['es']:>20}")
    print("-" * 58)
    print(f"k1 companies ({len(k1_cos)}): {sorted(k1_cos)[:10]}...")
    print(f"k3 companies ({len(k3_cos)}): {sorted(k3_cos)}")
    print(f"ES companies ({len(es_cos)}): {sorted(es_cos)}")


def _process_one_m5_filing(
    stmts: Any,
    own_col: str | None,
    spec: Any,
    col_counts: dict[str, int],
    filing_counts: dict[str, int],
) -> dict[str, bool]:
    bs_rows = stmts.raw.get("balance_sheet", [])
    own_hits: dict[str, bool] = defaultdict(bool)
    for p in stmts.periods:
        col = str(p.key)
        is_own = col == own_col
        verdicts = _eval_cash_period(stmts, col, spec, bs_rows)
        for k, hit in verdicts.items():
            if hit:
                col_counts[k] += 1
                if is_own:
                    filing_counts[k] += 1
                    own_hits[k] = True
    return own_hits


def cmd_m5(args: argparse.Namespace) -> None:
    conn = open_ro(args.db)
    all_filings = filings(conn)
    spec = REGISTRY["cash"]

    col_counts: dict[str, int] = defaultdict(int)
    filing_counts: dict[str, int] = defaultdict(int)
    k1_cos: set[str] = set()
    k3_cos: set[str] = set()
    es_cos: set[str] = set()

    for f in all_filings:
        fid = f["id"]
        tk = f["ticker"]
        stmts = statements_from_db(conn, fid)
        own_col = own_period_key(stmts, f["form"], f["period_end"])
        own_hits = _process_one_m5_filing(stmts, own_col, spec, col_counts, filing_counts)
        if own_hits.get("k1"):
            k1_cos.add(tk)
        if own_hits.get("k3"):
            k3_cos.add(tk)
        if own_hits.get("es"):
            es_cos.add(tk)

    _print_m5_report(col_counts, filing_counts, k1_cos, k3_cos, es_cos)

    out_data = {
        "columns": col_counts,
        "filings": filing_counts,
        "companies": {"k1": sorted(k1_cos), "k3": sorted(k3_cos), "es": sorted(es_cos)},
    }
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out_data, indent=2))


def _print_m6_report(
    counts: dict[str, int], no_cover_first: int, pipeline_quote: str, db_quote: str
) -> None:
    print(f"{'Category':20} {'Count':>10}")
    print("-" * 32)
    for k, v in counts.items():
        print(f"{k:20} {v:>10}")
    print("-" * 32)
    print(f"no_cover_shares first filings: {no_cover_first}/{counts['no_cover_shares']}")
    print("\nPipeline ordering check (_plan):")
    print(pipeline_quote)
    print("\nDatabase available_at on insert (db.py):")
    print(db_quote)


def cmd_m6(args: argparse.Namespace) -> None:
    conn = open_ro(args.db)
    all_filings = filings(conn)

    first_filing_ids: set[int] = set()
    by_asset: dict[int, list[Any]] = defaultdict(list)
    for f in all_filings:
        by_asset[f["asset_id"]].append(f)
    for _aid, flist in by_asset.items():
        flist_sorted = sorted(flist, key=lambda x: (x["period_end"], x["id"]))
        first_filing_ids.add(flist_sorted[0]["id"])

    counts: dict[str, int] = {
        "ok": 0,
        "stale_shares": 0,
        "no_cover_shares": 0,
        "no_recent_price": 0,
        "other": 0,
    }
    no_cover_first = 0

    for f in all_filings:
        aid = f["asset_id"]
        pe = f["period_end"]
        res = market_caps_as_of(conn, [aid], as_of=pe)
        if aid in res.caps:
            counts["ok"] += 1
        else:
            reason = res.missing.get(aid, "other")
            counts[reason if reason in counts else "other"] += 1
            if reason == "no_cover_shares" and f["id"] in first_filing_ids:
                no_cover_first += 1

    pipeline_path = Path("src/fundamental_agent/pipeline.py")
    pipeline_quote = (
        "\n".join(line.strip() for line in pipeline_path.read_text().splitlines()[214:230])
        if pipeline_path.exists()
        else ""
    )
    db_path = Path("src/fundamental_agent/db.py")
    db_quote = (
        "\n".join(line.strip() for line in db_path.read_text().splitlines()[428:442])
        if db_path.exists()
        else ""
    )

    _print_m6_report(counts, no_cover_first, pipeline_quote, db_quote)

    out_data = {
        "counts": counts,
        "no_cover_shares_first_filings": no_cover_first,
        "pipeline_order_quote": pipeline_quote,
        "db_insert_quote": db_quote,
    }
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out_data, indent=2))


def _evaluate_id16_for_company(
    r: dict[str, Any],
    edgar: EdgarClient,
    payloads_dir: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    ticker = r["ticker"]
    cik = r["cik"]
    form = r["form"]
    fdate = r["filing_date"]
    period_end = r["period_end"]
    acc = r["accession_number"]
    payload_path = payloads_dir / f"{cik}_{acc}.json"

    try:
        if payload_path.exists():
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        else:
            year = int(fdate[:4])
            payload = edgar.financials(cik, form, year, accession_number=acc)
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

        stmts = Statements.from_payload(payload)
        target_key = next(
            (p.key for p in stmts.periods if p.date == period_end), f"{period_end} (FY)"
        )

        flags_found: dict[str, Any] = {}
        for item in ("revenue", "net_income", "equity"):
            res = stmts.resolve(item, target_key)
            if "equals_dimensional_member" in res.flags:
                flags_found[item] = {
                    "ticker": ticker,
                    "cik": cik,
                    "accession_number": acc,
                    "period_end": period_end,
                    "value": res.value,
                    "concept": res.concept,
                }
    except Exception as e:
        return None, str(e)
    else:
        return flags_found, None


def cmd_id16(args: argparse.Namespace) -> None:
    db_path = args.db
    out_path = args.out
    payloads_dir = Path(args.payloads_dir)
    payloads_dir.mkdir(parents=True, exist_ok=True)

    conn = open_ro(db_path)
    query = """
        SELECT f.id, f.asset_id, a.ticker, a.cik, f.form, f.fiscal_year, f.fiscal_period, f.period_end, f.filing_date, f.accession_number
        FROM (
            SELECT f.*, ROW_NUMBER() OVER (PARTITION BY f.asset_id ORDER BY f.period_end DESC, f.id DESC) as rn
            FROM sec_filings f
            WHERE f.form LIKE '10-K%'
        ) f
        JOIN assets a ON a.id = f.asset_id
        WHERE f.rn = 1
        ORDER BY a.ticker
    """
    rows = [dict(r) for r in conn.execute(query).fetchall()]
    if args.limit:
        rows = rows[: args.limit]

    print(f"Total companies with latest 10-K: {len(rows)}", flush=True)

    gateway_url = os.environ.get("EDGAR_BASE_URL", "http://host.docker.internal:8000/edgar/edgar")
    flagged: dict[str, list[dict[str, Any]]] = {
        "revenue": [],
        "net_income": [],
        "equity": [],
    }
    errors: list[dict[str, Any]] = []

    with EdgarClient(gateway_url) as edgar:
        for idx, r in enumerate(rows):
            flags_found, err = _evaluate_id16_for_company(r, edgar, payloads_dir)
            if err is not None:
                print(f"[{idx + 1}/{len(rows)}] {r['ticker']:5} ERROR: {err}", flush=True)
                errors.append({**r, "error": err})
                continue

            assert flags_found is not None
            for item, item_data in flags_found.items():
                flagged[item].append(item_data)

            rev_s = "1" if "revenue" in flags_found else "0"
            ni_s = "1" if "net_income" in flags_found else "0"
            eq_s = "1" if "equity" in flags_found else "0"
            if (idx + 1) % 10 == 0 or idx == len(rows) - 1 or flags_found:
                print(
                    f"[{idx + 1}/{len(rows)}] {r['ticker']:5} (flags: rev={rev_s}, ni={ni_s}, eq={eq_s})",
                    flush=True,
                )

    counts = {item: len(flagged[item]) for item in flagged}
    print("\n--- Summary of ID-16 (equals_dimensional_member) ---", flush=True)
    print(f"Total companies examined: {len(rows)}", flush=True)
    print(f"Errors: {len(errors)}", flush=True)
    for item, count in counts.items():
        print(f"  {item:12}: {count} companies", flush=True)

    if out_path:
        out_data = {
            "total_examined": len(rows),
            "counts": counts,
            "flagged": flagged,
            "errors": errors,
        }
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(out_data, indent=2), encoding="utf-8")
        print(f"Wrote results to {out_path}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="T-148 part 2 verification measurements (M1-M6, ID16)"
    )
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    p_m1 = subparsers.add_parser("m1", help="M1: Revenue sum under D-02")
    p_m1.add_argument("--db", default="/workspaces/thesis/scratch/t148b/rebuilt_full3.db")
    p_m1.add_argument("--out")
    p_m1.set_defaults(func=cmd_m1)

    p_m2 = subparsers.add_parser("m2", help="M2: Bank map (D2)")
    p_m2.add_argument("--db", default="/workspaces/thesis/scratch/t148b/rebuilt_full3.db")
    p_m2.add_argument("--out")
    p_m2.set_defaults(func=cmd_m2)

    p_m3 = subparsers.add_parser("m3", help="M3: Share classes (MKT-03)")
    p_m3.add_argument("--db", default="/workspaces/thesis/scratch/t148b/rebuilt_full3.db")
    p_m3.add_argument("--out")
    p_m3.set_defaults(func=cmd_m3)

    p_m4 = subparsers.add_parser("m4", help="M4: REIT set (D6)")
    p_m4.add_argument("--db", default="/workspaces/thesis/scratch/t148b/rebuilt_full3.db")
    p_m4.add_argument("--out")
    p_m4.set_defaults(func=cmd_m4)

    p_m5 = subparsers.add_parser("m5", help="M5: Cash readings")
    p_m5.add_argument("--db", default="/workspaces/thesis/scratch/t148b/rebuilt_full3.db")
    p_m5.add_argument("--out")
    p_m5.set_defaults(func=cmd_m5)

    p_m6 = subparsers.add_parser("m6", help="M6: MKT-01 strict")
    p_m6.add_argument("--db", default="/workspaces/thesis/scratch/t148b/work_full.db")
    p_m6.add_argument("--out")
    p_m6.set_defaults(func=cmd_m6)

    p_id16 = subparsers.add_parser("id16", help="ID-16: Equals dimensional member flag prevalence")
    p_id16.add_argument("--db", default="/workspaces/thesis/scratch/t148b/rebuilt_full3.db")
    p_id16.add_argument("--out", default="/workspaces/thesis/scratch/t148b/out/id16.json")
    p_id16.add_argument("--payloads-dir", default="/workspaces/thesis/scratch/t148b/payloads")
    p_id16.add_argument("--limit", type=int, default=None)
    p_id16.set_defaults(func=cmd_id16)

    p_recompute = subparsers.add_parser(
        "recompute", help="Recompute all fundamental metrics from stored facts"
    )
    p_recompute.add_argument("--db", required=True)
    p_recompute.add_argument("--limit", type=int, default=None)
    p_recompute.set_defaults(func=cmd_recompute)

    p_dqc = subparsers.add_parser(
        "dqc", help="Verify REGISTRY nonnegative concepts against authoritative DQC lists"
    )
    p_dqc.add_argument("--out", default=None)
    p_dqc.set_defaults(func=cmd_dqc)

    args = parser.parse_args()
    args.func(args)


def cmd_dqc(args: argparse.Namespace) -> None:
    dqc_dir = Path("docs/checklist_sec/sec documentation/B_DQC_element_lists")
    if not dqc_dir.exists():
        print(f"Error: {dqc_dir} does not exist", file=sys.stderr)
        sys.exit(1)

    d15 = dqc15(dqc_dir)
    all_d15: set[str] = set()
    for s in d15.values():
        all_d15 |= s
    d14 = dqc14(dqc_dir)

    results: dict[str, Any] = {}
    mismatches: list[str] = []
    for item in REGISTRY.values():
        item_res: dict[str, Any] = {"concepts": {}, "nonnegative": sorted(item.nonnegative)}
        for c in item.concepts:
            local = c.split("_", 1)[1] if "_" in c else c
            is_dqc = (local in all_d15) or (item.name == "revenue" and local in d14)
            is_nonneg = c in item.nonnegative
            item_res["concepts"][c] = {"is_dqc": is_dqc, "is_nonnegative": is_nonneg}
            if is_dqc != is_nonneg:
                mismatches.append(f"{item.name}.{c}: DQC={is_dqc} vs REGISTRY={is_nonneg}")
        results[item.name] = item_res

    out_dir = Path("/workspaces/thesis/scratch/t148b/out")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json = Path(args.out) if args.out else out_dir / "dqc.json"
    out_txt = out_dir / "dqc.txt"

    out_json.write_text(json.dumps({"results": results, "mismatches": mismatches}, indent=2))
    with open(out_txt, "w") as f:
        f.write(f"DQC comparison total items checked: {len(REGISTRY)}\n")
        f.write(f"Mismatches found: {len(mismatches)}\n")
        for m in mismatches:
            f.write(f"  {m}\n")
    print(f"DQC check finished: {len(mismatches)} mismatches. Wrote {out_json} and {out_txt}")
    if mismatches:
        sys.exit(1)


def _recompute_single_filing(
    conn: Any,
    analyst: FundamentalAnalyst,
    f: dict[str, Any],
    a_info: dict[str, Any],
    cover_shares: list[CoverShares],
) -> bool:
    fid = f["id"]
    aid = f["asset_id"]
    engine = SimpleNamespace(conn=conn)

    stmts = statements_from_db(conn, fid)
    stmts.cover_shares = cover_shares
    if hasattr(stmts, "reit"):
        stmts.reit = a_info["is_reit"]

    own_key = own_period_key(stmts, f["form"], f["period_end"])
    if own_key is None:
        return False

    target_period = next((p for p in stmts.periods if p.key == own_key), None)
    if target_period is None:
        return False
    # T-151 parts 2 and 3: the company profile, the prior-year capital (MET-01/02, MD-02/35/38)
    # and the filing's accession (D14's provenance).
    db.attach_context(conn, aid, f["form"], own_key, stmts, accession=f["accession_number"])
    prior_period = stmts.prior_of(target_period)

    target = _Target(
        period=target_period,
        fiscal_period=f["fiscal_period"],
        prior=prior_period,
        fiscal_year=f["fiscal_year"],
    )

    year_task_kwargs: dict[str, Any] = {
        "asset_id": aid,
        "ticker": a_info["ticker"],
        "company_name": a_info["company_name"],
        "form": f["form"],
        "year": f["fiscal_year"],
        "cik": a_info["cik"],
    }
    if "reit" in {field.name for field in dataclasses.fields(_YearTask)}:
        year_task_kwargs["reit"] = a_info["is_reit"]
    task = _YearTask(**year_task_kwargs)

    share_scale_factors = db.detect_share_scale_factors(
        conn, aid, stmts, target.period.key, exclude_filing_id=fid
    )

    mc_res = market_caps_as_of(conn, [aid], as_of=f["period_end"])
    market_cap_obj = mc_res.caps.get(aid)
    market_cap_missing = mc_res.missing.get(aid)

    ttm = _ttm_flows(engine, task, stmts, target, accession=f["accession_number"])

    ctx_kwargs: dict[str, Any] = {
        "ticker": task.ticker,
        "company_name": task.company_name,
        "form": task.form,
        "fiscal_period": target.fiscal_period,
        "stmts": stmts,
        "period_key": target.period.key,
        "prior_key": target.prior.key if target.prior else None,
        "price": close_on_or_before(conn, aid, f["period_end"]),
        "share_scale_factors": share_scale_factors,
        "ttm_flows": ttm,
        "cik": task.cik,
    }
    fc_fields = {field.name for field in dataclasses.fields(FilingContext)}
    if "market_cap" in fc_fields:
        ctx_kwargs["market_cap"] = market_cap_obj
    if "market_cap_missing" in fc_fields:
        ctx_kwargs["market_cap_missing"] = market_cap_missing
    ctx = FilingContext(**ctx_kwargs)

    computed = analyst._compute_all(ctx)
    flat = {f"{g}.{r.name}": r.value for g, r in computed}
    # T-073: the facts-only verdict -- D1's coverage rule, then D3's pillar fallback.
    assessment = facts_only_synthesis(computed).assessment

    db.stamp_ytd(computed, ttm)
    db.record_metrics(conn, fid, computed, event_time=f["period_end"], run_id=None)
    utility_coverage.write(conn, fid, aid, stmts)
    quality.gate_version(conn, db.METRICS_ENGINE_VERSION, filing_id=fid, run_id=None)
    quality.record_ttm_crosscheck(
        conn, fid, aid, ttm, engine_version=db.METRICS_ENGINE_VERSION, run_id=None
    )
    db.insert_snapshot(
        conn,
        SnapshotRow(
            asset_id=aid,
            filing_id=fid,
            form=f["form"],
            fiscal_period=target.fiscal_period,
            score=assessment.score,
            rating=assessment.rating,
            narrative=assessment.narrative,
            strengths=assessment.strengths,
            risks=assessment.risks,
            model="deterministic-fallback",
            metrics=flat,
            event_time=f["period_end"],
            prompt_hash=analyst.prompt_hash,
        ),
    )
    return True


# The recompute deletes and rewrites rows of the current engine, so it only ever runs on a database
# under the task scratch (T-151 part 1, OPEN QUESTION 5). Tests override the root by monkeypatch;
# the production default is the scratch tree.
RECOMPUTE_SCRATCH_ROOT = Path("/workspaces/thesis/scratch")


def _refuse_outside_scratch(db_path: str) -> None:
    """Refuse a *db_path* outside :data:`RECOMPUTE_SCRATCH_ROOT` -- the recompute is destructive and
    must never touch the read-only production database or the pilot universe."""
    resolved = Path(db_path).expanduser().resolve()
    root = RECOMPUTE_SCRATCH_ROOT.expanduser().resolve()
    if not resolved.is_relative_to(root):
        raise SystemExit(
            f"recompute refuses {resolved}: it deletes and rewrites rows, so it runs only on a "
            f"database under {root} (T-151 part 1)"
        )


def cmd_recompute(args: argparse.Namespace) -> None:
    print(f"fundamental_agent source: {fundamental_agent.__file__}", flush=True)

    db_path = args.db
    _refuse_outside_scratch(db_path)
    conn = connect(db_path)

    # 1. Before writing: delete existing rows of the current engine and their quality issues
    # (T-151 part 1: ``metrics-v6`` -> ``metrics-v7``; read from the code, never hard-coded, so the
    # recompute always replaces the version the resolver is about to write)
    engine = db.METRICS_ENGINE_VERSION
    print(
        f"Deleting existing {engine} rows from fundamental_metrics and data_quality_issue...",
        flush=True,
    )
    conn.execute("DELETE FROM fundamental_metrics WHERE engine_version = ?", (engine,))
    conn.execute("DELETE FROM data_quality_issue WHERE metric_engine_version = ?", (engine,))
    conn.commit()

    # 2. Pre-load assets, their CIK, and their REIT status -- from the company profile (T-071 part 1:
    # the GICS predicate this used to read in SQL is now the profile's own `reit` column).
    asset_rows = conn.execute("""
        SELECT a.id, a.ticker, a.cik, a.company_name
        FROM assets a
        LEFT JOIN sectors s ON s.id = a.sector_id
    """).fetchall()
    assets = {
        r["id"]: {
            "ticker": r["ticker"],
            "cik": r["cik"],
            "company_name": r["company_name"] or r["ticker"],
            "is_reit": db.is_equity_reit(conn, int(r["id"])),
        }
        for r in asset_rows
    }

    # 3. Pre-load cover shares by filing_id
    cover_rows = conn.execute("""
        SELECT filing_id, value, as_of_date, class_member
        FROM filing_cover_shares
    """).fetchall()
    cover_by_filing: dict[int, list[CoverShares]] = defaultdict(list)
    for cr in cover_rows:
        cover_by_filing[cr["filing_id"]].append(
            CoverShares(cr["value"], cr["as_of_date"], cr["class_member"])
        )

    # 4. Fetch all filings in period-end order per company
    query = """
        SELECT f.id, f.asset_id, f.form, f.fiscal_year, f.fiscal_period, f.period_end, f.filing_date, f.accession_number
        FROM sec_filings f
        ORDER BY f.asset_id ASC, f.period_end ASC, f.id ASC
    """
    filing_rows = list(conn.execute(query).fetchall())
    if args.limit:
        filing_rows = filing_rows[: args.limit]

    print(
        f"Recomputing metrics for {len(filing_rows)} filings across {len(assets)} companies...",
        flush=True,
    )

    analyst = FundamentalAnalyst(None, "rule-based")
    t0 = time.time()
    skipped_filings: list[dict[str, Any]] = []
    for idx, f in enumerate(filing_rows, start=1):
        ok = _recompute_single_filing(
            conn, analyst, f, assets[f["asset_id"]], cover_by_filing.get(f["id"], [])
        )
        if not ok:
            skipped_filings.append(f)
        if idx % 500 == 0 or idx == len(filing_rows):
            conn.commit()
            dt = time.time() - t0
            print(
                f"[{idx}/{len(filing_rows)}] filings recomputed in {dt:.1f}s ({idx / dt:.1f} filings/s)...",
                flush=True,
            )

    conn.commit()

    if skipped_filings:
        print(f"\nSkipped {len(skipped_filings)} filings with no own period column:", flush=True)
        for sf in skipped_filings:
            print(
                f"  filing_id={sf['id']}, ticker={assets[sf['asset_id']]['ticker']}, "
                f"form={sf['form']}, period_end={sf['period_end']}, accession={sf['accession_number']}",
                flush=True,
            )
        out_skipped = Path("/workspaces/thesis/scratch/t148b/out/skipped_no_own_period.json")
        out_skipped.parent.mkdir(parents=True, exist_ok=True)
        out_skipped.write_text(json.dumps([dict(sf) for sf in skipped_filings], indent=2))

    n_filings = conn.execute("SELECT COUNT(*) FROM sec_filings").fetchone()[0]
    n_metrics = conn.execute(
        "SELECT COUNT(DISTINCT filing_id) FROM fundamental_metrics WHERE engine_version = ?",
        (engine,),
    ).fetchone()[0]
    metric_rows = conn.execute(
        "SELECT COUNT(*) FROM fundamental_metrics WHERE engine_version = ?", (engine,)
    ).fetchone()[0]
    print("\nRecompute finished:")
    print(f"  Filings in database:           {n_filings}")
    print(f"  Filings with {engine} rows:  {n_metrics}")
    print(f"  Total {engine} rows written: {metric_rows}")

    if not args.limit:
        expected_metrics = len(filing_rows) - len(skipped_filings)
        assert n_metrics == expected_metrics, (
            f"Expected {expected_metrics} filings to have metrics, found {n_metrics}"
        )
        print("Assertion passed: copy has metric rows for every filing with an own period column!")


if __name__ == "__main__":
    main()
