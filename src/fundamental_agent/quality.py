"""Ring-1 deterministic data-quality gates (``DQ_*``, T-065).

Seven threshold checks over a filing's *stored* ``fundamental_metrics``, zero LLM cost. A hit
is recorded in ``data_quality_issue`` (one row per gated metric) and does one or both of:

- **quarantine** the metric: consumers (``cycle``) read it as NULL, because the stored value
  is implausible enough to be a data error rather than a business fact;
- **HARD**: ``cycle`` raises a ``DATA_QUALITY`` veto on the asset. A SOFT row that is not
  quarantined is only a review item.

The gates are a permanent backstop, not a fix: F1/F2/F4 (``docs/model_fixes.md``) corrected
the known causes, and these catch whatever unknown cause produces the same symptoms next.
Thresholds are ``PLAN.md`` Work item 7's calibrated table; the rationale and the verification
against production data are in ``docs/model_fixes.md``'s T-065 entry.

| Rule               | Trigger                                            | Severity  | Quarantines |
|--------------------|----------------------------------------------------|-----------|-------------|
| ``DQ_FCF_YIELD``   | ``|free_cash_flow_yield| > 0.50``                   | HARD      | that yield  |
| ``DQ_MARGIN``      | ``|net_margin| > 5``                               | HARD      | net margin  |
| ``DQ_MARGIN_REVIEW``| ``|net_margin|`` in ``(1, 5]``                     | SOFT      | nothing     |
| ``DQ_OCF_MARGIN``  | ``|operating_cash_flow_margin| > 3``               | HARD      | OCF margin  |
| ``DQ_MCAP_SCALE``  | ``market_cap / total_assets`` outside ``[0.001, 100]`` | HARD  | market cap and every valuation metric built on it |
| ``DQ_NEG_EQUITY``  | ``equity <= 0``                                    | HARD if also ``debt_to_assets > 0.8`` or ``interest_coverage < 1.5``, else SOFT | D/E and ROE |
| ``DQ_REVENUE_POS`` | ``revenue <= 0`` or missing, with net income present | HARD    | the revenue-denominated ratios |

The gates read the stored rows (value + ``inputs_json``) of one metrics engine version at a
time, so every ``data_quality_issue`` row names the version it judged (T-090): a consumer that
reads ``metrics-v3`` is never quarantined by a verdict on ``metrics-v2``. Recording is
append-only under :data:`GATE_VERSION`; re-gating the same stored metrics is a no-op.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from portfolio_common.db import Database

from fundamental_agent.metrics.base import TTMFlow
from kg_schema import queries
from kg_schema.versions import DATA_QUALITY_GATE_VERSION, METRIC_GROUPS, MetricVersions

GATE_VERSION: Final[str] = DATA_QUALITY_GATE_VERSION

FCF_YIELD_MAX: Final[float] = 0.50
NET_MARGIN_HARD: Final[float] = 5.0
NET_MARGIN_REVIEW: Final[float] = 1.0
OCF_MARGIN_MAX: Final[float] = 3.0
MCAP_TO_ASSETS_MIN: Final[float] = 0.001
MCAP_TO_ASSETS_MAX: Final[float] = 100.0
NEG_EQUITY_DEBT_TO_ASSETS: Final[float] = 0.8
NEG_EQUITY_INTEREST_COVERAGE: Final[float] = 1.5

MetricKey = tuple[str, str]  # (metric_group, metric_name)

# Everything valuation derives from the market capitalisation (valuation.py).
_MCAP_DERIVED: Final[tuple[MetricKey, ...]] = (
    ("valuation", "market_capitalization"),
    ("valuation", "enterprise_value"),
    ("valuation", "free_cash_flow_yield"),
    ("valuation", "enterprise_fcf_yield"),
    ("valuation", "sbc_adjusted_fcf_yield"),
)
# Ratios with book equity in the denominator: meaningless when equity <= 0.
_EQUITY_DENOMINATED: Final[tuple[MetricKey, ...]] = (
    ("leverage", "debt_to_equity"),
    ("profitability", "return_on_equity"),
)
# Ratios with revenue in the denominator (or revenue as the growth base).
_REVENUE_DENOMINATED: Final[tuple[MetricKey, ...]] = (
    ("profitability", "gross_margin"),
    ("profitability", "operating_margin"),
    ("profitability", "net_margin"),
    ("cashflow", "operating_cash_flow_margin"),
    ("cashflow", "free_cash_flow_margin"),
    ("efficiency", "asset_turnover"),
    ("growth", "revenue_growth"),
)
# Where each raw statement input is read from, in order: the metric whose inputs carry it.
_INPUT_SOURCES: Final[dict[str, tuple[MetricKey, ...]]] = {
    "equity": (("leverage", "debt_to_equity"), ("profitability", "return_on_equity")),
    "total_assets": (("leverage", "debt_to_assets"), ("profitability", "return_on_assets")),
    "revenue": (("profitability", "net_margin"), ("cashflow", "operating_cash_flow_margin")),
    "net_income": (("profitability", "net_margin"), ("cashflow", "operating_cash_flow_margin")),
}


@dataclass(frozen=True)
class StoredMetric:
    value: float | None
    inputs: Mapping[str, Any] = field(default_factory=dict)


FilingMetrics = Mapping[MetricKey, StoredMetric]


@dataclass(frozen=True)
class Issue:
    rule_id: str
    severity: str  # 'HARD' | 'SOFT'
    quarantined: bool
    metric: MetricKey
    value: float | None
    evidence: Mapping[str, Any]


def _value(fm: FilingMetrics, key: MetricKey) -> float | None:
    stored = fm.get(key)
    return None if stored is None else stored.value


def _input(fm: FilingMetrics, name: str) -> float | None:
    """A raw statement figure (``equity``, ``revenue``, ...) from the inputs audit blob of the
    first metric that recorded it. Absent and NULL are the same: not reported."""
    for key in _INPUT_SOURCES[name]:
        stored = fm.get(key)
        if stored is not None and stored.inputs.get(name) is not None:
            return float(stored.inputs[name])
    return None


def _hits(  # noqa: PLR0913 - one issue per stored target metric
    fm: FilingMetrics,
    targets: Iterable[MetricKey],
    rule_id: str,
    severity: str,
    *,
    quarantined: bool,
    evidence: Mapping[str, Any],
) -> list[Issue]:
    """One issue per *target* metric the filing actually stored (value NULL or not)."""
    return [
        Issue(rule_id, severity, quarantined, key, fm[key].value, evidence)
        for key in targets
        if key in fm
    ]


def _abs_above(
    rule_id: str, key: MetricKey, limit: float
) -> Callable[[FilingMetrics], list[Issue]]:
    def gate(fm: FilingMetrics) -> list[Issue]:
        v = _value(fm, key)
        if v is None or abs(v) <= limit:
            return []
        return _hits(fm, [key], rule_id, "HARD", quarantined=True, evidence={"limit": limit})

    return gate


def _margin_review(fm: FilingMetrics) -> list[Issue]:
    key = ("profitability", "net_margin")
    v = _value(fm, key)
    if v is None or not NET_MARGIN_REVIEW < abs(v) <= NET_MARGIN_HARD:
        return []
    evidence = {"range": [NET_MARGIN_REVIEW, NET_MARGIN_HARD]}
    return _hits(fm, [key], "DQ_MARGIN_REVIEW", "SOFT", quarantined=False, evidence=evidence)


def _mcap_scale(fm: FilingMetrics) -> list[Issue]:
    mcap = _value(fm, ("valuation", "market_capitalization"))
    assets = _input(fm, "total_assets")
    if mcap is None or assets is None or assets <= 0:
        return []
    ratio = mcap / assets
    if MCAP_TO_ASSETS_MIN <= ratio <= MCAP_TO_ASSETS_MAX:
        return []
    evidence = {
        "market_capitalization": mcap,
        "total_assets": assets,
        "ratio": ratio,
        "range": [MCAP_TO_ASSETS_MIN, MCAP_TO_ASSETS_MAX],
    }
    return _hits(fm, _MCAP_DERIVED, "DQ_MCAP_SCALE", "HARD", quarantined=True, evidence=evidence)


def _neg_equity(fm: FilingMetrics) -> list[Issue]:
    equity = _input(fm, "equity")
    if equity is None or equity > 0:
        return []
    dta = _value(fm, ("leverage", "debt_to_assets"))
    cov = _value(fm, ("leverage", "interest_coverage"))
    distressed = (dta is not None and dta > NEG_EQUITY_DEBT_TO_ASSETS) or (
        cov is not None and cov < NEG_EQUITY_INTEREST_COVERAGE
    )
    evidence = {"equity": equity, "debt_to_assets": dta, "interest_coverage": cov}
    return _hits(
        fm,
        _EQUITY_DENOMINATED,
        "DQ_NEG_EQUITY",
        "HARD" if distressed else "SOFT",
        quarantined=True,
        evidence=evidence,
    )


def _revenue_pos(fm: FilingMetrics) -> list[Issue]:
    net_income = _input(fm, "net_income")
    revenue = _input(fm, "revenue")
    if net_income is None or (revenue is not None and revenue > 0):
        return []
    evidence = {"revenue": revenue, "net_income": net_income}
    return _hits(
        fm, _REVENUE_DENOMINATED, "DQ_REVENUE_POS", "HARD", quarantined=True, evidence=evidence
    )


# (rule_id, severity as catalogued, description, gate) -- in the order they are evaluated.
GATES: Final[tuple[tuple[str, str, str, Callable[[FilingMetrics], list[Issue]]], ...]] = (
    (
        "DQ_FCF_YIELD",
        "HARD",
        f"|free-cash-flow yield| > {FCF_YIELD_MAX}",
        _abs_above("DQ_FCF_YIELD", ("valuation", "free_cash_flow_yield"), FCF_YIELD_MAX),
    ),
    (
        "DQ_MARGIN",
        "HARD",
        f"|net margin| > {NET_MARGIN_HARD}",
        _abs_above("DQ_MARGIN", ("profitability", "net_margin"), NET_MARGIN_HARD),
    ),
    (
        "DQ_MARGIN_REVIEW",
        "SOFT",
        f"|net margin| in ({NET_MARGIN_REVIEW}, {NET_MARGIN_HARD}]",
        _margin_review,
    ),
    (
        "DQ_OCF_MARGIN",
        "HARD",
        f"|operating-cash-flow margin| > {OCF_MARGIN_MAX}",
        _abs_above("DQ_OCF_MARGIN", ("cashflow", "operating_cash_flow_margin"), OCF_MARGIN_MAX),
    ),
    (
        "DQ_MCAP_SCALE",
        "HARD",
        f"market cap / total assets outside [{MCAP_TO_ASSETS_MIN}, {MCAP_TO_ASSETS_MAX}]",
        _mcap_scale,
    ),
    (
        "DQ_NEG_EQUITY",
        "HARD/SOFT",
        "book equity <= 0: D/E and ROE quarantined; HARD only if debt/assets > "
        f"{NEG_EQUITY_DEBT_TO_ASSETS} or interest coverage < {NEG_EQUITY_INTEREST_COVERAGE}",
        _neg_equity,
    ),
    (
        "DQ_REVENUE_POS",
        "HARD",
        "revenue <= 0 or missing while net income is reported",
        _revenue_pos,
    ),
)
RULE_IDS: Final[tuple[str, ...]] = tuple(g[0] for g in GATES)


def evaluate(fm: FilingMetrics) -> list[Issue]:
    """Every gate over one filing's stored metrics (one engine version)."""
    return [issue for _rule, _sev, _desc, gate in GATES for issue in gate(fm)]


