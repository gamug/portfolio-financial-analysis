"""T-137: the benchmark's caps follow N (``quant.caps``), copied from and pinned to ``cycle``'s rule.

Hermetic: pure functions, seeded random covariances, and the in-memory quant pipeline.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from portfolio_common.db import Database

from cycle.construction import (
    BookCandidate,
    _feasible_sector_cap,
    _resolve_caps,
    _Selection,
    build_book,
)
from quant.caps import EffectiveCaps, feasible_sector_cap, resolve_caps
from quant.cli import _settings, build_parser
from quant.config import QuantSettings
from quant.db import load_sector_of
from quant.optimize import Constraints, min_variance, risk_parity
from quant.persist import effective_caps, run_build_risk_model, run_optimize
from quant.returns import run_build_returns

TOL = 1e-6  # the solver's tolerance (Clarabel's default is 1e-8; the books are stored to 1e-8)


# -- the rule ---------------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 3, 10, 20, 30])
@pytest.mark.parametrize("panel", [1, 2, 5, 20, 29, 30, 31, 100])
def test_n_held_and_the_default_name_cap(n: int, panel: int) -> None:
    caps = resolve_caps(n, panel, [panel])  # one sector: only the name cap is under test
    assert caps.n_held == min(n, panel)
    assert caps.max_name_weight == pytest.approx(min(1.0, 1.5 / caps.n_held))  # no 0.10 floor
    assert caps.top_n == n
    assert not [r for r in caps.relaxations if r.cap == "name"]


def test_the_pilot_one_panel_gets_a_name_cap_of_0075_not_the_degenerate_005() -> None:
    caps = resolve_caps(30, 20, [10, 10])
    assert caps.n_held == 20 and caps.max_name_weight == pytest.approx(0.075)


def test_at_n30_with_a_panel_of_30_or_more_the_cap_is_the_old_constant() -> None:
    for panel in (30, 31, 100, 500):
        assert resolve_caps(30, panel, [panel // 2, panel - panel // 2]).max_name_weight == (
            pytest.approx(0.05)
        )


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ([10, 10], 0.5),  # two sectors can never hold 0.30 each
        ([20], 1.0),  # one sector holds everything
        ([5, 5, 5, 5], 0.30),  # four sectors: 4 x 0.30 >= 1, no relaxation
        ([7, 7, 6], 1 / 3),  # three sectors: the smallest feasible cap
        ([1, 1, 1], 1 / 3),  # N = 3, one name per sector
    ],
)
def test_few_sector_panels_relax_the_sector_cap_to_the_smallest_feasible_value(
    counts: list[int], expected: float
) -> None:
    caps = resolve_caps(30, sum(counts), counts)
    assert caps.max_sector_weight == pytest.approx(expected)
    relaxed = [r for r in caps.relaxations if r.cap == "sector"]
    if expected > 0.30 + 1e-9:
        (r,) = relaxed
        assert (r.requested, r.effective) == (0.30, pytest.approx(expected)) and r.reason
    else:
        assert not relaxed


def test_assets_without_a_sector_are_uncapped_free_capacity() -> None:
    # 10 + 10 classified, 10 free at a 0.05 name cap: 0.5 of capacity needs only 0.25 per sector
    caps = resolve_caps(30, 30, [10, 10])
    assert caps.max_sector_weight == pytest.approx(0.30)  # 0.30 + 0.30 + 10 * 0.05 = 1.1 >= 1
    assert not caps.relaxations
    assert resolve_caps(30, 30, [10, 5]).max_sector_weight == pytest.approx(0.30)


def test_explicit_caps_override_the_derived_ones() -> None:
    caps = resolve_caps(30, 100, [50, 50], max_name_weight=0.04, max_sector_weight=0.55)
    assert (caps.max_name_weight, caps.max_sector_weight) == (0.04, 0.55)
    assert not caps.relaxations
    # an explicit name cap above the derived one wins too
    assert resolve_caps(10, 100, [100], max_name_weight=0.5).max_name_weight == 0.5
    # an explicit 1.0 is "no per-name cap"
    assert resolve_caps(30, 100, [100], max_name_weight=1.0).max_name_weight == 1.0


def test_an_explicit_name_cap_below_one_over_n_held_is_relaxed_and_recorded() -> None:
    caps = resolve_caps(30, 20, [10, 10], max_name_weight=0.01)
    assert caps.max_name_weight == pytest.approx(1 / 20)
    (r,) = [r for r in caps.relaxations if r.cap == "name"]
    assert (r.requested, r.effective) == (0.01, pytest.approx(0.05)) and "1/20" in r.reason
    # N bounds n_held: at N = 10 a panel of 100 needs 1/10
    assert resolve_caps(10, 100, [50, 50], max_name_weight=0.05).max_name_weight == (
        pytest.approx(0.1)
    )


def test_no_sector_cap_when_none_is_asked_for() -> None:
    caps = resolve_caps(30, 20, [20], max_sector_weight=None)
    assert caps.max_sector_weight is None and not caps.relaxations


def test_the_record_carries_everything() -> None:
    rec = resolve_caps(30, 20, [20]).record()
    assert set(rec) == {"top_n", "n_held", "max_name_weight", "max_sector_weight", "relaxations"}
    assert rec["n_held"] == 20 and rec["relaxations"][0]["cap"] == "sector"
    assert json.loads(json.dumps(rec)) == rec


@pytest.mark.parametrize(
    "kwargs",
    [
        {"top_n": 0, "panel_size": 5},
        {"top_n": 5, "panel_size": 0},
        {"top_n": 5, "panel_size": 5, "max_name_weight": 0.0},
        {"top_n": 5, "panel_size": 5, "max_name_weight": 1.5},
        {"top_n": 5, "panel_size": 5, "max_sector_weight": 0.0},
        {"top_n": 5, "panel_size": 2, "sector_counts": [3]},
    ],
)
def test_invalid_inputs_are_errors(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        resolve_caps(**kwargs)  # type: ignore[arg-type]


def test_a_resolved_cap_always_leaves_a_feasible_book() -> None:
    """Whatever the panel, the effective caps admit a fully invested book."""
    rng = random.Random(11)
    for _ in range(3000):
        sectors = [rng.randint(1, 40) for _ in range(rng.randint(1, 10))]
        free = rng.choice([0, 0, 0, rng.randint(1, 15)])
        panel = sum(sectors) + free
        n = rng.choice([1, 2, 3, 5, 10, 20, 30, 50])
        explicit = rng.choice([None, None, 0.01, 0.04, 0.1, 0.5, 1.0])
        sector = rng.choice([0.1, 0.2, 0.3, 0.5, 1.0])
        caps = resolve_caps(n, panel, sectors, max_name_weight=explicit, max_sector_weight=sector)
        assert caps.max_name_weight * caps.n_held >= 1.0 - 1e-9
        assert caps.max_sector_weight is not None and caps.max_sector_weight >= sector - 1e-12
        capacity = sum(min(caps.max_sector_weight, k * caps.max_name_weight) for k in sectors)
        assert capacity + free * caps.max_name_weight >= 1.0 - 1e-9
        # a relaxation is recorded exactly when a cap moved
        assert (caps.max_sector_weight > sector + 1e-12) == any(
            r.cap == "sector" for r in caps.relaxations
        )


# -- pinned to cycle's rule -------------------------------------------------------------------


def _cycle_name_rule(n: int, panel: int, explicit: float | None) -> tuple[float, list[tuple]]:
    """``cycle``'s effective name cap and name relaxations for a panel-sized candidate list."""
    sectors = ["A", "B", "C", "D", "E", "F", "G", "H"]
    cands = [
        BookCandidate(i, f"T{i:03d}", 100.0 - i, sectors[i % len(sectors)]) for i in range(panel)
    ]
    res = build_book(cands, n=n, scheme="score_tilt", max_name_weight=explicit)
    return res.max_name_weight, [
        (r.cap, r.requested, r.effective) for r in res.relaxations if r.cap == "name"
    ]


