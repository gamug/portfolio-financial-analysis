# SEMANTIC score — cross-repo boundary & open worries

**Status:** draft, decision pending · **Date:** 2026-09-03 · **Repos touched:**
`portfolio-nlp`, `portfolio-financial-analysis`, `portfolio-knowledge-graph`

This note captures the concerns surfaced while reviewing the `portfolio-nlp`
system map, so that when the SEMANTIC-score feature is built the pieces land in
the right repo the first time.

---

## Proposed decision

SEMANTIC becomes an externally-fed score dimension, sourced — like the others —
from the repo that owns the *method*:

- **`portfolio-nlp` computes and owns the semantic signal.** A new aggregation
  stage rolls its per-article `article_sentiment` (optionally weighted by
  `article_category`) into a per-`(asset, day)` measure, stored in its RESULTS
  store (`nlp.db`), point-in-time by construction.
- **`portfolio-financial-analysis` consumes it read-only.** New `KG_NLP_DB`
  opened `mode=ro`, mirroring the `urls.db` / `universe.db` seams. `cycle`
  normalizes the measure cross-sectionally, maps it to the 0–100
  `score_snapshot` scale, **materializes a local `score_snapshot[SEMANTIC]` row**
  with its own `run_id` / `as_of` / `code_version` provenance, and folds it into
  the blend (weight `0.1` today, `cycle/config.py`).
- **`portfolio-knowledge-graph` stops writing `score_snapshot[SEMANTIC]`.** It
  returns to being a pure downstream consumer of `v_*`. The dependency cycle
  (KG writing a table `cycle` reads back) is removed.

**Why this shape:** consistent with the system split — `nlp` = meaning,
`financial-analysis` = reasoning, `knowledge-graph` = triples — reuses the
established read-only-DB seam, and eliminates the write-back cycle.

---

## Where things stand today (2026-09-03)

