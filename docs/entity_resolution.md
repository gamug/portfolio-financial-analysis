# `entity_resolution/`

Roadmap step 7: candidate `sharedExecutiveWith` edges. A **read-only** consumer of
the news repo's `urls.db` that writes one table (`shared_executive_edge`) into
`KG_FINANCIAL_DB`. No SEC proxy scraping — the signal is people co-mentioned in the
news of multiple issuers.

```bash
uv run python -m entity_resolution build [--analysis-date 2021-06-30] [--min-weight 3] \
    [--max-tickers 15] [--min-articles 3] [--news-db /path/to/urls.db] [--universe-db PATH]
```

`--analysis-date` (optional, default: today) is the `cycle_run.cycle_date`
(with `cycle_run.code_version`), selects the universe from `universe.db` as of that
date, and drops any news whose `discovered_urls.pub_date` is after it **or NULL**
(strict no-lookahead — undated news cannot be date-verified).

## Configuration (`config.py`)

`KG_FINANCIAL_DB` (required) + `KG_NEWS_DB` (default
`/workspaces/thesis/data/urls.db`) + `KG_UNIVERSE_DB` (default
`/workspaces/thesis/data/universe.db`).

## Files

### `news_db.py` — the read-only accessor

**Hard rule: only two query shapes, both index-backed.**

| Function | Query | Index |
|---|---|---|
| `connect_ro(path)` | `file:…?mode=ro` (never touches the WAL) | — |
| `article_ids_for_ticker(news, ticker, *, until=None)` | `SELECT id FROM discovered_urls WHERE ticker = ?` (+ `AND pub_date IS NOT NULL AND pub_date <= ?` when `until` set) | `idx_ticker` |
| `per_entities_for_articles(news, ids)` | chunked (`≤ 900`) `SELECT article_id, text, score FROM article_entities WHERE entity_type='PER' AND article_id IN (…)` | `idx_article_entities_article_id` |

The module **never names `articles` or `body_text`** (a 4.5 GB unindexed table); a
SQL-tracing test (`test_entity_news_db`) fails if it does.

### `normalize.py` — `canonical(raw) -> str | None`

Strips honorifics / titles (`Mr`, `Dr`, `CEO`, …), title-cases, requires ≥ 2 alpha
tokens and length 4–40, drops single-token / first-name-only fragments (`Bob`,
`Cra`). Returns a `First Last` canonical form or `None`.

### `denylist.py` — `is_denied(name, *, distinct_ticker_count, max_tickers, mean_score)`

`STATIC_DENY` seeded from the most frequent PER spans in the `urls.db` snapshot: US
politicians (`Trump`, `Obama`, `Biden`) and CNBC *Fast Money* pundits (`Jim
Cramer`, `Pete/Jon Najarian`, `Guy Adami`, `Karen Finerman`, …). Heuristics: name
across more than `max_tickers` (15) distinct issuers → analyst/pundit; mean NER
score below `MIN_NER_SCORE` (0.90) → weak.

### `cooccurrence.py` — `build_edges(news, tickers, *, min_weight, max_tickers, min_articles) -> list[Edge]`

Per ticker: article ids → PER spans → `canonical` → accumulate
`{name: {ticker: (article_set, scores)}}`. For each surviving name on ≥ 2 tickers,
restrict to tickers with ≥ `min_articles` articles, then for every unordered pair
emit `Edge(ticker_a, ticker_b, person_name, article_count_a/b, weight, scores)`
with `weight = min(count_a, count_b)`, kept when `weight ≥ min_weight`.

### `db.py` — `replace_edges(conn, edges, *, method=METHOD, run_id=None)`

`METHOD = "news-per-cooccurrence-v1"`. Full method-versioned recompute: `DELETE …
WHERE method = ?` then `INSERT OR IGNORE` (asset ids resolved from `assets`,
ordered `asset_id_a < asset_id_b`; `evidence_json` carries the mean NER score). A
re-tuned run under a new `method` is a parallel dataset.

### `pipeline.py` — `run(settings, params) -> RunReport`

Opens an `ENTITY_RESOLUTION` `cycle_run` for provenance, loads the `assets` tickers,
`connect_ro(urls.db)`, `build_edges`, `replace_edges`, marks the cycle completed.

### `cli.py` — `build` subcommand

## Scale & gotchas

- ~950 article ids/ticker × ~500 tickers, all index seeks → **minutes**, not hours.
  Article↔ticker sets are disjoint (each article has exactly one ticker), so no
  cross-ticker caching is needed.
- `article_entities` is 17.7 M rows — cheap filtered by `article_id`, ruinous to
  scan. The accessor structurally prevents a scan; keep it that way.
- Open `urls.db` strictly `mode=ro` — it has its own `-wal`/`-shm` and may be
  written by the news pipeline concurrently.
- News co-occurrence ≠ an actual shared directorship. The denylist + thresholds
  are the whole game; they are CLI flags for tuning.
