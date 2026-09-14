# Project Constitution

Governing principles for `portfolio-financial-analysis` under a spec-driven
("spec coding") workflow: specs and plans are written before implementation,
and this document is the fixed reference they must not contradict. A spec or
plan that conflicts with a rule below must change the rule here first (see
Governance) rather than override it silently.

## Technological stock

`portfolio-financial-analysis` is a headless Python service — six CLI-driven
packages plus a read-only FastAPI surface, no UI of its own; every rule below
assumes the stack actually pinned in `pyproject.toml`.

1. **Runtime**: Python `>=3.12,<3.13`, dependency-managed with `uv` (lockfile
   `uv.lock`). Do not add a second package manager (pip/poetry/conda) — all
   installs go through `uv sync` / `uv add`.
2. **Analytical core**: deterministic ratio/statistics code (profitability,
   liquidity, leverage, efficiency, growth, cash-flow, ROIC, CAGR, valuation;
   Ledoit-Wolf covariance, equilibrium/James-Stein `mu`) plus one narrow LLM
   step — a [Strands](https://strandsagents.com) *metrics-master* agent
   (`fundamental_agent`) that consults specialist agents over the
   already-computed ratios to produce a narrative `FundamentalAssessment`.
   This repo has no trained/fine-tuned model of its own and no
   `transformers`/`torch` dependency — the LLM is an OpenAI-compatible chat
   endpoint (`LLM_API_KEY`/`LLM_MODEL`/`LLM_URL`, today DeepSeek) consulted at
   inference time, not a pinned checkpoint. `quant/`'s numeric stack (numpy /
   scipy / cvxpy / clarabel) is the repo's only heavy numeric dependency,
   confined to that one leaf package and pinned there by
   `tests/test_quant_import_isolation.py` — no other package may import it.
3. **Web/service layer**: FastAPI + `uvicorn[standard]` for the read-only
   `api/` package (`:8010`); `httpx` for the EDGAR and pricing gateway calls;
   `pydantic>=2.6` for request/response and settings models;
   `beautifulsoup4`/`lxml` for narrative SEC filing HTML (`fundamental_agent
   run --sections`).
4. **Storage**: SQLite, one shared `KG_FINANCIAL_DB` written by every
   analysis package, accessed through `portfolio_common.db.Database` /
   `in_clause` / `Allowlist` plus (since `v1.2.1`) its `Dialect` seam,
   schema-introspection helpers (`table_columns`/`relation_exists`/
   `relation_kind`/`relation_ddl`/`create_schema`/`ensure_columns`), the
   neutral `Row` type, and `DatabaseError` (git-tag-pinned `portfolio-common`,
   currently `v1.2.1` in `[tool.uv.sources]`) — a DB-engine-contract change is
   an explicit, reviewed re-pin, not a floating version. No non-test module
   under `src/` imports `sqlite3` directly anymore (`docs/portfolio-common-
   v1.2-engine-agnostic.md`); what stays SQLite-flavoured is held only as SQL
   *text* (the DDL dialect, `INSERT OR IGNORE`/`ON CONFLICT ... DO UPDATE`
   writes, `json_extract`/`json_each` in the `v_*` views), routed through
   `conn.dialect.*`, not a driver import. The domain schema itself (DDL,
   non-additive migrations, the `v_*` read-contract views, point-in-time
   universe reads, provenance, the `coverage` command) is **vendored
   locally** at `src/kg_schema/`, not part of `portfolio-common` (see
   `docs/kg_schema.md`, `docs/portfolio-common-v1-migration-plan.md`) —
   don't reintroduce a `portfolio_common.kg_schema` import. Two read-only
   companion databases are opened `mode=ro` and never written: `universe.db`
   (`KG_UNIVERSE_DB`, point-in-time S&P 500 membership) and `urls.db`
   (`KG_NEWS_DB`, `entity_resolution`'s news co-occurrence source). Chosen
   for zero-ops single-file deployment at this corpus scale; moving off
   SQLite is a `portfolio-common`-level decision, not a change made here.
5. **Package isolation over dependency groups**: unlike a repo that isolates
   a heavy stack behind an optional `[dependency-groups]` extra,
   `quant/`'s numeric stack is always installed but structurally kept off
   every other package's import path (no package other than `quant`/`api`
   entrypoints imports it) — the isolation contract is enforced by a test,
   not by an opt-in install group. A new heavy dependency for one package
   should follow this same pattern (leaf package + an import-isolation test)
   before reaching for a new dependency group.
