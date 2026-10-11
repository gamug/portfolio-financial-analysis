"""T-148 part 1 verification on scratch databases (docs/model_fixes.md): nothing here touches production.

    uv run python scripts/verify_t148a.py rebuild --prod DB --out /tmp/t148a/rebuilt.db --tickers PM,STZ,...
    uv run python scripts/verify_t148a.py replay  --prod DB --out /tmp/t148a/replay.db [--tickers PM,STZ,...]
    uv run python scripts/verify_t148a.py cost    --db  /tmp/t148a/rebuilt.db
    uv run python scripts/verify_t148a.py n13     --db  /tmp/t148a/rebuilt.db
    uv run python scripts/verify_t148a.py n23     --db  /tmp/t148a/rebuilt.db
    uv run python scripts/verify_t148a.py tables  --before before.json --after after.json

``--prod`` is opened ``mode=ro`` (``replay`` copies it, and the copy's sha1 is checked against the original's
before and after). Every write goes to ``--out``.

* ``rebuild`` -- a facts-only rebuild of a cohort from the EDGAR gateway: the pipeline with the analyst of
  ``verify_t149.py`` (every metric computed, the rule-based score, **no LLM call**). Needs the gateway.
* ``replay`` -- the same metrics from the stored ``financial_facts`` alone, for when the gateway is not reachable or
  to cover all of production: the facts get the sign hint the SEC's own filed values give (``companyfacts``: the
  displayed value times it is the filed one -- the field the gateway sends as ``preferred_sign``), and every
  metric group is computed at the filing's own period, without a TTM. A replay says nothing about the gateway's own
  hint (``signcheck.py`` is circular on it); it exercises everything else.
* ``cost`` -- MET-00's cost: the filings that now leave debt, the enterprise value or the ROIC empty where they were
  filled with 0, by sector.
* ``n13`` -- the filings that took the "available to common" line as net income (ID-03): how many file
  ``NetIncomeLoss`` (so they resolve now), how many are left empty and flagged.
* ``n23`` -- the debt and interest concepts the resolver did not read: how many filings now resolve through them.
* ``tables`` -- the before/after rows of two ``audit_sec_checklist.py measure`` results.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_t149
from sec_xcheck.common import (
    SecClient,
    cache_dir,
    cik_family,
    filings,
    iter_us_gaap,
    match_value,
    open_ro,
    own_period_key,
    statements_from_db,
)

from fundamental_agent import db as agent_db
from fundamental_agent.metrics import COMPUTERS
from fundamental_agent.statements import Statements
from kg_schema import connect, ensure

G = "us-gaap_"
N13_CONCEPT = G + "NetIncomeLossAvailableToCommonStockholdersBasic"
N23_DEBT = (
    G + "UnsecuredDebt",
    G + "SecuredDebt",
    G + "OtherLongTermDebtCurrent",
    G + "NotesPayableCurrent",
)
N23_INTEREST = G + "InterestExpenseDebt"
SHOWN = 5  # examples listed
# why a debt is empty, most specific first (the flags a Resolution of total_debt carries)
REASONS = ("short_term_debt_only", "partial_map_rejected", "capture_failure")
TABLE_ROWS = (
    "D1.income_tax_negative",
    "D2.equity_not_narrowest",
    "D2.net_income_profitloss_over_parent",
    "D3.income_tax_component",
    "D4.short_term_debt_order_differs",
    "D6c.tax_current_deferred_without_total",
    "D10.ev_debt_or_cash_zero_filled",
    "ID11.capital_expenditure_abs_changes_value",
    "ID11.interest_expense_abs_changes_value",
    "ID11.stock_based_compensation_abs_changes_value",
    "N3.ocf_continuing_resolved",
    "N4.debt_double_count",
    "N5.cash_double_count",
    "N6.balance_from_other_date",
    "N7.debt_absent",
    "N7.lt_debt_only",
    "N7.st_debt_only",
    "N13.net_income_available_to_common",
    "N14.da_depreciation_alone",
    "ID01.income_tax_label_or_standard_match",
    "ID01.net_income_label_or_standard_match",
    "ID01.equity_label_or_standard_match",
    "ID01.long_term_debt_label_or_standard_match",
    "ID01.short_term_debt_label_or_standard_match",
)


def sha1(path: Path) -> str:
    h = hashlib.sha1()  # noqa: S324 - a file fingerprint, not a security boundary
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# -- replay ---------------------------------------------------------------------------------------------


def _sec_index(client: SecClient, cik: str) -> dict[tuple[str, str, str], list[float]]:
    """``{(accession, period end, concept): [filed values]}`` of the company's SEC facts (duration and instant)."""
    index: dict[tuple[str, str, str], list[float]] = collections.defaultdict(list)
    for c in cik_family(cik):
        doc = client.get("companyfacts", c)
        if not doc:
            continue
        for _tax, concept, fact in iter_us_gaap(doc):
            index[(fact["accn"], fact["end"], G + concept)].append(fact["val"])
    return index


