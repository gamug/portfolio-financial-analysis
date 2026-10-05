"""Read-only check of every stored book against the Work item 18 rules (T-138, docs/model_fixes.md).

    uv run python scripts/verify_t138.py --db PATH [--replay-db PATH] [--quiet] [--tol 1e-9]
                                         [--quant-tol 1e-6]

For every book in each database it opens read-only (``--db``, and ``--replay-db`` when given):

* the **live** book -- one per completed SELECTION run that wrote positions, read from the
  ``portfolio_position`` stints open at the run's date;
* the **replay** book -- the same for REPLAY runs and ``portfolio_position_replay``;
* the **quant** books -- every ``opt-v2`` ``quant_portfolio`` (the live-book snapshot is not one),
  from its open ``quant_position`` rows;

it reports, per book date, from that run's own ``params_json`` (the effective caps, any relaxation):

* **invariants** -- the weights sum to 1; for a ``score_tilt`` book every weight is in
  ``[0.5/n_held, effective name cap]``; no weight exceeds the effective name cap and no sector the
  effective sector cap (all within ``--tol``, 1e-9; a quant book is the output of an interior-point
  solver, so it gets ``--quant-tol``, 1e-6, and positions below 1e-6 are not stored, which the sum
  tolerance allows for); the names held and the shortfall agree with what the run recorded; and the
  stint weights equal the run's ``cycle_ranking.target_weight``. A run that predates T-136 records no
  ``n_held`` and its legacy caps (0.10 / 0.30): it is checked against those, which is the point -- the
  stored production book breaks them;
* the **score dispersion** of the held names (``cycle_ranking``): min, max, range and population
  standard deviation of the blended score;
* **counts** -- names held, shortfall, names held past a full sector (overflow), and every relaxation.

It prints one line per book, a summary per (database, kind), and a total, and exits 1 on any
violation. Nothing is written: both connections are ``mode=ro``.
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

from portfolio_common.db import Database

from kg_schema.queries import connect_ro

# Static SQL, keyed by book kind (never an identifier interpolated into a query).
_STINTS = {
    "live": (
        "SELECT asset_id, weight FROM portfolio_position "
        "WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)"
    ),
    "replay": (
        "SELECT asset_id, weight FROM portfolio_position_replay "
        "WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)"
    ),
}
_RUN_TYPE = {"live": "SELECTION", "replay": "REPLAY"}
_BAND_FLOOR = 0.5  # score_tilt: every weight is at least 0.5 / n_held


@dataclass
class Book:
    db: str
    kind: str  # live | replay | quant
    date: str
    ref: str  # "run 12" / "portfolio 7 (min_var)"
    scheme: str
    weights: dict[int, float]
    params: dict
    legacy: bool  # recorded before T-136: no n_held, no relaxations
    n_held: int
    violations: list[str] = field(default_factory=list)
    relaxations: list[str] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)
    sector_sums: dict[str, float] = field(default_factory=dict)

    @property
    def shortfall(self) -> int:
        return int(self.params.get("shortfall") or 0)

    @property
    def overflow(self) -> int:
        return len(self.params.get("overflow_tickers") or [])

    def score_stats(self) -> tuple[float, float, float, float] | None:
        if not self.scores:
            return None
        lo, hi = min(self.scores), max(self.scores)
        return lo, hi, hi - lo, statistics.pstdev(self.scores)

    def band_use(self) -> float | None:
        """The weight spread as a fraction of the band's width ``1/n_held`` (1.0 = the full band)."""
        if self.scheme != "score_tilt" or not self.weights or self.n_held < 1:
            return None
        return (max(self.weights.values()) - min(self.weights.values())) * self.n_held


def _json(text: str | None) -> dict:
    try:
        value = json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _sector_names(conn: Database) -> tuple[dict[int, str], dict[int, str]]:
    names = {int(r["id"]): str(r["name"]) for r in conn.execute("SELECT id, name FROM sectors")}
    of = {
        int(r["id"]): names.get(int(r["sector_id"]), f"sector {r['sector_id']}")
        if r["sector_id"] is not None
        else "(none)"
        for r in conn.execute("SELECT id, sector_id FROM assets")
    }
    return of, names


def _relaxations(params: dict) -> list[str]:
    return [
        f"{r.get('cap')} {float(r.get('requested')):.4g}->{float(r.get('effective')):.4g}"
        for r in params.get("relaxations") or []
    ]


def _check_caps(book: Book, sector_of: dict[int, str], tol: float) -> None:
    p, w = book.params, book.weights
    name_cap, sector_cap = p.get("max_name_weight"), p.get("max_sector_weight")
    if name_cap is None:
        book.violations.append("no effective name cap recorded")
    elif max(w.values()) - float(name_cap) > tol:
        book.violations.append(f"name weight {max(w.values()):.6f} > cap {float(name_cap):.6f}")
    if book.scheme == "score_tilt" and not book.legacy:
        floor = _BAND_FLOOR / book.n_held
        if min(w.values()) < floor - tol:
            book.violations.append(f"weight {min(w.values()):.6f} < band floor {floor:.6f}")
    for aid, x in w.items():
        s = sector_of.get(aid, "(none)")
        book.sector_sums[s] = book.sector_sums.get(s, 0.0) + x
    if sector_cap is None:
        book.violations.append("no effective sector cap recorded")
        return
    for s, x in sorted(book.sector_sums.items()):
        if x > float(sector_cap) + tol:
            book.violations.append(f"sector {s} {x:.6f} > cap {float(sector_cap):.6f}")


