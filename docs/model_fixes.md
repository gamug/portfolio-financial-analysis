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
quarter, current)` *(as first written; it located the prior quarters by their
`fiscal_period` labels, which only line up for a December year-end — superseded
by the date-anchored `ttm_flows(conn, asset_id, *, period_end, current)` of the
"F4 addendum — fiscal-calendar alignment" below)*: for each flow item in
`current`, sums the filing's own quarter plus the three immediately preceding
ones (`_quarter_flow`/`_historical_flow`), falling back to `current * 4` for that item alone when
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

- **Update 2026-09-21 (`T-092`) — real TTM now works, and a non-calendar-year mismatch is
  exposed.** The pipeline now ingests every 10-Q of a year (it stored about one a year), so the
  three prior quarters this fix reads can exist: on a scratch re-ingest XOM's TTM is real from
  2023Q1 (TTM/quarter revenue 3.2–4.7×). But `db._quarter_flow` labels a fiscal year by the
  calendar year it *ends* in (`FY2024` = STZ's year ending Feb-2024) and a quarter by the calendar
  year it ends in (`2023Q1`–`Q3` belong to FY2024), so its Q4 derivation `FY{y} − {y}Q1..Q3` reads
  the *next* fiscal year's quarters for any non-calendar filer — on real stored values it would
  read STZ's 2024Q2 net income of −$1,199M (an FY2025 impairment quarter). Invisible while the
  quarters were missing; a clean re-ingest would activate it for STZ, BF.B and every other
  non-December filer. Not fixed by `T-092` — **fixed by `T-094`, next.**


### F4 addendum — fiscal-calendar alignment (`T-094`, 2026-09-21)

**Symptom.** Found by `T-092`'s acceptance. `ttm_flows` located a 10-Q's three prior quarters, and
derived a fiscal Q4 as `FY{y} − {y}Q1..Q3`, by the `fiscal_period` **labels**. The pipeline builds
those labels from the calendar year the period *ends* in (`Period.year` is `int(date[:4])`), so a fiscal
year and its own quarters carry the same year only for a December year-end. STZ's year ends in
February: `FY2024` covers Mar-2023..Feb-2024 and its quarters are labelled `2023Q1`–`Q3`, so the old
Q4 derivation read `2024Q1`–`Q3` — the *next* fiscal year's quarters. The `fiscal_year × 4 + quarter`
index was also non-monotonic in time for such a filer (2023Q3 → 2024Q4 → 2024Q1), so "the three
preceding quarters" were wrong before Q4 even came into play. It was invisible while about one 10-Q per
year was stored (the quarters were never all present, so the code fell back to `× 4`); `T-092`
ingests every quarter and made it reachable.

**Fix.** `ttm_flows(conn, asset_id, *, period_end, current)` finds the quarters by **date**: the ones
ending 3, 6 and 9 months before `period_end`, matched within a 20-day window on the asset's 10-Qs
(`_filing_near`). A fiscal Q4 (no 10-Q exists) is the 10-K whose period ends near that date, minus the
three 10-Qs at 3/6/9 months before *that 10-K's own* period end (`_quarter_flow_ending`). Still `× 4`
for an item only when a quarter is genuinely missing. `pipeline._ttm_flows` passes `target.period.date`.

**Design decisions.**
- **Dates, never labels.** The stored `fiscal_period` labels are kept (relabelling changes unique keys
  and every consumer) but nothing does arithmetic on them any more. Audit: the only label arithmetic in
  the codebase was in `_quarter_flow`/`ttm_flows`; every other use of `fiscal_period` is an identity key
  (uniqueness, the resume set, display), and growth, CAGR, `prior_of`, share-scale detection and
  `cycle`'s "latest filing" all key on dates or on the payload's own period columns.
- **Month-end preserving** (`2023-08-31` − 3 → `2023-05-31`, `2024-05-31` − 3 → `2024-02-29`), and a
  **20-day tolerance** because 52/53-week filers (AAPL) end quarters on a Saturday, a day or two from
  "exactly three months earlier"; adjacent quarters are ~91 days apart, so the window never matches two.
- **Form-matched**: quarters come from 10-Qs and only a Q4 from a 10-K, so a 10-K is never read as a quarter.
- **Kept the quarter-sum method** rather than switching to TTM = last FY + current YTD − prior-year YTD.
  The two agree exactly (below), and the quarter sum reuses the already-recorded raw inputs without
  changing the recorded-`inputs_json` contract that stops the adjustment compounding.

**Verification against real data** (constitution AI behavior #12). The real `EdgarClient` against the
live gateway, on a **scratch copy** of the purged DB with a stub analyst (no LLM), five filers on five
fiscal calendars, 2022–2026: XOM (Dec), STZ (Feb), BF.B (Apr), MSFT (Jun), AAPL (Sep, 52/53-week) →
**95 filings ingested, 0 failed**.
- **Independent cross-check.** For every quarter that has real history (revenue: XOM 11, AAPL 12, MSFT 9,
  BF.B 10, STZ 10 = **52 quarters**), the new TTM equals TTM = last FY + current YTD − prior-year YTD,
  computed from that 10-Q's *own* payload (a different data path): **maximum difference 0.00%.**
- **Before / after** on the same data, net income: of the **44** quarters where the old code did *not*
  fall back to `× 4`, **33 read the wrong quarters** — every one a non-calendar filer (AAPL 4 of 4,
  worst 15.5%; MSFT 9 of 9, 17.4%; BF.B 10 of 10, 69.0%; STZ 10 of 10, 409%) — while XOM, the calendar
  filer, was right in all 11. The worst: STZ 2025Q1, old TTM net income **+$1,366.5M** against a correct
  **−$442.3M**.
- **Fallback share** (still `× 4`, by design): XOM 3 of 14, AAPL 3 of 15, MSFT 5 of 14, BF.B 4 of 14,
  STZ 4 of 14 — the earliest quarters of the 2022+ window, where four quarters of history do not exist yet.
- 24 hermetic tests (`tests/test_ttm.py`, `tests/test_pipeline.py`), including the STZ February case with
  the FY2025 quarters poisoned (a −1,199 impairment) and 52/53-week Saturday ends; mutation-checked
  seven ways, including one that re-creates the original bug (a Q4 that reads the next fiscal year's
  quarters).

**Residual scope, deliberately deferred.**
- **Metrics recorded before this fix are wrong for non-calendar filers.** The derived data was purged
  (`T-088` step 2); the recompute is `T-088`'s re-run, under the `metrics-v2` bump.
- The stub analyst exercised ingestion and the TTM lookup, not the LLM scoring.
- A fiscal-year *change* (a transition period with a short quarter) is untested. By construction an
  off-cycle quarter is simply not matched by the 20-day date window, so it should count as missing and
  fall back to `× 4` rather than be mis-summed — but that has not been exercised on real data.
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

---

## C2 — Leverage-veto evasion via negative book equity

**Status**: Fixed 2026-09-15 (branch `fix/c2-leverage-veto-negative-equity`,
`T-064`).

### Symptom

`src/cycle/rules/builtin.py`'s `LEVERAGE_EXTREME` rule was a plain
`_ThresholdRule("leverage.debt_to_equity", ">", 3.0)`. A company with large
buyback-driven **negative book equity** produces a *negative*
`debt_to_equity` ratio — verified against production data: MCD's real
`debt_to_equity = -38.96` (debt $39.8B, equity **-$1.02B**). A negative
number is never `> 3.0`, so the sign flip trivially evaded the HARD veto
even though the firm is actually maximally leveraged. Independently,
`src/cycle/scores/valorization.py`'s "quality" factor includes
`("leverage.debt_to_equity", False)` — `normalize.rank_pct()`'s
`higher_is_better=False` inversion (`1.0 - pct`) means the *most negative*
value in a cohort, being the lowest raw number, was inverted to the
**highest** quality percentile, actively rewarding the same name: MCD's
`VALORIZATION` score inflated to 82.14 (partly compounded by the
still-uncorrected F1 market-cap bug at the time of that reading).

### Root cause

Two independent mechanisms, both stemming from the same underlying fact —
`debt_to_equity`'s *sign* carries information (whether equity is
positive) that a plain magnitude threshold discards:

1. `_ThresholdRule.evaluate()`'s `value > self.threshold` comparison is
   satisfied by no negative value when `threshold = 3.0`, regardless of
   how large `|value|` is.
2. `valorization._factor_score()`'s use of `rank_pct(..., higher_is_better=
   False)` assumes "lower debt_to_equity is always better" — true only
   when equity is positive; once it flips negative, "lower" (more
   negative) is actually *worse*, not better, but the ranking has no way
   to know that without a sign check.

### Fix

`src/cycle/rules/builtin.py`: replaced the `_ThresholdRule` entry with a
dedicated `_LeverageRule` dataclass. `leverage.py::_total_debt()` sums only
non-negative balance-sheet items (`long_term_debt`/`short_term_debt`), so
`debt_to_equity < 0` is *itself* reliable, already-available evidence that
equity is non-positive — no new persisted `equity` metric was needed. When
`debt_to_equity < 0`, the rule instead gates on `leverage.debt_to_assets`
(`> 0.8`) or `leverage.interest_coverage` (`< 1.5`) — either sufficient —
reusing PLAN.md's own Ring-1 `DQ_NEG_EQUITY` calibration verbatim (342
filings verified) rather than inventing new, unverified thresholds. Both
metrics are already computed in the same `leverage.py::compute()` call and
reach `RuleContext.metrics` the same way `debt_to_equity` does, so no
upstream change was needed. Positive `debt_to_equity` keeps the original
plain `> 3.0` check, byte-for-byte unchanged.

`src/cycle/scores/valorization.py`: added a `_VALUE_TRANSFORMS` map
applied in `_factor_score()` before `rank_pct()` — for
`"leverage.debt_to_equity"`, a negative value is replaced with
`float("inf")` before ranking, so it sorts as the cohort's *worst* (not
best) leverage; `rank_pct`'s bisection sorts `float("inf")` correctly as
the maximum, and the subsequent `1.0 - pct` inversion then correctly lands
it at `pct ≈ 0`.

### Design decisions

**No new persisted `equity` metric.** `RuleContext.metrics` has no raw
`equity` key today — it only lives inside `debt_to_equity`'s
`MetricResult.inputs` audit blob (`fundamental_agent/metrics/leverage.py`),
which `cycle/data.py::latest_metrics()` never reads. Adding one would mean
touching `fundamental_agent` as well as `cycle` for a signal the sign of
`debt_to_equity` already gives for free (debt is never negative), so this
fix stays entirely within `cycle`.

**Thresholds reused verbatim, not recalibrated.** PLAN.md's own Ring-1
`DQ_NEG_EQUITY` gate (Work item 7, blocked on `T-040`/Work item 5's
`data_quality_issue` table landing) already specifies and verifies
`debt_to_assets > 0.8` / `interest_coverage < 1.5` against 342 production
filings. This fix reuses those exact numbers — not `DQ_NEG_EQUITY`'s
quarantine mechanism itself, which stays blocked on `T-040` — so C2 has no
external dependency, matching its "no external dependency" status in
TASKS.md.

**Deliberately minimal `valorization.py` change.** This is a targeted
sign-fix, not the full valorization redesign (EV-based multiples, ROIC
replacing ROE, robust median/MAD intra-sector standardization) that Work
item 8/`T-071` already owns — PLAN.md explicitly ties that redesign to
this same finding ("this also motivates Work item 8's ROE→ROIC
substitution"), so this pass does not attempt it.

**Missing-data handling matches every other rule.** If `debt_to_equity <
0` but both `debt_to_assets` and `interest_coverage` are unavailable, no
veto fires — the same "quarantine/skip, don't guess" behavior every other
rule in the catalog already has for a missing metric.

### Verification

- New tests in `tests/test_cycle.py`: `test_leverage_rule_hard_vetoes_
  negative_equity_with_high_debt_to_assets`, `test_leverage_rule_hard_
  vetoes_negative_equity_with_low_interest_coverage` (proves the OR is a
  genuine OR — each signal independently sufficient),
  `test_leverage_rule_spares_negative_equity_with_healthy_debt_load`
  (including exactly-at-threshold values, locking in strict inequalities),
  `test_leverage_rule_negative_equity_with_no_corroborating_metrics_does_
  not_veto`, `test_leverage_rule_positive_debt_to_equity_path_unchanged`
  (same values as the pre-existing `test_threshold_and_drawdown_rules`),
  and `test_valorization_negative_equity_leverage_ranks_worst_not_best`.
- `uv run pytest -q` — 238 passed (was 232).
- `uv run ruff check` / `ruff format --check` / `uv run mypy` — all green.

### Residual scope, deliberately deferred

- **`rule_catalog` staleness in production.** `cycle/rules/__init__.py`'s
  `seed_catalog()` uses `INSERT OR IGNORE` and explicitly never overwrites
  an existing row. Production's DB almost certainly already has a
  `LEVERAGE_EXTREME` row seeded under the *old* description/`params_json`.
  Live veto **behavior** is unaffected — `evaluate()` runs the live Python
  `RULES` object directly, never reconstructing the rule from its DB row —
  but `v_rule_catalog`'s audit-facing `description`/`params_json`/
  `param_threshold` columns will keep showing the pre-fix values until a
  one-time `UPDATE rule_catalog SET description = ?, params_json = ?
  WHERE rule_id = 'LEVERAGE_EXTREME'` is run against production — an
  operational follow-up outside a code-only pass's authority to run
  unprompted (same category as F1/F2's "not done as part of this change"
  notes).
- **`DQ_NEG_EQUITY` itself remains blocked on `T-040`.** This fix reuses
  its thresholds, not its `data_quality_issue`-quarantine mechanism —
  implementing that gate is still Work item 7's `T-065`, unblocked only
  once Work item 5 lands.
- **`roic.py`'s NOPAT/invested-capital ratio and `leverage.py`'s
  `net_debt_to_ebitda`** (flagged in F4's own residual-scope note) are
  *also* flow-over-stock ratios that could show a related sign/magnitude
  distortion under negative equity — not examined as part of this pass.

---

## Q3 — Dividend shortage: Level-1 quarterly derivation from 10-Q YTD differences

**Status**: Fixed 2026-09-15 (branch `fix/q3-10q-ytd-dividend-derivation`,
`T-066`) — **then superseded 2026-09-20 by `T-085`**: the derivation this entry
describes (`derive_quarterly_dividends_from_10q_ytd`, and the FY-level
`derive_corporate_actions_from_facts` it ran beside) was **removed from `src/`**.
Dividends and splits now come only from the pricing gateway, because acquiring data
is `portfolio-data-mining`'s job and no other repository should host it. The
symptom below is real and the Level-1 derivation did close it, but the chosen
resolution is the gateway (`T-052` verifies it live), not a local derivation. The
`corpact-v0-approx` / `corpact-v1-derived` rows it wrote stay in `corporate_action` as
history; `quant.db.load_actions` no longer reads them. The rest of this entry is kept
as the historical record of how the fix worked.

### Symptom

`src/quant/actions.py`'s only derived-dividend source,
`derive_corporate_actions_from_facts`, reads a 10-K's fiscal-year cash
dividend per share and spreads it evenly across four synthetic quarterly
ex-dates. Verified against production data: only 301/503 assets have any
`corporate_action` row at all, and XOM/PG/T/NEE — all with 0 rows — show
`SUM(cash_dividend) = $0.00` in `quant_return_daily`, even though all four
are long-standing dividend payers. Firms whose 10-Ks don't carry a usable
annual DPS/aggregate-payments fact (or with no 10-K DPS fact processed yet)
get nothing from the FY-level path, regardless of what their 10-Q filings
report.

### Fix

`src/quant/actions.py` gains a second, independent derived source,
`derive_quarterly_dividends_from_10q_ytd`, run unconditionally alongside
the existing FY-level derivation in the `--source derive` path (never
replacing it) and tagged under its own `engine_version =
'corpact-v1-derived'`:

- For each of an asset's 10-Q filings (ascending `period_end`, capped by
  `as_of` — no lookahead), reads the dividend-per-share concept
  (`_DPS_CONCEPTS`, same tags `derive_corporate_actions_from_facts` already
  uses) tagged for that filing's own discrete quarter (e.g. `(Q2)`) when
  present — no differencing needed. When only a `(YTD)` tag exists (some
  filers tag `CommonStockDividendsPerShareDeclared` cumulatively in interim
  filings), the quarter's dividend is the difference from the running
  year-to-date total for that fiscal year: `DPS_quarter(Qn) = DPS_YTD(Qn) -
  DPS_YTD(Qn-1)`, tracked incrementally per fiscal year as filings are
  processed in period order.
- Falls back to aggregate dividends paid ÷ a share count (same fallback
  shape as the FY-level path, `_period_fact` generalized from the
  FY-only `_fy_fact` to accept any tag) when no per-share concept is tagged
  at all, tried at the discrete-quarter tag first, then `(YTD)`.
- Each derived dividend is anchored to the 10-Q's own `period_end` — coarse
  on purpose (no real ex-date at this level), same limitation the FY-level
  path already has.
- A `quarterly <= 0` result (a restatement/decrease artifact between
  successive YTD readings) is dropped rather than recorded as a negative
  dividend.

`src/quant/db.py::load_actions` is rewritten to resolve engine-version
priority **per asset**, not per `(asset, ex_date)`: it looks up which
engines have any row for the asset, picks the highest-priority one present
(`corpact-v2` > `corpact-v1` > `corpact-v1-derived` > `corpact-v0-approx`),
and returns only that engine's rows. This is deliberately *not*
`v_corporate_action`'s existing "most recently ingested row per
`(asset, action_type, ex_date)`" resolution: the two derived sources'
synthetic ex-dates rarely coincide (FY-level spreads across the fiscal
year's own quarter-boundaries; 10-Q-level anchors to each filing's actual
period_end), so that view would return **both** sources' rows side by
side for the same asset and roughly double-count the annual dividend
instead of one source superseding the other.

### Design decisions

**Both derived sources always run, never gated on which "wins."**
`backfill_corporate_actions`'s derive branch calls both
`derive_corporate_actions_from_facts` (→ `corpact-v0-approx`) and
`derive_quarterly_dividends_from_10q_ytd` (→ `corpact-v1-derived`)
unconditionally and upserts each under its own engine_version; resolving
which one a caller actually sees is entirely `load_actions`'s job. This
keeps `backfill-actions` idempotent and simple — no branching logic needed
to decide in advance which source an asset "should" get.

**`load_actions` picks one engine per asset, not per ex-date.** Blending
rows from two engines for the same asset would silently roughly double the
recorded annual dividend (both sources cover the same real dividend
history via different, non-overlapping synthetic ex-dates) — a much worse
outcome than picking a single, coarser-but-consistent source.

**No new persisted metric or `fundamental_agent` change.** The
dividend-per-share/aggregate-payments concepts this fix reads
(`_DPS_CONCEPTS`/`_DIV_PAID_CONCEPTS`/`_SHARES_CONCEPTS`) already reach
`financial_facts` today: `fundamental_agent.statements.iter_facts` extracts
*every* non-abstract, non-dimensional concept row from a filing's payload,
not just the ones `Statements.REGISTRY` recognizes — so no new
`fundamental_agent` ingestion work was needed to read them from `quant`.

**Final target unchanged.** Once Work item 6's gateway endpoint is live,
`backfill-actions --source gateway` still supersedes both derived sources
with real `corpact-v1` ex-dates/values, via the same priority list.

### Verification

- New tests in `tests/test_quant_actions.py`:
  `test_derive_from_10q_ytd_differences_a_flat_quarterly_dividend` (three
  successive YTD-tagged 10-Qs correctly difference into three equal
  quarterly dividends), `test_derive_from_10q_prefers_a_discrete_quarter_
  tag_over_ytd`, `test_derive_from_10q_ytd_falls_back_to_aggregate_paid_
  over_shares`, `test_load_actions_prefers_a_single_engine_never_blends_
  two` (proves the priority fix directly: two engines' distinct ex-dates
  for the same asset don't both come back), and
  `test_backfill_derive_writes_both_engines_for_a_10q_only_asset` (an
  asset with only 10-Q dividend facts — the XOM/PG/T/NEE shape — gets a
  non-zero, `load_actions`-visible dividend after `backfill-actions
  --source derive`).
- `uv run pytest -q` — 243 passed (was 238).
- `uv run ruff check` / `ruff format --check` / `uv run mypy` — all green.

### Residual scope, deliberately deferred

- **The `corpact-v1` gateway target (Work item 6)** was unimplemented when this
  fix landed. *(Updated 2026-09-20: it is built upstream —
  `portfolio-data-mining` PR #36 — and `T-085` made it `quant`'s only source,
  removing the derivation this fix added. Verified live 2026-09-21 (`T-052`): 7,481
  gateway rows in production, every asset fetched.)*
- **Not re-verified against the live production database** *(moot after
  `T-085`: the derive path this refers to no longer exists)*. Actually
  confirming XOM/PG/T/NEE show `cash_dividend > 0` in `quant_return_daily`
  requires running `quant backfill-actions --source derive` (idempotent,
  $0, no LLM calls, but still a production-data-mutating operation) against
  `data/financial.db` — deferred to `T-068`'s Phase A re-sequence, same
  category as F1/F2/F4/C1/C2's own "not done as part of this change" notes.
- **No attempt to reconcile disagreeing tag shapes across a single asset's
  own filings** — e.g. a filer that tags Q1 discretely but Q2/Q3
  cumulatively is handled correctly (the running YTD total updates from
  whichever signal was found each quarter), but a filer that tags the same
  quarter both ways with disagreeing values isn't cross-checked; the
  discrete tag simply wins outright per `_quarterly_dps_signal`'s order.

---

## Q2 — Empty forward evaluation: two independent findings, one code fix

**Status**: Fixed 2026-09-15 (`T-067`) — the `evaluate` half. The `frontier`
half needed **no code change**; see below.

### Symptom

`quant_benchmark_performance` and `quant_frontier_point` both had 0 rows.
PLAN.md's audit traced this to two independent causes bundled under one
finding.

### Finding 1 — `evaluate`'s `--from` had no default, and the documented
example value guarantees an empty forward window

`quant evaluate`'s `--from` was `required=True` with no default, and both
`docs/quant.md` and (untracked) `CLAUDE.md` documented running it with the
*same* `--analysis-date` value just used for `build-risk-model`/`optimize`
(e.g. `evaluate --from 2026-08-27 --analysis-date TODAY`, `optimize
--analysis-date 2026-08-27`). That date is, by construction, the **most
recent** date with any ingested price data at all — a book can't be
optimized against price history that doesn't exist yet. Using it as
`evaluate`'s `--from` leaves nothing between it and `--to` for a forward
return to realize over: `_evaluate_book` needs `quant_return_daily` rows
strictly after `as_of` (`load_forward_simple_returns(..., after=as_of,
until=date_to, ...)`), and `_snapshot_live_book`'s own forward window is
`(date_from, date_to]` — both empty when `date_from` already sits at the
data's leading edge. Verified: the audit's own run used
`date_from='2026-08-27'` (where `price_daily` ends) → `date_to=
'2026-09-03'`, netting exactly 0 rows.

**Fix**: `run_evaluate`'s `date_from` is now optional (`str | None = None`);
when omitted, it defaults to `quant.db.earliest_portfolio_as_of` — the
earliest `as_of` across every persisted `quant_portfolio` book, read live
from the database rather than hardcoded or guessed. Starting from the
earliest book maximizes whatever forward window the actually-ingested
price history allows, instead of a caller (or a doc example) picking a
date that happens to be the newest one available. If no book has been
persisted yet and `--from` is also omitted, `run_evaluate` now raises a
clear `ValueError` ("no --from given and no quant_portfolio rows exist
yet...") instead of silently proceeding to evaluate an empty range.
`EvaluateResult` gained a `date_from` field so the CLI's own summary line
reports the value actually used, not `None` when defaulted.
`docs/quant.md`'s example was corrected to stop demonstrating the
anti-pattern, with an explanatory note.

### Finding 2 — `quant_frontier_point`'s 0 rows is not a code defect

`optimize`'s default `--objectives` (`min_var, tangency, target_vol,
risk_parity`) has never included `frontier` — `frontier` is deliberately
opt-in (`objective.py::resolve_objectives` treats it as a recognized but
separately-handled name; `persist.py`'s `if "frontier" in
settings.objectives:` branch only runs when asked). This is confirmed
**already correct and already covered end-to-end** by the pre-existing
`tests/test_quant_pipeline.py::test_optimize_persists_one_book_per_
objective`, which explicitly requests `frontier` and asserts
`frontier_points == 5` plus real, monotone `quant_frontier_point` rows —
passing before this change, untouched by it. The audit's live-DB
observation is fully explained by the specific historical `optimize` run
never having been invoked with `--objectives ...,frontier` — an
operational choice, not a code path that silently drops the request. No
`_DEFAULT_OBJECTIVES`/`persist.py` change was made: flipping `frontier` on
by default would add a real, ongoing computational cost (an
`efficient_frontier` sweep of `frontier_k` extra QP solves) to every
default `optimize` run, a bigger behavioral change than this finding
calls for.

### Design decisions

**Default `--from`, not a hardcoded fallback.** `earliest_portfolio_as_of`
queries the live `quant_portfolio` table rather than any fixed date, so
the default stays correct as new books are optimized over time and needs
no manual updating.

**Fail loudly on the genuinely ambiguous case.** With no books and no
explicit `--from`, there is no sensible default to fall back to — raising
immediately (before `open_run` even creates a `quant_run` row) is more
useful than silently persisting an empty, misleading `evaluate` run.

**`--from` stays a CLI flag a caller can still narrow.** The default
covers "evaluate everything on record"; passing `--from` explicitly still
works exactly as before, e.g. to scope evaluation to books optimized on or
after a specific date.

### Verification

- New tests in `tests/test_quant_pipeline.py`:
  `test_evaluate_defaults_from_to_earliest_optimized_book` (omitting
  `--from` resolves to the earliest persisted book's `as_of` and produces
  real `perf_rows`), `test_evaluate_raises_when_no_from_given_and_no_
  books_persisted`.
- `uv run pytest -q` — 245 passed (was 243).
- `uv run ruff check` / `ruff format --check` / `uv run mypy` — all green.

### Residual scope, deliberately deferred

- **Not re-verified against the live production database.** Whether a
  real Phase-A `evaluate` run against `data/financial.db` now produces
  `quant_benchmark_performance` rows depends on whether `price_daily` has
  actually been re-ingested with dates forward of whatever `quant_
  portfolio.as_of` exists — a data-freshness precondition this fix cannot
  manufacture. Deferred to `T-068`'s Phase A re-sequence, same category as
  every prior fix's "not done as part of this change" note.
- **`quant_frontier_point`'s emptiness is closed operationally, not in
  code**: `T-068`'s Phase-A re-run must explicitly pass `--objectives
  min_var,tangency,target_vol,risk_parity,frontier` to `optimize` for
  `quant_frontier_point` to actually populate — this fix does not change
  `optimize`'s default behavior.

---

## T-095 — Revenue mis-resolution: a `total_concepts` tag mistagged on one small dimensional slice

**Status**: Fixed 2026-09-22 (branch `feat/t095-revenue-total-concepts-plausibility`, `T-095`).

### Symptom

Found by `T-088`'s 20-ticker acceptance audit (2026-09-22): APA's FY2021 10-K
`net_margin` computed to **121.3%** (`fundamental_metrics.net_margin =
1.2134935304990757`, `inputs_json.revenue = 1,082,000,000.0`), implausible
for an E&P company with net income of $1,313M. PLAN.md's initial write-up of
the finding also named PM FY2021/FY2022 as a second instance of the same
mechanism, flagged as needing its own read of the payload before assuming
so.

### Root cause — confirmed for APA, NOT reproduced for PM

Read live against the gateway (`GET /financials/APA?form=10-K&year=2022&
accession_number=0001784031-22-000009`, the FY2021 10-K), read-only,
2026-09-22: APA tags `us-gaap_Revenues` (F2's `total_concepts` set,
label "Total revenues") on **two** rows for FY2021 — a non-dimensional row
valued **$1,082,000,000** and a dimensional row (`dimension=true`,
`dimension_axis=us-gaap:EquityMethodInvestmentNonconsolidatedInvesteeAxis`)
valued the **identical** $1,082,000,000. This is a filer-side XBRL tagging
defect: the generic aggregate concept was (also) used, without a member
context, for what is really one dimensional disclosure slice (APA's
equity-method investee, Altus Midstream/BCP Raptor) — not the consolidated
total. The real total sits on
`us-gaap_RevenueFromContractWithCustomerIncludingAssessedTax`
($7,988,000,000), corroborated by APA's own custom-taxonomy
`apa_RevenuesAndOther` ("Total revenues and other", $7,928,000,000 — the
$60M gap is `apa_OtherSalesRevenueLossesNet`, not separately whitelisted).
F2's `_first_total_match` had no way to distinguish a mistagged
non-dimensional row from a genuine one: it takes the first `total_concepts`
match unconditionally, regardless of magnitude.

**PM does not reproduce this mechanism.** Read live against the gateway for
both FY2021 (`GET /financials/PM?form=10-K&year=2022&
accession_number=0001413329-22-000011`) and FY2022
(`.../PM?form=10-K&year=2023&accession_number=0001413329-23-000025`),
2026-09-22: PM tags **neither** `us-gaap_Revenues` nor
`us-gaap_RevenuesNetOfInterestExpense` (F2's `total_concepts`) at all, in
either filing — `_first_total_match` never fires for PM; Tier 1 is a no-op.
Resolution falls to Tier 2 (`sum_components`), which correctly picks
`us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax` ("Net
revenues", $31,405M FY2021 / $31,762M FY2022) over its
`synonym_groups`-paired `...IncludingAssessedTax` ("Revenues including
excise taxes", $82,223M / $80,669M) — exactly F2's Sourcery-follow-up
design (excluding-tax is the real income-statement line; this is the same
rule already live-verified for PM in F2's own writeup). The stored
`net_margin` for PM FY2021/FY2022 (30.9% / 30.0%, `inputs_json.revenue =
31,405,000,000.0` / `31,762,000,000.0`) is realistic for the filer, not an
outlier. **PLAN.md's initial flag on PM was a misattribution, corrected
here** — the excluding-tax figure it questioned is the intentional, already
independently verified behavior, not a new defect.

### Fix

`src/fundamental_agent/statements.py`, `Statements`:
- New `_total_is_plausible(spec, column, total_value)`: rejects a
  `total_concepts` match when it is **less than 50% of the largest**
  first-matching-row value among `spec.concepts` (a genuine aggregate is
  never far smaller than one of its own named components). A filer with no
  `spec.concepts` rows tagged at all (e.g. JPM's
  `RevenuesNetOfInterestExpense`) has nothing to compare against, so its
  total is trusted exactly as before — the check only ever rejects, never
  invents a floor.
- New `_largest_component_value`/`_matching_component_values` (the latter
  factored out of, and now shared with, `_sum_matching_components` — no
  behavior change to the sum path).
- `get()`: Tier 1 now requires `total_value is not None and
  self._total_is_plausible(...)`; a rejected total falls through to Tier 2
  exactly as if no `total_concepts` row had matched at all.
- The 0.5 floor is a documented, pinned constant
  (`Statements._TOTAL_PLAUSIBILITY_FLOOR`), not a magic number — chosen to
  sit well above every known-correct case (the total exactly equals or
  slightly exceeds the largest component; UDR's real shape, ratio ~1.0,
  F2's own regression fixture) and well below the observed defect (APA's
  ratio ~0.14).

### Verification

- `tests/test_statements.py`: 3 new tests —
  `test_revenue_rejects_a_total_far_smaller_than_a_named_component` (APA's
  real FY2021 values, non-dimensional rows only), and a parametrized pin of
  the exact floor boundary
  (`test_revenue_total_concepts_plausibility_floor_is_pinned`, ratio 0.5
  trusted / 0.49 rejected). Mutation-checked: reverting the plausibility
  gate call in `get()` fails both new floor-boundary assertions.
  `test_revenue_total_concepts_with_no_component_to_compare_is_trusted`
  pins the JPM-shape no-op case.
- Every existing F2 regression test unchanged and still green, including
  `test_revenue_prefers_total_over_components_regardless_of_document_order`
  (UDR, ratio ~1.0 — confirms the floor does not disturb the already-correct
  case the original F2 fix targeted) and the full `jpm_10k`/`aapl_10k`
  fixture suite (JPM's total has no `concepts` rows to compare against at
  all, so it stays trusted unconditionally, confirmed by
  `test_bank_has_no_operating_income_or_current_split`).
- `uv run pytest -q` — 375 passed (was 372 baseline on `master` post-`T-088`,
  +3 new). `uv run ruff check` / `ruff format --check` / `uv run mypy` — all
  green.

**Not done as part of this change** (same discipline as every prior fix):
re-persisting a corrected `fundamental_metrics`/`score_snapshot` row for
APA's FY2021 10-K needs a re-run against production, outside a code-review
pass's authority to run unprompted.

### Residual scope, deliberately deferred

- **The 0.5 floor is a heuristic**, verified against exactly one real
  defect (APA) and one real correct case (UDR); a future counter-example
  (a legitimately much-smaller-than-component total) would need its own
  live verification before changing the constant.
- **Not scanned for other filers/other line items.** This fix targets only
  `revenue`'s `total_concepts` path (the only registry item that sets it);
  whether the same mistagging shape recurs elsewhere in the 20-ticker
  sample or the full 503-asset universe was not swept — `T-088`'s
  acceptance flagged APA specifically, not a broader pattern.

---

## T-096 — 10-Q filing gaps beyond F4's expected fallback: three distinct root causes, two corrected

**Status**: Fixed 2026-09-22 (branch `feat/t096-10q-filing-gaps`, `T-096`) — the WAT local
half. The APO half is a confirmed upstream gap, routed rather than fixed here. The original
PG/BF.B/STZ characterization did not survive a precise reproduction and is corrected.

### Symptom

`T-088`'s 20-ticker acceptance flagged 12 of 278 10-Q filings computing their flows via F4's
`current × 4` fallback for no "first three quarters of stored history" reason — concentrated
in APO (8 of 13), and one instance each in PG, BF.B, STZ, WAT.

### Reproduction — a precise simulation of `db.ttm_flows`/`_quarter_flow_ending`, not a re-guess

The original count was a high-level tally; this task reproduces the *mechanism* by re-running
the exact live logic (`_filing_near`'s date-and-tolerance matching, `_recorded_flow`'s
`fundamental_metrics.inputs_json` read, and the fiscal-Q4-derived-from-10-K-minus-three-
quarters branch of `_quarter_flow_ending`) against the production database, read-only,
2026-09-22 — not a re-derivation of "which periods exist", which undercounts real fallbacks and
overcounts apparent ones. Three distinct root causes emerged, two different from the original
framing:

**1. APO — confirmed upstream, a single defect cascading forward (not 8 independent gaps).**
Read live against the gateway (`GET /financials/APO?form=10-Q&year=2023&accession_number=
0001858681-23-000017`, the 2023Q1 10-Q `filing_by_year` lists): the `/financials` payload's
`income_statement` and `cash_flow` carry **only** the `2022-12-31 (FY)` column — no quarterly
duration period at all — while `balance_sheet` correctly carries both `2023-03-31` and
`2022-12-31`. `Statements.quarter_periods()` finds nothing, so `_targets` returns `[]` and this
filing is **silently never even inserted into `sec_filings`** (not an error, not a skip count).
Every one of APO's other 7 flagged 10-Qs and 10-Ks traces back to this **same single gap**:
`_quarter_flow_ending`'s fiscal-Q4-derivation needs 2023Q1 (`_filing_near` finds nothing near
2023-03-31, by design — it was never ingested), so it cascades forward through every later
quarter/10-K that needs to reach back across that date, until (by 2025Q1) three full years of
otherwise-complete quarters no longer need to. **Decision: upstream** — `portfolio-data-mining`'s
`/financials` extraction for this one accession, per this repo's own rule that only it mines
data. No local code can synthesize duration columns the payload does not carry. Not filed in
that repo directly (no local checkout in this workspace); recorded here in full so it can be
filed from the reproduction above.

**2. WAT — corrected: NOT a missing 10-Q, a `net_income`-concept registry gap.** Re-read live
against the gateway for WAT's 10-Qs across 2022–2026: **every fiscal quarter is present** —
`filing_by_year` lists exactly 3 10-Qs every year, matching `sec_filings`. The original "missing
Q1" read was a **labeling artifact**: the gateway's own period tag for the *same real quarter*
(the one filed each May, ending late March/early April) is inconsistent across years —
`"(Q2)"` in WAT's 2022/2023 filings, `"(Q1)"` in 2024/2025 (confirmed live: even a single 2024
payload tags its *own* current period `2024-03-30` as `"(Q1)"` while its own prior-year
comparative column `2023-04-01` — the identical relative quarter one year earlier — is tagged
`"(Q2)"`). `_targets` trusts the gateway's own tag (`period.tag[1]`) rather than deriving a
quarter number itself (correctly, per F4/T-094's date-based-not-label-based lesson) — no bug
there. **The real defect, found while reproducing**: `net_income` (`REGISTRY["net_income"]`,
`concepts=("us-gaap_NetIncomeLoss", "us-gaap_ProfitLoss")`) silently resolves to `None` for
**14 of WAT's 18 `metrics-v2` filings** (every 10-Q and 10-K from FY2021 through FY2025, except
the four most recent quarters) — confirmed exclusive to WAT within the 20-ticker sample (0
other tickers affected). WAT tags neither whitelisted concept for most of its history; it uses
`us-gaap_NetIncomeLossAvailableToCommonStockholdersBasic` instead (live-verified,
WAT's real 2022-10-01 10-Q: $155,998,000, tagged nowhere else) — switching to the plain
`us-gaap_NetIncomeLoss` tag only starting 2025Q2 (also live-verified; the two never co-occur
non-dimensionally in the same WAT filing). A missing `net_income` poisons `net_margin`, ROA, ROE
directly, **and**, chained through F4's `ttm_flows`, every later quarter's trailing-twelve-month
window that needs this quarter's own recorded value — this is the actual mechanism behind WAT's
flagged 10-Q gap, not a missing filing.

**3. PG/BF.B/STZ — corrected: no extra gap beyond the unavoidable case.** All three have exactly
3 10-Qs every fiscal year, every year, in `sec_filings` — no missing quarter. The precise
simulation's only fallback beyond the "first 3 quarters of stored history" rule for these three
is the **fiscal-year-boundary case**: `_quarter_flow_ending`'s Q4-derivation for their earliest
stored 10-K needs a quarter that predates the start of each ticker's own stored history (e.g.
PG's FY2022 10-K needs its fiscal Q1 ending Sept 2021, one quarter before PG's earliest ingested
10-Q, 2021Q2/Dec 2021) — the same *class* of unavoidable gap the original "first 3 quarters"
rule already accounts for, just one more filing deep because it is reached via the 10-K's Q4
derivation rather than directly. **Not a defect**: the original "PG/BF.B/STZ ×1 each" tally
appears to have come from a coarser check (raw period-membership, not the actual
`_quarter_flow_ending` simulation) that does not account for the Q4-derivation branch; corrected
here rather than left standing.

### Fix

`src/fundamental_agent/statements.py`, `REGISTRY["net_income"]`: added
`us-gaap_NetIncomeLossAvailableToCommonStockholdersBasic` as a third `concepts` candidate.
Document-order resolution (Tier 2's plain first-match — `net_income` sets no
`total_concepts`/`sum_components`) is unchanged; the new concept only ever matches when
neither `NetIncomeLoss` nor `ProfitLoss` is tagged, which is the live-verified WAT shape.

### Verification

- `tests/test_statements.py`: 2 new tests — WAT's real 2022-10-01 10-Q value resolves via the
  new concept, and a defensive regression pinning that `NetIncomeLoss` still wins when both are
  present (the live-verified case never occurs for WAT, but the test documents the intended
  behavior regardless). Mutation-checked: removing the new concept fails the WAT test.
- Confirmed via a live production-DB query: exactly 0 tickers other than WAT, within the
  20-ticker sample, have a `profitability.return_on_assets` row missing `net_income` from its
  `inputs_json` — the fix's blast radius matches what was verified, nothing broader assumed.
- `uv run pytest -q` — **377 passed** (was 375 after T-095). `ruff`/`mypy` — all green.

**Not done as part of this change** (same discipline as every prior fix): re-persisting
corrected `fundamental_metrics`/TTM-dependent rows for WAT's affected filings needs a re-run
against production, outside a code-review pass's authority to run unprompted.

### Residual scope, deliberately deferred

- **APO's upstream gap is recorded, not filed** — this workspace has no local checkout of
  `portfolio-data-mining` to open a task in; the reproduction above (exact accession, exact
  missing columns) is written out in full so it can be filed from here.
- **The `NetIncomeLossAvailableToCommonStockholdersBasic` document-order risk is unverified
  beyond WAT** — a filer that genuinely tags both `NetIncomeLoss` and the "available to common"
  variant, with different values (real for any company with preferred-stock dividends), and
  happens to document-order the wrong one first, is a live, unverified risk; not swept across
  the wider universe.
- **Not swept for other registry items or other tickers.** This fix targets only WAT's
  `net_income` gap, live-verified; whether other items (`shares_outstanding`, `diluted_shares`,
  …) have a similar filer-specific concept gap elsewhere in the 20-ticker sample or the full
  503-asset universe was not checked.

---

## T-097 — Guard against an out-of-order (backdated) `cycle select` run mutating the live book

**Status**: Fixed 2026-09-22 (branch `feat/t097-cycle-out-of-order-guard`, `T-097`).

### Symptom

Found live while validating `T-088` (2026-09-22), not guessed at: to give `quant evaluate` a
forward window, a second `cycle select --analysis-date 2026-06-30` was run *after* an earlier
`select` at a later date. It silently closed a live position early (`valid_to` backdated to
2026-06-30) and left a stray `quant_portfolio(kind='live_book')` row — exactly the class of
false, unnoticed corruption `T-086` closed for `build-returns`. Reverted by hand on production,
with the user's explicit go-ahead first (a direct write to shared state), and confirmed restored
before this task was recorded.

### Root cause

`sync_positions` (`src/cycle/writers.py`, called from `orchestrator.py`'s "positions" step —
**SELECTION only**; `MONITORING` never reaches it, so `monitor` was never actually at risk
despite the original finding's "select/monitor" title, corrected here) always writes the live
`portfolio_position` book unconditionally: it closes whatever is open at `valid_to = cycle_date`
and opens the new targets at `valid_from = cycle_date`, with **no check** that `cycle_date` is
not older than a date the book has already moved to. `cycle_run` is keyed uniquely per
`(cycle_type, cycle_date)` (`check_manifest`), which prevents *resuming* an existing run under a
different manifest, but that guard has nothing to say about a **new**, valid, checkpointable run
at an *earlier* date than one already completed — the exact gap T-086 closed for
`quant_return_daily`'s `INSERT OR IGNORE` lock-in, one layer up.

### Fix

`src/cycle/writers.py`: new `OutOfOrderCycle` (mirrors `quant.actions.DividendsNotReady`) and
`out_of_order_reason(conn, cycle_date) -> str | None` — a pure read of
`MAX(valid_from) FROM portfolio_position` across *every* row, open or closed (a closed
position's `valid_from` still marks a date the book has already moved past), returning a reason
string when `cycle_date` is older. `sync_positions` itself is untouched — same pattern as
`run_build_returns` checking `dividends_not_ready_reason` before writing, not the low-level
writer checking itself.

`src/cycle/orchestrator.py`: the `_positions()` closure checks `out_of_order_reason` before
calling `sync_positions`, raising `OutOfOrderCycle` unless
`settings.allow_backdated_positions` is set; the bypass reason (when overridden) is recorded on
`CycleReport.backdated_guard_bypassed` for the CLI to surface — the same shape as
`ReturnsReport.dividends_guard_bypassed` (T-086).

`src/cycle/config.py`: new `CycleSettings.allow_backdated_positions: bool = False`.

`src/cycle/cli.py`: `--allow-backdated` added **only to `select`'s** parser (not `monitor`'s —
it would be a silent no-op there, since monitor never reaches the positions step); threaded into
`CycleSettings`; `OutOfOrderCycle` added to `main()`'s existing generic exception-to-exit-1
handler (alongside `VersionError`/`ManifestMismatch`, matching cycle's existing simpler style
rather than quant's dedicated multi-line handler — the reason string itself is already
complete and actionable); a `WARNING` line printed when the override was used, mirroring
`build-returns`'s own bypass warning.

**Review follow-up (2026-09-22, PR #58, automated review)**: a refused run's own `cycle_run` row
was originally left stuck at status `"running"` forever — `finish_cycle` was only ever called
once, at the very end of `_run`, on the success path, so *any* exception raised mid-run (this
guard's included) skipped it with no failure transition, even though `cycle_checkpoint`'s own DDL
comment already documented `'failed'` as an expected status no code wrote. Fixed generally, not
just for this guard: `_do()` now wraps `fn()` in `try`/`except` and marks that step's own
checkpoint `"failed"` (with the error message) before re-raising; `_run()`'s whole step sequence
is now one `try`/`except` that marks the parent `cycle_run` `"failed"` too, then re-raises
unchanged. `docs`/tests below updated to match (a refused run's `cycle_run` is now `"failed"`,
not `"running"`).

### Verification

- 4 new tests in `tests/test_cycle.py`: the pure `out_of_order_reason` function (no rows → safe;
  newer/same date → safe; older date → a reason naming both dates and `--allow-backdated`); a
  full `run_selection` at an older date raises `OutOfOrderCycle`, leaves the position book
  untouched, and marks both the refused run's `cycle_run` row and its `"positions"` checkpoint
  `"failed"` (not partially applied, and not left stuck `"running"`); the override succeeds,
  records the bypass reason on the report, and a closed position's `valid_from` still protects a
  *further* out-of-order attempt afterward. Mutation-checked: removing the guard call fails two
  of the four tests; separately, removing either the per-step or the per-run `"failed"` transition
  each fails the refusal test's new status assertions (checked by hand, not left in the suite).
- `uv run pytest -q` — **380 passed** (was 377 before T-097). `ruff`/`mypy` — all green.

**Not done as part of this change**: no production re-run needed — the incident this task
documents was already found and reverted by hand during `T-088`'s own validation, before this
task existed; this change only prevents a recurrence.

### Residual scope, deliberately deferred

- **`cycle backfill`** (the `--from`/`--to` loop subcommand) has no `--allow-backdated` flag of
  its own and will hit the same `OutOfOrderCycle` refusal, with no way to override it from that
  path, if run against a live book with a later `valid_from` than its own range — not exercised
  by T-088's incident (which used `select` directly) and not covered by this task's acceptance
  criteria; flagged, not built.
- **The guard is a `MAX(valid_from)` check, not a full ordering log** — a book that was itself
  corrupted by an *already-reverted* out-of-order run (as the original incident was) is
  indistinguishable, after cleanup, from one that was never touched; this guard only prevents a
  *future* recurrence, it cannot detect a *past* one that predates it.
- **Two further automated-review findings on PR #58, considered and declined**: (1) the guard's
  read (`out_of_order_reason`) and `sync_positions`'s write are not wrapped in one serialized
  transaction, so two genuinely *concurrent* `select` processes could both pass the check before
  either commits — not fixed, because nothing in `cycle`'s checkpoint/resume design assumes
  concurrent writers to begin with (every step here is read-checkpoint-write with its own
  `commit()`, not one enclosing transaction; `T-090`'s manifest check has the identical
  check-then-act shape), so this would be a new concurrency contract for the whole package, out
  of scope for one guard. (2) the refusal is "not atomic" in that the scoring/veto/ranking steps
  ahead of `"positions"` already committed by the time it fires — by design: those steps are
  idempotent per `(cycle_type, cycle_date)` and safe to have run (they are what `T-086`'s
  `DividendsNotReady` guard does too, checked deep in `run_build_returns`, not before every
  upstream step); the guard's only job is to keep the *live* `portfolio_position` book itself
  from being corrupted, and it does, unconditionally, before `sync_positions` runs.

---

## T-065 — Ring-1 deterministic data-quality gates (`DQ_*`)

**Status**: Built 2026-09-25 (branch `feat/t065-ring1-dq-gates`, `T-065`, with its table
`T-040`). Verified against a copy of production; production's own `data_quality_issue` is
filled by `T-100`'s full-universe run (it was already deferred there by `T-068`).

### Symptom

The 2026-09-08 forensic audit (`PLAN.md` Work item 7) found metric values that are not
business facts but data errors — margins above 500%, FCF yields in the thousands, market
capitalisations a million times too small — flowing unchecked into `cycle`'s scores and
vetoes. F1/F2/F4 fixed the causes it identified, but nothing caught the *next* unknown cause.
Re-checked against today's production (the 20-asset `T-068` sample, `metrics-v2`, 377
filings), the symptoms are still present: MCD's FY2023–2025Q2 filings (7) store a market cap
of about $200k (true: about $210B) and FCF yields of 318–33,410; NEE's 19 filings have no
revenue at all while reporting net income.

### Root cause — re-derived, not assumed

The two live cases have different causes, both outside the gates' job to fix:

- **MCD**: `inputs_json.shares` is `732.3` (FY2023) through `717.6` (2025Q2) — share counts
  tagged in millions — against `747,600,000`-style values before and after. It is F1's
  documented class (SEC scaling notices, F1's reference section), in a run of consecutive
  mis-scaled filings where F1's overlapping-history anchor is itself mis-scaled and EPS
  corroboration only covers `diluted_shares`. F1 fixed FY2025; these seven are its residual.
- **NEE**: the income statement reports `us-gaap_RegulatedAndUnregulatedOperatingRevenue`
  ("OPERATING REVENUES", a utility tag), which `statements.REGISTRY["revenue"]` does not list,
  so revenue resolves to NULL and every revenue-denominated ratio is NULL.

Both are tracked as their own fixes (`T-102`, `T-103`). The gates exist so that a defect like
these is quarantined and vetoed until it is fixed, instead of scored.

### Theoretical/technical reference

- **Scale errors** (`DQ_MCAP_SCALE`, and the FCF-yield/margin ceilings that they surface as):
  the SEC staff notices cited in F1 — filers tagging values off by a clean power of ten
  ("three additional zeros"). A market cap outside `[0.001, 100] ×` total assets is five
  orders of magnitude of tolerance around any real company, so a value outside it is a unit
  error, not a valuation.
- **Sign checks** (`DQ_REVENUE_POS`): XBRL US Data Quality Committee rule
  [DQC_0015 "Negative Values"](https://xbrl.us/data-rule/dqc_0015/): "The US GAAP Taxonomy is
  designed so that the majority of elements have a positive value. This rule tests whether the
  values for a given list of elements are negative." (Cited for the principle — sign checks on
  elements that should be positive — not for its exact element list, which was not verified to
  include `Revenues`.) A missing revenue beside a reported net income is the same class of
  defect: the filing's top line did not resolve.
- **Non-positive denominators** (`DQ_NEG_EQUITY`): a ratio over book equity changes sign when
  equity does, so D/E and ROE stop ordering companies by what they measure — C2's entry derives
  this against MCD's data; the HARD condition reuses the thresholds C2 already adopted.
- **Thresholds**: `PLAN.md` Work item 7's table, calibrated by the audit on the pre-fix
  production database (21 / 50 / 74 / 39 / 11 / 342 / 0 filings). Not recalibrated here.

### Fix

- **`data_quality_issue`** (`kg_schema/ddl.py`, T-040 — built here: `kg_schema` is vendored,
  so the "upstream `portfolio-common`" framing of Work item 5 no longer applies) plus the
  read-contract view `v_data_quality_issue`.
- **`fundamental_agent/quality.py`**: the seven gates as pure functions over a filing's stored
  metrics (value + `inputs_json`), recorded one row per gated metric. Run automatically after
  each filing's metrics are stored (`pipeline._analyze_one`), and as a backfill by
  `python -m fundamental_agent quality`.
- **`cycle`**: `data.data_quality()` reads the verdicts on each asset's latest filing;
  quarantined metrics read as NULL before any score or rule sees them (including the market
  cap used for size and earnings yield); a HARD verdict raises the new `DATA_QUALITY` veto.

### Design decisions

- **Keyed by metric engine version and gate version** (a deviation from `T-040`'s
  `UNIQUE(filing_id, metric_name, rule_id)`): T-090 lets a consumer choose among parallel
  metric versions, so a verdict must name the version it judged — `cycle` reading `metrics-v3`
  is never quarantined by a verdict on `metrics-v2`. `gate_version` (`dq-v1`) makes a future
  threshold change a parallel set of rows, the same append-only rule as every measurement
  table. `metric_group` joined the key because metric names are only unique within a group.
- **Quarantine and severity are separate columns.** `DQ_NEG_EQUITY` quarantines D/E and ROE
  whether or not the firm is distressed, but is HARD only when it is; `DQ_MARGIN_REVIEW` is
  SOFT and quarantines nothing (a review item). A SOFT verdict never becomes a SOFT veto —
  that would penalise the score, which `PLAN.md` reserves for the review list.
- **C2 is preserved.** With D/E quarantined, a negative-equity name would otherwise lose its
  leverage penalty in VALORIZATION; `cycle` ranks it as the worst leverage instead
  (`float("inf")`, C2's own transform). `_LeverageRule`'s negative-equity branch stays as a
  backstop for filings not yet gated; on gated ones the HARD case surfaces as `DATA_QUALITY`.
- **The gates live with the writer.** `fundamental_agent` owns `fundamental_metrics`; `cycle`
  only reads the table (it never imports an agent). The shared `DATA_QUALITY_GATE_VERSION`
  lives in `kg_schema.versions` so both agree without importing each other. The gate's read
  goes through T-090's `VERSION_FILTER_SQL`, so the reader guard holds.
- **`cycle`'s manifest names the gate version** (`"quality": "dq-v1"`): its output depends on
  it. Consequence: an existing `cycle_run` recorded before T-065 has a different manifest tag,
  so re-running *that same date* is refused by T-090's guard — new dates are unaffected.
- **Same T-1 lag as every veto** (FR-006): a verdict found on cycle N applies from N+1.
- **`quant` does not read the quarantine yet**: its equilibrium weights use the valuation
  group's market cap directly. Flagged below.

### Verification

- `tests/test_data_quality.py` (new): each gate's trigger and boundary (the strict
  inequalities of the table), per-metric recording, idempotence, per-version judging, the
  single-filing path, the CHECK and the view, the `quality` command, `cycle` reading only the
  latest filing's verdicts for its versions and gate version, a HARD gate vetoing end to end
  from the next cycle, a quarantined metric dropping out of the score, and negative equity
  keeping the worst leverage without a veto. `tests/test_pipeline.py`: the run gates every
  analysed filing. Mutation-checked by hand: dropping the quarantine, the veto context or the
  C2 transform each fails a test.
- Backfill on a copy of production (`quality`, 15 s, $0): 377 filings, 264 issue rows —
  `DQ_FCF_YIELD` 7 (MCD), `DQ_MARGIN` 0, `DQ_MARGIN_REVIEW` 6 (APO, CPT, HOOD), `DQ_OCF_MARGIN`
  1 (APO 2022Q1), `DQ_MCAP_SCALE` 7 (MCD, the same filings), `DQ_NEG_EQUITY` 41 (19 HARD:
  SBAC, debt/assets > 1; MCD, APA, APO SOFT), `DQ_REVENUE_POS` 19 (NEE). A second run inserts
  0 rows. The audit's counts were for the full pre-fix universe, so they are not comparable
  to a 20-asset sample; the pattern (scale and sign defects, negative equity) is the same.
- `cycle monitor --analysis-date 2026-09-23` on that copy: HARD vetoes NEE (`DATA_QUALITY`,
  `DQ_REVENUE_POS`) and SBAC (`DATA_QUALITY`, `DQ_NEG_EQUITY` — it was `LEVERAGE_EXTREME` on
  2026-09-22 under the same thresholds), MA unchanged (`LEVERAGE_EXTREME`, D/E 4.39). Net
  change on the live sample: NEE is now excluded until its revenue resolves.

### Residual scope, deliberately deferred

- **Production backfill** — `T-100` (full universe), per `T-068`.
- **`T-102`** NEE's revenue concept; **`T-103`** MCD's FY2023–2025Q2 share scale.
- **`quant` reading the quarantine** (market cap in the equilibrium weights) — not in
  `PLAN.md`'s acceptance; the MCD filings it would matter for are historical as-ofs.
- **`DQ_SECTOR_Z`** — Work item 8's companion, once robust standardization exists.
- **F1's ambiguous-case branch** could now also record into `data_quality_issue`
  (F1's residual note); not wired here.

---

## T-102 — Utilities' total operating revenue never resolved

**Status**: Fixed 2026-09-25 (branch `fix/t102-utility-revenue-concept`, `T-102`), metrics
engine bumped to `metrics-v3`. Production is not recomputed here (`T-100`).

### Symptom

`T-065`'s gates, backfilled on a copy of production, raised `DQ_REVENUE_POS` on all 19 NEE
filings: net income reported (e.g. FY2025 $5,332M), revenue NULL — so gross, operating and
net margin, both cash-flow margins, asset turnover and revenue growth were NULL too, and
`cycle` HARD-vetoed NEE in every run.

### Root cause — verified against the filings, then across the sector

NEE's income statement carries its revenue on one row, `us-gaap_RegulatedAndUnregulatedOperatingRevenue`
("OPERATING REVENUES", $27,412M for FY2025). `statements.REGISTRY["revenue"]` knew neither
that concept as a total nor as a component, and the `label_contains` fallback does not apply
to revenue, so `Statements.get("revenue")` returned None.

To see whether NEE was alone, the latest 10-K of every as-of S&P 500 utility (31, from
`universe.db` as of 2026-09-25) was fetched through the EDGAR gateway and resolved with the
pre-fix registry: **6 of 31 returned None** — AWK, DTE, DUK, NEE, SRE, XEL — and every one of
them tags its income-statement total only as this concept. Where the filer also reports the
breakdown, the lines sum to it exactly: DTE 8,849 + 6,965 = 15,814; DUK 29,060 + 2,870 +
307 = 32,237; SRE 7,319 + 4,552 + 1,831 = 13,702; XEL 12,160 + 2,452 + 57 = 14,669 ($M).
None of the other 25 utilities carries the concept.

### Theoretical/technical reference

US-GAAP taxonomy element `RegulatedAndUnregulatedOperatingRevenue` (standard label
"Regulated and Unregulated Operating Revenue"; documentation: "The total amount of operating
revenues recognized during the period"; credit, duration), whose children in the taxonomy's
presentation are `RegulatedOperatingRevenue` and `UnregulatedOperatingRevenue` — checked via
[Calcbench's element page](https://www.calcbench.com/element/RegulatedAndUnregulatedOperatingRevenue).
It is an aggregate by definition, which the four exact sums above confirm on real filings.

### Fix

`us-gaap_RegulatedAndUnregulatedOperatingRevenue` added to `REGISTRY["revenue"].total_concepts`
(beside `Revenues` and the banks' `RevenuesNetOfInterestExpense`). As a total it is used alone —
never summed with the regulated/unregulated lines (F2's rule) — and it passes through T-095's
plausibility floor like the other totals. `METRICS_ENGINE_VERSION` → `metrics-v3`.

### Design decisions

- **A total, not a component.** Listing it in `concepts` would work for NEE/AWK (one row) but
  is wrong in kind; and adding the regulated/unregulated lines as components instead would
  sum XEL's company-specific `xel_RegulatedOperatingRevenueElectric` out (not a us-gaap tag),
  under-counting it. The total is the reported figure.
- **Why bump the version.** Production holds `metrics-v2` rows for NEE with NULL margins;
  writing corrected rows under the same label would make two different computations share a
  version — the collision `engine_version` exists to prevent (T-088's reasoning). Consequence
  (T-090's resolver takes the newest version *present* per group): once any run writes
  `metrics-v3` rows for only part of the universe, `cycle`/`quant` should pin
  `--metrics-version metrics-v2` until the full recompute (`T-100`) has written `v3` for all.

### Verification

- Survey after the fix: all 31 utilities resolve; the 6 changed ones have net margins of
  9.2–21.6% (DTE lowest, AWK highest), plausible for utilities; the other 25 resolve to the
  same value as before. NEE's 10-Qs resolve as well (8 checked across 2022, 2024, 2026 —
  e.g. 2022Q1 $2,890M, net income −$693M, matching the gate's recorded evidence).
- Real captured fixtures added: NEE FY2025 10-K (the total alone) and XEL FY2025 10-K (the
  total beside its lines). Tests: both resolve to the reported total; the new total still
  loses to a far larger named component (T-095 floor); NEE's engine-computed metrics raise no
  `DQ_REVENUE_POS`. Mutation-checked: removing the concept fails the three real-filing tests.
- `uv run pytest -q` — 561 passed (was 557). `ruff`/`mypy` green.

### Residual scope, deliberately deferred

- **Production re-persist** — `T-100`'s full recompute writes the `metrics-v3` rows; then a
  `quality` backfill gates them.
- **Only utilities' 10-Ks were surveyed** for this concept (it is a utility-industry element);
  other sectors were not swept for other unrecognised revenue totals.

---

## T-103 — F1 residual: share-scale evidence read from the future

**Status**: Fixed 2026-09-25 (branch `fix/t103-share-scale-runs`, `T-103`), under
`metrics-v3` (T-102's bump; no `metrics-v3` row had been persisted yet). Production is not
recomputed here (`T-100`).

### Symptom

`T-065`'s gates flagged seven consecutive MCD filings, FY2023 through 2025Q2
(`DQ_MCAP_SCALE` + `DQ_FCF_YIELD`): stored market caps of $184k–$224k (true: $184B–$224B)
and FCF yields of 318–33,410. The diluted share counts are tagged in millions (`732.3` …
`717.6`); F1 exists to correct exactly this, and did correct MCD from 2025Q3 on — but not
these seven.

### Root cause — re-derived; T-103's own framing was wrong

The task text guessed that F1's history anchor was "itself mis-scaled inside the run" and
that EPS "only covers `diluted_shares`". Reproducing FY2023 with its real gateway payload
against production showed otherwise — the EPS signal *did* say ×10⁶ (8,468.8M / $11.56 =
732.6M against 732.3), and was overruled:

- `overlapping_history` read facts from **every other filing of the company, including ones
  filed later**. For FY2023 it found FY2023 restated as 732.3 by the FY2024 10-K (filed
  2025-02-25) and FY2025 10-K. That is a look-ahead — this repo never lets a filing's
  analysis read anything filed after it — and here it was also wrong evidence.
- F1 treats direct evidence *at the target period* as decisive. The later filings' 732.3
  matched the defective 732.3 exactly, so history declared the period **clean**, vetoing
  the EPS signal's correct ×10⁶. The same happened to all seven filings (each is restated,
  mis-scaled, by a later one), while from 2025Q3 no later mis-scaled restatement existed.

Removing the look-ahead alone was not safe. Re-running detection old vs. point-in-time over
**all 5,076 filings in production** (statements rebuilt from their stored facts) showed the
look-ahead had also been *masking* false signals, usually with a clean later restatement:

| Filing | Point-in-time alone would apply | Why it is wrong |
|---|---|---|
| ALL 2023Q3 | diluted ×0.1 | net income −$5M, but EPS divides −$41M *available to common* ($36M preferred dividends) |
| MCHP FY2025 | diluted ×0.1 | EPS −$0.01: rounding to the cent is far beyond the 10% tolerance |
| HAL 2022Q3–2024Q3 | diluted ×10⁻⁶ | the EPS fact is itself mis-scaled ($0.60 tagged 600,000) |
| RTX FY2022 | outstanding ×0.001 | its only earlier anchor is FY2021's own defective 1,708,065 |

### Theoretical/technical reference

- **No look-ahead**: a value used in a point-in-time analysis may depend only on information
  available at that time — the repo's own `--analysis-date` contract (SPEC.md FR-001: skip
  filings with a `filing_date` after the as-of date), applied here to the history a filing's
  own check may read.
- **EPS identity**: ASC 260-10-45-11 — "income available to common stockholders shall be
  computed by deducting both the dividends declared in the period on preferred stock ... and
  the dividends accumulated for the period on cumulative preferred stock ... from income from
  continuing operations and also from net income" (a net loss is *increased* by them); EPS is
  that numerator over the weighted-average share count (verified via
  [Deloitte DART, EPS Roadmap §3.2](https://dart.deloitte.com/USDART/home/codification/presentation/asc260-10/roadmap-earnings-per-share/chapter-3-basic-eps/3-2-income-available-common-stockholders)).
  Plain net income is the wrong numerator whenever preferred dividends exist.
- **Scale errors**: the SEC staff notices cited in F1 (values off by a clean power of ten).

### Fix (`fundamental_agent/db.py`, `statements.py`)

1. **Point-in-time history**: `overlapping_history` reads only filings filed strictly
   before the one analysed (undated current filing: old behaviour; undated earlier rows:
   skipped).
2. **EPS numerator**: net income *available to common* (new registry item
   `net_income_to_common`, diluted then basic concept) where reported, plain net income
   otherwise.
3. **EPS bounds**: only `$0.10 ≤ |EPS| ≤ $10,000` is used — below, cent rounding alone can
   exceed half the tolerance; above, the per-share figure is itself a scale defect.
4. **Cross-item signal** for `shares_outstanding`: when EPS confirms the same filing's
   diluted count at the target (within the 10% tolerance), outstanding is compared with it
   by order of magnitude (within ±25% of a power of ten) — same decade: clean; otherwise
   that power is a correction candidate. Diluted is never judged by outstanding (both are
   often mis-scaled together, and EPS is the stronger witness — F1's existing test).

### Design decisions

- **±25% cross-item band, not ×2.** Outstanding (a balance) and weighted diluted (a period
  average) differ by one period's buybacks and issuance. A ×2 band snapped NVR's *shares
  issued* (20.6M, a different concept the registry falls back to) onto 2.06M against ~3.5M
  outstanding; ±25% leaves it alone and still accepts RTX FY2021 (12% from 10³).
- **Conservative by construction.** Every signal can still only produce a correction when
  the signals that have an opinion agree on one factor; any direct "clean" still vetoes.
  A missed correction is quarantined by `DQ_MCAP_SCALE`; a false 10× correction usually is
  not — so each guard errs toward leaving a value alone.

### Verification

- **All 5,076 production filings, old vs. new** (stored facts, read-only): 14 corrections
  before → 24 after; 14 filings changed:
  - **gained, true** (10 filings): MCD FY2023–2025Q2 ×10⁶ (all 7), DLR FY2022 ×10³
    (297,919 diluted vs 303.6M EPS-implied — the old check missed it on DLR's preferred
    dividends), ECHO 2024Q3 ×10³ (271,736 vs 276.5M);
  - **gained, value-improving** (3): AEP 2022Q3/2023Q3/2024Q3 outstanding ×10 — the tagged
    51,868,653 disagrees with AEP's own 10-Ks (524M) and is likely another entity's figure
    in a combined filing, not a unit slip; ×10 lands within 1% of the EPS-confirmed diluted
    count, which is closer to the truth than before but by magnitude, not mechanism;
  - **dropped, false** (2): AEP FY2021/FY2022 outstanding ×0.1 — the old code borrowed a
    *later* 10-Q's 51.9M to shrink a correct 524M;
  - every other old correction (CHD, COP, ECHO 2025Q3, MCD 2025Q3+, RTX FY2021, TER, V,
    WAT) is unchanged; ALL/MCHP/HAL/RTX FY2022/NVR stay uncorrected.
- **Gates**: MCD's seven filings with ×10⁶ applied — market cap $184–224B, 3.4–4.0× total
  assets, FCF yield 0.6–3.3%: `DQ_MCAP_SCALE` and `DQ_FCF_YIELD` clear (the SOFT
  `DQ_NEG_EQUITY` remains — MCD's book equity is genuinely negative). Computed by scaling
  the stored valuation inputs; the pipeline path itself is covered by F1's valuation tests.
- Tests (`tests/test_share_scale.py`, +10): MCD's run with a later mis-scaled restatement;
  point-in-time history (dated, undated); one test per guard on ALL/MCHP/HAL/RTX×2/NVR's
  shapes; `_magnitude_offset`. Mutation-checked: removing the date filter, the
  to-common numerator, the EPS bounds or the cross-item signal, or widening the band to ×2,
  each fails its tests. F1's existing tests pass unchanged.
- `uv run pytest -q` — 571 passed (was 561). `ruff`/`mypy` green.

### Residual scope, deliberately deferred

- **Production re-persist** — `T-100`'s full recompute under `metrics-v3`, then `quality`.
- **Concept mismatches, not scale defects** (NVR's *issued* for outstanding; AEP's
  51.9M): the registry's `shares_outstanding` fallback to `CommonStockSharesIssued` and
  combined-filing entity selection are ingestion questions, not F1's.
- **A run of mis-scaled `shares_outstanding` with no EPS-confirmed diluted count** is still
  uncorrected (no independent witness); `DQ_MCAP_SCALE` quarantines it.
- **An earlier-filed restatement of the target period itself** (an amendment) is still
  decisive direct evidence; not observed in production.

---

## T-105 — F4's deferred ratios: FCF yields, net debt / EBITDA, ROIC on 10-Qs

**Status**: Fixed 2026-09-25 (branch `fix/t105-annualize-remaining-10q-ratios`, `T-105`), under
`metrics-v3` (T-102's bump; no `metrics-v3` row persisted yet). Production is recomputed by
`T-100`.

### Symptom

F4 annualized ROA/ROE and the turnovers and listed two more flow-over-stock ratios as deferred
(its residual scope). The second audit (`feedback_plan 1.md`) and production confirm they are
live, plus the FCF yields F4 never listed. 10-K vs 10-Q medians on production (metrics-v2): FCF
yield 3.4% vs 0.9% (and the enterprise and SBC-adjusted variants), `net_debt_to_ebitda` 2.62×
vs 10.47× (MCD: 2.7× on its 10-K, ~10× on every 10-Q), ROIC 14.2% vs 4.0%. The yields and ROIC
feed `cycle`'s VALORIZATION, so a name whose latest filing is a 10-Q scored ~4× worse on value.

### Root cause

`valuation.py`, `leverage.py` and `roic.py` ignored the `ttm` their `compute` receives and
divided the 10-Q's single-quarter FCF / EBITDA / NOPAT by a market cap, a net-debt stock or an
invested-capital stock. A second, hidden cause: F4's TTM sums four *recorded quarters*, but
many filers' 10-Q cash-flow statements carry only year-to-date columns (XOM), so no quarterly
cash-flow value ever exists to sum — for them FCF was not annualizable at all by F4's method.

### Theoretical/technical reference

- A flow-over-stock or flow-over-price ratio needs the flow on the stock's annual basis — F4's
  own rationale, unchanged.
- The trailing-twelve-month identity **TTM = latest fiscal year + current YTD − prior YTD** is
  the standard practitioner construction ([Wall Street Prep, "Trailing Twelve Months (TTM)"](https://www.wallstreetprep.com/knowledge/ttm-trailing-twelve-months/);
  [CFI, "LTM"](https://corporatefinanceinstitute.com/resources/valuation/last-twelve-months-ltm/)):
  the fiscal year covers twelve months, adding this year's YTD extends it, and subtracting the
  prior year's same YTD removes the overlap. It needs only the filing's own two YTD columns
  (every 10-Q reports its prior-year comparative) and the prior 10-K.

### Fix

- `db.ttm_detail` (new; `ttm_flows` keeps its contract): per flow, the YTD identity first — the
  prior 10-K must end strictly between last year's YTD end and this period end, so it is the
  fiscal year that YTD belongs to — then F4's four recorded quarters, then quarter × 4. Each
  result carries its method (`TTMFlow`).
- `pipeline._ttm_flows` builds each flow's YTD pair from the filing's own columns
  (`_ytd_columns`: this period's `(YTD)` and the one ~a year earlier, ±20 days for 52/53-week
  calendars; a Q1 filing's YTD is its `(Q1)`), over nine flows now (net income, revenue, cogs,
  operating cash flow, capex, operating income, D&A, interest expense, SBC).
- `valuation.compute` takes `ttm`: FCFE, FCFF (with TTM interest) and SBC-adjusted FCF are TTM
  on a 10-Q — and never a TTM flow beside a raw quarter (a missing TTM input gives `None`).
  `leverage`: EBITDA = TTM operating income + TTM D&A. `roic`: ROIC uses TTM NOPAT; the `nopat`
  metric itself stays the filing's own period value, as reported.
- Raw single-period values stay in every audit dict (later filings read them back); the
  annual-basis numerators are added as `*_ttm`. Each annualizing group is stamped
  `annualized_ttm = 1` or `annualized_x4 = 1` so a consumer can exclude crude annualization
  (the second audit asked for this provenance).

### Design decisions

- **The YTD identity first, not only as a fallback**: it is exact whenever its three inputs
  exist, needs no quarter to have been ingested, and covers YTD-only cash-flow statements.
  Four recorded quarters remain the second choice; × 4 the last, and flagged.
- **All nine flows through the same path**, including F4's three — so ROA/ROE/turnovers also
  prefer the identity now. On the sample they barely move (10-K/10-Q ROA median ratio 1.09).
- **`interest_coverage` stays quarterly/quarterly** — a flow over a flow is already
  basis-consistent (median ratio 1.01).

### Verification

- Recomputed with the new code over every production filing of the 20-asset sample (statements
  rebuilt from stored `financial_facts`, prior 10-Ks read from stored metrics; read-only):

  | Metric | 10-K median | 10-Q median | K/Q before → after |
  |---|---|---|---|
  | FCF yield | 3.31% | 3.83% | 3.64 → 0.86 |
  | enterprise FCF yield | 3.16% | 3.32% | 3.88 → 0.95 |
  | SBC-adjusted FCF yield | 3.19% | 3.38% | 3.66 → 0.94 |
  | net debt / EBITDA | 2.62× | 2.75× | 0.25 → 0.95 |
  | ROIC | 14.2% | 14.8% | 3.54 → 0.95 |
  | ROA (F4) | 6.15% | 5.62% | 1.07 → 1.09 |

  10-Q FCF yields now exist for 97 filings (YTD-only filers included). Methods: the identity for
  1,789 of 1,891 flows (94.6%; per flow 84% for cogs to 98% for operating cash flow), four
  quarters for 10, × 4 for 92.
  No recomputed |FCF yield| exceeds `DQ_FCF_YIELD`'s 0.5.
- XOM 2025Q3 net income TTM = 35,063 − 27,108 + 23,155 = 31,110 ($M), consistent within one
  concept (`ProfitLoss`, see residual); on attributable net income the same identity gives the
  audit's 33,680 − 26,070 + 22,343 = 29,953.
- `tests/test_ttm_t105.py` (+12): the identity (XOM's YTD-only shape), its precedence over four
  quarters, the fiscal-year guard, YTD column selection (incl. Q1 and a 52/53-week calendar),
  yields/EBITDA/ROIC on TTM with raw values kept, no TTM/raw mixing, the annualization stamps.
  Mutation-checked: yields on the quarter, quarterly EBITDA, quarterly NOPAT, no identity, or
  no fiscal-year guard each fails a test. F4's tests pass unchanged; `test_pipeline`'s two TTM
  tests now also assert the method.
- `uv run pytest -q` — 601 passed (was 589). `ruff`/`mypy` green.

### Residual scope, deliberately deferred

- **Production re-persist** — `T-100`'s full recompute under `metrics-v3`.
- **`net_income` includes non-controlling interests for filers whose statement lists
  `ProfitLoss` before `NetIncomeLoss`** (XOM): a pre-existing concept-order choice in
  `statements.REGISTRY`, not a TTM question; TTM is consistent within whichever concept wins.
- **`T-116`** can now calibrate the negative-equity distress screen on annualized
  `net_debt_to_ebitda`.
- **APA's revenue** (`T-117`, local guard; `T-118`, upstream root cause) — see the review
  follow-ups below.

### Review follow-ups (PR #77, 2026-09-25)

A reviewer asked for four changes before merge; two further items became tasks (`T-117`,
`T-118`).

1. **Provenance covers every valuation input.** `_GROUP_TTM_ITEMS["valuation"]` now includes
   `stock_based_compensation` and `interest_expense`: the SBC-adjusted yield and FCFF use them,
   so a × 4 on either stamps the group `annualized_x4`, not `annualized_ttm`.
2. **TTM reads are version-pinned.** `_recorded_flow` (and so the identity's prior 10-K and the
   four-quarter path) reads only rows of the engine version being written; a prior filing
   recorded only by an older engine reads as missing and the TTM falls through to its next
   method, never combining two engines' concept resolution. In production this is what the
   ordered full recompute (`T-100`) relies on: each filing's prior year is recorded under the
   same version first.
3. **Identity vs four-quarter cross-check.** Where both are computable, `TTMFlow.alt` carries
   the four-quarter sum; `quality.record_ttm_crosscheck` (called by the pipeline after the
   metrics are recorded) writes a **SOFT, unquarantined** `DQ_TTM_CROSSCHECK` row under the
   pseudo-group `ttm` with both values when they differ by more than 1%. The identity stays the
   value: it uses the filing's own *restated* comparative. Neither side is presumed right —
   AT&T after the 2022 WarnerMedia spin-off is the case where the as-filed quarters are the
   wrong side (Q1 2022 cogs $10.351B as filed, $6.036B restated). On the 20-asset sample: 780
   flows computable both ways, 16 over 1% — AT&T 2023Q1–Q3 (revenue 6.9%, cogs ~18.5%,
   operating income and interest 1.5–3.7%: the restatement), APA 2024Q1–Q3 revenue 11–25%
   (the `T-117` defect, differing filing by filing), WFC 2023Q3 net income 1.1%.
4. **Verification script.** `scripts/verify_t105.py` reproduces, read-only from stored facts:
   the before/after median table above, the TTM methods, the cross-check count **and each
   flagged flow** (ticker, period, flow, both values, gap), and APA's resolved revenue against
   its consolidated "Total revenues" — derived as "Total revenues and other" less the lines
   between the two totals, since that line is not stored as its own fact after FY2022:
   exactly 2.00× in FY2023–FY2025 (16,558 / 19,474 / 17,840 vs 8,279 / 9,737 / 8,920 $M),
   1.00 in FY2021–FY2022. (A second review round corrected the comparison line: against
   "Total revenues and other" it only matched in FY2024, where the in-between items net to
   zero.)

Tests (+4, mutation-checked): the valuation stamp with an SBC or interest × 4; an older
engine's prior year not feeding the TTM (and the same engine's doing so); the cross-check on
AT&T's restated shape (identity kept, review row with both values); no review within 1% or
with only one method. `uv run pytest -q` — 605 passed.

---

## T-106 — `cycle` read filings before they were filed

**Status**: Fixed 2026-09-25 (branch `fix/t106-point-in-time-cycle-readers`, `T-106`).

### Symptom

A `cycle` run on date D read, for each asset, the newest filing whose **period had ended** by
D — not the newest one **filed** by D. In production, filings arrive 48.7 days after their
period end on average for a 10-K and 34.4 for a 10-Q, up to 420. The FUNDAMENTAL score
readers had the same shape (`event_time`, which is the period end), and
`market_cap_estimates` (and `quant`'s mirror, `load_market_caps`) had no date filter at all:
a cycle dated 2023 could read a 2026 market cap. Replayed against production, the (reverted)
2026-06-30 selection run read an unfiled filing for **426 of 503** assets — MMM's, AOS's and
ABT's Q2 10-Qs, for example, filed 2026-07-21 to 2026-07-30.

### Root cause

The readers used the period end as the time a filing became known. That is what
`fundamental_metrics.event_time` and FUNDAMENTAL `score_snapshot.event_time` hold (what the
value is *about* — SPEC.md's definition, revisited by `T-107`), and `sec_filings.filing_date`
was never consulted outside `fundamental_agent`'s own ingestion bound.

### Theoretical/technical reference

- **Point-in-time data**: "never use a fact before its original `dateFiled`" — point-in-time
  data is "tagged with the date it actually became public, not just the date it describes"
  (StockFit, *Point-in-Time Data: Essential for Backtesting*,
  https://developer.stockfit.io/blog/point-in-time-data-backtesting, verified 2026-09-25).
- The repo's own contract, SPEC.md FR-012 (a run as of D uses nothing dated after D), whose
  acceptance now names the reads as well as the writes.

### Fix

Every fundamental reader keys on `sec_filings.filing_date <= cycle_date`:

- `cycle.data.latest_metrics` and `data_quality` pick **one** filing per asset (newest period
  end among the filed ones, ties by filing date then id) — the same filing for both.
- `cycle.data.latest_fundamental_rows` (new) picks each asset's newest FUNDAMENTAL snapshot
  through its own `filing_id`; `last_fundamental_dates`, `latest_fundamental_score` and the
  orchestrator (which had two private copies of the old query) read it.
- `cycle.data.market_cap_estimates(conn, cycle_date, ...)` and
  `quant.db.load_market_caps(..., as_of=)` filter the same way.
- A filing with no `filing_date` cannot be shown to be public and is never read (production
  has none; the gateway can omit it).

### Design decisions

- **Availability from the filing, not a new column.** Adding `available_at` to scores and
  metrics is `T-107`'s decision; every row already links to its filing, which carries the
  date, so this fix needs no schema change and holds under either choice there.
- **Undated means unknown.** Treating a NULL `filing_date` as public would reopen the leak for
  exactly the rows whose provenance is weakest.
- **FUNDAMENTAL normalization by id.** The normalize step updated every FUNDAMENTAL row of an
  asset that shared the raw score — older snapshots included. It now updates the snapshot it
  read, by id.

### Verification

`tests/test_point_in_time_readers.py` (9 tests, each reader's filter mutation-checked): a
filing whose period ended but is not yet filed is unreachable by every reader, readable on
its filing date; a late filer (421 days) keeps its prior filing; a day-by-day sweep over 18
months finds no value from a filing filed after the day; undated filings and filing-less
scores are skipped; a whole selection cycle dated before the seed's filing date normalizes
no FUNDAMENTAL score and cannot fire LEVERAGE_EXTREME on the unfiled leverage, while the day
after it does both. Production, read-only: the live 2026-09-22 cycle's reads are unchanged
(0/503 assets change filing, 0/20 scores, 0/20 market caps) — everything it read had been
filed; only historical or backdated cycles are affected.

### Residual scope, deliberately deferred

- `EARNINGS_MISSING` never fires for an asset with no FUNDAMENTAL score at all (it iterates
  only the scored assets) — found while writing these tests; a ranking change, so its own
  task (`T-119`).
- `SEMANTIC` scores are written by the integration repo; their `event_time` semantics are that
  repo's contract and are read unchanged.
- **Same-day use.** `filing_date <= cycle_date` treats a filing as usable on its own filing
  date, but EDGAR dates an after-close submission with that same day. The settled convention
  (PR #78 review) is use from the trading day after; it lands with `T-107`'s `available_at`,
  and these readers move to it then. **Done 2026-09-26** (`T-107` below).
- **Legacy shared-accession rows.** 41 pre-`T-091` accessions (13 tickers) each stand for
  several quarterly rows stamped with the Q3 10-Q's filing date. Here that errs late (a Q1
  row reads as public at Q3's date), never early; `T-120` re-ingests them.

## T-107 — fundamentals had no publication timestamp; same-day use of a filing

**Status**: Fixed 2026-09-26 (branch `fix/t107-available-at`, `T-107`; decided option (b) in
the PR #78 review).

### Symptom

All 377 FUNDAMENTAL `score_snapshot` rows and all 11,878 `fundamental_metrics` rows carried
only `event_time`, which is the period end. `T-106` made the as-of readers find availability
through each row's filing (`filing_date <= cycle_date`), but (1) nothing stored on the rows
themselves said when they became usable, so every new reader had to know to join the filing,
and (2) `filing_date <= D` counts a filing as usable **on** its filing date. EDGAR dates any
submission accepted by 17:30 ET with that day, and the NYSE closes at 16:00 ET, so an
after-close earnings 10-Q dated D was usable by a cycle dated D, before any session had
traded on it.

### Root cause

SPEC.md defines a FUNDAMENTAL row's `event_time` as what the value is *about*; availability
was never modelled as its own column. The `T-106` fix used the filing date directly, which is
the calendar day of the SEC's acceptance, not the first session that could act on it.

### Theoretical/technical reference

- **17 CFR 232.13(a)(1)–(2)** (Regulation S-T, *Date of filing*): "the business day on which a
  filing is received by the Commission shall be the date of filing", and "all filings
  submitted by direct transmission commencing on or before 5:30 p.m. Eastern Standard Time or
  Eastern Daylight Saving Time ... shall be deemed filed on the same business day"
  (https://www.law.cornell.edu/cfr/text/17/232.13, verified 2026-09-26).
- **NYSE hours and holidays**: core session "9:30 a.m. to 4:00 p.m. ET"; the 2026–2027 holiday
  list, and "Because the holiday falls on Saturday, January 1, 2028, no New Year's Day holiday
  is observed" (https://www.nyse.com/markets/hours-calendars, verified 2026-09-26).
- Point-in-time data, as cited under `T-106` (StockFit): a fact is usable from when it became
  public, not from the date it describes.

### Fix

- **`available_at`** on `sec_filings` (the first NYSE trading day strictly after
  `filing_date`), copied onto the filing's `fundamental_metrics` rows and FUNDAMENTAL
  `score_snapshot` rows. `event_time` keeps its meaning and the
  `UNIQUE(asset_id, score_type, event_time)` key is unchanged. `v_score_snapshot` and
  `v_sec_filing` expose the column.
- **`kg_schema.trading_calendar`**: a rule-based NYSE calendar (weekends; the regular holidays
  with their observance rules, New Year's on a Saturday not moved back; Juneteenth from 2022;
  the unscheduled closures since 2000). `available_from(filing_date)` is the only way the
  value is computed.
- **Writers**: `upsert_filing` stamps the filing; `record_metrics` and `insert_snapshot` copy
  the filing's value. The pipeline skips a filing the gateway lists with no date: it can never
  be shown to be usable.
- **Database guards** (`kg_schema.ddl.AVAILABILITY_TRIGGERS`): a dated filing must carry an
  `available_at` 1–7 days after its filing date, an undated one none; a metric or FUNDAMENTAL
  score must carry its filing's value and never NULL (so a row with no filing, or of an
  undated filing, is refused); re-dating a filing carries its rows along.
- **Backfill**: migration `m008` fills every row and puts the guards back after the older
  table rebuilds. It refuses, and rolls back, if a metric or FUNDAMENTAL score would be left
  without a value. `apply_migrations` drops the guards while rebuilds run, because SQLite
  refuses a rebuild's `RENAME` while a trigger names a table that is momentarily gone.
- **Readers**: `cycle.data.latest_metrics`, `data_quality`, `latest_fundamental_rows`,
  `market_cap_estimates`, `quant.db.load_market_caps` and the `coverage` fundamental/metric
  checks all filter `available_at <= D`. `cycle` and `quant` first call
  `kg_schema.availability.require`, which refuses to run on a database with rows the backfill
  has not reached, rather than read none of them.

### Design decisions

- **Option (b), keep `event_time`** (PR #78 review): option (a), re-keying `event_time` to the
  filing date, would put the filing date into the unique key. The reviewer's full-universe
  database has 46 (asset, filing date) pairs with more than one FUNDAMENTAL score, which would
  collide and be dropped silently.
- **On the filing as well as its rows.** The decision named scores and metrics. The filing
  pickers (`latest_metrics`, `data_quality`) choose a *filing*, and data-quality verdicts have
  no copy of their own, so the value lives on `sec_filings` too, and triggers keep the copies
  equal to it.
- **Refuse, don't default.** A NULL `available_at` is refused, never read as "known". An
  un-backfilled database stops `cycle` and `quant` with the command to run, rather than letting
  them read nothing.
- **A calendar in code, not a table.** No dependency needed. It is checked date by date
  against production's 1,167-session price spine (2022-01-03 to 2026-08-27: identical sets)
  and against the NYSE's published lists for 2022–2028.

### Verification

- `tests/test_available_at.py`: writers, guards, cascade, `m008` backfill and refusal, the
  `cycle`/`quant` guard, and a source scan that no SQL literal filters FUNDAMENTAL scores or
  metrics by `event_time`, and no as-of consumer compares `filing_date`/`period_end` to a date.
- `tests/test_trading_calendar.py`: the published holidays for 2022–2028, the spine's session
  count, and edge cases (weekends, Presidents' Day, the Carter closure, holidays observed on a
  Friday, the post-9/11 gap).
- `tests/test_point_in_time_readers.py` now requires use from the next session: a Friday
  filing is unreadable on Friday and over the weekend, readable Monday. A filing made the
  Friday before Presidents' Day is readable Tuesday. The 27-month sweep asserts every value
  comes from a filing filed strictly before the day and usable by then.
- 12 mutations (each reader back to `event_time`/`filing_date`, either guard removed,
  same-day availability, a holiday dropped, the cascade removed, the score backfill skipped,
  the trigger drop removed, the undated-filing skip removed): all caught.
- **Production copy** (`m008` dry run, 17 s): 5,076 filings, 11,878 metrics and 377 FUNDAMENTAL
  scores backfilled, 0 NULL, 0 copies differing from their filing. Gaps: 1 day for 3,912
  filings, 2 for 18, 3 for 1,002, 4 for 144. Every `available_at` up to the spine's end is a
  spine session (5,057 of 5,057). `quick_check` ok, FK clean. The live 2026-09-22 cycle's
  score reads are unchanged (0/20 differ; also 0 at 2026-06-30, 2025-06-30 and 2024-12-31).
- **Production, applied 2026-09-26** (`migrate`, 4 min 15 s, after a verified backup): results
  identical to the copy. Schema v8; 0 rows without `available_at`; 0 copies differing from
  their filing; `quick_check` ok, FK clean. The live cycle reads the same 20 scores and 16
  market caps.

## T-108 — the internal benchmark compounded the mean of log returns over every name

**Status**: Fixed 2026-09-26 (branch `fix/t108-benchmark-simple-mean`, `T-108`).

### Symptom

`SP500_EW_INTERNAL` (`bench-v1`), the yardstick every book's `active_return` is measured
against, compounded the cross-sectional **mean of log returns**. On production's 20-name
panel over 2022-01-04 to 2026-08-27, that formula gives 5.98 %/yr against 10.32 %/yr for a
true daily-rebalanced equal weight: −4.34 pp/yr (the audit measured −6.98 pp/yr on the full
universe). The series actually stored was staler still, built before the current return
series, and compounds at 1.24 %/yr. The index also averaged every name with a row that day,
not the gated panel the books are drawn from. `evaluate --benchmark X` for any X rebuilt
the internal index **under the name X**, so a loaded external series would have been
overwritten.

### Root cause

A portfolio's return is the weighted average of its constituents' *simple* returns. The mean
of log returns is the log of their *geometric* mean, which by Jensen's inequality is lower by
about half the cross-sectional variance each day (for +10 % and −10 %: 0 % against
−0.50 %/day). Log returns add up across *time*, not across *assets*.

### Theoretical/technical reference

- "Portfolio return is the proportion-weighted combination of the constituent assets'
  returns" (Wikipedia, *Modern portfolio theory*, "Risk and expected return",
  https://en.wikipedia.org/wiki/Modern_portfolio_theory, verified 2026-09-26). The returns
  combined there are simple returns.
- "Logarithmic returns are time-additive" (Wikipedia, *Rate of return*,
  https://en.wikipedia.org/wiki/Rate_of_return, verified 2026-09-26): additivity across
  periods, which is why the per-name series is stored as logs, and nothing more.

### Fix

- **`bench-v2`**: each day's return is `mean(expm1(tr_log_return))` over the panel names
  with a return that day (a name missing a day is left out of that day's mean, not counted
  as 0 %). `log_return = log1p(mean)`, and the level compounds `(1 + r)`.
- **The panel is the investable universe**: `quant.universe.benchmark_gate(conn, settings,
  as_of=)`, the same liquidity/history knobs `build-risk-model`'s `settings_gate` uses, but
  never a book's own hard-veto exclusion -- a veto is inside the strategy being graded, so it
  can't also shrink the yardstick it's graded against. Evaluated as of the window's start
  (`evaluate --from`, or `benchmark --from`, now required). `evaluate` records the version and
  the panel on its `quant_run` row.
- **Graded against what was built**: `evaluate` reads the benchmark version it just built
  (or, for an external series, the newest loaded version), not whatever `v_benchmark_series`
  shows. It writes `perf-v2`; `perf-v1` rows stay under their version, and
  `v_quant_benchmark_performance` shows the latest version per (book, date).
- **External series**: `quant load-benchmark --csv FILE --benchmark NAME` loads a
  total-return level series (`date,total_return_level`) as `csv-v1`, refusing unsorted,
  duplicate, non-ISO or non-positive rows. `evaluate` only reads such a series, and refuses
  when none is loaded for the window.

### Design decisions

- **Gate as of the window's start.** A book is frozen at its as-of date over the names the
  gate admitted then, so the fair comparison holds the same names. Gating each day would let
  names enter on information the book never had.
- **Missing name-days are renormalized**, the usual equal-weight index convention. How a
  *book* treats a missing asset-day is `T-111`'s question.
- **New versions, nothing overwritten**: `bench-v1` and `perf-v1` stay readable under their
  versions for comparison.

### Verification

- `tests/test_benchmark.py` (21 tests). The T-108 acceptance: the index matches an
  independent value-based equal-weight calculation (1/n stakes, rebalanced daily) to 1e-9,
  level and return, day by day. Also covered: missing-day renormalization; names outside the
  panel ignored; +10 %/−10 % stays flat where v1 reports −2.5 % in five days; empty panel
  refused; CSV loading and every malformed case; `evaluate` over the gated panel (a member
  with short history left out), under `perf-v2` and against the version it built even when
  the view prefers a stale one; external series read and never overwritten; the view
  showing one row per book and day; both commands.
- 9 mutations (log mean restored, panel ignored, missing day as 0 %, external overwritten,
  perf version not bumped, view unfiltered, CSV order unchecked, view read instead of the
  built version, benchmark panel not the books' gate): all caught.
- **Production copy** (`quant evaluate`, dry run, pre-correction): the benchmark was rebuilt
  over the 20 names gated as of 2026-06-30, still under a book's own hard-veto exclusion at
  that point. Superseded below.

### Post-merge correction: a human reviewer caught three real gaps

@eldova1702 approved (688 tests, ruff/format/mypy clean, bench-v2 independently reproduced to
1e-15) but flagged three issues before any production `quant evaluate`, all confirmed and
addressed the same day (2026-09-26):

1. **The benchmark panel was still gated by the strategy's own hard veto.** `_benchmark`
   (`quant/evaluate.py`) and the `benchmark` CLI command both called `settings_gate`, which
   applies `settings.exclude_hard_vetoed` (default `True`) -- the same veto filter a book is
   built under. That puts the veto inside the yardstick, so it can never show up as active
   return: on the 20-name production panel that dropped 3 names at T-1. **Fix**: a new
   `quant.universe.benchmark_gate` -- `settings_gate`'s knobs with `exclude_hard_vetoed`
   forced `False` -- is what `_benchmark` and the `benchmark` command build the panel from;
   `settings_gate` (still veto-aware) stays what `build-risk-model` and `persist` build a
   book from. New tests: `test_benchmark_gate_keeps_a_hard_vetoed_name_a_book_would_drop`
   (`tests/test_quant_gate.py`) and
   `test_evaluate_s_benchmark_panel_keeps_a_hard_vetoed_name_a_book_would_drop`
   (`tests/test_benchmark.py`).
2. **The dry run's "live book" was a stale snapshot.** `quant_portfolio` id 4 (as-of
   2026-06-30, `BF.B = 0.10`) is a T-104 leftover from a reverted run, not the current live
   book; the −6.16 % → −6.91 % comparison built on it has no meaning and is withdrawn above.
   That snapshot and its 41 `perf-v1`/`perf-v2` rows must be voided before any production
   `quant evaluate` -- an operational step against the production database, outside what this
   PR's code changes.
3. **Active return should be reported compounded, not summed daily.** Summing
   `active_return` across days is not the compounded gap between the book's and the
   benchmark's cumulative returns; it was the wrong statistic even before finding 2 made the
   underlying book moot. A corrected dry-run figure needs a re-run against a current
   production copy, over the real live book, with the benchmark now built by
   `benchmark_gate` -- deferred to that re-run rather than restated here without one.

Full suite after the correction: see `Verification` above for the count; the new gate/evaluate
tests are additions on top of it.

### Residual scope, deliberately deferred

- Books with a later as-of inside one `evaluate` window are graded against the panel gated
  at the window's start. The three production books dated 2026-09-22 have no forward prices
  yet (`T-110`).
- No external series is loaded yet; obtaining a cap-weighted total-return file is a
  data-acquisition step.
- A fresh production dry run (correct panel, real live book, compounded active return) is
  needed before `quant evaluate` runs against production; the stale `quant_portfolio` id 4
  snapshot and its 41 perf rows must be voided first (post-merge correction, above).

---

## T-109 — the equilibrium μ was an excess return; every downstream Sharpe subtracted `rf` twice

**Status**: Fixed 2026-09-27 (`T-109`).

### Symptom

`quant`'s risk model stores three expected-return estimators per asset (`hist_mean`,
`james_stein`, `equilibrium`), and `ret_estimator = "equilibrium"` is the default read by
every objective and reported on every book. `historical_mean`/`james_stein_mean` are annualized
means of the panel's own **total**-return series (`build_return_panel`'s returns are total,
per `returns.py`), but `persist.py::_expected_returns` called `equilibrium_returns(sigma, caps,
risk_aversion=...)` with no `rf` argument -- and `risk.equilibrium_returns`'s own signature
defaults `rf` to `0.0`. `risk.equilibrium_returns`'s docstring already states the intended
formula, `Pi = rf + lambda * Sigma @ w_mkt`; the stored value was actually just
`lambda * Sigma @ w_mkt`, an **excess** return, not the total return the other two estimators
are. Every objective (`min_var`, `tangency`, `target_vol`, `risk_parity`, `frontier`) and the
Sharpe reported on every persisted book (`optimize.py::_stats`) then computed
`sharpe = (w @ mu - rf) / vol`, expecting a total-return `mu` and subtracting `rf` exactly
once -- so for the `equilibrium` estimator specifically, `rf` was subtracted twice (once
missing from `mu` itself, once again in `_stats`), and every book's persisted
`expected_return` and `sharpe` understated by `rf` and `rf / vol` respectively whenever
`ret_estimator = "equilibrium"` (the default; every production risk model).

### Root cause

`persist.py::_expected_returns` never threaded the `rf` it had already loaded (via
`load_risk_free`, used two lines earlier for `quant_risk_model.rf_annual`) into the one
estimator whose formula needs it. `historical_mean`/`james_stein_mean` need no `rf` argument
at all -- they are plain means of an already-total-return series -- so the omission was easy
to miss: two of the three estimators were correct by construction, and `equilibrium_returns`'s
own unit tests (`tests/test_quant_lw.py`) call it directly with no `rf`, which is the correct
way to test that pure function in isolation; the bug was entirely in the one caller that
should have supplied `rf` and didn't.

### Theoretical/technical reference

- **The Sharpe ratio is (total portfolio return minus the risk-free rate) divided by
  volatility** (Sharpe, W.F., "Mutual Fund Performance," *Journal of Business*, 1966; the
  "reward-to-variability ratio") -- the numerator's minuend is the portfolio's own total
  return, and `rf` is subtracted exactly once to get the excess return the ratio is built on.
  `optimize.py::_stats`'s `sharpe = (ret - rf) / vol` already matches this construction; the
  defect was that `ret` (`mu @ w`) was silently an excess return already for the `equilibrium`
  estimator, so this formula's single subtraction became a second one.
- **Black-Litterman's equilibrium return Π is an excess return; `quant` stores the total
  return `rf + Π`.** Reverse optimization gives the market's implied excess equilibrium
  return `Pi = delta * Sigma @ w_mkt` (Black, F. and Litterman, R., "Global Portfolio
  Optimization," *Financial Analysts Journal*, 1992; He, G. and Litterman, R., "The
  Intuition Behind Black-Litterman Model Portfolios," Goldman Sachs, 1999). Because
  `hist_mean`/`james_stein` are total returns and `optimize.py::_stats` subtracts `rf`
  once, `quant` stores the equilibrium estimator as the total return
  `mu_eq = rf + delta * Sigma @ w_mkt`, the same form as CAPM's
  `E[R_i] = rf + beta_i * (E[R_m] - rf)`. The bug was that `persist.py`, the one caller of
  `risk.equilibrium_returns`, left `rf` at the function's `0.0` default, so the stored value
  was Π alone.

### Fix

`persist.py::_expected_returns` takes `rf: float` and passes it through:
`equilibrium_returns(sigma, caps, risk_aversion=settings.equilibrium_risk_aversion, rf=rf)`.
`run_build_risk_model` passes `rf=rf.annualized_rate` (the same `RiskFree` already loaded for
`quant_risk_model.rf_annual`). No change to `risk.equilibrium_returns` itself, to
`optimize.py::_stats`, or to `historical_mean`/`james_stein_mean` -- all three were already
correct; only the wiring between them was not.

### Design decisions

- **Total return, not excess, is the one convention.** `historical_mean`/`james_stein_mean`
  can't cheaply be made "excess" (that would require subtracting a daily `rf` from every
  observation before annualizing, a bigger change for no benefit), so `equilibrium` is made to
  match them rather than the other way around -- consistent with `_stats`'s existing
  `sharpe = (ret - rf) / vol`, which already assumes a total-return `mu`.
- **`equilibrium_returns` itself keeps `rf: float = 0.0`.** The pure function's contract (its
  own docstring already gives the total-return formula) doesn't change; only its one caller
  that was silently relying on the wrong default does.
- **A future estimator must follow the same convention.** `T-077`'s planned Carhart
  4-factor `ret_estimator` (`mu_i = rf + sum_k beta_i,k^shrunk * lambda_bar_k`) already writes
  `rf +` into its own formula in `TASKS.md`; this fix is what makes that consistent with what
  `_stats`/every objective already expect, rather than a second estimator needing its own
  after-the-fact correction.

### Verification

- New tests (`tests/test_quant_risk_model.py`):
  `test_equilibrium_mu_is_a_total_return_like_the_other_two_estimators` -- building the same
  risk model twice with `risk_free_rate` values 0.05 apart, `hist_mean`/`james_stein` are
  byte-identical between the two runs (they never touch `rf`) while `equilibrium` shifts by
  exactly the `rf` delta, uniformly across every asset.
  `test_book_sharpe_is_invariant_to_rf_once_mu_is_a_genuine_total_return` -- for `min_var`
  (weights independent of `mu`), the persisted book's `expected_return` shifts by exactly the
  `rf` delta between the two runs while its `sharpe` stays unchanged (`(w . mu_total - rf) /
  vol` doesn't depend on `rf` once `mu_total` is genuinely total) -- both tests confirmed to
  fail on the pre-fix code (reverted locally) with the exact signature the bug predicts:
  `expected_return` flat across the two runs instead of shifting, since the pre-fix
  `equilibrium` mu never included `rf` at all.
- `uv run pytest -q` -- 692 passed (was 690; +2 new tests).
- `uv run ruff check` / `ruff format --check` / `uv run mypy` -- all green.
- **Independent reproduction** (PR #85 review, `@eldova1702`): on a copy of `financial-2.db`,
  692 tests pass, ruff/format/mypy clean, and both new tests independently confirmed to fail
  on the pre-fix code.
- **The fix moves real weights, not just reported stats.** Recomputed from the stored
  (pre-fix) risk model: under the old excess-return μ, 15 of the production panel's 20 names
  had `mu < rf`, so `tangency` wrongly concentrated into the other 7 (a name's excess return
  looking negative merely because `rf` was never added back in is not a real signal to
  concentrate away from). Post-fix, 66% of `tangency`'s weight moves and its Sharpe goes
  0.038 -> 0.325. `min_var`, `target_vol` and `frontier` weights are unaffected (their
  optimization doesn't depend on `mu`'s level, only `tangency`'s does). This sharpens why the
  pending production re-run (below) isn't optional bookkeeping: any `tangency` result read
  from production before that re-run is the wrong book, not just a mis-reported one.

### Residual scope, deliberately deferred

- **Every risk model built before this fix carries an excess-return `equilibrium` μ** (and a
  book's `expected_return`/`sharpe` understated accordingly whenever `ret_estimator =
  "equilibrium"`, the default). Re-persisting corrected values for the live universe requires
  re-running `quant build-risk-model`/`optimize` against production -- outside a code-review
  pass's authority to run unprompted; a follow-up operational step, like F1/F2/F4's own
  deferred production re-runs. **`tangency` in particular must not be read from production
  until that re-run happens** -- its weights, not only its reported stats, are wrong today.
- `T-077`'s Carhart estimator is not implemented by this fix -- its own formula already plans
  to add `rf`, so no separate correction is expected when it lands, but that remains to be
  verified against real code once written, not assumed.

---

## T-110 — `quant`/`cycle` accepted an as-of past the price spine; `pricing_agent` could orphan `price_observation` rows

**Status**: Fixed 2026-09-27 (`T-110`).

### Symptom

Production `quant_run`s 7-10 and both `cycle_run`s are dated 2026-09-21/22 while
`price_daily` ends 2026-08-27 -- neither package noticed or recorded that its `--analysis-date`
was three-plus weeks past the last price actually stored, and proceeded as if "as of
2026-09-22" meant something for prices that stop three weeks earlier. Separately,
`price_observation` has 503 rows dated 2026-08-28 with no corresponding `price_daily` bar
that day, and one run's 503 `price_window` "full" rows also end 2026-08-28: a
`pricing_agent run --observations` invocation without `--store-daily` fetched fresher
candles than any earlier run had persisted to `price_daily`, and wrote per-day analytics
for a bar that was never stored anywhere.

### Root cause

Two independent gaps, both a missing check rather than a wrong formula:

- **No package that takes `--analysis-date` ever compared it against what price data
  actually exists.** `build-risk-model`/`optimize` (`quant`) and `select`/`monitor` (`cycle`)
  all read prices (directly, or via `quant_return_daily`/`price_observation`) up to their
  requested as-of, but silently used whatever was available below it -- a run dated weeks
  past the price spine is indistinguishable, in its own output, from one dated the day
  prices actually stop.
- **`pricing_agent._store` builds `price_window`/`price_observation` from the day's freshly
  *fetched* candles, not from what `price_daily` already holds.** `--store-daily` and
  `--observations` are independent opt-in flags (`pipeline.py`); when `--observations` is
  passed without `--store-daily`, the observation rows reference whatever the gateway
  returned that call, regardless of whether an earlier (or no) run ever persisted a matching
  `price_daily` bar for those same days.

### Theoretical/technical reference

This is the constitution's own no-lookahead contract (AI behavior #4: "a stage that writes
something dated after its own `--analysis-date` is a bug, not an edge case") and `SPEC.md`
FR-012's as-of guarantee, applied to a case neither had an explicit check for: *reading*
stale data under a fresh-looking as-of is the same "what did we believe as of D" guarantee
broken from the other side -- D itself was never validated against what the run could
actually see. `price_observation`'s own contract (`docs/pricing_agent.md`: "derived **per-day**
price analytics") is a referential one -- a per-day analytic that outlives the day's own
stored price is data pretending to rest on a foundation that isn't there.

### Fix

- **`kg_schema.queries`**: `last_price_date(conn) -> str | None` (`MAX(date) FROM
  price_daily`, tolerant of a database with no such table) and
  `stale_as_of_reason(conn, as_of) -> str | None` (`None` when `as_of <= last_price_date`, or
  when there is no stored price at all -- a different problem, caught elsewhere). Shared by
  both packages rather than duplicated.
- **`quant`**: `run_build_risk_model` checks `stale_as_of_reason` right after opening its
  `quant_run` row (so a refusal is still recorded, `"failed"`, with
  `stale_as_of_bypassed` in `params_json`) and raises `kg_schema.queries.StaleAsOf` unless
  `QuantSettings.allow_stale_prices` is set; `optimize` inherits this whenever it auto-builds
  a model (reusing an already-stored one performs no fresh price read, so nothing new to
  flag there). `--allow-stale-prices` on `build-risk-model`/`optimize`; the bypass reason
  is surfaced as a CLI `WARNING` and on `RiskModelResult`/`OptimizeRunResult`.
- **`cycle`**: `_run` checks `stale_as_of_reason` right after `check_manifest` (before
  `open_cycle`, the same pre-flight shape) and raises `StaleAsOf` unless
  `CycleSettings.allow_stale_prices` is set; both `select` and `monitor` are guarded (both
  read prices for TECHNICAL/veto), plus `backfill`. `--allow-stale-prices` on all three;
  the bypass reason lands in `cycle_run.params_json` and on `CycleReport.stale_price_bypassed`
  for the CLI's `WARNING`.
- **`pricing_agent`**: `--observations` now requires `--store-daily`, refused at the CLI
  (`parser.error`) and again in `pipeline.run` itself (a plain `ValueError`, so a
  programmatic caller is protected too, not just the CLI). `price_daily` and
  `price_observation` are now always written from the identical fetched `candles` in the
  same call, so they can never disagree on which days exist.

### Design decisions

- **`price_daily` is the one spine, for both packages.** `cycle` actually reads
  `price_observation`, not `price_daily`, for TECHNICAL scoring -- but after this fix the two
  are always populated together (`--observations` requires `--store-daily`), and `price_daily`
  is what the task's own evidence and `pricing_agent.md` call "the price spine," so both
  guards check the same table rather than inventing a second, `price_observation`-based
  notion of freshness.
- **Refuse, don't merely warn, by default.** `--allow-stale-prices` follows T-097's/T-086's
  own precedent (`--allow-backdated`, `--allow-no-dividends`): a silent warning is easy to
  miss in a batch run's output; a refusal forces a conscious choice, and the override still
  records why for the audit trail.
- **`price_window` itself is not gated by `price_daily`.** Unlike `price_observation`,
  `price_window` is documented as pricing_agent's base product, usable standalone (no
  `--store-daily` needed) -- gating it the same way would break that documented, legitimate
  use and wasn't what the symptom's own acceptance criterion ("zero orphan **observation**
  dates") asked for.
- **`optimize` checks staleness itself, independently of whether it reuses or builds the
  risk model.** The first cut of this fix only checked inside `_resolve_model_id`'s build
  path, on the reasoning that a reused model's price read had already happened (or not) when
  that model was originally built. Review on PR #87 (`@eldova1702`) found the gap: reusing a
  risk model built earlier with `--allow-stale-prices` let a later `optimize` at the same
  stale `as_of` -- without the flag -- rewrite the books silently, with `stale_prices_bypassed`
  `None` and no `stale_as_of_bypassed` key in that `optimize` run's own `params_json`. That
  fails T-110's own acceptance criterion (a run past the price cutoff is recorded as such) for
  every `optimize` invocation that happens to land on an already-built stale model, and it
  would just as easily silently rebuild `tangency` from a pre-T-109 `equilibrium` mu if an old
  model were reused. `run_optimize` now computes `stale_as_of_reason(conn, as_of)` and applies
  the same refuse-unless-`allow_stale_prices` gate before calling `_resolve_model_id` at all,
  and records the reason on its own `quant_run.params_json` and `OptimizeRunResult` every time
  -- `_resolve_model_id` no longer reports a bypass reason of its own, since the check it used
  to perform only on the build path is now `run_optimize`'s, unconditionally.

### Verification

- New tests: `tests/test_kg_schema.py` (+2, the shared helper: no rows/no table -> `None`;
  a stale vs. safe `as_of` against a seeded `price_daily` row). `tests/test_quant_risk_model.py`
  (+6: refuses a stale `as_of` with a `quant_run` row recorded `"failed"`; an `as_of` at or
  before the spine is unaffected; the override records the reason on both the result and
  `params_json`; `optimize` refuses when it must auto-build; `optimize` propagates the bypass
  reason when it does; `optimize` still refuses when it reuses a risk model that was itself
  only built via `--allow-stale-prices`, the PR #87 review finding above). `tests/test_cycle.py`
  (+4: `select` and `monitor` both refuse a stale cycle date with no `cycle_run` row written on
  refusal; an `as_of` at the spine is safe; the override records the reason on the report).
  `tests/test_pricing_pipeline.py` (+3: `--observations` alone is refused at both the pipeline
  and CLI layers; together with `--store-daily`, every `price_observation` date has a matching
  `price_daily` date).
- `uv run pytest -q` -- 707 passed (was 692; +15 new tests).
- `uv run ruff check` / `ruff format --check` / `uv run mypy` / `pre-commit` -- all green.

### Residual scope, deliberately deferred

- **Cleaning the production orphan rows themselves** (the 503 orphan `price_observation`
  rows at 2026-08-28, and whatever of `quant_run`s 7-10 / the two `cycle_run`s should be
  re-run or annotated now that the guard exists) is a production database action, not a code
  change -- tracked as `T-123` (added alongside this fix), gated on the user's explicit
  direction like every other production write in this file (`T-104`, `T-107`, `T-120`,
  `T-121`/`T-122`).
- **`evaluate`/`benchmark` are not guarded by this fix.** Both already degrade gracefully
  under missing forward data (FR-010: "a date lacking forward data is skipped, not
  fabricated"; `benchmark`'s own empty-gate refusal), which is a different, already-handled
  failure mode from `build-risk-model`/`optimize` silently *building* a model that looks
  fresh but isn't. Flagged, not built: if a future finding shows either command needs the
  same explicit `StaleAsOf` guard, it is a small, separate addition (`stale_as_of_reason` is
  already shared and ready to call).
- **`cycle backfill` behaves like `select`/`monitor`, not specially.** Its own loop can now
  raise `StaleAsOf` mid-range if the `--to` date runs past the price spine; `--allow-stale-prices`
  is offered on it too (unlike `T-097`'s `--allow-backdated`, which `backfill` deliberately
  does not carry), since a historical backfill legitimately may need to run past the
  spine's current edge.

## T-111 — a missing asset-day in `evaluate` counted as a 0% return, dragging the book toward zero

**Status**: Fixed 2026-09-27 (`T-111`).

### Symptom

`_evaluate_book`'s per-day realized return was `sum(w * fwd[d].get(a, 0.0) for a, w in
weights.items())`: a held name with no forward-return row for day `d` (a data gap -- a missed
fetch, a pricing-source outage, a not-yet-arrived candle -- not a permanent delisting, which
would instead leave the name out of every `fwd[d]` from that point on) contributed `w * 0.0`
to the sum while its weight `w` was still spent, exactly as if that name had earned a genuine
0% that day. The more of the book's weight fell on names missing that day, the further the
reported return was dragged toward zero relative to what the names actually still priced that
day, in fact, earned.

### Root cause

`weights` (the book's frozen, as-of positions) and `fwd[d]` (the names with an actual forward
return recorded for day `d`) are two different sets whenever any name has a data gap on `d`;
`.get(a, 0.0)` conflated "no return recorded" with "recorded a 0% return" without accounting
for the missing name at all. This is the single-asset, single-day analogue of `FR-010`'s
already-handled *whole-date* case (`_evaluate_book`'s own `for d in sorted(fwd)` loop already
skips a date with no `fwd[d]` entry at all, matching FR-010's "a date lacking forward data is
skipped, not fabricated") -- the missing case was a date that *is* in `fwd` but where not every
held name has a row in `fwd[d]`.

### Initial fix was itself wrong (PR #89 review, `@eldova1702`)

The first attempt renormalized *every* missing asset-day: drop the missing name from that
day's weights and divide the rest by their own sum. Review on PR #89 found this double counts
a genuine one-day price-data gap. `quant_return_daily` only ever has a missing asset-day
because `price_daily` is missing that day's bar for that asset; `returns.build_total_return_series`
computes the *next* available day's return from the last available close (`prev_c =
closes[i - 1][1]`), so that next return already contains the gap day's full price move,
compounded in. Renormalizing the gap day imputes the other names' average return for the
missing name on the gap day itself, and then the gap-spanning return the following day counts
that same name's real move a second time. Reproduced against the test fixtures: deleting the
largest holding's (31%) price bar for one day and rebuilding returns gave a 24-day cumulative
book return of 0.6316% with no gap at all (true), 0.6353% under the original, unfixed bug
(close -- the bug's dilution happens to be small here), and 0.7851% under the renormalize-every-
missing-day "fix" (the double count, materially wrong). A held name that is missing forever
after some day -- delisted, or its return series just ends -- is a genuinely different case
with no such following bridge return to double count against, and renormalizing away *that*
name's weight, from the day its data ends onward, is correct.

### Fix

```python
last_seen: dict[int, str] = {}
for d, day_map in fwd.items():
    for a in day_map:
        if a not in last_seen or d > last_seen[a]:
            last_seen[a] = d
