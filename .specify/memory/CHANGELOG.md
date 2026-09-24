# CHANGELOG.md — `portfolio-financial-analysis`

Legacy record of **closed** work items, moved here verbatim from
`.specify/memory/TASKS.md` so that file carries only open work. A work item is
closed once every task in it is checked, or explicitly superseded/moved elsewhere.
Task IDs are stable and never reused; `PLAN.md` keeps each work item's plan and
acceptance criteria. Ordered by work item number.

## Work item 1 — Adopt `portfolio-common` v1.2.x (engine-agnostic seam) — DONE

Resolved 2026-09-05, PR #32 (`refactor/engine-agnostic`, commit `8b1fd14`,
merged as `13fcbf9`). Checked off below on re-verification 2026-09-12
(`uv run pytest` 203 passed; `ruff check`/`ruff format --check`/`mypy`
green; `grep -rn "import sqlite3" src/` clean). See `PLAN.md` Work item 1.

- [x] **T-001** Rebase/inspect `refactor/engine-agnostic` against the tip of
      `refactor/rewire-connect-call-sites` (the just-landed single
      `connect()`/`connect_ro()` factory) — confirm which parts are still
      current before resuming. → `PLAN.md` Work item 1, step 1.
- [x] **T-002** Bump `[tool.uv.sources]`'s `portfolio-common` tag from
      `v1.0.0` to the target `v1.2.x` release in `pyproject.toml`. → step 2.
- [x] **T-003** Rewrite `src/kg_schema/db.py`'s connection/row types to the
      neutral `Row`/`DatabaseError` primitives; replace
      `except sqlite3.OperationalError` sites with `except DatabaseError`
      across `kg_schema`/`quant`/`fundamental_agent`/`pricing_agent`/
      `cycle`/`api`. → step 3.
- [x] **T-004** Replace `PRAGMA table_info` with `db.table_columns`;
      replace `sqlite_master` CHECK-widening probes with
      `db.relation_exists`/`relation_ddl`; replace `executescript` with
      `db.create_schema`; replace the `PRAGMA`+`ALTER` missing-columns loop
      with `db.ensure_columns`. → step 4.
- [x] **T-005** Route the remaining SQLite-flavoured SQL (`INSERT OR
      IGNORE`, `INSERT … ON CONFLICT … DO UPDATE SET … excluded.*`,
      `json_extract`/`json_each`) through `conn.dialect.*` rather than
      inline SQL text where the new seam supports it. → step 5.
- [x] **T-006** Full suite green after each package's rewrite:
      `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`,
      `uv run mypy --config-file .code_quality/mypy.ini src tests`. → step 6
      / `PLAN.md` acceptance criteria.
- [x] **T-007** `grep -rn "import sqlite3" src/` (excluding tests) returns
      nothing. → `PLAN.md` Work item 1 first acceptance criterion.