def _check_counts(book: Book) -> None:
    """The names held and the shortfall agree with what the run recorded (T-136 runs and later)."""
    if book.legacy or book.kind == "quant":
        return
    if len(book.weights) != book.n_held:
        book.violations.append(
            f"holds {len(book.weights)} names, run recorded n_held = {book.n_held}"
        )
    requested = book.params.get("top_n")
    if requested is not None and int(requested) - book.n_held != book.shortfall:
        book.violations.append(
            f"shortfall {book.shortfall} != top_n {requested} - n_held {book.n_held}"
        )


def check(book: Book, sector_of: dict[int, str], tol: float, sum_tol: float) -> Book:
    """Fill ``violations`` / ``sector_sums``: the invariants, against the run's own caps."""
    w = book.weights
    if not w:
        if book.n_held != 0:
            book.violations.append(f"empty book, but n_held = {book.n_held}")
        return book
    if abs(sum(w.values()) - 1.0) > sum_tol:
        book.violations.append(f"weights sum to {sum(w.values()):.12f}")
    _check_caps(book, sector_of, tol)
    _check_counts(book)
    book.relaxations = _relaxations(book.params)
    return book


def _stint_differs(ranking: list, weights: dict[int, float], tol: float) -> int | None:
    """The first selected asset whose stint weight is not the ranking's own target weight."""
    for r in ranking:
        tw, aid = r["target_weight"], int(r["asset_id"])
        if (
            r["selected"]
            and tw is not None
            and aid in weights
            and abs(float(tw) - weights[aid]) > tol
        ):
            return aid
    return None


def cycle_books(
    conn: Database, kind: str, db: str, sector_of: dict[int, str], tol: float
) -> list[Book]:
    """One book per completed run of this kind that wrote positions."""
    out: list[Book] = []
    runs = conn.execute(
        "SELECT r.id, r.cycle_date, r.params_json FROM cycle_run r WHERE r.cycle_type = ? "
        "AND r.status = 'completed' AND EXISTS (SELECT 1 FROM cycle_checkpoint c "
        "WHERE c.cycle_run_id = r.id AND c.step = 'positions' AND c.status = 'done') "
        "ORDER BY r.cycle_date, r.id",
        (_RUN_TYPE[kind],),
    ).fetchall()
    for run in runs:
        d = str(run["cycle_date"])
        params = _json(run["params_json"])
        weights = {
            int(r["asset_id"]): float(r["weight"])
            for r in conn.execute(_STINTS[kind], (d, d))
            if r["weight"] is not None
        }
        ranking = conn.execute(
            "SELECT asset_id, blended_score, selected, target_weight FROM cycle_ranking "
            "WHERE cycle_run_id = ?",
            (int(run["id"]),),
        ).fetchall()
        legacy = params.get("n_held") is None
        book = Book(
            db=db,
            kind=kind,
            date=d,
            ref=f"run {run['id']}",
            scheme=str(params.get("weight_scheme") or "?"),
            weights=weights,
            params=params,
            legacy=legacy,
            n_held=len(weights) if legacy else int(params["n_held"]),
        )
        book.scores = [float(r["blended_score"]) for r in ranking if int(r["asset_id"]) in weights]
        check(book, sector_of, tol, tol)
        differs = _stint_differs(ranking, weights, tol)
        if differs is not None:
            book.violations.append(f"stint {differs} differs from its target_weight")
        out.append(book)
    return out


def quant_books(conn: Database, db: str, sector_of: dict[int, str], tol: float) -> list[Book]:
    if not conn.table_exists("quant_portfolio"):
        return []
    out: list[Book] = []
    rows = conn.execute(
        "SELECT id, as_of, kind, engine_version, params_json FROM quant_portfolio "
        "WHERE engine_version LIKE 'opt-v2%' AND kind <> 'live_book' ORDER BY as_of, kind, id"
    ).fetchall()
    for row in rows:
        weights = {
            int(r["asset_id"]): float(r["weight"])
            for r in conn.execute(
                "SELECT asset_id, weight FROM quant_position "
                "WHERE portfolio_id = ? AND valid_to IS NULL",
                (int(row["id"]),),
            )
        }
        params = _json(row["params_json"])
        book = Book(
            db=db,
            kind="quant",
            date=str(row["as_of"]),
            ref=f"portfolio {row['id']} ({row['kind']})",
            scheme=str(row["kind"]),
            weights=weights,
            params=params,
            legacy=params.get("n_held") is None,
            n_held=int(params.get("n_held") or len(weights)),
        )
        # positions below 1e-6 are not stored (the book is sparsified): the sum may fall short of 1
        check(book, sector_of, tol, max(tol, len(weights) * 1e-6))
        out.append(book)
    return out


