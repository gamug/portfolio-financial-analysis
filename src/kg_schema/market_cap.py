"""Market capitalisation as of a date (T-132): the one reader ``cycle`` and ``quant`` share.

``market cap = (latest point-in-time share count) x (close at the as-of date)``.

* The share count is the filing's own **cover-page** count (``dei:EntityCommonStockSharesOutstanding``,
  stored in ``filing_cover_shares``), from the latest filing already *usable* on the as-of date
  (``available_at``, T-107). Never ``CommonStockSharesIssued`` (it includes treasury stock: PG's is
  1.7x its outstanding count) and never a weighted average.
* A count older than ``max_share_age_days`` is refused, not used: a stale count is a wrong cap that
  looks right (PG's 2024-03-31 cap was being used on 2026-09-29).
* The close is the last stored one on or before the as-of date, no older than
  ``max_price_age_days``. ``price_daily.close`` is split-adjusted **as of the last fetch**, a
  filing's count is on the basis of its own date, so a split in between would understate the cap by
  its ratio: the count is put on the price basis first, by every recorded split dated after the
  count and no later than the asset's newest stored bar (the bar the gateway's adjustment reaches).
* A name that cannot be valued is **reported with a reason** (``missing``), never given a number and
  never a silent zero -- what to do about it is the caller's decision.

Two classes are valued as one: the filer's own total when it filed one, else the classes summed,
all at the traded close. ``n_classes`` flags the sum so a caller can see it (a dual-class filer
whose classes differ in economic weight, e.g. BRK, is approximate until a per-class conversion is
modelled).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median
from typing import Any

from portfolio_common.db import Database

from . import availability, corporate_actions, queries

#: Newest cover count a cap may rest on: a quarter plus the filing lag plus slack. The normal worst
#: case is a count dated at one filing, still the latest one just before the next is available.
DEFAULT_MAX_SHARE_AGE_DAYS = 200
#: Newest close a cap may rest on (a long weekend and a missing bar or two).
DEFAULT_MAX_PRICE_AGE_DAYS = 10
DEFAULT_CORPACT_ENGINE = "corpact-v1"

NO_COVER_SHARES = "no_cover_shares"
STALE_SHARES = "stale_shares"
NO_RECENT_PRICE = "no_recent_price"
#: A recorded SPLIT row the tracked event list cannot explain (T-153 part 3d, D10): the cap is
#: refused rather than computed with a factor whose class nobody stated (MKT-02).
UNCLASSIFIED_ACTION = "unclassified_corporate_action"
#: A recorded SPLIT row the list classes as a combined class
#: (:data:`kg_schema.corporate_actions.COMBINED_CLASSES`, T-153 part 3d, D10): the vendor folded
#: two events into one factor that no filing states, so a cap the event would enter (the split
#: window or the spin-off window) is refused rather than valued from a guess. A cap outside both
#: windows is computed and the row contributes no factor.
COMBINED_ACTION = "combined_corporate_action"


@dataclass(frozen=True)
class MarketCap:
    """One asset's cap and every input it was built from (for the audit trail)."""

    asset_id: int
    value: float
    shares: float  # on the price's split basis: share_count x split_factor
    share_count: float  # as filed
    share_as_of: str
    share_age_days: int
    filing_id: int
    close: float
    close_date: str
    n_classes: int  # non-total classes summed into share_count; 0 for one class or a filed total
    split_factor: float
    # The spin-off factor (T-153 part 3d, D10): the ratio the recorded event adjusts the price
    # by, applied only while the close is on the pre-event basis. 1.0 when none applies.
    spinoff_factor: float = 1.0
    # The vendor of the close the cap used (T-153 part 3d, A3): ``'tiingo'`` when it came from the
    # private import. ``cycle``'s and quant's readers do not change; the fundamental agent's
    # narrative withholds the valuation group for such a filing.
    close_source: str | None = None
    cik: str | None = None  # the issuer: two listings of one company share it (GOOG/GOOGL)
    incomplete_classes: bool = False
    n_excluded: int = 0
    cover_member_not_common_excluded: bool = False


