"""Filing identity (T-149, checklist APP-10a): a company is its CIK, never its ticker.

Three guarantees that keep a company's metrics from ever reading another company's filings:

* ``assets.cik`` is write-once. :func:`refuse_cik_changes` stops a universe sync that would hand an
  existing ticker a different CIK (a reused ticker), and the sync never overwrites a non-null CIK.
* Every asset that has filings has a CIK, and one accession is attached to several assets only when
  they share it (the share classes of one issuer). :data:`kg_schema.ddl.IDENTITY_TRIGGERS` abort a
  write that breaks either; :func:`violations` / :func:`require` check rows written before them.
* A successor registrant keeps its predecessor's filings (APP-10b, XOM): ``cik_succession`` (seeded from
  ``data/cik_successions.csv``, each row citing the filing that establishes it) names the predecessors of an
  asset's CIK; :func:`cik_chain` is the CIKs its filings are fetched under, and a filing's own CIK
  (``sec_filings.cik``) must be one of them.
* The triggers are created by :func:`kg_schema.ensure` and migration ``m012``.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from portfolio_common.db import Database

from .ddl import IDENTITY_TABLES, IDENTITY_TRIGGERS
from .queries import UniverseMember

_CIK_DIGITS = 10
SUCCESSIONS_CSV = Path(__file__).resolve().parent / "data" / "cik_successions.csv"

# Fully literal SQL (constitution Code & Git #10): no identifier is ever built into a statement.
_DROP_TRIGGERS = (
    "DROP TRIGGER IF EXISTS trg_sf_cik_insert",
    "DROP TRIGGER IF EXISTS trg_sf_cik_update",
    "DROP TRIGGER IF EXISTS trg_sf_cross_cik_insert",
    "DROP TRIGGER IF EXISTS trg_sf_cross_cik_update",
    "DROP TRIGGER IF EXISTS trg_sf_chain_insert",
    "DROP TRIGGER IF EXISTS trg_sf_chain_update",
    "DROP TRIGGER IF EXISTS trg_assets_cik_keep",
    "DROP TRIGGER IF EXISTS trg_assets_cik_share",
)


class CikConflict(RuntimeError):
    """A ticker arrived with a CIK other than the one stored for it."""


class IdentityViolation(RuntimeError):
    """Rows hold filings of an asset with no CIK, or one accession on assets of different CIKs."""


def normalize_cik(value: object) -> str | None:
    """*value* as the 10-digit string the SEC and the gateway use, ``None`` for an empty one.

    ``320193``, ``"320193"`` and ``"0000320193"`` are one CIK. Anything that is not a number of at
    most ten digits is refused (``ValueError``) rather than passed on as an identifier."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if not text.isdigit() or len(text) > _CIK_DIGITS:
        raise ValueError(f"not a CIK: {value!r}")
    return text.zfill(_CIK_DIGITS)


def refuse_cik_changes(db: Database, members: Iterable[UniverseMember]) -> None:
    """Raise :class:`CikConflict` when a member's ticker is stored with another CIK.

    Checked for every member before anything is written, and the message names each ticker, its
    stored CIK and the incoming one. An empty incoming CIK is no conflict (the sync keeps the stored
    one); neither is a spelling of the same number."""
    stored = {
        str(r["ticker"]): normalize_cik(r["cik"])
        for r in db.execute("SELECT ticker, cik FROM assets WHERE cik IS NOT NULL")
    }
    clashes: list[str] = []
    for m in members:
        have, incoming = stored.get(m.symbol), normalize_cik(m.cik)
        if have is not None and incoming is not None and have != incoming:
            clashes.append(f"{m.symbol}: stored CIK {have}, incoming CIK {incoming}")
    if clashes:
        raise CikConflict(
            "an asset's CIK never changes silently (T-149); refusing the universe sync -- "
            + "; ".join(clashes)
        )


def _ready(db: Database) -> bool:
    return (
        all(db.relation_exists(t) for t in IDENTITY_TABLES)
        and "cik" in db.table_columns("assets")
        and {"accession_number", "cik"} <= set(db.table_columns("sec_filings"))
    )


def read_successions(path: Path = SUCCESSIONS_CSV) -> list[dict[str, str]]:
    """The versioned succession table: successor, predecessor, effective date and the evidence."""
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        row["successor_cik"] = str(normalize_cik(row["successor_cik"]))
        row["predecessor_cik"] = str(normalize_cik(row["predecessor_cik"]))
        if not all(
            row[k]
            for k in ("effective_date", "evidence_form", "evidence_accession", "evidence_quote")
        ):
            raise ValueError(f"cik_succession row without its evidence: {row}")
    return rows


def seed_successions(db: Database, path: Path = SUCCESSIONS_CSV) -> int:
    """Insert the table's rows that the database does not hold yet; returns how many were new."""
    if not db.relation_exists("cik_succession"):
        return 0
    new = 0
    for r in read_successions(path):
        new += db.execute(
            "INSERT OR IGNORE INTO cik_succession (successor_cik, predecessor_cik, effective_date, "
            "evidence_form, evidence_accession, evidence_quote) VALUES (?, ?, ?, ?, ?, ?)",
            (
                r["successor_cik"],
                r["predecessor_cik"],
                r["effective_date"],
                r["evidence_form"],
                r["evidence_accession"],
                r["evidence_quote"],
            ),
        ).rowcount
    db.commit()
    return new


