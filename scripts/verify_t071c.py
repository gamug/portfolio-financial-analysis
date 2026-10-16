"""T-071 part 2b measurement (D1-D6). No LLM, no network; the copies only.

    uv run python scripts/verify_t071c.py <subcommand> [options]

Subcommands
-----------
``scores --side before|after``   each side's own VALORIZATION at 2026-09-22 (its own ``compute``)
``blend --side before|after``    the side's own ``cycle monitor`` ranking (the blend and the top 30)
``compare``                      old vs new: Spearman, top-30 / top-decile overlap, the top decile's
                                 sector mix and the financial firms' tilt (D2/D6)
``dq02``                         D3's table: per score type, n, the values trimmed and the movers
``coverage``                     per date and company type: applicable/present/empty per measure
``book-equity``                  D1's preferred-stock measurement (members filing
                                 ``PreferredStockValue`` > 0)
``multi-class``                  D5's table (old -> new, the ``n_classes`` disagreements, the ADJ)
``reit-capex``                   D6's FRT/VTR before -> after on the resolver's own basis
``d14``                          the re-measured D-14 table (the REITs vetoed by NEGATIVE_FCF)
``all``                          every output above

The **before** side runs this script under master's code (a worktree at ``origin/master``):
``PYTHONPATH=<control>/src uv run --project <repo> python scripts/verify_t071c.py scores --side before
--db before.db --out out/scores_before.json``. It tolerates the branch-only readers (D1's rule input,
D6's capex).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Final

from cycle import data
from cycle.config import CycleSettings
from cycle.db import ensure_schema
from cycle.orchestrator import run_monitoring
from cycle.rules import RuleContext
from cycle.rules.builtin import _NegativeFcfRule
from cycle.scores import valorization
from cycle.scores.normalize import normalized_scores
from fundamental_agent import db as agent_db
from fundamental_agent import statements as S
from kg_schema import connect
from kg_schema.applicability import applicability
from kg_schema.company_profile import profile_as_of, profiles
from kg_schema.market_cap import market_caps_as_of
from kg_schema.versions import DATA_QUALITY_GATE_VERSION, resolve_metric_versions

UNIVERSE = "SP500"
UNIVERSE_DB = "/workspaces/thesis/data/universe_history.db"
DATES = (
    "2023-03-31",
    "2023-06-30",
    "2023-09-30",
    "2023-12-31",
    "2024-03-31",
    "2024-06-30",
    "2024-09-30",
    "2024-12-31",
    "2025-03-31",
    "2025-06-30",
    "2025-09-30",
    "2025-12-31",
    "2026-03-31",
    "2026-06-30",
    "2026-09-22",
)
MAIN = "2026-09-22"
#: D3's "moves by more than one point" threshold.
ONE_POINT = 1.0


def _data_quality(conn: Any, date: str, versions: Any, gate: str) -> Any:
    """``data.data_quality`` with the gate version when the reader accepts one (this branch), else
    without it (master's, before part 2a's ``gate_version`` parameter)."""
    try:
        return data.data_quality(conn, date, versions, gate_version=gate)
    except TypeError:  # pragma: no cover - master's reader
        return data.data_quality(conn, date, versions)


def _applicability(profile: Any, negative_equity: bool) -> dict[str, tuple[str, str]]:
    """The measure -> matrix ``(code, rule)`` map the cycle builds (D2); local, so this script runs
    under master's code too."""
    out: dict[str, tuple[str, str]] = {}
    for measure, metric in valorization.MATRIX_METRIC.items():
        out[measure] = (
            ("A", "")
            if profile is None
            else applicability(metric, profile, negative_equity=negative_equity)
        )
    if profile is not None and profile.regulated_utility:
        out[valorization.ENTERPRISE_FCF_YIELD] = ("NA", "the user's decision (D-13)")
    return out


class Snapshot:
    """One side's view of the members at a cycle date."""

    def __init__(self, db: str, date: str = MAIN, gate: str = DATA_QUALITY_GATE_VERSION) -> None:
        self.db, self.date = db, date
        self.conn = data.connect_ro(db)
        self.versions = resolve_metric_versions(self.conn, None)
        self.dq = _data_quality(self.conn, date, self.versions, gate)
        self.metrics = self.dq.apply(data.latest_metrics(self.conn, date, self.versions))
        self.rows = data.active_universe(self.conn, UNIVERSE, date, UNIVERSE_DB)
        self.asset_ids = [int(r["id"]) for r in self.rows]
        self.ticker = {int(r["id"]): str(r["ticker"]) for r in self.rows}
        self.sector = {int(r["id"]): r["sector"] for r in self.rows}
        self.mcap = data.market_cap_estimates(self.conn, date, self.asset_ids)
        self.caps = market_caps_as_of(self.conn, self.asset_ids, as_of=date).caps
        self.profiles = data.asset_profiles(self.conn, self.asset_ids, date)
        self.values = data.value_inputs(self.conn, date) if hasattr(data, "value_inputs") else {}
        self.trailing = data.latest_trailing_net_income(self.conn, date, self.versions)

    def v2_members(self) -> dict[int, Any]:
        """The branch's members (D1's rule input + the market cap on D)."""
        out = {}
        for a in self.asset_ids:
            rule = self.values.get(a) or {}
            profile = self.profiles.get(a)
            out[a] = valorization.Member(
                asset_id=a,
                market_cap=self.mcap.get(a),
                values=rule.get("inputs") or {},
                applicable=_applicability(profile, a in self.dq.negative_equity),
                quarantined=frozenset(self.dq.quarantined.get(a, set())),
                cohort=(
                    valorization.FINANCIAL
                    if profile is not None and profile.financial_firm
                    else valorization.OTHER
                ),
                share_date=self.caps[a].share_as_of if a in self.caps else None,
                close_date=self.caps[a].close_date if a in self.caps else None,
                accession=rule.get("accession"),
            )
        return out

    def v1_rows(self) -> dict[int, dict[str, float | None]]:
        """Master's rows: the stored metrics plus the synthetics its ``_valorization`` built."""
        out = {}
        for a in self.asset_ids:
            m = dict(self.metrics.get(a, {}))
            quarantined = self.dq.quarantined.get(a, set())
            mc = None if "valuation.market_capitalization" in quarantined else self.mcap.get(a)
            if a in self.dq.negative_equity:
                m["leverage.debt_to_equity"] = float("inf")
            ni = self.trailing.get(a)
            if ni is not None and mc and "profitability.return_on_assets" not in quarantined:
                m["earnings_yield"] = max(ni, 0.0) / mc
            if mc:
                m["neg_log_market_cap"] = -math.log(mc)
            out[a] = m
        return out

    def scores(self) -> list[Any]:
        """Each side's **own** ``valorization.compute``."""
        if getattr(valorization, "VERSION", "") == "valorization-v2":
            return valorization.compute(self.v2_members())
        rows: Any = self.v1_rows()
        return valorization.compute(rows)


