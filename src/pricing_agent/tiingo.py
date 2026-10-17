"""The private Tiingo leaver-price import (T-153 part 3d).

33 lines that left the S&P 500 between 2022 and 2026 get no candle from the pricing gateway
(measured: ``docs/leaver_price_coverage_2019.json``), so their prices were fetched once, out of
band, into a private file the data-mining repo produced. This module loads that file into
``price_daily`` (and its dividends/splits into ``corporate_action``) so the backtest can price
those lines.

The rules (the brief's §1, decided 2026-10-15; the read contract is D5's):

1. **The input is the private file, never committed.** It is read from
   ``/workspaces/thesis/data/private/tiingo/``; a path inside a git work tree is refused, and the
   file's sha256 is checked against the manifest before anything is written.
2. **One vendor per line, never stitched.** A line with any stored non-Tiingo ``price_daily`` row
   is gateway-only: the import writes nothing for it, for the whole line, and names it in the
   run's record (``lines_gateway_only``). The reverse -- the gateway later serving a line that
   holds Tiingo rows -- is refused by :mod:`pricing_agent.pipeline` (``stage='tiingo_stitch'``).
3. **The data is never redistributed.** Rows carry ``source='tiingo'`` per row; every ``v_*``
   view that exposes a close or a value computed from one filters on it (D4/D5).
4. **The run record counts the lines and rows from Tiingo** (the import's own ``pricing_run``).
5. **If Tiingo says no**, :func:`remove_tiingo` deletes the file's rows; the run is redone without
   them and the gap is declared under PER-11.

Nothing in this module prints a price, a volume or a dividend value: the report carries counts,
dates and hashes only.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from portfolio_common.db import Database, DatabaseError

import kg_schema
from kg_schema import corporate_actions, provenance
from kg_schema.queries import WINDOW_START, connect_ro, members_over_range, resolve_asset_ids
from pricing_agent import db as pricing_db
from pricing_agent.config import Settings
from pricing_agent.pricing_client import Candle

#: The directory the data-mining fetch delivered the private file to (section 2 of the brief).
DEFAULT_TIINGO_DIR = Path("/workspaces/thesis/data/private/tiingo")
DEFAULT_DATA_FILE = "leaver_prices.csv"
DEFAULT_MANIFEST_FILE = "leaver_prices.manifest.json"

#: The vendor marker (D1/D4/D6): one literal, written to every row the import creates and read by
#: every view filter and by the removal procedure.
TIINGO_SOURCE = corporate_actions.TIINGO_SOURCE

#: The corporate-action engine the Tiingo rows are written under (D6). A distinct build, so the
#: gateway's own engines (``corpact-v1``/``corpact-v2``) keep their meaning and the gateway wins
#: when a line comes back; ``quant.db._ACTION_ENGINE_PRIORITY`` reads it after them.
TIINGO_CORPACT_ENGINE = corporate_actions.TIINGO_ENGINE

#: The report's ``kind`` on the import's own ``pricing_run`` row (D7), and the removal's.
IMPORT_RUN_KIND = "tiingo-import"
REMOVAL_RUN_KIND = "tiingo-removal"

_DIVIDEND_COLUMN = "divCash"
_SPLIT_COLUMN = "splitFactor"


class TiingoImportError(RuntimeError):
    """The import refused before writing anything."""


class TiingoFileInWorkTree(TiingoImportError):
    """The data file sits inside a git work tree (rule 1)."""


class TiingoFileMissing(TiingoImportError):
    """The data file or its manifest does not exist."""


class TiingoHashMismatch(TiingoImportError):
    """The data file's sha256 differs from the manifest's (rule 1)."""


class TiingoLineNotInManifest(TiingoImportError):
    """The file names a line the manifest's list does not."""

    def __init__(self, keys: list[str]) -> None:
        super().__init__(f"the file names {len(keys)} line(s) not in the manifest: {_shown(keys)}")
        self.keys = keys


class TiingoLineHasNoAsset(TiingoImportError):
    """A line's CIK has no ``assets`` row (identity first, then import)."""

    def __init__(self, keys: list[str]) -> None:
        super().__init__(
            f"{len(keys)} line(s) resolve to no asset row (run an ingestion step first so the "
            f"line's identity exists): {_shown(keys)}"
        )
        self.keys = keys


