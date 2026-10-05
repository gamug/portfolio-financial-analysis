# `fundamental_agent/`

Fundamental economic analysis of S&P 500 SEC filings (10-K annual, 10-Q quarterly,
fiscal years ≥ 2022 by default). Per filing: pull statements from the EDGAR gateway
→ compute deterministic ratios → a Strands *metrics-master* agent consults
specialist sub-agents → a synthesis step produces a scored assessment → one
immutable `score_snapshot` row (`score_type='FUNDAMENTAL'`) per
`(asset, form, fiscal_period)`. Optionally also extracts narrative filing text.

```bash
uv run python -m fundamental_agent run [--analysis-date 2021-06-30] [--tickers AAPL,NVDA] \
    [--forms 10-K] [--since-year 2023] [--fresh] [--universe-db PATH] [--sections] [--allow-dirty]
uv run python -m fundamental_agent quality [--metrics-version metrics-v3]  # Ring-1 DQ gates (backfill)
uv run python -m fundamental_agent repair-accessions [--apply] [--drop-unresolved]  # T-120
uv run python -m fundamental_agent migrate        # shared-schema migrations
```

`--analysis-date YYYY-MM-DD` (optional, default: today) is the run's as-of date:
the universe is read from `universe.db` as of that date, filings with
`filing_date` (or `period_end`) after it are skipped, and `resolved_until()` is
capped at its year. It is written to `analysis_run.as_of` alongside
`analysis_run.code_version` (git short SHA via `kg_schema.provenance`), and every
`sec_filings` / `financial_facts` / `fundamental_metrics` / `score_snapshot` /
`sec_filing_section` row the run writes carries its `run_id`. `--refresh-universe`
is a deprecated no-op (membership is synced from `universe.db` every run).

`run` refuses to write its `analysis_run` row at all when its own `code_version()` is dirty
(uncommitted changes) — its results would come from code `HEAD` alone can't reproduce
(T-114) — unless `--allow-dirty`, which records why on `analysis_run.params_json`.

## Configuration (`config.py`)

`Settings.load()` requires `KG_FINANCIAL_DB` (via `kg_schema.env.database_path`,
which also still honours the old misspelled `KG_FINANTIAL_DB`), `LLM_API_KEY`,
`LLM_MODEL`, `LLM_URL`. Optional: `KG_UNIVERSE_DB` (via
`kg_schema.env.universe_database_path`; default `/workspaces/thesis/data/universe.db`),
`EDGAR_BASE_URL` (default `http://host.docker.internal:8000/edgar/edgar` — the
doubled `/edgar` is intentional), `SEC_USER_AGENT`.

## Files

### `edgar_client.py` — `EdgarClient`

Blocking HTTP client for the gateway. Endpoints: `/company_info/{t}`,
`/years_available/{t}?form=`, `/filing_by_year/{t}?form=&year=`,
`/financials/{t}?form=&year=[&accession_number=]`. Every response is a
`{"success", "data"}` envelope, unwrapped by `_unwrap`. Retries 500/502/503/504 +
transport errors (3 attempts, exp backoff ≤ 8 s); 404 or "Company not found" →
`EdgarNotFoundError`. `normalize_ticker` yields spelling candidates (`BRK.B` → `BRK-B`
→ `BRKB`).

