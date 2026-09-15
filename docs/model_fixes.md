# Model & methodology fixes

A durable, dated record of every fix to this repo's deterministic computation
methodology — a ratio formula, a data-quality gate, a veto-rule threshold, a
scoring weight, an estimator — required by constitution.md's AI behavior #12.
Where `docs/portfolio-common-v1.2-engine-agnostic.md` and its siblings record
*infrastructure* migrations, this file records *analytical-methodology*
changes: what was wrong, how that was verified (not assumed), what
theoretical/technical reference justifies the chosen fix, and what changed.

Each entry follows the same shape: **Status**, **Symptom**, **Root cause**,
**Theoretical/technical reference**, **Fix**, **Design decisions**,
**Verification**, **Residual scope / deliberately deferred**. Cross-referenced
from `.specify/memory/PLAN.md`'s Work item 7 (the 2026-09-08 forensic audit)
and `TASKS.md`'s `T-06x` tasks — this file is the detailed record those
documents point to rather than restate.

---

## F1 — Share-count scale-tagging defect (diluted/outstanding shares off by an exact power of ten)

**Status**: Fixed 2026-09-14 (branch `fix/f1-share-count-scale-defect`,
`T-060`).

### Symptom

Verified directly against the live production database
(`data/financial.db`, via `KG_FINANCIAL_DB`), 2026-09-14:

- **MCD** (`asset_id` 305): `fundamental_metrics.market_capitalization` for
  the 10-K filed for FY2025 (`sec_filings.id` 2884) is stored as
  **$218,953.34** (should be ≈$219B — six orders of magnitude off);
  `free_cash_flow_yield` correspondingly reads in the tens-of-thousands-of-
  percent range.
- **WAT** (`asset_id` 486): `market_capitalization` for the most recent 10-Q
  (`sec_filings.id` 4673, period_end 2026-07-04) is stored as
  **$37,247,795,999,145.51** (≈$37.2 **trillion**; should be ≈$37.2
  **billion** — again six orders of magnitude off, in the opposite
  direction).
