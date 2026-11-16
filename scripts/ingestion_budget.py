"""R1 (T-153 part 3a): the D2 ingestion scope and the never-latest filings, per company.

    uv run python scripts/ingestion_budget.py --universe-db PATH [--out FILE] [--analysis-date D]

Read-only. Under D2 (one rule for every company) a run on *D* ingests, for every CIK line that
was a member at any time in ``[2022-01-03, D]``: its 10-K/10-Q filings **filed from 2022-01-01 to
its last membership day** (nothing after its exit), and its prices from 2019-01-02 to that day.
The filings come from the SEC submissions files (the cache the ``sec_xcheck`` client writes),
under the CIK or its recorded succession chain (``cik_successions.csv``).

The **never-latest** count is the LLM cost of the symmetric rule: a filing that is never the
latest usable one on a date its company was **actually a member**. It is measured over each
company's own membership sessions (its stints in the universe file, intersected with the window
and *D*), never over the whole window -- a member that joined after 2022-01-03 is not a member
before it joined, so a filing it already held is not "used" then.

Output is committed (``docs/ingestion_budget.json``) so the budget in ``docs/model_fixes.md`` is
reproducible from the repo. Token figures are T-073's per-filing estimate (see ``TOKENS_IN``).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sec_xcheck.common import SecClient, cache_dir  # noqa: E402
from sec_xcheck.submissions import all_filings  # noqa: E402

from kg_schema import identity  # noqa: E402
from kg_schema.queries import WINDOW_START, connect_ro  # noqa: E402
from kg_schema.trading_calendar import available_from, is_trading_day  # noqa: E402

DEFAULT_OUT = REPO / "docs" / "ingestion_budget.json"
PRICES_SINCE = "2019-01-02"
FILINGS_SINCE = "2022-01-01"
FORMS = ("10-K", "10-Q")
# T-073's per-filing estimate (docs/model_fixes.md "T-073"): the rendered orchestrator prompt
# (1,582 tokens) plus the specialists' system prompts (38,922 chars / 4 = 9,730 tokens over the
# nine groups) plus the group user prompts -> 11-12k in; `agents._MAX_TOKENS` 1,500 out.
TOKENS_IN = 11_500
TOKENS_OUT = 1_500


@dataclass
class Company:
    """One CIK and the membership stints that put it in the window."""

    cik: str
    current: bool  # still a member (an open stint)
    stints: list[tuple[str, str | None]] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)

    @property
    def last_day(self) -> str | None:
        """The last membership day, or ``None`` while a stint is open."""
        if any(end is None for _, end in self.stints):
            return None
        return max(end for _, end in self.stints if end is not None)


def companies(universe: Any, window_start: str, analysis_date: str) -> list[Company]:
    """Every CIK line that was a member at any time in the window (one per CIK)."""
    rows = universe.execute(
        "SELECT symbol, cik, valid_from, valid_to FROM universe_membership "
        "WHERE cik IS NOT NULL AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?) "
        "ORDER BY cik, valid_from",
        (analysis_date, window_start),
    ).fetchall()
    by_cik: dict[str, Company] = {}
    for r in rows:
        cik = str(r["cik"])
        entry = by_cik.setdefault(cik, Company(cik, False))
        entry.stints.append((str(r["valid_from"]), str(r["valid_to"]) if r["valid_to"] else None))
        entry.symbols.append(str(r["symbol"]))
        entry.current = entry.current or r["valid_to"] is None
    return sorted(by_cik.values(), key=lambda c: c.cik)


def sessions(start: str, end: str) -> list[str]:
    """The NYSE sessions in ``[start, end]``, ascending."""
    day, last = date.fromisoformat(start), date.fromisoformat(end)
    out: list[str] = []
    while day <= last:
        if is_trading_day(day):
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def membership_sessions(company: Company, analysis_date: str, window_start: str) -> list[str]:
    """The sessions *company* was a member, inside ``[window_start, min(its exit, D)]``."""
    out: set[str] = set()
    for valid_from, valid_to in company.stints:
        start = max(valid_from, window_start)
        end = min(valid_to or analysis_date, analysis_date)
        if start <= end:
            out.update(sessions(start, end))
    return sorted(out)


def never_latest(filings: list[tuple[str, str]], member_days: list[str]) -> int:
    """Filings never the latest usable one on a membership session.

    *filings* is ``(filing_date, available_at)``. A filing is the latest usable one from its
    ``available_at`` to the next filing's, so it is *never-latest* when that interval holds no
    membership session (it was superseded before the company's next membership day)."""
    ordered = sorted(filings, key=lambda f: (f[1], f[0]))
    count = 0
    for i, (_, available) in enumerate(ordered):
        stop = ordered[i + 1][1] if i + 1 < len(ordered) else None
        lo = bisect_left(member_days, available)
        if lo >= len(member_days) or (stop is not None and member_days[lo] >= stop):
            count += 1
    return count


def measure(
    universe_path: str, client: Any, analysis_date: str, window_start: str
) -> dict[str, Any]:
    """The whole budget document."""
    preds: dict[str, list[str]] = {}
    for row in identity.read_successions():
        preds.setdefault(row["successor_cik"], []).append(row["predecessor_cik"])

    with contextlib.closing(connect_ro(universe_path)) as universe:
        lines = companies(universe, window_start, analysis_date)

    groups: dict[str, dict[str, int]] = {
        "current": {"companies": 0, "filings": 0, "trading_days": 0, "never_latest": 0},
        "leaver": {"companies": 0, "filings": 0, "trading_days": 0, "never_latest": 0},
    }
    detail: list[dict[str, Any]] = []
    for company in lines:
        group = "current" if company.current else "leaver"
        last_day = company.last_day
        filings_end = min(last_day or analysis_date, analysis_date)
        filings: list[tuple[str, str]] = []
        for cik in (company.cik, *preds.get(company.cik, [])):
            docs = all_filings(client, cik, FILINGS_SINCE) or []
            filings += [
                (f["filingDate"], available_from(f["filingDate"]))
                for f in docs
                if f["form"] in FORMS and FILINGS_SINCE <= f["filingDate"] <= filings_end
            ]
        member_days = membership_sessions(company, analysis_date, window_start)
        price_end = min(last_day or analysis_date, analysis_date)
        never = never_latest(filings, member_days)
        groups[group]["companies"] += 1
        groups[group]["filings"] += len(filings)
        groups[group]["trading_days"] += len(sessions(PRICES_SINCE, price_end))
        groups[group]["never_latest"] += never
        detail.append(
            {
                "cik": company.cik,
                "symbols": "/".join(sorted(set(company.symbols))),
                "group": group,
                "last_membership_day": last_day,
                "membership_days": len(member_days),
                "filings": len(filings),
                "never_latest": never,
            }
        )
    tokens = {
        name: {"in": n * TOKENS_IN, "out": n * TOKENS_OUT}
        for name, n in (
            ("current", groups["current"]["never_latest"]),
            ("leaver", groups["leaver"]["never_latest"]),
            ("total", groups["current"]["never_latest"] + groups["leaver"]["never_latest"]),
        )
    }
    totals = {
        k: sum(g[k] for g in groups.values())
        for k in ("companies", "filings", "trading_days", "never_latest")
    }
    return {
        "universe_db": str(universe_path),
        "analysis_date": analysis_date,
        "window_start": window_start,
        "prices_since": PRICES_SINCE,
        "filings_since": FILINGS_SINCE,
        "tokens_per_filing": {"in": TOKENS_IN, "out": TOKENS_OUT},
        "groups": groups,
        "totals": totals,
        "never_latest_tokens": tokens,
        "detail": detail,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--universe-db", required=True, help="universe_history.db (read-only)")
    ap.add_argument("--analysis-date", default="2026-10-14")
    ap.add_argument("--window-start", default=WINDOW_START)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    client = SecClient(cache_dir(), offline=True)
    report = measure(args.universe_db, client, args.analysis_date, args.window_start)
    Path(args.out).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    for name in ("current", "leaver"):
        g = report["groups"][name]
        print(
            f"{name:8} companies {g['companies']:4} filings {g['filings']:5} "
            f"trading days {g['trading_days']:7} never-latest {g['never_latest']:4} "
            f"({report['never_latest_tokens'][name]['in']:,} in + "
            f"{report['never_latest_tokens'][name]['out']:,} out tokens)"
        )
    t = report["totals"]
    print(
        f"total    companies {t['companies']:4} filings {t['filings']:5} "
        f"trading days {t['trading_days']:7} never-latest {t['never_latest']:4} "
        f"({report['never_latest_tokens']['total']['in']:,} in + "
        f"{report['never_latest_tokens']['total']['out']:,} out tokens)"
    )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
