"""Turnover (T-077): the previous book of a chain, the cap's constraint (a name that left the panel
still counts), its recorded relaxation, and ``evaluate``'s one-off cost under ``perf-v3``."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from portfolio_common.db import Database

import quant.evaluate as quant_evaluate
from kg_schema.versions import MetricVersions
from quant import cli
from quant.caps import Relaxation
from quant.config import QuantSettings
from quant.db import (
    PortfolioRow,
    insert_portfolio,
    load_book_weights,
    load_previous_book,
    sync_positions,
)
from quant.evaluate import PERF_ENGINE_VERSION, _turnover_cost, run_evaluate
from quant.manifest import QuantManifest
from quant.optimize import Constraints, max_sharpe, min_turnover, min_variance
from quant.persist import run_optimize
from quant.returns import run_build_returns
from quant.turnover import (
    PreviousBook,
    book_engine_version,
    check_turnover_cap,
    plan_turnover,
    turnover_between,
)

# -- pure helpers ---------------------------------------------------------------------------


def test_turnover_cap_must_be_in_zero_to_two() -> None:
    assert check_turnover_cap(None) is None
    assert check_turnover_cap(0.5) == 0.5 and check_turnover_cap(2.0) == 2.0
    for bad in (0.0, -1.0, 2.01):
        with pytest.raises(ValueError, match=r"\(0, 2\]"):
            check_turnover_cap(bad)


def test_the_book_key_changes_only_for_a_non_default_estimator_or_a_cap() -> None:
    manifest = QuantManifest(
        metrics=MetricVersions({"valuation": "metrics-v2"}), return_engine_version="qret-v2"
    )
    base = QuantSettings(db_path=Path(":memory:"))
    default = book_engine_version(base, manifest)
    assert default == manifest.book_tagged("opt-v2")  # a default book keeps its pre-T-077 key
    cc = base.model_copy(update={"ret_estimator": "carhart"})
    assert book_engine_version(cc, manifest) == f"opt-v2.mu-carhart+{manifest.book_tag}"
    capped = base.model_copy(update={"turnover_cap": 0.5})
    assert book_engine_version(capped, manifest) == f"opt-v2.to-0.5+{manifest.book_tag}"
    both = base.model_copy(update={"ret_estimator": "carhart", "turnover_cap": 0.25})
    assert book_engine_version(both, manifest) == f"opt-v2.mu-carhart.to-0.25+{manifest.book_tag}"


def test_turnover_counts_every_name_in_either_book() -> None:
    assert turnover_between({1: 0.5, 2: 0.5}, {1: 0.25, 3: 0.75}) == pytest.approx(
        0.25 + 0.5 + 0.75
    )
    assert turnover_between({1: 0.6, 2: 0.4}, None) == pytest.approx(
        1.0
    )  # the first book is bought
    assert turnover_between({1: 1.0}, {1: 1.0}) == 0.0


def _sigma() -> np.ndarray:
    return np.diag([0.04, 0.05, 0.06, 0.07]) + 0.01


def _cons(n: int = 4) -> Constraints:
    return Constraints(max_name_weight=0.6, max_sector_weight=None, asset_ids=list(range(n)))


def test_the_constraint_holds_and_a_name_outside_the_panel_counts() -> None:
    ids = [0, 1, 2, 3]
    # the previous book held 0.2 in asset 99, which is not in today's panel
    prev = PreviousBook(5, "2026-01-02", {0: 0.5, 1: 0.2, 2: 0.1, 99: 0.2})
    plan = plan_turnover(_cons(), ids, prev, 0.6)
    assert plan.record["outside_panel_weight"] == pytest.approx(0.2)
    assert plan.relaxation is None and plan.record["applied"] is True
    w = min_variance(_sigma(), constraints=plan.constraints).weights
    w_prev = np.array([0.5, 0.2, 0.1, 0.0])
    moved = np.abs(w - w_prev).sum() + 0.2  # the panel's moves plus the name that left
    assert moved <= 0.6 + 1e-6
    # unconstrained, the minimum-variance book trades more than that, so the cap actually binds
    free = min_variance(_sigma(), constraints=_cons()).weights
    assert np.abs(free - w_prev).sum() + 0.2 > 0.6 + 1e-3


def test_a_cap_below_the_smallest_feasible_turnover_is_relaxed_and_recorded() -> None:
    ids = [0, 1, 2, 3]
    prev = PreviousBook(5, "2026-01-02", {0: 0.5, 1: 0.2, 2: 0.1, 99: 0.2})
    smallest = (
        min_turnover(plan_turnover(_cons(), ids, prev, 2.0).constraints) + 0.2
    )  # what the other constraints allow, plus the name that left
    plan = plan_turnover(_cons(), ids, prev, 0.01)
    assert isinstance(plan.relaxation, Relaxation) and plan.relaxation.cap == "turnover"
    assert plan.relaxation.requested == 0.01
    assert plan.relaxation.effective == pytest.approx(smallest, abs=1e-5)
    assert plan.record["cap_effective"] == plan.relaxation.effective
    w = min_variance(
        _sigma(), constraints=plan.constraints
    ).weights  # feasible after the relaxation
    assert (
        np.abs(w - np.array([0.5, 0.2, 0.1, 0.0])).sum() + 0.2 <= plan.relaxation.effective + 1e-6
    )


def test_no_cap_no_previous_book_or_an_objective_that_ignores_it_binds_nothing() -> None:
    ids = [0, 1, 2, 3]
    prev = PreviousBook(5, "2026-01-02", {0: 1.0})
    for plan, why in (
        (plan_turnover(_cons(), ids, prev, None), "no turnover cap requested"),
        (plan_turnover(_cons(), ids, None, 0.5), "no previous book in the chain"),
        (plan_turnover(_cons(), ids, prev, 0.5, applies=False), "this objective ignores"),
    ):
        assert plan.record["applied"] is False and why in plan.record["reason_not_applied"]
        assert plan.constraints.turnover_cap is None and plan.constraints.w_prev is None


def test_tangency_with_a_binding_cap_is_the_best_frontier_point_and_without_one_is_exact() -> None:
    mu = np.array([0.10, 0.08, 0.06, 0.05])
    plain = max_sharpe(mu, _sigma(), rf=0.02, constraints=_cons())
    assert plain.status == "optimal"
    prev = PreviousBook(1, "2026-01-02", {0: 0.25, 1: 0.25, 2: 0.25, 3: 0.25})
    bound = plan_turnover(_cons(), [0, 1, 2, 3], prev, 0.3).constraints
    capped = max_sharpe(mu, _sigma(), rf=0.02, constraints=bound)
    assert capped.status == "from_frontier"
    assert np.abs(capped.weights - 0.25).sum() <= 0.3 + 1e-6
    # a cap with no previous book is not bound, so the exact y-space tangency still runs
    unbound = plan_turnover(_cons(), [0, 1, 2, 3], None, 0.3).constraints
    assert max_sharpe(mu, _sigma(), rf=0.02, constraints=unbound).status == "optimal"


def test_tangency_falls_back_to_the_frontier_when_the_caps_leave_no_positive_excess() -> None:
    """Audit Q5: with every name capped at 1/3 the book is forced equal-weight, whose excess
    return is negative although one name's is positive -- the y-space program is infeasible. It
    used to raise; it returns the best feasible book instead."""
    mu = np.array([0.10, 0.00, 0.00])
    cons = Constraints(max_name_weight=1 / 3 + 1e-9, max_sector_weight=None, asset_ids=[0, 1, 2])
    res = max_sharpe(mu, np.eye(3) * 0.04, rf=0.04, constraints=cons)
    assert res.weights == pytest.approx([1 / 3, 1 / 3, 1 / 3], abs=1e-4)
    assert res.status in {"from_frontier", "no_tangency"}


# -- the previous book ----------------------------------------------------------------------


def _row(
    as_of: str, kind: str = "min_var", engine: str = "opt-v2+abcd1234", **over: object
) -> PortfolioRow:
    base: dict[str, object] = {
        "as_of": as_of,
        "kind": kind,
        "objective": kind,
        "solver": "CLARABEL",
        "status": "optimal",
        "expected_return": 0.05,
        "expected_vol": 0.12,
        "sharpe": 0.4,
        "rf_annual": 0.045,
        "n_positions": 2,
        "engine_version": engine,
    }
    base.update(over)
    return PortfolioRow(**base)  # type: ignore[arg-type]


def test_the_previous_book_is_the_newest_earlier_one_of_the_same_chain(
    memory_quant_db: Database,
) -> None:
    conn = memory_quant_db
    ids = {
        "old": insert_portfolio(conn, _row("2026-01-02")),
        "newest": insert_portfolio(conn, _row("2026-02-02")),
        "same_day": insert_portfolio(conn, _row("2026-03-02")),
        "later": insert_portfolio(conn, _row("2026-04-02")),
        "other_kind": insert_portfolio(conn, _row("2026-02-20", kind="tangency")),
        "other_engine": insert_portfolio(conn, _row("2026-02-25", engine="opt-v1+abcd1234")),
        "other_estimator": insert_portfolio(
            conn, _row("2026-02-26", engine="opt-v2.mu-carhart+abcd1234")
        ),
        "other_cap": insert_portfolio(conn, _row("2026-02-27", engine="opt-v2.to-0.5+abcd1234")),
        "frontier_pt": insert_portfolio(conn, _row("2026-02-28", frontier_k=3)),
    }
    found = load_previous_book(
        conn, kind="min_var", frontier_k=None, engine_version="opt-v2+abcd1234", before="2026-03-02"
    )
    assert found == (
        ids["newest"],
        "2026-02-02",
    )  # not the same day, nor a later book, nor another chain
    assert (
        load_previous_book(
            conn,
            kind="min_var",
            frontier_k=None,
            engine_version="opt-v2+abcd1234",
            before="2026-01-02",
        )
        is None
    )
    assert load_previous_book(
        conn, kind="min_var", frontier_k=3, engine_version="opt-v2+abcd1234", before="2026-12-31"
    ) == (ids["frontier_pt"], "2026-02-28")
    capped = load_previous_book(
        conn,
        kind="min_var",
        frontier_k=None,
        engine_version="opt-v2.to-0.5+abcd1234",
        before="2026-12-31",
    )
    assert capped == (ids["other_cap"], "2026-02-27")


# -- through optimize -----------------------------------------------------------------------


def _settings(**over: object) -> QuantSettings:
    base: dict[str, object] = {
        "db_path": Path(":memory:"),
        "lookback_days": 150,
        "min_history_days": 100,
        "liquidity_min_dollar_volume": 0.0,
        "max_name_weight": None,
        "max_sector_weight": None,
        "objectives": ["min_var", "tangency", "target_vol", "risk_parity"],
    }
    base.update(over)
    return QuantSettings(**base)  # type: ignore[arg-type]


@pytest.fixture
def seeded(memory_quant_db: Database, quant_seed: Callable[..., Database]) -> Database:
    conn = quant_seed(memory_quant_db, n_assets=6, n_days=280, with_dividends=True)
    run_build_returns(
        QuantSettings(db_path=Path(":memory:")),
        date_from="2000-01-01",
        date_to="2100-01-01",
        conn=conn,
    )
    return conn


def _dates(conn: Database) -> list[str]:
    return [
        r[0] for r in conn.execute("SELECT DISTINCT obs_date FROM quant_return_daily ORDER BY 1")
    ]


def _book(conn: Database, pid: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT status, turnover, engine_version, params_json FROM quant_portfolio WHERE id = ?",
        (pid,),
    ).fetchone()
    return {**dict(row), "params": json.loads(row["params_json"])}


def test_a_default_run_has_no_cap_and_each_chain_starts_from_nothing(seeded: Database) -> None:
    d = _dates(seeded)
    first = run_optimize(_settings(), as_of=d[-60], conn=seeded)
    second = run_optimize(_settings(), as_of=d[-40], conn=seeded)
    for pid in first.books.values():
        b = _book(seeded, pid)
        assert b["turnover"] is None  # nothing to trade from
        assert "." not in str(b["engine_version"]).partition("+")[0]  # no variant marks
        assert b["params"]["turnover"]["reason_not_applied"] == "no turnover cap requested"
        assert b["params"]["turnover"]["realized"] is None
    for kind, pid in second.books.items():
        b = _book(seeded, pid)
        prev = load_book_weights(seeded, first.books[kind])
        now = load_book_weights(seeded, pid)
        assert b["turnover"] == pytest.approx(
            turnover_between(now, prev)
        )  # realized, still recorded
        assert b["params"]["turnover"]["previous_portfolio_id"] == first.books[kind]
        assert b["params"]["turnover"]["cap_requested"] is None


def test_the_cap_holds_against_the_previous_book_of_its_own_chain(seeded: Database) -> None:
    d = _dates(seeded)
    cap = 0.005  # the seeded books move ~1-3% a month, so this binds
    s = _settings(turnover_cap=cap)
    # an uncapped chain at the same dates must not become the capped chain's "previous book"
    free = {
        day: run_optimize(_settings(), as_of=day, conn=seeded) for day in (d[-60], d[-40], d[-20])
    }
    first = run_optimize(s, as_of=d[-60], conn=seeded)
    for pid in first.books.values():
        p = _book(seeded, pid)["params"]["turnover"]
        assert p["applied"] is False and p["reason_not_applied"] == "no previous book in the chain"
    prev = first
    for day in (d[-40], d[-20]):
        run = run_optimize(s, as_of=day, conn=seeded)
        for kind, pid in run.books.items():
            b = _book(seeded, pid)
            t = b["params"]["turnover"]
            assert str(b["engine_version"]).startswith("opt-v2.to-0.005+")
            assert t["previous_portfolio_id"] == prev.books[kind]
            if kind == "risk_parity":  # ignores the cap, and says so
                assert t["applied"] is False and "ignores" in t["reason_not_applied"]
                continue
            assert t["applied"] is True and t["cap_requested"] == cap
            if kind in {"min_var", "target_vol"}:  # the cap is not decoration: it binds
                assert float(_book(seeded, free[day].books[kind])["turnover"]) > cap * 1.5
            assert float(b["turnover"]) <= t["cap_effective"] + 1e-5
            now = load_book_weights(seeded, pid)
            before = load_book_weights(seeded, prev.books[kind])
            assert turnover_between(now, before) <= t["cap_effective"] + 1e-5
            if kind == "tangency":
                assert b["status"] == "from_frontier"
        prev = run


def test_a_loose_cap_leaves_a_mu_free_book_where_it_would_be_anyway(seeded: Database) -> None:
    d = _dates(seeded)
    s = _settings(objectives=["min_var"])
    run_optimize(s, as_of=d[-40], conn=seeded)
    free = run_optimize(s, as_of=d[-20], conn=seeded)
    run_optimize(s.model_copy(update={"turnover_cap": 2.0}), as_of=d[-40], conn=seeded)
    loose = run_optimize(s.model_copy(update={"turnover_cap": 2.0}), as_of=d[-20], conn=seeded)
    a, b = (
        load_book_weights(seeded, free.books["min_var"]),
        load_book_weights(seeded, loose.books["min_var"]),
    )
    assert {k: round(v, 5) for k, v in a.items()} == {k: round(v, 5) for k, v in b.items()}


def test_a_name_that_left_the_panel_still_counts_and_the_relaxation_is_recorded(
    seeded: Database,
) -> None:
    d = _dates(seeded)
    s = _settings(objectives=["min_var"], turnover_cap=0.5)
    first = run_optimize(s, as_of=d[-40], conn=seeded)
    pid = first.books["min_var"]
    # the previous book also held 25% in a name that is not in today's panel
    seeded.execute(
        "INSERT INTO assets (id, ticker, company_name, sector_id) VALUES (99, 'GONE', 'Gone', 1)"
    )
    sync_positions(seeded, pid, d[-40], {**load_book_weights(seeded, pid), 99: 0.25})

    second = run_optimize(s, as_of=d[-20], conn=seeded)
    b = _book(seeded, second.books["min_var"])
    t = b["params"]["turnover"]
    assert t["outside_panel_weight"] == pytest.approx(0.25)
    panel_moves = sum(
        abs(load_book_weights(seeded, second.books["min_var"]).get(a, 0.0) - w)
        for a, w in load_book_weights(seeded, pid).items()
        if a != 99
    ) + sum(
        w
        for a, w in load_book_weights(seeded, second.books["min_var"]).items()
        if a not in load_book_weights(seeded, pid)
    )
    assert panel_moves + 0.25 <= 0.5 + 1e-5  # the cap includes the sale of the name that left
    assert float(b["turnover"]) == pytest.approx(panel_moves + 0.25, abs=1e-5)

    # a cap no feasible book can meet is relaxed to the smallest feasible value, on the record
    tight = run_optimize(s.model_copy(update={"turnover_cap": 0.01}), as_of=d[-30], conn=seeded)
    p0 = _book(seeded, tight.books["min_var"])["params"]
    assert p0["turnover"]["applied"] is False  # first book of that chain
    sync_positions(
        seeded,
        tight.books["min_var"],
        d[-30],
        {**load_book_weights(seeded, tight.books["min_var"]), 99: 0.25},
    )
    again = run_optimize(s.model_copy(update={"turnover_cap": 0.01}), as_of=d[-20], conn=seeded)
    p = _book(seeded, again.books["min_var"])["params"]
    relax = [r for r in p["relaxations"] if r["cap"] == "turnover"]
    assert len(relax) == 1 and relax[0]["requested"] == 0.01
    assert relax[0]["effective"] > 0.25 and p["turnover"]["cap_effective"] == relax[0]["effective"]
    assert float(_book(seeded, again.books["min_var"])["turnover"]) <= relax[0]["effective"] + 1e-5


# -- evaluate: perf-v3 ----------------------------------------------------------------------


def _perf(conn: Database, pid: int) -> list[tuple[str, float, float, float | None]]:
    return [
        (r["date"], r["realized_return"], r["cumulative_return"], r["active_return"])
        for r in conn.execute(
            "SELECT date, realized_return, cumulative_return, active_return "
            "FROM quant_benchmark_performance WHERE portfolio_id = ? AND engine_version = ? "
            "ORDER BY date",
            (pid, PERF_ENGINE_VERSION),
        )
    ]


def test_the_cost_is_deducted_once_on_the_first_forward_day(
    seeded: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert PERF_ENGINE_VERSION == "perf-v3"
    d = _dates(seeded)
    s = _settings(objectives=["min_var"])
    first = run_optimize(s, as_of=d[-40], conn=seeded)
    second = run_optimize(s, as_of=d[-20], conn=seeded)
    pid1, pid2 = first.books["min_var"], second.books["min_var"]

    assert quant_evaluate.TURNOVER_COST_BPS == 10.0
    with monkeypatch.context() as m:  # a gross run, for the comparison only
        m.setattr(quant_evaluate, "TURNOVER_COST_BPS", 0.0)
        run_evaluate(s, date_from=d[-40], date_to=d[-1], conn=seeded)
    gross1, gross2 = _perf(seeded, pid1), _perf(seeded, pid2)
    book_rows = {r[0]: r[1] for r in seeded.execute("SELECT id, params_json FROM quant_portfolio")}
    run_evaluate(s, date_from=d[-40], date_to=d[-1], conn=seeded)  # 10 bps by definition
    net1, net2 = _perf(seeded, pid1), _perf(seeded, pid2)
    assert len(net1) == len(gross1) > 5 and len(net2) == len(gross2) > 5

    # the first book of a chain trades in from cash: cost = bps * sum |w| = 10 bps
    w1 = load_book_weights(seeded, pid1)
    cost1 = 10 / 10_000 * sum(abs(v) for v in w1.values())
    assert gross1[0][1] - net1[0][1] == pytest.approx(cost1, abs=1e-12)
    assert cost1 == pytest.approx(0.001, abs=1e-5)
    # ... once: every later day's realized return is untouched, and the cumulative differs by that one cost
    for g, n in zip(gross1[1:], net1[1:], strict=True):
        assert n[1] == pytest.approx(g[1], abs=1e-12)
    assert net1[0][2] == pytest.approx(gross1[0][2] - cost1, abs=1e-12)
    ratio = [(1 + n[2]) / (1 + g[2]) for g, n in zip(gross1, net1, strict=True)]
    assert ratio == pytest.approx([ratio[0]] * len(ratio), abs=1e-10)
    # active return is net too
    assert gross1[0][3] - net1[0][3] == pytest.approx(cost1, abs=1e-12)  # type: ignore[operator]

    # the second book trades only the difference from the first
    cost2 = 10 / 10_000 * turnover_between(load_book_weights(seeded, pid2), w1)
    assert gross2[0][1] - net2[0][1] == pytest.approx(cost2, abs=1e-12)
    assert cost2 < cost1
    # the cost is recorded on the evaluate run, keyed by portfolio id; the book row optimize wrote
    # is not touched, by this run or a re-run
    assert {
        r[0]: r[1] for r in seeded.execute("SELECT id, params_json FROM quant_portfolio")
    } == book_rows
    run = json.loads(
        seeded.execute(
            "SELECT params_json FROM quant_run WHERE command = 'evaluate' ORDER BY id DESC"
        ).fetchone()[0]
    )["turnover_cost"]
    cost = run[str(pid2)]
    assert cost["first_of_chain"] is False and cost["previous_portfolio_id"] == pid1
    assert cost["bps"] == 10 and cost["cost"] == pytest.approx(cost2)
    assert cost["engine_version"] == "perf-v3"
    assert run[str(pid1)]["first_of_chain"] is True
    assert run[str(pid1)]["turnover"] == pytest.approx(sum(w1.values()))
    assert "turnover_cost" not in json.loads(book_rows[pid1])


def test_perf_v3_means_ten_bps_by_definition_and_a_rerun_changes_nothing(seeded: Database) -> None:
    """PR #133 review: no flag and no setting can make a perf-v3 row carry another cost."""
    assert "turnover_cost_bps" not in QuantSettings.model_fields
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["evaluate", "--turnover-cost-bps", "15"])
    d = _dates(seeded)
    s = _settings(objectives=["min_var"])
    pid = run_optimize(s, as_of=d[-20], conn=seeded).books["min_var"]
    run_evaluate(s, date_from=d[-20], date_to=d[-1], conn=seeded)
    first = _perf(seeded, pid)
    run_evaluate(s, date_from=d[-20], date_to=d[-1], conn=seeded)
    assert _perf(seeded, pid) == first
    gross_first_day = first[0][1] + 10 / 10_000 * sum(load_book_weights(seeded, pid).values())
    assert gross_first_day > first[0][1]


