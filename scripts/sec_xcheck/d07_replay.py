"""D-07: the replayed book's sector exposure with and without the financial firms, on scratch copies.

The financial firms (APP-00: Article 9, Article 7 and other financial filers) among the pilot's 20 assets are excluded
through ``cycle backfill --exclude``, the supported way to build a replay book with a preference. Two **scratch copies**
of the pilot replay database are built outside the repo with identical flags, one without and one with the exclusion; the
originals are never opened for writing, and the copies are deleted afterwards. The exposure at each replay date is the
weight per sector of the positions open on it (``portfolio_position_replay``).
"""

from __future__ import annotations

import collections
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from sec_xcheck.common import REPO, open_ro
from sec_xcheck.findings import Finding

CMD = "uv run python scripts/audit_sec_checklist.py d07-replay"


def backfill(  # noqa: PLR0913, PLR0917 - the CLI's own arguments
    db: Path, universe_db: Path, start: str, end: str, top_n: int, exclude: list[str]
) -> None:
    """Run ``python -m cycle backfill`` against the scratch copy *db* (never the original)."""
    args = [
        sys.executable, "-m", "cycle", "backfill", "--from", start, "--to", end, "--db", str(db), "--force",
        "--allow-dirty", "--allow-stale-prices", "--top-n", str(top_n),
    ]  # fmt: skip
    if exclude:
        args += ["--exclude", ",".join(exclude)]
    env = {**os.environ, "KG_UNIVERSE_DB": str(universe_db)}
    subprocess.run(args, check=True, capture_output=True, cwd=REPO, env=env)  # noqa: S603


def exposure(db: Path) -> tuple[dict[str, float], dict[str, float], int]:
    """``(mean sector weight, mean financial weight by type is added by the caller, dates)`` over the replay dates."""
    conn = open_ro(str(db))
    dates = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT valid_from FROM portfolio_position_replay ORDER BY valid_from"
        )
    ]
    sector_of = {
        r["id"]: r["name"]
        for r in conn.execute(
            "SELECT a.id, s.name FROM assets a JOIN sectors s ON s.id = a.sector_id"
        )
    }
    totals: dict[str, float] = collections.defaultdict(float)
    names: dict[str, float] = collections.defaultdict(float)
    for d in dates:
        rows = conn.execute(
            "SELECT asset_id, weight FROM portfolio_position_replay WHERE valid_from <= ? "
            "AND (valid_to IS NULL OR valid_to > ?)",
            (d, d),
        ).fetchall()
        book = sum(r["weight"] or 0.0 for r in rows) or 1.0
        for r in rows:
            totals[sector_of[r["asset_id"]]] += (r["weight"] or 0.0) / book
            names["n"] += 1
    n_dates = len(dates) or 1
    return (
        {k: v / n_dates for k, v in totals.items()},
        {"names_per_date": names["n"] / n_dates},
        len(dates),
    )


def run(  # noqa: PLR0913, PLR0917 - the databases, the tickers, the range and the book size
    source_db: Path,
    universe_db: Path,
    financial_tickers: list[str],
    start: str,
    end: str,
    top_n: int = 10,
) -> dict[str, Any]:
    """Build both scratch copies, read their exposures, delete the copies."""
    work = Path(tempfile.mkdtemp(prefix="t147_d07_"))
    try:
        with_fin, without_fin = work / "with.db", work / "without.db"
        shutil.copyfile(source_db, with_fin)
        shutil.copyfile(source_db, without_fin)
        backfill(with_fin, universe_db, start, end, top_n, [])
        backfill(without_fin, universe_db, start, end, top_n, financial_tickers)
        base, base_extra, n = exposure(with_fin)
        excl, excl_extra, n2 = exposure(without_fin)
        return {"with": base, "without": excl, "names": (base_extra, excl_extra), "dates": (n, n2)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def findings(result: dict[str, Any], financial_sector_names: set[str]) -> list[Finding]:
    base, excl = result["with"], result["without"]
    fin_base = sum(w for s, w in base.items() if s in financial_sector_names)
    fin_excl = sum(w for s, w in excl.items() if s in financial_sector_names)
    detail = "; ".join(
        f"{s} {base.get(s, 0) * 100:.1f}% -> {excl.get(s, 0) * 100:.1f}%"
        for s in sorted(set(base) | set(excl))
    )
    return [
        Finding("D07.replay_financials_sector_weight", ["APP-00", "APP-02"], "Mean weight of the Financials sector in the replayed book, with -> without the financial firms (pilot replay, scratch copies)", "b", round(fin_base * 10000), 10000, 0, unit=f"basis points (with); without = {round(fin_excl * 10000)}", command=CMD, note=detail),
    ]  # fmt: skip