# -- persistence ---------------------------------------------------------------------------


@dataclass
class GateReport:
    filings: int = 0
    inserted: int = 0
    # rule_id -> distinct filings hit / rule_id -> of which HARD
    filings_by_rule: dict[str, int] = field(default_factory=dict)
    hard_by_rule: dict[str, int] = field(default_factory=dict)


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _load(
    conn: Database, versions: MetricVersions, filing_id: int | None
) -> dict[int, tuple[int, dict[MetricKey, StoredMetric]]]:
    """``filing_id -> (asset_id, metrics)`` for the resolved *versions* (every filing, or one)."""
    rows = conn.execute(
        """
        SELECT m.filing_id, f.asset_id, m.metric_group, m.metric_name, m.value, m.inputs_json
        FROM fundamental_metrics m
        JOIN sec_filings f ON f.id = m.filing_id
        WHERE (m.metric_group || '/' || m.engine_version) IN (SELECT value FROM json_each(?))
          AND (? IS NULL OR m.filing_id = ?)
        ORDER BY m.filing_id
        """,
        (versions.json_param(), filing_id, filing_id),
    ).fetchall()
    out: dict[int, tuple[int, dict[MetricKey, StoredMetric]]] = {}
    for r in rows:
        try:
            inputs = json.loads(r["inputs_json"] or "{}")
        except (TypeError, ValueError):
            inputs = {}
        fid = int(r["filing_id"])
        _aid, metrics = out.setdefault(fid, (int(r["asset_id"]), {}))
        metrics[(str(r["metric_group"]), str(r["metric_name"]))] = StoredMetric(
            r["value"], inputs if isinstance(inputs, dict) else {}
        )
    return out