...
for d in sorted(fwd):
    survivors = {a: w for a, w in weights.items() if last_seen.get(a, "") >= d}
    total_survivors = sum(survivors.values())
    realized = (
        sum(w * fwd[d].get(a, 0.0) for a, w in survivors.items()) / total_survivors
        if total_survivors > 0
        else 0.0
    )
```

Two cases, distinguished by whether a name has *any later* return in the window:

- **Alive but gapped today** (`last_seen[a] >= d`, but `a` missing from `fwd[d]`): the name
  stays a "survivor" -- its weight counts in `total_survivors` -- but contributes `0.0` for
  `d` specifically (via `.get(a, 0.0)`), because its real move for `d` is already folded into
  whatever day it next reappears. This is exactly the original, pre-T-111 formula, just scoped
  to survivors instead of the book's full (possibly already-shrunk) weights.
- **Genuinely gone as of today** (`last_seen.get(a, "") < d`, including a name that never
  appears in `fwd` at all, whose `last_seen.get(a, "")` is `""`, less than every real date):
  dropped from both the numerator and `total_survivors`, from `d` on -- its capital is
  reinvested across whatever survives, not left as dead weight dragging the book toward zero
  forever.

`build_internal_benchmark` (`benchmark.py`) gets the identical `last_seen`/survivors treatment,
so the book and its benchmark share one convention; the review's own estimate is this changes
the benchmark by under 0.001% on production data (two internal one-day gaps in 2022-2026,
FISV and MNST) -- it is a consistency fix there, not a live discrepancy.

### Design decisions

- **`last_seen` is computed once per book/panel, not re-derived per day.** It only depends on
  which dates a name has *any* row for in the whole window, which doesn't change as the day
  loop advances; computing it once up front, then indexing by `d` inside the loop, avoids
  re-scanning `fwd` on every iteration.
- **This is `evaluate`'s (and the benchmark's) own concern, not `optimize`'s.** The book's
  stored weights are left untouched -- they still describe what was actually bought at
  `as_of`; only the forward *scoring* of a gap or an ended series is adjusted, so re-evaluating
  the same book after a data gap is backfilled reproduces the same numbers (nothing about the
  persisted book or the backfilled gap day's contribution to any *other* day changes).
- **Transaction costs are out of scope here (`T-077`).** Reinvesting a permanently-gone name's
  capital across the survivors is a scoring convention for what "the book's return" means once
  a name is gone, not a rebalancing event; it charges no cost, unlike a genuine turnover.

### Verification

- New tests: `tests/test_quant_pipeline.py::test_evaluate_matches_the_no_gap_result_across_a_one_day_price_data_gap`
  (two identically-seeded DBs, one with a single `price_daily` bar removed and
  `quant_return_daily` rebuilt around it; their evaluated cumulative returns over the same
  window must agree within `1e-4` -- fails against the renormalize-every-day "fix" (obtained
  `0.00897` vs. expected `0.00609`) and passes against the corrected one) and
  `tests/test_quant_pipeline.py::test_evaluate_renormalizes_from_a_names_permanent_end_of_data`
  (ends one name's return series mid-window and asserts the day after is the survivors'
  renormalized return -- fails against the original, pre-T-111 code, which never renormalizes
  at all). `tests/test_benchmark.py` gets the matching pair for `build_internal_benchmark`:
  `test_a_name_missing_one_day_but_alive_later_counts_as_zero_that_day` (renamed and its
  expectation corrected from the old "excluded, panel of 2" behavior to "alive, panel of 3,
  contributes 0%") and `test_a_name_with_no_later_return_is_dropped_and_the_panel_renormalized`.
- `uv run pytest -q` -- 710 passed (was 707; +3 net new tests: the two replaced the one wrong
  `evaluate.py` test, plus one new `benchmark.py` test).
- `uv run ruff check` / `ruff format --check` / `uv run mypy` / `pre-commit` -- all green.
- `SPEC.md` FR-010 and `docs/quant.md` updated to state both cases.

## T-112 — a collapsed efficient frontier returned `k` identical copies of the min-variance point, all labelled `optimal`

**Status**: Fixed 2026-09-27 (`T-112`).

### Symptom

`optimize.efficient_frontier`'s `k`-point sweep needs a feasible return range above the
min-variance portfolio's own return to sweep over; when `mu` carries no cross-sectional signal
under the constraints (the common case for `james_stein` over ~5y of daily data, per
`docs/quant.md`'s "why the frontier can collapse"), that range collapses (`r_max` is `None` or
`<= r_min`). The code handled this by returning `k` copies of the min-variance point, every one
of them stamped `status = "optimal"` -- indistinguishable, to any caller reading
`quant_frontier_point`, from `k` genuinely distinct optimizer solves that happened to converge
to the same point.

### Root cause

`"optimal"` is the solver's own convergence verdict (`prob.status`, `_OK = ("optimal",
"optimal_inaccurate")`) for a problem it actually solved. The collapse path never calls the
solver `k` times at all -- it short-circuits before the `target_return_portfolio` loop -- so
labelling its output `"optimal"` overstates what happened: a reader has no way to tell a real,
sharp frontier of `k` distinct optimal solves from `k` copies of one point, printed because
there was nothing to sweep.

### Fix

```python
if r_max is None or r_max <= r_min + 1e-9:
    return [FrontierPoint(0, r_min, r_min, lo.expected_vol, lo.sharpe, "degenerate", lo.weights)]