@dataclass
class MarketCapResult:
    as_of: str
    max_share_age_days: int
    max_price_age_days: int
    caps: dict[int, MarketCap] = field(default_factory=dict)
    missing: dict[int, str] = field(default_factory=dict)  # asset_id -> reason code

    def get(self, asset_id: int) -> float | None:
        cap = self.caps.get(asset_id)
        return cap.value if cap is not None else None

    def coverage(self) -> dict[str, Any]:
        """JSON-able coverage and age summary, for a run's ``params_json`` (T-132(d))."""
        ages = [c.share_age_days for c in self.caps.values()]
        return {
            "as_of": self.as_of,
            "n_assets": len(self.caps) + len(self.missing),
            "n_with_cap": len(self.caps),
            "n_missing": len(self.missing),
            "missing": {str(a): r for a, r in sorted(self.missing.items())},
            "n_multi_class": sum(1 for c in self.caps.values() if c.n_classes > 1),
            "max_share_age_days": self.max_share_age_days,
            "max_price_age_days": self.max_price_age_days,
            "share_age_days_median": float(median(ages)) if ages else None,
            "share_age_days_max": max(ages) if ages else None,
        }


@dataclass(frozen=True)
class ShareCount:
    """A filing's share count: ``value`` shares outstanding on ``as_of_date``."""

    value: float
    as_of_date: str
    n_classes: int  # non-total classes summed into value; 0 for one class or a filed total
    n_excluded: int = 0
    cover_member_not_common_excluded: bool = False


#: Listed share classes per CIK, cited from cover page Trading Symbol table (MKT-03):
#: {cik: {class_member: ticker}}. Keyed by 10-digit zero-padded CIK.
CLASS_LISTING: dict[str, dict[str, str]] = {
    # Fox Corporation (CIK 0001754301): Class A Common Stock trades as FOXA, Class B Common Stock trades as FOX.
    # Cover page Trading Symbol table, 10-K FY2026 (accession 0001628280-26-053960).
    "0001754301": {
        "us-gaap:CommonClassAMember": "FOXA",
        "us-gaap:CommonClassBMember": "FOX",
    },
    # News Corporation (CIK 0001564708): Class A Common Stock trades as NWSA, Class B Common Stock trades as NWS.
    # Cover page Trading Symbol table, 10-K FY2026 (accession 0001564708-26-000175).
    "0001564708": {
        "us-gaap:CommonClassAMember": "NWSA",
        "us-gaap:CommonClassBMember": "NWS",
    },
}

#: Share classes that convert into the class the filer's listing trades, as ``{cik:
#: {class_member: units of the traded class per share}}`` (T-132, MKT-03).
#: Keyed by the zero-padded 10-digit CIK, members as the gateway names them.
CLASS_CONVERSION: dict[str, dict[str, float]] = {
    # Berkshire Hathaway Inc. (CIK 0001067983): Class A Common Stock converts into 1,500 Class B Common Stock.
    # 10-K FY2025 (accession 0001193125-26-083899).
    "0001067983": {"us-gaap:CommonClassAMember": 1500.0},
    # Erie Indemnity Co. (CIK 0000922621): "Class B shares are convertible into Class A shares at a conversion ratio of 2,400 to 1."
    # 10-K FY2025 (accession 0001628280-26-010666).
    "0000922621": {"us-gaap:CommonClassBMember": 2400.0},
    # Block, Inc. / XYZ (CIK 0001512673): "Shares of the Company's Class B common stock are convertible into an equivalent number of shares of its Class A common stock and generally convert into shares of its Class A common stock upon transfer." (1:1)
    # 10-K FY2025 (accession 0001628280-26-012254).
    "0001512673": {"us-gaap:CommonClassBMember": 1.0},
    # Coinbase Global, Inc. (CIK 0001679788): "In addition, each share of Class B common stock will automatically convert into a share of Class A common stock upon a sale or transfer." (1:1)
    # 10-K FY2025 (accession 0001679788-26-000015).
    "0001679788": {"us-gaap:CommonClassBMember": 1.0},
    # Robinhood Markets, Inc. (CIK 0001783879): "Shares of Class B common stock will also automatically convert into shares of Class A common stock upon sale or transfer" (1:1)
    # 10-K FY2025 (accession 0001783879-26-000023).
    "0001783879": {"us-gaap:CommonClassBMember": 1.0},
}


