"""T-156 phase 1 — probe: environment, versions, the headline empty-EBITDA count.

Run from the t156 worktree with its own UV_PROJECT_ENVIRONMENT. Read-only on work.db.
"""

from __future__ import annotations

import json
import sqlite3
import sys

from cycle import data
from kg_schema.versions import METRIC_GROUPS, resolve_metric_versions

DB = "/workspaces/thesis/scratch/t156/work.db"
UNIVERSE = "SP500"
UNIVERSE_DB = "/workspaces/thesis/data/universe_history.db"
DATE = "2026-09-22"


def main() -> None:
    conn = data.connect_ro(DB)
    versions = resolve_metric_versions(conn, None)
    out: dict[str, object] = {}
    out["metric_groups"] = list(METRIC_GROUPS)
    out["resolved_versions"] = versions.by_group
    out["versions_json"] = versions.json_param()
    rows = data.active_universe(conn, UNIVERSE, DATE, UNIVERSE_DB)
    out["members"] = len(rows)
    ids = [int(r["id"]) for r in rows]
    metrics = data.latest_metrics(conn, DATE, versions)
    out["members_with_metrics"] = len(metrics)
    ebitda_present = sum(1 for a in ids if metrics.get(a, {}).get("leverage.ebitda") is not None)
    out["ebitda_present"] = ebitda_present
    out["ebitda_empty"] = len(ids) - ebitda_present
    ndte = sum(1 for a in ids if metrics.get(a, {}).get("leverage.net_debt_to_ebitda") is not None)
    out["net_debt_to_ebitda_present"] = ndte
    print(json.dumps(out, indent=1, default=str))
    # assets
    c2 = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    out["assets_total"] = c2.execute("select count(*) from assets").fetchone()[0]
    out["filings_total"] = c2.execute("select count(*) from sec_filings").fetchone()[0]
    c2.close()
    conn.close()
    print(
        json.dumps(
            {"assets_total": out["assets_total"], "filings_total": out["filings_total"]}, indent=1
        )
    )


if __name__ == "__main__":
    sys.exit(main())
