"""Read-contract VIEWs consumed by the integration repo's RDF projection.

Every view joins ``assets`` so ``ticker`` sits beside ``asset_id``, and freezes a
stable column order. The integration repo should read these, never the base tables,
so the physical schema here can evolve underneath it. Views are dropped and
recreated on every :func:`kg_schema.ensure` call -- they are cheap and always current.

View groups (TOC -- see "Projection semantics" below for the per-view detail)
-------------------------------------------------------------------------------
Run log / provenance     v_analysis_run, v_pricing_run, v_quant_run, v_cycle_run,
                         v_orchestrator_run, v_orchestrator_step
Universe & coverage      v_universe_membership, v_ticker_history, v_universe_coverage, v_sector, v_industry
Scores & signals         v_score_snapshot, v_fundamental_metric, v_sector_aggregate_snapshot
Pricing & returns        v_price_observation, v_corporate_action, v_quant_return_daily,
                         v_risk_free_rate, v_benchmark_series
Filings & narrative      v_sec_filing, v_sec_filing_section
Rules & live portfolio   v_veto, v_rule_catalog, v_data_quality_issue, v_portfolio_position,
                         v_shared_executive_edge
Cycle ranking & weights  v_cycle_ranking, v_cycle_ranking_component, v_weight_scheme,
                         v_weight_component
Quant optimization       v_quant_risk_model, v_quant_portfolio, v_quant_position,
                         v_quant_frontier_point, v_quant_benchmark_performance, v_quant_vs_live
Backtest                 v_book, v_book_rebalance, v_guard_alert, v_guard_action,
                         v_book_weight_daily, v_book_daily_performance,
                         v_portfolio_period_summary, v_backtest_test

Projection semantics
--------------------
``v_score_snapshot``      one row per (asset, score_type, event_time). **T-153 part 3d:** for a
                          line the private import priced, a VALORIZATION row's ``inputs_json`` is
                          NULL (the whole block is built on the market cap) and a FUNDAMENTAL
                          row's has the five valuation keys removed (its pillars and every other
                          metric stay). ``raw_value``, ``normalized_score``, ranks and weights are
                          untouched: the rule keeps scores. ``event_time`` is
                          the filing period-end for FUNDAMENTAL, the cycle date for
                          TECHNICAL / VALORIZATION, and the article-day for SEMANTIC.
                          ``available_at`` is when the row became usable -- the column an
                          as-of reader filters on, never ``event_time``: for FUNDAMENTAL
                          (T-107) the first NYSE trading day after the filing date; for
                          TECHNICAL / VALORIZATION / SECTOR (T-144) the cycle date, i.e. its
                          ``event_time``. ``forensic_flags_json`` and ``prompt_hash`` are
                          FUNDAMENTAL only (NULL on every other score_type).
``v_fundamental_metric``  one row per (filing, metric_group, metric_name, engine_version) --
                          the deterministic ratios behind the FUNDAMENTAL score. ``metric_id``
                          (``group.name``) joins to ``v_rule_catalog.param_metric``.
                          ``is_current`` = the engine version ``kg_schema.versions.
                          resolve_metric_versions`` picks with no explicit selection (the
                          newest per metric group by explicit version order, never by
                          ``computed_at``); a filing not recomputed under it has no current
                          row. At most one current version per metric group (T-144).
                          **T-153 part 3d:** ``value`` is NULL for the five ``valuation`` metrics
                          of a line the private import priced (the row stays) -- a market cap is
                          the public cover count times the close.
``v_universe_membership``  one row per membership stint; ``valid_to IS NULL`` = current.
                          FROZEN: the agents no longer write ``universe_membership``
                          (the universe is read point-in-time from ``universe.db``).
                          Kept for back-compat; new readers should use ``universe.db``.
``v_ticker_history``      one row per symbol of a company's line (T-153 part 2): the symbol, its
                          ``valid_from`` / ``valid_to`` (``valid_to`` = the symbol's last day),
                          the line's ``canonical_symbol`` and the ``asset_id`` of the one asset
                          the line resolves to. Keyed by CIK -- the same symbol reused by another
                          CIK later is a different line. ``assets.ticker`` is the line's
                          canonical (current) symbol, so every other view shows the current
                          ticker even on a historical row; join this view for the as-of ticker.
``v_universe_coverage``   per (as_of, universe, symbol): whether that member had an
                          identity row / FUNDAMENTAL score / metrics / prices /
                          observations / returns as of the date. ``covered`` = every
                          required check passed. Written by the ``coverage`` command.
``v_analysis_run`` / ``v_pricing_run`` / ``v_quant_run`` / ``v_cycle_run``
                          the run log per agent: ``run_id``, the ``as_of``
                          (``analysis_date``; ``cycle_date`` for cycle),
                          ``code_version`` (the code tag that produced the run),
                          status, timings and ``params_json``. ``portfolio-reports``
                          enumerates and traces past runs through these.
                          In ``v_analysis_run``, the units are:
                          ``planned_units`` is one per (ticker, form, year) task;
                          ``completed_units`` and ``skipped_units`` are one per filing
                          (a 10-Q year task has several filings);
                          ``failed_units`` is one per failed (ticker, form, year) task
                          or per failed filing.
                          Because ``planned_units`` and the other three use different
                          units, completed_units + skipped_units + failed_units need
                          not equal planned_units.
``v_orchestrator_run`` / ``v_orchestrator_step``
                          the orchestrator's own provenance (T-012), written by
                          ``api/orchestrator.py``: one row per orchestrated run
                          (``run_id``, ``kind``, ``as_of``, ``code_version``, ``status``,
                          ``started_at``, ``finished_at``, ``settings_json``, ``handle``,
                          ``surface``) and one per step (``orchestrator_run_id``, ``handle``,
                          ``ordinal``, ``step``, ``status``, timings, ``exit_code``,
                          ``run_kind``, ``run_id`` -- the step's own run-log id, ``reason``,
                          ``argv_json``, ``output_tail``). ``GET /run/{handle}`` reads these.
``v_sector``              one row per GICS sector with its member-asset and sub-industry
                          counts (the reference lane's rollup anchor).
``v_industry``            one row per GICS sub-industry -> sector (the scrape has no
                          middle industry-group tier; sub-industry stands in for it).
``v_price_observation``    latest ``engine_version`` per (asset, obs_date); derived
                          price analytics only (raw OHLCV stays in ``price_daily``).
                          ``sma_200`` .. ``vol_60d_base`` (T-070, appended) exist from
                          ``priceobs-v2`` and are NULL on a ``priceobs-v1`` row and until their
                          window is full. **T-153 part 3d:** rows whose bar came from the private
                          Tiingo import (``source='tiingo'``) are **not returned** -- the view is
                          a price series and the data is never redistributed. An older NULL
                          ``source`` (every row stored before the import existed) stays
                          visible: the filter is ``COALESCE(source,'') <> 'tiingo'``.
``v_sec_filing``          one row per EDGAR filing (form, fiscal_period, accession,
                          period_end) -- the filing-level parent of ``v_sec_filing_section``.
``v_sec_filing_section``   narrative filing text; one row per (filing, section, ordinal).
                          ``item_label`` is the ontology ``itemLabel`` token
                          (``ITEM_1A_RISK_FACTORS``, ``ITEM_7_MDA``, ...), derived in SQL
                          from ``(item_number, section_type)`` -- kept identical to
                          ``fundamental_agent.sections.canonical_item_label``.
``v_veto``                 rule-hit stints (T-125); ``cleared_on IS NULL`` = an open (active)
                          stint. ``raised_on``/``cleared_on``/``last_seen_on`` are cycle dates;
                          ``detected_at``/``cleared_at`` are wall-clock metadata only.
                          ``expires_on`` (T-070, appended) is a temporal veto's earliest clearing
                          cycle date; NULL on every other veto.
``v_data_quality_issue``  one row per Ring-1 ``DQ_*`` gate hit on a filing's metric (T-065),
                          with the filing's form / period beside it. ``quarantined = 1`` =
                          consumers read the metric as NULL; HARD = a ``cycle`` DATA_QUALITY
                          veto; SOFT and not quarantined = the non-blocking review list.
``v_rule_catalog``        one row per veto rule (the rule catalog as data). ``params_json``
                          is kept verbatim; ``param_metric`` / ``param_operator`` /
                          ``param_threshold`` unpack the threshold-rule shape when present.
``v_portfolio_position``   position stints; ``valid_to IS NULL`` = open. **T-153 part 3d:**
                          ``cost_basis`` is the close the position opened at, so it is NULLed
                          when that line's prices came from the private Tiingo import; the row
                          (and its weight) stays.
``v_shared_executive_edge``pair-level aggregate of ``shared_executive_edge`` person rows;
                          ``computed_at`` / ``run_id`` are those of the build that wrote them.
``v_cycle_ranking``        one row per (``cycle_run``, asset) of EVERY cycle run, whatever its
                          ``status`` -- not just the latest. A reader that wants one cohort
                          filters ``status = 'completed'`` and picks the run (``cycle_type``,
                          ``cycle_date``) itself.
``v_cycle_ranking_component`` one row per non-null component of every ranking row, vetoed or
                          not: the component value, the blend weight configured for it
                          (``v_weight_component``) and the effective weight (configured over
                          the sum of the row's non-null components' weights), so that
                          sum(effective_weight * component_value) - the SOFT-veto penalty =
                          ``blended_score`` (T-144). The penalty is the sum, over the row's SOFT
                          rules (``veto_rules_json`` entries other than HARD / UNSCORED /
                          UNPRICED), of each
                          rule's ``penalty_points`` (``v_rule_catalog.params_json``; the run's
                          ``soft_veto_penalty`` where a rule declares none): 15 for most, 0 for the
                          flag-only ``BREAK_TREND_200`` (T-070), not 15 x their count.
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
                          **T-153 part 3d:** the private import's rows are **not returned**
                          (``COALESCE(source,'') <> 'tiingo'``) -- the section 4 rule names a
                          dividend value, so the licence's "-- never a value" applies here too.
                          ``quant`` reads the table, not this view, so returns do not change.
``v_quant_return_daily``  the quant/ total-return daily series; latest ``engine_version``
                          per (asset, obs_date). ``tr_log_return`` is the optimizer input.
                          Empty until ``quant build-returns`` has run. **T-153 part 3d:** the
                          ``vendor`` column (appended) names the origin of the close the row was
                          built from, and rows whose close came from the private Tiingo import
                          are **not returned** -- a level series reveals the close as much as
                          the close does.
``v_risk_free_rate``      risk-free curve points; latest ``engine_version`` per (curve, date).
``v_benchmark_series``    benchmark index levels / returns; latest ``engine_version`` per
                          (benchmark, obs_date).
``v_quant_risk_model``    one row per (as_of, model_version): the Markowitz risk model's
                          metadata (estimators, shrinkage, panel spec, rf). The mu vector
                          and covariance matrix stay in ``quant_expected_return`` /
                          ``quant_covariance`` and are not projected. From ``rm-v2`` (T-077) the
                          model also stores a ``carhart`` mu, and ``manifest_json`` carries a
                          ``carhart`` object (factor-file version and SHA-256, regression range,
                          lambda-bar, the beta prior, the shrinkage weights, the flagged assets) or
                          ``{"unavailable": reason}``. ``ret_estimator`` is the estimator configured
                          when the model was built, not a list of the mu stored.
``v_quant_portfolio``     one row per optimized benchmark book (as_of, kind); portfolio-level
                          expected/realized risk-return, not per asset. A book optimized under a
                          non-default estimator or a turnover cap (T-077) sits beside the default
                          book at the same (as_of, kind): its ``engine_version`` carries the marks before
                          the first ``+`` (``opt-v2.mu-<estimator>.to-<cap>+<tag>``), its
                          ``manifest_json`` names them, and ``turnover`` is its realized trade against
                          the previous book of that chain. A variant book is **never** current
                          (``opt-v<N>`` must be all digits up to the first ``+``), whatever the
                          compute order, so ``v_quant_vs_live``'s ``LIVE_ONLY`` rows follow the
                          default book only. ``is_current`` (T-144)
                          marks the newest ``opt-v<N>`` per (as_of, kind, frontier_k) -- by
                          ``N``, then ``computed_at``, then ``id``; at most one per key.
``v_quant_position``      the weights of each book; ``valid_to IS NULL`` = current (mirrors
                          ``v_portfolio_position``).
``v_quant_frontier_point`` the efficient-frontier sweep per risk model.
``v_quant_benchmark_performance`` forward realized daily / cumulative return of a frozen
                          book, and its active return vs the benchmark; the latest
                          ``engine_version`` per (book, date) -- ``perf-v1`` rows (graded
                          against ``bench-v1``) stay stored under their version (T-108). From
                          ``perf-v3`` (T-077) the returns are net of a one-off turnover cost on a
                          book's first forward day; ``perf-v2`` rows are gross.
``v_quant_vs_live``       per-name weight of every optimized book beside the live
                          ``portfolio_position`` book as of the same date (active weight).
                          Plus (T-042) one ``kind = 'LIVE_ONLY'`` row per live position held
                          in *no* optimized book of that as-of date: ``benchmark_weight`` NULL,
                          ``active_weight = -live_weight``. Before, such a name was absent from
                          the view altogether, so the live book looked smaller than it is.
                          Every kind but ``live_book`` is a benchmark book (T-144 dropped the
                          dead ``equal_weight`` / ``cap_weight`` filters). ``engine_version`` /
                          ``is_current`` are the book's; a ``LIVE_ONLY`` row has a NULL
                          ``engine_version`` and ``is_current = 1``.
``v_book`` / ``v_book_rebalance``  one row per strategy book (arm) of a ``backtest_run`` / per
                          scheduled rebalance of one book: decision and execution dates, the
                          ``quant_portfolio`` / ``cycle_run`` / ``quant_risk_model`` ids it used.
``v_guard_alert`` / ``v_guard_action``  the GUARD alerts a run raised (an L3/L4 scale alert has
                          ``asset_id`` NULL) and the action each book took on it: ``APPLIED``,
                          ``ARM_DISABLED`` (the NOGUARD twin's shadow alert, never applied),
                          ``NOT_HELD`` or ``SUPERSEDED``.
``v_book_weight_daily``   weights only: the Markowitz target, the drifted weight, the NOGUARD
                          reference (``pre_guard_weight``) and the final target for the next
                          session. Never a price or a return.
``v_book_daily_performance``  portfolio-level daily gross/net return, turnover, cost, NAV,
                          drawdown, cash weight and equity count, with the EW benchmark. Never
                          one asset's return.
``v_portfolio_period_summary``  per-book, per-segment statistics (P1, P2/P2a/P2b, P3).
``v_backtest_test``       the paired tests (Memmel-Jobson-Korkie, Ledoit-Wolf bootstrap, HAC) of
                          two arms of a run. ``knowledge_date`` is the row's own date for dated
                          rows, the run's creation date for run-level rows and ``date_to`` for a
                          summary.
"""

