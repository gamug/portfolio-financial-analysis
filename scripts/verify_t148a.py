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
* ``n23`` -- the debt and interest concepts the resolver did not read: how many filings now resolve through them, and
  how many REIT-pair filings (``SecuredDebt`` / ``UnsecuredDebt``, no map: no source) are left without a debt.
* ``ni`` -- the filings the first version of part 1 left without net income (only ``ProfitLoss``, no NCI-income row,
  or neither concept on the income statement): which ID-03 step now resolves each, and why the rest stay empty.
* ``cash`` -- the filings whose cash is empty, by what the balance sheet files instead (restricted-cash-inclusive line
  alone, ``Cash`` alone, an Article 9 bank, a missed ``CashAndCashEquivalentsAtCarryingValue``, nothing), by sector.
* ``flags`` -- how often each resolution flag is raised, per item.
* ``tables`` -- the before/after rows of two ``audit_sec_checklist.py measure`` results.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
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
from fundamental_agent.edgar_client import EdgarClient
from fundamental_agent.metrics import COMPUTERS
from fundamental_agent.statements import _NCI_INCOME_CONCEPTS, Statements, _names_nci_income
from kg_schema import connect, ensure

G = "us-gaap_"
N13_CONCEPT = G + "NetIncomeLossAvailableToCommonStockholdersBasic"
N23_DEBT = (
    G + "OtherLongTermDebtCurrent",
    G + "NotesPayableCurrent",
)
REIT_PAIR = (G + "UnsecuredDebt", G + "SecuredDebt")
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


def _leverage_inputs(conn: Any, filing_id: int) -> dict[str, Any] | None:
    """The audit inputs the pipeline stored with the filing's ``debt_to_equity``: the debt, equity and cash it read and
    the resolution flags -- what the engine computed from the whole payload (the stored facts keep neither the
    dimensional rows nor the values the gateway emptied, so a replay from them cannot say)."""
    row = conn.execute(
        "SELECT inputs_json FROM fundamental_metrics WHERE filing_id = ? AND metric_name = 'debt_to_equity' "
        "ORDER BY id DESC LIMIT 1",
        (filing_id,),
    ).fetchone()
    return None if row is None else dict(json.loads(row["inputs_json"] or "{}"))


def cmd_cost(args: argparse.Namespace) -> int:
    """Filings whose debt, EV or ROIC is now empty. The old code read a missing debt as 0 in the EV and the ROIC
    (``(total_debt or 0)``, ``sum_present(debt, equity)``), so each of these had a number before. Read from the
    stored metrics of the rebuilt database (own period)."""
    conn = open_ro(args.db)
    by_sector: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    reasons: collections.Counter[str] = collections.Counter()
    for f in filings(conn):
        inputs = _leverage_inputs(conn, f["id"])
        if inputs is None:
            continue
        counts = by_sector[_sector(conn, f["ticker"])]
        counts["filings"] += 1
        no_debt, no_cash = "total_debt" not in inputs, "cash" not in inputs
        counts["debt_empty"] += no_debt
        counts["cash_empty"] += no_cash
        counts["ev_empty_where_it_was_filled"] += no_debt or no_cash
        counts["roic_empty_where_it_was_filled"] += (no_debt or no_cash) and "equity" in inputs
        if no_debt:
            flags = {
                k.removeprefix("flag_total_debt_")
                for k in inputs
                if k.startswith("flag_total_debt_")
            }
            reasons[next((fl for fl in REASONS if fl in flags), "no balance sheet column")] += 1
        if inputs.get("flag_total_debt_no_debt_lines"):
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
    reit_filings: list[tuple[str, str, str, bool]] = []
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
        if lt.value is None and _files_any(stmts, "balance_sheet", REIT_PAIR, key):
            reit_filings.append((f["ticker"], f["form"], f["period_end"], debt.value is None))
    print(f"{total} own-period filings")
    for name, n in via.most_common():
        print(f"  {n:5d}  {name}")
    for name, n in gained.most_common():
        print(f"  {n:5d}  {name}")
    print(
        f"  {len(reit_filings):5d}  filings ({len({r[0] for r in reit_filings})} companies) file the REIT pair and no "
        f"other long-term line: long-term debt is empty; total debt empty in {sum(r[3] for r in reit_filings)}"
    )
    print("  examples:", [r[:3] for r in reit_filings[:SHOWN]])
    return 0


def _files_any(stmts: Statements, statement: str, concepts: tuple[str, ...], key: str) -> bool:
    """Whether *statement* has a non-dimensional row of one of *concepts* with a value at *key*'s balance-sheet
    column (an income-statement or cash-flow item: at *key* itself)."""
    column = stmts.resolve_column("cash", key) if statement == "balance_sheet" else key
    return column is not None and any(
        row.get("concept") in concepts and row.get(column) is not None
        for row in stmts.raw[statement]
        if not row.get("abstract") and not row.get("dimension")
    )