def test_perf_v2_rows_stay_stored_and_the_view_shows_perf_v3(seeded: Database) -> None:
    d = _dates(seeded)
    s = _settings(objectives=["min_var"])
    pid = run_optimize(s, as_of=d[-20], conn=seeded).books["min_var"]
    seeded.execute(
        "INSERT INTO quant_benchmark_performance (portfolio_id, date, realized_return, "
        "cumulative_return, engine_version, computed_at) VALUES (?, ?, 0.01, 0.01, 'perf-v2', "
        "'2000-01-01T00:00:00+00:00')",
        (pid, d[-19]),
    )
    seeded.commit()
    run_evaluate(s, date_from=d[-20], date_to=d[-1], conn=seeded)
    assert (
        seeded.execute(
            "SELECT COUNT(*) FROM quant_benchmark_performance WHERE engine_version = 'perf-v2'"
        ).fetchone()[0]
        == 1
    )
    shown = seeded.execute(
        "SELECT engine_version FROM v_quant_benchmark_performance WHERE portfolio_id = ? AND date = ?",
        (pid, d[-19]),
    ).fetchall()
    assert [r[0] for r in shown] == ["perf-v3"]


def test_a_capped_chain_pays_against_its_own_previous_book_and_the_live_book_pays_nothing(
    seeded: Database,
) -> None:
    d = _dates(seeded)
    s = _settings(objectives=["min_var"], turnover_cap=0.3)
    a = run_optimize(s, as_of=d[-40], conn=seeded).books["min_var"]
    b = run_optimize(s, as_of=d[-20], conn=seeded).books["min_var"]
    plain = run_optimize(_settings(objectives=["min_var"]), as_of=d[-30], conn=seeded).books[
        "min_var"
    ]
    cost_b = _turnover_cost(seeded, b, load_book_weights(seeded, b))
    assert (
        cost_b is not None and cost_b["previous_portfolio_id"] == a
    )  # not the uncapped book at d[-30]
    assert cost_b["turnover"] <= 0.3 + 1e-5
    live = insert_portfolio(seeded, _row(d[-20], kind="live_book", engine="opt-v2"))
    assert _turnover_cost(seeded, live, {1: 1.0}) is None
    assert plain != a


