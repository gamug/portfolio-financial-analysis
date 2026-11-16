#!/usr/bin/env python3
"""T-143 part A -- V4: every rule recomputed independently.

This session is an **independent** re-evaluation of the cycle's veto rules and of
the Ring-1 data-quality gates.  It reads the pilot database read-only and writes
three artefacts into the V4 handoff directory:

  * ``v4_independent.csv`` -- one row per (date, rule_id, asset):
    ``date, rule_id, asset_id, ticker, evaluated, hit, severity, inputs_json``
  * ``v4_gates.csv``      -- ``filing_id, gate, yours, stored, agree``
  * ``v4_summary.md``     -- per rule and date the evaluated/hit counts by
    sector, the boundary cases and every decision taken.

Standard library + ``sqlite3`` only.  No LLM, no network, no database write.

Sources of the rule texts (read-only, and the only ones used):
  * ``.specify/memory/SPEC.md`` FR-006 (the vetoes, thresholds, exemptions,
    temporal holds)
  * ``docs/model_fixes.md`` T-070, T-071 part 2a (D1, D2/D-04, D5, D-13) and
    part 2b (D-14), T-116
  * ``docs/checklist_sec/checklist_v1.3.md`` DQ-01, the CON gates, Annex A,
    APP-00/APP-02/APP-07/APP-09
  * ``docs/metric_dictionary.md``
  * ``src/kg_schema/data/company_profile.csv`` (the APP-00 profile)

Run:
    .venv/bin/python scripts/pilot_a/independent_rules.py
"""

from __future__ import annotations

import csv
import json
import math
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date

# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))

PILOT_DB = "/workspaces/thesis/pilot_a2/financial.db"
GICS_DB = "/workspaces/thesis/data/universe_history_gics.db"
PROFILE_CSV = os.path.join(REPO, "src", "kg_schema", "data", "company_profile.csv")
DATES_TXT = "/workspaces/thesis/handoff/pilot/v4/dates.txt"
OUT_DIR = "/workspaces/thesis/handoff/pilot/v4"

OBS_ENGINE = "priceobs-v2"
DQ_GATE_VERSION = "dq-v4"

# thresholds (every one cited in the summary)
LEV_DTE_THRESHOLD = 3.0
LEV_ND_EBITDA_THRESHOLD = 5.0
LEV_INTEREST_COVERAGE_THRESHOLD = 1.5
LIQ_CURRENT_RATIO_THRESHOLD = 1.0
LIQ_INTEREST_COVERAGE_THRESHOLD = 1.5
UTIL_CASH_COVERAGE_THRESHOLD = 0.05
TREND_BREAK_FACTOR = 0.95
VOL_SHOCK_THRESHOLD = 2.5
CRASH_Z_THRESHOLD = -2.5
PRICE_CRASH_THRESHOLD = -0.35
STALE_OBSERVATION_DAYS = 7

# the Annex A screens (checklist v1.3, Annex A): the gate fires OUTSIDE the range
DQ_MARGIN_LIMIT = 5.0
DQ_MARGIN_REVIEW_FLOOR = 1.0
DQ_OCF_MARGIN_LIMIT = 3.0
DQ_FCF_YIELD_LIMIT = 0.5
DQ_MCAP_SCALE_RANGE = (0.001, 100.0)

# the arithmetic tolerances
UNIT_TOLERANCE = 1e-6
QUARTER_DAY_TOLERANCE = 10
SECTOR_MIN_NAMES = 5

HARD_DQ_RULES = ("DQ_FCF_YIELD", "DQ_MARGIN", "DQ_MCAP_SCALE", "DQ_OCF_MARGIN", "DQ_REVENUE_POS")

RULE_SEVERITY = {
    "LEVERAGE_EXTREME": "HARD",
    "NEGATIVE_FCF": "HARD",
    "UTILITY_CASH_COVERAGE": "HARD",
    "LIQUIDITY_DISTRESS": "SOFT",
    "DATA_QUALITY": "HARD",
    "EARNINGS_MISSING": "SOFT",
    "BREAK_TREND_200": "SOFT",
    "PRICE_CRASH": "SOFT",
    "VOLATILITY_SHOCK": "HARD",
    "CRASH_Z_SCORE": "HARD",
}

# metric rows the rules read
METRIC_KEYS = (
    ("leverage", "debt_to_equity"),
    ("leverage", "ebitda"),
    ("leverage", "net_debt_to_ebitda"),
    ("leverage", "interest_coverage"),
    ("liquidity", "current_ratio"),
    ("cashflow", "operating_cash_flow_margin"),
    ("cashflow", "free_cash_flow_margin"),
    ("profitability", "net_margin"),
    ("valuation", "market_capitalization"),
    ("valuation", "enterprise_value"),
    ("valuation", "free_cash_flow_yield"),
    ("rule_input", "utility_cash_coverage_3y"),
)


def log(msg: str) -> None:
    print(msg, flush=True)


def d(x: str) -> date:
    y, m, dd = (int(p) for p in x.split("-"))
    return date(y, m, dd)