- Both distortions feed directly into `enterprise_value` and every FCF-yield
  metric derived from `market_cap`/`enterprise_value` in the same filing's
  `valuation` group, and (per `PLAN.md`'s Work item 7 §Q1) propagate further
  into `quant`'s equilibrium-μ estimator once these assets enter its panel.

### Root cause — corrected from the original audit's stated mechanism

The 2026-09-08 forensic audit that first reported this finding (its own
source markdown, `feedback_plan.md`, was deleted per the auditor's
instruction after being folded into `PLAN.md`) attributed it to *"raw XBRL
units are multiplied directly by share price without scaling checks"* —
implying a missing application of an XBRL `decimals`/`scale` attribute.
**That stated mechanism does not exist in this repo's data model.** Direct
inspection of `src/fundamental_agent/statements.py`'s parsing, the
`financial_facts` schema (`src/fundamental_agent/db.py`), and real captured
EDGAR-gateway responses (`tests/fixtures/financials_*.json`) confirms the
gateway's `/financials/{ticker}` endpoint returns an already-tabulated,
fully-scaled numeric view — there is no `val`/`decimals`/`unit` attribute
anywhere in the payload or the schema to "apply."

Querying `financial_facts` directly instead revealed the true mechanism —
an exact power-of-ten tagging defect on the
`us-gaap_WeightedAverageNumberOfDilutedSharesOutstanding` concept
specifically (not a whole-filing corruption: the same filings' `us-gaap_
Assets`/`us-gaap_Revenues`/`us-gaap_NetIncomeLoss` are all correctly
scaled throughout):

- **MCD**: the concept was reported correctly (e.g. `751,800,000`) through
  the 10-K filed for FY2022 (`sec_filings.id` 2881). Starting with the very
  next 10-K (FY2023, `id` 2882) and in **every filing since** (6 consecutive
  filings spanning ~2.5 years, including each one's own *restated*
  prior-period columns), the concept is reported divided by **exactly
  1,000,000** — `751.8` instead of `751,800,000`.
- **WAT**: the concept was reported correctly (~59-68M) through the 10-Q
  filed 2025-09-27 (`id` 4672). The single most recent 10-Q (`id` 4673,
  period_end 2026-07-04) reports it multiplied by **exactly 1,000** relative
  to the immediately preceding filing's own value for the overlapping
  period.

### Theoretical/technical reference

This is a documented, named class of SEC XBRL filing-quality defect,
independently verified against SEC.gov (not recalled from memory):

- SEC Division of Economic and Risk Analysis staff notice, ["Scaling Errors
  Between Entity Common Stock Shares Outstanding and Common Stock Shares
  Outstanding"](https://www.sec.gov/newsroom/whats-new/osd-announcement-110520-scaling-errors):
  *"some filers with no disclosed changes in capital structure had
  differences between the two reported values of more than a hundredfold...
  some filers disclosed three additional zeros in one reported value
  compared to the other."*
- SEC OSD staff notice, ["Scaling and Tagging Errors for Entity Common Stock
  Shares Outstanding and Common Stock Shares
  Outstanding"](https://www.sec.gov/newsroom/whats-new/osd-announcement-2210-dqreminder-entitycommonstocksharesoutstanding):
  *"Absent subsequent events after the balance sheet date that affect the
  number of shares of common stock outstanding, the fact values for
  `dei:EntityCommonStockSharesOutstanding` and
  `us-gaap:CommonStockSharesOutstanding` should not differ significantly,"*
  citing "tagging 555,555,000 shares as 555,555 shares" as an observed
  example — the same class of error (off by a clean power of ten), just a
  different multiple.

This repo does not ingest the `dei:` cover-page tag, so the SEC's own
recommended cross-tag pair isn't directly available. The fix generalizes the
same underlying principle — *two independently-disclosed facts for the same
thing shouldn't disagree, absent a real event* — to two signals this repo's
own ingested data already supports (see Fix, below).

### Fix

`src/fundamental_agent/statements.py`:
- `Statements.get_all(item)` — every period-column value on a registry
  item's matched row (not just one period, as `.get()` returns), needed to
  find an overlapping period against history.
- New registry item `eps_diluted` (`us-gaap_EarningsPerShareDiluted`).

`src/fundamental_agent/db.py`:
- `overlapping_history(...)` — already-ingested `financial_facts` values for
  an asset/concepts, restricted to periods the current filing also reports,
  excluding the current filing itself.
- `_eps_implied_diluted_shares(stmts)` — `net_income ÷ as-filed diluted EPS`
  per period, from the current filing's own payload alone.
- `detect_share_scale_factors(conn, asset_id, stmts, *, exclude_filing_id)`
  — for `shares_outstanding` and `diluted_shares` independently, checks
  **two signals, either sufficient**:
  1. **Temporal**: does this filing's own reported value, multiplied by a
     candidate power of ten (`1e-9`…`1e9`, within a 10% tolerance), match an
     already-ingested prior filing's value for an overlapping period?
  2. **EPS cross-check** (`diluted_shares` only — EPS is duration-based like
     diluted shares, never point-in-time like `shares_outstanding`): does
     the filing's own `net_income ÷ EPS_diluted` (no history needed) match
     the reported value under a candidate power-of-ten factor?

  Returns `{}` for an item where neither signal finds evidence, or where the
  two signals (or two overlapping periods within one signal) disagree on the
  factor — left ambiguous rather than guessed.

`src/fundamental_agent/pipeline.py::_analyze_one` — calls
`detect_share_scale_factors` right after `append_financial_facts` (so the
current filing's own facts are already queryable to exclude), threads the
result into `FilingContext`.

`src/fundamental_agent/agents.py` — `FilingContext.share_scale_factors`
field; `_compute_all` passes it to `valuation_metrics.compute`.

`src/fundamental_agent/metrics/valuation.py` — `_ShareCount` (a small frozen
dataclass replacing the old 2-tuple return) carries an optional
`scale_correction_factor`; `_share_count` applies it before market cap is
computed; `compute()` gains an optional 4th parameter (default `None` →
today's behavior, fully backward compatible) and records
`inputs["shares_scale_correction_factor"]` when a correction was applied.

### Design decisions

**Auto-correct when unambiguous, not quarantine-only.** When either signal
identifies a single power-of-ten factor, the value is corrected and used —
recorded in the metric's own `inputs` JSON for full audit traceability —
rather than nulled out. This is treated as a deterministic unit conversion
(the same category of operation as `Statements._instant_for`'s own
nearest-earlier-column resolution), not as "fabricating a value" under
constitution AI behavior #2 (which governs *missing* inputs, not a
mechanically mis-scaled *present* one). Ambiguity (the two signals, or two
overlapping periods, disagreeing) is the one case left untouched rather than
guessed.

**A second signal (EPS cross-check) had to be added mid-implementation.**
The originally-designed temporal-overlap signal alone — validated by a
dedicated design review and by synthetic tests reproducing the reported bug
shapes — was verified *read-only, against the actual live MCD/WAT filings*
before being declared done, per this repo's "verify against real data"
standard (constitution AI behavior #12). That check returned `{}` for
**both** real cases in their *current* state:

- MCD's defect has now persisted through 3 consecutive 10-Ks; each 10-K
  carries only 3 fiscal years of history, so by the current filing every
  period it reports was *already* corrupted from birth — no clean,
  pre-defect filing remains anywhere reporting those same periods.
- This repo ingests only one 10-Q per calendar year per company; WAT's
  ingested quarter's exact period-end date shifted between cycles (plausibly
  related to the concurrent Waters/BD reverse-Morris-Trust merger), so its
  bad filing's restated periods were never independently reported by *any*
  other filing in the database — no overlap exists to compare against, ever.

Both are structural blind spots of *any* purely temporal cross-filing
detector, not an implementation bug. The EPS-based signal — a same-filing,
same-period internal consistency check needing no history at all — closes
both gaps and was verified, again read-only against the live data, to
recover the correct factor for both real cases before being adopted (see
Verification).

**EPS never corroborates `shares_outstanding`.** EPS is a weighted-average,
duration-based measure; `shares_outstanding` is a point-in-time,
instant-dated balance-sheet figure. Cross-checking one against the other
would be a category error, so the EPS signal only ever corroborates
`diluted_shares`.

### Verification

Read-only, against the live production database (no writes, no LLM calls),
2026-09-14, using the actual fixed `detect_share_scale_factors`:

| | MCD (`id` 2884, FY2025 10-K) | WAT (`id` 4673, 10-Q, period_end 2026-07-04) |
|---|---|---|
| Detected factor | `diluted_shares: 1,000,000.0` | `diluted_shares: 0.001` |
| Reported (bad) diluted shares | `716.4` | `98,204,000,000.0` |
| Corrected diluted shares | `716,400,000` | `98,204,000` |
| Currently-stored (bad) market cap | `$218,953.34` | `$37,247,795,999,145.51` |
| Corrected market cap (period-end price × corrected shares) | **$218,953,335,498.05** (≈$219B) | **$37,247,795,999.15** (≈$37.2B) |

WAT's corrected figure (≈$37.2B) is *higher* than the ≈$22B figure the
original (deleted) audit approximated — that number matches WAT's **FY2025
10-K** (`sec_filings.id` 4668, `market_capitalization` = `$22,678,129,178.28`,
never corrupted), a different, earlier filing than the one this fix
verifies here. WAT's real diluted share count genuinely grew between those
two filings (~59.7M → ~98.2M), consistent with the concurrent BD-Biosciences
reverse-Morris-Trust share issuance — so ≈$37.2B for the *later* filing and
≈$22.7B for the *earlier* one are both plausible, independent data points,
not a contradiction.

Code-level verification:
- `uv run pytest -q` — 214 passed (was 203; +11 new: `tests/test_share_scale.py`
  ×7, `tests/test_statements.py` ×1, `tests/test_metrics_valuation.py` ×3),
  including dedicated regression tests for both the MCD shape (÷1e6,
  persisting past the reporting window) and the WAT shape (×1e3, isolated
  filing, no overlap), each proven to fail on the temporal-only signal and
  pass once the EPS signal is added.
- `uv run ruff check` / `ruff format --check` / `uv run mypy` — all green.
- `git diff` review: `valuation.py` stays free of any DB import (the
  architecture boundary — `metrics/*` are pure functions of `Statements`);
  all DB access lives in `db.py`.

**Not done as part of this change** (explicitly, not an oversight): actually
re-persisting corrected `fundamental_metrics`/`score_snapshot` rows for
MCD/WAT (or the rest of the universe) requires re-running
`fundamental_agent run --fresh` for the affected filings against the live
production database — a paid-LLM, production-data-mutating operation
outside a code-review pass's authority to run unprompted. The code fix is
verified correct and ready; applying it to the live data is a follow-up
operational step for whoever owns that run.

### Post-merge correction: a code-review bot caught two real gaps

An automated PR review ([Sourcery](https://sourcery.ai)) flagged two
`db.py` issues before merge, both confirmed as genuine bugs, not noise, and
fixed the same day (2026-09-15):

1. **A factor found from one period could contaminate an unrelated one.**
   The original design collected matched factors across every anchored
   period in a filing and, once exactly one distinct factor emerged,
   applied it to whatever period `_share_count` happened to be asked about
   — even a period that itself had direct, contradicting evidence of being
   correctly scaled (e.g. a filing that mis-restates an old period while
   its own current period is fine). **Fix**: `detect_share_scale_factors`
   now takes the actual target `period_key`; direct evidence *at that
   period* (from either signal) is checked first and is decisive —
   including when it proves the period needs no correction, which now
   overrides any factor a signal would otherwise infer from a *different*
   period in the same filing. Only with no direct evidence at the target
   does the detector fall back to inferring from the filing's other
   periods, and only if *every one* of them (not just one) agrees on the
   same factor — one period that fits no known defect shape now disqualifies
   the inference outright rather than being silently skipped. New
   regression tests: `test_direct_target_evidence_overrides_a_different_
   defective_period` (both directions — the defective period still
   corrects, the clean one in the same filing does not).
2. **The candidate factor list only covered thousands-grouped scales**
   (`1e-9, 1e-6, 1e-3, 1e3, 1e6, 1e9`) — an artifact of MCD and WAT both
   happening to show that grouping, not a real constraint; nothing rules
   out a filer being off by 10x or 100x. **Fix**: widened to every power of
   ten from `1e-9` to `1e9` (18 candidates, excluding `1e0`). New test:
   `test_detect_share_scale_factors_catches_a_non_thousands_power_of_ten`
   (a synthetic 100x defect).

`fundamental_agent.pipeline._analyze_one`'s call site was updated to pass
`target.period.key`; `Statements` gained `resolve_column(item, period_key)`
so a balance-sheet item's instant-date key resolves the same way `.get()`
already does internally, keeping `shares_outstanding` and `diluted_shares`
addressable by the one duration-style `period_key` the pipeline actually
has. Re-verified read-only against the live production database after the
fix: `detect_share_scale_factors` still recovers `{"diluted_shares":
1000000.0}` for MCD's FY2025 10-K and `{"diluted_shares": 0.001}` for WAT's
most recent 10-Q under the stricter logic. Full suite: 216 passed (was 214
before this correction; +2 new regression tests, on top of the +11 from the
original fix).

### Residual scope, deliberately deferred

- **`shares_outstanding` with no temporal overlap and no EPS corroboration**
  (EPS only ever corroborates `diluted_shares`) is not caught by this fix. A
  magnitude/plausibility backstop independent of both temporal continuity
  and per-share disclosures — `PLAN.md`'s `DQ_MCAP_SCALE`
  (`market_cap / total_assets` outside `[0.001, 100]`) — remains a separate,
  later task (`T-065`, Work item 7), gated on the `data_quality_issue` table
  landing upstream in `portfolio-common` (Work item 5 / `T-040`). This fix
  does not depend on or block that one.
- The SEC's own recommended `dei:EntityCommonStockSharesOutstanding` vs.
  `us-gaap:CommonStockSharesOutstanding` cross-tag check is not implemented
  — this repo doesn't ingest the `dei:` tag today; adding it is a separate
  ingestion-layer change with its own cost/benefit case.
- A genuinely ambiguous case (the two signals, or two overlapping periods,
  disagreeing on the factor) is left untouched, not quarantined into a
  `data_quality_issue` audit trail — that table doesn't exist yet (Work item
  5). Once it does, wiring this fix's ambiguous-case branch to it is a
  natural, small follow-up, not a redesign.