def _part1_net_income(stmts: Statements, key: str) -> float | None:
    """What the first version of part 1 read: the income statement alone -- ``NetIncomeLoss``; else ``ProfitLoss``
    less ``NetIncomeLossAttributableToNoncontrollingInterest`` when that row is filed (zero included); else empty."""

    def income(concept: str) -> float | None:
        for row in stmts.raw["income_statement"]:
            if (
                row.get("concept") == G + concept
                and not row.get("dimension")
                and row.get(key) is not None
            ):
                return float(row[key]) * (row.get("preferred_sign") or 1.0)
        return None

    if (parent := income("NetIncomeLoss")) is not None:
        return parent
    profit, nci = income("ProfitLoss"), income("NetIncomeLossAttributableToNoncontrollingInterest")
    return None if profit is None or nci is None else profit - nci


def _ni_step(r: Any) -> str:
    """Which step of ID-03 produced a net-income resolution (``r``: a ``Resolution``)."""
    if r.value is None:
        return "iv. empty: " + (r.flags[0] if r.flags else "?")
    if r.concept == G + "NetIncomeLoss":
        return "i. NetIncomeLoss from another statement of the filing"
    if r.how == "map":
        return "iii. ProfitLoss - NetIncomeLossAttributableToNoncontrollingInterest"
    return "ii. ProfitLoss, no NCI income filed"


def _nci_blockers(stmts: Statements, key: str) -> set[str]:
    """The rows that make ``ProfitLoss`` unusable as "no NCI income filed": ``concept | label``."""
    return {
        f"{str(row.get('concept')).removeprefix(G)} | {row.get('label')}"
        for row in stmts._valued_rows(key)
        if _names_nci_income(row)
    }


def _sec_files_net_income(client: SecClient, f: Any) -> bool:
    docs = [d for c in cik_family(f["cik"]) if (d := client.get("companyfacts", c))]
    return any(
        fact["accn"] == f["accession_number"]
        and fact["end"] == f["period_end"]
        and concept in ("NetIncomeLoss", "ProfitLoss")
        for doc in docs
        for _t, concept, fact in iter_us_gaap(doc)
    )


SEC_NCI_INCOME = (
    "NetIncomeLossAttributableToNoncontrollingInterest",
    "NetIncomeLossAttributableToRedeemableNoncontrollingInterest",
    "NetIncomeLossAttributableToNonredeemableNoncontrollingInterest",
)


def _sec_contradicts_no_nci(client: SecClient, f: Any, profit: float) -> tuple[str, float] | None:
    """Step (ii) against the source: the SEC files, for the accession and period end, an income attributable to the
    NCI (``"NCI income filed"``, with its largest share of the same duration's ``ProfitLoss``) or a ``NetIncomeLoss``
    that differs from the ``ProfitLoss`` of the same duration (``"net income differs"``). ``None`` when neither --
    including when the SEC files neither."""
    docs = [d for c in cik_family(f["cik"]) if (d := client.get("companyfacts", c))]
    by_concept: dict[str, dict[str | None, float]] = collections.defaultdict(dict)
    for doc in docs:
        for _t, concept, fact in iter_us_gaap(doc):
            if fact["accn"] == f["accession_number"] and fact["end"] == f["period_end"]:
                by_concept[concept][fact.get("start")] = fact["val"]
    share = max(
        (
            abs(v) / abs(by_concept["ProfitLoss"][start])
            for concept in SEC_NCI_INCOME
            for start, v in by_concept[concept].items()
            if v != 0 and by_concept["ProfitLoss"].get(start)
        ),
        default=0.0,
    )
    if share or any(v != 0 for c in SEC_NCI_INCOME for v in by_concept[c].values()):
        return "NCI income filed", share
    if any(
        not match_value(v, by_concept["ProfitLoss"][start])
        for start, v in by_concept["NetIncomeLoss"].items()
        if start in by_concept["ProfitLoss"]
    ):
        return "net income differs", 0.0
    return None


