# T-143 part A — pilot_a3 (the re-run after T-159a/b/c)

**Tag:** `pilot-a-2026-10-23` on `564127c2d08a1158371b267f84f5dfc34c150b87` (master)
**Gateway:** data-mining `165aae804e339b259fadabc53c7b323c1ca8d517`
**Analysis date:** 2026-10-18
**Database:** `/workspaces/thesis/pilot_a3/financial.db` (copy) and
`/home/vscode/pilot_a3_run/financial.db` (local), sha256
`d2b757b4c22b25ce58890c87262104a04441a89c0a67b31c07ec04ca72468fb8`
**Engine:** `metrics-v10`; **code version:** `564127c`

The full run diary is `/workspaces/thesis/scratch/t143a/PROGRESS.md`; every output is under
`/workspaces/thesis/scratch/t143a/out/a3_*`.

## 1. P2 — the fresh database

The run used the brief's environment and order: prices at `priceobs-v2`, the Tiingo import, the
observation pass for the 33 Tiingo lines, `quant backfill-actions --from 2019-01-02`, then
`fundamental_agent run --metrics-only` twice (the first pass and the retry pass), `quality`, the end
canaries, the copy and the manifest.

| step | result |
|---|---|
| prices | 591 lines, **1,077,955** daily rows and observations |
| Tiingo import | 33/33 lines, **44,989** bars, 442 dividends, 1 split; `gateway-only 0` |
| observations (33 lines) | 33/33, **1,077,955** observations |
| corporate actions | 591 assets, **13,075** rows |
| fundamentals | **10,196 filings**, **2,176,580 facts**, **357,231 metrics** |
| quality | exit 0; **3,616** data-quality rows |
| drop log | **99,743** rows, sha256 `f3db171b…c0b6fb` |
| cover counts | **11,389** filing-cover rows |

The run gate is clean even though the database was built during three host/Docker restarts:

* the first launch at 03:29Z was reaped after 3,399 price rows; its partial DB is preserved as
  `financial.aborted_0329.db`;
* the 12:02Z and 16:54Z restarts (the last from the host running out of storage) were recovered by
  copying the DB and journal, then rolling the hot journal back; `PRAGMA quick_check` = **ok** each
  time;
* the resumed runs are 4 and 5 (`completed`); runs 1–3 remain `running` rows from the killed
  processes, and are reported as an artefact, not data. The final database has no hot journal.
* the two data-check runs (`pending --metrics-only`) say **`no 591 members current, 2184 listings;
  22 known data failure`**.

**Error rows:** 104 rows over 26 distinct filings: 87 `period`, 15 `process`, 2 `label` (the label
rows are CNP and CSX warnings). They are the known pilot_a2 data classes; the retry pass was run.

## 2. §6 — the brief’s list, item by item

| the brief’s item | result |
|---|---|
| code `564127c`, no `-dirty`; engine `metrics-v10` | **PASS** — `analysis_run.code_version` is `564127c`; all 357,231 metrics are `metrics-v10` |
| every a2 accession in a3, except possibly JPM 10-Q 2024 | **PASS, stronger** — **0** a2 accessions missing; JPM `0000019617-24-000611` is present (19 JPM filings in both) |
| new T-159b accessions, at least KR’s five Q1 10-Qs | **PASS** — exactly **7** new filings: KR 10-Q 2022-05-21 (`0001558370-22-010253`), 2023-05-20 (`0001558370-23-011335`), 2024-05-25 (`0001558370-24-009615`), 2025-05-24 (`0001558370-25-008960`), 2026-05-23 (`0001104659-26-078236`); AAP 10-Q 2022-04-23 (`0001158449-22-000176`) and 2023-04-22 (`0001158449-23-000142`) |
| cover-count scale flag: exactly 4 | **PASS** — CMG 10-Q 2022-03-31; PKG 10-Q 2023-03-31, 10-Q 2024-03-31, 10-K 2025-12-31 |
| MPC: 0 CON-08 quarantined rows | **PASS** — 0 |
| `verify_pilot` T-159c PASS | **PASS** — 3,490 filed-fact items, 33,087 `not_reported` values |
| `verify_pilot` stale-metrics 0 | **MISS** — WARN lists **7 JPM filings** (FY2024 … 2026Q2): run 1 metrics vs an earlier-period filing written in run 2, caused by the host restarts. The point-in-time check is 0 violations; declared as an artefact, not a lookahead |
| `unexplained_split_rows` 0 | **MISS** — **1: CTVA 2026-10-01**, the same declared pilot_a2 limitation (the completion filing does not state the ratio) |
| combined-event refusals 0 | **PASS** — 0 |
| `lines_gateway_only` empty | **PASS** — 0 |
| fidelity gate ≥ 98 % per item | **PASS** — 0 items below the gate; lowest item still above 98 % |
| production sha1s, archive, Tiingo and pilot_a2 sha256 unchanged | **PASS** — see §5 |

