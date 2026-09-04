# portfolio-financial-analysis

## Fundamental SEC-filings analysis agent

`fundamental_agent/` runs a fundamental economic analysis over S&P 500 SEC filings
(10-K annual, 10-Q quarterly, fiscal years >= 2022) and writes the results to the
SQLite database named by `KG_FINANCIAL_DB`.

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
| `KG_FINANCIAL_DB` | SQLite path. `assets` / `sectors` are created only if missing and never overwritten; all other tables are owned by this agent. The old misspelled `KG_FINANTIAL_DB` is still read as a fallback. |
| `KG_UNIVERSE_DB` | Optional; point-in-time S&P 500 membership DB. Defaults to `/workspaces/thesis/data/universe.db`. Every agent reads the universe from here as of its `--analysis-date`. |
| `LLM_API_KEY` / `LLM_MODEL` / `LLM_URL` | OpenAI-compatible LLM (DeepSeek). Used via Strands' `OpenAIModel`. |
| `EDGAR_BASE_URL` | Optional; defaults to `http://host.docker.internal:8000/edgar/edgar`. |

### Run

```bash
uv run python -m fundamental_agent run                       # full universe, 10-K + 10-Q, since 2022
uv run python -m fundamental_agent run --analysis-date 2021-06-30   # as-of a past date (no lookahead)
uv run python -m fundamental_agent run --limit 5             # first 5 tickers (dev)
uv run python -m fundamental_agent run --tickers AAPL,NVDA --forms 10-K --since-year 2023
uv run python -m fundamental_agent run --fresh               # re-analyze instead of resuming
```

`--analysis-date` (optional, default: today) is the run's as-of date: the universe
is read from `universe.db` as of that date, filings filed after it are skipped, and
the year range is capped at its year. The membership is projected into
`assets`/`sectors` each run (`--refresh-universe` is a deprecated no-op). The run is
archived in `analysis_run` with its `as_of` and `code_version` (git SHA), and every
row it writes carries its `run_id`. A `tqdm` bar shows progress; failures are
recorded in `analysis_run_error` and do not stop the batch.

### Inspect results

```bash
uv run python -c "import os,sqlite3; c=sqlite3.connect(os.environ['KG_FINANCIAL_DB']); \
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
pricing gateway and writes them to the same `KG_FINANCIAL_DB` under its own tables.

**Per ticker:** one `GET /pricing/{ticker}?start_date&end_date` call over the whole range,
then a `price_window` row with the first/last trading day and close, period return, trading
days, **daily log-return std-dev**, **annualized volatility** (`std · √252`), min/max close and
average volume. `--by-year` adds one window per calendar year; `--store-daily` also keeps every
OHLCV bar in `price_daily`. The tracked universe is read from `universe.db` as of the run's
`--analysis-date` (`assets`/`sectors` created only if missing, never clobbered). Share-class
tickers are retried across spellings (`BRK.B` → `BRK-B`); tickers the gateway returns empty
are logged, not fatal.

```bash
uv run python -m pricing_agent run                          # full universe, 2022-01-01 → today
uv run python -m pricing_agent run --analysis-date 2021-06-30   # as-of universe + candle cutoff
uv run python -m pricing_agent run --limit 5 --by-year      # first 5 tickers, per-year windows
uv run python -m pricing_agent run --tickers AAPL,NVDA --store-daily
```

Config: `KG_FINANCIAL_DB` (required); `KG_UNIVERSE_DB` / `PRICING_BASE_URL` (optional).
`--analysis-date` (default: today) drives the universe and the fetch end date (candles after
it are dropped); `--end` is clamped to it. Re-runs skip windows already stored; `--fresh`
recomputes. Run stats land in `pricing_run` (with `as_of` + `code_version`) / `pricing_run_error`.

## Knowledge-graph projection layer

The relational tables above are the *source* the integration repo projects into an
RDF knowledge graph. This repo owns making that output rich and lossless enough to
project; the integration repo owns triple emission + SHACL validation + named-graph
minting.

`kg_schema` is a passive, behaviour-free package both agents call from
`ensure_schema`. It lives in the shared [`portfolio-common`](https://github.com/gamug/portfolio-common)
repo (imported as `portfolio_common.kg_schema`, git-tag-pinned in
`pyproject.toml`'s `[tool.uv.sources]`; override with an editable path for local
work), so every Portfolio Thesis repo that touches `KG_FINANCIAL_DB` shares one
copy. It adds (all `CREATE TABLE IF NOT EXISTS` / nullable `ADD COLUMN` — safe to
ship against the shared DB anytime):

| Table | Roadmap concept | Written by |
|---|---|---|
| `score_snapshot` | `ScoreSnapshot` (`FUNDAMENTAL` / `VALORIZATION` / `TECHNICAL` / `SEMANTIC`) | fundamental agent, `cycle`, integration repo |
| `universe_membership` | `UniverseMembership` (`valid_from` / `valid_to`) | **frozen** — the universe is now read point-in-time from `universe.db` (`KG_UNIVERSE_DB`); agents no longer write this table |
| `v_analysis_run` / `v_pricing_run` / `v_quant_run` / `v_cycle_run` | per-agent run log: `run_id`, `as_of`, `code_version`, params | every agent run |
| `price_observation` | `PriceObservation` (close/return/ATR/vol/drawdown/momentum) | `pricing_agent run --observations` |
| `sec_filing_section` | `SECFilingSection` (MD&A, risk factors) | `fundamental_agent run --sections` |
| `veto` / `rule_catalog` | `Veto` / `RuleClause` | `cycle` |
| `portfolio_position` / `cycle_ranking` | `PortfolioPosition` | `cycle select` |
| `shared_executive_edge` | `sharedExecutiveWith` | `entity_resolution build` |
| `cycle_run` / `cycle_checkpoint` | orchestrator provenance / resume | `cycle` |
| `corporate_action` / `quant_return_daily` | dividends + total-return series | `quant backfill-actions` / `build-returns` |
| `quant_risk_model` / `quant_portfolio` / `quant_position` | Markowitz benchmark book (μ, Σ, frontier, weights) | `quant build-risk-model` / `optimize` |
| `benchmark_series` / `quant_benchmark_performance` | benchmark index + forward realized returns | `quant benchmark` / `evaluate` |

Read-contract `v_*` VIEWs (documented in `portfolio_common/kg_schema/views.py`)
are what the integration repo consumes; the physical schema can evolve underneath
them.

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
uv run python -m cycle select --analysis-date 2026-06-30 --top-n 30  # score, veto, rank, open positions
uv run python -m cycle monitor --analysis-date 2026-07-31  # refresh vetoes / ranking only
```