@pytest.mark.parametrize("n", [1, 3, 10, 20, 30])
@pytest.mark.parametrize("panel", [1, 2, 7, 20, 29, 30, 45, 100])
@pytest.mark.parametrize("explicit", [None, 0.005, 0.03, 0.05, 0.2, 1.0])
def test_the_name_cap_rule_equals_cycles(n: int, panel: int, explicit: float | None) -> None:
    cycle_cap, cycle_relaxations = _cycle_name_rule(n, panel, explicit)
    q = resolve_caps(n, panel, [panel], max_name_weight=explicit)
    assert q.n_held == min(n, panel)
    assert q.max_name_weight == pytest.approx(cycle_cap)
    assert [
        (r.cap, r.requested, r.effective) for r in q.relaxations if r.cap == "name"
    ] == pytest.approx(cycle_relaxations)


def test_the_sector_feasibility_rule_equals_cycles_over_a_grid() -> None:
    rng = random.Random(5)
    for _ in range(4000):
        counts = [rng.randint(1, 45) for _ in range(rng.randint(1, 9))]
        hi = rng.choice([0.02, 0.05, 0.075, 0.1, 0.15, 0.3, 0.5, 1.0])
        requested = rng.choice([0.1, 0.2, 0.3, 0.5, 1.0])
        assert feasible_sector_cap(counts, hi, requested) == pytest.approx(
            _feasible_sector_cap(counts, 0.0, hi, requested)  # cycle's, with a band floor of 0
        )


