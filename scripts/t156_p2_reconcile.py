"""T-156 phase 1 — probe 2: reconcile the plan's 228 / 216 / 128 against stored inputs."""

from __future__ import annotations

import json

from cycle import data
from kg_schema.versions import resolve_metric_versions

DB = "/workspaces/thesis/scratch/t156/work.db"
UNIVERSE = "SP500"
UNIVERSE_DB = "/workspaces/thesis/data/universe_history.db"
DATE = "2026-09-22"


def main() -> None:
    conn = data.connect_ro(DB)
    versions = resolve_metric_versions(conn, None)
    rows = data.active_universe(conn, UNIVERSE, DATE, UNIVERSE_DB)
    ids = [int(r["id"]) for r in rows]
    metrics = data.latest_metrics(conn, DATE, versions)
    dq = data.data_quality(conn, DATE, versions)
    applied = dq.apply(metrics)

    out: dict[str, int] = {}
    out["members"] = len(ids)
    out["ebitda_present_raw"] = sum(
        1 for a in ids if metrics.get(a, {}).get("leverage.ebitda") is not None
    )
    out["ebitda_present_dq"] = sum(
        1 for a in ids if applied.get(a, {}).get("leverage.ebitda") is not None
    )
    out["ebitda_quarantined"] = sum(
        1 for a in ids if "leverage.ebitda" in dq.quarantined.get(a, set())
    )

    # stored inputs for the newest filing: which members have the leverage row at all, and its inputs
    rows2 = conn.execute(
        """
        WITH known AS (
            SELECT f.id, f.asset_id, ROW_NUMBER() OVER (
                PARTITION BY f.asset_id ORDER BY f.period_end DESC, f.available_at DESC, f.id DESC
            ) AS rn
            FROM sec_filings f
            WHERE f.period_end IS NOT NULL AND f.available_at IS NOT NULL AND f.available_at <= ?
        )
        SELECT k.asset_id, s.form, s.accession_number, s.period_end, s.fiscal_year, s.fiscal_period,
               m.value, m.inputs_json
        FROM known k
        JOIN sec_filings s ON s.id = k.id
        LEFT JOIN fundamental_metrics m
          ON m.filing_id = k.id AND m.metric_group='leverage' AND m.metric_name='ebitda'
             AND m.engine_version='metrics-v7'
        WHERE k.rn = 1
        """,
        (DATE,),
    ).fetchall()
    out["newest_rows"] = len(rows2)
    empty = [r for r in rows2 if r["value"] is None]
    out["empty_value"] = len(empty)
    out["empty_no_row"] = sum(1 for r in rows2 if r["inputs_json"] is None)
    out["empty_with_row"] = sum(1 for r in empty if r["inputs_json"] is not None)

    # among empty, how many have each input missing
    miss_op = miss_da = 0
    for r in empty:
        ij = json.loads(r["inputs_json"] or "{}")
        if ij.get("operating_income") is None:
            miss_op += 1
        if ij.get("depreciation_amortization") is None:
            miss_da += 1
    out["empty_miss_operating_income"] = miss_op
    out["empty_miss_depreciation_amortization"] = miss_da
    # count on the metric present set as well (sanity)
    print(json.dumps(out, indent=1))
    conn.close()


if __name__ == "__main__":
    main()