def _conversion(cik: str | None) -> dict[str, float]:
    return CLASS_CONVERSION.get(str(cik).zfill(10), {}) if cik else {}


def _is_excluded_cover_member(member: str) -> bool:
    if not member:
        return False
    local = member.rsplit(":", maxsplit=1)[-1]
    return "Preferred" in local or "FullyDiluted" in local or local.endswith("UnitsMember")


def cover_total(
    entries: Iterable[tuple[str, float, str]], *, cik: str | None = None
) -> ShareCount | None:
    """One filing's count from its cover entries ``(class_member, value, as_of_date)``: the filer's
    own total (class ``""``, its newest) when it filed one, else each class's newest value, converted
    to the traded class by :data:`CLASS_CONVERSION` for *cik*, summed and dated by the oldest of
    them. Non-common members (Preferred, FullyDiluted, *UnitsMember) are excluded per MKT-04.
    ``None`` for no entries.

    The one aggregation the stored valuation metric and the as-of reader share, so the two cannot
    disagree on what a dual-class filer's count is."""
    items = list(entries)
    totals = [e for e in items if not e[0]]
    if totals:
        _, value, as_of = max(totals, key=lambda e: e[2])
        return ShareCount(value, as_of, 0)
    excluded_members: set[str] = set()
    newest: dict[str, tuple[str, float, str]] = {}
    for e in items:
        if _is_excluded_cover_member(e[0]):
            excluded_members.add(e[0])
            continue
        if e[0] not in newest or e[2] > newest[e[0]][2]:
            newest[e[0]] = e
    if not newest:
        return None
    factor = _conversion(cik)
    n_excluded = len(excluded_members)
    return ShareCount(
        sum(e[1] * factor.get(e[0], 1.0) for e in newest.values()),
        min(e[2] for e in newest.values()),
        len(newest),
        n_excluded=n_excluded,
        cover_member_not_common_excluded=n_excluded > 0,
    )


@dataclass(frozen=True)
class _Count:
    filing_id: int
    available_at: str
    value: float
    as_of_date: str
    n_classes: int
    cik: str | None = None
    entries: tuple[tuple[str, float, str], ...] = ()
    n_excluded: int = 0
    cover_member_not_common_excluded: bool = False


def _filing_counts(rows: Iterable[Any]) -> dict[int, list[_Count]]:
    """Per asset, each usable filing's share count (:func:`cover_total`)."""
    by_filing: dict[tuple[int, int], list[Any]] = defaultdict(list)
    for r in rows:
        by_filing[(int(r["asset_id"]), int(r["filing_id"]))].append(r)
    out: dict[int, list[_Count]] = defaultdict(list)
    for (asset_id, filing_id), entries in by_filing.items():
        raw_entries = tuple(
            (str(e["class_member"]), float(e["value"]), str(e["as_of_date"])) for e in entries
        )
        total = cover_total(raw_entries, cik=entries[0]["cik"])
        if total is not None:
            out[asset_id].append(
                _Count(
                    filing_id,
                    str(entries[0]["available_at"]),
                    total.value,
                    total.as_of_date,
                    total.n_classes,
                    entries[0]["cik"],
                    raw_entries,
                    n_excluded=total.n_excluded,
                    cover_member_not_common_excluded=total.cover_member_not_common_excluded,
                )
            )
    return out