6. **Licensing**: every pinned dependency must carry a license compatible
   with this repo's own — flag anything copyleft or usage-restricted in the
   PR that introduces it.
7. **Adopting a new library/framework is a constitution-level change**: add
   it to `pyproject.toml` with a rationale in the PR, and if it changes a
   rule above, amend this section (see Governance).

## Project structure

1. **`src/` is one flat package per subsystem**, no nested sub-packages
   beyond what already exists: `kg_schema/` (passive, shared schema),
   `fundamental_agent/`, `pricing_agent/`, `cycle/`, `entity_resolution/`,
   `quant/`, `api/`. A new top-level package needs the same justification
   `quant/` has — a genuinely independent subsystem with its own numeric or
   service dependencies that must not leak onto other packages' import path
   — not just "this file is getting long."
2. **Every package is a `python -m <package>` CLI** (argparse), installed
   editable via the `[tool.hatch.build.targets.wheel]` list in
   `pyproject.toml` — except `api/`, which is also runnable as
   `python -m api` or `uvicorn --factory api.app:create_app`. Don't invent a
   second entrypoint convention (an `apps/`/`cli/`/`scripts/` split, an
   installed console-script) for a new package; match this one.
3. **Tests live flat under `tests/`** (`test_<module_or_feature>.py`, not
   mirrored into a per-package subdirectory), driven by `tests/conftest.py`
   and `tests/fixtures/`. `pythonpath = ["src"]` / `testpaths = ["tests"]`
   live in `pyproject.toml`'s `[tool.pytest.ini_options]` — there is no
   separate `pytest.ini`; don't add `sys.path` hacks inside test files to
   route around it. `tests/test_quant_import_isolation.py` is a structural
   test (import-graph shape), not a feature test — treat a failure there as
   an architecture violation, not a flaky test.
4. **Docs live under `docs/`**, one topic or package per file
   (`kg_schema.md`, `quant.md`, `api.md`, `cycle.md`,
   `fundamental_agent.md`, `pricing_agent.md`, `entity_resolution.md`,
   `docs/README.md` as the index, plus dated migration/boundary notes —
   `portfolio-common-v1-migration-plan.md`,
   `portfolio-common-v1.2-engine-agnostic.md`, `semantic-score-boundary.md`).
   A
   new cross-cutting doc goes in `docs/`; a spec-kit artifact (this
   constitution, `SPEC.md`, `PLAN.md`, `TASKS.md`) goes under `.specify/`.
   `skills/<ratio-name>/SKILL.md` documents one deterministic ratio
   calculation each (`roic`, `cagr`, `fcf_margin`, `free_cash_flow_yield`,
   `interest_coverage_ratio`) — a new ratio worth a specialist skill gets the
   same treatment, not an inline docstring only.
5. **Config lives where its tool expects it, not duplicated.** Ruff:
   `.code_quality/ruff.toml`. Mypy: `.code_quality/mypy.ini`. Pytest:
   `[tool.pytest.ini_options]` in `pyproject.toml`. Don't fork a second
   config file for a tool that already has one, and don't add a root
   `ruff.toml`/`pytest.ini` pointer file this repo doesn't already have.
6. **Environment**: `.env` (git-ignored) holds `KG_FINANCIAL_DB` /
   `KG_UNIVERSE_DB` / `KG_NEWS_DB` / `LLM_API_KEY` / `LLM_MODEL` / `LLM_URL`
   / `EDGAR_BASE_URL` / `PRICING_BASE_URL` / `API_HOST` / `API_PORT` /
   `API_ROOT_PATH`. This repo currently has **no committed `.env.example`**
   (unlike some sibling repos in this thesis) — the variable table in
   `README.md` is the source of truth today; if a `.env.example` is added,
   keep it in sync with every env var a package's `config.py` reads rather
   than letting the two drift.
