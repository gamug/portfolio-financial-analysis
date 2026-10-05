"""Turn a ranked, veto-filtered asset list into target portfolio weights.

Two constructions live here. ``target_weights`` is the original (HARD vetoes already removed ->
take the top N -> raw weights by scheme -> alternate name and sector caps 8 times -> renormalize);
it can silently return a book that breaks a cap, and ``orchestrator.py`` still calls it until
T-136 switches the caller and deletes it. ``build_book`` is its replacement (T-135, the T-134
decisions, SPEC FR-007): one pure, deterministic function whose result states every relaxation.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Candidate:
    asset_id: int
    blended_score: float
    sector_id: int | None
    realized_vol_90d: float | None


def _raw_weights(cands: list[Candidate], scheme: str) -> dict[int, float]:
    if scheme == "equal" or not cands:
        return {c.asset_id: 1.0 for c in cands}
    if scheme == "inverse_vol":
        inv = {c.asset_id: (1.0 / c.realized_vol_90d if c.realized_vol_90d else 0.0) for c in cands}
        if any(inv.values()):
            return inv
        return {c.asset_id: 1.0 for c in cands}
    # score_proportional (default): shift so the min score maps to a small positive weight
    lo = min(c.blended_score for c in cands)
    return {c.asset_id: (c.blended_score - lo) + 1.0 for c in cands}


def _cap_names(weights: dict[int, float], cap: float) -> dict[int, float]:
    """Water-fill: pin over-cap names at *cap*, spread the rest among the uncapped."""
    if not weights or cap * len(weights) < 1.0 - 1e-9:
        return _normalize(weights)  # cap too tight to sum to 1; best effort
    w = dict(weights)
    capped: set[int] = set()
    for _ in range(len(w) + 1):
        over = [aid for aid, v in w.items() if v > cap + 1e-12 and aid not in capped]
        if not over:
            break
        capped.update(over)
        for aid in over:
            w[aid] = cap
        used = sum(w[aid] for aid in capped)
        free = [aid for aid in w if aid not in capped]
        free_sum = sum(w[aid] for aid in free)
        if free_sum <= 0:
            break
        scale = (1.0 - used) / free_sum
        for aid in free:
            w[aid] *= scale
    return w


def _normalize(weights: dict[int, float]) -> dict[int, float]:
    total = sum(weights.values())
    if total <= 0:
        n = len(weights)
        return {aid: 1.0 / n for aid in weights} if n else {}
    return {aid: w / total for aid, w in weights.items()}


def target_weights(
    cands: list[Candidate],
    *,
    top_n: int,
    scheme: str = "score_proportional",
    max_name_weight: float = 0.10,
    max_sector_weight: float = 0.30,
) -> dict[int, float]:
    chosen = sorted(cands, key=lambda c: c.blended_score, reverse=True)[:top_n]
    if not chosen:
        return {}
    sector_of = {c.asset_id: c.sector_id for c in chosen}
    weights = _normalize(_raw_weights(chosen, scheme))

    # Alternate name and sector caps until both hold (or we give up and return the
    # closest feasible mix). Each cap step redistributes only to non-capped names.
    for _ in range(8):
        weights = _cap_names(weights, max_name_weight)
        before = dict(weights)
        weights = _cap_sectors(weights, sector_of, max_sector_weight)
        if (
            max(weights.values()) <= max_name_weight + 1e-9
            and _max_sector(weights, sector_of) <= max_sector_weight + 1e-9
        ):
            break
        if weights == before:
            break
    return weights


def _max_sector(weights: dict[int, float], sector_of: dict[int, int | None]) -> float:
    tot: dict[int | None, float] = {}
    for aid, w in weights.items():
        tot[sector_of[aid]] = tot.get(sector_of[aid], 0.0) + w
    return max(tot.values()) if tot else 0.0


def _cap_sectors(
    weights: dict[int, float], sector_of: dict[int, int | None], cap: float
) -> dict[int, float]:
    tot: dict[int | None, float] = {}
    for aid, wt in weights.items():
        tot[sector_of[aid]] = tot.get(sector_of[aid], 0.0) + wt
    over = {s for s, t in tot.items() if t > cap + 1e-12}
    if not over or all(sector_of[aid] in over for aid in weights):
        return _normalize(weights)
    w = dict(weights)
    for aid in w:
        if sector_of[aid] in over:
            w[aid] *= cap / tot[sector_of[aid]]
    used = sum(v for aid, v in w.items() if sector_of[aid] in over)
    free = [aid for aid in w if sector_of[aid] not in over]
    free_sum = sum(w[aid] for aid in free)
    if free_sum > 0:
        scale = (1.0 - used) / free_sum
        for aid in free:
            w[aid] *= scale
    return w


# -- build_book (T-135) -------------------------------------------------------------------------

VetoStatus = Literal["none", "SOFT", "HARD"]
SCHEMES = ("score_tilt", "equal", "score_proportional", "inverse_vol")
DEFAULT_SECTOR_CAP = 0.30
_LEGACY_NAME_CAP = 0.10  # the default name cap of the legacy schemes (equal/score_*/inverse_vol)
_TILT_FLOOR = 0.5  # score_tilt band, in multiples of 1/N_held: [0.5/N, 1.5/N]
_TILT_CEILING = 1.5
_EPS = 1e-12  # tolerance for "is this a real cap breach / relaxation", far below the 1e-9 contract
_FULL_SLACK = 1e-9  # keeps floor(0.3 * 10) at 3 despite float representation


@dataclass(frozen=True)
class BookCandidate:
    """One ranked asset offered to ``build_book``. HARD-vetoed assets are passed in too, marked,
    so a pin on one can be refused with a reason instead of vanishing."""

    asset_id: int
    ticker: str
    blended_score: float
    sector: str | None
    realized_vol_90d: float | None = None
    veto: VetoStatus = "none"


@dataclass(frozen=True)
class Relaxation:
    """A cap the book could not satisfy as requested, and what it was relaxed to."""

    cap: Literal["name", "sector"]
    requested: float
    effective: float
    reason: str


@dataclass(frozen=True)
class PinNote:
    """A pin the book refused (HARD veto, unknown ticker) or flagged (SOFT veto), with the reason."""

    ticker: str
    reason: str


@dataclass(frozen=True)
class BookResult:
    """The book and everything that was decided to build it. Nothing here is silent."""

    weights: dict[int, float]  # asset_id -> weight, in rank order (pins first)
    scheme: str
    requested_n: int
    n_held: int  # min(n, eligible names); the N the band and the fullness rule use
    shortfall: int  # requested_n - n_held; the book is never padded
    max_name_weight: float  # effective
    max_sector_weight: float  # effective
    relaxations: tuple[Relaxation, ...]
    refused_pins: tuple[PinNote, ...]
    flagged_pins: tuple[PinNote, ...]
    overflow_tickers: tuple[str, ...]  # held past a full sector because nothing else was left


def _project_sum(t: Sequence[float], lo: float, hi: float, total: float) -> list[float]:
    """Euclidean projection of *t* onto ``{lo <= w <= hi, sum(w) = total}``, exactly.

    The solution is ``w_i = clip(t_i + lam, lo, hi)`` for the one shift ``lam`` that makes the sum
    *total*. ``sum(clip(t_i + lam))`` is piecewise linear and non-decreasing in ``lam``, so one sweep
    over its breakpoints (where a name enters or leaves the band) finds the segment holding *total*
    and solves that linear piece directly: finite, no tolerance, no iteration count to tune.
    """
    n = len(t)
    if n == 0:
        return []
    lo_sum, hi_sum = n * lo, n * hi
    if total <= lo_sum:
        return [lo] * n
    if total >= hi_sum:
        return [hi] * n
    events = sorted([(lo - x, 1) for x in t] + [(hi - x, -1) for x in t])
    value, slope, at = lo_sum, 0, events[0][0]
    lam = at
    for point, delta in events:
        reached = value + slope * (point - at)
        if slope > 0 and reached >= total:
            lam = at + (total - value) / slope
            break
        value, at, slope = reached, point, slope + delta
    else:  # pragma: no cover -- total < hi_sum is reached inside the sweep
        lam = at
    return [min(hi, max(lo, x + lam)) for x in t]


def _project_book(
    t: Sequence[float], groups: Sequence[int], lo: float, hi: float, sector_cap: float
) -> list[float]:
    """Euclidean projection of the target *t* onto ``{lo <= w <= hi, sum = 1, sector sum <= cap}``.

    Exact active-set water-filling. Project onto the box-and-sum set (``_project_sum``); every sector
    that ends over its cap is fixed at the cap and projected inside it (sum = cap); the rest are
    re-projected onto what is left, and the check repeats. Fixing a sector at its cap pushes weight to
    the others, so a sector over its cap now stays over it later: the fixed set only grows, and at most
    one round per sector is needed. Requires a feasible problem (``_feasible_sector_cap``).
    """
    n = len(t)
    members: dict[int, list[int]] = {}
    for i, g in enumerate(groups):
        members.setdefault(g, []).append(i)
    w = [0.0] * n
    fixed: set[int] = set()
    for _ in range(len(members) + 1):
        free = [i for g, idx in members.items() if g not in fixed for i in idx]
        left = 1.0 - sector_cap * len(fixed)
        lo_f, hi_f = lo * len(free), hi * len(free)
        if free:  # float dust at an exactly tight problem; a real gap is a bug upstream
            if not lo_f - 1e-9 <= left <= hi_f + 1e-9:
                raise ValueError(f"infeasible projection: {left} outside [{lo_f}, {hi_f}]")
            left = min(hi_f, max(lo_f, left))
        for i, wi in zip(free, _project_sum([t[i] for i in free], lo, hi, left), strict=True):
            w[i] = wi
        over = [
            g
            for g, idx in members.items()
            if g not in fixed and sum(w[i] for i in idx) > sector_cap + _EPS
        ]
        if not over:
            return w
        for g in over:
            idx = members[g]
            for i, wi in zip(
                idx, _project_sum([t[i] for i in idx], lo, hi, sector_cap), strict=True
            ):
                w[i] = wi
            fixed.add(g)
    return w  # pragma: no cover -- the fixed set grows every round, so the loop always returns


def _feasible_sector_cap(counts: Sequence[int], lo: float, hi: float, requested: float) -> float:
    """The smallest sector cap >= *requested* under which a book exists.

    A sector holding k names can carry any weight in ``[k*lo, k*hi]``, so a cap *c* is feasible iff
    every ``k*lo <= c`` (the band floor fits) and ``sum(min(c, k*hi)) >= 1`` (the sectors can reach 1).
    The second is solved exactly on the sorted sector capacities.
    """
    floor_bound = max((k * lo for k in counts), default=0.0)
    capacities = sorted(k * hi for k in counts)
    reach = capacities[-1] if capacities else 0.0
    placed = 0.0
    for idx, cap in enumerate(capacities):
        c = (1.0 - placed) / (len(capacities) - idx)
        if c <= cap:
            reach = c
            break
        placed += cap
    return max(requested, floor_bound, reach)


def _target(book: Sequence[BookCandidate], scheme: str, lo: float, hi: float) -> list[float]:
    """The unconstrained target the projection starts from."""
    n = len(book)
    scores = [c.blended_score for c in book]
    if scheme == "score_tilt":
        low, high = min(scores), max(scores)
        if high - low <= 0.0:
            return [1.0 / n] * n
        return [lo + (s - low) / (high - low) * (hi - lo) for s in scores]
    if scheme == "equal":
        raw = [1.0] * n
    elif scheme == "inverse_vol":
        inv = [1.0 / c.realized_vol_90d if c.realized_vol_90d else None for c in book]
        known = [v for v in inv if v is not None]
        neutral = sum(known) / len(known) if known else 1.0  # no usable vol: an average name
        raw = [neutral if v is None else v for v in inv]
    else:  # score_proportional: the lowest score maps to a small positive weight
        raw = [(s - min(scores)) + 1.0 for s in scores]
    total = sum(raw)
    return [r / total for r in raw]


@dataclass(frozen=True)
class _Prefs:
    """The user's preferences, normalized (trimmed, case-folded) once."""

    pins: frozenset[str]
    exclude: frozenset[str]
    exclude_sectors: frozenset[str]
    only: frozenset[str] | None

    def barred(self, c: BookCandidate) -> bool:
        """Is *c* out by ticker, excluded sector or ``only_sectors``?"""
        sector = None if c.sector is None else _key(c.sector)
        if sector is not None and sector in self.exclude_sectors:
            return True
        if self.only is not None and (sector is None or sector not in self.only):
            return True
        return _key(c.ticker) in self.exclude


