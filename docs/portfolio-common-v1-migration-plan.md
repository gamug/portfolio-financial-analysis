# Migration plan: portfolio-common v1.0.0 (DB engine + business_folders split)

## What changed upstream

`portfolio-common` v1.0.0 is a clean-break rewrite: the shared library used to
mix two concerns — a generic SQLite connection engine, and business/domain
code owned by specific downstream repos (`kg_schema` was ours). As of v1.0.0:

- `portfolio-common` is **DB-engine-only**: `portfolio_common.db.Database` (a
  single connection class subsuming what `kg_schema/db.py.connect`/
  `connect_ro` did), plus `portfolio_common.db.in_clause` /
  `portfolio_common.db.Allowlist` — reusable injection-prevention primitives
  for building dynamic SQL text (`IN (...)` clauses and allowlisted dynamic
  identifiers).
- Everything under the old `portfolio_common.kg_schema` package has been
  extracted into `business_folders/financial_analysis/kg_schema/` in the
  `portfolio-common` repo, staged for this repo to adopt directly — it is
  **not** part of the installed `portfolio-common` package anymore, and will
  be deleted from that staging area once we've pulled it in here.
- There is **no backward-compatible shim** — pinning our `portfolio-common`
  git tag to `v1.0.0` breaks every `from portfolio_common import kg_schema` /
  `from portfolio_common.kg_schema import X` import in this repo until this
  plan is executed.

See `portfolio-common`'s `business_folders/financial_analysis/README.md` for
the exact file inventory and `CHANGELOG.md` for the full rationale.

## What to pull in

1. Copy `portfolio-common/business_folders/financial_analysis/kg_schema/`
   into this repo as `src/kg_schema/` (a new top-level package alongside
   `src/api`, `src/quant`, `src/pricing_agent`, etc.) — or another location
   if you'd rather nest it under an existing package; `src/kg_schema/` keeps
   every existing `portfolio_common.kg_schema.X` import a one-line rename to
   `kg_schema.X`.
2. Copy its `tests/` alongside our own `tests/` (e.g. `tests/kg_schema/`),
   merging with whatever pytest layout this repo already uses.
3. Copy its `docs/` content into `docs/kg_schema.md` if useful context beyond
   what's already there.
4. Delete `business_folders/financial_analysis/` from `portfolio-common` once
   the copy is verified working here (a follow-up PR against
   `portfolio-common`, not part of this repo's change).

## Import updates

Grep this repo for `portfolio_common.kg_schema` / `from portfolio_common
import kg_schema` and replace with the new local `kg_schema` package. Known
current call sites (confirmed by search when this plan was written — re-grep
before executing, this list may be stale):

- `src/api/db.py`, `src/api/dependencies.py`, `src/api/routers/universe.py`,
  `src/api/config.py`
- `src/quant/db.py`, `src/quant/cli.py`, `src/quant/config.py`,
  `src/quant/returns.py`, `src/quant/persist.py`, `src/quant/evaluate.py`,
  `src/quant/actions.py`
- `src/pricing_agent/db.py`, `src/pricing_agent/pipeline.py`,
  `src/pricing_agent/config.py`, `src/pricing_agent/cli.py`
- `src/fundamental_agent/db.py`, `src/fundamental_agent/pipeline.py`,
  `src/fundamental_agent/config.py`, `src/fundamental_agent/cli.py`
- `src/entity_resolution/news_db.py`, `src/entity_resolution/db.py`,
  `src/entity_resolution/pipeline.py`, `src/entity_resolution/config.py`,
  `src/entity_resolution/cli.py`
- `src/cycle/db.py`, `src/cycle/orchestrator.py`, `src/cycle/data.py`,
  `src/cycle/config.py`, `src/cycle/cli.py`
- Tests: `tests/test_entity_resolution.py`, `tests/test_quant_pipeline.py`,
  `tests/test_quant_ddl.py`, `tests/test_pricing_db.py`,
  `tests/test_kg_schema.py`, `tests/test_db.py`, `tests/test_cycle.py`,
  `tests/test_coverage.py`

## Adopt the Database engine + injection-safety helpers for our own ad hoc SQL

This repo has 7 of its own `db.py`-style modules that open `sqlite3`
connections and build dynamic SQL directly (mostly delegating connection
creation to `kg_schema.connect`/`connect_ro` already, which is good — but
several build `IN (...)` clauses or dynamic `SET`/`ORDER BY` fragments by
hand). Once `kg_schema` is copied in (and itself rewritten to use
`portfolio_common.db.Database` internally), replace the following with
`portfolio_common.db.in_clause` / `portfolio_common.db.Allowlist` (still a
`portfolio-common` runtime dependency for just these two primitives — keep
that dependency even after `kg_schema` is vendored):

- `src/quant/db.py:440` — `f"SELECT id, sector_id FROM assets WHERE id IN ({marks})"`
  → `in_clause(asset_ids)`
- `src/pricing_agent/db.py:385` — `f"UPDATE pricing_run SET {column} = {column} + 1 ..."`
  → the existing `raise ValueError(f"not a counter column: {column}")` guard
  becomes an `Allowlist` check
- `src/fundamental_agent/db.py:551` — same pattern as pricing_agent, for
  `analysis_run`
- `src/cycle/data.py:38` — `f"SELECT id, ticker, sector_id FROM assets WHERE id IN ({placeholders}) ..."`
  → `in_clause(...)`
- `src/api/routers/runs.py:34` — `f"SELECT ... FROM {_VIEW[k]}"` — `_VIEW[k]`
  is already a fixed-dict lookup; wrap it in an `Allowlist` for a
  self-enforcing guarantee instead of a comment

## `queries.py` convention

Every query function (in `kg_schema` and in these 7 modules) should live in
one ordered, documented place per domain — a module (or package `__init__`)
docstring table-of-contents listing every query function with a one-line
purpose, separate from orchestration/business logic. Apply this convention
going forward for new queries in this repo, matching how
`business_folders/financial_analysis/` was organized upstream.

## Version pin

Once this repo's own tests pass against the vendored `kg_schema`, bump
`pyproject.toml`'s `[tool.uv.sources]` pin:

```toml
portfolio-common = { git = "https://github.com/gamug/portfolio-common", tag = "v1.0.0" }
```

## Verification

- `uv sync`, `uv run pytest`, `uv run ruff check .`, `uv run mypy src` all
  pass with the vendored `kg_schema` and updated imports.
- Manually exercise at least one CLI path per subpackage
  (`quant/cli.py`, `pricing_agent/cli.py`, `fundamental_agent/cli.py`,
  `entity_resolution/cli.py`, `cycle/cli.py`) against a scratch DB to confirm
  the vendored `kg_schema.connect`/`ensure` still bring up schema correctly.

---
🤖 Generated with [Claude Code](https://claude.com/claude-code) as part of the
portfolio-common v1.0.0 DB-engine/business_folders split.
Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
