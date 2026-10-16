"""R1 (T-153 part 3c): the coverage union's share per cycle date, both filing windows.

Read-only. The verdict input is the **union** -- (i) "no 10-K/10-Q public yet" | unpriced, counted
once per asset (brief R1 / D2). No "(i)+0.046" sum is reported: part 3a's fix round dropped it
(0.046 is a 2026 figure and does not apply to a 2022 date -- docs/model_fixes.md, T-153 part 3a,
"The declared start effect", correction A).

Two undetermined members -- WTW (CIK 0001140536) and BNY Mellon (CIK 0001390777), whose older SEC
submissions page is not cached -- are given both ways: bound A counts them in (i), bound B out of
it. The trigger is a date where a bound is in [0.046, 0.054] **and** the bounds differ.

Two unpriced scenarios (reviewer B, 2026-10-16):
  before the Tiingo import -- every measured unpriced member;
  after it -- only the permanent gaps (INFO, WRK, the delivered lines' exit tails, and every
  non-request member's gap), the 33 Tiingo-dependent lines being treated as priced.

Every member whose series starts after its membership starts is classified: a parent's SPLIT
ex-date near the start is evidence regular-way trading began there, so a series that begins after
it is a **gap**; "pre-trading" needs a source for the first trading day, and no in-window case has
one (VLTO 2023-10-02/03 is the only one -- see /tmp/t153e/series_start.py, run 2026-10-16).

    uv run python scripts/unpriced_members.py --db DB --universe UNIV.db --out docs/unpriced_members.json
"""

from __future__ import annotations

import argparse
import bisect
import contextlib
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from sec_xcheck.common import SecClient, cache_dir
from sec_xcheck.submissions import page_names, rows_of

from kg_schema import identity
from kg_schema.db import connect_ro
from kg_schema.queries import members_asof, resolve_asset_ids
from kg_schema.trading_calendar import is_trading_day

WINDOWS = ("2021-01-01", "2022-01-01")
FIRST, LAST = "2022-01-03", "2026-10-05"
STEP_DAYS = 7  # cycle backfill's own step
FORMS = ("10-K", "10-Q")
MAX_OBSERVATION_AGE_DAYS = 7  # cycle/rules/base.py's price-freshness rule
BAND = (0.046, 0.054)  # 0.05 +/- 0.004, the two undetermined members' weight (2/503)
BAND_VERDICT = 0.05

# The request's 35 lines (docs/upstream_requests/data_mining_leaver_prices.md), keyed by the ticker
# each is asked under; last_served = the last NYSE session the delivered series covers
# (task_b_feasibility_report.md section 7, data-mining 2026-10-14); None = unobtainable, the whole
# line is a permanent gap. 33 lines are delivered (35 minus INFO and WRK).
REQUEST: dict[str, str | None] = {
    "XLNX": "2022-02-11",
    "INFO": None,
    "PBCT": "2022-04-01",
    "DISCK": "2022-04-08",
    "CERN": "2022-06-07",
    "CTXS": "2022-09-29",
    "DRE": "2022-09-30",
    "NLSN": "2022-10-11",
    "TWTR": "2022-10-27",
    "ABMD": "2022-12-21",
    "SBNY": "2023-03-10",
    "SIVB": "2023-03-09",
    "FRC": "2023-04-28",
    "DISH": "2023-06-16",
    "ATVI": "2023-10-12",
    "SEE": "2023-12-15",
    "PXD": "2024-05-02",
    "CMA": "2024-06-21",
    "WRK": None,
    "MRO": "2024-11-21",
    "CTLT": "2024-12-17",
    "QRVO": "2024-12-20",
    "DFS": "2025-05-16",
    "JNPR": "2025-07-01",
    "ANSS": "2025-07-16",
    "HES": "2025-07-17",
    "WBA": "2025-08-27",
    "IPG": "2025-11-26",
    "K": "2025-12-10",
    "DAY": "2026-02-03",
    "HOLX": "2026-04-06",
    "CTRA": "2026-05-06",
    "EA": "2026-08-04",
    "AVB": "2026-08-14",
    "WBD": "2026-10-05",
}
# a member symbol sharing a request line (the request's own "Covers" column)
ALIAS = {"DISCA": "WBD", "CDAY": "DAY"}
# a non-request member whose gateway series misses sessions its membership covers (reviewer B,
# 2026-10-16), with its evidence: DHR's spin-off SPLIT x1.128 ex-date 2023-10-02 is the first day
# DHR traded without Veralto; VLTO's first stored row (2023-10-04) carries 8.36M shares against
# 15.35M the next session, not a first-day volume.
MEMBER_GAPS = {"VLTO": "gateway gap, current member (VLTO's first two sessions 2023-10-02/03)"}
UNDETERMINED = {"0001140536": "WTW", "0001390777": "BNY Mellon"}
Observations = dict[int, tuple[list[str], list[float | None]]]