@dataclass(frozen=True)
class _Selection:
    chosen: list[BookCandidate]  # pins first, then rank order
    n_held: int
    overflow: list[BookCandidate]
    refused: list[PinNote]
    flagged: list[PinNote]


def _key(text: str) -> str:
    return text.strip().casefold()


def _rank_key(c: BookCandidate) -> tuple[float, str, int]:
    return (-c.blended_score, c.ticker, c.asset_id)  # ties by ticker: input order never matters


def _resolve_pins(
    ranked: Sequence[BookCandidate], prefs: _Prefs
) -> tuple[set[int], list[PinNote], list[PinNote]]:
    """``(held pin ids, refused, flagged)``: HARD refused with a reason, SOFT held and flagged."""
    by_ticker = {_key(c.ticker): c for c in ranked}
    held: set[int] = set()
    refused: list[PinNote] = []
    flagged: list[PinNote] = []
    for ticker in sorted(prefs.pins):
        c = by_ticker.get(ticker)
        if c is None:
            refused.append(
                PinNote(ticker, "not among the candidates (unknown, unscored or filtered)")
            )
        elif prefs.barred(c):
            raise ValueError(f"cannot pin {c.ticker}: its sector {c.sector!r} is excluded")
        elif c.veto == "HARD":
            refused.append(PinNote(c.ticker, "HARD veto: a HARD-vetoed name is never held"))
        else:
            held.add(c.asset_id)
            if c.veto == "SOFT":
                flagged.append(PinNote(c.ticker, "SOFT veto: held because it was pinned"))
    return held, refused, flagged