def json_compact(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


# --------------------------------------------------------------------------
# loaders
# --------------------------------------------------------------------------


def load_dates() -> list[str]:
    with open(DATES_TXT) as fh:
        return [line.strip() for line in fh if line.strip()]


def load_profile() -> dict[str, dict]:
    """the APP-00 profile, one row per CIK, with its validity interval."""
    with open(PROFILE_CSV) as fh:
        raw = fh.read()
    lines = [ln for ln in raw.split("\n") if ln and not ln.startswith("#")]
    out: dict[str, dict] = {}
    for row in csv.DictReader(lines):
        out[row["cik"]] = {
            "tickers": row["tickers"],
            "type": row["type"],
            "financial_firm": row["financial_firm"].strip() == "True",
            "reit": row["reit"].strip() == "True",
            "regulated_utility": row["regulated_utility"].strip() == "True",
            "valid_from": row["valid_from"].strip(),
            "valid_to": row["valid_to"].strip(),
        }
    return out


def load_gics() -> dict[str, list[tuple[str, str | None, str | None]]]:
    """the historical GICS membership: CIK -> [(valid_from, valid_to, sector)]."""
    con = sqlite3.connect(f"file:{GICS_DB}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT cik, valid_from, valid_to, gics_sector FROM universe_membership WHERE cik IS NOT NULL AND cik <> ''"
    ).fetchall()
    con.close()
    out: dict[str, list[tuple[str, str | None, str | None]]] = defaultdict(list)
    for cik, vfrom, vto, sector in rows:
        out[cik].append((vfrom, vto, sector))
    return out


def load_assets(con: sqlite3.Connection) -> dict[int, dict]:
    rows = con.execute("SELECT id, ticker, cik, sector_id FROM assets").fetchall()
    return {r[0]: {"ticker": r[1], "cik": r[2], "sector_id": r[3]} for r in rows}


def load_filings(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute(
        "SELECT id, asset_id, form, period_end, available_at, fiscal_period FROM sec_filings"
    ).fetchall()
    return [
        {
            "id": r[0],
            "asset_id": r[1],
            "form": r[2],
            "period_end": r[3],
            "available_at": r[4],
            "fiscal_period": r[5],
        }
        for r in rows
    ]


def load_metrics(con: sqlite3.Connection) -> dict[int, dict[tuple[str, str], dict]]:
    """filing_id -> {(group, name): {value, inputs}} for the rows the rules read."""
    out: dict[int, dict[tuple[str, str], dict]] = defaultdict(dict)
    wanted = set(METRIC_KEYS)
    for fid, group, name, value, inputs_json in con.execute(
        "SELECT filing_id, metric_group, metric_name, value, inputs_json FROM fundamental_metrics"
    ):
        if (group, name) not in wanted:
            continue
        if group == "rule_input":
            # only the utility rule input is parsed eagerly; value_inputs is not read
            pass
        out[fid][(group, name)] = {"value": value, "inputs_json": inputs_json}
    return out


def load_dq(con: sqlite3.Connection) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = defaultdict(list)
    for fid, rule, sev, quar, ev, gv in con.execute(
        "SELECT filing_id, rule_id, severity, quarantined, evidence_json, gate_version FROM data_quality_issue"
    ):
        out[fid].append(
            {
                "rule_id": rule,
                "severity": sev,
                "quarantined": quar,
                "evidence": ev,
                "gate_version": gv,
            }
        )
    return out


def load_observations(
    con: sqlite3.Connection, asset_ids: list[int], dates: list[str]
) -> dict[str, dict[int, dict]]:
    """date -> {asset_id: the newest price observation on or before the date}."""
    out: dict[str, dict[int, dict]] = {}
    for day in dates:
        rows = con.execute(
            "SELECT po.asset_id, po.obs_date, po.close, po.sma_200, po.ret_5d, po.vol_5d, "
            "po.mu_60d_base, po.vol_60d_base, po.max_drawdown_90d "
            "FROM price_observation po JOIN ("
            "  SELECT asset_id, MAX(obs_date) AS md FROM price_observation "
            "  WHERE engine_version = ? AND obs_date <= ? GROUP BY asset_id) m "
            "ON po.asset_id = m.asset_id AND po.obs_date = m.md "
            "WHERE po.engine_version = ?",
            (OBS_ENGINE, day, OBS_ENGINE),
        ).fetchall()
        out[day] = {
            r[0]: {
                "obs_date": r[1],
                "close": r[2],
                "sma_200": r[3],
                "ret_5d": r[4],
                "vol_5d": r[5],
                "mu_60d_base": r[6],
                "vol_60d_base": r[7],
                "max_drawdown_90d": r[8],
            }
            for r in rows
        }
    return out


# --------------------------------------------------------------------------
# point-in-time helpers
# --------------------------------------------------------------------------


def profile_as_of(profile: dict[str, dict], cik: str | None, day: str) -> dict | None:
    """the APP-00 profile row that covers the cycle date.

    The CSV's ``valid_to`` is EXCLUSIVE (``valid_from <= day < valid_to``); an
    empty ``valid_to`` means "still current".  An unknown profile is never
    exempt (FR-006, D5).
    """
    if not cik:
        return None
    row = profile.get(cik)
    if row is None:
        return None
    if row["valid_from"] and day < row["valid_from"]:
        return None
    if row["valid_to"] and day >= row["valid_to"]:
        return None
    return row


def sector_as_of(gics: dict, cik: str | None, day: str) -> str | None:
    """the GICS sector of the CIK's membership line on the cycle date.

    ``universe_history_gics.db``'s ``valid_to`` is the last membership day,
    INCLUSIVE (the same convention as ``universe_history.db``).
    """
    if not cik:
        return None
    best = None
    for vfrom, vto, sector in gics.get(cik, []):
        if vfrom > day:
            continue
        if vto is not None and day > vto:
            continue
        if sector is None or sector == "":
            continue
        if best is None or vfrom > best[0]:
            best = (vfrom, sector)
    return best[1] if best else None


def latest_filing(filings_by_asset: dict[int, list[dict]], asset_id: int, day: str) -> dict | None:
    """the newest usable filing: ``period_end`` DESC, ``available_at`` DESC."""
    best = None
    for f in filings_by_asset.get(asset_id, ()):
        if f["available_at"] is None or f["available_at"] > day:
            continue
        key = (f["period_end"] or "", f["available_at"] or "")
        if best is None or key > best[0]:
            best = (key, f)
    return best[1] if best else None


def latest_10k(filings_by_asset: dict[int, list[dict]], asset_id: int, day: str) -> dict | None:
    best = None
    for f in filings_by_asset.get(asset_id, ()):
        if f["form"] != "10-K":
            continue
        if f["available_at"] is None or f["available_at"] > day:
            continue
        key = (f["period_end"] or "", f["available_at"] or "")
        if best is None or key > best[0]:
            best = (key, f)
    return best[1] if best else None


# --------------------------------------------------------------------------
# the rules
# --------------------------------------------------------------------------


class Evaluator:
    def __init__(  # noqa: PLR0913, PLR0917
        self, con, assets, filings_by_asset, metrics, dq, profile, gics, observations
    ):
        self.con = con
        self.assets = assets
        self.filings_by_asset = filings_by_asset
        self.metrics = metrics
        self.dq = dq
        self.profile = profile
        self.gics = gics
        self.observations = observations
        self._inputs_cache: dict[tuple[int, str, str], dict] = {}
        self.n_fundamental_scores = con.execute(
            "SELECT COUNT(*) FROM fundamental_snapshot"
        ).fetchone()[0]

    # -- helpers ---------------------------------------------------------
    def inputs(self, filing_id: int | None, group: str, name: str) -> dict:
        if filing_id is None:
            return {}
        key = (filing_id, group, name)
        if key in self._inputs_cache:
            return self._inputs_cache[key]
        row = self.metrics.get(filing_id, {}).get((group, name))
        if row is not None and row["inputs_json"]:
            try:
                parsed = json.loads(row["inputs_json"])
            except (ValueError, TypeError):
                parsed = {}
        else:
            parsed = {}
        self._inputs_cache[key] = parsed
        return parsed

    def value(self, filing_id: int | None, group: str, name: str):
        if filing_id is None:
            return None
        row = self.metrics.get(filing_id, {}).get((group, name))
        return None if row is None else row["value"]

    def neg_equity_record(self, filing_id: int | None) -> dict | None:
        if filing_id is None:
            return None
        for row in self.dq.get(filing_id, ()):
            if row["rule_id"] == "DQ_NEG_EQUITY" and row["gate_version"] == DQ_GATE_VERSION:
                try:
                    return json.loads(row["evidence"] or "{}")
                except (ValueError, TypeError):
                    return {}
        return None

    # -- the fundamental vetoes -----------------------------------------
    def leverage_extreme(self, ctx) -> tuple[int, int, str, dict]:  # noqa: PLR0911
        """LEVERAGE_EXTREME -- HARD (T-071 2a D2/D-04, T-116)."""
        filing = ctx["filing"]
        if filing is None:
            return 0, 0, "no usable filing", {}
        fid = filing["id"]
        lev_inputs = self.inputs(fid, "leverage", "debt_to_equity")
        equity = lev_inputs.get("equity")
        dte = self.value(fid, "leverage", "debt_to_equity")
        record = self.neg_equity_record(fid)
        prof = ctx["profile"]
        evidence = {
            "filing_id": fid,
            "equity": equity,
            "debt_to_equity": dte,
            "profile_financial_firm": bool(prof and prof["financial_firm"]),
        }
        negative = record is not None or (equity is not None and equity <= 0)
        if not negative:
            # the positive-equity branch: debt_to_equity > 3.0
            if dte is None:
                evidence["reason"] = (
                    "debt_to_equity unavailable (APP-07 negative equity, APP-02 financial firm, or a capture failure)"
                )
                return 0, 0, evidence["reason"], evidence
            evidence["branch"] = "debt_to_equity"
            hit = 1 if dte > LEV_DTE_THRESHOLD else 0
            return 1, hit, "", evidence
        # the equity-sign branch (D2 / D-04)
        evidence["branch"] = "negative_equity"
        if prof and prof["financial_firm"]:
            evidence["reason"] = (
                "financial firm (APP-02): the negative-equity branch does not apply"
            )
            return 0, 0, evidence["reason"], evidence
        if record is None:
            evidence["reason"] = "no DQ_NEG_EQUITY record for the latest usable filing"
            return 0, 0, evidence["reason"], evidence
        ebitda = record.get("ebitda")
        net_debt = record.get("net_debt")
        nd_ebitda = record.get("net_debt_to_ebitda")
        coverage = record.get("interest_coverage")
        evidence.update(
            {
                "ebitda": ebitda,
                "net_debt": net_debt,
                "net_debt_to_ebitda": nd_ebitda,
                "interest_coverage": coverage,
            }
        )
        leg_ebitda = ebitda is not None and net_debt is not None and ebitda <= 0 and net_debt > 0
        leg_ratio = (
            nd_ebitda is not None
            and ebitda is not None
            and ebitda > 0
            and nd_ebitda > LEV_ND_EBITDA_THRESHOLD
        )
        leg_coverage = coverage is not None and coverage < LEV_INTEREST_COVERAGE_THRESHOLD
        evidence["legs"] = {
            "ebitda_le_0_and_net_debt_gt_0": leg_ebitda,
            "net_debt_to_ebitda_gt_5": leg_ratio,
            "interest_coverage_lt_1_5": leg_coverage,
        }
        usable = [v for v in (ebitda, net_debt, nd_ebitda, coverage) if v is not None]
        if leg_ebitda or leg_ratio or leg_coverage:
            evidence["hard"] = True
            return 1, 1, "", evidence
        # no usable credit input -> SOFT review.
        # READING A (T-116's own words, used here): both corroborating metrics
        # (``net_debt_to_ebitda`` and ``interest_coverage``) are None.
        # READING B (FR-006's phrase "no usable credit input at all"): all four
        # credit inputs (ebitda, net debt, net debt/EBITDA, interest coverage)
        # are None.  The two readings differ only here; both are reported.
        reading_a = nd_ebitda is None and coverage is None
        reading_b = not usable
        evidence["soft_review_reading_a_t116"] = reading_a
        evidence["soft_review_reading_b_fr006"] = reading_b
        if reading_a:
            evidence["soft_review"] = True
            evidence["severity"] = "SOFT"
            evidence["reason"] = (
                "no usable credit input (T-116: both corroborating metrics are None): SOFT review"
            )
            return 1, 1, "", evidence
        return 1, 0, "", evidence

    def negative_fcf(self, ctx) -> tuple[int, int, str, dict]:
        """NEGATIVE_FCF -- HARD (D-13 utility overlay, D-14 REIT overlay)."""
        filing = ctx["filing"]
        if filing is None:
            return 0, 0, "no usable filing", {}
        prof = ctx["profile"]
        margin = self.value(filing["id"], "cashflow", "free_cash_flow_margin")
        evidence = {"filing_id": filing["id"], "free_cash_flow_margin": margin}
        if prof and prof["regulated_utility"]:
            evidence["exempt"] = "regulated-utility overlay (D-13): evaluated and never hit"
            return 1, 0, "", evidence
        if prof and prof["reit"]:
            evidence["exempt"] = (
                "APP-00 REIT overlay on the cycle date (D-14): evaluated and never hit"
            )
            return 1, 0, "", evidence
        if margin is None:
            evidence["reason"] = "free_cash_flow_margin unavailable"
            return 0, 0, evidence["reason"], evidence
        hit = 1 if margin < 0 else 0
        return 1, hit, "", evidence

    def utility_cash_coverage(self, ctx) -> tuple[int, int, str, dict]:
        """UTILITY_CASH_COVERAGE -- HARD (D-13)."""
        prof = ctx["profile"]
        if not (prof and prof["regulated_utility"]):
            return 0, 0, "not a regulated utility: the rule does not apply", {}
        tenk = ctx["tenk"]
        if tenk is None:
            return 0, 0, "no usable 10-K on the cycle date", {}
        row = self.metrics.get(tenk["id"], {}).get(("rule_input", "utility_cash_coverage_3y"))
        if row is None:
            return 0, 0, "no utility_cash_coverage_3y rule input on the newest usable 10-K", {}
        data = self.inputs(tenk["id"], "rule_input", "utility_cash_coverage_3y")
        a_in = data.get("average_ambiguous_in")
        a_wc = data.get("average_ambiguous_as_wc")
        evidence = {
            "tenk_id": tenk["id"],
            "tenk_period_end": tenk["period_end"],
            "average_ambiguous_in": a_in,
            "average_ambiguous_as_wc": a_wc,
            "threshold": UTIL_CASH_COVERAGE_THRESHOLD,
        }
        if a_in is None or a_wc is None:
            evidence["flag"] = "ambiguous_working_capital"
            evidence["reason"] = "a bound is missing: evaluated and never hit"
            return 1, 0, "", evidence
        if a_in < UTIL_CASH_COVERAGE_THRESHOLD and a_wc < UTIL_CASH_COVERAGE_THRESHOLD:
            evidence["hard"] = True
            return 1, 1, "", evidence
        if (a_in < UTIL_CASH_COVERAGE_THRESHOLD) != (a_wc < UTIL_CASH_COVERAGE_THRESHOLD):
            evidence["flag"] = "ambiguous_working_capital"
        return 1, 0, "", evidence

    def liquidity_distress(self, ctx) -> tuple[int, int, str, dict]:
        """LIQUIDITY_DISTRESS -- SOFT (T-070 recalibration, D-5 exemption)."""
        filing = ctx["filing"]
        if filing is None:
            return 0, 0, "no usable filing", {}
        prof = ctx["profile"]
        cr = self.value(filing["id"], "liquidity", "current_ratio")
        ic = self.value(filing["id"], "leverage", "interest_coverage")
        ocf = self.value(filing["id"], "cashflow", "operating_cash_flow_margin")
        evidence = {
            "filing_id": filing["id"],
            "current_ratio": cr,
            "interest_coverage": ic,
            "operating_cash_flow_margin": ocf,
        }
        if prof and (prof["financial_firm"] or prof["regulated_utility"]):
            kind = "financial firm" if prof["financial_firm"] else "regulated-utility overlay"
            evidence["exempt"] = f"{kind} (APP-00 profile, D5): evaluated and never hit"
            return 1, 0, "", evidence
        if cr is None:
            evidence["reason"] = "current_ratio unavailable"
            return 0, 0, evidence["reason"], evidence
        if ic is None and ocf is None:
            # READING A (T-070's wording read literally): neither leg is
            # available -> the rule is not evaluated, whatever the current ratio.
            # READING B (used here): the first condition (``current_ratio <
            # 1.0``) already decides, so the rule IS evaluated and never hit
            # when ``current_ratio >= 1.0``; it is not evaluated only when
            # ``current_ratio < 1.0`` and neither leg is available.
            # READING B reproduces T-156's measured "LIQUIDITY_DISTRESS 470/1"
            # on this database exactly (reading A gives 467/1).
            if cr >= LIQ_CURRENT_RATIO_THRESHOLD:
                evidence["alt_evaluated"] = 1
                return 1, 0, "", evidence
            evidence["reason"] = (
                "neither interest_coverage nor operating_cash_flow_margin is available"
            )
            return 0, 0, evidence["reason"], evidence
        legs = []
        if ic is not None:
            legs.append(ic < LIQ_INTEREST_COVERAGE_THRESHOLD)
        if ocf is not None:
            legs.append(ocf < 0)
        hit = 1 if (cr < LIQ_CURRENT_RATIO_THRESHOLD and any(legs)) else 0
        return 1, hit, "", evidence

    def data_quality(self, ctx) -> tuple[int, int, str, dict]:
        """DATA_QUALITY -- HARD (the Ring-1 DQ_* gates, gate version dq-v4)."""
        filing = ctx["filing"]
        if filing is None:
            return 0, 0, "no usable filing", {}
        rows = [
            r
            for r in self.dq.get(filing["id"], ())
            if r["gate_version"] == DQ_GATE_VERSION and r["rule_id"] in HARD_DQ_RULES
        ]
        evidence = {
            "filing_id": filing["id"],
            "hard_gates": sorted({r["rule_id"] for r in rows}),
        }
        return 1, (1 if rows else 0), "", evidence

    def earnings_missing(self, ctx) -> tuple[int, int, str, dict]:
        """EARNINGS_MISSING -- SOFT (a stale FUNDAMENTAL score).

        The pilot holds no FUNDAMENTAL score at all (``fundamental_snapshot`` is
        empty), and the rule never fires for an asset with no FUNDAMENTAL score.
        """
        return 1, 0, "", {"fundamental_scores": self.n_fundamental_scores}

    # -- the price vetoes ------------------------------------------------
    def _obs(self, ctx) -> tuple[dict | None, str]:
        obs = ctx["obs"]
        if obs is None:
            return None, "no price observation on or before the cycle date"
        age = (d(ctx["date"]) - d(obs["obs_date"])).days
        if age > STALE_OBSERVATION_DAYS:
            return (
                None,
                f"price observation {obs['obs_date']} is {age} days old (> 7): not evaluated",
            )
        return obs, ""

    def break_trend_200(self, ctx) -> tuple[int, int, str, dict]:
        """BREAK_TREND_200 -- SOFT, flag-only (T-070)."""
        obs, reason = self._obs(ctx)
        if obs is None:
            return 0, 0, reason, {}
        close, sma = obs["close"], obs["sma_200"]
        if close is None or sma is None:
            return 0, 0, "close or sma_200 unavailable", {"obs_date": obs["obs_date"]}
        evidence = {
            "obs_date": obs["obs_date"],
            "close": close,
            "sma_200": sma,
            "threshold": TREND_BREAK_FACTOR,
        }
        hit = 1 if close < TREND_BREAK_FACTOR * sma else 0
        return 1, hit, "", evidence

    def price_crash(self, ctx) -> tuple[int, int, str, dict]:
        """PRICE_CRASH -- SOFT (T-070 names the rule: a 90-day drawdown < -35%)."""
        obs, reason = self._obs(ctx)
        if obs is None:
            return 0, 0, reason, {}
        dd = obs["max_drawdown_90d"]
        if dd is None:
            return 0, 0, "max_drawdown_90d unavailable", {"obs_date": obs["obs_date"]}
        evidence = {
            "obs_date": obs["obs_date"],
            "max_drawdown_90d": dd,
            "threshold": PRICE_CRASH_THRESHOLD,
        }
        hit = 1 if dd < PRICE_CRASH_THRESHOLD else 0
        return 1, hit, "", evidence

    def volatility_shock(self, ctx) -> tuple[int, int, str, dict]:
        """VOLATILITY_SHOCK -- HARD-temporal (T-070, sector-relative)."""
        obs, reason = self._obs(ctx)
        if obs is None:
            return 0, 0, reason, {}
        v5, v60 = obs["vol_5d"], obs["vol_60d_base"]
        if v5 is None or v60 is None or v60 == 0:
            return 0, 0, "vol_5d or vol_60d_base unavailable", {"obs_date": obs["obs_date"]}
        ratio = v5 / v60
        group = ctx["vol_group"].get(ctx["sector"])
        if ctx["sector"] is None or group is None or len(group) < SECTOR_MIN_NAMES:
            group = ctx["vol_all"]
            evidence = {"obs_date": obs["obs_date"], "ratio": ratio, "group": "cross-section"}
        else:
            evidence = {
                "obs_date": obs["obs_date"],
                "ratio": ratio,
                "group": "sector",
                "group_size": len(group),
            }
        if group:
            median = statistics.median(group)
            evidence["sector_median_ratio"] = median
            evidence["threshold"] = VOL_SHOCK_THRESHOLD
            evidence["sector_threshold"] = VOL_SHOCK_THRESHOLD * median
            leg1 = ratio > VOL_SHOCK_THRESHOLD
            leg2 = ratio > VOL_SHOCK_THRESHOLD * median
            evidence["legs"] = {"absolute": leg1, "sector_relative": leg2}
            hit = 1 if (leg1 and leg2) else 0
        else:
            evidence["legs"] = {"absolute": None, "sector_relative": None}
            hit = 0
        return 1, hit, "", evidence

    def crash_z_score(self, ctx) -> tuple[int, int, str, dict]:
        """CRASH_Z_SCORE -- HARD-temporal (T-070, sector-relative)."""
        obs, reason = self._obs(ctx)
        if obs is None:
            return 0, 0, reason, {}
        ret, mu, v60 = obs["ret_5d"], obs["mu_60d_base"], obs["vol_60d_base"]
        if ret is None or mu is None or v60 is None or v60 == 0:
            return (
                0,
                0,
                "ret_5d, mu_60d_base or vol_60d_base unavailable",
                {"obs_date": obs["obs_date"]},
            )
        denom = v60 * math.sqrt(5)
        z = (ret - 5 * mu) / denom
        group = ctx["crash_group"].get(ctx["sector"])
        if ctx["sector"] is None or group is None or len(group) < SECTOR_MIN_NAMES:
            group = ctx["crash_all"]
            evidence = {"obs_date": obs["obs_date"], "z": z, "group": "cross-section"}
        else:
            evidence = {
                "obs_date": obs["obs_date"],
                "z": z,
                "group": "sector",
                "group_size": len(group),
            }
        if group:
            median = statistics.median(group)
            evidence["sector_median_ret_5d"] = median
            evidence["threshold"] = CRASH_Z_THRESHOLD
            leg1 = z < CRASH_Z_THRESHOLD
            leg2 = (ret - median) / denom < CRASH_Z_THRESHOLD
            evidence["legs"] = {"absolute": leg1, "sector_relative": leg2}
            hit = 1 if (leg1 and leg2) else 0
        else:
            evidence["legs"] = {"absolute": None, "sector_relative": None}
            hit = 0
        return 1, hit, "", evidence


# --------------------------------------------------------------------------
# the gates (re-evaluated on every filing)
# --------------------------------------------------------------------------

IDENTITIES = {
    # gate: (statement, left concept, [right concepts], n_terms, base)
    "CON_01": (
        "balance_sheet",
        "us-gaap_Assets",
        ["us-gaap_LiabilitiesAndStockholdersEquity"],
        2,
        "assets",
    ),
    "CON_02": (
        "balance_sheet",
        "us-gaap_Assets",
        ["us-gaap_AssetsCurrent", "us-gaap_AssetsNoncurrent"],
        3,
        "assets",
    ),
    "CON_03": (
        "balance_sheet",
        "us-gaap_Liabilities",
        ["us-gaap_LiabilitiesCurrent", "us-gaap_LiabilitiesNoncurrent"],
        3,
        "assets",
    ),
    "CON_04": (
        "balance_sheet",
        "us-gaap_StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ["us-gaap_StockholdersEquity", "us-gaap_MinorityInterest"],
        3,
        "assets",
    ),
    "CON_08": (
        "cash_flow",
        "us-gaap_NetCashProvidedByUsedInOperatingActivities",
        [
            "us-gaap_NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            "us-gaap_CashProvidedByUsedInOperatingActivitiesDiscontinuedOperations",
        ],
        2,
        "change_in_cash",
    ),
    "CON_19": (
        "income_statement",
        "us-gaap_ProfitLoss",
        [
            "us-gaap_NetIncomeLoss",
            "us-gaap_NetIncomeLossAttributableToNoncontrollingInterest",
            "us-gaap_NetIncomeLossAttributableToRedeemableNoncontrollingInterest",
        ],
        3,
        "net_income",
    ),
}

# the optional legs of an identity: the identity still holds without them, and
# a leg that is filed is part of the right-hand side and of the term count.
OPTIONAL_LEGS = {
    "CON_08": ["us-gaap_CashProvidedByUsedInOperatingActivitiesDiscontinuedOperations"],
    "CON_19": ["us-gaap_NetIncomeLossAttributableToRedeemableNoncontrollingInterest"],
}

FACT_CONCEPTS = sorted(
    {c for stmt, left, rights, _n, base in IDENTITIES.values() for c in [left, *rights]}
    | {
        "us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
        "us-gaap_CashProvidedByUsedInOperatingActivitiesDiscontinuedOperations",
        "us-gaap_NetIncomeLossAttributableToRedeemableNoncontrollingInterest",
        "us-gaap_ProfitLoss",
        "us-gaap_NetIncomeLoss",
        "us-gaap_NetIncomeLossAttributableToNoncontrollingInterest",
        "us-gaap_CostOfGoodsAndServicesSold",
        "us-gaap_Revenues",
        "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap_Assets",
        "us-gaap_Liabilities",
    }
)


def infer_unit(values: list[float]) -> float:
    """L-10: the largest of USD 1 million and USD 1 thousand that divides every
    term exactly, else USD 1."""
    for unit in (1_000_000.0, 1_000.0):
        if all(abs(v / unit - round(v / unit)) < UNIT_TOLERANCE for v in values if v is not None):
            return unit
    return 1.0


def _shift_months(day: date, months: int) -> str:
    """the same day N months earlier, as an ISO date."""
    total = day.year * 12 + (day.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    last = [
        31,
        29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ][month - 1]
    return date(year, month, min(day.day, last)).isoformat()


def dq01(diff: float, base: float | None, values: list[float], n_terms: int) -> dict:
    unit = infer_unit(values)
    rounding = 0.5 * unit * n_terms
    threshold = rounding if base is None else max(rounding, 0.005 * abs(base))
    level = "block" if diff > threshold else "warn"
    return {
        "unit": unit,
        "rounding": rounding,
        "threshold": threshold,
        "level": level,
        "base": base,
        "base_missing": base is None,
    }


def split_period_key(key: str) -> tuple[str, str | None]:
    if key.endswith(")") and "(" in key:
        head, tag = key[:-1].split("(", 1)
        return head.strip(), tag.strip()
    return key, None


class GateEvaluator:
    """re-evaluates the Ring-1 gates on every filing and compares them with
    the stored ``data_quality_issue`` rows."""

    def __init__(self, con, metrics):
        self.con = con
        self.metrics = metrics
        self.facts: dict[int, dict[str, dict[str, float]]] = {}
        self._load_facts()

    def _load_facts(self) -> None:
        marks = ",".join("?" * len(FACT_CONCEPTS))
        # the placeholders are a constant length; the concepts are bound values
        rows = self.con.execute(
            f"SELECT filing_id, statement, concept, period_key, value, preferred_sign "  # noqa: S608
            f"FROM financial_facts WHERE concept IN ({marks}) AND value IS NOT NULL",
            tuple(FACT_CONCEPTS),
        ).fetchall()
        for fid, stmt, concept, pkey, value, sign in rows:
            # ID-11: the value used carries the sign filed in the instance
            signed = -value if (sign is not None and sign < 0) else value
            self.facts.setdefault(fid, {}).setdefault(stmt, {})[(concept, pkey)] = signed

    def _stmt(self, fid: int, statement: str) -> dict:
        return self.facts.get(fid, {}).get(statement, {})

    def own_column(self, fid: int, form: str, period_end: str, statement: str) -> str | None:
        """the filing's own period column for the statement."""
        keys = {k for (_c, k) in self._stmt(fid, statement)}
        if statement == "balance_sheet":
            return period_end if period_end in keys else None
        want = "FY" if form == "10-K" else None
        cand = sorted(k for k in keys if split_period_key(k)[0] == period_end)
        if not cand:
            return None
        if want:
            for k in cand:
                if split_period_key(k)[1] == want:
                    return k
            return None
        # a 10-Q: the own quarter (Q1/Q2/Q3/Q4) for the income statement, the
        # own year-to-date for the cash-flow statement
        tags = ("Q1", "Q2", "Q3", "Q4") if statement == "income_statement" else ("YTD",)
        for tag in tags:
            for k in cand:
                if split_period_key(k)[1] == tag:
                    return k
        return None

    def base_value(self, fid: int, statement: str, column: str, base_kind: str):
        stmt = self._stmt(fid, statement)
        if base_kind == "assets":
            return stmt.get(("us-gaap_Assets", column))
        if base_kind == "net_income":
            val = stmt.get(("us-gaap_NetIncomeLoss", column))
            if val is None:
                val = stmt.get(("us-gaap_ProfitLoss", column))
            return val
        if base_kind == "change_in_cash":
            for concept in (
                "us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
            ):
                val = stmt.get((concept, column))
                if val is not None:
                    return val
            return None
        return None

    def check_identity(self, fid: int, form: str, period_end: str, gate: str) -> bool | None:
        """True when the identity's condition holds (the gate fires)."""
        if gate == "CON_06":
            return self._check_con06(fid, "income_statement")
        statement, left_c, right_cs, _n_terms, base_kind = IDENTITIES[gate]
        stmt = self._stmt(fid, statement)
        column = self.own_column(fid, form, period_end, statement)
        if column is None:
            return None
        left = stmt.get((left_c, column))
        if left is None:
            return None
        rights = []
        optional = OPTIONAL_LEGS.get(gate, [])
        for c in right_cs:
            v = stmt.get((c, column))
            if v is None:
                if c in optional:
                    continue  # an optional leg: the identity holds without it
                return None
            rights.append(v)
        right = sum(rights)
        diff = abs(left - right)
        values = [left, *rights]
        base = self.base_value(fid, statement, column, base_kind)
        res = dq01(diff, base, values, len(values))
        return diff > res["rounding"]

    def _check_con06(  # noqa: C901, PLR0912
        self, fid: int, statement: str
    ) -> bool | None:
        """CON-06: the contiguous sub-periods sum to the aggregated period.

        Checked per durational concept, on every aggregated (YTD) column whose
        constituent quarters are all filed in the same filing.  The quarters are
        the k fiscal quarters of the same fiscal year that end at the aggregated
        column's own period end (k is the fiscal quarter the column ends in);
        a 52/53-week year is matched with a ten-day tolerance.
        """
        stmt = self._stmt(fid, statement)
        by_concept: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
        for (concept, pkey), value in stmt.items():
            head, tag = split_period_key(pkey)
            if tag:
                by_concept[concept][head][tag] = value
        fired = False
        evaluated = False
        for _concept, by_date in by_concept.items():
            for head, tags in by_date.items():
                if "YTD" not in tags:
                    continue
                end = date.fromisoformat(head)
                k = 0
                for qdate, qtags in by_date.items():
                    if qdate != head:
                        continue
                    for qtag in qtags:
                        if qtag in ("Q1", "Q2", "Q3", "Q4"):
                            k = int(qtag[1])
                if not k:
                    continue
                parts: list[float] = []
                complete = True
                for back in range(0, 3 * k, 3):
                    target = date.fromisoformat(_shift_months(end, -back))
                    found = None
                    for qdate, qtags in by_date.items():
                        if abs((date.fromisoformat(qdate) - target).days) > QUARTER_DAY_TOLERANCE:
                            continue
                        for qtag in ("Q1", "Q2", "Q3", "Q4"):
                            if qtag in qtags:
                                found = qtags[qtag]
                                break
                        break
                    if found is None:
                        complete = False
                        break
                    parts.append(found)
                if not complete or not parts:
                    continue
                evaluated = True
                agg = tags["YTD"]
                diff = abs(agg - sum(parts))
                values = [*parts, agg]
                base = self.base_value(fid, statement, f"{head} (YTD)", "net_income")
                res = dq01(diff, base, values, len(values))
                if diff > res["rounding"]:
                    fired = True
        if not evaluated:
            return None
        return fired

    def evaluate(
        self, fid: int, form: str, period_end: str, metrics_row: dict
    ) -> dict[str, bool | None]:
        out: dict[str, bool | None] = {}
        for gate in ("CON_01", "CON_02", "CON_03", "CON_04", "CON_06", "CON_08", "CON_19"):
            out[gate] = self.check_identity(fid, form, period_end, gate)
        # DQ_NEG_EQUITY: the filing's parent equity <= 0
        lev = self.metrics.get(fid, {}).get(("leverage", "debt_to_equity"))
        equity = None
        if lev and lev["inputs_json"]:
            try:
                equity = json.loads(lev["inputs_json"]).get("equity")
            except (ValueError, TypeError):
                equity = None
        out["DQ_NEG_EQUITY"] = None if equity is None else (equity <= 0)
        # Annex A screens: the gate fires when the value is OUTSIDE the
        # plausible range the screen states.
        nm = self._m(fid, "profitability", "net_margin")
        out["DQ_MARGIN"] = None if nm is None else (abs(nm) > DQ_MARGIN_LIMIT)
        out["DQ_MARGIN_REVIEW"] = (
            None if nm is None else (DQ_MARGIN_REVIEW_FLOOR < abs(nm) <= DQ_MARGIN_LIMIT)
        )
        ocf = self._m(fid, "cashflow", "operating_cash_flow_margin")
        out["DQ_OCF_MARGIN"] = None if ocf is None else (abs(ocf) > DQ_OCF_MARGIN_LIMIT)
        fy = self._m(fid, "valuation", "free_cash_flow_yield")
        out["DQ_FCF_YIELD"] = None if fy is None else (abs(fy) > DQ_FCF_YIELD_LIMIT)
        # DQ_REVENUE_POS: revenue > 0 whenever net income is reported.  The
        # revenue and the net income are the ones the metrics resolved (the
        # group's shared inputs); the gate fires when the revenue could not be
        # resolved (null) or is not positive.
        prof_inputs = self._inputs(fid, "profitability", "net_margin")
        revenue = prof_inputs.get("revenue")
        net_income = prof_inputs.get("net_income")
        if net_income is None:
            out["DQ_REVENUE_POS"] = None
        else:
            out["DQ_REVENUE_POS"] = revenue is None or revenue <= 0
        # DQ_MCAP_SCALE: market_capitalization / total_assets in [0.001, 100]
        mcap = self._m(fid, "valuation", "market_capitalization")
        lev_inputs = self._inputs(fid, "leverage", "debt_to_equity")
        total_assets = lev_inputs.get("total_assets")
        if mcap is None or not total_assets:
            out["DQ_MCAP_SCALE"] = None
        else:
            low, high = DQ_MCAP_SCALE_RANGE
            out["DQ_MCAP_SCALE"] = not (low <= (mcap / total_assets) <= high)
        return out

    def _own_revenue_and_net_income(self, fid: int, form: str, period_end: str):
        stmt = self._stmt(fid, "income_statement")
        column = self.own_column(fid, form, period_end, "income_statement")
        if column is None:
            return None, None
        net_income = stmt.get(("us-gaap_NetIncomeLoss", column))
        revenue = stmt.get(("us-gaap_Revenues", column))
        if revenue is None:
            revenue = stmt.get(
                ("us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax", column)
            )
        return revenue, net_income

    def _m(self, fid: int, group: str, name: str):
        row = self.metrics.get(fid, {}).get((group, name))
        return None if row is None else row["value"]

    def _inputs(self, fid: int, group: str, name: str) -> dict:
        row = self.metrics.get(fid, {}).get((group, name))
        if not row or not row["inputs_json"]:
            return {}
        try:
            return json.loads(row["inputs_json"])
        except (ValueError, TypeError):
            return {}


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main() -> int:  # noqa: C901, PLR0912, PLR0915
    log("== V4: the rules recomputed independently ==")
    dates = load_dates()
    log(f"dates ({len(dates)}): {dates[0]} .. {dates[-1]}")

    con = sqlite3.connect(f"file:{PILOT_DB}?mode=ro", uri=True)
    con.row_factory = None
    assets = load_assets(con)
    filings = load_filings(con)
    filings_by_asset: dict[int, list[dict]] = defaultdict(list)
    for f in filings:
        filings_by_asset[f["asset_id"]].append(f)
    log(f"assets: {len(assets)}; filings: {len(filings)}")
    metrics = load_metrics(con)
    log(f"metric rows read: {sum(len(v) for v in metrics.values())}")
    dq = load_dq(con)
    profile = load_profile()
    gics = load_gics()
    observations = load_observations(con, sorted(assets), dates)
    log("price observations loaded")

    ev = Evaluator(con, assets, filings_by_asset, metrics, dq, profile, gics, observations)

    rows: list[dict] = []
    boundary: list[dict] = []
    counts: dict[tuple[str, str], dict] = defaultdict(lambda: defaultdict(int))
    per_sector: dict[tuple[str, str, str], dict] = defaultdict(lambda: defaultdict(int))
    reasons: dict[tuple[str, str], dict] = defaultdict(lambda: defaultdict(int))
    sector_coverage: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    alt: dict[str, dict] = defaultdict(lambda: defaultdict(int))

    for day in dates:
        ctxs = []
        for aid in sorted(assets):
            asset = assets[aid]
            prof = profile_as_of(profile, asset["cik"], day)
            sector = sector_as_of(gics, asset["cik"], day)
            filing = latest_filing(filings_by_asset, aid, day)
            tenk = latest_10k(filings_by_asset, aid, day)
            ctxs.append(
                {
                    "date": day,
                    "asset_id": aid,
                    "ticker": asset["ticker"],
                    "cik": asset["cik"],
                    "profile": prof,
                    "sector": sector,
                    "filing": filing,
                    "tenk": tenk,
                    "obs": observations[day].get(aid),
                    "vol_ratio": {},
                    "vol_group": defaultdict(list),
                    "vol_all": [],
                    "crash_ret": {},
                    "crash_group": defaultdict(list),
                    "crash_all": [],
                }
            )
        sector_of = {c["asset_id"]: c["sector"] for c in ctxs}
        # the sector groups need the evaluated population first: a first pass
        # computes the inputs, a second applies the sector-relative legs.
        # The groups are per DATE (the cross-section of that day), not per asset.
        vol_ratio: dict[int, float] = {}
        crash_ret: dict[int, float] = {}
        for ctx in ctxs:
            obs = ctx["obs"]
            if obs is None:
                continue
            age = (d(day) - d(obs["obs_date"])).days
            if age > STALE_OBSERVATION_DAYS:
                continue
            v5, v60 = obs["vol_5d"], obs["vol_60d_base"]
            if v5 is not None and v60 not in (None, 0):
                vol_ratio[ctx["asset_id"]] = v5 / v60
            ret, mu = obs["ret_5d"], obs["mu_60d_base"]
            if ret is not None and mu is not None and v60 not in (None, 0):
                crash_ret[ctx["asset_id"]] = ret
        vol_all = list(vol_ratio.values())
        crash_all = list(crash_ret.values())
        vol_group: dict[str | None, list[float]] = defaultdict(list)
        crash_group: dict[str | None, list[float]] = defaultdict(list)
        for aid, ratio in vol_ratio.items():
            vol_group[sector_of[aid]].append(ratio)
        for aid, ret in crash_ret.items():
            crash_group[sector_of[aid]].append(ret)
        for ctx in ctxs:
            ctx["vol_ratio"] = vol_ratio
            ctx["crash_ret"] = crash_ret
            ctx["vol_group"] = vol_group
            ctx["vol_all"] = vol_all
            ctx["crash_group"] = crash_group
            ctx["crash_all"] = crash_all

        for ctx in ctxs:
            sector = ctx["sector"] or "NO_SECTOR"
            sector_coverage[day][sector] += 1
            for rule, rule_severity in RULE_SEVERITY.items():
                fn = getattr(ev, _RULE_METHOD[rule])
                evaluated, hit, reason, evidence = fn(ctx)
                if evaluated == 0 and reason:
                    evidence = dict(evidence)
                    evidence["reason"] = reason
                severity = (evidence.get("severity") or rule_severity) if hit else ""
                rows.append(
                    {
                        "date": day,
                        "rule_id": rule,
                        "asset_id": ctx["asset_id"],
                        "ticker": ctx["ticker"],
                        "evaluated": evaluated,
                        "hit": hit,
                        "severity": severity,
                        "inputs_json": json_compact(evidence),
                    }
                )
                key = (rule, day)
                counts[key]["evaluated"] += evaluated
                counts[key]["hit"] += hit
                per_sector[(rule, day, sector)]["evaluated"] += evaluated
                per_sector[(rule, day, sector)]["hit"] += hit
                if evaluated == 0 and reason:
                    reasons[(rule, day)][reason] += 1
                if rule == "LEVERAGE_EXTREME":
                    a = evidence.get("soft_review_reading_a_t116")
                    b = evidence.get("soft_review_reading_b_fr006")
                    if a is not None and b is not None and a != b:
                        alt["LEVERAGE_EXTREME"][day] += 1
                if rule == "LIQUIDITY_DISTRESS" and evidence.get("alt_evaluated"):
                    alt["LIQUIDITY_DISTRESS"][day] += 1
                b = _boundary_case(rule, evidence)
                if b is not None:
                    boundary.append({"date": day, "rule_id": rule, "ticker": ctx["ticker"], **b})
        log(f"  {day}: {len(ctxs)} assets evaluated")

    os.makedirs(OUT_DIR, exist_ok=True)
    out_csv = os.path.join(OUT_DIR, "v4_independent.csv")
    with open(out_csv, "w", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "date",
                "rule_id",
                "asset_id",
                "ticker",
                "evaluated",
                "hit",
                "severity",
                "inputs_json",
            ],
        )
        w.writeheader()
        for r in rows:
            w.writerow(r)
    log(f"wrote {out_csv}: {len(rows)} rows")

    # ---- the gates -----------------------------------------------------
    log("== the gates, re-evaluated on every filing ==")
    gates = GateEvaluator(con, metrics)
    stored: dict[int, set[str]] = defaultdict(set)
    for fid, rule in con.execute(
        "SELECT filing_id, rule_id FROM data_quality_issue WHERE gate_version = ?",
        (DQ_GATE_VERSION,),
    ):
        stored[fid].add(rule)
    gate_rows = []
    agree_count: dict[str, list[int]] = defaultdict(
        lambda: [0, 0, 0, 0]
    )  # agree, disagree, mine, stored
    for f in filings:
        res = gates.evaluate(f["id"], f["form"], f["period_end"], {})
        for gate, mine in res.items():
            st = gate in stored.get(f["id"], ())
            if mine is None:
                if not st:
                    continue
                gate_rows.append(
                    {
                        "filing_id": f["id"],
                        "gate": gate,
                        "yours": "",
                        "stored": 1,
                        "agree": 0,
                    }
                )
                c = agree_count[gate]
                c[1] += 1
                continue
            agree = 1 if bool(mine) == st else 0
            gate_rows.append(
                {
                    "filing_id": f["id"],
                    "gate": gate,
                    "yours": int(bool(mine)),
                    "stored": int(st),
                    "agree": agree,
                }
            )
            c = agree_count[gate]
            c[0] += agree
            c[1] += 1 - agree
            c[2] += 1 if mine else 0
            c[3] += 1 if st else 0
    gates_csv = os.path.join(OUT_DIR, "v4_gates.csv")
    with open(gates_csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["filing_id", "gate", "yours", "stored", "agree"])
        w.writeheader()
        for r in gate_rows:
            w.writerow(r)
    log(f"wrote {gates_csv}: {len(gate_rows)} rows")

    write_summary(
        counts, per_sector, reasons, boundary, agree_count, sector_coverage, dates, rows, alt
    )
    log("wrote " + os.path.join(OUT_DIR, "v4_summary.md"))
    return 0


_RULE_METHOD = {
    "LEVERAGE_EXTREME": "leverage_extreme",
    "NEGATIVE_FCF": "negative_fcf",
    "UTILITY_CASH_COVERAGE": "utility_cash_coverage",
    "LIQUIDITY_DISTRESS": "liquidity_distress",
    "DATA_QUALITY": "data_quality",
    "EARNINGS_MISSING": "earnings_missing",
    "BREAK_TREND_200": "break_trend_200",
    "PRICE_CRASH": "price_crash",
    "VOLATILITY_SHOCK": "volatility_shock",
    "CRASH_Z_SCORE": "crash_z_score",
}


def _boundary_case(rule: str, ev: dict):  # noqa: C901, PLR0911, PLR0912
    """a case within 5% of the rule's threshold."""
    if not ev:
        return None

    def near(value, threshold):
        if value is None:
            return None
        span = abs(threshold) if threshold else 1.0
        return abs(value - threshold) <= 0.05 * span

    if rule == "LEVERAGE_EXTREME":
        for key, thr in (
            ("debt_to_equity", LEV_DTE_THRESHOLD),
            ("net_debt_to_ebitda", LEV_ND_EBITDA_THRESHOLD),
            ("interest_coverage", LEV_INTEREST_COVERAGE_THRESHOLD),
        ):
            if near(ev.get(key), thr):
                return {"metric": key, "value": ev.get(key), "threshold": thr}
    elif rule == "LIQUIDITY_DISTRESS":
        for key, thr in (
            ("current_ratio", LIQ_CURRENT_RATIO_THRESHOLD),
            ("interest_coverage", LIQ_INTEREST_COVERAGE_THRESHOLD),
            ("operating_cash_flow_margin", 0.0),
        ):
            if near(ev.get(key), thr):
                return {"metric": key, "value": ev.get(key), "threshold": thr}
    elif rule == "NEGATIVE_FCF":
        if near(ev.get("free_cash_flow_margin"), 0.0):
            return {
                "metric": "free_cash_flow_margin",
                "value": ev.get("free_cash_flow_margin"),
                "threshold": 0.0,
            }
    elif rule == "UTILITY_CASH_COVERAGE":
        for key in ("average_ambiguous_in", "average_ambiguous_as_wc"):
            if near(ev.get(key), UTIL_CASH_COVERAGE_THRESHOLD):
                return {
                    "metric": key,
                    "value": ev.get(key),
                    "threshold": UTIL_CASH_COVERAGE_THRESHOLD,
                }
    elif rule == "BREAK_TREND_200":
        if ev.get("sma_200") and near(ev.get("close") / ev["sma_200"], TREND_BREAK_FACTOR):
            return {
                "metric": "close/sma_200",
                "value": ev["close"] / ev["sma_200"],
                "threshold": TREND_BREAK_FACTOR,
            }
    elif rule == "PRICE_CRASH":
        if near(ev.get("max_drawdown_90d"), PRICE_CRASH_THRESHOLD):
            return {
                "metric": "max_drawdown_90d",
                "value": ev.get("max_drawdown_90d"),
                "threshold": PRICE_CRASH_THRESHOLD,
            }
    elif rule == "VOLATILITY_SHOCK" and near(ev.get("ratio"), VOL_SHOCK_THRESHOLD):
        return {"metric": "ratio", "value": ev.get("ratio"), "threshold": VOL_SHOCK_THRESHOLD}
    elif rule == "CRASH_Z_SCORE" and near(ev.get("z"), CRASH_Z_THRESHOLD):
        return {"metric": "z", "value": ev.get("z"), "threshold": CRASH_Z_THRESHOLD}
    return None


def write_summary(  # noqa: PLR0913, PLR0915, PLR0917
    counts, per_sector, reasons, boundary, agree_count, sector_coverage, dates, rows, alt
):
    path = os.path.join(OUT_DIR, "v4_summary.md")
    with open(path, "w") as fh:
        w = fh.write
        w("# V4 -- the rules recomputed independently\n\n")
        w(
            "T-143 part A, the independent session.  Every rule's **condition** is recomputed "
            "here from the pilot database (read-only) and the documented rule text; no `cycle` "
            "code and no `veto` row is read (part A ran no cycle, so there is none).\n\n"
        )
        w("## The rule list\n\n")
        w("| rule_id | severity | source | condition (as recomputed) |\n|---|---|---|---|\n")
        w(
            "| `LEVERAGE_EXTREME` | HARD | FR-006, T-071 2a D2/D-04, T-116 | "
            "`debt_to_equity > 3.0`; or, for equity <= 0 and a non-financial-firm profile, "
            "HARD when EBITDA <= 0 with net debt > 0, or net debt/EBITDA > 5.0 (EBITDA > 0), "
            "or interest coverage < 1.5 (read from the `DQ_NEG_EQUITY` record); no usable "
            "credit input -> SOFT review |\n"
        )
        w(
            "| `NEGATIVE_FCF` | HARD | FR-006, D-13, D-14 | `free_cash_flow_margin < 0`; the "
            "regulated-utility overlay and the APP-00 REIT overlay on the cycle date are "
            "evaluated and never hit |\n"
        )
        w(
            "| `UTILITY_CASH_COVERAGE` | HARD | FR-006, D-13 | a regulated utility whose "
            "three-year average operating cash flow before working-capital changes over total "
            "debt is below 5% on **both** bounds, read from the newest usable 10-K's rule "
            "input; a straddling pair or a missing bound -> evaluated, never hit |\n"
        )
        w(
            "| `LIQUIDITY_DISTRESS` | SOFT | FR-006, T-070, D-5 | `current_ratio < 1.0` AND "
            "(`interest_coverage < 1.5` OR `operating_cash_flow_margin < 0`); a financial firm "
            "or the regulated-utility overlay is evaluated and never hit |\n"
        )
        w(
            "| `DATA_QUALITY` | HARD | FR-006, Annex A | the latest usable filing carries a "
            "HARD Ring-1 gate row (`DQ_FCF_YIELD`, `DQ_MARGIN`, `DQ_MCAP_SCALE`, "
            "`DQ_OCF_MARGIN`, `DQ_REVENUE_POS`) under gate version `dq-v4` |\n"
        )
        w(
            "| `EARNINGS_MISSING` | SOFT | `model_fixes.md` (T-156's veto table) | a stale "
            "FUNDAMENTAL score; the pilot holds no FUNDAMENTAL score, so it never fires |\n"
        )
        w(
            "| `BREAK_TREND_200` | SOFT | FR-006, T-070 | `close < 0.95 * sma_200` (flag-only, "
            "0 penalty points) |\n"
        )
        w("| `PRICE_CRASH` | SOFT | `model_fixes.md` T-070 | a 90-day drawdown < -35% |\n")
        w(
            "| `VOLATILITY_SHOCK` | HARD-temporal | FR-006, T-070 | `vol_5d/vol_60d_base > 2.5` "
            "AND `ratio > 2.5 * the sector median ratio` (a sector with fewer than 5 evaluated "
            "names, or no sector, uses the whole cross-section) |\n"
        )
        w(
            "| `CRASH_Z_SCORE` | HARD-temporal | FR-006, T-070 | "
            "`(ret_5d - 5*mu_60d_base)/(vol_60d_base*sqrt(5)) < -2.5` AND "
            "`(ret_5d - the sector median ret_5d)/(vol_60d_base*sqrt(5)) < -2.5` |\n"
        )
        w(
            "\n`DQ_NEG_EQUITY` is **a record, never a veto** (T-071 2a D1): it is re-evaluated in "
            "`v4_gates.csv`, not in `v4_independent.csv`.\n\n"
        )
        w("## Per rule and date: evaluated / hit\n\n")
        for rule, rule_severity in RULE_SEVERITY.items():
            w(f"### {rule} ({rule_severity})\n\n")
            w(
                "| date | evaluated | hit | by sector (evaluated / hit) | not evaluated, by reason |\n"
            )
            w("|---|---|---|---|---|\n")
            for day in dates:
                c = counts[(rule, day)]
                sec_bits = []
                for (r, dd, sector), v in sorted(per_sector.items()):
                    if r == rule and dd == day:
                        sec_bits.append(f"{sector} {v['evaluated']}/{v['hit']}")
                rs = "; ".join(f"{k} x{v}" for k, v in sorted(reasons[(rule, day)].items()))
                w(f"| {day} | {c['evaluated']} | {c['hit']} | {', '.join(sec_bits)} | {rs} |\n")
            w("\n")
        w("## The gates, re-evaluated on every filing\n\n")
        w(
            "| gate | compared | agree | disagree | mine fired | stored fired |\n|---|---|---|---|---|---|\n"
        )
        for gate, (ag, dis, mine, st) in sorted(agree_count.items()):
            w(f"| {gate} | {ag + dis} | {ag} | {dis} | {mine} | {st} |\n")
        w(
            "\n`yours` is the gate's **condition** re-evaluated on the filing; `stored` is the "
            "presence of a `data_quality_issue` row under the current gate version `dq-v4`.  "
            "A gate the filing cannot support is not compared (`yours` empty); where the store "
            "holds a row and the gate could not be evaluated, the row is counted as a "
            "disagreement.\n"
        )
        w("## The sector on each date\n\n")
        w("| date | assets with a GICS sector | assets without one |\n|---|---|---|\n")
        for day in dates:
            with_sector = sum(v for k, v in sector_coverage[day].items() if k != "NO_SECTOR")
            without = sector_coverage[day].get("NO_SECTOR", 0)
            w(f"| {day} | {with_sector} | {without} |\n")
        w(
            "\nThe sector is the GICS sector of the CIK's membership line in "
            "`universe_history_gics.db` on the cycle date (`valid_from <= C <= valid_to`, the "
            "last membership day inclusive).  An asset with no sector uses the whole "
            "cross-section for the price rules' sector-relative leg (FR-006).\n"
        )
        w("## Decisions (each one pending reviewer B)\n\n")
        w(
            "| # | the choice | what was taken | the other reading | rows that differ |\n"
            "|---|---|---|---|---|\n"
        )
        w(
            "| D-1 | `LEVERAGE_EXTREME`'s SOFT review | T-116's own words: both corroborating "
            "metrics (`net_debt_to_ebitda` and `interest_coverage`) are `None` | FR-006's "
            'phrase "no usable credit input at all": all four credit inputs (EBITDA, net '
            "debt, net debt/EBITDA, interest coverage) are `None` | "
            + ", ".join(f"{d}: {n}" for d, n in sorted(alt["LEVERAGE_EXTREME"].items()))
            + " (the rows on which the two readings differ) |\n"
        )
        w(
            '| D-2 | `LIQUIDITY_DISTRESS`\'s "not evaluated" | the first condition '
            "(`current_ratio < 1.0`) decides: the rule is evaluated and never hit when "
            "`current_ratio >= 1.0`, and not evaluated only when `current_ratio < 1.0` and "
            "neither `interest_coverage` nor `operating_cash_flow_margin` is available | T-070's "
            "wording read literally: neither leg available -> not evaluated, whatever the current "
            "ratio | "
            + ", ".join(f"{d}: {n}" for d, n in sorted(alt["LIQUIDITY_DISTRESS"].items()))
            + " (the rows on which the two readings differ; reading B reproduces T-156's measured "
            "`LIQUIDITY_DISTRESS 470/1` on this database exactly, reading A gives 467/1) |\n"
        )
        w(
            "| D-3 | the rule list | the 8 rules the brief names, plus `PRICE_CRASH` (a 90-day "
            "drawdown < -35%, SOFT, named in T-070) and `EARNINGS_MISSING` (a stale FUNDAMENTAL "
            "score, named in T-156's veto table) | `DQ_NEG_EQUITY` is a **record, never a veto** "
            "(T-071 2a D1), so it has no rows here; it is re-evaluated in `v4_gates.csv` | 16 x 591 "
            "rows for each rule that is or is not emitted |\n"
        )
        w(
            "| D-4 | the `severity` column | the rule's severity on a hit, empty otherwise | the "
            "rule's severity on every row | 94,560 rows |\n"
        )
        w(
            "| D-5 | an exemption | an exempt asset is **evaluated and never hit** (the D-13/D-5 "
            "pattern; the T-071 2a measurement counts 27 newly evaluated assets) | the exemption "
            "makes the rule not evaluated | every `NEGATIVE_FCF`, `LIQUIDITY_DISTRESS` and "
            "`UTILITY_CASH_COVERAGE` row of an exempt asset |\n"
        )
        w(
            "| D-6 | `UTILITY_CASH_COVERAGE`'s scope | a regulated utility reads the newest usable "
            "10-K's rule input; a non-utility is not evaluated | - | 562 rows per date |\n"
        )
        w(
            "| D-7 | the price rules' sector group | the names the rule evaluates that day, in the "
            "name's GICS sector; fewer than 5 names, or no sector, uses the whole cross-section | "
            "- | the rows of the names in a small sector |\n"
        )
        w(
            "| D-8 | the price rules' staleness | an observation older than 7 **calendar** days is "
            "not evaluated (T-070) | - | the stale rows |\n"
        )
        w(
            "| D-9 | the latest usable filing | the newest filing with `available_at <= C`, ordered "
            "by `period_end` then `available_at` (T-149) | the newest by `available_at` only | "
            "the rows of an asset whose filings arrive out of order |\n"
        )
        w("\n")
        w("## Boundary cases (within 5% of a threshold)\n\n")
        w("| date | rule | ticker | metric | value | threshold |\n|---|---|---|---|---|---|\n")
        for b in boundary:
            w(
                f"| {b['date']} | {b['rule_id']} | {b['ticker']} | {b['metric']} | "
                f"{b['value']} | {b['threshold']} |\n"
            )
        w("\n")
        w(f"total rows in `v4_independent.csv`: {len(rows)}\n")


if __name__ == "__main__":
    sys.exit(main())
