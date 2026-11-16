"""Phase 1 §1.1: the declared exit table and the engine's terminal values.

All figures are synthetic; no price or return of a real asset appears here."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cycle.backtest.engine import (
    ARM_MARKOWITZ_BASE,
    ArmSpec,
    EngineError,
    EngineInput,
    simulate,
)
from cycle.backtest.exits import (
    ExitEvent,
    ExitTableError,
    TerminalExit,
    load_exit_table,
    load_private_recoveries,
    policies_by_asset,
    recovery_manifest,
)

D0 = "2022-01-05"
FORMATION = "2022-01-06"
T1 = "2022-01-07"
T2 = "2022-01-10"
T3 = "2022-01-11"


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "ticker": "AAA",
        "asset_id": 1,
        "stint_end": "2022-01-07",
        "last_return_session": "2022-01-06",
        "type": "index_removal",
        "valuation_basis": "observed_last_close",
        "source": "https://example.invalid/announcement",
        "note": "synthetic",
    }
    row.update(overrides)
    return row


def _write_table(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    path = tmp_path / "exit_events.json"
    path.write_text(json.dumps({"version": 1, "events": rows}))
    return path


def test_the_public_table_has_no_return_column_and_refuses_an_unknown_type(
    tmp_path: Path,
) -> None:
    path = _write_table(tmp_path, [_row()])
    events = load_exit_table(path)
    assert events[0].valuation_basis == "observed_last_close"
    assert not hasattr(events[0], "terminal_return")
    bad = _write_table(tmp_path, [_row(type="unknown")])
    with pytest.raises(ExitTableError, match="unknown"):
        load_exit_table(bad)


def test_the_table_refuses_a_missing_source_and_a_wrong_basis(tmp_path: Path) -> None:
    with pytest.raises(ExitTableError, match="no public source"):
        load_exit_table(_write_table(tmp_path, [_row(source="")]))
    with pytest.raises(ExitTableError, match="cannot use valuation_basis"):
        load_exit_table(_write_table(tmp_path, [_row(valuation_basis="documented_worthless")]))


def test_the_table_refuses_a_duplicate_event(tmp_path: Path) -> None:
    with pytest.raises(ExitTableError, match="duplicate"):
        load_exit_table(_write_table(tmp_path, [_row(), _row()]))


def _event(**overrides: object) -> ExitEvent:
    row = _row(**overrides)
    asset_id = row["asset_id"]
    if not isinstance(asset_id, int):
        raise TypeError("the synthetic row needs an integer asset id")
    return ExitEvent(
        ticker=str(row["ticker"]),
        asset_id=asset_id,
        stint_end=str(row["stint_end"]),
        last_return_session=str(row["last_return_session"]),
        exit_type=str(row["type"]),
        valuation_basis=str(row["valuation_basis"]),
        source=str(row["source"]),
        note=str(row["note"]),
    )


def test_standard_bases_map_to_the_declared_constants() -> None:
    rows = [
        _event(ticker="AAA", asset_id=1),
        _event(
            ticker="BBB",
            asset_id=2,
            type="acquisition_cash",
            valuation_basis="approximation_last_close",
        ),
        _event(ticker="CCC", asset_id=3, type="failure", valuation_basis="documented_worthless"),
        _event(ticker="DDD", asset_id=4, type="failure", valuation_basis="imputed_reference"),
        _event(ticker="EEE", asset_id=5, type="suspension", valuation_basis="unobservable_frozen"),
    ]
    policies = policies_by_asset(rows)
    assert policies[1].terminal_return == 0.0
    assert policies[2].terminal_return == 0.0
    assert policies[3].terminal_return == -1.0
    assert policies[4].terminal_return == pytest.approx(-0.299)
    assert policies[5].frozen and policies[5].terminal_return is None


def test_a_documented_recovery_needs_the_private_file_and_its_date(tmp_path: Path) -> None:
    table = load_exit_table(
        _write_table(
            tmp_path,
            [_row(type="failure", valuation_basis="documented_recovery")],
        )
    )
    with pytest.raises(ExitTableError, match="no private row"):
        policies_by_asset(table)
    private = tmp_path / "recoveries.json"
    private.write_text(
        json.dumps(
            {
                "recoveries": [
                    {
                        "ticker": "AAA",
                        "terminal_return": -0.4,
                        "distribution_date": "2022-01-06",  # not after the last return
                        "source": "https://example.invalid/court",
                    }
                ]
            }
        )
    )
    with pytest.raises(ExitTableError, match="must be after"):
        policies_by_asset(table, load_private_recoveries(private))


def _input(
    *,
    sessions: list[str],
    returns: dict[str, dict[int, float]],
    targets: dict[str, dict[str, dict[int, float]]],
    exits: dict[int, TerminalExit] | None = None,
) -> EngineInput:
    return EngineInput(
        sessions=sessions,
        returns=returns,
        rf={},
        targets=targets,
        arms=[ArmSpec(ARM_MARKOWITZ_BASE, ARM_MARKOWITZ_BASE)],
        formation_date=FORMATION,
        decision_dates=[D0],
        exits=exits or {},
    )


def _two_asset_case(policy: TerminalExit) -> EngineInput:
    """Half the book exits on T1; the other half is a flat cash-like asset."""
    sessions = [D0, FORMATION, T1, T2]
    returns = {
        D0: {1: 0.0, 2: 0.0},
        FORMATION: {1: 0.0, 2: 0.0},
        T1: {2: 0.0},
        T2: {2: 0.0},
    }
    targets = {ARM_MARKOWITZ_BASE: {D0: {1: 0.5, 2: 0.5}}}
    return _input(sessions=sessions, returns=returns, targets=targets, exits={1: policy})


def test_observed_last_close_is_the_old_behaviour_with_the_residual_turnover() -> None:
    inp = _two_asset_case(
        TerminalExit(terminal_return=0.0, recognised_on=T1, basis="observed_last_close")
    )
    arm = simulate(inp).arms[ARM_MARKOWITZ_BASE]
    assert arm.performance[1].gross_return == pytest.approx(0.0)
    # The exiting half is liquidated at its drifted weight: 0.5 of the book.
    assert arm.executions[-1].date == T1
    assert arm.executions[-1].turnover == pytest.approx(0.5)
    assert arm.executions[-1].cost_return == pytest.approx(10 / 10_000 * 0.5)
    assert arm.weights[1].final_target == {2: pytest.approx(0.5)}
    assert arm.weights[1].cash_weight == pytest.approx(0.5)
    assert arm.performance[2].gross_return == pytest.approx(0.0)


def test_imputed_reference_applies_the_shumway_value_and_charges_the_residual() -> None:
    inp = _two_asset_case(
        TerminalExit(terminal_return=-0.299, recognised_on=T1, basis="imputed_reference")
    )
    arm = simulate(inp).arms[ARM_MARKOWITZ_BASE]
    # gross = -0.299 on half the book; the exiting half is still 100% of the reduced book,
    # so the residual turnover is 0.5 / (1 - 0.1495) = 0.5879...
    gross = -0.299 * 0.5
    residual = 0.5 * (1.0 - 0.299) / (1.0 + gross)
    assert arm.performance[1].gross_return == pytest.approx(gross)
    assert arm.executions[-1].turnover == pytest.approx(residual)
    assert arm.executions[-1].cost_return == pytest.approx(10 / 10_000 * residual)


def test_documented_worthless_has_zero_residual_and_pays_no_cost() -> None:
    inp = _two_asset_case(
        TerminalExit(terminal_return=-1.0, recognised_on=T1, basis="documented_worthless")
    )
    arm = simulate(inp).arms[ARM_MARKOWITZ_BASE]
    assert arm.performance[1].gross_return == pytest.approx(-0.5)
    # the worthless half has residual 0: its sale is free (plan §1.1)
    t1 = next(execution for execution in arm.executions if execution.date == T1)
    assert t1.turnover == pytest.approx(0.0)
    assert t1.cost_return == pytest.approx(0.0)
    assert arm.weights[1].final_target == {1: pytest.approx(0.0), 2: pytest.approx(1.0)}


def test_a_full_book_going_to_zero_is_refused_not_silently_repaired() -> None:
    sessions = [D0, FORMATION, T1]
    returns = {D0: {1: 0.0}, FORMATION: {1: 0.0}, T1: {}}
    targets = {ARM_MARKOWITZ_BASE: {D0: {1: 1.0}}}
    inp = _input(
        sessions=sessions,
        returns=returns,
        targets=targets,
        exits={1: TerminalExit(-1.0, T1, "documented_worthless")},
    )
    with pytest.raises(EngineError, match="wipes the book out"):
        simulate(inp)


def test_a_frozen_position_is_not_traded_and_keeps_its_value() -> None:
    inp = _two_asset_case(
        TerminalExit(terminal_return=None, recognised_on=None, basis="unobservable_frozen")
    )
    arm = simulate(inp).arms[ARM_MARKOWITZ_BASE]
    assert arm.performance[1].gross_return == pytest.approx(0.0)
    assert arm.executions[-1].turnover == pytest.approx(0.0)
    # both halves remain: the frozen name is still held, the tradable name is not sold
    assert set(arm.weights[1].final_target) == {1, 2}
    assert arm.weights[1].final_target[1] == pytest.approx(0.5)


def test_a_frozen_position_scales_a_later_rebalance_instead_of_being_sold() -> None:
    sessions = [D0, FORMATION, T1, T2, T3]
    returns = {
        D0: {1: 0.0, 2: 0.0},
        FORMATION: {1: 0.0, 2: 0.0},
        T1: {2: 0.0},
        T2: {2: 0.0},
        T3: {2: 0.0},
    }
    # the second decision (formed at T1's close) asks for all of asset 2
    targets = {ARM_MARKOWITZ_BASE: {D0: {1: 0.5, 2: 0.5}, T1: {2: 1.0}}}
    inp = _input(
        sessions=sessions,
        returns=returns,
        targets=targets,
        exits={1: TerminalExit(None, None, "unobservable_frozen")},
    )
    arm = simulate(inp).arms[ARM_MARKOWITZ_BASE]
    # T2 executes the target: the frozen half stays, the tradable half goes to 1 - 0.5.
    assert arm.weights[2].final_target[1] == pytest.approx(0.5)
    assert arm.weights[2].final_target[2] == pytest.approx(0.5)
    # the target already holds 0.5 of asset 2, so scaling to the free half trades nothing
    assert arm.executions[-1].turnover == pytest.approx(0.0)


def test_a_documented_recovery_stays_frozen_until_its_distribution_date() -> None:
    sessions = [D0, FORMATION, T1, T2, T3]
    returns = {
        D0: {1: 0.0, 2: 0.0},
        FORMATION: {1: 0.0, 2: 0.0},
        T1: {2: 0.0},
        T2: {2: 0.0},
        T3: {2: 0.0},
    }
    targets = {ARM_MARKOWITZ_BASE: {D0: {1: 0.5, 2: 0.5}}}
    inp = _input(
        sessions=sessions,
        returns=returns,
        targets=targets,
        exits={1: TerminalExit(-0.4, T3, "documented_recovery")},
    )
    arm = simulate(inp).arms[ARM_MARKOWITZ_BASE]
    assert arm.performance[1].gross_return == pytest.approx(0.0)  # frozen at T1
    t1 = next(execution for execution in arm.executions if execution.date == T1)
    assert t1.turnover == pytest.approx(0.0)
    assert arm.performance[2].gross_return == pytest.approx(0.0)  # still frozen at T2
    assert arm.performance[3].gross_return == pytest.approx(-0.2)  # -0.4 on half at T3
    t3 = next(execution for execution in arm.executions if execution.date == T3)
    residual = 0.5 * 0.6 / 0.8  # w (1 - 0.4) / (1 - 0.2)
    assert t3.turnover == pytest.approx(residual)


def test_the_private_manifest_records_only_the_sha_and_requires_the_file(
    tmp_path: Path,
) -> None:
    table = load_exit_table(
        _write_table(tmp_path, [_row(type="failure", valuation_basis="documented_recovery")])
    )
    with pytest.raises(ExitTableError, match="required"):
        recovery_manifest(None, table)
    private = tmp_path / "recoveries.json"
    private.write_text(json.dumps({"recoveries": []}))
    manifest = recovery_manifest(private, table)
    assert set(manifest) == {"exit_recoveries_sha256"}


def test_a_forced_exit_sells_a_continuing_series_at_the_recognition_session() -> None:
    sessions = [D0, FORMATION, T1, T2]
    # The series continues on T1 (ILMN re-enters in 2026): without the forced policy the
    # engine would keep earning T1's +10%.
    returns = {
        D0: {1: 0.0},
        FORMATION: {1: 0.0},
        T1: {1: 0.10},
        T2: {1: 0.10},
    }
    targets = {ARM_MARKOWITZ_BASE: {D0: {1: 1.0}}}
    inp = _input(
        sessions=sessions,
        returns=returns,
        targets=targets,
        exits={1: TerminalExit(0.0, T1, "observed_last_close", force=True)},
    )
    arm = simulate(inp).arms[ARM_MARKOWITZ_BASE]
    assert arm.performance[1].gross_return == pytest.approx(0.0)  # T1's +10% is not earned
    assert arm.executions[-1].date == T1
    assert arm.weights[1].final_target == {}
    assert arm.weights[1].cash_weight == pytest.approx(1.0)
    assert arm.performance[2].gross_return == pytest.approx(0.0)  # cash, not +10%