def _latest(counts: list[_Count]) -> _Count:
    return max(counts, key=lambda c: (c.as_of_date, c.available_at, c.filing_id))


def _action_factors(
    db: Database,
    asset_ids: list[int],
    engine_versions: Sequence[str],
    events: tuple[corporate_actions.CorporateActionEvent, ...],
) -> dict[int, _ActionFactors]:
    """Each asset's recorded SPLIT rows, partitioned by the event list's class (T-153 part 3d, D10).

    A split or reverse split multiplies the count for a cap dated on either side of the event;
    a spin-off multiplies it **only** while the close it is paired with is still on the pre-event
    basis (the vendor adjusts the price series, the count does not change). A row the list does
    not explain is kept apart, and the caller refuses the cap rather than apply a guess."""
    out: dict[int, _ActionFactors] = defaultdict(_ActionFactors)
    for r in queries.split_rows_from(db, asset_ids, engine_versions=engine_versions):
        value = float(r["value"])
        if value <= 0:
            continue
        asset_id = int(r["asset_id"])
        ex_date = str(r["ex_date"])
        event = corporate_actions.explained(
            events, cik=str(r["cik"] or ""), ex_date=ex_date, value=value
        )
        if event is None:
            out[asset_id].unexplained.append((ex_date, value))
        elif event.event_class in corporate_actions.COMBINED_CLASSES:
            out[asset_id].combined.append((ex_date, value))
        elif event.event_class == corporate_actions.SPINOFF:
            out[asset_id].spinoffs.append((ex_date, value))
        else:
            out[asset_id].splits.append((ex_date, value))
    return out


@dataclass
class _ActionFactors:
    """One asset's recorded action factors, by class (and the ones the list cannot explain)."""

    splits: list[tuple[str, float]] = field(default_factory=list)
    spinoffs: list[tuple[str, float]] = field(default_factory=list)
    #: Events the list classes as a combined class (``COMBINED_CLASSES``): never applied, they
    #: refuse a cap that falls in either of their windows.
    combined: list[tuple[str, float]] = field(default_factory=list)
    unexplained: list[tuple[str, float]] = field(default_factory=list)


def _in_window(ex_date: str, start: str, through: str | None) -> bool:
    """``start < ex_date`` and no later than the newest stored bar (``through``)."""
    return ex_date > start and (through is None or ex_date <= through)


def _combined_enters_window(
    combined: list[tuple[str, float]], count_date: str, close_date: str, through: str | None
) -> bool:
    """Whether an event of a combined class would enter the split **or** the spin-off window.

    The union of the two windows already used in :func:`market_caps_as_of`: the split window (the
    count's date) and the spin-off window (the close's date), both bounded by the newest stored
    bar (T-153 part 3d, D10, reviewer A 2026-10-18)."""
    return any(
        _in_window(ex_date, count_date, through) or _in_window(ex_date, close_date, through)
        for ex_date, _value in combined
    )


def _split_factors(
    db: Database, asset_ids: list[int], engine_version: Sequence[str] | str
) -> dict[int, list[tuple[str, float]]]:
    """Every recorded split factor per asset (kept for readers that want the raw list)."""
    engines = (engine_version,) if isinstance(engine_version, str) else tuple(engine_version)
    out: dict[int, list[tuple[str, float]]] = defaultdict(list)
    for r in queries.split_rows_from(db, asset_ids, engine_versions=engines):
        if float(r["value"]) > 0:
            out[int(r["asset_id"])].append((str(r["ex_date"]), float(r["value"])))
    return out


def _factor(splits: list[tuple[str, float]], *, after: str, through: str | None) -> float:
    """Product of the split ratios dated after *after* and no later than *through*."""
    factor = 1.0
    for ex_date, ratio in splits:
        if ex_date > after and (through is None or ex_date <= through):
            factor *= ratio
    return factor


