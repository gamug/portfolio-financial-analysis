"""SECTOR score: each asset's TECHNICAL strength relative to its GICS sector.

``SectorAggregateSnapshot`` rolls up members' TECHNICAL score per sector per
cycle; ``SectorRelativeMomentum`` (``score_snapshot`` type ``SECTOR``) is the
asset's own TECHNICAL raw minus that sector mean -- negative = lagging the sector.
Both are deterministic derivations of data already in the DB (TECHNICAL scores +
``assets.sector_id``); nothing new is fetched.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

SCORE_TYPE = "SECTOR"


@dataclass(frozen=True)
class SectorAggregate:
    sector_id: int
    member_count: int
    mean_raw: float
    mean_normalized: float | None


def roll_up(
    sector_of: dict[int, int | None],
    technical_raw: dict[int, float],
    technical_norm: dict[int, float],
) -> tuple[list[SectorAggregate], dict[int, float]]:
    """Return ``(per-sector aggregates, per-asset momentum)``.

    ``momentum[asset_id]`` is on the raw TECHNICAL scale: own raw minus the mean
    raw of its sector's members. Assets with no sector, or with no TECHNICAL
    score this cycle, are dropped.
    """
    members: dict[int, list[int]] = {}
    for asset_id, sector_id in sector_of.items():
        if sector_id is None or asset_id not in technical_raw:
            continue
        members.setdefault(int(sector_id), []).append(asset_id)

    aggregates: list[SectorAggregate] = []
    momentum: dict[int, float] = {}
    for sector_id, asset_ids in sorted(members.items()):
        raws = [technical_raw[a] for a in asset_ids]
        norms = [technical_norm[a] for a in asset_ids if a in technical_norm]
        mean_raw = statistics.fmean(raws)
        aggregates.append(
            SectorAggregate(
                sector_id=sector_id,
                member_count=len(asset_ids),
                mean_raw=mean_raw,
                mean_normalized=statistics.fmean(norms) if norms else None,
            )
        )
        for a in asset_ids:
            momentum[a] = technical_raw[a] - mean_raw
    return aggregates, momentum
