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

---

## F2 — Revenue-concept resolution (wrong row wins when a filer reports multiple revenue-tagged lines)

**Status**: Fixed 2026-09-15 (branch `fix/f2-revenue-concept-resolution`,
`T-061`).

### Symptom

Verified directly against the live production database
(`data/financial.db`), 2026-09-15: `fundamental_metrics.net_margin` and
`operating_cash_flow_margin` (both raw ratios, `net_income ÷ revenue` and
`operating_cash_flow ÷ revenue`) are wildly distorted for a real cohort of
filings — e.g. CPT (a REIT) `net_margin = 30.45` (3045%), UDR
`net_margin = 35.535` (3553.5%) — because `revenue` resolved to a small,
non-operating component line instead of the filer's actual total revenue.

### Root cause — corrected from PLAN.md's own stated framing

`PLAN.md`'s original entry for this finding described it as *"a REIT
revenue-concept scaling bug... `statements.py` picks a non-operating line
item as `revenue` for REITs (CPT, UDR, ESS, SBAC, …)"* and its proposed fix
as *"correct the `revenue` XBRL-concept selection **for REIT-classified
filers**."* **This framing is too narrow, and would have left most of the
real cohort unfixed.** `Statements`/`compute_group` carry no sector
information at all (confirmed: `assets.sector_id` never reaches
`FilingContext` or any metrics call site) — there is no way to special-case
"REITs" even if that were the right scope. Live-DB verification found the
identical mechanism in **`APO`** (an alternative asset manager),
**`WFC`** (a bank), **`HUM`** (a health insurer), **`HOOD`** (a fintech),
and **`APA`** (an E&P company) — none of them REITs.

The actual mechanism: `Statements.get()` iterates a statement's rows in
**raw document order** and returns the **first row** whose concept matches
`REGISTRY[item].concepts` — with no priority among the tuple's entries
(tuple order was not actually authoritative anywhere, despite a comment
implying otherwise). When a filer reports **multiple distinct,
non-dimensional revenue-tagged rows** — a component stream (e.g. ASC-842
lease income, or ASC-606 contract revenue) plus, usually, a separately
tagged aggregate "Total revenues" line — whichever appears first in the
filer's own document order wins, regardless of which is the real total.
Verified against real `financial_facts` rows for 6+ filings:

- **CPT**: only `us-gaap_OperatingLeaseLeaseIncome` ("Property revenues",
  $1.57B) + `us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax`
  ("Fee and asset management", $13M) are tagged — **no `us-gaap_Revenues`
  total row exists at all**, and `OperatingLeaseLeaseIncome` wasn't even
  whitelisted. `get()` picked the $13M fee line.
- **UDR, ESS, SBAC, BXP, APO**: the same lease/component-income-first
  shape, but these filings *do* separately tag `us-gaap_Revenues` — it
  just appears *after* the smaller component row in document order, so
  `get()` never reached it.

### Theoretical/technical reference

- **FASB ASC 606** (*Revenue from Contracts with Customers*) governs
  performance-obligation-based revenue (fee/service income;
  `us-gaap_RevenueFromContractWithCustomer{Excluding,Including}AssessedTax`).
- **FASB ASC 842** (*Leases*) governs lessor lease income *separately* from
  ASC 606 — a REIT's or tower company's rental/site-leasing revenue is not
  ASC-606 revenue at all, which is exactly why it needs its own XBRL concept
  (`us-gaap_OperatingLeaseLeaseIncome`) distinct from the contract-revenue
  tags above; a filer with both lease and fee income legitimately reports
  *two* separate, additive revenue-stream facts.