def cik_chain(db: Database, cik: object) -> tuple[str, ...]:
    """The CIKs *cik*'s filings are fetched under: itself first, then its recorded predecessors
    (most recent succession first). A company with no recorded succession has just itself."""
    own = normalize_cik(cik)
    if own is None:
        return ()
    if not db.relation_exists("cik_succession"):
        return (own,)
    rows = db.execute(
        "SELECT predecessor_cik FROM cik_succession WHERE successor_cik = ? "
        "ORDER BY effective_date DESC, predecessor_cik",
        (own,),
    ).fetchall()
    return (own, *(str(r["predecessor_cik"]) for r in rows))


def ensure_triggers(db: Database) -> None:
    """Seed the succession table and create the T-149 guards once ``assets`` (with ``cik``),
    ``sec_filings`` (with ``cik``) and ``cik_succession`` exist."""
    if _ready(db):
        seed_successions(db)
        db.create_schema(IDENTITY_TRIGGERS)
        db.commit()


def drop_triggers(db: Database) -> None:
    for statement in _DROP_TRIGGERS:
        db.execute(statement)


@dataclass
class Violations:
    """Stored rows the T-149 triggers would have refused."""

    no_cik: list[str] = field(default_factory=list)  # tickers with filings and no CIK
    cross_cik: list[str] = field(default_factory=list)  # "accession: TICKER (cik), TICKER (cik)"
    outside_chain: list[str] = field(default_factory=list)  # "TICKER accession (filing cik)"

    def __bool__(self) -> bool:
        return bool(self.no_cik or self.cross_cik or self.outside_chain)

    def describe(self) -> str:
        parts = []
        if self.no_cik:
            parts.append(f"filings of assets with no CIK: {', '.join(self.no_cik)}")
        if self.cross_cik:
            parts.append(f"accessions on assets of different CIKs: {'; '.join(self.cross_cik)}")
        if self.outside_chain:
            parts.append(
                f"filings of a CIK outside their asset's chain: {'; '.join(self.outside_chain)}"
            )
        return " | ".join(parts)


def violations(db: Database) -> Violations:
    """The stored rows that break the invariant (a trigger never checks the rows already there)."""
    found = Violations()
    if not _ready(db):
        return found
    found.no_cik = [
        str(r["ticker"])
        for r in db.execute(
            """
            SELECT DISTINCT a.ticker AS ticker FROM sec_filings f JOIN assets a ON a.id = f.asset_id
            WHERE COALESCE(a.cik, '') = '' ORDER BY a.ticker
            """
        )
    ]
    holders: dict[str, list[str]] = {}
    for r in db.execute(
        """
        SELECT f.accession_number AS accession_number, a.ticker AS ticker, COALESCE(a.cik, '') AS cik
        FROM sec_filings f JOIN assets a ON a.id = f.asset_id
        WHERE f.accession_number IN (
            SELECT g.accession_number FROM sec_filings g JOIN assets b ON b.id = g.asset_id
            WHERE g.accession_number IS NOT NULL
            GROUP BY g.accession_number HAVING COUNT(DISTINCT COALESCE(b.cik, '')) > 1
        )
        GROUP BY f.accession_number, a.ticker
        ORDER BY f.accession_number, a.ticker
        """
    ):
        holders.setdefault(str(r["accession_number"]), []).append(f"{r['ticker']} ({r['cik']})")
    found.cross_cik = [f"{accession}: {', '.join(who)}" for accession, who in holders.items()]
    found.outside_chain = [
        f"{r['ticker']} {r['accession_number']} ({r['cik']})"
        for r in db.execute(
            """
            SELECT a.ticker AS ticker, f.accession_number AS accession_number, f.cik AS cik
            FROM sec_filings f JOIN assets a ON a.id = f.asset_id
            WHERE f.cik IS NOT NULL AND f.cik IS NOT a.cik AND NOT EXISTS (
                SELECT 1 FROM cik_succession s
                WHERE s.successor_cik = a.cik AND s.predecessor_cik = f.cik)
            ORDER BY a.ticker, f.accession_number
            """
        )
    ]
    return found
    found.no_cik = [
        str(r["ticker"])
        for r in db.execute(
            """
            SELECT DISTINCT a.ticker AS ticker FROM sec_filings f JOIN assets a ON a.id = f.asset_id
            WHERE COALESCE(a.cik, '') = '' ORDER BY a.ticker
            """
        )
    ]
    found.cross_cik = [
        f"{r['accession_number']}: {r['members']}"
        for r in db.execute(
            """
            SELECT f.accession_number AS accession_number,
                   GROUP_CONCAT(DISTINCT a.ticker || ' (' || COALESCE(a.cik, '') || ')') AS members
            FROM sec_filings f JOIN assets a ON a.id = f.asset_id
            WHERE f.accession_number IS NOT NULL
            GROUP BY f.accession_number HAVING COUNT(DISTINCT COALESCE(a.cik, '')) > 1
            ORDER BY f.accession_number
            """
        )
    ]
    return found


def require(db: Database) -> None:
    """Raise :class:`IdentityViolation` when a stored row breaks the invariant."""
    found = violations(db)
    if found:
        raise IdentityViolation(
            f"stored filings break the filing-identity invariant (T-149): {found.describe()}"
        )