| Piece | Today | Target |
|---|---|---|
| Per-article sentiment | `nlp` stage 1 — FinBERT → `article_sentiment` (shipped) | unchanged |
| Article → asset attribution | `urls.db.discovered_urls.ticker` (data-mining's extractor); `nlp` NER emits ORG spans, not tickers | contracted; a named owner for `ticker` ↔ `asset_id` |
| Per-`(asset, day)` semantic score | **nobody builds it** — assigned to the integration repo on paper (`nlp` diagram caption, this repo's `docs/README.md` line 27), never implemented | **new `nlp` stage** → a table in `nlp.db` |
| 0–100 `score_snapshot[SEMANTIC]` | integration repo writes it (per `README.md` "Knowledge-graph projection layer") | `financial-analysis` writes it from the `nlp` measure |
| `cycle` blend consumption | `latest_semantic_score()` already reads pre-existing `score_snapshot[SEMANTIC]` rows, weight `0.1` | reader unchanged; the *source* of those rows changes |
| Point-in-time / as-of | `nlp` has no `--analysis-date`, no `as_of` — processes all pending rows | daily time-series rows, filtered `event_time <= D` downstream |

---

## Worries

Each: **what**, **why it bites**, **where it must be solved**, **mitigation**.

### Group A — `portfolio-nlp` does not yet produce what's needed

**W1 — the aggregation stage does not exist.**
`nlp` ships per-article `article_sentiment` / `article_category` only. There is
no per-`(asset, day)` roll-up, no scale, no time-decay, no article-count
normalization. The `nlp` diagram explicitly delegates that to the integration
repo — which never built it either.
*Where:* `portfolio-nlp` — a new stage 6 (`semantic_score` / `article_asset_score`)
writing a new table in the RESULTS store.
*Mitigation:* treat it as new `nlp` work with its own doc, tests, and idempotent
/ resumable contract like stages 1–5. Do **not** let it drift into `cycle`.

**W2 — article → asset attribution is not `nlp`'s today.**
The ticker comes from data-mining's `discovered_urls.ticker` (one ticker per
URL — coarse: an article comparing two names is attributed to one). `nlp`'s NER
produces `article_entities` ORG spans, not resolved tickers. This repo keys on
`asset_id`; `nlp` / data-mining speak `ticker`.
*Why it bites:* the aggregation needs a reliable, documented article→asset key,
and someone must own `ticker` ↔ `asset_id`.
*Where:* decide explicitly — options: (a) trust `discovered_urls.ticker` as-is;
(b) `nlp` resolves NER spans to tickers via a mapping table (new owner needed);
(c) `nlp` emits `ticker`, `financial-analysis` resolves to `asset_id` against
its own `assets` table on read.
*Mitigation:* pick (c) unless NER-level attribution is a stated goal — it keeps
identity resolution in the repo that owns `assets`.

**W3 — `nlp` is not point-in-time / analysis-date aware.**
Every agent here bounds ingestion so nothing dated after `--analysis-date` is
written; the no-lookahead guarantee is load-bearing. `nlp` has no `as_of`
concept.
*Where:* `portfolio-nlp` — emit the score as an append-only **daily time series**
(`event_time` = the day), recomputable, so any D reconstructs by filtering
`event_time <= D`. Preferable to an `--as-of` query param.
*Mitigation:* `financial-analysis` still defensively filters `event_time <= D`
and treats a NULL `pub_date` article as excluded, matching
`entity_resolution`'s rule.

### Group B — signal quality & reproducibility

**W4 — `nlp` has NO accuracy measurement.** (its own `sev-crit` gap)
No labelled eval set, no NER span-F1, no category macro-F1, no sentiment
agreement. Quality is asserted in prose.
*Why it bites:* a 10%-weight portfolio input would be an unvalidated signal;
bad picks driven by it would surface as a `financial-analysis` problem.
*Where:* `portfolio-nlp` — its plan step 2 (labelled eval + `metrics` in CI).
*Mitigation:* **gate** — keep the SEMANTIC blend weight at `0` (schema slot
present, contribution zero) until `nlp` ships the eval; document SEMANTIC as
provisional in `cycle.md` until then.

**W5 — `nlp` model revisions are unpinned.** (`setup.py` fetches HF models by
name, not commit SHA)
A silent upstream model update changes sentiment → changes the score → changes
rankings, with no version bump anywhere. This repo's `code_version` git tag
does **not** capture an upstream model change.
*Where:* `portfolio-nlp` — pin SHAs (its plan step 5).
*Mitigation:* the score rows carry `model_version` + `score_method_version`;
`financial-analysis` records both on the local `score_snapshot[SEMANTIC]` row
next to its own `code_version`, so a replay is fully identified.

**W9 — category taxonomy coupling.**
If the score weights news categories (e.g. earnings > ESG), that weighting is a
`financial-analysis` judgment but the 10-category taxonomy is `nlp`'s
(RavenPack / SASB / IPTC / Refinitiv-sourced). A taxonomy change shifts the
score's meaning.
*Where:* decide the contract shape — (a) `nlp` collapses to a single scalar per
`(asset, day)` (clean boundary, weighting baked into `nlp`); (b) `nlp` exposes
per-category sentiment and `financial-analysis` does the weighting (judgment
stays here, wider contract).
*Mitigation:* start with (a) plus a versioned `score_method_version`; revisit if
category weighting becomes a research lever.

### Group C — integration mechanics

**W6 — no freshness / cadence contract.**
`nlp` has no scheduled cadence even for the mandatory chain. `cycle` runs
"as of D" and needs SEMANTIC populated through D.
*Why it bites:* `cycle` runs before `nlp` has processed articles up to D →
silent staleness in the blend.
*Where:* `portfolio-nlp` documents a run cadence; `cycle` adds a freshness
check (max processed `pub_date` vs D → warn/fail, like the `coverage` gate).

**W7 — the data-mining → `nlp` shape underneath is uncontracted.** (`nlp`'s own
`sev-part` gap: only `body_text` presence is checked, not `gics_*`, `pub_date`)
The aggregation will lean on `pub_date` (as-of), possibly `gics_*` (sector), and
`discovered_urls.ticker` (attribution). A new consumer (`financial-analysis`)
now transitively depends on that uncontracted interface.
*Where:* `portfolio-nlp` + `portfolio-data-mining` — pin the `articles` /
`discovered_urls` columns the aggregation needs as part of this work.

**W8 — `nlp.db` has no identity tables + env-var convention.**
`nlp.db` (RESULTS) holds a "lean articles subset" with no `assets` / `sectors`.
Reads come back ticker-keyed.
*Where:* `financial-analysis` — a thin read adapter (mirror
`entity_resolution/news_db.py`), new `KG_NLP_DB` env var, `connect()` opened
`mode=ro`, ticker→`asset_id` resolved against local `assets` (see W2).
*Mitigation:* read **only** `nlp.db` RESULTS, never `nlp`'s SOURCE — the SOURCE
side carries the WAL/`ATTACH` fragility from `nlp`'s gap list (W15).

**W12 — persist locally vs live-read (replay).**
If `cycle` reads `nlp.db` live and never persists, "what did we believe as of D"
needs `nlp.db` at that exact state too — but `nlp.db` is a mutable serving
store, not point-in-time-versioned.
*Where:* `financial-analysis` — **materialize** a local `score_snapshot[SEMANTIC]`
row at `cycle` time (stamped `run_id` / `as_of` / `code_version` +
`model_version` / `score_method_version`). The write is then this repo's own,
so `KG_FINANCIAL_DB` keeps its "no external writer" property and single-DB
replay.

**W13 — `nlp` is GPU / `torch` heavy.**
Not an import problem (this repo only reads a DB), but an operational one:
producing SEMANTIC for a new date needs a GPU box running the FinBERT pipeline.
*Where:* the (still-unbuilt) cross-module orchestrator — sequence becomes
`pricing → fundamental → entity_resolution → nlp pipeline (GPU) → cycle`.
Document that SEMANTIC has a heavier precondition than the other dimensions.

**W14 — coverage policy for thin names.**
SEMANTIC coverage = universe members with ≥ N articles in the window. Quiet
weeks / small caps → NULL or low-confidence score.
*Where:* `financial-analysis` — extend the `coverage` command with a SEMANTIC
check; `cycle` needs a documented blend-degradation rule (renormalize remaining
weights vs. impute neutral vs. confidence floor). Requires `nlp` to emit
`article_count` + an aggregate confidence per `(asset, day)`.

### Group D — cutover

**W11 — the integration repo must actually stop writing SEMANTIC.**
`README.md` and `docs/README.md` (line 27) both still name the integration repo
as the writer. If `nlp`-derived and KG-derived rows coexist, the blend can
double-count or pick the wrong one.
*Where:* all three repos —
- `kg_schema` docs + the `score_snapshot` "Written by" column;
- remove the KG write path and update the KG repo's docs/diagram;
- add a `score_method` discriminator on `score_snapshot[SEMANTIC]` for a clean
  cutover window.

---

## Placement map

| Capability | Repo | Concrete home |
|---|---|---|
| Per-article sentiment / category | `portfolio-nlp` | stages 1 & 3 (shipped) |
| Article → `(asset, day)` aggregation, decay, article-count norm, confidence | `portfolio-nlp` | new stage → `nlp.db` table (append-only daily series) |
| `model_version` / `score_method_version` stamping, SHA-pinned models, labelled eval | `portfolio-nlp` | its plan steps 2 & 5 |
| `articles` / `discovered_urls` column contract | `portfolio-nlp` + `portfolio-data-mining` | preflight check + doc |
| Read adapter over `nlp.db` (`KG_NLP_DB`, `mode=ro`) | `portfolio-financial-analysis` | new module, mirrors `entity_resolution/news_db.py` |
| `ticker` → `asset_id` resolution | `portfolio-financial-analysis` | against local `assets` on read |
| Cross-sectional normalization, 0–100 mapping, blend weight, degradation policy | `portfolio-financial-analysis` | `cycle` (`data.py`, `config.py`) |
| Local `score_snapshot[SEMANTIC]` materialization + provenance | `portfolio-financial-analysis` | `cycle` write path |
| `coverage` + freshness check for SEMANTIC | `portfolio-financial-analysis` | `coverage` command, `cycle` preflight |
| Remove SEMANTIC write-back; `score_method` discriminator | `portfolio-knowledge-graph` + `kg_schema` | KG code/docs + schema doc |
| RDF projection of `score_snapshot[SEMANTIC]` via `v_*` | `portfolio-knowledge-graph` | unchanged — reads the view like any other score |

---

## Contract to define (`nlp` → `financial-analysis`)

A versioned doc, same discipline as the `v_*` read contract. Minimum fields on
each per-`(asset, day)` row:

| Field | Notes |
|---|---|
| asset key | `ticker` (string) + which namespace; resolution to `asset_id` is the consumer's job |
| `event_time` | the calendar day the score is *about* |
| `value` | the score |
| scale + direction | e.g. `[-1, 1]`, higher = more bullish — **stated, not implied** |
| `article_count` | inputs in the window — drives the coverage / degradation policy |
| `confidence` | aggregate confidence for the day |
| `model_version` | HF commit SHA(s) of the sentiment (and category) models |
| `score_method_version` | version of the aggregation logic itself |
| `computed_at` | when the row was written |
| missing convention | no row vs. explicit NULL for a covered-but-silent name |

Point-in-time guarantee: a row for `event_time = d` aggregates only articles with
`pub_date <= d` (NULL `pub_date` excluded).

---

## Open questions to settle before coding

1. Scalar per `(asset, day)`, or per-category vector? (W9)
2. `ticker` ↔ `asset_id` owner: consumer-side resolution (recommended), or an
   `nlp` mapping table? (W2)
3. Append-only daily time series (recommended), or an `nlp` `--as-of` query? (W3)
4. Does `financial-analysis` persist a local `score_snapshot[SEMANTIC]` row
   (recommended) or read `nlp.db` live? (W12)
5. Minimum `article_count` / confidence floor, and the blend degradation rule
   when SEMANTIC is absent for a name? (W14)
6. Gate: SEMANTIC weight stays `0` until `nlp` has a labelled eval — agreed? (W4)

---

## Rollout sequence

1. **Contract + column pins.** Write the `nlp → financial-analysis` contract
   doc; pin the `articles` / `discovered_urls` columns in `nlp` +
   `portfolio-data-mining`.
2. **`portfolio-nlp`.** Aggregation stage → `nlp.db` daily-series table;
   `model_version` / `score_method_version` stamping; SHA-pinned models;
   labelled eval + `metrics` in CI.
3. **`portfolio-financial-analysis`.** `KG_NLP_DB` read adapter (mirror
   `entity_resolution`); `ticker` → `asset_id` resolution; `cycle` materializes
   `score_snapshot[SEMANTIC]` with provenance; `coverage` + freshness checks;
   blend degradation policy. Weight stays `0`.
4. **`portfolio-knowledge-graph` + `kg_schema`.** Remove the SEMANTIC write
   path; add the `score_method` discriminator; update `README.md`,
   `docs/README.md` (line 27), `docs/kg_schema.md`, and the KG repo's diagram.
5. **Flip the weight** `0` → `0.1` once the `nlp` eval exists and coverage on
   the point-in-time universe is acceptable.