def _count_issues(conn: Database) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM data_quality_issue").fetchone()[0])


def gate_version(
    conn: Database,
    engine_version: str,
    *,
    filing_id: int | None = None,
    run_id: int | None = None,
) -> GateReport:
    """Gate the metrics stored under *engine_version* -- every filing, or just *filing_id* --
    and record the hits. Returns what was hit (whether or not it was already recorded)."""
    versions = MetricVersions(dict.fromkeys(METRIC_GROUPS, engine_version))
    filings = _load(conn, versions, filing_id)
    now = _now()
    report = GateReport(filings=len(filings))
    rows: list[tuple[Any, ...]] = []
    for fid, (aid, metrics) in filings.items():
        issues = evaluate(metrics)
        for rule_id in {i.rule_id for i in issues}:
            report.filings_by_rule[rule_id] = report.filings_by_rule.get(rule_id, 0) + 1
        for rule_id in {i.rule_id for i in issues if i.severity == "HARD"}:
            report.hard_by_rule[rule_id] = report.hard_by_rule.get(rule_id, 0) + 1
        rows.extend(
            (
                fid,
                aid,
                i.metric[0],
                i.metric[1],
                engine_version,
                i.rule_id,
                i.severity,
                int(i.quarantined),
                i.value,
                json.dumps(dict(i.evidence), sort_keys=True),
                GATE_VERSION,
                now,
                run_id,
            )
            for i in issues
        )
    before = _count_issues(conn)
    conn.executemany(
        """
        INSERT OR IGNORE INTO data_quality_issue
            (filing_id, asset_id, metric_group, metric_name, metric_engine_version, rule_id,
             severity, quarantined, value, evidence_json, gate_version, created_at, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    report.inserted = _count_issues(conn) - before
    return report


def gate_all(
    conn: Database, *, engine_version: str | None = None, run_id: int | None = None
) -> dict[str, GateReport]:
    """The backfill: gate every stored filing under each metrics engine version (or just
    *engine_version*). Deterministic and LLM-free, so re-running it costs nothing."""
    present = sorted({v for vs in queries.metric_versions_present(conn).values() for v in vs})
    chosen = [v for v in present if engine_version is None or v == engine_version]
    return {v: gate_version(conn, v, run_id=run_id) for v in chosen}


# -- TTM cross-check (T-105 review) ------------------------------------------------------------

TTM_CROSSCHECK_RULE: Final[str] = "DQ_TTM_CROSSCHECK"
# Relative gap between the two TTM constructions above which a filing goes to review.
TTM_CROSSCHECK_TOLERANCE: Final[float] = 0.01


def record_ttm_crosscheck(  # noqa: PLR0913 - the filing's identity plus provenance
    conn: Database,
    filing_id: int,
    asset_id: int,
    flows: Mapping[str, TTMFlow],
    *,
    engine_version: str,
    run_id: int | None = None,
) -> int:
    """Where a flow's TTM was computable both ways -- the year-to-date identity (its value) and
    four recorded quarters (:attr:`TTMFlow.alt`) -- and they differ by more than
    :data:`TTM_CROSSCHECK_TOLERANCE`, record a SOFT, unquarantined review row with both.

    Neither side is treated as the truth, so nothing is quarantined or vetoed: the identity is
    kept because it uses the filing's own *restated* comparative column, while the quarters
    are as-filed -- after AT&T's 2022 WarnerMedia spin-off its Q1 2022 cogs were $10.351B as
    filed and $6.036B restated, and the four-quarter sum is the wrong side. A gap is still worth
    a look: it can equally mean a concept resolved differently in one filing. The row sits
    under the pseudo-group ``ttm`` (never a metric group, so no consumer reads it as a metric).
    Returns the rows inserted."""
    now = _now()
    rows = []
    for item, flow in sorted(flows.items()):
        if flow.method != "ytd" or flow.alt is None:
            continue
        scale = max(abs(flow.value), abs(flow.alt))
        if scale == 0 or abs(flow.value - flow.alt) <= TTM_CROSSCHECK_TOLERANCE * scale:
            continue
        evidence = {
            "identity": flow.value,
            "four_quarters": flow.alt,
            "relative_gap": abs(flow.value - flow.alt) / scale,
        }
        rows.append(
            (
                filing_id,
                asset_id,
                "ttm",
                item,
                engine_version,
                TTM_CROSSCHECK_RULE,
                "SOFT",
                0,
                flow.value,
                json.dumps(evidence, sort_keys=True),
                GATE_VERSION,
                now,
                run_id,
            )
        )
    before = _count_issues(conn)
    conn.executemany(
        """
        INSERT OR IGNORE INTO data_quality_issue
            (filing_id, asset_id, metric_group, metric_name, metric_engine_version, rule_id,
             severity, quarantined, value, evidence_json, gate_version, created_at, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return _count_issues(conn) - before