def _class_by_class_val(  # noqa: C901, PLR0913, PLR0917
    count: _Count,
    factor: float,
    close: float,
    ticker_to_asset: dict[str, int],
    closes: dict[int, tuple[str, float, str | None]],
    actions: dict[int, _ActionFactors],
    basis_end: dict[int, str],
) -> tuple[float | None, bool, str | None]:
    """Value a CIK in CLASS_LISTING class by class (MKT-03).

    Each class's own factors apply (T-153 part 3d, D10): its splits after the count's date, its
    spin-offs while its close is on the pre-event basis. The same guard the parent's own row gets is
    applied to each **sibling class**: a class asset whose recorded SPLIT row the list cannot
    explain, or whose combined-class event falls inside that class's own window (its own
    count date and close date), refuses the whole cap with the matching reason (reviewer B, fix
    round 1). Returns ``(value, incomplete, refusal_reason)``; ``(None, False, None)`` when a class
    close is missing."""
    cik_norm = str(count.cik).zfill(10) if count.cik else ""
    class_map = CLASS_LISTING.get(cik_norm, {})
    conv_map = CLASS_CONVERSION.get(cik_norm, {})
    newest: dict[str, tuple[str, float, str]] = {}
    for e in count.entries:
        if _is_excluded_cover_member(e[0]):
            continue
        if e[0] not in newest or e[2] > newest[e[0]][2]:
            newest[e[0]] = e
    total_val = 0.0
    incomplete = False
    for member, val, class_as_of in newest.values():
        if not member:
            continue
        if member in class_map:
            t = class_map[member]
            class_aid = ticker_to_asset.get(t.upper())
            if class_aid is None or class_aid not in closes:
                return None, False, None
            c_close = closes[class_aid][1]
            c_close_date = closes[class_aid][0]
            c_factors = actions.get(class_aid, _ActionFactors())
            if c_factors.unexplained:
                return None, False, UNCLASSIFIED_ACTION
            if _combined_enters_window(
                c_factors.combined, class_as_of, c_close_date, basis_end.get(class_aid)
            ):
                return None, False, COMBINED_ACTION
            c_fac = _factor(
                c_factors.splits,
                after=count.as_of_date,
                through=basis_end.get(class_aid),
            )
            c_spinoff = _factor(
                c_factors.spinoffs,
                after=c_close_date,
                through=basis_end.get(class_aid),
            )
            total_val += val * c_fac * c_spinoff * c_close
        elif member in conv_map:
            total_val += val * conv_map[member] * factor * close
        else:
            incomplete = True
            total_val += val * factor * close
    return total_val, incomplete, None


def _is_incomplete_classes(count: _Count) -> bool:
    if count.n_classes <= 1:
        return False
    cik_norm = str(count.cik).zfill(10) if count.cik else ""
    conv_map = CLASS_CONVERSION.get(cik_norm, {})
    newest: dict[str, tuple[str, float, str]] = {}
    for e in count.entries:
        if _is_excluded_cover_member(e[0]):
            continue
        if e[0] not in newest or e[2] > newest[e[0]][2]:
            newest[e[0]] = e
    non_totals = [m for m in newest if m]
    if len(non_totals) <= 1:
        return False
    unconverted = [c for c in non_totals if c not in conv_map]
    return len(unconverted) > 1