def _fill(
    eligible: Sequence[BookCandidate], held_pins: set[int], n_held: int, threshold: int
) -> tuple[list[BookCandidate], list[BookCandidate]]:
    """``(chosen, overflow)``: pins, then the rest by rank skipping any name whose sector is full;
    if that leaves the book short, the skipped names fill it by rank (the overflow)."""
    chosen = [c for c in eligible if c.asset_id in held_pins]
    in_sector: dict[str | None, int] = {}
    for c in chosen:
        in_sector[c.sector] = in_sector.get(c.sector, 0) + 1
    skipped: list[BookCandidate] = []
    for c in (c for c in eligible if c.asset_id not in held_pins):
        if len(chosen) >= n_held:
            break
        if in_sector.get(c.sector, 0) >= threshold:
            skipped.append(c)  # its sector is full: take the next-ranked name instead
            continue
        chosen.append(c)
        in_sector[c.sector] = in_sector.get(c.sector, 0) + 1
    overflow = skipped[: max(0, n_held - len(chosen))]
    chosen.extend(overflow)
    chosen.sort(key=lambda c: (c.asset_id not in held_pins, *_rank_key(c)))
    return chosen, overflow


def _select(
    candidates: Sequence[BookCandidate], n: int, sector_cap: float, prefs: _Prefs
) -> _Selection:
    """Steps 1-5 of ``build_book``: who is held, before any weight is computed."""
    ranked = sorted(candidates, key=_rank_key)
    held_pins, refused, flagged = _resolve_pins(ranked, prefs)
    if len(held_pins) > n:
        raise ValueError(f"{len(held_pins)} pins do not fit in n = {n}")
    eligible = [c for c in ranked if c.veto != "HARD" and not prefs.barred(c)]
    n_held = min(n, len(eligible))
    threshold = max(1, math.floor(sector_cap * n_held + _FULL_SLACK))
    chosen, overflow = _fill(eligible, held_pins, n_held, threshold)
    return _Selection(chosen, n_held, overflow, refused, flagged)


