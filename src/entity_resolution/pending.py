"""``python -m entity_resolution pending``: is the stored edge set stale? (T-017)

Read-only (``connect_ro`` on the database and on ``urls.db``), no write. One line,
``pending: yes|no <evidence>``.

``replace_edges`` deletes every edge of its method before inserting, so the stored set belongs
to whichever run wrote it last and carries no date of its own. Let R be the newest
``ENTITY_RESOLUTION`` ``cycle_run`` (``started_at``, then ``id``). Pending iff any of:

1. there is no R;
2. R is not ``completed`` (a killed build);
3. R was built for another date than D;
4. R recorded no ``news_watermark`` (a run from before T-017), or built with other tuning
   (``min_weight``/``max_tickers``/``min_articles``) than the orchestrator's defaults;
5. an edge of this method was written by a run other than R;
6. the news now holds something R did not see: ``news_watermark`` (see
   :func:`entity_resolution.news_db.news_watermark`) differs from the one R recorded, or is
   unavailable (a table whose ``id`` is not a rowid key).

With no news database the answer is ``no`` ("no news source configured"): the orchestrator skips
the step before it asks.
"""

from __future__ import annotations

import json
from pathlib import Path

from portfolio_common.db import Database, DatabaseError

from entity_resolution.config import Settings
from entity_resolution.db import METHOD
from entity_resolution.news_db import news_watermark
from entity_resolution.pipeline import RunParams
from kg_schema.db import connect_ro
from kg_schema.queries import symbols_asof

_TUNING = ("min_weight", "max_tickers", "min_articles")


def _stale_run(  # noqa: PLR0911 - one return per way the stored build can be stale
    conn: Database, analysis_date: str
) -> tuple[int | None, dict[str, object], str | None]:
    """``(run id, params, why)``: *why* is set when the newest run cannot be the build for
    *analysis_date* (clauses 1 to 5)."""
    if not conn.relation_exists("cycle_run"):
        return None, {}, "no cycle_run table (fresh database)"
    run = conn.execute(
        "SELECT id, cycle_date, status, params_json FROM cycle_run "
        "WHERE cycle_type = 'ENTITY_RESOLUTION' ORDER BY started_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if run is None:
        return None, {}, "no ENTITY_RESOLUTION run recorded"
    rid = int(run["id"])
    if run["status"] != "completed":
        return rid, {}, f"run {rid} is {run['status']}, not completed"
    if run["cycle_date"] != analysis_date:
        return rid, {}, f"run {rid} was built for {run['cycle_date']}, not {analysis_date}"
    try:
        recorded = json.loads(run["params_json"] or "{}")
    except ValueError:
        recorded = {}
    if not isinstance(recorded, dict) or "news_watermark" not in recorded:
        return rid, {}, f"run {rid} recorded no news watermark (built before T-017)"
    defaults = vars(RunParams())
    if any(recorded.get(k) != defaults[k] for k in _TUNING):
        return rid, {}, f"run {rid} was built with other tuning than the orchestrator's"
    if conn.relation_exists("shared_executive_edge"):
        foreign = conn.execute(
            "SELECT 1 FROM shared_executive_edge WHERE method = ? "
            "AND (run_id IS NULL OR run_id <> ?) LIMIT 1",
            (METHOD, rid),
        ).fetchone()
        if foreign is not None:
            return rid, {}, f"edges of {METHOD} written by a run other than {rid}"
    return rid, recorded, None


def check(
    conn: Database, news: Database, tickers: list[str], analysis_date: str
) -> tuple[bool, str]:
    rid, recorded, why = _stale_run(conn, analysis_date)
    if why is not None:
        return True, why
    live = news_watermark(news, tickers, until=analysis_date)
    if "unavailable" in live:
        return True, str(live["unavailable"])
    if live != recorded["news_watermark"]:
        return True, f"the news changed since run {rid}: {recorded['news_watermark']} -> {live}"
    return False, f"run {rid} for {analysis_date} saw the current news {live}"


def pending_line(settings: Settings, analysis_date: str) -> str:
    news_path = Path(settings.news_db_path)
    if not news_path.exists():
        return f"pending: no no news source configured ({news_path} not found)"
    if not Path(settings.db_path).exists():
        return f"pending: yes {settings.db_path} does not exist (fresh database)"
    conn = connect_ro(settings.db_path)
    universe = connect_ro(settings.universe_db_path)
    news = connect_ro(news_path)
    try:
        tickers = symbols_asof(universe, analysis_date)
        try:
            pending, evidence = check(conn, news, tickers, analysis_date)
        except DatabaseError as exc:  # a partial schema is a P2 doubt
            return f"pending: yes cannot read the database ({exc})"
        return f"pending: {'yes' if pending else 'no'} {evidence}"
    finally:
        news.close()
        universe.close()
        conn.close()