7. **Naming**: modules and functions describe the pipeline stage or table
   they own — match the stage/table pairing (fundamental ratios ->
   `score_snapshot[FUNDAMENTAL]` / `fundamental_metrics`, pricing ->
   `price_window`/`price_daily`/`price_observation`, cycle scoring ->
   `score_snapshot[TECHNICAL/VALORIZATION/SECTOR]`, veto ->
   `rule_catalog`/`veto`, quant -> `quant_*`) rather than inventing a new
   term for the same concept.

## AI behavior

*This repo's own AI component (narrow, not a trained model):*

1. **The LLM is a narrative synthesis step over deterministic output, never
   the computation itself.** `fundamental_agent`'s ratios (profitability,
   liquidity, leverage, efficiency, growth, cash-flow, ROIC, CAGR, valuation)
   are plain arithmetic over EDGAR facts; the Strands metrics-master agent
   only turns already-computed ratios into a score/rating/narrative. A
   feature must not route a number that should be computed deterministically
   through the LLM instead.
2. **Fallback is fail-fast for missing inputs, graceful for the LLM call
   itself.** `require_source_text`-equivalent checks (missing EDGAR
   statements, no `price_daily` row for a valuation date) skip the affected
   metric/group loudly (logged, not silently zeroed) rather than fabricating
   a value; but if the LLM synthesis call itself returns nothing usable
   (DeepSeek does not accept OpenAI `response_format` JSON-schema today), the
   code falls back to a rule-based score derived from the same computed
   ratios — it never leaves `fundamental_snapshot`/`score_snapshot` empty for
   a filing that has data.
3. **Ranking, veto, and position math stay deterministic and auditable.**
   `cycle`'s cross-sectional normalization, `rule_catalog`/`veto` evaluation,
   and `quant`'s optimizers are plain, inspectable numeric code — no LLM
   call sits on that path. If a future feature wants an LLM-scored input, it
   must land as its own `score_snapshot` type with clear provenance, not be
   blended silently into an existing deterministic score.
4. **No lookahead, ever.** Every agent's `--analysis-date` (default: today)
   bounds what it reads and writes: the universe is resolved point-in-time
   from `universe.db` as of that date, and a source row dated after it
   (a filing, a price candle, a news article) must be skipped, not ingested.
   A stage that writes something dated after its own `--analysis-date` is a
   bug, not an edge case — this guarantee is what lets a downstream report
   replay "what did we believe as of D."
5. **Outputs are decision-support, not investment advice.**
   `FundamentalAssessment`, the TECHNICAL/VALORIZATION/SECTOR/SEMANTIC
   `score_snapshot` rows, `veto` flags, `portfolio_position` targets, and the
   `quant` Markowitz benchmark are portfolio-construction inputs for the
   downstream report — nothing in this repo should present them as verified
   fact or a trading recommendation.

*Claude Code / coding-agent conduct on this repo:*

6. **Match existing structure before introducing new structure** — check
   where a file's siblings live and follow that package's placement, naming,
   and CLI-argument conventions (`--analysis-date` as the canonical name,
   `--date`/`--as-of` only as documented aliases) rather than a generic
   layout.
7. **This constitution and `.specify/memory/SPEC.md` are the binding
   reference for planning and review** — read both before drafting a
   spec/plan, and resolve any conflict between a request and a stated
   principle or requirement by surfacing it or proposing an amendment, not
   by quietly overriding either. A local, untracked `CLAUDE.md` may carry
   situational/session notes, but it is never authoritative and must not be
   treated as a source of fact for anything either document already states.
8. **Prefer the smallest change consistent with the existing pattern**; no
   opportunistic refactors, renames, or new abstractions outside what the
   spec/task calls for.
9. **`CLAUDE.md` must always exist on disk and must never be deleted**,
   even though it is intentionally untracked, and it must always carry a
   reference to both this constitution (`.specify/memory/constitution.md`)
   and `.specify/memory/SPEC.md`. If `CLAUDE.md` is missing at the start of
   a session, run `/init` to regenerate it before doing anything else; if it
   exists but is missing either reference (freshly `/init`-generated or
   otherwise edited), add it before proceeding — don't treat the reference
   as a one-time regeneration step.
