"""As-of ticker history (T-153 part 2, checklist L-11 / APP-10a): which symbol a company's
line carried when.

A company is its CIK, never its ticker (APP-10a; ``T-149``). Over the backtest window a
company may change its ticker -- Facebook's FB became META, FleetCor's FLT became Corpay's
CPAY -- and it stays **one asset**. Identity is therefore one asset per ``(CIK, line)``: a
*line* is a maximal chain of one CIK's symbols where each symbol's stint begins the day after
the previous one ends (``valid_from == valid_to + 1``; ``valid_to`` is the last day of
membership, T-153 part 1). Share classes are separate lines of one CIK that *coexist* on the
same day (GOOGL/GOOG, FOXA/FOX, NWSA/NWS) and stay separate assets.

``data/ticker_history.csv`` is the source of truth: one row per symbol, each citing the notice
that dates the change (L-11: an S&P DJI press release or the company's own notice), and the
line's ``canonical_symbol`` (its newest symbol). The file is seeded into the ``ticker_history``
table on every :func:`kg_schema.ensure`; ``universe_history.db`` is only a cross-check in the
tests, never the source.

Because a ticker is reassigned (the symbol ``FB`` is ProShares Trust, CIK 0001174610, today),
resolution must compare the **CIK**: :func:`kg_schema.queries.resolve_asset_ids` maps a member
by its CIK, and a matched asset whose CIK differs is left missing rather than merged.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from portfolio_common.db import Database

from .identity import normalize_cik

HISTORY_CSV = Path(__file__).resolve().parent / "data" / "ticker_history.csv"


@dataclass(frozen=True)
class HistoryRow:
    """One symbol's stint on a CIK's line, with the notice that dates it."""

    cik: str
    symbol: str
    valid_from: str
    valid_to: str | None
    canonical_symbol: str
    evidence_url: str
    evidence_date: str
    rules: str


@dataclass(frozen=True)
class Adoption:
    """A renamed line adopted in place: an existing asset retitled (T-153 part 2, C4)."""

    old_ticker: str
    new_ticker: str
    asset_id: int
    cik: str


@dataclass(frozen=True)
class SyncResult:
    """What a ``sync_universe`` wrote: the members it touched and any adoptions (C4)."""

    members: int
    adoptions: list[Adoption]


def _records(path: Path) -> list[str]:
    """The file's data lines: the CSV header plus records, without the ``#`` comments."""
    text = path.read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip() and not line.startswith("#")]


def read_history(path: Path = HISTORY_CSV) -> list[HistoryRow]:
    """Every row of the versioned ticker history, each validated for its citation (L-11)."""
    rows: list[HistoryRow] = []
    for row in csv.DictReader(_records(path)):
        for key in ("cik", "symbol", "valid_from", "canonical_symbol", "evidence_url"):
            if not (row.get(key) or "").strip():
                raise ValueError(f"ticker_history row without its {key}: {row}")
        urls = [part.strip() for part in row["evidence_url"].split(";") if part.strip()]
        dates = [
            part.strip() for part in (row.get("evidence_date") or "").split(";") if part.strip()
        ]
        if not urls or len(urls) != len(dates):
            raise ValueError(f"ticker_history row needs one citation date per evidence URL: {row}")
        rows.append(
            HistoryRow(
                cik=str(normalize_cik(row["cik"])),
                symbol=row["symbol"].strip().upper(),
                valid_from=row["valid_from"].strip(),
                valid_to=(row["valid_to"].strip() or None) if row.get("valid_to") else None,
                canonical_symbol=row["canonical_symbol"].strip().upper(),
                evidence_url=" ; ".join(urls),
                evidence_date=" ; ".join(dates),
                rules=(row.get("rules") or "").strip(),
            )
        )
    return rows


def seed(db: Database, path: Path = HISTORY_CSV) -> int:
    """Insert the rows the database does not hold yet; returns how many were new."""
    if not db.relation_exists("ticker_history"):
        return 0
    new = 0
    for r in read_history(path):
        new += db.execute(
            "INSERT OR IGNORE INTO ticker_history (cik, symbol, valid_from, valid_to, "
            "canonical_symbol, evidence_url, evidence_date, rules) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r.cik,
                r.symbol,
                r.valid_from,
                r.valid_to,
                r.canonical_symbol,
                r.evidence_url,
                r.evidence_date,
                r.rules,
            ),
        ).rowcount
    db.commit()
    return new