class TiingoSplitNotClassified(TiingoImportError):
    """A SPLIT row the tracked event list does not explain (T-153 part 3d, D10; MKT-02)."""

    def __init__(self, detail: list[str]) -> None:
        super().__init__(
            f"{len(detail)} imported SPLIT row(s) the tracked event list does not explain "
            f"({corporate_actions.EVENTS_CSV.name}); class nothing by a ratio's shape, and use no "
            f"default class -- source the event first: {_shown(detail)}"
        )
        self.detail = detail


class TiingoRowAfterMembership(TiingoImportError):
    """A row is dated after its line's last membership day (FR-004, T-153 part 3a)."""

    def __init__(self, detail: list[str]) -> None:
        super().__init__(
            f"{len(detail)} line(s) carry a row dated after their last membership day: "
            f"{_shown(detail)}"
        )
        self.detail = detail


class TiingoRowAfterAnalysisDate(TiingoImportError):
    """A row is dated after ``--analysis-date`` (NR-001)."""

    def __init__(self, detail: list[str]) -> None:
        super().__init__(
            f"{len(detail)} line(s) carry a row dated after the analysis date: {_shown(detail)}"
        )
        self.detail = detail


def _shown(items: list[str], limit: int = 5) -> str:
    return ", ".join(items[:limit]) + (", ..." if len(items) > limit else "")


# -- the manifest and the data file -----------------------------------------


@dataclass(frozen=True)
class ManifestLine:
    """One delivered line of the fetch manifest."""

    ask: str
    cik: str
    tiingo_symbol: str
    rows_kept: int
    expected_nyse_sessions: int
    first_date: str
    last_date: str
    dividend_rows: int
    split_rows: int


@dataclass(frozen=True)
class Manifest:
    """The fetch manifest: the file it describes and the lines it delivered."""

    path: Path
    data_file: str
    data_file_sha256: str
    lines: tuple[ManifestLine, ...]
    not_delivered: tuple[str, ...]

    def by_key(self) -> dict[tuple[str, str], ManifestLine]:
        return {(line.ask.upper(), line.cik): line for line in self.lines}


def read_manifest(path: Path) -> Manifest:
    """The manifest at *path*, validated for the fields the import reads."""
    if not path.exists():
        raise TiingoFileMissing(f"no manifest at {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    lines = tuple(
        ManifestLine(
            ask=str(entry["ask"]),
            cik=str(entry["cik"]),
            tiingo_symbol=str(entry["tiingo_symbol"]),
            rows_kept=int(entry["rows_kept"]),
            expected_nyse_sessions=int(entry["expected_nyse_sessions"]),
            first_date=str(entry["first_date"]),
            last_date=str(entry["last_date"]),
            dividend_rows=int(entry["dividend_rows"]),
            split_rows=int(entry["split_rows"]),
        )
        for entry in raw["lines"]
    )
    return Manifest(
        path=path,
        data_file=str(raw["data_file"]),
        data_file_sha256=str(raw["data_file_sha256"]),
        lines=lines,
        not_delivered=tuple(str(entry["ask"]) for entry in raw.get("not_delivered", [])),
    )


@dataclass(frozen=True)
class TiingoBar:
    """One row of the private file, in the schema's own terms.

    ``close``/``open``/``high``/``low`` are the file's **split-adjusted** values, the same basis
    as the gateway's stored close (the manifest's own ``checks.convention`` matches ``adj_close``
    to <= 7e-06), and ``volume`` is its *adjusted* volume, so ``close * volume`` is the day's
    dollar volume on one basis (``raw_close * raw_volume == adj_close * adj_volume``).
    """

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    dividend: float
    split_factor: float


@dataclass(frozen=True)
class TiingoLine:
    """The file's rows for one line, with its manifest entry."""

    ask: str
    cik: str
    manifest: ManifestLine
    bars: tuple[TiingoBar, ...]

    @property
    def first_date(self) -> str:
        return self.bars[0].date

    @property
    def last_date(self) -> str:
        return self.bars[-1].date


def _float(value: str) -> float:
    return float(value) if value not in ("", None) else 0.0