from __future__ import annotations

import logging

from portfolio_common.db import Database, DatabaseError

from .migrations import MIGRATIONS

log = logging.getLogger(__name__)

VIEWS: dict[str, str] = {
    "v_score_snapshot": """
        CREATE VIEW v_score_snapshot AS
        SELECT s.id, a.ticker, s.asset_id, s.score_type, s.raw_value, s.normalized_score,
               s.event_time, s.computed_at, s.model,
               CASE
                   WHEN s.score_type = 'VALORIZATION'
                        AND EXISTS (SELECT 1 FROM price_daily pd
                                    WHERE pd.asset_id = s.asset_id AND pd.source = 'tiingo')
                       THEN NULL
                   WHEN s.score_type = 'FUNDAMENTAL'
                        AND EXISTS (SELECT 1 FROM price_daily pd
                                    WHERE pd.asset_id = s.asset_id AND pd.source = 'tiingo')
                       THEN json_remove(
                           s.inputs_json,
                           '$."valuation.market_capitalization"',
                           '$."valuation.enterprise_value"',
                           '$."valuation.free_cash_flow_yield"',
                           '$."valuation.sbc_adjusted_fcf_yield"',
                           '$."valuation.enterprise_fcf_yield"')
                   ELSE s.inputs_json END AS inputs_json,
               s.filing_id,
               s.rating, s.narrative, s.strengths_json, s.risks_json,
               s.run_id, s.run_kind,
               CASE WHEN s.score_type IN ('TECHNICAL', 'VALORIZATION', 'SECTOR')
                    THEN COALESCE(s.available_at, s.event_time)
                    ELSE s.available_at END AS available_at,
               CASE WHEN s.score_type = 'FUNDAMENTAL' THEN s.forensic_flags_json END
                   AS forensic_flags_json,
               CASE WHEN s.score_type = 'FUNDAMENTAL' THEN s.prompt_hash END AS prompt_hash
        FROM score_snapshot s JOIN assets a ON a.id = s.asset_id
    """,
    "v_fundamental_metric": """
        CREATE VIEW v_fundamental_metric AS
        WITH versions AS (
            -- the order of kg_schema.versions.version_key: family rank, then N. A string that is
            -- not exactly '<family>-v<digits>', or of a group the resolver does not read, has no
            -- rank and is never current. METRIC_GROUPS is pinned by tests/test_kg_view_contract.py.
            SELECT metric_group, engine_version,
                   CASE WHEN engine_version GLOB 'pre-v[0-9]*'
                             AND substr(engine_version, 6) NOT GLOB '*[^0-9]*' THEN 0
                        WHEN engine_version GLOB 'metrics-v[0-9]*'
                             AND substr(engine_version, 10) NOT GLOB '*[^0-9]*' THEN 1 END
                       AS family_rank,
                   CAST(substr(engine_version, instr(engine_version, '-v') + 2) AS INTEGER)
                       AS version_n
            FROM (SELECT DISTINCT metric_group, engine_version FROM fundamental_metrics
                  WHERE engine_version IS NOT NULL
                    AND metric_group IN ('profitability', 'liquidity', 'leverage', 'efficiency',
                                         'growth', 'cashflow', 'roic', 'cagr', 'valuation'))
        ),
        current_version AS (
            -- equal ranks (metrics-v02 and metrics-v2) tie-break on the string, so at most one
            -- version per group is current
            SELECT v.metric_group, v.engine_version FROM versions v
            WHERE v.family_rank IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM versions v2
                  WHERE v2.metric_group = v.metric_group AND v2.family_rank IS NOT NULL
                    AND (v2.family_rank > v.family_rank
                         OR (v2.family_rank = v.family_rank
                             AND (v2.version_n > v.version_n
                                  OR (v2.version_n = v.version_n
                                      AND v2.engine_version > v.engine_version)))))
        )
        SELECT a.ticker, f.asset_id, m.filing_id, m.metric_group, m.metric_name,
               m.metric_group || '.' || m.metric_name AS metric_id, m.unit,
               -- T-153 part 3d: a market cap is the (public) cover count times the close, so a
               -- valuation value is the close in another form -- NULLed for a line the private
               -- import priced, from the same per-line predicate the positions view uses.
               CASE WHEN m.metric_group = 'valuation'
                         AND m.metric_name IN ('market_capitalization', 'enterprise_value',
                                               'free_cash_flow_yield', 'sbc_adjusted_fcf_yield',
                                               'enterprise_fcf_yield')
                         AND EXISTS (SELECT 1 FROM price_daily pd
                                     WHERE pd.asset_id = f.asset_id AND pd.source = 'tiingo')
                       THEN NULL ELSE m.value END AS value,
               m.engine_version,
               CASE WHEN cv.engine_version IS NULL THEN 0 ELSE 1 END AS is_current,
               m.event_time, m.available_at, m.run_id
        FROM fundamental_metrics m
        JOIN sec_filings f ON f.id = m.filing_id
        JOIN assets a ON a.id = f.asset_id
        LEFT JOIN current_version cv
               ON cv.metric_group = m.metric_group AND cv.engine_version = m.engine_version
    """,
    "v_universe_membership": """
        CREATE VIEW v_universe_membership AS
        SELECT m.id, a.ticker, m.asset_id, m.universe, m.valid_from, m.valid_to,
               m.detected_at, m.source, m.run_id, m.run_kind
        FROM universe_membership m JOIN assets a ON a.id = m.asset_id
    """,
    "v_ticker_history": """
        CREATE VIEW v_ticker_history AS
        SELECT t.cik, t.symbol, t.valid_from, t.valid_to, t.canonical_symbol,
               a.id AS asset_id, t.evidence_url, t.evidence_date, t.rules
        FROM ticker_history t
        LEFT JOIN assets a ON a.cik = t.cik AND a.ticker = t.canonical_symbol
    """,
    "v_analysis_run": """
        CREATE VIEW v_analysis_run AS
        SELECT id AS run_id, as_of, code_version, status, started_at, finished_at,
               universe_size, planned_units, completed_units, skipped_units, failed_units,
               params_json
        FROM analysis_run
    """,
    "v_pricing_run": """
        CREATE VIEW v_pricing_run AS
        SELECT id AS run_id, as_of, code_version, status, started_at, finished_at,
               universe_size, planned_units, completed_units, skipped_units, failed_units,
               params_json
        FROM pricing_run
    """,
    "v_quant_run": """
        CREATE VIEW v_quant_run AS
        SELECT id AS run_id, command, as_of, code_version, engine_version, status,
               started_at, finished_at, error, params_json
        FROM quant_run
    """,
    "v_cycle_run": """
        CREATE VIEW v_cycle_run AS
        SELECT id AS run_id, cycle_type, cycle_date AS as_of, code_version, status,
               started_at, finished_at, params_json
        FROM cycle_run
    """,
    "v_orchestrator_run": """
        CREATE VIEW v_orchestrator_run AS
        SELECT id AS run_id, kind, as_of, code_version, status, started_at, finished_at,
               settings_json, handle, surface
        FROM orchestrator_run
    """,
    "v_orchestrator_step": """
        CREATE VIEW v_orchestrator_step AS
        SELECT s.orchestrator_run_id, r.handle, s.ordinal, s.step, s.status,
               s.started_at, s.finished_at, s.exit_code, s.run_kind, s.run_id, s.reason,
               s.argv_json, s.output_tail
        FROM orchestrator_step s JOIN orchestrator_run r ON r.id = s.orchestrator_run_id
    """,
    "v_universe_coverage": """
        CREATE VIEW v_universe_coverage AS
        SELECT id, as_of, universe, symbol, asset_id, in_assets, has_fundamental, has_metrics,
               has_pricing, has_observations, has_returns, covered, missing_json,
               checked_at, run_id
        FROM universe_coverage
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
               p.dollar_volume, p.source, p.event_time, p.computed_at, p.engine_version,
               p.sma_200, p.ret_5d, p.vol_5d, p.mu_60d_base, p.vol_60d_base
        FROM price_observation p JOIN assets a ON a.id = p.asset_id
        WHERE p.engine_version = (
            SELECT p2.engine_version FROM price_observation p2
            WHERE p2.asset_id = p.asset_id AND p2.obs_date = p.obs_date
            ORDER BY p2.computed_at DESC, p2.id DESC LIMIT 1
        )
          AND COALESCE(p.source, '') <> 'tiingo'
    """,
    "v_sec_filing": """
        CREATE VIEW v_sec_filing AS
        SELECT f.id, a.ticker, f.asset_id, f.form, f.fiscal_year, f.fiscal_period,
               f.filing_date, f.accession_number, f.period_end, f.retrieved_at, f.available_at
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
        SELECT v.id, a.ticker, v.asset_id, v.rule_id, v.severity, v.raised_on, v.cleared_on,
               v.last_seen_on, v.detected_at, v.cleared_at, v.evidence_json, v.run_id,
               v.expires_on
        FROM veto v JOIN assets a ON a.id = v.asset_id
    """,
    "v_data_quality_issue": """
        CREATE VIEW v_data_quality_issue AS
        SELECT d.id, a.ticker, d.asset_id, d.filing_id, f.form, f.fiscal_period, f.period_end,
               d.metric_group, d.metric_name, d.metric_engine_version, d.rule_id, d.severity,
               d.quarantined, d.value, d.evidence_json, d.gate_version, d.created_at, d.run_id
        FROM data_quality_issue d
        JOIN assets a ON a.id = d.asset_id
        JOIN sec_filings f ON f.id = d.filing_id
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
               CASE WHEN EXISTS (
                        SELECT 1 FROM price_daily pd
                        WHERE pd.asset_id = p.asset_id AND pd.source = 'tiingo'
                   )
                    THEN NULL ELSE p.cost_basis END AS cost_basis,
               p.opened_by_cycle, p.run_id
        FROM portfolio_position p JOIN assets a ON a.id = p.asset_id
    """,
    "v_shared_executive_edge": """
        CREATE VIEW v_shared_executive_edge AS
        SELECT e.asset_id_a, aa.ticker AS ticker_a, e.asset_id_b, ab.ticker AS ticker_b,
               COUNT(*) AS person_count, SUM(e.weight) AS total_weight,
               MIN(e.first_seen) AS first_seen, MAX(e.last_seen) AS last_seen,
               e.method, MAX(e.computed_at) AS computed_at, MAX(e.run_id) AS run_id
        FROM shared_executive_edge e
        JOIN assets aa ON aa.id = e.asset_id_a
        JOIN assets ab ON ab.id = e.asset_id_b
        GROUP BY e.asset_id_a, e.asset_id_b, e.method
    """,
    "v_cycle_ranking": """
        CREATE VIEW v_cycle_ranking AS
        SELECT r.cycle_run_id, cr.cycle_type, cr.cycle_date, a.ticker, r.asset_id,
               r.rank, r.blended_score, r.components_json, r.vetoed, r.veto_rules_json,
               r.selected, r.target_weight, cr.status
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
    "v_cycle_ranking_component": """
        CREATE VIEW v_cycle_ranking_component AS
        SELECT r.cycle_run_id, r.asset_id, je.key AS score_type, je.value AS component_value,
               wc.weight AS configured_weight,
               wc.weight / NULLIF(SUM(wc.weight) OVER (PARTITION BY r.id), 0)
                   AS effective_weight
        FROM cycle_ranking r
        JOIN json_each(r.components_json) je ON je.value IS NOT NULL
        LEFT JOIN v_weight_component wc
               ON wc.cycle_run_id = r.cycle_run_id AND wc.score_type = je.key
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
          AND COALESCE(c.source, '') <> 'tiingo'
    """,
    "v_quant_return_daily": """
        CREATE VIEW v_quant_return_daily AS
        SELECT q.id, a.ticker, q.asset_id, q.obs_date, q.close_split_adj, q.adj_close,
               q.tr_index, q.cash_dividend, q.split_factor, q.price_log_return,
               q.tr_log_return, q.source, q.engine_version, q.computed_at,
               q.vendor
        FROM quant_return_daily q JOIN assets a ON a.id = q.asset_id
        WHERE q.engine_version = (
            SELECT q2.engine_version FROM quant_return_daily q2
            WHERE q2.asset_id = q.asset_id AND q2.obs_date = q.obs_date
            ORDER BY q2.computed_at DESC, q2.id DESC LIMIT 1
        )
          AND COALESCE(q.vendor, '') <> 'tiingo'
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
    "v_quant_risk_model": """
        CREATE VIEW v_quant_risk_model AS
        SELECT rm.id, rm.as_of, rm.model_version, rm.lookback_days, rm.min_history_days,
               rm.n_assets, rm.cov_estimator, rm.cov_shrinkage, rm.ret_estimator,
               rm.periods_per_year, rm.panel_engine_version, rm.panel_spec_json,
               rm.rf_annual, rm.computed_at, rm.quant_run_id, rm.manifest_json
        FROM quant_risk_model rm
    """,
    "v_quant_portfolio": """
        CREATE VIEW v_quant_portfolio AS
        SELECT qp.id, qp.as_of, qp.kind, qp.frontier_k, qp.objective, qp.solver, qp.status,
               qp.expected_return, qp.expected_vol, qp.sharpe, qp.rf_annual, qp.n_positions,
               qp.turnover, qp.target_param, qp.model_id, qp.engine_version, qp.computed_at,
               qp.manifest_json,
               CASE WHEN qp.engine_version GLOB 'opt-v[0-9]*'
                         AND substr(qp.engine_version || '+', 6, instr(qp.engine_version || '+', '+') - 6)
                             NOT GLOB '*[^0-9]*'
                         AND qp.id = (
                        SELECT q2.id FROM quant_portfolio q2
                        WHERE q2.as_of = qp.as_of AND q2.kind = qp.kind
                          AND q2.frontier_k IS qp.frontier_k
                          AND q2.engine_version GLOB 'opt-v[0-9]*'
                          AND substr(q2.engine_version || '+', 6,
                                     instr(q2.engine_version || '+', '+') - 6)
                              NOT GLOB '*[^0-9]*'
                        ORDER BY CAST(substr(q2.engine_version, 6) AS INTEGER) DESC,
                                 q2.computed_at DESC, q2.id DESC LIMIT 1)
                    THEN 1 ELSE 0 END AS is_current
        FROM quant_portfolio qp
    """,
    "v_quant_position": """
        CREATE VIEW v_quant_position AS
        SELECT p.id, p.portfolio_id, qp.as_of, qp.kind, a.ticker, p.asset_id,
               p.weight, p.valid_from, p.valid_to
        FROM quant_position p
        JOIN quant_portfolio qp ON qp.id = p.portfolio_id
        JOIN assets a ON a.id = p.asset_id
    """,
    "v_quant_frontier_point": """
        CREATE VIEW v_quant_frontier_point AS
        SELECT fp.id, fp.model_id, rm.as_of, fp.k, fp.target_return, fp.expected_return,
               fp.expected_vol, fp.sharpe, fp.status, fp.weights_json, fp.portfolio_id
        FROM quant_frontier_point fp
        JOIN quant_risk_model rm ON rm.id = fp.model_id
    """,
    "v_quant_benchmark_performance": """
        CREATE VIEW v_quant_benchmark_performance AS
        SELECT bp.id, bp.portfolio_id, qp.as_of, qp.kind, bp.date, bp.realized_return,
               bp.cumulative_return, bp.benchmark, bp.benchmark_return, bp.active_return,
               bp.engine_version, bp.computed_at
        FROM quant_benchmark_performance bp
        JOIN quant_portfolio qp ON qp.id = bp.portfolio_id
        WHERE bp.engine_version = (
            SELECT bp2.engine_version FROM quant_benchmark_performance bp2
            WHERE bp2.portfolio_id = bp.portfolio_id AND bp2.date = bp.date
            ORDER BY bp2.computed_at DESC, bp2.id DESC LIMIT 1
        )
    """,
    "v_quant_vs_live": """
        CREATE VIEW v_quant_vs_live AS
        SELECT qp.as_of, qp.kind, a.ticker, qpos.asset_id,
               qpos.weight AS benchmark_weight, pp.weight AS live_weight,
               COALESCE(qpos.weight, 0) - COALESCE(pp.weight, 0) AS active_weight,
               qp.engine_version, qp.is_current
        FROM v_quant_portfolio qp
        JOIN quant_position qpos ON qpos.portfolio_id = qp.id AND qpos.valid_to IS NULL
        JOIN assets a ON a.id = qpos.asset_id
        LEFT JOIN portfolio_position pp ON pp.asset_id = qpos.asset_id
             AND pp.valid_from <= qp.as_of
             AND (pp.valid_to IS NULL OR pp.valid_to > qp.as_of)
        WHERE qp.kind <> 'live_book'
        UNION ALL
        SELECT b.as_of, 'LIVE_ONLY' AS kind, a.ticker, pp.asset_id,
               NULL AS benchmark_weight, pp.weight AS live_weight,
               -COALESCE(pp.weight, 0) AS active_weight,
               NULL AS engine_version, 1 AS is_current
        FROM (SELECT DISTINCT as_of FROM quant_portfolio WHERE kind <> 'live_book') b
        JOIN portfolio_position pp ON pp.valid_from <= b.as_of
             AND (pp.valid_to IS NULL OR pp.valid_to > b.as_of)
        JOIN assets a ON a.id = pp.asset_id
        WHERE NOT EXISTS (
            SELECT 1 FROM v_quant_portfolio q2
            JOIN quant_position p2 ON p2.portfolio_id = q2.id AND p2.valid_to IS NULL
            WHERE q2.as_of = b.as_of AND p2.asset_id = pp.asset_id
              AND q2.kind <> 'live_book' AND q2.is_current = 1)
    """,
    # -- backtest engine (m018, WP-A). Every view carries knowledge_date; no view exposes
    #    one asset's price or return -- weights, alerts and portfolio aggregates only.
    "v_book": """
        CREATE VIEW v_book AS
        SELECT b.id AS book_id, b.backtest_run_id, r.window_id, r.d_start, r.d_end,
               r.formation_date, r.status AS run_status, b.arm_code, b.objective,
               b.exclude_hard_vetoed, b.guard_layers, r.code_version,
               substr(r.created_at, 1, 10) AS knowledge_date
        FROM strategy_book b
        JOIN backtest_run r ON r.id = b.backtest_run_id
    """,
    "v_book_rebalance": """
        CREATE VIEW v_book_rebalance AS
        SELECT br.id AS rebalance_id, br.book_id, b.backtest_run_id, b.arm_code,
               br.decision_date, br.execution_date, br.quant_portfolio_id, br.cycle_run_id,
               br.risk_model_id, br.status,
               br.execution_date AS knowledge_date
        FROM book_rebalance br
        JOIN strategy_book b ON b.id = br.book_id
    """,
    "v_guard_alert": """
        CREATE VIEW v_guard_alert AS
        SELECT ga.id AS alert_id, ga.backtest_run_id, r.window_id, ga.date, ga.layer,
               a.ticker, ga.asset_id, ga.rule_id, ga.value, ga.threshold, ga.state,
               ga.date AS knowledge_date
        FROM guard_alert ga
        JOIN backtest_run r ON r.id = ga.backtest_run_id
        LEFT JOIN assets a ON a.id = ga.asset_id
    """,
    "v_guard_action": """
        CREATE VIEW v_guard_action AS
        SELECT act.id AS action_id, act.book_id, b.backtest_run_id, b.arm_code,
               act.alert_id, act.decision_date, act.effective_date,
               a.ticker, act.asset_id, act.status, act.weight_before, act.weight_after,
               act.effective_date AS knowledge_date
        FROM guard_action act
        JOIN strategy_book b ON b.id = act.book_id
        LEFT JOIN assets a ON a.id = act.asset_id
    """,
    "v_book_weight_daily": """
        CREATE VIEW v_book_weight_daily AS
        SELECT w.book_id, b.backtest_run_id, b.arm_code, w.date, w.holding_key, w.asset_id,
               a.company_name AS holding_name,
               w.markowitz_target_weight, w.drifted_weight, w.pre_guard_weight,
               w.final_target_weight,
               w.date AS knowledge_date
        FROM book_weight_daily w
        JOIN strategy_book b ON b.id = w.book_id
        LEFT JOIN assets a ON a.id = w.asset_id
    """,
    "v_book_daily_performance": """
        CREATE VIEW v_book_daily_performance AS
        SELECT p.book_id, b.backtest_run_id, b.arm_code, r.window_id, p.date,
               p.gross_return, p.turnover, p.cost_return, p.net_return, p.nav, p.drawdown,
               p.cash_weight, p.n_equity, p.benchmark_return,
               p.date AS knowledge_date
        FROM book_daily_performance p
        JOIN strategy_book b ON b.id = p.book_id
        JOIN backtest_run r ON r.id = b.backtest_run_id
    """,
    "v_portfolio_period_summary": """
        CREATE VIEW v_portfolio_period_summary AS
        SELECT s.book_id, b.backtest_run_id, b.arm_code, r.window_id, s.segment,
               s.date_from, s.date_to, s.cumulative_return, s.annualized_return,
               s.annualized_vol, s.ex_post_sharpe, s.max_drawdown, s.total_turnover,
               s.total_cost_bps, s.n_rebalances, s.n_guard_actions,
               s.date_to AS knowledge_date
        FROM backtest_summary s
        JOIN strategy_book b ON b.id = s.book_id
        JOIN backtest_run r ON r.id = b.backtest_run_id
    """,
    "v_backtest_test": """
        CREATE VIEW v_backtest_test AS
        SELECT t.backtest_run_id, r.window_id, t.segment, t.arm_a, t.arm_b, t.test,
               t.statistic, t.p_value, t.n_obs, t.params_json,
               substr(r.created_at, 1, 10) AS knowledge_date
        FROM backtest_test t
        JOIN backtest_run r ON r.id = t.backtest_run_id
    """,
}