## 3. P3 — V1 to V6

### V1 — integrity

`verify_pilot`: **40 checks, 7 FAIL, 2 WARN** (16.9 s). The 7 FAILs are the same known classes as
pilot_a2:

1. T-108 — no bench-v2 series (N/A: part A runs no cycle/quant);
2. T-111 — no benchmark performance (N/A);
3. T-131 — one split-shaped day-over-day move (MRNA 2026-08-19, the declared real move);
4. T-140 — 27 `DQ_REVENUE_POS` filings (the known gateway-dropped revenue rows);
5. the valuation filings with a stored price but no market cap — **618**, exactly p2b’s list
   (T-132/K1);
6. WI-18 — no books (N/A);
7. T-153g — CTVA 2026-10-01 (the declared split row).

WARNs: the T-140 quarters with recorded run errors (APO, ARES, DELL, ENPH) and the JPM
stale-metrics artefact above.

`v1_integrity`: point-in-time **0** violations; identity **0** wrong CIK; CNP 2022Q1/2022Q3 correct
with no label collision; coverage 591 lines; observations: **1** line with an unexplained missing
session (FISV 2025-11-12, the declared vendor gap). Market-cap refusals **582**, all K1.

`v1_market_cap` / review: **72 events, 70 measured, 70 OK, 0 failed**, 2 not measured (EXC
2022-02-02, BDX 2026-02-10); **0 combined_corporate_action refusals**.

`v1_open_items` reproduces MRNA’s real move and FISV’s missing session. `tiingo_gateway_only` is
empty.

**Idempotency:** the P2 commands were re-run on `a3_copy.db`; **0 rows were written in every
table** (filings, facts, metrics, actions, cover shares, quality rows). The compare took 12.1 s.

### V2 — the SEC cross-checks

* **fidelity**: 10,188 filings compared, **0 items below the 98 % gate**, **63 mismatching
  (filing, item) pairs** (long_term_debt 33, D&A 17, revenue 13). The pairs are the known
  debt/D&A composition families — ESS/AVB/WELL long-term debt, GPN/MRSH/SYK D&A, and the ZION
  revenue rebuild — now including T-159a’s broader debt/D&A readings. The count rose from p2b’s 32
  for that reason; the gate passes.
* **signcheck**: **0 FLIPPED**; 20 interest-expense and 142 capex “other value” cases are the
  documented sourced-composition cases, with the sign agreeing (162 total).
* **precedence**: the raw committed check says `resolver == concept-first everywhere: False`,
  because it treats T-159a’s **substitute** items as plain: 775 receivables and 1,051 short-term
  investments are substitute reads (e.g. ALLE receivables = `AccountsNotesAndLoansReceivableNetCurrent`,
  concept-first `None`). Re-running the same check with substitute items excluded (the script’s own
  “composite/sum/fallback are not plain” rule) gives **True** and **0** mismatches
  (`out/a3_v2_precedence_plain.log`). This is a checker gap, not a data change.
* **itemcheck**: the raw counts show `OTHER_CONCEPT` for net_income 119, D&A 24, cash 740,
  pretax 23. The first three are T-159a’s **concept_flags** and resolver concepts the committed
  `own_concepts()` does not know: adding them gives net_income `OTHER_CONCEPT` **0** (WARN 11) and
  lowers D&A to 13 and cash to 542; the remainder are the known cash/pretax WARN classes (not on the
  plan’s V2 FAIL list). `revenue` keeps its 16 `FAIL_gateway_dropped` rows from the gateway drop log
  (the known F0 class).

The V2 tools therefore need a small follow-up for T-159a’s substitute/concept-flag tiers. The D&A
count falls from 24 to 13 after adding the known sourced-sum children; the remaining 13 are still
under concepts the committed `own_concepts()` does not know, so they are a checker follow-up, not a
new value change. No stored value was changed by this analysis.

### V3 — every metric recomputed independently

**357,231 rows compared; 357,195 agree; 36 mismatches**, all the known FOX/NWS multi-class market
capitalization (MKT-03: the row records one class’s count and price, not the sum over the classes);
**0 unexplained**. The summary is `out/a3_v3_summary.md`, results `out/a3_v3_results.csv`. The
summary’s “database” line is the script’s hard-coded `pilot_a2` label; the sha256 and the 10,196
filing counts are pilot_a3.