def _sign_of(vals: list[float] | None, displayed: float | None) -> float | None:
    if not vals or displayed in (None, 0.0):
        return None
    assert displayed is not None
    if any(match_value(v, displayed) for v in vals):
        return 1.0
    if any(match_value(v, -displayed) for v in vals):
        return -1.0
    return None


def cmd_replay(args: argparse.Namespace) -> int:
    prod, out = Path(args.prod), Path(args.out)
    before = sha1(prod)
    if out.exists():
        raise SystemExit(f"{out} exists; remove it first (a replay starts from a copy)")
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(prod, out)
    assert sha1(out) == before, "the copy differs from production"
    conn = connect(out)
    ensure(conn, run_migrations=True)
    conn.execute("DELETE FROM fundamental_metrics")
    conn.commit()
    client = SecClient(cache_dir(args.cache), offline=True)
    tickers = set(args.tickers.split(",")) if args.tickers else None
    done = skipped = signed = unsigned = 0
    index: dict[tuple[str, str, str], list[float]] = {}
    current_cik = None
    for f in filings(conn):
        if tickers and f["ticker"] not in tickers:
            continue
        if f["cik"] != current_cik:
            current_cik, index = f["cik"], _sec_index(client, f["cik"])
        rows = conn.execute(
            "SELECT id, concept, period_key, value FROM financial_facts WHERE filing_id = ?",
            (f["id"],),
        ).fetchall()
        updates = []
        for r in rows:
            sign = _sign_of(
                index.get((f["accession_number"], r["period_key"][:10], r["concept"])), r["value"]
            )
            if sign is not None:
                updates.append((sign, r["id"]))
        conn.executemany("UPDATE financial_facts SET preferred_sign = ? WHERE id = ?", updates)
        signed, unsigned = signed + len(updates), unsigned + len(rows) - len(updates)
        stmts = statements_from_db(conn, f["id"])
        key = own_period_key(stmts, f["form"], f["period_end"])
        if key is None:
            skipped += 1
            continue
        period = next(p for p in stmts.periods if p.key == key)
        prior = stmts.prior_of(period)
        results = [
            (group, result)
            for group, compute in COMPUTERS.items()
            for result in compute(stmts, key, prior.key if prior else None, None)
        ]
        agent_db.record_metrics(conn, f["id"], results, event_time=f["period_end"])
        done += 1
    conn.commit()
    print(
        f"replayed {done} filings, skipped {skipped}; sign hint on {signed} facts, none on {unsigned}"
    )
    assert sha1(prod) == before, "PRODUCTION CHANGED"
    print(f"production sha1 unchanged: {before}")
    return 0


# -- rebuild (the gateway) --------------------------------------------------------------------------------


def cmd_rebuild(args: argparse.Namespace) -> int:
    return verify_t149.cmd_pilot(
        argparse.Namespace(
            prod=args.prod,
            out=args.out,
            tickers=args.tickers,
            analysis_date=args.analysis_date,
            members_from_prod=True,
            no_prices=args.no_prices,
        )
    )


# -- the counts the PR reports ------------------------------------------------------------------------------


def _sector(conn: Any, ticker: str) -> str:
    row = conn.execute(
        "SELECT s.name FROM assets a LEFT JOIN sectors s ON s.id = a.sector_id WHERE a.ticker = ?",
        (ticker,),
    ).fetchone()
    return str(row[0]) if row and row[0] else "(none)"


