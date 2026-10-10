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

### Correction (T-117, 2026-09-28): the root cause above is wrong

**The "filer-side XBRL tagging defect" diagnosis above does not hold.** Re-verified directly
against SEC's own `companyfacts`/`companyconcept` APIs (`https://data.sec.gov`, live, read-only,
2026-09-28), independent of our own gateway: APA (CIK `0001841666`) has **never filed a
`us-gaap:Revenues` fact at all**, in any context, dimensional or not, in its entire XBRL filing
history — `companyconcept/CIK0001841666/us-gaap/Revenues.json` returns `404 NoSuchKey`. Its only
two ever-used revenue-named `us-gaap` concepts are `BusinessAcquisitionsProFormaRevenue` and
`RevenueFromContractWithCustomerIncludingAssessedTax` (`companyfacts`, 360 total `us-gaap`
concepts scanned). The "two rows, one dimensional, one not, both $1,082,000,000" shape this
entry originally described is therefore not something APA's real filing contains — it does not
exist upstream, in either form. Further, APA's actual rendered FY2023 10-K income statement
(`R3.htm`, `sec.gov/Archives/edgar/data/1841666/000178403124000003/`, "STATEMENT OF CONSOLIDATED
OPERATIONS") has **no "Total revenues" line at all**: it opens directly with the four
adjustment lines (derivative gains, divestiture gains, property-sale losses, other, net) and
"Total revenues and other" — confirming `apa_RevenuesAndOther` is the *only* revenue subtotal
APA's real statement presents.

The value our own stored `financial_facts` carries under `concept = 'us-gaap_Revenues'` for APA
is therefore not sourced from any real `us-gaap:Revenues` tag APA ever filed — it is an artifact
introduced somewhere in the gateway's own processing (`sec_edgar`, upstream in
`portfolio-data-mining`), most plausibly a revenue-disaggregation footnote's own internal
subtotal (ASC 606 disclosures commonly tag a "Total" row with the generic `us-gaap:Revenues`
concept, valid *within that footnote's own dimensional context*) read out of that context and
presented as if it were the primary statement's consolidated total. This is the same failure
mode T-117 (`docs/model_fixes.md`) fixes for FY2023-2025's *too-large* case (a breakdown value
mislabeled as the total) — this entry's FY2021 *too-small* case is now understood as the other
side of the identical upstream defect, not a separate filer-side tagging mistake. `T-118`
tracks the upstream fix; see `T-117`'s own entry for this repo's local guard, which now handles
both directions structurally rather than relying on this entry's original, incorrect
"duplicate-tagging" explanation. The **fix and its verification above are unaffected** — the
plausibility floor correctly rejects the implausible value regardless of *why* it is wrong.

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
  **Update (T-122, 2026-09-29):** the void happened; a first re-run attempt still couldn't
  complete the live-book comparison (`price_daily`'s spine then capped three weeks before the
  live book's own `2026-09-22` start -- a pricing-data gap, not a code defect). After the user
  raised the pricing gateway and a fresh `pricing_agent run --store-daily --observations`
  extended `price_daily` through `2026-09-29`, a second, properly-scoped re-run (20-ticker
  sample universe, `--universe-db universe_sample20.db`) completed it in full: `quant evaluate
  --from 2026-09-22 --analysis-date 2026-09-29 --benchmark SP500_EW_INTERNAL` snapshotted the
  real live book (`quant_portfolio` id 10, 10 positions, matching it exactly) and evaluated it
  against the rebuilt `SP500_EW_INTERNAL` benchmark over 5 real forward trading days
  (`2026-09-23`..`2026-09-29`): `cumulative_return -0.88%` against the benchmark, daily
  `active_return` ranging `-0.46%` to `+0.91%` (`quant_benchmark_performance`, `perf-v2`,
  `portfolio_id = 10`). This is the fix's own intended output -- a genuine live-book-vs-panel
  comparison, not a stale snapshot's meaningless figures -- confirmed on real production data.
  See `T-122` (`TASKS.md`) for the full record and `T-109`'s own correction below for the
  `optimize`-side numbers.

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

### Correction (T-122 production re-run, 2026-09-29)

The deferred production re-run above happened, at the user's direction, after `T-121` voided
the stale live-book snapshot. `price_daily`'s own spine caps at `2026-08-27` (T-110's own
finding), so `build-risk-model`/`optimize` ran at `--analysis-date 2026-08-27` (the newest
non-stale date) rather than today, over the 17-name panel `quant_return_daily`'s `qret-v2`
series currently covers (a development-scope universe pending `T-100`'s full-universe cutover,
unrelated to this fix). Real, non-dry-run numbers: **tangency Sharpe `0.038 -> 0.325`**
(stale book id 2's `0.03789` -> fresh book id 5's `0.32490`), `expected_return` `0.0528 ->
0.0890`, `expected_vol` `0.2056 -> 0.1355` -- matching this entry's own dry-run prediction
above almost exactly. `min_var` Sharpe `-0.089 -> 0.288`, `target_vol` `0.0068 -> 0.319`.
**The live-book-vs-benchmark forward comparison (`evaluate`, T-108's own territory) could not
complete this first pass**: the live book's only stints ever opened are `valid_from =
2026-09-22`, three weeks *after* the price spine's own end, so there was no date with both a
live book and forward price data available at once. A pricing-data gap, not a code defect --
this fix's own correctness was confirmed either way, since the `tangency` book's weights and
reported stats already demonstrably matched the reviewer's prediction on real production data.

**Completed (second pass, 2026-09-29):** once the user raised the pricing gateway and a fresh
`pricing_agent run` extended `price_daily` through `2026-09-29`, a properly-scoped re-run
(20-ticker sample universe, `--universe-db universe_sample20.db`) produced `build-risk-model`
model id 3 / `optimize` books ids 7-9, confirming the same fixed numbers again (tangency Sharpe
`0.3253`, day-to-day drift from the first pass's `0.3249`, both real) and, this time,
`quant evaluate --from 2026-09-22 --analysis-date 2026-09-29 --benchmark SP500_EW_INTERNAL`
snapshotted the actual live book (id 10, 10 positions) and evaluated it against the
T-108-fixed `SP500_EW_INTERNAL` benchmark over 5 real forward trading days: `cumulative_return
-0.88%`, daily `active_return` `-0.46%` to `+0.91%`. See `T-122` (`TASKS.md`) for the full
record.

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
  `T-121`/`T-122`). **Done 2026-09-29** (`T-123`, `TASKS.md`): the orphan rows turned out
  already zero, a side effect of `T-122`'s own `--store-daily --observations` pricing refresh;
  `quant_run`s 8/9 and `cycle_run` 1 (genuinely stale-as-of) got a retroactive
  `stale_as_of_bypassed` annotation, rather than being re-run. **Precision (PR #101 review):**
  this matches `quant_run`'s own going-forward behavior exactly (`open_run` there precedes the
  `StaleAsOf` check, so every `quant_run` row already carries this key either way) but is a
  one-off retroactive write for `cycle_run` specifically -- `cycle`'s check precedes
  `open_cycle`, so a normal refused stale `select` never creates a row there at all; the guard
  itself would never have "recorded" anything for a run it refused outright. Re-running `cycle
  select` was deliberately avoided regardless, since it would reopen/close live positions with
  real portfolio consequences well beyond a metadata cleanup. `quant_run`s 7/10 and `cycle_run`
  2 were assessed as not subject to this guard by design or already handled for an unrelated
  reason (see `T-123`'s own record in `TASKS.md` for the per-run reasoning).
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
run-to-run variance), and no score recorded which *prompt version* actually produced it -- the
audit plan's own wording, and what `T-079` needs to separate scores written before/after the
`T-073`/`T-074`/`T-076` prompt edits.

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
- `_synthesize` now returns a small `_Synthesis(assessment, used_fallback)` instead of a bare
  `FundamentalAssessment`; `used_fallback` is `True` only on the `_fallback_assessment` return
  path.
- New `_prompt_version_hash(model_name)`: `sha256` of a canonical JSON payload of
  `MASTER_PROMPT`, every specialist's system prompt (`_specialist_system_prompt(g)` for `g` in
  `_SPECIALIST_GROUPS` -- 5 of 9 read a `skills/<name>/SKILL.md` file, so an SOP edit is
  captured too), `_SYNTHESIS_PROMPT`, `_REPAIR_PROMPT`, and the model config
  (`model_name`/`temperature`/`seed`/`max_tokens`). `FundamentalAnalyst.__init__` computes this
  **once**, as `self.prompt_hash`, and `analyze()` stamps that same value on every
  `AnalysisResult` it returns -- one hash per analyst instance (one per run), not one per
  filing.
- `pipeline.py::_analyze_one` sets `SnapshotRow.model = FALLBACK_MODEL_LABEL`
  ("`rule-based-fallback-v1`", never a real model's own id) when `result.used_fallback`, else
  `engine.analyst.model_name` as before; `SnapshotRow.prompt_hash = result.prompt_hash`,
  written on every row, fallback or not. `db.bump_run_counter(..., "fallback_units")` fires
  once per fallback row.
- New columns, both nullable/additive (`kg_schema.ddl.REQUIRED_COLUMNS`, safe against the
  shared production DB per constitution AI behavior #12/FR-011): `score_snapshot.prompt_hash`
  (`TEXT`) and `analysis_run.fallback_units` (`INTEGER NOT NULL DEFAULT 0`).

### First cut was itself wrong (PR #91 review, `@eldova1702`)

The first attempt computed `prompt_hash` **per filing**, as a sha256 of the orchestrator's
full message history (system prompt, every specialist round-trip, the synthesis/repair
attempt) at the point the reply was accepted or every attempt had failed. Review found this
answers the wrong question: the transcript is never itself stored, so the hash can never be
independently verified against anything; it is unique per row by construction (every filing's
specialist readings differ), so it cannot group scores by *which prompt version* produced
them -- exactly what `T-079` needs, to tell a score written before the `T-073`/`T-074`/`T-076`
prompt edits apart from one written after. A value that stays constant across filings and
changes only when a prompt actually changes was the intended behavior all along (the audit
plan's own wording: "`prompt_version` appears nowhere in `src/`"). Corrected by moving the hash
from per-filing (`_synthesize`, over `orchestrator.messages`) to per-analyst (`__init__`, over
the fixed prompt templates + model config) as described above.

### Design decisions

- **Reuse the existing `model` column for the fallback label, rather than a new boolean
  column.** `model` already means "what produced this score"; a distinct, unmistakable
  sentinel (`FALLBACK_MODEL_LABEL`) answers that question directly and keeps every existing
  reader that groups/filters by `model` correct with no further change -- a `model = 'deepseek-
  chat'` filter now genuinely means "the configured model's own output," not "the configured
  model, or maybe the fallback that ran instead of it."
- **One hash per analyst (effectively per run), not per filing.** Computing
  `_prompt_version_hash` once in `__init__` and reusing it for every `analyze()` call is both
  the semantically correct behavior (see above) and cheaper than re-hashing per filing.
- **`temperature=0`/`seed=0` reduce variance; they are not a reproducibility guarantee.** No
  LLM provider guarantees deterministic output at temperature 0 (review: docs must not claim
  otherwise). `seed` is opportunistic and not verified live -- this environment's network
  egress policy blocks reaching the configured LLM endpoint directly from a test/session;
  `seed` is a standard, well-known OpenAI chat-completions field the openai-python client
  (which Strands' `OpenAIModel` wraps) accepts without validating it against a fixed
  allowed-keys schema, so including it is safe (no client-side error) whether or not DeepSeek's
  backend actually uses it. Confirming it live against the real endpoint is left as a step
  before `T-079` begins.

### Verification

- New tests: `tests/test_agents.py` -- `test_two_filings_in_one_run_share_the_same_prompt_hash`,
  `test_changing_one_specialist_prompt_changes_the_hash` (monkeypatches an inline specialist
  prompt), `test_changing_the_model_id_changes_the_hash`. `tests/test_pipeline.py`'s existing
  `test_run_labels_a_fallback_score_and_records_it_on_the_run` -- a stub analyst that always
  falls back; asserts every written `score_snapshot.model == FALLBACK_MODEL_LABEL`, every row's
  `prompt_hash` is the stubbed hash, and `analysis_run.fallback_units` equals the number of
  filings processed.
- `uv run pytest -q` -- 714 passed (was 710 before this fix; +3 net new tests, all in
  `tests/test_agents.py` -- `test_pipeline.py`'s fallback test was already counted at 711 in
  the first, review-corrected cut).
- `uv run ruff check` / `ruff format --check` / `uv run mypy` / `pre-commit` -- all green.
- `SPEC.md` FR-002 and the `score_snapshot` table row updated; `docs/fundamental_agent.md`
  updated (the `agents.py` section and `insert_snapshot`'s row) to describe the per-run,
  prompt-version hash and the "reduces variance" (not "reproducible") wording.

### Residual scope, deliberately deferred

- **The one existing production fallback row (and any others already written) keeps its old
  `model = 'deepseek-chat'` label and a `NULL prompt_hash`.** This fix only changes what a
  *future* run writes; relabeling historical rows is a production data correction, not a code
  change -- tracked as `T-124`, gated on the user's explicit direction like every other
  production write in this file. `prompt_hash` stays `NULL` for any row written before this
  fix regardless: the original prompt templates/model config in effect when it was written are
  not independently recoverable from the row itself.
  **Done 2026-09-29** (`T-124`, `TASKS.md`): identified precisely via `_fallback_assessment`'s
  own fixed `narrative` string, which no real LLM reply ever produces verbatim -- exactly one
  of the 377 production FUNDAMENTAL rows matches (`id` 171, asset `PG`). Relabeled `model` to
  `agents.FALLBACK_MODEL_LABEL`; `prompt_hash` stays `NULL` as expected. Backed up first;
  post-write `quick_check`/`foreign_key_check` clean.
- **Confirming the live DeepSeek endpoint actually accepts (or ignores) `seed`** is left as an
  explicit step before `T-079` begins, per review -- this environment cannot reach it.

## T-114 — a dirty working tree could silently write production runs (`cycle`, `quant`, `fundamental_agent`, `entity_resolution`, `pricing_agent`)

**Status**: Fixed 2026-09-27 (`T-114`); extended 2026-09-27 per PR #92 review
(`@eldova1702`): two more run writers guarded, and "dirty" rescoped to the paths that
actually change a run's results.

### Symptom

Production run-log rows (`analysis_run`, `quant_run`, `cycle_run`) carry `code_version`
`359797e-dirty`: `kg_schema.provenance.code_version()` already appends `-dirty` when `git
status --porcelain` reports anything uncommitted, so the *fact* that a run's code diverged
from `HEAD` was always recorded -- but nothing ever *acted* on that fact. A run from a
work-in-progress checkout wrote its `analysis_run`/`quant_run`/`cycle_run` row exactly like a
clean one; the only way to notice was to separately query for the `-dirty` suffix after the
fact, by which point the results (scores, risk models, books, positions) were already
persisted and, in `quant`/`cycle`'s case, already fed into anything reading the *next* run's
inputs.

### Root cause

`code_version()` computes and returns a *descriptive* tag; no caller ever compared it against
anything or refused on its behalf. This is the same shape of gap `T-097`/`T-086`/`T-110` each
closed for their own guard (an out-of-order cycle date, missing dividends, a stale price
spine): a fact was recorded but not enforced, so a production run could still write over it
silently.

### Fix

- `kg_schema.provenance`: new `DirtyTree(RuntimeError)` and `dirty_tree_reason(version=None)`
  -- `None` if *version* (default: `code_version()` itself) doesn't end in `-dirty`, else a
  message naming it. A fallback tag (`pkg-*`/`unknown`, no git repo at all) never ends in
  `-dirty` and is never refused over.
  ```python
  def dirty_tree_reason(version: str | None = None) -> str | None:
      v = version if version is not None else code_version()
      if not v.endswith("-dirty"):
          return None
      return f"code_version {v} is from an uncommitted (dirty) working tree"
  ```
- `fundamental_agent.pipeline.run`, `quant.persist.run_build_risk_model`/`run_optimize`,
  `quant.returns.run_build_returns`, `quant.actions.backfill_corporate_actions`,
  `quant.evaluate.run_evaluate`, and `cycle.orchestrator._run` (shared by `select`/`monitor`/
  `backfill`) each compute `dirty_tree_reason(code_version())` and raise `DirtyTree` unless
  `settings.allow_dirty`, mirroring exactly where each package's own T-110/T-097/T-086 guard
  already raises in that same function (before the run row exists for `cycle`; inside the
  inner `try`, right after `open_run`, for `quant`; before `start_run` for
  `fundamental_agent`) -- so a refusal is either never logged (cycle-style) or logged and
  immediately marked `"failed"` (quant/fundamental_agent-style), consistent with how each
  package already handles its other guards.
- `--allow-dirty` added to every affected CLI subcommand (`quant build-risk-model`/`optimize`/
  `evaluate`/`build-returns`/`backfill-actions`; `cycle select`/`monitor`/`backfill`;
  `fundamental_agent run`), recording the bypass reason on the run's `params_json` and
  surfacing a CLI `WARNING`, the same convention as `--allow-stale-prices`/
  `--allow-no-dividends`/`--allow-backdated`.
- New result fields carrying the bypass reason through to the CLI: `RiskModelResult`/
  `OptimizeRunResult`/`EvaluateResult`.`dirty_tree_bypassed` and `ActionsReport`/
  `ReturnsReport`.`dirty_tree_bypassed` (`quant`); `CycleReport.dirty_tree_bypassed` (`cycle`);
  `RunReport.dirty_tree_bypassed` (`fundamental_agent`, not yet CLI-printed -- this package's
  CLI doesn't surface any of its other run-level bypass reasons either, e.g. `T-113`'s
  `fallback_units`, so none is added here for consistency within the package).

### PR #92 review follow-up

`@eldova1702` found the first pass incomplete on two counts (both fixed in the same PR,
before merge):

1. **Two more run writers stamped `code_version()` with no guard at all.**
   `entity_resolution.pipeline._open_cycle` (the `sharedExecutiveWith` edges the
   knowledge-graph repo reads) and `pricing_agent.pipeline.run` (`price_observation`
   analytics that feed `TECHNICAL` scoring, veto evaluation, and the quant gate) were
   missed by the original sweep -- both packages predate this task and neither had an
   existing guard convention to extend, which is likely why they were overlooked when the
   other six call sites were being enumerated. Fixed identically to the other six: each
   computes `dirty_tree_reason(code_version())` right after its own `ensure_schema` call
   and raises `DirtyTree` unless `settings.allow_dirty`, before any row is written (no
   partial `cycle_run`/`pricing_run` row on refusal, matching `cycle`'s convention since
   neither writer had one of its own to match). `--allow-dirty` added to `entity_resolution
   build` and `pricing_agent run`; both now print the same CLI `WARNING` on an override.
2. **"Dirty" meant *any* uncommitted file in the checkout, not just one that changes a
   run's results.** `_git_version` called plain `git status --porcelain`, which counts
   every untracked file -- an empty scratch note, a local review doc, a stray `.DS_Store`
   -- turning `af2a710` into `af2a710-dirty` and refusing every run until it was removed or
   `--allow-dirty` was passed on every single run, which defeats the guard's point (a real
   uncommitted *code* change should refuse; a stray untracked file should not). Verified
   directly: adding one empty untracked file at the repo root reproduced the false
   `-dirty` tag before this fix. Rescoped to `git status --porcelain -- src skills
   pyproject.toml uv.lock` (`kg_schema.provenance._DIRTY_SCOPE`) -- the source tree, the
   ratio-skill markdown a prompt can cite, and the two files that pin the resolved
   dependency set. An untracked file *inside* one of those paths (a new, not-yet-committed
   `skills/<ratio>/SKILL.md`) still counts as dirty; only files outside the scope are
   exempt. Applied inside `_git_version` itself, so the recorded `code_version` tag and
   every `dirty_tree_reason` check derived from it agree by construction -- there is no
   second, separately-maintained scope to drift out of sync.

### Design decisions

- **Hermetic tests must not depend on the ambient git state (NR-006).** `code_version()`
  reads *this actual checkout's* live `git status`, and is `functools.lru_cache`d for the
  whole process -- during active development the working tree legitimately has uncommitted
  changes (this very fix's own commits, before they land), which must not make the test suite
  spuriously refuse in every test that exercises one of these six driver functions.
  `tests/conftest.py` gained a session-scoped autouse fixture that primes `code_version()`'s
  cache with one clean, deterministic value (patching `_git_version`/`_package_version` only
  long enough to prime it, not for the whole session), so ambient repo dirtiness never leaks
  into unrelated tests; `tests/test_provenance.py`'s own tests of `code_version()` itself
  clear the cache and patch `subprocess.run` per test, unaffected by this priming. A test that
  specifically exercises one of the six *guards* monkeypatches that driver module's own
  imported `code_version` name (e.g. `quant.persist.code_version`) to a dirty value for that
  one test.
- **One shared `dirty_tree_reason`, six call sites, not a shared "open a run" choke point.**
  `quant.state.open_run` and `cycle.state.open_cycle` are deliberately separate, undecorated
  DB helpers (see `quant/state.py`'s own docstring: importing `cycle` into `quant` would pull
  the optimizer's numeric dependencies onto `cycle`'s import path); the guard belongs to each
  *driver* function's own settings-aware call, the same place T-110's price-spine check and
  T-086's dividends check already live.
- **`evaluate`'s CLI dispatch didn't have any exception handling before this fix** (a
  `ValueError` -- e.g. "no --from given and no quant_portfolio rows exist yet" -- propagated
  as a raw traceback). Adding the `DirtyTree` catch needed a real `try`/`except` there
  regardless, so `ValueError` is now caught alongside it (`quant.cli._run_evaluate`,
  extracted from an inline `main()` block) -- a small, natural improvement bundled with the
  guard it was already necessary to add, not separate scope.

### Verification

- New tests: `tests/test_provenance.py` (+4: `dirty_tree_reason` for a clean/dirty explicit
  version, defaulting to `code_version()`, and a fallback tag never counting as dirty).
  `tests/test_quant_risk_model.py` (+3: `build-risk-model`/`optimize` refuse a dirty version;
  the override records the reason on both the result and `params_json`).
  `tests/test_quant_pipeline.py` (+1: `evaluate` refuses/overrides). `tests/test_quant_dividends_guard.py`
  (+2: `build-returns` refuses/overrides; the `--allow-dirty` flag exists on all five quant
  subcommands). `tests/test_quant_actions.py` (+1: `backfill-actions` refuses/overrides).
  `tests/test_cycle.py` (+4: `select`/`monitor` both refuse; the override records the reason;
  the flag exists on `select`/`monitor`/`backfill`). `tests/test_pipeline.py` (+3:
  `fundamental_agent run` refuses/overrides; the flag exists).
- PR #92 review follow-up: `tests/test_entity_resolution.py` (+2: `build` refuses a dirty
  version before either read-only DB is opened; the override records the reason on both the
  result and `cycle_run.params_json`). `tests/test_pricing_pipeline.py` (+2: `run` refuses/
  overrides the same way). `tests/test_provenance.py` (+3, against a real throwaway git repo
  rather than a mocked `subprocess.run`, so the actual pathspec is what's under test: an
  untracked file outside `src`/`skills`/`pyproject.toml`/`uv.lock` stays clean; a tracked-file
  edit under `src/` and an untracked file under `skills/` both still count as dirty).
- `uv run pytest -q` -- 739 passed (was 732; +7: +2 `entity_resolution`, +2 `pricing_agent`,
  +3 provenance scoping).
- `uv run ruff check` / `ruff format --check` / `uv run mypy` / `pre-commit` -- all green.
- `SPEC.md` FR-012 updated; `docs/quant.md`, `docs/cycle.md`, `docs/fundamental_agent.md`,
  `docs/kg_schema.md` updated with the new flag(s) and the scoped dirty-tree contract.

### Residual scope, deliberately deferred

- **No existing production run is retroactively annotated.** This fix only changes what a
  *future* run does; the already-`-dirty` historical rows this task's own audit found are a
  separate, already-tracked concern (`T-121`/`T-122`/`T-123`/`T-124` cover the specific
  production corrections those runs' *other* defects need; being dirty was a symptom pointing
  at them, not a defect requiring its own separate cleanup once the runs it flagged are
  otherwise corrected or accepted).

## T-115 — `cycle backfill` could not replay history without risking the live book

**Status**: Fixed 2026-09-27 (`T-115`); PR #94 review follow-up fixed same day.

### Symptom

`cycle backfill --from D1 --to D2` called the same live `run_selection` a real `cycle select`
uses, looping it over the date range. Flagged as residual scope by `T-097`'s own entry above
("`cycle backfill` ... has no `--allow-backdated` flag of its own and will hit the same
`OutOfOrderCycle` refusal, with no way to override it from that path"): the moment the live
`portfolio_position` book already held an entry dated later than the range being backfilled —
true of almost any real historical replay, since backfill exists precisely to recompute *past*
dates against an already-running production book — every one of `sync_positions`'s two guards
(`out_of_order_reason`'s pre-check, and its own unconditional "would end positions opened
later" invariant) refused the run, with no override on that path. Separately, `cycle_run` is
unique per `(cycle_type, cycle_date)`; a `backfill` invocation covering a date already
completed (by an earlier `backfill` attempt, or by a real `select`) silently resumed that same
row and skipped every checkpointed step — including `"positions"` — so re-running backfill
after fixing a bug in an upstream stage never actually recomputed the dates it needed to.

### Root cause

`backfill` was never given its own write path or its own run-log identity — it reused
`SELECTION`'s exactly, which is correct for computing scores/vetoes/ranking (there is only one
right answer for a given date and metrics version) but wrong for positions specifically: a
historical replay's positions are a simulation, not the one production book every other repo
reads as ground truth, and conflating the two made the live book's own out-of-order protection
(`T-097`) an unintended obstacle to replaying history at all.

### Fix

- `kg_schema.ddl`: new `portfolio_position_replay` table — the same columns, `UNIQUE
  (asset_id, valid_from)` key, and T-104 valid_to-before-valid_from triggers as
  `portfolio_position`, but written only by a replay. `cycle_run.cycle_type` gains a third
  value, `'REPLAY'`, alongside `'SELECTION'`/`'MONITORING'`.
- `src/cycle/replay.py` (new): `out_of_order_replay_reason` / `sync_replay_positions` — the
  same shape as `writers.out_of_order_reason` / `writers.sync_positions`, against
  `portfolio_position_replay` instead of `portfolio_position`, kept as their own literal-SQL
  functions rather than a table-name-parameterized version of the live ones (Code & Git #10:
  an identifier a `?` placeholder can't bind must never be built from a variable, even one
  this module fully controls — an automated SAST scanner flags the pattern itself). Also
  `reset_replay_range(conn, date_from)`: `--force`'s implementation — deletes any replay
  stint opened on or after `date_from` (the redo decides whether it ever existed), reopens
  any it closed on or after `date_from` (the redo decides when, if ever, it closes again),
  and drops every `cycle_run` row on or after `date_from` (cascading to their
  checkpoints/ranking) so every step re-executes instead of being skipped as already
  `"done"` — nothing *before* `date_from` is touched. (Originally bounded at `date_to` too;
  corrected by the PR #94 review below.)
- `src/cycle/orchestrator.py`: a new public entrypoint, `run_replay` (same step sequence as
  `run_selection`, `cycle_type='REPLAY'`); the `"positions"` step branches on `cycle_type` —
  `REPLAY` checks `out_of_order_replay_reason`/calls `sync_replay_positions`, with **no**
  `--allow-backdated`-style override (there is nothing to override: `--force` resets the
  requested range up front instead, so a normal, in-range replay write is never out of order
  to begin with); anything else keeps `T-097`'s existing live-book path unchanged.
- `src/cycle/cli.py`: `backfill` now calls `run_replay`, not `run_selection`; new `--force`
  flag calls `reset_replay_range` once, before the date loop, over the requested
  `--from`/`--to` (printing that it did, the same convention as the other bypass `WARNING`s).

### Design decisions

- **One replay table, not a per-batch/per-experiment one.** T-115's acceptance criterion is a
  single "a full replay" that "can be re-run after a fix," not concurrent independent replay
  experiments over overlapping ranges — `portfolio_position_replay` mirrors
  `portfolio_position`'s own single-continuous-timeline shape exactly (one implicit book,
  the same way there is one implicit live book), rather than adding a `cycle_replay_batch`
  table and threading a batch id through every row, which nothing here actually needs yet.
- **`--force` resets `date_from` onward, not the whole table, and not a bounded
  `date_from..date_to` range either (PR #94 review).** The first pass scoped the wipe to
  exactly `--from`/`--to`, on the reasoning that a replay of one window shouldn't discard a
  separately-replayed, non-overlapping one — true, but a replay is path-dependent: a stint
  past `--to` still constrains whether `--from` can redo cleanly, so leaving it in place left
  the book internally inconsistent (see PR #94 review follow-up below). An open-ended `>=
  date_from` reset is still strictly safer than an unconditional truncate (nothing before
  `date_from` is ever touched), at no extra complexity.
- **The out-of-order guard still exists for the replay book, without an override flag of its
  own.** A *forced* range is always internally consistent (reset, then replayed strictly
  forward) so it never trips; the guard exists only to catch a genuinely new, never-before-
  replayed date that happens to be older than the replay book's current latest — a real
  footgun (an unrelated later `backfill` call landing before an earlier historical range you
  never got around to), not a case that needs its own bypass: the fix is to `--force` a wide
  enough range, not to write around the inconsistency it would otherwise create.

### Verification

- 8 new tests in `tests/test_cycle.py`: the pure `out_of_order_replay_reason` function (no
  rows → safe; newer/same date → safe; older date → a reason naming both dates and
  `--force`); a replay writes only `portfolio_position_replay` (the live book stays at 0
  rows) under `cycle_run.cycle_type = 'REPLAY'`; a replay at a date older than an
  *already-established live* book succeeds (proving isolation from `T-097`, which the old
  backfill-via-`run_selection` path did not have); resuming the same replay date a second
  time skips `"positions"` and does not duplicate rows; a genuinely new, older replay date is
  refused (`OutOfOrderCycle`, naming `--force`) with the replay book left exactly as before;
  `reset_replay_range` lets a completed date recompute (`"positions"` runs again, not
  skipped) and leaves every earlier date's stints untouched; the `--force` flag exists only on
  `backfill`'s parser.
- PR #94 review follow-up (first round): `tests/test_cycle.py` (+4): `reset_replay_range`
  resets a stint past `--to` too, so redoing `--from` no longer trips the out-of-order-replay
  guard (reproduces the reviewer's exact scenario); a bare `reset_replay_range(conn,
  date_from)` still leaves every stint before `date_from` untouched; `backfill` refuses
  without `--db`; `backfill` refuses when `--db` resolves to `KG_FINANCIAL_DB`'s configured
  path; `backfill` runs normally against an explicit, non-production `--db`.
- PR #94 review follow-up (second round): `tests/test_cycle.py` (+2): `backfill` refuses a
  relative path to the production database (`--db data/financial.db`, run from its directory)
  and a symlink to it, not just its literal configured string.
- `uv run pytest -q` — 753 passed (was 739 before T-115; 747 after the first pass; 751 after
  the first review round; +2 from the second). `ruff check` / `ruff format --check` /
  `uv run mypy` — all green.
- `docs/cycle.md` and `docs/kg_schema.md` updated with `portfolio_position_replay`, the
  `REPLAY` cycle type, and `--force`.

### PR #94 review follow-up

`@eldova1702` found two real bugs and one unrelated pre-existing gap worth its own task (all
fixed/recorded in this same PR, before merge):

1. **The replay was not isolated from the live system.** Only the `"positions"` step reads/
   writes an isolated table; every other step (`score_snapshot`, `veto`,
   `sector_aggregate_snapshot`, `cycle_ranking`) wrote the *same* shared tables a live
   `select`/`monitor` and `quant`'s universe gate read. Reproduced: after
   `run_replay("2026-06-30")`, `quant.db.hard_vetoed_as_of("2026-06-30")` picked up the
   replay's own veto rows, and a subsequent live `run_selection("2026-07-01")` ranked an
   asset vetoed that would not otherwise be. Per-table isolation for every step `backfill`
   touches is a much larger change than this task's scope; the reviewer's proposed "simplest
   full isolation" — a separate database — is what shipped instead. `--db` is now mandatory
   on `backfill` and refused outright when it resolves to the configured production path
   (`KG_FINANCIAL_DB`, `kg_schema.env.database_path`); `cycle/cli.py`'s new
   `_refuse_production_backfill` performs both checks (missing `--db`; `--db` equal to the
   resolved production path) before anything else runs, and `--help` documents "copy the
   database first."
2. **`--force` did not reset far enough.** See the `reset_replay_range`/Design-decisions
   corrections above — a stint past `--to` was left in place, so redoing `--from` after a
   `--force` immediately tripped `out_of_order_replay_reason` against it, with the requested
   range now half-deleted. Fixed by dropping `date_to` from `reset_replay_range` entirely: it
   resets from `date_from` onward, unconditionally.
3. **New task recorded, not implemented here (out of this PR's scope):** `T-125` — vetoes are
   written as per-date events (`write_vetoes` clears only `cycle_date = ?`, the run's own
   date), not stints, so a HARD veto never actually clears once raised and a SOFT rule that
   holds across several weekly cycles is penalized once per cycle it held, not once while
   held. Added to `.specify/memory/TASKS.md` as P0, blocking any `backfill` run and the next
   live `select` — a correctness bug in the live system today, `backfill`'s database-copy
   fix does not touch it (a copy of a wrong book is still wrong).

Second round, on the fix for (1) above: `_refuse_production_backfill` compared `--db` and the
resolved `KG_FINANCIAL_DB` path as plain strings, so a relative alias (`--db
data/financial.db` from the production directory), a `..`-laden path, or a symlink to the
production file all compared unequal to its canonical path and were accepted — exactly the
cases the check exists to catch, now that a separate database is `backfill`'s only isolation
boundary. Fixed with a new `_same_database(a, b)` helper: `os.path.samefile` (inode
comparison) when both paths exist, falling back to comparing each side's `.resolve()`d
(symlink-following) absolute path when one doesn't exist yet (a throwaway copy not yet
created, or a typo'd production path — still worth naming correctly, even though there is
nothing on disk to `samefile` against). 2 new tests: a relative path and a symlink to the
production file are both refused.

### Residual scope, deliberately deferred

- **No repair tool for the replay book.** `cycle/repair.py`'s `plan_undo`/`apply_undo` (T-104)
  only ever address the live `portfolio_position`; `portfolio_position_replay` needs none of
  that machinery today because `--force`/`reset_replay_range` already gives it a full,
  intentional reset path the live book deliberately does not have (the live book must never
  be bulk-reset; a replay book is meant to be).
- **`quant`'s analogous multi-book design (`quant_portfolio`/`quant_position`, keyed by
  `portfolio_id`) was not adopted here**, even though it would let several replay experiments
  coexist — flagged in Design decisions above as unneeded for this task's acceptance
  criterion, not overlooked; a future task that actually wants concurrent replay experiments
  should reach for that shape rather than re-deriving it.

---

## T-116 — Negative-equity distress screen recalibrated: `net_debt_to_ebitda` replaces `debt_to_assets`

**Status**: Fixed 2026-09-28 (`T-116`); PR #95 review (`@eldova1702`) found two real bugs and
two docs corrections, all fixed/corrected the same day — see "PR #95 review" below.

### Symptom

`LEVERAGE_EXTREME`'s negative-equity branch (`cycle/rules/builtin.py`, C2) and
`DQ_NEG_EQUITY`'s HARD/SOFT split (`fundamental_agent/quality.py`, T-065) both gate on
`debt_to_assets > 0.8 OR interest_coverage < 1.5`. Recomputed against every negative-book-
equity filing in the 20-asset production sample (`financial.db`, read-only; statements rebuilt
from stored `financial_facts`, leverage ratios recomputed with today's code including T-105's
TTM annualization — same method as `scripts/verify_t105.py`), `debt_to_assets` never moves:
MCD's 19 filings (FY2021-2026Q2) sit in a narrow `[0.661, 0.719]` band, always comfortably
under the 0.8 guard, regardless of the buyback program that pushed `debt_to_equity` from
-7.74 to -38.97 over the same window. That MCD *should* be spared is not in question (its
`interest_coverage`, 5.89-9.42x, and `net_debt_to_ebitda`, 2.53-2.96x, both say so) — the
methodology gap is that `debt_to_assets` gives the right answer for the wrong reason here: it
is a balance-sheet solvency ratio (debt against total assets), not a leverage-capacity one
(debt against cash-flow-generating capacity), so it cannot distinguish a genuinely over-
levered buyback name from a healthy one whose asset base happens to keep the ratio under 0.8 —
it would stay just as inert for a name whose fundamentals actually warranted the veto, as long
as its balance sheet shape was similar. Independently, when *both* corroborating metrics are
missing, `_LeverageRule.evaluate()`'s `if breached:` guard means the negative-`debt_to_equity`
signal is dropped with no `VetoHit` at all — not even a review item: APA's FY2021 10-K and
2022Q1 10-Q (`debt_to_equity` -4.71 / -327.17) and APO's 2022Q3 10-Q (-4.54) all have neither
`net_debt_to_ebitda` nor `interest_coverage` resolvable from the stored facts, and evaluated to
nothing, silently, on the cycle side (`DQ_NEG_EQUITY` in `fundamental_agent` does not share
this half of the bug — see Design decisions).

### Root cause

Two independent gaps in the same negative-equity branch, both pre-dating T-105 (`net_debt_to_
ebitda` was not reliably annualized until then, so C2's original author reused
`debt_to_assets`, the only leverage-adjacent metric already trustworthy on a 10-Q):

1. `debt_to_assets` was never the right metric for a *leverage-capacity* screen — a corporate
   credit analysis convention keys leverage on debt relative to earnings (`net_debt_to_ebitda`),
   not debt relative to total assets. S&P Global Ratings' "Corporate Methodology" (Nov. 2013),
   Table 16, sets its "Aggressive" debt/EBITDA band at 4x-5x and "Highly leveraged" at above
   5x, the industry-standard reference point this fix's `5.0` threshold reuses directly.
   `interest_coverage < 1.5` (debt-service capacity, the other half of the standard credit
   pair) was already correctly calibrated and is unchanged. **Limitation (PR #95 review):**
   S&P's band is defined on *adjusted* debt (capitalized leases, pension underfunding, surplus-
   cash netting per its own criteria); `net_debt_to_ebitda` here is plain balance-sheet debt
   minus cash (`leverage.py::_total_debt`/`compute`), so this screen is systematically more
   lenient than the cited band, most for lease-heavy names (retail, restaurants) whose
   capitalized-lease debt the numerator omits. Not corrected in this pass — adjusting for
   leases/pensions is its own, separately-verified methodology change.
2. `_LeverageRule.evaluate()`'s `if breached: hits.append(...)` only ever appends a hit when
   the OR condition is true; when both corroborating metrics are `None`, `breached` is `False`
   by Python's short-circuit `and`/`or` semantics, so nothing is recorded — an unrecoverable
   "can't tell" case was conflated with a verified "not distressed" one.

### Fix

- `src/cycle/rules/builtin.py::_LeverageRule`: `neg_equity_debt_to_assets_threshold` (0.8)
  replaced with `neg_equity_net_debt_to_ebitda_threshold` (5.0); `evaluate()`'s negative-
  `debt_to_equity` branch now reads `leverage.net_debt_to_ebitda` in place of
  `leverage.debt_to_assets`, and — before the OR check — a new branch: when both
  `net_debt_to_ebitda` and `interest_coverage` are `None`, appends a `SOFT` `VetoHit` (evidence
  naming both as unavailable) instead of returning nothing. `interest_coverage`'s threshold
  (1.5) and the OR-not-AND structure (either signal independently sufficient) are unchanged.
- `src/fundamental_agent/quality.py::_neg_equity`: `NEG_EQUITY_DEBT_TO_ASSETS` (0.8) replaced
  with `NEG_EQUITY_NET_DEBT_TO_EBITDA` (5.0); `distressed` now reads `("leverage",
  "net_debt_to_ebitda")` in place of `("leverage", "debt_to_assets")`. No structural change
  needed here — `_neg_equity` already always returns an `Issue` (HARD or SOFT) whenever
  `equity <= 0`, so it never had the silent-drop half of the bug.
- `kg_schema.versions.DATA_QUALITY_GATE_VERSION` bumped `dq-v1` -> `dq-v2`: this is a gate-
  *methodology* change (the HARD/SOFT boundary for `DQ_NEG_EQUITY` moves), so it re-gates as a
  parallel, append-only set of `data_quality_issue` rows exactly the way T-065's own design
  intends ("`gate_version` makes a future threshold change a parallel set of rows") rather than
  silently reclassifying `dq-v1`'s already-recorded verdicts in place.
- **(PR #95 review, point 1)** New `kg_schema.queries.StaleGateVersion`/
  `stale_gate_version_reason(conn, gate_version)`: `None` when `data_quality_issue` has no rows
  at all (Ring-1 never run -- a bootstrap situation) or already has rows under the current gate
  version; otherwise names the older version(s) present. Wired into `cycle`'s shared `_run`
  (`orchestrator.py`) the same way as T-110's/T-114's guards: refuses `select`/`monitor`/
  `backfill` unless `CycleSettings.allow_stale_dq_gate` (new, default `False`; CLI
  `--allow-stale-dq-gate`) is set, recording the bypass reason on the run
  (`stale_dq_gate_bypassed`, `CycleReport`/`params_json`) for the CLI's `WARNING`. Without this,
  merging the `dq-v1` -> `dq-v2` bump alone would have let the very next cycle run against a
  production database still holding only `dq-v1` rows: `cycle/data.py::data_quality` reads only
  `d.gate_version = DATA_QUALITY_GATE_VERSION`, so every quarantine and every HARD
  `DQ_*`/`DATA_QUALITY` veto would silently read as clean, not because filings got cleaner.
- **(PR #95 review, point 2)** `net_debt_to_ebitda = safe_div(net_debt, ebitda)` goes
  *negative* when EBITDA itself is negative, even with large positive net debt -- the most
  distressed profile of all, not a healthy one, and a plain `> 5.0` check never catches it
  (real data: WAT 10-Q 2026-04-04, stored `metrics-v2`, EBITDA -$11M, net debt $4.4B, ratio
  -399.36 -- reproduced directly from production, not just synthetically). Fixed differently on
  each side, per what each can see:
  - `fundamental_agent/quality.py::_neg_equity` reconstructs EBITDA and net debt directly from
    the same stored inputs the ratio was divided from (`ebitda_ttm` when annualized on a 10-Q,
    else `operating_income + depreciation_amortization`; `total_debt - cash`) via two new
    helpers, `_ebitda`/`_net_debt`, and new `_INPUT_SOURCES` entries. `EBITDA <= 0` with
    `net_debt > 0` is HARD outright, ahead of the ratio-threshold check -- distinguishing WAT's
    shape from a genuine net-cash position (HUM 10-Q 2023-03-31, ratio -1.1, healthy: net debt
    is negative there, not positive) that also happens to read as a negative ratio.
  - `cycle/rules/builtin.py::_LeverageRule` has no such reconstruction available -- it only
    ever reads the already-divided `leverage.net_debt_to_ebitda` metric, never the raw
    statement facts (`cycle` never imports `fundamental_agent`). A negative ratio is therefore
    treated as *unresolved*, not healthy: it falls back to `interest_coverage` alone, the same
    as if `net_debt_to_ebitda` were missing (including feeding the NULL-on-both `SOFT` branch
    above when `interest_coverage` is also unavailable).

### Design decisions

- **Threshold value: 5.0x, not re-derived from scratch.** Reusing a cited, external credit-
  methodology band (S&P's "highly leveraged" boundary) rather than curve-fitting a number to
  the 20-asset sample follows Code & Git/constitution AI behavior #12's citation requirement,
  and it happens to reproduce the sample's existing HARD/SOFT split exactly (see Verification)
  — evidence the number is doing genuine work, not just backfitted to match.
- **`interest_coverage`'s threshold and OR structure are untouched.** The task ("the standard
  credit pair `net_debt_to_ebitda` + `interest_coverage`") only asked to replace the balance-
  sheet leg; `interest_coverage < 1.5` already came from the same 342-filing Ring-1 calibration
  C2 originally cited and nothing in this pass's data contradicts it.
- **The NULL-on-both fix is cycle-only.** `DQ_NEG_EQUITY` already emits an `Issue` unconditionally
  once `equity <= 0` (quarantining D/E and ROE either way); only `_LeverageRule`'s veto path had
  the "no hit at all" gap, so only it needed the new branch. The new hit is `SOFT`, not `HARD`
  — an unresolvable case is a review item, the same severity `DQ_NEG_EQUITY` already assigns it,
  not an assumption of distress.
- **A gate-version bump, not an in-place reclassification.** `data_quality_issue` is append-
  only by design (T-065); production's existing `dq-v1` rows keep recording what was true under
  the old threshold, and a `quality` re-run writes fresh `dq-v2` rows alongside them once
  someone runs it — consistent with every other versioned measurement table in this repo.

### Verification

- Recomputed over every negative-book-equity filing in the 20-asset production sample (41
  filings, 4 tickers: MCD 19, SBAC 19, APA 2, APO 1 — matches T-065's own production backfill
  count exactly, cross-validating this pass's read against that independently-recorded one):

  | Ticker | Filings | `net_debt_to_ebitda` range | `interest_coverage` range | Before (`debt_to_assets`) | After (`net_debt_to_ebitda`) |
  |---|---|---|---|---|---|
  | MCD | 19 | 2.53x-2.96x | 5.89x-9.42x | SOFT (`d/a` 0.661-0.719, always < 0.8) | SOFT (both metrics healthy, always < 5.0 / > 1.5) |
  | SBAC | 19 | 6.91x-8.16x | not resolvable | HARD (`d/a` 1.084-1.255, always > 0.8) | HARD (`net_debt_to_ebitda` always > 5.0) |
  | APA | 2 | not resolvable | not resolvable | `DQ_NEG_EQUITY` SOFT; `LEVERAGE_EXTREME` dropped the hit entirely (both metrics `None`) | `DQ_NEG_EQUITY` still SOFT (unchanged); `LEVERAGE_EXTREME` now SOFT too, but dead on a *gated* filing (see PR #95 review, point 3) |
  | APO | 1 | not resolvable | not resolvable | `DQ_NEG_EQUITY` SOFT; `LEVERAGE_EXTREME` dropped the hit entirely | same as APA |

  `DQ_NEG_EQUITY`'s HARD/SOFT split is unchanged in count (19 HARD / 22 SOFT, same as T-065's
  production backfill) — the new metric reclassifies nothing in this sample, it closes the gap
  for a future name the old ratio would have missed. `LEVERAGE_EXTREME` now records a `SOFT`
  hit for APA/APO's 3 filings instead of dropping them silently, but **not on the live path**
  for any of today's 41 filings — all are already `DQ_NEG_EQUITY`-gated, which quarantines
  `debt_to_equity` to `None` before `_LeverageRule` ever runs (PR #95 review, point 3, corrects
  this entry's original claim that these "now surface a SOFT review hit" in the live cycle).
- New/changed tests: `tests/test_cycle.py` --
  `test_leverage_rule_hard_vetoes_negative_equity_with_high_net_debt_to_ebitda` (SBAC-shaped:
  7.35x with `interest_coverage` unavailable still trips HARD),
  `test_leverage_rule_hard_vetoes_negative_equity_with_low_interest_coverage` (the OR is still
  a genuine OR), `test_leverage_rule_spares_negative_equity_with_healthy_debt_load` (MCD-
  shaped, including exactly-at-threshold values), and
  `test_leverage_rule_negative_equity_with_no_corroborating_metrics_soft_vetoes` (replaces the
  old `..._does_not_veto` test: now asserts a `SOFT` hit, not silence). `tests/test_data_quality.py`
  -- `test_negative_equity_quarantines_de_and_roe_hard_only_when_distressed` reparametrized on
  `net_debt_to_ebitda`; `test_the_table_checks_severity_and_the_view_names_the_filing` updated
  to trigger HARD via the new metric.
- **Point 3's requested stat**: of the 41 negative-equity filings, 3 (APA x2, APO x1; 7.3%)
  have neither `net_debt_to_ebitda` nor `interest_coverage` resolvable -- the NULL-on-both case.
  0 of 41 have `EBITDA <= 0` with positive net debt (point 2's failure shape) in today's sample
  -- `_neg_equity`'s new branch changes no classification here either; both fixes are verified-
  correct but forward-looking for this specific 20-asset sample, not reclassifications of it.
- `uv run pytest -q` -- 771 passed (759 before this review, itself after T-121's unrelated +6;
  this review adds 12: 8 for the gate-version guard (`tests/test_kg_schema.py` +3: safe with no
  rows, safe once the current version has rows, names the older version left behind;
  `tests/test_cycle.py` +5: `select`/`monitor` both refuse, safe with no rows, safe once the
  current version has rows, `--allow-stale-dq-gate` overrides and records it) and 4 for the
  EBITDA-sign fix (`tests/test_data_quality.py` +2: WAT-shaped HARD, HUM-shaped still SOFT;
  `tests/test_cycle.py` +2: unresolved falls back to `interest_coverage`, `interest_coverage`
  alone still fires)). `ruff check` / `ruff format --check` / `uv run mypy` -- all green.
- `docs/fundamental_agent.md`, `docs/cycle.md` updated (`DQ_NEG_EQUITY`'s threshold text,
  `dq-v1` -> `dq-v2` in the manifest example).

### PR #95 review (`@eldova1702`, 2026-09-28)

All four points confirmed and addressed the same day, two code fixes and two docs corrections:

1. **Bumping to `dq-v2` would have silently disabled Ring-1 until the re-gate ran.**
   `cycle/data.py::data_quality` reads only `d.gate_version = DATA_QUALITY_GATE_VERSION`; with
   production holding only `dq-v1` rows and the re-gate deferred, the very next cycle would get
   zero quarantines and zero HARD issues, with no warning. Fixed: new
   `kg_schema.queries.StaleGateVersion`/`stale_gate_version_reason`, wired into `cycle`'s shared
   `_run` as a precheck (same shape as T-110's/T-114's guards), refusing `select`/`monitor`/
   `backfill` unless `--allow-stale-dq-gate` overrides it (recorded on the run). See Fix above.
2. **Negative EBITDA with positive net debt was read as healthy.**
   `net_debt_to_ebitda = safe_div(net_debt, ebitda)` goes negative when EBITDA `< 0`, so `> 5.0`
   never fires on the most distressed profile; reproduced both synthetically
   (`evaluate(_fm(equity=-10, net_debt_to_ebitda=-3.0, interest_coverage=None))` gave
   `DQ_NEG_EQUITY` `SOFT`) and against real, currently-*stored* `metrics-v2` data (WAT 10-Q
   2026-04-04: -399.36, reviewer's exact figure) -- not yet the T-105-annualized number, but the
   sign defect reproduces identically either way. Fixed by reconstructing EBITDA/net debt
   directly on the `fundamental_agent` side (has the raw inputs) and treating a negative ratio
   as unresolved on the `cycle` side (does not). See Fix above.
3. **The "APA/APO now surface a SOFT review hit" claim didn't hold in the live cycle.**
   `DQ_NEG_EQUITY` quarantines `debt_to_equity` for SOFT and HARD alike, so `dq.apply` nulls it
   before `_LeverageRule` ever sees the asset -- reproduced: after the quarantine, the rule
   returns no hit for the both-missing shape. The live path is `DQ_NEG_EQUITY` HARD ->
   `DATA_QUALITY` veto (`_DataQualityRule`); `DQ_NEG_EQUITY` SOFT -> `debt_to_equity` reads as
   `+inf` in VALORIZATION (`orchestrator.py`'s `_valorization`, C2's own transform, keyed off
   `DataQuality.negative_equity` rather than the quarantined value), no veto. `_LeverageRule`'s
   branch only runs for a filing Ring-1 has not yet gated (its documented "backstop" role, C2's
   own Design decisions) -- true of none of today's 41 filings, all already gated. Corrected in
   Symptom/Design decisions/Verification above (this entry originally claimed otherwise); the
   requested "how often both metrics are missing" stat is in Verification.
4. **S&P's `>5.0x` band is defined on adjusted debt** (capitalized leases, pension
   underfunding, surplus-cash netting per its own criteria); this screen's `net_debt_to_ebitda`
   is plain balance-sheet debt minus cash, so it is systematically more lenient than the cited
   band, notably for lease-heavy names. Stated as a limitation next to the citation in Root
   cause above; not corrected in this pass -- a lease/pension adjustment is its own,
   separately-verified methodology change.

### Residual scope, deliberately deferred

- **Production re-gate under `dq-v2`.** Production's `data_quality_issue` table still only
  carries `dq-v1` rows; a `python -m fundamental_agent quality` re-run is needed to populate
  `dq-v2` verdicts before `cycle` (which reads only the live `DATA_QUALITY_GATE_VERSION`, and
  now refuses to run without them or `--allow-stale-dq-gate`) sees them — the same category of
  pending production action as F1/F2/F4/T-108/T-109's deferred re-persists, held pending
  explicit user direction. This is now also enforced, not just documented (point 1 above).
- **`rule_catalog` staleness in production**, the same residual C2 already flagged: the seeded
  `LEVERAGE_EXTREME` row's `description`/`params_json` in production still reflect the pre-
  T-116 threshold text until a one-time catalog `UPDATE` is run; live veto *behavior* is
  unaffected (`evaluate()` runs the live Python `RULES` object directly).
- **`net_debt_to_ebitda`'s lease/pension adjustment** (point 4 above) -- not attempted here;
  the screen is more lenient than its cited band for lease-heavy names in the interim.

---

## T-117 — A revenue `total_concepts` tag can be implausibly *large*, not just too small

**Status**: Fixed 2026-09-28 (`T-117`).

### Symptom

APA's `us-gaap_Revenues` ("Total revenues", F2's `total_concepts` Tier 1 match) is exactly
~2x the income statement's own later, smaller "Total revenues and other" subtotal
(`apa_RevenuesAndOther`) every fiscal year since FY2023 -- $16,558M/$8,192M (FY2023),
$19,474M/$9,737M (FY2024), $17,840M/$9,220M (FY2025) -- while FY2022 is correctly
$11,075M/$12,132M (the "and other" total properly *exceeds* revenue, as it must: "and other"
only adds non-operating items). T-095's plausibility floor (`_total_is_plausible`) only ever
rejects a Tier 1 total for being too *small* relative to a named `spec.concepts` component; it
has no mechanism for a total that is too *large*, and APA tags none of `spec.concepts` for
these years, so nothing bounds it at all -- the wrong value passes straight through.
`net_debt_to_ebitda`'s revenue-adjacent TTM identity also flags APA's 2024Q1-Q3 quarters
independently (`DQ_TTM_CROSSCHECK`, 11-25% gaps, `docs/model_fixes.md` T-105) -- a second,
independent symptom of the same underlying bad revenue figure feeding the TTM computation.

### Root cause

Re-verified directly against SEC's own `companyfacts`/`companyconcept` APIs (live, read-only,
2026-09-28; see T-095's Correction above for the full citation): APA has never filed a
`us-gaap:Revenues` fact in its real XBRL history, and its actual rendered FY2023 10-K income
statement has no "Total revenues" line at all -- only "Total revenues and other". The value our
own gateway presents under `concept = 'us-gaap_Revenues'` is an artifact of its own upstream
processing (`sec_edgar`, `portfolio-data-mining`; tracked as `T-118`), not a real filed fact --
consistent with T-095's FY2021 case (now understood as the same defect's *too-small* direction,
not a separate filer-side tagging mistake, per its Correction above). Both directions share one
root cause: the gateway presents a breakdown/component figure as if it were the consolidated
total, and there is no way to distinguish good from bad by magnitude alone in the too-large
direction the way T-095's floor does for too-small.

### Fix

`src/fundamental_agent/statements.py`, `Statements`:
- New `_label_total_correction(spec, column, total_value)`: after `_first_total_match` finds a
  Tier 1 winner, scans the rest of the same statement, in document order, for a *later* row
  whose **label** (not concept name -- no filer's own custom-taxonomy extension concept is ever
  named) reads as a revenue total (`_LABEL_TOTAL_RE`, `r"\btotal\b.{0,40}\brevenues?\b"`,
  excluding any label containing "cost" -- `_LABEL_TOTAL_EXCLUDE_RE`, see Design decisions) and
  is materially smaller than the Tier 1 total (`_LABEL_TOTAL_CONTRADICTION_RATIO = 0.75`). A
  well-formed statement's later, broader total is never smaller than an earlier, narrower one
  labeled the same way, so finding one *is* the contradiction. When found, and every row
  between the two totals is individually small enough to trust as an adjustment item
  (`_BETWEEN_ROW_CEILING = 0.25` of the later total), the corrected value is the later row's
  value less those in-between rows -- recovering "Total revenues" to the dollar for APA's
  FY2023-FY2025 without naming `apa_RevenuesAndOther` (or any other APA concept) anywhere in
  the code. If contradicted but the in-between rows are too large to trust, returns no guess:
  the caller must not trust the Tier 1 total either, but this does not invent a number.
- `get()`: Tier 1 now checks `_label_total_correction` before (and independent of)
  `_total_is_plausible` -- a contradiction found there returns the corrected value directly; a
  contradiction found with no safe derivation skips `_total_is_plausible` entirely (the total
  is already known-bad) and falls through to Tier 2, same as a rejected-too-small total.

### Design decisions

- **Label-pattern, not concept-name, detection.** The alternative -- adding `apa_RevenuesAndOther`
  to `total_concepts` or a new APA-specific field -- would be exactly the "tuned on APA" mistake
  the task's acceptance criterion calls out T-095 for risking. A label regex generalizes to any
  filer whose statement independently exhibits the same two-total shape, verified by scanning
  every stored filing (see Verification) rather than asserted.
- **The "cost" exclusion was not anticipated -- it was found by the full-universe scan.**
  The first cut of `_LABEL_TOTAL_RE` (`r"\btotal\b.{0,40}\brevenue"`, no exclusion) matched
  "Total cost of revenues" trivially (both words are substrings), firing on 6 tickers/81
  filing-periods that were never a revenue-total defect at all: ADBE, STE, TER, TSLA, URI, XYZ
  all use that exact COGS-line phrase. Every one of those was inspected (see Verification)
  before the exclusion was added, not assumed benign -- exactly the "each inspected" the task's
  acceptance criterion asks for, and the reason the mechanism is a label-pattern search with
  guardrails rather than a bare substring match.
- **`_LABEL_TOTAL_CONTRADICTION_RATIO = 0.75` and `_BETWEEN_ROW_CEILING = 0.25`.** Picked to
  sit strictly between APA's real defect ratios (later/total ~0.42-0.50 for the too-large case;
  each between-row is 2.6-3.3% of the later total across FY2023-2025) and its real correct case
  (FY2022's later/total ~1.10, the "and other" total legitimately exceeding revenue) --
  documented, pinned constants (`tests/test_statements.py`), not tuned to fit one number.
- **A contradiction with no safe correction returns `None`, not the Tier 1 total.** Guessing a
  number from an untrustworthy shape (large, uncertain in-between rows) risks a worse error
  than a `None` a downstream `DQ_REVENUE_POS` gate already knows how to flag; T-117's mandate
  is "reject a revenue total the statement contradicts," and rejection is itself a valid, safe
  outcome the mechanism must support, not only correction.

### Verification

- `scripts/verify_t117.py` (new, mirrors `scripts/verify_t105.py`'s style): scans every stored
  filing's own reporting period across the **full universe actually stored in production**
  (503 assets, 5,076 filings -- not a 20-name sample) and calls
  `_label_total_correction` directly. Result: **16 filing-periods flagged, all APA** (1 ticker),
  all 16 safely corrected, 0 rejected-only, 0 false positives on any other of the 502 other
  assets. Before the "cost" exclusion (Design decisions), the same scan found 81
  filing-periods across 6 tickers (ADBE, STE, TER, TSLA, URI, XYZ), every one inspected and
  confirmed a "Total cost of revenues" false match, none a real defect -- fixed before this
  entry's numbers, not left in production.
- Recomputed APA's `revenue` for all 5 stored 10-Ks against real `financial_facts`
  (`Statements.get("revenue", ...)`, today's code): FY2021 $7,988,000,000 (T-095's existing
  too-small path, unchanged), FY2022 $11,075,000,000 (untouched, correctly not contradicted),
  FY2023 $8,279,000,000, FY2024 $9,737,000,000, FY2025 $8,920,000,000 -- matching this task's
  acceptance criterion's named figures exactly, derived purely from each filing's own stored
  facts, with no APA-specific code anywhere in `statements.py`.
- `tests/test_statements.py` (+5): `test_revenue_rejects_a_total_far_larger_than_the_
  statement_s_own_later_total` (APA FY2023's real shape, resolves to the exact $8,279M),
  `test_revenue_total_label_correction_leaves_a_genuinely_larger_total_alone` (FY2022, not a
  defect), `test_revenue_total_label_correction_ignores_cost_of_revenue_lines` (the false-
  positive shape the full-universe scan found), `test_revenue_total_label_correction_refuses_
  to_guess_when_between_rows_are_too_large` (contradicted-but-unsafe returns no value, not the
  bad total). `uv run pytest -q` -- 769 passed (was 765 post-`T-116`, +5, `T-121`'s own +6
  landing separately). `ruff check` / `ruff format --check` / `uv run mypy` -- all green.
- `docs/model_fixes.md`'s T-095 entry corrected (see its own "Correction (T-117)" section
  above) -- the original "filer-side XBRL tagging defect" diagnosis does not hold; re-verified
  live against SEC's `companyfacts`/`companyconcept` APIs and APA's actual rendered financial
  statement.

### Residual scope, deliberately deferred

- **Production re-persist.** `metrics-v3`'s existing full recompute (`T-100`) will pick up the
  corrected revenue automatically; no separate production action is needed beyond that already-
  planned re-run, since APA's metrics have not yet been persisted under `metrics-v3` in
  production (per `T-105`'s own residual scope).
- **`T-118`** (upstream, `portfolio-data-mining`) tracks the actual gateway fix; this entry's
  guard stays as the local defense until it lands and `T-117`'s guard stops finding anything to
  correct on APA.
- **The exact upstream mechanism (which footnote/context the gateway's `us-gaap_Revenues`
  value is actually drawn from) was not fully traced** -- SEC's `companyconcept` data for
  `RevenueFromContractWithCustomerIncludingAssessedTax` also disagreed with what our gateway
  stores under that concept name for APA (a second, unexplained discrepancy found during this
  verification), suggesting the conflation is broader than the one concept this entry fixes
  around. Left for `T-118`'s upstream investigation, not re-derived speculatively here.

### Correction (T-118 upstream finding, 2026-09-28)

The upstream mechanism deferred above is now confirmed, via `portfolio-data-mining` PR #44's
review (`@eldova1702`): `edgartools==5.44.1`'s `xbrl.statements.income_statement().
to_dataframe()` *synthesizes* a non-dimensional "total" row for a concept by summing that
concept's dimensional axis members, and double-counts whenever one member is itself a parent
whose value already includes its own children. APA's FY2023 case: `8279 (parent "Oil and gas")
+ 7385 + 894 (its two children, which already sum to 8279) = 16558`; FY2024:
`9737 + 8196 + 1541 = 19474` (`8196 + 1541 = 9737`, same shape). This supersedes the earlier
"Oil and gas" corroborating-row observation with the actual arithmetic mechanism producing it.

This is confirmed **general, not APA/revenue-specific**: a full-universe scan (500 companies,
the 61 `us-gaap` concepts `fundamental_agent` reads, compared against each company's SEC
`companyfacts`) found 163 stored values matching no SEC-filed value at that period end, and 105
(company, concept) pairs -- 1,101 rows total -- never filed non-dimensionally at all, so every
value for them is synthesized. `PR #44` fixes **APA's revenue instance only**; it does not close
`T-118`, and this entry's own `T-117` local guard stays in place as the load-bearing backstop
for that scope, not a redundant one. The upstream general fix (`portfolio-data-mining`'s own
`T-042`, not yet started) will validate every synthesized non-dimensional row against the
filing's own default-context facts and mark or replace whatever doesn't reconcile -- see
`portfolio-data-mining`'s `TASKS.md` Work item 5 and `docs/modules/sec-edgar.md` for that
repo's own record.

**Added scope (PR #98 review, 2026-09-28):** when this repo re-ingests against the redeployed
`sec_edgar`, `fundamental_agent` must persist upstream's new `data["corrections"]` list --
today `Statements.from_payload` reads only the three statement keys
(`income_statement`/`balance_sheet`/`cash_flow`), so a derived value like APA's corrected
revenue would land in `financial_facts` indistinguishable from a real filed fact. Needs a flag
or provenance field on the affected `financial_facts` row(s). Not yet designed or implemented;
tracked as `T-118`'s step (3) in `TASKS.md`.

**Step (3) done, 2026-09-30.** `Statements` gained a `corrections` field (`from_payload` reads
`payload.get("corrections") or []`, `[]` for a payload from a gateway version that predates
PR #44 -- no error). `iter_facts` builds a `{(concept, column): rule}` lookup from it and tags
each matching fact's dict with `correction_rule` (`None` for every fact not named in the list,
meaning filed as-is). `financial_facts` gained a nullable `correction_rule TEXT` column
(`kg_schema.ddl.REQUIRED_COLUMNS`, additive -- grafted onto an existing database by
`kg_schema.ensure` the same way `filing_version`/`run_id` already are, no migration needed
since it never existed under a different shape). `db.append_financial_facts` persists
`fact.get("correction_rule")` into it, gated on the column actually being present (mirrors the
existing `has_versioned` fallback for a database that predates `kg_schema.ensure`, so a
pre-`ensure` write still degrades to the base seven columns rather than erroring).
`repair.py`'s re-ingestion path shares this for free -- it already calls `iter_facts`.
**Scope note**: this closes step (3) only, not the rest of `T-118` -- steps (1) (re-verifying
`T-117`'s guard against the actual redeployed `sec_edgar`) and (2) (the upstream general
defect, `portfolio-data-mining`'s own `T-042`, not yet started there) are unaffected. Step (1)
specifically needs a live call to the real gateway (`http://host.docker.internal:8000`), which
this sandbox has no network path to (confirmed: `curl` to it times out) -- it stays gated on
the user's own devcontainer, the same category as `T-121`-`T-124`'s production-DB actions.
Tests: `tests/test_statements.py` (+2: a payload with no `corrections` key parses to `[]`;
`iter_facts` tags only the named `(concept, column)` cell, leaving the same concept's other
periods and every other concept `None`), `tests/test_db.py` (+1: `append_financial_facts`
round-trips `correction_rule` through a real `memory_db`, uncorrected fact reads back `NULL`).
`uv run pytest -q` -- 802 passed (was 799); `ruff check` / `ruff format --check` / `uv run
mypy` -- all clean.

**Steps (1) and (2), live re-verification -- done 2026-09-30.** The user raised the
`sec_edgar` gateway (unreachable from the sandbox as of the entry above; confirmed reachable
now, `curl http://host.docker.internal:8000/edgar/edgar/company_info/APA` -> 200) and merged
the upstream general fix the same day (`portfolio-data-mining` PR #45, closing its own `T-042`,
plus PR #46 isolating its per-statement reconciliation failures).

*Step (1) -- re-verified against the real, redeployed gateway (no mocking).* Pulled APA's
10-Ks live via the real `EdgarClient`/`Statements`/`iter_facts` path:

| Filing | `revenue` resolves to | Acceptance figure | `correction_rule` |
|---|---|---|---|
| FY2023 10-K | $8,279,000,000 | $8,279,000,000 | `T-118` |
| FY2024 10-K | $9,737,000,000 | $9,737,000,000 | `T-118` |
| FY2025 10-K | $8,920,000,000 | $8,920,000,000 | `T-118` |

End-to-end ingestion through the real production path (`db.append_financial_facts` into a
scratch in-memory database, never the tracked file) persists `correction_rule = 'T-118'` on
these rows, matching `T-118` step (3)'s own design. **`T-117`'s local guard never fires on any
of these** -- the gateway's own corrected value already arrives inside `total_concepts`'s Tier
1 slot, above `_total_is_plausible`'s floor, with nothing later in the statement to contradict
it -- meeting this task's original, narrower acceptance criterion exactly ("the gateway returns
APA's statement-level totals; `T-117`'s guard no longer rejects APA").

**A further finding, live and real, not a regression to fix:** APA's FY2021 and FY2022 10-Ks
(each filing's *own* target period, not a later filing's comparative column) now resolve
`revenue` to `None`, where `T-095`'s original 2026-09-22 fix had trusted $7,988,000,000 /
$11,075,000,000. Traced against the live payload: both were themselves *uncaught instances of
the same synthesis defect* `T-117`/`T-118` fixed for `us-gaap_Revenues`, just one concept over,
on `us-gaap_RevenueFromContractWithCustomerIncludingAssessedTax` -- the exact concept `T-095`'s
Tier 2 fallback had trusted as a clean, non-dimensional filed fact. The FY2021 10-K's own
dimensional breakdown rows for that concept sum *exactly* to $7,988,000,000
(`$6,501,000,000` "Oil and Gas, Exploration and Production" -- itself
`$3,280M` US + `$2,085M` Egypt + `$1,136M` North Sea -- plus `$1,487,000,000`
"Oil and gas, purchased"), proving it was a synthesized rollup of segment breakdowns, never a
literally-filed non-dimensional fact -- confirmed independently against SEC's own
`companyconcept` API (`data.sec.gov/api/xbrl/companyconcept/CIK0001841666/us-gaap/
RevenueFromContractWithCustomerIncludingAssessedTax.json`): no FY-period value exists under
that concept at all, only four stray zero/partial-quarter 2021 rows. `T-042`'s
`reconcile_with_filed_facts` correctly drops it (`data["corrections"]`: `rule: "T-042"`,
`reason: "no_filed_nondimensional_fact"`) rather than reconstructing a number -- `T-118`'s
revenue-specific layer has no later, smaller, contradicting total row in *these* filings to
reconstruct one from (that mechanism is what recovers FY2023-2025's revenue, not a general
segment-sum reconstruction).

This is **not a regression needing a code change here**: `fundamental_agent.quality._revenue_pos`
already HARD-quarantines a filing with `net_income` set and `revenue` `None`
(`DQ_REVENUE_POS`), the same safety net a genuinely-missing revenue concept has always hit.
Recorded here so a future audit does not mistake newly-`None` FY2021/FY2022 revenue for a new
defect -- it is the corrected, more conservative answer once the general synthesis defect is
accounted for; the old $7,988M/$11,075M values were themselves never provably real. No
production `financial_facts` are affected (the real database has not yet been re-ingested
against the redeployed gateway; that re-ingestion is part of `T-100`'s eventual full-universe
run, same as every other `metrics-v3`-era recompute).

*Step (2)* is upstream's own closed task (`portfolio-data-mining` `T-042`, PR #45/#46) --
nothing further for this repo to implement. No code, test, lint or type change accompanied
this live-data verification itself -- run against a scratch in-memory database.

**PR #104 review (`@eldova1702`), fixed 2026-09-30.** The reviewer independently ran this
PR's own code against 8 real live 10-Ks (APA, SNA, CTVA, MSFT, AEP, STZ, PG, XOM) and
confirmed every `T-042` correction found was a genuine `no_filed_nondimensional_fact` drop
(spot-checked against SEC `companyconcept`), then found three required changes:

1. **`data["reconciliation_errors"]` (upstream PR #46) was never read.** Since that PR, a
   statement whose reconciliation query itself fails comes back *rendered-but-unvalidated*
   (possibly still synthesized) rather than failing the whole response, with the failure
   named in this top-level list. Left unread, such a value would have been stored with
   `correction_rule = NULL` -- indistinguishable from genuinely filed -- exactly what this
   task exists to prevent. Fixed in `EdgarClient.financials` (`edgar_client.py`): a
   non-empty `reconciliation_errors` now raises `EdgarError` naming the statement and
   error, before `Statements.from_payload` ever sees the payload. No new plumbing needed
   downstream -- `pipeline._run_filing`'s existing per-filing failure handling already
   turns any `EdgarError` from `financials()` into an unscored, retried unit
   (`failed_units`, `analysis_run_error`), never touching `financial_facts`.
2. **`iter_facts`'s correction lookup was keyed by `(concept, column)`, not
   `(statement, concept, column)`.** The original reasoning -- "a correction's concept
   only ever appears on one statement" -- held for `T-118`'s revenue-only layer, but
   stopped once `T-042` started reconciling all three statements: a concept like
   `NetIncomeLoss` or `DepreciationDepletionAndAmortization` can appear on more than one,
   corrected independently on each. The reviewer found no live clash in their 8-filing
   sample, but fixed it for correctness -- every upstream correction entry already carries
   its own `"statement"` field (PR #45), so the fix is keying on it too.
3. **Stored facts are not replaced by a plain re-run.** `db.append_financial_facts` is
   `INSERT OR IGNORE` on `(filing_id, statement, concept, period_key, filing_version =
   accession_number)` -- re-running `fundamental_agent run` against a database that
   already has a filing's facts under its accession changes nothing, gateway redeploy or
   not. The reviewer reproduced this directly on their own local database
   (`/Users/dova/thesis/data/financial.db`): after a post-redeploy re-ingest, APA FY2023
   still stores revenue `16,558,000,000` and COGS `1,076,000,000` (both values `T-042` now
   drops) under `correction_rule = NULL`, because that accession's rows predate
   2026-09-30. This is not a bug to fix here -- it is `financial_facts`' documented
   append-only design working as intended -- but it means `T-100`'s eventual full-universe
   run **must start from a database with no pre-2026-09-30 `financial_facts` rows**, never
   one carried forward; `TASKS.md`'s `T-100` entry now says so explicitly, and the
   priority note there and in `PLAN.md` no longer points straight at Work item 8 -- the
   system-review follow-up tasks and the pilot reaching `verify_pilot` 0 FAIL come first,
   so `T-079`'s LLM re-run does not start early.

Also confirmed, non-blocking: the `T-121`-`T-124` production actions this task's own
`TASKS.md`/`CHANGELOG.md` record as "applied at the user's direction" genuinely were --
each task's own entry already carried its production-apply date and post-write
`PRAGMA quick_check` result before this review, unaffected by it.

Tests: `tests/test_edgar_client.py` (+2: a non-empty `reconciliation_errors` raises, an
empty or absent one passes through unchanged), `tests/test_pipeline.py` (+1, full-run
integration: a reconciliation failure writes no `financial_facts`/`score_snapshot` rows and
counts one failed unit), `tests/test_statements.py` (+1: the same `(concept, column)` on
two statements, only one corrected, only that one tagged). `uv run pytest -q` -- 806 passed
(was 802); `ruff check` / `ruff format --check` / `uv run mypy` -- all clean.

**`T-118` is now fully done.** See `TASKS.md`'s own entry for the task-tracking record; Work
item 14 (the second forensic audit) closes with it and moves to `CHANGELOG.md`.

## T-119 — An unscored asset silently escaped every rule check; now ineligible immediately, with a universe-wide circuit breaker

**Status**: Fixed 2026-09-29 (`T-119`, PR #78 review, found while testing `T-106`); PR #99
review (`@eldova1702`, Sourcery) found one real bug and one real design mistake, both fixed the
same day -- see "PR #99 review" below.

### Symptom

`EARNINGS_MISSING` (`cycle/rules/builtin.py::_StaleFundamentalRule`) is meant to flag a stale
FUNDAMENTAL score (SOFT), but its `if last is None or ...` branch checking for "no score at
all" was unreachable: `ctx.last_fundamental` (`cycle/data.py::last_fundamental_dates`) is built
from a SQL query that only ever includes an asset as a *key* when it has a usable FUNDAMENTAL
score at all -- an asset with zero rows is simply absent, never present with a `None` value. No
effect in production today (every one of the 20 ranked assets in the current small selection
has a score); on the full-universe run (`T-100`, 503 assets) any member that had never been
scored at all -- a new listing, a scoring failure, a gap in `fundamental_agent`'s coverage --
would rank and could be selected with no veto, no penalty, and no record of the gap at all.

### Root cause

`last_fundamental_dates`/`latest_fundamental_rows` (`cycle/data.py`) are plain `SELECT`s keyed
by `score_snapshot.asset_id` -- an asset with no row simply never appears, by SQL's own
semantics, not a defect in the query itself. `_StaleFundamentalRule.evaluate()` iterates
`ctx.last_fundamental.items()`, so an asset absent as a key is never visited at all; its `last
is None` branch was written for a value the real data-access layer never actually produces,
only for the ad-hoc test double that pre-dated this fix (`tests/test_cycle.py::test_threshold_
and_drawdown_rules` constructs `last_fundamental={..., 2: None}` by hand).

PR #78's review decision was explicit that fixing this by simply back-filling `None` for every
unscored asset in `last_fundamental_dates` -- the seemingly obvious repair -- is the wrong
mechanism: doing so would make `EARNINGS_MISSING` (a SOFT veto, `soft_veto_penalty` points off
the blended score, still eligible for selection) fire for an asset with *no* fundamental history
at all the same way it fires for one whose score has merely aged past 400 days. Those are not
the same risk: a name with a genuinely stale filing has at least once passed through
`fundamental_agent`'s deterministic ratio computation and LLM synthesis and can be reasoned
about; a name with zero FUNDAMENTAL rows has never been assessed at all, and blending it in at
a score-based discount treats "unknown" as a weak "known-and-bad," understating the actual
uncertainty. This is a portfolio-construction data-completeness policy decision, not a ratio or
GAAP question, so the citation here is to that review decision and to standard
factor-portfolio-construction practice of excluding, rather than discount-scoring, a constituent
lacking the underlying data a factor is computed from -- imputing or discount-scoring a missing
fundamental factor is exactly the kind of silent extrapolation a decision-support tool must not
present as equivalent to a real, computed score (constitution AI behavior #5).

### Fix

- `cycle/data.py`: new `unscored_assets(asset_ids, scored) -> list[int]` -- the universe's own
  key-membership diff against `last_fundamental_dates`/`latest_fundamental_rows`'s keys. New
  `TooManyUnscored(RuntimeError)` and `too_many_unscored_reason(unscored, universe_size,
  max_share) -> str | None`.
- `cycle/config.py`: new `CycleSettings.unscored_max_share: float = 0.05`, no `--allow-*`
  override -- unlike T-110/T-114/T-116's guards, this isn't a "deliberate run despite a known
  gap" case to opt into; a run that hits it needs the underlying coverage fixed, not a flag.
- `cycle/orchestrator.py::_rank`: computes `unscored = data.unscored_assets(asset_ids,
  per_type["FUNDAMENTAL"])` and raises `TooManyUnscored` immediately (before ranking/writing
  anything) when `too_many_unscored_reason` returns non-`None`. Otherwise every unscored asset's
  `cycle_ranking` row is marked `vetoed = True` with `"UNSCORED"` appended to `veto_rules` --
  written directly in this same `rank` step's own computation, never through the `veto` table or
  `hard_vetoed_as_of`/`active_soft_vetoes`'s T-1 cutoff, so it excludes the asset from
  `positions` the very cycle it's detected, not the next one. `CycleReport.unscored`/
  `unscored_tickers` record the count and names for visibility (read back from `cycle_ranking`
  after the `rank` step, correct on both a fresh run and a resumed one that skips it -- see "PR
  #99 review" below). `cli.py`'s exception tuple gets `TooManyUnscored` alongside the other
  guards, and `select`/`monitor`/`backfill` all print the unscored tickers when any exist.
- `cycle/rules/builtin.py::_StaleFundamentalRule`: docstring and `DESCRIPTION` updated to state
  its narrower, now-actually-reachable scope precisely ("a FUNDAMENTAL score exists but has aged
  past the lookback window") -- no behavior change, since its `if last is None` branch was
  already correct for the shape a hand-built `RuleContext` can carry, just unreachable via the
  real data path.

### Design decisions

- **Ineligible, not penalized (PR #78 review).** The two failure modes are kept structurally
  separate rather than merged into one rule: `EARNINGS_MISSING` (SOFT, `veto` table, T-1 lag,
  `soft_veto_penalty`) for a score that exists but is stale; the new `_rank`-level mechanism
  (immediate, no table row, no score penalty, excluded outright) for no score at all. Reusing
  `EARNINGS_MISSING`'s own SOFT-veto path for the latter would understate the difference between
  "known and old" and "never assessed."
- **Not through the T-1 lag.** Every other veto (HARD or SOFT) is detected on day *D* but only
  takes effect ranking day *D+1* (`_t_minus_1`, `hard_vetoed_as_of`/`active_soft_vetoes`'s
  cutoff) -- deliberately, so a same-day rule change doesn't retroactively exclude a name a prior
  cycle already scored. That lag doesn't apply here: an asset with no score has never been
  properly ranked at all, so there is no prior ranking decision to protect from retroactive
  change by waiting a day.
- **A universe-wide circuit breaker, not overridable.** `unscored_max_share` (default 0.05) has
  no CLI flag to bypass it, unlike T-110/T-114/T-116's `--allow-*` guards -- those exist for a
  deliberate run despite a *known*, already-understood gap (stale prices, a dirty tree, an
  un-migrated gate version); a universe suddenly missing FUNDAMENTAL coverage on more than 5% of
  its members is itself the anomaly to investigate, not a state to run through.

### PR #99 review

- **(blocking, `@eldova1702`) The whole-universe exemption inverted the safeguard.** The first
  pass exempted a cycle from `TooManyUnscored` when *every* universe member lacked a FUNDAMENTAL
  score, reasoning that a cycle dated before any filing is public yet (T-106/T-107) legitimately
  has zero coverage and there is no scored peer to be missing relative to. Reproduced live
  against `cycle_seed`: deleting 1 of 5 assets' FUNDAMENTAL score raised `TooManyUnscored`
  (1/5 = 20% > 5%) as intended, but deleting all 5 ran to completion and built a portfolio on
  TECHNICAL/VALORIZATION alone (AAA 0.639, BBB 0.333, CCC 0.027) with zero `UNSCORED` marks and
  no warning -- silently the opposite of what the guard exists to prevent. PR #78's own decision
  was literal: "more than 5% of the universe is unscored" -- 100% is more than 5%, and the fix's
  invented carve-out for it was never in that decision; the "before any filing is public" case
  is `EARNINGS_MISSING`/`T-106`/`T-107`'s territory (compute on whatever else is available), not
  a reason to exempt *this* guard, which exists specifically to catch exactly this shape of
  total-coverage loss regardless of its cause. **Fixed**: dropped the `len(unscored) >=
  len(asset_ids)`/`>= universe_size` early-outs from `unscored_assets` and
  `too_many_unscored_reason` -- both now treat 100% the same as any other share over the limit.
  `tests/test_point_in_time_readers.py::test_a_cycle_before_the_filings_are_public_sees_no_
  fundamentals` updated to expect `TooManyUnscored` on the two pre-filing dates (its
  `normalize`/`veto`-step assertions still hold -- both steps run and are checkpointed before
  `rank` raises); new `test_all_unscored_also_refuses_the_selection_cycle` (`tests/test_cycle.
  py`) reproduces the exact scenario above and asserts zero `portfolio_position` rows result.
- **(`@eldova1702`) List the unscored tickers in the cycle output, not just a count.**
  `CycleReport.unscored` was a bare count; the `select`/`monitor`/`backfill` CLI summary lines
  never printed it at all, so an excluded asset was invisible without a direct `cycle_ranking`
  query. **Fixed**: new `CycleReport.unscored_tickers: list[str]`; `cli.py`'s new
  `_print_unscored` prints `"N unscored (ineligible): TICKER, TICKER, ..."` after each of
  `select`/`monitor`/`backfill`'s existing summary lines, whenever `unscored > 0`.
- **(Sourcery, `broader_impact`) `report.unscored` misreported 0 on a resumed run.** `_rank`'s
  own local `unscored` list only exists when that step's closure actually runs; `_do` skips an
  already-`done` step entirely on resume (T-097's own resume contract), so a resumed run whose
  `rank` step had already completed left `report.unscored` at its dataclass default (0) even
  though the persisted `cycle_ranking` still carried `UNSCORED` rows. **Fixed**: `report.
  unscored`/`unscored_tickers` are now populated by one query against `cycle_ranking` (joined to
  `assets` for tickers, filtered by `json_each(veto_rules_json)` for `'UNSCORED'`) placed right
  after `_do("rank", _rank)` returns -- unconditionally correct whether that call executed
  `_rank` or skipped it, since it reads persisted state either way rather than a step-local
  variable. New `test_unscored_report_field_survives_resume` (`tests/test_cycle.py`): a second
  `run_selection` call over the same cycle date, with `"rank" in again.steps_skipped` asserted
  alongside `again.unscored == 1` and the correct ticker.

### Verification

- `tests/test_cycle.py` (+10 net across both review rounds):
  `test_unscored_assets_diffs_universe_against_scored_keys`,
  `test_unscored_assets_includes_every_asset_when_none_is_scored`,
  `test_too_many_unscored_reason_pure_function` (pure-function unit tests);
  `test_unscored_asset_ineligible_immediately_not_through_t1_lag` (`cycle_seed`, 5 assets, one
  score deleted, `unscored_max_share` relaxed to 0.5 so the run completes: the affected asset's
  `cycle_ranking` row is `vetoed = 1` with `veto_rules_json == ["UNSCORED"]` on the *same* cycle
  date, no `veto` table row, excluded from `portfolio_position`, `report.unscored_tickers ==
  ["BBB"]`); `test_unscored_report_field_survives_resume`; `test_print_unscored_names_the_
  tickers` / `test_print_unscored_silent_when_none`; `test_too_many_unscored_refuses_the_
  selection_cycle` / `test_monitor_also_refuses_too_many_unscored` (1/5 = 20%, over the 5%
  default, both cycle types raise `TooManyUnscored`, `cycle_run.status == 'failed'`);
  `test_all_unscored_also_refuses_the_selection_cycle` (PR #99 review, above).
- Regression: `tests/test_point_in_time_readers.py::test_a_cycle_before_the_filings_are_
  public_sees_no_fundamentals` updated (not merely re-passing unmodified) to assert
  `TooManyUnscored` on the two pre-filing dates, per the PR #99 correction above; its
  `normalize`/`veto` checkpoint assertions are unaffected, since both steps complete before
  `rank` raises.
- `uv run pytest -q` -- 779 passed. `ruff check` / `ruff format --check` / `uv run mypy` -- all
  clean.

---

## T-125 — Veto lifecycle: a per-cycle-date event model made a HARD veto permanent and double-counted a held SOFT one

**Status**: Code done 2026-09-29 (`T-125`, P0, added 2026-09-27 from PR #94's review). Production
`migrate` (the schema rebuild, `m009`) is **deferred, pending explicit user direction** -- the
same category as every other pending production action in `TASKS.md` (`T-121`-`T-124`).

### Symptom

Verified directly against a read-only copy of production (`data/financial-2.db`, schema floor 6,
via `KG_FINANCIAL_DB`), 2026-09-29 -- 19 `veto` rows total, all `cleared_at IS NULL`:

- **WAT** (`asset_id` 486) carries a HARD `NEGATIVE_FCF` row dated `cycle_date = 2026-06-30`
  (from the backdated run T-104 later reverted) with no later row at all for that
  `(asset, rule)` pair -- i.e. the 2026-09-22 cycle re-evaluated WAT and found the condition no
  longer breached, but the code has no way to record that: it still reads as an active HARD veto
  today, permanently, unless someone notices and hand-deletes the row.
- **9 other `(asset_id, rule_id)` pairs** -- APA/T/STZ/NEE/PM/PG/SBAC's `LIQUIDITY_DISTRESS`
  (SOFT) and MA/SBAC's `LEVERAGE_EXTREME` (HARD) -- each carry **two** separate rows, one dated
  `2026-06-30` and one `2026-09-22`, both still uncleared. Reading the old
  `active_soft_vetoes`/`hard_vetoed_as_of` predicate (`cycle_date <= cutoff AND cleared_at IS
  NULL`) at a projected next cycle's T-1 cutoff (`2026-09-28`) returns **both** rows for each of
  the 7 SOFT-vetoed names -- `soft_veto_penalty * 2` (30 points at the default 15/rule) instead
  of `* 1` -- confirming the "4 weekly cycles = -60, not -15" shape the PR #94 review described,
  generalized to any name whose SOFT condition is simply still true across cycles.

### Root cause

`veto` (`src/kg_schema/ddl.py`) keyed each row on `(asset_id, rule_id, cycle_date)` -- a per-date
*event*, not a *condition*. `writers.write_vetoes` (`src/cycle/writers.py`) only ever cleared
rows `WHERE cycle_date = ?` for the run's own date, so a prior date's row was never touched by a
later run: a HARD hit, once written, had no code path that could ever set its `cleared_at`
again, and a condition true on N separate cycle dates produced N separate rows, each read as an
independent, simultaneously-active hit by `hard_vetoed_as_of`/`active_soft_vetoes`'s
`cycle_date <= cutoff` scan. Neither reader nor writer had any notion of "the same veto,
continuing" versus "a new one" -- exactly the shape `portfolio_position` had before T-104
introduced `valid_from`/`valid_to` stints for the identical reason (a position, once opened,
needs a record of when it *closed*, not a fresh row every time it's still held).

### Theoretical/technical reference

This is a data-modeling correctness question, not a ratio/GAAP one: representing "a condition
that holds over an interval of time, with a definite start and (if any) end" is the standard
slowly-changing-dimension "Type 2" pattern (Kimball & Ross, *The Data Warehouse Toolkit*, 3rd
ed., ch. 5) -- an open-ended validity interval per state change, one row per interval, never
mutated in place except to close it. This repo already adopted exactly that shape for
`portfolio_position` (T-104, `valid_from`/`valid_to`) and `portfolio_position_replay` (T-115);
`veto`'s `raised_on`/`cleared_on`/`last_seen_on` is the same pattern applied to the same repo's
own veto-lifecycle gap PR #94's review identified, not a new technique.

### Fix

- **Schema** (`src/kg_schema/ddl.py`): `veto` now holds stints -- `raised_on`, `cleared_on`
  (`NULL` = open), `last_seen_on` (cycle dates); `detected_at`/`cleared_at` stay as wall-clock
  metadata only, never read by the point-in-time predicate. `UNIQUE (asset_id, rule_id,
  raised_on)` plus a partial unique index (`ux_veto_open ... WHERE cleared_on IS NULL`) enforce
  at most one open stint per `(asset_id, rule_id)` -- a re-raise after a clear opens a new,
  distinct stint. The two new indexes name the new columns, so they cannot ship inline in
  `ADDITIVE_DDL` the way the table itself can (a pre-T-125 database's `CREATE TABLE IF NOT
  EXISTS veto` safely no-ops against the old shape, but an unconditional `CREATE INDEX` naming
  a column that shape lacks would abort `kg_schema.ensure`'s additive path outright, which must
  stay safe to run anytime, migrated or not) -- `kg_schema._ensure_veto_indexes` creates them
  only once `raised_on` actually exists (a fresh database, or a migrated one).
- **Three-state evaluation** (`src/cycle/rules/base.py`, `builtin.py`): `Rule.evaluate()` now
  returns a `RuleResult(hits, evaluated)` -- `evaluated` is the `frozenset[int]` of asset_ids the
  rule could actually resolve this cycle, hit or not, distinguishing "evaluated, no longer hit"
  (clears an open stint) from "couldn't tell this cycle, e.g. no filing yet" (leaves an open
  stint untouched, never opens a new one). `RuleResult` iterates as its own `hits`, so every
  existing `for h in rule.evaluate(ctx)` call site (tests included) keeps working unchanged.
  Each of the 5 built-in rules defines its own evaluated set from what it actually reads:
  `_ThresholdRule`/`_LeverageRule` by the deciding metric being non-`None`; `_DrawdownRule` by
  `max_drawdown_90d` being non-`None`; `_StaleFundamentalRule` by asset membership in
  `ctx.last_fundamental` (T-119: only ever populated for a scored asset); `_DataQualityRule` by
  a non-empty `ctx.metrics` entry (the same usable-filing set Ring-1 gates against).
- **Transitions + idempotent re-run** (`src/cycle/writers.py::write_vetoes`): takes `hits`,
  `evaluated` (`dict[rule_id, frozenset[asset_id]]`), and `disabled_rule_ids`. Before applying
  anything, it undoes *this cycle_date's own* prior transitions (deletes stints it raised,
  reopens stints it cleared), then reapplies: hit + no open stint -> open; hit + open stint ->
  bump `last_seen_on` (and `severity`, which can flip between SOFT/HARD run to run for
  `LEVERAGE_EXTREME`'s negative-equity branch); evaluated, not hit, open stint -> close; a rule
  disabled in `rule_catalog` -> close its open stints too, via `cycle.rules.disabled_rule_ids`
  (a disabled rule is never asked to evaluate, so nothing else would ever clear it).
- **One shared point-in-time predicate** (`src/kg_schema/queries.py`): `hard_vetoed_as_of` /
  `active_soft_vetoes` -- `raised_on <= cutoff AND (cleared_on IS NULL OR cleared_on > cutoff)`
  -- replace two independent copies (`cycle.writers`, and `quant.db`'s own "copied ... to avoid
  importing cycle"). `active_soft_vetoes` now returns one entry per open stint, so
  `orchestrator._rank`'s existing `soft_veto_penalty * len(soft.get(a, []))` is "once per open
  stint" for free, with no formula change needed.
- **Out-of-order guard** (`kg_schema.queries.veto_out_of_order_reason`, wired into
  `cycle.orchestrator._run`): mirrors T-097's `portfolio_position` guard -- refuses a
  `cycle_date` older than the latest transition recorded anywhere in the shared `veto` table
  unless `--allow-backdated-veto` (`CycleSettings.allow_backdated_veto`, new CLI flag on
  `select`/`monitor`). Same date as the latest transition is safe (the ordinary idempotent
  same-date re-run above).
- **Replay reset** (`src/cycle/replay.py::reset_replay_range`, T-115's `--force`): now also
  deletes stints raised on/after `--from` and reopens stints cleared on/after `--from`, mirroring
  the existing `portfolio_position_replay` void/reopen pair -- `veto` is one table shared between
  live and REPLAY runs (unlike `portfolio_position`/`portfolio_position_replay`), so a REPLAY at
  an older date needs the same reset before it, not a separate override flag.
- **Migration** (`src/kg_schema/migrations.py::_m009_veto_stints`): collapses the old
  per-`(asset, rule, cycle_date)` hit rows into stints, using the set of cycle dates the "veto"
  checkpoint step actually completed on (any `cycle_run`, any `cycle_type`) as the evaluation
  timeline: a stint opens on a pair's first hit date, extends across consecutive hit dates, and
  closes at the first evaluation date with no hit row for that pair -- reopening a new stint if
  hit again later. Registered as `MIGRATIONS`' version 9.

### Design decisions

- **`RuleResult` iterates as its own hits, deliberately.** Changing `evaluate()`'s return type
  outright would have forced touching every existing rule-level test (`test_threshold_and_
  drawdown_rules` and the six `_LeverageRule` tests) for no behavioral reason -- they only ever
  consume the hits, never an evaluated set. Making the return value iterable keeps every one of
  those call sites correct unchanged, per constitution's "prefer the smallest change consistent
  with the existing pattern."
- **`_DataQualityRule`'s evaluated set is `ctx.metrics`, not `ctx.data_quality`'s own keys.**
  `ctx.data_quality` is seeded by the orchestrator only for assets *with* a HARD issue
  (`{a: dq.hard[a] for a in asset_ids if a in dq.hard}`), so its keys are already the hit set,
  not a could-evaluate set -- an asset Ring-1 gated and found clean never appears there at all.
  Ring-1 runs against the same usable-filing set `latest_metrics` populates, so a non-empty
  `ctx.metrics[aid]` is the correct proxy for "this asset's latest filing was gated this cycle."
- **Veto stays one shared table between live and REPLAY runs -- not mirrored into a
  `veto_replay` table.** T-115 fully isolated `portfolio_position` into a parallel
  `portfolio_position_replay` table; this task's own text only asks `reset_replay_range` to also
  undo veto transitions, not to isolate the table itself, and `cycle backfill`'s `--db` is
  already required to be a throwaway copy of the database for exactly this reason (score_
  snapshot, veto and cycle_ranking are all acknowledged, pre-existing non-isolated writes --
  `cli.py::_BACKFILL_DB_HELP`). Full isolation would need a second physical table (and would
  conflict with (a)'s single global "at most one open stint" constraint, which a two-stream
  design can't honor without weakening it); out of this task's scope.
- **The out-of-order guard applies uniformly to live and REPLAY runs, with different escape
  hatches.** `test_replay_never_conflicts_with_an_existing_live_book` (T-115) had assumed a
  REPLAY never conflicts with anything the live book has done, true for `portfolio_position` but
  never actually true for `veto` (see the point above) -- updated to reflect that a REPLAY whose
  date is older than an existing veto transition needs the same `--allow-backdated-veto`
  override a live run would (in production, `--force`'s now-extended reset is the intended path,
  since `--db` there always names a copy, not the live database).

### Verification

- Real-data before/after, on a scratch copy of `data/financial-2.db` (never the tracked file;
  copy discarded after use) run through `kg_schema.ensure(conn, run_migrations=True)`:
  - **Before** (raw per-date rows): 19 rows; WAT's HARD `NEGATIVE_FCF` shows as active at any
    cutoff on/after `2026-06-30`, forever; at a projected `2026-09-28` cutoff, 7 SOFT names each
    contribute 2 rule-id entries (`penalty x 2`).
  - **After migration**: 10 stints. WAT's `NEGATIVE_FCF` stint is `raised_on = 2026-06-30,
    cleared_on = 2026-09-22` -- correctly inactive from 2026-09-22 on. The other 9 pairs
    collapse to one open stint each (`raised_on = 2026-06-30, last_seen_on = 2026-09-22`). At the
    same `2026-09-28` cutoff, `kg_schema.queries.active_soft_vetoes` now returns exactly one
    rule-id per name (`penalty x 1`), and `hard_vetoed_as_of` returns `{MA, SBAC}` only -- WAT
    correctly excluded.
- New tests (+9, `uv run pytest -q` 794 passed, was 785): `tests/test_cycle.py` --
  `test_rule_result_iterates_as_its_hits`,
  `test_write_vetoes_opens_extends_then_clears_a_hard_stint` (open -> extend -> clear -> re-raise
  as a new stint, plus the point-in-time predicate either side of the clear date),
  `test_write_vetoes_soft_stint_penalizes_once_not_per_cycle_held`,
  `test_write_vetoes_leaves_open_stint_untouched_when_asset_not_evaluated`,
  `test_write_vetoes_disabled_rule_closes_its_open_stints`,
  `test_write_vetoes_same_date_rerun_is_idempotent`,
  `test_veto_out_of_order_reason_pure_function`,
  `test_reset_replay_range_also_undoes_veto_transitions`; `tests/test_kg_schema.py` --
  `test_m009_collapses_per_date_veto_hits_into_stints` (the exact WAT/re-raise shape found in
  production above, hand-built at schema floor 8, asserting the resulting stints and that the
  migration is idempotent).
- Updated (schema-shape or guard-interaction, not behavior-under-test):
  `test_t_minus_1_hard_veto_excludes_asset`,
  `test_hard_veto_detected_via_rules_excludes_asset_starting_next_cycle`,
  `test_a_cycle_before_the_filings_are_public_sees_no_fundamentals`,
  `test_gate_drops_illiquid_short_and_vetoed`,
  `test_benchmark_gate_keeps_a_hard_vetoed_name_a_book_would_drop`,
  `test_evaluate_s_benchmark_panel_keeps_a_hard_vetoed_name_a_book_would_drop`,
  `test_select_refuses_to_write_a_backdated_book`,
  `test_allow_backdated_cannot_end_newer_open_positions`,
  `test_allow_backdated_overrides_the_guard_and_records_it`,
  `test_replay_never_conflicts_with_an_existing_live_book`,
  `test_m008_backfills_every_row_and_restores_the_guards`,
  `test_migrations_rebuild_and_preserve_rows` (schema floor 8 -> 9).
- `ruff check` / `ruff format --check` / `uv run mypy` -- all clean.

### Residual scope / deliberately deferred

- **Production `migrate`** (the real `data/financial.db`'s `m009` rebuild) is deferred pending
  explicit user direction, same as `T-121`-`T-124`.
- **Veto is still not fully isolated between live and REPLAY runs** -- see "Design decisions"
  above. A future full isolation (a `veto_replay` table) is a larger, separate change, not
  something this task's own acceptance criteria called for.

### PR #103 review follow-up (2026-09-30) -- 5 required fixes

A human review of PR #103 (T-125's own PR) found five bugs in the initial implementation above;
two overlapped with `sourcery-ai`'s inline findings on the same PR (`src/cycle/writers.py:175`,
`src/cycle/replay.py:151`). All five are fixed here, each verified against a scratch copy of
`/workspaces/thesis/data/financial.db` (never the tracked file; copy discarded after use) --
schema floor 8, the same un-migrated shape (19 `veto` rows, all `cleared_at IS NULL`) the
original T-125 verification used.

1. **`--force`'s replay reset left a stale `last_seen_on` behind.**
   `cycle.replay.reset_replay_range`'s void/reopen pair only ever touched
   `raised_on`/`cleared_on` -- a stint raised *before* `date_from` but extended (`last_seen_on`
   bumped forward) by a hit on or after it survived untouched, itself a transition dated on/after
   `date_from` that immediately tripped `veto_out_of_order_reason` again on the very redo `--force`
   exists to unblock. Fixed by also running `UPDATE veto SET last_seen_on = raised_on WHERE
   raised_on < ? AND last_seen_on >= ?` in the same reset. `veto_out_of_order_reason` itself keeps
   reading `last_seen_on` unchanged -- only the reset needed the fix.
2. **The veto out-of-order guard ran too early, blocking a plain resume.** It sat at the top of
   `cycle.orchestrator._run`, before `done_steps` was even read, so re-invoking `cycle backfill
   --from F --to T` after it was killed refused outright at `F` even when every step there
   (`veto` included) was already `done` and no write would happen. Moved inside the `_veto()`
   step closure -- mirroring exactly where the T-097 positions guard already lives inside
   `_positions()` -- so it fires only when the step is actually about to execute.
3. **`--allow-backdated-veto` rewrote veto history instead of replaying it.** `write_vetoes` only
   ever undoes/redoes *its own* cycle_date's rows (the idempotent-same-date-rerun design, T-125 f);
   called at an older, already-superseded date under the override, it instead deleted the stint(s)
   raised on the true latest date and rolled a still-open earlier stint's `last_seen_on` back --
   reproduced case: `hard_vetoed_as_of` went from `{asset}` to `set()` for a cutoff between the
   backdated date and the (now-deleted) later transition, i.e. an asset retroactively stopped
   reading as vetoed. Fixed by making the override read-only: `_veto()` still evaluates the rules
   (so `vetoed`/the CLI's hard-veto count reflect today's conditions) but returns before calling
   `write_vetoes` whenever the guard was bypassed. `_rank` already reads existing stints
   point-in-time through `hard_vetoed_as_of`/`active_soft_vetoes`, so ranking stays correct with
   nothing written.
4. **The veto predicates silently misread a pre-`migrate` database as vetoless.**
   `hard_vetoed_as_of`/`active_soft_vetoes`/`veto_out_of_order_reason` each caught a bare
   `DatabaseError` to handle "no `veto` table yet" -- which also swallows "`veto` exists but has
   no `raised_on` column" (the exact state production sits in until `migrate` runs, deliberately
   deferred by this same PR) the same way, returning an empty/`None` result instead of an error.
   `cycle.writers.write_vetoes` had the opposite problem: no guard at all, so it crashed on the
   same missing column with a raw `DatabaseError` traceback instead of an actionable message
   (`sourcery-ai`'s own finding on `writers.py:175`). Both fixed by one shared helper,
   `kg_schema.queries.require_veto_stint_columns`: `False` when `veto` doesn't exist yet (every
   caller's existing empty default), raises the new `kg_schema.queries.VetoSchemaStale` when it
   exists but predates `m009`, naming `migrate` as the fix -- the same pattern `StaleGateVersion`
   already uses for a stale Ring-1 gate version. Wired into `cycle`'s and `quant`'s CLI exception
   handlers (`cycle.cli.main`, `quant.cli._run_build_risk_model`/`_run_optimize`) alongside the
   other guard exceptions, so it prints one line and exits 1 rather than a traceback.
5. **`_m009_veto_stints` read a same-date-corrected row as a hit.** The pre-T-125 writer set
   `cleared_at` on a row when a same-date re-run no longer hit that `(asset, rule)` pair (`ON
   CONFLICT ... cleared_at = NULL` on a fresh hit, left set otherwise) -- such a row's `cycle_date`
   was *not* a hit in that date's final, persisted verdict, but the migration's `old_rows` query
   read every row regardless, so it would have opened or extended a stint from a veto that was
   never actually active on that date. Fixed with a `WHERE cleared_at IS NULL` filter (the date
   still counts as an evaluation date through `cycle_checkpoint`, so the pair still closes
   correctly on it). **Verified count on the real (scratch) database: 0 such rows** -- this PR's
   `m009` run against it is behaviorally unchanged (same 10 stints, same WAT `NEGATIVE_FCF` close
   on `2026-09-22` as the original T-125 verification above); the fix only matters for a database
   that does carry a same-date correction, none of which exist in this one.

**New tests (+5, `uv run pytest -q` 799 passed, was 794):** `tests/test_cycle.py` --
`test_veto_guard_does_not_block_resuming_an_already_completed_date` (fix 2: two `run_replay`s
then a resume of the earlier date must not raise), `test_allow_backdated_veto_is_read_only_not_a_
history_rewrite` (fix 3: the override runs the step but leaves `veto` byte-for-byte unchanged),
`test_reset_replay_range_rolls_back_last_seen_on_past_date_from` (fix 1),
`test_veto_predicates_raise_a_clear_error_against_a_pre_m009_veto_table` (fix 4: all four of
`hard_vetoed_as_of`/`active_soft_vetoes`/`veto_out_of_order_reason`/`write_vetoes` against a
hand-built old-shape `veto` table); `tests/test_kg_schema.py` --
`test_m009_ignores_a_row_the_old_writer_had_already_cleared_same_date` (fix 5).
`ruff check` / `ruff format --check` / `uv run mypy` -- all clean.

---

## T-128 — A revenue `total_concepts` tag is rejected as a duplicated dimensional slice, not by a 50% magnitude floor

**Status**: Fixed 2026-10-01 (branch `fix/work-item-16-fine-tuning-followups`, `T-128`). Production is
recomputed by `T-100` (still `metrics-v2` there).

### Symptom

`fixes_feedback.md` §4.2 swept `T-095`'s floor over the full universe (stored facts, 985
income-statement totals checked) and found it rejects 5 legitimate totals besides the defect it was
calibrated on: EQT FY2021 (total $3,064.7M vs largest component $6,804.0M, ratio 0.45), EQT FY2024
(0.35), EQT FY2025 (0.36), EXE FY2022 (0.42) and EXE FY2024 (4,235M / 8,518M = 0.497, a hair under
the 0.5 floor). Natural-gas producers report
revenue net of derivative settlement losses, so the top line is legitimately far below gross sales.
Rejecting the total sends `Statements.get("revenue")` to Tier 2, which returns the *gross* sales
component — a revenue overstated 1.5-3x, and every margin built on it understated by the same factor.

### Root cause — re-derived against the filings, and one earlier claim corrected

The floor is a *magnitude* proxy for "this total is a mis-promoted slice". The actual defect shape
(`T-095`, APA FY2021) is structural. Re-derived from APA's FY2021 10-K inline-XBRL instance
(accession `0001784031-22-000009`, `apa-20211231.htm`, read from `sec.gov/Archives/edgar`,
2026-10-01): `us-gaap:Revenues` is filed for FY2021 **only** with the dimension
`EquityMethodInvestmentNonconsolidatedInvesteeAxis` ($1,082M); there is no un-dimensional
`us-gaap:Revenues` fact. The gateway's un-dimensional row is therefore a dimensional slice
presented as a total, and its payload carries both rows with the identical value.

**This corrects `T-095`'s and `T-117`'s statement that APA "has never filed a `us-gaap:Revenues` fact in
any context".** That was checked against SEC's `companyfacts`/`companyconcept` APIs, which expose
only facts without dimensions; the dimensional fact exists in the filing. The conclusion that the
un-dimensional row is not a filed fact stands, and is what makes the structural test sound.

The five false positives were checked the same way (inline XBRL, FY facts of `us-gaap:Revenues`):
EQT FY2021 (`eqt-20211231.htm`), FY2024, FY2025; EXE FY2022 (`chk-20221231.htm`), FY2024. In every one
the total exists un-dimensionally and **no dimensional `us-gaap:Revenues` row carries its value**
(EQT FY2025's dimensional rows are 565M, 9,898M, 572M, 1,301M, 8,024M and -1,254M against a total of
8,644M). They are genuine totals.

### Theoretical/technical reference

- XBRL Dimensions 1.0 (xbrl.org; no stable URL could be confirmed from here): a fact with a
  dimension qualifies a *portion* of an entity's activity; the same concept without dimensions is the
  consolidated value. FASB ASC 280-10-50 (segment reporting) for why a segment disclosure may
  legitimately equal the whole. A total that is *equal to one slice and smaller than a named component* cannot be the
  consolidated aggregate.
- Why the net total is below gross sales: these registrants present derivative gains and losses
  inside the revenue section of the income statement (observed in EQT's FY2021 10-K: sales of natural
  gas, NGLs and oil $6,804M, total operating revenues $3,065M), not a tagging defect.

### Fix (`fundamental_agent/statements.py`, `Statements`)

`_total_is_plausible` now rejects a Tier 1 `total_concepts` match only when **both** hold:
1. a named `spec.concepts` component is **larger** than the total (a real aggregate is never smaller
   than its own component) — this is the T-095 premise kept, minus the percentage; and
2. the total **exactly duplicates a dimensional row** of the same concept in the same column
   (`_duplicates_a_dimensional_slice`, `math.isclose` at 1e-9), on an axis that is not whole-entity.

`_TOTAL_PLAUSIBILITY_FLOOR` is removed. Tier 1/Tier 2 resolution, `_label_total_correction` (T-117)
and every other path are unchanged.

### Design decisions

- **Whole-entity axes are exempt** (`srt:ConsolidationItemsAxis`,
  `us-gaap:StatementBusinessSegmentsAxis`). A single-segment filer's segment revenue *is* its total,
  so the twin is expected there, not evidence. Without the exemption, a single-segment gas producer
  with hedging netted below gross sales would be rejected again — the false positive being fixed.
  APA's twin is on the equity-method-investee axis, so it is still caught.
- **Conjunction with "below a component"**, not the twin alone: a single-product filer legitimately
  has a twin on a product axis, and its total is never below its own component.
- **Residual risk**: only the `dimension_axis` the gateway reports is inspected (a row with several
  axes carries one). A duplicate hidden behind a multi-axis row is not seen — it fails open (trusts the
  total), the safe direction for a gate that "only ever rejects".

### Verification

- **Real filings** (above): APA FY2021 has the twin and is rejected → $7,988M; the five EQT/EXE
  totals have no twin → kept at $3,064.7M / $5,273.3M / $8,644.2M / $11,743M / $4,235M. The gateway
  was not reachable from this environment, so the payload was reasoned from the instance rather than
  replayed; the unit tests build the gateway's documented row shape (`dimension`, `dimension_axis`),
  taken from the captured `tests/fixtures/financials_*.json`.
- `tests/test_statements.py`: the APA test now carries the equity-method twin; the 0.5/0.49 boundary pin
  is replaced by EQT-shaped totals at ratios 0.45 and 0.27 that are trusted, a duplicate at 0.9994 of
  its component that is rejected, and three guards (whole-entity axis, total not below a component, twin in a
  different column or off by 0.02%). Mutation-checked: never rejecting fails the APA, 0.9-ratio and
  utility tests; dropping the axis exemption fails the whole-entity test; dropping the
  "below a component" condition fails the single-product test. `uv run pytest -q`, `ruff` and `mypy` green.
- Every existing F2/T-095/T-117 test and the captured fixtures (`jpm_10k`, `aapl_10k`, XEL, NEE) pass
  unchanged.

### Residual scope, deliberately deferred

- **Production re-persist** — `T-100`'s full recompute. EQT/EXE are not in the 20-asset sample.
- **The gateway's `corrections` list (T-118)** is an independent, upstream guard; this is the
  defence-in-depth on the consuming side.


## T-131 — Price ingestion integrity: a partial session, windowed observations and a half-adjusted split were all stored permanently

**Status**: Fixed 2026-10-01 (branch `fix/t131-price-ingestion-integrity`, `T-131` (a)-(d)). Production is
**not** repaired by this change: its clean-up (below) is a separate action at the user's direction, and
`T-100` starts from a fresh database.

### Symptom

From `docs/md primera revision/system_review_2026-09-29.md` N1-N3, re-measured on production
`financial.db` (read-only) on 2026-10-01:

- **N1 — partial session.** Runs on 2026-09-29 at 14:04-14:10 UTC (10:04 ET) stored that day's bar. Total
  volume across the 503 names is **362,731,192** against **2,940,224,500** (09-28) and 2,684,015,824 (09-25).
  `price_daily` self-heals on the next fetch; `price_observation` and `quant_return_daily` were
  `INSERT OR IGNORE`, so the partial day is permanent in the benchmark, the evaluation rows and risk model 3.
- **N2 — windowed observations.** `build_observations` saw only the candles of that run. T-122's refresh used
  `--start 2026-08-20`, leaving `realized_vol_90d`, `momentum_252d`, `max_drawdown_90d` and `momentum_63d` NULL on
  **10,500 rows** (500 assets, every observation since 2026-08-31 that has more than 260 stored bars).
- **N3 — split seam.** **7** split-shaped day-over-day jumps in `price_daily`: APH x0.491 on 2026-08-20 (2:1 split
  2026-09-03; the refresh start) and six on MNST (x0.489, x1.956, x0.493, x1.941, x0.498, x0.504 between 2026-07-20 and
  2026-08-11; 2:1 split 2026-08-11), the stored closes alternating between the two bases.
- **(d)** `quant.universe._history_counts` / `_median_dollar_volume` counted `quant_return_daily` /
  `price_observation` rows with no `engine_version` filter, so a second version doubles every history.

### Root cause

1. `pricing_agent` accepted `--analysis-date` = today at any hour and stored whatever the gateway returned.
2. `_store` built observations from the fetched `candles`, not the stored history, and wrote them with
   `INSERT OR IGNORE`, so the first (wrong or incomplete) value was kept for good.
3. The gateway returns history split-adjusted *as of the fetch date*. A refresh that rewrites only its window leaves older
   stored rows on the previous basis; nothing compared the two.
4. The readers took `COUNT(*)` over a table keyed by `engine_version`.

### Theoretical/technical reference

- NYSE, *Holidays & Trading Hours*: the core session runs 9:30-16:00 ET (13:00 on the few early-close days). A bar read before
  the close is a partial one. The guard uses 16:00 ET plus a 1-hour buffer for the vendor's volume to settle and treats an
  early-close day as a full one (it only ever waits longer).
- A split changes the price level by its ratio and nothing else, so a correctly adjusted series has no day-over-day move of
  exactly that ratio at a split date. The jump ratios checked (x2, x0.5, x3, x1/3, x4, x1/4, x10, x0.1, +-3%) are the set
  `verify_pilot.py` counts, so the guard and the pilot acceptance agree.
- Windowed estimators (Wilder ATR, a 90-day realized volatility, a 252-day momentum) are defined on the series *before* the
  date, so they can only be computed from the full history -- the same reasoning as T-105's TTM.

### Fix

- **(a)** `kg_schema.trading_calendar.session_final_at / session_is_open_or_pending / last_final_session` (US DST computed by
  rule, no tz database) and `pipeline._require_closed_session`: `run` raises `SessionNotClosed` for a trading-day end whose bar is
  not final, before the DB is opened. `rundate.now()` is the clock seam.
- **(b)** `_store` builds observations from `db.load_daily_candles` (the full stored history up to the end date, read back after
  the write), and `upsert_price_observations` is now `ON CONFLICT ... DO UPDATE ... WHERE <a value differs>`: an identical
  recompute touches no row, a changed input rewrites the row.
- **(c)** `_reconcile_history` (only with `--store-daily`): when a recorded SPLIT postdates a bar written before it
  (`db.has_pre_split_rows`, by `price_daily.ingested_at`), or the stored-plus-fetched series has a split-shaped jump at the run's
  seam that matches a recorded split, the asset's full history is re-fetched **once** (the stamps move past the ex-date, so it
  does not repeat). A jump that survives, or that no recorded split explains, makes `_SplitJumpRefused`: the ticker fails
  (`pricing_run_error.stage = 'split_jump'`) and **nothing** is written for it. `--allow-split-jumps` overrides. Only jumps
  involving a bar the run fetched count. `quant.db.upsert_return_daily` follows the same update-on-change rule, so
  `quant build-returns` rebuilds a returns series after a re-adjustment (it was `INSERT OR IGNORE`).
- **(d)** the two readers filter by a **pinned** `engine_version`, as every other quant reader does (panel, benchmark, evaluate):
  `QuantSettings.return_engine_version` for returns (`persist.py` passes the resolved manifest version, which is what the panel
  reads under `--returns-version`) and a new `QuantSettings.observation_engine_version = "priceobs-v1"` for observations (quant
  cannot import `pricing_agent`'s constant). First version of this fix read the `v_*` views, which resolve "latest per day" by
  `computed_at`; PR #109's review showed that can count a different series than the panel is built from, and that the rewrites
  this change introduces bump `computed_at`, so "latest" could flip per row after a rebuild.

### Design decisions

- **Refuse, don't clamp.** Clamping a today-before-close end to the last final session would write a different date than the
  caller asked for. The error names the latest final session. Consequence: the *default* `--analysis-date` (today, UTC) is refused
  for most of a trading day; the pilot already uses an explicit `D`.
- **The jump is not accepted just because a split is recorded.** A recorded split changes the *remedy* (re-fetch once), not the
  verdict: a jump left after the re-fetch is still a mis-adjusted series.
- **`INSERT OR IGNORE` -> update-on-change for the two derived series** weakens NR-007's "never overwritten" for them only
  (amended in `SPEC.md`): they are pure functions of stored prices, so "unchanged input -> unchanged row" is the invariant
  that matters, and a frozen first value is exactly what made N1-N3 permanent.
- **The first run after this change re-fetches every asset that has a split in its history once** (577,325 production bars have
  no `ingested_at`, 69 recorded splits would trigger). It is a one-off per asset, not per run.

### Verification

- **Production `financial.db`, read-only, 2026-10-01**: the detector finds exactly the 7 jumps (APH 1, MNST 6), and each ratio matches
  the recorded 2:1 split of its asset; a full recompute of the 10,500 NULL observation rows over the stored history leaves **0** NULL
  in the four fields. The gateway was not reachable from this environment, so the re-fetch path was first exercised against a stub
  gateway; the PR #109 review then ran it **live** (real gateway, scratch copy of the DB with the 09-29 partial bar and the
  APH/MNST seams): 09-29 volume AAPL 5.6M -> 38.5M, APH and MNST re-fetched in full automatically, split-shaped jumps 7 -> 0, NULL
  `realized_vol_90d` 21 -> 0 per ticker, a second identical run changed 0 observation rows, and PG's 09-29 `tr_log_return` moved
  -0.00592 -> -0.00478 after `build-returns`.
- `tests/test_pricing_integrity.py` (32), `tests/test_trading_calendar.py`, `tests/test_quant_t131.py` (two versions present, the
  counts follow the pinned one even when the other is newer). Mutation-checked: building
  observations from the fetched candles fails 3 tests; dropping the session guard 2; dropping the jump refusal 2; dropping the
  pre-split trigger 1; dropping the touched-bar filter 1; making observations `DO NOTHING` 2; leaving the observation or returns pin off in
  `quant.universe` 1-2 each, ignoring the manifest version in `settings_gate` 1; making the returns upsert `DO NOTHING` 1. `uv run pytest -q` (860), `ruff`, `mypy` green.
- `verify_pilot.py` T-131 checks (last session volume, NULL long-window analytics, split-shaped jumps) run in the pilot.

### Residual scope, deliberately deferred

- **Production clean-up** (user's direction only; unnecessary if production is not used before `T-100`): delete the 2026-09-29 rows
  from `price_observation` / `quant_return_daily` and the benchmark, performance and risk-model rows built on them; re-fetch APH and
  MNST in full; rebuild observations and returns.
- `build-returns` over a window that extends past a new ex-date rewrites that asset's whole history, because `adj_close` is
  back-adjusted to the window end (PR #109 review: MCD/NEE/BF.B/UDR, 1,167 rows each); nothing reads `adj_close`, so results are
  unaffected, and it is left in the change test so the column never goes stale. Exact float comparison can rewrite a row on 1e-18 noise.
- A genuine one-day x2 / x0.5 move is indistinguishable from a seam and needs `--allow-split-jumps` (none in the current universe).
- Early-close days are treated as full days (refuses slightly longer than needed).

## T-132 — Market capitalization: "shares issued", an unbounded-age stored cap, a silent zero weight and a period-end price

**Status**: Fixed 2026-10-02 (branch `feat/t132-market-capitalization`, PR #110, approved; `T-132` (a)-(d)). The upstream half of (a)
(`portfolio-data-mining` T-043, PR #48, merged 2026-10-02) is what makes the cover count available. Production is **not**
repaired by this change: its filings carry no cover counts until they are re-ingested (`T-100` starts from a fresh database).

### Symptom

From `docs/md primera revision/system_review_2026-09-29.md` N4, on production `financial.db` (read-only):

- `statements.REGISTRY["shares_outstanding"]` fell back to `us-gaap_CommonStockSharesIssued`, which includes treasury stock: PG's
  market cap came out at about $650B on 4.009B issued shares against 2.324B outstanding on SEC's cover (about 1.7x too high); 139
  stored filings have only "issued".
- XOM, PM, NEE and HUM have no share concept stored at all: 101 of the 359 sample filings (28%) have no cap, and 238 of the 258 that
  do use the diluted **weighted-average** count.
- `quant.db.load_market_caps` kept the latest non-null cap with no age limit (PG's 2024-03-31 value on 2026-09-29) while
  `cycle.data.market_cap_estimates` took the latest filing even when its cap was NULL -- two readers, two answers.
- `_expected_returns` did `caps_by_id.get(a, 0.0)`: a missing cap was silently weight 0 in the equilibrium market portfolio (XOM 0,
  PG about 40% of the weight in risk model 3), and an all-zero vector fell back to equal weights.
- Caps were valued at the *filing's period-end* price, not the as-of price.

### Root cause

1. The share count had one source, the balance sheet, and a fallback chain that ended on the wrong concept: an absent
   `CommonStockSharesOutstanding` became "issued", and then the weighted average.
2. The cap was computed once, at ingest, and stored as a metric; every reader took whichever stored value it found, with no notion of
   how old the count was or of what the price was on the date being asked about.
3. Nothing treated "no cap" as a condition: the optimizer's input was a dict with a `0.0` default.

### Theoretical/technical reference

- Market capitalization = shares *outstanding* x price; treasury shares are issued but not outstanding. The SEC cover page
  (`dei:EntityCommonStockSharesOutstanding`) is the filer's own count on a stated date, the most current number a filing carries.
- Black-Litterman / equilibrium returns `pi = rf + lambda * Sigma * w_mkt` need market-cap weights `w_mkt` for **every** asset; an
  asset left out is not neutral, it removes its weight from the market portfolio and inflates everyone else's.
- A split between a count and a price puts them on different share bases (a 2:1 split halves an adjusted close but not an older count).

### Fix

- **(a)** `data["cover"]["shares_outstanding"]` from the gateway is parsed (`Statements.cover_shares`; entries without a positive
  value or an `as_of_date` are dropped) and stored in a new additive table `filing_cover_shares` (`kg_schema/ddl.py`: one row per
  filing, class and date; `class_member = ''` is a single-class count or a filer's own total). `EdgarClient.financials` no longer
  raises on a reconciliation error whose statement is `cover` (the gateway's cover read fails in isolation, the statements are still
  validated); `Statements.cover_error` carries it (read from `data["cover"]["error"]`, the final contract of upstream PR #48; the earlier `reconciliation_errors` entry is still read) and the pipeline records it as a `cover` run error. `ensure()` creates the table,
  so no migration is needed.
- **(b)** `CommonStockSharesIssued` is removed from the `shares_outstanding` concepts. The stored `valuation` metrics now take the
  cover count first, then the balance-sheet outstanding count, then (flagged `shares_are_diluted_average`) the weighted average;
  `inputs.shares_from_cover_page` records which. `METRICS_ENGINE_VERSION` -> `metrics-v4`.
- **(c)** `kg_schema/market_cap.py`: `market_caps_as_of(db, asset_ids, as_of=...)` -- the latest cover count from a filing already
  *usable* on the date (`available_at`, T-107) x the last stored close on or before it. It refuses a count older than 200 days
  (`stale_shares`), a close older than 10 days (`no_recent_price`) and a name with no count (`no_cover_shares`); the reason is
  returned, never a number. The count is put on the price's split basis first (every `corpact-v1` SPLIT dated after the count and no
  later than the asset's newest stored bar). `cycle.data.market_cap_estimates(conn, date, asset_ids)` and `quant` both call it;
  `quant.db.load_market_caps` is gone.
- **(d)** `quant.persist._market_caps`: a panel asset the reader cannot value raises `MissingMarketCaps` (naming asset ids and reasons)
  and fails the run. `--allow-missing-caps` builds anyway with those assets at weight 0 and records them; a panel with no cap at
  all always refuses (the equal-weight fallback is gone). Coverage and age (`n_with_cap`, `missing{asset: reason}`,
  `share_age_days_median/max`, `n_multi_class`) are merged into `quant_run.params_json["market_caps"]` and into the risk model's
  `params_json`, and printed by the CLI. `QuantSettings.market_cap_max_share_age_days / market_cap_max_price_age_days /
  allow_missing_caps` carry the knobs.

### Design decisions

- **One reader, no fallback chain.** The reader uses the cover count only. A balance-sheet or weighted-average stand-in would
  reintroduce exactly the silent substitution this fixes; the stored `valuation` metrics keep their fallbacks (a yield needs *a* cap
  more than it needs a point-in-time one) but are no longer what `cycle`'s size factor or `quant`'s market portfolio read.
- **The as-of price, not the period-end price.** `cap(D) = count(latest usable on D) x close(D)`. The stored metric's period-end price
  is unchanged for the yields it feeds.
- **Dual-class filers.** The filer's own non-dimensional total is used when it filed one; otherwise the classes are summed and
  `n_multi_class` flags it, at the traded close. That is exact when the classes carry the same economics (BF.B, MA, HOOD, STZ,
  GOOGL); Berkshire's do not (1 class A = 1,500 class B, the listing BRK.B trades B), so `market_cap.CLASS_CONVERSION` — a small,
  explicit table `{cik: {class_member: units of the traded class}}` — converts A before the sum, in `cover_total`, which the stored
  metric and the reader share (PR #110 review). Summed 1:1 BRK's cap was 35.6% low and its cycle yields ~1.55× high.
- **One company, one weight (quant only).** The reader gives every *listing* the company's whole cap, which is right for `cycle`
  (yields and size are company-level). The equilibrium market portfolio would count GOOG + GOOGL, FOX + FOXA and NWS + NWSA (one CIK
  each) twice, so `quant.persist._split_dual_listings` splits the company cap equally among the panel assets sharing a CIK and
  the grouping is recorded as `market_caps.dual_listed` (PR #110 review). Sibling classes are nearly collinear, so
  `pi = delta*Sigma*w` barely depends on the split.
- **200 / 10 days.** A count is at worst a quarter plus the filing lag old just before the next filing becomes available; both limits
  are settings, not constants. A refusal is preferred to a stale cap.
- **No silent zero, but an override.** Refusing by default stops a risk model from being quietly wrong; the override exists for a
  deliberate run and leaves the evidence on the run row.
- **`metrics-v4`** because valuation outputs change for the same filings (T-102's precedent); `cycle`/`quant` resolve the newest
  version present per group, so until `T-100` has written `v4` for the whole universe, pin `--metrics-version metrics-v3`.
- **No backfill command.** A filing ingested before this change has no cover count and so no cap; re-ingesting it (`run --fresh`, or
  `T-100`) fills it. A dedicated backfill would be a cheaper way to repair a populated database and can be added if the pilot needs it.
- **Quant's `--metrics-version` is now vestigial.** Market caps were the only `fundamental_metrics` quant read; the manifest keeps
  the `metrics` entry so that existing risk-model keys stay comparable, but it no longer changes any number.

### Verification

- **Live gateway, 2026-10-02** (real `EdgarClient`): PG 2,324,433,060 (2026-07-31), XOM 4,166,763,453 (2026-01-31), HUM 120,595,967
  (2026-01-31), GOOGL A 5,822M / B 837M / C 5,438M (2026-01-28) -- identical to the upstream PR's table.
- **Scratch copy of production `financial-3.db`** (a copy; production untouched): cover counts for the 20 sample names' latest 10-K and
  10-Q (48 rows) stored through `db.insert_cover_shares`, then `market_caps_as_of(..., "2026-09-29")`: **20 of 20 names have a cap**
  (6 had none before: BF.B, HUM, NEE, PG, PM, XOM), count ages 29-95 days (median 68), 4 multi-class. PG = 2,324.4M x $148.15 =
  **$344.4B** (SEC's cover count exactly; the ~$650B on issued shares is gone). `quant build-risk-model --analysis-date 2026-09-29` on
  that copy: 18/18 caps, median age 71 d, and XOM's equilibrium return moved from 1.99% (risk model 1, cap 0) to 9.98%.
- `tests/test_market_cap.py` (22: formula, point in time, age limits, splits, classes, coverage), `tests/test_cover_shares.py` (28:
  parsing, client, registry, valuation, storage, pipeline), `tests/test_quant_market_caps.py` (12: refusal, override, recorded coverage,
  CLI, cycle = quant). Mutation-checked, all caught: shares issued back in the registry; no age limit; no split adjustment; no
  `available_at` filter; missing caps weighted 0 silently; the balance sheet before the cover; a cover error failing the filing; the
  all-missing fallback; a split after the last bar applied; the filer's total summed with its classes; the cover not stored by the
  pipeline; the legacy corporate-action engine counted; `cycle` not calling the reader. `uv run pytest -q` (919), `ruff`, `mypy` green.
- **PR #110 review follow-ups**, live: the gateway's final payload carries `data["cover"]["error"]` (`null` on success) and an empty
  `reconciliation_errors`; BRK-B's latest 10-Q gives 1,408,035,161 B + 1,500 × 488,450 A = 2,140,710,161 B-equivalents × $502.61 =
  **$1,075.95B** as of 2026-09-29 (summed 1:1 it was $707.7B, 34% low). Tests added for the final error shape, the BRK conversion in
  both the metric and the reader, and the dual-listing split; mutation-checked (ignoring `cover.error`, counting dual listings twice,
  dropping the conversion, the metric ignoring the issuer — all caught). `uv run pytest -q` 927.
- `verify_pilot.py`'s T-132 check (latest filing's stored `market_capitalization` is not NULL) runs in the pilot; it reads the stored
  metric, which now prefers the cover count.

### Residual scope, deliberately deferred

- A new multi-class filer whose classes are not 1:1 needs a line in `CLASS_CONVERSION` (BRK is the only one in the universe today).
- The equal split between listings of one issuer is a convention; a class-based split is equally defensible.
- The stored `valuation.market_capitalization` can differ from the as-of cap (period-end price, and a fallback count when a filing has
  no cover entry); only the yields and `DQ_MCAP_SCALE` read it.
- Production has no cover counts until it is re-ingested.

## T-133 — Quarterly cash flow: a 10-Q's year-to-date columns left FCF empty for 95% of Q2/Q3, and one quarter vetoed a company

**Status**: Fixed 2026-10-02 (branch `feat/t133-quarterly-cash-flow`, PR #111, approved; `T-133` (a)-(c)). Folded into `metrics-v4`, the version `T-132`
introduced: no `metrics-v4` row has been written to any production database yet, so a further bump would only add a version nothing
reads. Production is **not** repaired by this change: its stored metrics are `metrics-v2` and are replaced by the `T-100` re-run.

### Symptom

From `docs/md primera revision/system_review_2026-09-29.md` N5/N6, on the 20-ticker sample of production `financial-3.db`:

- A 10-Q's cash-flow statement reports year-to-date columns only (Q2 = six months, Q3 = nine), and the `cashflow` group read the quarter
  column. **FCF margin was missing for 172 of 182 Q2/Q3 10-Qs (94.5%)**; the cashflow group, VALORIZATION's quality factor and
  `NEGATIVE_FCF` therefore worked only on Q1 10-Qs and 10-Ks, so names were compared on different inputs depending on the quarter of
  their latest filing.
- Where the quarter column did exist it was **one quarter**. `NEGATIVE_FCF` (HARD) read that margin: WAT's first quarter of 2026 has
  OCF −$3M and capex −$39M on revenue of $1,267M (FCF margin −3.3%) while its trailing year is positive, and `T-125` makes the veto
  permanent. BF.B (2 filings) and PM (4) had the same shape.
- APA and PSX had no FCF on any 10-K: the capex registry named three PP&E concepts, and APA files its drilling spend under
  `PaymentsToExploreAndDevelopOilAndGasProperties` (ASC 932) and PSX under the custom `psx_CapitalExpendituresAndInvestments`.

### Root cause

1. The group computed ratios from `stmts.get(item, period_key)`, the filing's own quarter column. T-105 had already built the
   trailing-twelve-month flows (`db.ttm_detail`: `FY(prior 10-K) - YTD(last year) + YTD(this year)`, which needs no quarter column) and
   handed them to `valuation`, `leverage` and `roic`, but not to `cashflow`.
2. A threshold rule reads whatever the metric holds; nothing said the metric should be a year.
3. Capex was found by a closed list of concepts, with no path for a filer whose spending is not "property, plant and equipment".
4. (Found while verifying.) The year-to-date detection of T-105 treats a first quarter's quarter column as its YTD only when the gateway
   tags it `(Q1)`. The gateway tags a column by the calendar quarter its date falls in, so Waters' first quarter (ends 2026-04-04) arrives
   as `(Q2)`, beside a prior-year `(Q1)`, with no `(YTD)` column at all: no pair, no identity, and the flow fell to `quarter x 4`.

### Theoretical/technical reference

- Free cash flow is a flow: its margin is only comparable across filings over the same horizon. The trailing twelve months is the
  horizon a 10-K already reports and the one `T-105` standardised on for every other flow ratio.
- A company-exclusion rule should not act on seasonal or lumpy single-quarter cash (working-capital timing, a capex cluster); a
  quarter multiplied by four is that quarter's ratio again, not a trailing year.
- XBRL ASC 932 (extractive activities) capitalises exploration and development under dedicated concepts; an *acquisition* of
  reserves (`PaymentsToAcquireOilAndGasProperty`) is not maintenance or development capex.

### Fix

- **(a)/(b)** `cashflow.compute(stmts, key, prior_key, ttm)`: `ttm is None` (a 10-K) reads the annual columns as before; a dict (a
  10-Q, even an empty one) computes **every** ratio from the TTM flows alone -- OCF, capex, revenue, net income -- so a missing flow
  leaves the ratio empty rather than dividing a 12-month numerator by a 3-month denominator. The audit inputs keep the raw
  single-period figures (a later filing's TTM reads `operating_cash_flow` and `capital_expenditure` back from them) and add
  `*_ttm` keys. `agents._group_ttm` hands the group `FilingContext.real_ttm`: the `ytd` and `quarters` flows, **never the `x4`
  fallback**. `NEGATIVE_FCF` still reads `cashflow.free_cash_flow_margin`; that margin is now a trailing-twelve-month figure (its
  description says so), so the rule needs no change of its own, and a filing whose year cannot be built has no margin and is left
  unevaluated rather than judged on a quarter.
- **(c)** `LineItem` gains `fallback_concepts` (ordered tiers) and `label_fallback`/`label_fallback_exclude`, consulted only when the
  ordinary pass finds nothing, so they can never displace a value that was found before. `capital_expenditure` falls back to, in order,
  `PaymentsToExploreAndDevelopOilAndGasProperties`, `PaymentsToAcquireOilAndGasPropertyAndEquipment` (EOG),
  `PaymentsToAcquireOilAndGasProperty`, `PaymentsForProceedsFromProductiveAssets` (WAT's 10-Ks to FY2024), then a cash-flow-statement
  caption "capital expenditure(s)" that excludes the statement's non-cash reconciling rows and proceeds, **and only when exactly one
  line reads that way**.
- **Year-to-date detection** (`pipeline._is_first_quarter`, used by `_ytd_columns`): a quarter is a fiscal year's first when the gateway
  tags it `(Q1)` **or** the balance sheet carries an instant 60-115 days before its end (the comparative column of a 10-Q is the last
  fiscal year end, one quarter back for a first quarter, two or three for the others). Its prior-year column may carry any quarter tag.

### Design decisions

- **TTM for every 10-Q, not "YTD minus the prior quarter's YTD".** The task allowed either; the identity already exists, is exercised
  by `valuation`/`leverage`/`roic`, needs only the filing's own columns plus the prior 10-K, and makes the cash-flow group consistent with
  them. Deriving a quarter would also have meant recording a derived value where the pipeline records raw ones.
- **`x4` is excluded from the cash-flow group.** The other groups keep it (flagged `annualized_x4`), because their ratios are not
  exclusion rules. A quarter x 4 inside a HARD rule would reproduce N6 for any filing whose year cannot be built; the cost is that such a
  filing has no cash-flow ratios, which affects only the first quarters stored before any 10-K of the asset (below).
- **A partial capex is worse than none.** NEE files "Capital expenditures of FPL" ($9.1B), a duplicate "Capital expenditures", "Other
  capital expenditures" and the independent-power line; any one alone put NEE's FY2022 FCF margin at −3.8% where the total is far lower.
  Ambiguity returns no capex. The same rule is why the concept tiers are ordered and the caption is a last resort.
- **An oil & gas filer's "other PP&E" line is added to its oil & gas spend** (PR #111 review). EOG files $6,115M of oil and gas
  additions and, on a separate line, $479M of `PaymentsToAcquireOtherPropertyPlantAndEquipment`; stopping at the first tier put its FCF
  at $3,929M instead of $3,450M (~14% high). `fallback_addends` joins that concept to the three oil & gas tiers only (not to the "net"
  additions line, and it is never capex on its own): a separate line cannot double count. Live: EOG capex -6,594M, FCF margin 15.2%.
- **Folded into `metrics-v4`** rather than `v5` (see Status).

### Verification

- **Live gateway, 2026-10-02** (real `EdgarClient`, FY2025 10-Ks): APA capex −2,740M (FCF margin 20.2%), PSX −2,233M (2.1%), EOG
  −6,115M (17.4%), FANG −3,523M (development, **not** the −5,938M of acquisitions listed beside it; 34.8%), COP −12,553M, XOM −28,358M
  (unchanged concept). NEE: no capex (ambiguous), as designed. WAT's first-quarter 10-Q arrives as `(Q2)` with `(Q1)` as its comparative:
  `_is_first_quarter` is true and the pair `("2026-04-04 (Q2)", "2025-03-29 (Q1)")` is found; the single quarter reads −3.3%.
- **Replay of the scratch copy of `financial-3.db`** (a copy; production untouched): every stored filing rebuilt into a payload from
  `financial_facts`, run chronologically through the real `_targets`/`_ytd_columns`/`db.ttm_detail`/`cashflow.compute`, each filing's raw
  flows recorded under a throwaway engine version so the next filing's identity reads them. 20-ticker sample, 182 Q2/Q3 10-Qs:
  **FCF margin missing 172 (94.5%) -> 45 (24.7%)**. 41 of the 45 are five names with no capex line the registry recognises or that is
  ambiguous (APO, WFC, HOOD: financials; ESS: a REIT; NEE: a utility) -- not the year-to-date defect; the other 4 are the first quarters
  stored before any 10-K of PG, BF.B and STZ (no prior fiscal year to anchor a TTM). Among names that have an FCF on their 10-Ks, 9 of
  146 Q2/Q3 (6.2%) are missing, all of that kind; over the whole 503-ticker database the same figure is 141 of 2,136 (6.6%), the
  scratch database holding only part of each ticker's history. Read as written ("< 5% of Q2/Q3 FCF margins missing") the
  target is not reached; the PR #111 review recalibrated it to the population where an FCF is defined -- companies with a 10-K FCF,
  10-Qs after their first 10-K -- where the remaining gap is the earliest quarters with no prior year, and treats it as met.
  `verify_pilot.py`'s T-133 check measures that population; since pilot-1 (2026-10-04) it uses the Q2/Q3 10-Qs whose latest prior 10-K has
  an FCF margin (0/159 missing on pilot-1). Companies with no capex line (APO, WFC), one the registry does not recognise
  (HOOD reports "Purchases of property, software, and equipment" every year; from FY2023 it is tagged
  `us-gaap:PaymentsToAcquireOtherProductiveAssets`, part of the capex-concept inventory deferred to `T-071` below),
  or one deliberately left empty (ESS; NEE's split capex), are outside it.
- **WAT**: first quarter of 2026 FCF margin **-3.3% -> +7.0%** (TTM = FY2025 - Q1 2025 + Q1 2026, method `ytd` for OCF, capex and
  revenue), so `NEGATIVE_FCF` no longer fires; the later 10-Q reads +8.6%. Across the sample, 7 10-Qs flip from a negative single
  quarter to a positive year (WAT 1, BF.B 2, PM 4). New negatives are real: HUM 2024Q1-Q3 (-3% to -5%) and HOOD.
- `tests/test_cashflow_ttm.py` (25): the TTM ratios and their inputs, no mixing of bases, an empty TTM is still a 10-Q, the WAT shape,
  which flows each group is given, the capex tiers and their order, the caption fallback (non-cash and proceeds rows, ambiguity), the
  first-quarter detection both ways, and the analyst wiring. Mutation-checked, all caught: ambiguity returning the first line; the
  non-cash exclusion removed; the acquisition tier ahead of development; a fallback displacing a found value; a 3-month denominator in
  TTM mode; an empty TTM treated as a 10-K; `x4` inside `real_ttm`; the group given `x4`; a 10-K given a TTM; the balance-sheet
  detection disabled or always true; the prior-year column required to carry the same tag. `uv run pytest -q`, `ruff`, `mypy` green.
- **PR #111 review follow-up**: the oil & gas "other PP&E" addend above (3 tests; mutation-checked: addend tiers 0 and 4 both
  caught). `uv run pytest -q` 955.
- `verify_pilot.py`'s T-133 checks run in the pilot.

### Residual scope, deliberately deferred

- **Capex-concept inventory** (recorded as scope of `T-071`, sector-appropriate cash-flow measures in the valorization redesign, per the
  PR #111 review -- not a new task). Filers whose capex is a utility's construction line (`PaymentsForConstructionInProcess`: AEP, ED),
  split into utility and non-utility lines (DTE, LNT, NEE), or tagged `PaymentsToAcquireOtherPropertyPlantAndEquipment` /
  `...OtherProductiveAssets` (LLY, EQIX, DAL, HOOD) still have no FCF. Each needs a decision about summing lines that the single-line
  lookup cannot make; banks, insurers and asset managers have no capex line at all.
- The first quarters of an asset's stored history (before its first 10-K) have no cash-flow ratios. They are the oldest filings, which
  `cycle` never reads as an asset's latest.
- A quarter whose year can be built only as `x4` still gets `x4` ratios in the other groups, flagged `annualized_x4`.
- Production carries `metrics-v2`; the re-run is `T-100`'s.

## T-140 — A revenue total the gateway dropped, and filings lost to a repeated period label (pilot-1 F1/F2)

Pilot-1 (2026-10-04, 20 tickers on a fresh database) ran at 2 FAIL with the original verification. Both failures trace to two ingestion
defects; neither raised an error. Methodology change (constitution AI behavior #12): the engine becomes **`metrics-v5`**
(`db.METRICS_ENGINE_VERSION`), so a re-run writes beside the `metrics-v4` rows instead of colliding with them.

### Symptom

- **F1 -- APA has no revenue for 2021Q1-2023Q3 (11 filings).** Pilot-1 stored `revenue` `None` for nine of them and a **$0** (a surviving
  "production revenues" component) for 2021Q1 and 2021Q2, so every revenue ratio was empty and `DQ_REVENUE_POS` (HARD) fired **77 times on
  11 filings** -- all APA -- and vetoed it (replay 2024-01-05 to 2024-02-23). FY2023 onward was fine (`T-117`).
- **F2 -- a 10-Q lost to a repeated label.** Waters' quarter ended 2023-07-01 (fiscal Q2) arrived tagged `(Q3)`, the same tag as the real Q3
  (2023-09-30); the resume key `(ticker, form, fiscal_period)` read the second as already done (`pipeline.py:343-347`) and skipped it, no
  error logged. WAT was stored with 22 filings and a 183-day hole, and two of them labelled **Q4** (2021-10-02, 2022-10-01). A 10-Q has no Q4.
  Production has 40 names with a 10-Q labelled "Q4" (JNJ, PFE, TMO, AAPL, INTC, DIS, TGT among them): the known scope for `T-100`.
  APO's Q1-2023 10-Q was neither stored nor logged: the gateway returns only the prior fiscal year's column (`2022-12-31 (FY)`).

### Root cause

- **F1.** The gateway's `T-042` rule (`no_filed_nondimensional_fact`) removes a value a filer tags only with a dimension. APA tags
  `us-gaap_Revenues` ("Total revenues") that way, so the row survives in the payload **empty**, and so do its components; only the later
  "Total revenues and other" ($12,132M for FY2022) keeps a value. `Statements.get("revenue")` found no `total_concepts` value and fell to
  Tier 2: nothing, or a $0 component. The gateway's `corrections` list names what it dropped (`original` 11,075 for FY2022 -- see below for
  when that is, and is not, the total).
- **F2.** The gateway tags a column from the *calendar month* of the period end relative to the fiscal year end's month, so a 52/53-week
  filer's quarter that closes a few days after a month boundary is tagged one ahead: Waters 2023-04-01 `(Q2)`, 2023-07-01 `(Q3)`,
  2021-10-02 `(Q4)`; Johnson & Johnson 2023-10-01 `(Q4)`. `pipeline._targets` built the label from that tag, and the resume key, the
  `sec_filings` upsert and the accession trigger all key on the label. The same flaw hides a year later in `FY<calendar year of the end>`:
  J&J's fiscal 2022 ended 2023-01-01 and fiscal 2023 ended 2023-12-31 -- both `FY2023` (found while auditing the keys; a Starbucks-shaped
  calendar does the same with `2023Q1`).

### Fix

**(a) `Statements.rebuild_total` (`statements.py`, beside `T-117`'s `_label_total_correction`).** Only where no `total_concepts` row has a
value in the column (a total that is present but rejected, `T-095`/`T-128`, is a mistagged value, not a dropped one). By label, never a
filer's concept name:

1. The *revenue section* ends at the last valued row of a registry revenue concept (`concepts`/`total_concepts`).
2. The anchor is the first valued row **after** it whose label reads as a revenue total (`T-117`'s regex, **"cost" lines excluded**).
3. Revenue = anchor minus the valued rows between the section and the anchor. APA FY2022: 12,132 - (-114 + 1,180 - 157 + 148) = **11,075**;
   FY2021: 7,928 - (94 + 67 - 446 + 228) = **7,985**.
4. `T-117`'s trust rule: every row between must be at most 25% of the anchor, else **no value** (never a guess). A non-positive result is
   refused too. Works on any column -- 10-K, a 10-Q's quarter, its year-to-date.
5. A component that survived but is *below* the rebuilt total (APA 2021Q1's $0 row) is a partial stream and loses; one *above* it
   contradicts the rebuild and the pre-`T-140` answer stands.

**(b) `fiscal.py`, `pipeline.py`, `db.py`.** A 10-Q's quarter is its distance from the fiscal year end, `round(days / 91.3)` modulo a year,
within 30 days of a quarter boundary, only 1-3 (the fourth is the 10-K) -- the same on any 52/53-week or 4-4-5 calendar. The year end is the
filing's own balance sheet comparative column (`payload_fiscal_year_end`: the same on every path, `repair.py`, which has no database at hand,
included, and right for a company that changed its fiscal year -- FERG, July to December, whose stored 10-K of 2025-07-31 would put its
2026-06-30 quarter at no quarter; PR #115 review), else the asset's latest stored 10-K before the period (`db.latest_fiscal_year_end`). Label year is the calendar year of the period
end, **counting an end in the first week of January as the year before's** (`fiscal.label_year`); the stored `fiscal_year` follows.

- **Resume key**: `(ticker, form, period_end)` (`db.completed_units`, `_Unit`); `score_snapshot` was already keyed on `event_time`.
- **`upsert_filing` refuses a label already held by another period end** (`FilingLabelCollision`): its `ON CONFLICT (asset, form,
  fiscal_period)` used to overwrite the first filing in place. A second period under a label is now a recorded failure, never a silent loss.
- **A filing with no period of its own is recorded** (`analysis_run_error`, stage `period`, with the accession and the columns the payload
  held; counted as skipped and printed by the CLI): no quarter column at all (APO Q1-2023), a latest quarter column that ends before the
  balance sheet's date (a comparative, not the filing's own), or a date that is no quarter end of the fiscal year.
- **`Statements.prior_of` pairs a quarter with the quarter column that ended a year before it** (date, +-20 days), not the same tag: Waters'
  `2023-09-30 (Q3)` sits beside `2022-10-01 (Q4)`, so the tag found no prior year and the filing the key used to drop would have been stored
  without a year-over-year comparison. Other periods keep the same-tag rule.

### Design decisions

- **The 25% ceiling stays `T-117`'s, and is not tuned on APA.** Two own-quarter APA columns exceed it: 2021Q3 (a $446M loss on a $1,651M
  subtotal, 27%) and 2022Q1 (a $1,176M divestiture gain on $3,828M, 31%). Refusing them leaves two APA filings with no revenue and
  `DQ_REVENUE_POS` still firing. They are admitted on **corroboration**, not on a looser constant: when the gateway's own record of the
  dropped total (`corrections[].original`, same statement, concept in `total_concepts`, same column) equals the rebuilt value to the dollar
  (2,059 and 2,669), two independent derivations agree. Without the gateway's figure -- the replay from `financial_facts`, which does not
  keep `corrections` -- they are refused (below). The `original` is **never trusted alone**: it can be the promoted dimensional slice
  (next bullet), which no rebuild equals.
- **Cross-check against the gateway's `original`, all 11 APA payloads (34 dropped `us-gaap_Revenues` entries): the rebuild equals it in 26.
  The other 8 are the equity-method-investee slice** ($1,082M for FY2021, $707M FY2020, $302M FY2019, and $812M, $531M twice, $351M and $254M in
  quarterly and year-to-date columns) -- exactly one of the filing's dimensional `us-gaap_Revenues` rows each, 5-17% of the real total. That is `T-128`'s
  shape, so the stated check ("every rebuilt value equals the original") holds only where the original is the consolidated total, and the
  rebuilt figure is the one corroborated by the FY2022 10-K's original for FY2021 (7,985).
- **Label year stays the calendar year of the period end** (not the fiscal year): every label a calendar filer has, and every one a
  non-calendar filer's tag already got right, is unchanged (below). Moving to fiscal-year labels would relabel all non-calendar filers.
- **The unique constraints are kept**, `sec_filings (asset_id, form, fiscal_period)` and the legacy `fundamental_snapshot`: with a label that
  is a function of the period end they hold, and the collision guard makes any violation loud. Rebuilding SQLite tables to key on the period end
  would be a non-additive migration for no gain. Relabelling is **not done in place** on an existing database (its snapshots are immutable and
  point at the old row): re-ingest into a fresh one (`T-100`); a non-fresh run over an old-labelled row fails loudly through the guard.
- **No fallback to the gateway's tag** when no fiscal year end can be found: no label is better than a wrong one that can collide.

### Consumers of `fiscal_period` and of the quarter tag -- none changes for a calendar-quarter filer

| Consumer | Reads | Effect |
|---|---|---|
| `sec_filings` upsert / `UNIQUE (asset, form, fiscal_period)` / accession triggers | the label | label unchanged for calendar filers; collisions now refused (above) |
| `completed_units` (resume) | **was** the label | now the period end |
| `score_snapshot`, `fundamental_snapshot` (a view over `score_snapshot` after the shared-schema migration) | `event_time` (period end); label shown only | none |
| TTM pairing (`db._filing_near`, `ttm_detail`, `_fiscal_year_between`) | period end (dates), by design since `T-094` | none |
| `_is_first_quarter`, `_ytd_columns` (`T-133` TTM path) | the column tag `Q1`, or the balance-sheet gap; the YTD columns by date | none (not the label) |
| `Statements.prior_of` -> growth, CAGR | **was** the column tag | quarters now by date: identical for a calendar filer, finds the prior year for a shifted one (7 pilot filings, all WAT) |
| `repair.py` (`_targets` labels vs stale rows) | the label | same derivation; legacy stale rows carry old labels (run `repair-accessions` before a re-ingest, as before) |
| LLM prompt text (`agents.py`), CLI, `v_sec_filing` | the label, for display | the text differs only for the relabelled filings |

### Verification

- **Replay of a scratch copy of the pilot-1 database** (449 filings, 1,487 income-statement columns, every period of every
  stored filing, rebuilt from `financial_facts`; `scripts/verify_t140.py`; the copy deleted afterwards). **(a)** the rebuild runs on 110
  columns and changes the value of **31, all APA**; PSX's 74 equal the value Tier 2 already gave (the consolidated "Total Revenues and Other
  Income" less equity earnings, gains and other income reproduces its "Sales and other operating revenues" to the dollar -- the rule agrees
  with a second filer); 5 APA columns are refused (3 comparatives and the two own columns above, admitted only with the gateway's figure);
  no component was above a rebuilt total. **(b)** 449 10-K/10-Qs, 334 of them 10-Qs: **10 relabelled, all WAT**; the other 19 tickers
  (324 10-Qs, every 10-K) keep their label, among them the non-calendar BF.B, PG and STZ. The two Q4 labels become Q3.
- **Every APA own-period column, from the live gateway** (11 filings, 2021Q1-2023Q3, by `Statements.get` on the real payloads):
  1,871 / 1,756 / **2,059**\* / 7,985 (FY2021) / **2,669**\* / 3,047 / 2,887 / **11,075** (FY2022) / 2,008 / 1,796 / 2,308 ($M; \* admitted by
  the gateway's figure). The rows between: for 2021Q1, 2,092 - (158 + 2 + 61); for 2023Q3, 2,309 - (0 + 1 + 0).
  The corroborated quarters agree with the year-to-date arithmetic (2022 YTD-Q2 5,716 - Q2 3,047 = 2,669; 2021 YTD-Q3 5,686 - H1 3,627 = 2,059).
- **End to end on a fresh scratch database, real gateway, the LLM stubbed** (APA, WAT, APO, 2021-2023; not pilot-2): 29 filings analysed, 5
  skipped, 0 failed, every row `metrics-v5`. APA: the eleven revenues above, net margins 24.1% / 23.2% / -1.5% / 16.4% (FY2021) / 72.9% (the
  one-off gain) / 35.0% / 18.4% / 36.9% (FY2022) / 16.2% / 25.7% / 24.1%; **`DQ_REVENUE_POS` = 0** (pilot-1: 77). WAT: 12 filings, all
  three 2023 quarters stored as `2023Q1`/`Q2`/`Q3` (periods 04-01, 07-01, 09-30), 2021-10-02 and 2022-10-01 labelled Q3, **no gap over 110
  days, no 10-Q labelled Q4**. APO: Q1-2023 recorded as `analysis_run_error` (stage `period`: "the payload has no quarter column of its own
  (periods: 2022-12-31 (FY)); the gateway returned only another period").
- Real J&J payloads (10-K year ended 2023-01-01 and 2023-12-31; the 10-Q ended 2023-10-01 tagged `(Q4)`): `FY2022` and `FY2023`, `2023Q3`.
- `tests/test_revenue_rebuild.py` (20) and `tests/test_quarter_labels.py` (58), on real gateway captures (APA, WAT, APO, J&J; trimmed to the rows
  needed): the real APA/WAT shapes, the rule on small synthetic statements, a 15-year 52/53-week calendar, the calendar filer's unchanged
  labels, the resume key and the collision guard through a full pipeline run, APO's recorded skip, a comparative-only payload. Mutation-checked,
  all caught: corroboration removed; the cost-line exclusion dropped; the ceiling disabled; a surviving component always beating the rebuild;
  the section start ignored; the rebuild run even when a total survives; the sign flipped; the resume key back on the label; the collision
  guard removed; the label taken from the gateway's tag; APO's recording removed; the payload's fiscal year end ignored; the 30-day tolerance
  removed; quarter 4 allowed; the own-quarter check removed; the year wraparound removed; `prior_of` back on the tag; the January shift
  removed. `uv run pytest -q` 960 -> 1,038; `ruff`, `ruff format --check`, `mypy` green.

### Known scope and residual

- **`T-100` counts**: the filings whose revenue is rebuilt and the filings whose label changes, over the full universe, inspected. Known:
  the 40 production names with a "Q4" 10-Q (above), every 10-K ending in the first week of January (J&J's fiscal 2021 `FY2022` -> `FY2021`,
  2022 `FY2023` -> `FY2022`), and any 10-Q ending then. Not measured here: production data was out of reach of this change.
- **APO Q1-2023 stays unstored** until `portfolio-data-mining` returns the quarter: the run error is the record, and a pilot-2
  verification of "no gap over 110 days" will still see APO's 181-day hole (2022-12-31 -> 2023-06-30). Upstream note drafted, not filed.
- A rebuild needs the statement's own later "total ... revenue" line; a filer with none still has no revenue when the gateway drops it.
- A pilot verification that expects a single metrics version must accept `metrics-v5` beside `metrics-v4` on a re-run.

## T-134 – T-139 — The portfolio weight rule: caps that cannot hold, and a book that was not a function of N (Work item 18)

The 2026-10-02 audit found that the thesis book could break its own caps without saying so. Methodology change (constitution AI behavior #12):
the construction of the live and replay books is replaced (`cycle.construction.build_book`, `T-135`/`T-136`) and the quant benchmark follows the
same rule (`quant.caps`, `T-137`, optimizer engine **`opt-v2`**). Everything below was measured read-only against production, or on scratch
copies of the pilot databases (deleted afterwards); `scripts/verify_t138.py` is the read-only check of every stored book. **Production is not
re-selected by any of this**: the live book changes only when `cycle select` is next run, which waits for the user's direction.

### Defect

`cycle.construction.target_weights` took `top_n` but kept the caps as constants (`max_name_weight` 0.10, `max_sector_weight` 0.30) whatever N
was, then alternated the two caps for **8 rounds** and returned whatever it had, with no error, log or record. At `N = 10` a 0.10 name cap forces
every name to exactly 0.10, and a sector holding four of the ten then sums to 0.40, over the 0.30 cap: **no** weight vector satisfies both, and
nothing said so. Production `cycle_run` 1 (SELECTION 2026-09-22, N = 10, `score_proportional`) is the case:

| | stored (`target_weights`) |
|---|---|
| MA, HOOD, APO, WFC (Financials, 4 names) | 0.075 each = **0.300** |
| APA, PSX, XOM (Energy, 3 names) | 0.1167 each = **0.350 against a 0.30 cap** |
| PG, PM (Consumer Staples), UDR (Real Estate) | 0.1167 each |
| largest name | **0.1167 against a 0.10 cap** (six names) |

`docs/cycle.md` described the caps as holding "within rounding"; a 17% breach is not rounding. `verify_t138.py` against production (read-only)
flags exactly these two violations on the one stored book. The quant benchmark had its own constant caps (0.05 / 0.30), so it was not comparable
to the live book at any other N, and on the 20-asset pilot panel `20 x 0.05 = 1` forced every `min_var`, `tangency` and `target_vol` book to
**exactly 0.05 in every name**, whatever the covariance (pilot-1).

### The rule that replaces it (the T-134 decisions, user, 2026-10-04)

1. **N** is `--top-n` (default 30). `n_held = min(N, eligible names)`; fewer eligible than N: hold them all, band on the actual count, record the
   **shortfall**, never pad.
2. **`score_tilt`** is the new default scheme: an equal-weight core with a bounded score tilt. Scores map linearly onto `[0.5/n_held, 1.5/n_held]`
   (lowest held score to the floor, highest to the ceiling; all equal -> equal weights) and the result is the **exact Euclidean projection** of
   that target onto `{sum = 1, band, sector sum <= cap}` (a common shift, bisection-free, finitely many steps). The name cap is `1.5/n_held` with
   **no 0.10 floor** (0.05 at N = 30, 0.15 at N = 10, 0.50 at N = 3); an explicit `--max-name-weight` wins, one below `1/n_held` is relaxed to it
   and recorded. `equal`, `score_proportional` and `inverse_vol` stay selectable (the same projection, floor 0, name cap 0.10 unless given).
3. **Sectors**: cap 0.30 with a **sector-aware fill** -- a ranked name whose sector already holds `max(1, floor(cap x n_held))` names is skipped
   for the next. When the cap cannot hold it is relaxed to the smallest feasible value and recorded; the book is always fully invested.
4. **Preferences** (`--pin`, `--exclude`, `--exclude-sectors`, `--only-sectors`) are inputs to the pure function; a HARD-vetoed pin is refused with
   its reason, a SOFT-vetoed pin is held and flagged. Books built with preferences are **decision support, never thesis results**; a writing
   `select` refuses them (`select --dry-run` previews them, `backfill` replays with them).
5. Everything applied -- the effective caps, relaxations, shortfall, preferences, refused/flagged pins -- is recorded in `cycle_run.params_json`
   (`v_weight_scheme` reports the effective caps, no view change); a resume with other settings is refused; `select --dry-run` is strictly
   read-only. The quant benchmark uses the same `1.5/n_held` and 0.30 with no limit on the number of names (no integer programming).

Reference: DeMiguel, Garlappi & Uppal (2009), "Optimal Versus Naive Diversification: How Inefficient is the 1/N Portfolio Strategy?", *Review of
Financial Studies* 22(5), 1915-1953 -- the 1/N book is hard to beat out of sample, so the thesis book is an equal-weight core with a bounded
tilt (`equal` is its zero-tilt case) rather than score-proportional weights.

### Verification

**1. Production's failing shape, read-only** (`cycle select --analysis-date 2026-09-22 --dry-run --top-n 10`; the stored ranking of `cycle_run` 1;
nothing written; the production file's SHA-256 before and after: `78878b160bb2f609e2dbe66aa4e0bcc35cc6f7c40d316920fd4edabf46f3369c`, **identical**):

| ticker | sector | stored | new `score_tilt` |
|---|---|---|---|
| MA | Financials | 0.0750 | 0.1478 |
| PG | Consumer Staples | 0.1167 | 0.1379 |
| APA | Energy | 0.1167 | 0.1056 |
| PSX | Energy | 0.1167 | 0.1051 |
| HOOD | Financials | 0.0750 | 0.0898 |
| XOM | Energy | 0.1167 | 0.0893 |
| PM | Consumer Staples | 0.1167 | 0.0990 |
| APO | Financials | 0.0750 | 0.0625 |
| WFC (rank 9, the **fourth Financial**) | Financials | 0.0750 | **skipped** |
| UDR | Real Estate | 0.1167 | 0.0855 |
| ESS (rank 11, the next-ranked name from another sector) | Real Estate | -- | 0.0776 |

Effective caps 0.15 / 0.30, no relaxation. Every weight is in `[0.0625, 0.1478]` (band `[0.05, 0.15]`; the stored book had 0.1167 over its 0.10);
Financials **0.300** (3 names) and Energy **0.300** (3 names), against the stored 0.30 / **0.35**; Consumer Staples 0.237, Real Estate 0.163. The
default dry run on the same date (N = 30): the cohort has **20** eligible names, so the book holds 20 (shortfall 10 recorded), name cap
`1.5/20 = 0.075`, weights in `[0.0296, 0.0750]`, the largest sector Financials 0.239 (the sector cap does not bind).

**2. The default thesis book and the N sensitivity** (three scratch copies of the pilot-1 replay database; `cycle backfill --from 2024-01-05 --to
2026-10-02 --step-days 7 --force --top-n N`, default scheme, 144 weekly dates; `scripts/verify_t138.py` on each; no performance is reported -- it
is not a thesis result here, decision 6, and the evaluation belongs to `T-100`):

| | N = 10 | N = 20 | N = 30 |
|---|---|---|---|
| invariant violations | **0** | **0** | **0** |
| dates with a relaxation (kinds) | 0 | 0 | 0 |
| effective name / sector cap | 0.15 / 0.30 | `1.5/n_held` (n_held 15-19) / 0.30 | the same |
| average names held | 10.00 | 17.56 | 17.56 |
| dates with a shortfall | 0 | 144 | 144 |
| mean one-way turnover per week | 0.0559 | 0.0178 | 0.0178 |
| median score range of the held names (min / max) | 16.53 (10.40 / 25.51) | 36.69 (17.87 / 49.32) | 36.69 (17.87 / 49.32) |
| median sd of the held scores | 5.03 | 9.03 | 9.03 |
| dates the name cap binds / the sector cap binds | 112 / 88 | 27 / 0 | 27 / 0 |

The pilot universe is a **20-ticker sample**, so only 15-19 names are eligible on any date: at N = 20 and N = 30 every date holds all of them
and the two books are **bit-identical** (2,405 stints); the sensitivity that distinguishes N is N = 10 against the rest. The pilot-1 replay
(`score_proportional`, caps 0.10 / 0.30 at N = 30) had **0** cap violations on this small universe -- the defect needs a sector that holds a
large share of a small N, which is production's shape, not the sample's -- and a mean one-way turnover of 0.0290 (the tilt's 0.0178 is lower).

**3. One run with preferences -- decision support, not a thesis result** (`cycle select --analysis-date 2026-10-02 --dry-run --top-n 5 --pin MA
--only-sectors "Financials,Energy"` on a scratch copy of the pilot database; nothing written): MA 0.1418, PSX 0.3000, HOOD 0.2237, XOM 0.2000,
APO 0.1345. Relaxations: the **sector cap 0.30 -> 0.50** (two sectors over five names cannot hold 0.30; the fill ran out of names outside full
sectors, so HOOD, XOM and APO were held past a full sector, listed as overflow). Pin notes: MA is **SOFT-vetoed**, held and flagged. The same
date and N without preferences: PSX 0.300, BF.B 0.232, HOOD 0.186, MCD 0.164, UDR 0.118, no relaxation.

**4. The quant benchmark** (scratch copy of the pilot database, `build-risk-model` then `optimize` at 2026-07-09, default N; 20 assets: name cap
`1.5/20 = 0.075`, sector cap 0.30, no relaxation; `verify_t138.py` on the `opt-v2` books: **0 violations**, also at a 1e-9 tolerance):

| book | `opt-v1` min / max weight | `opt-v1` vol | `opt-v2` min / max weight | `opt-v2` vol | names held | max sector |
|---|---|---|---|---|---|---|
| `min_var` | 0.0500 / 0.0500 | 0.1425 | 0.0080 / 0.0750 | 0.1250 | 18 | 0.232 |
| `tangency` | 0.0500 / 0.0500 | 0.1425 | 0.0091 / 0.0750 | 0.1331 | 20 | 0.233 |
| `target_vol` | 0.0500 / 0.0500 | 0.1425 | 0.0068 / 0.0750 | 0.1562 | 18 | 0.300 |
| `risk_parity` | 0.0466 / 0.0503 | 0.1418 | 0.0261 / 0.0750 | 0.1310 | 20 | 0.230 |

The books are no longer 0.05 in every name, and `min_var` buys 1.75 pp of volatility with the freedom. With a panel of 30 or more the caps are
the old 0.05 / 0.30 and a feasible book is unchanged; `opt-v2` differs on a smaller panel (above), and `risk_parity` is now also held to the
sector cap.

### What the tilt does to the weight spread

**The tilt spans the held names' own score range.** The lowest held score maps to `0.5/n_held` and the highest to `1.5/n_held` whatever the
scores' spread, so the *target* weight spread is always the full `[0.5/N, 1.5/N]` band even when the scores are tightly clustered: a weight
carries a name's **position inside the held set's score range**, not the size of the score gap. Two cohorts whose scores differ by 10 points or
by 0.1 point get the same weights. Evidence: on four real replay dates (the three tightest held score ranges, 10.40, 10.48 and 11.16 points, and
the widest, 25.51) compressing every score toward the cohort mean by 10x and by 100x (held range down to 0.10 point) left the weights unchanged
(maximum difference 5e-15). The measured **realized** spread after the exact projection onto the caps is smaller than the band where the caps
bind: as a fraction of the band's width `1/n_held`, **median 0.894 at N = 10** (min 0.611, max 0.999; the sector cap binds on 88 of 144 dates)
and **median 0.941 at N = 20/30** (min 0.838, max 1.000). Measured score dispersion of the held names (blended score, 0-100): N = 10 range
10.40 / 16.53 / 25.51 (min / median / max), sd median 5.03; N = 20 and 30 range 17.87 / 36.69 / 49.32, sd median 9.03; production `cycle_run` 1
(N = 10) range 20.3, sd 5.92. Consequence for reading a result: the tilt is a *ranking* tilt, which is what an equal-weight core with a bounded
tilt means; if a score-magnitude-sensitive weighting is ever wanted, that is a different scheme (`score_proportional` stays selectable).

### Known scope and residual

- **Production is not re-selected** (above); the stored `cycle_run` 1 book keeps its breaches until the user directs a re-select.
- A run recorded before `T-136` has no `n_held`/relaxations in `params_json`; `verify_t138.py` checks it against the legacy caps it recorded.
- The pilot sample cannot exercise the defect's own shape or N = 30 meaningfully (15-19 eligible names); `T-100`'s full-universe ingestion and
  replay is where N = 20 and 30 differ.
- Assets with no sector are uncapped in the quant benchmark (as `quant.optimize` always treated them): 0 of 503 in production, 0 of 20 in the pilot.
- `scripts/verify_t138.py` (read-only) and `tests/test_verify_t138.py` (16, mutation-checked) are the record's tooling; `T-100`'s fresh-database
  run should be checked with it.

## T-141 — Equal composite weights: 1/3 each, SEMANTIC out of the defaults (Work item 8, step 9)

**Status.** Implemented on `feat/t141-equal-composite-weights` (PR number in the status commit). Methodology change (constitution AI behavior
#12): the composite blend's default weights. Everything below was measured on **scratch copies** under `/tmp/t141/` (listed at the end);
production `financial.db` was only read, and its SHA-1 is the same before and after (`5c5c0642619bcc2f80aa72f13599bf824f676675`).

### Symptom

`cycle.config._DEFAULT_WEIGHTS` blended FUNDAMENTAL 0.4 / VALORIZATION 0.3 / TECHNICAL 0.2 / SEMANTIC 0.1, while the earlier decision (PLAN §5.3) was
equal weights; neither choice was ever recorded or justified. SEMANTIC has never been written (`components_json` carries `"SEMANTIC": null` on all
2,799 pilot ranking rows), so its 0.1 was a placeholder the renormalization silently discarded: the **effective** blend was 0.444 / 0.333 / 0.222.

### Reference and decision (user, 2026-10-05)

FUNDAMENTAL, VALORIZATION and TECHNICAL at **1/3 each**; SEMANTIC is removed from the defaults until Work item 4 produces it. With no basis to
prefer one component's out-of-sample information over another's, the naive 1/N combination is hard to beat out of sample (DeMiguel, Garlappi &
Uppal 2009, "Optimal Versus Naive Diversification", *Review of Financial Studies* 22(5), 1915-1953), and it matches Work item 18's equal-weight
core. `soft_veto_penalty` (15 points per SOFT veto), the T-1 veto lag and the book construction are untouched. `_blended` still renormalizes over the
components an asset has.

### Fix

- `src/cycle/config.py`: `_DEFAULT_WEIGHTS = {FUNDAMENTAL, VALORIZATION, TECHNICAL: 1/3}`. `orchestrator._blended` is unchanged (it only iterates the
  configured weights, so stored SEMANTIC rows are ignored until the weights name SEMANTIC).
- **Resume guard** (`src/cycle/state.py` `check_score_weights`, `ScoreWeightsMismatch`; called in `orchestrator._run` for every cycle type, before
  `open_cycle`; reported by the CLI like `ConstructionMismatch`, exit 1). `params_json.score_weights` is written **once**, by `open_cycle` on a run's
  first attempt (`settings.model_dump(...)`); the upsert's `ON CONFLICT` never rewrites `params_json`, and nothing else writes that key. T-136's
  `check_construction` compared N, scheme, caps and preferences but not `score_weights`, so a run started under the old weights and resumed under the
  new ones would have ranked with weights it did not record, and `v_weight_scheme`, `v_weight_component` and `v_cycle_ranking_component` (which
  read `params_json.score_weights`) would have disagreed with the blend. A resume with other weights is now refused with the changed keys named; a run
  that recorded no weights is left alone. There is no CLI flag for `score_weights`: a run recorded under `0.4/0.3/0.2/0.1` is refused by the new
  defaults and is re-run from scratch (`backfill --force` for a `REPLAY` range) or on another date.

### Consequence for the read contract

- `v_weight_component`: **three rows per new run** (FUNDAMENTAL, VALORIZATION, TECHNICAL at 0.3333), no SEMANTIC row; runs stored earlier keep their four
  rows. `v_weight_scheme.weights_json` has three keys for a new run.
- `v_cycle_ranking_component`: **unchanged**. It lists the non-null components of each ranking row, and SEMANTIC was always null; a new run's
  `components_json` simply has no SEMANTIC key. `effective_weight` is the configured weight over the sum of the row's non-null components'
  weights, as before (an asset missing a component: 0.5 / 0.5).

### Verification

**(c) The claim the task rests on: one common scale.** Confirmed in the code. In `orchestrator._normalize`, TECHNICAL, VALORIZATION (and SEMANTIC)
are each `normalized_scores(raw)` over that date's cohort, and FUNDAMENTAL is `normalized_scores(raw)` over each asset's latest public filing
snapshot (`data.latest_fundamental_rows`); `normalized_scores` is `z_to_score(cross_sectional_z(raw, winsor=0.02))` = `clamp(50 + 10z, 0, 100)`
with z against the **winsorized** mean and sd. All three are on that scale, so equal weights are equal weights on one scale. Two precisions the
measurements below make visible: the sd of a component is **not exactly 10** (the z uses the winsorized sd but the values themselves are not
winsorized), and equal *weights* are not equal *variance shares* once the components are correlated.

**(a) The pilot replay** (scratch copy of `data/pilot/financial_pilot_replay.db`: 147 `cycle_run` rows, 2,799 ranking rows; the analysis covers the
**144 REPLAY cycles, 2024-01-05 -> 2026-10-02, 2,739 rows**, 20 assets, 18-20 ranked per date; the 3 other runs, 60 rows, are the pilot's
SELECTION/MONITORING). Both blends are recomputed offline from each row's stored `components_json` and `veto_rules_json` with `orchestrator._blended`
and the 15-point penalty per SOFT veto (a rule that is neither `HARD` nor `UNSCORED`). **Check on the method: the old weights reproduce the stored
`blended_score` of every one of the 2,739 rows exactly (max |difference| 0).**

| component (2,739 values each) | median per-date sd (min - max) | pooled mean / sd | value at 0 or 100 |
|---|---|---|---|
| FUNDAMENTAL | 11.85 (10.02 - 13.73) | 50.50 / 11.90 | 0 (0.00%) |
| VALORIZATION | 10.60 (10.01 - 11.46) | 50.19 / 10.68 | 0 (0.00%) |
| TECHNICAL | 10.60 (10.02 - 12.58) | 49.95 / 10.72 | 0 (0.00%) |

(population sd per date, then the median over the 144 dates; observed ranges FUNDAMENTAL 21.2 - 88.8, VALORIZATION 27.2 - 76.0, TECHNICAL 17.0 - 77.2.
The clamp cannot bind on a 20-name cohort: |z| = 5 is unreachable.)

| pair (Pearson) | pooled (n = 2,739) | median per date (min / max, 144 dates) |
|---|---|---|
| FUNDAMENTAL ~ VALORIZATION | -0.057 | -0.018 (-0.459 / +0.265) |
| FUNDAMENTAL ~ TECHNICAL | +0.212 | +0.234 (-0.369 / +0.650) |
| VALORIZATION ~ TECHNICAL | -0.113 | -0.114 (-0.585 / +0.418) |

The components are close to independent, so the blend diversifies; but FUNDAMENTAL has the widest spread (sd 11.9 against 10.6), and the share of the
blend's **variance** each component carries (median per date) was FUNDAMENTAL / VALORIZATION / TECHNICAL = **0.625 / 0.229 / 0.150** under the old
weights and is **0.412 / 0.251 / 0.336** under 1/3 each. Equal weights bring the three much closer; they do not make the variance shares equal
(VALORIZATION is anti-correlated with the others, FUNDAMENTAL is the widest).

**Blended from fewer than three components: 0 of 2,739 asset-dates.** Every row has all three; no row has a non-null SEMANTIC. (So the renormalization is
exercised in the pilot never; it is covered by `tests/test_cycle_weights.py`.)

Old (0.4/0.3/0.2/0.1, SEMANTIC null, effective 0.444/0.333/0.222) against new (1/3 each); eligible = not HARD-vetoed and not UNSCORED (median 18 per date,
15 - 19):

| | median | min |
|---|---|---|
| Spearman rank correlation per date, eligible names (144 dates) | **0.9772** | 0.9174 |
| Spearman rank correlation per date, all ranked names | 0.9774 | 0.9216 |

| N | plain top-N overlap, eligible (`|∩| / min(N, eligible)`): median / mean / min | the cycle's own book builder (`build_book`, default `score_tilt`): held-name overlap median / min | one-way weight turnover between the two books: median / max |
|---|---|---|---|
| 30 (the `top_n` recorded in all 144 runs) | 1.000 / 1.000 / 1.000 | 1.000 / 1.000 | 0.0174 / 0.0366 |
| 10 | 1.000 / 0.961 / 0.800 | 1.000 / 0.800 | 0.0418 / 0.1449 |
| 5 | 1.000 / 0.919 / 0.400 | 1.000 / 0.600 | 0.0752 / 0.3907 |

**N = 30 is trivial on this data**: every run recorded `top_n = 30` and a 20-asset universe has at most 19 eligible names, so the top 30 is every
eligible name under both blends and the overlap is 100% by construction (the recorded books were `score_proportional`, caps 0.10/0.30). What the
weights do change at N = 30 is the weights (turnover 0.017 median), through the score tilt. The informative rows are N = 10 and N = 5 (extra, not
recorded in the runs). The sector-aware fill makes the real book differ from a plain top-N (at N = 5 a sector is full at one name, at N = 10
at three), which is why `build_book` is called on both rankings and its held names are the second column: the medians agree (1.000), and the worst date
holds 0.6 of the same names at N = 5 against 0.4 for plain top-5.

**(b) A full cross-section: refused, not forced.** A MONITORING cycle with the branch code on a scratch copy of production at **2026-09-22**
(`cycle monitor --analysis-date 2026-09-22 --allow-dirty`, `KG_FINANCIAL_DB`, `KG_UNIVERSE_DB` and `--db`/`--universe-db` all pointing to scratch
files; `--allow-dirty` because the working tree had untracked files) **refused**: `483/503 universe members (96.0%) have no FUNDAMENTAL score at all,
over the 5% limit` (`TooManyUnscored`, the T-119 circuit breaker, raised by `rank`). Reason: production holds fundamentals for the pilot's **20** assets
only (`score_snapshot` FUNDAMENTAL: 20 assets; `fundamental_metrics`: 20), against 503 assets with prices through 2026-09-29. The price-spine guard
accepted 2026-09-22 (the spine ends 2026-09-29). Two preparations on the scratch copy only: `fundamental_agent migrate` (production's schema predates
migration v9, which the cycle requires) and a scratch 503-ticker `universe.db` built from the copy's `assets` (the real `universe.db` is the pilot's 20
tickers and was neither used nor modified). The 503-row old-vs-new comparison therefore **cannot be made until the full-universe fundamentals exist
(`T-100`/`T-143`)**; the refusal is the answer, nothing was forced.

What the steps before `rank` did write on the 503 names is still a (partial) cross-section: TECHNICAL, normalized over 503 names: sd **10.18**, mean
49.99, range 25.3 - 72.0, **0** values at 0 or 100 (so the clamp does not bind on a 503-name cohort either). VALORIZATION: **every one of the 503
names is exactly 50.00 (sd 0)**: only 20 assets have metrics, the other 483 share one raw value, and the winsorized sd of a 483-fold tie is 0, so
`cross_sectional_z` returns zeros. That is a data-coverage effect of production, not of this change (a database with fundamentals for every name would not show it),
but it is the degenerate-cohort failure mode `docs/cycle.md` now records: such a component carries 1/3 of the weight and discriminates nothing.

### Residual scope / deliberately deferred

- The 503-name comparison and the clamp rate on real, heavy-tailed fundamentals wait for the fresh full-universe database (`T-100`/`T-143`).
- A degenerate cohort (nearly every name tied on a component) silently yields a constant component; a coverage floor for VALORIZATION/FUNDAMENTAL
  is not part of this change (TECHNICAL's is `T-070`).
- Existing runs keep the weights they recorded; production is not re-selected by this change.
- The verification script is a throwaway (not committed).

Scratch paths: `/tmp/t141/replay.db` (copy of `data/pilot/financial_pilot_replay.db`), `/tmp/t141/prod_scratch.db` (copy of production, then migrated,
then the refused MONITORING run), `/tmp/t141/universe_full.db` (503 tickers from that copy's `assets`).


---

## T-077 — Carhart four-factor μ with Vasicek-shrunk betas, a live turnover cap and a turnover cost (Work item 8, step 6)

**Status**: Implemented on `feat/t077-carhart-mu-turnover` (SPEC 1.5.0, FR-009/FR-010); equilibrium stays the default. The
equilibrium-vs-carhart **portfolio** comparison ran on the **20 pilot names only**, because the production copy has no cover-page share counts
(see "What could not be verified"). Supersedes Work item 3 (`T-020`–`T-026`).

### Symptom

Checked against a scratch copy of production (`data/financial.db`, sha1 `5c5c0642619bcc2f80aa72f13599bf824f676675` before and after), 2026-10-10:

- **One expected-return estimator carried the return-aware objectives.** `equilibrium`'s cross-sectional dispersion on the 20-name panel is small (mean
  sd **1.57 pp**, values 6.6–16.0% over the 20 monthly dates) and it is cap-weighted by construction; `james_stein` is a **constant on 5 of 20 dates** (sd
  0.0000, 2025-01 to 2025-05: the sample means' dispersion is below its estimation noise) and 1.0–13.9 pp wide otherwise; `hist_mean` is 14–26 pp wide.
  There was no estimator driven by factor exposure rather than by 3 years of sample means or by market cap.
- **`--turnover-cap` was inert.** `Constraints.w_prev` is declared in `quant/optimize.py` and assigned nowhere under `src/` (`git grep w_prev=` at `774a315`:
  only `replace(c, turnover_cap=None, w_prev=None)`), and `_w_constraints` adds the constraint only when both are set; so the flag was accepted and changed
  nothing. `tangency` nevertheless switched to its frontier scan whenever a cap was set.
- **`evaluate` charged no cost for trading**, so any comparison of two books was gross of what moving between them costs.
- **Books of different estimators overwrote each other.** A book is keyed `(as_of, kind, frontier_k, engine_version)`; none of those carries the estimator (or the
  cap), so `optimize --mu james_stein` after the default updated the same row in place.
- **Audit Q3.** `rates.load_risk_free` with a CSV, for an as-of before the series' first date: `usable = [...] or points` then `usable[-1]` returned the
  series' **newest** row. Reproduced at `774a315` with a series starting 2026-01-01 and an as-of 2025-12-31: `rate_date 2026-06-01, annualized_rate 0.045`
  (a rate that did not exist yet).
- **Audit Q5.** `max_sharpe`'s y-space program is infeasible when the caps force a book whose excess return is not positive although one name's is (three names
  capped at 1/3, μ = (10%, 0, 0), rf 4%): `OptimizeError: no solver converged (SCS:infeasible)` at `774a315`.

### Root cause

Not a defect of a number but missing capability plus one dead path: the factor-model estimator was planned (Work item 3, then Work item 8 step 6) and never
built; `w_prev` was never populated from the previous book; the book key predates `--mu` having more than one meaningful value; `evaluate` was written
for gross comparison. Re-derived from the code, not from the audit's wording; Q3's mechanism is `or points` + `usable[-1]` (the newest rate, not the first).

### Theoretical/technical reference

- Carhart, M. M. (1997), "On Persistence in Mutual Fund Performance", *Journal of Finance* 52(1), 57–82 — the four-factor model (market, size, value, momentum).
- Fama, E. F. & French, K. R. (1993), "Common risk factors in the returns on stocks and bonds", *Journal of Financial Economics* 33(1), 3–56 — Mkt-RF, SMB, HML.
- Vasicek, O. A. (1973), "A Note on Using Cross-Sectional Information in Bayesian Estimation of Security Betas", *Journal of Finance* 28(5), 1233–1239 — shrinking a
  beta toward the cross-sectional mean with weight `σ²/(σ² + se²)`.
- Kenneth R. French Data Library (`mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html`): "Fama/French 3 Factors [Daily]" and "Momentum Factor (Mom)
  [Daily]", both *"created by using the 202608 CRSP database"*, downloaded 2026-10-10; SHA-256 in `src/quant/data/manifest.json`.

### Fix

`quant/factors.py` (loader, OLS, Vasicek, premia, μ), `quant/turnover.py` (the chain, the previous book, the cap's constraint and its relaxation), `quant/persist.py`
(the estimator beside the other three in `build-risk-model`, `rm-v1` → `rm-v2`; `w_prev` and the realized turnover in `optimize`), `quant/evaluate.py`
(`perf-v2` → `perf-v3`, the cost), `quant/rates.py` (the refusal), `quant/optimize.py` (the turnover constraint with weight outside the panel, `min_turnover`, the Q5
fallback), `quant/cli.py` (`--mu carhart`, `--turnover-cap` validated in (0, 2], `evaluate --turnover-cost-bps`). Full method: `docs/quant.md` ("`factors.py`", "`turnover.py`"),
`SPEC.md` FR-009/FR-010.

### Design decisions

1. **Data vendored byte-for-byte, SHA-256 on load, no network.** A mismatch fails the build (it is not downgraded to "carhart unavailable"): data that is not the
   pinned data is not estimated from. The files are stored with `.gitattributes -text` so git cannot change their line endings. Percent → decimals on load; rows after
   the as-of are not parsed into the result.
2. **Log → simple before the regression; French's RF for the excess return.** The factors are simple returns, and `Mkt-RF` is measured against French's own RF.
3. **Vasicek per factor, prior = equal-weighted cross-sectional mean, prior variance = cross-sectional variance (`ddof = 1`)**, `w = σ²/(σ² + se²)` — as specified;
   `PLAN.md`'s `1 − Var(β̂_i)/(Var(β̂_i) + Var(β̄))` is the same expression. Declared limitations: the four betas are shrunk separately; the prior variance contains the
   sampling noise (a textbook Vasicek prior subtracts it), so `w` errs low.
4. **λ̄ from 1963-07-01** (Fama–French sample start), arithmetic mean × 252. The intercept is estimated and dropped (no alpha in μ).
5. **Overlap, not the as-of, bounds the regression.** A panel past the file's last date is regressed on the overlap and the gap recorded; under 504 dates → carhart
   unbuilt, the other three built, the reason in the model's manifest, `optimize --mu carhart` refuses.
6. **A chain is a configuration over time.** The previous book is the newest earlier one with the same `(kind, frontier_k, engine_version)`, and `engine_version`
   carries the estimator and the cap as suffixes (`+mu-carhart`, `+to-0.5`) while the default keeps its key. That makes books of different estimators/caps coexist (which
   the verification needs) and makes "previous book" well defined. It also means `v_quant_portfolio.is_current` can flag a variant (below).
7. **The frontier sweep is not turnover-constrained** (its points are keyed by risk model, not chain), `risk_parity` ignores the cap (as before), and `tangency` under a
   binding cap is the best-Sharpe point of a frontier scan (status `from_frontier`). The default `target_vol` is 1.25 × the min-variance volatility computed **without**
   the turnover constraint, so a cap changes the book, not the target.
8. **Cost on target weights**, once, on a book's first forward day; the first book of a chain pays `bps × Σ|w|`; the `live_book` snapshot and the benchmark pay none.

### Verification

Everything below is on scratch copies under `/tmp/t077/`; production was opened by nothing but `cp`.

**Preparation.** `fundamental_agent migrate` on the copy (v9, v10 applied). `quant backfill-actions --from 2022-01-01 --analysis-date 2026-09-29` against the
deployed gateway: 499 of 499 assets fetched (7,391 dividends + 69 splits; 7 new rows — 426 assets already had actions). `quant build-returns`: 499 assets, 567,541
new rows, 407 with dividends. Production's `universe.db` holds only the **20 pilot names**, so for the 499-name panel `KG_UNIVERSE_DB` pointed at a scratch copy of
`universe_history.db` (894 stints: 503 live snapshot + 391 Wikipedia-change backfill; point in time, so HOOD enters 2025-09-22, APO 2024-12-23, …); for the 20-name path
`KG_UNIVERSE_DB` pointed at a copy of the real `universe.db`. The 20 monthly as-ofs are the last trading day of each month from 2025-01-31 (the first with 756 days of
history; 2024-12-31 has 753) to 2026-08-31, evaluated to 2026-09-29. All product runs were from clean commits (`quant_run.code_version` `0a27fb9` / `c0bb2dc`, no `-dirty`).

#### What could not be verified

`build-risk-model` on the 499-name panel **refuses** (`no panel asset has a market cap as of 2025-01-31`): the market cap rests on `filing_cover_shares` (T-132), which
`fundamental_agent` fills as it ingests a filing, and production's table is **empty** (its filings predate T-132). Backfilling it for all ~2,600 filings since 2024-07 from the
EDGAR gateway (the product's own client and parser) ran at 4–10 filings a minute (192 filings in ~35 minutes) and was stopped by decision; for the 20 pilot names the 156
missing filings were fetched (0 errors, ~8 minutes), so on the 20 names the whole product path runs. **Therefore the equilibrium-vs-carhart portfolio comparison ran on 20
names only; on 499 names only the estimator was called, directly, with no market caps and no `equilibrium`.** (A second, smaller finding on the 499-name panel: GEHC has a
price gap the panel cannot heal on 2025-11-28 — `build_return_panel` raises — so the product's `build-risk-model` would refuse that date too; the direct call drops it
there and records it.)

#### 1. The 499-name panel, the estimator only (`scripts/verify_t077.py diagnostics`)

The same gate (`settings_gate`), the same panel (756 days, ≥ 504 observations) and the same factor data as the model, `rf` 4.5%. N 464–490 per date; **0 assets flagged
under 504 observations** on every date (the panel is dense by construction: a name needs ≥ 741 of 756 days, so the flag is a guard; it did not fire), **0 panel dates past the
factor file** (every as-of ≤ 2026-08-31 = the file's last date). Per-date tables: `docs/t077_verification/diagnostics_499.md`. **`equilibrium` is not available here.**

| estimator | mean of the cross-sectional mean | mean cross-sectional sd (range over dates) | mean min .. mean max |
|---|---|---|---|
| hist_mean | 12.13% | 18.33 pp (14.9–21.4) | −49.1% .. 105.5% |
| james_stein | 12.13% | 1.24 pp (**0.00**–5.02; constant on 10 of 20 dates) | 8.5% .. 18.9% |
| carhart | 10.95% | 3.87 pp (2.90–5.20) | 3.3% .. 22.7% |

Correlation of carhart μ with `hist_mean`: Pearson 0.49 / Spearman 0.46 (0.22–0.72 over dates); with `james_stein`: 0.59 / 0.55 (0.47–0.72; **undefined on the 10 dates
where `james_stein` is constant**).

| factor | λ̄ (1963→D) mean (range over dates) | β̄ (prior mean) | σ² (prior variance) | median shrinkage weight | lowest single weight |
|---|---|---|---|---|---|
| MKT | 7.21% (7.01–7.35) | 0.895 (0.883–0.908) | 0.157 (0.100–0.208) | 0.979 (0.974–0.981) | 0.67 |
| SMB | 1.08% (1.02–1.18) | 0.075 (0.051–0.109) | 0.089 (0.077–0.097) | 0.924 (0.916–0.930) | 0.33 |
| HML | 3.65% (3.52–3.77) | 0.267 (0.251–0.279) | 0.177 (0.167–0.187) | 0.970 (0.959–0.979) | 0.51 |
| MOM | 7.16% (7.05–7.43) | −0.142 (−0.165 – −0.106) | 0.078 (0.048–0.155) | 0.954 (0.941–0.976) | 0.52 |

On 756 daily observations a beta's sampling variance is small next to the cross-sectional spread of betas, so **the shrinkage is mild** (median weight 0.92–0.98): it matters for the
noisiest SMB and MOM betas (weights down to 0.33) and barely elsewhere.

**(b) Premia sensitivity** (the same shrunk betas; μ under the 1963 start vs the factor file's own 1926 start vs the mean of the last 756 days):

| premia | median change in μ vs 1963 | rank correlation with the 1963 μ |
|---|---|---|
| 1926 start | +0.64 pp (+0.61 … +0.71) | 0.998 (min 0.997) |
| last 756 days | +3.87 pp (−2.25 … +6.98) | 0.830 (min 0.525) |

The start year moves the level by ~0.6 pp and the ranking not at all; the 756-day window moves the level by several points and the ranking materially (over the last 756 days SMB
averaged −4 to −7% a year, MKT 6–18% and MOM 3–17%, against 1.1%, 7.2% and 7.2% since 1963). The estimator ranks assets by factor exposure far better than it prices them.

#### 2. The 20 pilot names, end to end through the product

`build-risk-model` (`rm-v2+9d34ff69`, N 17–20, caps 17/17, 19/19 or 20/20 assets, share-count age median 27–98 days, max 123) on all 20 dates; the stored carhart μ equals a direct call of the
estimator to 1e-9 on every date. `optimize --objectives min_var,tangency,target_vol,frontier` with `--mu equilibrium` and `--mu carhart`, each at no cap, at `--turnover-cap 0.5` and
(beyond the brief) at 0.1: 120 runs, 360 books and 120 frontier sweeps, in **two database copies, one per estimator**, because the frontier points are keyed by risk model and would
overwrite each other. `evaluate --from 2025-01-31 --analysis-date 2026-09-29` wrote `perf-v3` rows for 187 books per database. Per-date tables: `docs/t077_verification/diagnostics_20.md`,
`chains.md`.

**(a) on the 20 names** (equilibrium available):

| estimator | mean of the cross-sectional mean | mean sd (range over dates) | mean min .. mean max |
|---|---|---|---|
| hist_mean | 6.76% | 19.81 pp (13.6–25.6) | −27.9% .. 59.8% |
| james_stein | 6.76% | 5.71 pp (**0.00**–13.9; constant on 5 of 20 dates) | −2.7% .. 24.0% |
| equilibrium | 9.26% | **1.57 pp** (1.37–2.09) | 7.1% .. 13.3% |
| carhart | 10.02% | **4.61 pp** (3.39–5.63) | 5.0% .. 20.5% |

Correlation of carhart μ with `equilibrium`: Pearson **0.85** / Spearman 0.80 (0.71–0.92); with `hist_mean` 0.53 / 0.49; with `james_stein` 0.63 / 0.58 (undefined on the 5 constant dates).
λ̄ is the same as above (the premia do not depend on the panel). Flagged: 0 on every date. Gap: 0.

| factor | β̄ (20 names) | β̄ (499 names) | σ² (20) | σ² (499) | median w (20) | median w (499) |
|---|---|---|---|---|---|---|
| MKT | 0.769 | 0.895 | 0.193 | 0.157 | 0.983 | 0.979 |
| SMB | −0.046 | 0.075 | 0.096 | 0.089 | 0.917 | 0.924 |
| HML | 0.458 | 0.267 | 0.152 | 0.177 | 0.966 | 0.970 |
| MOM | −0.224 | −0.142 | 0.055 | 0.078 | 0.933 | 0.954 |

**How noisy the prior is on a small panel** (`docs/t077_verification/prior_comparison.md`, per date): the 20-name prior mean differs from the 499-name one by 0.126 (MKT), 0.122 (SMB), 0.191 (HML) and 0.082
(MOM) in absolute terms on average — 0.13 of a unit market beta, and for HML 71% of the large-panel value — and the prior variance is 1.27× (MKT), 1.33× (SMB), 0.86× (HML), 0.68× (MOM) the large-panel one in the median
(range 0.36–1.64×). The 20 names are a defensive/value tilt (low MKT, high HML, strongly negative MOM), so their prior is a poor stand-in for the market's, and their shrinkage target is not the market beta.

**(c) Chained monthly series** (computed in `verify_t077.py chains`, not in the product): each monthly book held at its target weights until the next as-of (the last until 2026-09-29), net of the cost on each
book's first day (the first pays `bps × Σ|w|` = 10 bps), 20 books, 417 days, `rf` 4.5%; Sharpe = (mean daily × 252 − rf)/vol. Cap 0.5 never binds on this sample (the largest realized monthly turnover is 0.29 for `min_var` and `tangency`, 0.38–0.39 for `target_vol`), so its rows differ from "none" only through `tangency`'s frontier-scan path; cap 0.1 holds
11–12 of the 60 books per estimator at the cap, 3 of them relaxed (2026-07-31, when three HARD-vetoed names holding 11–14% of the previous book left the panel, so no feasible book trades less than 0.23–0.29). Costs at 10 bps; the last column is 15 bps (return / Sharpe).

| objective | estimator | cap | ann. return | vol | Sharpe | max DD | mean monthly turnover | at 15 bps |
|---|---|---|---|---|---|---|---|---|
| min_var | equilibrium | none | 7.31% | 13.31% | 0.26 | -9.55% | 0.065 | 7.24% / 0.25 |
| min_var | equilibrium | 0.5 | 7.31% | 13.31% | 0.26 | -9.55% | 0.065 | 7.24% / 0.25 |
| min_var | equilibrium | 0.1 | 7.30% | 13.31% | 0.26 | -9.55% | 0.065 | 7.23% / 0.25 |
| min_var | carhart | none | 7.31% | 13.31% | 0.26 | -9.55% | 0.065 | 7.24% / 0.25 |
| min_var | carhart | 0.5 | 7.31% | 13.31% | 0.26 | -9.55% | 0.065 | 7.24% / 0.25 |
| min_var | carhart | 0.1 | 7.30% | 13.31% | 0.26 | -9.55% | 0.065 | 7.23% / 0.25 |
| tangency | equilibrium | none | 9.46% | 13.74% | 0.40 | -12.04% | 0.046 | 9.40% / 0.40 |
| tangency | equilibrium | 0.5 | 9.36% | 13.78% | 0.39 | -12.15% | 0.048 | 9.29% / 0.39 |
| tangency | equilibrium | 0.1 | 9.55% | 13.74% | 0.41 | -12.05% | 0.044 | 9.49% / 0.40 |
| tangency | carhart | none | 11.72% | 15.94% | 0.49 | -14.45% | 0.070 | 11.65% / 0.49 |
| tangency | carhart | 0.5 | 11.45% | 15.94% | 0.48 | -14.46% | 0.069 | 11.37% / 0.47 |
| tangency | carhart | 0.1 | 11.36% | 15.91% | 0.47 | -14.45% | 0.066 | 11.29% / 0.47 |
| target_vol | equilibrium | none | 12.77% | 16.07% | 0.55 | -15.50% | 0.097 | 12.67% / 0.54 |
| target_vol | equilibrium | 0.5 | 12.77% | 16.07% | 0.55 | -15.50% | 0.097 | 12.67% / 0.54 |
| target_vol | equilibrium | 0.1 | 13.45% | 16.03% | 0.59 | -15.50% | 0.083 | 13.37% / 0.58 |
| target_vol | carhart | none | 8.39% | 16.90% | 0.30 | -15.90% | 0.084 | 8.30% / 0.29 |
| target_vol | carhart | 0.5 | 8.39% | 16.90% | 0.30 | -15.90% | 0.084 | 8.30% / 0.29 |
| target_vol | carhart | 0.1 | 10.02% | 16.86% | 0.38 | -15.91% | 0.076 | 9.94% / 0.38 |
| frontier | equilibrium | none | 9.13% | 13.73% | 0.38 | -12.05% | 0.050 | 9.07% / 0.37 |
| frontier | equilibrium | 0.5 | 9.13% | 13.73% | 0.38 | -12.05% | 0.050 | 9.07% / 0.37 |
| frontier | equilibrium | 0.1 | 9.13% | 13.73% | 0.38 | -12.05% | 0.050 | 9.07% / 0.37 |
| frontier | carhart | none | 11.57% | 15.87% | 0.49 | -14.29% | 0.072 | 11.49% / 0.48 |
| frontier | carhart | 0.5 | 11.57% | 15.87% | 0.49 | -14.29% | 0.072 | 11.49% / 0.48 |
| frontier | carhart | 0.1 | 11.57% | 15.87% | 0.49 | -14.29% | 0.072 | 11.49% / 0.48 |

`min_var` is **identical under both estimators** at every cap: max |weight difference| **0.0**, max |daily return difference| **0.0**, equal turnovers. `frontier` is the stored frontier's best-Sharpe point and is not
turnover-constrained, so its capped rows equal the uncapped ones by construction. The script agrees with the product: for 3 objectives × 20 monthly holding windows, the largest difference between `evaluate`'s
`perf-v3` daily `realized_return` and the script's chain is 2.3e-8 (the product renormalizes the weights of a name with no later return; the script keeps them).

**(d) What this does and does not show.** About 20 months on 17–20 names is **descriptive only**; no significance claim is made (T-078 was deprecated for this reason). Carhart μ has about three times equilibrium's
dispersion and is strongly rank-correlated with it; the chained results move in both directions (carhart higher for `tangency` and `frontier`, lower for `target_vol`), and the cap/cost effects are small next to that.
`equilibrium` stays the default unless the user decides otherwise.

**Tests** (hermetic, on a fixture cut from the real files): `tests/test_quant_factors.py` (OLS recovers known betas; log→simple and percent→decimal; Vasicek weights near 1 / near 0, flagged asset gets the prior mean,
mean beta = β̄ under equal noise; no factor row after the as-of is read; the overlap refusal and recorded gap; the SHA-256 refusal), `tests/test_quant_carhart_pipeline.py`, `tests/test_quant_turnover.py` (previous-book
selection; the constraint holds including a name that left the panel; the relaxation is recorded; the cost once on the first day under `perf-v3`), `tests/test_quant_rates.py` (the rf refusal), `tests/test_verify_t077.py`,
and `tests/test_quant_import_isolation.py` still passes.

### Residual scope / deliberately deferred

- **Re-run note (not a new task): the full-universe comparison.** Once a database with cover-page share counts exists (`T-143`/`T-100`), repeat (a)–(c) on the full universe: `export KG_FINANCIAL_DB=<that db> KG_UNIVERSE_DB=<a point-in-time
  universe covering the priced names>`; `uv run python -m quant backfill-actions --from 2022-01-01 --analysis-date 2026-09-29`; `uv run python -m quant build-returns --from 2022-01-01 --analysis-date 2026-09-29`; for each
  month-end `D`: `uv run python -m quant build-risk-model --analysis-date D`; copy the database once per estimator; in each, for each `D` in order and for `--mu equilibrium|carhart` × `--turnover-cap` absent|0.5:
  `uv run python -m quant optimize --analysis-date D --objectives min_var,tangency,target_vol,frontier --mu <est> [--turnover-cap 0.5]`; then `uv run python -m quant evaluate --from 2025-01-31 --analysis-date 2026-09-29`
  and `uv run python scripts/verify_t077.py chains --run equilibrium:none=<db> --run equilibrium:0.5=<db> --run carhart:none=<db> --run carhart:0.5=<db> --end 2026-09-29 --out <dir>` (and `diagnostics --db <db>
  --universe-db <universe> --label <name> --out <dir>` for (a)/(b)). On 499 names expect `build-risk-model` to refuse 2025-11-28 (GEHC's price gap) until that is healed.
- `tangency` under a cap that does not bind still takes the frontier-scan path (so cap 0.5 differs from "none" for `tangency` only); returning the exact y-space tangency when its turnover is within the cap would remove the
  discontinuity. Not done: the brief asked to document the fallback.
- The frontier sweep takes no turnover cap and is keyed by risk model (a second `--mu` on the same model overwrites the points); a per-chain frontier would need a schema change.
- `v_quant_portfolio` has **no `ret_estimator` column** and none was added (no column changes, as asked); the estimator is in `engine_version`'s suffix and the book's `manifest_json`, and `is_current` can flag a variant book.
- The factor files are a 202608 vintage; a later vintage may differ slightly from what was public on a past date. Per-factor shrinkage, the equal-weighted prior, target-weight costs and the 20-month sample are declared limitations (`docs/quant.md`).

Scratch paths: `/tmp/t077/prod.db` (copy of production, migrated; backfilled actions and returns; cover shares for the pilot names and 192 other filings), `/tmp/t077/pilot.db` (+ `pilot_eq.db`, `pilot_cc.db`: 20 risk models, and the books and
`perf-v3` rows), `/tmp/t077/universe.db` (copy of `universe.db`), `/tmp/t077/universe_hist.db` (copy of `universe_history.db`), `/tmp/t077/factors_dl/` (the downloaded zips), `/tmp/t077/report/` (the generated tables),
`/tmp/t077/tools/` (the cover-share backfill and the driver, throwaway). Production `financial.db` sha1 `5c5c0642619bcc2f80aa72f13599bf824f676675` before and after.
