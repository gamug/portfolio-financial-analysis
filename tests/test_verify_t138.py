"""``scripts/verify_t138.py``: the read-only check of stored books against the Work item 18 rules."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from portfolio_common.db import Database

_SPEC = importlib.util.spec_from_file_location(
    "verify_t138", Path(__file__).resolve().parent.parent / "scripts" / "verify_t138.py"
)
assert _SPEC and _SPEC.loader
verify = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_t138"] = verify
_SPEC.loader.exec_module(verify)

TOL = 1e-9


def _seed_sectors(conn: Database, sectors: list[str]) -> None:
    names = sorted(set(sectors))
    for i, name in enumerate(names, start=1):
        conn.execute("INSERT INTO sectors (id, name) VALUES (?, ?)", (i, name))
    for i, s in enumerate(sectors, start=1):
        conn.execute(
            "INSERT INTO assets (id, ticker, sector_id) VALUES (?, ?, ?)",
            (i, f"T{i:02d}", names.index(s) + 1),
        )


def _run(  # noqa: PLR0913 - a test seeder with one keyword per knob
    conn: Database,
    *,
    run_id: int,
    day: str,
    weights: list[float],
    params: dict,
    cycle_type: str = "SELECTION",
    table: str = "portfolio_position",
    target_weights: list[float] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO cycle_run (id, cycle_type, cycle_date, started_at, status, params_json) "
        "VALUES (?, ?, ?, 'now', 'completed', ?)",
        (run_id, cycle_type, day, json.dumps(params)),
    )
    conn.execute(
        "INSERT INTO cycle_checkpoint (cycle_run_id, step, status, updated_at) "
        "VALUES (?, 'positions', 'done', 'now')",
        (run_id,),
    )
    tw = target_weights if target_weights is not None else weights
    for i, (w, t) in enumerate(zip(weights, tw, strict=True), start=1):
        conn.execute(
            "INSERT INTO cycle_ranking (cycle_run_id, asset_id, rank, blended_score, selected, "
            "target_weight) VALUES (?, ?, ?, ?, 1, ?)",
            (run_id, i, i, 80.0 - 5 * i, t),
        )
        conn.execute(
            f"INSERT INTO {table} (asset_id, valid_from, weight, run_id) VALUES (?, ?, ?, ?)",  # noqa: S608
            (i, day, w, run_id),
        )
    conn.commit()


def _books(conn: Database, kind: str = "live", tol: float = TOL) -> list:
    sector_of, _ = verify._sector_names(conn)
    return list(verify.cycle_books(conn, kind, "test", sector_of, tol))


def _tilt_params(**over: object) -> dict:
    base = {
        "weight_scheme": "score_tilt",
        "top_n": 4,
        "n_held": 4,
        "shortfall": 0,
        "max_name_weight": 0.375,
        "max_sector_weight": 0.5,
        "relaxations": [],
        "overflow_tickers": [],
    }
    base.update(over)
    return base


def test_a_valid_score_tilt_book_passes_with_its_dispersion_and_counts(memory_db: Database) -> None:
    _seed_sectors(memory_db, ["A", "A", "B", "C"])
    _run(
        memory_db,
        run_id=1,
        day="2026-01-02",
        weights=[0.30, 0.20, 0.30, 0.20],
        params=_tilt_params(),
    )
    (b,) = _books(memory_db)
    assert b.violations == [] and b.relaxations == [] and not b.legacy
    assert b.sector_sums["A"] == pytest.approx(0.5)
    lo, hi, rng, sd = b.score_stats()
    assert (lo, hi, rng) == (60.0, 75.0, 15.0) and sd == pytest.approx(5.5901699)
    assert b.band_use() == pytest.approx(0.10 * 4)  # weight spread x n_held
    assert (b.shortfall, b.overflow, len(b.weights)) == (0, 0, 4)


def test_the_stored_production_shape_is_flagged(memory_db: Database) -> None:
    """cycle_run 1: six names at 0.1167 against a 0.10 cap, Energy at 0.35 against 0.30 -- a run that
    predates T-136 (no n_held), checked against the legacy caps it recorded."""
    sectors = ["Fin"] * 4 + ["Energy"] * 3 + ["Staples"] * 2 + ["RE"]
    w = [0.075] * 4 + [0.35 / 3] * 3 + [0.35 / 3] * 2 + [0.35 / 3]
    w[4:] = [0.11666666666666665] * 6
    _seed_sectors(memory_db, sectors)
    _run(
        memory_db, run_id=1, day="2026-09-22", weights=w,
        params={"weight_scheme": "score_proportional", "top_n": 10, "max_name_weight": 0.10,
                "max_sector_weight": 0.30},
    )  # fmt: skip
    (b,) = _books(memory_db)
    assert b.legacy
    assert any("name weight 0.116667 > cap 0.100000" in v for v in b.violations)
    assert any("sector Energy" in v and "0.350000" in v for v in b.violations)


def test_a_pre_t136_run_is_not_held_to_counts_it_never_recorded(memory_db: Database) -> None:
    _seed_sectors(memory_db, ["A", "B", "C", "D"])
    legacy = {
        "weight_scheme": "score_proportional",
        "top_n": 30,
        "max_name_weight": 0.3,
        "max_sector_weight": 0.5,
    }
    _run(memory_db, run_id=1, day="2026-01-02", weights=[0.25] * 4, params=legacy)
    (b,) = _books(memory_db)
    assert (
        b.legacy and b.violations == []
    )  # 4 names held of top_n 30: no n_held, no shortfall to check


@pytest.mark.parametrize(
    ("weights", "params", "expected"),
    [
        ([0.30, 0.20, 0.30, 0.19], {}, "weights sum to"),
        ([0.20, 0.30, 0.30, 0.20], {"max_name_weight": 0.25}, "name weight"),
        ([0.45, 0.05, 0.30, 0.20], {}, "name weight"),
        ([0.38, 0.12, 0.30, 0.20], {"max_name_weight": 0.5}, "band floor"),
        ([0.30, 0.25, 0.25, 0.20], {"max_sector_weight": 0.5}, "sector A"),
        ([0.30, 0.20, 0.30, 0.20], {"n_held": 3}, "n_held"),
        ([0.30, 0.20, 0.30, 0.20], {"top_n": 9}, "shortfall"),
        ([0.30, 0.20, 0.30, 0.20], {"max_name_weight": None}, "no effective name cap"),
    ],
)
def test_each_invariant_is_checked(
    memory_db: Database, weights: list[float], params: dict, expected: str
) -> None:
    _seed_sectors(memory_db, ["A", "A", "B", "C"])
    _run(memory_db, run_id=1, day="2026-01-02", weights=weights, params=_tilt_params(**params))
    (b,) = _books(memory_db)
    assert any(expected in v for v in b.violations), b.violations


def test_a_relaxed_cap_is_checked_against_the_relaxed_value_and_listed(memory_db: Database) -> None:
    _seed_sectors(memory_db, ["A", "A", "B", "B"])
    relaxation = {"cap": "sector", "requested": 0.3, "effective": 0.5, "reason": "two sectors"}
    _run(
        memory_db, run_id=1, day="2026-01-02", weights=[0.25, 0.25, 0.25, 0.25],
        params=_tilt_params(max_sector_weight=0.5, relaxations=[relaxation]),
    )  # fmt: skip
    (b,) = _books(memory_db)
    assert b.violations == [] and b.relaxations == ["sector 0.3->0.5"]
    # the same book against the unrelaxed value would fail: the recorded effective cap is what counts
    memory_db.execute(
        "UPDATE cycle_run SET params_json = ?", (json.dumps(_tilt_params(max_sector_weight=0.3)),)
    )
    (c,) = _books(memory_db)
    assert any("sector" in v for v in c.violations)


def test_a_stint_that_differs_from_the_rankings_target_weight_is_flagged(
    memory_db: Database,
) -> None:
    _seed_sectors(memory_db, ["A", "B", "C", "D"])
    _run(
        memory_db, run_id=1, day="2026-01-02", weights=[0.25] * 4, params=_tilt_params(),
        target_weights=[0.25, 0.25, 0.26, 0.24],
    )  # fmt: skip
    (b,) = _books(memory_db)
    assert any("differs from its target_weight" in v for v in b.violations)


def test_the_replay_book_is_read_from_its_own_table_and_turnover_is_one_way(
    memory_db: Database,
) -> None:
    _seed_sectors(memory_db, ["A", "B", "C", "D"])
    _run(memory_db, run_id=1, day="2026-01-02", weights=[0.25] * 4, params=_tilt_params(),
         cycle_type="REPLAY", table="portfolio_position_replay")  # fmt: skip
    _run(memory_db, run_id=2, day="2026-01-09", weights=[0.30, 0.30, 0.20, 0.20],
         params=_tilt_params(), cycle_type="REPLAY", table="portfolio_position_replay")  # fmt: skip
    # the same asset's first stint is closed when the second opens
    memory_db.execute(
        "UPDATE portfolio_position_replay SET valid_to = '2026-01-09' WHERE valid_from = '2026-01-02'"
    )
    books = _books(memory_db, "replay")
    assert [b.date for b in books] == ["2026-01-02", "2026-01-09"]
    assert books[1].weights == {1: 0.30, 2: 0.30, 3: 0.20, 4: 0.20}  # the open stints at that date
    assert verify.one_way_turnover(books) == pytest.approx(0.10)
    assert _books(memory_db, "live") == []
    line = verify.summarize("replay books", books)
    assert "2 books" in line and "mean one-way turnover 0.1000" in line


def test_the_main_exit_code_follows_the_violations_and_the_database_is_untouched(
    memory_db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_sectors(memory_db, ["A", "A", "B", "C"])
    _run(
        memory_db,
        run_id=1,
        day="2026-01-02",
        weights=[0.30, 0.20, 0.30, 0.20],
        params=_tilt_params(),
    )
    path = tmp_path / "books.db"
    target = sqlite3.connect(path)
    memory_db._conn.backup(target)
    target.close()
    before = path.read_bytes()
    monkeypatch.setattr(sys, "argv", ["verify_t138.py", "--db", str(path)])
    assert verify.main() == 0
    assert "0 violations" in capsys.readouterr().out
    assert path.read_bytes() == before
    # break the book: exit 1, and still nothing written
    raw = sqlite3.connect(path)
    raw.execute("UPDATE portfolio_position SET weight = 0.5 WHERE asset_id = 1")
    raw.commit()
    raw.close()
    before = path.read_bytes()
    assert verify.main() == 1
    assert "VIOLATION" in capsys.readouterr().out
    assert path.read_bytes() == before


def test_quant_books_are_read_from_opt_v2_portfolios_and_checked(memory_quant_db: Database) -> None:
    conn = memory_quant_db
    _seed_sectors(conn, ["A", "A", "B", "B"])
    caps = {
        "top_n": 30,
        "n_held": 4,
        "max_name_weight": 0.375,
        "max_sector_weight": 0.5,
        "relaxations": [],
    }
    for pid, version, w in (
        (1, "opt-v2+abcd", [0.3, 0.2, 0.3, 0.2]),
        (2, "opt-v1+abcd", [0.25] * 4),
    ):
        conn.execute(
            "INSERT INTO quant_portfolio (id, as_of, kind, objective, solver, status, engine_version, "
            "n_positions, computed_at, params_json) VALUES (?, '2026-07-09', 'min_var', 'x', 'CLARABEL', "
            "'optimal', ?, 4, 'now', ?)",
            (pid, version, json.dumps(caps)),
        )
        for i, x in enumerate(w, start=1):
            conn.execute(
                "INSERT INTO quant_position (portfolio_id, asset_id, valid_from, weight) "
                "VALUES (?, ?, '2026-07-09', ?)",
                (pid, i, x),
            )
    conn.commit()
    sector_of, _ = verify._sector_names(conn)
    books = verify.quant_books(conn, "test", sector_of, 1e-6)
    assert [b.ref for b in books] == ["portfolio 1 (min_var)"]  # opt-v1 is not an opt-v2 book
    assert books[0].violations == []
    conn.execute("UPDATE quant_position SET weight = 0.4 WHERE portfolio_id = 1 AND asset_id = 1")
    (bad,) = verify.quant_books(conn, "test", sector_of, 1e-6)
    assert any("name weight" in v for v in bad.violations)