def market_caps_as_of(  # noqa: C901, PLR0913 - keyword-only knobs with defaults
    db: Database,
    asset_ids: list[int],
    *,
    as_of: str,
    max_share_age_days: int = DEFAULT_MAX_SHARE_AGE_DAYS,
    max_price_age_days: int = DEFAULT_MAX_PRICE_AGE_DAYS,
    corpact_engine_version: str = DEFAULT_CORPACT_ENGINE,
) -> MarketCapResult:
    """Every asset's cap on *as_of*, or its reason for not having one. See the module docstring."""
    availability.require(db)  # T-107: never read none of an un-backfilled database's filings
    result = MarketCapResult(as_of, max_share_age_days, max_price_age_days)
    wanted = sorted(set(asset_ids))
    if not wanted:
        return result
    listing_members = [
        (ticker, cik) for cik, classes in CLASS_LISTING.items() for ticker in classes.values()
    ]
    ticker_to_asset, _ = queries.resolve_asset_ids(db, listing_members, as_of=as_of)
    all_needed_assets = sorted(set(wanted) | set(ticker_to_asset.values()))

    counts = _filing_counts(queries.cover_share_rows(db, wanted, as_of))
    floor = (date.fromisoformat(as_of) - timedelta(days=max_price_age_days)).isoformat()
    closes = {
        int(r["asset_id"]): (str(r["date"]), float(r["close"]), r["source"])
        for r in queries.last_closes(db, all_needed_assets, as_of=as_of, floor=floor)
    }
    basis_end = queries.last_price_dates(db, all_needed_assets)
    engines = (
        (corpact_engine_version,)
        if isinstance(corpact_engine_version, str)
        else tuple(corpact_engine_version)
    )
    engine_versions = tuple(dict.fromkeys((*engines, *corporate_actions.READABLE_ENGINES)))
    actions = _action_factors(
        db, all_needed_assets, engine_versions, corporate_actions.read_events()
    )

    for aid in wanted:
        if aid not in counts:
            result.missing[aid] = NO_COVER_SHARES
            continue
        count = _latest(counts[aid])
        age = (date.fromisoformat(as_of) - date.fromisoformat(count.as_of_date)).days
        if age > max_share_age_days:
            result.missing[aid] = STALE_SHARES
            continue
        if aid not in closes:
            result.missing[aid] = NO_RECENT_PRICE
            continue
        close_date, close, close_source = closes[aid]
        factors = actions.get(aid, _ActionFactors())
        if factors.unexplained:
            # MKT-02: the count and the price must be on one basis, and only the event list says
            # which basis a recorded factor puts the count on. Never a guessed class.
            result.missing[aid] = UNCLASSIFIED_ACTION
            continue
        if _combined_enters_window(
            factors.combined, count.as_of_date, close_date, basis_end.get(aid)
        ):
            # D10: the vendor folded two events into one ratio no filing states, so a cap it would
            # enter is refused instead of valued from a decomposed guess; outside both windows the
            # row contributes nothing.
            result.missing[aid] = COMBINED_ACTION
            continue
        factor = _factor(factors.splits, after=count.as_of_date, through=basis_end.get(aid))
        spinoff_factor = _factor(factors.spinoffs, after=close_date, through=basis_end.get(aid))
        shares = count.value * factor * spinoff_factor
        cik_norm = str(count.cik).zfill(10) if count.cik else ""
        if cik_norm in CLASS_LISTING:
            val, incomplete, reason = _class_by_class_val(
                count, factor, close, ticker_to_asset, closes, actions, basis_end
            )
            if reason is not None:
                result.missing[aid] = reason
                continue
            if val is None:
                result.missing[aid] = NO_RECENT_PRICE
                continue
        else:
            val = shares * close
            incomplete = _is_incomplete_classes(count)

        result.caps[aid] = MarketCap(
            asset_id=aid,
            value=val,
            shares=shares,
            share_count=count.value,
            share_as_of=count.as_of_date,
            share_age_days=age,
            filing_id=count.filing_id,
            close=close,
            close_date=close_date,
            n_classes=count.n_classes,
            split_factor=factor,
            spinoff_factor=spinoff_factor,
            close_source=None if close_source is None else str(close_source),
            cik=count.cik,
            incomplete_classes=incomplete,
            n_excluded=count.n_excluded,
            cover_member_not_common_excluded=count.cover_member_not_common_excluded,
        )
    return result
