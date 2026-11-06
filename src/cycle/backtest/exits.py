"""Declared terminal values for assets whose point-in-time stint ends (Phase 1 §1.1).

The mandate is index-constrained: the investable universe is the S&P 500 point-in-time
membership, and a held asset whose stint ends is sold at the close of its last session in the
base (``BACKTEST_SPEC`` §0, "Exits from the panel").  That price series ends at the index exit,
so the engine sees a missing return on the first session after the last observed close
(``t_rec``).  This module states what the asset earns on ``t_rec`` from a *public* table and a
small set of standards keyed by ``valuation_basis`` (Shumway 1997):

* ``observed_last_close`` / ``approximation_last_close`` -> 0 (sold at the exit close);
* ``documented_worthless`` -> -1.0 (the session after the last close);
* ``imputed_reference`` -> -0.299 (Shumway 1997, Table V, line 5);
* ``unobservable_frozen`` -> no value change and no trade, until a documented terminal event;
* ``documented_recovery`` -> an asset-specific value from a private file outside the repo.

The public table carries **no per-asset return** (only ticker, asset id, stint end, last
return session, type, valuation basis, source URL and note).  The asset-specific recovery
value of a ``documented_recovery`` case lives only in the private file
``/workspaces/thesis/data/private/exit_recoveries.json``; its sha256 is recorded in the run
manifest and it never enters the repository.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

#: The exit types of plan §1.1.
EXIT_TYPES: Final = (
    "index_removal",
    "acquisition_cash",
    "acquisition_stock",
    "failure",
    "suspension",
    "other",
)

#: The valuation bases of plan §1.1.
VALUATION_BASES: Final = (
    "observed_last_close",
    "approximation_last_close",
    "documented_worthless",
    "documented_recovery",
    "imputed_reference",
    "unobservable_frozen",
)

#: The standards, keyed by ``valuation_basis`` (Shumway 1997, Table V).  A value here is a
#: code constant, never a per-row figure: the public table has no return column at all.
TERMINAL_RETURN: Final[dict[str, float]] = {
    "observed_last_close": 0.0,
    "approximation_last_close": 0.0,
    "documented_worthless": -1.0,
    "imputed_reference": -0.299,
}

#: Types that may use each basis.  The mapping is the plan's table read literally; a row that
#: violates it is refused instead of being silently interpreted.
_ALLOWED_BASES: Final[dict[str, frozenset[str]]] = {
    "index_removal": frozenset({"observed_last_close"}),
    "other": frozenset({"observed_last_close"}),
    "acquisition_cash": frozenset({"approximation_last_close"}),
    "acquisition_stock": frozenset({"approximation_last_close"}),
    "failure": frozenset({"documented_worthless", "documented_recovery", "imputed_reference"}),
    "suspension": frozenset({"unobservable_frozen"}),
}

#: The private file carrying the asset-specific ``documented_recovery`` values.
PRIVATE_RECOVERIES_PATH: Final = Path("/workspaces/thesis/data/private/exit_recoveries.json")
#: The manifest key the private file's sha256 is recorded under.
PRIVATE_RECOVERIES_KEY: Final = "exit_recoveries_sha256"


class ExitTableError(RuntimeError):
    """The exit table, or the private recovery file, is malformed or inconsistent."""


@dataclass(frozen=True)
class ExitEvent:
    """One public exit-event row (no return value)."""

    ticker: str
    asset_id: int | None
    stint_end: str
    last_return_session: str | None
    exit_type: str
    valuation_basis: str
    source: str
    note: str = ""
    #: ILMN's series continues after its 2024 exit because the asset rejoins the index in
    #: 2026.  A held position must still be sold at the exit close, so the row carries an
    #: explicit recognition date and the engine overrides the return on that session.
    force_exit: bool = False
    recognition_date: str | None = None


@dataclass(frozen=True)
class TerminalExit:
    """What the engine needs for one asset: the value and the session it is recognised.

    ``terminal_return`` is ``None`` only for ``unobservable_frozen``: the position is frozen at
    its last close and never traded until (unless) a documented terminal event is recognised.
    ``recognised_on`` is ``t_rec`` for the standard bases, the distribution date for a
    ``documented_recovery``, and ``None`` for a position frozen to the window end.
    """

    terminal_return: float | None
    recognised_on: str | None
    basis: str
    #: True when the exit must fire on ``recognised_on`` even though a return row exists
    #: (ILMN's continuing series; see ``ExitEvent.force_exit``).
    force: bool = False

    @property
    def frozen(self) -> bool:
        return self.basis == "unobservable_frozen"

    @property
    def frozen_until_recognition(self) -> bool:
        """A documented recovery keeps the position frozen until its distribution date."""
        return self.basis == "documented_recovery"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_date(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ExitTableError(f"{label}: expected an ISO date, got {value!r}")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ExitTableError(f"{label}: expected an ISO date, got {value!r}") from exc
    return value


def _require_asset_id(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ExitTableError(f"{label}: asset_id must be a positive integer or null")
    return value


def load_exit_table(  # noqa: C901, PLR0912 - one validator per row field
    path: str | Path,
) -> list[ExitEvent]:
    """Read and validate the public table (``docs/exit_events.json``).

    ``unknown`` (or any type outside the plan's six) is refused: the plan says an unsourced
    event is reported, not guessed, so a run must never start on a table with one.
    """
    table_path = Path(path).expanduser().resolve()
    if not table_path.is_file():
        raise ExitTableError(f"exit table {table_path} does not exist")
    try:
        payload = json.loads(table_path.read_text())
    except json.JSONDecodeError as exc:
        raise ExitTableError(f"exit table {table_path} is not JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise ExitTableError(f"exit table {table_path} has no 'events' list")
    seen: set[tuple[str, str]] = set()
    events: list[ExitEvent] = []
    for index, raw in enumerate(payload["events"]):
        label = f"{table_path.name}[{index}]"
        if not isinstance(raw, dict):
            raise ExitTableError(f"{label}: not an object")
        ticker = str(raw.get("ticker", "")).strip().upper()
        if not ticker:
            raise ExitTableError(f"{label}: empty ticker")
        exit_type = str(raw.get("type", ""))
        if exit_type not in EXIT_TYPES:
            raise ExitTableError(
                f"{label} {ticker}: type {exit_type!r} is not one of {EXIT_TYPES} "
                "(an unsourced event must be reported as unknown, never run)"
            )
        basis = str(raw.get("valuation_basis", ""))
        if basis not in VALUATION_BASES:
            raise ExitTableError(f"{label} {ticker}: unknown valuation_basis {basis!r}")
        if basis not in _ALLOWED_BASES[exit_type]:
            raise ExitTableError(
                f"{label} {ticker}: {exit_type} cannot use valuation_basis {basis!r}"
            )
        source = str(raw.get("source", "")).strip()
        if not source:
            raise ExitTableError(f"{label} {ticker}: no public source")
        stint_end = _require_date(raw.get("stint_end"), f"{label} {ticker} stint_end")
        raw_last = raw.get("last_return_session")
        if raw_last is None:
            # INFO and WRK were acquired with no price held in the archive: the row records
            # the absence rather than inventing a session.  Such an asset can never be held.
            last = None
        else:
            last = _require_date(raw_last, f"{label} {ticker} last_return_session")
            if last > stint_end:
                raise ExitTableError(
                    f"{label} {ticker}: last_return_session {last} is after stint_end {stint_end}"
                )
        key = (ticker, stint_end)
        if key in seen:
            raise ExitTableError(f"{label} {ticker}: duplicate event for {stint_end}")
        seen.add(key)
        events.append(
            ExitEvent(
                ticker=ticker,
                asset_id=_require_asset_id(raw.get("asset_id"), f"{label} {ticker}"),
                stint_end=stint_end,
                last_return_session=last,
                exit_type=exit_type,
                valuation_basis=basis,
                source=source,
                note=str(raw.get("note", "")),
                force_exit=bool(raw.get("force_exit", False)),
                recognition_date=(
                    _require_date(raw["recognition_date"], f"{label} {ticker} recognition_date")
                    if raw.get("recognition_date") is not None
                    else None
                ),
            )
        )
    return events


@dataclass(frozen=True)
class RecoveryValue:
    """One private ``documented_recovery`` row: the value, its date, and its documentation."""

    terminal_return: float
    distribution_date: str
    source: str


def load_private_recoveries(  # noqa: C901 - one validator
    path: str | Path,
) -> dict[str, RecoveryValue]:
    """Read the private per-asset recovery values, keyed by ticker.

    The file is optional only while the public table has no ``documented_recovery`` row; a
    caller that needs one passes the path and a missing file is an error.
    """
    file_path = Path(path).expanduser().resolve()
    if not file_path.is_file():
        raise ExitTableError(f"private recovery file {file_path} does not exist")
    try:
        payload = json.loads(file_path.read_text())
    except json.JSONDecodeError as exc:
        raise ExitTableError(f"private recovery file {file_path} is not JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("recoveries"), list):
        raise ExitTableError(f"private recovery file {file_path} has no 'recoveries' list")
    out: dict[str, RecoveryValue] = {}
    for index, raw in enumerate(payload["recoveries"]):
        label = f"{file_path.name}[{index}]"
        if not isinstance(raw, dict):
            raise ExitTableError(f"{label}: not an object")
        ticker = str(raw.get("ticker", "")).strip().upper()
        if not ticker:
            raise ExitTableError(f"{label}: empty ticker")
        raw_value = raw.get("terminal_return")
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise ExitTableError(f"{label} {ticker}: terminal_return must be a number")
        value = float(raw_value)
        if not math.isfinite(value) or value < -1.0:
            raise ExitTableError(f"{label} {ticker}: terminal_return must be >= -1.0")
        date = _require_date(raw.get("distribution_date"), f"{label} {ticker} date")
        source = str(raw.get("source", "")).strip()
        if not source:
            raise ExitTableError(f"{label} {ticker}: no public source")
        if ticker in out:
            raise ExitTableError(f"{label} {ticker}: duplicate recovery row")
        out[ticker] = RecoveryValue(value, date, source)
    return out


def policies_by_asset(
    events: Sequence[ExitEvent],
    recoveries: Mapping[str, RecoveryValue] | None = None,
) -> dict[int, TerminalExit]:
    """The engine's map ``asset_id -> TerminalExit`` from the declared table.

    * A standard basis takes its code constant and is recognised at ``last_return_session``'s
      next session (the caller's calendar supplies the date; the engine applies it on the
      first held session after the last stored return).
    * ``documented_recovery`` needs a private row and is recognised on its distribution date;
      any *other* event of a ticker in that state is refused.
    * ``unobservable_frozen`` has no terminal return and no recognition date: frozen to the
      window end unless the private table supplies a later documented event.
    """
    out: dict[int, TerminalExit] = {}
    recoveries = recoveries or {}
    for event in events:
        if event.asset_id is None:
            continue
        if event.valuation_basis == "documented_recovery":
            recovery = recoveries.get(event.ticker)
            if recovery is None:
                raise ExitTableError(
                    f"{event.ticker}: documented_recovery has no private row in "
                    "exit_recoveries.json (the asset-specific value never enters the repo)"
                )
            if event.last_return_session is None:
                raise ExitTableError(
                    f"{event.ticker}: a documented_recovery needs a last return session"
                )
            if recovery.distribution_date <= event.last_return_session:
                raise ExitTableError(
                    f"{event.ticker}: distribution date {recovery.distribution_date} must be "
                    f"after the last return session {event.last_return_session}"
                )
            policy = TerminalExit(
                terminal_return=recovery.terminal_return,
                recognised_on=recovery.distribution_date,
                basis=event.valuation_basis,
            )
        elif event.valuation_basis == "unobservable_frozen":
            policy = TerminalExit(
                terminal_return=None, recognised_on=None, basis="unobservable_frozen"
            )
        else:
            if event.force_exit and event.recognition_date is None:
                raise ExitTableError(
                    f"{event.ticker}: force_exit needs a recognition_date (the first session "
                    "after the stint's last session in the base)"
                )
            policy = TerminalExit(
                terminal_return=TERMINAL_RETURN[event.valuation_basis],
                # A standard exit is recognised on the first held session without a return;
                # a forced exit names the session (its series would otherwise continue).
                recognised_on=event.recognition_date,
                basis=event.valuation_basis,
                force=event.force_exit,
            )
        existing = out.get(event.asset_id)
        if existing is not None and existing != policy:
            raise ExitTableError(
                f"{event.ticker}: asset {event.asset_id} has two different exit policies"
            )
        out[event.asset_id] = policy
    return out


def recovery_manifest(path: str | Path | None, events: Iterable[ExitEvent]) -> dict[str, Any]:
    """The manifest fields for the private recovery file (sha256, never the values)."""
    needs_recovery = any(event.valuation_basis == "documented_recovery" for event in events)
    if path is None:
        if needs_recovery:
            raise ExitTableError(
                "the exit table has a documented_recovery row: --exit-recoveries is required"
            )
        return {}
    file_path = Path(path).expanduser().resolve()
    return {PRIVATE_RECOVERIES_KEY: sha256_file(file_path)}
