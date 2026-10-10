# SPEC.md — `portfolio-financial-analysis`

Part of the thesis *"Sistema inteligente para la optimización de la inversión
en portafolios mediante integración de información financiera estructurada y
no estructurada de acciones del S&P500"* — Gabriel Jaime Múnera González &
Dovaribi Carupia Yagari, Universidad Pontificia Bolivariana (UPB). Referred
to elsewhere in this document and the architecture artifacts by its working
nickname, "Portfolio Thesis."

The technical contract for this repository: requirements, architecture, data
model, and acceptance criteria. Where `.specify/memory/constitution.md` is
the philosophy/principles/code-style layer this repo commits to regardless of
feature, this document is the "what, precisely" layer for the system it
implements — every requirement below should be traceable to a test, and every
design decision should be explainable by a principle in the constitution.
Link liberally: a design choice justified by, e.g., the constitution's
Technological stock or AI behavior principles is annotated `(constitution: …)`
below rather than re-argued here.

Requirement IDs (`FR-0xx` functional, `NR-0xx` non-functional) are stable —
don't renumber an existing one, even if it's later superseded; mark it
superseded in place instead. Reference them in commits/PRs/tests
(`test_rundate.py::test_no_lookahead  # NR-001`) so a reviewer can trace
implementation back to requirement and requirement back to test.

---

## 1. Overview & Purpose

`portfolio-financial-analysis` is the analytical/reasoning core of a
six-repository system (the "Portfolio Thesis") that builds and maintains an
S&P 500 portfolio on top of a knowledge graph. Data flows in one direction
through the system:

```
sources (Wikipedia/news/Finnhub/SEC EDGAR)
  → portfolio-data-mining      (acquisition: discovers URLs, extracts article text)
  → portfolio-nlp              (semantic layer: sentiment, entities, category, summaries)
  → portfolio-financial-analysis (THIS REPO — fundamentals/pricing/cycle/quant → scores)
  → portfolio-knowledge-graph   (RDF/OWL projection + SPARQL evidence surface)
  → portfolio-reports           (as-of run engine, per-name evidence, HTML report)
  → portfolio-app                (thin Streamlit client)
```

with one feedback edge running back up (a user-defined decision criterion,
compiled once in `reports` and propagated into `financial-analysis` and the
knowledge graph) — out of scope for this repo, noted here only for context.

**What this repo is for**: raw EDGAR filings, daily pricing bars, and a
point-in-time S&P 500 universe are not usable as portfolio-decision inputs on
their own. `portfolio-financial-analysis` turns them into structured,
per-asset scores and a rule-driven veto/ranking lane — deterministic ratio
analysis over SEC filings (`fundamental_agent`), a pricing collector
(`pricing_agent`), cross-sectional TECHNICAL/VALORIZATION/SECTOR scoring and
selection cycles with a T-1 contagion-lagged veto (`cycle`), shared-executive
entity edges from news co-occurrence (`entity_resolution`), and a Markowitz
mean-variance benchmark to grade the blended-score book against (`quant`) —
with per-row provenance (`run_id`, `as_of`, `code_version`) and a shared,
passive schema (`kg_schema`) so everything downstream (the knowledge-graph
repo, via `v_*` views; `portfolio-reports`/`portfolio-app`, via the read-only
`api/` package) can treat the result tables as a stable, auditable contract
rather than re-deriving meaning from raw filings/prices/news itself.