10. **Ask before expanding scope this constitution doesn't cover** — a new
    external gateway/service, a new heavy dependency, a schema change to
    `KG_FINANCIAL_DB`, a change to the `v_*` read-contract views the
    knowledge-graph repo depends on, or anything touching the cross-repo
    SEMANTIC-score boundary (`docs/semantic-score-boundary.md`).
11. **Reconcile the architecture artifacts at the close of every
    development effort** — when a PR/feature/fix is done (merged, or ready
    to merge), update both:
    - the general, system-wide artifact — [Portfolio
      Thesis](https://claude.ai/code/artifact/d3865a63-2894-4e20-b38a-7e50cf0d4040)
      (the six-repo integrated architecture overview); and
    - the repository-specific artifact — [Portfolio Financial
      Analysis](https://claude.ai/code/artifact/bfc6efde-aecd-4408-83b8-081bc3abccb0)
      (this repo's component flow, package table, gaps, and plan).

    to close whatever gaps the effort closed and reconcile the artifact's
    prose with what the code now actually does — an artifact describing a
    gap that was just fixed, or a plan step that was just built, is now
    wrong and must be corrected in the same pass, not left stale. **Never**
    rename either artifact when doing this — **NEVER** change its title
    (the `<title>` tag / the name shown in the artifact gallery). Content,
    diagrams, gap lists, and plans update freely; the name is stable
    forever, independent of content changes. (See `Artifact` tool
    guidance: title changes are an explicit, separate, user-directed
    action, never a side effect of a content update.)

## Executable cmds

Canonical commands — a spec/plan should reference these, not invent new
ad-hoc invocations:

```bash
uv sync --group dev                                   # install deps
uv run pre-commit install --install-hooks             # + hooks

uv run python -m fundamental_agent run [--analysis-date D]   # SEC ratio + LLM synthesis
uv run python -m fundamental_agent run --sections             # + narrative filing text
uv run python -m fundamental_agent coverage --analysis-date D # as-of data-coverage report
uv run python -m fundamental_agent migrate                    # non-additive schema migration

uv run python -m pricing_agent run [--analysis-date D] [--store-daily] [--observations]
uv run python -m pricing_agent coverage --analysis-date D
uv run python -m pricing_agent migrate

uv run python -m entity_resolution build --min-weight N [--analysis-date D]

uv run python -m cycle select  --analysis-date D [--top-n 30]
uv run python -m cycle monitor --analysis-date D
uv run python -m cycle backfill --from D1 --to D2 --step-days N

uv run python -m quant backfill-actions [--source derive|gateway]
uv run python -m quant build-returns
uv run python -m quant build-risk-model --analysis-date D
uv run python -m quant optimize --analysis-date D --objectives min_var,tangency,target_vol,frontier
uv run python -m quant evaluate --from D
uv run python -m quant coverage --analysis-date D [--strict]

uv run python -m api                                  # FastAPI :8010
uv run uvicorn --factory api.app:create_app --reload  # dev, docs at /docs

uv run pytest                                # full suite
uv run pytest -q                             # as run in CI

uv run ruff check --config .code_quality/ruff.toml .          # lint
uv run ruff format --config .code_quality/ruff.toml .         # format
uv run mypy --config-file .code_quality/mypy.ini src tests    # types

uv run pre-commit run --all-files            # all of the above hooks, plus hygiene checks
```

1. **CI (`ci.yml`) is the source of truth for the required gate order**, two
   parallel jobs: `quality` (`uv sync --frozen --group dev` → ruff check →
   ruff format --check → `pre-commit run --all-files` [which runs mypy in
   the project venv]) and `tests` (`uv sync --frozen --group dev` →
   `pytest -q`). Run the same checks locally before opening a PR; don't rely
   on CI to catch a lint/type/test failure first.
2. **There is no separate accuracy-evaluation workflow in this repo** (no
   `news_nlp.eval`-style LLM-as-judge subsystem) — don't invent one; the
   closest analogue, `quant`'s `evaluate` command (forward realized return
   vs. the live book), is a CLI command, not a CI gate.
3. **Don't hardcode a different Python/uv invocation** (bare `python`,
   `pip install`, `pytest` without `uv run`) in scripts, docs, or CI — every
   command goes through `uv run` so it resolves the locked environment.

## Code & Git

1. **Formatting/linting/types are enforced, not advisory**: `ruff-check
   --fix` + `ruff-format` + `mypy` (project venv, whole-graph, `src tests`)
   all run via `pre-commit` and again in CI. A `# noqa` / `# type: ignore`
   needs a comment saying why the finding is wrong for this code, not just
   silence.
2. **Commit messages are Conventional Commits**, enforced by the
   `commitizen` pre-commit/pre-push hook — `type(scope): summary`, matching
   the existing history (`feat(api): ...`, `refactor(db): ...`,
   `docs(semantic): ...`, `chore: ...`, `fix: ...`). Reference the PR number
   in the subject once it exists, as the existing log does (`(#29)`).
3. **Branch off `master`, never commit to it directly.** `master` is the
   integration branch (`origin/HEAD -> origin/master`); feature/fix/docs/
   refactor/chore work happens on a descriptively-named branch opened as a
   PR.
4. **Never `git checkout` / `git reset --hard` onto a state that predates
   `.claude/`/`CLAUDE.md` being git-ignored, or that would otherwise discard
   them from disk** — see the `CLAUDE.md`-protection rule under AI behavior.
   Keep local `master` fast-forwarded from `origin/master` instead of
   rewriting it.
5. **Pre-commit hooks are mandatory, not optional**: `check-yaml`,
   `check-case-conflict`, `debug-statements`, `detect-private-key`,
   `check-merge-conflict`, `check-added-large-files` (100 MB) run alongside
   ruff/mypy/commitizen — install them (`uv run pre-commit install
   --install-hooks`) rather than relying on remembering to run checks
   manually.
6. **CI must be green before merge**: both the `quality` and `tests` jobs in
   `.github/workflows/ci.yml` gate every push to `master` and every PR. A PR
   that turns either red does not merge until it's fixed, not suppressed.
7. **No secrets committed.** `.env` stays git-ignored; `detect-private-key`
   is a backstop, not the first line of defense — never paste a real key
   (EDGAR/LLM/pricing gateway credentials) into a commit, issue, or PR
   description to "show" a config.
8. **Leave the working tree checked out on the branch just pushed/PR'd.**
   After opening a PR, don't switch back to `master` (or anywhere else) —
   the local checkout stays on that branch so the user can review the
   actual working tree immediately, without asking for a checkout or doing
   it themselves. Only move off it (per item 3, always to a fresh branch off
   up-to-date `master`) when starting genuinely new work, or when asked to.
9. **Claude Code / agent-tool artifacts are never tracked.** `CLAUDE.md`,
   `.claude/`, and any local agent scratch directory stay git-ignored — see
   `.gitignore`'s "Claude Code / agent tool artifacts" block. `.specify/` is
   the deliberate exception: it holds this constitution and `SPEC.md`/
   `PLAN.md`/`TASKS.md`, the project's own versioned source of truth, not a
   tool scratch dir — it stays tracked. Don't move spec-kit content into
   `.claude/` or vice versa.

## Governance

This constitution supersedes ad-hoc convention when the two conflict. A
spec or plan may not silently contradict a rule above; instead:

1. Propose the amendment as its own change (state which section, what
   changes, and why).
2. Get it reviewed the same way a code PR would be (this repo's normal
   review path) before relying on it.
3. Bump the version below per semver: **MAJOR** for a removed/redefined
   principle, **MINOR** for a new principle or materially expanded
   guidance, **PATCH** for wording/typo fixes.
4. Record the change under "Last Amended" with the date.

Compliance is expected to be checked the same way lint/type/test gates
are — a reviewer (human or agent) rejecting a PR that violates a principle
above should cite the section by name.

**Version**: 1.0.2 | **Ratified**: 2026-09-12 | **Last Amended**: 2026-09-12

**Amendment log**: 1.0.2 (2026-09-12) — PATCH: corrected the skills-doc
path from `skills/skills/<ratio-name>/SKILL.md` to the actual, working
`skills/<ratio-name>/SKILL.md` (verified against
`src/fundamental_agent/skills.py::skills_dir`) — a copy-paste artifact
from adapting this constitution from a sibling repo's, not an intentional
convention; no code changed, only this document's own factual claim.
