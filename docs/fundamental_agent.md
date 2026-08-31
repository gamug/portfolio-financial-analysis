# `fundamental_agent/`

Fundamental economic analysis of S&P 500 SEC filings (10-K annual, 10-Q quarterly,
fiscal years ≥ 2022 by default). Per filing: pull statements from the EDGAR gateway
→ compute deterministic ratios → a Strands *metrics-master* agent consults
specialist sub-agents → a synthesis step produces a scored assessment → one
immutable `score_snapshot` row (`score_type='FUNDAMENTAL'`) per
`(asset, form, fiscal_period)`. Optionally also extracts narrative filing text.

```bash
uv run python -m fundamental_agent run [--tickers AAPL,NVDA] [--forms 10-K] \
    [--since-year 2023] [--fresh] [--refresh-universe] [--sections]
uv run python -m fundamental_agent migrate        # shared-schema migrations
```

## Configuration (`config.py`)

`Settings.load()` requires `KG_FINANTIAL_DB`, `LLM_API_KEY`, `LLM_MODEL`,
`LLM_URL`. Optional: `EDGAR_BASE_URL` (default
`http://host.docker.internal:8000/edgar/edgar` — the doubled `/edgar` is
intentional), `WIKIPEDIA_USER_AGENT`, `SEC_USER_AGENT`.

## Files

### `edgar_client.py` — `EdgarClient`

Blocking HTTP client for the gateway. Endpoints: `/company_info/{t}`,
`/years_available/{t}?form=`, `/filing_by_year/{t}?form=&year=`,
`/financials/{t}?form=&year=`. Every response is a `{"success", "data"}` envelope,
unwrapped by `_unwrap`. Retries 500/502/503/504 + transport errors (3 attempts,
exp backoff ≤ 8 s); 404 → `EdgarNotFoundError`. `normalize_ticker` yields spelling
candidates (`BRK.B` → `BRK-B` → `BRKB`).

### `universe.py`

Scrapes the Wikipedia S&P 500 constituents table (`#constituents`) with
BeautifulSoup + lxml into `Company` models. Needs a descriptive UA
(`WIKIPEDIA_USER_AGENT`); datacenter browser-mimic UAs get 403. `fetch_sp500()` is
the entry the pipeline calls.

### `statements.py` — `Statements`, `iter_facts`

`Statements.from_payload` parses `income_statement` / `balance_sheet` /
`cash_flow`. Duration columns look like `"2023-09-30 (FY)"` / `"(Q3)"` / `"(YTD)"`;
**balance-sheet rows use bare instant dates** and resolve against the
nearest-earlier instant. `REGISTRY` maps ~25 line items to US-GAAP tags with
standard/label fallbacks. `iter_facts` flattens every non-abstract numeric cell for
`financial_facts`.

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
| `cashflow` | OCF margin, FCF margin, FCF conversion, capex intensity; `free_cash_flow()` helper |
| `roic` | `effective_tax_rate` (clamped, 0.21 default), NOPAT, ROIC |
| `cagr` | multi-year revenue / net income / OCF CAGR from a 10-K's FY columns |
| `valuation` | market cap, EV, equity/enterprise/SBC-adjusted FCF yield — **needs a period-end price** |

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
rule-based fallback score. Module constants `FACTS_ENGINE_VERSION`,
`METRICS_ENGINE_VERSION` (also in `db.py`) key the immutable rows.

### `pricing.py` — the one cross-module link

`close_on_or_before(conn, asset_id, period_end)` runs a plain `SELECT` on
`price_daily` (last close within 7 days at/before the period-end; nothing before
2022). **No `import pricing_agent`** — `sqlite3.OperationalError` (table absent) →
`None`. This is the reference pattern for all cross-cutting read code.

### `filing_text.py` — `fetch_primary_document(cik, accession_number, *, client=None) -> (html, source_url)`

Fetches a filing's primary document directly from `www.sec.gov` (the gateway is
financials-only). Resolves `…/Archives/edgar/data/{cik}/{accession_nodash}/index.json`
→ picks the first non-`index`, non-`R\d+` `.htm`. Descriptive UA (`SEC_USER_AGENT`),
retries 429/5xx. Swappable for a gateway text endpoint later without touching
`sections.py`.

### `sections.py` — `split_sections(html, form) -> list[Section]`

Deterministic Item splitter (`_SPECS` per form: 10-K → Business/Risk
Factors/Legal/MD&A = Items 1/1A/3/7; 10-Q → 1A/2). Flattens HTML to text, finds
every `Item N.` line, and for each wanted Item picks **the occurrence with the most
following text** (rejects the short table-of-contents line). `Section` carries
`text`, `sha256`, `word_count`, char offsets. `_MIN_SECTION_CHARS = 400` filter.

### `db.py`

`connect` / `ensure_schema` (runs `SCHEMA` then `kg_schema.ensure`). Key writers:

| Function | Notes |
|---|---|
| `sync_universe(conn, companies, *, as_of=None)` | upserts `assets`, then `universe_membership.reconcile` |
| `upsert_filing` | `sec_filings` upsert on `(asset_id, form, fiscal_period)` |
| `append_financial_facts(…, *, filing_version, event_time)` | **append-only** — `INSERT OR IGNORE`, no DELETE. Falls back to the pre-migration column set if the versioned columns aren't there yet |
| `record_metrics(…, *, engine_version, event_time)` | append-only `INSERT OR IGNORE` |
| `insert_snapshot(row)` | writes `score_snapshot` (`FUNDAMENTAL`, `ON CONFLICT DO NOTHING`); `SnapshotRow` carries `event_time` = filing period-end |
| `completed_units(conn)` | `(ticker, form, fiscal_period)` triples with a FUNDAMENTAL score — drives `--fresh`-off resume |
| `insert_filing_sections(…, *, engine_version, event_time, source_url, run_id)` | append-only; `SECTIONS_ENGINE_VERSION = "edgar-html-item-split-v1"` |
| `filings_with_sections(conn)` | resume set for `--sections` |

### `pipeline.py`

`run(settings, params)` → connect, `ensure_schema`, sync universe, plan
asset×form×year tasks, `_drive` with a `tqdm` bar. `_process` → fetch financials +
meta → for each `_target` (10-K: latest FY + prior; 10-Q: matching-year quarters)
→ `_analyze_one`. `_analyze_one`: upsert filing → `append_financial_facts` → (if
`--sections`) `_extract_sections` (non-fatal, logged `stage='sections'`) → build
`FilingContext` (+ price via `close_on_or_before`) → `analyst.analyze` →
`record_metrics` → `insert_snapshot`. Failures per task go to `analysis_run_error`
and don't stop the batch.

### `cli.py`

`run` subcommand (flags above) + `migrate` subcommand → `kg_schema.cli.run_migrate`.

## Gotchas

- The 10-K short-circuit in `_process` skips a filing whose FUNDAMENTAL snapshot
  already exists (unless `--fresh`).
- `--sections` adds a `www.sec.gov` dependency and ~10 req/s courtesy limit; keep
  it opt-in.
- Line-item matching is scoped per statement — otherwise it hits cash-flow
  "increase/decrease in …" rows and returns negatives.