def read_lines(path: Path, manifest: Manifest) -> dict[tuple[str, str], TiingoLine]:
    """The data file grouped into lines, keyed ``(ASK, CIK)``.

    A line is the manifest's own key (T-153 part 2: two lines of one CIK -- DISCA/WBD and
    DISCK -- stay separate), never the ticker alone.
    """
    if not path.exists():
        raise TiingoFileMissing(f"no data file at {path}")
    grouped: dict[tuple[str, str], list[TiingoBar]] = defaultdict(list)
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            key = (str(row["ask_ticker"]).upper(), str(row["cik"]))
            grouped[key].append(
                TiingoBar(
                    date=str(row["date"]),
                    open=_float(row["adj_open"]),
                    high=_float(row["adj_high"]),
                    low=_float(row["adj_low"]),
                    close=_float(row["adj_close"]),
                    volume=_float(row["adj_volume"]),
                    dividend=_float(row[_DIVIDEND_COLUMN]),
                    split_factor=_float(row[_SPLIT_COLUMN]) if row[_SPLIT_COLUMN] else 1.0,
                )
            )
    by_key = manifest.by_key()
    unknown = sorted(f"{ask} ({cik})" for ask, cik in grouped if (ask, cik) not in by_key)
    if unknown:
        raise TiingoLineNotInManifest(unknown)
    return {
        key: TiingoLine(
            ask=key[0],
            cik=key[1],
            manifest=by_key[key],
            bars=tuple(sorted(bars, key=lambda bar: bar.date)),
        )
        for key, bars in grouped.items()
    }


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_sha256(path: Path, expected: str) -> str:
    """The file's sha256, refused when it differs from *expected* (rule 1)."""
    actual = sha256_of(path)
    if actual != expected:
        raise TiingoHashMismatch(
            f"{path} sha256 {actual} does not match the manifest's {expected}; "
            "the import writes nothing from an unverified file"
        )
    return actual


def check_not_in_work_tree(path: Path) -> None:
    """Refuse a data file inside a git work tree (rule 1).

    A work tree has a ``.git`` entry (a directory, or a file for a linked worktree/submodule) in
    one of the file's parent directories. The walk is plain filesystem work -- no subprocess --
    so the check stays hermetic in tests.
    """
    resolved = path.resolve()
    for parent in (resolved, *resolved.parents):
        if (parent / ".git").exists():
            raise TiingoFileInWorkTree(
                f"{path} is inside the git work tree at {parent}: the private file is read from "
                "outside a work tree and never committed"
            )


# -- the checks against the database ----------------------------------------