def _fmt_book(b: Book) -> str:
    w = b.weights
    stats = b.score_stats()
    score = (
        f"score min {stats[0]:.1f} max {stats[1]:.1f} range {stats[2]:.1f} sd {stats[3]:.2f}"
        if stats
        else "score -"
    )
    caps = (
        f"cap name {float(b.params['max_name_weight']):.4f} "
        f"sector {float(b.params['max_sector_weight']):.4f}"
        if b.params.get("max_name_weight") is not None
        and b.params.get("max_sector_weight") is not None
        else "cap not recorded"
    )
    wr = f"w [{min(w.values()):.4f}, {max(w.values()):.4f}]" if w else "w -"
    relax = f"  RELAXED {', '.join(b.relaxations)}" if b.relaxations else ""
    legacy = "  (pre-T-136 run)" if b.legacy and b.kind != "quant" else ""
    status = "OK" if not b.violations else "VIOLATION: " + "; ".join(b.violations)
    return (
        f"  {b.kind:<6} {b.date} {b.ref:<26} {b.scheme:<18} held {len(w):>3}/{b.n_held:<3} "
        f"short {b.shortfall} ovf {b.overflow} {wr} {caps}  {score}{relax}{legacy}  {status}"
    )


def one_way_turnover(books: list[Book]) -> float | None:
    """The mean of ``0.5 * sum|w_t - w_(t-1)|`` over consecutive books of one kind."""
    ts = []
    for prev, cur in pairwise(books):
        ids = set(prev.weights) | set(cur.weights)
        ts.append(0.5 * sum(abs(cur.weights.get(a, 0.0) - prev.weights.get(a, 0.0)) for a in ids))
    return statistics.fmean(ts) if ts else None


def summarize(label: str, books: list[Book]) -> str:
    bad = [b for b in books if b.violations]
    relaxed = [b for b in books if b.relaxations]
    kinds = collections.Counter(r.split()[0] for b in relaxed for r in b.relaxations)
    ranges = [s[2] for b in books if (s := b.score_stats())]
    stds = [s[3] for b in books if (s := b.score_stats())]
    use = [u for b in books if (u := b.band_use()) is not None]
    turn = one_way_turnover(books) if books[0].kind != "quant" else None  # different objectives
    parts = [
        f"{label}: {len(books)} books",
        f"{sum(len(b.violations) for b in bad)} violations in {len(bad)} books",
        f"{len(relaxed)} with a relaxation {dict(kinds) or ''}".rstrip(),
        f"avg names held {statistics.fmean(len(b.weights) for b in books):.2f}",
        f"shortfall in {sum(1 for b in books if b.shortfall)} books",
        f"overflow in {sum(1 for b in books if b.overflow)} books",
    ]
    if turn is not None:
        parts.append(f"mean one-way turnover {turn:.4f}")
    if ranges:
        parts.append(
            f"score range min/median/max {min(ranges):.2f}/{statistics.median(ranges):.2f}/"
            f"{max(ranges):.2f} (median sd {statistics.median(stds):.2f})"
        )
    if use:
        parts.append(
            f"weight spread / band width min/median/max "
            f"{min(use):.3f}/{statistics.median(use):.3f}/{max(use):.3f}"
        )
    return "  " + "; ".join(parts)


def verify_db(path: str, *, quiet: bool, tol: float, quant_tol: float) -> list[Book]:
    conn = connect_ro(Path(path))
    try:
        sector_of, _ = _sector_names(conn)
        groups: list[tuple[str, list[Book]]] = []
        for kind in ("live", "replay"):
            if conn.table_exists("cycle_run"):
                groups.append((kind, cycle_books(conn, kind, path, sector_of, tol)))
        groups.append(("quant", quant_books(conn, path, sector_of, quant_tol)))
    finally:
        conn.close()
    print(f"\n{path}")
    everything: list[Book] = []
    for kind, books in groups:
        if not books:
            continue
        if not quiet:
            for b in books:
                print(_fmt_book(b))
        else:
            for b in books:
                if b.violations:
                    print(_fmt_book(b))
        print(summarize(f"{kind} books", books))
        everything.extend(books)
    return everything


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--db", required=True, help="the database (live book, replay runs, quant books)"
    )
    ap.add_argument("--replay-db", help="a separate replay database")
    ap.add_argument("--quiet", action="store_true", help="print only violations and the summaries")
    ap.add_argument("--tol", type=float, default=1e-9, help="invariant tolerance (default 1e-9)")
    ap.add_argument(
        "--quant-tol", type=float, default=1e-6, help="tolerance for quant (solver) books"
    )
    args = ap.parse_args()
    books: list[Book] = []
    for path in (args.db, args.replay_db):
        if path:
            books.extend(verify_db(path, quiet=args.quiet, tol=args.tol, quant_tol=args.quant_tol))
    bad = [b for b in books if b.violations]
    print(
        f"\nTOTAL: {len(books)} books checked, {sum(len(b.violations) for b in bad)} violations "
        f"in {len(bad)} books, {sum(1 for b in books if b.relaxations)} with a relaxation"
    )
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