def cmd_cost(args: argparse.Namespace) -> int:
    """Filings whose debt, EV or ROIC is now empty. The old code read a missing debt as 0 in the EV and the ROIC
    (``(total_debt or 0)``, ``sum_present(debt, equity)``), so each of these had a number before."""
    conn = open_ro(args.db)
    by_sector: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    reasons: collections.Counter[str] = collections.Counter()
    for f in filings(conn):
        stmts = statements_from_db(conn, f["id"])
        key = own_period_key(stmts, f["form"], f["period_end"])
        if key is None:
            continue
        sector = _sector(conn, f["ticker"])
        counts = by_sector[sector]
        counts["filings"] += 1
        debt = stmts.total_debt(key)
        cash = stmts.get("cash", key)
        equity = stmts.get("equity", key)
        no_debt, no_cash = debt.value is None, cash is None
        counts["debt_empty"] += no_debt
        counts["cash_empty"] += no_cash
        counts["ev_empty_where_it_was_filled"] += no_debt or no_cash
        counts["roic_empty_where_it_was_filled"] += (no_debt or no_cash) and equity is not None
        if no_debt:
            reasons[
                next((fl for fl in REASONS if fl in debt.flags), "no balance sheet column")
            ] += 1
        if debt.value == 0.0:
            counts["debt_zero_rule"] += 1
    total: collections.Counter[str] = collections.Counter()
    print(
        f"{'sector':26s} {'filings':>8s} {'debt empty':>11s} {'cash empty':>11s} {'EV empty':>9s} {'ROIC empty':>11s} {'debt=0 (ID-02b)':>16s}"
    )
    for sector, c in sorted(by_sector.items()):
        total.update(c)
        print(
            f"{sector:26s} {c['filings']:8d} {c['debt_empty']:11d} {c['cash_empty']:11d} "
            f"{c['ev_empty_where_it_was_filled']:9d} {c['roic_empty_where_it_was_filled']:11d} {c['debt_zero_rule']:16d}"
        )
    c = total
    print(
        f"{'ALL':26s} {c['filings']:8d} {c['debt_empty']:11d} {c['cash_empty']:11d} "
        f"{c['ev_empty_where_it_was_filled']:9d} {c['roic_empty_where_it_was_filled']:11d} {c['debt_zero_rule']:16d}"
    )
    print("why debt is empty:", dict(reasons.most_common()))
    return 0


def _old_net_income_concept(stmts: Statements, key: str) -> str | None:
    """The concept T-096's registry order picked before T-148: the first income-statement row, in document order, of
    ``NetIncomeLoss``, ``ProfitLoss`` or the available-to-common line that has a value in *key*."""
    wanted = (G + "NetIncomeLoss", G + "ProfitLoss", N13_CONCEPT)
    for row in stmts.raw["income_statement"]:
        if row.get("concept") in wanted and not row.get("dimension") and row.get(key) is not None:
            return str(row["concept"])
    return None


def cmd_n13(args: argparse.Namespace) -> int:
    """The filings the old resolver gave the "available to common" line (ID-03, N13): the income statement files that
    line and neither ``NetIncomeLoss`` nor ``ProfitLoss``. ``NetIncomeLoss`` filed at the source, or in another
    statement of the filing, is what the new resolver could use; the rest are empty and flagged."""
    conn = open_ro(args.db)
    client = SecClient(cache_dir(args.cache), offline=True)
    population: list[tuple[Any, ...]] = []
    for f in filings(conn):
        stmts = statements_from_db(conn, f["id"])
        key = own_period_key(stmts, f["form"], f["period_end"])
        if key is None:
            continue
        if _old_net_income_concept(stmts, key) == N13_CONCEPT:
            population.append((f, stmts, key))
    resolved = empty = in_other_statement = at_source = profit_in_cash_flow = 0
    companies: set[str] = set()
    examples: list[tuple[Any, ...]] = []
    for f, stmts, key in population:
        companies.add(f["ticker"])
        r = stmts.resolve("net_income", key)
        resolved += r.value is not None
        empty += r.value is None
        other = [
            (st, row["concept"])
            for st in ("cash_flow", "balance_sheet")
            for row in stmts.raw[st]
            if row["concept"] in (G + "NetIncomeLoss", G + "ProfitLoss")
            and not row.get("dimension")
        ]
        in_other_statement += bool(other)
        profit_in_cash_flow += any(c == G + "ProfitLoss" for _s, c in other)
        docs = [d for c in cik_family(f["cik"]) if (d := client.get("companyfacts", c))]
        at_source += any(
            fact["accn"] == f["accession_number"]
            and fact["end"] == f["period_end"]
            and concept == "NetIncomeLoss"
            for doc in docs
            for _t, concept, fact in iter_us_gaap(doc)
        )
        if len(examples) < SHOWN:
            examples.append((f["ticker"], f["form"], f["period_end"], r.flags))
    print(f"N13 population: {len(population)} filings, {len(companies)} companies")
    print(f"  now resolved to NetIncomeLoss / ProfitLoss - NCI: {resolved}")
    print(f"  now empty and flagged:                             {empty}")
    print(f"  NetIncomeLoss filed at the SEC for the accession/period: {at_source}")
    print(
        f"  NetIncomeLoss or ProfitLoss on another statement of the stored filing: {in_other_statement} (ProfitLoss on the cash flow: {profit_in_cash_flow})"
    )
    print("  companies:", sorted(companies))
    print("  examples:", examples)
    return 0