- [x] **T-008** Update `SPEC.md` §13 item 7 to note the re-pin resolved
      (annotate in place, keep the item number) — done in `SPEC.md` itself.
      The two architecture artifacts (Portfolio Thesis + Portfolio Financial
      Analysis) were verified 2026-09-12 to already document the resolved
      state in full (a dedicated section/pill on each, per constitution AI
      behavior #11) — no further edit needed. → `PLAN.md` Work item 1, last
      acceptance criterion.

## Work item 3 — `quant`: a factor-aware μ estimator — SUPERSEDED, see Work item 8

**Superseded 2026-09-12** by Work item 8 step 6 (`T-077`): a Carhart
4-factor estimator with Vasicek beta shrinkage replaces the single-factor
CAPM plan below. Task IDs `T-020`–`T-026` stay unchecked as the historical
record of the superseded plan per this file's own "mark superseded in
place, don't renumber" rule — **do not implement them**; implement `T-077`
instead.

- [ ] **T-020** *(superseded, see above — do not implement)* Add a
      factor-based `ret_estimator` (`mu_i = rf + beta_i ·
      ERP`, betas from the `quant_return_daily` panel vs. a
      market/benchmark series) in `risk.py`, wired into `optimize.py`'s
      `--mu` registry alongside `equilibrium`/`james_stein`/`hist_mean`. →
      `PLAN.md` Work item 3, step 1.
- [ ] **T-021** Load enough of `risk_free_rate` and `benchmark_series` to
      compute ERP over the panel window the new estimator needs — a
      precise, dividend-accurate series is explicitly not required here
      (SPEC.md §13 item 5 stays open). → step 2.
- [ ] **T-022** Keep `equilibrium` the default `--mu`; gate the new
      estimator as opt-in until a comparative `evaluate` run exists. →
      step 3.
- [ ] **T-023** Run `optimize`/`evaluate` with the new estimator against
      `equilibrium` over the same as-of window(s); record forward realized
      return, active return, and Sharpe for both. → step 4.
- [ ] **T-024** Add a unit test for the new estimator analogous to
      `tests/test_quant_lw.py`'s covariance-estimator coverage; confirm
      `tests/test_quant_gate.py`'s score-independence guarantee still
      holds (the estimator never reads `score_snapshot`). → `PLAN.md`
      acceptance criteria, first two bullets.
- [ ] **T-025** Document the before/after comparison in `docs/quant.md`. →
      third acceptance criterion.
- [ ] **T-026** Update `SPEC.md` §13 item 1 with the dated result —
      resolved, or explicitly re-scoped if the comparison doesn't show an
      improvement. → fourth acceptance criterion.

## Work item 6 — Upstream: `portfolio-data-mining` corporate-actions endpoint (P0, external) — implementation MOVED, verification stays

**Moved 2026-09-19**: the yfinance-backed endpoint is data acquisition, not
analysis, so its implementation is tracked in `portfolio-data-mining`
(`.specify/memory/PLAN.md` Work item 3, `T-020`–`T-027`; PR
https://github.com/gamug/portfolio-data-mining/pull/30). `T-050`/`T-051`
stay unchecked here as the historical record, per this file's own "mark
superseded in place" rule — do not implement them in this repo. `T-052`
stays open: it is this repo's consumer-side verification and is now
**blocked on that upstream work landing and being redeployed**.

- [ ] **T-050** *(moved to `portfolio-data-mining` `T-020`–`T-022` — do not
      implement here)* Implement `GET /pricing/{ticker}/actions` (or
      `actions=true` on the existing route) returning
      `{dividends, splits, source}` per the probe's expected contract,
      backed by `yfinance`'s `Ticker(t).dividends`/`.splits`; empty lists
      (never 404) when no actions exist in range. → `PLAN.md` Work item 6,
      step 1.
- [ ] **T-051** *(moved to `portfolio-data-mining`, redeploy is its
      `T-026` handoff — do not implement here)* Redeploy the `:8000`
      gateway service. → step 3.
- [x] **T-052** *(was blocked on `portfolio-data-mining` `T-020`–`T-026`;
      the consumer code it exercises is `T-085` — now the **only** source)*
      Verify: `QuantPricingClient(...).probe('XOM')` returns
      `True`; after `quant backfill-actions` (priority `corpact-v1`),
      XOM/PG/T/NEE show `cash_dividend > 0` in `quant_return_daily`. Run
      it against the *deployed* `PRICING_BASE_URL` (the gateway's `/pricing`
      mount), not a local copy of the service — upstream's `T-026`
      verified the client against the latter only. →
      `PLAN.md` acceptance criteria. **Done 2026-09-21**, live against the
      deployed gateway (`http://host.docker.internal:8000/pricing`; the route is
      `/pricing/pricing/{ticker}/actions`, the single-segment path is a 404).
      `quant backfill-actions --analysis-date 2026-09-21` (run from the `T-085`
      branch, `quant_run` 6, `completed`, exit 0, backup taken first): **503 of
      503 assets fetched, 0 errored, 7,412 dividends + 69 splits = 7,481
      `corpact-v1` rows**; the only tables that changed versus the backup are
      `corporate_action` (+7,481) and `quant_run` (+1). XOM, PG, T and NEE each
      have 19 gateway dividends (all had $0 before); BF.B has 19 through the
      dotted-ticker spelling (`BF-B`); NVDA's 10-for-1 split (2024-06-10) is
      present. On a **scratch copy** of the DB, `build-returns` wrote 580,343
      `qret-v2` rows with 409 assets carrying dividends and
      `SUM(cash_dividend) > 0` for XOM (18.16), PG (18.72), T (5.524), NEE (9.146);
      production `build-returns` was deliberately not run (`T-086` not built,
      `T-088` will rebuild). Independent cross-check of the 77 assets with no
      gateway rows: only GNRC, BSX and APTV had filing-derived dividends, all
      phantom values ($0.001–$0.058) from the removed derivation — none of
      them pays a dividend — while the gateway found dividends for 111 assets
      the derivation had missed; BRK.B (dotted) is a genuine non-payer, and
      HOOD and WAT are non-payers. Notes: ex-dates after the last price bar
      (2026-08-27) — NEE's 2026-08-28, BF.B's 2026-09-03 — cannot fold into
      the return series until prices are refreshed; T shows a 2022-04-11
      `SPLIT` of 1.324 (yfinance's encoding of the WarnerMedia spin-off) —
      splits are recorded for provenance only and never re-applied.

## Work item 10 — P0: the `portfolio-data-mining` corporate-actions gateway as `quant`'s only source (this repo) — DONE

Added 2026-09-20, after PR #45 moved the endpoint's *implementation* to
`portfolio-data-mining` (`T-050`/`T-051`) and left the *consumption* here
un-tasked. **Scope, corrected the same day**: the gateway is the *only* source —
not the primary one with a derived fallback, which was the first draft. Only
`portfolio-data-mining` mines data; no other repository hosts that kind of
service, so `quant`'s XBRL-derived dividend engines are removed. No external
dependency for the code; live verification is `T-052`.

- [x] **T-085** Make the `portfolio-data-mining` pricing gateway `quant`'s
      **only** corporate-actions source, and remove the derivation:
      (1) delete `derive_corporate_actions_from_facts`,
      `derive_quarterly_dividends_from_10q_ytd` and their helpers/constants
      from `src/quant/actions.py`, the `--source` flag, and the report's
      per-source fields — `quant.actions` no longer reads `financial_facts`;
      `load_actions` reads only the gateway engines (`corpact-v2` >
      `corpact-v1`), and rows the retired `corpact-v0-approx` /
      `corpact-v1-derived` engines wrote stay as unread history;
      (2) **fail fast, never degrade**: a failed probe, or the circuit breaker
      opening after `K` consecutive gateway *errors*
      (`QuantSettings.gateway_max_consecutive_failures`, default 3), raises
      `GatewayUnavailable` — `quant_run.status = 'failed'`, CLI exit 1, rows
      already written kept; (3) an asset the gateway cannot serve
      (`ActionsUnavailable` — a non-null `warning`, which upstream returns
      with empty lists when yfinance fails —, `ActionsNotSupported`, or one
      isolated error) gets **no rows** and is listed in the report; the run
      completes and the CLI exits 1 because the data is incomplete; (4)
      `QuantPricingClient`: `GatewayError` (not a bare
      `HTTPStatusError`/`JSONDecodeError`) for an error status or unusable
      body, `probe()` `False` while yfinance is down, and dotted share classes
      requested in yfinance's spelling (`BF.B` → `BF-B`); (5) per-run counts in
      the summary and `quant_run.params_json`; (6) a hermetic
      `httpx.MockTransport` contract test, plus a test pinning that
      `quant.actions` neither reads `financial_facts` nor exposes a derive
      function; (7) update `docs/quant.md`, `README.md`, `SPEC.md` and
      `docs/model_fixes.md`'s Q3 entry (superseded), and correct
      `T-066`/`T-068`/`T-052`. **Live** verification is `T-052`, not this box.
      → `PLAN.md` Work item 10. **Done 2026-09-20**: only gateway *errors*
      count toward the breaker — a per-ticker warning means the gateway
      answered, so it resets the streak, and a global yfinance outage lists
      every asset without tripping it. 27 hermetic tests
      (`tests/test_quant_pricing_client.py`, `tests/test_quant_actions.py`);
      the fixtures that seeded dividends through the derive path now seed
      `corpact-v1` rows directly; full suite (263), ruff, mypy green.
      Mutation-checked: a probe or breaker that no longer fails the run, an
      exit code that ignores errored assets, `load_actions` reading the
      retired engines, ignoring the warning, dropping the dotted-ticker
      spelling, and a breaker that never opens or never resets each fail
      their own test. **Consequences** (deliberate, flagged): with no
      derive path, `quant` had **no dividends until `T-052`** passed *(it did,
      2026-09-21: 7,481 gateway rows in production)*, and
      `build-returns` must follow a successful `backfill-actions` (a
      dividend-less series locks in under `qret-v2`); yfinance is a single,
      unofficial source that cannot tell an unknown symbol from a name that
      paid nothing, so such a symbol stays at `$0` with no error until
      `T-052`'s live check (`BF.B` is the one dotted symbol in the current
      set); `v_corporate_action` still resolves legacy derived rows for the
      knowledge-graph repo (a separate decision); and the constitution's
      "Executable cmds" line still lists `--source derive|gateway` — that
      needs its own governed change (PATCH), not made here.
