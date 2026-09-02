"""Read-contract VIEWs consumed by the integration repo's RDF projection.

Every view joins ``assets`` so ``ticker`` sits beside ``asset_id``, and freezes a
stable column order. The integration repo should read these, never the base tables,
so the physical schema here can evolve underneath it. Views are dropped and
recreated on every :func:`kg_schema.ensure` call -- they are cheap and always current.

Projection semantics
--------------------
``v_score_snapshot``      one row per (asset, score_type, event_time). ``event_time`` is
                          the filing period-end for FUNDAMENTAL, the cycle date for
                          TECHNICAL / VALORIZATION, and the article-day for SEMANTIC.
``v_universe_membership``  one row per membership stint; ``valid_to IS NULL`` = current.
``v_sector``              one row per GICS sector with its member-asset and sub-industry
                          counts (the reference lane's rollup anchor).
``v_industry``            one row per GICS sub-industry -> sector (the scrape has no
                          middle industry-group tier; sub-industry stands in for it).
``v_price_observation``    latest ``engine_version`` per (asset, obs_date); derived
                          price analytics only (raw OHLCV stays in ``price_daily``).
``v_sec_filing``          one row per EDGAR filing (form, fiscal_period, accession,
                          period_end) -- the filing-level parent of ``v_sec_filing_section``.
``v_sec_filing_section``   narrative filing text; one row per (filing, section, ordinal).
                          ``item_label`` is the ontology ``itemLabel`` token
                          (``ITEM_1A_RISK_FACTORS``, ``ITEM_7_MDA``, ...), derived in SQL
                          from ``(item_number, section_type)`` -- kept identical to
                          ``fundamental_agent.sections.canonical_item_label``.
``v_veto``                 active + cleared rule hits; ``cleared_at IS NULL`` = active.
``v_rule_catalog``        one row per veto rule (the rule catalog as data). ``params_json``
                          is kept verbatim; ``param_metric`` / ``param_operator`` /
                          ``param_threshold`` unpack the threshold-rule shape when present.
``v_portfolio_position``   position stints; ``valid_to IS NULL`` = open.
``v_shared_executive_edge``pair-level aggregate of ``shared_executive_edge`` person rows.
``v_cycle_ranking``        the ranked cohort of the latest cycle per cycle_type.
``v_weight_scheme``       one row per ``cycle_run`` that recorded a blend: the scheme id and
                          the scalar knobs (``top_n``, name/sector caps, soft-veto penalty).
                          ``cycle_date`` is the scheme's effective date -- a later run with a
                          changed blend is a new row, no bespoke ``valid_from``/``valid_to``.
``v_weight_component``     one row per (``cycle_run``, ``score_type``): the blend weight that
                          score type carried in that run. Explodes ``score_weights``.
``v_sector_aggregate_snapshot`` per-cycle mean of members' TECHNICAL score per sector.
                          Its per-asset counterpart is ``score_snapshot`` rows of type
                          ``SECTOR`` (own TECHNICAL raw minus this mean), reachable through
                          ``v_score_snapshot``.
``v_corporate_action``    one row per (asset, action_type, ex_date) at the latest
                          ``engine_version``. Dividends (cash/share) and splits (ratio).
``v_quant_return_daily``  the quant/ total-return daily series; latest ``engine_version``
                          per (asset, obs_date). ``tr_log_return`` is the optimizer input.
                          Empty until ``quant build-returns`` has run.
``v_risk_free_rate``      risk-free curve points; latest ``engine_version`` per (curve, date).
``v_benchmark_series``    benchmark index levels / returns; latest ``engine_version`` per
                          (benchmark, obs_date).
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
    "v_sector": """
        CREATE VIEW v_sector AS
        SELECT s.id AS sector_id, s.name AS sector_name,
               COUNT(a.id) AS asset_count,
               COUNT(DISTINCT NULLIF(a.sub_industry, '')) AS sub_industry_count
        FROM sectors s LEFT JOIN assets a ON a.sector_id = s.id
        GROUP BY s.id, s.name
    """,
    "v_industry": """
        CREATE VIEW v_industry AS
        SELECT a.sub_industry AS industry_name, s.name AS sector_name, s.id AS sector_id,
               COUNT(*) AS asset_count
        FROM assets a JOIN sectors s ON s.id = a.sector_id
        WHERE a.sub_industry IS NOT NULL AND a.sub_industry <> ''
        GROUP BY a.sub_industry, s.name, s.id
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
    "v_sec_filing": """
        CREATE VIEW v_sec_filing AS
        SELECT f.id, a.ticker, f.asset_id, f.form, f.fiscal_year, f.fiscal_period,
               f.filing_date, f.accession_number, f.period_end, f.retrieved_at
        FROM sec_filings f JOIN assets a ON a.id = f.asset_id
    """,
    "v_sec_filing_section": """
        CREATE VIEW v_sec_filing_section AS
        SELECT sec.id, a.ticker, f.asset_id, sec.filing_id, f.form, f.fiscal_period,
               sec.section_type, sec.item_number,
               CASE
                 WHEN sec.item_number IS NULL OR TRIM(sec.item_number) = ''
                   THEN CASE sec.section_type WHEN 'MD&A' THEN 'MDA'
                        ELSE REPLACE(REPLACE(UPPER(sec.section_type), ' ', '_'), '&', 'AND') END
                 ELSE 'ITEM_' || UPPER(TRIM(sec.item_number)) || '_' ||
                      CASE sec.section_type WHEN 'MD&A' THEN 'MDA'
                        ELSE REPLACE(REPLACE(UPPER(sec.section_type), ' ', '_'), '&', 'AND') END
               END AS item_label,
               sec.heading, sec.ordinal,
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
    "v_rule_catalog": """
        CREATE VIEW v_rule_catalog AS
        SELECT rc.rule_id, rc.description, rc.severity, rc.enabled, rc.params_json,
               json_extract(rc.params_json, '$.metric')    AS param_metric,
               json_extract(rc.params_json, '$.op')        AS param_operator,
               json_extract(rc.params_json, '$.threshold') AS param_threshold,
               rc.created_at
        FROM rule_catalog rc
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
    "v_weight_scheme": """
        CREATE VIEW v_weight_scheme AS
        SELECT cr.id AS cycle_run_id, cr.cycle_type, cr.cycle_date,
               json_extract(cr.params_json, '$.weight_scheme')          AS scheme_id,
               json_extract(cr.params_json, '$.score_weights')          AS weights_json,
               CAST(json_extract(cr.params_json, '$.top_n') AS INTEGER) AS top_n,
               json_extract(cr.params_json, '$.max_name_weight')        AS max_name_weight,
               json_extract(cr.params_json, '$.max_sector_weight')      AS max_sector_weight,
               json_extract(cr.params_json, '$.soft_veto_penalty')      AS soft_veto_penalty
        FROM cycle_run cr
        WHERE json_extract(cr.params_json, '$.score_weights') IS NOT NULL
    """,
    "v_weight_component": """
        CREATE VIEW v_weight_component AS
        SELECT cr.id AS cycle_run_id, cr.cycle_type, cr.cycle_date,
               je.key AS score_type, je.value AS weight
        FROM cycle_run cr,
             json_each(json_extract(cr.params_json, '$.score_weights')) je
        WHERE json_extract(cr.params_json, '$.score_weights') IS NOT NULL
    """,
    "v_sector_aggregate_snapshot": """
        CREATE VIEW v_sector_aggregate_snapshot AS
        SELECT sa.id, s.name AS sector_name, sa.sector_id, sa.cycle_date, sa.metric_type,
               sa.member_count, sa.mean_raw, sa.mean_normalized, sa.computed_at, sa.run_id
        FROM sector_aggregate_snapshot sa JOIN sectors s ON s.id = sa.sector_id
    """,
    "v_corporate_action": """
        CREATE VIEW v_corporate_action AS
        SELECT c.id, a.ticker, c.asset_id, c.action_type, c.ex_date, c.value, c.currency,
               c.declared_date, c.record_date, c.pay_date, c.frequency,
               c.source, c.engine_version, c.ingested_at
        FROM corporate_action c JOIN assets a ON a.id = c.asset_id
        WHERE c.engine_version = (
            SELECT c2.engine_version FROM corporate_action c2
            WHERE c2.asset_id = c.asset_id AND c2.action_type = c.action_type
              AND c2.ex_date = c.ex_date
            ORDER BY c2.ingested_at DESC, c2.id DESC LIMIT 1
        )
    """,
    "v_quant_return_daily": """
        CREATE VIEW v_quant_return_daily AS
        SELECT q.id, a.ticker, q.asset_id, q.obs_date, q.close_split_adj, q.adj_close,
               q.tr_index, q.cash_dividend, q.split_factor, q.price_log_return,
               q.tr_log_return, q.source, q.engine_version, q.computed_at
        FROM quant_return_daily q JOIN assets a ON a.id = q.asset_id
        WHERE q.engine_version = (
            SELECT q2.engine_version FROM quant_return_daily q2
            WHERE q2.asset_id = q.asset_id AND q2.obs_date = q.obs_date
            ORDER BY q2.computed_at DESC, q2.id DESC LIMIT 1
        )
    """,
    "v_risk_free_rate": """
        CREATE VIEW v_risk_free_rate AS
        SELECT r.id, r.curve, r.rate_date, r.annualized_rate, r.source,
               r.engine_version, r.ingested_at
        FROM risk_free_rate r
        WHERE r.engine_version = (
            SELECT r2.engine_version FROM risk_free_rate r2
            WHERE r2.curve = r.curve AND r2.rate_date = r.rate_date
            ORDER BY r2.ingested_at DESC, r2.id DESC LIMIT 1
        )
    """,
    "v_benchmark_series": """
        CREATE VIEW v_benchmark_series AS
        SELECT b.id, b.benchmark, b.obs_date, b.level, b.total_return_level, b.log_return,
               b.source, b.engine_version, b.ingested_at
        FROM benchmark_series b
        WHERE b.engine_version = (
            SELECT b2.engine_version FROM benchmark_series b2
            WHERE b2.benchmark = b.benchmark AND b2.obs_date = b.obs_date
            ORDER BY b2.ingested_at DESC, b2.id DESC LIMIT 1
        )
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
