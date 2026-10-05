"""T-136: the orchestrator builds the book with ``build_book`` (live and REPLAY), records it, and the
CLI exposes the construction flags. Hermetic: a seeded in-memory database, no network, no LLM."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from conftest import write_universe_db
from portfolio_common.db import Database

from cycle import orchestrator
from cycle.cli import _settings, build_parser
from cycle.cli import main as cycle_main
from cycle.config import CycleSettings
from cycle.construction import BookCandidate
from cycle.orchestrator import PreferencesNeedDryRun, run_replay, run_selection
from cycle.rules import seed_catalog
from cycle.state import ConstructionMismatch

DAY = "2026-06-30"
TOL = 1e-9
FIVE = ["Financials", "Energy", "Tech", "Health", "Utilities", "Industrials", "Materials"]


def seed_cohort(
    conn: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, members: list[tuple[str, str]]
) -> Database:
    """*members* is ``(ticker, sector)`` in rank order: FUNDAMENTAL and TECHNICAL both fall with
    the index, so the blended rank is the list order."""
    sectors = sorted({s for _, s in members})
    for sid, name in enumerate(sectors, start=1):
        conn.execute("INSERT INTO sectors (id, name) VALUES (?, ?)", (sid, name))
    udb = write_universe_db(tmp_path / "universe.db", [(t, "2026-01-01", None) for t, _ in members])
    monkeypatch.setenv("KG_UNIVERSE_DB", str(udb))
    start = date(2026, 2, 1)
    for i, (ticker, sector) in enumerate(members, start=1):
        conn.execute(
            "INSERT INTO assets (id, ticker, sector_id) VALUES (?, ?, ?)",
            (i, ticker, sectors.index(sector) + 1),
        )
        fid = conn.execute(
            "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, period_end, "
            "filing_date, available_at, retrieved_at) VALUES (?, '10-K', 2025, 'FY2025', "
            "'2025-12-31', '2026-01-30', '2026-02-02', '2026-02-01T00:00:00Z') RETURNING id",
            (i,),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO score_snapshot (asset_id, score_type, raw_value, normalized_score, "
            "event_time, computed_at, model, run_kind, filing_id, available_at) VALUES "
            "(?, 'FUNDAMENTAL', ?, ?, '2025-12-31', '2026-02-01T00:00:00Z', 'seed', 'analysis', ?, "
            "'2026-02-02')",
            (i, 100 - 3 * i, 100 - 3 * i, fid),
        )
        for d in range(120):
            day = (start + timedelta(days=d)).isoformat()
            conn.execute(
                "INSERT INTO price_observation (asset_id, obs_date, close, atr_14, "
                "realized_vol_90d, max_drawdown_90d, momentum_63d, momentum_21d, event_time, "
                "computed_at, engine_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'2026-06-30T00:00:00Z', 'priceobs-v1')",
                (
                    i,
                    day,
                    100.0 + d,
                    2.0 + 0.1 * i,
                    0.10 + 0.01 * i,
                    -0.02 * i,
                    0.5 - 0.03 * i,
                    0.1 - 0.005 * i,
                    day,
                ),
            )
    conn.commit()
    return conn


def veto(conn: Database, asset_id: int, severity: str) -> None:
    """An open stint raised the day before the cycle: visible at the T-1 cutoff."""
    seed_catalog(conn)
    conn.execute(
        "INSERT INTO veto (asset_id, rule_id, severity, raised_on, last_seen_on, detected_at) "
        "VALUES (?, ?, ?, '2026-06-29', '2026-06-29', '2026-06-29T00:00:00Z')",
        (asset_id, "LEVERAGE_EXTREME" if severity == "HARD" else "EARNINGS_MISSING", severity),
    )
    conn.commit()


def settings(**kw: object) -> CycleSettings:
    return CycleSettings(db_path=Path(":memory:"), **kw)  # type: ignore[arg-type]


def params(conn: Database, cycle_type: str = "SELECTION") -> dict:
    row = conn.execute(
        "SELECT params_json FROM cycle_run WHERE cycle_type = ? AND cycle_date = ?",
        (cycle_type, DAY),
    ).fetchone()
    return dict(json.loads(row["params_json"]))


def live_weights(conn: Database) -> dict[str, float]:
    return {
        r["ticker"]: r["weight"]
        for r in conn.execute(
            "SELECT a.ticker, p.weight FROM portfolio_position p JOIN assets a ON a.id = p.asset_id "
            "WHERE p.valid_to IS NULL"
        )
    }


@pytest.fixture
def cohort(memory_db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Database:
    """14 names over 7 sectors, two each, ranked T01 > T02 > ... > T14."""
    members = [(f"T{i:02d}", FIVE[(i - 1) % 7]) for i in range(1, 15)]
    return seed_cohort(memory_db, monkeypatch, tmp_path, members)


@pytest.fixture
def production_shape(
    memory_db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Database:
    """cycle_run 1's shape: the top 10 hold 4 Financials and 3 Energy (the live book broke 0.30)."""
    sectors = (
        ["Financials"] * 4
        + ["Energy"] * 3
        + ["Tech", "Health", "Utilities"]
        + ["Industrials", "Materials", "Staples"]
    )
    members = [(f"P{i:02d}", s) for i, s in enumerate(sectors, start=1)]
    return seed_cohort(memory_db, monkeypatch, tmp_path, members)


