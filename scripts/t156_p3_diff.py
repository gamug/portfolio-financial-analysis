"""T-156 phase 1 — probe 3: the 272/275 disagreement between latest_metrics and a direct row read."""

from __future__ import annotations

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
    via_reader = {a for a in ids if metrics.get(a, {}).get("leverage.ebitda") is not None}

    rows2 = conn.execute(
        """
        WITH known AS (
            SELECT f.id, f.asset_id, ROW_NUMBER() OVER (
                PARTITION BY f.asset_id ORDER BY f.period_end DESC, f.available_at DESC, f.id DESC
            ) AS rn
            FROM sec_filings f
            WHERE f.period_end IS NOT NULL AND f.available_at IS NOT NULL AND f.available_at <= ?
        )
        SELECT k.asset_id, k.id AS filing_id, s.form, s.accession_number, s.period_end, m.value
        FROM known k JOIN sec_filings s ON s.id = k.id
        LEFT JOIN fundamental_metrics m ON m.filing_id = k.id AND m.metric_group='leverage'
             AND m.metric_name='ebitda' AND m.engine_version='metrics-v7'
        WHERE k.rn = 1
        """,
        (DATE,),
    ).fetchall()
    via_row = {int(r["asset_id"]) for r in rows2 if r["value"] is not None}
    print("reader present", len(via_reader), "row present", len(via_row))
    print("in reader not row", len(via_reader - via_row))
    for r in rows2:
        a = int(r["asset_id"])
        if (a in via_reader) != (a in via_row):
            vals = metrics.get(a, {})
            print(
                "DIFF asset",
                a,
                "row_value",
                r["value"],
                "form",
                r["form"],
                "acc",
                r["accession_number"],
                "pe",
                r["period_end"],
                "filing_id",
                r["filing_id"],
                "reader_ebitda",
                vals.get("leverage.ebitda"),
                "reader_keys",
                sorted(k for k in vals if k.startswith("leverage.")),
            )
    conn.close()


if __name__ == "__main__":
    main()
