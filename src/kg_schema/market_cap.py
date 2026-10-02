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
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median
from typing import Any

from portfolio_common.db import Database

from . import availability, queries

#: Newest cover count a cap may rest on: a quarter plus the filing lag plus slack. The normal worst
#: case is a count dated at one filing, still the latest one just before the next is available.
DEFAULT_MAX_SHARE_AGE_DAYS = 200
#: Newest close a cap may rest on (a long weekend and a missing bar or two).
DEFAULT_MAX_PRICE_AGE_DAYS = 10
DEFAULT_CORPACT_ENGINE = "corpact-v1"

NO_COVER_SHARES = "no_cover_shares"
STALE_SHARES = "stale_shares"
NO_RECENT_PRICE = "no_recent_price"


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


def cover_total(entries: Iterable[tuple[str, float, str]]) -> ShareCount | None:
    """One filing's count from its cover entries ``(class_member, value, as_of_date)``: the filer's
    own total (class ``""``, its newest) when it filed one, else each class's newest value summed
    and dated by the oldest of them. ``None`` for no entries.

    The one aggregation the stored valuation metric and the as-of reader share, so the two cannot
    disagree on what a dual-class filer's count is."""
    items = list(entries)
    totals = [e for e in items if not e[0]]
    if totals:
        _, value, as_of = max(totals, key=lambda e: e[2])
        return ShareCount(value, as_of, 0)
    newest: dict[str, tuple[str, float, str]] = {}
    for e in items:
        if e[0] not in newest or e[2] > newest[e[0]][2]:
            newest[e[0]] = e
    if not newest:
        return None
    return ShareCount(
        sum(e[1] for e in newest.values()), min(e[2] for e in newest.values()), len(newest)
    )


@dataclass(frozen=True)
class _Count:
    filing_id: int
    available_at: str
    value: float
    as_of_date: str
    n_classes: int


def _filing_counts(rows: Iterable[Any]) -> dict[int, list[_Count]]:
    """Per asset, each usable filing's share count (:func:`cover_total`)."""
    by_filing: dict[tuple[int, int], list[Any]] = defaultdict(list)
    for r in rows:
        by_filing[(int(r["asset_id"]), int(r["filing_id"]))].append(r)
    out: dict[int, list[_Count]] = defaultdict(list)
    for (asset_id, filing_id), entries in by_filing.items():
        total = cover_total(
            (str(e["class_member"]), float(e["value"]), str(e["as_of_date"])) for e in entries
        )
        if total is not None:
            out[asset_id].append(
                _Count(
                    filing_id,
                    str(entries[0]["available_at"]),
                    total.value,
                    total.as_of_date,
                    total.n_classes,
                )
            )
    return out


def _latest(counts: list[_Count]) -> _Count:
    return max(counts, key=lambda c: (c.as_of_date, c.available_at, c.filing_id))


def _split_factors(
    db: Database, asset_ids: list[int], engine_version: str
) -> dict[int, list[tuple[str, float]]]:
    out: dict[int, list[tuple[str, float]]] = defaultdict(list)
    for r in queries.split_rows(db, asset_ids, engine_version=engine_version):
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


def market_caps_as_of(  # noqa: PLR0913 - keyword-only knobs with defaults
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
    counts = _filing_counts(queries.cover_share_rows(db, wanted, as_of))
    floor = (date.fromisoformat(as_of) - timedelta(days=max_price_age_days)).isoformat()
    closes = {
        int(r["asset_id"]): (str(r["date"]), float(r["close"]))
        for r in queries.last_closes(db, wanted, as_of=as_of, floor=floor)
    }
    basis_end = queries.last_price_dates(db, wanted)
    splits = _split_factors(db, wanted, corpact_engine_version)

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
        close_date, close = closes[aid]
        factor = _factor(splits.get(aid, []), after=count.as_of_date, through=basis_end.get(aid))
        shares = count.value * factor
        result.caps[aid] = MarketCap(
            asset_id=aid,
            value=shares * close,
            shares=shares,
            share_count=count.value,
            share_as_of=count.as_of_date,
            share_age_days=age,
            filing_id=count.filing_id,
            close=close,
            close_date=close_date,
            n_classes=count.n_classes,
            split_factor=factor,
        )
    return result
