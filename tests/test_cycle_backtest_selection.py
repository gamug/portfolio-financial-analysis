"""The backtest's selection cuts (WP-C, BACKTEST_SPEC §0/§2/§8.3).

``run_replay`` gains two REPLAY-only knobs -- ``risk_model_id`` (the Top-N cohort is the quant
risk model's panel) and ``veto_cutoff_same_day`` (R instead of the live R-1 contagion lag) -- plus
``CycleReport.selected_asset_ids``. The live ``select`` must not move: its veto cutoff stays R-1
and it never reads a risk model.

The tests run the real rule/veto/ranking/positions path on the ``cycle_seed`` cohort (5 names, 2
sectors, ``top_n = 3``) and drive the cutoff test through a real HARD rule (``NEGATIVE_FCF`` made
to fire on the top-ranked name by giving it a negative free-cash-flow margin), so the R vs R-1
comparison is the production mechanism, not a stub.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from portfolio_common.db import Database

import cycle.orchestrator as cycle_orchestrator
from cycle.config import CycleSettings
from cycle.data import TooFewCandidates
from cycle.orchestrator import CycleReport, run_replay, run_selection
from cycle.replay import reset_replay_range
from cycle.rules import seed_catalog
from cycle.state import ReplayInputsMismatch
from kg_schema.queries import UnknownRiskModel, risk_model_asset_ids

D = "2026-06-30"


def _settings() -> CycleSettings:
    return CycleSettings(db_path=Path(":memory:"), top_n=3)


def _risk_model(conn: Database, asset_ids: list[int], *, version: str = "rm-test") -> int:
    """A ``quant_risk_model`` row as ``quant build-risk-model`` writes it: the panel spec carries
    the id vector, ``n_assets`` counts it -- no ``quant_covariance`` rows needed."""
    return int(
        conn.execute(
            "INSERT INTO quant_risk_model (as_of, model_version, lookback_days, min_history_days, "
            "n_assets, cov_estimator, ret_estimator, periods_per_year, panel_engine_version, "
            "panel_spec_json, computed_at) VALUES (?, ?, 756, 740, ?, 'ledoit_wolf_cc', "
            "'hist_mean', 252, 'ret-v1', ?, '2026-06-30T00:00:00Z') RETURNING id",
            (
                D,
                version,
                len(asset_ids),
                json.dumps(
                    {
                        "asset_ids": asset_ids,
                        "date_start": "2019-01-02",
                        "date_end": D,
                        "n_dates": 1000,
                        "sha256": "0" * 64,
                    }
                ),
            ),
        ).fetchone()["id"]
    )


def _only_negative_fcf(conn: Database, asset_id: int = 1) -> None:
    """Leave ``NEGATIVE_FCF`` (HARD) as the only active rule and make it fire on *asset_id*.

    The cohort's seeded debt/equity (1.5 .. 5.5) trips ``LEVERAGE_EXTREME`` on four names, which
    would drown the one veto this test compares across cutoffs; disabling a rule in
    ``rule_catalog`` is the catalogue's own switch (``seed_catalog`` keeps ``enabled`` on
    conflict), not a code change."""
    seed_catalog(conn, 15.0)
    conn.execute("UPDATE rule_catalog SET enabled = 0 WHERE rule_id <> 'NEGATIVE_FCF'")
    conn.execute(
        "UPDATE fundamental_metrics SET value = -0.5 WHERE metric_name = 'free_cash_flow_margin' "
        "AND filing_id IN (SELECT id FROM sec_filings WHERE asset_id = ?)",
        (asset_id,),
    )
    conn.commit()


def _ranking(conn: Database, cycle_run_id: int) -> list[dict]:
    return [
        {
            "asset_id": int(r["asset_id"]),
            "ticker": str(r["ticker"]),
            "rank": int(r["rank"]),
            "veto_rules": json.loads(r["veto_rules_json"] or "[]"),
            "selected": int(r["selected"]),
        }
        for r in conn.execute(
            "SELECT r.asset_id, a.ticker, r.rank, r.veto_rules_json, r.selected FROM cycle_ranking r "
            "JOIN assets a ON a.id = r.asset_id WHERE r.cycle_run_id = ? ORDER BY r.rank",
            (cycle_run_id,),
        )
    ]


# -- the veto cutoff: R vs R-1 ---------------------------------------------------------------


def test_veto_cutoff_same_day_filters_a_veto_raised_on_the_cycle_date(
    cycle_seed: Database,
) -> None:
    """With ``veto_cutoff_same_day=True`` the HARD veto the run itself raises at R applies to R:
    the top-ranked name (AAA) is out and the book fills below it."""
    conn = cycle_seed
    _only_negative_fcf(conn)

    report = run_replay(_settings(), D, conn=conn, veto_cutoff_same_day=True)

    rows = _ranking(conn, report.cycle_run_id)
    top = rows[0]
    assert top["ticker"] == "AAA"
    assert "HARD" in top["veto_rules"]  # the veto step raised it at R, at the R cutoff
    assert top["selected"] == 0
    assert report.selected == 3
    assert report.selected_asset_ids == [r["asset_id"] for r in rows[1:4]]
    assert "AAA" not in [t for t, _s, _w in report.book_rows]
    # the run recorded the cut it applied
    params = json.loads(
        conn.execute(
            "SELECT params_json FROM cycle_run WHERE id = ?", (report.cycle_run_id,)
        ).fetchone()["params_json"]
    )
    assert params["veto_cutoff_same_day"] is True
    assert params["risk_model_id"] is None


def test_veto_cutoff_r_minus_1_keeps_a_veto_raised_on_the_cycle_date(
    cycle_seed: Database,
) -> None:
    """The same data, the live cutoff (R-1): AAA has no veto *yet*, so the book holds it -- the
    T-070 contagion lag the REPLAY cut replaces for the backtest."""
    conn = cycle_seed
    _only_negative_fcf(conn)

    report = run_replay(_settings(), D, conn=conn, veto_cutoff_same_day=False)

    rows = _ranking(conn, report.cycle_run_id)
    assert rows[0]["ticker"] == "AAA"
    assert rows[0]["veto_rules"] == []  # the stint opens at R, invisible at R-1
    assert rows[0]["selected"] == 1
    assert report.selected_asset_ids[0] == rows[0]["asset_id"]


def test_the_two_cuts_differ_on_identical_data(cycle_seed: Database) -> None:
    """One database, one date: the R cutoff and the R-1 cutoff select different books.

    ``reset_replay_range`` is the project's own way to redo a REPLAY date (``backfill --force``),
    so the second run recomputes from the same inputs rather than resuming."""
    conn = cycle_seed
    _only_negative_fcf(conn)
    same_day = run_replay(_settings(), D, conn=conn, veto_cutoff_same_day=True)
    same_day_ids = same_day.selected_asset_ids

    reset_replay_range(conn, D)
    lagged = run_replay(_settings(), D, conn=conn, veto_cutoff_same_day=False)

    assert same_day_ids != lagged.selected_asset_ids
    assert 1 not in same_day_ids  # AAA, HARD-vetoed at R
    assert 1 in lagged.selected_asset_ids


# -- the Top-N cohort is the risk model's panel ----------------------------------------------


def test_selection_skips_names_the_risk_model_does_not_cover(cycle_seed: Database) -> None:
    """A ranked name outside the model's panel is marked NO_RISK_MODEL, never selected, and the
    book keeps going down the ranking under the same sector cap (``build_book``'s overflow),
    ending with exactly ``top_n`` names."""
    conn = cycle_seed
    model_id = _risk_model(conn, [2, 3, 4, 5])  # every name but AAA
    settings = _settings()

    report = run_replay(settings, D, conn=conn, risk_model_id=model_id)

    rows = _ranking(conn, report.cycle_run_id)
    excluded = [r for r in rows if "NO_RISK_MODEL" in r["veto_rules"]]
    assert [r["ticker"] for r in excluded] == ["AAA"]
    assert excluded[0]["selected"] == 0
    assert excluded[0]["asset_id"] not in report.selected_asset_ids
    assert report.selected == settings.top_n
    assert len(report.selected_asset_ids) == settings.top_n
    assert set(report.selected_asset_ids) <= {2, 3, 4, 5}
    # it went *down* the ranking past a full sector rather than stopping at the shortfall
    assert report.book is not None and report.book.shortfall == 0
    assert report.book.overflow_tickers != ()
    assert set(report.book.overflow_tickers) <= {t for t, _s, _w in report.book_rows}
    # and the caps it reports are the caps it applied
    assert report.book.max_sector_weight >= settings.max_sector_weight - 1e-12
    by_sector: dict[str | None, float] = {}
    for _t, sector, weight in report.book_rows:
        by_sector[sector] = by_sector.get(sector, 0.0) + weight
    assert sum(by_sector.values()) == pytest.approx(1.0)
    assert all(w <= report.book.max_sector_weight + 1e-9 for w in by_sector.values())
    # NO_RISK_MODEL is an eligibility marker, never a SOFT veto: no penalty off the score
    assert excluded[0]["veto_rules"] == ["NO_RISK_MODEL"]


def test_selection_refuses_a_short_book_under_a_risk_model(cycle_seed: Database) -> None:
    """Two covered names cannot make a Top-3 book: ``TooFewCandidates``, no position written and
    the run marked failed -- never a smaller portfolio."""
    conn = cycle_seed
    model_id = _risk_model(conn, [1, 2])

    with pytest.raises(TooFewCandidates, match=rf"risk model {model_id}.*3 are NO_RISK_MODEL"):
        run_replay(_settings(), D, conn=conn, risk_model_id=model_id)

    run = conn.execute("SELECT id, status FROM cycle_run WHERE cycle_type = 'REPLAY'").fetchone()
    assert run["status"] == "failed"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM portfolio_position_replay WHERE opened_by_cycle = ?",
            (int(run["id"]),),
        ).fetchone()[0]
        == 0
    )


def test_the_report_carries_the_run_id_and_the_selected_ids(cycle_seed: Database) -> None:
    """``CycleReport.selected_asset_ids`` is the persisted ranking's ``selected = 1`` rows in rank
    order, on the run that wrote them *and* on a resume that skips ``positions``."""
    conn = cycle_seed
    report = run_replay(_settings(), D, conn=conn)
    assert isinstance(report, CycleReport)
    assert report.cycle_run_id == int(
        conn.execute("SELECT id FROM cycle_run WHERE cycle_type = 'REPLAY'").fetchone()["id"]
    )
    selected = [
        int(r["asset_id"])
        for r in conn.execute(
            "SELECT asset_id FROM cycle_ranking WHERE cycle_run_id = ? AND selected = 1 "
            "ORDER BY rank",
            (report.cycle_run_id,),
        )
    ]
    assert report.selected_asset_ids == selected
    assert len(selected) == 3

    again = run_replay(_settings(), D, conn=conn)
    assert "positions" in again.steps_skipped
    assert again.selected_asset_ids == report.selected_asset_ids
    assert again.selected == 3


# -- the recording and the resume refusal ----------------------------------------------------


def test_a_resume_under_another_selection_cut_is_refused(cycle_seed: Database) -> None:
    """The cut is recorded once; resuming the same date under another one would replace its
    cohort, so it is refused -- the ``check_score_weights`` pattern."""
    conn = cycle_seed
    model_id = _risk_model(conn, [1, 2, 3, 4, 5])
    first = run_replay(_settings(), D, conn=conn, risk_model_id=model_id)
    assert first.selected == 3

    with pytest.raises(ReplayInputsMismatch, match=r"risk_model_id = 1.*risk_model_id = None"):
        run_replay(_settings(), D, conn=conn)
    with pytest.raises(ReplayInputsMismatch, match="veto_cutoff_same_day"):
        run_replay(_settings(), D, conn=conn, risk_model_id=model_id, veto_cutoff_same_day=True)

    # same cut again: an ordinary resume, nothing rewritten
    again = run_replay(_settings(), D, conn=conn, risk_model_id=model_id)
    assert again.selected_asset_ids == first.selected_asset_ids
    assert "positions" in again.steps_skipped


def test_an_unknown_risk_model_is_refused_before_a_run_row_exists(cycle_seed: Database) -> None:
    conn = cycle_seed
    with pytest.raises(UnknownRiskModel, match="no quant_risk_model with id 999"):
        run_replay(_settings(), D, conn=conn, risk_model_id=999)
    assert conn.execute("SELECT COUNT(*) FROM cycle_run").fetchone()[0] == 0


# -- the live select does not move -----------------------------------------------------------


def test_live_selection_keeps_the_t_minus_1_veto_lag(cycle_seed: Database) -> None:
    """``select`` has no cutoff knob: a HARD veto raised at R still leaves the name in the R book
    (T-070: the veto bites from R+1)."""
    conn = cycle_seed
    _only_negative_fcf(conn)

    report = run_selection(_settings(), D, conn=conn)

    rows = _ranking(conn, report.cycle_run_id)
    assert rows[0]["ticker"] == "AAA"
    assert rows[0]["selected"] == 1
    params = json.loads(
        conn.execute(
            "SELECT params_json FROM cycle_run WHERE id = ?", (report.cycle_run_id,)
        ).fetchone()["params_json"]
    )
    assert params["veto_cutoff_same_day"] is False
    # a plain resume (no cut arguments, as the CLI passes them) is not refused
    assert run_selection(_settings(), D, conn=conn).selected == report.selected


def test_the_same_day_cut_is_refused_on_a_selection_run(cycle_seed: Database) -> None:
    """The REPLAY-only guard, at the runner: a SELECTION cut of R is never allowed."""
    with pytest.raises(ValueError, match="REPLAY-only"):
        cycle_orchestrator._run(
            _settings(),
            "SELECTION",
            D,
            cycle_orchestrator._SELECTION_STEPS,
            conn=cycle_seed,
            fundamental_hook=None,
            veto_cutoff_same_day=True,
        )


# -- risk_model_asset_ids: the panel spec is authoritative ------------------------------------


def test_risk_model_asset_ids_reads_the_panel_spec(memory_db: Database) -> None:
    """The panel spec, not the stored covariance: a model with no ``quant_covariance`` rows (the
    matrix was not stored) still reports its full panel, and the lower-triangle cache never
    narrows it."""
    conn = memory_db
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'A'), (2, 'B'), (3, 'C')")
    model_id = _risk_model(conn, [1, 2, 3])
    assert risk_model_asset_ids(conn, model_id) == {1, 2, 3}

    # a partial lower-triangle cache says nothing about the panel
    conn.executemany(
        "INSERT INTO quant_covariance (model_id, asset_id_i, asset_id_j, value) VALUES (?, ?, ?, ?)",
        [(model_id, 1, 1, 0.04), (model_id, 1, 2, 0.01)],
    )
    conn.commit()
    assert risk_model_asset_ids(conn, model_id) == {1, 2, 3}


def test_risk_model_asset_ids_refuses_an_unknown_or_inconsistent_model(memory_db: Database) -> None:
    conn = memory_db
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'A'), (2, 'B')")
    assert risk_model_asset_ids(conn, _risk_model(conn, [1, 2])) == {1, 2}
    with pytest.raises(UnknownRiskModel, match="no quant_risk_model"):
        risk_model_asset_ids(conn, 404)
    # n_assets disagrees with the spec: refused, never half-read
    bad = _risk_model(conn, [1, 2], version="rm-bad")
    conn.execute("UPDATE quant_risk_model SET n_assets = 3 WHERE id = ?", (bad,))
    conn.commit()
    with pytest.raises(UnknownRiskModel, match="n_assets = 3"):
        risk_model_asset_ids(conn, bad)
