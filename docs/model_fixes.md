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