### V4 — every rule recomputed independently

**80,490 common rows, 0 hit disagreements, 0 severity disagreements.** The JSON’s 8,049
`disagreements` are all `EARNINGS_MISSING` `system_evaluated=0 / independent_evaluated=1`, the known
part A class (no FUNDAMENTAL score); the 200 opened examples in `a3_v4_compare.csv` show no hit
difference. The MPC `NEGATIVE_FCF` disagreement of p2b is gone (T-159c’s F8 fix).

### V5 — the 25 cards

225 comparisons: **190 match, 4 differ, 19 card empty (7 known, 12 by rule), 12 both empty**. The
four differences, all explained:

| ticker | item | card | reader | explanation |
|---|---|---:|---:|---|
| CINF | debt | 861 M | 886 M | `NotesPayable` 25 M is on T-159a’s rejected list; a financial firm’s debt is not applicable by rule |
| CPAY | debt | 10,001.687 M | 8,178.687 M | the reader’s own note: the 1,823 M securitization facility is paired with restricted receivables and is excluded from the three-part debt definition |
| CRWD | D&A | 250.218 M | 281.451 M | the `crwd_` PP&E D&A plus intangibles; the T-159 F5a known family |
| VMRK | capex | 1,115.442 M | 346.623 M | APP-04b’s REIT capex composite (development/acquisitions); by rule |

The p2b card set had 9 differences; the three card defects fixed by PR #180 are gone. Cards:
`out/a3_v5_cards/`, comparison `out/a3_v5_html_vs_card.*`.

### V6 — the earlier decisions re-measured

* **D-13, utilities’ 5 % check:** 29 utilities; `below_5_on_both_bounds` is **[]** in both the fresh
  database and the copy. The fresh has **27** utilities with both bounds (copy 26); **114 field
  differences** across ATO, AWK, CEG, D and DTE come from T-159a’s debt components. The decision
  does not change.
* multi-class: **0 differing rows**; REIT capex: **0 differing rows**; book-equity/D-14/DQ-02:
  **0 differing fields**.
* weights: spearman **0.995491** fresh vs **0.995434** copy; 550 vs 480 ranked (the scope).
  Unscored 30/591 vs 23/503 (5.08 % vs 4.57 %, the scope); fallback 10,196 vs 9,340; provenance
  357,231 vs 315,435 rows, with **0 empty rows without a reason** in both.
* `verify_t151c` has 12,846 rule-input pseudo-group rows without provenance (2,650
  `utility_cash_coverage_3y` + 10,196 `value_inputs`), the known class; every empty metric row has a
  reason.

No decision’s evidence changes materially; no V6 decision is reopened.

## 4. The a3-vs-a2 comparison

`out/a3_vs_a2_metrics.csv` — 357,231 metric rows:

| class | rows |
|---|---:|
| same | 300,661 |
| fill | 6,393 |
| emptied | 28 |
| changed | 11,885 |
| reason_only | 38,026 |
| only_a2 | 0 |
| only_a3 | 238 (7 new filings) |

The 28 emptied rows are the ROIC/ROIC-effective pair of **FTNT** (4 filings) and **P** (10
filings): T-159a now reads their short-term investments (FTNT 1,194 M; P’s classes), making invested
capital non-positive, so MET-09 empties the ratio. The old value used the zero default; the new
emptiness is the correct treatment, not a regression.

The 38,026 `reason_only` rows are the T-159c reason reclassification
(`missing_input` → `not_reported` 33,077, → `not_applicable` 2,775, → `denominator_not_positive`
1,296, → `capture_failure` 237, and the smaller classes).

Empty values by cause on the shared filings (common rows; price group separate). The full table
from `out/a3_vs_a2_summary.md`:

| row | side | cause | count |
|---|---|---|---:|
| nonprice | a2 | `missing_input` | 51,382 |
| nonprice | a3 | `not_reported` | 29,893 |
| nonprice | a3 | `not_applicable` | 20,624 |
| nonprice | a2 | `not_applicable` | 17,849 |
| nonprice | a3 | `missing_input` | 13,224 |
| nonprice | a2 | `capture_failure` | 3,549 |
| nonprice | a2 | `prior_year_missing` | 3,280 |
| nonprice | a3 | `prior_year_missing` | 3,179 |
| nonprice | a3 | `growth_base_not_positive` | 2,857 |
| nonprice | a2 | `growth_base_not_positive` | 2,679 |
| nonprice | a3 | `denominator_not_positive` | 2,452 |
| nonprice | a3 | `capture_failure` | 2,189 |
| nonprice | a2 | `(none)` | 1,493 |
| nonprice | a3 | `(none)` | 1,254 |
| nonprice | a2 | `denominator_not_positive` | 931 |
| price | a2 | `missing_input` | 5,996 |
| price | a2 | `not_applicable` | 4,606 |
| price | a3 | `not_applicable` | 4,606 |
| price | a3 | `prior_year_missing` | 3,319 |
| price | a3 | `not_reported` | 3,185 |
| price | a2 | `prior_year_missing` | 3,180 |
| price | a3 | `missing_input` | 2,345 |
| price | a2 | `capture_failure` | 1,910 |
| price | a3 | `capture_failure` | 1,363 |