def cmd_ni(args: argparse.Namespace) -> int:  # noqa: C901 - a report: one linear pass
    """ID-03 (the review of PR #136): the filings the first version left without net income, by the step of the rule
    that now resolves them -- (i) ``NetIncomeLoss`` from another statement of the filing, (ii) ``ProfitLoss`` when no
    NCI income is filed, (iii) ``ProfitLoss`` less the NCI total -- and why the rest stay empty. Own-period filings."""
    conn = open_ro(args.db)
    client = SecClient(cache_dir(args.cache), offline=True)
    steps: dict[str, list[tuple[str, str, str]]] = collections.defaultdict(list)
    caught: collections.Counter[str] = collections.Counter()
    caption_only: list[tuple[str, str, str]] = []
    sec_has: list[tuple[str, str, str]] = []
    contradicted: dict[str, list[tuple[str, str, str]]] = collections.defaultdict(list)
    largest_share = 0.0
    total = population = 0
    for f in filings(conn):
        stmts = statements_from_db(conn, f["id"])
        key = own_period_key(stmts, f["form"], f["period_end"])
        if key is None:
            continue
        total += 1
        if _part1_net_income(stmts, key) is not None:
            continue
        population += 1
        ident = (f["ticker"], f["form"], f["period_end"])
        r = stmts.resolve("net_income", key)
        steps[_ni_step(r)].append(ident)
        if r.value is not None and "no_nci_income_filed" in r.flags:
            contradiction = _sec_contradicts_no_nci(client, f, r.value)
            if contradiction:
                contradicted[contradiction[0]].append(ident)
                largest_share = max(largest_share, contradiction[1])
        if r.value is None and _sec_files_net_income(client, f):
            sec_has.append(ident)
        if "profit_loss_nci_income_not_total" in r.flags:
            names = _nci_blockers(stmts, key)
            caught.update(names)
            if not any(
                n.split(" | ")[0] in {c.removeprefix(G) for c in _NCI_INCOME_CONCEPTS}
                for n in names
            ):
                caption_only.append(ident)
    print(
        f"{total} own-period filings; {population} had no net income in the first version of part 1"
    )
    for step in sorted(steps):
        v = steps[step]
        print(f"  {len(v):5d} f / {len({x[0] for x in v}):3d} co  {step}")
        print("        e.g.", v[:SHOWN])
    print(
        "  step (ii) against the SEC's companyfacts (a NetIncomeLoss that differs from ProfitLoss, or an NCI income filed):",
        {k: (len(v), v[:SHOWN]) for k, v in contradicted.items()} or "no contradiction",
        f"-- the largest NCI income is {largest_share:.2%} of that duration's ProfitLoss",
    )
    print(
        f"  empty, with NetIncomeLoss / ProfitLoss filed at the SEC for the accession: {len(sec_has)}",
        sec_has[:SHOWN],
    )
    print(
        f"  empty because only a caption or an extension names the NCI income: {len(caption_only)}",
        caption_only[:SHOWN],
    )
    print("  rows that block ProfitLoss as 'NCI income filed' (concept | label):")
    for name, n in caught.most_common(15):
        print(f"    {n:4d}  {name}")
    return 0


CASH_CONCEPT = G + "CashAndCashEquivalentsAtCarryingValue"
RESTRICTED_CASH = G + "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"
BANK_LINES = (G + "Deposits", G + "InterestBearingDepositLiabilities", G + "DepositsDomestic")
CASH_NAME_RE = re.compile("cash", re.IGNORECASE)


NO_CASH_LINE = "e. no cash line filed (non-dimensional) at the period end"


def _live_no_cash_lines(conn: Any, idents: list[tuple[str, str, str]]) -> None:
    """Category (e) against the gateway's live payload, one 10-K per company: is a cash line there at all -- filed
    only with a dimension (the stored facts never keep those), or not on the balance sheet?"""
    by_key = {(f["ticker"], f["form"], f["period_end"]): f for f in filings(conn)}
    seen: set[str] = set()
    with EdgarClient(os.environ["EDGAR_BASE_URL"]) as edgar:
        for ticker, form, period_end in idents:
            if ticker in seen or not form.startswith("10-K"):
                continue
            seen.add(ticker)
            f = by_key[(ticker, form, period_end)]
            payload = edgar.financials(
                f["cik"], form, int(f["filing_date"][:4]), accession_number=f["accession_number"]
            )
            rows = [
                r
                for r in payload["balance_sheet"]
                if CASH_NAME_RE.search(str(r.get("concept"))) and not r.get("abstract")
            ]
            dropped = [
                c
                for c in payload.get("corrections") or []
                if c.get("statement") == "balance_sheet"
                and c.get("concept") == CASH_CONCEPT
                and c.get("column") == period_end
            ]
            print(
                f"  live {ticker} {form} {period_end}: gateway corrections for the cash total:",
                [
                    (c.get("rule"), c.get("reason"), c.get("original"), c.get("corrected"))
                    for c in dropped
                ]
                or "none",
            )
            print(
                f"  live {ticker} {form} {period_end}: cash-like balance-sheet rows (dimensional or not):",
                sorted(
                    {f"{r['concept']}{' [dimension]' if r.get('dimension') else ''}" for r in rows}
                )
                or "none",
            )