def cmd_n23(args: argparse.Namespace) -> int:
    """How many own-period filings resolve debt or interest through the N23 concepts (not read before)."""
    conn = open_ro(args.db)
    via: collections.Counter[str] = collections.Counter()
    gained: collections.Counter[str] = collections.Counter()
    total = 0
    for f in filings(conn):
        stmts = statements_from_db(conn, f["id"])
        key = own_period_key(stmts, f["form"], f["period_end"])
        if key is None:
            continue
        total += 1
        lt, st = stmts.resolve("long_term_debt", key), stmts.resolve("short_term_debt", key)
        for concept in N23_DEBT:
            if concept in (lt.concept or "") or concept in (st.concept or ""):
                via[concept.removeprefix(G)] += 1
        if any(c in (lt.concept or "") for c in (G + "UnsecuredDebt", G + "SecuredDebt")):
            gained["long-term debt now resolved (REIT pair)"] += 1
        interest = stmts.resolve("interest_expense", key)
        if interest.concept == N23_INTEREST:
            via["InterestExpenseDebt (interest expense)"] += 1
        debt = stmts.total_debt(key)
        if (
            debt.value is not None
            and lt.value is not None
            and any(c in (lt.concept or "") + (st.concept or "") for c in N23_DEBT)
        ):
            gained["total debt resolves through an N23 concept"] += 1
    print(f"{total} own-period filings")
    for name, n in via.most_common():
        print(f"  {n:5d}  {name}")
    for name, n in gained.most_common():
        print(f"  {n:5d}  {name}")
    return 0


def cmd_tables(args: argparse.Namespace) -> int:
    def load(path: str) -> dict[str, Any]:
        return {
            r["key"]: r for r in json.loads(Path(path).read_text()) if r.get("db") == "production"
        }

    def cell(r: dict[str, Any] | None) -> str:
        if r is None:
            return "-"
        # own-period filings / companies, then filings with the value in any column (T-147's convention)
        return f"{r['target_count']} f / {r['target_companies']} co; any column {r['filings']} f"

    before, after = load(args.before), load(args.after)
    print("| finding | before (production) | after |")
    print("|---|---|---|")
    for fid in TABLE_ROWS:
        print(f"| `{fid}` | {cell(before.get(fid))} | {cell(after.get(fid))} |")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn in (("rebuild", cmd_rebuild), ("replay", cmd_replay)):
        p = sub.add_parser(name)
        p.add_argument("--prod", required=True)
        p.add_argument("--out", required=True)
        p.add_argument("--tickers")
        p.add_argument("--cache")
        p.add_argument("--analysis-date", default="2026-09-30")
        p.add_argument("--no-prices", action="store_true")
        p.set_defaults(fn=fn)
    for name, fn in (("cost", cmd_cost), ("n13", cmd_n13), ("n23", cmd_n23)):
        p = sub.add_parser(name)
        p.add_argument("--db", required=True)
        p.add_argument("--cache")
        p.set_defaults(fn=fn)
    p = sub.add_parser("tables")
    p.add_argument("--before", required=True)
    p.add_argument("--after", required=True)
    p.set_defaults(fn=cmd_tables)
    args = parser.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
