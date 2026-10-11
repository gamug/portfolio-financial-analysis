"""T-149 verification on scratch databases (docs/model_fixes.md): nothing here touches production.

    uv run python scripts/verify_t149.py triggers --prod DB --out /tmp/t149/prod_copy.db
    uv run python scripts/verify_t149.py pilot    --prod DB --out /tmp/t149/pilot.db [--analysis-date D]
    uv run python scripts/verify_t149.py n2       --db /tmp/t149/pilot.db --cycle-date D
    uv run python scripts/verify_t149.py coreg    --db DB --tickers DUK,SO,... [--year 2025]
    uv run python scripts/verify_t149.py twitter  --out /tmp/t149/twitter.db

``--prod`` is opened ``mode=ro`` (``triggers`` copies it first and reads the copy; the copy's sha1 is
checked against the original's before and after). Every write goes to ``--out``.

* ``triggers`` -- the identity triggers (m012) against every stored row of a production copy: the
  copy is migrated to the current contract (which refuses it if a row breaks the invariant), every row
  is re-inserted and re-updated under the triggers inside a transaction that is rolled back, and the
  negative controls (a filing of an asset with no CIK, an accession given to an asset of another CIK)
  are shown to abort.
* ``pilot`` -- a facts-only rebuild of the 20-name pilot: the pipeline with an analyst that computes
  every metric and scores with the rule-based fallback, **no LLM call**. Prices come from the
  production copy for the same names, so the price-based metrics exist as they did in the pilot.
* ``n2`` -- the earnings yield's effect on VALORIZATION, apart from every data-treatment change:
  the same database, the same cycle date, with the trailing net income read (this fix) or withheld
  (what every real cycle did before).
* ``compare`` -- the stored metrics of the same names, an earlier pilot database against the rebuilt one.
* ``coreg`` -- ID-10 across the live combined filings of a list of tickers (read-only, gateway only).
* ``twitter`` -- one company that left the index, ingested by CIK alone (the path ``T-153`` takes).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

from cycle import data as cycle_data
from cycle.config import CycleSettings
from cycle.orchestrator import run_selection
from fundamental_agent import db, pipeline
from fundamental_agent.agents import AnalysisResult, FundamentalAnalyst, _fallback_assessment
from fundamental_agent.config import Settings
from fundamental_agent.edgar_client import EdgarClient
from fundamental_agent.pipeline import RunParams
from fundamental_agent.statements import REGISTRY, Statements, _parse_period
from kg_schema import connect, ensure, identity, queries
from kg_schema.queries import UniverseMember
from kg_schema.versions import MetricVersions, resolve_metric_versions
from pricing_agent import db as pricing_db
from pricing_agent.observations import build_observations

PILOT_TICKERS = [
    "MCD",
    "WAT",
    "XOM",
    "PG",
    "T",
    "NEE",
    "MA",
    "CPT",
    "UDR",
    "ESS",
    "SBAC",
    "APO",
    "WFC",
    "HUM",
    "HOOD",
    "APA",
    "BF.B",
    "PM",
    "STZ",
    "PSX",
]
SHOWN = 12  # rows listed per category
PRICE_TABLES = ("price_daily", "price_window", "price_observation", "corporate_action")


def sha1(path: Path) -> str:
    h = hashlib.sha1()  # noqa: S324 - a file fingerprint, not a security boundary
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def copy_prod(prod: Path, out: Path) -> None:
    before = sha1(prod)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(prod, out)
    assert sha1(out) == before, "the copy differs from production"
    print(f"copied {prod} -> {out} (sha1 {before})")


def _replay(conn: Any) -> tuple[int, int, int]:
    """Every stored filing re-written under the triggers: an UPDATE of the guarded columns, then each
    row deleted and put back. ``foreign_keys`` off so a delete cascades nowhere; rolled back."""
    conn.execute("PRAGMA foreign_keys = OFF")
    rejected_update = rejected_insert = 0
    columns = [r[1] for r in conn.execute("PRAGMA table_info(sec_filings)")]
    rows = conn.execute("SELECT * FROM sec_filings ORDER BY id").fetchall()
    conn.execute("BEGIN")
    try:
        for r in rows:
            try:
                conn.execute(
                    "UPDATE sec_filings SET asset_id = asset_id, accession_number = accession_number WHERE id = ?",
                    (r["id"],),
                )
            except sqlite3.IntegrityError as exc:
                rejected_update += 1
                print("  UPDATE rejected:", r["id"], exc)
        placeholders = ", ".join("?" * len(columns))
        # the insert path is fed by one row at a time: delete it, put it back
        for r in rows:
            conn.execute("DELETE FROM sec_filings WHERE id = ?", (r["id"],))
            try:
                conn.execute(
                    f"INSERT INTO sec_filings ({', '.join(columns)}) VALUES ({placeholders})",  # noqa: S608 - the table's own columns
                    tuple(r[c] for c in columns),
                )
            except sqlite3.IntegrityError as exc:
                rejected_insert += 1
                print("  INSERT rejected:", r["id"], exc)
    finally:
        conn.execute("ROLLBACK")
    return len(rows), rejected_update, rejected_insert


def _controls(conn: Any) -> None:
    """Negative controls: the writes the triggers exist to refuse, in a transaction rolled back."""
    conn.execute("BEGIN")
    try:
        a1, a2 = conn.execute(
            "SELECT a.id FROM assets a WHERE a.cik IS NOT NULL ORDER BY a.id LIMIT 2"
        ).fetchall()
        accession = conn.execute(
            "SELECT accession_number FROM sec_filings WHERE asset_id = ? LIMIT 1", (a1["id"],)
        ).fetchone()[0]
        for label, sql, params in (
            (
                "an accession given to an asset of another CIK",
                "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, accession_number, "
                "retrieved_at) VALUES (?, '10-K', 1999, 'FY1999', ?, 'now')",
                (a2["id"], accession),
            ),
            (
                "a filing of an asset with no CIK",
                "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, retrieved_at) "
                "VALUES ((SELECT MAX(id) + 1 FROM assets), '10-K', 1999, 'FY1999', 'now')",
                (),
            ),
        ):
            try:
                if "MAX(id) + 1" in sql:
                    conn.execute(
                        "INSERT INTO assets (id, ticker) VALUES ((SELECT MAX(id) + 1 FROM assets), 'ZZNOCIK')"
                    )
                conn.execute(sql, params)
                print(f"  CONTROL FAILED -- accepted: {label}")
            except sqlite3.IntegrityError as exc:
                print(f"  control ok -- {label}: ABORT ({exc})")
        try:
            conn.execute("UPDATE assets SET cik = '0000000001' WHERE id = ?", (a1["id"],))
            print("  CONTROL FAILED -- accepted: a new CIK for an asset with filings")
        except sqlite3.IntegrityError as exc:
            print(f"  control ok -- a new CIK for an asset with filings: ABORT ({exc})")
    finally:
        conn.execute("ROLLBACK")


def cmd_triggers(args: argparse.Namespace) -> int:
    prod, out = Path(args.prod), Path(args.out)
    before = sha1(prod)
    copy_prod(prod, out)
    conn = connect(out)
    print(f"schema_version of the copy before: {queries.current_version(conn)}")
    found = identity.violations(conn)
    print(
        f"violations before the triggers: no-CIK {len(found.no_cik)}, cross-CIK {len(found.cross_cik)}"
    )
    applied = ensure(
        conn, run_migrations=True
    )  # m012 refuses the copy if a row breaks the invariant
    print(
        f"migrations applied to the copy: {applied}; schema_version now {queries.current_version(conn)}"
    )
    names = sorted(
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    )
    print(
        "identity triggers present:",
        [n for n in names if n.startswith(("trg_sf_cik", "trg_sf_cross", "trg_assets_cik"))],
    )

    n_filings = conn.execute("SELECT COUNT(*) FROM sec_filings").fetchone()[0]
    shared = conn.execute(
        "SELECT f.accession_number, COUNT(DISTINCT f.asset_id) n, COUNT(DISTINCT a.cik) ciks, "
        "GROUP_CONCAT(DISTINCT a.ticker) tickers FROM sec_filings f JOIN assets a ON a.id = f.asset_id "
        "WHERE f.accession_number IS NOT NULL GROUP BY f.accession_number HAVING COUNT(DISTINCT f.asset_id) > 1"
    ).fetchall()
    cross = [r for r in shared if r["ciks"] > 1]
    nocik = conn.execute(
        "SELECT COUNT(*) FROM sec_filings f JOIN assets a ON a.id = f.asset_id WHERE COALESCE(a.cik,'') = ''"
    ).fetchone()[0]
    print(
        f"stored filings {n_filings}; accessions on more than one asset {len(shared)} "
        f"(of one CIK: {len(shared) - len(cross)}, of several: {len(cross)}); filings of an asset with no CIK {nocik}"
    )
    print("  share classes:", sorted({r["tickers"] for r in shared}))

    n_rows, rejected_update, rejected_insert = _replay(conn)
    print(
        f"re-updated {n_rows} rows: {rejected_update} rejected; "
        f"re-inserted {n_rows} rows: {rejected_insert} rejected"
    )
    _controls(conn)
    conn.close()
    assert sha1(prod) == before, "PRODUCTION CHANGED"
    print(f"production sha1 unchanged: {before}")
    return 0 if not (rejected_update or rejected_insert or cross or nocik) else 1


class _FactsOnlyAnalyst:
    """The pipeline's analyst without the LLM: every metric is computed, the score is the
    deterministic rule-based fallback (``agents._fallback_assessment``). No network, no spend."""

    def __init__(self, _model: object, model_name: str) -> None:
        self._inner = FundamentalAnalyst.__new__(FundamentalAnalyst)
        self._inner._model = None
        self._inner.model_name = model_name
        self.model_name = model_name

    def analyze(self, ctx: Any) -> Any:
        computed = self._inner._compute_all(ctx)
        flat = {f"{g}.{r.name}": r.value for g, r in computed}
        return AnalysisResult(
            assessment=_fallback_assessment(flat),
            metrics=computed,
            flat_metrics=flat,
            used_fallback=True,
            prompt_hash="facts-only-no-llm",
        )


def _members_of(prod: Path, tickers: list[str]) -> list[UniverseMember]:
    """UniverseMembers for *tickers* from production's assets (read-only): symbol, name, CIK, sector."""
    conn = sqlite3.connect(f"file:{prod}?mode=ro", uri=True)
    rows = [
        conn.execute(
            "SELECT a.ticker, a.company_name, a.cik, s.name, a.sub_industry FROM assets a "
            "LEFT JOIN sectors s ON s.id = a.sector_id WHERE a.ticker = ?",
            (ticker,),
        ).fetchone()
        for ticker in tickers
    ]
    return [
        UniverseMember(t, name or t, cik, sector, sub, None, None, None, "2022-01-01", None)
        for t, name, cik, sector, sub in (r for r in rows if r is not None)
    ]