def ensure_seeded(db: Database, path: Path = HISTORY_CSV) -> int:
    """Seed the table when the database has it (a no-op on a partial schema)."""
    return seed(db, path)


def canonical_symbol(db: Database, cik: object, symbol: str) -> str | None:
    """The line's canonical symbol for a member's ``(CIK, symbol)``, or ``None`` when the CIK
    has no recorded line for that symbol -- an unknown company, or a reused ticker (the symbol
    a *different* CIK trades under today)."""
    own = normalize_cik(cik)
    if own is None or not db.relation_exists("ticker_history"):
        return None
    row = db.execute(
        "SELECT canonical_symbol FROM ticker_history WHERE cik = ? AND symbol = ? "
        "ORDER BY valid_from DESC LIMIT 1",
        (own, symbol.strip().upper()),
    ).fetchone()
    return str(row["canonical_symbol"]) if row else None


def symbol_line(db: Database, symbol: str, as_of: str) -> tuple[str, str] | None:
    """``(CIK, canonical symbol)`` of the line *symbol* named on *as_of*, or ``None``.

    Date-aware on purpose: ``FB`` named on a 2022 date is Facebook's line, but the same symbol
    after the rename belongs to ProShares Trust (a different CIK), which this table does not
    name -- so a caller never merges the two."""
    if not db.relation_exists("ticker_history"):
        return None
    row = db.execute(
        "SELECT cik, canonical_symbol FROM ticker_history WHERE symbol = ? "
        "AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?) "
        "ORDER BY valid_from DESC LIMIT 1",
        (symbol.strip().upper(), as_of, as_of),
    ).fetchone()
    return (str(row["cik"]), str(row["canonical_symbol"])) if row else None


def canonical_for_member(
    db: Database, *, symbol: str, cik: object, as_of: str | None = None
) -> str:
    """The symbol a member is stored/fetched under: its line's canonical, else *symbol* itself.

    A member with a CIK is resolved by that CIK (:func:`canonical_symbol`). A member with no
    CIK keeps the symbol as given, unless *as_of* is passed and the symbol names a recorded line
    on that day -- then the line's canonical, a bare ticker match without the CIK check."""
    if normalize_cik(cik) is not None:
        return canonical_symbol(db, cik, symbol) or symbol
    if as_of is not None:
        line = symbol_line(db, symbol, as_of)
        if line is not None:
            return line[1]
    return symbol


def line_symbols(db: Database, cik: object, canonical: str) -> set[str]:
    """Every symbol of the line whose canonical symbol is *canonical* for *cik*."""
    own = normalize_cik(cik)
    if own is None or not db.relation_exists("ticker_history"):
        return set()
    return {
        str(r["symbol"])
        for r in db.execute(
            "SELECT symbol FROM ticker_history WHERE cik = ? AND canonical_symbol = ?",
            (own, canonical.strip().upper()),
        )
    }


class TickerConflict(RuntimeError):
    """A member would give a CIK a second asset with no recorded line linking them (T-153 part 2).

    Raised before anything is written, the way :class:`kg_schema.identity.CikConflict` is: a
    rename the ``ticker_history`` file does not declare is refused rather than silently split
    into two assets (the ``m012`` triggers would allow it, as if it were a share class)."""


@dataclass(frozen=True)
class MemberAsset:
    """Where a member's symbol belongs: refresh an existing asset, insert a new one, or adopt.

    ``ticker`` is the canonical symbol to store/fetch under. ``action`` is ``"refresh"`` (the
    line's asset already exists under *ticker*), ``"insert"`` (a new asset, a share class or a
    first sighting) or ``"adopt"`` (retitle the *different* existing asset ``asset_id``, which
    currently carries ``adopt_from``, to *ticker* -- a recorded rename the database predates)."""

    symbol: str
    cik: str | None
    ticker: str
    action: str
    asset_id: int | None = None
    adopt_from: str | None = None