# The views no agent writes to any more: kept for back-compat, documented as frozen above and
# published as ``frozen`` by the API's ``/contract`` (T-152). Every name must be a key of VIEWS.
FROZEN_VIEWS: frozenset[str] = frozenset({"v_universe_membership"})


def schema_version(db: Database) -> int:
    """The highest recorded ``schema_version``; 0 when the table is missing or empty. Never
    creates the table (``kg_schema.queries.current_version`` does), so it is safe on a
    read-only connection. Any other read error propagates: guessing 0 would let the rebuild
    remove a newer contract's views."""
    if not db.relation_exists("schema_version"):
        return 0
    row = db.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"]) if row is not None and row["v"] is not None else 0


def ensure_views(db: Database) -> None:
    """Drop and recreate every read-contract view. Tolerates missing base tables.

    ``CREATE VIEW`` does not validate its base tables, so after creating each view
    we probe it with a zero-row ``SELECT``; a view whose base table is absent in
    this (partial / single-agent) DB is dropped rather than left dangling -- a
    dangling view would otherwise break the view re-parse that a later
    ``ALTER TABLE ... RENAME`` in :mod:`kg_schema.migrations` performs.

    Does nothing when the database's ``schema_version`` is above the highest migration
    this code knows (T-144): a later contract may have added columns and views this code
    does not define, and rebuilding would remove them. It protects later code from the next
    contract change; it cannot retrofit code that predates it.
    """
    known = max(ver for ver, _, _ in MIGRATIONS)
    found = schema_version(db)
    if found > known:
        log.warning(
            "schema_version %d is above the highest migration this code knows (%d): "
            "leaving the read-contract views as they are",
            found,
            known,
        )
        return
    for name, ddl in VIEWS.items():
        db.execute(f"DROP VIEW IF EXISTS {name}")
        try:
            db.execute(ddl)
            db.execute(f"SELECT 1 FROM {name} LIMIT 0")  # noqa: S608 - name is our own key
        except DatabaseError:
            db.execute(f"DROP VIEW IF EXISTS {name}")
    db.commit()