Filings only in a2: **none**. Filings only in a3: the seven listed in §2.

`out/a3_vs_a2_items.csv` — **9,210** (accession, item) rows whose value, concept or `how` differs,
with both values/concepts, the a3 `how`/flags, and the SEC companyfacts value for the concept and
period (empty where the cache has no fact). The largest groups are D&A (2,362), short-term debt
(2,091), short-term investments (1,090), receivables (804), COGS (466), gross profit (447), interest
expense (394), long-term debt (359), inventory (348); the changes are the T-159a/b/c substitutions,
totals, maps and filed facts (e.g. APD short-term debt 112.9 M → 199.7 M, both equal to the SEC’s
facts).

## 5. Files, hashes and the final state

* **a3 local and copy**: `d2b757b4c22b25ce58890c87262104a04441a89c0a67b31c07ec04ca72468fb8`
  (identical).
* **production sha1** (unchanged): `financial.db` `5c5c0642619bcc2f80aa72f13599bf824f676675`;
  `universe.db` `04adf644f892597d5de1b60f11c0cd6cbc271ed1`; `universe_history.db`
  `fddbdccb71ab4243a0fb76b3027774474d352b99`; `universe_history_gics.db`
  `db5b16290b03a191b55215e8b98fc893a93f0e5b`.
* **archive** `6bddc77c3d69f428313af52ad99cf05c23a38cfcba6d1544c49e0dcf9383cd6a`;
  **Tiingo** `d6ef4052aac37fa1c1b584e0653c0e3abc3ff690bd12e64ed40a2ec8a7589179`; **pilot_a2**
  `0b5bc086a70169347efdb2d93ee3081509a7bd5537a91eea120628c185684f0c` (unchanged).
* **manifest**: `out/a3_manifest.json`; **final hashes**: `out/a3_final_hashes.json`.
* **P3**: `out/a3_p3_summary.json`, `out/a3_p3.log`; **comparison**:
  `out/a3_vs_a2_metrics.csv`, `out/a3_vs_a2_items.csv`, `out/a3_vs_a2_summary.md`.
* **V1**: `out/a3_v1_integrity.json`, `a3_v1_market_cap*.json`, `a3_v1_open_items.*`,
  `a3_v1_idempotency.json`, `a3_p3_v1_verify_pilot.log`.
* **V2**: `out/a3_v2_fidelity_golden.*`, `a3_p3_v2_signcheck.log`, `a3_p3_v2_precedence.log`
  (+ `a3_v2_precedence_plain.log`), `a3_p3_v2_itemcheck.log` (+ `a3_v2_itemcheck_patched.log`).
* **V3–V6**: `out/a3_v3_*`, `out/a3_v4_*`, `out/a3_v5_*`, `out/a3_v6_*`.

## 6. Acceptance

| layer | result |
|---|---|
| V1 | **7 FAIL, 2 WARN** — the known N/A/declared classes; one restart WARN (JPM); 0 new data FAIL |
| V2 | signcheck **0 FLIPPED**, fidelity **0 below the gate**; the raw precedence/itemcheck counts are tooling false positives/gaps from T-159a’s substitute/concept-flag tiers (plain precedence **True**; net-income OTHER 119→0; D&A 24→13; cash 740→542) — **follow-up needed** |
| V3 | **36 known** FOX/NWS market-cap mismatches; **0 unexplained** |
| V4 | **0 hit / 0 severity disagreements**; 8,049 known `EARNINGS_MISSING` evaluated-flag rows |
| V5 | **4 differences**, each explained; 19 card empty (7 known, 12 by rule) |
| V6 | all changes listed above; **no decision reopened** |

**Misses against the brief’s §6:** `stale-metrics` is a 7-row WARN (restart artefact), and
`unexplained_split_rows` is 1 (CTVA, the declared limitation). The raw V2 tools need the T-159a
substitute/concept-flag follow-up. Everything else in §6 passes.