def stored_gateway_rows(conn: Database, asset_id: int) -> int:
    """Stored ``price_daily`` rows of the line that are not the import's own (rule 2)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM price_daily WHERE asset_id = ? AND COALESCE(source, '') <> ?",
        (asset_id, TIINGO_SOURCE),
    ).fetchone()
    return int(row["n"]) if row is not None else 0


def membership_ends(settings: Settings, conn: Database) -> dict[tuple[str, str], str | None]:
    """``{(CANONICAL_ASK, CIK): window_to}`` for every line that was a member since 2022.

    ``window_to is None`` = the line is still a member, so nothing bounds it (T-153 part 3a).
    """
    with contextlib.closing(connect_ro(settings.universe_db_path)) as universe:
        window = members_over_range(universe, conn, WINDOW_START, "9999-12-31")
    out: dict[tuple[str, str], str | None] = {}
    for entry in window:
        member = entry.member
        out[(member.symbol.upper(), _cik_key(member.cik))] = entry.window_to
    return out


def _cik_key(cik: object) -> str:
    return str(cik or "").lstrip("0") or "0"


# -- the import --------------------------------------------------------------


@dataclass(frozen=True)
class GatewayOnlyLine:
    """A line the gateway already serves: the import writes nothing for it (rule 2, A2)."""

    ticker: str
    cik: str
    asset_id: int
    stored_rows: int

    def record(self) -> dict[str, object]:
        return {
            "ticker": self.ticker,
            "cik": self.cik,
            "asset_id": self.asset_id,
            "stored_rows": self.stored_rows,
        }


@dataclass
class ImportReport:
    """What one import wrote (counts, dates and ids only -- never a value)."""

    run_id: int
    file_sha256: str
    lines: int
    lines_imported: int
    price_rows: int
    dividend_rows: int
    split_rows: int
    first_date: str | None
    last_date: str | None
    gateway_only: list[GatewayOnlyLine] = field(default_factory=list)
    per_line: list[dict[str, object]] = field(default_factory=list)

    def params(
        self, *, data_file: Path, manifest: Manifest, analysis_date: str
    ) -> dict[str, object]:
        return {
            "kind": IMPORT_RUN_KIND,
            "file": str(data_file),
            "manifest": str(manifest.path),
            "file_sha256": self.file_sha256,
            "analysis_date": analysis_date,
            "lines": self.lines,
            "lines_imported": self.lines_imported,
            "price_rows": self.price_rows,
            "dividend_rows": self.dividend_rows,
            "split_rows": self.split_rows,
            "first_date": self.first_date,
            "last_date": self.last_date,
            "lines_gateway_only": [line.record() for line in self.gateway_only],
            "lines_gateway_only_count": len(self.gateway_only),
            # per line: rows written, dividends, splits, first and last date -- counts, ids and
            # dates only, the evidence R1 reports (never a value).
            "per_line": self.per_line,
        }


def import_tiingo(  # noqa: PLR0913 - the two paths, the date and the two test seams
    settings: Settings,
    *,
    data_file: Path,
    manifest_path: Path,
    analysis_date: str,
    dry_run: bool = False,
    conn: Database | None = None,
) -> ImportReport:
    """Load the private file into ``price_daily``/``corporate_action`` (D1-D3, D6, D7).

    Every check runs before anything is written, so a refusal leaves the database untouched.
    """
    manifest = read_manifest(manifest_path)
    check_not_in_work_tree(data_file)
    digest = check_sha256(data_file, manifest.data_file_sha256)
    lines = read_lines(data_file, manifest)

    owns = conn is None
    conn = conn or kg_schema.connect(settings.db_path)
    try:
        pricing_db.ensure_schema(conn)
        assets = _resolve_lines(conn, lines)
        _check_dates(conn, settings, lines, analysis_date)
        _check_split_events(conn, lines, assets)

        gateway_only: list[GatewayOnlyLine] = []
        to_import: list[tuple[TiingoLine, int]] = []
        for key, line in sorted(lines.items(), key=lambda item: item[0][0]):
            asset_id = assets[key]
            stored = stored_gateway_rows(conn, asset_id)
            if stored:
                gateway_only.append(GatewayOnlyLine(line.ask, line.cik, asset_id, stored))
            else:
                to_import.append((line, asset_id))

        report = ImportReport(
            run_id=0,
            file_sha256=digest,
            lines=len(lines),
            lines_imported=len(to_import),
            price_rows=0,
            dividend_rows=0,
            split_rows=0,
            first_date=min((line.first_date for line in lines.values()), default=None),
            last_date=max((line.last_date for line in lines.values()), default=None),
            gateway_only=gateway_only,
        )
        if dry_run:
            return report

        run_id = pricing_db.start_run(
            conn,
            params_json=json.dumps(
                report.params(data_file=data_file, manifest=manifest, analysis_date=analysis_date),
                sort_keys=True,
            ),
            as_of=analysis_date,
            code_version=provenance.code_version(),
        )
        report.run_id = run_id
        for line, asset_id in sorted(to_import, key=lambda item: item[0].ask):
            candles, actions = _line_rows(line)
            written = pricing_db.upsert_tiingo_prices(conn, asset_id, candles, run_id=run_id)
            dividends, splits = pricing_db.insert_tiingo_actions(
                conn, asset_id, actions, engine_version=TIINGO_CORPACT_ENGINE, run_id=run_id
            )
            report.price_rows += written
            report.dividend_rows += dividends
            report.split_rows += splits
            report.per_line.append(
                {
                    "ticker": line.ask,
                    "cik": line.cik,
                    "asset_id": asset_id,
                    "price_rows": written,
                    "dividend_rows": dividends,
                    "split_rows": splits,
                    "first_date": line.first_date,
                    "last_date": line.last_date,
                }
            )
        # The record carries the counts the writes produced, not the plan (rule 4), and the
        # gateway-only list with them (A2).
        conn.execute(
            "UPDATE pricing_run SET params_json = ? WHERE id = ?",
            (
                json.dumps(
                    report.params(
                        data_file=data_file, manifest=manifest, analysis_date=analysis_date
                    ),
                    sort_keys=True,
                ),
                run_id,
            ),
        )
        conn.commit()
        pricing_db.update_run_plan(
            conn, run_id, universe_size=len(lines), planned_units=report.lines_imported
        )
        for _ in range(report.lines_imported):
            pricing_db.bump_run_counter(conn, run_id, "completed_units")
        pricing_db.finish_run(conn, run_id, status="completed")
        return report
    finally:
        if owns:
            conn.close()


def _resolve_lines(
    conn: Database, lines: dict[tuple[str, str], TiingoLine]
) -> dict[tuple[str, str], int]:
    """Each line's ``assets.id`` by CIK through its own ticker line (D1).

    ``resolve_asset_ids`` maps a member by CIK to the asset of *its symbol's* line, so two lines
    of one CIK (DISCA->WBD and DISCK) resolve to two different assets. A line with no asset row is
    refused: the import never invents identity (an ingestion step does that first).
    """
    pairs = [(line.ask, line.cik) for line in lines.values()]
    mapping, _missing = resolve_asset_ids(conn, pairs, as_of="9999-12-31")
    out: dict[tuple[str, str], int] = {}
    absent: list[str] = []
    for key, line in lines.items():
        asset_id = mapping.get(line.ask.upper())
        if asset_id is None:
            absent.append(f"{line.ask} ({line.cik})")
        else:
            out[key] = asset_id
    if absent:
        raise TiingoLineHasNoAsset(sorted(absent))
    return out


def _check_dates(
    conn: Database,
    settings: Settings,
    lines: dict[tuple[str, str], TiingoLine],
    analysis_date: str,
) -> None:
    """No row after the line's last membership day, and none after ``--analysis-date`` (D1)."""
    ends = membership_ends(settings, conn)
    late: list[str] = []
    future: list[str] = []
    for _key, line in lines.items():
        window_to = ends.get((line.ask.upper(), _cik_key(line.cik)))
        if window_to is not None and line.last_date > window_to:
            late.append(f"{line.ask} ({line.last_date} > {window_to})")
        if line.last_date > analysis_date:
            future.append(f"{line.ask} ({line.last_date} > {analysis_date})")
    if late:
        raise TiingoRowAfterMembership(sorted(late))
    if future:
        raise TiingoRowAfterAnalysisDate(sorted(future))