def test_the_whole_rule_equals_cycles_resolve_caps_for_the_same_inputs() -> None:
    """Counts that sum to n_held (cycle's chosen set) and a band floor that does not bind: the
    effective name cap, sector cap and relaxations are identical."""
    rng = random.Random(9)
    checked = 0
    for _ in range(3000):
        counts = [rng.randint(1, 12) for _ in range(rng.randint(1, 8))]
        total = sum(counts)
        n = rng.choice(
            [total, total + 3, total + 20]
        )  # n_held = total: the chosen set is the panel
        explicit = rng.choice([None, 0.02, 0.1, 0.3])
        requested = rng.choice([0.2, 0.3, 0.5])
        chosen = [
            BookCandidate(i, f"T{i}", 1.0, f"S{s}")
            for s, k in enumerate(counts)
            for i in range(sum(counts[:s]), sum(counts[:s]) + k)
        ]
        sel = _Selection(chosen, total, [], [], [])
        cyc = _resolve_caps(sel, "score_tilt", explicit, requested)
        if max(counts) * 0.5 / total > requested:  # cycle's band floor would bind: out of scope
            continue
        checked += 1
        q = resolve_caps(n, total, counts, max_name_weight=explicit, max_sector_weight=requested)
        assert q.n_held == total
        assert q.max_name_weight == pytest.approx(cyc.name)
        assert q.max_sector_weight == pytest.approx(cyc.sector)
        assert [(r.cap, r.requested, r.effective) for r in q.relaxations] == pytest.approx(
            [(r.cap, r.requested, r.effective) for r in cyc.relaxations]
        )
    assert checked > 1500


# -- the optimizer uses the effective caps -----------------------------------------------------