# -- the orchestrator calls build_book, live and REPLAY -----------------------------------------


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []
    real = orchestrator.build_book

    def wrapper(cands: list[BookCandidate], **kw: object) -> object:
        calls.append({"cands": list(cands), **kw})
        return real(cands, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(orchestrator, "build_book", wrapper)
    return calls


def test_the_live_and_the_replay_path_both_call_build_book(
    cohort: Database, spy: list[dict]
) -> None:
    run_selection(settings(top_n=10), DAY, conn=cohort)
    run_replay(settings(top_n=10), DAY, conn=cohort)
    assert len(spy) == 2
    for call in spy:
        assert call["n"] == 10 and call["scheme"] == "score_tilt"
        assert call["max_name_weight"] is None and call["max_sector_weight"] == 0.30
        assert {c.ticker for c in call["cands"]} == {f"T{i:02d}" for i in range(1, 15)}
        first = next(c for c in call["cands"] if c.ticker == "T01")
        assert first.sector == "Financials" and first.veto == "none"  # the sector *name*
        assert first.realized_vol_90d == pytest.approx(0.11)
    # the live book and the replay book are separate tables, both built by it
    assert len(live_weights(cohort)) == 10
    assert cohort.execute("SELECT COUNT(*) FROM portfolio_position_replay").fetchone()[0] == 10


def test_a_hard_vetoed_name_is_passed_in_marked_and_never_held(
    cohort: Database, spy: list[dict]
) -> None:
    veto(cohort, 1, "HARD")  # T01
    veto(cohort, 2, "SOFT")  # T02
    run_selection(settings(top_n=10), DAY, conn=cohort)
    by_ticker = {c.ticker: c for c in spy[0]["cands"]}
    assert by_ticker["T01"].veto == "HARD" and by_ticker["T02"].veto == "SOFT"
    assert by_ticker["T03"].veto == "none"
    assert "T01" not in live_weights(cohort)


def test_the_legacy_defaults_are_gone() -> None:
    s = CycleSettings(db_path=Path(":memory:"))
    assert s.weight_scheme == "score_tilt"
    assert s.max_name_weight is None  # not 0.10: an explicit 0.10 would override 1.5/N silently
    assert s.max_sector_weight == 0.30 and s.top_n == 30
    assert not s.has_preferences


# -- what is recorded -----------------------------------------------------------------------------


def test_params_json_and_the_view_carry_the_effective_default_caps(cohort: Database) -> None:
    run_selection(settings(top_n=10), DAY, conn=cohort)
    p = params(cohort)
    assert p["max_name_weight"] == pytest.approx(0.15)  # 1.5/N at N = 10, not 0.10
    assert p["max_sector_weight"] == pytest.approx(0.30)
    assert (p["weight_scheme"], p["top_n"], p["n_held"], p["shortfall"]) == (
        "score_tilt",
        10,
        10,
        0,
    )
    assert p["relaxations"] == [] and p["refused_pins"] == [] and p["overflow_tickers"] == []
    row = cohort.execute(
        "SELECT scheme_id, top_n, max_name_weight, max_sector_weight FROM v_weight_scheme "
        "WHERE cycle_type = 'SELECTION'"
    ).fetchone()
    assert tuple(row) == ("score_tilt", 10, pytest.approx(0.15), pytest.approx(0.30))
    w = live_weights(cohort)
    assert max(w.values()) <= 0.15 + TOL and max(w.values()) > 0.10  # no longer the 0.10 ceiling
    assert min(w.values()) >= 0.05 - TOL and sum(w.values()) == pytest.approx(1.0)


def test_an_explicit_cap_is_recorded_as_given(cohort: Database) -> None:
    run_selection(
        settings(top_n=10, max_name_weight=0.12, max_sector_weight=0.40), DAY, conn=cohort
    )
    p = params(cohort)
    assert p["max_name_weight"] == pytest.approx(0.12)
    assert p["max_sector_weight"] == pytest.approx(0.40)
    assert p["construction"]["max_name_weight"] == 0.12  # what was asked for
    assert max(live_weights(cohort).values()) <= 0.12 + TOL


def test_a_relaxation_the_shortfall_and_the_overflow_are_recorded(
    cohort: Database, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("WARNING")
    run_selection(settings(top_n=20, max_name_weight=0.01), DAY, conn=cohort)  # 14 eligible < 20
    p = params(cohort)
    assert p["n_held"] == 14 and p["shortfall"] == 6
    assert [r["cap"] for r in p["relaxations"]] == ["name"]
    assert p["relaxations"][0]["requested"] == 0.01
    assert p["max_name_weight"] == pytest.approx(1 / 14)  # the effective cap, not the request
    assert p["construction"]["max_name_weight"] == 0.01
    assert "relaxed" in caplog.text and "never padded" in caplog.text


def test_a_hard_pin_is_refused_and_a_soft_pin_flagged_and_both_recorded(
    cohort: Database, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("WARNING")
    veto(cohort, 13, "HARD")  # T13
    veto(cohort, 14, "SOFT")  # T14
    run_replay(settings(top_n=10, pins=("T13", "t14")), DAY, conn=cohort)
    p = params(cohort, "REPLAY")
    (refused,) = p["refused_pins"]
    assert refused["ticker"] == "T13" and "HARD" in refused["reason"]
    (flagged,) = p["flagged_pins"]
    assert flagged["ticker"] == "T14" and "SOFT" in flagged["reason"]
    assert p["pins"] == ["T13", "t14"]
    replay = {
        r["ticker"]
        for r in cohort.execute(
            "SELECT a.ticker FROM portfolio_position_replay p JOIN assets a ON a.id = p.asset_id "
            "WHERE p.valid_to IS NULL"
        )
    }
    assert "T14" in replay and "T13" not in replay and len(replay) == 10
    assert "pin T13 refused" in caplog.text and "pin T14 flagged" in caplog.text


# -- preferences never reach the live book ----------------------------------------------------


def test_a_writing_select_with_preferences_is_refused_before_any_write(cohort: Database) -> None:
    with pytest.raises(PreferencesNeedDryRun, match="dry-run"):
        run_selection(settings(top_n=10, pins=("T05",)), DAY, conn=cohort)
    assert cohort.execute("SELECT COUNT(*) FROM cycle_run").fetchone()[0] == 0
    assert cohort.execute("SELECT COUNT(*) FROM portfolio_position").fetchone()[0] == 0
    for prefs in (
        {"exclude": ("T01",)},
        {"exclude_sectors": ("Tech",)},
        {"only_sectors": ("Tech",)},
    ):
        with pytest.raises(PreferencesNeedDryRun):
            run_selection(settings(top_n=10, **prefs), DAY, conn=cohort)


def test_the_cli_refuses_a_writing_select_with_preferences_with_a_clear_message(
    cohort: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_on(cohort, monkeypatch)
    assert cycle_main(["select", "--analysis-date", DAY, "--pin", "T05,T06", "--top-n", "10"]) == 1
    err = capsys.readouterr().err
    assert "--dry-run" in err and "backfill" in err and "decision-support" in err
    assert cohort.execute("SELECT COUNT(*) FROM portfolio_position").fetchone()[0] == 0


def _cli_on(conn: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the CLI at *conn* (a seeded in-memory database) instead of KG_FINANCIAL_DB."""
    monkeypatch.setattr("cycle.cli.CycleSettings.load", settings)
    monkeypatch.setattr("cycle.cli.make_hook", lambda _s: None)
    for name in ("run_selection", "run_replay"):
        real = getattr(orchestrator, name)
        monkeypatch.setattr(
            f"cycle.cli.{name}", lambda s, d, _r=real, **k: _r(s, d, conn=conn, **k)
        )


def test_select_dry_run_prints_the_book_and_writes_no_positions(
    cohort: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    veto(cohort, 13, "HARD")
    _cli_on(cohort, monkeypatch)
    code = cycle_main(
        [
            "select", "--analysis-date", DAY, "--top-n", "10", "--dry-run",
            "--pin", "T12,T13", "--exclude", "T01", "--exclude-sectors", "Materials",
        ]
    )  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0
    assert "dry run" in out and "ticker" in out and "sector" in out and "weight" in out
    table = out.split("ticker")[1].split("total")[0]
    assert "T12" in table and "T01" not in table and "Materials" not in table
    assert "name cap 0.15" in out and "sector cap" in out
    assert "REFUSED pin T13" in out  # the HARD pin, with its reason
    assert cohort.execute("SELECT COUNT(*) FROM portfolio_position").fetchone()[0] == 0
    done = {
        r["step"] for r in cohort.execute("SELECT step FROM cycle_checkpoint WHERE status='done'")
    }
    assert "positions" not in done  # a later real run must still find it pending
    assert "construction" not in params(cohort)  # nothing about the book is recorded


def test_a_dry_run_leaves_the_live_book_and_a_later_real_select_alone(
    cohort: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_on(cohort, monkeypatch)
    assert cycle_main(["select", "--analysis-date", DAY, "--top-n", "10"]) == 0
    live = live_weights(cohort)
    rows = cohort.execute("SELECT * FROM portfolio_position ORDER BY id").fetchall()
    # a dry run with other settings, same date: refused for nothing, writes nothing
    assert (
        cycle_main(["select", "--analysis-date", DAY, "--top-n", "4", "--dry-run", "--pin", "T14"])
        == 0
    )
    assert [tuple(r) for r in cohort.execute("SELECT * FROM portfolio_position ORDER BY id")] == [
        tuple(r) for r in rows
    ]
    assert live_weights(cohort) == live  # the old dry run (top_n = 0) closed every position


def test_a_real_select_after_a_dry_run_still_writes_its_book(
    cohort: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_on(cohort, monkeypatch)
    assert (
        cycle_main(["select", "--analysis-date", DAY, "--top-n", "6", "--dry-run", "--pin", "T09"])
        == 0
    )
    assert cycle_main(["select", "--analysis-date", DAY, "--top-n", "10"]) == 0
    assert len(live_weights(cohort)) == 10
    assert params(cohort)["n_held"] == 10


def test_backfill_accepts_preferences_and_writes_only_the_replay_book(cohort: Database) -> None:
    run_replay(
        settings(top_n=8, pins=("T14",), exclude=("T01",), only_sectors=tuple(FIVE)),
        DAY,
        conn=cohort,
    )
    assert cohort.execute("SELECT COUNT(*) FROM portfolio_position").fetchone()[0] == 0
    assert cohort.execute("SELECT COUNT(*) FROM portfolio_position_replay").fetchone()[0] == 8
    p = params(cohort, "REPLAY")
    assert p["pins"] == ["T14"] and p["exclude"] == ["T01"] and p["only_sectors"] == FIVE


# -- resume ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changed",
    [
        {"top_n": 8},
        {"weight_scheme": "equal"},
        {"max_name_weight": 0.2},
        {"max_sector_weight": 0.5},
    ],
)
def test_resuming_with_other_construction_settings_is_refused(
    cohort: Database, changed: dict[str, object]
) -> None:
    run_selection(settings(top_n=10), DAY, conn=cohort)
    before = live_weights(cohort)
    with pytest.raises(ConstructionMismatch, match="mix two books"):
        run_selection(settings(**{"top_n": 10, **changed}), DAY, conn=cohort)
    assert live_weights(cohort) == before
    # the same settings resume fine
    run_selection(settings(top_n=10), DAY, conn=cohort)


def test_resuming_a_replay_with_other_preferences_is_refused(cohort: Database) -> None:
    run_replay(settings(top_n=10, pins=("T14",)), DAY, conn=cohort)
    with pytest.raises(ConstructionMismatch, match="pins"):
        run_replay(settings(top_n=10, pins=("T13",)), DAY, conn=cohort)
    with pytest.raises(ConstructionMismatch):
        run_replay(settings(top_n=10), DAY, conn=cohort)
    run_replay(
        settings(top_n=10, pins=("t14",)), DAY, conn=cohort
    )  # case-insensitive: the same book


def test_a_monitoring_run_and_a_run_that_never_built_a_book_are_not_constrained(
    cohort: Database,
) -> None:
    orchestrator.run_monitoring(settings(top_n=10), DAY, conn=cohort)
    orchestrator.run_monitoring(settings(top_n=3, weight_scheme="equal"), DAY, conn=cohort)
    assert "construction" not in params(cohort, "MONITORING")


# -- the production shape, through the orchestrator ------------------------------------------


def test_the_production_shape_holds_both_caps_through_the_orchestrator(
    production_shape: Database,
) -> None:
    run_selection(settings(top_n=10), DAY, conn=production_shape)
    w = live_weights(production_shape)
    sector = {
        r["ticker"]: r["name"]
        for r in production_shape.execute(
            "SELECT a.ticker, s.name FROM assets a JOIN sectors s ON s.id = a.sector_id"
        )
    }
    by_sector: dict[str, float] = {}
    for t, x in w.items():
        by_sector[sector[t]] = by_sector.get(sector[t], 0.0) + x
    assert len(w) == 10 and sum(w.values()) == pytest.approx(1.0, abs=TOL)
    assert "P04" not in w  # the fourth Financial is skipped...
    assert "P11" in w  # ...and the next-ranked name from another sector is taken
    assert by_sector["Financials"] <= 0.30 + TOL and by_sector["Energy"] <= 0.30 + TOL
    assert all(0.05 - TOL <= x <= 0.15 + TOL for x in w.values())  # not 0.1167 / 0.35
    p = params(production_shape)
    assert p["relaxations"] == [] and p["max_sector_weight"] == pytest.approx(0.30)


# -- CLI surface -------------------------------------------------------------------------------


def test_select_and_backfill_accept_the_construction_flags_and_document_them(
    capsys: pytest.CaptureFixture[str],
) -> None:
    flags = [
        "--top-n", "12", "--max-name-weight", "0.07", "--max-sector-weight", "0.4",
        "--weight-scheme", "equal", "--pin", "A, b", "--exclude", "C", "--exclude-sectors",
        "Tech,Energy", "--only-sectors", "Health",
    ]  # fmt: skip
    for head in (["select"], ["backfill", "--from", "2026-01-01", "--to", "2026-02-01"]):
        a = build_parser().parse_args([*head, *flags])
        assert (a.top_n, a.max_name_weight, a.max_sector_weight, a.weight_scheme) == (
            12, 0.07, 0.4, "equal",
        )  # fmt: skip
        assert a.pins == ("A", "b") and a.exclude == ("C",)
        assert a.exclude_sectors == ("Tech", "Energy") and a.only_sectors == ("Health",)
    for head in (["select"], ["backfill"]):
        with pytest.raises(SystemExit):
            build_parser().parse_args([*head, "--help"])
        text = " ".join(capsys.readouterr().out.split())
        for needle in ("--pin", "--exclude-sectors", "--only-sectors", "score_tilt", "1.5/N"):
            assert needle in text
        assert "decision support" in text
    with pytest.raises(SystemExit):
        build_parser().parse_args(["select", "--weight-scheme", "bogus"])


def test_the_flags_reach_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cycle.cli.CycleSettings.load", settings)
    a = build_parser().parse_args(
        [
            "select",
            "--max-name-weight",
            "0.07",
            "--pin",
            "A,B",
            "--only-sectors",
            "Tech",
            "--dry-run",
        ]
    )
    s = _settings(a)
    assert s.max_name_weight == 0.07 and s.pins == ("A", "B") and s.only_sectors == ("Tech",)
    assert s.weight_scheme == "score_tilt" and s.has_preferences
    assert not _settings(build_parser().parse_args(["select"])).has_preferences


def test_a_contradictory_preference_is_reported_not_a_traceback(
    cohort: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_on(cohort, monkeypatch)
    code = cycle_main(
        ["select", "--analysis-date", DAY, "--dry-run", "--pin", "T05", "--exclude", "T05"]
    )
    assert code == 1 and "pin and exclude" in capsys.readouterr().err
