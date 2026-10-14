"""Read helpers over the shared tables -- plain SQL, no imports of the agent packages."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from portfolio_common.db import Database, Row, in_clause

from kg_schema.company_profile import CompanyProfile, profile_as_of
from kg_schema.env import universe_database_path
from kg_schema.market_cap import market_caps_as_of
from kg_schema.pillar_score import total_from_block, weights_differ
from kg_schema.queries import connect_ro, members_asof, resolve_asset_ids
from kg_schema.versions import (
    DATA_QUALITY_GATE_VERSION,
    RULE_INPUT_GROUP,
    UTILITY_COVERAGE_AMBIGUOUS_FLAG,
    UTILITY_COVERAGE_BOUND_AS_WC,
    UTILITY_COVERAGE_BOUND_IN,
    UTILITY_COVERAGE_METRIC,
    UTILITY_COVERAGE_MISSING_BOUND_FLAG,
    MetricVersions,
    resolve_metric_versions,
)


def active_universe(
    conn: Database,
    universe: str,
    cycle_date: str,
    universe_db_path: str | Path | None = None,
) -> list[Row]:
    """``(id, ticker, sector_id, sector)`` (``sector`` is the sector's name) for the *universe* members as of *cycle_date*,
    read point-in-time from ``universe.db`` and mapped by ticker.

    Raises ``RuntimeError`` when ``universe.db`` has no members as of *cycle_date*
    or none of them exist in ``assets`` yet (run the agents for this date first)."""
    upath = universe_database_path(str(universe_db_path) if universe_db_path else None)
    uconn = connect_ro(upath)
    try:
        members = members_asof(uconn, cycle_date, universe=universe)
    finally:
        uconn.close()
    if not members:
        raise RuntimeError(f"universe.db ({upath}) has no {universe} members as of {cycle_date}")
    mapping, _missing = resolve_asset_ids(
        conn, [(m.symbol, m.cik) for m in members], as_of=cycle_date
    )
    if not mapping:
        raise RuntimeError(
            f"none of the {len(members)} {universe} members as of {cycle_date} exist in assets yet "
            f"-- run fundamental_agent / pricing_agent for this date first"
        )
    ids = sorted(mapping.values())
    return list(
        conn.execute(
            f"SELECT a.id, a.ticker, a.sector_id, s.name AS sector FROM assets a "  # noqa: S608
            f"LEFT JOIN sectors s ON s.id = a.sector_id WHERE a.id IN {in_clause(ids)} "
            "ORDER BY a.ticker",
            ids,
        )
    )


def asset_labels(conn: Database, asset_ids: list[int]) -> dict[int, tuple[str, str | None]]:
    """``asset_id -> (ticker, sector name)`` (a read; used by the read-only dry run)."""
    if not asset_ids:
        return {}
    rows = conn.execute(
        f"SELECT a.id, a.ticker, s.name AS sector FROM assets a "  # noqa: S608
        f"LEFT JOIN sectors s ON s.id = a.sector_id WHERE a.id IN {in_clause(asset_ids)}",
        asset_ids,
    )
    return {int(r["id"]): (str(r["ticker"]), r["sector"]) for r in rows}


def asset_profiles(
    conn: Database, asset_ids: list[int], as_of: str
) -> dict[int, CompanyProfile | None]:
    """``asset_id -> its APP-00 company profile`` as of *as_of*, or ``None`` when the CIK has no
    profile (T-071 part 2a). The rules key "financial firm" (APP-00/APP-02) and the regulated-utility
    overlay (D-13) on this, never on the GICS sector; an unknown CIK is never guessed."""
    if not asset_ids:
        return {}
    rows = conn.execute(
        f"SELECT id, cik FROM assets WHERE id IN {in_clause(asset_ids)}",  # noqa: S608
        asset_ids,
    )
    return {int(r["id"]): profile_as_of(r["cik"], as_of) for r in rows}


def latest_metrics(
    conn: Database, cycle_date: str, versions: MetricVersions
) -> dict[int, dict[str, float | None]]:
    """asset_id -> {"group.name": value} from each asset's newest filing *usable* on
    *cycle_date* (``available_at <= cycle_date``, the first trading day after its filing date,
    T-106/T-107 -- never one whose period merely ended by then, nor one filed that very day),
    read from the metrics engine *versions* the run resolved (T-090) -- never every version. A
    filing with no ``filing_date`` has no ``available_at`` and is skipped."""
    rows = conn.execute(
        """
        WITH known AS (
            SELECT f.id, ROW_NUMBER() OVER (
                PARTITION BY f.asset_id ORDER BY f.period_end DESC, f.available_at DESC, f.id DESC
            ) AS rn
            FROM sec_filings f
            WHERE f.period_end IS NOT NULL AND f.available_at IS NOT NULL AND f.available_at <= ?
        )
        SELECT f.asset_id, m.metric_group, m.metric_name, m.value
        FROM fundamental_metrics m
        JOIN sec_filings f ON f.id = m.filing_id
        JOIN known k ON k.id = f.id AND k.rn = 1
        WHERE (m.metric_group || '/' || m.engine_version) IN (SELECT value FROM json_each(?))
        """,
        (cycle_date, versions.json_param()),
    ).fetchall()
    out: dict[int, dict[str, float | None]] = {}
    for r in rows:
        out.setdefault(int(r["asset_id"]), {})[f"{r['metric_group']}.{r['metric_name']}"] = r[
            "value"
        ]
    return out


def utility_cash_coverage(conn: Database, cycle_date: str) -> dict[int, dict[str, Any]]:
    """asset_id -> the D-13 regulated-utility cash-coverage rule input of its newest **10-K**
    *usable* on *cycle_date* (``available_at <= cycle_date``), read at the newest stored
    ``rule_input`` engine version -- never a mix of versions.

    A 10-Q does not carry the input (it is a three-year average over a 10-K's own cash-flow
    statement), so this deliberately keys on ``form LIKE '10-K%'`` rather than reusing
    :func:`latest_metrics`' "newest filing". The input lives outside
    ``kg_schema.versions.METRIC_GROUPS``, so its engine version is resolved on its own here; an
    asset whose newest 10-K has no such row is simply absent (never guessed).

    The stored ``value`` is the primary bound (``average_ambiguous_in``); both bounds and the two
    flags come from the row's ``inputs_json``, so the rule can require *both* bounds below the
    cutoff (R2)."""
    versions = resolve_metric_versions(conn, None, groups=(RULE_INPUT_GROUP,))
    if not versions.by_group:
        return {}
    rows = conn.execute(
        """
        WITH known AS (
            SELECT f.id, ROW_NUMBER() OVER (
                PARTITION BY f.asset_id ORDER BY f.period_end DESC, f.available_at DESC, f.id DESC
            ) AS rn
            FROM sec_filings f
            WHERE f.form LIKE '10-K%' AND f.period_end IS NOT NULL
              AND f.available_at IS NOT NULL AND f.available_at <= ?
        )
        SELECT f.asset_id, m.value, m.inputs_json
        FROM fundamental_metrics m
        JOIN sec_filings f ON f.id = m.filing_id
        JOIN known k ON k.id = f.id AND k.rn = 1
        WHERE m.metric_group = ? AND m.metric_name = ?
          AND (m.metric_group || '/' || m.engine_version) IN (SELECT value FROM json_each(?))
        """,
        (cycle_date, RULE_INPUT_GROUP, UTILITY_COVERAGE_METRIC, versions.json_param()),
    ).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        inputs = _json_object(r["inputs_json"])
        out[int(r["asset_id"])] = {
            "value": r["value"],
            "ambiguous_in": inputs.get(UTILITY_COVERAGE_BOUND_IN),
            "ambiguous_as_wc": inputs.get(UTILITY_COVERAGE_BOUND_AS_WC),
            "missing_bound": bool(inputs.get(UTILITY_COVERAGE_MISSING_BOUND_FLAG)),
            "ambiguous_working_capital": bool(inputs.get(UTILITY_COVERAGE_AMBIGUOUS_FLAG)),
        }
    return out


def latest_trailing_net_income(
    conn: Database, cycle_date: str, versions: MetricVersions
) -> dict[int, float]:
    """asset_id -> the trailing-year net income of each asset's newest filing *usable* on
    *cycle_date* (the same filing :func:`latest_metrics` reads), for the earnings yield (N2,
    MET-05, T-149). It is ``inputs.net_income_ttm`` of the filing's ``profitability`` metric rows: a
    10-K's fiscal year, or a 10-Q's real trailing twelve months. A 10-Q that could only be
    annualized as one quarter times four (``annualized_x4``) has none -- that is the quarter again,
    not a trailing year -- and neither has a filing recorded before the input existed: the asset is
    absent, never estimated."""
    rows = conn.execute(
        """
        WITH known AS (
            SELECT f.id, ROW_NUMBER() OVER (
                PARTITION BY f.asset_id ORDER BY f.period_end DESC, f.available_at DESC, f.id DESC
            ) AS rn
            FROM sec_filings f
            WHERE f.period_end IS NOT NULL AND f.available_at IS NOT NULL AND f.available_at <= ?
        )
        SELECT f.asset_id, m.inputs_json
        FROM fundamental_metrics m
        JOIN sec_filings f ON f.id = m.filing_id
        JOIN known k ON k.id = f.id AND k.rn = 1
        WHERE m.metric_group = 'profitability' AND m.metric_name = 'return_on_assets'
          AND (m.metric_group || '/' || m.engine_version) IN (SELECT value FROM json_each(?))
        """,
        (cycle_date, versions.json_param()),
    ).fetchall()
    out: dict[int, float] = {}
    for r in rows:
        inputs = json.loads(r["inputs_json"] or "{}")
        value = inputs.get("net_income_ttm")
        real = isinstance(value, (int, float)) and not isinstance(value, bool)
        if real and not inputs.get("annualized_x4"):
            out[int(r["asset_id"])] = float(value)
    return out


@dataclass
class DataQuality:
    """What the Ring-1 gates (T-065) decided about each asset's latest filing, for the
    metric versions this run reads."""

    # asset_id -> {"group.name"} read as NULL
    quarantined: dict[int, set[str]] = field(default_factory=dict)
    # asset_id -> the HARD issues (the DATA_QUALITY veto's evidence)
    hard: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    # asset_id -> the DQ_NEG_EQUITY record's evidence for a latest filing with book equity <= 0
    # (equity, net debt/EBITDA, interest coverage, TTM EBITDA, net debt). D-04 (T-071 part 2a): the
    # gate is always SOFT now, and this record is the equity sign the LEVERAGE_EXTREME screen keys
    # on (the filing's D/E is quarantined, so the rule never sees a negative ratio).
    negative_equity: dict[int, dict[str, Any]] = field(default_factory=dict)

    def apply(
        self, metrics: dict[int, dict[str, float | None]]
    ) -> dict[int, dict[str, float | None]]:
        """*metrics* with every quarantined value replaced by None (the input is untouched)."""
        out: dict[int, dict[str, float | None]] = {}
        for aid, values in metrics.items():
            q = self.quarantined.get(aid, set())
            out[aid] = {k: (None if k in q else v) for k, v in values.items()}
        return out


def data_quality(conn: Database, cycle_date: str, versions: MetricVersions) -> DataQuality:
    """The quarantines and HARD issues recorded against each asset's latest *usable* filing
    (the same filing :func:`latest_metrics` reads, T-106/T-107), for the metric *versions* the run resolved and the
    current gate version only. A SOFT, unquarantined issue is a review item and is skipped."""
    rows = conn.execute(
        """
        WITH known AS (
            SELECT f.id, ROW_NUMBER() OVER (
                PARTITION BY f.asset_id ORDER BY f.period_end DESC, f.available_at DESC, f.id DESC
            ) AS rn
            FROM sec_filings f
            WHERE f.period_end IS NOT NULL AND f.available_at IS NOT NULL AND f.available_at <= ?
        )
        SELECT f.asset_id, d.metric_group, d.metric_name, d.rule_id, d.severity,
               d.quarantined, d.value, d.evidence_json
        FROM data_quality_issue d
        JOIN sec_filings f ON f.id = d.filing_id
        JOIN known k ON k.id = f.id AND k.rn = 1
        WHERE (d.metric_group || '/' || d.metric_engine_version) IN (SELECT value FROM json_each(?))
          AND d.gate_version = ?
          AND (d.quarantined = 1 OR d.severity = 'HARD')
        ORDER BY f.asset_id, d.rule_id, d.metric_group, d.metric_name
        """,
        (cycle_date, versions.json_param(), DATA_QUALITY_GATE_VERSION),
    ).fetchall()
    out = DataQuality()
    for r in rows:
        aid = int(r["asset_id"])
        key = f"{r['metric_group']}.{r['metric_name']}"
        if r["quarantined"]:
            out.quarantined.setdefault(aid, set()).add(key)
        if r["rule_id"] == "DQ_NEG_EQUITY":
            out.negative_equity[aid] = _json_object(r["evidence_json"])
        if r["severity"] == "HARD":
            out.hard.setdefault(aid, []).append(
                {"rule_id": r["rule_id"], "metric": key, "value": r["value"]}
            )
    return out


class MissingObservations(RuntimeError):
    """``price_observation`` holds rows, but none at the engine version the cycle reads."""


def latest_price_observation(
    conn: Database, cycle_date: str, engine_version: str
) -> dict[int, dict[str, float | None]]:
    """Each asset's newest ``price_observation`` row on or before *cycle_date*, at *engine_version*
    only (T-070) -- never a mix of versions, even when one date holds rows of two.

    Raises :class:`MissingObservations` when the table has rows but none at *engine_version*
    (pricing has not rebuilt the series under it yet), rather than score nothing silently."""
    rows = conn.execute(
        """
        SELECT p.*
        FROM price_observation p
        JOIN (
            SELECT asset_id, MAX(obs_date) AS d FROM price_observation
            WHERE obs_date <= ? AND engine_version = ? GROUP BY asset_id
        ) last ON last.asset_id = p.asset_id AND last.d = p.obs_date
        WHERE p.engine_version = ?
        """,
        (cycle_date, engine_version, engine_version),
    ).fetchall()
    if not rows:
        other = conn.execute("SELECT engine_version FROM price_observation LIMIT 1").fetchone()
        if other is not None:
            raise MissingObservations(
                f"price_observation has no rows at engine_version {engine_version!r} (it holds "
                f"{other['engine_version']!r}); run `python -m pricing_agent run --store-daily "
                "--observations` to rebuild the series under it"
            )
    return {int(r["asset_id"]): dict(r) for r in rows}


def latest_fundamental_rows(conn: Database, cycle_date: str) -> list[Row]:
    """Each asset's newest **scored** FUNDAMENTAL snapshot usable on *cycle_date*
    (``id, asset_id, event_time, raw_value, normalized_score, inputs_json``). A FUNDAMENTAL row's
    ``event_time`` is its period end -- what the score is *about* -- not when it became known;
    that is its ``available_at``, the first trading day after its filing's date (T-107).

    T-073 (D1): the newest *usable* filing decides. A filing whose applicable pillar has no value
    carries no ``raw_value`` -- it is recorded (so the run resumes) but it is not a score, so the
    member reads as unscored (T-119) and an older *scored* filing is **not** carried forward in its
    place: that would read a stale value as the current one, which MET-00 rules out. The ranking
    therefore comes first and the row is dropped afterwards.

    ``inputs_json`` rides along for D4: it holds the row's pillar block, so a cycle under other
    weights recomputes the total from the ratings the row stored."""
    return conn.execute(
        """
        WITH known AS (
            SELECT s.id, s.asset_id, s.event_time, s.raw_value, s.normalized_score, s.inputs_json,
                   ROW_NUMBER() OVER (
                       PARTITION BY s.asset_id ORDER BY s.event_time DESC, s.id DESC
                   ) AS rn
            FROM score_snapshot s
            WHERE s.score_type = 'FUNDAMENTAL'
              AND s.available_at IS NOT NULL AND s.available_at <= ?
        )
        SELECT id, asset_id, event_time, raw_value, normalized_score, inputs_json
        FROM known
        WHERE rn = 1 AND raw_value IS NOT NULL
        ORDER BY asset_id
        """,
        (cycle_date,),
    ).fetchall()


def fundamental_value_under(row: Row, weights: Mapping[str, float]) -> float:
    """A FUNDAMENTAL row's total under *weights* (T-073, D4).

    The row's ``raw_value`` is its total under the weights it was scored with. When the cycle's
    weights differ from the weights stored with that row, the total is recomputed from the pillar
    ratings the row stored -- no LLM call and no rewrite of the stored row. A row with no pillar
    block (written before T-073, or one the code left unscored) keeps its own ``raw_value``.
    """
    stored = _json_object(row["inputs_json"])
    if not stored or not weights_differ(stored, weights):
        return float(row["raw_value"])
    recomputed = total_from_block(stored, weights)
    return float(row["raw_value"]) if recomputed is None else recomputed


def _json_object(raw: str | None) -> dict[str, Any]:
    """A stored JSON audit blob (``inputs_json`` / ``evidence_json``) as a dict; ``{}`` when it
    carries none or it is not an object."""
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def last_fundamental_dates(conn: Database, cycle_date: str) -> dict[int, str | None]:
    """asset_id -> the period end of its newest **scored** FUNDAMENTAL filing usable on
    *cycle_date*.

    Only ever has an asset as a key when it *has* a usable FUNDAMENTAL score -- an asset whose
    newest usable filing is unscored (T-073, D1: no ``raw_value``) and one with no filing at all
    are both simply absent, never present with a ``None`` value (T-119): a consumer that wants "no
    score at all" must diff its own universe against this dict's keys, not iterate its values --
    see ``unscored_assets``."""
    return {int(r["asset_id"]): r["event_time"] for r in latest_fundamental_rows(conn, cycle_date)}


def unscored_assets(asset_ids: list[int], scored: dict[int, Any]) -> list[int]:
    """Universe members with no FUNDAMENTAL **score** at all, not merely a stale one (T-119).

    *scored* is keyed by every asset that has one (``last_fundamental_dates`` or
    ``latest_fundamental_rows``'s normalized-score map both qualify) -- this is a key-membership
    diff against the full universe, not a value check, since an asset with no score value is never
    a key at all (T-073: a filing whose applicable pillar has no value is recorded but not scored,
    so it is unscored here too). Includes *every* asset when none is scored -- PR #78's decision
    ("more than 5% unscored stops the cycle") applies at 100% too (PR #99 review): a cycle with
    no FUNDAMENTAL coverage at all must refuse, not silently build a portfolio on TECHNICAL/
    VALORIZATION alone."""
    return [a for a in asset_ids if a not in scored]


class TooManyUnscored(RuntimeError):
    """More than ``CycleSettings.unscored_max_share`` of the universe has no FUNDAMENTAL score
    at all (T-119, PR #78 review) -- refuses to rank/select rather than silently building a
    portfolio blind on most of the universe. An unscored asset is ineligible for selection, not
    penalized like a stale one (``EARNINGS_MISSING``, SOFT): this is a data-completeness circuit
    breaker on the whole run, not a per-asset veto."""


def too_many_unscored_reason(
    unscored: list[int], universe_size: int, max_share: float
) -> str | None:
    """``None`` when *unscored*'s share of *universe_size* is within *max_share*; otherwise the
    refusal message ``TooManyUnscored`` should carry (T-119). 100% unscored is not exempt (PR
    #99 review) -- a cycle dated before any filing is public yet must refuse the same as any
    other coverage gap over the threshold, not silently rank/select on TECHNICAL/VALORIZATION
    alone."""
    if universe_size == 0 or not unscored:
        return None
    share = len(unscored) / universe_size
    if share <= max_share:
        return None
    return (
        f"{len(unscored)}/{universe_size} universe members ({share:.1%}) have no FUNDAMENTAL "
        f"score at all, over the {max_share:.0%} limit: asset_ids {sorted(unscored)}"
    )


def latest_fundamental_score(conn: Database, cycle_date: str) -> dict[int, float | None]:
    return {int(r["asset_id"]): r["raw_value"] for r in latest_fundamental_rows(conn, cycle_date)}


def latest_semantic_score(conn: Database, cycle_date: str) -> dict[int, float | None]:
    """Pre-existing SEMANTIC scores (written by the integration repo), if any."""
    rows = conn.execute(
        """
        SELECT s.asset_id, s.raw_value
        FROM score_snapshot s
        JOIN (
            SELECT asset_id, MAX(event_time) AS et FROM score_snapshot
            WHERE score_type = 'SEMANTIC' AND event_time <= ? GROUP BY asset_id
        ) last ON last.asset_id = s.asset_id AND last.et = s.event_time
        WHERE s.score_type = 'SEMANTIC'
        """,
        (cycle_date,),
    ).fetchall()
    return {int(r["asset_id"]): r["raw_value"] for r in rows}


def market_cap_estimates(
    conn: Database, cycle_date: str, asset_ids: list[int]
) -> dict[int, float | None]:
    """Each asset's market cap on *cycle_date* from the shared as-of reader (T-132): the latest
    cover-page share count usable that day times the close on or before it. ``None`` where the
    reader refuses to value the name (no count, a count too old, no recent close) -- a missing
    size factor, never a stale or inflated one. ``quant`` reads the same function."""
    result = market_caps_as_of(conn, asset_ids, as_of=cycle_date)
    return {aid: result.get(aid) for aid in asset_ids}