Every agent takes an optional `--analysis-date YYYY-MM-DD` (default: today) — the single
as-of date for the run. It selects the universe (from `universe.db`), bounds ingestion so
nothing dated after it is written, and is recorded on the run-log row alongside a
`code_version` git tag so `portfolio-reports` can trace an old run. `cycle` still accepts
`--date` and `quant build-risk-model` / `optimize` still accept `--as-of` as aliases.

**Universe-coverage check.** Because the universe is now a mutable dated list, a member as
of a past date may have no EDGAR / pricing / observation rows yet (it left the index, or
was never ingested that far back) — `cycle` and `quant` would then silently run on the
covered subset. `coverage` (on `fundamental_agent` / `pricing_agent` / `quant`) reports
per-member coverage and records it in `universe_coverage` / `v_universe_coverage`:

```bash
uv run python -m quant coverage --analysis-date 2024-06-30            # report + persist
uv run python -m quant coverage --analysis-date 2024-06-30 --strict   # exit 1 if < --min-fraction
uv run python -m fundamental_agent coverage --analysis-date 2024-06-30 --print-fill-commands
```

Default is **warn** (report + exit 0); `--strict` fails when the covered fraction is below
`--min-fraction` (default 1.0). `--print-fill-commands` emits the `... run --tickers <missing>
--analysis-date D` lines to close the gaps (delisted names may still fail — see `*_run_error`).

### Markowitz benchmark portfolio (`quant/`)

The base case the blended-score `portfolio_position` book is evaluated against —
a mean-variance optimizer over a score-independent liquidity/data universe. First
numeric dependency in the repo (numpy / scipy / cvxpy); confined to `quant/`,
which no other package imports. See [docs/quant.md](docs/quant.md).

```bash
uv run python -m quant backfill-actions --source derive          # dividends -> corporate_action
uv run python -m quant build-returns                             # total-return series -> quant_return_daily
uv run python -m quant build-risk-model --analysis-date 2026-08-27   # Ledoit-Wolf Sigma + shrunk mu
uv run python -m quant optimize --analysis-date 2026-08-27 \
    --objectives min_var,tangency,target_vol,frontier            # persist the benchmark books
uv run python -m quant evaluate --from 2026-08-27               # forward returns vs the live book
```

For the `--from`/`--to` subcommands (`backfill-actions`, `build-returns`, `benchmark`,
`evaluate`), `--analysis-date` (default: today) is the upper bound: `--to` is clamped to it,
`quant_run.as_of` is stamped with it, and `backfill-actions --source derive` only reads
filings with `period_end <= analysis-date`.

`entity_resolution build` also takes `--analysis-date`: it drives the `cycle_run.cycle_date`
stamp, reads the universe from `universe.db` as of that date, and drops news whose
`discovered_urls.pub_date` is after it or NULL. It reads the news repo's `urls.db` strictly
read-only (via `KG_NEWS_DB`, default `/workspaces/thesis/data/urls.db`).

## Read-only HTTP API (`api/`)

A thin FastAPI surface over the `v_*` read-contract views and `universe.db` — a stable
network boundary for `portfolio-reports` / `portfolio-app` instead of opening the SQLite
files. It **only reads** (`mode=ro`); the agents stay CLI-driven and own every write.

```bash
uv run python -m api                                   # serve on API_HOST:API_PORT (default 0.0.0.0:8010)
uv run uvicorn --factory api.app:create_app --reload   # dev; docs at /docs
```

Endpoints under `/api/v1`: `GET /health` · `GET /health/db` · `GET /runs` (run log,
`?kind=`) · `GET /universe?as_of=D` · `GET /universe/coverage?as_of=D` · `GET /scores`
(`?ticker=`/`?score_type=`/`?as_of=`) · `GET /portfolio/positions` · `GET /portfolio/ranking`.
Config: `KG_FINANCIAL_DB` (required), `KG_UNIVERSE_DB` / `API_HOST` / `API_PORT` /
`API_ROOT_PATH` (optional). See [docs/api.md](docs/api.md).

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