def cmd_pilot(args: argparse.Namespace) -> int:
    prod, out = Path(args.prod), Path(args.out)
    before = sha1(prod)
    if out.exists():
        raise SystemExit(f"{out} exists; remove it first (a rebuild starts from nothing)")
    out.parent.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LLM_API_KEY", "unused-facts-only")
    os.environ.setdefault("LLM_MODEL", "unused-facts-only")
    os.environ.setdefault("LLM_URL", "http://unused.invalid")
    settings = Settings(
        db_path=out,
        universe_db_path=Path(os.environ["KG_UNIVERSE_DB"]),
        llm_api_key="unused-facts-only",
        llm_model="facts-only-no-llm",
        llm_url="http://unused.invalid",
        edgar_base_url=os.environ["EDGAR_BASE_URL"],
    )
    # 1. the universe's assets, then the pilot names' prices from production (read-only)
    conn = connect(out)
    db.ensure_schema(conn)
    tickers = args.tickers.split(",") if args.tickers else list(PILOT_TICKERS)
    if (
        args.members_from_prod
    ):  # a cohort outside the pilot universe: the production assets, as members
        members = _members_of(prod, tickers)
        pipeline._load_members = lambda _s, _d: members  # type: ignore[assignment]
    else:
        members = pipeline._load_members(settings, args.analysis_date)
    db.sync_universe(conn, [m for m in members if m.symbol in tickers])
    pricing_db.ensure_schema(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(f"ATTACH DATABASE 'file:{prod}?mode=ro' AS prod")
    for table in () if args.no_prices else PRICE_TABLES:
        if not conn.execute("SELECT 1 FROM prod.sqlite_master WHERE name = ?", (table,)).fetchone():
            continue
        cols = [r[1] for r in conn.execute(f"PRAGMA main.table_info({table})")]
        pcols = {r[1] for r in conn.execute(f"PRAGMA prod.table_info({table})")}
        use = [c for c in cols if c in pcols and c not in ("id", "asset_id")]
        n = conn.execute(
            f"INSERT INTO main.{table} (asset_id, {', '.join(use)}) "  # noqa: S608 - table names are our own constants
            f"SELECT m.id, {', '.join('p.' + c for c in use)} FROM prod.{table} p "
            "JOIN prod.assets pa ON pa.id = p.asset_id JOIN main.assets m ON m.ticker = pa.ticker"
        ).rowcount
        print(f"  prices: {table}: {n} rows copied")
    conn.commit()
    conn.execute("DETACH DATABASE prod")
    conn.close()

    # 2. the pipeline, facts-only
    pipeline.FundamentalAnalyst = _FactsOnlyAnalyst  # type: ignore[misc,assignment]
    pipeline.build_model = lambda _s: None  # type: ignore[assignment]
    params = RunParams(
        forms=("10-K", "10-Q"),
        since_year=2022,
        tickers=tickers,
        analysis_date=args.analysis_date,
        fresh=True,
    )
    report = pipeline.run(settings, params)
    print(
        f"run {report.run_id}: planned {report.planned}, completed {report.completed}, "
        f"skipped {report.skipped}, failed {report.failed}"
    )
    for line in report.errors:
        print("  ERR", line[:300])
    assert sha1(prod) == before, "PRODUCTION CHANGED"
    print(f"production sha1 unchanged: {before}")
    return 0


def _cycle(
    db_path: Path, cycle_date: str, *, with_earnings_yield: bool
) -> dict[str, dict[str, Any]]:
    """One cycle over a copy of the pilot, the trailing net income read or withheld. Returns, per
    ticker, the value factor, the VALORIZATION score and the blended rank."""
    conn = connect(db_path)
    ticker_of = {
        int(r["id"]): str(r["ticker"]) for r in conn.execute("SELECT id, ticker FROM assets")
    }
    # priceobs-v2 from the stored daily bars, as pricing_agent run --observations would write them
    for aid in ticker_of:
        history = pricing_db.load_daily_candles(conn, aid, end=cycle_date)
        pricing_db.upsert_price_observations(
            conn,
            aid,
            build_observations(history, engine_version=pricing_db.PRICE_OBSERVATION_ENGINE_VERSION),
        )
    conn.commit()
    real = cycle_data.latest_trailing_net_income
    if not with_earnings_yield:  # what every real cycle did before T-149
        cycle_data.latest_trailing_net_income = lambda *_a, **_k: {}  # type: ignore[assignment]
    try:
        run_selection(CycleSettings(db_path=db_path, top_n=10), cycle_date, conn=conn)
    finally:
        cycle_data.latest_trailing_net_income = real  # type: ignore[assignment]
    step = json.loads(
        conn.execute(
            "SELECT detail_json FROM cycle_checkpoint WHERE step = 'valorization'"
        ).fetchone()[0]
    )
    out: dict[str, dict[str, Any]] = {"_step": step}
    for r in conn.execute(
        "SELECT asset_id, raw_value, normalized_score, inputs_json FROM score_snapshot "
        "WHERE score_type = 'VALORIZATION' AND event_time = ?",
        (cycle_date,),
    ):
        parts = json.loads(r["inputs_json"])
        out[ticker_of[int(r["asset_id"])]] = {
            "value": parts.get("value"),
            "valorization": r["raw_value"],
            "valorization_norm": r["normalized_score"],
        }
    for r in conn.execute(
        "SELECT cr.asset_id, cr.rank, cr.blended_score FROM cycle_ranking cr JOIN cycle_run c "
        "ON c.id = cr.cycle_run_id WHERE c.cycle_date = ?",
        (cycle_date,),
    ):
        out.setdefault(ticker_of[int(r["asset_id"])], {}).update(
            {"rank": r["rank"], "blended": r["blended_score"]}
        )
    conn.close()
    return out


def cmd_n2(args: argparse.Namespace) -> int:
    src = Path(args.db)
    on, off = Path(args.out_dir) / "pilot_n2_on.db", Path(args.out_dir) / "pilot_n2_off.db"
    for path in (on, off):
        shutil.copyfile(src, path)
    with_ey = _cycle(on, args.cycle_date, with_earnings_yield=True)
    without = _cycle(off, args.cycle_date, with_earnings_yield=False)
    print(f"valorization step, with the fix:    {with_ey.pop('_step')}")
    print(f"valorization step, before the fix:  {without.pop('_step')}")
    conn = connect(on)
    ni = data_net_income(conn, args.cycle_date)
    conn.close()
    rows = []
    for t in sorted(with_ey):
        a, b = with_ey[t], without.get(t, {})
        rows.append(
            {
                "ticker": t,
                "trailing_ni": ni.get(t),
                "value_before": b.get("value"),
                "value_after": a.get("value"),
                "valorization_before": b.get("valorization"),
                "valorization_after": a.get("valorization"),
                "rank_before": b.get("rank"),
                "rank_after": a.get("rank"),
            }
        )
    Path(args.out_dir, "n2_effect.json").write_text(json.dumps(rows, indent=1))
    print(
        "| ticker | trailing NI (M) | value factor before -> after | VALORIZATION before -> after | rank before -> after |"
    )
    print("|---|---:|---|---|---|")
    fmt = lambda v, k=1.0: "-" if v is None else f"{v * k:,.3f}"  # noqa: E731
    for r in rows:
        ni_m = "-" if r["trailing_ni"] is None else f"{r['trailing_ni'] / 1e6:,.0f}"
        print(
            f"| {r['ticker']} | {ni_m} | {fmt(r['value_before'])} -> {fmt(r['value_after'])} | "
            f"{fmt(r['valorization_before'])} -> {fmt(r['valorization_after'])} | "
            f"{r['rank_before']} -> {r['rank_after']} |"
        )
    moved = [r for r in rows if r["rank_before"] != r["rank_after"]]
    print(f"names whose blended rank moved: {len(moved)} of {len(rows)}")
    return 0


def data_net_income(conn: Any, cycle_date: str) -> dict[str, float]:
    versions: MetricVersions = resolve_metric_versions(conn, None)
    by_id = cycle_data.latest_trailing_net_income(conn, cycle_date, versions)
    names = {int(r["id"]): str(r["ticker"]) for r in conn.execute("SELECT id, ticker FROM assets")}
    return {names[a]: v for a, v in by_id.items()}


def cmd_coreg(args: argparse.Namespace) -> int:
    """ID-10 over the gateway's live combined filings: does any resolved item equal a value held only
    by a legal-entity-member row? (Matched by period date, so a balance-sheet instant and a duration
    column of one date are compared as the resolver compares them.)"""
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    total_leaks = 0
    with EdgarClient(os.environ["EDGAR_BASE_URL"]) as edgar:
        for ticker in args.tickers.split(","):
            cik = conn.execute("SELECT cik FROM assets WHERE ticker = ?", (ticker,)).fetchone()[0]
            refs = [r for r in edgar.filing_by_year(cik, "10-K", args.year) if r.form == "10-K"]
            payload = edgar.financials(
                cik, "10-K", args.year, accession_number=refs[0].accession_number
            )
            entity: dict[str, set[float]] = {}
            consolidated: dict[str, set[float]] = {}
            n_entity_rows = 0
            for statement in ("income_statement", "balance_sheet", "cash_flow"):
                for row in payload[statement]:
                    legal = (
                        bool(row.get("dimension"))
                        and row.get("dimension_axis") == "dei:LegalEntityAxis"
                    )
                    n_entity_rows += legal
                    for column, value in row.items():
                        period = _parse_period(str(column))
                        if period is not None and isinstance(value, (int, float)):
                            (entity if legal else consolidated).setdefault(period.date, set()).add(
                                float(value)
                            )
            stmts = Statements.from_payload(payload)
            leaks = [
                (item, p.key, v)
                for item in REGISTRY
                for p in stmts.periods
                if (v := stmts.get(item, p.key)) is not None
                and v in entity.get(p.date, set())
                and v not in consolidated.get(p.date, set())
            ]
            total_leaks += len(leaks)
            print(
                f"{ticker:<5} {refs[0].accession_number}  legal-entity rows {n_entity_rows:>4}  leaks {leaks}"
            )
    return 1 if total_leaks else 0


def cmd_compare(args: argparse.Namespace) -> int:
    """What T-149's data-treatment changes did to the stored metrics of the same names: the earlier
    pilot database (read-only) against the rebuilt one, filing by filing on (ticker, form, period
    end), metric by metric on (group, name) in the current engine version of each."""
    old = sqlite3.connect(f"file:{args.old}?mode=ro", uri=True)
    new = sqlite3.connect(f"file:{args.new}?mode=ro", uri=True)

    def load(
        conn: sqlite3.Connection,
    ) -> tuple[dict[tuple[str, str, str], str], dict[tuple[str, str, str, str, str], float | None]]:
        filings = {
            (r[0], r[1], r[2]): r[3]
            for r in conn.execute(
                "SELECT a.ticker, f.form, f.period_end, f.accession_number FROM sec_filings f "
                "JOIN assets a ON a.id = f.asset_id"
            )
        }
        version = conn.execute(
            "SELECT engine_version FROM fundamental_metrics GROUP BY engine_version ORDER BY MAX(id) DESC LIMIT 1"
        ).fetchone()[0]
        metrics = {
            (r[0], r[1], r[2], r[3], r[4]): r[5]
            for r in conn.execute(
                "SELECT a.ticker, f.form, f.period_end, m.metric_group, m.metric_name, m.value "
                "FROM fundamental_metrics m JOIN sec_filings f ON f.id = m.filing_id "
                "JOIN assets a ON a.id = f.asset_id WHERE m.engine_version = ?",
                (version,),
            )
        }
        return filings, metrics

    (f_old, m_old), (f_new, m_new) = load(old), load(new)
    tickers = {k[0] for k in f_new} & {k[0] for k in f_old}
    f_old = {k: v for k, v in f_old.items() if k[0] in tickers}
    m_old = {k: v for k, v in m_old.items() if k[0] in tickers}
    only_old, only_new = sorted(set(f_old) - set(f_new)), sorted(set(f_new) - set(f_old))
    print(
        f"names compared {len(tickers)}; filings old {len(f_old)}, new {len(f_new)}, in both {len(set(f_old) & set(f_new))}"
    )
    print("  only in the earlier pilot:", only_old[:SHOWN], "..." if len(only_old) > SHOWN else "")
    print("  only in the rebuilt pilot:", only_new[:SHOWN], "..." if len(only_new) > SHOWN else "")
    print(
        "  accession differs on the same period:",
        [k for k in set(f_old) & set(f_new) if f_old[k] != f_new[k]][:10],
    )
    changed: dict[str, int] = {}
    filled = emptied = same = 0
    examples: list[tuple[Any, ...]] = []
    for key in set(m_old) & set(m_new):
        a, b = m_old[key], m_new[key]
        if a is None and b is None:
            same += 1
        elif a is None:
            filled += 1
        elif b is None:
            emptied += 1
        elif abs(a - b) > 1e-9 * max(1.0, abs(a), abs(b)):
            changed[f"{key[3]}.{key[4]}"] = changed.get(f"{key[3]}.{key[4]}", 0) + 1
            if len(examples) < SHOWN:
                examples.append((*key, a, b))
        else:
            same += 1
    print(
        f"metric rows in both: {same + filled + emptied + sum(changed.values())}: unchanged {same}, "
        f"empty -> value {filled}, value -> empty {emptied}, value changed {sum(changed.values())}"
    )
    for name, n in sorted(changed.items(), key=lambda kv: -kv[1])[:15]:
        print(f"    changed {n:>4}  {name}")
    for e in examples:
        print("    e.g.", e)
    return 0


def cmd_twitter(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if out.exists():
        raise SystemExit(f"{out} exists; remove it first")
    out.parent.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        db_path=out,
        universe_db_path=Path(os.environ["KG_UNIVERSE_DB"]),
        llm_api_key="unused-facts-only",
        llm_model="facts-only-no-llm",
        llm_url="http://unused.invalid",
        edgar_base_url=os.environ["EDGAR_BASE_URL"],
    )
    # The asset a T-153 historical universe would hand over: Twitter's ticker no longer resolves,
    # its CIK does. Built directly (universe_history.db is read by T-153, not here).
    twitter = UniverseMember(
        "TWTR",
        "Twitter, Inc.",
        "1418091",
        "Communication Services",
        "Interactive Media & Services",
        None,
        None,
        None,
        "2013-11-07",
        "2022-10-28",
    )
    pipeline._load_members = lambda _s, _d: [twitter]  # type: ignore[assignment]
    pipeline.FundamentalAnalyst = _FactsOnlyAnalyst  # type: ignore[misc,assignment]
    pipeline.build_model = lambda _s: None  # type: ignore[assignment]
    report = pipeline.run(
        settings,
        RunParams(
            forms=("10-K",),
            since_year=2022,
            until_year=2022,
            analysis_date="2022-06-30",
            fresh=True,
        ),
    )
    print(
        f"run {report.run_id}: completed {report.completed}, skipped {report.skipped}, failed {report.failed}"
    )
    for line in report.errors:
        print("  ERR", line[:300])
    conn = connect(out)
    for r in conn.execute(
        "SELECT a.ticker, a.cik, f.form, f.fiscal_period, f.period_end, f.filing_date, f.accession_number, "
        "(SELECT COUNT(*) FROM financial_facts x WHERE x.filing_id = f.id) facts, "
        "(SELECT COUNT(*) FROM fundamental_metrics m WHERE m.filing_id = f.id) metrics "
        "FROM sec_filings f JOIN assets a ON a.id = f.asset_id"
    ):
        print("  stored:", dict(r))
    print(
        "  ticker 'TWTR' at the gateway: Company not found (the capture is tests/fixtures/edgar_filing_identity_responses.json)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("--old", required=True)
    cmp_.add_argument("--new", required=True)
    cmp_.set_defaults(func=cmd_compare)
    co = sub.add_parser("coreg")
    co.add_argument("--db", default=os.environ.get("KG_FINANCIAL_DB"))
    co.add_argument("--tickers", required=True)
    co.add_argument("--year", type=int, default=2025)
    co.set_defaults(func=cmd_coreg)
    n2 = sub.add_parser("n2")
    n2.add_argument("--db", required=True)
    n2.add_argument("--out-dir", required=True)
    n2.add_argument("--cycle-date", default="2026-09-22")
    n2.set_defaults(func=cmd_n2)
    for name, func in (("triggers", cmd_triggers), ("pilot", cmd_pilot), ("twitter", cmd_twitter)):
        p = sub.add_parser(name)
        p.add_argument("--out", required=True)
        if name != "twitter":
            p.add_argument("--prod", default=os.environ.get("KG_FINANCIAL_DB"))
        if name == "pilot":
            p.add_argument("--analysis-date", default="2026-09-22")
            p.add_argument("--tickers", help="comma-separated; default: the 20 pilot names")
            p.add_argument("--members-from-prod", action="store_true")
            p.add_argument("--no-prices", action="store_true")
        p.set_defaults(func=func)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
