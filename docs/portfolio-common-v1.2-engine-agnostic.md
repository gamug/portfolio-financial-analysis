# Coordination note: `portfolio-common` v1.2.x — engine-agnostic seam

## Context

`portfolio-common` v1.2.0 / v1.2.1 make the database engine a single-repo
concern: a `Dialect` seam, `Database.connect_url`, schema-introspection
helpers (`table_columns` / `table_exists` / `ensure_columns` / `create_schema`
/ `relation_exists` / `relation_kind` / `relation_ddl` / `schema_version`), a
neutral `Row` type + `RowLike` contract, and `DatabaseError` (an engine-neutral
alias for `sqlite3.OperationalError`). See `portfolio-nlp`'s
`docs/engine-agnostic-rollout.md` for the full cross-repo plan.

## What changed here (Phase 4)

- **`pyproject.toml`** — `[tool.uv.sources]` `portfolio-common` `tag = "v1.0.0"`
  → `tag = "v1.2.1"`. Additive release; the pin bump alone left ruff + mypy +
  pytest green.
- **No `import sqlite3` in non-test `src/`** (was in 14 modules):
  - `sqlite3.Row` type hints → `portfolio_common.db.Row` (`quant/db.py`,
    `cycle/data.py`, `cycle/orchestrator.py`, `fundamental_agent/db.py`,
    `fundamental_agent/pipeline.py`, `pricing_agent/db.py`).
  - `except sqlite3.OperationalError` / `sqlite3.Error` →
    `except portfolio_common.db.DatabaseError` (~12 sites across `quant`,
    `kg_schema`, `api`, `fundamental_agent`).
  - `isinstance(row, sqlite3.Row)` → `isinstance(row, Row)`.
- **Introspection / DDL through `Database`:**
  - `PRAGMA table_info(x)` → `db.table_columns("x")`
    (`kg_schema/migrations.py`, `fundamental_agent/db.py` ×3,
    `pricing_agent/db.py`).
  - `SELECT 1 FROM sqlite_master WHERE name = ?` → `db.relation_exists(name)` /
    `db.relation_kind(name) == "view"` (`kg_schema/migrations.py`).
  - `SELECT sql FROM sqlite_master ...` → `db.relation_ddl("score_snapshot")`
    (`kg_schema/migrations.py` m005/m006).
  - `db.executescript(DDL)` → `db.create_schema(DDL)` (all 11 sites:
    `kg_schema/{__init__,queries,migrations}.py`, `quant/db.py`,
    `fundamental_agent/db.py`, `pricing_agent/db.py`).
  - `kg_schema._add_missing_columns`'s `sqlite_master` + `PRAGMA table_info` +
    `ALTER TABLE ADD COLUMN` loop → one `db.ensure_columns(table, columns)`
    per table.
- **`.code_quality/ruff.toml`** — `target-version` `py310` → `py312` (matches
  `requires-python`); no reformatting fallout.

## What is deliberately left as SQLite-flavoured SQL text

Held only as strings passed to `db.execute` — not an engine *import* — and
flagged in `src/kg_schema/__init__.py`'s docstring:

- **DDL dialect** in `kg_schema/ddl.py` + each agent's `SCHEMA`: `INTEGER
  PRIMARY KEY` rowid aliases, partial indexes (`... WHERE valid_to IS NULL`),
  inline `CHECK (... IN (...))`, `ON DELETE CASCADE`. Standard-ish; only the
  `AUTOINCREMENT` token has a `dialect.autoincrement_pk` seam.
- **`INSERT OR IGNORE` (×18) and `INSERT ... ON CONFLICT (...) DO UPDATE
  SET ... excluded.*` (×20)** in the agents' `db.py` write helpers, `cycle`,
  `entity_resolution`. `ON CONFLICT` is already SQLite+PostgreSQL; the seam
  for the rest is `conn.dialect.insert_or_ignore` /
  `conn.dialect.upsert(update=/do_nothing=)`.
- **`json_extract` / `json_each` (×12)** in `kg_schema/views.py`'s 30
  read-contract VIEWs, alongside correlated "latest engine_version"
  subqueries and `REPLACE`/`||` string funcs. The VIEW layer and the
  `kg_schema/migrations.py` rebuild scripts (the `CREATE <t>__new` / copy /
  drop / rename dance + `PRAGMA foreign_keys` toggles) are SQLite's own
  ALTER-workaround — a non-SQLite migration/read path is written fresh, not
  translated.

## Verification

- `uv sync --frozen --group dev` (lock updated to `v1.2.1`)
- `uv run ruff check --config .code_quality/ruff.toml .` — clean
- `uv run ruff format --config .code_quality/ruff.toml --check .` — clean
- `uv run mypy --config-file=.code_quality/mypy.ini src` — clean (105 files)
- `uv run pytest -q` — **203 passed**
- `grep -rn "import sqlite3" src` — empty

## Companion PRs

- `portfolio-common#9` (v1.2.0), `#10` (v1.2.1) — the seam. Merged, tagged.
- `portfolio-nlp#24` — Phase 2. Merged.
- `portfolio-knowledge-graph#9` — Phase 3. Merged.
- `portfolio-data-mining` — Phase 5, tracked in
  `portfolio-nlp/docs/engine-agnostic-rollout.md`.

---
🤖 Generated with [Claude Code](https://claude.com/claude-code)
Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