```

One point, not `k`, carrying the min-variance portfolio's own values and a `status` that says
plainly what happened: the frontier is degenerate, not that a sweep ran and every point tied.
`insert_frontier_points` (`db.py`) already deletes any previously-stored `quant_frontier_point`
row with `k >=` the new point count before inserting (`DELETE ... WHERE model_id = ? AND k >=
?`), so a book that later re-optimizes into a *non*-degenerate frontier is not left with stale
higher-`k` rows from an earlier degenerate run -- no schema or persistence change needed.

### Design decisions

- **`"degenerate"` is a plain, additive status value, not a new column.** Every existing
  reader that filters `status in ("optimal", "optimal_inaccurate")` -- `evaluate`'s and the
  API's read paths, and this repo's own tests -- already treats any other status as "not a
  point to trust for shape/Sharpe comparisons," so a caller filtering for real frontier points
  now correctly gets zero of them on a degenerate run rather than `k` decoys, with no other
  code change required.
- **One point, not zero.** The min-variance portfolio is still a real, usable result (it is
  `min_var`'s own headline book); returning it once, clearly labelled, keeps a caller that
  wants "the frontier's one representative point regardless of shape" able to read it off
  `quant_frontier_point` without a special no-rows case to handle.

### Verification

- Updated test: `tests/test_quant_optimize.py::test_frontier_collapses_on_flat_mu_but_spans_on_dispersed_mu`
  now asserts `len(flat_pts) == 1` and `flat_pts[0].status == "degenerate"` on a flat `mu`
  (confirmed to fail against the pre-fix code: `6 == 1` false, and every one of the 6 stamped
  `"optimal"`).
- `uv run pytest -q` -- 710 passed (test count unchanged: the existing test was strengthened,
  not added to); `test_optimize_persists_one_book_per_objective`'s non-degenerate,
  dispersed-`mu` frontier (`res.frontier_points == 5`) is unaffected.
- `uv run ruff check` / `ruff format --check` / `uv run mypy` / `pre-commit` -- all green.
- `docs/quant.md` updated (the `frontier` objective's own row and "why the frontier can
  collapse" section) to describe the one-point, `degenerate`-labelled result.

## T-113 — a rule-based fallback score was stored under the LLM's own name; no seed, no prompt hash

**Status**: Fixed 2026-09-27 (`T-113`).

### Symptom

`fundamental_agent`'s synthesis step (`agents.py::_synthesize`) asks the LLM for a JSON
verdict and, if the reply never parses (or the call itself errors), falls back to a
deterministic, rule-based score (`_fallback_assessment`) instead of leaving the row unwritten
(FR-002). Both paths wrote `SnapshotRow.model = engine.analyst.model_name` -- the *configured*
model's own id -- so a fallback row was stored as if the model had produced it: 1 of
production's 377 FUNDAMENTAL scores is a fallback labelled `deepseek-chat`, indistinguishable
from the other 376 without independently re-deriving the score from its inputs. Separately,
`build_model` called the LLM at `temperature = 0.2` with no seed (an unexplained source of
run-to-run drift for a score meant to be reproducible given the same filing), and no score
recorded which prompt/interaction actually produced it.

### Root cause

`_synthesize` returned only a `FundamentalAssessment` -- the verdict itself -- with no signal
of *which path* produced it, so its one caller (`FundamentalAnalyst.analyze`) had nothing to
pass through to `pipeline.py`'s `SnapshotRow` construction except the model it *would have*
asked, whether or not that model's own reply is what ended up stored.

### Fix

- `build_model` (`agents.py`): `params={"temperature": 0, "seed": 0, "max_tokens": 1500}` (was
  `{"temperature": 0.2, "max_tokens": 1500}`). `seed` is a standard OpenAI-schema field,
  forwarded verbatim by Strands' `OpenAIModel` into the underlying client call rather than
  validated against a strict schema, so passing it is safe regardless of whether the
  configured endpoint's backend actually honours it.
- `_synthesize` now returns a small `_Synthesis(assessment, used_fallback, prompt_hash)`
  instead of a bare `FundamentalAssessment`; `used_fallback` is `True` only on the
  `_fallback_assessment` return path. `prompt_hash` is `sha256(json.dumps(orchestrator.messages,
  sort_keys=True))` -- the *whole* per-filing message history (system prompt, every specialist
  tool round-trip, the synthesis/repair attempt), not a hash of the fixed `_SYNTHESIS_PROMPT`
  template text alone, which would be near-constant across every filing and worthless as
  provenance.
- `AnalysisResult` carries both fields through to `pipeline.py::_analyze_one`, which sets
  `SnapshotRow.model = FALLBACK_MODEL_LABEL` ("`rule-based-fallback-v1`", never a real model's
  own id) when `used_fallback`, else `engine.analyst.model_name` as before; `SnapshotRow` and
  `insert_snapshot` gained `prompt_hash`, written on every row, fallback or not.
  `db.bump_run_counter(..., "fallback_units")` fires once per fallback row.
- New columns, both nullable/additive (`kg_schema.ddl.REQUIRED_COLUMNS`, safe against the
  shared production DB per constitution AI behavior #12/FR-011): `score_snapshot.prompt_hash`
  (`TEXT`) and `analysis_run.fallback_units` (`INTEGER NOT NULL DEFAULT 0`).

### Design decisions

- **Reuse the existing `model` column for the fallback label, rather than a new boolean
  column.** `model` already means "what produced this score"; a distinct, unmistakable
  sentinel (`FALLBACK_MODEL_LABEL`) answers that question directly and keeps every existing
  reader that groups/filters by `model` correct with no further change -- a `model = 'deepseek-
  chat'` filter now genuinely means "the configured model's own output," not "the configured
  model, or maybe the fallback that ran instead of it."
