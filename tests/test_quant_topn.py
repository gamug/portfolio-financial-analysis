"""The backtest's Top-30 book: ``optimize --cycle-run-id N --risk-model-id M`` (WP-B).

BACKTEST_SPEC §2 step 5 / §8.2: the universe is ``cycle_ranking.selected = 1`` of a completed
cycle run for the same as-of, the covariance is the stored risk model's ``[T, T]`` sub-matrix
(no re-estimation), the caps are today's (name 1.5 / n_held, sector 0.30, no turnover cap), and
the book is persisted as ``kind = 'min_var_topn'`` beside the plain ``min_var`` book.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np
import pytest
from portfolio_common.db import Database

from quant.cli import _run_optimize, build_parser
from quant.config import QuantSettings
from quant.db import load_book_weights, load_covariance
from quant.optimize import Constraints, min_variance
from quant.persist import (
    CycleSelectionError,
    TopnBookResult,
    run_build_risk_model,
    run_optimize,
    run_optimize_topn,
)
from quant.returns import run_build_returns

SELECTED = 30


def _book_settings(**over: object) -> QuantSettings:
    base: dict[str, object] = {
        "db_path": Path(":memory:"),
        "lookback_days": 220,
        "min_history_days": 150,
        "liquidity_min_dollar_volume": 0.0,
        "max_name_weight": None,
        "max_sector_weight": 0.30,
        "objectives": ["min_var"],
    }
    base.update(over)
    return QuantSettings(**base)  # type: ignore[arg-type]


def _as_of(conn: Database) -> str:
    return str(conn.execute("SELECT MAX(obs_date) FROM quant_return_daily").fetchone()[0])


def _add_cycle_run(
    conn: Database,
    as_of: str,
    ids: Sequence[int],
    *,
    status: str = "completed",
    cycle_type: str = "REPLAY",
) -> int:
    cur = conn.execute(
        "INSERT INTO cycle_run (cycle_type, cycle_date, started_at, finished_at, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (cycle_type, as_of, f"{as_of}T00:00:00Z", f"{as_of}T01:00:00Z", status),
    )
    run_id = int(cur.lastrowid or 0)
    for rank, aid in enumerate(ids, start=1):
        conn.execute(
            "INSERT INTO cycle_ranking (cycle_run_id, asset_id, rank, blended_score, selected) "
            "VALUES (?, ?, ?, ?, 1)",
            (run_id, aid, rank, 100.0 - rank),
        )
    conn.commit()
    return run_id


@pytest.fixture
def ready(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> tuple[Database, str, int]:
    conn = quant_seed(memory_quant_db, n_assets=36, n_days=300, with_dividends=True)
    # four sectors, so a 0.30 sector cap is feasible under the 0.05 name cap (two sectors hold
    # at most 0.6 of a fully invested book); the panel itself is unchanged
    conn.execute("INSERT OR IGNORE INTO sectors (id, name) VALUES (3, 'S3'), (4, 'S4')")
    conn.execute("UPDATE assets SET sector_id = 1 + (id % 4)")
    conn.commit()
    run_build_returns(
        QuantSettings(db_path=Path(":memory:")),
        date_from="2000-01-01",
        date_to="2100-01-01",
        conn=conn,
    )
    as_of = _as_of(conn)
    model = run_build_risk_model(_book_settings(), as_of=as_of, conn=conn)
    return conn, as_of, model.model_id


# -- the book ---------------------------------------------------------------------------------


def test_the_topn_book_is_min_var_on_the_selected_names(ready: tuple[Database, str, int]) -> None:
    conn, as_of, model_id = ready
    selected = list(range(1, SELECTED + 1))
    cyc = _add_cycle_run(conn, as_of, selected)

    res = run_optimize_topn(
        _book_settings(), as_of=as_of, cycle_run_id=cyc, risk_model_id=model_id, conn=conn
    )

    assert res.kind == "min_var_topn"
    assert res.model_id == model_id
    assert res.cycle_run_id == cyc
    row = conn.execute(
        "SELECT kind, model_id, status, n_positions, params_json FROM quant_portfolio WHERE id = ?",
        (res.portfolio_id,),
    ).fetchone()
    assert (row["kind"], row["model_id"], row["status"]) == ("min_var_topn", model_id, "optimal")
    params = json.loads(row["params_json"])
    assert params["cycle_run_id"] == cyc
    assert params["risk_model_id"] == model_id
    assert params["selection_count"] == SELECTED
    assert params["turnover"]["cap_requested"] is None

    weights = load_book_weights(conn, res.portfolio_id)
    assert set(weights) <= set(selected)
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-6)
    assert max(weights.values()) <= 0.05 + 1e-6  # name cap 1.5 / 30
    by_sector: dict[int, float] = {}
    for aid, w in weights.items():
        sector = conn.execute("SELECT sector_id FROM assets WHERE id = ?", (aid,)).fetchone()[0]
        by_sector[sector] = by_sector.get(sector, 0.0) + w
    assert max(by_sector.values()) <= 0.30 + 1e-6
    assert res.weight_sum == pytest.approx(1.0, abs=1e-6)


def test_it_does_not_collide_with_the_plain_min_var_book(ready: tuple[Database, str, int]) -> None:
    conn, as_of, model_id = ready
    cyc = _add_cycle_run(conn, as_of, list(range(1, SELECTED + 1)))

    plain = run_optimize(_book_settings(), as_of=as_of, conn=conn)
    top = run_optimize_topn(
        _book_settings(), as_of=as_of, cycle_run_id=cyc, risk_model_id=model_id, conn=conn
    )

    books = conn.execute(
        "SELECT id, kind FROM quant_portfolio WHERE as_of = ? ORDER BY kind", (as_of,)
    ).fetchall()
    assert [(int(b["kind"] == "min_var"), b["kind"]) for b in books] == [
        (1, "min_var"),
        (0, "min_var_topn"),
    ]
    assert plain.books["min_var"] != top.portfolio_id
    assert conn.execute("SELECT COUNT(*) FROM quant_portfolio").fetchone()[0] == 2


def test_it_uses_the_stored_covariance_without_re_estimation(
    ready: tuple[Database, str, int],
) -> None:
    conn, as_of, model_id = ready
    selected = list(range(1, SELECTED + 1))
    cyc = _add_cycle_run(conn, as_of, selected)
    res = run_optimize_topn(
        _book_settings(), as_of=as_of, cycle_run_id=cyc, risk_model_id=model_id, conn=conn
    )

    ids, sigma = load_covariance(conn, model_id)
    index = [ids.index(a) for a in selected]
    sub = sigma[np.ix_(index, index)]
    sector_of: dict[int, int | None] = {
        int(r["id"]): int(r["sector_id"]) for r in conn.execute("SELECT id, sector_id FROM assets")
    }
    reference = min_variance(
        sub,
        constraints=Constraints(
            max_name_weight=0.05,
            max_sector_weight=0.30,
            sector_of=sector_of,
            asset_ids=selected,
        ),
    )
    expected = {
        selected[i]: float(w) for i, w in enumerate(reference.weights) if abs(float(w)) > 1e-6
    }
    got = load_book_weights(conn, res.portfolio_id)
    assert got.keys() == expected.keys()
    for aid, weight in expected.items():
        assert got[aid] == pytest.approx(weight, abs=1e-6)


def test_unselected_names_are_not_part_of_the_book(ready: tuple[Database, str, int]) -> None:
    conn, as_of, model_id = ready
    selected = [
        3,
        5,
        7,
        9,
        11,
        13,
        15,
        17,
        19,
        21,
        23,
        25,
        27,
        29,
        31,
        2,
        4,
        6,
        8,
        10,
        12,
        14,
        16,
        18,
        20,
        22,
        24,
        26,
        28,
        30,
    ]
    cyc = _add_cycle_run(conn, as_of, selected)
    # five ranking rows for the same run that were NOT selected
    for rank, aid in enumerate([32, 33, 34, 35, 36], start=SELECTED + 1):
        conn.execute(
            "INSERT INTO cycle_ranking (cycle_run_id, asset_id, rank, blended_score, selected) "
            "VALUES (?, ?, ?, ?, 0)",
            (cyc, aid, rank, 1.0),
        )
    conn.commit()

    res = run_optimize_topn(
        _book_settings(), as_of=as_of, cycle_run_id=cyc, risk_model_id=model_id, conn=conn
    )
    assert set(load_book_weights(conn, res.portfolio_id)) <= set(selected)


def test_other_objectives_are_ignored_and_recorded(ready: tuple[Database, str, int]) -> None:
    conn, as_of, model_id = ready
    cyc = _add_cycle_run(conn, as_of, list(range(1, SELECTED + 1)))
    res = run_optimize_topn(
        _book_settings(objectives=["min_var", "tangency"]),
        as_of=as_of,
        cycle_run_id=cyc,
        risk_model_id=model_id,
        conn=conn,
    )
    params = json.loads(
        conn.execute(
            "SELECT params_json FROM quant_portfolio WHERE id = ?", (res.portfolio_id,)
        ).fetchone()[0]
    )
    assert params["objectives_ignored"] == ["tangency"]
    assert conn.execute("SELECT COUNT(*) FROM quant_portfolio").fetchone()[0] == 1


# -- refusals ---------------------------------------------------------------------------------


def test_it_refuses_a_selection_that_is_not_exactly_thirty(
    ready: tuple[Database, str, int],
) -> None:
    conn, as_of, model_id = ready
    for n in (SELECTED - 1, SELECTED + 1):
        cyc = _add_cycle_run(conn, as_of, list(range(1, n + 1)))
        with pytest.raises(CycleSelectionError, match=r"needs exactly 30"):
            run_optimize_topn(
                _book_settings(), as_of=as_of, cycle_run_id=cyc, risk_model_id=model_id, conn=conn
            )
        conn.execute("DELETE FROM cycle_run WHERE id = ?", (cyc,))
        conn.commit()


def test_it_refuses_a_name_outside_the_models_panel(ready: tuple[Database, str, int]) -> None:
    conn, as_of, model_id = ready
    conn.execute(
        "INSERT INTO assets (id, ticker, company_name, sector_id) VALUES (99, 'ZZ', 'Z', 1)"
    )
    cyc = _add_cycle_run(conn, as_of, [*range(1, SELECTED), 99])
    with pytest.raises(CycleSelectionError, match=r"does not cover 1 selected asset"):
        run_optimize_topn(
            _book_settings(), as_of=as_of, cycle_run_id=cyc, risk_model_id=model_id, conn=conn
        )


def test_it_refuses_a_missing_or_unfinished_cycle_run(ready: tuple[Database, str, int]) -> None:
    conn, as_of, model_id = ready
    with pytest.raises(CycleSelectionError, match="not stored"):
        run_optimize_topn(
            _book_settings(), as_of=as_of, cycle_run_id=999999, risk_model_id=model_id, conn=conn
        )
    failed = _add_cycle_run(
        conn, as_of, list(range(1, SELECTED + 1)), status="failed", cycle_type="SELECTION"
    )
    with pytest.raises(CycleSelectionError, match=r"failed, not completed"):
        run_optimize_topn(
            _book_settings(), as_of=as_of, cycle_run_id=failed, risk_model_id=model_id, conn=conn
        )


def test_it_refuses_a_cycle_run_from_another_date(ready: tuple[Database, str, int]) -> None:
    conn, as_of, model_id = ready
    earlier = str(
        conn.execute(
            "SELECT obs_date FROM quant_return_daily WHERE obs_date < ? "
            "GROUP BY obs_date ORDER BY obs_date DESC LIMIT 1",
            (as_of,),
        ).fetchone()[0]
    )
    cyc = _add_cycle_run(conn, earlier, list(range(1, SELECTED + 1)))
    with pytest.raises(CycleSelectionError, match=f"not the analysis date {as_of}"):
        run_optimize_topn(
            _book_settings(), as_of=as_of, cycle_run_id=cyc, risk_model_id=model_id, conn=conn
        )


def test_it_refuses_a_model_from_another_date_or_other_inputs(
    ready: tuple[Database, str, int],
) -> None:
    conn, as_of, _ = ready
    cyc = _add_cycle_run(conn, as_of, list(range(1, SELECTED + 1)))

    earlier = str(
        conn.execute(
            "SELECT obs_date FROM quant_return_daily WHERE obs_date < ? "
            "GROUP BY obs_date ORDER BY obs_date DESC LIMIT 1",
            (as_of,),
        ).fetchone()[0]
    )
    old_model = run_build_risk_model(_book_settings(), as_of=earlier, conn=conn)
    with pytest.raises(CycleSelectionError, match=r"is as of .*not"):
        run_optimize_topn(
            _book_settings(),
            as_of=as_of,
            cycle_run_id=cyc,
            risk_model_id=old_model.model_id,
            conn=conn,
        )

    wide_model = run_build_risk_model(
        _book_settings(exclude_hard_vetoed=False), as_of=as_of, conn=conn
    )
    with pytest.raises(CycleSelectionError, match="built on different inputs"):
        run_optimize_topn(
            _book_settings(),
            as_of=as_of,
            cycle_run_id=cyc,
            risk_model_id=wide_model.model_id,
            conn=conn,
        )


def test_it_refuses_a_model_without_a_stored_covariance(ready: tuple[Database, str, int]) -> None:
    conn, as_of, model_id = ready
    cyc = _add_cycle_run(conn, as_of, list(range(1, SELECTED + 1)))
    # a model built with --no-store-cov has no quant_covariance rows
    conn.execute("DELETE FROM quant_covariance WHERE model_id = ?", (model_id,))
    conn.commit()
    with pytest.raises(CycleSelectionError, match="no stored covariance"):
        run_optimize_topn(
            _book_settings(), as_of=as_of, cycle_run_id=cyc, risk_model_id=model_id, conn=conn
        )


def test_it_refuses_a_covariance_that_is_not_psd(ready: tuple[Database, str, int]) -> None:
    conn, as_of, model_id = ready
    cyc = _add_cycle_run(conn, as_of, list(range(1, SELECTED + 1)))
    a, b = 1, 2
    conn.execute(
        "DELETE FROM quant_covariance WHERE model_id = ? AND (asset_id_i IN (?, ?) OR "
        "asset_id_j IN (?, ?))",
        (model_id, a, b, a, b),
    )
    lo, hi = sorted((a, b))
    conn.executemany(
        "INSERT INTO quant_covariance (model_id, asset_id_i, asset_id_j, value) VALUES (?, ?, ?, ?)",
        [(model_id, lo, lo, 1e-8), (model_id, hi, hi, 1e-8), (model_id, lo, hi, 1e-4)],
    )
    conn.commit()
    with pytest.raises(CycleSelectionError, match="not positive semidefinite"):
        run_optimize_topn(
            _book_settings(), as_of=as_of, cycle_run_id=cyc, risk_model_id=model_id, conn=conn
        )


def test_it_refuses_a_turnover_cap(ready: tuple[Database, str, int]) -> None:
    conn, as_of, model_id = ready
    cyc = _add_cycle_run(conn, as_of, list(range(1, SELECTED + 1)))
    with pytest.raises(CycleSelectionError, match="no turnover cap"):
        run_optimize_topn(
            _book_settings(turnover_cap=0.5),
            as_of=as_of,
            cycle_run_id=cyc,
            risk_model_id=model_id,
            conn=conn,
        )


# -- the CLI ----------------------------------------------------------------------------------


def test_the_flags_exist_and_must_be_given_together(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()
    args = parser.parse_args(["optimize", "--cycle-run-id", "3", "--risk-model-id", "7"])
    assert (args.cycle_run_id, args.risk_model_id) == (3, 7)
    assert _run_optimize(_book_settings(), "2026-01-02", cycle_run_id=3) == 1
    assert "--cycle-run-id and --risk-model-id must be given together" in capsys.readouterr().err
    assert _run_optimize(_book_settings(), "2026-01-02", risk_model_id=7) == 1
    assert "--cycle-run-id and --risk-model-id must be given together" in capsys.readouterr().err


def test_the_topn_cli_ends_with_the_exact_json_contract(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake(*_a: object, **_k: object) -> TopnBookResult:
        return TopnBookResult(21, 7, "2026-01-02", 3, "min_var_topn", 12, "optimal", 1.0)

    monkeypatch.setattr("quant.cli.run_optimize_topn", fake)
    assert _run_optimize(_book_settings(), "2026-01-02", cycle_run_id=3, risk_model_id=7) == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(last) == {"portfolio_id": 21, "model_id": 7, "kind": "min_var_topn"}


def test_the_cli_exits_1_on_a_refused_selection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*_a: object, **_k: object) -> TopnBookResult:
        raise CycleSelectionError(
            "cycle run 3 selected 29 asset(s); the Top-30 book needs exactly 30"
        )

    monkeypatch.setattr("quant.cli.run_optimize_topn", refuse)
    assert _run_optimize(_book_settings(), "2026-01-02", cycle_run_id=3, risk_model_id=7) == 1
    assert "needs exactly 30" in capsys.readouterr().err