- **SEC Regulation S-X, Article 5** requires an aggregate revenue line in
  the income statement presentation — this is *why* a `us-gaap_Revenues`
  (or, for banks, `us-gaap_RevenuesNetOfInterestExpense`) subtotal reliably
  exists once a filer has more than one revenue stream, and why preferring
  it over any component tag is the theoretically correct resolution, not a
  guess. When no such aggregate is separately tagged (CPT's case), GAAP's
  own additive construction of "total revenue" means the components' sum is
  the correct reconstruction.

### Fix

`src/fundamental_agent/statements.py`:
- `LineItem` gains two optional fields, defaulting to no-ops so every other
  registry entry (~24 items) is byte-for-byte unaffected: `total_concepts`
  (an aggregate that, if present, wins outright) and `sum_components` (sum
  the first row per distinct matching concept instead of returning just the
  first, when no total is tagged).
- `Statements.get()` rewritten as a two-tier resolution (extracted into
  `_rows_for`/`_first_total_match`/`_first_component_match`/
  `_sum_matching_components` helpers to keep cyclomatic complexity low):
  **Tier 1** — any row tagged with a `total_concepts` entry wins,
  order-independent. **Tier 2** — the original first-match loop (default,
  unchanged for every item that doesn't set `sum_components`), or, for
  revenue, the sum of every distinct matching component.
- `REGISTRY["revenue"]`: added `us-gaap_OperatingLeaseLeaseIncome` to
  `concepts`; added `total_concepts=("us-gaap_Revenues",
  "us-gaap_RevenuesNetOfInterestExpense")`; set `sum_components=True`.
- `Statements.get_all()` is **not** touched — it shares the identical
  first-match defect but is only ever called for `shares_outstanding`/
  `diluted_shares`/`net_income`/`eps_diluted` (F1's
  `detect_share_scale_factors`), never `revenue`.

### Design decisions

**Opt-in fields, not a global algorithm change.** Every other multi-concept
registry item (`cogs`, `interest_expense`, `depreciation_amortization`,
`operating_cash_flow`, `capital_expenditure`, `stock_based_compensation`,
`shares_outstanding`, `cash`, `long_term_debt`, `short_term_debt`, …) keeps
`total_concepts=()`/`sum_components=False`, so Tier 1 no-ops and Tier 2 is
the exact original loop for all of them — verified by construction and by
every one of the four existing fixtures (AAPL, JPM, MSFT, NVDA) resolving to
the identical value as before. A global "prefer tuple order" or "always sum
multiple matches" change was considered and rejected: several other items'
concept tuples are genuine filer-convention *synonyms* (pick one, not
components to sum), and summing them would have introduced new bugs to fix
this one.

**Flagged, not fixed, for a future pass** — some other registry items share
F2's exact risk shape and may need the same treatment eventually, but each
needs its own verification pass first, the same discipline applied here:

| item | risk |
|---|---|
| `cogs`, `interest_expense`, `depreciation_amortization`, `long_term_debt` | same total/component shape as revenue — plausible, not verified live |
| `short_term_debt` | would need `sum_components` with **no** `total_concepts` (CPT's shape) — these are typically simultaneous distinct lines with no GAAP "total short-term debt" tag |
| `cash` | the **opposite** risk — `CashCashEquivalentsAndShortTermInvestments` is itself an aggregate that already includes what the separate `short_term_investments` registry item also captures; a careless `sum_components=True` here would double-count |

### Verification

Live re-verification against the production database, 2026-09-15, using
the **actual shipped code** (not the pre-fix estimate) — reconstructed each
outlier filing's `Statements` from its own `financial_facts` rows and
re-ran the real `net_margin`/`operating_cash_flow_margin` formulas through
the fixed `Statements.get("revenue", ...)`:

| metric | pre-fix outliers (matches PLAN.md exactly) | resolved post-fix |
|---|---|---|
| `net_margin` outside `[-1,1]` | 124 filings / 34 tickers | ~~**98 (79.0%)**~~ **93 (75.0%)** — see Post-merge correction below |
| `operating_cash_flow_margin` outside `[-1.5,1.5]` | 54 filings / 17 tickers | **46 (85.2%)** (unaffected by the correction) |

Code-level verification:
- `uv run pytest -q` — 222 passed (was 217; +5 new: `tests/test_statements.py`
  ×4 — total-wins-regardless-of-order [parametrized both orderings],
  sum-when-no-total, and a defensive test proving a *different* item
  [`cogs`] with 2 matching concepts still returns only the first match, not
  a sum — plus `tests/test_metrics.py` ×1, an end-to-end
  `net_margin`/`operating_cash_flow_margin` sanity check for a synthetic
  UDR-shape payload).
- `uv run ruff check` / `ruff format --check` / `uv run mypy` — all green.
- `git diff` review: confirmed every other `REGISTRY` entry unchanged, and
  `test_income_line_items_resolve`/`test_bank_has_no_operating_income_or_current_split`
  needed zero changes (traced by hand and confirmed by the passing suite).

**Not done as part of this change** (same as F1, explicitly not an
oversight): re-persisting corrected `fundamental_metrics`/`score_snapshot`
rows for the affected universe requires a `--fresh` re-run against
production (paid LLM calls, mutates shared data) — outside a code-review
pass's authority to run unprompted.

### Post-merge correction: a code-review bot caught two real gaps

An automated PR review ([Sourcery](https://sourcery.ai)) flagged two
`statements.py` issues before merge. Both were verified against live
`financial_facts` (not just accepted on the bot's say-so) and turned out to
be real, *currently-occurring* double-counting bugs in the just-shipped
`sum_components` path — fixed the same day (2026-09-15):

1. **`ExcludingAssessedTax`/`IncludingAssessedTax` are alternate encodings
   of one line, not two additive amounts** — summing them roughly triples
   revenue. Live-verified across **37 real filings** (`BF.B`, `PM`, `STZ`,
   `TAP`, `EXC`): every one tags *both* concepts for *every* period, and
   they are never independent amounts — e.g. STZ FY2019 tags
   `...ExcludingAssessedTax` = "Net revenues" **$29.8B** and
   `...IncludingAssessedTax` = "Revenues including excise taxes" **$77.9B**
   for the identical period; the smaller, excluding-tax figure is the real
   income-statement "Net sales"/"Net revenues" line (ASC 606-10-32-2 scopes
   amounts collected on behalf of a third party, e.g. excise tax, out of the
   transaction price), the larger one a supplemental gross disclosure.
   **Fix**: a new `LineItem.synonym_groups` field — concepts sharing a group
   are mutually exclusive; `_sum_matching_components` now counts at most one
   value per group, preferring the group member listed earliest in
   `concepts` (excluding-tax, deterministically, regardless of document
   order). New test: `test_revenue_prefers_excluding_assessed_tax_over_the_
   synonym_variant`.
2. **The `sum_components` path matched via `_matches()`, which includes the
   fuzzy `label_contains` fallback** — a filer's own unrecognized
   custom-taxonomy "Total ..." extension concept (not in `total_concepts`,
   which only lists the two standard `us-gaap_Revenues*` tags) could match
   by label text alone and get summed alongside the real components,
   double-counting. Live-verified across **42 real filings** (`PSX`, `LOW`,
   `ICE`, `SHW`, and others using issuer-specific total concepts like
   `psx_RevenuesAndOtherIncome`, `axp_TotalRevenuesNetOfInterestExpense
   AfterProvisionsForLosses`, `bk_TotalRevenuesIncludingRevenueGeneratedBy
   VariableInterestEntities`) — e.g. PSX FY2021 would have summed
   `psx_RevenuesAndOtherIncome` ("Total Revenues and Other Income", $114.9B,
   matched only by its label containing "total revenue") with
   `us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax` ("Sales and
   other operating revenues", $111.5B) to $226.3B — nearly double the real
   figure. One case (LOW) also surfaced a sharper version of the same risk:
   `low_RevenueFromContractWithCustomerExcludingAssessedTaxPercentage`, a
   *disclosure percentage* (value `1.0`), matches the label hint "net
   sales" too. **Fix**: `_sum_matching_components` now matches only
   `spec.concepts` (exact XBRL concept tag) — never `label_contains`, which
   stays reserved for the (unchanged) single-match `Tier 2` fallback other
   registry items use. Revenue's own now-unreachable `label_contains` entry
   was removed rather than left as dead/misleading config. New test:
   `test_revenue_sum_ignores_a_label_only_match_from_a_custom_total_concept`.

Neither gap was hypothetical: both were confirmed live, and re-running the
exact F2 verification query afterward changed the headline number — **98/124
→ 93/124 for `net_margin`** (five filings that the original, buggy summing
had coincidentally pushed back inside `[-1,1]` are, correctly, still
outliers post-fix; none of the 37+42 double-counting-risk filings above were
themselves in the outlier cohort, so the five are a separate, smaller
overlap not yet individually characterized). `operating_cash_flow_margin`
(46/54) was unaffected — no `ocf_margin`-outlier filing in the cohort
exercised either gap. Full suite: 224 passed (was 222 before this
correction; +2 new regression tests, on top of the +5 from the original
fix). `uv run ruff check` / `ruff format --check` / `uv run mypy` — all
green.

### Residual scope, deliberately deferred

- **31 of 124 net_margin-outlier filings (25.0%) and 8 of 54
  operating_cash_flow_margin-outlier filings (14.8%) are NOT resolved by
  this fix** (updated post-merge-correction count for `net_margin`, see
  above) — most have only a single matching revenue concept, so F2's
  multiple-rows mechanism doesn't apply; their distortion (if the underlying
  number is even wrong at all, rather than a genuinely unusual quarter, e.g.
  MRNA's pandemic-era revenue collapse) has some other, unexamined cause.
  Several of the residual (`FITB`, `HBAN`, `IBKR`, `MTB`) are banks/brokers
  whose revenue is tagged via issuer-specific custom XBRL extension concepts
  (e.g. `fitb_CommercialBankingRevenue`) entirely outside the standard
  `us-gaap` taxonomy this repo's `REGISTRY` whitelists — a distinct, larger
  investigation (a bank/broker revenue-taxonomy pass), not part of F2.
- `Statements.get_all()` carries the identical first-match defect and does
  not consult `total_concepts`/`sum_components` — if a future caller ever
  needs `revenue`'s full period series, it needs the same two-tier
  treatment ported over.
- The `cogs`/`interest_expense`/`depreciation_amortization`/
  `long_term_debt`/`short_term_debt`/`cash` risk table above is flagged,
  not fixed — each needs its own live-data verification pass before
  changing, the same discipline F1 and F2 both applied.

---

## F4 — 10-Q flow/stock mismatch, never annualized

**Status**: Fixed 2026-09-15 (`T-062`).

### Symptom

`metrics/profitability.py` and `metrics/efficiency.py` divide a 10-Q's
3-month flow (net income, revenue, cogs) by an instantaneous balance-sheet
stock (total assets, equity, inventory, receivables) with no annualization.
Verified (robust medians, PLAN.md): `return_on_assets` 10-K median 6.01% vs.
10-Q median 1.56% (3.86×); `return_on_equity` 15.80% vs. 4.17% (3.79×);
`asset_turnover` 0.5347× vs. 0.1395× (3.83×) — all converging on ≈4×, the
expected quarterly factor. This is not a rounding artifact: a 3-month flow
compared against an annual-basis stock understates every affected ratio by
roughly the number of quarters per year, making a 10-Q filing's ROA/ROE/
turnover look structurally worse than the same company's own 10-K, for
reasons that have nothing to do with its actual performance.

### Fix

Scope is exactly PLAN.md's two cited call sites — the specific
flow-over-stock ratios, not every metric that touches these flows:
`profitability.py`'s `return_on_assets`/`return_on_equity` (net income) and
`efficiency.py`'s `asset_turnover`/`inventory_turnover`/
`receivables_turnover` (revenue, cogs). Margins (`gross_margin`,
`operating_margin`, `net_margin`) divide a flow by another flow from the
*same* period and need no adjustment.

`fundamental_agent/db.py` gains `ttm_flows(conn, asset_id, *, fiscal_year,
quarter, current)`: for each flow item in `current`, sums the filing's own
quarter plus the three immediately preceding ones (`_quarter_flow`/
`_historical_flow`), falling back to `current * 4` for that item alone when
the trailing history isn't fully available. A quarter's own value is read
back from an **already-recorded** metric row's `inputs_json` — never
re-derived from raw `financial_facts` — so it reuses whatever concept
resolution was correct at the time that earlier filing was processed
(F2's fix included) rather than duplicating that logic. A 10-Q never itself
reports quarter 4 (the fiscal year's 10-K reports only the full-year total),
so Q4 is derived as `FY − (Q1 + Q2 + Q3)`, and only when all four of those
rows exist; otherwise the whole item falls back to the `×4` approximation.

`pipeline._analyze_one` computes this once per 10-Q filing (`_ttm_flows`,
empty for a 10-K) using the filing's own just-computed `net_income`/
`revenue`/`cogs` as `current`, and threads the result through
`FilingContext.ttm` into `compute_group`. Every `metrics/*.compute()`
function gained a `ttm: dict[str, float] | None = None` parameter for one
uniform `ComputeFn` signature; only `profitability`/`efficiency` consult it.
Critically, each metric group's recorded `inputs` (`inputs_json`) still
carries the **raw, single-quarter** value regardless of the TTM adjustment —
that's what a *later* filing's own TTM lookup reads back as one of its
trailing quarters, so the adjustment never compounds across filings.

### Design decisions

**Read the flow back from `fundamental_metrics.inputs_json`, not
`financial_facts`.** The alternative — re-deriving each historical quarter's
revenue/net_income/cogs straight from stored XBRL facts — would require
re-implementing `Statements.get()`'s full total/sum/synonym resolution
(F2) against raw facts outside a `Statements` object. Reading the value
back from the metric row a prior run already computed avoids duplicating
that logic and stays correct automatically as `Statements.get()` evolves.

**Fallback is per-item, not all-or-nothing.** A filing missing `cogs` (a
financial firm, say) still gets a real TTM `net_income`/`revenue` if that
history exists — each of the three flow items is evaluated independently in
`ttm_flows`.

**Scope held to PLAN.md's two cited call sites.** `roic.py`'s NOPAT/invested
capital and `leverage.py`'s `net_debt_to_ebitda` have the identical
flow-over-stock structure, but neither was in F4's verified scope — flagged
below, not silently fixed alongside this change.

### Verification

- New tests: `tests/test_ttm.py` (7 cases) — `db.ttm_flows` summing four
  real quarters including a derived Q4, falling back to `×4` with no prior
  history at all, and falling back when the prior year's 10-K (needed to
  derive its Q4) hasn't been ingested even though its three quarters have;
  `profitability.compute`/`efficiency.compute` picking up a supplied `ttm`
  value for exactly the flow-over-stock ratios while leaving `net_margin`
  and the recorded `inputs` untouched. `tests/test_pipeline.py` (+2) —
  `_ttm_flows` returns `{}` for a 10-K and falls back to `×4` for a 10-Q
  with no history.
- `uv run pytest -q` — 231 passed (was 224).
- `uv run ruff check` / `ruff format --check` / `uv run mypy` — all green.

**Not done as part of this change** (same as F1/F2, explicitly not an
oversight): re-persisting corrected `fundamental_metrics`/`score_snapshot`
rows for the live universe requires a `--fresh` re-run against production
(paid LLM calls, mutates shared data) — outside a code-review pass's
authority to run unprompted. `T-068`'s Phase A re-sequence is where that
re-run belongs, after `T-063`/`T-064` (C1/C2) also land.

### Residual scope, deliberately deferred

- **`roic.py`'s NOPAT/invested-capital ratio** and **`leverage.py`'s
  `net_debt_to_ebitda`** divide a flow by a stock the same way ROA/ROE do,
  but neither was in PLAN.md's verified F4 scope — a separate, later pass
  should verify and fix them the same way, not assume this fix already
  covers them.
- The TTM lookup's `_quarter_flow`/`_historical_flow` pick the
  *most-recently-written* `fundamental_metrics` row when more than one
  `engine_version` exists for the same key (mirrors `overlapping_history`'s
  rule) — it does not attempt to reconcile disagreeing engine versions
  beyond that.

---

## C1 — T-1 veto cutoff: diagnosis corrected, no code fix

**Status**: Investigated 2026-09-15 (`T-063`) — **no code change made**.

### Original claim

PLAN.md's 2026-09-08 forensic audit described a "Critical" bug in
`src/cycle/orchestrator.py`'s `_rank()`: live `data/financial.db` allegedly
showed `cycle_ranking` with **0 rows ever having `vetoed != 0` out of 503**,
with Mastercard (MA) ranking #13 despite carrying an "active HARD
`LEVERAGE_EXTREME` veto" — framed as an off-by-one/direction bug in the T-1
cutoff comparator (`_t_minus_1`, `writers.hard_vetoed_as_of`,
`writers.active_soft_vetoes`), with the proposed fix: "correct the
off-by-one/direction bug in the cutoff comparator (or in how
`veto.cycle_date` is stamped relative to the run it should first apply
to)."

### Investigation — corrected from PLAN.md's own stated framing

Same pattern as F2's own corrected framing: the audit's diagnosis doesn't
survive direct code-level and reproduction testing.

**The comparator is not buggy — it matches spec exactly.** `_rank()`
computes `cutoff = _t_minus_1(cycle_date)` (yesterday) and queries
`WHERE cycle_date <= cutoff`; `_veto()` stamps a newly detected veto with
the run's own (today's) `cycle_date`. This is precisely SPEC.md's FR-006:
"a veto row inserted for cycle date N does not exclude the asset from
`cycle_ranking`/`portfolio_position` computed for date N itself, but does
for the next cycle run at N+1." `tests/test_cycle.py::
test_t_minus_1_hard_veto_excludes_asset` already passed before this
investigation, proving the mechanism for a hand-seeded prior-day veto row.
A new test added here,
`test_hard_veto_detected_via_rules_excludes_asset_starting_next_cycle`,
closes the one remaining gap — proof through the *real* rule-detection path
(`cycle_seed`'s EEE, `debt_to_equity = 5.5` naturally trips
`LEVERAGE_EXTREME`), not a hand-inserted row: `run_selection` on day 1
correctly leaves EEE unvetoed (same-day exemption) while writing the HARD
veto row; `run_selection` again for day 2 correctly excludes EEE
(`vetoed = 1`, `selected = 0`). `git log` on `orchestrator.py`/`writers.py`
shows neither file has ever been touched by a prior fix commit — the code
the audit describes as buggy is the same code sitting in this repo today,
unmodified, and it already matches spec.

**What actually explains "0/503 always" — an operational/data-cadence
artifact, not a code defect.** `cycle select`/`cycle monitor`'s
`--analysis-date` defaults to `kg_schema.rundate.today()` (wall-clock
"today") on *every* invocation — nothing advances it forward between runs.
`cycle_run` has `UNIQUE(cycle_type, cycle_date)`, and
`state.open_cycle` explicitly creates-**or-resumes** the row for that key;
`writers.write_ranking` deletes and reinserts `cycle_ranking` scoped to one
`cycle_run_id`. So repeated invocations that omit `--analysis-date` — the
natural result of the default above — collapse onto the **same** single
day's `cycle_run`/`cycle_ranking` snapshot rather than ever advancing to a
genuinely new calendar date. The exact match between "503" and the known
S&P 500 universe size is consistent with this being one run's worth of
rows, not an accumulation across many distinct dates. Under this reading,
the T-1 "settling" period simply has never actually elapsed once in
production — the pipeline has apparently never yet been re-run on a later
calendar date since any veto was first raised — not that an elapsed period
was ignored by a bug.

### Verification

- New regression test (see above) added to `tests/test_cycle.py`,
  exercising the real detection path across two genuinely different
  `cycle_date`s — closes the one gap `test_t_minus_1_hard_veto_excludes_
  asset`'s hand-seeded row left open.
- `uv run pytest -q` — full suite green (232 passed, was 231).
- `uv run ruff check` / `ruff format --check` / `uv run mypy` — all green
  (no `src/` changes in this pass).
- `git log -- src/cycle/orchestrator.py src/cycle/writers.py` reviewed:
  neither file has been touched by any prior commit in this repo's history.

### Residual scope, deliberately not addressed here

- **The operational gap itself is out of scope for this code-only pass.**
  Actually exercising T-1 exclusion in production requires running `cycle
  select`/`monitor` with an explicit `--analysis-date` that genuinely
  advances day over day (or a scheduled daily invocation) — a deployment/
  operations concern, not a code defect this pass is positioned to fix.
  No CLI change (e.g. a warning when `--analysis-date` is omitted and
  today's `cycle_run` already exists) was made; the option was considered
  and explicitly declined for this pass.
- If a genuine subtle defect does surface later (e.g. under a real
  multi-day production cadence this investigation had no live data to
  exercise), it should be re-opened as a new, separately-verified finding —
  not assumed to be within this entry's scope.
