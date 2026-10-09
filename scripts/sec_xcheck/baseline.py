"""The "before" baseline of the thesis' data-treatment impact analysis (D-11): a read-only export.

    uv run python scripts/audit_sec_checklist.py baseline --out <DIR outside the repo> --date YYYY-MM-DD

Exports, from the database as it stands (opened ``mode=ro``), one CSV per table, rows in primary-key order:

* ``cycle_run``, ``cycle_ranking`` (the final ranking, its components, vetoes and target weights),
  ``portfolio_position`` -- per cycle date;
* ``score_snapshot`` -- every score type, so each asset's VALORIZATION components (``inputs_json``) travel with it;
* ``veto`` and ``rule_catalog``;
* ``fundamental_metrics`` -- the stored metrics the scores read, with ``inputs_json`` and ``available_at``;
* ``PROVENANCE.txt`` -- the database path, its sha1 before and after the export, and the export time.

Nothing is overwritten: an existing file stops the export. The files stay outside the repo; the repo tracks only
the manifest (file list, row counts, SHA-256 of each file) written by :func:`manifest_markdown`.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
from pathlib import Path
from typing import Any

# table -> (columns are read from the table itself) ordering clause; a static dict of literal SQL, never an
# interpolated identifier.
EXPORTS: dict[str, str] = {
    "cycle_run": "SELECT * FROM cycle_run ORDER BY id",
    "cycle_ranking": "SELECT * FROM cycle_ranking ORDER BY id",
    "portfolio_position": "SELECT * FROM portfolio_position ORDER BY id",
    "score_snapshot": "SELECT * FROM score_snapshot ORDER BY id",
    "veto": "SELECT * FROM veto ORDER BY id",
    "rule_catalog": "SELECT * FROM rule_catalog ORDER BY rule_id",
    "fundamental_metrics": "SELECT * FROM fundamental_metrics ORDER BY id",
}


def file_digest(path: Path, algo: str = "sha256") -> str:
    h = hashlib.new(algo)
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def export_table(conn: Any, name: str, out_dir: Path) -> tuple[Path, int]:
    path = out_dir / f"{name}.csv"
    if path.exists():
        raise FileExistsError(f"{path} exists; the baseline is never overwritten")
    cursor = conn.execute(EXPORTS[name])
    header = [d[0] for d in cursor.description]
    n = 0
    with path.open("x", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for row in cursor:
            writer.writerow(["" if v is None else v for v in tuple(row)])
            n += 1
    return path, n


def export_baseline(conn: Any, db_path: Path, out_dir: Path) -> list[tuple[str, int, str]]:
    """Write every table and ``PROVENANCE.txt``; return ``[(file, rows, sha256)]``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    before = file_digest(db_path, "sha1")
    result: list[tuple[str, int, str]] = []
    for name in EXPORTS:
        path, n = export_table(conn, name, out_dir)
        result.append((path.name, n, file_digest(path)))
    after = file_digest(db_path, "sha1")
    provenance = out_dir / "PROVENANCE.txt"
    with provenance.open("x", encoding="utf-8") as fh:
        fh.write(
            f"database: {db_path}\nsha1 before export: {before}\nsha1 after export:  {after}\n"
            f"exported at: {dt.datetime.now(dt.UTC).isoformat(timespec='seconds')}\n"
            "opened: mode=ro (read-only)\n"
        )
    if before != after:
        raise RuntimeError(f"the database changed during the export: {before} -> {after}")
    result.append((provenance.name, sum(1 for _ in provenance.open()), file_digest(provenance)))
    return result


def manifest_markdown(
    files: list[tuple[str, int, str]], db_path: Path, db_sha1: str, out_dir: Path, exported_on: str
) -> str:
    lines = [
        '# Baseline manifest — the "before" state of the data-treatment impact analysis (D-11)',
        "",
        f"Exported {exported_on} by `scripts/audit_sec_checklist.py baseline`, read-only (`mode=ro`).",
        "",
        f"- Database: `{db_path}`",
        f"- Database sha1 (before and after the export, unchanged): `{db_sha1}`",
        f"- Files (outside the repo): `{out_dir}/`",
        "",
        "| File | Rows | SHA-256 |",
        "|---|---|---|",
    ]
    lines += [f"| `{name}` | {rows:,} | `{sha}` |" for name, rows, sha in files]
    lines += [
        "",
        "Rows count data rows (the CSV header excluded); `PROVENANCE.txt` counts its lines.",
        "",
    ]
    return "\n".join(lines)
