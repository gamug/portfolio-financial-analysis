# Module documentation

One document per package. Every package lives under `src/<name>/`, is installed
editable into the uv env (hatchling build backend), and runs as `python -m <name>`
from any directory; `pytest` adds `src` via `pythonpath`.

| Package | Role | Doc |
|---|---|---|
| `fundamental_agent/` | SEC 10-K/10-Q ratio analysis → `score_snapshot` (FUNDAMENTAL) + `sec_filing_section` | [fundamental_agent.md](fundamental_agent.md) |
| `pricing_agent/` | S&P 500 daily pricing → `price_window` + `price_observation` | [pricing_agent.md](pricing_agent.md) |
| `kg_schema/` | Passive shared schema, migrations, read-contract views | [kg_schema.md](kg_schema.md) |
| `cycle/` | Strands selection / monitoring cycles → TECHNICAL/VALORIZATION scores, `veto`, `portfolio_position` | [cycle.md](cycle.md) |
| `entity_resolution/` | `sharedExecutiveWith` edges from news co-occurrence | [entity_resolution.md](entity_resolution.md) |
| `quant/` | Markowitz mean-variance benchmark portfolio → `corporate_action`, `quant_return_daily`, `quant_*` | [quant.md](quant.md) |

## How they fit together

```
                    ┌─────────────────────────── KG_FINANCIAL_DB (SQLite) ───────────────────────────┐
                    │  assets / sectors        (identity, owned elsewhere)                           │
 EDGAR gateway ───▶ │  sec_filings, financial_facts, fundamental_metrics, score_snapshot[FUND]      │ ◀── fundamental_agent
 www.sec.gov   ───▶ │  sec_filing_section                                                            │ ◀── fundamental_agent --sections
 pricing gw    ───▶ │  price_window, price_daily, price_observation                                  │ ◀── pricing_agent (--observations)
 (both agents) ───▶ │  universe_membership                                                           │ ◀── */sync_universe
 urls.db (ro)  ───▶ │  shared_executive_edge                                                         │ ◀── entity_resolution
                    │  score_snapshot[TECH/VALOR], veto, rule_catalog, cycle_*, portfolio_position   │ ◀── cycle
                    │  score_snapshot[SEMANTIC]                                                       │ ◀── integration repo
                    │  corporate_action, quant_return_daily, quant_* (Markowitz benchmark book)       │ ◀── quant
                    │  v_* read-contract views  ──────────────────────────────────────────────────▶  │ ──▶ integration repo → RDF graph
                    └───────────────────────────────────────────────────────────────────────────────┘
```

- **This repo** produces rich, append-only, provenance-tagged relational rows and a
  set of `v_*` views. It never emits RDF.
- **The integration repo** reads the `v_*` views (and `urls.db` for SEMANTIC),
  validates with SHACL, and mints named graphs.
- `pricing_agent` has **zero imports** to/from `fundamental_agent`. `cycle/` and
  `entity_resolution/` sit above both and read shared tables via plain SQL, mirroring
  the `fundamental_agent/pricing.py` accessor pattern.
- `quant/` is a leaf: no other package imports it, so its numeric dependencies
  (numpy, scipy, cvxpy) never load on their import path. It reads shared tables via
  plain SQL and imports only `kg_schema`.

## Shared conventions

- `connect()` in every package: `foreign_keys=ON`, `busy_timeout=30000`, **no WAL**
  (the DB can be on a bind mount whose `-shm` support is unreliable).
- Two timestamps on observation rows: `event_time` (what the row is *about*) and
  `computed_at` / `ingested_at` / `retrieved_at` (when it was written).
- Measurement tables are append-only; a re-run with the same `*_version` collides on
  the unique key and is ignored, a new version writes a parallel row.
- `run_id` + `run_kind` (`'analysis'` / `'pricing'` / `'cycle'`) identify the writer.