def _random_cov(n: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    vols = rng.uniform(0.10, 0.45, n)
    a = rng.normal(size=(n, n + 5))
    corr = np.corrcoef(a)
    return corr * np.outer(vols, vols)


def test_the_pilot_one_degeneracy_is_gone_min_var_is_not_equal_weight() -> None:
    sigma = _random_cov(20, seed=3)
    old = min_variance(sigma, constraints=Constraints(max_name_weight=0.05, max_sector_weight=None))
    assert np.allclose(old.weights, 0.05, atol=1e-6)  # 20 x 0.05 = 1: forced equal weight

    caps = resolve_caps(30, 20, [10, 10], max_sector_weight=None)
    assert caps.max_name_weight == pytest.approx(0.075)
    new = min_variance(
        sigma, constraints=Constraints(max_name_weight=caps.max_name_weight, max_sector_weight=None)
    )
    assert new.weights.max() <= 0.075 + TOL and new.weights.sum() == pytest.approx(1.0, abs=TOL)
    assert new.weights.max() > 0.06 and new.weights.min() < 0.04  # it tilts: not 0.05 everywhere
    assert new.expected_vol < old.expected_vol  # and the freedom buys variance


def test_risk_parity_respects_both_effective_caps_after_projection() -> None:
    sig = np.diag([0.0004, 0.0004, 0.09, 0.09, 0.09, 0.09])  # two tiny-vol names carry the ERC book
    ids = list(range(6))
    sector_of: dict[int, int | None] = {0: 1, 1: 1, 2: 2, 3: 2, 4: 3, 5: 3}
    caps = resolve_caps(30, 6, [2, 2, 2])
    cons = Constraints(
        max_name_weight=caps.max_name_weight,
        max_sector_weight=caps.max_sector_weight,
        sector_of=sector_of,
        asset_ids=ids,
    )
    res = risk_parity(sig, constraints=cons)
    assert res.weights.sum() == pytest.approx(1.0, abs=TOL)
    assert res.weights.max() <= caps.max_name_weight + TOL
    assert caps.max_sector_weight is not None
    assert res.weights[0] + res.weights[1] <= caps.max_sector_weight + TOL


# -- through the pipeline: every objective, the recording, the settings --------------------------


def _settings_for(**over: object) -> QuantSettings:
    base: dict[str, object] = {
        "db_path": Path(":memory:"),
        "lookback_days": 200,
        "min_history_days": 140,
        "liquidity_min_dollar_volume": 0.0,
        "objectives": ["min_var", "tangency", "target_vol", "risk_parity", "frontier"],
        "frontier_k": 5,
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


def _as_of(conn: Database) -> str:
    return str(conn.execute("SELECT MAX(obs_date) FROM quant_return_daily").fetchone()[0])


def _book(conn: Database, pid: int) -> dict[int, tuple[float, int]]:
    return {
        int(r["asset_id"]): (float(r["weight"]), int(r["sector_id"]))
        for r in conn.execute(
            "SELECT p.asset_id, p.weight, a.sector_id FROM quant_position p "
            "JOIN assets a ON a.id = p.asset_id WHERE p.portfolio_id = ? AND p.valid_to IS NULL",
            (pid,),
        )
    }


def test_every_objective_respects_the_effective_caps_or_records_the_relaxation(
    seeded: Database,
) -> None:
    """Six assets in two sectors at the default N = 30: n_held = 6 (name cap 0.25) and two sectors
    cannot hold 0.30 each, so the sector cap is relaxed to 0.5 -- and every book obeys exactly that."""
    as_of = _as_of(seeded)
    settings = _settings_for()
    res = run_optimize(settings, as_of=as_of, conn=seeded)
    run = json.loads(
        seeded.execute("SELECT params_json FROM quant_run WHERE command = 'optimize'").fetchone()[0]
    )
    caps = run["caps"]
    assert caps["n_held"] == 6 and caps["max_name_weight"] == pytest.approx(0.25)
    assert caps["max_sector_weight"] == pytest.approx(0.5)
    assert [r["cap"] for r in caps["relaxations"]] == ["sector"]
    assert set(res.books) == {"min_var", "tangency", "target_vol", "risk_parity"}
    for kind, pid in res.books.items():
        book = _book(seeded, pid)
        assert sum(w for w, _ in book.values()) == pytest.approx(1.0, abs=1e-4), kind
        assert max(w for w, _ in book.values()) <= caps["max_name_weight"] + TOL, kind
        by_sector: dict[int, float] = {}
        for w, s in book.values():
            by_sector[s] = by_sector.get(s, 0.0) + w
        assert max(by_sector.values()) <= caps["max_sector_weight"] + TOL, kind
    # the frontier points obey them too
    pts = seeded.execute("SELECT weights_json FROM quant_frontier_point").fetchall()
    assert len(pts) >= 1
    sectors = {
        int(r["id"]): int(r["sector_id"])
        for r in seeded.execute("SELECT id, sector_id FROM assets")
    }
    for p in pts:
        pw = {int(k): v for k, v in json.loads(p[0]).items()}
        assert max(pw.values()) <= caps["max_name_weight"] + TOL
        s_tot: dict[int, float] = {}
        for a, v in pw.items():
            s_tot[sectors[a]] = s_tot.get(sectors[a], 0.0) + v
        assert max(s_tot.values()) <= caps["max_sector_weight"] + TOL


def test_the_effective_caps_are_recorded_on_the_run_and_on_each_book(seeded: Database) -> None:
    as_of = _as_of(seeded)
    run_optimize(
        _settings_for(objectives=["min_var", "risk_parity"], top_n=4), as_of=as_of, conn=seeded
    )
    for (blob,) in seeded.execute("SELECT params_json FROM quant_portfolio").fetchall():
        rec = json.loads(blob)
        assert rec["top_n"] == 4 and rec["n_held"] == 4
        assert rec["max_name_weight"] == pytest.approx(1.5 / 4)
        assert rec["max_sector_weight"] == pytest.approx(0.5)  # 2 sectors: relaxed from 0.30
        assert rec["relaxations"] and rec["relaxations"][0]["requested"] == 0.30
    run = json.loads(
        seeded.execute("SELECT params_json FROM quant_run WHERE command = 'optimize'").fetchone()[0]
    )
    assert run["top_n"] == 4 and run["caps"]["n_held"] == 4  # the requested N and what it became
    assert run["max_name_weight"] is None  # what was asked for: derive


def test_an_explicit_cap_is_used_and_an_infeasible_one_is_relaxed_on_the_record(
    seeded: Database,
) -> None:
    as_of = _as_of(seeded)
    run_optimize(
        _settings_for(objectives=["min_var"], max_name_weight=0.30, max_sector_weight=0.80),
        as_of=as_of,
        conn=seeded,
    )
    rec = json.loads(seeded.execute("SELECT params_json FROM quant_portfolio").fetchone()[0])
    assert rec["max_name_weight"] == 0.30 and rec["max_sector_weight"] == 0.80
    assert rec["relaxations"] == []
    # a name cap that cannot sum to 1 over six names (6 x 0.1 < 1) is relaxed to 1/6
    seeded.execute("DELETE FROM quant_portfolio")
    run_optimize(
        _settings_for(objectives=["min_var"], max_name_weight=0.10, max_sector_weight=None),
        as_of=as_of,
        conn=seeded,
    )
    rec = json.loads(seeded.execute("SELECT params_json FROM quant_portfolio").fetchone()[0])
    assert rec["max_name_weight"] == pytest.approx(1 / 6)
    assert [r["cap"] for r in rec["relaxations"]] == ["name"]
    assert rec["relaxations"][0]["requested"] == 0.10


def test_build_risk_model_records_the_panels_caps_too(seeded: Database) -> None:
    run_build_risk_model(_settings_for(top_n=5), as_of=_as_of(seeded), conn=seeded)
    run = json.loads(
        seeded.execute(
            "SELECT params_json FROM quant_run WHERE command = 'build-risk-model'"
        ).fetchone()[0]
    )
    assert run["caps"]["top_n"] == 5 and run["caps"]["n_held"] == 5
    assert run["caps"]["max_name_weight"] == pytest.approx(0.3)


def test_effective_caps_reads_the_panels_sectors(seeded: Database) -> None:
    ids = [int(r[0]) for r in seeded.execute("SELECT id FROM assets ORDER BY id")]
    caps = effective_caps(QuantSettings(db_path=Path(":memory:")), ids, load_sector_of(seeded, ids))
    assert isinstance(caps, EffectiveCaps) and caps.n_held == 6
    assert caps.max_sector_weight == pytest.approx(0.5)  # two sectors


def test_a_20_asset_panel_at_the_default_n_is_not_equal_weight_end_to_end(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=20, n_days=280, with_dividends=True)
    run_build_returns(
        QuantSettings(db_path=Path(":memory:")),
        date_from="2000-01-01",
        date_to="2100-01-01",
        conn=conn,
    )
    as_of = _as_of(conn)
    s = _settings_for(objectives=["min_var"], max_sector_weight=None)
    res = run_optimize(s, as_of=as_of, conn=conn)
    w = [v for v, _ in _book(conn, res.books["min_var"]).values()]
    caps = json.loads(conn.execute("SELECT params_json FROM quant_portfolio").fetchone()[0])
    assert caps["max_name_weight"] == pytest.approx(0.075)
    assert max(w) <= 0.075 + TOL
    assert not np.allclose(w, 0.05, atol=1e-4)  # the pilot-1 degeneracy: every name at 0.05


# -- settings and CLI -----------------------------------------------------------------------------


def test_the_quant_defaults_follow_the_rule() -> None:
    s = QuantSettings(db_path=Path(":memory:"))
    assert s.max_name_weight is None  # derived: 0.05 passed explicitly would override the rule
    assert s.top_n == 30 and s.max_sector_weight == 0.30
    assert s.optimizer_engine_version == "opt-v2"


def test_the_cap_flags_exist_on_optimize_and_build_risk_model_and_reach_the_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "quant.cli.QuantSettings.load", lambda: QuantSettings(db_path=Path(":memory:"))
    )
    for cmd in ("optimize", "build-risk-model"):
        a = build_parser().parse_args(
            [cmd, "--top-n", "12", "--max-name-weight", "0.07", "--max-sector-weight", "0.4"]
        )
        s = _settings(a)
        assert (s.top_n, s.max_name_weight, s.max_sector_weight) == (12, 0.07, 0.4)
        assert _settings(build_parser().parse_args([cmd])).max_name_weight is None
        with pytest.raises(SystemExit):
            build_parser().parse_args([cmd, "--help"])
        text = " ".join(capsys.readouterr().out.split())
        for needle in ("--top-n", "--max-name-weight", "--max-sector-weight", "1.5 / min(N"):
            assert needle in text