@dataclass(frozen=True)
class _Caps:
    floor: float  # band floor (0 for the legacy schemes)
    name: float  # effective name cap
    sector: float  # effective sector cap
    relaxations: tuple[Relaxation, ...]


def _resolve_caps(
    sel: _Selection, scheme: str, max_name_weight: float | None, max_sector_weight: float
) -> _Caps:
    """The effective caps. The name cap goes first: it fixes the band the sector cap is checked
    against."""
    n_held = sel.n_held
    relaxations: list[Relaxation] = []
    lo = _TILT_FLOOR / n_held if scheme == "score_tilt" else 0.0
    requested = max_name_weight if max_name_weight is not None else _name_default(scheme, n_held)
    name_cap = requested
    if requested < 1.0 / n_held - _EPS:
        name_cap = 1.0 / n_held
        why = f"{n_held} names at most {requested:.6g} each cannot sum to 1; relaxed to 1/{n_held}"
        relaxations.append(Relaxation("name", requested, name_cap, why))
    name_cap = min(name_cap, 1.0)

    counts: dict[str | None, int] = {}
    for c in sel.chosen:
        counts[c.sector] = counts.get(c.sector, 0) + 1
    sector_cap = _feasible_sector_cap(list(counts.values()), lo, name_cap, max_sector_weight)
    if sector_cap <= max_sector_weight + _EPS:
        return _Caps(lo, name_cap, max_sector_weight, tuple(relaxations))
    why = (
        f"{len(counts)} sector(s) over {n_held} names, band [{lo:.6g}, {name_cap:.6g}]: no book sums "
        f"to 1 with every sector at most {max_sector_weight:.6g}; relaxed to the smallest feasible value"
    )
    if sel.overflow:
        why = f"the sector-aware fill ran out of names outside full sectors; {why}"
    relaxations.append(Relaxation("sector", max_sector_weight, sector_cap, why))
    return _Caps(lo, name_cap, sector_cap, tuple(relaxations))