@dataclass(frozen=True)
class _Assets:
    """The existing ``assets`` rows, indexed for planning (one read per ``sync_universe``)."""

    by_ticker: dict[str, int]  # UPPER(ticker) -> id
    cik_by_id: dict[int, str | None]  # id -> normalized CIK
    by_cik: dict[str, list[tuple[int, str]]]  # CIK -> [(id, ticker)]


def _load_assets(db: Database) -> _Assets:
    rows = list(db.execute("SELECT id, ticker, cik FROM assets"))
    by_cik: dict[str, list[tuple[int, str]]] = {}
    for r in rows:
        own = normalize_cik(r["cik"])
        if own is not None:
            by_cik.setdefault(own, []).append((int(r["id"]), str(r["ticker"])))
    return _Assets(
        by_ticker={str(r["ticker"]).upper(): int(r["id"]) for r in rows},
        cik_by_id={int(r["id"]): normalize_cik(r["cik"]) for r in rows},
        by_cik=by_cik,
    )


def _line_asset(by_cik: dict[str, list[tuple[int, str]]], own: str, canonical: str) -> int | None:
    return next(
        (aid for aid, ticker in by_cik.get(own, []) if ticker.upper() == canonical.upper()),
        None,
    )


def _adopt_or_insert(
    db: Database, symbol: str, own: str, canonical: str, mine: list
) -> MemberAsset:
    adoptable = [
        (aid, t)
        for aid, t in mine
        if t.upper() in line_symbols(db, own, canonical) and t.upper() != canonical.upper()
    ]
    if len(adoptable) == 1:
        aid, old = adoptable[0]
        return MemberAsset(symbol, own, canonical, "adopt", aid, old)
    if not adoptable:
        return MemberAsset(symbol, own, canonical, "insert")
    raise TickerConflict(
        f"{symbol} (CIK {own}) could adopt several assets of that CIK "
        f"({', '.join(t for _, t in adoptable)}): which one is not decidable (T-153 part 2)"
    )


def _plan_one(
    db: Database, symbol: str, cik: object, assets: _Assets, batch: Counter
) -> MemberAsset:
    own = normalize_cik(cik)
    if own is None:  # no CIK: today's behaviour, a bare ticker upsert
        asset_id = assets.by_ticker.get(symbol.upper())
        return MemberAsset(symbol, None, symbol, "refresh" if asset_id else "insert", asset_id)
    canonical = canonical_symbol(db, own, symbol) or symbol
    line_asset = _line_asset(assets.by_cik, own, canonical)
    if line_asset is not None:
        return MemberAsset(symbol, own, canonical, "refresh", line_asset)
    taken = assets.by_ticker.get(canonical.upper())
    if taken is not None and assets.cik_by_id.get(taken) not in (None, own):
        raise TickerConflict(
            f"{symbol} (CIK {own}) resolves to {canonical}, already asset {taken} of CIK "
            f"{assets.cik_by_id.get(taken)}: a reused ticker is never merged (T-153 part 2)"
        )
    mine = assets.by_cik.get(own, [])
    if batch.get(own, 0) > 1:  # a share class is a member alongside it on the same date
        return MemberAsset(symbol, own, canonical, "insert")
    if canonical_symbol(db, own, symbol) is not None:  # a recorded line: adopt or first sighting
        return _adopt_or_insert(db, symbol, own, canonical, mine)
    if mine:
        aid, existing = mine[0]
        raise TickerConflict(
            f"{symbol} (CIK {own}) would be a second asset for that CIK: asset {aid} ({existing}) "
            f"already exists and no recorded rename links them (T-153 part 2)"
        )
    return MemberAsset(symbol, own, canonical, "insert")


def plan_members(db: Database, members: list[tuple[str, object]]) -> list[MemberAsset]:
    """Classify each member into the asset it belongs to, refusing an undeclared split.

    *members* are ``(symbol, cik)`` pairs of one dated universe. A member whose CIK already has
    an asset under a *different* symbol is: a **share class** (another member of the same CIK is
    in this batch) -> insert, as today; a **recorded rename** (the file names the symbol) ->
    adopt; otherwise -> :class:`TickerConflict`. Nothing is written here, so a conflict leaves
    the database untouched."""
    assets = _load_assets(db)
    batch = Counter(own for _, cik in members if (own := normalize_cik(cik)) is not None)
    return [_plan_one(db, symbol, cik, assets, batch) for symbol, cik in members]