**What this repo is explicitly not**: it does not discover URLs, crawl pages,
or extract article text (`portfolio-data-mining`'s job); it does not compute
per-article NLP signals — sentiment, NER, category (`portfolio-nlp`'s job —
this repo consumes only the resulting `article_sentiment`/`article_category`
tables, and even that SEMANTIC aggregation is moving to be owned by
`portfolio-nlp` itself, see §13); it never emits RDF or performs SHACL
validation (`portfolio-knowledge-graph`'s job — this repo's `v_*` views are
what that repo reads); it renders no report or UI
(`portfolio-reports`/`portfolio-app`'s job); it makes no final portfolio or
trading decision — its outputs are scored, ranked, and veto-flagged inputs to
that downstream decision.

## 2. Scope & Requirements

### 2.1 In scope

- Six analysis packages over a shared `KG_FINANCIAL_DB`: `fundamental_agent`,
  `pricing_agent`, `cycle`, `entity_resolution`, `quant`, and the passive
  shared schema `kg_schema`, each idempotent/resumable and each taking an
  `--analysis-date` that bounds ingestion to a point-in-time as-of date.
- A point-in-time S&P 500 universe read from a companion `universe.db`
  (read-only), plus a `coverage` command reporting which as-of members
  actually have core data.
- A FastAPI layer (`api/`): read-only endpoints over the `v_*` read-contract
  views and `universe.db`, plus (from Work item 2) the run endpoints of the
  repo's single entry point for orchestrated and remote runs (the per-package
  CLIs stay).
- A rule-driven veto lane (`rule_catalog` → `veto`) with a T-1 contagion lag,
  and a Markowitz mean-variance benchmark book (`quant`) to grade the
  blended-score portfolio against.

### 2.2 Out of scope

- URL discovery / page crawling / article extraction (`portfolio-data-mining`).
- Per-article NLP signal computation — sentiment, NER, category, summaries
  (`portfolio-nlp`); this repo consumes NLP output, it doesn't produce it.
- RDF/OWL projection, SHACL validation, named-graph minting
  (`portfolio-knowledge-graph`) — this repo emits no RDF itself, only the
  `v_*` relational read contract that repo consumes.
- Report rendering or any UI (`portfolio-reports`, `portfolio-app`).

### 2.3 Functional requirements

| ID | Requirement | Acceptance criteria |
|---|---|---|
| **FR-001** | `fundamental_agent` pulls SEC 10-K/10-Q statements from the EDGAR gateway, computes deterministic ratios (profitability, liquidity, leverage, efficiency, growth, cash-flow, ROIC, CAGR, and — when a period-end price exists — valuation), and has a Strands metrics-master agent synthesize a `FundamentalAssessment` (0–100 score, bullish/neutral/bearish rating, narrative, strengths, risks), writing one immutable `score_snapshot[FUNDAMENTAL]` row per `(asset, form, fiscal_period)`. | A pipeline run on an unanalyzed filing writes exactly one `score_snapshot` row with `score_type='FUNDAMENTAL'` and one `fundamental_metrics` row; a re-run on the same filing — identified by `(asset, form, period end)`, never by the derived `fiscal_period` label (T-140) — writes no new row (skip, not overwrite); `--analysis-date` caps the year range and skips filings with a `filing_date` after it. The gateway's cover-page share count (`data["cover"]["shares_outstanding"]`, `dei:EntityCommonStockSharesOutstanding`, portfolio-data-mining T-043) is stored in `filing_cover_shares` (one row per class and date); `us-gaap_CommonStockSharesIssued` is never read as shares outstanding; the stored `valuation` metrics take the cover count, then the balance-sheet outstanding count, then (flagged) the weighted-average diluted count; a failed cover read (`data["cover"]["error"]`) is recorded and does not fail the filing (T-132, `tests/test_cover_shares.py`). The `cashflow` group is measured over the fiscal year on a 10-K and over the trailing twelve months on a 10-Q (a 10-Q's cash-flow statement is year-to-date only); the TTM uses only real `ytd`/`quarters` flows, never `quarter x 4`, so a HARD `NEGATIVE_FCF` never reads one quarter; capex also resolves oil & gas development spend and a single "capital expenditures" caption, and is absent rather than partial when a filer reports it in parts (T-133, `tests/test_cashflow_ttm.py`). A revenue total the gateway dropped (T-042: tagged only with a dimension) is rebuilt from the statement's own later "total ... revenue" line less the valued rows between the revenue section and it, by label only, with T-117's 25% per-row trust ceiling (an over-ceiling derivation is admitted only when the gateway's own `corrections[].original` for the dropped total equals it to the dollar; otherwise no value) (T-140, `tests/test_revenue_rebuild.py`). A filing is identified by its **period end**, never by the derived label: the resume unit is `(ticker, form, period_end)`; a 10-Q's quarter is counted from the filing's own balance-sheet fiscal year end (else the asset's latest stored 10-K), so no 10-Q is a Q4 and a 52/53-week filer's quarters do not repeat a label; a period ending in the first week of January labels as the year before; `upsert_filing` refuses a label already held by another period end rather than overwriting it; and a filing with no period of its own is recorded in `analysis_run_error` (`stage='period'`), not skipped silently (T-140, `tests/test_quarter_labels.py`). The engine stamps `metrics-v5`. |
| **FR-002** | If the LLM synthesis call returns nothing usable (the current DeepSeek endpoint doesn't accept OpenAI `response_format` JSON-schema), `fundamental_agent` falls back to a rule-based score derived from the same computed ratios rather than leaving the row unwritten -- distinguishably so, never stored under the LLM's own `model_id` (T-113). Every `score_snapshot[FUNDAMENTAL]` row, fallback or not, carries a `prompt_hash` identifying the *prompt version* (the prompt templates + model config, not any one filing's own transcript) that produced it -- constant across every filing scored under the same code/skills/model config, changing only when a prompt, skill SOP, or model config changes -- and the LLM is called at `temperature=0` (a `seed` passed optimistically) to reduce run-to-run variance, not a claim of guaranteed reproducibility; `analysis_run.fallback_units` counts a run's fallbacks. | A test that forces the LLM call to fail/return unparseable output still produces a `score_snapshot[FUNDAMENTAL]` row with a non-null `score`/`rating` (constitution: AI behavior #2), `model = fundamental_agent.agents.FALLBACK_MODEL_LABEL` (never the configured LLM's `model_id`), and a non-null `prompt_hash`; that run's `analysis_run.fallback_units` is incremented by exactly the number of such rows; two filings scored under the same run share one `prompt_hash`, and changing one specialist's prompt changes it. |
| **FR-003** | `fundamental_agent run --sections` additionally fetches narrative filing sections (MD&A, risk factors) from `www.sec.gov` and writes `sec_filing_section` rows tagged with a canonical `item_label`. | A normal (non-`--sections`) run leaves `sec_filing_section` untouched; `--sections` populates it with `item_label` values matching the ontology's `itemLabel` token set (`v_sec_filing_section`'s SQL-built label matches `fundamental_agent.sections.canonical_item_label`). |
| **FR-004** | `pricing_agent` is standalone (no `fundamental_agent` coupling) and, per ticker, makes one gateway call over the requested date range, writing a `price_window` summary (return, daily-return std-dev, annualized volatility, trading days, min/max close, average volume); `--store-daily` additionally persists every OHLCV bar to `price_daily`, and `--observations` derives `price_observation` analytics (ATR, realized vol, drawdown, momentum; since `priceobs-v2`, T-070, also `sma_200`, `ret_5d`, `vol_5d` and the mean and volatility of a 60-day baseline of daily log returns that ends 5 sessions before the date, each NULL until its window is full). | `pricing_agent run` produces exactly one `price_window` row per requested ticker/range; re-running an unchanged range writes no duplicate row; `--analysis-date`'s fetch upper bound clamps `--end` and drops any candle dated after it. Price ingestion integrity (T-131): a run whose end date is an NYSE session that has not closed and settled (16:00 ET + 1 h) is refused before anything is written; `price_observation` is derived from the asset's **full stored** `price_daily` history (an incremental refresh yields the observations of a full recompute, no NULL long-window fields) and a row is rewritten when its inputs change; a stored-plus-fetched series with a split-shaped close jump (x2, x0.5, x3 …) at the run's seam is re-fetched in full once if a `corporate_action` SPLIT explains it, and refused (not stored) if it survives or nothing explains it, unless `--allow-split-jumps`. |
| **FR-005** | `cycle` computes TECHNICAL, VALORIZATION, and SECTOR `score_snapshot` rows from the agents' existing tables (read via plain SQL, no cross-package import), cross-sectionally normalizes each score type (`cross_sectional_z` + `z_to_score`, clamped to `[0, 100]`), and rolls per-sector TECHNICAL means into `sector_aggregate_snapshot`. **TECHNICAL is version 2 (T-070, model `technical-v2`)**: `0.50·z(mom_12_1) − 0.30·z(realized_vol_90d) + 0.20·z(max_drawdown_90d)`, `mom_12_1 = close(t−21)/close(t−252) − 1` (Jegadeesh & Titman 1993; 253 closes), each signal standardized within the asset's GICS sector on the cycle date (population sd; a sector with fewer than 5 names with the signal uses the whole cross-section's mean and sd), renormalized over the signals present; an asset with fewer than 2 of the 3 signals gets **no TECHNICAL score** (audit C5), and the blend renormalizes over the components present. `cycle` reads exactly one `price_observation` engine version (`observation_engine_version`, default `priceobs-v2`), never "latest per day", and refuses when the table has rows but none at it. | For a cycle run over a non-degenerate cohort, every present asset has a normalized `score_snapshot` row per computed score type in `[0, 100]`; a cohort of size 1 (degenerate z-score) does not raise, it returns a defined neutral value. TECHNICAL v2 (`tests/test_technical_v2.py`): `mom_12_1` equals `close(t−21)/close(t−252) − 1` and is NULL below 253 closes; a sector of 5 names with the signal is standardized within itself, one of 4 against the cross-section; the blend is renormalized over the signals present; an asset below the 2-of-3 floor has no `TECHNICAL` row (it is not scored 50) and is still ranked; the cycle never reads a `priceobs-v1` row (`tests/test_price_vetoes.py`). |
| **FR-006** | `cycle` evaluates `rule_catalog` rules into `veto` **stints** (`raised_on`/`cleared_on`/`last_seen_on`, T-125) with a **T-1 contagion lag** applied to both raising and clearing: a stint active at cutoff `C` (`raised_on <= C AND (cleared_on IS NULL OR cleared_on > C)`) affects ranking for cycle `C+1` and later, never the cycle it was raised or cleared on. At most one open stint per `(asset_id, rule_id)`; a rule that could not evaluate an asset this cycle (missing data) leaves its open stint untouched rather than clearing or re-raising it. Vetoes are closed (`cleared_on` set), never deleted; a re-raise after a clear opens a new, distinct stint. `cycle` also refuses to write veto transitions at a cycle date older than the latest one already recorded (unless `--allow-backdated-veto`, in which case the veto step runs read-only instead -- it evaluates the rules but never calls the writer, since applying an older date's transitions on top of the shared `veto` table would delete or roll back later, still-current ones rather than replaying history; PR #103 review), and refuses to run at all when `data_quality_issue` (the Ring-1 `DQ_*` gate feeding the `DATA_QUALITY` veto) holds rows under an older gate version than the current one but none under it yet, unless `--allow-stale-dq-gate`, which records why on the run (T-116). **Price vetoes (T-070).** `BREAK_TREND_200` (SOFT: `close < 0.95·sma_200`); `VOLATILITY_SHOCK` and `CRASH_Z_SCORE` (HARD, *temporal*): `vol_5d/vol_60d_base > 2.5` and `(ret_5d − 5·mu_60d_base)/(vol_60d_base·√5) < −2.5`, **each also required to be extreme against the name's GICS sector that day** (`ratio > 2.5 ·` the sector's median ratio; `(ret_5d − ` the sector's median `ret_5d)/(vol_60d_base·√5) < −2.5`; a sector with fewer than 5 evaluated names, or no sector, uses the whole cross-section), so a move a whole sector makes together raises no HARD veto -- a declared limitation (systematic moves are carried by TECHNICAL and the risk model). A temporal stint opens with `expires_on = raised_on + 10 NYSE sessions` and **cannot clear before it** even when its condition is gone; at the first cycle on or after `expires_on` it clears if the condition no longer holds, and otherwise stays open with `expires_on` moved to that cycle + 10 sessions; the T-1 lag is unchanged, and with cycles further apart than 10 sessions the hold is effectively one cycle. An observation older than 7 days is not evaluated. A same-date re-run and `cycle backfill --force` restore `expires_on` exactly; `cycle undo-run` never touches `veto`. **`LIQUIDITY_DISTRESS`** (SOFT) fires only when `current_ratio < 1.0` **and** `interest_coverage < 1.5` or `operating_cash_flow_margin < 0`, and is not applied to GICS Financials or Utilities (until `T-071`'s company profile replaces the sector test). | A veto stint raised on cycle date `N` does not exclude the asset from `cycle_ranking`/`portfolio_position` computed for date `N` itself, but does for the next cycle run at `N+1`; a stint that clears on cycle date `M` (the first cycle its condition is evaluated and found false) still excludes the asset through `M`'s own ranking, only stopping at `M+1`. `kg_schema.queries.hard_vetoed_as_of`/`active_soft_vetoes` (the one point-in-time predicate `cycle` and `quant` both read) never return a stint whose `cleared_on` is at or before the query cutoff as still active; `active_soft_vetoes` returns exactly one entry per open stint regardless of how many cycles it has held, so `soft_veto_penalty` is charged once per active rule, not once per cycle. A gate-version bump (e.g. `dq-v1` -> `dq-v2`) without its one-time re-gate having run refuses the cycle rather than silently reading zero quarantines/HARD issues. Temporal stints (`tests/test_price_vetoes.py`): a stint raised on a cycle date has `expires_on` 10 NYSE sessions later; evaluated clean cycles before it leave it open, untouched; a hit before it does not move it; the first cycle on or after it clears a gone condition (T-1: active through the day before) or, when the condition holds, extends it by 10 sessions from that cycle; a same-date re-run and a replay reset put `expires_on` back exactly; each veto fires strictly past its threshold on both legs; `LIQUIDITY_DISTRESS` needs both conditions and never hits Financials/Utilities (the audit's PG, NEE, PM, T, STZ, APA and SBAC are no longer flagged). |
| **FR-007** | `cycle select`/`monitor` blends the present score types by `score_weights` (default FUNDAMENTAL, VALORIZATION and TECHNICAL at 1/3 each, `T-141`; SEMANTIC is removed from the defaults until Work item 4 writes it; the weights are renormalized over whatever of the weighted types are actually present for an asset, so a name missing one is blended from the other two at 1/2 each), applies `soft_veto_penalty` per active SOFT veto, excludes T-1 HARD-vetoed assets, ranks the cohort into `cycle_ranking`, and (`select` only) writes `portfolio_position` targets for N names (`--top-n`, default 30) under the `score_tilt` scheme (each weight in [0.5/N, 1.5/N], proportional to the score inside that band; the name cap is 1.5/N with no 0.10 floor, e.g. 0.05 at N = 30, 0.15 at N = 10, 0.50 at N = 3, and an explicit `--max-name-weight` still wins), with a sector cap of 0.30 and a sector-aware fill (a ranked name whose sector is full is skipped for the next one; a sector is full at `max(1, floor(sector cap × N))` names, N being the names actually held — 9 at N = 30, 3 at N = 10, 1 at N = 5 and N = 3, where the cap relaxes to 1/3 and the relaxation is recorded). An explicit `--max-name-weight` below 1/N cannot sum to 1: it is relaxed to 1/N and the relaxation is recorded, never silently and never as a failed run. The optional preferences are `--pin`, `--exclude`, `--exclude-sectors` and `--only-sectors`. The effective caps, relaxations, shortfall and preferences are recorded in `cycle_run.params_json`. *(T-134 decision; implemented by T-135–T-137, verified by T-138; PRs #117–#121; default weights and the weights refusal: T-141)* | Every `select` book sums to 1 and satisfies the band and both caps within 1e-9, or records the relaxation; `select --dry-run` is strictly read-only — it builds the book from the stored SELECTION (else MONITORING) ranking for the date, refuses with "run `cycle monitor` first" when there is none, and no `cycle_run`, checkpoint, ranking or position is written or modified; a writing `select` refuses the preference flags (`--pin`, `--exclude`, `--exclude-sectors`, `--only-sectors`: decision support, never the thesis book; `backfill` and `--dry-run` accept them); a resume with different construction settings (N, scheme, caps, preferences) or different `score_weights` is refused, never two books or two blends mixed (a run that recorded no weights is left alone; `params_json.score_weights`, which `v_weight_scheme`, `v_weight_component` and `v_cycle_ranking_component` report, is written once by the run's first attempt); a `monitor` run never writes `portfolio_position`; both are checkpointed via `cycle_run`/`cycle_checkpoint` and resumable — killing and re-running a cycle skips steps already marked `done`. |
| **FR-008** | `entity_resolution build` derives `sharedExecutiveWith` candidate edges from the data-mining repo's `urls.db` (news co-occurrence), reading it strictly read-only via `KG_NEWS_DB`. | `shared_executive_edge` rows are written only for pairs meeting `--min-weight`; no code path in `entity_resolution` opens `urls.db` for write; `--analysis-date` drives `cycle_run.cycle_date` and excludes news whose `pub_date` is after it or `NULL`. |
| **FR-009** | `quant` is a leaf package (imports only `kg_schema`; no other package imports `quant`) implementing a Markowitz mean-variance **benchmark** book, methodologically independent of the blended-score system: a dividend-folded total-return series, Ledoit-Wolf-shrunk covariance, an expected-return estimator (`equilibrium` default, `james_stein`, `hist_mean`, `carhart`), and five objectives (`min_var`, `risk_parity`, `tangency`, `target_vol`, `frontier`). The benchmark's caps follow N, the same rule as the thesis book's (T-137): the name cap is `1.5 / min(N, panel size)` (`--top-n`, default 30; an explicit cap below `1/n_held` is relaxed and recorded), the sector cap is 0.30 relaxed to the smallest feasible value and recorded, there is no limit on the number of names (no integer programming), every objective — `risk_parity` included — respects both effective caps, and the optimizer engine is `opt-v2`. All four estimators are **total returns** (`equilibrium` is `rf + delta*Sigma@w_mkt`, not the excess `delta*Sigma@w_mkt` alone, T-109), and every objective's Sharpe subtracts `rf` from that total exactly once. **`carhart` (T-077)** is `rf + Σ_k β^shrunk_i,k · λ̄_k` over `{Mkt-RF, SMB, HML, Mom}`: each asset's daily excess simple return (the panel's log returns through `expm1`, less French's daily RF) is regressed by OLS on Kenneth French's four daily factors, vendored byte-for-byte under `src/quant/data/` beside a manifest pinning the URL, download date, the library's version statement, SHA-256 and first/last dates. The loader refuses a file whose SHA-256 differs from the manifest, converts percent to decimals, and reads only rows dated on or before the as-of date. The betas are shrunk per factor (Vasicek 1973) toward their equal-weighted cross-sectional mean, with their cross-sectional variance as the prior variance, `w = σ² / (σ² + se²)`; an asset under 504 observations takes the prior mean and is flagged; `λ̄` is the arithmetic mean of each factor's daily return from 1963-07-01 to the as-of, × 252; the intercept is dropped. A panel running past the files' last date is regressed on the overlap and the gap recorded; an overlap under 504 dates leaves `carhart` unbuilt (the other three are built and the reason recorded in the model's manifest) and `optimize --mu carhart` refuses, as it does on a model with no carhart rows. The risk model is `rm-v2` (an `rm-v1` model holds none; the factor vintage is not in the model's key, so a new vintage needs a new `risk_model_version`, pinned by a test to "202608 CRSP"), and its `manifest_json` records the factor files' version and SHA-256, the regression range, `λ̄`, `β̄` and `σ²` per factor, the shrinkage-weight distribution and the flagged assets; `equilibrium` stays the default. **Turnover (T-077).** `optimize --turnover-cap C` (`C` in (0, 2], default off) bounds `Σ|w − w_prev|` against the previous book of the same chain — the same objective, `frontier_k`, estimator, optimizer engine, inputs and cap, the newest `as_of` before the book's — counting weight that book held outside today's panel; with no previous book there is no constraint; a cap below the smallest feasible turnover is relaxed to it and recorded, with the realized turnover, in the book's `params_json` (and `quant_portfolio.turnover`). `risk_parity` and the frontier sweep ignore the cap; `tangency` under a binding cap is the best-Sharpe point of a frontier scan. A book whose estimator is not `equilibrium`, or that has a cap, carries it as marks before the first `+` of its `engine_version` (`opt-v2.mu-carhart.to-0.5+<tag>`), so it never overwrites the default book and is never `is_current` (only the default configuration can be). A risk-free CSV refuses an as-of before its first date instead of using a later rate. | `tests/test_quant_import_isolation.py` passes (no non-`quant`/`api` module imports `numpy`/`scipy`/`cvxpy`); the cap rule `quant` copies from `cycle` (`quant.caps`) is pinned against `cycle`'s — name cap, sector cap and relaxations equal for the same inputs over a grid of N, panel sizes and sector mixes — by `tests/test_quant_caps.py`, every objective's book and every frontier point sums to 1 and respects both effective caps (or the relaxation is recorded), and `quant_run.params_json["caps"]` and each `quant_portfolio.params_json` carry `top_n`, `n_held`, the effective caps and the relaxations (T-137); `quant`'s `liquidity_data_gate` output is provably unaffected by the presence/absence of `score_snapshot`/`cycle_ranking` rows (`tests/test_quant_gate.py`); the gate counts history and median dollar volume at the **pinned** `return_engine_version` / `observation_engine_version` (the series the panel reads), never "latest per day", so a second engine version neither double-counts nor flips it (T-131, `tests/test_quant_t131.py`); each objective run persists a `quant_portfolio`/`quant_position` book plus its `quant_run` (`as_of`, `code_version`); `hist_mean`/`james_stein`/`equilibrium` agree on the total-return convention and a book's Sharpe is invariant to `rf` once its weights don't depend on `mu` (`tests/test_quant_risk_model.py`, T-109); a `frontier` sweep with no feasible return range above the min-variance point's own return (`mu` has no cross-sectional signal under the constraints) persists one `quant_frontier_point` row, `status = "degenerate"`, not `k` rows all stamped `"optimal"` as if `k` distinct solves had run (T-112); the `equilibrium` prior weights assets by **market cap as of the date** from the one shared reader `kg_schema.market_cap` (the latest cover-page share count usable that day × the close on or before it; a count older than 200 days or a close older than 10 is refused), and `build-risk-model` **refuses** when any panel asset has no cap, naming them, unless `--allow-missing-caps` (those assets weight 0, recorded); two listings of one issuer (same CIK) count once in the market portfolio; a class not 1:1 with the traded one (BRK's A = 1,500 B) is converted before the classes are summed — a panel with no cap at all always refuses — with the coverage, missing names and count ages recorded on `quant_run.params_json["market_caps"]` and the risk model (T-132, `tests/test_quant_market_caps.py`, `tests/test_market_cap.py`); the Carhart estimator is pinned by `tests/test_quant_factors.py` (OLS recovers known betas; the log-to-simple and percent-to-decimal conversions; Vasicek weights near 1 for `se² ≪ σ²` and near 0 for `se² ≫ σ²`, a flagged asset takes the prior mean, the mean shrunk beta is `β̄` under equal noise; no factor row after the as-of is read; the overlap refusal and the recorded gap; the SHA-256 refusal) and `tests/test_quant_carhart_pipeline.py` (`rm-v2` stores the rows and the manifest record, degrades with a recorded reason, a tampered file fails the build, `optimize --mu carhart` refuses an `rm-v1` model); turnover by `tests/test_quant_turnover.py` (previous-book selection, the constraint holds including a name that left the panel, the relaxation is recorded, `risk_parity` ignores it); the risk-free refusal by `tests/test_quant_rates.py` (T-077). |
| **FR-010** | `quant evaluate` computes forward realized and active return of a previously-optimized, frozen book against a chosen benchmark. From `perf-v3` (T-077) the realized and active returns are **net of a turnover cost** (10 bps, a constant: `perf-v3` means 10 bps by definition, another cost is a new perf version): `bps / 1e4 × Σ|w − w_prev|` on target (not drifted) weights, deducted once on each optimized book's first forward day, `w_prev` being the previous book of its chain and the first book of a chain paying `bps × Σ|w|`; the `live_book` snapshot and the internal benchmark pay none. `perf-v2` rows stay stored under their version. | `quant_benchmark_performance` rows carry `portfolio_id`, `date`, and realized/active return figures for every date in the requested `--from`/`--to` window with sufficient forward price data; a date lacking forward data is skipped, not fabricated. A name missing one date's forward return but present again later (a `price_daily` gap — its move is folded into the next date it reappears) contributes 0% that date with its weight kept, not diluted or renormalized away; a name with no later return anywhere in the window (delisted, or its series ends) is dropped and the remaining names' weights renormalized from that date on (T-111). The internal benchmark applies the same two-case convention and matches an independent equal-weight (mean of simple returns) calculation over the same gated panel to 1e-9; a loaded external benchmark is only read, never overwritten (T-108); under `perf-v3` the cost is deducted once, on the first forward day (the later days' returns are untouched), the first book of a chain pays `bps × Σ|w|`, a later one only its change, `perf-v2` rows stay stored and `v_quant_benchmark_performance` shows `perf-v3` (`tests/test_quant_turnover.py`, T-077). |
| **FR-011** | `kg_schema` (vendored at `src/kg_schema/`, not an external dependency — see §3) is the single schema entrypoint every package calls from its own `ensure_schema`: additive `CREATE TABLE/INDEX IF NOT EXISTS` DDL plus nullable `ADD COLUMN`s run unconditionally and safely against the shared production DB; non-additive migrations (widening a `CHECK`, renaming a column's semantic value, promoting a table to a view) run only via an explicit `python -m <agent> migrate`, advancing a monotonic `schema_version` floor other repos can assert against. **Contract 11 (T-070, `m011`)**: additive -- `price_observation.sma_200`/`ret_5d`/`vol_5d`/`mu_60d_base`/`vol_60d_base` and `veto.expires_on` (plus the bookkeeping `expiry_history_json`), appended at the end of `v_price_observation` and `v_veto`; existing rows keep NULL. | `kg_schema.ensure(db)` run twice in a row is a no-op the second time (no error, no duplicate DDL effect); `python -m fundamental_agent migrate` run twice is idempotent (the second run applies zero migrations); `schema_version` only ever increases. |
| **FR-012** | Every agent takes an optional `--analysis-date YYYY-MM-DD` (default: today) that selects the S&P 500 universe point-in-time from `universe.db` as of that date (predicate `valid_from <= D AND (valid_to IS NULL OR valid_to > D)`), bounds ingestion so nothing dated after it is written, and is recorded on the run-log row (`analysis_run`/`pricing_run`/`quant_run`/`cycle_run`) alongside a `code_version` git tag. | For a fixed `--analysis-date D`, no row written by that run has an `event_time`/`filing_date`/`pub_date`/obs date after `D`, and no fundamental value it reads (`cycle`'s metrics, data-quality verdicts, FUNDAMENTAL scores; the market cap `cycle` and `quant` both read from the shared `kg_schema.market_cap` reader) comes from a filing whose `available_at` — the first NYSE trading day after its `filing_date` — is after `D`, or that has none (T-106, T-107); every such row carries a non-null `available_at`, and no as-of reader filters them by `event_time`; the corresponding run-log row's `as_of` equals `D` and `code_version` is a non-empty git SHA/tag string. `quant`/`cycle` additionally refuse a `D` past the last date `price_daily` actually holds a bar for (the price spine), unless `--allow-stale-prices`, which records why on the run (T-110). Every agent (`fundamental_agent`/`quant`/`cycle`/`entity_resolution`/`pricing_agent`) additionally refuses to write its run-log row at all when its own `code_version()` is dirty, unless `--allow-dirty`, which records why on the run (T-114); "dirty" means an uncommitted change under `src/`, `skills/`, `pyproject.toml`, or `uv.lock` specifically (`kg_schema.provenance._DIRTY_SCOPE`), not any uncommitted file in the checkout — an untracked file outside that scope leaves `code_version` clean and is never refused over (T-114, PR #92 review). |
| **FR-013** | A `coverage` command (shared implementation in `kg_schema.cli`, exposed on `fundamental_agent`/`pricing_agent`/`quant`) reports, for the as-of universe, which members have core EDGAR/pricing/observation data, persisting one `universe_coverage` row per member; default behavior is warn (report + exit 0), `--strict` exits 1 below `--min-fraction`. | `python -m quant coverage --analysis-date D` upserts exactly one `universe_coverage` row per `(D, universe, symbol)`; `--strict` with `--min-fraction 1.0` against a universe with any uncovered member exits non-zero. |
| **FR-014** | `api/` exposes the `v_*` read-contract views and the point-in-time universe over HTTP (`/api/v1/health`, `/health/db`, `/runs`, `/universe`, `/universe/coverage`, `/scores`, `/portfolio/positions`, `/portfolio/ranking`, and, from `T-152`, `/contract` and `/contract/database`). The two **contract** routes publish the `v_*` view contract as metadata, never rows: every view of `kg_schema.views.VIEWS` in order with its `frozen` flag and columns in order, built from the code, plus `contract_version` (the highest migration the code knows); and the connected database's `schema_version` with the views present and missing. Within one `contract_version`, columns are only added. These **read endpoints** open every database `mode=ro` and never trigger an agent run. From Work item 2 (decided 2026-10-06, constitution 2.0.0) `api/` is also the repo's single entry point for orchestrated and remote runs (the per-package CLIs stay) and adds **run endpoints** — the orchestrator and each of its steps — which are the only `api/` code that may start a run or open a write-capable connection; they live in router module(s) separate from the read routers. The run-status (poll) endpoint only reads provenance, so it is a **read** endpoint: `GET`, `mode=ro`, in a read router. | Every `KG_FINANCIAL_DB`/`universe.db` connection opened by the read routers is `mode=ro` (`grep` for a write-capable `connect(` call in the read routers' modules returns none, and no read router imports a run router); a request against a view whose base table doesn't exist in a partial DB returns an empty list, not a `500`. The run endpoints' acceptance criteria are `PLAN.md` Work item 2's "Run endpoints" bullets, verified by `T-019`. `/contract` lists exactly the views and columns an in-memory build of `VIEWS` has, in order (a test fails on a view missing from it), and its `contract_version` equals the highest migration (`T-152`). |
| **FR-015** | **Deferred (Work item 20, after `T-100`; this repo is not yet a production deployment).** The `api/` run endpoints (FR-014) require access control: the run routers are mounted only when `API_ENABLE_RUNS` is set and require a bearer token from the environment (`API_RUN_TOKEN`), refusing every call when none is configured; the read endpoints are unaffected. The mechanism is the proposed shape and is confirmed when Work item 20 starts. Until this lands, the run endpoints are for the dev container / pilot only and must not be reachable from an untrusted network (the API binds `0.0.0.0` by default). | With `API_ENABLE_RUNS` unset, every run route is absent (`404`); with it set and no `API_RUN_TOKEN`, a run call is refused; a missing or wrong token is rejected (`401`) and nothing is started or written; a correct token runs. A read endpoint answers identically with and without these settings. Covered by `T-146`. |

### 2.4 Non-functional requirements

| ID | Requirement | Acceptance criteria |
|---|---|---|
| **NR-001** | No lookahead: a source row (filing, price candle, news article, universe membership) dated after a run's `--analysis-date` must never be written or read as if current. | `tests/test_rundate.py` and the per-agent coverage/gate tests assert no written row's date column exceeds the run's `as_of`; a synthetic fixture with a future-dated filing/candle/article is excluded, not ingested. |
| **NR-002** | `quant`'s numeric dependencies (numpy, scipy, cvxpy, clarabel) never load on any other package's import path. | `tests/test_quant_import_isolation.py` is green — importing `fundamental_agent`, `pricing_agent`, `cycle`, `entity_resolution`, `api`, or `kg_schema` does not import `numpy`/`scipy`/`cvxpy`. |
| **NR-003** | Every package connects to `KG_FINANCIAL_DB` through one shared convention (`kg_schema.db.connect`/`connect_ro`, itself wrapping `portfolio_common.db.Database`): `foreign_keys=ON`, a reapplied `busy_timeout`, and **no WAL** (the DB may sit on a bind mount with unreliable `-shm` support); `universe.db`/`urls.db` are opened `read_only=True`. | `grep -rn "PRAGMA journal_mode=WAL"` returns nothing under `src/`; every non-test `sqlite3`/`Database` connection to `KG_FINANCIAL_DB` in `src/` goes through `kg_schema.db.connect`, not a bare driver call. |
| **NR-004** | A DB-engine-contract change happens only through `portfolio-common`'s DB-engine-only surface (`Database`/`Dialect`/`in_clause`/`Allowlist`), git-tag-pinned in `[tool.uv.sources]` — never a floating version, and never a second SQL driver imported directly. As of the `v1.2.1` re-pin, no non-test module under `src/` imports `sqlite3`. | `grep -rn "import sqlite3" src` (excluding tests) returns nothing; `pyproject.toml`'s `portfolio-common` source is a `git`+`tag` pin (currently `v1.2.1`), not a bare version range; a remaining SQLite-specific fragment (DDL dialect, `INSERT OR IGNORE`/`ON CONFLICT`, `json_extract`/`json_each`) is SQL text routed through `conn.dialect.*`, not a driver import. |
| **NR-005** | The `api/` package degrades gracefully, never with a server error, when the underlying schema is partial (a package's tables/views haven't been created yet in a fresh or single-agent DB). | A request to any `/api/v1/*` endpoint against a `KG_FINANCIAL_DB` missing the queried view's base table returns `200` with an empty result, not `500` (`kg_schema.views.ensure_views` already drops a view whose base table is absent; `api/` must not assume the view exists). |
| **NR-006** | The hermetic test suite requires no live network access to the EDGAR gateway, the pricing gateway, `www.sec.gov`, or the LLM endpoint. | `uv run pytest` passes with `tests/fixtures/*.json` (real, captured EDGAR responses) and monkeypatched HTTP/LLM clients standing in for every external call — no test opens a live socket. |
| **NR-007** | Every write to a measurement/versioned table is append-only with a re-run collision on the same `*_version`/natural key silently skipped, not overwritten in place — **except** the two series that are pure functions of stored prices, `price_observation` and `quant_return_daily` (T-131): a collision whose derived values differ is rewritten (the inputs changed — a corrected bar, a split re-adjustment), one whose values are identical is a no-op. | `quant_return_daily` (`engine_version`), `price_observation` (`engine_version`), `corporate_action` (`engine_version`) all use `UNIQUE`-constrained upserts (`INSERT OR IGNORE` for `corporate_action`; `ON CONFLICT … DO UPDATE … WHERE <a value differs>` for the two derived series); re-running the same command twice on unchanged input produces zero new/changed rows the second time. |

## 3. Technology Stack & Architecture Decisions

Full stack and rationale: `.specify/memory/constitution.md` §Technological
stock. Summary for traceability:

- **Runtime**: Python `>=3.12,<3.13`, `uv`-managed (`uv.lock` committed).
- **Analytical core**: deterministic ratio/statistics code across every
  package, plus one narrow LLM step — a Strands metrics-master agent in
  `fundamental_agent` that synthesizes a narrative assessment over
  already-computed ratios. No trained model, no `transformers`/`torch`.
- **Numeric leaf**: `quant`'s Markowitz benchmark (numpy/scipy/cvxpy/
  clarabel) — the repo's only heavy numeric dependency, import-isolated.
- **Serving**: FastAPI + `uvicorn`, read endpoints read-only and run endpoints
  write-capable (FR-014), `pydantic` request/response models; `httpx` for the EDGAR/pricing gateway calls.
- **Storage**: SQLite, one shared `KG_FINANCIAL_DB` via `kg_schema.db`
  (wrapping git-tag-pinned `portfolio_common.db.Database`, `v1.2.1`) — no raw
  `sqlite3` driver usage outside that seam (NR-004); the remaining
  SQLite-specific SQL (DDL dialect, `INSERT OR IGNORE`/`ON CONFLICT`, `json_*`
  in views) is routed through `portfolio_common.db`'s `Dialect`, not a driver
  import.

Architecture decisions this repo has already made and should not be
re-litigated without a constitution amendment:

- **The domain schema is vendored, not a dependency.** `src/kg_schema/`
  owns DDL/migrations/views/provenance/universe-reads/coverage directly;
  `portfolio-common` supplies only the engine-agnostic `Database`/`Dialect`/
  `in_clause`/`Allowlist` primitives underneath it. (History: this schema
  lived in `portfolio_common.kg_schema` before `portfolio-common`'s v1.0.0
  DB-engine/business-logic split pushed it back here — see
  `docs/portfolio-common-v1-migration-plan.md` — and the v1.2.1 re-pin then
  removed every non-test `import sqlite3` from `src/`, routing what stays
  SQLite-flavoured through `conn.dialect.*` instead — see
  `docs/portfolio-common-v1.2-engine-agnostic.md`.)
- **`pricing_agent` has zero imports to/from `fundamental_agent`.** Both are
  standalone; `cycle`/`entity_resolution` sit above them and read their
  tables via plain SQL, never by importing the producing package.
- **`quant` is a leaf, not a dependency of anything.** It reads shared
  tables/views via plain SQL and imports only `kg_schema`, so its numeric
  stack never loads on another package's import path (FR-009, NR-002); its
  universe/liquidity gate is deliberately blind to `score_snapshot`/
  `cycle_ranking` so the benchmark stays an independent control.
- **The universe is read point-in-time from an external `universe.db`, not
  frozen locally.** `universe_membership`/`v_universe_membership` in this
  repo's own DB are frozen (no longer written); every agent resolves its
  as-of cohort from the data-mining repo's `universe.db` instead (FR-012).
- **This repo emits no RDF.** It produces rich, append-only, provenance-
  tagged relational rows and a set of `v_*` read-contract views;
  `portfolio-knowledge-graph` owns triple emission, SHACL validation, and
  named-graph minting from those views.

## 4. System Architecture

```mermaid
flowchart TB
    EDGAR[("EDGAR gateway<br/>SEC statements")]
    SECGOV[("www.sec.gov<br/>narrative filing text")]
    PRICING[("pricing gateway<br/>daily OHLCV + corporate actions")]
    URLSDB[("urls.db (ro)<br/>KG_NEWS_DB")]
    UNIVDB[("universe.db (ro)<br/>KG_UNIVERSE_DB<br/>point-in-time S&P 500")]

    FUND["fundamental_agent<br/>ratios + Strands LLM synthesis"]
    PRICE["pricing_agent<br/>standalone · price_window/_daily/_observation"]
    ER["entity_resolution<br/>sharedExecutiveWith"]
    CYCLE["cycle<br/>score · veto (T-1) · rank · positions"]
    QUANT["quant<br/>Markowitz benchmark · leaf"]
    KGS[("kg_schema<br/>DDL · migrations · v_* views · connect()")]
    DB[("KG_FINANCIAL_DB (SQLite)")]
    API["api/<br/>read-only FastAPI :8010"]
    KG(["portfolio-knowledge-graph<br/>consumes v_* → RDF (downstream)"])

    EDGAR --> FUND
    SECGOV -.->|"run --sections"| FUND
    PRICING --> PRICE
    URLSDB -->|read-only| ER
    UNIVDB -.->|as-of universe| FUND
    UNIVDB -.-> PRICE
    UNIVDB -.-> ER
    UNIVDB -.-> CYCLE
    UNIVDB -.-> QUANT

    FUND -->|append-only writes| DB
    PRICE -->|append-only writes| DB
    ER -->|append-only writes| DB
    CYCLE -->|"SQL read of FUND/PRICE tables"| DB
    QUANT -->|"SQL read of v_* only"| DB
    KGS -.->|ensure_schema, DDL, migrations, views| DB

    DB -->|"v_* read-contract views"| API
    UNIVDB -.-> API
    API --> KG
    DB -.->|"v_* views (direct read, no RDF here)"| KG
```

**Reading this diagram**: `fundamental_agent` and `pricing_agent` are
independent (no cross-import); `cycle` sits above both, reading their tables
by plain SQL; `quant` is deliberately isolated — it touches only `kg_schema`
and the `v_*` views, so numpy/cvxpy never load on the agents' import path.
Every agent takes `--analysis-date` and resolves its as-of cohort from
`universe.db`; `kg_schema` is the one shared schema/connection seam every
package calls into. The `v_*` views are the one contract
`portfolio-knowledge-graph` depends on; everything behind them can change,
and the `api/` read endpoints serve those same views over HTTP for
`portfolio-reports`/`portfolio-app`.

Full detail, with hover tooltips per component, the shipped/partial/critical
status of each piece, and the gap/plan lists this document's §13/`PLAN.md`
draw from: [the repository
artifact](https://claude.ai/code/artifact/bfc6efde-aecd-4408-83b8-081bc3abccb0).
System-level placement of this repo among the other five: [the architecture
overview](https://claude.ai/code/artifact/d3865a63-2894-4e20-b38a-7e50cf0d4040).

## 5. Data Model

Canonical DDL: `src/kg_schema/ddl.py` (`ADDITIVE_DDL`, `REQUIRED_COLUMNS`).
Every table below lives in the one shared `KG_FINANCIAL_DB`; `assets` /
`sectors` are identity tables created only if missing (never overwritten) and
are not owned by any single package.

| Table | Key | Notable columns | Written by |
|---|---|---|---|
| `score_snapshot` | `UNIQUE(asset_id, score_type, event_time)` | `score_type ∈ {FUNDAMENTAL, VALORIZATION, TECHNICAL, SEMANTIC, SECTOR}`, `raw_value`, `normalized_score`, nullable `forensic_flags_json` (T-041); `event_time` is what the score is *about* (FUNDAMENTAL: the period end); `available_at` (FUNDAMENTAL only, required, T-107) is when it became usable — its filing's; nullable `prompt_hash` (FUNDAMENTAL only, T-113) | `fundamental_agent`, `cycle`, (SEMANTIC: external — see §13) |
| `fundamental_metrics` / `financial_facts` | `UNIQUE(..., engine_version)` | per-group ratio outputs, each with its filing's required `available_at` (T-107); raw EDGAR facts with `event_time`/`ingested_at`/`filing_version`, nullable `correction_rule` (`financial_facts` only, T-118) — the gateway's own `data["corrections"]` rule id (e.g. `"T-118"`) when a value was derived rather than filed; `NULL` for a filed fact | `fundamental_agent` |
| `filing_cover_shares` | `UNIQUE(filing_id, class_member, as_of_date)`, `ON DELETE CASCADE` to `sec_filings` | the filing's own cover-page share count (`value > 0`, `as_of_date`); `class_member = ''` for a single class or a filed total | `fundamental_agent run` (T-132) |
| `sec_filing_section` | `UNIQUE(filing_id, section_type, ordinal, engine_version)` | narrative text, `item_label` (ontology `itemLabel` token) | `fundamental_agent run --sections` |
| `price_window` / `price_daily` | natural key per ticker/range; `price_daily` per bar | return, daily-return std-dev, annualized vol; OHLCV | `pricing_agent` |
| `price_observation` | `UNIQUE(asset_id, obs_date, engine_version)`; derived from the asset's full stored `price_daily`, rewritten only when a value differs (T-131) | ATR, realized vol, drawdown, momentum; `priceobs-v2` (T-070): `sma_200`, `ret_5d`, `vol_5d`, `mu_60d_base`, `vol_60d_base` (NULL until the window is full; v1 rows keep NULL) | `pricing_agent run --observations` |
| `rule_catalog` | `rule_id` PK | veto rule definition (metric/operator/threshold) | `cycle` (`seed_catalog`) |
| `data_quality_issue` | `UNIQUE(filing_id, metric_group, metric_name, metric_engine_version, rule_id, gate_version)` | Ring-1 `DQ_*` gate hit: `severity` (HARD → `cycle` `DATA_QUALITY` veto), `quarantined` (metric read as NULL), gated value | `fundamental_agent` (per filing + `quality` backfill) |
| `veto` | `UNIQUE(asset_id, rule_id, raised_on)`, partial unique `(asset_id, rule_id) WHERE cleared_on IS NULL` | `severity` (HARD/SOFT); `raised_on`/`cleared_on`/`last_seen_on` are stint cycle dates (T-125), `cleared_on IS NULL` = open, closed never deleted; `detected_at`/`cleared_at` are wall-clock metadata only; `expires_on` (T-070) is a temporal veto's earliest clearing cycle date, NULL otherwise, with `expiry_history_json` its bookkeeping | `cycle` |
| `portfolio_position` | `UNIQUE(asset_id, valid_from)` | position stints, weight | `cycle select` |
| `portfolio_position_replay` | `UNIQUE(asset_id, valid_from)` | `portfolio_position`'s own shape, isolated from it (T-115) | `cycle backfill` |
| `cycle_ranking` | `UNIQUE(cycle_run_id, asset_id)` | ranked cohort of a cycle | `cycle` |
| `cycle_run` / `cycle_checkpoint` | `UNIQUE(cycle_type, cycle_date)` / `UNIQUE(cycle_run_id, step)` | orchestrator provenance + resume; `cycle_type` incl. `'REPLAY'` (`cycle backfill`, T-115) | `cycle` |
| `sector_aggregate_snapshot` | `UNIQUE(sector_id, cycle_date, metric_type)` | per-cycle GICS-sector roll-up of members' TECHNICAL score | `cycle` |
| `shared_executive_edge` | `UNIQUE(asset_id_a, asset_id_b, person_name, method)` | `sharedExecutiveWith` candidate | `entity_resolution build` |
| `media_cooccurrence` | `UNIQUE(asset_id_a, asset_id_b, person_name, method)` | `shared_executive_edge`'s shape for non-executive (press/analyst) co-occurrence (T-043) | `entity_resolution build` (once T-082 lands) |
| `universe_membership` | `UNIQUE(asset_id, universe, valid_from)` | **frozen** — superseded by `universe.db` | not on any write path |
| `universe_coverage` | `UNIQUE(as_of, universe, symbol)` | per-member core-data coverage for a dated universe | `coverage` command |
| `corporate_action` | `UNIQUE(asset_id, action_type, ex_date, engine_version)` | dividends/splits (pricing gateway only; legacy XBRL-derived rows remain as history, unread) | `quant backfill-actions` |
| `quant_return_daily` | `UNIQUE(asset_id, obs_date, engine_version)`; a row is rewritten when its values change, a no-op otherwise (T-131) | total-return daily series, dividend-folded | `quant build-returns` |
| `risk_free_rate` / `benchmark_series` | `UNIQUE(curve, rate_date, engine_version)` / `UNIQUE(benchmark, obs_date, engine_version)` | rf curve + benchmark index | `quant` |
| `quant_risk_model` / `quant_expected_return` / `quant_covariance` | `UNIQUE(as_of, model_version)` / … | Markowitz μ / Σ per as-of model; `model_version` carries the run's input-manifest tag (T-090) | `quant build-risk-model` |
| `quant_portfolio` / `quant_position` / `quant_frontier_point` | `UNIQUE(as_of, kind, frontier_k, engine_version)` / … | optimized benchmark books + frontier | `quant optimize` |
| `quant_benchmark_performance` | `UNIQUE(portfolio_id, date, engine_version)` | forward realized/active return of a frozen book, against `SP500_EW_INTERNAL` (`bench-v2`: the mean of simple returns over the same liquidity/history gated panel, but never a book's own hard-veto exclusion, T-108) or a loaded external series | `quant evaluate` |
| `analysis_run` / `analysis_run_error`, `pricing_run` / `pricing_run_error`, `quant_run`, `cycle_run` | run PK | `run_id`, `as_of`, `code_version`, params, status | every agent |

**Read-contract `v_*` views** (documented in `src/kg_schema/views.py`) are
what `portfolio-knowledge-graph` consumes — the physical schema above can
evolve underneath them: `v_score_snapshot`, `v_sector`, `v_industry`,
`v_price_observation`, `v_sec_filing`, `v_sec_filing_section`, `v_veto`,
`v_rule_catalog`, `v_data_quality_issue`, `v_portfolio_position`, `v_shared_executive_edge`,
`v_cycle_ranking`, `v_weight_scheme`, `v_weight_component`,
`v_sector_aggregate_snapshot`, the `quant`-side `v_quant_*`/`v_corporate_
action`/`v_risk_free_rate`/`v_benchmark_series`/`v_quant_vs_live`, and the
run-log views `v_analysis_run`/`v_pricing_run`/`v_quant_run`/`v_cycle_run`/
`v_universe_coverage`. `v_universe_membership` is **frozen** — downstream
readers should move to `universe.db`/`kg_schema.queries` instead.

**Non-additive migrations** (`src/kg_schema/migrations.py`, `m001`–`m009`)
advance a monotonic `schema_version` floor other repos can assert against —
notably `m004` folded the legacy `fundamental_snapshot` table into
`score_snapshot[FUNDAMENTAL]` behind a compatibility view, and `m006` renamed
`score_type='QUANTITATIVE'` to `'VALORIZATION'` everywhere it's persisted
(the CHECK, stored rows, and `cycle_run.params_json`/`cycle_ranking.
components_json` keys), `m008` backfilled `available_at` on `sec_filings`,
`fundamental_metrics` and FUNDAMENTAL scores and put its guards in place (T-107),
and `m009` collapsed `veto`'s old per-(asset, rule, cycle_date) hit rows into
the `raised_on`/`cleared_on`/`last_seen_on` stints FR-006 now describes (T-125).
Full detail: `docs/kg_schema.md`.

## 6. Core Workflows

**Fundamental analysis** (`fundamental_agent run`): pull EDGAR statements for
the as-of universe → compute deterministic ratios per group → the Strands
metrics-master agent consults specialists over those ratios (or a rule-based
fallback if the LLM call fails) → write one `score_snapshot[FUNDAMENTAL]` +
`fundamental_metrics` row per `(asset, form, fiscal_period)`, skipping
filings already analyzed (resume is keyed on the period end, T-140); `--sections` additionally fetches and stores
narrative filing text from `www.sec.gov`.

**Pricing collection** (`pricing_agent run`): for each as-of-universe ticker,
one gateway call over the requested range → a `price_window` summary;
`--store-daily`/`--observations` add `price_daily`/`price_observation`.
Standalone — no dependency on `fundamental_agent`'s output.

**Selection/monitoring cycle** (`cycle select`/`monitor`): read the agents'
existing tables via plain SQL → compute TECHNICAL/VALORIZATION/SECTOR
`score_snapshot` rows → cross-sectionally normalize → evaluate
`rule_catalog` into `veto` rows (T-1 lag) → blend present score types →
rank into `cycle_ranking` → (`select` only) derive `portfolio_position`
targets. Checkpointed via `cycle_run`/`cycle_checkpoint` so a killed run
resumes from the last completed step rather than restarting.

**Entity resolution** (`entity_resolution build`): read the news repo's
`urls.db` (`KG_NEWS_DB`, strictly read-only) → derive `sharedExecutiveWith`
candidate edges from executive co-occurrence above `--min-weight`.

**Quant benchmark** (`quant backfill-actions` → `build-returns` →
`build-risk-model` → `optimize` → `evaluate`): pull corporate actions from the
pricing gateway (the only source; a gateway that cannot serve fails the run)
→ build a dividend-folded total-return series → gate the universe on
liquidity/history/T-1-hard-veto (score-blind) → estimate Σ (Ledoit-Wolf) and
μ (equilibrium/James-Stein/hist_mean) → optimize one or more objectives →
persist the book → later, evaluate its forward realized/active return
against a chosen benchmark.

**Coverage check** (`fundamental_agent`/`pricing_agent`/`quant coverage`): a
standalone pre-run check reporting which as-of universe members have core
EDGAR/pricing/observation data. It reads the data tables but persists its
result to `universe_coverage` (FR-013), so it is not a read-only operation.

**Serving** (`api/`): a read request opens `KG_FINANCIAL_DB`/`universe.db`
`mode=ro` and reads the `v_*` views / point-in-time universe directly — read
endpoints never trigger an agent run. From Work item 2 (decided 2026-10-06)
`api/` is also this repo's single entry point for orchestrated and remote runs
(the per-package CLIs stay): separate run endpoints start the orchestrator or
any one step (see FR-014), and are the only part with write access.

## 7. Business Logic & Algorithms

- **Fundamental ratios** are plain, code-computed arithmetic over EDGAR
  facts (profitability, liquidity, leverage, efficiency, growth, cash-flow,
  ROIC, CAGR); **valuation** (equity/enterprise FCF yield, an SBC-adjusted
  variant, market cap, enterprise value) additionally needs a period-end
  market price — it reads the latest `price_daily` close on or before the
  filing's period-end with a plain `SELECT` (no import of `pricing_agent`);
  without price rows the group is silently skipped and every other metric is
  unaffected. Its share count is the cover-page count, else the balance
  sheet's outstanding count, else the weighted-average diluted count
  (flagged) — never `CommonStockSharesIssued` (T-132). The market cap that
  `cycle` and `quant` consume is **not** this stored metric but
  `kg_schema.market_cap`'s as-of value (latest usable cover count × the close
  on the date, split-basis adjusted, refused when stale). Individual ratio specifications are documented per-skill under
  `skills/<ratio>/SKILL.md` (`roic`, `cagr`, `fcf_margin`,
  `free_cash_flow_yield`, `interest_coverage_ratio`).
- **LLM synthesis** (`fundamental_agent`'s Strands metrics-master) consumes
  only the already-computed ratios, never raw filing text, to produce the
  narrative `FundamentalAssessment` — the LLM cannot see anything the
  deterministic layer didn't already compute (constitution: AI behavior #1).
  It parses a JSON reply rather than using `Agent.structured_output` because
  the current DeepSeek endpoint doesn't accept OpenAI `response_format`
  json-schema; a rule-based score derived from the same ratios is the
  fallback when that parse fails (constitution: AI behavior #2).
- **Cross-sectional normalization** (`cycle.scores.normalize`):
  `cross_sectional_z` winsorizes (2% by default) then z-scores against the
  live cohort (returns zeros for a degenerate/size-1 cohort rather than
  raising); `z_to_score` maps `z` to `clamp(50 + 10z, 0, 100)`.
- **TECHNICAL score** (version 2, T-070): `0.50·z(mom_12_1) − 0.30·z(realized_vol_90d)
  + 0.20·z(max_drawdown_90d)`, each z taken within the asset's GICS sector on the
  cycle date (the cross-section when the sector has fewer than 5 names with the
  signal), renormalized over the signals present, no score under 2 of the 3
  (`docs/cycle.md`; Jegadeesh & Titman 1993 for the 12-1 momentum).
- **Veto evaluation**: most rules in `rule_catalog` are a single flat
  threshold comparison (`param_metric`/`param_operator`/`param_threshold`),
  not an AND/OR clause tree — a simplification versus the system ontology's
  `RuleClause` (see §13); three are not: `LIQUIDITY_DISTRESS` (the current
  ratio and a cash-coverage test), `VOLATILITY_SHOCK` and `CRASH_Z_SCORE` (an
  absolute and a sector-relative leg, both required — T-070, FR-006). A HARD
  veto excludes an asset from selection as of the day *after* it fires (T-1
  contagion lag), and a temporal one (the two price shocks) is held until its
  `expires_on`; a SOFT veto subtracts `soft_veto_penalty` (15 pts default)
  from the blended score instead of excluding.
- **Portfolio blend**: `score_weights` (FUNDAMENTAL, VALORIZATION and
  TECHNICAL at 1/3 each by default, `T-141`; SEMANTIC is not weighted until
  Work item 4 writes it — see §13) are renormalized over whichever score
  types are actually present for an asset before blending — a name missing
  one component is not silently scored a third lower, the remaining weights
  absorb that share (1/2 each). All three components are winsorized
  cross-sectional z-scores mapped to `50 + 10z` and clamped to `[0, 100]`
  (`cycle/scores/normalize.py`), so equal weights are equal weights on one
  scale; `docs/cycle.md` records the known effects.
- **Markowitz benchmark** (`quant`): Ledoit-Wolf (2004) linear-shrinkage
  covariance toward a constant-correlation target, always symmetrized and
  eigenvalue-floored before the optimizer sees it; expected returns from one
  of `equilibrium` (reverse-optimized from cap weights — the default,
  because it carries real cross-sectional dispersion with near-zero
  estimation noise), `james_stein`, or `hist_mean` (both of which, over ~5y
  of daily data, shrink toward a flat mean and collapse the frontier onto
  `min_var` — see §13's critical gap). The universe/liquidity gate reads no
  score/ranking data, keeping the benchmark an independent control
  (`tests/test_quant_gate.py`).
- **Total-return construction**: `tr_log_return_t = ln((C_t + D_t) /
  C_{t-1})`, dividends sourced **only** from the pricing gateway
  (`corpact-v1`; `portfolio-data-mining`'s yfinance-backed
  `GET /pricing/{ticker}/actions`) — `quant` mines nothing itself and has no
  filing-derived fallback, so a gateway that cannot serve fails
  `backfill-actions` rather than degrading (see §13 and `PLAN.md` Work item 10).

## 8. Error Handling & Resilience

Governing principle: **fail loudly for structural problems, log-and-skip for
per-item data gaps that don't invalidate the rest of the run**. Concretely:

- A missing/empty universe resolution (`active_universe`/
  `load_universe_asset_ids` finding nothing for the as-of date) raises
  loudly rather than silently falling back to "all assets" — the silent
  fallback that used to exist in `quant` has been removed.
- Per-ticker/per-filing failures (a dead ticker, an unparseable filing) are
  recorded in `analysis_run_error`/`pricing_run_error` and do **not** stop
  the batch — a `tqdm` progress bar keeps running and the run completes with
  a partial result plus a visible error log, not a silent gap or a full
  abort.
- The `valuation` metrics group is skipped (not errored) when no
  period-end price row exists — every other metrics group is computed
  regardless.
- `coverage`'s default mode is **warn** (report + exit 0); only `--strict`
  turns an under-covered as-of universe into a hard failure — the intent is
  visibility by default, a CI/ops gate only when explicitly requested.
- `kg_schema.views.ensure_views` probes each view with a zero-row `SELECT`
  right after creation and drops it if its base table is absent in this
  (partial/single-agent) database — a package that hasn't run yet must not
  leave a dangling view that later breaks a migration's `ALTER TABLE …
  RENAME` re-parse.
- Non-additive migrations (`kg_schema.migrations`) each run inside one
  transaction, are guarded (check the table exists / columns are present /
  the migration hasn't already run), and record their version only on
  success — a partially-applied migration cannot silently masquerade as
  done.

## 9. Performance & Scalability Expectations

This repo has no throughput/latency SLA, and defining one is out of scope
(§14) — a real-time or high-volume performance target belongs to a
production system this project isn't. What is enforced by design:

- **Import isolation** (NR-002): `quant`'s numeric stack never loads outside
  `quant`/`api`'s own boot path, so the other five packages' cold-start cost
  is unaffected by numpy/scipy/cvxpy being installed.
- **Resumability over raw speed** (FR-006, FR-007): `cycle`'s checkpointed
  runner and every agent's skip-already-processed idempotency mean a slow or
  interrupted run costs wall-clock time on re-run, not correctness — no
  throughput number is asserted or needed for that guarantee to hold.
- **No articles/sec, filings/sec, or API-latency number is stated anywhere
  in this document** — none has been measured, and inventing one with no
  load test behind it would be worse than stating plainly that none exists
  (§14).

## 10. Testing Strategy & Acceptance Criteria

- **Hermetic by construction** (NR-006): `tests/conftest.py` and
  `tests/fixtures/*.json` (real, captured EDGAR gateway responses) build
  minimal SQLite fixtures and monkeypatch every outbound HTTP/LLM call — no
  test opens a live network socket.
- **Coverage mapping**: `test_kg_schema.py`/`test_kg_queries.py`/`test_db.py`
  exercise the shared schema/connection seam (FR-011, NR-003/NR-004);
  `test_pipeline.py`/`test_metrics*.py`/`test_statements.py`/
  `test_edgar_client.py`/`test_sections.py`/`test_provenance.py` cover
  `fundamental_agent` (FR-001/002/003); `test_pricing_*.py` covers
  `pricing_agent` (FR-004); `test_cycle.py` covers `cycle` (FR-005/006/007);
  `test_entity_resolution.py` covers `entity_resolution` (FR-008);
  `test_quant_*.py` (returns basis, LW covariance, objectives, optimize,
  panel, gate, import isolation, config, DDL, rates, state redaction) covers
  `quant` (FR-009/010, NR-002); `test_coverage.py`/`test_rundate.py` cover
  the point-in-time universe + `--analysis-date` contract (FR-012/013,
  NR-001); `test_api.py` covers `api/` (FR-014, NR-005); `test_skills.py`
  covers the `skills/*/SKILL.md` ratio specifications.
- **New requirement → new test first** (constitution: Code & Git #1's
  enforced-not-advisory spirit) — a change implementing or altering an
  FR/NR above should land with a test that references the requirement ID in
  a comment or test name.
- **Acceptance criteria in §2.3/§2.4 are the test spec** — each row should
  be directly expressible as one or more `pytest` assertions; a PR claiming
  to satisfy an FR/NR without a corresponding test is incomplete.
- **Structural tests are architecture tests, not feature tests**:
  `test_quant_import_isolation.py` failing means the leaf-package boundary
  (constitution: Technological stock #2) was violated, not that a feature
  regressed — treat it accordingly in review.

## 11. Deployment Procedures

There is no formal CD pipeline for this repo yet; what exists:

1. `uv sync --group dev`.
2. Configure `.env` (or real environment) with `KG_FINANCIAL_DB` (required
   by every package) and, as needed per package, `KG_UNIVERSE_DB` /
   `KG_NEWS_DB` / `LLM_API_KEY` / `LLM_MODEL` / `LLM_URL` /
   `EDGAR_BASE_URL` / `PRICING_BASE_URL` / `API_HOST` / `API_PORT` /
   `API_ROOT_PATH` — README.md's configuration table is the source of truth
   (no `.env.example` is committed today, see constitution: Project
   structure #6).
3. Run whichever package CLI the operator needs, in the order the data
   dependencies imply (`pricing_agent`/`fundamental_agent` first for
   `cycle`'s inputs, `cycle` before reading its output, `quant` any time
   after `pricing_agent` — no scheduler or orchestrator is wired in today,
   see §13) and/or run `uv run python -m api` as a long-lived read-only
   service.
4. CI gate before merge (`.github/workflows/ci.yml`, `master`/PRs): two
   parallel jobs — `quality` (`uv sync --frozen --group dev` → ruff check →
   ruff format --check → `pre-commit run --all-files`, which includes
   mypy) and `tests` (`uv sync --frozen --group dev` → `pytest -q`).

## 12. Dependencies & Integrations

- **Upstream (required, external services)**: an EDGAR gateway (SEC
  financial statements; `portfolio-data-mining`'s `sec_edgar` service) and a
  pricing gateway (daily OHLCV, and `quant`'s only source of dividends/splits;
  `portfolio-data-mining`'s pricing service),
  both defaulting to `host.docker.internal:8000`; `www.sec.gov` directly for
  `fundamental_agent run --sections`; an OpenAI-compatible LLM endpoint
  (`LLM_API_KEY`/`LLM_MODEL`/`LLM_URL`, today DeepSeek) for the metrics-
  master synthesis.
- **Execution order across the data repositories**: `portfolio-data-mining` →
  `portfolio-nlp` → `portfolio-financial-analysis`. This repo's orchestrator
  (Work item 2) assumes `portfolio-nlp` has already run for the same date;
  it only checks for its output, never triggers it, and does not enforce the
  order across repositories. **Inert until Work item 4 lands**: until then this
  repo reads no `portfolio-nlp` output (`PLAN.md` Work item 2), so nothing has
  to wait for an nlp run. The architecture artifacts follow this statement.
- **Upstream (required, read-only data)**: `universe.db` (`KG_UNIVERSE_DB`,
  `portfolio-data-mining`'s point-in-time S&P 500 membership — every agent's
  as-of universe source, FR-012); `urls.db` (`KG_NEWS_DB`,
  `portfolio-data-mining`'s news database, read only by `entity_resolution`
  for co-occurrence, FR-008); `portfolio-nlp`'s `article_sentiment`/
  `article_category` tables, today read directly by the integration repo for
  SEMANTIC scoring (moving — see §13).
- **Upstream (library)**: `portfolio-common`, git-tag-pinned in
  `pyproject.toml` (`[tool.uv.sources]`, currently `v1.2.1`) — DB-engine-only
  (`Database`/`Dialect`/`in_clause`/`Allowlist`); a re-pin is an explicit,
  reviewed change, never a floating version (NR-004). This repo is "Phase 4"
  of a six-repo engine-agnostic rollout tracked in `portfolio-nlp`'s
  `docs/engine-agnostic-rollout.md`; `portfolio-data-mining`'s Phase 5 is
  the one piece still outstanding system-wide.
- **Downstream (consumers, read-only via `v_*` views and/or `api/`)**:
  `portfolio-knowledge-graph` (RDF projection + SHACL validation, no RDF
  emitted here); `portfolio-reports`/`portfolio-app` (via the read-only
  `api/` package, a stable network boundary instead of opening the SQLite
  files directly).
- **No dependency on**: any repo downstream of this one
  (`knowledge-graph`, `reports`, `app` never call into this repo except
  through `v_*`/`api/`); `portfolio-data-mining` internals beyond the three
  seams above.

## 13. Open Questions & Risks

Carried forward from the last recorded architecture review
([the repository artifact](https://claude.ai/code/artifact/bfc6efde-aecd-4408-83b8-081bc3abccb0))
and this document's own drafting — resolve or explicitly accept before
treating a related FR/NR as done:

1. **`quant`'s μ has no cross-sectional signal** (critical). Expected
   returns estimated from ~5 years of daily data have a standard error far
   larger than the true cross-sectional spread, so both return-aware
   estimators (`james_stein`, `hist_mean`) collapse mean-variance to
   minimum-variance in practice; `equilibrium` is the accepted default
   precisely because it avoids this, but the frontier/`tangency`/
   `target_vol` objectives are not yet return-aware in a defensible sense.
   Not started.
2. **Survivorship bias — root cause fixed, residuals remain.** The universe
   is now read point-in-time from `universe.db` as of `--analysis-date`
   (this repo's own `universe_membership` is frozen), so a run gates to the
   constituents that were actually in the index then. Residual gaps:
   `price_daily` starts 2022 with no delisted-name backfill; EDGAR itself
   isn't point-in-time (restatements can leak into a historical read); and
   the fact/score store is keyed by `(asset, form, fiscal_period)` — not
   partitioned by `as_of` — so it cannot hold parallel point-in-time
   snapshots for the same filing read at two different dates. `coverage`
   flags as-of members with no core data but doesn't solve the underlying
   partitioning gap.
3. **No cross-module orchestrator.** `pricing_agent` → `fundamental_agent`
   → `entity_resolution` → `cycle` → `quant` are sequenced by hand today;
   there is no single `run --analysis-date D` that drives all five with
   checkpointing the way `cycle` itself is checkpointed internally.
4. **The SEMANTIC score is an external boundary, not yet cut over.** The
   schema accepts `score_snapshot[SEMANTIC]` and `cycle` already reads it
   (`latest_semantic_score`; no longer in the default blend weights, `T-141`), but the per-`(asset, day)`
   aggregation from `portfolio-nlp`'s `article_sentiment`/`article_category`
   is designed, not built, in either repo — today the integration
   (knowledge-graph) repo is still the nominal writer, which this repo's own
   `docs/README.md` and `docs/semantic-score-boundary.md` both flag as a
   dependency cycle to remove (the proposed shape: `portfolio-nlp` computes
   and owns the signal, this repo reads it read-only via a new `KG_NLP_DB`
   and materializes its own `score_snapshot[SEMANTIC]` row, the
   knowledge-graph repo stops writing it). Full worry list, placement map,
   and open sub-questions: `docs/semantic-score-boundary.md`. Mitigation
   until cut over: SEMANTIC is out of the default blend weights (`T-141`),
   so its absence is never read as a zero score.
5. **Dividends come only from the pricing gateway (live and verified 2026-09-21, `T-052`).**
   `quant` does not mine or derive corporate actions: acquiring data is
   `portfolio-data-mining`'s job, and its yfinance-backed
   `GET /pricing/{ticker}/actions` (that repo's `PLAN.md` Work item 3) is built
   and, as of 2026-09-21, deployed and verified from here (`T-052`): production
   `corporate_action` holds 7,481 gateway rows for all 503 assets. `build-returns`
   must follow a successful `backfill-actions` (a series built without
   dividends locks in as price-only); and since `T-086` (2026-09-21) it refuses to run otherwise. The consumer (`PLAN.md` Work item 10 / `T-085`) fails the
   run when the gateway cannot serve, and gives an asset the gateway cannot serve
   no rows. Upstream is yfinance: unofficial, no SLA, and it cannot tell an unknown
   symbol from a name that paid nothing. The retired XBRL-derived engines
   (`corpact-v0-approx`, `corpact-v1-derived`) left history in `corporate_action`
   that `quant` no longer reads. No vendor risk-free curve or index series is
   loaded yet either (the tables/CSV loaders exist, unpopulated).
6. **Vetoes are flat threshold rules, not an AND/OR clause tree.**
   `rule_catalog` has no analogue of the system ontology's `RuleClause`
   composite structure — every rule is a single leaf comparison (§7).
7. ~~The `portfolio-common` pin lags the artifact's recorded upstream
   state~~ — **resolved 2026-09-05, PR #32** (`refactor/engine-agnostic`,
   commit `8b1fd14`). `pyproject.toml` now pins `tag = "v1.2.1"`; every
   non-test `import sqlite3` under `src/` is gone (`sqlite3.Row` → the
   neutral `Row`, `except sqlite3.OperationalError` → `except
   DatabaseError`, `PRAGMA`/`sqlite_master` introspection →
   `table_columns`/`relation_exists`/`relation_ddl`, `executescript` →
   `create_schema`, the missing-columns loop → `ensure_columns`). What
   remains SQLite-flavoured (the DDL dialect, `INSERT OR IGNORE`/`ON
   CONFLICT ... DO UPDATE`, `json_extract`/`json_each` in the `v_*` views)
   is held as SQL text routed through `conn.dialect.*`, not a driver
   import — full record: `docs/portfolio-common-v1.2-engine-agnostic.md`.
   Kept here, struck through, only so the item number stays stable.
8. **`fundamental_agent`'s LLM synthesis has no accuracy measurement.**
   Unlike `portfolio-nlp`'s LLM-as-judge evaluation subsystem, there is no
   labelled or judged check on the metrics-master's narrative/rating quality
   — its correctness rests on the deterministic ratios underneath it plus
   the rule-based fallback (FR-002), not on a measured accuracy baseline.

## 14. Scope Boundaries (Out of Scope, Not Deferred)

**This repository is a thesis/research artifact. Productizing it is not a
goal of this project and no production phase is planned.** Every requirement
and acceptance criterion above (§2–§13) describes and governs that scope
honestly — nothing above should be read as an implicit production-readiness
claim. The items below are **permanently out of scope as this project is
currently defined**, not a backlog or a roadmap; they exist so a reader
doesn't mistake "not built" for "overlooked."

### What this stage validates

Per the design rationale in §7 and the acceptance criteria in §2.3/§2.4,
this repo currently validates:

- **Structural correctness** of the point-in-time universe contract (FR-012,
  NR-001 — no-lookahead is a tested guarantee, not an assumption) and of
  `quant`'s score-independence (FR-009, `tests/test_quant_gate.py`).
- **Idempotency/resumability** of every batch package and of `cycle`'s
  checkpointed runner (FR-006/007/013), exercised by the test suite (§10).
- **Import-graph isolation** of `quant`'s numeric stack (NR-002), enforced
  by a dedicated structural test rather than convention alone.
- **Deterministic-computation correctness** of the fundamental ratios and
  the Markowitz estimators (§7) — not the *quality* of the LLM narrative
  synthesis layered on top, which has no accuracy baseline (§13 item 8).

### What this project explicitly does not do (out of scope)

None of the following exist today, none are assumed by any FR/NR above, and
none are planned — this list is here so that absence reads as a deliberate
boundary of what this project is, not a gap someone forgot to close:

- **Access control**: the `api/` service has no authentication or
  authorization — every `/api/v1/*` endpoint is open to anyone who can reach
  it. Acceptable only because the service is expected to run on a
  private/trusted network with a single operator, not because it's been
  assessed as safe for broader exposure.
- **Operational tooling**: no monitoring/alerting, no on-call runbook, no
  documented disaster-recovery procedure for `KG_FINANCIAL_DB`, no scheduler
  or cross-module orchestrator (§13 item 3).
- **Throughput/latency SLAs and load testing** (§9) — a production
  requirement this project doesn't have.
- **A precise, vendor-sourced total-return/corporate-actions series, a
  factor-based μ estimator, and a `RuleClause`-style veto tree** (§13 items
  1/5/6) — accepted approximations at this scope, not oversights.

### §13 items: disposition

| §13 item | Disposition | Would only matter if |
|---|---|---|
| 1 — μ has no cross-sectional signal | **Open, critical** — the return-aware objectives are not yet defensible; see `PLAN.md` | Someone reads `tangency`/`frontier`/`target_vol` output as return-informed today |
| 2 — survivorship residuals | Root cause fixed (point-in-time universe); residuals accepted at current scope, tracked in `PLAN.md` | A historical replay needed pre-2022 delisted-name price history or point-in-time EDGAR |
| 3 — no cross-module orchestrator | Accepted for now, being actively worked (see `PLAN.md`) | Manual sequencing became error-prone at higher run frequency |
| 4 — SEMANTIC boundary uncut | Accepted, designed, not yet built — full plan in `docs/semantic-score-boundary.md` | SEMANTIC re-enters the blend weights or `portfolio-nlp` ships the aggregation stage |
| 5 — gateway-only dividends, no rf/index series | Closed for dividends: the consumer is `PLAN.md` Work item 10 / `T-085` (done), verified live by `T-052` (2026-09-21). rf/index series accepted as approximations | yfinance's accuracy mattered, or a symbol yfinance does not recognise paid a dividend (it returns a clean empty result) |
| 6 — flat veto rules | Accepted; sufficient for the current rule set | The rule set needed genuine AND/OR composition to express a policy |
| 7 — ~~`portfolio-common` pin lags upstream~~ | **Resolved (2026-09-05, PR #32)** — `v1.2.1` re-pin landed, no `import sqlite3` remains under `src/` | — |
| 8 — no LLM synthesis accuracy measurement | Accepted; the rule-based fallback and deterministic ratios are the load-bearing correctness guarantee, not the narrative | The narrative/rating output itself became a scored input rather than context |

## 15. Sign-off

This SPEC.md is the technical contract implementers, reviewers, and (per
`.specify/memory/constitution.md`'s AI behavior section) coding agents plan
against. A change that adds/removes a functional capability, alters an
acceptance criterion, or introduces a new external dependency should update
the relevant `FR-0xx`/`NR-0xx` entry (or add a new one) **in the same PR**
that implements it — not as a follow-up. A PR that contradicts this document
without amending it here first is out of spec; raise the conflict rather
than silently diverging (constitution: Governance).

| Role | Name | Date | Notes |
|---|---|---|---|
| Author | Gabriel Jaime Múnera González | | Universidad Pontificia Bolivariana (UPB) |
| Author | Dovaribi Carupia Yagari | | Universidad Pontificia Bolivariana (UPB) |
| Reviewer | Camilo Andrés Soto Montoya | | Universidad Pontificia Bolivariana (UPB) |

**Version**: 1.6.0 | **Last Amended**: 2026-10-10 (FR-004, FR-005, FR-006, FR-011, §3 data model and §7 algorithms: `priceobs-v2`, TECHNICAL v2, the three price vetoes and their temporal semantics, the recalibrated `LIQUIDITY_DISTRESS`, `m011` / contract 11, `T-070`). 1.5.0, 2026-10-10 (FR-009: the `carhart` estimator, `rm-v2`, the turnover cap with its previous-book chain, and the risk-free refusal; FR-010: `perf-v3` net of the turnover cost, `T-077`). 1.4.0, 2026-10-10 (FR-007: equal composite weights, SEMANTIC removed from the defaults, and the `score_weights` resume refusal, `T-141`). 1.3.0, 2026-10-09 (FR-014: the view-contract routes `/contract` and `/contract/database`, `T-152`). 1.2.0, 2026-10-06 (FR-014 and the new deferred FR-015, §2.1, §2.2, §4 "Serving" and "Coverage check", §12 execution order: run endpoints, T-018). Amendments between 2026-09-12 and 2026-10-06 (e.g. FR-001, FR-007, FR-012) were not versioned; 1.2.0 is the first bump since 1.1.0 and covers them too.