**Multi-filing contract (T-092).** Since `portfolio-data-mining` PR #39, `year` is the
*filing* year and one year can hold several filings of a form (three 10-Qs).
`filing_by_year` returns a **list** of `FilingRef(form, filing_date, accession_number)`,
most recent first, and an empty list — not an error — when there is none; an
object-shaped (pre-#39) payload raises `EdgarError` rather than being guessed at.
`financials` takes an optional `accession_number`, required whenever the year holds more
than one filing of the form: without it the gateway answers `success: false` ("Found 3
'10-Q' filings … Available: …"), surfaced as `EdgarAmbiguousError(candidates=[…])`. It also
raises a plain `EdgarError` when `data["reconciliation_errors"]` (T-042, `portfolio-data-
mining` PR #46) is non-empty — a statement whose own reconciliation failed comes back
rendered-but-unvalidated, so the filing is treated as a failed unit (retried next run, no
facts written) rather than risk an unvalidated value reaching `financial_facts` looking
filed. The exception is an entry whose `statement` is `"cover"` (the cover-page share count,
read independently upstream, `portfolio-data-mining` T-043): the statements are still validated,
so the filing is analysed, `Statements.cover_error` carries the message, and the pipeline records
it as a `cover` run error (T-132).

### universe source

The Wikipedia scraper is gone. `pipeline._load_members` reads
`kg_schema.queries.members_asof(universe_conn, analysis_date)` — a
point-in-time query over `universe.db` — and `db.sync_universe` upserts those
`UniverseMember`s into `assets` / `sectors` (the identity-write path where a new
S&P 500 symbol first gets its `assets.id`). No `universe_membership` /
`reconcile` write anymore.

### `statements.py` — `Statements`, `iter_facts`

`Statements.from_payload` parses `income_statement` / `balance_sheet` /
`cash_flow`. Duration columns look like `"2023-09-30 (FY)"` / `"(Q3)"` / `"(YTD)"`;
**balance-sheet rows use bare instant dates** and resolve against the
nearest-earlier instant. `REGISTRY` maps ~25 line items to US-GAAP tags with
standard/label fallbacks. Revenue prefers an aggregate (`total_concepts`: `Revenues`,
`RevenuesNetOfInterestExpense` for banks, `RegulatedAndUnregulatedOperatingRevenue` for
utilities — T-102) over summing its named components, subject to T-095's too-small
plausibility floor and T-117's too-large label-total contradiction check (a later,
label-matched "total revenue" row in the same statement that materially disagrees — detected
structurally, no filer's own concept ever named — corrected by subtracting the rows between
the two, or rejected with no guess if those rows are too large to trust). Where no `total_concepts` row has a value at all (the
gateway's `T-042` drops a total a filer tags only with a dimension: APA 2021Q1-2023Q3), `rebuild_total` (T-140) rebuilds it from the
statement's own later "Total revenues and other" line less the valued rows between the revenue section and it, by label only, with
`T-117`'s 25% trust ceiling (an over-ceiling row is admitted only when the gateway's own `corrections[].original` for the dropped total
equals the result to the dollar) -- else no value. `Statements.prior_of` pairs a quarter with the quarter column that ended a year
before it, by date (a 52/53-week filer's tags drift), and any other period with the same-tag column one step earlier. `iter_facts` flattens
every non-abstract numeric cell for `financial_facts`, tagging a cell named in the gateway's own
`data["corrections"]` (T-118) with that entry's `rule` id as `correction_rule` -- keyed by
`(statement, concept, column)`, not `concept`/`column` alone, since `T-042` corrects a concept
like `NetIncomeLoss` independently on more than one statement -- everything else gets `None`
(filed as-is). `Statements.corrections` holds the raw list (`[]` for a payload from a gateway
version that predates it).

`Statements.cover_shares` (T-132) is the payload's `data["cover"]["shares_outstanding"]` — the
filing's own count of shares outstanding (`dei:EntityCommonStockSharesOutstanding`) with its
`as_of_date` and `class_member` (`""` for a single class or a filed total). An entry without a
positive value or a date is dropped. The pipeline stores them in `filing_cover_shares`
(`db.insert_cover_shares`, idempotent), and `kg_schema.market_cap` reads them back as of a date.

### `metrics/` — one module per group

Each exposes `GROUP` and `compute(stmts, period_key, prior_key=None) ->
list[MetricResult]`. `MetricResult(name, value, unit, inputs)` — `unit ∈
{"ratio","pct","x","usd"}`. `base.py` has `safe_div` (None / near-zero guard),
`present`, `sum_present`.

| Module | Ratios |
|---|---|
| `profitability` | gross/operating/net margin, ROA, ROE |
| `liquidity` | current, quick, cash ratio |
| `leverage` | debt/equity, debt/assets, interest coverage, net-debt/EBITDA |
| `efficiency` | asset / inventory / receivables turnover |
| `growth` | YoY revenue / operating income / net income / FCF growth (needs prior period) |
| `cashflow` | OCF margin, FCF margin, FCF conversion, capex intensity; `free_cash_flow()` helper. Fiscal-year columns on a 10-K, **trailing twelve months on a 10-Q** (real `ytd`/`quarters` flows only, never the `x4` fallback; a flow missing from the TTM leaves its ratios empty) — a 10-Q's cash-flow statement is year-to-date only (T-133) |
| `roic` | `effective_tax_rate` (clamped, 0.21 default), NOPAT, ROIC |
| `cagr` | multi-year revenue / net income / OCF CAGR from a 10-K's FY columns |
| `valuation` | market cap, EV, equity/enterprise/SBC-adjusted FCF yield — **needs a period-end price**; shares are the filing's cover-page count (`filing_cover_shares`, T-132), else the balance sheet's outstanding count, else the weighted-average diluted count (flagged `shares_are_diluted_average`) — never `CommonStockSharesIssued` |

`__init__.py`: `CORE_GROUPS` always computed; `OPTIONAL_GROUPS` the orchestrator may
add; `valuation` is special-cased (needs a price arg).

### `agents.py` — Strands orchestration

`FundamentalAnalyst.analyze(ctx)` computes all deterministic groups, builds a
Strands `Agent` ("metrics-master", `MASTER_PROMPT`) whose tools are one `@tool`
specialist per group, then a synthesis step. Each specialist spins up its own
`Agent` with a system prompt from `skills/<name>/SKILL.md` (mapped groups:
`fcf_margin`, `interest_coverage_ratio`, `roic`, `cagr`,
`free_cash_flow_yield`) or a short inline brief. DeepSeek rejects OpenAI
`response_format` json-schema, so synthesis parses a JSON-text reply with a
rule-based fallback score. `build_model` sets `temperature=0`/`seed=0` to reduce
run-to-run variance (T-113 -- no LLM provider guarantees deterministic output at
temperature 0, so this is not a claim of reproducibility; `seed` is forwarded
verbatim, honoured or silently ignored depending on the endpoint, unverified
against the live DeepSeek endpoint since this environment's network egress
policy blocks reaching it -- confirm before `T-079`). A fallback score is
persisted under `model = agents.FALLBACK_MODEL_LABEL`, never the LLM's own
`model_id` (T-113: distinguishable in `score_snapshot`); every score, fallback or
not, carries a `prompt_hash` -- `FundamentalAnalyst.prompt_hash`
(`agents._prompt_version_hash`), a sha256 of every prompt template
(`MASTER_PROMPT`, every specialist's system prompt, the synthesis/repair
prompts) plus the model config, computed once when the analyst is built and
reused for every filing it scores. It is **not** a per-filing transcript hash
(rejected on review: unverifiable, since the transcript itself isn't stored,
and useless for grouping scores by the prompt version that produced them) --
two filings scored in the same run share one `prompt_hash`, and it changes
only when a prompt, a skill SOP file, or the model config actually changes,
which is what `T-079` needs to separate scores from before/after a prompt
edit. `analysis_run.fallback_units` counts how many of a run's scores fell
back. Module constants `FACTS_ENGINE_VERSION`, `METRICS_ENGINE_VERSION` (also
in `db.py`) key the immutable rows.

### `pricing.py` — the one cross-module link

`close_on_or_before(conn, asset_id, period_end)` runs a plain `SELECT` on
`price_daily` (last close within 7 days at/before the period-end; nothing before
2022). **No `import pricing_agent`** — `sqlite3.OperationalError` (table absent) →
`None`. This is the reference pattern for all cross-cutting read code.

### `filing_text.py` — `fetch_primary_document(cik, accession_number, *, form=None, client=None) -> (html, source_url)`

Fetches a filing's primary document directly from `www.sec.gov` (the gateway is
financials-only). The archive folder hangs off the **filer** CIK (the accession-number
prefix), not the asset's current CIK — this is why re-registered filers like XOM
resolved to 404s before. When `form` is given, the primary document is taken from the
filing's `…-index.html` **Document Format Files** table (the row whose `Type` is the
form, or its `/A` amendment, at the lowest sequence number) using that row's own
`href`; that is authoritative and survives filers whose `index.json` is incomplete.
Without `form`, or when the table has no body row, it falls back to `index.json` →
first non-`index`, non-`R\d+` `.htm` by size. A `200` with an empty body (SEC serves
these for a few genuinely-missing archived docs) is retried then raised. Descriptive
UA (`SEC_USER_AGENT`), retries 429/5xx. Swappable for a gateway text endpoint later
without touching `sections.py`.

### `sections.py` — `split_sections(html, form) -> list[Section]`

Deterministic Item splitter (`_SPECS` per form: 10-K → Business/Risk
Factors/Legal/MD&A = Items 1/1A/3/7; 10-Q → 1A/2). Flattens HTML to text **one line
per block element** — inline runs are joined tight, so a heading chopped mid-word
across `<span>`s (`Ite`+`m 2.`) is healed. Candidate headings are `Item N` lines
**and** descriptive-title lines (`MANAGEMENT'S DISCUSSION AND ANALYSIS OF …`), the
latter for the many financial-sector filers that keep Item numbers only in a front
cross-reference table. For each wanted section, picks **the occurrence with the most
following text** before the next *different* section heading — which rejects
table-of-contents rows and page running-headers (`(continued)`) alike. A body is
clamped at `_MAX_SECTION_CHARS = 400_000` so a mis-bounded section on a marker-less
filer can't swallow the rest of the document. `Section` carries `text`, `sha256`,
`word_count`, char offsets. `_MIN_SECTION_CHARS = 400` and a TOC-slice guard filter
the result.

`canonical_item_label(item_number, section_type)` is the canonical KG `itemLabel`
vocabulary (`ITEM_1A_RISK_FACTORS`, `ITEM_7_MDA`, …). It is not stored — the
projection view `v_sec_filing_section` builds the identical string in SQL; a test
pins the two together. DEF 14A director sections are **not** extracted here (they
would need a third form + a new parser); the `Executive` / `sharedExecutiveWith`
graph is fed by `entity_resolution` from news co-occurrence, not proxy filings.

### `db.py`

`connect` / `ensure_schema` (runs `SCHEMA` then `kg_schema.ensure`). Key writers:

| Function | Notes |
|---|---|
| `sync_universe(conn, members)` | upserts `assets` / `sectors` from `UniverseMember`s (identity write path only; no `universe_membership` write) |
| `load_universe(conn, *, tickers=None, symbols=None, limit=None)` | asset rows restricted to the point-in-time `symbols` (and optional `tickers`) |
| `start_run(conn, *, params, as_of=None, code_version=None)` | `analysis_run` row with the run's as-of + code tag |
| `upsert_filing(…, *, run_id=None, commit=True)` | `sec_filings` upsert on `(asset_id, form, fiscal_period)`; raises `FilingLabelCollision` when the label is already held by another `period_end` (T-140: the upsert would overwrite that filing in place). Triggers `trg_sf_accession_insert/update` refuse a second row of the asset with the same `accession_number` (T-120: one filing, one row) |
| `append_financial_facts(…, *, filing_version, event_time)` | **append-only** — `INSERT OR IGNORE`, no DELETE. Falls back to the pre-migration column set if the versioned columns aren't there yet. A fact carrying `correction_rule` (T-118: set by `iter_facts` from the gateway's own `data["corrections"]`) is persisted with that rule id in the like-named column; `NULL` means filed as-is |
| `record_metrics(…, *, engine_version, event_time)` | append-only `INSERT OR IGNORE` |
| `insert_snapshot(row)` | writes `score_snapshot` (`FUNDAMENTAL`, `ON CONFLICT DO NOTHING`); `SnapshotRow` carries `event_time` = filing period-end and `prompt_hash` (T-113) |
| `completed_units(conn)` | `(ticker, form, period_end)` triples with a FUNDAMENTAL score — drives `--fresh`-off resume. Keyed on the date, not the `fiscal_period` label (T-140: two of Waters' quarters shared a label and the second was skipped as done) |
| `latest_fiscal_year_end(conn, asset_id, before)` | period end of the asset's latest stored 10-K before a date — the fallback fiscal year end for a 10-Q whose balance sheet carries none (T-140) |
| `shared_accession_filings(conn)` | filing rows whose accession another row of the same asset carries — the legacy pre-T-091 shape (T-120) |
| `insert_filing_sections(…, *, engine_version, event_time, source_url, run_id)` | append-only; `SECTIONS_ENGINE_VERSION = "edgar-html-item-split-v2"` (v2 = block-aware flatten + title-only headings + filer-CIK paths) |
| `filings_with_sections(conn)` | resume set for `--sections` |

### `pipeline.py`

`run(settings, params)` → connect, `ensure_schema`, sync universe, plan
asset×form×year tasks, `_drive` with a `tqdm` bar. `_run_task` → `_list_filings` (the
ticker spelling EDGAR knows + every filing of the form that year) → `_select_filings`
(**oldest first** — F4's TTM reads the prior quarters' already-recorded values — and, for
a 10-K, only the most recent of several matches) → for each filing `_process_filing`:
skip it if it is filed after the analysis date or its accession is already scored (a
resumed run makes no `financials` call for it), fetch its statements by
`accession_number`, then for its `_target` → `_analyze_one`. A filing has exactly **one**
target — its *own* reporting period (10-K: the latest FY + prior; 10-Q: the latest quarter
in the payload), or **none**, in which case `_resolve_target` says why and the filing is counted
as skipped *and* recorded in `analysis_run_error` (`stage='period'`) and the run report: no
quarter column of its own (APO's Q1-2023 10-Q, whose payload holds only the prior fiscal year), a
latest quarter column that ends before the balance sheet's date (a comparative), or a date that is
no quarter end of the fiscal year (T-140). The label is derived, not copied from the gateway: a
10-K is `FY<year>`; a 10-Q is `<year>Q<n>` with *n* counted from the fiscal year end (the filing's own
balance sheet comparative column, else the asset's latest stored 10-K, so a changed fiscal year labels correctly;
`fundamental_agent.fiscal`),
because the gateway tags a column from the calendar month of its end and a 52/53-week filer's
quarter lands one tag ahead (Waters 2023-07-01 `(Q3)` beside the real Q3 2023-09-30; no 10-Q is a
Q4). `<year>` is the calendar year of the period end, a period ending in the first week of January
counting as the year before (J&J's fiscal 2022 ended 2023-01-01). A calendar-quarter filer's labels
are unchanged. The resume unit is `(ticker, form, period_end)`. The comparative columns (prior quarter, prior year) stay facts and never
become a filing row of their own, which would stamp them with this filing's accession and
date. One failing filing goes to `analysis_run_error` (with its accession) and does not stop
its siblings. `_analyze_one`: upsert filing → `append_financial_facts` → (if
`--sections`) `_extract_sections` (non-fatal, logged `stage='sections'`) → build
`FilingContext` (+ price via `close_on_or_before`) → `analyst.analyze` →
`record_metrics` → `quality.gate_version` (the Ring-1 gates over the metrics just stored) →
`insert_snapshot`. Failures per task go to `analysis_run_error`
and don't stop the batch.

### TTM on 10-Qs (F4, T-105)

Every ratio that divides a flow by a stock or a price level uses trailing-twelve-month flows on
a 10-Q: ROA/ROE, asset/inventory/receivables turnover, FCF yield (all three variants), net debt
/ EBITDA and ROIC. `pipeline._ttm_flows` builds them with `db.ttm_detail`, which tries, per flow:
the year-to-date identity `FY(prior 10-K) − YTD(last year) + YTD(this year)` (the filing's own
two YTD columns — a first quarter's YTD is its quarter — plus the recorded prior 10-K); then the
four recorded quarters; then quarter × 4. Each annualizing group's audit inputs carry
`annualized_ttm = 1` or `annualized_x4 = 1` (crude, excludable). Raw single-period values always
stay in the inputs — later filings read them back.

### `repair.py` — legacy shared-accession quarters (T-120)

Before T-091/T-092 one 10-Q per year was stored as a row per quarter column of its payload,
all carrying its accession, filing date and facts. `shared_groups(conn)` lists each shared
accession, the row for the filing's own (latest) period (kept) and the stale rows (refusing,
with `RepairRefused`, any stale row that metrics, a score, a data-quality verdict or a legacy
snapshot were computed from); `find_replacements(gateway, group)` fetches each stale
quarter's own 10-Q (the filing whose `_targets` period and fiscal period match);
`apply_group` deletes the stale rows (facts and sections cascade) and writes the
replacements' filing rows and facts in one transaction — no metrics or score, which the
stale rows never had. A group is only touched once every replacement is found (or with
`drop_unresolved`); a gateway error leaves it as it was. `run` refuses to start while any
shared accession remains (`pipeline.SharedAccessionsError`), since it would re-key a stale
row in place and append the real facts beside the borrowed ones.

### `quality.py` — Ring-1 data-quality gates (`DQ_*`, T-065)

Seven deterministic, LLM-free checks over a filing's **stored** metrics (value +
`inputs_json`), one metrics engine version at a time: `DQ_FCF_YIELD` (`|FCF yield| > 0.5`),
`DQ_MARGIN` (`|net margin| > 5`), `DQ_MARGIN_REVIEW` (`(1, 5]`, SOFT, review only),
`DQ_OCF_MARGIN` (`|OCF margin| > 3`), `DQ_MCAP_SCALE` (market cap / total assets outside
`[0.001, 100]`), `DQ_NEG_EQUITY` (equity ≤ 0 — D/E and ROE quarantined; HARD if net debt/EBITDA
> 5.0, or EBITDA ≤ 0 with positive net debt (a negative ratio, read directly off the raw
inputs rather than the ratio's sign, since the ratio alone can't tell that apart from a genuine
net-cash position — PR #95 review), or interest coverage < 1.5; recalibrated from debt/assets
by T-116), `DQ_REVENUE_POS` (revenue ≤ 0 or missing with net income reported). Each hit is one
`data_quality_issue` row per gated metric, keyed by the metric engine version and
`GATE_VERSION` (`dq-v2`), append-only (`INSERT OR IGNORE`).
`evaluate(fm)` is the pure core; `gate_version(conn, version, *, filing_id=None, run_id=None)`
records; `gate_all(conn, *, engine_version=None)` is the backfill. Rationale and the
production-copy verification: `docs/model_fixes.md`, T-065.

### `cli.py`

`run` subcommand (flags above), `quality` (the gate backfill over every stored filing; no LLM
variables needed; exit 1 if no metrics are stored for `--metrics-version`),
`repair-accessions` (dry run unless `--apply`; gateway only, no LLM variables; exit 1 while
any accession is left unresolved) and `migrate` →
`kg_schema.cli.run_migrate`.

## Gotchas

- The 10-K short-circuit in `_process` skips a filing whose FUNDAMENTAL snapshot
  already exists (unless `--fresh`).
- `--sections` adds a `www.sec.gov` dependency and ~10 req/s courtesy limit; keep
  it opt-in.
- Line-item matching is scoped per statement — otherwise it hits cash-flow
  "increase/decrease in …" rows and returns negatives.
- `capital_expenditure` is found by its PP&E concepts, then (T-133) by ordered fallback tiers — oil & gas development, oil & gas
  acquisition, the "net" additions line — and last by a cash-flow caption "capital expenditure(s)" **only when exactly one line reads
  that way**: a filer reporting capex in parts (NEE) has no capex rather than a fraction of it. Banks, insurers and many utilities
  therefore still have no FCF.
- A 10-Q's first quarter is its year-to-date; the gateway tags it by calendar quarter (Waters' arrives as `(Q2)`), so
  `pipeline._is_first_quarter` also reads the balance sheet's comparative column (the last fiscal year end, one quarter back).