def _write(path: str, payload: Any) -> None:
    Path(path).write_text(json.dumps(payload, indent=1, default=str))
    print(f"wrote {path}")


# -- scores ---------------------------------------------------------------------------------------


def cmd_scores(args: argparse.Namespace) -> None:
    snap = Snapshot(args.db, args.date)
    payload = {
        "side": args.side,
        "date": args.date,
        "version": getattr(valorization, "VERSION", "valorization-v1"),
        "members": {},
    }
    for score in snap.scores():
        aid = int(score.asset_id)
        profile = snap.profiles.get(aid)
        payload["members"][str(aid)] = {
            "ticker": snap.ticker.get(aid),
            "sector": snap.sector.get(aid),
            "type": None if profile is None else profile.type,
            "financial_firm": bool(profile and profile.financial_firm),
            "reit": bool(profile and profile.reit),
            "regulated_utility": bool(profile and profile.regulated_utility),
            "multi_class": bool(profile and profile.multi_class),
            "raw": score.raw_value,
            "components": score.components,
        }
    _write(args.out, payload)


def cmd_compare(args: argparse.Namespace) -> None:
    before = json.loads(Path(args.before).read_text())["members"]
    after = json.loads(Path(args.after).read_text())["members"]
    common = sorted(set(before) & set(after), key=lambda a: after[a]["ticker"] or "")
    pairs = [
        (float(before[a]["raw"]), float(after[a]["raw"]))
        for a in common
        if before[a]["raw"] is not None and after[a]["raw"] is not None
    ]
    ranks_b = _ranks({a: before[a]["raw"] for a in common})
    ranks_a = _ranks({a: after[a]["raw"] for a in common})
    both = [a for a in common if a in ranks_b and a in ranks_a]
    spearman = _spearman([ranks_b[a] for a in both], [ranks_a[a] for a in both])
    top_b, top_a = _top(ranks_b, 30), _top(ranks_a, 30)
    dec_b, dec_a = (
        _top(ranks_b, max(1, len(common) // 10)),
        _top(ranks_a, max(1, len(common) // 10)),
    )
    fin = [a for a in common if after[a]["financial_firm"]]
    payload = {
        "date": args.date,
        "members": len(common),
        "scored_before": len(pairs),
        "scored_after": sum(1 for a in common if after[a]["raw"] is not None),
        "spearman": spearman,
        "top30_overlap": sorted({after[a]["ticker"] for a in top_b & top_a}),
        "top30_only_before": sorted({after[a]["ticker"] for a in top_b - top_a}),
        "top30_only_after": sorted({after[a]["ticker"] for a in top_a - top_b}),
        "top_decile_overlap": len(dec_b & dec_a),
        "top_decile_size": len(dec_a),
        "sector_mix_top_decile": _sector_mix(after, dec_a),
        "sector_mix_top_decile_before": _sector_mix(before, dec_b),
        "financial_share_of_members": len(fin) / len(common) if common else None,
        "financial_share_of_top_decile_after": (
            sum(1 for a in dec_a if after[a]["financial_firm"]) / len(dec_a) if dec_a else None
        ),
        "financial_share_of_top_decile_before": (
            sum(1 for a in dec_b if before[a]["financial_firm"]) / len(dec_b) if dec_b else None
        ),
        "financial_rank_median_before": _median([ranks_b[a] for a in fin if a in ranks_b]),
        "financial_rank_median_after": _median([ranks_a[a] for a in fin if a in ranks_a]),
        "spearman_note": "Spearman over the members both sides score (a NULL raw is never ranked)",
    }
    _write(args.out, payload)


def _ranks(raws: dict[str, float | None]) -> dict[str, int]:
    known = sorted(
        ((a, float(v)) for a, v in raws.items() if v is not None),
        key=lambda pair: pair[1],
        reverse=True,
    )
    return {a: i + 1 for i, (a, _value) in enumerate(known)}


def _top(ranks: dict[str, int], n: int) -> set[str]:
    return set(sorted(ranks, key=lambda a: ranks[a])[:n])


def _spearman(a: list[int], b: list[int]) -> float | None:
    if len(a) < 2:  # noqa: PLR2004 - a rank correlation needs two points
        return None
    n = len(a)
    ra, rb = _rank_values(a), _rank_values(b)
    mean_a, mean_b = sum(ra) / n, sum(rb) / n
    num = sum((x - mean_a) * (y - mean_b) for x, y in zip(ra, rb, strict=True))
    da = math.sqrt(sum((x - mean_a) ** 2 for x in ra))
    db = math.sqrt(sum((y - mean_b) ** 2 for y in rb))
    return num / (da * db) if da and db else None


def _rank_values(values: list[int]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    for rank, i in enumerate(order, start=1):
        out[i] = float(rank)
    return out


def _median(values: list[int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return float(ordered[mid]) if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _sector_mix(members: dict[str, Any], chosen: set[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for a in chosen:
        sector = members[a]["sector"] or "(none)"
        out[sector] = out.get(sector, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# -- value_measures: the measures on D and the period-end value --------------------------------


def cmd_value_measures(args: argparse.Namespace) -> None:
    """Per member at the date: the profile, the four measures (value on D, status, percentile), the
    market cap and EV on D -- and, for E/P and the enterprise FCF yield, the **period-end** value
    the stored per-filing metrics carry, so MKT-01's change is visible."""
    snap = Snapshot(args.db, args.date)
    scores = {int(s.asset_id): s for s in snap.scores()}
    rows: dict[str, Any] = {}
    diffs: dict[str, list[float]] = {"earnings_yield": [], "enterprise_fcf_yield": []}
    for aid, score in scores.items():
        components = score.components or {}
        measures = components.get("measures") or {}
        profile = snap.profiles.get(aid)
        stored = snap.metrics.get(aid) or {}
        stored_cap = stored.get("valuation.market_capitalization")
        net_income = (snap.values.get(aid) or {}).get("inputs", {}).get("net_income_ttm")
        period_end = {
            "earnings_yield": (
                max(float(net_income), 0.0) / float(stored_cap)
                if isinstance(net_income, (int, float)) and stored_cap
                else None
            ),
            "enterprise_fcf_yield": stored.get("valuation.enterprise_fcf_yield"),
        }
        entry = {
            "ticker": snap.ticker.get(aid),
            "sector": snap.sector.get(aid),
            "type": None if profile is None else profile.type,
            "financial_firm": bool(profile and profile.financial_firm),
            "reit": bool(profile and profile.reit),
            "regulated_utility": bool(profile and profile.regulated_utility),
            "multi_class": bool(profile and profile.multi_class),
            "market_cap_on_d": components.get("market_cap"),
            "enterprise_value_on_d": components.get("enterprise_value"),
            "stored_market_capitalization": stored_cap,
            "raw": score.raw_value,
            "measures": {},
        }
        for name, measure in measures.items():
            item = {
                "value_on_d": measure.get("value"),
                "percentile": measure.get("percentile"),
                "status": measure.get("status"),
                "flags": measure.get("flags"),
            }
            if name in period_end:
                item["period_end_value"] = period_end[name]
                on_d = measure.get("value")
                if on_d is not None and period_end[name] is not None:
                    item["difference"] = on_d - period_end[name]
                    diffs[name].append(item["difference"])
            entry["measures"][name] = item
        rows[str(aid)] = entry
    payload = {
        "date": args.date,
        "n_members": len(rows),
        "members": rows,
        "differences": {
            name: {
                "n": len(values),
                "min": min(values) if values else None,
                "median": _median_float(values),
                "max": max(values) if values else None,
            }
            for name, values in diffs.items()
        },
        "note": (
            "value_on_d is priced on the analysis date (MKT-01); period_end_value is what the stored "
            "per-filing metric carried (E/P over the stored period-end market cap, and the stored "
            "enterprise_fcf_yield), which VALORIZATION no longer reads."
        ),
    }
    _write(args.out, payload)


# -- the blend ------------------------------------------------------------------------------------


def cmd_blend(args: argparse.Namespace) -> None:
    """The side's own cycle ranking (``cycle monitor``), plus its ``select --dry-run`` book."""
    if args.fresh:
        # a completed run would be resumed step-by-step and nothing would re-run: drop it (a copy)
        conn0 = connect(args.db)
        ensure_schema(conn0)
        run_ids = [
            int(r["id"])
            for r in conn0.execute(
                "SELECT id FROM cycle_run WHERE cycle_type = 'MONITORING' AND cycle_date = ?",
                (args.date,),
            )
        ]
        for run_id in run_ids:
            conn0.execute("DELETE FROM cycle_ranking WHERE cycle_run_id = ?", (run_id,))
            conn0.execute("DELETE FROM cycle_checkpoint WHERE cycle_run_id = ?", (run_id,))
            conn0.execute("DELETE FROM cycle_run WHERE id = ?", (run_id,))
        conn0.commit()
        conn0.close()
    settings = CycleSettings(
        db_path=Path(args.db),
        top_n=30,
        observation_engine_version=args.observation_engine_version,
    )
    run_monitoring(settings, args.date, conn=None)
    conn = connect(args.db)
    ensure_schema(conn)
    rows = list(
        conn.execute(
            "SELECT a.ticker, r.rank, r.blended_score, r.components_json, r.veto_rules_json "
            "FROM cycle_ranking r JOIN assets a ON a.id = r.asset_id "
            "JOIN cycle_run cr ON cr.id = r.cycle_run_id "
            "WHERE cr.cycle_date = ? AND cr.cycle_type = 'MONITORING' ORDER BY r.rank",
            (args.date,),
        )
    )
    _write(
        args.out,
        {
            "side": args.side,
            "date": args.date,
            "ranking": [
                {
                    "ticker": r["ticker"],
                    "rank": r["rank"],
                    "blended_score": r["blended_score"],
                    "components": json.loads(r["components_json"] or "{}"),
                    "veto_rules": json.loads(r["veto_rules_json"] or "[]"),
                }
                for r in rows
            ],
        },
    )


# -- DQ-02 ----------------------------------------------------------------------------------------


def cmd_dq02(args: argparse.Namespace) -> None:
    """D3/F2: per score type, n, the values trimmed per tail, and the members whose normalized score
    moves by more than one point between 2% and DQ-02's 0.5%.

    FUNDAMENTAL is measured with the **cycle's own reader** (``latest_fundamental_rows`` +
    ``fundamental_value_under`` under the cycle's weights) -- the same totals ``_normalize``
    normalizes -- and the blended top 30 is compared **before -> after the winsorizing change
    alone** (the other components are the stored run's, and each asset's soft-veto penalty is held
    at the value the stored blend implies)."""
    snap = Snapshot(args.db, args.date)
    payload: dict[str, Any] = {"date": args.date, "score_types": {}}
    for stype in ("TECHNICAL", "VALORIZATION", "SEMANTIC"):
        raws = [
            float(r["raw_value"])
            for r in snap.conn.execute(
                "SELECT raw_value FROM score_snapshot WHERE score_type = ? AND event_time = ? "
                "AND raw_value IS NOT NULL",
                (stype, args.date),
            )
        ]
        if not raws:
            continue
        payload["score_types"][stype] = _winsor_row(raws)
    weights = CycleSettings(db_path=Path(args.db)).fundamental_pillar_weights
    fundamental_rows = [
        r for r in data.latest_fundamental_rows(snap.conn, args.date) if r["raw_value"] is not None
    ]
    totals = [data.fundamental_value_under(r, weights) for r in fundamental_rows]
    if totals:
        entry = _winsor_row(totals)
        entry["members_normalized"] = len(fundamental_rows)
        entry["blended_top30"] = _blend_under(
            snap,
            args.date,
            {int(r["asset_id"]): v for r, v in zip(fundamental_rows, totals, strict=True)},
        )
        payload["score_types"]["FUNDAMENTAL"] = entry
    _write(args.out, payload)


def _winsor_row(raws: list[float]) -> dict[str, Any]:

    old = normalized_scores(raws, winsor=0.02)
    new = normalized_scores(raws, winsor=0.005)
    return {
        "n": len(raws),
        "trimmed_per_tail_at_half_percent": int(len(raws) * 0.005),
        "trimmed_per_tail_at_two_percent": int(len(raws) * 0.02),
        "movers_over_one_point": sum(
            1 for a, b in zip(old, new, strict=True) if abs(a - b) > ONE_POINT
        ),
        "max_move": max((abs(a - b) for a, b in zip(old, new, strict=True)), default=0.0),
    }


def _blend_under(snap: Snapshot, date: str, fundamental: dict[int, float]) -> dict[str, Any]:
    """The stored MONITORING ranking re-blended with each winsorizing of FUNDAMENTAL alone."""

    rows = list(
        snap.conn.execute(
            "SELECT r.asset_id, a.ticker, r.blended_score, r.components_json FROM cycle_ranking r "
            "JOIN assets a ON a.id = r.asset_id JOIN cycle_run cr ON cr.id = r.cycle_run_id "
            "WHERE cr.cycle_date = ? AND cr.cycle_type = 'MONITORING'",
            (date,),
        )
    )
    if not rows:
        return {"note": "no MONITORING ranking stored at this date"}
    assets = [int(r["asset_id"]) for r in rows if int(r["asset_id"]) in fundamental]
    if not assets:
        return {"note": "the ranking holds no member the FUNDAMENTAL reader scores"}
    totals = [fundamental[a] for a in assets]
    old_norm = normalized_scores(totals, winsor=0.02)
    new_norm = normalized_scores(totals, winsor=0.005)
    old_by = dict(zip(assets, old_norm, strict=True))
    new_by = dict(zip(assets, new_norm, strict=True))
    out: dict[str, Any] = {"before": [], "after": [], "overlap": []}
    for label, replacement in (("before", old_by), ("after", new_by)):
        scored = []
        for r in rows:
            aid = int(r["asset_id"])
            components = json.loads(r["components_json"] or "{}")
            present = [v for k, v in components.items() if v is not None and k != "FUNDAMENTAL"]
            if aid in replacement:
                present.append(replacement[aid])
            if not present:
                continue
            mean = sum(present) / len(present)
            stored_mean = _stored_mean(components)
            penalty = float(r["blended_score"]) - stored_mean if stored_mean is not None else 0.0
            scored.append((str(r["ticker"]), mean - penalty))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        out[label] = [ticker for ticker, _score in scored[:30]]
    out["overlap"] = sorted(set(out["before"]) & set(out["after"]))
    out["note"] = (
        "the other components are the stored run's (the copies hold no TECHNICAL); each asset's "
        "soft-veto penalty is held at what the stored blend implies (blended_score - its stored "
        "component mean), so only the FUNDAMENTAL winsorizing moves"
    )
    return out


def _stored_mean(components: dict[str, Any]) -> float | None:
    values = [float(v) for v in components.values() if v is not None]
    return sum(values) / len(values) if values else None


# -- coverage -------------------------------------------------------------------------------------


def cmd_coverage(args: argparse.Namespace) -> None:
    """Per date and company type: the applicable/present/empty counts per measure, and the
    VALORIZATION-empty members with their reason."""
    payload: dict[str, Any] = {"dates": {}}
    for date in args.dates:
        snap = Snapshot(args.db, date)
        scores = {int(s.asset_id): s for s in snap.scores()}
        by_type: dict[str, Any] = {}
        for aid, score in scores.items():
            profile = snap.profiles.get(aid)
            kind = _kind(profile)
            entry = by_type.setdefault(
                kind,
                {
                    "members": 0,
                    "empty": 0,
                    "no_market_cap": 0,
                    "measures": {m: {"applicable": 0, "present": 0} for m in valorization.MEASURES},
                    "empty_reasons": {},
                },
            )
            entry["members"] += 1
            components = score.components or {}
            measures = components.get("measures") or {}
            if score.raw_value is None:
                entry["empty"] += 1
                if snap.mcap.get(aid) is None:
                    entry["no_market_cap"] += 1
                reason = next(
                    (
                        f"{name}:{m['status']}"
                        for name, m in measures.items()
                        if m.get("status") not in (None, "not_applicable", "present")
                    ),
                    "not_applicable",
                )
                entry["empty_reasons"][reason] = entry["empty_reasons"].get(reason, 0) + 1
            for name, measure in measures.items():
                if measure.get("matrix") != "NA":
                    entry["measures"][name]["applicable"] += 1
                if measure.get("status") == "present":
                    entry["measures"][name]["present"] += 1
        payload["dates"][date] = by_type
    payload["ebitda_empty"] = _ebitda_breakdown(Snapshot(args.db, MAIN))
    _write(args.out, payload)


def _ebitda_breakdown(snap: Snapshot) -> dict[str, Any]:
    """F4: why ``leverage.ebitda`` is empty on the members whose latest filing lacks it -- the D&A
    refusal (by its own flag), an absent D&A, or an absent operating income -- with the counts and
    the 20 largest companies by market cap in each class."""
    classes: dict[str, list[dict[str, Any]]] = {}
    missing = 0
    for aid in snap.asset_ids:
        if (snap.metrics.get(aid) or {}).get("leverage.ebitda") is not None:
            continue
        missing += 1
        row = snap.conn.execute(
            """
            WITH known AS (
                SELECT f.id, f.asset_id, ROW_NUMBER() OVER (
                    PARTITION BY f.asset_id ORDER BY f.period_end DESC, f.available_at DESC, f.id DESC
                ) AS rn
                FROM sec_filings f
                WHERE f.period_end IS NOT NULL AND f.available_at IS NOT NULL AND f.available_at <= ?
            )
            SELECT m.inputs_json FROM fundamental_metrics m
            JOIN known k ON k.id = m.filing_id AND k.rn = 1
            WHERE k.asset_id = ? AND m.metric_group = 'leverage' AND m.metric_name = 'ebitda'
            """,
            (snap.date, aid),
        ).fetchone()
        inputs = json.loads(row["inputs_json"]) if row is not None else {}
        flags = sorted(k for k in inputs if k.startswith("flag_"))
        da_flags = [f for f in flags if "depreciation" in f or "amortization" in f]
        op_flags = [f for f in flags if "operating_income" in f]
        if da_flags:
            klass = f"da_refused:{da_flags[0]}"
        elif op_flags:
            klass = f"operating_income_refused:{op_flags[0]}"
        elif "depreciation_amortization" not in inputs:
            klass = "da_absent"
        elif "operating_income" not in inputs:
            klass = "operating_income_absent"
        else:
            klass = "other"
        classes.setdefault(klass, []).append(
            {"ticker": snap.ticker.get(aid), "market_cap": snap.mcap.get(aid)}
        )
    return {
        "members": len(snap.asset_ids),
        "members_without_ebitda": missing,
        "classes": {
            klass: {
                "count": len(rows),
                "largest_by_market_cap": [
                    r["ticker"]
                    for r in sorted(rows, key=lambda r: r["market_cap"] or 0.0, reverse=True)[:20]
                ],
            }
            for klass, rows in sorted(classes.items(), key=lambda kv: -len(kv[1]))
        },
    }


def _kind(profile: Any) -> str:
    if profile is None:
        return "unknown_profile"
    if profile.financial_firm:
        return "financial_firm"
    if profile.reit:
        return "reit"
    if profile.regulated_utility:
        return "regulated_utility"
    if profile.type == "multi_sector_holding":
        return "multi_sector_holding"
    return "operating"


# -- D1: the preferred-stock measurement ----------------------------------------------------------


def _median_float(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def cmd_book_equity(args: argparse.Namespace) -> None:
    """D1: the members filing ``PreferredStockValue`` > 0 and its median share of parent equity."""
    snap = Snapshot(args.db, args.date)
    rows = []
    for aid in snap.asset_ids:
        filing = snap.conn.execute(
            """
            WITH known AS (
                SELECT f.id, f.asset_id, ROW_NUMBER() OVER (
                    PARTITION BY f.asset_id ORDER BY f.period_end DESC, f.available_at DESC, f.id DESC
                ) AS rn
                FROM sec_filings f
                WHERE f.period_end IS NOT NULL AND f.available_at IS NOT NULL AND f.available_at <= ?
            )
            SELECT ff.period_key, ff.value FROM financial_facts ff
            JOIN known k ON k.id = ff.filing_id AND k.rn = 1
            WHERE k.asset_id = ? AND ff.concept = 'us-gaap_PreferredStockValue' AND ff.value > 0
            """,
            (args.date, aid),
        ).fetchone()
        if filing is None:
            continue
        inputs = (snap.values.get(aid) or {}).get("inputs") or {}
        equity = inputs.get("book_equity")
        preferred = float(filing["value"])
        rows.append(
            {
                "ticker": snap.ticker.get(aid),
                "period_key": filing["period_key"],
                "preferred": preferred,
                "book_equity": equity,
                "share_of_equity": (preferred / equity) if equity else None,
            }
        )
    shares = [r["share_of_equity"] for r in rows if r["share_of_equity"] is not None]
    payload = {
        "date": args.date,
        "members": len(snap.asset_ids),
        "members_filing_preferred_positive": len(rows),
        "median_share_of_equity": _median_float(shares),
        "rows": rows,
        "note": (
            "D1's declared difference from C4: preferred stock is NOT subtracted from book equity "
            "(the checklist leaves it pending, as in MD-35). This is its measured size: the members "
            "whose newest filing reports a positive us-gaap_PreferredStockValue, and that value's "
            "median share of the parent equity the value input carries (book_equity)."
        ),
    }
    _write(args.out, payload)


# -- D5 -------------------------------------------------------------------------------------------


def cmd_multi_class(args: argparse.Namespace) -> None:
    """D5's table: the staged cover classes and the profile's flag."""
    payload: dict[str, Any] = {"rows": {}, "summary": {}}
    staged = {}
    covers = Path(args.covers)
    if covers.exists():
        for line in covers.read_text().splitlines():
            if not line or line.startswith("#") or line.startswith("cik,"):
                continue
            parts = line.split(",")
            staged[parts[0]] = (parts[9] == "True", parts[8])
    prof = profiles()
    for cik, profile in prof.items():
        flag, n = staged.get(cik, (None, None))
        payload["rows"][cik] = {
            "ticker": profile.ticker,
            "leaver": bool(profile.valid_to),
            "profile_multi_class": profile.multi_class,
            "cover_multi_class": flag,
            "cover_n_common_classes": n,
        }
    payload["summary"] = {
        "rows": len(payload["rows"]),
        "staged": len(staged),
        "leaver_flags": sum(
            1 for r in payload["rows"].values() if r["leaver"] and r["profile_multi_class"]
        ),
        "current_true": sum(
            1 for r in payload["rows"].values() if not r["leaver"] and r["profile_multi_class"]
        ),
    }
    _write(args.out, payload)


# -- D6: FRT/VTR and the D-14 table ---------------------------------------------------------------


def cmd_reit_capex(args: argparse.Namespace) -> None:
    """D6: the capex resolution of the named REITs' newest 10-K and 10-Q (the side's own resolver)."""
    conn = data.connect_ro(args.db)
    payload: dict[str, Any] = {"side": args.side, "companies": {}}
    for ticker in args.tickers.split(","):
        row = conn.execute(
            "SELECT a.id, a.cik, f.id AS filing_id, f.form, f.period_end FROM sec_filings f "
            "JOIN assets a ON a.id = f.asset_id WHERE a.ticker = ? AND f.period_end IS NOT NULL "
            "ORDER BY f.period_end DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        if row is None:
            continue
        stmts = agent_db.statements_from_facts(conn, int(row["filing_id"]))
        profile = profile_as_of(row["cik"], str(row["period_end"]))
        stmts.reit = bool(profile and profile.reit)
        tag = "FY" if str(row["form"]).startswith("10-K") else "YTD"
        column = next(
            (
                p.key
                for p in stmts.periods
                if p.tag == tag and p.date == str(row["period_end"])[:10]
            ),
            None,
        )
        if column is None:
            continue
        capex = stmts.resolve("capital_expenditure", column)
        ocf = stmts.resolve("operating_cash_flow", column)
        payload["companies"][ticker] = {
            "form": str(row["form"]),
            "period_end": str(row["period_end"]),
            "column": column,
            "capex": capex.value,
            "terms": capex.concept,
            "how": capex.how,
            "flags": sorted(capex.flags),
            "operating_cash_flow": ocf.value,
            "fcf": (ocf.value - capex.value)
            if ocf.value is not None and capex.value is not None
            else None,
        }
    _write(args.out, payload)


# APP-04b's four parts: corporate items + existing properties + development + acquisitions.
PARTS: Final[tuple[str, ...]] = (
    "corporate",
    "existing_properties",
    "development",
    "acquisitions",
)
_CONCEPT_PART: Final[dict[str, str]] = {
    "us-gaap_PaymentsToAcquirePropertyPlantAndEquipment": "corporate",
    "us-gaap_PaymentsToAcquireProductiveAssets": "corporate",
    "us-gaap_PaymentsToAcquireOtherProductiveAssets": "corporate",
    "us-gaap_PaymentsForCapitalImprovements": "existing_properties",
    "us-gaap_PaymentsToDevelopRealEstateAssets": "development",
    "us-gaap_PaymentsToAcquireRealEstate": "acquisitions",
    "us-gaap_PaymentsToAcquireRealEstateHeldForInvestment": "acquisitions",
    "us-gaap_PaymentsToAcquireCommercialRealEstate": "acquisitions",
}


def _part_of(concept: str) -> str:
    """APP-04b's part of one term the capex composite summed (a filer extension by its own wording)."""
    if concept in _CONCEPT_PART:
        return _CONCEPT_PART[concept]
    low = concept.lower()
    if "development" in low or "redevelopment" in low:
        return "development"
    if "acquir" in low:
        return "acquisitions"
    if "capitalimprovement" in low:
        return "existing_properties"
    return "corporate"


def _parts_of_column(
    conn: Any, spec: Any, stmts: Any, column: str
) -> tuple[dict[str, float], str | None]:
    """APP-04b's four parts of one column's capex composite, from the terms it summed.

    The provenance records a year-to-date column's tag as ``(YTD)``; on a **first quarter** filing
    the facts key that same column ``(Q1)`` (``_is_first_quarter``), so the bare column is tried
    first and its quarter tag next."""
    resolution = stmts.resolve("capital_expenditure", column)
    if resolution.concept is None and str(column).endswith("(YTD)"):
        column = f"{str(column).removesuffix('(YTD)').rstrip()} (Q1)"
        resolution = stmts.resolve("capital_expenditure", column)
    parts = dict.fromkeys(PARTS, 0.0)
    for raw in (resolution.concept or "").split(" + "):
        concept = raw.strip()
        if not concept:
            continue
        value = stmts._concept_value(spec, column, concept)  # the resolver's own read
        if value is None:
            continue
        parts[_part_of(concept)] += float(value)
    return parts, resolution.concept


def _first(item: Any) -> dict[str, Any]:
    """A provenance item: the block's own list-of-one (T-151 part 3's shape), or the dict itself."""
    if isinstance(item, list):
        return item[0] if item and isinstance(item[0], dict) else {}
    return item if isinstance(item, dict) else {}


def _reit_capex_row(conn: Any, spec: Any, aid: int, ticker: str, date: str) -> dict[str, Any]:
    """One REIT's capex on the **TTM basis** (a 10-K's own fiscal year), split into APP-04b's four
    parts, from the composite's own provenance terms."""
    filing = conn.execute(
        """
        WITH known AS (
            SELECT f.id, ROW_NUMBER() OVER (
                PARTITION BY f.asset_id ORDER BY f.period_end DESC, f.available_at DESC, f.id DESC
            ) AS rn
            FROM sec_filings f
            WHERE f.period_end IS NOT NULL AND f.available_at IS NOT NULL AND f.available_at <= ?
        )
        SELECT f.id, f.period_end, f.accession_number, m.inputs_json FROM fundamental_metrics m
        JOIN sec_filings f ON f.id = m.filing_id JOIN known k ON k.id = f.id AND k.rn = 1
        WHERE f.asset_id = ? AND m.metric_group = 'cashflow'
          AND m.metric_name = 'free_cash_flow_margin'
        """,
        (date, aid),
    ).fetchone()
    if filing is None:
        return {"ticker": ticker, "note": "no cashflow metric"}
    inputs = json.loads(filing["inputs_json"] or "{}")
    provenance = inputs.get("provenance") or {}
    items = provenance.get("items") or {}
    ocf_item = _first(items.get("operating_cash_flow"))
    capex_item = _first(items.get("capital_expenditure"))
    stmts = agent_db.statements_from_facts(conn, int(filing["id"]))
    profile = profile_as_of(
        conn.execute("SELECT cik FROM assets WHERE id = ?", (aid,)).fetchone()["cik"],
        str(filing["period_end"]),
    )
    stmts.reit = bool(profile and profile.reit)
    # the terms the composite summed, per column the TTM used (its provenance's own columns).
    # A **10-K** records no TTM columns and no per-column value: its own fiscal year is resolved
    # (the basis its metrics use), from the same terms the provenance names.
    columns = capex_item.get("columns") or []
    if not columns:
        column = f"{filing['period_end']} (FY)"
        parts, concepts = _parts_of_column(conn, spec, stmts, column)
        total = sum(parts.values())
        ocf_value = stmts.resolve("operating_cash_flow", column).value
        capex_value = stmts.resolve("capital_expenditure", column).value
        return {
            "ticker": ticker,
            "period_end": str(filing["period_end"]),
            "basis": "fiscal_year",
            "method": None,
            "operating_cash_flow": ocf_value,
            "capex": capex_value,
            "capex_parts": parts,
            "capex_total_from_parts": total,
            "acquisition_share": (parts["acquisitions"] / total) if total else None,
            "free_cash_flow": _difference(ocf_value, capex_value),
            "terms": concepts,
        }
    per_column: dict[str, dict[str, float]] = {}
    concepts_by_column: dict[str, str | None] = {}
    for label, raw_column, accession in columns:
        # the provenance's fiscal-year column carries the bare period end; the facts key it "(FY)"
        column = (
            f"{raw_column} (FY)"
            if label in ("prior_fiscal_year", "fiscal_year")
            and not str(raw_column).endswith("(FY)")
            else raw_column
        )
        source = stmts
        row = conn.execute(
            "SELECT id FROM sec_filings WHERE accession_number = ?", (accession,)
        ).fetchone()
        if row is not None and int(row["id"]) != int(filing["id"]):
            source = agent_db.statements_from_facts(conn, int(row["id"]))
            source.reit = stmts.reit
        per_column[label], concepts_by_column[label] = _parts_of_column(conn, spec, source, column)
    parts = dict.fromkeys(PARTS, 0.0)
    if capex_item.get("basis") == "ttm" and {"prior_fiscal_year", "prior_ytd", "ytd"} <= set(
        per_column
    ):
        for part in PARTS:
            parts[part] = (
                per_column["prior_fiscal_year"][part]
                - per_column["prior_ytd"][part]
                + per_column["ytd"][part]
            )
    else:  # a 10-Q whose TTM used another column set: its own year-to-date
        own = per_column.get("ytd") or next(iter(per_column.values()), dict.fromkeys(PARTS, 0.0))
        parts = dict(own)
    total = sum(parts.values())
    return {
        "ticker": ticker,
        "period_end": str(filing["period_end"]),
        "basis": capex_item.get("basis"),
        "method": capex_item.get("method"),
        "operating_cash_flow": ocf_item.get("value"),
        "capex": capex_item.get("value"),
        "capex_parts": parts,
        "capex_total_from_parts": total,
        "acquisition_share": (parts["acquisitions"] / total) if total else None,
        "free_cash_flow": _difference(ocf_item.get("value"), capex_item.get("value")),
        "terms": concepts_by_column,
    }


def cmd_negative_fcf(args: argparse.Namespace) -> None:
    """G1: NEGATIVE_FCF's hits by sector at each date, and the REITs the exemption takes out. Run
    once per side (the before side under master's code, whose rule has no REIT exemption)."""
    payload: dict[str, Any] = {"side": args.side, "dates": {}}
    for date in args.dates:
        snap = Snapshot(args.db, date)
        ctx = RuleContext(
            cycle_date=date,
            metrics={a: snap.metrics.get(a, {}) for a in snap.asset_ids},
            price_obs={},
            last_fundamental={},
            profiles=snap.profiles,
        )
        result = _NegativeFcfRule().evaluate(ctx)
        hits = sorted(h.asset_id for h in result.hits)
        by_sector: dict[str, list[str]] = {}
        reits: list[str] = []
        for aid in hits:
            ticker = snap.ticker.get(aid) or ""
            sector = snap.sector.get(aid) or "(none)"
            by_sector.setdefault(sector, []).append(ticker)
            profile = snap.profiles.get(aid)
            if profile is not None and profile.reit:
                reits.append(ticker)
        payload["dates"][date] = {
            "hits": len(hits),
            "evaluated": len(result.evaluated),
            "members": len(snap.asset_ids),
            "by_sector": {k: sorted(v) for k, v in sorted(by_sector.items())},
            "reit_hits": sorted(reits),
            "reit_members": sorted(
                snap.ticker.get(a, "") or ""
                for a in snap.asset_ids
                if (p := snap.profiles.get(a)) is not None and p.reit
            ),
        }
    _write(args.out, payload)


def _difference(left: Any, right: Any) -> float | None:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) - float(right)
    return None


def cmd_d14(args: argparse.Namespace) -> None:
    """The re-measured D-14 table, decision-ready: for every REIT vetoed by NEGATIVE_FCF at
    2026-09-22 **and at each quarter end**, its operating cash flow, capex split into APP-04b's four
    parts (from the composite's own provenance terms, on the TTM basis on a 10-Q), FCF and the
    acquisition share."""
    spec = S.REGISTRY["capital_expenditure"]
    conn = data.connect_ro(args.db)
    payload: dict[str, Any] = {"dates": {}}
    for date in args.dates:
        snap = Snapshot(args.db, date)
        vetoed = []
        for aid in snap.asset_ids:
            profile = snap.profiles.get(aid)
            if profile is None or not profile.reit:
                continue
            margin = (snap.metrics.get(aid) or {}).get("cashflow.free_cash_flow_margin")
            if margin is not None and margin < 0:
                vetoed.append((aid, snap.ticker.get(aid) or ""))
        rows = []
        for aid, ticker in vetoed:
            row = _reit_capex_row(conn, spec, aid, ticker, date)
            row["free_cash_flow_margin"] = (snap.metrics.get(aid) or {}).get(
                "cashflow.free_cash_flow_margin"
            )
            rows.append(row)
        payload["dates"][date] = {
            "reits_with_negative_fcf": sorted(ticker for _aid, ticker in vetoed),
            "rows": rows,
        }
    payload["note"] = (
        "NEGATIVE_FCF = cashflow.free_cash_flow_margin < 0 on the member's newest usable filing. "
        "capex_parts is APP-04b's split of the terms the capex composite summed, on the metric's own "
        "basis (TTM on a 10-Q: prior fiscal year - last year's YTD + this year's YTD); the parts' sum "
        "is checked against the metric's capex in capex_total_from_parts."
    )
    _write(args.out, payload)


def _common(p: argparse.ArgumentParser) -> None:
    """``--db``, ``--out`` and ``--date`` for the subcommands that read one database."""
    p.add_argument("--db", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--date", default=MAIN)


def _subcommands(sub: Any) -> None:
    """Wire every subcommand (kept out of :func:`main`, which stays a short dispatcher)."""
    p = sub.add_parser("scores")
    _common(p)
    p.add_argument("--side", required=True)
    p.set_defaults(func=cmd_scores)

    p = sub.add_parser("compare")
    p.add_argument("--before", required=True)
    p.add_argument("--after", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--date", default=MAIN)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("value-measures")
    _common(p)
    p.set_defaults(func=cmd_value_measures)

    p = sub.add_parser("blend")
    _common(p)
    p.add_argument("--side", required=True)
    p.add_argument("--observation-engine-version", default="priceobs-v2")
    p.add_argument("--fresh", action="store_true", help="drop the stored run first, so it re-runs")
    p.set_defaults(func=cmd_blend)

    p = sub.add_parser("dq02")
    _common(p)
    p.set_defaults(func=cmd_dq02)

    p = sub.add_parser("coverage")
    _common(p)
    p.add_argument("--dates", default=",".join(DATES), type=lambda s: s.split(","))
    p.set_defaults(func=cmd_coverage)

    p = sub.add_parser("book-equity")
    _common(p)
    p.set_defaults(func=cmd_book_equity)

    p = sub.add_parser("multi-class")
    p.add_argument("--covers", default="docs/multi_class_covers.csv")
    p.add_argument("--out", required=True)
    p.add_argument("--db", default=None)
    p.add_argument("--date", default=MAIN)
    p.set_defaults(func=cmd_multi_class)

    p = sub.add_parser("reit-capex")
    _common(p)
    p.add_argument("--side", default="after")
    p.add_argument("--tickers", default="FRT,VTR,BXP,HST,O,WELL,DOC,UDR,CPT,ESS,VMRK,WY,PSA")
    p.set_defaults(func=cmd_reit_capex)

    p = sub.add_parser("negative-fcf")
    _common(p)
    p.add_argument("--side", required=True)
    p.add_argument("--dates", default=",".join(DATES), type=lambda s: s.split(","))
    p.set_defaults(func=cmd_negative_fcf)

    p = sub.add_parser("d14")
    _common(p)
    p.add_argument("--dates", default=",".join(DATES), type=lambda s: s.split(","))
    p.set_defaults(func=cmd_d14)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    _subcommands(sub)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