def test_a_variant_book_is_never_current_whatever_the_compute_order(seeded: Database) -> None:
    """PR #133 review: only the default configuration (equilibrium, no cap) can be
    ``is_current``; a variant computed after it neither takes over nor moves ``v_quant_vs_live``."""
    d = _dates(seeded)
    day = d[-20]
    s = _settings(objectives=["min_var", "tangency"])
    seeded.execute(
        "INSERT INTO portfolio_position (asset_id, valid_from, weight) VALUES (1, ?, 0.6), (6, ?, 0.4)",
        (day, day),
    )
    seeded.commit()
    default = run_optimize(s, as_of=day, conn=seeded)

    def current() -> dict[int, int]:
        return {
            int(r["id"]): int(r["is_current"])
            for r in seeded.execute(
                "SELECT id, is_current FROM v_quant_portfolio WHERE as_of = ?", (day,)
            )
        }

    def vs_live() -> list[tuple[object, ...]]:
        return [
            tuple(r)
            for r in seeded.execute(
                "SELECT kind, ticker, benchmark_weight, live_weight FROM v_quant_vs_live "
                "WHERE as_of = ? AND is_current = 1 ORDER BY kind, ticker",
                (day,),
            )
        ]

    before_current, before_live = current(), vs_live()
    assert {before_current[p] for p in default.books.values()} == {1}
    assert before_live

    variants = [
        run_optimize(s.model_copy(update={"ret_estimator": "hist_mean"}), as_of=day, conn=seeded),
        run_optimize(s.model_copy(update={"turnover_cap": 0.5}), as_of=day, conn=seeded),
        run_optimize(
            s.model_copy(update={"ret_estimator": "james_stein", "turnover_cap": 0.3}),
            as_of=day,
            conn=seeded,
        ),
    ]
    after = current()
    assert len(after) == 8
    for pid in default.books.values():
        assert after[pid] == 1  # the default stays current, though it was computed first
    for v in variants:
        for pid in v.books.values():
            assert after[pid] == 0
    assert vs_live() == before_live