def _check_split_events(
    conn: Database, lines: dict[tuple[str, str], TiingoLine], assets: dict[tuple[str, str], int]
) -> None:
    """Every SPLIT row the import brings must be explained by the tracked event list (D10).

    A row the list does not explain stops the import: the cap reader refuses an unexplained
    factor rather than applying a guess (MKT-02), so importing one would leave the line
    unvaluable with no one told."""
    events = corporate_actions.read_events()
    unexplained: list[str] = []
    for key, line in lines.items():
        cik = _raw_cik(conn, assets[key])
        for bar in line.bars:
            if bar.split_factor == 1.0:
                continue
            if (
                corporate_actions.explained(
                    events, cik=cik, ex_date=bar.date, value=bar.split_factor
                )
                is None
            ):
                unexplained.append(f"{line.ask} {bar.date} {bar.split_factor}")
    if unexplained:
        raise TiingoSplitNotClassified(sorted(unexplained))


def _raw_cik(conn: Database, asset_id: int) -> str:
    """The asset's stored CIK, exactly as the event list keys it."""
    row = conn.execute("SELECT cik FROM assets WHERE id = ?", (asset_id,)).fetchone()
    return str(row["cik"]) if row is not None and row["cik"] else ""


def _line_rows(line: TiingoLine) -> tuple[list[Candle], list[tuple[str, str, float]]]:
    """One line's bars as candles, plus its ``(action_type, ex_date, value)`` rows (D6).

    The dividend value is ``divCash / split_cum``: the manifest proves the gateway's stored
    dividend is the split-adjusted one (``checks.dividend`` on AAPL, ``max_abs_diff_adjusted``
    0.0 against ``max_abs_diff_raw`` 0.615). ``split_cum(d)`` is the product of the line's
    ``splitFactor`` values on split ex-dates **after** ``d`` -- the fetch's own convention.
    """
    ratios = {bar.date: bar.split_factor for bar in line.bars if bar.split_factor != 1.0}
    splits = sorted(ratios)

    def split_cum(day: str) -> float:
        cum = 1.0
        for ex_date in splits:
            if ex_date > day:
                cum *= ratios[ex_date]
        return cum

    candles: list[Candle] = []
    actions: list[tuple[str, str, float]] = []
    for bar in line.bars:
        candles.append(
            Candle(
                date=bar.date,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                source=TIINGO_SOURCE,
            )
        )
        if bar.dividend:
            actions.append(("DIVIDEND", bar.date, bar.dividend / split_cum(bar.date)))
        if bar.split_factor != 1.0:
            actions.append(("SPLIT", bar.date, bar.split_factor))
    return candles, actions


