"""The cover-count audit layer of the final pilot database (T-143 part A, FIXES #8, #12, #13, #15).

Three versioned data files make a new run reproduce what was applied directly to ``pilot_a3``:

* ``data/cover_class_conversion.csv`` -> table ``cover_class_conversion`` (L-43 / D-21, MKT-03): the
  rate at which an unlisted class converts into the traded one, as the filing itself prints it, each
  row citing the accession that prints it. :func:`kg_schema.queries.cover_class_rates` reads it.
* ``data/audit_prior_cover_shares.csv`` -> table ``audit_prior_cover_shares`` (F-A12, MKT-01): the
  cover count of a company's last filing *before* its first stored one, from the SEC's own
  ``dei:EntityCommonStockSharesOutstanding`` fact. Resolved to an asset by CIK.
* ``data/cover_corrections.csv`` -> table ``audit_cover_corrections`` (D-20, reviewer A): a cover count
  the filing's own EDGAR page contradicts (an x1,000 scale, a lost digit, a shell's placeholder 100)
  is corrected in place, and every correction is logged with the page it was read from.

:func:`ensure_seeded` loads the first two and the correction log on every :func:`kg_schema.ensure`
and never changes a row it already holds. :func:`correct_filing_covers` applies the corrections to a
filing's cover rows when they are written, so a run that reads the gateway's wrong count stores the
verified one; it only touches a row that still holds the recorded old value.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from portfolio_common.db import Database

from .identity import normalize_cik

_DATA = Path(__file__).resolve().parent / "data"
CONVERSION_CSV = _DATA / "cover_class_conversion.csv"
PRIOR_COVER_CSV = _DATA / "audit_prior_cover_shares.csv"
CORRECTIONS_CSV = _DATA / "cover_corrections.csv"


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _seed_conversion(db: Database, path: Path) -> int:
    if not db.relation_exists("cover_class_conversion"):
        return 0
    rows = _read(path)
    db.executemany(
        "INSERT OR IGNORE INTO cover_class_conversion "
        "(accession, class_member, rate, source_accession, source_text, rule) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                r["accession"],
                r["class_member"],
                float(r["rate"]),
                r["source_accession"],
                r["source_text"],
                r["rule"] or None,
            )
            for r in rows
        ],
    )
    return len(rows)


def _seed_prior_covers(db: Database, path: Path) -> int:
    if not (
        db.relation_exists("audit_prior_cover_shares")
        and db.relation_exists("assets")
        and "cik" in db.table_columns("assets")  # a pre-T-148 schema has no CIK to match on
    ):
        return 0
    assets = {
        normalize_cik(r["cik"]): int(r["id"])
        for r in db.execute("SELECT id, cik FROM assets WHERE cik IS NOT NULL").fetchall()
    }
    n = 0
    for r in _read(path):
        asset_id = assets.get(normalize_cik(r["cik"]))
        if asset_id is None:
            continue  # a company this database does not hold
        db.execute(
            "INSERT INTO audit_prior_cover_shares "
            "(asset_id, sec_accession, value, as_of_date, available_at, source) "
            "SELECT ?, ?, ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM audit_prior_cover_shares "
            "WHERE asset_id = ? AND sec_accession = ? AND as_of_date = ?)",
            (
                asset_id,
                r["sec_accession"],
                float(r["value"]),
                r["as_of_date"],
                r["available_at"],
                r["source"],
                asset_id,
                r["sec_accession"],
                r["as_of_date"],
            ),
        )
        n += 1
    return n


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _log_correction(db: Database, r: dict[str, str], row_id: int) -> None:
    db.execute(
        "INSERT INTO audit_cover_corrections (table_name, row_id, accession, class_member, "
        "as_of_date, old_value, new_value, action, source_accession, edgar_page, rule, "
        "corrected_at) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS "
        "(SELECT 1 FROM audit_cover_corrections WHERE table_name = ? AND accession = ? "
        "AND as_of_date = ? AND old_value = ?)",
        (
            r["table_name"],
            row_id,
            r["accession"],
            r["class_member"] or None,
            r["as_of_date"],
            float(r["old_value"]),
            float(r["new_value"]) if r["new_value"] else None,
            r["action"],
            r["source_accession"],
            r["edgar_page"],
            r["rule"],
            _now(),
            r["table_name"],
            r["accession"],
            r["as_of_date"],
            float(r["old_value"]),
        ),
    )


def _seed_correction_log(db: Database, path: Path) -> int:
    """The log lines of the corrections made to ``audit_prior_cover_shares``: that table is seeded
    from a file that holds the corrected counts already, so its log lines are written here. The
    ``filing_cover_shares`` ones are logged when :func:`correct_filing_covers` applies them."""
    if not (
        db.relation_exists("audit_cover_corrections")
        and db.relation_exists("audit_prior_cover_shares")
    ):
        return 0
    n = 0
    for r in _read(path):
        if r["table_name"] != "audit_prior_cover_shares":
            continue
        row = db.execute(
            "SELECT id FROM audit_prior_cover_shares WHERE sec_accession = ? AND as_of_date = ?",
            (r["accession"], r["as_of_date"]),
        ).fetchone()
        if row is not None:  # a company this database does not hold has no row to log
            _log_correction(db, r, int(row["id"]))
            n += 1
    return n


def ensure_seeded(
    db: Database,
    *,
    conversion: Path = CONVERSION_CSV,
    prior: Path = PRIOR_COVER_CSV,
    corrections: Path = CORRECTIONS_CSV,
) -> None:
    """Load the three tables from their files; a no-op on a database that lacks a table."""
    _seed_conversion(db, conversion)
    _seed_prior_covers(db, prior)
    _seed_correction_log(db, corrections)
    db.commit()


def correct_filing_covers(
    db: Database, filing_id: int, *, corrections: Path = CORRECTIONS_CSV
) -> int:
    """Apply the recorded D-20 corrections to *filing_id*'s cover rows and log each one. Returns how
    many rows changed. A row is touched only while it still holds the recorded old value."""
    if not (db.relation_exists("audit_cover_corrections") and db.relation_exists("sec_filings")):
        return 0
    filing = db.execute(
        "SELECT accession_number FROM sec_filings WHERE id = ?", (filing_id,)
    ).fetchone()
    if filing is None:
        return 0
    accession = str(filing["accession_number"])
    changed = 0
    for r in _read(corrections):
        if r["table_name"] != "filing_cover_shares" or r["accession"] != accession:
            continue
        row = db.execute(
            "SELECT id FROM filing_cover_shares WHERE filing_id = ? AND class_member = ? "
            "AND as_of_date = ? AND value = ?",
            (filing_id, r["class_member"], r["as_of_date"], float(r["old_value"])),
        ).fetchone()
        if row is None:
            continue
        if r["action"] == "delete":
            db.execute("DELETE FROM filing_cover_shares WHERE id = ?", (row["id"],))
        else:
            db.execute(
                "UPDATE filing_cover_shares SET value = ? WHERE id = ?",
                (float(r["new_value"]), row["id"]),
            )
        _log_correction(db, r, int(row["id"]))
        changed += 1
    return changed
