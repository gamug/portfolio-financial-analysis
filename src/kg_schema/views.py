"""Read-contract VIEWs consumed by the integration repo's RDF projection.

Every view joins ``assets`` so ``ticker`` sits beside ``asset_id``, and freezes a
stable column order. The integration repo should read these, never the base tables,
so the physical schema here can evolve underneath it. Views are dropped and
recreated on every :func:`kg_schema.ensure` call -- they are cheap and always current.

Projection semantics
--------------------
``v_score_snapshot``      one row per (asset, score_type, event_time). ``event_time`` is
                          the filing period-end for FUNDAMENTAL, the cycle date for
                          TECHNICAL / QUANTITATIVE, and the article-day for SEMANTIC.
``v_universe_membership``  one row per membership stint; ``valid_to IS NULL`` = current.
``v_price_observation``    latest ``engine_version`` per (asset, obs_date); derived
                          price analytics only (raw OHLCV stays in ``price_daily``).
``v_sec_filing_section``   narrative filing text; one row per (filing, section, ordinal).
``v_veto``                 active + cleared rule hits; ``cleared_at IS NULL`` = active.
``v_portfolio_position``   position stints; ``valid_to IS NULL`` = open.
``v_shared_executive_edge``pair-level aggregate of ``shared_executive_edge`` person rows.
``v_cycle_ranking``        the ranked cohort of the latest cycle per cycle_type.
"""

from __future__ import annotations

import sqlite3

VIEWS: dict[str, str] = {
    "v_score_snapshot": """
        CREATE VIEW v_score_snapshot AS
        SELECT s.id, a.ticker, s.asset_id, s.score_type, s.raw_value, s.normalized_score,
               s.event_time, s.computed_at, s.model, s.inputs_json, s.filing_id,
               s.rating, s.narrative, s.strengths_json, s.risks_json,
               s.run_id, s.run_kind
        FROM score_snapshot s JOIN assets a ON a.id = s.asset_id
    """,
    "v_universe_membership": """
        CREATE VIEW v_universe_membership AS
        SELECT m.id, a.ticker, m.asset_id, m.universe, m.valid_from, m.valid_to,
               m.detected_at, m.source, m.run_id, m.run_kind
        FROM universe_membership m JOIN assets a ON a.id = m.asset_id
    """,
    "v_price_observation": """
        CREATE VIEW v_price_observation AS
        SELECT p.id, a.ticker, p.asset_id, p.obs_date, p.close, p.prev_close, p.log_return,
               p.true_range, p.atr_14, p.realized_vol_21d, p.realized_vol_90d,
               p.max_drawdown_90d, p.momentum_21d, p.momentum_63d, p.momentum_252d,
               p.dollar_volume, p.source, p.event_time, p.computed_at, p.engine_version
        FROM price_observation p JOIN assets a ON a.id = p.asset_id
        WHERE p.engine_version = (
            SELECT p2.engine_version FROM price_observation p2
            WHERE p2.asset_id = p.asset_id AND p2.obs_date = p.obs_date
            ORDER BY p2.computed_at DESC, p2.id DESC LIMIT 1
        )
    """,
    "v_sec_filing_section": """
        CREATE VIEW v_sec_filing_section AS
        SELECT sec.id, a.ticker, f.asset_id, sec.filing_id, f.form, f.fiscal_period,
               sec.section_type, sec.item_number, sec.heading, sec.ordinal,
               sec.text, sec.text_sha256, sec.word_count, sec.extraction_method,
               sec.source_url, sec.event_time, sec.retrieved_at, sec.engine_version
        FROM sec_filing_section sec
        JOIN sec_filings f ON f.id = sec.filing_id
        JOIN assets a ON a.id = f.asset_id
    """,
    "v_veto": """
        CREATE VIEW v_veto AS
        SELECT v.id, a.ticker, v.asset_id, v.rule_id, v.severity, v.detected_at,
               v.cycle_date, v.cleared_at, v.evidence_json, v.run_id
        FROM veto v JOIN assets a ON a.id = v.asset_id
    """,
    "v_portfolio_position": """
        CREATE VIEW v_portfolio_position AS
        SELECT p.id, a.ticker, p.asset_id, p.valid_from, p.valid_to, p.weight,
               p.cost_basis, p.opened_by_cycle, p.run_id
        FROM portfolio_position p JOIN assets a ON a.id = p.asset_id
    """,
    "v_shared_executive_edge": """
        CREATE VIEW v_shared_executive_edge AS
        SELECT e.asset_id_a, aa.ticker AS ticker_a, e.asset_id_b, ab.ticker AS ticker_b,
               COUNT(*) AS person_count, SUM(e.weight) AS total_weight,
               MIN(e.first_seen) AS first_seen, MAX(e.last_seen) AS last_seen,
               e.method
        FROM shared_executive_edge e
        JOIN assets aa ON aa.id = e.asset_id_a
        JOIN assets ab ON ab.id = e.asset_id_b
        GROUP BY e.asset_id_a, e.asset_id_b, e.method
    """,
    "v_cycle_ranking": """
        CREATE VIEW v_cycle_ranking AS
        SELECT r.cycle_run_id, cr.cycle_type, cr.cycle_date, a.ticker, r.asset_id,
               r.rank, r.blended_score, r.components_json, r.vetoed, r.veto_rules_json,
               r.selected, r.target_weight
        FROM cycle_ranking r
        JOIN cycle_run cr ON cr.id = r.cycle_run_id
        JOIN assets a ON a.id = r.asset_id
    """,
}


def ensure_views(conn: sqlite3.Connection) -> None:
    """Drop and recreate every read-contract view. Tolerates missing base tables."""
    for name, ddl in VIEWS.items():
        try:
            conn.execute(f"DROP VIEW IF EXISTS {name}")
            conn.execute(ddl)
        except sqlite3.OperationalError:
            # A base table this view needs does not exist yet in this DB; skip it.
            conn.execute(f"DROP VIEW IF EXISTS {name}")
    conn.commit()