def _validate(
    candidates: Sequence[BookCandidate],
    n: int,
    scheme: str,
    max_name_weight: float | None,
    max_sector_weight: float,
) -> None:
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    if scheme not in SCHEMES:
        raise ValueError(f"unknown weight scheme {scheme!r}; expected one of {SCHEMES}")
    if not 0.0 < max_sector_weight <= 1.0:
        raise ValueError(f"max_sector_weight must be in (0, 1], got {max_sector_weight}")
    if max_name_weight is not None and not 0.0 < max_name_weight <= 1.0:
        raise ValueError(f"max_name_weight must be in (0, 1], got {max_name_weight}")
    ids = {c.asset_id for c in candidates}
    tickers = {_key(c.ticker) for c in candidates}
    if len(ids) != len(candidates) or len(tickers) != len(candidates):
        raise ValueError("candidates must have unique asset ids and tickers")
    if any(not math.isfinite(c.blended_score) for c in candidates):
        raise ValueError("every blended score must be finite")


def build_book(  # noqa: PLR0913 - keyword-only inputs, one per T-134 decision
    candidates: Sequence[BookCandidate],
    *,
    n: int,
    scheme: str = "score_tilt",
    max_name_weight: float | None = None,
    max_sector_weight: float = DEFAULT_SECTOR_CAP,
    pins: Iterable[str] = (),
    exclude: Iterable[str] = (),
    exclude_sectors: Iterable[str] = (),
    only_sectors: Iterable[str] | None = None,
) -> BookResult:
    """The target book for *n* names: pure, deterministic, always fully invested (T-134, FR-007).

    **Selection**, in this order: (1) drop excluded tickers and sectors, or keep only
    *only_sectors* (excluding a pinned ticker is a ``ValueError``); (2) HARD-vetoed candidates are
    never held, a HARD-vetoed pin is refused with its reason, a SOFT-vetoed pin is held and flagged;
    (3) pins first, then the rest, each by blended score descending (ties by ticker, so every run and
    every input order gives the same book); (4) fill by the sector-aware rule — a sector is *full* at
    ``max(1, floor(sector cap x n_held))`` names, ``n_held = min(n, eligible)`` — and if fullness keeps
    the book short of ``n_held``, continue by rank ignoring it (``overflow_tickers``) and let the
    sector cap relax to the smallest feasible value; (5) fewer eligible names than *n*: hold them all
    and record the ``shortfall``, never pad.

    **Weights.** ``score_tilt``: the name cap is ``1.5/n_held`` (an explicit *max_name_weight* wins; one
    below ``1/n_held`` cannot sum to 1 and is relaxed to it, recorded) and the band is
    ``[0.5/n_held, name cap]``; scores map linearly onto ``[0.5/n_held, 1.5/n_held]`` (all equal ->
    equal weights) and that target is projected exactly onto {sum = 1, band, sector sums <= cap}
    (``_project_book``). Where no bound binds the projection is a common shift, so weights stay linear
    in the score. ``equal``, ``score_proportional`` and ``inverse_vol`` keep their raw weights as the
    target and go through the same projection with floor 0 and name cap *max_name_weight* (else 0.10).
    Pins obey the band and the caps by the weights; they count toward the fullness of their sector but
    are never skipped for it.
    """
    _validate(candidates, n, scheme, max_name_weight, max_sector_weight)
    prefs = _Prefs(
        frozenset(map(_key, pins)),
        frozenset(map(_key, exclude)),
        frozenset(map(_key, exclude_sectors)),
        None if only_sectors is None else frozenset(map(_key, only_sectors)),
    )
    if prefs.only is not None and not prefs.only:
        raise ValueError("only_sectors is empty; pass None to allow every sector")
    if prefs.pins & prefs.exclude:
        raise ValueError(f"cannot both pin and exclude: {sorted(prefs.pins & prefs.exclude)}")

    sel = _select(candidates, n, max_sector_weight, prefs)
    if not sel.chosen:  # nothing eligible: an empty book, the whole of n is the shortfall
        name_cap = max_name_weight or _name_default(scheme, n)
        return BookResult(
            weights={},
            scheme=scheme,
            requested_n=n,
            n_held=0,
            shortfall=n,
            max_name_weight=name_cap,
            max_sector_weight=max_sector_weight,
            relaxations=(),
            refused_pins=tuple(sel.refused),
            flagged_pins=tuple(sel.flagged),
            overflow_tickers=(),
        )
    caps = _resolve_caps(sel, scheme, max_name_weight, max_sector_weight)
    groups: dict[str | None, int] = {}
    for c in sel.chosen:
        groups.setdefault(c.sector, len(groups))
    target_hi = _TILT_CEILING / sel.n_held if scheme == "score_tilt" else caps.name
    target = _target(sel.chosen, scheme, caps.floor, target_hi)
    weights = _project_book(
        target, [groups[c.sector] for c in sel.chosen], caps.floor, caps.name, caps.sector
    )
    return BookResult(
        weights={c.asset_id: w for c, w in zip(sel.chosen, weights, strict=True)},
        scheme=scheme,
        requested_n=n,
        n_held=sel.n_held,
        shortfall=n - sel.n_held,
        max_name_weight=caps.name,
        max_sector_weight=caps.sector,
        relaxations=caps.relaxations,
        refused_pins=tuple(sel.refused),
        flagged_pins=tuple(sel.flagged),
        overflow_tickers=tuple(c.ticker for c in sel.overflow),
    )


def _name_default(scheme: str, n_held: int) -> float:
    return _TILT_CEILING / n_held if scheme == "score_tilt" else _LEGACY_NAME_CAP