def line_of(symbol: str) -> str:
    """The request line a member symbol belongs to (the request's own aliases applied)."""
    return ALIAS.get(symbol.upper(), symbol.upper())


def unpriced_after_import(line: str, day: str) -> bool:
    """Does *line* stay unpriced on *day* once the import has landed?"""
    if line not in REQUEST:
        return True  # a non-request member's gap is not fixed by the import
    last = REQUEST[line]
    return last is None or day > last


def unpriced_class(line: str, day: str) -> str:
    """The class of an unpriced member on *day*: tiingo-dependent, or permanent with its reason."""
    if line not in REQUEST:
        return "permanent: gateway gap"
    last = REQUEST[line]
    if last is None:
        return "permanent: unobtainable (the whole line)"
    if day > last:
        return f"permanent: exit tail (delivered through {last})"
    return "tiingo-dependent"


def trigger(pairs: list[tuple[str, float, float]]) -> list[tuple[str, float, float]]:
    """The rows where a bound is in the band **and** the two bounds differ."""
    return [
        (d, a, b)
        for d, a, b in pairs
        if (BAND[0] <= a <= BAND[1] or BAND[0] <= b <= BAND[1]) and a != b
    ]


def sha1(path: str) -> str:
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()  # noqa: S324


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def filings_by_cik(
    client: SecClient, universe: str
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """CIK -> its 10-K/10-Q filing dates; CIK -> the windows whose older page is not cached."""
    with contextlib.closing(connect_ro(universe)) as univ:
        ciks = sorted(
            {
                str(r["cik"])
                for r in univ.execute(
                    "SELECT DISTINCT cik FROM universe_membership WHERE cik IS NOT NULL"
                )
            }
        )
    ciks = sorted(set(ciks) | {r["predecessor_cik"] for r in identity.read_successions()})
    out: dict[str, list[str]] = {}
    undetermined: dict[str, list[str]] = {}
    for cik in ciks:
        doc = client.get("submissions", cik)
        if doc is None:
            out[cik] = []
            continue
        rows = rows_of(doc.get("filings", {}).get("recent", {}))
        missing: list[str] = []
        for window in WINDOWS:
            for name in page_names(doc, window):
                try:
                    page = client.get_file("submissions", name)
                except FileNotFoundError:
                    missing.append(window)
                    continue
                if page:
                    rows.extend(rows_of(page))
        out[cik] = sorted({r["filingDate"] for r in rows if r["form"] in FORMS and r["filingDate"]})
        if missing:
            undetermined[cik] = sorted(set(missing))
    return out, undetermined


def observations(db: str) -> Observations:
    with contextlib.closing(connect_ro(db)) as conn:
        rows = conn.execute(
            "SELECT asset_id, obs_date, close FROM price_observation "
            "WHERE engine_version='priceobs-v2' ORDER BY asset_id, obs_date"
        ).fetchall()
    out: Observations = {}
    for r in rows:
        dates, closes = out.setdefault(int(r["asset_id"]), ([], []))
        dates.append(str(r["obs_date"]))
        closes.append(r["close"])
    return out


def usable(pair: tuple[list[str], list[float | None]] | None, day: date) -> bool:
    """The price vetoes' own freshness rule: a row within the age limit with a non-NULL close."""
    if pair is None:
        return False
    dates, closes = pair
    i = bisect.bisect_right(dates, day.isoformat()) - 1
    return (
        i >= 0
        and (day - date.fromisoformat(dates[i])).days <= MAX_OBSERVATION_AGE_DAYS
        and closes[i] is not None
    )


def has_filing(dates: list[str], window: str, day: str) -> bool:
    i = bisect.bisect_left(dates, window)
    return i < len(dates) and dates[i] <= day


@dataclass
class Measure:
    """The shared per-day inputs, so each day's row is one call."""

    universe: str
    mapping: dict[str, int]
    obs: Observations
    dates_by_cik: dict[str, list[str]]
    undetermined: dict[str, list[str]]
    preds: dict[str, list[str]]
    member_gaps: dict[str, str] = field(default_factory=dict)

    def unpriced(self, day: date, ids: set[int], by_id: dict[int, str]) -> dict[int, str]:
        """asset_id -> class for every member with no usable observation on *day*."""
        out: dict[int, str] = {}
        for aid in ids:
            if usable(self.obs.get(aid), day):
                continue
            symbol = by_id.get(aid, "")
            line = line_of(symbol)
            out[aid] = unpriced_class(line, day.isoformat())
            if line not in REQUEST:
                self.member_gaps.setdefault(
                    symbol,
                    MEMBER_GAPS.get(symbol, "gateway gap (no source for a first trading day)"),
                )
        return out

    def i_sets(self, members: list[Any], day: str) -> dict[str, tuple[set[int], set[int]]]:
        """window -> (base, undetermined) asset_ids with no 10-K/10-Q in [window, day]."""
        out: dict[str, tuple[set[int], set[int]]] = {}
        for window in WINDOWS:
            base: set[int] = set()
            undet: set[int] = set()
            for m in members:
                cik = str(m.cik) if m.cik else ""
                chain = (cik, *self.preds.get(cik, ()))
                if any(has_filing(self.dates_by_cik.get(c, []), window, day) for c in chain):
                    continue
                aid = self.mapping.get(m.symbol.upper())
                if aid is None:
                    continue
                if any(window in self.undetermined.get(c, []) for c in chain):
                    undet.add(aid)
                else:
                    base.add(aid)
            out[window] = (base, undet)
        return out

    def day_row(self, day: str) -> dict[str, Any]:
        with contextlib.closing(connect_ro(self.universe)) as univ:
            members = members_asof(univ, day)
        ids = {self.mapping[m.symbol.upper()] for m in members if m.symbol.upper() in self.mapping}
        by_id = {
            self.mapping[m.symbol.upper()]: m.symbol
            for m in members
            if m.symbol.upper() in self.mapping
        }
        unpriced = self.unpriced(date.fromisoformat(day), ids, by_id)
        permanent = {a for a, klass in unpriced.items() if klass.startswith("permanent")}
        row: dict[str, Any] = {
            "date": day,
            "members": len(members),
            "unpriced_before": len(unpriced),
            "unpriced_after": len(permanent),
            "unpriced_members": sorted(f"{by_id.get(a, '?')}:{k}" for a, k in unpriced.items()),
        }
        for window, (base, undet) in self.i_sets(members, day).items():
            row[f"i_{window}"] = [len(base | undet), len(base)]
            row[f"union_before_{window}"] = [
                len(base | undet | set(unpriced)),
                len(base | set(unpriced)),
            ]
            row[f"union_after_{window}"] = [len(base | undet | permanent), len(base | permanent)]
        return row


def curve(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows where the unpriced count or an (i) count changes."""
    out: list[dict[str, Any]] = []
    prev: tuple[Any, ...] | None = None
    for r in rows:
        key = (r["unpriced_before"], tuple(r["i_2021-01-01"]), tuple(r["i_2022-01-01"]))
        if key != prev:
            out.append({k: v for k, v in r.items() if k != "unpriced_members"})
            prev = key
    return out


def measure(db: str, universe: str, cache: str | None) -> dict[str, Any]:
    client = SecClient(Path(cache) if cache else cache_dir(), offline=True)  # never fetches
    dates_by_cik, undetermined = filings_by_cik(client, universe)
    preds: dict[str, list[str]] = {}
    for succession in identity.read_successions():
        preds.setdefault(succession["successor_cik"], []).append(succession["predecessor_cik"])
    obs = observations(db)

    days: list[str] = []
    day = date.fromisoformat(FIRST)
    while day <= date.fromisoformat(LAST):
        days.append(day.isoformat())
        day += timedelta(days=1)
    with contextlib.closing(connect_ro(universe)) as univ:
        pairs: set[tuple[str, str | None]] = set()
        for d in days:
            pairs.update((m.symbol, m.cik) for m in members_asof(univ, d))
    with contextlib.closing(connect_ro(db)) as arch:
        mapping, missing = resolve_asset_ids(arch, sorted(pairs), as_of=LAST)

    state = Measure(universe, mapping, obs, dates_by_cik, undetermined, preds)
    rows = [state.day_row(d) for d in days]

    weekly = []
    step = date.fromisoformat(FIRST)
    while step <= date.fromisoformat(LAST):
        weekly.append(step.isoformat())
        step += timedelta(days=STEP_DAYS)
    weekly_set = set(weekly)

    report: dict[str, Any] = {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "db": db,
        "db_sha256": sha256(db),
        "universe": universe,
        "universe_sha1": sha1(universe),
        "windows": list(WINDOWS),
        "first": FIRST,
        "last": LAST,
        "step_days": STEP_DAYS,
        "days": len(rows),
        "sessions": sum(1 for r in rows if is_trading_day(date.fromisoformat(r["date"]))),
        "undetermined_members": UNDETERMINED,
        "undetermined_pages_missing": {c: v for c, v in sorted(undetermined.items())},
        "member_gaps": state.member_gaps,
        "request_lines": REQUEST,
        "members_resolved": len(mapping),
        "members_missing": sorted(missing),
        "trigger": {},
        "over_0_05": {},
        "weekly": [r for r in rows if r["date"] in weekly_set],
        "curve": curve(rows),
    }
    for window in WINDOWS:
        for variant in ("before", "after"):
            key = f"union_{variant}_{window}"
            shares = [(r["date"], r[key][0] / r["members"], r[key][1] / r["members"]) for r in rows]
            hit_dates = {d for d, _, _ in trigger(shares)}
            report["trigger"][f"{window}/{variant}"] = [
                {
                    "date": r["date"],
                    "counts": r[key],
                    "members": r["members"],
                    "a": r[key][0] / r["members"],
                    "b": r[key][1] / r["members"],
                }
                for r in rows
                if r["date"] in hit_dates
            ]
            report["over_0_05"][f"{window}/{variant}"] = [
                {
                    "date": r["date"],
                    "union": r[key][0],
                    "members": r["members"],
                    "share": r[key][0] / r["members"],
                }
                for r in rows
                if r[key][0] / r["members"] > BAND_VERDICT
            ]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="the archive copy to measure (read-only)")
    parser.add_argument("--universe", required=True, help="universe_history_gics.db")
    parser.add_argument("--out", required=True, help="the JSON to write")
    parser.add_argument(
        "--cache", default=None, help="the SEC cache dir (default: .cache/sec_xcheck)"
    )
    args = parser.parse_args(argv)
    report = measure(args.db, args.universe, args.cache)
    Path(args.out).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    print(
        f"{report['days']} calendar days ({report['sessions']} NYSE sessions), "
        f"{len(report['weekly'])} weekly replay dates; "
        f"{report['members_resolved']} members resolved, "
        f"{len(report['members_missing'])} missing; "
        f"non-request gaps: {sorted(report['member_gaps'])}"
    )
    for key, hits in report["trigger"].items():
        print(f"  trigger {key}: {len(hits)} dates")
    for key, over in report["over_0_05"].items():
        print(
            f"  over 0.05 {key}: {len(over)} dates"
            + (f" ({over[0]['date']} .. {over[-1]['date']})" if over else "")
        )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
