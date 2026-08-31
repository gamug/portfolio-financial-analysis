# portfolio-financial-analysis

## Fundamental SEC-filings analysis agent

`fundamental_agent/` runs a fundamental economic analysis over S&P 500 SEC filings
(10-K annual, 10-Q quarterly, fiscal years >= 2022) and writes the results to the
SQLite database named by `KG_FINANTIAL_DB`.

**Pipeline per filing:** pull statements from the EDGAR gateway → compute deterministic
ratios (profitability, liquidity, leverage, efficiency, growth, cash flow, roic, cagr,
and — when a period-end share price is on hand — valuation) → a
[Strands](https://strandsagents.com) *metrics-master* agent decides which specialist
agents to consult and gathers their readings → a synthesis step produces a
`FundamentalAssessment` (score 0-100, `bullish`/`neutral`/`bearish` rating, narrative,
strengths, risks). Each `(asset, form, fiscal_period)` produces one immutable
`fundamental_snapshot` row; re-running skips filings already analyzed.

### Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `KG_FINANTIAL_DB` | SQLite path. `assets` / `sectors` are created only if missing and never overwritten; all other tables are owned by this agent. |
| `LLM_API_KEY` / `LLM_MODEL` / `LLM_URL` | OpenAI-compatible LLM (DeepSeek). Used via Strands' `OpenAIModel`. |
| `EDGAR_BASE_URL` | Optional; defaults to `http://host.docker.internal:8000/edgar/edgar`. |
| `WIKIPEDIA_USER_AGENT` | Optional; set a descriptive UA with a real contact URL for the S&P 500 scrape. |

### Run

```bash
uv run python -m fundamental_agent run                       # full universe, 10-K + 10-Q, since 2022
uv run python -m fundamental_agent run --limit 5             # first 5 tickers (dev)
uv run python -m fundamental_agent run --tickers AAPL,NVDA --forms 10-K --since-year 2023
uv run python -m fundamental_agent run --fresh               # re-analyze instead of resuming
```

The universe is scraped once from Wikipedia's S&P 500 list into `assets`/`sectors`
(re-scrape with `--refresh-universe`). A `tqdm` bar shows progress and ETA; failures
are recorded in `analysis_run_error` and do not stop the batch.

### Inspect results

```bash
uv run python -c "import os,sqlite3; c=sqlite3.connect(os.environ['KG_FINANTIAL_DB']); \
  [print(r) for r in c.execute('SELECT a.ticker,s.form,s.fiscal_period,round(s.score,1),s.rating \
   FROM fundamental_snapshot s JOIN assets a ON a.id=s.asset_id ORDER BY 1,3')]"
```

Note: the LLM synthesis parses a JSON reply rather than using
`Agent.structured_output`, because DeepSeek does not currently accept OpenAI
`response_format` json-schema. If the model returns nothing usable, a rule-based
score derived from the computed ratios is stored instead.

**Valuation / free-cash-flow yield.** The `valuation` group (equity and enterprise FCF
yield, an SBC-adjusted variant, market cap, enterprise value) needs a market price. It
reads the last `price_daily` close on or before each filing's period-end — the table the
pricing collector below fills — with a plain `SELECT`, no import of `pricing_agent`. Run
`pricing_agent` first for these metrics; without price rows the group is silently skipped
and every other metric is unaffected.

## S&P 500 pricing collector

`pricing_agent/` is a **standalone** module (no `fundamental_agent` coupling; cross-module
orchestration is a separate branch) that collects daily-pricing window summaries from the
pricing gateway and writes them to the same `KG_FINANTIAL_DB` under its own tables.

**Per ticker:** one `GET /pricing/{ticker}?start_date&end_date` call over the whole range,
then a `price_window` row with the first/last trading day and close, period return, trading
days, **daily log-return std-dev**, **annualized volatility** (`std · √252`), min/max close and
average volume. `--by-year` adds one window per calendar year; `--store-daily` also keeps every
OHLCV bar in `price_daily`. The tracked universe comes from the gateway's `/universe` endpoint
(`assets`/`sectors` created only if missing, never clobbered). Share-class tickers are retried
across spellings (`BRK.B` → `BRK-B`); tickers the gateway returns empty are logged, not fatal.

```bash
uv run python -m pricing_agent run                          # full universe, 2022-01-01 → today
uv run python -m pricing_agent run --limit 5 --by-year      # first 5 tickers, per-year windows
uv run python -m pricing_agent run --tickers AAPL,NVDA --store-daily
```

Config: `KG_FINANTIAL_DB` (required); `PRICING_BASE_URL` (optional, defaults to
`http://host.docker.internal:8000/pricing`). Re-runs skip windows already stored; `--fresh`
recomputes. Progress + ETA via `tqdm`; run stats land in `pricing_run` / `pricing_run_error`.

## Knowledge-graph projection layer

The relational tables above are the *source* the integration repo projects into an
RDF knowledge graph. This repo owns making that output rich and lossless enough to
project; the integration repo owns triple emission + SHACL validation + named-graph
minting.

`kg_schema/` is a passive, behaviour-free package both agents call from
`ensure_schema`. It adds (all `CREATE TABLE IF NOT EXISTS` / nullable
`ADD COLUMN` — safe to ship against the shared DB anytime):

| Table | Roadmap concept | Written by |
|---|---|---|
| `score_snapshot` | `ScoreSnapshot` (`FUNDAMENTAL` / `QUANTITATIVE` / `TECHNICAL` / `SEMANTIC`) | fundamental agent, `cycle`, integration repo |
| `universe_membership` | `UniverseMembership` (`valid_from` / `valid_to`) | both agents' `sync_universe` |
| `price_observation` | `PriceObservation` (close/return/ATR/vol/drawdown/momentum) | `pricing_agent run --observations` |
| `sec_filing_section` | `SECFilingSection` (MD&A, risk factors) | `fundamental_agent run --sections` |
| `veto` / `rule_catalog` | `Veto` / `RuleClause` | `cycle` |
| `portfolio_position` / `cycle_ranking` | `PortfolioPosition` | `cycle select` |
| `shared_executive_edge` | `sharedExecutiveWith` | `entity_resolution build` |
| `cycle_run` / `cycle_checkpoint` | orchestrator provenance / resume | `cycle` |

Read-contract `v_*` VIEWs (documented in `kg_schema/views.py`) are what the
integration repo consumes; the physical schema can evolve underneath them.

**Non-additive migrations** (widening unique keys, `fundamental_snapshot` →
`score_snapshot` + a compatibility view) are gated behind an explicit command and
advance `schema_version` (a floor other repos can assert against):

```bash
uv run python -m fundamental_agent migrate     # quiesce other writers first; back up the DB
uv run python -m pricing_agent migrate         # (idempotent -- a no-op after the first)
```

### New collectors / cycles

```bash
uv run python -m pricing_agent run --observations          # + PriceObservation analytics
uv run python -m fundamental_agent run --sections          # + narrative filing text (fetches www.sec.gov)
uv run python -m entity_resolution build --min-weight 3    # sharedExecutiveWith from urls.db news co-occurrence
uv run python -m cycle select --date 2026-06-30 --top-n 30 # score, veto, rank, open positions
uv run python -m cycle monitor --date 2026-07-31           # refresh vetoes / ranking only
```

`entity_resolution` reads the news repo's `urls.db` strictly read-only (via
`KG_NEWS_DB`, default `/workspaces/thesis/data/urls.db`). The SEMANTIC score
aggregation itself lives in the integration repo, not here.

## Development

This project uses [uv](https://docs.astral.sh/uv/) for environment management and
[pre-commit](https://pre-commit.com/) (Ruff + mypy + Commitizen) for code quality.

### Quick start

```bash
uv sync --group dev                 # create .venv from uv.lock
uv run pre-commit install --install-hooks
```

Opening the repo in the provided **Dev Container** (`.devcontainer/`) runs both
steps automatically and pins the exact toolchain (Python 3.12 + uv).

### Common tasks

```bash
uv run ruff check --config .code_quality/ruff.toml .      # lint
uv run ruff format --config .code_quality/ruff.toml .     # format
uv run mypy --config-file .code_quality/mypy.ini .        # type-check
uv run pre-commit run --all-files                         # everything
```

Commits follow [Conventional Commits](https://www.conventionalcommits.org/);
`cz commit` (via `uv run cz commit`) helps compose them.