# -- the removal (rule 5 / D9) ----------------------------------------------


@dataclass
class RemovalReport:
    """What one removal deleted (counts only)."""

    run_id: int
    price_rows: int
    observation_rows: int
    return_rows: int
    action_rows: int
    remaining: dict[str, int] = field(default_factory=dict)

    def params(self) -> dict[str, object]:
        return {
            "kind": REMOVAL_RUN_KIND,
            "price_rows": self.price_rows,
            "observation_rows": self.observation_rows,
            "return_rows": self.return_rows,
            "action_rows": self.action_rows,
            "remaining": self.remaining,
        }


_REMAINING_SQL: dict[str, str] = {
    "price_daily": "SELECT COUNT(*) AS n FROM price_daily WHERE source = ?",
    "price_observation": "SELECT COUNT(*) AS n FROM price_observation WHERE source = ?",
    "quant_return_daily": "SELECT COUNT(*) AS n FROM quant_return_daily WHERE vendor = ?",
    "corporate_action": "SELECT COUNT(*) AS n FROM corporate_action WHERE source = ?",
}


def remaining_tiingo_rows(conn: Database) -> dict[str, int]:
    """The postcondition of the removal: Tiingo-marked rows left anywhere (D9).

    ``price_daily``/``corporate_action`` by ``source``, ``price_observation`` by its own row
    marker (D4) and ``quant_return_daily`` by its marker column (D4). A missing table counts 0 --
    a fresh database has nothing to remove.
    """
    out: dict[str, int] = {}
    for name, sql in _REMAINING_SQL.items():  # a static dict: no identifier is interpolated
        try:
            row = conn.execute(sql, (TIINGO_SOURCE,)).fetchone()
        except DatabaseError:  # a partial database (no such table/column yet)
            out[name] = 0
            continue
        out[name] = int(row["n"]) if row is not None else 0
    return out


def remove_tiingo(
    settings: Settings, *, analysis_date: str, dry_run: bool = False, conn: Database | None = None
) -> RemovalReport:
    """Delete every row the import created (rule 5 / D9), then report what remains.

    Ordered price-derived series first, then the actions, then the import's own run row. The
    derived scores, rankings and positions are *not* touched: rule 5 redos the run without the
    import, and on a fresh database that is a fresh run.
    """
    owns = conn is None
    conn = conn or kg_schema.connect(settings.db_path)
    try:
        report = RemovalReport(
            run_id=0, price_rows=0, observation_rows=0, return_rows=0, action_rows=0
        )
        if dry_run:
            report.remaining = remaining_tiingo_rows(conn)
            return report
        run_id = pricing_db.start_run(
            conn,
            params_json=json.dumps({"kind": REMOVAL_RUN_KIND}, sort_keys=True),
            as_of=analysis_date,
            code_version=provenance.code_version(),
        )
        report.run_id = run_id
        report.observation_rows = pricing_db.delete_tiingo_rows(conn, "price_observation")
        report.return_rows = pricing_db.delete_tiingo_rows(conn, "quant_return_daily")
        report.price_rows = pricing_db.delete_tiingo_rows(conn, "price_daily")
        report.action_rows = pricing_db.delete_tiingo_rows(conn, "corporate_action")
        report.remaining = remaining_tiingo_rows(conn)
        conn.execute(
            "UPDATE pricing_run SET params_json = ? WHERE id = ?",
            (json.dumps(report.params(), sort_keys=True), run_id),
        )
        conn.commit()
        pricing_db.update_run_plan(conn, run_id, universe_size=0, planned_units=1)
        pricing_db.bump_run_counter(conn, run_id, "completed_units")
        pricing_db.finish_run(conn, run_id, status="completed")
        return report
    finally:
        if owns:
            conn.close()
