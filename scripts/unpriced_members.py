"""R1 (T-153 part 3c, amended): the coverage union per replay date under the session rule.

Read-only. Eligibility (reviewer A, 2026-10-16; C11 printed p.36 applied by analogy, index
addition -> selection): a member is usable on cycle date D only if its line has a close on the
market's **latest session on or before D** -- `trading_calendar.last_session_on_or_before(D)` --
in the cycle's own `price_observation` engine version (`priceobs-v2`), with a non-NULL close. That
replaces the 7-day window for eligibility; the price vetoes keep their own 7 days.

The verdict input is the **union** -- (i) "no 10-K/10-Q public yet" | unpriced, counted once per
asset. No "(i)+0.046" sum is reported (part 3a's correction A dropped it).

WTW and BNY Mellon (their older SEC submissions page is not cached) are given both ways: bound A
counts them in (i), bound B out of it.

Two unpriced sides: before the Tiingo import (the archive's gateway rows), and after it (the
manifest's per-line `last_date`/`missing_sessions` -- dates only, never a price).

    uv run python scripts/unpriced_members.py --db ARCHIVE --universe U.db \
        --manifest leaver_prices.manifest.json --exits exit_list.json --out docs/unpriced_members.json
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
from kg_schema.trading_calendar import is_trading_day, last_session_on_or_before

WINDOWS = ("2021-01-01", "2022-01-01")
FIRST, LAST = "2022-01-03", "2026-10-05"
STEP_DAYS = 7  # cycle backfill's own step: the replay dates are the weekly ones
FORMS = ("10-K", "10-Q")
MAX_OBSERVATION_AGE_DAYS = 7  # the price vetoes' own window (not eligibility)
BAND = (0.046, 0.054)  # 0.05 +/- 0.004, the two undetermined members' weight (2/503)
BAND_VERDICT = 0.05
MAJORITY = 0.5  # "most of that date's members" for the store's own latest session

# The request's 35 lines (docs/upstream_requests/data_mining_leaver_prices.md). INFO and WRK were
# not delivered; the other 33 are in the manifest (which carries each one's own dates).
REQUEST_LINES = (
    "XLNX",
    "INFO",
    "PBCT",
    "DISCK",
    "CERN",
    "CTXS",
    "DRE",
    "NLSN",
    "TWTR",
    "ABMD",
    "SBNY",
    "SIVB",
    "FRC",
    "DISH",
    "ATVI",
    "SEE",
    "PXD",
    "CMA",
    "WRK",
    "MRO",
    "CTLT",
    "QRVO",
    "DFS",
    "JNPR",
    "ANSS",
    "HES",
    "WBA",
    "IPG",
    "K",
    "DAY",
    "HOLX",
    "CTRA",
    "EA",
    "AVB",
    "WBD",
)
ALIAS = {"DISCA": "WBD", "CDAY": "DAY"}  # the request's own "Covers" column
UNOBTAINABLE = {"INFO", "WRK"}
MEMBER_GAPS = {"VLTO": "gateway gap, current member (VLTO's first two sessions 2023-10-02/03)"}
UNDETERMINED = {"0001140536": "WTW", "0001390777": "BNY Mellon"}
Observations = dict[int, tuple[list[str], list[float | None]]]


def line_of(symbol: str) -> str:
    """The request line a member symbol belongs to (the request's own aliases applied)."""
    return ALIAS.get(symbol.upper(), symbol.upper())


def unpriced_after_import(line: str, day: str) -> bool:
    """Does *line* stay unpriced on *day* once the import has landed? (date-based only)"""
    if line not in REQUEST_LINES:
        return True  # a non-request member's gap is not fixed by the import
    return line in UNOBTAINABLE or day > MANIFEST_LAST.get(line, "")


def unpriced_class(line: str, day: str) -> str:
    """The class of an unpriced member on *day*: tiingo-dependent, or permanent with its reason."""
    if line not in REQUEST_LINES:
        return "permanent: gateway gap"
    if line in UNOBTAINABLE:
        return "permanent: unobtainable (the whole line)"
    last = MANIFEST_LAST.get(line, "")
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


def usable(pair: tuple[list[str], list[float | None]] | None, day: date) -> bool:
    """The price vetoes' own freshness rule: a row within 7 days with a non-NULL close."""
    if pair is None:
        return False
    dates, closes = pair
    i = bisect.bisect_right(dates, day.isoformat()) - 1
    return (
        i >= 0
        and (day - date.fromisoformat(dates[i])).days <= MAX_OBSERVATION_AGE_DAYS
        and closes[i] is not None
    )


def usable_on_session(pair: tuple[list[str], list[float | None]] | None, session: str) -> bool:
    """Eligibility: a close on exactly *session* (the latest session on or before the cycle date)."""
    if pair is None:
        return False
    dates, closes = pair
    i = bisect.bisect_left(dates, session)
    return i < len(dates) and dates[i] == session and closes[i] is not None


def last_close_date(pair: tuple[list[str], list[float | None]] | None, day: str) -> str | None:
    """The date of the member's latest close on or before *day* -- never the close's value."""
    if pair is None:
        return None
    dates, _closes = pair
    i = bisect.bisect_right(dates, day) - 1
    return dates[i] if i >= 0 else None


def has_filing(dates: list[str], window: str, day: str) -> bool:
    i = bisect.bisect_left(dates, window)
    return i < len(dates) and dates[i] <= day


def load_manifest(path: str) -> tuple[dict[str, str], dict[str, list[str]]]:
    """ask -> last served date; ask -> the sessions it does not cover (dates only)."""
    doc = json.loads(Path(path).read_text())
    last = {ln["ask"]: str(ln["last_date"]) for ln in doc["lines"]}
    missing = {ln["ask"]: [str(d) for d in ln.get("missing_sessions", [])] for ln in doc["lines"]}
    return last, missing


MANIFEST_LAST: dict[str, str] = {}
MANIFEST_MISSING: dict[str, list[str]] = {}


def manifest_usable(line: str, session: str) -> bool:
    """After the import: is a delivered line priced on *session*? (the manifest's own dates)"""
    if line in UNOBTAINABLE or line not in MANIFEST_LAST:
        return False
    return session <= MANIFEST_LAST[line] and session not in MANIFEST_MISSING.get(line, [])


def manifest_window_usable(line: str, day: str) -> bool:
    """After the import, under the *7-day* rule: is the line's latest covered session within 7
    days of *day*? (the manifest carries dates only -- a covered session has a close)."""
    if line in UNOBTAINABLE or line not in MANIFEST_LAST:
        return False
    last = MANIFEST_LAST[line]
    if day < last:
        return True  # inside the delivered range
    return (date.fromisoformat(day) - date.fromisoformat(last)).days <= MAX_OBSERVATION_AGE_DAYS


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
    """priceobs-v2 only -- the version the cycle reads (the archive holds v1 rows too)."""
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


@dataclass
class Measure:
    """The shared per-day inputs, so each replay date's row is one call."""

    universe: str
    mapping: dict[str, int]
    obs: Observations
    dates_by_cik: dict[str, list[str]]
    undetermined: dict[str, list[str]]
    preds: dict[str, list[str]]
    classes: dict[str, str] = field(default_factory=dict)
    member_gaps: dict[str, str] = field(default_factory=dict)

    def unpriced(self, day: date, ids: set[int], by_id: dict[int, str]) -> dict[int, str]:
        """asset_id -> class for every member with no close on the day's session."""
        session = last_session_on_or_before(day).isoformat()
        out: dict[int, str] = {}
        for aid in ids:
            if usable_on_session(self.obs.get(aid), session):
                continue
            symbol = by_id.get(aid, "")
            line = line_of(symbol)
            out[aid] = unpriced_class(line, day.isoformat())
            if line not in REQUEST_LINES:
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
        session = last_session_on_or_before(date.fromisoformat(day)).isoformat()
        unpriced = self.unpriced(date.fromisoformat(day), ids, by_id)
        window_unpriced = {a for a in ids if not usable(self.obs.get(a), date.fromisoformat(day))}
        # after the import: the manifest's own dates for the delivered lines; the gateway's
        # behaviour for everything else (a non-request member's gap stays)
        after = set()
        window_after = set()
        for a in ids:
            line = line_of(by_id.get(a, ""))
            if line in MANIFEST_LAST or line in UNOBTAINABLE:
                if not manifest_usable(line, session):
                    after.add(a)
                if not manifest_window_usable(line, day):
                    window_after.add(a)
            else:  # the gateway's own behaviour is unchanged by the import
                if a in unpriced:
                    after.add(a)
                if a in window_unpriced:
                    window_after.add(a)
        newly = {a for a in unpriced if a not in window_unpriced}
        # the session rule against the 7-day window, both on the same (post-import) data
        newly_after = after - window_after
        row: dict[str, Any] = {
            "date": day,
            "session": session,
            "members": len(members),
            "unpriced_before": len(unpriced),
            "unpriced_after": len(after),
            "unpriced_window_rule": len(window_unpriced),
            "unpriced_after_window_rule": len(window_after),
            "unpriced_members": sorted(f"{by_id.get(a, '?')}:{k}" for a, k in unpriced.items()),
            "newly_unpriced": sorted(
                [
                    {
                        "ticker": by_id.get(a, "?"),
                        "class": self.classes.get(by_id.get(a, "?"), unpriced[a]),
                        "last_close_date": last_close_date(self.obs.get(a), day),
                    }
                    for a in newly
                ],
                key=lambda x: x["ticker"],
            ),
            "newly_unpriced_after_import": sorted(
                [
                    {
                        "ticker": by_id.get(a, "?"),
                        "class": self.classes.get(by_id.get(a, "?"), unpriced[a]),
                        "tiingo_last_date": MANIFEST_LAST.get(line_of(by_id.get(a, ""))),
                    }
                    for a in newly_after
                ],
                key=lambda x: x["ticker"],
            ),
        }
        for window, (base, undet) in self.i_sets(members, day).items():
            row[f"i_{window}"] = [len(base | undet), len(base)]
            for side, extra in (("before", set(unpriced)), ("after", after)):
                row[f"union_{side}_{window}"] = [len(base | undet | extra), len(base | extra)]
            row[f"union_window_rule_{window}"] = [
                len(base | undet | window_unpriced),
                len(base | window_unpriced),
            ]
        return row


def session_comparison(
    rows: list[dict[str, Any]], universe: str, obs: Observations, mapping: dict[str, int]
) -> list[dict[str, Any]]:
    """Per replay date: the calendar's session(D) vs the store's own latest session on or before D
    (the latest priceobs-v2 date on which most of that date's members have a close)."""
    out: list[dict[str, Any]] = []
    for row in rows:
        day = row["date"]
        with contextlib.closing(connect_ro(universe)) as univ:
            members = members_asof(univ, day)
        ids = [mapping[m.symbol.upper()] for m in members if m.symbol.upper() in mapping]
        if not ids:
            continue
        candidates: dict[str, int] = {}
        for aid in ids:
            pair = obs.get(aid)
            if pair is None:
                continue
            dates, closes = pair
            for d, c in zip(dates, closes, strict=True):
                if d <= day and c is not None:
                    candidates[d] = candidates.get(d, 0) + 1
        store = None
        for d in sorted(candidates, reverse=True):
            if candidates[d] >= MAJORITY * len(ids):
                store = d
                break
        if store != row["session"]:
            out.append(
                {
                    "date": day,
                    "calendar_session": row["session"],
                    "store_session": store,
                    "members": len(ids),
                    "closes_on_store_session": candidates.get(store or "", 0),
                }
            )
    return out


def cross_checks(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Reviewer B's two cross-checks, reproduced."""
    holes = [r for r in rows if r["newly_unpriced"]]
    end_sessions = [
        {
            "date": r["date"],
            "members": r["members"],
            "newly_unpriced_after_import": r["newly_unpriced_after_import"],
        }
        for r in rows
        if r["newly_unpriced_after_import"]
    ]
    return {
        "before_import_newly_unpriced_dates": len(holes),
        "before_import_newly_unpriced": holes,
        "after_import_end_sessions_on_replay_dates": end_sessions,
    }


def sections(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The trigger, the refusals under both rules, and the refusal comparison."""
    out: dict[str, Any] = {
        "trigger": {},
        "refusals": {},
        "refusals_7_day": {},
        "refusal_changes": {},
    }
    for window in WINDOWS:
        for side in ("before", "after"):
            key = f"union_{side}_{window}"
            shares = [(r["date"], r[key][0] / r["members"], r[key][1] / r["members"]) for r in rows]
            hit_dates = {d for d, _, _ in trigger(shares)}
            out["trigger"][f"{window}/{side}"] = [
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
            out["refusals"][f"{window}/{side}"] = [
                {
                    "date": r["date"],
                    "union": r[key][0],
                    "members": r["members"],
                    "share": r[key][0] / r["members"],
                }
                for r in rows
                if r[key][0] / r["members"] > BAND_VERDICT
            ]
        key = f"union_window_rule_{window}"
        out["refusals_7_day"][window] = [
            {
                "date": r["date"],
                "union": r[key][0],
                "members": r["members"],
                "share": r[key][0] / r["members"],
            }
            for r in rows
            if r[key][0] / r["members"] > BAND_VERDICT
        ]
    # the refusal comparison (section 3.3): the session rule against dac2c01's 7-day results,
    # which are the archive's own (before the import)
    for window in WINDOWS:
        for side in ("before", "after"):
            now = {r["date"] for r in out["refusals"][f"{window}/{side}"]}
            before = {r["date"] for r in out["refusals_7_day"][window]}
            if now != before:
                out["refusal_changes"][f"{window}/{side}"] = {
                    "newly_refused": sorted(now - before),
                    "no_longer_refused": sorted(before - now),
                }
    return out


def measure(
    db: str, universe: str, cache: str | None, manifest: str, exits: str | None
) -> dict[str, Any]:
    global MANIFEST_LAST, MANIFEST_MISSING
    MANIFEST_LAST, MANIFEST_MISSING = load_manifest(manifest)
    classes: dict[str, str] = {}
    if exits:
        doc = json.loads(Path(exits).read_text())
        for e in doc.get("exits", []):
            for sym in e.get("symbols", [e["line"]]):
                classes[sym] = e["cause"]
        for e in doc.get("left_index_exit_later", []):
            for sym in e.get("symbols", [e["line"]]):
                classes[sym] = "left the index, exit later"
    client = SecClient(Path(cache) if cache else cache_dir(), offline=True)  # never fetches
    dates_by_cik, undetermined = filings_by_cik(client, universe)
    preds: dict[str, list[str]] = {}
    for succession in identity.read_successions():
        preds.setdefault(succession["successor_cik"], []).append(succession["predecessor_cik"])
    obs = observations(db)

    replay: list[str] = []
    day = date.fromisoformat(FIRST)
    while day <= date.fromisoformat(LAST):
        replay.append(day.isoformat())
        day += timedelta(days=STEP_DAYS)
    with contextlib.closing(connect_ro(universe)) as univ:
        pairs: set[tuple[str, str | None]] = set()
        for d in replay:
            pairs.update((m.symbol, m.cik) for m in members_asof(univ, d))
    with contextlib.closing(connect_ro(db)) as arch:
        mapping, missing = resolve_asset_ids(arch, sorted(pairs), as_of=LAST)

    state = Measure(universe, mapping, obs, dates_by_cik, undetermined, preds, classes)
    rows = [state.day_row(d) for d in replay]
    sessions = sum(1 for d in replay if is_trading_day(date.fromisoformat(d)))

    report: dict[str, Any] = {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rule": "a close on the latest NYSE session on or before D (C11 printed p.36 by analogy)",
        "db": db,
        "db_sha256": sha256(db),
        "universe": universe,
        "universe_sha1": sha1(universe),
        "manifest": manifest,
        "manifest_sha256": sha256(manifest),
        "windows": list(WINDOWS),
        "first": FIRST,
        "last": LAST,
        "step_days": STEP_DAYS,
        "replay_dates": len(rows),
        "replay_dates_that_are_nyse_sessions": sessions,
        "undetermined_members": UNDETERMINED,
        "undetermined_pages_missing": {c: v for c, v in sorted(undetermined.items())},
        "member_gaps": state.member_gaps,
        "members_resolved": len(mapping),
        "members_missing": sorted(missing),
        "session_comparison": session_comparison(rows, universe, obs, mapping),
        "cross_checks": cross_checks(rows),
        "rows": rows,
        "exit_list": (json.loads(Path(exits).read_text()) if exits else None),
    }
    report.update(sections(rows))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="the archive copy (read-only)")
    parser.add_argument("--universe", required=True, help="universe_history_gics.db")
    parser.add_argument("--manifest", required=True, help="leaver_prices.manifest.json")
    parser.add_argument("--exits", default=None, help="exit_list.json, for the member classes")
    parser.add_argument("--out", required=True, help="the JSON to write")
    parser.add_argument("--cache", default=None)
    args = parser.parse_args(argv)
    report = measure(args.db, args.universe, args.cache, args.manifest, args.exits)
    Path(args.out).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    print(
        f"{report['replay_dates']} weekly replay dates "
        f"({report['replay_dates_that_are_nyse_sessions']} NYSE sessions); "
        f"{report['members_resolved']} members resolved, {len(report['members_missing'])} missing; "
        f"non-request gaps: {sorted(report['member_gaps'])}"
    )
    print(
        f"session comparison: {len(report['session_comparison'])} dates where the calendar and "
        f"the store differ"
    )
    for key, hits in report["trigger"].items():
        print(f"  trigger {key}: {len(hits)} dates")
    for key, over in report["refusals"].items():
        print(
            f"  refusal {key}: {len(over)} dates"
            + (f" ({over[0]['date']} .. {over[-1]['date']})" if over else "")
        )
    for key, over in report["refusals_7_day"].items():
        print(
            f"  refusal (7-day rule) {key}: {len(over)} dates"
            + (f" ({over[0]['date']} .. {over[-1]['date']})" if over else "")
        )
    print(
        f"refusal changes: {json.dumps({k: {kk: len(vv) for kk, vv in v.items()} for k, v in report['refusal_changes'].items()})}"
    )
    cc = report["cross_checks"]
    print(
        f"cross-checks: before-import newly unpriced dates {cc['before_import_newly_unpriced_dates']}; "
        f"after-import end sessions on replay dates "
        f"{len(cc['after_import_end_sessions_on_replay_dates'])}"
    )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
