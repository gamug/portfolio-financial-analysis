"""T-154 verification: the one data check (3.2).

`python -m fundamental_agent migrate` used to replay m001..m014 on a database that had the current
columns but no migration floor, and each rebuilding migration copies only the columns it knows, so
`financial_facts.preferred_sign` (T-148) and `correction_rule` (T-118) came back NULL. This script
copies the T-148b scratch database (which is in exactly that state) into the T-154 scratch root,
counts the two columns, runs `apply_migrations` on the copy, counts them again and prints both.

Run from the repo root:

    uv run python scripts/verify_t154.py replay

Writes only under the scratch root; never opens a production database read-write.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

from portfolio_common.db import Database

import kg_schema
from kg_schema.migrations import REBUILD_GUARDS, apply_migrations, at_current_shape

SCRATCH_ROOT = Path("/workspaces/thesis/scratch")
DEFAULT_SOURCE = SCRATCH_ROOT / "t148b" / "after_full.db"
DEFAULT_WORK = SCRATCH_ROOT / "t154" / "check.db"


def _counts(path: Path) -> dict[str, int]:
    """Row counts of the two columns the replay used to drop, plus the table's own row count."""
    conn = kg_schema.connect_ro(path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS facts, "
            "COUNT(preferred_sign) AS preferred_sign, "
            "COUNT(correction_rule) AS correction_rule FROM financial_facts"
        ).fetchone()
        return {key: int(row[key]) for key in ("facts", "preferred_sign", "correction_rule")}
    finally:
        conn.close()


def _shape(path: Path) -> list[str]:
    """The stored unique keys of financial_facts and fundamental_metrics, for the report."""
    raw = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    raw.row_factory = sqlite3.Row
    conn = Database(raw)
    out: list[str] = []
    try:
        for table in ("financial_facts", "fundamental_metrics"):
            for index in conn.execute("SELECT name FROM pragma_index_list(?)", (table,)):
                if int(
                    conn.execute(
                        'SELECT "unique" AS u FROM pragma_index_list(?) WHERE name = ?',
                        (table, str(index["name"])),
                    ).fetchone()["u"]
                ):
                    columns = [
                        str(r["name"])
                        for r in conn.execute(
                            "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                            (str(index["name"]),),
                        )
                    ]
                    out.append(f"{table}: UNIQUE ({', '.join(columns)})")
    finally:
        conn.close()
    return out


def _guards(path: Path) -> dict[int, bool]:
    conn = kg_schema.connect_ro(path)
    try:
        return {version: check(conn) for version, check in REBUILD_GUARDS.items()}
    finally:
        conn.close()


def _fmt(counts: dict[str, int]) -> str:
    return " ".join(f"{key}={value:,}" for key, value in counts.items())


def cmd_replay(args: argparse.Namespace) -> int:
    source, work = Path(args.source), Path(args.work)
    if not work.resolve().is_relative_to(SCRATCH_ROOT):
        print(f"refusing to write outside {SCRATCH_ROOT}: {work}")
        return 2
    shutil.copy2(source, work)
    before = _counts(work)
    print(f"source:      {source}")
    print(f"copy:        {work}")
    print(f"shape before: {'; '.join(_shape(work))}")
    print(f"guards before (at the target shape?): {_guards(work)}")
    print(f"at_current_shape before: {_is_current(work)}")
    print(f"before:      {_fmt(before)}")

    conn = kg_schema.connect(work)
    try:
        applied = apply_migrations(conn)
    finally:
        conn.close()

    after = _counts(work)
    print(f"applied:     {applied}")
    print(f"shape after:  {'; '.join(_shape(work))}")
    print(f"after:       {_fmt(after)}")
    equal = before == after
    print(f"counts equal: {equal}")
    if not args.keep:
        work.unlink()
        print(f"removed:     {work}")
    return 0 if equal else 1


def _is_current(path: Path) -> bool:
    conn = kg_schema.connect_ro(path)
    try:
        return at_current_shape(conn)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    replay = sub.add_parser("replay", help="3.2: a migration on a copy must change no count")
    replay.add_argument("--source", default=str(DEFAULT_SOURCE))
    replay.add_argument("--work", default=str(DEFAULT_WORK))
    replay.add_argument("--keep", action="store_true", help="keep the copy instead of deleting it")
    replay.set_defaults(func=cmd_replay)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