def _cash_category(stmts: Statements, key: str | None) -> str:
    """Why a filing's cash is empty (the review of PR #136): what the balance sheet files at the period end instead."""
    column = None if key is None else stmts.resolve_column("cash", key)
    if column is None:
        return "z. no balance-sheet column at the period end"
    rows = [
        r for r in stmts.raw["balance_sheet"] if not r.get("abstract") and not r.get("dimension")
    ]
    cash = {
        r["concept"]
        for r in rows
        if CASH_NAME_RE.search(r["concept"]) and r.get(column) is not None
    }
    bank = any(r["concept"] in BANK_LINES for r in rows)
    named = ", ".join(sorted(c.split("_", 1)[-1] for c in cash))
    return next(
        label
        for applies, label in (
            (bank, "c. Article 9 bank (deposits on the balance sheet)"),
            (
                CASH_CONCEPT in cash,
                "d. CashAndCashEquivalentsAtCarryingValue is filed and was missed (a bug)",
            ),
            (
                cash == {RESTRICTED_CASH},
                "a. the restricted-cash-inclusive line is the only cash line",
            ),
            (cash == {G + "Cash"}, "b. Cash is the only cash line"),
            (bool(cash), f"f. other cash line(s): {named}"),
            (True, NO_CASH_LINE),
        )
        if applies
    )


def cmd_cash(args: argparse.Namespace) -> int:
    """The filings whose cash is empty (the stored leverage inputs carry no ``cash``), by category and by sector,
    with the EV / ROIC empties they cause."""
    conn = open_ro(args.db)
    cats: dict[str, list[tuple[str, str, str]]] = collections.defaultdict(list)
    sectors: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    total = 0
    for f in filings(conn):
        inputs = _leverage_inputs(conn, f["id"])
        if inputs is None:
            continue
        total += 1
        if "cash" in inputs:
            continue
        stmts = statements_from_db(conn, f["id"])
        key = own_period_key(stmts, f["form"], f["period_end"])
        cat = _cash_category(stmts, key)
        cats[cat].append((f["ticker"], f["form"], f["period_end"]))
        sectors[_sector(conn, f["ticker"])][cat[0]] += 1
    empty = sum(len(v) for v in cats.values())
    print(f"{total} filings with leverage inputs; cash empty in {empty} ({empty / total:.1%})")
    if args.live:
        _live_no_cash_lines(conn, cats.get(NO_CASH_LINE, []))
    for cat in sorted(cats):
        v = cats[cat]
        print(f"  {len(v):5d} f / {len({x[0] for x in v}):3d} co  {cat}")
        print("        e.g.", [f"{t} {frm} {pe}" for t, frm, pe in v[:SHOWN]])
        print("        companies:", sorted({x[0] for x in v}))
    print("by sector (category letter: filings):")
    for sector, c in sorted(sectors.items()):
        print(f"  {sector:26s} {sum(c.values()):4d}  {dict(sorted(c.items()))}")
    return 0


def cmd_flags(args: argparse.Namespace) -> int:
    """How often each resolution flag the pipeline stored (``flag_<item>_<flag>`` in the audit inputs) is raised,
    over the filings' own-period metric rows."""
    conn = open_ro(args.db)
    counts: collections.Counter[str] = collections.Counter()
    filings_seen: set[int] = set()
    for row in conn.execute(
        "SELECT filing_id, inputs_json FROM fundamental_metrics WHERE inputs_json LIKE '%flag_%'"
    ):
        for key in json.loads(row["inputs_json"] or "{}"):
            if key.startswith("flag_"):
                counts[(key, row["filing_id"])] += 0  # one filing counts once per flag
                filings_seen.add(row["filing_id"])
    per_flag: collections.Counter[str] = collections.Counter(key for key, _fid in counts)
    total = conn.execute("SELECT COUNT(DISTINCT filing_id) FROM fundamental_metrics").fetchone()[0]
    print(f"{total} filings with metrics; {len(filings_seen)} carry a flag")
    for key, n in per_flag.most_common():
        print(f"  {n:5d}  {key}")
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
    for name, fn in (
        ("cost", cmd_cost),
        ("n13", cmd_n13),
        ("n23", cmd_n23),
        ("ni", cmd_ni),
        ("flags", cmd_flags),
    ):
        p = sub.add_parser(name)
        p.add_argument("--db", required=True)
        p.add_argument("--cache")
        p.set_defaults(fn=fn)
    p = sub.add_parser("cash")
    p.add_argument("--db", required=True)
    p.add_argument(
        "--live", action="store_true", help="also read category (e)'s 10-Ks from the gateway"
    )
    p.set_defaults(fn=cmd_cash)
    p = sub.add_parser("tables")
    p.add_argument("--before", required=True)
    p.add_argument("--after", required=True)
    p.set_defaults(fn=cmd_tables)
    args = parser.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