- **Hash the full message history, not the prompt template.** `_SYNTHESIS_PROMPT`/
  `_REPAIR_PROMPT` are fixed strings shared by every filing; hashing them alone would produce
  the same 1-2 hash values for literally every score ever written -- technically "a prompt
  hash" but useless for the provenance the task asks for. Hashing `orchestrator.messages`
  instead captures what was actually specific to this filing: the computed metrics brief, each
  specialist's numbers and reply, and the final instruction -- so two filings' hashes actually
  differ, and a re-run against unchanged inputs reproduces the same hash.
- **`seed` is opportunistic, not verified live.** This environment's network egress policy
  blocks reaching the configured LLM endpoint directly from a test/session; `seed` is a
  standard, well-known OpenAI chat-completions field the openai-python client (which Strands'
  `OpenAIModel` wraps) accepts without validating it against a fixed allowed-keys schema, so
  including it is safe (no client-side error) whether or not DeepSeek's backend actually uses
  it. `temperature = 0` alone already removes the larger, unconditional source of drift.

### Verification

- New test: `tests/test_pipeline.py::test_run_labels_a_fallback_score_and_records_it_on_the_run`
  -- a stub analyst that always falls back; asserts every written `score_snapshot.model ==
  FALLBACK_MODEL_LABEL`, every row's `prompt_hash` is the stubbed hash, and
  `analysis_run.fallback_units` equals the number of filings processed.
- `uv run pytest -q` -- 711 passed (was 710; +1 new test).
- `uv run ruff check` / `ruff format --check` / `uv run mypy` / `pre-commit` -- all green.
- `SPEC.md` FR-002 and the `score_snapshot` table row updated; `docs/fundamental_agent.md`
  updated (the `agents.py` section and `insert_snapshot`'s row).

### Residual scope, deliberately deferred

- **The one existing production fallback row (and any others already written) keeps its old
  `model = 'deepseek-chat'` label and a `NULL prompt_hash`.** This fix only changes what a
  *future* run writes; relabeling or backfilling historical rows is a production data
  correction, not a code change, and is not in this fix's scope.
